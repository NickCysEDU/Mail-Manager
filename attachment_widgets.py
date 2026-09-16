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

from PySide6.QtCore import (QEasingCurve, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter, QPainterPath,
                           QPixmap,
                           QPen, QRadialGradient)
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLayout,
                               QSlider, QStyle, QStyleOptionSlider,
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


def _hz_label(value) -> str:
    """73Hz, 1.4kHz, 22kHz - as the reference meters are labelled."""
    if value >= 1000:
        thousands = value / 1000.0
        text = f"{thousands:.0f}" if thousands == int(thousands) else f"{thousands:.1f}"
        return f"{text}kHz"
    return f"{int(value)}Hz"


class SpectrumState:
    """Everything a scene is handed, and nothing it has to work out."""

    __slots__ = ("levels", "peaks", "bass", "mid", "synth", "high", "hit",
                 "hue", "phase", "scroll", "strobe", "sparks", "labels",
                 "dials", "dial_labels", "dial_colour", "background")

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
        self.dials: List[float] = []
        self.dial_labels: List[str] = []
        #: The reference these are copied from is red on near black.
        self.dial_colour = QColor(226, 62, 48)
        self.background = QColor(6, 4, 6)


class Spectrum(QWidget):
    """The equaliser, and whichever scene is drawing it.

    The numbers are worked out once, before playback. This keeps the smoothed
    state and hands it to a scene; changing theme swaps one object and costs
    nothing. Nothing runs while nothing is playing.
    """

    HEIGHT = 240

    #: Shapes the strip can take, as width-to-height. None keeps the fixed
    #: strip. Portrait is genuinely taller than it is wide, which several
    #: of the scenes suit better than a letterbox.
    SHAPES = (("Strip", None), ("Cinema 21:9", 21 / 9), ("Wide 16:9", 16 / 9),
              ("Square", 1.0), ("Portrait 3:4", 3 / 4))
    #: However tall a shape asks for, never more than this.
    MAX_HEIGHT = 900

    #: Frames up to this many pixels are drawn at their real size. Above
    #: it the scene is drawn into a smaller buffer and stretched, because
    #: antialiased strokes are charged by area and a full screen of them
    #: does not fit in a sixtieth of a second.
    SHARP_PIXELS = 600_000

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
        self._dial_frames: List = []
        self._dial_level: List[float] = []
        #: Which frequency each meter reads. Chosen by the user; starts at
        #: the ten from the photograph the scene was copied from.
        self._dial_centres = None
        self._state = SpectrumState()
        self._scene = visualizers.SCENES[0]
        self._sparks = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(self.SPARKS)]
        self._next_spark = 0
        self._target = 0.0
        self._unbounded = False
        self._reserve = 0
        #: None when idle, else 0..1 while the track is being analysed.
        self._working = None
        self._post = True
        self._effects = PostProcess()
        self._buffer = None
        #: None for the fixed strip, else width-to-height.
        self._aspect = None
        #: The most the strip may take, set by whoever owns the layout.
        #: Without it a tall shape simply demanded its height, the layout
        #: could not fit the transport underneath, and the controls ended
        #: up drawn on top of the scene.
        self._budget = None
        #: Middle of the slider until somebody moves it.
        self._strobe_rate = 0.5
        self._since_hit = 99
        self._timer = QTimer(self)
        # Sixty a second. Every scene paints in well under a frame at
        # 1080p, so the limit is the display rather than the drawing.
        self._timer.setInterval(16)
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

    def set_post(self, on: bool) -> None:
        """Turn the polish pass off, for a slower machine."""
        self._post = bool(on)
        if not on:
            self._buffer = None
        self.update()

    def set_aspect(self, ratio) -> None:
        """Choose the strip's shape, or None to keep the fixed height."""
        self._aspect = None if ratio is None else max(0.2, float(ratio))
        self.updateGeometry()
        if not self._unbounded:
            self._reveal_changed(self._reveal)
        self.update()

    def set_budget(self, pixels) -> None:
        """The most this strip may occupy, whatever shape is chosen."""
        self._budget = None if pixels is None else max(80, int(pixels))
        if not self._unbounded:
            self._reveal_changed(self._reveal)
        self.updateGeometry()

    def _full_height(self) -> int:
        """How tall the strip wants to be when fully revealed.

        Never more than the budget: a shape is a preference, not a claim
        on space the window does not have.
        """
        if self._aspect is None:
            wanted = self.HEIGHT
        else:
            width = self.width() or self.sizeHint().width() or 420
            wanted = max(120, min(self.MAX_HEIGHT, int(width / self._aspect)))
        if self._budget is not None:
            wanted = min(wanted, self._budget)
        return max(60, wanted)

    def set_strobe_rate(self, rate: float) -> None:
        """How willing the strobe is to fire, 0 sparing to 1 eager.

        Two things move together: how big a jump in the bass counts as a
        hit, and how long the scene waits before it will call another one.
        Driving only the threshold makes a busy track strobe continuously
        at one end of the slider and never at the other.
        """
        self._strobe_rate = max(0.0, min(1.0, float(rate)))

    def set_labels(self, labels) -> None:
        self._state.labels = list(labels or [])

    def dial_centres(self):
        """The frequency each meter is reading, in Hz."""
        import attachment_audio

        return tuple(self._dial_centres or attachment_audio.DIAL_CENTRES)

    def set_dial_centres(self, centres) -> None:
        """Point the meters at different frequencies.

        The analysis is not redone: regroup re-reads the frames that are
        already in memory against whatever centres are asked for, so this
        is immediate however long the track is.
        """
        import attachment_audio

        cleaned = []
        for value in centres:
            try:
                hertz = int(value)
            except (TypeError, ValueError):
                continue
            # Below 20 Hz nobody hears it, and above the Nyquist limit of
            # the decode there is nothing in the signal to read.
            top = attachment_audio.DECODE_RATE // 2
            cleaned.append(max(20, min(top, hertz)))
        if not cleaned:
            return
        self._dial_centres = tuple(cleaned)
        self._rebuild_dials()
        self.update()

    def _rebuild_dials(self) -> None:
        import attachment_audio

        centres = self._dial_centres or attachment_audio.DIAL_CENTRES
        self._dial_frames = attachment_audio.regroup(self._frames, centres)
        self._dial_level = [0.0] * len(centres)
        self._state.dial_labels = [_hz_label(c) for c in centres]

    def set_colours(self, dial=None, background=None) -> None:
        if dial is not None:
            self._state.dial_colour = QColor(dial)
        if background is not None:
            self._state.background = QColor(background)
        self.update()

    @property
    def colours(self):
        return self._state.dial_colour, self._state.background

    def set_frames(self, frames: List, rate: int) -> None:
        """The analysis, which lands a moment after playback starts."""
        import attachment_audio

        self._frames = frames or []
        self._rate = max(1, rate)
        width = len(self._frames[0]) if self._frames else 0
        self._level = [0.0] * width
        self._peak = [0.0] * width
        # The dial scene wants ten named bands rather than the twenty-seven
        # the equaliser uses, so they are read out of the same frames once.
        self._rebuild_dials()
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
        self._target = 0.0
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
        if self._reveal >= 1.0 and self.maximumHeight() >= self._full_height():
            return
        self._animate_to(1.0)

    def conceal(self) -> None:
        self._away.stop()
        self._animate_to(0.0)

    def _animate_to(self, target: float) -> None:
        self._target = float(target)
        self._flow.stop()
        self._flow.setStartValue(float(self._reveal))
        self._flow.setEndValue(float(target))
        self._flow.start()

    def set_working(self, fraction) -> None:
        """Show that analysis is running, and roughly how far along.

        Analysis takes a few seconds on a long track. Without this the
        strip is blank for all of it, which reads as nothing happening -
        or, when it ran on the UI thread, as the app having died.
        """
        self._working = None
        self._post = True
        self._effects = PostProcess()
        self._buffer = None
        #: None for the fixed strip, else width-to-height.
        self._aspect = None
        #: The most the strip may take, set by whoever owns the layout.
        #: Without it a tall shape simply demanded its height, the layout
        #: could not fit the transport underneath, and the controls ended
        #: up drawn on top of the scene.
        self._budget = None
        #: Middle of the slider until somebody moves it.
        self._strobe_rate = 0.5
        self._since_hit = 99 if fraction is None else max(0.0, min(1.0, float(fraction)))
        if self._working is not None:
            self.reveal()
            if not self._timer.isActive():
                self._timer.start()
        self.update()

    def set_reserve(self, pixels: int) -> None:
        """Leave this many pixels clear at the bottom of the scene."""
        self._reserve = max(0, int(pixels))
        self.update()

    def set_unbounded(self, free: bool) -> None:
        """Stop holding the widget to its strip height.

        In the pane the scene is a 240px band and the reveal animation
        drives that height. Full screen wants the whole window, so the
        clamps come off - without this the animation kept reapplying them
        and the scene sat as a band across the middle of the screen.
        """
        self._unbounded = bool(free)
        if free:
            self.setMinimumHeight(0)
            self.setMaximumHeight(16_777_215)
        else:
            self._reveal_changed(self._reveal)
        self.updateGeometry()

    def _reveal_changed(self, value) -> None:
        self._reveal = max(0.0, min(1.0, float(value)))
        height = int(self._full_height() * self._reveal)
        if self._unbounded:
            if self._reveal <= 0.001 and self._target <= 0.0:
                self._timer.stop()
                self._idling = False
            self.update()
            return
        # Minimum, maximum and both hints together. A maximum on its own
        # leaves the minimum at zero and the hint at -1, so a layout hands
        # out whatever is spare - which in a full pane is nothing.
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        # Only a slide that is heading for zero means "gone". The first
        # frame of a slide *away* from zero also reports about zero, and
        # stopping on that killed the scene every time it opened - which
        # looked like a visualiser that would not come back after a hide.
        if self._reveal <= 0.001 and self._target <= 0.0:
            self._timer.stop()
            self._idling = False
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._aspect is not None and not self._unbounded:
            wanted = int(self._full_height() * self._reveal)
            if abs(self.maximumHeight() - wanted) > 1:
                self.setMinimumHeight(wanted)
                self.setMaximumHeight(wanted)
                self.updateGeometry()

    def sizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        if self._unbounded:
            return QSize(1280, 720)
        return QSize(420, int(self._full_height() * self._reveal))

    def minimumSizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        if self._unbounded:
            return QSize(0, 0)
        return QSize(0, int(self._full_height() * self._reveal))

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
        rate = self._strobe_rate
        jump = 0.22 - rate * 0.17          # 0.22 sparing, 0.05 eager
        wait = int(34 - rate * 32)         # frames to wait before the next
        self._since_hit += 1
        if bass - self._last_bass > jump and self._since_hit >= wait:
            state.hit = 1.0
            self._since_hit = 0
        self._last_bass = bass

        state.scroll = (state.scroll + 0.012 + state.bass * 0.05) % 1.0
        state.phase += 0.0045
        state.hue = (state.phase * 0.5) % 1.0
        state.levels = self._level
        state.peaks = self._peak
        state.sparks = self._sparks

        if self._dial_frames and not self._idling:
            exact = self._position / 1000.0 * self._rate
            index = min(len(self._dial_frames) - 1, max(0, int(exact)))
            wanted = self._dial_frames[index]
            for i, value in enumerate(wanted):
                if i >= len(self._dial_level):
                    break
                current = self._dial_level[i]
                # A moving coil has mass: quick to rise, slow to fall back.
                self._dial_level[i] = (value if value > current
                                       else current * 0.86 + value * 0.14)
        elif self._idling and self._dial_level:
            for i in range(len(self._dial_level)):
                self._dial_level[i] = (0.10 + 0.08 * _math.sin(
                    self._drift * 1.6 + i * 0.6)) 
        state.dials = self._dial_level

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
        """Paint, and never leave the painter open.

        An exception raised out of a paintEvent does not propagate: Qt
        catches it, prints it, and carries on with a painter still active
        on the backing store, which then crashes the process. Whatever
        goes wrong in a scene, the painter has to be closed.
        """
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        if self._reserve:
            # The strip is part of the picture, not a gap in it, so it takes
            # the scene's own background - including a picked one.
            painter.fillRect(rect, self._state.background)
            # Kept clear for the floating control bar. Reserving the strip
            # permanently rather than while the bar shows means the scene
            # never has a button sitting on top of it, and never resizes
            # underneath the viewer when the bar fades.
            rect = rect.adjusted(0.0, 0.0, 0.0,
                                 -min(float(self._reserve), rect.height() / 3.0))
        if self._reveal <= 0.001:
            return
        if self._reveal < 0.999:
            painter.setOpacity(self._reveal)
            painter.translate(0.0, (1.0 - self._reveal) * rect.height() * 0.45)
        if not self._level:
            painter.fillRect(rect, QColor(8, 6, 18))
            if self._working is not None:
                self._draw_working(painter, rect)
                return
            painter.setPen(QPen(QColor(150, 150, 170, 120)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "the spectrum appears when something is playing")
            return
        self._paint_scene(painter, rect)

    def _paint_scene(self, painter, rect) -> None:
        """The scene, then whatever polish it asks for.

        A scene that wants no post-processing is drawn straight onto the
        widget, exactly as before - the buffer and the extra passes only
        exist for the ones that do.
        """
        import visualizers

        recipe = visualizers.post_for(self._scene) if self._post else {}
        ratio = self.devicePixelRatioF()
        pixels = rect.width() * ratio * rect.height() * ratio
        # Antialiasing is what these scenes cost, and it is charged per
        # pixel of every stroke: Ambience measured 10.3 ms a frame at 1080p
        # with it on and 1.6 ms with it off. Rather than give it up and
        # draw jagged curves, big frames are drawn smaller and stretched,
        # which costs the same as turning it off and still looks smooth.
        # Floored at a fifth rather than a half: on a retina screen the
        # physical frame is four times the logical one, and a half-scale
        # floor left the buffer at nearly three times the target however
        # low the target was set.
        shrink = 1.0 if pixels <= self.SHARP_PIXELS else max(
            0.20, (self.SHARP_PIXELS / pixels) ** 0.5)
        if (not recipe and shrink >= 0.999) or rect.width() < 8.0 or rect.height() < 8.0:
            self._scene.paint(painter, rect, self._state)
            return
        wanted = QSize(max(1, int(rect.width() * ratio * shrink)),
                       max(1, int(rect.height() * ratio * shrink)))
        if self._buffer is None or self._buffer.size() != wanted:
            self._buffer = QPixmap(wanted)
            self._buffer.setDevicePixelRatio(ratio * shrink)
        self._buffer.fill(QColor(0, 0, 0, 0))
        inner = QPainter(self._buffer)
        inner.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        side = self._buffer.size() / self._buffer.devicePixelRatio()
        self._scene.paint(inner, QRectF(0, 0, side.width(), side.height()),
                          self._state)
        inner.end()
        if recipe:
            self._effects.apply(painter, rect, self._buffer, recipe)
        else:
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawPixmap(QRectF(rect), self._buffer,
                               QRectF(self._buffer.rect()))

    def _draw_working(self, painter, rect) -> None:
        """A bar that fills, and a line saying what is happening."""
        painter.setPen(QPen(QColor(150, 150, 170, 150)))
        painter.drawText(rect.adjusted(0, 0, 0, -int(rect.height() * 0.18)),
                         Qt.AlignmentFlag.AlignCenter,
                         "listening to the track…")
        width = min(rect.width() * 0.5, 320.0)
        track = QRectF(rect.center().x() - width / 2,
                       rect.center().y() + rect.height() * 0.14,
                       width, 5.0)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(90, 90, 110, 110))
        painter.drawRoundedRect(track, 2.5, 2.5)
        filled = QRectF(track)
        filled.setWidth(max(4.0, track.width() * float(self._working or 0.0)))
        painter.setBrush(QColor(150, 190, 255, 210))
        painter.drawRoundedRect(filled, 2.5, 2.5)
        painter.setBrush(Qt.BrushStyle.NoBrush)


class FullScreenSpectrum(QWidget):
    """The scene alone, filling the screen, with controls that get out of it.

    It borrows the running Spectrum rather than building a second one, so
    there is one analysis, one timer and one set of smoothed values however
    many windows are looking. On the way out the widget goes back where it
    came from.

    The controls float on top and fade after a few seconds of stillness.
    Moving the mouse brings them back, and they stay while the pointer is on
    them - otherwise reaching for the volume makes them vanish under it.
    """

    #: Stillness before the controls go, and before the pointer does.
    IDLE_MS = 2600

    def __init__(self, spectrum: Spectrum, owner=None) -> None:
        super().__init__(None)
        self.setWindowTitle("Visualiser")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
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
        spectrum.set_unbounded(True)
        spectrum.setParent(self)
        layout.addWidget(spectrum)

        self.bar = QWidget(self)
        self.bar.setMouseTracking(True)
        self.bar.setStyleSheet(
            "background: rgba(12,10,18,215); border-radius: 10px;")
        # The same wrapping row the window uses. A fixed line squeezed its
        # controls into nothing on a small screen rather than taking a
        # second line.
        self._bar_layout = FlowRow(spacing=10)
        self._bar_layout.setContentsMargins(14, 10, 14, 10)
        self.bar.setLayout(self._bar_layout)

        self._fade = QVariantAnimation(self)
        self._fade.setDuration(320)
        self._fade.valueChanged.connect(self._set_bar_opacity)
        self._effect = QGraphicsOpacityEffect(self.bar)
        self._effect.setOpacity(1.0)
        self.bar.setGraphicsEffect(self._effect)

        self._idle = QTimer(self)
        self._idle.setSingleShot(True)
        self._idle.setInterval(self.IDLE_MS)
        self._idle.timeout.connect(self._hide_controls)
        self._idle.start()

    # -- what goes in the bar ---------------------------------------------
    def add_control(self, widget, stretch: int = 0) -> None:
        widget.setMouseTracking(True)
        self._bar_layout.addWidget(widget)
        if stretch:
            self._bar_layout.set_stretch(widget)

    def add_stretch(self) -> None:
        self._bar_layout.addStretch(1)

    # -- showing and hiding ------------------------------------------------
    def _set_bar_opacity(self, value) -> None:
        self._effect.setOpacity(max(0.0, min(1.0, float(value))))
        self.bar.setVisible(self._effect.opacity() > 0.01)

    def _show_controls(self) -> None:
        if self._effect.opacity() < 0.99:
            self._fade.stop()
            self._fade.setStartValue(self._effect.opacity())
            self._fade.setEndValue(1.0)
            self._fade.start()
        self.unsetCursor()
        self._idle.start()

    def _hide_controls(self) -> None:
        if self.bar.underMouse():
            self._idle.start()
            return
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        self.setCursor(Qt.CursorShape.BlankCursor)

    def keyPressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Escape leaves; J, K and L work the transport.

        One method, deliberately. There were two, and the later one won,
        so the transport keys were dead the whole time - pressing them
        did nothing but wake the control bar.
        """
        self._show_controls()
        keys = {Qt.Key.Key_J: "back", Qt.Key.Key_K: "toggle",
                Qt.Key.Key_L: "forward", Qt.Key.Key_Space: "toggle"}
        action = keys.get(event.key())
        if action is not None:
            handler = getattr(self._owner, "transport", None)
            if handler is not None:
                handler(action)
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def mouseMoveEvent(self, event) -> None:      # noqa: N802 - Qt's name
        self._show_controls()
        super().mouseMoveEvent(event)

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._place_bar()

    def showEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().showEvent(event)
        self._place_bar()

    def _place_bar(self) -> None:
        # Wide enough that the seek bar is obviously the long one and the
        # volume slider obviously the short one, but never wider than the
        # screen it has to sit on.
        width = max(320, min(int(self.width() * 0.86), self.width() - 48))
        margins = self._bar_layout.contentsMargins()
        rows = self._bar_layout.heightForWidth(
            width - margins.left() - margins.right())
        height = max(48, rows + margins.top() + margins.bottom())
        self.bar.setGeometry(int((self.width() - width) / 2),
                             int(self.height() - height - 28), width, height)
        self.bar.raise_()
        # Exactly what the bar occupies, so the scene stops above it rather
        # than being drawn underneath and reading as a cut-off control.
        self._spectrum.set_reserve(height + 44)

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Put the spectrum back exactly where it was."""
        self._idle.stop()
        self._fade.stop()
        spectrum = self._spectrum
        spectrum.setParent(None)
        spectrum.set_reserve(0)
        spectrum.set_unbounded(False)
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


class FlowRow(QLayout):
    """A row of controls that wraps instead of running off the edge.

    The visualiser controls grow and shrink with what is selected - the
    colour button only exists for the meters - and a plain QHBoxLayout
    keeps laying them out in one line however narrow the pane gets, so
    they overlap each other and then leave the window. This puts what
    fits on a line and moves the rest down.
    """

    def __init__(self, parent=None, spacing: int = 10) -> None:
        super().__init__(parent)
        self._items: list = []
        self._gaps: dict = {}
        self._stretch = None
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    # -- the bits QLayout insists on ---------------------------------------
    def addItem(self, item) -> None:      # noqa: N802 - Qt's name
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):      # noqa: N802 - Qt's name
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):      # noqa: N802 - Qt's name
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):      # noqa: N802 - Qt's name
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:      # noqa: N802 - Qt's name
        return True

    def heightForWidth(self, width: int) -> int:      # noqa: N802 - Qt's name
        return self._lay(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect) -> None:      # noqa: N802 - Qt's name
        super().setGeometry(rect)
        self._lay(rect, apply=True)

    def sizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        return self.minimumSize()

    def minimumSize(self) -> QSize:      # noqa: N802 - Qt's name
        size = QSize(0, 0)
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def set_stretch(self, widget) -> None:
        """Let this one widget take whatever width is spare on its line."""
        self._stretch = widget

    def add_gap(self, pixels: int) -> None:
        """A wider space, to separate one group of controls from the next."""
        self._gaps[len(self._items)] = int(pixels)

    # -- the actual placing ------------------------------------------------
    def _rows(self, rect):
        """Split the items into lines that fit, keeping each one's size."""
        rows, current, x, tallest = [], [], rect.x(), 0
        for index, item in enumerate(self._items):
            widget = item.widget()
            if widget is not None and widget.isHidden():
                continue
            wanted = item.sizeHint()
            lead = self._gaps.get(index, 0) if current else 0
            if current and x + lead + wanted.width() > rect.right():
                rows.append((current, tallest))
                current, x, tallest = [], rect.x(), 0
                lead = 0
            x += lead
            current.append((item, QRect(QPoint(x, 0), wanted)))
            x += wanted.width() + self._gap
            tallest = max(tallest, wanted.height())
        if current:
            rows.append((current, tallest))
        if self._stretch is not None:
            self._widen(rows, rect)
        return rows

    def _widen(self, rows, rect) -> None:
        """Give the stretchy widget the room its line has left over."""
        for row, _tallest in rows:
            for index, (item, box) in enumerate(row):
                if item.widget() is not self._stretch:
                    continue
                used = sum(other.width() for _, other in row)
                spare = rect.width() - used - self._gap * (len(row) - 1)
                if spare > 0:
                    grown = QRect(box)
                    grown.setWidth(box.width() + spare)
                    row[index] = (item, grown)
                    for after in range(index + 1, len(row)):
                        later_item, later_box = row[after]
                        moved = QRect(later_box)
                        moved.moveLeft(later_box.left() + spare)
                        row[after] = (later_item, moved)
                return

    def _lay(self, rect, apply: bool) -> int:
        y = rect.y()
        for index, (row, tallest) in enumerate(self._rows(rect)):
            if index:
                y += self._gap
            for item, box in row:
                if apply:
                    # Centred on the line rather than hung from the top: a
                    # combo box is taller than a tick box, and left flush
                    # they read as two rows of controls rather than one.
                    placed = QRect(box)
                    placed.moveTop(y + (tallest - box.height()) // 2)
                    item.setGeometry(placed)
            y += tallest
        return y - rect.y()


class PostProcess:
    """Cheap screen-space polish applied after a scene has drawn itself.

    No shaders are available here, so each effect is something Qt can do
    quickly and the expensive one - bloom - is done at a fraction of the
    resolution and scaled back up, which is what a blur is anyway. The
    overlays that never change are drawn once into tiles and repeated.

    Everything is optional per scene, and the whole pass is skipped when a
    scene asks for nothing, so the fast path stays exactly as fast.
    """

    #: Bloom is computed at this fraction of the frame. Small enough that
    #: the cost barely moves between a strip and a full screen.
    BLOOM_DIVISOR = 8
    #: Never build a bloom buffer smaller than this.
    BLOOM_MIN = 32

    #: Effects in the order they are given up when there is not time for
    #: them. Bloom and the vignette carry most of the look, so they go last.
    ORDER = ("aberration", "grain", "scanlines", "bloom", "vignette")
    #: The pass may have this long. The rest of the frame needs the other
    #: ten milliseconds of a sixty-a-second budget.
    BUDGET_MS = 6.5

    def __init__(self) -> None:
        self._lines: dict = {}
        self._grain: dict = {}
        self._cost = 0.0
        self._allow = len(self.ORDER)
        self._area = 0.0
        #: Frames to leave alone after a change, so a decision is given a
        #: chance to show its effect before the next one is made.
        self._settle = 0

    def _permitted(self, recipe: dict) -> dict:
        """The recipe minus whatever there is no time for.

        Measured rather than guessed from the pixel count: the same frame
        costs very different amounts on different machines, and a rule
        written against this one would be wrong on any other.
        """
        if self._allow >= len(self.ORDER):
            return recipe
        dropped = set(self.ORDER[:len(self.ORDER) - self._allow])
        return {k: v for k, v in recipe.items() if k not in dropped}

    def _record(self, taken_ms: float) -> None:
        self._cost = self._cost * 0.8 + taken_ms * 0.2
        if self._settle > 0:
            self._settle -= 1
            return
        if self._cost > self.BUDGET_MS and self._allow > 1:
            self._allow -= 1
            # Seeded at the budget rather than zero. Zeroing it made the
            # next frame look instantly cheap, which put the effect
            # straight back and left the whole thing oscillating between
            # four and five effects for ever.
            self._cost = self.BUDGET_MS
            self._settle = 30
        elif (self._cost < self.BUDGET_MS * 0.45
              and self._allow < len(self.ORDER)):
            self._allow += 1
            self._cost = self.BUDGET_MS
            self._settle = 120

    def apply(self, painter, rect, frame, recipe: dict) -> None:
        """Draw ``frame`` into ``painter`` with ``recipe`` applied.

        Every pass runs on the buffer, at the buffer's own size, and the
        result is stretched to the frame once at the end. Doing it the
        other way round - stretching first, then shading the full output -
        charged every pass for the whole screen: six milliseconds a frame
        at 1080p on a retina display, against a budget of sixteen for
        everything.
        """
        import time as _time

        started = _time.perf_counter()
        area = rect.width() * rect.height()
        if area > self._area * 1.3 or area < self._area * 0.7:
            self._area = area
            self._allow = (5 if area <= 500_000 else
                           4 if area <= 1_200_000 else
                           3 if area <= 2_400_000 else 2)
            self._cost = self.BUDGET_MS * 0.7
            self._settle = 8
        recipe = self._permitted(recipe)

        inner = QPainter(frame)
        inner.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = frame.devicePixelRatio() or 1.0
        box = QRectF(0.0, 0.0, frame.width() / scale, frame.height() / scale)
        try:
            bloom = float(recipe.get("bloom", 0.0))
            shift = float(recipe.get("aberration", 0.0))
            if bloom > 0.01 or shift > 0.05:
                halo = self._halo(box, frame)
                if bloom > 0.01:
                    self._bloom(inner, box, halo, bloom)
                if shift > 0.05:
                    # In buffer pixels, so the effect looks the same
                    # whatever the buffer was scaled to.
                    self._aberration(inner, box, halo,
                                     shift * box.width() / max(1.0, rect.width()))
            lines = float(recipe.get("scanlines", 0.0))
            if lines > 0.01:
                self._scanlines(inner, box, lines)
            grain = float(recipe.get("grain", 0.0))
            if grain > 0.01:
                self._noise(inner, box, grain)
            fade = float(recipe.get("vignette", 0.0))
            if fade > 0.01:
                self._vignette_over(inner, box, fade)
        finally:
            inner.end()

        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(rect), frame, QRectF(frame.rect()))
        self._record((_time.perf_counter() - started) * 1000.0)

    # -- the expensive one, kept cheap -------------------------------------
    def _halo(self, rect, frame):
        """A small, blurred copy of the frame.

        Small is the whole trick: the blur is the downscale, and every
        later pass reads this instead of the full frame, so the cost barely
        moves between a strip and a full screen.
        """
        small = QSize(max(self.BLOOM_MIN, int(rect.width() / self.BLOOM_DIVISOR)),
                      max(self.BLOOM_MIN, int(rect.height() / self.BLOOM_DIVISOR)))
        return frame.scaled(small, Qt.AspectRatioMode.IgnoreAspectRatio,
                            Qt.TransformationMode.SmoothTransformation)

    def _bloom(self, painter, rect, halo, amount: float) -> None:
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setOpacity(min(0.85, amount))
        # Scaled during the blit. Building a full-size blurred copy first
        # cost thirty milliseconds a frame at full screen, which is most of
        # the frame gone for something nobody can see the edges of anyway.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(rect), halo, QRectF(halo.rect()))
        painter.restore()

    def _aberration(self, painter, rect, halo, shift: float) -> None:
        """Red and blue pulled apart, the way a cheap lens does it."""
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        painter.setOpacity(0.16)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        source = QRectF(halo.rect())
        target = QRectF(rect)
        painter.drawPixmap(target.translated(shift, 0.0), halo, source)
        painter.drawPixmap(target.translated(-shift, 0.0), halo, source)
        painter.restore()

    # -- the cached overlays -----------------------------------------------
    def _scanlines(self, painter, rect, amount: float) -> None:
        key = int(amount * 100)
        tile = self._lines.get(key)
        if tile is None:
            tile = QPixmap(4, 4)
            tile.fill(QColor(0, 0, 0, 0))
            inner = QPainter(tile)
            inner.fillRect(0, 0, 4, 2, QColor(0, 0, 0, int(150 * amount)))
            inner.end()
            self._lines[key] = tile
        painter.drawTiledPixmap(rect, tile)

    def _noise(self, painter, rect, amount: float) -> None:
        key = int(amount * 100)
        tile = self._grain.get(key)
        if tile is None:
            import random

            side = 64
            image = QImage(side, side, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(0)
            spots = random.Random(11)
            strength = int(90 * amount)
            for y in range(side):
                for x in range(side):
                    value = spots.randint(0, strength)
                    image.setPixelColor(x, y, QColor(255, 255, 255, value))
            tile = QPixmap.fromImage(image)
            self._grain[key] = tile
        painter.save()
        painter.setOpacity(0.5)
        painter.drawTiledPixmap(rect, tile)
        painter.restore()

    def _vignette_over(self, painter, rect, amount: float) -> None:
        shade = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.72)
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(0.65, QColor(0, 0, 0, int(30 * amount)))
        shade.setColorAt(1.0, QColor(0, 0, 0, int(230 * amount)))
        painter.fillRect(rect, shade)
