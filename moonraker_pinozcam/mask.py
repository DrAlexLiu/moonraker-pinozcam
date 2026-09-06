import hashlib
"""Mask out regions the detector should ignore.

Ported from the OctoPrint build's mask.py, keeping its arithmetic exactly.
What is NOT ported is the camera-signature machinery: that build stores a
signature derived from the camera URL and suspends the mask when the camera
or aspect ratio changes, because a grid drawn against one picture blacks out
an arbitrary region of a different one -- a silent detection hole. Here the
camera is resolved from Moonraker or the config file and does not change
under the service, so the mask is applied whenever one is set.

⚠️ If this build ever gains camera switching, that machinery has to come
with it. A stale mask does not fail loudly; it quietly hides part of the
frame from detection.
"""

import math

from PIL import ImageDraw

# The grid the UI paints on. Kept equal to the OctoPrint build's so a mask
# string can be copied between the two.
MASK_GRID = 128


def decode(data, grid=MASK_GRID):
    """Decode a mask string into a square boolean matrix.

    The stored resolution is taken from the string's length rather than
    assumed, so a mask written by an older version (64x64 = 4096 chars)
    still loads; anything smaller is upscaled by nearest neighbour, which
    is exact for power-of-two ratios. Users never have to redraw.
    """
    empty = [[False] * grid for _ in range(grid)]
    if not data:
        return empty
    size = int(math.sqrt(len(data)))
    if size == 0 or size * size != len(data):
        return empty            # not square -- ignore rather than guess
    rows = [[data[r * size + c] == "1" for c in range(size)]
            for r in range(size)]
    if size == grid:
        return rows
    return [[rows[r * size // grid][c * size // grid] for c in range(grid)]
            for r in range(grid)]


def encode(matrix):
    """Encode a boolean matrix back into the stored string form."""
    return "".join("1" if cell else "0" for row in matrix for cell in row)


def is_empty(data):
    return not data or "1" not in data


def signature(source_size, source):
    """The coordinate system a mask was painted in.

    A 128x128 grid is normalised over the frame, so a different pixel SIZE
    with the same shape still maps correctly. What does not map is a
    different aspect ratio or a different transform -- those move every
    cell, and a mask applied across such a change blacks out the wrong
    region and can hide a real failure without saying anything.

    ⚠️ The aspect is the SOURCE frame's, taken BEFORE rotation swaps the
    axes, and the rotation is recorded separately. Storing the
    post-transform ratio would be actively harmful: turning rotation on
    would move the ratio and the flag together, so a plain rotation would
    look like a camera swap -- the one case that must not be treated as
    one. Same reasoning as the OctoPrint build's _camera_signature.
    """
    width, height = source_size
    aspect = round(width / float(height), 3) if height else 0.0
    return "%.3f|%d|%d%d|%s" % (
        aspect,
        int(getattr(source, "rotation", 0) or 0) % 360,
        int(bool(getattr(source, "flip_h", False))),
        int(bool(getattr(source, "flip_v", False))),
        _camera_identity(source))


def signature_matches(stored, live):
    """Whether a stored signature still describes the live camera.

    ⚠️ Not plain equality, because the identity field was added later. A
    signature saved before it existed has three fields, and comparing it
    to a four-field one would suspend every mask that already exists --
    silently turning off a zone the user painted and relies on. An old
    signature is honoured on its geometry alone, which is exactly the
    guarantee it was written with; it gets the identity the next time the
    mask is saved. A four-field signature is compared in full.
    """
    stored = (stored or "").strip()
    live = (live or "").strip()
    if not stored or not live:
        return True
    if stored.count("|") < 3:
        return live.split("|")[:3] == stored.split("|")[:3]
    return stored == live


def _camera_identity(source):
    """A short, stable fingerprint of WHICH camera this is.

    ⚠️ Geometry alone is not identity. Two cameras of the same aspect ratio
    -- the overwhelmingly common case, since almost everything is 16:9 --
    produce the same signature, so pointing the config at a different one
    kept applying a zone painted for the first and silently blacked out the
    wrong region. Upstream's _camera_signature carries the source and the
    URL for exactly this reason.

    Hashed, not stored plain: the signature lives in the config file and is
    shown on a page that has no login, and a snapshot URL can carry
    credentials in its userinfo or query string.
    """
    url = (getattr(source, "snapshot_url", None)
           or getattr(source, "stream_url", None) or "")
    origin = getattr(source, "origin", "") or ""
    if not url and not origin:
        return "?"
    digest = hashlib.sha256(("%s\0%s" % (origin, url)).encode("utf-8"))
    return digest.hexdigest()[:12]


def apply_to_image(image, data, grid=MASK_GRID):
    """Return a copy of `image` with masked cells painted black."""
    masked = image.convert("RGB")
    if is_empty(data):
        return masked
    matrix = decode(data, grid)
    if not any(any(row) for row in matrix):
        return masked

    draw = ImageDraw.Draw(masked)
    width, height = masked.size
    for row in range(grid):
        # Compute both edges from the exact ratio rather than multiplying a
        # rounded-up block size, which overran the image by up to 63 px on
        # heights that are not a multiple of the grid.
        y1 = height * row // grid
        y2 = height * (row + 1) // grid
        col = 0
        while col < grid:
            if not matrix[row][col]:
                col += 1
                continue
            # Merge horizontally adjacent cells into one rectangle: 128x128
            # cells would otherwise be up to 16384 draw calls per frame,
            # which matters on a 1 GHz A53.
            start = col
            while col < grid and matrix[row][col]:
                col += 1
            x1 = width * start // grid
            x2 = width * col // grid
            # A frame smaller than the grid collapses cells to zero extent,
            # and Pillow rejects a rectangle whose second corner precedes
            # its first. Skipping them is correct -- a sub-pixel cell has
            # nothing to cover, and the mask becomes as coarse as the frame
            # allows.
            if x2 > x1 and y2 > y1:
                draw.rectangle((x1, y1, x2 - 1, y2 - 1), fill=(0, 0, 0))
    return masked
