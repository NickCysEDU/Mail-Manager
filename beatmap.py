"""Where the beats are, worked out once before anything is played.

The strobe used to fire on a rule: if this band is louder than it was last
frame by more than a fixed amount, flash. Measured against a kick on its own
that rule fires about twice per beat, scattered across fifty milliseconds
either side of it. Measured against anything with sustained material in it -
a pad, a held chord, hats between the kicks - the frame-to-frame rise never
reaches the threshold at all, and sixteen seconds of a four-to-the-floor
track produced *one* flash. That is the inconsistency: not that it is
sometimes early, but that on most real music it is not there.

This finds the beats properly, the way onset detection is normally done, and
it does it once, off the analysis that has already been computed:

**Spectral flux.** How much the spectrum *rose* between one frame and the
next, summed across a range of bands. Rises only: a note ending is not an
onset. Summed across bands rather than watched in one, because a kick moves
several and noise moves one.

**A threshold that follows the music.** The flux is compared against the
median of the flux around it, not against a number chosen in advance, so one
sensitivity setting means the same thing in a sparse passage and a dense one.
This is the part that stops a busy mix starving the detector. It is not what
makes a quiet *recording* work - the analysis already normalises each track,
so that was never the problem it looked like.

**Sub-frame timing.** The analysis runs at fifteen frames a second, so a peak
is only located to the nearest sixty-six milliseconds, which is enough jitter
to see. Fitting a parabola through the peak and its neighbours puts it inside
about ten.

**A tempo, where there is one.** Onsets alone are uneven - a fill has more of
them, a held note has none - and lighting that follows them exactly looks
nervous. So the onset envelope is autocorrelated to find the beat period, the
phase is fitted against the onsets, and the beats handed back are the grid.
A track with a steady tempo gets rock-steady lighting that carries through
the bars where nothing was hit. Where no tempo is found - speech, ambient,
rubato - the onsets themselves are used, which is still far better than a
fixed threshold.

Four maps are built, one per thing the strobe can be told to listen to, so
switching between bass and treble is instant and each one is locked to what
it actually watches.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["Beat", "BeatMap", "SOURCES", "build", "flux", "onsets", "tempo_of"]

#: What the strobe can be told to listen to, and the slice of the band
#: range each one means, as fractions of the band count. The same splits
#: the meters use, so "Bass" means the same thing everywhere.
SOURCES: Dict[str, Tuple[float, float]] = {
    "Bass": (0.00, 0.16),
    "Mids": (0.16, 0.55),
    "Synths": (0.30, 0.72),
    "Treble": (0.55, 1.00),
}

#: Pulses worth looking for, in beats per minute. The top of the range
#: is well above any tempo anybody counts in, on purpose: what is being
#: found is the fastest steady pulse, not the tempo. A 120 BPM track with
#: hats between the kicks has an onset every eighth note, which is a
#: pulse of 240 - and with the range stopping at 190 there was no period
#: it could fit at all, so a perfectly regular track came back as having
#: no tempo. The preference for ordinary tempos below sorts out which
#: multiple to report, and the rate control thins whatever is found.
MIN_BPM = 66.0
MAX_BPM = 240.0

#: How far either side of a frame the local median is taken, in seconds.
#: About a second: long enough that one loud bar does not raise the bar for
#: itself, short enough to follow a track that gets louder.
WINDOW = 0.9

#: A peak has to beat the local median by this much of the local spread
#: before it counts, at the middle sensitivity.
BASE_K = 1.4

#: Nothing may fire twice inside this, whatever the settings say. Two
#: flashes 50ms apart are one flash with a stutter in it.
FLOOR_GAP = 0.09

#: How tightly the onsets have to bunch before the grid is trusted over
#: the onsets themselves.
#:
#: Measured rather than guessed, because the two populations turned out
#: to be much closer than they look. Two hundred and fifty candidate
#: periods are tried and the best kept, and taking the best of that many
#: tries inflates what pure chance can produce: twenty scattered onsets
#: score 0.36 to 0.61 this way, where a single try would average 0.20.
#: Measured on the weaker half of the track (see ``tempo_of``): ten
#: different minutes of noise reach 0.53 at most, and mixes at five
#: tempos start at 0.67. Failing it is not a failure - the onsets
#: themselves are still used, which is what a track with no steady tempo
#: deserves.
LOCK_SCORE = 0.60


@dataclass(frozen=True)
class Beat:
    """One flash: when, and how hard it was hit."""

    at: float
    strength: float


@dataclass(frozen=True)
class BeatMap:
    """Every beat found for one thing to listen to."""

    beats: Tuple[Beat, ...] = ()
    #: Zero when no tempo was found and the onsets are being used raw.
    bpm: float = 0.0
    #: Whether those beats are a grid or the onsets themselves.
    locked: bool = False

    def __bool__(self) -> bool:
        return bool(self.beats)

    def describe(self) -> str:
        if not self.beats:
            return "nothing to lock to"
        if self.locked:
            return f"{self.bpm:.0f} BPM"
        return f"{len(self.beats)} onsets, no steady tempo"

    def at_least(self, strength: float) -> Tuple[Beat, ...]:
        """The beats hit at least this hard, for the sensitivity control."""
        return tuple(b for b in self.beats if b.strength >= strength)


def flux(frames: Sequence, low: float, high: float) -> List[float]:
    """How much the spectrum rose per frame, over a slice of the bands.

    Rises only. A note stopping is not something to flash on, and counting
    falls as well turns every gap in the music into an onset.
    """
    if not frames:
        return []
    bands = len(frames[0])
    start = max(0, min(bands - 1, int(bands * low)))
    stop = max(start + 1, min(bands, int(math.ceil(bands * high))))
    out = [0.0]
    previous = frames[0]
    for frame in frames[1:]:
        total = 0.0
        for index in range(start, stop):
            rise = frame[index] - previous[index]
            if rise > 0.0:
                total += rise
        out.append(total / (stop - start))
        previous = frame
    return out


def _local(values: Sequence[float], index: int, reach: int) -> Tuple[float, float]:
    """The median around a point, and how spread out that neighbourhood is.

    The median rather than the mean, because the thing being measured is
    "what does this passage usually do" and a mean is dragged upwards by
    the very peaks it is supposed to be judging.
    """
    low = max(0, index - reach)
    high = min(len(values), index + reach + 1)
    window = sorted(values[low:high])
    if not window:
        return 0.0, 0.0
    middle = window[len(window) // 2]
    # The spread, as the distance from the median to the point three
    # quarters of the way up. Not the standard deviation: one crash cymbal
    # in a quiet passage doubles a standard deviation and the threshold
    # with it, so the passage goes dark for the second it needed most.
    upper = window[int(len(window) * 0.75)]
    return middle, max(1e-6, upper - middle)


def onsets(envelope: Sequence[float], rate: float,
           sensitivity: float = 0.5) -> List[Beat]:
    """Peaks in the flux that stand out from the flux around them.

    ``sensitivity`` runs 0 to 1: at 0 only what is unmistakable, at 1
    nearly anything. It moves the multiplier on the local spread, so it
    means the same thing on a quiet track as on a loud one - which is the
    entire point of measuring against the neighbourhood at all.
    """
    if len(envelope) < 3 or rate <= 0:
        return []
    reach = max(2, int(WINDOW * rate))
    k = BASE_K * (2.0 - 1.8 * max(0.0, min(1.0, sensitivity)))
    found: List[Beat] = []
    last_at = -99.0
    loudest = max(envelope) or 1.0
    for index in range(1, len(envelope) - 1):
        here = envelope[index]
        if here <= envelope[index - 1] or here < envelope[index + 1]:
            continue        # not a peak
        middle, spread = _local(envelope, index, reach)
        if here < middle + k * spread:
            continue
        # Where the peak really is, between the frames either side of it.
        # A fifteen-a-second analysis places it to sixty-six milliseconds
        # and a parabola through three points places it to about ten,
        # which is the difference between lighting that sits on the beat
        # and lighting that is nearly on it.
        before, after = envelope[index - 1], envelope[index + 1]
        divisor = before - 2.0 * here + after
        shift = 0.0 if divisor == 0 else 0.5 * (before - after) / divisor
        shift = max(-0.5, min(0.5, shift))
        at = (index + shift) / rate
        if at - last_at < FLOOR_GAP:
            continue
        found.append(Beat(at=at, strength=min(1.0, here / loudest)))
        last_at = at
    return found


def tempo_of(hits: Sequence[Beat]) -> Tuple[float, float, float]:
    """The tempo the onsets agree on, how strongly, and its phase.

    Worked in seconds rather than in frames, which is the whole reason
    this is not an autocorrelation. The analysis runs at fifteen frames a
    second, so a lag has to be a whole number of frames, and at 120 BPM
    the beat is 7.5 of them - a period that simply cannot be expressed.
    Autocorrelating gave 82 BPM for a 120 BPM click track, every time,
    because the nearest representable answers were 112 and 129 and some
    unrelated lag scored better than either.

    Instead each candidate period is scored by how tightly the onsets
    bunch when folded into it. Every onset is turned into an angle - how
    far through the period it falls - and those angles are added as unit
    vectors. Onsets that all land on the beat point the same way and sum
    to nearly their own number; onsets scattered through the bar cancel
    out. The length of that sum, over the total, is the score, and its
    direction is the phase. Periods are then free to be any length at all.
    """
    if len(hits) < 6:
        return 0.0, 0.0, 0.0
    best = (0.0, 0.0, 0.0)
    # Half a BPM apart, which is finer than anybody can hear a strobe be
    # wrong over the length of a track.
    steps = int((MAX_BPM - MIN_BPM) * 2) + 1
    for step in range(steps):
        bpm = MIN_BPM + step * 0.5
        period = 60.0 / bpm
        score, phase = _fit(hits, period)
        # Everything that fits a period also fits half of it, and a
        # quarter, so the raw score cannot tell 75 BPM from 150. A gentle
        # preference for ordinary tempos breaks the tie the way a person
        # would: a track is far likelier to be at 120 than at 60 or 240.
        score *= math.exp(-((math.log(bpm / 120.0)) ** 2) / 0.72)
        if score > best[0]:
            best = (score, bpm, phase)
    if not best[1]:
        return 0.0, 0.0, 0.0

    # The score handed back is not the fit to the whole track: it is the
    # fit to whichever half of it agrees less.
    #
    # Two hundred and fifty periods are tried and the best kept, and the
    # best of that many tries scores respectably on material with no beat
    # in it at all - measured, a minute of noise fits some period or
    # other at 0.61, against 0.70 for a real four-to-the-floor mix, which
    # is not a gap anybody can put a threshold in. A tempo that is really
    # there is there in both halves of the track; one that came out of the
    # search is carried by one half. Split that way the two populations
    # separate properly: noise reaches 0.53 and music starts at 0.67.
    period = 60.0 / best[1]
    middle = len(hits) // 2
    early, _ = _fit(hits[:middle], period)
    late, _ = _fit(hits[middle:], period)
    return best[1], min(early, late), best[2]


def _fit(hits: Sequence[Beat], period: float) -> Tuple[float, float]:
    """How tightly these onsets bunch when folded into a period, and where.

    Every onset becomes an angle - how far through the period it falls -
    and those angles are added as unit vectors. Onsets that all land on
    the beat point the same way and sum to nearly their own number; onsets
    scattered through the bar cancel out. The length of that sum over the
    total is the fit; its direction is the phase.
    """
    if not hits or period <= 0:
        return 0.0, 0.0
    x = y = 0.0
    total = 0.0
    for beat in hits:
        weight = max(0.05, beat.strength)
        angle = (beat.at % period) / period * math.tau
        x += weight * math.cos(angle)
        y += weight * math.sin(angle)
        total += weight
    if total <= 0:
        return 0.0, 0.0
    phase = math.atan2(y, x) / math.tau * period
    return math.hypot(x, y) / total, phase % period


def _lock_bar(count: int) -> float:
    """How well a tempo has to fit before it is believed, for this many
    onsets.

    A fixed number will not do. The score is how tightly the onsets bunch
    when folded into a period, and a handful of onsets bunch respectably
    by pure chance - the expected score for `n` scattered ones is about
    the square root of pi over four n, which for a dozen is a third. So
    twelve random noise peaks "found" a tempo of 84 BPM and the strobe
    locked to it. The bar is raised to well clear of chance, and it comes
    down as the evidence comes in.
    """
    if count < 8:
        return 2.0        # unreachable: too few to be sure of anything
    return max(LOCK_SCORE, 1.9 * math.sqrt(math.pi / (4.0 * count)))


def _grid(beats: Sequence[Beat], bpm: float, span: float,
           phase: float) -> List[Beat]:
    """A steady grid at this tempo and phase, across the whole track.

    Laid down everywhere, not only where something was hit, so the bars
    where the drummer left a gap are still lit - which is the difference
    between lighting a room and following a waveform.
    """
    period = 60.0 / bpm
    if period <= 0 or span <= 0:
        return []
    phase = phase % period

    # How hard each grid line was hit, taken from the nearest onset. A
    # line with nothing near it still fires, softly, which is what keeps
    # lighting going through a held chord.
    out: List[Beat] = []
    index = 0
    at = phase
    while at <= span:
        nearest = 0.0
        while index < len(beats) and beats[index].at < at - period * 0.5:
            index += 1
        look = index
        while look < len(beats) and beats[look].at <= at + period * 0.5:
            nearest = max(nearest, beats[look].strength)
            look += 1
        # Floored high enough that every line on the grid fires at the
        # middle sensitivity. A grid exists so the lighting is even; one
        # whose quiet lines are gated out is an uneven grid, which is the
        # worst of both. Turning sensitivity down then keeps the lines
        # that were actually hit hard, which is a musical thing to ask
        # for and still lands on the beat.
        out.append(Beat(at=at, strength=max(0.55, nearest)))
        at += period
    return out


def build(frames: Sequence, rate: float,
          sensitivity: float = 0.5) -> Dict[str, BeatMap]:
    """A map per source, from frames the analysis has already produced.

    Costs one pass over the frames per source and an autocorrelation of
    each envelope - milliseconds against the seconds the analysis itself
    takes, and it happens in the same worker, so nothing waits on it that
    was not already waiting.
    """
    maps: Dict[str, BeatMap] = {}
    if not frames or rate <= 0:
        return {name: BeatMap() for name in SOURCES}
    span = len(frames) / rate
    for name, (low, high) in SOURCES.items():
        envelope = flux(frames, low, high)
        hits = onsets(envelope, rate, sensitivity)
        if len(hits) < 6:
            maps[name] = BeatMap(beats=tuple(hits))
            continue
        bpm, score, phase = tempo_of(hits)
        if bpm and score >= _lock_bar(len(hits)):
            grid = _grid(hits, bpm, span, phase)
            if len(grid) >= 4:
                maps[name] = BeatMap(beats=tuple(grid), bpm=bpm, locked=True)
                continue
        maps[name] = BeatMap(beats=tuple(hits), bpm=bpm)
    return maps


def next_after(beats: Sequence[Beat], when: float) -> Optional[Beat]:
    """The first beat at or after a moment, by binary search."""
    low, high = 0, len(beats)
    while low < high:
        middle = (low + high) // 2
        if beats[middle].at < when:
            low = middle + 1
        else:
            high = middle
    return beats[low] if low < len(beats) else None
