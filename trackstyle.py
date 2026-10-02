"""What kind of music a track is, and how it is put together.

Music rider built every track from the same rules: a figure every two beats
or so, the same eight shapes in turn, a quarter of them obstacles, a road
whose hills are the loudness and whose bends are the stereo. A house record
and a drum and bass record came out as the same ride at two speeds. What
makes a ride *of that record* is what makes the record that record, and
most of it is already measured by the time anything is drawn: where every
kick, snare and hat is, the tempo, the loudness from end to end, and - from
the harmony pass - the chords and the melody.

So this reads two things off them.

**The style**, as a handful of numbers rather than a genre's name. A name
is a guess and a brittle one - a record is house *and* garage, techno *and*
trance - and what the ride needs is not what the record is called but how
it moves: whether the kick lands on every beat (``steady``), whether it
dodges the beat (``broken``), whether the snare waits for the third beat
(``heavy``, half time), whether the hats swing (``swung``), whether they
roll (``rolls``), how fast and hard it all is (``hard``), whether there is
a melody worth following (``melodic``), and how much it rises and falls
(``drama``). Each is 0 to 1, measured, and they mix: a record that is a
little of everything gets a little of everything.

**The sections**, bar by bar: where the drums come in and drop out and the
loudness steps up or falls away. Each one is an intro, a build, a drop, a
groove, a break or an outro, and each has its own intensity. Audiosurf 2
times its loops and power-ups to "big moments in your music"; a drop is the
biggest there is, and this is what finds it.

And a **seed**, from the track itself, so the same record makes the same
ride every time it is played - which is what makes a best mean anything -
while every record gets its own.

Pure Python and cheap: it walks the chart once and the loudness once.
"""

from __future__ import annotations

import hashlib
import math
import statistics
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

#: Beats in a bar. Everything with a bar in it is in four.
BAR = 4

#: How near a beat a hit has to be to be on it, as a share of a beat.
ON_BEAT = 0.15

#: The fewest bars a section may be. A change of energy shorter than this
#: is a fill, not a new part of the track.
LEAST_BARS = 4


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass
class Section:
    """One part of a track, in seconds, with what kind of part it is."""

    start: float
    end: float
    kind: str
    #: How much is going on, 0 to 1 against the rest of the track.
    level: float
    #: Whether the kick is in it.
    drums: bool
    #: How much louder its second half is than its first, and whether the
    #: snares and hats thicken through it: the snare roll before a drop.
    rising: float = 0.0
    busier: bool = False

    @property
    def length(self) -> float:
        return self.end - self.start


@dataclass
class Style:
    """How a track moves, and how it is put together. See the module."""

    tempo: float = 0.0
    steady: float = 0.0
    broken: float = 0.0
    heavy: float = 0.0
    swung: float = 0.0
    rolls: float = 0.0
    hard: float = 0.0
    melodic: float = 0.0
    calm: float = 0.0
    drama: float = 0.0
    #: How much faster than the tempo it was counted in the record really
    #: is: 2 for drum and bass heard at half its tempo, otherwise 1.
    faster: float = 1.0
    #: The time of a beat, in seconds, from the drums themselves.
    beat_phase: float = 0.0
    #: Whether the tempo and the beat are the drums' own (see rhythm_of),
    #: rather than what the style was read at.
    from_drums: bool = False
    #: The beat times where the tempo moves (see follow), else empty.
    beats: List[float] = field(default_factory=list)
    #: The time of one first beat of a bar.
    downbeat: Optional[float] = None
    #: The measurements the numbers above came from, for anybody checking.
    measured: Dict[str, float] = field(default_factory=dict)
    sections: List[Section] = field(default_factory=list)
    seed: int = 0

    def clock(self):
        """Where this track's beats fall: see beat_clock."""
        from beat_clock import BeatClock

        return BeatClock(60.0 / self.tempo if self.tempo > 0.0 else 0.0,
                         self.beat_phase, self.beats, self.downbeat)

    def section_at(self, when: float) -> Optional[Section]:
        starts = [section.start for section in self.sections]
        index = bisect_left(starts, when + 1e-9) - 1
        if 0 <= index < len(self.sections):
            here = self.sections[index]
            if here.start <= when < here.end:
                return here
        return None

    def family(self) -> str:
        """The strongest of the style's numbers, by name: for a label on
        the screen and for anybody reading a log, never for a decision."""
        weights = {"steady": self.steady, "broken": self.broken,
                   "heavy": self.heavy, "swung": self.swung,
                   "hard": self.hard, "melodic": self.melodic,
                   "calm": self.calm}
        return max(weights, key=lambda name: weights[name])

    def drops(self) -> List[Section]:
        return [section for section in self.sections if section.kind == "drop"]


def _near(times: Sequence[float], when: float, reach: float) -> bool:
    index = bisect_left(times, when - reach)
    return index < len(times) and times[index] <= when + reach


def seed_of(contour: Optional[dict], chart: Optional[dict]) -> int:
    """A number that is this track's and no other's, from what was heard.

    The loudness to two places and the first few hundred drum hits to the
    hundredth of a second: the same record gives the same number on every
    machine, and a different one gives a different number."""
    digest = hashlib.sha256()
    for value in (contour or {}).get("loud") or ():
        digest.update(b"%d," % int(round(float(value) * 100)))
    for name in sorted(chart or {}):
        digest.update(name.encode())
        for when in list((chart or {})[name])[:400]:
            digest.update(b"%d," % int(round(float(when) * 100)))
    return int.from_bytes(digest.digest()[:8], "big")


def _bars(first: float, beat: float, length: float) -> List[float]:
    """The start of every bar, from the first beat."""
    span = beat * BAR
    count = int(max(0.0, length - first) / span) + 1
    return [first + index * span for index in range(count)]


def _downbeat(kicks: Sequence[float], snares: Sequence[float], grid: float,
              beat: float) -> float:
    """Which beat of four is the first of a bar.

    The one the kick lands on most and the snare least: on four to the
    floor every beat has a kick and the snare says which are two and four;
    on half time the kick is on one and the snare on three; on a break the
    kick still starts the bar.
    """
    scores = [0.0] * BAR
    for when in kicks:
        place = (when - grid) / beat
        nearest = round(place)
        if abs(place - nearest) < ON_BEAT:
            scores[int(nearest) % BAR] += 1.0
    for when in snares:
        place = (when - grid) / beat
        nearest = round(place)
        if abs(place - nearest) < ON_BEAT:
            scores[int(nearest) % BAR] -= 0.8
    best = max(range(BAR), key=lambda index: scores[index])
    first = grid + best * beat
    while first - beat * BAR >= -beat * 0.5:
        first -= beat * BAR
    return first


def _changes_on(loud: Sequence[float], rate: float, first: float,
               span: float, length: float) -> float:
    """How much the loudness changes across the start of bars that begin
    at ``first`` and are ``span`` long: the loudness a beat after each
    start against a beat before it."""
    if not loud or rate <= 0.0 or span <= 0.0:
        return 0.0
    total = 0.0
    at = first % span
    beat = span / BAR
    while at < length:
        before = _level_at(loud, rate, max(0.0, at - beat), at)
        after = _level_at(loud, rate, at, min(length, at + beat))
        total += abs(after - before)
        at += span
    return total


def _level_at(loud: Sequence[float], rate: float, start: float,
              end: float) -> float:
    if not loud or rate <= 0.0:
        return 0.0
    low = max(0, int(start * rate))
    high = min(len(loud), max(low + 1, int(end * rate)))
    if low >= len(loud):
        return 0.0
    return sum(loud[low:high]) / (high - low)


def _count(times: Sequence[float], start: float, end: float) -> int:
    return bisect_left(times, end) - bisect_left(times, start)


def _bar_strength(curve: Optional[Tuple[Sequence[float], float]],
                  start: float, end: float) -> float:
    if not curve or not curve[0] or curve[1] <= 0.0:
        return 0.0
    values, rate = curve
    first = max(0, int(start * rate))
    last = min(len(values), max(first + 1, int(end * rate)))
    if first >= len(values):
        return 0.0
    return sum(values[first:last]) / (last - first)


def sections(chart: dict, beat: float, grid: float, contour: dict,
             length: float, kick: Optional[Tuple[Sequence[float], float]] = None,
             first: Optional[float] = None) -> List[Section]:
    """The track, cut where it changes and named for what each part is.

    Where the drums are in comes from ``kick`` - the kick's onset strength,
    (readings, rate) - averaged over each bar, when there is one: counting
    detected kicks counts the detector's mistakes too, and on a real mix
    those are most of them. ``first`` is the first downbeat, if known.
    """
    loud = list((contour or {}).get("loud") or ())
    rate = float((contour or {}).get("rate") or 0.0)
    kicks = sorted(chart.get("Kick", ()))
    snares = sorted(chart.get("Snare", ()))
    hats = sorted(chart.get("Hats", ()))
    if beat <= 0.0 or length <= 0.0 or not loud:
        return [Section(0.0, max(length, 0.0), "groove", 0.5, bool(kicks))]
    if first is None:
        first = _downbeat(kicks, snares, grid, beat)
    starts = [start for start in _bars(first, beat, length) if start < length]
    if first > 0.0:
        # What comes before the first downbeat belongs to the first bar,
        # not to a bar of its own: a sliver of silence at the top of the
        # file made a bar with nothing in it, and a part rising out of it.
        if starts and first < beat * BAR * 0.5:
            starts[0] = 0.0
        else:
            starts.insert(0, 0.0)
    ends = starts[1:] + [length]
    energy = [_level_at(loud, rate, s, e) for s, e in zip(starts, ends)]
    if kick and kick[0]:
        kicked = [_bar_strength(kick, s, e) for s, e in zip(starts, ends)]
    else:
        kicked = [float(_count(kicks, s, e)) for s, e in zip(starts, ends)]
    busy = [_count(snares, s, e) + _count(hats, s, e)
            for s, e in zip(starts, ends)]
    ordered = sorted(energy)
    low = ordered[int(len(ordered) * 0.1)]
    high = ordered[min(len(ordered) - 1, int(len(ordered) * 0.9))]
    reach = max(1e-6, high - low)
    level = [_clamp((value - low) / reach) for value in energy]
    # Against the typical bar that has any kick in it at all, so a record
    # that is mostly drums and one that is mostly not both split cleanly.
    some = sorted(value for value in kicked if value > 0.0)
    typical_kicks = some[len(some) * 2 // 3] if some else 0.0
    least = typical_kicks * 0.5 if kick and kick[0] else max(1.0, typical_kicks * 0.5)
    drums = [value >= least and value > 0.0 for value in kicked]
    # Where it changes: the two bars before against the two after, in
    # loudness and in whether the kick is there.
    novelty = [0.0] * len(starts)
    for index in range(2, len(starts) - 1):
        before = range(max(0, index - 2), index)
        after = range(index, min(len(starts), index + 2))
        step = abs(sum(level[i] for i in after) / len(after)
                   - sum(level[i] for i in before) / len(before))
        kick_step = abs(sum(drums[i] for i in after) / len(after)
                        - sum(drums[i] for i in before) / len(before))
        novelty[index] = step + 0.6 * kick_step
    cuts = [0]
    candidates = sorted(range(len(novelty)), key=lambda i: -novelty[i])
    for index in candidates:
        if novelty[index] < 0.18:
            break
        if all(abs(index - cut) >= LEAST_BARS for cut in cuts) and (
                len(starts) - index >= LEAST_BARS):
            cuts.append(index)
    cuts.sort()
    out: List[Section] = []
    for number, cut in enumerate(cuts):
        stop = cuts[number + 1] if number + 1 < len(cuts) else len(starts)
        bars = range(cut, stop)
        mean = sum(level[i] for i in bars) / max(1, len(bars))
        with_drums = sum(drums[i] for i in bars) / max(1, len(bars)) >= 0.5
        # Rising through it, and busier at the end than the start: the
        # snare roll and the filter sweep before a drop.
        half = max(1, len(bars) // 2)
        early = [level[i] for i in list(bars)[:half]]
        late = [level[i] for i in list(bars)[half:]] or early
        rising = (sum(late) / len(late)) - (sum(early) / len(early))
        busy_early = sum(busy[i] for i in list(bars)[:half]) / half
        busy_late = (sum(busy[i] for i in list(bars)[half:])
                     / max(1, len(bars) - half))
        out.append(Section(starts[cut], ends[stop - 1], "groove", mean,
                           with_drums, rising,
                           busy_late > busy_early * 1.3))
    for index, section in enumerate(out):
        before = out[index - 1] if index > 0 else None
        after = out[index + 1] if index + 1 < len(out) else None
        if (section.drums and section.level >= 0.55 and before is not None
                and (before.level <= section.level - 0.2 or not before.drums)):
            section.kind = "drop"
        elif (after is not None and after.level >= section.level + 0.2
              and (section.rising > 0.05 or section.busier)):
            section.kind = "build"
        elif not section.drums or section.level < 0.35:
            section.kind = "break"
        if index == 0 and section.kind in ("break", "groove") and (
                section.level < 0.5 or not section.drums):
            section.kind = "intro"
        if (index == len(out) - 1 and index > 0
                and section.kind in ("break", "groove")
                and section.level < 0.5):
            section.kind = "outro"
    # Two parts of the same kind side by side are one part: a cut inside a
    # drop - a fill, a detector's noise - made two drops of one, and a
    # corkscrew and a power block for the second half of it.
    merged: List[Section] = []
    for section in out:
        if merged and merged[-1].kind == section.kind:
            last = merged[-1]
            total = last.length + section.length
            last.level = ((last.level * last.length
                           + section.level * section.length) / max(1e-9, total))
            last.drums = last.drums or section.drums
            last.end = section.end
        else:
            merged.append(section)
    return merged


#: Positions a beat is read at: twenty-four, so that sixteenths (every
#: six), triplets (every eight) and thirty-seconds (every three) each land
#: on one.
PER_BEAT = 24

#: Where the kick's four beats become four to the floor: from as strong as
#: this share of the strongest, over this much more (see rhythm, read).
FOUR_FROM, FOUR_OVER = 0.45, 0.4


def envelope_from(times: Sequence[float], rate: float, length: float
                  ) -> List[float]:
    """An onset strength made from hit times, for a chart that came with
    none: a spike at every hit, dying over a sixtieth of a second or so."""
    count = int(max(0.0, length) * rate) + 2
    out = [0.0] * count
    for when in times:
        index = int(round(when * rate))
        if 0 <= index < count:
            out[index] += 1.0
            if index + 1 < count:
                out[index + 1] += 0.4
    return out


def fold(values: Sequence[float], rate: float, beat: float, phase: float,
         spans: Sequence[Tuple[float, float]], beats: int = BAR) -> List[float]:
    """The average onset strength at each of ``beats * PER_BEAT`` places in
    a bar, over the stretches in ``spans``: where in the bar a drum really
    is, with every false hit a detector would make smeared into an even
    floor under it."""
    bins = beats * PER_BEAT
    total = [0.0] * bins
    count = [0] * bins
    for start, end in spans:
        first = max(0, int(start * rate))
        last = min(len(values), int(end * rate))
        for index in range(first, last):
            place = ((index / rate - phase) / beat) % beats
            slot = int(place * PER_BEAT + 0.5) % bins
            total[slot] += values[index]
            count[slot] += 1
    return [total[i] / count[i] if count[i] else 0.0 for i in range(bins)]


def _peak(pattern: Sequence[float], place: int) -> float:
    size = len(pattern)
    return max(pattern[(place + offset) % size] for offset in (-1, 0, 1))


def _floor(pattern: Sequence[float]) -> float:
    ordered = sorted(pattern)
    return ordered[len(ordered) // 2] if ordered else 0.0


def _roll_share(values: Sequence[float], rate: float,
                spans: Sequence[Tuple[float, float]], beat: float,
                phase: float) -> float:
    """The share of beats with six or more separate hits in them - peaks
    of at least three tenths of that beat's strongest."""
    rolled = counted = 0
    for start, end in spans:
        at = phase + math.ceil((start - phase) / beat) * beat
        while at + beat <= end:
            first = int(at * rate)
            last = min(len(values), int((at + beat) * rate))
            window = values[first:last]
            at += beat
            if len(window) < 3:
                continue
            top = max(window)
            if top <= 0.0:
                continue
            counted += 1
            peaks = sum(1 for i in range(1, len(window) - 1)
                        if window[i] >= top * 0.3
                        and window[i] > window[i - 1]
                        and window[i] >= window[i + 1])
            if peaks >= 6:
                rolled += 1
    return rolled / counted if counted else 0.0


def rhythm(kick: Sequence[float], snare: Sequence[float],
           hats: Sequence[float], rate: float, beat: float,
           spans: Sequence[Tuple[float, float]]) -> Dict[str, float]:
    """What the drums do, read off their onset strength folded on the bar.

    Nothing here depends on which beat of the bar is the first, which the
    analysis does not know: every measure compares the four beats with one
    another, or a beat with what is between beats.
    """
    # Where the beat falls: the place in a beat the kick and the snare
    # together are strongest. Not the kick alone - on a broken beat half
    # of its hits are between beats, and the phase it gave put the snares
    # there too - and not the hats, which live between beats.
    one = fold(kick, rate, beat, 0.0, spans, beats=1)
    two = fold(snare, rate, beat, 0.0, spans, beats=1)
    top_one = max(one) or 1.0
    top_two = max(two) or 1.0
    both = [a / top_one + b / top_two for a, b in zip(one, two)]
    smooth = [both[i - 1] + 2.0 * both[i] + both[(i + 1) % len(both)]
              for i in range(len(both))]
    place = smooth.index(max(smooth))
    # And the snare on the beat rather than between: an off-beat bass line
    # in the kick's bands can outweigh the kick itself, and put the beat
    # half a beat out - three records in twenty-four.
    half = PER_BEAT // 2
    on = two[place] + two[(place + 1) % PER_BEAT] + two[place - 1]
    off_place = (place + half) % PER_BEAT
    off = (two[off_place] + two[(off_place + 1) % PER_BEAT]
           + two[off_place - 1])
    if off > on * 1.3:
        place = off_place
    phase = place / PER_BEAT * beat
    k = fold(kick, rate, beat, phase, spans)
    sn = fold(snare, rate, beat, phase, spans)
    h = fold(hats, rate, beat, phase, spans)
    out: Dict[str, float] = {"phase": phase}

    def quarters(pattern):
        base = _floor(pattern)
        return [max(0.0, _peak(pattern, q * PER_BEAT) - base) for q in range(BAR)]

    def between(pattern, offset):
        base = _floor(pattern)
        return [max(0.0, _peak(pattern, q * PER_BEAT + offset) - base)
                for q in range(BAR)]

    def share(part, whole, pattern):
        # Against a floor of a twentieth of the pattern's peak, so a
        # pattern with nothing at all somewhere is not infinitely more of
        # something else.
        return part / (whole + 0.05 * (max(pattern) or 1.0))

    kq = quarters(k)
    top = max(kq) or 1e-9
    # Four to the floor: every beat of the four as strong as the strongest.
    out["four"] = min(kq) / top if max(kq) > 0 else 0.0
    # And how much of the kick is between the beats, against on them.
    off = between(k, PER_BEAT // 2) + between(k, PER_BEAT // 4) + between(
        k, 3 * PER_BEAT // 4)
    out["kick_between"] = share(sum(off) / len(off), sum(kq) / BAR, k)
    snare_quarters = quarters(sn)
    sq = sorted(snare_quarters, reverse=True)
    first = sq[0] or 1e-9
    # A backbeat is two strong beats in four; half time is one.
    out["second_snare"] = sq[1] / first
    out["third_snare"] = sq[2] / first
    # Half time: the snare two beats after the kick's strongest beat, where
    # a backbeat puts it one beat either side. Measured from the kick
    # rather than by how many strong beats the snare has: on a real mix a
    # dubstep record's snare band is full of its bass, and its second
    # strongest beat was 0.83 of the first against a house record's 0.94 -
    # thirteen dubstep and trap records, sixteen house and techno. From the
    # kick, the dubstep records came out at 1.9 and the house records at
    # 0.94, and thirteen drum and bass records at no more than 0.74.
    #
    # Only where the kick has a strongest beat. Four to the floor has four
    # alike, and which of them came out strongest was chance - half the
    # time the one two beats before a clap, which is then half time. So it
    # fades out as the kick comes to every beat, where ``steady`` fades in.
    # (Weighing every beat by its kick instead, rather than taking the
    # strongest, lost the dubstep records: their kick's bands carry the
    # bass on the other beats.)
    lead = kq.index(max(kq))
    near = max(snare_quarters[(lead + 1) % BAR],
               snare_quarters[(lead + 3) % BAR])
    out["half"] = min(20.0, snare_quarters[(lead + 2) % BAR] / (
        near + 0.05 * (max(snare_quarters) or 1.0))) * (
        1.0 - _clamp((out["four"] - FOUR_FROM) / FOUR_OVER))
    snare_off = between(sn, PER_BEAT // 2)
    out["snare_offbeat"] = share(sum(snare_off) / BAR, sum(sq) / BAR, sn)
    # A snare on every beat it is counted in, evenly - on the beats or
    # between them, whichever the phase put it: drum and bass counted at
    # half its tempo, whose two and four are then every beat. Hip hop at
    # ninety has its snare on every other beat.
    on = sorted(quarters(sn))
    off = sorted(snare_off)
    evenly = max(on[0] / (on[-1] or 1e-9), off[0] / (off[-1] or 1e-9))
    out["snare_every"] = evenly if max(on[-1], off[-1]) > 0.0 else 0.0
    # Hats: sixteenths against eighths, thirty-seconds against sixteenths,
    # and where the off-beat eighth really falls - late of the middle of
    # the beat is swing.
    eighths = quarters(h) + between(h, PER_BEAT // 2)
    sixteenths = between(h, PER_BEAT // 4) + between(h, 3 * PER_BEAT // 4)
    out["hat_sixteenths"] = share(sum(sixteenths) / len(sixteenths),
                                  sum(eighths) / len(eighths), h)
    # Rolls by counting them: the share of beats in which the hats hit six
    # times or more - sixteenths are four, a roll of thirty-seconds eight.
    # Not by where in the bar they fall: a swung off-beat lands next to a
    # thirty-second, and a roll is a rate, not a place.
    out["hat_rolls"] = _roll_share(hats, rate, spans, beat, phase)
    late = []
    for q in range(BAR):
        window = [(h[(q * PER_BEAT + o) % len(h)], o)
                  for o in range(PER_BEAT // 2 - 2, PER_BEAT // 2 + 6)]
        strength, where = max(window)
        if strength > _floor(h):
            late.append((where - PER_BEAT // 2) / PER_BEAT)
    late.sort()
    out["swing"] = late[len(late) // 2] if late else 0.0
    # How far the drums stand out of their own floor at all: a record with
    # no drums folds to a flat line, and every measure above is noise.
    # The kick's strongest place in the bar against its floor - not the
    # four beats' average, which on a record whose kick is on one beat of
    # four is mostly empty beats, and under a detector's false hits fell
    # below any line a record with drums could be told by.
    out["kick_contrast"] = min(20.0, (max(k) - _floor(k)) / (
        _floor(k) + 0.02 * (max(k) or 1.0)))
    return out


#: The tempos looked for, in beats a minute, and where a person counts
#: from when a pulse could be counted at more than one speed.
SLOWEST, FASTEST = 60.0, 200.0
COUNTED = 120.0

#: Half time counted at this or faster is a backbeat counted twice over:
#: see rhythm_of. Dubstep, riddim and trap are counted at 140 to 150; hip
#: hop and half-time drum and bass at 80 to 100, whose double is 160 to
#: 200. Against a DJ program's tempos on 89 records, recounting from here
#: put four more right and none wrong that had been right.
HALVED_FROM = 155.0
#: How far the snare two beats from the kick has to stand over the snare
#: one beat from it for the record to be half time (see rhythm's "half").
HALF_FROM, HALF_SURE = 1.1, 1.8


def _sharpness(values: Sequence[float], rate: float, beat: float,
               step: int = 1) -> float:
    """How sharply ``values`` folds onto a beat of ``beat`` seconds: the
    strongest place in the beat against the average of them all. At the
    tempo a record is really in, every beat's hits land in the same place
    and the fold is a spike; a hundredth of a beat a minute off and over
    seven minutes they have drifted apart and it is a smear."""
    bins = [0.0] * PER_BEAT
    count = [0] * PER_BEAT
    for index in range(0, len(values), step):
        slot = int((index / rate / beat) % 1.0 * PER_BEAT)
        bins[slot] += values[index]
        count[slot] += 1
    means = [bins[i] / count[i] if count[i] else 0.0 for i in range(PER_BEAT)]
    smooth = [means[i - 1] + 2.0 * means[i] + means[(i + 1) % PER_BEAT]
              for i in range(PER_BEAT)]
    average = sum(smooth) / PER_BEAT
    return max(smooth) / average if average > 0.0 else 0.0


def tempo_from(values: Sequence[float], rate: float) -> float:
    """The tempo of an onset strength, in beats a minute, or 0.

    The lag it repeats itself at most - with the bar above it counted in,
    and a preference for tempos near COUNTED so that of two pulses an
    octave apart the one a person would tap is chosen - and then refined
    to a hundredth of a beat a minute by how sharply the whole track folds
    onto it. See _sharpness.
    """
    count = len(values)
    if count < rate * 8:
        return 0.0
    mean = sum(values) / count
    centred = [value - mean for value in values]
    shortest = max(2, int(60.0 * rate / FASTEST))
    longest = int(60.0 * rate / SLOWEST) + 1

    def repeat(lag: int) -> float:
        if lag >= count:
            return 0.0
        return sum(centred[i] * centred[i + lag]
                   for i in range(0, count - lag, 2)) / (count - lag)

    table: Dict[int, float] = {}

    def at(lag: int) -> float:
        if lag not in table:
            table[lag] = repeat(lag)
        return table[lag]

    best, best_score = 0, -1e18
    for lag in range(shortest, longest + 1):
        # A comb: the beat, and two, three and four of it. A pattern that
        # repeats a bar at a time - drum and bass's kick is on one and the
        # and of three - lines up at four beats however broken it is
        # inside the bar, and a false tempo whose multiples never land on
        # the bar gets nothing from it.
        comb = sum(at(lag * k) / k for k in (1, 2, 3, 4) if lag * k < count)
        bpm = 60.0 * rate / lag
        prefer = math.exp(-0.5 * (math.log2(bpm / COUNTED) / 0.9) ** 2)
        score = comb * prefer
        if score > best_score:
            best, best_score = lag, score
    if best <= 0 or at(best) <= 0.0 and best_score <= 0.0:
        return 0.0
    rough = 60.0 * rate / best
    # Finer than a whole lag can say: at sixty readings a second one lag
    # is four beats a minute at 128.
    step = 2 if count > rate * 120 else 1
    candidates = [rough * (1.0 + offset / 400.0) for offset in range(-12, 13)]
    rough = max(candidates, key=lambda bpm: _sharpness(values, rate,
                                                      60.0 / bpm, step))
    fine = [rough + offset * 0.02 for offset in range(-15, 16)]
    return max(fine, key=lambda bpm: _sharpness(values, rate, 60.0 / bpm, step))


def _counted_from(values: Sequence[float], rate: float) -> float:
    """The tempo beatmap reads off these onsets, brought between SLOWEST
    and FASTEST, or 0."""
    import beatmap

    hits = beatmap.onsets(list(values), rate, 0.5)
    if len(hits) < 6:
        return 0.0
    bpm = beatmap.tempo_of(hits)[0]
    if not bpm or bpm <= 0.0:
        return 0.0
    while bpm > FASTEST:
        bpm /= 2.0
    while bpm < SLOWEST:
        bpm *= 2.0
    return bpm


#: What a reading of a tempo is most often wrong by, other than an octave:
#: a pattern that repeats every dotted beat - drum and bass's kick on one
#: and the and of three - reads as three quarters or four thirds of it, a
#: shuffle as two thirds or three halves.
RELATIVES = (1.0, 2.0, 0.5, 1.5, 2.0 / 3.0, 4.0 / 3.0, 0.75, 1.25, 0.8)

#: How wide the preference for tempos near COUNTED is, in octaves.
PREFER_WIDTH = 1.5


def choose_tempo(values: Sequence[float], rate: float,
                 readings: Sequence[float]) -> float:
    """The tempo, to a hundredth, among the readings and their usual
    mistakes (RELATIVES): the one the whole track folds onto most sharply,
    near a tempo a person would count. Measured against a DJ program's
    tempos on sixty records: 55 right (40 exactly, 15 an octave apart,
    which is how it files drum and bass), against 24 of 40 exactly for the
    beat maps' own readings - the rest were wrong by a third or a quarter.
    """
    candidates = sorted({round(within(reading * ratio), 3)
                         for reading in readings if reading and reading > 0.0
                         for ratio in RELATIVES} - {0.0})
    if not candidates:
        return 0.0
    step = 2 if len(values) > rate * 120 else 1
    best = max(candidates, key=lambda bpm: _sharpness(
        values, rate, 60.0 / bpm, step) * math.exp(
        -0.5 * (math.log2(bpm / COUNTED) / PREFER_WIDTH) ** 2))
    return _refined(values, rate, best)


def _refined(values: Sequence[float], rate: float, bpm: float) -> float:
    """``bpm`` to a hundredth, by how sharply the track folds onto it."""
    if bpm <= 0.0:
        return 0.0
    step = 2 if len(values) > rate * 120 else 1
    coarse = [bpm * (1.0 + offset / 400.0) for offset in range(-12, 13)]
    near = max(coarse, key=lambda b: _sharpness(values, rate, 60.0 / b, step))
    fine = [near + offset * 0.02 for offset in range(-15, 16)]
    return max(fine, key=lambda b: _sharpness(values, rate, 60.0 / b, step))


def _drum_spans(kick: Sequence[float], rate: float, beat: float
                ) -> List[Tuple[float, float]]:
    """The stretches the kick is playing in, a bar at a time: the bars
    whose kick strength is at least half of a typical kicked bar's."""
    span = beat * BAR
    length = len(kick) / rate
    bars = []
    at = 0.0
    while at < length:
        bars.append((at, min(length, at + span),
                     _bar_strength((kick, rate), at, min(length, at + span))))
        at += span
    some = sorted(value for _s, _e, value in bars if value > 0.0)
    if not some:
        return [(0.0, length)]
    typical = some[len(some) * 2 // 3]
    out: List[Tuple[float, float]] = []
    for start, end, value in bars:
        if value >= typical * 0.5:
            if out and abs(out[-1][1] - start) < 1e-6:
                out[-1] = (out[-1][0], end)
            else:
                out.append((start, end))
    return out or [(0.0, length)]


def within(bpm: float) -> float:
    """A tempo brought between SLOWEST and FASTEST by octaves, or 0."""
    if not bpm or bpm <= 0.0 or bpm != bpm:
        return 0.0
    while bpm > FASTEST:
        bpm /= 2.0
    while bpm < SLOWEST:
        bpm *= 2.0
    return bpm


def rhythm_of(kit: Optional[dict], tempo: float = 0.0,
              hint: float = 0.0) -> Optional[dict]:
    """The drums' own tempo, beat and pattern, from the kit's onset
    strength (see beatmap.BeatMap.flux). The heavy half of reading a
    track's style, done where the drums are found - in a process of its
    own - rather than on the thread drawing the picture.

    ``{"tempo", "phase", "faster", "measured", "spans", "beats"}``: beats
    a minute; the time of a beat, in seconds; 2 for a record heard at half
    its speed (see ``read``) and otherwise 1; what the drums do (see
    ``rhythm``); where the kick is playing; and the beat times where the
    tempo moves, or None where one steady grid fits (see ``follow``). None
    with no kit.

    The beat maps the rest of the app counts in are found at fifteen
    readings a second and phased from the first thing they heard, which on
    a real record is as often between two beats as on one: measured on
    sixteen records, a third of a beat out on one and nearly half on
    another. This is phased from where the kick and the snare land over
    the whole track, at sixty readings a second.
    """
    if not kit:
        return None
    curves = {}
    for name in ("Kick", "Snare", "Hats"):
        found = kit.get(name)
        values = tuple(getattr(found, "flux", ()) or ())
        rate = float(getattr(found, "rate", 0.0) or 0.0)
        if not values or rate <= 0.0:
            return None
        curves[name] = (values, rate)
    rate = curves["Kick"][1]
    both = [a + b for a, b in zip(curves["Kick"][0], curves["Snare"][0])]
    if tempo <= 0.0:
        tempo = choose_tempo(both, rate, [
            hint, _counted_from(curves["Kick"][0], rate),
            _counted_from(curves["Snare"][0], rate),
            _counted_from(both, rate),
            _counted_from(curves["Hats"][0], rate)])
    if tempo <= 0.0:
        tempo = tempo_from(both, rate)
    if tempo <= 0.0:
        return None
    beat = 60.0 / tempo
    spans = _drum_spans(curves["Kick"][0], rate, beat)
    measured = rhythm(curves["Kick"][0], curves["Snare"][0],
                      curves["Hats"][0], rate, beat, spans)
    # Kick on one and snare on three at 176 is the kick and the snare of a
    # backbeat at 88, beat for beat: the fold cannot tell hip hop from half
    # time, only the tempo can. That fast it is hip hop, or half-time drum
    # and bass, and counted where a DJ program and a nodding head count it
    # - at 88, with the snare on two and four. Ridden at 176 the road ran
    # at twice the speed of the music. (Two hip hop records in eight were
    # counted at double; dubstep at 140 or 150 is not touched.)
    if (tempo >= HALVED_FROM and measured["half"] >= HALF_SURE
            and measured["kick_contrast"] > 1.6):
        tempo /= 2.0
        beat *= 2.0
        measured = rhythm(curves["Kick"][0], curves["Snare"][0],
                          curves["Hats"][0], rate, beat, spans)
    faster = 1.0
    # Counted at half its real tempo - drum and bass at 87 rather than
    # 174 - a record has its snare on the off-beat of every beat it is
    # counted in, which at its own tempo is two and four.
    # Only where there are drums: a snare roll through a build is on every
    # beat too, and a record with no kick at all doubled on it.
    if (tempo <= 100.0 and measured["snare_every"] > 0.6
            and measured["kick_contrast"] > 1.6):
        faster = 2.0
        beat /= 2.0
        measured = rhythm(curves["Kick"][0], curves["Snare"][0],
                          curves["Hats"][0], rate, beat, spans)
    phase, beat = on_the_hits(measured["phase"], beat, kit)
    measured["phase"] = phase
    return {"tempo": 60.0 / beat, "phase": phase,
            "faster": faster, "measured": measured, "spans": spans,
            "beats": follow(both, rate, 60.0 / beat, phase)}


#: How far from the grid a hit may be and still say where the grid is, in
#: beats: a kick on the beat is a few hundredths out at most, and one on
#: the and is half a beat out and says nothing about it.
NEAR_THE_GRID = 0.15
#: How far the hits may move the tempo the fold found, as a share of it:
#: a refinement, not a second opinion.
REFINE_MOST = 0.002


def on_the_hits(phase: float, beat: float,
                kit: Optional[dict]) -> Tuple[float, float]:
    """The beat's phase and length, moved onto where the drums really hit.

    The fold finds the beat to one twenty-fourth of it - 21 ms at 120 - and
    a hit's onset strength peaks a reading before the hit itself, so the
    beat it found sat a slot early: every scene's pulse came round 17 ms
    before the kick it was for. And its tempo is found to a fiftieth of a
    beat a minute, which over five minutes is 50 ms of drift by the end.

    The hits the kit found are timed to the sample. Each kick near the grid
    is given its beat's number, and the straight line through them - when
    each landed against which beat it was - is the grid, phase and length
    both; the snares where there is no kick. Where the line will not do,
    too few hits or a tempo further from the fold's than a refinement
    should move it, the median of how far they are out puts the phase on
    them and the tempo stays.
    """
    if beat <= 0.0:
        return phase, beat
    for name in ("Kick", "Snare"):
        found = (kit or {}).get(name)
        hits = [float(getattr(hit, "at", hit))
                for hit in (getattr(found, "beats", None) or ())]
        if len(hits) < 8:
            continue
        here, length = phase, beat
        for _round in range(2):
            numbered = []
            for at in hits:
                place = (at - here) / length
                number = round(place)
                if abs(place - number) <= NEAR_THE_GRID:
                    numbered.append((number, at))
            if len(numbered) < 8:
                break
            count = len(numbered)
            mean_n = sum(n for n, _t in numbered) / count
            mean_t = sum(t for _n, t in numbered) / count
            spread = sum((n - mean_n) ** 2 for n, _t in numbered)
            slope = (sum((n - mean_n) * (t - mean_t) for n, t in numbered)
                     / spread) if spread > 0.0 else 0.0
            if spread > 0.0 and abs(slope - beat) <= beat * REFINE_MOST:
                here, length = mean_t - slope * mean_n, slope
            else:
                offsets = sorted(t - (here + n * length) for n, t in numbered)
                here += offsets[len(offsets) // 2]
                break
        if len(numbered) >= 8:
            # Near the start of the track, so the number is small.
            here -= math.floor((here - hits[0]) / length) * length
            return here, length
    return phase, beat


#: Tempo following. The drums are read in stretches of FOLLOW_SPAN beats,
#: FOLLOW_HOP apart. A stretch fails the track's tempo when another tempo
#: folds it far more sharply (FAIL) and is more than STEADY away; only a run
#: of FAILING such stretches means the tempo really moves.
FOLLOW_SPAN = 16
FOLLOW_HOP = 8
STEADY = 0.015
FAIL = 0.6
FAILING = 4
#: A stretch that folds best at one of these shares of the track's tempo is
#: playing a different rhythm at the same speed, not a new tempo.
METRES = (0.5, 2.0 / 3.0, 0.75, 4.0 / 3.0, 1.5, 2.0)
#: How far from the whole track's tempo a stretch's is looked for.
FOLLOW_RANGE = 0.3
#: How sharp a stretch's fold must be to say anything: a breakdown with no
#: drums folds flat.
CLEAR = 1.5
#: Stretches whose tempos are this close are one part.
SAME_PART = 0.01


def _stretch_tempo(stretch: Sequence[float], rate: float, tempo: float):
    """The tempo a stretch folds onto most sharply near ``tempo``, and how
    sharply."""
    best = (0.0, tempo)
    steps = int(FOLLOW_RANGE * 100)
    for k in range(-steps, steps + 1):
        bpm = tempo * (1.0 + k / 100.0)
        best = max(best, (_sharpness(stretch, rate, 60.0 / bpm), bpm))
    near = best[1]
    for k in range(-5, 6):
        bpm = near * (1.0 + k / 1000.0)
        best = max(best, (_sharpness(stretch, rate, 60.0 / bpm), bpm))
    return best[1], best[0]


def _part_phase(values: Sequence[float], rate: float, beat: float,
                start: float, end: float) -> float:
    """Where the beat falls in one part of a track, as a time."""
    pattern = fold(values, rate, beat, 0.0, [(start, end)], beats=1)
    smooth = [pattern[i - 1] + 2.0 * pattern[i]
              + pattern[(i + 1) % PER_BEAT] for i in range(PER_BEAT)]
    return smooth.index(max(smooth)) / PER_BEAT * beat


def follow(values: Sequence[float], rate: float, tempo: float,
           phase: float) -> Optional[List[float]]:
    """Beat times that follow the tempo where it moves, or None where one
    steady grid at ``tempo`` and ``phase`` fits the whole track.

    A track that changes tempo is cut into parts, each with a steady grid
    of its own fitted the way the whole track's is."""
    if tempo <= 0.0 or rate <= 0.0 or not values:
        return None
    beat = 60.0 / tempo
    length = len(values) / rate
    span = beat * FOLLOW_SPAN
    if length < span * 2.0:
        return None
    readings = []      # (start, end, tempo or None, fails)
    start = 0.0
    while start + span <= length:
        stretch = values[int(start * rate):int((start + span) * rate)]
        bpm, sharp = _stretch_tempo(stretch, rate, tempo)
        here = _sharpness(stretch, rate, beat)
        clear = sharp >= CLEAR
        ratio = bpm / tempo
        fails = (clear and abs(ratio - 1.0) > STEADY and here < sharp * FAIL
                 and not any(abs(ratio / metre - 1.0) < STEADY
                             for metre in METRES))
        readings.append((start, start + span, bpm if clear else None, fails))
        start += beat * FOLLOW_HOP
    # A run of failing stretches that agree with each other on the tempo
    # they do fit.
    run, longest, agreed = 0, 0, None
    for _s, _e, bpm, fails in readings:
        if fails and agreed is not None and abs(bpm / agreed - 1.0) <= STEADY:
            run += 1
        elif fails:
            run, agreed = 1, bpm
        else:
            run, agreed = 0, None
        longest = max(longest, run)
    if longest < FAILING:
        return None
    # Parts: neighbouring stretches whose tempos agree. A stretch with no
    # clear beat belongs to the part before it.
    parts: List[List] = []        # [start, end, [tempos]]
    for begin, finish, bpm, _fails in readings:
        if bpm is None or (parts and abs(bpm / statistics.median(
                parts[-1][2]) - 1.0) <= SAME_PART):
            if parts:
                parts[-1][1] = finish
                if bpm is not None:
                    parts[-1][2].append(bpm)
                continue
            if bpm is None:
                continue
        parts.append([begin, finish, [bpm]])
    if len(parts) < 2:
        return None
    parts[0][0] = 0.0
    parts[-1][1] = length
    # Parts overlap by the stretches' overlap; each starts where the one
    # before it is half way through their shared stretch.
    for before, after in zip(parts, parts[1:]):
        middle = (after[0] + before[1]) / 2.0
        before[1] = after[0] = middle
    out: List[float] = []
    for begin, finish, bpms in parts:
        bpm = _refined(values[int(begin * rate):int(finish * rate)], rate,
                       statistics.median(bpms))
        length_here = 60.0 / bpm
        found = _part_phase(values, rate, length_here, begin, finish)
        at = found + math.ceil((begin - found) / length_here) * length_here
        # From the last beat of the part before, never sooner than half a
        # beat after it.
        if out:
            while at < out[-1] + length_here * 0.5:
                at += length_here
            while at - length_here >= out[-1] + length_here * 0.5:
                at -= length_here
        else:
            while at - length_here >= 0.0:
                at -= length_here
        while at < finish:
            out.append(at)
            at += length_here
    return out


def read(chart: Optional[dict], beat: float, grid: Optional[float],
         contour: Optional[dict], harmony: Optional[dict] = None,
         length: float = 0.0, flux: Optional[dict] = None,
         rhythm_found: Optional[dict] = None, light: bool = False) -> Style:
    """The style and the sections of a track - the cheap half, done where
    the game is. ``rhythm_found`` is ``rhythm_of``'s answer, worked out
    with the drums; without it, the drums' pattern is read here from
    ``flux`` or, failing that, from the chart's hit times at ``beat``.
    ``grid`` is the time of any one beat, for when nothing better is
    known. ``light`` reads no pattern here at all - for the thread that
    draws, while the drums' own reading has not arrived - and leaves the
    style's numbers at nothing but the sections."""
    chart = {name: sorted(times) for name, times in (chart or {}).items()}
    loud = list((contour or {}).get("loud") or ())
    rate = float((contour or {}).get("rate") or 0.0)
    if length <= 0.0 and loud and rate > 0.0:
        length = len(loud) / rate
    style = Style(seed=seed_of(contour, chart))
    curves: Dict[str, Tuple[Sequence[float], float]] = {}
    for name in ("Kick", "Snare", "Hats"):
        kept = (flux or {}).get(name)
        if kept and kept[0] and kept[1] > 0.0:
            curves[name] = (kept[0], float(kept[1]))
        else:
            curves[name] = (envelope_from(chart.get(name, ()), 60.0, length),
                            60.0)
    found = rhythm_found
    style.from_drums = found is not None
    if found is None and light:
        style.sections = sections(chart, beat, grid or 0.0, contour or {},
                                  length)
        if beat > 0.0:
            style.tempo = 60.0 / beat
        return style
    if found is None and beat > 0.0:
        both = [a + b for a, b in zip(curves["Kick"][0], curves["Snare"][0])]
        if any(both):
            kit = {name: _Kept(values, rate_here)
                   for name, (values, rate_here) in curves.items()}
            found = rhythm_of(kit, tempo=60.0 / beat)
    if found is None:
        if beat <= 0.0 or grid is None:
            # No tempo: nothing about the beat can be said. What is left
            # is how busy and how dramatic, and whether there is a tune.
            style.calm = 1.0 - _clamp(sum(len(t) for t in chart.values())
                                      / max(1.0, length) / 6.0)
            style.melodic = _clamp(float((harmony or {}).get("tonal", 0.0)))
            style.sections = sections(chart, 0.0, 0.0, contour or {}, length)
            return style
        found = {"tempo": 60.0 / beat, "phase": grid % beat, "faster": 1.0,
                 "measured": rhythm(curves["Kick"][0], curves["Snare"][0],
                                    curves["Hats"][0], curves["Kick"][1],
                                    beat, [(0.0, length)]),
                 "spans": [(0.0, length)]}
    beat = 60.0 / found["tempo"]
    measured = dict(found["measured"])
    style.tempo = found["tempo"]
    style.faster = float(found.get("faster", 1.0))
    style.beat_phase = float(found["phase"])
    style.beats = list(found.get("beats") or ())
    # The first downbeat: the beat of four the kick is strongest on and the
    # snare weakest, from the drums folded over the whole track.
    whole = [(0.0, length)]
    k4 = fold(curves["Kick"][0], curves["Kick"][1], beat, style.beat_phase,
              whole)
    s4 = fold(curves["Snare"][0], curves["Snare"][1], beat, style.beat_phase,
              whole)
    scores = [_peak(k4, q * PER_BEAT) - 0.8 * _peak(s4, q * PER_BEAT)
              for q in range(BAR)]
    top = max(scores)
    # Near the best by a share of how far apart the best and the worst
    # are: a build's snare roll on every beat leaves the first and the
    # third a hair apart, and a tolerance off the best alone split them.
    near = [q for q in range(BAR)
            if scores[q] >= top - 0.25 * (top - min(scores))]
    # Where four to the floor leaves the first beat and the third alike,
    # the one the track changes on: a part begins on a bar, so the
    # loudness jumps at the start of bars and not in the middle of them.
    downbeat = max(near, key=lambda q: _changes_on(
        loud, rate, style.beat_phase + q * beat, beat * BAR, length))
    first = style.beat_phase + downbeat * beat
    while first - beat * BAR >= -beat * 0.5:
        first -= beat * BAR
    style.downbeat = first
    style.sections = sections(chart, beat, style.beat_phase, contour or {},
                              length, kick=curves["Kick"], first=first)
    tonal = float((harmony or {}).get("tonal", 0.0))
    lead = [value for value in (harmony or {}).get("lead") or ()]
    moves = sum(1 for a, b in zip(lead, lead[1:])
                if a is not None and b is not None and abs(a - b) >= 0.9)
    held = sum(1 for value in lead if value is not None)
    motion = moves / held if held else 0.0
    onsets = sum(len(times) for times in chart.values()) / max(1.0, length)
    spread = 0.0
    if style.sections:
        levels = sorted(s.level for s in style.sections)
        spread = levels[-1] - levels[0]
    measured.update({"tonal": tonal, "motion": motion, "onsets": onsets,
                     "spread": spread})
    style.measured = measured
    # How far the kick stands out of its own floor: a record with drums
    # 3.7 to 20 even buried in false hits, a chance fold of hits scattered
    # anywhere about 1.2.
    drums = _clamp((measured["kick_contrast"] - 1.6) / 1.0)
    style.steady = drums * _clamp((measured["four"] - FOUR_FROM)
                                  / FOUR_OVER) * (
        1.0 - _clamp((measured["kick_between"] - 0.35) / 0.4))
    half = _clamp((measured["half"] - HALF_FROM) / (HALF_SURE - HALF_FROM))
    style.heavy = drums * half * (1.0 - style.steady)
    style.broken = drums * _clamp((measured["kick_between"] - 0.3) / 0.4) * (
        1.0 - style.heavy) * (1.0 - style.steady)
    style.swung = _clamp((measured["swing"] - 0.04) / 0.1)
    style.rolls = _clamp((measured["hat_rolls"] - 0.04) / 0.15)
    style.hard = _clamp((style.tempo - 135.0) / 25.0) * style.steady
    style.melodic = _clamp(tonal * 1.2) * _clamp(motion / 0.35)
    style.calm = (1.0 - drums) * (1.0 - _clamp(onsets / 7.0))
    style.drama = _clamp(spread / 0.7)
    return style


class _Kept:
    """An onset strength standing in for a kit's beat map. See read."""

    def __init__(self, flux, rate) -> None:
        self.flux = flux
        self.rate = rate
