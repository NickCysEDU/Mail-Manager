"""When the music is heard, and when a frame is seen: the picture's allowance
for both.

"Xxxxxx xxx xxxxxxxxxxx xxx xxxxxxxxx xx xxxx xxx xxx xxxxxxx." A media
player's position is the sound it has handed to the machine's audio, not the
sound anybody is hearing: that is the output device's latency later - a
fifth of a millisecond per sample of buffer on built-in speakers, a sixth
of a second on Bluetooth headphones. And a frame drawn now is on the glass a
frame or two later. So the picture shows the music at

    position + display latency - audio latency + trim

which is what is in the listener's ears at the moment the frame reaches
their eyes. The audio half is read from the device, which knows it: on a
Mac, Core Audio reports the device's own latency, its safety offset, its
buffer and its stream's latency, and the four together are when a sample
handed over now comes out. The display half cannot be read and is a
frame and a half of the screen's refresh - the swap waits for the next
refresh and the compositor holds it for one more. The trim is the
listener's own, for whatever neither can know.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import struct
import sys
import time
from typing import Optional

log = logging.getLogger(__name__)

#: How many refreshes after it is drawn a frame is on the screen.
DISPLAY_FRAMES = 1.5
#: How often the audio device is asked again: headphones come and go.
ASK_EVERY = 5.0
#: The most either way a latency is believed, in seconds. A device that
#: reports more than this is reporting something else.
MOST = 0.5


def _fourcc(text: str) -> int:
    return struct.unpack(">I", text.encode("ascii"))[0]


class _Address(ctypes.Structure):
    _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                ("element", ctypes.c_uint32)]


_core = None


def _core_audio():
    global _core
    if _core is None:
        found = ctypes.util.find_library("CoreAudio")
        if not found:
            raise OSError("no Core Audio")
        library = ctypes.cdll.LoadLibrary(found)
        library.AudioObjectGetPropertyData.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p]
        library.AudioObjectGetPropertyDataSize.argtypes = [
            ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        _core = library
    return _core


def _read(core, thing: int, selector: str, scope: str, kind):
    address = _Address(_fourcc(selector), _fourcc(scope), 0)
    value = kind()
    size = ctypes.c_uint32(ctypes.sizeof(value))
    failed = core.AudioObjectGetPropertyData(
        thing, ctypes.byref(address), 0, None, ctypes.byref(size),
        ctypes.byref(value))
    return None if failed else value.value


def device_latency() -> Optional[float]:
    """How long a sample handed to the default output device takes to come
    out of it, in seconds; None where that cannot be asked."""
    if sys.platform != "darwin":
        return None
    core = _core_audio()
    system = 1       # kAudioObjectSystemObject
    device = _read(core, system, "dOut", "glob", ctypes.c_uint32)
    if not device:
        return None
    rate = _read(core, device, "nsrt", "glob", ctypes.c_double)
    if not rate:
        return None
    frames = 0
    for selector, scope in (("ltnc", "outp"), ("saft", "outp"),
                            ("fsiz", "glob")):
        frames += _read(core, device, selector, scope, ctypes.c_uint32) or 0
    # The streams' own latency, the largest of them.
    address = _Address(_fourcc("stm#"), _fourcc("outp"), 0)
    size = ctypes.c_uint32(0)
    if not core.AudioObjectGetPropertyDataSize(device, ctypes.byref(address),
                                               0, None, ctypes.byref(size)):
        count = size.value // 4
        if count:
            streams = (ctypes.c_uint32 * count)()
            if not core.AudioObjectGetPropertyData(
                    device, ctypes.byref(address), 0, None,
                    ctypes.byref(size), streams):
                frames += max((_read(core, stream, "ltnc", "glob",
                                     ctypes.c_uint32) or 0)
                              for stream in streams)
    seconds = frames / rate
    return seconds if 0.0 <= seconds <= MOST else None


class Allowance:
    """The picture's allowance for the ear and the eye, kept current.

    ``ahead(refresh)`` is how far ahead of the player's position the
    picture should be, in seconds, for a screen refreshing ``refresh``
    times a second: the display's latency, less the audio's, plus the
    trim.
    """

    def __init__(self, ask=device_latency, clock=time.monotonic) -> None:
        self._ask = ask
        self._clock = clock
        self._audio = 0.0
        self._asked_at: Optional[float] = None
        self._failed = False
        #: The listener's own adjustment, in seconds: more is the picture
        #: earlier.
        self.trim = 0.0

    def audio(self) -> float:
        """The output device's latency, asked again every ASK_EVERY."""
        now = self._clock()
        if self._failed:
            return self._audio
        if self._asked_at is None or now - self._asked_at >= ASK_EVERY:
            self._asked_at = now
            try:
                found = self._ask()
            except Exception as exc:      # noqa: BLE001 - a guess, then
                self._failed = True
                log.info("Could not ask the audio device its latency (%s).",
                         exc)
                found = None
            if found is not None:
                self._audio = max(0.0, min(MOST, float(found)))
        return self._audio

    @staticmethod
    def display(refresh: float) -> float:
        refresh = refresh if refresh and refresh > 0.0 else 60.0
        return DISPLAY_FRAMES / refresh

    def ahead(self, refresh: float = 60.0) -> float:
        return self.display(refresh) - self.audio() + self.trim
