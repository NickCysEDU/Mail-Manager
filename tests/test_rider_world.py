"""Music rider's lit world on the graphics card (see rider_gl). The world's
bookkeeping runs offscreen with the rest of the suite; what it draws runs
through the card harness in test_gpu_canvas, which skips without a context.
"""

from __future__ import annotations

import math
import textwrap

import pytest

from test_gpu_canvas import on_the_card

#: A pane playing the rider to a steady 120 bpm kick, on a stepped clock, so
#: the game is the same on every machine.
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
                 "_bloom_bump", "_flash_lane", "_glow", "_punch"):
        setattr(world, name, 0.0)
    world._glow_colour = (1.0, 1.0, 1.0)
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
    @pytest.mark.parametrize("name", ["Cruiser", "Arrow", "Interceptor",
                                      "Needle"])
    def test_it_is_a_whole_model_facing_down_the_road(self, name):
        import rider_gl

        body = rider_gl.ship_triangles(name)
        glass = rider_gl.canopy_triangles(name)
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


class TestEachLevelHasItsCraft:
    """Each level has its craft: harder levels show fewer beats of road and run
    faster, and their craft look it."""

    LEVELS = ("Easy", "Normal", "Hard", "Expert")

    def test_every_level_has_one_and_normal_s_is_the_first(self):
        import rider_gl
        import visualizers

        assert set(rider_gl.CRAFT_FOR) == set(visualizers.Rider.DIFFICULTIES)
        assert len({rider_gl.CRAFT_FOR[level] for level in self.LEVELS}) == 4
        assert rider_gl.CRAFT_FOR["Normal"] == "Arrow"
        assert rider_gl.ship_triangles() == rider_gl.ship_triangles("Arrow")

    def test_the_faster_the_road_the_leaner_the_craft(self):
        import rider_gl

        shapes = []
        for level in self.LEVELS:
            name = rider_gl.CRAFT_FOR[level]
            body = rider_gl.ship_triangles(name)
            xs = [x for x, _y, _z in body]
            zs = [z for _x, _y, z in body]
            shapes.append(((max(zs) - min(zs)) / (max(xs) - min(xs)),
                           rider_gl.craft(name)["flame"]))
        leanness = [lean for lean, _flame in shapes]
        flames = [flame for _lean, flame in shapes]
        assert leanness == sorted(leanness), leanness
        assert flames == sorted(flames) and flames[0] < flames[-1]

    @pytest.mark.parametrize("name", ["Cruiser", "Arrow", "Interceptor",
                                      "Needle"])
    def test_a_flame_at_every_engine(self, name):
        import rider_gl

        spec = rider_gl.craft(name)
        flames = rider_gl.nozzles(name)
        assert len(flames) == spec["engines"]
        assert all(z > 0.3 for _x, _y, z in flames), "a flame at the front"
        assert abs(sum(x for x, _y, _z in flames)) < 1e-9, "lopsided"

    def test_the_flat_craft_follows_the_level_too(self):
        import visualizers

        broad = [visualizers.Rider.FLAT_CRAFT[level][0]
                 for level in self.LEVELS]
        long = [visualizers.Rider.FLAT_CRAFT[level][1]
                for level in self.LEVELS]
        assert broad == sorted(broad, reverse=True)
        assert long == sorted(long)

    def test_the_world_flies_the_level_s_craft_and_it_hides_no_more(self):
        """The fastest craft, flown with the least warning, hides no more road
        than the first; stretched both ways, its tail came at the camera."""
        got = on_the_card(RIDER + textwrap.dedent("""
            visualizers.Rider._find_twists = lambda self, *a, **k: []
            def frame(level, craft=True):
                made, scene = rider_pane(size=(640, 400))
                scene.set_difficulty(level)
                def each(i):
                    world = made._canvas.world
                    if world is not None and not craft:
                        world._draw_ship = lambda frame: None
                        world._draw_trim = lambda frame: None
                shot = play(made, 0.6, each=each)
                return shot, made._canvas.world, scene
            out = {}
            for level in ("Normal", "Expert"):
                shot, world, scene = frame(level)
                bare, _w, _s = frame(level, craft=False)
                craft = world.craft_of(scene)
                # Where the craft is: what changes when it is not drawn -
                # over the whole frame, at however many pixels a point
                # this screen has.
                covered = sum(1 for y in range(0, shot.height(), 3)
                              for x in range(0, shot.width(), 3)
                              if abs(shot.pixelColor(x, y).valueF()
                                     - bare.pixelColor(x, y).valueF()) > 0.08)
                out[level] = [craft, world.ship is world._crafts[craft][0],
                              covered]
            print(json.dumps(out))
        """))
        assert got["Normal"][:2] == ["Arrow", True]
        assert got["Expert"][:2] == ["Needle", True]
        assert got["Expert"][2] <= got["Normal"][2] * 1.1, got


class TestWhatWasTaken:
    """The world draws a block as the game says it ended (taken or hit: gone;
    missed: going on past), never worked out again from the craft's
    position, which once drew a jumped prize going into the ship."""

    @staticmethod
    def _met(scene, blocks, lane=1, **state):
        scene._lane = lane
        scene._lane_here = scene._lane_at(lane)
        scene._heard = 5.0
        for name, value in state.items():
            setattr(scene, name, value)
        scene._blocks = [list(block) for block in blocks]
        scene._collide()
        return scene._blocks

    def test_one_the_craft_took_is_drawn_taken(self, qapp):
        scene = _rider()
        taken, missed = self._met(scene, [(4.0, 1, "block", False, False),
                                          (4.0, 2, "block", False, False)])
        world = _bare_world()
        world._notice(scene)
        assert id(taken) in world._taken
        assert id(missed) not in world._taken
        assert len(world._taken_now) == 1

    def test_a_prize_jumped_over_goes_on_past(self, qapp):
        scene = _rider()
        (under,) = self._met(scene, [(4.0, 1, "block", False, False)],
                             _air=0.5)
        assert under[3], "the game did not let it go by"
        world = _bare_world()
        world._notice(scene)
        assert id(under) not in world._taken, (
            "a prize the craft jumped over was drawn going into it")
        assert world._taken_now == []

    def test_a_grey_hit_is_gone_and_so_is_one_straight_after(self, qapp):
        scene = _rider()
        (hit,) = self._met(scene, [(4.0, 1, "block", False, True)],
                           _shield=0.0)
        assert scene._hits == 1
        world = _bare_world()
        world._notice(scene)
        assert id(hit) in world._taken
        assert world._taken_now == [], "a hit lit the lane like a pickup"
        # One straight after is a hit as well and lands again: every obstacle
        # met is felt.
        (again,) = self._met(scene, [(4.0, 1, "block", False, True)])
        assert scene._hits == 2
        world._notice(scene)
        assert id(again) in world._taken

    @pytest.mark.parametrize("kind, grey, mode, shield, how", [
        ("block", False, "Mono", 0.0, "taken"),
        ("coin", False, "Mono", 0.0, "taken"),
        ("power", False, "Mono", 0.0, "taken"),
        ("block", False, "Puzzle", 0.0, "taken"),
        ("block", True, "Mono", 1.0, "shatter"),
        ("block", True, "Mono", 0.0, "hit"),
    ])
    def test_the_game_says_what_it_did_with_each(self, qapp, kind, grey,
                                                  mode, shield, how):
        scene = _rider()
        scene.set_mode(mode)
        (block,) = self._met(scene, [(4.0, 1, kind, False, grey)],
                             _shield=shield)
        assert scene.struck(block) == how
        # And a lane away, nothing.
        (away,) = self._met(scene, [(4.0, 0, kind, False, grey)],
                            _shield=shield, _sore=0.0)
        assert scene.struck(away) is None

    def test_the_record_goes_with_the_block(self, qapp):
        scene = _rider()
        (taken,) = self._met(scene, [(4.0, 1, "block", False, False)])
        assert scene.struck(taken) == "taken"
        scene._blocks = []
        scene._collide()
        assert scene._struck == {}, "blocks off the road are still recorded"
        # An id is unique only while its thing lives: a new block with an old
        # id is not told the old outcome.
        stranger = [4.0, 1, "block", True, False]
        scene._struck[id(stranger)] = ([4.0, 1, "block", True, True], "hit")
        assert scene.struck(stranger) is None

    def test_it_is_noticed_once(self, qapp):
        scene = _rider()
        self._met(scene, [(4.0, 1, "block", False, False)])
        world = _bare_world()
        world._notice(scene)
        assert len(world._taken_now) == 1
        world._notice(scene)
        assert world._taken_now == []

    def test_what_the_game_drops_is_forgotten(self, qapp):
        scene = _rider()
        self._met(scene, [(4.0, 1, "block", False, False)])
        world = _bare_world()
        world._notice(scene)
        assert world._taken and world._done_seen
        scene._blocks = []
        world._notice(scene)
        assert not world._taken and not world._done_seen


class TestAnObstacleIsSeenHit:
    """A hit is judged at the craft's middle, reached first by its nose, and
    the obstacle is drawn stopped there: drawn shrinking like a prize, what
    hit the player could not be seen."""

    @staticmethod
    def _approach(beside=0.0):
        scene = _rider()
        world = _bare_world()
        wide = scene.LANE_WIDE * 0.5
        size = (wide * 0.95, 0.46 * 1.15, wide * 0.95)
        out = []
        for step in range(61):
            gap = 2.0 - step * (2.0 / 60.0)
            place, scale, yaw = world._rammed(
                (beside, 0.46 * 0.62, scene.RIDER_AT + gap), size, 0.785,
                gap, beside, scene)
            depth = 0.5 * (abs(math.sin(yaw)) * scale[0]
                           + abs(math.cos(yaw)) * scale[2])
            out.append((gap, place, scale, yaw, place[2] - depth))
        return scene, world, size, out

    def test_it_is_never_inside_the_hull(self):
        scene, world, size, out = self._approach()
        for gap, _place, _scale, _yaw, near in out:
            assert near >= scene.RIDER_AT + world.NOSE - 1e-6, (
                f"{gap:.2f} out, its near face is inside the craft")

    def test_it_is_never_shrunk_and_is_flat_on_the_nose_at_the_hit(self):
        scene, world, size, out = self._approach()
        for gap, place, scale, _yaw, _near in out:
            assert scale[0] >= size[0] - 1e-9 and scale[1] >= size[1] - 1e-9, (
                f"{gap:.2f} out it is drawn smaller, as a prize is")
            assert place[1] >= 0.46 * 0.62 - 1e-9, "it sank into the road"
        _gap, _place, scale, yaw, _near = out[-1]
        assert scale[2] < size[2] * 0.3 and abs(yaw) < 1e-6, (
            "at the hit it is not flattened square against the nose")

    @staticmethod
    def _drawn(scene, gap, air=0.0):
        """What _draw_blocks sends to the card for the one block, ``gap`` ahead
        of the craft: its place, scale and turn."""
        world = _bare_world()
        sent = []

        class Program:
            now = {}

            def set(self, name, value):
                self.now[name] = value

            def release(self):
                pass

        class Shape:
            def draw(self, gl, program):
                sent.append(dict(program.now))

        world.gl = None
        world.cube = world.coin = Shape()
        world._solid = lambda frame: Program()
        scene._where = lambda when: scene.RIDER_AT + gap
        world._draw_blocks({"scene": scene, "beat": 0.0,
                            "across": scene._lane_here, "air": air})
        if not sent:
            return None
        got = sent[-1]
        return (tuple(got["uPlace"].toTuple()), tuple(got["uScale"].toTuple()),
                got["uAngles"].x())

    def test_what_is_sent_to_the_card_is_never_inside_the_hull(self, qapp):
        scene = _rider()
        world = _bare_world()
        scene._lane_here = scene._lane_at(1)
        full = None
        for step in range(40):
            gap = 1.9 - step * 0.048
            scene._blocks = [[5.0, 1, "block", False, True]]
            place, scale, yaw = self._drawn(scene, gap)
            full = full or scale
            near = place[2] - 0.5 * (abs(math.sin(yaw)) * scale[0]
                                     + abs(math.cos(yaw)) * scale[2])
            assert near >= scene.RIDER_AT + world.NOSE - 1e-4, (
                f"{gap:.2f} out, the obstacle is drawn inside the craft")
            assert scale[0] >= full[0] - 1e-4, (
                f"{gap:.2f} out, the obstacle is drawn shrinking")

    def test_a_prize_still_goes_into_the_ship_unless_jumped(self, qapp):
        scene = _rider()
        scene._lane_here = scene._lane_at(1)
        scene._blocks = [[5.0, 1, "block", False, False]]
        _place, whole, _yaw = self._drawn(scene, 2.0)
        _place, taking, _yaw = self._drawn(scene, 0.2)
        assert taking[0] < whole[0] * 0.5, "a prize is not drawn going in"
        _place, over, _yaw = self._drawn(scene, 0.2, air=0.6)
        assert over[0] > whole[0] * 0.9, (
            "a prize under a jumping craft is drawn going into it")

    def test_one_met_unhurt_passes_through_whole(self, qapp):
        scene = _rider()
        scene._lane_here = scene._lane_at(1)
        scene._blocks = [[5.0, 1, "block", False, True]]
        _place, whole, yaw = self._drawn(scene, 2.0)
        scene._sore = 0.5
        _place, scale, turn = self._drawn(scene, 0.2)
        assert (scale, turn) == (whole, yaw), (
            "an obstacle the craft cannot be hurt by is squashed on it")

    def test_one_a_lane_over_is_left_alone(self, qapp):
        scene = _rider()
        _scene, world, size, out = self._approach(beside=scene.LANE_WIDE)
        for gap, place, scale, yaw, _near in out:
            assert scale == size and yaw == 0.785
            assert abs(place[2] - (scene.RIDER_AT + gap)) < 1e-9


class TestTheRoadTurnsOver:
    """A corkscrew winds the road round and the camera rides it: the craft
    stays put on the glass while the world goes round."""

    @staticmethod
    def _twisting(heard):
        scene = _rider()
        scene._twists = (10.0,)
        scene._heard = heard
        scene._at = scene._flat(heard) if scene._beat > 0.0 else heard * scene.FREE_RUN
        return scene

    def test_the_road_never_folds(self, qapp):
        """Neighbouring samples never differ by more than the corkscrew's own
        turn between them, including at its end."""
        import rider_gl

        world = _bare_world()
        worst = 0.0
        for tenth in range(0, 200):
            scene = self._twisting(6.0 + tenth * 0.05)
            road = world._read_road(scene)
            rolls = road[2::4]
            worst = max(worst, max(abs(b - a) for a, b in zip(rolls, rolls[1:])))
        assert worst < 0.6, f"the road turns {worst:.2f} between two samples"
        assert rider_gl.ROAD_SAMPLES == len(road) // 4

    def test_the_turn_is_recorded_apart_from_the_bend(self, qapp):
        """The camera leans into a bend by a share of it and rides a corkscrew
        fully, so the two are kept apart in the road."""
        world = _bare_world()
        scene = self._twisting(11.25)
        road = world._read_road(scene)
        for index in range(0, len(road), 4):
            z = -6.0 + index // 4 * (82.0 / 95.0)
            across, lift, roll = scene._road(z)
            assert abs(road[index + 2] - road[index + 3]
                       - roll * world.ROLL_SHARE) < 1e-9
        turn = world._turn_at(road, scene.RIDER_AT)
        assert 1.0 < turn < 5.0, f"half way through, the road is at {turn:.2f}"
        # The camera's turn about the road at the craft is the one that keeps a
        # road point where it was.
        centre = world._on_road(road, 0.0, 0.0, scene.RIDER_AT)
        plain = world._on_road(road, 0.6, 0.4, scene.RIDER_AT, plain=True)
        turned = world._about(plain, centre, turn)
        wound = world._on_road(road, 0.6, 0.4, scene.RIDER_AT)
        assert max(abs(a - b) for a, b in zip(turned, wound)) < 1e-9


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

    @pytest.mark.parametrize("kind", ["hit", "shatter"])
    def test_what_was_hit_breaks_up_where_it_met_the_nose(self, qapp, kind):
        """The obstacle is drawn flat on the nose until the hit and gone after,
        its pieces flying from there, red."""
        world = self._happened(kind)
        scene = _rider()
        nose = scene.RIDER_AT + world.NOSE + float(scene._at)
        count = world.PARTICLES
        data = world._particles
        red = [i for i in range(count)
               if abs(data[i * 12 + 2] - nose) < 1e-4
               and data[i * 12 + 8] > 2.0 * max(data[i * 12 + 9],
                                                data[i * 12 + 10])]
        assert len(red) >= 20, f"{len(red)} pieces of it"

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
    """Drawn flat or as a world, the game is one game: both move it on with
    ``_step``."""

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


class TestTheWorldOnTheCard:
    def test_it_draws_at_every_rung(self):
        """At full resolution as well as half: a GL paint device already
        reports its height in pixels, and multiplying by the ratio again
        drew the world off the top of the frame at full resolution."""
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
        """Left lane and right lane, same block, same moment: the picture
        differs on the side the game put it."""
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
            # The two runs are the same run but for the lane, so the
            # difference between them is the block and nothing else:
            # where the left-lane shot is brighter, and where the
            # right-lane one is.
            left = right = 0.0
            for y in range(0, h, 4):
                for x in range(0, w, 4):
                    d = (a.pixelColor(x, y).valueF()
                         - b.pixelColor(x, y).valueF())
                    if x < w * 0.5:
                        left += d
                    else:
                        right -= d
            print(json.dumps({"left": left, "right": right}))
        """))
        assert got["left"] > 1.0, (
            f"the block in the left lane did not light the left of the "
            f"picture: {got}")
        assert got["right"] > 1.0, (
            f"the block in the right lane did not light the right: {got}")

    def test_what_is_coming_is_seen_from_far_off(self):
        """The game lays blocks five seconds ahead, about sixty units of road,
        and the world draws them from far out so none appears a few beats
        away."""
        got = on_the_card(RIDER + textwrap.dedent("""
            shots = {}
            for far in (False, True):
                made, scene = rider_pane(size=(800, 500))
                def one_far_off(i, scene=scene, far=far):
                    scene._placed = scene._laid = 1e9
                    # Forty units ahead of the craft, however fast the
                    # road is running there.
                    due = scene._when(scene.RIDER_AT + 40.0)
                    scene._blocks = ([[due, 1, "block", False, False]]
                                     if far else [])
                shots[far] = play(made, 0.6, each=one_far_off)
            a, b = shots[True], shots[False]
            w, h = a.width(), a.height()
            lit = sum(max(0.0, a.pixelColor(x, y).valueF()
                          - b.pixelColor(x, y).valueF())
                      for y in range(0, h, 2) for x in range(0, w, 2))
            print(json.dumps({"lit": lit}))
        """))
        assert got["lit"] > 2.0, (
            f"a block forty units down the road lit {got['lit']:.2f}: it "
            f"is not drawn")

    def test_a_hit_is_something_you_see(self):
        """Running into an obstacle turns the picture red, hard."""
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

    def test_what_hits_you_is_seen_hitting_you(self):
        """Just before the hit, the obstacle fills at least as much of the
        picture as three units out: squashed on the nose, not shrunk into
        the hull."""
        got = on_the_card(RIDER + textwrap.dedent("""
            runs = {}
            for there in (True, False):
                made, scene = rider_pane(size=(800, 500))
                shots, gaps = [], []
                with Clock() as clock:
                    for i in range(90):
                        clock.step(1 / 60)
                        made.set_position(int((10 + i / 60) * 1000))
                        scene._placed = scene._laid = 1e9
                        if i < 60:
                            scene._blocks = []
                        elif i == 60 and there:
                            scene._blocks = [[scene._heard + 0.35,
                                              scene._lane, "block", False,
                                              True]]
                        scene._shield = 0.0
                        made._tick()
                        shots.append(made._canvas.grabFramebuffer())
                        # Only this one: the game lays a road of its own
                        # on the first frame, before it is cleared.
                        block = (scene._blocks[0]
                                 if i >= 60 and scene._blocks else None)
                        gaps.append(None if block is None or block[3] else
                                    scene._where(block[0]) - scene.RIDER_AT)
                runs[there] = (shots, gaps)
            shots, gaps = runs[True]
            empty = runs[False][0]
            far = next(i for i, g in enumerate(gaps)
                       if g is not None and g < 3.0)
            assert far >= 60, far
            near = max(i for i, g in enumerate(gaps) if g is not None)
            def covered(i):
                a, b = shots[i], empty[i]
                return sum(1 for y in range(0, a.height(), 2)
                           for x in range(0, a.width(), 2)
                           if abs(a.pixelColor(x, y).valueF()
                                  - b.pixelColor(x, y).valueF()) > 0.12)
            print(json.dumps({"far": covered(far), "near": covered(near),
                              "gap": gaps[near]}))
        """))
        assert got["gap"] < 0.6, f"never got close: {got}"
        assert got["near"] >= got["far"] * 0.8, (
            f"just before the hit the obstacle covers {got['near']} points "
            f"against {got['far']} three units out: it is not seen hitting")

    def test_a_corkscrew_turns_the_world_round_the_craft(self):
        """Through a whole corkscrew the craft stays where the card is told to
        draw it, and half way round the foot of the frame is the same road
        while the top has gone round. Against the same tunnel with the turn
        taken out of the world's road, so only the turn differs."""
        got = on_the_card(RIDER + textwrap.dedent("""
            runs = {}
            for twisted in (False, True):
                made, scene = rider_pane(size=(800, 500))
                ships, rolled, shots = [], [], []
                with Clock() as clock:
                    for i in range(int(3.6 * 60)):
                        clock.step(1 / 60)
                        made.set_position(int((10 + i / 60) * 1000))
                        scene._placed = scene._laid = 1e9
                        scene._blocks = []
                        scene._twists = (10.6,)
                        world = made._canvas.world
                        if not twisted and world is not None:
                            world._twist = lambda scene, z: 0.0
                        made._tick()
                        shots.append(made._canvas.grabFramebuffer())
                        ships.append(made._canvas.world.seen["ship"])
                        rolled.append(scene._rolled)
                runs[twisted] = (ships, rolled, shots)
            plain, turned = runs[False], runs[True]
            mid = min(range(len(turned[1])),
                      key=lambda i: abs(turned[1][i] - 0.5))
            def changed(a, b, top, bottom, left=0.0, right=1.0):
                h, w = a.height(), a.width()
                points = [(x, y) for y in range(int(h * top), int(h * bottom), 4)
                          for x in range(int(w * left), int(w * right), 4)]
                return sum(1 for x, y in points
                           if abs(a.pixelColor(x, y).valueF()
                                  - b.pixelColor(x, y).valueF()) > 0.15
                           ) / len(points)
            a, b = plain[2][mid], turned[2][mid]
            print(json.dumps({
                "most": max(turned[1]), "mid": turned[1][mid],
                "drift": max(abs(p[0] - t[0]) + abs(p[1] - t[1])
                             for p, t in zip(plain[0], turned[0])),
                # The road straight below the craft, which is under it
                # whichever way the world has gone. Its edges a few units
                # on are already winding up the sides of the frame, which
                # is the corkscrew and not a fault.
                "foot": changed(a, b, 0.80, 1.0, 0.30, 0.70),
                "top": changed(a, b, 0.0, 0.45)}))
        """))
        assert got["most"] > 0.9 and abs(got["mid"] - 0.5) < 0.05, got
        assert got["drift"] < 0.03, (
            f"the craft moved {got['drift']:.3f} of the frame through the "
            f"corkscrew")
        assert got["foot"] < 0.15 and got["top"] > got["foot"] * 2.0, (
            f"half way round, {got['foot']:.0%} of the foot of the frame and "
            f"{got['top']:.0%} of the top changed")

    def test_the_craft_turns_over_in_one_piece(self):
        """The craft alone on a road turned evenly and on one turning steeply
        ahead: as one piece it is the same picture. Placed point by point
        with the road's roll at each point, it was wrung along its length."""
        got = on_the_card(RIDER + textwrap.dedent("""
            ALONE = ("_draw_sky", "_draw_city", "_draw_road", "_draw_blocks",
                     "_draw_barriers", "_draw_beacons", "_draw_gates",
                     "_draw_streaks", "_draw_trim", "_draw_particles",
                     "_draw_tunnel")
            def craft_only(steep):
                made, scene = rider_pane(size=(800, 500))
                with Clock() as clock:
                    for i in range(40):
                        clock.step(1 / 60)
                        made.set_position(int((10 + i / 60) * 1000))
                        scene._placed = scene._laid = 1e9
                        scene._blocks = []
                        world = made._canvas.world
                        if world is not None:
                            for name in ALONE:
                                setattr(world, name, lambda frame: None)
                            at = scene.RIDER_AT
                            world._twist = (
                                (lambda scene, z: 2.0 + 0.8 * max(0.0, z - at))
                                if steep else (lambda scene, z: 2.0))
                        made._tick()
                        shot = made._canvas.grabFramebuffer()
                return shot
            a, b = craft_only(False), craft_only(True)
            lit = [(x, y) for y in range(0, a.height(), 2)
                   for x in range(0, a.width(), 2)
                   if max(a.pixelColor(x, y).valueF(),
                          b.pixelColor(x, y).valueF()) > 0.1]
            moved = sum(1 for x, y in lit
                        if abs(a.pixelColor(x, y).valueF()
                               - b.pixelColor(x, y).valueF()) > 0.1)
            print(json.dumps({"lit": len(lit),
                              "moved": moved / max(1, len(lit))}))
        """))
        assert got["lit"] > 300, f"the craft was not drawn: {got}"
        assert got["moved"] < 0.05, (
            f"{got['moved']:.0%} of the craft moved when only the road "
            f"ahead of it turned: it is not turned in one piece")

    def test_the_canopy_is_glass_not_paint(self):
        """The craft's canopy is glass: across its points it runs from the dim
        cockpit to the bright world it reflects at its edges, and carries
        the world's hues and movement. A flat tint measured a hue spread of
        0.02 and changed no more than the hull as the craft travelled; the
        glass spreads 0.36 and changes five times the hull's amount."""
        got = on_the_card(RIDER + textwrap.dedent("""
            import math
            ALONE = ("_draw_sky", "_draw_city", "_draw_road", "_draw_blocks",
                     "_draw_barriers", "_draw_beacons", "_draw_gates",
                     "_draw_streaks", "_draw_trim", "_draw_particles",
                     "_draw_tunnel")
            def craft(canopy):
                made, scene = rider_pane(size=(800, 500))
                shots = []
                with Clock() as clock:
                    for i in range(40):
                        clock.step(1 / 60)
                        made.set_position(int((10 + i / 60) * 1000))
                        scene._placed = scene._laid = 1e9
                        scene._blocks = []
                        world = made._canvas.world
                        if world is not None:
                            for name in ALONE:
                                setattr(world, name, lambda frame: None)
                            if not canopy:
                                world.canopy.draw = lambda gl, p: None
                        made._tick()
                        shot = made._canvas.grabFramebuffer()
                        if i in (29, 39):
                            shots.append(shot)
                return shots
            (a, later), (b, _later) = craft(True), craft(False)
            points, hull = [], []
            for y in range(0, a.height(), 2):
                for x in range(0, a.width(), 2):
                    one, two = a.pixelColor(x, y), b.pixelColor(x, y)
                    if abs(one.valueF() - two.valueF()) > 0.03 or abs(
                            one.hueF() - two.hueF()) > 0.05:
                        points.append((x, y))
                    elif two.valueF() > 0.05:
                        hull.append((x, y))
            glass = sorted(a.pixelColor(x, y).valueF() for x, y in points)
            hues = [a.pixelColor(x, y).hueF() for x, y in points
                    if a.pixelColor(x, y).saturationF() > 0.15
                    and a.pixelColor(x, y).valueF() > 0.08]
            # One minus the length of the mean of the hues as angles: 0 for
            # one hue, towards 1 for every hue there is.
            spread = (1.0 - math.hypot(
                sum(math.cos(h * math.tau) for h in hues),
                sum(math.sin(h * math.tau) for h in hues)) / len(hues)
                if hues else 0.0)
            def change(where):
                return sum(abs(a.pixelColor(x, y).valueF()
                               - later.pixelColor(x, y).valueF())
                           for x, y in where) / max(1, len(where))
            print(json.dumps({"points": len(glass),
                              "low": glass[len(glass) // 10] if glass else 0,
                              "high": glass[len(glass) * 9 // 10] if glass else 0,
                              "top": glass[-1] if glass else 0,
                              "spread": spread, "moving": change(points),
                              "hull_moving": change(hull)}))
        """))
        assert got["points"] > 40, f"the canopy was not found: {got}"
        assert got["high"] > got["low"] * 2.2 + 0.05, (
            f"the canopy runs from {got['low']:.2f} to {got['high']:.2f}: a "
            f"flat tint")
        assert got["top"] > 0.85, "nothing on it catches the light"
        assert got["spread"] > 0.15, (
            f"the canopy is one colour (a spread of hues of "
            f"{got['spread']:.3f}): a tint, not the world reflected")
        assert got["moving"] > 2.5 * got["hull_moving"], (
            f"the canopy changes {got['moving']:.3f} as the craft travels "
            f"against the hull's {got['hull_moving']:.3f}: nothing slides "
            f"over it")

    def test_it_survives_being_moved_into_full_screen(self):
        """Full screen can bring a new context: the world built on the old one
        is let go and rebuilt, and the rider stays a world rather than going
        flat."""
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
        """Whether a move brings a new context depends on Qt sharing resources,
        so the reset is checked where it happens: a fresh context drops the
        world and the next frame builds another."""
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
        """``MAIL_MANAGER_WORLD=0``, to tell a problem in the world from one in
        the game."""
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
        """Loose, being about this machine's card: at 2x and every pixel the
        world must not be a slideshow."""
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


class TestTheCorkscrewIsATunnel:
    """A corkscrew is ridden through a tunnel, with the city outside: the city
    stands along the road and turns with it, so towers went round with the
    track."""

    def test_it_opens_before_the_turn_and_closes_after(self):
        import visualizers

        scene = visualizers.Rider()
        scene._twists = [20.0]
        enter = 20.0 - scene.TUNNEL_LEAD
        leave = 20.0 + scene.TWIST_FOR + scene.TUNNEL_TAIL
        assert scene._tunnel_at(enter - scene.TUNNEL_RAMP - 0.01) == 0.0
        assert scene._tunnel_at(enter - scene.TUNNEL_RAMP / 2) == (
            pytest.approx(0.5))
        for inside in (enter, 20.0, 21.0, leave):
            assert scene._tunnel_at(inside) == 1.0, inside
        assert scene._tunnel_at(leave + scene.TUNNEL_RAMP + 0.01) == 0.0
        scene._twists = []
        assert scene._tunnel_at(20.0) == 0.0

    def test_inside_it_the_walls_are_all_there_is(self):
        """The same corkscrew moment with and without the tunnel: with it, the
        view above the road is the tunnel's, and the turning city is behind
        its walls."""
        got = on_the_card(RIDER + textwrap.dedent("""
            visualizers.Rider._find_twists = lambda self, *a, **k: [12.0]
            def at(when, tunnel=True):
                made, scene = rider_pane(size=(640, 400))
                shot = None
                with Clock() as clock:
                    for i in range(int((when - 10.0) * 60)):
                        clock.step(1.0 / 60)
                        made.set_position(int((10.0 + i / 60) * 1000))
                        world = made._canvas.world
                        if world is not None and not tunnel:
                            world._draw_tunnel = lambda frame: None
                        made._tick()
                        shot = made._canvas.grabFramebuffer()
                world = made._canvas.world
                return shot, world.inside, max(world.tunnel_line)
            def differ(a, b):
                rows = range(0, a.height() // 2, 4)
                cols = range(0, a.width(), 4)
                changed = sum(1 for y in rows for x in cols
                              if abs(a.pixelColor(x, y).valueF()
                                     - b.pixelColor(x, y).valueF()) > 0.08
                              or abs(a.pixelColor(x, y).hueF()
                                     - b.pixelColor(x, y).hueF()) > 0.08)
                return changed / (len(rows) * len(cols))
            inside, here, _most = at(12.9)
            bare, _h, _m = at(12.9, tunnel=False)
            before, was, seen_ahead = at(10.4)
            print(json.dumps({"here": here, "was": was,
                              "seen_ahead": seen_ahead,
                              "covered": differ(inside, bare)}))
        """))
        assert got["here"] == 1.0, got
        assert got["covered"] > 0.6, (
            f"the tunnel changed {got['covered']:.0%} of the view above the "
            f"road: the city is still showing through it")
        assert got["was"] == 0.0 and got["seen_ahead"] > 0.0, (
            "the tunnel is not seen coming")

    def test_far_from_a_corkscrew_there_is_none(self):
        got = on_the_card(RIDER + textwrap.dedent("""
            visualizers.Rider._find_twists = lambda self, *a, **k: [200.0]
            made, scene = rider_pane(size=(480, 300))
            play(made, 0.5)
            world = made._canvas.world
            print(json.dumps({"most": max(world.tunnel_line),
                              "inside": world.inside}))
        """))
        assert got == {"most": 0.0, "inside": 0.0}

    def test_the_flat_picture_has_it_too(self, qapp):
        """Without a card: the tunnel's colours wheeling round the road's
        end."""
        import ridekit
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        def spread_at(seconds):
            scene, _log, _truth, _beat = ridekit.ride("house", seconds=seconds)
            image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            scene.paint(painter, QRectF(0, 0, 320, 180), scene._ridden_state)
            painter.end()
            hues = {round(image.pixelColor(x, y).hueF(), 1)
                    for x in range(0, 320, 8) for y in range(0, 90, 8)
                    if image.pixelColor(x, y).saturationF() > 0.4
                    and image.pixelColor(x, y).valueF() > 0.25}
            return scene, len(hues)

        scene, _hues = spread_at(1.0)
        twist = scene._twists[0]
        _s, inside = spread_at(twist + 1.0)
        _s, outside = spread_at(twist - 3.0)
        assert inside >= 7 and inside > outside + 3, (inside, outside)


class TestTheCityThroughACorkscrew:
    """The towers stand upright whatever the road does, outside a tunnel and
    in it: turned with the road, they twisted in the distance as a corkscrew
    came, and the skyline went round with it on the way out."""

    def test_the_towers_stay_upright_whatever_the_road_does(self):
        got = on_the_card(RIDER + textwrap.dedent("""
            ONLY = ("_draw_road", "_draw_tunnel", "_draw_blocks",
                    "_draw_ship", "_draw_trim", "_draw_gates",
                    "_draw_barriers", "_draw_streaks", "_draw_particles")

            def city(turn=0.0, inside=0.0, towers=True):
                made, scene = rider_pane(size=(640, 400))
                def each(i):
                    world = made._canvas.world
                    if world is None:
                        return
                    for name in ONLY:
                        setattr(world, name, lambda frame: None)
                    if not towers:
                        world._draw_city = lambda frame: None
                        world._draw_beacons = lambda frame: None
                    # Turned only well ahead of the craft, as a corkscrew
                    # coming is: the view itself stays level.
                    world._twist = lambda scene, z: turn if z > 12.0 else 0.0
                    real = type(world)._read_road
                    def read(scene, real=real, world=world):
                        out = real(world, scene)
                        world.inside = inside
                        return out
                    world._read_road = read
                return play(made, 0.5, each=each)

            def apart(a, b):
                w, h = a.width(), a.height()
                return sum(abs(a.pixelColor(x, y).valueF()
                               - b.pixelColor(x, y).valueF())
                           for y in range(0, h, 2) for x in range(0, w, 2))

            level = city()
            out = {"city": apart(level, city(towers=False)),
                   "outside": apart(level, city(turn=1.3)),
                   "inside": apart(city(inside=1.0),
                                   city(turn=1.3, inside=1.0))}
            print(json.dumps(out))
        """))
        assert got["city"] > 50.0, f"no city to see: {got}"
        assert got["outside"] < got["city"] * 0.01, (
            f"the towers ahead turned with the road: {got}")
        assert got["inside"] < got["city"] * 0.01, (
            f"inside the tunnel the towers turned with the road: {got}")


class TestEveryHitLandsAndRunsOfThemBuild:
    """Each obstacle met lands again however close to the last, and one hard on
    the last lands harder; a prize answers with its colour and a punch
    forward, building through a quick run."""

    @staticmethod
    def _hits(scene, times):
        for when in times:
            scene._lane = 1
            scene._lane_here = scene._lane_at(1)
            scene._heard = when
            scene._shield = 0.0
            scene._blocks = [[when, 1, "block", False, True]]
            scene._collide()
        return [pop[2] for pop in scene._pops if pop[0] == "hit"]

    def test_a_run_of_hits_lands_harder_each_time(self, qapp):
        strengths = self._hits(_rider(), (4.0, 4.4, 4.8))
        assert len(strengths) == 3
        assert strengths[0] < strengths[1] < strengths[2]

    def test_and_starts_again_after_a_gap(self, qapp):
        strengths = self._hits(_rider(), (4.0, 9.0))
        assert strengths[0] == pytest.approx(strengths[1])

    def test_the_world_answers_every_one_from_the_start(self):
        world = _bare_world()
        scene = _rider()
        scene._pops = [["hit", 0.0, 1.0, 0.0, 0.95, ""]]
        world._events(scene)
        assert world._shock == 0.0 and world._shock_hard == 1.0
        world._shock = 0.6           # the first one's ring is on its way
        scene._pops.append(["hit", 0.0, 1.5, 0.0, 0.95, ""])
        world._events(scene)
        assert world._shock == 0.0, "the second hit did not start again"
        assert world._shock_hard == pytest.approx(1.5)

    def test_prizes_bring_their_colour_and_build(self):
        world = _bare_world()
        scene = _rider()
        green = (0.1, 1.0, 0.2)
        world._taken_now = [("block", False, green)]
        scene._pops = [["prize", 0.0, 1.0, 0.33, 0.9, ""]]
        world._events(scene)
        one = (world._glow, world._punch)
        assert world._glow_colour == green and one[0] > 0.0 and one[1] > 0.0
        scene._pops.append(["prize", 0.0, 1.0, 0.33, 0.9, ""])
        world._events(scene)
        assert world._glow > one[0] and world._punch > one[1]

    def test_a_prize_colours_the_edges_of_the_picture(self):
        got = on_the_card(RIDER + textwrap.dedent("""
            def edges(glow):
                made, scene = rider_pane(size=(640, 400))
                def each(i):
                    world = made._canvas.world
                    if world is not None:
                        world._glow = glow
                        world._glow_colour = (0.0, 1.0, 0.0)
                        world._punch = 0.0
                shot = play(made, 0.3, each=each)
                w, h = shot.width(), shot.height()
                green = other = 0.0
                for y in range(0, h, 6):
                    for x in (2, w // 12, w - w // 12, w - 3):
                        c = shot.pixelColor(x, y)
                        green += c.greenF()
                        other += c.redF() + c.blueF()
                return green, other
            print(json.dumps({"off": edges(0.0), "on": edges(1.5)}))
        """))
        assert got["on"][0] > got["off"][0] * 1.3, got


class TestTheEyeStaysClearOfTheRoad:
    """Behind and above the craft, the eye is above the road at every point
    between the two: over a crest, and where a corkscrew has turned the road
    behind the craft further than the craft, the road was through the
    camera."""

    @staticmethod
    def _scene(crest: bool, twist=None):
        scene = _rider()
        rate = 8.0
        scene._every = rate
        count = int(40.0 * rate)
        # Depth below the start: a sharp rise and fall at twenty seconds.
        scene._hill = tuple(
            -8.0 * max(0.0, 1.0 - abs(index / rate - 20.0) / 1.5)
            if crest else 0.0 for index in range(count))
        scene._curve = tuple(math.sin(index / rate * 0.7) * 3.0
                             for index in range(count))
        scene._twists = () if twist is None else (twist,)
        return scene

    @staticmethod
    def _heights(world, road, eye, ship_z: float, half: float) -> list:
        """The eye's height above the road, in each sample's own frame,
        between it and the craft; None beside the road."""
        out = []
        for step in range(9):
            z = -eye[2] + (ship_z + eye[2]) * step / 8.0
            dx = eye[0] - world._sample(road, z, 0)
            dy = eye[1] - world._sample(road, z, 1)
            r = world._sample(road, z, 2)
            if abs(dx * math.cos(r) + dy * math.sin(r)) > half + 1.0:
                out.append(None)
            else:
                out.append(-dx * math.sin(r) + dy * math.cos(r))
        return out

    def _eyes(self, scene, heard: float) -> tuple:
        world = _bare_world()
        world._knock = 0.0
        scene._heard = heard
        scene._at = heard * scene.FREE_RUN
        road = world._read_road(scene)
        ship_z = scene.RIDER_AT
        half = scene.LANE_WIDE * scene.LANES / 2.0
        eye = world._on_road(road, 0.0, world.CAM_UP, ship_z - world.CAM_BACK,
                             plain=True)
        turn = world._turn_at(road, ship_z)
        centre = world._on_road(road, 0.0, 0.0, ship_z)
        turned = world._about(eye, centre, turn)
        clear = world._clear_of_road(road, turned, ship_z, half)
        return (self._heights(world, road, turned, ship_z, half),
                self._heights(world, road, clear, ship_z, half), turned, clear)

    def test_over_a_crest_it_is_lifted_clear(self, qapp):
        import rider_gl

        lifted = False
        for heard in (19.4, 19.7, 20.0, 20.3, 20.6):
            before, after, turned, clear = self._eyes(self._scene(True), heard)
            assert all(h is None or h >= rider_gl.RiderWorld.CAM_CLEAR - 1e-3
                       for h in after), (heard, after)
            if any(h is not None and h < rider_gl.RiderWorld.CAM_CLEAR
                   for h in before):
                lifted = True
                assert clear != turned
        assert lifted, "the crest never came near the camera"

    def test_through_a_corkscrew_on_a_crest(self, qapp):
        import rider_gl

        scene = self._scene(True, twist=18.6)
        for heard in (18.8, 19.2, 19.6, 20.0, 20.4, 20.8, 21.1):
            _before, after, _turned, _clear = self._eyes(scene, heard)
            assert all(h is None or h >= rider_gl.RiderWorld.CAM_CLEAR - 1e-3
                       for h in after), (heard, after)

    def test_a_clear_eye_is_left_where_it_is(self, qapp):
        for heard in (5.0, 12.0, 30.0):
            _before, _after, turned, clear = self._eyes(self._scene(False),
                                                        heard)
            assert clear == turned


class TestTheCraftIsOnePiece:
    """A craft is placed in the road's frame where it stands, not point by
    point along the road's line: set on the line, its nose and tail were on
    different slopes and it bent over every crest."""

    @staticmethod
    def _road(crest: bool, heard: float = 20.0):
        scene = TestTheEyeStaysClearOfTheRoad._scene(crest)
        world = _bare_world()
        scene._heard = heard
        scene._at = heard * scene.FREE_RUN
        return world, world._read_road(scene), scene

    NOSE, TAIL, WING = (0.0, 0.0, -1.1), (0.0, 0.0, 0.9), (0.9, 0.0, 0.0)

    def test_its_points_keep_their_distances_over_a_crest(self, qapp):
        for heard in (19.4, 20.0, 20.6):
            world, road, scene = self._road(True, heard)
            place = (0.4, world.HOVER, scene.RIDER_AT)
            nose, tail, wing = (world._rigid(road, place, local)
                                for local in (self.NOSE, self.TAIL, self.WING))
            assert math.dist(nose, tail) == pytest.approx(2.0, abs=1e-6)
            assert math.dist(nose, wing) == pytest.approx(
                math.dist(self.NOSE, self.WING), abs=1e-6)

    def test_it_lies_along_the_slope(self, qapp):
        """On the way up to the crest the nose is higher than the tail, and
        on the way down lower."""
        world, road, scene = self._road(True, 19.6)
        place = (0.0, world.HOVER, scene.RIDER_AT)
        nose = world._rigid(road, place, self.NOSE)
        tail = world._rigid(road, place, self.TAIL)
        assert nose[1] > tail[1] + 0.2
        world, road, scene = self._road(True, 20.4)
        nose = world._rigid(road, place, self.NOSE)
        tail = world._rigid(road, place, self.TAIL)
        assert nose[1] < tail[1] - 0.2

    def test_on_the_flat_it_is_where_the_road_puts_it(self, qapp):
        world, road, scene = self._road(False, 5.0)
        scene._curve = tuple(0.0 for _ in scene._curve)
        road = world._read_road(scene)
        place = (0.7, 0.5, scene.RIDER_AT)
        assert world._rigid(road, place, (0.0, 0.0, 0.0)) == pytest.approx(
            world._on_road(road, 0.7, 0.5, scene.RIDER_AT), abs=1e-6)


class TestTheCityIsTheSameAfterACorkscrew:
    def test_once_the_tunnel_has_closed_nothing_has_moved(self):
        """A whole turn is level: once the tunnel is gone the skyline is
        exactly where it would have been with no corkscrew at all."""
        got = on_the_card(RIDER + textwrap.dedent("""
            shots = {}
            for twisted in (False, True):
                made, scene = rider_pane(size=(800, 500))
                with Clock() as clock:
                    for i in range(int(4.4 * 60)):
                        clock.step(1 / 60)
                        made.set_position(int((10 + i / 60) * 1000))
                        scene._placed = scene._laid = 1e9
                        scene._blocks = []
                        scene._twists = (10.6,) if twisted else ()
                        made._tick()
                        shot = made._canvas.grabFramebuffer()
                shots[twisted] = shot
            a, b = shots[False], shots[True]
            h, w = a.height(), a.width()
            points = [(x, y) for y in range(0, int(h * 0.45), 3)
                      for x in range(0, w, 3)]
            print(json.dumps({"tunnel": scene._tunnel_at(scene._heard),
                              "moved": sum(
                1 for x, y in points
                if abs(a.pixelColor(x, y).valueF()
                       - b.pixelColor(x, y).valueF()) > 0.15) / len(points)}))
        """))
        assert got["tunnel"] == 0.0, got
        assert got["moved"] < 0.02, (
            f"{got['moved']:.0%} of the sky and city differ after a corkscrew")
