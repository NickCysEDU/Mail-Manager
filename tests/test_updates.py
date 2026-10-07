"""New versions: found, offered, checked, and put in place."""

from __future__ import annotations

import hashlib
import io
import json
import plistlib
import subprocess
import time
import sys
from pathlib import Path

import pytest

import updates

MAC = sys.platform == "darwin"
DMG_URL = "https://github.com/example/app/releases/download/v1.1.0/App.dmg"


class TestVersions:
    @pytest.mark.parametrize("text, parts", [
        ("1.2.0", (1, 2, 0)), ("v1.10.2", (1, 10, 2)), ("2", (2,)),
        ("1.3.0-beta", (1, 3, 0)), ("", ()), ("latest", ())])
    def test_read(self, text, parts):
        assert updates.version_of(text) == parts

    @pytest.mark.parametrize("candidate, current, later", [
        ("1.3.0", "1.2.0", True), ("v1.10.0", "1.9.9", True),
        ("1.2.0", "1.2.0", False), ("1.1.9", "1.2.0", False),
        ("nonsense", "1.2.0", False)])
    def test_newer(self, candidate, current, later):
        assert updates.newer(candidate, current) is later


GITHUB = {
    "tag_name": "v1.3.0", "html_url": "https://github.com/example/app/r",
    "body": "## New in 1.3.0\n\nThings.\n\n## Installing\n\nDrag it.",
    "assets": [
        {"name": "notes.txt", "browser_download_url": "https://x/notes"},
        {"name": "Mail.Manager.dmg", "size": 1234,
         "browser_download_url": DMG_URL, "digest": "sha256:ABCDEF"}],
}


class TestTheRelease:
    def test_it_is_read_from_what_github_says(self):
        release = updates.release_from(GITHUB)
        assert release.version == "1.3.0"
        assert release.url == DMG_URL and release.size == 1234
        assert release.sha256 == "abcdef"
        assert release.page == GITHUB["html_url"]

    def test_drafts_and_prereleases_are_not_offered(self):
        assert updates.release_from({**GITHUB, "draft": True}) is None
        assert updates.release_from({**GITHUB, "prerelease": True}) is None

    def test_one_without_a_disk_image_has_only_its_page(self):
        release = updates.release_from({**GITHUB, "assets": []})
        assert release.url == "" and release.version == "1.3.0"

    def test_it_is_asked_for_and_nothing_else_is_sent(self):
        sent = []

        def opener(request, timeout):
            sent.append(request)
            return io.BytesIO(json.dumps(GITHUB).encode())

        release = updates.fetch_latest(opener=opener)
        assert release.version == "1.3.0"
        request, = sent
        assert request.full_url == updates.API
        assert request.data is None
        assert set(request.headers) == {"Accept", "User-agent"}

    def test_once_a_day_and_only_when_allowed(self):
        class Settings:
            check_updates = True
            update_checked = 1000.0

        assert not updates.due(Settings, now=1000.0 + updates.DAY - 1)
        assert updates.due(Settings, now=1000.0 + updates.DAY)
        Settings.check_updates = False
        assert not updates.due(Settings, now=1e12)

    def test_a_skipped_version_is_not_offered_again(self):
        release = updates.Release(version="1.3.0")
        assert updates.offered(release, "1.2.0", "")
        assert not updates.offered(release, "1.2.0", "1.3.0")
        assert not updates.offered(release, "1.3.0", "")
        assert not updates.offered(None, "1.2.0", "")


def _app(folder: Path, version: str, ident: str = "com.example.updatetest",
         says: str = "old") -> Path:
    app = folder / "Test App.app"
    macos = app / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    with open(app / "Contents" / "Info.plist", "wb") as handle:
        plistlib.dump({"CFBundleIdentifier": ident,
                       "CFBundleShortVersionString": version,
                       "CFBundleExecutable": "Test App",
                       "CFBundlePackageType": "APPL"}, handle)
    program = macos / "Test App"
    program.write_text(f"#!/bin/sh\necho {says}\n")
    program.chmod(0o755)
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)],
                   check=True, capture_output=True)
    return app


def _image(app: Path, out: Path) -> bytes:
    # A hosted runner's hdiutil refuses now and then, "Resource busy", and
    # is fine a moment later.
    for _ in range(5):
        done = subprocess.run(
            ["hdiutil", "create", "-srcfolder", str(app.parent), "-format",
             "UDZO", "-ov", "-volname", "Test", str(out)],
            capture_output=True, text=True)
        if done.returncode == 0:
            return out.read_bytes()
        time.sleep(2.0)
    raise AssertionError(f"hdiutil would not make the image: "
                         f"{done.stderr.strip()[-500:]}")


class _Served(io.BytesIO):
    def __init__(self, data: bytes, url: str) -> None:
        super().__init__(data)
        self._url = url

    def geturl(self) -> str:
        return self._url


def _opener(data: bytes, landed: str = DMG_URL):
    return lambda request, timeout=None: _Served(data, landed)


@pytest.fixture(scope="module")
def image(tmp_path_factory):
    """The new version's disk image, made once."""
    if not MAC:
        pytest.skip("disk images and code signing are macOS's")
    folder = tmp_path_factory.mktemp("build")
    return _image(_app(folder, "1.1.0", says="new"), folder.parent / "App.dmg")


@pytest.fixture
def made(image, tmp_path):
    installed = tmp_path / "Applications"
    installed.mkdir()
    old = _app(installed, "1.0.0")
    release = updates.Release(version="1.1.0", url=DMG_URL, size=len(image),
                              sha256=hashlib.sha256(image).hexdigest())
    return old, image, release, tmp_path


@pytest.mark.timeout(240)
class TestInstalling:
    def test_it_downloads_checks_stages_and_swaps(self, made):
        old, data, release, _tmp = made
        installer = updates.Installer(release, old, opener=_opener(data))
        script = installer.run()
        staged = installer.staging / old.name
        assert updates.bundle_facts(staged)["version"] == "1.1.0"
        assert updates.bundle_facts(old)["version"] == "1.0.0"
        # The swap, as it runs after this process has gone: a process that
        # has already exited stands in for it, and nothing is opened.
        gone = subprocess.Popen(["true"])
        gone.wait()
        subprocess.run(["/bin/sh", str(script), str(gone.pid), str(old),
                        str(staged), str(installer.staging / "aside.app"),
                        "true"], check=True, timeout=60)
        assert updates.bundle_facts(old)["version"] == "1.1.0"
        said = subprocess.run([str(old / "Contents" / "MacOS" / "Test App")],
                              capture_output=True, text=True).stdout
        assert said.strip() == "new"
        assert not installer.staging.exists()

    def test_if_the_new_one_cannot_go_in_the_old_one_stays(self, made):
        old, _data, _release, tmp = made
        script = tmp / "swap.sh"
        script.write_text(updates.SWAP)
        gone = subprocess.Popen(["true"])
        gone.wait()
        subprocess.run(["/bin/sh", str(script), str(gone.pid), str(old),
                        str(tmp / "missing" / "Test App.app"),
                        str(tmp / "aside.app"), "true"], timeout=60)
        assert updates.bundle_facts(old)["version"] == "1.0.0"
        assert not (tmp / "aside.app").exists()

    def test_a_download_that_does_not_match_its_checksum(self, made):
        old, data, release, _tmp = made
        wrong = updates.Release(**{**release.__dict__, "sha256": "0" * 64})
        with pytest.raises(updates.UpdateError, match="checksum"):
            updates.Installer(wrong, old, opener=_opener(data)).run()
        assert updates.bundle_facts(old)["version"] == "1.0.0"

    def test_a_download_cut_short(self, made, monkeypatch):
        """Where the app's certificate is what is checked and GitHub gave no
        checksum, the size is what says it all arrived."""
        old, data, release, _tmp = made
        monkeypatch.setattr(updates, "certificate", lambda app: "same")
        bare = updates.Release(**{**release.__dict__, "sha256": ""})
        with pytest.raises(updates.UpdateError, match="not the size"):
            updates.Installer(bare, old, opener=_opener(data[:-4096])).run()

    def test_a_download_sent_elsewhere(self, made):
        old, data, release, _tmp = made
        with pytest.raises(updates.UpdateError, match="other than GitHub"):
            updates.Installer(release, old, opener=_opener(
                data, landed="https://example.net/App.dmg")).run()

    def test_a_download_not_over_https_from_github(self, made):
        old, data, release, _tmp = made
        for url in ("http://github.com/x/App.dmg",
                    "https://example.net/App.dmg"):
            moved = updates.Release(**{**release.__dict__, "url": url})
            with pytest.raises(updates.UpdateError, match="not from GitHub"):
                updates.Installer(moved, old, opener=_opener(data)).run()

    def test_a_different_app(self, made, tmp_path):
        old, _data, release, _tmp = made
        other = tmp_path / "other"
        other.mkdir()
        data = _image(_app(other, "1.1.0", ident="com.example.other"),
                      tmp_path / "Other.dmg")
        release = updates.Release(version="1.1.0", url=DMG_URL,
                                  size=len(data),
                                  sha256=hashlib.sha256(data).hexdigest())
        with pytest.raises(updates.UpdateError, match="different app"):
            updates.Installer(release, old, opener=_opener(data)).run()

    def test_the_wrong_version(self, made):
        old, data, release, _tmp = made
        claimed = updates.Release(**{**release.__dict__, "version": "1.2.0"})
        with pytest.raises(updates.UpdateError, match="not the version"):
            updates.Installer(claimed, old, opener=_opener(data)).run()

    def test_signed_by_someone_else(self, made, monkeypatch):
        old, data, release, _tmp = made
        monkeypatch.setattr(updates, "certificate",
                            lambda app: "mine" if app == old else "theirs")
        with pytest.raises(updates.UpdateError, match="not signed by"):
            updates.Installer(release, old, opener=_opener(data)).run()

    def test_nothing_to_check_it_against(self, made):
        old, data, release, _tmp = made
        bare = updates.Release(**{**release.__dict__, "sha256": ""})
        with pytest.raises(updates.UpdateError, match="nothing to check"):
            updates.Installer(bare, old, opener=_opener(data)).run()


class TestTheWindow:
    def test_where_it_cannot_install_it_sends_you_to_the_page(self, qtbot,
                                                               monkeypatch):
        from PySide6.QtGui import QDesktopServices

        from update_dialog import UpdateDialog

        opened = []
        monkeypatch.setattr(QDesktopServices, "openUrl",
                            lambda url: opened.append(url.toString()) or True)
        release = updates.release_from(GITHUB)
        dialog = UpdateDialog(release, app=None)
        qtbot.addWidget(dialog)
        assert dialog.update_button.text() == "Download…"
        dialog.update_button.click()
        assert opened == [release.page]

    def test_skip_says_which(self, qtbot):
        from update_dialog import UpdateDialog

        dialog = UpdateDialog(updates.release_from(GITHUB), app=None)
        qtbot.addWidget(dialog)
        skipped = []
        dialog.skipped.connect(skipped.append)
        dialog.skip_button.click()
        assert skipped == ["1.3.0"]

    def test_only_what_is_new_is_shown(self):
        from update_dialog import notes

        assert notes(GITHUB["body"]) == "## New in 1.3.0\n\nThings."
        assert notes("Intro.\n\n" + GITHUB["body"]) == \
            "## New in 1.3.0\n\nThings."
        assert notes("Only words.") == "Only words."


class TestTheMainWindow:
    @staticmethod
    def _window(**settings):
        class Settings:
            check_updates = True
            update_checked = 0.0
            skipped_version = ""
            saved = 0

            def save(self):
                self.saved += 1

        from PySide6.QtCore import QObject

        import gui

        class Window(QObject):
            demo = dry_run = False
            _update_look = None
            _update_looked = gui.MainWindow._update_looked
            _update_found = gui.MainWindow._update_found
            _update_failed = gui.MainWindow._update_failed

        window = Window()
        window.settings = Settings()
        for name, value in settings.items():
            setattr(window.settings, name, value)
        return window

    def test_the_suite_never_asks_github(self, qapp):
        """conftest answers the update check at once, so no test that leaves
        a window open reaches the network."""
        import update_dialog

        look = update_dialog.Look()
        said = []
        look.failed.connect(said.append)
        look.run()
        assert said == ["no network in tests"]

    def test_shutting_down_leaves_no_check_running(self, qapp, monkeypatch):
        """Destroying a running QThread aborts the process, so quitting
        during a slow check waits for it or lets it go."""
        import threading

        import gui
        import update_dialog
        import widgets
        from config import InMemoryCredentialStore, Settings

        release = threading.Event()
        monkeypatch.setattr(update_dialog.Look, "run",
                            lambda self: release.wait(10))
        window = gui.MainWindow(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        try:
            window._look_for_updates(by_hand=True)
            look = window._update_look
            assert look.isRunning()
            monkeypatch.setattr(update_dialog.Look, "wait",
                                lambda self, *_args: False)
            window.shutdown()
            assert look.parent() is None, "a running check is still the window's"
            assert look in widgets._ABANDONED
        finally:
            release.set()
            update_dialog.QThread.wait(look, 5000)
            window.close()

    def test_shutting_down_stops_a_download(self, qapp, tmp_path):
        """An update being downloaded is told to stop, and waited for."""
        import time

        import gui
        import updates
        import widgets
        from config import InMemoryCredentialStore, Settings
        from update_dialog import Fetch, UpdateDialog

        class Slow:
            cancelled = False

            def run(self):
                while not self.cancelled:
                    time.sleep(0.01)
                raise updates.UpdateError("cancelled")

        window = gui.MainWindow(Settings(icloud_email="you@icloud.example"),
                                InMemoryCredentialStore())
        try:
            dialog = UpdateDialog(updates.Release(version="99.0.0",
                                                  url="https://example.com/x"),
                                  app=tmp_path / "Mail Manager.app",
                                  parent=window)
            fetch = Fetch(Slow(), dialog)
            dialog._fetch = fetch
            window._update_dialog = dialog
            fetch.start()
            assert fetch.isRunning()
            window.shutdown()
            assert fetch.installer.cancelled
            assert not fetch.isRunning(), "the download was left running"
            assert fetch not in widgets._ABANDONED
        finally:
            fetch.installer.cancelled = True
            fetch.wait(5000)
            window.close()

    def test_it_looks_only_when_due(self, qapp, monkeypatch):
        import gui
        import update_dialog

        started = []
        monkeypatch.setattr(update_dialog.Look, "start",
                            lambda self: started.append(self))
        window = self._window(update_checked=1e18)
        gui.MainWindow._look_for_updates(window)
        assert started == []
        gui.MainWindow._look_for_updates(window, by_hand=True)
        assert len(started) == 1

    def test_never_in_the_demo(self, qapp, monkeypatch):
        import gui
        import update_dialog

        started = []
        monkeypatch.setattr(update_dialog.Look, "start",
                            lambda self: started.append(self))
        window = self._window()
        window.demo = True
        gui.MainWindow._look_for_updates(window, by_hand=True)
        assert started == []

    def test_up_to_date_is_said_only_when_asked(self, qapp, monkeypatch):
        import gui

        said = []
        monkeypatch.setattr(gui.QMessageBox, "information",
                            lambda *args: said.append(args[1]))
        window = self._window()
        same = updates.Release(version=gui.APP_VERSION)
        gui.MainWindow._update_found(window, same, by_hand=False)
        assert said == [] and window.settings.update_checked > 0
        gui.MainWindow._update_found(window, same, by_hand=True)
        assert said == ["Up to date"]


class TestTheImageIsTriedAgain:
    """hdiutil refuses now and then on a busy machine and is fine a moment
    later: one refusal does not fail an update, three do, with its words."""

    @staticmethod
    def _refusing(monkeypatch, refusals: int):
        import subprocess as real

        calls = []
        done = real.run

        def run(command, *args, **kwargs):
            if command[:2] == ["hdiutil", "attach"]:
                calls.append(command)
                if len(calls) <= refusals:
                    return real.CompletedProcess(command, 1, b"", b"hdiutil: attach failed - Resource busy")
            return done(command, *args, **kwargs)

        monkeypatch.setattr(updates.subprocess, "run", run)
        monkeypatch.setattr(updates.time, "sleep", lambda _s: None)
        return calls

    def test_one_refusal_is_tried_again(self, made, monkeypatch):
        old, data, release, _tmp = made
        calls = self._refusing(monkeypatch, 1)
        updates.Installer(release, old, opener=_opener(data)).run()
        assert len(calls) == 2
        assert (old / "Contents" / "MacOS" / "Test App").exists()

    def test_three_refusals_say_why(self, made, monkeypatch):
        old, data, release, _tmp = made
        calls = self._refusing(monkeypatch, 3)
        with pytest.raises(updates.UpdateError, match="Resource busy"):
            updates.Installer(release, old, opener=_opener(data)).run()
        assert len(calls) == 3
