"""Entry point: connect to Moonraker and follow the printer.

Deliberately minimal at this stage. It establishes the process shape the
detector will live in -- config, logging, signals, a Moonraker connection
that survives restarts -- and proves that shape on real hardware before any
inference code depends on it.
"""

import argparse
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


def _set_password(config_path):
    """Prompt for the annotated view's password and store it.

    A command rather than a field on the page, deliberately: the page has
    no login until this is set, so offering it there would let anyone who
    can reach the port set one and lock the owner out. Reading it from a
    prompt also keeps it out of the shell history that an argument would
    land in.
    """
    import getpass
    from .config import Config, ConfigError
    try:
        cfg = Config(config_path)
    except ConfigError as exc:
        print("  ! %s" % exc, file=sys.stderr)
        return 1
    current = (cfg.web.get("password") or "").strip()
    user = (cfg.web.get("user") or "pinozcam").strip()
    print("Config : %s" % cfg.path)
    print("User   : %s   (change it with [web] user)" % user)
    print("Status : %s" % ("a password is set" if current else "no password"))
    print("Leave both prompts empty to remove the password.\n")
    try:
        first = getpass.getpass("New password: ")
        again = getpass.getpass("Repeat       : ")
    except (EOFError, KeyboardInterrupt):
        print("\n  ! cancelled", file=sys.stderr)
        return 1
    if first != again:
        print("  ! they do not match; nothing was changed", file=sys.stderr)
        return 1
    cfg.write_options("web", {"password": first})
    if first:
        print("\n  Password set. The page and the settings API now ask for")
        print("  %s and this password. Snapshots and the stream stay open,"
              % user)
        print("  so PiNozCam keeps working in Mainsail's camera list.")
    else:
        print("\n  Password removed; the page answers anyone again.")
    print("  Takes effect immediately -- no restart needed.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="moonraker-pinozcam")
    ap.add_argument("-c", "--config", required=True,
                    help="path to moonraker-pinozcam.cfg")
    ap.add_argument("--set-password", action="store_true",
                    help="set or clear the web page's password, then exit")
    args = ap.parse_args(argv)

    if args.set_password:
        return _set_password(args.config)

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

    # 0 = notify only, 1 = pause, 2 = stop -- the OctoPrint build's own
    # encoding, read fresh on every failure so a saved change applies.
    ACTION_NAMES = {1: "Print paused", 2: "Print stopped"}

    def latest_jpeg():
        """A frame for /check -- the annotated one if there is one.

        ⚠️ This must NOT return None when detection is idle. Check is the
        command a user presses BETWEEN prints ("what does the printer look
        like right now"), and the detector only produces frames while a job
        is running. The annotated view already resolves this exactly the
        way the OctoPrint build's check_reply() does -- last analysed frame,
        else a live camera fetch, else the NO SIGNAL placeholder -- so ask
        it rather than reaching into the detector.
        """
        jpeg, _ = view.frames.latest()
        return jpeg

    def status_line():
        """The /check caption, in the OctoPrint build's six-line shape."""
        st = client.state
        lines = ["Printer: %s" % (cfg.get("printer", "name", "")
                                  or client.printer_name()),
                 "Status: %s%s" % (st.state,
                                   " (paused)" if st.is_paused else "")]
        if st.state in ("printing", "paused"):
            lines.append("Progress: %.0f%%" % (st.progress * 100))
        if st.nozzle_temp is not None:
            lines.append("Nozzle Temp: %.1f°C" % st.nozzle_temp)
        if st.bed_temp is not None:
            lines.append("Bed Temp: %.1f°C" % st.bed_temp)
        if st.filename:
            lines.append("File: %s" % st.filename)

        d = detector_ref[0]
        if d is None or not d.running:
            lines.append("AI: not detecting")
        else:
            w = d.window.stats
            lines.append("AI: %d frames, %d alarming (%.0f%%)%s"
                         % (w["window_frames"], w["alarming"],
                            w["ratio"] * 100,
                            "" if w["armed"] else ", warming up"))
        return "\n".join(lines)

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
        # ⚠️ ACT FIRST, THEN NOTIFY. The printer action is the safety
        # boundary; a chat message is alert material. This used to send the
        # alert first, which put a synchronous Telegram and Discord round
        # trip -- Discord's read timeout alone is 20 s -- in front of a
        # Pause the user had already asked for. The OctoPrint build orders
        # it the same way for the same reason.
        action = int(cfg.detection["action"] or 0)
        acted, action_failed = False, None
        if action in ACTION_NAMES:
            try:
                if action == 1:
                    client.pause_print()
                else:
                    client.cancel_print()
                LOG.warning("%s", ACTION_NAMES[action])
                acted = True
            except Exception as exc:                         # noqa: BLE001
                action_failed = (
                    "PiNozCam could not perform the requested printer "
                    "action. Check the printer connection and the log.")
                LOG.exception("Requested printer action %s failed: %s",
                              action, exc)
        else:
            LOG.warning("Failure detected; action=0, so the printer was "
                        "not touched")

        caption = ("Print failure detected (%.0f%% of the last %ds)"
                   % (result.get("ratio", 0) * 100,
                      cfg.detection["count_time"]))
        if acted:
            caption = "%s. %s" % (ACTION_NAMES[action], caption)
        elif action_failed:
            caption = "%s\n%s" % (action_failed, caption)
        delivered = False
        try:
            jpeg = latest_jpeg()
            delivered = bool(notifier.alert(caption, image=jpeg))
        except Exception as exc:                             # noqa: BLE001
            LOG.error("Could not send the alert: %s", exc)

        # Report back so the detector only latches this episode when
        # something actually happened.
        #
        # ⚠️ `action == 0` used to stand in for success unconditionally, so
        # "alert only" latched the episode even when every channel had
        # failed: the quota was spent, nothing was delivered, and no later
        # frame retried. With no channel configured at all there is nothing
        # to deliver and nothing to retry, so that case still counts --
        # otherwise a printer with no bots would re-run this handler on
        # every single frame of the failure.
        if acted:
            return True
        if action != 0:
            return False
        return delivered or not notifier.has_channel()

    # Probe the inference backend now rather than at the first print, so a
    # missing binary is visible in the startup log and on the page instead
    # of minutes into a job. The OctoPrint build does the same. Constructing
    # one starts no process; preflight only checks the files.
    backend_info = {"name": None, "error": None}
    try:
        from .nozcam_backend import NozcamBackend, BackendUnavailable
        probe = NozcamBackend(plugin_dir=None, logger=LOG,
                              backend=cfg.detection.get("ai_backend")
                              or "auto")
        probe.preflight()
        backend_info["name"] = probe.describe()
        LOG.info("Inference backend ready: %s", backend_info["name"])
    except Exception as exc:                                 # noqa: BLE001
        backend_info["error"] = "Inference backend unavailable: %s" % exc
        LOG.error("%s", backend_info["error"])

    view = AnnotatedView(cfg, client, LOG, detector_ref=detector_ref,
                         notifier=notifier, backend_info=backend_info)
    detector = Detector(cfg, client, LOG, on_failure=on_failure, view=view,
                        on_notice=lambda text: notifier.alert(
                            text, with_buttons=True))
    detector_ref[0] = detector

    def on_state_change(state):
        """Start and stop detecting with the print.

        Detection is tied to the job, not to the service: there is nothing
        to detect on an idle printer, and the camera and daemon should not
        be held open between prints.
        """
        LOG.info("printer: %s", state)
        previous, on_state_change.previous = on_state_change.previous, \
            ("paused" if state.is_paused else state.state)
        if state.is_printing and not detector.running:
            # ⚠️ Distinguish a NEW print from a resume. Pausing stops the
            # detector, so resuming came through here too and called
            # new_print() -- which wipes the window, un-mutes alerts and
            # re-arms the failure episode. A user who paused, fixed
            # something and resumed lost all of that silently. The
            # OctoPrint build only clears state on a real PRINT_STARTED.
            # Klipper's own transition says which this is: a print that
            # was paused comes back as paused -> printing, anything else is
            # a new job. Read from the state we last saw, not guessed from
            # a filename that is identical either way.
            resumed = previous == "paused"
            if resumed:
                LOG.info("Print resumed -- continuing detection")
            else:
                LOG.info("Print started -- beginning detection")
                notifier.new_print()
            detector.start(fresh=not resumed)
        elif not state.is_printing and detector.running:
            LOG.info("Print no longer active (%s) -- stopping detection",
                     state.state)
            detector.stop()

    on_state_change.previous = None
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
        # ⚠️ Only inside this branch. A Discord button carries the printer
        # id, and the id falls back to the hostname while Moonraker's
        # database is out of reach -- so asking before the connection is up
        # would read a normal slow start as a rename.
        try:
            notifier.announce_button_id_change()
        except Exception as exc:                             # noqa: BLE001
            LOG.warning("could not check the Discord button id: %s", exc)
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
