"""The polish's bloom, worked out on the graphics card (see
attachment_widgets.PostProcess).

Bloom is the frame blurred and added back over itself. On the card it was
taken from a copy of the frame an eighth of its size, each pixel of which
was the average of a block of eight by eight, and that copy was stretched
back over the frame in one go. A thin line lit the blocks by how much of it
crossed each, so its glow was a chain of tiles, and the tiles changed as the
line moved; and where a line and its glow added up past white, the white
took the tiles' steps along its sides. At full screen the rave's wireframe
had stepped, crawling outlines.

Here the frame is halved, which is an exact average, blurred at that size
by a true Gaussian in two passes, and laid back over the frame through the
card's own smoothing. The glow is as wide as the tiles made it and the same
at every place and every angle, so a line keeps even sides as it turns:
along a thin line on a slant, the middle of the old glow wandered up to 2.2
real pixels off the line, and this one stays within a fifth of a pixel. And
nothing comes back off the card, where the small copy did every frame.
"""

from __future__ import annotations

import math
from array import array

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QVector2D
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLShader,
                              QOpenGLShaderProgram)

GL_FLOAT = 0x1406
GL_TRIANGLE_STRIP = 0x0005
GL_BLEND = 0x0BE2
GL_SCISSOR_TEST = 0x0C11
GL_ONE = 1
GL_FUNC_ADD = 0x8006
GL_COLOR_BUFFER_BIT = 0x4000
GL_FRAMEBUFFER = 0x8D40
GL_FRAMEBUFFER_BINDING = 0x8CA6
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803

#: The most pairs of texels the blur reads either side of a pixel: the
#: card's smoothing reads two neighbours for the price of one. The widest
#: glow asked for, four real pixels at full screen, needs three.
MOST_PAIRS = 8

HEADER = """
#version 120
"""

QUAD_VERTEX = """
attribute vec2 aCorner;
varying vec2 vUv;
void main() {
    vUv = aCorner * 0.5 + 0.5;
    gl_Position = vec4(aCorner, 0.0, 1.0);
}
"""

#: One direction of the Gaussian. Each pair is two texels read as one,
#: between them, by the card's linear smoothing.
BLUR_FRAGMENT = """
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uStep;
uniform float uCentre;
uniform float uOffset[%(most)d];
uniform float uWeight[%(most)d];
uniform int uPairs;
void main() {
    vec3 c = texture2D(uTex, vUv).rgb * uCentre;
    for (int i = 0; i < %(most)d; i++) {
        if (i >= uPairs) {
            break;
        }
        vec2 o = uStep * uOffset[i];
        c += (texture2D(uTex, vUv + o).rgb
              + texture2D(uTex, vUv - o).rgb) * uWeight[i];
    }
    gl_FragColor = vec4(c, 1.0);
}
""" % {"most": MOST_PAIRS}

#: The glow, added over the frame, with its two side copies: the fringing.
#: Alpha is left as it was.
LAY_FRAGMENT = """
varying vec2 vUv;
uniform sampler2D uTex;
uniform float uAmount;
uniform float uFringe;
uniform vec2 uApart;
void main() {
    vec3 c = texture2D(uTex, vUv).rgb * uAmount;
    c += (texture2D(uTex, vUv + uApart).rgb
          + texture2D(uTex, vUv - uApart).rgb) * uFringe;
    gl_FragColor = vec4(c, 0.0);
}
"""


def taps(sigma: float) -> tuple:
    """The Gaussian of standard deviation ``sigma`` texels, as the centre's
    weight and up to MOST_PAIRS (offset, weight) pairs for either side,
    each pair two neighbouring texels read between them. Its weights sum to
    one. No pairs for a blur too narrow to reach the next texel."""
    if sigma < 0.2:
        return 1.0, ()
    reach = min(2 * MOST_PAIRS, max(2, int(math.ceil(sigma * 3.0))))
    if reach % 2:
        reach += 1
    weights = [math.exp(-(k * k) / (2.0 * sigma * sigma))
               for k in range(reach + 1)]
    total = weights[0] + 2.0 * sum(weights[1:])
    weights = [w / total for w in weights]
    pairs = []
    for k in range(1, reach, 2):
        both = weights[k] + weights[k + 1]
        pairs.append(((k * weights[k] + (k + 1) * weights[k + 1]) / both,
                      both))
    return weights[0], tuple(pairs)


def _bound(gl) -> int:
    found = gl.glGetIntegerv(GL_FRAMEBUFFER_BINDING)
    if isinstance(found, (list, tuple)):
        found = found[0]
    return int(found)


def _program(fragment: str) -> QOpenGLShaderProgram:
    program = QOpenGLShaderProgram()
    if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex,
                                           HEADER + QUAD_VERTEX):
        raise RuntimeError("vertex shader: " + program.log())
    if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, HEADER + fragment):
        raise RuntimeError("fragment shader: " + program.log())
    if not program.link():
        raise RuntimeError("link: " + program.log())
    return program


class Glow:
    """The bloom on one card's context, built there and dropped with it."""

    def __init__(self, gl) -> None:
        self.gl = gl
        self._blur = _program(BLUR_FRAGMENT)
        self._lay = _program(LAY_FRAGMENT)
        self._quad = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        if not self._quad.create():
            raise RuntimeError("the card would not make a vertex buffer")
        self._quad.bind()
        corners = array("f", [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0])
        self._quad.allocate(corners.tobytes(), len(corners) * 4)
        self._quad.release()
        self._size = None
        self._ping = self._pong = None

    def apply(self, flat, area: QRect, spread: float, amount: float,
              fringe: float = 0.0, apart: float = 0.0) -> None:
        """Bloom ``area`` of ``flat``, counted in its own rows from the
        bottom: the frame there blurred by ``spread`` real pixels (a
        standard deviation) and added back at ``amount``, with a copy
        ``apart`` real pixels to either side at ``fringe``.

        Leaves the framebuffer that was bound bound, and blending off.
        """
        # Only what is in the frame: a scene sliding up into view is partly
        # below it, and the card reads anything at all from outside it.
        area = area.intersected(QRect(0, 0, flat.width(), flat.height()))
        if area.width() < 2 or area.height() < 2:
            return
        gl = self.gl
        target = _bound(gl)
        wide, high = area.width() // 2, area.height() // 2
        even = QRect(area.x(), area.y(), wide * 2, high * 2)
        self._fit(wide, high)
        ping, pong = self._ping, self._pong
        whole = QRect(0, 0, wide, high)
        # A linear blit at half the size averages two by two, exactly.
        QOpenGLFramebufferObject.blitFramebuffer(
            ping, whole, flat, even, GL_COLOR_BUFFER_BIT, GL_LINEAR)
        gl.glDisable(GL_BLEND)
        gl.glDisable(GL_SCISSOR_TEST)
        # The halving and the stretch back up blur by a variance of one real
        # pixel between them, so the Gaussian makes up the rest, in half
        # pixels.
        centre, pairs = taps(math.sqrt(max(0.0, spread * spread - 1.0)) / 2.0)
        if pairs:
            self._pass(ping, pong, QVector2D(1.0 / wide, 0.0), centre, pairs)
            self._pass(pong, ping, QVector2D(0.0, 1.0 / high), centre, pairs)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, flat.handle())
        gl.glViewport(even.x(), even.y(), even.width(), even.height())
        gl.glEnable(GL_BLEND)
        gl.glBlendEquation(GL_FUNC_ADD)
        gl.glBlendFunc(GL_ONE, GL_ONE)
        program = self._lay
        program.bind()
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, ping.texture())
        program.setUniformValue1i(program.uniformLocation("uTex"), 0)
        program.setUniformValue1f(program.uniformLocation("uAmount"),
                                  float(amount))
        program.setUniformValue1f(program.uniformLocation("uFringe"),
                                  float(fringe))
        program.setUniformValue(program.uniformLocation("uApart"),
                                QVector2D(apart / even.width(), 0.0))
        self._draw(program)
        program.release()
        gl.glBindTexture(GL_TEXTURE_2D, 0)
        gl.glDisable(GL_BLEND)
        gl.glBindFramebuffer(GL_FRAMEBUFFER, target)

    def _fit(self, wide: int, high: int) -> None:
        if self._size == (wide, high):
            return
        gl = self.gl
        made = []
        for _ in range(2):
            shape = QOpenGLFramebufferObjectFormat()
            shape.setSamples(0)
            buffer = QOpenGLFramebufferObject(QSize(wide, high), shape)
            if not buffer.isValid():
                raise RuntimeError("the card would not make the glow's "
                                   "buffers")
            # Smoothed when read, which the blur's pairs and the stretch
            # back over the frame both rely on.
            gl.glBindTexture(GL_TEXTURE_2D, buffer.texture())
            for which, value in ((GL_TEXTURE_MIN_FILTER, GL_LINEAR),
                                 (GL_TEXTURE_MAG_FILTER, GL_LINEAR),
                                 (GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE),
                                 (GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE)):
                gl.glTexParameteri(GL_TEXTURE_2D, which, value)
            made.append(buffer)
        gl.glBindTexture(GL_TEXTURE_2D, 0)
        self._ping, self._pong = made
        self._size = (wide, high)

    def _pass(self, source, target, step: QVector2D, centre: float,
              pairs: tuple) -> None:
        gl = self.gl
        gl.glBindFramebuffer(GL_FRAMEBUFFER, target.handle())
        gl.glViewport(0, 0, target.width(), target.height())
        program = self._blur
        program.bind()
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, source.texture())
        program.setUniformValue1i(program.uniformLocation("uTex"), 0)
        program.setUniformValue(program.uniformLocation("uStep"), step)
        program.setUniformValue1f(program.uniformLocation("uCentre"),
                                  float(centre))
        offsets = [offset for offset, _weight in pairs]
        weights = [weight for _offset, weight in pairs]
        padding = [0.0] * (MOST_PAIRS - len(pairs))
        program.setUniformValueArray(program.uniformLocation("uOffset"),
                                     offsets + padding, MOST_PAIRS, 1)
        program.setUniformValueArray(program.uniformLocation("uWeight"),
                                     weights + padding, MOST_PAIRS, 1)
        program.setUniformValue1i(program.uniformLocation("uPairs"),
                                  len(pairs))
        self._draw(program)
        program.release()

    def _draw(self, program) -> None:
        self._quad.bind()
        where = program.attributeLocation("aCorner")
        program.enableAttributeArray(where)
        program.setAttributeBuffer(where, GL_FLOAT, 0, 2, 0)
        self.gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        program.disableAttributeArray(where)
        self._quad.release()
