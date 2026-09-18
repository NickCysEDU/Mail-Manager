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
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter,
                           QPainterPath,
                           QPen, QRadialGradient)


class Scene:
    """One way of drawing the music."""

    name = "scene"
    blurb = "a scene"

    #: How many pixels this scene can afford to draw at their real size.
    #: Zero means "whatever the pane's own floor is". A scene raises it
    #: when most of its frame is a blit rather than a stroke, because the
    #: pane's floor is set for the ones that stroke curves and applying it
    #: to the others softens them for nothing.
    sharp_pixels = 0

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


class Plasma:
    """The morphing coloured field the old media player drew behind things.

    Sums of sines over a coarse grid, stretched up smooth. At full size
    this would be millions of evaluations a frame; at 44 by 26 it is about
    a thousand, and stretched with a smooth transform nobody can tell -
    the thing being drawn has no hard edges in it anywhere.
    """

    COLUMNS = 36
    ROWS = 22
    #: Frames between recomputes. The field morphs over seconds, so
    #: redrawing it thirty times a second rather than sixty is not
    #: something anybody can see, and it is half the arithmetic.
    EVERY = 2

    def __init__(self) -> None:
        self._image = None
        self._countdown = 0
        self._drift_a = 0.0
        self._drift_b = 0.0
        self._drift_c = 0.0

    def paint(self, painter, rect, state, strength: float = 1.0) -> None:
        if rect.width() < 4 or rect.height() < 4:
            return
        if self._image is None:
            self._image = QImage(self.COLUMNS, self.ROWS,
                                 QImage.Format.Format_RGB32)
            self._countdown = 0
        self._countdown -= 1
        if self._countdown > 0:
            painter.setRenderHint(
                QPainter.RenderHint.SmoothPixmapTransform, True)
            painter.drawImage(rect, self._image)
            return
        self._countdown = self.EVERY
        # The field used to slide one way at one speed. Three clocks
        # running at different rates, each turning a wave in a different
        # direction, make it fold and drift instead - and the music drives
        # both how fast they run and how deep the folds are, so a loud
        # passage churns and a quiet one barely moves.
        pace = 0.55 + state.bass * 1.9 + state.mid * 0.8
        self._drift_a += 0.016 * pace
        self._drift_b -= 0.011 * pace + state.high * 0.02
        self._drift_c += 0.007 * pace
        swell = 0.55 + state.bass * 0.8 + self.flash_of(state) * 0.9
        hue_shift = state.hue
        image = self._image
        for row in range(self.ROWS):
            y = row / self.ROWS
            for column in range(self.COLUMNS):
                x = column / self.COLUMNS
                # Three waves at angles to each other, which is what makes
                # the field fold through itself instead of scrolling.
                # One wave across, one down, one diagonal - each on its
                # own clock, so no single direction dominates.
                value = (math.sin((x * 3.1 + self._drift_a) * math.pi)
                         + math.sin((y * 2.7 + self._drift_b) * math.pi)
                         + math.sin(((x - y) * 2.3 + self._drift_c) * math.pi))
                shade = (value / 6.0 + 0.5 + hue_shift) % 1.0
                level = 0.10 + 0.5 * swell * (0.5 + value / 6.0)
                image.setPixelColor(column, row, QColor.fromHsvF(
                    shade, 0.85, max(0.0, min(1.0, level * strength))))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, image)

    @staticmethod
    def flash_of(state) -> float:
        return state.hit if state.strobe else 0.0


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
        radius = horizon * (0.46 + state.bass * 0.26 + flash * 0.85)
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

        # A hit no longer shoves every ring down the corridor at once -
        # that made the whole field jump and read as a glitch. It fires a
        # shockwave instead: one bright ring thrown outwards, drawn after
        # the corridor.
        flash = self.flash(state)
        rush = 0.0
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
        self._shockwave(painter, centre, width, height, flash)
        self._sparks(painter, state)

    def _shockwave(self, painter, centre, width, height, flash) -> None:
        """One ring thrown out of the middle on a hit, fading as it goes."""
        if flash <= 0.02:
            return
        # Newest hits are small and bright; as the flash decays the ring
        # is further out and fainter, which reads as one thing travelling.
        travel = 1.0 - flash
        radius = 20.0 + travel * max(width, height) * 0.75
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step, (width_scale, alpha) in enumerate(((3.0, 0.9), (7.0, 0.35))):
            painter.setPen(QPen(
                QColor.fromHsvF(0.12, 0.25, 1.0, alpha * flash),
                width_scale * (0.4 + flash)))
            painter.drawEllipse(centre, radius + step * 4, radius + step * 4)

    def _spokes(self, painter, centre, reach, state) -> None:
        """The bands as rays out of the middle, not a row along the bottom.

        A bar graph at the foot of this scene fought with the perspective;
        spokes belong to it, and they read as the same numbers.
        """
        levels = state.levels
        count = len(levels)
        if not count:
            return
        # The spokes do not take the strobe. They are the bars, and flashing
        # them at the same moment as the rings and the shockwave made three
        # things move on one beat, which reads as a mess rather than a hit.
        flash = 0.0
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

    The persistence is the point, and it is done the way the tube does it
    rather than the way a drawing program would. There is a screen - an
    image that survives between frames - and each frame dims what is
    already on it and lays one new trace over the top. How fast it dims is
    the decay control. Short reads like a modern digital scope; long smears
    several cycles together and shows how a sound moves.

    The first version of this kept a list of old traces and redrew all of
    them every frame, fainter each time. That is a picture of persistence
    rather than persistence, and it cost what it looked like it cost: at
    the top of the decay slider, ninety antialiased thousand-point paths a
    frame, which was around 120ms - eight frames a second on a machine
    asked for sixty. A screen that fades costs one path a frame at any
    decay setting, which is why the slider is now free to go anywhere.
    """

    name = "Oscilloscope"
    blurb = "the waveform swept round a circle, on a phosphor you can set"

    #: How faint a trace is when its decay time is up. Not zero: the decay
    #: is exponential, like a phosphor's, so "gone" has to be a number.
    FADED = 0.02
    #: The longest step the fade will take in one go. Coming back from a
    #: paused window or a stalled frame, the real gap can be seconds, and
    #: fading by seconds in one step wipes the screen with a visible jolt.
    MAX_STEP = 0.25
    #: Seconds of persistence at each end of the slider.
    MIN_DECAY = 0.03
    MAX_DECAY = 1.50

    #: How the beam is driven. Sweep is a clock going round once a frame;
    #: X-Y drives it from the two channels at once, which is what a record
    #: written for a scope expects and what draws the picture in it.
    MODES = ("Sweep", "X-Y")

    #: How far the trace swings either side of the zero ring, as a share
    #: of that ring's radius. Fixed, so the shape of a trace does not
    #: depend on anything that changes between frames - which is what
    #: makes a built path worth keeping.
    SWING = 0.42
    #: How much the strobe pumps the gain. The whole figure grows, the way
    #: a scope's does when you turn the volts per division down, rather
    #: than only the peaks moving - and a uniform scale is a transform, so
    #: it costs nothing and does not invalidate a cached path.
    FLASH_GAIN = 0.03

    def __init__(self) -> None:
        self._decay = 0.28
        self._mode = "Sweep"
        self._plasma = Plasma()
        #: The screen itself: what the beam has drawn and not yet lost.
        self._screen = None
        #: When it was last dimmed, so the decay is in seconds rather than
        #: in frames - the same slider then means the same thing whether
        #: the window is managing sixty a second or fifteen.
        self._last = None
        #: The last trace burned in. A paused track hands the same one
        #: back every frame, and drawing it again would pile brightness on
        #: brightness until the screen was a solid disc.
        self._burned = None

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in self.MODES:
            self._mode = mode

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
        # A dim field behind the graticule, so the screen looks lit from
        # within rather than painted on black. Kept faint and tinted
        # towards the phosphor, because the trace is the subject.
        painter.save()
        painter.setOpacity(0.32 + flash * 0.25)
        self._plasma.paint(painter, rect, state, strength=0.45)
        painter.restore()
        self._grid(painter, rect, flash)

        vector = getattr(state, "vector", None)
        drawing = self._mode == "X-Y" and vector is not None
        if drawing:
            trace = vector
        else:
            trace = getattr(state, "trace", None)
            if trace is None:
                trace = self._from_levels(state)
        if trace is None:
            return

        # How many real pixels one unit of this rect is worth. The pane
        # draws big frames into a smaller buffer and stretches them, so
        # the rect a scene is handed is in logical units that can be
        # nearly twice the pixels underneath. Sizing the tube from the
        # rect alone built a 1920-wide screen to be squeezed into a
        # 1030-wide buffer, which cost the full frame and then threw half
        # of it away - and was most of what this scene cost.
        dpr = abs(painter.combinedTransform().m11()) or 1.0
        screen = self._tube(rect, trace, drawing, flash, dpr)
        if screen is not None:
            painter.drawImage(rect, screen, QRectF(screen.rect()))

    # -- the tube ---------------------------------------------------------
    def _tube(self, rect, trace, drawing: bool, flash: float, dpr: float = 1.0):
        """Dim what is on the screen, lay the new trace over it, hand it back.

        Everything that makes this cheap is here. The screen is one image
        that outlives the frame, so however long the phosphor is set to
        glow, a frame is one fade and one path - not one path per frame of
        history. The fade is a ``DestinationIn`` fill, which multiplies
        what is already there by an alpha and touches nothing else, so the
        graticule and the field behind it stay crisp: they are drawn live,
        underneath, and never go into the tube at all.
        """
        size = QSize(max(0, int(rect.width() * dpr)),
                     max(0, int(rect.height() * dpr)))
        if size.width() < 2 or size.height() < 2:
            return None
        screen = self._fit(size)

        now = time.monotonic()
        step = self.MAX_STEP if self._last is None else min(
            self.MAX_STEP, max(0.0, now - self._last))
        self._last = now

        # A paused track hands back the trace it handed back last frame.
        # Neither fading nor redrawing it is right - the picture should
        # simply sit there - so a repeat is left alone entirely.
        if trace is self._burned:
            return screen
        self._burned = trace

        beam = QPainter(screen)
        try:
            beam.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn)
            # How much survives this step. Exponential, so the trace is
            # down to FADED of its brightness after `decay` seconds
            # whatever the frame rate happens to be.
            keep = self.FADED ** (step / max(1e-3, self._decay))
            beam.fillRect(screen.rect(),
                          QColor(0, 0, 0, max(0, min(255, int(keep * 255)))))
            beam.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_SourceOver)
            beam.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            beam.setBrush(Qt.BrushStyle.NoBrush)
            self._strike(beam, screen, trace, drawing, flash, dpr)
        finally:
            beam.end()
        return screen

    def _fit(self, size):
        """The screen at this size, keeping what was on the old one.

        Scaled rather than cleared. Dragging a window edge is a stream of
        sizes, and starting from black on every one of them means the
        trace disappears for as long as the drag lasts.
        """
        screen = self._screen
        if screen is not None and screen.size() == size:
            return screen
        fresh = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        fresh.fill(Qt.GlobalColor.transparent)
        if screen is not None and not screen.isNull():
            copier = QPainter(fresh)
            try:
                copier.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                                     True)
                copier.drawImage(QRectF(fresh.rect()), screen,
                                 QRectF(screen.rect()))
            finally:
                copier.end()
        self._screen = fresh
        return fresh

    def _strike(self, beam, screen, trace, drawing: bool, flash: float,
                dpr: float = 1.0) -> None:
        """One pass of the beam: one stroke, and the bloom makes it glow.

        The glow used to be three strokes - a wide translucent green under
        a narrower one under a hot core - which is a reasonable way to draw
        a lit phosphor and a bad way to pay for one. A real trace is not a
        smooth curve: a thousand consecutive samples of music reverse
        direction constantly, and a wide round-joined pen over a thousand
        reversals costs four times what the same pen costs over a smooth
        line. Measured at the size the pane actually draws, the three
        strokes were 21ms of a 16ms frame, and the widest of them was half
        of that on its own.

        So the beam is struck once, hot, and the scene's bloom pass turns
        it into a glow - which is what a bloom is for, and what it was
        already doing to the old halo anyway.
        """
        side = min(screen.width(), screen.height())
        if drawing:
            scale = side * (0.44 + flash * 0.08)
        else:
            scale = side * 0.30 * (1.0 + flash * self.FLASH_GAIN)
        beam.translate(screen.width() / 2.0, screen.height() / 2.0)
        beam.scale(scale, scale)
        path = self._path(trace, drawing)
        # A figure has detail in it that a fat beam fills in, so X-Y is
        # struck finer than a sweep.
        # In the tube's own pixels, so the beam ends up the same thickness
        # against the graticule however much the pane is shrinking the
        # frame it draws into.
        core = ((1.3 if drawing else 1.9) + flash * 1.2) * dpr
        colour = QColor.fromHsvF(0.34 - flash * 0.08,
                                 max(0.0, 0.42 - flash * 0.3), 1.0, 0.92)
        pen = QPen(colour, core, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.FlatCap, Qt.PenJoinStyle.BevelJoin)
        # In device pixels, so the scale above does not turn a two pixel
        # beam into a hundred pixel stripe. Bevelled and flat-capped
        # because a trace made of a thousand short segments has a join at
        # every one of them, and a round join there is an arc nobody can
        # see and everybody pays for.
        pen.setCosmetic(True)
        beam.setPen(pen)
        beam.drawPath(path)

    # -- paths -------------------------------------------------------------
    def _path(self, trace, drawing: bool):
        """One trace, in a box that does not depend on the window.

        Built at unit scale so that resizing, and the strobe pumping the
        gain, are a transform rather than a rebuild.
        """
        return self._vector_path(trace) if drawing else self._sweep_path(trace)

    def _vector_path(self, trace):
        """Left against right, plotted straight, in a unit box.

        No trigger and no clock: where the beam is, is what the record
        says. A disc cut for a scope draws a picture here; an ordinary mix
        draws the blob a vectorscope shows, leaning with the stereo image.
        """
        path = QPainterPath()
        count = len(trace) // 2
        # Stored as int16 so a long track's worth fits in memory.
        scale = 1.0 / 32768.0
        for index in range(count):
            x = trace[index * 2] * scale
            # Screen y grows downwards and a scope's does not.
            y = -trace[index * 2 + 1] * scale
            if index:
                path.lineTo(x, y)
            else:
                path.moveTo(x, y)
        return path

    def _sweep_path(self, trace):
        """One sweep, swept around a circle rather than across.

        The beam starts at twelve o'clock and goes round once; how far
        the signal is from zero is how far the trace is from the ring.
        A steady tone draws a closed flower, and the trace joins up with
        itself because the capture is triggered on a zero crossing.

        Built with the zero ring at radius one, so the caller scales it to
        whatever the window is now.
        """
        path = QPainterPath()
        count = len(trace)
        first = None
        for index, value in enumerate(trace):
            angle = (index / count) * math.tau - math.pi / 2.0
            reach = 1.0 + value * self.SWING
            point = QPointF(math.cos(angle) * reach, math.sin(angle) * reach)
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
        """A polar graticule, drawn like a scope's rather than a chart's.

        Rings for amplitude and ticks around the rim for phase. The spokes
        used to run right through the middle and cross the trace, which
        made the whole thing look like graph paper with a squiggle on it.
        """
        centre = rect.center()
        reach = min(rect.width(), rect.height()) * 0.44
        base = min(rect.width(), rect.height()) * 0.30
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # Amplitude rings, faint, evenly spaced either side of the zero.
        faint = QColor.fromHsvF(0.33, 0.55, 1.0, 0.08 + flash * 0.12)
        painter.setPen(QPen(faint, 1.0))
        for step in (0.4, 0.6, 0.8, 1.2, 1.4):
            painter.drawEllipse(centre, base * step, base * step)

        # Phase ticks at the rim only, longer every quarter turn.
        painter.setPen(QPen(QColor.fromHsvF(0.33, 0.5, 1.0,
                                            0.18 + flash * 0.25), 1.0))
        for step in range(24):
            angle = step * math.tau / 24.0
            long = step % 6 == 0
            inner = reach * (0.94 if long else 0.97)
            painter.drawLine(
                QPointF(centre.x() + math.cos(angle) * inner,
                        centre.y() + math.sin(angle) * inner),
                QPointF(centre.x() + math.cos(angle) * reach,
                        centre.y() + math.sin(angle) * reach))

        # The zero ring: where the trace sits in silence.
        zero = QColor.fromHsvF(0.33, 0.45, 1.0, 0.30 + flash * 0.35)
        painter.setPen(QPen(zero, 1.4))
        painter.drawEllipse(centre, base, base)

        # And the bezel, which makes it read as an instrument.
        painter.setPen(QPen(QColor.fromHsvF(0.33, 0.35, 1.0,
                                            0.14 + flash * 0.2), 2.0))
        painter.drawEllipse(centre, reach, reach)


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
                    hue = 0.33 - share * 0.33 - flash * 0.18   # green up into red
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

    #: Four megapixels drawn sharp. A face is rendered once per size and
    #: blitted after that, so a full screen of them is ten pixmap copies
    #: and ten needles - the pane's usual budget would halve the
    #: resolution of an instrument panel whose whole point is that the
    #: numbers on it are readable.
    sharp_pixels = 4_000_000

    #: The needle's travel, in degrees, measured the way Qt measures arcs.
    #: Centred on straight up, so the face sits square in its cell - the
    #: first attempt started at 202 and leaned the whole dial to the left.
    START = 145.0
    SWEEP = -110.0

    #: The face's own shape, in radii, and the only place it is written
    #: down. Everything else measures against these, so the drawing and
    #: the space reserved for it cannot disagree - which they did, and
    #: which is what made the dials look stretched.
    #:
    #: Taken off the reference rather than chosen: its arc is a 460-wide
    #: chord rising 120, which is a radius of 280 and a sweep of 110
    #: degrees, and the centre of that circle sits at the very bottom of
    #: the face. So the face is a wide, shallow thing - about two radii
    #: across and a quarter over one tall - and the code used to reserve
    #: 2.55 by 2.20, which is 1.76 times the height it ever draws in.
    #: Wide enough for the labels that sit beyond the ends of the arc,
    #: not just for the arc: at 2.05 the "-24" of one face and the "3" of
    #: the one before it were touching.
    FACE_WIDE = 2.34
    #: And tall enough for the row of numbers above the arc, which reach
    #: about a third of a radius past its apex. At 1.25 the top row of a
    #: grid had its "-3" cut off by the edge of the frame.
    FACE_TALL = 1.46
    #: How far below the top of the face the arc's centre sits. The
    #: difference between this and FACE_TALL is the room under the hub.
    FACE_DROP = 1.36
    #: Where the two lines of text sit, above the centre and inside the
    #: arc, which is where the reference puts them. They used to be below
    #: the centre, outside everything, which is what the extra height was
    #: being reserved for.
    DB_AT = 0.46
    LABEL_AT = 0.24
    #: Type sizes, as shares of the radius, in one place so a face keeps
    #: its proportions at every size it is drawn at.
    DB_TYPE = 0.125
    PERCENT_TYPE = 0.082
    UNIT_TYPE = 0.100
    LABEL_TYPE = 0.115

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
    #: It used to be 150, which no cell on a 1080p screen ever reached
    #: with ten meters on it, so the row that is half of what a VU face
    #: looks like had never once been drawn outside a test.
    PERCENT_RADIUS = 76.0
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
        #: How many are on each row, so the last one can be centred.
        on_row = [min(columns, count - r * columns) for r in range(rows)]
        flash = self.flash(state)
        # How many real pixels one unit of this rect is worth, so a face
        # is rendered at the resolution it will be shown at and no more.
        # It used to be supersampled two to one whatever the pane was
        # doing, which was right while the pane drew scenes at half size
        # and stretched them, and pure waste once this one asked to be
        # drawn sharp: ten faces at twice the size they are blitted at is
        # four times the pixels to copy every frame.
        dpr = abs(painter.combinedTransform().m11()) or 1.0
        for index in range(count):
            row, column = divmod(index, columns)
            # A short row is centred rather than left-aligned: three
            # meters hanging off the left of a five-wide grid reads as
            # two that failed to draw.
            spare = (columns - on_row[row]) * cell_w / 2.0
            box = QRectF(rect.left() + spare + column * cell_w,
                         rect.top() + row * cell_h,
                         cell_w, cell_h)
            label = (state.dial_labels[index]
                     if index < len(state.dial_labels) else "")
            self._meter(painter, box, levels[index], label, state, flash, dpr)

    @staticmethod
    def _grid(rect, count: int):
        """Columns and rows that make the faces as big as they can be.

        Scored on the radius each arrangement yields rather than on how
        square its cells come out. That sounds like the same thing and is
        not: a face is twice as wide as it is tall, so the arrangement
        with the tidiest cells is usually the one that wastes the most
        room. Ten meters on a 16:9 screen in five columns of two gives
        cells that are taller than they are wide, and the faces end up
        limited by width with a third of every cell empty underneath.

        Leaving a row short is allowed, and the short row is centred, so
        the space it does not use sits at the ends where it reads as
        margin rather than as a meter that failed to draw.
        """
        best = (1, count, 0.0)
        for columns in range(1, count + 1):
            rows = (count + columns - 1) // columns
            cell_w = rect.width() / columns
            cell_h = rect.height() / rows
            if cell_w <= 48 or cell_h <= 34:
                continue
            pad = min(cell_w, cell_h) * 0.05
            radius = min((cell_w - 2 * pad) / Meters.FACE_WIDE,
                         (cell_h - 2 * pad) / Meters.FACE_TALL)
            # A small nudge towards filling the grid, so that when two
            # arrangements give nearly the same size the tidy one wins.
            radius *= 1.0 - 0.02 * (columns * rows - count)
            if radius > best[2]:
                best = (columns, rows, radius)
        return best[0], best[1]

    # -- one meter --------------------------------------------------------
    def _meter(self, painter, box, value, label, state, flash,
               dpr: float = 1.0) -> None:
        key = (int(box.width()), int(box.height()), label,
               state.dial_colour.rgba(), round(dpr, 2))
        face = self._faces.get(key)
        if face is None:
            if len(self._faces) > 48:
                self._faces.clear()
            face = self._render_face(box, label, state, dpr)
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
            tint.setAlphaF(0.55 * flash)
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
        pad = min(box.width(), box.height()) * 0.05
        inner = box.adjusted(pad, pad, -pad, -pad)
        radius = min(inner.width() / Meters.FACE_WIDE,
                     inner.height() / Meters.FACE_TALL)
        # Centred in whatever is left over, in both directions. A face is
        # much wider than it is tall, so on most cells the width caps the
        # radius and there is spare height; hung from the top, every dial
        # sits jammed against the ceiling with a gap underneath.
        block = radius * Meters.FACE_TALL
        top = inner.top() + max(0.0, (inner.height() - block) / 2.0)
        centre_x = inner.center().x()
        centre_y = top + radius * Meters.FACE_DROP
        return {
            "radius": radius,
            "centre": QPointF(centre_x, centre_y),
            # At the arc's own centre. It was moved below it on the
            # reasoning that a moving coil hinges lower, which is true of
            # the movement and not of the face: on the reference the
            # needle is a radius of the arc it reads against, and hinging
            # it lower made it half as long again and dragged the whole
            # face taller to fit.
            "pivot": QPointF(centre_x, centre_y),
            "inner": inner,
        }

    def _render_face(self, box, label, state, dpr: float = 1.0):
        from PySide6.QtGui import QPixmap

        # One and a half times what it is shown at, capped. Rendering a
        # face exactly to size leaves its thin strokes and small numbers
        # aliased against the arc; going much past this buys nothing and
        # costs a bigger blit on every frame.
        ratio = max(1.0, min(2.0, dpr * 1.5))
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
        # Two arcs, because a real face has two. The scale is one weight
        # from the bottom of the range up to 0 dB and heavier from there
        # to the end of the travel - that heavier run is the red zone, and
        # it is the one marking on the instrument that means anything at a
        # glance.
        zero = self._db_at(0.0)
        painter.setPen(QPen(colour, max(1.4, radius * 0.030),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(span, int(self.START * 16),
                        int(self.SWEEP * zero * 16))
        painter.setPen(QPen(colour, max(2.2, radius * 0.052),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(span, int((self.START + self.SWEEP * zero) * 16),
                        int(self.SWEEP * (1.0 - zero) * 16))

        # A small face drops what it cannot show legibly rather than
        # printing it on top of itself. Ten meters in a strip two hundred
        # pixels tall leaves each one about forty pixels of radius, and
        # everything a full face carries will not fit in that.
        roomy = radius >= self.ROOMY
        marks = self.DB_MARKS if roomy else self.SPARSE_MARKS

        for _value, fraction in marks:
            self._tick(painter, centre, radius, fraction, colour,
                       0.84, 1.0, max(1.3, radius * 0.026))
        if roomy:
            # Dots, not lines. Every meter of this kind puts a row of
            # small points inside the arc between the numbered marks, and
            # short strokes hanging off the scale read as a comb instead.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dim)
            size = max(0.9, radius * 0.020)
            numbered = {round(f, 4) for _v, f in marks}
            for fraction in self.MINOR:
                if round(fraction, 4) in numbered:
                    continue
                angle = self._angle(fraction)
                painter.drawEllipse(
                    QPointF(centre.x() + math.cos(angle) * radius * 0.90,
                            centre.y() - math.sin(angle) * radius * 0.90),
                    size, size)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        font = painter.font()
        # Measured against the radius, and the same proportion at every
        # size. These were all a half larger than the reference, which is
        # what made a face look like a diagram of a meter rather than a
        # meter: the numbers were competing with the scale instead of
        # labelling it.
        font.setPointSizeF(max(6.0, radius * self.DB_TYPE))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QPen(colour))
        for value, fraction in marks:
            self._label(painter, centre, radius * 1.20, fraction, str(value))
        # The per-cent row shares the arc with the dB row. Ten faces across
        # a window leaves it about a hundred pixels of arc for six numbers,
        # which reads as a smudge, so it waits for a face big enough to
        # carry it - which is what full screen is for.
        if radius >= self.PERCENT_RADIUS:
            # Out near the arc and set small. The per-cent marks are
            # bunched into the left two thirds of the travel - a hundred
            # per cent is 0 dB, not the end of the scale - so the room
            # between them is the arc length at whatever radius they are
            # drawn at, and at 0.70 radii in a face this size the numbers
            # were wider than the gaps and ran into each other.
            font.setPointSizeF(max(4.0, radius * self.PERCENT_TYPE))
            painter.setFont(font)
            inside = QColor(colour)
            inside.setAlphaF(0.78)
            painter.setPen(QPen(inside))
            for value, fraction in self.PERCENT_MARKS:
                self._label(painter, centre, radius * 0.80, fraction,
                            str(value), tight=True)
        painter.setPen(QPen(colour))
        if roomy:
            font.setPointSizeF(max(5.0, radius * self.UNIT_TYPE))
            painter.setFont(font)
            painter.drawText(
                QRectF(centre.x() - radius * 0.5,
                       centre.y() - radius * (self.DB_AT + 0.14),
                       radius, radius * 0.28),
                Qt.AlignmentFlag.AlignCenter, "dB")
        if label:
            font.setPointSizeF(max(5.5, radius * self.LABEL_TYPE))
            painter.setFont(font)
            painter.drawText(
                QRectF(centre.x() - radius * 0.95,
                       centre.y() - radius * (self.LABEL_AT + 0.16),
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
        # The box the text is centred in. Narrower than the gap to its
        # neighbour, or two numbers share pixels - which is why the inner
        # row gets a much tighter one than the outer - but never narrower
        # than the text, which is how "100" came out as "10(".
        size = max(8.0, distance * (0.11 if tight else 0.26))
        width = max(size * 2.0,
                    painter.fontMetrics().horizontalAdvance(text) + 4.0)
        height = max(size * 0.9, painter.fontMetrics().height())
        painter.drawText(QRectF(point.x() - width / 2.0,
                                point.y() - height / 2.0, width, height),
                         Qt.AlignmentFlag.AlignCenter, text)

    def _needle(self, painter, geometry, value, state) -> None:
        centre = geometry["centre"]
        radius = geometry["radius"]
        pivot = geometry["pivot"]
        angle = self._angle(value)
        reach = QPointF(math.cos(angle), -math.sin(angle))
        # Placed against the scale, measured from the arc's own centre, so
        # the needle reads true; it is only hinged lower, where a real
        # movement sits. Short of the arc rather than through it - at 0.86
        # it crossed the scale it is reading and went through the number
        # at the top.
        tip = centre + reach * (radius * 0.90)
        # From the hub outwards, not from the dead centre: a needle drawn
        # through its own pivot has no hub, and the collar is the thing
        # that makes it read as hinged rather than as a line.
        tail = pivot + reach * (radius * 0.10)
        halo = QColor(state.dial_colour)
        halo.setAlphaF(0.26)
        painter.setPen(QPen(halo, max(3.0, radius * 0.075),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(tail, tip)
        # Tapered, in three strokes from the hinge out. A needle is
        # broad where it is anchored and fine where it has to be read
        # against a scale, and a line of one width is the one thing that
        # makes a drawn meter look drawn.
        for start, finish, width in ((0.0, 0.45, 0.032),
                                     (0.40, 0.78, 0.023),
                                     (0.74, 1.0, 0.015)):
            painter.setPen(QPen(state.dial_colour, max(1.1, radius * width),
                                Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(tail + (tip - tail) * start,
                             tail + (tip - tail) * finish)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(state.dial_colour)
        painter.drawEllipse(pivot, radius * 0.050, radius * 0.050)
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

    def __init__(self) -> None:
        self._plasma = Plasma()

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        middle = height * 0.5
        painter.fillRect(rect, QColor(3, 2, 8))
        # The morphing field, behind everything and dim enough that the
        # ribbons still read as the bright thing.
        self._plasma.paint(painter, rect, state, strength=0.55)
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
            reach = middle * (0.18 + band * 0.74 + flash * 0.55)
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
        width, height = rect.width(), rect.height()

        field = getattr(state, "history", None) or [list(levels)]
        field = field[-self.DEPTH:]
        # Every row is a line across the whole plot, so the cost is the
        # number of rows times the number of bands - about twelve hundred
        # antialiased segments at full depth, which is more than a slow
        # machine can draw sixty times a second. On a big frame every
        # other row is dropped: the ridges are wider there anyway and the
        # landscape reads the same.
        # Every other row at most. A third of them left fifteen ridges
        # with gaps between, which reads as tangled lines rather than as
        # a surface - the saving was not worth what it cost to look at.
        stride = 2 if rect.width() * rect.height() > 480_000 else 1
        if stride > 1:
            # Keep the newest row whatever the stride, so the front edge
            # is always the current frame.
            field = field[::-1][::stride][::-1]

        flash = self.flash(state)
        # The plot sits in the lower left, leaning up and to the right.
        # Gutters for the axes, so the numbers sit beside the plot rather
        # than on top of the data.
        left = max(38.0, width * 0.05)
        foot = max(30.0, height * 0.075)
        plot_w = (width - left) * (1.0 - self.SKEW_X) * 0.98
        plot_h = (height - foot) * (1.0 - self.SKEW_Y) * 0.80
        origin_x = left
        origin_y = height - foot
        # The landscape used to rear up on a hit, which moves every ridge
        # at once and reads as the plot glitching rather than as a beat.
        # The strobe lights it instead: the floor brightens and the ridges
        # gain colour, and the geometry stays where it was.
        rise = plot_h

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
            # Which bucket the previous segment went into. Neighbouring
            # bands are nearly always the same loudness, so this is how a
            # row gets drawn as a handful of joined-up runs instead of
            # forty-seven separate ones. It is worth doing because a
            # subpath is stroked with a cap at each end, and forty-seven
            # of them meant ninety-four round caps per row - measured at
            # a third of what this scene cost on a big frame, for
            # something nobody can see: the caps are drawn on top of each
            # other at the joins.
            was = None
            for index, value in enumerate(row):
                x = origin_x + offset_x + plot_w * (index / max(1, count - 1))
                y = origin_y - offset_y - value * rise
                here = QPointF(x, y)
                if previous is not None:
                    bucket = min(len(self.SHADES) - 1,
                                 int(value * len(self.SHADES)))
                    path = buckets[bucket]
                    if bucket != was:
                        path.moveTo(previous)
                    # A curve between the two, with the control points
                    # level with each end. Straight segments made every
                    # ridge a zig-zag; this rounds the peaks the way a
                    # spectrum actually moves between bands.
                    half = (previous.x() + here.x()) * 0.5
                    path.cubicTo(QPointF(half, previous.y()),
                                 QPointF(half, here.y()), here)
                    was = bucket
                previous = here

        painter.setBrush(Qt.BrushStyle.NoBrush)
        for index, path in enumerate(buckets):
            if path.isEmpty():
                continue
            hue, value = self.SHADES[index]
            share = index / max(1, len(self.SHADES) - 1)
            colour = QColor.fromHsvF(hue, 0.85 - flash * 0.5, value,
                                     min(1.0, 0.35 + share * 0.55
                                         + flash * 0.45))
            # The strobe used to add more than a pixel to every ridge at
            # once, which is the whole plot drawn wider on the beat: the
            # frames that hit cost twice what the quiet ones did, and they
            # are exactly the frames nobody wants to see stutter. It
            # brightens instead, above, which costs nothing.
            painter.setPen(QPen(colour, 1.0 + share * 1.4 + flash * 0.4,
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
