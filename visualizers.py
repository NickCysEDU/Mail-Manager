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

    # -- helpers every scene wants ---------------------------------------
    @staticmethod
    def bars(painter, rect, state, baseline: float, height: float,
             hue_shift: float = 0.0, mirror: bool = False) -> None:
        """The equaliser itself, which every theme shows somewhere."""
        levels = state.levels
        count = len(levels)
        if not count:
            return
        span = rect.width() * 0.9
        left = rect.left() + (rect.width() - span) / 2.0
        gap = max(1.0, span / count * 0.18)
        bar = max(1.0, (span - gap * (count - 1)) / count)
        for index, value in enumerate(levels):
            x = left + index * (bar + gap)
            tall = value * height
            shade = ((index / count) * 0.42 + state.hue + hue_shift) % 1.0
            top = QColor.fromHsvF(shade, 0.60, 1.0, 0.95)
            base = QColor.fromHsvF((shade + 0.1) % 1.0, 0.88, 0.80, 0.9)
            gradient = QLinearGradient(0.0, baseline - tall, 0.0, baseline)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, base)
            painter.fillRect(QRectF(x, baseline - tall, bar, tall), gradient)
            cap = state.peaks[index] * height
            if cap > 3:
                painter.fillRect(QRectF(x, baseline - cap - 2.0, bar, 2.0),
                                 QColor.fromHsvF(shade, 0.12, 1.0, 0.92))
            if mirror:
                fade = QLinearGradient(0.0, baseline, 0.0, baseline + tall * 0.45)
                dim = QColor(base)
                dim.setAlphaF(0.26)
                fade.setColorAt(0.0, dim)
                fade.setColorAt(1.0, QColor(0, 0, 0, 0))
                painter.fillRect(QRectF(x, baseline, bar, tall * 0.45), fade)

    @staticmethod
    def strobe(painter, rect, state) -> None:
        """A flash on a bass transient, if it is switched on."""
        if not state.strobe or state.hit <= 0.01:
            return
        painter.fillRect(rect, QColor(255, 255, 255, int(70 * state.hit)))


class Vaporwave(Scene):
    """A grid, a sun, a skyline. The one everybody pictures."""

    name = "Vaporwave city"
    blurb = "grid, sun and a skyline that rises with the bass"

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        horizon = height * 0.54
        hue = state.hue

        sky = QLinearGradient(0.0, 0.0, 0.0, horizon)
        sky.setColorAt(0.0, QColor(12, 6, 30))
        sky.setColorAt(1.0, QColor.fromHsvF((hue + 0.74) % 1.0, 0.86, 0.34))
        painter.fillRect(QRectF(0, 0, width, horizon), sky)

        radius = horizon * (0.46 + state.bass * 0.26)
        sun = QRadialGradient(QPointF(width / 2.0, horizon), radius)
        sun.setColorAt(0.0, QColor.fromHsvF(hue, 0.50, 1.0, 0.60 + state.bass * 0.3))
        sun.setColorAt(0.75, QColor.fromHsvF((hue + 0.05) % 1.0, 0.9, 1.0, 0.18))
        sun.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, width, horizon), sun)

        self._skyline(painter, width, horizon, state)
        self._floor(painter, width, height, horizon, state)
        self.bars(painter, rect, state, horizon, horizon * 0.72, mirror=True)
        self._ribbons(painter, width, horizon, state)
        self._orb(painter, width, horizon, state)
        self.strobe(painter, rect, state)

    def _skyline(self, painter, width, horizon, state) -> None:
        """Towers, each one a band. A city that is also the equaliser."""
        levels = state.levels
        count = len(levels)
        if not count:
            return
        block = width / count
        for index, value in enumerate(levels):
            tall = horizon * (0.10 + value * 0.42)
            x = index * block
            shade = ((index / count) * 0.2 + state.hue + 0.6) % 1.0
            painter.fillRect(QRectF(x, horizon - tall, block * 0.92, tall),
                             QColor.fromHsvF(shade, 0.75, 0.22, 0.95))
            # Lit windows, only on the taller half, so a quiet band goes dark.
            if value > 0.25:
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor.fromHsvF(shade, 0.35, 1.0,
                                                 0.28 + value * 0.5))
                rows = int(tall / 9)
                for row in range(rows):
                    if (index + row) % 3 == 0:
                        painter.drawRect(QRectF(x + block * 0.22,
                                                horizon - tall + row * 9 + 3,
                                                block * 0.2, 3.0))

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

        for index in range(self.RINGS, 0, -1):
            t = ((index + state.scroll) % self.RINGS) / self.RINGS
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

        self.bars(painter, rect, state, height * 0.94, height * 0.34,
                  hue_shift=0.3)
        self._sparks(painter, state)
        self.strobe(painter, rect, state)

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

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        middle = height / 2.0
        painter.fillRect(rect, QColor(2, 10, 4))

        painter.setPen(QPen(QColor(40, 120, 60, 90), 1.0))
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
            painter.setPen(QPen(QColor(120, 255, 150, 230), 2.0))
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

        self.bars(painter, rect, state, height * 0.97, height * 0.24,
                  hue_shift=0.28)
        self.strobe(painter, rect, state)


class Bars(Scene):
    """Just the equaliser, drawn properly, with the frequencies written on."""

    name = "Equaliser"
    blurb = "the bands and nothing else, with their frequencies"

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        painter.fillRect(rect, QColor(10, 10, 14))
        baseline = height * 0.86
        self.bars(painter, rect, state, baseline, baseline * 0.9)

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
        self.strobe(painter, rect, state)


#: Every theme, in the order the picker offers them.
SCENES = (Vaporwave(), Tunnel(), Oscilloscope(), Bars())


def by_name(name: str) -> Scene:
    for scene in SCENES:
        if scene.name == name:
            return scene
    return SCENES[0]
