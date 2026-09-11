#!/usr/bin/env python3
"""Make every compiled dependency in a virtualenv universal2.

PySide6 and shiboken6 already ship both architectures in one file. A couple of
the smaller Rust extensions - jiter and pydantic_core, both pulled in by the
Anthropic SDK - publish one wheel per architecture instead, so pip installs
whichever matches the machine doing the install. PyInstaller then refuses to
build a universal app, because one of the pieces only exists in half of it.

This downloads the missing half of each and joins them with lipo, which is
exactly what a universal2 wheel is. Run it after installing requirements:

    python tools/make_universal_deps.py

It is safe to run twice; anything already universal is left alone.
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import site
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from typing import List, Optional, Tuple

#: The macOS platform tags a single-architecture wheel can carry, newest
#: first. More than one because a project picks a deployment target and
#: sticks to it, and they do not all pick the same: cffi 2.1.1 publishes
#: macosx_10_15_x86_64, which a lookup for macosx_10_12_x86_64 answers with
#: cffi 1.17.1 instead, and lipo then joins two different versions.
TAGS = {
    "arm64": (
        "macosx_14_0_arm64", "macosx_11_0_arm64", "macosx_10_9_universal2",
    ),
    "x86_64": (
        "macosx_14_0_x86_64", "macosx_11_0_x86_64", "macosx_10_15_x86_64",
        "macosx_10_12_x86_64", "macosx_10_9_universal2",
    ),
}


def architectures(path: pathlib.Path) -> List[str]:
    """Which architectures a Mach-O file contains."""
    try:
        out = subprocess.run(["lipo", "-archs", str(path)],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return out.stdout.split() if out.returncode == 0 else []


def site_dirs() -> List[pathlib.Path]:
    paths = [pathlib.Path(p) for p in site.getsitepackages()]
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        paths.append(pathlib.Path(purelib))
    return [p for p in dict.fromkeys(paths) if p.is_dir()]


def single_arch_modules() -> List[Tuple[pathlib.Path, str]]:
    """Every compiled module that is missing an architecture."""
    found = []
    for root in site_dirs():
        for so in sorted(root.rglob("*.so")):
            archs = architectures(so)
            if len(archs) == 1:
                found.append((so, archs[0]))
    return found


def distribution_for(path: pathlib.Path) -> Optional[str]:
    """The installed distribution a file belongs to."""
    from importlib import metadata

    target = str(path.resolve())
    for dist in metadata.distributions():
        for file in dist.files or ():
            try:
                if str(pathlib.Path(dist.locate_file(file)).resolve()) == target:
                    return dist.metadata["Name"]
            except (OSError, ValueError):
                continue
    # Fall back to the top-level package name, which is right often enough.
    return path.parent.name or None


def installed_version(name: str) -> Optional[str]:
    from importlib import metadata
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def fetch_other_half(name: str, want: str, into: pathlib.Path) -> Optional[pathlib.Path]:
    """Download `name`'s wheel for the `want` architecture.

    Pinned to the version already installed. Taking whatever is newest looks
    like it works and then fails only on the other architecture: pydantic
    checks that its compiled core is the exact version it expects, so a
    mismatched slice passes every test run on the build machine and breaks on
    everybody else's.
    """
    tags = TAGS.get(want)
    if not tags:
        return None
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    exact = installed_version(name)
    # Pinned to the installed version, always. An unpinned download of the
    # other half is how two different versions get joined into one file.
    requirement = f"{name}=={exact}" if exact else name
    for tag in tags:
        command = [
            sys.executable, "-m", "pip", "download", "--no-deps", "--quiet",
            "--only-binary=:all:", "--platform", tag,
            "--python-version", version, "--dest", str(into), requirement,
        ]
        if subprocess.run(command, capture_output=True, text=True).returncode != 0:
            continue
        wheels = sorted(into.glob("*.whl"))
        if wheels:
            return wheels[-1]
    return None


def merge(target: pathlib.Path, other: pathlib.Path) -> bool:
    """lipo two slices into one file, in place."""
    joined = target.with_suffix(target.suffix + ".universal")
    result = subprocess.run(
        ["lipo", "-create", str(target), str(other), "-output", str(joined)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"    lipo failed: {result.stderr.strip()}", file=sys.stderr)
        joined.unlink(missing_ok=True)
        return False
    shutil.move(str(joined), str(target))
    # Joining invalidates the signature each slice arrived with.
    subprocess.run(["codesign", "-f", "-s", "-", str(target)], capture_output=True)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="report what is single-architecture and stop")
    args = parser.parse_args()

    if sys.platform != "darwin":
        print("Only meaningful on macOS.", file=sys.stderr)
        return 0

    pending = single_arch_modules()
    if not pending:
        print("Every compiled module is already universal2.")
        return 0

    print(f"{len(pending)} module(s) carry one architecture:")
    for path, arch in pending:
        print(f"  {arch:<7} {path.name}")
    if args.check:
        return 1

    failures = 0
    with tempfile.TemporaryDirectory() as work:
        for path, arch in pending:
            want = "x86_64" if arch == "arm64" else "arm64"
            name = distribution_for(path)
            if not name:
                print(f"  could not identify the package for {path.name}")
                failures += 1
                continue
            pinned = installed_version(name)
            print(f"  {name}: fetching the {want} half"
                  + (f" of {pinned}" if pinned else "") + "…")
            into = pathlib.Path(work) / name
            into.mkdir(parents=True, exist_ok=True)
            wheel = fetch_other_half(name, want, into)
            if wheel is None:
                print(f"    no {want} wheel published for {name}")
                failures += 1
                continue
            extracted = into / "unpacked"
            with zipfile.ZipFile(wheel) as archive:
                archive.extractall(extracted)
            twin = next((p for p in extracted.rglob(path.name)), None)
            if twin is None:
                print(f"    {wheel.name} does not contain {path.name}")
                failures += 1
                continue
            if merge(path, twin):
                print(f"    {path.name} -> {' + '.join(architectures(path))}")
            else:
                failures += 1

    remaining = single_arch_modules()
    if remaining:
        print(f"\n{len(remaining)} module(s) are still single-architecture.")
        return 1
    print("\nEvery compiled module is now universal2.")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
