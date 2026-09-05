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


def _absolutise(url, client):
    """Turn Moonraker's relative webcam URLs into fetchable ones."""
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return "http://%s/%s" % (client.host, url.lstrip("/"))
