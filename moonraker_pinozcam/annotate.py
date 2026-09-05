"""Draw detection boxes onto a frame.

Kept separate from both the detector and the web server: the detector
should not know how results are displayed, and the web server should not
know what a detection is.
"""

import io

from PIL import Image, ImageDraw

# Boxes are drawn on the ORIGINAL frame, so a fixed pixel width looks hairline
# on 1080p and heavy on 640x480. Scale it, with a floor so it never vanishes.
_MIN_WIDTH = 2
_WIDTH_DIVISOR = 400

ALARM_COLOR = (255, 64, 64)
QUIET_COLOR = (255, 196, 0)


def annotate(image, boxes, scores, severity, quality=85):
    """Return JPEG bytes of `image` with `boxes` drawn on it.

    Colour carries the verdict: amber for detections that are present but
    below the alarm level, red once severity crosses it. A user glancing at
    a phone should not have to read numbers to know which it is.
    """
    frame = image.convert("RGB")
    draw = ImageDraw.Draw(frame)
    width = max(_MIN_WIDTH, min(frame.size) // _WIDTH_DIVISOR)
    colour = ALARM_COLOR if severity >= 0.5 else QUIET_COLOR

    for index, box in enumerate(boxes or []):
        try:
            x1, y1, x2, y2 = (float(v) for v in box[:4])
        except (TypeError, ValueError):
            continue
        draw.rectangle([x1, y1, x2, y2], outline=colour, width=width)
        if scores is not None and index < len(scores):
            label = "%.2f" % float(scores[index])
            # Put the label inside the box when it would fall off the top,
            # which happens for anything detected near the frame edge.
            ty = y1 - 14 if y1 > 16 else y1 + 2
            draw.text((x1 + 2, ty), label, fill=colour)

    buffer = io.BytesIO()
    frame.save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


def passthrough(jpeg_bytes):
    """Return the frame unchanged, for when there is nothing to draw."""
    return jpeg_bytes
