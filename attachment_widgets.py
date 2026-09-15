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

import visualizers

from PySide6.QtCore import (QEasingCurve, QPointF, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QLinearGradient, QPainter, QPainterPath,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import (QLabel, QSlider, QStyle, QStyleOptionSlider,
                               QVBoxLayout, QWidget)


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


class SpectrumState:
    """Everything a scene is handed, and nothing it has to work out."""

    __slots__ = ("levels", "peaks", "bass", "mid", "synth", "high", "hit",
                 "hue", "phase", "scroll", "strobe", "sparks", "labels")

    def __init__(self) -> None:
        self.levels: List[float] = []
        self.peaks: List[float] = []
        self.bass = self.mid = self.synth = self.high = 0.0
        self.hit = 0.0
        self.hue = 0.0
        self.phase = 0.0
        self.scroll = 0.0
        self.strobe = False
        self.sparks: List[List[float]] = []
        self.labels: List[str] = []


class Spectrum(QWidget):
    """The equaliser, and whichever scene is drawing it.

    The numbers are worked out once, before playback. This keeps the smoothed
    state and hands it to a scene; changing theme swaps one object and costs
    nothing. Nothing runs while nothing is playing.
    """

    HEIGHT = 240

    #: Which bands feed which aggregate, as fractions of the band count.
    BASS = (0.00, 0.16)
    MID = (0.20, 0.52)
    SYNTH = (0.52, 0.74)
    HIGH = (0.76, 1.00)

    SPARKS = 60
    IDLE_SECONDS = 30

    def __init__(self) -> None:
        super().__init__()
        self.setMaximumHeight(0)
        self._frames: List = []
        self._rate = 15
        self._position = 0
        self._level: List[float] = []
        self._peak: List[float] = []
        self._last_high = 0.0
        self._last_bass = 0.0
        self._state = SpectrumState()
        self._scene = visualizers.SCENES[0]
        self._sparks = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(self.SPARKS)]
        self._next_spark = 0
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

        self._reveal = 0.0
        self._idling = False
        self._drift = 0.0
        self._wanted = False
        self._source = None

        self._flow = QVariantAnimation(self)
        self._flow.setDuration(900)
        self._flow.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._flow.valueChanged.connect(self._reveal_changed)

        self._away = QTimer(self)
        self._away.setSingleShot(True)
        self._away.setInterval(self.IDLE_SECONDS * 1000)
        self._away.timeout.connect(self.conceal)

    # -- what it shows ----------------------------------------------------
    def set_scene(self, scene) -> None:
        self._scene = scene
        self.update()

    def set_strobe(self, on: bool) -> None:
        self._state.strobe = bool(on)

    def set_labels(self, labels) -> None:
        self._state.labels = list(labels or [])

    def set_frames(self, frames: List, rate: int) -> None:
        """The analysis, which lands a moment after playback starts."""
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

    def follow(self, source) -> None:
        """Where to read the position, rather than waiting to be told.

        positionChanged fires only when the position changes, and not at all
        until the player has produced samples - so the display sat on frame
        zero, which is the silence at the top of a track. It looked dead
        until the track was paused and skipped, which forced a report out.
        """
        self._source = source

    def set_playing(self, playing: bool) -> None:
        self._wanted = bool(playing)
        if playing and self._frames:
            self._idling = False
            self._away.stop()
            self.reveal()
            self._timer.start()
            return
        if self._reveal > 0.0 and self._frames:
            self._idling = True
            self._timer.start()
            self._away.start()
            return
        self._timer.stop()
        self.update()

    def clear(self) -> None:
        self._flow.stop()
        self._away.stop()
        self._reveal = 0.0
        self._idling = False
        self._wanted = False
        self.setMinimumHeight(0)
        self.setMaximumHeight(0)
        self._timer.stop()
        self._frames = []
        self._level = []
        self._peak = []
        for spark in self._sparks:
            spark[4] = 0.0
        self.updateGeometry()
        self.update()

    @property
    def ready(self) -> bool:
        return bool(self._frames)

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
        height = int(self.HEIGHT * self._reveal)
        # Minimum, maximum and both hints together. A maximum on its own
        # leaves the minimum at zero and the hint at -1, so a layout hands
        # out whatever is spare - which in a full pane is nothing.
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        if self._reveal <= 0.001:
            self._timer.stop()
            self._idling = False
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        return QSize(420, int(self.HEIGHT * self._reveal))

    def minimumSizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        return QSize(0, int(self.HEIGHT * self._reveal))

    # -- the numbers ------------------------------------------------------
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

    def _idle_row(self) -> List[float]:
        count = len(self._level) or 24
        return [0.05 + 0.09 * (1.0 + _math.sin(self._drift * 2.1 + i * 0.44)) / 2.0
                * (0.35 + 0.65 * _math.sin(self._drift * 0.7 + i * 0.13) ** 2)
                for i in range(count)]

    def _band(self, row: List[float], span) -> float:
        count = len(row)
        low = int(span[0] * count)
        high = max(low + 1, int(span[1] * count))
        return sum(row[low:high]) / max(1, high - low)

    def _tick(self) -> None:
        if self._source is not None and not self._idling:
            try:
                self._position = max(0, int(self._source()))
            except Exception:      # noqa: BLE001 - a dead player is not fatal
                pass
        self._drift += 0.035
        row = self._idle_row() if self._idling else self._row()
        if row is None:
            self.update()
            return

        for i, value in enumerate(row):
            if i >= len(self._level):
                break
            current = self._level[i]
            self._level[i] = value if value > current else current * 0.78 + value * 0.22
            if self._level[i] >= self._peak[i]:
                self._peak[i] = self._level[i]
            else:
                self._peak[i] = max(self._level[i], self._peak[i] - 0.016)

        state = self._state
        bass = self._band(row, self.BASS)
        state.bass = state.bass * 0.70 + bass * 0.30
        state.mid = state.mid * 0.80 + self._band(row, self.MID) * 0.20
        state.synth = state.synth * 0.88 + self._band(row, self.SYNTH) * 0.12
        high = self._band(row, self.HIGH)
        state.high = state.high * 0.55 + high * 0.45

        # Transients, not loudness: a cymbal is sudden and so is a kick.
        if high - self._last_high > 0.09:
            self._spawn(min(4, int((high - self._last_high) * 22)))
        self._last_high = high
        state.hit = max(0.0, state.hit - 0.16)
        if bass - self._last_bass > 0.10:
            state.hit = 1.0
        self._last_bass = bass

        state.scroll = (state.scroll + 0.012 + state.bass * 0.05) % 1.0
        state.phase += 0.0045
        state.hue = (state.phase * 0.5) % 1.0
        state.levels = self._level
        state.peaks = self._peak
        state.sparks = self._sparks

        for spark in self._sparks:
            if spark[4] <= 0.0:
                continue
            spark[0] += spark[2]
            spark[1] += spark[3]
            spark[3] += 0.045
            spark[4] -= 0.028
        self.update()

    def _spawn(self, count: int) -> None:
        width = max(1, self.width())
        for _ in range(count):
            spark = self._sparks[self._next_spark]
            self._next_spark = (self._next_spark + 1) % self.SPARKS
            phase = self._state.phase
            spark[0] = width * (0.5 + (phase * 7.3 % 1.0 - 0.5) * 0.8)
            spark[1] = self.height() * 0.52
            spark[2] = (phase * 11.7 % 1.0 - 0.5) * 3.4
            spark[3] = -1.6 - (phase * 5.1 % 1.0) * 1.4
            spark[4] = 1.0

    # -- painting ---------------------------------------------------------
    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect()
        if self._reveal <= 0.001:
            return
        if self._reveal < 0.999:
            painter.setOpacity(self._reveal)
            painter.translate(0.0, (1.0 - self._reveal) * rect.height() * 0.45)
        if not self._level:
            painter.fillRect(rect, QColor(8, 6, 18))
            painter.setPen(QPen(QColor(150, 150, 170, 120)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "the spectrum appears when something is playing")
            return
        self._scene.paint(painter, rect, self._state)


class FullScreenSpectrum(QWidget):
    """The scene on its own, filling the screen, until Escape.

    It borrows the running Spectrum rather than building a second one, so
    there is one analysis, one timer and one set of smoothed values however
    many windows are looking at them. On the way out the widget goes back
    where it came from.
    """

    def __init__(self, spectrum: Spectrum, owner=None) -> None:
        super().__init__(None)
        self.setWindowTitle("Visualiser")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._spectrum = spectrum
        self._owner = owner
        self._home = spectrum.parentWidget()
        self._layout_index = None

        parent_layout = self._home.layout() if self._home else None
        if parent_layout is not None:
            self._layout_index = parent_layout.indexOf(spectrum)

        self._min, self._max = spectrum.minimumHeight(), spectrum.maximumHeight()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        spectrum.setMinimumHeight(0)
        spectrum.setMaximumHeight(16_777_215)
        layout.addWidget(spectrum)

        hint = QLabel("Escape to return")
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("color: rgba(255,255,255,120); padding: 6px;")
        layout.addWidget(hint)

    def keyPressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if event.key() in (Qt.Key.Key_Escape, Qt.Key.Key_F, Qt.Key.Key_Space):
            self.close()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Put the spectrum back exactly where it was."""
        spectrum = self._spectrum
        spectrum.setParent(None)
        spectrum.setMinimumHeight(self._min)
        spectrum.setMaximumHeight(self._max)
        parent_layout = self._home.layout() if self._home else None
        if parent_layout is not None and self._layout_index is not None:
            parent_layout.insertWidget(self._layout_index, spectrum)
        elif self._home is not None:
            spectrum.setParent(self._home)
        spectrum.show()
        if self._owner is not None:
            self._owner._full = None
        super().closeEvent(event)
