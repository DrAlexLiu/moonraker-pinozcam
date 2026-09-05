# PiNozCam architecture, and what a Moonraker port inherits

Written by reading the OctoPrint plugin's source, not from memory. Every
class, constant and file reference below was verified against
`octoprint_pinozcam/` at the 1.1.0 release.

Its purpose is to separate **what PiNozCam is** from **what OctoPrint
happens to provide**, so the Moonraker port reimplements only the second
part.

---

## 1. The five layers

```
   ┌─ frame source ────────────────────────────────────────────┐
   │  MJPEG stream (push) │ HTTP snapshot (poll) │ static file │
   └──────────────────────┬────────────────────────────────────┘
                          │  wait_next(last_seq, stop, timeout)
   ┌─ detection loop ─────▼────────────────────────────────────┐
   │  sample -> letterbox -> infer -> severity -> sliding window│
   └──────────────────────┬────────────────────────────────────┘
                          │  "this print is failing"
   ┌─ decision ───────────▼────────────────────────────────────┐
   │  duty-cycle ratio over count_time, then pause/stop/notify  │
   └──────────────────────┬────────────────────────────────────┘
        ┌─────────────────┴─────────────────┐
   ┌────▼─────────┐                  ┌──────▼──────────────────┐
   │ printer      │                  │ notification            │
   │ control      │                  │ Telegram / Discord      │
   └──────────────┘                  └─────────────────────────┘
```

Only the **printer control** box is genuinely OctoPrint-specific. The frame
source is plain HTTP, and inference is a subprocess speaking a private
protocol on stdin/stdout.

---

## 2. Frame acquisition — two modes, one interface

This is the part most likely to be got wrong in a port, because the
"obvious" design (poll a snapshot URL) is only half of what PiNozCam does.

### 2.1 Three sources, one abstract base

`framesource.py` defines `FrameSource` with exactly four methods:

| method | contract |
|---|---|
| `start()` | begin producing; may spawn a reader thread |
| `wait_next(last_sequence, stop, timeout)` | block until a frame **newer than `last_sequence`** exists, or stop/timeout |
| `close()` | stop within a bounded time |
| `stats()` | counters for the UI |

Three implementations:

| class | transport | how frames arrive |
|---|---|---|
| `MjpegFrameSource` | `multipart/x-mixed-replace` | **PUSH** — a reader thread parses parts as the server sends them |
| `HttpSnapshotFrameSource` | `image/*` | **POLL** — a thread issues one GET at a time |
| `StaticFileFrameSource` | `file://` | reads a fixed file (development/test only) |

**The consumer cannot tell them apart.** The detection loop only ever calls
`wait_next()`. That single decision is what lets a push transport and a poll
transport share one loop, and the port must preserve it.

### 2.2 How the mode is chosen — by Content-Type, not by URL text

`camera.py:_classify_custom_url()` performs a real GET and reads the header:

```python
if media_type == "multipart/x-mixed-replace":  return "mjpeg"
if media_type.startswith("image/"):            return "snapshot"
raise ValueError("unrecognised Content-Type ...")
```

⚠️ **Never guess from the URL string.** `?action=stream` vs `?action=snapshot`
is a ustreamer convention, not a standard; other servers use neither.

### 2.3 Where the URL comes from

`camera.py:_new_source()`:

```python
custom_url = self._plugin.custom_snapshot_url
if custom_url:
    return self._from_custom_url(custom_url)   # user override wins
return self._from_provider()                    # ask OctoPrint
```

`_from_provider()` calls `octoprint.webcams.get_snapshot_webcam()` — **the
one true OctoPrint dependency in the whole camera path.**

**→ Moonraker equivalent:** `GET /server/webcams/list` returns the webcams
the user configured in Mainsail/Fluidd, each with `snapshot_url`,
`stream_url` and the rotation/flip settings. The port should prefer it and
treat the config file's `snapshot_url` as the override, mirroring the
structure above exactly. Moonraker's `webcam` component is present on a
stock install (verified on a BTT CB2).

### 2.4 Two rates, and they are not the same number

| constant | value | meaning |
|---|---|---|
| `framesource.HTTP_SNAPSHOT_MIN_INTERVAL` | **0.1 s** | floor between snapshot GETs; a slow request slows the rate rather than causing a catch-up burst |
| `__init__.py: frame_sample_interval` | **0.2 s** | how often the *detection loop* takes a frame |

The source may produce faster than the loop consumes; `wait_next()`'s
`last_sequence` argument is what makes the loop skip frames it missed
instead of falling behind.

### 2.5 Robustness details worth copying verbatim

- **Capacity-one slot.** Only the newest frame is kept. A slow detector
  never builds a backlog of stale frames.
- **Bounded reads.** `_read_bounded()` caps bytes *and* wall-clock, because
  per-read timeouts do not bound a response that dribbles bytes forever.
- **A snapshot URL that is really a stream** is detected and reported, not
  left to hang (`framesource.py:70-75`).
- **Reconnect with backoff** on stream end: 0.1 s doubling to 5 s.
- **Stale clearing.** A failed poll clears the slot rather than letting the
  detector keep scoring a frame from minutes ago.

---

## 3. Inference — already portable, do not rewrite

`nozcam_backend.py` (52 KB) contains **zero OctoPrint imports**. It:

1. picks a runner binary + model for this machine (`_resolve_backend()`),
2. spawns it as a subprocess,
3. speaks a fixed binary protocol over stdin/stdout.

The protocol is frozen and documented in the workspace's
`RUNNER_CONTRACT.md`: little-endian `[4B cmd][4B len][payload]`, with
`INFER` carrying `[4B req_id][16B content rect][HWC uint8]` = 737,300 bytes
for a 640x384 frame.

**→ The port reuses this file as-is.** Same runtime wheels, same daemons,
same models, same accuracy. That is the single biggest reason a port is
weeks of work and not months.

⚠️ The backend selection reads *hardware capability*, never a hostname or a
product name — device-tree compatible strings, device nodes, and whether a
vendor runtime `.so` is actually installed. Keep that discipline: see
`scripts/setup_accelerator.sh` for the `librknnrt.so` case, where the kernel
driver being present says nothing about the userspace library being present.

---

## 4. Geometry — letterbox, and the trap in it

`NozcamBackend._fit()` scales to fit and pads with black (never stretches),
turns a portrait frame a quarter turn first, and passes a **content rect**
`(x, y, w, h)` to the daemon so padding cannot dilute the severity fraction.
`_unfit()` maps boxes back to the original image.

⚠️ Three facts that cost real time to learn:

- **Which side gets bars depends on 640:384 = 5:3**, not on "the long side".
  16:9 fits the width (640x360, 6.2% bars); 4:3 fits the height (512x384,
  20% bars).
- **The quarter-turn direction is a convention, not a derivation.** It
  matches OctoPrint's own rotate90. Getting it backwards is **silent** to
  geometry tests — box round-trips still pass, only the pixels differ —
  and costs 5/34 alarms, because the detector is not rotation invariant.
- **Resize must stay PIL BICUBIC.** `cv2.INTER_CUBIC` measures |dscore|
  0.0390 against PIL's 0.0000 — larger than the entire int8 quantization
  error the project spent a day minimising. PIL antialiases on downscale;
  OpenCV does not, and their bicubic coefficients differ (-0.5 vs -0.75).

---

## 5. Decision — a ratio, not a count

Severity per frame = affected area / `sensitivity`, clamped to [0,1]; a frame
"alarms" at severity >= 0.5 (area >= 2% of the content rect at the default
0.04 sensitivity).

The print is failing when **alarming frames / total frames** over the
`count_time` window exceeds `failure_ratio` (default 0.30).

⚠️ **It must be a ratio, not a count.** A count's scale tracks hardware
speed: a 300 s window holds 61 frames on a Pi Zero 2 W and 1107 on a Pi 5,
an 18x difference in false-positive exposure. A ratio is the sample mean and
is speed-invariant by construction. Needs a **>= 20 s span floor** so 1-of-1
cannot fire.

---

## 6. Printer control — the one part that is genuinely OctoPrint

| PiNozCam does | OctoPrint API | **Moonraker equivalent** |
|---|---|---|
| pause | `self._printer.pause_print()` | `POST /printer/print/pause` |
| resume | `self._printer.resume_print()` | `POST /printer/print/resume` |
| cancel | `self._printer.cancel_print()` | `POST /printer/print/cancel` |
| is it printing? | `self._printer.is_printing()` | `print_stats.state` == `printing` |
| job / file name | printer state | `print_stats.filename` |
| printer name | `appearance.name` | `machine` / config, or user-set |

Moonraker also offers a **WebSocket subscription**
(`printer.objects.subscribe`) so state changes arrive as events instead of
being polled — strictly better than what OctoPrint offers here. Objects
verified present on a stock install: `print_stats`, `virtual_sdcard`,
`pause_resume`, `display_status`, `idle_timeout`.

---

## 7. Notifications — portable, with guardrails worth keeping

`telegram_bot.py` and `discord_bot.py` have no OctoPrint imports; only
`notify.py` (which calls printer control and reads `appearance.name`) does.

Both bots run **`threading` + blocking I/O**, never asyncio, because the
plugin lives inside OctoPrint's synchronous process. ⚠️ A Moonraker port is
its own process and *could* use asyncio — but keeping the existing threading
model means the bot files port unchanged, which is worth more than
architectural purity.

Guardrails in the confirm/cancel flow that must survive the port:

- **nonce match** — rejects a reused button from an older message,
- **expiry** — "that request expired, press it again",
- **state re-validation between offering and confirming** — the print may
  have ended while the button sat unanswered.

⚠️ Telegram's `getUpdates` is **exclusive per token**: two instances polling
one token fight, and each button press reaches whichever won. The plugin
detects HTTP 409 and says so explicitly rather than retrying forever. A port
must keep that, because multi-printer users hit it immediately.

---

## 8. What the port must write, and what it must not

| | |
|---|---|
| **Reuse unchanged** | `nozcam_backend.py`, `telegram_bot.py`, `discord_bot.py`, `framesource.py`, `mjpegstream.py`, `framestore.py`, `framebuffer.py`, `mask.py`, `cpu_affinity.py`, `credentials.py`, `confirm.py` |
| **Rewrite** | plugin entry point, settings (OctoPrint dialog -> INI file), printer control (OctoPrint API -> Moonraker HTTP/WS), camera discovery (`octoprint.webcams` -> `/server/webcams/list`), the OctoPrint-specific half of `notify.py` |
| **Drop** | the Jinja/Knockout UI, OctoPrint blueprints and API endpoints, the settings schema |
| **Add** | `install.sh`, systemd unit, `update_manager` registration, INI config parsing |

**The detection loop itself sits in between**: its logic is portable, but it
is currently a mixin on the OctoPrint plugin object and reads
`self._plugin.*` throughout, so it needs mechanical extraction rather than a
rewrite.

---

## 9. Non-obvious constraints that apply to both versions

- **Memory is the binding constraint, not speed.** The C++ daemon's `VmHWM`
  is **146 MB and constant across every board** (input is always resized to
  640x384, so the memory plan is fixed). That is what rules out a
  128 MB-RAM printer host outright, and makes 512 MB tight once Klipper,
  Moonraker, a frontend and a camera streamer are also resident.
- **`nice` and CPU affinity do different jobs.** Niceness governs the worst
  case (10.66 ms -> 5.44 ms of scheduling delay); affinity governs typical
  jitter (2.28 ms -> 0.13 ms p99, a 17x difference). Both are needed, and a
  synthetic CPU-burn load does **not** reproduce the effect — the real
  detector is memory-bandwidth heavy, which is exactly what gcode streaming
  also needs.
- **Never use `platform.machine()`** to choose a binary: it reports the
  *kernel* arch and returns `aarch64` on a 64-bit-kernel/32-bit-userspace
  board. Use `struct.calcsize("P") * 8`.
