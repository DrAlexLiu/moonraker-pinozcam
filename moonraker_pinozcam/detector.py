"""The detection loop: frames in, decisions out.

Structured as one thread that owns the frame source and the inference
backend, so nothing else has to reason about their lifetimes. It runs only
while a print is actually in progress -- there is nothing to detect on an
idle printer, and the detector is memory-bandwidth heavy, which is exactly
what gcode streaming also needs.
"""

import threading
import time

from . import annotate, camera, cpu_affinity, framesource, mask, nozcam_backend
from .window import FailureWindow

# One tick of the loop. It is independent of the frame source's own rate: a source may produce
# faster, and wait_next()'s sequence argument makes the loop skip what it
# missed rather than fall behind.
# Sampling cadence comes from frame_sample_interval; see Detector._refresh.

# How long to wait for a frame before calling the camera missing.
FRAME_WAIT = 5.0
CAMERA_OFFLINE_AFTER = 30.0

# A frame alarms at severity >= 0.5, i.e. affected area >= 2% of the
# content rect at the default 0.04 sensitivity.


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
        self._fired = False                # one episode, not one per frame
        self._paused_by_switch = False
        self.last_result = None
        # The one line worth putting in front of the user when detection is
        # not working. Setup failure used to be logged and nothing else, so
        # the page showed "AI not started" with no reason and the user had
        # to find the journal to learn the backend was missing.
        self.last_error = None
        # Edge state for the camera watch, matching the OctoPrint build.
        self.camera_ok_at = 0.0
        self.camera_alerted = False
        # Kept so a /check command can answer with a real photo rather than
        # fetching its own, which would race the detection loop for the
        # camera and cost an extra second.
        self.last_jpeg = None
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
            self._fired = False
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
        last_seq = -1
        self.camera_ok_at = time.monotonic()
        self.camera_alerted = False

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
            frame = self._source.wait_next(last_seq, self._stop, FRAME_WAIT)

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
            last_seq = frame.sequence

            # Minimum interval between checks, a brake for slow hosts.
            if (self._detection_interval
                    and tick - self._last_check_at < self._detection_interval):
                self._pace(tick)
                continue
            self._last_check_at = tick

            try:
                self._score(frame)
            except Exception as exc:                         # noqa: BLE001
                self._log.error("Frame scoring failed: %s", exc)
                self.last_error = "Frame scoring failed: %s" % exc
            else:
                self.last_error = None

            self._pace(tick)

        self._teardown()
        self._log.info("Detection stopped")

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
            self._fired = False
            self._log.info(
                "Detection criteria changed (%s); window and warm-up reset "
                "-- results measured on the old scale are not comparable.",
                ", ".join(changed))

    def _pace(self, tick_started):
        """Sleep out the remainder of this tick, interruptibly."""
        remaining = self._sample_interval - (time.monotonic() - tick_started)
        if remaining > 0:
            self._stop.wait(remaining)

    def _score(self, frame):
        """Infer on one frame and feed the verdict into the window."""
        from io import BytesIO
        from PIL import Image

        self.last_jpeg = frame.jpeg_bytes
        image = Image.open(BytesIO(frame.jpeg_bytes)).convert("RGB")
        # Before the mask and before inference: see camera.transform_image.
        image = camera.transform_image(image, self.camera_source,
                                       logger=self._log)

        # Mask before inference, not after: painting the ignored region
        # black means the model never sees it, so nothing there can score.
        # Filtering detections afterwards would still let a masked region
        # influence the letterbox content rect and the severity fraction.
        scored_image = (mask.apply_to_image(image, self._mask)
                        if not mask.is_empty(self._mask) else image)

        scores, boxes, labels, severity, pct_area, elapsed = \
            self._backend.infer(scored_image, self._score_threshold,
                                self._sensitivity, self._cpus)

        # ⚠️ The OctoPrint build's rule, verbatim: the AREA against the
        # sensitivity, not the severity against a half. severity is
        # area/sensitivity clamped to [0,1], so `severity >= 0.5` -- what
        # this used to say -- fires at HALF the configured area and made
        # this build twice as trigger-happy as the one it is ported from.
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
                self._view.publish(
                    annotate.annotate(scored_image, boxes, scores, severity))
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
        if not met:
            if self._fired:
                self._log.info(
                    "Failure criterion cleared (%.0f%%); armed again.",
                    ratio * 100)
            self._fired = False
        elif not self._fired:
            self._log.warning(
                "FAILURE: %.0f%% of the last %ds alarmed (threshold %.0f%%)",
                ratio * 100, self.window.count_time,
                self.window.failure_ratio * 100)
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
            self._fired = bool(handled)
            if not handled:
                self._log.warning(
                    "The failure action did not complete; staying armed so "
                    "the next frame can try again.")
