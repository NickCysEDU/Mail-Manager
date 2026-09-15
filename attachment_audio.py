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

#: Points per analysis window. At 22 kHz this is 21.5 Hz a bin, which is
#: what separating 50 Hz from 63 Hz needs. Smaller windows put the whole
#: bottom of the spectrum in one or two bins and every bass band moves
#: together, which does not read as an equaliser.
WINDOW = 1024

#: Analyses per second of audio. The display interpolates between frames at
#: thirty, so fifteen is indistinguishable and costs a quarter less.
RATE = 15

#: Bands on screen. Third-octave centres from 31.5 Hz to 16 kHz, which is
#: what a graphic equaliser shows and what the ear divides sound into.
#: Stops at 10 kHz because the analysis decodes at 22 kHz, and half of that
#: is the highest frequency that exists in the signal. Bands above it would
#: be drawn from noise.
CENTRES = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
           1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000)
BANDS = len(CENTRES)

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


def _band_edges(sample_rate: int) -> List[tuple]:
    """(low bin, high bin) per third-octave band.

    A third-octave band runs from centre/2**(1/6) to centre*2**(1/6). Above
    the Nyquist limit the bands collapse onto the top bin, which is correct:
    there is nothing up there to show.
    """
    bins = WINDOW // 2
    ratio = 2.0 ** (1.0 / 6.0)
    hz_per_bin = sample_rate / WINDOW
    edges = []
    for centre in CENTRES:
        low = max(1, int((centre / ratio) / hz_per_bin))
        high = min(bins - 1, max(low + 1, int((centre * ratio) / hz_per_bin) + 1))
        edges.append((min(low, bins - 2), high))
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
        for band, (lo, hi) in enumerate(edges):
            # Power summed across the band, which is what an equaliser reads,
            # rather than the single loudest bin in it.
            power = 0.0
            for bin_index in range(lo, hi):
                value = spectrum[bin_index]
                power += value.real * value.real + value.imag * value.imag
            rms = math.sqrt(power / max(1, hi - lo))
            # Decibels, floored at -70, because loudness is logarithmic and a
            # linear bar spends its whole height on the loudest thing.
            db = 20.0 * math.log10(rms + 1e-9)
            row[band] = max(0.0, (db + 70.0) / 70.0)
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
        from PySide6.QtCore import QLoggingCategory, QUrl
        from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
    except ImportError:
        on_fail("audio decoding is unavailable in this build")
        return None

    # FFmpeg narrates every file it opens, and an AAC track makes it complain
    # that it could not restamp the samples it skipped - the encoder delay at
    # the top of the file, which is 45 ms it is meant to skip. Harmless, and
    # alarming in a terminal, so the category is quietened once.
    _quieten()

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


_QUIET = False


def _quieten() -> None:
    """Stop the FFmpeg backend narrating into the terminal.

    It reports the container, every stream, and - for AAC - that it could
    not update timestamps for the samples it skipped. That last one is the
    encoder delay, which is supposed to be skipped; decoding is exact either
    way, and a person running the app from a terminal should not be told
    otherwise in red.
    """
    global _QUIET
    if _QUIET:
        return
    _QUIET = True
    try:
        from PySide6.QtCore import QLoggingCategory

        QLoggingCategory.setFilterRules(
            "qt.multimedia.ffmpeg=false\n"
            "qt.multimedia.ffmpeg.*=false\n"
            "qt.multimedia.audiodecoder=false\n")
    except Exception:      # noqa: BLE001 - quiet is a courtesy, not a feature
        pass
