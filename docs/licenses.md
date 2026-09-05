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
| the inference runtime — daemon binaries + the model | a **wheel** downloaded from the OctoPrint build's GitHub Releases, matched to your board | see below |
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

## Icons on the built-in page

| icon | source | licence |
|---|---|---|
| GitHub and Discord brand links | copied verbatim from the OctoPrint build's own tab template | AGPL-3.0, same project |
| eye-off, wrench, Telegram, Discord glyphs | Font Awesome Free 7.3.1 | CC-BY-4.0 |
| the camera icon on the Live Camera button | Font Awesome Free | CC-BY-4.0 |

Brand marks remain the property of their owners and are used only to refer
to those services.
