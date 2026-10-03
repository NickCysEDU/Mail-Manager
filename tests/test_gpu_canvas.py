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


def on_the_card(body: str) -> dict:
    """Run ``body`` on the real platform and return what it printed.

    Fails if it did not finish cleanly: an exception that escaped into Qt,
    or a crash on the way out, is a fault even where the numbers printed
    look right."""
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
        """The bloom is a small copy of the frame, made big again. An eightfold
        shrink in one linear blit reads one pixel in sixteen, so a thin line
        reached the small copy only where it crossed the pixels read, and
        the bloom strung soft blobs along every chevron. In halves, each
        step is an exact average.

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
        lit only in its top quarter. The bloom's small copy must be lit at
        its top too; read upside down, it comes from a band the scene is not
        in.
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
            for i in range(card.WARMUP + card.WINDOW + 2):
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
