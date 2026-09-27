"""Music rider, drawn in three dimensions on the graphics card.

"Xxxxx xx xxxxxx xx xxxxxxxxxxxxx xxx xxxxx xx xx xxxxxxx xxxxxxxx xx
xxxxxxx obstacles xxxx xxx xxxx xxx xxxx." Xxx rider was drawn with a
QPainter, a road of lines projected by hand onto a flat picture, and
that is a ceiling: no depth, no light, nothing brighter than white, and
a bloom made from a small copy of the frame read back off the card.

This draws the same game as a lit world. The track is a ribbon of glass
with neon rails; the blocks are solid, and pulse on the beat; the craft
is a ship with an engine; a city of equaliser towers lines the road; the
air is full of particles; and every beat is a gate over the track that
lights as you pass through it, on the beat. It is drawn in floating
point, so a neon rail can be far brighter than white and bloom, while
everything that is not lit stays deep and saturated rather than washed
over by a glow.

Nothing here decides anything. Every position comes from the scene's
own road function, sampled once a frame into the shader that places
every vertex - so a block drawn here is where the game says it is, on
the frame it says so. The QPainter drawing stays, as the picture where
there is no card to draw on and as what the game's tests look at.
"""

from __future__ import annotations

import math
import random
from array import array

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QMatrix4x4, QVector2D, QVector3D
from PySide6.QtOpenGL import (QOpenGLBuffer, QOpenGLFramebufferObject,
                              QOpenGLFramebufferObjectFormat, QOpenGLShader,
                              QOpenGLShaderProgram)

# -- the few GL enums used, by value ---------------------------------------
GL_FLOAT = 0x1406
GL_TRIANGLES = 0x0004
GL_TRIANGLE_STRIP = 0x0005
GL_LINES = 0x0001
GL_POINTS = 0x0000
GL_DEPTH_TEST = 0x0B71
GL_BLEND = 0x0BE2
GL_CULL_FACE = 0x0B44
GL_SCISSOR_TEST = 0x0C11
GL_STENCIL_TEST = 0x0B90
GL_ONE = 1
GL_ZERO = 0
GL_SRC_ALPHA = 0x0302
GL_ONE_MINUS_SRC_ALPHA = 0x0303
GL_COLOR_BUFFER_BIT = 0x4000
GL_DEPTH_BUFFER_BIT = 0x0100
GL_TEXTURE_2D = 0x0DE1
GL_TEXTURE0 = 0x84C0
GL_LINEAR = 0x2601
GL_CLAMP_TO_EDGE = 0x812F
GL_TEXTURE_MIN_FILTER = 0x2801
GL_TEXTURE_MAG_FILTER = 0x2800
GL_TEXTURE_WRAP_S = 0x2802
GL_TEXTURE_WRAP_T = 0x2803
GL_FRAMEBUFFER = 0x8D40
GL_FRAMEBUFFER_BINDING = 0x8CA6
GL_RGBA16F = 0x881A
GL_R11F_G11F_B10F = 0x8C3A
GL_RGBA8 = 0x8058
GL_LEQUAL = 0x0203
GL_LESS = 0x0201
GL_PROGRAM_POINT_SIZE = 0x8642
GL_POINT_SPRITE = 0x8861
GL_FUNC_ADD = 0x8006

# -- the road, as the shader sees it ----------------------------------------
#: How many times a frame the scene's road is read, and over what stretch.
#: From behind the camera to well past the last block, so the track runs
#: on into the fog rather than stopping where the game stops laying.
ROAD_SAMPLES = 96
ROAD_FROM = -6.0
ROAD_TO = 76.0
ROAD_STEP = (ROAD_TO - ROAD_FROM) / (ROAD_SAMPLES - 1)

#: The levels the towers are lit from.
BANDS = 24

HEADER = """
#version 120
"""

#: Every vertex of the world goes through here: across, up and along the
#: road, to a point in the world. One function, so nothing can disagree
#: about where the road is.
ROAD_GLSL = """
uniform vec4 uRoad[%(samples)d];
uniform float uRoadFrom;
uniform float uRoadStep;

vec4 roadAt(float z) {
    float f = clamp((z - uRoadFrom) / uRoadStep, 0.0, %(last)d.0 - 0.001);
    int i = int(floor(f));
    float t = f - float(i);
    return mix(uRoad[i], uRoad[i + 1], t);
}

// u across, h up, z along: to the world, where the road runs to -z.
vec3 onRoad(float u, float h, float z) {
    vec4 r = roadAt(z);
    float c = cos(r.z);
    float s = sin(r.z);
    return vec3(r.x + u * c - h * s, r.y + u * s + h * c, -z);
}
""" % {"samples": ROAD_SAMPLES, "last": ROAD_SAMPLES - 1}

#: Shared by everything that fades into the distance.
FOG_GLSL = """
uniform vec3 uFog;
uniform float uFogFrom;
uniform float uFogTo;
vec3 fogged(vec3 colour, float z) {
    float f = clamp((z - uFogFrom) / (uFogTo - uFogFrom), 0.0, 1.0);
    return mix(colour, uFog, f * f * (3.0 - 2.0 * f));
}
"""

HUES_GLSL = """
vec3 hsv(float h, float s, float v) {
    vec3 k = clamp(abs(mod(h * 6.0 + vec3(0.0, 4.0, 2.0), 6.0) - 3.0) - 1.0,
                   0.0, 1.0);
    return v * mix(vec3(1.0), k, s);
}
float hash(vec2 p) {
    return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453);
}
"""


class _Program:
    """A linked program, and the uniforms it has been asked about.

    Uniforms are set by location, found once: PySide resolves a name and
    a float to none of its overloads, and a location is cheaper anyway.
    """

    def __init__(self, program: QOpenGLShaderProgram) -> None:
        self.program = program
        self._where: dict = {}

    def _at(self, name: str) -> int:
        found = self._where.get(name)
        if found is None:
            found = self._where[name] = self.program.uniformLocation(name)
        return found

    def set(self, name: str, value) -> None:
        where = self._at(name)
        if where < 0:
            return
        if isinstance(value, bool) or isinstance(value, int):
            self.program.setUniformValue1i(where, int(value))
        elif isinstance(value, float):
            self.program.setUniformValue1f(where, value)
        else:
            self.program.setUniformValue(where, value)

    def array(self, name: str, values, count: int, size: int) -> None:
        where = self._at(name)
        if where >= 0:
            self.program.setUniformValueArray(where, values, count, size)

    def bind(self) -> None:
        self.program.bind()

    def release(self) -> None:
        self.program.release()

    def attributeLocation(self, name) -> int:      # noqa: N802 - Qt's name
        return self.program.attributeLocation(name)

    def enableAttributeArray(self, where: int) -> None:      # noqa: N802
        self.program.enableAttributeArray(where)

    def disableAttributeArray(self, where: int) -> None:      # noqa: N802
        self.program.disableAttributeArray(where)

    def setAttributeBuffer(self, *args) -> None:      # noqa: N802
        self.program.setAttributeBuffer(*args)


def _program(vertex: str, fragment: str) -> "_Program":
    program = QOpenGLShaderProgram()
    if not program.addShaderFromSourceCode(QOpenGLShader.ShaderTypeBit.Vertex,
                                           HEADER + vertex):
        raise RuntimeError("vertex shader: " + program.log())
    if not program.addShaderFromSourceCode(
            QOpenGLShader.ShaderTypeBit.Fragment, HEADER + fragment):
        raise RuntimeError("fragment shader: " + program.log())
    if not program.link():
        raise RuntimeError("link: " + program.log())
    return _Program(program)


class _Mesh:
    """A static buffer of floats, and how its attributes are laid out."""

    def __init__(self, floats, layout, mode=GL_TRIANGLES) -> None:
        self.buffer = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
        self.buffer.create()
        self.buffer.bind()
        data = array("f", floats)
        self.buffer.allocate(data.tobytes(), len(data) * 4)
        self.buffer.release()
        self.layout = layout            # [(name, size), ...]
        self.stride = sum(size for _name, size in layout)
        self.count = len(data) // self.stride
        self.mode = mode

    def replace(self, floats) -> None:
        data = array("f", floats)
        self.buffer.bind()
        self.buffer.allocate(data.tobytes(), len(data) * 4)
        self.buffer.release()
        self.count = len(data) // self.stride

    def draw(self, gl, program, first=0, count=None) -> None:
        if self.count == 0:
            return
        self.buffer.bind()
        used = []
        offset = 0
        for name, size in self.layout:
            where = program.attributeLocation(name)
            if where >= 0:
                program.enableAttributeArray(where)
                program.setAttributeBuffer(where, GL_FLOAT, offset * 4, size,
                                           self.stride * 4)
                used.append(where)
            offset += size
        gl.glDrawArrays(self.mode, first,
                        self.count - first if count is None else count)
        for where in used:
            program.disableAttributeArray(where)
        self.buffer.release()


# ==========================================================================
# Shaders
# ==========================================================================
QUAD_VERTEX = """
attribute vec2 aCorner;
varying vec2 vUv;
void main() {
    vUv = aCorner * 0.5 + 0.5;
    gl_Position = vec4(aCorner, 0.0, 1.0);
}
"""

SKY_FRAGMENT = HUES_GLSL + """
varying vec2 vUv;
uniform float uHue;
uniform float uLoud;
uniform float uKick;
uniform float uTime;
uniform float uHorizon;      // where the horizon is, 0 at the bottom
uniform float uRoll;
uniform float uAspect;
uniform vec2 uSun;           // where the road meets the sky, 0..1
uniform float uHurt;
void main() {
    vec2 p = vUv - 0.5;
    p.x *= uAspect;
    float c = cos(uRoll), s = sin(uRoll);
    vec2 q = vec2(c * p.x + s * p.y, -s * p.x + c * p.y);
    float up = q.y + 0.5 - uHorizon;
    // Deep space above, a band of light at the horizon in the passage's
    // own colour, and dark again below it where the city stands.
    vec3 deep = hsv(uHue + 0.55, 0.8, 0.012 + uLoud * 0.012);
    vec3 band = hsv(uHue, 0.95, 0.22 + uLoud * 0.22 + uKick * 0.25);
    vec3 colour = mix(band, deep, smoothstep(0.0, 0.16, up));
    colour = mix(colour, deep * 0.6, smoothstep(0.0, -0.12, up));
    // Stars, still - a sky that moves reads as the camera moving.
    vec2 cell = floor(vUv * vec2(uAspect, 1.0) * 180.0);
    float star = step(0.9965, hash(cell));
    float twinkle = 0.55 + 0.45 * sin(uTime * 3.0 + hash(cell + 7.0) * 40.0);
    colour += vec3(0.9, 0.95, 1.0) * star * twinkle * smoothstep(0.02, 0.2, up)
              * (1.2 + uKick);
    // The sun the road runs into: a disc on the horizon, lit harder on
    // the kick, with the bands a synthwave sun is cut into.
    vec2 d = (vUv - uSun) * vec2(uAspect, 1.0);
    float r = length(d);
    float disc = smoothstep(0.095 + uKick * 0.012, 0.088 + uKick * 0.012, r);
    // The upper half, cut into thinner and thinner bands towards the
    // horizon it sits on - the sun every synthwave sky is lit by.
    float low = d.y / 0.095;
    float cut = step(0.0, d.y)
                * max(step(0.42, low), step(0.18 + (0.42 - low) * 0.9,
                                            fract(low * 9.0)));
    vec3 sun = mix(hsv(uHue - 0.08, 0.95, 1.4), hsv(uHue + 0.06, 0.85, 2.4),
                   clamp(d.y * 8.0 + 0.5, 0.0, 1.0));
    float above = smoothstep(uHorizon - 0.002, uHorizon + 0.004, q.y + 0.5);
    colour = mix(colour, sun * (1.0 + uKick * 0.8),
                 disc * clamp(cut, 0.0, 1.0) * above);
    colour += hsv(uHue, 0.9, 1.0) * exp(-r * 7.0) * (0.18 + uKick * 0.45);
    colour = mix(colour, vec3(0.5, 0.02, 0.03) * length(colour) * 1.6,
                 uHurt * 0.6);
    gl_FragColor = vec4(colour, 1.0);
}
"""

ROAD_VERTEX = ROAD_GLSL + """
attribute vec2 aRoad;        // across, along
uniform mat4 uView;
varying float vU;
varying float vZ;
varying float vDepth;
void main() {
    vec3 p = onRoad(aRoad.x, 0.0, aRoad.y);
    vec4 eye = uView * vec4(p, 1.0);
    vU = aRoad.x;
    vZ = aRoad.y;
    vDepth = eye.w;
    gl_Position = eye;
}
"""

ROAD_FRAGMENT = HUES_GLSL + FOG_GLSL + """
varying float vU;
varying float vZ;
varying float vDepth;
uniform float uHalf;         // half the road's width
uniform float uLane;         // one lane's width
uniform float uTravel;       // how far the road has come, in road units
uniform float uPerBeat;
uniform float uRider;
uniform float uHue;
uniform float uLoud;
uniform float uBeat;         // 1 on the beat, falling away
uniform float uKick;
uniform float uWave;         // where the kick's wave of light has got to
uniform float uWaveLit;
uniform vec3 uFlashColour;   // the lane that just paid, lit in its colour
uniform float uFlashLane;
uniform float uFlash;
uniform float uHurt;
uniform float uShipU;
void main() {
    float a = abs(vU);
    float along = vZ + uTravel;
    vec3 hue = hsv(uHue, 0.92, 1.0);
    // Dark glass, faintly the colour of the passage.
    vec3 colour = hsv(uHue, 0.7, 0.05 + uLoud * 0.05);
    // A grid scrolling under you: a line every quarter of a beat across
    // the road, and every lane along it.
    float quarter = fract(along / (uPerBeat * 0.25));
    float fine = 1.0 - smoothstep(0.0, 0.035, min(quarter, 1.0 - quarter));
    colour += hue * fine * 0.10;
    // The beat itself: a bright line across the road every beat, which
    // reaches the craft exactly on it. Lit harder as it gets close, and
    // hardest as it passes under you.
    float beats = along / uPerBeat;
    float onBeat = 1.0 - smoothstep(0.0, 0.018, min(fract(beats),
                                                      1.0 - fract(beats)));
    float near = exp(-abs(vZ - uRider) * 0.25);
    colour += hue * onBeat * (0.35 + near * 1.6 + uBeat * near * 3.0);
    // Lane lines.
    float g = fract(vU / uLane + 0.5);
    float lane = 1.0 - smoothstep(0.015, 0.05, min(g, 1.0 - g) * uLane);
    lane *= step(a, uHalf - 0.1);
    colour += hue * lane * (0.22 + uBeat * 0.35);
    // The rails: neon, far brighter than white, which is what the bloom
    // is for. Pulsing with the kick.
    float rail = smoothstep(uHalf - 0.01, uHalf + 0.01, a)
                 * (1.0 - smoothstep(uHalf + 0.07, uHalf + 0.10, a));
    colour = mix(colour, hue * (2.4 + uKick * 4.0 + uLoud * 1.5), rail);
    // The kick's wave: a band of light running away down the road from
    // under the craft.
    float wave = exp(-pow((vZ - uWave) * 0.9, 2.0)) * uWaveLit;
    colour += hue * wave * 1.8 * (0.4 + rail * 2.0);
    // What was just collected lights its lane near the craft.
    float inLane = 1.0 - smoothstep(uLane * 0.3, uLane * 0.55,
                                    abs(vU - uFlashLane));
    colour += uFlashColour * inLane * uFlash * exp(-abs(vZ - uRider - 0.8) * 1.1)
              * 1.6;
    // The craft's own light on the road under it.
    float under = exp(-pow((vU - uShipU) * 2.2, 2.0) - pow((vZ - uRider) * 1.1, 2.0));
    colour += hue * under * (0.5 + uBeat * 0.8);
    colour = mix(colour, vec3(colour.r * 1.5 + 0.2, colour.g * 0.2,
                              colour.b * 0.2), uHurt * 0.8);
    gl_FragColor = vec4(fogged(colour, vDepth), 1.0);
}
"""

SOLID_VERTEX = ROAD_GLSL + """
attribute vec3 aPos;
attribute vec3 aNormal;
uniform mat4 uView;
uniform vec3 uPlace;        // across, up, along
uniform vec3 uScale;
uniform vec3 uAngles;       // yaw, pitch, roll
varying vec3 vLocal;
varying vec3 vLocalNormal;
varying vec3 vNormal;
varying float vDepth;
varying vec3 vWorld;
mat3 turned(vec3 a) {
    float cy = cos(a.x), sy = sin(a.x);
    float cp = cos(a.y), sp = sin(a.y);
    float cr = cos(a.z), sr = sin(a.z);
    mat3 yaw = mat3(cy, 0.0, -sy, 0.0, 1.0, 0.0, sy, 0.0, cy);
    mat3 pitch = mat3(1.0, 0.0, 0.0, 0.0, cp, sp, 0.0, -sp, cp);
    mat3 roll = mat3(cr, sr, 0.0, -sr, cr, 0.0, 0.0, 0.0, 1.0);
    return yaw * pitch * roll;
}
void main() {
    mat3 uTurn = turned(uAngles);
    vec3 local = uTurn * (aPos * uScale);
    vec3 p = onRoad(uPlace.x + local.x, uPlace.y + local.y,
                    uPlace.z - local.z);
    vec4 r = roadAt(uPlace.z);
    float c = cos(r.z), s = sin(r.z);
    vec3 n = uTurn * aNormal;
    vNormal = vec3(n.x * c - n.y * s, n.x * s + n.y * c, n.z);
    vLocal = aPos;
    vLocalNormal = aNormal;
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    vWorld = p;
    gl_Position = eye;
}
"""

SOLID_FRAGMENT = HUES_GLSL + FOG_GLSL + """
varying vec3 vLocal;
varying vec3 vLocalNormal;
varying vec3 vNormal;
varying float vDepth;
varying vec3 vWorld;
uniform vec3 uColour;
uniform float uEmit;         // how much of it is its own light
uniform float uEdge;         // how bright its edges are
uniform float uGrey;         // an obstacle, not a prize
uniform float uAlpha;
uniform vec3 uLight;
uniform float uCoin;         // struck metal rather than a lit block
void main() {
    vec3 n = normalize(vNormal);
    float lit = 0.35 + 0.65 * max(dot(n, normalize(uLight)), 0.0);
    // How near an edge of the shape this point is, for the neon trim.
    vec3 a = abs(vLocal) * 2.0;
    float e1 = max(a.x, a.y), e2 = max(a.y, a.z), e3 = max(a.x, a.z);
    float edge = smoothstep(0.86, 0.97, min(min(e1, e2), e3));
    vec3 body = uColour * (lit * (1.0 - uEmit) + uEmit);
    vec3 colour = body + uColour * edge * uEdge;
    // An obstacle is dark metal with a warning in its edges.
    colour = mix(colour, vec3(0.05, 0.05, 0.07) * lit
                 + vec3(1.0, 0.18, 0.12) * edge * uEdge, uGrey);
    // A rim, so a shape reads against a dark sky.
    float rim = pow(1.0 - abs(n.z), 3.0);
    colour += uColour * rim * 0.4 * (1.0 - uGrey);
    if (uCoin > 0.5) {
        // A coin: a raised rim round a face, the face lit across its
        // width so it catches the light as it turns, and a glint.
        float r = length(vLocal.xy) * 2.0;
        float face = step(0.9, abs(vLocalNormal.z));
        float ring = smoothstep(0.70, 0.78, r) * face;
        float sheen = 0.55 + 0.45 * sin((vLocal.x - vLocal.y) * 7.0 + n.x * 3.0);
        vec3 gold = uColour * (0.55 + 0.55 * lit) * mix(sheen, 1.25, ring);
        gold = mix(gold, uColour * 0.45, (1.0 - face) * 0.8);
        float glint = pow(max(dot(reflect(-normalize(uLight), n),
                                  vec3(0.0, 0.0, 1.0)), 0.0), 24.0);
        colour = gold + vec3(1.0, 0.95, 0.8) * glint * 2.0;
    }
    gl_FragColor = vec4(fogged(colour, vDepth), uAlpha);
}
"""

POINT_VERTEX = ROAD_GLSL + """
attribute vec3 aFrom;       // across, up, along the road's own length
attribute vec3 aGo;         // how it moves, a second
attribute vec2 aLife;       // born, lasts
attribute vec4 aLook;       // colour, size
uniform mat4 uView;
uniform float uNow;
uniform float uTravel;
uniform float uScreen;      // pixels per unit at a distance of one
varying vec4 vLook;
varying float vLeft;
varying float vDepth;
void main() {
    float t = uNow - aLife.x;
    vLeft = 1.0 - t / max(aLife.y, 0.001);
    vec3 at = aFrom + aGo * t + vec3(0.0, -4.0, 0.0) * t * t * 0.5;
    vec3 p = onRoad(at.x, at.y, at.z - uTravel);
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    vLook = aLook;
    float alive = step(0.0, t) * step(0.0, vLeft);
    gl_PointSize = alive * aLook.a * uScreen / max(vDepth, 0.2);
    gl_Position = eye;
}
"""

POINT_FRAGMENT = """
varying vec4 vLook;
varying float vLeft;
varying float vDepth;
void main() {
    vec2 d = gl_PointCoord - 0.5;
    float r = length(d) * 2.0;
    float glow = exp(-r * r * 4.0);
    float left = clamp(vLeft, 0.0, 1.0);
    gl_FragColor = vec4(vLook.rgb * glow * left * left, 1.0);
}
"""

TOWER_VERTEX = ROAD_GLSL + """
attribute vec3 aCorner;     // a unit box, 0..1 up
attribute vec4 aTower;      // across, along its loop, width, band
uniform mat4 uView;
uniform float uLevels[%(bands)d];
uniform float uTravel;
uniform float uLoop;
uniform float uFrom;
uniform float uKick;
varying vec3 vCorner;
varying float vDepth;
varying float vLevel;
varying float vTall;
void main() {
    float level = uLevels[int(aTower.w)];
    float reach = abs(aTower.x);
    float tall = (1.8 + level * 8.0 + uKick * level * 2.5)
                 * (0.55 + clamp(reach / 18.0, 0.0, 0.9));
    float along = mod(aTower.y - uTravel, uLoop) + uFrom;
    vec3 local = vec3(aCorner.x * aTower.z, aCorner.y * tall,
                      aCorner.z * aTower.z);
    vec3 p = onRoad(aTower.x + local.x, local.y - 1.2, along + local.z);
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    vCorner = aCorner;
    vLevel = level;
    vTall = tall;
    gl_Position = eye;
}
""" % {"bands": BANDS}

TOWER_FRAGMENT = HUES_GLSL + FOG_GLSL + """
varying vec3 vCorner;
varying float vDepth;
varying float vLevel;
varying float vTall;
uniform float uHue;
uniform float uBeat;
void main() {
    vec3 hue = hsv(uHue + 0.5 + (hash(vec2(floor(vTall), 3.0)) - 0.5) * 0.12,
                   0.85, 1.0);
    vec3 colour = vec3(0.012, 0.012, 0.03);
    // Windows: a grid of lit cells up the face, more of them lit the
    // louder the band.
    vec2 cell = vec2(vCorner.x + vCorner.z, vCorner.y * vTall * 1.6);
    vec2 f = fract(cell * vec2(4.0, 1.0));
    float window = step(0.25, f.x) * step(f.x, 0.75) * step(0.3, f.y)
                   * step(f.y, 0.7);
    float on = step(hash(floor(cell * vec2(4.0, 1.0)) + floor(vLevel * 6.0)),
                    0.25 + vLevel * 0.6);
    colour += hue * window * on * (0.25 + vLevel * 0.9);
    // The roof line, the brightest thing on it.
    float roof = smoothstep(0.96, 1.0, vCorner.y);
    colour += hue * roof * (0.8 + vLevel * 3.0 + uBeat * 1.5);
    gl_FragColor = vec4(fogged(colour, vDepth), 1.0);
}
"""

BRIGHT_FRAGMENT = """
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uTexel;
uniform float uThreshold;
void main() {
    vec3 c = vec3(0.0);
    c += texture2D(uTex, vUv + uTexel * vec2(-0.5, -0.5)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(0.5, -0.5)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(-0.5, 0.5)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(0.5, 0.5)).rgb;
    c *= 0.25;
    float peak = max(c.r, max(c.g, c.b));
    float knee = uThreshold * 0.5;
    float soft = clamp(peak - uThreshold + knee, 0.0, 2.0 * knee);
    soft = soft * soft / (4.0 * knee + 0.0001);
    float kept = max(soft, peak - uThreshold) / max(peak, 0.0001);
    gl_FragColor = vec4(c * kept, 1.0);
}
"""

DOWN_FRAGMENT = """
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uTexel;
void main() {
    vec3 c = texture2D(uTex, vUv).rgb * 4.0;
    c += texture2D(uTex, vUv + uTexel * vec2(-1.0, -1.0)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(1.0, -1.0)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(-1.0, 1.0)).rgb;
    c += texture2D(uTex, vUv + uTexel * vec2(1.0, 1.0)).rgb;
    gl_FragColor = vec4(c / 8.0, 1.0);
}
"""

UP_FRAGMENT = """
varying vec2 vUv;
uniform sampler2D uTex;
uniform vec2 uTexel;
uniform float uSpread;
void main() {
    vec2 o = uTexel * uSpread;
    vec3 c = texture2D(uTex, vUv + vec2(-o.x, 0.0)).rgb;
    c += texture2D(uTex, vUv + vec2(o.x, 0.0)).rgb;
    c += texture2D(uTex, vUv + vec2(0.0, -o.y)).rgb;
    c += texture2D(uTex, vUv + vec2(0.0, o.y)).rgb;
    c += texture2D(uTex, vUv + vec2(-o.x, -o.y) * 0.7).rgb * 0.5;
    c += texture2D(uTex, vUv + vec2(o.x, -o.y) * 0.7).rgb * 0.5;
    c += texture2D(uTex, vUv + vec2(-o.x, o.y) * 0.7).rgb * 0.5;
    c += texture2D(uTex, vUv + vec2(o.x, o.y) * 0.7).rgb * 0.5;
    gl_FragColor = vec4(c / 6.0, 1.0);
}
"""

FINAL_FRAGMENT = HUES_GLSL + """
varying vec2 vUv;
uniform sampler2D uScene;
uniform sampler2D uBloom;
uniform float uBloomAmount;
uniform float uExposure;
uniform float uSplit;        // colour fringing, from the middle out
uniform vec2 uShockAt;       // a hit's shockwave: where, how far, how hard
uniform float uShock;
uniform float uShockHard;
uniform float uAspect;
uniform float uHurt;
uniform float uFlash;        // the strobe
uniform float uOpacity;
uniform float uTime;
uniform float uVignette;
vec3 aces(vec3 x) {
    return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14),
                 0.0, 1.0);
}
void main() {
    vec2 uv = vUv;
    // The shockwave: a ring of bent light running out from the hit.
    vec2 d = (uv - uShockAt) * vec2(uAspect, 1.0);
    float r = length(d);
    float ring = exp(-pow((r - uShock) * 18.0, 2.0)) * uShockHard;
    uv -= normalize(d + 0.0001) / vec2(uAspect, 1.0) * ring * 0.035;
    // Colour split, from the middle out, harder at the edges.
    vec2 outward = uv - 0.5;
    float edge = dot(outward, outward) * 4.0;
    vec2 shift = outward * uSplit * (0.004 + edge * 0.012) + ring * 0.01;
    vec3 c;
    c.r = texture2D(uScene, uv + shift).r;
    c.g = texture2D(uScene, uv).g;
    c.b = texture2D(uScene, uv - shift).b;
    vec3 glow = texture2D(uBloom, uv).rgb;
    c += glow * uBloomAmount;
    c *= uExposure * (1.0 + uFlash * 0.6);
    c = aces(c);
    // Richer rather than paler: pull the colour away from grey a little,
    // which is what tone mapping takes out of the brightest things.
    float grey = dot(c, vec3(0.299, 0.587, 0.114));
    c = mix(vec3(grey), c, 1.18);
    // A hit takes the colour out of the world and leaves red.
    c = mix(c, vec3(grey * 1.4, grey * 0.15, grey * 0.12) + vec3(0.25, 0.0, 0.0)
            * uHurt, uHurt * 0.75);
    float v = smoothstep(1.25, 0.25, length((vUv - 0.5) * vec2(1.0, 0.8)) * 1.6);
    c *= mix(1.0, v, uVignette);
    // Dithered, a least significant bit of triangular noise per channel:
    // a slow dark gradient quantised to eight bits is a set of rings.
    vec2 seed = gl_FragCoord.xy + fract(uTime) * 61.0;
    vec3 noise = vec3(hash(seed) + hash(seed + 17.1) - 1.0,
                      hash(seed + 3.7) + hash(seed + 29.3) - 1.0,
                      hash(seed + 7.9) + hash(seed + 41.7) - 1.0);
    c += noise / 255.0;
    gl_FragColor = vec4(clamp(c, 0.0, 1.0), uOpacity);
}
"""

STREAK_VERTEX = ROAD_GLSL + """
attribute vec4 aStreak;      // across, up, along its loop, which end
uniform mat4 uView;
uniform float uTravel;
uniform float uLoop;
uniform float uFrom;
uniform float uLen;
varying float vDepth;
varying float vEnd;
void main() {
    float along = mod(aStreak.z - uTravel, uLoop) + uFrom;
    along -= aStreak.w * uLen;
    vec3 p = onRoad(aStreak.x, aStreak.y, along);
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    vEnd = aStreak.w;
    gl_Position = eye;
}
"""

STREAK_FRAGMENT = FOG_GLSL + """
varying float vDepth;
varying float vEnd;
uniform vec3 uColour;
uniform float uAmount;
void main() {
    float far = 1.0 - clamp(vDepth / 60.0, 0.0, 1.0);
    gl_FragColor = vec4(uColour * uAmount * (1.0 - vEnd) * far, 1.0);
}
"""


# ==========================================================================
# Geometry, built once
# ==========================================================================
def _flat_normals(triangles):
    """(x, y, z) corners in threes, to x, y, z, nx, ny, nz per corner."""
    out = []
    for index in range(0, len(triangles), 3):
        a, b, c = triangles[index:index + 3]
        u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
        v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
        n = (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2],
             u[0] * v[1] - u[1] * v[0])
        size = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
        n = (n[0] / size, n[1] / size, n[2] / size)
        for corner in (a, b, c):
            out.extend(corner)
            out.extend(n)
    return out


def cube_triangles():
    """A unit cube about the origin, as triangles."""
    h = 0.5
    faces = (
        ((-h, -h, h), (h, -h, h), (h, h, h), (-h, h, h)),
        ((h, -h, -h), (-h, -h, -h), (-h, h, -h), (h, h, -h)),
        ((-h, -h, -h), (-h, -h, h), (-h, h, h), (-h, h, -h)),
        ((h, -h, h), (h, -h, -h), (h, h, -h), (h, h, h)),
        ((-h, h, h), (h, h, h), (h, h, -h), (-h, h, -h)),
        ((-h, -h, -h), (h, -h, -h), (h, -h, h), (-h, -h, h)),
    )
    out = []
    for a, b, c, d in faces:
        out.extend((a, b, c, a, c, d))
    return out


#: The craft, in its own frame: across, up, and along, with the nose at
#: -z - the way the road runs away from the camera. Built by lofting
#: cross-sections, the way a hull is drawn: a sequence of outlines along
#: its length, joined up.

def _loft(sections, close_front=True, close_back=True) -> list:
    """Triangles through ``sections``: [(z, [(x, y), ...]), ...], each
    outline going the same way round with the same number of points."""
    out = []
    for (z0, ring0), (z1, ring1) in zip(sections, sections[1:]):
        count = len(ring0)
        for i in range(count):
            j = (i + 1) % count
            a = (ring0[i][0], ring0[i][1], z0)
            b = (ring0[j][0], ring0[j][1], z0)
            c = (ring1[j][0], ring1[j][1], z1)
            d = (ring1[i][0], ring1[i][1], z1)
            out.extend((a, c, b, a, d, c))
    for (z, ring), front in ((sections[0], True), (sections[-1], False)):
        if (front and not close_front) or (not front and not close_back):
            continue
        middle = (sum(x for x, _ in ring) / len(ring),
                  sum(y for _, y in ring) / len(ring), z)
        for i in range(len(ring)):
            j = (i + 1) % len(ring)
            a = (ring[i][0], ring[i][1], z)
            b = (ring[j][0], ring[j][1], z)
            out.extend((middle, a, b) if front else (middle, b, a))
    return out


def _outline(width: float, low: float, high: float, top: float = 1.0,
             points: int = 10) -> list:
    """A rounded cross-section: wider at the waist, ``top`` pinching the
    upper half, clockwise seen from the nose."""
    out = []
    middle = (low + high) / 2.0
    half_tall = (high - low) / 2.0
    for i in range(points):
        a = math.tau * i / points
        x = math.cos(a) * width / 2.0
        y = math.sin(a)
        squeeze = top if y > 0 else 1.0
        out.append((x * (squeeze if y > 0 else 1.0) ** 0.5,
                    middle + y * half_tall))
    return out[::-1]


HULL = [(-1.10, _outline(0.02, 0.05, 0.07)),
        (-0.80, _outline(0.12, 0.02, 0.10, 0.7)),
        (-0.40, _outline(0.24, 0.00, 0.15, 0.7)),
        (0.05, _outline(0.30, -0.01, 0.17, 0.8)),
        (0.45, _outline(0.26, 0.01, 0.15, 0.8)),
        (0.62, _outline(0.18, 0.03, 0.12, 0.8))]
CANOPY = [(-0.42, _outline(0.02, 0.13, 0.14)),
          (-0.28, _outline(0.13, 0.12, 0.22, 0.6)),
          (-0.02, _outline(0.17, 0.12, 0.26, 0.6)),
          (0.20, _outline(0.12, 0.12, 0.20, 0.6)),
          (0.30, _outline(0.02, 0.13, 0.14))]
NACELLE_X = 0.36
NACELLE_Y = 0.06
NACELLE = [(-0.30, _outline(0.07, -0.03, 0.04, 1.0, 8)),
           (-0.20, _outline(0.14, -0.06, 0.07, 1.0, 8)),
           (0.40, _outline(0.15, -0.065, 0.075, 1.0, 8)),
           (0.62, _outline(0.12, -0.05, 0.06, 1.0, 8))]
#: Where the engines' glow is, in the craft's frame.
NOZZLES = ((-NACELLE_X, NACELLE_Y, 0.64), (NACELLE_X, NACELLE_Y, 0.64))
#: How big the craft is drawn, against its own model.
SHIP = 0.92


def _shifted(triangles, dx, dy, dz=0.0):
    return [(x + dx, y + dy, z + dz) for x, y, z in triangles]


def _wing(side: float) -> list:
    """A swept slab from the hull out past the engine, thin."""
    t = 0.018
    root_front = (side * 0.12, 0.05, -0.30)
    root_back = (side * 0.12, 0.05, 0.40)
    tip_back = (side * 0.62, 0.02, 0.50)
    tip_front = (side * 0.58, 0.02, 0.30)
    quad = [root_front, tip_front, tip_back, root_back]
    top = [(x, y + t, z) for x, y, z in quad]
    bottom = [(x, y - t, z) for x, y, z in quad]
    out = []
    order = (0, 1, 2, 0, 2, 3) if side < 0 else (0, 2, 1, 0, 3, 2)
    out.extend(top[i] for i in order)
    out.extend(bottom[i] for i in reversed(order))
    for i in range(4):
        j = (i + 1) % 4
        out.extend((top[i], bottom[i], top[j], top[j], bottom[i], bottom[j]))
    return out


def _fin(side: float) -> list:
    """A small upright fin on each engine."""
    x = side * NACELLE_X
    base_front = (x, 0.12, 0.20)
    base_back = (x, 0.12, 0.60)
    top_back = (x + side * 0.03, 0.30, 0.62)
    t = 0.012
    tri = [base_front, base_back, top_back]
    left = [(px - t, py, pz) for px, py, pz in tri]
    right = [(px + t, py, pz) for px, py, pz in tri]
    return left + right[::-1] + [left[0], right[0], left[1],
                                 left[1], right[0], right[1]]


def ship_triangles():
    """The craft's body: hull, wings, engines and fins, in its metal."""
    out = _loft(HULL)
    for side in (-1.0, 1.0):
        out += _wing(side)
        out += _shifted(_loft(NACELLE), side * NACELLE_X, NACELLE_Y)
        out += _fin(side)
    return out


def canopy_triangles():
    return _loft(CANOPY)


def ship_outline():
    """The lines the craft's neon trim is drawn along."""
    pairs = []
    # Down the spine and along each flank at the waist.
    for index in range(len(HULL) - 1):
        z0, ring0 = HULL[index]
        z1, ring1 = HULL[index + 1]
        for k in (0, len(ring0) // 2):
            pairs += [(ring0[k][0], ring0[k][1], z0),
                      (ring1[k][0], ring1[k][1], z1)]
    for side in (-1.0, 1.0):
        # The wings' leading and trailing edges.
        pairs += [(side * 0.12, 0.07, -0.30), (side * 0.58, 0.04, 0.30),
                  (side * 0.58, 0.04, 0.30), (side * 0.62, 0.04, 0.50),
                  (side * 0.62, 0.04, 0.50), (side * 0.12, 0.07, 0.40)]
        # A ring round each intake and each nozzle.
        for z, ring in (NACELLE[1], NACELLE[-1]):
            for i in range(len(ring)):
                j = (i + 1) % len(ring)
                pairs += [(side * NACELLE_X + ring[i][0],
                           NACELLE_Y + ring[i][1], z),
                          (side * NACELLE_X + ring[j][0],
                           NACELLE_Y + ring[j][1], z)]
    out = []
    for corner in pairs:
        out.extend(corner)
        out.extend((0.0, 1.0, 0.0))
    return out


def disc_triangles(segments=28):
    """A coin: a flat cylinder standing up, facing along the road."""
    out = []
    thick = 0.08
    for index in range(segments):
        a0 = index / segments * math.tau
        a1 = (index + 1) / segments * math.tau
        p0 = (math.cos(a0) * 0.5, math.sin(a0) * 0.5)
        p1 = (math.cos(a1) * 0.5, math.sin(a1) * 0.5)
        front = (0.0, 0.0, -thick)
        back = (0.0, 0.0, thick)
        out.extend((front, (p1[0], p1[1], -thick), (p0[0], p0[1], -thick)))
        out.extend((back, (p0[0], p0[1], thick), (p1[0], p1[1], thick)))
        out.extend(((p0[0], p0[1], -thick), (p1[0], p1[1], -thick),
                    (p1[0], p1[1], thick)))
        out.extend(((p0[0], p0[1], -thick), (p1[0], p1[1], thick),
                    (p0[0], p0[1], thick)))
    return out


def arch_triangles(half: float, segments=48, thick=0.022):
    """A gate over the road: half a ring, flat along the road."""
    out = []
    radius = half + 0.55
    for index in range(segments):
        a0 = index / segments * math.pi
        a1 = (index + 1) / segments * math.pi
        inner0 = (math.cos(a0) * radius, math.sin(a0) * radius * 0.62)
        inner1 = (math.cos(a1) * radius, math.sin(a1) * radius * 0.62)
        outer0 = (math.cos(a0) * (radius + thick * 2),
                  math.sin(a0) * (radius + thick * 2) * 0.62)
        outer1 = (math.cos(a1) * (radius + thick * 2),
                  math.sin(a1) * (radius + thick * 2) * 0.62)
        for a, b, c in ((inner0, outer0, outer1), (inner0, outer1, inner1)):
            out.extend(((a[0], a[1], 0.0), (b[0], b[1], 0.0),
                        (c[0], c[1], 0.0)))
    return out


def road_floats(half: float) -> list:
    """The deck, as (across, along) pairs of triangles."""
    across = [-half - 0.24, -half - 0.12, -half + 0.2,
              -half * 0.5, 0.0, half * 0.5,
              half - 0.2, half + 0.12, half + 0.24]
    along = []
    z = ROAD_FROM + 0.5
    while z < ROAD_TO - 0.5:
        along.append(z)
        z += 0.18 if z < 12 else 0.3 if z < 30 else 0.6
    out = []
    for i in range(len(along) - 1):
        z0, z1 = along[i], along[i + 1]
        for j in range(len(across) - 1):
            u0, u1 = across[j], across[j + 1]
            out.extend((u0, z0, u1, z0, u1, z1, u0, z0, u1, z1, u0, z1))
    return out


def tower_floats(half: float, count=150, loop=80.0, seed=11) -> list:
    """The city: boxes both sides of the road, open at the bottom."""
    rng = random.Random(seed)
    box = cube_triangles()
    out = []
    for index in range(count):
        side = -1.0 if index % 2 else 1.0
        away = half + 4.2 + rng.random() ** 1.3 * 18.0
        width = 0.7 + rng.random() * 1.9 * (0.6 + away / 14.0)
        along = rng.random() * loop
        band = min(BANDS - 1, int(rng.random() * BANDS))
        for x, y, z in box:
            if y < 0:
                y = 0.0
            else:
                y = 1.0
            out.extend((x, y, z, side * away, along, width, float(band)))
    return out


def streak_floats(half: float, count=420, loop=70.0, seed=5) -> list:
    rng = random.Random(seed)
    out = []
    for _ in range(count):
        side = -1.0 if rng.random() < 0.5 else 1.0
        across = side * (half + 0.8 + rng.random() * 9.0)
        up = -0.8 + rng.random() * 6.5
        along = rng.random() * loop
        out.extend((across, up, along, 0.0, across, up, along, 1.0))
    return out


# ==========================================================================
# The world
# ==========================================================================
class RiderWorld:
    """Everything the rider draws on the card, for one GL context.

    Made the first time a frame is drawn in a context and thrown away
    with it - see Spectrum's canvas, which makes a new one when a move
    into full screen brings a new context.
    """

    #: Where the camera rides: behind the craft, above it, and looking
    #: this far down the road.
    CAM_BACK = 2.45
    CAM_UP = 1.02
    CAM_AHEAD = 6.5
    CAM_LOOK_UP = 0.1
    #: How the camera follows, as a share of the way a second.
    CAM_FOLLOW = 9.0
    #: How much of the road's bank the camera takes on. All of it is the
    #: world tipping every time the road turns, which is disorienting;
    #: none of it is a camera bolted level to a road that is not.
    CAM_BANK = 0.45
    #: The field of view, calm and at full tilt, in degrees.
    FOV_CALM = 62.0
    FOV_FAST = 74.0
    #: How far the craft hovers.
    HOVER = 0.28

    PARTICLES = 3000

    def __init__(self, gl) -> None:
        self.gl = gl
        self.quad = _Mesh([-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0],
                          [(b"aCorner", 2)], GL_TRIANGLE_STRIP)
        self.sky = _program(QUAD_VERTEX, SKY_FRAGMENT)
        self.road = _program(ROAD_VERTEX, ROAD_FRAGMENT)
        self.solid = _program(SOLID_VERTEX, SOLID_FRAGMENT)
        self.points = _program(POINT_VERTEX, POINT_FRAGMENT)
        self.towers = _program(TOWER_VERTEX, TOWER_FRAGMENT)
        self.streak = _program(STREAK_VERTEX, STREAK_FRAGMENT)
        self.bright = _program(QUAD_VERTEX, BRIGHT_FRAGMENT)
        self.down = _program(QUAD_VERTEX, DOWN_FRAGMENT)
        self.up = _program(QUAD_VERTEX, UP_FRAGMENT)
        self.final = _program(QUAD_VERTEX, FINAL_FRAGMENT)
        self._half = None
        self._fbo_size = None
        self.hdr = self.resolved = None
        self.chain = []
        self._particles = array("f", [0.0] * (self.PARTICLES * 12))
        self._particle_next = 0
        self._particle_mesh = _Mesh(self._particles,
                                    [(b"aFrom", 3), (b"aGo", 3),
                                     (b"aLife", 2), (b"aLook", 4)], GL_POINTS)
        self._particles_dirty = False
        self._rng = random.Random(3)
        self._now = 0.0
        self._cam_eye = None
        self._cam_look = None
        self._seen_pops = set()
        self._wave = -99.0
        self._wave_lit = 0.0
        self._kick_was = 0.0
        self._kick_punch = 0.0
        self._flash_lane = 0.0
        self._flash = 0.0
        self._flash_colour = (1.0, 1.0, 1.0)
        self._shock = 9.0
        self._shock_hard = 0.0
        self._shock_at = (0.5, 0.3)
        self._split = 0.0
        self._knock = 0.0
        self._trim = 0.0
        self._trim_colour = (1.0, 1.0, 1.0)
        self._bloom_bump = 0.0
        self._screen_ship = (0.5, 0.3)
        #: Blocks seen finished, and which of those the craft took.
        self._done_seen = set()
        self._taken = set()
        self._taken_now = []

    # -- meshes that depend on the road's width ---------------------------
    def _build(self, half: float) -> None:
        if self._half == half:
            return
        self._half = half
        self.road_mesh = _Mesh(road_floats(half), [(b"aRoad", 2)])
        cube = _flat_normals(cube_triangles())
        self.cube = _Mesh(cube, [(b"aPos", 3), (b"aNormal", 3)])
        self.ship = _Mesh(_flat_normals(ship_triangles()),
                          [(b"aPos", 3), (b"aNormal", 3)])
        self.canopy = _Mesh(_flat_normals(canopy_triangles()),
                            [(b"aPos", 3), (b"aNormal", 3)])
        self.ship_trim = _Mesh(ship_outline(), [(b"aPos", 3), (b"aNormal", 3)],
                               GL_LINES)
        self.coin = _Mesh(_flat_normals(disc_triangles()),
                          [(b"aPos", 3), (b"aNormal", 3)])
        self.arch = _Mesh(_flat_normals(arch_triangles(half)),
                          [(b"aPos", 3), (b"aNormal", 3)])
        self.city = _Mesh(tower_floats(half), [(b"aCorner", 3), (b"aTower", 4)])
        self.streaks = _Mesh(streak_floats(half), [(b"aStreak", 4)], GL_LINES)

    # -- framebuffers ------------------------------------------------------
    #: The world's own light, in floating point but packed: eleven bits
    #: for red and green and ten for blue, a third of the memory of four
    #: half floats - and memory is what a multisampled full screen costs.
    #: Falls back to half floats, and then to bytes, where a card will not
    #: make one.
    FORMATS = (GL_R11F_G11F_B10F, GL_RGBA16F, GL_RGBA8)

    def _framebuffers(self, width: int, height: int, samples: int = 4) -> None:
        if self._fbo_size == (width, height, samples):
            return
        self._fbo_size = (width, height, samples)
        gl = self.gl
        formats = self.FORMATS

        def made(w, h, samples=0, depth=False):
            made = None
            for internal in formats:
                shape = QOpenGLFramebufferObjectFormat()
                shape.setSamples(samples)
                shape.setInternalTextureFormat(internal)
                if depth:
                    shape.setAttachment(
                        QOpenGLFramebufferObject.Attachment.Depth)
                made = QOpenGLFramebufferObject(QSize(max(1, w), max(1, h)),
                                                shape)
                if made.isValid():
                    break
            if samples == 0:
                gl.glBindTexture(GL_TEXTURE_2D, made.texture())
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER,
                                   GL_LINEAR)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER,
                                   GL_LINEAR)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S,
                                   GL_CLAMP_TO_EDGE)
                gl.glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T,
                                   GL_CLAMP_TO_EDGE)
                gl.glBindTexture(GL_TEXTURE_2D, 0)
            return made

        self.hdr = made(width, height, samples=samples, depth=True)
        self.resolved = made(width, height)
        self.chain = []
        w, h = width // 2, height // 2
        while w >= 8 and h >= 8 and len(self.chain) < 6:
            self.chain.append(made(w, h))
            w, h = w // 2, h // 2
        self.bloomed = [made(fbo.width(), fbo.height()) for fbo in self.chain]

    # -- the road, read once a frame ----------------------------------------
    #: How much of the road's roll is drawn as the road's own bank, and
    #: which way: the scene's roll is measured for a picture whose y runs
    #: down the screen.
    ROLL_SHARE = -0.5

    def _read_road(self, scene) -> list:
        side, under = scene._side, scene._under
        out = []
        for index in range(ROAD_SAMPLES):
            z = ROAD_FROM + index * ROAD_STEP
            across, lift, roll = scene._road(z)
            out.extend((across - side, -(lift - under),
                        roll * self.ROLL_SHARE, 0.0))
        return out

    @staticmethod
    def _on_road(road, u: float, h: float, z: float) -> tuple:
        """The shader's onRoad, in Python, for the camera and the HUD."""
        f = max(0.0, min((z - ROAD_FROM) / ROAD_STEP, ROAD_SAMPLES - 1.001))
        i = int(f)
        t = f - i
        a = road[i * 4:i * 4 + 4]
        b = road[i * 4 + 4:i * 4 + 8]
        x, y, r = (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t,
                   a[2] + (b[2] - a[2]) * t)
        c, s = math.cos(r), math.sin(r)
        return (x + u * c - h * s, y + u * s + h * c, -z)

    # -- one frame -----------------------------------------------------------
    def draw(self, scene, state, target: int, viewport: QRect,
             opacity: float, now: float, samples: int = 4) -> None:
        """The world, into ``target`` at ``viewport``, which is in the
        target's own pixels with its origin at the bottom left."""
        gl = self.gl
        dt = 1.0 / 60.0 if not self._now else max(0.0, min(0.1, now - self._now))
        self._now = now
        half = scene.LANE_WIDE * scene.LANES / 2.0
        self._build(half)
        width, height = max(8, viewport.width()), max(8, viewport.height())
        self._framebuffers(width, height, samples)
        frame = self._frame(scene, state, dt, width / height)

        gl.glDisable(GL_SCISSOR_TEST)
        gl.glDisable(GL_STENCIL_TEST)
        gl.glDisable(GL_CULL_FACE)
        self.hdr.bind()
        gl.glViewport(0, 0, width, height)
        gl.glClearColor(0.0, 0.0, 0.0, 1.0)
        gl.glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        gl.glDisable(GL_DEPTH_TEST)
        gl.glDisable(GL_BLEND)
        self._draw_sky(frame)
        gl.glEnable(GL_DEPTH_TEST)
        gl.glDepthFunc(GL_LEQUAL)
        gl.glDepthMask(1)
        self._draw_city(frame)
        self._draw_road(frame)
        self._draw_blocks(frame)
        self._draw_ship(frame)
        # What glows, over the top, adding light and hiding nothing.
        gl.glEnable(GL_BLEND)
        gl.glBlendFunc(GL_ONE, GL_ONE)
        gl.glDepthMask(0)
        self._draw_gates(frame)
        self._draw_streaks(frame)
        self._draw_trim(frame)
        self._draw_particles(frame)
        gl.glDepthMask(1)
        gl.glDisable(GL_DEPTH_TEST)
        gl.glDisable(GL_BLEND)
        self.hdr.release()
        whole = QRect(0, 0, width, height)
        QOpenGLFramebufferObject.blitFramebuffer(
            self.resolved, whole, self.hdr, whole, GL_COLOR_BUFFER_BIT, 0x2600)
        self._bloom(frame)

        gl.glBindFramebuffer(GL_FRAMEBUFFER, target)
        gl.glViewport(viewport.x(), viewport.y(), width, height)
        if opacity < 0.999:
            gl.glEnable(GL_BLEND)
            gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        self._draw_final(frame, opacity)
        gl.glDisable(GL_BLEND)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, 0)

    # -- what a frame needs, worked out once --------------------------------
    def _frame(self, scene, state, dt: float, aspect: float) -> dict:
        import colorsys

        road = self._read_road(scene)
        kit = state.kit or {}
        kick = float(kit.get("Kick", 0.0) or 0.0)
        beat = float(getattr(scene, "_beat_lit", 0.0))
        if not kit:
            kick = beat
        # A kick is a moment, not a level: on the way up through the
        # middle, and then the wave goes out and the view punches wider.
        if kick > 0.55 >= self._kick_was:
            self._wave = scene.RIDER_AT
            self._wave_lit = 1.0
            self._kick_punch = 1.0
        self._kick_was = kick
        self._wave += dt * 38.0
        self._wave_lit *= math.exp(-dt * 2.2)
        self._kick_punch *= math.exp(-dt * 7.0)
        self._notice(scene)
        self._events(scene)
        self._flash *= math.exp(-dt * 7.0)
        self._trim *= math.exp(-dt * 5.0)
        self._bloom_bump *= math.exp(-dt * 4.0)
        self._knock *= math.exp(-dt * 6.0)
        self._shock += dt * 1.1
        self._shock_hard *= math.exp(-dt * 3.0)
        self._split *= math.exp(-dt * 5.0)

        loud = float(getattr(scene, "_loudness", 0.0))
        rush = max(0.0, min(1.0, float(getattr(scene, "_rushing", 0.0))))
        hurt = float(getattr(scene, "_hurt", 0.0))
        hue = float(getattr(scene, "_hue_now", 0.6))
        air = float(getattr(scene, "_air", 0.0))
        across = float(scene._lane_here)

        # The camera, on a spring, following the craft down the road.
        ship_z = scene.RIDER_AT
        eye = self._on_road(road, across * 0.35,
                            self.CAM_UP + air * 0.4 + self._knock * 0.5,
                            ship_z - self.CAM_BACK - self._knock * 0.8)
        look = self._on_road(road, across * 0.55, self.CAM_LOOK_UP + air * 0.3,
                             ship_z + self.CAM_AHEAD)
        follow = 1.0 - math.exp(-self.CAM_FOLLOW * dt)
        if self._cam_eye is None:
            self._cam_eye, self._cam_look = eye, look
        self._cam_eye = tuple(c + (w - c) * follow
                              for c, w in zip(self._cam_eye, eye))
        self._cam_look = tuple(c + (w - c) * follow
                               for c, w in zip(self._cam_look, look))
        shake = float(getattr(scene, "_shake", 0.0))
        t = self._now
        wobble = (shake * shake * 0.05 + hurt * hurt * 0.16)
        jolt = (math.sin(t * 31.0) * wobble, math.sin(t * 27.0 + 1.3) * wobble,
                0.0)
        eye_at = tuple(c + j for c, j in zip(self._cam_eye, jolt))
        bank = road[int((ship_z - ROAD_FROM) / ROAD_STEP) * 4 + 2]
        roll = (bank * self.CAM_BANK
                + float(getattr(scene, "_rolled", 0.0)) * math.tau
                + math.sin(float(getattr(scene, "_wobble", 0.0)) * 2.3)
                * hurt * 0.10)
        fov = (self.FOV_CALM + (self.FOV_FAST - self.FOV_CALM) * rush
               + self._kick_punch * 3.0)
        view = QMatrix4x4()
        view.perspective(fov / max(1.0, aspect / 1.6) ** 0.35, aspect,
                         0.05, 400.0)
        view.lookAt(QVector3D(*eye_at), QVector3D(*self._cam_look),
                    QVector3D(math.sin(roll), math.cos(roll), 0.0))

        def screen(point):
            v = view.map(QVector3D(*point))
            return (v.x() * 0.5 + 0.5, v.y() * 0.5 + 0.5)

        ship_at = self._on_road(road, across, self.HOVER + air, ship_z)
        self._screen_ship = screen(ship_at)
        horizon = screen(self._on_road(road, 0.0, 0.0, ROAD_TO - 2.0))
        sun = screen(self._on_road(road, 0.0, 6.0, ROAD_TO - 2.0))
        # The colour of the horizon, so the road runs into the sky rather
        # than stopping short of it.
        fog = colorsys.hsv_to_rgb(hue % 1.0, 0.95, 0.20 + loud * 0.18)

        levels = list(getattr(state, "levels", ()) or ())
        if levels:
            bands = [max(0.0, min(1.0, float(levels[min(
                len(levels) - 1, index * len(levels) // BANDS)])))
                for index in range(BANDS)]
        else:
            bands = [0.0] * BANDS
        travel = float(scene._at)
        per_beat = scene.PER_BEAT if scene._beat > 0.0 else 1e6
        return {
            "road": road, "view": view, "hue": hue, "loud": loud,
            "rush": rush, "hurt": hurt, "beat": beat, "kick": kick,
            "air": air, "across": across, "aspect": aspect, "roll": roll,
            "horizon": horizon, "sun": sun, "fog": fog, "bands": bands,
            "travel": travel, "per_beat": per_beat, "scene": scene,
            "flash": float(scene.flash(state)) if hasattr(scene, "flash")
            else 0.0,
            "half": scene.LANE_WIDE * scene.LANES / 2.0,
            "pixels": 1.0,
        }

    def _road_uniforms(self, program, frame) -> None:
        program.array("uRoad", frame["road"], ROAD_SAMPLES, 4)
        program.set("uRoadFrom", float(ROAD_FROM))
        program.set("uRoadStep", float(ROAD_STEP))
        program.set("uView", frame["view"])

    def _fog_uniforms(self, program, frame) -> None:
        program.set("uFog", QVector3D(*frame["fog"]))
        program.set("uFogFrom", 16.0)
        program.set("uFogTo", 72.0)

    # -- the passes -----------------------------------------------------------
    def _draw_sky(self, frame) -> None:
        p = self.sky
        p.bind()
        p.set("uHue", float(frame["hue"]))
        p.set("uLoud", float(frame["loud"]))
        p.set("uKick", float(self._kick_punch))
        p.set("uTime", float(self._now % 1000.0))
        p.set("uHorizon", float(frame["horizon"][1]))
        p.set("uRoll", float(frame["roll"]))
        p.set("uAspect", float(frame["aspect"]))
        p.set("uSun", QVector3D(frame["sun"][0], frame["sun"][1],
                                             0.0).toVector2D())
        p.set("uHurt", float(frame["hurt"]))
        self.quad.draw(self.gl, p)
        p.release()

    def _draw_city(self, frame) -> None:
        p = self.towers
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.array("uLevels", frame["bands"], BANDS, 1)
        p.set("uTravel", float(frame["travel"]))
        p.set("uLoop", 80.0)
        p.set("uFrom", float(ROAD_FROM))
        p.set("uKick", float(self._kick_punch))
        p.set("uHue", float(frame["hue"]))
        p.set("uBeat", float(frame["beat"]))
        self.city.draw(self.gl, p)
        p.release()

    def _draw_road(self, frame) -> None:
        import colorsys

        scene = frame["scene"]
        p = self.road
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.set("uHalf", float(frame["half"]))
        p.set("uLane", float(scene.LANE_WIDE))
        p.set("uTravel", float(frame["travel"] - scene.RIDER_AT))
        p.set("uPerBeat", float(frame["per_beat"]))
        p.set("uRider", float(scene.RIDER_AT))
        p.set("uHue", float(frame["hue"]))
        p.set("uLoud", float(frame["loud"]))
        p.set("uBeat", float(frame["beat"]))
        p.set("uKick", float(self._kick_punch))
        p.set("uWave", float(self._wave))
        p.set("uWaveLit", float(self._wave_lit))
        p.set("uFlashColour", QVector3D(*self._flash_colour))
        p.set("uFlashLane", float(self._flash_lane))
        p.set("uFlash", float(self._flash))
        p.set("uHurt", float(frame["hurt"]))
        p.set("uShipU", float(frame["across"]))
        self.road_mesh.draw(self.gl, p)
        p.release()

    def _solid(self, frame):
        p = self.solid
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.set("uLight", QVector3D(0.3, 1.0, 0.6))
        return p

    @staticmethod
    def _put(p, place, scale, angles, colour, emit, edge, grey, alpha=1.0):
        p.set("uPlace", QVector3D(*place))
        p.set("uScale", QVector3D(*scale))
        p.set("uAngles", QVector3D(*angles))
        p.set("uColour", QVector3D(*colour))
        p.set("uEmit", float(emit))
        p.set("uEdge", float(edge))
        p.set("uGrey", float(grey))
        p.set("uAlpha", float(alpha))
        p.set("uCoin", 0.0)

    def _draw_blocks(self, frame) -> None:
        import colorsys

        scene = frame["scene"]
        p = self._solid(frame)
        beat = frame["beat"]
        spin = float(getattr(scene, "_coin_spin", 0.0))
        far = float(scene.FAR)
        for block in scene._blocks:
            when, lane, kind, done, grey = block
            z = scene._where(when)
            if done and id(block) in self._taken:
                # Taken: it went into the ship. One that was not goes on
                # past and out of the picture, as anything you missed
                # would. See _notice.
                continue
            if z < scene.RIDER_AT - 4.0 or z > far + 0.5:
                continue
            # Grown in over the last few units rather than appearing.
            grow = max(0.0, min(1.0, (far + 0.5 - z) / 3.0))
            grow = grow * grow * (3.0 - 2.0 * grow)
            across = scene._lane_at(lane)
            # The last stretch before the craft, in its lane: drawn in
            # to it and shrinking, so a block being taken is seen going
            # into the ship rather than the ship vanishing inside it.
            gap = z - scene.RIDER_AT
            if (not done and gap < 0.9
                    and abs(across - frame["across"]) < scene.LANE_WIDE * 0.5):
                pull = max(0.0, min(1.0, 1.0 - gap / 0.9))
                grow *= 1.0 - pull * 0.85
                across += (frame["across"] - across) * pull
                if gap < 0.0:
                    continue
            if kind == "coin":
                size = scene.COIN_SIZE * 1.35 * grow
                self._put(p, (across, scene.COIN_TALL + 0.05, z),
                          (size, size, size),
                          (spin + when * 5.0, 0.0, 0.0),
                          (1.35, 0.92, 0.28), 0.0, 0.0, 0.0)
                p.set("uCoin", 1.0)
                self.coin.draw(self.gl, p)
                p.set("uCoin", 0.0)
                continue
            if kind == "power":
                size = 0.55 * grow * (1.0 + beat * 0.25)
                self._put(p, (across, 0.55, z), (size, size, size),
                          (spin * 1.3, 0.6, spin), (3.0, 3.0, 3.2),
                          0.9, 4.0, 0.0)
                self.cube.draw(self.gl, p)
                continue
            # The block itself: a prize is lit from inside in the colour
            # of its passage; an obstacle is dark metal with a warning.
            pulse = 1.0 + beat * (0.10 if grey else 0.16)
            wide = scene.LANE_WIDE * 0.50 * grow * pulse
            tall = 0.46 * grow * pulse
            if grey:
                self._put(p, (across, tall * 0.62, z),
                          (wide * 0.95, tall * 1.15, wide * 0.95),
                          (0.785, 0.0, 0.0), (0.9, 0.2, 0.15),
                          0.0, 2.2 + beat * 3.0, 1.0)
            else:
                colour = self._colour_of(scene, block)
                glow = 1.3 + beat * 1.2
                if done:
                    # Gone past: going dark as it leaves, so a miss
                    # does not smear light across the edge of the frame.
                    glow *= max(0.0, 1.0 + gap / 1.5)
                self._put(p, (across, tall * 0.5 + 0.02, z),
                          (wide, tall, wide * 0.8), (0.0, 0.0, 0.0),
                          tuple(c * glow for c in colour), 0.75, 2.6, 0.0)
            self.cube.draw(self.gl, p)
        p.release()

    @staticmethod
    def _colour_of(scene, block) -> tuple:
        """What a block is lit in: its passage's colour, gold for a
        coin, white for a power block, red for an obstacle."""
        import colorsys

        when, _lane, kind, _done, grey = block
        if grey:
            return (1.0, 0.18, 0.12)
        if kind == "coin":
            return (1.0, 0.72, 0.18)
        if kind == "power":
            return (1.0, 1.0, 1.0)
        shade = scene.TIERS[min(scene._tier_of(when), len(scene.TIERS) - 1)]
        return colorsys.hsv_to_rgb(shade, 0.85, 1.0)

    def _ship_place(self, frame):
        scene = frame["scene"]
        swerve = float(getattr(scene, "_swerve", 0.0))
        bank = max(-18.0, min(18.0, swerve * 0.8))
        bob = math.sin(self._now * 5.0) * 0.02 + frame["beat"] * 0.05
        return ((frame["across"], self.HOVER + frame["air"] + bob,
                 scene.RIDER_AT),
                (-swerve * 0.004, -0.05 - frame["air"] * 0.1,
                 math.radians(bank)))

    def _draw_ship(self, frame) -> None:
        import colorsys

        p = self._solid(frame)
        place, angles = self._ship_place(frame)
        hurt = frame["hurt"]
        # Gunmetal, lit by the world; red while it is hurt.
        body = (0.16 + hurt * 0.8, 0.17, 0.21)
        self._put(p, place, (SHIP, SHIP, SHIP), angles, body, 0.0, 0.0, 0.0)
        self.ship.draw(self.gl, p)
        # The canopy: tinted glass with the cockpit's light behind it.
        glass = colorsys.hsv_to_rgb((frame["hue"] + 0.5) % 1.0, 0.85, 0.75)
        self._put(p, place, (SHIP, SHIP, SHIP), angles, glass, 0.55, 0.0, 0.0)
        self.canopy.draw(self.gl, p)
        p.release()

    def _draw_trim(self, frame) -> None:
        import colorsys

        scene = frame["scene"]
        p = self._solid(frame)
        place, angles = self._ship_place(frame)
        heat = float(scene._heat()) if hasattr(scene, "_heat") else 0.0
        base = colorsys.hsv_to_rgb(0.55 - heat * 0.45, 0.35 + heat * 0.5, 1.0)
        mix = self._trim
        colour = tuple((b * (1 - mix) + t * mix) * (2.5 + heat * 2.0 + mix * 3)
                       for b, t in zip(base, self._trim_colour))
        if frame["hurt"] > 0.0:
            colour = (colour[0] + frame["hurt"] * 6.0,
                      colour[1] * (1 - frame["hurt"]),
                      colour[2] * (1 - frame["hurt"]))
        self._put(p, place, (SHIP, SHIP, SHIP), angles, colour, 1.0, 0.0, 0.0)
        self.ship_trim.draw(self.gl, p)
        # The engine: a hot core at the tail, and a flame that grows on
        # the beat and with the pace of the passage.
        flame = 0.12 + frame["beat"] * 0.14 + frame["rush"] * 0.20
        engine = colorsys.hsv_to_rgb((frame["hue"] + 0.5) % 1.0, 0.55, 1.0)
        yaw, pitch, roll = angles
        for nx, ny, nz in NOZZLES:
            across, up = nx * SHIP, ny * SHIP
            nozzle = (place[0] + across * math.cos(roll) - up * math.sin(roll),
                      place[1] + across * math.sin(roll) + up * math.cos(roll),
                      place[2] - nz * SHIP)
            self._put(p, nozzle, (0.07, 0.07, flame), angles,
                      tuple(c * 2.0 for c in engine), 1.0, 0.0, 0.0)
            self.cube.draw(self.gl, p)
            self._spawn_exhaust(frame, nozzle, engine)
        p.release()

    def _draw_gates(self, frame) -> None:
        import colorsys

        scene = frame["scene"]
        if scene._beat <= 0.0:
            return
        per = scene.PER_BEAT
        p = self._solid(frame)
        colour = colorsys.hsv_to_rgb(frame["hue"], 0.8, 1.0)
        base = scene._at - scene.RIDER_AT
        first = math.ceil((base + scene.RIDER_AT - 3.0) / per)
        for n in range(first, first + 10):
            z = scene.RIDER_AT + n * per - scene._at
            if z > 46.0:
                break
            if z < scene.RIDER_AT - 2.5:
                continue
            gap = z - scene.RIDER_AT
            if gap >= 0.0:
                lit = 0.12 + 1.4 * math.exp(-gap * 0.45)
            else:
                lit = 4.0 * math.exp(gap * 3.0)
            if abs(gap) < 0.6:
                lit += 5.0 * (1.0 - abs(gap) / 0.6)
            # Every fourth one taller and brighter: the bar, which is
            # the shape a phrase is counted in.
            bar = 1.35 if n % 4 == 0 else 1.0
            lit *= 1.6 if n % 4 == 0 else 1.0
            self._put(p, (0.0, 0.0, z), (bar, bar, 1.0), (0.0, 0.0, 0.0),
                      tuple(c * lit for c in colour), 1.0, 0.0, 0.0)
            self.arch.draw(self.gl, p)
        p.release()

    def _draw_streaks(self, frame) -> None:
        import colorsys

        p = self.streak
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.set("uTravel", float(frame["travel"] * 1.4))
        p.set("uLoop", 70.0)
        p.set("uFrom", float(ROAD_FROM))
        p.set("uLen", float(0.3 + frame["rush"] * 3.0
                                         + self._kick_punch * 1.5))
        colour = colorsys.hsv_to_rgb((frame["hue"] + 0.08) % 1.0, 0.4, 1.0)
        p.set("uColour", QVector3D(*colour))
        p.set("uAmount", float(0.25 + frame["rush"] * 1.2
                                            + self._kick_punch * 0.8))
        self.streaks.draw(self.gl, p)
        p.release()

    # -- particles ------------------------------------------------------------
    def _spawn(self, count, place, speed, colour, size, life, spread=1.0,
               up=1.0, back=0.0) -> None:
        """``count`` points from ``place`` (across, up, along the road's
        own length), flung outwards at ``speed``."""
        rng = self._rng
        data = self._particles
        for _ in range(count):
            angle = rng.random() * math.tau
            lift = rng.random()
            go = speed * (0.4 + rng.random() * 0.6)
            index = self._particle_next * 12
            self._particle_next = (self._particle_next + 1) % self.PARTICLES
            data[index:index + 12] = array("f", (
                place[0], place[1], place[2],
                math.cos(angle) * go * spread,
                (lift * 1.6 - 0.2) * go * up,
                math.sin(angle) * go * spread * 0.6 + back,
                self._now, life * (0.6 + rng.random() * 0.4),
                colour[0], colour[1], colour[2],
                size * (0.6 + rng.random() * 0.8)))
        self._particles_dirty = True

    def _spawn_exhaust(self, frame, place, colour) -> None:
        scene = frame["scene"]
        rng = self._rng
        travel = frame["travel"]
        for _ in range(2):
            self._spawn(1, (place[0] + (rng.random() - 0.5) * 0.04,
                            place[1], place[2] + travel),
                        0.5, tuple(c * 3.0 for c in colour), 0.30, 0.22,
                        spread=0.3, up=0.2, back=-4.0)

    def _draw_particles(self, frame) -> None:
        if self._particles_dirty:
            self._particle_mesh.replace(self._particles)
            self._particles_dirty = False
        p = self.points
        p.bind()
        self._road_uniforms(p, frame)
        p.set("uNow", float(self._now))
        p.set("uTravel", float(frame["travel"]))
        p.set("uScreen", float(self._fbo_size[1] * 0.05))
        self.gl.glEnable(GL_PROGRAM_POINT_SIZE)
        self.gl.glEnable(GL_POINT_SPRITE)
        self._particle_mesh.draw(self.gl, p)
        self.gl.glDisable(GL_POINT_SPRITE)
        self.gl.glDisable(GL_PROGRAM_POINT_SIZE)
        p.release()

    # -- what the game did this frame ------------------------------------------
    def _notice(self, scene) -> None:
        """Which blocks finished this frame, and which the craft took.

        The game marks a block done when its moment passes, taken or
        not. One in the craft's lane when it went was taken.
        """
        self._taken_now = []
        here = float(scene._lane_here)
        alive = set()
        for block in scene._blocks:
            alive.add(id(block))
            if not block[3] or id(block) in self._done_seen:
                continue
            self._done_seen.add(id(block))
            if abs(scene._lane_at(block[1]) - here) < scene.FORGIVE:
                self._taken.add(id(block))
                self._taken_now.append(
                    (block[2], block[4], self._colour_of(scene, block)))
        self._done_seen &= alive
        self._taken &= alive

    def _events(self, scene) -> None:
        import colorsys

        pops = list(getattr(scene, "_pops", ()) or ())
        alive = {id(pop) for pop in pops}
        self._seen_pops &= alive
        travel = float(scene._at)
        at = (float(scene._lane_here), self.HOVER + 0.1,
              scene.RIDER_AT + travel)
        for pop in pops:
            if id(pop) in self._seen_pops:
                continue
            self._seen_pops.add(id(pop))
            kind = pop[0]
            hue = pop[3] if pop[3] is not None else scene._hue_now
            colour = colorsys.hsv_to_rgb(hue % 1.0, min(1.0, pop[4]), 1.0)
            strength = float(pop[2])
            if kind == "hit":
                self._shock, self._shock_hard = 0.0, 1.0
                self._shock_at = self._screen_ship
                self._knock = 1.0
                self._split = 1.0
                self._spawn(110, at, 10.0, (5.0, 0.6, 0.2), 0.45, 0.7)
                self._spawn(40, at, 6.0, (0.9, 0.9, 1.0), 0.6, 0.9)
            elif kind == "shatter":
                self._shock, self._shock_hard = 0.0, 0.6
                self._shock_at = self._screen_ship
                self._split = 0.5
                self._spawn(140, at, 8.0, (1.8, 2.8, 4.0), 0.5, 0.8)
            else:
                if self._taken_now:
                    colour = self._taken_now[-1][2]
                self._flash_lane = float(scene._lane_here)
                self._flash_colour = colour
                self._flash = 1.0
                self._trim = 1.0
                self._trim_colour = colour
                self._bloom_bump = max(self._bloom_bump, 0.5 * strength)
                self._spawn(int(40 + 40 * strength), at, 6.0,
                            tuple(c * 3.0 for c in colour), 0.55, 0.6)
                if kind == "milestone":
                    self._split = 0.4
                    for _ in range(3):
                        sky = (self._rng.uniform(-9.0, 9.0),
                               self._rng.uniform(5.0, 9.0),
                               scene.RIDER_AT + travel
                               + self._rng.uniform(22.0, 34.0))
                        self._spawn(140, sky, 7.0,
                                    tuple(c * 4.0 for c in colour), 2.4, 1.6)

    # -- the polish -------------------------------------------------------------
    def _bloom(self, frame) -> None:
        gl = self.gl
        source = self.resolved
        for index, target in enumerate(self.chain):
            target.bind()
            gl.glViewport(0, 0, target.width(), target.height())
            program = self.bright if index == 0 else self.down
            program.bind()
            gl.glActiveTexture(GL_TEXTURE0)
            gl.glBindTexture(GL_TEXTURE_2D, source.texture())
            program.set("uTex", 0)
            program.set("uTexel", QVector2D(1.0 / source.width(), 1.0 / source.height()))
            if index == 0:
                program.set("uThreshold", 0.9)
            self.quad.draw(gl, program)
            program.release()
            target.release()
            source = target
        # Back up the chain, each level laid over the one above it.
        up = self.up
        below = self.chain[-1]
        for index in range(len(self.chain) - 2, -1, -1):
            target = self.bloomed[index]
            target.bind()
            gl.glViewport(0, 0, target.width(), target.height())
            up.bind()
            gl.glActiveTexture(GL_TEXTURE0)
            gl.glBindTexture(GL_TEXTURE_2D, below.texture())
            up.set("uTex", 0)
            up.set("uTexel", QVector3D(
                1.0 / below.width(), 1.0 / below.height(), 0.0).toVector2D())
            up.set("uSpread", 1.0)
            self.quad.draw(gl, up)
            # Plus this level's own light.
            gl.glEnable(GL_BLEND)
            gl.glBlendFunc(GL_ONE, GL_ONE)
            gl.glBindTexture(GL_TEXTURE_2D, self.chain[index].texture())
            up.set("uTexel", QVector3D(
                1.0 / target.width(), 1.0 / target.height(), 0.0).toVector2D())
            up.set("uSpread", 0.5)
            self.quad.draw(gl, up)
            gl.glDisable(GL_BLEND)
            up.release()
            target.release()
            below = target
        self._bloom_texture = below.texture()

    def _draw_final(self, frame, opacity: float) -> None:
        gl = self.gl
        p = self.final
        p.bind()
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEXTURE_2D, self.resolved.texture())
        p.set("uScene", 0)
        gl.glActiveTexture(GL_TEXTURE0 + 1)
        gl.glBindTexture(GL_TEXTURE_2D, self._bloom_texture)
        p.set("uBloom", 1)
        p.set("uBloomAmount", float(0.55 + self._bloom_bump
                                                 + frame["flash"] * 0.4))
        p.set("uExposure", 1.0)
        p.set("uSplit", float(0.25 + self._split * 3.0
                                           + self._kick_punch * 0.5))
        p.set("uShockAt", QVector3D(self._shock_at[0],
                                                 self._shock_at[1],
                                                 0.0).toVector2D())
        p.set("uShock", float(self._shock))
        p.set("uShockHard", float(self._shock_hard))
        p.set("uAspect", float(frame["aspect"]))
        p.set("uHurt", float(frame["hurt"]))
        p.set("uFlash", float(frame["flash"]))
        p.set("uOpacity", float(opacity))
        p.set("uTime", float(self._now % 100.0))
        p.set("uVignette", 0.55)
        self.quad.draw(gl, p)
        p.release()
        gl.glActiveTexture(GL_TEXTURE0 + 1)
        gl.glBindTexture(GL_TEXTURE_2D, 0)


# ==========================================================================
# What is read rather than seen
# ==========================================================================
class Hud:
    """The numbers, over the world, drawn with the painter.

    The score was a line of small grey text in the corner - "422 chain 13
    clean best 0" - which is a status bar, not a game. Here the score is
    the biggest thing that is not the road, and it counts up rather than
    jumping, so earning is something you watch happen; the chain has a
    place of its own and warms with the run; and how far through the
    track you are runs along the top edge, which is what makes a song
    something you are getting through.
    """

    def __init__(self) -> None:
        self._shown = 0.0
        self._was = 0
        self._bump = 0.0
        self._chain_bump = 0.0
        self._chain_was = 0
        self._now = None

    def draw(self, painter, rect, scene, state) -> None:
        import time as _time

        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QPainterPath, QPen

        now = _time.monotonic()
        dt = 1.0 / 60.0 if self._now is None else max(0.0, min(0.1, now - self._now))
        self._now = now
        worth = int(scene._worth())
        if worth > self._was:
            self._bump = 1.0
        elif worth < self._was:
            self._shown = float(worth)
        self._was = worth
        # Counts up quickly, but visibly: the gap closes by most of itself
        # in a quarter of a second.
        self._shown += (worth - self._shown) * (1.0 - math.exp(-dt * 12.0))
        if abs(worth - self._shown) < 0.5:
            self._shown = float(worth)
        self._bump *= math.exp(-dt * 6.0)
        chain = int(scene._cleared if scene._mode == "Puzzle" else scene._chain)
        if chain > self._chain_was:
            self._chain_bump = 1.0
        self._chain_was = chain
        self._chain_bump *= math.exp(-dt * 7.0)

        hue = float(getattr(scene, "_hue_now", 0.6))
        heat = float(scene._heat()) if hasattr(scene, "_heat") else 0.0
        tall = rect.height()
        painter.save()
        painter.setRenderHint(painter.RenderHint.Antialiasing, True)
        painter.setRenderHint(painter.RenderHint.TextAntialiasing, True)

        # How far through the track.
        contour = getattr(state, "contour", None) or {}
        loud = contour.get("loud") or ()
        rate = float(contour.get("rate") or 0.0)
        if loud and rate > 0.0:
            length = len(loud) / rate
            through = max(0.0, min(1.0, float(state.at) / max(1e-6, length)))
            line = max(2.0, tall * 0.004)
            painter.fillRect(QRectF(rect.left(), rect.top(), rect.width(), line),
                             QColor(255, 255, 255, 34))
            lit = QColor.fromHsvF(hue % 1.0, 0.8, 1.0, 0.9)
            painter.fillRect(QRectF(rect.left(), rect.top(),
                                    rect.width() * through, line), lit)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(255, 255, 255, 230))
            painter.drawEllipse(QPointF(rect.left() + rect.width() * through,
                                        rect.top() + line / 2.0),
                                line * 1.6, line * 1.6)

        def text(where, words, size, colour, align, glow=None, weight=900):
            font = QFont(painter.font())
            font.setPointSizeF(max(8.0, size))
            font.setWeight(QFont.Weight(weight))
            font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 104)
            path = QPainterPath()
            from PySide6.QtGui import QFontMetricsF

            metrics = QFontMetricsF(font)
            wide = metrics.horizontalAdvance(words)
            if align == "centre":
                x = where.x() - wide / 2.0
            elif align == "right":
                x = where.x() - wide
            else:
                x = where.x()
            path.addText(QPointF(x, where.y()), font, words)
            if glow is not None:
                for spread, alpha in ((size * 0.28, 40), (size * 0.14, 70)):
                    painter.setPen(QPen(QColor(glow.red(), glow.green(),
                                               glow.blue(), alpha), spread,
                                        Qt.PenStyle.SolidLine,
                                        Qt.PenCapStyle.RoundCap,
                                        Qt.PenJoinStyle.RoundJoin))
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                    painter.drawPath(path)
            painter.setPen(QPen(QColor(0, 0, 0, 150), max(1.0, size * 0.06)))
            painter.setBrush(colour)
            painter.drawPath(path)

        # The score, top middle.
        size = tall * 0.052 * (1.0 + self._bump * 0.18)
        glow = QColor.fromHsvF(hue % 1.0, 0.7, 1.0)
        text(QPointF(rect.center().x(), rect.top() + tall * 0.085),
             f"{int(round(self._shown)):,}", size, QColor(255, 255, 255),
             "centre", glow)
        if scene._double > 1.0:
            text(QPointF(rect.center().x(), rect.top() + tall * 0.125),
                 "×2 NEXT", tall * 0.022, QColor(255, 240, 200), "centre",
                 QColor(255, 200, 80), weight=800)

        # The chain, bottom left, warming with the run.
        label = "CLEARED" if scene._mode == "Puzzle" else "CHAIN"
        warm = QColor.fromHsvF((0.55 - heat * 0.45) % 1.0, 0.25 + heat * 0.7,
                               1.0)
        left = rect.left() + rect.width() * 0.035
        floor = rect.bottom() - tall * 0.05
        text(QPointF(left, floor - tall * 0.085), label, tall * 0.018,
             QColor(255, 255, 255, 200), "left", None, weight=700)
        text(QPointF(left, floor), f"{chain}",
             tall * 0.07 * (1.0 + self._chain_bump * 0.25), warm, "left",
             warm)
        # How far to the next milestone.
        marks = getattr(scene, "MILESTONES", ())
        if scene._mode != "Puzzle" and marks:
            below = max([0] + [m for m in marks if m <= chain])
            above = next((m for m in marks if m > chain), None)
            if above is not None:
                share = (chain - below) / max(1, above - below)
                bar = QRectF(left, floor + tall * 0.018, tall * 0.16,
                             max(2.0, tall * 0.006))
                painter.fillRect(bar, QColor(255, 255, 255, 40))
                bar.setWidth(bar.width() * share)
                painter.fillRect(bar, warm)

        # Coins and the shield, bottom right.
        right = rect.right() - rect.width() * 0.035
        if scene._coins:
            text(QPointF(right, floor - tall * 0.05),
                 f"● {scene._coins}" + (f"  ×{scene._coin_run}"
                                        if scene._coin_run > 1 else ""),
                 tall * 0.028, QColor(255, 210, 90), "right",
                 QColor(255, 170, 40), weight=800)
        shield = max(0.0, min(1.0, float(scene._shield)))
        bar = QRectF(right - tall * 0.16, floor, tall * 0.16,
                     max(2.0, tall * 0.008))
        painter.fillRect(bar, QColor(255, 255, 255, 38))
        full = shield >= 1.0
        bar.setWidth(bar.width() * shield)
        painter.fillRect(bar, QColor(140, 220, 255, 235 if full else 150))
        text(QPointF(right, floor - tall * 0.012),
             "SHIELD" if full else f"SHIELD {shield:.0%}", tall * 0.016,
             QColor(200, 235, 255, 220 if full else 150), "right", None,
             weight=700)
        if scene._clean and scene._score:
            text(QPointF(rect.center().x(), rect.top() + tall * 0.15),
                 "CLEAN", tall * 0.018, QColor(220, 255, 230, 190), "centre",
                 None, weight=700)
        painter.restore()

    def callout(self, painter, rect, words, hue, through, strength) -> None:
        """A word across the upper middle, for a moment."""
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QColor

        grow = min(1.0, through / 0.08)
        fade = 1.0 if through < 0.55 else max(0.0, 1.0 - (through - 0.55) / 0.45)
        if fade <= 0.0:
            return
        size = rect.height() * 0.085 * (0.6 + 0.4 * grow) * min(1.4, strength)
        size *= 1.0 + (1.0 - grow) * 0.5
        colour = QColor.fromHsvF(hue % 1.0, 0.35, 1.0, fade)
        glow = QColor.fromHsvF(hue % 1.0, 0.9, 1.0, fade)
        painter.save()
        painter.setOpacity(painter.opacity() * fade)
        # Rises a little as it goes, so it reads as leaving.
        y = rect.top() + rect.height() * (0.34 - through * 0.04)
        from PySide6.QtCore import QRectF

        self._text(painter, QPointF(rect.center().x(), y), words, size,
                   colour, glow)
        painter.restore()

    @staticmethod
    def _text(painter, where, words, size, colour, glow) -> None:
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QPainterPath,
                                   QPen)

        font = QFont(painter.font())
        font.setPointSizeF(max(8.0, size))
        font.setWeight(QFont.Weight.Black)
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 108)
        wide = QFontMetricsF(font).horizontalAdvance(words)
        path = QPainterPath()
        path.addText(QPointF(where.x() - wide / 2.0, where.y()), font, words)
        for spread, alpha in ((size * 0.30, 45), (size * 0.15, 80)):
            painter.setPen(QPen(QColor(glow.red(), glow.green(), glow.blue(),
                                       alpha), spread, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)
        painter.setPen(QPen(QColor(0, 0, 0, 170), max(1.0, size * 0.06)))
        painter.setBrush(colour)
        painter.drawPath(path)
