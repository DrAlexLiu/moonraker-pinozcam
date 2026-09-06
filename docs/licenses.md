# Licences and what ships where

[← Back to README](../README.md)

PiNozCam for Moonraker is **AGPL-3.0** ([LICENSE](../LICENSE)), the same as
the OctoPrint build it is ported from.

What you install is not all from this repository, so this page says where
each piece comes from and where its licence text is.

## What `install.sh` puts on your printer

| piece | where it comes from | licence |
|---|---|---|
| this service (`moonraker_pinozcam/`) | this repository | AGPL-3.0 |
| the inference runtime — daemon binaries + the model | a **wheel** built by this repository's own CI from `src/`, downloaded from its GitHub Releases and matched to your board | see below |
| Python dependencies | PyPI (`requirements.txt`) | their own |

The runtime wheel is the same artifact OctoPrint users install. Both
products run the identical daemon and the identical model; this port only
replaces the host around them.

## The runtime wheel carries its own licence texts

Verified on a board — after installation they are inside the installed
package, next to the binaries they cover:

```
<venv>/lib/python3.*/site-packages/pinozcam_runtime_<target>/
├── PiNozCam.LICENSE
├── THIRD_PARTY_LICENSES/
│   ├── ExecuTorch.LICENSE      XNNPACK.LICENSE       cpuinfo.LICENSE
│   ├── flatbuffers.LICENSE     FP16.LICENSE          FXdiv.LICENSE
│   ├── gflags.LICENSE          pthreadpool.LICENSE   volk.LICENSE
│   └── Vulkan-Headers.LICENSE  VulkanMemoryAllocator.LICENSE
├── bin/     the daemon(s) for your board
└── models/  the model for your board
```

This repository deliberately does **not** duplicate them: a licence belongs
next to the binary it covers, and that binary is delivered by the wheel.

## ⚠️ Statically linked glibc, and the LGPL

The CPU daemon (`nozcam_daemon.*.static`) is **statically linked against
glibc**, which is LGPL-2.1. That is not an oversight — it is why the daemon
runs at all on the older distributions Klipper hosts often carry. A
dynamically linked build cannot be made to work here: a trivial C++ program
built by these toolchains already requires `GLIBC_2.34` (2.34 merged
libpthread into libc), while OctoPi 1.0.0's base is 2.31.

LGPL-2.1 §6 therefore applies, and it is satisfied by **§6(a): the complete
source of the covered work is published**, in the OctoPrint build's public
repository:

- `src/executorch/nozcam_daemon.cpp` — the daemon
- `src/common/nozcam_postprocess.{cpp,h}` — decode, NMS and severity
- `src/` also holds the Rockchip, Allwinner and D-Robotics daemons

<https://github.com/DrAlexLiu/OctoPrint-PiNozCam/tree/master/src>

Both shipped ARM binaries have been verified to rebuild **byte-identically**
from that source, so the published source is demonstrably the source of the
shipped binary.

⚠️ The wheel's `THIRD_PARTY_LICENSES/` does **not** currently include a copy
of the LGPL-2.1 text itself; the OctoPrint build's repository does, at
`THIRD_PARTY_LICENSES/glibc.LGPL-2.1`. If you are redistributing PiNozCam
rather than merely running it, carry that file too.

## The model

The detector's weights ship inside the runtime wheel under the same
`PiNozCam.LICENSE`. Nothing in the shipped model file names an architecture,
a layer or an operator — the graph lives inside opaque delegate blobs — so
the file is not a description of the model, only a way to run it.

## Code shared with the OctoPrint build

Both projects are AGPL-3.0 and have the same author, so this is one work in
two shapes rather than a borrowing between projects. The files below are
**byte-for-byte identical** to their counterparts in
[OctoPrint-PiNozCam](https://github.com/DrAlexLiu/OctoPrint-PiNozCam);
`tools/check_upstream_sync.py` fetches the originals and fails if any of them
has drifted.

⚠️ They deliberately carry **no per-file licence header**. Adding one would
end the byte-identity that makes the check meaningful, and a copy that can
silently diverge from the protocol it implements is a worse outcome than a
missing header -- writing one of these from a description, rather than
copying it, once produced three defects at once. The attribution lives here
instead, naming the files exactly.

| file | what it is |
|---|---|
| `moonraker_pinozcam/telegram_bot.py` | Telegram transport, buttons, confirmation nonces |
| `moonraker_pinozcam/discord_bot.py` | Discord Gateway client, buttons, interaction routing |
| `moonraker_pinozcam/confirm.py` | the confirm/cancel nonce store both bots use |
| `moonraker_pinozcam/credentials.py` | credential validation and redaction |
| `moonraker_pinozcam/framesource.py` | camera source abstraction |
| `moonraker_pinozcam/mjpegstream.py` | MJPEG multipart reader |
| `moonraker_pinozcam/framestore.py` | the frame the page and the bots serve |
| `moonraker_pinozcam/framebuffer.py` | sharpest-of-N candidate selection |
| `moonraker_pinozcam/cpu_affinity.py` | core limiting, so the detector yields to gcode streaming |

`moonraker_pinozcam/nozcam_backend.py` is shared too, with **18 documented
differing lines** — the checker knows the exact count and fails if it moves.

Six methods of `moonraker_pinozcam/notify.py` are copied verbatim as well,
and checked at function granularity rather than whole-file, because the rest
of that module is written against Moonraker: `handle_telegram_command`,
`handle_discord_command`, `_action_state_problem`, `check_reply`,
`telegram_send_with_reply`, `get_printer_status`.

The inference runtime's own sources, under `src/`, started as a copy of the
same project's build tree. From the move into this repository the two evolve
independently and are **not** drift-checked.

## Icons on the built-in page

| icon | source | licence |
|---|---|---|
| GitHub and Discord brand links | copied verbatim from the OctoPrint build's own tab template | AGPL-3.0, same project |
| eye-off, wrench, Telegram, Discord glyphs | Font Awesome Free 7.3.1 | CC-BY-4.0 |
| the camera icon on the Live Camera button | Font Awesome Free | CC-BY-4.0 |

Brand marks remain the property of their owners and are used only to refer
to those services.
