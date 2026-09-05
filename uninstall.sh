#!/bin/bash
# Remove PiNozCam from this printer.
#
#   ./uninstall.sh [-y] [-c /path/to/moonraker.conf]
#
#   -y  do not ask for confirmation
#   -c  Moonraker config to clean up (default: autodetected)
#
# What it removes: the service, the virtualenv, our entry in moonraker.conf
# and the webcam we registered. What it KEEPS: your configuration file, and
# librknnrt.so if we installed it.
#
# The config is kept on purpose -- it holds bot tokens and tuning a user
# spent time on, and reinstalling should not make them do that again. It is
# a plain text file they can delete themselves; silently destroying it is
# the kind of thing an uninstaller should never do.
set -euo pipefail

PROJECT_NAME=moonraker-pinozcam
SERVICE_NAME=moonraker-pinozcam
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/../${PROJECT_NAME}-env"
ASSUME_YES=0
MOONRAKER_CONF=""

while getopts "yc:h" o; do
    case "${o}" in
        y) ASSUME_YES=1 ;;
        c) MOONRAKER_CONF="${OPTARG}" ;;
        *) sed -n '2,14p' "$0"; exit 0 ;;
    esac
done

say()  { echo -e "\n\033[1;36m==>\033[0m $*"; }
ok()   { echo -e "  \033[1;32m✓\033[0m $*"; }
warn() { echo -e "  \033[1;33m!\033[0m $*"; }

[ "$(id -u)" -ne 0 ] || { echo "run as your printer user, not root" >&2; exit 1; }

if [ -z "${MOONRAKER_CONF}" ]; then
    for c in "${HOME}"/printer_data/config/moonraker.conf \
             "${HOME}"/klipper_config/moonraker.conf; do
        [ -f "$c" ] && { MOONRAKER_CONF="$c"; break; }
    done
fi

if [ "${ASSUME_YES}" != "1" ]; then
    echo "This will remove the ${SERVICE_NAME} service and its virtualenv."
    echo "Your configuration file will be kept."
    read -r -p "Continue? [y/N] " reply
    case "${reply}" in [yY]*) ;; *) echo "Cancelled."; exit 0 ;; esac
fi

# --------------------------------------------------------------------------
say "1/4  Stopping the service"
# NOTE Do not detect this with `systemctl list-unit-files | grep -q`. Under
# `set -o pipefail`, grep -q exits at the first match, systemctl dies of
# SIGPIPE writing the remaining ~360 lines, and the pipeline reports
# failure -- so the branch is skipped and the service is left running while
# the script cheerfully reports "no service was installed". Ask systemd
# about the one unit instead; no pipe, no early exit.
if systemctl cat "${SERVICE_NAME}.service" >/dev/null 2>&1 \
   || [ -f "/etc/systemd/system/${SERVICE_NAME}.service" ]; then
    sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
    sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
    sudo rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    sudo systemctl daemon-reload
    sudo systemctl reset-failed 2>/dev/null || true
    ok "service removed"
else
    ok "no service was installed"
fi

# --------------------------------------------------------------------------
say "2/4  Removing the webcam entry"
# Best effort: Moonraker may already be gone, or never have had the entry.
MR_PORT=7125
if [ -n "${MOONRAKER_CONF}" ] && [ -f "${MOONRAKER_CONF}" ]; then
    p=$(sed -n 's/^[[:space:]]*port:[[:space:]]*\([0-9]\+\).*/\1/p' \
        "${MOONRAKER_CONF}" | head -1)
    MR_PORT="${p:-7125}"
fi
if curl -fsS -m 5 -X DELETE \
     "http://127.0.0.1:${MR_PORT}/server/webcams/item?name=PiNozCam" \
     >/dev/null 2>&1; then
    ok "webcam entry removed from Moonraker"
else
    ok "no webcam entry to remove"
fi

# --------------------------------------------------------------------------
say "3/4  Cleaning up moonraker.conf"
if [ -n "${MOONRAKER_CONF}" ] && [ -f "${MOONRAKER_CONF}" ] \
   && grep -q "^\[update_manager ${PROJECT_NAME}\]" "${MOONRAKER_CONF}"; then
    cp "${MOONRAKER_CONF}" "${MOONRAKER_CONF}.pinozcam-uninstall-backup"
    # Delete our section and everything up to the next section header or EOF.
    # A blank-line-delimited delete would stop at the first empty line inside
    # the block, leaving orphaned keys that make Moonraker refuse to start.
    python3 - "${MOONRAKER_CONF}" "${PROJECT_NAME}" <<'PYEOF'
import io, re, sys
path, name = sys.argv[1], sys.argv[2]
text = io.open(path, encoding="utf-8").read()
pattern = re.compile(
    r"\n*^\[update_manager %s\][^\n]*\n(?:(?!^\[).*\n?)*" % re.escape(name),
    re.MULTILINE)
io.open(path, "w", encoding="utf-8").write(pattern.sub("\n", text))
PYEOF
    ok "update_manager entry removed (backup kept)"
    warn "restart Moonraker for that to take effect"
else
    ok "nothing to clean up in moonraker.conf"
fi

# --------------------------------------------------------------------------
say "4/4  Removing the virtualenv"
if [ -d "${VENV_DIR}" ]; then
    rm -rf "${VENV_DIR}"
    ok "removed ${VENV_DIR}"
else
    ok "no virtualenv found"
fi

CFG_DIR="$(dirname "${MOONRAKER_CONF:-$HOME/printer_data/config/x}")"
cat <<DONE

  PiNozCam has been removed.

  Kept on purpose:
    ${CFG_DIR}/moonraker-pinozcam.cfg   your settings and bot tokens
    /usr/lib/librknnrt.so                 shared with other RKNN software
    ${PROJECT_DIR}                        this directory; delete it yourself

  Also still installed: the runtime package, inside the virtualenv that was
  just deleted -- nothing else to do.

DONE
