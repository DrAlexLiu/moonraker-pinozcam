#!/bin/bash
# Detect this board's AI accelerator and make sure its userspace runtime is
# present. Called by install.sh; safe to run on its own.
#
# Why this exists: a board image can ship the kernel driver for an NPU while
# omitting the userspace library that every application needs. Nothing fails
# loudly in that state -- the hardware is there, the driver is loaded, and
# applications quietly fall back to the CPU. On a BIGTREETECH CB2 that costs
# a measured 6x (257 ms -> 1544 ms per frame).
#
# Rockchip publishes no static librknnrt.a, so an application cannot bundle
# the library into its own package; it has to be installed system-wide.
set -euo pipefail

NONINTERACTIVE=0
[ "${1:-}" = "-n" ] && NONINTERACTIVE=1

ok()   { echo -e "  \033[1;32m✓\033[0m $*"; }
warn() { echo -e "  \033[1;33m!\033[0m $*"; }
info() { echo -e "  $*"; }

# --- Which chip is this? ---------------------------------------------------
# Read the device tree, never a hostname or a product name: the same board
# model ships under several names, and names are not capabilities.
CHIP=""
if [ -r /proc/device-tree/compatible ]; then
    CHIP=$(tr -d '\0' < /proc/device-tree/compatible | tr ',' '\n' \
           | grep -oE '^rk3[0-9]{3}$' | head -1)
fi

if [ -z "${CHIP}" ]; then
    info "no Rockchip NPU on this board -- PiNozCam will use the CPU."
    exit 0
fi
ok "Rockchip ${CHIP} detected"

case "${CHIP}" in
    rk3566|rk3568|rk3576|rk3588) ;;
    *) warn "${CHIP} has no NPU PiNozCam supports; using the CPU."; exit 0 ;;
esac

# --- Is the kernel driver there? ------------------------------------------
# Identify the NPU by its udev path, not by device-node number. On a 6.1
# kernel a CB2 exposes six DRM nodes and only two belong to the NPU; the
# others are the display subsystem and the Mali GPU.
NPU_NODES=""
for n in /dev/rknpu /dev/dri/card* /dev/dri/renderD*; do
    [ -e "$n" ] || continue
    p=$(udevadm info --query=path --name="$n" 2>/dev/null || true)
    case "$n:$p" in
        /dev/rknpu:*|*:*npu*) NPU_NODES="${NPU_NODES} $n" ;;
    esac
done

if [ -z "${NPU_NODES}" ]; then
    warn "NPU driver not active (no device node). PiNozCam will use the CPU."
    warn "This needs a kernel with RKNPU enabled -- ask your board vendor."
    exit 0
fi
ok "NPU device:${NPU_NODES}"

DRIVER_VER=$(dmesg 2>/dev/null | grep -oE 'Initialized rknpu [0-9.]+' \
             | tail -1 | awk '{print $3}' || true)
[ -n "${DRIVER_VER}" ] && ok "NPU driver ${DRIVER_VER}"

# --- Can we reach it? ------------------------------------------------------
# Report only; never widen permissions silently. Granting a group is an
# administrator's decision, and the right group differs per image (the GID
# for "render" is 107 on some, 991 on others) so it must be read, not assumed.
DENIED=""
for n in ${NPU_NODES}; do
    { [ -r "$n" ] && [ -w "$n" ]; } || DENIED="${DENIED} $n($(stat -c %G "$n"))"
done
if [ -n "${DENIED}" ]; then
    warn "no access to:${DENIED}"
    GROUPS_NEEDED=$(for n in ${NPU_NODES}; do stat -c %G "$n"; done | sort -u | paste -sd,)
    warn "fix with:  sudo usermod -aG ${GROUPS_NEEDED} ${USER}   (then log out and back in)"
else
    ok "device permissions are correct"
fi

# --- The userspace library -------------------------------------------------
for p in /usr/lib/librknnrt.so /usr/lib/aarch64-linux-gnu/librknnrt.so; do
    if [ -e "$p" ]; then
        V=$(strings "$p" 2>/dev/null | grep -m1 'librknnrt version' || echo "unknown version")
        ok "librknnrt.so present (${V#librknnrt version: })"
        exit 0
    fi
done

warn "librknnrt.so is MISSING -- the NPU cannot be used without it."
info ""
info "  Your board has a working ${CHIP} NPU and its kernel driver, but the"
info "  image does not ship the userspace library. Installing it makes"
info "  PiNozCam roughly 6x faster."
info ""
info "  The library comes from Rockchip:"
info "    https://github.com/airockchip/rknn-toolkit2"
info "    rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"
info ""

if [ "${NONINTERACTIVE}" = "1" ]; then
    warn "non-interactive mode: not downloading. PiNozCam will use the CPU."
    exit 0
fi

read -r -p "  Download it from Rockchip's repository and install it? [y/N] " REPLY
case "${REPLY}" in
    [yY]*) ;;
    *) warn "skipped. PiNozCam will use the CPU."; exit 0 ;;
esac

URL="https://raw.githubusercontent.com/airockchip/rknn-toolkit2/master/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"
TMP=$(mktemp -d); trap 'rm -rf "${TMP}"' EXIT

info "  downloading..."
curl -fsSL --retry 3 -o "${TMP}/librknnrt.so" "${URL}" \
    || { warn "download failed. Install it by hand; PiNozCam works on the CPU meanwhile."; exit 0; }

# Verify we got a library, not an HTML error page saved with a .so name.
if ! head -c4 "${TMP}/librknnrt.so" | grep -q $'\x7fELF'; then
    warn "downloaded file is not an ELF library -- refusing to install it."
    exit 0
fi
GOT=$(strings "${TMP}/librknnrt.so" | grep -m1 'librknnrt version' || echo "?")

sudo install -m 0644 -o root -g root "${TMP}/librknnrt.so" /usr/lib/librknnrt.so
command -v ldconfig >/dev/null && sudo ldconfig || sudo /usr/sbin/ldconfig
ok "installed /usr/lib/librknnrt.so (${GOT#librknnrt version: })"

if [ -n "${DRIVER_VER}" ]; then
    info ""
    info "  Note: library and kernel driver must be compatible. Driver here is"
    info "  ${DRIVER_VER}. If inference misbehaves, install the librknnrt.so"
    info "  matching your board's BSP instead of the latest one."
fi
