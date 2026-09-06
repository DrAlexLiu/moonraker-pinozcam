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

## No interface screenshots, by decision

The OctoPrint build's remaining images are of **its** tab and settings
pages, which do not exist here, so they were deliberately not copied and
nothing was substituted for them. Shots of this build's own page were
considered and **decided against**: the images above already carry what a
reader needs to judge the product -- the hardware, a real failure the
detector caught, and the alerts as they arrive -- and the README and the
guides describe the page in words. This is settled; do not re-open it as
an outstanding task.

⚠️ If that is ever revisited, do not photograph a page with real
credentials in frame. The settings page shows a saved token as eight dots
and never the value, so that tab is safe by itself; the surrounding window
may not be.
