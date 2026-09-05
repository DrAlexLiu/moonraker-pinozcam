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


def check_runtime_version():
    pinned = None
    path = os.path.join(HERE, "scripts", "install_runtime.py")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("RUNTIME_VERSION"):
                pinned = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if pinned is None:
        print("  ! RUNTIME_VERSION not found")
        return False
    try:
        latest = json.loads(fetch(RELEASES))["tag_name"]
    except (urllib.error.URLError, OSError, KeyError, ValueError) as exc:
        print("  ? pinned %s, upstream unreachable (%s)" % (pinned, exc))
        return True
    if pinned == latest:
        print("  OK   runtime %s == upstream latest" % pinned)
        return True
    print("  DIFF runtime pinned %s, upstream latest %s" % (pinned, latest))
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true",
                        help="exit non-zero on drift (for CI)")
    args = parser.parse_args()

    print("Shared modules (must be byte-identical to upstream):")
    drifted, missing, checked = check_modules()
    print("\nRuntime version:")
    version_ok = check_runtime_version()
    print("\n%d checked, %d drifted, %d missing"
          % (checked, len(drifted), len(missing)))

    if drifted:
        print("\nResync from the OctoPrint build:")
        for name in drifted:
            print("  cp ../test/octoprint_pinozcam/%s moonraker_pinozcam/%s"
                  % (name, name))
        print("\nIf a local change is intentional, that module no longer "
              "belongs in SHARED -- move it out and record why.")

    failed = bool(drifted or missing) or not version_ok
    return 1 if (failed and args.strict) else 0


if __name__ == "__main__":
    sys.exit(main())
