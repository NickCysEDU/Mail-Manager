"""Music rider's lit world, on the graphics card. See rider_gl.

Two kinds of test. The world's own bookkeeping - which block was taken,
what a hit or a pickup turns into - is plain Python and runs with the
rest of the suite on the offscreen platform. What it draws can only be
seen on a card, so those run through the card harness in
test_gpu_canvas: a script on the real platform in a subprocess, which
skips on a machine that cannot make a context.
"""

from __future__ import annotations

import math
import textwrap

import pytest

from test_gpu_canvas import on_the_card

#: A pane playing the rider to a steady 120 bpm kick, which the game lays
#: its road from. Everything after it runs on a stepped clock, so the
#: game is the same game on every machine.
RIDER = textwrap.dedent("""
    import time
    import beatmap
    import visualizers

    def kick_track(bpm=120.0, seconds=300.0):
        step = 60.0 / bpm
        beats = tuple(beatmap.Beat(at=i * step + 0.25, strength=1.0)
                      for i in range(int(seconds / step)))
        return beatmap.BeatMap(beats=beats, bpm=bpm, locked=True)

    class Clock:
        def __init__(self):
            self.now = 5000.0
            self._was = time.monotonic
        def __enter__(self):
            time.monotonic = lambda: self.now
            return self
        def __exit__(self, *_):
            time.monotonic = self._was
        def step(self, seconds):
            self.now += seconds

    def rider_pane(size=(960, 600), rung=None):
        kick = kick_track()
        made = pane(size=size)
        made.set_beats({"Kick": kick})
        made.set_elements({"Kick": kick})
        if rung is not None:
            made._card.choice = lambda pixels, ratio, scene: rung
        return made, made._scene

    def play(made, seconds, start=10.0, fps=60, each=None):
        shot = None
        with Clock() as clock:
            for i in range(int(seconds * fps)):
                clock.step(1.0 / fps)
                made.set_position(int((start + i / fps) * 1000))
                if each is not None:
                    each(i)
                made._tick()
                shot = made._canvas.grabFramebuffer()
        return shot

    def mean_colour(image, box=None):
        w, h = image.width(), image.height()
        x0, y0, x1, y1 = box or (0, 0, w, h)
        r = g = b = n = 0
        for y in range(y0, y1, max(1, (y1 - y0) // 40)):
            for x in range(x0, x1, max(1, (x1 - x0) // 40)):
                c = image.pixelColor(x, y)
                r += c.redF(); g += c.greenF(); b += c.blueF(); n += 1
        return (r / n, g / n, b / n)
""")


# ==========================================================================
# The world's bookkeeping, offscreen
# ==========================================================================
def _bare_world():
    """A RiderWorld without a card: only what its bookkeeping needs."""
    import random
    from array import array

    import rider_gl

    world = rider_gl.RiderWorld.__new__(rider_gl.RiderWorld)
    world._done_seen = set()
    world._taken = set()
    world._taken_now = []
    world._seen_pops = set()
    world._particles = array("f", [0.0] * (world.PARTICLES * 12))
    world._particle_next = 0
    world._particles_dirty = False
    world._rng = random.Random(1)
    world._now = 1.0
    world._screen_ship = (0.5, 0.3)
    for name in ("_shock_hard", "_knock", "_split", "_flash", "_trim",
                 "_bloom_bump", "_flash_lane"):
        setattr(world, name, 0.0)
    world._shock = 9.0
    world._shock_at = (0.5, 0.5)
    world._flash_colour = (1.0, 1.0, 1.0)
    world._trim_colour = (1.0, 1.0, 1.0)
    return world


def _rider():
    import visualizers

    scene = visualizers.Rider()
    scene._energy = ()
    return scene


class TestTheCraft:
    def test_it_is_a_whole_model_facing_down_the_road(self):
        import rider_gl

        body = rider_gl.ship_triangles()
        glass = rider_gl.canopy_triangles()
        assert len(body) % 3 == 0 and len(glass) % 3 == 0
        xs = [x for x, _y, _z in body]
        zs = [z for _x, _y, z in body]
        # Longer than it is wide, and pointing away from the camera.
        assert max(zs) - min(zs) > max(xs) - min(xs)
        assert min(zs) < -0.9, "the nose is not at the front"
        assert abs(max(xs) + min(xs)) < 1e-6, "the craft is lopsided"

    def test_every_face_has_a_normal(self):
        import rider_gl

        flat = rider_gl._flat_normals(rider_gl.ship_triangles())
        for index in range(0, len(flat), 6):
            n = flat[index + 3:index + 6]
            assert abs(math.sqrt(sum(c * c for c in n)) - 1.0) < 1e-6


class TestWhatWasTaken:
    """The game marks a block done when its moment passes, taken or not.
    The world draws a taken one going into the ship and a missed one
    going on past it, so it has to tell them apart."""

    def test_one_in_the_craft_s_lane_was_taken(self, qapp):
        scene = _rider()
        scene._lane_here = scene._lane_at(1)
        taken = [5.0, 1, "block", True, False]
        missed = [5.0, 2, "block", True, False]
        scene._blocks = [taken, missed]
        world = _bare_world()
        world._notice(scene)
        assert id(taken) in world._taken
        assert id(missed) not in world._taken
        assert len(world._taken_now) == 1

    def test_it_is_noticed_once(self, qapp):
        scene = _rider()
        scene._lane_here = scene._lane_at(1)
        scene._blocks = [[5.0, 1, "block", True, False]]
        world = _bare_world()
        world._notice(scene)
        world._notice(scene)
        assert world._taken_now == []

    def test_what_the_game_drops_is_forgotten(self, qapp):
        scene = _rider()
        scene._lane_here = scene._lane_at(1)
        scene._blocks = [[5.0, 1, "block", True, False]]
        world = _bare_world()
        world._notice(scene)
        scene._blocks = []
        world._notice(scene)
        assert not world._taken and not world._done_seen


class TestWhatTheGameDidBecomesWhatTheWorldDoes:
    def _happened(self, kind, taken=None, hue=None):
        scene = _rider()
        scene._hue_now = 0.6
        scene._pops = [[kind, 0.0, 1.0, hue, 0.9, ""]]
        world = _bare_world()
        if taken is not None:
            world._taken_now = [("block", False, taken)]
        world._events(scene)
        return world

    def test_a_hit_throws_the_camera_and_bends_the_picture(self, qapp):
        world = self._happened("hit")
        assert world._knock == 1.0
        assert world._shock_hard == 1.0 and world._shock == 0.0
        assert world._particles_dirty, "nothing flew off"

    def test_a_pickup_lights_its_lane_in_the_colour_of_what_was_taken(
            self, qapp):
        green = (0.1, 0.9, 0.2)
        world = self._happened("prize", taken=green)
        assert world._flash == 1.0
        assert world._flash_colour == green
        assert world._trim_colour == green

    def test_a_shield_saves_you_without_throwing_you(self, qapp):
        world = self._happened("shatter")
        assert world._shock_hard > 0.0
        assert world._knock == 0.0

    def test_an_event_is_answered_once(self, qapp):
        scene = _rider()
        scene._hue_now = 0.6
        pop = ["hit", 0.0, 1.0, 0.0, 0.9, ""]
        scene._pops = [pop]
        world = _bare_world()
        world._events(scene)
        world._knock = 0.0
        world._events(scene)
        assert world._knock == 0.0, "the same hit threw the camera twice"


class TestTheSameGameEitherWay:
    """Drawn flat or drawn as a world, the game underneath is one game:
    ``paint`` and the card both move it on with ``_step``."""

    def test_stepping_is_what_painting_does_to_the_game(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import beatmap
        import visualizers
        from attachment_widgets import SpectrumState

        beats = tuple(beatmap.Beat(at=i * 0.5 + 0.25, strength=1.0)
                      for i in range(200))

        def state_at(t):
            state = SpectrumState()
            state.levels = [0.5] * 27
            state.bass = state.mid = state.high = 0.5
            state.kit = {}
            state.at = t
            state.tempo = 120.0
            state.beat_at = ((t - 0.25) / 0.5) % 1.0
            state.chart = {"Kick": tuple(b.at for b in beats)}
            return state

        clock = [100.0]
        was = visualizers.time.monotonic
        visualizers.time.monotonic = lambda: clock[0]
        try:
            painted, stepped = visualizers.Rider(), visualizers.Rider()
            image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
            for index in range(600):
                clock[0] += 1.0 / 60.0
                t = 2.0 + index / 60.0
                image.fill(QColor(0, 0, 0))
                painter = QPainter(image)
                painted.paint(painter, QRectF(0, 0, 320, 200), state_at(t))
                painter.end()
                stepped._step(state_at(t))
                stepped._camera(QRectF(0, 0, 320, 200), stepped._loudness,
                                0.5)
                if index == 300:
                    painted.steer(1)
                    stepped.steer(1)
        finally:
            visualizers.time.monotonic = was

        def game(scene):
            return (scene._score, scene._chain, scene._hits, scene._coins,
                    round(scene._at, 6), scene._lane,
                    [(round(b[0], 6), b[1], b[2], b[3]) for b in scene._blocks])

        assert painted._blocks, "the game laid nothing to compare"
        assert game(painted) == game(stepped)


# ==========================================================================
# What it draws, on the card
# ==========================================================================
class TestTheWorldOnTheCard:
    def test_it_draws_at_every_rung(self):
        """At full resolution as well as at half. The world is put into
        the frame at a viewport counted from the bottom, and a GL paint
        device reports its height in pixels already: multiplied by the
        pixel ratio again, the world was drawn off the top of the frame
        and only the words over it showed - and only at full resolution,
        which is exactly where the governor was not at when it was
        looked at."""
        got = on_the_card(RIDER + textwrap.dedent("""
            out = {}
            for rung in ((4, 1.0), (4, 0.5)):
                made, scene = rider_pane(rung=rung)
                shot = play(made, 1.2)
                out[str(rung)] = [made._canvas.world is not None,
                                  made._canvas.world_failed,
                                  sum(mean_colour(shot)) / 3.0,
                                  made._canvas.devicePixelRatioF()]
            print(json.dumps(out))
        """))
        for rung, (built, failed, bright, ratio) in got.items():
            assert built and not failed, f"no world at {rung}"
            assert bright > 0.06, (
                f"the frame at {rung} is {bright:.3f} bright: the world is "
                f"not in it")

    def test_a_block_is_drawn_where_the_game_says(self):
        """Left lane and right lane, the same block, the same moment:
        the picture differs on the side the game put it."""
        got = on_the_card(RIDER + textwrap.dedent("""
            from array import array
            shots = {}
            for lane in (0, 2):
                made, scene = rider_pane(size=(800, 500))
                def only_one(i, scene=scene, lane=lane):
                    # Nothing but this one block, a second and a bit
                    # ahead of the craft.
                    scene._placed = scene._laid = 1e9
                    scene._blocks = ([[scene._heard + 1.2, lane, "block",
                                       False, False]] if i >= 40 else [])
                shot = play(made, 0.9, each=only_one)
                shots[lane] = shot
            a, b = shots[0], shots[2]
            w, h = a.width(), a.height()
            left = right = 0.0
            for y in range(0, h, 4):
                for x in range(0, w, 4):
                    d = abs(a.pixelColor(x, y).valueF()
                            - b.pixelColor(x, y).valueF())
                    if x < w * 0.5:
                        left += d
                    else:
                        right += d
            # Where the lane-0 block is: brighter on the left in shot a.
            lit_left = sum(a.pixelColor(x, y).valueF()
                           for y in range(h // 3, h * 2 // 3, 4)
                           for x in range(0, w // 2, 4))
            lit_right = sum(a.pixelColor(x, y).valueF()
                            for y in range(h // 3, h * 2 // 3, 4)
                            for x in range(w // 2, w, 4))
            print(json.dumps({"left": left, "right": right,
                              "lit_left": lit_left, "lit_right": lit_right}))
        """))
        assert got["left"] > 1.0 and got["right"] > 1.0, got
        assert got["lit_left"] > got["lit_right"], (
            "a block in the left lane lit the right of the picture more")

    def test_a_hit_is_something_you_see(self):
        """"Xxxxx xx xx xxxxxxx xxxxxxxx xx xxxxxxx obstacles that the
        user can feel." Run into an obstacle for real: the picture goes
        red, and hard."""
        got = on_the_card(RIDER + textwrap.dedent("""
            made, scene = rider_pane(size=(800, 500))
            reds = []
            def grey_ahead(i):
                # The game's own road cleared, so the one obstacle is
                # this one, a third of a second ahead in the craft's lane.
                scene._placed = scene._laid = 1e9
                if i < 60:
                    scene._blocks = []
                elif i == 60:
                    scene._blocks = [[scene._heard + 0.35, scene._lane,
                                      "block", False, True]]
            with Clock() as clock:
                for i in range(120):
                    clock.step(1 / 60)
                    made.set_position(int((10 + i / 60) * 1000))
                    grey_ahead(i)
                    scene._shield = 0.0
                    made._tick()
                    shot = made._canvas.grabFramebuffer()
                    r, g, b = mean_colour(shot)
                    reds.append(r / max(1e-6, r + g + b))
            print(json.dumps({"hits": scene._hits, "reds": reds}))
        """))
        assert got["hits"] >= 1, "the obstacle was never hit"
        reds = got["reds"]
        before = sum(reds[40:60]) / 20.0
        after = max(reds[80:110])
        assert after > before + 0.12, (
            f"the picture's share of red went from {before:.2f} to "
            f"{after:.2f} at the hit")

    def test_it_survives_being_moved_into_full_screen(self):
        """Full screen takes the pane out of the window and puts it in
        another, which can bring a new context: the world built on the
        old one has to be let go of and built again on the new, and the
        rider has to go on being drawn as a world rather than flat."""
        got = on_the_card(RIDER + textwrap.dedent("""
            from PySide6.QtWidgets import QWidget
            home, away = QWidget(), QWidget()
            home.setWindowTitle("Visualiser - test, in the window")
            away.setWindowTitle("Visualiser - test, full screen")
            made, scene = rider_pane()
            made.setParent(home)
            before = play(made, 0.4)
            first = made._canvas.world
            made.setParent(away)
            away.resize(960, 600)
            made.resize(960, 600)
            after = play(made, 0.4, start=11.0)
            print(json.dumps({
                "world": made._canvas.world is not None,
                "failed": made._canvas.world_failed,
                "before": sum(mean_colour(before)) / 3.0,
                "after": sum(mean_colour(after)) / 3.0}))
        """))
        assert got["world"] and not got["failed"]
        assert got["after"] > got["before"] * 0.6, (
            f"the world was {got['before']:.3f} bright in the window and "
            f"{got['after']:.3f} after the move")

    def test_a_new_context_gets_a_new_world(self):
        """Whether a move brings a new context depends on whether Qt
        shares resources between them, which it does here and need not
        elsewhere - so the reset is checked where it happens: a context
        made afresh drops the world, and the next frame builds another."""
        got = on_the_card(RIDER + textwrap.dedent("""
            made, scene = rider_pane()
            play(made, 0.2)
            first = made._canvas.world
            made._canvas.makeCurrent()
            made._canvas.initializeGL()
            dropped = made._canvas.world is None
            made._canvas.doneCurrent()
            play(made, 0.2, start=11.0)
            print(json.dumps({"dropped": dropped,
                              "again": made._canvas.world is not None
                              and made._canvas.world is not first}))
        """))
        assert got["dropped"] is True
        assert got["again"] is True

    def test_a_world_that_will_not_build_draws_the_rider_flat(self):
        got = on_the_card(RIDER + textwrap.dedent("""
            import rider_gl
            def refuse(self, gl):
                raise RuntimeError("no shaders on this card")
            rider_gl.RiderWorld.__init__ = refuse
            made, scene = rider_pane()
            shot = play(made, 0.6)
            print(json.dumps({"failed": made._canvas.world_failed,
                              "gpu": made.on_gpu,
                              "bright": sum(mean_colour(shot)) / 3.0}))
        """))
        assert got["failed"] is True and got["gpu"] is True
        assert got["bright"] > 0.02, "and then drew nothing at all"

    def test_the_switch_draws_the_rider_flat(self):
        """``MAIL_MANAGER_WORLD=0``, for telling a problem in the world
        from a problem in the game."""
        got = on_the_card(RIDER + textwrap.dedent("""
            os.environ["MAIL_MANAGER_WORLD"] = "0"
            made, scene = rider_pane()
            shot = play(made, 0.4)
            print(json.dumps({"world": made._canvas.world is not None,
                              "bright": sum(mean_colour(shot)) / 3.0}))
        """))
        assert got["world"] is False
        assert got["bright"] > 0.02

    def test_it_fits_a_frame_at_full_resolution(self):
        """Loosely, because it is a statement about this machine's card:
        at 2x and every pixel the world must not be a slideshow."""
        got = on_the_card(RIDER + textwrap.dedent("""
            import statistics
            made, scene = rider_pane(size=(1280, 800), rung=(4, 1.0))
            costs = []
            with Clock() as clock:
                for i in range(150):
                    clock.step(1 / 60)
                    made.set_position(int((10 + i / 60) * 1000))
                    made._tick()
                    if i == 0:
                        made._canvas.grabFramebuffer()
                    c = made._canvas
                    c.makeCurrent()
                    f = c.context().functions()
                    f.glFinish()
                    started = time.perf_counter()
                    c.paintGL()
                    f.glFinish()
                    costs.append((time.perf_counter() - started) * 1000.0)
                    c.doneCurrent()
            print(json.dumps({"median": statistics.median(costs[30:]),
                              "ratio": made._canvas.devicePixelRatioF()}))
        """))
        assert got["median"] < 25.0, (
            f"a frame of the world costs {got['median']:.1f} ms at "
            f"{got['ratio']}x")
