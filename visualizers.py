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
        sky.setColorAt(0.0, QColor(12, 6, 30))
        sky.setColorAt(1.0, QColor.fromHsvF((hue + 0.74) % 1.0, 0.86, 0.34))
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
        self._orb(painter, width, horizon, state)

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
                                              0.80, 0.16, 0.95))

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
        for index, value in enumerate(levels):
            tall = horizon * (0.10 + value * 0.42)
            x = index * block
            shade = ((index / count) * 0.2 + state.hue + 0.6) % 1.0
            painter.fillRect(QRectF(x, horizon - tall, block * 0.92, tall),
                             QColor.fromHsvF(shade, 0.75, 0.22, 0.95))
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

    def _orb(self, painter, width, horizon, state) -> None:
        size = 8.0 + state.mid * 32.0
        centre = QPointF(width / 2.0, horizon - size * 0.6)
        glow = QRadialGradient(centre, size * 1.9)
        shade = (state.hue + 0.45) % 1.0
        glow.setColorAt(0.0, QColor.fromHsvF(shade, 0.30, 1.0, 0.5 + state.mid * 0.4))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(centre, size * 1.9, size * 1.9)


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
            radius = 14.0 + scale * max(width, height) * 0.62
            energy = state.levels[int(t * (len(state.levels) - 1))] if state.levels else 0.0
            shade = (state.hue + t * 0.5) % 1.0
            alpha = (1.0 - t) * (0.35 + energy * 0.6)
            painter.setPen(QPen(QColor.fromHsvF(shade, 0.7, 1.0, alpha),
                                1.0 + energy * 4.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            squash = 1.0 - state.bass * 0.18
            painter.drawEllipse(centre, radius, radius * squash)

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
    """Green on black. The oldest way of looking at a signal."""

    name = "Oscilloscope"
    blurb = "phosphor green, a trace and a grid"

    def _steps(self, painter, rect, state) -> None:
        """A stepped outline along the foot, drawn like a plotted signal.

        Not a bar graph: one continuous line that jumps between levels, the
        way a scope draws a sampled waveform.
        """
        levels = state.levels
        count = len(levels)
        if not count:
            return
        flash = self.flash(state)
        foot = rect.height() * 0.97
        tall = rect.height() * 0.26
        left, bar, gap = self.geometry(rect, count, 0.96)
        path = QPainterPath()
        path.moveTo(QPointF(left, foot))
        for index, value in enumerate(levels):
            x = left + index * (bar + gap)
            y = foot - value * tall
            path.lineTo(QPointF(x, y))
            path.lineTo(QPointF(x + bar, y))
        path.lineTo(QPointF(left + count * (bar + gap), foot))
        painter.setPen(QPen(QColor(150, 255, 170, int(200 + flash * 55)),
                            1.6 + flash * 2.0))
        painter.setBrush(QColor(60, 200, 90, int(38 + flash * 60)))
        painter.drawPath(path)

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        middle = height / 2.0
        flash = self.flash(state)
        # The strobe here is the tube blooming, which is what an overdriven
        # phosphor screen actually does.
        painter.fillRect(rect, QColor(2, int(10 + flash * 40), 4))

        painter.setPen(QPen(QColor(40, 120, 60, int(90 + flash * 90)), 1.0))
        for index in range(1, 10):
            x = width * index / 10.0
            painter.drawLine(QPointF(x, 0), QPointF(x, height))
        for index in range(1, 6):
            y = height * index / 6.0
            painter.drawLine(QPointF(0, y), QPointF(width, y))

        levels = state.levels
        if levels:
            # The trace is the spectrum read as a waveform, which is not what
            # a scope shows but is what there is - and it moves like one.
            painter.setPen(QPen(QColor(120, 255, 150, 230),
                                2.0 + flash * 3.0))
            path = QPainterPath()
            points = max(2, len(levels) * 4)
            for step in range(points + 1):
                t = step / points
                band = levels[min(len(levels) - 1, int(t * len(levels)))]
                wobble = math.sin(t * 22.0 + state.phase * 14.0)
                y = middle - wobble * band * middle * 0.86
                point = QPointF(t * width, y)
                path.moveTo(point) if step == 0 else path.lineTo(point)
            painter.drawPath(path)

        self._steps(painter, rect, state)


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

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        flash = self.flash(state)
        painter.fillRect(rect, QColor(10, 10, 14))
        baseline = height * 0.86
        self._segments(painter, rect, state, baseline, baseline * 0.9, flash)

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

    #: dB marks along the arc, and where each one sits across the sweep.
    DB_MARKS = ((-24, 0.00), (-12, 0.20), (-3, 0.46), (0, 0.68),
                (1, 0.79), (2, 0.89), (3, 1.00))
    #: Below this face radius the per-cent row is dropped as unreadable.
    PERCENT_RADIUS = 150.0
    #: Per cent marks, on the inside.
    PERCENT_MARKS = ((0, 0.02), (20, 0.19), (40, 0.36), (60, 0.53),
                     (80, 0.68), (100, 0.82))

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
        radius = min(inner.width() / 2.55, inner.height() / 2.30)
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

        # Long marks at the numbered stops, short ones between.
        for _value, fraction in self.DB_MARKS:
            self._tick(painter, centre, radius, fraction, colour,
                       0.88, 1.0, max(1.2, radius * 0.026))
        painter.setPen(QPen(dim, max(0.8, radius * 0.014)))
        for step in range(1, 28):
            self._tick(painter, centre, radius, step / 28.0, dim,
                       0.94, 1.0, max(0.8, radius * 0.014))

        font = painter.font()
        font.setPointSizeF(max(5.5, radius * 0.155))
        font.setBold(False)
        painter.setFont(font)
        painter.setPen(QPen(colour))
        for value, fraction in self.DB_MARKS:
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


#: Every theme, in the order the picker offers them.
SCENES = (Vaporwave(), Tunnel(), Oscilloscope(), Bars(), Meters())


def by_name(name: str) -> Scene:
    for scene in SCENES:
        if scene.name == name:
            return scene
    return SCENES[0]
