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
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = "pinozcamframe"

# Cap the MJPEG rate independently of detection. The detector runs at
# whatever the hardware allows; a browser tab does not need more than this,
# and each frame served costs a JPEG encode.
STREAM_MAX_FPS = 5.0

# How long a stream waits for a new frame before sending the current one
# again. Without this a paused detector would leave the browser hanging on
# a connection that never produces bytes, which reads as "camera broken".
STREAM_KEEPALIVE = 2.0


class FrameHolder(object):
    """The most recent annotated frame, shared with the detection thread."""

    def __init__(self):
        self._jpeg = None
        self._seq = 0
        self._cv = threading.Condition()

    def publish(self, jpeg_bytes):
        with self._cv:
            self._jpeg = jpeg_bytes
            self._seq += 1
            self._cv.notify_all()

    def latest(self):
        with self._cv:
            return self._jpeg, self._seq

    def wait_newer(self, last_seq, timeout):
        """Block for a frame newer than last_seq; return (jpeg, seq)."""
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._seq <= last_seq:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._jpeg, self._seq
                self._cv.wait(remaining)
            return self._jpeg, self._seq


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

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/snapshot", "/current"):
            self._snapshot()
        elif path in ("/stream", "/"):
            self._stream()
        elif path == "/health":
            body = b"ok"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

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

    def __init__(self, addr, frames, stopping):
        self.frames = frames
        self.stopping = stopping
        ThreadingHTTPServer.__init__(self, addr, _Handler)


class AnnotatedView(object):
    """The HTTP server plus its Moonraker registration."""

    WEBCAM_NAME = "PiNozCam"

    def __init__(self, config, client, logger):
        self._cfg = config.web
        self._client = client
        self._log = logger
        self.frames = FrameHolder()
        self._stopping = threading.Event()
        self._server = None
        self._thread = None

    @property
    def enabled(self):
        return int(self._cfg.get("port") or 0) > 0

    def publish(self, jpeg_bytes):
        """Called by the detector for every scored frame."""
        self.frames.publish(jpeg_bytes)

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
                                   self._stopping)
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
