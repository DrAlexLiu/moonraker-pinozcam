"""Read the INI configuration.

The config file is the only settings surface this program has: there is no
dialog to fill in defaults, so every value must have a sane fallback and an
unreadable file must fail with a message a user can act on.
"""

import configparser
import io
import os
import threading


class ConfigError(Exception):
    """The configuration file is missing or unusable."""


class Config(object):
    """Typed access to moonraker-pinozcam.cfg."""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        if not os.path.isfile(self.path):
            raise ConfigError("config file not found: %s" % self.path)
        self._lock = threading.RLock()
        self._cp = configparser.ConfigParser()
        # ⚠️ Stamp BEFORE reading, never after. Between the stat and the
        # read someone may save; a stamp taken afterwards would name a
        # moment newer than the content actually loaded, and
        # reload_if_changed would then see no change and keep the stale
        # copy. Taken first, the worst case is one redundant re-read.
        self._mtime = self._stamp()
        try:
            self._cp.read(self.path)
        except configparser.Error as exc:
            raise ConfigError("%s is not valid INI: %s" % (self.path, exc))

    def _stamp(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def reload_if_changed(self):
        """Re-read the file if it changed on disk. True if it did.

        Mainsail and Fluidd edit this file in the browser, and the docs
        tell users to -- so "saved settings take effect" cannot mean only
        "settings saved through our own page". Cheap enough to call every
        detection tick: one stat().

        ⚠️ There is no "first call" case, and there must not be one. An
        earlier version established its baseline here instead of in
        __init__ and returned False without re-reading -- so an edit made
        between construction and the first poll advanced the baseline
        while the old content stayed loaded, and that edit was lost for
        good rather than merely delayed. __init__ stamps the file it read;
        every call from then on is a real comparison.
        """
        stamp = self._stamp()
        if stamp is None or stamp == self._mtime:
            return False
        parser = configparser.ConfigParser()
        try:
            parser.read(self.path)
        except configparser.Error:
            # Mid-save: leave the stamp alone so the next tick tries again
            # rather than treating a half-written file as seen.
            return False
        with self._lock:
            self._cp = parser
            self._mtime = stamp
        return True

    def _get(self, section, option, fallback=None):
        try:
            return self._cp.get(section, option)
        except (configparser.NoSectionError, configparser.NoOptionError):
            return fallback

    def get(self, section, option, fallback=None):
        v = self._get(section, option, fallback)
        return v.strip() if isinstance(v, str) else v

    def getint(self, section, option, fallback=0):
        v = self._get(section, option)
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    def getfloat(self, section, option, fallback=0.0):
        v = self._get(section, option)
        try:
            return float(str(v).strip())
        except (TypeError, ValueError):
            return fallback

    def getbool(self, section, option, fallback=False):
        v = self._get(section, option)
        if v is None:
            return fallback
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    def get_section(self, section):
        """Return one section as a dict, with values typed by name.

        Bot credentials are read this way rather than through a typed
        property because the two bots take different key names and a
        missing section must read as "not configured", not as an error.
        """
        out = {}
        if not self._cp.has_section(section):
            return out
        for key in self._cp.options(section):
            raw = self.get(section, key)
            if key in ("enabled",):
                out[key] = self.getbool(section, key, False)
            else:
                out[key] = raw
        return out

    def write_options(self, section, values):
        """Write options back, preserving comments and layout.

        ConfigParser would round-trip this file by rewriting it, which
        discards every comment -- and in this build the comments ARE the
        documentation: there is no settings dialog explaining what
        failure_ratio means. So edit the lines in place instead, and append
        to the section only for keys that are not there yet.

        ⚠️ Read-modify-write, so it must not interleave. The web server is
        a ThreadingHTTPServer: saving settings and saving the mask are two
        requests that can arrive together, and each one rewrites the whole
        file from what it read. Without this lock the later writer wins
        and the other's change is simply gone -- measured as lost updates
        and FileNotFoundError, the latter because both writers used one
        shared temporary path and the first rename removed it under the
        second.
        """
        with self._lock:
            self._write_options_locked(section, values)

    def _write_options_locked(self, section, values):
        with io.open(self.path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        header = "[%s]" % section
        start = None
        for index, line in enumerate(lines):
            if line.strip() == header:
                start = index
                break
        if start is None:
            lines.extend(["", header])
            start = len(lines) - 1

        end = len(lines)
        for index in range(start + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                end = index
                break

        remaining = dict(values)
        for index in range(start + 1, end):
            stripped = lines[index].lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            key = stripped.split("=", 1)[0].strip()
            if key in remaining:
                lines[index] = "%s = %s" % (key, remaining.pop(key))

        if remaining:
            # Insert before any trailing blank lines so the section does not
            # grow a gap in the middle each time this runs.
            insert_at = end
            while insert_at > start + 1 and not lines[insert_at - 1].strip():
                insert_at -= 1
            for key in sorted(remaining):
                lines.insert(insert_at, "%s = %s" % (key, remaining[key]))
                insert_at += 1

        # Write via a temporary file in the same directory: a truncated
        # config on a power cut would stop the service from starting. The
        # name carries the pid and this thread's id so two writers cannot
        # share one, which the lock above already prevents inside a single
        # process -- but a second instance pointed at the same config is a
        # supported layout and must not corrupt it either.
        temp = "%s.%d.%d.tmp" % (self.path, os.getpid(),
                                 threading.current_thread().ident or 0)
        try:
            with io.open(temp, "w", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
            os.replace(temp, self.path)
        except BaseException:
            # Never leave a stray temp behind for a config editor to show
            # the user as a file they did not create.
            try:
                os.unlink(temp)
            except OSError:
                pass
            raise

        self._cp = configparser.ConfigParser()
        self._cp.read(self.path)
        # The content just written IS what is loaded, so stamp it: leaving
        # the old stamp makes the next reload_if_changed re-read our own
        # write and report it as an external edit, which resets the
        # detector's baseline for no reason.
        self._mtime = self._stamp()

    # ---- typed views -------------------------------------------------

    @property
    def moonraker(self):
        return {
            "host": self.get("moonraker", "host", "127.0.0.1"),
            "port": self.getint("moonraker", "port", 7125),
            "api_key": self.get("moonraker", "api_key") or None,
        }

    @property
    def camera(self):
        return {
            # Empty means "ask Moonraker" -- the frontend already knows
            # which camera the user picked, including its rotation.
            "snapshot_url": self.get("camera", "snapshot_url") or None,
            "rotate": self.getint("camera", "rotate", 0),
            "flip_h": self.getbool("camera", "flip_h", False),
            "flip_v": self.getbool("camera", "flip_v", False),
            # MASK_GRID x MASK_GRID of "0"/"1", row-major, "1" = ignore.
            "mask_image_data": self.get("camera", "mask_image_data", ""),
            # The coordinate system that mask was painted in; see mask.py.
            "mask_signature": self.get("camera", "mask_signature", ""),
        }

    @property
    def detection(self):
        """Detection settings.

        Names, defaults and ranges mirror the OctoPrint build's
        settings_schema.py so the two behave identically. The single
        deliberate difference is cpu_share; see the config sample.
        """
        return {
            "enable_ai": self.getbool("detection", "enable_ai", True),
            "ai_backend": self.get("detection", "ai_backend", "auto"),
            "action": self.getint("detection", "action", 0),
            "ai_start_delay": self.getint("detection", "ai_start_delay", 0),
            "detection_interval": self.getint(
                "detection", "detection_interval", 0),
            "img_sensitivity": self.getfloat(
                "detection", "img_sensitivity", 0.04),
            "scores_threshold": self.getfloat(
                "detection", "scores_threshold", 0.87),
            "count_time": self.getint("detection", "count_time", 120),
            "failure_ratio": self.getfloat(
                "detection", "failure_ratio", 0.05),
            "print_layout_threshold": self.getfloat(
                "detection", "print_layout_threshold", 0.5),
            # Milliseconds on the wire, seconds in the attribute -- the
            # same convention the OctoPrint schema documents.
            "frame_sample_interval": self.getint(
                "detection", "frame_sample_interval", 200) / 1000.0,
            "frame_buffer_max_age": self.getint(
                "detection", "frame_buffer_max_age", 4),
            "frame_buffer_capacity": self.getint(
                "detection", "frame_buffer_capacity", 5),
            # 0.75 here, 0.5 in the OctoPrint build. Klipper's ~1 s MCU
            # buffer absorbs the host stalls OctoPrint's line-by-line
            # streaming cannot.
            "cpu_share": self.getfloat("detection", "cpu_share", 0.75),
        }

    @property
    def notification(self):
        return {
            "max_notification": self.getint(
                "notification", "max_notification", 0),
            "notify_interval": self.getint(
                "notification", "notify_interval", 60),
        }

    @property
    def web(self):
        return {
            "port": self.getint("web", "port", 58888),
            "register_webcam": self.getbool("web", "register_webcam", True),
            # Empty means no login at all, which is the default and what
            # this page has always done. Set it and the page and the whole
            # settings API ask for it; see AnnotatedView._authorised for
            # what deliberately stays open and why.
            "user": self.get("web", "user", "pinozcam"),
            "password": self.get("web", "password", ""),
        }

    # ⚠️ There is no `action` property, deliberately. An earlier one read
    # `[action] on_failure`, a section nothing ever writes -- not the
    # sample, not the settings page, not this file's own `detection`
    # property -- so it always fell back to its "pause" default and
    # "Alert only" paused the print anyway. The one true setting is
    # `[detection] action`, an int 0/1/2, matching the OctoPrint build's
    # single `action` setting.

    @property
    def logging(self):
        return {
            "path": self._log_path(),
            "level": (self.get("logging", "level", "INFO") or "INFO").upper(),
        }

    def state_path(self):
        """Where this service keeps its own small state, or None.

        Not the config file: the config is the user's, is edited in
        Mainsail, and its mtime drives reload_if_changed() -- writing to it
        from inside would both add noise to what the user reads and make
        the service trigger its own reload.

        Beside the config instead, dot-prefixed so a config editor's file
        list stays what the user put there. None when the file cannot be
        placed, which callers must treat as "no memory", never as an error.
        """
        config_dir = os.path.dirname(self.path)
        if not os.path.isdir(config_dir):
            return None
        return os.path.join(config_dir, ".moonraker-pinozcam.state")

    def _log_path(self):
        """The log file to write, or None for journald only.

        An ABSENT `path` means "use the default"; a `path` that is present
        but empty means "no file". Those have to be distinguished by asking
        whether the option exists, because get() strips whitespace, so the
        two look identical by value.
        """
        if self._cp.has_option("logging", "path"):
            return self.get("logging", "path") or None
        return self._default_log_path()

    def _default_log_path(self):
        """`<printer_data>/logs/moonraker-pinozcam.log`, if that exists.

        Logging to a file is ON by default, unlike a bare systemd service,
        because on a Klipper host that directory IS the log UI: Mainsail's
        Machine -> Logs page lists the files in it, and klippy, moonraker,
        crowsnest and KlipperScreen all write there. A service that only
        reached journald would be the one component a user cannot read
        without SSH.

        Derived from where the config file actually is rather than from
        $HOME, because a multi-printer host has several printer_data
        directories and this process is told which one by its --config
        path. Falls back to no file (journald only) if the layout is not
        the standard one -- guessing a path and creating a stray directory
        would be worse than logging to one place.
        """
        config_dir = os.path.dirname(self.path)          # .../config
        logs = os.path.join(os.path.dirname(config_dir), "logs")
        if os.path.isdir(logs):
            return os.path.join(logs, "moonraker-pinozcam.log")
        return None
