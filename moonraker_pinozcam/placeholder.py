"""The "no signal" image, drawn rather than shipped as a file.

Ported from the OctoPrint build, including the reason it looks like this:
it deliberately matches ustreamer's own --blank default. The two mean
different things -- ustreamer's fires when the camera DEVICE is offline,
this one when we could not fetch a frame (a misconfigured URL, a network
hiccup, crowsnest restarting) and the camera may be perfectly fine -- but
there is no reason for two failure layers to look unrelated to whoever is
staring at the view.

Drawn, not loaded: a packaged JPEG can go missing, forces a file-not-found
branch at every call site, and its aspect ratio would not match anything
else this produces. All geometry is a fraction of the shorter side, so it
is correct at any size and any aspect ratio.
"""

import io
import os

from PIL import Image, ImageDraw, ImageFont

SYSTEM_FONTS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
)

# Draw at 2x and downsample: the built-in bitmap font has no antialiasing,
# and the text is the only thing on the image.
SUPERSAMPLE = 2
TEXT = "NO SIGNAL"
GRAY = (176, 176, 176)

_cache = {}


def load_font(size):
    """A scalable font if the system has one, else Pillow's built-in."""
    for path in SYSTEM_FONTS:
        if not os.path.exists(path):
            continue
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    try:
        return ImageFont.load_default(size=size)   # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def no_signal(size=(640, 480)):
    """Return a PIL image reading NO SIGNAL, centred."""
    scale = SUPERSAMPLE
    width, height = size[0] * scale, size[1] * scale
    side = min(width, height)
    image = Image.new("RGB", (width, height), (0, 0, 0))
    draw = ImageDraw.Draw(image)

    font = load_font(int(round(side * 0.09)))
    # Centre on the glyphs' own bounding box, not on the nominal text box:
    # ascender and descender padding differ between fonts and would push
    # the text visibly off-centre.
    bbox = font.getmask(TEXT).getbbox()
    if bbox is None:
        x, y = width / 2.0, height / 2.0
    else:
        x = (width - (bbox[2] - bbox[0])) / 2.0 - bbox[0]
        y = (height - (bbox[3] - bbox[1])) / 2.0 - bbox[1]
    draw.text((x, y), TEXT, fill=GRAY, font=font)
    return image.resize(size, Image.LANCZOS)


def no_signal_jpeg(size=(640, 480)):
    """The same image as JPEG bytes, cached -- it never changes."""
    key = tuple(size)
    if key not in _cache:
        buffer = io.BytesIO()
        no_signal(size).save(buffer, format="JPEG", quality=85)
        _cache[key] = buffer.getvalue()
    return _cache[key]
