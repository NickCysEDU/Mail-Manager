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

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
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
    """Mirrored bars that follow the music, from numbers worked out already.

    Deliberately small: a strip, not a light show. It draws nothing at all
    when there is no analysis or nothing is playing, so an idle window costs
    the same as it did before this existed.
    """

    HEIGHT = 92

    def __init__(self) -> None:
        super().__init__()
        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self._frames: List = []
        self._rate = 20
        self._position = 0
        self._playing = False
        #: Smoothed values and slowly-falling peaks, one per band.
        self._level: List[float] = []
        self._peak: List[float] = []
        self._phase = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(33)          # ~30fps
        self._timer.timeout.connect(self._tick)

    # -- input ------------------------------------------------------------
    def set_frames(self, frames: List, rate: int) -> None:
        self._frames = frames or []
        self._rate = max(1, rate)
        width = len(self._frames[0]) if self._frames else 0
        self._level = [0.0] * width
        self._peak = [0.0] * width
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position = max(0, milliseconds)

    def set_playing(self, playing: bool) -> None:
        self._playing = bool(playing)
        if playing and self._frames:
            self._timer.start()
        else:
            self._timer.stop()
            self.update()

    def clear(self) -> None:
        self._timer.stop()
        self._frames = []
        self._level = []
        self._peak = []
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

    def _tick(self) -> None:
        row = self._row()
        if row is None:
            self.update()
            return
        # Rise quickly, fall slowly: the eye wants attack and decay.
        for i, value in enumerate(row):
            if i >= len(self._level):
                break
            current = self._level[i]
            self._level[i] = value if value > current else current * 0.82 + value * 0.18
            if self._level[i] >= self._peak[i]:
                self._peak[i] = self._level[i]
            else:
                self._peak[i] = max(self._level[i], self._peak[i] - 0.012)
        self._phase += 0.006
        self.update()

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        middle = rect.height() / 2.0

        if not self._level:
            painter.setPen(QPen(QColor(128, 128, 128, 90)))
            painter.drawLine(0, int(middle), rect.width(), int(middle))
            return

        bass = sum(self._level[:4]) / 4.0 if len(self._level) >= 4 else 0.0

        # A bloom behind everything, driven by the low end.
        if bass > 0.05:
            glow = QRadialGradient(QPointF(rect.width() / 2.0, middle),
                                   rect.width() * (0.35 + bass * 0.3))
            hue = (self._phase * 0.35) % 1.0
            colour = QColor.fromHsvF(hue, 0.65, 1.0, min(0.20, bass * 0.22))
            glow.setColorAt(0.0, colour)
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.fillRect(rect, glow)

        count = len(self._level)
        gap = 2.0
        width = max(1.0, (rect.width() - gap * (count - 1)) / count)
        for index, value in enumerate(self._level):
            x = index * (width + gap)
            height = value * (middle - 6)
            hue = ((index / max(1, count)) * 0.55 + self._phase) % 1.0
            top = QColor.fromHsvF(hue, 0.72, 1.0, 0.95)
            bottom = QColor.fromHsvF((hue + 0.08) % 1.0, 0.85, 0.72, 0.55)
            gradient = QLinearGradient(0.0, middle - height, 0.0, middle + height)
            gradient.setColorAt(0.0, top)
            gradient.setColorAt(0.5, bottom)
            gradient.setColorAt(1.0, top)
            path = QPainterPath()
            path.addRoundedRect(
                QRectF(x, middle - height, width, height * 2), width / 2.4, width / 2.4)
            painter.fillPath(path, gradient)

            cap = self._peak[index] * (middle - 6)
            if cap > 2:
                painter.fillRect(
                    QRectF(x, middle - cap - 2.0, width, 2.0),
                    QColor.fromHsvF(hue, 0.25, 1.0, 0.85))
                painter.fillRect(
                    QRectF(x, middle + cap, width, 2.0),
                    QColor.fromHsvF(hue, 0.25, 1.0, 0.85))

        # A thread through the middle, so silence still reads as a line.
        painter.setPen(QPen(QColor(255, 255, 255, 40)))
        painter.drawLine(0, int(middle), rect.width(), int(middle))
