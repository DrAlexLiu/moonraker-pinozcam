"""Decide where frames come from.

A Klipper host has three independent layers, and only the first is
guaranteed to exist:

  1. crowsnest/ustreamer actually producing frames on 127.0.0.1:8080
  2. nginx reverse-proxying it at /webcam/ .. /webcam4/
  3. Moonraker's `webcams` database, which the frontend reads

Layers 1 and 2 are set up when Klipper is installed; layer 3 is only
populated when a human adds a camera in Mainsail/Fluidd. A working printer
with a working camera very often has an empty layer 3, so "ask Moonraker"
cannot be the only strategy.
"""

import requests

# The name AnnotatedView registers with Moonraker. Defined here, and
# imported by web.py, so the skip below and the registration can never
# disagree about what our own entry is called.
SELF_WEBCAM_NAME = "PiNozCam"

# Set by Mainsail's own nginx site file at install time, so it is a
# convention that holds across virtually every Klipper install.
NGINX_WEBCAM_PATHS = ("/webcam/", "/webcam2/", "/webcam3/", "/webcam4/")

PROBE_CONNECT_TIMEOUT = 3.0
PROBE_READ_TIMEOUT = 5.0


class CameraUnavailable(Exception):
    """No usable frame source could be found."""


class CameraSource(object):
    """A resolved place to fetch frames from."""

    def __init__(self, snapshot_url, stream_url=None, origin="unknown",
                 rotation=0, flip_h=False, flip_v=False):
        self.snapshot_url = snapshot_url
        self.stream_url = stream_url
        self.origin = origin          # how it was found, for the log
        self.rotation = rotation
        self.flip_h = flip_h
        self.flip_v = flip_v

    def __repr__(self):
        return "<CameraSource %s via %s>" % (self.snapshot_url, self.origin)


def _probe(url):
    """Return the Content-Type if the URL answers usably, else None."""
    try:
        r = requests.get(url, stream=True,
                         timeout=(PROBE_CONNECT_TIMEOUT, PROBE_READ_TIMEOUT))
        try:
            if r.status_code != 200:
                return None
            return (r.headers.get("Content-Type") or "").split(";")[0].strip()
        finally:
            # Close without draining: a stream URL never ends on its own.
            r.close()
    except requests.RequestException:
        return None


def _usable(content_type):
    """Whether a Content-Type is something we can decode frames from."""
    if not content_type:
        return False
    return (content_type == "multipart/x-mixed-replace"
            or content_type.startswith("image/"))


def resolve(config_camera, client, logger=None):
    """Find a frame source, preferring what the user explicitly configured.

    Order matters and each step exists for a reason:
      1. the config file  -- an explicit choice always wins
      2. Moonraker        -- inherits the rotation/flip the user already set
      3. nginx convention -- covers the common empty-database case
    """
    def log(level, msg, *args):
        if logger is not None:
            getattr(logger, level)(msg, *args)

    # 1. Explicit configuration.
    url = (config_camera or {}).get("snapshot_url")
    if url:
        log("info", "Camera: using snapshot_url from the config file")
        return CameraSource(
            url, origin="config",
            rotation=config_camera.get("rotate", 0),
            flip_h=config_camera.get("flip_h", False),
            flip_v=config_camera.get("flip_v", False))

    # 2. Moonraker's database. Worth preferring over the nginx guess because
    # it carries the rotation and flip the user set once in the frontend.
    try:
        for cam in client.list_webcams():
            # ⚠️ Skip the entry WE registered. PiNozCam publishes its own
            # annotated view as a webcam, so it appears in this list --
            # first, on this board. Picking it would feed the detector its
            # own output, and probing it re-enters this function from the
            # web server's thread, since that endpoint resolves a camera to
            # answer. It only failed to happen by luck: right after a
            # restart our endpoint has no frame and answers 503, so it was
            # judged unusable. With a frame cached it would be chosen.
            if (cam.get("name") or "").strip() == SELF_WEBCAM_NAME:
                log("debug", "Camera: skipping our own %r entry",
                    SELF_WEBCAM_NAME)
                continue
            snap = cam.get("snapshot_url") or ""
            stream = cam.get("stream_url") or ""
            if not snap and not stream:
                continue
            # Moonraker stores these relative on a stock Mainsail setup.
            snap = _absolutise(snap, client)
            stream = _absolutise(stream, client)
            if _usable(_probe(snap or stream)):
                log("info", "Camera: %r from Moonraker's webcam list",
                    cam.get("name", "?"))
                return CameraSource(
                    snap or stream, stream or None, origin="moonraker",
                    rotation=int(cam.get("rotation") or 0),
                    flip_h=bool(cam.get("flip_horizontal")),
                    flip_v=bool(cam.get("flip_vertical")))
    except Exception as exc:                                 # noqa: BLE001
        log("debug", "could not read Moonraker's webcam list: %s", exc)

    # 3. The nginx convention. This is the case where a camera is plainly
    # working but nobody ever added it in the frontend.
    host = client.host
    for path in NGINX_WEBCAM_PATHS:
        snap = "http://%s%s?action=snapshot" % (host, path)
        if _usable(_probe(snap)):
            log("info", "Camera: found at %s (not registered in Moonraker; "
                        "add it in Mainsail to control rotation there)", path)
            return CameraSource(
                snap, "http://%s%s?action=stream" % (host, path),
                origin="nginx")

    raise CameraUnavailable(
        "no camera found. Add one in Mainsail/Fluidd (Settings -> Webcams), "
        "or set snapshot_url in the [camera] section of the config file.")


def is_hls_or_webrtc_stream_url(url):
    """True if `url` is HLS or WebRTC.

    Neither is something an <img> tag can read, so a Live Camera toggle
    pointing at one would render a broken image. Parsed, not
    string-matched: a bare endswith(".m3u8") misses a query-stringed URL
    like `/stream.m3u8?token=...`, and a bare startswith("webrtc") would
    match a path that merely began with those letters. Same rule, same
    reasoning, as the OctoPrint build's check of the same name.
    """
    from urllib.parse import urlparse
    parts = urlparse(url or "")
    return (parts.scheme.lower().startswith("webrtc")
            or parts.path.lower().endswith(".m3u8"))


def _absolutise(url, client):
    """Turn Moonraker's relative webcam URLs into fetchable ones."""
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "http://%s/%s" % (client.host, url.lstrip("/"))


def transform_image(image, source, logger=None):
    """Apply the camera's flip and rotation to a decoded frame.

    ⚠️ This must run BEFORE the mask and before inference. Moonraker
    reports the rotation and flips the user set in Mainsail, and PiNozCam
    read them into CameraSource and then never applied them -- so the
    detector saw a differently-oriented picture from the one the frontend
    shows, boxes came back in the wrong place, and a mask painted on the
    displayed image covered the wrong region.

    Flips first, then rotation, matching the OctoPrint build's
    transform_image. ⚠️ `Image.rotate(90, expand=True)` is
    COUNTER-clockwise; a rotation is not a transpose.

    Moonraker's rotation is degrees (0/90/180/270), not OctoPrint's single
    rotate90 boolean, so all four are handled.
    """
    from PIL import Image
    flip_h = bool(getattr(source, "flip_h", False))
    flip_v = bool(getattr(source, "flip_v", False))
    rotation = int(getattr(source, "rotation", 0) or 0) % 360
    if not (flip_h or flip_v or rotation):
        return image
    flags = (flip_h, flip_v, rotation)
    if logger is not None and getattr(source, "_logged_transform", None) != flags:
        source._logged_transform = flags
        # Once per change, not per frame: at the default cadence that would
        # be five identical lines a second.
        logger.info("Camera transform: flipH=%s flipV=%s rotate=%d",
                    flip_h, flip_v, rotation)
    if flip_h:
        image = image.transpose(Image.FLIP_LEFT_RIGHT)
    if flip_v:
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
    if rotation:
        # ⚠️ NEGATED on purpose. Moonraker's webcam spec defines `rotation`
        # as "the amount of CLOCKWISE rotation" and Mainsail applies it as
        # CSS `rotate(Ndeg)`, which is clockwise for a positive angle.
        # PIL's Image.rotate is COUNTER-clockwise. Passing the value
        # straight through turned every 90/270 camera 180 degrees away
        # from what the user configured and from what Mainsail shows --
        # and this detector is not rotation invariant, so that is a
        # detection-accuracy defect, not a cosmetic one.
        image = image.rotate(-rotation, expand=True)
    return image


def transform_jpeg(jpeg, source, logger=None):
    """Apply the camera transform to encoded JPEG bytes.

    For callers that hold bytes rather than an image -- the live-view
    fallback. Without this the page showed an UNtransformed picture between
    prints and a transformed one during them, and the mask editor painted
    on whichever it happened to get.
    """
    if not jpeg:
        return jpeg
    if not (getattr(source, "flip_h", False)
            or getattr(source, "flip_v", False)
            or int(getattr(source, "rotation", 0) or 0) % 360):
        return jpeg                     # nothing to do; keep the bytes
    from io import BytesIO
    from PIL import Image
    try:
        image = transform_image(
            Image.open(BytesIO(jpeg)).convert("RGB"), source, logger=logger)
        out = BytesIO()
        image.save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:                                        # noqa: BLE001
        return jpeg


def build_frame_source(source, logger=None, identity="pinozcam"):
    """The right FrameSource for this camera's URL.

    ⚠️ Do not hardcode HttpSnapshotFrameSource here. The shared framesource
    module ships three readers and the URL decides which one is correct:
    an endless `multipart/x-mixed-replace` stream fed to the snapshot
    reader never returns a frame -- it just runs until that reader's own
    size/time bound trips and raises -- and a `file://` URL is not
    something `requests` can open at all. Both of those are documented as
    supported camera sources, and both were broken while this function did
    not exist.

    Same selection the OctoPrint build's CameraSourceFactory makes.
    """
    from . import framesource
    # ⚠️ Prefer the STREAM when the camera offers one. A snapshot source
    # re-opens an HTTP request per frame; an MJPEG stream is pushed, so it
    # costs one connection and delivers frames as fast as the camera makes
    # them. Moonraker's webcam entries normally carry both, and this used
    # to start from snapshot_url unconditionally, so the common
    # crowsnest setup always took the slower path.
    stream = getattr(source, "stream_url", "") or ""
    if stream and stream != source.snapshot_url and not \
            is_hls_or_webrtc_stream_url(stream):
        if _probe(stream) == "multipart/x-mixed-replace":
            spec = framesource.SourceSpec("mjpeg", identity, stream, None)
            if logger:
                logger.info("Camera source: MJPEG stream (pushed)")
            return framesource.MjpegFrameSource(spec, logger=logger)

    url = source.snapshot_url or ""
    if url.startswith("file://"):
        spec = framesource.SourceSpec("file", identity, url, None)
        if logger:
            logger.info("Camera source: static file")
        return framesource.StaticFileFrameSource(spec, logger=logger)
    if _probe(url) == "multipart/x-mixed-replace":
        spec = framesource.SourceSpec("mjpeg", identity, url, None)
        if logger:
            logger.info("Camera source: MJPEG stream")
        return framesource.MjpegFrameSource(spec, logger=logger)
    spec = framesource.SourceSpec("snapshot", identity, url, None)
    if logger:
        logger.info("Camera source: HTTP snapshot")
    return framesource.HttpSnapshotFrameSource(spec, logger=logger)


def grab_jpeg(source, logger=None, timeout=8.0):
    """One JPEG from `source`, or None. For callers with no sampler.

    A throwaway reader rather than a bare `requests.get`, for the same
    reason build_frame_source exists: a stream and a local file are not
    fetchable that way. Started and closed inside this call, so it never
    leaves a reader thread behind.
    """
    import threading
    url = source.snapshot_url or ""
    if not url.startswith("file://"):
        # ⚠️ ONE request in the common case. This used to _probe() first and
        # then fetch, so every call cost two round trips to the camera --
        # and the page's idle path calls it twice a second. The response
        # itself says whether it is a still or a stream, so ask once and
        # read the answer.
        try:
            r = requests.get(url, stream=True, timeout=(3.0, 6.0))
            try:
                kind = (r.headers.get("Content-Type") or "").split(";")[0]
                if r.status_code == 200 and kind.strip() != \
                        "multipart/x-mixed-replace":
                    body = r.content
                    return body if body[:2] == b"\xff\xd8" else None
            finally:
                # Close without draining: a stream never ends on its own.
                r.close()
        except requests.RequestException:
            return None

    reader = build_frame_source(source, logger=logger)
    stop = threading.Event()
    try:
        reader.start()
        frame = reader.wait_next(-1, stop, timeout)
        return frame.jpeg_bytes if frame is not None else None
    except Exception as exc:                                 # noqa: BLE001
        if logger:
            logger.debug("could not grab a frame: %s", exc)
        return None
    finally:
        try:
            reader.close()
        except Exception:                                    # noqa: BLE001
            pass
