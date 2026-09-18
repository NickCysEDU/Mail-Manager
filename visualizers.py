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

import logging
import math
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QColor, QImage, QLinearGradient, QPainter,
                           QPainterPath,
                           QPen, QRadialGradient)


log = logging.getLogger(__name__)

#: The face the meter dials are lettered in, and where it comes from.
#:
#: It ships with the app rather than being asked for by name, and that is
#: the whole point of it. The dials are drawn from a photograph of a real
#: meter whose numbers are set in a square, Eurostile-like face, and the
#: code used to ask for one with a fallback list - Eurostile, Microgramma,
#: Square721, Bank Gothic, then Verdana and DejaVu.
#:
#: None of the first four ship with macOS or with a build runner. Measured
#: over the 181 families installed here, *nothing* installed has square
#: digits. So Qt silently took the first name it recognised, which was
#: Verdana: a humanist sans, round where the reference is square, and a
#: different face again on Linux. That is why "the font does not match at
#: all", and why a fallback list was never going to fix it.
#:
#: Michroma is a square techno face under the SIL Open Font License, which
#: is what the licence is for. It is loaded once, by file, so that every
#: machine draws the same dial.
FONT_FILE = "Michroma-Regular.ttf"
FONT_FAMILY = "Michroma"

_LOADED: Optional[str] = None


def dial_face() -> Optional[str]:
    """The family the dials are lettered in, or None if it did not load.

    Loaded once and remembered. A failure is not fatal - the dials fall
    back to whatever Qt finds, which is what they did before - but it is
    logged, because a face silently swapped for another is exactly the
    thing this was written to stop.
    """
    global _LOADED
    if _LOADED is not None:
        return _LOADED or None
    try:
        from PySide6.QtGui import QGuiApplication

        if QGuiApplication.instance() is None:
            # Qt cannot register a font before there is an application,
            # and the answer would be remembered for the life of the
            # process. Ask again later rather than deciding now.
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


#: The width, in real screen pixels, above which Qt stops being quick.
#:
#: Qt's raster engine has a dedicated path for one-pixel lines and nothing
#: comparable above it. Measured on the Waterfall's ridges at 1080p - about
#: a thousand antialiased curve segments - one frame took:
#:
#:      pen width 0.9   1.62 ms        pen width 1.01  58.00 ms
#:      pen width 1.0   2.21 ms        pen width 2.0   66.35 ms
#:
#: A thirty-six fold cliff at exactly one pixel. This is the whole reason
#: the scenes used to be cheap: the old fixed resolution budget kept the
#: buffer at about a quarter of the screen's pixels, which put a 2.4 unit
#: pen at 1.3 *real* pixels, under the cliff. Drawing at the resolution the
#: screen actually has pushes every one of them over it. The scenes were
#: fast because they were fuzzy.
HAIRLINE = 1.0

#: Rings of offset hairlines are spaced this far apart, in real pixels.
#: Below one there are no gaps to see between them.
HAIR_STEP = 0.9

#: Past this many passes a real wide pen is cheaper, and correct.
#:
#: Fourteen is measured, and it is a floor as well as a ceiling. Swept
#: over three scenes at 1080p, the Rave costs 31 ms a frame here, 40 at
#: twenty passes, and about 100 at four, six, eight or ten - because at
#: those a line that *should* stack gets a real pen instead. The
#: Waterfall wants at least six for the same reason. A clamp on the
#: Rave's own widths, to keep its trusses stacking, was tried and made
#: that scene slower still: it moved lines out of the pen and into
#: thirteen-pass stacks, which is the worst of both.
#:
#: Nothing in these scenes draws a line that thick, so this is a
#: backstop. Raising it to 26 was tried, so that the Rave's widened
#: trusses would stack rather than fall back to a pen: it halved that
#: scene's worst frame and still left it at three times the median, so
#: the width is kept where stacking is cheap instead.
HAIR_MOST = 14

#: Solved alphas, kept because the answer depends only on how wide the
#: line is and how solid it is meant to be.
_HAIR_ALPHA: dict = {}


def smooth_path(points):
    """A curve through these points, rather than a line between them.

    Forty-four straight segments across a 1080p frame is one every
    forty-four pixels, and at a ribbon's peak - where the direction
    changes fastest - that reads as a corner. "Smooth out the lines in
    ambience, they look sectioned and straight in places" is exactly
    that: not the wrong shape, a shape drawn as a polygon.

    Each sample becomes the control point of a quadratic, and the
    midpoints between samples become the points the curve passes
    through. The curve leaves every segment tangent to the one before
    it, so there are no corners anywhere - at the same sample count,
    which is the whole reason for doing it this way rather than by
    adding points until nobody can see the joins.
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
    # Straight to the last point, not a curve through the one before
    # it: the loop above finishes at the midpoint of the final pair,
    # so a quadratic controlled by the *earlier* of them turns back on
    # itself. It put a hook on the end of every ribbon - 179 degrees
    # of turn in one step, at the one place a ribbon is supposed to
    # taper away.
    path.lineTo(points[-1])
    return path


def stroke(painter, path, colour, width: float,
           cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin) -> None:
    """Draw ``path`` in ``colour`` at ``width``, the quick way where there is one.

    A wide line is drawn as a handful of one-pixel lines nudged around a
    circle - which is what a wide line *is*: the curve swept by a disc.
    Above, HAIRLINE explains why that is worth doing.

    The hairlines are cosmetic pens, so they stay one real pixel however
    the painter is scaled, and the offsets are converted back out of real
    pixels into whatever units the painter is working in.

    Overlapping strokes accumulate alpha, so each pass is drawn fainter.
    How much fainter is solved for rather than guessed at - see
    ``_hair_alpha``, and the note there on why a constant cannot do it.

    A Waterfall frame at 1080p went from 41.4 ms to 7.7 ms this way, and
    the picture differs from the real thing by half of one channel step
    out of 255.
    """
    # Only where the passes stack the way the correction assumes they do.
    # Under additive compositing they do not: each pass adds its light
    # instead of covering what is under it, so a stack drawn at the
    # reduced alpha comes out hollow - a dark core with bright edges,
    # which is what it did to Ambience's ribbons.
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
    # Width zero is what makes it one real pixel whatever the painter is
    # scaled to; setCosmetic says so out loud.
    pen = QPen(faint, 0.0, Qt.PenStyle.SolidLine, cap, join)
    pen.setCosmetic(True)
    painter.setPen(pen)
    for dx, dy in spots:
        painter.translate(dx / scale, dy / scale)
        painter.drawPath(path)
        painter.translate(-dx / scale, -dy / scale)


def _hair_spots(reach: float) -> tuple:
    """Where to put the hairlines to fill a disc of radius ``reach``.

    The centre, then rings out to the edge no more than HAIR_STEP apart,
    each with enough points that neighbours on it are no further apart
    than that either - otherwise the ring scallops and the line looks
    beaded rather than thick.

    All of it or none of it. Stopping partway through leaves a line drawn
    to the radius of the last ring that fitted, which is a *thinner* line
    rather than a cheaper one: truncated at a five pixel width it put down
    47 per cent of the ink. An empty answer means "too thick for this -
    use a real pen", which is what ``stroke`` does with it.
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
    """How solid each pass must be for the stack to read as one wide line.

    There is no constant that does this. The passes overlap each other
    most when they are nearly on top of one another and least when they
    are spread out, so the same correction that is right for a 1.2 pixel
    line lays down 56 per cent of the ink at 3.2 pixels. Measured against
    a real pen, the share of the passes that has to carry the colour runs
    from about 0.85 at 1.2 pixels to 0.25 at 3.2.

    So it is solved instead. Take a straight line under the stack; at
    every offset ``d`` across it, count the passes whose own offset puts
    them within half a pixel of ``d`` - that is how many times that column
    gets painted, and ``1 - (1 - a)**k`` is how solid it ends up. Sum
    that across the line, average over the directions the line might run
    in, and find the ``a`` that totals what a pen of this width would.

    Bisection, over a histogram of those counts rather than the counts
    themselves, so it is a few dozen multiplications. Cached: the answer
    depends on nothing that changes within a frame.
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

    #: How a smoothed strobe rises and falls. Quick up, slow down, about a
    #: third of a second of tail.
    BLOOM_RISE = 0.30
    BLOOM_FALL = 0.055

    def bloom(self, state) -> float:
        """The strobe, smoothed, and advanced one frame.

        The hit a scene is handed is a step: full height on the frame it
        lands, then a linear decay over six. That is right for a scene
        made of bars and wrong for anything with a shape in it, because a
        step in the size of a shape is not a flash, it is a glitch - the
        vaporwave sun nearly doubled its radius in one frame and sprang
        back, which is what "it will flash huge and go back to normal
        movement" was.

        Call it once a frame. A scene that wants the raw step still has
        ``flash``.
        """
        hit = self.flash(state)
        was = getattr(self, "_bloom", 0.0)
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

    def paint(self, painter, rect, state, strength: float = 1.0,
              flash: float = None) -> None:
        """The field. ``flash`` overrides the strobe this reads.

        Ambience passes its own smoothed one: the field is half of what
        that scene shows, and a field that snaps while the ribbons bloom
        is not one strobe, it is two.
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
        # The field used to slide one way at one speed. Three clocks
        # running at different rates, each turning a wave in a different
        # direction, make it fold and drift instead - and the music drives
        # both how fast they run and how deep the folds are, so a loud
        # passage churns and a quiet one barely moves.
        pace = 0.55 + state.bass * 1.9 + state.mid * 0.8
        self._drift_a += 0.016 * pace
        self._drift_b -= 0.011 * pace + state.high * 0.02
        self._drift_c += 0.007 * pace
        hit = self.flash_of(state) if flash is None else max(0.0, flash)
        swell = 0.55 + state.bass * 0.8 + hit * 0.9
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

        # The strobe belongs to the sun here: a kick makes it flare rather
        # than washing the whole frame white. Smoothed, and it is mostly
        # light: at a raw hit of one the radius grew by 0.85 of the
        # horizon in a single frame and sprang back over six, which reads
        # as the sun glitching rather than as a beat.
        flash = self.bloom(state)
        self._sun(painter, width, horizon, hue, state.bass, flash)

        # No bar graph here on purpose: it stood in front of the city and
        # hid the thing that is already showing the same numbers. The
        # towers are the equaliser - one per band, rising with it.
        self._far_skyline(painter, width, horizon, state)
        self._skyline(painter, width, horizon, state)
        self._floor(painter, width, height, horizon, state)
        self._reflection(painter, width, height, horizon, state)
        self._ribbons(painter, width, horizon, state)
        self._stars(painter, width, horizon, state)

    #: The gaps across the sun: how far apart they sit and how far up the
    #: disc they go, both as a fraction of its radius.
    BAR_APART = 0.105
    BAR_TOP = 0.90

    #: How much of a strobe reaches the sun's size. The rest of it is
    #: brightness, which is what a flare actually is.
    FLARE_SIZE = 0.08

    @staticmethod
    def sun_radius(horizon: float, bass: float, flash: float) -> float:
        """How big the sun is. Separate so it can be asked for."""
        return horizon * (0.46 + bass * 0.26
                          + flash * Vaporwave.FLARE_SIZE)

    def _sun(self, painter, width, horizon, hue: float, bass: float,
             flash: float) -> None:
        """The sun: a glow, a face, and the gaps cut across it.

        The bars in front of it looked wrong, and fixing where they were
        drawn was only half of it. They were drawn from one edge of the
        sun's *bounding box* to the other, so they carried on out past the
        glow and across the skyline as dark rectangles - but they also
        implied a disc that was never there. All the sun had was a soft
        radial glow with no edge anywhere, so bars across it had nothing
        to belong to and read as rectangles lying on top of the picture.

        So there is a disc now, with the face every picture of this has:
        pale and warm at the top, deepening to magenta at the horizon. The
        gaps are drawn inside a clip of that disc, which is what stops
        them at its edge - no arithmetic, and they follow the circle
        exactly. They are spaced evenly and grow towards the horizon, so
        the sun dissolves into stripes at the bottom and stays whole at
        the top, rather than being evenly barred like a barcode.
        """
        radius = self.sun_radius(horizon, bass, flash)
        if radius <= 1.0:
            return
        centre = QPointF(width / 2.0, horizon)
        sky = QRectF(0, 0, width, horizon)

        # The air around it, which is what makes it a sunset rather than a
        # circle on a background.
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

    #: How many lines of the floor go by in a bar. Four, so one arrives
    #: on every beat: the floor is the only thing in this scene that
    #: travels, so it is the only thing that can carry the tempo.
    FLOOR_LINES = 15
    PER_BAR = 4.0

    def _scroll(self, state) -> float:
        """Where the floor has got to, on the beat where there is one.

        state.scroll is a free-running counter the pane advances by a
        little each frame and a little more when the bass is up - fine for
        a scene nobody is counting along with, and wrong for this one,
        whose horizontal lines march towards you in plain sight. On a
        record with a tempo they march *past* the beat, which reads as the
        scene ignoring the music it is drawn from.
        """
        tempo = getattr(state, "tempo", 0.0)
        if tempo <= 0.0:
            return state.scroll
        # One line a beat, which is the phase the pane hands over. Read
        # from the playhead each frame rather than accumulated, so a seek
        # lands where it should instead of somewhere it remembered.
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
        # A shade thicker than it was. A real trace is a glowing filament
        # rather than a pen line, and at 1.3 pixels the figures read as a
        # diagram of one.
        core = ((1.8 if drawing else 2.2) + flash * 1.2) * dpr
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
        count = len(trace) // 2
        # Stored as int16 so a long track's worth fits in memory.
        scale = 1.0 / 32768.0
        points = []
        for index in range(count):
            points.append(QPointF(
                trace[index * 2] * scale,
                # Screen y grows downwards and a scope's does not.
                -trace[index * 2 + 1] * scale))
        # A curve through the samples, not a line between them.
        #
        # A beam is a physical thing with a mass of electrons in it and a
        # deflection coil that cannot change direction instantly, so it
        # rounds every corner it is asked to draw. Joining the samples
        # with straight lines draws the corners the signal asks for and
        # not the ones a scope makes, which is why the figures came out
        # "straight and taking sharp turns".
        return smooth_path(points)

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




def _dots_between(*tables, apart: float = 0.035) -> tuple:
    """One point midway between each neighbouring pair of numbered marks.

    A class body cannot be read from inside a comprehension defined in
    it, so this is a function rather than two lines where it is used.
    """
    marks = sorted({fraction for table in tables for _v, fraction in table})
    out: list = []
    for first, second in zip(marks, marks[1:]):
        middle = (first + second) / 2.0
        if not out or middle - out[-1] > apart:
            out.append(middle)
    return tuple(out)


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
    #: The face's size, in radii, measured off the reference rather than
    #: adjusted towards it.
    #:
    #: Its cell is 562 by 339 and its arc runs from (55,192) to (500,192)
    #: over an apex at y=75 - a chord of 445 rising 117, which is a
    #: radius of 270 and a sweep of 111 degrees. Everything drawn fits in
    #: 499 by 269 of that cell, which is 1.85 radii by 1.00, and the
    #: centre of the arc sits 1.12 radii below the top of it: at the very
    #: bottom, where the needle is hinged and just past what is drawn.
    #:
    #: The numbers here were 2.34 by 1.46. Being too wide is what made
    #: the faces small - the radius is whichever of the two dimensions
    #: runs out first, and asking for a quarter more width than the face
    #: uses throws that quarter away.
    #: What a face actually draws in, measured rather than reasoned about.
    #:
    #: These were 1.95 by 1.02 and the drawing needs 1.89 by 1.10. Six per
    #: cent of missing height does not sound like much and it is what put
    #: the bottom row of a full screen off the bottom of it: every cell
    #: overflowed by fourteen pixels, and the last row overflowed into
    #: nothing. The needle is what does it - it hinges below the arc and
    #: swings past the frequency label - so the reserved height has to
    #: cover a face at rest and a face pinned, which is what the measuring
    #: script checks at three deflections.
    FACE_WIDE = 1.93
    FACE_TALL = 1.13
    #: How far below the top of the face the arc's centre sits. The
    #: difference between this and FACE_TALL is the room under the hub.
    FACE_DROP = 1.16
    #: Where the two lines of text sit, above the centre and inside the
    #: arc, which is where the reference puts them. They used to be below
    #: the centre, outside everything, which is what the extra height was
    #: being reserved for.
    DB_AT = 0.46
    LABEL_AT = 0.24
    #: Type sizes, as shares of the radius, in one place so a face keeps
    #: its proportions at every size it is drawn at.
    #: How far out the dB numbers sit. The reference puts them at 1.07
    #: radii - close in, almost touching the arc - and 1.20 is what made
    #: the face taller than the reference's by the difference.
    #: The arc's own stroke, measured where nothing crosses it: 0.020
    #: radii, which on the reference's 270 pixel radius is 5.3 px. It was
    #: drawn at 0.030, and the part above 0 dB at 0.052 - one and a half
    #: to two and a half times too heavy, which is most of why the face
    #: read as a diagram rather than as an instrument.
    ARC_STROKE = 0.020
    #: The run above 0 dB is heavier, but only a little.
    ARC_STROKE_HOT = 0.027
    #: Where the arc's stroke sits. Not at the radius itself: measured,
    #: it runs 0.95 to 0.98, so its centre line is a little inside.
    ARC_AT = 0.968
    #: Ticks reach outward past the arc, not inward from it. Measured at
    #: -3, 0, +1, +2 and +3 the ink continues to about 1.05 radii and
    #: there is none inside; they were being drawn from 0.84 to 1.00,
    #: which is the wrong side of the line they mark.
    TICK_IN = 0.945
    TICK_OUT = 1.052
    #: The small marks between them are dots, and they are outside the
    #: arc too - measured widths of 0.005 to 0.008 of the sweep, against
    #: 0.037 to 0.047 for a tick.
    DOT_AT = 1.012
    DOT_SIZE = 0.011
    #: A squarish face, like the reference's: its "0" is a rounded
    #: rectangle rather than a circle. That is the Eurostile family,
    #: which is not on a Mac, so this is the closest of the ones that
    #: are - measured on the width of a "0" against its height and how
    #: much of its box the ink fills.
    #: In order of preference, because none of these is on every
    #: machine and a face that silently falls back to the system default
    #: is the thing this is trying to avoid. Eurostile and Microgramma
    #: are the real article; the rest are the closest of what a Mac and a
    #: Linux build machine actually carry, ranked by measuring the width
    #: of a "0" against its height and how much of its box the ink fills.
    #: The face, which ships with the app - see ``dial_face``. The rest
    #: are only what Qt falls back to if that file ever goes missing, and
    #: none of them is right: nothing installed on a Mac or on a build
    #: runner has square digits.
    FAMILIES = ("Eurostile", "Microgramma", "Square721 BT", "Bank Gothic",
                "Verdana", "DejaVu Sans", "Futura", "Gill Sans",
                "Avenir Next", "Liberation Sans", "Helvetica Neue")

    @staticmethod
    def _lettering(font):
        """Put the dial's own face on a font, if it loaded."""
        family = dial_face()
        font.setFamilies(([family] if family else [])
                         + list(Meters.FAMILIES))
        # Michroma has one weight and it is the right one. Asking for bold
        # makes Qt synthesise a heavier version by smearing it sideways,
        # which is what turned the numbers into blobs at small sizes.
        font.setBold(not family)
        return font
    #: 1.09 rather than the 1.07 measured to the reference's own label
    #: centres, because this face's numerals are a shade taller than its
    #: and at 1.07 their bottoms sat on the arc instead of above it.
    #: Further out than it was, and smaller. Both because the face
    #: changed: Michroma is a wide, square design, so the same point size
    #: sets numbers half again as wide as Verdana's and they ran into the
    #: arc, into the ticks, and into each other at the crowded left end.
    #: Solved rather than nudged, against two things at once: the
    #: reference's face is 1.85 radii wide, and the numbers have to clear
    #: what is under them.
    #:
    #: Michroma is a wide design, so the size that looks right for a
    #: humanist sans is half again too big here - at 0.098 radii of type
    #: the face came out 2.09 across against the reference's 1.85, because
    #: the outermost thing on a face is the "-24" and it had grown.
    #:
    #: The first solve cleared the *arc*, at 0.968 radii, and put the row
    #: at 1.06. That is under the ticks, which reach out to 1.052, so
    #: every number came to rest on a tick tip. What has to be cleared is
    #: whichever of the two reaches further.
    DB_AT_R = 1.12
    DB_TYPE = 0.072
    #: Nearly as large as the dB row, which is what the reference has:
    #: they read as two scales on one face rather than as a scale and a
    #: footnote. At 0.082 they were a smudge under the arc.
    PERCENT_TYPE = 0.062
    UNIT_TYPE = 0.088
    LABEL_TYPE = 0.096

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
    #: Where the per-cent row sits, inside the arc. Further in than the
    #: 0.86 it was, for the same reason the dB row moved out: with a wide
    #: face at 0.86 radii the numbers are nearly touching the scale they
    #: are inside of.
    PERCENT_AT = 0.82

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
    #: Built from both tables: one dot between each neighbouring pair of
    #: numbered marks, wherever they fall, thinned so two that nearly
    #: coincide do not print on top of each other.
    DOTS = _dots_between(DB_MARKS, PERCENT_MARKS)
    #: Where the dots go: midway between each pair of marks that carries
    #: a number, on both scales. A handful, evenly spread - the reference
    #: has eight or nine of them. What was here was one per decibel from
    #: -20 up, which is twenty-four marks crowded into the left half and
    #: is what made the row read as a smear rather than as points.
    @staticmethod
    def _between(marks):
        at = sorted(set(marks))
        return [(a + b) / 2.0 for a, b in zip(at, at[1:])]

    #: Kept so anything that still names it finds an empty tuple rather
    #: than an attribute error. The face draws DOTS above.
    MINOR: tuple = ()

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
        # Cells the size of a face, not the size of the frame divided up.
        #
        # A rack of ten on a wide screen is four across and three down,
        # and a face is 1.74 times as wide as it is tall where a third of
        # a 16:9 frame is 1.78 - so dividing the frame evenly left about a
        # hundred pixels of air under every row, all of which piled up at
        # the bottom and read as the rack having slipped upwards. The
        # block is built at its own size and centred instead.
        radius = self._radius(rect, columns, rows)
        cell_w = min(rect.width() / columns, radius * self.FACE_WIDE * 1.06)
        cell_h = min(rect.height() / rows, radius * self.FACE_TALL * 1.06)
        left = rect.left() + (rect.width() - cell_w * columns) / 2.0
        top = rect.top() + (rect.height() - cell_h * rows) / 2.0
        #: How many are on each row, so the last one can be centred.
        on_row = [min(columns, count - r * columns) for r in range(rows)]
        boxes = []
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
            box = QRectF(left + spare + column * cell_w,
                         top + row * cell_h,
                         cell_w, cell_h)
            label = (state.dial_labels[index]
                     if index < len(state.dial_labels) else "")
            boxes.append((box, levels[index], label))

        # The backlights first, all of them, before any face is drawn.
        #
        # Each one used to be filled inside its own cell, and a meter's
        # backlight reaches half a radius past the face - so it stopped
        # dead at the edge of the cell with a straight line down it, and
        # the next meter's face was then drawn over the top of whatever
        # had spilled. That is the strobe "clipping behind other meters".
        # Light does not belong to a cell.
        if flash > 0.02:
            for box, _value, _label in boxes:
                self._backlight(painter, rect, box, state, flash)
        for box, value, label in boxes:
            self._meter(painter, box, value, label, state, flash, dpr)

    def _backlight(self, painter, rect, box, state, flash) -> None:
        """One meter's lamp coming up, over whatever is around it."""
        geometry = self._geometry(QRectF(0, 0, box.width(), box.height()))
        pivot = geometry["pivot"] + box.topLeft()
        reach = geometry["radius"] * 1.5
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
            left = count - (rows - 1) * columns
            # Never one on its own at the bottom. Ten meters three across
            # is four rows of 3, 3, 3 and 1, and that single meter under
            # the rack reads as a mistake rather than as a layout - which
            # is what "I want a grid layout, not just one on the bottom
            # row" was about. Two or more is a short row; one is a stray.
            if rows > 1 and left == 1:
                continue
            cell_w = rect.width() / columns
            cell_h = rect.height() / rows
            if cell_w <= 48 or cell_h <= 34:
                continue
            radius = Meters._radius(rect, columns, rows)
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

        # A small face drops what it cannot show legibly rather than
        # printing it on top of itself. Ten meters in a strip two hundred
        # pixels tall leaves each one about forty pixels of radius, and
        # everything a full face carries will not fit in that.
        roomy = radius >= self.ROOMY
        marks = self.DB_MARKS if roomy else self.SPARSE_MARKS

        for _value, fraction in marks:
            self._tick(painter, centre, radius, fraction, colour,
                       self.TICK_IN, self.TICK_OUT,
                       max(1.0, radius * self.ARC_STROKE))
        if roomy:
            # Dots, not lines. Every meter of this kind puts a row of
            # small points inside the arc between the numbered marks, and
            # short strokes hanging off the scale read as a comb instead.
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
        # Measured against the radius, and the same proportion at every
        # size. These were all a half larger than the reference, which is
        # what made a face look like a diagram of a meter rather than a
        # meter: the numbers were competing with the scale instead of
        # labelling it.
        font.setPointSizeF(max(6.0, radius * self.DB_TYPE))
        painter.setFont(font)
        painter.setPen(QPen(colour))
        for value, fraction in marks:
            self._label(painter, centre, radius * self.DB_AT_R, fraction,
                        str(value))
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
            # Dimmer than the dB row, as the reference has them: two
            # scales on one face, and only one of them is the scale.
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
        # Stopped well short of the hinge, and with no collar drawn at
        # it. The reference shows no hub at all: the needle simply runs
        # off the bottom of the face, and the lowest thing on it is the
        # frequency. Drawing a hub put the face's bottom edge a sixth of
        # a radius lower than the reference's, which is what kept the
        # proportions wrong however the rest was adjusted.
        tail = pivot + reach * (radius * 0.15)
        # No halo behind it. There was one - a wide, faint stroke under
        # the needle to suggest a lit pointer - and it read as a smear
        # rather than as light, because a glow around a hard edge is a
        # thing a camera does and this is not a photograph of a meter, it
        # is a meter. The reference has none either: the needle there is
        # a clean tapered blade.
        #
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

    #: How much of the strobe the shape gets. The rest is light.
    #:
    #: This scene is made of long curves, and a step in the shape of a
    #: long curve is a lurch: the ribbons used to jump half their height
    #: outwards and snap back inside a tenth of a second. They brighten
    #: and bloom where they cross now, and barely move. The smoothing
    #: itself is ``Scene.bloom``.
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
        # The morphing field, behind everything and dim enough that the
        # ribbons still read as the bright thing. It lifts with the strobe
        # too, so the whole frame breathes rather than only the ribbons.
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
            # Capped. Additive compositing charges per pixel of stroke,
            # and an uncapped width put a sixteen pixel ribbon across a
            # 1080p frame ten times over - seventeen milliseconds before
            # any polish, which is the whole frame gone.
            thick = max(1.6, min(9.0, height * 0.010
                                  * (0.6 + band + flash * 0.5)))
            for side in (+1, -1):
                path = self._ribbon_path(width, middle, reach, turn,
                                         ribbon, side)
                stroke(painter, path, colour, thick)
        painter.restore()
        self._core(painter, width, middle, state, flash)

    #: Kept as a name on the scene because that is where it was found,
    #: and because a test names it. The curve itself is shared with the
    #: oscilloscope now - both draw a path through samples, and both were
    #: drawing it as a polygon.
    _smooth = staticmethod(smooth_path)

    def _ribbon_path(self, width, middle, reach, turn, index, side):
        """One curve, mirrored by ``side``.

        Two sines of different periods rather than one, because a single
        sine reads as a rope and two read as something being blown about.
        """
        points = []
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
            stroke(painter, path, colour, 1.0 + share * 1.4 + flash * 0.4)

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
class Rave(Scene):
    """A room lit by the kit, seen from inside it.

    Every other scene here draws the spectrum. This one draws the *hits*:
    the analysis picks the kick, the snare, the hats and the synth apart
    before a note plays, and each one is wired to something different, so
    what the room does is what the drummer did rather than a general
    reaction to loudness.

        kick    the floor and ceiling lunge towards you, and the horizon
                pushes back - the whole room moves rather than a shape in it
        snare   a ring leaves the middle and crosses the room
        hats    beams flick out along the grid, one per hit
        bass    how far the corridor opens up, and how hot the haze is
        synth   the colour of everything, swung round the wheel

    Perspective is one divide per point - x/z and y/z, with the grid laid
    out in world coordinates and projected each frame. Not a camera in the
    full sense: there is no rotation to speak of, because a lighting rig
    does not tumble and a scene that does is unwatchable at this speed.

    Drawn back to front so nearer things cover further ones, and every
    line is one stroke of a path rather than a segment at a time, since a
    grid is the one thing here with enough segments for that to matter.
    """

    name = "Rave"
    blurb = "a room lit by the kit: kick, snare, hats and synth, in 3D"

    #: How far down the corridor the grid runs, and how finely.
    DEPTH = 26
    ACROSS = 9
    #: Nearest and furthest z. Nothing is drawn nearer than NEAR, because
    #: a point at z=0 projects to infinity.
    NEAR = 0.55
    FAR = 15.0
    #: How fast the world comes towards you at rest, in z per second.
    DRIFT = 2.6

    #: How fast each part of the kit reaches the room, and how slowly it
    #: lets go. A drum is a step, and a room that steps is a room that
    #: glitches: the kick used to move the horizon, the focal length, the
    #: walls and every line width in the single frame it landed on, and
    #: back over the six after it. What a kick does to a room is push it,
    #: and a push takes time to arrive and longer to fade.
    #: How much a full bass front-loads the travel within a beat. At 0
    #: the room moves evenly; at 1.6 it covers three quarters of the beat's
    #: distance in the first third of it.
    SURGE = 1.6

    THUMP_RISE, THUMP_FALL = 0.34, 0.075
    CRACK_RISE, CRACK_FALL = 0.85, 0.22
    WASH_RISE, WASH_FALL = 0.30, 0.030
    FIZZ_RISE, FIZZ_FALL = 0.55, 0.16

    def __init__(self) -> None:
        self._z = 0.0
        self._last = None
        self._rings: list = []
        self._beams: list = []
        self._spin = 0.0
        self._haze_key = None
        self._haze_image = None
        #: The kit, smoothed: the kick pushing the room, the snare washing
        #: its colour, the hats shaking the thing in the middle.
        self._thump = 0.0
        self._crack = 0.0
        self._beat_was = None
        self._beat_count = 0.0
        self._wash = 0.0
        self._wash_hue = 0.0
        self._fizz = 0.0

    # -- the clock --------------------------------------------------------
    def _beats_done(self, state) -> float:
        """How many beats have gone by, counting fractions.

        Kept as a running total rather than read from the playhead each
        frame, because the phase the pane hands over wraps at every beat
        and a wrap is a jump. This adds up the wraps.
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
        """Move the world on by however long the last frame took.

        By the clock rather than by the frame, so the room travels at the
        same speed whatever the pane is managing - a corridor that speeds
        up when the window is small is the sort of thing that makes a
        scene feel cheap.
        """
        now = time.monotonic()
        step = 0.016 if self._last is None else min(0.1, max(0.0, now - self._last))
        self._last = now

        def ease(was, to, rise, fall):
            return was + (to - was) * (rise if to > was else fall)

        kit = state.kit
        self._thump = ease(self._thump, kit.get("Kick", 0.0),
                           self.THUMP_RISE, self.THUMP_FALL)
        # And a second, much faster envelope off the same kick, for the
        # one thing that is meant to snap: a shake is a shake or it is a
        # wobble.
        self._crack = ease(self._crack, kit.get("Kick", 0.0),
                           self.CRACK_RISE, self.CRACK_FALL)
        self._fizz = ease(self._fizz, kit.get("Hats", 0.0),
                          self.FIZZ_RISE, self.FIZZ_FALL)
        snare = kit.get("Snare", 0.0)
        if snare > self._wash + 0.12:
            # A snare does not brighten the room, it repaints it: each one
            # moves the colour on by a step of its own, and the colour
            # then stays where it was put until the next.
            self._wash_hue = (self._wash_hue + 0.13 + snare * 0.09) % 1.0
        self._wash = ease(self._wash, snare, self.WASH_RISE, self.WASH_FALL)

        # Speed is the bass. It was one term of three and the smallest of
        # them; it is the one that should be felt, because how fast a room
        # comes at you is how hard the track is pushing.
        bass = max(state.bass, kit.get("Bass", 0.0))
        tempo = getattr(state, "tempo", 0.0)
        if tempo > 0.0:
            # On the grid: one truss passes you every beat, exactly.
            #
            # A room that travels at whatever the bass happens to be is a
            # room that never arrives anywhere - the only things in it
            # with a length are the trusses, and if they drift past the
            # beat then nothing in the scene is on the music. So the
            # *distance* per beat is fixed and the bass changes how it is
            # spent: at rest the room moves evenly through the beat, and
            # under a heavy bass most of the beat's travel happens in the
            # first part of it, which is a lunge on the beat and a coast
            # before the next one. Same tempo, much more push.
            beats = self._beats_done(state)
            surge = 1.0 / (1.0 + bass * self.SURGE)
            whole = math.floor(beats)
            through = beats - whole
            self._z = (whole + through ** surge) * self.TRUSS
        else:
            self._z += step * self.DRIFT * (1.0 + bass * 3.4
                                            + self._thump * 0.9)
        self._spin += step * (0.25 + kit.get("Synth", 0.0) * 1.1
                              + self._fizz * 2.2)
        return step

    def paint(self, painter, rect, state) -> None:
        step = self._advance(state)
        # The eased kick everywhere the room moves. The raw one still
        # fires the things that are *meant* to be sudden - the rings and
        # the beams - because a snare hit is an event and the room is not.
        kick = self._thump
        snare = state.kit.get("Snare", 0.0)
        hats = state.kit.get("Hats", 0.0)
        synth = state.kit.get("Synth", 0.0)
        bass = max(state.kit.get("Bass", 0.0), state.bass)
        flash = self.bloom(state)

        painter.fillRect(rect, QColor(3, 2, 8))
        centre = rect.center()
        # The kick pushes the horizon away and pulls the walls in, which
        # reads as the room breathing rather than as a shape being scaled.
        span = min(rect.width(), rect.height())
        focal = span * (0.62 - kick * 0.10)
        horizon = QPointF(centre.x(),
                          centre.y() - rect.height() * (0.02 + kick * 0.05))
        hue = (self._spin * 0.11 + synth * 0.22 + self._wash_hue) % 1.0

        self._haze(painter, rect, horizon, bass, synth, flash)
        self._grid(painter, rect, horizon, focal, hue, bass, kick, flash)
        self._rings_now(painter, rect, horizon, focal, snare, step, hue, flash)
        self._beams_now(painter, rect, horizon, focal, hats, step, hue)
        self._core(painter, horizon, span, hue, bass, kick, synth, flash,
                   self._weight(rect))

    # -- the parts --------------------------------------------------------
    #: How big the haze is actually drawn before being stretched over the
    #: frame. A gradient has no detail in it, so nobody can tell - and a
    #: full-frame gradient is pure fill rate, which is the one thing a
    #: machine without a graphics card is worst at. Measured on a build
    #: runner, this scene cost four times what it costs here while the
    #: others cost twice; painting the haze small is most of that gap.
    HAZE = 96

    def _haze(self, painter, rect, horizon, bass, synth, flash) -> None:
        """The air in the room, lit from the far end."""
        small = self._haze_tile(rect, horizon, bass, synth, flash)
        if small is None:
            return
        painter.drawImage(rect, small, QRectF(small.rect()))

    def _haze_tile(self, rect, horizon, bass, synth, flash):
        """The glow, painted into a small image and kept while it fits.

        Rebuilt only when what it looks like changes enough to see, which
        for a gradient is not often: the colour is quantised to a few
        dozen steps and the rest of the time the same image is stretched
        again.
        """
        if rect.width() < 2 or rect.height() < 2:
            return None
        # Brighter and wider than it was. The corridor is closed in now,
        # and a closed corridor that fades to nothing at the far end has a
        # hole in it rather than a distance - the grid lines run out and
        # what is left is a dark rectangle the eye reads as a wall.
        key = (round((0.62 + synth * 0.3) % 1.0, 2),
               round(min(1.0, 0.34 + bass * 0.5 + flash * 0.4), 2),
               round(min(1.0, 0.46 + bass * 0.42), 2),
               round(0.58 + bass * 0.35, 2),
               round((horizon.x() - rect.left()) / rect.width(), 2),
               round((horizon.y() - rect.top()) / rect.height(), 2))
        if self._haze_key == key and self._haze_image is not None:
            return self._haze_image
        size = QSize(self.HAZE, max(2, int(self.HAZE * rect.height()
                                           / max(1.0, rect.width()))))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        middle = QPointF(key[4] * size.width(), key[5] * size.height())
        reach = min(size.width(), size.height()) * key[3] * 2.0
        glow = QRadialGradient(middle, max(1.0, reach))
        glow.setColorAt(0.0, QColor.fromHsvF(key[0], 0.75, key[1], key[2]))
        glow.setColorAt(1.0, QColor(0, 0, 0, 0))
        into = QPainter(image)
        try:
            into.setPen(Qt.PenStyle.NoPen)
            into.setBrush(glow)
            into.drawRect(QRectF(0, 0, size.width(), size.height()))
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

    #: How many depth bands the grid is drawn in. Each is one stroke with
    #: its own weight, so the corridor dissolves into the haze instead of
    #: stopping dead at the far end. Four is where it stops being visible
    #: as banding and starts reading as distance.
    BANDS = 4
    #: Every this many rows, a frame around the corridor. They are what
    #: gives the room a length: a grid alone is a floor and a ceiling, and
    #: a truss every few metres is a building.
    TRUSS = 5

    #: Lines across a side wall. Far fewer than the floor gets, because
    #: the corridor is about eight times wider than it is tall: laid out
    #: with the floor's count they came out a twentieth of a unit apart
    #: and read as hatching rather than as a grid.
    UPRIGHTS = 3

    def _surfaces(self, lift, span):
        """The four walls of the corridor: where a point across each one
        is, how far its colour is turned, and how many lines it gets.

        Floor, ceiling and both sides from one description, because they
        are the same grid turned, and writing them out separately is how
        four surfaces drift apart.
        """
        # The two walls are one surface with two faces. They share a hue,
        # so drawing them in one path halves what they cost, and a stroke
        # is what this scene spends its frame on. The cross-lines are
        # drawn in halves (see _grid) so that nothing joins them across
        # the room.
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
        """The corridor: four surfaces, fading with distance, with trusses.

        It used to be a floor and a ceiling and nothing at the sides, so
        the room was a pair of planes with the dark showing between them.
        Closing it in is most of what makes it a room.

        Each surface is drawn in depth bands rather than as one path, so
        the near lines are bright and heavy and the far ones fade into the
        haze. That is the whole difference between a wireframe and a
        space: a grid drawn at one weight ends abruptly wherever the loop
        happens to stop.
        """
        painter.setBrush(Qt.BrushStyle.NoBrush)
        weight = self._weight(rect)
        glow = self._glow(rect)
        width = (1.0 + bass * 1.2) * weight
        # How far the surfaces are from the eye - the corridor opening up
        # on a bass note is most of what makes the room feel big.
        lift = 0.55 + bass * 0.22
        span = self.ACROSS * 0.5
        offset = self._z % 1.0
        reach = self.FAR - self.NEAR
        for place, shift, lines in self._surfaces(lift, span):
            base = QColor.fromHsvF(
                (hue + shift) % 1.0, 0.85 - flash * 0.4, 1.0, 1.0)
            # The lines that run away from you, drawn whole: they carry
            # the perspective, and cutting them into bands would show the
            # joins.
            away = QPainterPath()
            for column in range(-lines, lines + 1):
                across = column / lines
                x, y = place(across)
                away.moveTo(self._project(horizon, focal, x, y, self.NEAR))
                away.lineTo(self._project(horizon, focal, x, y, self.FAR))
            stroke(painter, away,
                   self._shade(base, min(1.0, (0.20 + bass * 0.30
                                          + kick * 0.26
                                          + flash * 0.26) * glow)),
                   width * (1.0 + kick * 1.4))

            # And the ones across it, marching towards you, in bands.
            for band in range(self.BANDS):
                path = QPainterPath()
                for row in range(band, self.DEPTH, self.BANDS):
                    z = self.NEAR + (row + offset) * reach / self.DEPTH
                    # In two halves, so a surface that is really two
                    # faces - the pair of walls - does not draw a line
                    # straight across the room joining them.
                    for lo, hi in ((-1.0, -0.002), (0.002, 1.0)):
                        path.moveTo(
                            self._project(horizon, focal, *place(lo), z))
                        path.lineTo(
                            self._project(horizon, focal, *place(hi), z))
                # Bands further back are dimmer and thinner. Squared, so
                # the fall is steep near the eye and gentle in the
                # distance, which is how air actually works.
                near = 1.0 - band / self.BANDS
                stroke(painter, path,
                       self._shade(base,
                                   min(1.0, (0.10 + bass * 0.26 + kick * 0.24
                                             + flash * 0.24) * glow
                                       * (0.14 + near * near))),
                       width * (0.45 + near * 0.75) * (1.0 + kick * 1.4))

        self._trusses(painter, horizon, focal, hue, lift, span, bass, kick,
                      flash, reach, weight, glow)

    #: The frame this scene's line weights were chosen against. A line
    #: thicker than a real pixel is drawn by ``stroke`` as a stack of
    #: hairlines, so making them grow with the frame costs nothing.
    DRAWN_FOR = 700.0

    #: How much of the contrast a big frame gets back as *light* rather
    #: than as width. Brightness is free and width is not: stacking a four
    #: pixel line costs nineteen passes, and drawing it with a real pen
    #: costs a hundred milliseconds a frame at 1080p. So the lines grow a
    #: little and brighten a lot.
    LIFT = 0.45

    @classmethod
    def _glow(cls, rect) -> float:
        """How much brighter to draw, for a frame this size."""
        return 1.0 + (cls._weight(rect) - 1.0) * cls.LIFT

    @classmethod
    def _weight(cls, rect) -> float:
        """How thick to draw, for a frame this size.

        The lines used to be cosmetic - a fixed number of real pixels
        however big the frame was - so a full screen got the same
        hairlines spread over four times the area and washed out. Measured
        across sizes, the contrast fell by a third from 640x360 to 1080p
        and the brightest tenth of the picture went from 107 to 82 of 765.
        That is "the rave scene doesn't have as much contrast in full
        screen mode, it actually looks better in windowed".
        """
        return max(0.75, min(1.30, rect.height() / cls.DRAWN_FOR))

    @staticmethod
    def _shade(base, alpha: float):
        colour = QColor(base)
        colour.setAlphaF(max(0.0, min(1.0, alpha)))
        return colour

    def _trusses(self, painter, horizon, focal, hue, lift, span, bass, kick,
                 flash, reach, weight, glow) -> None:
        """A frame round the corridor every few metres, coming at you.

        The thing the room was missing: a grid tells you where the floor
        is and a truss tells you how far down the room you are looking.
        They brighten on the kick with everything else, and the nearest
        one is much the brightest, so the eye has something travelling
        rather than a field of lines that all move together.
        """
        corners = ((-span, lift), (span, lift), (span, -lift), (-span, -lift))
        # On their own clock, not the grid's.
        #
        # They used to ride the grid's offset, which wraps every *row*:
        # so a truss crept back one row's worth and then jumped forward
        # five to where the next one had been, sixty times a minute. That
        # is what stopped it reading as a continuous walk forward - the
        # only things in the room with a length to them stuttered, and
        # they did it whether anything was playing or not.
        offset = self._z % self.TRUSS
        for step in range(0, self.DEPTH, self.TRUSS):
            row = step + offset
            if row >= self.DEPTH:
                continue
            z = self.NEAR + row * reach / self.DEPTH
            near = max(0.0, 1.0 - (z - self.NEAR) / reach)
            path = QPainterPath()
            first = None
            for x, y in corners:
                point = self._project(horizon, focal, x, y, z)
                if first is None:
                    path.moveTo(point)
                    first = point
                else:
                    path.lineTo(point)
            path.lineTo(first)
            base = QColor.fromHsvF((hue + 0.04) % 1.0,
                                   max(0.0, 0.6 - flash * 0.4), 1.0, 1.0)
            stroke(painter, path,
                   self._shade(base,
                               min(1.0, (0.16 + bass * 0.22 + kick * 0.34
                                         + flash * 0.3) * glow
                                   * (0.30 + near * near * 1.4))),
                   (0.9 + near * 2.2) * (1.0 + kick * 1.1) * weight)

    def _rings_now(self, painter, rect, horizon, focal, snare, step, hue,
                   flash):
        """A ring per snare, leaving the far end and passing you."""
        if snare > 0.75 and (not self._rings or self._rings[-1][0] > 1.2):
            self._rings.append([self.FAR * 0.9, snare])
        alive = []
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for ring in self._rings:
            ring[0] -= step * 9.0
            if ring[0] <= self.NEAR:
                continue
            alive.append(ring)
            z, force = ring
            fade = max(0.0, min(1.0, (z - self.NEAR) / (self.FAR - self.NEAR)))
            radius = focal * (1.9 * force + 0.6) / z
            colour = QColor.fromHsvF((hue + 0.5) % 1.0, 0.55, 1.0,
                                     (1.0 - fade) * 0.85 * force)
            pen = QPen(colour, (1.0 + (1.0 - fade) * 4.0 + flash * 2.0)
                       * self._weight(rect))
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawEllipse(horizon, radius, radius * 0.62)
        # Never more than a bar's worth on screen at once.
        self._rings = alive[-8:]

    def _beams_now(self, painter, rect, horizon, focal, hats, step, hue):
        """A beam per hat, flicked out along the floor and gone."""
        if hats > 0.5 and (not self._beams or self._beams[-1][2] < 0.72):
            angle = (self._spin * 2.3 + len(self._beams) * 1.7) % math.tau
            self._beams.append([angle, hats, 1.0])
        alive = []
        for beam in self._beams:
            beam[2] -= step * 5.5
            if beam[2] <= 0.0:
                continue
            alive.append(beam)
            angle, force, life = beam
            colour = QColor.fromHsvF((hue + 0.18) % 1.0, 0.35, 1.0,
                                     life * 0.7 * force)
            pen = QPen(colour, (1.0 + life * 2.4) * self._weight(rect))
            pen.setCosmetic(True)
            painter.setPen(pen)
            far = self._project(horizon, focal,
                                math.cos(angle) * 4.5,
                                math.sin(angle) * 1.6, self.FAR * 0.55)
            painter.drawLine(horizon, far)
        self._beams = alive[-14:]

    def _core(self, painter, horizon, span, hue, bass, kick, synth, flash,
              weight=1.0):
        """The thing in the middle: a wireframe that turns, swells and shakes.

        Drawn last and small. It is the only object in the room with a
        shape of its own, and the room is the subject.

        The *kick* is what throws its corners about, and the bass is what
        drives the room past you. They were both doing a bit of both,
        which is why neither read as itself: a kick and a loud bassline
        arrive together most of the time, so two effects sharing them look
        like one effect. One object shaking and one room moving is a
        difference you can see.

        The hats still spin it (in ``_advance``), which is a different
        thing again - a spin is continuous and a shake is not.
        """
        # The raw hit, not the eased one. The room is pushed slowly on
        # purpose; the thing in the middle is supposed to be hit.
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
            # Per corner, and on its own phase, so the shape breaks up
            # rather than translating.
            shake = 1.0 + fizz * 0.55 * math.sin(turn * 6.1 + corner * 2.3)
            points.append(QPointF(
                horizon.x() + math.cos(angle) * size * shake,
                horizon.y() + math.sin(angle) * size * (0.5 + lean) * shake))
        colour = QColor.fromHsvF((hue + 0.32 + synth * 0.1) % 1.0,
                                 max(0.0, 0.25 - fizz * 0.2), 1.0,
                                 min(1.0, 0.5 + kick * 0.5 + fizz * 0.3))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        path = QPainterPath()
        # Every corner to every other: a wireframe rather than an outline,
        # which is what makes it read as a solid seen through.
        for a in range(len(points)):
            for b in range(a + 1, len(points)):
                path.moveTo(points[a])
                path.lineTo(points[b])
        stroke(painter, path, colour,
               (1.4 + kick * 2.4 + flash * 1.6 + fizz * 1.2) * weight)


SCENES = (Vaporwave(), Tunnel(), Oscilloscope(), Bars(), Meters(),
          Ambience(), Waterfall(), Rave())


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
    # A room full of haze and beams: heavy bloom, a strong vignette, and
    # the colour fringing a wide lens gives. No scanlines - this is not a
    # screen, it is a place.
    "Rave": {"bloom": 0.92, "vignette": 0.52, "aberration": 1.6,
             "grain": 0.04},
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


#: What the strobe should be doing in each scene, as (source, rate,
#: sensitivity), used when somebody picks a scene and has not set the
#: controls themselves.
#:
#: One setting cannot suit all of them, because the scenes do quite
#: different things with a flash. The meters brighten, so a fast strobe
#: there is a lamp flickering; the tunnel lurches, so a fast one is
#: motion sickness; the rave scene is built to be hit hard and a slow
#: one leaves it looking asleep. These are starting points, not locks -
#: touching either slider stops them being applied.
STROBE_SETUP = {
    # Hits the whole room, and is meant to: the eagerest of the set, and
    # still short of the point where the strobe starts running through
    # held notes. Reaching that is something somebody does with the two
    # sliders, not something that happens because they picked a scene.
    "Rave": ("Kick", 0.58, 0.58),
    # Neon and glass: the flash is a lurch forward, so it wants to be
    # rare enough to read as an event.
    "Neon tunnel": ("Kick", 0.30, 0.45),
    # A skyline lighting up. On the beat rather than on every drum.
    "Vaporwave city": ("Bass", 0.45, 0.50),
    # An instrument. The needles are the subject and the flash is the
    # lamp behind them, so it stays out of the way.
    "VU meters": ("Kick", 0.22, 0.40),
    # The trace gains gain on a hit; the hats give it a shimmer without
    # moving the picture.
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
