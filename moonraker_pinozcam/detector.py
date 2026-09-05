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
SAMPLE_INTERVAL = 0.2   # overridden per-config by frame_sample_interval

# How long to wait for a frame before calling the camera missing.
FRAME_WAIT = 5.0
CAMERA_OFFLINE_AFTER = 30.0

# A frame alarms at severity >= 0.5, i.e. affected area >= 2% of the
# content rect at the default 0.04 sensitivity.
ALARM_SEVERITY = 0.5


class Detector(object):
    """Owns one detection run for one print."""

    def __init__(self, config, client, logger, on_failure=None,
                 on_frame=None, view=None):
        self._cfg = config
        self._client = client
        self._log = logger
        self._on_failure = on_failure      # called once per escalation
        self._on_frame = on_frame          # called for every scored frame
        self._view = view                  # AnnotatedView, or None

        d = config.detection
        self._score_threshold = d["scores_threshold"]
        self._sensitivity = d["img_sensitivity"]
        self._start_delay = d["ai_start_delay"]
        self._cpu_share = d["cpu_share"]
        self._cpus = None          # resolved once, at setup
        self._mask = config.camera.get("mask_image_data") or ""

        self.window = FailureWindow(
            count_time=d["count_time"], failure_ratio=d["failure_ratio"])

        self._thread = None
        self._stop = threading.Event()
        self._source = None
        self._backend = None
        self._fired = False                # one action per print, not per frame
        self.last_result = None
        # Kept so a /check command can answer with a real photo rather than
        # fetching its own, which would race the detection loop for the
        # camera and cost an extra second.
        self.last_jpeg = None

    # ---- lifecycle ----------------------------------------------------

    def start(self):
        """Start detecting. Safe to call when already running."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
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
        spec = framesource.SourceSpec(
            "snapshot", "pinozcam", src.snapshot_url, None)
        self._source = framesource.HttpSnapshotFrameSource(
            spec, logger=self._log)
        self._source.start()

        self._backend = nozcam_backend.NozcamBackend(
            plugin_dir=None, logger=self._log, backend="auto")
        self._log.info("Inference backend ready: %s",
                       self._backend.describe())

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
            self._teardown()
            return

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
        last_frame_at = time.monotonic()
        camera_warned = False

        while not self._stop.is_set():
            tick = time.monotonic()
            frame = self._source.wait_next(last_seq, self._stop, FRAME_WAIT)

            if frame is None:
                # Report a missing camera once, not every tick.
                if (time.monotonic() - last_frame_at > CAMERA_OFFLINE_AFTER
                        and not camera_warned):
                    camera_warned = True
                    self._log.warning(
                        "No camera frame for %ds -- detection is blind",
                        int(CAMERA_OFFLINE_AFTER))
                self._pace(tick)
                continue

            if camera_warned:
                camera_warned = False
                self._log.info("Camera is back; detection resumed")
            last_seq = frame.sequence
            last_frame_at = time.monotonic()

            try:
                self._score(frame)
            except Exception as exc:                         # noqa: BLE001
                self._log.error("Frame scoring failed: %s", exc)

            self._pace(tick)

        self._teardown()
        self._log.info("Detection stopped")

    def _pace(self, tick_started):
        """Sleep out the remainder of this tick, interruptibly."""
        remaining = SAMPLE_INTERVAL - (time.monotonic() - tick_started)
        if remaining > 0:
            self._stop.wait(remaining)

    def _score(self, frame):
        """Infer on one frame and feed the verdict into the window."""
        from io import BytesIO
        from PIL import Image

        self.last_jpeg = frame.jpeg_bytes
        image = Image.open(BytesIO(frame.jpeg_bytes)).convert("RGB")

        # Mask before inference, not after: painting the ignored region
        # black means the model never sees it, so nothing there can score.
        # Filtering detections afterwards would still let a masked region
        # influence the letterbox content rect and the severity fraction.
        scored_image = (mask.apply_to_image(image, self._mask)
                        if not mask.is_empty(self._mask) else image)

        scores, boxes, labels, severity, pct_area, elapsed = \
            self._backend.infer(scored_image, self._score_threshold,
                                self._sensitivity, self._cpus)

        alarming = severity >= ALARM_SEVERITY
        self.window.add(alarming)
        met, ratio = self.window.decide()
        self.last_result = {
            "severity": severity, "percentage_area": pct_area,
            "boxes": boxes, "scores": scores, "elapsed": elapsed,
            "ratio": ratio, "alarming": alarming,
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

        # Fire once per print. Re-alarming every frame after the threshold
        # is crossed would spam a user who has already been told.
        if met and not self._fired:
            self._fired = True
            self._log.warning(
                "FAILURE: %.0f%% of the last %ds alarmed (threshold %.0f%%)",
                ratio * 100, self.window.count_time,
                self.window.failure_ratio * 100)
            if self._on_failure is not None:
                try:
                    self._on_failure(self.last_result)
                except Exception as exc:                     # noqa: BLE001
                    self._log.error("failure handler raised: %s", exc)
