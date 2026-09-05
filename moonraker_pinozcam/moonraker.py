"""Moonraker client: printer state over WebSocket, control over REST.

Threading, not asyncio, on purpose. The bot modules this service reuses are written against threads and
blocking I/O, so keeping that model lets them be shared verbatim -- worth
more than an event loop this program has no other use for.
"""

import json
import threading
import time

import requests
import websocket

# Objects PiNozCam needs. Subscribing narrowly matters: Moonraker pushes an
# update whenever any subscribed field changes, and print_stats alone is
# noisy enough during a print.
SUBSCRIBE = {
    "print_stats": ["state", "filename", "print_duration", "message"],
    "virtual_sdcard": ["is_active", "progress"],
    "pause_resume": ["is_paused"],
    "webhooks": ["state", "state_message"],
}

# Reconnect backoff. Moonraker restarts whenever the user saves a config
# from Mainsail, so disconnects are routine rather than exceptional.
INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 30.0

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 15.0


class MoonrakerError(Exception):
    """Moonraker refused a request or could not be reached."""


class PrinterState(object):
    """The subset of printer state PiNozCam acts on."""

    def __init__(self):
        self.state = "unknown"      # standby/printing/paused/complete/error
        self.filename = ""
        self.print_duration = 0.0
        self.is_paused = False
        self.klippy_state = "unknown"
        self.updated_at = 0.0

    @property
    def is_printing(self):
        """Whether a job is actively laying down material.

        Paused is deliberately excluded: a paused print is not producing
        new failures, and re-alarming on a frozen frame would spam the
        user with an alert they have already acted on.
        """
        return self.state == "printing" and not self.is_paused

    def __repr__(self):
        return "<PrinterState %s%s file=%r>" % (
            self.state, " (paused)" if self.is_paused else "", self.filename)


class MoonrakerClient(object):
    """One connection to Moonraker, reconnecting for the process's life.

    The WebSocket carries state; control commands go over REST. Splitting
    them keeps a control call from being silently lost while the socket is
    mid-reconnect -- a REST call fails loudly instead.
    """

    def __init__(self, host="127.0.0.1", port=7125, api_key=None,
                 logger=None, on_state_change=None):
        self.host = host
        self.port = int(port)
        self.api_key = api_key or None
        self._logger = logger
        self._on_state_change = on_state_change

        self.state = PrinterState()
        self.connected = False

        self._ws = None
        self._thread = None
        self._stop = threading.Event()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._session = requests.Session()
        if self.api_key:
            self._session.headers["X-Api-Key"] = self.api_key

    # ---- addressing ---------------------------------------------------

    @property
    def http_base(self):
        return "http://%s:%d" % (self.host, self.port)

    @property
    def ws_url(self):
        url = "ws://%s:%d/websocket" % (self.host, self.port)
        # Moonraker accepts the key as a query parameter on the WebSocket;
        # there is no header stage to attach it to during the handshake.
        if self.api_key:
            url += "?token=%s" % self.api_key
        return url

    def _log(self, level, msg, *args):
        if self._logger is not None:
            getattr(self._logger, level)(msg, *args)

    # ---- lifecycle ----------------------------------------------------

    def start(self):
        """Begin connecting in the background. Returns immediately."""
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="pinozcam-moonraker", daemon=True)
        self._thread.start()

    def stop(self, timeout=10.0):
        """Stop reconnecting and close the socket."""
        self._stop.set()
        ws = self._ws
        if ws is not None:
            try:
                ws.close()
            except Exception:                                # noqa: BLE001
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)

    def wait_until_connected(self, timeout=30.0):
        """Block until the first successful subscription, or time out."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.connected:
                return True
            if self._stop.wait(0.2):
                return False
        return False

    def _run(self):
        """Connect, subscribe, read, and reconnect until stopped."""
        backoff = INITIAL_BACKOFF
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._session_once()
            except Exception as exc:                         # noqa: BLE001
                self._log("warning", "Moonraker connection lost: %s", exc)
            finally:
                self.connected = False

            if self._stop.is_set():
                return
            # A connection that lasted a while was healthy; do not carry a
            # long backoff over from an unrelated earlier failure.
            if time.monotonic() - started > 60.0:
                backoff = INITIAL_BACKOFF
            self._log("info", "Reconnecting to Moonraker in %.0fs", backoff)
            if self._stop.wait(backoff):
                return
            backoff = min(backoff * 2, MAX_BACKOFF)

    def _session_once(self):
        """One connection: subscribe, then read until the socket closes."""
        self._ws = websocket.create_connection(
            self.ws_url, timeout=CONNECT_TIMEOUT)
        try:
            # A short read timeout is what lets stop() take effect promptly;
            # without it a quiet printer blocks the reader indefinitely.
            self._ws.settimeout(1.0)
            self._subscribe()
            self.connected = True
            self._log("info", "Connected to Moonraker at %s", self.http_base)
            while not self._stop.is_set():
                try:
                    raw = self._ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    return                                   # closed cleanly
                self._handle(raw)
        finally:
            try:
                self._ws.close()
            except Exception:                                # noqa: BLE001
                pass
            self._ws = None

    # ---- protocol -----------------------------------------------------

    def _rpc_id(self):
        with self._id_lock:
            self._next_id += 1
            return self._next_id

    def _subscribe(self):
        """Subscribe and seed state from the reply's immediate snapshot."""
        req = {
            "jsonrpc": "2.0",
            "method": "printer.objects.subscribe",
            "params": {"objects": SUBSCRIBE},
            "id": self._rpc_id(),
        }
        self._ws.send(json.dumps(req))
        # The subscribe reply carries current values, so state is correct
        # from the first moment rather than after the first change.
        deadline = time.monotonic() + READ_TIMEOUT
        while time.monotonic() < deadline:
            try:
                raw = self._ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            msg = json.loads(raw)
            if msg.get("id") == req["id"]:
                status = (msg.get("result") or {}).get("status") or {}
                self._apply(status, initial=True)
                return
            self._handle(raw)         # an event can arrive before the reply
        raise MoonrakerError("Moonraker did not answer the subscription")

    def _handle(self, raw):
        """Dispatch one incoming WebSocket message."""
        try:
            msg = json.loads(raw)
        except ValueError:
            return
        method = msg.get("method")
        if method == "notify_status_update":
            params = msg.get("params") or [{}]
            self._apply(params[0] if params else {})
        elif method == "notify_klippy_shutdown":
            self.state.klippy_state = "shutdown"
            self._emit()
        elif method == "notify_klippy_disconnected":
            self.state.klippy_state = "disconnected"
            self._emit()
        elif method == "notify_klippy_ready":
            self.state.klippy_state = "ready"
            self._emit()

    def _apply(self, status, initial=False):
        """Merge a partial status update into local state.

        Moonraker sends only changed fields, so this must merge rather than
        replace: a print_stats update carrying just print_duration would
        otherwise wipe the filename.
        """
        changed = False
        ps = status.get("print_stats") or {}
        if "state" in ps and ps["state"] != self.state.state:
            self.state.state, changed = ps["state"], True
        if "filename" in ps:
            self.state.filename = ps["filename"] or ""
        if "print_duration" in ps:
            self.state.print_duration = ps["print_duration"] or 0.0

        pr = status.get("pause_resume") or {}
        if "is_paused" in pr and pr["is_paused"] != self.state.is_paused:
            self.state.is_paused, changed = bool(pr["is_paused"]), True

        wh = status.get("webhooks") or {}
        if "state" in wh and wh["state"] != self.state.klippy_state:
            self.state.klippy_state, changed = wh["state"], True

        self.state.updated_at = time.time()
        if changed or initial:
            self._emit()

    def _emit(self):
        """Notify the owner, never letting its exception kill the reader."""
        if self._on_state_change is None:
            return
        try:
            self._on_state_change(self.state)
        except Exception as exc:                             # noqa: BLE001
            self._log("error", "state-change handler raised: %s", exc)

    # ---- REST ---------------------------------------------------------

    def _post(self, path):
        try:
            r = self._session.post(
                "%s/%s" % (self.http_base, path.lstrip("/")),
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            return r.json().get("result")
        except requests.RequestException as exc:
            raise MoonrakerError("%s failed: %s" % (path, exc)) from exc

    def _get(self, path, params=None):
        try:
            r = self._session.get(
                "%s/%s" % (self.http_base, path.lstrip("/")),
                params=params, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            return r.json().get("result")
        except requests.RequestException as exc:
            raise MoonrakerError("%s failed: %s" % (path, exc)) from exc

    def pause_print(self):
        """Pause. Idempotent at Moonraker's end if already paused."""
        return self._post("printer/print/pause")

    def resume_print(self):
        return self._post("printer/print/resume")

    def cancel_print(self):
        return self._post("printer/print/cancel")

    def server_info(self):
        return self._get("server/info")

    def printer_info(self):
        return self._get("printer/info")

    # ---- webcams ------------------------------------------------------

    def list_webcams(self):
        """Return the webcams the user configured in Mainsail/Fluidd.

        Preferring this over a hand-entered URL also inherits the rotation
        and flip settings the user already set once in the frontend.
        """
        result = self._get("server/webcams/list") or {}
        return result.get("webcams") or []

    def register_webcam(self, name, snapshot_url, stream_url,
                        icon="mdiCctv"):
        """Add or update a webcam entry so frontends can display it.

        Used to publish PiNozCam's annotated view. Moonraker keys entries
        by name, so re-registering updates in place instead of duplicating.
        """
        payload = {
            "name": name,
            "service": "mjpegstreamer",
            "snapshot_url": snapshot_url,
            "stream_url": stream_url,
            "icon": icon,
            "enabled": True,
        }
        try:
            r = self._session.post(
                "%s/server/webcams/item" % self.http_base, json=payload,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
            return r.json().get("result")
        except requests.RequestException as exc:
            raise MoonrakerError("registering webcam failed: %s" % exc) from exc

    def unregister_webcam(self, name):
        try:
            r = self._session.delete(
                "%s/server/webcams/item" % self.http_base,
                params={"name": name},
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
            r.raise_for_status()
        except requests.RequestException as exc:
            raise MoonrakerError("removing webcam failed: %s" % exc) from exc
