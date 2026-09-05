# Images

Two kinds live here. Sizes matter: this repository is cloned onto printer
hosts, so keep each file under ~400 KB and no wider than ~1600 px unless
there is a reason.

## Shared with the OctoPrint build — do not re-shoot

These show hardware or the detector's own output, both of which are
identical in the two products (same model, same boxes burned into the same
JPEG). Copied verbatim from `DrAlexLiu/OctoPrint-PiNozCam`.

| file | what it shows | used by |
|---|---|---|
| `nozzle_cam_setup.jpg` | a nozzle camera mounted close in | `docs/camera.md` |
| `overview_camera_setup.jpg` | an overview/chamber camera | `docs/camera.md` |
| `failure_detection1.jpg` | spaghetti caught by a nozzle camera | `README.md` |
| `failure_detection_side.jpeg` | the same from an overview camera | `README.md` |
| `failure_detection2.jpg` | a second nozzle-camera example | spare |
| `telegram_remote_control.jpg` | the Telegram alert and its buttons | `README.md` |
| `discord_notification.jpg` | the Discord alert and its buttons | `README.md` |

These two were downscaled on the way in — upstream carries them at camera
resolution (2639×2214 and 3024×4032, 904 KB and 1.2 MB), which is far more
than a 420 px-wide table cell needs on a machine that also has to run a
printer. They are therefore **not** byte-identical to upstream's copies;
everything else here is.

## Still needed — this build's own interface

The OctoPrint build's remaining screenshots are of **its** tab and settings
pages, which do not exist here, so they were deliberately not copied. These
have to be taken from the page this service serves.

Open `http://<printer>:58888` in a browser and capture:

| file to create | what it must show | how to get there |
|---|---|---|
| `page_main.jpg` | the whole page during a print: camera view, Status panel with its two buttons, the chip row (AI running / Telegram / Discord / backend), and the failure-ratio gauge with its threshold mark | start a print and wait for detection to arm |
| `page_settings.jpg` | the settings dialog on **Detection**, showing the five-step Sensitivity slider and a couple of the `?` help texts expanded | press the wrench |
| `page_zone.jpg` | the undetect-zone editor with a real mask painted over something fixed — a bed clip or the parked toolhead — and the toolbar visible | press the eye-with-a-slash |
| `page_welcome.jpg` | the first-run page | clear `pinozcam-welcomed` from the browser's local storage and reload |
| `mainsail_camera.jpg` | **PiNozCam appearing in Mainsail's own camera list**, next to the ordinary camera | Mainsail → the webcam selector |

That last one is worth taking care over: it is the thing this port does that
the OctoPrint build has no equivalent of, and it is what shows a Klipper
user that PiNozCam lands inside the UI they already use rather than beside
it.

Take them at a browser width around 1200-1400 px so the two-column layout
is visible rather than the stacked mobile one. A dark-theme browser matches
the page, which is drawn in Vuetify's dark palette to sit beside Mainsail.

⚠️ Do not photograph a page that has real bot credentials on screen. The
settings page shows a saved token as eight dots, never the value, so the
Monitoring tab is safe — but check the chip row and any log output in the
same frame.
