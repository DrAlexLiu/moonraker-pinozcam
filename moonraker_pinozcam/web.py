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

import hashlib
import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BOUNDARY = "pinozcamframe"

# How long a plain camera frame is reused when detection is idle. This is
# also the idle frame rate the page sees: the stream waits the remainder of
# it rather than sending the same picture several times, so 0.5 s means a
# genuinely new picture twice a second. Fetching one costs ~5 ms from a
# local ustreamer, so the bound here is politeness, not cost.
FALLBACK_CACHE = 0.5

# A publisher that has gone quiet for longer than this is treated as
# stopped, so the stream serves the live camera instead of waiting out
# STREAM_KEEPALIVE for an annotated frame that is not coming.
PUBLISHER_IDLE_AFTER = 3.0

# Cap the MJPEG rate independently of detection. The detector runs at
# whatever the hardware allows; a browser tab does not need more than this,
# and each frame served costs a JPEG encode.
STREAM_MAX_FPS = 5.0
# How many browsers may pull the camera THROUGH this process at once. The
# proxy is a byte pump -- no decode, no encode -- but 11.5 Mbit/s measured
# on one viewer is still bandwidth taken from the detector, which is itself
# memory-bandwidth bound. Beyond this a viewer is told 503 and its page
# falls back to stills rather than everyone's video degrading together.
CAMERA_PROXY_VIEWERS = 2
CAMERA_PROXY_CHUNK = 65536

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
        self._published_at = 0.0
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
            self._published_at = time.monotonic()
            self._cv.notify_all()

    def latest(self):
        """The newest annotated frame, or a live one.

        ⚠️ Bounded by freshness. This used to return the annotated frame
        whenever one had EVER been published, so after a print ended it
        served that print's last frame forever -- to /check, to the mask
        editor, and to Mainsail. A user painting an undetect zone would be
        painting on a picture from hours ago.
        """
        with self._cv:
            if self._jpeg is not None and self._publishing():
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
        """Block for a frame newer than last_seq; return (jpeg, seq).

        ⚠️ Two ways this went wrong before, both only when NOT detecting,
        which is most of the time a user has the page open:

        * On a service that had not run a print yet, _seq is 0 and the
          caller starts at -1, so `_seq <= last_seq` was false immediately,
          the loop's else branch ran, and it returned the _jpeg that does
          not exist yet -- None. The stream handler treats None as "stop",
          so the connection closed after zero frames and the live view was
          simply dead.
        * Once a print HAD run, _jpeg holds its last annotated frame
          forever, so the same path served that stale picture every two
          seconds and never fetched the camera again. That is what "the
          camera is slow" looked like.

        A frame only counts as new when the sequence advanced AND there is
        something to send; anything else falls through to the live camera.
        """
        deadline = time.monotonic() + timeout
        with self._cv:
            if self._seq > last_seq and self._jpeg is not None:
                return self._jpeg, self._seq
            # Only wait for an annotated frame while something is actually
            # producing them. Waiting out the keepalive on every frame of
            # an idle stream cost a full 2 s each -- 0.45 fps, which is
            # what "the camera is slow" was.
            if self._publishing():
                while self._seq <= last_seq or self._jpeg is None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cv.wait(remaining)
                else:
                    return self._jpeg, self._seq
        # No NEW annotated frame. Serve the live camera and keep the
        # caller's sequence, so it asks again next tick.
        return self._live_fresh(), last_seq

    def peek(self):
        """The annotated frame and its sequence, without any waiting."""
        with self._cv:
            return self._jpeg, self._seq

    def publishing(self):
        return self._publishing()

    def _publishing(self):
        """Whether a detector is currently feeding us annotated frames."""
        return (self._published_at > 0.0
                and time.monotonic() - self._published_at
                < PUBLISHER_IDLE_AFTER)

    def _live_fresh(self):
        """A live frame, waiting out the cache so it is a NEW picture.

        Without the wait the stream would send the same cached JPEG several
        times a second: bytes on the wire for a picture that has not
        changed.
        """
        due = self._fallback_at + FALLBACK_CACHE - time.monotonic()
        if 0 < due < FALLBACK_CACHE:
            time.sleep(due)
        return self._live()


def _redact_userinfo(url):
    """Replace a URL's password with asterisks, keeping it recognisable.

    ⚠️ Parsed, not string-matched. A first attempt took the LAST "@" in
    the string, which for `http://a:b@h/p?x=y@z` is the one in the query
    -- so it decided there was no userinfo and returned the password in
    full. The authority is what "@" has to be looked for in, and urlsplit
    is what knows where the authority ends.
    """
    from urllib.parse import urlsplit, urlunsplit
    if not url or "@" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or "@" not in parts.netloc:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    user, colon, _password = userinfo.partition(":")
    if not colon:
        return url                      # a username alone is not a secret
    return urlunsplit(parts._replace(
        netloc="%s:***@%s" % (user, hostport)))


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
        # ⚠️ Not authentication -- this server deliberately has none, and
        # the README says so. This only closes the drive-by case: a page on
        # some other site cannot make a visitor's browser POST settings
        # here, because the browser attaches an Origin the browser itself
        # sets and cannot be forged by script. A request with no Origin at
        # all (curl, a script, Mainsail's own fetch) is allowed through, as
        # it always was.
        origin = self.headers.get("Origin")
        if origin:
            host = self.headers.get("Host") or ""
            if origin.split("//", 1)[-1] != host:
                self.log_error("cross-origin POST from %s refused", origin)
                return self._json({"error": "cross-origin request"}, 403)
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
        elif path == "/camera":
            self._camera()
        elif path == "/":
            self._page()
        elif path == "/api/settings":
            self._json(self.server.owner.read_settings())
        elif path == "/api/status":
            # The Host the BROWSER used, so a camera URL built for it is
            # one the browser can actually reach.
            self._json(self.server.owner.read_status(
                self.headers.get("Host") or ""))
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
        jpeg, seq = self.server.frames.latest()
        if jpeg is None:
            return self._no_frame()
        # ETag so the page can refetch on every frame_id change without
        # paying for a picture it already has. Mainsail and other pollers
        # get the same benefit for free.
        etag = '"%s"' % hashlib.md5(jpeg).hexdigest()
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("ETag", etag)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(jpeg)))
        # Frontends poll this; a cached frame would look like a frozen camera.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(jpeg)

    def _camera(self):
        """Pump the camera's own stream through this service, unchanged.

        The fallback for the case the direct URL cannot cover. Mainsail can
        hand a browser the relative `/webcam/?action=stream` because it is
        served by the very nginx that proxies it -- same origin, so the
        browser resolves it correctly and never touches Moonraker. This
        service is on another port, so the same string would resolve
        against US. stream_info() answers that by rewriting the host, which
        assumes the browser can also reach port 80: true on a stock
        Mainsail host, false behind a proxy that publishes only this port,
        and meaningless on a Klipper host with no nginx at all.

        Here the relative path works because we ARE the origin. The cost is
        that every byte crosses this process, so it is offered second and
        capped -- see CAMERA_PROXY_VIEWERS.

        Deliberately transparent: whatever the camera sends, including its
        own multipart boundary, is what the browser gets. Re-framing it
        would make this a second implementation of /stream's format and a
        second thing that can disagree with the camera.
        """
        owner = self.server.owner
        url = owner.camera_stream_url()
        if not url:
            return self.send_error(503, "no camera stream")
        if not owner.proxy_acquire():
            return self.send_error(503, "too many camera viewers")
        try:
            self._pump(url)
        finally:
            owner.proxy_release()

    def _pump(self, url):
        import requests
        try:
            # connect timeout, then read timeout: an MJPEG stream that
            # stops sending is dead, and holding the thread would leak a
            # proxy slot for as long as the browser stays open.
            upstream = requests.get(url, stream=True, timeout=(5, 30))
            upstream.raise_for_status()
        except Exception as exc:                             # noqa: BLE001
            self.server.owner.log_proxy_error(url, exc)
            return self.send_error(502, "camera unreachable")
        # No Content-Length is possible on a live stream, so the connection
        # cannot be reused; say so rather than leaving the client waiting
        # for a body that never ends.
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", upstream.headers.get(
                "Content-Type", "multipart/x-mixed-replace"))
            self.send_header("Cache-Control",
                             "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            for chunk in upstream.iter_content(CAMERA_PROXY_CHUNK):
                if self.server.stopping.is_set():
                    break
                if chunk:
                    self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass            # the tab was closed; ordinary, not an error
        finally:
            upstream.close()

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

    # One definition, in camera.py, which also has to recognise it in
    # Moonraker's list so we never resolve to our own output.
    from .camera import SELF_WEBCAM_NAME as WEBCAM_NAME

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
        "frame_sample_interval": (int, 10, 1000),
        "frame_buffer_max_age": (int, 4, 16),
        "frame_buffer_capacity": (int, 4, 16),
        "cpu_share": (float, 0.01, 1.0),
        "action": (int, 0, 2),
        # Not a number: validated against the set the backend resolver
        # accepts, because a typo here would quietly disable the NPU.
        "ai_backend": (("auto", "cpu", "rknn", "awnn", "acl", "bpu",
                        "vulkan", "coreml"), 0, 0),
    }

    # Written to [camera] rather than [detection].
    EDITABLE_CAMERA = {
        "snapshot_url": (str, 0, 0),
    }

    # Schemes this API will accept for a camera URL.
    #
    # ⚠️ `file://` is deliberately NOT here, although the config file and
    # the frame source both support it. This endpoint has no login and
    # binds 0.0.0.0, so accepting it would let anyone on the network point
    # the camera at any file the service can read and then fetch it from
    # /snapshot. It stays available by editing the config file, which is
    # protected by that file's ownership.
    CAMERA_URL_SCHEMES = ("http://", "https://")

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
                 notifier=None, backend_info=None):
        self._config = config
        self._cfg = config.web
        self._client = client
        self._log = logger
        self._detector_ref = detector_ref
        self._notifier = notifier
        # {"name": str|None, "error": str|None} from the startup probe, so
        # the page can name the hardware before the first print.
        self._backend_info = backend_info or {}
        self._browser_camera = None        # (host, url) once resolved
        self.frames = FrameHolder(fallback=self._live_camera_jpeg)
        self._camera_source = None
        self._proxy_lock = threading.Lock()
        self._proxy_viewers = 0
        self._proxy_error_at = 0.0
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
            jpeg = camera_mod.grab_jpeg(self._camera_source)
            if jpeg:
                # Same orientation the detector works in, so the page and
                # the mask editor never see an untransformed picture.
                return camera_mod.transform_jpeg(jpeg, self._camera_source)
        except Exception:                                    # noqa: BLE001
            # Camera unplugged or crowsnest restarting; the page shows the
            # last frame it had rather than an error.
            self._camera_source = None      # re-resolve next time
        return None

    def camera_stream_url(self):
        """The camera stream this process can fetch, for /camera to pump.

        Deliberately the SAME url stream_info() would hand the browser
        directly, before any host rewriting -- the proxy is a transport
        fallback, not a second camera. So the rules that withhold a direct
        stream withhold the proxy too, and there is one gate, not two.
        """
        info = self._stream_source()
        return info[0].stream_url if info else None

    def proxy_acquire(self):
        with self._proxy_lock:
            if self._proxy_viewers >= CAMERA_PROXY_VIEWERS:
                return False
            self._proxy_viewers += 1
            return True

    def proxy_release(self):
        with self._proxy_lock:
            self._proxy_viewers = max(0, self._proxy_viewers - 1)

    def log_proxy_error(self, url, exc):
        # Rate-limited: a browser retries a broken <img> on its own, and a
        # camera that is down would otherwise fill the log at that rate.
        now = time.monotonic()
        if now - self._proxy_error_at < 30.0:
            return
        self._proxy_error_at = now
        self._log.warning("camera proxy could not reach %s: %s",
                          _redact_userinfo(url), exc)

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
        # ⚠️ Userinfo stripped. The OctoPrint build marks this setting
        # secret with the note "an IP camera URL routinely carries
        # user:pass@", and this server answers anyone on the network, so
        # returning it verbatim handed out a camera password. The host and
        # path stay visible, which is what makes the field usable; posting
        # the redacted form back leaves the stored URL alone.
        out["snapshot_url"] = _redact_userinfo(camera.get("snapshot_url") or "")
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
        if isinstance(caster, tuple):
            text = str(value).strip()
            if text not in caster:
                raise ValueError("%s must be one of: %s"
                                 % (key, ", ".join(caster)))
            return text
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
                if key == "snapshot_url":
                    text = str(value).strip()
                    if text and not text.startswith(
                            self.CAMERA_URL_SCHEMES):
                        raise ValueError(
                            "snapshot_url must start with http:// or "
                            "https:// -- other schemes can only be set by "
                            "editing the config file")
                    if text == _redact_userinfo(
                            self._config.camera.get("snapshot_url") or ""):
                        continue          # unchanged; keep the credentials
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
        # ⚠️ Recorded WITH the mask: the grid only means anything in the
        # coordinate system it was painted in. Without this a camera swap
        # or a rotation change silently blacked out the wrong region.
        updates = {"mask_image_data": data}
        signature = self._mask_signature()
        if signature:
            updates["mask_signature"] = signature
        self._config.write_options("camera", updates)
        self._log.info("Mask updated from the web page (%d cells ignored, "
                       "signature %s)", data.count("1"), signature or "?")

    def _mask_signature(self):
        """The coordinate system the editor's picture was in, or None."""
        from io import BytesIO
        from . import camera as camera_mod
        from . import mask as mask_mod
        source = self._camera_source
        if source is None:
            return None
        try:
            from PIL import Image
            jpeg = camera_mod.grab_jpeg(source)
            if not jpeg:
                return None
            # The SOURCE size, before the transform -- see mask.signature.
            return mask_mod.signature(
                Image.open(BytesIO(jpeg)).size, source)
        except Exception as exc:                             # noqa: BLE001
            self._log.debug("could not read the mask signature: %s", exc)
            return None

    def stream_info(self, request_host):
        """How the page should show live video, or None for stills.

        Two ways, in preference order, because they fail in different
        places:

        * `url` -- the camera's own address, opened by the BROWSER. Costs
          this service nothing and runs at the camera's real frame rate.
          Requires the browser to reach the camera's host and port, which
          is not something this process can verify.
        * `proxy` -- a relative path this service serves itself
          (/camera). Always reachable wherever this page is, and always
          present, because that is what makes it a fallback.

        None only when there should be no live view at all -- see
        _stream_source for the rules, which are the OctoPrint build's.

        The flip flags ride along: the stream is untransformed either way,
        so only it needs them applied in CSS.
        """
        info = self._stream_source()
        if info is None:
            return None
        source, _ = info
        out = {"flipH": bool(source.flip_h), "flipV": bool(source.flip_v),
               # Relative on purpose: the browser resolves it against the
               # origin it used to reach this page, so it works wherever
               # this page works -- which is the whole point of the
               # fallback. Handled by /camera.
               "proxy": "camera"}
        direct = self._direct_stream_url(source, request_host)
        if direct:
            # Preferred when present: the browser talks to the camera and
            # not one byte crosses this process. Absent when the URL is one
            # only THIS process can reach, in which case the page goes
            # straight to the proxy instead of showing nothing.
            out["url"] = direct
        return out

    def _stream_source(self):
        """The camera whose stream may be shown, or None.

        The single gate for both the direct URL and the proxy: a reason to
        withhold a live view is a reason to withhold both. Same rules as
        the OctoPrint build.
        """
        if self._config.camera.get("snapshot_url"):
            return None                 # may not be the streamed camera
        detector = self._detector_ref[0] if self._detector_ref else None
        # The detector's camera while it runs; otherwise the one this view
        # already resolved for its own idle fallback, so the live view does
        # not have to wait for a print to appear.
        source = (getattr(detector, "camera_source", None)
                  or self._camera_source)
        if source is None or not source.stream_url or source.rotation:
            return None
        from . import camera as camera_mod
        if camera_mod.is_hls_or_webrtc_stream_url(source.stream_url):
            # <img> speaks MJPEG and nothing else, and proxying would not
            # change that -- the browser still could not play it.
            return None
        return source, source.stream_url

    def _direct_stream_url(self, source, request_host):
        """The stream URL a BROWSER could open, or None if only we can.

        ⚠️ Moonraker registers this camera as the relative
        `/webcam/?action=stream` and camera.resolve() absolutised it
        against the Moonraker host so THIS process could fetch it, which is
        why it usually reads 127.0.0.1. Mainsail never has to do this: it
        is served by the same nginx that proxies /webcam/, so the browser
        resolves the relative path correctly on its own. Here the host the
        browser used to reach us is put back in, keeping the port -- how a
        relative URL was always meant to be read.

        None when that rewrite cannot be justified, and None is not a
        failure any more: the caller falls back to the proxy.
        """
        from urllib.parse import urlparse, urlunparse
        url = source.stream_url
        host = (request_host or "").split(":")[0]
        parts = urlparse(url)
        if parts.hostname in ("127.0.0.1", "localhost", "::1"):
            # Only for a URL we built ourselves; a loopback the user typed
            # is excluded by the snapshot_url rule in _stream_source.
            if source.origin not in ("moonraker", "nginx") or not host:
                return None
            netloc = host if parts.port is None else "%s:%d" % (host,
                                                                parts.port)
            return urlunparse(parts._replace(netloc=netloc))
        if url.startswith("/"):
            return "http://%s%s" % (host, url) if host else None
        return url

    def read_status(self, request_host=""):
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
            # Null hides the page's Live Camera toggle.
            "stream": self.stream_info(request_host),
        }
        # What the live view should be showing. frame_id is a cheap change
        # detector: the page refetches the picture only when it moves.
        # ⚠️ Unlike the OctoPrint build, box COORDINATES are not sent --
        # this build annotates the JPEG itself, because it also registers
        # that view as a Moonraker webcam and Mainsail renders it as a
        # plain <img> that cannot draw anything of its own. Sending them
        # too would put every box on the picture twice.
        annotated, seq = self.frames.peek()
        if annotated is not None and self.frames.publishing():
            out["frame_kind"] = "analysis"
            out["frame_id"] = "a%d" % seq
        else:
            # The camera fallback re-encodes at most once per cache
            # window, so its id only needs to move at that cadence.
            out["frame_kind"] = "camera"
            out["frame_id"] = "c%d" % int(time.monotonic() / FALLBACK_CACHE)
        # Named from the startup probe until a detector supersedes it.
        if self._backend_info.get("name"):
            out["backend"] = self._backend_info["name"]
        if self._backend_info.get("error"):
            out["error"] = self._backend_info["error"]
        if detector is not None:
            # Shown as a banner on the page. Without it a detector that
            # failed to start looked identical to one that was simply idle.
            # A transient failure wins the banner; a standing warning
            # holds it when there is no failure to report.
            notice = detector.last_error or getattr(
                detector, "last_warning", None)
            if notice:
                out["error"] = notice
            stats = detector.window.stats
            out.update({"window_frames": stats["window_frames"],
                        "alarming": stats["alarming"],
                        "ratio": stats["ratio"], "armed": stats["armed"]})
            last = detector.last_result
            if last:
                out["severity"] = last.get("severity")
                out["model_ms"] = (last.get("elapsed") or 0) * 1000.0
                # ⚠️ NOT "alarming" -- that name already holds the window's
                # alarming-frame COUNT, and overwriting it with a boolean
                # made the page render "true of 137 frames alarming".
                out["frame_alarming"] = bool(last.get("alarming"))
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
        # ⚠️ Resolve the camera NOW, off the request path. The page shows the
        # camera's own stream whenever stream_info() can name one, and that
        # answer depends on a source only ever resolved lazily by the first
        # /snapshot -- so a freshly started service told the first status
        # poll there was no stream, and the page opened on a still it then
        # had to replace. Resolution costs an HTTP probe and must not run on
        # /api/status, which is polled every two seconds.
        threading.Thread(target=self._warm_camera, name="pinozcam-webcam-warm",
                         daemon=True).start()

    def _warm_camera(self):
        """Populate _camera_source once, so the first poll knows the stream."""
        try:
            self._live_camera_jpeg()
        except Exception:                                    # noqa: BLE001
            pass        # no camera yet; the next /snapshot resolves it

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
