"""Which build this is, in one line, for when somebody reports a bug.

A version number alone does not identify a build during development: every
change between releases carries the same one. The commit does, so it is read
from git when running from a checkout and baked in at build time when not.
"""

from __future__ import annotations

import datetime as _datetime
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Optional

from models import APP_VERSION

#: Written by the build script into the frozen app, where there is no git.
STAMP_FILE = "BUILD_STAMP"


def _root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


@lru_cache(maxsize=1)
def _baked() -> str:
    """What the build script recorded, if this is a built app.

    Only a frozen app is asked. Building leaves a stamp in the checkout, and
    reading it from a development run would report whichever commit was last
    built rather than the one actually running - wrong in the About box and
    wrong in every bug report made from a checkout afterwards.
    """
    if not getattr(sys, "frozen", False):
        return ""
    try:
        return (_root() / STAMP_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


@lru_cache(maxsize=1)
def _from_git() -> str:
    """The current commit, when running from a checkout. Never raises."""
    if getattr(sys, "frozen", False):
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(Path(__file__).resolve().parent),
             "describe", "--always", "--dirty=+", "--abbrev=7"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def commit() -> str:
    """The short commit this build came from, or empty if unknown."""
    return _baked().split()[0] if _baked() else _from_git()


def built_on() -> str:
    """The date the app was built, when it was built rather than run."""
    parts = _baked().split()
    return parts[1] if len(parts) > 1 else ""


def short() -> str:
    """One short line for a corner of the window: "1.0.0 · a1b2c3d"."""
    reference = commit()
    return f"{APP_VERSION} · {reference}" if reference else APP_VERSION


def full() -> str:
    """Everything known about this build, for About and bug reports."""
    bits = [f"{APP_VERSION}"]
    reference = commit()
    if reference:
        bits.append(f"build {reference}")
    when = built_on()
    if when:
        bits.append(f"built {when}")
    bits.append(f"Python {sys.version.split()[0]}")
    bits.append("Apple silicon" if _machine() == "arm64"
                else "Intel" if _machine() == "x86_64" else _machine())
    return " · ".join(bits)


def _machine() -> str:
    """Which slice of a universal binary is actually running."""
    import platform

    return platform.machine()


def write_stamp(target: Path, commit_reference: str = "") -> str:
    """Record the build for a frozen app. Used by the build script."""
    reference = commit_reference or _from_git() or "unknown"
    when = _datetime.date.today().isoformat()
    line = f"{reference} {when}\n"
    target.write_text(line, encoding="utf-8")
    return line.strip()


if __name__ == "__main__":  # pragma: no cover - build-script entry point
    destination = Path(os.environ.get("BUILD_STAMP_PATH", STAMP_FILE))
    print(write_stamp(destination))
