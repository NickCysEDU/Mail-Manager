"""The visualiser pane, drawn on the graphics card.

"Xxxxxxxx xxxxxxxxxx xx xxxx xxxxxx xx xxx xxxxx xx xxxxx." It was not
sharp because it could not afford to be: at a Retina full screen - 2880x1800
real pixels - Music rider cost 42 ms a frame on the CPU with its bloom
and vignette, so the pane drew it at half the resolution and stretched it.
On the card the same QPainter calls cost 10 ms at the full resolution.

The rest of the suite runs on the offscreen platform, where no OpenGL
context can be made - so the canvas is never created there, and every
test of what the scenes draw goes through the CPU path, which is also
what any machine without a working card falls back to. This file is the
one that runs the card. Each test launches a short script on the real
platform in a subprocess, reports back as JSON, and skips on a machine
that cannot make a context at all.
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


#: A line one real pixel wide down the middle of the pane, drawn in
#: whatever pixels the painter has - which is how sharpness is measured.
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


def on_the_card(body: str) -> dict:
    """Run ``body`` on the real platform and give back what it printed."""
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    env["MAIL_MANAGER_GPU"] = "1"
    done = subprocess.run(
        [sys.executable, "-c", HEAD + textwrap.dedent(body)],
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
    return found


class TestThePaneIsOnTheCard:
    def test_it_draws_at_the_screens_own_resolution(self):
        """Every real pixel, rather than one buffer pixel per point
        stretched - which is what made a Retina full screen soft.

        Measured on a line one real pixel wide, which stays one pixel
        wide only if nothing was drawn small and stretched. Measuring
        the size of the frame instead passes either way: a stretched
        frame is exactly as big as a sharp one.
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
        """The bloom is a small copy of the frame, made big again. An
        eightfold shrink done in one linear blit reads one pixel in
        sixteen, so a thin line reached the small copy only where it
        crossed the pixels that were read - and the bloom strung a row
        of soft blobs along every chevron on the road. In halves, each
        step is an exact average.

        Read off the small copy the pane itself made, through its own
        path. The first version of this ran its own copy of the halving
        loop, and passed with the pane's loop deleted.
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
        """A framebuffer counts its rows from the bottom and a painter
        from the top. With the scene centred the two agree, so the scene
        here sits high - the strip under it reserved for the controls,
        which is how it sits in the app - and is lit only in its top
        quarter. The small copy the bloom is made from has to be lit at
        its top as well. Read the wrong way up, it is made from a band
        of the frame the scene is not even in.
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
        # Lit across the whole of its top, not half of it: a frame
        # drawn smaller has fewer pixels per point, and reading it as
        # if it had as many takes the copy from a patch of the frame
        # twice the size, with the scene in one corner of it.
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
        """Full screen takes the pane out of the window and puts it in
        another - which can mean a new context, and a canvas that kept
        buffers built on the old one would draw nothing."""
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


class TestABigScreenOnASmallCard:
    """Fewer samples, then fewer pixels, when the card cannot keep up.

    Measured on an M1 for Music rider at every real pixel with four
    samples a pixel: 8.9 ms on a MacBook Air's screen, 13.8 on a 16-inch
    MacBook Pro's, and 22.7 and 32.3 on 5K and 6K displays - 44 and 31
    frames a second. See CardSharpness.
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

    def test_it_gives_up_samples_before_pixels(self):
        """Two samples a pixel at a Retina density is most of the
        smoothness of four; half the pixels is a picture that has gone
        soft. So the samples go first, and the resolution only goes to
        the screen's logical resolution - a whole-number stretch."""
        card = self._card()
        self._past_the_warmup(card)
        seen = [card.choice(5120 * 2880, 2.0, "rider")]
        for _ in range(4):
            self._judge(card, 30.0)
            for _ in range(card.SETTLE):
                card.record(30.0, 2.0)
            seen.append(card.choice(5120 * 2880, 2.0, "rider"))
        assert seen == [(4, 1.0), (2, 1.0), (4, 0.5), (2, 0.5), (2, 0.5)]

    def test_a_screen_at_one_pixel_a_point_keeps_its_pixels(self):
        """Half the pixels of a screen that has one per point is a
        picture below the window it sits in. It gives up its samples
        instead, all of them if it has to."""
        from attachment_widgets import CardSharpness

        assert CardSharpness.rungs(1.0) == ((4, 1.0), (2, 1.0), (0, 1.0))
        assert all(share >= 1.0 for _, share in CardSharpness.rungs(1.0))

    def test_a_frame_that_fits_stays_where_it_is(self):
        card = self._card()
        self._past_the_warmup(card)
        for _ in range(5):
            self._judge(card, 9.0)
        assert card.choice(5120 * 2880, 2.0, "rider") == (4, 1.0)

    def test_one_slow_frame_in_thirty_is_not_a_slow_rung(self):
        """The system takes a frame for something else now and then. A
        running average moved a MacBook Pro's own screen to half its
        resolution over that, for a frame that fitted nine times in
        ten; a median does not."""
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
        """Whatever drove it down may have been something else on the
        machine - an analysis, a sync, another app. Without this, one busy
        moment kept a full screen soft for the rest of the session."""
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
        """Stretched to the whole screen, and without smoothing: at half
        the pixels of a 2x screen every buffer pixel is exactly two, and
        a line one buffer pixel wide comes out exactly two real pixels
        wide at full brightness, rather than three or four of fading
        grey. See blit_scene for why that matters more than it sounds.
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
        """Asking the pane to repaint as well as the canvas drew every
        frame twice - the pane's own paint asks the canvas again a frame
        later - which is 120 frames a second off a timer asking for 60,
        and twice the card's work for nothing."""
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
        """A frame the card cannot finish in its budget is measured as
        one, on the card, and the pane gives up samples for it and slows
        its timer to what the card can actually do."""
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
    """A pane's canvas held the pane back, which made the two a cycle;
    a cycle is freed by Python's collector whenever it next runs, and it
    runs when enough has been allocated - at no particular moment. Taking
    a GL widget apart makes its context current and then none at all, so
    when that happened in the middle of another pane's frame the painter
    dereferenced a context that was no longer there and the process
    crashed: a second pane drawing while the first was dropped, which is
    a viewer reopened, crashed three runs in three."""

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
