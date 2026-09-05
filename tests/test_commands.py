"""Drive the copied handlers with fake transports, on the REAL custom_ids.

Every assertion here is about delivery, not computation: the defects this
replaces all computed the right answer and dropped it.
"""
import os, sys, types, logging
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
logging.basicConfig(level=logging.CRITICAL)
from moonraker_pinozcam.notify import Notifier
from moonraker_pinozcam import discord_bot

class FakeTG:
    def __init__(self): self.sent=[]
    def send(self, caption="", image=None, keyboard=None, silent=False):
        self.sent.append((caption, keyboard, image)); return len(self.sent)
class FakeDC:
    def __init__(self): self.sent=[]
    def send(self, content="", image=None, components=None, silent=False):
        self.sent.append(("send", content, components, image)); return True
    def followup(self, interaction, content, components=None, image=None,
                 silent=True):
        self.sent.append(("followup", content, components, image))

class St:
    def __init__(self, state="printing", paused=False, klippy="ready"):
        self.state, self.is_paused, self.klippy_state = state, paused, klippy
        self.progress, self.filename = 0.42, "job.gcode"
        self.nozzle_temp, self.bed_temp = 210.0, 60.0
    @property
    def is_printing(self): return self.state=="printing" and not self.is_paused
class Cli:
    def __init__(self): self.state=St(); self.calls=[]
    def printer_name(self): return "testprinter"
    def pause_print(self): self.calls.append("pause"); self.state=St("paused",True)
    def resume_print(self): self.calls.append("resume"); self.state=St("printing")
    def cancel_print(self): self.calls.append("cancel"); self.state=St("cancelled")
class Cfg:
    def get(self,*a): return ""
    def get_section(self,s): return {}

JPEG = None
def build():
    n = Notifier(Cfg(), Cli(), logging.getLogger("t"),
                 snapshot=lambda: JPEG, status=lambda: "S")
    n.telegram_bot, n.discord_bot = FakeTG(), FakeDC()
    return n

fails=[]
def ck(label, cond, detail=""):
    print("  %-56s %s" % (label, "OK" if cond else "FAIL "+str(detail)))
    if not cond: fails.append(label)

class Call:                      # telebot CallbackQuery stand-in
    def __init__(self, data): self.data=data; self.message=types.SimpleNamespace(message_id=1)

print("\n1. Telegram Stop -> Yes  (yes:/no: prefixes)")
n=build(); n.handle_telegram_command("stop", Call("stop"))
ck("offer sent", len(n.telegram_bot.sent)==1, n.telegram_bot.sent)
ck("has confirm keyboard", n.telegram_bot.sent[0][1] is not None)
nonce=n.confirm_tokens[("telegram","stop")][0]
n.handle_telegram_command("yes:stop:%s"%nonce, Call("yes:stop:%s"%nonce))
ck("cancel_print called", n._client.calls==["cancel"], n._client.calls)
ck("RESULT DELIVERED", len(n.telegram_bot.sent)==2, n.telegram_bot.sent)
ck("says stopped", "stopped" in n.telegram_bot.sent[-1][0], n.telegram_bot.sent[-1][0])

print("\n2. Discord Stop -> Yes  (confirm:/cancel: prefixes -- the real ids)")
n=build(); n.handle_discord_command("stop", {"id":"i"})
ck("offer sent as followup", n.discord_bot.sent[0][0]=="followup")
ck("nonce keyed to discord", ("discord","stop") in n.confirm_tokens,
   list(n.confirm_tokens))
nonce=n.confirm_tokens[("discord","stop")][0]
raw = discord_bot.confirm_buttons("p","stop",nonce)[0]["components"][0]["custom_id"]
who, cid = discord_bot.untag(raw)
print("     real custom_id -> %r" % cid)
n.handle_discord_command(cid, {"id":"i"})
ck("cancel_print called", n._client.calls==["cancel"], n._client.calls)
ck("RESULT DELIVERED", len(n.discord_bot.sent)==2, n.discord_bot.sent)
ck("says stopped", "stopped" in n.discord_bot.sent[-1][1].lower(),
   n.discord_bot.sent[-1][1])

print("\n3. Discord Cancel button withdraws the offer")
n=build(); n.handle_discord_command("pause", {"id":"i"})
nonce=n.confirm_tokens[("discord","pause")][0]
n.handle_discord_command("cancel:pause:%s"%nonce, {"id":"i"})
ck("printer untouched", n._client.calls==[], n._client.calls)
ck("nonce dropped", ("discord","pause") not in n.confirm_tokens)
ck("says never mind", "Never mind" in n.discord_bot.sent[-1][1],
   n.discord_bot.sent[-1][1])

print("\n4. A telegram nonce cannot be used from discord")
n=build(); n.handle_telegram_command("stop", Call("stop"))
nonce=n.confirm_tokens[("telegram","stop")][0]
n.handle_discord_command("confirm:stop:%s"%nonce, {"id":"i"})
ck("printer untouched", n._client.calls==[], n._client.calls)
ck("told why", "no longer valid" in n.discord_bot.sent[-1][1],
   n.discord_bot.sent[-1][1])

print("\n5. Pause while paused offers RESUME")
n=build(); n._client.state=St("paused",True)
n.handle_telegram_command("pause", Call("pause"))
ck("offer names resume", "resume" in n.telegram_bot.sent[0][0],
   n.telegram_bot.sent[0][0])
nonce=n.confirm_tokens[("telegram","resume")][0]
n.handle_telegram_command("yes:resume:%s"%nonce, Call("y"))
ck("resume_print called", n._client.calls==["resume"], n._client.calls)

print("\n6. Idle printer: no nonce, and the user is told")
n=build(); n._client.state=St("standby")
n.handle_telegram_command("stop", Call("stop"))
ck("no nonce", n.confirm_tokens=={}, n.confirm_tokens)
ck("refusal delivered", "no active print" in n.telegram_bot.sent[-1][0],
   n.telegram_bot.sent)

print("\n7. Print ends between offer and Yes")
n=build(); n.handle_telegram_command("stop", Call("stop"))
nonce=n.confirm_tokens[("telegram","stop")][0]
n._client.state=St("complete")
n.handle_telegram_command("yes:stop:%s"%nonce, Call("y"))
ck("printer untouched", n._client.calls==[], n._client.calls)
ck("told why", "no active print" in n.telegram_bot.sent[-1][0],
   n.telegram_bot.sent[-1][0])

print("\n8. Check answers only the asking channel, with a picture")
import io as _io
from PIL import Image
buf=_io.BytesIO(); Image.new("RGB",(64,48),(1,2,3)).save(buf,"JPEG")
JPEG=buf.getvalue()
n=build(); n.handle_discord_command("check", {"id":"i"})
ck("discord answered", len(n.discord_bot.sent)==1)
ck("telegram NOT spammed", len(n.telegram_bot.sent)==0, n.telegram_bot.sent)
ck("carried an image", n.discord_bot.sent[0][3] is not None)

print("\n9. No camera -> placeholder, never empty")
JPEG=None
n=build(); n.handle_telegram_command("/hi", None)
ck("still sent a photo", len(n.telegram_bot.sent)==1 and
   n.telegram_bot.sent[0][2] is not None, n.telegram_bot.sent)
ck("caption warns", "No camera" in n.telegram_bot.sent[0][0],
   n.telegram_bot.sent[0][0])

print("\n10. mute/unmute both reply")
n=build(); n.handle_telegram_command("mute", Call("mute"))
ck("muted", n.alerts_muted is True)
ck("reply delivered", len(n.telegram_bot.sent)==1, n.telegram_bot.sent)
n.handle_discord_command("unmute", {"id":"i"})
ck("unmuted", n.alerts_muted is False)
ck("discord reply delivered", len(n.discord_bot.sent)==1)

print("\n11. !status text-only reply")
n=build(); n.handle_discord_command("status", {"id":"i"})
ck("delivered", len(n.discord_bot.sent)==1)
ck("has temps", "Nozzle" in n.discord_bot.sent[0][1], n.discord_bot.sent[0][1])

print("\n12. alert() gives each channel its own stream")
n=build(); n.alert("boom", image=JPEG or b"\xff\xd8xx")
t=n.telegram_bot.sent[0][2]; d=n.discord_bot.sent[0][3]
ck("both got bytes", t is not None and d is not None)
ck("streams are distinct objects", t is not d)
ck("both non-empty", len(t.read())>0 and len(d.read())>0)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s"%fails))
sys.exit(1 if fails else 0)
