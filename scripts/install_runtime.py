"""Install the runtime package this machine needs.

The runtime carries the runner binaries and models; without it there is
nothing to infer with. Which one is right depends on the hardware, so this
asks nozcam_backend -- the same code the detector uses at run time -- rather
than reimplementing detection and risking the two disagreeing.

CPU and GPU runtimes are on PyPI. The NPU ones are not: they are per-chip
artifacts with no PyPI project, published only as GitHub Release assets, so
they install from a direct URL.

Run as:  python -m scripts.install_runtime <path-to-venv-pip>
"""

import subprocess
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The Release this installer pulls from.
#
# ⚠️ THREE sources, in this order, because a source ZIP has no git. The
# release workflow accepts 1.1.0rc* tags and stamps the Wheels with the
# same version, so an RC that resolves to "1.1.0" asks for assets that do
# not exist -- which is what a hardcoded constant did, and what reading
# only the git tag still did for anyone who downloaded the ZIP rather
# than cloning.
#
#   1. PINOZCAM_RUNTIME_VERSION, for building or testing against another
#      Release without editing anything;
#   2. the tag this checkout is on, when it is a git checkout on one;
#   3. the DIRECTORY NAME, because GitHub's source archive for a tag
#      unpacks to `<repo>-<tag>` -- so a ZIP download carries its own
#      version even with no .git, which is the case that made an RC ask
#      for the release's assets;
#   4. moonraker_pinozcam.__version__, for a plain branch checkout.
def _package_version():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, root)
    try:
        from moonraker_pinozcam import __version__
        return __version__
    except Exception:                                        # noqa: BLE001
        return "1.1.0"


def _runtime_version():
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        out = subprocess.run(
            ["git", "-C", root, "describe", "--tags", "--exact-match"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10)
    except Exception:                                        # noqa: BLE001
        return _DEFAULT_RUNTIME_VERSION
    tag = out.stdout.decode("utf-8", "replace").strip()
    if out.returncode == 0 and tag:
        return tag.lstrip("v")
    from_dir = _archive_version(root)
    if from_dir:
        return from_dir
    return _package_version()


def _archive_version(root):
    """The version in a GitHub source-archive directory name, or None.

    `moonraker-pinozcam-1.1.0rc0` -> `1.1.0rc0`. Deliberately strict: it
    only answers when what follows the repository name looks like a
    version, so an ordinary clone in a directory someone renamed does not
    get a version invented for it.
    """
    import re
    name = os.path.basename(os.path.normpath(root))
    match = re.match(r"^moonraker-pinozcam-(v?\d+\.\d+\.\d+[0-9A-Za-z.\-]*)$",
                     name)
    return match.group(1).lstrip("v") if match else None


RUNTIME_VERSION = os.environ.get("PINOZCAM_RUNTIME_VERSION") \
    or _runtime_version()

RELEASE_BASE = (
    # This repository's own Releases. The wheels are built here, by
    # .github/workflows/build-runtime-release.yml, from the sources in
    # src/ -- so an install pulls artifacts from the same repository the
    # rest of the service came from, and a tag pushed here is the only
    # thing that changes what a user gets.
    #
    # ⚠️ Nothing is published to PyPI. Every target, without exception,
    # resolves to a Release asset URL; there is no name-based fallback
    # that could quietly resolve to somebody else's project.
    "https://github.com/DrAlexLiu/moonraker-pinozcam/releases/download")

DISTS = {
    "armhf": "pinozcam-runtime",
    "aarch64": "pinozcam-runtime",
    "x86_64": "pinozcam-runtime",
    "rknn3566": "pinozcam-runtime-rknn3566",
    "rknn3576": "pinozcam-runtime-rknn3576",
    "rknn3588": "pinozcam-runtime-rknn3588",
    "awnn": "pinozcam-runtime-a733",
    "awnnt527": "pinozcam-runtime-t527",
    "bpu_x5": "pinozcam-runtime-rdkx5",
    "acl": "pinozcam-runtime-ascend310b",
    "vulkan": "pinozcam-runtime-gpu",
    "vulkan_x86_64": "pinozcam-runtime-gpu",
}

WHEELS = {
    "armhf": "pinozcam_runtime-%s-py3-none-manylinux2014_armv7l.whl",
    "aarch64": "pinozcam_runtime-%s-py3-none-manylinux2014_aarch64.whl",
    "x86_64": "pinozcam_runtime-%s-py3-none-manylinux2014_x86_64.whl",
    "rknn3566": "pinozcam_runtime_rknn3566-%s-py3-none-linux_aarch64.whl",
    "rknn3576": "pinozcam_runtime_rknn3576-%s-py3-none-linux_aarch64.whl",
    "rknn3588": "pinozcam_runtime_rknn3588-%s-py3-none-linux_aarch64.whl",
    "awnn": "pinozcam_runtime_a733-%s-py3-none-linux_aarch64.whl",
    "awnnt527": "pinozcam_runtime_t527-%s-py3-none-linux_aarch64.whl",
    "bpu_x5": "pinozcam_runtime_rdkx5-%s-py3-none-linux_aarch64.whl",
    "acl": "pinozcam_runtime_ascend310b-%s-py3-none-linux_aarch64.whl",
    "vulkan": "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_aarch64.whl",
    "vulkan_x86_64":
        "pinozcam_runtime_gpu-%s-py3-none-manylinux_2_35_x86_64.whl",
}


def detect_target():
    """Return the runtime target for this machine, or None if unclear."""
    from moonraker_pinozcam import nozcam_backend as nb
    kind, chip = nb._resolve_backend("auto")
    target = nb._runtime_target(kind, chip)
    return kind, chip, target


def requirement_for(target):
    """Return a pip requirement string for this target."""
    if DISTS.get(target) is None:
        return None
    wheel = WHEELS[target] % RUNTIME_VERSION
    return "%s/%s/%s" % (RELEASE_BASE, RUNTIME_VERSION, wheel)


def main(argv):
    if len(argv) < 2:
        print("usage: install_runtime.py <path-to-pip>", file=sys.stderr)
        return 2
    pip = argv[1]

    try:
        kind, chip, target = detect_target()
    except Exception as exc:                                 # noqa: BLE001
        print("  ! could not detect hardware: %s" % exc, file=sys.stderr)
        return 1

    print("  detected backend : %s%s" % (kind, " (%s)" % chip if chip else ""))
    print("  runtime target   : %s" % target)

    req = requirement_for(target)
    if req is None:
        print("  ! no runtime package is published for target %r" % target,
              file=sys.stderr)
        return 1

    # Show the source, because a direct-URL install is worth being visible:
    # a user debugging a failed install needs to know where it came from.
    print("  installing from  : %s"
          % ("PyPI" if req.startswith("pinozcam") else "GitHub Release"))
    rc = subprocess.call([pip, "install", "--upgrade", req])
    if rc != 0:
        print("  ! runtime install failed (pip exit %d)" % rc, file=sys.stderr)
        return rc

    # Verify by importing, not by trusting pip's exit code: a wheel can
    # install cleanly and still be the wrong architecture for this machine.
    try:
        from moonraker_pinozcam import nozcam_backend as nb
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bin_dir, model_dir, resolved = nb._runtime_directories(
            here, kind, chip)
        # "legacy-in-tree" means it fell back to a source checkout rather
        # than finding the installed package -- that would work here but
        # not for a normal user, so treat it as a failure.
        if resolved == "legacy-in-tree":
            print("  ! resolved to an in-tree checkout, not the installed "
                  "package", file=sys.stderr)
            return 1
        print("  runtime package  : %s" % resolved)
        print("  bin              : %s" % bin_dir)
        print("  models           : %s" % model_dir)
    except Exception as exc:                                 # noqa: BLE001
        print("  ! installed, but the runtime does not resolve: %s" % exc,
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
