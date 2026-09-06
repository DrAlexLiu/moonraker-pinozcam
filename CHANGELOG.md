# Changelog

Notable changes to PiNozCam for Moonraker. Versions track the shared
inference runtime, so `1.1.0` here uses runtime `1.1.0`.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Runtime Wheels are now built by **this** repository's own GitHub Actions,
  from the sources in `src/`, and published as Release assets here. Nothing
  is published to PyPI: every target resolves to a Release URL, so there is
  no name-based lookup that could resolve to another project.
- `/camera` serves the printer's camera stream through this service. The
  page prefers the camera's own address, which costs this service nothing,
  and falls back to `/camera` where a browser cannot reach that address --
  behind a proxy that publishes only this port, through a tunnel, or on a
  Klipper host with no nginx. Capped at two viewers.
- Discord buttons are re-issued automatically when the printer id changes,
  because a rename otherwise disables remote control silently.
- CI: the tests, an import check on Python 3.9 and 3.13, and a parse check
  on the settings page's JavaScript.

### Changed

- The page shows the camera's **live stream** by default, and interrupts it
  for three seconds with the annotated frame when a check comes back
  alarming -- the OctoPrint build's behaviour. It previously opened on a
  still and hid the stream behind a button.
- Credential fields say whether one is stored (`configured` / `will be
  saved` / `not set`). The value itself is still never sent to the page:
  this service has no login and binds `0.0.0.0`.

### Fixed

- **Every Discord button on an alert was dead.** `alert()` tagged them with
  the display name while the bot answered to the instance id. A mismatched
  button is dropped without an acknowledgement and logs only at DEBUG, so a
  press did nothing and left no trace, while Telegram kept working.
- The camera is resolved at startup rather than on the first snapshot, so
  the first status poll already knows how to show live video.

### Security

- `docs/notifications.md` used a real Discord channel id as its example.
  Replaced with a placeholder, and the history was rewritten to remove it.

## [1.1.0] - 2026-09-05

First working build on real hardware: BIQU CB2 (RK3566 NPU, 257 ms/frame),
with Telegram and Discord verified end to end against live bots.

### Added

- Detection loop, alerting, and pause/stop, driven by Moonraker's WebSocket.
- Telegram and Discord bots, sharing their transport modules byte-for-byte
  with the OctoPrint build so one protocol has one implementation.
- The annotated view on port 58888: printer tab, settings, undetect zone
  editor, and a first-run welcome page.
- Registration of that view as a Moonraker webcam, so Mainsail and Fluidd
  list it.
- `install.sh` / `uninstall.sh`, and Moonraker `update_manager` support.

[Unreleased]: https://github.com/DrAlexLiu/moonraker-pinozcam/compare/1.1.0...HEAD
[1.1.0]: https://github.com/DrAlexLiu/moonraker-pinozcam/releases/tag/1.1.0
