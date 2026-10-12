"""The visualiser pane, drawn on the graphics card.

At a Retina full screen (2880x1800 real pixels) Music rider cost 42 ms a
frame on the CPU with its bloom and vignette, so the pane drew it at half
resolution and stretched it. On the card the same QPainter calls cost 10 ms
at full resolution.

The rest of the suite runs offscreen, where no OpenGL context can be made,
so scenes are tested through the CPU path, which is also the fallback on a
machine without a working card. Each test here runs a short script on the
real platform in a subprocess, reports back as JSON, and skips where no
context can be made.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Everything a script on the card needs before its own body.
HEAD = textwrap.dedent("""
    import json, math, os, sys
    # An exception that reaches Qt from a paint or a timer is printed and
    # swallowed there, and the frame it came from is kept (as
    # sys.last_traceback) until the interpreter shuts down - painter and
    # all, which then ended itself on a device already gone and aborted
    # the process with a crash report on the screen. Here it ends the
    # script at once instead, and the test sees it.
    def _unhandled(kind, value, trace):
        import traceback
        traceback.print_exception(kind, value, trace)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(70)
    sys.excepthook = _unhandled
    sys.path.insert(0, {root!r})
    from array import array
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtGui import (QColor, QImage, QPainter, QPen,
                               QOpenGLContext, QOffscreenSurface)
    from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
    app = QApplication.instance() or QApplication([])
    # Named, so anything these put on the screen says what it is rather
    # than "python", which is what an unnamed application's windows are
    # called.
    app.setApplicationName("Mail Manager")
    app.setApplicationDisplayName("Mail Manager - test of the graphics card")
    probe_surface = QOffscreenSurface()
    probe_surface.create()
    probe = QOpenGLContext()
    if not probe.create():
        print(json.dumps({{"skip": "no OpenGL context on this machine"}}))
        sys.exit(0)
    del probe
    import visualizers
    import attachment_widgets
    from attachment_widgets import Spectrum

    def pane(scene=None, size=(640, 400)):
        made = Spectrum()
        made.setWindowTitle("Visualiser - test of the graphics card")
        made.set_unbounded(True)
        made.set_frames([array("f", [0.4] * 27)] * 900, 15)
        made.set_labels([str(i) for i in range(27)])
        made.set_scene(scene or type(visualizers.by_name("Music rider"))())
        made.set_playing(True)
        made._reveal_changed(1.0)
        made.setMinimumSize(*size)
        made.resize(*size)
        made._fresh = 1.0
        made._drawn = 99
        return made

    def lightness(image, x, y):
        return image.pixelColor(x, y).lightnessF()
""").format(root=str(ROOT))


#: A line one real pixel wide down the middle of the pane, drawn at whatever
#: resolution the painter has: how sharpness is measured.
HAIRLINE = textwrap.dedent("""
    class Hairline:
        name = "Hairline"
        blurb = "test"
        sharp_pixels = 0
        def reset(self):
            pass
        def paint(self, painter, rect, state):
            pen = QPen(QColor(255, 255, 255))
            pen.setCosmetic(True)
            pen.setWidthF(1.0)
            painter.setPen(pen)
            r = painter.device().devicePixelRatioF()
            # The middle of a buffer pixel, in points.
            x = (int(rect.width() * r / 2) + 0.5) / r
            painter.drawLine(QPointF(x, rect.top() + 20),
                             QPointF(x, rect.bottom() - 20))
""")


#: Every script ends without the interpreter's teardown, which takes Qt's
#: objects apart in any order (a painter after its buffer); the script has
#: nothing left to do.
TAIL = textwrap.dedent("""
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)
""")


#: Whether the real platform here can run these at all, found out once per
#: process: None where it can, otherwise why not. A hosted runner's window
#: server never answered, and every script run there waited out its whole
#: three minutes; one that drew a frame a second would have done the same.
_real_platform: list = []

#: The most the probe may take in all, and the most twenty frames of the
#: rider may take within it, in seconds. Twenty frames are under a second on
#: a Mac of the last few years; the scripts here draw hundreds.
PROBE_FOR = 45.0
FRAMES_FOR = 10.0


def real_platform_or_skip() -> None:
    """Skip, with the reason, where no script could run on the card here."""
    if not _real_platform:
        _real_platform.append(_probe_real_platform())
    if _real_platform[0] is not None:
        pytest.skip(_real_platform[0])


def _probe_real_platform():
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["MAIL_MANAGER_GPU"] = "1"
    body = HEAD + textwrap.dedent("""
        import time
        made = pane(size=(320, 200))
        made.show()
        started = time.monotonic()
        for i in range(20):
            made.set_position(i * 50)
            made._tick()
            made._canvas.grabFramebuffer()
        print(json.dumps({"frames": time.monotonic() - started}))
    """) + TAIL
    try:
        done = subprocess.run([sys.executable, "-c", body],
                              capture_output=True, text=True,
                              timeout=PROBE_FOR, env=env, cwd=ROOT)
    except subprocess.TimeoutExpired:
        return f"the real platform did not answer within {PROBE_FOR:.0f}s"
    lines = [line for line in done.stdout.splitlines()
             if line.startswith("{")]
    if not lines or done.returncode != 0:
        return (f"no script runs on the real platform here (exit "
                f"{done.returncode}): {done.stderr.strip()[-300:]}")
    found = json.loads(lines[-1])
    if "skip" in found:
        return found["skip"]
    if found["frames"] > FRAMES_FOR:
        return (f"the card here draws too slowly for these: "
                f"{found['frames']:.0f}s for twenty frames")
    return None


def on_the_card(body: str) -> dict:
    """Run ``body`` on the real platform and return what it printed.

    Fails if it did not finish cleanly: an exception that escaped into Qt,
    or a crash on the way out, is a fault even where the numbers printed
    look right."""
    real_platform_or_skip()
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["MAIL_MANAGER_GPU"] = "1"
    done = subprocess.run(
        [sys.executable, "-c", HEAD + textwrap.dedent(body) + TAIL],
        capture_output=True, text=True, timeout=180, env=env, cwd=ROOT)
    lines = [line for line in done.stdout.splitlines()
             if line.startswith("{")]
    if not lines:
        raise AssertionError(
            f"the script on the card printed nothing it could report "
            f"(exit {done.returncode}):\n{done.stderr[-3000:]}")
    found = json.loads(lines[-1])
    if "skip" in found:
        pytest.skip(found["skip"])
    if done.returncode != 0:
        raise AssertionError(
            f"the script on the card did not finish cleanly (exit "
            f"{done.returncode}):\n{done.stderr[-3000:]}")
    return found


class TestThePaneIsOnTheCard:
    def test_it_draws_at_the_screens_own_resolution(self):
        """Every real pixel, rather than one buffer pixel per point stretched,
        which made a Retina full screen soft.

        Measured on a line one real pixel wide, which stays one pixel wide
        only if nothing was drawn small and stretched. The frame's size
        would pass either way.
        """
        got = on_the_card(HAIRLINE + textwrap.dedent("""
            made = pane(scene=Hairline(), size=(640, 400))
            for i in range(3):
                made._tick()
            shot = made._canvas.grabFramebuffer()
            r = made._canvas.devicePixelRatioF()
            y = shot.height() // 2
            lit = [x for x in range(shot.width())
                   if lightness(shot, x, y) > 0.35]
            print(json.dumps({"gpu": made.on_gpu, "ratio": r,
                              "size": [shot.width(), shot.height()],
                              "lit": lit}))
        """))
        assert got["gpu"] is True, "the pane is not drawing on the card"
        ratio = got["ratio"]
        assert got["size"] == [round(640 * ratio), round(400 * ratio)]
        assert len(got["lit"]) == 1, (
            f"a line one real pixel wide came out {len(got['lit'])} "
            f"pixels wide ({got['lit']}), so the frame was drawn smaller "
            f"than the screen and stretched")

    def test_it_draws_the_same_picture_as_the_cpu(self):
        """One scene, one moment, painted both ways and compared."""
        got = on_the_card("""
            from PySide6.QtGui import QOpenGLContext, QOffscreenSurface
            from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
                QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice)
            from attachment_widgets import SpectrumState
            W, H = 960, 600
            def state():
                s = SpectrumState()
                s.levels = [0.4] * 27
                s.bass = s.mid = s.high = 0.4
                s.synth = 0.2
                s.kit = {"Kick": 0.5}
                s.at = 1.0
                s.tempo = 128.0
                s.beat_at = 0.1
                s.chart = {"Kick": tuple(i * 0.47 for i in range(80))}
                s.settle()
                return s
            def paint(target_painter):
                scene = type(visualizers.by_name("Music rider"))()
                scene._last = None
                was = visualizers.time.monotonic
                visualizers.time.monotonic = lambda: 500.0
                try:
                    scene.paint(target_painter, QRectF(0, 0, W, H), state())
                finally:
                    visualizers.time.monotonic = was
            surface = QOffscreenSurface()
            surface.create()
            context = QOpenGLContext()
            context.create()
            context.makeCurrent(surface)
            shape = QOpenGLFramebufferObjectFormat()
            shape.setSamples(4)
            shape.setAttachment(
                QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
            buffer = QOpenGLFramebufferObject(QSize(W, H), shape)
            buffer.bind()
            device = QOpenGLPaintDevice(QSize(W, H))
            p = QPainter(device)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0))
            paint(p)
            p.end()
            buffer.release()
            gpu = buffer.toImage()
            cpu = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)
            cpu.fill(QColor(0, 0, 0))
            p = QPainter(cpu)
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            paint(p)
            p.end()
            points = [(x, y) for x in range(0, W, 6) for y in range(0, H, 6)]
            diff = sum(abs(lightness(gpu, x, y) - lightness(cpu, x, y))
                       for x, y in points) / len(points)
            print(json.dumps({"diff": diff}))
        """)
        assert got["diff"] < 0.01, (
            f"the card and the CPU draw the same moment "
            f"{got['diff']:.4f} apart in brightness on average")

    def test_the_bloom_is_made_from_an_even_average(self):
        """Where the card will not work the bloom out itself (see
        TestTheGlowOnTheCard), it is a small copy of the frame, made big
        again. An eightfold shrink in one linear blit reads one pixel in
        sixteen, so a thin line reached the small copy only where it crossed
        the pixels read, and the bloom strung soft blobs along every
        chevron. In halves, each step is an exact average.

        Read off the pane's own small copy: a first version ran its own copy
        of the halving loop, and passed with the pane's loop deleted.
        """
        got = on_the_card("""
            class Fan:
                name = "Music rider"      # borrows the rider's polish
                blurb = "test"
                sharp_pixels = 0
                def reset(self):
                    pass
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(0, 0, 0))
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    painter.setPen(QPen(QColor(255, 200, 120), 1.0))
                    c = rect.center()
                    for i in range(24):
                        a = i / 24 * math.pi
                        painter.drawLine(c, c + QPointF(
                            math.cos(a) * rect.width() * 0.4,
                            math.sin(a) * rect.height() * 0.4))
            made = pane(scene=Fan(), size=(960, 600))
            made._canvas.glow_failed = True
            for i in range(3):
                made._tick()
            made._canvas.grabFramebuffer()
            halo = made._canvas.last_halo.toImage()
            # The CPU's own average of the same picture, for scale.
            r = made._canvas.devicePixelRatioF()
            full = QImage(int(960 * r), int(600 * r),
                          QImage.Format.Format_ARGB32_Premultiplied)
            full.setDevicePixelRatio(r)
            full.fill(QColor(0, 0, 0))
            p = QPainter(full)
            Fan().paint(p, QRectF(0, 0, 960, 600), None)
            p.end()
            cpu = full.scaled(halo.width(), halo.height(),
                              Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
            def lumpy(img):
                lit = [lightness(img, x, y) for y in range(img.height())
                       for x in range(img.width())
                       if lightness(img, x, y) > 0.02]
                mean = sum(lit) / len(lit)
                return (sum((v - mean) ** 2 for v in lit) / len(lit)) ** 0.5
            print(json.dumps({"halves": lumpy(halo), "cpu": lumpy(cpu)}))
        """)
        assert got["halves"] < got["cpu"] * 1.4, (
            f"the bloom's small copy is {got['halves']:.3f} lumpy along "
            f"a thin line against the CPU's even average of "
            f"{got['cpu']:.3f}: the lines will bloom as rows of blobs")

    @pytest.mark.parametrize("rung", [(4, 1.0), (2, 0.5)])
    def test_the_polish_comes_from_where_the_scene_is(self, rung):
        """A framebuffer counts rows from the bottom and a painter from the
        top. With the scene centred the two agree, so here the scene sits
        high (the strip under it is for the controls, as in the app) and is
        lit only in its top quarter. The bloom's small copy, where the card
        makes one, must be lit at its top too; read upside down, it comes
        from a band the scene is not in. See TestTheGlowOnTheCard for the
        bloom the card works out itself.
        """
        got = on_the_card("""
            class TopQuarter:
                name = "Music rider"      # borrows the rider's polish
                blurb = "test"
                sharp_pixels = 0
                def reset(self):
                    pass
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(0, 0, 0))
                    painter.fillRect(QRectF(rect.x(), rect.y(),
                                            rect.width(),
                                            rect.height() * 0.25),
                                     QColor(255, 255, 255))
            made = pane(scene=TopQuarter(), size=(800, 600))
            made._canvas.glow_failed = True
            made._card.choice = lambda pixels, ratio, scene: RUNG
            made.set_reserve(200)
            for i in range(3):
                made._tick()
            made._canvas.grabFramebuffer()
            halo = made._canvas.last_halo.toImage()
            h = halo.height()
            def band(y0, y1):
                values = [lightness(halo, x, y)
                          for y in range(int(y0), int(y1))
                          for x in range(halo.width())]
                return sum(values) / max(1, len(values))
            print(json.dumps({"top": band(0, h * 0.2),
                              "bottom": band(h * 0.5, h)}))
        """.replace("RUNG", repr(rung)))
        # Lit across its whole top, not half: a frame drawn smaller has fewer
        # pixels per point, and reading it as if it had as many takes the copy
        # from a patch twice the size, with the scene in one corner.
        assert got["top"] > 0.8, (
            f"the small copy of a scene lit at its top is only "
            f"{got['top']:.2f} bright at its top: it was taken from the "
            f"wrong band of the frame")
        assert got["bottom"] < 0.1, (
            f"and {got['bottom']:.2f} at its bottom, where the scene is "
            f"black")

    def test_a_card_that_fails_hands_back_to_the_cpu(self):
        """Once, for the rest of the session, and without a crash."""
        got = on_the_card("""
            made = pane(size=(640, 400))
            made._tick()
            made._canvas.grabFramebuffer()
            def broken(*args, **kwargs):
                raise RuntimeError("a card that has stopped working")
            made._canvas.buffers = broken
            made._tick()
            made._canvas.grabFramebuffer()
            for _ in range(20):
                app.processEvents()
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            p = QPainter(image)
            made._paint(p)
            p.end()
            print(json.dumps({
                "gpu": made.on_gpu,
                "lit": sum(1 for x in range(0, 640, 8)
                           for y in range(0, 400, 8)
                           if lightness(image, x, y) > 0.05),
            }))
        """)
        assert got["gpu"] is False, (
            "a card that raised is still being drawn on")
        assert got["lit"] > 50, (
            "after giving up on the card the pane drew nothing on the CPU")

    def test_it_survives_being_moved_into_full_screen(self):
        """Full screen moves the pane into another window, which can mean a new
        context; a canvas keeping buffers from the old one would draw
        nothing."""
        got = on_the_card("""
            home, away = QWidget(), QWidget()
            made = pane(size=(640, 400))
            made.setParent(home)
            made._tick()
            before = made._canvas.grabFramebuffer()
            made.setParent(away)
            away.resize(640, 400)
            made.resize(640, 400)
            made._tick()
            after = made._canvas.grabFramebuffer()
            def lit(image):
                return sum(1 for x in range(0, image.width(), 8)
                           for y in range(0, image.height(), 8)
                           if lightness(image, x, y) > 0.05)
            print(json.dumps({"before": lit(before), "after": lit(after),
                              "gpu": made.on_gpu}))
        """)
        assert got["gpu"] is True
        assert got["after"] > got["before"] * 0.5, (
            f"after being moved the canvas lit {got['after']} samples "
            f"against {got['before']} before")


#: The scope painted both ways, frame after frame: into a buffer on the card
#: through Qt's OpenGL engine, and into an image on the CPU. A new trace comes
#: fifteen times a second, so what is compared is a phosphor with several
#: fading traces: the fade and the layering as well as the beam.
SCOPE_BOTH_WAYS = textwrap.dedent("""
    from PySide6.QtGui import QOpenGLContext, QOffscreenSurface
    from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
        QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice)
    from attachment_widgets import SpectrumState
    W, H = 900, 520

    def traces(mode, count):
        out = []
        for frame in range(count):
            if mode == "X-Y":
                v = []
                for i in range(900):
                    t = i / 900 * math.tau
                    v.append(int(26000 * math.sin(3 * t + frame * 0.3)))
                    v.append(int(26000 * math.sin(2 * t)))
                out.append(v)
            else:
                out.append([0.8 * math.sin(i / 1024 * math.tau * 3
                                           + frame * 0.7)
                            + 0.15 * math.sin(i / 1024 * math.tau * 17)
                            for i in range(1024)])
        return out

    def state_for(trace, mode):
        s = SpectrumState()
        s.levels = [0.4] * 27
        s.bass = s.mid = s.high = 0.4
        s.at = 1.0
        if mode == "X-Y":
            s.vector = trace
        else:
            s.trace = trace
        s.settle()
        return s

    def run(paint_on, mode, opacity=1.0, frames=6):
        scene = type(visualizers.by_name("Oscilloscope"))()
        scene.set_mode(mode)
        clock = [500.0]
        was = visualizers.time.monotonic
        visualizers.time.monotonic = lambda: clock[0]
        try:
            for trace in traces(mode, frames):
                clock[0] += 1.0 / 15.0
                paint_on(scene, state_for(trace, mode), opacity)
        finally:
            visualizers.time.monotonic = was
        return scene

    surface = QOffscreenSurface()
    surface.create()
    context = QOpenGLContext()
    context.create()
    context.makeCurrent(surface)
    shape = QOpenGLFramebufferObjectFormat()
    shape.setSamples(4)
    shape.setAttachment(
        QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
    buffer = QOpenGLFramebufferObject(QSize(W, H), shape)
    device = QOpenGLPaintDevice(QSize(W, H))
    cpu = QImage(W, H, QImage.Format.Format_ARGB32_Premultiplied)

    def on_card(scene, state, opacity):
        buffer.bind()
        p = QPainter(device)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0))
        p.setOpacity(opacity)
        scene.paint(p, QRectF(0, 0, W, H), state)
        p.end()
        buffer.release()

    def on_cpu(scene, state, opacity):
        cpu.fill(QColor(0, 0, 0))
        p = QPainter(cpu)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setOpacity(opacity)
        scene.paint(p, QRectF(0, 0, W, H), state)
        p.end()

    def apart(a, b):
        points = [(x, y) for x in range(0, W, 3) for y in range(0, H, 3)]
        return sum(abs(lightness(a, x, y) - lightness(b, x, y))
                   for x, y in points) / len(points)

    def lit(image):
        # Where the trace is, and not the dim field and grid behind it.
        return sum(1 for x in range(0, W, 3) for y in range(0, H, 3)
                   if lightness(image, x, y) > 0.3)
""")


#: A scene the tests below make, with a recipe of bloom alone: every scene's
#: own recipe has a vignette or grain as well, which would be measured too.
GLOW_TEST = textwrap.dedent("""
    visualizers.POST["Glow test"] = {"bloom": 0.9}

    class GlowTest:
        name = "Glow test"
        blurb = "test"
        sharp_pixels = 0
        def reset(self):
            pass

    def polished(made, ticks=3, bare=None):
        # The frame with its polish, and the frame without it. A shared
        # runner's card now and then hands back a frame without the polish
        # laid; it is taken again, up to three times, until it differs from
        # the frame without. How much it adds and where it lands are then
        # measured on that one, as ever, and a polish that never lands still
        # fails.
        if bare is None:
            made.set_post(False)
            made._tick()
            bare = made._canvas.grabFramebuffer()
        made.set_post(True)
        for tries in range(1, 4):
            for i in range(ticks):
                made._tick()
            lit = made._canvas.grabFramebuffer()
            if lit != bare:
                break
        return lit, bare, tries
""")


class TestTheGlowOnTheCard:
    """The bloom worked out on the card (see glow_gl): the frame halved,
    blurred there by a true Gaussian and laid back over itself. It was a
    copy an eighth of the frame's size stretched back up, and at full screen
    the rave's wireframe glowed in tiles that moved as it turned.
    """

    def test_a_thin_lines_glow_is_centred_on_it_all_along(self):
        """A glow made of blocks is centred on the blocks, not on the line:
        along a line on a slant it wandered up to 2.2 real pixels either
        side of it at full screen, over and over as the line crossed them,
        which is the tiling that was seen; this one stays within a fifth of
        a pixel of it. Measured on the light the polish adds, column by
        column, by its centre of weight, against where the line is. And it
        spreads as far as the blocks did, half a block's side, measured by
        how far that light lies from its centre, less how far the line's
        own does."""
        got = on_the_card(GLOW_TEST + textwrap.dedent("""
            SLOPE = 0.13
            class Slant(GlowTest):
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(0, 0, 0))
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    painter.setPen(QPen(QColor(90, 90, 90), 1.5))
                    top = rect.top() + rect.height() * 0.4
                    painter.drawLine(
                        QPointF(rect.left(), top),
                        QPointF(rect.right(), top + rect.width() * SLOPE))
            made = pane(scene=Slant(), size=(1440, 900))
            lit, bare, tries = polished(made)
            r = made._canvas.devicePixelRatioF()
            w = lit.width()
            def spread_of(weights, centre):
                total = sum(w for _y, w in weights)
                return sum(w * (y - centre) ** 2
                           for y, w in weights) / total
            apart = []
            widths = []
            clipped = 0
            for x in range(int(w * 0.2), int(w * 0.8), 2):
                centre = 900 * 0.4 * r + (x + 0.5) * SLOPE
                rows = range(int(centre) - 30, int(centre) + 31)
                added = []
                line = []
                for y in rows:
                    shown = lit.pixelColor(x, y).green()
                    under = bare.pixelColor(x, y).green()
                    clipped += shown >= 250
                    added.append((y + 0.5, shown - under))
                    line.append((y + 0.5, under))
                total = sum(a for _y, a in added)
                apart.append(sum(y * a for y, a in added) / total - centre)
                widths.append(spread_of(added, centre)
                              - spread_of(line, centre))
            # The side of a block of the small copy the glow was made from.
            block = w / attachment_widgets.PostProcess.halo_size(
                w, lit.height()).width()
            print(json.dumps({
                "gpu": made.on_gpu, "glow": made._canvas.glow is not None,
                "tries": tries, "spread": block / 2.0,
                "clipped": clipped,
                "most": max(abs(a) for a in apart),
                "width": (sum(widths) / len(widths)) ** 0.5}))
        """))
        assert got["gpu"] is True and got["glow"] is True
        # Fair only if nothing reached white, which would cut the glow off
        # where the line is, and where there is a glow to measure.
        assert got["clipped"] == 0
        assert got["spread"] >= 2.0
        assert got["most"] < 0.4, (
            f"the glow of a thin line wanders {got['most']:.2f} real pixels "
            f"off it: it is laid in blocks, which move as the line does")
        assert abs(got["width"] - got["spread"]) < got["spread"] * 0.15, (
            f"the glow spreads {got['width']:.2f} real pixels from the "
            f"line, where the blocks spread it {got['spread']:.2f}")

    def test_its_fringes_lie_either_side_by_the_recipes_distance(self):
        """The fringing is two more copies of the glow, the recipe's
        distance either side, which spread it sideways by just that much:
        measured across an upright line, as how much further its light lies
        from the line with the copies alone than the glow does."""
        got = on_the_card(GLOW_TEST + textwrap.dedent("""
            class Upright(GlowTest):
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(0, 0, 0))
                    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                    # Wide and bright enough that the copies' faint edges
                    # are not lost to rounding: they are most of how far
                    # out the light lies.
                    painter.setPen(QPen(QColor(120, 120, 120), 3.0))
                    x = rect.center().x() + 0.3
                    painter.drawLine(QPointF(x, rect.top()),
                                     QPointF(x, rect.bottom()))
            made = pane(scene=Upright(), size=(1440, 900))
            made.set_post(False)
            for i in range(3):
                made._tick()
            bare = made._canvas.grabFramebuffer()
            r = made._canvas.devicePixelRatioF()
            middle = (720 + 0.3) * r
            def spread(image):
                found = []
                for y in range(int(image.height() * 0.3),
                               int(image.height() * 0.7), 3):
                    weights = [(x + 0.5, image.pixelColor(x, y).green()
                                - bare.pixelColor(x, y).green())
                               for x in range(int(middle) - 45,
                                              int(middle) + 46)]
                    total = sum(w for _x, w in weights)
                    found.append(sum(w * (x - middle) ** 2
                                     for x, w in weights) / total)
                return sum(found) / len(found)
            seen = {}
            for label, recipe in (("glow", {"bloom": 0.9}),
                                  ("fringes", {"aberration": 1.6})):
                visualizers.POST["Glow test"] = recipe
                lit, _bare, _tries = polished(made, ticks=1, bare=bare)
                seen[label] = spread(lit)
            print(json.dumps({"ratio": r,
                              "apart": max(0.0, seen["fringes"]
                                           - seen["glow"]) ** 0.5}))
        """))
        wanted = 1.6 * got["ratio"]
        assert abs(got["apart"] - wanted) < wanted * 0.2, (
            f"the fringes lie {got['apart']:.2f} real pixels either side of "
            f"the glow, where the recipe puts them {wanted:.2f}")

    def test_it_adds_what_the_recipe_asks_for(self):
        """As much as the small copy added: the recipe's bloom, to at most
        GLOW_MOST of the frame, and a copy either side at FRINGE where it
        asks for fringing; and faded with the scene as it comes up, or the
        glow arrives at full strength over a scene still half there."""
        got = on_the_card(GLOW_TEST + textwrap.dedent("""
            class Grey(GlowTest):
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(60, 60, 60))
            made = pane(scene=Grey(), size=(640, 400))
            got = {}
            for label, recipe, reveal in (
                    ("bloom", {"bloom": 0.5}, 1.0),
                    ("most", {"bloom": 0.95}, 1.0),
                    ("fringed", {"bloom": 0.9, "aberration": 1.2}, 1.0),
                    ("faded", {"bloom": 0.9, "aberration": 1.2}, 0.5)):
                made._reveal_changed(reveal)
                visualizers.POST["Glow test"] = recipe
                lit, bare, _tries = polished(made, ticks=1)
                x, y = lit.width() // 2, lit.height() // 2
                got[label] = (lit.pixelColor(x, y).green()
                              / bare.pixelColor(x, y).green())
            got["glow"] = made._canvas.glow is not None
            print(json.dumps(got))
        """))
        from attachment_widgets import PostProcess

        assert got["glow"] is True
        most, fringe = PostProcess.GLOW_MOST, PostProcess.FRINGE
        wanted = {"bloom": 1.5, "most": 1.0 + most,
                  "fringed": 1.0 + most + 2 * fringe,
                  "faded": 1.0 + 0.5 * (most + 2 * fringe)}
        for label, ratio in wanted.items():
            assert abs(got[label] - ratio) < 0.03, (
                f"{label}: the polish made the frame {got[label]:.3f} times "
                f"as bright, where the recipe asks for {ratio:.3f}")

    @pytest.mark.parametrize("rung", [(4, 1.0), (2, 0.5)])
    def test_it_lands_where_the_scene_is(self, rung):
        """Read and laid back in the framebuffer's rows, which count from
        the bottom. The scene sits high, with the controls' strip under
        it, and is lit only in its top quarter: the glow spills below that
        band and nowhere near the bottom of the scene, which, read upside
        down, it would light."""
        got = on_the_card(GLOW_TEST + textwrap.dedent("""
            class TopQuarter(GlowTest):
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(0, 0, 0))
                    painter.fillRect(QRectF(rect.x(), rect.y(), rect.width(),
                                            rect.height() * 0.25),
                                     QColor(200, 200, 200))
            made = pane(scene=TopQuarter(), size=(800, 600))
            made._card.choice = lambda pixels, ratio, scene: RUNG
            made.set_reserve(200)
            lit, bare, tries = polished(made)
            r = made._canvas.devicePixelRatioF()
            box = made._scene_box(QRectF(0, 0, 800, 400))
            top, height = box.y() * r, box.height() * r
            def added(y0, y1):
                values = [lit.pixelColor(x, y).green()
                          - bare.pixelColor(x, y).green()
                          for y in range(int(y0), int(y1), 2)
                          for x in range(int(box.x() * r) + 20,
                                         int(box.right() * r) - 20, 7)]
                return sum(values) / max(1, len(values))
            band = top + height * 0.25
            print(json.dumps({"glow": made._canvas.glow is not None,
                              "tries": tries,
                              "below": added(band + 1, band + 5),
                              "bottom": added(top + height * 0.5,
                                              top + height - 2)}))
        """).replace("RUNG", repr(rung)))
        assert got["glow"] is True
        assert got["below"] > 5, (
            f"the band lit at the top of the scene spills only "
            f"{got['below']:.1f} below itself: the glow was not laid there "
            f"(the frame was taken {got['tries']} times)")
        assert got["bottom"] < 1, (
            f"and {got['bottom']:.1f} reaches the bottom of the scene, "
            f"which is black: the glow came from the wrong band")

    def test_a_card_that_will_not_make_it_makes_a_small_copy(self):
        """Once, logged, and the bloom carries on the way it was made
        before, from a small copy read back off the card."""
        got = on_the_card(GLOW_TEST + textwrap.dedent("""
            import glow_gl
            tries = []
            class Refused:
                def __init__(self, gl):
                    tries.append(1)
                    raise RuntimeError("a card that will not build it")
            glow_gl.Glow = Refused
            visualizers.POST["Glow test"] = {"bloom": 0.5}
            class Grey(GlowTest):
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(60, 60, 60))
            made = pane(scene=Grey(), size=(640, 400))
            for i in range(4):
                made._tick()
                made._canvas.grabFramebuffer()
            lit, bare, _tries = polished(made, ticks=1)
            x, y = lit.width() // 2, lit.height() // 2
            print(json.dumps({
                "gpu": made.on_gpu, "tries": len(tries),
                "failed": made._canvas.glow_failed,
                "copy": made._canvas.last_halo is not None,
                "ratio": (lit.pixelColor(x, y).green()
                          / bare.pixelColor(x, y).green())}))
        """))
        assert got["gpu"] is True, "the pane left the card"
        assert got["failed"] is True and got["tries"] == 1, (
            f"the glow was tried {got['tries']} times on a card that "
            f"refused it")
        assert got["copy"] is True
        assert abs(got["ratio"] - 1.5) < 0.03, (
            f"and without it the frame is {got['ratio']:.3f} times as "
            f"bright, not the 1.5 its bloom asks for")


class TestAWideLineOnTheCard:
    """A line a few real pixels wide is stroked with a real pen on the card.
    The CPU draws one as a stack of hairlines, which is quicker there (see
    visualizers.stroke); on the card each hairline was its own faint pass
    with the card's four samples, and the stack came out ridged down its
    middle and lit twice where lines crossed: 24 levels in 255 from a real
    pen, on average over the rave's wireframe at its usual width.
    """

    def test_it_is_drawn_as_a_real_pen_draws_it(self):
        got = on_the_card("""
            from PySide6.QtGui import QPainterPath
            from PySide6.QtOpenGL import (QOpenGLFramebufferObject,
                QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice)
            surface = QOffscreenSurface()
            surface.create()
            context = QOpenGLContext()
            context.create()
            context.makeCurrent(surface)
            W, H = 840, 520
            def drawn(how, width):
                shape = QOpenGLFramebufferObjectFormat()
                shape.setSamples(4)
                shape.setAttachment(
                    QOpenGLFramebufferObject.Attachment.CombinedDepthStencil)
                multi = QOpenGLFramebufferObject(QSize(W, H), shape)
                flat = QOpenGLFramebufferObject(QSize(W, H))
                multi.bind()
                device = QOpenGLPaintDevice(QSize(W, H))
                device.setDevicePixelRatio(2.0)
                p = QPainter(device)
                p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0))
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                # The rave's wireframe: every corner of a hexagon to every
                # other.
                corners = [QPointF(W / 4 + math.cos(0.3 + k * math.tau / 6)
                                   * 150,
                                   H / 4 + math.sin(0.3 + k * math.tau / 6)
                                   * 90)
                           for k in range(6)]
                path = QPainterPath()
                for a in range(6):
                    for b in range(a + 1, 6):
                        path.moveTo(corners[a])
                        path.lineTo(corners[b])
                ink = QColor.fromHsvF(0.5, 0.25, 1.0, 0.7)
                if how == "pen":
                    p.setPen(QPen(ink, width, Qt.PenStyle.SolidLine,
                                  Qt.PenCapStyle.RoundCap,
                                  Qt.PenJoinStyle.RoundJoin))
                    p.drawPath(path)
                else:
                    visualizers.stroke(p, path, ink, width)
                p.end()
                multi.release()
                QOpenGLFramebufferObject.blitFramebuffer(flat, multi)
                return flat.toImage()
            apart = {}
            for width in (1.2, 1.4, 1.7):
                pen, stroked = drawn("pen", width), drawn("stroke", width)
                total = count = 0
                for y in range(H):
                    for x in range(W):
                        one = pen.pixelColor(x, y).green()
                        two = stroked.pixelColor(x, y).green()
                        if one > 8 or two > 8:
                            total += abs(one - two)
                            count += 1
                apart[str(width)] = total / max(1, count)
            print(json.dumps(apart))
        """)
        for width, apart in got.items():
            assert apart < 0.5, (
                f"at {width} points ({float(width) * 2:.1f} real pixels) the "
                f"line is {apart:.1f} levels in 255 from a real pen's, on "
                f"average where either drew")


class TestTheScopeOnTheCard:
    """At a Retina full screen the scope's phosphor cost 16 ms a trace on the
    CPU, plus the upload, and ran at 33 frames a second. Its screen now
    stays on the card (see scope_gl) and must look exactly as it did."""

    @pytest.mark.parametrize("mode,opacity", [("Sweep", 1.0), ("X-Y", 1.0),
                                              ("Sweep", 0.55)])
    def test_it_draws_what_the_cpu_draws(self, mode, opacity):
        got = on_the_card(SCOPE_BOTH_WAYS + textwrap.dedent(f"""
            card = run(on_card, {mode!r}, {opacity})
            gpu = buffer.toImage()
            plain = run(on_cpu, {mode!r}, {opacity})
            print(json.dumps({{
                "diff": apart(gpu, cpu), "lit_gpu": lit(gpu),
                "lit_cpu": lit(cpu),
                "on_card": type(card._card).__name__,
                "cpu_screen": card._screen is not None}}))
        """))
        assert got["on_card"] == "Tube" and not got["cpu_screen"], (
            "the scope kept its screen on the CPU")
        assert got["lit_cpu"] > 100, "the trace drew nothing to compare"
        assert got["diff"] < 0.01, (
            f"the card and the CPU draw the scope {got['diff']:.4f} apart "
            f"in brightness on average")
        assert abs(got["lit_gpu"] - got["lit_cpu"]) < 0.1 * got["lit_cpu"], (
            got["lit_gpu"], got["lit_cpu"])

    def test_a_window_resized_while_paused_keeps_its_picture(self):
        """Scaled to the new size, as the CPU's screen is, rather than started
        dark: a paused track strikes nothing new, so dragging the window's
        edge would otherwise wipe the scope."""
        got = on_the_card(SCOPE_BOTH_WAYS + textwrap.dedent("""
            def resized(paint_on, target):
                scene = type(visualizers.by_name("Oscilloscope"))()
                clock = [500.0]
                was = visualizers.time.monotonic
                visualizers.time.monotonic = lambda: clock[0]
                try:
                    held = traces("Sweep", 5)
                    for trace in held:
                        clock[0] += 1.0 / 15.0
                        paint_on(scene, state_for(trace, "Sweep"), 1.0,
                                 QRectF(0, 0, W, H))
                    # Paused: the same trace again, in a smaller window.
                    clock[0] += 1.0 / 15.0
                    paused = state_for(held[-1], "Sweep")
                    paused.trace = held[-1]
                    paint_on(scene, paused, 1.0, QRectF(0, 0, 700, 420))
                finally:
                    visualizers.time.monotonic = was

            def card_in(scene, state, opacity, rect):
                buffer.bind()
                p = QPainter(device)
                p.fillRect(QRectF(0, 0, W, H), QColor(0, 0, 0))
                scene.paint(p, rect, state)
                p.end()
                buffer.release()

            def cpu_in(scene, state, opacity, rect):
                cpu.fill(QColor(0, 0, 0))
                p = QPainter(cpu)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                scene.paint(p, rect, state)
                p.end()

            resized(card_in, None)
            gpu = buffer.toImage()
            resized(cpu_in, None)
            print(json.dumps({"lit_gpu": lit(gpu), "lit_cpu": lit(cpu),
                              "diff": apart(gpu, cpu)}))
        """))
        assert got["lit_cpu"] > 100
        assert got["lit_gpu"] > 0.8 * got["lit_cpu"], (
            f"resized while paused, the scope on the card lit "
            f"{got['lit_gpu']} against the CPU's {got['lit_cpu']}")
        assert got["diff"] < 0.01, got["diff"]

    def test_its_screen_is_only_as_big_as_the_beam_reaches(self):
        """On a wide screen a quarter of the pixels are where no beam can go,
        and fading them costs as much as any others."""
        import scope_gl

        assert scope_gl.reach(2880, 1800) == (2160, 1800)
        assert scope_gl.reach(1000, 1600) == (1000, 1200)
        assert scope_gl.reach(500, 500) == (500, 500)

    def test_a_card_that_will_not_keep_it_hands_it_to_the_cpu(self):
        """Once, and the scope goes on drawing."""
        got = on_the_card(SCOPE_BOTH_WAYS + textwrap.dedent("""
            import scope_gl
            def broken(*args, **kwargs):
                raise RuntimeError("a card that will not keep a screen")
            scope_gl.Tube.draw = broken
            card = run(on_card, "Sweep")
            gpu = buffer.toImage()
            print(json.dumps({"card": card._card, "lit": lit(gpu)}))
        """))
        assert got["card"] is False
        assert got["lit"] > 100, "the scope drew nothing after the card failed"


class TestABigScreenOnASmallCard:
    """Fewer samples, then fewer pixels, when the card cannot keep up.

    Measured on an M1 for Music rider at every real pixel with four samples
    a pixel: 8.9 ms on a MacBook Air's screen, 13.8 on a 16-inch MacBook
    Pro's, and 22.7 and 32.3 on 5K and 6K displays (44 and 31 frames a
    second). See CardSharpness.
    """

    @staticmethod
    def _card():
        from attachment_widgets import CardSharpness

        card = CardSharpness()
        card.choice(5120 * 2880, 2.0, "rider")
        return card

    @staticmethod
    def _judge(card, *frames, ratio=2.0):
        """Everything it takes for one rung to be judged, at these costs."""
        for ms in (list(frames) * card.WINDOW)[:card.WINDOW]:
            card.record(ms, ratio)

    @staticmethod
    def _past_the_warmup(card, ratio=2.0):
        for _ in range(card.WARMUP):
            card.record(99.0, ratio)

    def test_it_starts_at_every_pixel_and_four_samples(self):
        assert self._card().choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def test_it_gives_up_samples_and_never_pixels(self):
        """Two samples a pixel at Retina density keep most of the smoothness of
        four, and none still draws every pixel. Half the pixels is a soft
        picture, which it used to drop to in a track's first seconds while
        the analysis was busy."""
        card = self._card()
        self._past_the_warmup(card)
        seen = [card.choice(5120 * 2880, 2.0, "rider")]
        for _ in range(4):
            self._judge(card, 30.0)
            for _ in range(card.SETTLE):
                card.record(30.0, 2.0)
            seen.append(card.choice(5120 * 2880, 2.0, "rider"))
        assert seen == [(4, 1.0), (2, 1.0), (0, 1.0), (0, 1.0), (0, 1.0)]
        assert all(share == 1.0 for _, share in seen)

    def test_a_screen_at_one_pixel_a_point_keeps_its_pixels(self):
        """On a screen with one pixel per point, half the pixels would be
        coarser than the window around it; it gives up samples instead, all
        of them if need be."""
        from attachment_widgets import CardSharpness

        for ratio in (1.0, 1.5, 2.0, 3.0):
            assert all(share >= 1.0 for _, share in CardSharpness.rungs(ratio))

    def test_a_frame_that_fits_stays_where_it_is(self):
        card = self._card()
        self._past_the_warmup(card)
        for _ in range(5):
            self._judge(card, 9.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def test_one_slow_frame_in_thirty_is_not_a_slow_rung(self):
        """The system takes a frame for something else now and then. A running
        average halved a MacBook Pro screen's resolution over that, for a
        frame that fitted nine times in ten; a median does not."""
        card = self._card()
        self._past_the_warmup(card)
        self._judge(card, *([9.0] * 26 + [60.0] * 4))
        assert card.choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def test_the_first_frames_are_not_believed(self):
        """A new scene's first frames are the card setting it up."""
        card = self._card()
        for _ in range(card.WARMUP):
            card.record(200.0, 2.0)
        self._judge(card, 9.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def _down_once(self, card, ratio=2.0):
        """Past the warm-up, then one rung measured over the budget."""
        self._past_the_warmup(card, ratio)
        self._judge(card, 20.0, ratio=ratio)
        for _ in range(card.SETTLE):
            card.record(4.0, ratio)

    def test_a_rung_that_did_not_fit_is_not_tried_again_for_a_while(self):
        """Or the picture would go soft and sharp by turns."""
        card = self._card()
        self._down_once(card)
        frames = 0
        while frames + card.WINDOW < card.RETRY - card.SETTLE:
            self._judge(card, 4.0)
            frames += card.WINDOW
        assert card.choice(5120 * 2880, 2.0, "rider") == (2, 1.0)

    def test_it_climbs_back_when_the_frames_say_there_is_room(self):
        """What drove it down may have been something else on the machine (an
        analysis, a sync, another app); without this, one busy moment kept a
        full screen soft for the rest of the session."""
        card = self._card()
        self._down_once(card)
        for _ in range(card.RETRY // card.WINDOW + 2):
            self._judge(card, 4.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def test_it_does_not_climb_when_the_frames_say_there_is_not(self):
        """10 ms at two samples is 15 at four, over the budget: trying it
        would only be a stutter."""
        card = self._card()
        self._down_once(card)
        for _ in range(3 * card.RETRY // card.WINDOW):
            self._judge(card, 10.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (2, 1.0)

    def test_every_failed_climb_doubles_the_wait(self):
        """A frame that fits below and not above is tried again, but
        less and less often: five seconds, then ten, then twenty."""
        card = self._card()
        self._down_once(card)
        climbs = []
        for window in range(int(8 * card.RETRY / card.WINDOW)):
            before = card.choice(5120 * 2880, 2.0, "rider")
            # Cheap below, too dear above: every climb fails.
            self._judge(card, 4.0 if before == (2, 1.0) else 20.0)
            if card.choice(5120 * 2880, 2.0, "rider") != before \
                    and before == (2, 1.0):
                climbs.append(window)
            for _ in range(card.SETTLE if card._settle else 0):
                card.record(4.0, 2.0)
        gaps = [b - a for a, b in zip(climbs, climbs[1:])]
        assert len(climbs) >= 3, (
            f"it only tried to climb at windows {climbs}")
        assert all(later > earlier * 1.5 for earlier, later
                   in zip(gaps, gaps[1:])), (
            f"it tried to climb at windows {climbs}: the waits between "
            f"tries are not growing")

    def test_a_new_scene_starts_where_this_size_fitted(self):
        """Not at the top every time, stepping down through a second of
        slow frames at every change of scene on a big display."""
        card = self._card()
        self._past_the_warmup(card)
        self._judge(card, 30.0)
        for _ in range(card.SETTLE):
            card.record(30.0, 2.0)
        self._judge(card, 9.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (2, 1.0)
        assert card.choice(5120 * 2880, 2.0, "tunnel") == (2, 1.0)
        assert card.choice(2560 * 1600, 2.0, "tunnel") == (4, 1.0), (
            "a size nothing has been measured at starts at the top")

    def test_a_new_scene_or_a_new_size_starts_sharp_again(self):
        card = self._card()
        self._past_the_warmup(card)
        self._judge(card, 30.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (2, 1.0)
        assert card.choice(5120 * 2880, 2.0, "tunnel") == (4, 1.0)
        self._past_the_warmup(card)
        self._judge(card, 30.0)
        assert card.choice(2560 * 1600, 2.0, "tunnel") == (4, 1.0)

    def test_the_timer_is_told_what_a_frame_costs(self):
        """A timer asking for a frame every 16 ms of a card taking 25
        does not get them any faster; it fills the event queue, and the
        pane stops answering the mouse."""
        card = self._card()
        assert card.interval_ms(16) == 16
        self._past_the_warmup(card)
        self._judge(card, 25.0)
        assert card.interval_ms(16) > 25
        assert card.interval_ms(16) <= 33

    def test_a_frame_drawn_smaller_fills_the_screen_and_stays_crisp(self):
        """Stretched to the whole screen without smoothing: at half the pixels
        of a 2x screen a line one buffer pixel wide comes out exactly two
        real pixels wide at full brightness, not three or four of fading
        grey (see blit_scene).
        """
        got = on_the_card(HAIRLINE + textwrap.dedent("""
            made = pane(scene=Hairline(), size=(640, 400))
            made._card.choice = lambda pixels, ratio, scene: (4, 0.5)
            for i in range(3):
                made._tick()
            shot = made._canvas.grabFramebuffer()
            r = made._canvas.devicePixelRatioF()
            y = shot.height() // 2
            lit = {x: round(lightness(shot, x, y), 2)
                   for x in range(shot.width())
                   if lightness(shot, x, y) > 0.05}
            print(json.dumps({"ratio": r,
                              "size": [shot.width(), shot.height()],
                              "lit": lit}))
        """))
        if got["ratio"] < 2.0:
            pytest.skip("half the pixels is the logical resolution only "
                        "on a 2x screen")
        assert got["size"] == [1280, 800]
        lit = {int(x): v for x, v in got["lit"].items()}
        assert sorted(lit) == [640, 641], (
            f"a line one buffer pixel wide at the middle of the pane came "
            f"out at {sorted(lit)}: it should be two real pixels at the "
            f"middle of the screen")
        assert min(lit.values()) > 0.8, (
            f"and at full brightness, not smoothed into grey: {lit}")

    def test_each_tick_is_drawn_once(self):
        """Repainting the pane as well as the canvas drew every frame twice
        (the pane's paint asks the canvas again a frame later): 120 frames a
        second from a timer asking for 60, and twice the card's work."""
        got = on_the_card("""
            import time
            made = pane(size=(640, 400))
            made._timer.stop()
            made.show()
            drawn = []
            real = made._paint_on_gpu
            made._paint_on_gpu = lambda canvas: (drawn.append(1),
                                                 real(canvas))

            def settle():
                end = time.monotonic() + 0.05
                while time.monotonic() < end:
                    app.processEvents()
                    time.sleep(0.002)

            for i in range(10):
                made._tick()
                settle()
            drawn.clear()
            for i in range(30):
                made._tick()
                settle()
            print(json.dumps({"drawn": len(drawn)}))
        """)
        assert got["drawn"] <= 33, (
            f"thirty ticks drew {got['drawn']} frames")
        assert got["drawn"] >= 27, (
            f"thirty ticks drew only {got['drawn']} frames")

    def test_asked_again_with_nothing_new_it_puts_up_what_it_drew(self):
        """Qt asks for the canvas again whenever a widget over it changes
        (the full-screen controls' seek bar, every 50 ms): drawn afresh each
        time, each cost a whole frame of the card. The picture drawn is put
        up again instead, and the next tick draws a new one."""
        got = on_the_card("""
            import time
            made = pane(size=(320, 200))
            # Running, as it is whenever frames come, but not ticking here.
            made._timer.start(10 ** 7)
            made.show()

            def settle(seconds=0.1):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    app.processEvents()
                    time.sleep(0.002)

            # Until the strip has finished sliding open, which is new each
            # frame.
            settle(1.0)
            scenes = []
            real = made._paint
            made._paint = lambda painter: (scenes.append(1), real(painter))
            made._tick()
            settle()
            first = made._canvas.grabFramebuffer()
            drawn = len(scenes)
            made._canvas.update()
            settle()
            again = made._canvas.grabFramebuffer()
            asked_again = len(scenes) - drawn
            made._tick()
            settle()
            print(json.dumps({"drawn": drawn, "asked_again": asked_again,
                              "same": first == again,
                              "next_tick": len(scenes) - drawn
                              - asked_again}))
        """)
        assert got["drawn"] >= 1
        assert got["asked_again"] == 0, (
            f"the scene was drawn {got['asked_again']} more times with "
            f"nothing new to show")
        assert got["same"], "and what was put up again is not what was drawn"
        assert got["next_tick"] >= 1, "and the next tick drew nothing"

    def test_a_widget_over_it_changing_asks_for_no_frame(self):
        """The pane asked the canvas for a frame from its own paint, which Qt
        runs for a widget over the picture that changes, so each such change
        drew a frame for nothing and put the next one a refresh late. Qt
        itself puts the window together around the canvas's last frame
        here, and on a hosted runner drew the canvas once as well."""
        got = on_the_card("""
            import time
            from PySide6.QtWidgets import QLabel
            made = pane(size=(320, 200))
            made._timer.start(10 ** 7)
            label = QLabel("over the picture", made)
            label.move(10, 10)
            made.show()

            def settle(seconds=0.1):
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    app.processEvents()
                    time.sleep(0.002)

            # Until the strip has finished sliding open.
            settle(1.0)
            asked = []
            real = made._paint_on_gpu
            made._paint_on_gpu = lambda canvas: (asked.append(1), real(canvas))
            requested = []
            real_update = made._canvas.update
            made._canvas.update = lambda *how: (requested.append(1),
                                                real_update(*how))
            label.setText("changed over the picture")
            settle()
            print(json.dumps({"asked": len(asked),
                              "requested": len(requested)}))
        """)
        assert got["requested"] == 0, (
            f"the pane asked the canvas for {got['requested']} frames when "
            f"a widget over the picture changed")
        assert got["asked"] <= 1, (
            f"one change over the picture drew {got['asked']} frames")

    def test_one_frame_in_four_waits_for_the_card(self):
        """Every frame waited for the card to finish, to be measured, which
        held the app's own thread for 9 to 12 ms of every sixtieth at full
        screen."""
        got = on_the_card("""
            made = pane(size=(320, 200))
            measured = []
            real = made._card.record
            made._card.record = lambda ms, ratio: (measured.append(ms),
                                                   real(ms, ratio))
            for i in range(40):
                made._tick()
                made._canvas.grabFramebuffer()
            print(json.dumps({"measured": len(measured),
                              "every": made.MEASURE_EVERY}))
        """)
        assert got["every"] == 4
        assert got["measured"] == 10, (
            f"forty frames were measured {got['measured']} times")

    def test_a_measured_frame_is_measured_alone(self):
        """Nothing waits for the card between frames, so frames asked for
        faster than it finishes them queue up. A frame measured with work
        still queued ahead of it counted that work as its own: the rave at
        full screen read 13 ms a frame against its own 10, and lost half
        its samples. Here the card is handed a pile of work no one waits
        for just before a measured frame."""
        got = on_the_card("""
            import time
            from PySide6.QtOpenGL import QOpenGLFramebufferObject
            made = pane(size=(320, 200))
            for i in range(6):
                made._tick()
                made._canvas.grabFramebuffer()
            canvas = made._canvas
            canvas.makeCurrent()
            gl = canvas.context().functions()
            side = 2048
            one = QOpenGLFramebufferObject(QSize(side, side))
            two = QOpenGLFramebufferObject(QSize(side, side))
            whole = QRect(0, 0, side, side)
            def pile(n):
                for i in range(n):
                    QOpenGLFramebufferObject.blitFramebuffer(
                        two if i % 2 else one, whole,
                        one if i % 2 else two, whole, 0x4000, 0x2601)
            def left(n):
                # How much of the pile is still to do once it has been
                # handed over.
                gl.glFinish()
                pile(n)
                started = time.perf_counter()
                gl.glFinish()
                return (time.perf_counter() - started) * 1000.0
            measured = []
            made._card.record = lambda ms, ratio: measured.append(ms)
            def frame(queued):
                made._tick()
                made._picture_due = True
                made._card_frames = made.MEASURE_EVERY - 1
                gl.glFinish()
                if queued:
                    pile(count)
                made._paint_on_gpu(canvas)
            for i in range(3):
                frame(False)
            own = sorted(measured)[1]
            count = 8
            leftover = left(count)
            while leftover < max(30.0, own * 4) and count < 8192:
                count *= 2
                leftover = left(count)
            measured.clear()
            for i in range(3):
                frame(True)
            canvas.doneCurrent()
            print(json.dumps({"own": own, "leftover": leftover,
                              "queued": sorted(measured)[1]}))
        """)
        if got["leftover"] < got["own"] * 3:
            pytest.skip(f"the card here finishes the pile as it is handed "
                        f"over ({got['leftover']:.0f} ms left, against "
                        f"{got['own']:.0f} ms a frame)")
        assert got["queued"] < got["own"] + got["leftover"] / 2, (
            f"a frame of {got['own']:.1f} ms measured {got['queued']:.1f} "
            f"with {got['leftover']:.0f} ms of other work queued ahead of it")

    def test_a_card_too_slow_for_its_screen_draws_less(self):
        """A frame the card cannot finish in its budget is measured on the
        card, and the pane gives up samples and slows its timer to what the
        card can do."""
        got = on_the_card("""
            import time
            class Heavy:
                name = "Heavy"
                blurb = "test"
                sharp_pixels = 0
                def reset(self):
                    pass
                def paint(self, painter, rect, state):
                    painter.fillRect(rect, QColor(20, 20, 40))
                    time.sleep(0.02)
            made = pane(scene=Heavy(), size=(640, 400))
            card = made._card
            every = made.MEASURE_EVERY
            for i in range((card.WARMUP + card.WINDOW + 2) * every):
                made._tick()
                made._canvas.grabFramebuffer()
            made._tick()
            print(json.dumps({"gpu": made.on_gpu,
                              "rung": list(card._rung),
                              "timer": made._timer.interval()}))
        """)
        assert got["gpu"] is True
        assert got["rung"] == [2, 1.0], (
            f"a card taking over 20 ms a frame is still at {got['rung']}")
        assert got["timer"] > 20, (
            f"and the timer is still asking every {got['timer']} ms")


#: What the scripts below have before their own bodies: the screen's
#: refreshes counted as the pane is handed them.
REFRESHES = textwrap.dedent("""
    import gc, platform, time
    import refreshes_mac
    if not refreshes_mac.available():
        if int(platform.mac_ver()[0].split(".")[0]) < 14:
            print(json.dumps({"skip": "this Mac hands out no refreshes: a "
                                      "view makes them from macOS 14"}))
        else:
            print(json.dumps({"unanswered": refreshes_mac._rt().missing(
                refreshes_mac.NEEDED)}))
        sys.stdout.flush()
        os._exit(0)
    calls = []
    real_refreshed = Spectrum._refreshed

    def counted(self, stamp):
        calls.append(stamp)
        return real_refreshed(self, stamp)

    Spectrum._refreshed = counted

    def settle(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            app.processEvents()
            time.sleep(0.002)

    _kept = []

    def screen_refreshes(window, seconds):
        # How many refreshes the system sends ``window``'s view over the
        # next ``seconds``, through a display link made with the runtime and
        # not refreshes_mac, beside the pane's own. A sleeping screen sends
        # none; a hosted runner's virtual one sent a window like the pane's
        # two in a second and a half, and one shown before it at least half
        # its rate.
        import ctypes
        import touchbar_mac
        rt = touchbar_mac._Runtime()
        counted = []
        imp = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                               ctypes.c_void_p)(
            lambda _self, _cmd, _link: counted.append(1))
        _kept.append(imp)
        name = b"TestRefreshCounter%d" % len(_kept)
        made = rt.objc.objc_allocateClassPair(rt.cls("NSObject"), name, 0)
        rt.objc.class_addMethod(made, rt.sel("counted:"),
                                ctypes.cast(imp, ctypes.c_void_p), b"v@:@")
        rt.objc.objc_registerClassPair(made)
        target = rt.send(rt.send(made, "alloc"), "init")
        link = rt.send(int(window.winId()), "displayLinkWithTarget:selector:",
                       target, rt.sel("counted:"),
                       argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        rt.send(link, "retain")
        foundation = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Foundation.framework/Foundation")
        modes = ctypes.c_void_p.in_dll(foundation,
                                       "NSRunLoopCommonModes").value
        rt.send(link, "addToRunLoop:forMode:",
                rt.send(rt.cls("NSRunLoop"), "mainRunLoop"), modes,
                restype=None, argtypes=[ctypes.c_void_p, ctypes.c_void_p])
        settle(seconds)
        rt.send(link, "invalidate", restype=None)
        return len(counted)

    def sent_or_skip(window, seconds):
        # The refreshes the system sends the window meanwhile, beside the
        # pane's: skipped where it sends too few to judge the pane by.
        rate = window.screen().refreshRate()
        sent = screen_refreshes(window, seconds)
        if sent < rate * seconds * 0.5:
            print(json.dumps({"skip": "this screen sent the window %d "
                                      "refreshes in %.1fs at %.0f a second: "
                                      "it is asleep, or a hosted runner's"
                                      % (sent, seconds, rate)}))
            sys.stdout.flush()
            os._exit(0)
        return sent
""")


class TestTheScreensRefreshesKeepTheFrames:
    """On the card each frame starts at a refresh of the screen, from a
    display link the window's view makes. See refreshes_mac."""

    def test_they_come_and_each_is_a_tick(self):
        got = on_the_card(REFRESHES + textwrap.dedent("""
            made = pane(size=(320, 200))
            made.show()
            ticks = []
            real_tick = made._tick
            made._tick = lambda: (ticks.append(1), real_tick())
            settle(0.5)
            calls.clear()
            ticks.clear()
            sent = sent_or_skip(made, 1.5)
            print(json.dumps({"refreshes": len(calls), "sent": sent,
                              "ticks": len(ticks),
                              "following": made._refreshes.running,
                              "keeping": made._refreshing()}))
        """))
        assert "unanswered" not in got, (
            f"this Mac does not answer {got.get('unanswered')}")
        assert got["following"]
        assert got["keeping"], (
            "the refreshes came but the timer kept the frames", got)
        assert got["refreshes"] >= got["sent"] * 0.8, (
            f"the pane was handed {got['refreshes']} of the {got['sent']} "
            f"refreshes its window was sent")
        assert abs(got["ticks"] - 90) <= 12, (
            f"{got['ticks']} ticks in a second and a half")

    def test_a_pane_let_go_of_lets_them_go(self):
        """With its window, which takes the view the refreshes came from:
        no refresh comes to find the pane gone, so the link goes with the
        pane, not left running for the rest of the session."""
        got = on_the_card(REFRESHES + textwrap.dedent("""
            made = pane(size=(320, 200))
            made.show()
            settle(0.5)
            following = made._refreshes.running
            del made
            gc.collect()
            settle(0.5)
            print(json.dumps({"following": following,
                              "links": len(refreshes_mac._links)}))
        """))
        assert got["following"]
        assert got["links"] == 0, f"{got['links']} links were left running"

    def test_they_follow_the_pane_into_another_window(self):
        """As going full screen moves it, perhaps to another screen."""
        got = on_the_card(REFRESHES + textwrap.dedent("""
            home, away = QWidget(), QWidget()
            for window in (home, away):
                window.resize(400, 300)
            made = pane(size=(320, 200))
            made.setParent(home)
            home.show()
            made.show()
            settle(0.5)
            first = made._refreshes._view == int(home.winId())
            away.show()
            made.setParent(away)
            made.show()
            settle(0.5)
            calls.clear()
            sent = sent_or_skip(away, 1.0)
            print(json.dumps({
                "first": first,
                "now": made._refreshes._view == int(away.winId()),
                "refreshes": len(calls), "sent": sent,
                "links": len(refreshes_mac._links)}))
        """))
        assert got["first"], "the refreshes were not the first window's"
        assert got["now"], "they did not follow the pane to the second"
        assert got["refreshes"] >= got["sent"] * 0.8, got
        assert got["links"] == 1, f"{got['links']} links are running"


class TestNothingIsTakenApartMidFrame:
    """A pane's canvas referred back to the pane, a cycle that Python's
    collector frees at no particular moment. Taking a GL widget apart makes
    its context current and then none, so when that happened during another
    pane's frame the painter used a context that was gone and the process
    crashed: a reopened viewer crashed three runs in three."""

    def test_a_pane_let_go_of_is_gone_at_once(self):
        got = on_the_card("""
            import weakref
            made = pane()
            for i in range(3):
                made._tick()
                made._canvas.grabFramebuffer()
            gone = weakref.ref(made)
            del made
            print(json.dumps({"alive": gone() is not None}))
        """)
        assert got["alive"] is False, (
            "a pane nothing refers to is still alive, waiting for the "
            "collector - which can come in the middle of another frame")

    def test_the_collector_waits_for_the_frame_to_finish(self):
        got = on_the_card("""
            import gc
            seen = []
            class Watching:
                name = "Watching"
                blurb = "test"
                sharp_pixels = 0
                def reset(self):
                    pass
                def paint(self, painter, rect, state):
                    seen.append(gc.isenabled())
            made = pane(scene=Watching())
            for i in range(3):
                made._tick()
                made._canvas.grabFramebuffer()
            print(json.dumps({"during": seen, "after": gc.isenabled()}))
        """)
        assert got["during"] and not any(got["during"]), (
            "the collector could run in the middle of a frame")
        assert got["after"] is True, "and was left off afterwards"


#: Three scenes that each fill the frame with one colour.
SOLIDS = textwrap.dedent("""
    class Solid:
        blurb = "test"
        sharp_pixels = 0
        def __init__(self, name, colour):
            self.name = name
            self.colour = colour
        def reset(self):
            pass
        def paint(self, painter, rect, state):
            painter.fillRect(rect, self.colour)
    red = Solid("Red", QColor(255, 0, 0))
    blue = Solid("Blue", QColor(0, 0, 255))
    green = Solid("Green", QColor(0, 255, 0))
    def middle(made):
        image = made._canvas.grabFramebuffer()
        found = image.pixelColor(image.width() // 2, image.height() // 2)
        return [found.red(), found.green(), found.blue()]
""")


class TestChangingSceneOnTheCard:
    """A new scene fades up over the last frame of the one before, so
    changing scene - however often - never passes through black."""

    def test_the_last_frame_stays_until_the_next_is_up_over_it(self):
        got = on_the_card(SOLIDS + textwrap.dedent("""
            made = pane(scene=red, size=(320, 200))
            made.set_change(0.6)
            for _ in range(3):
                made._tick()
                before = middle(made)
            made.set_scene(blue)
            seen = []
            for _ in range(150):
                made._tick()
                seen.append(middle(made))
            print(json.dumps({"before": before, "seen": seen}))
        """))
        assert got["before"] == [255, 0, 0]
        seen = got["seen"]
        assert seen[0][0] > 240 and seen[0][2] < 15, (
            f"the scene before was not held: {seen[0]}")
        # The channels add up to one full one throughout a fade between
        # these colours, and to nothing at black.
        assert all(sum(colour) > 200 for colour in seen), (
            f"the change passed through black: {min(seen, key=sum)}")
        mixed = [c for c in seen if c[0] > 60 and c[2] > 60]
        assert mixed, "the two scenes were never mixed"
        assert seen[-1][2] > 240 and seen[-1][0] < 15, seen[-1]

    def test_a_change_during_a_change_carries_on_from_what_shows(self):
        got = on_the_card(SOLIDS + textwrap.dedent("""
            made = pane(scene=red, size=(320, 200))
            made.set_change(0.6)
            made._tick()
            middle(made)
            made.set_scene(blue)
            while made._fresh < 0.45:
                made._tick()
                shown = middle(made)
            made.set_scene(green)
            after = []
            for _ in range(150):
                made._tick()
                after.append(middle(made))
            print(json.dumps({"shown": shown, "after": after}))
        """))
        shown, after = got["shown"], got["after"]
        assert shown[0] > 60 and shown[2] > 60, shown
        # The next frame is the one that was showing, not black and not
        # the new scene cut in.
        first = after[0]
        assert all(abs(a - b) <= 12 for a, b in zip(first, shown)), (
            shown, first)
        assert all(sum(colour) > 200 for colour in after), (
            min(after, key=sum))
        assert after[-1][1] > 240 and max(after[-1][0], after[-1][2]) < 15

    def test_cut_changes_on_the_next_frame(self):
        got = on_the_card(SOLIDS + textwrap.dedent("""
            made = pane(scene=red, size=(320, 200))
            made.set_change(0.0)
            made._tick()
            middle(made)
            made.set_scene(blue)
            made._tick()
            print(json.dumps({"next": middle(made)}))
        """))
        assert got["next"] == [0, 0, 255]


class TestTheSuiteStaysOnTheCpu:
    """The offscreen platform the suite runs on has no card."""

    def test_no_canvas_is_made_offscreen(self, qtbot):
        from attachment_widgets import Spectrum

        pane = Spectrum()
        qtbot.addWidget(pane)
        assert pane._canvas is None and pane.on_gpu is False

    def test_the_switch_turns_the_card_off(self, monkeypatch):
        """``MAIL_MANAGER_GPU=0``, for anybody chasing a drawing problem
        who wants to know whether the card is part of it."""
        import attachment_widgets

        monkeypatch.setattr(attachment_widgets, "_platform_name",
                            lambda: "cocoa")
        monkeypatch.setenv("MAIL_MANAGER_GPU", "1")
        assert attachment_widgets._gpu_wanted() is True
        monkeypatch.setenv("MAIL_MANAGER_GPU", "0")
        assert attachment_widgets._gpu_wanted() is False

    def test_offscreen_and_minimal_never_ask_for_one(self, monkeypatch):
        import attachment_widgets

        monkeypatch.setenv("MAIL_MANAGER_GPU", "1")
        for name in ("", "offscreen", "minimal", "vnc"):
            monkeypatch.setattr(attachment_widgets, "_platform_name",
                                lambda name=name: name)
            assert attachment_widgets._gpu_wanted() is False, name
        monkeypatch.setattr(attachment_widgets, "_platform_name",
                            lambda: "cocoa")
        assert attachment_widgets._gpu_wanted() is True
