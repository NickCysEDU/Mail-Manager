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


def kick(length: float = 0.16) -> List[float]:
    """A pitch falling from 120 Hz to 45, which is what a kick is."""
    out = []
    for index in range(int(length * RATE)):
        moment = index / RATE
        hertz = 45.0 + 75.0 * _fall(moment, 34.0)
        out.append(math.sin(2 * math.pi * hertz * moment)
                   * _fall(moment, 26.0))
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


def track(kind: str = "bright", bpm: float = 120.0, seconds: float = 22.0,
          melody: bool = False) -> Tuple[array, Dict[str, List[float]]]:
    """The pattern, as stereo PCM, and where every hit really is."""
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
    for bar in range(int(seconds / (beat * BEATS))):
        top = bar * beat * BEATS
        for step in range(BEATS * 2):
            put(top + step * beat * 0.5, one_hat, 0.45)
            truth["Hats"].append(top + step * beat * 0.5)
        for step in (0, 2):
            put(top + step * beat, one_kick, 0.95)
            truth["Kick"].append(top + step * beat)
        for step in (1, 3):
            put(top + step * beat, one_snare, 0.85)
            truth["Snare"].append(top + step * beat)
        if melody:
            # On the off-beats, never on a snare.
            for sound, when in zip(notes, (0.5, 2.5, 3.5)):
                put(top + beat * when, sound, 0.30)

    peak = max(1e-9, max(abs(v) for v in buffer))
    pcm = array("h")
    for value in buffer:
        one = int(max(-1.0, min(1.0, value / peak * 0.88)) * 32000)
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
