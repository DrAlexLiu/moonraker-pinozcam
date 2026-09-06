"""The optional password: off by default, and what it does and does not cover."""
import base64, os, sys
sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from moonraker_pinozcam.web import AnnotatedView, _Handler

fails = []
def ck(label, cond, detail=""):
    print("  %-58s %s" % (label, "OK" if cond else "FAIL " + str(detail)))
    if not cond: fails.append(label)

class Cfg:
    def __init__(self, user="pinozcam", password=""):
        self._w = {"port": 58888, "register_webcam": True,
                   "user": user, "password": password}
    web = property(lambda self: self._w)
    camera = {}
    def get(self, *a): return ""
    def get_section(self, s): return {}

def view(user="pinozcam", password=""):
    v = AnnotatedView.__new__(AnnotatedView)
    v._config = Cfg(user, password)
    return v

def basic(user, pw):
    return "Basic " + base64.b64encode(("%s:%s" % (user, pw)).encode()).decode()

print("1. no password configured means no password required")
v = view()
ck("password reads empty", v.password == "")
ck("check_basic is irrelevant then", v.check_basic("") is False)  # gate skips it

print("\n2. with one set, only the right pair passes")
v = view(password="s3cret")
ck("correct pair", v.check_basic(basic("pinozcam", "s3cret")) is True)
ck("wrong password", v.check_basic(basic("pinozcam", "wrong")) is False)
ck("wrong user", v.check_basic(basic("root", "s3cret")) is False)
ck("no header", v.check_basic("") is False)
ck("not Basic", v.check_basic("Bearer s3cret") is False)
ck("malformed base64", v.check_basic("Basic !!!not-base64!!!") is False)
ck("empty password offered", v.check_basic(basic("pinozcam", "")) is False)
ck("a custom user is honoured",
   view("alice", "p").check_basic(basic("alice", "p")) is True)
ck("password is whitespace-stripped consistently",
   view(password=" s3cret ").check_basic(basic("pinozcam", "s3cret")) is True)

print("\n3. the open list is exactly the images plus health")
ck("images open", {"/snapshot", "/current", "/stream", "/camera"}
   <= _Handler.OPEN_PATHS)
ck("health open", "/health" in _Handler.OPEN_PATHS)
for guarded in ("/", "/api/settings", "/api/status", "/api/mask"):
    ck("%s is guarded" % guarded, guarded not in _Handler.OPEN_PATHS)

print("\n4. the password can never be read or written through the API")
from moonraker_pinozcam.web import AnnotatedView as AV
ck("listed as a secret", "password" in AV.SECRETS)
ck("not settable through the API", "password" not in AV.EDITABLE)
ck("nor through the camera or notify field lists",
   "password" not in AV.EDITABLE_CAMERA and "password" not in AV.EDITABLE_NOTIFY)

print("\n%s" % ("ALL PASS" if not fails else "FAILED: %s" % fails))
sys.exit(1 if fails else 0)
