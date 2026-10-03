"""The oscilloscope's phosphor, kept on the graphics card (see
visualizers.Oscilloscope).

Its tube is a screen that outlives the frame: each trace dims what is there
and strikes one more. On the CPU at a Retina full screen, striking the beam
cost 16 ms and handing the image to the card more, and the scope ran at 33
frames a second.

Here the screen is a framebuffer on the card and the beam is the same
painter calls through Qt's OpenGL engine, so the tube looks as it always
did; only the trace goes to the card each frame, and nothing comes back.

Composed as the CPU composes it: the screen is kept in plain pixels and
dimmed there, and only the new trace is struck with four samples a pixel,
into a scratch buffer, and laid over it. Keeping the whole screen
multisampled cost three milliseconds a trace at full screen, and resolving
it two more.
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, QSize
from PySide6.QtGui import QOpenGLContext, QPainter
from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat,
                              QOpenGLPaintDevice, QOpenGLTextureBlitter)

GL_FRAMEBUFFER = 0x8D40
GL_READ_FRAMEBUFFER = 0x8CA8
GL_DRAW_FRAMEBUFFER = 0x8CA9
GL_FRAMEBUFFER_BINDING = 0x8CA6
GL_COLOR_BUFFER_BIT = 0x4000
GL_STENCIL_BUFFER_BIT = 0x0400
GL_DEPTH_BUFFER_BIT = 0x0100
GL_NEAREST = 0x2600
GL_BLEND = 0x0BE2
GL_ZERO = 0
GL_ONE = 1
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_CONSTANT_ALPHA = 0x8003

#: How much of the pane the beam can reach, as a share of its shorter
#: side: a sweep's trace swings to 0.44 of it from the middle and a figure
#: to 0.52, and a beam has width and rounds its corners. Only that square
#: is kept on the card - on a wide screen, three quarters of the pixels.
REACH = 1.2


def _bound(gl) -> int:
    found = gl.glGetIntegerv(GL_FRAMEBUFFER_BINDING)
    if isinstance(found, (list, tuple)):
        found = found[0]
    return int(found)


def reach(width: int, height: int):
    """The size of the screen kept for a pane ``width`` by ``height``
    real pixels: its shorter side whole, and its longer side as far as
    the beam can reach. Centred in the pane, so the middle of the one is
    the middle of the other and the beam lands where it would."""
    side = min(width, height)
    most = int(round(side * REACH))
    return min(width, most), min(height, most)


class Tube:
    """One scope's screen, on whichever card it is being drawn on."""

    #: Antialiasing, the way everything else on the card gets it: the
    #: beam is struck into a multisampled buffer and resolved.
    SAMPLES = 4

    def __init__(self) -> None:
        self._key = None
        #: What the beam has drawn and not yet lost, in plain pixels.
        self._screen = None
        #: Where a new trace is struck, what that is resolved into, and
        #: the painter's view of the first.
        self._multi = None
        self._fresh = None
        self._device = None
        self._blitter = None

    @property
    def size(self):
        """The screen's size in real pixels, or None before the first
        frame."""
        return None if self._key is None else QSize(self._key[0],
                                                    self._key[1])

    def draw(self, scope, painter, rect: QRectF, trace, drawing: bool,
             flash: float) -> None:
        """Dim the screen, strike ``trace`` into it if it is new, and lay
        it over what ``painter`` has drawn so far in ``rect``."""
        device = painter.device()
        box = painter.combinedTransform().mapRect(rect)
        width, height = reach(int(round(box.width())),
                              int(round(box.height())))
        if width < 2 or height < 2:
            return
        # How many real pixels one unit of the scene is worth, as the
        # tube on the CPU measures it, so the beam is as thick here.
        dpr = abs(painter.combinedTransform().m11()) or 1.0
        where = QRectF(box.center().x() - width / 2.0,
                       box.center().y() - height / 2.0, width, height)
        # The target's size in its own pixels, which is what the viewport
        # is. A GL paint device reports it in pixels already; a widget
        # painted on directly reports points.
        if isinstance(device, QOpenGLPaintDevice):
            full = device.size()
        else:
            ratio = device.devicePixelRatioF() or 1.0
            full = QSize(int(round(device.width() * ratio)),
                         int(round(device.height() * ratio)))
        painter.beginNativePainting()
        context = QOpenGLContext.currentContext()
        gl = context.functions()
        target = _bound(gl)
        try:
            self._fit(context, gl, width, height)
            keep = scope._fade_now(trace)
            if keep is not None:
                self._strike(scope, context, gl, keep, trace, drawing,
                             flash, dpr)
            gl.glBindFramebuffer(GL_FRAMEBUFFER, target)
            gl.glViewport(0, 0, full.width(), full.height())
            self._lay(gl, where, QRect(0, 0, full.width(), full.height()),
                      painter.opacity())
        finally:
            # Back to the painter's own buffer, which nothing but this
            # knows to put back: Qt's engine rebinds its state when native
            # painting ends, but not the framebuffer it was drawing into.
            gl.glBindFramebuffer(GL_FRAMEBUFFER, target)
            painter.endNativePainting()

    # -- the buffers ---------------------------------------------------------
    def _fit(self, context, gl, width: int, height: int) -> None:
        """The screen at this size on this card, keeping what was on it.

        Scaled rather than cleared, as the CPU's is: a window being
        dragged is a stream of sizes. A new card - full screen can bring
        one - starts dark, because what was on the old one went with it.
        """
        key = (width, height, id(context))
        if key == self._key:
            return
        same_card = self._key is not None and self._key[2] == id(context)
        old = self._screen if same_card else None
        if not same_card or self._blitter is None:
            self._blitter = QOpenGLTextureBlitter()
            self._blitter.create()
        shape = QOpenGLFramebufferObjectFormat()
        shape.setSamples(self.SAMPLES)
        # A stencil, because the engine strokes a translucent pen through
        # one so that where a stroke crosses itself is not lit twice.
        shape.setAttachment(
            QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
        multi = QOpenGLFramebufferObject(QSize(width, height), shape)
        plain = QOpenGLFramebufferObjectFormat()
        plain.setSamples(0)
        screen = QOpenGLFramebufferObject(QSize(width, height), plain)
        fresh = QOpenGLFramebufferObject(QSize(width, height), plain)
        if not all(made.isValid() for made in (multi, screen, fresh)):
            raise RuntimeError("the card would not make the scope's screen")
        gl.glBindFramebuffer(GL_FRAMEBUFFER, screen.handle())
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(GL_COLOR_BUFFER_BIT)
        if old is not None:
            gl.glDisable(GL_BLEND)
            whole = QRect(0, 0, width, height)
            self._blitter.bind()
            self._blitter.blit(
                old.texture(),
                QOpenGLTextureBlitter.targetTransform(QRectF(whole), whole),
                QOpenGLTextureBlitter.Origin.OriginBottomLeft)
            self._blitter.release()
        self._screen, self._multi, self._fresh = screen, multi, fresh
        self._device = QOpenGLPaintDevice(QSize(width, height))
        self._key = key

    # -- the beam ------------------------------------------------------------
    def _strike(self, scope, context, gl, keep: float, trace, drawing: bool,
                flash: float, dpr: float) -> None:
        """The CPU's fade, and the CPU's beam laid over it."""
        width, height = self._key[0], self._key[1]
        whole = QRect(0, 0, width, height)
        # The new trace on its own, smoothed.
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._multi.handle())
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glClear(GL_COLOR_BUFFER_BIT | GL_STENCIL_BUFFER_BIT
                   | GL_DEPTH_BUFFER_BIT)
        beam = QPainter(self._device)
        try:
            beam.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            scope._strike(beam, self._device, trace, drawing, flash, dpr)
        finally:
            beam.end()
        extra = context.extraFunctions()
        extra.glBindFramebuffer(GL_READ_FRAMEBUFFER, self._multi.handle())
        extra.glBindFramebuffer(GL_DRAW_FRAMEBUFFER, self._fresh.handle())
        extra.glBlitFramebuffer(0, 0, width, height, 0, 0, width, height,
                                GL_COLOR_BUFFER_BIT, GL_NEAREST)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, self._screen.handle())
        gl.glViewport(0, 0, width, height)
        gl.glEnable(GL_BLEND)
        self._blitter.bind()
        place = QOpenGLTextureBlitter.targetTransform(QRectF(whole), whole)
        # Dimmed: every channel of what is there times ``keep``, which is
        # the CPU's DestinationIn fill. The quad's own colour is ignored.
        gl.glBlendColor(0.0, 0.0, 0.0, keep)
        gl.glBlendFunc(GL_ZERO, GL_CONSTANT_ALPHA)
        self._blitter.blit(self._fresh.texture(), place,
                           QOpenGLTextureBlitter.Origin.OriginBottomLeft)
        # And the new trace over it, as light already multiplied by its
        # own alpha - which is how the engine drew it.
        gl.glBlendFunc(GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        self._blitter.blit(self._fresh.texture(), place,
                           QOpenGLTextureBlitter.Origin.OriginBottomLeft)
        self._blitter.release()
        gl.glDisable(GL_BLEND)

    def _lay(self, gl, box: QRectF, viewport: QRect, opacity: float) -> None:
        """The screen over the scene, as a painter lays an image: over
        what is there by its own alpha, and faded with the scene.

        The screen holds light already multiplied by its alpha, so it is
        added over one minus that alpha. The blitter fades only the alpha,
        so the colour is faded by the blend's constant instead - which
        makes the whole thing the painter's own ``opacity`` over
        premultiplied light.
        """
        gl.glEnable(GL_BLEND)
        gl.glBlendColor(0.0, 0.0, 0.0, opacity)
        gl.glBlendFuncSeparate(GL_CONSTANT_ALPHA, GL_ONE_MINUS_SRC_ALPHA,
                               GL_ONE, GL_ONE_MINUS_SRC_ALPHA)
        self._blitter.bind()
        self._blitter.setOpacity(opacity)
        self._blitter.blit(
            self._screen.texture(),
            QOpenGLTextureBlitter.targetTransform(box, viewport),
            QOpenGLTextureBlitter.Origin.OriginBottomLeft)
        self._blitter.setOpacity(1.0)
        self._blitter.release()
        gl.glDisable(GL_BLEND)
