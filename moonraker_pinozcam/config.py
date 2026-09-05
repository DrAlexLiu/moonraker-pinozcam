"""Read the INI configuration.

The config file is the only settings surface this program has: there is no
dialog to fill in defaults, so every value must have a sane fallback and an
unreadable file must fail with a message a user can act on.
"""

import configparser
import io
import os


class ConfigError(Exception):
    """The configuration file is missing or unusable."""


class Config(object):
    """Typed access to moonraker-pinozcam.cfg."""

    def __init__(self, path):
        self.path = os.path.expanduser(path)
        if not os.path.isfile(self.path):
            raise ConfigError("config file not found: %s" % self.path)
        self._cp = configparser.ConfigParser()
        try:
            self._cp.read(self.path)
        except configparser.Error as exc:
            raise ConfigError("%s is not valid INI: %s" % (self.path, exc))

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
        """
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
        # config on a power cut would stop the service from starting.
        temp = self.path + ".tmp"
        with io.open(temp, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        os.replace(temp, self.path)

        self._cp = configparser.ConfigParser()
        self._cp.read(self.path)

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
        }

    @property
    def action(self):
        return {"on_failure": self.get("action", "on_failure", "pause")}

    @property
    def logging(self):
        return {
            "path": self.get("logging", "path") or None,
            "level": (self.get("logging", "level", "INFO") or "INFO").upper(),
        }
