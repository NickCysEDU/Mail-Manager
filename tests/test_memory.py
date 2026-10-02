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
