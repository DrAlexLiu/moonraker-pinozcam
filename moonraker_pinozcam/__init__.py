"""PiNozCam for Moonraker -- local AI print-failure detection for Klipper."""

# Tracks the OctoPrint build's version, since both install the same runtime
# wheel and share the inference core. tools/check_upstream_sync.py checks
# the runtime pin against upstream's latest release.
__version__ = "1.1.0"
