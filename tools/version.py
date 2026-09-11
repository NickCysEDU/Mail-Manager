#!/usr/bin/env python3
"""Read the app's version, or move it on.

The number lives in three files that have to agree: `models.py`, which the
window and the log read; `pyproject.toml`, which the packaging metadata reads;
and the disk image's Get Info panel. Three copies is three chances to ship a
build that says one thing in the corner of the window and another in the
Finder, which is exactly the kind of thing nobody notices until they are
trying to work out which version a bug report is about.

    ./dev version              # what it is now
    ./dev version patch        # 1.0.0 -> 1.0.1
    ./dev version minor        # 1.0.1 -> 1.1.0
    ./dev version major        # 1.1.0 -> 2.0.0
    ./dev version 1.4.2        # or say it outright

`MailManager.spec` imports the number rather than repeating it, so the disk
image follows on its own. A test checks the other two still agree, which is
what stops a half-finished bump from shipping.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: Every file that states the version, and the pattern that finds it.
SOURCES = (
    (ROOT / "models.py", re.compile(r'^(APP_VERSION\s*=\s*")([^"]+)(")', re.M)),
    (ROOT / "pyproject.toml", re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)),
)

SEMVER = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def current() -> str:
    from models import APP_VERSION
    return APP_VERSION


def stated() -> dict:
    """What each file says, so a disagreement can be named."""
    out = {}
    for path, pattern in SOURCES:
        found = pattern.search(path.read_text(encoding="utf-8"))
        out[path.name] = found.group(2) if found else None
    return out


def bumped(version: str, part: str) -> str:
    found = SEMVER.match(version)
    if not found:
        raise ValueError(f"{version!r} is not major.minor.patch")
    major, minor, patch = (int(n) for n in found.groups())
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write(version: str) -> list:
    if not SEMVER.match(version):
        raise ValueError(f"{version!r} is not major.minor.patch")
    touched = []
    for path, pattern in SOURCES:
        text = path.read_text(encoding="utf-8")
        updated, count = pattern.subn(rf"\g<1>{version}\g<3>", text, count=1)
        if not count:
            raise SystemExit(f"could not find the version in {path.name}")
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            touched.append(path.name)
    return touched


def released() -> set:
    """Versions that already have a git tag, so one is not reused."""
    try:
        out = subprocess.run(["git", "tag", "--list"], cwd=ROOT,
                             capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return set()
    return {line.lstrip("v").strip() for line in out.stdout.splitlines() if line.strip()}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="version", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("what", nargs="?",
                        help="major, minor, patch, or a version outright. "
                             "Omit to print the current one.")
    args = parser.parse_args(argv)

    said = stated()
    disagree = len(set(said.values())) > 1
    if not args.what:
        import buildinfo
        print(buildinfo.full())
        for name, value in said.items():
            print(f"  {name:18} {value}")
        if disagree:
            print("\nThese disagree. Run ./dev version <new> to set them together.",
                  file=sys.stderr)
            return 1
        if current() in released():
            print(f"\nv{current()} is already tagged. Bump before building a "
                  "release.", file=sys.stderr)
        return 0

    if disagree:
        print(f"The files disagree {said}; setting them all.", file=sys.stderr)
    target = (bumped(current(), args.what)
              if args.what in ("major", "minor", "patch") else args.what)
    if target in released():
        print(f"v{target} is already tagged. Pick another.", file=sys.stderr)
        return 1
    touched = write(target)
    print(f"{current()} -> {target}   ({', '.join(touched) or 'no change'})")
    print("MailManager.spec reads it from models.py, so the disk image follows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
