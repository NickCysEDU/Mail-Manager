"""Turn an audio file into something worth looking at, once, cheaply.

The spectrum is computed ahead of time rather than tapped live. Qt6 removed
the audio probe that used to make live taps possible, and a precomputed
analysis is better anyway: the paint loop then does no arithmetic at all, it
interpolates between two rows of numbers that are already in memory. Thirty
frames a second of that costs almost nothing, which is the whole point - this
is an easter egg in a mail sorter, not a reason for the fan to spin up.

Sizing, chosen to keep the whole thing small:

- 256-point FFT, so eight butterfly stages rather than ten
- 20 analysed frames a second, interpolated up to whatever the screen wants
- 32 bands, spaced logarithmically because hearing is

A three minute track lands around 3,600 frames of 32 floats - under half a
megabyte, computed in a worker thread while the first attachment is still
being looked at.
"""

from __future__ import annotations

import cmath
import math
from array import array
from typing import List, Optional

#: Points per analysis window.
WINDOW = 256

#: Analyses per second of audio.
RATE = 20

#: Bands on screen.
BANDS = 32

#: Refuse to analyse more than this; a long podcast is not worth the wait.
MAX_SECONDS = 900


def _twiddles(n: int) -> List[complex]:
    return [cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)]


_TWIDDLE = _twiddles(WINDOW)
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (WINDOW - 1)) for i in range(WINDOW)]


def _fft(values: List[complex]) -> List[complex]:
    """Iterative radix-2, in place, with the twiddles precomputed."""
    n = len(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= n:
        step = n // length
        half = length // 2
        for start in range(0, n, length):
            k = 0
            for offset in range(start, start + half):
                partner = offset + half
                temp = values[partner] * _TWIDDLE[k]
                values[partner] = values[offset] - temp
                values[offset] = values[offset] + temp
                k += step
        length <<= 1
    return values


def _band_edges(sample_rate: int) -> List[int]:
    """Logarithmic band edges, in FFT bin numbers."""
    bins = WINDOW // 2
    low, high = 30.0, min(16_000.0, sample_rate / 2.0)
    edges = []
    for index in range(BANDS + 1):
        hz = low * (high / low) ** (index / BANDS)
        edges.append(min(bins - 1, max(1, int(hz * WINDOW / sample_rate))))
    for index in range(1, len(edges)):
        if edges[index] <= edges[index - 1]:
            edges[index] = min(bins - 1, edges[index - 1] + 1)
    return edges


def analyse(samples: array, sample_rate: int, channels: int = 1) -> List[array]:
    """Band energies per frame, each 0..1.

    ``samples`` is interleaved 16-bit PCM as an array("h").
    """
    if not samples or sample_rate <= 0:
        return []
    channels = max(1, channels)
    total = len(samples) // channels
    if total <= WINDOW or total / sample_rate > MAX_SECONDS:
        return []

    hop = max(1, sample_rate // RATE)
    edges = _band_edges(sample_rate)
    frames: List[array] = []
    scale = 1.0 / 32768.0

    at = 0
    while at + WINDOW <= total:
        block: List[complex] = []
        if channels == 1:
            for i in range(WINDOW):
                block.append(complex(samples[at + i] * scale * _HANN[i], 0.0))
        else:
            for i in range(WINDOW):
                base = (at + i) * channels
                mono = (samples[base] + samples[base + 1]) * 0.5
                block.append(complex(mono * scale * _HANN[i], 0.0))
        spectrum = _fft(block)
        row = array("f", [0.0]) * BANDS
        for band in range(BANDS):
            lo, hi = edges[band], edges[band + 1]
            peak = 0.0
            for bin_index in range(lo, hi):
                value = abs(spectrum[bin_index])
                if value > peak:
                    peak = value
            # Log compression, so quiet detail is visible next to a kick drum.
            row[band] = math.log10(1.0 + peak * 18.0)
        frames.append(row)
        at += hop

    # Normalise to the track rather than to an absolute scale. A quiet
    # recording and a loud one should both fill the strip; without this a
    # mastered track sits pinned at the top and a podcast barely moves.
    if frames:
        everything = sorted(value for row in frames for value in row)
        high = everything[int(len(everything) * 0.97)] or 1.0
        if high > 0:
            gain = 0.92 / high
            for row in frames:
                for index in range(len(row)):
                    row[index] = min(1.0, row[index] * gain)
    return frames


def decode(path, on_done, on_fail) -> Optional[object]:
    """Decode a file to PCM with Qt, then hand the frames back.

    Returns the decoder, which the caller must keep alive. Qt does the
    decoding on its own thread; only the arithmetic happens here.
    """
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
    except ImportError:
        on_fail("audio decoding is unavailable in this build")
        return None

    decoder = QAudioDecoder()
    wanted = QAudioFormat()
    wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    wanted.setChannelCount(1)
    wanted.setSampleRate(22050)
    decoder.setAudioFormat(wanted)

    collected = array("h")
    state = {"rate": 22050, "channels": 1}

    def buffer_ready() -> None:
        buffer = decoder.read()
        if not buffer.isValid():
            return
        fmt = buffer.format()
        state["rate"] = fmt.sampleRate() or 22050
        state["channels"] = fmt.channelCount() or 1
        raw = buffer.constData()
        try:
            chunk = array("h")
            chunk.frombytes(bytes(raw)[: (len(bytes(raw)) // 2) * 2])
            collected.extend(chunk)
        except Exception:      # noqa: BLE001 - a bad buffer is not fatal
            pass

    def finished() -> None:
        try:
            frames = analyse(collected, state["rate"], state["channels"])
        except Exception as exc:      # noqa: BLE001
            on_fail(str(exc))
            return
        on_done(frames)

    decoder.bufferReady.connect(buffer_ready)
    decoder.finished.connect(finished)
    # The signal is named differently across Qt 6 point releases, and a
    # visualiser is not worth a crash if it is absent.
    for name in ("errorOccurred", "error"):
        signal = getattr(decoder, name, None)
        if signal is not None and hasattr(signal, "connect"):
            try:
                signal.connect(lambda *_: on_fail("this file will not decode"))
                break
            except (TypeError, RuntimeError):
                continue
    decoder.setSource(QUrl.fromLocalFile(str(path)))
    decoder.start()
    return decoder
