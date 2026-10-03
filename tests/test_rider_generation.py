"""Music rider's road, laid for a whole record (see rider_layout, trackstyle).
Records of each genre written to known times (songkit) are ridden end to end
through the real game (ridekit), and what was laid is checked: which figures
and where, how many are obstacles, whether a good player gets through, and
whether two records ride alike.
"""

from __future__ import annotations

import collections
import os
import sys

import pytest

import ridekit
import songkit

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))


def _in(log, truth, kind):
    spans = [(a, b) for k, a, b in truth if k == kind]
    return [f for f in log.figures if any(a <= f[0] < b for a, b in spans)]


@pytest.fixture(scope="module")
def ridden():
    """Each kind of record, ridden once and kept for every test here."""
    out = {}
    for style in ("house", "dnb", "dubstep", "trap", "garage", "hardstyle",
                  "hiphop", "ambient"):
        out[style] = ridekit.ride(style)
    return out


class TestThePartsOfARecord:
    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "garage"])
    def test_nothing_to_dodge_in_a_break(self, ridden, style):
        _scene, log, truth, _beat = ridden[style]
        assert not [f for f in _in(log, truth, "break") if f[2]]

    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "trap",
                                       "garage", "hiphop"])
    def test_a_drop_is_where_the_danger_is(self, ridden, style):
        _scene, log, truth, _beat = ridden[style]
        drop = _in(log, truth, "drop")
        build = _in(log, truth, "build")
        share = sum(1 for f in drop if f[2]) / max(1, len(drop))
        assert 0.2 <= share <= 0.55, share
        assert share > sum(1 for f in build if f[2]) / max(1, len(build))

    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "trap",
                                       "garage", "hiphop"])
    def test_every_drop_has_its_share_not_just_the_first(self, ridden,
                                                          style):
        """Each drop on its own: counted together, a garage record's second
        drop hid that its figures had settled on the snares, with nothing to
        dodge."""
        _scene, log, truth, _beat = ridden[style]
        for start, end in [(a, b) for k, a, b in truth if k == "drop"]:
            figures = [f for f in log.figures if start <= f[0] < end]
            share = sum(1 for f in figures if f[2]) / max(1, len(figures))
            assert 0.25 <= share <= 0.55, (style, start, share)

    def test_a_build_tightens_as_the_drop_comes(self, ridden):
        _scene, log, truth, _beat = ridden["house"]
        build = [(a, b) for k, a, b in truth if k == "build"][0]
        times = [f[0] for f in log.figures if build[0] <= f[0] < build[1]]
        half = (build[0] + build[1]) / 2
        early = [t for t in times if t < half]
        late = [t for t in times if t >= half]
        assert len(late) > len(early), (len(early), len(late))

    def test_the_road_climbs_to_a_drop_and_falls_into_it(self, ridden):
        """Quiet passages climb and loud ones plunge downhill. Heights are
        depths here: larger is lower."""
        scene, _log, truth, _beat = ridden["house"]
        build = [(a, b) for k, a, b in truth if k == "build"][0]
        drop = [(a, b) for k, a, b in truth if k == "drop"][0]
        at = lambda when: scene._read(scene._hill, when)
        assert at(build[1]) < at(build[0]) - 3.0, "the build did not climb"
        assert at(drop[0] + 3.0) > at(drop[0]) + 2.0, "the drop did not fall"

    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "hiphop"])
    def test_a_corkscrew_lands_on_the_first_beat_of_a_drop(self, ridden,
                                                           style):
        scene, _log, truth, _beat = ridden[style]
        drops = [a for k, a, b in truth if k == "drop"]
        for twist in scene._twists:
            end = twist + scene.TWIST_FOR
            assert min(abs(end - drop) for drop in drops) < 0.6, (end, drops)
        assert scene._twists, "no corkscrew anywhere"


class TestEachKindOfRecordRidesItsOwnWay:
    #: The figures each kind of music is ridden with in its drops, one of
    #: which at least has to be there.
    SIGNATURE = {"house": ("gate", "wall"), "hardstyle": ("gate", "wall"),
                 "dnb": ("chicane", "run"), "garage": ("pair", "chicane"),
                 "dubstep": ("stream", "wall"), "trap": ("stream", "gate")}

    @pytest.mark.parametrize("style", sorted(SIGNATURE))
    def test_a_drop_is_ridden_the_way_the_music_moves(self, ridden, style):
        _scene, log, truth, _beat = ridden[style]
        figures = {f[1] for f in _in(log, truth, "drop")}
        assert figures & set(self.SIGNATURE[style]), (style, figures)

    def test_the_drops_of_different_music_are_different_figures(self, ridden):
        mixes = {}
        for style in ("house", "dnb", "dubstep", "garage", "hiphop", "trap"):
            _scene, log, truth, _beat = ridden[style]
            count = collections.Counter(f[1] for f in _in(log, truth, "drop"))
            mixes[style] = tuple(name for name, _n in count.most_common(3))
        assert len(set(mixes.values())) >= len(mixes) - 1, mixes

    def test_drum_and_bass_rides_at_its_own_tempo(self, ridden):
        scene, _log, _truth, beat = ridden["dnb"]
        assert abs(60.0 / scene._beat - 60.0 / beat) < 0.5
        assert scene._style.faster == 2.0

    def test_calm_music_has_nothing_to_dodge(self, ridden):
        _scene, log, _truth, _beat = ridden["ambient"]
        assert not [f for f in log.figures if f[2]]


def _in_bar(log, truth, beat, kind="drop"):
    """Where in the bar each figure of every ``kind`` part lands, in
    beats."""
    return [round((f[0] / beat) % 4.0, 2) % 4.0 for f in _in(log, truth, kind)]


class TestTheHeaviestHitNearby:
    def test_a_half_time_drop_is_ridden_on_one_and_three(self, ridden):
        """In half time the kick on one and the snare on three are what a head
        nods to. Ranked below the kick, the snare lost its slot to the kick
        half a beat later, and a dubstep drop was laid entirely on the and
        of three."""
        for style in ("dubstep", "trap"):
            _scene, log, truth, beat = ridden[style]
            places = _in_bar(log, truth, beat)
            assert places, style
            assert all(min(abs(p - 0.0), abs(p - 4.0), abs(p - 2.0)) < 0.05
                       for p in places), (style, sorted(set(places)))
            assert any(abs(p - 2.0) < 0.05 for p in places), style
            assert any(min(p, 4.0 - p) < 0.05 for p in places), style

    def test_a_heavier_hit_between_beats_does_not_take_the_slot(self, ridden):
        """Hip hop's snare on two is followed half a beat later by a heavier
        kick; the figure stays on the beat."""
        _scene, log, truth, beat = ridden["hiphop"]
        places = _in_bar(log, truth, beat)
        assert places
        assert all(min(abs(p - round(p)), abs(p - 4.0)) < 0.02
                   for p in places), sorted(places)

    @pytest.mark.parametrize("style", sorted(songkit.PATTERNS))
    def test_every_figure_starts_on_a_beat(self, style):
        """Swung, broken or half time, figures stay on the beat rather than the
        eighth."""
        scene, log, _truth, _beat = ridekit.ride(style, seconds=90.0)
        clock = scene._clock
        # The log keeps a figure's time to the millisecond.
        off = [round(when, 3) for when, _shape, _grey, _mirror in log.figures
               if abs(clock.number(when) - round(clock.number(when)))
               > 0.01]
        assert not off, off[:10]


class TestEveryBlockArrivesOnItsMoment:
    def test_between_the_beats_as_well_as_on_them(self):
        """The road lunges into each beat, and a block between beats placed
        where an even road would be arrived up to eighty milliseconds early.
        On the road's own curve each arrives within its frame."""
        scene, log, _truth, beat = ridekit.ride("garage", fps=60,
                                                seconds=50.0)
        assert len(log.crossings) > 60
        between = [(due, crossed) for due, crossed in log.crossings
                   if 0.15 < scene._clock.number(due) % 1.0 < 0.85]
        assert len(between) > 20, "hardly anything between the beats"
        late = [abs(crossed - due) for due, crossed in log.crossings]
        assert max(late) <= 1.0 / 60.0 + 1e-3, (
            f"a block crossed {max(late) * 1000:.0f} ms from its moment")

    def test_what_is_on_the_road_stays_where_it_is(self):
        """A block's place is fixed once it is in view: each beat's lunge is
        decided as the beat comes into view, so blocks in sight never move."""
        import visualizers

        # Well into a drop, where the lunge is strong, until something between
        # beats is in sight: a coin, or a run's later steps.
        _s, _l, truth, _b = ridekit.ride("garage", seconds=1.0)
        drop = [a for kind, a, _e in truth if kind == "drop"][0]
        scene, _log, _truth, _beat = ridekit.ride("garage",
                                                  seconds=drop + 6.0)
        state = scene._ridden_state
        clock = [2000.0]
        was = visualizers.time.monotonic
        visualizers.time.monotonic = lambda: clock[0]

        def on():
            clock[0] += 1 / 60.0
            state.at += 1 / 60.0
            state.kit = {"Bass": 1.0, "Kick": 1.0}
            state.bass = 1.0
            scene._step(state)

        try:
            for _frame in range(8 * 60):
                seen = [block for block in scene._blocks
                        if scene.RIDER_AT + 2.0 < scene._where(block[0])
                        < scene.FAR]
                between = [block for block in seen
                           if 0.15 < scene._clock.number(block[0]) % 1.0
                           < 0.85]
                if between:
                    break
                on()
            assert between, "nothing between the beats in sight"
            now = scene._beat_number(scene._heard)
            assert max(scene._lunge_of(n) for n in range(now, now + 4)) > 1.5
            before = [scene._where(block[0]) + scene._at for block in seen]
            # Through two whole beats, with the bass as hard as it goes,
            # frame by frame on the scene's own clock.
            for _frame in range(int(2 * scene._beat * 60) + 2):
                on()
                where = [scene._where(block[0]) + scene._at
                         for block in seen]
                assert where == pytest.approx(before, abs=1e-9)
        finally:
            visualizers.time.monotonic = was

    def test_a_hit_eases_the_lunge_of_the_beats_after_it(self):
        """A hit eases the push into the beats that come into view after it;
        those already in sight keep theirs."""
        import visualizers

        def lunges(slow):
            scene = visualizers.Rider()
            scene._beat, scene._origin = 0.5, 0.0
            scene._clock = visualizers.BeatClock(0.5, 0.0)
            scene._energy = [0.9] * 400
            scene._slow = slow
            scene._decide_lunges(0, 0.9)
            return [scene._lunge_of(n) for n in range(4)]

        eased, plain = lunges(visualizers.Rider.SLOW), lunges(1.0)
        assert all(a < b - 0.3 for a, b in zip(eased, plain)), (eased, plain)

    def test_starting_on_the_beat_is_not_a_burst_of_speed(self):
        """Until there is a beat to count from the road runs on its own clock,
        and switching to the beat's is a change of coordinates, not travel
        (counted as travel it was fifty-five units a second for a frame)."""
        import visualizers
        from attachment_widgets import SpectrumState

        scene = visualizers.Rider()
        state = SpectrumState()
        state.levels = [0.4] * 27
        state.kit = {}
        state.tempo = 120.0
        speeds = []
        for frame in range(30):
            at = frame / 60.0
            state.at = at
            # The beat's phase nowhere near the start of the file.
            state.beat_at = (at / 0.5 + 0.4) % 1.0
            scene._advance(state)
            speeds.append(scene._speed)
        steady = sorted(speeds)[len(speeds) // 2]
        assert steady > 0.0
        assert max(speeds) < 2.5 * steady, speeds[:4]


class TestEveryRecordIsItsOwnRoad:
    def test_the_same_record_is_the_same_road(self):
        _s, one, _t, _b = ridekit.ride("house", seconds=60.0)
        _s, two, _t, _b = ridekit.ride("house", seconds=60.0)
        assert one.figures == two.figures

    def test_whatever_the_frame_rate(self):
        """Laid a sixtieth of a second at a time, the drum chosen for a figure
        depended on where frames fell; committed short of what has been
        read, it does not."""
        _s, slow, _t, _b = ridekit.ride("dubstep", fps=24, seconds=60.0)
        _s, fast, _t, _b = ridekit.ride("dubstep", fps=60, seconds=60.0)
        assert slow.figures == fast.figures

    def test_two_records_of_one_kind_are_two_roads(self):
        _s, one, _t, _b = ridekit.ride("house", seconds=60.0)
        _s, two, _t, _b = ridekit.ride("house", seconds=60.0, nudge=0.02)
        assert [f[1:] for f in one.figures] != [f[1:] for f in two.figures]


class TestItCanBePlayed:
    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "trap",
                                       "garage", "hardstyle"])
    @pytest.mark.parametrize("mode", ["Mono", "Ninja"])
    def test_a_good_player_is_never_hit(self, style, mode):
        """Every obstacle can be passed: a player who looks ahead and moves out
        of the way is never hit, in any record, in either game with
        obstacles."""
        from playtest import steer_well

        scene, log, _truth, _beat = ridekit.ride(style, mode=mode,
                                                 steer=steer_well, fps=60)
        assert scene._hits == 0, f"{scene._hits} hits in {style}, {mode}"
        assert any(f[2] for f in log.figures), "nothing to dodge at all"

    @pytest.mark.parametrize("style", ["house", "dnb", "trap", "garage"])
    def test_never_all_three_lanes_at_once(self, ridden, style):
        _scene, log, _truth, _beat = ridden[style]
        greys = collections.defaultdict(set)
        for when, lane, _kind, _done, grey in log.blocks.values():
            if grey:
                greys[round(when, 2)].add(lane)
        assert all(len(lanes) < 3 for lanes in greys.values())


class TestARowOfCoins:
    def test_a_row_is_one_trail(self, qapp):
        """Two trails a bar apart are two rows: the second starts over."""
        import visualizers

        scene = visualizers.Rider()
        scene._lane = 1
        scene._lane_here = scene._lane_at(1)
        runs = []
        for start in (4.0, 6.0):
            for step in range(3):
                when = start + step * 0.16
                scene._heard = when + 0.01
                scene._blocks = [[when, 1, "coin", False, False]]
                scene._collide()
                runs.append(scene._coin_run)
        assert runs == [1, 2, 3, 1, 2, 3], runs


class TestTheRoadItself:
    def test_calm_music_turns_over_rarely(self, ridden):
        scene, _log, _truth, _beat = ridden["ambient"]
        assert len(scene._twists) <= 2, scene._twists

    def test_broken_music_turns_more_often_than_steady(self, ridden):
        """A turn every two bars on a broken beat; a sweep a phrase long on
        four to the floor."""
        def turns(style):
            scene, _log, _truth, _beat = ridden[style]
            bends = scene._bends(len(scene._energy))
            steps = [b - a for a, b in zip(bends, bends[1:])]
            return sum(1 for a, b in zip(steps, steps[1:])
                       if (a > 0) != (b > 0) and abs(a - b) > 1e-6)
        assert turns("dnb") > turns("house") * 1.5

    def test_the_flat_picture_shows_a_share_of_the_hills(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        scene, _log, _truth, _beat = ridekit.ride("house", seconds=5.0)
        image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        from attachment_widgets import SpectrumState

        state = SpectrumState()
        state.levels = [0.5] * 27
        scene.paint(painter, QRectF(0, 0, 320, 200), state)
        painter.end()
        assert scene._relief == scene.FLAT_RELIEF < 1.0

    def test_a_late_plan_does_not_move_the_road_in_view(self, monkeypatch):
        """The drums and tempo land a few seconds after the picture, and the
        road is planned again: the road in view stays where it was, and the
        road beyond follows the new plan."""
        import trackstyle

        scene, _log, _truth, _beat = ridekit.ride("house", seconds=20.0)
        state = scene._ridden_state
        ahead = [scene._heard + step * 0.5 for step in range(12)]
        far = [scene._heard + 20.0 + step for step in range(10)]
        near_before = [scene._read(scene._hill, when) for when in ahead]
        far_before = [scene._read(scene._hill, when) for when in far]
        real = trackstyle.read

        def all_breaks(*args, **kwargs):
            style = real(*args, **kwargs)
            for section in style.sections:
                section.kind = "break"
            return style

        monkeypatch.setattr(trackstyle, "read", all_breaks)
        scene._styled_from = None
        scene._carve(state)
        near_after = [scene._read(scene._hill, when) for when in ahead]
        far_after = [scene._read(scene._hill, when) for when in far]
        assert near_after == pytest.approx(near_before, abs=1e-9)
        assert far_after != pytest.approx(far_before, abs=1e-3)

    def test_the_drums_arriving_late_do_not_lurch_the_road(self):
        """Drum and bass first heard at 87, until the drums' reading lands
        twenty seconds in and says 174: counting the road again from its
        origin moved it 4,953 units a second for a frame, against sixteen
        and a half either side."""
        import statistics

        scene, log, _truth, _beat = ridekit.ride("dnb", seconds=40.0,
                                                 drums_at=20.0)
        settled = statistics.median(v for t, v in log.speeds if 25 < t < 35)
        around = max(v for t, v in log.speeds if 19.5 < t < 22)
        assert around < 2.0 * settled, (around, settled)
        assert scene._whole

    def test_a_change_of_tempo_lays_again_without_being_a_seek(self):
        scene, _log, _truth, _beat = ridekit.ride("dnb", seconds=30.0)
        assert scene._whole, "the drums' tempo arriving made the run a skip"


class TestTheMelodyLeadsThePrizes:
    def test_a_prize_goes_where_the_note_is(self):
        """Low notes left, high notes right: a rising tune takes the prizes
        across the road."""
        lead = [60.0 + (i // 8) % 12 for i in range(4 * 200)]
        harmony = {"lead": lead, "rate": 4.0, "lead_from": 0.0,
                   "key": {"tonic": 0, "mode": "major", "confidence": 0.3},
                   "tonal": 0.9, "chords": [], "tuning": 0.0}
        scene, log, _truth, _beat = ridekit.ride("ambient", harmony=harmony,
                                                 seconds=80.0)
        pairs = []
        for when, lane, kind, _done, grey in log.blocks.values():
            if kind == "block" and not grey:
                note = lead[min(len(lead) - 1, int(when * 4.0))]
                pairs.append((note, lane))
        low = [lane for note, lane in pairs if note <= 63]
        high = [lane for note, lane in pairs if note >= 69]
        assert low and high
        assert sum(high) / len(high) > sum(low) / len(low) + 0.8

    def test_runs_and_stairs_of_prizes_follow_it_too(self):
        """Every coloured block, including each in a run or a stair, in the
        lane of the note played at its moment."""
        import trackstyle
        import visualizers

        scene = visualizers.Rider()
        scene._clock = visualizers.BeatClock(0.5, 0.0)
        scene._beat = 0.5
        # A tune that climbs a note every quarter second.
        lead = [60.0 + i for i in range(40)]
        scene._harmony = {"lead": lead, "rate": 4.0, "lead_from": 0.0}
        scene._style = trackstyle.Style()
        scene._style.sections = [trackstyle.Section(0.0, 10.0, "break", 0.5,
                                                    False)]
        for pattern, when in (("run", 3.0), ("stairs", 6.0)):
            scene._blocks = []
            scene._shape(pattern, when, grey=False)
            placed = sorted((b[0], b[1]) for b in scene._blocks)
            expected = [visualizers.Rider._melody_lane(scene, at, -1)
                        for at, _lane in placed]
            assert [lane for _at, lane in placed] == expected, pattern
            assert -1 not in expected
        # Without a tune, the figure keeps its own shape.
        scene._harmony = None
        scene._blocks = []
        scene._shape("run", 3.0, grey=False)
        assert len({b[1] for b in scene._blocks}) == 3


class TestTheLevelsRideDifferently:
    def test_a_level_is_kept_through_a_change_of_game_and_back(self):
        import visualizers

        scene = visualizers.Rider()
        scene.set_difficulty("Expert")
        assert scene.difficulty == "Expert" and scene.LOOK_BEATS == 2.0
        scene.set_mode("Ninja")
        assert scene.difficulty == "Expert", "a new game forgot the level"
        scene.set_difficulty("Easy")
        assert scene._mode == "Ninja", "a new level forgot the game"
        assert scene.PER_BEAT == pytest.approx(
            (scene.FAR - scene.RIDER_AT) / 4.0)

    def test_harder_is_more_to_dodge_and_less_warning(self):
        counts = {}
        for level in ("Easy", "Normal", "Expert"):
            scene, log, truth, _beat = ridekit.ride("house", difficulty=level)
            drops = [(a, b) for k, a, b in truth if k == "drop"]
            figures = [f for f in log.figures
                       if any(a <= f[0] < b for a, b in drops)]
            counts[level] = (sum(1 for f in figures if f[2]), len(figures),
                             scene.PER_BEAT)
        assert counts["Easy"][0] < counts["Normal"][0] < counts["Expert"][0]
        # As many figures or more at harder levels: a kick on every beat is
        # laid a figure every two beats at any level; obstacles, warning and
        # the shield make it harder.
        assert counts["Easy"][1] <= counts["Normal"][1] <= counts["Expert"][1]
        # The road is fewer beats long, so it runs faster through them.
        assert counts["Expert"][2] > counts["Normal"][2] > counts["Easy"][2]

    def test_expert_has_no_shield_and_easy_s_comes_back_sooner(self):
        import visualizers

        expert = visualizers.Rider()
        expert.set_difficulty("Expert")
        assert expert._shield == 0.0 and expert._shield_back is None
        easy = visualizers.Rider()
        easy.set_difficulty("Easy")
        assert easy._shield_back < visualizers.Rider().SHIELD_BACK

    def test_a_point_is_worth_more_the_harder_it_is(self):
        import visualizers

        worth = {}
        for level in ("Easy", "Normal", "Expert"):
            scene = visualizers.Rider()
            scene.set_difficulty(level)
            worth[level] = scene._paid(100)
        assert worth == {"Easy": 75, "Normal": 100, "Expert": 170}

    @pytest.mark.parametrize("level", ["Hard", "Expert"])
    @pytest.mark.parametrize("style", ["house", "dnb", "dubstep", "garage"])
    @pytest.mark.parametrize("mode", ["Mono", "Ninja"])
    def test_a_good_player_is_never_hit_at_any_level(self, level, style,
                                                     mode):
        from playtest import steer_well

        scene, log, _truth, _beat = ridekit.ride(style, mode=mode,
                                                 steer=steer_well, fps=60,
                                                 difficulty=level)
        assert scene._hits == 0, f"{scene._hits} hits: {style} {mode} {level}"
        assert any(f[2] for f in log.figures)


class TestTheRoadRunsWithTheMusic:
    """Slow where the music is calm, fast where it drives: each beat's length
    of road from the loudness, weight and kind of part there (see
    Rider._pace_target)."""

    @staticmethod
    def _speed(log, spans):
        import statistics

        found = [speed for at, speed in log.speeds
                 if speed > 0.0 and any(a + 1.0 < at < b - 0.5
                                        for a, b in spans)]
        return statistics.mean(found)

    def test_calm_is_slow_and_a_drop_is_fast(self):
        _scene, log, truth, _beat = ridekit.ride("house")
        drop = self._speed(log, [(a, b) for k, a, b in truth if k == "drop"])
        for calm in ("intro", "break", "outro"):
            spans = [(a + (b - a) / 2.0, b) for k, a, b in truth if k == calm]
            assert drop > 3.5 * self._speed(log, spans), calm

    def test_a_build_speeds_up_into_its_drop(self):
        _scene, log, truth, _beat = ridekit.ride("house")
        for kind, start, end in truth:
            if kind != "build":
                continue
            part = (end - start) / 4.0
            first = self._speed(log, [(start, start + part)])
            last = self._speed(log, [(end - part, end)])
            assert last > 2.5 * first, (start, first, last)

    @pytest.mark.parametrize("level", ["Easy", "Normal", "Hard", "Expert"])
    def test_however_fast_there_is_the_level_s_warning(self, level):
        """A beat's road is never long enough to cross the road in sight faster
        than the level's least warning."""
        import rider_layout

        least = float(rider_layout.level(level)["warning"])
        worst = []

        def watch(scene):
            if not scene._clock:
                return
            for number, pace in scene._paces.items():
                seconds = scene._clock.length(scene._clock.time(number + 0.5))
                worst.append(scene.LOOK_BEATS * seconds / pace)

        for style in ("house", "dnb"):
            ridekit.ride(style, difficulty=level, steer=watch)
        assert worst and min(worst) >= least - 1e-6, (level, min(worst))

    def test_the_arches_are_turned_down_where_it_is_calm(self):
        lit = {}

        def watch(scene):
            if scene._clock and scene._style is not None:
                section = scene._section_at(scene._heard)
                # Inside a part, clear of the beats it starts and ends on.
                if (section is not None
                        and section.start + 1.0 < scene._heard
                        < section.end - 1.0):
                    now = scene._beat_number(scene._heard)
                    lit.setdefault(section.kind, []).append(
                        scene.gate_light(now))

        ridekit.ride("house", steer=watch)
        assert max(lit["break"][len(lit["break"]) // 2:]) < 0.35
        assert min(lit["drop"]) > 0.85

    def test_the_tall_arch_is_on_the_first_beat_of_the_bar(self):
        """Counted from the drums' first beat of a bar, not wherever the road
        started."""
        scene, _log, truth, _beat = ridekit.ride("house", seconds=40.0)
        clock = scene._clock
        assert clock.downbeat is not None
        drop = [a for kind, a, _b in truth if kind == "drop"][0]
        first = round(clock.number(drop))
        assert scene.bar_place(first) == 0
        assert [scene.bar_place(first + n) for n in range(1, 4)] == [1, 2, 3]

    def test_a_calm_part_winds_and_a_drop_runs_straighter(self):
        """How much the road in sight bends per unit, in calm parts against
        drops."""
        import statistics

        bends = {}

        def watch(scene):
            section = scene._section_at(scene._heard)
            if (section is None or not scene._curve
                    or not section.start + 2.0 < scene._heard
                    < section.end - 2.0):
                return
            across = [scene._road(scene.RIDER_AT + step)[0]
                      for step in range(0, 18)]
            turning = sum(abs(a - 2.0 * b + c) for a, b, c in
                          zip(across, across[1:], across[2:]))
            bends.setdefault(section.kind, []).append(turning)

        ridekit.ride("house", steer=watch)
        calm = statistics.mean(bends["break"] + bends["intro"])
        drop = statistics.mean(bends["drop"])
        assert calm > 20.0 * drop, (calm, drop)
