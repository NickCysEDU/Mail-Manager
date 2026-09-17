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
            should_stop=None, on_progress=None, calibration=None) -> List[array]:
    """Band energies per frame, each 0..1.

    ``samples`` is interleaved 16-bit PCM as an array("h").

    ``should_stop`` is polled every so often and, if it returns true, the
    work is abandoned and an empty list comes back: a track nobody is
    waiting for should not keep a core busy. ``on_progress`` is called with
    a fraction so something on screen can move.

    The returned numbers are stretched to fill the display, which makes a
    quiet recording watchable but means a bar's height is no longer a
    level. Pass a dict as ``calibration`` and it is filled with what is
    needed to get back to decibels, so a scene that prints a scale can
    print a true one.
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
            row[band] = max(0.0, (db + RANGE_DB) / RANGE_DB)
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
                row[index] = scaled ** GAMMA
        if calibration is not None:
            # Enough to undo all of it: raw = floor + reach * shown ** (1/g),
            # and dB = raw * RANGE_DB - RANGE_DB.
            calibration.update({"floor": floor, "reach": reach,
                                "gamma": GAMMA, "range_db": RANGE_DB})
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
            calibration: dict = {}
            frames = analyse(self._samples, self._rate, self._channels,
                             should_stop=lambda: self._stop,
                             on_progress=lambda f: self.progress.emit(f * 0.85),
                             calibration=calibration)
            # The waveform the oscilloscope draws, on the same schedule as
            # the bands so one index reads both.
            shapes = traces(self._samples, self._rate, self._channels,
                            should_stop=lambda: self._stop)
            vectors = vector_traces(self._samples, self._rate, self._channels,
                                    should_stop=lambda: self._stop)
            # Where the beats are. Off the frames that have just been
            # computed, on this thread, because it is milliseconds of work
            # against the seconds the analysis takes and doing it here
            # means the strobe knows the whole track before a note plays -
            # which is what lets it sit on the beat instead of chasing it.
            import beatmap
            beats = beatmap.build(frames, RATE)
        except Exception as exc:      # noqa: BLE001
            if not self._stop:
                self.failed.emit(str(exc))
            return
        if not self._stop:
            self.done.emit((frames, shapes, vectors, calibration, beats))


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
    # Two channels, so the scope can plot one against the other. That is
    # what oscilloscope music is: the picture lives in the difference
    # between left and right, and a mono decode throws it away.
    wanted.setChannelCount(2)
    wanted.setSampleRate(DECODE_RATE)
    decoder.setAudioFormat(wanted)

    collected = array("h")
    state = {"rate": DECODE_RATE, "channels": 2}

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


#: The window of level the bands are spread across, in decibels, and the
#: bend applied afterwards so quiet detail still shows.
RANGE_DB = 55.0
GAMMA = 0.72

#: Points in one X-Y frame. A drawing is cut at audio rate, so every
#: sample in the window is part of the picture: taking every eighth one,
#: as the sweep does, turns a detailed figure into a scribble.
#:
#: The length was chosen by rendering Oscilloscope Music's "Function" at
#: 130, 260, 520 and 1024 and looking at the results. Its figures repeat
#: about every 65 samples, so 512 is eight passes of the same shape laid
#: over each other - enough that the figure is solid, few enough that it
#: has not moved on to the next one. At 1024 the later figures smear into
#: the earlier ones; at 260 the shape is not finished.
#:
#: Kept as int16 rather than floats: the same density in floats is four
#: times the memory for no more picture.
VECTOR_POINTS = 512

#: Points in one oscilloscope trace. Enough to show a waveform's shape at
#: any width the scene is drawn at, small enough that a three minute track
#: costs about a megabyte of them.
TRACE_POINTS = 256


def traces(samples: array, sample_rate: int, channels: int = 1,
           should_stop=None) -> List[array]:
    """One short slice of the actual waveform per frame, -1 to 1.

    The oscilloscope was drawing a shape derived from band energies, which
    is a picture of a spectrum pretending to be a waveform. This is the
    signal itself, decimated to a fixed number of points, so what is on
    screen is what is in the file.

    Each trace starts at a zero crossing where one can be found nearby,
    which is what a scope's trigger does and what stops the waveform
    sliding sideways from frame to frame.
    """
    if not samples or sample_rate <= 0:
        return []
    channels = max(1, channels)
    total = len(samples) // channels
    if total <= WINDOW:
        return []

    hop = max(1, sample_rate // RATE)
    # One cycle of 80 Hz at the decode rate, which shows a bass waveform
    # whole and a treble one as several cycles.
    span = min(total, max(TRACE_POINTS, sample_rate // 80))
    step = max(1, span // TRACE_POINTS)
    scale = 1.0 / 32768.0
    out: List[array] = []
    at = 0
    checked = 0
    while at + span <= total:
        checked += 1
        if should_stop is not None and not checked % 40 and should_stop():
            return []
        start = _trigger(samples, at, min(span, hop), channels)
        row = array("f", [0.0]) * TRACE_POINTS
        for point in range(TRACE_POINTS):
            index = (start + point * step) * channels
            if index + channels <= len(samples):
                value = samples[index]
                if channels > 1:
                    value = (value + samples[index + 1]) * 0.5
                row[point] = max(-1.0, min(1.0, value * scale))
        out.append(row)
        at += hop
    return out


def vector_traces(samples: array, sample_rate: int, channels: int = 2,
                  should_stop=None) -> List[array]:
    """Left against right, which is how oscilloscope music draws.

    Interleaved as x, y, x, y - so one trace is 2 * TRACE_POINTS long.
    A record that was written for a scope puts a picture in here; an
    ordinary stereo mix puts a blob that leans with the stereo image,
    which is what a vectorscope shows and is worth looking at anyway.

    No trigger: the position in the file is the position in the drawing,
    and hunting for a zero crossing would tear the picture apart.
    """
    if not samples or sample_rate <= 0 or channels < 2:
        return []
    total = len(samples) // channels
    if total <= WINDOW:
        return []

    hop = max(1, sample_rate // RATE)
    # A longer window than the sweep uses: a drawing takes more than one
    # cycle of anything to complete.
    # Consecutive samples, not every nth. The window is about twenty
    # milliseconds, which is roughly how long a figure takes to be drawn
    # once, and every sample in it is a point on the figure.
    span = min(total, VECTOR_POINTS)
    out: List[array] = []
    at = 0
    checked = 0
    while at + span <= total:
        checked += 1
        if should_stop is not None and not checked % 40 and should_stop():
            return []
        row = array("h", [0]) * (span * 2)
        for point in range(span):
            index = (at + point) * channels
            if index + 1 < len(samples):
                row[point * 2] = samples[index]
                row[point * 2 + 1] = samples[index + 1]
        out.append(row)
        at += hop
    return out


def _trigger(samples: array, at: int, window: int, channels: int) -> int:
    """The first rising zero crossing near ``at``, or ``at`` itself.

    Without this the trace starts wherever the frame happens to land and
    the waveform crawls across the screen instead of standing still.
    """
    previous = samples[at * channels]
    for offset in range(1, window):
        index = (at + offset) * channels
        if index >= len(samples):
            break
        value = samples[index]
        if previous < 0 <= value:
            return at + offset
        previous = value
    return at


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
