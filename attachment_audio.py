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

try:      # pragma: no cover - exercised wherever Qt is present
    from PySide6.QtCore import QObject as QObject_base
    from PySide6.QtCore import QThread as _QThread_base
    from PySide6.QtCore import Signal as _Signal
except ImportError:      # pragma: no cover - the analysis half still imports
    QObject_base = object
    _QThread_base = object

    def _Signal(*_args, **_kwargs):      # noqa: N802 - matches Qt's name
        return None

#: Points per analysis window. At 48 kHz this is 23.4 Hz a bin, which
#: separates 73 Hz from 120 Hz. Smaller windows put the whole bottom of the
#: spectrum into one or two bins and every bass band moves together, which
#: does not read as an equaliser.
WINDOW = 2048

#: What the decoder is asked for. 48 kHz rather than 22 because the dial
#: scene has bands at 18 kHz and 22 kHz, and half the sample rate is all
#: that exists in a signal.
DECODE_RATE = 48000

#: Analyses per second of audio. The display interpolates between frames at
#: thirty, so fifteen is indistinguishable and costs a quarter less.
RATE = 15

#: Bands on screen. Third-octave centres from 31.5 Hz to 16 kHz, which is
#: what a graphic equaliser shows and what the ear divides sound into.
#: Third-octave centres, 50 Hz to 20 kHz. The decoder runs at 48 kHz so
#: everything here is below the Nyquist limit and none of it is noise.
CENTRES = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
           1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
           10000, 12500, 16000, 20000)
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


def analyse(samples: array, sample_rate: int, channels: int = 1,
            should_stop=None, on_progress=None) -> List[array]:
    """Band energies per frame, each 0..1.

    ``samples`` is interleaved 16-bit PCM as an array("h").

    ``should_stop`` is polled every so often and, if it returns true, the
    work is abandoned and an empty list comes back: a track nobody is
    waiting for should not keep a core busy. ``on_progress`` is called with
    a fraction so something on screen can move.
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
    # Checked about forty times over the whole track: often enough to quit
    # promptly, rarely enough that the polling costs nothing.
    checkpoint = max(hop, (total // 40) // hop * hop or hop)
    since = 0
    while at + WINDOW <= total:
        since += hop
        if since >= checkpoint:
            since = 0
            if should_stop is not None and should_stop():
                return []
            if on_progress is not None:
                on_progress(min(0.99, at / total))
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
            # A 55 dB window rather than 70. Seventy put ordinary music in
            # the top third of the range and nothing appeared to move; this
            # spends the whole height on the part anybody can hear.
            row[band] = max(0.0, (db + 55.0) / 55.0)
        frames.append(row)
        at += hop

    # Normalise to the track rather than to an absolute scale. A quiet
    # recording and a loud one should both fill the strip; without this a
    # mastered track sits pinned at the top and a podcast barely moves.
    if frames:
        everything = sorted(value for row in frames for value in row)
        high = everything[int(len(everything) * 0.97)] or 1.0
        low = everything[int(len(everything) * 0.30)]
        floor = min(low, high * 0.5)
        reach = max(0.05, high - floor)
        for row in frames:
            for index in range(len(row)):
                # Stretch the band the music actually occupies across the
                # whole height, then bend it so quiet detail still shows.
                scaled = (row[index] - floor) / reach
                scaled = max(0.0, min(1.0, scaled))
                row[index] = scaled ** 0.72
    return frames


#: Every analysis still in flight. A pane destroyed as somebody's child
#: never sees a DeferredDelete of its own, so it cannot be relied on to
#: cancel its own work - and Qt aborts the process if a running QThread is
#: destroyed. This is the backstop that runs when the application quits.
_LIVE: "set" = set()


def stop_all() -> None:
    """Cancel every analysis still running. Safe at any time."""
    for handle in list(_LIVE):
        try:
            handle.cancel()
        except Exception:      # noqa: BLE001 - shutting down regardless
            pass
    _LIVE.clear()


def _arm_shutdown() -> None:
    """Make sure stop_all runs however the process ends."""
    import atexit

    global _ARMED
    if _ARMED:
        return
    _ARMED = True
    atexit.register(stop_all)
    try:
        from PySide6.QtCore import QCoreApplication

        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(stop_all)
    except Exception:      # noqa: BLE001 - atexit alone still covers it
        pass


_ARMED = False


class _AnalysisThread(_QThread_base):
    """analyse() on its own thread, reporting through signals.

    Signals rather than a posted callback: a QThread runs no event loop of
    its own once run() returns, so a QTimer created there never fires and
    the result never arrives. A signal connected across threads is queued
    onto the receiver's thread, which is the one that owns the widgets.
    """

    done = _Signal(object)
    failed = _Signal(str)
    progress = _Signal(float)

    def __init__(self, samples, rate: int, channels: int) -> None:
        super().__init__()
        self._samples = samples
        self._rate = rate
        self._channels = channels
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        try:
            frames = analyse(self._samples, self._rate, self._channels,
                             should_stop=lambda: self._stop,
                             on_progress=self.progress.emit)
        except Exception as exc:      # noqa: BLE001
            if not self._stop:
                self.failed.emit(str(exc))
            return
        if not self._stop:
            self.done.emit(frames)


class _Analysis(QObject_base):
    """Owns the decoder and the analysis thread.

    One object for the caller to keep alive, and one to cancel.
    """

    def __init__(self, decoder, on_done, on_fail, on_progress=None) -> None:
        super().__init__()
        self._decoder = decoder
        self._on_done = on_done
        self._on_fail = on_fail
        self._on_progress = on_progress
        self._thread = None
        self._stop = False
        _LIVE.add(self)
        _arm_shutdown()

    # -- lifetime ---------------------------------------------------------
    def cancel(self) -> None:
        """Abandon the work. Safe to call more than once."""
        self._stop = True
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            try:
                decoder.stop()
            except Exception:      # noqa: BLE001 - already gone
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.stop()
            if thread.isRunning():
                # A running QThread destroyed by Qt is fatal, so wait for it.
                thread.wait(4000)
        _LIVE.discard(self)

    @property
    def cancelled(self) -> bool:
        return self._stop

    # -- the work ---------------------------------------------------------
    def start_analysis(self, samples, rate: int, channels: int) -> None:
        if self._stop:
            return
        thread = _AnalysisThread(samples, rate, channels)
        thread.done.connect(self._finished)
        thread.failed.connect(self._failed)
        if self._on_progress is not None:
            thread.progress.connect(self._report)
        self._thread = thread
        thread.start()

    def _report(self, fraction: float) -> None:
        if not self._stop and self._on_progress is not None:
            self._on_progress(fraction)

    def _finished(self, frames) -> None:
        _LIVE.discard(self)
        if not self._stop:
            self._on_done(frames)

    def _failed(self, detail: str) -> None:
        _LIVE.discard(self)
        if not self._stop:
            self._on_fail(detail)


def decode(path, on_done, on_fail, on_progress=None) -> Optional[object]:
    """Decode a file to PCM with Qt, then hand the frames back.

    Returns a handle the caller must keep alive and may ``cancel()``. Qt
    decodes on its own thread and the arithmetic runs on another, so the
    UI thread only ever copies buffers.
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
    wanted.setSampleRate(DECODE_RATE)
    decoder.setAudioFormat(wanted)

    collected = array("h")
    state = {"rate": DECODE_RATE, "channels": 1}

    def buffer_ready() -> None:
        buffer = decoder.read()
        if not buffer.isValid():
            return
        fmt = buffer.format()
        state["rate"] = fmt.sampleRate() or DECODE_RATE
        state["channels"] = fmt.channelCount() or 1
        raw = buffer.constData()
        try:
            chunk = array("h")
            chunk.frombytes(bytes(raw)[: (len(bytes(raw)) // 2) * 2])
            collected.extend(chunk)
        except Exception:      # noqa: BLE001 - a bad buffer is not fatal
            pass

    def finished() -> None:
        # Off the UI thread. This used to run here, and a three minute
        # track spent five and a half seconds inside analyse() with the
        # event loop stopped - the window went grey and the pointer became
        # a beachball, which reads as a crash rather than as work.
        handle.start_analysis(collected, state["rate"], state["channels"])

    handle = _Analysis(decoder, on_done, on_fail, on_progress)
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
    return handle


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


#: The bands the dial scene shows, which are not third-octave and are asked
#: for by name in the design it copies.
DIAL_CENTRES = (73, 120, 300, 576, 1400, 2400, 6000, 9000, 18000, 22000)


def regroup(frames: List[array], centres, source=CENTRES) -> List[array]:
    """Re-read existing frames against a different set of centres.

    The analysis is expensive and the dial scene wants ten bands that are
    not the twenty-seven the equaliser uses. Rather than analysing twice,
    each wanted centre takes the loudest of the source bands that fall
    within a third of an octave of it.
    """
    if not frames:
        return []
    picks = []
    for centre in centres:
        low, high = centre / 1.26, centre * 1.26
        chosen = [i for i, hz in enumerate(source) if low <= hz <= high]
        if not chosen:
            # Nothing that close: take the nearest single band.
            chosen = [min(range(len(source)),
                          key=lambda i: abs(math.log2(source[i] / centre)))]
        picks.append(chosen)
    out = []
    for row in frames:
        made = array("f", [0.0]) * len(centres)
        for index, chosen in enumerate(picks):
            made[index] = max(row[i] for i in chosen)
        out.append(made)
    return out
