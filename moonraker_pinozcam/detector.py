"""The detection loop: frames in, decisions out.

Structured as one thread that owns the frame source and the inference
backend, so nothing else has to reason about their lifetimes. It runs only
while a print is actually in progress -- there is nothing to detect on an
idle printer, and the detector is memory-bandwidth heavy, which is exactly
what gcode streaming also needs.
"""

import threading
import time
from io import BytesIO

from PIL import Image

from . import (annotate, camera, cpu_affinity, framebuffer, framesource, mask,
               nozcam_backend)
from .window import FailureWindow

# Sampling cadence comes from frame_sample_interval; see Detector._refresh.

# How long to wait for a frame before calling the camera missing.
FRAME_WAIT = 5.0
CAMERA_OFFLINE_AFTER = 30.0

# How long the consumer waits for a candidate. An empty buffer means "the
# next frame is on its way", never "fetch one yourself" -- the sampler is
# the only camera reader.
CONSUMER_WAIT = 1.0

# The centre crop sharpness is measured on, from the OctoPrint build.
SHARPNESS_CROP = (512, 512)


class Detector(object):
    """Owns one detection run for one print."""

    def __init__(self, config, client, logger, on_failure=None,
                 on_frame=None, view=None, on_notice=None):
        self._cfg = config
        self._client = client
        self._log = logger
        self._on_failure = on_failure      # called once per escalation
        self._on_frame = on_frame          # called for every scored frame
        self._view = view                  # AnnotatedView, or None
        self._on_notice = on_notice        # operational notice -> the bots

        d = config.detection
        # ⚠️ These are re-read every tick by _refresh(), not cached for the
        # life of the run. They used to be read once here, so saving a
        # threshold, a mask or the master switch did nothing until the
        # service was restarted -- while the page said "Saved."
        self._score_threshold = d["scores_threshold"]
        self._sensitivity = d["img_sensitivity"]
        self._sample_interval = d["frame_sample_interval"]
        self._detection_interval = d["detection_interval"]
        self._enabled = d["enable_ai"]
        self._cpu_share = d["cpu_share"]
        self._mask = config.camera.get("mask_image_data") or ""
        # Read once per RUN, like the OctoPrint build: both need a new
        # backend or a new thread, so they take effect at the next print.
        self._start_delay = d["ai_start_delay"]
        self._ai_backend = d.get("ai_backend") or "auto"
        self._cpus = None          # resolved once, at setup
        self._last_check_at = 0.0

        self.window = FailureWindow(
            count_time=d["count_time"], failure_ratio=d["failure_ratio"])

        self._thread = None
        self._stop = threading.Event()
        self._source = None
        self._backend = None
        # The action level already carried out this episode, or None
        # while nothing has. Not a boolean -- see _decide.
        self._fired_level = None
        self._paused_by_switch = False
        self._buffer = None
        self._buffer_max_age = None
        self._mask_fits = True
        self.last_result = None
        # The one line worth putting in front of the user when detection is
        # not working. Setup failure used to be logged and nothing else, so
        # the page showed "AI not started" with no reason and the user had
        # to find the journal to learn the backend was missing.
        # ⚠️ Two different lifetimes, so two fields. last_error is
        # TRANSIENT -- a frame that would not score, a camera gone quiet --
        # and the next good frame clears it. last_warning is a STANDING
        # condition a good frame does not resolve, such as a mask that no
        # longer fits the camera. Sharing one field meant the mask warning
        # was raised and then wiped by the very next successful frame.
        self.last_error = None
        self.last_warning = None
        # Edge state for the camera watch, matching the OctoPrint build.
        self.camera_ok_at = 0.0
        self.camera_alerted = False
        # Kept so a /check command can answer with a real photo rather than
        # fetching its own, which would race the detection loop for the
        # camera and cost an extra second.
        self.backend_name = None
        self.camera_source = None

    # ---- lifecycle ----------------------------------------------------

    def start(self, fresh=True):
        """Start detecting. Safe to call when already running.

        `fresh=False` keeps the failure window and the episode latch --
        that is a RESUME, not a new job. Discarding them on resume threw
        away the history the ratio is computed from and re-armed an episode
        the user had already been told about.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        if fresh:
            self._fired_level = None
            self.window.reset()
        self._thread = threading.Thread(
            target=self._run, name="pinozcam-detect", daemon=True)
        self._thread.start()

    def stop(self, timeout=15.0):
        """Stop detecting and release the camera and the daemon."""
        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._teardown()

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # ---- setup / teardown ---------------------------------------------

    def _setup(self):
        """Resolve the camera and start the inference backend."""
        src = camera.resolve(self._cfg.camera, self._client, logger=self._log)
        # Kept so the page can offer a Live Camera toggle pointing at the
        # SAME camera the detector watches -- never a different one.
        self.camera_source = src
        self._source = camera.build_frame_source(src, logger=self._log)
        self._source.start()

        d = self._cfg.detection
        self._buffer_max_age = d["frame_buffer_max_age"]
        self._buffer = framebuffer.FrameBuffer(
            capacity=d["frame_buffer_capacity"],
            max_age=d["frame_buffer_max_age"])

        self._backend = nozcam_backend.NozcamBackend(
            plugin_dir=None, logger=self._log, backend=self._ai_backend)
        self.backend_name = self._backend.describe()
        self._log.info("Inference backend ready: %s", self.backend_name)

        # Which cores the daemon may use. Resolved from the live topology
        # rather than from a core count, because a heterogeneous board's
        # LITTLE cores are not interchangeable with its big ones.
        #
        # Extra cores buy very little on the in-order A53/A55 hosts this
        # runs on -- measured 2->4 cores is +15% on an A55 and +8% on an
        # A53 -- so leaving one for Klipper costs almost nothing.
        try:
            share = max(0.01, min(1.0, self._cpu_share))
            topology = cpu_affinity.detect_cpu_topology(
                cpu_affinity.read_cpu_topology())
            selection = cpu_affinity.select_ai_cpus(share, topology)
            self._cpus = selection.cpus
            self._log.info("Detector CPUs: %s", selection.description)
        except Exception as exc:                             # noqa: BLE001
            # Not fatal: an unknown topology means "leave affinity alone",
            # which is what the daemon does with an empty list.
            self._log.warning("Could not select CPUs (%s); leaving "
                              "affinity unchanged", exc)
            self._cpus = None

    def _teardown(self):
        for obj, what in ((self._source, "camera"), (self._backend, "backend")):
            if obj is None:
                continue
            try:
                obj.close() if what == "camera" else obj.stop()
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("error closing %s: %s", what, exc)
        self._source = self._backend = None

    # ---- the loop -----------------------------------------------------

    def _run(self):
        try:
            self._setup()
        except Exception as exc:                             # noqa: BLE001
            self._log.error("Detection could not start: %s", exc)
            self.last_error = "Detection could not start: %s" % exc
            self._teardown()
            return
        self.last_error = None

        # aiStartDelay's whole purpose: the first layer looks wrong to a
        # detector trained on established prints, so skip it rather than
        # teach users to raise the threshold.
        if self._start_delay > 0:
            self._log.info("Waiting %ds before detecting (start delay)",
                           self._start_delay)
            if self._stop.wait(self._start_delay):
                self._teardown()
                return

        self._log.info("Detecting")
        self.camera_ok_at = time.monotonic()
        self.camera_alerted = False

        # ⚠️ The sampler is the ONLY camera reader. This loop takes the
        # best candidate it has offered. Before this the loop read frames
        # itself and scored whichever arrived, so frame_buffer_capacity and
        # frame_buffer_max_age were settings with nothing behind them --
        # framebuffer.py was vendored and never instantiated.
        sampler = threading.Thread(target=self._sample, name="pinozcam-grab",
                                   daemon=True)
        sampler.start()

        try:
            self._consume()
        finally:
            # ⚠️ Unconditional. Without it an exception anywhere in the loop
            # left the sampler thread running against a source nobody reads
            # and the inference daemon alive for the life of the service.
            self._stop.set()
            self._buffer.close()
            sampler.join(timeout=5)
            self._teardown()
            self._log.info("Detection stopped")

    def _consume(self):
        """Score the best candidate the sampler offers, until stopped."""
        while not self._stop.is_set():
            tick = time.monotonic()
            self._refresh()
            if not self._enabled:
                # The master switch. Detection stops; the camera, the bots
                # and the annotated view all keep working, which is what
                # "only pauses the analysis" means.
                if not self._paused_by_switch:
                    self._paused_by_switch = True
                    self._log.info("enable_ai is off; not analysing frames.")
                self._pace(tick)
                continue
            if self._paused_by_switch:
                self._paused_by_switch = False
                self._log.info("enable_ai is on again; analysing.")
            # ⚠️ Checked BEFORE taking. take() REMOVES what it returns,
            # so testing the interval afterwards discarded the sharpest
            # candidate the buffer held -- and kept the buffer drained, so
            # the frame eventually scored was whichever single one happened
            # to be there. That defeats the selection the buffer exists for.
            if (self._detection_interval
                    and tick - self._last_check_at < self._detection_interval):
                self._pace(tick)
                continue

            frame = self._buffer.take(timeout=CONSUMER_WAIT)

            if frame is None:
                # One notice per outage, not one per tick.
                if self._camera_watch(False, time.monotonic()) == "offline":
                    self.last_error = (
                        "No camera frame for %ds -- detection is blind."
                        % int(CAMERA_OFFLINE_AFTER))
                    self._notify_camera(
                        "\u26a0\ufe0f Camera lost: no frame for %d s. Print "
                        "failure detection is BLIND until the camera "
                        "returns." % int(CAMERA_OFFLINE_AFTER))
                self._pace(tick)
                continue

            if self._camera_watch(True, time.monotonic()) == "recovered":
                self.last_error = None
                self._notify_camera(
                    "\u26a0\ufe0f Camera is back. Print failure detection "
                    "has resumed.")
            self._last_check_at = tick

            try:
                self._score(frame)
            except Exception as exc:                         # noqa: BLE001
                self._log.error("Frame scoring failed: %s", exc)
                self.last_error = "Frame scoring failed: %s" % exc
            else:
                self.last_error = None

            self._pace(tick)



    def _sample(self):
        """Grab frames, measure them, and offer them to the buffer.

        Sharpness is only measured when the buffer is deep enough for the
        answer to change anything -- with one candidate there is nothing to
        choose between, and the measurement costs a crop, a blur and a
        difference on every frame.
        """
        last_seq = -1
        while not self._stop.is_set():
            tick = time.monotonic()
            frame = self._source.wait_next(last_seq, self._stop, FRAME_WAIT)
            if frame is None:
                self._pace(tick)
                continue
            last_seq = frame.sequence
            try:
                image = Image.open(BytesIO(frame.jpeg_bytes)).convert("RGB")
                # The SOURCE size, captured before the transform -- which is
                # what mask.signature is defined against.
                self._check_mask_fits(image.size)
                image = camera.transform_image(image, self.camera_source,
                                               logger=self._log)
                score = (self._measure_sharpness(image)
                         if self._buffer.should_measure() else float("-inf"))
                self._buffer.put(image, score, tick)
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("could not sample a frame: %s", exc)
            self._pace(tick)

    def _check_mask_fits(self, source_size):
        """Suspend the mask if the camera is no longer the one it was for.

        Blacking out the wrong region is worse than not masking: it can
        hide a real failure and say nothing. So a mismatch disables the
        mask, loudly, and leaves the picture alone.
        """
        stored = (self._cfg.camera.get("mask_signature") or "").strip()
        if not self._mask or not stored:
            self._mask_fits = True
            return
        live = mask.signature(source_size, self.camera_source)
        fits = mask.signature_matches(stored, live)
        if fits == self._mask_fits:
            return
        self._mask_fits = fits
        if fits:
            self._log.info("Undetect zone applies again (%s).", live)
            self.last_warning = None
        else:
            self._log.warning(
                "Undetect zone SUSPENDED: it was painted for %s and the "
                "camera now delivers %s. Repaint it, or restore the old "
                "camera settings.", stored, live)
            self.last_warning = (
                "The undetect zone was painted for a different camera "
                "geometry and is not being applied. Repaint it.")

    def _boxes_to_draw(self, scores, boxes):
        """The (box, score) pairs the annotated view shows.

        ⚠️ The backend returns every detection NMS kept, down to its decode
        floor -- far below the score threshold the detector actually
        counts. Drawing all of them put boxes on the picture that
        contributed nothing to the failure area, in the alert photo and in
        Mainsail's camera list, which is precisely the disagreement
        upstream's boxes_to_draw exists to prevent.

        A `break`, not a filter, because scores arrive sorted descending
        from NMS -- kept identical to upstream, including that a stray low
        score mid-list would end the list there.
        """
        shown = []
        for box, score in zip(boxes or [], scores or []):
            if score < self._score_threshold:
                break
            shown.append((box, score))
        return shown

    @staticmethod
    def _measure_sharpness(image):
        """High-pass energy on a centre crop, without NumPy.

        Copied from the OctoPrint build's camera.measure_sharpness.
        """
        from PIL import ImageChops, ImageFilter, ImageStat
        width, height = image.size
        crop_w = min(width, SHARPNESS_CROP[0])
        crop_h = min(height, SHARPNESS_CROP[1])
        left = (width - crop_w) // 2
        top = (height - crop_h) // 2
        grey = image.crop(
            (left, top, left + crop_w, top + crop_h)).convert("L")
        blurred = grey.filter(ImageFilter.GaussianBlur(1))
        return ImageStat.Stat(ImageChops.difference(grey, blurred)).mean[0]

    def _camera_watch(self, ok, now):
        """Edge-triggered camera-outage detector for the chat channels.

        Returns "offline" once per outage (after CAMERA_OFFLINE_AFTER
        seconds without a frame), "recovered" once when frames return after
        an alert, None otherwise. Same shape and the same constant as the
        OctoPrint build's `_camera_watch`, so the two products behave
        identically when a camera drops mid-print.

        Only the detection loop calls this, so it fires only during a print.
        """
        if ok:
            self.camera_ok_at = now
            if self.camera_alerted:
                self.camera_alerted = False
                return "recovered"
            return None
        if (not self.camera_alerted
                and now - self.camera_ok_at > CAMERA_OFFLINE_AFTER):
            self.camera_alerted = True
            return "offline"
        return None

    def _notify_camera(self, text):
        """One operational notice to every configured medium.

        Mute is respected by the notifier. Deliberately outside the normal
        alert budget: a blind detector is not a print failure, and being
        silenced by max_notification is the opposite of what this is for.
        """
        self._log.warning("%s", text)
        if self._on_notice is None:
            return
        try:
            self._on_notice(text)
        except Exception as exc:                             # noqa: BLE001
            self._log.error("Could not send the camera notice: %s", exc)

    def _refresh(self):
        """Apply any settings change to the running detector.

        Three groups, the same split the OctoPrint build documents:

        * thresholds that MOVE THE LINE (failure_ratio, action, the
          notification limits) apply to the next frame and keep the
          window -- they change the decision, not the measurement;
        * MEANING-CHANGING values (img_sensitivity, scores_threshold,
          count_time) apply to the next frame AND reset the window and
          warm-up, because statistics gathered under the old scale are not
          comparable to ones gathered under the new;
        * ai_start_delay and ai_backend are read once per run, because
          they decide how a run starts.
        """
        if not self._cfg.reload_if_changed():
            return
        d = self._cfg.detection
        self._sample_interval = d["frame_sample_interval"]
        self._detection_interval = d["detection_interval"]
        self._enabled = d["enable_ai"]
        self._cpu_share = d["cpu_share"]
        # ⚠️ Rebuilt, not resized. framebuffer.py is one of the modules
        # kept byte-identical with the OctoPrint build, so it does not grow
        # a setter for this. Losing the few candidates in flight is the
        # right trade for a settings change the user just made.
        if (self._buffer is not None
                and (self._buffer.capacity != d["frame_buffer_capacity"]
                     or self._buffer_max_age != d["frame_buffer_max_age"])):
            self._buffer_max_age = d["frame_buffer_max_age"]
            self._buffer = framebuffer.FrameBuffer(
                capacity=d["frame_buffer_capacity"],
                max_age=d["frame_buffer_max_age"])
            self._log.info("Candidate buffer rebuilt: capacity %d, max age "
                           "%.0fs", d["frame_buffer_capacity"],
                           d["frame_buffer_max_age"])
        mask = self._cfg.camera.get("mask_image_data") or ""
        if mask != self._mask:
            self._mask = mask
            self._log.info("Undetect zone updated (%d cells ignored)",
                           mask.count("1"))
        if d["failure_ratio"] != self.window.failure_ratio:
            self.window.failure_ratio = float(d["failure_ratio"])
            self._log.info("Failure ratio threshold is now %.0f%%",
                           self.window.failure_ratio * 100)
        changed = [name for name, old, new in (
            ("img_sensitivity", self._sensitivity, d["img_sensitivity"]),
            ("scores_threshold", self._score_threshold,
             d["scores_threshold"]),
            ("count_time", self.window.count_time, d["count_time"]),
        ) if old != new]
        if changed:
            self._sensitivity = d["img_sensitivity"]
            self._score_threshold = d["scores_threshold"]
            self.window.count_time = float(d["count_time"])
            self.window.reset()
            self._fired_level = None
            self._log.info(
                "Detection criteria changed (%s); window and warm-up reset "
                "-- results measured on the old scale are not comparable.",
                ", ".join(changed))

    def _pace(self, tick_started):
        """Sleep out the remainder of this tick, interruptibly."""
        remaining = self._sample_interval - (time.monotonic() - tick_started)
        if remaining > 0:
            self._stop.wait(remaining)

    def _score(self, candidate):
        """Infer on one candidate and feed the verdict into the window.

        The image arrives already decoded and already transformed -- the
        sampler does both, because it has to decode anyway to measure
        sharpness.
        """
        image = candidate.image

        # Mask before inference, not after: painting the ignored region
        # black means the model never sees it, so nothing there can score.
        # Filtering detections afterwards would still let a masked region
        # influence the letterbox content rect and the severity fraction.
        scored_image = (mask.apply_to_image(image, self._mask)
                        if self._mask_fits and not mask.is_empty(self._mask)
                        else image)

        scores, boxes, labels, severity, pct_area, elapsed = \
            self._backend.infer(scored_image, self._score_threshold,
                                self._sensitivity, self._cpus)

        # ⚠️ The OctoPrint build's rule, verbatim: the AREA against the
        # sensitivity, not the severity against a half. severity is
        # area/sensitivity clamped to [0,1], so `severity >= 0.5` -- what
        # this used to say -- fires at HALF the configured area and made
        # this build twice as trigger-happy as the one it is ported from.
        # ⚠️ Inference takes hundreds of milliseconds, and a print can end
        # or the service can shut down inside that window. Judge the frame,
        # but do not let a result that arrived after the run was stopped
        # touch the printer -- the OctoPrint build checks the same thing at
        # the same point.
        if self._stop.is_set():
            self._log.info("Run stopped while this frame was being judged; "
                           "discarding it.")
            return

        alarming = pct_area > self._sensitivity
        self.window.add(alarming)
        met, ratio = self.window.decide()
        # Normalised 0..1 so the page can draw them over an image of any
        # displayed size, which is what the OctoPrint build sends too. The
        # pixel boxes stay for the annotator, which works on the real image.
        width, height = scored_image.size
        boxes_norm = [[round(b[0] / width, 5), round(b[1] / height, 5),
                       round(b[2] / width, 5), round(b[3] / height, 5)]
                      for b in boxes] if width and height else []
        self.last_result = {
            "severity": severity, "percentage_area": pct_area,
            "boxes": boxes, "boxes_norm": boxes_norm,
            "scores": [round(float(x), 4) for x in scores],
            "elapsed": elapsed, "ratio": ratio, "alarming": alarming,
        }

        # Publish the annotated frame for the web view. Encoding costs a
        # JPEG per frame, so skip it when nothing is watching.
        if self._view is not None and self._view.enabled:
            try:
                # Annotate the MASKED image so the view shows exactly what
                # the detector saw -- a user looking at an unmasked picture
                # would wonder why an obvious failure was ignored.
                shown = self._boxes_to_draw(scores, boxes)
                self._view.publish(annotate.annotate(
                    scored_image, [b for b, _ in shown],
                    [sc for _, sc in shown], alarming))
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("could not annotate frame: %s", exc)

        if self._on_frame is not None:
            try:
                self._on_frame(image, self.last_result)
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("frame callback raised: %s", exc)

        # One episode, not one alert per frame -- but an EPISODE, which
        # ends when the criterion stops being met. Re-arming here is what
        # makes a second failure later in the same print reportable.
        # ⚠️ A LEVEL, not a flag. The configured action can be raised while
        # a failure is still under way -- "alert only" was enough until the
        # user looked at the picture and now wants it stopped -- and with a
        # boolean that raise did nothing: the episode was already latched,
        # so the printer kept going until the ratio fell back below the
        # threshold and crossed it again. Upstream tracks the same thing as
        # episode_action_level. 0 means nothing has fired this episode.
        want = int(self._cfg.detection.get("action") or 0)
        if not met:
            if self._fired_level is not None:
                self._log.info(
                    "Failure criterion cleared (%.0f%%); armed again.",
                    ratio * 100)
            self._fired_level = None
        elif self._fired_level is None or want > self._fired_level:
            if self._fired_level is None:
                self._log.warning(
                    "FAILURE: %.0f%% of the last %ds alarmed "
                    "(threshold %.0f%%)",
                    ratio * 100, self.window.count_time,
                    self.window.failure_ratio * 100)
            else:
                self._log.warning(
                    "Failure action raised from %d to %d while the failure "
                    "was still under way; acting on the new one.",
                    self._fired_level, want)
            # ⚠️ Latched only AFTER the handler reports success. Setting it
            # first meant one failed pause -- or one Telegram outage -- shut
            # detection up for the rest of the print: the flag stayed set,
            # nothing reset it, and no later frame could ever fire again.
            handled = False
            if self._on_failure is not None:
                try:
                    handled = self._on_failure(self.last_result) is not False
                except Exception as exc:                     # noqa: BLE001
                    self._log.error("failure handler raised: %s", exc)
            else:
                handled = True
            # Latch at the level that actually ran, so raising it again
            # later in the same episode still gets through.
            self._fired_level = want if handled else self._fired_level
            if not handled:
                self._log.warning(
                    "The failure action did not complete; staying armed so "
                    "the next frame can try again.")
