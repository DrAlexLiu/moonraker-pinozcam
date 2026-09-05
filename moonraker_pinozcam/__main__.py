"""Entry point: connect to Moonraker and follow the printer.

Deliberately minimal at this stage. It establishes the process shape the
detector will live in -- config, logging, signals, a Moonraker connection
that survives restarts -- and proves that shape on real hardware before any
inference code depends on it.
"""

import argparse
import io
import logging
import logging.handlers
import os
import signal
import sys
import threading

from .config import Config, ConfigError
from .detector import Detector
from .moonraker import MoonrakerClient
from .notify import Notifier
from .web import AnnotatedView

LOG = logging.getLogger("pinozcam")


def setup_logging(cfg):
    """Log to journald via stderr, and to a file when one is configured."""
    level = getattr(logging, cfg.logging["level"], logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    # systemd captures stderr into the journal, so this is the primary sink.
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    path = cfg.logging["path"]
    if path:
        path = os.path.expanduser(path)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # Rotate: a printer host's filesystem is small, and this service
            # runs for the length of a print farm's day.
            fh = logging.handlers.RotatingFileHandler(
                path, maxBytes=2 * 1024 * 1024, backupCount=3)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            LOG.warning("cannot write log file %s: %s", path, exc)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="moonraker-pinozcam")
    ap.add_argument("-c", "--config", required=True,
                    help="path to moonraker-pinozcam.cfg")
    args = ap.parse_args(argv)

    try:
        cfg = Config(args.config)
    except ConfigError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2

    setup_logging(cfg)
    LOG.info("PiNozCam for Moonraker starting (config: %s)", cfg.path)

    stopping = threading.Event()
    mr_cfg = cfg.moonraker
    client = MoonrakerClient(
        host=mr_cfg["host"], port=mr_cfg["port"], api_key=mr_cfg["api_key"],
        logger=LOG)

    action = (cfg.action["on_failure"] or "pause").lower()

    def latest_jpeg():
        """A fresh frame for /check, or None if detection is not running."""
        d = detector_ref[0]
        return d.last_jpeg if d is not None else None

    def status_line():
        st = client.state
        d = detector_ref[0]
        if d is None or not d.running:
            return "Not detecting. Printer: %s" % st.state
        w = d.window.stats
        return ("Detecting. Printer: %s | window %d frames, %d alarming "
                "(%.0f%%)%s" % (st.state, w["window_frames"], w["alarming"],
                                w["ratio"] * 100,
                                "" if w["armed"] else " | warming up"))

    detector_ref = [None]
    notifier = Notifier(cfg, client, LOG,
                        snapshot=latest_jpeg, status=status_line)

    def on_failure(result):
        """Act once a failure is confirmed."""
        # Re-check state rather than trusting the detector's view: the print
        # may have ended between the last frame and this call, and pausing a
        # finished print would confuse the user more than saying nothing.
        if not client.state.is_printing:
            LOG.info("Failure confirmed but the printer is no longer "
                     "printing (%s); taking no action", client.state.state)
            return
        # Alert first, act second: if pausing fails the user still gets
        # told, and the photo is what makes the alert actionable.
        caption = ("Print failure detected (%.0f%% of the last %ds)"
                   % (result.get("ratio", 0) * 100, cfg.detection["count_time"]))
        try:
            jpeg = latest_jpeg()
            notifier.alert(caption,
                           image=io.BytesIO(jpeg) if jpeg else None)
        except Exception as exc:                             # noqa: BLE001
            LOG.error("Could not send the alert: %s", exc)

        try:
            if action == "pause":
                client.pause_print()
                LOG.warning("Paused the print")
            elif action == "stop":
                client.cancel_print()
                LOG.warning("Cancelled the print")
            else:
                LOG.warning("Failure detected; on_failure=%s, so no printer "
                            "action was taken", action)
        except Exception as exc:                             # noqa: BLE001
            LOG.error("Could not %s the print: %s", action, exc)

    view = AnnotatedView(cfg, client, LOG, detector_ref=detector_ref)
    detector = Detector(cfg, client, LOG, on_failure=on_failure, view=view)
    detector_ref[0] = detector

    def on_state_change(state):
        """Start and stop detecting with the print.

        Detection is tied to the job, not to the service: there is nothing
        to detect on an idle printer, and the camera and daemon should not
        be held open between prints.
        """
        LOG.info("printer: %s", state)
        if state.is_printing and not detector.running:
            LOG.info("Print started -- beginning detection")
            # Invalidate confirmations from the previous job: a button
            # pressed now must not act on the print that just started.
            notifier.new_print()
            detector.start()
        elif not state.is_printing and detector.running:
            LOG.info("Print no longer active (%s) -- stopping detection",
                     state.state)
            detector.stop()

    client._on_state_change = on_state_change

    def shutdown(signum, _frame):
        # Log the signal: an unexplained exit in a journal is hard to chase.
        LOG.info("received %s, shutting down",
                 signal.Signals(signum).name)
        stopping.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    notifier.start()
    client.start()
    view.start()
    if client.wait_until_connected(timeout=30.0):
        try:
            cams = client.list_webcams()
            LOG.info("Moonraker reports %d webcam(s): %s", len(cams),
                     ", ".join(c.get("name", "?") for c in cams) or "none")
        except Exception as exc:                             # noqa: BLE001
            LOG.warning("could not list webcams: %s", exc)
    else:
        # Not fatal: Moonraker may simply be starting later than we did,
        # and the client keeps retrying on its own.
        LOG.warning("Moonraker not reachable yet at %s -- still retrying",
                    client.http_base)

    # The printer may already be printing when this service starts, e.g.
    # after an update restart mid-job.
    if client.state.is_printing:
        LOG.info("A print is already in progress -- beginning detection")
        detector.start()

    LOG.info("running; waiting for print activity")
    while not stopping.wait(1.0):
        pass

    detector.stop()
    view.stop()
    notifier.stop()
    client.stop()
    LOG.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
