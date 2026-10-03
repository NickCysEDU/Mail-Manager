"""What Music rider lays on its road for a given record. See rider_layout.

The road used to be laid from one rule for every record - the same eight
shapes in turn, a quarter of them obstacles - and every record rode alike,
only faster or slower. These are the rules that replaced it: which figures
a kind of music gets, how a part of a track walks them, and that every
record draws its own.
"""

from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import rider_layout
import trackstyle


def _style(seed=1, **traits):
    base = dict(steady=0.0, broken=0.0, heavy=0.0, swung=0.0, rolls=0.0,
                hard=0.0, melodic=0.0, calm=0.0, seed=seed)
    base.update(traits)
    return SimpleNamespace(**base)


def _section(kind, level=0.9, start=0.0, end=30.0):
    return trackstyle.Section(start, end, kind, level, kind != "break")


class TestWhichFigures:
    def test_four_to_the_floor_weaves_and_a_break_beat_steps(self):
        steady = rider_layout.weights(_style(steady=1.0), "drop", True)
        broken = rider_layout.weights(_style(broken=1.0), "drop", True)
        assert max(steady, key=steady.get) == "gate"
        assert max(broken, key=broken.get) == "chicane"

    def test_a_melody_is_climbed_and_a_hat_roll_is_ridden(self):
        tune = rider_layout.weights(_style(melodic=1.0), "break", False)
        rolls = rider_layout.weights(_style(rolls=1.0), "build", False)
        assert max(tune, key=tune.get) in ("stairs", "melody")
        assert max(rolls, key=rolls.get) == "stream"

    def test_obstacles_are_obstacles_and_prizes_are_prizes(self):
        assert set(rider_layout.weights(_style(), "drop", True)) <= set(
            rider_layout.DANGER)
        assert set(rider_layout.weights(_style(), "drop", False)) <= set(
            rider_layout.REWARD)


class TestHowManyAndHowOften:
    def test_a_drop_is_dangerous_and_a_break_is_not(self):
        assert rider_layout.danger_share("drop", "Mono") >= 0.35
        assert rider_layout.danger_share("break", "Mono") == 0.0
        assert rider_layout.danger_share("break", "Ninja") > 0.0
        assert (rider_layout.danger_share("drop", "Ninja")
                > rider_layout.danger_share("drop", "Mono"))

    def test_a_build_tightens_towards_its_drop(self):
        early = rider_layout.spacing("build", 0.0)
        late = rider_layout.spacing("build", 1.0)
        assert early >= 2.5 and late <= 1.2

    def test_never_closer_than_a_beat(self):
        for kind in rider_layout.SPACING:
            for through in (0.0, 0.5, 1.0):
                assert rider_layout.spacing(
                    kind, through, _style(broken=1.0, hard=1.0)) >= 1.0

    def test_louder_is_closer_and_still_a_beat_apart(self):
        """The section says what the figures are; how loud the track is
        where they land says how many, as Audiosurf's traffic does."""
        assert (rider_layout.spacing("break", 0.5, loud=1.0)
                < rider_layout.spacing("break", 0.5, loud=0.0))
        assert rider_layout.spacing("break", 0.5, loud=0.5) == (
            rider_layout.spacing("break", 0.5))
        assert rider_layout.spacing("drop", 1.0, _style(broken=1.0),
                                    loud=1.0) >= 1.0

    def test_whatever_the_table_says(self, monkeypatch):
        """A beat is the floor, not a figure in the table that happens to
        stay above it: a drop tightened to half a beat, and quickened
        again for a broken record, is still laid a beat apart."""
        monkeypatch.setitem(rider_layout.SPACING, "drop", (0.5, 0.5))
        assert rider_layout.spacing("drop", 0.5,
                                    _style(broken=1.0)) == 1.0


class TestEveryRecordItsOwn:
    def test_a_section_walks_a_palette_in_a_pattern(self):
        plan = rider_layout.Plan(_style(seed=5, steady=1.0))
        drop = _section("drop")
        figures = [plan.figure(drop, slot, True)[0] for slot in range(8)]
        assert figures[:4] == figures[4:8], "the walk does not repeat"
        assert len(set(figures)) >= 1

    def test_a_part_that_comes_back_is_itself_mirrored(self):
        plan = rider_layout.Plan(_style(seed=5, steady=1.0))
        first, second = _section("drop"), _section("drop", start=60, end=90)
        one = [plan.figure(first, slot, True) for slot in range(4)]
        two = [plan.figure(second, slot, True) for slot in range(4)]
        assert [f for f, _m in one] == [f for f, _m in two]
        assert not one[0][1] and two[0][1], "the second time is not mirrored"

    def test_different_records_draw_different_roads(self):
        """Fresh: over twenty records of the same kind of music, the
        drops alone come out many different ways."""
        seen = set()
        for seed in range(20):
            plan = rider_layout.Plan(_style(seed=seed * 7919, steady=0.6,
                                            broken=0.4, melodic=0.5))
            drop = _section("drop")
            seen.add(tuple(plan.figure(drop, slot, grey)[0]
                           for slot in range(4) for grey in (True, False)))
        assert len(seen) >= 8, f"only {len(seen)} ways for twenty records"

    def test_the_same_record_is_the_same_road_in_every_process(self):
        """Python salts the hash of a string per process; a palette seeded
        from one would give a record a new road every time the app
        started, and a best set on it would mean nothing."""
        code = ("import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'tests');"
                "import rider_layout, trackstyle, test_rider_layout as t;"
                "plan = rider_layout.Plan(t._style(seed=99, heavy=1.0));"
                "s = t._section('drop');"
                "print([plan.figure(s, i, g)[0] for i in range(4) for g in (0, 1)])")
        runs = set()
        for salt in ("1", "2", "3"):
            out = subprocess.run([sys.executable, "-c", code],
                                 capture_output=True, text=True,
                                 env={"PYTHONHASHSEED": salt, "PATH": ""},
                                 cwd=str(__import__("pathlib").Path(
                                     __file__).resolve().parent.parent))
            assert out.returncode == 0, out.stderr
            runs.add(out.stdout.strip())
        assert len(runs) == 1, runs


class TestTheMelody:
    HARMONY = {"lead": [60.0, 62.0, 64.0, 65.0, 67.0, 69.0, 71.0, 72.0,
                        None, 72.0, 71.0, 69.0],
               "rate": 4.0, "lead_from": 0.0}

    def test_low_notes_left_and_high_notes_right(self):
        low, high = rider_layout.lead_range(self.HARMONY, 0.0, 3.0)
        lanes = [rider_layout.lead_lane(self.HARMONY, i / 4.0, 3, low, high)
                 for i in range(8)]
        assert lanes == sorted(lanes) and lanes[0] == 0 and lanes[-1] == 2

    def test_which_way_it_goes(self):
        assert rider_layout.rising(self.HARMONY, 0.0) is True
        assert rider_layout.rising(self.HARMONY, 2.25) is False
        assert rider_layout.rising(None, 0.0) is None

    def test_where_new_notes_start(self):
        starts = rider_layout.lead_onsets(self.HARMONY, 0.0, 3.0)
        assert starts[:3] == [0.25, 0.5, 0.75]
        assert 2.25 in starts, "a note after a rest was missed"


class TestTheLevels:
    """Music rider's four levels."""

    LEVELS = ("Easy", "Normal", "Hard", "Expert")

    def test_each_is_harder_than_the_one_before(self):
        table = [rider_layout.level(name) for name in self.LEVELS]
        dangers = [row["danger"] for row in table]
        spacings = [row["spacing"] for row in table]
        looks = [row["look"] for row in table]
        scores = [row["score"] for row in table]
        assert dangers == sorted(dangers) and len(set(dangers)) == 4
        assert spacings == sorted(spacings, reverse=True)
        assert looks == sorted(looks, reverse=True)
        assert scores == sorted(scores) and scores[1] == 1.0

    def test_normal_is_the_game_as_it_was(self):
        for kind in rider_layout.SPACING:
            assert rider_layout.spacing(kind, 0.5, difficulty="Normal") == (
                rider_layout.spacing(kind, 0.5))
            assert rider_layout.danger_share(kind, "Mono",
                                             difficulty="Normal") == (
                rider_layout.danger_share(kind, "Mono"))

    def test_the_figures_come_closer_the_harder_it_is(self):
        easy, normal, expert = (rider_layout.spacing("groove", 0.5,
                                                     difficulty=name)
                                for name in ("Easy", "Normal", "Expert"))
        assert easy > normal > expert >= 1.0

    def test_the_hardest_still_leaves_a_beat_between(self):
        for kind in rider_layout.SPACING:
            assert rider_layout.spacing(
                kind, 1.0, _style(broken=1.0, hard=1.0), loud=1.0,
                difficulty="Expert") >= 1.0

    def test_a_level_there_is_none_of_is_normal(self):
        assert rider_layout.level("Nightmare") == rider_layout.level("Normal")

    def test_expert_is_twice_the_obstacles_and_never_nothing(self):
        """Twice Normal's where there are any, and something to dodge even
        in a break - unless the music is calm, which is ridden, not
        dodged, at every level."""
        for kind in ("drop", "groove", "build"):
            normal = rider_layout.danger_share(kind, "Mono")
            expert = rider_layout.danger_share(kind, "Mono",
                                               difficulty="Expert")
            assert expert >= min(rider_layout.DANGER_MOST, normal * 2.0) - 1e-9
        assert rider_layout.danger_share("break", "Mono") == 0.0
        assert rider_layout.danger_share("break", "Mono",
                                         difficulty="Expert") >= 0.15
        assert rider_layout.danger_share("break", "Mono",
                                         difficulty="Hard") > 0.0
        assert rider_layout.danger_share("break", "Mono", _style(calm=1.0),
                                         difficulty="Expert") == 0.0

    def test_there_is_always_something_to_take(self):
        for kind in rider_layout.DANGER_SHARE:
            for mode in ("Mono", "Ninja"):
                share = rider_layout.danger_share(
                    kind, mode, _style(heavy=1.0), difficulty="Expert")
                assert share <= rider_layout.DANGER_MOST < 1.0
