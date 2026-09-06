"""Rotation must match Moonraker's spec: `rotation` is CLOCKWISE degrees."""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from PIL import Image
from moonraker_pinozcam.camera import CameraSource, transform_image

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

def marked():
    """A 40x20 black frame with a white 4x4 mark in the TOP-LEFT corner."""
    im = Image.new("RGB", (40, 20), (0, 0, 0))
    for x in range(4):
        for y in range(4):
            im.putpixel((x, y), (255, 255, 255))
    return im

def corner(im):
    """Which corner holds the white mark."""
    w, h = im.size
    probes = {"top-left": (1, 1), "top-right": (w - 2, 1),
              "bottom-left": (1, h - 2), "bottom-right": (w - 2, h - 2)}
    for name, xy in probes.items():
        if im.getpixel(xy) == (255, 255, 255):
            return name
    return "?"

def src(rot=0, fh=False, fv=False):
    return CameraSource("http://c/s", None, origin="moonraker",
                        rotation=rot, flip_h=fh, flip_v=fv)

ck("no rotation leaves it top-left", corner(transform_image(marked(), src(0))) == "top-left")

# Clockwise 90 sends a top-left mark to the TOP-RIGHT.
got = corner(transform_image(marked(), src(90)))
ck("rotation=90 is clockwise -> top-right", got == "top-right", got)

got = corner(transform_image(marked(), src(270)))
ck("rotation=270 is clockwise -> bottom-left", got == "bottom-left", got)

got = corner(transform_image(marked(), src(180)))
ck("rotation=180 -> bottom-right", got == "bottom-right", got)

# 90/270 swap the axes; 180 does not.
ck("90 swaps the axes", transform_image(marked(), src(90)).size == (20, 40))
ck("180 keeps them", transform_image(marked(), src(180)).size == (40, 20))

# Flips are applied before the rotation; check one composite stays sane.
got = corner(transform_image(marked(), src(0, fh=True)))
ck("flip_horizontal -> top-right", got == "top-right", got)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
