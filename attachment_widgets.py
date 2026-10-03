"""The pieces the attachment window is built from.

SeekBar moves to where it is clicked and ignores the player's stale reports
after a seek, so the handle does not jump back. Spectrum interpolates
between rows of a precomputed analysis (Qt6 has no audio probe) and stops
when nothing plays.
"""

from __future__ import annotations

import math
from typing import List, Optional

import math as _math
import time as _time

import visualizers
from flowlayout import FlowHolder as _FlowHolder

from PySide6.QtCore import (QAbstractAnimation, QEasingCurve, QPoint, QPointF,
                            QRect, QRectF, QSize, Qt,
                            QTimer, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient,
                           QPainter, QPainterPath, QPixmap,
                           QPen, QRadialGradient, QGuiApplication)
from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat,
                              QOpenGLPaintDevice, QOpenGLTextureBlitter)
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLayout, QSizePolicy,
                               QSlider, QStyle, QStyleOptionSlider,
                               QVBoxLayout, QWidget)


class SeekBar(QSlider):
    """A slider that goes where you click and stays where you put it."""

    seeked = Signal(int)
    #: Where the player says it is, for anything following this bar. ``report``
    #: sets the value with signals blocked, so ``valueChanged`` never fires
    #: while a track plays; follow this instead.
    moved = Signal(int)

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

        A player keeps reporting its old position for a while after a seek,
        out of order, so the guard holds for the whole settling window and
        drops anything far from the target.
        """
        if self._dragging:
            return
        if self._pending is not None:
            tolerance = max(750, self.maximum() // 100)
            if abs(position - self._pending) > tolerance:
                return
            # Near enough to be real: follow it, but keep guarding until the
            # window closes, since more stale reports may follow.
            self._pending = position
        self.blockSignals(True)
        self.setValue(position)
        self.blockSignals(False)
        self.moved.emit(position)


def _hz_label(value) -> str:
    """73Hz, 1.4kHz, 22kHz - as the reference meters are labelled."""
    if value >= 1000:
        thousands = value / 1000.0
        text = f"{thousands:.0f}" if thousands == int(thousands) else f"{thousands:.1f}"
        return f"{text}kHz"
    return f"{int(value)}Hz"


#: Whether the font machinery has been woken up yet. See warm_the_glyphs.
_WARMED = False


#: Enough of the alphabet to cover every caption and readout: the scenes
#: letter their labels, their cards and their dials from it.
_LETTERS = ("0123456789 abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ %.:-+/,()[]")


def warm_the_glyphs() -> None:
    """Pay the font machinery's one-off cost before anything animates.

    The first text drawn in a process populates Qt's font database, which
    cost 168 ms of a scene's first frame at 1440x810. One drawText is
    enough. It must run on the GUI thread: populateFamilyAliases off it
    races the GUI thread and crashes. Called while the pane is built, before
    anything moves; see Spectrum.WARM_FRAMES.
    """
    global _WARMED

    if _WARMED:
        return
    _WARMED = True
    try:
        image = QImage(700, 48, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        try:
            for family in (None, visualizers.dial_face()):
                font = QFont(family) if family else QFont(painter.font())
                font.setPointSizeF(12.0)
                painter.setFont(font)
                painter.drawText(QRectF(0, 0, 700, 48), 0, _LETTERS)
        finally:
            painter.end()
    except Exception:      # noqa: BLE001 - a cold cache is not fatal
        pass


class SpectrumState:
    """Everything a scene is handed, and nothing it has to work out."""

    __slots__ = ("levels", "peaks", "bass", "mid", "synth", "high", "hit",
                 "hue", "phase", "scroll", "strobe", "sparks", "labels",
                 "dials", "dial_labels", "dial_colour", "background",
                 "trace", "vector", "calibration", "history",
                 "trace_history", "vector_history", "kit", "tempo",
                 "beat_at", "at", "chart", "moving", "contour", "harmony",
                 "flux", "rhythm", "playing", "jumps", "rhythm_due")

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
        self.trace = None
        self.vector = None
        self.calibration: dict = {}
        #: Recent frames, oldest first, for the scenes that show time.
        self.history: List = []
        self.trace_history: List = []
        self.vector_history: List = []
        self.dials: List[float] = []
        self.dial_labels: List[str] = []
        #: The reference these are copied from is red on near black.
        self.dial_colour = QColor(226, 62, 48)
        self.background = QColor(6, 4, 6)
        #: How recently each part of the kit was hit: 1 at the hit, falling
        #: away after. Scenes read these, so they need not know the playhead or
        #: the frame time.
        self.kit: dict = {}
        #: Where the playhead is, in seconds, on the pane's own clock.
        self.at = 0.0
        #: Every hit in the track, by name, in seconds, for a scene that draws
        #: something before its beat.
        self.chart: dict = {}
        #: How hard each part of the kit hit at every moment, as (readings,
        #: readings a second), for reading the rhythm without trusting single
        #: hits. See trackstyle.
        self.flux: dict = {}
        #: The track's shape from end to end (attachment_audio's ``contour``);
        #: None until the analysis lands.
        self.contour = None
        #: Key, tuning, chords and lead (see harmony). None until that pass
        #: lands, which is last, and for good on a track with nothing to hear.
        self.harmony = None
        #: The drums' tempo, beat and pattern (see trackstyle); None until the
        #: kit is found.
        self.rhythm = None
        #: Whether the track is moving. A paused player reports the same
        #: position every frame, and a scene that travels must not creep while
        #: stopped.
        self.moving = True
        #: Whether the player is playing, as the pane was told; None where
        #: nothing has said.
        self.playing = None
        #: How many jumps the pane has made (see Spectrum.seek_to), and whether
        #: the drums' beat is still to come; None where nothing says.
        self.jumps = None
        self.rhythm_due = None
        #: The tempo in beats a minute (0 where none was found), and how far
        #: through the current beat the playhead is, from 0 to just under 1.
        #: For scenes that anticipate the next beat rather than react to the
        #: last.
        self.tempo = 0.0
        self.beat_at = 0.0


    def settle(self) -> None:
        """Force every number back into the range it claims.

        A bad decode or a tempo looked for in silence can put a nan or an
        infinity here. Scenes keep running sums, so one bad value spoils
        every later frame, and a nan reaching ``int()`` raises out of paint
        and closes the window. ``phase`` is a running angle and is only
        checked for being a number; ``trace`` and ``vector`` are samples
        shared with the history and are left alone.
        """
        from visualizers import bounded

        self.levels = [bounded(value) for value in self.levels]
        self.peaks = [bounded(value) for value in self.peaks]
        # A scene reads peaks and levels by the same index, so they are made
        # the same length here.
        if len(self.peaks) != len(self.levels):
            self.peaks = (self.peaks + [0.0] * len(self.levels)
                          )[:len(self.levels)]
        self.bass = bounded(self.bass)
        self.mid = bounded(self.mid)
        self.synth = bounded(self.synth)
        self.high = bounded(self.high)
        self.hit = bounded(self.hit)
        self.hue = bounded(self.hue)
        self.scroll = bounded(self.scroll)
        self.phase = bounded(self.phase, most=1e9)
        self.beat_at = bounded(self.beat_at)
        #: Bounded both ways: a tempo near zero is a beat of millions of
        #: seconds, and a huge one lays thousands of figures a second.
        self.tempo = bounded(self.tempo, most=1000.0)
        #: A day of music. An infinite playhead sets the rider's origin to
        #: infinity, and its position then comes out as a nan.
        self.at = bounded(self.at, most=86400.0)
        if self.kit:
            self.kit = {name: bounded(value)
                        for name, value in self.kit.items()}


def _gpu_wanted() -> bool:
    """Whether the pane may draw on the graphics card.

    Not on the offscreen and minimal platforms, where the tests run: those
    use the CPU path, which any machine without a working GPU also falls
    back to. ``MAIL_MANAGER_GPU=0`` forces the CPU path everywhere.
    """
    import os

    if os.environ.get("MAIL_MANAGER_GPU", "1").strip() == "0":
        return False
    return _platform_name() not in ("", "offscreen", "minimal", "vnc")


def _platform_name() -> str:
    """Which windowing platform Qt is running on, or nothing yet."""
    app = QGuiApplication.instance()
    return app.platformName() if app is not None else ""


class _GpuCanvas(QOpenGLWidget):
    """Where the pane draws when there is a graphics card.

    At a Retina full screen the rider with its effects cost 42 ms a frame on
    the CPU, so the pane drew at half resolution and stretched it. Qt's
    OpenGL paint engine draws the same QPainter calls on the card in 10 ms,
    with the same picture. It is transparent to the mouse and covers the
    pane, which then paints nothing itself.
    """

    def __init__(self, pane) -> None:
        super().__init__(pane)
        # Weakly: a canvas holding the pane made a cycle, and the collector can
        # free a cycle in the middle of another pane's frame. See paintGL.
        import weakref

        self._pane = weakref.ref(pane)
        #: The framebuffers, built for one size on one context.
        self._buffers = None
        self._halves = {}
        self._blitter = None
        #: The last frame of the scene before, while the next fades up over it.
        #: See hold.
        self._held = None
        #: The small copy of the last frame the bloom was made from.
        self.last_halo = None
        #: The rider's lit world, for this context. See Spectrum._world.
        self.world = None
        self.world_failed = False
        #: The samples the governor asked for this frame, for the world.
        self.world_samples = 4
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents,
                          True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def initializeGL(self) -> None:      # noqa: N802 - Qt's name
        # A new context, which reparenting into full screen can bring: whatever
        # was built on the old one is gone.
        self._buffers = None
        self._halves = {}
        self._held = None
        self.world = None
        self._blitter = QOpenGLTextureBlitter()
        self._blitter.create()
        # The rider's world frees its GL names with its context, so dropping it
        # here is enough. (Releasing explicitly as the context goes cannot be
        # done from Python: PySide has let go of the context by then.)

    def buffers(self, width: int, height: int, halo: QSize,
                samples: int = 4):
        """The three framebuffers and the device for this size: multisampled
        for the scene, a plain one it resolves into, and a small one for the
        bloom.
        """
        key = (width, height, halo.width(), halo.height(), samples,
               id(self.context()))
        if self._buffers is None or self._buffers[0] != key:
            def made(w, h, samples=0):
                shape = QOpenGLFramebufferObjectFormat()
                shape.setSamples(samples)
                shape.setAttachment(
                    QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
                return QOpenGLFramebufferObject(QSize(w, h), shape)

            device = QOpenGLPaintDevice(QSize(width, height))
            self._buffers = (key, made(width, height, samples),
                             made(width, height),
                             made(halo.width(), halo.height()), device)
            self._halves = {}
        return self._buffers[1:]

    def half(self, width: int, height: int):
        """A plain framebuffer of this size, kept for the next frame."""
        key = (width, height)
        found = self._halves.get(key)
        if found is None:
            shape = QOpenGLFramebufferObjectFormat()
            shape.setSamples(0)
            found = self._halves[key] = QOpenGLFramebufferObject(
                QSize(width, height), shape)
        return found

    def hold(self, fold=None) -> bool:
        """Keep the frame last drawn, for the next scene to fade up over.

        ``fold`` is how far a scene already fading up had come; the held
        frame and the last one are mixed in that proportion, so a change
        during a change carries on from what is showing. False if nothing
        has been drawn.
        """
        if self._buffers is None:
            return False
        last = self._buffers[2]
        size = last.size()
        held = self._held
        if fold is not None and held is not None and held.size() == size:
            held.bind()
            self.context().functions().glViewport(
                0, 0, size.width(), size.height())
            self.cover(last.texture(), QRect(0, 0, size.width(),
                                             size.height()), fold)
            held.release()
            return True
        if held is None or held.size() != size:
            shape = QOpenGLFramebufferObjectFormat()
            shape.setSamples(0)
            held = self._held = QOpenGLFramebufferObject(size, shape)
            functions = self.context().functions()
            functions.glBindTexture(0x0DE1, held.texture())
            for which in (0x2800, 0x2801):      # magnify, minify
                functions.glTexParameteri(0x0DE1, which, 0x2601)
        whole = QRect(0, 0, size.width(), size.height())
        QOpenGLFramebufferObject.blitFramebuffer(held, whole, last, whole)
        return True

    def held(self):
        """The frame being faded away from, if there is one."""
        return self._held

    def let_go(self) -> None:
        """The fade is over: the held frame is not wanted until the next."""
        self._held = None

    def cover(self, texture, screen: QRect, alpha: float = 1.0) -> None:
        """Draw ``texture`` over the whole of the bound framebuffer, at
        ``alpha`` of its strength over what is there."""
        functions = self.context().functions()
        if alpha < 0.999:
            functions.glEnable(0x0BE2)      # blending
            functions.glBlendColor(0.0, 0.0, 0.0, float(alpha))
            # By a constant, not the texture's alpha, which the rider's world
            # does not always leave at one.
            functions.glBlendFunc(0x8003, 0x8004)
        else:
            functions.glDisable(0x0BE2)
        self._blitter.bind()
        self._blitter.blit(
            texture,
            QOpenGLTextureBlitter.targetTransform(QRectF(screen), screen),
            QOpenGLTextureBlitter.Origin.OriginBottomLeft)
        self._blitter.release()
        functions.glDisable(0x0BE2)

    def paintGL(self) -> None:      # noqa: N802 - Qt's name
        """One frame, with Python's collector held off until it is done.

        If the collector frees a widget with its own GL context mid-frame,
        that context becomes current and then none, and the painter's next
        call crashed the process (three runs in three with a second pane
        dropped while the first drew). Collection waits for the end of the
        frame instead.
        """
        import gc

        pane = self._pane()
        if pane is None:
            return
        was = gc.isenabled()
        gc.disable()
        try:
            pane._paint_on_gpu(self)
        finally:
            if was:
                gc.enable()


class Spectrum(QWidget):
    """The equaliser, and whichever scene draws it. The numbers are worked out
    before playback; this keeps the smoothed state and hands it to a scene.
    Nothing runs while nothing plays.
    """

    #: How tall the plain strip wants to be: at 240 the scenes' detail was
    #: squashed.
    HEIGHT = 320

    #: Shapes the picture can take, as width to height; None fills the height
    #: the window can spare.
    SHAPES = (("Fill", None),
               ("Cinema 21:9", 21 / 9), ("Wide 16:9", 16 / 9),
               ("Photo 3:2", 3 / 2), ("Classic 4:3", 4 / 3),
               ("Square", 1.0),
               ("Portrait 4:5", 4 / 5), ("Portrait 3:4", 3 / 4),
               ("Portrait 2:3", 2 / 3), ("Portrait 9:16", 9 / 16))
    #: However tall a shape asks for, never more than this.
    MAX_HEIGHT = 900

    #: Frames of history kept for the scenes that plot time.
    HISTORY = 96

    #: The least it will ever take: below this the scenes have nowhere to put
    #: their detail, so the controls wrap instead.
    FLOOR = 150

    #: What the strobe has been set to when a scene changed it, so the controls
    #: can follow.
    strobe_settings_changed = Signal(str, float, float)

    #: Sixty a second, which is what the scenes are budgeted against.
    FRAME_MS = 16

    #: How long one scene takes to give way to the next; the first is the
    #: default.
    CHANGES = (("Quick", 0.2), ("Smooth", 0.6), ("Slow", 1.5), ("Cut", 0.0))

    #: How much of the way a new scene fades up each frame, at sixty a second
    #: and the default length.
    FRESH_STEP = FRAME_MS / 1000.0 / CHANGES[0][1]

    #: How many frames a scene draws, unseen, before its fade starts.
    #:
    #: A scene's first frames cost several times its later ones (one-off
    #: allocations, cached pixmaps, fonts), and the fade used to show exactly
    #: those. Six because only the first frame is expensive once fonts are
    #: warmed (12 to 16 ms against 5 to 11); twenty-four left the pane blank
    #: for four tenths of a second.
    WARM_FRAMES = 6

    #: Frames up to this many pixels are drawn at their real size without
    #: measuring, since every scene holds a frame there. Above it Sharpness
    #: times the scene and decides. A scene that can afford more says so
    #: (``Scene.sharp_pixels``), as the dials do: their faces are cached.
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
        #: How fast each needle moves, and when it last moved: without momentum
        #: a meter is a bar graph.
        self._dial_speed: List[float] = []
        self._dial_clock = None
        #: Which frequency each meter reads, chosen by the user; starts at the
        #: ten on the reference meter.
        self._dial_centres = None
        #: One slice of the real waveform per frame, for the scope.
        self._traces: List = []
        #: Left against right, for the vector mode.
        self._vectors: List = []
        self._state = SpectrumState()
        self._scene = visualizers.SCENES[0]
        self._sparks = [[0.0, 0.0, 0.0, 0.0, 0.0] for _ in range(self.SPARKS)]
        self._next_spark = 0
        self._target = 0.0
        self._unbounded = False
        self._reserve = 0
        #: Set while somebody is working the controls. See GIVE_WAY.
        self._giving_way = False
        #: None when idle, else 0..1 while the track is being analysed.
        self._working = None
        self._post = True
        self._effects = PostProcess()
        self._sharpness = Sharpness()
        #: The same question, asked of the graphics card. See _paint_on_gpu.
        self._card = CardSharpness()
        self._buffer = None
        #: None for the fixed strip, else width-to-height.
        self._aspect = None
        #: The most the strip may take, set by whoever owns the layout. Without
        #: it a tall shape took its height and the controls were drawn over the
        #: scene.
        self._budget = None
        #: Middle of the slider until somebody moves it.
        self._strobe_rate = 0.5
        self._strobe_sense = 0.5
        #: Which part of the sound the strobe listens to.
        self._strobe_source = "Bass"
        #: Whether the user has touched the strobe controls; until then a scene
        #: sets them to suit itself.
        self._strobe_chosen = False
        #: 0 while a track plays, 1 while the scene drifts on its own; in
        #: between is the crossfade.
        self._settle = 0.0
        #: 0 to 1 while a newly chosen scene fades up. See FRESH_STEP.
        self._fresh = 1.0
        #: How long that takes, in seconds. See set_change.
        self._change = self.CHANGES[0][1]
        #: On the card, the scene before is kept and the new one fades up over
        #: it: asked for, under way, and how far it had got when last shown.
        self._swap_pending = False
        self._crossing = False
        self._shown_fresh = 0.0
        #: How many frames the current scene has drawn; the fade waits for
        #: these (see WARM_FRAMES).
        self._drawn = 0
        self._last_watched = 0.0
        self._since_hit = 99
        #: A clock of our own that leans on the playhead. See ``_heard``.
        self._heard_now = None
        self._heard_at = None
        #: Where the player was last sent, and when, while waiting for it to
        #: move on. See seek_to.
        self._seek_hold = None
        self._seek_at = 0.0
        self._seek_from = None
        self._was_playing = False
        #: Started again and not heard from since, then closing on the player's
        #: first report. See _heard.
        self._settling = False
        self._catching = False
        #: How many times the picture has jumped rather than moved; a scene
        #: compares it with the count it last saw.
        self._jumps = 0
        #: Whether the drums' beat is still being worked out for this track.
        #: See expect_rhythm.
        self._rhythm_due = False
        #: The moment of the music this frame shows, worked out once a frame
        #: (see _heard and _tick) and read by everything drawn.
        self._now = 0.0
        #: The allowance for the ear and the eye (see av_sync), set by whoever
        #: plays the audio. None shows the player's position as it is.
        self.allowance = None
        #: The last position the source reported, and when; a report is a
        #: timestamp, not a level. See _heard.
        self._said_was = None
        self._said_at = None
        #: Whether the manual key is being held down.
        self._holding = False
        self._spamming = False
        #: What the two sliders mean in Manual (see HAND_SLOWEST and HAND_ON),
        #: kept apart from the automatic pair so switching mode carries nothing
        #: over.
        self._hand_rate = 0.5
        self._hand_shape = 0.0
        #: Where a flash from the hand is heading: what the shape rises towards
        #: and falls from.
        self._hand_want = 0.0
        #: When the rapid-fire key last fired, by the wall clock rather than
        #: frames, so its rate does not move with the frame rate.
        self._spam_at = None
        #: The beats found before playback started, one map per source.
        self._beats: dict = {}
        #: The kit on its own, for scenes that want to know which is which.
        self._elements: dict = {}
        self._chart_from = None
        #: The track's shape, built once the frames and the traces are both in.
        #: See set_traces.
        self._contour = None
        self._contour_from = None
        self._contour_whole = False
        #: See set_harmony and set_rhythm.
        self._harmony = None
        self._rhythm = None
        #: The drums' beats where the tempo moves, as a clock: (the rhythm it
        #: was built from, the clock). See _clock.
        self._rhythm_clock = None
        self._moved_at = None
        #: How far through each element's list the playhead has got.
        self._kit_at: dict = {}
        self._kit_seen = -1.0
        #: How far through the map the playhead has got, so each frame only
        #: looks at what happened since the last.
        self._beat_at = 0
        self._beat_seen = -1.0
        #: How long the watched band has held, and when the rapid strobe last
        #: fired.
        self._held = 0.0
        self._held_seen = 0.0
        self._rapid_at = -99.0
        self._timer = QTimer(self)
        # Sixty a second; every scene paints in well under a frame at 1080p.
        self._timer.setInterval(self.FRAME_MS)
        self._timer.timeout.connect(self._tick)

        self._reveal = 0.0
        self._idling = False
        self._drift = 0.0
        self._wanted = False
        self._source = None

        self._flow = QVariantAnimation(self)
        # Long enough to read as growing, short enough that turning the
        # visualiser on feels immediate.
        self._flow.setDuration(380)
        self._flow.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._flow.valueChanged.connect(self._reveal_changed)

        # Kept, never started: conceal() is still called directly by a pane
        # putting a file away.
        self._away = QTimer(self)
        self._away.setSingleShot(True)
        # Before anything animates. See warm_the_glyphs.
        warm_the_glyphs()
        #: Whether this frame's scene was drawn on the card, and through what,
        #: for the polish to go over it. See _paint_on_gpu.
        self._on_gpu = False
        self._gpu_scene = None
        #: The GPU canvas, where there is a GPU. See _GpuCanvas.
        self._canvas = self._make_canvas()

    # -- drawing on the graphics card --------------------------------------
    def _make_canvas(self):
        """A GPU canvas over the pane, where there is a GPU to draw on."""
        if not _gpu_wanted():
            return None
        try:
            canvas = _GpuCanvas(self)
            canvas.setGeometry(self.rect())
            canvas.show()
            return canvas
        except Exception:      # noqa: BLE001 - the CPU path is always there
            return None

    def _drop_canvas(self) -> None:
        """Give up on the card for the rest of the session, quietly: the pane
        draws as it did without a canvas, and a card that failed is not
        asked again.
        """
        canvas, self._canvas = self._canvas, None
        self._crossing = self._swap_pending = False
        if canvas is not None:
            canvas.hide()
            canvas.deleteLater()
        super().update()

    def _world(self):
        """The rider's world for the canvas's context, made on first use. None
        without a canvas, with ``MAIL_MANAGER_WORLD=0``, or where this card
        would not build it (logged once; the flat drawing is used from then
        on).
        """
        import os

        canvas = self._canvas
        if canvas is None or canvas.world_failed:
            return None
        if os.environ.get("MAIL_MANAGER_WORLD", "1").strip() == "0":
            return None
        if canvas.world is None:
            try:
                import rider_gl

                canvas.world = rider_gl.RiderWorld(
                    canvas.context().functions())
            except Exception:      # noqa: BLE001 - the flat one is there
                import logging

                logging.getLogger(__name__).exception(
                    "the rider's world could not be built on this card; "
                    "drawing it flat")
                canvas.world_failed = True
                return None
        return canvas.world

    @property
    def on_gpu(self) -> bool:
        """Whether the pane is drawing on the graphics card."""
        return self._canvas is not None

    def update(self, *args) -> None:      # noqa: D401 - Qt's name
        if self._canvas is None:
            super().update(*args)
            return
        # Only the canvas: it covers the pane, and asking the pane too painted
        # every frame twice. Its size is set here as well as in resizeEvent,
        # because Qt holds back a hidden widget's resizes, and a pane built
        # hidden left the canvas with no height.
        if self._canvas.geometry() != self.rect():
            self._canvas.setGeometry(self.rect())
        self._canvas.update()

    def _paint_on_gpu(self, canvas) -> None:
        """One frame, drawn on the card at the screen's own resolution.

        The same _paint, into a multisampled framebuffer the size of the
        pane in real pixels. The scene's polish goes over it from a small
        copy made on the card (the bloom is a blur, so only that small
        picture comes back). A card that cannot draw that many pixels sixty
        times a second gets fewer samples, then fewer pixels: see
        CardSharpness.
        """
        import time as _time

        started = _time.perf_counter()
        try:
            ratio = canvas.devicePixelRatioF()
            screen_w = int(round(canvas.width() * ratio))
            screen_h = int(round(canvas.height() * ratio))
            if screen_w < 2 or screen_h < 2:
                # A pane with no room has nothing to draw, and the card will
                # not paint into a framebuffer with no height.
                return
            samples, share = self._card.choice(
                screen_w * screen_h, ratio, self._scene)
            width = max(2, int(round(screen_w * share)))
            height = max(2, int(round(screen_h * share)))
            halo = QSize(
                max(PostProcess.BLOOM_MIN, width // PostProcess.BLOOM_DIVISOR),
                max(PostProcess.BLOOM_MIN, height // PostProcess.BLOOM_DIVISOR))
            # A scene with a world of its own antialiases it there, so the
            # canvas's own samples would be the same work twice (five
            # milliseconds at full screen).
            world = (self._world()
                     if hasattr(self._scene, "paint_on_card") else None)
            canvas.world_samples = samples
            if self._swap_pending:
                # What is on screen, kept before anything is drawn over it, for
                # the new scene to fade up over.
                self._swap_pending = False
                self._crossing = canvas.hold(
                    self._shown_fresh if self._crossing else None)
            multi, flat, small, device = canvas.buffers(
                width, height, halo, 0 if world is not None else samples)
            device.setDevicePixelRatio(ratio * share)
            self._gpu_scene = None
            multi.bind()
            painter = QPainter(device)
            if not painter.isActive():
                # The card would not start a painter on its own buffer: it is
                # not going to work.
                multi.release()
                raise RuntimeError("the graphics card refused a painter")
            self._on_gpu = True
            try:
                self._paint(painter)
            finally:
                self._on_gpu = False
                painter.end()
            multi.release()
            full = QRect(0, 0, width, height)
            QOpenGLFramebufferObject.blitFramebuffer(flat, full, multi, full)
            if self._gpu_scene is not None:
                self._polish_on_gpu(canvas, flat, small, device, ratio * share)
            # Back onto the canvas's own framebuffer, which the blits above
            # unbound.
            functions = canvas.context().functions()
            functions.glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
            screen = QRect(0, 0, screen_w, screen_h)
            functions.glViewport(0, 0, screen_w, screen_h)
            functions.glClearColor(0.0, 0.0, 0.0, 1.0)
            functions.glClear(0x00004000)
            # Stretched without smoothing when the stretch is a whole number,
            # which keeps the edges a smoothed stretch blurs (see blit_scene).
            grew = screen_w / width
            whole = abs(grew - round(grew)) < 0.02
            smooth = (getattr(self._scene, "stretch_smooth", False)
                      or not whole)
            functions.glBindTexture(0x0DE1, flat.texture())
            functions.glTexParameteri(0x0DE1, 0x2800,
                                      0x2601 if smooth else 0x2600)
            held = canvas.held() if self._crossing else None
            shown = max(0.0, min(1.0, self._fresh))
            if held is not None and shown < 0.999:
                # The scene before, and this one coming up over it.
                canvas.cover(held.texture(), screen)
                canvas.cover(flat.texture(), screen, shown)
                self._shown_fresh = shown
            else:
                if self._crossing:
                    self._crossing = False
                    canvas.let_go()
                canvas.cover(flat.texture(), screen)
            # Waited for, so the measurement is what the card took, not how
            # long it took to be asked.
            functions.glFinish()
            self._card.record((_time.perf_counter() - started) * 1000.0,
                              ratio)
        except Exception:      # noqa: BLE001
            import logging

            logging.getLogger(__name__).exception(
                "drawing on the graphics card failed; drawing on the CPU "
                "from here on")
            QTimer.singleShot(0, self._drop_canvas)

    def _polish_on_gpu(self, canvas, flat, small, device, ratio) -> None:
        """The scene's post-processing, over the frame on the card.

        Through the same PostProcess, with the transform and opacity the
        scene was drawn through, so the polish slides and fades with the
        scene. Into the scene's framebuffer before it reaches the screen: at
        6K each full-screen pass costs two milliseconds on a small card, and
        a smaller buffer then makes all of the frame cheaper.
        """
        rect, transform, opacity, recipe = self._gpu_scene
        # Where the scene landed in real pixels, which the small copy is taken
        # from: the polish belongs to the scene, not the background around it.
        box = transform.mapRect(rect)
        source = QRect(int(box.x() * ratio), int(box.y() * ratio),
                       max(1, int(box.width() * ratio)),
                       max(1, int(box.height() * ratio)))
        # Upside down: a framebuffer counts rows from the bottom and a painter
        # from the top. With the scene letterboxed, the bloom would otherwise
        # come from the wrong band.
        flipped = QRect(source.x(),
                        flat.height() - source.y() - source.height(),
                        source.width(), source.height())
        # Down to the bloom's size in halves. A linear blit averages two by
        # two, so each halving is exact, while one eightfold shrink reads one
        # pixel in sixteen and turned thin lines into rows of soft blobs. This
        # matches the CPU's shrink.
        here, area = flat, flipped
        while (area.width() // 2 >= small.width() * 2
               and area.height() // 2 >= small.height() * 2):
            step = canvas.half(max(1, area.width() // 2),
                               max(1, area.height() // 2))
            whole = QRect(0, 0, step.width(), step.height())
            QOpenGLFramebufferObject.blitFramebuffer(
                step, whole, here, area, 0x00004000, 0x2601)
            here, area = step, whole
        QOpenGLFramebufferObject.blitFramebuffer(
            small, QRect(0, 0, small.width(), small.height()), here, area,
            0x00004000, 0x2601)
        halo = QPixmap.fromImage(small.toImage())
        # Kept, so the source of the bloom can be inspected: it is the one
        # thing that comes back off the card.
        canvas.last_halo = halo
        flat.bind()
        painter = QPainter(device)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setWorldTransform(transform)
            painter.setOpacity(opacity)
            self._effects.apply_on_gpu(painter, rect, halo, recipe)
        finally:
            painter.end()
            flat.release()


    # -- what it shows ----------------------------------------------------
    def set_scene(self, scene) -> None:
        was, self._scene = self._scene, scene
        # There is one of each scene for the session, so picking one returns it
        # as the last track left it. Optional: a stand-in that only paints has
        # nothing to reset.
        start = getattr(scene, "reset", None)
        if scene is not was and callable(start):
            start()
            self._drawn = 0
            if self._change <= 0.0:
                self._fresh = 1.0
                self._crossing = self._swap_pending = False
            else:
                # Faded in rather than cut to: a scene's first frames are half
                # built. On the card it fades up over the last frame of the
                # scene before, so changing scene quickly never passes through
                # black.
                self._fresh = 0.0
                self._swap_pending = (
                    self._canvas is not None and self._reveal >= 0.999
                    and bool(self._level) and self._working is None)
        self._suit_the_scene(scene)
        self.update()

    def _suit_the_scene(self, scene) -> None:
        """Put the strobe where this scene wants it, until the user moves a
        slider or chooses a source; after that switching scene leaves the
        controls alone.
        """
        if self._strobe_chosen:
            return
        setup = visualizers.strobe_setup(scene)
        if not setup:
            return
        source, rate, sense = setup
        if source in self.STROBE_SOURCES:
            self._strobe_source = source
        self._strobe_rate = max(0.0, min(1.0, float(rate)))
        self._strobe_sense = max(0.0, min(1.0, float(sense)))
        # What the sliders should show, which is not this pair when the scene
        # asked for Manual.
        self.strobe_settings_changed.emit(*self.strobe_shown())

    def set_change(self, seconds: float) -> None:
        """How long a newly chosen scene takes to replace the one before, in
        seconds; 0 cuts."""
        self._change = max(0.0, float(seconds))
        if self._change <= 0.0:
            self._fresh = 1.0
            self._crossing = self._swap_pending = False

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

    def _scene_box(self, rect):
        """The part of the widget the scene is drawn in: all of it for the
        plain strip, otherwise the largest centred rectangle of the chosen
        ratio.
        """
        if self._aspect is None or rect.width() <= 0 or rect.height() <= 0:
            return rect
        wide = rect.width() / rect.height()
        if abs(wide - self._aspect) < 0.01:
            return rect
        if wide > self._aspect:
            width = rect.height() * self._aspect
            return QRectF(rect.left() + (rect.width() - width) / 2.0,
                          rect.top(), width, rect.height())
        height = rect.width() / self._aspect
        return QRectF(rect.left(), rect.top() + (rect.height() - height) / 2.0,
                      rect.width(), height)

    def _full_height(self) -> int:
        """How tall the strip wants to be when fully revealed, never more than
        the budget.
        """
        if self._aspect is None:
            # All the height there is to spare, where that is known.
            wanted = self.HEIGHT if self._budget is None else self._budget
        else:
            width = self.width() or self.sizeHint().width() or 420
            wanted = max(120, min(self.MAX_HEIGHT, int(width / self._aspect)))
        if self._budget is not None:
            wanted = min(wanted, self._budget)
        return max(60, wanted)

    def set_strobe_rate(self, rate: float) -> None:
        """How soon after a flash the next may fire, 0 rare to 1 often. In
        Manual, nothing fires by itself, so it sets how fast the rapid-fire
        key repeats.
        """
        self._strobe_chosen = True
        value = max(0.0, min(1.0, float(rate)))
        if self._strobe_source == self.BY_HAND:
            self._hand_rate = value
        else:
            self._strobe_rate = value

    #: What the strobe can listen to: four ranges, then the parts of the kit. A
    #: range is a place in the spectrum and an instrument is a place and a
    #: shape, so Kick fires on kicks and not on the bass under them.

    #: Manual is last and is not part of the sound: nothing fires by itself and
    #: the only light is the hotkey's. The hotkey works in every mode, adding a
    #: flash on top of the track.
    STROBE_SOURCES = ("Bass", "Mids", "Treble", "Synths",
                      "Kick", "Snare", "Hats", "Synth", "Manual")

    #: The one source that listens to nobody.
    BY_HAND = "Manual"

    #: How hard a hotkey flash is, and how long a held key keeps the light up
    #: before it sags. Full brightness.
    HAND_HIT = 1.0

    #: Frames between flashes while the rapid-fire key is held, outside Manual:
    #: twelve a second at sixty frames, the rate the strobe's warning is about.
    SPAM_EVERY = 5

    #: How far the hit from an automatic flash falls each frame.
    HIT_FALL = 0.16

    #: In Manual the two sliders set how fast a hand strobe repeats and how it
    #: rises and falls.

    #: Flashes a second at the two ends of the rate slider. Its middle is their
    #: geometric mean, the twelve a second the rapid-fire key has always used.
    HAND_SLOWEST = 4.8
    HAND_FASTEST = 30.0

    #: Brightness a hand flash gains and loses each frame at the two ends of
    #: the shape slider: left, there and gone in one frame; right, a quarter of
    #: a second up and half a second down.
    HAND_ON = (1.0, 0.075)
    HAND_OFF = (1.0, 0.030)

    #: How early a repeat may fire and still count as on time: half a frame.
    #: Frames do not land on exact multiples of a period, so thirty a second,
    #: asked for every 33.3 ms, missed a frame at 33.2 and ran at twenty.
    SPAM_SLACK = 0.008

    def flash(self, strength: float = 1.0) -> None:
        """Fire the strobe now, whatever it listens to. A tap lands on the next
        frame, not the next beat: the timing is the player's.
        """
        want = max(0.0, min(1.0, float(strength)))
        if self._strobe_source == self.BY_HAND:
            # Moved towards at once rather than on the next frame: with the
            # shape slider hard left the rise is a whole flash.
            self._hand_want = max(self._hand_want, want)
            self._state.hit = min(self._hand_want,
                                  self._state.hit + self.hand_curve()[0])
        else:
            self._state.hit = max(self._state.hit, want)
        self._since_hit = 0
        # Back to full rate at once if the controls had slowed it.
        self._pace()
        self.update()

    def hold_flash(self, on: bool) -> None:
        """Keep the light up while the key is down. ``spam_flash`` is the other
        key: one holds a light, the other strobes.
        """
        self._holding = bool(on)
        if on:
            self.flash(self.HAND_HIT)

    def spam_flash(self, on: bool) -> None:
        """Fire over and over for as long as the key is down."""
        self._spamming = bool(on)
        self._spam_at = _time.monotonic() if on else None
        if on:
            self.flash(self.HAND_HIT)

    def cycle_strobe_source(self, step: int = 1) -> str:
        """Move to the next thing the strobe listens to, and say which."""
        try:
            at = self.STROBE_SOURCES.index(self._strobe_source)
        except ValueError:
            at = 0
        name = self.STROBE_SOURCES[(at + int(step)) % len(self.STROBE_SOURCES)]
        self.set_strobe_source(name)
        return name

    def set_beats(self, maps) -> None:
        """The beat maps the analysis found, one per thing to listen to,
        keeping the kit if it is already here: the drums can arrive first.
        """
        self._beats = dict(maps or {})
        self._beats.update(self._elements)
        self._beat_at = 0
        self._beat_seen = -1.0

    def set_elements(self, maps) -> None:
        """The kit: where the kicks, snares and hats are. In the same table as
        the beat maps, so the strobe and the scenes ask for "Kick" as they
        ask for "Bass". They arrive after the rest, so readers must cope
        with their absence.
        """
        self._elements = dict(maps or {})
        self._chart_from = None
        self._beats.update(self._elements)
        self._beat_at = 0
        self._beat_seen = -1.0
        self._kit_at = {}
        self._kit_seen = -1.0

    def elements(self) -> dict:
        """The kit, or an empty table while the finer pass is still running."""
        return dict(self._elements)

    def beat_map(self):
        """The map for whatever the strobe is listening to now."""
        return self._beats.get(self._strobe_source)

    def set_strobe_source(self, name: str) -> None:
        """Which part of the sound sets the strobe off. The sliders mean other
        things in Manual, so they are told what to show here.
        """
        self._strobe_chosen = True
        if name in self.STROBE_SOURCES:
            self._strobe_source = name
            self.strobe_settings_changed.emit(*self.strobe_shown())

    def set_strobe_sense(self, sense: float) -> None:
        """How big a jump counts as a hit, 0 fussy to 1 eager. In Manual, the
        shape of a flash: left is on and off, right fades up and down.
        """
        self._strobe_chosen = True
        value = max(0.0, min(1.0, float(sense)))
        if self._strobe_source == self.BY_HAND:
            self._hand_shape = value
        else:
            self._strobe_sense = value

    def strobe_shown(self) -> tuple:
        """(source, rate, sense) as the sliders should show them: the pair for
        the strobe's mode.
        """
        if self._strobe_source == self.BY_HAND:
            return self._strobe_source, self._hand_rate, self._hand_shape
        return self._strobe_source, self._strobe_rate, self._strobe_sense

    def hand_every(self) -> float:
        """Seconds between flashes while the rapid-fire key is held."""
        span = self.HAND_FASTEST / self.HAND_SLOWEST
        return 1.0 / (self.HAND_SLOWEST * span ** self._hand_rate)

    def hand_curve(self) -> tuple:
        """(rise, fall) a frame for a hand flash. Geometric between the ends:
        halfway along a linear run from 1.0 to 0.075 is still instant.
        """
        shape = self._hand_shape
        return (self.HAND_ON[0] * (self.HAND_ON[1] / self.HAND_ON[0]) ** shape,
                self.HAND_OFF[0] * (self.HAND_OFF[1] / self.HAND_OFF[0])
                ** shape)

    def _shape_hand(self, state) -> None:
        """Move the hand strobe towards its target. The tap aims at full and
        lets go, the held key keeps aiming at full, and the rapid fire
        re-aims each time it comes round.
        """
        rise, fall = self.hand_curve()
        if self._holding:
            self._hand_want = self.HAND_HIT
        elif self._since_hit > 0 and state.hit >= self._hand_want - 1e-6:
            # Arrived and not held: start falling, before the move, or the
            # light sits at the top for a frame. Never on the frame it was
            # fired, though: at the instant end of the slider the flash would
            # never be drawn.
            self._hand_want = 0.0
        if state.hit < self._hand_want:
            state.hit = min(self._hand_want, state.hit + rise)
        else:
            state.hit = max(self._hand_want, state.hit - fall)

    def set_scope_mode(self, mode: str) -> None:
        """Sweep or X-Y, for whichever scene has a beam."""
        setter = getattr(self._scene, "set_mode", None)
        if setter is not None:
            setter(mode)
        self.update()

    def set_decay(self, seconds: float) -> None:
        """Pass the phosphor decay to whichever scene has one."""
        setter = getattr(self._scene, "set_decay", None)
        if setter is not None:
            setter(seconds)
        self.update()

    def set_labels(self, labels) -> None:
        self._state.labels = list(labels or [])

    def dial_centres(self):
        """The frequency each meter is reading, in Hz."""
        import attachment_audio

        return tuple(self._dial_centres or attachment_audio.DIAL_CENTRES)

    def set_dial_centres(self, centres) -> None:
        """Point the meters at different frequencies. The frames in memory are
        re-read against the new centres, so this is immediate.
        """
        import attachment_audio

        cleaned = []
        for value in centres:
            try:
                hertz = int(value)
            except (TypeError, ValueError):
                continue
            # Below 20 Hz nothing is heard, and above the decode's Nyquist
            # limit there is nothing to read.
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
        self._dial_speed = [0.0] * len(centres)
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

    def set_calibration(self, calibration) -> None:
        """What is needed to read a bar's height back as a level."""
        self._state.calibration = dict(calibration or {})

    def set_traces(self, shapes, vectors=None) -> None:
        """The waveform slices that go with the frames."""
        self._traces = list(shapes or [])
        self._vectors = list(vectors or [])
        # The track's shape, worked out again with the traces in it unless the
        # analysis already sent it whole; then it is final, so a road does not
        # change under the rider when the traces arrive. See set_contour.
        if not self._contour_whole:
            self._contour = None
            self._contour_from = None

    def set_harmony(self, harmony) -> None:
        """The track's key, tuning and chords, once they are known."""
        self._harmony = harmony

    def set_rhythm(self, rhythm) -> None:
        """The drums' tempo, beat and pattern once known; None when they could
        not be."""
        self._rhythm = rhythm
        self._rhythm_due = False

    def expect_rhythm(self) -> None:
        """The drums' beat is still being worked out: a scene that needs it can
        wait."""
        self._rhythm_due = True

    def rhythm(self):
        return self._rhythm

    def harmony(self):
        return self._harmony

    def set_contour(self, contour) -> None:
        """The track's whole shape, sent with the bands. Final."""
        if contour is None:
            return
        self._contour = contour
        self._contour_from = self._frames
        self._contour_whole = True

    def frames_list(self):
        """The frames the pane is drawing from, as it was handed them."""
        return self._frames

    def set_frames(self, frames: List, rate: int) -> None:
        """The analysis, which lands a moment after playback starts."""
        import attachment_audio

        if frames and frames is self._frames and max(1, rate) == self._rate:
            # The same analysis again: starting over would zero every level
            # mid-song.
            return
        self._frames = frames or []
        self._contour_whole = False
        self._rate = max(1, rate)
        width = len(self._frames[0]) if self._frames else 0
        self._level = [0.0] * width
        self._peak = [0.0] * width
        # The dial scene reads ten named bands rather than the equaliser's
        # twenty-seven, taken once from the same frames.
        self._rebuild_dials()
        if self._frames and self._wanted:
            self.set_playing(True)
        self.update()

    def set_position(self, milliseconds: int) -> None:
        self._position = max(0, milliseconds)

    def follow(self, source) -> None:
        """Where to read the position rather than waiting to be told:
        positionChanged fires only on a change, and not at all until the
        player has produced samples.
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
            # Idling, not leaving: the strip stays until the tick box says
            # otherwise.
            self._idling = True
            self._timer.start()
            return
        self._timer.stop()
        self.update()

    def clear(self, keep_open: bool = False) -> None:
        """Put the track down, and the strip away with it, unless
        ``keep_open``: a new track in a showing pane keeps its room while it
        is read."""
        if not keep_open:
            self._flow.stop()
            self._away.stop()
            self._reveal = 0.0
            self._target = 0.0
            self.setMinimumHeight(0)
            self.setMaximumHeight(0)
        self._idling = False
        self._wanted = False
        self._crossing = self._swap_pending = False
        self._timer.stop()
        self._frames = []
        self._level = []
        self._peak = []
        self._harmony = None
        self._rhythm = None
        self._rhythm_due = False
        self._heard_now = None
        self._seek_hold = None
        self._said_was = None
        self._said_at = None
        self._state.history = []
        self._state.trace_history = []
        self._state.vector_history = []
        for spark in self._sparks:
            spark[4] = 0.0
        # A new track gets the scene as it was built (see Scene.reset), faded
        # up once the track is read.
        start = getattr(self._scene, "reset", None)
        if callable(start):
            start()
        self._fresh = 0.0 if self._change > 0.0 else 1.0
        self._drawn = 0
        self.updateGeometry()
        self.update()

    @property
    def ready(self) -> bool:
        return bool(self._frames)

    # -- arriving and leaving ---------------------------------------------
    def reveal(self) -> None:
        if self._reveal >= 1.0 and self.maximumHeight() >= self._full_height():
            return
        # Already on its way. Restarted on every progress report, the strip
        # never finished opening while a track was read.
        if (self._target >= 1.0 and self._flow.state()
                == QAbstractAnimation.State.Running):
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

    #: How often the clock ticks while a track is analysed. The analysis and
    #: the scenes share the interpreter lock, and painting alongside made the
    #: analysis 1.6 times slower; twelve a second is plenty for a progress bar.
    WORKING_MS = 80

    def set_working(self, fraction) -> None:
        """Show that analysis is running, and roughly how far along: a long
        track takes a few seconds, and a blank strip reads as nothing
        happening.
        """
        self._working = (None if fraction is None
                         else max(0.0, min(1.0, float(fraction))))
        if self._working is not None:
            self.reveal()
            # Slowed while the analysis has the processor, and restored when it
            # finishes.
            if self._timer.interval() != self.WORKING_MS:
                self._timer.setInterval(self.WORKING_MS)
            if not self._timer.isActive():
                self._timer.start()
        elif self._timer.interval() != self.FRAME_MS:
            self._timer.setInterval(self.FRAME_MS)
        self.update()

    def set_reserve(self, pixels: int) -> None:
        """Leave this many pixels clear at the bottom of the scene."""
        self._reserve = max(0, int(pixels))
        self.update()

    def set_unbounded(self, free: bool) -> None:
        """Stop holding the widget to its strip height. Full screen wants the
        whole window; otherwise the reveal animation keeps reapplying the
        clamps.
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
        height = int(round(self._full_height() * self._reveal))
        if self._unbounded:
            if self._reveal <= 0.001 and self._target <= 0.0:
                self._timer.stop()
                self._idling = False
            self.update()
            return
        # The maximum is the height it wants; the minimum is small enough that
        # it can always give way. With both the same, a short pane drew the
        # transport over the scene.
        self.setMaximumHeight(height)
        # The floor is what the strip would like, bounded by the budget: a
        # minimum larger than the room puts the transport over the picture.
        floor = self.FLOOR if self._budget is None else min(self.FLOOR,
                                                            self._budget)
        self.setMinimumHeight(min(height, floor))
        # Only a slide heading for zero means gone. The first frame of a slide
        # away from zero also reports about zero, and stopping on it killed the
        # scene as it opened.
        if self._reveal <= 0.001 and self._target <= 0.0:
            self._timer.stop()
            self._idling = False
        self.updateGeometry()
        self.update()

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        if self._canvas is not None:
            self._canvas.setGeometry(self.rect())
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
        floor = self.FLOOR if self._budget is None else min(self.FLOOR,
                                                            self._budget)
        return QSize(0, min(int(self._full_height() * self._reveal), floor))

    # -- the numbers ------------------------------------------------------
    def _row(self) -> Optional[List[float]]:
        if not self._frames:
            return None
        # A frame is the sound of a window starting at its slot, so it is read
        # at the middle of the window.
        exact = (self._now - self.FRAME_MIDDLE) * self._rate
        if exact < 0.0:
            exact = 0.0
        index = int(exact)
        if index >= len(self._frames):
            return None
        first = self._frames[index]
        if index + 1 < len(self._frames):
            second = self._frames[index + 1]
            blend = exact - index
            # Falling, eased between frames; rising, not until the next frame's
            # moment, or a kick starts to rise a frame early.
            return [a + (b - a) * blend if b < a else a
                    for a, b in zip(first, second)]
        return list(first)

    def _vector_now(self):
        if not self._vectors:
            return None
        exact = self._now * self._rate
        return self._vectors[min(len(self._vectors) - 1, max(0, int(exact)))]

    def _trace_now(self):
        """The waveform slice for wherever the track is now."""
        if not self._traces:
            return None
        exact = self._now * self._rate
        index = min(len(self._traces) - 1, max(0, int(exact)))
        return self._traces[index]

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

    #: A frame's allowed length while the controls are being used, as a
    #: multiple of the ordinary one. The pane spends most of each sixtieth of a
    #: second painting, leaving about a millisecond for the cursor and the
    #: controls, so the scene halves its rate while they are up.
    GIVE_WAY = 2.0

    def set_giving_way(self, giving: bool) -> None:
        """Ask for fewer frames, because the controls are on screen."""
        giving = bool(giving)
        if giving != self._giving_way:
            self._giving_way = giving
            self._pace()

    def _pace(self) -> None:
        """Ask the timer for frames at a rate the scene can meet: a
        sixteen-millisecond timer handed thirty-millisecond frames fills the
        event queue, and the pane stops answering the mouse.
        """
        if self._working is not None:
            return      # the analysis has its own, slower, interval
        # The scene's time plus the polish's, both measured: what matters is
        # whether the whole frame fits.
        if self._canvas is not None:
            # On the card the whole frame is measured at once. See
            # CardSharpness.
            wanted = self._card.interval_ms(self.FRAME_MS)
        else:
            wanted = self._sharpness.interval_ms(
                self.devicePixelRatioF(), self.FRAME_MS,
                extra=self._effects.cost_ms())
        if self._giving_way and not (self._holding or self._spamming):
            # Not while the strobe is being played by hand: the bar comes up on
            # any mouse movement, and the halved rate delayed the light by a
            # frame and a half.
            wanted = int(wanted * self.GIVE_WAY)
        if self._timer.interval() != wanted:
            self._timer.setInterval(wanted)

    def _tick(self) -> None:
        self._pace()
        if self._source is not None and not self._idling:
            try:
                self._position = max(0, int(self._source()))
            except Exception:      # noqa: BLE001 - a dead player is not fatal
                pass
        # One moment for the whole frame, from the clock rather than the
        # player's last report. The player updates every 50 ms; read straight
        # off it, hits landed up to 33 ms late or 25 ms early depending on
        # where the frame fell.
        self._now = max(0.0, self._heard() + self._ahead())
        if (self._fresh < 1.0 and self._level and self._working is None
                and self._drawn >= self.WARM_FRAMES):
            # Only once there is a scene to fade in: during analysis the pane
            # shows a progress ring, and the fade used to run out behind it. By
            # the frame's own length, so a slower-paced scene still takes the
            # time chosen.
            if self._change <= 0.0:
                self._fresh = 1.0
            else:
                step = max(self.FRAME_MS, self._timer.interval()) / 1000.0
                self._fresh = min(1.0, self._fresh + step / self._change)
        self._drift += 0.035
        # Ease between the track and the idle drift rather than swapping, or
        # the scene lurches when a track ends or pauses.
        target = 1.0 if self._idling else 0.0
        if self._settle < target:
            self._settle = min(target, self._settle + 0.045)
        elif self._settle > target:
            self._settle = max(target, self._settle - 0.045)

        live = self._row()
        drift = self._idle_row() if self._settle > 0.001 else None
        if live is None and drift is None:
            self.update()
            return
        if live is None:
            row = drift
        elif drift is None:
            row = live
        else:
            share = self._settle
            width = min(len(live), len(drift))
            row = [live[i] * (1.0 - share) + drift[i] * share
                   for i in range(width)]

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
        # Eased both ways, up twice as fast as down. Eased up slowly, it lagged
        # a kick by 17 to 50 ms; straight up, every flicker jumped it and the
        # road shook.
        rise = self.BASS_RISE if bass > state.bass else self.BASS_FALL
        state.bass += (bass - state.bass) * rise
        state.mid = state.mid * 0.80 + self._band(row, self.MID) * 0.20
        state.synth = state.synth * 0.88 + self._band(row, self.SYNTH) * 0.12
        high = self._band(row, self.HIGH)
        state.high = state.high * 0.55 + high * 0.45

        # Transients, not loudness: a cymbal is sudden and so is a kick.
        if high - self._last_high > 0.09:
            self._spawn(min(4, int((high - self._last_high) * 22)))
        self._last_high = high
        hand = self._strobe_source == self.BY_HAND
        if not hand:
            state.hit = max(0.0, state.hit - self.HIT_FALL)
        # Sensitivity decides what counts as a hit; rate decides how soon
        # another may follow. At one end the strobe waits for the unmistakable
        # and fires at most twice a bar; at the other it takes almost anything.
        watched = {"Bass": bass, "Mids": state.mid, "Treble": high,
                   "Synths": state.synth}.get(self._strobe_source, bass)
        self._since_hit += 1
        self._decay_kit(state)
        if self._spamming and hand:
            # At whatever rate the slider asks for, on the wall clock.
            now = _time.monotonic()
            if (self._spam_at is None
                    or now - self._spam_at >= self.hand_every()
                    - self.SPAM_SLACK):
                self._spam_at = now
                self.flash(self.HAND_HIT)
        elif self._spamming:
            # Hit again and again, as fast as a person could manage it.
            if self._since_hit >= self.SPAM_EVERY:
                self.flash(self.HAND_HIT)
        elif self._holding and not hand:
            # Held, so it does not decay: the key is the light switch.
            state.hit = self.HAND_HIT
        elif hand:
            pass      # nothing fires by itself; the hotkey is the whole act
        elif not self._fire_from_the_map(state):
            self._fire_from_the_frame(state, watched)
        if hand:
            # The hand strobe has a shape of its own, which is what the sliders
            # set in this mode.
            self._shape_hand(state)
        self._last_watched = watched
        self._last_bass = bass

        self._clock(state)
        state.scroll = (state.scroll + 0.012 + state.bass * 0.05) % 1.0
        state.phase += 0.0045
        state.hue = (state.phase * 0.5) % 1.0
        state.levels = self._level
        state.peaks = self._peak
        state.trace = self._trace_now()
        state.vector = self._vector_now()
        # History belongs to the clock, not the paint, so how much a scene
        # remembers does not depend on how often it is redrawn.
        state.history.append(list(self._level))
        if len(state.history) > self.HISTORY:
            del state.history[:len(state.history) - self.HISTORY]
        if state.trace is not None:
            state.trace_history.append(list(state.trace))
            if len(state.trace_history) > self.HISTORY:
                del state.trace_history[:len(state.trace_history) - self.HISTORY]
        if state.vector is not None:
            state.vector_history.append(list(state.vector))
            if len(state.vector_history) > self.HISTORY:
                del state.vector_history[:len(state.vector_history) - self.HISTORY]
        state.sparks = self._sparks

        if self._dial_frames and not self._idling:
            exact = self._now * self._rate
            index = min(len(self._dial_frames) - 1, max(0, int(exact)))
            self._swing(self._dial_frames[index])
        elif self._idling and self._dial_level:
            self._swing([0.10 + 0.08 * _math.sin(self._drift * 1.6 + i * 0.6)
                         for i in range(len(self._dial_level))])
        state.dials = self._dial_level

        for spark in self._sparks:
            if spark[4] <= 0.0:
                continue
            spark[0] += spark[2]
            spark[1] += spark[3]
            spark[3] += 0.045
            spark[4] -= 0.028
        self.update()

    #: What a VU movement does, per the standard: 300 ms to reach 99% of a
    #: step, with about a percent of overshoot. It is a mass on a spring, so it
    #: is modelled as one.
    VU_SECONDS = 0.30
    #: Damping chosen for that overshoot: exp(-pi*z/sqrt(1-z*z)) is how far a
    #: second-order system overshoots, and 0.83 makes it one percent. At 0.62
    #: the needle overshoots eight percent and slaps the zero pin.
    VU_DAMPING = 0.83

    def _swing(self, wanted) -> None:
        """Move every needle towards its reading as a coil does: accelerated
        towards it, slowed in proportion to its speed, arriving with a
        slight overshoot. An instant rise threw the needle across the face
        in one frame.
        """
        now = _time.monotonic()
        # One frame on the first call; the whole settling time would put every
        # needle at its reading before anyone saw it move.
        step = (1.0 / 60.0 if self._dial_clock is None
                else min(0.1, max(0.0, now - self._dial_clock)))
        self._dial_clock = now
        if step <= 0.0:
            return
        # 4.6 / (zeta * omega) is the time to settle inside one per cent.
        omega = 4.6 / (self.VU_DAMPING * self.VU_SECONDS)
        if len(self._dial_speed) != len(self._dial_level):
            self._dial_speed = [0.0] * len(self._dial_level)
        # Several small steps when a frame runs late: the integrator below is
        # only stable while the step is short against the swing.
        slices = max(1, int(step / 0.02) + 1)
        piece = step / slices
        for index in range(len(self._dial_level)):
            target = wanted[index] if index < len(wanted) else 0.0
            here = self._dial_level[index]
            speed = self._dial_speed[index]
            for _ in range(slices):
                speed += (omega * omega * (target - here)
                          - 2.0 * self.VU_DAMPING * omega * speed) * piece
                here += speed * piece
            # Off the scale is a reading, not a position: the needle stops at
            # the pin.
            if here < 0.0:
                here, speed = 0.0, max(0.0, speed)
            elif here > 1.15:
                here, speed = 1.15, min(0.0, speed)
            self._dial_level[index] = here
            self._dial_speed[index] = speed

    #: How long a hit stays lit, in seconds, per part of the kit: a hat is over
    #: at once and a bass note holds.
    KIT_HOLD = {"Kick": 0.16, "Snare": 0.20, "Hats": 0.07,
                "Bass": 0.30, "Synth": 0.34}

    def _clock(self, state) -> None:
        """Where the playhead is on the beat, for scenes that want it: from
        whichever map has a tempo, preferring the kick, then the strobe's
        source, then nothing, which scenes read as free running.
        """
        state.at = self._now
        # From the position, not the smoothed clock, which keeps creeping
        # briefly after a pause.
        state.moving = (self._moved_at is None
                        or abs(self._position - self._moved_at) > 0)
        # And whether the player is playing, which is known: the clock eases
        # onto a pause for half a second, and scenes ran on past it.
        state.playing = bool(self._wanted and not self._idling)
        state.jumps = self._jumps
        state.rhythm_due = self._rhythm_due
        self._moved_at = self._position
        # The track's shape: how loud it is and which way it leans, a few times
        # a second from end to end. Built once, the first frame after the
        # frames and the traces have both landed.
        if self._contour_from is not self._frames:
            import attachment_audio

            self._contour_from = self._frames
            self._contour = (attachment_audio.contour(
                self._frames, self._vectors,
                self._state.calibration)
                if self._frames else None)
        state.contour = self._contour
        state.harmony = self._harmony
        state.rhythm = self._rhythm
        if self._chart_from is not self._elements:
            # Built once per analysis: rebuilding it every frame would walk
            # every hit in the track sixty times a second.
            self._chart_from = self._elements
            state.chart = {name: tuple(beat.at for beat in found.beats)
                           for name, found in self._elements.items()
                           if getattr(found, "beats", None)}
            state.flux = {name: (found.flux, found.rate)
                          for name, found in self._elements.items()
                          if getattr(found, "flux", None)}
        # The drums' tempo and beat once known, folded over the whole track;
        # the maps below are phased from the first thing they heard and can be
        # a third of a beat out. Against a DJ program's grids the drums' beat
        # is 7 ms out at the median.
        rhythm = self._rhythm
        if rhythm and rhythm.get("beats"):
            # A tempo that moves: counted on the drums' own beats.
            if self._rhythm_clock is None or self._rhythm_clock[0] is not rhythm:
                from beat_clock import BeatClock

                self._rhythm_clock = (rhythm, BeatClock(times=rhythm["beats"]))
            clock = self._rhythm_clock[1]
            here = clock.tempo(self._now)
            state.tempo = visualizers.folded_tempo(here)
            share = here / max(1e-6, state.tempo)
            state.beat_at = (clock.number(self._now) / share) % 1.0
            return
        if rhythm and float(rhythm.get("tempo") or 0.0) > 0.0:
            state.tempo = visualizers.folded_tempo(float(rhythm["tempo"]))
            period = 60.0 / max(1e-6, state.tempo)
            since = self._now - float(rhythm.get("phase") or 0.0)
            state.beat_at = (since / period) % 1.0
            return
        found = None
        for name in ("Kick", "Bass", self._strobe_source, "Mids"):
            candidate = self._beats.get(name)
            if candidate is not None and getattr(candidate, "bpm", 0.0) > 0:
                found = candidate
                break
        if found is None or not found.beats:
            state.tempo = 0.0
            return
        # Folded first, or a detector reporting the pulse doubled drives every
        # scene at twice the song's speed; the phase below uses the same folded
        # period. See folded_tempo.
        state.tempo = visualizers.folded_tempo(found.bpm)
        period = 60.0 / max(1e-6, state.tempo)
        # Against the first beat, not zero: a grid starting at the top of the
        # track is wrong by the length of the intro.
        since = self._now - found.beats[0].at
        state.beat_at = (since / period) % 1.0 if since >= 0 else 0.0

    #: How far the playhead may disagree with the clock before it counts as a
    #: seek rather than drift, and how hard drift is corrected each frame.
    SEEK_GAP = 0.30
    PULL = 0.06
    #: After starting again, the most faster or slower than time the clock runs
    #: while it closes on the player's first report.
    CATCH_RATE = 0.25
    #: The most a hit may be taken early for being nearer this frame than the
    #: next: half a frame at sixty. Half of the time since the last frame, it
    #: fired strobes on beats that had not come.
    NEAREST_MOST = 1.0 / 60.0 / 2.0
    #: How much of the way to the new bass level the scenes' bass goes in a
    #: frame, rising and falling.
    BASS_RISE = 0.6
    BASS_FALL = 0.3
    #: Where in its slot a frame of the bands is heard, in seconds: half a
    #: window in (attachment_audio's WINDOW over twice its DECODE_RATE).
    FRAME_MIDDLE = 2048 / 2 / 48000
    #: How far a report may be run forward before it is distrusted: past any
    #: player's update interval, and inside SEEK_GAP so a stalled source cannot
    #: fake a seek.
    STALE_MOST = 0.25

    #: How long after a seek the picture waits to hear the player move on
    #: before believing it, and how far past the time since the seek its first
    #: report may be.
    SEEK_SETTLE = 1.0
    REPORT_SLACK = 0.1

    def seek_to(self, milliseconds: int) -> None:
        """The player was sent to ``milliseconds``: go there at once and wait
        until the player is heard moving on. Qt's player holds the new
        position for most of a tenth of a second while it restarts; run on
        from the seek, the picture was ahead and spent a second easing back."""
        import time as _time

        self._seek_from = self._heard_now
        self._position = max(0, int(milliseconds))
        self._seek_hold = self._position
        self._seek_at = _time.monotonic()
        self._heard_now = self._position / 1000.0
        self._jumps += 1

    def _heard(self) -> float:
        """The moment the music is at: the player's last report run on from
        when it landed and eased towards each new one, since it only moves
        every 50 ms. Exact while paused, held after a seek until the player
        moves on; an unannounced jump is taken at once."""
        import time as _time

        now = _time.monotonic()
        said = self._position / 1000.0
        step = 0.0 if self._heard_at is None else max(
            0.0, min(0.25, now - self._heard_at))
        self._heard_at = now
        playing = self._wanted and not self._idling
        # Starting to play: the last report is where it stopped, true as of
        # now.
        resumed = playing and not self._was_playing
        word = said != self._said_was
        if word or resumed:
            self._said_was = said
            self._said_at = now
        if resumed:
            self._settling = True
        self._was_playing = playing
        stale = 0.0 if self._said_at is None else now - self._said_at
        if self._seek_hold is not None:
            held = self._seek_hold / 1000.0
            since = now - self._seek_at
            # Moved on: just past where it was sent, by no more than the time
            # since. A report still at the target, or before it, is waited
            # through; anything else is a jump of its own.
            moved_on = (playing
                        and held < said <= held + since + self.REPORT_SLACK)
            late = (said == held or (self._seek_from is not None and abs(
                said - self._seek_from) <= self.SEEK_GAP))
            if not moved_on and late and since <= self.SEEK_SETTLE:
                self._heard_now = held
                return held
            self._seek_hold = None
            if not moved_on and abs(said - held) > self.SEEK_GAP:
                self._jumps += 1
            self._heard_now = said
            return said
        if not playing or resumed:
            self._heard_now = said
            return said
        run = said + min(self.STALE_MOST, stale)
        if self._settling and word:
            # The player's first report since starting again, which can be some
            # way from a clock run on from the pause: closed at a little faster
            # or slower than time, never in a jump.
            self._settling = False
            self._catching = True
        if self._heard_now is None or abs(run - self._heard_now) > self.SEEK_GAP:
            if self._heard_now is not None:
                self._jumps += 1
            self._heard_now = run
            return run
        if stale <= self.STALE_MOST:
            self._heard_now += step
        gap = run - self._heard_now
        if self._catching:
            most = self.CATCH_RATE * step
            self._heard_now += max(-most, min(most, gap))
            self._catching = abs(gap) > most
        else:
            self._heard_now += gap * self.PULL
        return self._heard_now

    def _ahead(self) -> float:
        """How far ahead of the player's position the picture is shown (see
        av_sync); nothing without an allowance."""
        if self.allowance is None:
            return 0.0
        screen = self.screen()
        refresh = screen.refreshRate() if screen is not None else 60.0
        return self.allowance.ahead(refresh)

    def _decay_kit(self, state) -> None:
        """Light whichever parts of the kit are due, and fade the rest. A scene
        asks how recently the kick was hit, which, unlike the playhead
        against a list of times, does not depend on the frame rate.
        """
        import beatmap

        now = self._now
        step = max(0.0, min(0.25, now - self._kit_seen)) if self._kit_seen >= 0 else 0.0
        seeking = self._kit_seen < 0 or now < self._kit_seen or step >= 0.25
        for name in beatmap.ELEMENTS:
            found = self._elements.get(name)
            beats = getattr(found, "beats", ())
            hold = self.KIT_HOLD.get(name, 0.2)
            was = state.kit.get(name, 0.0)
            state.kit[name] = max(0.0, was - (step / hold if hold else 1.0))
            if not beats:
                continue
            cursor = self._kit_at.get(name, 0)
            if seeking:
                nxt = beatmap.next_after(beats, now)
                self._kit_at[name] = beats.index(nxt) if nxt is not None else len(beats)
                continue
            # In the frame nearest the hit, not the first after it, which made
            # hits up to a frame late.
            while (cursor < len(beats)
                   and beats[cursor].at <= now + min(self.NEAREST_MOST,
                                                     step / 2)):
                state.kit[name] = max(0.35, beats[cursor].strength)
                cursor += 1
            self._kit_at[name] = cursor
        self._kit_seen = now

    def _fire_from_the_map(self, state) -> bool:
        """Flash because a beat is due; returns whether the map was used.

        The map is the whole track's beats, found before playback, so this
        is a lookup, not detection: the flash lands on the beat and the grid
        carries on through gaps. Sensitivity picks how hard a beat must be
        hit to count and rate how close flashes may come, so turning one
        down thins the lighting rather than switching it off.
        """
        found = self._beats.get(self._strobe_source)
        beats = getattr(found, "beats", ())
        if not beats:
            return False
        now = self._now
        # A seek either way starts again from where the playhead landed.
        if now < self._beat_seen or now - self._beat_seen > 1.0:
            import beatmap
            nxt = beatmap.next_after(beats, now)
            self._beat_at = beats.index(nxt) if nxt is not None else len(beats)
            self._beat_seen = now
            return True
        floor = 0.06 + (1.0 - self._strobe_sense) * 0.72
        gap = 0.08 + (1.0 - self._strobe_rate) * 1.60
        # A held note gets one flash on a grid, and then nothing until the next
        # bar. When the watched band holds and both knobs are up, the grid is
        # divided and the strobe runs at a multiple of the beat. Both knobs
        # must be up on purpose: this is the loudest thing the visualiser does.
        self._machine_gun(state, beats, now, floor)
        # In the frame nearest the beat, as the kit is.
        due = now + min(self.NEAREST_MOST,
                        max(0.0, now - self._beat_seen) / 2)
        while (self._beat_at < len(beats)
               and beats[self._beat_at].at <= due):
            beat = beats[self._beat_at]
            self._beat_at += 1
            if beat.strength < floor:
                continue
            if self._since_hit / 60.0 < gap:
                continue
            state.hit = min(1.0, 0.55 + beat.strength * 0.45)
            self._since_hit = 0
        self._beat_seen = now
        return True

    #: Both knobs have to be past this before the grid is subdivided.
    RAPID_KNOB = 0.62
    #: And the watched band has to have been this loud for this long.
    RAPID_LEVEL = 0.45
    RAPID_HOLD = 0.35
    #: The fastest it runs, in flashes a second. Photosensitive epilepsy is
    #: most often provoked between fifteen and twenty a second, and
    #: accessibility guidance draws its line at three. The strobe is off by
    #: default and this speed needs both sliders most of the way up, but ten is
    #: capped on purpose, and the control says so.
    RAPID_CEILING = 10.0

    def _sustained(self, state) -> float:
        """How long the watched band has held, in seconds, from the smoothed
        level: a beat list only knows where notes started.
        """
        watched = {"Bass": state.bass, "Kick": state.bass,
                   "Mids": state.mid, "Synths": state.synth,
                   "Synth": state.synth, "Treble": state.high,
                   "Snare": state.mid, "Hats": state.high}.get(
                       self._strobe_source, state.bass)
        now = self._now
        step = max(0.0, min(0.25, now - self._held_seen))
        self._held_seen = now
        if watched >= self.RAPID_LEVEL:
            self._held += step
        else:
            self._held = 0.0
        return self._held

    def _machine_gun(self, state, beats, now: float, floor: float) -> None:
        """Run the strobe at a multiple of the beat while a note holds."""
        if (self._strobe_rate < self.RAPID_KNOB
                or self._strobe_sense < self.RAPID_KNOB):
            self._held = 0.0
            self._held_seen = now
            return
        if self._sustained(state) < self.RAPID_HOLD:
            return
        # How far past the switch-on point both knobs are, which sets the
        # subdivision: just past doubles the beat, all the way is the fastest.
        over = min(1.0, ((self._strobe_rate - self.RAPID_KNOB)
                         + (self._strobe_sense - self.RAPID_KNOB))
                   / (2.0 * (1.0 - self.RAPID_KNOB)))
        period = self._beat_period(beats)
        if period <= 0.0:
            return
        divisions = 2 ** int(1 + over * 2.99)          # 2, 4 or 8
        every = max(1.0 / self.RAPID_CEILING, period / divisions)
        if now - self._rapid_at < every:
            return
        self._rapid_at = now
        state.hit = max(state.hit, 0.72 + over * 0.28)
        self._since_hit = 0

    def _beat_period(self, beats) -> float:
        """The gap between beats, from the map or from the gaps in it."""
        found = self._beats.get(self._strobe_source)
        bpm = getattr(found, "bpm", 0.0)
        if bpm:
            return 60.0 / bpm
        if len(beats) >= 3:
            gaps = sorted(beats[i + 1].at - beats[i].at
                          for i in range(min(24, len(beats) - 1)))
            return gaps[len(gaps) // 2]
        return 0.0

    def _fire_from_the_frame(self, state, watched: float) -> None:
        """Flash on a jump in one band, while there is no map yet. The
        threshold is absolute, so a quiet track never reaches it and a loud
        one is always past it; hence only a fallback.
        """
        jump = 0.40 - self._strobe_sense * 0.39    # 0.40 fussy, 0.01 eager
        wait = int(75 - self._strobe_rate * 73)    # frames before the next
        if watched - self._last_watched > jump and self._since_hit >= wait:
            state.hit = 1.0
            self._since_hit = 0

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
        """Paint, and never leave the painter open. An exception out of
        paintEvent is caught and printed by Qt, which carries on with a
        painter still active on the backing store and then crashes.
        """
        if self._canvas is not None:
            # The canvas covers the pane and draws it, so any repaint of the
            # pane is a repaint of the canvas.
            self._canvas.update()
            return
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = QRectF(self.rect())
        # Every pixel, every frame, first. Neither WA_OpaquePaintEvent nor
        # autoFillBackground is set, so the backing store keeps the last frame,
        # and the paths that do not cover the widget (closed strip, reveal,
        # fade-in) left a ghost of it.
        painter.fillRect(rect, self._state.background)
        if self._reserve:
            # Kept clear for the floating control bar, always rather than while
            # it shows, so no button sits on the scene and nothing resizes when
            # the bar fades.
            rect = rect.adjusted(0.0, 0.0, 0.0,
                                 -min(float(self._reserve), rect.height() / 3.0))
        if self._reveal <= 0.001:
            return
        # The chosen shape fitted inside the room there is, with the sides
        # filled, as a video player does. By height alone every ratio clamped
        # to the same number.
        scene_box = self._scene_box(rect)
        if scene_box != rect:
            rect = scene_box
        if self._reveal < 0.999:
            painter.setOpacity(self._reveal)
            painter.translate(0.0, (1.0 - self._reveal) * rect.height() * 0.45)
        # Before the new scene's fade, which waits for the analysis and hid the
        # progress bar.
        if not self._level:
            painter.fillRect(rect, QColor(8, 6, 18))
            if self._working is not None:
                self._draw_working(painter, rect)
                return
            painter.setPen(QPen(QColor(150, 150, 170, 120)))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter,
                             "the spectrum appears when something is playing")
            return
        if self._fresh < 0.999 and not (self._on_gpu and self._crossing):
            # The new scene coming up over the background. On the card it is
            # drawn whole and faded up over the last frame instead: see
            # _GpuCanvas.hold.
            painter.setOpacity(painter.opacity() * self._fresh)
        self._paint_scene(painter, rect)
        self._tell_listener()

    def set_listener(self, listener) -> None:
        """Something handed the scene after every frame (the rider's sounds,
        which answer what the game did); None to stop."""
        self._listener = listener

    def _tell_listener(self) -> None:
        listener = getattr(self, "_listener", None)
        if listener is None:
            return
        try:
            listener(self._scene)
        except Exception:      # noqa: BLE001 - the picture goes on without it
            import logging

            logging.getLogger(__name__).exception(
                "a listener on the pane failed; it will not be asked again")
            self._listener = None

    def _paint_scene(self, painter, rect) -> None:
        """The scene, then whatever polish it asks for. A scene with no
        post-processing is drawn straight onto the widget.
        """
        import visualizers

        import time as _time

        # Nothing a scene is handed is outside its range. See
        # SpectrumState.settle.
        self._state.settle()
        self._drawn += 1
        recipe = visualizers.post_for(self._scene) if self._post else {}
        ratio = self.devicePixelRatioF()
        pixels = rect.width() * ratio * rect.height() * ratio
        # Antialiasing is what these scenes cost, per pixel of every stroke
        # (Ambience: 10.3 ms at 1080p with it, 1.6 without). A frame that will
        # not fit is drawn smaller and stretched instead, which costs the same
        # and stays smooth. How much smaller is measured: see Sharpness and
        # SHARP_PIXELS.
        if self._on_gpu:
            # A scene with a world of its own to draw on the card draws that,
            # polish and all. See rider_gl.
            on_card = getattr(self._scene, "paint_on_card", None)
            world = self._world() if on_card is not None else None
            if world is not None:
                try:
                    world.samples = self._canvas.world_samples
                    on_card(painter, rect, self._state, world)
                    return
                except Exception:      # noqa: BLE001 - the flat one is there
                    import logging

                    logging.getLogger(__name__).exception(
                        "the rider's world failed on this card; drawing it "
                        "flat from here on")
                    self._canvas.world = None
                    self._canvas.world_failed = True
            # Straight in, at every pixel the screen has: no buffer to shrink
            # into and nothing stretched. The polish goes on afterwards; see
            # _paint_on_gpu.
            self._scene.paint(painter, rect, self._state)
            if recipe:
                self._gpu_scene = (QRectF(rect), painter.worldTransform(),
                                   painter.opacity(), recipe)
            return
        shrink = self._sharpness.scale_for(pixels, ratio, self._scene)
        if (not recipe and shrink >= 0.999) or rect.width() < 8.0 or rect.height() < 8.0:
            started = _time.perf_counter()
            self._scene.paint(painter, rect, self._state)
            self._sharpness.record(
                (_time.perf_counter() - started) * 1000.0, ratio)
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
        started = _time.perf_counter()
        self._scene.paint(inner, QRectF(0, 0, side.width(), side.height()),
                          self._state)
        inner.end()
        self._sharpness.record((_time.perf_counter() - started) * 1000.0, ratio)
        if recipe:
            self._effects.apply(painter, rect, self._buffer, recipe,
                                getattr(self._scene, "stretch_smooth", False))
        else:
            blit_scene(painter, rect, self._buffer,
                       getattr(self._scene, "stretch_smooth", False))

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


class _KeysCard(QWidget):
    """The list of playing keys, hidden until asked for: ``?`` opens and closes
    it, and it is the only thing in full screen that does not fade on its
    own. Painted rather than built from a dozen labels.
    """

    #: The keys, in the order they are worth learning.
    KEYS = (
        ("1 – 9", "the scenes, in the order the menu lists them"),
        ("← →", "change lane, in Music rider"),
        ("X", "the game's sounds on or off"),
        ("S", "strobe on or off"),
        ("A / D", "step through what the strobe listens to"),
        ("M", "listen to nobody: nothing fires but G and H"),
        ("G", "flash by hand: tap for a flash, hold for a held light"),
        ("H", "hold for a strobe, twelve a second"),
        (None, None),
        ("J / K / L", "back ten seconds, play or pause, forward ten"),
        ("space", "play or pause"),
        ("?", "this list"),
        ("esc", "leave full screen"),
    )

    PAD = 26
    LINE = 26
    GAP = 34

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setVisible(False)

    def wanted(self) -> QSize:
        """How big the card has to be to hold what is in it."""
        metrics = QFontMetricsF(self._face())
        widest = 0.0
        for key, what in self.KEYS:
            if key is None:
                continue
            widest = max(widest, metrics.horizontalAdvance(what))
        keys = max(metrics.horizontalAdvance(k or "")
                   for k, _w in self.KEYS)
        tall = self.PAD * 2 + self.LINE * len(self.KEYS) + self.LINE
        return QSize(int(self.PAD * 2 + keys + self.GAP + widest),
                     int(tall))

    def _face(self):
        from widgets import system_font

        font = system_font()
        font.setPointSizeF(13.0)
        return font

    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            panel = QRectF(self.rect())
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(10, 10, 16, 232))
            painter.drawRoundedRect(panel, 14.0, 14.0)
            painter.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5),
                                    14.0, 14.0)

            font = self._face()
            painter.setFont(font)
            metrics = QFontMetricsF(font)
            keys = max(metrics.horizontalAdvance(k or "")
                       for k, _w in self.KEYS)
            heading = QFont(font)
            heading.setBold(True)
            painter.setFont(heading)
            painter.setPen(QColor(236, 236, 244))
            y = self.PAD
            painter.drawText(QRectF(self.PAD, y, self.width(), self.LINE),
                             Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter, "Playing keys")
            y += self.LINE
            painter.setFont(font)
            for key, what in self.KEYS:
                if key is None:
                    y += self.LINE * 0.5
                    continue
                painter.setPen(QColor(150, 210, 255))
                painter.drawText(QRectF(self.PAD, y, keys, self.LINE),
                                 Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignVCenter, key)
                painter.setPen(QColor(206, 206, 218))
                painter.drawText(
                    QRectF(self.PAD + keys + self.GAP, y,
                           self.width(), self.LINE),
                    Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter, what)
                y += self.LINE
        finally:
            painter.end()


class _ControlBar(QWidget):
    """The floating strip of controls, painted rather than stylesheeted: a
    stylesheet gives only a colour and a radius, which looked like a hole in
    the picture. A gradient, a hairline highlight along the top and a shadow
    make it float. Qt has no box-shadow, and the bar's one graphics effect
    is its fade.
    """

    #: How far the shadow reaches past the panel, in pixels.
    SHADOW = 18
    RADIUS = 12

    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        # The panel is inset by the shadow's reach on every side, and the
        # layout's margins come from the same number, so the controls stay
        # inside the panel.
        panel = QRectF(self.rect()).adjusted(
            self.SHADOW, self.SHADOW, -self.SHADOW, -self.SHADOW)
        if panel.width() < 8 or panel.height() < 8:
            return

        # The shadow, as a few rounded rectangles of falling opacity; a blur
        # would cost a full-size image every frame the bar fades.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for step in range(5, 0, -1):
            spread = step * (self.SHADOW / 5.0)
            painter.setPen(QPen(QColor(0, 0, 0, 30), spread * 1.6))
            painter.drawRoundedRect(panel.adjusted(-spread * 0.4, -spread * 0.4,
                                                   spread * 0.4, spread * 0.4),
                                    self.RADIUS + spread * 0.4,
                                    self.RADIUS + spread * 0.4)

        # The panel: lit from above, like every other surface in the app.
        fill = QLinearGradient(panel.topLeft(), panel.bottomLeft())
        fill.setColorAt(0.0, QColor(34, 32, 44, 236))
        fill.setColorAt(1.0, QColor(14, 13, 20, 232))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(fill)
        painter.drawRoundedRect(panel, self.RADIUS, self.RADIUS)

        # A hairline round the outside, and a brighter one along the top.
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(255, 255, 255, 28), 1.0))
        painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5),
                                self.RADIUS, self.RADIUS)
        painter.setPen(QPen(QColor(255, 255, 255, 46), 1.0))
        painter.drawLine(QPointF(panel.left() + self.RADIUS, panel.top() + 1.0),
                         QPointF(panel.right() - self.RADIUS, panel.top() + 1.0))


class FullScreenSpectrum(QWidget):
    """The scene alone, filling the screen, with controls that get out of the
    way.

    It borrows the running Spectrum, so there is one analysis and one timer
    however many windows look, and gives it back on the way out. The
    controls fade after a few seconds of stillness and return on movement,
    and stay while the pointer is on them.
    """

    #: Stillness before the controls go, and before the pointer does.
    IDLE_MS = 2600

    def __init__(self, spectrum: Spectrum, owner=None) -> None:
        super().__init__(None)
        self.setWindowTitle("Visualiser")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMouseTracking(True)
        # Near black, not the theme's colour: a scene that does not fill the
        # screen shows this at the sides.
        self.setAutoFillBackground(True)
        palette = self.palette()
        palette.setColor(self.backgroundRole(), QColor(6, 5, 9))
        self.setPalette(palette)
        self._spectrum = spectrum
        self._owner = owner
        self._home = spectrum.parentWidget()
        self._layout_index = None
        #: Whether the pointer is hidden, so showing it again happens once
        #: rather than on every mouse move.
        self._hidden_cursor = False

        parent_layout = self._home.layout() if self._home else None
        self._stretch = 0
        if parent_layout is not None:
            self._layout_index = parent_layout.indexOf(spectrum)
            if self._layout_index >= 0 and hasattr(parent_layout, "stretch"):
                self._stretch = parent_layout.stretch(self._layout_index)
        self._min, self._max = spectrum.minimumHeight(), spectrum.maximumHeight()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        spectrum.set_unbounded(True)
        spectrum.setParent(self)
        layout.addWidget(spectrum)

        self.bar = _ControlBar(self)
        self.bar.setMouseTracking(True)
        self.bar.setObjectName("visualiserBar")
        # The same wrapping row the window uses, so a small screen gets a
        # second line. Its margins leave room for the shadow painted outside
        # the panel.
        pad = _ControlBar.SHADOW
        self._bar_layout = FlowRow(spacing=14)
        self._bar_layout.setContentsMargins(pad + 16, pad + 11,
                                            pad + 16, pad + 11)
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

        #: The list of playing keys, hidden until the key that shows it is
        #: pressed.
        self.keys = _KeysCard(self)

    # -- what goes in the bar ---------------------------------------------
    #: A slider's size hint describes its groove, not its handle, so rows sized
    #: from hints cut off the tops of the knobs.
    CONTROL_HEIGHT = 30

    def add_control(self, widget, stretch: int = 0) -> None:
        widget.setMouseTracking(True)
        if widget.sizeHint().height() < self.CONTROL_HEIGHT:
            widget.setMinimumHeight(self.CONTROL_HEIGHT)
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
        if self._hidden_cursor:
            # Only when it is hidden: changing a cursor walks the widget tree
            # and tells the window system, and this ran on every mouse move.
            self._hidden_cursor = False
            self.unsetCursor()
        self._spectrum.set_giving_way(True)
        self._idle.start()

    def _hide_controls(self) -> None:
        if self.bar.underMouse():
            self._idle.start()
            return
        self._fade.stop()
        self._fade.setStartValue(self._effect.opacity())
        self._fade.setEndValue(0.0)
        self._fade.start()
        if not self._hidden_cursor:
            self._hidden_cursor = True
            self.setCursor(Qt.CursorShape.BlankCursor)
        self._spectrum.set_giving_way(False)

    def keyPressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Escape leaves; J, K and L work the transport; the rest play it. One
        method: with two, the later one won and the transport keys were
        dead.
        """
        if event.key() in (Qt.Key.Key_Question, Qt.Key.Key_Slash):
            self.show_keys(not self.keys.isVisibleTo(self))
            event.accept()
            return
        keys = {Qt.Key.Key_J: "back", Qt.Key.Key_K: "toggle",
                Qt.Key.Key_L: "forward", Qt.Key.Key_Space: "toggle"}
        action = keys.get(event.key())
        if action is not None:
            # The transport moves the playhead, which the bar shows, so these
            # bring it back.
            self._show_controls()
            handler = getattr(self._owner, "transport", None)
            if handler is not None:
                handler(action)
                event.accept()
                return
        if event.key() == Qt.Key.Key_Escape:
            if self.keys.isVisibleTo(self):
                self.show_keys(False)
            else:
                self.close()
            event.accept()
            return
        # The playing keys do not wake the bar: changing scene with a number
        # would slide the controls over the picture each time.
        if self._play_it(event, held=True):
            return
        self._show_controls()
        super().keyPressEvent(event)

    def show_keys(self, on: bool) -> None:
        """Show or hide the list of playing keys."""
        self.keys.setVisible(bool(on))
        if on:
            self._place_keys()
            self.keys.raise_()

    def _place_keys(self) -> None:
        size = self.keys.wanted()
        self.keys.setGeometry(int((self.width() - size.width()) / 2),
                              int((self.height() - size.height()) / 2),
                              size.width(), size.height())

    def keyReleaseEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Letting the strobe key go puts the light out. Auto-repeat is ignored
        on both sides, or a held light switches itself off at the keyboard's
        repeat rate.
        """
        if self._play_it(event, held=False):
            return
        super().keyReleaseEvent(event)

    def _play_it(self, event, held: bool) -> bool:
        """Hand one of the playing keys to whoever owns the controls."""
        if event.isAutoRepeat():
            event.accept()
            return True
        owner = self._owner
        reader = getattr(owner, "vj_action", None)
        handler = getattr(owner, "vj", None)
        if reader is None or handler is None:
            return False
        found = reader(event.key())
        if found is None:
            return False
        action, value = found
        if action in ("flash", "spam"):
            action = action if held else "un" + action
        elif not held:
            return False      # everything else acts on the way down only
        if handler(action, value):
            event.accept()
            return True
        return False

    def mouseMoveEvent(self, event) -> None:      # noqa: N802 - Qt's name
        self._show_controls()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """A click brings the controls back, as a move does: on a trackpad the
        first thing anybody does is click.
        """
        self._show_controls()
        super().mousePressEvent(event)

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._place_bar()
        if self.keys.isVisibleTo(self):
            self._place_keys()

    def showEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().showEvent(event)
        self._place_bar()

    def _place_bar(self) -> None:
        # Wide enough that the seek bar is clearly the long one and the volume
        # clearly the short one, but never wider than the screen.
        width = max(320, min(int(self.width() * 0.86), self.width() - 48))
        # Margins included: the layout counts its own, and adding them here as
        # well made the bar half as tall again.
        height = max(48 + _ControlBar.SHADOW * 2,
                     self._bar_layout.heightForWidth(width))
        # The widget is the panel plus its shadow, so the gap at the bottom is
        # measured to the panel.
        self.bar.setGeometry(
            int((self.width() - width) / 2),
            int(self.height() - height - 30 + _ControlBar.SHADOW),
            width, height)
        self.bar.raise_()
        # No strip kept clear: full screen is the whole screen, and the bar
        # fades when unused.
        self._spectrum.set_reserve(0)

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        """Put the spectrum back exactly where it was."""
        self._idle.stop()
        self._fade.stop()
        spectrum = self._spectrum
        spectrum.setParent(None)
        spectrum.set_reserve(0)
        # set_unbounded recomputes the height from the shape and the budget;
        # restoring the saved minimum and maximum forced a height and put the
        # transport inside the picture.
        spectrum.set_unbounded(False)
        parent_layout = self._home.layout() if self._home else None
        if parent_layout is not None and self._layout_index is not None:
            # With the stretch it had: insertWidget defaults to none.
            parent_layout.insertWidget(self._layout_index, spectrum,
                                       self._stretch)
        elif self._home is not None:
            spectrum.setParent(self._home)
        if spectrum.parentWidget() is not None:
            spectrum.show()
        else:
            # Nowhere to go back to: shown here, the spectrum would become a
            # window of its own that outlives everything that knew about it.
            spectrum.hide()
        if self._owner is not None:
            release = getattr(self._owner, "release_full_screen", None)
            if release is not None:
                release()
            else:
                self._owner._full = None
        # The window the scene came from, back in front; otherwise the main
        # window came forward and covered the viewer.
        home = spectrum.window()
        if home is not None and home is not self:
            home.raise_()
            home.activateWindow()
        super().closeEvent(event)


class FlowRow(QLayout):
    """A row of controls that wraps instead of running off the edge: what fits
    goes on a line and the rest moves down.
    """

    #: Widgets here can draw outside their rectangle (a combo box reserves
    #: about eleven pixels for its focus ring), so the gap is wider than that.
    BLEED = 14

    def __init__(self, parent=None, spacing: int = 16) -> None:
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
        """How tall the controls are at this width, margins included:
        setGeometry takes them off again, so both must count them.
        """
        margins = self.contentsMargins()
        inner = max(0, width - margins.left() - margins.right())
        rows = self._lay(QRect(0, 0, inner, 0), apply=False)
        return rows + margins.top() + margins.bottom()

    def setGeometry(self, rect) -> None:      # noqa: N802 - Qt's name
        """Lay the controls out inside the margins: a QLayout subclass must
        inset its rectangle by its own contents margins.
        """
        super().setGeometry(rect)
        margins = self.contentsMargins()
        self._lay(rect.adjusted(margins.left(), margins.top(),
                                -margins.right(), -margins.bottom()),
                  apply=True)

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
            # Never below what the widget needs: sized to the hint alone, combo
            # boxes and tick boxes came out two pixels short and lost the
            # bottoms of their letters.
            hint = item.sizeHint()
            # From the widget, not the item: a QWidgetItem's minimumSize is the
            # minimum set on the widget, usually none.
            least = (widget.minimumSizeHint() if widget is not None
                     else item.minimumSize())
            wanted = QSize(max(hint.width(), least.width()),
                           max(hint.height(), least.height()))
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
                    # Centred on the line, or a combo box and a tick box read
                    # as two rows.
                    placed = QRect(box)
                    placed.moveTop(y + (tallest - box.height()) // 2)
                    item.setGeometry(placed)
            y += tallest
        return y - rect.y()


def blit_scene(painter, rect, buffer, smooth: bool = False) -> None:
    """Put a scene's buffer on the screen at the size it has to be: smoothly,
    unless it goes up by a whole number of pixels.

    ``smooth`` overrides that for a scene that asks: doubling suits thin
    bright lines and not arcs and lettering (see Scene.stretch_smooth).
    Measured at 1512x982 on a 2x display, as the mean step in brightness
    between neighbouring pixels:

        full resolution                 0.0040
        half resolution, smoothed       0.0029
        half resolution, not smoothed   0.0040

    Smoothing lost 42 per cent of the edge, which reads as a greyer picture,
    and was slower (2.35 ms against 1.92). Only for a whole-number stretch:
    at 0.8 or 0.67 some pixels would double and their neighbours not, and
    the unevenness crawls as the scene moves.
    """
    target = QRectF(rect)
    if buffer.width() <= 0 or target.width() <= 1.0:
        return
    ratio = painter.device().devicePixelRatioF() or 1.0
    grew = target.width() * ratio / buffer.width()
    whole = abs(grew - round(grew)) < 0.02 and round(grew) >= 1
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform,
                          smooth or not whole)
    painter.drawPixmap(target, buffer, QRectF(buffer.rect()))


class Sharpness:
    """How much of the screen's own resolution a scene is drawn at.

    Antialiased strokes cost by area, so a scene that paints in a
    millisecond in a strip costs thirty at full screen. A fixed pixel budget
    put full screen below its logical resolution while a windowed strip drew
    at 1.8 times it. So it is measured: draw at the largest scale that fits
    the frame, which is a sixtieth of a second while the picture is sharper
    than the window and a thirtieth once it is not, since below one buffer
    pixel per point the picture goes soft.

    That line is 0.5 of the pixels on a 2x display. The rungs are coarse and
    decisions settle, so the buffer is not reallocated every frame and a scene
    between two rungs does not flicker.
    """

    #: Fractions of the screen's real pixels; a display's own ratio always has
    #: a rung (see _rungs). Each divides into 1, so the buffer goes up by a
    #: whole number and blit_scene need not smooth it, which matters more than
    #: the resolution. Mean step in brightness at 1512x982 on a 2x display:
    #:
    #:     1.00  a whole 1x   0.0020        0.80  1.25x   0.0015
    #:     0.50  a whole 2x   0.0020        0.67  1.49x   0.0015
    #:     0.25  a whole 4x   0.0019        0.40  2.50x   0.0016
    #:
    #: A quarter of the resolution stretched evenly holds more edge than four
    #: fifths stretched unevenly, so the uneven rungs are gone.
    SCALES = (1.0, 0.50, 1.0 / 3.0, 0.25)

    #: The least it will ever draw at, however slow the machine.
    FLOOR = 0.22

    #: What the scene may take at sixty a second, and at thirty; the rest of
    #: the frame is the polish (6.5 ms) and Qt.
    SMOOTH_MS = 8.5
    SOFT_MS = 24.0

    #: Frames ignored after a scene or size changes: a scene's first frame is
    #: fonts, gradients and first-time branches (the Equaliser's: 64 ms against
    #: 1.4 settled), and one such reading convinced the governor full
    #: resolution was impossible.
    WARMUP = 24

    def __init__(self) -> None:
        self._scale = 0.0          # 0 means "not chosen yet"
        self._cost = 0.0
        self._settle = 0
        self._warm = self.WARMUP
        self._key = None
        #: What each rung measured once tried; a guess is only used for a rung
        #: nothing is known about.
        self._seen: dict = {}

    # -- what to draw at ---------------------------------------------------
    def _rungs(self, ratio: float) -> tuple:
        """The scales, with the display's logical resolution among them: on
        1.5x or 1.25x displays it is added, since the budget changes there.
        """
        logical = 1.0 / max(1.0, ratio)
        rungs = set(self.SCALES)
        rungs.add(round(logical, 4))
        return tuple(sorted((r for r in rungs if r >= self.FLOOR),
                            reverse=True))

    def scale_for(self, pixels: float, ratio: float, scene) -> float:
        """The fraction of ``pixels`` to draw for this scene and screen. A new
        scene or size starts again at the sharpest rung it is likely to
        hold.
        """
        rungs = self._rungs(ratio)
        key = (id(scene), int(pixels / 100_000.0))
        if key != self._key:
            self._key = key
            self._cost = 0.0
            self._settle = 0
            self._warm = self.WARMUP
            self._seen = {}
            self._scale = self._first(pixels, rungs, ratio, scene)
        return self._scale

    def _first(self, pixels: float, rungs: tuple, ratio: float, scene) -> float:
        """Where to begin before anything is measured: small frames sharp, big
        ones at the screen's logical resolution, climbing if the machine has
        room.
        """
        floor = int(getattr(scene, "sharp_pixels", 0) or 0)
        if pixels <= max(600_000, floor):
            return rungs[0]
        logical = round(1.0 / max(1.0, ratio), 4)
        return next((r for r in rungs if r <= logical), rungs[-1])

    # -- what it cost ------------------------------------------------------
    def record(self, taken_ms: float, ratio: float) -> None:
        """Time one scene paint, and move a rung when the average asks."""
        if self._warm > 0:
            self._warm -= 1
            return
        self._cost = (taken_ms if self._cost <= 0.0
                      else self._cost * 0.8 + taken_ms * 0.2)
        if self._settle > 0:
            self._settle -= 1
            return
        self._seen[self._scale] = self._cost
        rungs = self._rungs(ratio)
        if self._scale not in rungs:
            return
        at = rungs.index(self._scale)
        here = self._budget_for(self._scale, ratio)
        if self._cost > here and at < len(rungs) - 1:
            self._step(rungs[at + 1])
        elif self._cost < here * 0.45 and at > 0:
            # Against the rung above's budget, not this one's: the budget
            # changes at logical resolution, and a scene just under it stepped
            # up and back down every 45 frames.
            up = rungs[at - 1]
            if self._predict(up) < self._budget_for(up, ratio):
                self._step(up)

    def _predict(self, scale: float) -> float:
        """What a frame would cost at ``scale``: measured if the rung was ever
        drawn (Waterfall costs 14 ms at 960x540 and 24 at 1267x713, where
        any tidy model says 18). For a rung never tried, the guess is linear
        in the side, not the area: strokes cost by length. Over a 28-fold
        range of area, Vaporwave's cost grew five-fold.
        """
        known = self._seen.get(scale)
        if known is not None:
            return known
        if self._scale <= 0.0:
            return self._cost
        return self._cost * scale / self._scale

    def _step(self, to: float) -> None:
        # Seeded with the new rung's expected cost: zero makes the next frame
        # look free and sends it straight back.
        self._cost = self._predict(to)
        self._scale = to
        self._settle = 45

    def _budget_for(self, scale: float, ratio: float) -> float:
        """Sixty a second while the picture is sharp, thirty once it is not."""
        logical = 1.0 / max(1.0, ratio)
        return self.SMOOTH_MS if scale > logical + 1e-6 else self.SOFT_MS

    def budget_ms(self, ratio: float) -> float:
        """What the scene is allowed at the scale it is drawing at."""
        return self._budget_for(self._scale, ratio)

    def interval_ms(self, ratio: float, frame_ms: int,
                    extra: float = 0.0) -> int:
        """How often to repaint, given what a frame costs. Painting for longer
        than the timer's period fills the event queue and the pane stops
        answering the mouse, so the timer is told the truth. ``extra`` is
        the polish pass.
        """
        whole = self._cost + max(0.0, extra)
        if whole <= frame_ms * 0.85:
            return frame_ms
        return max(frame_ms, min(33, int(whole * 1.1) + 1))


class CardSharpness:
    """What a frame on the graphics card may ask of it.

    Measured on an M1, the least of Apple's GPUs, for Music rider with all
    its polish at every real pixel and four samples a pixel:

    2560x1664 8.9 ms 3456x2234 13.8 ms 6016x3384 32.3 ms 3024x1964 11.2 ms
    5120x2880 22.7 ms

    What gives is the multisampling: at Retina density two samples are most
    of the smoothness of four, and at 5K half the cost; after that, none.
    Never the resolution: dropping it reacted to the first seconds of a
    track, when the analysis is busy, and left full screen at half its
    pixels. A frame that will not fit at every pixel with no samples is
    drawn a little later instead. The whole finished frame is measured,
    since drawing calls return before the card is done.
    """

    #: Everything the card does for a frame at sixty a second; the rest is Qt
    #: and the app.
    BUDGET_MS = 12.5

    #: Frames not believed after a scene, size or rung changes: new
    #: framebuffers' first frames are setup. See Sharpness.
    WARMUP = 24
    SETTLE = 12

    #: How many frames a rung is judged on, by their median. A running average
    #: let one stray slow frame in thirty halve a MacBook Pro's resolution for
    #: frames that were 10.5 ms nine times in ten.
    WINDOW = 30

    #: What the rung above is expected to cost against this one until drawn at:
    #: half the samples measured 1.2 to 1.5 times cheaper, half the pixels
    #: about twice.
    DEARER_SAMPLES = 1.5
    DEARER_PIXELS = 2.0

    #: Frames before a rung that did not fit is tried again (five seconds at
    #: sixty), doubling each time it still does not.
    RETRY = 300

    def __init__(self) -> None:
        self._rung = None
        self._cost = 0.0
        self._settle = 0
        self._warm = self.WARMUP
        self._key = None
        self._window: list = []
        self._count = 0
        #: What each rung measured, once it has been drawn at.
        self._seen: dict = {}
        #: When each rung that did not fit may be tried again, and how long the
        #: next wait will be.
        self._retry_at: dict = {}
        self._backoff: dict = {}
        #: The best rung that has fitted at each size this session, whatever
        #: was drawn. See choice.
        self._fits: dict = {}

    @staticmethod
    def rungs(ratio: float) -> tuple:
        """(samples a pixel, share of the screen's pixels), best first: every
        pixel, whatever the screen."""
        return ((4, 1.0), (2, 1.0), (0, 1.0))

    def choice(self, pixels: float, ratio: float, scene) -> tuple:
        """What to draw this frame at. A new scene or size starts at the best
        rung anything has fitted at on a screen this size: starting at the
        top cost a second of slow frames at every scene change on a big
        display.
        """
        size = (int(pixels / 100_000.0), round(ratio, 3))
        key = (id(scene),) + size
        if key != self._key:
            self._key = key
            self._cost = 0.0
            self._settle = 0
            self._warm = self.WARMUP
            self._window = []
            self._count = 0
            self._seen = {}
            self._retry_at = {}
            self._backoff = {}
            rungs = self.rungs(ratio)
            start = self._fits.get(size, rungs[0])
            self._rung = start if start in rungs else rungs[0]
        return self._rung

    def record(self, taken_ms: float, ratio: float) -> None:
        """One finished frame, and a rung moved when the median asks."""
        self._count += 1
        if self._warm > 0:
            self._warm -= 1
            return
        if self._settle > 0:
            self._settle -= 1
            return
        self._window.append(taken_ms)
        if len(self._window) < self.WINDOW:
            return
        self._cost = sorted(self._window)[len(self._window) // 2]
        self._window = []
        self._seen[self._rung] = self._cost
        rungs = self.rungs(ratio)
        if self._rung not in rungs:
            return
        at = rungs.index(self._rung)
        size = self._key[1:]
        if self._cost > self.BUDGET_MS:
            if at < len(rungs) - 1:
                # Not tried again for a while, twice as long each time it still
                # does not fit, so a frame between two rungs cannot flick
                # between soft and sharp.
                wait = self._backoff.get(self._rung, self.RETRY)
                self._retry_at[self._rung] = self._count + wait
                self._backoff[self._rung] = wait * 2
                if self._fits.get(size) == self._rung:
                    del self._fits[size]
                self._move(rungs[at + 1])
            return
        best = self._fits.get(size)
        if best is None or best not in rungs or rungs.index(best) > at:
            self._fits[size] = self._rung
        if at == 0:
            return
        # Up, when the frames say the rung above would fit: what drove it down
        # may have been something else on the machine, and one busy moment
        # should not keep the picture soft.
        up = rungs[at - 1]
        if self._count < self._retry_at.get(up, 0):
            return
        dearer = (self.DEARER_SAMPLES if up[1] == self._rung[1]
                  else self.DEARER_PIXELS)
        if self._cost * dearer < self.BUDGET_MS * 0.9:
            self._move(up)

    def _move(self, to: tuple) -> None:
        self._rung = to
        self._settle = self.SETTLE
        self._window = []

    def interval_ms(self, frame_ms: int) -> int:
        """How often to ask for a frame, given what one costs: asking faster
        than frames can be drawn fills the event queue.
        """
        if self._cost <= frame_ms * 0.85:
            return frame_ms
        return max(frame_ms, min(33, int(self._cost * 1.1) + 1))


class PostProcess:
    """Cheap screen-space polish after a scene has drawn itself. No shaders
    here, so bloom is done at a fraction of the resolution and scaled up
    (which is a blur), and fixed overlays are tiles. The pass is skipped
    when a scene asks for nothing.
    """

    #: Bloom is worked out at this fraction of the frame, so its cost barely
    #: changes between a strip and full screen.
    BLOOM_DIVISOR = 8
    #: Never build a bloom buffer smaller than this.
    BLOOM_MIN = 32

    #: Effects in the order they are given up when time runs short. Bloom and
    #: the vignette carry most of the look, so they go last; aberration is the
    #: only one that adds colour, so it outlasts grain and scanlines.
    ORDER = ("grain", "scanlines", "aberration", "bloom", "vignette")
    #: The pass may take this long; the rest of a sixtieth of a second needs
    #: the other ten milliseconds.
    BUDGET_MS = 6.5

    def __init__(self) -> None:
        self._lines: dict = {}
        self._grain: dict = {}
        self._cost = 0.0
        self._allow = len(self.ORDER)
        self._area = 0.0
        #: Frames left alone after a change, so a decision shows its effect
        #: before the next.
        self._settle = 0

    def cost_ms(self) -> float:
        """What the pass has been taking, for whoever is pacing frames."""
        return max(0.0, self._cost)

    def _permitted(self, recipe: dict) -> dict:
        """The recipe minus whatever there is no time for, measured: the same
        frame costs very different amounts on different machines.
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
            # Seeded at the budget, not zero: zero made the next frame look
            # cheap and the effects oscillated between four and five.
            self._cost = self.BUDGET_MS
            self._settle = 30
        elif (self._cost < self.BUDGET_MS * 0.45
              and self._allow < len(self.ORDER)):
            self._allow += 1
            self._cost = self.BUDGET_MS
            self._settle = 120

    def apply(self, painter, rect, frame, recipe: dict,
              smooth: bool = False) -> None:
        """Draw ``frame`` into ``painter`` with ``recipe`` applied. Every pass
        runs on the buffer at its own size and the result is stretched once
        at the end; stretching first charged every pass for the whole screen
        (six milliseconds a frame at 1080p on Retina).
        """
        import time as _time

        started = _time.perf_counter()
        area = rect.width() * rect.height()
        if area > self._area * 1.3 or area < self._area * 0.7:
            # Start again with all of them, and measure. The passes run on the
            # buffer, not the frame, so the frame's area is no guide: guessing
            # from it gave a window all five effects and full screen three. All
            # five cost 4.84 ms on that buffer against a 6.5 ms budget.
            self._area = area
            self._allow = len(self.ORDER)
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
                # In halo pixels, so the offset lands in the same place
                # whatever the buffer was scaled to.
                apart = 0.0
                if shift > 0.05:
                    apart = (shift * halo.width()
                             / max(1.0, rect.width()))
                self._bloom(inner, box,
                            self._glow(halo, max(bloom, 0.0), apart))
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

        blit_scene(painter, rect, frame, smooth)
        self._record((_time.perf_counter() - started) * 1000.0)

    # -- the expensive one, kept cheap -------------------------------------
    def _halo(self, rect, frame):
        """A small, blurred copy of the frame: the blur is the downscale, and
        later passes read this, so the cost barely changes with the frame's
        size.
        """
        small = QSize(max(self.BLOOM_MIN, int(rect.width() / self.BLOOM_DIVISOR)),
                      max(self.BLOOM_MIN, int(rect.height() / self.BLOOM_DIVISOR)))
        shrunk = frame.scaled(small, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        # Plain pixels from here on. scaled() keeps the device pixel ratio, so
        # on a 2x display the halo claimed half its size and the polish landed
        # in the top-left quarter of a windowed scene.
        shrunk.setDevicePixelRatio(1.0)
        return shrunk

    #: How much of the halo each offset copy adds, for the fringing.
    FRINGE = 0.16

    def _glow(self, halo, amount: float, shift: float):
        """The halo plus its two offset copies, composed at the halo's own size
        and put up once: one full-size blit instead of three (at 1512x982
        the blit alone is 1.92 ms). Added with Plus either way round, so
        composing small only changes where the result clips at white.
        """
        wide = QPixmap(halo.size())
        wide.fill(QColor(0, 0, 0, 0))
        inner = QPainter(wide)
        try:
            inner.setOpacity(min(0.85, amount))
            inner.drawPixmap(0, 0, halo)
            if shift > 0.0:
                inner.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Plus)
                inner.setOpacity(self.FRINGE)
                inner.setRenderHint(
                    QPainter.RenderHint.SmoothPixmapTransform, True)
                source = QRectF(halo.rect())
                inner.drawPixmap(source.translated(shift, 0.0), halo, source)
                inner.drawPixmap(source.translated(-shift, 0.0), halo, source)
        finally:
            inner.end()
        return wide

    def _bloom(self, painter, rect, glow) -> None:
        painter.save()
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Plus)
        # Scaled during the blit: a full-size blurred copy cost thirty
        # milliseconds a frame at full screen.
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(rect), glow, QRectF(glow.rect()))
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

    def apply_on_gpu(self, painter, rect, halo, recipe: dict) -> None:
        """The recipe over a frame already on the card: every effect the scene
        asks for, since on the card the whole polish costs a fraction of a
        millisecond. ``halo`` is the small copy the bloom is made from (see
        Spectrum._paint_on_gpu).
        """
        bloom = float(recipe.get("bloom", 0.0))
        shift = float(recipe.get("aberration", 0.0))
        if bloom > 0.01 or shift > 0.05:
            apart = 0.0
            if shift > 0.05:
                apart = shift * halo.width() / max(1.0, rect.width())
            self._bloom(painter, rect, self._glow(halo, max(bloom, 0.0), apart))
        lines = float(recipe.get("scanlines", 0.0))
        if lines > 0.01:
            self._scanlines(painter, rect, lines)
        grain = float(recipe.get("grain", 0.0))
        if grain > 0.01:
            self._noise(painter, rect, grain)
        fade = float(recipe.get("vignette", 0.0))
        if fade > 0.01:
            self._vignette_over(painter, rect, fade)

    def _vignette_over(self, painter, rect, amount: float) -> None:
        """One gradient over the frame. Caching it small saved nothing (0.91 ms
        against 0.86): the cost is covering the frame.
        """
        shade = QRadialGradient(rect.center(),
                                max(rect.width(), rect.height()) * 0.72)
        shade.setColorAt(0.0, QColor(0, 0, 0, 0))
        shade.setColorAt(0.65, QColor(0, 0, 0, int(30 * amount)))
        shade.setColorAt(1.0, QColor(0, 0, 0, int(230 * amount)))
        painter.fillRect(rect, shade)

class Waveform(QWidget):
    """The shape of the whole track, above the seek bar, so seeking is aiming:
    it shows where the drop and the break are. Mirrored bars, with the
    played part in the highlight colour; click or drag to seek. Drawn from
    the scenes' analysis, so it is empty until that lands.
    """

    #: Emitted with a position in milliseconds when somebody clicks it.
    seeked = Signal(int)
    #: Whether there is a shape to draw; the pane shows the plain seek bar when
    #: there is not.
    shapeChanged = Signal(bool)

    #: How tall the bar is, and how wide a column is with its gap. Three
    #: pixels: narrower and the gaps close up, wider and a long track is drawn
    #: from too few readings.
    TALL = 44
    STEP = 3.0
    BAR = 2.0
    #: The least a column is drawn at, so silence is still a line.
    FLOOR = 1.5

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._shape: List[float] = []
        self._span = 0
        self._at = 0
        self.setFixedHeight(self.TALL)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        policy = self.sizePolicy()
        policy.setHorizontalPolicy(QSizePolicy.Policy.Expanding)
        self.setSizePolicy(policy)
        self.setToolTip("Click to jump to a point in the track")
        self.hide()

    # -- what it is showing ------------------------------------------------
    def set_shape(self, shape) -> None:
        """The outline of the track, or nothing to clear it."""
        self._shape = list(shape or ())
        self.shapeChanged.emit(bool(self._shape))
        self.update()

    def set_span(self, milliseconds: int) -> None:
        self._span = max(0, int(milliseconds))
        self.update()

    def set_position(self, milliseconds: int) -> None:
        was = self._at
        self._at = max(0, int(milliseconds))
        # Only when it would move a column; otherwise it redraws sixty times a
        # second for nothing.
        if self._span > 0 and self.width() > 0:
            step = max(1, self._span * int(self.STEP) // max(1, self.width()))
            if abs(self._at - was) < step:
                return
        self.update()

    def clear(self) -> None:
        """Forget the shape, and only the shape. Length and position belong to
        the player, which reports them once; clearing them too left a
        waveform that could not be clicked after the visualiser was switched
        on. A new track resets them itself; see forget_track.
        """
        self.set_shape(())

    def forget_track(self) -> None:
        """Everything, for a track that is going away."""
        self.set_shape(())
        self._span = self._at = 0
        self.update()

    # -- seeking -----------------------------------------------------------
    def _seek_to(self, x: float) -> None:
        if self._span <= 0 or self.width() <= 0:
            return
        share = min(1.0, max(0.0, x / self.width()))
        self.seeked.emit(int(share * self._span))

    def mousePressEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if event.button() == Qt.MouseButton.LeftButton:
            self._seek_to(event.position().x())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if event.buttons() & Qt.MouseButton.LeftButton:
            self._seek_to(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    # -- drawing -----------------------------------------------------------
    def paintEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if not self._shape:
            return
        painter = QPainter(self)
        try:
            self._paint(painter)
        finally:
            painter.end()

    def _paint(self, painter) -> None:
        width, tall = self.width(), self.height()
        middle = tall / 2.0
        played = (self._at / self._span * width) if self._span > 0 else 0.0
        tint = self.palette().highlight().color()
        rest = self.palette().windowText().color()
        rest.setAlphaF(0.30)
        painter.setPen(Qt.PenStyle.NoPen)
        columns = int(width / self.STEP) + 1
        for column in range(columns):
            x = column * self.STEP
            # The nearest reading, not an average: the outline is a peak per
            # column, and averaging peaks flattens the waveform.
            index = min(len(self._shape) - 1,
                        int(column * len(self._shape) / max(1, columns)))
            high = max(self.FLOOR, self._shape[index] * (middle - 2.0))
            painter.setBrush(tint if x + self.BAR <= played else rest)
            painter.drawRect(QRectF(x, middle - high, self.BAR, high * 2.0))
        # The column the playhead is in is half played, so the line moves
        # smoothly.
        if 0.0 < played < width:
            painter.setBrush(tint)
            painter.drawRect(QRectF(played - 1.0, 0.0, 1.0, tall))


#: Kept under its old name, where every pane reaches for it; the class is
#: shared with the triage window's wrapping rows.
FlowHolder = _FlowHolder
