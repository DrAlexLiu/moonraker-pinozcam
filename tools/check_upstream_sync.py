"""Verify this build has not drifted from the OctoPrint build.

Two kinds of drift are possible and neither announces itself:

  1. A shared module edited on one side only. The OctoPrint project already
     lived through exactly this: two of its repositories shared files and
     drifted in BOTH directions for months before anyone noticed.

  2. RUNTIME_VERSION falling behind upstream's latest release. Nothing
     breaks -- an older runtime still runs -- this build just silently
     ships an older daemon and model than OctoPrint users get.

Symlinks were considered for the first problem and rejected: git stores a
symlink as a path string, so a user's clone resolves it against a directory
that does not exist on their machine. install.sh would complete, the
service would start, and the first inference would fail. Copies plus this
check keep a clone self-contained.

    python3 tools/check_upstream_sync.py [--strict]

Without network access it reports and skips rather than failing, so it can
sit in a pre-commit hook as well as in CI (--strict).
"""

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

UPSTREAM = "DrAlexLiu/OctoPrint-PiNozCam"
RAW = "https://raw.githubusercontent.com/%s/master/octoprint_pinozcam/%s"
RELEASES = "https://api.github.com/repos/%s/releases/latest" % UPSTREAM
OWN_RELEASES = ("https://api.github.com/repos/"
                "DrAlexLiu/moonraker-pinozcam/releases/latest")

# Copied verbatim from the OctoPrint build: the inference core and the
# notification transports. They are deliberately NOT adapted, so any
# difference here is drift, not a porting decision.
SHARED = [
    "framesource.py", "mjpegstream.py", "framestore.py", "framebuffer.py",
    "cpu_affinity.py", "credentials.py", "confirm.py", "telegram_bot.py",
    "discord_bot.py",
]

# Files that track upstream but carry a known, documented difference. The
# value is how many lines are expected to differ; a change in that count
# means something NEW diverged and needs looking at. Keeping this dict tiny
# is the point -- an allowed-differences list that grows is exactly how two
# codebases drift apart while the check stays green.
ALLOWED_DIFF = {
    # plugin_dir may be None here (this build always installs its runtime
    # as a package) and the in-tree package name differs.
    "nozcam_backend.py": 18,
}

# Individual METHODS copied verbatim out of a module that is otherwise not
# shared. notify.py cannot be shared whole -- it is written against
# OctoPrint's plugin mixin -- but its two command handlers are the other
# half of telegram_bot.py's and discord_bot.py's protocol, which ARE
# shared. Re-implementing them from a description produced three
# simultaneous defects that no unit test without a real button could see:
# Discord's confirm:/cancel: custom_ids matched against Telegram's
# yes:/no:, confirmation nonces looked up under the wrong channel, and
# replies returned as strings that both transports discard. So they are
# copied, and checked here at function granularity.
SHARED_METHODS = {
    "notify.py": [
        "handle_telegram_command", "handle_discord_command",
        "_action_state_problem", "check_reply",
        "telegram_send_with_reply", "get_printer_status",
    ],
}

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCAL_DIR = os.path.join(HERE, "moonraker_pinozcam")


def fetch(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": "pinozcam-sync"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read()


def digest(data):
    return hashlib.sha256(data).hexdigest()[:16]


def diff_line_count(a, b):
    """How many lines differ, regardless of where."""
    import difflib
    left = a.decode("utf-8", "replace").splitlines()
    right = b.decode("utf-8", "replace").splitlines()
    return sum(1 for line in difflib.unified_diff(left, right, n=0)
               if line[:1] in "+-" and not line.startswith(("+++", "---")))


def check_modules():
    drifted, missing, checked = [], [], 0
    for name in SHARED + list(ALLOWED_DIFF):
        local_path = os.path.join(LOCAL_DIR, name)
        if not os.path.isfile(local_path):
            missing.append(name)
            print("  ! %-22s not present locally" % name)
            continue
        with open(local_path, "rb") as handle:
            local = handle.read()
        try:
            upstream = fetch(RAW % (UPSTREAM, name))
        except (urllib.error.URLError, OSError) as exc:
            print("  ? %-22s upstream unreachable (%s)" % (name, exc))
            continue
        checked += 1
        if local == upstream:
            print("  OK   %-20s %s" % (name, digest(local)))
        elif name in ALLOWED_DIFF:
            got, want = diff_line_count(upstream, local), ALLOWED_DIFF[name]
            if got == want:
                print("  OK   %-20s %d differing lines, as documented"
                      % (name, got))
            else:
                print("  DIFF %-20s %d differing lines, expected %d -- "
                      "something new diverged" % (name, got, want))
                drifted.append(name)
        else:
            print("  DIFF %-20s local %s vs upstream %s"
                  % (name, digest(local), digest(upstream)))
            drifted.append(name)
    return drifted, missing, checked


def extract_method(text, name):
    """The exact source of one method, or None.

    Indentation-based rather than AST-based on purpose: this must work on
    upstream text that may use syntax this interpreter does not parse.
    """
    lines = text.splitlines(keepends=True)
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("def %s(" % name):
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("    def ") or lines[index].startswith(
                "class "):
            end = index
            break
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "".join(lines[start:end])


def check_methods():
    """Compare the individually-copied methods, not whole files."""
    drifted, checked = [], 0
    for module, names in SHARED_METHODS.items():
        local_path = os.path.join(LOCAL_DIR, module)
        if not os.path.isfile(local_path):
            print("  ! %-22s not present locally" % module)
            drifted.append(module)
            continue
        with open(local_path, encoding="utf-8") as handle:
            local_text = handle.read()
        try:
            upstream_text = fetch(RAW % (UPSTREAM, module)).decode("utf-8")
        except (urllib.error.URLError, OSError) as exc:
            print("  ? %-22s upstream unreachable (%s)" % (module, exc))
            continue
        for name in names:
            checked += 1
            mine = extract_method(local_text, name)
            theirs = extract_method(upstream_text, name)
            label = "%s:%s" % (module, name)
            if theirs is None:
                # Upstream removed or renamed it. Not automatically wrong,
                # but it means this copy no longer tracks anything.
                print("  GONE %-34s not in upstream any more" % label)
                drifted.append(label)
            elif mine is None:
                print("  MISS %-34s not copied here" % label)
                drifted.append(label)
            elif mine == theirs:
                print("  OK   %-34s %s"
                      % (label, digest(mine.encode("utf-8"))))
            else:
                got = diff_line_count(theirs.encode("utf-8"),
                                      mine.encode("utf-8"))
                print("  DIFF %-34s %d differing lines" % (label, got))
                drifted.append(label)
    return drifted, checked


def check_runtime_version():
    """Compare the version the installer will ask for with what exists.

    ⚠️ Two things about this were wrong and both were invisible, because
    the summary line at the end counts only file drift:

    * it read RUNTIME_VERSION by matching a line of text, and that line
      became an expression -- so it "compared" the literal string
      `os.environ.get("PINOZCAM_RUNTIME_VERSION") \` against a real
      version and printed a DIFF nobody read. It imports the module now,
      which is the only way to get the answer the installer will use.
    * it compared against the OctoPrint build's latest release. The
      installer stopped resolving there when this repository started
      building its own Wheels, so the question is what THIS repository
      has published.
    """
    sys.path.insert(0, os.path.join(HERE, "scripts"))
    sys.path.insert(0, HERE)
    try:
        import install_runtime
        pinned = install_runtime.RUNTIME_VERSION
    except Exception as exc:                                 # noqa: BLE001
        print("  ! could not read RUNTIME_VERSION: %s" % exc)
        return False
    try:
        latest = json.loads(fetch(OWN_RELEASES))["tag_name"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        # A repository with no Release yet is the normal state before the
        # first one, not a failure -- but say which it is.
        print("  ? installer wants %s; this repository has published no "
              "release yet (%s)" % (pinned, type(exc).__name__))
        return True
    if pinned == latest:
        print("  OK   runtime %s == this repository's latest release"
              % pinned)
        return True
    print("  DIFF installer wants %s, latest release here is %s"
          % (pinned, latest))
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on drift (for CI)")
    args = parser.parse_args()

    print("Shared modules (must be byte-identical to upstream):")
    drifted, missing, checked = check_modules()
    print("\nMethods copied verbatim out of an unshared module:")
    method_drift, method_count = check_methods()
    drifted += method_drift
    checked += method_count
    print("\nRuntime version:")
    version_ok = check_runtime_version()
    print("\n%d checked, %d drifted, %d missing"
          % (checked, len(drifted), len(missing)))

    if drifted:
        print("\nResync from the OctoPrint build:")
        for name in drifted:
            if ":" in name:
                module, method = name.split(":", 1)
                print("  re-copy %s() from ../test/octoprint_pinozcam/%s"
                      % (method, module))
            else:
                print("  cp ../test/octoprint_pinozcam/%s "
                      "moonraker_pinozcam/%s" % (name, name))
        print("\nIf a local change is intentional, that module no longer "
              "belongs in SHARED -- move it out and record why.")

    failed = bool(drifted or missing) or not version_ok
    return 1 if (failed and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
