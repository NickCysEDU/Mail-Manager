"""Where the beats are, worked out once before anything plays.

A fixed rise-per-frame rule fired twice a beat on a lone kick and almost
never on real music with pads, chords or hats in it. This uses standard
onset detection on the analysis already computed:

**Spectral flux.** How much the spectrum rose between frames, summed across
bands; rises only, since a note ending is not an onset.

**A threshold that follows the music.** The flux is compared against the
local median, so one sensitivity means the same in sparse and dense
passages.

**Sub-frame timing.** A parabola through the peak and its neighbours places
it to about ten milliseconds rather than the analysis's sixty-six.

**A tempo, where there is one.** The onsets are folded to find the beat
period and phase, and the grid is handed back, so the lighting is steady and
carries through bars where nothing was hit. With no tempo (speech, ambient,
rubato) the onsets themselves are used.

One map per strobe source, so switching between them is instant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

__all__ = ["Beat", "BeatMap", "SOURCES", "build", "fits", "flux",
           "onsets", "tempo_of"]

#: What the strobe can listen to, as slices of the band range (fractions of the
#: band count): the meters' splits, so "Bass" means the same everywhere.
SOURCES: Dict[str, Tuple[float, float]] = {
    "Bass": (0.00, 0.16),
    "Mids": (0.16, 0.55),
    "Synths": (0.30, 0.72),
    "Treble": (0.55, 1.00),
}

#: The parts of a kit, and what each looks like in a spectrum: a place and a
#: shape, not just a band. (low, high, needs_wide, how_fast): needs_wide asks
#: that the rest of the spectrum move too, which tells a snare from a bass
#: note; how_fast is the shortest gap between two hits. Ranges are fractions of
#: the onset pass's twelve log-spaced bands: 0 is 46-93 Hz, 2 is 93-234, 5 is
#: 609-1078, 11 runs to the top of hearing.
ELEMENTS: Dict[str, Tuple[float, float, float]] = {
    "Kick": (0.00, 0.19, 0.13),
    "Snare": (0.25, 0.62, 0.14),
    "Hats": (0.70, 1.00, 0.05),
    "Bass": (0.00, 0.26, 0.11),
    "Synth": (0.25, 0.75, 0.09),
}

#: The parts of the kit whose onset strength is kept with their beats.
KEPT_FLUX = ("Kick", "Snare", "Hats")

#: The three places a hit can be: bottom, middle or top. Coarse on purpose; a
#: finer split is noisier, not truer.
LOW, MID, HIGH = (0.00, 0.25), (0.25, 0.64), (0.64, 1.00)

#: What each instrument's rise looks like, as bounds on its bottom, middle and
#: top shares, measured against kits written to known times (tests/drumkit.py)
#: and real recordings.
#:
#: Snare: top between 0.01 and 0.25. Hats sit at 0.29 and up and the brightest
#: snare (a clap) at 0.21; above 0.25 hats leak in (at 0.45 a bar of hats gave
#: forty-seven snares). The floor asks for some air, which a pitched synth note
#: in the same range lacks, but at 0.01, since a clap on a kick in four-
#: to-the-floor styles shows almost nothing up top; that took the mean F1 over
#: eleven styles from 49 to 79 with rock and jazz still perfect.
#:
#: Kick: the bottom must lead, with the top capped at 0.20. Asking for 0.70 of
#: the whole rise in the bottom only fits a kick alone; in a mix it threw away
#: three candidates in four. Above 0.20 precision falls (0.26 took it from 88
#: to 49 per cent). Not lowered further: a sweep's 0.06 took a real recording
#: from 27 kicks a minute to 9.
#:
#: Hats: a floor of 0.10 on the top. A kick alone shows 0.085 there, so any
#: lower floor calls every kick a hat; a hat landing on a kick is mostly kick
#: and cannot be recovered from shares.
PROFILE: Dict[str, dict] = {
    "Kick":  {"top": (0.00, 0.20), "leads": "bottom"},
    "Snare": {"bottom": (0.00, 0.85), "middle": (0.14, 1.01),
              "top": (0.01, 0.25)},
    "Hats":  {"top": (0.10, 1.01)},
    "Bass":  {"bottom": (0.55, 1.01), "top": (0.00, 0.14)},
    "Synth": {"bottom": (0.00, 0.62), "middle": (0.30, 1.01),
              "top": (0.02, 1.01)},
}


def fits(profile: Optional[dict], bottom: float, middle: float,
         top: float) -> bool:
    """Whether a rise divided like this belongs to that instrument. Named
    bounds rather than a five-tuple, so it is clear which bound decides.
    ``leads`` names a share that must be the largest of the three.
    """
    if not profile:
        return True
    shares = {"bottom": bottom, "middle": middle, "top": top}
    for key, value in shares.items():
        low, high = profile.get(key, (0.0, 1.01))
        if not low <= value <= high:
            return False
    leads = profile.get("leads")
    if leads and shares[leads] < max(v for k, v in shares.items()
                                     if k != leads):
        return False
    return True


#: Elements are found with a fussier setting than the strobe's: at sixty frames
#: a second every ripple is a peak. Raising it finds no more real kicks, only
#: false ones (eight written tracks against a real one):
#:
#:      0.22   kick 95.5% recall at 88.0 precision   74.8 kicks/min
#:      0.35        95.5             68.9            85.0
#:      0.45        95.5             58.1            86.7
#:      0.60        93.2             48.8            91.2
ELEMENT_SENSE = 0.22

#: Pulses looked for, in beats a minute. The top is above any counted tempo on
#: purpose: the fastest steady pulse is what is found (hats on the eighths of
#: 120 BPM pulse at 240). The preference for ordinary tempos picks which
#: multiple to report.
MIN_BPM = 66.0
MAX_BPM = 240.0

#: How far either side of a frame the local median is taken, in seconds: long
#: enough that a loud bar does not raise its own bar, short enough to follow a
#: track that grows.
WINDOW = 0.9

#: How far a peak must beat the local median, in local spreads, at the middle
#: sensitivity.
BASE_K = 1.4

#: Nothing fires twice inside this: two flashes 50 ms apart are one with a
#: stutter.
FLOOR_GAP = 0.09

#: And nothing counts below this share of the track's loudest. In silence the
#: median and spread are zero and any wobble clears the bar: on a written
#: pattern, most "snares" found were rises of a hundredth of a real one in the
#: gaps.
QUIET_FLOOR = 0.02

#: How tightly the onsets must bunch before the grid is trusted over the
#: onsets. The best of 250 candidate periods scores well by chance (twenty
#: scattered onsets reach 0.36 to 0.61); on the weaker half of a track (see
#: tempo_of) noise reaches 0.53 at most and mixes start at 0.67. Failing it
#: falls back to the onsets.
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
    #: The onset strength a reading every ``rate``th of a second, per part of
    #: the kit, peak or not. On a real mix most detected peaks are not the
    #: drum; this, folded on the bar, shows where it is (see trackstyle). Empty
    #: where not kept.
    flux: Tuple[float, ...] = ()
    rate: float = 0.0

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


def spread(frames: Sequence, at: int, low: float, high: float) -> float:
    """How much of the spectrum outside a band moved at this frame: a snare
    takes the whole top with it, and a bass note does not.
    """
    if at <= 0 or at >= len(frames):
        return 0.0
    bands = len(frames[0])
    start = max(0, min(bands - 1, int(bands * low)))
    stop = max(start + 1, min(bands, int(math.ceil(bands * high))))
    here, before = frames[at], frames[at - 1]
    rose = 0
    counted = 0
    for index in range(bands):
        if start <= index < stop:
            continue
        counted += 1
        if here[index] - before[index] > 0.01:
            rose += 1
    return rose / max(1, counted)


def flux(frames: Sequence, low: float, high: float) -> List[float]:
    """How much the spectrum rose per frame over a slice of the bands, rises
    only: counting falls turns every gap into an onset.
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
    """The median around a point and its spread. The median, since a mean is
    pulled up by the very peaks being judged.
    """
    low = max(0, index - reach)
    high = min(len(values), index + reach + 1)
    window = sorted(values[low:high])
    if not window:
        return 0.0, 0.0
    middle = window[len(window) // 2]
    # The spread as the distance from the median to the upper quartile: one
    # crash in a quiet passage doubles a standard deviation and darkens the
    # second that needed light.
    upper = window[int(len(window) * 0.75)]
    return middle, max(1e-6, upper - middle)


def onsets(envelope: Sequence[float], rate: float,
           sensitivity: float = 0.5,
           gap: float = FLOOR_GAP) -> List[Beat]:
    """Peaks in the flux that stand out from their neighbourhood.
    ``sensitivity`` runs 0 (only the unmistakable) to 1 (nearly anything)
    and scales the multiplier on the local spread.
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
        if here < loudest * QUIET_FLOOR:
            continue        # silence, not a hit
        middle, spread = _local(envelope, index, reach)
        if here < middle + k * spread:
            continue
        # Where the peak really is between its neighbours: a parabola through
        # three points places it to about ten milliseconds instead of
        # sixty-six.
        before, after = envelope[index - 1], envelope[index + 1]
        divisor = before - 2.0 * here + after
        shift = 0.0 if divisor == 0 else 0.5 * (before - after) / divisor
        shift = max(-0.5, min(0.5, shift))
        at = (index + shift) / rate
        if at - last_at < max(0.02, gap):
            continue
        found.append(Beat(at=at, strength=min(1.0, here / loudest)))
        last_at = at
    return found


def tempo_of(hits: Sequence[Beat]) -> Tuple[float, float, float]:
    """The tempo the onsets agree on, how strongly, and its phase. In seconds
    rather than frames: at fifteen frames a second, 120 BPM is 7.5 frames,
    which no lag can express, and autocorrelating gave 82 BPM for a 120 BPM
    click. Each candidate period is scored by folding the onsets into it as
    unit vectors: the length of their sum over the count is the fit, its
    direction the phase.
    """
    if len(hits) < 6:
        return 0.0, 0.0, 0.0
    best = (0.0, 0.0, 0.0)
    # Half a BPM apart, finer than a strobe can be heard to be wrong over a
    # track.
    steps = int((MAX_BPM - MIN_BPM) * 2) + 1
    for step in range(steps):
        bpm = MIN_BPM + step * 0.5
        period = 60.0 / bpm
        score, phase = _fit(hits, period)
        # Whatever fits a period also fits its half and quarter, so a gentle
        # preference for ordinary tempos breaks the tie, as a person would.
        score *= math.exp(-((math.log(bpm / 120.0)) ** 2) / 0.72)
        if score > best[0]:
            best = (score, bpm, phase)
    if not best[1]:
        return 0.0, 0.0, 0.0

    # The score returned is the fit to whichever half of the track agrees less:
    # the best of 250 tries fits noise at 0.61 against 0.70 for a real mix, but
    # a real tempo holds in both halves, and split that way noise reaches 0.53
    # and music starts at 0.67.
    period = 60.0 / best[1]
    middle = len(hits) // 2
    early, _ = _fit(hits[:middle], period)
    late, _ = _fit(hits[middle:], period)
    return best[1], min(early, late), best[2]


def _fit(hits: Sequence[Beat], period: float) -> Tuple[float, float]:
    """How tightly these onsets bunch when folded into a period, and where:
    each onset becomes an angle, the angles are summed as unit vectors, and
    the sum's length over the count is the fit and its direction the phase.
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
    """How well a tempo must fit to be believed, for this many onsets: n
    scattered onsets score about sqrt(pi / 4n) by chance (a third for a
    dozen), so the bar starts well clear of chance and comes down with more
    evidence.
    """
    if count < 8:
        return 2.0        # unreachable: too few to be sure of anything
    return max(LOCK_SCORE, 1.9 * math.sqrt(math.pi / (4.0 * count)))


def _grid(beats: Sequence[Beat], bpm: float, span: float,
           phase: float) -> List[Beat]:
    """A steady grid at this tempo and phase across the whole track, including
    bars where nothing was hit.
    """
    period = 60.0 / bpm
    if period <= 0 or span <= 0:
        return []
    phase = phase % period

    # How hard each grid line was hit, from the nearest onset; a line with
    # nothing near still fires softly, through a held chord.
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
        # Floored so every grid line fires at the middle sensitivity, keeping
        # the grid even; turning sensitivity down keeps the lines hit hardest.
        out.append(Beat(at=at, strength=max(0.55, nearest)))
        at += period
    return out


#: How many frames either side of an onset make its attack. A kick's harmonics
#: arrive a frame or two after its fundamental, so one frame alone read every
#: kick as a rimshot; an instrument's signature is its whole attack, about
#: fifty milliseconds.
ATTACK = 2


def shares(frames: Sequence, at: int, span: int = ATTACK) -> Tuple[float, float, float]:
    """How the rise across a hit divided between bottom, middle and top; the
    three add to one when anything rose.
    """
    if at <= 0 or at >= len(frames):
        return 0.0, 0.0, 0.0
    bands = len(frames[0])
    first = max(1, at - span)
    last = min(len(frames) - 1, at + span)
    totals = [0.0, 0.0, 0.0]
    for step in range(max(1, first), last + 1):
        here, before = frames[step], frames[step - 1]
        for index in range(bands):
            rise = here[index] - before[index]
            if rise <= 0.0:
                continue
            share = index / max(1, bands - 1)
            which = 0 if share < LOW[1] else (1 if share < MID[1] else 2)
            totals[which] += rise
    everything = sum(totals)
    if everything <= 0.0:
        return 0.0, 0.0, 0.0
    return (totals[0] / everything, totals[1] / everything,
            totals[2] / everything)


def elements(frames: Sequence, rate: float,
             sensitivity: float = ELEMENT_SENSE) -> Dict[str, BeatMap]:
    """One map per part of the kit, from the fine onset pass. Finding where
    something started is easy; deciding what started is not, since a kick
    has harmonics in the snare's range and a snare body in the kick's. So
    each candidate is weighed by how its rise divides (bottom for a kick,
    top for a hat, middle plus the top's sizzle for a snare). No tempo is
    fitted: these follow the drummer, not a grid.
    """
    found: Dict[str, BeatMap] = {}
    if not frames or rate <= 0:
        return {name: BeatMap() for name in ELEMENTS}
    profiles: Dict[int, Tuple[float, float, float]] = {}
    for name, (low, high, gap) in ELEMENTS.items():
        envelope = flux(frames, low, high)
        kept: List[Beat] = []
        wants = PROFILE.get(name)
        for beat in onsets(envelope, rate, sensitivity, gap=gap):
            index = int(round(beat.at * rate))
            if index not in profiles:
                profiles[index] = shares(frames, index)
            bottom, middle, top = profiles[index]
            if not fits(wants, bottom, middle, top):
                continue
            kept.append(beat)
        # Rounded: three places is a thousandth of the range, and halves what
        # crosses between processes.
        found[name] = BeatMap(beats=tuple(kept),
                              flux=tuple(round(value, 3) for value in envelope)
                              if name in KEPT_FLUX else (),
                              rate=float(rate))
    return found


def build(frames: Sequence, rate: float,
          sensitivity: float = 0.5) -> Dict[str, BeatMap]:
    """A map per source from frames already produced: milliseconds of work in
    the same worker.
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
