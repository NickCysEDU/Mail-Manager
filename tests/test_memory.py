"""Decoding and analysing a track gives all of its memory back.

A finished analysis kept its decoder alive through the decoder's own signal
connections, and the decoder kept the whole decoded track: one process
decoding track after track grew by every one of them.
"""

from __future__ import annotations

import gc
import math
import struct
import time
import wave
import weakref

import pytest


def _track(path, seconds=3.0, rate=48000):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        frames = bytearray()
        for index in range(int(seconds * rate)):
            beat = (index % (rate // 2)) < rate // 50
            value = int(12000 * math.sin(2 * math.pi * 110 * index / rate)
                        * (1.0 if beat else 0.2))
            frames += struct.pack("<hh", value, value)
        handle.writeframes(bytes(frames))
    return path


def _decode(qapp, path, timeout=60.0):
    """Decode and analyse ``path`` to the end, as a batch tool does, without
    cancelling; the handle, and weak references to what it decoded."""
    import attachment_audio

    got = {}
    handle = attachment_audio.decode(
        str(path), lambda r: got.setdefault("done", r),
        lambda d: got.setdefault("failed", d),
        on_elements=lambda k: got.setdefault("elements", k))
    started = time.monotonic()
    samples = None
    while time.monotonic() - started < timeout:
        qapp.processEvents()
        thread = getattr(handle, "_thread", None)
        if samples is None and thread is not None:
            samples = weakref.ref(thread._samples)
        if "failed" in got or ("done" in got and "elements" in got
                               and (thread is None or not thread.isRunning())):
            break
        time.sleep(0.005)
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.005)
    return handle, samples, got


class TestAFinishedAnalysisLetsGo:
    def test_the_decoded_track_is_freed(self, qapp, tmp_path):
        import attachment_audio

        handle, samples, got = _decode(qapp, _track(tmp_path / "t.wav"))
        assert "done" in got and samples is not None, got.keys()
        del handle
        for _ in range(10):
            qapp.processEvents()
            gc.collect()
        assert samples() is None, "the decoded track is still held"
        assert not attachment_audio._LIVE

    def test_or_while_the_handle_is_kept(self, qapp, tmp_path):
        """The window keeps the handle for as long as the track is shown;
        the decoded track need not be kept with it."""
        handle, samples, got = _decode(qapp, _track(tmp_path / "t.wav"))
        try:
            assert "done" in got
            for _ in range(10):
                qapp.processEvents()
                gc.collect()
            assert samples() is None, (
                "the decoded track is held as long as the window")
        finally:
            handle.cancel()

    def test_one_after_another_does_not_grow(self, qapp, tmp_path):
        path = _track(tmp_path / "t.wav")
        held = []
        for _round in range(3):
            handle, samples, _got = _decode(qapp, path)
            held.append(samples)
            del handle
            for _ in range(10):
                qapp.processEvents()
                gc.collect()
        assert all(ref is not None and ref() is None for ref in held)


class TestTooLongIsNotDecodedWhole:
    def test_it_stops_at_the_limit_and_says_why(self, qapp, tmp_path,
                                                monkeypatch):
        import attachment_audio

        monkeypatch.setattr(attachment_audio, "MAX_SECONDS", 1)
        path = _track(tmp_path / "long.wav", seconds=6.0)
        handle, samples, got = _decode(qapp, path, timeout=20.0)
        try:
            assert "failed" in got and "minutes" in got["failed"]
            assert "done" not in got
            assert samples is None, "it was analysed anyway"
        finally:
            handle.cancel()


@pytest.fixture(autouse=True)
def _no_workers_left():
    yield
    import attachment_audio

    for handle in list(attachment_audio._LIVE):
        handle.cancel()


class TestLettingGoIsSafe:
    """Letting go of a finished decoder stopped it, stopping it said
    "finished" again, and that started a second analysis of the track - whose
    thread was then freed with the decoder while it ran, and Qt aborted the
    process. Run in a process of its own, since what this guards against
    ends one."""

    SCRIPT = r'''
import math, os, struct, sys, time, wave
sys.path.insert(0, {root!r})
from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtWidgets import QApplication
app = QApplication.instance() or QApplication([])
import attachment_audio, attachments
from attachment_view import AudioPane

made = [0]
real_init = attachment_audio._AnalysisThread.__init__
def counted(self, *args, **options):
    real_init(self, *args, **options)
    made[0] += 1
attachment_audio._AnalysisThread.__init__ = counted

def track(path, seconds, pitch):
    with wave.open(path, "wb") as out:
        out.setnchannels(2); out.setsampwidth(2); out.setframerate(48000)
        frames = bytearray()
        for i in range(int(seconds * 48000)):
            beat = (i % 24000) < 900
            v = int(9000 * math.sin(2 * math.pi * pitch * i / 48000) * (1.0 if beat else 0.3))
            frames += struct.pack("<hh", v, v)
        out.writeframes(bytes(frames))
    return path

def wait(seconds):
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()

pane = AudioPane()
pane.volume.setValue(0)
pane.enable_box.setChecked(True)
for index in range(4):
    path = track(os.path.join({folder!r}, f"t{{index}}.wav"), 6.0, 110 + 40 * index)
    data = open(path, "rb").read()
    item = attachments.Attachment(part="1", name=f"t{{index}}.wav",
                                  content_type="audio/wav", size=len(data),
                                  data=data)
    pane.load(__import__("pathlib").Path(path), item)
    started = time.monotonic()
    while attachment_audio._LIVE and time.monotonic() - started < 60:
        wait(0.05)
    wait(0.5)
print("done", made[0], flush=True)
os._exit(0)
'''

    def test_tracks_one_after_another_do_not_end_the_process(self, tmp_path):
        import os
        import subprocess
        import sys

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        script = self.SCRIPT.format(root=root, folder=str(tmp_path))
        done = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            timeout=300, env=dict(os.environ, QT_QPA_PLATFORM="offscreen"))
        assert "Destroyed while thread" not in done.stderr, done.stderr[-2000:]
        assert done.returncode == 0 and "done" in done.stdout, (
            done.returncode, done.stderr[-2000:])
        # And each track analysed once: stopping a finished decoder said
        # "finished" again and started a second analysis of it.
        assert done.stdout.split()[-1] == "4", done.stdout
