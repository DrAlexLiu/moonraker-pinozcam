"""Reload semantics and concurrent writes -- both shipped broken once."""
import io, os, shutil, sys, tempfile, threading, time
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from moonraker_pinozcam.config import Config

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

TMP = tempfile.mkdtemp()
def fresh(body="[detection]\nfailure_ratio = 0.05\n"):
    p = os.path.join(TMP, "c%d.cfg" % time.time_ns())
    io.open(p, "w", encoding="utf-8").write(body)
    return p

print("1. an edit between construction and the first poll is NOT lost")
# ⚠️ The exact defect: the baseline used to be established on the first
# poll, which advanced the stamp without re-reading, so this edit was
# dropped permanently rather than merely late.
p = fresh(); c = Config(p)
ck("loaded the original", c.get("detection", "failure_ratio") == "0.05")
time.sleep(0.01)
io.open(p, "w", encoding="utf-8").write("[detection]\nfailure_ratio = 0.13\n")
ck("first poll reports the change", c.reload_if_changed() is True)
ck("and the value is the new one", c.get("detection", "failure_ratio") == "0.13",
   c.get("detection", "failure_ratio"))

print("\n2. an unchanged file is not re-read")
ck("second poll is quiet", c.reload_if_changed() is False)

print("\n3. our own write does not read back as an external edit")
p = fresh(); c = Config(p)
c.write_options("detection", {"failure_ratio": "0.20"})
ck("value applied", c.get("detection", "failure_ratio") == "0.20")
ck("poll after our own write is quiet", c.reload_if_changed() is False)

print("\n4. a half-written file is retried, not treated as seen")
p = fresh(); c = Config(p)
time.sleep(0.01)
io.open(p, "w", encoding="utf-8").write("[detection\nfailure_ratio = 9\n")  # broken INI
ck("broken INI is not accepted", c.reload_if_changed() is False)
ck("old value survives", c.get("detection", "failure_ratio") == "0.05")
time.sleep(0.01)
io.open(p, "w", encoding="utf-8").write("[detection]\nfailure_ratio = 0.31\n")
ck("the repaired file IS picked up", c.reload_if_changed() is True)
ck("with its value", c.get("detection", "failure_ratio") == "0.31")

print("\n5. a save through the shared object IS visible to the detector")
# ⚠️ The regression this exists to stop. One Config object is shared by
# the web server and the detector; a page save updates it in-process, so
# the file's mtime tells the detector nothing and reload_if_changed()
# answers False. Watching the generation is what makes the save land.
p = fresh(); c = Config(p)
gen0 = c.generation
c.write_options("detection", {"failure_ratio": "0.42"})
ck("the value is loaded", c.detection["failure_ratio"] == 0.42,
   c.detection["failure_ratio"])
ck("the generation moved", c.generation > gen0, (gen0, c.generation))
ck("and mtime alone would NOT have said so", c.reload_if_changed() is False)
gen1 = c.generation
time.sleep(0.01)
io.open(p, "w", encoding="utf-8").write("[detection]\nfailure_ratio = 0.44\n")
ck("an external edit also moves it", c.reload_if_changed() is True
   and c.generation > gen1, (gen1, c.generation))
ck("an unchanged file does not", c.reload_if_changed() is False
   and c.generation == gen1 + 1)

print("\n6. concurrent writes lose nothing and leave no stray temp")
# Two request threads, as ThreadingHTTPServer would run them: one saving
# settings, one saving the mask. Both rewrite the whole file.
p = fresh("[detection]\nfailure_ratio = 0.05\n\n[camera]\nmask_image_data = \n")
c = Config(p)
errors = []
def writer(section, key, value, n):
    for i in range(n):
        try: c.write_options(section, {key: "%s%d" % (value, i)})
        except Exception as exc: errors.append("%s: %r" % (section, exc))
a = threading.Thread(target=writer, args=("detection", "failure_ratio", "0.1", 40))
b = threading.Thread(target=writer, args=("camera", "mask_image_data", "m", 40))
a.start(); b.start(); a.join(); b.join()
ck("no exceptions", not errors, errors[:3])
final = Config(p)
ck("BOTH sections survived", final.get("detection", "failure_ratio").startswith("0.1")
   and final.get("camera", "mask_image_data").startswith("m"),
   (final.get("detection", "failure_ratio"), final.get("camera", "mask_image_data")))
strays = [f for f in os.listdir(TMP) if f.endswith(".tmp")]
ck("no temp files left behind", not strays, strays)

print("\n7. Detector._refresh applies a save made through the shared object")
# ⚠️ The end-to-end regression, driven directly because _refresh only runs
# inside the detection loop and that loop only runs during a print -- so
# the board cannot show this without one.
from moonraker_pinozcam.detector import Detector
p = fresh("[detection]\nfailure_ratio = 0.05\nimg_sensitivity = 0.04\n"
          "scores_threshold = 0.87\ncount_time = 300\n")
c = Config(p)

class W:
    failure_ratio, count_time = 0.05, 300.0
    def reset(self): self.was_reset = True
class D:
    _refresh = Detector._refresh
    def __init__(self, cfg):
        self._cfg, self._cfg_generation = cfg, -1
        self.window = W()
        self._sensitivity = cfg.detection["img_sensitivity"]
        self._score_threshold = cfg.detection["scores_threshold"]
        self._sample_interval = cfg.detection["frame_sample_interval"]
        self._detection_interval = cfg.detection["detection_interval"]
        self._enabled = True
        self._paused_by_switch = False
        self._buffer = None
        self._buffer_max_age = cfg.detection["frame_buffer_max_age"]
        self._mask = ""
        self._fired_level = None
        self._ai_backend = cfg.detection.get("ai_backend")
        class L:
            def info(self, *a): pass
            def warning(self, *a): pass
        self._log = L()

d = D(c)
d._refresh()                                   # first pass adopts the file
ck("starts from the file", d.window.failure_ratio == 0.05,
   d.window.failure_ratio)
c.write_options("detection", {"failure_ratio": "0.19"})   # the page saves
d._refresh()
ck("a page save reaches the detector", d.window.failure_ratio == 0.19,
   d.window.failure_ratio)
before = d.window.failure_ratio
d._refresh()
ck("and a second refresh is a no-op", d.window.failure_ratio == before)

time.sleep(0.01)
io.open(p, "w", encoding="utf-8").write(
    "[detection]\nfailure_ratio = 0.23\nimg_sensitivity = 0.04\n"
    "scores_threshold = 0.87\ncount_time = 300\n")
d._refresh()
ck("an external edit reaches it too", d.window.failure_ratio == 0.23,
   d.window.failure_ratio)

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
