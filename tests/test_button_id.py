"""Renaming a printer must re-issue its Discord buttons, exactly once."""
import json, logging, os, shutil, sys, tempfile
sys.path.insert(0, os.path.abspath("."))
from moonraker_pinozcam.notify import Notifier

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

class St:
    state, is_paused, klippy_state = "printing", False, "ready"
    is_printing = True
class FakeDC:
    def __init__(self): self.sent = []
    def send(self, content="", image=None, components=None, silent=False):
        self.sent.append((content, components)); return True
class Cli:
    def __init__(self, tag, settled=True):
        self.state, self._tag, self._settled = St(), tag, settled
    def printer_name(self): return "bench"
    def instance_tag(self): return self._tag
    def instance_tag_settled(self): return self._settled

TMP = tempfile.mkdtemp()
CFGFILE = os.path.join(TMP, "moonraker-pinozcam.cfg")
open(CFGFILE, "w").write("[discord]\n")

class Cfg:
    def __init__(self, name=""): self._name = name
    def get(self, section, option, fallback=""):
        return self._name if (section, option) == ("printer", "name") else ""
    def get_section(self, s): return {}
    notification = {"max_notification": 0, "notify_interval": 0}
    def state_path(self): return os.path.join(TMP, ".state")

def build(tag, name="", settled=True, discord=True):
    n = Notifier(Cfg(name), Cli(tag, settled), logging.getLogger("t"))
    n.discord_bot = FakeDC() if discord else None
    return n

def ids(components):
    return [b.get("custom_id","") for r in (components or [])
            for b in r.get("components",[]) if b.get("custom_id")]

print("1. first run remembers, and says nothing")
n = build("printer-aaaa1111"); n.announce_button_id_change()
ck("no message", n.discord_bot.sent == [], n.discord_bot.sent)
ck("id remembered",
   json.load(open(Cfg().state_path()))["discord_button_id"]=="printer-aaaa1111")

print("\n2. same id again stays quiet")
n = build("printer-aaaa1111"); n.announce_button_id_change()
ck("no message", n.discord_bot.sent == [], n.discord_bot.sent)

print("\n3. a rename re-issues buttons, with the NEW id")
n = build("printer-aaaa1111", name="Bench Left"); n.announce_button_id_change()
ck("one message", len(n.discord_bot.sent) == 1, n.discord_bot.sent)
got = ids(n.discord_bot.sent[0][1]) if n.discord_bot.sent else []
ck("buttons carry the new id",
   bool(got) and all(i.split(":")[1] == "Bench Left" for i in got), got)
ck("text explains the old ones are dead",
   "no longer respond" in (n.discord_bot.sent[0][0] if n.discord_bot.sent else ""))

print("\n4. and only once -- the next start is quiet")
n = build("printer-aaaa1111", name="Bench Left"); n.announce_button_id_change()
ck("no second message", n.discord_bot.sent == [], n.discord_bot.sent)

print("\n5. an unsettled tag is NOT a rename (slow Moonraker)")
os.remove(Cfg().state_path())
n = build("printer-aaaa1111"); n.announce_button_id_change()      # seed
n = build("bigtreetech-cb2", settled=False); n.announce_button_id_change()
ck("no message", n.discord_bot.sent == [], n.discord_bot.sent)
ck("id NOT overwritten by the fallback",
   json.load(open(Cfg().state_path()))["discord_button_id"]=="printer-aaaa1111")

print("\n6. Discord off: the announcement is owed, not lost")
n = build("printer-bbbb2222", discord=False); n.announce_button_id_change()
ck("old id kept, so it fires when Discord returns",
   json.load(open(Cfg().state_path()))["discord_button_id"]=="printer-aaaa1111")
n = build("printer-bbbb2222"); n.announce_button_id_change()
ck("now it is announced", len(n.discord_bot.sent) == 1, n.discord_bot.sent)

print("\n7. a send failure is retried, not swallowed")
class Boom(FakeDC):
    def send(self, **kw): raise RuntimeError("gateway down")
n = build("printer-cccc3333"); n.discord_bot = Boom()
n.announce_button_id_change()
ck("id not persisted on failure",
   json.load(open(Cfg().state_path()))["discord_button_id"]=="printer-bbbb2222")
n = build("printer-cccc3333"); n.announce_button_id_change()
ck("retried on the next start", len(n.discord_bot.sent) == 1)

print("\n8. a corrupt state file costs one message, not a crash")
open(Cfg().state_path(), "w").write("{not json")
n = build("printer-cccc3333"); n.announce_button_id_change()
ck("treated as first run: quiet", n.discord_bot.sent == [], n.discord_bot.sent)
ck("rewritten", json.load(open(Cfg().state_path()))["discord_button_id"]
   =="printer-cccc3333")

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
