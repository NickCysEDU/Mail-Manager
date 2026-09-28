"""Songs written to a known key, known chords and a known tuning.

drumkit writes drums to known times so the drum detector can be measured.
This writes harmony to known pitches so the harmony can be: a chord
sequence in a key, a bass under it, a lead over it, drums if wanted, and a
tuning off A = 440 if wanted - and the truth about every one of them,
because this file put them there. Nothing anybody owns is in it.

Written through a wavetable rather than a sine a sample, because a test
that spends ten seconds making its input is a test nobody runs.
"""

from __future__ import annotations

import math
from array import array
from typing import Dict, List, Optional, Sequence, Tuple

NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")
TONES = {"maj": (0, 4, 7), "min": (0, 3, 7)}

_TABLE = 4096


def _wavetable(partials: Sequence[Tuple[int, float]]) -> List[float]:
    table = [0.0] * _TABLE
    for harmonic, weight in partials:
        for index in range(_TABLE):
            table[index] += weight * math.sin(
                2.0 * math.pi * harmonic * index / _TABLE)
    top = max(abs(v) for v in table) or 1.0
    return [v / top for v in table]


#: A soft pad, a bass, and a brighter lead.
PAD = _wavetable(((1, 1.0), (2, 0.45), (3, 0.25), (4, 0.12)))
BASS = _wavetable(((1, 1.0), (2, 0.35)))
LEAD = _wavetable(((1, 1.0), (2, 0.6), (3, 0.4), (5, 0.2)))


def hertz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _add(buffer: List[float], rate: int, start: float, length: float,
         midi: float, table: List[float], loud: float,
         attack: float = 0.02, release: float = 0.08) -> None:
    first = int(start * rate)
    count = int(length * rate)
    step = hertz(midi) * _TABLE / rate
    phase = 0.0
    for index in range(count):
        at = first + index
        if at >= len(buffer):
            break
        t = index / rate
        env = min(1.0, t / attack, (length - t) / release)
        buffer[at] += loud * max(0.0, env) * table[int(phase) & (_TABLE - 1)]
        phase += step


def song(chords: Sequence[Tuple[int, str]], bpm: float = 120.0,
         beats_per_chord: int = 4, repeats: int = 2, rate: int = 8000,
         cents: float = 0.0, lead: Optional[Sequence[int]] = None,
         lead_every: float = 1.0) -> Tuple[array, int, Dict]:
    """A chord sequence, played ``repeats`` times: mono PCM, its rate, and
    the truth.

    ``chords`` is (root pitch class, "maj" or "min") per chord. ``lead`` is
    MIDI notes for a melody, one every ``lead_every`` beats, cycled. The
    whole song is ``cents`` off A = 440.
    """
    beat = 60.0 / bpm
    span = beat * beats_per_chord
    total = span * len(chords) * repeats
    buffer = [0.0] * int(total * rate + rate * 0.2)
    shift = cents / 100.0
    truth_chords = []
    for index in range(len(chords) * repeats):
        root, quality = chords[index % len(chords)]
        start = index * span
        truth_chords.append((start, start + span, root, quality))
        for step in TONES[quality]:
            # Voiced around middle C.
            midi = 60 + (root + step) % 12 + shift
            _add(buffer, rate, start, span, midi, PAD, 0.22)
        _add(buffer, rate, start, span, 36 + root + shift, BASS, 0.35)
    truth_lead = []
    if lead:
        every = beat * lead_every
        count = int(total / every)
        for index in range(count):
            midi = lead[index % len(lead)]
            start = index * every
            _add(buffer, rate, start, every * 0.9, midi + shift, LEAD, 0.25,
                 release=0.03)
            truth_lead.append((start, start + every * 0.9, midi))
    peak = max(abs(v) for v in buffer) or 1.0
    pcm = array("h", [int(v / peak * 26000) for v in buffer])
    return pcm, rate, {"chords": truth_chords, "lead": truth_lead,
                       "seconds": total}


def arpeggio(chords: Sequence[Tuple[int, str]], bpm: float = 120.0,
             beats_per_chord: int = 8, repeats: int = 2, rate: int = 8000,
             note_beats: float = 0.5, order: Sequence[int] = (0, 1, 2, 1),
             bass: float = 0.0) -> Tuple[array, int, Dict]:
    """Each chord played one note at a time, ``order`` being which of its
    three notes in turn, a note every ``note_beats`` - and a bass on its
    root only if ``bass`` says how loud. What a trance breakdown does."""
    beat = 60.0 / bpm
    span = beat * beats_per_chord
    total = span * len(chords) * repeats
    buffer = [0.0] * int(total * rate + rate * 0.2)
    truth_chords = []
    for index in range(len(chords) * repeats):
        root, quality = chords[index % len(chords)]
        start = index * span
        truth_chords.append((start, start + span, root, quality))
        tones = TONES[quality]
        step = 0
        while step * beat * note_beats < span - 1e-9:
            degree = tones[order[step % len(order)]]
            _add(buffer, rate, start + step * beat * note_beats,
                 beat * note_beats, 60 + (root + degree) % 12, PAD, 0.4,
                 release=0.03)
            step += 1
        if bass:
            _add(buffer, rate, start, span, 36 + root, BASS, bass)
    peak = max(abs(v) for v in buffer) or 1.0
    pcm = array("h", [int(v / peak * 26000) for v in buffer])
    return pcm, rate, {"chords": truth_chords, "seconds": total}


#: Where things fall in a bar of each kind of record, in beats: the same
#: patterns drumkit writes as audio. Bass is where the bass line moves.
PATTERNS = {
    "house": dict(bpm=124.0, kick=(0, 1, 2, 3), snare=(1, 3),
                  hat=(0.5, 1.5, 2.5, 3.5), bass=(0.5, 1.5, 2.5, 3.5)),
    "techno": dict(bpm=132.0, kick=(0, 1, 2, 3), snare=(1, 3),
                   hat=tuple(i / 4 for i in range(16)),
                   bass=tuple(i / 4 + 0.25 for i in range(0, 16, 2))),
    "trance": dict(bpm=138.0, kick=(0, 1, 2, 3), snare=(1, 3),
                   hat=(0.5, 1.5, 2.5, 3.5),
                   bass=tuple(i / 4 for i in range(16) if i % 4)),
    "hardstyle": dict(bpm=150.0, kick=(0, 1, 2, 3), snare=(1, 3),
                      hat=(0.5, 1.5, 2.5, 3.5), bass=(0.5, 1.5, 2.5, 3.5)),
    "dubstep": dict(bpm=140.0, kick=(0, 2.5), snare=(2,),
                    hat=(0.5, 1, 1.5, 2.5, 3, 3.5), bass=(0, 0.75, 1.5, 2.5)),
    "trap": dict(bpm=140.0, kick=(0, 0.75, 2.5), snare=(2,),
                 hat=(0, 0.25, 0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.25, 2.5,
                      2.75, 3, 3.125, 3.25, 3.375, 3.5, 3.625, 3.75, 3.875),
                 bass=(0, 0.75, 2.5)),
    "dnb": dict(bpm=174.0, kick=(0, 1.75, 2.5), snare=(1, 3),
                hat=tuple(i / 2 for i in range(8)), bass=(0, 1.75, 2.5)),
    "garage": dict(bpm=132.0, kick=(0, 1.75, 2.5), snare=(1, 3),
                   hat=(0.5, 1.5, 2.5, 3.5), bass=(0, 1.75, 2.5), swing=0.1),
    "hiphop": dict(bpm=90.0, kick=(0, 1.5, 2.75), snare=(1, 3),
                   hat=tuple(i / 2 for i in range(8)), bass=(0, 1.5, 2.75),
                   swing=0.08),
    "ambient": dict(bpm=80.0, kick=(), snare=(), hat=(), bass=(0,)),
}

#: A dance record's shape: (kind, bars, loudness, drums in it).
DANCE = (("intro", 8, 0.35, False), ("build", 8, 0.5, False),
         ("drop", 16, 0.95, True), ("break", 8, 0.3, False),
         ("build", 8, 0.55, False), ("drop", 16, 0.95, True),
         ("outro", 8, 0.35, True))


def chart(style: str, shape: Sequence = DANCE, offset: float = 0.0,
          rate: float = 8.0) -> Tuple[Dict[str, List[float]], dict, float,
                                        List[Tuple[str, float, float]]]:
    """What the analysis would have found in a record of ``style`` built
    in ``shape``: the chart, the loudness contour, the beat period and
    the truth about where each part is."""
    spec = PATTERNS[style]
    beat = 60.0 / spec["bpm"]
    swing = float(spec.get("swing", 0.0))
    out: Dict[str, List[float]] = {"Kick": [], "Snare": [], "Hats": [],
                                   "Bass": []}
    # Silence before the record starts, in the loudness as in the hits.
    loud: List[float] = [0.0] * int(offset * rate)
    truth = []
    at = offset
    for kind, bars, level, drums in shape:
        start = at
        for bar in range(bars):
            top = at + bar * beat * 4
            share = bar / max(1, bars - 1)

            def place(position: float) -> float:
                part = position - math.floor(position)
                if swing and 0.4 < part < 0.6:
                    position += swing
                return top + position * beat

            if drums:
                for position in spec["kick"]:
                    out["Kick"].append(place(position))
                for position in spec["snare"]:
                    out["Snare"].append(place(position))
                for position in spec["hat"]:
                    out["Hats"].append(place(position))
                for position in spec["bass"]:
                    out["Bass"].append(place(position))
            elif kind == "build":
                # The roll: a snare a beat, then every half, then every
                # quarter, as the drop comes.
                every = 1.0 if share < 0.5 else (0.5 if share < 0.85 else 0.25)
                position = 0.0
                while position < 4.0:
                    out["Snare"].append(place(position))
                    position += every
            elif kind in ("intro", "break"):
                for position in (0.5, 1.5, 2.5, 3.5):
                    out["Hats"].append(place(position))
        span = bars * beat * 4
        for index in range(int(span * rate)):
            share = index / max(1, int(span * rate) - 1)
            here = level + (0.35 * share if kind == "build" else 0.0)
            loud.append(max(0.0, min(1.0, here)))
        truth.append((kind, start, start + span))
        at += span
    for name in out:
        out[name].sort()
    contour = {"loud": loud, "lean": [0.0] * len(loud), "rate": rate}
    return out, contour, beat, truth
