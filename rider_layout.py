"""What Music rider lays on its road, and where, for a given record.

The road used to be laid from one rule for everything: a figure every two
beats or so, the same eight shapes in turn - wall, block, wall, run, wall,
block, wall, wall - and a quarter of them obstacles. Every record rode the
same, only faster or slower. This decides instead, from how the record
moves and how it is put together (see trackstyle):

**Which figures.** A vocabulary of them, each a way of moving to music:

    wall      two lanes shut, one open                 the kick you dodge
    gate      a wall whose open lane walks across      a four-to-the-floor
              the road, one gate a beat or two         weave
    chicane   two walls half a beat apart, open        a broken beat's
              lanes side by side                       double step
    block     one block in one lane                    a hit
    run       three prizes stepping across             a fill
    stairs    prizes climbing the lanes the way the    a melody going up
              melody goes, a sixteenth apart           (or down)
    stream    a row of coins in one lane               a hat roll
    pair      two prizes side by side                  a chord stab
    melody    one prize in the lane of the note        a tune

**How many of each**, from the style: four to the floor leans on gates and
walls, broken beats on chicanes and runs, half time on walls and streams,
swing on pairs, a melody on stairs. And from the section: a drop is dense
and dangerous, a build rolls towards it, a break is a melody to collect and
nothing to dodge.

**In what order.** Each section draws a short palette of figures, weighted
as above, and walks it in a pattern - A A B A, A B A C - so a part of a
track has a rhythm of its own rather than a figure picked afresh every
time. A section that comes back - the second chorus - takes the palette
the first one drew, mirrored, so it feels like the part it is and not
like the same road twice.

Everything is drawn from the track's own seed, so a record lays out the
same way every time it is played and every record lays out its own way.
"""

from __future__ import annotations

import random
import zlib
from typing import Dict, List, Optional, Tuple

#: The figures that are obstacles, and the ones that are prizes.
DANGER = ("wall", "gate", "chicane", "block")
REWARD = ("block", "run", "stairs", "stream", "pair", "melody")

#: How much each style likes each figure. Read as "on music that moves
#: like this, this is how you ride it".
AFFINITY: Dict[str, Dict[str, float]] = {
    "steady": {"gate": 2.0, "wall": 1.5, "block": 1.0, "stairs": 1.0,
               "pair": 0.6, "run": 0.6},
    "hard": {"gate": 3.0, "chicane": 1.2, "wall": 1.2, "block": 0.6},
    "broken": {"chicane": 2.5, "run": 2.0, "block": 1.5, "stream": 0.6,
               "wall": 0.8},
    "heavy": {"wall": 2.5, "block": 1.2, "stream": 1.5, "pair": 1.0},
    "swung": {"pair": 2.0, "run": 1.5, "block": 1.0, "chicane": 0.8},
    "rolls": {"stream": 3.0},
    "melodic": {"stairs": 2.5, "melody": 2.0, "pair": 1.0},
    "calm": {"melody": 3.0, "stairs": 1.0, "pair": 0.6},
}

#: Everything is possible everywhere, a little: the floor under the
#: weights, so a style with nothing to say still gets a road.
BASE: Dict[str, float] = {"wall": 0.6, "gate": 0.4, "chicane": 0.3,
                          "block": 0.8, "run": 0.6, "stairs": 0.5,
                          "stream": 0.3, "pair": 0.4, "melody": 0.4}

#: And how a kind of section changes them.
SECTION: Dict[str, Dict[str, float]] = {
    "drop": {"gate": 1.5, "chicane": 1.5, "wall": 1.4, "melody": 0.6},
    "groove": {},
    "build": {"stream": 3.0, "stairs": 1.5, "gate": 0.5, "wall": 0.4,
              "chicane": 0.3},
    "break": {"melody": 2.0, "stairs": 1.5, "pair": 1.2, "stream": 0.5},
    "intro": {"melody": 1.5, "stairs": 1.2, "stream": 0.5},
    "outro": {"melody": 1.5, "stairs": 1.2},
}

#: The share of figures that are obstacles, by kind of section, in the
#: road's two games that have obstacles to dodge.
DANGER_SHARE: Dict[str, Tuple[float, float]] = {
    #          Mono  Ninja
    "drop": (0.40, 0.65),
    "groove": (0.30, 0.55),
    "build": (0.15, 0.35),
    "break": (0.00, 0.15),
    "intro": (0.08, 0.25),
    "outro": (0.12, 0.30),
}

#: How many beats between figures, by kind of section: tight in a drop,
#: loose in a break, and a build tightening from the first number to the
#: second as the drop comes.
SPACING: Dict[str, Tuple[float, float]] = {
    "drop": (1.5, 1.5), "groove": (2.0, 2.0), "build": (3.0, 1.0),
    "break": (3.0, 3.0), "intro": (3.0, 3.0), "outro": (2.5, 2.5),
}

#: The patterns a section walks its palette in: indexes into it.
WALKS = ((0, 0, 1, 0), (0, 1, 0, 2), (0, 1, 0, 1), (0, 0, 1, 2),
         (0, 1, 2, 1))


def weights(style, kind: str, danger: bool) -> Dict[str, float]:
    """How likely each figure is, for obstacles or for prizes, in a
    section of ``kind`` of a track that moves like ``style``."""
    names = DANGER if danger else REWARD
    out = {}
    for name in names:
        weight = BASE.get(name, 0.0)
        for trait, likes in AFFINITY.items():
            weight += float(getattr(style, trait, 0.0)) * likes.get(name, 0.0)
        weight *= SECTION.get(kind, {}).get(name, 1.0)
        out[name] = max(0.0, weight)
    return out


def _draw(rng: random.Random, table: Dict[str, float], count: int) -> List[str]:
    """``count`` different names from ``table``, by weight."""
    left = dict(table)
    out = []
    for _ in range(min(count, len(left))):
        total = sum(left.values())
        if total <= 0.0:
            break
        pick = rng.random() * total
        for name, weight in left.items():
            pick -= weight
            if pick <= 0.0:
                out.append(name)
                del left[name]
                break
    return out or [next(iter(table))]


class Plan:
    """The palettes of a track's sections, drawn once and kept.

    Asked for a figure by section and by the slot's place in it; the
    answer is the same every time for the same track.
    """

    def __init__(self, style, mode: str = "Mono") -> None:
        self.style = style
        self.mode = mode
        self._palettes: Dict[tuple, tuple] = {}
        self._seen: Dict[tuple, int] = {}
        self._by_section: Dict[int, tuple] = {}

    def _signature(self, section) -> tuple:
        # A section that comes back is the same kind at about the same
        # level: the second chorus, the second breakdown.
        return (section.kind, round(section.level * 4) / 4)

    def palette(self, section) -> tuple:
        """(obstacle figures, prize figures, walk, mirrored) for a section."""
        key = id(section)
        found = self._by_section.get(key)
        if found is not None:
            return found
        signature = self._signature(section)
        occurrence = self._seen.get(signature, 0)
        self._seen[signature] = occurrence + 1
        base = self._palettes.get(signature)
        if base is None:
            # A checksum rather than hash(): Python salts the hash of a
            # string per process, which would give a record a new road
            # every time the app started.
            seed = (getattr(self.style, "seed", 0)
                    ^ zlib.crc32(repr(signature).encode()))
            rng = random.Random(seed)
            danger = tuple(_draw(rng, weights(self.style, section.kind, True),
                                 2))
            reward = tuple(_draw(rng, weights(self.style, section.kind,
                                              False), 3))
            walk = WALKS[rng.randrange(len(WALKS))]
            base = (danger, reward, walk)
            self._palettes[signature] = base
        found = base + (occurrence % 2 == 1,)
        self._by_section[key] = found
        return found

    def figure(self, section, slot: int, danger: bool) -> Tuple[str, bool]:
        """Which figure the ``slot``th figure of ``section`` is, and
        whether its lanes are mirrored."""
        obstacles, prizes, walk, mirrored = self.palette(section)
        pool = obstacles if danger else prizes
        return pool[walk[slot % len(walk)] % len(pool)], mirrored


def danger_share(kind: str, mode: str, style=None) -> float:
    """The share of a section's figures that are obstacles."""
    mono, ninja = DANGER_SHARE.get(kind, (0.3, 0.55))
    share = ninja if mode == "Ninja" else mono
    if style is not None and kind == "drop":
        share += 0.05 * max(float(getattr(style, "heavy", 0.0)),
                            float(getattr(style, "hard", 0.0)),
                            float(getattr(style, "broken", 0.0)))
    if style is not None:
        share -= 0.1 * float(getattr(style, "calm", 0.0))
    return max(0.0, min(0.8, share))


#: How much the loudness where a figure lands stretches or tightens its
#: section's spacing: at silence, and at the loudest the track gets.
#:
#: Audiosurf's traffic follows the music's intensity, and the kind of
#: section says what the figures are, not only how many. A record's drums
#: arrive a few seconds after its picture, and until they do every loud
#: part reads as a break - which spaced a chorus exactly like the quiet
#: intro before it, three beats apart both.
LOUDNESS = (1.2, 0.8)


def spacing(kind: str, through: float, style=None,
            loud: Optional[float] = None) -> float:
    """Beats between figures ``through`` (0 to 1) a section of ``kind``,
    where the track is ``loud`` (0 to 1) if that is known."""
    first, last = SPACING.get(kind, (2.0, 2.0))
    beats = first + (last - first) * max(0.0, min(1.0, through))
    if style is not None:
        if kind == "drop":
            quick = max(float(getattr(style, "broken", 0.0)),
                        float(getattr(style, "hard", 0.0)))
            beats *= 1.0 - 0.25 * quick
        beats *= 1.0 + 0.3 * float(getattr(style, "calm", 0.0))
    if loud is not None:
        loud = max(0.0, min(1.0, float(loud)))
        beats *= LOUDNESS[0] + (LOUDNESS[1] - LOUDNESS[0]) * loud
    return max(1.0, beats)


def lead_at(harmony: Optional[dict], when: float) -> Optional[float]:
    """The melody's note at ``when``, or None."""
    if not harmony:
        return None
    lead = harmony.get("lead") or ()
    rate = float(harmony.get("rate") or 0.0)
    if not lead or rate <= 0.0:
        return None
    index = int(round((when - float(harmony.get("lead_from", 0.0))) * rate))
    if 0 <= index < len(lead):
        return lead[index]
    return None


def lead_lane(harmony: Optional[dict], when: float, lanes: int,
              low: float, high: float) -> Optional[int]:
    """The lane a note of the melody belongs in: low notes on the left,
    high on the right, across the range the melody spans here."""
    note = lead_at(harmony, when)
    if note is None or high <= low:
        return None
    share = (note - low) / (high - low)
    return max(0, min(lanes - 1, int(share * lanes)))


def lead_range(harmony: Optional[dict], start: float, end: float
               ) -> Tuple[float, float]:
    """The melody's usual range between two moments: the fifth and the
    ninety-fifth percentiles of its notes, so one stray note does not
    squeeze the rest into one lane."""
    if not harmony:
        return 0.0, 0.0
    lead = harmony.get("lead") or ()
    rate = float(harmony.get("rate") or 0.0)
    offset = float(harmony.get("lead_from", 0.0))
    if not lead or rate <= 0.0:
        return 0.0, 0.0
    first = max(0, int((start - offset) * rate))
    last = min(len(lead), int((end - offset) * rate) + 1)
    notes = sorted(value for value in lead[first:last] if value is not None)
    if len(notes) < 4:
        return 0.0, 0.0
    return notes[len(notes) // 20], notes[min(len(notes) - 1,
                                              len(notes) * 19 // 20)]


def rising(harmony: Optional[dict], when: float, ahead: float = 0.5
           ) -> Optional[bool]:
    """Whether the melody is going up from ``when``, or None."""
    here = lead_at(harmony, when)
    there = lead_at(harmony, when + ahead)
    if here is None or there is None or abs(there - here) < 0.5:
        return None
    return there > here


def lead_onsets(harmony: Optional[dict], start: float, end: float
                ) -> List[float]:
    """Where a new note of the melody starts, between two moments."""
    if not harmony:
        return []
    lead = harmony.get("lead") or ()
    rate = float(harmony.get("rate") or 0.0)
    offset = float(harmony.get("lead_from", 0.0))
    if not lead or rate <= 0.0:
        return []
    out = []
    first = max(1, int((start - offset) * rate))
    last = min(len(lead), int((end - offset) * rate) + 1)
    for index in range(first, last):
        here, before = lead[index], lead[index - 1]
        if here is not None and (before is None or abs(here - before) >= 0.9):
            out.append(offset + index / rate)
    return out
