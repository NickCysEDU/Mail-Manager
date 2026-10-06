"""Visualiser scenes. Each is handed the same analysed state every frame and
draws it; none of them computes a spectrum.
"""

from __future__ import annotations

import bisect
import logging
import math
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QFontMetricsF, QImage,
                           QLinearGradient,
                           QPainter, QPainterPath,
                           QPen, QRadialGradient, QTransform)

from beat_clock import BeatClock


log = logging.getLogger(__name__)


def _on_card(painter) -> bool:
    """Whether ``painter`` draws on the graphics card rather than into an
    image."""
    from PySide6.QtGui import QPaintEngine

    engine = painter.paintEngine()
    return (engine is not None
            and engine.type() == QPaintEngine.Type.OpenGL2)


#: The dials' typeface, shipped with the app so every machine draws the same
#: dial: Michroma, under the SIL Open Font License.
FONT_FILE = "Michroma-Regular.ttf"
FONT_FAMILY = "Michroma"
#: Its licence, which has to travel with it.
FONT_LICENCE = "Michroma-OFL.txt"

_LOADED: Optional[str] = None


def dial_face() -> Optional[str]:
    """The family the dials are lettered in, or None if it did not load. Loaded
    once; a failure is logged.
    """
    global _LOADED
    if _LOADED is not None:
        return _LOADED or None
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:
            # A font cannot be registered before there is an application; ask
            # again later.
            return None
    except Exception:      # noqa: BLE001
        return None
    _LOADED = ""
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    for root in (here, Path(__file__).resolve().parent):
        candidate = root / "assets" / "fonts" / FONT_FILE
        if not candidate.exists():
            continue
        try:
            from PySide6.QtGui import QFontDatabase

            at = QFontDatabase.addApplicationFont(str(candidate))
            families = QFontDatabase.applicationFontFamilies(at)
        except Exception as exc:      # noqa: BLE001 - decoration, not mail
            log.info("Could not load the dial face (%s).", exc)
            return None
        if families:
            _LOADED = families[0]
            return _LOADED
        log.info("The dial face at %s loaded no families.", candidate)
        return None
    log.info("The dial face %s is not beside this module.", FONT_FILE)
    return None


#: Qt's raster engine is fast for one-pixel lines and far slower above them (a
#: frame of curves at 1080p: 2.2 ms against 58 ms), so wide lines are drawn as
#: stacks of hairlines. See stroke.
HAIRLINE = 1.0

#: Rings of offset hairlines are this far apart, in real pixels.
HAIR_STEP = 0.9

#: Past this many passes a real wide pen is cheaper. Measured on three scenes
#: at 1080p.
HAIR_MOST = 14

#: Solved alphas, by width and solidity.
_HAIR_ALPHA: dict = {}


def smooth_path(points):
    """A smooth curve through these points: each sample is a quadratic's
    control point and the midpoints between samples are on the curve, so
    there are no corners.
    """
    path = QPainterPath()
    if not points:
        return path
    if len(points) < 3:
        path.moveTo(points[0])
        for point in points[1:]:
            path.lineTo(point)
        return path
    path.moveTo(points[0])
    for index in range(1, len(points) - 1):
        here, following = points[index], points[index + 1]
        path.quadTo(here, QPointF((here.x() + following.x()) * 0.5,
                                  (here.y() + following.y()) * 0.5))
    # Straight to the last point; a quadratic there would turn back on itself.
    path.lineTo(points[-1])
    return path


def stroke(painter, path, colour, width: float,
           cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin) -> None:
    """Draw ``path`` in ``colour`` at ``width``: as a stack of one-pixel lines
    where that is cheaper (see HAIRLINE), each pass faint enough that the
    stack reads as one line (see _hair_alpha).
    """
    # Only under ordinary compositing. Under additive compositing the passes
    # add light, and the reduced alpha leaves a hollow line.
    if (painter.compositionMode()
            != QPainter.CompositionMode.CompositionMode_SourceOver):
        painter.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, cap, join))
        painter.drawPath(path)
        return
    scale = abs(painter.combinedTransform().m11()) or 1.0
    thick = width * scale
    if thick <= HAIRLINE + 0.01:
        painter.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, cap, join))
        painter.drawPath(path)
        return
    spots = _hair_spots((thick - HAIRLINE) / 2.0)
    if not spots or len(spots) > HAIR_MOST:
        painter.setPen(QPen(colour, width, Qt.PenStyle.SolidLine, cap, join))
        painter.drawPath(path)
        return
    faint = QColor(colour)
    faint.setAlphaF(_hair_alpha(spots, (thick - HAIRLINE) / 2.0,
                                colour.alphaF()))
    # Width zero keeps it one real pixel however the painter is scaled.
    pen = QPen(faint, 0.0, Qt.PenStyle.SolidLine, cap, join)
    pen.setCosmetic(True)
    painter.setPen(pen)
    for dx, dy in spots:
        painter.translate(dx / scale, dy / scale)
        painter.drawPath(path)
        painter.translate(-dx / scale, -dy / scale)


def _hair_spots(reach: float) -> tuple:
    """Where to put the hairlines to fill a disc of radius ``reach``: the
    centre, then rings no more than HAIR_STEP apart. Empty when that would
    take more than HAIR_MOST passes.
    """
    spots = [(0.0, 0.0)]
    if reach <= 0.05:
        return tuple(spots)
    rings = max(1, int(math.ceil(reach / HAIR_STEP)))
    for step in range(1, rings + 1):
        radius = reach * step / rings
        count = max(4, int(math.ceil(2.0 * math.pi * radius / HAIR_STEP)))
        if len(spots) + count > HAIR_MOST:
            return ()
        for index in range(count):
            angle = 2.0 * math.pi * index / count
            spots.append((math.cos(angle) * radius, math.sin(angle) * radius))
    return tuple(spots)


def _hair_alpha(spots: tuple, reach: float, target: float) -> float:
    """How solid each pass must be for the stack to put down as much ink as a
    pen of this width. Solved by bisection over how many passes cover each
    column across the line, and cached.
    """
    key = (round(reach, 2), round(target, 3))
    found = _HAIR_ALPHA.get(key)
    if found is not None:
        return found
    step, angles = 0.1, 8
    edge = int((reach + 0.5) / step) + 1
    tally: dict = {}
    for turn in range(angles):
        angle = math.pi * turn / angles
        across = [x * -math.sin(angle) + y * math.cos(angle) for x, y in spots]
        for index in range(-edge, edge + 1):
            here = index * step
            covers = sum(1 for c in across if abs(c - here) <= 0.5)
            if covers:
                tally[covers] = tally.get(covers, 0) + 1
    wanted = target * (2.0 * reach + 1.0) * angles / step
    low, high = 0.0, 1.0
    for _ in range(24):
        middle = (low + high) / 2.0
        got = sum(n * (1.0 - (1.0 - middle) ** k) for k, n in tally.items())
        if got < wanted:
            low = middle
        else:
            high = middle
    answer = min(1.0, (low + high) / 2.0)
    _HAIR_ALPHA[key] = answer
    return answer


def bounded(value, most: float = 1.0, least: float = 0.0) -> float:
    """A number from the analysis, forced back into its range. A nan comes back
    as the floor.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        return least
    if value != value:
        return least
    if value < least:
        return least
    return most if value > most else value


#: The tempos a scene is driven at. Beyond them a tempo is taken for an octave
#: error and folded back.
TEMPO_LEAST = 70.0
TEMPO_MOST = 165.0


def folded_tempo(tempo: float) -> float:
    """The same pulse, counted the way a person would count it. Apply it where
    tempo and phase are worked out together, or the phase belongs to the
    other tempo.
    """
    try:
        tempo = float(tempo)
    except (TypeError, ValueError):
        return 0.0
    if not tempo > 0.0 or tempo != tempo:
        return 0.0
    for _ in range(8):
        if tempo > TEMPO_MOST:
            tempo /= 2.0
        elif tempo < TEMPO_LEAST:
            tempo *= 2.0
        else:
            break
    return tempo


class Scene:
    """One way of drawing the music."""

    name = "scene"
    blurb = "a scene"

    #: How many pixels this scene can draw at their real size; zero means the
    #: pane's own floor. Raised by scenes that are mostly blits.
    sharp_pixels = 0

    #: Whether a stretched buffer is smoothed even at a whole-number scale.
    #: False suits thin bright lines; arcs and lettering want True.
    stretch_smooth = False

    #: The smoothed strobe; see bloom.
    _bloom = 0.0

    def paint(self, painter: QPainter, rect, state) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        """Forget everything and start again, keeping what the viewer chose
        (KEPT). There is one of each scene for the session, so a new track
        or a newly picked scene starts from here.
        """
        kept = {name: getattr(self, name) for name in self.KEPT
                if hasattr(self, name)}
        fresh = type(self)()
        for name, value in vars(fresh).items():
            setattr(self, name, value)
        for name, value in kept.items():
            setattr(self, name, value)

    #: What the viewer chose with the pane's controls, by attribute name; reset
    #: keeps it.
    KEPT: tuple = ()

    @staticmethod
    def flash(state) -> float:
        """How hard the strobe is hitting, 0 to 1, or 0 when it is off. Each
        scene does its own thing with it.
        """
        return state.hit if state.strobe else 0.0

    #: How a smoothed strobe rises and falls: quick up, about a third of a
    #: second down.
    BLOOM_RISE = 0.30
    BLOOM_FALL = 0.055

    def bloom(self, state) -> float:
        """The strobe, smoothed and advanced one frame, for scenes where a step
        would read as a glitch. Call it once a frame; ``flash`` is the raw
        step.
        """
        hit = self.flash(state)
        was = self._bloom
        speed = self.BLOOM_RISE if hit > was else self.BLOOM_FALL
        now = was + (hit - was) * speed
        if now < 0.002:
            now = 0.0
        self._bloom = now
        return now

    @staticmethod
    def geometry(rect, count: int, width_fraction: float = 0.9):
        """(left, bar width, gap) for a row of ``count`` bars."""
        span = rect.width() * width_fraction
        left = rect.left() + (rect.width() - span) / 2.0
        gap = max(1.0, span / max(1, count) * 0.18)
        bar = max(1.0, (span - gap * (count - 1)) / max(1, count))
        return left, bar, gap


class Plasma:
    """The morphing coloured field: sums of sines on a coarse grid, stretched
    up smooth.
    """

    COLUMNS = 36
    ROWS = 22
    #: Frames between recomputes; the field moves over seconds.
    EVERY = 2

    def __init__(self) -> None:
        self._image = None
        self._countdown = 0
        self._drift_a = 0.0
        self._drift_b = 0.0
        self._drift_c = 0.0

    def paint(self, painter, rect, state, strength: float = 1.0,
              flash: float = None, going: float = 1.0) -> None:
        """The field. ``flash`` overrides the strobe, and ``going`` is how much
        of a frame's movement to take: zero holds it still.
        """
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
        # Three clocks at different rates, turning waves in different
        # directions, make the field fold rather than scroll; the music drives
        # their speed and depth. Bounded, because they accumulate.
        bass, mid, high = (bounded(state.bass), bounded(state.mid),
                           bounded(state.high))
        pace = (0.55 + bass * 1.9 + mid * 0.8) * going
        self._drift_a += 0.016 * pace
        self._drift_b -= 0.011 * pace + high * 0.02 * going
        self._drift_c += 0.007 * pace
        hit = bounded(self.flash_of(state) if flash is None else flash)
        swell = 0.55 + bass * 0.8 + hit * 0.9
        hue_shift = bounded(state.hue)
        image = self._image
        for row in range(self.ROWS):
            y = row / self.ROWS
            for column in range(self.COLUMNS):
                x = column / self.COLUMNS
                # Three waves at angles to each other, each on its own clock.
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

        # The strobe flares the sun rather than washing the frame, smoothed so
        # it does not jump.
        flash = self.bloom(state)
        self._sun(painter, width, horizon, hue, state.bass, flash)

        # The towers are the equaliser: one per band.
        self._far_skyline(painter, width, horizon, state)
        self._skyline(painter, width, horizon, state)
        self._floor(painter, width, height, horizon, state)
        self._reflection(painter, width, height, horizon, state)
        self._ribbons(painter, width, horizon, state)
        self._stars(painter, width, horizon, state)

    #: The gaps across the sun: their spacing and how far up the disc they go,
    #: as shares of its radius.
    BAR_APART = 0.105
    BAR_TOP = 0.90

    #: How much of a strobe goes into the sun's size; the rest is brightness.
    FLARE_SIZE = 0.08

    @staticmethod
    def sun_radius(horizon: float, bass: float, flash: float) -> float:
        """How big the sun is. Separate so it can be asked for."""
        return horizon * (0.46 + bass * 0.26
                          + flash * Vaporwave.FLARE_SIZE)

    def _sun(self, painter, width, horizon, hue: float, bass: float,
             flash: float) -> None:
        """The sun: a glow, a disc shaded from pale to magenta, and gaps cut
        across it inside a clip of the disc, wider towards the horizon.
        """
        radius = self.sun_radius(horizon, bass, flash)
        if radius <= 1.0:
            return
        centre = QPointF(width / 2.0, horizon)
        sky = QRectF(0, 0, width, horizon)

        # The glow around it.
        glow = QRadialGradient(centre, radius * 1.55)
        glow.setColorAt(0.0, QColor.fromHsvF(
            hue, max(0.0, 0.55 - flash * 0.45), 1.0,
            min(1.0, 0.42 + bass * 0.22 + flash * 0.52)))
        glow.setColorAt(0.45, QColor.fromHsvF(
            (hue + 0.04) % 1.0, max(0.0, 0.9 - flash * 0.4), 1.0,
            min(1.0, 0.16 + flash * 0.46)))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.fillRect(sky, glow)

        face = QLinearGradient(0.0, horizon - radius, 0.0, horizon)
        face.setColorAt(0.0, QColor.fromHsvF((hue + 0.07) % 1.0,
                                             0.42 - flash * 0.3, 1.0))
        face.setColorAt(0.52, QColor.fromHsvF((hue + 0.99) % 1.0,
                                              0.72 - flash * 0.4, 1.0))
        face.setColorAt(1.0, QColor.fromHsvF((hue + 0.92) % 1.0,
                                             0.92 - flash * 0.5, 0.97))

        painter.save()
        disc = QPainterPath()
        disc.addEllipse(centre, radius, radius)
        painter.setClipRect(sky)
        painter.setClipPath(disc, Qt.ClipOperation.IntersectClip)
        painter.fillRect(sky, face)
        painter.setPen(Qt.PenStyle.NoPen)
        apart = max(3.0, radius * self.BAR_APART)
        up = apart
        while up < radius * self.BAR_TOP:
            share = up / (radius * self.BAR_TOP)      # 0 at the horizon
            thick = apart * 0.86 * (1.0 - share) ** 1.05
            if thick < 0.5:
                break
            painter.fillRect(
                QRectF(0.0, horizon - up - thick / 2.0, width, thick),
                QColor(12, 5, 26, 225))
            up += apart
        painter.restore()

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
        mirrored = QPainterPath()
        roofs = QPainterPath()
        for index, value in enumerate(levels):
            tall = horizon * (0.10 + value * 0.42)
            x = index * block
            shade = ((index / count) * 0.2 + state.hue + 0.6) % 1.0
            # Dark bodies, so the lit windows and roof lines carry the shape.
            painter.fillRect(QRectF(x, horizon - tall, block * 0.92, tall),
                             QColor.fromHsvF(shade, 0.88, 0.13, 1.0))
            # A lit roof edge, so the city reads against the sun. Collected and
            # filled once.
            roofs.addRect(QRectF(x, horizon - tall, block * 0.92,
                                 max(1.0, horizon * 0.006)))
            # Lit windows, collected into one path and filled once.
            if value > 0.25:
                spacing = max(9.0, horizon * 0.022)
                rows = int(tall / spacing)
                for row in range(rows):
                    if (index + row) % 3 == 0:
                        top = horizon - tall + row * spacing + 3
                        deep = max(2.0, spacing * 0.3)
                        windows.addRect(QRectF(x + block * 0.22, top,
                                               block * 0.2, deep))
                        # The window's reflection in the floor, collected in
                        # the same loop and filled once.
                        mirrored.addRect(QRectF(
                            x + block * 0.22,
                            horizon + (tall - (row * spacing + 3))
                            * self.MIRROR,
                            block * 0.2, deep * self.MIRROR))
        painter.setPen(Qt.PenStyle.NoPen)
        # The floor first, so the city stands on it rather than under it.
        painter.fillPath(mirrored, QColor.fromHsvF(
            (state.hue + 0.6) % 1.0, 0.30 - self.flash(state) * 0.25, 1.0,
            (0.42 + self.flash(state) * 0.45) * self.MIRROR_LIT))
        # Windows brighten together on a hit, like a block losing its blinds.
        painter.fillPath(roofs, QColor.fromHsvF(
            (state.hue + 0.68) % 1.0, 0.45, 1.0, 0.80))
        painter.fillPath(windows, QColor.fromHsvF(
            (state.hue + 0.6) % 1.0, 0.30 - self.flash(state) * 0.25, 1.0,
            0.42 + self.flash(state) * 0.45))

    #: How far the floor squashes what it reflects, and how much of a window's
    #: light survives; the same squash as the towers' reflection.
    MIRROR = 0.5
    MIRROR_LIT = 0.38

    #: Floor lines in a bar: four, so one arrives on every beat.
    FLOOR_LINES = 15
    PER_BAR = 4.0

    def _scroll(self, state) -> float:
        """Where the floor has got to: on the beat where there is one, so the
        lines march with the music rather than past it.
        """
        tempo = getattr(state, "tempo", 0.0)
        if tempo <= 0.0:
            return state.scroll
        # One line a beat, read from the playhead each frame so a seek lands
        # where it should.
        return getattr(state, "beat_at", 0.0)

    def _floor(self, painter, width, height, horizon, state) -> None:
        depth = height - horizon
        colour = QColor.fromHsvF(state.hue, 0.72, 1.0, 0.28 + state.bass * 0.5)
        painter.setPen(QPen(colour, 1.0))
        scroll = self._scroll(state)
        for step in range(1, self.FLOOR_LINES):
            t = ((step + scroll) / self.FLOOR_LINES) ** 2.4
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
        """A few fixed stars high in the sky, brightening with the treble."""
        painter.setPen(Qt.PenStyle.NoPen)
        shade = (state.hue + 0.5) % 1.0
        # One path, one fill.
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

        # A hit fires one bright ring outwards, drawn after the corridor,
        # rather than moving every ring at once.
        flash = self.flash(state)
        rush = 0.0
        for index in range(self.RINGS, 0, -1):
            t = ((index + state.scroll + rush) % self.RINGS) / self.RINGS
            # Perspective: near rings are large and bright, far ones small.
            scale = t ** 1.8
            # Bass swells each ring evenly; squashing it read as a mistake.
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
        # A new hit's ring is small and bright, and fainter as it travels out.
        travel = 1.0 - flash
        radius = 20.0 + travel * max(width, height) * 0.75
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step, (width_scale, alpha) in enumerate(((3.0, 0.9), (7.0, 0.35))):
            painter.setPen(QPen(
                QColor.fromHsvF(0.12, 0.25, 1.0, alpha * flash),
                width_scale * (0.4 + flash)))
            painter.drawEllipse(centre, radius + step * 4, radius + step * 4)

    def _spokes(self, painter, centre, reach, state) -> None:
        """The bands as rays out of the middle, which suit the perspective
        better than a bar graph.
        """
        levels = state.levels
        count = len(levels)
        if not count:
            return
        # The spokes ignore the strobe; the rings and the shockwave already
        # answer it.
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
    """A real scope: the waveform itself, triggered on a rising zero crossing,
    on a phosphor that fades. The screen is an image kept between frames:
    each frame dims it and draws one new trace, so any decay costs the same.
    """

    name = "Oscilloscope"
    blurb = "the waveform swept round a circle, on a phosphor you can set"

    #: How faint a trace is when its decay time is up; the fade is exponential,
    #: so "gone" needs a number.
    FADED = 0.02
    #: The longest step the fade takes at once, so a stalled frame does not
    #: wipe the screen in one jolt.
    MAX_STEP = 0.25
    #: Seconds of persistence at each end of the slider.
    MIN_DECAY = 0.03
    MAX_DECAY = 1.50

    #: How the beam is driven: Sweep goes round once a frame; X-Y plots left
    #: against right, which draws the picture in a record made for a scope.
    MODES = ("Sweep", "X-Y")

    #: How far the trace swings either side of the zero ring, as a share of its
    #: radius. Fixed, so a built path can be kept.
    SWING = 0.42
    #: How much the strobe pumps the gain: the whole figure grows, as a
    #: transform, so a cached path stays valid.
    FLASH_GAIN = 0.03

    #: A beam glows brighter where it moves slowly, which is most of what
    #: oscilloscope music looks like. DWELL_AIM is the step between samples,
    #: in the figure's unit box, at full brightness, measured against the
    #: trace's own moving steps; faster stretches fade towards DWELL_LEAST.
    DWELL_AIM = 0.62
    DWELL_LEAST = 0.22
    #: How many brightnesses the trace is cut into; each is one stroke a frame.
    DWELL_LEVELS = 6
    #: How many samples share one brightness. A level per sample cut a noisy
    #: trace into hundreds of short paths (154 ms a frame against 6).
    DWELL_BLOCK = 8
    #: How far a parked beam is nudged so that it draws: Qt strokes nothing for
    #: a subpath of zero length.
    DWELL_PARKED = 1e-4

    #: How much wider the beam is drawn where it is brightest, as a hot spot
    #: blooms in the glass.
    DWELL_SPREAD = (0.80, 1.55)
    #: How far the hottest parts wash out towards white.
    DWELL_WHITE = 0.34

    #: The beam and the glow, as the controls set them.
    KEPT = ("_mode", "_decay")

    def __init__(self) -> None:
        self._decay = 0.28
        self._mode = "Sweep"
        self._plasma = Plasma()
        #: The screen itself: what the beam has drawn and not yet lost.
        self._screen = None
        #: When it was last dimmed, so the decay is in seconds whatever the
        #: frame rate.
        self._last = None
        #: The last trace burned in. A paused track hands the same one back,
        #: and drawing it again would pile up brightness.
        self._burned = None
        #: The screen on the graphics card when the pane draws on one (see
        #: scope_gl); False once the card has failed.
        self._card = None

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in self.MODES:
            self._mode = mode

    @property
    def decay(self) -> float:
        return self._decay

    def set_decay(self, seconds: float) -> None:
        self._decay = max(self.MIN_DECAY, min(self.MAX_DECAY, float(seconds)))

    def paint(self, painter, rect, state) -> None:
        painter.fillRect(rect, QColor(2, 8, 4))
        flash = self.flash(state)
        # A faint field behind the graticule, so the screen looks lit from
        # within.
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

        if self._card is not False and _on_card(painter):
            try:
                if self._card is None:
                    import scope_gl

                    self._card = scope_gl.Tube()
                self._card.draw(self, painter, rect, trace, drawing, flash)
                return
            except Exception:      # noqa: BLE001 - a picture, not the mail
                log.exception("The scope's screen could not be kept on the "
                              "graphics card; keeping it on the CPU.")
                self._card = False
        # Real pixels per unit of this rect, so the tube is built at the size
        # it is drawn at rather than the logical size.
        dpr = abs(painter.combinedTransform().m11()) or 1.0
        screen = self._tube(rect, trace, drawing, flash, dpr)
        if screen is not None:
            painter.drawImage(rect, screen, QRectF(screen.rect()))

    def _tube(self, rect, trace, drawing: bool, flash: float, dpr: float = 1.0):
        """Dim what is on the screen, lay the new trace over it, and hand it
        back. The fade is a DestinationIn fill; the graticule is drawn live
        underneath and never goes into the tube.
        """
        size = QSize(max(0, int(rect.width() * dpr)),
                     max(0, int(rect.height() * dpr)))
        if size.width() < 2 or size.height() < 2:
            return None
        screen = self._fit(size)
        keep = self._fade_now(trace)
        if keep is None:
            return screen

        beam = QPainter(screen)
        try:
            beam.setCompositionMode(
                QPainter.CompositionMode.CompositionMode_DestinationIn)
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

    def _fade_now(self, trace):
        """How much of the screen survives to this frame, or None where
        ``trace`` is the one already burned in (a paused track).
        """
        now = time.monotonic()
        step = self.MAX_STEP if self._last is None else min(
            self.MAX_STEP, max(0.0, now - self._last))
        self._last = now
        if trace is self._burned:
            return None
        self._burned = trace
        # Exponential, so the trace is at FADED after ``decay`` seconds
        # whatever the frame rate.
        return self.FADED ** (step / max(1e-3, self._decay))

    def _fit(self, size):
        """The screen at this size, scaled from the old one so a window being
        resized keeps its trace.
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
        """One pass of the beam: one hot stroke, which the scene's bloom turns
        into a glow.
        """
        side = min(screen.width(), screen.height())
        if drawing:
            scale = side * (0.44 + flash * 0.08)
        else:
            scale = side * 0.30 * (1.0 + flash * self.FLASH_GAIN)
        beam.translate(screen.width() / 2.0, screen.height() / 2.0)
        beam.scale(scale, scale)
        # X-Y figures are struck finer than a sweep, and the width is in the
        # tube's own pixels so it is the same against the graticule at any
        # size.
        core = ((1.8 if drawing else 2.2) + flash * 1.2) * dpr
        for level, path in enumerate(self._beams(self._points(trace,
                                                              drawing))):
            if path.isEmpty():
                continue
            share = level / max(1, self.DWELL_LEVELS - 1)
            # Bright and white where the beam lingered, faint and green where
            # it hurried.
            colour = QColor.fromHsvF(
                max(0.0, 0.34 - flash * 0.08),
                max(0.0, (0.42 - flash * 0.3)
                    * (1.0 - share * self.DWELL_WHITE)),
                1.0,
                self.DWELL_LEAST + (1.0 - self.DWELL_LEAST) * share)
            thin, fat = self.DWELL_SPREAD
            # Through ``stroke``, as a stack of hairlines: one wide pen over a
            # real trace took 880 ms a frame, the stack 3. Widths are in the
            # painter's units.
            stroke(beam, path, colour,
                   core * (thin + (fat - thin) * share) / scale,
                   # Round caps, so a parked beam still draws a dot; bevelled
                   # joins, because a trace has a join at every sample.
                   cap=Qt.PenCapStyle.RoundCap,
                   join=Qt.PenJoinStyle.BevelJoin)

    def _points(self, trace, drawing: bool):
        """One trace, as points in a unit box, so resizing and the strobe's
        gain are a transform rather than a rebuild.
        """
        return (self._vector_points(trace) if drawing
                else self._sweep_points(trace))

    def _beams(self, points):
        """One path per brightness, dimmest first, so bright stretches lie over
        faint ones. Each run starts at the point before it, so runs join
        without a gap.
        """
        top = self.DWELL_LEVELS - 1
        if len(points) < 2:
            paths = [QPainterPath() for _ in range(self.DWELL_LEVELS)]
            if points:
                paths[top].moveTo(points[0])
            return paths
        steps = [math.hypot(points[i].x() - points[i - 1].x(),
                            points[i].y() - points[i - 1].y())
                 for i in range(1, len(points))]
        # The seventieth percentile of the steps rather than the median, which
        # is zero when the beam is parked for over half the trace.
        ranked = sorted(steps)
        middle = ranked[min(len(ranked) - 1, int(len(ranked) * 0.70))]
        reach = max(1e-9, middle * self.DWELL_AIM)
        levels = []
        for start in range(0, len(steps), self.DWELL_BLOCK):
            # Energy per unit length, over a block of samples so the shading
            # does not dither into noise.
            block = steps[start:start + self.DWELL_BLOCK]
            step = sum(block) / len(block)
            lit = 1.0 if step <= 1e-9 else min(1.0, reach / step)
            levels.extend([min(top, int(lit * self.DWELL_LEVELS))]
                          * len(block))
        paths = [QPainterPath() for _ in range(self.DWELL_LEVELS)]
        start = 0
        for index in range(1, len(levels) + 1):
            if index < len(levels) and levels[index] == levels[start]:
                continue
            run = points[start:index + 1]
            paths[levels[start]].addPath(self._run(run))
            start = index
        return paths

    def _run(self, run):
        """One stretch of the trace as a path, parked or moving. Always a
        curve: it is shorter to stroke than straight segments, and was
        faster at every threshold.
        """
        spread = max(abs(run[-1].x() - run[0].x()),
                     abs(run[-1].y() - run[0].y()))
        if len(run) > 1 and spread < self.DWELL_PARKED:
            # The beam stopped: a hair, for the round cap to sit on. See
            # DWELL_PARKED.
            dot = QPainterPath()
            dot.moveTo(run[0])
            dot.lineTo(QPointF(run[0].x() + self.DWELL_PARKED, run[0].y()))
            return dot
        return smooth_path(run)

    def _vector_points(self, trace):
        """Left against right, plotted straight, in a unit box. The caller
        draws a curve through the points, as a real beam rounds the corners
        it is asked to draw.
        """
        count = len(trace) // 2
        # Stored as int16 so a long track's worth fits in memory.
        scale = 1.0 / 32768.0
        return [QPointF(trace[index * 2] * scale,
                        # Screen y grows downwards and a scope's does not.
                        -trace[index * 2 + 1] * scale)
                for index in range(count)]

    def _sweep_points(self, trace):
        """One sweep, around a circle: the distance from the ring is the
        signal. Built with the zero ring at radius one.
        """
        count = len(trace)
        points = []
        for index, value in enumerate(trace):
            angle = (index / count) * math.tau - math.pi / 2.0
            reach = 1.0 + value * self.SWING
            points.append(QPointF(math.cos(angle) * reach,
                                  math.sin(angle) * reach))
        if points:
            points.append(points[0])      # close the sweep
        return points

    def _from_levels(self, state):
        """A fallback shape from the bands, for analyses with no captured
        waveform.
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
        """A polar graticule: rings for amplitude and ticks around the rim,
        with nothing crossing the trace.
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
        """The plain equaliser as columns of lit segments, amber near the top
        and red at the top.
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
        """Where a level sits, 0 at the foot and 1 at the top: the analysis's
        stretch undone, relative to the loudest the track gets.
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




def _dots_between(*tables, apart: float = 0.035) -> tuple:
    """A point midway between each neighbouring pair of numbered marks. A
    function, because a class body cannot be read from inside a
    comprehension in it.
    """
    marks = sorted({fraction for table in tables for _v, fraction in table})
    out: list = []
    for first, second in zip(marks, marks[1:]):
        middle = (first + second) / 2.0
        if not out or middle - out[-1] > apart:
            out.append(middle)
    return tuple(out)


class Meters(Scene):
    """Ten analogue VU meters in a rack, drawn from a photograph of a real one.
    Each face is drawn once per cell size and blitted; only the needles
    move. Colours come from the picker.
    """

    name = "VU meters"
    blurb = "ten analogue dials, one per band, with a colour picker"

    #: Four megapixels drawn sharp: the faces are blitted, so a full screen of
    #: them is cheap, and the numbers should be readable.
    sharp_pixels = 4_000_000

    #: Smoothed when stretched: arcs and lettering pixelate when doubled.
    stretch_smooth = True

    #: The needle's travel, in degrees as Qt measures arcs, centred on straight
    #: up.
    START = 145.0
    SWEEP = -110.0

    #: The face's shape in radii, measured off the reference photograph, and
    #: the only place it is written: the drawing and the space reserved for it
    #: both read these. The height covers the needle at rest and pinned.
    FACE_WIDE = 1.93
    FACE_TALL = 1.13
    #: How far below the top of the face the arc's centre sits.
    FACE_DROP = 1.16
    #: Where the two lines of text sit: above the centre, inside the arc.
    DB_AT = 0.46
    LABEL_AT = 0.24
    #: Type sizes and the arc's stroke, as shares of the radius, measured off
    #: the reference.
    ARC_STROKE = 0.020
    #: The run above 0 dB is heavier, but only a little.
    ARC_STROKE_HOT = 0.027
    #: Where the arc's stroke sits, a little inside the radius.
    ARC_AT = 0.968
    #: Ticks reach outward past the arc.
    TICK_IN = 0.945
    TICK_OUT = 1.052
    #: The small marks between them are dots, outside the arc.
    DOT_AT = 1.012
    DOT_SIZE = 0.011
    #: The face ships with the app (see ``dial_face``); these are only what Qt
    #: falls back to if the file goes missing.
    FAMILIES = ("Eurostile", "Microgramma", "Square721 BT", "Bank Gothic",
                "Verdana", "DejaVu Sans", "Futura", "Gill Sans",
                "Avenir Next", "Liberation Sans", "Helvetica Neue")

    @staticmethod
    def _lettering(font):
        """Put the dial's own face on a font, if it loaded."""
        family = dial_face()
        font.setFamilies(([family] if family else [])
                         + list(Meters.FAMILIES))
        # Michroma has one weight; asking for bold makes Qt smear it.
        font.setBold(not family)
        return font
    #: Where the dB numbers sit, clear of whichever of the arc and the ticks
    #: reaches further.
    DB_AT_R = 1.12
    DB_TYPE = 0.072
    #: Nearly as large as the dB row: two scales on one face.
    PERCENT_TYPE = 0.062
    UNIT_TYPE = 0.088
    LABEL_TYPE = 0.096

    #: Where 0 dB (also 100 per cent) sits along the travel, with +3 dB at the
    #: end. A VU movement is linear in voltage.
    _FULL = 1.0 / (10.0 ** (3.0 / 20.0))

    @staticmethod
    def _db_at(db: float) -> float:
        return (10.0 ** (db / 20.0)) * Meters._FULL

    #: dB marks along the arc, and where each one sits across the sweep.
    DB_MARKS = tuple((db, (10.0 ** (db / 20.0)) / (10.0 ** (3.0 / 20.0)))
                     for db in (-24, -12, -3, 0, 1, 2, 3))
    #: Where the per-cent row sits, inside the arc.
    PERCENT_AT = 0.82

    #: Below this face radius the per-cent row is dropped as unreadable.
    PERCENT_RADIUS = 76.0
    #: And below this, the face shows only what it can show clearly.
    ROOMY = 62.0
    #: The three numbers worth keeping when there is no room for seven.
    SPARSE_MARKS = ((-24, 0.0447), (0, 0.7079), (3, 1.0))
    #: Per-cent marks on the inside, linear in deflection.
    PERCENT_MARKS = tuple((pc, pc / 100.0 / (10.0 ** (3.0 / 20.0)))
                          for pc in (0, 20, 40, 60, 80, 100))
    #: One dot between each neighbouring pair of numbered marks, thinned where
    #: two nearly coincide.
    DOTS = _dots_between(DB_MARKS, PERCENT_MARKS)
    #: Where the dots go: midway between each pair of numbered marks, on both
    #: scales.
    @staticmethod
    def _between(marks):
        at = sorted(set(marks))
        return [(a + b) / 2.0 for a, b in zip(at, at[1:])]

    #: Kept as an empty tuple for anything that still names it.
    MINOR: tuple = ()

    def __init__(self) -> None:
        self._faces: dict = {}

    def paint(self, painter, rect, state) -> None:
        painter.fillRect(rect, state.background)
        levels = state.dials or state.levels
        count = len(levels)
        if not count:
            return
        columns, rows = self._grid(rect, count)
        # Cells the size of a face, with the block centred, rather than the
        # frame divided evenly.
        radius = self._radius(rect, columns, rows)
        cell_w = min(rect.width() / columns, radius * self.FACE_WIDE * 1.06)
        cell_h = min(rect.height() / rows, radius * self.FACE_TALL * 1.06)
        left = rect.left() + (rect.width() - cell_w * columns) / 2.0
        top = rect.top() + (rect.height() - cell_h * rows) / 2.0
        #: How many are on each row, so the last one can be centred.
        on_row = [min(columns, count - r * columns) for r in range(rows)]
        boxes = []
        flash = self.flash(state)
        # Real pixels per unit of this rect, so a face is rendered at the size
        # it is shown.
        dpr = abs(painter.combinedTransform().m11()) or 1.0
        for index in range(count):
            row, column = divmod(index, columns)
            # A short row is centred.
            spare = (columns - on_row[row]) * cell_w / 2.0
            box = QRectF(left + spare + column * cell_w,
                         top + row * cell_h,
                         cell_w, cell_h)
            label = (state.dial_labels[index]
                     if index < len(state.dial_labels) else "")
            boxes.append((box, levels[index], label))

        # All the backlights first, over the whole frame: a lamp reaches past
        # its own cell, and filled inside it, it was cut off at the edge.
        if flash > 0.02:
            # A wash over the whole frame, so the lamps sit in lit air.
            wash = QColor(state.dial_colour)
            wash.setAlphaF(min(1.0, 0.16 * flash))
            painter.fillRect(rect, wash)
            for box, _value, _label in boxes:
                self._backlight(painter, rect, box, state, flash)
        for box, value, label in boxes:
            self._meter(painter, box, value, label, state, flash, dpr)

    def _backlight(self, painter, rect, box, state, flash) -> None:
        """One meter's lamp coming up, with its reach cut to what fits in the
        frame, so a lamp near an edge is smaller rather than clipped.
        """
        geometry = self._geometry(QRectF(0, 0, box.width(), box.height()))
        pivot = geometry["pivot"] + box.topLeft()
        room = min(pivot.x() - rect.left(), rect.right() - pivot.x(),
                   pivot.y() - rect.top(), rect.bottom() - pivot.y())
        reach = max(geometry["radius"] * 0.5,
                    min(geometry["radius"] * 1.5, room))
        glow = QRadialGradient(pivot, reach)
        tint = QColor(state.dial_colour)
        tint.setAlphaF(0.55 * flash)
        glow.setColorAt(0.0, tint)
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        spill = QRectF(pivot.x() - reach, pivot.y() - reach,
                       reach * 2, reach * 2).intersected(rect)
        painter.fillRect(spill, glow)

    @staticmethod
    def _radius(rect, columns: int, rows: int) -> float:
        """The biggest face that fits a cell of this grid."""
        cell_w = rect.width() / max(1, columns)
        cell_h = rect.height() / max(1, rows)
        pad = min(cell_w, cell_h) * 0.05
        return max(1.0, min((cell_w - 2 * pad) / Meters.FACE_WIDE,
                            (cell_h - 2 * pad) / Meters.FACE_TALL))

    @staticmethod
    def _grid(rect, count: int):
        """The columns and rows that make the faces biggest, scored on the
        radius each gives. A short row is allowed and centred.
        """
        best = (1, count, 0.0)
        for columns in range(1, count + 1):
            rows = (count + columns - 1) // columns
            left = count - (rows - 1) * columns
            # Never one meter alone on the last row.
            if rows > 1 and left == 1:
                continue
            cell_w = rect.width() / columns
            cell_h = rect.height() / rows
            if cell_w <= 48 or cell_h <= 34:
                continue
            radius = Meters._radius(rect, columns, rows)
            # A nudge towards filling the grid, so the tidier of two near-equal
            # arrangements wins.
            radius *= 1.0 - 0.02 * (columns * rows - count)
            if radius > best[2]:
                best = (columns, rows, radius)
        return best[0], best[1]

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
        self._needle(painter, geometry, value, state)
        painter.restore()

    @staticmethod
    def _geometry(box) -> dict:
        """Where the arc, the pivot and the text go inside one cell, worked out
        from the cell so nothing reaches outside it.
        """
        pad = min(box.width(), box.height()) * 0.05
        inner = box.adjusted(pad, pad, -pad, -pad)
        radius = min(inner.width() / Meters.FACE_WIDE,
                     inner.height() / Meters.FACE_TALL)
        # Centred in the spare room both ways.
        block = radius * Meters.FACE_TALL
        top = inner.top() + max(0.0, (inner.height() - block) / 2.0)
        centre_x = inner.center().x()
        centre_y = top + radius * Meters.FACE_DROP
        return {
            "radius": radius,
            "centre": QPointF(centre_x, centre_y),
            # Hinged at the arc's own centre, as on the reference.
            "pivot": QPointF(centre_x, centre_y),
            "inner": inner,
        }

    def _render_face(self, box, label, state, dpr: float = 1.0):
        from PySide6.QtGui import QPixmap

        # Rendered at one and a half times its shown size, capped, so thin
        # strokes and small numbers are not aliased.
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
        # Two arcs: the scale up to 0 dB, and a heavier red zone above it.
        zero = self._db_at(0.0)
        span = QRectF(centre.x() - radius * self.ARC_AT,
                      centre.y() - radius * self.ARC_AT,
                      radius * self.ARC_AT * 2, radius * self.ARC_AT * 2)
        painter.setPen(QPen(colour, max(1.0, radius * self.ARC_STROKE),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(span, int(self.START * 16),
                        int(self.SWEEP * zero * 16))
        painter.setPen(QPen(colour, max(1.3, radius * self.ARC_STROKE_HOT),
                            Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
        painter.drawArc(span, int((self.START + self.SWEEP * zero) * 16),
                        int(self.SWEEP * (1.0 - zero) * 16))

        # A small face drops what it cannot show legibly.
        roomy = radius >= self.ROOMY
        marks = self.DB_MARKS if roomy else self.SPARSE_MARKS

        for _value, fraction in marks:
            self._tick(painter, centre, radius, fraction, colour,
                       self.TICK_IN, self.TICK_OUT,
                       max(1.0, radius * self.ARC_STROKE))
        if roomy:
            # Dots, not lines.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(dim)
            size = max(0.7, radius * self.DOT_SIZE)
            for fraction in self.DOTS:
                angle = self._angle(fraction)
                painter.drawEllipse(
                    QPointF(centre.x() + math.cos(angle) * radius * self.DOT_AT,
                            centre.y() - math.sin(angle) * radius * self.DOT_AT),
                    size, size)
            painter.setBrush(Qt.BrushStyle.NoBrush)

        font = self._lettering(painter.font())
        # Sized against the radius, the same proportion at every size.
        font.setPointSizeF(max(6.0, radius * self.DB_TYPE))
        painter.setFont(font)
        painter.setPen(QPen(colour))
        for value, fraction in marks:
            self._label(painter, centre, radius * self.DB_AT_R, fraction,
                        str(value))
        # The per-cent row waits for a face big enough to carry it.
        if radius >= self.PERCENT_RADIUS:
            # Out near the arc and small, so the numbers do not run into each
            # other.
            font.setPointSizeF(max(4.0, radius * self.PERCENT_TYPE))
            painter.setFont(font)
            # Dimmer than the dB row.
            inside = QColor(colour)
            inside.setAlphaF(0.62)
            painter.setPen(QPen(inside))
            for value, fraction in self.PERCENT_MARKS:
                self._label(painter, centre, radius * self.PERCENT_AT,
                            fraction, str(value), tight=True)
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
        # The box the text is centred in: narrower than the gap to its
        # neighbour, but never narrower than the text.
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
        # Short of the arc, measured from its centre.
        tip = centre + reach * (radius * 0.90)
        # Stopped short of the hinge, with no hub, as on the reference.
        tail = pivot + reach * (radius * 0.15)
        # A tapered blade in three strokes, broad at the hinge and fine where
        # it is read; no halo.
        for start, finish, width in ((0.0, 0.45, 0.032),
                                     (0.40, 0.78, 0.023),
                                     (0.74, 1.0, 0.015)):
            painter.setPen(QPen(state.dial_colour, max(1.1, radius * width),
                                Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap))
            painter.drawLine(tail + (tip - tail) * start,
                             tail + (tip - tail) * finish)
        painter.setBrush(Qt.BrushStyle.NoBrush)


class Ambience(Scene):
    """Smooth ribbons from the middle outwards, mirrored top to bottom, each
    riding part of the spectrum, drawn additively so they brighten where
    they cross.
    """

    name = "Ambience"
    blurb = "mirrored ribbons folding over each other, as it was in 2001"

    #: How many ribbons, and how many points along each.
    RIBBONS = 5
    STEPS = 44

    #: How much of the strobe goes into the shape; the rest is light, so the
    #: ribbons brighten rather than lurch.
    BLOOM_SHAPE = 0.16

    def __init__(self) -> None:
        self._plasma = Plasma()
        self._bloom = 0.0

    def _ease(self, state) -> float:
        return self.bloom(state)

    def paint(self, painter, rect, state) -> None:
        width, height = rect.width(), rect.height()
        middle = height * 0.5
        painter.fillRect(rect, QColor(3, 2, 8))
        flash = self._ease(state)
        # The morphing field behind everything, dim enough that the ribbons
        # stay the bright thing.
        self._plasma.paint(painter, rect, state,
                           strength=0.55 + flash * 0.28, flash=flash)
        levels = state.levels
        if not levels:
            return

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.save()
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Plus)
        for ribbon in range(self.RIBBONS):
            share = ribbon / max(1, self.RIBBONS - 1)
            # Each ribbon listens to its own quarter of the spectrum.
            low = int(share * (len(levels) - 1) * 0.75)
            band = sum(levels[low:low + 4]) / max(1, len(levels[low:low + 4]))
            reach = middle * (0.18 + band * 0.74
                              + flash * self.BLOOM_SHAPE)
            turn = state.phase * (0.7 + ribbon * 0.23)
            shade = (state.hue + share * 0.42 + 0.1) % 1.0
            colour = QColor.fromHsvF(shade, 0.72 - flash * 0.42, 1.0,
                                     min(1.0, 0.30 + band * 0.45
                                         + flash * 0.34))
            # Capped: additive compositing is charged per pixel of stroke.
            thick = max(1.6, min(9.0, height * 0.010
                                  * (0.6 + band + flash * 0.5)))
            for side in (+1, -1):
                path = self._ribbon_path(width, middle, reach, turn,
                                         ribbon, side)
                stroke(painter, path, colour, thick)
        painter.restore()
        self._core(painter, width, middle, state, flash)

    #: Kept as a name on the scene, which a test uses; the curve is shared with
    #: the oscilloscope.
    _smooth = staticmethod(smooth_path)

    def _ribbon_path(self, width, middle, reach, turn, index, side):
        """One curve, mirrored by ``side``: two sines of different periods."""
        points = []
        for step in range(self.STEPS + 1):
            across = step / self.STEPS
            x = across * width
            wave = (math.sin(across * math.tau * 1.4 + turn)
                    * 0.66
                    + math.sin(across * math.tau * 2.7 - turn * 1.3 + index)
                    * 0.34)
            # Pinched at both ends, so the ribbons meet rather than run off the
            # frame.
            pinch = math.sin(across * math.pi) ** 0.7
            points.append(QPointF(x, middle + side * wave * reach * pinch))
        return smooth_path(points)


    def _core(self, painter, width, middle, state, flash) -> None:
        """A soft line along the middle, brightest where the bass is."""
        glow = QLinearGradient(0.0, middle, width, middle)
        shade = (state.hue + 0.5) % 1.0
        edge = QColor.fromHsvF(shade, 0.6, 1.0, 0.0)
        centre = QColor.fromHsvF(shade, 0.25, 1.0,
                                 min(1.0, 0.25 + state.bass * 0.5
                                     + flash * 0.45))
        glow.setColorAt(0.0, edge)
        glow.setColorAt(0.5, centre)
        glow.setColorAt(1.0, edge)
        painter.setPen(QPen(glow, max(1.0, middle * 0.012)))
        painter.drawLine(QPointF(0.0, middle), QPointF(width, middle))


class Waterfall(Scene):
    """A spectrum analyser as a landscape seen from one corner: frequency
    across, time away from you, loudness as height. An oblique projection,
    coloured by height in a few bands, one path each.
    """

    name = "Waterfall"
    blurb = "the spectrum as a landscape, running away from you"

    #: Frames kept on screen, about three quarters of a second at sixty.
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
        # Every row, at every size; the hairline pen made them cheap.
        field = field[-self.DEPTH:]

        flash = self.flash(state)
        # The plot sits in the lower left, with gutters for the axes.
        left = max(38.0, width * 0.05)
        foot = max(30.0, height * 0.075)
        plot_w = (width - left) * (1.0 - self.SKEW_X) * 0.98
        plot_h = (height - foot) * (1.0 - self.SKEW_Y) * 0.80
        origin_x = left
        origin_y = height - foot
        # The strobe lights the landscape rather than moving it.
        rise = plot_h

        self._floorplan(painter, rect, origin_x, origin_y, plot_w, flash)

        buckets = [QPainterPath() for _ in self.SHADES]
        total = len(field)
        for depth, row in enumerate(field):
            # Oldest at the back, drawn first, with the rows spread over the
            # whole plot.
            back = (total - 1 - depth) / max(1, total - 1)
            offset_x = back * width * self.SKEW_X
            offset_y = back * height * self.SKEW_Y
            count = len(row)
            previous = None
            # Which height band the previous segment went into, so a row is
            # drawn as a few joined runs rather than one capped subpath per
            # band.
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
                    # A curve between the two, with control points level with
                    # each end, so the ridges are rounded rather than zig-zag.
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
            # One pass a pixel wide, with the width carried as light: a stack
            # of hairlines over thousands of segments cost twice as much.
            wide = 1.0 + share * 1.4 + flash * 0.4
            lit = QColor(colour)
            lit.setAlphaF(min(1.0, lit.alphaF() * wide))
            pen = QPen(lit, 0.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
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
        """Frequency along the front, level up the side and time going back,
        all in a gutter outside the plot.
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

        # Every fourth band along the front, never two in the same place.
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
        # One caption, in the empty triangle left of the depth edge.
        painter.setPen(QPen(dim))
        seconds = self.DEPTH / 60.0
        painter.drawText(
            QRectF((origin_x + back_x) / 2.0 - 104,
                   (origin_y + back_y) / 2.0 - 8, 96, 15),
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
            f"time  −{seconds:.1f}s")


#: Every theme, in the order the picker offers them.
class Rave(Scene):
    """A room lit by the drums, seen from inside it. The kick pushes the room,
    the snare sends a ring and turns the colour, the hats flick beams along
    the grid, the bass opens the corridor, and the synth sets the colour.
    Perspective is one divide per point, drawn back to front, a path per
    line.
    """

    name = "Rave"
    blurb = "a room lit by the kit: kick, snare, hats and synth, in 3D"

    #: How far down the corridor the grid runs, and how finely.
    DEPTH = 26
    ACROSS = 9
    #: Nearest and furthest z; a point at z=0 would project to infinity.
    NEAR = 0.55
    FAR = 15.0
    #: How fast the world comes towards you at rest, in z per second.
    DRIFT = 2.6

    #: How hard a full bass front-loads the travel within a beat, as a multiple
    #: of the average speed: ``1 - (1 - t) ** k`` has slope exactly k at the
    #: start, so the lunge is as hard as it says and the room never goes
    #: backwards.
    SURGE = 1.6

    #: How quickly the room notices that the track has stopped.
    GOING_EASE = 0.18

    #: How fast the push behind the room follows the bass.
    PUSH_RISE, PUSH_FALL = 0.22, 0.045

    #: A ring marks a big moment: the room getting louder than it has been over
    #: the last several seconds, as at a drop.
    QUICK_RISE, QUICK_FALL = 0.40, 0.03
    CALM_RATE = 0.010
    #: How much louder than the last several seconds counts as a moment, how
    #: quiet the room can be and still have one, and how long before another.
    RING_OVER = 1.30
    RING_QUIET = 0.04
    RING_WAIT = 0.45
    #: Seconds of listening before the first ring can fire.
    RING_SETTLE = 1.5

    THUMP_RISE, THUMP_FALL = 0.34, 0.075
    CRACK_RISE, CRACK_FALL = 0.85, 0.22
    WASH_RISE, WASH_FALL = 0.30, 0.030
    FIZZ_RISE, FIZZ_FALL = 0.55, 0.16

    def __init__(self) -> None:
        self._z = 0.0
        self._spin = 0.0
        #: What the trusses are built from (see TRUSS_NEAR), filled once a
        #: frame.
        self._chart: dict = _NO_CHART
        self._said = 0.0
        self._per_beat = 0.0
        self._beats_now = 0.0
        self._coming: dict = {}
        self._last = None
        self._rings: list = []
        #: How far through its sweep the laser rig is.
        self._fan = 0.0
        self._haze_key = None
        self._haze_image = None
        #: The drums, smoothed: the kick pushes the room, the snare washes its
        #: colour, the hats shake the middle.
        self._thump = 0.0
        self._crack = 0.0
        self._beat_was = None
        self._beat_count = 0.0
        self._wash = 0.0
        self._wash_hue = 0.0
        self._fizz = 0.0
        #: How loud the room is over the last breath and over the last several
        #: seconds; see RING_OVER.
        self._push = 0.0
        #: 1 while the track is going, 0 while it is paused.
        self._going = 1.0
        self._lunge_held = 1.0
        self._peak = 0.0
        self._quiet = None
        self._quick = 0.0
        self._calm = None
        self._loud = 0.0
        self._ring_wait = 0.0
        self._heard_for = 0.0

    def _beats_done(self, state) -> float:
        """How many beats have gone by, counting fractions: a running total,
        because the pane's phase wraps at every beat.
        """
        at = getattr(state, "beat_at", 0.0)
        if self._beat_was is None:
            self._beat_was = at
        step = at - self._beat_was
        if step < -0.5:
            step += 1.0          # it wrapped past the beat
        elif step < 0.0:
            step = 0.0           # a seek backwards: hold still for a frame
        self._beat_was = at
        self._beat_count += step
        return self._beat_count

    def _advance(self, state) -> float:
        """Move the world on by the clock, so the room travels at the same
        speed at any frame rate.
        """
        now = time.monotonic()
        step = 0.016 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now
        # Nothing travels under a stopped track; eased, so a pause is a stop
        # rather than a freeze.
        self._going += ((1.0 if getattr(state, "moving", True) else 0.0)
                        - self._going) * self.GOING_EASE
        step *= self._going

        def ease(was, to, rise, fall):
            return was + (to - was) * (rise if to > was else fall)

        kit = state.kit
        self._thump = ease(self._thump, kit.get("Kick", 0.0),
                           self.THUMP_RISE, self.THUMP_FALL)
        # A much faster envelope off the same kick, for the shake.
        self._crack = ease(self._crack, kit.get("Kick", 0.0),
                           self.CRACK_RISE, self.CRACK_FALL)
        self._fizz = ease(self._fizz, kit.get("Hats", 0.0),
                          self.FIZZ_RISE, self.FIZZ_FALL)
        snare = kit.get("Snare", 0.0)
        if snare > self._wash + 0.12:
            # Each snare moves the colour on a step, and it stays there.
            self._wash_hue = (self._wash_hue + 0.13 + snare * 0.09) % 1.0
        self._wash = ease(self._wash, snare, self.WASH_RISE, self.WASH_FALL)

        # Speed is the bass.
        bass = max(state.bass, kit.get("Bass", 0.0))
        self._push = ease(self._push, bass, self.PUSH_RISE, self.PUSH_FALL)
        tempo = getattr(state, "tempo", 0.0)
        # Kept once a frame rather than read per truss.
        self._chart = getattr(state, "chart", None) or _NO_CHART
        self._said = getattr(state, "at", 0.0) or 0.0
        self._per_beat = 60.0 / tempo if tempo > 0.0 else 0.0
        self._beats_now = self._beats_done(state) if tempo > 0.0 else 0.0
        self._coming.clear()
        if tempo > 0.0:
            # One truss passes every beat, exactly. The bass changes how the
            # beat's distance is spent: evenly at rest, mostly at the start
            # under a heavy bass.
            beats = self._beats_done(state)
            lunge = 1.0 + self._push * self.SURGE
            whole = math.floor(beats)
            through = beats - whole
            # The curve is frozen with the track.
            if self._going > 0.02:
                self._lunge_held = lunge
            went = 1.0 - (1.0 - through) ** self._lunge_held
            # Never backwards.
            self._z = max(self._z, (whole + went) * self.TRUSS)
        else:
            self._z += step * self.DRIFT * (1.0 + self._push * 3.4
                                            + self._thump * 0.9)
        self._spin += step * (0.25 + kit.get("Synth", 0.0) * 1.1
                              + self._fizz * 2.2)

        # How loud the room is now against how loud it has been.
        loud = (bass + state.mid + state.high) / 3.0
        self._loud = loud
        self._quick = ease(self._quick, loud,
                           self.QUICK_RISE, self.QUICK_FALL)
        if self._calm is None:
            # Seeded from the first frame, or the start of every track reads as
            # a moment.
            self._calm = loud
        self._calm += (loud - self._calm) * self.CALM_RATE
        # The loudest the room has been lately, from the eased level so one
        # frame cannot set it.
        self._peak = max(self._quick, self._peak * self.PEAK_FALL)
        if self._quiet is None:
            # Seeded from the frame's own loudness, so an intro does not read
            # as a drop.
            self._quiet = loud
        self._quiet += (self._quick - self._quiet) * (
            self.QUIET_DOWN if self._quick < self._quiet else self.QUIET_UP)
        self._ring_wait = max(0.0, self._ring_wait - step)
        self._heard_for += step
        return step

    def _big_moment(self) -> float:
        """How much of a moment this frame is, 0 to 1: zero unless the room is
        louder than it has been, and for RING_WAIT seconds after a ring.
        """
        if (self._ring_wait > 0.0 or (self._calm or 0.0) < self.RING_QUIET
                or self._heard_for < self.RING_SETTLE):
            return 0.0
        over = self._quick / max(1e-6, self._calm)
        if over < self.RING_OVER:
            return 0.0
        self._ring_wait = self.RING_WAIT
        # The moment becomes the new normal, so one drop fires one ring.
        self._calm = max(self._calm, self._loud * 0.92)
        return max(0.35, min(1.0, (over - self.RING_OVER) * 1.4))

    def paint(self, painter, rect, state) -> None:
        step = self._advance(state)
        # The eased kick wherever the room moves; the raw hats fire the beams.
        kick = self._thump
        hats = state.kit.get("Hats", 0.0)
        synth = state.kit.get("Synth", 0.0)
        bass = max(state.kit.get("Bass", 0.0), state.bass)
        flash = self.bloom(state)

        painter.fillRect(rect, QColor(3, 2, 8))
        centre = rect.center()
        # The kick pushes the horizon away and pulls the walls in.
        span = min(rect.width(), rect.height())
        focal = span * (0.62 - kick * 0.10)
        horizon = QPointF(centre.x(),
                          centre.y() - rect.height() * (0.02 + kick * 0.05))
        hue = (self._spin * 0.11 + synth * 0.22 + self._wash_hue) % 1.0

        self._haze(painter, rect, horizon, bass, synth, flash)
        self._grid(painter, rect, horizon, focal, hue, bass, kick, flash)
        self._rings_now(painter, rect, horizon, focal, self._big_moment(),
                        step, hue, flash)
        self._beams_now(painter, rect, horizon, focal, hats, step, hue,
                        bass, flash)
        self._core(painter, horizon, span, hue, bass, kick, synth, flash,
                   self._weight(rect))

    #: How big the haze is painted before being stretched over the frame: a
    #: gradient has no detail to lose, and a full-frame one costs fill rate.
    HAZE = 128

    def _haze(self, painter, rect, horizon, bass, synth, flash) -> None:
        """The air in the room, lit from the far end."""
        small = self._haze_tile(rect, horizon, bass, synth, flash)
        if small is None:
            return
        # Smoothly, or the tile's pixels show.
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, small, QRectF(small.rect()))
        painter.restore()

    #: How far round the wheel the air's second colour sits from its first.
    HAZE_TURN = 0.46
    #: How far the wash reaches, as a share of the frame.
    HAZE_REACH = 1.9

    #: The tallest the air is laid out, as height over width; a taller frame
    #: gets this shape's air, stretched, so a full screen keeps the strip's
    #: colour.
    HAZE_TALLEST = 0.3
    #: How much deeper the wash is in a frame much taller than a strip.
    HAZE_DEEPER = 0.25

    def _haze_tile(self, rect, horizon, bass, synth, flash):
        """The air in the room, painted small and stretched: a wash warmer at
        the floor, a second colour a third of the wheel away, and a hot
        core. Rebuilt when it changes enough to see; a rebuild costs
        microseconds.
        """
        if rect.width() < 2 or rect.height() < 2:
            return None
        hue = (0.62 + synth * 0.3) % 1.0
        key = (round(hue, 2),
               # Brightness, over a range that keeps the colour from going
               # black or white.
               round(min(1.0, 0.38 + bass * 0.24 + flash * 0.13), 2),
               round(min(1.0, 0.52 + bass * 0.26), 2),
               round(0.58 + bass * 0.35, 2),
               round((horizon.x() - rect.left()) / rect.width(), 2),
               round((horizon.y() - rect.top()) / rect.height(), 2),
               round(min(1.0, 0.30 + bass * 0.34 + flash * 0.25), 2),
               # How deep the colour runs: the bass makes the room vivid.
               round(min(1.30, 0.80 + bass * 0.50), 2))
        if self._haze_key == key and self._haze_image is not None:
            return self._haze_image
        (shade, value, alpha, spread, across, down, second, deep) = key

        def rich(base: float) -> float:
            """A saturation, taken as deep as the bass asks."""
            return max(0.0, min(1.0, base * deep))
        # Laid out in the frame's own shape, so the lamps are round.
        size = QSize(self.HAZE, max(2, int(self.HAZE * min(
            rect.height() / max(1.0, rect.width()), 2.0))))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        wide, tall = size.width(), size.height()
        # What the lamps are sized by: the height of a strip this wide, or the
        # frame's own if shorter.
        lamp = min(wide, tall, wide * self.HAZE_TALLEST)
        middle = QPointF(across * wide, down * tall)
        box = QRectF(0, 0, wide, tall)
        other = (shade + self.HAZE_TURN) % 1.0
        into = QPainter(image)
        try:
            into.setPen(Qt.PenStyle.NoPen)

            # The room's light, warmer at the floor, and deeper in a frame
            # taller than a strip.
            fill = 1.0 + self.HAZE_DEEPER * max(0.0, min(1.0, (
                tall / wide - self.HAZE_TALLEST) / self.HAZE_TALLEST))
            wash = QLinearGradient(0.0, 0.0, 0.0, tall)
            wash.setColorAt(0.0, QColor.fromHsvF(other, rich(0.92),
                                                 min(1.0, value * 0.52 * fill),
                                                 min(1.0, alpha * 0.80 * fill)))
            wash.setColorAt(down, QColor.fromHsvF(shade, rich(0.80),
                                                  min(1.0, value * 0.34 * fill),
                                                  min(1.0, alpha * 0.34 * fill)))
            wash.setColorAt(1.0, QColor.fromHsvF((shade + 0.12) % 1.0,
                                                 rich(0.88),
                                                 min(1.0, value * 0.86 * fill),
                                                 min(1.0, alpha * 0.86 * fill)))
            into.setBrush(wash)
            into.drawRect(box)

            # A second lamp, off to one side, in the other colour.
            away = QRadialGradient(
                QPointF(middle.x() - wide * 0.22, middle.y() + lamp * 0.10),
                max(1.0, lamp * spread * self.HAZE_REACH))
            away.setColorAt(0.0, QColor.fromHsvF(other, rich(0.74),
                                                 min(1.0, value * 1.15),
                                                 second))
            away.setColorAt(0.55, QColor.fromHsvF((other + 0.08) % 1.0,
                                                  rich(0.95),
                                                  value * 0.7, second * 0.45))
            away.setColorAt(1.0, QColor(0, 0, 0, 0))
            into.setBrush(away)
            into.drawRect(box)

            # And the light at the end of it.
            glow = QRadialGradient(middle, max(1.0, lamp * spread * 2.0))
            # The lamp itself, the one place nearly white.
            glow.setColorAt(0.0, QColor.fromHsvF(shade, rich(0.34),
                                                 min(1.0, value * 1.25),
                                                 min(1.0, alpha * 1.05)))
            glow.setColorAt(0.18, QColor.fromHsvF(shade, rich(0.78),
                                                  value, alpha))
            glow.setColorAt(0.55, QColor.fromHsvF((shade + 0.10) % 1.0,
                                                  rich(0.94),
                                                  value * 0.7, alpha * 0.45))
            glow.setColorAt(1.0, QColor(0, 0, 0, 0))
            into.setBrush(glow)
            into.drawRect(box)
        finally:
            into.end()
        self._haze_key = key
        self._haze_image = image
        return image

    def _project(self, horizon, focal, x: float, y: float, z: float):
        """One point of the world, on the glass."""
        if z < self.NEAR:
            z = self.NEAR
        return QPointF(horizon.x() + focal * x / z, horizon.y() + focal * y / z)

    #: How many depth bands the grid is drawn in, so the corridor fades into
    #: the haze.
    BANDS = 4
    #: Every this many rows, a truss around the corridor.
    TRUSS = 5

    #: A truss is the beat it belongs to: one passes every beat, so a truss
    #: five away is five beats ahead, and the chart says what is on it. A kick
    #: swells the frame; a snare turns its colour. How near a hit must be to
    #: the beat, as a share of one, and how much a kick swells it.
    TRUSS_NEAR = 0.40
    TRUSS_SWELL = 0.22
    TRUSS_TURN = 0.10

    #: Lines across a side wall: far fewer than the floor's, as the corridor is
    #: much wider than tall.
    UPRIGHTS = 3

    #: How far the floor and ceiling are from the eye, and how much further the
    #: bass pushes them. One definition, so the beams land on the floor the
    #: grid drew.
    LIFT_AT_REST = 0.55
    LIFT_ON_BASS = 0.22

    @classmethod
    def _lift(cls, bass: float) -> float:
        return cls.LIFT_AT_REST + bass * cls.LIFT_ON_BASS

    def _surfaces(self, lift, span):
        """The corridor's four walls, as one grid turned four ways."""
        # The two side walls are one surface with two faces, drawn as one path.
        def walls(across):
            return ((-span if across < 0 else span),
                    (abs(across) * 2.0 - 1.0) * lift)

        return (
            # across -1..1        ->  (x, y)        hue    lines
            (lambda t: (t * span, lift), 0.00, self.ACROSS),      # floor
            (lambda t: (t * span, -lift), 0.08, self.ACROSS),     # ceiling
            (walls, 0.16, self.UPRIGHTS * 2),                     # both sides
        )

    def _grid(self, painter, rect, horizon, focal, hue, bass, kick, flash):
        """The corridor: four surfaces in depth bands, near lines bright and
        heavy, far ones fading, with trusses.
        """
        painter.setBrush(Qt.BrushStyle.NoBrush)
        weight = self._weight(rect)
        glow = self._glow(rect)
        lift = self._lift(bass)
        span = self.ACROSS * 0.5
        # Counted down, so the room travels towards you between wraps.
        offset = 1.0 - (self._z % 1.0)
        reach = self.FAR - self.NEAR
        for place, shift, lines in self._surfaces(lift, span):
            base = QColor.fromHsvF(
                (hue + shift) % 1.0, 0.85 - flash * 0.4, 1.0, 1.0)
            # The lines running away from you are drawn whole.
            away = QPainterPath()
            for column in range(-lines, lines + 1):
                across = column / lines
                x, y = place(across)
                away.moveTo(self._project(horizon, focal, x, y, self.NEAR))
                away.lineTo(self._project(horizon, focal, x, y, self.FAR))
            self._beam(painter, away,
                       self._shade(base, min(1.0, (0.20 + bass * 0.30
                                             + kick * 0.26
                                             + flash * 0.26) * glow)))

            # And the ones across it, marching towards you, in bands.
            for band in range(self.BANDS):
                path = QPainterPath()
                # A slice of the corridor's depth, not every fourth row of all
                # of it.
                first = band * self.DEPTH // self.BANDS
                last = (band + 1) * self.DEPTH // self.BANDS
                for row in range(first, last):
                    z = self.NEAR + (row + offset) * reach / self.DEPTH
                    # In two halves, so the pair of walls is not joined across
                    # the room.
                    for lo, hi in ((-1.0, -0.002), (0.002, 1.0)):
                        path.moveTo(
                            self._project(horizon, focal, *place(lo), z))
                        path.lineTo(
                            self._project(horizon, focal, *place(hi), z))
                # Further bands are dimmer and thinner, squared.
                near = 1.0 - band / self.BANDS
                self._beam(painter, path,
                           self._shade(base,
                                       min(1.0, (0.10 + bass * 0.26
                                                 + kick * 0.24
                                                 + flash * 0.24) * glow
                                           * (0.14 + near * near))))

        self._trusses(painter, horizon, focal, hue, lift, span, bass, kick,
                      flash, reach, weight, glow)

    #: The frame size this scene's line weights were chosen at.
    DRAWN_FOR = 700.0

    #: What the line width used to carry, as light instead.
    BEAM_LIFT = 1.6

    #: The trusses' alpha carries the width they would have had; ``_beam`` adds
    #: BEAM_LIFT on top, so this takes it back out.
    TRUSS_LIFT = 1.0 / BEAM_LIFT

    #: How much of the contrast a big frame gets back as light.
    LIFT = 1.15

    @classmethod
    def _glow(cls, rect) -> float:
        """How much brighter to draw, for a frame this size."""
        return 1.0 + (cls._weight(rect) - 1.0) * cls.LIFT

    @classmethod
    def _weight(cls, rect) -> float:
        """How thick to draw for a frame this size, so a full screen keeps the
        window's contrast.
        """
        return max(0.75, min(1.30, rect.height() / cls.DRAWN_FOR))

    @staticmethod
    def _shade(base, alpha: float):
        colour = QColor(base)
        colour.setAlphaF(max(0.0, min(1.0, alpha)))
        return colour

    @staticmethod
    def _beam(painter, path, colour) -> None:
        """One line of the room: one pixel wide, one pass, its weight carried
        by alpha. A laser grid's lines are thin and bright; at 1080p this
        costs 7.4 ms a frame against 32 for stacked hairlines. BEAM_LIFT
        restores the contrast the width gave.
        """
        lit = QColor(colour)
        lit.setAlphaF(min(1.0, lit.alphaF() * Rave.BEAM_LIFT))
        pen = QPen(lit, 0.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)

    def _on_beat(self, index: int) -> dict:
        """What the drums play on the beat a truss belongs to, read forward
        from the chart and cached per frame.
        """
        if index in self._coming:
            return self._coming[index]
        found: dict = {}
        if self._chart and self._per_beat > 0.0:
            when = self._said + (index - self._beats_now) * self._per_beat
            reach = self._per_beat * self.TRUSS_NEAR
            for name in ("Kick", "Snare", "Hats"):
                times = self._chart.get(name)
                if not times:
                    continue
                at = bisect.bisect_left(times, when)
                near = min((abs(times[i] - when)
                            for i in (at - 1, at) if 0 <= i < len(times)),
                           default=None)
                if near is not None and near <= reach:
                    found[name] = 1.0 - near / reach
        self._coming[index] = found
        return found

    def _trusses(self, painter, horizon, focal, hue, lift, span, bass, kick,
                 flash, reach, weight, glow) -> None:
        """A frame round the corridor every few metres, coming at you,
        brightest nearest.
        """
        # The beat the nearest truss belongs to; the one k slots away is k
        # beats on.
        first = math.ceil(self._z / self.TRUSS) if self._per_beat > 0.0 else 0
        # On their own clock, counted down, so the nearest truss closes on you
        # through the beat and the next takes its place.
        offset = self.TRUSS - (self._z % self.TRUSS)
        for step in range(0, self.DEPTH, self.TRUSS):
            row = step + offset
            if row >= self.DEPTH:
                continue
            z = self.NEAR + row * reach / self.DEPTH
            near = max(0.0, 1.0 - (z - self.NEAR) / reach)
            # What is on this one's beat; see TRUSS_NEAR.
            coming = self._on_beat(first + step // self.TRUSS)
            swell = 1.0 + coming.get("Kick", 0.0) * self.TRUSS_SWELL
            wide, tall = span * swell, lift * swell
            corners = ((-wide, tall), (wide, tall),
                       (wide, -tall), (-wide, -tall))
            path = QPainterPath()
            start = None
            for x, y in corners:
                point = self._project(horizon, focal, x, y, z)
                if start is None:
                    path.moveTo(point)
                    start = point
                else:
                    path.lineTo(point)
            path.lineTo(start)
            base = QColor.fromHsvF(
                (hue + 0.04 + coming.get("Snare", 0.0) * self.TRUSS_TURN)
                % 1.0,
                max(0.0, 0.6 - flash * 0.4), 1.0, 1.0)
            # Hairlines with the width carried as light, except the nearest
            # truss, which keeps its width.
            thick = ((0.9 + near * 2.2) * (1.0 + kick * 1.1) * weight
                     * (1.0 + coming.get("Hats", 0.0) * 0.35))
            alpha = ((0.16 + bass * 0.22 + kick * 0.34 + flash * 0.3)
                     * glow * (0.30 + near * near * 1.4)
                     * (1.0 + coming.get("Kick", 0.0) * 0.5))
            if step == 0:
                # The nearest keeps its width: it is the one the eye is on.
                stroke(painter, path, self._shade(base, min(1.0, alpha)),
                       thick)
            else:
                self._beam(painter, path,
                           self._shade(base, min(1.0, alpha * thick
                                                 * self.TRUSS_LIFT)))

    #: How fast a ring closes on you, as a share of its own distance a second,
    #: so it grows evenly, and how near it gets before it is gone.
    RING_CLOSE = 2.1
    RING_GONE = 0.34

    #: How many rings a moment sends, and how far apart they start.
    RING_ECHOES = 3
    RING_APART = 1.7

    def _rings_now(self, painter, rect, horizon, focal, moment, step, hue,
                   flash):
        """Rings from a big moment (see RING_OVER), leaving the far end and
        sweeping past.
        """
        if moment > 0.0:
            for echo in range(self.RING_ECHOES):
                self._rings.append([self.FAR * 0.9 + echo * self.RING_APART,
                                    moment * (1.0 - echo * 0.22)])
        alive = []
        painter.setBrush(Qt.BrushStyle.NoBrush)
        weight = self._weight(rect)
        for ring in self._rings:
            ring[0] -= step * self.RING_CLOSE * ring[0]
            # Past the eye, so the ring leaves through the edges of the frame.
            if ring[0] <= self.RING_GONE:
                continue
            alive.append(ring)
            z, force = ring
            fade = max(0.0, min(1.0, (z - self.NEAR) / (self.FAR - self.NEAR)))
            radius = focal * (1.9 * force + 0.6) / z
            # Brightest mid-travel, fading as it arrives and as it goes.
            going = max(0.0, min(1.0, (z - self.RING_GONE) / 0.9))
            lit = min(1.0, (1.0 - fade) * 1.15 * force * going)
            wide = (1.2 + (1.0 - fade) * 5.5 + flash * 2.0) * weight
            # Three passes: a wide soft one, a narrow bright one and a thin
            # line inside the rim.
            for grow, share, thin in ((1.0, 0.30, 2.6),
                                      (1.0, 1.00, 1.0),
                                      (0.90, 0.45, 0.45)):
                colour = QColor.fromHsvF(
                    (hue + 0.5 + (0.06 if thin < 0.5 else 0.0)) % 1.0,
                    0.62 if thin > 0.5 else 0.30, 1.0,
                    min(1.0, lit * share))
                pen = QPen(colour, max(0.8, wide * thin))
                pen.setCosmetic(True)
                painter.setPen(pen)
                painter.drawEllipse(horizon, radius * grow,
                                    radius * grow * 0.62)
        # Never more than a bar's worth on screen at once.
        self._rings = alive[-8:]

    #: The laser rig: a lamp either side, deep down the room, each sweeping a
    #: fan of beams onto the floor in front of you. Deep lamps and near feet
    #: give the beams length in perspective.
    FAN = 11
    FAN_HANG = 0.78
    FAN_AT = 8.5
    FAN_NEAR, FAN_FAR = 0.7, 2.5
    FAN_OPEN = 0.95
    FAN_REACH = 1.0
    #: Sweeps a second at rest, and how much the hats hurry it.
    FAN_SWEEP = 0.55
    FAN_HURRY = 1.8
    #: How dim a beam is at the lamp against its near end, drawn as one
    #: gradient so there is no step along it.
    FAN_FADE = 0.4
    #: How much a strobe hit adds to the rig and how much colour it takes away.
    FAN_STROBE = 0.75
    FAN_BLEACH = 0.45
    #: Below this there is no rig at all, so a quiet passage has none.
    FAN_FAINT = 0.03
    #: Seconds of listening before the rig can come on.
    FAN_SETTLE = 2.0

    #: A drop is a level, not a change: the rig reads where the room sits
    #: between its usual quiet and the loudest it has been, ``(now - quiet) /
    #: (loudest - quiet)``, which holds for the length of a drop. The loudest
    #: decays slowly.
    PEAK_FALL = 0.9996
    PEAK_SPAN = 0.08

    #: The quiet the track keeps coming back to: follows the room down quickly
    #: and climbs back slowly, so it stays near the verse through a drop.
    QUIET_DOWN = 0.02
    QUIET_UP = 0.0004

    def _lasers_lit(self) -> float:
        """How hard the rig is running, 0 to 1: held for as long as the passage
        is loud (see PEAK_FALL), with the hats adding to it in proportion.
        """
        quiet = self._quiet
        if (quiet is None or self._peak < self.RING_QUIET
                or self._heard_for < self.FAN_SETTLE):
            return 0.0
        span = self._peak - quiet
        if span < self.PEAK_SPAN:
            return 0.0      # nothing to drop from
        level = max(0.0, min(1.0, (self._quick - quiet) / span))
        return max(0.0, min(1.0, level * 0.85 + self._fizz * 0.5 * level))

    def _beams_now(self, painter, rect, horizon, focal, hats, step, hue,
                   bass, flash=0.0):
        """The laser rig: two fans sweeping across the floor, each beam from a
        lamp deep down the room to a foot in front of you. See
        ``_lasers_lit``.
        """
        lit = self._lasers_lit()
        if lit + flash * self.FAN_STROBE < self.FAN_FAINT:
            return
        lift = self._lift(bass)
        span = self.ACROSS * 0.5
        self._fan += step * (self.FAN_SWEEP + self._fizz * self.FAN_HURRY)
        # Back and forth, the way a rig sweeps.
        phase = math.sin(self._fan) * 0.8

        pairs = []
        for side in (-1.0, 1.0):
            lamp = self._project(horizon, focal,
                                 side * self.FAN_HANG * span, -lift,
                                 self.FAN_AT)
            for index in range(self.FAN):
                spread = (index / (self.FAN - 1.0)) * 2.0 - 1.0
                angle = phase + spread * self.FAN_OPEN
                # Mirrored exactly, so the two fans are one rig.
                foot = self._project(
                    horizon, focal,
                    side * math.sin(angle) * span * self.FAN_REACH, lift,
                    self.FAN_NEAR + (math.cos(angle) * 0.5 + 0.5)
                    * (self.FAN_FAR - self.FAN_NEAR))
                pairs.append((lamp, foot))

        # A strobe hit takes the whole rig with it and bleaches it towards
        # white.
        lit = min(1.0, lit + flash * self.FAN_STROBE)
        shade = (hue + 0.18) % 1.0
        deep = max(0.0, 0.42 - flash * self.FAN_BLEACH)
        wide = (0.9 + lit * 1.8) * self._weight(rect)
        for lamp, foot in pairs:
            shine = QLinearGradient(lamp, foot)
            shine.setColorAt(0.0, QColor.fromHsvF(
                shade, deep, 1.0, min(1.0, lit * 0.8 * self.FAN_FADE)))
            shine.setColorAt(1.0, QColor.fromHsvF(
                shade, deep, 1.0, min(1.0, lit * 0.8)))
            pen = QPen(QBrush(shine), wide)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(lamp, foot)

    def _core(self, painter, horizon, span, hue, bass, kick, synth, flash,
              weight=1.0):
        """The thing in the middle: a wireframe that turns with the hats,
        swells and shakes with the kick. Drawn last and small.
        """
        # The raw kick, not the eased one: it is meant to be hit.
        fizz = max(self._crack, self._fizz * 0.25)
        size = span * (0.045 + bass * 0.05 + kick * 0.05 + flash * 0.02
                       + fizz * 0.035)
        if size < 2.0:
            return
        turn = self._spin * 1.7
        points = []
        for corner in range(6):
            angle = turn + corner * math.tau / 6.0
            lean = math.sin(turn * 0.7 + corner) * 0.35
            # Per corner, on its own phase, so the shape breaks up rather than
            # moving.
            shake = 1.0 + fizz * 0.55 * math.sin(turn * 6.1 + corner * 2.3)
            points.append(QPointF(
                horizon.x() + math.cos(angle) * size * shake,
                horizon.y() + math.sin(angle) * size * (0.5 + lean) * shake))
        colour = QColor.fromHsvF((hue + 0.32 + synth * 0.1) % 1.0,
                                 max(0.0, 0.25 - fizz * 0.2), 1.0,
                                 min(1.0, 0.5 + kick * 0.5 + fizz * 0.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        # Every corner to every other: a wireframe.
        for a in range(len(points)):
            for b in range(a + 1, len(points)):
                path.moveTo(points[a])
                path.lineTo(points[b])
        stroke(painter, path, colour,
               (1.4 + kick * 2.4 + flash * 1.6 + fizz * 1.2) * weight)


#: One empty chart, shared. See Rider._lay.
_NO_CHART: dict = {}


class Rider(Scene):
    """A game played on a road the music builds: three lanes, blocks on the
    beat, moved between with the arrow keys. The chart is laid ahead of the
    playhead from every hit in the track, at a pace that can be played (see
    GAP).
    """

    name = "Music rider"
    #: The games. Mono: colours are points on a chain and greys are hazards.
    #: Ninja: more greys, with coins for riding close. Wakeboard: jumps.
    #: Puzzle: colours drop into a grid and score in clusters.
    MODES = ("Mono", "Ninja", "Wakeboard", "Puzzle")
    blurb = "a game: three lanes, and the track is the song"

    # The road
    LANES = 3
    LANE_WIDE = 1.30
    #: The near and far ends of the road. NEAR is behind the rider, so the road
    #: runs off the bottom of the frame however it banks.
    NEAR, FAR = -2.4, 20.0
    #: How far in front of the eye the road starts, and the nearest z
    #: projected.
    NEAR_EYE = 0.22
    #: Cross-pieces down the road; also how smooth its bends look.
    RUNGS = 44

    #: Where the eye sits: high and behind the rider, so the road ahead fills
    #: most of the picture.
    EYE_UP = 2.1
    EYE_BACK = 2.4

    #: The longest a track can be, in seconds: a sanity check on the playhead.
    LONGEST = 86400.0

    #: Where the rider sits along the road, and how fast it slides lanes.
    RIDER_AT = 3.0
    #: How fast the rider slides to a new lane, as a share of the way per
    #: sixtieth of a second: about 50 ms. See ``_slide``.
    SNAP = 0.55

    #: How far ahead the chart is read, in seconds: LOOK_BEATS at the slowest
    #: tempo.
    READ = 5.0
    #: The least time between figures, in seconds and in beats, whichever is
    #: longer.
    GAP = 0.80
    GAP_BEATS = 2.0
    #: How the gap stretches and tightens with the passage's energy, in beats.
    GAP_LEAST = 1.5
    GAP_MOST = 3.0

    def _apart(self, when: float) -> float:
        """How many beats apart the figures are here: by the section where the
        track has been heard (see rider_layout.spacing), otherwise by how
        loud it is."""
        section = self._section_at(when)
        if section is not None:
            import rider_layout

            through = (when - section.start) / max(1e-6, section.length)
            loud = self._read(self._energy, when) if self._energy else None
            return rider_layout.spacing(section.kind, through, self._style,
                                        loud, self._difficulty)
        if not self._energy:
            return self.GAP_BEATS
        energy = max(0.0, min(1.0, self._read(self._energy, when)))
        return self.GAP_MOST - (self.GAP_MOST - self.GAP_LEAST) * energy
    #: How far a heavier drum may be from the first candidate and still take
    #: its place: six tenths of a beat, enough to reach the next kick from a
    #: hat on the half beat.
    PREFER_BEATS = 0.6
    PREFER = 0.17
    #: How far short of the gap still counts, so float error on the grid does
    #: not cost a slot.
    SLACK = 0.03
    #: How far apart the three blocks of one run are.
    RUN_GAP = 0.16

    #: How far the road travels in a beat, and how many beats lie between the
    #: horizon and the rider. The road's position is a function of the beat, so
    #: a block laid on beat n arrives exactly on it, and everything moves
    #: together.
    LOOK_BEATS = 3.0
    PER_BEAT = (FAR - RIDER_AT) / LOOK_BEATS

    #: Road units a second when no tempo has been found.
    FREE_RUN = 11.0

    #: How much a full bass front-loads the travel within a beat. The curve is
    #: the identity at both ends, so blocks still arrive on the beat.
    LUNGE = 1.6
    #: How much of the beat's travel is lunged rather than even; the road never
    #: drops below 1 - LUNGE_MIX of its pace.
    LUNGE_MIX = 0.55

    #: How hard the road bends, climbs and rolls, growing with the passage's
    #: loudness.
    BEND = 2.6
    CLIMB = 1.9
    #: How tightly the road turns, in radians of phase per road unit.
    BEND_EVERY = 0.17
    #: How much of the bend, climb and bank a quiet passage gets, and how much
    #: a loud one adds.
    PUSH_REST = 0.45
    PUSH_GAIN = 0.35
    #: How slowly the road's bends follow the loudness, in seconds.
    PUSH_EASE = 0.9

    #: How hard the track's own stereo lean bends the road, and the most one
    #: reading may push. See ``_carve``.
    TRACK_BEND = 0.15
    LEAN_MOST = 1.5

    #: Seconds of the track either side of a point averaged into the road's
    #: shape there.
    SMOOTH_FOR = 0.75
    #: How hard the road banks into its own turn.
    BANK = 1.2

    #: How far past the rider a block is still drawn.
    GONE = 1.2

    #: Half a lane, and the rider gets the benefit of it.
    FORGIVE = 0.45
    #: Seconds of flashing, and of not being hit again, after a hit.
    SORE = 0.9

    #: How long the picture shows a hit: a red, dark wash says it happened to
    #: you.
    HURT_FOR = 0.6
    #: How hard a hit washes the frame, throws the camera and dims everything
    #: else.
    HURT_WASH = 0.34
    HURT_THROW = 2.3
    HURT_DIM = 0.72

    SHAKE = 0.030
    SHAKE_FALL = 0.10

    #: How far down the road the camera aims, how hard it turns towards it, and
    #: how quickly the aim moves.
    AIM = 7.0
    AIM_PULL = 0.11
    AIM_EASE = 0.06

    #: The focal length as a share of the frame, at a crawl and at a sprint:
    #: shorter is wider, about sixty degrees to eighty. Wider than that shrinks
    #: the lanes.
    FOCAL_SLOW = 0.86
    FOCAL_FAST = 0.62

    #: How much further back the eye is dragged at a sprint, and the spring
    #: that drags it, damped enough not to wobble.
    CHASE = 0.55
    CHASE_SPRING = 0.020
    CHASE_DAMP = 0.86
    #: How fast the rig notices a passage has got louder: the shape of the
    #: song, not the bar.
    RUSH_EASE = 0.02

    #: How far the view banks into a bend, in degrees for a full turn.
    TILT = 5.0
    #: How hard the camera follows the road up a hill, measured from the road
    #: under the rider (see ``_eye``), and the most of the frame it may give
    #: up.
    PITCH = 0.05
    PITCH_MOST = 0.085

    #: Where the horizon sits, as a share of the frame above its middle.
    HORIZON_UP = 0.10

    #: How much of the kick's shake reaches the camera.
    SHAKE_LESS = 0.25

    #: What a hit does to the road: how far the speed drops and how fast it
    #: comes back.
    SLOW = 0.45
    SLOW_BACK = 0.030
    #: How many pieces a hit throws off, how fast and for how long.
    SPARKS = 14
    SPARK_GO = 7.0
    SPARK_FADE = 1.9

    def __init__(self) -> None:
        self._lane = 1
        self._lane_here = 0.0
        #: What a run is judged on at the end; see result.
        self._offered = 0
        self._taken = 0
        self._chain_most = 0
        self._saves = 0
        self._finished = False
        self._result = None
        #: The run as it went, for the strip at the end, and the track's
        #: loudness and length, kept at the finish.
        self._log: list = []
        self._ridden = None
        #: Whether this run is the whole track with no seek in it, the only
        #: kind a best is kept for. See _finish.
        self._whole = True
        #: Whether the playhead jumped this frame, and from where.
        self._jumped = False
        self._jumped_from = 0.0
        #: What the road is counted in (the beat's length), and whether that
        #: changed this frame. See _advance.
        self._counted_in = None
        self._rebased = False
        #: Set where the bests are kept: this track's best before, and whether
        #: this run beat it.
        self.best_before = None
        self.new_best = False
        self._at = 0.0
        self._last = None
        self._heard = 0.0
        self._shake = 0.0
        self._sore = 0.0
        #: 1 the moment something was hit, falling to 0 over HURT_FOR.
        self._hurt = 0.0
        #: Coloured blocks taken in a row, and 1 the moment one is. See
        #: CHAIN_FIRST.
        self._chain = 0
        self._got = 0.0
        #: The last of each kind of run and how long it is. See _combo.
        self._combos: dict = {}
        #: Whether a grey has been touched yet. See CLEAN_BONUS.
        self._clean = True
        #: How far off the road the craft is, how fast it is rising, and the
        #: peak it left from. See JUMP_UP.
        self._air = 0.0
        self._air_up = 0.0
        self._air_from = 0.0
        self._airs = 0
        self._best_air = 0
        #: Coins taken, coins in a row, and the best row. See COIN_WORTH.
        self._coins = 0
        self._coin_run = 0
        self._coin_best = 0
        #: How far through its spin each coin is, so a trail turns together.
        self._coin_spin = 0.0
        #: The bumper: 1 when up, 0 the moment it shatters a grey, back over
        #: SHIELD_BACK.
        self._shield = 1.0
        #: The puzzle grid, a list of colours per column from the bottom up.
        self._cells = [[] for _ in range(self.CELLS_WIDE)]
        self._fuse = 0.0
        self._fused = 0
        self._stunned = 0.0
        self._cleared = 0
        #: Mono or Puzzle. See MODES.
        self._mode = self.MODES[0]
        self._score = 0
        self._streak = 0
        self._best = 0
        self._hits = 0
        self._blocks: list = []
        #: What became of each block the craft met; see struck.
        self._struck: dict = {}
        self._laid = 0.0
        self._chart_from = None
        #: When the last figure was put down.
        self._placed = -99.0
        self._loudness = 0.0
        self._pushing = 0.0
        self._speed = self.FREE_RUN
        #: How hard the road lunges into a beat not yet decided. See _lunge_of.
        self._lunge = 1.0
        #: The lunge each beat was given as it came into view, fixed from then.
        self._lunges: dict = {}
        #: Each beat's length of road against PER_BEAT and where it starts,
        #: decided with its lunge. See _decide_lunges.
        self._paces: dict = {}
        self._starts: dict = {}
        #: How driven the music is at each decided beat, 0 to 1.
        self._drives: dict = {}
        #: The decided beats' starts in order: (number of the first, starts).
        self._marks = None
        #: Added to every place on the road, so the road carries on when the
        #: beats it is counted in change.
        self._road_shift = 0.0
        #: Where the beats fall (see beat_clock): the drums', or the pane's
        #: grid until they are known. None with no tempo.
        self._clock = None
        #: Seconds in the beat being played, or 0 with no tempo.
        self._beat = 0.0
        #: A moment that is known to be on the beat, for snapping to.
        self._grid = None
        #: Where the music clock was last frame.
        self._last_heard = None
        #: The moment beat zero started, which distance is measured from.
        self._origin = None
        #: How far through the current beat the track is, 0 to 1.
        self._pulse = 0.0
        #: Set while an obstacle has just been hit. See SLOW and _sparks.
        self._slow = 1.0
        self._sparks: list = []
        #: The playhead last frame, to tell a paused track from a playing one.
        self._was_at = None
        #: The pane's count of jumps as of the last frame. See _advance.
        self._jumps_seen = None
        #: Whether the drums' beat is still on its way, and whether any road
        #: has been shown yet.
        self._waiting = False
        self._started = False
        #: Blocks in sight and where they were, kept when the road is counted
        #: afresh, and where the road was on the old count. See _advance.
        self._kept: list = []
        self._carried = None
        #: How far the beat-locked parts of the road have come up since there
        #: was a beat, 0 to 1.
        self._beat_shown = 0.0
        self._rolling = 1.0
        #: Where the camera is looking, and how hard it is banked.
        self._aimed = 0.0
        self._pitched = 0.0
        self._banked = 0.0
        #: How hard the road is running against its resting speed, eased.
        self._rushing = 0.0
        #: Where the eye is behind the rider, and the spring dragging it.
        self._chase = self.EYE_BACK
        self._chase_to = 0.0
        #: The shake's own clock, so it is not tied to anything else.
        self._wobble = 0.0
        #: How far through a whole turn the road is. See ``_find_twists``.
        self._rolled = 0.0
        self._spun = 0.0
        #: How close to a beat the track is, held while it is stopped.
        self._beat_lit = 0.0
        #: How fast the craft is crossing lanes, for the bank.
        self._swerve = 0.0
        #: What the screen is still answering, and where the craft is on the
        #: glass. See POPS.
        self._pops = []
        self._craft_glass = None
        self._craft_spot = None
        #: The road's colour this frame, for anything that answers in it.
        self._hue_now = 0.0
        #: The last frame's worth of the track's clock. See ``_advance``.
        self._went = 0.0
        self._bend = 0.0
        self._climb = 0.0
        #: How high the road is under the rider; the eye rides on it.
        self._under = 0.0
        #: How far across the road is under the rider.
        self._side = 0.0
        #: The track's shape, and the road made out of it. See ``_shape``.
        self._shaped = None
        self._hill = ()
        self._curve = ()
        self._energy = ()
        #: How much bass there is along the track, as the contour has it.
        self._low = ()
        #: Where the road turns over on itself. See ``_find_twists``.
        self._twists = ()
        #: Which twists have had their power block laid.
        self._twisted = set()
        #: How the track moves and how it is put together, and what that was
        #: read from. See trackstyle.
        self._style = None
        self._styled_from = None
        #: The key and chords, for laying blocks along the melody.
        self._harmony = None
        #: Which figures each section lays, and how many it has laid. See
        #: rider_layout.
        self._plan = None
        self._slots: dict = {}
        #: Where a gate's open lane has walked to.
        self._gate_step = 0
        #: When the last coin taken was due. See COIN_ROW_GAP.
        self._coin_last = None
        #: How much of each section's share of obstacles is still owed. See
        #: _greyed.
        self._owed: dict = {}
        #: What the road was last planned from. See ``_carve``.
        self._planned = None
        #: How much of the road's rise and fall a drawing shows: all of it on
        #: the card, a share of it flat.
        self._relief = 1.0
        #: What the next thing collected is multiplied by, if anything.
        self._double = 1.0
        self._every = 1.0
        #: Where the road starts, a fixed distance in front of the eye.
        self._near = self.NEAR
        self._quick = 0.0
        self._quiet = None
        self._peak = 0.0
        self._plasma = Plasma()
        #: How hard it is, and what that changes - see set_difficulty.
        self._set_level("Normal")

    def steer(self, way: int) -> bool:
        """Move a lane. Returns whether the key meant anything here."""
        was = self._lane
        self._lane = max(0, min(self.LANES - 1, self._lane + int(way)))
        return self._lane != was

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Change game and start it fresh, keeping the level."""
        if mode in self.MODES and mode != self._mode:
            self.reset()
            self._mode = mode

    #: How hard it is: see rider_layout.DIFFICULTY.
    DIFFICULTIES = ("Easy", "Normal", "Hard", "Expert")

    @property
    def difficulty(self) -> str:
        return self._difficulty

    def set_difficulty(self, name: str) -> None:
        """Change the level and start again."""
        if name in self.DIFFICULTIES and name != self._difficulty:
            self.reset()
            self._set_level(name)

    #: The game and level (the player's) and the relief (the painter's), kept
    #: across a reset.
    KEPT = ("_mode", "_difficulty", "_relief")

    def reset(self) -> None:
        """A new run in the chosen game and level, with the level applied
        again."""
        super().reset()
        self._set_level(self._difficulty)

    def _set_level(self, name: str) -> None:
        """What a level changes: see rider_layout.DIFFICULTY."""
        import rider_layout

        level = rider_layout.level(name)
        self._difficulty = name if name in self.DIFFICULTIES else "Normal"
        # How many beats of road are in sight, set on the scene for everything
        # that measures the road in beats.
        self.LOOK_BEATS = float(level["look"])
        self.PER_BEAT = (self.FAR - self.RIDER_AT) / self.LOOK_BEATS
        shield = level["shield"]
        self._shield_back = (None if shield is None
                             else self.SHIELD_BACK * float(shield))
        if self._shield_back is None:
            self._shield = 0.0
        self._score_share = float(level["score"])
        # The least time between figures, never under a beat; see
        # rider_layout.spacing.
        self._gap_least = self.GAP * float(level["spacing"])
        self._least_warning = float(level.get("warning")
                                    or self.LEAST_WARNING)

    def _paid(self, points: float) -> int:
        """Points as this level pays them."""
        return int(round(points * self._score_share))

    def report(self) -> dict:
        return {"score": self._score, "streak": self._streak,
                "best": self._best, "hits": self._hits,
                "chain": self._chain, "clean": self._clean,
                "worth": self._worth(), "cleared": self._cleared,
                "shield": self._shield, "coins": self._coins,
                "double": self._double, "twists": len(self._twists),
                "air": self._air, "airs": self._airs,
                "best_air": self._best_air,
                "coin_run": self._coin_run, "coin_best": self._coin_best,
                "stunned": self._stunned > 0.0,
                "cells": [list(pile) for pile in self._cells]}

    #: Which drum makes which shape, in the order they win a slot.
    PATTERNS = (("Kick", "wall"), ("Snare", "block"), ("Hats", "run"))

    #: Two bars of shapes, indexed by the slot, so a track lays out the same
    #: way each time and four-to-the-floor is not all walls.
    POOL = ("wall", "block", "wall", "run",
            "wall", "block", "wall", "wall")

    def _lay(self, state) -> None:
        """Put the next stretch of chart on the road: only what has come into
        view since the last frame.
        """
        # The same empty table every time, so no chart does not look like a new
        # one each frame.
        chart = getattr(state, "chart", None) or _NO_CHART
        started, self._started = self._started, True
        if self._waiting:
            return
        fresh = (chart is not self._chart_from or self._jumped
                 or self._rebased)
        if fresh and (self._jumped or not started):
            # A seek, or the first frame: laid from here.
            self._chart_from = chart
            self._blocks = []
            self._laid = self._heard
            self._placed = -99.0
            self._twisted = set()
        elif fresh:
            # The same ride counted afresh: what is in sight stays, and the
            # rest is laid from the far end.
            self._chart_from = chart
            end = self._when(self.RIDER_AT + self.IN_SIGHT)
            self._blocks = [block for block in self._blocks
                            if block[0] <= end]
            self._laid = end
            kept = [block[0] for block in self._blocks
                    if block[2] != "coin"]
            self._placed = max(kept) if kept else -99.0
            self._twisted = {start for start in self._twisted
                             if start <= end}
        ahead = self._heard + self.READ
        if self._clock:
            # Further where the road runs slowly, so a block comes out of the
            # distance.
            ahead = max(ahead, self._when(self.RIDER_AT + self.SEEN))
        # Committed a little short of what has been read, so each choice sees
        # the candidates after it; see PREFER_BEATS.
        reach = (self._beat * self.PREFER_BEATS if self._beat > 0.0
                 else self.PREFER)
        commit = ahead - reach
        if commit <= self._laid:
            return
        low, self._laid = self._laid, commit

        # Every hit in the window, heaviest first at the same moment.
        due = []
        for order, (name, shape) in enumerate(self.PATTERNS):
            for when in chart.get(name, ()):
                if low < when <= ahead:
                    # Under a kick, the hats are what figures land between, not
                    # on. With no drums they are the rhythm.
                    if name == "Hats":
                        section = self._section_at(when)
                        if section is not None and section.drums:
                            continue
                    due.append((when, order, shape))
        # Then the melody: the synths' onsets and the notes heard in the lead,
        # which lay a breakdown with no drums.
        melodic = len(self.PATTERNS)
        for when in chart.get("Synth", ()):
            if low < when <= ahead:
                due.append((when, melodic, "melody"))
        import rider_layout

        for when in rider_layout.lead_onsets(self._harmony, low, ahead):
            due.append((when, melodic + 1, "melody"))
        due.sort()
        index = 0
        while index < len(due):
            when, _order, shape = due[index]
            if when > commit:
                break
            # How far apart the figures are where this one lands, not where the
            # playhead is.
            gap = self._gap_least
            if self._beat > 0.0:
                gap = max(gap, self._beat * self._apart(when))
            if when - self._placed < gap - self.SLACK:
                index += 1
                continue
            # The heaviest drum within a moment of it, not whichever came
            # first, or half the figures land off the beat.
            reach = (self._beat * self.PREFER_BEATS if self._beat > 0.0
                     else self.PREFER)
            def rank(at):
                # A hit on the beat before a heavier one between beats.
                return (self._off_beat(due[at][0]) > self.ON_BEAT * self._beat,
                        self._weight(due[at][1]), self._off_beat(due[at][0]))

            best = index
            for other in range(index + 1, len(due)):
                if due[other][0] - when > reach:
                    break
                if rank(other) < rank(best):
                    best = other
            when, order, shape = due[best]
            when = self._snap(when)
            self._placed = when
            grey = self._greyed(when, order)
            figure, mirrored = self._figure(when, grey, shape)
            self._shape(figure, when, grey=grey, mirrored=mirrored)
            index = best + 1
        # Prizes on the beat where the track goes quiet, so the road never sits
        # empty for long, laid from the start of the window where nothing has
        # been placed.
        if self._beat > 0.0:
            every = self._beat * self.QUIET_BEATS
            last = max(self._placed, low - every)
            while last + every <= commit:
                when = self._snap(max(last + every, low + 1e-6))
                if when <= last:
                    break
                last = self._placed = when
                figure, mirrored = self._figure(when, False, "block")
                self._shape(figure, when, grey=False, mirrored=mirrored)
        # The power block at the mouth of each corkscrew.
        for start in self._twists:
            if low < start <= commit and start not in self._twisted:
                self._twisted.add(start)
                self._blocks.append(
                    [self._snap(start + self.TWIST_FOR * 0.5),
                     self.LANES // 2, "power", False, False])
        if fresh:
            # Nothing at or behind the craft, which nobody could reach.
            self._blocks = [block for block in self._blocks
                            if block[0] > self._heard]
        self._blocks = self._blocks[-200:]

    #: How long the road may have nothing on it, in beats, before something is
    #: put there: a bar.
    QUIET_BEATS = 4.0

    #: Which slots carry an obstacle rather than a prize: about a quarter,
    #: indexed by the slot, with a length unlike the shapes' so the pattern
    #: does not repeat every eight figures.
    GREY_POOL = (False, False, True, False, False, True, False)

    #: Ninja's: the same road with far more to dodge, four slots in seven, each
    #: with coins beside it. The figures come no closer together.
    NINJA_POOL = (True, False, True, True, False, True, False)

    def _weight(self, order: int) -> int:
        """How heavy a hit of the drum at ``order`` is: the kick, and on
        half-time music the snare it waits for.
        """
        if order == 1 and self._style is not None and self._style.heavy > 0.5:
            return 0
        return order

    #: The most obstacle a section may carry to its next kick: never two in a
    #: row for want of kicks.
    OWED_MOST = 1.25

    def _greyed(self, when: float, order: int) -> bool:
        """Whether the figure at this slot is an obstacle: the section's share,
        paid on the heavy hits you can hear coming.
        """
        # Obstacles land on the heavy hits: the kick, and on half-time music
        # the snare.
        heavy = self._weight(order) == 0
        # Nothing to dodge inside a corkscrew, where left stops meaning left.
        if self._twist_at(when) is not None:
            return False
        if self._beat <= 0.0:
            return heavy
        section = self._section_at(when)
        if section is not None:
            # The section's share of obstacles (rider_layout.DANGER_SHARE),
            # carried from slot to slot rather than drawn each time, with a
            # little of the track's seed in where each lands.
            import random

            import rider_layout

            share = rider_layout.danger_share(section.kind, self._mode,
                                              self._style, self._difficulty)
            if order == 1:
                share *= 0.7
            key = id(section)
            owed = self._owed.get(key)
            if owed is None:
                owed = random.Random(self._style.seed
                                     ^ int(section.start * 1000)).random()
            owed += share
            if not heavy:
                # Owed all the same and paid on the next kick, held under
                # OWED_MOST.
                owed = min(owed, self.OWED_MOST)
                # And paid on the snare when the kicks will not come.
                if order == 1 and owed >= self.OWED_MOST:
                    self._owed[key] = owed - 1.0
                    return True
                self._owed[key] = owed
                return False
            jitter = random.Random(self._style.seed
                                   ^ int(round(when * 1000))).uniform(-0.2, 0.2)
            grey = owed >= 1.0 + jitter
            if grey:
                owed -= 1.0
            self._owed[key] = owed
            return grey
        if not heavy:
            return False
        slot = int(round(when / self._beat / max(1e-6, self.GAP_BEATS)))
        pool = self.NINJA_POOL if self._mode == "Ninja" else self.GREY_POOL
        return pool[slot % len(pool)]

    def _figure(self, when: float, grey: bool, shape: str) -> tuple:
        """What figure goes here, and whether it is mirrored: from the
        section's palette (see rider_layout), or the pool of shapes."""
        section = self._section_at(when)
        if section is None or self._plan is None:
            return self._varied(shape, when), False
        slot = self._slots.get(id(section), 0)
        self._slots[id(section)] = slot + 1
        figure, mirrored = self._plan.figure(section, slot, grey)
        if not grey and figure == "block" and shape == "melody":
            figure = "melody"
        return figure, mirrored

    def _varied(self, shape: str, when: float) -> str:
        """What shape this slot takes, from the pool, indexed by the slot."""
        if not self._clock:
            return shape
        slot = int(round(self._clock.number(when)
                         / max(1e-6, self.GAP_BEATS)))
        return self.POOL[slot % len(self.POOL)]

    def _snap(self, when: float) -> float:
        """The nearest beat to ``when``, or ``when`` with no grid."""
        if not self._clock:
            return when
        return self._clock.time(round(self._clock.number(when)))

    #: How near a beat a hit must be, as a share of one, to count as on it.
    ON_BEAT = 0.12

    def _off_beat(self, when: float) -> float:
        """How far a moment is from the nearest beat, in seconds; 0 with no
        tempo."""
        if not self._clock:
            return 0.0
        beats = self._clock.number(when)
        return abs(beats - round(beats)) * self._clock.length(when)

    #: Coins: worth, how many sit beside one obstacle, and how far apart. A
    #: trail in the lane next to an obstacle pays for holding it while the grey
    #: goes by; the far lane is safe and pays nothing.
    COIN_WORTH = 25
    COIN_STEP = 25
    COIN_MOST = 200
    COINS_RUN = 3
    COIN_GAP = 0.16
    #: How long after the last coin a coin still carries its row on.
    COIN_ROW_GAP = 0.6

    #: How long before a wall arrives its coin trail must end: the trail runs
    #: in a lane the wall is about to close.
    COIN_LEAD = 0.30

    #: Wakeboard: how hard a jump pushes off, gravity, how far ahead a crest is
    #: read, and what a jump off one pays. About two thirds of a second in the
    #: air, and nothing is collected up there.
    JUMP_UP = 3.2
    JUMP_DOWN = 10.0
    CREST_LOOK = 2.5
    CREST_FULL = 0.45
    AIR_WORTH = 150

    def jump(self) -> bool:
        """Leave the road, in the game that allows it. Returns whether the key
        meant anything, like ``steer``.
        """
        if self._mode != "Wakeboard" or self._air > 0.0 or self._air_up > 0.0:
            return False
        self._air_up = self.JUMP_UP
        # The crest as the craft leaves, which the jump is scored on.
        self._air_from = self._crest()
        return True

    def _crest(self) -> float:
        """How much of a peak the road is at, 0 to 1: where the road ahead
        falls away from the road underneath.
        """
        here = self._road(self.RIDER_AT)[1]
        ahead = self._road(self.RIDER_AT + self.CREST_LOOK)[1]
        return max(0.0, min(1.0, (ahead - here) / self.CREST_FULL))

    def _fly(self, step: float) -> None:
        """Carry a jump through the air, and land it."""
        if self._air <= 0.0 and self._air_up <= 0.0:
            return
        self._air += self._air_up * step
        self._air_up -= self.JUMP_DOWN * step
        if self._air > 0.0:
            return
        # Down. A jump pays for the peak it left from; off the flat it scores
        # nothing.
        self._air = 0.0
        self._air_up = 0.0
        paid = int(self.AIR_WORTH * self._air_from * self._double)
        if paid:
            self._airs += 1
            self._best_air = max(self._best_air, paid)
            self._score += self._paid(paid)
            self._double = 1.0
            self._got = 1.0
            self._burst(self._lane_here, prize=True)
            self._pop("air", hue=0.50, sat=0.55,
                      strength=0.6 + self._air_from * 0.8,
                      text=(f"AIR +{paid}" if self._air_from > 0.6 else ""))
        self._air_from = 0.0

    #: What a power block multiplies (the next thing that pays), and how long
    #: it waits to be spent.
    POWER_DOUBLE = 2.0

    def _coins_beside(self, when: float, lane: int, grey: bool) -> None:
        """A trail of coins in the lane beside a single obstacle."""
        if not grey:
            return
        beside = [side for side in (lane - 1, lane + 1)
                  if 0 <= side < self.LANES]
        if not beside:
            return
        # Which side, from the time, so a track lays out the same way each
        # time.
        side = beside[int(when * 613) % len(beside)]
        self._coin_trail(when - (self.COINS_RUN - 1) * self.COIN_GAP / 2.0,
                         side)

    def _coins_before(self, when: float, shut, grey: bool) -> None:
        """A trail in a lane a wall is about to close. See COIN_LEAD."""
        if not grey or not shut:
            return
        shut = sorted(shut)
        side = shut[int(when * 613) % len(shut)]
        last = when - self.COIN_LEAD
        self._coin_trail(last - (self.COINS_RUN - 1) * self.COIN_GAP, side)

    def _coin_trail(self, first: float, side: int, count: int = 0,
                    gap: float = 0.0) -> None:
        """Lay a trail if the lane is free for all of it."""
        count = count or self.COINS_RUN
        gap = gap or self.COIN_GAP
        last = first + (count - 1) * gap
        pad = gap / 2.0
        for other, taken, _kind, _done, _grey in self._blocks:
            if taken == side and first - pad <= other <= last + pad:
                return
        for step in range(count):
            self._blocks.append(
                [first + step * gap, side, "coin", False, False])

    def _shape(self, pattern: str, when: float, grey: bool = True,
               mirrored: bool = False) -> None:
        """One figure, as blocks in lanes; see rider_layout. ``grey`` is an
        obstacle to dodge, a colour a prize to take; ``mirrored`` swaps left
        and right for a repeated part.
        """
        # The lane comes from the time, not a random number.
        seed = int(when * 977) % self.LANES
        top = self.LANES - 1
        if mirrored:
            seed = top - seed
        step = max(self.SIXTEENTH, self._beat / 4.0 if self._beat > 0.0
                   else self.SIXTEENTH)
        if pattern == "wall":
            for lane in range(self.LANES):
                if lane != seed:
                    self._blocks.append([when, lane, "wall", False, grey])
            self._coins_before(
                when, [lane for lane in range(self.LANES) if lane != seed],
                grey)
        elif pattern == "gate":
            # The open lane walks across the road and back: a weave to the
            # kick.
            walk = (0, 1, 2, 1)
            open_lane = walk[self._gate_step % len(walk)]
            self._gate_step += 1
            if mirrored:
                open_lane = top - open_lane
            for lane in range(self.LANES):
                if lane != open_lane:
                    self._blocks.append([when, lane, "wall", False, grey])
            self._coins_before(
                when, [lane for lane in range(self.LANES) if lane != open_lane],
                grey)
        elif pattern == "chicane":
            # Two walls half a beat apart with their gaps side by side.
            first = 0 if seed < 1 or (seed == 1 and int(when * 31) % 2) else top
            second = 1
            later = when + max(self.CHICANE_LEAST, self._beat * 0.5)
            for lane in range(self.LANES):
                if lane != first:
                    self._blocks.append([when, lane, "wall", False, grey])
                if lane != second:
                    self._blocks.append([later, lane, "wall", False, grey])
        elif pattern == "block":
            lane = seed
            if not grey:
                lane = self._melody_lane(when, seed)
            self._blocks.append([when, lane, "block", False, grey])
            self._coins_beside(when, lane, grey)
        elif pattern == "melody":
            lane = self._melody_lane(when, seed)
            self._blocks.append([when, lane, "block", False, grey])
        elif pattern == "run":
            for count in range(3):
                at = when + count * self.RUN_GAP
                lane = (seed + count) % self.LANES
                if not grey:
                    # A run of prizes goes where the melody goes.
                    lane = self._melody_lane(at, lane)
                self._blocks.append([at, lane, "run", False, grey])
            # Coins beside the first of them only.
            self._coins_beside(when, seed, grey)
        elif pattern == "stairs":
            # Across the lanes the way the melody goes: right as it climbs,
            # left as it falls.
            import rider_layout

            going = rider_layout.rising(self._harmony, when)
            if going is None:
                going = (seed + int(mirrored)) % 2 == 0
            lanes = list(range(self.LANES)) if going else list(
                reversed(range(self.LANES)))
            for count, lane in enumerate(lanes):
                at = when + count * max(step, self.RUN_GAP)
                # Each step on the note being played then.
                self._blocks.append([at, self._melody_lane(at, lane),
                                     "run", False, False])
        elif pattern == "stream":
            # A row of coins in one lane, a sixteenth apart.
            lane = self._melody_lane(when, seed)
            self._coin_trail(when, lane, count=self.STREAM, gap=step)
        elif pattern == "pair":
            # Two prizes side by side on a chord: take one.
            lanes = (0, top) if seed != 1 else ((0, 1) if mirrored else (1, top))
            for lane in lanes:
                self._blocks.append([when, lane, "block", False, False])

    #: The shortest a sixteenth may be, in seconds, for figures that step on
    #: them.
    SIXTEENTH = 0.14
    #: The shortest time between a chicane's two walls.
    CHICANE_LEAST = 0.26
    #: How many coins a stream is.
    STREAM = 5

    def _melody_lane(self, when: float, fallback: int) -> int:
        """The lane of the melody's note here, low notes left and high right
        across this part's range, or ``fallback`` with no melody."""
        import rider_layout

        section = self._section_at(when)
        if section is None or self._harmony is None:
            return fallback
        low, high = rider_layout.lead_range(self._harmony, section.start,
                                            section.end)
        lane = rider_layout.lead_lane(self._harmony, when, self.LANES, low,
                                      high)
        return fallback if lane is None else lane

    #: The road's colour by how much is going on: purple at the quietest
    #: through blue, green and yellow to red at the loudest.
    TIERS = (0.78, 0.60, 0.33, 0.15, 0.00)
    #: How far the synth may push the colour off its tier.
    TIER_SYNTH = 0.05

    def _tier(self, energy: float, synth: float) -> float:
        """The road's colour for this much energy. See TIERS."""
        place = max(0.0, min(1.0, energy)) * (len(self.TIERS) - 1)
        low = min(len(self.TIERS) - 2, int(place))
        share = place - low
        hue = self.TIERS[low] + (self.TIERS[low + 1] - self.TIERS[low]) * share
        return (hue + synth * self.TIER_SYNTH) % 1.0

    def _restyle(self, state) -> None:
        """Read the track's style again when the chart, shape, harmony or tempo
        changes. See trackstyle."""
        import trackstyle

        chart = getattr(state, "chart", None) or _NO_CHART
        contour = getattr(state, "contour", None)
        harmony = getattr(state, "harmony", None)
        rhythm = getattr(state, "rhythm", None)
        flux = getattr(state, "flux", None)
        # Keyed on the tempo the pane counts in, not the ride's own, which the
        # style itself decides.
        counted = bounded(getattr(state, "tempo", 0.0), most=1000.0)
        wanted = (id(chart), id(contour), id(harmony), id(rhythm),
                  round(counted, 3))
        if wanted == self._styled_from:
            return
        self._styled_from = wanted
        self._harmony = harmony
        if not contour and not chart:
            self._style = None
            self._plan = None
            return
        import rider_layout

        beat = 60.0 / counted if counted > 0.0 else 0.0
        self._style = trackstyle.read(
            chart, beat, self._grid if beat > 0.0 else None, contour,
            harmony, flux=flux, rhythm_found=rhythm, light=rhythm is None)
        self._plan = rider_layout.Plan(self._style, self._mode)
        self._slots = {}
        self._owed = {}

    def _section_at(self, when: float):
        """The part of the track ``when`` is in, once heard with a tempo;
        otherwise None."""
        if self._style is None or self._beat <= 0.0:
            return None
        return self._style.section_at(when)

    #: When a better plan arrives mid-ride, the road in view is kept this many
    #: seconds past the playhead and the new one faded in after it.
    PLAN_KEEP = 7.0
    PLAN_FADE = 4.0

    def _carve(self, state) -> None:
        """Make a road from the track's shape: the height is the loudness
        summed (quiet climbs, loud plunges; see ``_terrain``), and the curve
        is the stereo lean summed, with a turn a phrase long on top (see
        ``_bends``). Built again when any of that changes, never under the
        rider.
        """
        self._restyle(state)
        shape = getattr(state, "contour", None)
        # And the tempo, which the plan's sections and corkscrews are counted
        # in.
        wanted = (id(shape), id(self._style), round(self._beat, 4))
        if wanted == self._planned:
            return
        self._planned = wanted
        self._shaped = shape
        loud = (shape or {}).get("loud") or ()
        lean = (shape or {}).get("lean") or ()
        self._every = float((shape or {}).get("rate") or 0.0) or 1.0
        if not loud:
            self._hill = self._curve = self._energy = self._low = ()
            return
        # Kept as it came as well: the road's colour and how thick the figures
        # come both read it directly.
        self._energy = tuple(loud)
        self._low = tuple((shape or {}).get("low") or ())
        hill = self._terrain()
        curve, run = [], 0.0
        # The lean about its own middle, in units of how much this record
        # leans: a mix's constant bias would otherwise sum to a road that turns
        # one way forever, which looks straight.
        wide = [lean[index] if index < len(lean) else 0.0
                for index in range(len(loud))]
        middle = sum(wide) / len(wide)
        spread = math.sqrt(
            sum((value - middle) ** 2 for value in wide) / len(wide))
        bends = self._bends(len(loud))
        for index, value in enumerate(wide):
            # And no single reading may throw the road across.
            step = (value - middle) / (spread or 1.0)
            run += max(-self.LEAN_MOST, min(self.LEAN_MOST, step))
            curve.append(run + bends[index])
        curve = self._eased(curve)
        twists = self._find_twists()
        riding = self._heard > 0.5 and bool(self._hill)
        if riding:
            hill = self._splice(self._hill, hill, self._heard)
            curve = self._splice(self._curve, curve, self._heard)
            horizon = self._heard + self._kept_ahead() + self.PLAN_FADE
            twists = tuple(sorted(
                [start for start in self._twists if start < horizon]
                + [start for start in twists if start >= horizon]))
        else:
            self._twisted = set()
        self._hill = tuple(hill)
        self._curve = tuple(curve)
        self._twists = twists

    def _kept_ahead(self) -> float:
        """How far ahead the road is in view, in seconds, and so kept when
        re-planned."""
        ahead = self.PLAN_KEEP
        if self._clock:
            ahead = max(ahead, self._when(self.RIDER_AT + self.SEEN)
                        - self._heard)
        return ahead

    def _splice(self, old, new, when: float) -> tuple:
        """``new``, but ``old`` for as long as it is in view past ``when``,
        faded over PLAN_FADE and continuous where they meet."""
        if not old or not new:
            return tuple(new)
        keep = min(len(new) - 1,
                   int((when + self._kept_ahead()) * self._every))
        fade = max(1, int(self.PLAN_FADE * self._every))
        if keep <= 0:
            return tuple(new)
        pinned = old[min(keep, len(old) - 1)]
        shift = pinned - new[keep]
        out = [old[min(index, len(old) - 1)] for index in range(keep)]
        for index in range(keep, len(new)):
            moved = new[index] + shift
            before = old[index] if index < len(old) else moved
            share = min(1.0, (index - keep) / fade)
            share = share * share * (3.0 - 2.0 * share)
            out.append(before + (moved - before) * share)
        return tuple(out)

    #: The road's slope, in height a second per unit of loudness below the
    #: track's middle: quiet passages climb and loud ones run down.
    SLOPE = 2.4
    #: What a build adds to the climb towards its crest, and how hard and for
    #: how many bars the road falls into the drop.
    BUILD_CLIMB = 1.6
    DROP_PLUNGE = 4.5
    DROP_BARS = 2

    #: How much of the rise and fall the flat picture shows.
    FLAT_RELIEF = 0.3

    def _terrain(self) -> list:
        """The road's height as depth below its start, a reading at a time. See
        SLOPE."""
        energy = self._eased(self._energy)
        middle = sorted(energy)[len(energy) // 2]
        slope = [self.SLOPE * (middle - value) for value in energy]
        style = self._style
        if style is not None and style.sections and self._beat > 0.0:
            rate = self._every
            for section in style.sections:
                first = int(section.start * rate)
                last = min(len(slope), int(section.end * rate))
                if section.kind == "build":
                    for index in range(first, last):
                        share = (index - first) / max(1, last - first)
                        slope[index] += self.BUILD_CLIMB * share
                elif section.kind == "drop":
                    span = max(1, int(self.DROP_BARS * 4 * self._beat * rate))
                    for index in range(first, min(len(slope), first + span)):
                        share = (index - first) / span
                        slope[index] -= self.DROP_PLUNGE * (1.0 - share) ** 1.5
        depth, run = [], 0.0
        for value in slope:
            run -= value / self._every
            depth.append(run)
        return depth

    #: How hard a phrase's own turn pulls the road sideways, a second, at a
    #: drop.
    BEND_RATE = 9.0
    #: How much of that each kind of section gets: calm parts wind most.
    BEND_SHARE = {"drop": 0.6, "groove": 0.75, "build": 0.6,
                  "break": 1.0, "intro": 0.95, "outro": 0.95}

    def _bends(self, count: int) -> list:
        """A turn a phrase long, one way and then mostly the other: long sweeps
        on a steady four, every two bars on broken music, every four on half
        time. Its strength follows the section, and its direction the
        track's seed.
        """
        import random

        style = self._style
        if (style is None or self._beat <= 0.0 or not style.sections
                or count <= 0):
            return [0.0] * count
        weights = {8: style.steady * 0.6 + style.melodic + style.calm,
                   4: style.heavy + style.swung + style.steady * 0.4,
                   2: style.broken + style.hard}
        bars = max(weights, key=lambda key: weights[key])
        if weights[bars] <= 0.0:
            bars = 4
        rng = random.Random(style.seed ^ 0x5EED)
        length = bars * 4 * self._beat
        first = style.sections[0].start
        rate = self._every
        out, run = [], 0.0
        direction = rng.choice((-1.0, 1.0))
        segment = -1
        for index in range(count):
            when = index / rate
            here = int(max(0.0, when - first) // length)
            if here != segment:
                segment = here
                if rng.random() < 0.8:
                    direction = -direction
            section = style.section_at(when)
            share = self.BEND_SHARE.get(section.kind if section else "groove",
                                        0.6)
            phase = (max(0.0, when - first) % length) / length
            pull = math.sin(math.pi * phase) ** 2
            run += direction * self.BEND_RATE * share * pull / rate
            out.append(run)
        return out

    #: The corkscrews: how loud a moment must be (near the track's own peak),
    #: how long one takes, and the least road between two.
    TWIST_LOUD = 0.80
    TWIST_FOR = 2.5
    #: When a corkscrew's tunnel begins and ends around it, and how long its
    #: mouth takes to open or close, in seconds. See _tunnel_at.
    TUNNEL_LEAD = 0.8
    TUNNEL_TAIL = 0.6
    TUNNEL_RAMP = 0.5
    #: The tunnel's radius beyond the road's edge, and how high its middle is
    #: above the road.
    TUNNEL_ROOM = 1.7
    TUNNEL_MIDDLE = 0.9
    TWIST_APART = 25.0
    #: How long the city takes to whip round once, on its own, after a
    #: corkscrew's tunnel has closed behind the craft.
    EXIT_SPIN_FOR = 0.6

    def _find_twists(self) -> tuple:
        """The moments the road turns over: into the drops, landing level on a
        drop's first beat, biggest first; with no drops, the loudest
        moments. From the track, so a record corkscrews in the same places
        every time.
        """
        style = self._style
        if style is not None and self._beat > 0.0:
            drops = []
            for index, section in enumerate(style.sections):
                if section.kind != "drop":
                    continue
                before = style.sections[index - 1].level if index else 0.0
                drops.append((section.level - before, section.start))
            chosen: list = []
            for _step, start in sorted(drops, reverse=True):
                at = start - self.TWIST_FOR
                if at > 0.0 and all(abs(at - other) >= self.TWIST_APART
                                    for other in chosen):
                    chosen.append(at)
            if chosen:
                return tuple(sorted(chosen))
        if not self._energy or self._every <= 0.0:
            return ()
        loudest = max(self._energy)
        if loudest <= 0.0:
            return ()
        # Loud against this track as well as near its peak, so a track with no
        # dynamics never corkscrews.
        middle = sorted(self._energy)[len(self._energy) // 2]
        gate = max(loudest * self.TWIST_LOUD, (middle + loudest) / 2.0)
        # Further apart on calm music: twenty-five seconds with drums, up to
        # seventy-five without.
        apart = self.TWIST_APART * (1.0 + 2.0 * (
            style.calm if style is not None else 0.0))
        found = []
        for index, value in enumerate(self._energy):
            if value <= gate:
                continue
            at = index / self._every
            if found and at - found[-1] < apart:
                continue
            found.append(at)
        return tuple(found)

    def _tunnel_at(self, when: float) -> float:
        """How much of a corkscrew's tunnel there is at a moment, 0 to 1. The
        tunnel opens before the road turns and closes after it is level,
        with the city outside it. Drawn by rider_gl on the card and
        _flat_tunnel here."""
        best = 0.0
        for start in self._twists or ():
            enter = start - self.TUNNEL_LEAD
            leave = start + self.TWIST_FOR + self.TUNNEL_TAIL
            if when < enter:
                here = 1.0 - (enter - when) / self.TUNNEL_RAMP
            elif when > leave:
                here = 1.0 - (when - leave) / self.TUNNEL_RAMP
            else:
                here = 1.0
            best = max(best, here)
        return max(0.0, min(1.0, best))

    def _twist_at(self, when: float):
        """How far through a corkscrew a moment is, 0 to 1, or None."""
        for start in self._twists:
            if start <= when < start + self.TWIST_FOR:
                return (when - start) / self.TWIST_FOR
        return None

    @staticmethod
    def _turned(through: float) -> float:
        """The roll at that point of a corkscrew, in whole turns: still at both
        ends, quickest in the middle, and exactly one turn.
        """
        return through * through * (3.0 - 2.0 * through)

    def _exit_spin(self, when: float) -> float:
        """How far round the city's own quick turn is, in whole turns, in the
        moment after a corkscrew's tunnel has closed behind the craft: a
        whole turn in EXIT_SPIN_FOR, and nothing at any other time. The road
        and the craft stay where they are; only the world round them goes
        round.
        """
        for start in self._twists or ():
            since = when - (start + self.TWIST_FOR + self.TUNNEL_TAIL)
            if 0.0 <= since < self.EXIT_SPIN_FOR:
                return self._turned(since / self.EXIT_SPIN_FOR)
        return 0.0

    def _eased(self, table) -> tuple:
        """The readings averaged over a second or so, so the road follows the
        song's shape rather than its transients.
        """
        reach = max(1, int(self._every * self.SMOOTH_FOR))
        out = []
        for index in range(len(table)):
            low = max(0, index - reach)
            high = min(len(table), index + reach + 1)
            out.append(sum(table[low:high]) / (high - low))
        return tuple(out)

    def _read(self, table, when: float) -> float:
        """One reading of the track's shape between two of them, on a
        Catmull-Rom curve, so the road has no corners for its markings to
        splay at.
        """
        if not table:
            return 0.0
        place = when * self._every
        low = int(math.floor(place))
        if low < 0:
            return table[0]
        if low >= len(table) - 1:
            return table[-1]
        share = place - low
        # The two either side as well, held at the ends.
        before = table[max(0, low - 1)]
        here = table[low]
        after = table[low + 1]
        beyond = table[min(len(table) - 1, low + 2)]
        return 0.5 * (
            2.0 * here
            + (-before + after) * share
            + (2.0 * before - 5.0 * here + 4.0 * after - beyond)
            * share * share
            + (-before + 3.0 * here - 3.0 * after + beyond)
            * share * share * share)

    def _when(self, at: float, exact: bool = False) -> float:
        """The moment of the track a point on the road belongs to: exact,
        through each beat's lunge, for putting a block back; otherwise
        evenly through the beat, which the road's shape is read with."""
        reach = self._at + at - self.RIDER_AT
        if not self._clock:
            return (reach - self._road_shift) / self.FREE_RUN
        number = self._beat_at_road(reach)
        road = (reach - self._road_shift) / self.PER_BEAT
        covered = max(0.0, min(1.0, (road - self._start_of(number)) / max(
            1e-9, self._pace_of(number))))
        through = (self._uncovered(covered, self._lunge_of(number))
                   if exact else covered)
        return self._clock.time(number + through)

    def _uncovered(self, covered: float, lunge: float) -> float:
        """How far through a beat its road is ``covered`` along, 0 to 1: the
        inverse of _covered."""
        through = covered
        mix = self.LUNGE_MIX
        for _round in range(6):
            miss = self._covered(through, lunge) - covered
            if abs(miss) < 1e-12:
                break
            slope = (1.0 - mix) + mix * lunge * (1.0 - through) ** (lunge - 1.0)
            through = max(0.0, min(1.0, through - miss / max(1e-9, slope)))
        return through

    def _beat_at_road(self, units: float) -> int:
        """The beat whose road a point ``units`` along it is on."""
        road = (units - self._road_shift) / self.PER_BEAT
        if not self._starts or self._marks is None:
            return math.floor(road)
        first, starts = self._marks
        last = first + len(starts) - 1
        if road < starts[0]:
            return first - math.ceil((starts[0] - road)
                                     / max(1e-9, self._pace_of(first)))
        if road >= starts[-1]:
            return last + math.floor((road - starts[-1])
                                     / max(1e-9, self._pace_of(last)))
        return first + bisect.bisect_right(starts, road) - 1

    def _road(self, at: float) -> tuple:
        """Where the road is at distance ``at``: across, up and rolled. One
        function for every rung, block and the rider, so they all agree; the
        roll is the rate the road turns, so it banks into its own bends.
        """
        push = self.PUSH_REST + self._pushing * self.PUSH_GAIN
        if self._curve:
            return self._from_track(at, push)
        # Behind the rider the road runs straight: it is magnified there, and a
        # bend swung it across the frame.
        line = max(at, self.RIDER_AT)
        turn = math.cos(line * self.BEND_EVERY + self._bend) * self.BEND_EVERY
        return (math.sin(line * self.BEND_EVERY + self._bend)
                * self.BEND * push,
                math.sin(at * 0.11 + self._climb) * self.CLIMB * push,
                -turn * self.BEND * push * self.BANK)

    def _cruise(self) -> float:
        """The road's speed with nothing pushing it: a beat's worth of road a
        beat.
        """
        if self._beat > 0.0:
            return self.PER_BEAT / self._beat
        return self.FREE_RUN

    def _camera(self, rect, surge: float, bass: float) -> tuple:
        """Where the eye is: the vanishing point, focal length and bank. Eases
        the camera as well, so it is called once a frame.
        """
        span = min(rect.width(), rect.height())
        # How hard the passage is pushing, which the rig follows: the loudness,
        # eased, and only while the track plays.
        going = self._rolling
        self._rushing += ((self._loudness - self._rushing)
                          * self.RUSH_EASE * going)
        wide = max(0.0, min(1.0, self._rushing))
        # Never wider than FOCAL_FAST, or the road's edges bow.
        focal = span * max(self.FOCAL_FAST,
                           self.FOCAL_SLOW
                           + (self.FOCAL_FAST - self.FOCAL_SLOW) * wide
                           - surge * 0.06 - bass * 0.04)
        # A spring drags the eye back at a drop and lets it close in on a
        # climb.
        pull = (self.EYE_BACK * (1.0 + wide * self.CHASE) - self._chase)
        self._chase_to += pull * self.CHASE_SPRING * going
        self._chase_to *= self.CHASE_DAMP
        self._chase += self._chase_to * going
        # Where the road starts: a fixed distance in front of the eye, which
        # moves.
        self._near = self.NEAR_EYE - self._chase
        centre = rect.center()
        # The camera aims a little way down the road, so the track stays near
        # the middle of the frame and a bend becomes a lean. Eased.
        across_ahead, up_ahead, _roll = self._road(self.RIDER_AT + self.AIM)
        # Measured from the road under the rider: the part of the bend still
        # ahead.
        self._aimed += ((across_ahead - self._side - self._aimed)
                        * self.AIM_EASE * going)
        # The rise ahead of the rider, which is the hill.
        self._pitched += ((up_ahead - self._under - self._pitched)
                          * self.AIM_EASE * going)
        # How hard the road turns, which the view banks into.
        self._banked += (self._aimed - self._banked) * self.AIM_EASE * going
        # The shake: a decaying wobble on a clock in sixtieths of a second, the
        # same at any frame rate.
        self._wobble += self._went * 60.0
        shake = self._shake * self.SHAKE * self.SHAKE_LESS * span
        # Up the hill with the road, within reason. The sign matters: the other
        # way the camera runs from the hill.
        lift = max(-self.PITCH_MOST, min(self.PITCH_MOST,
                                         self._pitched * self.PITCH))
        horizon = QPointF(
            centre.x() - self._aimed * focal * self.AIM_PULL
            + math.sin(self._wobble * 1.9) * shake,
            centre.y() - rect.height() * (self.HORIZON_UP + lift)
            + math.sin(self._wobble * 2.7) * shake)
        # Negative on a right-hand bend, so the right of the horizon comes up.
        tilt = max(-self.TILT, min(self.TILT, -self._banked * self.TILT))
        # Not the corkscrew, which turns the world about the road: see
        # ``paint``.
        return horizon, focal, tilt

    def _from_track(self, at: float, push: float) -> tuple:
        """The road where the track says it goes; straight behind the rider.
        See ``_shape``.
        """
        line = max(at, self.RIDER_AT)
        when = self._when(line)
        # Not scaled by the loudness here: the plan already sets how hard each
        # part turns (see _bends).
        across = self._read(self._curve, when) * self.TRACK_BEND
        # The turn is the curve's slope here, read over a step of road.
        on = self._read(self._curve, self._when(line + 1.0))
        turn = (on * self.TRACK_BEND - across)
        # The height as it is: a sum over the whole track.
        return (across,
                self._read(self._hill, self._when(at)) * self._relief,
                -turn * self.BANK)

    def _eye(self, horizon, focal, lane_x: float, up: float, at: float):
        """A point on the road, on the glass. Heights are measured from the
        road under the rider, so the near end stays put and the hills keep
        their shape.
        """
        z = max(self.NEAR_EYE, at + self._chase)
        across, lift, roll = self._road(at)
        x = across + lane_x - self._side
        y = up + lift - self._under
        turn = roll * 0.5
        sx = x * math.cos(turn) - y * math.sin(turn)
        sy = x * math.sin(turn) + y * math.cos(turn)
        return QPointF(horizon.x() + focal * sx / z,
                       horizon.y() + focal * (sy + self.EYE_UP) / z)

    #: How far down the road, in road units, a block is in sight.
    IN_SIGHT = 60.0
    #: How long the arches and beat lines take to come up once there is a beat,
    #: in seconds of the track.
    BEAT_SHOW = 1.0

    #: A playhead move bigger than this in one frame is a seek, for callers
    #: with no count of jumps (see Spectrum.seek_to).
    JUMP = 0.35

    def _advance(self, state) -> float:
        self._last = time.monotonic()
        kit = {name: bounded(value)
               for name, value in (state.kit or {}).items()}
        bass = max(bounded(state.bass), kit.get("Bass", 0.0))
        loud = (bass + bounded(state.mid) + bounded(state.high)) / 3.0

        # The playhead: the pane's one moment a frame, taken as it is. A clock
        # of the scene's own ran ahead and on past a pause.
        said = bounded(getattr(state, "at", 0.0), most=self.LONGEST)
        first = self._was_at is None
        playing = getattr(state, "playing", None)
        if playing is None:
            moving = first or abs(said - self._was_at) > 1e-4
        else:
            moving = first or bool(playing)
        jumps = getattr(state, "jumps", None)
        if first:
            jumped = False
        elif jumps is not None:
            jumped = jumps != self._jumps_seen
        else:
            jumped = abs(said - self._was_at) > self.JUMP
        self._jumps_seen = jumps
        # How much of the track went by this frame, which everything animated
        # moves by, so a stopped track stops it all.
        if first or jumped or not moving:
            step = 0.0
        else:
            step = max(0.0, min(0.1, said - self._was_at))
        self._was_at = said
        self._rolling += ((1.0 if moving or said <= 0.0 else 0.0)
                          - self._rolling) * 0.25
        if self._rolling < 1e-4:
            self._rolling = 0.0
        elif self._rolling > 1.0 - 1e-4:
            self._rolling = 1.0
        if jumped:
            self._jumped_from = self._heard
        self._heard = said
        # A seek is a jump from somewhere the run has been. See _finish.
        self._jumped = jumped
        if first and self._heard >= self.START_AGAIN:
            self._whole = False

        # Bounded: a nan tempo is truthy, and a beat of a millionth of a second
        # would lay a million figures a second.
        tempo = bounded(getattr(state, "tempo", 0.0), most=1000.0)
        self._beat = 60.0 / tempo if tempo > 0.0 else 0.0
        self._pulse = bounded(getattr(state, "beat_at", 0.0))
        # The drums' own beats once known (see trackstyle.rhythm_of).
        style = self._style
        # While the drums' beat is on its way, the road runs free rather than
        # being counted on another grid first and laid twice.
        waiting = (bool(getattr(state, "rhythm_due", None))
                   and not (style is not None and style.from_drums))
        if waiting:
            self._beat = 0.0
        self._waiting = waiting
        drums = (style is not None and style.from_drums
                 and style.tempo > 0.0)
        if drums:
            drums_clock = style.clock()
            self._beat = drums_clock.length(said)
            self._pulse = drums_clock.number(said) % 1.0
        # A change of what the road is counted in lays the road again from
        # here, as a seek does, without being one.
        if drums:
            counted_in = ("drums", round(style.tempo, 4),
                          round(style.beat_phase, 4), len(style.beats))
        else:
            counted_in = ("pane", round(self._beat, 5))
        rebase = counted_in != self._counted_in and self._counted_in is not None
        self._counted_in = counted_in
        # The pane's grid, where that is what the road is counted on, nudged
        # towards the playhead by less than a beat while playing.
        if not drums and self._beat > 0.0 and said > 0.0:
            self._grid = said + (1.0 - self._pulse) * self._beat
            start = said - self._pulse * self._beat
            if self._origin is None or jumped or rebase:
                self._origin = start
            elif moving:
                off = (start - self._origin) / self._beat
                self._origin += (off - round(off)) * self._beat * 0.1
        if drums:
            clock = drums_clock
        elif self._beat > 0.0 and self._origin is not None:
            clock = BeatClock(self._beat, self._origin)
        else:
            clock = None
        # A beat to count from appearing or going is a change of coordinates;
        # see _road_shift.
        had_clock = bool(self._clock)
        onto_beat = bool(clock) != had_clock
        self._carried = None
        if onto_beat or jumped or rebase:
            if not jumped:
                # Where the road would be now on the old count, and where each
                # block in sight is on it, to carry them over.
                self._carried = self._world(self._heard)
                self._kept = [
                    (block, self.RIDER_AT + self._flat(block[0])
                     - self._carried)
                    for block in self._blocks
                    if self.RIDER_AT + self._flat(block[0]) - self._carried
                    <= self.IN_SIGHT]
            self._lunges = {}
            self._paces = {}
            self._starts = {}
            self._drives = {}
            self._marks = None
        self._clock = clock
        # From here, a frame's movement is however far the track moved, so a
        # stopped track stops everything.
        step *= self._rolling
        # How loud this passage is, followed only while the track plays.
        if self._rolling > 0.02:
            self._quick += (loud - self._quick) * (0.40 if loud > self._quick
                                                   else 0.03)
            if self._quiet is None:
                self._quiet = loud
            self._quiet += (self._quick - self._quiet) * (
                0.02 if self._quick < self._quiet else 0.0004)
            self._peak = max(self._quick, self._peak * 0.9996)
            self._loudness = self._surge()
        elif self._quiet is None:
            self._quiet = loud
        # What the bends are scaled by: the loudness followed slowly, so the
        # road does not jump when the music hits.
        self._pushing += ((self._loudness - self._pushing)
                          * (1.0 - math.exp(-step / self.PUSH_EASE)))
        self._slow = min(1.0, self._slow + self.SLOW_BACK * self._rolling)
        # Each beat's lunge, decided as it comes into view, and not while
        # stopped.
        beat_now = self._beat_number(self._heard)
        if self._rolling > 0.02 and beat_now is not None:
            self._decide_lunges(beat_now, bass)
        was, was_when = self._at, self._last_heard
        self._last_heard = self._heard
        rolled = self._world(self._heard)
        if self._carried is not None:
            # New beats to count in, not a new place: on from where the old
            # count had it.
            self._road_shift += self._carried - rolled
            rolled = self._carried
        # Never backwards, except across a seek.
        self._at = rolled if jumped else max(self._at, rolled)
        if self._kept:
            # The moment each kept block now stands for, on the new count.
            for block, where in self._kept:
                block[0] = self._when(where, exact=True)
            self._kept = []
        if self._clock:
            self._beat_shown = min(1.0, self._beat_shown
                                   + step / self.BEAT_SHOW)
        else:
            self._beat_shown = 0.0
        self._rebased = rebase or onto_beat
        # Against the track's clock, not the frame's, which is zero at a pause.
        went = self._heard - (was_when if was_when is not None
                              else self._heard)
        # Nothing across a seek, which re-bases the road.
        self._speed = (0.0 if jumped or went <= 1e-6
                       else (self._at - was) / went)
        self._drift_sparks(step)
        self._bend += step * (0.30 + self._loudness * 0.85)
        self._climb += step * (0.19 + self._loudness * 0.55)
        # Once a frame, after the road has moved: the road under the rider, up
        # and across, which the eye rides on.
        self._under = self._road(self.RIDER_AT)[1]
        self._side = self._road(self.RIDER_AT)[0]
        # On the track's clock, in sixtieths of a second.
        frames = step * 60.0
        self._shake = max(0.0, self._shake
                          - self._shake * min(1.0, self.SHAKE_FALL * frames)
                          - step * 0.9)
        self._shake = min(1.0, self._shake
                          + kit.get("Kick", 0.0) * 0.35 * min(1.0, frames))
        self._sore = max(0.0, self._sore - step)
        self._hurt = max(0.0, self._hurt - step / self.HURT_FOR)
        self._got = max(0.0, self._got - step / 0.35)
        if self._shield_back is not None:
            self._shield = min(1.0, self._shield + step / self._shield_back)
        self._coin_spin += step * self.COIN_TURN
        self._fly(step)
        self._age_pops(step)
        # How close the track is to a beat, 1 on it: held while stopped, so
        # nothing it lights creeps.
        if self._rolling > 0.02:
            self._beat_lit = ((1.0 - self._pulse) ** 3
                              if self._beat > 0.0 else 0.0)
        # How far over the road is turned, if at all, worked out once a frame.
        through = self._twist_at(self._heard)
        self._rolled = 0.0 if through is None else self._turned(through)
        self._spun = self._exit_spin(self._heard)
        #: How much of a sixtieth of a second this frame was, on the track's
        #: clock; the rig reads it. See ``_slide``.
        self._went = step
        self._burn(step)
        return step

    def _surge(self) -> float:
        """How loud this passage is between the quiet and the loudest."""
        quiet = self._quiet
        if quiet is None or self._peak < 0.04:
            return 0.0
        span = self._peak - quiet
        if span < 0.08:
            return 0.0
        return max(0.0, min(1.0, (self._quick - quiet) / span))

    #: Mono scoring is a chain: the first colour is worth one and each after it
    #: four more, up to two hundred. A grey breaks it.
    CHAIN_FIRST = 1
    CHAIN_STEP = 4
    CHAIN_MOST = 200
    #: And finishing without touching one is worth a third again.
    CLEAN_BONUS = 0.30

    #: What Ninja pays for a clean finish: twice Mono's.
    STEALTH_BONUS = 0.60

    #: Mono's bumpers: how long one takes to come back after shattering a grey.
    #: The first slip costs the shield and the second the chain. A shattered
    #: grey still ends a clean run.
    SHIELD_BACK = 8.0

    def _collide(self) -> None:
        # Over the lot of it: a jump clears the lane and collects none of it.
        if self._struck:
            self._forget_struck()
        if self._air > 0.0:
            for block in self._blocks:
                if not block[3] and self._heard >= block[0]:
                    block[3] = True
            return
        for block in self._blocks:
            when, lane, kind, done, grey = block
            if done or self._heard < when:
                continue
            block[3] = True
            on_it = abs(self._lane_at(lane) - self._lane_here) < self.FORGIVE
            if kind == "power":
                if on_it:
                    self._record(block, "taken")
                    self._double = self.POWER_DOUBLE
                    self._got = 1.0
                    self._burst(self._lane_at(lane), prize=True)
                    self._pop("power", hue=0.14, sat=0.08, text="DOUBLE")
                continue
            if kind == "coin":
                # Taken or missed. A missed coin ends the row.
                if on_it:
                    self._record(block, "taken")
                    # A coin long after the last starts a new row.
                    if (self._coin_last is not None
                            and when - self._coin_last > self.COIN_ROW_GAP):
                        self._coin_run = 0
                    self._coin_last = when
                    self._note(when, "coin")
                    self._coins += 1
                    self._coin_run += 1
                    self._coin_best = max(self._coin_best, self._coin_run)
                    self._score += self._paid(min(
                        self.COIN_MOST,
                        self.COIN_WORTH + (self._coin_run - 1) * self.COIN_STEP))
                    self._got = 1.0
                    self._burst(self._lane_at(lane), prize=True)
                    # Gold, and bigger the longer the row.
                    self._pop("coin", hue=0.13, sat=0.60,
                              strength=0.75 + min(0.75,
                                                  self._coin_run * 0.12))
                else:
                    self._coin_run = 0
                continue
            if not grey:
                self._offered += 1
            if grey and on_it:
                if self._shield >= 1.0:
                    # Shattered rather than hit; the clean run is still over.
                    self._shield = 0.0
                    self._saves += 1
                    self._record(block, "shatter")
                    self._note(when, "saved")
                    self._clean = False
                    self._sore = self.SORE
                    self._shake = min(1.0, self._shake + 0.35)
                    self._burst(self._lane_at(lane), prize=True)
                    # A cold white ring, not the red of a hit.
                    self._pop("shatter", hue=0.55, sat=0.30,
                              text="SHIELD")
                    continue
                lost = self._chain
                self._record(block, "hit")
                self._note(when, "hit")
                self._hits += 1
                self._streak = 0
                self._chain = 0
                self._coin_run = 0
                self._clean = False
                self._sore = self.SORE
                # Each hit lands again, and one hard on the last lands harder.
                combo = self._combo("hit", when)
                self._shake = min(1.0 + 0.15 * (combo - 1),
                                  self._shake + 0.8)
                self._slow = self.SLOW
                self._hurt = 1.0
                self._burst(self._lane_at(lane))
                # Say what it cost, when a long chain went.
                self._pop("hit", hue=0.0, sat=0.95,
                          strength=1.0 + min(0.5, lost / 80.0)
                          + self.COMBO_LIFT * (combo - 1),
                          text=(f"CHAIN LOST  {lost}"
                                if lost >= self.LOST_WORTH else ""))
            elif grey:
                self._streak += 1
                self._best = max(self._best, self._streak)
            elif on_it:
                if self._mode == "Puzzle":
                    # Worth nothing on its own: it goes in the grid.
                    if self._stunned <= 0.0:
                        self._record(block, "taken")
                        self._note(when, "taken")
                        self._taken += 1
                        self._drop(self._tier_of(when), lane)
                        self._got = 1.0
                        self._burst(self._lane_at(lane), prize=True)
                        self._pop("prize", strength=0.8)
                else:
                    # A prize. See CHAIN_FIRST.
                    before = self._chain
                    self._record(block, "taken")
                    self._note(when, "taken")
                    self._chain += 1
                    self._taken += 1
                    self._chain_most = max(self._chain_most, self._chain)
                    self._score += self._paid(int(min(
                        self.CHAIN_MOST,
                        self.CHAIN_FIRST
                        + (self._chain - 1) * self.CHAIN_STEP) * self._double))
                    self._double = 1.0
                    self._got = 1.0
                    self._burst(self._lane_at(lane), prize=True)
                    # Bigger as the run gets hotter, and again for one hard on
                    # the last.
                    combo = self._combo("prize", when)
                    self._pop("prize", strength=0.7 + self._heat() * 0.6
                              + self.COMBO_LIFT * min(4, combo - 1))
                    self._milestone(before, self._chain)
            else:
                self._note(when, "missed")

    #: The puzzle grid: collected blocks drop into three columns, six deep, and
    #: three or more of a colour touching clear and pay.
    CELLS_WIDE = 3
    CELLS_DEEP = 6

    #: What a colour is worth, by the tier of the passage that produced it.
    WORTH = (10, 20, 30, 50, 80)

    #: How long a cluster sits before it goes, reset whenever another block of
    #: its colour joins it: the window that grows three into nine.
    FUSE = 0.75

    #: An overfilled column locks the grid for this long and breaks the chain.
    STUN = 3.0

    def _drop(self, colour: int, column: int) -> None:
        """Put a collected block into the grid, and see what it does."""
        column = max(0, min(self.CELLS_WIDE - 1, column))
        pile = self._cells[column]
        if len(pile) >= self.CELLS_DEEP:
            # An eighth block in a column of seven. The grid locks.
            self._stunned = self.STUN
            self._chain = 0
            self._streak = 0
            self._shake = min(1.0, self._shake + 0.6)
            self._hurt = max(self._hurt, 0.7)
            return
        pile.append(colour)
        self._fuse_up()

    def _clusters(self) -> list:
        """Every run of three or more touching blocks of one colour: a flood
        fill, four connected.
        """
        seen = set()
        found = []
        for column in range(self.CELLS_WIDE):
            for row in range(len(self._cells[column])):
                if (column, row) in seen:
                    continue
                colour = self._cells[column][row]
                group = []
                edge = [(column, row)]
                seen.add((column, row))
                while edge:
                    at = edge.pop()
                    group.append(at)
                    for step in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                        near = (at[0] + step[0], at[1] + step[1])
                        if near in seen:
                            continue
                        if not 0 <= near[0] < self.CELLS_WIDE:
                            continue
                        pile = self._cells[near[0]]
                        if not 0 <= near[1] < len(pile):
                            continue
                        if pile[near[1]] != colour:
                            continue
                        seen.add(near)
                        edge.append(near)
                if len(group) >= 3:
                    found.append((colour, group))
        return found

    def _fuse_up(self) -> None:
        """Start or restart the fuse if anything is matched, so a cluster can
        be grown.
        """
        found = self._clusters()
        if not found:
            self._fuse = 0.0
            self._fused = 0
            return
        size = sum(len(group) for _colour, group in found)
        if size != self._fused:
            self._fuse = self.FUSE
            self._fused = size

    def _burn(self, step: float) -> None:
        """Run the fuse down, and clear what it was holding."""
        if self._stunned > 0.0:
            self._stunned = max(0.0, self._stunned - step)
            return
        if self._fuse <= 0.0:
            return
        self._fuse -= step
        if self._fuse > 0.0:
            return
        self._fuse = 0.0
        self._fused = 0
        going = self._clusters()
        if not going:
            return
        for colour, group in going:
            # Quadratic in the size, so one cluster of six is worth twice two
            # of three; doubled if a power block is waiting.
            self._score += self._paid(int(self.WORTH[colour] * len(group)
                                          * len(group) * self._double))
            self._cleared += len(group)
        # A cluster going gets an answer to match its size.
        biggest = max(len(group) for _colour, group in going)
        self._pop("clear", hue=self.TIERS[going[0][0]], sat=0.85,
                  strength=0.7 + min(0.8, (biggest - 3) * 0.2),
                  text=(f"CLEAR {biggest}" if biggest >= 5 else ""))
        self._double = 1.0
        going_cells = {at for _colour, group in going for at in group}
        for column in range(self.CELLS_WIDE):
            self._cells[column] = [
                colour for row, colour in enumerate(self._cells[column])
                if (column, row) not in going_cells]
        # And anything left standing falls, which may match again.
        self._fuse_up()

    def _tier_of(self, when: float) -> int:
        """Which colour a block laid at this moment is."""
        if not self._energy:
            return len(self.WORTH) // 2
        energy = max(0.0, min(1.0, self._read(self._energy, when)))
        return min(len(self.WORTH) - 1, int(energy * len(self.WORTH)))

    def _worth(self) -> int:
        """The score with the clean-finish bonus in, if still kept: what the
        run is worth if it ends now. The running score shows what has been
        earned, and the bonus is paid at the end.
        """
        if not self._clean:
            return self._score
        return int(self._score * (1.0 + self.bonus()))

    def bonus(self, mode: Optional[str] = None) -> float:
        """The share a clean finish adds, in ``mode`` or this one."""
        return (self.STEALTH_BONUS if (mode or self._mode) == "Ninja"
                else self.CLEAN_BONUS)

    def _burst(self, across: float, prize: bool = False) -> None:
        """Throw pieces off the block just taken or hit: a prize throws fewer,
        upwards.
        """
        how_many = self.SPARKS // 2 if prize else self.SPARKS
        for index in range(how_many):
            angle = (index / max(1, how_many)) * math.tau + self._at
            self._sparks.append([
                across, 0.0, self.RIDER_AT,
                math.cos(angle) * self.SPARK_GO * (0.14 if prize else 0.22),
                -abs(math.sin(angle)) * self.SPARK_GO
                * (0.26 if prize else 0.16),
                math.sin(angle * 1.7) * self.SPARK_GO * 0.12,
                1.0])

    def _drift_sparks(self, step: float) -> None:
        """Move the pieces on and drop the ones that have gone out."""
        if not self._sparks:
            return
        alive = []
        for spark in self._sparks:
            spark[6] -= step * self.SPARK_FADE
            if spark[6] <= 0.0:
                continue
            spark[0] += spark[3] * step
            spark[1] += spark[4] * step
            spark[2] += spark[5] * step - step * self._speed * 0.12
            spark[4] += step * 2.4      # they come back down
            alive.append(spark)
        self._sparks = alive[-80:]

    def _lane_at(self, lane: int) -> float:
        return (lane - (self.LANES - 1) / 2.0) * self.LANE_WIDE

    def _beat_number(self, when: float):
        """Which beat of the track a moment is in, or None without one."""
        if not self._clock:
            return None
        return math.floor(self._clock.number(when))

    def _world(self, when: float) -> float:
        """Where the road is at a moment, in road units: each beat is a length
        of road of its own (see _decide_lunges), so the road reaches beat n
        exactly when it is played; inside a beat the bass front-loads the
        travel.
        """
        if not self._clock:
            return when * self.FREE_RUN + self._road_shift
        beats = self._clock.number(when)
        whole = math.floor(beats)
        return (self._start_of(whole) + self._pace_of(whole)
                * self._covered(beats - whole, self._lunge_of(whole))
                ) * self.PER_BEAT + self._road_shift

    def _covered(self, through: float, lunge: float) -> float:
        """How much of a beat's road is covered ``through`` (0 to 1) it."""
        lunged = 1.0 - (1.0 - through) ** lunge
        return through * (1.0 - self.LUNGE_MIX) + lunged * self.LUNGE_MIX

    def _lunge_of(self, number: int) -> float:
        """The lunge of beat ``number``, as decided, or the undecided one
        beyond."""
        found = self._lunges.get(number)
        return self._lunge if found is None else found

    def _pace_of(self, number: int) -> float:
        """How long beat ``number``'s road is against PER_BEAT, as decided or
        the nearest decided beyond."""
        found = self._paces.get(number)
        if found is not None:
            return found
        if not self._paces:
            return 1.0
        last = max(self._paces)
        if number > last:
            return self._paces[last]
        return self._paces[min(self._paces)]

    def _start_of(self, number: int) -> float:
        """Where beat ``number`` starts on the road, in lengths of PER_BEAT."""
        found = self._starts.get(number)
        if found is not None:
            return found
        if not self._starts:
            return float(number)
        last = max(self._starts)
        if number > last:
            return self._starts[last] + (number - last) * self._pace_of(last)
        first = min(self._starts)
        return self._starts[first] - (first - number) * self._pace_of(first)

    def beat_on_road(self, number: int) -> float:
        """Where beat ``number`` is on the road, in road units, for anything
        that marks the beats."""
        return self._start_of(number) * self.PER_BEAT + self._road_shift

    def gate_light(self, number: int) -> float:
        """How brightly the arch on beat ``number`` is lit: fully where the
        music drives, turned down where it is calm."""
        drive = self._drives.get(number)
        if drive is None:
            drive = 0.5
        return (self.GATE_CALM + (1.0 - self.GATE_CALM) * drive
                ) * self._beat_shown

    #: How lit the arches are in the calmest music, against a drop's.
    GATE_CALM = 0.12

    def bar_place(self, number: int) -> int:
        """Which beat of its bar beat ``number`` is: 0 is the first."""
        if not self._clock:
            return number % 4
        return int(round(self._clock.in_bar(number))) % 4

    #: How fast the road runs against PER_BEAT, at its calmest and at its
    #: heaviest. See _pace_target.
    PACE_LEAST = 0.4
    PACE_MOST = 2.0
    #: What the kind of section adds to how driven a beat is.
    PACE_BY_KIND = {"drop": 0.35, "groove": 0.0,
                    "break": -0.4, "intro": -0.3, "outro": -0.3}
    #: A build's, from its start to its end: it speeds up into the drop.
    PACE_BUILD = (-0.35, 0.15)
    #: How much of the way to its target each beat's pace goes.
    PACE_EASE = 0.3
    #: What the road falling away adds to its pace, over what the music's
    #: drive sets: the plunge into a drop is a rush.
    PACE_FALL = 0.5
    #: The least warning a block gets however fast the road runs, in seconds,
    #: where the level does not say.
    LEAST_WARNING = 0.6
    #: How far past the craft every beat's road is decided, in road units.
    SEEN = 80.0

    def _pace_target(self, number: int) -> tuple:
        """How fast the road wants to run over beat ``number``, and how driven
        the music is there, 0 to 1."""
        if not self._energy:
            # Nothing known about the track's shape yet.
            return 1.0, 0.5
        when = self._clock.time(number + 0.5)
        loud = self._read(self._energy, when)
        low = self._read(self._low, when) if self._low else loud
        drive = 0.55 * max(0.0, min(1.0, loud)) + 0.45 * max(0.0, min(1.0, low))
        section = self._section_at(when)
        if section is not None and section.kind == "build":
            through = max(0.0, min(1.0, (when - section.start)
                                   / max(1e-6, section.end - section.start)))
            first, last = self.PACE_BUILD
            drive += first + (last - first) * through
        elif section is not None:
            drive += self.PACE_BY_KIND.get(section.kind, 0.0)
        # Pushed towards its ends, so calm is calm and a drop is a drop.
        drive = max(0.0, min(1.0, (drive - 0.15) / 0.7))
        drive = drive * drive * (3.0 - 2.0 * drive)
        pace = self.PACE_LEAST + (self.PACE_MOST - self.PACE_LEAST) * drive
        beat = self._clock.length(when)
        if beat > 0.0:
            pace += self.PACE_FALL * self._falling(when, beat)
            pace = min(pace, self.LOOK_BEATS * beat / self._least_warning)
        return pace, drive

    def _falling(self, when: float, beat: float) -> float:
        """How steeply the road falls over the beat from ``when``, as a share
        of a drop's plunge, 0 to 1; nothing where it climbs."""
        if not self._hill:
            return 0.0
        fall = (self._read(self._hill, when + beat)
                - self._read(self._hill, when)) / beat
        return max(0.0, min(1.0, fall / self.DROP_PLUNGE))

    def _decide_lunges(self, now: int, bass: float) -> None:
        """Give every beat coming into view its lunge and its length of road,
        once, before anything on it is seen: from the track's loudness at
        that beat where it is known, otherwise the bass now, eased after a
        hit.
        """
        # From the beat after the last decided, out past everything shown.
        number = now if not self._paces else min(now, max(self._paces) + 1)
        horizon = None
        changed = False
        while True:
            if number not in self._lunges:
                changed = True
                push = bass
                if self._energy and self._clock:
                    push = self._read(self._energy, self._clock.time(number))
                push = max(0.0, min(1.0, push))
                self._lunges[number] = 1.0 + push * self._slow * self.LUNGE
                before = number - 1
                if before in self._paces:
                    start = self._starts[before] + self._paces[before]
                    eased = self._paces[before]
                else:
                    start = self._start_of(number)
                    eased = None
                target, drive = (self._pace_target(number) if self._clock
                                 else (1.0, 0.5))
                pace = (target if eased is None
                        else eased + (target - eased) * self.PACE_EASE)
                self._starts[number] = start
                self._paces[number] = pace
                self._drives[number] = drive
            if number >= now and horizon is None:
                horizon = self._start_of(now) + self.SEEN / self.PER_BEAT
            if horizon is not None and self._start_of(number) > horizon:
                break
            number += 1
        for older in [n for n in self._lunges if n < now - 2]:
            changed = True
            del self._lunges[older]
            self._paces.pop(older, None)
            self._starts.pop(older, None)
            self._drives.pop(older, None)
        if changed:
            first = min(self._starts)
            self._marks = (first, [self._starts[n] for n in
                                   range(first, max(self._starts) + 1)])

    def _where(self, when: float) -> float:
        """How far down the road a hit due at ``when`` is now: zero is level
        with the rider.
        """
        return self.RIDER_AT + self._flat(when) - self._at

    def _flat(self, when: float) -> float:
        """Where a moment sits on the road, on the road's own curve."""
        return self._world(when)

    def _slide(self, step: float) -> float:
        """How much of the way to the wanted lane this frame is worth: SNAP per
        sixtieth of a second on the track's clock, the same at any frame
        rate.
        """
        if step <= 0.0:
            return 0.0
        return 1.0 - (1.0 - self.SNAP) ** (step * 60.0)

    def _step(self, state) -> float:
        """The game, one frame on, before anything is drawn."""
        self._carve(state)
        step = self._advance(state)
        self._lay(state)
        wanted = self._lane_at(self._lane)
        was_across = self._lane_here
        self._lane_here += (wanted - self._lane_here) * self._slide(step)
        # How hard the craft moves sideways, for the bank: on the track's clock
        # and eased.
        if step > 0.0:
            self._swerve += ((self._lane_here - was_across) / step
                             - self._swerve) * self.SWERVE_EASE
        self._collide()
        self._finish(state)
        return step

    def _note(self, when: float, how: str) -> None:
        """A block's outcome, for the ride drawn at the end; nothing after the
        finish."""
        if not self._finished:
            self._log.append((when, how, self._hue_now))

    def _record(self, block, how: str) -> None:
        # The block itself is kept with its outcome: an id is only unique while
        # the object lives.
        self._struck[id(block)] = (block, how)

    def _forget_struck(self) -> None:
        # Only blocks still on the road.
        alive = {id(block) for block in self._blocks}
        self._struck = {key: kept for key, kept in self._struck.items()
                        if key in alive}

    def struck(self, block) -> Optional[str]:
        """What the craft did to ``block`` as the game scored it: "taken",
        "hit", "shatter", or None. Drawing and sound ask this, so they
        cannot disagree with the score.
        """
        kept = self._struck.get(id(block))
        return kept[1] if kept is not None and kept[0] is block else None

    #: How close to the end counts as the end.
    FINISH_BEFORE = 0.3

    @staticmethod
    def _length(state) -> float:
        """How long the track is, from its contour; 0 until there is one."""
        shape = getattr(state, "contour", None) or {}
        loud = shape.get("loud") or ()
        rate = float(shape.get("rate") or 0.0)
        return len(loud) / rate if loud and rate > 0.0 else 0.0

    #: How near the start a seek has to land to be a new run.
    START_AGAIN = 2.0

    def _finish(self, state) -> None:
        """The end of the track ends the run, and going back to the start
        begins a new one. Any other seek, or starting part way in, makes a
        run that is judged but keeps no best.
        """
        if self._jumped:
            if (self._heard < self.START_AGAIN
                    and self._jumped_from >= self.START_AGAIN):
                self.reset()
                return
            if self._heard >= self.START_AGAIN:
                self._whole = False
        length = self._length(state)
        if length <= 0.0:
            return
        at = bounded(getattr(state, "at", 0.0), most=self.LONGEST)
        if self._finished:
            if at < min(2.0, length * 0.5):
                # Back to the start: a new run.
                self.reset()
            return
        if at >= length - self.FINISH_BEFORE:
            self._finished = True
            self._result = self.result()
            shape = getattr(state, "contour", None) or {}
            self._ridden = (list(shape.get("loud") or ()),
                            float(shape.get("rate") or 0.0), length)
            self._pop("finish", hue=0.13, sat=0.5, strength=1.4)

    def result(self) -> dict:
        """How the run went, as it stands: what the end of a track shows."""
        share = self._taken / self._offered if self._offered else 0.0
        return {
            "worth": self._worth(), "score": self._score,
            "grade": self.grade(share, self._hits), "share": share,
            "taken": self._taken, "offered": self._offered,
            "chain": self._chain_most, "coins": self._coins,
            "hits": self._hits, "saves": self._saves,
            "clean": self._clean, "mode": self._mode,
            "airs": self._airs, "cleared": self._cleared,
            "whole": self._whole, "difficulty": self._difficulty,
        }

    #: The grades, best first: the share of prizes taken and the most greys
    #: hit.
    GRADES = (("S", 0.95, 0), ("A", 0.85, 2), ("B", 0.70, 5), ("C", 0.50, 10))

    @classmethod
    def grade(cls, share: float, hits: int) -> str:
        """A letter for a run; both conditions must be met."""
        for letter, least, most in cls.GRADES:
            if share >= least and hits <= most:
                return letter
        return "D"

    def paint_on_card(self, painter, rect, state, world) -> None:
        """The same game drawn as a lit world on the graphics card (see
        rider_gl), with the words and numbers still drawn by the painter.
        """
        import time as _time

        from PySide6.QtCore import QRect
        from PySide6.QtGui import QOpenGLContext

        self._relief = 1.0
        self._step(state)
        kit = state.kit or {}
        bass = max(state.bass, kit.get("Bass", 0.0))
        surge = self._loudness
        # Kept moving: the world reads the eye's springs and the wobble too.
        self._camera(rect, surge, bass)
        self._hue_now = self._tier(surge, state.synth)
        device = painter.device()
        ratio = device.devicePixelRatioF() or 1.0
        box = painter.worldTransform().mapRect(rect)
        # The target's height in its own pixels, which a GL viewport counts
        # from the bottom.
        size = getattr(device, "size", None)
        tall = (int(size().height()) if callable(size)
                else int(round(device.height() * ratio)))
        x = int(round(box.x() * ratio))
        y = int(round(box.y() * ratio))
        w = max(1, int(round(box.width() * ratio)))
        h = max(1, int(round(box.height() * ratio)))
        painter.beginNativePainting()
        try:
            gl = QOpenGLContext.currentContext().functions()
            target = gl.glGetIntegerv(0x8CA6)
            if isinstance(target, (list, tuple)):
                target = target[0]
            world.draw(self, state, int(target), QRect(x, tall - y - h, w, h),
                       painter.opacity(), _time.monotonic(),
                       samples=int(getattr(world, "samples", 4)))
        finally:
            painter.endNativePainting()
        self._hud_on_card(painter, rect, state, world)

    def _hud_on_card(self, painter, rect, state, world) -> None:
        """What is read rather than seen, over the world."""
        import rider_gl

        hud = getattr(world, "hud", None)
        if hud is None:
            hud = world.hud = rider_gl.Hud()
        painter.save()
        try:
            for kind, age, strength, hue, sat, text in self._pops:
                if not text:
                    continue
                spec = self.POPS.get(kind)
                if spec is None:
                    continue
                through = min(1.0, age / max(1e-6, spec[0]))
                hud.callout(painter, rect, text,
                            self._hue_now if hue is None else hue,
                            through, strength)
        finally:
            painter.restore()
        if self._mode == "Puzzle":
            self._matrix(painter, rect)
        hud.draw(painter, rect, self, state)
        self._results(painter, rect)

    def paint(self, painter, rect, state) -> None:
        self._relief = self.FLAT_RELIEF
        self._step(state)

        flash = self.flash(state)
        surge = self._loudness
        kit = state.kit or {}
        bass = max(state.bass, kit.get("Bass", 0.0))

        painter.fillRect(rect, QColor(3, 2, 8))
        # The same morphing field as Ambience, behind everything and dim.
        self._plasma.paint(painter, rect, state,
                           strength=0.07 + surge * 0.07 + flash * 0.05,
                           flash=flash, going=self._rolling)

        horizon, focal, tilt = self._camera(rect, surge, bass)
        hue = self._tier(surge, state.synth)
        self._hue_now = hue
        # How close the track is to a beat, 1 on it; see ``_advance``.
        beat = self._beat_lit

        # The view banks into the bend about the horizon, and a hit adds a fast
        # roll on top for as long as it lasts.
        lean = tilt + (math.sin(self._wobble * 2.3) * self._hurt
                       * self.HURT_THROW)
        # A corkscrew turns the world round the road, not the road: the road,
        # what is on it and the craft stay put on the glass. The world is drawn
        # first, so the road passes in front of it.
        painter.save()
        painter.translate(horizon)
        painter.rotate(lean + (self._rolled + self._spun) * 360.0)
        painter.translate(-horizon)
        self._glow(painter, rect, horizon, hue, surge, bass, beat, flash)
        self._flat_tunnel(painter, rect, horizon, focal, hue, beat, flash)
        self._pillars(painter, horizon, focal, hue, kit, beat, flash)
        painter.restore()

        painter.save()
        painter.translate(horizon)
        painter.rotate(lean)
        painter.translate(-horizon)
        self._surface(painter, rect, horizon, focal, hue, surge,
                      flash + beat * 0.35)
        self._lanes(painter, horizon, focal, hue, beat, flash)
        self._markings(painter, horizon, focal, hue, kit,
                       flash + beat * 0.45)
        self._edges(painter, rect, horizon, focal, hue, kit,
                    flash + beat * 0.30)
        self._walls(painter, rect, horizon, focal, hue, flash)
        self._bits(painter, horizon, focal, hue)
        self._ship(painter, rect, horizon, focal, hue, flash,
                   max(beat, kit.get("Kick", 0.0)))
        # Where the craft landed on the glass, for the answers below to come
        # from.
        if self._craft_spot is not None:
            self._craft_glass = QTransform().translate(
                horizon.x(), horizon.y()).rotate(lean).translate(
                -horizon.x(), -horizon.y()).map(self._craft_spot)
        painter.restore()
        # The beat, on the grid and on the drum both.
        self._rim(painter, rect, hue,
                  max(beat, kit.get("Kick", 0.0)))
        self._pops_now(painter, rect)
        self._wash(painter, rect)
        if self._mode == "Puzzle":
            self._matrix(painter, rect)
        self._card(painter, rect, hue)
        self._results(painter, rect)

    #: Where the grid sits and how big it is, as shares of the frame: bottom
    #: left, out of the road's way.
    CELL_AT = (0.035, 0.96)
    CELL_SIDE = 0.038
    CELL_GAP = 0.15

    def matrix_box(self, rect) -> QRectF:
        """Where the grid is in ``rect``, for the grid and for anything that
        must keep out of its way."""
        side = min(rect.width(), rect.height()) * self.CELL_SIDE
        step = side * (1.0 + self.CELL_GAP)
        left = rect.left() + rect.width() * self.CELL_AT[0]
        floor = rect.top() + rect.height() * self.CELL_AT[1]
        return QRectF(left, floor - self.CELLS_DEEP * step,
                      (self.CELLS_WIDE - 1) * step + side,
                      self.CELLS_DEEP * step)

    def _matrix(self, painter, rect) -> None:
        """The puzzle grid in the corner, drawn from the bottom up."""
        side = min(rect.width(), rect.height()) * self.CELL_SIDE
        step = side * (1.0 + self.CELL_GAP)
        box = self.matrix_box(rect)
        left = box.left()
        floor = box.bottom()
        painter.setPen(Qt.PenStyle.NoPen)
        # The well first, so an empty column still reads as a column.
        stunned = self._stunned > 0.0
        # Flashing while locked.
        lit = stunned and int(self._stunned * 12) % 2 == 0
        well = (QColor(255, 60, 60, 90) if lit
                else QColor(255, 255, 255, 28 if stunned else 16))
        painter.setBrush(well)
        for column in range(self.CELLS_WIDE):
            for row in range(self.CELLS_DEEP):
                painter.drawRect(QRectF(
                    left + column * step,
                    floor - (row + 1) * step, side, side))
        for column in range(self.CELLS_WIDE):
            for row, colour in enumerate(self._cells[column]):
                # A cluster about to go pulses.
                going = self._fuse > 0.0
                shade = QColor.fromHsvF(
                    self.TIERS[min(colour, len(self.TIERS) - 1)],
                    0.85, 1.0,
                    0.95 if not going else 0.55 + 0.45
                    * abs(math.sin(self._fuse * 14.0)))
                painter.setBrush(shade)
                painter.drawRect(QRectF(
                    left + column * step,
                    floor - (row + 1) * step, side, side))
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _wash(self, painter, rect) -> None:
        """What a hit does to the whole picture: the frame goes red and dark
        from its edges in. Drawn only while it fades.
        """
        if self._hurt <= 0.0:
            return
        # Strongest at the hit and gone in HURT_FOR, front-loaded.
        hurt = self._hurt * self._hurt
        # Multiplied, not washed over, so the frame goes red and dark rather
        # than brighter.
        painter.save()
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Multiply)
        painter.fillRect(rect, QColor(
            255,
            int(255 - (255 - 46) * hurt * self.HURT_DIM),
            int(255 - (255 - 38) * hurt * self.HURT_DIM)))
        painter.restore()
        # A rim of red light from the edges in.
        middle = rect.center()
        edge = QRadialGradient(middle, max(rect.width(), rect.height()) * 0.62)
        edge.setColorAt(0.0, QColor(150, 12, 16, 0))
        edge.setColorAt(0.6, QColor(150, 12, 16, 0))
        edge.setColorAt(1.0, QColor(160, 10, 14,
                                    int(255 * hurt * self.HURT_WASH)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(edge)
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _rung(self, horizon, focal, at: float, out: float = 0.0):
        """The two ends of the road at ``at``, as a pair of points."""
        edge = self.LANE_WIDE * self.LANES / 2.0 + out
        return (self._eye(horizon, focal, -edge, 0.0, at),
                self._eye(horizon, focal, edge, 0.0, at))

    def _surface(self, painter, rect, horizon, focal, hue, surge,
                 flash) -> None:
        """The road itself, filled: one path of quads between consecutive
        rungs.
        """
        reach = self.FAR - self._near
        offset = self._at % 1.0
        deck = QPainterPath()
        last = None
        near_y = far_y = None
        for step in range(self.RUNGS + 1):
            at = self._near + (step + offset) * reach / self.RUNGS
            here = self._rung(horizon, focal, at)
            if last is not None:
                deck.moveTo(last[0])
                deck.lineTo(last[1])
                deck.lineTo(here[1])
                deck.lineTo(here[0])
                deck.closeSubpath()
            else:
                near_y = here[0].y()
            far_y = here[0].y()
            last = here
        painter.setPen(Qt.PenStyle.NoPen)
        # Into fog at the far end. Kept dark, so the blocks on it are the
        # bright thing.
        shade = QColor.fromHsvF(
            (hue + 0.02) % 1.0, 0.85 - flash * 0.2,
            self.ROAD_LIT + surge * 0.04 + flash * 0.05, 1.0)
        gone = QColor(shade)
        gone.setAlphaF(0.0)
        fog = QLinearGradient(0.0, far_y if far_y is not None else 0.0,
                              0.0, near_y if near_y is not None else 1.0)
        fog.setColorAt(0.0, gone)
        fog.setColorAt(self.FOG, shade)
        fog.setColorAt(1.0, shade)
        painter.fillPath(deck, QBrush(fog))

    #: How far down the road the fog has cleared, and how light the road's
    #: surface is. See ``_surface``.
    ROAD_LIT = 0.085

    FOG = 0.35
    #: How solid a block is at the far end of the road: never so faint that it
    #: cannot be read in time.
    FOG_LEAST = 0.55

    #: How far apart the chevrons under the road are, in road units, and how
    #: many rungs each covers.
    MARK_EVERY = 2.0

    def _markings(self, painter, horizon, focal, hue, kit, flash) -> None:
        """The chevrons on the road, streaming at the road's speed, brightening
        on the kick.
        """
        kick = kit.get("Kick", 0.0)
        marks = QPainterPath()
        first = math.ceil((self._near + self._at) / self.MARK_EVERY)
        for index in range(first, first + int(self.FAR / self.MARK_EVERY) + 2):
            at = index * self.MARK_EVERY - self._at
            if not self._near <= at <= self.FAR:
                continue
            for lane in range(self.LANES):
                across = self._lane_at(lane)
                wide = self.LANE_WIDE * 0.30
                marks.moveTo(self._eye(horizon, focal, across - wide, 0.0, at))
                marks.lineTo(self._eye(horizon, focal, across, 0.0,
                                       at + self.MARK_EVERY * 0.22))
                marks.lineTo(self._eye(horizon, focal, across + wide, 0.0, at))
        self._beam(painter, marks, QColor.fromHsvF(
            (hue + 0.10) % 1.0, 0.55, 1.0,
            min(1.0, 0.20 + kick * 0.45 + flash * 0.3)))

    def _edges(self, painter, rect, horizon, focal, hue, kit, flash) -> None:
        """The rails either side: the left answers the snare, the right the
        hats.
        """
        reach = self.FAR - self._near
        offset = self._at % 1.0
        snare = kit.get("Snare", 0.0)
        hats = kit.get("Hats", 0.0)
        for side, lit in ((-1.0, snare), (1.0, hats)):
            rail = QPainterPath()
            edge = self.LANE_WIDE * self.LANES / 2.0
            first = None
            for step in range(self.RUNGS + 1):
                at = self._near + (step + offset) * reach / self.RUNGS
                point = self._eye(horizon, focal, side * edge,
                                  -0.10 - lit * 0.45, at)
                if first is None:
                    rail.moveTo(point)
                    first = point
                else:
                    rail.lineTo(point)
            self._beam(painter, rail, QColor.fromHsvF(
                (hue + (0.42 if side < 0 else 0.16)) % 1.0,
                max(0.0, 0.85 - flash * 0.4), 1.0,
                min(1.0, 0.35 + lit * 0.6 + flash * 0.3)))

    #: The colour of each kind of block, as a turn from the road's hue.
    BLOCK_HUE = {"wall": 0.42, "block": 0.30, "run": 0.16}
    #: How far a block's reflection reaches into the road.
    MIRROR = 0.55

    def _walls(self, painter, rect, horizon, focal, hue, flash) -> None:
        """The blocks, filled, with a lit edge."""
        edge = self.LANE_WIDE * 0.5 * 0.82
        tall = 0.62
        painter.setPen(Qt.PenStyle.NoPen)
        # Greys first and colours over them.
        for grey_now in (True, False):
            for kind in ("wall", "block", "run"):
                self._blocks_of(painter, rect, horizon, focal, hue, flash,
                                kind, grey_now, edge, tall)
        # And coins over both.
        self._coins_now(painter, horizon, focal, flash)

    #: How wide a coin is, how high it floats and how fast it turns: small, so
    #: it never hides the obstacle beside it.
    COIN_SIZE = 0.26
    COIN_TALL = 0.40
    COIN_TURN = 2.6
    #: How much bigger a power block is than a coin.
    POWER_SIZE = 1.9

    def _coins_now(self, painter, horizon, focal, flash) -> None:
        """The coins, as white discs standing on the road and turning: the one
        thing with no hue, so they read against any colour of road.
        """
        for when, lane, kind, _done, _grey in self._blocks:
            if kind not in ("coin", "power"):
                continue
            power = kind == "power"
            at = self._where(when)
            if at < self.GONE or at > self.FAR:
                continue
            near = max(0.0, min(1.0, 1.0 - (at - self._near)
                                / max(1e-6, self.FAR - self._near)))
            seen = self.FOG_LEAST + (1.0 - self.FOG_LEAST) * min(
                1.0, near / max(1e-6, self.FOG))
            across = self._lane_at(lane)
            # Each turned a little further than the one before, so a trail
            # reads as one thing rolling.
            phase = self._coin_spin + when * 5.0
            size = self.COIN_SIZE * (self.POWER_SIZE if power else 1.0)
            tall = self.COIN_TALL * (1.25 if power else 1.0)
            # Never fully edge-on.
            wide = size * max(0.28, abs(math.cos(phase)))
            # A true ellipse: everything in a coin is at one distance, where
            # the view is a straight scale, turn and shift of the road, so a
            # disc lands on the glass as an ellipse and three points fix it.
            middle = self._eye(horizon, focal, across, -tall, at)
            side = self._eye(horizon, focal, across + wide, -tall, at)
            top = self._eye(horizon, focal, across, -tall - size, at)
            glass = QTransform(side.x() - middle.x(), side.y() - middle.y(),
                               top.x() - middle.x(), top.y() - middle.y(),
                               middle.x(), middle.y())
            disc = QPainterPath()
            disc.addEllipse(QPointF(0.0, 0.0), 1.0, 1.0)
            face = glass.map(disc)
            back = (QTransform.fromScale(self.BACKING, self.BACKING)
                    * glass).map(disc)
            # The same dark silhouette every block gets, so a coin is read
            # against that rather than whatever the music has put behind it.
            painter.fillPath(back, QColor(3, 2, 8, int(215 * seen)))
            painter.fillPath(face, QColor.fromHsvF(
                0.13, (0.02 if power else 0.20) - flash * 0.1, 1.0,
                min(1.0, 0.95 * seen)))
            self._beam(painter, face, QColor.fromHsvF(
                0.12, 0.25 if power else 0.55, 1.0,
                min(1.0, 0.6 + 0.4 * seen)))

    #: What a grey obstacle and a coloured prize are made of, which must be
    #: told apart at the far end of the road: a grey has no hue and no light in
    #: it, a prize is the road's own colour at full strength. BACKING is how
    #: much wider than the block its dark backing is drawn.
    BACKING = 1.16

    GREY_SAT = 0.10
    GREY_LIT = 0.42
    PRIZE_SAT = 0.95
    PRIZE_LIT = 1.00

    def _blocks_of(self, painter, rect, horizon, focal, hue, flash,
                   kind, grey_now, edge, tall) -> None:
        """One shape of one kind, as one batch of paths."""
        if grey_now:
            shade = (hue + self.BLOCK_HUE[kind]) % 1.0
            wet = max(0.0, self.GREY_SAT - flash * 0.08)
            lit = self.GREY_LIT + flash * 0.30
        else:
            shade = hue
            wet = max(0.0, self.PRIZE_SAT - flash * 0.4)
            lit = self.PRIZE_LIT
        # A shade bigger than the block, for the dark it is drawn on.
        wider = edge * self.BACKING
        taller = tall * self.BACKING
        faces = QPainterPath()
        rims = QPainterPath()
        backs = QPainterPath()
        edges = QPainterPath()
        for when, lane, shape, _done, grey in self._blocks:
            if shape != kind or grey is not grey_now:
                continue
            at = self._where(when)
            # Gone once it is behind the rider; clamped to NEAR instead,
            # everything already passed piled up at the bottom of the frame.
            if at < self.GONE or at > self.FAR:
                continue
            # Out of the fog with the road, rather than whole at the far end of
            # it.
            near = max(0.0, min(1.0, 1.0 - (at - self._near)
                                / max(1e-6, self.FAR - self._near)))
            seen = self.FOG_LEAST + (1.0 - self.FOG_LEAST) * min(
                1.0, near / max(1e-6, self.FOG))
            across = self._lane_at(lane)
            foot_l = self._eye(horizon, focal, across - edge, 0.0, at)
            foot_r = self._eye(horizon, focal, across + edge, 0.0, at)
            top_r = self._eye(horizon, focal, across + edge, -tall, at)
            top_l = self._eye(horizon, focal, across - edge, -tall, at)
            faces.moveTo(foot_l)
            faces.lineTo(foot_r)
            faces.lineTo(top_r)
            faces.lineTo(top_l)
            faces.closeSubpath()
            backs.moveTo(self._eye(horizon, focal, across - wider, 0.0, at))
            backs.lineTo(self._eye(horizon, focal, across + wider, 0.0, at))
            backs.lineTo(self._eye(horizon, focal, across + wider,
                                   -taller, at))
            backs.lineTo(self._eye(horizon, focal, across - wider,
                                   -taller, at))
            backs.closeSubpath()
            rims.moveTo(top_l)
            rims.lineTo(top_r)
            edges.moveTo(foot_l)
            edges.lineTo(foot_r)
            edges.lineTo(top_r)
            edges.lineTo(top_l)
            edges.lineTo(foot_l)
            # The block's reflection in the road under it, squashed and dim:
            # one more quad a block, and most of what makes blocks stand on the
            # road rather than hover. Every drawn block gets one; dropping
            # distant ones saved 0.2 ms of 8.9 and lost it at the distances a
            # player reads.
            pool = QPainterPath()
            pool.moveTo(foot_l)
            pool.lineTo(foot_r)
            pool.lineTo(self._eye(horizon, focal, across + edge,
                                  tall * self.MIRROR, at))
            pool.lineTo(self._eye(horizon, focal, across - edge,
                                  tall * self.MIRROR, at))
            pool.closeSubpath()
            # A dark silhouette under it first, so how well a block reads does
            # not depend on what is behind it.
            painter.fillPath(backs, QColor(3, 2, 8, int(225 * seen)))
            painter.fillPath(pool, QColor.fromHsvF(
                shade, wet, lit * 0.62, 0.30 * seen))
            # One path per block, since each is a different distance into the
            # fog.
            painter.fillPath(faces, QColor.fromHsvF(
                shade, wet, lit, 0.90 * seen))
            self._beam(painter, rims, QColor.fromHsvF(
                shade, max(0.0, wet - 0.45), 1.0,
                min(1.0, (0.85 + flash * 0.15) * seen)))
            # And an edge all the way round, at full strength at any distance:
            # far away, a block's face is a few pixels the lamp has washed out,
            # and the edge is what says it is there.
            self._beam(painter, edges, QColor.fromHsvF(
                shade, max(0.0, wet - 0.55), 1.0,
                min(1.0, 0.55 + 0.45 * seen)))
            faces = QPainterPath()
            rims = QPainterPath()
            backs = QPainterPath()
            edges = QPainterPath()

    #: The horizon lamp: how far it reaches as a share of the frame, and how
    #: much the bass opens it. Kept small, since it sits where a block is read
    #: while there is still time to move; reaching half the frame, it washed
    #: those blocks out.
    GLOW_REACH = 0.30
    GLOW_BASS = 0.10
    #: The most of the frame the lamp may take.
    GLOW_MOST = 0.26

    #: How the screen answers a run: a ring spreading from the craft, a flash
    #: of colour from the frame's edge, and for moments that deserve one, a
    #: callout. Drawn in screen space, outside the bank and the shake, and gone
    #: inside half a second, at the edges or around the craft, never where a
    #: block is read.
    #:
    #: Per kind: how long it lives, how far a ring spreads as a share of the
    #: frame, how strong the edge flash is, how bright the ring, and how thick.
    #: A hit has the heaviest ring and a red flash of its own: the damage wash
    #: multiplies, so on a dark road it barely shows.
    POPS = {
        "coin":      (0.40, 0.30, 0.30, 0.85, 1.0),
        "prize":     (0.38, 0.24, 0.18, 0.60, 0.8),
        "power":     (0.70, 0.75, 0.75, 1.00, 1.6),
        "shatter":   (0.45, 0.40, 0.35, 0.90, 1.2),
        "air":       (0.50, 0.45, 0.40, 0.90, 1.2),
        "clear":     (0.55, 0.50, 0.45, 0.95, 1.4),
        "hit":       (0.55, 0.60, 0.65, 1.00, 2.4),
        "milestone": (0.95, 0.95, 0.80, 1.00, 1.8),
        "finish":    (2.20, 1.10, 0.60, 1.00, 2.0),
    }
    #: Hits, or prizes, this close together are one run of them, and each
    #: after the first lands harder by COMBO_LIFT.
    COMBO_GAP = 1.0
    COMBO_LIFT = 0.25

    def _combo(self, which: str, when: float) -> int:
        """How many ``which`` in a row, counting this one at ``when``."""
        last, count = self._combos.get(which, (None, 0))
        if last is not None and 0.0 <= when - last <= self.COMBO_GAP:
            count += 1
        else:
            count = 1
        self._combos[which] = (when, count)
        return count

    #: Chains worth a callout. Past a hundred the chain pays its cap, so they
    #: thin out.
    MILESTONES = (10, 25, 50, 75, 100, 150, 200, 300, 500)
    #: A chain at least this long is worth telling somebody they lost.
    LOST_WORTH = 10

    def _pop(self, kind: str, hue: float = None, sat: float = 0.85,
             strength: float = 1.0, text: str = "") -> None:
        """Answer something the player did. See POPS."""
        if hue is None:
            hue = self._hue_now
        self._pops.append([kind, 0.0, max(0.0, min(2.0, strength)),
                           hue % 1.0, sat, text])
        # Never a queue of them: the newest few are all anybody sees.
        if len(self._pops) > 12:
            # The oldest go, but never the finish: the results card comes up
            # from it, and a burst of pickups on the last beat would otherwise
            # push it out.
            kept = [pop for pop in self._pops if pop[0] == "finish"]
            rest = [pop for pop in self._pops if pop[0] != "finish"]
            self._pops = kept + rest[len(rest) - (12 - len(kept)):]

    def _age_pops(self, step: float) -> None:
        """On the track's clock, like everything else."""
        if not self._pops or step <= 0.0:
            return
        for pop in self._pops:
            pop[1] += step
        self._pops = [pop for pop in self._pops
                      if pop[1] < self.POPS[pop[0]][0]]

    def _milestone(self, before: int, after: int) -> None:
        """A callout, if the chain has just crossed one of MILESTONES."""
        for mark in self.MILESTONES:
            if before < mark <= after:
                self._pop("milestone", sat=0.55,
                          strength=1.0 + min(0.5, mark / 400.0),
                          text=f"CHAIN {mark}")
                return

    def _pops_now(self, painter, rect) -> None:
        """Every answer still on screen, over the top of the picture."""
        if not self._pops:
            return
        span = min(rect.width(), rect.height())
        origin = self._craft_glass
        if origin is None:
            origin = QPointF(rect.center().x(), rect.bottom() - span * 0.2)
        painter.save()
        try:
            flash = self._pop_rings(painter, rect, span, origin)
            if flash is not None and flash[0] > 0.01:
                self._pop_flash(painter, rect, *flash)
        finally:
            # Always restored, after the flash as well as the rings: an
            # unbalanced save takes the pane down at the end of the frame, and
            # a leftover pen and brush would draw whatever comes next.
            painter.restore()

    def _pop_rings(self, painter, rect, span, origin):
        """The rings and the words; the strongest edge flash, returned."""
        painter.setBrush(Qt.BrushStyle.NoBrush)
        flash = None
        for kind, age, strength, hue, sat, text in self._pops:
            life, spread, edge, bright, thick = self.POPS[kind]
            through = min(1.0, age / life)
            fade = (1.0 - through) ** 2
            # One edge flash for all of them, the strongest, rather than a
            # gradient across the frame for each.
            if edge > 0.0:
                amount = edge * strength * fade
                if flash is None or amount > flash[0]:
                    flash = (amount, hue, sat)
            # The ring: fast out and slowing, so it reads as thrown off the
            # craft rather than drawn round it.
            if spread > 0.0 and bright > 0.0:
                out = 1.0 - (1.0 - through) ** 3
                radius = max(1.0, span * spread * strength * out)
                width = max(1.0, span * 0.018 * (1.0 - through) * strength
                            * thick)
                pen = QPen(QColor.fromHsvF(
                    hue, sat if kind != "hit" else 0.95, 1.0,
                    min(1.0, bright * fade)), width)
                painter.setPen(pen)
                painter.drawEllipse(origin, radius, radius * 0.62)
            if text:
                self._callout(painter, rect, text, hue, through, strength)
        return flash

    def _pop_flash(self, painter, rect, amount, hue, sat) -> None:
        """One edge flash, in the colour of whatever earned it."""
        centre = rect.center()
        reach = max(1.0, math.hypot(rect.width(), rect.height()) / 2.0)
        glow = QRadialGradient(centre, reach)
        glow.setColorAt(0.45, QColor(0, 0, 0, 0))
        glow.setColorAt(1.0, QColor.fromHsvF(hue, sat, 1.0,
                                             min(1.0, amount)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(glow)
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _callout(self, painter, rect, text, hue, through, strength) -> None:
        """A word across the upper middle of the frame, for a moment: full size
        in the first tenth and faded over the last half, above the road's
        far end where nothing is read.
        """
        grow = min(1.0, through / 0.10)
        size = max(10.0, rect.height() * 0.075 * (0.7 + 0.3 * grow)
                   * min(1.3, strength))
        fade = 1.0 if through < 0.5 else max(0.0, 1.0 - (through - 0.5) / 0.5)
        font = QFont(painter.font())
        font.setPointSizeF(size)
        font.setBold(True)
        painter.setFont(font)
        box = QRectF(rect.left(), rect.top() + rect.height() * 0.18,
                     rect.width(), size * 2.0)
        # A dark stroke under the word, so it reads over anything behind it.
        painter.setPen(QColor(0, 0, 0, int(200 * fade)))
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            painter.drawText(box.translated(dx, dy),
                             int(Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignTop), text)
        painter.setPen(QColor.fromHsvF(hue, 0.45, 1.0, fade))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop), text)

    #: How hard the frame's edge lights on the beat, and how far in it reaches.
    #: The road is kept dark so blocks read against it, so the beat hits the
    #: edge instead: a rim of the road's colour on the kick, transparent well
    #: before the middle.
    RIM_MOST = 0.45
    RIM_REACH = 0.58

    def _rim(self, painter, rect, hue, punch) -> None:
        """The frame's edge, lit on the beat. Outside the bank and the shake:
        it belongs to the picture, not the road.
        """
        if punch <= 0.02:
            return
        centre = rect.center()
        reach = max(1.0, math.hypot(rect.width(), rect.height()) / 2.0)
        rim = QRadialGradient(centre, reach)
        rim.setColorAt(self.RIM_REACH, QColor(0, 0, 0, 0))
        rim.setColorAt(1.0, QColor.fromHsvF(
            (hue + 0.04) % 1.0, 0.80, 1.0,
            min(1.0, self.RIM_MOST * punch)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(rim)
        # One fill, not a ring of four: four gradient bands cost more than one
        # full-frame fill, because the per-call setup dominates.
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def _ring_fill(painter, rect, middle, reach: float) -> None:
        """Fill only the part of ``rect`` a radial gradient can reach.

        A radial brush costs its whole rectangle even where it is
        transparent: at 1920x1080 the two gradients here were 3 ms of a 9.6
        ms frame. Clipping to the circle's bounding box draws the same
        picture. Skipping a transparent middle the same way is slower, since
        per-call setup dominates.
        """
        box = QRectF(middle.x() - reach, middle.y() - reach,
                     reach * 2.0, reach * 2.0).intersected(rect)
        if box.isEmpty():
            return
        painter.drawRect(box)

    def _flat_tunnel(self, painter, rect, horizon, focal, hue, beat,
                     flash) -> None:
        """A corkscrew's tunnel drawn flat: every colour wheeling round the end
        of the road and a ring round the road at every beat, as the world on
        the card draws it. See _tunnel_at."""
        if not self._twists:
            return
        inside = self._tunnel_at(self._heard)
        middle = self._eye(horizon, focal, 0.0, self.TUNNEL_MIDDLE,
                           self.FAR * 0.9)
        if inside > 0.01:
            from PySide6.QtGui import QConicalGradient

            wheel = QConicalGradient(middle, (self._bend * 60.0) % 360.0)
            for step in range(7):
                wheel.setColorAt(step / 6.0, QColor.fromHsvF(
                    (hue + step / 6.0 + self._climb * 0.05) % 1.0, 0.95,
                    0.55 + beat * 0.25 + flash * 0.2))
            painter.save()
            painter.setOpacity(painter.opacity() * inside * 0.85)
            # Past the frame's corners every way: the picture turns over
            # through a corkscrew, and a frame-sized fill left the corners
            # black.
            wide = math.hypot(rect.width(), rect.height())
            painter.fillRect(rect.adjusted(-wide, -wide, wide, wide),
                             QBrush(wheel))
            painter.restore()
        if self._beat <= 0.0:
            return
        radius = self.LANE_WIDE * self.LANES / 2.0 + self.TUNNEL_ROOM
        first = self._beat_at_road(self._at + self._near - self.RIDER_AT)
        for number in range(first, first + 16):
            at = self.RIDER_AT + self.beat_on_road(number) - self._at
            if at > self.FAR:
                break
            if not self._near < at:
                continue
            here = self._tunnel_at(self._when(at))
            if here <= 0.01:
                continue
            centre = self._eye(horizon, focal, 0.0, self.TUNNEL_MIDDLE, at)
            edge = self._eye(horizon, focal, radius, self.TUNNEL_MIDDLE, at)
            reach = math.hypot(edge.x() - centre.x(), edge.y() - centre.y())
            near = 1.0 - (at - self._near) / max(1e-6, self.FAR - self._near)
            pen = QPen(QColor.fromHsvF(
                (hue + 0.5 + number * 0.13) % 1.0, 0.6, 1.0,
                min(1.0, here * (0.35 + near * 0.65))),
                max(1.0, reach * 0.05))
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(centre, reach, reach)

    def _glow(self, painter, rect, horizon, hue, surge, bass, beat,
              flash) -> None:
        """A lamp at the end of the road, behind everything: the cheapest depth
        in the scene, one gradient a frame.
        """
        reach = max(1.0, rect.height() * (self.GLOW_REACH
                                          + bass * self.GLOW_BASS))
        # The area to fill comes from the widest the lamp can ever be, which is
        # constant for a frame size. The bass never settles exactly, so an area
        # measured from it creeps, and its edge shows as one pixel changing
        # under a stopped track.
        widest = max(1.0, rect.height() * (self.GLOW_REACH
                                           + self.GLOW_BASS))
        lamp = QRadialGradient(horizon, reach)
        lamp.setColorAt(0.0, QColor.fromHsvF(
            (hue + 0.08) % 1.0, max(0.0, 0.70 - flash * 0.4), 1.0,
            min(self.GLOW_MOST,
                0.14 + surge * 0.14 + beat * 0.09 + flash * 0.12)))
        lamp.setColorAt(0.45, QColor.fromHsvF(
            (hue + 0.02) % 1.0, 0.85, 0.8,
            min(self.GLOW_MOST, 0.06 + surge * 0.08 + beat * 0.05)))
        lamp.setColorAt(1.0, QColor(0, 0, 0, 0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(lamp)
        # Only where the lamp can reach. See _ring_fill.
        self._ring_fill(painter, rect, horizon, widest)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _lanes(self, painter, horizon, focal, hue, beat, flash) -> None:
        """The lines between the lanes, brighter than the chevrons and dashed
        down the road, so they read as lane markings.
        """
        reach = self.FAR - self._near
        offset = self._at % 2.0
        lines = QPainterPath()
        for lane in range(1, self.LANES):
            across = (-self.LANE_WIDE * self.LANES / 2.0
                      + lane * self.LANE_WIDE)
            step = 0
            while step < self.RUNGS:
                at = self._near + (step + offset) * reach / self.RUNGS
                on = self._near + (step + 1.4 + offset) * reach / self.RUNGS
                if at > self.FAR:
                    break
                lines.moveTo(self._eye(horizon, focal, across, 0.0, at))
                lines.lineTo(self._eye(horizon, focal, across, 0.0,
                                       min(self.FAR, on)))
                step += 3
        self._beam(painter, lines, QColor.fromHsvF(
            (hue + 0.06) % 1.0, max(0.0, 0.30 - flash * 0.25), 1.0,
            min(1.0, 0.55 + beat * 0.30 + flash * 0.25)))

    #: How far apart the pillars are down the road, and how tall.
    PILLAR_EVERY = 5.0
    PILLAR_TALL = 1.45

    def _pillars(self, painter, horizon, focal, hue, kit, beat,
                 flash) -> None:
        """Gates down either side, passing at the road's own speed: they stand
        on the kick and light on the snare.
        """
        edge = self.LANE_WIDE * self.LANES / 2.0 + 0.45
        tall = self.PILLAR_TALL * (1.0 + kit.get("Kick", 0.0) * 0.35)
        posts = QPainterPath()
        first = math.ceil((self._near + self._at) / self.PILLAR_EVERY)
        for index in range(first, first + int(self.FAR / self.PILLAR_EVERY) + 2):
            at = index * self.PILLAR_EVERY - self._at
            if not self._near + 0.3 <= at <= self.FAR:
                continue
            for side in (-1.0, 1.0):
                foot = self._eye(horizon, focal, side * edge, 0.0, at)
                head = self._eye(horizon, focal, side * edge, -tall, at)
                posts.moveTo(foot)
                posts.lineTo(head)
                # A short arm turning in over the road, so a pillar reads as a
                # gate.
                posts.lineTo(self._eye(horizon, focal,
                                       side * (edge - 0.55), -tall, at))
        self._beam(painter, posts, QColor.fromHsvF(
            (hue + 0.30) % 1.0, max(0.0, 0.75 - flash * 0.4), 1.0,
            min(1.0, 0.35 + kit.get("Snare", 0.0) * 0.45 + beat * 0.20
                + flash * 0.3)))

    def _bits(self, painter, horizon, focal, hue) -> None:
        """The pieces thrown off a block that was hit."""
        if not self._sparks:
            return
        shards = QPainterPath()
        for across, up, at, _dx, _dy, _dz, life in self._sparks:
            if at <= self._near + 0.05:
                continue
            here = self._eye(horizon, focal, across, up, at)
            back = self._eye(horizon, focal, across, up + 0.10 * life,
                             at + 0.14)
            shards.moveTo(here)
            shards.lineTo(back)
        self._beam(painter, shards, QColor.fromHsvF(
            (hue + 0.5) % 1.0, 0.25, 1.0, 0.85))

    #: How long a run has to get before the craft is as hot as it gets, and how
    #: much of a lift that is worth. Forty, because there the chain pays near
    #: its cap and a grey starts costing the run rather than points.
    CHAIN_HOT = 40.0
    HEAT_HALO = 1.7
    HEAT_HUE = 0.10

    def _heat(self) -> float:
        """How far into a run the craft is, from nothing to all of it."""
        if self._mode == "Puzzle":
            # The grid's game keeps no chain: what it builds is the cluster in
            # the columns.
            return max(0.0, min(1.0, sum(len(pile) for pile in self._cells)
                                / max(1.0, self.CELLS_WIDE * self.CELLS_DEEP)))
        return max(0.0, min(1.0, self._chain / self.CHAIN_HOT))

    #: How far the craft rolls into a lane change, the most it rolls, and how
    #: quickly the roll follows the move. One lane peaks the swerve at 16 units
    #: a second and two at 32, so 1.0 banks a single change 16 degrees and
    #: keeps the ceiling for a dash across the road. The slide is nine tenths
    #: done in 50 ms; a third of the way each frame settles inside three, so
    #: the roll keeps up with it.
    SWERVE_BANK = 1.0
    SWERVE_MOST = 26.0
    SWERVE_EASE = 0.34

    #: How big the craft's own halo is on a kick, and how strong.
    HALO_REACH = 0.085
    HALO_MOST = 0.55

    #: The flat craft of each level, as how wide it is and how far its nose
    #: reaches against the first: a broad cruiser at Easy, a needle at
    #: Expert, as the world on the card flies them (rider_gl.CRAFT_FOR).
    FLAT_CRAFT = {"Easy": (1.25, 0.85), "Normal": (1.0, 1.0),
                  "Hard": (0.85, 1.15), "Expert": (0.62, 1.32)}

    def _ship(self, painter, rect, horizon, focal, hue, flash,
              punch=0.0) -> None:
        """The rider: a lit triangle, low on the road."""
        at = self.RIDER_AT
        across = self._lane_here
        broad, long = self.FLAT_CRAFT.get(self._difficulty, (1.0, 1.0))
        wide = self.LANE_WIDE * 0.42 * broad
        # Nose down the road and tail towards the camera, so it reads as a
        # craft leaning into the road; pointed the other way it read as an
        # arrow at the viewer. Heights grow downwards, so being off the road is
        # a height taken away.
        lift = self._air
        self._craft_spot = self._eye(horizon, focal, across, -0.14 - lift,
                                     at + 0.4)
        nose = self._eye(horizon, focal, across, -0.26 - lift,
                         at + 1.4 * long)
        left = self._eye(horizon, focal, across - wide, -0.02 - lift, at)
        right = self._eye(horizon, focal, across + wide, -0.02 - lift, at)
        # The craft answers the kick with a halo rather than a brighter fill,
        # which is already near the top of the scale. In the foreground and
        # below the road's far end, so it is never what a block is read
        # against.
        if punch > 0.02:
            spot = self._eye(horizon, focal, across, -0.14 - lift,
                             at + 0.4)
            # Bigger and hotter the longer the run is. See CHAIN_HOT.
            heat = self._heat()
            reach = max(2.0, rect.height() * self.HALO_REACH
                        * (0.75 + punch * 0.5)
                        * (1.0 + heat * (self.HEAT_HALO - 1.0)))
            # On whole pixels: the camera never settles exactly, so a gradient
            # centred at full precision rounds differently somewhere along its
            # ramp each frame, and a pixel changes under a stopped track.
            middle = QPointF(round(spot.x()), round(spot.y()))
            reach = float(round(reach))
            halo = QRadialGradient(middle, reach)
            halo.setColorAt(0.0, QColor.fromHsvF(
                (hue + 0.5 - heat * self.HEAT_HUE) % 1.0,
                0.45 + heat * 0.35, 1.0,
                min(1.0, self.HALO_MOST * punch * (1.0 + heat * 0.6))))
            halo.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setBrush(halo)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(middle, reach, reach)
            painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        path.moveTo(nose)
        path.lineTo(left)
        path.lineTo(right)
        path.closeSubpath()
        # Banked into the move around the craft's own middle, so the nose comes
        # up on the side it is heading for. Everything to the end of the craft
        # is drawn inside it, the bumpers included.
        bank = max(-self.SWERVE_MOST,
                   min(self.SWERVE_MOST, -self._swerve * self.SWERVE_BANK))
        rolled = abs(bank) > 0.05
        if rolled:
            painter.save()
            painter.translate(left.x() + (right.x() - left.x()) * 0.5,
                              left.y() + (right.y() - left.y()) * 0.5)
            painter.rotate(bank)
            painter.translate(-(left.x() + (right.x() - left.x()) * 0.5),
                              -(left.y() + (right.y() - left.y()) * 0.5))
        hurt = self._sore > 0.0 and int(self._sore * 14) % 2 == 0
        shade = 0.0 if hurt else (hue + 0.5) % 1.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(path, QColor.fromHsvF(
            shade, 0.85 if hurt else max(0.0, 0.40 - flash * 0.3),
            1.0, 0.85))
        stroke(painter, path, QColor.fromHsvF(shade, 0.2, 1.0, 1.0),
               2.0 * max(0.75, min(1.3, rect.height() / 700.0)))
        # The bumpers, when up: two short bars either side of the craft, faint
        # while they are coming back.
        if self._shield > 0.01:
            ready = self._shield >= 1.0
            guard = QPainterPath()
            for side in (-1.0, 1.0):
                out = across + side * wide * 1.55
                guard.moveTo(self._eye(horizon, focal, out, -0.02, at - 0.3))
                guard.lineTo(self._eye(horizon, focal, out, -0.30,
                                       at + 0.55))
            stroke(painter, guard, QColor.fromHsvF(
                (hue + 0.34) % 1.0, 0.25 if ready else 0.75, 1.0,
                (0.95 if ready else 0.30 + 0.25 * self._shield)),
                (2.4 if ready else 1.2)
                * max(0.75, min(1.3, rect.height() / 700.0)))
        if rolled:
            painter.restore()

    def _card(self, painter, rect, hue) -> None:
        painter.save()
        font = painter.font()
        font.setPointSizeF(max(9.0, rect.height() * 0.022))
        painter.setFont(font)
        painter.setPen(QColor.fromHsvF((hue + 0.5) % 1.0, 0.25, 1.0, 0.75))
        painter.drawText(
            rect.adjusted(14, 10, -14, 0),
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop),
            f"{self._score}"
            + (f"   cleared {self._cleared}" if self._mode == "Puzzle"
               else f"   chain {self._chain}")
            + (f"   air {self._airs}" if self._airs else "")
            + ("   x2" if self._double > 1.0 else "")
            + (f"   coins {self._coins}" if self._coins else "")
            + (f" x{self._coin_run}" if self._coin_run > 1 else "")
            + (f"   clean +{self.bonus():.0%}"
               if self._clean and self._score else "")
            + ("" if self._shield >= 1.0 else "   shield "
               + f"{self._shield:.0%}")
            + f"   best {self._best}")
        painter.restore()

    #: How long the ride takes to draw itself across the strip at the end.
    PLAYBACK = 1.2

    def _results(self, painter, rect) -> None:
        """The end of the track: how the run went, over the stopped road.

        The score and what made it, then the ride along the track's shape,
        each block taken in the road's colour at the time and each miss and
        hit, drawn across in a moment. Stays until the track is played again
        from the start.
        """
        if not self._finished or self._result is None:
            return
        spec = self.POPS["finish"]
        age = next((pop[1] for pop in self._pops if pop[0] == "finish"),
                   spec[0])
        result = self._result
        tall, wide = rect.height(), rect.width()
        left = rect.left() + wide * 0.07
        right = rect.right() - wide * 0.07
        plain = QColor(255, 255, 255, 235)
        dim = QColor(255, 255, 255, 135)
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * min(1.0, age / 0.5))
            painter.fillRect(rect, QColor(5, 4, 12, 165))

            def font_of(size, weight):
                font = QFont(painter.font())
                font.setPointSizeF(max(7.0, size))
                font.setWeight(weight)
                return font

            def say(text, x, y, font, colour):
                painter.setFont(font)
                painter.setPen(colour)
                painter.drawText(QPointF(x, y), text)
                return QFontMetricsF(font).horizontalAdvance(text)

            y = rect.top() + tall * 0.27
            small = font_of(tall * 0.026, QFont.Weight.Medium)
            say(" · ".join([result["mode"], result.get("difficulty") or "",
                            f"grade {result['grade']}"]).replace(" ·  · ",
                                                                  " · "),
                left, y, small, dim)
            y += tall * 0.14
            say(f"{result['worth']:,}", left - tall * 0.006, y,
                font_of(tall * 0.12, QFont.Weight.Light), plain)
            if not result.get("whole", True):
                best = "skipped through, so no best is kept"
            elif self.new_best:
                best = "a new best"
            elif self.best_before:
                best = f"best {self.best_before:,}"
            else:
                best = ""
            y += tall * 0.065
            if best:
                say(best, left, y, font_of(tall * 0.03, QFont.Weight.Medium),
                    QColor.fromHsvF(self._hue_now % 1.0, 0.45, 1.0)
                    if self.new_best and result.get("whole", True) else dim)
            # What it was made of, as words: a figure in white, what it
            # counts in grey.
            parts = []
            if result["mode"] == "Puzzle":
                parts.append(((f"{result['cleared']}", True),
                              (" cleared", False)))
            if result["offered"]:
                parts.append(((f"{result['taken']}", True),
                              (f" of {result['offered']} taken", False)))
            if result["mode"] != "Puzzle":
                parts.append((("longest chain ", False),
                              (f"{result['chain']}", True)))
            parts.append(((f"{result['hits']}", True),
                          (" hit" if result["hits"] == 1 else " hits", False))
                         if result["hits"] else (("no hits", False),))
            if result["saves"]:
                parts.append(((f"{result['saves']}", True),
                              (" saved by the shield", False)))
            if result["coins"]:
                parts.append(((f"{result['coins']}", True),
                              (" coins", False)))
            if result["airs"]:
                parts.append(((f"{result['airs']}", True),
                              (" jumps", False)))
            if result["clean"] and result["score"]:
                parts.append((("clean finish ", False),
                               (f"+{self.bonus(result['mode']):.0%}", True),
                               (" on ", False),
                               (f"{result['score']:,}", True)))
            figures = font_of(tall * 0.028, QFont.Weight.Normal)
            metrics = QFontMetricsF(figures)
            gap = metrics.horizontalAdvance("    ")
            y += tall * 0.075
            x = left
            for part in parts:
                width = sum(metrics.horizontalAdvance(text) for text, _ in part)
                if x > left and x + width > right:
                    x = left
                    y += metrics.height() * 1.3
                for text, figure in part:
                    x += say(text, x, y, figures, plain if figure else dim)
                x += gap
            self._ride_strip(painter, rect, left, right,
                             min(1.0, age / self.PLAYBACK), dim)
        finally:
            painter.restore()

    def _ride_strip(self, painter, rect, left, right, through, dim) -> None:
        """The whole ride along the track's loudness: taken above the
        line, hits and saves below it, the misses faint on it. Drawn as
        far as ``through`` of the way across."""
        loud, rate, length = self._ridden or ((), 0.0, 0.0)
        if length <= 0.0 or right <= left:
            return
        tall = rect.height()
        top = rect.top() + tall * 0.68
        band = tall * 0.16
        middle = top + band * 0.5
        span = right - left
        # Eased, so it slows into the end of the track.
        through = through * through * (3.0 - 2.0 * through)
        upto = length * through
        reached = left + span * through
        if loud and rate > 0.0:
            columns = max(8, int(span / 3.0))
            most = max(loud) or 1.0
            shape = QPainterPath()
            shape.moveTo(left, middle)
            bottom = []
            for index in range(columns + 1):
                share = index / columns
                if share > through:
                    break
                low = int(share * len(loud))
                high = max(low + 1, int((index + 1) / columns * len(loud)))
                level = max(loud[low:high] or (0.0,)) / most
                x = left + span * share
                shape.lineTo(x, middle - level * band * 0.3)
                bottom.append((x, middle + level * band * 0.3))
            for x, y in reversed(bottom):
                shape.lineTo(x, y)
            shape.closeSubpath()
            painter.fillPath(shape, QColor(255, 255, 255, 24))
        painter.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
        painter.drawLine(QPointF(left, middle), QPointF(reached, middle))
        thin = max(1.0, tall / 500.0)
        for when, kind, hue in self._log:
            if when > upto:
                continue
            x = left + span * max(0.0, min(1.0, when / length))
            if kind == "taken":
                # Pale, so a block taken where the road ran red is not
                # mistaken for a hit.
                painter.setPen(QPen(QColor.fromHsvF(hue % 1.0, 0.4, 1.0, 0.95),
                                    thin * 1.4))
                painter.drawLine(QPointF(x, middle),
                                 QPointF(x, middle - band * 0.42))
            elif kind == "coin":
                painter.setPen(QPen(QColor(255, 214, 110, 210), thin * 2.2,
                                    Qt.PenStyle.SolidLine,
                                    Qt.PenCapStyle.RoundCap))
                painter.drawPoint(QPointF(x, middle - band * 0.5))
            elif kind == "missed":
                painter.setPen(QPen(QColor(255, 255, 255, 60), thin))
                painter.drawLine(QPointF(x, middle),
                                 QPointF(x, middle - band * 0.12))
            elif kind == "hit":
                painter.setPen(QPen(QColor(255, 72, 72, 235), thin * 1.8))
                painter.drawLine(QPointF(x, middle),
                                 QPointF(x, middle + band * 0.42))
            elif kind == "saved":
                painter.setPen(QPen(QColor(190, 235, 255, 220), thin * 1.8))
                painter.drawLine(QPointF(x, middle),
                                 QPointF(x, middle + band * 0.3))
        times = QFont(painter.font())
        times.setPointSizeF(max(7.0, tall * 0.022))
        painter.setFont(times)
        painter.setPen(dim)
        below = top + band + tall * 0.045
        painter.drawText(QPointF(left, below), "0:00")
        end = f"{int(length) // 60}:{int(length) % 60:02d}"
        painter.drawText(QPointF(right - QFontMetricsF(times)
                                 .horizontalAdvance(end), below), end)

    @staticmethod
    def _beam(painter, path, colour) -> None:
        """One pass, one pixel, as the rave's room is drawn."""
        pen = QPen(colour, 0.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)


SCENES = (Vaporwave(), Tunnel(), Oscilloscope(), Bars(), Meters(),
          Ambience(), Waterfall(), Rave(), Rider())


def by_name(name: str) -> Scene:
    for scene in SCENES:
        if scene.name == name:
            return scene
    return SCENES[0]


#: What each scene asks for after it has drawn itself. Kept here rather than
#: on the classes so the whole look of the set can be read at once.
#:
#: bloom     how much light bleeds out of bright areas
#: scanlines a CRT's horizontal lines, 0 to 1
#: vignette  how much the corners fall off
#: grain     film noise, which hides banding in the gradients
#: aberration how far the red and blue channels separate, in pixels
POST = {
    # A room of haze and beams: heavy bloom, a strong vignette and the fringing
    # of a wide lens. No scanlines.
    "Rave": {"bloom": 0.92, "vignette": 0.52, "aberration": 1.6,
             "grain": 0.04},
    # A CRT showing a sunset: bloom for the neon, scanlines and a little lens
    # error for the tube, grain to hide banding in the sky.
    "Vaporwave city": {"bloom": 0.60, "scanlines": 0.16, "vignette": 0.42,
                       "grain": 0.05, "aberration": 1.2},
    # Glass and neon, no tube: heavy bloom, a strong vignette for depth, and
    # fringing at the edges.
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
    # A game, so the picture has to stay readable: enough bloom for the
    # neon and a vignette to hold the eye on the road, and no grain or
    # scanlines over the thing being aimed at.
    "Music rider": {"bloom": 0.70, "vignette": 0.50, "aberration": 1.2},
    # An instrument, not a light show: enough bloom to lift the ridges off
    # the background and a vignette to keep the eye in the plot.
    "Waterfall": {"bloom": 0.50, "vignette": 0.38, "grain": 0.03},
}


def post_for(scene) -> dict:
    """The recipe for this scene, or nothing if it wants none."""
    return POST.get(getattr(scene, "name", ""), {})


#: What the strobe does in each scene, as (source, rate, sensitivity), used
#: when a scene is picked and the controls have not been set by hand. One
#: setting cannot suit them all: a fast strobe on the meters is a flickering
#: lamp, in the tunnel it is motion sickness, and a slow one leaves the rave
#: asleep. Touching either slider stops them being applied.
STROBE_SETUP = {
    # Hits the whole room: the eagerest of the set, still short of running
    # through held notes.
    "Rave": ("Kick", 0.58, 0.58),
    # Neon and glass: the flash is a lurch forward, so it wants to be
    # rare enough to read as an event.
    "Neon tunnel": ("Kick", 0.30, 0.45),
    # A skyline lighting up. On the beat rather than on every drum.
    "Vaporwave city": ("Bass", 0.45, 0.50),
    # An instrument. The needles are the subject and the flash is the
    # lamp behind them, so it stays out of the way.
    "VU meters": ("Kick", 0.22, 0.40),
    # The road is already on the beat, so the strobe is the room around
    # it rather than the beat itself.
    "Music rider": ("Kick", 0.40, 0.50),
    # The hats give the trace a shimmer without moving the picture.
    "Oscilloscope": ("Hats", 0.55, 0.55),
    # A graph. The flash brightens the bars and nothing moves.
    "Equaliser": ("Snare", 0.40, 0.50),
    # Overlapping washes: the synth is what it is made of.
    "Ambience": ("Synth", 0.50, 0.55),
    # A plot of the spectrum over time. Lit on the snare, which is the
    # thing that shows up across the whole width of it.
    "Waterfall": ("Snare", 0.35, 0.50),
}


def strobe_setup(scene) -> tuple:
    """(source, rate, sensitivity) for this scene, or None."""
    return STROBE_SETUP.get(getattr(scene, "name", ""))
