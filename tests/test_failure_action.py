"""Episode latching: raising the action mid-failure, and a failed alert."""
import logging, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from moonraker_pinozcam.notify import Notifier

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

# ---- 1. alert() reports delivery ---------------------------------------
class TG:
    def __init__(self, ok=True): self.ok, self.n = ok, 0
    def send(self, **kw):
        self.n += 1
        return 42 if self.ok else None        # message id, or None on failure
class DC:
    def __init__(self, ok=True, boom=False): self.ok, self.boom, self.n = ok, boom, 0
    def send(self, **kw):
        self.n += 1
        if self.boom: raise RuntimeError("gateway down")
        return True if self.ok else False
class St:
    state, is_paused, klippy_state = "printing", False, "ready"
    nozzle_temp = bed_temp = None
    @property
    def is_printing(self): return True
class Cli:
    def __init__(self): self.state = St()
    def printer_name(self): return "bench"
    def instance_tag(self): return "printer-abcd1234"
class Cfg:
    def get(self, *a): return ""
    def get_section(self, s): return {}
    notification = {"max_notification": 0, "notify_interval": 0}

def notifier(tg=None, dc=None):
    n = Notifier(Cfg(), Cli(), logging.getLogger("t"))
    n.telegram_bot, n.discord_bot = tg, dc
    return n

print("1. alert() tells the caller whether anything was delivered")
ck("both channels fine -> True", notifier(TG(True), DC(True)).alert("x") is True)
ck("both channels fail  -> False", notifier(TG(False), DC(False)).alert("x") is False)
ck("one of two works    -> True", notifier(TG(False), DC(True)).alert("x") is True)
ck("a raising channel is a failure, not a crash",
   notifier(TG(False), DC(boom=True)).alert("x") is False)
n = notifier(TG(False), DC(boom=True))
n.alert("x")
ck("a raising Discord did not stop Telegram being tried", n.telegram_bot.n == 1)
ck("no channel configured -> False", notifier(None, None).alert("x") is False)
ck("has_channel() reflects that", notifier(None, None).has_channel() is False)
n = notifier(TG(True), DC(True)); n.alerts_muted = True
ck("muted counts as handled, or every frame re-fires", n.alert("x") is True)
ck("muted really sent nothing", n.telegram_bot.n == 0 and n.discord_bot.n == 0)

# ---- 2. the episode latches at a LEVEL ---------------------------------
print("\n2. raising the action mid-failure acts again")
class FakeDet:
    """Just the latching decision, lifted out of Detector._decide."""
    def __init__(self, action):
        self.action, self.fired, self.calls = action, None, []
    def tick(self, met, handled=True):
        want = self.action
        if not met:
            self.fired = None
            return
        if self.fired is None or want > self.fired:
            self.calls.append(want)
            if handled:
                self.fired = want

d = FakeDet(0)
d.tick(True)                      # alert only fires
ck("alert-only fired once", d.calls == [0], d.calls)
d.tick(True)
ck("and does not repeat", d.calls == [0], d.calls)
d.action = 2                      # user raises it to Stop mid-failure
d.tick(True)
ck("raising to Stop acts immediately", d.calls == [0, 2], d.calls)
d.tick(True)
ck("but only once at the new level", d.calls == [0, 2], d.calls)
d.action = 1                      # lowering must NOT re-fire
d.tick(True)
ck("lowering does not re-fire", d.calls == [0, 2], d.calls)
d.tick(False); d.tick(True)
ck("a new episode fires again", d.calls == [0, 2, 1], d.calls)

print("\n3. a handler that fails leaves the episode armed")
d = FakeDet(2); d.tick(True, handled=False)
ck("did not latch", d.fired is None)
d.tick(True, handled=True)
ck("so the next frame retries", d.calls == [2, 2], d.calls)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
