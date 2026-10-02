"""Where the beats of a track fall.

A steady grid (one beat length and the time of one beat) or, where the
tempo moves, the list of beat times themselves. Beat numbers are
continuous: 12.5 is half way from beat 12 to beat 13.
"""

from __future__ import annotations

import bisect
import math
from typing import Optional, Sequence

#: Beats in a bar.
BAR = 4


class BeatClock:
    def __init__(self, beat: float = 0.0, phase: float = 0.0,
                 times: Optional[Sequence[float]] = None,
                 downbeat: Optional[float] = None) -> None:
        self.times = [float(t) for t in (times or ())]
        if len(self.times) < 2:
            self.times = []
        if self.times:
            first, last = self.times[0], self.times[-1]
            beat = (last - first) / (len(self.times) - 1)
            phase = first
        self.beat = float(beat) if beat and beat > 0.0 else 0.0
        self.phase = float(phase)
        #: The time of one first beat of a bar, if known.
        self.downbeat = downbeat

    def __bool__(self) -> bool:
        return self.beat > 0.0

    def __eq__(self, other) -> bool:
        return (isinstance(other, BeatClock) and self.times == other.times
                and abs(self.beat - other.beat) < 1e-9
                and abs(self.phase - other.phase) < 1e-9
                and self.downbeat == other.downbeat)

    __hash__ = None

    # -- time to beats and back -------------------------------------------
    def number(self, when: float) -> float:
        """The beat number at ``when``; beat 0 is the first beat."""
        if not self:
            return 0.0
        times = self.times
        if not times:
            return (when - self.phase) / self.beat
        if when <= times[0]:
            return (when - times[0]) / (times[1] - times[0])
        if when >= times[-1]:
            return (len(times) - 1) + (when - times[-1]) / (
                times[-1] - times[-2])
        index = bisect.bisect_right(times, when) - 1
        return index + (when - times[index]) / (times[index + 1]
                                                - times[index])

    def time(self, number: float) -> float:
        """The time of beat ``number``, which may be fractional."""
        if not self:
            return 0.0
        times = self.times
        if not times:
            return self.phase + number * self.beat
        if number <= 0.0:
            return times[0] + number * (times[1] - times[0])
        last = len(times) - 1
        if number >= last:
            return times[-1] + (number - last) * (times[-1] - times[-2])
        index = int(math.floor(number))
        return times[index] + (number - index) * (times[index + 1]
                                                  - times[index])

    def length(self, when: float) -> float:
        """How long the beat at ``when`` lasts, in seconds."""
        if not self:
            return 0.0
        if not self.times:
            return self.beat
        number = math.floor(self.number(when))
        return self.time(number + 1) - self.time(number)

    def tempo(self, when: float) -> float:
        """Beats a minute at ``when``."""
        span = self.length(when)
        return 60.0 / span if span > 0.0 else 0.0

    def nearest(self, when: float, division: int = 1) -> float:
        """The time of the nearest beat, or of the nearest ``division``th
        of one."""
        division = max(1, int(division))
        return self.time(round(self.number(when) * division) / division)

    # -- bars ---------------------------------------------------------------
    def in_bar(self, number: float) -> float:
        """Where beat ``number`` is in its bar, 0 to BAR: 0 is the first
        beat. Counted from the downbeat where it is known."""
        first = 0.0
        if self.downbeat is not None:
            first = round(self.number(self.downbeat))
        return (number - first) % BAR

    def is_downbeat(self, number: int) -> bool:
        return abs(self.in_bar(float(number))) < 1e-6
