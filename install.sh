#!/bin/bash
# Install PiNozCam as a Moonraker-connected service.
#
#   ./install.sh [-c /path/to/moonraker.conf] [-n]
#
#   -c  Moonraker config to read/extend (default: autodetected)
#   -n  non-interactive; accept every default and never prompt
#
# Safe to re-run: an existing config is never overwritten, and Moonraker's
# conf is only appended to if our section is not in it yet.
set -euo pipefail

PROJECT_NAME=moonraker-pinozcam
SERVICE_NAME=moonraker-pinozcam
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_DIR}/../${PROJECT_NAME}-env"
NONINTERACTIVE=0
MOONRAKER_CONF=""

while getopts "c:nh" o; do
    case "${o}" in
        c) MOONRAKER_CONF="${OPTARG}" ;;
        n) NONINTERACTIVE=1 ;;
        *) sed -n '2,10p' "$0"; exit 0 ;;
    esac
done

say()  { echo -e "\n\033[1;36m==>\033[0m $*"; }
ok()   { echo -e "  \033[1;32m✓\033[0m $*"; }
warn() { echo -e "  \033[1;33m!\033[0m $*"; }
die()  { echo -e "\n\033[1;31merror:\033[0m $*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal printer user, not root."

# --------------------------------------------------------------------------
say "1/7  Locating your Klipper installation"

# Find printer_data by asking Moonraker's own service file where it points,
# rather than assuming ~/printer_data: multi-printer hosts have several.
if [ -z "${MOONRAKER_CONF}" ]; then
    for c in "${HOME}"/printer_data/config/moonraker.conf \
             "${HOME}"/klipper_config/moonraker.conf; do
        [ -f "$c" ] && { MOONRAKER_CONF="$c"; break; }
    done
fi
[ -n "${MOONRAKER_CONF}" ] && [ -f "${MOONRAKER_CONF}" ] \
    || die "moonraker.conf not found. Pass it with -c /path/to/moonraker.conf"

CONFIG_DIR="$(dirname "${MOONRAKER_CONF}")"
PRINTER_DATA="$(dirname "${CONFIG_DIR}")"
LOG_DIR="${PRINTER_DATA}/logs"
PINOZCAM_CONF="${CONFIG_DIR}/moonraker-pinozcam.cfg"
ok "moonraker.conf : ${MOONRAKER_CONF}"
ok "config dir     : ${CONFIG_DIR}"

# Read Moonraker's real port instead of assuming 7125.
MR_PORT=$(sed -n 's/^[[:space:]]*port:[[:space:]]*\([0-9]\+\).*/\1/p' \
          "${MOONRAKER_CONF}" | head -1)
MR_PORT="${MR_PORT:-7125}"
ok "moonraker port : ${MR_PORT}"

# --------------------------------------------------------------------------
say "2/7  System packages"
PKGS=""
command -v python3 >/dev/null || PKGS="${PKGS} python3"
python3 -c 'import venv' 2>/dev/null || PKGS="${PKGS} python3-venv"
# Pillow needs these only when no manylinux wheel matches the platform.
python3 -c 'import PIL' 2>/dev/null || PKGS="${PKGS} python3-dev libjpeg-dev zlib1g-dev"

if [ -n "${PKGS}" ]; then
    warn "installing:${PKGS}"
    sudo apt-get update -qq
    # shellcheck disable=SC2086
    sudo apt-get install --yes ${PKGS}
else
    ok "everything needed is already installed"
fi

# --------------------------------------------------------------------------
say "3/7  Accelerator runtime"
# PiNozCam can use an NPU where one exists. The kernel driver must come from
# the board image, but the userspace library often does not ship with it --
# BIGTREETECH's CB2 image, for instance, has the driver and no library, which
# silently costs a 6x slowdown. Install it here, matched to the driver.
bash "${PROJECT_DIR}/scripts/setup_accelerator.sh" \
    $([ "${NONINTERACTIVE}" = "1" ] && echo "-n") || \
    warn "accelerator setup skipped; PiNozCam will run on the CPU"

# --------------------------------------------------------------------------
say "4/7  Python environment"
if [ ! -d "${VENV_DIR}" ]; then
    python3 -m venv "${VENV_DIR}"
    ok "created ${VENV_DIR}"
else
    ok "reusing ${VENV_DIR}"
fi
"${VENV_DIR}/bin/pip" install -q --upgrade pip
"${VENV_DIR}/bin/pip" install -q -r "${PROJECT_DIR}/requirements.txt"
ok "dependencies installed"

# The runtime package carries the runner binaries and models. Which one is
# right depends on the hardware, and the NPU variants are not on PyPI --
# they install from a GitHub Release asset.
say "4b/7 Inference runtime"
"${VENV_DIR}/bin/python" "${PROJECT_DIR}/scripts/install_runtime.py" \
    "${VENV_DIR}/bin/pip" || warn "runtime not installed; detection will not run"

# --------------------------------------------------------------------------
say "5/7  Configuration"
if [ -f "${PINOZCAM_CONF}" ]; then
    ok "keeping your existing ${PINOZCAM_CONF##*/}"
else
    sed "s|^# path = .*|# path = ${LOG_DIR}/moonraker-pinozcam.log|; \
         s|^port = 7125|port = ${MR_PORT}|" \
        "${PROJECT_DIR}/moonraker-pinozcam.cfg.sample" > "${PINOZCAM_CONF}"
    ok "wrote ${PINOZCAM_CONF}"
    warn "edit it in Mainsail/Fluidd under Machine -> Configuration Files"
fi

# Register with Moonraker's update manager so the web UI can update us.
if grep -q "^\[update_manager ${PROJECT_NAME}\]" "${MOONRAKER_CONF}"; then
    ok "already registered with Moonraker's update manager"
else
    cp "${MOONRAKER_CONF}" "${MOONRAKER_CONF}.pinozcam-backup"
    {
        echo ""
        sed "s|~/${PROJECT_NAME}|${PROJECT_DIR}|; \
             s|~/${PROJECT_NAME}-env|${VENV_DIR}|" \
            "${PROJECT_DIR}/moonraker-pinozcam-update.cfg.sample"
    } >> "${MOONRAKER_CONF}"
    ok "registered (backup: ${MOONRAKER_CONF##*/}.pinozcam-backup)"
fi

# --------------------------------------------------------------------------
say "6/7  Service"
sudo /bin/sh -c "cat > /etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=PiNozCam - local AI print-failure detection for Klipper
After=network-online.target moonraker.service
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${VENV_DIR}/bin/python -m moonraker_pinozcam -c ${PINOZCAM_CONF}
Restart=on-failure
RestartSec=10
# Deliberately NOT nice'd as a whole: the inference subprocess carries its
# own niceness, while this parent also handles alerts that must not be
# delayed behind a busy detector.

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}" >/dev/null 2>&1
ok "installed ${SERVICE_NAME}.service"

# --------------------------------------------------------------------------
say "7/7  Starting"
sudo systemctl restart "${SERVICE_NAME}"
sleep 3
if systemctl is-active --quiet "${SERVICE_NAME}"; then
    ok "running"
else
    warn "not running yet -- check: journalctl -u ${SERVICE_NAME} -n 40"
fi

cat <<DONE

  PiNozCam is installed.

  Settings   ${PINOZCAM_CONF}
             (editable from Mainsail/Fluidd: Machine -> Configuration Files)
  Logs       journalctl -u ${SERVICE_NAME} -f
  Restart    sudo systemctl restart ${SERVICE_NAME}

DONE
