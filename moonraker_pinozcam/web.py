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
            return self._fallback_jpeg
        # Camera unreachable. Show NO SIGNAL rather than a blank element or
        # a stale frame from minutes ago -- a user tuning a mask needs to
        # know the picture is not current.
        from .placeholder import no_signal_jpeg
        self._fallback_jpeg = no_signal_jpeg()
        self._fallback_at = now
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
        "scores_threshold": (float, 0.0, 1.0),
        "img_sensitivity": (float, 1e-4, 1.0),
        "failure_ratio": (float, 0.01, 1.0),
        "count_time": (int, 10, 3600),
        "ai_start_delay": (int, 0, 60000),
        "cpu_share": (float, 0.01, 1.0),
        "action": (int, 0, 2),
    }

    def __init__(self, config, client, logger, detector_ref=None):
        self._config = config
        self._cfg = config.web
        self._client = client
        self._log = logger
        self._detector_ref = detector_ref
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
        out["mask_image_data"] = self._config.camera.get(
            "mask_image_data") or ""
        return out

    def save_settings(self, payload):
        """Validate and write settings back to the config file."""
        updates = {}
        for key, value in (payload or {}).items():
            if key not in self.EDITABLE:
                continue                    # ignore rather than fail
            caster, low, high = self.EDITABLE[key]
            try:
                number = caster(value)
            except (TypeError, ValueError):
                raise ValueError("%s is not a number" % key)
            if not low <= number <= high:
                raise ValueError("%s must be between %s and %s"
                                 % (key, low, high))
            updates[key] = number
        if updates:
            self._config.write_options("detection", updates)
            self._log.info("Settings updated from the web page: %s",
                           ", ".join(sorted(updates)))

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
        out = {
            "printer_state": state.state,
            "detecting": bool(detector and detector.running),
            "version": __import__("moonraker_pinozcam").__version__,
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
