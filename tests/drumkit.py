"""Drum tracks written to known times, so detection can be measured.

The element detector says where the kick, the snare and the hats are.
Whether it is any good at it is not a question you can ask of a recording
without first sitting down and writing out where every hit is - and the
answer would then be about that recording.

So the recordings are written here instead. Every hit is placed by this
file, which means the truth is exactly known, the same on every machine,
and nothing anybody owns has to be checked into a public repository.

Four snares, because "snare" is not one sound and pretending it is, is
what was wrong with the detector:

    bright   an acoustic snare: a two hundred hertz body and a lot of air
    tight    an electronic one: body, a little noise, and no air at all
    clap     all crack and no body
    rim      a sharp mid crack, nothing above or below it

Held against those four, the detector used to find the first two
perfectly and neither of the other two.

One rule matters when writing these. **A sample that stops while it is
still moving is a step, and a step is a broadband click.** The kick here
stopped at about 1.5 per cent of full height, which put a click 150 ms
after every one of them; the detector called those hits, quite correctly,
and the false positives looked exactly like a rimshot. That very nearly
became a change to the detector. Everything is faded to nothing now, and
``test_the_written_track_is_clean`` checks that it stayed that way.
"""

from __future__ import annotations

import math
import random
from array import array
from typing import Dict, List, Sequence, Tuple

RATE = 48000

#: Beats in a bar, and the pattern: kick on one and three, snare on two
#: and four, hats on every eighth. The oldest pattern there is, and the
#: one any of this has to work on before it works on anything else.
BEATS = 4


def _fall(t: float, rate: float) -> float:
    return math.exp(-t * rate)


def taper(samples: List[float], share: float = 0.12) -> List[float]:
    """Fade the last ``share`` of a sample to nothing.

    See the note at the top: without this every sample ends in a click,
    and the click is a real onset that a detector is right to find.
    """
    count = len(samples)
    edge = max(1, int(count * share))
    for index in range(count - edge, count):
        samples[index] *= (count - 1 - index) / edge
    return samples


#: How much beater there is in a kick, and how fast it goes.
#:
#: A kick is not only a falling pitch. A beater striking a head makes a
#: click, and the click is broadband: real kicks carry real energy above
#: 2 kHz for the first few milliseconds.
#:
#: Leaving it out is a way of drawing wrong conclusions. Without it the
#: written kicks had almost nothing up top, so a sweep of the detector's
#: cap on the top share said 0.06 was best - and on real recordings 0.06
#: took one track from 27 kicks a minute to 9 and another from 25 to none.
#:
#: **The cap on the kick's top share cannot be tuned from this file.**
#: Even with a beater the written kicks only reach 0.09 up top, and real
#: ones plainly go well past 0.10: capping there cost 45 per cent of them
#: on a real recording. A mix has cymbals, synths and room in it, and they
#: all land on the kick sometimes. What is written here is one kick alone
#: in silence, which is the wrong question to ask of that bound.
#:
#: 0.6 and 90 is an 11 ms beater at a bit over half the body's height,
#: which is a kick. It is not chosen to make any number come out.
CLICK = 0.6
CLICK_FALL = 90.0


def kick(length: float = 0.16) -> List[float]:
    """A pitch falling from 120 Hz to 45, with a beater on the front."""
    rng = random.Random(7)
    out = []
    for index in range(int(length * RATE)):
        moment = index / RATE
        hertz = 45.0 + 75.0 * _fall(moment, 34.0)
        body = (math.sin(2 * math.pi * hertz * moment)
                * _fall(moment, 26.0))
        click = rng.uniform(-1.0, 1.0) * CLICK * _fall(moment, CLICK_FALL)
        out.append(body + click)
    return taper(out)


def hat(length: float = 0.05) -> List[float]:
    """High-passed hiss: the difference between one noise sample and the
    last, which takes everything but the top off it."""
    rng = random.Random(11)
    out, last = [], 0.0
    for index in range(int(length * RATE)):
        noise = rng.uniform(-1.0, 1.0)
        out.append((noise - last) * _fall(index / RATE, 90.0))
        last = noise
    return taper(out)


def snare(kind: str = "bright", length: float = 0.19,
          seed: int = 5) -> List[float]:
    """One of the four snares. See the module docstring for what each is."""
    rng = random.Random(seed)
    count = int(length * RATE)
    out: List[float] = []
    soft = hard = 0.0
    for index in range(count):
        moment = index / RATE
        noise = rng.uniform(-1.0, 1.0)
        if kind == "bright":
            soft = soft * 0.55 + noise * 0.45
            body = math.sin(2 * math.pi * 190.0 * moment) * _fall(moment, 34.0)
            value = (body * 0.5 + (noise - soft) * 0.75) * _fall(moment, 22.0)
        elif kind == "tight":
            soft = soft * 0.90 + noise * 0.10
            body = (math.sin(2 * math.pi * 200.0 * moment)
                    + 0.6 * math.sin(2 * math.pi * 330.0 * moment))
            value = (body * 0.7 + soft * 0.6) * _fall(moment, 30.0)
        elif kind == "clap":
            soft = soft * 0.72 + noise * 0.28
            hard = hard * 0.20 + soft * 0.80
            value = (soft - hard * 0.35) * _fall(moment, 26.0)
        elif kind == "rim":
            value = (math.sin(2 * math.pi * 420.0 * moment)
                     + 0.7 * math.sin(2 * math.pi * 1150.0 * moment)
                     + 0.4 * noise) * _fall(moment, 60.0)
        else:
            raise ValueError(f"no such snare: {kind}")
        out.append(value)
    return taper(out)


def note(hertz: float, length: float = 0.42) -> List[float]:
    """A pitched tone with two harmonics.

    The thing a snare gets confused with: it lives in the same part of
    the spectrum and it is not a drum. Deliberately never placed on a
    snare, so anything it triggers is a false positive and can be counted.
    """
    out = []
    for index in range(int(length * RATE)):
        moment = index / RATE
        out.append((math.sin(2 * math.pi * hertz * moment)
                    + 0.5 * math.sin(2 * math.pi * hertz * 2 * moment)
                    + 0.3 * math.sin(2 * math.pi * hertz * 3 * moment))
                   * _fall(moment, 5.0))
    return taper(out)


def sub(hertz: float, length: float) -> List[float]:
    """A held bass note, which is what a kick has to be found under.

    House is the case that showed this up: a kick on every beat with a
    bassline running underneath it, in the same part of the spectrum. The
    band the kick is found in is never quiet, so the kick's *rise* in it
    is a fraction of what it is over silence.
    """
    out = []
    count = int(length * RATE)
    for index in range(count):
        moment = index / RATE
        # Barely decaying: the point is that it is still sounding when the
        # next kick lands.
        out.append((math.sin(2 * math.pi * hertz * moment)
                    + 0.35 * math.sin(2 * math.pi * hertz * 2 * moment))
                   * (0.75 + 0.25 * math.sin(2 * math.pi * 1.5 * moment)))
    return taper(out, 0.06)


def track(kind: str = "bright", bpm: float = 120.0, seconds: float = 22.0,
          melody: bool = False, house: bool = False
          ) -> Tuple[array, Dict[str, List[float]]]:
    """The pattern, as stereo PCM, and where every hit really is.

    ``house`` is four to the floor with a bassline under it: a kick on
    every beat, offbeat hats, and a held sub that never gets out of the
    way. It is the pattern the kick detector was reported as missing.
    """
    frames = int(seconds * RATE)
    buffer = [0.0] * frames
    beat = 60.0 / bpm
    truth: Dict[str, List[float]] = {"Kick": [], "Snare": [], "Hats": []}

    def put(at: float, sample: Sequence[float], gain: float) -> None:
        start = int(at * RATE)
        for index, value in enumerate(sample):
            where = start + index
            if 0 <= where < frames:
                buffer[where] += value * gain

    one_kick, one_hat, one_snare = kick(), hat(), snare(kind)
    notes = [note(330.0), note(440.0), note(247.0)]
    bassline = [sub(hz, beat * BEATS) for hz in (55.0, 65.4, 49.0, 58.3)]
    for bar in range(int(seconds / (beat * BEATS))):
        top = bar * beat * BEATS
        if house:
            put(top, bassline[bar % len(bassline)], 0.55)
        hats_on = ((1, 3, 5, 7) if house else range(BEATS * 2))
        for step in hats_on:
            put(top + step * beat * 0.5, one_hat, 0.45)
            truth["Hats"].append(top + step * beat * 0.5)
        for step in (range(BEATS) if house else (0, 2)):
            put(top + step * beat, one_kick, 0.95)
            truth["Kick"].append(top + step * beat)
        for step in ((1, 3) if not house else (1, 3)):
            put(top + step * beat, one_snare, 0.85 if not house else 0.55)
            truth["Snare"].append(top + step * beat)
        if melody:
            # On the off-beats, never on a snare.
            for sound, when in zip(notes, (0.5, 2.5, 3.5)):
                put(top + beat * when, sound, 0.30)

    peak = max(1e-9, max(abs(v) for v in buffer))
    pcm = array("h")
    for value in buffer:
        level = value / peak
        if house:
            # What a master limiter does, which is most of what makes a
            # dance record hard to read: the loud parts are pulled down
            # towards the quiet ones until the whole thing sits near the
            # ceiling, and the rise at a transient - which is the only
            # thing any of this detects - is a fraction of what it was.
            level = math.tanh(level * 3.4) / math.tanh(3.4)
        one = int(max(-1.0, min(1.0, level * 0.88)) * 32000)
        pcm.append(one)
        pcm.append(one)
    for name in truth:
        truth[name] = sorted(truth[name])
    return pcm, truth


def score(found: Sequence[float], truth: Sequence[float],
          slack: float = 0.07) -> Tuple[int, float, float]:
    """(matched, recall, precision), one detected hit to one real one.

    One to one on purpose: two flashes on one snare is not two right
    answers, and a detector that fires ten times a beat should not score
    perfectly for having covered everything.
    """
    taken: set = set()
    matched = 0
    for at in found:
        best, which = slack + 1.0, None
        for index, real in enumerate(truth):
            if index in taken:
                continue
            apart = abs(real - at)
            if apart < best:
                best, which = apart, index
        if which is not None and best <= slack:
            taken.add(which)
            matched += 1
    return (matched, matched / max(1, len(truth)),
            matched / max(1, len(found)))


# --------------------------------------------------------------------------
# Styles
# --------------------------------------------------------------------------
#: Patterns, written as where each drum falls within a bar of four beats.
#:
#: ``track`` above writes one pattern, which is the oldest one there is and
#: the one anything has to work on first. It is not the one this is used on.
#: A detector tuned against a rock beat meets half-time, breakbeats, hat
#: rolls at three times the tempo, and four-to-floor kicks buried under a
#: sub that never stops - and "kick on one and three" tells you nothing
#: about any of them.
#:
#: Each entry is (bpm, kick beats, snare beats, hat beats, snare kind,
#: how loud the sub is, whether it is limited, swing). Beats are counted
#: from zero and may be fractions. Swing delays every off-beat by that
#: share of a half-beat, the way a shuffle does.
STYLES: Dict[str, dict] = {
    # Four to the floor, offbeat hats, a clap on two and four, and a sub
    # that never gets out of the kick's way.
    "house": dict(bpm=124.0, kick=(0, 1, 2, 3), snare=(1, 3),
                  hat=(0.5, 1.5, 2.5, 3.5), kind="clap", sub=0.55,
                  limit=True),
    # Faster, harder, sixteenth hats, and the snare barely there.
    "techno": dict(bpm=132.0, kick=(0, 1, 2, 3), snare=(1, 3),
                   hat=tuple(i / 2 for i in range(8)), kind="rim",
                   sub=0.6, limit=True, snare_gain=0.35),
    "trance": dict(bpm=138.0, kick=(0, 1, 2, 3), snare=(1, 3),
                   hat=(0.5, 1.5, 2.5, 3.5), kind="bright", sub=0.5,
                   limit=True),
    # Half time: the snare waits until the third beat, and the space
    # between is the whole point of the genre.
    "dubstep": dict(bpm=140.0, kick=(0, 2.5), snare=(2,),
                    hat=(0.5, 1, 1.5, 2.5, 3, 3.5), kind="bright",
                    sub=0.8, limit=True),
    # Half time again, with hat rolls at four times the tempo and an 808
    # that slides under everything.
    "trap": dict(bpm=140.0, kick=(0, 0.75, 2.5), snare=(2,),
                 hat=(0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75,
                      2, 2.25, 2.5, 2.75, 3, 3.125, 3.25, 3.375,
                      3.5, 3.625, 3.75, 3.875),
                 kind="clap", sub=0.75, limit=True),
    # Twice the tempo of everything else, and a broken kick pattern.
    "dnb": dict(bpm=174.0, kick=(0, 2.5), snare=(1, 3),
                hat=(0.5, 1.5, 2.5, 3.5), kind="bright", sub=0.55,
                limit=True),
    # Swung, syncopated, and the kick off the grid.
    "garage": dict(bpm=132.0, kick=(0, 2.5), snare=(1, 3),
                   hat=(0.5, 1.5, 2.5, 3.5), kind="clap", sub=0.5,
                   limit=True, swing=0.28),
    "breaks": dict(bpm=130.0, kick=(0, 1.75, 2.5), snare=(1, 3),
                   hat=tuple(i / 2 for i in range(8)), kind="bright",
                   sub=0.4, limit=True),
    # Not dance music, as a control. No sub, no limiter, nothing buried.
    "rock": dict(bpm=120.0, kick=(0, 2), snare=(1, 3),
                 hat=tuple(i / 2 for i in range(8)), kind="bright",
                 sub=0.0, limit=False),
    "hiphop": dict(bpm=90.0, kick=(0, 1.5, 2.75), snare=(1, 3),
                   hat=tuple(i / 2 for i in range(8)), kind="clap",
                   sub=0.5, limit=True),
    # Swung ride, brushed snare off the beat, kick barely there.
    "jazz": dict(bpm=120.0, kick=(0,), snare=(1.5, 3.5),
                 hat=(0, 0.66, 1, 1.66, 2, 2.66, 3, 3.66), kind="rim",
                 sub=0.0, limit=False, kick_gain=0.5, swing=0.2),
}


def styled(style: str, seconds: float = 22.0
           ) -> Tuple[array, Dict[str, List[float]]]:
    """One of ``STYLES``, as stereo PCM, and where every hit really is."""
    if style not in STYLES:
        raise ValueError(f"no such style: {style}")
    spec = STYLES[style]
    bpm = float(spec["bpm"])
    beat = 60.0 / bpm
    swing = float(spec.get("swing", 0.0))
    frames = int(seconds * RATE)
    buffer = [0.0] * frames
    truth: Dict[str, List[float]] = {"Kick": [], "Snare": [], "Hats": []}

    def put(at: float, sample: Sequence[float], gain: float) -> None:
        start = int(at * RATE)
        for index, value in enumerate(sample):
            where = start + index
            if 0 <= where < frames:
                buffer[where] += value * gain

    def when(top: float, position: float) -> float:
        """Where in the bar a beat lands, swung if the style swings."""
        whole = math.floor(position)
        part = position - whole
        if swing and 0.4 < part < 0.6:
            part += swing * 0.5
        return top + (whole + part) * beat

    one_kick = kick()
    one_hat = hat()
    one_snare = snare(str(spec["kind"]))
    bassline = [sub(hz, beat * BEATS) for hz in (55.0, 65.4, 49.0, 58.3)]
    kick_gain = float(spec.get("kick_gain", 0.95))
    snare_gain = float(spec.get("snare_gain", 0.7))
    bars = max(1, int(seconds / (beat * BEATS)))
    for bar in range(bars):
        top = bar * beat * BEATS
        if spec.get("sub"):
            put(top, bassline[bar % len(bassline)], float(spec["sub"]))
        for position in spec["hat"]:
            at = when(top, float(position))
            put(at, one_hat, 0.45)
            truth["Hats"].append(at)
        for position in spec["kick"]:
            at = when(top, float(position))
            put(at, one_kick, kick_gain)
            truth["Kick"].append(at)
        for position in spec["snare"]:
            at = when(top, float(position))
            put(at, one_snare, snare_gain)
            truth["Snare"].append(at)

    peak = max(1e-9, max(abs(v) for v in buffer))
    pcm = array("h")
    for value in buffer:
        level = value / peak
        if spec.get("limit"):
            # What a master limiter does, which is most of what makes a
            # dance record hard to read.
            level = math.tanh(level * 3.4) / math.tanh(3.4)
        one = int(max(-1.0, min(1.0, level * 0.88)) * 32000)
        pcm.append(one)
        pcm.append(one)
    for name in truth:
        truth[name] = sorted(truth[name])
    return pcm, truth
