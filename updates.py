"""New versions: found on GitHub, and installed over this copy of the app.

At most once a day, and only while Settings allows it, the app asks GitHub for
its latest release. Only that request is made; nothing about the person or
their mail goes with it.

An update is installed only when it checks out: the disk image matches the
size and SHA-256 GitHub gives for it, and the app inside has the same bundle
ID, says the version it should, and is signed with the same certificate as
this copy. Then this copy quits and a small script puts the new one in its
place and opens it.
"""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Tuple
from urllib.parse import urlsplit

REPO = "NickCysEDU/Mail-Manager"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"
#: How often to look, at most.
DAY = 24 * 60 * 60
#: Where a download may come from, after GitHub's redirects.
HOSTS = ("github.com", "objects.githubusercontent.com",
         "release-assets.githubusercontent.com")
#: Bigger than any build will be.
MOST_BYTES = 600 * 1024 * 1024


class UpdateError(Exception):
    """Why an update was not installed, in words for the person."""


@dataclass(frozen=True)
class Release:
    version: str
    notes: str = ""
    page: str = PAGE
    url: str = ""
    size: int = 0
    sha256: str = ""


def version_of(text: str) -> Tuple[int, ...]:
    """``"v1.10.2"`` as ``(1, 10, 2)``; as far as it reads as numbers."""
    parts = []
    for piece in (text or "").strip().lstrip("vV").split("."):
        digits = ""
        for character in piece:
            if not character.isdigit():
                break
            digits += character
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def newer(candidate: str, current: str) -> bool:
    """Whether ``candidate`` is a later version than ``current``."""
    found, mine = version_of(candidate), version_of(current)
    return bool(found) and bool(mine) and found > mine


def release_from(data: dict) -> Optional[Release]:
    """The release GitHub describes, with its disk image if it has one."""
    tag = str(data.get("tag_name") or "")
    if not version_of(tag) or data.get("draft") or data.get("prerelease"):
        return None
    image = next((asset for asset in data.get("assets") or ()
                  if str(asset.get("name", "")).lower().endswith(".dmg")),
                 None)
    sha256 = ""
    url = ""
    size = 0
    if image is not None:
        url = str(image.get("browser_download_url") or "")
        size = int(image.get("size") or 0)
        digest = str(image.get("digest") or "")
        if digest.lower().startswith("sha256:"):
            sha256 = digest.split(":", 1)[1].strip().lower()
    return Release(version=tag.lstrip("vV"), notes=str(data.get("body") or ""),
                   page=str(data.get("html_url") or PAGE), url=url,
                   size=size, sha256=sha256)


def fetch_latest(opener: Callable = urllib.request.urlopen,
                 timeout: float = 15.0) -> Optional[Release]:
    """The latest release, from GitHub. Raises OSError when it cannot ask."""
    import certs

    certs.ensure()
    request = urllib.request.Request(API, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Mail-Manager"})
    with opener(request, timeout=timeout) as response:
        data = json.loads(response.read(2 * 1024 * 1024).decode("utf-8"))
    return release_from(data) if isinstance(data, dict) else None


def due(settings, now: Optional[float] = None) -> bool:
    """Whether to look now: allowed, and a day since the last look."""
    if not getattr(settings, "check_updates", False):
        return False
    now = time.time() if now is None else now
    return now - float(getattr(settings, "update_checked", 0.0) or 0.0) >= DAY


def offered(release: Optional[Release], current: str, skipped: str) -> bool:
    """Whether to offer ``release``: newer, and not one already declined."""
    return (release is not None and newer(release.version, current)
            and release.version != (skipped or ""))


def running_app() -> Optional[Path]:
    """This copy's bundle, when it is a built app in a folder it may replace;
    None from source, from a disk image, or where it cannot write."""
    if not getattr(sys, "frozen", False):
        return None
    app = Path(sys.executable).resolve().parents[2]
    if app.suffix != ".app" or str(app).startswith("/Volumes/"):
        return None
    if not os.access(app.parent, os.W_OK) or not os.access(app, os.W_OK):
        return None
    return app


def certificate(app: Path) -> Optional[str]:
    """The SHA-256 of the certificate ``app`` is signed with, or None for an
    app signed without one."""
    with tempfile.TemporaryDirectory() as folder:
        prefix = Path(folder) / "certificate"
        try:
            subprocess.run(["codesign", "-d",
                            f"--extract-certificates={prefix}", str(app)],
                           capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            return None
        leaf = Path(f"{prefix}0")
        return (hashlib.sha256(leaf.read_bytes()).hexdigest()
                if leaf.is_file() else None)


def bundle_facts(app: Path) -> dict:
    """The bundle ID and version an app says it is."""
    try:
        with open(app / "Contents" / "Info.plist", "rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ValueError):
        return {}
    return {"id": str(info.get("CFBundleIdentifier") or ""),
            "version": str(info.get("CFBundleShortVersionString") or "")}


#: Put the new app where the old one was, once the old one has quit, and
#: open it. Paths come in as arguments, never as text in the script. If the
#: new one cannot be moved in, the old one goes back.
SWAP = """#!/bin/sh
pid="$1"; old="$2"; new="$3"; aside="$4"; opener="$5"
while kill -0 "$pid" 2>/dev/null; do sleep 0.2; done
if mv "$old" "$aside"; then
  if mv "$new" "$old"; then
    rm -rf "$aside"
  else
    mv "$aside" "$old"
  fi
fi
xattr -dr com.apple.quarantine "$old" 2>/dev/null
rm -rf "$(dirname "$new")"
"$opener" "$old"
"""


class Installer:
    """Download, check, and stage a release beside the app it replaces.

    ``stage`` and ``progress`` are told what is happening, for the window.
    Raises UpdateError with the reason when anything does not check out.
    """

    def __init__(self, release: Release, app: Path,
                 opener: Callable = urllib.request.urlopen,
                 hosts: Tuple[str, ...] = HOSTS,
                 stage: Callable[[str], None] = lambda words: None,
                 progress: Callable[[float], None] = lambda share: None):
        self.release = release
        self.app = Path(app)
        self.opener = opener
        self.hosts = hosts
        self.stage = stage
        self.progress = progress
        self.staging = self.app.parent / f".{self.app.stem} update"
        self.cancelled = False

    def run(self) -> Path:
        """Everything up to the swap; the script that does the swap."""
        mine = certificate(self.app)
        if not mine and not self.release.sha256:
            raise UpdateError("There is nothing to check this download "
                              "against, so it is not installed. Download it "
                              "from the release page instead.")
        if self.staging.exists():
            shutil.rmtree(self.staging, ignore_errors=True)
        self.staging.mkdir(parents=True)
        image = self.download()
        staged = self.unpack(image)
        self.check(staged, mine)
        script = self.staging / "swap.sh"
        script.write_text(SWAP, encoding="utf-8")
        return script

    def download(self) -> Path:
        release = self.release
        if urlsplit(release.url).scheme != "https" or not self._allowed(
                release.url):
            raise UpdateError("The download is not from GitHub.")
        self.stage("Downloading")
        import certs

        certs.ensure()
        target = self.staging / "update.dmg"
        digest = hashlib.sha256()
        got = 0
        request = urllib.request.Request(release.url,
                                         headers={"User-Agent": "Mail-Manager"})
        try:
            with self.opener(request, timeout=60) as response, \
                    open(target, "wb") as out:
                final = getattr(response, "geturl", lambda: release.url)()
                if not self._allowed(final):
                    raise UpdateError("The download was sent somewhere other "
                                      "than GitHub.")
                while True:
                    if self.cancelled:
                        raise UpdateError("Stopped.")
                    chunk = response.read(256 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > MOST_BYTES or (release.size and got > release.size):
                        raise UpdateError("The download is bigger than it "
                                          "should be.")
                    digest.update(chunk)
                    out.write(chunk)
                    if release.size:
                        self.progress(got / release.size)
        except OSError as exc:
            raise UpdateError(f"The download failed: {exc}") from exc
        if release.size and got != release.size:
            raise UpdateError("The download is not the size it should be.")
        if release.sha256 and digest.hexdigest() != release.sha256:
            raise UpdateError("The download does not match its checksum.")
        return target

    def unpack(self, image: Path) -> Path:
        """The app out of the disk image, beside the app it replaces."""
        self.stage("Opening the disk image")
        mount = Path(tempfile.mkdtemp(prefix="mm-update-"))
        try:
            attached = subprocess.run(
                ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen",
                 "-mountpoint", str(mount), str(image)],
                capture_output=True, timeout=180)
            if attached.returncode != 0:
                raise UpdateError("The disk image would not open.")
            try:
                apps = [path for path in mount.iterdir()
                        if path.suffix == ".app" and not path.is_symlink()]
                if len(apps) != 1:
                    raise UpdateError("The disk image does not hold one app.")
                staged = self.staging / self.app.name
                copied = subprocess.run(["ditto", str(apps[0]), str(staged)],
                                        capture_output=True, timeout=600)
                if copied.returncode != 0:
                    raise UpdateError("The app could not be copied out.")
            finally:
                subprocess.run(["hdiutil", "detach", str(mount), "-force"],
                               capture_output=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as exc:
            raise UpdateError(f"The disk image could not be read: {exc}") \
                from exc
        finally:
            shutil.rmtree(mount, ignore_errors=True)
        return staged

    def check(self, staged: Path, mine: Optional[str]) -> None:
        """The staged app is whole, and is this app at the new version."""
        self.stage("Checking it")
        verified = subprocess.run(
            ["codesign", "--verify", "--deep", "--strict", str(staged)],
            capture_output=True, timeout=300)
        if verified.returncode != 0:
            raise UpdateError("The new app's signature does not check out.")
        if mine and certificate(staged) != mine:
            raise UpdateError("The new app is not signed by whoever signed "
                              "this one.")
        old, new = bundle_facts(self.app), bundle_facts(staged)
        if not new or new.get("id") != old.get("id"):
            raise UpdateError("The download is a different app.")
        if version_of(new.get("version", "")) != version_of(
                self.release.version):
            raise UpdateError("The new app is not the version it should be.")

    def _allowed(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").lower()
        return host in self.hosts

    def launch(self, script: Path, opener: str = "open") -> None:
        """Start the swap, to happen when this process has quit."""
        aside = self.staging / f"{self.app.stem} (old).app"
        subprocess.Popen(
            ["/bin/sh", str(script), str(os.getpid()), str(self.app),
             str(self.staging / self.app.name), str(aside), opener],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True,
            close_fds=True)
