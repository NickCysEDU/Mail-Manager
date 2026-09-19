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

__all__ = ["Beat", "BeatMap", "SOURCES", "build", "fits", "flux",
           "onsets", "tempo_of"]

#: What the strobe can be told to listen to, and the slice of the band
#: range each one means, as fractions of the band count. The same splits
#: the meters use, so "Bass" means the same thing everywhere.
SOURCES: Dict[str, Tuple[float, float]] = {
    "Bass": (0.00, 0.16),
    "Mids": (0.16, 0.55),
    "Synths": (0.30, 0.72),
    "Treble": (0.55, 1.00),
}

#: The parts of a kit, and what each one looks like in a spectrum.
#:
#: These are not the band ranges above under different names. A band is a
#: place; an instrument is a place *and* a shape. A kick is a short burst
#: at the very bottom and almost nothing above it. A snare is a crack
#: around two hundred hertz with a wash of noise over the whole top half,
#: which is what tells it from a tom. A hat is the top of the range and
#: nothing else, and it is over before a kick has finished starting.
#:
#: (low, high, needs_wide, how_fast) - needs_wide asks that the rest of
#: the spectrum move too, which is what separates a snare from a bass
#: note at the same moment; how_fast is the shortest gap between two of
#: them, since a hat can repeat four times inside one kick.
#: Ranges are fractions of the band index, and the bands are the twelve
#: log-spaced ones the onset pass reports: 0 is 46-93 Hz, 2 is 93-234,
#: 5 is 609-1078, and 11 runs to the top of hearing.
ELEMENTS: Dict[str, Tuple[float, float, float]] = {
    "Kick": (0.00, 0.19, 0.13),
    "Snare": (0.25, 0.62, 0.14),
    "Hats": (0.70, 1.00, 0.05),
    "Bass": (0.00, 0.26, 0.11),
    "Synth": (0.25, 0.75, 0.09),
}

#: The three places a hit can be, used to decide which instrument it was.
#: Coarse on purpose: the question is only "bottom, middle or top", and a
#: finer split makes the shares noisier without making them truer.
LOW, MID, HIGH = (0.00, 0.25), (0.25, 0.64), (0.64, 1.00)

#: What each instrument's rise looks like, as shares of bottom, middle
#: and top: the bounds each share has to sit inside.
#:
#: The snare's was measured off one kit playing one pattern, and it wrote
#: that down as a narrow window: bottom 0.04-0.74, middle at least 0.26,
#: **top 0.04 to 0.20**. That last bound is a description of one snare
#: rather than of snares.
#:
#: Held against four written to known times - see tests/drumkit.py - it
#: found two of them and called a third a cymbal:
#:
#:      acoustic snare   bottom 0.47  middle 0.37  top 0.16   found
#:      electronic              0.42          0.53      0.05  found
#:      clap                    0.46          0.33      0.21  called a hat
#:      rimshot                 0.18          0.72      0.10  found
#:
#: A clap is all crack and no body, and 0.21 of its rise is in the top -
#: over a ceiling of 0.20 by one hundredth. So it went to the hats, and
#: so did every bright snare, and on a real track the same ceiling was
#: one of the two things holding the snare map down to thirteen hits a
#: minute against ninety-one kicks. That is what "the snare isn't too
#: accurate" was.
#:
#: The ceiling is 0.25 now, and there is a real gap to put it in. Over
#: the four written snares and a bar of hats played on their own:
#:
#:      the brightest snare, a clap      top 0.21
#:      the dimmest hat                  top 0.29
#:      a hat, typically                 top 0.34
#:
#: So 0.25, in the middle of the gap. It cannot go much higher: the
#: ceiling is the only thing keeping hats out of the snare map, and at
#: 0.45 a bar of hats alone produced forty-seven snares.
#:
#: The floor of 0.04 stays, and it is worth saying why, because taking it
#: away looks tempting and is wrong. It asks a snare to have *some* air
#: over it, which is what a pitched note in the same part of the spectrum
#: does not. Removing it doubled the real track's snares, from 71 a
#: minute to 151, and every one of the extras was a synth.
#:
#: The kick had the same shape of mistake in it, and it cost more.
#: It asked for 0.70 of a frame's *whole* rise to be in the bottom third,
#: which is a kick played on its own. In a mix something else is nearly
#: always happening on the beat - a hat, a stab, the snare - so the kick
#: owns well under two thirds of the moment while being unmistakably
#: there. On a real track that threw away 227 of 309 candidates and left
#: 44 kicks a minute where the pulse says there should be about 140,
#: which is "kick detection is off, only gets one every once in a while".
#:
#: What is asked now is that the bottom *leads* - that it is the largest
#: of the three - with little up top. The cap is 0.20, which is where it
#: stops being free: at 0.26 a written track's kick precision falls from
#: 88 per cent to 49, and at 0.20 it does not move at all while the real
#: track goes from 65 kicks a minute to 75. A fifth of the rise up top is
#: a hat landing on the kick; a quarter of it is a cymbal. That is what a kick is, and it
#: needs no floor of its own: with the top capped at 0.09, leading
#: already means the bottom has at least 0.455 of the rise, which is why
#: every floor from 0.45 down made no difference to anything. It takes
#: the same track from 44 a minute to 74 and changes nothing on eight
#: written ones.
#:
#: Turning the sensitivity up does not do this. From 0.22 to 0.80 it
#: found 309 candidates rather than 428 and kept 44 a minute rather than
#: 49: the threshold was never what was in the way.
#:
#: Hats are the exception and are left almost unconstrained. When a hat
#: lands on a kick the frame's rise is thirty times more kick than hat,
#: so no share of it can recover the hat - and asking for one lost half
#: of them, which reads as lighting that stops during the loud parts.
#: The snare's and the hats' bounds were then measured again, against
#: eleven styles written to known times rather than one - see STYLES in
#: tests/drumkit.py. Held against those, the bounds above scored a mean F1
#: of 49 for the snare, and the way they failed was not subtle:
#:
#:      house 0    trance 0    techno 8    trap 8    hiphop 12
#:      rock 100   jazz 100    breaks 92   dnb 80
#:
#: Perfect on the two patterns they were measured from and nothing at all
#: on four-to-floor. The reason is that a clap in house, trance or techno
#: lands on beats two and four, where there is *also* a kick - so the
#: frame's rise is mostly kick, and a rule reading shares of that rise
#: sees a kick. Moving the kick off those beats took house from 0 to 19,
#: and taking the sub out as well took it to 100, which is what proves it.
#:
#: What separates a kick from a kick with a clap on it is not the middle,
#: where they are 0.11 against 0.13 and no threshold can help. It is that
#: a kick alone has *nothing* up top - 0.00 against the clap's 0.03 - and
#: the old floor of 0.04 sat just above the clap. Lowering it to 0.01,
#: letting the bottom go to 0.85 since a kick may legitimately own the
#: frame, and asking the middle for 0.14 rather than 0.26, takes the mean
#: from 49 to 79 with rock and jazz still perfect.
#:
#: The hats' floor of 0.10 is left where it is, and the reason is worth
#: writing down because the numbers argue for moving it. Over the eleven
#: styles it costs 46 per cent of the hats - 54 per cent recall against 86
#: at 0.04 - and on two real recordings lowering it changes nothing at all
#: (402 hats a minute against 409). It still cannot move: a kick alone
#: comes in at 0.085 up top, so any floor under 0.09 calls every kick a
#: hat, and ``test_kicks_alone_are_never_called_hats`` duly failed on 24
#: hats in a track with no hats in it. Asking instead for the top to
#: *lead* separates them perfectly and finds 20 per cent of the hats,
#: because a hat landing on a kick is mostly kick.
#:
#: The eleven styles cannot see this, because every one of them has hats
#: all the way through. That is what the older tests are for.
#:
#: The kick's cap is deliberately left alone. See the note on CLICK in
#: tests/drumkit.py: the written kicks are too clean up top for that bound
#: to be measured here, and the sweep's answer of 0.06 took a real
#: recording from 27 kicks a minute to 9.
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
    """Whether a rise divided like this belongs to that instrument.

    A table of named bounds rather than a five-tuple, because the tuple
    said ``(0.04, 0.74, 0.26, 0.04, 0.20)`` and nobody reading that could
    tell which number was the one doing the damage.

    ``leads`` names a share that has to be the largest of the three. It
    says what a bound cannot: that this is a hit the bottom carries,
    whatever else is going on at the same moment.
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


#: Elements are found with a fussier setting than the strobe's own. At
#: sixty frames a second every ripple is a local peak, and lighting that
#: fires on all of them is not reacting to the drums, it is reacting to
#: the noise floor.
#:
#: Turning it up is the obvious way to find more kicks and it is the wrong
#: one. Measured over eight written tracks, against a real one:
#:
#:      0.22   kick 95.5% recall at 88.0 precision   74.8 kicks/min
#:      0.35        95.5             68.9            85.0
#:      0.45        95.5             58.1            86.7
#:      0.60        93.2             48.8            91.2
#:
#: The real track's rate goes up because the false positives do. What
#: actually found more kicks was the profile - see PROFILE - which found
#: them without inventing any.
ELEMENT_SENSE = 0.22

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

#: And nothing counts at all below this share of the loudest thing in the
#: track.
#:
#: The threshold everything else uses is the local median plus a multiple
#: of the local spread, which is what makes one sensitivity setting mean
#: the same thing in a sparse passage and a dense one. In silence it means
#: nothing: the median is zero, the spread is zero, and any wobble in the
#: last decimal place clears the bar. Measured on a written drum pattern,
#: forty-three of the sixty-six snares found were in the gaps between the
#: hits, on rises of 0.003 where a snare is 0.3 - a hundredth of one.
#:
#: Two per cent, so a quiet hit still counts and a hundredth of one does
#: not. It was never noticed before because the old snare profile happened
#: to reject them for a different reason, having asked for a sizzle they
#: did not have either.
QUIET_FLOOR = 0.02

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


def spread(frames: Sequence, at: int, low: float, high: float) -> float:
    """How much of the spectrum outside a band moved at this frame.

    A snare and a bass note can land in the same place at the same
    moment; what separates them is that the snare takes the whole top of
    the spectrum with it.
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
           sensitivity: float = 0.5,
           gap: float = FLOOR_GAP) -> List[Beat]:
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
        if here < loudest * QUIET_FLOOR:
            continue        # silence, not a hit
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
        if at - last_at < max(0.02, gap):
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


#: How many frames either side of an onset make up its attack.
#:
#: One frame was not enough, and the reason is worth keeping. A kick's
#: fundamental arrives first and its harmonics a frame or two behind it,
#: so in the later frame the bottom has already peaked - it is not rising
#: any more - and the only thing still going up is the middle. Read one
#: frame at a time, that moment divides as 0.00 bottom, 0.98 middle, 0.02
#: top, which is a perfect description of a rimshot. Every kick in a
#: written pattern produced one, and the snare map came back with twice
#: as many hits as there were snares.
#:
#: An instrument's signature is its attack, which is fifty milliseconds,
#: not the sixteen of a single frame. Reading the rise across the whole
#: attack puts the kick's bottom back where it belongs.
ATTACK = 2


def shares(frames: Sequence, at: int, span: int = ATTACK) -> Tuple[float, float, float]:
    """How the rise across a hit divided between bottom, middle and top.

    The three add to one when anything rose at all, so each is the share
    of this moment that belongs to that part of the spectrum - which is
    what says whether a hit was a kick, a snare or a hat.
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
    """One map per part of the kit, off a fine-grained onset pass.

    Two steps, and the second is the one that matters. Finding where
    something started in a band is easy; deciding *what* started is not,
    because a kick has harmonics in the snare's range and a snare has body
    in the kick's. Three detectors watching three bands find the same hit
    three times, and lighting driven off that has nothing to tell apart -
    everything flashes on everything.

    So each candidate is weighed: how did the rise at that moment divide
    between the bottom, the middle and the top of the spectrum? A kick
    puts most of it at the bottom. A hat puts nearly all of it at the top.
    A snare sits in the middle and, unlike a tom at the same pitch, takes
    the top with it - which is the sizzle, and the test for it.

    No tempo is fitted to any of these. A grid is the right answer for
    lighting a room to the beat and the wrong one for following a
    drummer: the whole point of knowing where the snare is, separately
    from the kick, is to put something different on each.
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
        found[name] = BeatMap(beats=tuple(kept))
    return found


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
