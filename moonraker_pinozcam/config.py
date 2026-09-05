"""Read the INI configuration.

The config file is the only settings surface this program has: there is no
dialog to fill in defaults, so every value must have a sane fallback and an
unreadable file must fail with a message a user can act on.
"""

import configparser
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
        }

    @property
    def detection(self):
        return {
            "score_threshold": self.getfloat("detection", "score_threshold", 0.87),
            "sensitivity": self.getfloat("detection", "sensitivity", 0.04),
            "failure_ratio": self.getfloat("detection", "failure_ratio", 0.30),
            "count_time": self.getint("detection", "count_time", 300),
            "start_delay": self.getint("detection", "start_delay", 60),
            "cpu_percent": self.getint("detection", "cpu_percent", 75),
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
