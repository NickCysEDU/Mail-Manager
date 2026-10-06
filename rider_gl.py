"""Music rider, drawn in three dimensions on the graphics card.

The same game as a lit world: a glass track with neon rails, solid blocks
that pulse on the beat, a craft with an engine, a city of equaliser towers,
particles in the air, and a gate over the track on every beat that lights as
you pass. Drawn in floating point, so neon can be far brighter than white
and bloom while the unlit world stays deep and saturated.

Nothing here decides anything: every position comes from the scene's own
road function, sampled once a frame into the vertex shader, so a block is
drawn where the game says it is. The QPainter drawing remains for machines
without a card and for the game's tests.
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

# The few GL enums used, by value
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

#: How many times a frame the road is read, and over what stretch: from behind
#: the camera to well past the last block, so the track runs on into the fog.
ROAD_SAMPLES = 96
ROAD_FROM = -6.0
ROAD_TO = 76.0
ROAD_STEP = (ROAD_TO - ROAD_FROM) / (ROAD_SAMPLES - 1)

#: The levels the towers are lit from.
BANDS = 24

#: How far down the road blocks are drawn. The game lays them five seconds
#: ahead, about sixty units on most records; drawn this far, a block comes out
#: of the fog rather than appearing a few beats away.
SEEN_AHEAD = 56.0

HEADER = """
#version 120
"""

#: Every vertex of the world goes through here, from across, up and along the
#: road to a point in the world, so nothing can disagree about where the road
#: is.
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

// The road's frame at z: across it, up from it and along its line, in
// the road's own space, where z runs ahead.
mat3 roadFrame(float z) {
    vec4 r = roadAt(z);
    vec4 on = roadAt(z + 0.5);
    vec4 back = roadAt(z - 0.5);
    vec3 along = normalize(vec3(on.x - back.x, on.y - back.y, 1.0));
    vec3 across = vec3(cos(r.z), sin(r.z), 0.0);
    across = normalize(across - dot(across, along) * along);
    return mat3(across, cross(along, across), along);
}

// A thing that is one piece: every point of it placed from where the
// thing stands, in the road's frame there, so it is rigid. Through a
// corkscrew the roll changes by a turn in a few dozen units, and a craft
// turned point by point was wrung along its length like a cloth; set
// point by point on the road's line, it bent over every crest. ``local``
// is in the thing's own space, where z runs back.
vec3 onRoadAs(vec3 local, vec3 place) {
    vec4 r = roadAt(place.z);
    vec3 p = vec3(r.x, r.y, place.z) + roadFrame(place.z)
             * vec3(place.x + local.x, place.y + local.y, -local.z);
    return vec3(p.x, p.y, -p.z);
}
""" % {"samples": ROAD_SAMPLES, "last": ROAD_SAMPLES - 1}

#: How many beat starts the shaders get, from behind the eye to past the road's
#: end: a few dozen at the slowest.
BEAT_MARKS = 64

#: The beat number at a point of the road, from the starts of the beats near
#: it: a beat is not one fixed length of road (see Rider._decide_lunges).
BEATS_GLSL = """
uniform float uBeatAt[%(marks)d];
uniform float uBeatFirst;
float beatsAt(float along) {
    float first = uBeatAt[0];
    float last = uBeatAt[%(last)d];
    if (along <= first) {
        return uBeatFirst + (along - first) / max(1e-4, uBeatAt[1] - first);
    }
    if (along >= last) {
        return uBeatFirst + %(last)d.0
               + (along - last) / max(1e-4, last - uBeatAt[%(last)d - 1]);
    }
    int low = 0;
    int high = %(last)d;
    for (int step = 0; step < %(steps)d; step++) {
        int middle = (low + high) / 2;
        if (uBeatAt[middle] <= along) {
            low = middle;
        } else {
            high = middle;
        }
    }
    float start = uBeatAt[low];
    return uBeatFirst + float(low)
           + (along - start) / max(1e-4, uBeatAt[low + 1] - start);
}
""" % {"marks": BEAT_MARKS, "last": BEAT_MARKS - 1,
       "steps": int(math.ceil(math.log2(BEAT_MARKS - 1)))}

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
    """A linked program and its uniforms. Uniforms are set by location, found
    once: PySide matches a name and a float to none of its overloads.
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
    // Mountains along the horizon, dark, their ridges and a few contour
    // lines lit in the passage's colour: the range every synthwave road
    // runs towards. In the sky itself, so it costs nothing and turns with
    // the view.
    float x = q.x * 2.2;
    float ridge = 0.055 * (0.55 * sin(x * 3.1 + 1.3) + 0.3 * sin(x * 7.7 + 0.4)
                           + 0.15 * sin(x * 17.3 + 2.1))
                  + 0.07 * abs(sin(x * 1.3 + 0.8));
    ridge = max(ridge, 0.0) * smoothstep(0.02, 0.35, abs(q.x));
    float inside = step(up, ridge) * step(-0.005, up);
    float line = (1.0 - smoothstep(0.0, 0.0025, abs(up - ridge)))
                 * step(-0.005, up);
    float contour = (1.0 - smoothstep(0.0, 0.08, abs(fract(up / max(ridge, 0.0001)
                    * 4.0) - 0.5) * 2.0 - 0.9)) * inside;
    colour = mix(colour, hsv(uHue + 0.55, 0.6, 0.02), inside * 0.95);
    colour += hsv(uHue, 0.9, 1.0) * (line * (1.2 + uKick * 1.5)
                                     + contour * 0.12);
    // Stars, still - a sky that moves reads as the camera moving.
    vec2 cell = floor(vUv * vec2(uAspect, 1.0) * 180.0);
    float star = step(0.9965, hash(cell));
    float twinkle = 0.55 + 0.45 * sin(uTime * 3.0 + hash(cell + 7.0) * 40.0);
    colour += vec3(0.9, 0.95, 1.0) * star * twinkle * smoothstep(0.02, 0.2, up)
              * (1.2 + uKick) * (1.0 - inside);
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

ROAD_FRAGMENT = HUES_GLSL + FOG_GLSL + BEATS_GLSL + """
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
    float beats = beatsAt(along);
    float quarter = fract(beats * 4.0);
    float fine = 1.0 - smoothstep(0.0, 0.035, min(quarter, 1.0 - quarter));
    colour += hue * fine * 0.10;
    // The beat itself: a bright line across the road every beat, which
    // reaches the craft exactly on it. Lit harder as it gets close, and
    // hardest as it passes under you.
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
    vec3 p = onRoadAs(local, uPlace);
    vec3 n = roadFrame(uPlace.z) * (uTurn * aNormal * vec3(1.0, 1.0, -1.0));
    vNormal = vec3(n.x, n.y, -n.z);
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
uniform float uGlass;        // glass: the canopy
uniform vec3 uEye;           // where the camera is, for what glass reflects
uniform float uClock;        // seconds, for what slides past in it
uniform vec3 uSkyLow;        // the world it reflects: the horizon's colour,
uniform vec3 uSkyHigh;       // the sky's above it,
uniform vec3 uCity;          // and the city's lights
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
    if (uGlass > 0.5) {
        // Glass: the world reflected in it, more of it the more obliquely
        // it is seen (Schlick's Fresnel), over a dim cockpit lit from
        // below by its instruments, with the key light's highlight on top.
        vec3 v = normalize(uEye - vWorld);
        float facing = abs(dot(n, v));
        float fresnel = 0.05 + 0.95 * pow(1.0 - facing, 5.0);
        vec3 r = reflect(-v, n);
        float up = clamp(r.y * 0.5 + 0.5, 0.0, 1.0);
        vec3 sky = mix(uSkyLow, uSkyHigh, smoothstep(0.5, 0.95, up));
        // The city's lights overhead, sliding back over the glass as the
        // craft goes forward under them.
        float lights = pow(max(0.0, sin(r.x * 11.0 + r.z * 5.0
                                         - uClock * 7.0)), 28.0)
                       * smoothstep(0.45, 0.75, up);
        float more = pow(max(0.0, sin(r.x * 23.0 - r.z * 9.0
                                       - uClock * 11.0)), 40.0)
                     * smoothstep(0.55, 0.85, up);
        sky += uCity * (lights * 1.6 + more * 0.9);
        // And the sun on the road ahead, low in the reflection.
        float sun = pow(max(dot(r, normalize(vec3(0.0, 0.12, -1.0))), 0.0), 48.0);
        sky += uSkyLow * sun * 4.0;
        float low = smoothstep(0.30, 0.13, vLocal.y);
        vec3 inside = uColour * (0.10 + 0.45 * low)
                      + uCity * 0.08 * low;
        float shine = pow(max(dot(reflect(-normalize(uLight), n), v), 0.0),
                          80.0);
        colour = mix(inside, sky, fresnel) + vec3(1.0, 0.97, 0.92) * shine * 3.0
                 + uColour * fresnel * 0.25;
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

TOWER_GLSL = """
float towerTall(float level, float reach, float kick) {
    return (1.8 + level * 8.0 + kick * level * 2.5)
           * (0.55 + clamp(reach / 18.0, 0.0, 0.9));
}
// Beside the road's line and upright: a tower takes neither the road's
// bank nor a corkscrew's turn. Turned with the road, the city twisted
// whenever the road did; inside a tunnel it is hidden anyway.
vec3 onRoadUpright(float u, float h, float z) {
    vec4 r = roadAt(z);
    return vec3(r.x + u, r.y + h, -z);
}
"""

TOWER_VERTEX = ROAD_GLSL + TOWER_GLSL + """
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
varying float vStyle;
void main() {
    float level = uLevels[int(aTower.w)];
    vStyle = fract(aTower.y * 0.618 + aTower.z * 3.1);
    float along = mod(aTower.y - uTravel, uLoop) + uFrom;
    float tall = towerTall(level, abs(aTower.x), uKick);
    vec3 local = vec3(aCorner.x * aTower.z, aCorner.y * tall,
                      aCorner.z * aTower.z);
    vec3 p = onRoadUpright(aTower.x + local.x, local.y - 1.2,
                           along + local.z);
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
varying float vStyle;
uniform float uHue;
uniform float uBeat;
void main() {
    vec3 hue = hsv(uHue + 0.5 + (hash(vec2(floor(vTall), 3.0)) - 0.5) * 0.12,
                   0.85, 1.0);
    vec3 colour = vec3(0.012, 0.012, 0.03);
    // Three kinds of tower, by a number of its own: a grid of windows,
    // strips of neon up its corners, or bands of light round it.
    float style = floor(vStyle * 3.0);
    vec2 cell = vec2(vCorner.x + vCorner.z, vCorner.y * vTall * 1.6);
    vec2 f = fract(cell * vec2(4.0, 1.0));
    if (style < 0.5) {
        // Windows, more of them lit the louder the band.
        float window = step(0.25, f.x) * step(f.x, 0.75) * step(0.3, f.y)
                       * step(f.y, 0.7);
        float on = step(hash(floor(cell * vec2(4.0, 1.0)) + floor(vLevel * 6.0)),
                        0.25 + vLevel * 0.6);
        colour += hue * window * on * (0.25 + vLevel * 0.9);
    } else if (style < 1.5) {
        // Neon up the corners, climbing with the band.
        // A corner is where both coordinates reach the edge; on a face
        // one of them always has.
        float edge = 1.0 - smoothstep(0.0, 0.04, 0.5 - min(abs(vCorner.x),
                                                            abs(vCorner.z)));
        float lit = step(vCorner.y, 0.15 + vLevel * 0.95);
        colour += hue * edge * lit * (0.9 + vLevel * 2.0);
    } else {
        // Bands of light round it, one every few floors.
        float band = 1.0 - smoothstep(0.0, 0.06,
                                      abs(fract(vCorner.y * vTall * 0.45) - 0.5));
        colour += hue * band * (0.25 + vLevel * 1.2);
    }
    // The roof line, the brightest thing on it.
    float roof = smoothstep(0.96, 1.0, vCorner.y);
    colour += hue * roof * (0.8 + vLevel * 3.0 + uBeat * 1.5);
    gl_FragColor = vec4(fogged(colour, vDepth), 1.0);
}
"""

BEACON_VERTEX = ROAD_GLSL + TOWER_GLSL + """
attribute vec4 aTower;      // across, along its loop, width, band
uniform mat4 uView;
uniform float uLevels[%(bands)d];
uniform float uTravel;
uniform float uLoop;
uniform float uFrom;
uniform float uKick;
uniform float uScreen;
uniform float uBeat;
varying float vDepth;
varying float vOn;
void main() {
    float level = uLevels[int(aTower.w)];
    float along = mod(aTower.y - uTravel, uLoop) + uFrom;
    float tall = towerTall(level, abs(aTower.x), uKick);
    vec3 p = onRoadUpright(aTower.x, tall - 1.2 + 0.25, along);
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    // One tower in three carries a beacon, and they flash on the beat.
    float has = step(0.66, fract(aTower.y * 1.37 + aTower.z));
    vOn = has * (0.25 + uBeat * 1.8);
    gl_PointSize = has * 0.35 * uScreen / max(vDepth, 0.5);
    gl_Position = eye;
}
""" % {"bands": BANDS}

BEACON_FRAGMENT = FOG_GLSL + """
varying float vDepth;
varying float vOn;
void main() {
    vec2 d = gl_PointCoord - 0.5;
    float glow = exp(-dot(d, d) * 16.0);
    float far = 1.0 - clamp((vDepth - 20.0) / 60.0, 0.0, 1.0);
    gl_FragColor = vec4(vec3(3.0, 0.35, 0.25) * glow * vOn * far, 1.0);
}
"""

BARRIER_VERTEX = ROAD_GLSL + """
attribute vec3 aBar;        // which side, how high (0..1), along
uniform mat4 uView;
uniform float uHalf;
varying float vUp;
varying float vZ;
varying float vDepth;
void main() {
    vec3 p = onRoad(aBar.x * (uHalf + 0.26), aBar.y * 0.34, aBar.z);
    vec4 eye = uView * vec4(p, 1.0);
    vUp = aBar.y;
    vZ = aBar.z;
    vDepth = eye.w;
    gl_Position = eye;
}
"""

BARRIER_FRAGMENT = HUES_GLSL + FOG_GLSL + BEATS_GLSL + """
varying float vUp;
varying float vZ;
varying float vDepth;
uniform float uHue;
uniform float uBeat;
uniform float uTravel;
uniform float uPerBeat;
uniform float uWave;
uniform float uWaveLit;
uniform float uRider;
void main() {
    vec3 hue = hsv(uHue, 0.85, 1.0);
    // Glass: barely there, a little brighter towards its top edge, and
    // the edge itself a line of light.
    float glass = 0.04 + vUp * 0.10;
    float top = 1.0 - smoothstep(0.0, 0.06, 1.0 - vUp);
    // Posts every half beat, lit on the beat.
    float along = beatsAt(vZ + uTravel) * 2.0;
    float post = 1.0 - smoothstep(0.0, 0.03, abs(fract(along) - 0.5));
    // The kick's wave running along it with the one on the road.
    float wave = exp(-pow((vZ - uWave) * 0.9, 2.0)) * uWaveLit;
    float near = exp(-abs(vZ - uRider) * 0.08);
    vec3 colour = hue * (glass + top * (0.9 + uBeat * 1.2)
                         + post * (0.25 + uBeat * 1.0) * near
                         + wave * (1.0 + top * 2.0));
    float far = 1.0 - clamp((vDepth - 14.0) / 58.0, 0.0, 1.0);
    gl_FragColor = vec4(colour * far, 1.0);
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
uniform float uGlow;         // a prize: its colour, in from the edges
uniform vec3 uGlowColour;
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
    // A prize does the opposite: its colour, coming in from the edges and
    // lifting the world where a hit drains it.
    float rim = smoothstep(0.15, 0.95,
                           length((vUv - 0.5) * vec2(1.0, 0.8)) * 1.6);
    c += uGlowColour * uGlow * (0.10 + rim * 0.55);
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

#: The tunnel a corkscrew is ridden through: a tube round the road, turning
#: with it, in the passage's colours and the rest of the spectrum. See
#: World._draw_tunnel.
TUNNEL_VERTEX = ROAD_GLSL + """
attribute vec2 aTube;       // angle round the road, and how far along it
uniform mat4 uView;
uniform float uTunnel[%(samples)d];
uniform float uRadius;
uniform float uCentre;
varying float vAngle;
varying float vAlong;
varying float vHere;
varying float vDepth;
float tunnelAt(float z) {
    float f = clamp((z - uRoadFrom) / uRoadStep, 0.0, %(last)d.0 - 0.001);
    int i = int(floor(f));
    return mix(uTunnel[i], uTunnel[i + 1], f - float(i));
}
void main() {
    float z = aTube.y;
    vHere = tunnelAt(z);
    // Wider where it is only beginning: a mouth, opening out of the city
    // ahead, rather than a pipe that is suddenly there.
    float r = uRadius * (1.0 + (1.0 - vHere) * 1.8);
    vec3 p = onRoad(r * cos(aTube.x), uCentre + r * sin(aTube.x), z);
    vec4 eye = uView * vec4(p, 1.0);
    vDepth = eye.w;
    vAngle = aTube.x;
    vAlong = z;
    gl_Position = eye;
}
""" % {"samples": ROAD_SAMPLES, "last": ROAD_SAMPLES - 1}

TUNNEL_FRAGMENT = HUES_GLSL + FOG_GLSL + BEATS_GLSL + """
varying float vAngle;
varying float vAlong;
varying float vHere;
varying float vDepth;
uniform float uTravel;
uniform float uTime;
uniform float uHue;
uniform float uBeat;
uniform float uKick;
uniform float uPerBeat;
uniform float uRider;
void main() {
    if (vHere < 0.01) discard;
    // Fixed to the road, so it streams past at the road's own speed.
    float along = vAlong + uTravel;
    float spiral = sin(6.0 * vAngle + along * 0.35 - uTime * 3.0);
    float weave = sin(11.0 * vAngle - along * 0.9 + uTime * 1.7);
    float hue = uHue + vAngle / 6.28318 + along * 0.021 + uTime * 0.07;
    vec3 colour = hsv(hue, 0.92, 0.18 + 0.30 * (spiral * 0.5 + 0.5));
    colour += hsv(hue + 0.33, 1.0, 0.30 * max(0.0, weave));
    // A ring of light at every beat, as the gates are: they pass the
    // craft on the beat, so the beat is still there to be seen in here.
    float beat = fract(beatsAt(vAlong - uRider + uTravel));
    float ring = exp(-min(beat, 1.0 - beat) * uPerBeat * 3.0);
    colour += hsv(hue + 0.5, 0.55, 1.0) * ring * (0.9 + uBeat * 2.2);
    colour *= 0.85 + uKick * 0.9;
    float fade = smoothstep(0.0, 1.0, vHere);
    gl_FragColor = vec4(fogged(colour, vDepth), fade);
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


#: The craft in its own frame (across, up, along, nose at -z, as the road runs
#: away from the camera), lofted from cross-sections along its length like a
#: hull.

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


#: The craft each level is flown in: hull width, height and length against the
#: first, wing reach and sweep, engine placement and count, and flame length.
#: The faster the road runs (see rider_layout.DIFFICULTY), the longer, thinner
#: and more swept the craft and the longer its flame.
CRAFTS = {
    "Cruiser": {"wide": 1.30, "tall": 1.10, "long": 0.86, "span": 1.08,
                "sweep": -0.04, "pods": 0.44, "engines": 2, "fins": True,
                "canopy": 1.20, "flame": 0.8, "size": 1.0},
    "Arrow": {"wide": 1.0, "tall": 1.0, "long": 1.0, "span": 1.0,
              "sweep": 0.0, "pods": NACELLE_X, "engines": 2, "fins": True,
              "canopy": 1.0, "flame": 1.0, "size": 1.0},
    "Interceptor": {"wide": 0.84, "tall": 0.90, "long": 1.18, "span": 1.02,
                    "sweep": 0.14, "pods": 0.31, "engines": 2, "fins": True,
                    "canopy": 0.92, "flame": 1.35, "size": 0.95},
    "Needle": {"wide": 0.62, "tall": 0.82, "long": 1.42, "span": 0.70,
               "sweep": 0.24, "pods": 0.25, "engines": 3, "fins": False,
               "canopy": 0.78, "flame": 1.5, "size": 0.80},
}
#: How much of a longer craft's extra length goes behind its middle. Stretched
#: evenly, the fastest craft's tail came at the camera and hid the most road at
#: the level with the least warning.
TAIL_SHARE = 0.25
#: Which craft each level flies. See Rider.difficulty.
CRAFT_FOR = {"Easy": "Cruiser", "Normal": "Arrow", "Hard": "Interceptor",
             "Expert": "Needle"}


def craft(name: str) -> dict:
    """A craft's shape, or the first one's for a name there is none of."""
    return CRAFTS.get(name, CRAFTS["Arrow"])


def _along(z: float, spec) -> float:
    """A point's place along a craft: stretched by its length in front of
    the middle, and by a share of it behind (see TAIL_SHARE)."""
    if z <= 0.0:
        return z * spec["long"]
    return z * (1.0 + (spec["long"] - 1.0) * TAIL_SHARE)


def _scaled(sections, spec, width=1.0):
    """Lofting sections stretched to a craft: across by its width, up by
    its height, along by its length."""
    return [(_along(z, spec), [(x * spec["wide"] * width, y * spec["tall"])
                               for x, y in ring])
            for z, ring in sections]


def _wing(side: float, spec=None) -> list:
    """A swept slab from the hull out past the engine, thin."""
    spec = spec or CRAFTS["Arrow"]
    t = 0.018
    span, sweep = spec["span"], spec["sweep"]
    root = 0.12 * spec["wide"]
    root_front = (side * root, 0.05, _along(-0.30, spec))
    root_back = (side * root, 0.05, _along(0.40, spec))
    tip_back = (side * 0.62 * span, 0.02, _along(0.50 + sweep, spec))
    tip_front = (side * 0.58 * span, 0.02, _along(0.30 + sweep, spec))
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


def _fin(side: float, spec=None) -> list:
    """A small upright fin on each engine."""
    spec = spec or CRAFTS["Arrow"]
    x = side * spec["pods"]
    base_front = (x, 0.12, _along(0.20, spec))
    base_back = (x, 0.12, _along(0.60, spec))
    top_back = (x + side * 0.03, 0.30, _along(0.62, spec))
    t = 0.012
    tri = [base_front, base_back, top_back]
    left = [(px - t, py, pz) for px, py, pz in tri]
    right = [(px + t, py, pz) for px, py, pz in tri]
    return left + right[::-1] + [left[0], right[0], left[1],
                                 left[1], right[0], right[1]]


def _pods(spec) -> list:
    """Where the engines are, across and up, in the craft's frame: one
    each side, and the third down the middle under the tail."""
    out = [(-spec["pods"], NACELLE_Y), (spec["pods"], NACELLE_Y)]
    if spec["engines"] >= 3:
        out.append((0.0, NACELLE_Y - 0.02))
    return out


def nozzles(name: str = "Arrow") -> tuple:
    """Where each engine's flame comes out, in the craft's frame."""
    spec = craft(name)
    return tuple((x, y, _along(0.64, spec)) for x, y in _pods(spec))


#: The first craft's, kept for anything that asks for them by name.
NOZZLES = nozzles("Arrow")


def ship_triangles(name: str = "Arrow"):
    """A craft's body: hull, wings, engines and fins, in its metal."""
    spec = craft(name)
    out = _loft(_scaled(HULL, spec))
    pod = _scaled(NACELLE, dict(spec, wide=1.0, tall=1.0))
    for across, up in _pods(spec):
        out += _shifted(_loft(pod), across, up)
    for side in (-1.0, 1.0):
        out += _wing(side, spec)
        if spec["fins"]:
            out += _fin(side, spec)
    return out


def canopy_triangles(name: str = "Arrow"):
    spec = craft(name)
    return _loft(_scaled(CANOPY, spec, spec["canopy"]))


def ship_outline(name: str = "Arrow"):
    """The lines a craft's neon trim is drawn along."""
    spec = craft(name)
    hull = _scaled(HULL, spec)
    span, sweep = spec["span"], spec["sweep"]
    pairs = []
    # Down the spine and along each flank at the waist.
    for index in range(len(hull) - 1):
        z0, ring0 = hull[index]
        z1, ring1 = hull[index + 1]
        for k in (0, len(ring0) // 2):
            pairs += [(ring0[k][0], ring0[k][1], z0),
                      (ring1[k][0], ring1[k][1], z1)]
    root = 0.12 * spec["wide"]
    for side in (-1.0, 1.0):
        # The wings' leading and trailing edges.
        front = (side * 0.58 * span, 0.04, _along(0.30 + sweep, spec))
        back = (side * 0.62 * span, 0.04, _along(0.50 + sweep, spec))
        pairs += [(side * root, 0.07, _along(-0.30, spec)), front, front,
                  back, back, (side * root, 0.07, _along(0.40, spec))]
    # A ring round each intake and each nozzle.
    pod = _scaled(NACELLE, dict(spec, wide=1.0, tall=1.0))
    for across, up in _pods(spec):
        for z, ring in (pod[1], pod[-1]):
            for i in range(len(ring)):
                j = (i + 1) % len(ring)
                pairs += [(across + ring[i][0], up + ring[i][1], z),
                          (across + ring[j][0], up + ring[j][1], z)]
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


def tube_floats(rings=150, around=36) -> list:
    """The tunnel as triangles: (angle round, distance along) at every
    corner, from just behind the craft to as far as is seen."""
    out = []
    step = (SEEN_AHEAD - ROAD_FROM) / rings
    for ring in range(rings):
        z0 = ROAD_FROM + ring * step
        z1 = z0 + step
        for side in range(around):
            a0 = side / around * math.tau
            a1 = (side + 1) / around * math.tau
            out += [a0, z0, a1, z0, a1, z1,
                    a0, z0, a1, z1, a0, z1]
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


def barrier_floats() -> list:
    """Both sides of the road: a strip, (side, up, along) per corner."""
    along = []
    z = ROAD_FROM + 0.5
    while z < ROAD_TO - 0.5:
        along.append(z)
        z += 0.25 if z < 12 else 0.5 if z < 30 else 1.0
    out = []
    for side in (-1.0, 1.0):
        for z0, z1 in zip(along, along[1:]):
            out.extend((side, 0.0, z0, side, 1.0, z0, side, 1.0, z1,
                        side, 0.0, z0, side, 1.0, z1, side, 0.0, z1))
    return out


def beacon_floats(half: float) -> list:
    """One point per tower, at its roof: the towers' own numbers."""
    towers = tower_floats(half)
    stride = 7 * 36
    out = []
    for index in range(0, len(towers), stride):
        out.extend(towers[index + 3:index + 7])
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


def _unit(vector: tuple) -> tuple:
    length = math.sqrt(sum(part * part for part in vector)) or 1.0
    return tuple(part / length for part in vector)


class RiderWorld:
    """Everything the rider draws on the card, for one GL context: made on the
    first frame in a context and dropped with it (a move into full screen
    can bring a new one; see Spectrum's canvas).
    """

    #: Where the camera rides: behind and above the craft, looking this far
    #: down the road.
    CAM_BACK = 2.45
    CAM_UP = 1.02
    CAM_AHEAD = 6.5
    CAM_LOOK_UP = 0.1
    #: How the camera follows, as a share of the way a second.
    CAM_FOLLOW = 9.0
    #: How much of the road's bank the camera takes on: all of it tips the
    #: world at every turn, none of it bolts the camera level to a road that is
    #: not.
    CAM_BANK = 0.45
    #: The field of view, calm and at full tilt, in degrees.
    FOV_CALM = 62.0
    FOV_FAST = 74.0
    #: How many degrees a prize opens the view by, at full strength.
    PUNCH = 7.0
    #: How far the craft hovers.
    HOVER = 0.28
    #: The least the eye may be above the road at any point between it and
    #: the craft: over a crest, or where a corkscrew has turned the road
    #: behind the craft further than the craft, the road was through the
    #: camera.
    CAM_CLEAR = 0.45

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
        self.beacon = _program(BEACON_VERTEX, BEACON_FRAGMENT)
        self.barrier = _program(BARRIER_VERTEX, BARRIER_FRAGMENT)
        self.tunnel = _program(TUNNEL_VERTEX, TUNNEL_FRAGMENT)
        self.tube = _Mesh(tube_floats(), [(b"aTube", 2)])
        #: How much tunnel there is at each road sample and at the craft. See
        #: _tunnel_at.
        self.tunnel_line = [0.0] * ROAD_SAMPLES
        self.inside = 0.0
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
        #: A prize taken: its colour in from the picture's edges and the camera
        #: punched forward; each adds, so a quick run builds.
        self._glow = 0.0
        self._glow_colour = (1.0, 1.0, 1.0)
        self._punch = 0.0
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

    def _build(self, half: float) -> None:
        if self._half == half:
            return
        self._half = half
        self.road_mesh = _Mesh(road_floats(half), [(b"aRoad", 2)])
        cube = _flat_normals(cube_triangles())
        self.cube = _Mesh(cube, [(b"aPos", 3), (b"aNormal", 3)])
        self._crafts = {}
        self.ship, self.canopy, self.ship_trim = self._craft_meshes("Arrow")
        self.coin = _Mesh(_flat_normals(disc_triangles()),
                          [(b"aPos", 3), (b"aNormal", 3)])
        self.arch = _Mesh(_flat_normals(arch_triangles(half)),
                          [(b"aPos", 3), (b"aNormal", 3)])
        self.city = _Mesh(tower_floats(half), [(b"aCorner", 3), (b"aTower", 4)])
        self.beacons = _Mesh(beacon_floats(half), [(b"aTower", 4)], GL_POINTS)
        self.barriers = _Mesh(barrier_floats(), [(b"aBar", 3)])
        self.streaks = _Mesh(streak_floats(half), [(b"aStreak", 4)], GL_LINES)

    #: The world's light in packed floating point (eleven bits red and green,
    #: ten blue): a third of the memory of half floats, which a multisampled
    #: full screen needs. Falls back to half floats, then bytes.
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

    #: How much of the road's roll is drawn as its bank, and which way: the
    #: scene measures roll for a picture whose y runs down.
    ROLL_SHARE = -0.5

    def _read_road(self, scene) -> list:
        """The road's line as the shaders read it: per sample, across, up,
        roll, and how much of the roll is a corkscrew (see _turn_at)."""
        side, under = scene._side, scene._under
        out = []
        tunnel = []
        twisted = bool(getattr(scene, "_twists", None))
        for index in range(ROAD_SAMPLES):
            z = ROAD_FROM + index * ROAD_STEP
            across, lift, roll = scene._road(z)
            turn = self._twist(scene, z)
            out.extend((across - side, -(lift - under),
                        roll * self.ROLL_SHARE + turn, turn))
            tunnel.append(scene._tunnel_at(scene._when(z))
                          if twisted else 0.0)
        self.tunnel_line = tunnel
        self.inside = (scene._tunnel_at(scene._when(scene.RIDER_AT))
                       if twisted else 0.0)
        return out

    @staticmethod
    def _twist(scene, z: float) -> float:
        """How far a corkscrew has turned the road at ``z``, in radians: the
        turn due at that point's moment of the track; behind the craft, the
        craft's. The road itself turns over, so the corkscrew is seen coming
        and the craft rides it, staying put on the screen.
        """
        twist_at = getattr(scene, "_twist_at", None)
        if twist_at is None or not getattr(scene, "_twists", None):
            return 0.0
        when = scene._when(max(z, scene.RIDER_AT))
        # Counted on through the corkscrews already ridden rather than back to
        # zero: a whole turn is level, and samples either side of an ending
        # must not differ by a turn, or the road between them folds.
        done = sum(1 for start in scene._twists
                   if start + scene.TWIST_FOR <= when)
        through = twist_at(when)
        part = 0.0 if through is None else scene._turned(through)
        return (done + part) * math.tau

    @staticmethod
    def _sample(road, z: float, part: int) -> float:
        f = max(0.0, min((z - ROAD_FROM) / ROAD_STEP, ROAD_SAMPLES - 1.001))
        i = int(f)
        t = f - i
        a, b = road[i * 4 + part], road[i * 4 + 4 + part]
        return a + (b - a) * t

    @classmethod
    def _turn_at(cls, road, z: float) -> float:
        """The corkscrew's share of the road's roll at ``z``."""
        return cls._sample(road, z, 3)

    @classmethod
    def _on_road(cls, road, u: float, h: float, z: float,
                 plain: bool = False) -> tuple:
        """The shader's onRoad in Python, for the camera and the HUD. ``plain``
        leaves out any corkscrew."""
        x, y = cls._sample(road, z, 0), cls._sample(road, z, 1)
        r = cls._sample(road, z, 2)
        if plain:
            r -= cls._turn_at(road, z)
        c, s = math.cos(r), math.sin(r)
        return (x + u * c - h * s, y + u * s + h * c, -z)

    @staticmethod
    def _about(point, centre, angle: float) -> tuple:
        """``point`` turned by ``angle`` about the road's line through
        ``centre``, the way onRoad turns a point with the road's roll."""
        dx, dy = point[0] - centre[0], point[1] - centre[1]
        c, s = math.cos(angle), math.sin(angle)
        return (centre[0] + dx * c - dy * s, centre[1] + dx * s + dy * c,
                point[2])

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
        self._draw_tunnel(frame)
        # What glows, over the top, adding light and hiding nothing.
        gl.glEnable(GL_BLEND)
        gl.glBlendFunc(GL_ONE, GL_ONE)
        gl.glDepthMask(0)
        self._draw_barriers(frame)
        self._draw_beacons(frame)
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

    def _frame(self, scene, state, dt: float, aspect: float) -> dict:
        import colorsys

        road = self._read_road(scene)
        kit = state.kit or {}
        kick = float(kit.get("Kick", 0.0) or 0.0)
        beat = float(getattr(scene, "_beat_lit", 0.0))
        if not kit:
            kick = beat
        # A kick is a moment, not a level: on the way up through the middle the
        # wave goes out and the view punches wider.
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
        self._glow *= math.exp(-dt * 4.5)
        self._punch *= math.exp(-dt * 5.5)

        loud = float(getattr(scene, "_loudness", 0.0))
        rush = max(0.0, min(1.0, float(getattr(scene, "_rushing", 0.0))))
        hurt = float(getattr(scene, "_hurt", 0.0))
        hue = float(getattr(scene, "_hue_now", 0.6))
        air = float(getattr(scene, "_air", 0.0))
        across = float(scene._lane_here)

        # The camera on a spring, following the craft, placed as if the road
        # were not turning over and then turned with it below.
        ship_z = scene.RIDER_AT
        half = scene.LANE_WIDE * scene.LANES / 2.0
        eye = self._on_road(road, across * 0.35,
                            self.CAM_UP + air * 0.4 + self._knock * 0.5,
                            ship_z - self.CAM_BACK - self._knock * 0.8,
                            plain=True)
        look = self._on_road(road, across * 0.55, self.CAM_LOOK_UP + air * 0.3,
                             ship_z + self.CAM_AHEAD, plain=True)
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
        # Through a corkscrew the whole rig (eye, aim and up) turns about the
        # road at the craft by the road's turn there, so the craft and the road
        # under it stay put and the world goes round them.
        turn = self._turn_at(road, ship_z)
        centre = self._on_road(road, 0.0, 0.0, ship_z)
        eye_at = self._about(eye_at, centre, turn)
        eye_at = self._clear_of_road(road, eye_at, ship_z, half)
        look_at = self._about(self._cam_look, centre, turn)
        index = int((ship_z - ROAD_FROM) / ROAD_STEP) * 4
        bank = road[index + 2] - road[index + 3]
        roll = (bank * self.CAM_BANK - turn
                + math.sin(float(getattr(scene, "_wobble", 0.0)) * 2.3)
                * hurt * 0.10)
        fov = (self.FOV_CALM + (self.FOV_FAST - self.FOV_CALM) * rush
               + self._kick_punch * 3.0 + self._punch * self.PUNCH)
        view = QMatrix4x4()
        view.perspective(fov / max(1.0, aspect / 1.6) ** 0.35, aspect,
                         0.05, 400.0)
        view.lookAt(QVector3D(*eye_at), QVector3D(*look_at),
                    QVector3D(math.sin(roll), math.cos(roll), 0.0))

        def screen(point):
            v = view.map(QVector3D(*point))
            return (v.x() * 0.5 + 0.5, v.y() * 0.5 + 0.5)

        ship_at = self._rigid(road, (across, self.HOVER + air, ship_z),
                              (0.0, 0.0, 0.0))
        self._screen_ship = screen(ship_at)
        horizon = screen(self._on_road(road, 0.0, 0.0, ROAD_TO - 2.0))
        # Where the sky is, not the road: it stays put as the road turns over
        # under it.
        sun = screen(self._on_road(road, 0.0, 6.0, ROAD_TO - 2.0, plain=True))
        # The horizon's colour, so the road runs into the sky.
        fog = colorsys.hsv_to_rgb(hue % 1.0, 0.95, 0.20 + loud * 0.18)

        #: What the camera did this frame, for anybody measuring it: roll in
        #: radians, field of view in degrees, and where the horizon and the
        #: craft landed, 0 to 1.
        self.seen = {"roll": roll, "fov": fov, "horizon": horizon,
                     "ship": self._screen_ship, "eye": eye_at,
                     "road": screen(self._on_road(road, 0.0, 0.0, 12.0))}
        levels = list(getattr(state, "levels", ()) or ())
        if levels:
            bands = [max(0.0, min(1.0, float(levels[min(
                len(levels) - 1, index * len(levels) // BANDS)])))
                for index in range(BANDS)]
        else:
            bands = [0.0] * BANDS
        travel = float(scene._at)
        per_beat = 1e6
        marks_first = 0.0
        marks = [index * 1e6 for index in range(BEAT_MARKS)]
        if scene._clock:
            here = scene._beat_number(scene._heard) or 0
            per_beat = scene.PER_BEAT * scene._pace_of(here)
            first = scene._beat_at_road(
                travel - scene.RIDER_AT + ROAD_FROM) - 1
            marks_first = float(first)
            marks = [scene.beat_on_road(first + index)
                     for index in range(BEAT_MARKS)]
        return {
            "road": road, "view": view, "hue": hue, "loud": loud,
            "rush": rush, "hurt": hurt, "beat": beat, "kick": kick,
            "air": air, "across": across, "aspect": aspect, "roll": roll,
            "horizon": horizon, "sun": sun, "fog": fog, "bands": bands,
            "travel": travel, "per_beat": per_beat, "scene": scene,
            "marks": marks, "marks_first": marks_first,
            "flash": float(scene.flash(state)) if hasattr(scene, "flash")
            else 0.0,
            "half": half,
            "pixels": 1.0,
            "eye": eye_at,
        }

    @classmethod
    def _clear_of_road(cls, road, eye, ship_z: float, half: float) -> tuple:
        """``eye`` lifted, along the road's up at the craft, until it is
        CAM_CLEAR above the road's surface at every sample between it and
        the craft. Beside the road there is nothing under it to clear."""
        z_eye = -eye[2]
        up = cls._sample(road, ship_z, 2)
        # Twice: the samples' frames differ from the craft's by a little
        # through a corkscrew, so one lift along the craft's up leaves a
        # hair.
        for _ in range(2):
            short = 0.0
            for step in range(9):
                z = z_eye + (ship_z - z_eye) * step / 8.0
                dx = eye[0] - cls._sample(road, z, 0)
                dy = eye[1] - cls._sample(road, z, 1)
                r = cls._sample(road, z, 2)
                if abs(dx * math.cos(r) + dy * math.sin(r)) > half + 1.0:
                    continue
                short = max(short, cls.CAM_CLEAR + dx * math.sin(r)
                            - dy * math.cos(r))
            if short <= 0.0:
                break
            eye = (eye[0] - math.sin(up) * short,
                   eye[1] + math.cos(up) * short, eye[2])
        return eye

    @classmethod
    def _road_frame(cls, road, z: float) -> tuple:
        """The shader's roadFrame in Python: across, up and along at ``z``,
        in the road's own space."""
        on = (cls._sample(road, z + 0.5, 0), cls._sample(road, z + 0.5, 1))
        back = (cls._sample(road, z - 0.5, 0), cls._sample(road, z - 0.5, 1))
        along = _unit((on[0] - back[0], on[1] - back[1], 1.0))
        r = cls._sample(road, z, 2)
        across = (math.cos(r), math.sin(r), 0.0)
        lean = sum(a * b for a, b in zip(across, along))
        across = _unit(tuple(a - lean * b for a, b in zip(across, along)))
        up = (along[1] * across[2] - along[2] * across[1],
              along[2] * across[0] - along[0] * across[2],
              along[0] * across[1] - along[1] * across[0])
        return across, up, along

    @classmethod
    def _rigid(cls, road, place, local) -> tuple:
        """The shader's onRoadAs in Python: a point ``local`` of a rigid thing
        standing at ``place`` (across, up, along), in the world."""
        across, up, along = cls._road_frame(road, place[2])
        centre = (cls._sample(road, place[2], 0),
                  cls._sample(road, place[2], 1), place[2])
        u, h, a = place[0] + local[0], place[1] + local[1], -local[2]
        x, y, z = (c + u * i + h * j + a * k
                   for c, i, j, k in zip(centre, across, up, along))
        return (x, y, -z)

    @staticmethod
    def _beat_uniforms(program, frame) -> None:
        """Where the beats start on the road, for BEATS_GLSL."""
        program.array("uBeatAt", frame["marks"], BEAT_MARKS, 1)
        program.set("uBeatFirst", float(frame["marks_first"]))

    def _road_uniforms(self, program, frame) -> None:
        program.array("uRoad", frame["road"], ROAD_SAMPLES, 4)
        program.set("uRoadFrom", float(ROAD_FROM))
        program.set("uRoadStep", float(ROAD_STEP))
        program.set("uView", frame["view"])

    def _fog_uniforms(self, program, frame) -> None:
        program.set("uFog", QVector3D(*frame["fog"]))
        program.set("uFogFrom", 16.0)
        program.set("uFogTo", 72.0)

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

    def _draw_tunnel(self, frame) -> None:
        """The tunnel round a corkscrew, where there is one: solid where it is
        all there, hiding the city; the mouth faded in."""
        if max(self.tunnel_line) < 0.01:
            return
        scene = frame["scene"]
        gl = self.gl
        p = self.tunnel
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.array("uTunnel", self.tunnel_line, ROAD_SAMPLES, 1)
        p.set("uRadius", float(frame["half"] + scene.TUNNEL_ROOM))
        p.set("uCentre", float(scene.TUNNEL_MIDDLE))
        p.set("uTravel", float(frame["travel"]))
        p.set("uTime", float(self._now % 1000.0))
        p.set("uHue", float(frame["hue"]))
        p.set("uBeat", float(frame["beat"]))
        p.set("uKick", float(self._kick_punch))
        p.set("uPerBeat", float(frame["per_beat"]))
        self._beat_uniforms(p, frame)
        p.set("uRider", float(scene.RIDER_AT))
        gl.glEnable(GL_BLEND)
        gl.glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
        self.tube.draw(gl, p)
        gl.glDisable(GL_BLEND)
        p.release()

    def _draw_road(self, frame) -> None:
        scene = frame["scene"]
        p = self.road
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.set("uHalf", float(frame["half"]))
        p.set("uLane", float(scene.LANE_WIDE))
        p.set("uTravel", float(frame["travel"] - scene.RIDER_AT))
        p.set("uPerBeat", float(frame["per_beat"]))
        self._beat_uniforms(p, frame)
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
        p.set("uGlass", 0.0)
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
        scene = frame["scene"]
        p = self._solid(frame)
        beat = frame["beat"]
        spin = float(getattr(scene, "_coin_spin", 0.0))
        far = SEEN_AHEAD
        for block in scene._blocks:
            when, lane, kind, done, grey = block
            z = scene._where(when)
            if done and id(block) in self._taken:
                # Taken: it went into the ship. One not taken goes on past and
                # out of the picture. See _notice.
                continue
            if z < scene.RIDER_AT - 4.0 or z > far + 0.5:
                continue
            # Grown in out of the fog over the last stretch, rather than
            # appearing.
            grow = max(0.0, min(1.0, (far + 0.5 - z) / 6.0))
            grow = grow * grow * (3.0 - 2.0 * grow)
            across = scene._lane_at(lane)
            # The last stretch before the craft, in its lane: drawn in and
            # shrinking, so a block taken is seen going into the ship. Not an
            # obstacle, which is hit (see _rammed), and not under a craft in
            # the air, which takes nothing.
            gap = z - scene.RIDER_AT
            beside = abs(across - frame["across"])
            flying = frame["air"] > 0.05
            if (not done and not grey and not flying and gap < 0.9
                    and beside < scene.LANE_WIDE * 0.5):
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
            # A prize is lit from inside in its passage's colour; an obstacle
            # is dark metal with a warning.
            pulse = 1.0 + beat * (0.10 if grey else 0.16)
            wide = scene.LANE_WIDE * 0.50 * grow * pulse
            tall = 0.46 * grow * pulse
            if grey:
                place = (across, tall * 0.62, z)
                size = (wide * 0.95, tall * 1.15, wide * 0.95)
                yaw = 0.785
                if (not done and not flying
                        and float(getattr(scene, "_sore", 0.0)) <= 0.0):
                    place, size, yaw = self._rammed(
                        place, size, yaw, gap, beside, scene)
                self._put(p, place, size, (yaw, 0.0, 0.0), (0.9, 0.2, 0.15),
                          0.0, 2.2 + beat * 3.0, 1.0)
            else:
                colour = self._colour_of(scene, block)
                # Colour in the faces and light in the edges: lit brightly all
                # over, tone mapping turned the blocks milky.
                glow = 0.85 + beat * 0.55
                if done:
                    # Gone past: darkening as it leaves, so a miss does not
                    # smear light across the frame's edge.
                    glow *= max(0.0, 1.0 + gap / 1.5)
                self._put(p, (across, tall * 0.5 + 0.02, z),
                          (wide, tall, wide * 0.8), (0.0, 0.0, 0.0),
                          tuple(c * glow for c in colour), 0.7, 3.6, 0.0)
            self.cube.draw(self.gl, p)
        p.release()

    #: How far the craft's nose reaches ahead of the point it is judged at.
    NOSE = 1.10 * SHIP

    def _rammed(self, place, size, yaw, gap, beside, scene):
        """An obstacle about to be hit, flattened on the craft's nose. A hit is
        judged at the craft's middle, on the beat, and the nose gets there
        first: drawn whole the block sat inside the hull, and drawn
        shrinking it looked taken. Here it stops at the nose, turned square
        on and squashed wider and taller, so the hit is seen landing and the
        burst comes on the beat. ``place`` is (across, up, along), ``size``
        the cube's scale, ``yaw`` its turn; all three are returned changed.
        """
        # How squarely it is in the way: all of it in the craft's path, none a
        # lane over, in between as the craft slides.
        half_lane = scene.LANE_WIDE * 0.5
        square = max(0.0, min(1.0, (half_lane - beside) / (half_lane * 0.3)))
        if square <= 0.0:
            return place, size, yaw

        def reach(turn, scale):
            return 0.5 * (abs(math.sin(turn)) * scale[0]
                          + abs(math.cos(turn)) * scale[2])

        contact = self.NOSE + reach(yaw, size)
        if gap >= contact:
            return place, size, yaw
        crush = max(0.0, min(1.0, 1.0 - gap / contact)) * square
        turn = yaw * (1.0 - crush)
        scale = (size[0] * (1.0 + 0.30 * crush),
                 size[1] * (1.0 + 0.15 * crush),
                 size[2] * (1.0 - 0.80 * crush))
        # Its near face held at the nose however far the road has run on.
        pinned = max(place[2], scene.RIDER_AT + self.NOSE + reach(turn, scale))
        along = place[2] + (pinned - place[2]) * square
        # Standing on the road still as it grows taller.
        up = place[1] * scale[1] / max(1e-6, size[1])
        return (place[0], up, along), scale, turn

    @staticmethod
    def _colour_of(scene, block) -> tuple:
        """What a block is lit in: its passage's colour, gold for a coin, white
        for a power block, red for an obstacle."""
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

    def _craft_meshes(self, name: str) -> tuple:
        """A craft's body, canopy and trim, made the first time it flies on
        this card."""
        found = self._crafts.get(name)
        if found is None:
            found = self._crafts[name] = (
                _Mesh(_flat_normals(ship_triangles(name)),
                      [(b"aPos", 3), (b"aNormal", 3)]),
                _Mesh(_flat_normals(canopy_triangles(name)),
                      [(b"aPos", 3), (b"aNormal", 3)]),
                _Mesh(ship_outline(name), [(b"aPos", 3), (b"aNormal", 3)],
                      GL_LINES))
        return found

    @staticmethod
    def craft_of(scene) -> str:
        """The craft the scene's level is flown in. See CRAFT_FOR."""
        return CRAFT_FOR.get(getattr(scene, "difficulty", "Normal"), "Arrow")

    def _draw_ship(self, frame) -> None:
        import colorsys

        self.ship, self.canopy, self.ship_trim = self._craft_meshes(
            self.craft_of(frame["scene"]))
        p = self._solid(frame)
        place, angles = self._ship_place(frame)
        hurt = frame["hurt"]
        size = SHIP * craft(self.craft_of(frame["scene"]))["size"]
        # Gunmetal, lit by the world; red while it is hurt.
        body = (0.16 + hurt * 0.8, 0.17, 0.21)
        self._put(p, place, (size, size, size), angles, body, 0.0, 0.0, 0.0)
        self.ship.draw(self.gl, p)
        # The canopy: glass reflecting the world it passes through, the sky,
        # the sun on the road and the city's lights, with the cockpit dim
        # behind.
        glass = colorsys.hsv_to_rgb((frame["hue"] + 0.5) % 1.0, 0.85, 0.75)
        low = colorsys.hsv_to_rgb(frame["hue"] % 1.0, 0.9, 1.4)
        high = colorsys.hsv_to_rgb((frame["hue"] + 0.55) % 1.0, 0.8, 0.10)
        city = colorsys.hsv_to_rgb((frame["hue"] + 0.5) % 1.0, 0.7, 1.6)
        p.set("uGlass", 1.0)
        p.set("uEye", QVector3D(*frame["eye"]))
        p.set("uClock", float(frame["travel"]) * 0.12)
        p.set("uSkyLow", QVector3D(*low))
        p.set("uSkyHigh", QVector3D(*high))
        p.set("uCity", QVector3D(*city))
        self._put(p, place, (size, size, size), angles, glass, 0.0, 0.0, 0.0)
        self.canopy.draw(self.gl, p)
        p.set("uGlass", 0.0)
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
        size = SHIP * craft(self.craft_of(scene))["size"]
        self._put(p, place, (size, size, size), angles, colour, 1.0, 0.0, 0.0)
        self.ship_trim.draw(self.gl, p)
        # The engine: a hot core at the tail and a flame that grows on the beat
        # and with the passage's pace.
        name = self.craft_of(scene)
        flame = (0.12 + frame["beat"] * 0.14 + frame["rush"] * 0.20) * (
            craft(name)["flame"])
        engine = colorsys.hsv_to_rgb((frame["hue"] + 0.5) % 1.0, 0.55, 1.0)
        yaw, pitch, roll = angles
        for nx, ny, nz in nozzles(name):
            across, up = nx * size, ny * size
            nozzle = (place[0] + across * math.cos(roll) - up * math.sin(roll),
                      place[1] + across * math.sin(roll) + up * math.cos(roll),
                      place[2] - nz * size)
            self._put(p, nozzle, (0.07, 0.07, flame), angles,
                      tuple(c * 2.0 for c in engine), 1.0, 0.0, 0.0)
            self.cube.draw(self.gl, p)
            self._spawn_exhaust(frame, nozzle, engine)
        p.release()

    def _draw_gates(self, frame) -> None:
        import colorsys

        scene = frame["scene"]
        if not scene._clock:
            return
        p = self._solid(frame)
        colour = colorsys.hsv_to_rgb(frame["hue"], 0.8, 1.0)
        first = scene._beat_at_road(scene._at - 3.0)
        for n in range(first, first + BEAT_MARKS):
            z = scene.RIDER_AT + scene.beat_on_road(n) - scene._at
            if z > SEEN_AHEAD:
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
            # The first beat of each bar taller and brighter, all turned down
            # where the music is calm.
            downbeat = scene.bar_place(n) == 0
            bar = 1.35 if downbeat else 1.0
            lit *= (1.6 if downbeat else 1.0) * scene.gate_light(n)
            self._put(p, (0.0, 0.0, z), (bar, bar, 1.0), (0.0, 0.0, 0.0),
                      tuple(c * lit for c in colour), 1.0, 0.0, 0.0)
            self.arch.draw(self.gl, p)
        p.release()

    def _draw_barriers(self, frame) -> None:
        scene = frame["scene"]
        p = self.barrier
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.set("uHalf", float(frame["half"]))
        p.set("uHue", float(frame["hue"]))
        p.set("uBeat", float(frame["beat"]))
        p.set("uTravel", float(frame["travel"] - scene.RIDER_AT))
        p.set("uPerBeat", float(frame["per_beat"]))
        self._beat_uniforms(p, frame)
        p.set("uWave", float(self._wave))
        p.set("uWaveLit", float(self._wave_lit))
        p.set("uRider", float(scene.RIDER_AT))
        self.barriers.draw(self.gl, p)
        p.release()

    def _draw_beacons(self, frame) -> None:
        p = self.beacon
        p.bind()
        self._road_uniforms(p, frame)
        self._fog_uniforms(p, frame)
        p.array("uLevels", frame["bands"], BANDS, 1)
        p.set("uTravel", float(frame["travel"]))
        p.set("uLoop", 80.0)
        p.set("uFrom", float(ROAD_FROM))
        p.set("uKick", float(self._kick_punch))
        p.set("uBeat", float(frame["beat"]))
        p.set("uScreen", float(self._fbo_size[1] * 0.05))
        self.gl.glEnable(GL_PROGRAM_POINT_SIZE)
        self.gl.glEnable(GL_POINT_SPRITE)
        self.beacons.draw(self.gl, p)
        self.gl.glDisable(GL_POINT_SPRITE)
        self.gl.glDisable(GL_PROGRAM_POINT_SIZE)
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

    def _notice(self, scene) -> None:
        """Which blocks finished this frame, and which the craft met. The game
        marks a block done when its moment passes and says what happened
        (see Rider.struck): one taken or hit leaves the picture; one missed,
        jumped, a lane away or met while the craft could not be hurt goes on
        past. Working it out here from the craft's position drew jumped
        prizes going into the ship.
        """
        self._taken_now = []
        alive = set()
        for block in scene._blocks:
            alive.add(id(block))
            if not block[3] or id(block) in self._done_seen:
                continue
            self._done_seen.add(id(block))
            how = scene.struck(block)
            if how is None:
                continue
            self._taken.add(id(block))
            if how == "taken":
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
            # Where the obstacle met the nose: what is left is thrown up and
            # carried on down the road past the craft.
            nose = (at[0], 0.35, scene.RIDER_AT + self.NOSE + travel)
            if kind == "hit":
                # Again from the start for each, and harder in a run.
                hard = max(1.0, strength)
                self._shock, self._shock_hard = 0.0, min(1.8, hard)
                self._shock_at = self._screen_ship
                self._knock = 1.0
                self._split = min(2.0, hard)
                self._spawn(int(110 * hard), at, 10.0, (5.0, 0.6, 0.2),
                            0.45, 0.7)
                self._spawn(40, at, 6.0, (0.9, 0.9, 1.0), 0.6, 0.9)
                self._spawn(int(34 * hard), nose, 5.5, (3.2, 0.35, 0.22),
                            1.0, 1.1, spread=1.3, up=1.1)
            elif kind == "shatter":
                self._shock, self._shock_hard = 0.0, 0.6
                self._shock_at = self._screen_ship
                self._split = 0.5
                self._spawn(140, at, 8.0, (1.8, 2.8, 4.0), 0.5, 0.8)
                self._spawn(24, nose, 5.0, (2.6, 0.3, 0.2), 0.9, 1.0,
                            spread=1.3, up=1.1)
            elif kind == "finish":
                # The end of the track: fireworks down the road.
                self._bloom_bump = 1.0
                for burst in range(6):
                    sky = (self._rng.uniform(-10.0, 10.0),
                           self._rng.uniform(4.0, 10.0),
                           scene.RIDER_AT + travel
                           + self._rng.uniform(16.0, 40.0))
                    shade = colorsys.hsv_to_rgb((hue + burst * 0.17) % 1.0,
                                                0.7, 1.0)
                    self._spawn(160, sky, 8.0, tuple(c * 4.0 for c in shade),
                                2.2, 1.8)
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
                if kind in ("prize", "clear", "power", "milestone"):
                    self._glow = min(1.6, self._glow + 0.6 * strength)
                    self._glow_colour = colour
                    self._punch = min(1.6, self._punch + 0.55 * strength)
                if kind == "milestone":
                    self._split = 0.4
                    for _ in range(3):
                        sky = (self._rng.uniform(-9.0, 9.0),
                               self._rng.uniform(5.0, 9.0),
                               scene.RIDER_AT + travel
                               + self._rng.uniform(22.0, 34.0))
                        self._spawn(140, sky, 7.0,
                                    tuple(c * 4.0 for c in colour), 2.4, 1.6)

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
        # Louder passages bloom more, out in the open; a tunnel's walls are
        # already alight.
        p.set("uBloomAmount", float(0.55 + self._bloom_bump
                                    + frame["flash"] * 0.4
                                    + frame["loud"] * 0.12
                                    * (1.0 - self.inside)))
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
        p.set("uGlow", float(self._glow))
        p.set("uGlowColour", QVector3D(*self._glow_colour))
        p.set("uFlash", float(frame["flash"]))
        p.set("uOpacity", float(opacity))
        p.set("uTime", float(self._now % 100.0))
        p.set("uVignette", 0.55)
        self.quad.draw(gl, p)
        p.release()
        gl.glActiveTexture(GL_TEXTURE0 + 1)
        gl.glBindTexture(GL_TEXTURE_2D, 0)


class Hud:
    """The numbers over the world, drawn with the painter. The score is the
    biggest thing that is not the road and counts up, so earning is seen
    happening; the chain has its own place and warms with the run; progress
    through the track runs along the top edge.
    """

    def __init__(self) -> None:
        self._shown = 0.0
        self._was = 0
        self._bump = 0.0
        self._chain_bump = 0.0
        self._chain_was = 0
        self._now = None
        #: Where each number was last drawn, by name, for checking nothing
        #: overlaps.
        self.placed = {}

    def draw(self, painter, rect, scene, state) -> None:
        import time as _time

        from PySide6.QtCore import QPointF, QRectF, Qt
        from PySide6.QtGui import QColor, QFont, QPainterPath, QPen

        now = _time.monotonic()
        dt = 1.0 / 60.0 if self._now is None else max(0.0, min(0.1, now - self._now))
        self._now = now
        # What has been earned, without the clean bonus: see Rider._worth.
        worth = int(scene._score)
        if worth > self._was:
            self._bump = 1.0
        elif worth < self._was:
            self._shown = float(worth)
        self._was = worth
        # Counts up quickly but visibly: most of the gap closes in a quarter of
        # a second.
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
            return path.boundingRect()

        self.placed = {}
        if getattr(scene, "_finished", False):
            # The results card says all of it now. See Rider._results.
            painter.restore()
            return

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
        if scene._mode == "Puzzle" and hasattr(scene, "matrix_box"):
            # Beside the grid, not on it: the grid is in this corner too.
            box = scene.matrix_box(rect)
            left = box.right() + tall * 0.035
            floor = box.bottom()
        self.placed["chain label"] = text(
            QPointF(left, floor - tall * 0.085), label, tall * 0.018,
            QColor(255, 255, 255, 200), "left", None, weight=700)
        self.placed["chain"] = text(
            QPointF(left, floor), f"{chain}",
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
            # What keeping it clean is worth, paid at the end.
            text(QPointF(rect.center().x(), rect.top() + tall * 0.15),
                 f"CLEAN +{scene.bonus():.0%}", tall * 0.018,
                 QColor(220, 255, 230, 190), "centre", None, weight=700)
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
