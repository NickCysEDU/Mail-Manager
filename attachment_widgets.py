"""The pieces the attachment window is built from.

Two of these exist because the obvious version was wrong:

*SeekBar.* A QSlider does not move to where you click, and a media player
reports its old position for a moment after a seek. Together those make a
click flash to the new place and slide back, which is the bug that made
scrubbing feel broken. Clicking is handled here, and reports from the player
are ignored until one arrives near where the seek was aimed.

*Spectrum.* Qt6 has no audio probe, so the analysis is precomputed and the
paint loop only interpolates between two rows of numbers. No arithmetic per
frame beyond that, no allocation in paintEvent, and it stops entirely when
nothing is playing.
"""

from __future__ import annotations

import math
from typing import List, Optional

import math as _math

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import QSlider, QStyle, QStyleOptionSlider, QWidget


class SeekBar(QSlider):
    """A slider that goes where you click and stays where you put it."""

    seeked = Signal(int)

    def __init__(self) -> None:
        super().__init__(Qt.Orientation.Horizontal)
        self.setRange(0, 0)
        self._pending: Optional[int] = None
        self._dragging = False
        # If the player never confirms, stop ignoring it rather than freezing.
        self._giveup = QTimer(self)
        self._giveup.setSingleShot(True)
        self._giveup.setInterval(1200)
        self._giveup.timeout.connect(self._stop_waiting)

    # -- clicking ---------------------------------------------------------
    def _value_at(self, x: int) -> int:
        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderGroove, self)
        handle = self.style().subControlRect(
            QStyle.ComplexControl.CC_Slider, option,
            QStyle.SubControl.SC_SliderHandle, self)
        span = groove.width() - handle.width()
        if span <= 0:
            return self.minimum()
        position = min(max(x - groove.x() - handle.width() / 2, 0), span)
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), int(position), int(span))

    def mousePressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if event.button() == Qt.MouseButton.LeftButton and self.maximum() > 0:
            self._dragging = True
            value = self._value_at(int(event.position().x()))
            self.setValue(value)
            self._request(value)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:      # noqa: N802
        if self._dragging and self.maximum() > 0:
            value = self._value_at(int(event.position().x()))
            self.setValue(value)
            self._request(value)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:      # noqa: N802
        if self._dragging:
            self._dragging = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _request(self, value: int) -> None:
        self._pending = value
        self._giveup.start()
        self.seeked.emit(value)

    def _stop_waiting(self) -> None:
        self._pending = None

    # -- reports from the player -----------------------------------------
    def report(self, position: int) -> None:
        """Where the player says it is. Ignored while a seek is settling.

        A player keeps emitting the position it had before the seek for a
        little while after it, and those reports arrive out of order. The
        first version of this cleared the guard as soon as one report landed
        near the target, which let the *next* stale one through - so a quick
        run of clicks still snapped the handle backwards. The guard now holds
        for the whole settling window, and anything far from where the seek
        was aimed is dropped for its duration.
        """
        if self._dragging:
            return
        if self._pending is not None:
            tolerance = max(750, self.maximum() // 100)
            if abs(position - self._pending) > tolerance:
                return
            # Near enough to be real: follow it, but keep guarding until the
            # window closes, because more stale reports may be behind it.
            self._pending = position
        self.blockSignals(True)
        self.setValue(position)
        self.blockSignals(False)


class Spectrum(QWidget):
    """A small window onto the music, with the parts of it kept separate.

    Four things move independently, because one bar graph reacting to
    everything reads as noise:

    *The floor* is a perspective grid that recedes to a horizon. It scrolls
    at a steady rate and its brightness follows the bass, so a kick lands as
    a pulse down the whole plane. This is the nostalgic part and it is on
    purpose.

    *The bars* are the spectrum, drawn twice: once standing on the floor at
    the horizon, once as a dimmer reflection in front of it. Perspective is a
    single horizontal scale that shrinks with height, which is enough to read
    as depth and costs one multiply.

    *The orb* in the middle breathes with the mid band - where a voice sits -
    so singing pushes it out and an instrumental passage lets it settle.

    *The sparks* are triggered by transients in the top bands, which is where
    cymbals and consonants live. They are a fixed pool of sixty, reused, so a
    loud passage cannot allocate anything.

    All of it draws from numbers worked out before playback started. The
    paint loop interpolates and draws; it computes no spectra. Nothing runs
    when nothing is playing.
    """

    HEIGHT = 168

    #: Which bands feed which element, as fractions of the band count.
    BASS = (0.00, 0.14)
    MID = (0.18, 0.52)
    SYNTH = (0.52, 0.74)
    HIGH = (0.76, 1.00)

    #: Sparks in the pool. Fixed, so a busy passage allocates nothing.
    SPARKS = 60

    #: How long a paused track keeps its visualiser before it flows away.
    IDLE_SECONDS = 30

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(0)
        self._frames: List = []
        self._rate = 20
        self._position = 0
        self._level: List[float] = []
        self._peak: List[float] = []
        self._bass = 0.0
        self._mid = 0.0
        self._synth = 0.0
        self._high = 0.0
        self._last_high = 0.0
        self._scroll = 0.0
        self._phase = 0.0
        # x, y, vx, vy, life - reused rather than reallocated.
        self._sparks = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(self.SPARKS)]
        self._next_spark = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)          # ~30fps
        self._timer.timeout.connect(self._tick)

        #: 0 hidden, 1 fully out. Everything drawn is scaled by this, so the
        #: arrival and the departure are the same code read in two
        #: directions.
        self._reveal = 0.0
        self._idling = False
        self._drift = 0.0
        #: Playing was asked for, whether or not there was anything to show.
        self._wanted = False
        self.setMaximumHeight(0)

        self._flow = QVariantAnimation(self)
        self._flow.setDuration(900)
        self._flow.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._flow.valueChanged.connect(self._reveal_changed)

        #: Paused, and counting down to going away.
        self._away = QTimer(self)
        self._away.setSingleShot(True)
        self._away.setInterval(self.IDLE_SECONDS * 1000)
        self._away.timeout.connect(self.conceal)

    # -- input ------------------------------------------------------------
    def set_frames(self, frames: List, rate: int) -> None:
        """The analysis, which finishes a moment after playback starts.

        Whoever presses play does it before this arrives, so the request to
        appear is remembered and acted on here. Without that the spectrum
        waits for a second press that never comes.
        """
        self._frames = frames or []
        self._rate = max(1, rate)
        width = len(self._frames[0]) if self._frames else 0
        self._level = [0.0] * width
        self._peak = [0.0] * width
        if self._frames and self._wanted:
            self.set_playing(True)
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position = max(0, milliseconds)

    def set_playing(self, playing: bool) -> None:
        """Arrive on play, idle on pause, leave after a while of neither."""
        self._wanted = bool(playing)
        if playing and self._frames:
            self._idling = False
            self._away.stop()
            self.reveal()
            self._timer.start()
            return
        if self._reveal > 0.0 and self._frames:
            # Keep moving, quietly, and go if nothing happens.
            self._idling = True
            self._timer.start()
            self._away.start()
            return
        self._timer.stop()
        self.update()

    # -- arriving and leaving ---------------------------------------------
    def reveal(self) -> None:
        if self._reveal >= 1.0 and self.maximumHeight() >= self.HEIGHT:
            return
        self._animate_to(1.0)

    def conceal(self) -> None:
        self._away.stop()
        self._animate_to(0.0)

    def _animate_to(self, target: float) -> None:
        self._flow.stop()
        self._flow.setStartValue(float(self._reveal))
        self._flow.setEndValue(float(target))
        self._flow.start()

    def _reveal_changed(self, value) -> None:
        self._reveal = max(0.0, min(1.0, float(value)))
        self.setMaximumHeight(int(self.HEIGHT * self._reveal))
        if self._reveal <= 0.001:
            self._timer.stop()
            self._idling = False
        self.updateGeometry()
        self.update()

    def clear(self) -> None:
        self._flow.stop()
        self._away.stop()
        self._reveal = 0.0
        self._idling = False
        self.setMaximumHeight(0)
        self._timer.stop()
        self._frames = []
        self._level = []
        self._peak = []
        self._bass = self._mid = self._synth = self._high = 0.0
        self._wanted = False
        for spark in self._sparks:
            spark[4] = 0.0
        self.update()

    @property
    def ready(self) -> bool:
        return bool(self._frames)

    # -- animation --------------------------------------------------------
    def _row(self) -> Optional[List[float]]:
        if not self._frames:
            return None
        exact = self._position / 1000.0 * self._rate
        index = int(exact)
        if index >= len(self._frames):
            return None
        first = self._frames[index]
        if index + 1 < len(self._frames):
            second = self._frames[index + 1]
            blend = exact - index
            return [a + (b - a) * blend for a, b in zip(first, second)]
        return list(first)

    def _band(self, row: List[float], span) -> float:
        count = len(row)
        low = int(span[0] * count)
        high = max(low + 1, int(span[1] * count))
        return sum(row[low:high]) / max(1, high - low)

    def _idle_row(self) -> List[float]:
        """A slow wave travelling across the bands.

        Paused is not stopped. Something that freezes mid-song looks broken;
        something that breathes looks like it is waiting.
        """
        count = len(self._level) or 32
        return [0.06 + 0.10 * (1.0 + _math.sin(self._drift * 2.2 + i * 0.42)) / 2.0
                * (0.35 + 0.65 * _math.sin(self._drift * 0.7 + i * 0.13) ** 2)
                for i in range(count)]

    def _tick(self) -> None:
        self._drift += 0.035
        row = self._idle_row() if self._idling else self._row()
        if row is None:
            self.update()
            return
        for i, value in enumerate(row):
            if i >= len(self._level):
                break
            current = self._level[i]
            self._level[i] = value if value > current else current * 0.80 + value * 0.20
            if self._level[i] >= self._peak[i]:
                self._peak[i] = self._level[i]
            else:
                self._peak[i] = max(self._level[i], self._peak[i] - 0.014)

        bass = self._band(row, self.BASS)
        mid = self._band(row, self.MID)
        synth = self._band(row, self.SYNTH)
        high = self._band(row, self.HIGH)
        self._bass = self._bass * 0.72 + bass * 0.28
        self._mid = self._mid * 0.80 + mid * 0.20
        # Slower than the rest: a pad should swell, not flicker.
        self._synth = self._synth * 0.88 + synth * 0.12
        self._high = self._high * 0.55 + high * 0.45

        # A transient in the top bands, not loudness: cymbals are sudden.
        if high - self._last_high > 0.10:
            self._spawn(min(4, int((high - self._last_high) * 22)))
        self._last_high = high

        self._scroll = (self._scroll + 0.012 + self._bass * 0.05) % 1.0
        self._phase += 0.0045
        for spark in self._sparks:
            if spark[4] <= 0.0:
                continue
            spark[0] += spark[2]
            spark[1] += spark[3]
            spark[3] += 0.045          # a little gravity
            spark[4] -= 0.028
        self.update()

    def _spawn(self, count: int) -> None:
        width = max(1, self.width())
        for _ in range(count):
            spark = self._sparks[self._next_spark]
            self._next_spark = (self._next_spark + 1) % self.SPARKS
            spark[0] = width * (0.5 + (self._phase * 7.3 % 1.0 - 0.5) * 0.8)
            spark[1] = self.HEIGHT * 0.52
            spark[2] = (self._phase * 11.7 % 1.0 - 0.5) * 3.4
            spark[3] = -1.6 - (self._phase * 5.1 % 1.0) * 1.4
            spark[4] = 1.0

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        width, height = rect.width(), rect.height()
        horizon = height * 0.52

        painter.fillRect(rect, QColor(8, 6, 18))

        if self._reveal <= 0.001:
            return
        if self._reveal < 0.999:
            # Rise into place and fade, rather than appearing whole.
            painter.setOpacity(self._reveal)
            painter.translate(0.0, (1.0 - self._reveal) * height * 0.45)

        if not self._level:
            painter.setPen(QPen(QColor(150, 150, 170, 70)))
            painter.drawLine(0, int(horizon), width, int(horizon))
            painter.setPen(QPen(QColor(150, 150, 170, 120)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "the spectrum appears when something is playing")
            return

        hue = (self._phase * 0.5) % 1.0
        self._paint_sky(painter, width, horizon, hue)
        self._paint_floor(painter, width, height, horizon, hue)
        self._paint_bars(painter, width, horizon, hue)
        self._paint_ribbons(painter, width, horizon, hue)
        self._paint_orb(painter, width, horizon, hue)
        self._paint_sparks(painter, hue)

    def _paint_sky(self, painter, width, horizon, hue) -> None:
        sky = QLinearGradient(0.0, 0.0, 0.0, horizon)
        sky.setColorAt(0.0, QColor(10, 8, 26))
        sky.setColorAt(1.0, QColor.fromHsvF((hue + 0.72) % 1.0, 0.85, 0.30, 1.0))
        painter.fillRect(QRectF(0, 0, width, horizon), sky)
        # A sun on the horizon, swelling with the low end.
        radius = horizon * (0.42 + self._bass * 0.22)
        glow = QRadialGradient(QPointF(width / 2.0, horizon), radius)
        glow.setColorAt(0.0, QColor.fromHsvF(hue, 0.55, 1.0, 0.55 + self._bass * 0.3))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(QRectF(0, 0, width, horizon), glow)

    def _paint_floor(self, painter, width, height, horizon, hue) -> None:
        """A grid in perspective. Lines, not polygons: cheap and it reads."""
        depth = height - horizon
        colour = QColor.fromHsvF(hue, 0.7, 1.0, 0.30 + self._bass * 0.45)
        painter.setPen(QPen(colour, 1.0))
        # Receding horizontals, spaced so they bunch towards the horizon.
        for step in range(1, 13):
            t = ((step + self._scroll) / 13.0) ** 2.4
            y = horizon + t * depth
            painter.drawLine(QPointF(0, y), QPointF(width, y))
        # Verticals converging on the middle.
        middle = width / 2.0
        for index in range(-7, 8):
            x = middle + index * width * 0.16
            painter.drawLine(QPointF(middle + index * 6.0, horizon),
                             QPointF(x, height))

    def _paint_bars(self, painter, width, horizon, hue) -> None:
        count = len(self._level)
        if not count:
            return
        span = width * 0.86
        left = (width - span) / 2.0
        gap = 2.0
        bar = max(1.0, (span - gap * (count - 1)) / count)
        ceiling = horizon - 10
        for index, value in enumerate(self._level):
            x = left + index * (bar + gap)
            tall = value * ceiling * 0.82
            shade = ((index / count) * 0.42 + hue) % 1.0
            top = QColor.fromHsvF(shade, 0.62, 1.0, 0.96)
            base = QColor.fromHsvF((shade + 0.1) % 1.0, 0.9, 0.85, 0.9)
            gradient = QLinearGradient(0.0, horizon - tall, 0.0, horizon)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(1.0, base)
            painter.fillRect(QRectF(x, horizon - tall, bar, tall), gradient)
            # Its reflection, squashed and faded, standing on the floor.
            mirror = QLinearGradient(0.0, horizon, 0.0, horizon + tall * 0.42)
            faded = QColor(base)
            faded.setAlphaF(0.28)
            mirror.setColorAt(0.0, faded)
            mirror.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(QRectF(x, horizon, bar, tall * 0.42), mirror)
            cap = self._peak[index] * ceiling * 0.82
            if cap > 3:
                painter.fillRect(QRectF(x, horizon - cap - 2.0, bar, 2.0),
                                 QColor.fromHsvF(shade, 0.15, 1.0, 0.9))

    def _paint_ribbons(self, painter, width, horizon, hue) -> None:
        """The synth band: slow ribbons undulating across the sky.

        Three polylines at twenty points each. They swell with the upper
        mids, which is where a pad or a lead sits, and they keep moving in
        the idle state so a paused track still looks alive.
        """
        strength = self._synth
        if strength < 0.02:
            return
        points = 20
        for ribbon in range(3):
            amplitude = horizon * (0.05 + strength * 0.16) * (1.0 - ribbon * 0.22)
            middle = horizon * (0.28 + ribbon * 0.13)
            shade = (hue + 0.52 + ribbon * 0.06) % 1.0
            colour = QColor.fromHsvF(shade, 0.55, 1.0,
                                     (0.22 + strength * 0.45) * (1.0 - ribbon * 0.25))
            painter.setPen(QPen(colour, 2.0 - ribbon * 0.4))
            path = QPainterPath()
            for step in range(points + 1):
                t = step / points
                x = t * width
                y = middle + _math.sin(
                    t * 6.0 + self._phase * 9.0 + ribbon * 1.7) * amplitude
                if step == 0:
                    path.moveTo(QPointF(x, y))
                else:
                    path.lineTo(QPointF(x, y))
            painter.drawPath(path)

    def _paint_orb(self, painter, width, horizon, hue) -> None:
        """The mid band, where a voice sits."""
        size = 8.0 + self._mid * 34.0
        centre = QPointF(width / 2.0, horizon - size * 0.6)
        glow = QRadialGradient(centre, size * 1.9)
        glow.setColorAt(0.0, QColor.fromHsvF((hue + 0.45) % 1.0, 0.35, 1.0,
                                             0.55 + self._mid * 0.4))
        glow.setColorAt(0.55, QColor.fromHsvF((hue + 0.45) % 1.0, 0.8, 1.0, 0.22))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setBrush(glow)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(centre, size * 1.9, size * 1.9)

    def _paint_sparks(self, painter, hue) -> None:
        painter.setPen(Qt.PenStyle.NoPen)
        for spark in self._sparks:
            life = spark[4]
            if life <= 0.0:
                continue
            colour = QColor.fromHsvF((hue + 0.15) % 1.0, 0.18, 1.0, life * 0.9)
            painter.setBrush(colour)
            radius = 1.2 + life * 1.8
            painter.drawEllipse(QPointF(spark[0], spark[1]), radius, radius)
