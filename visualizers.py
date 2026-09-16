"""Scenes for the audio spectrum. One set of numbers, several ways to read it.

Every scene is handed the same thing each frame: the equaliser bands, four
smoothed aggregates taken from them, a drifting phase, and whether strobe is
on. None of them computes a spectrum; the analysis happened once, before
playback started, and the paint loop only interpolates.

A scene is a function of state, with no memory of its own beyond what the
caller keeps. That keeps switching themes instant and keeps every one of them
cheap enough to run at thirty frames a second beside a mail sorter.
"""

from __future__ import annotations

import math
from typing import List

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)


class Scene:
    """One way of drawing the music."""

    name = "scene"
    blurb = "a scene"

    def paint(self, painter: QPainter, rect, state) -> None:
        raise NotImplementedError

    # -- what every scene shares ------------------------------------------
    @staticmethod
    def flash(state) -> float:
        """How hard the strobe is hitting, 0 to 1, or 0 when it is off.

        Scenes ask for this and do something of their own with it. A white
        rectangle over the top looked the same in all five and hid whatever
        was underneath, which is the opposite of what a strobe should do.
        """
        return state.hit if state.strobe else 0.0

    @staticmethod
    def geometry(rect, count: int, width_fraction: float = 0.9):
        """(left, bar width, gap) for a row of ``count`` bars."""
        span = rect.width() * width_fraction
        left = rect.left() + (rect.width() - span) / 2.0
        gap = max(1.0, span / max(1, count) * 0.18)
        bar = max(1.0, (span - gap * (count - 1)) / max(1, count))
        return left, bar, gap


class Vaporwave(Scene):
    """A grid, a sun, a skyline. The one everybody pictures."""

    name = "Vaporwave city"
    blurb = "a skyline that is the equaliser, with a grid and a sun"

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        horizon = height * 0.54
        hue = state.hue

        sky = QLinearGradient(0.0, 0.0, 0.0, horizon)
        sky.setColorAt(0.0, QColor(4, 2, 14))
        sky.setColorAt(0.55, QColor.fromHsvF((hue + 0.72) % 1.0, 0.92, 0.22))
        sky.setColorAt(1.0, QColor.fromHsvF((hue + 0.78) % 1.0, 0.80, 0.46))
        painter.fillRect(QRectF(0, 0, width, horizon), sky)

        # The strobe belongs to the sun here: a kick makes it flare and
        # widen rather than washing the whole frame white.
        flash = self.flash(state)
        radius = horizon * (0.46 + state.bass * 0.26 + flash * 0.30)
        sun = QRadialGradient(QPointF(width / 2.0, horizon), radius)
        sun.setColorAt(0.0, QColor.fromHsvF(hue, 0.50 - flash * 0.4, 1.0,
                                            0.60 + state.bass * 0.3 + flash * 0.35))
        sun.setColorAt(0.75, QColor.fromHsvF((hue + 0.05) % 1.0, 0.9, 1.0,
                                             0.18 + flash * 0.25))
        sun.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, width, horizon), sun)
        # Scanlines across the sun, which is the look this is copying.
        painter.setPen(QPen(QColor(10, 6, 24, 150), max(1.0, horizon * 0.018)))
        step = max(4.0, horizon * 0.055)
        y = horizon - radius * 0.62
        while y < horizon:
            painter.drawLine(QPointF(width / 2.0 - radius, y),
                             QPointF(width / 2.0 + radius, y))
            y += step

        # No bar graph here on purpose: it stood in front of the city and
        # hid the thing that is already showing the same numbers. The
        # towers are the equaliser - one per band, rising with it.
        self._far_skyline(painter, width, horizon, state)
        self._skyline(painter, width, horizon, state)
        self._floor(painter, width, height, horizon, state)
        self._reflection(painter, width, height, horizon, state)
        self._ribbons(painter, width, horizon, state)
        self._stars(painter, width, horizon, state)

    def _far_skyline(self, painter, width, horizon, state) -> None:
        """A dimmer row behind, offset, so the city has depth."""
        levels = state.levels
        count = len(levels)
        if count < 2:
            return
        block = width / (count - 1)
        painter.setPen(Qt.PenStyle.NoPen)
        far = QPainterPath()
        for index in range(count - 1):
            value = (levels[index] + levels[index + 1]) * 0.5
            tall = horizon * (0.06 + value * 0.30)
            far.addRect(QRectF(index * block + block * 0.3, horizon - tall,
                               block * 0.84, tall))
        painter.fillPath(far, QColor.fromHsvF((state.hue + 0.66) % 1.0,
                                              0.85, 0.10, 1.0))

    def _reflection(self, painter, width, height, horizon, state) -> None:
        """The city again, upside down in the floor, fading out."""
        levels = state.levels
        count = len(levels)
        if not count:
            return
        block = width / count
        path = QPainterPath()
        for index, value in enumerate(levels):
            tall = horizon * (0.10 + value * 0.42) * 0.5
            path.addRect(QRectF(index * block, horizon, block * 0.92, tall))
        colour = QColor.fromHsvF((state.hue + 0.6) % 1.0, 0.7, 0.9, 0.16)
        painter.fillPath(path, colour)

    def _skyline(self, painter, width, horizon, state) -> None:
        """Towers, each one a band. A city that is also the equaliser."""
        levels = state.levels
        count = len(levels)
        if not count:
            return
        block = width / count
        windows = QPainterPath()
        roofs = QPainterPath()
        for index, value in enumerate(levels):
            tall = horizon * (0.10 + value * 0.42)
            x = index * block
            shade = ((index / count) * 0.2 + state.hue + 0.6) % 1.0
            # Darker bodies than before, so the lit windows and the roof
            # line carry the shape rather than the block itself.
            painter.fillRect(QRectF(x, horizon - tall, block * 0.92, tall),
                             QColor.fromHsvF(shade, 0.88, 0.13, 1.0))
            # A lit roof edge. This is what makes the city read against a
            # bright sun instead of dissolving into it. Collected and
            # filled once, like the windows: one fillRect per tower was
            # twenty-seven brush changes a frame.
            roofs.addRect(QRectF(x, horizon - tall, block * 0.92,
                                 max(1.0, horizon * 0.006)))
            # Lit windows. Collected into one path and filled once at the
            # end: several hundred drawRect calls with a brush change each
            # was most of what this scene cost at 1080p.
            if value > 0.25:
                spacing = max(9.0, horizon * 0.022)
                rows = int(tall / spacing)
                for row in range(rows):
                    if (index + row) % 3 == 0:
                        windows.addRect(QRectF(
                            x + block * 0.22,
                            horizon - tall + row * spacing + 3,
                            block * 0.2, max(2.0, spacing * 0.3)))
        painter.setPen(Qt.PenStyle.NoPen)
        # Windows brighten together on a hit, like a block losing its blinds.
        painter.fillPath(roofs, QColor.fromHsvF(
            (state.hue + 0.68) % 1.0, 0.45, 1.0, 0.80))
        painter.fillPath(windows, QColor.fromHsvF(
            (state.hue + 0.6) % 1.0, 0.30 - self.flash(state) * 0.25, 1.0,
            0.42 + self.flash(state) * 0.45))

    def _floor(self, painter, width, height, horizon, state) -> None:
        depth = height - horizon
        colour = QColor.fromHsvF(state.hue, 0.72, 1.0, 0.28 + state.bass * 0.5)
        painter.setPen(QPen(colour, 1.0))
        for step in range(1, 15):
            t = ((step + state.scroll) / 15.0) ** 2.4
            y = horizon + t * depth
            painter.drawLine(QPointF(0, y), QPointF(width, y))
        middle = width / 2.0
        for index in range(-8, 9):
            painter.drawLine(QPointF(middle + index * 5.0, horizon),
                             QPointF(middle + index * width * 0.15, height))

    def _ribbons(self, painter, width, horizon, state) -> None:
        if state.synth < 0.02:
            return
        for ribbon in range(3):
            amplitude = horizon * (0.05 + state.synth * 0.15) * (1.0 - ribbon * 0.22)
            middle = horizon * (0.22 + ribbon * 0.11)
            shade = (state.hue + 0.52 + ribbon * 0.06) % 1.0
            painter.setPen(QPen(QColor.fromHsvF(
                shade, 0.55, 1.0,
                (0.20 + state.synth * 0.45) * (1.0 - ribbon * 0.25)),
                2.0 - ribbon * 0.4))
            path = QPainterPath()
            for step in range(21):
                t = step / 20
                y = middle + math.sin(t * 6.0 + state.phase * 9.0
                                      + ribbon * 1.7) * amplitude
                point = QPointF(t * width, y)
                path.moveTo(point) if step == 0 else path.lineTo(point)
            painter.drawPath(path)

    def _stars(self, painter, width, horizon, state) -> None:
        """A few fixed stars high in the sky, brightening with the treble.

        Cheap detail that gives the top of the frame something to do now
        the orb has gone, and it does not sit in front of the city.
        """
        painter.setPen(Qt.PenStyle.NoPen)
        shade = (state.hue + 0.5) % 1.0
        # One path, one fill. Fourteen brush changes a frame is not much on
        # its own, but this scene was already the most expensive of the set.
        sky = QPainterPath()
        for index in range(14):
            # Deterministic placement, so they do not crawl about.
            x = ((index * 97) % 100) / 100.0 * width
            y = ((index * 37) % 100) / 100.0 * horizon * 0.52
            twinkle = 0.35 + 0.65 * abs(math.sin(state.phase * 1.6 + index))
            size = 0.8 + state.high * 1.8 * twinkle
            sky.addEllipse(QPointF(x, y), size, size)
        painter.fillPath(sky, QColor.fromHsvF(shade, 0.10, 1.0,
                                              0.30 + state.high * 0.45))


class Tunnel(Scene):
    """Rings receding down a corridor, each one a moment of the music."""

    name = "Neon tunnel"
    blurb = "rings falling away, wide when the bass hits"

    RINGS = 14

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        centre = QPointF(width / 2.0, height * 0.5)
        painter.fillRect(rect, QColor(6, 4, 14))

        # A hit shoves every ring a step down the corridor at once.
        rush = self.flash(state) * 0.45
        for index in range(self.RINGS, 0, -1):
            t = ((index + state.scroll + rush) % self.RINGS) / self.RINGS
            # Perspective: near rings are large and bright, far ones small.
            scale = t ** 1.8
            # Bass swells the ring rather than squashing it. The squash made
            # every ring an ellipse, which read as a mistake next to the
            # circular scenes rather than as a reaction to the music.
            swell = 1.0 + state.bass * 0.14
            radius = (14.0 + scale * max(width, height) * 0.62) * swell
            energy = state.levels[int(t * (len(state.levels) - 1))] if state.levels else 0.0
            shade = (state.hue + t * 0.5) % 1.0
            alpha = (1.0 - t) * (0.35 + energy * 0.6)
            painter.setPen(QPen(QColor.fromHsvF(shade, 0.7, 1.0, alpha),
                                1.0 + energy * 4.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, radius, radius)

        self._spokes(painter, centre, min(width, height) * 0.5, state)
        self._sparks(painter, state)

    def _spokes(self, painter, centre, reach, state) -> None:
        """The bands as rays out of the middle, not a row along the bottom.

        A bar graph at the foot of this scene fought with the perspective;
        spokes belong to it, and they read as the same numbers.
        """
        levels = state.levels
        count = len(levels)
        if not count:
            return
        flash = self.flash(state)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, value in enumerate(levels):
            angle = (index / count) * math.tau + state.phase * 0.6
            inner = reach * 0.20
            outer = inner + value * reach * (0.74 + flash * 0.2)
            shade = ((index / count) * 0.5 + state.hue + 0.3) % 1.0
            painter.setPen(QPen(
                QColor.fromHsvF(shade, 0.62 - flash * 0.4, 1.0,
                                0.30 + value * 0.6 + flash * 0.3),
                max(1.5, reach * 0.016 * (1.0 + value))))
            painter.drawLine(
                QPointF(centre.x() + math.cos(angle) * inner,
                        centre.y() + math.sin(angle) * inner),
                QPointF(centre.x() + math.cos(angle) * outer,
                        centre.y() + math.sin(angle) * outer))

    def _sparks(self, painter, state) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for spark in state.sparks:
            if spark[4] <= 0.0:
                continue
            painter.setBrush(QColor.fromHsvF((state.hue + 0.2) % 1.0, 0.15, 1.0,
                                             spark[4] * 0.9))
            painter.drawEllipse(QPointF(spark[0], spark[1]),
                                1.2 + spark[4] * 2.0, 1.2 + spark[4] * 2.0)


class Oscilloscope(Scene):
    """A real scope: the waveform itself, on a phosphor that takes its time.

    What was here before derived a shape from band energies, which is a
    picture of a spectrum pretending to be a waveform. This draws the
    signal - the same samples that are in the file, triggered on a rising
    zero crossing so the trace stands still instead of crawling.

    The persistence is the point. Old traces are kept and drawn fainter,
    the way a CRT's phosphor keeps glowing after the beam has gone, and how
    long they last is the decay control. Short reads like a modern digital
    scope; long smears several cycles together and shows how a sound moves.
    """

    name = "Oscilloscope"
    blurb = "the waveform swept round a circle, on a phosphor you can set"

    #: Traces kept at the longest decay. At sixty a second this is about a
    #: second and a half of history, which is longer than anybody sets it.
    MAX_HISTORY = 90
    #: Seconds of persistence at each end of the slider.
    MIN_DECAY = 0.03
    MAX_DECAY = 1.50

    def __init__(self) -> None:
        self._decay = 0.28

    # -- the control ------------------------------------------------------
    @property
    def decay(self) -> float:
        return self._decay

    def set_decay(self, seconds: float) -> None:
        self._decay = max(self.MIN_DECAY, min(self.MAX_DECAY, float(seconds)))

    # -- drawing ----------------------------------------------------------
    def paint(self, painter, rect, state) -> None:
        painter.fillRect(rect, QColor(2, 8, 4))
        flash = self.flash(state)
        self._grid(painter, rect, flash)

        trace = getattr(state, "trace", None)
        if trace is None:
            trace = self._from_levels(state)
        if trace is None:
            return

        # How many frames are worth keeping for the decay that is set.
        keep = max(1, min(self.MAX_HISTORY, int(self._decay * 60.0)))
        kept = getattr(state, "trace_history", None) or [list(trace)]
        kept = kept[-keep:]

        painter.setBrush(Qt.BrushStyle.NoBrush)
        total = len(kept)
        for age, old in enumerate(kept):
            # Newest last, so it is drawn over the faded ones.
            fresh = (age + 1) / total
            # Squared, because a phosphor does not fade in a straight line.
            alpha = fresh ** 2.2
            if alpha < 0.02:
                continue
            width = 1.0 + fresh * (1.8 + flash * 2.4)
            green = QColor.fromHsvF(0.33 - flash * 0.08,
                                    0.85 - flash * 0.5,
                                    1.0,
                                    min(1.0, alpha * (0.85 + flash * 0.4)))
            painter.setPen(QPen(green, width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(self._path(rect, old, state, flash))

    def _path(self, rect, trace, state, flash):
        """One sweep, swept around a circle rather than across.

        The beam starts at twelve o'clock and goes round once; how far
        the signal is from zero is how far the trace is from the ring.
        A steady tone draws a closed flower, and the trace joins up with
        itself because the capture is triggered on a zero crossing.
        """
        path = QPainterPath()
        count = len(trace)
        centre = rect.center()
        base = min(rect.width(), rect.height()) * 0.30
        swing = min(rect.width(), rect.height()) * (0.17 + flash * 0.07)
        first = None
        for index, value in enumerate(trace):
            angle = (index / count) * math.tau - math.pi / 2.0
            reach = base + value * swing
            point = QPointF(centre.x() + math.cos(angle) * reach,
                            centre.y() + math.sin(angle) * reach)
            if index:
                path.lineTo(point)
            else:
                path.moveTo(point)
                first = point
        if first is not None:
            path.lineTo(first)        # close the sweep
        return path

    def _from_levels(self, state):
        """A fallback shape when no waveform was captured.

        Older analyses have no traces; rather than draw nothing, the bands
        are folded into something that at least moves with the music.
        """
        levels = state.levels
        if not levels:
            return None
        count = len(levels)
        out = []
        for index in range(128):
            share = index / 127.0
            band = levels[min(count - 1, int(share * count))]
            out.append(math.sin(share * math.tau * 6.0 + state.phase * 3.0)
                       * band)
        return out

    def _grid(self, painter, rect, flash) -> None:
        """A polar graticule: rings for amplitude, spokes for phase."""
        centre = rect.center()
        reach = min(rect.width(), rect.height()) * 0.47
        painter.setBrush(Qt.BrushStyle.NoBrush)
        faint = QColor.fromHsvF(0.33, 0.6, 1.0, 0.09 + flash * 0.16)
        painter.setPen(QPen(faint, 1.0))
        for step in range(1, 5):
            radius = reach * step / 5.0
            painter.drawEllipse(centre, radius, radius)
        for step in range(12):
            angle = step * math.tau / 12.0
            painter.drawLine(
                QPointF(centre.x() + math.cos(angle) * reach * 0.12,
                        centre.y() + math.sin(angle) * reach * 0.12),
                QPointF(centre.x() + math.cos(angle) * reach,
                        centre.y() + math.sin(angle) * reach))
        # The zero ring, brighter: the trace sits on it when there is
        # silence, which is the line a flat scope draws.
        zero = QColor.fromHsvF(0.33, 0.5, 1.0, 0.26 + flash * 0.35)
        painter.setPen(QPen(zero, 1.3))
        base = min(rect.width(), rect.height()) * 0.30
        painter.drawEllipse(centre, base, base)


class Bars(Scene):
    """Just the equaliser, drawn properly, with the frequencies written on."""

    name = "Equaliser"
    blurb = "the bands and nothing else, with their frequencies"

    #: Stacked blocks, as on a hardware meter.
    SEGMENTS = 22

    def _segments(self, painter, rect, state, baseline, height, flash) -> None:
        """Discrete blocks rather than a smooth bar.

        This is the plain equaliser, so it looks like the thing itself: a
        column of lit segments with a gap between each, amber near the top
        and red at the very top, which is how a meter warns you.
        """
        levels = state.levels
        count = len(levels)
        if not count:
            return
        left, bar, gap = self.geometry(rect, count)
        block = height / self.SEGMENTS
        for index, value in enumerate(levels):
            x = left + index * (bar + gap)
            lit = int(value * self.SEGMENTS)
            for step in range(self.SEGMENTS):
                top = baseline - (step + 1) * block + block * 0.18
                share = step / self.SEGMENTS
                if step < lit:
                    hue = 0.33 - share * 0.33          # green up into red
                    colour = QColor.fromHsvF(max(0.0, hue), 0.85, 1.0,
                                             0.95 if share < 0.9 else 1.0)
                    if flash > 0.02 and share > 0.72:
                        colour = QColor.fromHsvF(0.12, 0.5 - flash * 0.4, 1.0,
                                                 0.9 + flash * 0.1)
                else:
                    colour = QColor(255, 255, 255, 16)
                painter.fillRect(QRectF(x, top, bar, block * 0.64), colour)
            cap = int(state.peaks[index] * self.SEGMENTS)
            if 0 < cap <= self.SEGMENTS:
                top = baseline - cap * block + block * 0.18
                painter.fillRect(QRectF(x, top, bar, block * 0.64),
                                 QColor(255, 255, 255, 190))

    #: Where the level marks go, in decibels below the track's own peak.
    DB_MARKS = (0, -3, -6, -12, -24, -40)

    @staticmethod
    def _height_for(db: float, calibration: dict) -> float:
        """Where a given level sits, 0 at the foot and 1 at the top.

        Undoes exactly what the analysis did. The bars are stretched to
        fill the display, which makes a quiet recording watchable but
        leaves a bar's height meaning nothing on its own; with the numbers
        that did the stretching the scale can be drawn truthfully.

        Relative to the loudest the track gets rather than to full scale,
        because the analysis is not calibrated against an absolute
        reference and a scale claiming otherwise would be made up.
        """
        reach = float(calibration.get("reach", 0.0))
        gamma = float(calibration.get("gamma", 0.72))
        span = float(calibration.get("range_db", 55.0))
        if reach <= 0.0 or span <= 0.0:
            return -1.0
        share = (reach + db / span) / reach
        if share <= 0.0:
            return 0.0
        return min(1.0, share) ** gamma

    def _scale(self, painter, rect, state, baseline, height) -> None:
        """Level marks up the left-hand side, and a line across at each."""
        calibration = getattr(state, "calibration", None)
        if not calibration or rect.width() < 420:
            return
        font = painter.font()
        font.setPointSizeF(max(6.5, min(9.5, rect.width() / 110.0)))
        painter.setFont(font)
        for db in self.DB_MARKS:
            where = self._height_for(db, calibration)
            if where < 0.0:
                return
            y = baseline - where * height
            if y < rect.top() + 2:
                continue
            painter.setPen(QPen(QColor(255, 255, 255, 26), 1.0))
            painter.drawLine(QPointF(rect.left() + 34, y),
                             QPointF(rect.right(), y))
            painter.setPen(QPen(QColor(200, 200, 210, 140)))
            painter.drawText(QRectF(rect.left(), y - 7, 30, 14),
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter,
                             f"{db}" if db else "0")

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        flash = self.flash(state)
        painter.fillRect(rect, QColor(10, 10, 14))
        baseline = height * 0.86
        self._segments(painter, rect, state, baseline, baseline * 0.9, flash)

        self._scale(painter, rect, state, baseline, baseline * 0.9)

        labels = state.labels
        if not labels or width < 360:
            return
        painter.setPen(QPen(QColor(200, 200, 210, 150)))
        font = painter.font()
        font.setPointSizeF(max(7.0, min(10.0, width / 90.0)))
        painter.setFont(font)
        span = width * 0.9
        left = (width - span) / 2.0
        step = span / len(labels)
        # Every third, so they never collide at a small size.
        for index in range(0, len(labels), 3):
            painter.drawText(QRectF(left + index * step - step, baseline + 4,
                                    step * 3, 16),
                             Qt.AlignmentFlag.AlignCenter, labels[index])




class Meters(Scene):
    """Ten analogue VU meters, laid out like a rack of them.

    Copied from a photograph: a shallow arc across the top, dB marks above
    it running -24 to +3, a second row of numbers inside reading 0 to 100,
    the word dB below those, the band's frequency below that, and a long
    needle hinged off the bottom of the face.

    Everything except the needle is drawn once into a pixmap per cell size
    and blitted, because ten faces of arcs and text every frame is most of
    what a scene like this costs.

    Colours come from the caller: this is the scene with a picker attached.
    """

    name = "VU meters"
    blurb = "ten analogue dials, one per band, with a colour picker"

    #: The needle's travel, in degrees, measured the way Qt measures arcs.
    #: Centred on straight up, so the face sits square in its cell - the
    #: first attempt started at 202 and leaned the whole dial to the left.
    START = 143.0
    SWEEP = -106.0

    #: Where 0 dB - which is also 100 per cent - sits along the travel.
    #: A VU movement deflects in proportion to voltage, so per cent is
    #: linear along the arc and every dB mark falls where 20*log10 puts
    #: it. Taking +3 dB as the end of the travel fixes the rest.
    _FULL = 1.0 / (10.0 ** (3.0 / 20.0))

    @staticmethod
    def _db_at(db: float) -> float:
        return (10.0 ** (db / 20.0)) * Meters._FULL

    #: dB marks along the arc, and where each one sits across the sweep.
    DB_MARKS = tuple((db, (10.0 ** (db / 20.0)) / (10.0 ** (3.0 / 20.0)))
                     for db in (-24, -12, -3, 0, 1, 2, 3))
    #: Below this face radius the per-cent row is dropped as unreadable.
    PERCENT_RADIUS = 150.0
    #: And below this, the face shows only what it can show clearly.
    ROOMY = 62.0
    #: The three numbers worth keeping when there is no room for seven.
    SPARSE_MARKS = ((-24, 0.0447), (0, 0.7079), (3, 1.0))
    #: Per cent marks, on the inside. Linear in deflection, as the movement
    #: is: the eyeballed set put 100 per cent at 0.82 of the travel, which
    #: is nearly a decibel and a half out.
    PERCENT_MARKS = tuple((pc, pc / 100.0 / (10.0 ** (3.0 / 20.0)))
                          for pc in (0, 20, 40, 60, 80, 100))
    #: Unnumbered ticks, one per dB. They crowd towards the quiet end
    #: because the scale does, which is what a real face looks like.
    MINOR = tuple((10.0 ** (db / 20.0)) / (10.0 ** (3.0 / 20.0))
                  for db in range(-20, 4))

    def __init__(self) -> None:
        self._faces: dict = {}

    # -- layout -----------------------------------------------------------
    def paint(self, painter, rect, state) -> None:
        painter.fillRect(rect, state.background)
        levels = state.dials or state.levels
        count = len(levels)
        if not count:
            return
        columns, rows = self._grid(rect, count)
        cell_w = rect.width() / columns
        cell_h = rect.height() / rows
        flash = self.flash(state)
        for index in range(count):
            box = QRectF(rect.left() + (index % columns) * cell_w,
                         rect.top() + (index // columns) * cell_h,
                         cell_w, cell_h)
            label = (state.dial_labels[index]
                     if index < len(state.dial_labels) else "")
            self._meter(painter, box, levels[index], label, state, flash)

    @staticmethod
    def _grid(rect, count: int):
        """Columns and rows that keep every cell on the screen.

        The old version always used five across, so at anything narrow than
        a wide window the faces ran past the edge and the labels went with
        them. This picks the arrangement whose cells are closest to the
        shape a meter wants, which is a little wider than it is tall.
        """
        best = (1, count, 1e9)
        for columns in range(1, count + 1):
            rows = (count + columns - 1) // columns
            cell_w = rect.width() / columns
            cell_h = rect.height() / rows
            if cell_w <= 60 or cell_h <= 54:
                continue
            # Empty cells look like something failed to draw, so a grid that
            # fills exactly is worth a lot more than a slightly better shape.
            waste = columns * rows - count
            score = waste * 2.0 + abs(cell_w / cell_h - 1.5)
            if score < best[2]:
                best = (columns, rows, score)
        return best[0], best[1]

    # -- one meter --------------------------------------------------------
    def _meter(self, painter, box, value, label, state, flash) -> None:
        key = (int(box.width()), int(box.height()), label,
               state.dial_colour.rgba())
        face = self._faces.get(key)
        if face is None:
            if len(self._faces) > 48:
                self._faces.clear()
            face = self._render_face(box, label, state)
            self._faces[key] = face
        painter.drawPixmap(box.topLeft(), face)

        geometry = self._geometry(QRectF(0, 0, box.width(), box.height()))
        painter.save()
        painter.translate(box.topLeft())
        if flash > 0.02:
            # The strobe is the meter's own backlight coming up, not a flash
            # over the top of it.
            glow = QRadialGradient(geometry["pivot"], geometry["radius"] * 1.5)
            tint = QColor(state.dial_colour)
            tint.setAlphaF(0.20 * flash)
            glow.setColorAt(0.0, tint)
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(QRectF(0, 0, box.width(), box.height()), glow)
        self._needle(painter, geometry, value, state)
        painter.restore()

    @staticmethod
    def _geometry(box) -> dict:
        """Where the arc, the pivot and the text live inside one cell.

        Worked out from the cell rather than assumed, so nothing can reach
        outside it however the window is shaped.
        """
        pad = min(box.width(), box.height()) * 0.06
        inner = box.adjusted(pad, pad, -pad, -pad)
        # Worked backwards from what has to fit. The outermost label sits
        # 1.20 radii above the centre and the frequency 0.78 below it, so
        # the two together decide how big the arc can be - which is why the
        # faces used to run off the top of the window.
        radius = min(inner.width() / 2.55, inner.height() / 2.46)
        # A face is 2.30 radii tall. On a tall cell the width caps the
        # radius, so that block has to be centred in what is left or every
        # dial sits jammed against the top with empty space underneath.
        block = radius * 2.30
        top = inner.top() + max(0.0, (inner.height() - block) / 2.0)
        centre_x = inner.center().x()
        centre_y = top + radius * 1.30
        return {
            "radius": radius,
            "centre": QPointF(centre_x, centre_y),
            # A real meter hinges the needle at the centre of its own arc.
            "pivot": QPointF(centre_x, centre_y),
            "inner": inner,
        }

    def _render_face(self, box, label, state):
        from PySide6.QtGui import QPixmap

        ratio = 2.0
        pixmap = QPixmap(max(1, int(box.width() * ratio)),
                         max(1, int(box.height() * ratio)))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(QColor(0, 0, 0, 0))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        geometry = self._geometry(QRectF(0, 0, box.width(), box.height()))
        self._draw_face(painter, geometry, label, state)
        painter.end()
        return pixmap

    def _draw_face(self, painter, geometry, label, state) -> None:
        centre = geometry["centre"]
        radius = geometry["radius"]
        colour = state.dial_colour
        dim = QColor(colour)
        dim.setAlphaF(0.42)

        span = QRectF(centre.x() - radius, centre.y() - radius,
                      radius * 2, radius * 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(colour, max(1.4, radius * 0.030)))
        painter.drawArc(span, int(self.START * 16), int(self.SWEEP * 16))

        # A small face drops what it cannot show legibly rather than
        # printing it on top of itself. Ten meters in a strip two hundred
        # pixels tall leaves each one about forty pixels of radius, and
        # everything a full face carries will not fit in that.
        roomy = radius >= self.ROOMY
        marks = self.DB_MARKS if roomy else self.SPARSE_MARKS

        for _value, fraction in marks:
            self._tick(painter, centre, radius, fraction, colour,
                       0.88, 1.0, max(1.2, radius * 0.026))
        if roomy:
            painter.setPen(QPen(dim, max(0.8, radius * 0.014)))
            for fraction in self.MINOR:
                self._tick(painter, centre, radius, fraction, dim,
                           0.94, 1.0, max(0.8, radius * 0.014))

        font = painter.font()
        font.setPointSizeF(max(5.5, radius * 0.155))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(colour))
        for value, fraction in marks:
            self._label(painter, centre, radius * 1.20, fraction, str(value))
        # The per-cent row shares the arc with the dB row. Ten faces across
        # a window leaves it about a hundred pixels of arc for six numbers,
        # which reads as a smudge, so it waits for a face big enough to
        # carry it - which is what full screen is for.
        if radius >= self.PERCENT_RADIUS:
            font.setPointSizeF(max(5.0, radius * 0.115))
            painter.setFont(font)
            painter.setPen(QPen(dim))
            for value, fraction in self.PERCENT_MARKS:
                self._label(painter, centre, radius * 0.84, fraction,
                            str(value), tight=True)
        font.setPointSizeF(max(5.5, radius * 0.155))
        painter.setFont(font)

        painter.setPen(QPen(colour))
        if roomy:
            font.setPointSizeF(max(5.5, radius * 0.150))
            painter.setFont(font)
            painter.drawText(
                QRectF(centre.x() - radius * 0.5, centre.y() + radius * 0.14,
                       radius, radius * 0.28),
                Qt.AlignmentFlag.AlignCenter, "dB")
        if label:
            font.setPointSizeF(max(6.0, radius * 0.175))
            painter.setFont(font)
            painter.drawText(
                QRectF(centre.x() - radius * 0.95, centre.y() + radius * 0.52,
                       radius * 1.9, radius * 0.32),
                Qt.AlignmentFlag.AlignCenter, label)

    def _angle(self, fraction: float) -> float:
        return math.radians(self.START + self.SWEEP
                            * max(0.0, min(1.0, fraction)))

    def _tick(self, painter, centre, radius, fraction, colour,
              inner_at, outer_at, width) -> None:
        angle = self._angle(fraction)
        painter.setPen(QPen(colour, width))
        painter.drawLine(
            QPointF(centre.x() + math.cos(angle) * radius * inner_at,
                    centre.y() - math.sin(angle) * radius * inner_at),
            QPointF(centre.x() + math.cos(angle) * radius * outer_at,
                    centre.y() - math.sin(angle) * radius * outer_at))

    def _label(self, painter, centre, distance, fraction, text,
               tight: bool = False) -> None:
        angle = self._angle(fraction)
        point = QPointF(centre.x() + math.cos(angle) * distance,
                        centre.y() - math.sin(angle) * distance)
        size = max(11.0, distance * (0.16 if tight else 0.26))
        painter.drawText(QRectF(point.x() - size, point.y() - size * 0.45,
                                size * 2, size * 0.9),
                         Qt.AlignmentFlag.AlignCenter, text)

    def _needle(self, painter, geometry, value, state) -> None:
        radius = geometry["radius"]
        pivot = geometry["pivot"]
        angle = self._angle(value)
        reach = QPointF(math.cos(angle), -math.sin(angle))
        tip = pivot + reach * (radius * 0.86)
        tail = pivot - reach * (radius * 0.14)
        halo = QColor(state.dial_colour)
        halo.setAlphaF(0.26)
        painter.setPen(QPen(halo, max(3.0, radius * 0.085),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(tail, tip)
        painter.setPen(QPen(state.dial_colour, max(1.4, radius * 0.030),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(tail, tip)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(state.dial_colour)
        painter.drawEllipse(pivot, radius * 0.045, radius * 0.045)
        painter.setBrush(Qt.BrushStyle.NoBrush)


class Ambience(Scene):
    """The one that came with the media player everybody had.

    Smooth ribbons drawn from the middle outwards and mirrored top to
    bottom, each one riding a different part of the spectrum, colours
    wandering slowly through the wheel. No bars anywhere: this scene is
    about long curves that fold over each other, and the overlaps doing
    the work that a glow would.

    Drawn additively, so where two ribbons cross they brighten - which is
    what made the original look lit from behind rather than painted.
    """

    name = "Ambience"
    blurb = "mirrored ribbons folding over each other, as it was in 2001"

    #: How many ribbons, and how many points along each. Sixty points is
    #: past the width of a pixel at any size this is drawn at.
    RIBBONS = 5
    STEPS = 44

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        middle = height * 0.5
        painter.fillRect(rect, QColor(3, 2, 8))
        levels = state.levels
        if not levels:
            return

        flash = self.flash(state)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Plus)
        for ribbon in range(self.RIBBONS):
            share = ribbon / max(1, self.RIBBONS - 1)
            # Each ribbon listens to its own quarter of the spectrum.
            low = int(share * (len(levels) - 1) * 0.75)
            band = sum(levels[low:low + 4]) / max(1, len(levels[low:low + 4]))
            reach = middle * (0.18 + band * 0.74 + flash * 0.20)
            turn = state.phase * (0.7 + ribbon * 0.23)
            shade = (state.hue + share * 0.42 + 0.1) % 1.0
            colour = QColor.fromHsvF(shade, 0.72 - flash * 0.35, 1.0,
                                     0.30 + band * 0.45 + flash * 0.2)
            # Capped. Additive compositing charges per pixel of stroke,
            # and an uncapped width put a sixteen pixel ribbon across a
            # 1080p frame ten times over - seventeen milliseconds before
            # any polish, which is the whole frame gone.
            painter.setPen(QPen(colour,
                                max(1.6, min(9.0, height * 0.010 * (0.6 + band))),
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            self._ribbon(painter, width, middle, reach, turn, ribbon, +1)
            self._ribbon(painter, width, middle, reach, turn, ribbon, -1)
        painter.restore()
        self._core(painter, width, middle, state, flash)

    def _ribbon(self, painter, width, middle, reach, turn, index, side) -> None:
        """One curve, mirrored by ``side``.

        Two sines of different periods rather than one, because a single
        sine reads as a rope and two read as something being blown about.
        """
        path = QPainterPath()
        for step in range(self.STEPS + 1):
            across = step / self.STEPS
            x = across * width
            wave = (math.sin(across * math.tau * 1.4 + turn)
                    * 0.66
                    + math.sin(across * math.tau * 2.7 - turn * 1.3 + index)
                    * 0.34)
            # Pinched at both ends, so the ribbons meet rather than being
            # cut off by the edge of the frame.
            pinch = math.sin(across * math.pi) ** 0.7
            y = middle + side * wave * reach * pinch
            if step == 0:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
        painter.drawPath(path)

    def _core(self, painter, width, middle, state, flash) -> None:
        """A soft line along the middle, brightest where the bass is."""
        glow = QLinearGradient(0.0, middle, width, middle)
        shade = (state.hue + 0.5) % 1.0
        edge = QColor.fromHsvF(shade, 0.6, 1.0, 0.0)
        centre = QColor.fromHsvF(shade, 0.25, 1.0,
                                 0.25 + state.bass * 0.5 + flash * 0.25)
        glow.setColorAt(0.0, edge)
        glow.setColorAt(0.5, centre)
        glow.setColorAt(1.0, edge)
        painter.setPen(QPen(glow, max(1.0, middle * 0.012)))
        painter.drawLine(QPointF(0.0, middle), QPointF(width, middle))


class Waterfall(Scene):
    """A spectrum analyser plotted as a landscape, seen from one corner.

    Frequency runs left to right, time runs away from you, and how loud a
    band was is how high the surface stands. Each new frame is laid down
    at the front and the whole field slides back, so a sustained note
    reads as a ridge running into the distance and a drum as a row of
    peaks marching away.

    Drawn with an oblique projection rather than a real camera: it costs
    two multiplications a point and, for a plot seen from a fixed corner,
    looks the same as the arithmetic nobody can afford sixty times a
    second.

    Colour is the height, quantised into a few bands and stroked one band
    at a time - six paths a frame rather than a pen change per segment,
    which is what makes it cheap enough to keep.
    """

    name = "Waterfall"
    blurb = "the spectrum as a landscape, running away from you"

    #: Frames kept on screen. At sixty a second the field turns over in
    #: about three quarters of a second, which reads as motion without
    #: smearing everything into one lump.
    DEPTH = 44
    #: How far each step back moves, as a fraction of the frame.
    SKEW_X = 0.30
    SKEW_Y = 0.42
    #: Colour buckets, low to high.
    SHADES = ((0.62, 0.75), (0.50, 0.85), (0.33, 0.90),
              (0.16, 0.95), (0.08, 1.00), (0.00, 1.00))

    def paint(self, painter, rect, state) -> None:
        painter.fillRect(rect, QColor(4, 4, 10))
        levels = state.levels
        if not levels:
            return

        field = getattr(state, "history", None) or [list(levels)]
        field = field[-self.DEPTH:]

        flash = self.flash(state)
        width, height = rect.width(), rect.height()
        # The plot sits in the lower left, leaning up and to the right.
        # Gutters for the axes, so the numbers sit beside the plot rather
        # than on top of the data.
        left = max(38.0, width * 0.05)
        foot = max(30.0, height * 0.075)
        plot_w = (width - left) * (1.0 - self.SKEW_X) * 0.98
        plot_h = (height - foot) * (1.0 - self.SKEW_Y) * 0.80
        origin_x = left
        origin_y = height - foot
        rise = plot_h * (1.0 + flash * 0.22)

        self._floorplan(painter, rect, origin_x, origin_y, plot_w, flash)

        buckets = [QPainterPath() for _ in self.SHADES]
        total = len(field)
        for depth, row in enumerate(field):
            # Oldest at the back, so it is drawn first and sits behind.
            back = (total - 1 - depth) / max(1, self.DEPTH - 1)
            offset_x = back * width * self.SKEW_X
            offset_y = back * height * self.SKEW_Y
            count = len(row)
            previous = None
            for index, value in enumerate(row):
                x = origin_x + offset_x + plot_w * (index / max(1, count - 1))
                y = origin_y - offset_y - value * rise
                here = QPointF(x, y)
                if previous is not None:
                    bucket = min(len(self.SHADES) - 1,
                                 int(value * len(self.SHADES)))
                    buckets[bucket].moveTo(previous)
                    buckets[bucket].lineTo(here)
                previous = here

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, path in enumerate(buckets):
            if path.isEmpty():
                continue
            hue, value = self.SHADES[index]
            share = index / max(1, len(self.SHADES) - 1)
            colour = QColor.fromHsvF(hue, 0.85 - flash * 0.45, value,
                                     0.35 + share * 0.55 + flash * 0.15)
            painter.setPen(QPen(colour, 1.0 + share * 1.4 + flash * 1.2,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)

        self._axis(painter, rect, state, origin_x, origin_y, plot_w, rise)

    def _floorplan(self, painter, rect, origin_x, origin_y, plot_w, flash) -> None:
        """The floor the landscape stands on."""
        faint = QColor(150, 170, 210, int(38 + flash * 50))
        painter.setPen(QPen(faint, 1.0))
        back_x = origin_x + rect.width() * self.SKEW_X
        back_y = origin_y - rect.height() * self.SKEW_Y
        painter.drawLine(QPointF(origin_x, origin_y),
                         QPointF(origin_x + plot_w, origin_y))
        painter.drawLine(QPointF(origin_x, origin_y), QPointF(back_x, back_y))
        painter.drawLine(QPointF(origin_x + plot_w, origin_y),
                         QPointF(back_x + plot_w, back_y))
        painter.drawLine(QPointF(back_x, back_y),
                         QPointF(back_x + plot_w, back_y))

    def _axis(self, painter, rect, state, origin_x, origin_y, plot_w,
              rise) -> None:
        """Frequency along the front, level up the side, time going back.

        Everything sits in a gutter outside the plot. It used to be
        written over the data with stub ticks floating in mid-air, which
        read as debris rather than as a scale.
        """
        if rect.width() < 380 or rect.height() < 220:
            return
        font = painter.font()
        size = max(7.0, min(10.0, rect.width() / 115.0))
        font.setPointSizeF(size)
        painter.setFont(font)
        ink = QColor(205, 214, 232, 190)
        dim = QColor(150, 165, 195, 130)
        line = QColor(150, 170, 210, 90)

        # Up the left, with a real axis line and the ticks on the outside.
        painter.setPen(QPen(line, 1.0))
        painter.drawLine(QPointF(origin_x, origin_y),
                         QPointF(origin_x, origin_y - rise))
        calibration = getattr(state, "calibration", None)
        painter.setPen(QPen(ink))
        for db in (0, -6, -12, -24):
            if calibration:
                where = Bars._height_for(db, calibration)
                if where < 0.0:
                    break
            else:
                where = 1.0 + db / 48.0
            if not 0.0 <= where <= 1.0:
                continue
            y = origin_y - where * rise
            painter.setPen(QPen(line, 1.0))
            painter.drawLine(QPointF(origin_x - 5, y), QPointF(origin_x, y))
            painter.setPen(QPen(ink))
            painter.drawText(QRectF(origin_x - 40, y - 8, 33, 16),
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignVCenter, str(db))
        painter.setPen(QPen(dim))
        painter.drawText(QRectF(origin_x - 40, origin_y - rise - 19, 33, 16),
                         Qt.AlignmentFlag.AlignRight
                         | Qt.AlignmentFlag.AlignVCenter, "dB")

        # Along the front. Every fourth band, and never two in the same
        # place however narrow the frame gets.
        labels = state.labels
        if labels:
            room = max(1, int(len(labels) * 54.0 / max(1.0, plot_w)))
            every = max(4, room)
            painter.setPen(QPen(ink))
            step = plot_w / max(1, len(labels) - 1)
            for index in range(0, len(labels), every):
                x = origin_x + index * step
                painter.setPen(QPen(line, 1.0))
                painter.drawLine(QPointF(x, origin_y),
                                 QPointF(x, origin_y + 4))
                painter.setPen(QPen(ink))
                painter.drawText(QRectF(x - 27, origin_y + 5, 54, 15),
                                 Qt.AlignmentFlag.AlignCenter, labels[index])
            painter.setPen(QPen(dim))
            painter.drawText(QRectF(origin_x, origin_y + 20, plot_w, 15),
                             Qt.AlignmentFlag.AlignCenter, "frequency")

        # Into the distance, written along the depth edge and outside it.
        back_x = origin_x + rect.width() * self.SKEW_X
        back_y = origin_y - rect.height() * self.SKEW_Y
        # One caption, in the empty triangle left of the depth edge. The
        # duration used to be written separately at the far end, which put
        # it on top of the landscape.
        painter.setPen(QPen(dim))
        seconds = self.DEPTH / 60.0
        painter.drawText(
            QRectF((origin_x + back_x) / 2.0 - 104,
                   (origin_y + back_y) / 2.0 - 8, 96, 15),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"time  −{seconds:.1f}s")


#: Every theme, in the order the picker offers them.
SCENES = (Vaporwave(), Tunnel(), Oscilloscope(), Bars(), Meters(),
          Ambience(), Waterfall())


def by_name(name: str) -> Scene:
    for scene in SCENES:
        if scene.name == name:
            return scene
    return SCENES[0]


# ==========================================================================
# Post-processing
# ==========================================================================
#: What each scene asks for after it has drawn itself. Kept here rather than
#: on the classes so the whole look of the set can be read at once.
#:
#: bloom     how much light bleeds out of bright areas
#: scanlines a CRT's horizontal lines, 0 to 1
#: vignette  how much the corners fall off
#: grain     film noise, which hides banding in the gradients
#: aberration how far the red and blue channels separate, in pixels
POST = {
    # A CRT showing a sunset: bloom for the neon, scanlines and a little
    # lens error for the tube, grain to hide banding in the sky gradient.
    "Vaporwave city": {"bloom": 0.60, "scanlines": 0.16, "vignette": 0.42,
                       "grain": 0.05, "aberration": 1.2},
    # Glass and neon, no tube: heavy bloom, a strong vignette to sell the
    # depth, and the colour fringing a wide lens gives at the edges.
    "Neon tunnel": {"bloom": 0.75, "vignette": 0.58, "aberration": 1.8},
    # Phosphor: the glow is most of the look, and the scanlines are the
    # screen it is painted on.
    "Oscilloscope": {"bloom": 0.88, "scanlines": 0.22, "vignette": 0.34},
    # A hardware meter under a lamp. Enough bloom that the lit segments
    # spill, not so much that the unlit ones wash out.
    "Equaliser": {"bloom": 0.34, "vignette": 0.26, "grain": 0.03},
    # Glass over a lit dial: grain reads as the texture of the face.
    "VU meters": {"bloom": 0.42, "vignette": 0.46, "grain": 0.07},
    # The overlaps do the work here, so the bloom is high and everything
    # else stays out of the way.
    "Ambience": {"bloom": 0.82, "vignette": 0.32, "aberration": 1.0},
    # An instrument, not a light show: enough bloom to lift the ridges off
    # the background and a vignette to keep the eye in the plot.
    "Waterfall": {"bloom": 0.50, "vignette": 0.38, "grain": 0.03},
}


def post_for(scene) -> dict:
    """The recipe for this scene, or nothing if it wants none."""
    return POST.get(getattr(scene, "name", ""), {})
