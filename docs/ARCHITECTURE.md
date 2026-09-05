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

---

## 10. Measured on a BIQU CB2, 2026-09-05 — where the time actually goes

Camera is a UVC 4K module (`0edc:3080`, "DH Camera") on USB 2.0, feeding
crowsnest/ustreamer with `--format MJPEG --encoder HW`.

### The frame pipeline costs more CPU than the NPU costs

At 1920x1080, median of 5, measured on the board:

| stage | ms | who does the work |
|---|---:|---|
| HTTP fetch of one snapshot | **101.7** | waiting for the camera's next frame |
| PIL JPEG decode | **42.8** | CPU |
| convert("RGB") + BICUBIC resize to 640x360 | **85.7** | CPU |
| **frame total** | **230.2** | |
| NPU inference | 233 | NPU |

⚠️ **The resize is the single most expensive step, not the decode.** It also
cannot be swapped for something faster: OpenCV's `INTER_CUBIC` measures
|dscore| 0.0390 against PIL BICUBIC's 0.0000 -- larger than the entire int8
quantization error. That 85 ms is the price of matching what the model was
calibrated on.

⚠️ **ustreamer itself costs almost nothing.** The camera emits MJPEG and
ustreamer forwards it, so the 101.7 ms is dominated by waiting for the next
frame, not by processing.

### Resolution changes the CPU cost and nothing else

| camera mode | model_ms | fetch_ms | total_ms |
|---|---:|---:|---:|
| 640x480 | 230 | 261-348 | 522-608 |
| **1920x1080** | **233** | 274-311 | **607-638** |
| 3840x2160 (4K) | 233 | 336-367 | **896-916** |

**Inference is identical at all three** because every frame is scaled to the
model's 640x384 regardless. 4K therefore costs ~300 ms per frame to decode
and discard pixels the model never sees. **1080p is the sweet spot**: same
accuracy as 4K, 300 ms cheaper, and visibly better than 640x480 for a human
watching in Mainsail.

### Camera facts worth not rediscovering

- **Auto-exposure costs 3.4x the frame rate.** Measured at 1080p:
  auto 7.66 fps, manual exposure 25.84 fps. In dim light the sensor extends
  exposure time to gain brightness. **Leave it on anyway** -- detection needs
  ~4 fps and brightness matters more than frames PiNozCam would discard.
- **The descriptor's 60 fps is not real**: 25.8 fps was the measured best,
  and only with auto-exposure disabled.
- **No 4K@60**: the UVC descriptor offers 4K@30 *or* 1080p@60, never both.
- UVC does not expose the CMOS sensor model; only the USB identity
  (`0edc:3080`, `DH-220902-ZW`) is visible. Reading it requires opening the
  module or asking the vendor.

### ⏳ Idea, not implemented: ask ustreamer for two resolutions

crowsnest can run more than one stream. Serving 1080p for humans and
640x480 for the detector would cut the decode+resize cost (~128 ms) to
nearly nothing, since 640x480 needs almost no scaling to reach 640x384.
**Not measured, and it is unknown whether feeding the model a 640x480
source changes accuracy** -- the letterbox geometry differs from 1080p's.
Revisit after the core loop is finished.

---

## 11. Does the detector disturb Klipper? Measured 2026-09-05 — no

The OctoPrint plugin ships a conservative "25% of cores" default to protect
gcode streaming (issue #11: starved streaming leaves marks on the print).
**That default exists because of an assumption Klipper does not share.**

### Why Klipper is structurally safer

`klipper/klippy/toolhead.py`:

```python
BUFFER_TIME_HIGH  = 1.0      # seconds
BUFFER_TIME_START = 0.250
```

Klipper computes a step schedule on the host and pushes **about a second of
it** into the MCU ahead of time; the MCU then executes from that queue.
OctoPrint+Marlin streams gcode line by line in real time, so a 10 ms write
delay is 10 ms of extra nozzle dwell and a visible artifact. Klipper absorbs
host stalls of hundreds of milliseconds without the MCU noticing.

⚠️ **The buffer protects motion already computed, not the computing of it.**
klippy must keep refilling that queue, and its motion planning lives in the
main Python thread (GIL-bound; `serialq`/`serialhdl` are light C helpers).
Starve klippy of CPU *continuously* and the queue drains to
`Timer too close`, which **aborts the print** -- not a soft degradation.
So "there is a 1 s buffer" is not a licence to occupy every core forever.

### Measured, on a CB2 with a real 1080p camera

Metric is Klipper's own `mcu.last_stats`, sampled every 0.5 s for 20 s per
configuration, with a continuous inference load running:

| config | stddev median | max | retransmit | affinity (verified) | inferences/s |
|---|---:|---:|---:|---|---:|
| idle baseline | 22.0 us | 37.0 | 0 | — | — |
| **NPU** | **16.0 us** | 25.0 | 0 | 0-3 | **3.00** |
| CPU 1 core | 11.0 us | 13.0 | 0 | 0 | 0.35 |
| CPU 2 cores | 12.0 us | 13.0 | 0 | 0-1 | 0.61 |
| CPU 3 cores | 17.0 us | 18.0 | 0 | 0-2 | 0.65 |
| CPU 4 cores | 16.0 us | 24.0 | 0 | 0-3 | 0.70 |

**Every configuration sits at or below the idle baseline.** Some loaded runs
even measure *lower* jitter than idle, which is the giveaway: **22 us is
measurement noise, not signal**. Against a 1 s buffer -- 1,000,000 us --
these numbers are four orders of magnitude away from mattering.

**→ On NPU, use every core.** The heavy work is on the NPU; the CPU only
fetches and scales, and yields readily.

**→ On CPU, use N-1 anyway, because it costs almost nothing:**

```
1 core 0.35  →  2 cores 0.61 (+74%)  →  3 cores 0.65 (+7%)  →  4 cores 0.70 (+8%)
```

Doubling from 2 to 4 cores buys 15%. That is the A53/A55 signature recorded
elsewhere in this project: in-order cores are **memory-latency bound**, so
extra cores mostly wait on memory together. Giving the 4th core back to
klippy costs 7% throughput and is worth it.

⚠️ **CPU-only is perfectly usable, contrary to an earlier claim in this
session.** 0.70/s means a frame every 1.4 s; print monitoring glances every
few seconds. This project ships boards at 7.7 s/frame (Pi 3B+) and calls
that adequate.

### ⚠️ What this measurement CANNOT tell you

The board runs a **host MCU** (`serial: /tmp/klipper_host_mcu`, a unix
socket). **There is no physical serial link**, so the very thing that would
drop packets under host stall does not exist here, and `retransmit: 0` is a
tautology rather than a result. **Re-run this against a real MCU** (the
planned Prusa Mini Buddy board over USB serial) before trusting it for a
machine that actually moves.

### ⚠️ Three ways this measurement silently produced fake data first

All three passed as plausible output before being caught:

1. **`logger=None`** — `NozcamBackend` calls `self._logger.error()`, so every
   load thread died instantly. The jitter table looked perfectly normal and
   would have "proven" the detector has zero impact. Caught only because the
   script also printed inferences/second (0.0).
2. **`cpus="0,1"` as a string** — the daemon builds argv with
   `",".join(str(c) for c in sorted(cpus))`, so a *string* is iterated
   character by character into `,,0,1`. The daemon logged one WARNING and
   **carried on using all four cores**, making every "core count" row
   identical without saying so.
3. **The guard itself** — the load counter was written only when the thread
   *returned*, which it never did during measurement, so the
   "is the load alive?" check always saw zero. This one failed safely
   (reported "void") rather than inventing numbers.

**→ A performance test must verify that its experimental condition actually
took effect**, not merely that its numbers look reasonable. This script now
reads back `Cpus_allowed_list` from `/proc/<pid>/status` and requires a
non-zero inference counter before it will report anything.

### Can a virtual USB port stand in for the real one? Partly — measured

The host MCU already **is** a virtual serial port: `/tmp/klipper_host_mcu`
is a symlink to `/dev/pts/1`, and klippy holds it as a normal tty fd. So
Klipper's serial stack — tty reads and writes, CRC, sequence tracking,
retransmit logic — is exercised for real. What a PTY cannot reproduce is the
**physical** failure mode:

```
host CPU busy -> USB interrupt handling delayed -> CDC-ACM driver's
receive buffer overruns -> bytes lost -> CRC fails -> retransmit
     ^                                    ^
  PTY has this                    PTY does not have this
```

A PTY's buffer lives in kernel memory; when it fills, the writer blocks
rather than dropping. So `retransmit: 0` on a host MCU is a property of the
transport, not evidence about a real board. Injecting artificial loss would
also answer the wrong question — how Klipper *copes* with loss, not whether
the detector *causes* it.

**But the first link of that chain is shared, and it can be measured.**
`/proc/<pid>/task/<tid>/schedstat` field 2 is the nanoseconds a thread spent
runnable-but-waiting — exactly the delay that would postpone USB interrupt
handling. Over 15 s per configuration:

| load | `serialhdl mcu` waited | `serialq mcu` waited |
|---|---:|---:|
| idle | 0.0 ms | 0.1 ms |
| **NPU** | **0.0 ms** | **0.0 ms** |
| CPU 3 cores | 0.3 ms | 0.2 ms |
| **CPU 4 cores (saturated)** | **0.5 ms (0.004%)** | 0.4 ms |

**Saturated, Klipper's serial threads are kept off the CPU for 0.5 ms out of
15 s.** They get to run 99.996% of the time they want to. On NPU the figure
is indistinguishable from idle.

This is stronger evidence than the `mcu_task_stddev` table above, which
carried enough noise to show *lower* jitter under load. `schedstat` is a
kernel counter of a specific thread's queueing, with no such ambiguity.

⚠️ **Those numbers were measured under a worse condition than intended.**

**Known platform behaviour, not a bug in this project: an RKNN daemon always
ends up at `nice -19`, whatever it asks for.** User-confirmed on rk3588 as
well as rk3566. The daemon passes `nice 10` (yield) in argv and calls
`setpriority()` with it; the CPU daemon, running the identical code path,
reports `nice=10` correctly. Only the RKNN backend is overridden.

It does not come through the normal route: `nm --undefined-only -D
librknnrt.so` finds **zero** references to `setpriority`, `sched_set*` or
`pthread_setschedparam`, so the library is not calling libc for it -- either
it issues the syscall directly, or the rknpu kernel driver raises the
priority when the process opens the NPU device. The library does carry
`GPUPriorityLevel::PRIORITY_{HIGH,LOW,NORMAL}` strings. **Do not spend time
trying to make `nice` stick on an RKNN board; it will not.**

The measurement is stronger for it, not weaker: even with the detector
preempting everything at the highest priority the scheduler offers,
Klipper's serial threads waited 0.5 ms out of 15 s.

⚠️ Still not a substitute for a real board. It shows the detector does not
push the serial threads aside; it says nothing about whether a Buddy board's
USB controller drops bytes under that particular delay. ⚠️ And it was
measured **while not printing** — klippy's motion planning is idle, so the
contention picture during a real job differs. Re-run `sched_delay.py`
against the Prusa Mini during an actual print.
