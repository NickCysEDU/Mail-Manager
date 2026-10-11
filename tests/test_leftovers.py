"""What earlier runs left in the temporary folder, cleared at the next start.

A run that crashed or was made to quit could not clear its own folders, and
they stayed until macOS got round to clearing old files out.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import leftovers

ROOT = Path(__file__).resolve().parents[1]


def _gone_pid() -> int:
    """A process that has finished, so nothing runs as it."""
    done = subprocess.Popen([sys.executable, "-c", "pass"])
    done.wait()
    return done.pid


def _folder(place: Path, name: str, owner=None, age: float = 0.0) -> Path:
    folder = place / name
    folder.mkdir()
    (folder / "part.mp3").write_bytes(b"a copy of a part")
    if owner is not None:
        (folder / leftovers.CLAIM).write_text(str(owner))
    if age:
        when = time.time() - age
        os.utime(folder, (when, when))
    return folder


class TestTheSweep:
    def test_a_folder_whose_process_has_gone_goes(self, tmp_path):
        left = _folder(tmp_path, "mm-attach-left", owner=_gone_pid())
        assert leftovers.sweep(tmp_path) == 1
        assert not left.exists()

    def test_a_running_processs_folder_stays(self, tmp_path):
        mine = _folder(tmp_path, "mm-attach-mine", owner=os.getpid())
        # Somebody else's process, which the sweep may not even signal.
        launchd = _folder(tmp_path, "mm-attach-system", owner=1)
        assert leftovers.sweep(tmp_path) == 0
        assert mine.exists() and launchd.exists()

    def test_an_unclaimed_folder_goes_once_it_is_an_hour_old(self, tmp_path):
        old = _folder(tmp_path, "mm-attach-old",
                      age=leftovers.UNCLAIMED_FOR + 60)
        new = _folder(tmp_path, "mm-attach-new", age=60)
        update = _folder(tmp_path, "mm-update-old",
                         age=leftovers.UNCLAIMED_FOR + 60)
        assert leftovers.sweep(tmp_path) == 2
        assert not old.exists() and not update.exists()
        assert new.exists()

    def test_nothing_else_is_touched(self, tmp_path):
        other = _folder(tmp_path, "someone-else", owner=_gone_pid(),
                        age=leftovers.UNCLAIMED_FOR + 60)
        loose = tmp_path / "mm-attach-file"
        loose.write_bytes(b"not a folder")
        kept = tmp_path / "kept"
        kept.mkdir()
        (kept / leftovers.CLAIM).write_text(str(_gone_pid()))
        (tmp_path / "mm-attach-link").symlink_to(kept)
        assert leftovers.sweep(tmp_path) == 0
        assert other.exists() and loose.exists()
        assert (kept / leftovers.CLAIM).exists()

    def test_a_viewer_claims_its_folder(self, qtbot):
        from attachment_view import AttachmentViewer

        viewer = AttachmentViewer([], "")
        qtbot.addWidget(viewer)
        try:
            claim = viewer._temp / leftovers.CLAIM
            assert claim.read_text() == str(os.getpid())
        finally:
            viewer._sweep()
        assert not viewer._temp.exists()


class TestTheAppClearsThemAsItStarts:
    @pytest.mark.timeout(120)
    def test_what_a_crashed_run_left_is_gone_soon_after_a_start(
            self, tmp_path):
        """The app itself, from source, in a temporary folder of its own."""
        temp = tmp_path / "temp"
        temp.mkdir()
        left = _folder(temp, "mm-attach-crashed", owner=_gone_pid())
        env = dict(os.environ, TMPDIR=str(temp), QT_QPA_PLATFORM="offscreen",
                   ICLOUD_TRIAGE_HOME=str(tmp_path / "home"))
        app = subprocess.Popen([sys.executable, str(ROOT / "main.py"),
                                "--demo"], cwd=ROOT, env=env,
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.PIPE, text=True)
        try:
            end = time.monotonic() + 60
            while left.exists() and time.monotonic() < end:
                if app.poll() is not None:
                    break
                time.sleep(0.2)
            assert app.poll() is None, (
                f"the app ended first: {app.stderr.read()[-1500:]}")
            assert not left.exists(), (
                "what the crashed run left is still there")
        finally:
            app.kill()
            app.wait(10)
