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

import bisect
import logging
import math
import sys
import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QLinearGradient,
                           QPainter, QPainterPath,
                           QPen, QRadialGradient, QTransform)


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
#: different face again on Linux. That is why "xxx xxxx xxxx xxx xxxxx xx
#: xxx", and why a fallback list was never going to fix it.
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
    changes fastest - that reads as a corner. "Xxxxxx xxx xxx xxxxx xx
    xxxxxxxx, xxxx xxxx sectioned and straight in places" is exactly
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


def bounded(value, most: float = 1.0, least: float = 0.0) -> float:
    """A number from the analysis, forced back into the range it claims.

    Levels are nought to one by construction and tempos are positive,
    but a decode that goes wrong, a calibration that comes out zero or a
    tempo found in silence can put a nan or an infinity in one. Several
    of these scenes *accumulate* what they are given - the field's drift,
    the rider's envelope followers - so a single bad frame does not draw
    a bad frame, it poisons the scene for the rest of the session and
    takes the window with it if the value reaches an ``int()``.

    Nan comes back as the floor rather than as the nearest bound,
    because a nan is an answer that was never computed.
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


#: The range of tempos a scene is driven at, and what happens to a
#: track that comes out beyond it.
#:
#: Tempo detectors make octave errors: they find the right pulse and
#: report it doubled or halved. Measured across eight real records, one
#: came out at 230 bpm on a track anybody would tap at 115. A road built
#: on that is a different game from the song - it runs at 21.7 units a
#: second where the others run at 12, lays its figures twice as thick,
#: and gives 0.78 s of warning where the rest give 1.5.
#:
#: The bounds are wide enough to leave every real reading in the batch
#: alone - 78, 128, 130, 137 and 155 all pass through - and only the
#: octave error moves.
TEMPO_LEAST = 70.0
TEMPO_MOST = 165.0


def folded_tempo(tempo: float) -> float:
    """The same pulse, counted the way a person would count it.

    Applied where the tempo and the beat phase are worked out together.
    Folding it in a scene and leaving the phase alone is worse than not
    folding it at all: the phase then belongs to a grid at the other
    tempo, and the correction that keeps the road's origin on the beat
    spends every frame pulling against it. Measured, that made the road
    run at twice the speed its own beat asked for.
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

    #: How many pixels this scene can afford to draw at their real size.
    #: Zero means "whatever the pane's own floor is". A scene raises it
    #: when most of its frame is a blit rather than a stroke, because the
    #: pane's floor is set for the ones that stroke curves and applying it
    #: to the others softens them for nothing.
    sharp_pixels = 0

    #: Whether a stretched buffer should be smoothed even when it goes up
    #: by a whole number of pixels.
    #:
    #: False suits a picture made of thin bright lines: smoothing spreads
    #: a one-pixel line over two and takes most of it away. It does not
    #: suit a picture made of arcs and lettering, where doubling every
    #: pixel is plainly doubling every pixel.
    stretch_smooth = False

    def paint(self, painter: QPainter, rect, state) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        """Forget everything and start again.

        There is one of each scene for the whole session - see SCENES -
        so a scene that keeps state keeps it between one track and the
        next and between one opening of the window and the next. The room
        in the rave scene came back a minute down the corridor with its
        lasers already running and its rings already in flight, which is
        "it just doesn't look like it xxxxxx xxxxx, xxxxxxxxxx xxxx x xxxx
        xxxx xxxxxx xxxx".

        A scene with nothing to forget does not need to say so. The ones
        that hold envelopes, positions or caches put themselves back to
        how they were built.
        """
        fresh = type(self)()
        for name, value in vars(fresh).items():
            setattr(self, name, value)

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
        back, which is what "xx xxxx xxxxx xxxx xxx xx xxxx xx xxxxxx
        xxxxxxxx" was.

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
              flash: float = None, going: float = 1.0) -> None:
        """The field. ``flash`` overrides the strobe this reads.

        ``going`` is how much of a frame's worth of movement to take:
        zero holds the field exactly where it is, for a scene whose track
        has stopped. The field has its own drift and would otherwise go
        on folding under a paused song.

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
        # Bounded, because these three accumulate: a level that arrives
        # as an infinity puts the drift beyond every sine in the frame
        # and it never comes back. See ``bounded``.
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
        mirrored = QPainterPath()
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
                        top = horizon - tall + row * spacing + 3
                        deep = max(2.0, spacing * 0.3)
                        windows.addRect(QRectF(x + block * 0.22, top,
                                               block * 0.2, deep))
                        # The same window in the floor. The towers were
                        # already reflected as solid blocks and the lit
                        # windows were not, so the reflection was a
                        # silhouette of a city whose lights were all out.
                        #
                        # Collected in the loop that is already running
                        # over them and filled once, like the windows
                        # themselves: building it anywhere else would walk
                        # every tower a second time, and several hundred
                        # rectangles a frame was most of what this scene
                        # used to cost.
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

    #: How far the floor squashes what it reflects, and how much of a
    #: window's light survives the trip. The same squash the towers'
    #: own reflection uses, so the lights sit on the blocks they came
    #: from rather than beside them.
    MIRROR = 0.5
    MIRROR_LIT = 0.38

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

    # -- dwell -------------------------------------------------------------
    #: What makes this a tube rather than a drawing of one.
    #:
    #: A beam deposits energy at a rate, so how bright a stretch of trace
    #: comes out depends on how long the beam spent there. Where the
    #: signal moves slowly - the turning points, the corners of a figure,
    #: anywhere the beam reverses - the phosphor is struck hard and glows
    #: white. Where it crosses the screen quickly it barely marks it. That
    #: single fact is most of what oscilloscope music looks like, and a
    #: trace stroked at one alpha is a line drawing whatever else is done
    #: to it.
    #:
    #: DWELL_STEP is the distance between two samples, in the figure's own
    #: unit box, at which the beam is at full brightness: about what a
    #: circle of radius one drawn with a thousand samples takes. Slower
    #: than that saturates, faster than that fades - down to DWELL_LEAST,
    #: because a fast stroke on a real tube is faint and not absent.
    #: Measured against the trace's own median step rather than against a
    #: fixed distance, which is a person turning the intensity up until
    #: the figure is right: a small figure and a big one are then both
    #: exposed properly and what shows is the shading *within* each,
    #: which is the part that carries the shape. Below one, so the median
    #: sits in the upper middle and there is room above it for the slow
    #: parts to blaze.
    DWELL_AIM = 0.62
    DWELL_LEAST = 0.22
    #: How many brightnesses the trace is cut into. Each is one stroke, so
    #: this is also what the beam costs: six is enough that the shading
    #: reads as continuous and few enough that a frame is six paths.
    DWELL_LEVELS = 6
    #: How many samples share one brightness.
    #:
    #: Six paths a frame is cheap; the number of *stretches* inside them
    #: is not. A figure written for a scope changes speed smoothly and
    #: gives long runs, but an ordinary stereo mix is noise, and taking a
    #: level per sample cut a thousand-point trace into eight hundred
    #: stretches - each one a subpath with two ends to cap. Measured, that
    #: was 154 ms a frame at full screen against 6 before.
    #:
    #: A block of eight caps it at a hundred and twenty-eight, and the
    #: shading loses nothing anybody can see: the beam has mass and the
    #: phosphor integrates, so brightness that changes every eighth of a
    #: sample was never real.
    DWELL_BLOCK = 8
    #: How far a parked beam is nudged so that it draws at all.
    #:
    #: A beam that stops moving is a stretch of trace with no length in
    #: it, and Qt strokes nothing for a subpath of exactly zero length -
    #: not even a round cap. Measured: a zero-length run painted 0 pixels
    #: and one a ten-thousandth of a unit long painted the dot. A parked
    #: beam is the brightest thing on a scope; it should not be the one
    #: thing missing from it.
    DWELL_PARKED = 1e-4

    #: How much wider the beam is drawn where it is brightest.
    #:
    #: A tube blooms in the glass as well as in the phosphor: drive the
    #: spot hard and it grows. Without this the shading is a change of
    #: colour on a line of constant thickness, which reads as a drawing
    #: shaded in rather than as a filament being run hotter.
    DWELL_SPREAD = (0.80, 1.55)
    #: How far the hottest parts wash out towards white. A phosphor struck
    #: hard stops being green and goes white in the middle, which is the
    #: other half of why a bright node looks bright.
    DWELL_WHITE = 0.34

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
        # A figure has detail in it that a fat beam fills in, so X-Y is
        # struck finer than a sweep.
        # In the tube's own pixels, so the beam ends up the same thickness
        # against the graticule however much the pane is shrinking the
        # frame it draws into.
        # A shade thicker than it was. A real trace is a glowing filament
        # rather than a pen line, and at 1.3 pixels the figures read as a
        # diagram of one.
        core = ((1.8 if drawing else 2.2) + flash * 1.2) * dpr
        for level, path in enumerate(self._beams(self._points(trace,
                                                              drawing))):
            if path.isEmpty():
                continue
            share = level / max(1, self.DWELL_LEVELS - 1)
            # Bright and white where the beam lingered, faint and green
            # where it hurried: a phosphor struck harder or softer rather
            # than a pen changed for another pen.
            colour = QColor.fromHsvF(
                max(0.0, 0.34 - flash * 0.08),
                max(0.0, (0.42 - flash * 0.3)
                    * (1.0 - share * self.DWELL_WHITE)),
                1.0,
                self.DWELL_LEAST + (1.0 - self.DWELL_LEAST) * share)
            thin, fat = self.DWELL_SPREAD
            # Through ``stroke``, which fakes a wide line with a stack of
            # hairlines. See HAIRLINE: Qt's raster engine falls off a
            # thirty-six fold cliff at exactly one pixel of pen, and a
            # trace is the worst thing to take over it - hundreds of
            # reversals, every one of them a join. Measured on the worst
            # trace in a real record at full screen, one wide pen over
            # this path was 880 ms and the hairline stack is 3.
            #
            # Widths are handed over in the painter's own units, because
            # that is what ``stroke`` scales; the beam is thought about
            # in real pixels, hence the divide.
            stroke(beam, path, colour,
                   core * (thin + (fat - thin) * share) / scale,
                   # Round-capped, because a beam that stops moving draws
                   # a stretch of zero length and a flat cap draws
                   # nothing at all for one - a parked beam is the
                   # brightest thing on a scope, not the one thing
                   # missing from it. Bevelled joins, because a trace
                   # made of a thousand short segments has a join at
                   # every one of them and a round join there is an arc
                   # nobody can see and everybody pays for.
                   cap=Qt.PenCapStyle.RoundCap,
                   join=Qt.PenJoinStyle.BevelJoin)

    # -- paths -------------------------------------------------------------
    def _points(self, trace, drawing: bool):
        """One trace, as points in a box that does not depend on the window.

        Built at unit scale so that resizing, and the strobe pumping the
        gain, are a transform rather than a rebuild.
        """
        return (self._vector_points(trace) if drawing
                else self._sweep_points(trace))

    def _beams(self, points):
        """One path per brightness, dimmest first.

        Per brightness rather than per stretch of trace. A stretch is a
        run of samples that happen to share a level, and on real music
        the level changes every few samples - so a path per run is a
        thousand paths a frame, while a path per level is four however
        noisy the signal is.

        Dimmest first, so the bright stretches are laid over the faint
        ones where they meet rather than under them.

        Each run carries the point before it as well, so consecutive runs
        share the sample between them and there is no gap where the
        brightness changes.
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
        # The seventieth of them rather than the middle one. A signal
        # that is parked for more than half the trace - silence, a held
        # note, the gap between two figures - has a median step of zero,
        # and a reference of zero puts every moving part of the trace at
        # the dimmest level there is. Taking a step from the part that is
        # actually moving exposes the movement properly and leaves the
        # parked beam where it belongs, which is blazing.
        ranked = sorted(steps)
        middle = ranked[min(len(ranked) - 1, int(len(ranked) * 0.70))]
        reach = max(1e-9, middle * self.DWELL_AIM)
        levels = []
        for start in range(0, len(steps), self.DWELL_BLOCK):
            # Energy per unit length: how long the beam spent here. Over
            # a block rather than a sample, because a single sample's
            # spacing on real music jitters enough to dither the shading
            # into noise - and see DWELL_BLOCK for what that dithering
            # costs to draw.
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
        """One stretch of the trace as a path, parked or moving.

        Always a curve, never a polyline. Drawing the fast stretches
        straight was tried on the grounds that a beam crossing the whole
        screen between two samples really does fly straight: it was
        slower, at every threshold, because the curve cuts the corners
        and there is less of it to stroke. 31 ms for the worst frame of a
        real record with the curve, 56 without.
        """
        spread = max(abs(run[-1].x() - run[0].x()),
                     abs(run[-1].y() - run[0].y()))
        if len(run) > 1 and spread < self.DWELL_PARKED:
            # The beam stopped. See DWELL_PARKED: a hair, so that there
            # is a subpath for the round cap to sit on.
            dot = QPainterPath()
            dot.moveTo(run[0])
            dot.lineTo(QPointF(run[0].x() + self.DWELL_PARKED, run[0].y()))
            return dot
        return smooth_path(run)

    def _vector_points(self, trace):
        """Left against right, plotted straight, in a unit box.

        No trigger and no clock: where the beam is, is what the record
        says. A disc cut for a scope draws a picture here; an ordinary mix
        draws the blob a vectorscope shows, leaning with the stereo image.

        The curve through them, rather than a line between them, is drawn
        by the caller: a beam is a physical thing with a mass of electrons
        in it and a deflection coil that cannot change direction
        instantly, so it rounds every corner it is asked to draw. Joining
        the samples with straight lines draws the corners the signal asks
        for and not the ones a scope makes, which is why the figures came
        out "straight and taking sharp turns".
        """
        count = len(trace) // 2
        # Stored as int16 so a long track's worth fits in memory.
        scale = 1.0 / 32768.0
        return [QPointF(trace[index * 2] * scale,
                        # Screen y grows downwards and a scope's does not.
                        -trace[index * 2 + 1] * scale)
                for index in range(count)]

    def _sweep_points(self, trace):
        """One sweep, swept around a circle rather than across.

        The beam starts at twelve o'clock and goes round once; how far
        the signal is from zero is how far the trace is from the ring.
        A steady tone draws a closed flower, and the trace joins up with
        itself because the capture is triggered on a zero crossing.

        Built with the zero ring at radius one, so the caller scales it to
        whatever the window is now.
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

    #: Smoothed when it is stretched, unlike the scenes made of lines.
    #: A dial is arcs and lettering, and doubling every pixel of those is
    #: plainly doubling every pixel: "XX xxxxxx xxxxx xxxxxxx xxx xxxxx
    #: xxxxxxxx pixelated in full screen".
    stretch_smooth = True

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
            # A wash over the whole frame first, so the lamps sit in lit
            # air rather than in the dark. It is also what puts back the
            # light the lamps near an edge had to give up.
            wash = QColor(state.dial_colour)
            wash.setAlphaF(min(1.0, 0.16 * flash))
            painter.fillRect(rect, wash)
            for box, _value, _label in boxes:
                self._backlight(painter, rect, box, state, flash)
        for box, value, label in boxes:
            self._meter(painter, box, value, label, state, flash, dpr)

    def _backlight(self, painter, rect, box, state, flash) -> None:
        """One meter's lamp coming up, over whatever is around it.

        The lamp finishes inside the frame. A radial gradient is drawn by
        filling a rectangle with it, and the rectangle is clipped to the
        picture - so a lamp whose reach ran past the edge was cut off
        while it was still bright, leaving a straight bright line down the
        side of the frame. In a window the strip is short and every meter
        is near an edge, which is "XX xxxxx xxxxxx xxxxxxx xxxx xx
        xxxxxxxx xxxx xx xxxx".

        So the reach is whatever fits. A lamp near an edge is a smaller
        lamp rather than a cut one, and the wash below puts the light it
        gave up back into the frame.
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
            # is what "X xxxx x xxxx xxxxxx, xxx xxxx xxx xx xxx xxxxxx
            # xxx" was about. Two or more is a short row; one is a stray.
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
        # Every row is a line across the whole plot, so the cost is the
        # number of rows times the number of bands - about twelve hundred
        # antialiased segments at full depth, which is more than a slow
        # machine can draw sixty times a second. On a big frame every
        # other row is dropped: the ridges are wider there anyway and the
        # landscape reads the same.
        # Every other row at most. A third of them left fifteen ridges
        # with gaps between, which reads as tangled lines rather than as
        # a surface - the saving was not worth what it cost to look at.
        # Every row, at every size.
        #
        # A big frame used to drop every other one, which is "xxxxxxxxx
        # xxxxx xxx xxxx xxxxx xx xxxxxxxxxx xxxxxxxx xx xxxxxxxx". It was
        # dropping them because the rows were expensive, and they are not
        # any more - see the note on the pen below. At full density the
        # scene costs 5.4 ms at 1512x982 against the 7.05 it cost at half
        # density before, so this is denser *and* cheaper.
        field = field[-self.DEPTH:]

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
            #
            # Over the rows there are, not over the rows there would have
            # been. A big frame draws every other one, so this divided 21
            # by 43 and the landscape stopped half way back: "xxxxxxxxx xx
            # xxxxxx xx xxx xxxxxxxx xxxx xxxxxxxx xx xxx xxxxxxxxxx
            # xxxx". It was not shorter in time, it was shorter on screen.
            # Spreading the rows it has over the whole plot costs nothing;
            # drawing twice as many of them cost 12.2 ms a frame against
            # 6.3, which is the other way this could have been fixed.
            back = (total - 1 - depth) / max(1, total - 1)
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
            # One pass a pixel wide, with the width carried as light.
            #
            # A width over one pixel is faked with a stack of hairlines,
            # and this scene lays down a couple of thousand curve segments
            # a frame: measured, 9.04 ms at 900x500 and 7.05 at 1512x982
            # against 3.92 and 2.72 drawn as single passes. That saving is
            # what pays for the density below.
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
    #: How much a full bass front-loads the travel within a beat, as a
    #: multiple of the average speed. At 0 the room moves evenly; at 1.6
    #: the first frame of a beat travels 2.6 times as far as the mean and
    #: the last barely moves.
    #:
    #: A multiple, because the curve is what the room's smoothness is.
    #: This was ``through ** (1 / (1 + bass * SURGE))``, whose slope at the
    #: start of a beat is not 2.6 times the mean, it is infinite: measured
    #: at 128 bpm, the worst frame travelled 1.02 rows against a median of
    #: 0.145, so the whole lunge happened in one frame and the rest of the
    #: beat crawled. With a bass that moves the way a tracked band does,
    #: two frames in eight seconds travelled *backwards*. That is "xxxx xx
    #: xxxxx x xxx xxxxxxx".
    #:
    #: ``1 - (1 - t) ** k`` front-loads the same way and its slope at the
    #: start is exactly k, so the lunge is as hard as it says and no
    #: harder.
    SURGE = 1.6

    #: How quickly the room notices that the track has stopped.
    GOING_EASE = 0.18

    #: How fast the push behind the room follows the bass. The curve above
    #: is chosen by it, so a value that jumps about changes where the room
    #: is rather than how fast it is going.
    PUSH_RISE, PUSH_FALL = 0.22, 0.045

    #: A ring is a big moment, not a snare.
    #:
    #: It used to fire on any snare over 0.75, which in most tracks is
    #: every other beat: a thing that happens twice a bar cannot signify
    #: anything, which is "I am not sure what's xxxxx xx xxxx xxx xxxxxx
    #: xxxx gets bigger". What fires one now is the room getting louder
    #: than it has been - the energy over the last breath against the
    #: energy over the last several seconds - which is what a drop, a
    #: chorus arriving or a break coming back in actually is.
    QUICK_RISE, QUICK_FALL = 0.40, 0.03
    CALM_RATE = 0.010
    #: How much louder than the last several seconds counts as a moment,
    #: how quiet the room can be and still have one, and how long before
    #: another can fire.
    RING_OVER = 1.30
    RING_QUIET = 0.04
    RING_WAIT = 0.45
    #: Seconds of listening before the first ring can fire. The slow
    #: average starts at nothing, so for the first moment of a track
    #: everything is louder than it has been and the room fired three
    #: rings before it had heard anything.
    RING_SETTLE = 1.5

    THUMP_RISE, THUMP_FALL = 0.34, 0.075
    CRACK_RISE, CRACK_FALL = 0.85, 0.22
    WASH_RISE, WASH_FALL = 0.30, 0.030
    FIZZ_RISE, FIZZ_FALL = 0.55, 0.16

    def __init__(self) -> None:
        self._z = 0.0
        self._spin = 0.0
        #: What the trusses are built from: see TRUSS_NEAR. Filled once a
        #: frame by ``_advance`` and read by ``_trusses``.
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
        #: The kit, smoothed: the kick pushing the room, the snare washing
        #: its colour, the hats shaking the thing in the middle.
        self._thump = 0.0
        self._crack = 0.0
        self._beat_was = None
        self._beat_count = 0.0
        self._wash = 0.0
        self._wash_hue = 0.0
        self._fizz = 0.0
        #: How loud the room is over the last breath, and over the last
        #: several seconds. A big moment is the first running away from
        #: the second - see ``RING_OVER``.
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
        # Nothing travels under a stopped track.
        #
        # The room's position is worked out from how far through the beat
        # the track is *and* from the push behind it, and the push went on
        # easing towards the last bass it saw after a pause - so the
        # corridor crept forward and jittered while nothing was playing.
        # Eased rather than switched, so that pausing is a stop rather
        # than a freeze-frame.
        self._going += ((1.0 if getattr(state, "moving", True) else 0.0)
                        - self._going) * self.GOING_EASE
        step *= self._going

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
        self._push = ease(self._push, bass, self.PUSH_RISE, self.PUSH_FALL)
        tempo = getattr(state, "tempo", 0.0)
        # What the trusses are built from. Kept once a frame rather than
        # read per truss, and cleared, because the answer for a given beat
        # moves as the playhead does.
        self._chart = getattr(state, "chart", None) or _NO_CHART
        self._said = getattr(state, "at", 0.0) or 0.0
        self._per_beat = 60.0 / tempo if tempo > 0.0 else 0.0
        self._beats_now = self._beats_done(state) if tempo > 0.0 else 0.0
        self._coming.clear()
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
            lunge = 1.0 + self._push * self.SURGE
            whole = math.floor(beats)
            through = beats - whole
            # The curve is frozen with the track. Recomputing it while
            # paused moves the room even though the beat has not, because
            # the push is still easing.
            if self._going > 0.02:
                self._lunge_held = lunge
            went = 1.0 - (1.0 - through) ** self._lunge_held
            # Never backwards. The curve is chosen by the push, so a push
            # that moves within a beat moves the whole mapping, and the
            # room can be asked to stand where it stood two frames ago.
            # Beats only ever go forwards, so neither does the room.
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
            # Seeded from the first frame rather than from nothing. A slow
            # average starting at zero means that for the first second of
            # any track everything is louder than it has been, and the
            # room fired a ring before it had heard anything.
            self._calm = loud
        self._calm += (loud - self._calm) * self.CALM_RATE
        # The loudest the room has been lately, which is what a drop is
        # measured against. From the eased level rather than the raw one,
        # so a single frame cannot set it.
        self._peak = max(self._quick, self._peak * self.PEAK_FALL)
        if self._quiet is None:
            # Seeded from the frame's own loudness, not from the eased
            # level, which starts at nothing and takes a second to arrive.
            # Seeded from that, the floor sat far below the room for the
            # whole of an intro and the rig read the intro as a drop.
            self._quiet = loud
        self._quiet += (self._quick - self._quiet) * (
            self.QUIET_DOWN if self._quick < self._quiet else self.QUIET_UP)
        self._ring_wait = max(0.0, self._ring_wait - step)
        self._heard_for += step
        return step

    def _big_moment(self) -> float:
        """How much of a moment this frame is, from 0 to 1.

        Zero unless the room has got louder than it has been, and zero for
        ``RING_WAIT`` seconds afterwards, so a long loud passage gives one
        ring at the start of it rather than one a frame.
        """
        if (self._ring_wait > 0.0 or (self._calm or 0.0) < self.RING_QUIET
                or self._heard_for < self.RING_SETTLE):
            return 0.0
        over = self._quick / max(1e-6, self._calm)
        if over < self.RING_OVER:
            return 0.0
        self._ring_wait = self.RING_WAIT
        # The moment becomes the new normal. Without this a drop fires
        # again every RING_WAIT for as long as it stays loud, because the
        # slow average takes several seconds to climb: measured on a
        # written arrangement, one drop sent three rings 0.45 apart.
        #
        # From the loudness itself rather than from the quick envelope,
        # which is still on its way up when the first ring goes: taking
        # the envelope left the bar low enough that the rest of the same
        # rise cleared it again half a second later.
        self._calm = max(self._calm, self._loud * 0.92)
        return max(0.35, min(1.0, (over - self.RING_OVER) * 1.4))

    def paint(self, painter, rect, state) -> None:
        step = self._advance(state)
        # The eased kick everywhere the room moves. The raw hats still
        # fire the beams, which are meant to be sudden; the snare no longer
        # fires anything directly - it moves the colour, in _advance.
        kick = self._thump
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
        self._rings_now(painter, rect, horizon, focal, self._big_moment(),
                        step, hue, flash)
        self._beams_now(painter, rect, horizon, focal, hats, step, hue,
                        bass, flash)
        self._core(painter, horizon, span, hue, bass, kick, synth, flash,
                   self._weight(rect))

    # -- the parts --------------------------------------------------------
    #: How big the haze is actually drawn before being stretched over the
    #: frame. A gradient has no detail in it, so nobody can tell - and a
    #: full-frame gradient is pure fill rate, which is the one thing a
    #: machine without a graphics card is worst at. Measured on a build
    #: runner, this scene cost four times what it costs here while the
    #: others cost twice; painting the haze small is most of that gap.
    HAZE = 128

    def _haze(self, painter, rect, horizon, bass, synth, flash) -> None:
        """The air in the room, lit from the far end."""
        small = self._haze_tile(rect, horizon, bass, synth, flash)
        if small is None:
            return
        # Smoothly, or the tile's own pixels show. It is 96 across and the
        # frame is up to 1920, so without this the air comes out in
        # twenty-pixel blocks - which nobody noticed while the tile was
        # one gradient with two stops and everybody would notice now.
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawImage(rect, small, QRectF(small.rect()))
        painter.restore()

    #: How far round the wheel the air's second colour sits from its
    #: first. A third: far enough that the two read as different lights
    #: rather than as one light being uneven, close enough that they are
    #: still the same room.
    HAZE_TURN = 0.46
    #: How far the wash reaches, as a share of the frame.
    HAZE_REACH = 1.9

    #: The tallest the air is laid out, as height over width. A frame
    #: taller than this gets the air of a frame this shape, stretched.
    #:
    #: "Xxxx xxxxx xxxxxxx xxx xxxxxxxxx xx xxxxxxxx xxxx xxx xxxxx xxxx
    #: xxxxxxxx xx xxxx xxxxxx." The pane in the window is a strip - 906
    #: by 270 points in a 1300 wide viewer - and in a strip the lamp at
    #: the end of the room lights the middle while the sides keep the
    #: wash: orange and magenta on one side, deep blue on the other, dark
    #: in the corners. Laid out for a 16:10 screen the same lamp covers
    #: most of the frame in one smooth gradient, and the variety goes.
    #:
    #: Two rounds were spent making up for that on a big frame - a lamp
    #: shrunk as the frame grew, and the bare air it left filled with
    #: extra light - and both were measured against a 640x360 window
    #: rather than against the strip anybody actually sees. The extra
    #: light is what washed the colour out: it carried the wash towards
    #: full brightness, and a colour carried towards white is a pastel.
    #: Measured on a real track at the same moment, windowed and full
    #: screen, before this: brightness 0.633 against 0.746, saturation
    #: 0.622 against 0.608. Across frames of the same synthetic music,
    #: the spread of hues in a full screen was 0.104 against the strip's
    #: 0.176.
    #:
    #: Air is soft, so it stretches without anything to show it has been
    #: stretched, and laid out as the strip's the full screen is the
    #: strip's picture: hue spread 0.170 against 0.176, colourfulness 84.8
    #: against 82.6, saturation of the lit 0.605 against 0.583, brightness
    #: 0.541 against 0.539. The lamp becomes a column of light down the
    #: middle, which is what a lamp at the end of a room full of haze is.
    HAZE_TALLEST = 0.3

    def _haze_tile(self, rect, horizon, bass, synth, flash):
        """The air in the room, painted small and stretched.

        It was one radial gradient with two stops: a colour at the
        vanishing point fading to nothing. That is a glow, and a glow is
        not the same thing as air - a room lit by a rig has more than one
        lamp in it, and what makes it look like air rather than a smudge
        is that the colours disagree with each other from place to place.

        Three passes now, all in the same cached image, all free:

          a deep vertical wash   the floor warmer than the ceiling, which
                                 is what an actual room does - the light
                                 lands on the floor
          a wide second colour   a third of the way round the wheel from
                                 the first, off to one side of the
                                 vanishing point, so the two mix across
                                 the middle distance
          the hot core           four stops rather than two, opening
                                 almost white at the very centre

        The snare moves the hue (see ``_advance``) and both colours move
        with it, a third apart, so a change of colour is a change of
        *light* rather than a tint over the top.

        Rebuilt when what it looks like changes enough to see, which on
        moving music is most frames: the key is quantised to a hundredth
        and a tracked band does not sit that still, so the cache turns out
        to earn its keep mainly while the music is quiet - 540 rebuilds in
        600 frames on a bass envelope that actually moves. It does not
        matter, which is worth knowing before anyone tries to fix it: a
        rebuild is 59 us against a 7.3 ms frame at 1920x1080, because the
        tile is 128 across however big the frame is. The saving was never
        the caching, it was painting the air small.
        """
        if rect.width() < 2 or rect.height() < 2:
            return None
        hue = (0.62 + synth * 0.3) % 1.0
        key = (round(hue, 2),
               # Brightness, over a narrower range than it had.
               #
               # It used to run from 0.34 to 1.24 and clamp: quiet
               # passages were nearly black, where a colour cannot show,
               # and loud ones sat against the ceiling, where every colour
               # goes to white. Both ends read as grey, which is how the
               # same room could be "mostly grey and not that coloured"
               # and "blinding at some points" at once. The floor is
               # higher and the ceiling lower, and what moves with the
               # music now is mostly the *depth* of the colour.
               round(min(1.0, 0.38 + bass * 0.24 + flash * 0.13), 2),
               round(min(1.0, 0.52 + bass * 0.26), 2),
               round(0.58 + bass * 0.35, 2),
               round((horizon.x() - rect.left()) / rect.width(), 2),
               round((horizon.y() - rect.top()) / rect.height(), 2),
               round(min(1.0, 0.30 + bass * 0.34 + flash * 0.25), 2),
               # How deep the colour runs. The bass is what makes the room
               # vivid: quiet passages are muted and a bass hit floods
               # them, which is the one thing a smooth wash of light can
               # do that reads as loud without simply being brighter.
               round(min(1.30, 0.80 + bass * 0.50), 2))
        if self._haze_key == key and self._haze_image is not None:
            return self._haze_image
        (shade, value, alpha, spread, across, down, second, deep) = key

        def rich(base: float) -> float:
            """A saturation, taken as deep as the bass asks."""
            return max(0.0, min(1.0, base * deep))
        # Never taller than a strip. See HAZE_TALLEST.
        size = QSize(self.HAZE, max(2, int(self.HAZE * min(
            rect.height() / max(1.0, rect.width()),
            self.HAZE_TALLEST))))
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        wide, tall = size.width(), size.height()
        middle = QPointF(across * wide, down * tall)
        box = QRectF(0, 0, wide, tall)
        other = (shade + self.HAZE_TURN) % 1.0
        into = QPainter(image)
        try:
            into.setPen(Qt.PenStyle.NoPen)

            # The room's own light: dim at the ceiling, warmer at the
            # floor, which is what an actual room does - the light lands
            # on the floor.
            wash = QLinearGradient(0.0, 0.0, 0.0, tall)
            wash.setColorAt(0.0, QColor.fromHsvF(other, rich(0.92),
                                                 value * 0.52,
                                                 alpha * 0.80))
            wash.setColorAt(down, QColor.fromHsvF(shade, rich(0.80),
                                                  value * 0.34,
                                                  alpha * 0.34))
            wash.setColorAt(1.0, QColor.fromHsvF((shade + 0.12) % 1.0,
                                                 rich(0.88),
                                                 value * 0.86,
                                                 alpha * 0.86))
            into.setBrush(wash)
            into.drawRect(box)

            # A second lamp, off to one side, in the other colour.
            away = QRadialGradient(
                QPointF(middle.x() - wide * 0.22, middle.y() + tall * 0.10),
                max(1.0, min(wide, tall) * spread * self.HAZE_REACH))
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
            glow = QRadialGradient(middle,
                                   max(1.0, min(wide, tall) * spread * 2.0))
            # The very centre is the lamp itself, so it is the one place
            # allowed to be nearly white - and even there the bass pulls
            # colour back into it.
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

    #: How many depth bands the grid is drawn in. Each is one stroke with
    #: its own weight, so the corridor dissolves into the haze instead of
    #: stopping dead at the far end. Four is where it stops being visible
    #: as banding and starts reading as distance.
    BANDS = 4
    #: Every this many rows, a frame around the corridor. They are what
    #: gives the room a length: a grid alone is a floor and a ceiling, and
    #: a truss every few metres is a building.
    TRUSS = 5

    #: What a truss is made of: the beat it belongs to.
    #:
    #: One truss passes you every beat - that is what ``_advance`` fixes
    #: the distance per beat for - so a truss five slots away is the beat
    #: five beats from now, and the chart the analysis found already says
    #: what is on it. "Make rave obstacles react to music as well": these
    #: are the only things in the room with a length, and until now every
    #: one of them was the same size whatever the track did. A kick swells
    #: the frame it lands on and a snare turns its colour, so the shape of
    #: the corridor ahead of you is the shape of the bar coming.
    #:
    #: How far a hit may be from the beat and still belong to it, as a
    #: share of a beat; and how much a kick swells the frame.
    TRUSS_NEAR = 0.40
    TRUSS_SWELL = 0.22
    TRUSS_TURN = 0.10

    #: Lines across a side wall. Far fewer than the floor gets, because
    #: the corridor is about eight times wider than it is tall: laid out
    #: with the floor's count they came out a twentieth of a unit apart
    #: and read as hatching rather than as a grid.
    UPRIGHTS = 3

    #: How far the floor and the ceiling are from the eye, and how much
    #: further a bass note pushes them. The corridor opening up is most of
    #: what makes the room feel big.
    #:
    #: One definition, because the beams have to land on the floor the
    #: grid drew rather than somewhere near it.
    LIFT_AT_REST = 0.55
    LIFT_ON_BASS = 0.22

    @classmethod
    def _lift(cls, bass: float) -> float:
        return cls.LIFT_AT_REST + bass * cls.LIFT_ON_BASS

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
        lift = self._lift(bass)
        span = self.ACROSS * 0.5
        # Counted down, not up. See ``_trusses``: with the offset rising,
        # every row's z rises with it and the whole room travels *away*
        # from you between one wrap and the next.
        offset = 1.0 - (self._z % 1.0)
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
            self._beam(painter, away,
                       self._shade(base, min(1.0, (0.20 + bass * 0.30
                                             + kick * 0.26
                                             + flash * 0.26) * glow)))

            # And the ones across it, marching towards you, in bands.
            for band in range(self.BANDS):
                path = QPainterPath()
                # A slice of the corridor, not every fourth row of it.
                #
                # This said range(band, DEPTH, BANDS), which walks the
                # whole corridor taking every fourth row - so the four
                # "depth bands" were four interleaved sets spread from
                # your feet to the vanishing point, and dimming one dimmed
                # a quarter of the lines everywhere rather than the far
                # ones. It read as a faint texture and not as distance,
                # and the test that was supposed to catch it could not,
                # because the two came out within a tenth of a per cent of
                # each other.
                first = band * self.DEPTH // self.BANDS
                last = (band + 1) * self.DEPTH // self.BANDS
                for row in range(first, last):
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
                self._beam(painter, path,
                           self._shade(base,
                                       min(1.0, (0.10 + bass * 0.26
                                                 + kick * 0.24
                                                 + flash * 0.24) * glow
                                           * (0.14 + near * near))))

        self._trusses(painter, horizon, focal, hue, lift, span, bass, kick,
                      flash, reach, weight, glow)

    #: The frame this scene's line weights were chosen against. A line
    #: thicker than a real pixel is drawn by ``stroke`` as a stack of
    #: hairlines, so making them grow with the frame costs nothing.
    DRAWN_FOR = 700.0

    #: What the line width used to carry, as light instead.
    BEAM_LIFT = 1.6

    #: The same for the trusses, which were the last thing drawn with a
    #: real width. A line of width w at alpha a lays down about w times a
    #: of ink, so the alpha is multiplied by the width it would have had.
    #: ``_beam`` then applies BEAM_LIFT on top, so this takes it back out.
    TRUSS_LIFT = 1.0 / BEAM_LIFT

    #: How much of the contrast a big frame gets back as light.
    #:
    #: All of it, now. The grid is drawn with one-pixel lines and nothing
    #: else - see ``_beam`` - so brightness is the only knob left, and it
    #: is the one that was always free.
    LIFT = 1.15

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
        That is "the rave scene doesn't xxxx xx xxxx xxxxxxxx xx xxxx
        xxxxxx xxxx, xx xxxxxxxx xxxxx xxxxxx xx xxxxxxxx".
        """
        return max(0.75, min(1.30, rect.height() / cls.DRAWN_FOR))

    @staticmethod
    def _shade(base, alpha: float):
        colour = QColor(base)
        colour.setAlphaF(max(0.0, min(1.0, alpha)))
        return colour

    @staticmethod
    def _beam(painter, path, colour) -> None:
        """One line of the room, one pixel wide, one pass.

        This scene is a corridor made of about six hundred line segments,
        and it was drawing every one of them as a stack of hairlines to
        fake a wide antialiased pen. Measured at 1080p, the whole scene:

            stacked hairlines      32.3 ms a frame
            one hairline a line     7.4 ms
            one real wide pen      96.4 ms

        Four and a half times the cost of the thing it is imitating, for a
        room whose lines are *supposed* to be beams. A laser grid is not a
        painted one: its lines are as thin as they can be and what makes
        them read is how bright they are and how many there are. So the
        weight is carried entirely by alpha now, which costs nothing, and
        the scene went from the most expensive of the eight to cheaper
        than the middle one.

        The trusses and the thing in the middle still go through
        ``stroke``: there are a dozen of them against six hundred, and
        they are the two things in the room meant to look solid.

        BEAM_LIFT is what the width used to carry. Measured against the
        same scene drawn with stacked wide lines, 1.6 puts the contrast
        and the brightest tenth of the picture back where they were - 24
        of spread against 22, and a 95th percentile of 89 against 95 -
        and it is free.
        """
        lit = QColor(colour)
        lit.setAlphaF(min(1.0, lit.alphaF() * Rave.BEAM_LIFT))
        pen = QPen(lit, 0.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawPath(path)

    def _on_beat(self, index: int) -> dict:
        """What the kit plays on the beat a truss belongs to.

        The chart is every hit in the track by name, from the same
        element detection the strobe uses, so this can read *forward*:
        the truss five slots down the room is the beat five beats from
        now, and what is on it is known before it arrives.

        Cached per frame, because six trusses ask and the answer for a
        beat does not change within one.
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
        """A frame round the corridor every few metres, coming at you.

        The thing the room was missing: a grid tells you where the floor
        is and a truss tells you how far down the room you are looking.
        They brighten on the kick with everything else, and the nearest
        one is much the brightest, so the eye has something travelling
        rather than a field of lines that all move together.
        """
        # The beat the nearest truss belongs to. One passes you every
        # beat, so the one k slots away is k beats from now.
        first = math.ceil(self._z / self.TRUSS) if self._per_beat > 0.0 else 0
        # On their own clock, not the grid's.
        #
        # They used to ride the grid's offset, which wraps every *row*:
        # so a truss crept back one row's worth and then jumped forward
        # five to where the next one had been, sixty times a minute. That
        # is what stopped it reading as a continuous walk forward - the
        # only things in the room with a length to them stuttered, and
        # they did it whether anything was playing or not.
        #
        # And it counts *down*. With the offset rising, a row's z rises
        # with it: measured over one beat at 128 bpm, the nearest truss
        # went from z 2.78 out to 3.33 and then snapped back to 0.65 - the
        # room crawling backwards and jumping forwards once a beat, which
        # is the opposite of everything written above and is most of what
        # "rave acts weird and glitchy" was. Counting the offset down runs
        # the wrap the other way: the nearest truss closes on you through
        # the beat and the next one takes its place.
        offset = self.TRUSS - (self._z % self.TRUSS)
        for step in range(0, self.DEPTH, self.TRUSS):
            row = step + offset
            if row >= self.DEPTH:
                continue
            z = self.NEAR + row * reach / self.DEPTH
            near = max(0.0, 1.0 - (z - self.NEAR) / reach)
            # What is on this one's beat. See TRUSS_NEAR: the frame swells
            # on a kick and turns colour on a snare, so the corridor ahead
            # of you has the shape of the bar coming.
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
            # Hairlines, with the width carried as light, which is what
            # the rest of the room already does - see ``_beam``.
            #
            # These were the last thing here drawn with a real width, on
            # the grounds that a truss is meant to look solid. Five
            # rectangles were costing 1.72 ms a frame at 1512x982 against
            # 0.41 drawn as single passes, because a width over one pixel
            # is faked with a stack of hairlines and the stack is the
            # whole cost.
            #
            # Brightness cannot buy the width back: the near ones are
            # already at full alpha, and what a wide line has that a thin
            # one does not is area. Measured, the brightest twentieth of
            # the frame fell from 0.784 to 0.643 at 1512x982 with all five
            # on hairlines, and no amount of lift moved it. So the nearest
            # one keeps its width and the four behind it do not.
            thick = ((0.9 + near * 2.2) * (1.0 + kick * 1.1) * weight
                     * (1.0 + coming.get("Hats", 0.0) * 0.35))
            alpha = ((0.16 + bass * 0.22 + kick * 0.34 + flash * 0.3)
                     * glow * (0.30 + near * near * 1.4)
                     * (1.0 + coming.get("Kick", 0.0) * 0.5))
            if step == 0:
                # The nearest one keeps its width. It is the one the eye
                # is on, it is the only one wide enough for the width to
                # show, and one of them costs about a third of a
                # millisecond where five cost 1.7.
                stroke(painter, path, self._shade(base, min(1.0, alpha)),
                       thick)
            else:
                self._beam(painter, path,
                           self._shade(base, min(1.0, alpha * thick
                                                 * self.TRUSS_LIFT)))

    #: How fast a ring closes on you, as a share of its own distance a
    #: second, and how near it gets before it is done with.
    #:
    #: A share of its distance, not a fixed speed. A ring's *apparent*
    #: size goes as one over its distance, so moving it at a steady speed
    #: through the room means it sits far away looking tiny for a second
    #: and then does the whole of its expansion in the last tenth of one.
    #: That is why "I don't xxx xxx xxx xxxxxx xxxxxx xxxxx xx xxx xxxxxxx
    #: xxxx xxxxx xxxx xx xx" - it was there, and it was over before you
    #: could see it. Closing by a share of the distance each frame makes
    #: the growth even, and the ring spends most of its life at a size
    #: worth looking at: 0.8 of a second inside the frame rather than 0.15.
    RING_CLOSE = 2.1
    RING_GONE = 0.34

    #: How many rings a moment sends, and how far apart they start. A
    #: single outline was hard to read as anything; three, staggered down
    #: the room, arrive as one shape with a depth to it.
    RING_ECHOES = 3
    RING_APART = 1.7

    def _rings_now(self, painter, rect, horizon, focal, moment, step, hue,
                   flash):
        """Rings from a big moment, leaving the far end and sweeping past.

        See ``RING_OVER`` for what a moment is. This used to fire on any
        snare over 0.75.
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
            # Past the eye rather than stopped at the near wall, so the
            # last thing it does is sweep out through the edges of the
            # frame instead of being taken away at its biggest.
            if ring[0] <= self.RING_GONE:
                continue
            alive.append(ring)
            z, force = ring
            fade = max(0.0, min(1.0, (z - self.NEAR) / (self.FAR - self.NEAR)))
            radius = focal * (1.9 * force + 0.6) / z
            # Brightest in the middle of its travel and fading again as it
            # goes by, so it arrives out of the distance and leaves
            # through the walls rather than blinking out at full strength.
            going = max(0.0, min(1.0, (z - self.RING_GONE) / 0.9))
            lit = min(1.0, (1.0 - fade) * 1.15 * force * going)
            wide = (1.2 + (1.0 - fade) * 5.5 + flash * 2.0) * weight
            # Three passes, cheapest first: a wide soft one under a narrow
            # bright one, and a thin line just inside the rim. One outline
            # of one width reads as a circle drawn on the picture; this
            # reads as something with an edge that is lit.
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

    #: The hat beams. Where the lamps hang across the ceiling, how far
    #: down the room a beam is thrown, how many rows back it starts, and
    #: how quickly one fades.
    #: The laser rig.
    #:
    #: One lamp either side, hung on the ceiling deep down the room, each
    #: throwing a fan of beams onto the floor in front of you. The fan
    #: sweeps back and forth rather than firing one beam at a time.
    #:
    #: A beam used to be one line per hat, thrown a couple of metres. Two
    #: lines appearing and going out again at whatever the hats were doing
    #: read as "random lines" however carefully they were placed: nothing
    #: connected one to the next, and nothing in the room made a shape out
    #: of them. A fan does. Nine beams from one point, all of them moving
    #: together, is a thing somebody aimed.
    #:
    #: Long, too, and that is a matter of where the ends are rather than
    #: how far apart they are in the room. A beam from z 8.5 to z 6.5
    #: crosses two metres of room and draws 151 pixels, because both of
    #: its ends are far away and perspective shrinks them together. The
    #: lamps stay deep, at 8.5, where they sit near the vanishing point
    #: and the fan opens towards you; the feet land between 0.7 and 2.5,
    #: right in front of the eye, where perspective makes them large.
    #: Measured across a sweep: 340 pixels of beam against 151, and
    #: against 320 for the one line a hat used to throw.
    #:
    #: The feet stay on the floor. Reaching past the walls measured much
    #: longer again, 604 pixels, and a beam that ends outside the room is
    #: the floating line this was meant to stop being.
    FAN = 11
    FAN_HANG = 0.78
    FAN_AT = 8.5
    FAN_NEAR, FAN_FAR = 0.7, 2.5
    FAN_OPEN = 0.95
    FAN_REACH = 1.0
    #: Sweeps a second at rest, and how much the hats hurry it.
    FAN_SWEEP = 0.55
    FAN_HURRY = 1.8
    #: How dim a beam is at the lamp against the end coming at you.
    #:
    #: Drawn as a gradient along the beam rather than in two pieces. Two
    #: pieces is a step, and a step at 45 per cent of the way along is
    #: exactly "it xxxxx xxxx xxx xxxxxx xxx xxxxxxxx xxxxxxx xxxxxxx xxx
    #: xxxx" - which they did, from 0.45 to 1.0 in one pixel. Measured at
    #: 1512x982 with 22 beams, a gradient pen costs 2.54 ms against 2.26
    #: for the two-piece version and 2.38 for a four-piece one, so the
    #: smooth one is worth its 0.28 ms.
    FAN_FADE = 0.4
    #: How much a strobe hit adds to the rig, and how much of its colour
    #: it takes away. A flash is white.
    FAN_STROBE = 0.75
    FAN_BLEACH = 0.45
    #: Below this there is no rig at all, so a quiet passage has none.
    FAN_FAINT = 0.03
    #: Seconds of listening before the rig can come on, so that the first
    #: sound of a track is not read as the loudest it has ever been.
    FAN_SETTLE = 2.0

    #: How the rig decides a drop is happening.
    #:
    #: Not the question the rings ask. A ring marks the moment the room
    #: gets louder, and the ratio it reads dies about two seconds into a
    #: drop as the slow average catches up with it. Measured over a
    #: written dubstep arrangement, that ratio came out at 0.16 through
    #: the drop against 0.15 through the intro before it, which is no
    #: difference at all - and a rig that goes out two seconds into the
    #: drop is worse than one that never came on.
    #:
    #: A drop is a level, not a change. What the rig reads is where the
    #: room sits between the quiet it keeps coming back to and the loudest
    #: it has been: ``(now - quiet) / (loudest - quiet)``. That holds at 1
    #: for as long as the drop lasts, falls to nothing in the breakdown,
    #: and is 0 through an intro, an intro being the quiet. See QUIET_DOWN
    #: for what "the quiet" is and why it is not an average.
    #:
    #: The loudest decays slowly, so the rig still knows about the first
    #: drop a minute later. A track with no dynamics in it has no drop,
    #: and the span guard leaves the rig off rather than on for ever.
    PEAK_FALL = 0.9996
    PEAK_SPAN = 0.08

    #: The quiet the track keeps coming back to.
    #:
    #: Not ``_calm``, which the rings use. That is an average over about a
    #: second and three quarters, so four seconds into a drop it has risen
    #: to 0.685 against the drop's own 0.702 and there is no span left to
    #: measure anything in. An average of a loud passage is loud.
    #:
    #: A floor is not an average. It follows the room down quickly and
    #: climbs back slowly, so it stays near the verse for the length of a
    #: drop and is back where it belongs a second into the breakdown.
    QUIET_DOWN = 0.02
    QUIET_UP = 0.0004

    def _lasers_lit(self) -> float:
        """How hard the rig is running, from 0 to 1.

        Held up for as long as the passage is loud, rather than fired by
        each hat. A drop is not an event, it is a minute. See PEAK_FALL
        for what is being read and why it is not what the rings read.

        The hats add to it, scaled by the level, so a roll inside a drop
        shows and a roll in the intro does not bring the rig on.
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
        """The laser rig: two fans sweeping across the floor.

        One line per hat is what this was, and two lines appearing and
        going out again read as "random lines" however carefully each one
        was placed. Nothing connected one to the next. A fan does: nine
        beams leaving one point together, sweeping together, is a thing
        somebody aimed.

        Each beam runs from a lamp deep down the room to a foot on the
        floor in front of you, so it crosses most of the room rather than
        a fifteenth of it, and the perspective has a length to work on.
        Each is drawn in two parts, brighter at the lamp end, which is
        what a beam in haze does.

        See ``_lasers_lit``: the rig follows how loud the passage is, so a
        dubstep drop has it running for the whole drop.
        """
        lit = self._lasers_lit()
        if lit + flash * self.FAN_STROBE < self.FAN_FAINT:
            return
        lift = self._lift(bass)
        span = self.ACROSS * 0.5
        self._fan += step * (self.FAN_SWEEP + self._fizz * self.FAN_HURRY)
        # Back and forth, the way a rig sweeps, rather than round and
        # round: a fan that spins has no front.
        phase = math.sin(self._fan) * 0.8

        pairs = []
        for side in (-1.0, 1.0):
            lamp = self._project(horizon, focal,
                                 side * self.FAN_HANG * span, -lift,
                                 self.FAN_AT)
            for index in range(self.FAN):
                spread = (index / (self.FAN - 1.0)) * 2.0 - 1.0
                angle = phase + spread * self.FAN_OPEN
                # Mirrored exactly, so the two fans are one rig rather
                # than two that happen to be near each other.
                foot = self._project(
                    horizon, focal,
                    side * math.sin(angle) * span * self.FAN_REACH, lift,
                    self.FAN_NEAR + (math.cos(angle) * 0.5 + 0.5)
                    * (self.FAN_FAR - self.FAN_NEAR))
                pairs.append((lamp, foot))

        # A strobe hit takes the whole rig with it, on top of whatever the
        # music already has it doing, and bleaches it towards white.
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


#: One empty chart, shared. See Rider._lay.
_NO_CHART: dict = {}


class Rider(Scene):
    """A game you play on the track the music builds.

    Three lanes down a road that climbs, dives and twists with the song.
    Blocks sit on the road on the beat; you move between lanes with the
    arrow keys and try not to hit them.

    What makes it a rhythm game rather than a scene with keys is that the
    chart is laid out *ahead* of the playhead. ``state.kit`` says what is
    happening now, which is too late to put a block in front of somebody:
    it has to leave the horizon seconds before its beat so that it arrives
    on it. ``state.chart`` carries every hit in the track by name, from the
    same element detection the strobe uses, so this can read forward.

    The first version of this put a block on every hit it found, which on a
    house track is two kicks a second with hats between them: "xxxx xxx
    xxxxxxx xxx xxx xxxx xxx it's unplayable". Hits are now a *candidate*
    list, and the chart takes from it at a pace somebody can play - see
    ``GAP`` - choosing the shape from whichever drum won the slot.
    """

    name = "Music rider"
    #: The two games Audiosurf plays on the same road.
    #:
    #: Mono is the road: colours are points on a chain and greys are
    #: hazards, and the grid is only somewhere for them to go. Puzzle is
    #: the grid: a colour is worth nothing until three of them touch, and
    #: what the road hands you is a supply problem.
    MODES = ("Mono", "Ninja", "Wakeboard", "Puzzle")
    blurb = "a game: three lanes, and the track is the song"

    # -- the road ---------------------------------------------------------
    LANES = 3
    LANE_WIDE = 1.30
    #: The near and far ends of the road. FAR was 34, which converges to
    #: a sliver: two thirds of the road was a few pixels tall and the
    #: whole thing read as a cone rather than as a road.
    #: NEAR is behind the rider, not level with them, so that the road
    #: runs off the bottom of the frame rather than stopping short of it
    #: with a hard edge across the picture.
    #:
    #: It has to be behind, because the eye rides the road (see ``_eye``)
    #: and the road under the near edge is not the road under the rider.
    #: Swept over every phase of the hill, the bend and the roll, at five
    #: frame sizes and both ends of the bank: from level with the rider
    #: the near edge climbs up to 489 px into the picture.
    #:
    #: 2.4 rather than the 1.6 that was enough before. The road banks
    #: into its own turn now - see ``_road`` - so the roll is largest
    #: where the turn is, which is not where the old free-running roll
    #: put it: at 1.6 the near edge came 158 px into a 1512x982 frame.
    #: Swept again at the same five sizes, 2.4 keeps it 108 px or more
    #: below the bottom of every one of them, and it is the best of them
    #: - further back than that and the corner starts coming round again.
    NEAR, FAR = -2.4, 20.0
    #: How far in front of the eye the road starts, and the z nearer
    #: than which nothing is projected at all.
    #:
    #: 0.22 rather than a third. The road banks into its own turn, which
    #: tips its near edge: one corner goes a long way down and the other
    #: not nearly as far, and the camera's own lean then swings the high
    #: one back up. At a third of a unit that corner came 50 px into a
    #: 640x360 frame. Starting the road nearer to the eye pushes both
    #: corners further down in proportion, which is the only lever here
    #: that does not cost the bank.
    NEAR_EYE = 0.22
    #: Cross-pieces down the road. The road is filled between them, so
    #: this is also how smooth its bends look.
    RUNGS = 44

    #: Where the eye sits above the road and how far back from the rider.
    #:
    #: High and back, which is the whole difference between seeing what is
    #: coming and not: "xxx xxxxxxx xxxxxx xxxxx xxxx xxxx xx xxxx xx xxx
    #: what's xxxxxx xx xx xxxx xxxxxx xxx xxxxxx". From 0.55 up and level
    #: with the rider to 2.1 up and 2.4 behind, the road ahead goes from a
    #: thin band across the middle of the frame to most of the picture.
    EYE_UP = 2.1
    EYE_BACK = 2.4

    #: The longest a track can be, in seconds - a day of music.
    #:
    #: Not a limit, a sanity check on the playhead. Everything here is
    #: measured *from* the playhead, and an infinite one is worse than a
    #: large one: the origin is set from it too, so the road's position
    #: comes out as infinity minus infinity, which is a nan, and the
    #: first thing that asks which beat it is in raises out of paint.
    LONGEST = 86400.0

    #: Where the rider sits along the road, and how fast it slides lanes.
    RIDER_AT = 3.0
    #: How fast the rider slides to a new lane, as a share of the way
    #: there each frame.
    #:
    #: The blueprint asks for "an incredibly tight interpolation window,
    #: roughly 50ms-70ms". At 0.30 a lane change was nine tenths done
    #: after 140 ms, which at twelve units of road a second is two units
    #: of ground spent arriving. 0.55 puts it at 50 ms, and the
    #: difference is whether a dodge you begin on the beat lands on it.
    #:
    #: A share of the way there per sixtieth of a second, not per frame.
    #: See ``_slide``.
    SNAP = 0.55

    # -- pace -------------------------------------------------------------
    #: Seconds from the horizon to the rider. This is the reaction time the
    #: game gives you and it is the number that decides whether it can be
    #: played at all.
    #: How far ahead the chart is read, in seconds. It has to cover
    #: LOOK_BEATS of them at the slowest tempo anybody plays: three beats
    #: at 60 bpm is three seconds.
    READ = 5.0
    #: The least time between one figure and the next, in seconds and in
    #: beats, whichever is longer.
    #:
    #: In beats as well as seconds, because a gap in seconds is a
    #: different musical distance at every tempo, and a figure that lands
    #: between beats is a figure that feels wrong however far apart they
    #: are. Two beats is one every 0.94 s at 128 bpm and one every 0.69 at
    #: 175, which is drum and bass keeping its feet.
    GAP = 0.80
    GAP_BEATS = 2.0
    #: And how that stretches and tightens with the energy of the
    #: passage: three beats apart where nothing is happening, one and a
    #: half where everything is. GAP_BEATS is the middle of it and is
    #: what the pool of shapes is still indexed by, so the shapes cycle
    #: the same way however thick the figures come.
    GAP_LEAST = 1.5
    GAP_MOST = 3.0

    def _apart(self, when: float) -> float:
        """How many beats apart the figures are at this point in the
        track. See GAP_LEAST."""
        if not self._energy:
            return self.GAP_BEATS
        energy = max(0.0, min(1.0, self._read(self._energy, when)))
        return self.GAP_MOST - (self.GAP_MOST - self.GAP_LEAST) * energy
    #: How far a heavier drum may be from the first candidate and still
    #: take its place, in beats and in seconds when there is no tempo.
    #:
    #: Six tenths of a beat, because that is what it takes to reach the
    #: kick on the next beat from a hat on the half. At a sixth of a
    #: second it could not: the first figure of the track landed on a hat
    #: at the half-beat, the gap put the next one a hat later, and the
    #: whole chart ran along the off-beat. Measured, the median figure sat
    #: 234 ms from a beat, which is exactly half of one.
    PREFER_BEATS = 0.6
    PREFER = 0.17
    #: How much short of the gap still counts as far enough.
    #:
    #: The gap is a length of time and the beats are a grid, and the two
    #: do not divide: at 90 bpm two beats is 1.333 s while GAP asks for
    #: 0.80, so the gap used is 1.333 and the kick that lands exactly
    #: there is short of it by a floating-point hair. Rejecting it costs
    #: the whole slot, because the next candidate is a beat later and the
    #: one after that. Measured over thirty seconds, 26 figures with this
    #: and 18 without at 90 bpm, 40 against 33 at 140.
    #:
    #: It is not what keeps the chart on the beat. That is PREFER_BEATS.
    SLACK = 0.03
    #: How far apart the three blocks of one run are. Inside a figure, not
    #: between figures.
    RUN_GAP = 0.16

    # -- one clock --------------------------------------------------------
    #: How far the road travels in a beat, and how many beats of it lie
    #: between the horizon and the rider.
    #:
    #: The whole world runs off this. It used to run off two clocks: the
    #: ground and the pillars moved at ``RUN + bass * RUN_BASS`` road
    #: units a second, which is 6 to 23, while a block's distance was
    #: worked out from its *time* and came to a flat 6.5 whatever the
    #: track was doing. So the road slid under the blocks and the
    #: streetlights overtook them - "xxx xxxxxxxxxxxx xxxx xxxxxx xxxx xxx
    #: obstacles xx xxx xxxx, xxx xxxx xxxxxxx xxxxx xxxxxx xxxx xxx
    #: obstacles, this does not feel right".
    #:
    #: One clock instead: the road's position is a function of the beat,
    #: so a block laid on beat n sits at n * PER_BEAT and is level with
    #: the rider exactly when the road reaches it. Everything moves
    #: together because there is only one thing moving.
    #:
    #: Three beats of look-ahead rather than the 2.6 seconds it was. In
    #: beats, because a length of time is a different musical distance at
    #: every tempo; and three because that is a bar's worth of warning at
    #: four to the floor and it puts the road at 12 units a second at 128
    #: bpm against the 6.5 the blocks used to manage.
    LOOK_BEATS = 3.0
    PER_BEAT = (FAR - RIDER_AT) / LOOK_BEATS

    #: Road units a second when no tempo has been found. About what
    #: PER_BEAT comes to at an ordinary tempo, so a track the analysis
    #: could not lock to still moves at the speed of one it could.
    FREE_RUN = 11.0

    #: How much a full bass front-loads the travel within a beat.
    #:
    #: The speed, now that the timing is not negotiable. At 0 the road
    #: moves evenly through the beat; at 1.6 the first frame of a beat
    #: travels 2.6 times as far as the mean and the last barely moves,
    #: which is a lunge onto the beat and a coast before the next one.
    #: Same arrival time, much more push - and the block arrives exactly
    #: on the beat either way, because the curve is the identity at both
    #: ends of it.
    LUNGE = 1.6
    #: And how much of the beat's travel is lunged rather than even.
    #:
    #: The floor under the coast is ``1 - LUNGE_MIX`` of the average
    #: speed, so at 0.55 the road never drops below 45 per cent of its
    #: own pace and still reaches 1.9 times it into the beat. All lunge
    #: reached 2.5 times and dropped to a two-hundredth, which is a stop.
    LUNGE_MIX = 0.55

    #: How hard the road bends, climbs and rolls.
    #:
    #: "Xxxx xxx xxxxx xxxxx xxx xx xx xxx xxxx xxxxxx xxxx
    #: xxxxxxxxxxxx." Three or four times what it was, and all three grow
    #: with how loud the passage is, so a drop throws the road about and a
    #: quiet passage is nearly straight.
    BEND = 2.6
    CLIMB = 1.9
    #: How tightly the road turns, in radians of phase per road unit.
    #: Pulled out of the sine because the roll is its derivative.
    BEND_EVERY = 0.17
    #: How much of the bend, the climb and the bank a quiet passage gets,
    #: and how much a loud one adds.
    #:
    #: It used to run 0.35 to 1.00, which at a drop put the road 19 per
    #: cent of the frame's width off its own line and rolled it fifteen
    #: degrees - and the camera opens its field of view at a drop too, so
    #: the two compounded and the pattern ahead became a diagonal band.
    #: 0.45 to 0.80 keeps the wander at 15 per cent and the roll at
    #: twelve degrees, and the energy is carried by the rig instead.
    PUSH_REST = 0.45
    PUSH_GAIN = 0.35
    #: How slowly the road's bends follow the loudness, in seconds.
    PUSH_EASE = 0.9

    #: How hard the track's own lean bends the road, and how far any
    #: single reading of it may push. See ``_carve``.
    #:
    #: The lean is summed about its own middle and in units of how much
    #: the record leans at all, so what comes out is a wander rather
    #: than a ramp and a narrow mix turns as much as a wide one.
    #:
    #: Measured over the whole visible length of road on a real record:
    #: with the lean summed raw and a tenth of it taken, the road moved
    #: eight thousandths of a lane sideways and was straight a hundred
    #: per cent of the time. The clamp is what keeps the tail from
    #: turning a bend into a hairpin - one reading on that record is
    #: thirteen spreads out on its own, and unclamped it swung the
    #: visible road three and a half lanes.
    TRACK_BEND = 0.22
    LEAN_MOST = 1.5

    #: How much of the track either side of a point is averaged into the
    #: shape of the road there, in seconds.
    #:
    #: Three quarters of a second each way. A road is a landscape and a
    #: song is not: its amplitude changes from one eighth of a second to
    #: the next by more than any hill should, and a curve drawn through
    #: readings that jump is a curve that jumps smoothly.
    SMOOTH_FOR = 0.75
    #: How hard the road banks into its own turn.
    #:
    #: 1.2 puts the roll where the old free-running one was at its
    #: strongest - about sixteen degrees - but now it is the turn that
    #: puts it there. See ``_road``.
    BANK = 1.2

    #: How far past the rider a block is still drawn. It has to go
    #: somewhere rather than stop dead on the rider's nose.
    GONE = 1.2

    #: Half a lane, and the rider gets the benefit of it.
    FORGIVE = 0.45
    #: Seconds of flashing, and of not being hit again, after a hit.
    SORE = 0.9

    #: How long the whole picture shows a hit, and what it does to it.
    #:
    #: The screen wash is the part that carries it. A shake says
    #: something happened to the camera; a frame that goes red and dark
    #: says something happened to *you*, which is what a hit is. Six
    #: tenths of a second, which is about a beat and a half - long enough
    #: to read and short enough to be over before the next figure.
    HURT_FOR = 0.6
    #: How hard a hit washes the frame, throws the camera and drops the
    #: light out of everything else.
    HURT_WASH = 0.34
    HURT_THROW = 2.3
    HURT_DIM = 0.72

    SHAKE = 0.030
    SHAKE_FALL = 0.10

    #: How far down the road the camera aims, how hard it turns towards
    #: it, and how quickly the aim itself moves.
    AIM = 7.0
    AIM_PULL = 0.11
    AIM_EASE = 0.06

    # -- the rig ----------------------------------------------------------
    #: The focal length as a share of the frame, at a crawl and at a
    #: sprint. Shorter is wider: 0.86 is about sixty degrees across the
    #: frame and 0.56 about ninety, which is the range a camera behind
    #: something moving is worth having.
    #: Shorter is wider. 0.86 is about sixty degrees across the frame and
    #: 0.62 about eighty - the range a camera behind something moving is
    #: worth having, stopped where it is because a wider one shrinks what
    #: it is showing: at 0.56 two lanes are 30 px apart where you have to
    #: choose between them, and at 0.62 they are 34.
    FOCAL_SLOW = 0.86
    FOCAL_FAST = 0.62

    #: How much further back the eye is dragged at a sprint, and the
    #: spring that drags it. Critically damped enough not to wobble: the
    #: point is a lag of a fraction of a second, not a bounce.
    CHASE = 0.55
    CHASE_SPRING = 0.020
    CHASE_DAMP = 0.86
    #: How fast the rig notices a passage has got louder. Slow: this is
    #: the shape of the song, not the shape of the bar.
    RUSH_EASE = 0.02

    #: How far the view banks into a bend, in degrees for a full turn.
    #:
    #: The camera leans the way a rider leans. Without it a bend is the
    #: picture sliding sideways; with it the horizon rolls and the road
    #: stays under you, which is the difference between watching a road
    #: and being on one.
    TILT = 5.0
    #: How hard the camera follows the road up a hill, and how much of the
    #: frame it is allowed to give up doing it.
    #:
    #: The rise it follows is the road ahead measured *from the road under
    #: the rider* - see ``_eye`` - so this is a camera looking up a hill
    #: rather than one reacting to where the whole road happens to sit.
    #:
    #: At a twentieth, the middle of the road ahead holds within 10 px of
    #: one row through every phase of the hill, against 79 px with the
    #: camera held still. Both of those are hills you can see: what moves
    #: is the frame, not the road. Harder than this and it overshoots -
    #: 30 px of swing at 0.08 - because it is then correcting more than
    #: the hill put there. PITCH_MOST is a rail rather than the usual
    #: case: the follow asks for 0.071 of the frame at its steepest.
    PITCH = 0.05
    PITCH_MOST = 0.085

    #: Where the horizon sits, as a share of the frame above its middle.
    #: A pure translation of the picture - it changes where the road sits
    #: in the frame and nothing about how much of it you can see.
    HORIZON_UP = 0.10

    #: The shake, which was too much of the picture. A kick moved the
    #: whole frame by three per cent of its width; at 1.1 per cent it is
    #: a knock rather than a camera being dropped.
    SHAKE_LESS = 0.25

    #: What a hit does to the road: how far the speed drops, and how fast
    #: it comes back. Half speed, back over about a second, which is long
    #: enough to be a punishment and short enough not to be a sulk.
    SLOW = 0.45
    SLOW_BACK = 0.030
    #: How many pieces a hit throws off, how fast they go and how long
    #: they last.
    SPARKS = 14
    SPARK_GO = 7.0
    SPARK_FADE = 1.9

    def __init__(self) -> None:
        self._lane = 1
        self._lane_here = 0.0
        #: What a run is judged on at the end: the prizes that went by and
        #: the ones taken, the longest chain, and the greys the shield
        #: took instead of you. See result.
        self._offered = 0
        self._taken = 0
        self._chain_most = 0
        self._saves = 0
        self._finished = False
        self._result = None
        #: Whether this run is the whole track, ridden from the start with
        #: no seek in it - the only kind a best is kept for. See _finish.
        self._whole = True
        #: Whether the playhead jumped this frame, and from where.
        self._jumped = False
        self._jumped_from = 0.0
        #: Set from outside, where the bests are kept: the best this
        #: track has been played to before, and whether this run beat it.
        self.best_before = None
        self.new_best = False
        self._at = 0.0
        self._last = None
        self._heard = 0.0
        self._shake = 0.0
        self._sore = 0.0
        #: 1 the moment something was hit, falling to nothing over
        #: HURT_FOR. Everything the picture does about a hit reads this.
        self._hurt = 0.0
        #: How many coloured blocks have been taken in a row, and 1 the
        #: moment one is. See CHAIN_FIRST.
        self._chain = 0
        self._got = 0.0
        #: Whether a grey has been touched yet. See CLEAN_BONUS.
        self._clean = True
        #: How far off the road the craft is, how fast it is rising,
        #: and how much of a peak it left from. See JUMP_UP.
        self._air = 0.0
        self._air_up = 0.0
        self._air_from = 0.0
        self._airs = 0
        self._best_air = 0
        #: Coins taken, coins in a row now, and the best row of the run.
        #: See COIN_WORTH.
        self._coins = 0
        self._coin_run = 0
        self._coin_best = 0
        #: How far through its spin each coin is, so a trail of them
        #: turns together rather than each on its own phase.
        self._coin_spin = 0.0
        #: The bumper: 1 when it is up, 0 the moment it shatters a grey,
        #: and back to 1 over SHIELD_BACK.
        self._shield = 1.0
        #: Audiosurf's matrix, a list of colours per column from the
        #: bottom up. See CELLS_WIDE.
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
        #: What became of each block the craft met, as the game decided
        #: it: see struck.
        self._struck: dict = {}
        self._laid = 0.0
        self._chart_from = None
        #: When the last figure was put down, so the next one can be held
        #: off until there is room for it.
        self._placed = -99.0
        self._loudness = 0.0
        self._pushing = 0.0
        self._speed = self.FREE_RUN
        #: How hard the road is lunging into the beat, and the beat it
        #: was chosen on. See LUNGE.
        self._lunge = 1.0
        self._lunge_from = None
        #: Seconds in a beat, or 0 when nothing has found a tempo.
        self._beat = 0.0
        #: A moment that is known to be on the beat, for snapping to.
        self._grid = None
        #: Where the music clock was last frame, for working out how fast
        #: the road is going against the track rather than the wall.
        self._last_heard = None
        #: The moment beat zero started, for measuring distance from. See
        #: ``_advance`` for why it is not the same thing as ``_grid``.
        self._origin = None
        #: How far through the current beat the track is, 0 to 1.
        self._pulse = 0.0
        #: Set while an obstacle has just been hit: it slows the road and
        #: throws pieces off. See SLOW and _sparks.
        self._slow = 1.0
        self._sparks: list = []
        #: The playhead as it was last frame, so a paused track can be
        #: told from a playing one.
        self._was_at = None
        self._rolling = 1.0
        #: Where the camera is looking, across the road and up it, and
        #: how hard it is banked into the bend.
        self._aimed = 0.0
        self._pitched = 0.0
        self._banked = 0.0
        #: How hard the road is running against its resting speed, eased.
        self._rushing = 0.0
        #: Where the eye is behind the rider, and the spring dragging it.
        #: Starts where it rests, so the first frame is drawn from a
        #: camera rather than from inside the rider's nose.
        self._chase = self.EYE_BACK
        self._chase_to = 0.0
        #: The shake's own clock, so it is not tied to anything else.
        self._wobble = 0.0
        #: How far through a whole turn the road is. See ``_find_twists``.
        self._rolled = 0.0
        #: How close to a beat the track is, held while it is stopped.
        self._beat_lit = 0.0
        #: How fast the craft is crossing lanes, for the bank.
        self._swerve = 0.0
        #: What the screen is still answering, and where the craft is on
        #: the glass for the answers to come from. See POPS.
        self._pops = []
        self._craft_glass = None
        self._craft_spot = None
        #: The road's colour this frame, for anything that answers in it.
        self._hue_now = 0.0
        #: The last frame's worth of the track's clock. See ``_advance``.
        self._went = 0.0
        self._bend = 0.0
        self._climb = 0.0
        #: How high the road is under the rider. The eye rides on it
        #: rather than hovering at a fixed height in the world - see
        #: ``_eye``.
        self._under = 0.0
        #: And how far across it is, so the eye rides the road sideways
        #: as well as up. See ``_advance``.
        self._side = 0.0
        #: The track's shape, and the road made out of it. See ``_shape``.
        self._shaped = None
        self._hill = ()
        self._curve = ()
        self._energy = ()
        #: Where the road turns over on itself. See ``_find_twists``.
        self._twists = ()
        #: Which twists have had their power block laid.
        self._twisted = set()
        #: What the next thing collected is multiplied by, if anything.
        self._double = 1.0
        self._every = 1.0
        #: Where the road starts, a fixed distance in front of the eye.
        #: Kept until the camera has been worked out for the frame.
        self._near = self.NEAR
        self._quick = 0.0
        self._quiet = None
        self._peak = 0.0
        self._plasma = Plasma()

    # -- playing ----------------------------------------------------------
    def steer(self, way: int) -> bool:
        """Move a lane. Returns whether the key meant anything here."""
        was = self._lane
        self._lane = max(0, min(self.LANES - 1, self._lane + int(way)))
        return self._lane != was

    @property
    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        """Change game, and start the new one fresh.

        The two do not share a score, a grid or a chain, so carrying any
        of it across would be carrying a number that meant something
        else. ``reset`` rebuilds this from a new one of itself, which
        would put the mode back as well - so it is set again afterwards.
        """
        if mode in self.MODES and mode != self._mode:
            self.reset()
            self._mode = mode

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

    # -- the chart --------------------------------------------------------
    #: Which drum makes which shape, and the order they win a slot in.
    #: A kick beats a snare beats a run of hats, so the heaviest thing in
    #: a slot is what you see.
    PATTERNS = (("Kick", "wall"), ("Snare", "block"), ("Hats", "run"))

    #: Two bars of shapes, so a track with a kick on every beat is not two
    #: bars of identical walls.
    #:
    #: Preferring the heaviest drum in a slot puts every figure on a beat,
    #: which is what makes it feel like music - and on four-to-floor it
    #: also means the kick wins every slot and every figure is a wall.
    #: The drum decides when a figure lands, which is what keeps the chart
    #: on the beat; the pool decides what it looks like. Indexed by the
    #: slot, so it repeats every eight figures and a track lays out the
    #: same way every time it is played.
    POOL = ("wall", "block", "wall", "run",
            "wall", "block", "wall", "wall")

    def _lay(self, state) -> None:
        """Put the next stretch of chart on the road.

        Only the part that has come into view since the last frame, so
        this walks each hit once however long the track is.
        """
        # The same empty table every time, so that a track with no chart
        # yet does not look like a new chart on every frame and throw the
        # road away sixty times a second.
        chart = getattr(state, "chart", None) or _NO_CHART
        # And across a seek, from where it landed. Left laid from where it
        # was, a jump forward met every block in the stretch it skipped in
        # a single frame and took the ones in the craft's lane - five
        # thousand points for skipping to the end - and a jump back found
        # the stretch it went back over already laid and gone, so empty.
        fresh = chart is not self._chart_from or self._jumped
        if fresh:
            self._chart_from = chart
            self._blocks = []
            self._laid = self._heard
            self._placed = -99.0
            # The corkscrews' power blocks with the rest: one already in
            # view when the road was laid again was never laid again.
            self._twisted = set()
        ahead = self._heard + self.READ
        if ahead <= self._laid:
            return
        low, self._laid = self._laid, ahead

        # Every hit in the window, heaviest first at the same moment, so a
        # kick and a hat on the same beat give a wall rather than both.
        due = []
        for order, (name, shape) in enumerate(self.PATTERNS):
            for when in chart.get(name, ()):
                if low < when <= ahead:
                    due.append((when, order, shape))
        due.sort()
        index = 0
        while index < len(due):
            when, _order, shape = due[index]
            # How far apart the figures are here. Audiosurf spawns more
            # blocks where there is more going on, and the place to ask
            # is where the figure lands rather than where the playhead
            # is - a figure is laid three beats before anybody sees it.
            gap = self.GAP
            if self._beat > 0.0:
                gap = max(gap, self._beat * self._apart(when))
            if when - self._placed < gap - self.SLACK:
                index += 1
                continue
            # The heaviest drum within a moment of it, not whichever came
            # first. A hat lands on the eighth and a kick on the beat, and
            # taking the first candidate meant half the figures sat on an
            # off-beat: measured on a 128 bpm track, the median figure was
            # 234 ms from a beat, which is exactly half of one.
            reach = (self._beat * self.PREFER_BEATS if self._beat > 0.0
                     else self.PREFER)
            best = index
            for other in range(index + 1, len(due)):
                if due[other][0] - when > reach:
                    break
                if (due[other][1], self._off_beat(due[other][0])) < (
                        due[best][1], self._off_beat(due[best][0])):
                    best = other
            when, order, shape = due[best]
            when = self._snap(when)
            self._placed = when
            self._shape(self._varied(shape, when), when,
                        grey=self._greyed(when, order))
            index = best + 1
        # And something to do where the track went quiet.
        #
        # The chart is the drums, and a breakdown has none - so the road
        # had nothing on it at all. Measured on a real record, it was
        # bare 16 per cent of the time and one stretch ran 8.6 seconds,
        # which is nine seconds of a game with nothing in it. Audiosurf
        # thins out where a track thins out; it does not stop.
        #
        # Prizes rather than hazards, on the beat like everything else.
        # A quiet passage is a place to collect, not a place to be
        # caught out by something the music never played, and the rule
        # that puts hazards on the beats you can hear coming would have
        # to be broken to put one here.
        #
        # From the start of the window when nothing has been placed yet,
        # rather than waiting for the chart to place something first:
        # waiting meant a track the detector found no drums in at all -
        # ambient, orchestral, a voice - had an empty road for its whole
        # length.
        if self._beat > 0.0:
            every = self._beat * self.QUIET_BEATS
            last = max(self._placed, low - every)
            while last + every <= ahead:
                when = self._snap(max(last + every, low + 1e-6))
                if when <= last:
                    break
                last = self._placed = when
                self._shape(self._varied("block", when), when, grey=False)
        # And the powerup at the mouth of each corkscrew. Audiosurf 2
        # puts "corkscrew loops and powerups timed perfectly with big
        # moments in your music" - one comes with the other.
        for start in self._twists:
            if low < start <= ahead and start not in self._twisted:
                self._twisted.add(start)
                self._blocks.append(
                    [self._snap(start + self.TWIST_FOR * 0.5),
                     self.LANES // 2, "power", False, False])
        if fresh:
            # Laid from right where the craft is, a hit just after it can
            # be put on the beat just before it, and was met straight
            # away: a prize nobody could have reached, counted as missed.
            self._blocks = [block for block in self._blocks
                            if block[0] > self._heard]
        self._blocks = self._blocks[-200:]

    #: How long the road may have nothing on it before something is
    #: put there anyway, in beats.
    #:
    #: Four, which is a bar. Long enough that a real gap in the drums
    #: still reads as the track thinning out, short enough that it never
    #: becomes a road with nothing to do on it.
    QUIET_BEATS = 4.0

    #: Which slots carry an obstacle rather than a prize.
    #:
    #: A quarter of them. Audiosurf's greys are a hazard among the
    #: colours, not the other way round - a road of nothing but obstacles
    #: is a road you cannot score on, and tying them to the kick gave
    #: exactly that: on four to the floor the kick wins every slot.
    #:
    #: Indexed by the slot like the shapes are, so a track lays out the
    #: same way every time it is played, and the two pools are different
    #: lengths so the pattern of shape-against-hazard does not repeat
    #: every eight figures.
    GREY_POOL = (False, False, True, False, False, True, False)

    #: And Ninja's, which is the same road with far more to dodge.
    #:
    #: Audiosurf 2: "the final monocolor mode is Ninja Mode, which
    #: contains a much larger collection of obstacles and a special set
    #: of bonuses for successfully dodging them". The bonuses are
    #: already here - a coin trail goes beside every obstacle - so more
    #: obstacles is more to dodge *and* more to be paid for dodging,
    #: which is the shape the mode is meant to have.
    #:
    #: Four slots in seven against two. Still a pool rather than every
    #: slot: a road of nothing but obstacles is a road nobody can score
    #: on.
    #:
    #: The obstacles are what changes, not how often a figure lands.
    #: Putting the figures closer together as well was tried and is not
    #: worth having: the gap has a floor in seconds, so at 128 bpm both
    #: games quantise to the same two beats and nothing happens at all,
    #: while at slower tempos it pushes under the floor that "xxxx xxx
    #: xxxxxxx xxx xxx xxxx xxx it's unplayable" put there. A larger
    #: collection of obstacles is a larger collection of obstacles
    #: whether or not there is more of everything else.
    NINJA_POOL = (True, False, True, True, False, True, False)

    def _greyed(self, when: float, order: int) -> bool:
        """Whether the figure at this slot is an obstacle.

        The drum still has a say: a slot the pool calls safe stays safe,
        and one it calls dangerous is only dangerous if the heaviest
        thing in it was the kick. So the obstacles land on the beats you
        can hear coming.
        """
        if order != 0:
            return False
        # Nothing to dodge inside a corkscrew. The world turns all the
        # way over there and left stops meaning left half way round, so
        # an obstacle in one is not a thing you failed to dodge, it is a
        # thing nobody could have. The corkscrew is the spectacle and
        # the power block in it is the reward; the greys wait.
        if self._twist_at(when) is not None:
            return False
        if self._beat <= 0.0:
            return True
        slot = int(round(when / self._beat / max(1e-6, self.GAP_BEATS)))
        pool = self.NINJA_POOL if self._mode == "Ninja" else self.GREY_POOL
        return pool[slot % len(pool)]

    def _varied(self, shape: str, when: float) -> str:
        """What shape this slot takes.

        The drum decides *when* a figure lands, which is what keeps the
        chart on the beat. The pool decides what it looks like, which is
        what stops four-to-floor being two bars of identical walls: the
        kick wins every slot on that music, so every figure was a wall.

        Indexed by the slot rather than by the beat. Slots are GAP_BEATS
        apart, so indexing by the beat only ever reached the even entries
        of the pool - and with the walls at even positions, that was the
        same wall again.
        """
        if self._beat <= 0.0:
            return shape
        slot = int(round(when / self._beat / max(1e-6, self.GAP_BEATS)))
        return self.POOL[slot % len(self.POOL)]

    def _snap(self, when: float) -> float:
        """The nearest beat to ``when``, or ``when`` if there is no grid.

        The detector says where it heard a drum, and on real music that is
        a few tens of milliseconds either side of the beat and not the
        same amount each time. Choosing the heaviest drum in a slot puts
        figures on the *right* drums; it cannot put them on the grid,
        because the drums themselves are not exactly on it. Snapping does.

        Only as far as half a beat, so a figure never moves to a beat that
        is not the one it came from.
        """
        if self._beat <= 0.0 or self._grid is None:
            return when
        steps = (when - self._grid) / self._beat
        return self._grid + round(steps) * self._beat

    def _off_beat(self, when: float) -> float:
        """How far a moment is from the nearest beat, in seconds.

        Measured from ``_grid`` rather than from zero. Counting beats from
        the start of the file assumes the first beat is at 0:00, which is
        true of a written test track and of nothing anybody has recorded:
        a track whose beats sit on the half would have had every candidate
        scored as maximally off, and the tiebreak this feeds would have
        picked by drum weight alone.

        Nothing when no tempo has been found, so that the chart falls back
        to taking the heaviest drum and nothing else.
        """
        if self._beat <= 0.0:
            return 0.0
        beats = (when - (self._grid or 0.0)) / self._beat
        return abs(beats - round(beats)) * self._beat

    #: Coins: what they are worth, how many sit beside one obstacle and
    #: how far apart.
    #:
    #: The problem they solve is that dodging is free. Three lanes, one
    #: obstacle, two ways past it - and the two are worth exactly the
    #: same, so the best play is to sit in the far lane and wait, which
    #: is the least interesting thing the game can ask for. Audiosurf 2
    #: answers it in Ninja mode with "a special set of bonuses for
    #: successfully dodging" rather than for merely not being hit, and
    #: this is that bonus made concrete: a short trail of coins in the
    #: lane *next to* the obstacle, spanning the moment it passes.
    #:
    #: So the far lane is safe and pays nothing, and the near lane pays
    #: three coins to whoever will hold it while a grey goes by an arm's
    #: length away. Twenty-five is Audiosurf 2's own base block value.
    COIN_WORTH = 25
    COIN_STEP = 25
    COIN_MOST = 200
    COINS_RUN = 3
    COIN_GAP = 0.16

    #: How long before a wall arrives its coin trail has to have ended.
    #:
    #: A wall closes two lanes of three, so there is no lane "beside" it
    #: to be brave in - the one way through is the one way through, and
    #: a coin in it would pay for having nowhere else to go. Measured on
    #: a real record, walls are two thirds of every figure laid, so a
    #: coin that only ever sat beside a single obstacle almost never
    #: appeared at all: one trail in a minute of music.
    #:
    #: So the trail goes in a lane the wall is about to *close*, and
    #: ends before it gets there. You ride the doomed lane, take what is
    #: in it and leave. Three tenths of a second is six lane changes'
    #: worth of room at the snap the craft actually moves at, and about
    #: three quarters of a beat at 130.
    COIN_LEAD = 0.30

    #: Wakeboard: how hard a jump pushes off, how hard it comes down,
    #: how far ahead a crest is read, and what a jump off one pays.
    #:
    #: Audiosurf 2's fourth mode is "like mono but puts you on a
    #: surfboard that can leap off the track, gaining more points for
    #: jumping at a peak". The road already has peaks: it is cut from
    #: the track's own amplitude, so a crest is where the music is about
    #: to drop away. The jump is the one thing on this road that is not
    #: locked to it - the blueprint's "the vehicle is completely locked
    #: to the 3D spline" holds for every other game here.
    #:
    #: Three and a fifth of push against ten of gravity clears half a
    #: unit, which is most of a block's height, and is two thirds of a
    #: second in the air - about a beat and a half at 128, or one
    #: figure's worth. Long enough to be a decision and short enough not
    #: to be a way of sitting out the hard parts; and nothing at all is
    #: collected up there, so it never is one. Two units of push was
    #: tried first and lifted the craft a quarter of a unit, which
    #: against a block six tenths tall does not read as leaving the
    #: road at all.
    JUMP_UP = 3.2
    JUMP_DOWN = 10.0
    CREST_LOOK = 2.5
    CREST_FULL = 0.45
    AIR_WORTH = 150

    def jump(self) -> bool:
        """Leave the road, in the game that lets you.

        Returns whether the key meant anything, the way ``steer`` does,
        so it is left to whatever else wanted it in the eight scenes and
        three games that are not this one.
        """
        if self._mode != "Wakeboard" or self._air > 0.0 or self._air_up > 0.0:
            return False
        self._air_up = self.JUMP_UP
        # What the road was doing at the moment it left, which is what a
        # jump is scored on. Read here rather than on landing: by then
        # the crest is behind you.
        self._air_from = self._crest()
        return True

    def _crest(self) -> float:
        """How much of a peak the road is at, from nothing to one.

        A crest is where the road ahead falls away from the road under
        you. Heights are drawn larger downwards - see ``_eye`` - so the
        road ahead sitting at a *larger* height than the road here is
        the road dropping away, which is the top of a hill and the place
        a board would leave the ground on its own.
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
        # Down. What it paid is how much of a peak it left from, which
        # is the whole of "more points for jumping at a peak": a jump
        # off the flat scores nothing at all.
        self._air = 0.0
        self._air_up = 0.0
        paid = int(self.AIR_WORTH * self._air_from * self._double)
        if paid:
            self._airs += 1
            self._best_air = max(self._best_air, paid)
            self._score += paid
            self._double = 1.0
            self._got = 1.0
            self._burst(self._lane_here, prize=True)
            self._pop("air", hue=0.50, sat=0.55,
                      strength=0.6 + self._air_from * 0.8,
                      text=(f"AIR +{paid}" if self._air_from > 0.6 else ""))
        self._air_from = 0.0

    #: What a power block multiplies, and how long it waits to be spent.
    #:
    #: Audiosurf 1's blueprint: "Xxxxxxxx x xxxx xxxxxx x xxxxxxx Xxxxx
    #: Xxxxxxxxxx Xxxxx xx xxx centre xxxx. Xxxxxxxxxx xx xxxxxxxxx
    #: xxxxxxx xxx xxxxx xxxxx xx xxxxxxxx xxxxxx xxx xxxxxxxxx xxxx
    #: xxxxxx xxx xxxxxx'x xxxx." Audiosurf 2 has the same thing at 1.5x
    #: with a big one at 2x. Here it doubles the next thing collected
    #: that pays - the next cluster in Puzzle, the next prize in Mono -
    #: and is spent when it does.
    POWER_DOUBLE = 2.0

    def _coins_beside(self, when: float, lane: int, grey: bool) -> None:
        """A trail of coins in the lane next to an obstacle.

        Beside a single obstacle: the far lane is safe and pays nothing,
        the near one pays for being held while a grey goes past.
        """
        if not grey:
            return
        beside = [side for side in (lane - 1, lane + 1)
                  if 0 <= side < self.LANES]
        if not beside:
            return
        # Which side, from the time, so a track lays out the same way
        # every time it is played.
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

    def _coin_trail(self, first: float, side: int) -> None:
        """Lay one, if the lane is free for the whole of it.

        Never into a lane something else is already using: a coin a
        player cannot take without being hit is not a reward, and one
        sitting inside a prize is a coin nobody can see.
        """
        last = first + (self.COINS_RUN - 1) * self.COIN_GAP
        pad = self.COIN_GAP / 2.0
        for other, taken, _kind, _done, _grey in self._blocks:
            if taken == side and first - pad <= other <= last + pad:
                return
        for step in range(self.COINS_RUN):
            self._blocks.append(
                [first + step * self.COIN_GAP, side, "coin", False, False])

    def _shape(self, pattern: str, when: float, grey: bool = True) -> None:
        """One hit, as one or more blocks in lanes.

        ``grey`` is Audiosurf's distinction and the whole of its Mono
        mode: a grey block is an obstacle to be dodged and a coloured one
        is a prize to be driven into. A figure is grey when the drum that
        put it there was the kick - the heavy one, the one you feel
        coming - and coloured otherwise.
        """
        # The lane comes from the time rather than from a random number,
        # so a track lays out the same way every time it is played.
        seed = int(when * 977) % self.LANES
        if pattern == "wall":
            for lane in range(self.LANES):
                if lane != seed:
                    self._blocks.append([when, lane, "wall", False, grey])
            self._coins_before(
                when, [lane for lane in range(self.LANES) if lane != seed],
                grey)
        elif pattern == "block":
            self._blocks.append([when, seed, "block", False, grey])
            self._coins_beside(when, seed, grey)
        elif pattern == "run":
            for step in range(3):
                lane = (seed + step) % self.LANES
                self._blocks.append([when + step * self.RUN_GAP, lane,
                                     "run", False, grey])
            # Beside the first of them only. A slalom already asks for
            # three moves; paying for a fourth lane change between each
            # pair would ask for something nobody can do.
            self._coins_beside(when, seed, grey)

    # -- the world --------------------------------------------------------
    #: The colour of the road, by how much is going on in the music.
    #:
    #: Audiosurf runs a track from purple at its quietest through blue,
    #: green and yellow to red at its loudest, and the colour is most of
    #: how a track reads at a glance: you can see a chorus coming in the
    #: hue of the road before you can hear it. These are the hues those
    #: five tiers sit at on the wheel.
    TIERS = (0.78, 0.60, 0.33, 0.15, 0.00)
    #: How far the synth may push the colour off its tier. Small: the
    #: tier is the point, and a synth line that moved it a fifth of the
    #: way round would make the whole scheme mean nothing.
    TIER_SYNTH = 0.05

    def _tier(self, energy: float, synth: float) -> float:
        """The road's colour for this much energy. See TIERS."""
        place = max(0.0, min(1.0, energy)) * (len(self.TIERS) - 1)
        low = min(len(self.TIERS) - 2, int(place))
        share = place - low
        hue = self.TIERS[low] + (self.TIERS[low + 1] - self.TIERS[low]) * share
        return (hue + synth * self.TIER_SYNTH) % 1.0

    def _carve(self, state) -> None:
        """Take the track's shape and make a road out of it.

        Audiosurf does not invent its track: it reads the song once,
        before anything is drawn, and the amplitude becomes the incline
        while the balance between the channels becomes the curve. That is
        what ``attachment_audio.contour`` produces, and this turns it
        into the two arrays the road is read out of.

        The hill is the amplitude about its own middle, so a chorus runs
        downhill and a breakdown climbs. The curve is the lean *summed*
        along the road rather than used directly, because a lean is a
        direction and a road is where following a direction gets you -
        used directly it would be a road that kinks, and summed it is a
        road that turns. What that sum drifts to does not matter: the
        camera is pinned to the road, so only the bend is ever seen.

        Built once a track, and the road is then the same road every time
        that track is played.
        """
        shape = getattr(state, "contour", None)
        if shape is self._shaped:
            return
        self._shaped = shape
        loud = (shape or {}).get("loud") or ()
        lean = (shape or {}).get("lean") or ()
        self._every = float((shape or {}).get("rate") or 0.0) or 1.0
        if not loud:
            self._hill = self._curve = self._energy = ()
            return
        # Kept as it came as well as centred, because the colour of the
        # road and how thickly the figures come are both "how much is
        # going on here", which is the reading itself.
        self._energy = tuple(loud)
        middle = sorted(loud)[len(loud) // 2]
        self._hill = self._eased(
            [(value - middle) * 2.0 for value in loud])
        curve, run = [], 0.0
        # The lean about its own middle, in units of how much this
        # record leans at all.
        #
        # Summing it raw is what a road built from a *direction* wants,
        # but a mix has a bias: measured on a real record the lean
        # averaged -0.0097, which over three and a half minutes summed
        # to -16 and swamped everything else in it. A road built from
        # that turns constantly in one direction at a near-constant
        # rate - and since the camera is pinned to the road, a constant
        # rate is exactly what straight looks like. The whole visible
        # length of it moved eight thousandths of a lane sideways: the
        # road was straight a hundred per cent of the time.
        #
        # A mix that sits slightly left for a whole song is not a road
        # that turns left for ever. It is a road that goes straight,
        # because every part of it leans the same way. What turns a road
        # is one part leaning further than the rest, which is the lean
        # about its own middle - and dividing by how much it varies
        # means a narrow mix turns as much as a wide one, rather than a
        # nearly-mono record getting a road with no corners in it.
        wide = [lean[index] if index < len(lean) else 0.0
                for index in range(len(loud))]
        middle = sum(wide) / len(wide)
        spread = math.sqrt(
            sum((value - middle) ** 2 for value in wide) / len(wide))
        for value in wide:
            # And no single reading may throw the road across it. The
            # distribution has a long tail - one reading on the record
            # measured above is thirteen spreads out on its own - and
            # unclamped that one reading swung the visible road three
            # and a half lanes sideways, which is a hairpin rather than
            # a bend.
            step = (value - middle) / (spread or 1.0)
            run += max(-self.LEAN_MOST, min(self.LEAN_MOST, step))
            curve.append(run)
        self._curve = self._eased(curve)
        self._twists = self._find_twists()
        self._twisted = set()

    #: The corkscrews: how loud a moment has to be to turn the road
    #: over, how long one takes end to end, and how much road there is
    #: between two of them.
    #:
    #: Audiosurf 2 puts "corkscrew loops and powerups timed perfectly
    #: with big moments in your music", and what counts as a big moment
    #: is already measured: the loudness contour the road's hill is cut
    #: from. A corkscrew goes where the track is within a fifth of its
    #: own loudest and nowhere else, which on most records means the
    #: drops and the last chorus.
    #:
    #: Twenty-five seconds apart at the least, because a corkscrew is
    #: an event and three in a row is a fairground ride. Measured on a
    #: loud dance record, fourteen gave one every fifteen seconds and
    #: ate a sixth of the track: the gate is a share of the *track's*
    #: own peak, and on something compressed most of the song is near
    #: it, so the spacing rather than the loudness is what decides. Two
    #: and a half seconds end to end: long enough to read as a whole
    #: turn of the world and short enough that nobody is upside down
    #: while a decision matters.
    TWIST_LOUD = 0.80
    TWIST_FOR = 2.5
    TWIST_APART = 25.0

    def _find_twists(self) -> tuple:
        """The moments the road turns over, from the loudest of the song.

        Off the track rather than off a timer, so a record corkscrews in
        the same places every time it is played - which is the whole
        promise of the scene.
        """
        if not self._energy or self._every <= 0.0:
            return ()
        loudest = max(self._energy)
        if loudest <= 0.0:
            return ()
        # Loud for this track, and loud *against this track*. The first
        # alone is a share of the peak, which every reading of a track
        # with no dynamics in it meets - a wall of noise would corkscrew
        # every fourteen seconds for no reason, because nothing in it is
        # a big moment. Half way from the middle of the track to its
        # peak is the second test, and on a flat track that is the whole
        # track, so nothing passes.
        middle = sorted(self._energy)[len(self._energy) // 2]
        gate = max(loudest * self.TWIST_LOUD, (middle + loudest) / 2.0)
        found = []
        for index, value in enumerate(self._energy):
            if value <= gate:
                continue
            at = index / self._every
            if found and at - found[-1] < self.TWIST_APART:
                continue
            found.append(at)
        return tuple(found)

    def _twist_at(self, when: float):
        """How far through a corkscrew a moment is, 0 to 1, or None."""
        for start in self._twists:
            if start <= when < start + self.TWIST_FOR:
                return (when - start) / self.TWIST_FOR
        return None

    @staticmethod
    def _turned(through: float) -> float:
        """The roll at that point of a corkscrew, in whole turns.

        Smoothed at both ends rather than linear. A linear sweep starts
        and stops the world spinning in one frame, which is a cut rather
        than a corkscrew; this one is still at both ends and quickest
        through the middle. It is exactly one whole turn either way, so
        the world comes back to where it started and the moment the
        twist ends is not a moment anything jumps.
        """
        return through * through * (3.0 - 2.0 * through)

    def _eased(self, table) -> tuple:
        """The readings with the jitter taken out of them.

        A road is a landscape. The amplitude of a track changes from one
        eighth of a second to the next by more than any hill should, and
        a curve through readings that jump is a curve that jumps
        smoothly. Averaged over a second or so of them, what is left is
        the shape of the song rather than the shape of its transients.
        """
        reach = max(1, int(self._every * self.SMOOTH_FOR))
        out = []
        for index in range(len(table)):
            low = max(0, index - reach)
            high = min(len(table), index + reach + 1)
            out.append(sum(table[low:high]) / (high - low))
        return tuple(out)

    def _read(self, table, when: float) -> float:
        """One reading of the track's shape, between two of them.

        On a Catmull-Rom curve through the readings, which is what the
        blueprint asks the track to be and what it has to be to be drawn
        at all. Straight lines between readings leave a corner at every
        one of them, and a corner in the road is a corner in everything
        laid on the road: the chevrons and lane dashes span a stretch of
        it, so their two ends land on different sides of the kink and the
        shape splays. On a real track at eight readings a second that is
        not a subtle artefact - the markings came out as jagged spikes.
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

    def _when(self, at: float) -> float:
        """The moment of the track a point on the road belongs to."""
        reach = self._at + at - self.RIDER_AT
        if self._beat > 0.0 and self._origin is not None:
            return self._origin + reach / self.PER_BEAT * self._beat
        return reach / self.FREE_RUN

    def _road(self, at: float) -> tuple:
        """Where the road is at distance ``at``: across, up, and rolled.

        One function, called for every rung, every block and the rider, so
        that everything on the road agrees about where the road is.

        The roll is the rate the road is turning at, not a wobble of its
        own. It used to be a third sine on a third phase, which tumbles
        the world independently of where the road goes - so at some
        phases the road leaned one way while turning the other, and which
        lane a block was in became a guess. A track banks into its own
        turn, which is both what makes it readable and what makes it a
        track: "it's hard to see obstacle patterns in some angles".
        """
        push = self.PUSH_REST + self._pushing * self.PUSH_GAIN
        if self._curve:
            return self._from_track(at, push)
        # Behind the rider the road runs straight.
        #
        # It is drawn from 2.4 behind them so that its near edge stays
        # off the bottom of the frame, and at that distance the eye is a
        # unit and a half away: the projection multiplies whatever is
        # there by two hundred. A bend carried on back there swung the
        # part of the road nobody can use across the whole frame and took
        # its near edge off a corner, which is a road that reads as a
        # diagonal slab. The curve belongs ahead of you.
        line = max(at, self.RIDER_AT)
        turn = math.cos(line * self.BEND_EVERY + self._bend) * self.BEND_EVERY
        return (math.sin(line * self.BEND_EVERY + self._bend)
                * self.BEND * push,
                math.sin(at * 0.11 + self._climb) * self.CLIMB * push,
                -turn * self.BEND * push * self.BANK)

    def _cruise(self) -> float:
        """The speed the road runs at with nothing pushing it.

        One beat's worth of road a beat, which is what the road covers
        when the lunge is flat. Everything about the rig is measured
        against it rather than against a number, so it stays right at
        every tempo.
        """
        if self._beat > 0.0:
            return self.PER_BEAT / self._beat
        return self.FREE_RUN

    def _camera(self, rect, surge: float, bass: float) -> tuple:
        """Where the eye is: the vanishing point, the focal length and
        the bank, as one call a frame.

        One place rather than a block inside ``paint``, so that a test
        can ask the scene where it is looking instead of working it out
        again from the constants and being wrong differently.

        Moves the camera as well as reporting it - the aim, the hill and
        the bank are all eased towards where the road is - so it is called
        once a frame and no more.
        """
        span = min(rect.width(), rect.height())
        # How hard the passage is pushing, which is what the rig follows.
        #
        # Audiosurf drives the field of view from the vehicle's linear
        # speed, and its linear speed *is* the song's amplitude - loud
        # passages are downhills. Here the average speed is not free to
        # move: a beat covers exactly one beat's worth of road, which is
        # what puts a block under the rider on its beat. So the rig reads
        # the amplitude directly, which is the same quantity by a shorter
        # route, and the speed carries it inside the beat as the lunge.
        #
        # Eased, because a field of view that jumped about would be a
        # strobe rather than a camera.
        # Every ease here is gated on whether the track is playing. A
        # camera that goes on settling under a stopped song moves the
        # whole picture, which is most of what "xxxxxxxxxx xxxxxx xxxx
        # xxxx xxxxx xxxxx xx xxxxxx" was: measured, 72 per cent of the
        # frame still changed from one frame to the next.
        going = self._rolling
        self._rushing += ((self._loudness - self._rushing)
                          * self.RUSH_EASE * going)
        wide = max(0.0, min(1.0, self._rushing))
        # Never wider than FOCAL_FAST. The strobe and the bass open it a
        # little further on top of the passage's own push, and without a
        # floor the three together took it to 0.46 of the frame - past
        # ninety degrees, where the edges of the road bow.
        focal = span * max(self.FOCAL_FAST,
                           self.FOCAL_SLOW
                           + (self.FOCAL_FAST - self.FOCAL_SLOW) * wide
                           - surge * 0.06 - bass * 0.04)
        # And the eye is dragged back by it. A spring rather than a
        # follow, so a drop pulls the rider away down the road for a
        # moment before the camera catches up, and a climb lets it close
        # in - which is the lag a camera on a boom would have and a
        # camera welded to the ship would not.
        pull = (self.EYE_BACK * (1.0 + wide * self.CHASE) - self._chase)
        self._chase_to += pull * self.CHASE_SPRING * going
        self._chase_to *= self.CHASE_DAMP
        self._chase += self._chase_to * going
        # Where the road starts, which is a fixed distance in front of
        # the *eye* rather than a fixed distance behind the rider. The
        # eye is on a spring now and slides back at a drop; measured from
        # the rider, that brought the road's near edge 156 px into a
        # 640x360 frame, which is the hard edge straight across the
        # picture that NEAR exists to prevent.
        self._near = self.NEAR_EYE - self._chase
        centre = rect.center()
        # The camera looks down the road rather than straight ahead while
        # the road swings away from it.
        #
        # The road bends hard now, and a camera pinned to the middle of
        # the frame meant the whole picture swung across it: "xxxxxx xx
        # xxx xxxx xxx xxxxx". Aiming at where the road is a little way
        # ahead holds the track roughly in the middle of the frame and
        # turns the swing into a lean, which is what being on a road
        # feels like. Eased, so the aim itself does not snap.
        across_ahead, up_ahead, _roll = self._road(self.RIDER_AT + self.AIM)
        # Measured from the road under the rider, which is where the eye
        # now sits: what is left is the lead, the bit of the bend that is
        # still ahead of you.
        self._aimed += ((across_ahead - self._side - self._aimed)
                        * self.AIM_EASE * going)
        # The rise *ahead of the rider*, which is the hill. Measured from
        # the road under them, the same way everything else is.
        self._pitched += ((up_ahead - self._under - self._pitched)
                          * self.AIM_EASE * going)
        # How hard the road is turning, which is what the view banks
        # into. The aim is already measured from under the rider.
        self._banked += (self._aimed - self._banked) * self.AIM_EASE * going
        # The shake is a decaying wobble on its own fast clock rather than
        # a sine of the spin, which never stopped moving. In sixtieths of
        # a second rather than in frames, so the rattle is the same
        # rattle on a pane managing thirty as on one running at 120 -
        # counted in frames it halves in frequency when the machine is
        # busy, which turns a hit from a crack into a sway.
        self._wobble += self._went * 60.0
        shake = self._shake * self.SHAKE * self.SHAKE_LESS * span
        # Up the hill with the road, within reason. A climb puts the road
        # ahead higher in the frame, so the view drops to meet it - which
        # is a positive lift on a negative rise, and the reason the sign
        # is worth a line: the other way round the camera runs away from
        # the hill and doubles its swing across the frame.
        lift = max(-self.PITCH_MOST, min(self.PITCH_MOST,
                                         self._pitched * self.PITCH))
        horizon = QPointF(
            centre.x() - self._aimed * focal * self.AIM_PULL
            + math.sin(self._wobble * 1.9) * shake,
            centre.y() - rect.height() * (self.HORIZON_UP + lift)
            + math.sin(self._wobble * 2.7) * shake)
        # Negative on a right-hand bend, which is the way round it has to
        # be: leaning right tips the camera's up-vector right, so the
        # world turns the other way and the right-hand end of the horizon
        # comes *up*. Qt's positive rotation takes it down.
        tilt = max(-self.TILT, min(self.TILT, -self._banked * self.TILT))
        # Not the corkscrew, which turns the world round the road rather
        # than the road: see ``paint``.
        return horizon, focal, tilt

    def _from_track(self, at: float, push: float) -> tuple:
        """The road where the track says it goes. See ``_shape``.

        Straight behind the rider for the same reason the free-running
        one is: that part of the road is magnified two hundred times and
        nobody can use it.
        """
        line = max(at, self.RIDER_AT)
        when = self._when(line)
        across = self._read(self._curve, when) * self.TRACK_BEND * push
        # The turn is what the curve is doing here, which is what the
        # road banks into. Read over a step of road rather than
        # differentiated, because the readings are a few a second and the
        # difference between two of them *is* the slope.
        on = self._read(self._curve, self._when(line + 1.0))
        turn = (on * self.TRACK_BEND * push - across)
        return (across,
                self._read(self._hill, self._when(at)) * self.CLIMB * push,
                -turn * self.BANK)

    def _eye(self, horizon, focal, lane_x: float, up: float, at: float):
        """A point on the road, on the glass.

        Heights are measured from the road under the rider, not from the
        world, which is the difference between a road and a glitch. The
        eye sits ``EYE_UP`` above the world floor; a passage that lifted
        the whole road by nearly that much brought it up to eye level,
        where everything from here to the horizon lands within a few
        pixels of the same row. Measured across every phase of the hill
        the road ahead spanned anywhere from 265 px down to *minus* 35 -
        negative, meaning the far end drew below the near end and the
        road folded over on itself.

        Subtracting the road under the rider pins the near end where it
        belongs and leaves the hills as what they are, the shape of the
        road ahead: the same sweep now spans 62 to 168 px, right way up
        throughout.
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

    def _advance(self, state) -> float:
        now = time.monotonic()
        step = 0.016 if self._last is None else min(0.1, max(0.0,
                                                             now - self._last))
        self._last = now
        kit = {name: bounded(value)
               for name, value in (state.kit or {}).items()}
        bass = max(bounded(state.bass), kit.get("Bass", 0.0))
        loud = (bass + bounded(state.mid) + bounded(state.high)) / 3.0

        # The playhead, which everything here is measured from. A player
        # that reports a nan position would otherwise hand it to the
        # road, the chart and the grid in one frame.
        said = bounded(getattr(state, "at", 0.0), most=self.LONGEST)
        # Whether the track is actually playing. A paused player reports
        # the same position every frame, and the road went on rolling
        # under a stopped song: "xxxxxx xxxxxx xxxxxx xxxx xxxx xxxxxx".
        first = self._was_at is None
        moving = first or abs(said - self._was_at) > 1e-4
        self._was_at = said
        self._rolling += ((1.0 if moving or said <= 0.0 else 0.0)
                          - self._rolling) * 0.25
        # The music's own clock. Smoothed between the player's reports,
        # because they arrive a few a second and the road runs at sixty -
        # but never moved on its own while the track is stopped. It used
        # to creep forward by ``step`` whatever was happening and get
        # yanked back whenever it drifted a third of a second from the
        # playhead, which is a road that crawls forward and snaps back
        # over and over: "road continues moving and obstacles xx xxxxxx
        # xxx xxxxxx xxx xxxxxx xxxx xx xxxxx xxxxxxxx xxxxxxxx xxx xxxx
        # xx xxxxxx xxx xxxxxx xx x xxxx".
        jumped = False
        if said <= 0.0:
            self._heard += step
        elif abs(said - self._heard) > 0.35:
            self._jumped_from = self._heard
            self._heard = said       # a seek, or the first frame
            jumped = True
        elif moving:
            self._heard += step * self._rolling + (said - self._heard) * 0.06
        # A seek is a jump from somewhere the run has been. The first
        # frame is not one - there is nothing behind it to skip or to go
        # back over - but a run that starts part way in is not the whole
        # track either. See _finish.
        self._jumped = jumped and not first
        if first and self._heard >= self.START_AGAIN:
            self._whole = False

        # Truthiness is not a tempo test: a nan is true, and a nan beat
        # is a nan road position, a nan block distance and finally an
        # int() of a nan, which is a window that closes. Bounded at a
        # thousand because the road is laid a beat at a time and a beat
        # of a millionth of a second is a million figures a second.
        tempo = bounded(getattr(state, "tempo", 0.0), most=1000.0)
        self._beat = 60.0 / tempo if tempo > 0.0 else 0.0
        self._pulse = bounded(getattr(state, "beat_at", 0.0))
        if self._beat > 0.0 and said > 0.0:
            # One known beat, so that a figure can be put exactly on the
            # grid rather than wherever the detector heard a drum. See
            # ``_snap``. Re-derived every frame, which keeps it exactly
            # on the phase the analysis reports.
            self._grid = said + (1.0 - self._pulse) * self._beat
            # And a *fixed* one to measure distance from, which is not
            # the same thing at all: the snapping reference walks forward
            # with the playhead, and a distance measured from something
            # that walks with you is always the same distance. Set once,
            # and thereafter only nudged to keep its phase - a correction
            # of a fraction of a beat, never a whole one, so the beat a
            # moment falls on cannot change under it.
            start = said - self._pulse * self._beat
            if self._origin is None or jumped:
                self._origin = start
            elif moving:
                # Only while the track is playing. This is a correction
                # towards the phase the analysis reports, and with the
                # track stopped there is nothing to correct towards - but
                # the correction still had somewhere to go, because the
                # error it is easing away from sits at a fixed fraction
                # of a beat and never reaches zero. The road is measured
                # from here, so it crept ten units a second under a
                # stopped song.
                off = (start - self._origin) / self._beat
                self._origin += (off - round(off)) * self._beat * 0.1
        # From here on, a frame's worth of movement is however much of a
        # frame the *track* moved. Everything the scene animates reads
        # this rather than the wall clock, so a stopped track stops the
        # lot: "xxxxxx xxxxx xxx xxxxxxxxxx xxxxxx xxxx xxxx xxxxx xxxxx
        # xx xxxxxx". The road already did; the shake, the sparks, the
        # field behind it, the fuse under the grid and the hurt from a
        # hit all had clocks of their own.
        step *= self._rolling
        # How loud this passage is, on the track's clock as well.
        #
        # These are envelope followers and they converge on whatever they
        # are fed, so a held level walks them somewhere: the fast one
        # settles on it, the slow one follows, and the peak decays
        # towards them. What comes out is the *surge*, which colours the
        # road, opens the field of view and lights the air - so a stopped
        # track went on slowly changing colour and brightness. Measured,
        # four per cent of the frame changed from one frame to the next
        # and it grew from there.
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
        # What the road's bends are scaled by: the loudness, followed
        # slowly. The loudness itself answers an attack at once, which is
        # right for a flash and wrong for the shape of the road - scaled
        # by it, every bend in view moved at once when the music hit, and
        # the first note after a silence always reads as the loudest yet,
        # so a second into a song the whole road jumped sideways by a
        # fiftieth of the screen in one frame. Most of the way there in
        # about a second, on the track's clock like everything else.
        self._pushing += ((self._loudness - self._pushing)
                          * (1.0 - math.exp(-step / self.PUSH_EASE)))
        self._slow = min(1.0, self._slow + self.SLOW_BACK * self._rolling)
        # The lunge is chosen once a beat and held for the whole of it.
        #
        # It shapes where the road is *within* a beat, so changing it
        # part-way through moves the road - and the road may only ever go
        # forwards, so it stops instead and waits for the curve to catch
        # up. Measured on a real track, the slowest the road ran was
        # zero: "I don't xxxx xxx xxx xxxx xxxxxx xxxxx xxxxxxx xxxxx".
        # Changing it only on a beat boundary costs nothing, because
        # every curve in the family agrees there: they are all zero at
        # the start of a beat and one at the end.
        #
        # And not at all while the track is stopped, because the bass
        # goes on easing after a pause and the road would move without
        # the beat having.
        beat_now = self._beat_number(self._heard)
        if self._rolling > 0.02 and beat_now != self._lunge_from:
            self._lunge_from = beat_now
            self._lunge = 1.0 + bass * self._slow * self.LUNGE
        was, was_when = self._at, self._last_heard
        self._last_heard = self._heard
        rolled = self._world(self._heard)
        # Never backwards. The curve is chosen by the push, so a push that
        # moves within a beat moves the whole mapping and the road can be
        # asked to stand where it stood two frames ago. Beats only go
        # forwards, so neither does the road - except across a seek, which
        # is the one time it may.
        self._at = rolled if jumped else max(self._at, rolled)
        # Against the track's own clock, not the frame's: the frame's is
        # gated by whether the track is playing, so dividing by it at a
        # pause divides by nothing.
        went = self._heard - (was_when if was_when is not None
                              else self._heard)
        # Nothing across a seek: the road is measured from an origin and
        # a seek re-bases it, so the step across one is a change of
        # coordinates rather than a distance travelled.
        self._speed = (0.0 if jumped or went <= 1e-6
                       else (self._at - was) / went)
        self._drift_sparks(step)
        self._bend += step * (0.30 + self._loudness * 0.85)
        self._climb += step * (0.19 + self._loudness * 0.55)
        # Once a frame, after the road has moved: everything drawn this
        # frame measures its height and its line from here.
        #
        # Sideways as well as up. The eye used to sit at world zero while
        # the road wandered left and right past it, so a bend did not
        # curve away ahead of you - it dragged the whole road across the
        # frame and took the near end off one corner. Audiosurf pins the
        # camera to the spline, x = 0, and the bend is then what it
        # should be: the road ahead curving, with the part under you
        # straight.
        self._under = self._road(self.RIDER_AT)[1]
        self._side = self._road(self.RIDER_AT)[0]
        self._shake = max(0.0, self._shake - self._shake * self.SHAKE_FALL
                          - step * 0.9)
        self._shake = min(1.0, self._shake + kit.get("Kick", 0.0) * 0.35)
        self._sore = max(0.0, self._sore - step)
        self._hurt = max(0.0, self._hurt - step / self.HURT_FOR)
        self._got = max(0.0, self._got - step / 0.35)
        self._shield = min(1.0, self._shield + step / self.SHIELD_BACK)
        self._coin_spin += step * self.COIN_TURN
        self._fly(step)
        self._age_pops(step)
        # How close the track is to a beat, 1 on it and falling away -
        # held while the track is stopped rather than recomputed.
        #
        # The pane's clock closes on the playhead asymptotically, so a
        # stopped track still creeps a hair every frame, and everything
        # the beat lights moved with it. That was invisible while the
        # beat only fed things held near the floor; the rim is a
        # gradient across the whole frame and it showed up as a pixel
        # changing between two frames a second apart under a stopped
        # track. Stopped means stopped.
        if self._rolling > 0.02:
            self._beat_lit = ((1.0 - self._pulse) ** 3
                              if self._beat > 0.0 else 0.0)
        # How far over the road is turned, if it is turning at all.
        # Worked out here rather than in the camera because the camera
        # is asked for an answer more than once a frame.
        through = self._twist_at(self._heard)
        self._rolled = 0.0 if through is None else self._turned(through)
        #: How much of a sixtieth of a second this frame was worth on the
        #: track's clock. The rig reads it: a shake counted in frames is
        #: a different shake on every machine. See ``_slide``.
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

    #: Audiosurf's Mono scoring, which is a chain rather than a tally.
    #:
    #: The first coloured block is worth one, and every one after it four
    #: more - 1, 5, 9, 13 - up to two hundred. Hitting a grey breaks the
    #: chain and the next colour starts at one again. What that does to
    #: how it plays is the whole point: a run of forty clean blocks is
    #: worth more than four runs of ten, so a grey costs far more than
    #: the points it does not give you.
    CHAIN_FIRST = 1
    CHAIN_STEP = 4
    CHAIN_MOST = 200
    #: And finishing without touching one is worth a third again.
    CLEAN_BONUS = 0.30

    #: And what Ninja pays for the same thing, which is worth more
    #: because there is so much more of it to get past.
    #:
    #: Audiosurf 2 has Mono's clean finish at 10 per cent and Ninja's
    #: stealth bonus at "20 per cent or more" - twice as much, for the
    #: mode with the spikes in it. Audiosurf 1's blueprint puts Mono's
    #: at a flat 30, which is the number this road already used, so
    #: Ninja's is twice that.
    STEALTH_BONUS = 0.60

    #: Mono's bumpers: how long one takes to come back after it has
    #: shattered a grey, and what using one costs.
    #:
    #: Audiosurf's Mono rides with side-lane bumpers that shatter a grey
    #: safely, once, and then need time. Without them a single mistake
    #: forty blocks into a chain takes the whole chain, which is a game
    #: that punishes one slip more than it rewards a good minute. With
    #: them the first slip costs the shield and the second costs the
    #: chain, and the eight seconds between are played differently -
    #: which is the tension the mechanic is for.
    #:
    #: It does not save the clean-finish bonus. Shattering a grey is
    #: still touching one.
    SHIELD_BACK = 8.0

    def _collide(self) -> None:
        # Over the lot of it. A jump clears whatever is in the lane and
        # collects none of it either: it is not a way past the hard
        # parts, it is a trade. See JUMP_UP.
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
                # Taken, or missed. A coin is worth more than the one
                # before it and missing one puts the row back to nothing,
                # which is what makes a trail worth holding the lane for
                # rather than clipping the end of.
                if on_it:
                    self._record(block, "taken")
                    self._coins += 1
                    self._coin_run += 1
                    self._coin_best = max(self._coin_best, self._coin_run)
                    self._score += min(
                        self.COIN_MOST,
                        self.COIN_WORTH + (self._coin_run - 1) * self.COIN_STEP)
                    self._got = 1.0
                    self._burst(self._lane_at(lane), prize=True)
                    # Gold, and bigger the longer the row: a trail taken
                    # whole should feel like more than three of one.
                    self._pop("coin", hue=0.13, sat=0.60,
                              strength=0.75 + min(0.75,
                                                  self._coin_run * 0.12))
                else:
                    self._coin_run = 0
                continue
            if not grey:
                self._offered += 1
            if grey and on_it:
                if self._sore > 0.0:
                    continue
                if self._shield >= 1.0:
                    # Shattered rather than hit. It still counts as
                    # having touched one, so the clean run is over.
                    self._shield = 0.0
                    self._saves += 1
                    self._record(block, "shatter")
                    self._clean = False
                    self._sore = self.SORE
                    self._shake = min(1.0, self._shake + 0.35)
                    self._burst(self._lane_at(lane), prize=True)
                    # Saved, and it should look like being saved: a
                    # cold white ring rather than the red of a hit.
                    self._pop("shatter", hue=0.55, sat=0.30,
                              text="SHIELD")
                    continue
                lost = self._chain
                self._record(block, "hit")
                self._hits += 1
                self._streak = 0
                self._chain = 0
                self._coin_run = 0
                self._clean = False
                self._sore = self.SORE
                self._shake = min(1.0, self._shake + 0.8)
                self._slow = self.SLOW
                # "Xxxx xxx xxxxxx xxxxxx xxxxx xx xx obstacle hit."
                # Not the block, not the ship: the picture.
                self._hurt = 1.0
                self._burst(self._lane_at(lane))
                # And say what it cost, when it cost something. A chain
                # of forty going is the worst thing that can happen on
                # this road and it used to look exactly like a chain of
                # two going.
                self._pop("hit", hue=0.0, sat=0.95,
                          strength=1.0 + min(0.5, lost / 80.0),
                          text=(f"CHAIN LOST  {lost}"
                                if lost >= self.LOST_WORTH else ""))
            elif grey:
                self._streak += 1
                self._best = max(self._best, self._streak)
            elif on_it:
                if self._mode == "Puzzle":
                    # Worth nothing on its own: it goes in the grid, and
                    # three of a colour touching is what pays.
                    if self._stunned <= 0.0:
                        self._record(block, "taken")
                        self._taken += 1
                        self._drop(self._tier_of(when), lane)
                        self._got = 1.0
                        self._burst(self._lane_at(lane), prize=True)
                        self._pop("prize", strength=0.8)
                else:
                    # A prize. See CHAIN_FIRST.
                    before = self._chain
                    self._record(block, "taken")
                    self._chain += 1
                    self._taken += 1
                    self._chain_most = max(self._chain_most, self._chain)
                    self._score += int(min(
                        self.CHAIN_MOST,
                        self.CHAIN_FIRST
                        + (self._chain - 1) * self.CHAIN_STEP) * self._double)
                    self._double = 1.0
                    self._got = 1.0
                    self._burst(self._lane_at(lane), prize=True)
                    # Bigger as the run gets hotter, so the run is felt
                    # in every prize and not only at the milestones.
                    self._pop("prize", strength=0.7 + self._heat() * 0.6)
                    self._milestone(before, self._chain)

    # -- the grid ---------------------------------------------------------
    #: Audiosurf's matrix, and the half of the game the road is the other
    #: half of. Blocks you collect do not score on their own: they drop
    #: into a grid of three columns, and three or more of a colour
    #: touching each other clear it and pay.
    #:
    #: Three wide because there are three lanes, and six deep for Casual
    #: and Pro - Elite gets seven, which is not a difficulty this has.
    CELLS_WIDE = 3
    CELLS_DEEP = 6

    #: What a colour is worth, by the tier of the passage that produced
    #: it: purple, blue, green, yellow, red. A block from a chorus is
    #: worth eight of one from an outro.
    WORTH = (10, 20, 30, 50, 80)

    #: How long a cluster sits before it goes, and what joining it does.
    #:
    #: Three quarters of a second, reset every time another block of the
    #: same colour touches it. That window is the whole skill of the
    #: game: it is what turns three blocks into nine.
    FUSE = 0.75

    #: What an overfilled column costs. The grid locks for three seconds,
    #: nothing can be collected, and the chain goes.
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
        """Every run of three or more of one colour that touch.

        A flood fill over the cells, four-connected: blocks joined corner
        to corner are not joined at all, which is the rule that makes the
        grid a puzzle rather than a soup.
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
        """Start or restart the fuse if anything is matched.

        Restarted rather than left running, which is what lets a cluster
        be grown: another block of the same colour landing against it
        gives you the whole window again.
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
            # Quadratic in the size, so one cluster of six is worth
            # twice two of three - which is what makes the fuse window
            # worth playing for rather than clearing on sight.
            #
            # And doubled if a power block is waiting to be spent, which
            # is what one is for: it is worth carrying through a
            # corkscrew and cashing on a big cluster rather than on the
            # next thing that happens to clear.
            self._score += int(self.WORTH[colour] * len(group) * len(group)
                               * self._double)
            self._cleared += len(group)
        # A cluster going is the puzzle game's payout, and a big one is
        # the biggest thing that game does - quadratic in the size - so
        # it gets the biggest answer, and a word when it is worth one.
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
        """The score with the clean-finish bonus in, if it is still kept:
        what the run is worth if it ends now. The end of the track pays it
        (see _results) and the bests keep it.

        It is not what the running score shows. That showed this, and so
        dropped by a quarter the moment a grey was touched - including
        the moment the shield saved you, which is the one time the game
        says you did well. The running score is what has been earned;
        what a clean run would add is said beside it (see bonus), and
        paid at the end, as Audiosurf pays it.
        """
        if not self._clean:
            return self._score
        return int(self._score * (1.0 + self.bonus()))

    def bonus(self, mode: Optional[str] = None) -> float:
        """The share a clean finish adds, in ``mode`` or this one."""
        return (self.STEALTH_BONUS if (mode or self._mode) == "Ninja"
                else self.CLEAN_BONUS)

    def _burst(self, across: float, prize: bool = False) -> None:
        """Throw pieces off the block that was just taken or hit.

        A prize throws fewer and throws them up: a shower rather than a
        wreck, so that the two read differently at a glance.
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
        if self._beat <= 0.0 or self._origin is None:
            return None
        return math.floor((when - self._origin) / self._beat)

    def _world(self, when: float) -> float:
        """Where the road is at a moment of the track, in road units.

        The one clock. A beat is PER_BEAT units long, so a block laid on
        beat n sits at n * PER_BEAT and the road reaches it exactly on
        beat n - which is what makes the dodge land on the beat rather
        than near it.

        Inside a beat the travel is front-loaded by the bass. That is the
        whole of the speed control now, and it costs nothing in timing:
        ``1 - (1 - t) ** k`` is zero at zero and one at one, so however
        hard it lunges, a beat still covers exactly one beat's worth of
        road and arrives exactly on time.
        """
        if self._beat <= 0.0 or self._origin is None:
            return when * self.FREE_RUN
        beats = (when - self._origin) / self._beat
        whole = math.floor(beats)
        through = beats - whole
        # Part lunged and part even.
        #
        # All lunge is a road that stops: the curve's slope at the end of
        # a beat is zero however hard it lunges, so the last frames of
        # every beat travelled 0.06 units a second against a mean of 12
        # - "I don't xxxx xxx xxx xxxx xxxxxx xxxxx xxxxxxx xxxxx".
        # Mixing in a straight run puts a floor under it, and costs
        # nothing in timing because both curves are zero at zero and one
        # at one: a beat still covers exactly one beat's worth of road
        # and arrives exactly on time.
        lunged = 1.0 - (1.0 - through) ** self._lunge
        went = through * (1.0 - self.LUNGE_MIX) + lunged * self.LUNGE_MIX
        return (whole + went) * self.PER_BEAT

    def _where(self, when: float) -> float:
        """How far down the road a hit due at ``when`` is now.

        The difference between where the road has got to and where that
        moment of the track sits on it. Zero difference is the rider, so
        a block is level with them exactly on its beat.
        """
        return self.RIDER_AT + self._flat(when) - self._at

    def _flat(self, when: float) -> float:
        """Where a moment sits on the road, with no lunge in it.

        A block's place is fixed when it is laid; only the road moves.
        Putting the lunge in here as well would shuffle every block on the
        road every time the bass changed.
        """
        if self._beat <= 0.0 or self._origin is None:
            return when * self.FREE_RUN
        return (when - self._origin) / self._beat * self.PER_BEAT

    # -- drawing ----------------------------------------------------------
    def _slide(self, step: float) -> float:
        """How much of the way to the wanted lane this frame is worth.

        SNAP is a share of the remaining distance per sixtieth of a
        second rather than per frame. Taken per frame, the dodge window
        is whatever the pane is managing: the same lane change takes
        50 ms at 60 fps, 100 ms at 30 and 25 ms at 120, so a busy
        machine plays a slower game than a quiet one and the blueprint's
        "incredibly tight interpolation window" holds on neither.

        Off the track's clock, so a stopped track slides nowhere.
        """
        if step <= 0.0:
            return 0.0
        return 1.0 - (1.0 - self.SNAP) ** (step * 60.0)

    def _step(self, state) -> float:
        """The game, one frame on: everything that happens before any of
        it is drawn, whichever way it is drawn."""
        self._carve(state)
        step = self._advance(state)
        self._lay(state)
        wanted = self._lane_at(self._lane)
        was_across = self._lane_here
        self._lane_here += (wanted - self._lane_here) * self._slide(step)
        # How hard the craft is moving sideways, for the bank. Against
        # the track's own clock so it means the same on every machine,
        # and eased so the craft rolls out of a move rather than
        # snapping flat the instant it arrives.
        if step > 0.0:
            self._swerve += ((self._lane_here - was_across) / step
                             - self._swerve) * self.SWERVE_EASE
        self._collide()
        self._finish(state)
        return step

    def _record(self, block, how: str) -> None:
        # The block itself is kept with its outcome, not only its id: an
        # id is only unique while the thing is alive, and a block laid
        # after one was dropped can be given the same one.
        self._struck[id(block)] = (block, how)

    def _forget_struck(self) -> None:
        # Held here, a block keeps its id to itself, so a block on the
        # road with that id is this one.
        alive = {id(block) for block in self._blocks}
        self._struck = {key: kept for key, kept in self._struck.items()
                        if key in alive}

    def struck(self, block) -> Optional[str]:
        """What the craft did to ``block``, as the game scored it: "taken",
        "hit", "shatter" (the shield took it), or None - it went by, was
        jumped over, or came while the craft could not be hurt.

        The picture asks this rather than working it out from where the
        craft was, so that what is drawn, what is heard and what is scored
        cannot disagree: a prize jumped over was drawn going into the ship
        and scored nothing.
        """
        kept = self._struck.get(id(block))
        return kept[1] if kept is not None and kept[0] is block else None

    #: How close to the end of the track counts as the end: a player
    #: stops a few frames short, and waits for nothing after the last.
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
        """The end of the track is the end of the run, and the start of
        the track after it is a new one.

        So is going back to the start part way through. Any other seek -
        or starting part way in - leaves a run that is not the whole
        track: it plays on and is judged, but no best is kept for it, or
        a ride of the last minute would stand as the track's best.
        """
        if self._jumped:
            if (self._heard < self.START_AGAIN
                    and self._jumped_from >= self.START_AGAIN):
                mode = self._mode
                self.reset()
                self._mode = mode
                return
            if self._heard >= self.START_AGAIN:
                self._whole = False
        length = self._length(state)
        if length <= 0.0:
            return
        at = bounded(getattr(state, "at", 0.0), most=self.LONGEST)
        if self._finished:
            if at < min(2.0, length * 0.5):
                # Back to the start: a new run, in the same game.
                mode = self._mode
                self.reset()
                self._mode = mode
            return
        if at >= length - self.FINISH_BEFORE:
            self._finished = True
            self._result = self.result()
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
            "whole": self._whole,
        }

    #: The grades, best first: the share of the prizes taken, and the
    #: most greys hit, to earn each.
    GRADES = (("S", 0.95, 0), ("A", 0.85, 2), ("B", 0.70, 5), ("C", 0.50, 10))

    @classmethod
    def grade(cls, share: float, hits: int) -> str:
        """A letter for a run. Both have to be earned: taking everything
        while hitting everything is not an S."""
        for letter, least, most in cls.GRADES:
            if share >= least and hits <= most:
                return letter
        return "D"

    def paint_on_card(self, painter, rect, state, world) -> None:
        """The same game, drawn as a lit world on the graphics card.

        See rider_gl. The game moves on exactly as it does in ``paint``;
        only the drawing differs, and the words and numbers over the top
        are still drawn with the painter, which is what draws type well.
        """
        import time as _time

        from PySide6.QtCore import QRect
        from PySide6.QtGui import QOpenGLContext

        self._step(state)
        kit = state.kit or {}
        bass = max(state.bass, kit.get("Bass", 0.0))
        surge = self._loudness
        # Kept moving, because the eye's springs and the wobble a hit
        # throws are read by the world too.
        self._camera(rect, surge, bass)
        self._hue_now = self._tier(surge, state.synth)
        device = painter.device()
        ratio = device.devicePixelRatioF() or 1.0
        box = painter.worldTransform().mapRect(rect)
        # The target's height in its own pixels, which is what a GL
        # viewport counts from the bottom of. A GL paint device reports
        # its size in pixels already; anything else reports points.
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
        self._step(state)

        flash = self.flash(state)
        surge = self._loudness
        kit = state.kit or {}
        bass = max(state.bass, kit.get("Bass", 0.0))

        painter.fillRect(rect, QColor(3, 2, 8))
        # The same morphing field the Ambience scene is built on, behind
        # everything and dim: "xxxx xxx xxxxxxxxxx xx xxxxx xxxxx xxxx
        # xxxxxxx xx xxx xxxxxxxx xxxxxxxxxx". It reads the same state, so
        # it moves with the music on its own.
        # Dim. It is the room the road is in, not the subject: at the
        # strength Ambience uses it for its own sake it drowns the track.
        self._plasma.paint(painter, rect, state,
                           strength=0.07 + surge * 0.07 + flash * 0.05,
                           flash=flash, going=self._rolling)

        horizon, focal, tilt = self._camera(rect, surge, bass)
        hue = self._tier(surge, state.synth)
        self._hue_now = hue
        # How close the track is to a beat, 1 on it and falling away.
        # Worked out on the track's clock: see ``_advance``.
        beat = self._beat_lit

        # The whole view banks into the bend. One transform around the
        # horizon, so everything drawn after it leans together.
        # And a hit throws it. A high-frequency roll on its own clock for
        # as long as the hit lasts, on top of the lean: the blueprint's
        # stun shake, which uncouples the camera from its own smoothing
        # so that a failure is felt rather than noticed.
        lean = tilt + (math.sin(self._wobble * 2.3) * self._hurt
                       * self.HURT_THROW)
        # A corkscrew turns the world round the road, all the way, and
        # not the road: the glow and the gates either side go round, and
        # the road, what is on it and the craft stay where they are on
        # the glass. It used to turn all of it together about the
        # horizon, and the craft went round the frame with it - at the
        # top of the picture halfway through, which is the one thing a
        # player steering it needs never to move. The lit world does the
        # same with the road itself wound round (see rider_gl._twist);
        # flat, the world going round behind a steady road is the part
        # that can be drawn. The world is drawn first so that the road
        # passes in front of it as it turns.
        painter.save()
        painter.translate(horizon)
        painter.rotate(lean + self._rolled * 360.0)
        painter.translate(-horizon)
        self._glow(painter, rect, horizon, hue, surge, bass, beat, flash)
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
        # Where the craft landed on the glass, through the bank and the
        # throw that everything above was drawn inside, for the answers
        # below to come from.
        if self._craft_spot is not None:
            self._craft_glass = QTransform().translate(
                horizon.x(), horizon.y()).rotate(lean).translate(
                -horizon.x(), -horizon.y()).map(self._craft_spot)
        painter.restore()
        # The beat, where nothing is read against anything. On the grid
        # and on the drum both: the grid keeps it in time through a bar
        # the drummer left alone, and the drum makes it land on what was
        # actually played.
        self._rim(painter, rect, hue,
                  max(beat, kit.get("Kick", 0.0)))
        self._pops_now(painter, rect)
        self._wash(painter, rect)
        if self._mode == "Puzzle":
            self._matrix(painter, rect)
        self._card(painter, rect, hue)
        self._results(painter, rect)

    #: Where the grid sits and how big it is, as shares of the frame.
    #: Bottom left, out of the road's way: the road runs up the middle
    #: and the eye that is reading it is at the top.
    CELL_AT = (0.035, 0.96)
    CELL_SIDE = 0.038
    CELL_GAP = 0.15

    def _matrix(self, painter, rect) -> None:
        """Audiosurf's grid, in the corner.

        Drawn from the bottom up, because that is the way it fills: a
        column you can see the top of is a column with room in it, which
        is the only thing you need to read off this at speed.
        """
        side = min(rect.width(), rect.height()) * self.CELL_SIDE
        step = side * (1.0 + self.CELL_GAP)
        left = rect.left() + rect.width() * self.CELL_AT[0]
        floor = rect.top() + rect.height() * self.CELL_AT[1]
        painter.setPen(Qt.PenStyle.NoPen)
        # The well first, so an empty column still reads as a column.
        stunned = self._stunned > 0.0
        # Flashing while it is locked, which is the one thing here that
        # has to be noticed rather than read.
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
                # A cluster that is about to go pulses, so the window to
                # grow it is visible rather than remembered.
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
        """What a hit does to the whole picture.

        "Xxxx xxx xxxxxx xxxxxx xxxxx xx xx obstacle hit." A shake says
        something happened to the camera. A frame that goes red from its
        edges in, with the light dropped out of everything under it, says
        something happened to *you*.

        Two fills a frame and only while it is fading, so it costs
        nothing the rest of the time.
        """
        if self._hurt <= 0.0:
            return
        # Strongest at the moment of the hit and gone in HURT_FOR, with
        # the curve front-loaded so it lands hard and lets go.
        hurt = self._hurt * self._hurt
        # Multiplied, not washed over.
        #
        # Laying red over the picture can only add light to it, and on a
        # world this dark that is a flashbulb: measured, the frame came
        # out twice as bright after a hit as before one. Multiplying by a
        # red takes the green and the blue out of everything and leaves
        # the red where it was, so the frame goes red *and* dark, which
        # is what damage looks like.
        painter.save()
        painter.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Multiply)
        painter.fillRect(rect, QColor(
            255,
            int(255 - (255 - 46) * hurt * self.HURT_DIM),
            int(255 - (255 - 38) * hurt * self.HURT_DIM)))
        painter.restore()
        # And a rim of red light from the edges in, which is the part
        # that reads as a blow rather than as a filter.
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
        """The road itself, filled.

        "Make the track opaque." One path of quads between consecutive
        rungs, filled once: a road drawn as lines is a ladder floating in
        the dark, and blocks standing on nothing read as floating too.
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
        # Into fog rather than to a point.
        #
        # The road ends somewhere, and where it ended it was a hard little
        # vertex: the whole thing read as a cone with a tip rather than as
        # a road going away. Filled with a gradient down its length, the
        # far end simply stops being there.
        # Dark, and it has to stay dark.
        #
        # A block and the road it stands on used to differ in hue and not
        # in brightness: measured on a real track, an orange prize came
        # out at a luminance of 0.400 on a road at 0.401 - a contrast of
        # one to one, which is not dim, it is invisible. Hue alone does
        # not separate two things at speed. So the road is held down near
        # the floor and the blocks are the only bright thing on it.
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

    #: How far down the road the fog has finished clearing, as a share of
    #: the way from the far end to the rider.
    #: How light the road's surface is. See ``_surface``.
    ROAD_LIT = 0.085

    FOG = 0.35
    #: How solid a block is at the far end of the road.
    #:
    #: The fog is there so blocks come out of the distance rather than
    #: appearing whole, and it used to take them all the way to nothing.
    #: Measured against the background right behind it, a block two and
    #: a half beats out read at 1.14 to 1 - which is not dim, it is
    #: invisible: "xxx xxxxxx xxx xxxxxxxxxx xx xxx xxxxxxxxx". Three to
    #: one is the usual floor for something this size, and a block has to
    #: be read while there is still time to move.
    FOG_LEAST = 0.55

    #: How far apart the chevrons under the road are, in road units, and
    #: how many rungs of it each one covers.
    MARK_EVERY = 2.0

    def _markings(self, painter, horizon, focal, hue, kit, flash) -> None:
        """The pattern on the road, which moves under you.

        "Xxx xxxxxxxx xxxxx xxx xxxxx xxxx xxxxx xx xxxxx." Chevrons at a
        fixed spacing in road units, so they stream towards you at the
        road's own speed, brightening on the kick.
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
        """The rails either side, which answer the kit.

        "Xxx xxxxxxx xxxxx xxx xxxxx xx xxx xxxxx xxxx xxxxx xx xxxxxxxx
        xx xxx xxxxx xx xxxx." The left rail is the snare and the right is
        the hats, so the two sides of the road are doing different things
        and the difference is the music.
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
        """The blocks, filled, with a lit edge.

        "Make obstacles xxxxxx xxxx xxxxxx xx xxx xxxxx." Filled so they
        sit on the road rather than floating over it, and outlined bright
        so they read against it.
        """
        edge = self.LANE_WIDE * 0.5 * 0.82
        tall = 0.62
        painter.setPen(Qt.PenStyle.NoPen)
        # Grey first and coloured second, so the prizes are laid over
        # the obstacles where they overlap - which is the way round that
        # tells you the lane is worth taking.
        for grey_now in (True, False):
            for kind in ("wall", "block", "run"):
                self._blocks_of(painter, rect, horizon, focal, hue, flash,
                                kind, grey_now, edge, tall)
        # And the coins over the top of both, because a coin sits beside
        # an obstacle and the thing you need to see is which lane it is
        # in.
        self._coins_now(painter, horizon, focal, flash)

    #: How wide a coin is, how high off the road it floats, and how fast
    #: it turns. Small: it is a reward for being in a lane rather than
    #: something to steer at, and a coin the size of a block would hide
    #: the obstacle it is next to.
    COIN_SIZE = 0.26
    COIN_TALL = 0.40
    COIN_TURN = 2.6
    #: How much bigger a power block is than a coin. It is the thing a
    #: corkscrew is *for*, so it is not something to notice late.
    POWER_SIZE = 1.9

    def _coins_now(self, painter, horizon, focal, flash) -> None:
        """The coins, as discs standing on the road and turning.

        White rather than the road's own colour. The road runs from
        purple at its quietest to red at its loudest and a coin has to
        read against every part of that - at a chorus the road is
        already gold - so a coin is the one thing in the scene with no
        hue at all, lit to the top of the scale. It is the greys' trick
        the other way round: colour against grey there, white against
        colour here.
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
            # Turned a little further than the coin before it, so a
            # trail of three reads as one object rolling rather than
            # three things flickering.
            phase = self._coin_spin + when * 5.0
            size = self.COIN_SIZE * (self.POWER_SIZE if power else 1.0)
            tall = self.COIN_TALL * (1.25 if power else 1.0)
            # Never edge-on to nothing: a disc exactly side on is one
            # pixel wide and reads as a coin that vanished.
            wide = size * max(0.28, abs(math.cos(phase)))
            # A true ellipse rather than a ring of points. Everything in
            # a coin sits at one distance, and at one distance the view
            # is a straight scale, turn and shift of the road - so a disc
            # lands on the glass as an ellipse exactly, and three points
            # say which. It had been ten points joined by straight lines,
            # which was round enough while the frame was drawn at half
            # the screen's resolution and read as a ten-sided shape once
            # it was drawn at all of it.
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
            # The same dark silhouette every block gets, for the same
            # reason: what a coin is read against is this rather than
            # whatever the music has put behind it.
            painter.fillPath(back, QColor(3, 2, 8, int(215 * seen)))
            painter.fillPath(face, QColor.fromHsvF(
                0.13, (0.02 if power else 0.20) - flash * 0.1, 1.0,
                min(1.0, 0.95 * seen)))
            self._beam(painter, face, QColor.fromHsvF(
                0.12, 0.25 if power else 0.55, 1.0,
                min(1.0, 0.6 + 0.4 * seen)))

    #: What a grey obstacle and a coloured prize are made of.
    #:
    #: Audiosurf's Mono mode is grey against colour and nothing else, so
    #: the two have to be unmistakable at the far end of the road. A grey
    #: is a grey: no hue worth the name and no light in it. A prize is
    #: the road's own colour at full strength, which is the tier the
    #: passage is in - red in a chorus, blue in a verse.
    #: How much wider than the block its dark backing is drawn.
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
            # Gone once it is behind the rider. Clamping it to NEAR
            # instead left everything that had already gone past
            # stacked against the bottom of the frame at the size of a
            # house, which is most of what the first attempt looked
            # like.
            if at < self.GONE or at > self.FAR:
                continue
            # Out of the fog with the road, rather than appearing
            # whole at the far end of it.
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
            # The block in the road under it. Squashed and dim, the
            # way a wet floor holds a light: one more quad a block,
            # and it is most of what makes them stand on the road
            # rather than hover over it.
            #
            # Every block that is drawn at all gets one. Dropping them
            # past a distance was tried: it took a third of the frame's
            # fills out and saved 0.2 ms of 8.9, and what it spent was
            # the thing that makes a block sit on the road at exactly
            # the distances a player is reading. Not a trade.
            pool = QPainterPath()
            pool.moveTo(foot_l)
            pool.lineTo(foot_r)
            pool.lineTo(self._eye(horizon, focal, across + edge,
                                  tall * self.MIRROR, at))
            pool.lineTo(self._eye(horizon, focal, across - edge,
                                  tall * self.MIRROR, at))
            pool.closeSubpath()
            # A dark silhouette under it first. Whatever is behind a
            # block - the lamp at the end of the road, a bright wash, the
            # plasma at a drop - this is what the block is actually read
            # against, so how well it reads stops depending on the
            # background at all.
            painter.fillPath(backs, QColor(3, 2, 8, int(225 * seen)))
            painter.fillPath(pool, QColor.fromHsvF(
                shade, wet, lit * 0.62, 0.30 * seen))
            # One path per block rather than one for the lot, because
            # each is a different distance into the fog.
            painter.fillPath(faces, QColor.fromHsvF(
                shade, wet, lit, 0.90 * seen))
            self._beam(painter, rims, QColor.fromHsvF(
                shade, max(0.0, wet - 0.45), 1.0,
                min(1.0, (0.85 + flash * 0.15) * seen)))
            # And an edge all the way round it, at full strength however
            # far away it is.
            #
            # A block at the far end of the road is a dozen pixels of a
            # colour that the lamp behind it has already washed out:
            # measured on real tracks, one eighteen units out read at
            # 1.05 to one against what surrounded it, which is
            # invisible. Everything else about a block fades with
            # distance, as it should - this does not, because it is the
            # thing that says a block is there at all.
            self._beam(painter, edges, QColor.fromHsvF(
                shade, max(0.0, wet - 0.55), 1.0,
                min(1.0, 0.55 + 0.45 * seen)))
            faces = QPainterPath()
            rims = QPainterPath()
            backs = QPainterPath()
            edges = QPainterPath()

    #: The horizon lamp: how far it reaches as a share of the frame, and
    #: how much the bass opens it.
    #: How far the lamp reaches, as a share of the frame, and how much
    #: the bass opens it.
    #:
    #: It sits at the vanishing point, which is exactly where a block is
    #: when there is still time to move out of its lane. Reaching over
    #: half the frame it did not light the end of the road, it erased it:
    #: measured on a real track, the brightest pixel of a block eighteen
    #: units out came to 1.02 against what surrounded it. A glow at the
    #: end of the road, not a sky.
    GLOW_REACH = 0.30
    GLOW_BASS = 0.10
    #: The most of the frame the lamp may take. It sits exactly where the
    #: road's far end is, which is where a block has to be read while
    #: there is still time to move: at full strength it washed that part
    #: of the picture out altogether.
    GLOW_MOST = 0.26

    # -- what the things you do look like ---------------------------------
    #: How the screen answers a run: a ring spreading from the craft, a
    #: flash of colour from the frame's edge, and for the moments that
    #: deserve one, a callout.
    #:
    #: Measured against the beat on the same road, the things a player
    #: did registered fifteen times weaker than the music did. A beat
    #: moved 43 per cent of the frame; taking a coin moved 2.7, a prize
    #: 2.3, and reaching a chain of forty 2.6 - indistinguishable from
    #: any other prize, so the moment a run became worth protecting was
    #: not a moment at all. A hit moved 13.5. The flag every collection
    #: set so that something could answer it had never been drawn.
    #:
    #: Screen space, outside the bank and the shake, because these are
    #: the game talking to the player rather than things in the world.
    #: Rings and flashes are gone inside half a second and mostly at the
    #: edges or around the craft, which is never what a block down the
    #: road is read against.
    #:
    #: Per kind: how long it lives, how far a ring spreads as a share of
    #: the frame, how strong the edge flash is, how bright the ring, and
    #: how thick.
    #:
    #: A hit had no edge flash of its own at first, on the grounds that
    #: the damage wash already reddens the edges - and measured, it
    #: moved 13.6 per cent of the frame, less than a coin. The wash
    #: *multiplies*, and a black frame multiplied by red is still black,
    #: so on this road it barely showed. The worst thing that can happen
    #: on the road now has the heaviest ring and a red flash of its own.
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
    #: Chains worth stopping the world for. Ten is the first that means
    #: anything, and past a hundred the chain is paying its cap, so the
    #: moments thin out rather than go on counting.
    MILESTONES = (10, 25, 50, 75, 100, 150, 200, 300, 500)
    #: A chain at least this long is worth telling somebody they lost.
    LOST_WORTH = 10

    def _pop(self, kind: str, hue: float = None, sat: float = 0.85,
             strength: float = 1.0, text: str = "") -> None:
        """Answer something the player did. See POPS."""
        if hue is None:
            hue = self._hue_now
        self._pops.append([kind, 0.0, max(0.0, min(1.5, strength)),
                           hue % 1.0, sat, text])
        # Never a queue of them: the newest few are all anybody sees.
        if len(self._pops) > 12:
            # The oldest go, but never the end of the track: it is what
            # the results card comes up from, and a burst of pickups on
            # the last beat is exactly when it would otherwise be pushed
            # out.
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
            # Always, and after the flash as well as the rings: a
            # painter left with a saved state takes the whole pane down
            # at the end of the frame, and one left with this pen and
            # brush draws whatever comes next in them.
            painter.restore()

    def _pop_rings(self, painter, rect, span, origin):
        """The rings and the words; the strongest edge flash, returned."""
        painter.setBrush(Qt.BrushStyle.NoBrush)
        flash = None
        for kind, age, strength, hue, sat, text in self._pops:
            life, spread, edge, bright, thick = self.POPS[kind]
            through = min(1.0, age / life)
            fade = (1.0 - through) ** 2
            # The edge flash is one fill for the lot of them: the
            # strongest wins, rather than every pop paying for a
            # gradient across the whole frame.
            if edge > 0.0:
                amount = edge * strength * fade
                if flash is None or amount > flash[0]:
                    flash = (amount, hue, sat)
            # The ring. Fast out and slowing, so it reads as something
            # thrown off the craft rather than something drawn round it.
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
        """A word across the upper middle of the frame, for a moment.

        Up fast and held, then gone: the size lands in the first tenth
        and the fade takes the last half, so it is read rather than
        glimpsed. Above the road's far end, where nothing is read.
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
        # A dark stroke under the word so it reads over anything the
        # music has put behind it.
        painter.setPen(QColor(0, 0, 0, int(200 * fade)))
        for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
            painter.drawText(box.translated(dx, dy),
                             int(Qt.AlignmentFlag.AlignHCenter
                                 | Qt.AlignmentFlag.AlignTop), text)
        painter.setPen(QColor.fromHsvF(hue, 0.45, 1.0, fade))
        painter.drawText(box, int(Qt.AlignmentFlag.AlignHCenter
                                  | Qt.AlignmentFlag.AlignTop), text)

    #: How hard the frame's own edge lights on the beat, and how far in
    #: from the edge it reaches.
    #:
    #: The beat had nowhere left to hit. Everything the road is made of
    #: is held near the floor on purpose - a block is read against the
    #: road, the lamp and the sky behind it, and brightening those is
    #: exactly how blocks became invisible the first time. Measured
    #: against the other scenes on the same 128 bpm track, the rider
    #: swung the picture's brightness 0.026 on a beat where the rave
    #: swung 0.140, and changed 7 per cent of the frame where the rave
    #: changed 97.
    #:
    #: So it hits where nothing is read: the edge. A rim of the road's
    #: own colour on the kick, transparent well before the middle, which
    #: is a lot of pixels doing something and none of them behind a
    #: block.
    RIM_MOST = 0.45
    RIM_REACH = 0.58

    def _rim(self, painter, rect, hue, punch) -> None:
        """The frame's edge, lit on the beat.

        Outside the bank and the shake, because it belongs to the
        picture rather than to the road: a rim that tilted with the
        camera would read as part of the world and this is the world
        hitting *you*.
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
        # One fill, not a ring of four. Skipping the transparent middle
        # was tried and is slower: four gradient fills of a band cost
        # more than one of the whole frame, because the per-call setup
        # is what dominates rather than the pixels.
        painter.drawRect(rect)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    @staticmethod
    def _ring_fill(painter, rect, middle, reach: float) -> None:
        """Fill only the part of ``rect`` a radial gradient can reach.

        A radial brush that is transparent outside ``reach`` still costs
        the whole rectangle to fill, and at 1920x1080 that is two
        million pixels for a lamp a few hundred across. It costs the
        whole rectangle for a transparent *middle* too. Measured, the
        two gradients in this scene were 3 ms of a 9.6 ms frame.

        So the rectangle is clipped to the circle's bounding box, which
        is the same picture: nothing is drawn in the part left out.

        Skipping a transparent *middle* the same way was tried and is
        slower - four gradient fills of a band cost more than one of the
        whole frame, because the per-call setup dominates rather than
        the pixels - so only the bounding box is worth taking.
        """
        box = QRectF(middle.x() - reach, middle.y() - reach,
                     reach * 2.0, reach * 2.0).intersected(rect)
        if box.isEmpty():
            return
        painter.drawRect(box)

    def _glow(self, painter, rect, horizon, hue, surge, bass, beat,
              flash) -> None:
        """A lamp at the end of the road, behind everything.

        The road runs into something rather than into nothing, and it is
        the cheapest depth in the scene: one gradient a frame.
        """
        reach = max(1.0, rect.height() * (self.GLOW_REACH
                                          + bass * self.GLOW_BASS))
        # The area to fill is worked out from the *widest* the lamp can
        # ever be, not from how wide it is now.
        #
        # Nothing in this frame settles exactly: the pane's levels ease
        # towards a held row asymptotically, so under a stopped track
        # the bass still creeps in the tenth decimal place and anything
        # measured from it creeps with it. A gradient's interior rounds
        # through that without moving, but the *edge of the area it is
        # painted into* is a step, and a step on a creeping number
        # changes. Measured: one pixel of a 640x360 frame, one step of
        # red, between two frames a second apart with the track
        # stopped. The widest case is a constant for a given frame, so
        # the area is one too.
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
        """The lines between the lanes.

        Without them the road is one slab and which lane you are in is a
        guess: "xxx xxxxxxx xxxxx xxxxxxx xxx xxxxx xx xxxxx xxxxx".
        Drawn brighter than the chevrons and dashed down the road, so they
        read as lane markings rather than as more decoration.
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
        """Gates down either side, passing at the road's own speed.

        What gives the road somewhere to be. They stand on the kick and
        light on the snare, so the two sides of the frame are doing
        something the music is doing.
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
                # A short arm turning in over the road, so a pillar reads
                # as a gate rather than as a stick.
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

    #: How long a run has to get before the craft is as hot as it gets,
    #: and how much of a lift that is worth.
    #:
    #: A chain of forty is worth far more than four of ten, and it felt
    #: exactly the same as a chain of one: a number in small text at the
    #: top of the frame. What a run is worth is the whole of Mono's
    #: scoring, so what a run is worth has to be something you can see
    #: without reading - and something you can feel yourself lose.
    #:
    #: Forty because that is the number the scoring is built around: at
    #: forty the chain is paying near its cap, which is the point at
    #: which a grey stops costing points and starts costing the run.
    CHAIN_HOT = 40.0
    HEAT_HALO = 1.7
    HEAT_HUE = 0.10

    def _heat(self) -> float:
        """How far into a run the craft is, from nothing to all of it."""
        if self._mode == "Puzzle":
            # The grid's game keeps no chain: what it is building is the
            # cluster sitting in the columns.
            return max(0.0, min(1.0, sum(len(pile) for pile in self._cells)
                                / max(1.0, self.CELLS_WIDE * self.CELLS_DEEP)))
        return max(0.0, min(1.0, self._chain / self.CHAIN_HOT))

    #: How far the craft rolls into a lane change, and how quickly the
    #: roll follows the move.
    #:
    #: The craft slid between lanes perfectly flat, which reads as a
    #: shape being moved rather than a thing being ridden. The whole
    #: road banks into its own turns already - the blueprint asks for it
    #: twice - and the one thing on the road that never did was the
    #: thing you are steering.
    #:
    #: The slide is nine tenths done in 50 ms, so the roll has to be
    #: quicker than that to be a bank rather than a wobble arriving
    #: after the move: a third of the way there each frame settles
    #: inside three.
    #: Measured: one lane peaks the swerve at 16 units a second and two
    #: at 32, so 1.0 puts a single change at 16 degrees and leaves the
    #: ceiling for a dash across the road. At 3.4 every move of any size
    #: hit the ceiling, which is a bank that reads as a switch rather
    #: than as the craft leaning into what it is doing.
    SWERVE_BANK = 1.0
    SWERVE_MOST = 26.0
    SWERVE_EASE = 0.34

    #: How big the craft's own halo is on a kick, and how strong.
    HALO_REACH = 0.085
    HALO_MOST = 0.55

    def _ship(self, painter, rect, horizon, focal, hue, flash,
              punch=0.0) -> None:
        """The rider: a lit triangle, low on the road."""
        at = self.RIDER_AT
        across = self._lane_here
        wide = self.LANE_WIDE * 0.42
        # Nose forward and up, tail low and wide, so it reads as a craft
        # leaning into the road rather than as a bar lying on it.
        # Nose down the road, tail towards the camera: pointed the other
        # way it read as an arrow aimed at the viewer.
        # Heights are drawn larger downwards, so being off the road
        # is a height taken away.
        lift = self._air
        self._craft_spot = self._eye(horizon, focal, across, -0.14 - lift,
                                     at + 0.4)
        nose = self._eye(horizon, focal, across, -0.26 - lift, at + 1.4)
        left = self._eye(horizon, focal, across - wide, -0.02 - lift, at)
        right = self._eye(horizon, focal, across + wide, -0.02 - lift, at)
        # And the craft itself answers the kick. A halo around it rather
        # than a brighter fill: the fill is already near the top of the
        # scale, and what wants to be felt is the thing being ridden
        # reacting rather than the thing being lit. In the foreground and
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
            # On whole pixels, both of them.
            #
            # A soft glow does not need placing to a fraction of a pixel,
            # and a gradient's ramp does: nothing in a frame settles
            # exactly - the camera closes on its mark asymptotically -
            # so a centre carried at full precision moves a millionth of
            # a pixel every frame and rounds differently somewhere along
            # the ramp. Measured as one pixel of a 640x360 frame
            # changing by one step of red between two frames a second
            # apart with the track stopped. Stopped means stopped.
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
        # Banked into the move, around the craft's own middle, so the
        # nose comes up on the side it is heading for. Everything from
        # here to the end of the craft is drawn inside it; the bumpers
        # go with it, because they are bolted to the thing.
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
        # The bumpers, when they are up: two short bars either side of
        # the craft. Faint while they are coming back, so the state you
        # are playing in is something you can see rather than remember.
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

    def _results(self, painter, rect) -> None:
        """The end of the track: how the run went, over the stopped road.

        Comes up over the first half second after the finish, and stays
        until the track is played again from the start.
        """
        if not self._finished or self._result is None:
            return
        spec = self.POPS["finish"]
        age = next((pop[1] for pop in self._pops if pop[0] == "finish"),
                   spec[0])
        shown = min(1.0, age / 0.5)
        result = self._result
        painter.save()
        try:
            painter.setOpacity(painter.opacity() * shown)
            painter.fillRect(rect, QColor(4, 3, 10, 170))
            wide = min(rect.width() * 0.62, rect.height() * 1.05)
            tall = rect.height() * 0.62
            panel = QRectF(rect.center().x() - wide / 2.0,
                           rect.center().y() - tall / 2.0, wide, tall)
            painter.setPen(QPen(QColor.fromHsvF(self._hue_now % 1.0, 0.7,
                                                1.0, 0.8), 2.0))
            painter.setBrush(QColor(10, 8, 22, 225))
            painter.drawRoundedRect(panel, 14.0, 14.0)

            def line(text, y, size, colour, weight=QFont.Weight.Bold,
                     align=Qt.AlignmentFlag.AlignHCenter):
                font = QFont(painter.font())
                font.setPointSizeF(max(8.0, size))
                font.setWeight(weight)
                painter.setFont(font)
                painter.setPen(colour)
                painter.drawText(QRectF(panel.left() + wide * 0.08, y,
                                        wide * 0.84, size * 1.8),
                                 int(align | Qt.AlignmentFlag.AlignVCenter),
                                 text)

            unit = tall / 20.0
            top = panel.top() + unit * 0.8
            line("TRACK COMPLETE", top, unit * 0.75,
                 QColor(220, 220, 240, 210))
            grade = result["grade"]
            colour = {"S": QColor(255, 215, 90), "A": QColor(120, 255, 170),
                      "B": QColor(120, 200, 255), "C": QColor(210, 160, 255)
                      }.get(grade, QColor(230, 120, 120))
            line(grade, top + unit * 1.3, unit * 3.2, colour,
                 QFont.Weight.Black)
            line(f"{result['worth']:,}", top + unit * 6.3, unit * 1.6,
                 QColor(255, 255, 255), QFont.Weight.Black)
            if not result.get("whole", True):
                best = "SKIPPED THROUGH · NO BEST KEPT"
            elif self.new_best:
                best = "NEW BEST"
            elif self.best_before:
                best = f"BEST {self.best_before:,}"
            else:
                best = ""
            if best:
                line(best, top + unit * 9.2, unit * 0.8,
                     QColor(255, 225, 120) if self.new_best
                     else QColor(200, 200, 220, 200))
            rows = []
            if result["mode"] == "Puzzle":
                rows.append(("Cleared", f"{result['cleared']}"))
            if result["offered"]:
                rows.append(("Taken", f"{result['taken']} of "
                                      f"{result['offered']}  "
                                      f"{result['share']:.0%}"))
            if result["mode"] != "Puzzle":
                rows.append(("Longest chain", f"{result['chain']}"))
            if result["coins"]:
                rows.append(("Coins", f"{result['coins']}"))
            if result["airs"]:
                rows.append(("Jumps", f"{result['airs']}"))
            rows.append(("Hits", f"{result['hits']}"))
            if result["saves"]:
                rows.append(("Saved by the shield", f"{result['saves']}"))
            if result["clean"] and result["score"]:
                # What was earned, then what keeping it clean adds: the
                # big number above is the two together.
                rows.append(("Score", f"{result['score']:,}"))
                rows.append(("Clean finish",
                             f"+{self.bonus(result['mode']):.0%}"))
            y = top + unit * 10.9
            for name, value in rows:
                line(name, y, unit * 0.62, QColor(190, 190, 215),
                     QFont.Weight.Medium, Qt.AlignmentFlag.AlignLeft)
                line(value, y, unit * 0.62, QColor(255, 255, 255),
                     QFont.Weight.Bold, Qt.AlignmentFlag.AlignRight)
                y += unit * 1.05
        finally:
            painter.restore()

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
    # The road is already on the beat, so the strobe is the room around
    # it rather than the beat itself.
    "Music rider": ("Kick", 0.40, 0.50),
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
