"""Turn an audio file into something worth looking at, once, cheaply.

The spectrum is computed ahead of time rather than tapped live (Qt6 has no
audio probe), so the paint loop only interpolates between rows already in
memory. A 2048-point transform at 48 kHz, fifteen frames a second, in
third-octave bands; the drums get a finer pass of their own. The work runs
in worker processes, off the GUI thread.
"""

from __future__ import annotations

import cmath
import logging
import math
from array import array
from operator import mul
from typing import List, Optional, Sequence, Tuple

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

#: Points per analysis window: at 48 kHz, 23.4 Hz a bin, enough to separate 73
#: Hz from 120 Hz. Smaller windows put the bass in one or two bins and every
#: bass band moves together.
WINDOW = 2048

#: What the decoder is asked for: 48 kHz, because the dial scene has bands at
#: 18 and 22 kHz.
DECODE_RATE = 48000

#: Analyses per second of audio; the display interpolates at thirty, so fifteen
#: looks the same and costs less.
RATE = 15

#: Bands on screen: third-octave centres from 50 Hz to 20 kHz, as a graphic
#: equaliser shows, all below the decoder's Nyquist limit.
CENTRES = (50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
           1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000,
           10000, 12500, 16000, 20000)
BANDS = len(CENTRES)

#: Refuse to analyse more than this; a long podcast is not worth the wait.
MAX_SECONDS = 900
#: How much of the progress bar is decoding, most of the wait on a long track;
#: the analysis is the rest.
DECODE_SHARE = 0.5


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

    A real signal's spectrum is conjugate-symmetric and an imaginary one's
    conjugate-antisymmetric, so two frames ride in one transform, one in
    each part, and are separated afterwards:

    A[k] = (Z[k] + conj(Z[N-k])) / 2 B[k] = (Z[k] - conj(Z[N-k])) / 2j

    The transform is three quarters of an analysis's cost, and this halves
    how many are needed, exactly. Only the first half of each spectrum is
    returned, which is all the bands read.
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
    """(low bin, high bin) per third-octave band: from centre/2**(1/6) to
    centre*2**(1/6). Above the Nyquist limit the bands collapse onto the top
    bin, since there is nothing there.
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


#: A second, finer pass, only for finding drums. At fifteen frames a second a
#: frame is longer than a drum hit, so a kick and the snare after it cannot be
#: told apart. This pass halves the rate, then takes 512-point windows (21 ms,
#: as fine as 1024 at 48 kHz for half the work) sixty times a second; shorter
#: windows start their lowest band above where a kick lives.
ONSET_DECIMATE = 2
ONSET_WINDOW = 512
ONSET_RATE = 60
#: The coarse bands this pass reports, as fractions of the spectrum: enough to
#: tell a kick from a snare from a hat.
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
    """The track as one channel at half the rate, for the drum pass. An onset
    only asks whether something started and roughly where in the spectrum,
    and halving the rate halves the window for the same span of time. Summed
    to mono, then averaged in pairs: a crude low-pass that serves as the
    anti-aliasing filter, losing only the top of the hats.
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
    """Log-spaced bin ranges, from the first bin to the last: each band the
    same musical width, written as a ratio raised to a power. Halving from
    the top made the lowest band the widest, lumping kick and snare
    together.
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
    """Coarse band energies at sixty a second, for finding drums, not drawing:
    twelve bands and short windows answer whether something started and
    where, and a finer answer says nothing more.
    """
    if not samples or sample_rate <= 0:
        return []
    channels = max(1, channels)
    total = len(samples) // channels
    if total <= ONSET_WINDOW or total / sample_rate > MAX_SECONDS:
        return []
    # One channel at half the rate, made once, so the pass reads a plain array
    # (de-interleaving every time was a fifth of its cost).
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
    # Linear, scaled to the loudest band in the track, not decibels: in
    # decibels a kick's hundredfold difference between bottom and top is twenty
    # units of seventy, and kick, snare and hat profiles agreed to within four
    # per cent.
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
    """Band energies per frame, each 0 to 1.

    ``samples`` is interleaved 16-bit PCM as an array("h"). ``should_stop``
    is polled and, if true, the work is abandoned and an empty list
    returned; ``on_progress`` gets a fraction. The numbers are stretched to
    fill the display, so a bar's height is not a level: pass a dict as
    ``calibration`` to receive what converts them back to decibels.
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
    # Checked about forty times over the track: prompt enough to quit, rare
    # enough to cost nothing.
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
        # Two frames at a time, two real transforms in one complex one; the
        # last may have no partner and is transformed alone.
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
                # Power summed across the band, as an equaliser reads, not its
                # loudest bin.
                power = 0.0
                for bin_index in range(lo, hi):
                    value = spectrum[bin_index]
                    power += value.real * value.real + value.imag * value.imag
                rms = math.sqrt(power / max(1, hi - lo))
                # Decibels, floored at -70: loudness is logarithmic.
                db = 20.0 * math.log10(rms + 1e-9)
                # A 55 dB window rather than 70: seventy put ordinary music in
                # the top third and nothing seemed to move.
                row[band] = max(0.0, (db + RANGE_DB) / RANGE_DB)
            frames.append(row)
        at += hop * len(spectra)

    # Normalised to the track, so a quiet recording and a loud one both fill
    # the strip.
    if frames:
        everything = sorted(value for row in frames for value in row)
        high = everything[int(len(everything) * 0.97)] or 1.0
        low = everything[int(len(everything) * 0.30)]
        floor = min(low, high * 0.5)
        reach = max(0.05, high - floor)
        for row in frames:
            for index in range(len(row)):
                # Stretch the band the music occupies across the whole height,
                # then bend it so quiet detail shows.
                scaled = (row[index] - floor) / reach
                scaled = max(0.0, min(1.0, scaled))
                row[index] = scaled ** GAMMA
        if calibration is not None:
            # Enough to undo all of it: raw = floor + reach * shown ** (1/g),
            # and dB = raw * RANGE_DB - RANGE_DB.
            calibration.update({"floor": floor, "reach": reach,
                                "gamma": GAMMA, "range_db": RANGE_DB})
    return frames


#: Readings of the track's shape a second. About two seconds of road is in
#: view, so eight a second give fifteen points to bend through; four drew a
#: polygon.
CONTOUR_RATE = 8


def balance(samples: Sequence, sample_rate: int, channels: int = 2,
            columns: int = 0) -> List[float]:
    """How far the mix sits to one side, -1 to +1, ``columns`` times a track:
    read off the samples, one frame in sixteen, so it arrives with the
    bands. See ``contour``.
    """
    if channels < 2 or columns <= 0 or not samples:
        return [0.0] * max(0, columns)
    pairs = len(samples) // channels
    lean = [0.0] * columns
    for index in range(columns):
        start = index * pairs // columns
        end = max(start + 1, (index + 1) * pairs // columns)
        left = right = 0
        for frame in range(start, end, 16):
            at = frame * channels
            left += abs(samples[at])
            right += abs(samples[at + 1])
        total = left + right
        lean[index] = 0.0 if total <= 0 else (right - left) / total
    return lean


def contour(frames: Sequence, vectors: Optional[Sequence] = None,
            calibration: Optional[dict] = None,
            lean: Optional[Sequence] = None) -> dict:
    """The track's shape as the thing that drives a road: built before play,
    with amplitude as the incline and the channel balance as the curve.

    ``loud`` is the amplitude envelope, the same one the waveform is drawn
    from. ``lean`` is how far the mix sits to one side, -1 to +1, from the
    scope's traces, thinned to one trace in four and one point in eight: two
    thousand readings a track, the same answer to a hundredth.
    """
    loud = outline(frames, calibration, columns=max(
        1, int(len(frames) / max(1, RATE) * CONTOUR_RATE)))
    low = bass_line(frames, len(loud))
    if lean is not None and loud:
        # Worked out from the samples already. See ``balance``.
        given = list(lean)[:len(loud)]
        given += [0.0] * (len(loud) - len(given))
        return {"loud": loud, "lean": given, "low": low,
                "rate": float(CONTOUR_RATE)}
    lean = [0.0] * len(loud)
    if vectors and loud:
        span = len(vectors) / max(1e-6, RATE)
        for index in range(len(loud)):
            when = (index + 0.5) / CONTOUR_RATE
            at = min(len(vectors) - 1, int(when / max(1e-6, span)
                                           * len(vectors)))
            trace = vectors[at]
            left = right = 0
            for step in range(0, len(trace) - 1, 16):
                left += abs(trace[step])
                right += abs(trace[step + 1])
            total = left + right
            lean[index] = 0.0 if total <= 0 else (right - left) / total
    return {"loud": loud, "lean": lean, "low": low,
            "rate": float(CONTOUR_RATE)}


#: The bands a bass line is in, from the bottom.
BASS_BANDS = 3


def bass_line(frames: Sequence, columns: int) -> List[float]:
    """How much bass there is across the track, 0 to 1 a column: the lowest
    bands averaged per column, scaled so the 95th percentile is full.
    """
    if not frames or columns <= 0:
        return []
    out = []
    count = len(frames)
    for index in range(columns):
        start = index * count // columns
        end = max(start + 1, (index + 1) * count // columns)
        total = 0.0
        for frame in frames[start:end]:
            total += sum(frame[:BASS_BANDS]) / max(1, min(BASS_BANDS, len(frame)))
        out.append(total / (end - start))
    ordered = sorted(out)
    top = ordered[int(len(ordered) * 0.95)] if ordered else 0.0
    if top <= 0.0:
        return [0.0] * columns
    return [min(1.0, value / top) for value in out]


#: Every analysis still in flight. A pane destroyed as a child never gets its
#: own DeferredDelete, and Qt aborts if a running QThread is destroyed, so this
#: is the backstop at quit.
_LIVE: "set" = set()

#: Threads that would not stop in time, kept forever: destroying a running
#: QThread is fatal and cannot be caught, so a thread that outlasts the wait is
#: cut loose and referenced here while it finishes. Every step it takes is
#: bounded.
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


#: Whether the analysis runs in processes of its own. See _worker.
WORKERS = True

#: How many traces go back from a worker at a time. See _worker.
SLICE = 400


def _whole_contour(frames, samples, rate: int, channels: int,
                   calibration) -> Optional[dict]:
    """The road's whole shape, sent with the bands, so the bends do not arrive
    seconds later from the scope's traces and start mid-song.
    """
    if not frames:
        return None
    columns = max(1, int(len(frames) / max(1, RATE) * CONTOUR_RATE))
    return contour(frames, None, calibration,
                   lean=balance(samples, rate, channels, columns))


def _worker(connection, rate: int, channels: int, part: str) -> None:
    """One part of the analysis, in a process of its own.

    A thread takes turns with the drawing for the interpreter: on a seven
    and a half minute track the picture came up late and played at 21 to 26
    frames a second with a hitch every second until the rest finished. A
    process has its own interpreter.

    ``part`` is ``"picture"`` (the bands, then the traces and the beat
    maps), ``"drums"`` (the finer pass for the kit), or ``"harmony"`` (key,
    tuning and chords; see harmony). They run side by side.
    """
    import os

    try:
        # Below the app, so a busy machine takes its time from this.
        os.nice(10)
    except (AttributeError, OSError):
        pass
    try:
        samples = array("h")
        samples.frombytes(connection.recv_bytes())
        import beatmap

        if part == "picture":
            calibration: dict = {}
            said = [-1.0]

            def progress(fraction: float) -> None:
                if fraction - said[0] >= 0.01:
                    said[0] = fraction
                    connection.send(("progress", fraction))

            frames = analyse(samples, rate, channels, on_progress=progress,
                             calibration=calibration)
            connection.send(("bands", (frames, calibration,
                                       _whole_contour(frames, samples, rate,
                                                      channels, calibration))))
            # The beats straight after: they take milliseconds, and the game
            # lays its road on them.
            beats = beatmap.build(frames, RATE)
            connection.send(("beats", beats))
            # In slices: taking 27 MB of traces apart in one piece held the
            # other interpreter for 18 ms, a dropped frame.
            for kind, rows in (("shapes", traces(samples, rate, channels)),
                               ("vectors", vector_traces(samples, rate,
                                                         channels))):
                for start in range(0, len(rows), SLICE):
                    connection.send((kind, rows[start:start + SLICE]))
            connection.send(("done", beats))
        elif part == "harmony":
            import harmony

            connection.send(("harmony", harmony.analyse(samples, rate,
                                                        channels)))
        else:
            fine = onset_frames(samples, rate, channels)
            kit = beatmap.elements(fine, ONSET_RATE) if fine else None
            connection.send(("elements", kit))
            # And the drums' tempo, beat and pattern, worked out here rather
            # than on the drawing thread. See trackstyle.
            connection.send(("rhythm", _rhythm_of(fine, kit)))
    except Exception as exc:      # noqa: BLE001 - reported, not raised
        try:
            connection.send(("failed", f"{type(exc).__name__}: {exc}"))
        except Exception:      # noqa: BLE001 - nobody left to tell
            pass
    finally:
        connection.close()


def _rhythm_of(fine, kit) -> Optional[dict]:
    """The drums' tempo, beat and pattern: see trackstyle.rhythm_of."""
    if not fine or not kit:
        return None
    import trackstyle

    try:
        return trackstyle.rhythm_of(kit)
    except Exception as exc:      # noqa: BLE001 - a style, not the mail
        log.info("Could not read the drums' rhythm (%s).", exc)
        return None


def worker_check(timeout: float = 30.0) -> str:
    """Run a second of audio through a worker process, for the self-test: in
    the built app a worker is the app itself relaunched, which can work from
    source and fail from the bundle.
    """
    import math
    import multiprocessing
    import time

    rate = DECODE_RATE
    pcm = array("h")
    for index in range(rate):
        value = int(12000 * math.sin(2 * math.pi * 220 * index / rate))
        pcm.append(value)
        pcm.append(value)
    context = multiprocessing.get_context("spawn")
    ours, theirs = context.Pipe()
    started = time.monotonic()
    process = context.Process(target=_worker,
                              args=(theirs, rate, 2, "picture"), daemon=True)
    process.start()
    theirs.close()
    try:
        ours.send_bytes(pcm)
        while time.monotonic() - started < timeout:
            if not ours.poll(0.1):
                if not process.is_alive():
                    raise RuntimeError("the worker died before it answered")
                continue
            kind, payload = ours.recv()
            if kind == "bands":
                frames = payload[0]
                if not frames:
                    raise RuntimeError("the worker sent no frames")
                return (f"in a process of its own, "
                        f"{time.monotonic() - started:.1f} s for a second "
                        f"of audio")
            if kind == "failed":
                raise RuntimeError(payload)
        raise RuntimeError("the worker did not answer")
    finally:
        if process.is_alive():
            process.terminate()
        process.join(2.0)


class _AnalysisThread(_QThread_base):
    """The analysis off the UI thread, reporting through signals: a QThread has
    no event loop once run() returns, so a posted callback never fires,
    while a cross-thread signal is queued to the receiver's thread.
    """

    done = _Signal(object)
    #: The bands as soon as they exist: most scenes need only these, so the
    #: picture comes up before the waveform and the X-Y traces are worked out.
    bands = _Signal(object)
    #: The drums, after the rest: their finer pass costs twice the display
    #: frames, and no scene needs them in its first second.
    elements = _Signal(object)
    #: Key, tuning and chords, from their own pass (see harmony); only the
    #: game's sounds need them, so they come last.
    harmony = _Signal(object)
    #: The beat maps, straight after the bands.
    beats = _Signal(object)
    #: The drums' tempo, beat and pattern, with the kit. See
    #: trackstyle.rhythm_of.
    rhythm = _Signal(object)
    failed = _Signal(str)
    progress = _Signal(float)

    def __init__(self, samples, rate: int, channels: int,
                 wants_elements: bool = True,
                 wants_harmony: bool = False) -> None:
        super().__init__()
        self._samples = samples
        self._rate = rate
        self._channels = channels
        self._stop = False
        self._wants_elements = wants_elements
        self._wants_harmony = wants_harmony
        #: The worker processes, while there are any. See _worker.
        self._processes: list = []

    def stop(self) -> None:
        self._stop = True

    def run(self) -> None:
        if WORKERS:
            try:
                if self._run_in_workers():
                    return
            except Exception as exc:      # noqa: BLE001 - there is a fallback
                log.info("Analysing on a thread instead (%s).", exc)
            finally:
                self._end_workers()
            if self._stop:
                return
        self._run_here()

    def _end_workers(self) -> None:
        """Every worker gone, whatever state it was in."""
        processes, self._processes = self._processes, []
        for process in processes:
            try:
                if process.is_alive():
                    process.terminate()
                process.join(2.0)
                if process.is_alive():
                    process.kill()
                    process.join(1.0)
            except Exception:      # noqa: BLE001 - already gone
                pass

    def _run_in_workers(self) -> bool:
        """The analysis in worker processes, relayed to the signals. False when
        no worker started and nothing was sent, so the caller can do it
        here. Once anything has been handed over, a dying worker is reported
        as a failure, not restarted, so nothing arrives twice.
        """
        import multiprocessing
        from multiprocessing.connection import wait

        context = multiprocessing.get_context("spawn")
        parts = (["picture"] + (["drums"] if self._wants_elements else [])
                 + (["harmony"] if self._wants_harmony else []))
        running: dict = {}
        for part in parts:
            ours, theirs = context.Pipe()
            process = context.Process(
                target=_worker, args=(theirs, self._rate, self._channels, part),
                name=f"mail-manager-{part}", daemon=True)
            process.start()
            theirs.close()
            self._processes.append(process)
            running[ours] = part
        for ours in running:
            # The samples' own buffer, not a copy: a long track is tens of
            # megabytes.
            ours.send_bytes(self._samples)
        frames = calibration = None
        shapes: list = []
        vectors: list = []
        held = None
        held_harmony = None
        held_rhythm = None
        sent_bands = False
        while running:
            if self._stop:
                return True
            for ours in wait(list(running), timeout=0.1):
                part = running[ours]
                try:
                    kind, payload = ours.recv()
                except (EOFError, OSError):
                    kind, payload = "failed", f"the {part} worker stopped"
                if self._stop:
                    return True
                if kind == "progress":
                    self.progress.emit(float(payload) * 0.85)
                    continue
                if kind in ("shapes", "vectors"):
                    (shapes if kind == "shapes" else vectors).extend(payload)
                    continue
                if kind == "beats":
                    self.beats.emit(payload)
                    continue

                if kind == "bands":
                    frames, calibration, _shape = payload
                    self.bands.emit(payload)
                    sent_bands = True
                    if held is not None:
                        self.elements.emit(held)
                        held = None
                    if held_harmony is not None:
                        self.harmony.emit(held_harmony)
                        held_harmony = None
                    if held_rhythm is not None:
                        self.rhythm.emit(held_rhythm)
                        held_rhythm = None
                    continue
                if kind == "done":
                    self.done.emit((frames, shapes, vectors, calibration,
                                    payload))
                elif kind == "elements":
                    if payload is not None:
                        # Not before the bands: the pane has nothing to hang
                        # the kit on until it has the frames.
                        if sent_bands:
                            self.elements.emit(payload)
                        else:
                            held = payload
                    # Not the drums' last word: the rhythm follows it.
                    continue
                elif kind == "rhythm":
                    if payload is not None:
                        if sent_bands:
                            self.rhythm.emit(payload)
                        else:
                            held_rhythm = payload
                elif kind == "harmony":
                    if payload is not None:
                        # Not before the bands either: see elements.
                        if sent_bands:
                            self.harmony.emit(payload)
                        else:
                            held_harmony = payload
                elif kind == "failed":
                    if part == "harmony":
                        log.info("Could not hear the harmony (%s).", payload)
                    elif part != "picture":
                        log.info("Could not pick the drums out (%s).", payload)
                    elif not sent_bands:
                        # Nothing has gone out: do it here instead.
                        log.info("The analysis worker failed (%s).", payload)
                        return False
                    else:
                        self.failed.emit(str(payload))
                        return True
                del running[ours]
                ours.close()
        return True

    def _run_here(self) -> None:
        """The whole analysis on this thread, for when no worker will start."""
        try:
            calibration: dict = {}
            frames = analyse(self._samples, self._rate, self._channels,
                             should_stop=lambda: self._stop,
                             on_progress=lambda f: self.progress.emit(f * 0.85),
                             calibration=calibration)
            if self._stop:
                return
            # Out with it: every scene but the oscilloscope can draw from here,
            # and the two passes below take two thirds as long again.
            self.bands.emit((frames, calibration, _whole_contour(
                frames, self._samples, self._rate, self._channels,
                calibration)))
            import beatmap
            early = beatmap.build(frames, RATE)
            self.beats.emit(early)
            # The waveform the oscilloscope draws, on the bands' schedule so
            # one index reads both.
            shapes = traces(self._samples, self._rate, self._channels,
                            should_stop=lambda: self._stop)
            vectors = vector_traces(self._samples, self._rate, self._channels,
                                    should_stop=lambda: self._stop)
            # Where the beats are, from the frames just computed: milliseconds
            # of work, and the strobe then knows the whole track before a note
            # plays.
            beats = early
        except Exception as exc:      # noqa: BLE001
            if not self._stop:
                self.failed.emit(str(exc))
            return
        if self._stop:
            return
        self.done.emit((frames, shapes, vectors, calibration, beats))

        # Now the slow part, with the picture already up, and only if someone
        # is waiting for it.
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
            found_rhythm = _rhythm_of(fine, kit)
            if found_rhythm is not None and not self._stop:
                self.rhythm.emit(found_rhythm)
        if self._stop or not self._wants_harmony:
            return
        try:
            import harmony

            found = harmony.analyse(self._samples, self._rate,
                                    self._channels,
                                    should_stop=lambda: self._stop)
        except Exception as exc:      # noqa: BLE001 - sounds, not the mail
            log.info("Could not hear the harmony (%s).", exc)
            return
        if not self._stop and found is not None:
            self.harmony.emit(found)


class _Analysis(QObject_base):
    """Owns the decoder and the analysis thread: one object for the caller to
    keep alive, and one to cancel.
    """

    def __init__(self, decoder, on_done, on_fail, on_progress=None,
                 on_elements=None, on_bands=None, on_harmony=None,
                 on_beats=None, on_rhythm=None) -> None:
        super().__init__()
        self._decoder = decoder
        self._on_done = on_done
        self._on_bands = on_bands
        self._on_harmony = on_harmony
        self._on_beats = on_beats
        self._on_rhythm = on_rhythm
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
        self._release_decoder()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.stop()
            if thread.isRunning():
                # A running QThread destroyed by Qt is fatal, so wait for it.
                if not thread.wait(4000):
                    # It did not stop. Dropping the last reference would let Qt
                    # destroy it while it runs, which aborts the process, so it
                    # is kept and finishes on its own.
                    log.warning(
                        "An analysis did not stop in time. Letting it "
                        "finish.")
                    if thread not in _ABANDONED:
                        _ABANDONED.append(thread)
        _LIVE.discard(self)

    def _release_decoder(self) -> None:
        """Stop the decoder and delete it: its signals hold closures that hold
        it and the decoded track, a cycle through Qt the collector cannot
        see.
        """
        decoder, self._decoder = self._decoder, None
        if decoder is not None:
            try:
                # Quiet first: stopping a decoder says "finished" again, which
                # started the analysis a second time.
                decoder.blockSignals(True)
                decoder.stop()
                decoder.deleteLater()
            except Exception:      # noqa: BLE001 - already gone
                pass

    @property
    def cancelled(self) -> bool:
        return self._stop

    # -- the work ---------------------------------------------------------
    def start_analysis(self, samples, rate: int, channels: int) -> None:
        if self._stop:
            return
        thread = _AnalysisThread(samples, rate, channels,
                                 self._on_elements is not None,
                                 self._on_harmony is not None)
        thread.done.connect(self._finished)
        if self._on_bands is not None:
            thread.bands.connect(self._early)
        thread.failed.connect(self._failed)
        if self._on_elements is not None:
            thread.elements.connect(self._kit)
        if self._on_harmony is not None:
            thread.harmony.connect(self._harmonised)
        if self._on_beats is not None:
            thread.beats.connect(self._beaten)
        if self._on_rhythm is not None:
            thread.rhythm.connect(self._rhythmic)
        thread.finished.connect(self._thread_done)
        if self._on_progress is not None:
            thread.progress.connect(self._report)
        self._thread = thread
        thread.start()

    def _early(self, result) -> None:
        """The bands, before the rest of the analysis has finished."""
        if not self._stop and self._on_bands is not None:
            self._on_bands(result)

    def _kit(self, elements) -> None:
        """The drums, once the finer pass has finished."""
        if not self._stop and self._on_elements is not None:
            self._on_elements(elements)

    def _rhythmic(self, found) -> None:
        """The drums' own tempo and beat, with the kit."""
        if not self._stop and self._on_rhythm is not None:
            self._on_rhythm(found)

    def _beaten(self, maps) -> None:
        """The beat maps, straight after the bands."""
        if not self._stop and self._on_beats is not None:
            self._on_beats(maps)

    def _harmonised(self, found) -> None:
        """The key and the chords, last of all."""
        if not self._stop and self._on_harmony is not None:
            self._on_harmony(found)

    def _report(self, fraction: float) -> None:
        """The analysis's progress, after the decoding's share of the bar."""
        if not self._stop and self._on_progress is not None:
            self._on_progress(DECODE_SHARE + (1.0 - DECODE_SHARE) * fraction)

    def decoding(self, fraction: float) -> None:
        """How far the decoding has got: the first share of the bar."""
        if not self._stop and self._on_progress is not None:
            self._on_progress(DECODE_SHARE * max(0.0, min(1.0, fraction)))

    def _finished(self, frames) -> None:
        # Still in _LIVE: the thread goes on to find the drums, and collecting
        # this object while it runs takes the process down. It leaves with the
        # thread, below.
        if not self._stop:
            self._on_done(frames)

    def _thread_done(self) -> None:
        """The thread's own signal: run() has returned, so the decoder and the
        track go. Qt sends this just before the thread ends, and the last
        reference to it can be in the decoder's callbacks, so it is waited
        for first (a moment at most).
        """
        if self._thread is not None:
            self._thread.wait()
            self._thread._samples = None
        _LIVE.discard(self)
        self._release_decoder()

    def _failed(self, detail: str) -> None:
        # Sent from the thread while it runs; what it holds is let go in
        # _thread_done.
        if not self._stop:
            self._on_fail(detail)


def decode(path, on_done, on_fail, on_progress=None,
           on_elements=None, on_bands=None,
           on_harmony=None, on_beats=None,
           on_rhythm=None) -> Optional[object]:
    """Decode a file to PCM with Qt, then hand the frames back. Returns a
    handle the caller must keep alive and may ``cancel()``. Qt decodes on
    its own thread and the arithmetic runs on another; the UI thread only
    copies buffers.
    """
    try:
        from PySide6.QtCore import QLoggingCategory, QUrl
        from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
    except ImportError:
        on_fail("audio decoding is unavailable in this build")
        return None

    # FFmpeg narrates every file, and an AAC track makes it complain about the
    # samples it skipped (the encoder delay, which it is meant to skip), so the
    # category is quietened once.
    _quieten()

    decoder = QAudioDecoder()
    wanted = QAudioFormat()
    wanted.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    # Two channels, so the scope can plot one against the other: oscilloscope
    # music lives in the difference between left and right.
    wanted.setChannelCount(2)
    wanted.setSampleRate(DECODE_RATE)
    decoder.setAudioFormat(wanted)

    collected = array("h")
    state = {"rate": DECODE_RATE, "channels": 2, "too_long": False,
             "said": -1.0}

    def buffer_ready() -> None:
        buffer = decoder.read()
        if not buffer.isValid() or state["too_long"]:
            return
        fmt = buffer.format()
        state["rate"] = fmt.sampleRate() or DECODE_RATE
        state["channels"] = fmt.channelCount() or 1
        try:
            data = bytes(buffer.constData())
            chunk = array("h")
            chunk.frombytes(data[: (len(data) // 2) * 2])
            collected.extend(chunk)
        except Exception:      # noqa: BLE001 - a bad buffer is not fatal
            pass
        # Nothing past MAX_SECONDS is analysed, so nothing past it is kept: an
        # hour-long mix decoded whole and copied to every worker was gigabytes.
        # And progress every few per cent.
        whole = decoder.duration()
        if whole and whole > 0:
            through = buffer.startTime() / 1000.0 / whole
            if through - state["said"] >= 0.02:
                state["said"] = through
                handle.decoding(through)
        most = (MAX_SECONDS + 1) * state["rate"] * state["channels"]
        if len(collected) > most:
            state["too_long"] = True
            del collected[:]
            handle.cancel()
            on_fail(f"longer than {MAX_SECONDS // 60} minutes")

    def finished() -> None:
        # Once: a decoder can say it has finished more than once.
        if state["too_long"] or state.get("analysed"):
            return
        state["analysed"] = True
        # Off the UI thread: analysed here, a three-minute track stopped the
        # event loop for five seconds.
        handle.start_analysis(collected, state["rate"], state["channels"])

    handle = _Analysis(decoder, on_done, on_fail, on_progress, on_elements,
                       on_bands, on_harmony, on_beats, on_rhythm)
    decoder.bufferReady.connect(buffer_ready)
    decoder.finished.connect(finished)
    # The signal's name varies across Qt 6 releases, and a missing one is not
    # worth a crash.
    for name in ("errorOccurred", "error"):
        signal = getattr(decoder, name, None)
        if signal is not None and hasattr(signal, "connect"):
            def refused(*_args) -> None:
                handle.cancel()
                on_fail("this file will not decode")
            try:
                signal.connect(refused)
                break
            except (TypeError, RuntimeError):
                continue
    decoder.setSource(QUrl.fromLocalFile(str(path)))
    decoder.start()
    return handle


_QUIET = False


def _quieten() -> None:
    """Stop the FFmpeg backend narrating into the terminal: the container,
    every stream and, for AAC, the skipped encoder delay. Decoding is exact
    either way.
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


#: The dial scene's bands, which are not third-octave; they follow the meter it
#: copies.
DIAL_CENTRES = (73, 120, 300, 576, 1400, 2400, 6000, 9000, 18000, 22000)


#: The window of level the bands are spread across, in decibels, and the bend
#: applied so quiet detail shows.
RANGE_DB = 55.0
GAMMA = 0.72

#: The most points kept for one X-Y trace: a cap, and the memory knob (about 28
#: MB for a fifteen-minute track either way). A trace spans one figure (see
#: _figure_lag); 512 consecutive samples, the old span, were a third to a half
#: of a figure, so each frame drew a different fragment.
VECTOR_POINTS = 1024

#: How often the figure rate is measured, in traces: it belongs to the passage
#: (across one record it went 25 Hz, 200, 132, 123, 25, 104, 10.6, 50.5), so it
#: is followed. Fifteen traces is about a second.
FIGURE_EVERY = 30

#: The figure rates looked for, as lags at the decode rate: about 8 Hz to 200
#: Hz.
FIGURE_SLOWEST = 4000
FIGURE_FASTEST = 240

#: How much signal is searched for the rate, and how coarsely: on a copy
#: decimated by four, sixteen times cheaper and within a sample or two, then
#: refined at full rate.
FIGURE_LOOK = 8192
FIGURE_COARSE = 4

#: How near the best a shorter lag must score to be preferred: every multiple
#: of a figure's period fits equally well, and the shortest is the figure. See
#: _figure_lag.
FIGURE_PREFER = 0.93

#: Below this the passage has no figure (noise, a cymbal, silence), and the
#: fixed window is used.
FIGURE_SURE = 0.45

#: Points in one oscilloscope trace: enough at any width, about a megabyte for
#: a three-minute track.
TRACE_POINTS = 256


#: Columns the track outline is cut into: more than any window is wide, and a
#: few kilobytes in all.
OUTLINE_COLUMNS = 2000


def outline(frames: Sequence, calibration: Optional[dict] = None,
            columns: int = OUTLINE_COLUMNS) -> List[float]:
    """How loud the track is across its length, 0 to 1 a column: the shape
    above the seek bar.

    From the analysis's frames, not a second pass over the samples, which
    would cost as much as the analysis. The frames are stretched for the
    bars, which drew a mastered track as a solid block, so the calibration
    puts them back into decibels and then amplitude. Peak within a column,
    since transients make a waveform readable; scaled to the 98th
    percentile, so one clipped moment does not flatten the rest.
    """
    if not frames or columns <= 0:
        return []
    floor = (calibration or {}).get("floor")
    reach = (calibration or {}).get("reach")
    gamma = (calibration or {}).get("gamma") or GAMMA
    range_db = (calibration or {}).get("range_db") or RANGE_DB
    levels: List[float] = []
    for frame in frames:
        if not frame:
            levels.append(0.0)
            continue
        if floor is None or not reach:
            levels.append(0.35 * (sum(frame) / len(frame)) + 0.65 * max(frame))
            continue
        # Back out of the display stretch into decibels, then amplitude, and
        # the bands summed as power: how loud the moment was.
        power = 0.0
        for shown in frame:
            raw = floor + reach * (max(0.0, shown) ** (1.0 / gamma))
            amplitude = 10.0 ** ((raw * range_db - range_db) / 20.0)
            power += amplitude * amplitude
        levels.append(math.sqrt(power))
    out: List[float] = []
    for column in range(columns):
        low = column * len(levels) // columns
        high = max(low + 1, (column + 1) * len(levels) // columns)
        out.append(max(levels[low:high]))
    ranked = sorted(out)
    top = ranked[int(len(ranked) * 0.98)] or max(ranked) or 1.0
    return [min(1.0, value / top) for value in out]


def traces(samples: array, sample_rate: int, channels: int = 1,
           should_stop=None) -> List[array]:
    """One short slice of the actual waveform per frame, -1 to 1, decimated to
    a fixed number of points. Each starts at a nearby zero crossing, as a
    scope's trigger does, so the waveform does not slide from frame to
    frame.
    """
    if not samples or sample_rate <= 0:
        return []
    channels = max(1, channels)
    total = len(samples) // channels
    if total <= WINDOW:
        return []

    hop = max(1, sample_rate // RATE)
    # One cycle of 80 Hz: a bass waveform whole, a treble one as several
    # cycles.
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
    """How long one figure takes, in samples, and how sure that is. A scope
    record draws the same shape over and over, and a real scope's time base
    is turned until it stands still. The beam's path is (left, right), so
    this is an autocorrelation of both together. Searched on a copy
    decimated by four, then refined at full rate: straight, it took 21 ms a
    time.
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

    # sum(map(mul, ...)) so the work happens in C: 24.5 ms a measurement became
    # 11.5.
    scored = []
    best_score = 0.0
    lag = max(1, FIGURE_FASTEST // step)
    top = min(FIGURE_SLOWEST // step, count - 32)
    while lag <= top:
        span = count - lag
        here = (sum(map(mul, xs, xs[lag:])) + sum(map(mul, ys, ys[lag:])))
        # Against the energy of the overlapping part, so a long lag is not
        # punished for having less of itself to compare.
        score = here / max(1.0, energy * span / count)
        scored.append((lag, score))
        if score > best_score:
            best_score = score
        # Geometric: the range is twelve hertz to two hundred, and a fixed step
        # would spend itself at the slow end.
        lag += max(1, lag // 40)
    if not scored or best_score <= 0.0:
        return 0, 0.0

    # The shortest lag as good as the best, not the best: a repeating figure
    # fits equally well at every multiple of its period, and the top score came
    # back as 2859 for a figure of 953 samples, drawing three figures.
    best_lag = scored[-1][0]
    for lag, score in scored:
        if score >= best_score * FIGURE_PREFER:
            best_lag = lag
            best_score = score
            break

    # Back to full rate, searching half the gap to the neighbouring coarse lag:
    # the grid is geometric, so near 950 its rungs are ninety-five samples
    # apart.
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
    """Left against right, as oscilloscope music draws, interleaved x, y, x, y.
    A scope record puts a picture here; an ordinary stereo mix a blob that
    leans with the stereo image, as a vectorscope shows.

    One trace is one figure: the time base is measured from the record (see
    _figure_lag) and followed as it changes. A longer figure is thinned
    rather than cut short. No trigger: the position in the file is the
    position in the drawing.
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
    """The first rising zero crossing near ``at``, or ``at`` itself, so the
    waveform stands still.
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
    """Re-read frames against different centres: each wanted centre takes the
    loudest source band within a third of an octave, rather than analysing
    twice.
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
            # Only the bands this row has: a short row raised an IndexError out
            # of set_frames, and the window failed to open.
            here = [row[i] for i in chosen if i < len(row)]
            made[index] = max(here) if here else 0.0
        out.append(made)
    return out
