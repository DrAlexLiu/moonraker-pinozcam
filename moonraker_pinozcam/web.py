"""Serve the annotated frame so Mainsail and Fluidd can show it.

This is not a settings UI. It exists because a Klipper build has no plugin
panel to draw into, and because Moonraker's webcam list is *data*: an entry
pointing here appears in every frontend without asking anyone to write Vue
components for us. The user never types this port -- they open Mainsail as
usual and the annotated view is one more camera in the list.

Deliberately built on http.server rather than a framework: two endpoints,
no routing to speak of, and this runs beside Klipper on boards with 512 MB
of RAM.
"""

import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = "pinozcamframe"

# How long a plain camera frame is reused when detection is idle. Short
# enough to look live, long enough that an open tab does not hammer the
# camera at the stream's frame rate.
FALLBACK_CACHE = 0.8

# Cap the MJPEG rate independently of detection. The detector runs at
# whatever the hardware allows; a browser tab does not need more than this,
# and each frame served costs a JPEG encode.
STREAM_MAX_FPS = 5.0

# How long a stream waits for a new frame before sending the current one
# again. Without this a paused detector would leave the browser hanging on
# a connection that never produces bytes, which reads as "camera broken".
STREAM_KEEPALIVE = 2.0


class FrameHolder(object):
    """The most recent annotated frame, shared with the detection thread.

    When detection is not running there are no annotated frames, but the
    page still has to show something: tuning the sensitivity and painting a
    mask are things a user does BETWEEN prints, and an empty view then is
    useless. So a fallback fetches the plain camera image on demand.
    """

    def __init__(self, fallback=None):
        self._jpeg = None
        self._seq = 0
        self._cv = threading.Condition()
        self._fallback = fallback      # () -> jpeg bytes or None
        self._fallback_jpeg = None
        self._fallback_at = 0.0
        # None until something has been tried; then True for a real frame
        # and False for the NO SIGNAL placeholder. The page asks for this,
        # and comparing bytes against a freshly drawn placeholder would be
        # both wasteful and fragile.
        self.camera_ok = None

    def publish(self, jpeg_bytes):
        with self._cv:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cv.notify_all()

    def latest(self):
        with self._cv:
            if self._jpeg is not None:
                return self._jpeg, self._seq
        return self._live(), self._seq

    def _live(self):
        """Plain camera frame, cached briefly.

        Cached because /stream calls this on every tick when idle; without
        it an open browser tab would poll the camera as fast as it can.
        """
        if self._fallback is None:
            return None
        now = time.monotonic()
        if (self._fallback_jpeg is not None
                and now - self._fallback_at < FALLBACK_CACHE):
            return self._fallback_jpeg
        try:
            jpeg = self._fallback()
        except Exception:                                    # noqa: BLE001
            jpeg = None
        if jpeg:
            self._fallback_jpeg = jpeg
            self._fallback_at = now
            self.camera_ok = True
            return self._fallback_jpeg
        # Camera unreachable. Show NO SIGNAL rather than a blank element or
        # a stale frame from minutes ago -- a user tuning a mask needs to
        # know the picture is not current.
        from .placeholder import no_signal_jpeg
        self._fallback_jpeg = no_signal_jpeg()
        self._fallback_at = now
        self.camera_ok = False
        return self._fallback_jpeg

    def wait_newer(self, last_seq, timeout):
        """Block for a frame newer than last_seq; return (jpeg, seq)."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._seq <= last_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if self._jpeg is not None:
                        return self._jpeg, self._seq
                    break
                self._cv.wait(remaining)
            else:
                return self._jpeg, self._seq
        # Idle: no annotated frame arrived, so serve the live camera and
        # bump nothing -- the caller keeps its sequence and comes back.
        return self._live(), last_seq


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PiNozCam"
    sys_version = ""

    def log_message(self, fmt, *args):
        """Silence per-request logging; a stream would flood the journal."""

    def _no_frame(self):
        self.send_response(503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return None
        # Bound the read: this listens on 0.0.0.0, and a settings POST is
        # a few hundred bytes while a mask is ~16 KB.
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    def do_POST(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        owner = self.server.owner
        payload = self._read_json()
        if payload is None:
            return self._json({"error": "bad request"}, 400)
        try:
            if path == "/api/settings":
                owner.save_settings(payload)
            elif path == "/api/mask":
                owner.save_mask(payload.get("mask_image_data", ""))
            else:
                return self.send_error(404)
        except Exception as exc:                             # noqa: BLE001
            return self._json({"error": str(exc)}, 500)
        return self._json({"ok": True})

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/snapshot", "/current"):
            self._snapshot()
        elif path == "/stream":
            self._stream()
        elif path == "/":
            self._page()
        elif path == "/api/settings":
            self._json(self.server.owner.read_settings())
        elif path == "/api/status":
            self._json(self.server.owner.read_status())
        elif path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def _page(self):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "index.html")
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self.send_error(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _snapshot(self):
        jpeg, _ = self.server.frames.latest()
        if jpeg is None:
            return self._no_frame()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        # Frontends poll this; a cached frame would look like a frozen camera.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(jpeg)

    def _stream(self):
        jpeg, seq = self.server.frames.latest()
        if jpeg is None:
            return self._no_frame()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=%s" % BOUNDARY)
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        interval = 1.0 / STREAM_MAX_FPS
        last_seq = -1
        try:
            while not self.server.stopping.is_set():
                started = time.monotonic()
                jpeg, last_seq = self.server.frames.wait_newer(
                    last_seq, STREAM_KEEPALIVE)
                if jpeg is None:
                    break
                self.wfile.write(
                    b"--" + BOUNDARY.encode() + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() +
                    b"\r\n\r\n" + jpeg + b"\r\n")
                self.wfile.flush()
                slack = interval - (time.monotonic() - started)
                if slack > 0:
                    time.sleep(slack)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # The browser closed the tab. Normal, not worth a log line.
            pass


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, frames, stopping, owner):
        self.frames = frames
        self.stopping = stopping
        self.owner = owner
        ThreadingHTTPServer.__init__(self, addr, _Handler)


class AnnotatedView(object):
    """The HTTP server plus its Moonraker registration."""

    WEBCAM_NAME = "PiNozCam"

    # Settings the page may change, with the bounds the OctoPrint schema
    # declares. Anything outside this dict is not writable over HTTP --
    # this listens on 0.0.0.0, so the set is deliberately narrow and holds
    # no credentials.
    EDITABLE = {
        "enable_ai": (bool, 0, 1),
        "scores_threshold": (float, 0.0, 1.0),
        "img_sensitivity": (float, 1e-4, 1.0),
        "failure_ratio": (float, 0.01, 1.0),
        "count_time": (int, 10, 3600),
        "ai_start_delay": (int, 0, 60000),
        "detection_interval": (int, 0, 3600),
        "print_layout_threshold": (float, 0.0, 1.0),
        "frame_sample_interval": (int, 10, 1000),
        "frame_buffer_max_age": (int, 4, 16),
        "frame_buffer_capacity": (int, 4, 16),
        "cpu_share": (float, 0.01, 1.0),
        "action": (int, 0, 2),
    }

    # Written to [camera] rather than [detection].
    EDITABLE_CAMERA = {
        "snapshot_url": (str, 0, 0),
    }

    # Written to [notification].
    EDITABLE_NOTIFY = {
        "max_notification": (int, 0, 60000),
        "notify_interval": (int, 0, 3600),
    }

    # Credentials, in their own INI sections. WRITE-ONLY over HTTP: a
    # token can be set here but is never handed back.
    #
    # ⚠️ The asymmetry is the whole point. This server has no login and
    # binds 0.0.0.0, so anything read_settings() returns is readable by
    # everyone on the network -- but WRITE access adds no new exposure,
    # because every other setting is already writable here and Mainsail
    # edits the same file in the browser anyway. So the page gets the
    # same fields the OctoPrint build has, and a reader still cannot
    # lift the token off the wire.
    EDITABLE_TELEGRAM = {
        "enabled": (bool, 0, 1),
        "token": (str, 0, 0),
        "chat_id": (str, 0, 0),
    }
    EDITABLE_DISCORD = {
        "enabled": (bool, 0, 1),
        "bot_token": (str, 0, 0),
        "channel_id": (str, 0, 0),
    }

    # Sent in place of a stored token. Posted back unchanged it means
    # "leave it alone"; an empty string means "clear it". Anything else is
    # a new value. Without this a page load followed by Save would wipe
    # every credential it was never shown.
    SECRET_MASK = "\u2022" * 8

    # Which of the fields above are true credentials rather than
    # addresses. A chat or channel id is not usable without its token --
    # Discord channel ids are visible to everyone in the server -- and
    # masking them would leave no way to check what is configured.
    SECRETS = ("token", "bot_token")

    def __init__(self, config, client, logger, detector_ref=None,
                 notifier=None):
        self._config = config
        self._cfg = config.web
        self._client = client
        self._log = logger
        self._detector_ref = detector_ref
        self._notifier = notifier
        self.frames = FrameHolder(fallback=self._live_camera_jpeg)
        self._camera_source = None
        self._stopping = threading.Event()
        self._server = None
        self._thread = None

    def _live_camera_jpeg(self):
        """Fetch one plain frame straight from the camera.

        Resolved lazily and cached: at start() the camera may not be
        configured yet, and resolution costs an HTTP probe.
        """
        from . import camera as camera_mod
        import requests
        if self._camera_source is None:
            try:
                self._camera_source = camera_mod.resolve(
                    self._config.camera, self._client, logger=None)
            except Exception:                                # noqa: BLE001
                return None
        try:
            response = requests.get(self._camera_source.snapshot_url,
                                    timeout=(3.0, 6.0))
            if response.status_code == 200 and response.content[:2] == b"\xff\xd8":
                return response.content
        except Exception:                                    # noqa: BLE001
            # Camera unplugged or crowsnest restarting; the page shows the
            # last frame it had rather than an error.
            self._camera_source = None      # re-resolve next time
        return None

    def _camera_reachable(self):
        """Whether the last frame served was a real one.

        Reads what the frame holder already recorded rather than probing:
        the welcome page asks this on load, and a probe there would block
        the page for as long as an unplugged camera takes to time out.
        None means nothing has been fetched yet.
        """
        if self.frames.camera_ok is None and self._camera_source is not None:
            return True         # resolved a camera, just no frame served yet
        return self.frames.camera_ok

    @property
    def enabled(self):
        return int(self._cfg.get("port") or 0) > 0

    def publish(self, jpeg_bytes):
        """Called by the detector for every scored frame."""
        self.frames.publish(jpeg_bytes)

    # ---- API backing the page -----------------------------------------

    def read_settings(self):
        detection = self._config.detection
        out = {k: detection[k] for k in self.EDITABLE if k in detection}
        # detection[] holds seconds; the page and the config file both use
        # milliseconds, which is the OctoPrint schema's own convention.
        if "frame_sample_interval" in out:
            out["frame_sample_interval"] = int(
                round(out["frame_sample_interval"] * 1000))
        camera = self._config.camera
        out["snapshot_url"] = camera.get("snapshot_url") or ""
        out["mask_image_data"] = camera.get("mask_image_data") or ""
        notification = self._config.notification
        for key in self.EDITABLE_NOTIFY:
            if key in notification:
                out[key] = notification[key]
        for prefix, section, allowed in (
                ("telegram_", "telegram", self.EDITABLE_TELEGRAM),
                ("discord_", "discord", self.EDITABLE_DISCORD)):
            values = self._config.get_section(section)
            for key in allowed:
                value = values.get(key)
                if key in self.SECRETS:
                    # Present-or-absent, never the value.
                    value = self.SECRET_MASK if value else ""
                elif key == "enabled":
                    value = bool(value)
                out[prefix + key] = value if value is not None else ""
        return out

    @staticmethod
    def _cast(key, value, spec):
        """Range-check one incoming setting, or raise ValueError."""
        caster, low, high = spec
        if caster is str:
            return str(value).strip()
        if caster is bool:
            return "true" if value in (True, 1, "1", "true", "on") else "false"
        try:
            number = caster(value)
        except (TypeError, ValueError):
            raise ValueError("%s is not a number" % key)
        if not low <= number <= high:
            raise ValueError("%s must be between %s and %s"
                             % (key, low, high))
        return number

    def save_settings(self, payload):
        """Validate and write settings back to the config file.

        Unknown keys are ignored rather than rejected: the page posts one
        blob for every tab, and a build of the page newer than the service
        should still be able to save the settings this service knows.
        """
        sections = (("detection", self.EDITABLE, ""),
                    ("camera", self.EDITABLE_CAMERA, ""),
                    ("notification", self.EDITABLE_NOTIFY, ""),
                    ("telegram", self.EDITABLE_TELEGRAM, "telegram_"),
                    ("discord", self.EDITABLE_DISCORD, "discord_"))
        written = []
        for section, allowed, prefix in sections:
            updates = {}
            for key, spec in allowed.items():
                field = prefix + key
                if field not in (payload or {}):
                    continue
                value = payload[field]
                # The mask is what the page was given in place of a stored
                # secret; posting it back means "unchanged".
                if key in self.SECRETS and value == self.SECRET_MASK:
                    continue
                updates[key] = self._cast(key, value, spec)
            if updates:
                self._config.write_options(section, updates)
                # Never log a credential, not even its name next to a value.
                written.extend(prefix + k for k in updates)
        if written:
            self._log.info("Settings updated from the web page: %s",
                           ", ".join(sorted(written)))

    def save_mask(self, data):
        data = "".join(ch for ch in (data or "") if ch in "01")
        if data:
            side = int(len(data) ** 0.5)
            if side * side != len(data):
                raise ValueError("mask length %d is not a square"
                                 % len(data))
        self._config.write_options("camera", {"mask_image_data": data})
        self._log.info("Mask updated from the web page (%d cells ignored)",
                       data.count("1"))

    def read_status(self):
        detector = self._detector_ref[0] if self._detector_ref else None
        state = self._client.state
        notifier = self._notifier
        out = {
            "printer_state": state.state,
            "detecting": bool(detector and detector.running),
            "version": __import__("moonraker_pinozcam").__version__,
            # Live objects, not config flags: a channel whose credentials
            # were rejected is configured but not connected, and that is
            # exactly the state a user needs to see.
            "telegram": bool(notifier is not None
                             and notifier.telegram_bot is not None),
            "discord": bool(notifier is not None
                            and notifier.discord_bot is not None),
            "camera_ok": self._camera_reachable(),
        }
        if detector is not None:
            stats = detector.window.stats
            out.update({"window_frames": stats["window_frames"],
                        "alarming": stats["alarming"],
                        "ratio": stats["ratio"], "armed": stats["armed"]})
            last = detector.last_result
            if last:
                out["severity"] = last.get("severity")
                out["model_ms"] = (last.get("elapsed") or 0) * 1000.0
            if detector.backend_name:
                out["backend"] = detector.backend_name
        return out

    def start(self):
        if not self.enabled:
            self._log.info("Annotated view disabled (web.port = 0)")
            return
        port = int(self._cfg["port"])
        try:
            # 0.0.0.0 on purpose: users watch from a phone or another
            # machine, and binding to loopback would force every one of
            # them to configure a reverse proxy. Spoolman, the ecosystem's
            # precedent for a third-party service with its own port, does
            # the same.
            self._server = _Server(("0.0.0.0", port), self.frames,
                                   self._stopping, self)
        except OSError as exc:
            self._log.error("Annotated view could not bind port %d: %s "
                            "-- is something else using it?", port, exc)
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="pinozcam-web",
            daemon=True)
        self._thread.start()
        self._log.info("Annotated view on http://0.0.0.0:%d/stream", port)
        if self._cfg.get("register_webcam", True):
            self._register(port)

    def _register(self, port):
        """Publish this view to Moonraker so frontends list it.

        The URL has to be reachable from the BROWSER, not from here, so it
        carries the printer's own address rather than 127.0.0.1.
        """
        host = self._local_address()
        base = "http://%s:%d" % (host, port)
        try:
            self._client.register_webcam(
                self.WEBCAM_NAME,
                snapshot_url="%s/snapshot" % base,
                stream_url="%s/stream" % base,
                icon="mdiCctv")
            self._log.info(
                "Registered %r with Moonraker -- it appears in "
                "Mainsail/Fluidd's camera list", self.WEBCAM_NAME)
        except Exception as exc:                             # noqa: BLE001
            self._log.warning(
                "Could not register the webcam with Moonraker (%s); the "
                "view still works at %s/stream", exc, base)

    @staticmethod
    def _local_address():
        """This host's address on the network a browser would come from.

        Asks the routing table by opening a UDP socket, which sends nothing
        but resolves the source address. Reading `hostname -I` instead would
        return whichever interface came up first -- on a board with a docker
        bridge or an unplugged ethernet port that is the wrong one, and the
        registered URL would silently point somewhere unreachable.
        """
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("1.1.1.1", 80))
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"
        finally:
            sock.close()

    def stop(self):
        self._stopping.set()
        if self._server is not None:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception as exc:                         # noqa: BLE001
                self._log.debug("error stopping the web server: %s", exc)
        self._server = None
