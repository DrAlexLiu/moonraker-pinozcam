"""The sampler must not pace a camera that pushes."""
import os, sys, time
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from io import BytesIO
from PIL import Image
from moonraker_pinozcam import framesource

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

def jpeg(colour=(10, 20, 30)):
    b = BytesIO(); Image.new("RGB", (64, 48), colour).save(b, "JPEG")
    return b.getvalue()

class Buf:
    def __init__(self, deep): self.deep, self.puts = deep, []
    def should_measure(self): return self.deep
    def put(self, image, score, at): self.puts.append((score, at))

class Det:
    """Only the two sampler halves, with their real bodies."""
    from moonraker_pinozcam.detector import Detector
    _sample_pushed = Detector._sample_pushed
    _sample_pulled = Detector._sample_pulled
    _decode = Detector._decode
    def __init__(self, deep, interval=0.2):
        self._buffer = Buf(deep)
        self._sample_interval = interval
        self.camera_source = None
        self.decodes = 0
        class L:
            def debug(*a): pass
        self._log = L()
    def _check_mask_fits(self, size): pass
    def _measure_sharpness(self, image):
        return 1.0
    def _decode_counted(self, frame):
        self.decodes += 1
        return Det._decode(self, frame)

F = framesource.EncodedFrame

print("1. a shallow buffer takes every pushed frame, ignoring the interval")
d = Det(deep=False)
last = 0.0
for i in range(5):
    last = d._sample_pushed(F(jpeg(), 100.0 + i, i), last)
ck("all five kept", len(d._buffer.puts) == 5, len(d._buffer.puts))
ck("none of them scored", all(s == float("-inf") for s, _ in d._buffer.puts))
ck("captured_at is the frame's, not the loop's",
   [at for _, at in d._buffer.puts] == [100.0, 101.0, 102.0, 103.0, 104.0],
   [at for _, at in d._buffer.puts])

print("\n2. a deep buffer inside the interval drops WITHOUT decoding")
d = Det(deep=True, interval=10.0)
d._decode = d._decode_counted
last = d._sample_pushed(F(jpeg(), 200.0, 0), 0.0)
ck("the first one is taken", len(d._buffer.puts) == 1)
before = d.decodes
for i in range(1, 6):
    last = d._sample_pushed(F(jpeg(), 200.0 + i, i), last)
ck("the next five are refused", len(d._buffer.puts) == 1, len(d._buffer.puts))
ck("and none of them was decoded", d.decodes == before, d.decodes - before)

print("\n3. a deep buffer past the interval decodes and scores")
d = Det(deep=True, interval=0.0)
last = d._sample_pushed(F(jpeg(), 300.0, 0), 0.0)
last = d._sample_pushed(F(jpeg(), 301.0, 1), last)
ck("both taken", len(d._buffer.puts) == 2, len(d._buffer.puts))
ck("both scored", all(s == 1.0 for s, _ in d._buffer.puts))

print("\n4. a pull source is never paced by us -- it self-paces")
d = Det(deep=True, interval=10.0)
for i in range(4):
    d._sample_pulled(F(jpeg(), 400.0 + i, i))
ck("every frame accepted despite a 10 s interval",
   len(d._buffer.puts) == 4, len(d._buffer.puts))
ck("captured_at preserved",
   [at for _, at in d._buffer.puts] == [400.0, 401.0, 402.0, 403.0])

print("\n5. an undecodable frame is skipped, not fatal")
d = Det(deep=False)
last = d._sample_pushed(F(b"not a jpeg", 500.0, 0), 0.0)
ck("nothing put", d._buffer.puts == [])
ck("and the accept time did not move", last == 0.0, last)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
