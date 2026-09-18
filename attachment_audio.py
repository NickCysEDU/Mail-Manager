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
import logging
import math
from array import array
from operator import mul
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

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


def _two_real_ffts(first: List[float], second: List[float] = None,
                   transform=None, there: List[float] = None) -> tuple:
    """Two real spectra out of one complex transform.

    The signal being analysed is real, and a real transform throws half of
    a complex one away: the negative frequencies are just the positive
    ones conjugated. So two frames can ride in one transform - one in the
    real part, one in the imaginary - and be separated afterwards, because
    a real input's spectrum is conjugate-symmetric and an imaginary one's
    is conjugate-antisymmetric.

        A[k] = (Z[k] + conj(Z[N-k])) / 2
        B[k] = (Z[k] - conj(Z[N-k])) / 2j

    The transform is what analysing a track costs - measured, three
    quarters of it - and this halves how many are needed. It is exact
    arithmetic, not an approximation: the bands that come out are the same
    bands to the last bit that floating point allows.

    Only the first half of each spectrum is returned, which is all the
    bands ever read.
    """
    second = second if second is not None else there
    transform = transform or _fft
    n = len(first)
    packed = [complex(first[i], second[i]) for i in range(n)]
    spectrum = transform(packed)
    half = n // 2
    a: List[complex] = [0j] * half
    b: List[complex] = [0j] * half
    for k in range(half):
        here = spectrum[k]
        # Z[0]'s partner is Z[0] itself, which the modulo handles.
        there = spectrum[(n - k) % n].conjugate()
        a[k] = (here + there) * 0.5
        b[k] = (here - there) * -0.5j
    return a, b


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


#: A second, much finer pass, used only for finding drums.
#:
#: The display frames run at fifteen a second, which is a frame every
#: sixty-seven milliseconds - longer than a whole drum hit. At that rate a
#: kick and the snare after it are three and a half frames apart and a hat
#: pattern is finer than the sampling, so no amount of cleverness
#: afterwards can tell one from another: the information is not there.
#:
#: This pass is short windows taken often. Ten milliseconds is far too
#: short to resolve a bass note and exactly right for catching the start
#: of one, which is all an onset is. A 512-point transform is about a
#: ninth of the work of a 2048-point one, so taking four times as many
#: costs about half as much again in total.
#: Twenty-one milliseconds. Not shorter: at 512 the lowest band a
#: transform can report starts at 93 Hz, which is above where a kick
#: lives, so the one instrument the bottom of the range exists for was
#: invisible to it.
#: How much the drum pass thins the signal before it looks at it. The
#: window below is in samples *after* this, so the two together decide the
#: bin width - which is what actually has to be fine enough to put a kick
#: in a band of its own. 512 over 24 kHz resolves what 1024 over 48 did,
#: for half the arithmetic.
ONSET_DECIMATE = 2
ONSET_WINDOW = 512
ONSET_RATE = 60
#: The coarse bands this pass reports, as fractions of the spectrum. Only
#: enough to tell the bottom from the middle from the top, because that is
#: all that separates a kick from a snare from a hat.
ONSET_BANDS = 12

_ONSET_TWIDDLE = _twiddles(ONSET_WINDOW)
_ONSET_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (ONSET_WINDOW - 1))
               for i in range(ONSET_WINDOW)]


def _onset_fft(values: List[complex]) -> List[complex]:
    """The same radix-2 as _fft, on the shorter window's twiddles."""
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
                temp = values[partner] * _ONSET_TWIDDLE[k]
                values[partner] = values[offset] - temp
                values[offset] = values[offset] + temp
                k += step
        length <<= 1
    return values


def _onset_window_at(samples: array, at: int, channels: int,
                     scale: float) -> List[float]:
    """One short Hann-windowed frame of mono, for the onset pass."""
    if channels == 1:
        return [samples[at + i] * scale * _ONSET_HANN[i]
                for i in range(ONSET_WINDOW)]
    out = []
    for i in range(ONSET_WINDOW):
        base = (at + i) * channels
        mono = (samples[base] + samples[base + 1]) * 0.5
        out.append(mono * scale * _ONSET_HANN[i])
    return out


def _onset_mono(samples: array, channels: int) -> array:
    """The track as one channel at half the rate, for the drum pass.

    Everything the onset pass asks of the signal is "did something start
    here, and roughly where in the spectrum" - see ``onset_frames``. That
    question does not need 24 kHz of bandwidth or two channels, and
    carrying both was most of what the pass cost: the transform is the
    expensive part and it grows with the window, so halving the rate
    halves the window for the same span of time and the same musical
    resolution.

    Summed to mono first, then averaged in pairs, which is a crude
    low-pass and the right one here: it is the anti-aliasing filter, and
    what it rolls off is the top of the hats, which the top band still
    reaches without.
    """
    channels = max(1, channels)
    total = len(samples) // channels
    out = array("f", bytes(4 * (total // 2)))
    if channels == 1:
        for index in range(total // 2):
            out[index] = (samples[index * 2] + samples[index * 2 + 1]) * 0.5
        return out
    for index in range(total // 2):
        base = index * 2 * channels
        out[index] = (samples[base] + samples[base + 1]
                      + samples[base + channels]
                      + samples[base + channels + 1]) * 0.25
    return out


def _onset_edges(bins: int) -> List[tuple]:
    """Log-spaced bin ranges, from the first bin to the last.

    Each band is the same musical width as the one before it, which means
    the bottom of the range gets narrow bands and the top gets wide ones.
    Written as a ratio raised to a power rather than by halving from the
    top - the first attempt did the latter, which made the *lowest* band
    the widest and lumped everything under a few hundred hertz together,
    so the one band a kick lives in was also the one a snare lives in.
    """
    span = bins / 1.0
    edges = []
    for band in range(ONSET_BANDS):
        low = 1.0 * (span ** (band / ONSET_BANDS))
        high = 1.0 * (span ** ((band + 1) / ONSET_BANDS))
        lo = max(1, int(low))
        hi = max(lo + 1, min(bins, int(math.ceil(high))))
        edges.append((lo, hi))
    return edges


def onset_frames(samples: array, sample_rate: int, channels: int = 1,
                 should_stop=None) -> List[array]:
    """Coarse band energies at sixty a second, for finding drums.

    Not for drawing. These are deliberately crude - twelve bands, a
    ten-millisecond window - because what is being asked of them is
    "did something start here, and where in the spectrum", and a finer
    answer to that question costs more and says nothing extra.
    """
    if not samples or sample_rate <= 0:
        return []
    channels = max(1, channels)
    total = len(samples) // channels
    if total <= ONSET_WINDOW or total / sample_rate > MAX_SECONDS:
        return []
    # One channel at half the rate, made once. The pass then reads a
    # plain array instead of de-interleaving a stereo frame every time,
    # which was a fifth of what it cost on its own.
    mono = _onset_mono(samples, channels)
    total = len(mono)
    sample_rate = sample_rate // ONSET_DECIMATE
    if total <= ONSET_WINDOW:
        return []
    hop = max(1, sample_rate // ONSET_RATE)
    bins = ONSET_WINDOW // 2
    edges = _onset_edges(bins)

    out: List[array] = []
    scale = 1.0 / 32768.0
    at = 0
    checkpoint = max(hop, (total // 20) // hop * hop or hop)
    since = 0
    while at + ONSET_WINDOW <= total:
        since += hop
        if since >= checkpoint:
            since = 0
            if should_stop is not None and should_stop():
                return []
        # Two frames per transform, the same trick the display pass uses.
        here = [mono[at + i] * scale * _ONSET_HANN[i]
                for i in range(ONSET_WINDOW)]
        second_at = at + hop
        if second_at + ONSET_WINDOW <= total:
            there = [mono[second_at + i] * scale * _ONSET_HANN[i]
                     for i in range(ONSET_WINDOW)]
            spectra = _two_real_ffts(there=there, first=here,
                                     transform=_onset_fft)
        else:
            spectra = (_onset_fft([complex(v, 0.0) for v in here])[:bins],)
        for spectrum in spectra:
            row = array("f", [0.0]) * ONSET_BANDS
            for band, (lo, hi) in enumerate(edges):
                power = 0.0
                for bin_index in range(lo, hi):
                    value = spectrum[bin_index]
                    power += value.real * value.real + value.imag * value.imag
                row[band] = math.sqrt(power / max(1, hi - lo))
            out.append(row)
        at += hop * len(spectra)
    # Linear, and scaled to the loudest band in the track rather than put
    # through decibels.
    #
    # Decibels are right for a display and wrong for deciding what an
    # instrument was. The whole question is "where did this hit put its
    # energy", and a kick is a hundred times the power at the bottom that
    # it is at the top - which in decibels is twenty units against a scale
    # of seventy, so once every band is compressed that way they all rise
    # together and a kick, a snare and a hat come out looking the same.
    # Measured: their profiles agreed to within four per cent.
    loudest = 0.0
    for row in out:
        for value in row:
            if value > loudest:
                loudest = value
    if loudest > 0.0:
        scale_to = 1.0 / loudest
        for row in out:
            for index in range(len(row)):
                row[index] *= scale_to
    return out


def _window_at(samples: array, at: int, channels: int,
               scale: float) -> List[float]:
    """One Hann-windowed frame of mono, as plain floats."""
    if channels == 1:
        return [samples[at + i] * scale * _HANN[i] for i in range(WINDOW)]
    out = []
    for i in range(WINDOW):
        base = (at + i) * channels
        mono = (samples[base] + samples[base + 1]) * 0.5
        out.append(mono * scale * _HANN[i])
    return out


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
        # Two frames at a time, because two real transforms fit in one
        # complex one. The second is whatever comes a hop later; at the
        # very end there may not be one, and then it is transformed on
        # its own the plain way.
        here = _window_at(samples, at, channels, scale)
        second_at = at + hop
        if second_at + WINDOW <= total:
            there = _window_at(samples, second_at, channels, scale)
            spectra = _two_real_ffts(here, there)
        else:
            spectra = (_fft([complex(v, 0.0) for v in here])[:WINDOW // 2],)
        for spectrum in spectra:
            row = array("f", [0.0]) * BANDS
            for band, (lo, hi) in enumerate(edges):
                # Power summed across the band, which is what an equaliser
                # reads, rather than the single loudest bin in it.
                power = 0.0
                for bin_index in range(lo, hi):
                    value = spectrum[bin_index]
                    power += value.real * value.real + value.imag * value.imag
                rms = math.sqrt(power / max(1, hi - lo))
                # Decibels, floored at -70, because loudness is logarithmic
                # and a linear bar spends its whole height on the loudest
                # thing.
                db = 20.0 * math.log10(rms + 1e-9)
                # A 55 dB window rather than 70. Seventy put ordinary music
                # in the top third of the range and nothing appeared to
                # move; this spends the whole height on the part anybody
                # can hear.
                row[band] = max(0.0, (db + RANGE_DB) / RANGE_DB)
            frames.append(row)
        at += hop * len(spectra)

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

#: Threads that would not stop in time, kept forever.
#:
#: Qt calls qFatal when a running QThread is destroyed, and qFatal aborts
#: the process - it is not an exception anybody can catch. Waiting is the
#: answer and it can time out, so when it does the thread is cut loose
#: instead: a reference is kept here so neither Python nor Qt can collect
#: it, and the process exits normally while it finishes on its own. Every
#: step it takes is bounded, so it does finish.
_ABANDONED: "list" = []


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
    #: The drums, which arrive after the rest.
    #:
    #: Finding them needs a second, much finer pass over the samples, and
    #: that pass costs about twice what the display frames cost. Making
    #: the visualiser wait for it would mean the picture appeared later
    #: than it does now, in exchange for something no scene needs in its
    #: first second. So the frames go out as soon as they are ready and
    #: the kit follows a few seconds later.
    elements = _Signal(object)
    failed = _Signal(str)
    progress = _Signal(float)

    def __init__(self, samples, rate: int, channels: int,
                 wants_elements: bool = True) -> None:
        super().__init__()
        self._samples = samples
        self._rate = rate
        self._channels = channels
        self._stop = False
        self._wants_elements = wants_elements

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
        if self._stop:
            return
        self.done.emit((frames, shapes, vectors, calibration, beats))

        # Now the slow part, with the picture already on screen - and
        # only if somebody is waiting for it. Doing it regardless meant a
        # thread went on running for seconds after the only caller that
        # wanted the result had been told it was finished.
        if self._stop or not self._wants_elements:
            return
        try:
            import beatmap as _beatmap

            fine = onset_frames(self._samples, self._rate, self._channels,
                                should_stop=lambda: self._stop)
            if self._stop or not fine:
                return
            kit = _beatmap.elements(fine, ONSET_RATE)
        except Exception as exc:      # noqa: BLE001 - lighting, not the mail
            log.info("Could not pick the drums out (%s).", exc)
            return
        if not self._stop:
            self.elements.emit(kit)


class _Analysis(QObject_base):
    """Owns the decoder and the analysis thread.

    One object for the caller to keep alive, and one to cancel.
    """

    def __init__(self, decoder, on_done, on_fail, on_progress=None,
                 on_elements=None) -> None:
        super().__init__()
        self._decoder = decoder
        self._on_done = on_done
        self._on_fail = on_fail
        self._on_progress = on_progress
        self._on_elements = on_elements
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
                if not thread.wait(4000):
                    # It did not stop. Dropping the last reference here
                    # would let Qt destroy it while it runs, which aborts
                    # the process - so it is kept instead, and the
                    # process exits while it finishes on its own.
                    log.warning(
                        "An analysis did not stop in time; letting it "
                        "finish on its own rather than destroying it.")
                    if thread not in _ABANDONED:
                        _ABANDONED.append(thread)
        _LIVE.discard(self)

    @property
    def cancelled(self) -> bool:
        return self._stop

    # -- the work ---------------------------------------------------------
    def start_analysis(self, samples, rate: int, channels: int) -> None:
        if self._stop:
            return
        thread = _AnalysisThread(samples, rate, channels,
                                 self._on_elements is not None)
        thread.done.connect(self._finished)
        thread.failed.connect(self._failed)
        if self._on_elements is not None:
            thread.elements.connect(self._kit)
        thread.finished.connect(self._thread_done)
        if self._on_progress is not None:
            thread.progress.connect(self._report)
        self._thread = thread
        thread.start()

    def _kit(self, elements) -> None:
        """The drums, once the finer pass has finished."""
        if not self._stop and self._on_elements is not None:
            self._on_elements(elements)

    def _report(self, fraction: float) -> None:
        if not self._stop and self._on_progress is not None:
            self._on_progress(fraction)

    def _finished(self, frames) -> None:
        # Deliberately still in _LIVE. The frames are ready but the thread
        # is not: it goes on to pick the drums out afterwards, and letting
        # this object be collected while its thread is running is how Qt
        # takes the process down. It leaves when the thread does, below.
        if not self._stop:
            self._on_done(frames)

    def _thread_done(self) -> None:
        """The thread's own signal: run() has returned and it is safe."""
        _LIVE.discard(self)

    def _failed(self, detail: str) -> None:
        _LIVE.discard(self)
        if not self._stop:
            self._on_fail(detail)


def decode(path, on_done, on_fail, on_progress=None,
           on_elements=None) -> Optional[object]:
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

    handle = _Analysis(decoder, on_done, on_fail, on_progress, on_elements)
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
#: The most points kept for one X-Y trace.
#:
#: A cap now rather than a fixed count, and it is the memory knob: at 512
#: every trace cost the same whatever it held, and a fifteen minute track
#: comes to about 28 MB either way.
#:
#: What changed is what a trace *spans*. It used to be 512 consecutive
#: samples, about eleven milliseconds, taken once every sixty-seven. On a
#: record written for a scope that is between a third and a half of one
#: figure - so every frame drew part of a drawing, a different part each
#: time, and the phosphor stacked four unrelated fragments on top of each
#: other. That is "laggy, shaky and all over the place": the figures
#: really were incomplete, and they really were different every frame.
#:
#: See ``_figure_lag``: the span is one figure now.
VECTOR_POINTS = 1024

#: How often the figure rate is measured, in traces. The rate is a
#: property of the passage rather than of the track - measured across one
#: record it went 25 Hz, 200, 132, 123, 25, 104, 10.6, 50.5 - so it is
#: followed rather than decided once. Fifteen traces is about a second.
FIGURE_EVERY = 30

#: The band of figure rates looked for, in samples of lag at the decode
#: rate: about 8 Hz to 200 Hz.
FIGURE_SLOWEST = 4000
FIGURE_FASTEST = 240

#: How much of the signal is looked at to find the rate, and how coarsely.
#: Both are for speed: the search runs on a copy decimated by four, which
#: costs sixteen times less and finds the same lag to within a sample or
#: two, and the answer is then refined at full rate.
FIGURE_LOOK = 8192
FIGURE_COARSE = 4

#: How near the best a shorter lag has to score before it is preferred.
#: See ``_figure_lag``: every multiple of a figure's period fits it
#: equally well, and the shortest one is the figure.
FIGURE_PREFER = 0.93

#: Below this the passage has no figure in it - a noise sweep, a cymbal,
#: silence - and a span chosen from the lag would be arbitrary. The old
#: fixed window is used instead.
FIGURE_SURE = 0.45

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


def _figure_lag(samples: array, channels: int, at: int,
                look: int = FIGURE_LOOK) -> Tuple[int, float]:
    """How long one figure takes, in samples, and how sure that is.

    A record written for a scope draws the same shape over and over, and
    how long that takes is the one setting a person reaches for first on
    a real instrument: you turn the time base until the figure stands
    still. Nothing here was doing that, so the window was whatever eleven
    milliseconds happened to contain.

    The beam's path is (left, right), so this is an autocorrelation of the
    two together - how well the path lies on top of itself a lag later.

    Done on a copy decimated by four, which is sixteen times less
    arithmetic for an answer within a sample or two, and then refined at
    full rate. Straight, it measured 21 ms a go, which over a long track
    is most of a minute spent on a visualiser.
    """
    total = len(samples) // max(1, channels)
    if at < 0 or at + look > total or channels < 2:
        return 0, 0.0
    step = FIGURE_COARSE
    xs = samples[at * channels: (at + look) * channels: channels * step]
    ys = samples[at * channels + 1: (at + look) * channels + 1: channels * step]
    count = min(len(xs), len(ys))
    if count < 32:
        return 0, 0.0
    energy = sum(map(mul, xs, xs)) + sum(map(mul, ys, ys))
    if energy <= 0.0:
        return 0, 0.0

    # sum(map(mul, ...)) rather than a loop over indices, because all of
    # it then happens in C. Same answer, less than half the time: 24.5 ms
    # a measurement became 11.5, and over a seven minute record that is
    # the difference between eleven seconds of the analysis and five.
    scored = []
    best_score = 0.0
    lag = max(1, FIGURE_FASTEST // step)
    top = min(FIGURE_SLOWEST // step, count - 32)
    while lag <= top:
        span = count - lag
        here = (sum(map(mul, xs, xs[lag:])) + sum(map(mul, ys, ys[lag:])))
        # Against the energy of the part that overlaps, so a long lag is
        # not punished for having less of itself left to compare.
        score = here / max(1.0, energy * span / count)
        scored.append((lag, score))
        if score > best_score:
            best_score = score
        # Geometric, because the interesting range is twelve hertz to two
        # hundred and a fixed step would spend all of itself at the slow
        # end.
        lag += max(1, lag // 40)
    if not scored or best_score <= 0.0:
        return 0, 0.0

    # The *shortest* lag that is as good as the best, not the best.
    #
    # A figure that really repeats lies on top of itself at every multiple
    # of its period, and all of those score the same. Taking the highest
    # score then picks whichever one a floating point comparison happened
    # to favour: a figure written with a period of 953 samples came back
    # as 2859, and one of 241 as 1687. Both are perfectly good answers to
    # the question asked and completely wrong for the question meant -
    # they draw three figures and seven, which is the smear this whole
    # change is about.
    best_lag = scored[-1][0]
    for lag, score in scored:
        if score >= best_score * FIGURE_PREFER:
            best_lag = lag
            best_score = score
            break

    # Back to full rate. The search has to cover half the gap to the
    # neighbouring coarse lag, not a fixed few samples: the grid is
    # geometric, so near a lag of 950 its rungs are ninety-five samples
    # apart and looking four either side finds nothing.
    coarse = best_lag * step
    reach = max(step, best_lag // 40 * step // 2 + step)
    finest, found = best_score, coarse
    full_x = samples[at * channels: (at + look) * channels: channels]
    full_y = samples[at * channels + 1: (at + look) * channels + 1: channels]
    thin_x = full_x[::step]
    thin_y = full_y[::step]
    for lag in range(max(1, coarse - reach), coarse + reach + 1):
        span = look - lag
        if span < 32:
            break
        here = (sum(map(mul, thin_x, full_x[lag::step]))
                + sum(map(mul, thin_y, full_y[lag::step])))
        here = here / max(1.0, energy * span / look)
        if here > finest:
            finest, found = here, lag
    return found, finest


def vector_traces(samples: array, sample_rate: int, channels: int = 2,
                  should_stop=None) -> List[array]:
    """Left against right, which is how oscilloscope music draws.

    Interleaved as x, y, x, y - so one trace is twice as long as it has
    points. A record that was written for a scope puts a picture in here;
    an ordinary stereo mix puts a blob that leans with the stereo image,
    which is what a vectorscope shows and is worth looking at anyway.

    **One trace is one figure.** The time base is measured from the record
    - see ``_figure_lag`` - and followed as it changes, which is the thing
    a person does first with a real scope and the thing this was not doing
    at all. Before, a trace was 512 samples whatever the record was doing:
    between a third and a half of one figure, so every frame drew a
    different fragment of a drawing and the phosphor stacked four of them.

    Where the figure is longer than the points allowed, it is thinned
    rather than cut short - a whole figure at half the samples is still
    the figure, and half a figure at every sample is not.

    No trigger: the position in the file is the position in the drawing,
    and hunting for a zero crossing would tear the picture apart.
    """
    if not samples or sample_rate <= 0 or channels < 2:
        return []
    total = len(samples) // channels
    if total <= WINDOW:
        return []

    hop = max(1, sample_rate // RATE)
    out: List[array] = []
    at = 0
    checked = 0
    lag, sure = 0, 0.0
    since = FIGURE_EVERY
    while at < total:
        checked += 1
        if should_stop is not None and not checked % 40 and should_stop():
            return []
        if since >= FIGURE_EVERY:
            since = 0
            lag, sure = _figure_lag(samples, channels, at)
        since += 1
        if sure >= FIGURE_SURE and lag:
            span = lag
        else:
            span = VECTOR_POINTS // 2
        span = max(64, min(span, total - at))
        if span < 64:
            break
        step = max(1, -(-span // VECTOR_POINTS))      # ceil
        points = span // step
        if points < 8:
            break
        row = array("h", [0]) * (points * 2)
        for point in range(points):
            index = (at + point * step) * channels
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
            # Only the bands this row actually has. Every row analyse
            # produces is the full width, and this used to assume that -
            # a short one raised an IndexError out of set_frames, which
            # is not a frame that fails to draw but a window that does
            # not open. Nothing should be able to take the viewer down by
            # being half a row.
            here = [row[i] for i in chosen if i < len(row)]
            made[index] = max(here) if here else 0.0
        out.append(made)
    return out
