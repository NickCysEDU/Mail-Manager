"""Where the beats fall, steady or moving, and everything that counts on them.

See beat_clock, trackstyle.follow, Spectrum._clock and Rider._world.
"""

from __future__ import annotations

import random
import statistics

import pytest

from beat_clock import BeatClock


def _changing(first_bpm=120.0, second_bpm=140.0, change=40.0, length=85.0,
              start=0.25):
    """Beat times that go from one tempo to another at ``change``."""
    times, at = [], start
    while at < change:
        times.append(at)
        at += 60.0 / first_bpm
    while at < length:
        times.append(at)
        at += 60.0 / second_bpm
    return times


def _onsets(times, length, rate=60.0, noise=0.05, seed=1):
    """An onset strength with a hit at each of ``times``."""
    rng = random.Random(seed)
    values = [rng.random() * noise for _ in range(int(length * rate))]
    for at in times:
        index = int(round(at * rate))
        for offset, weight in ((-1, 0.4), (0, 1.0), (1, 0.6), (2, 0.25)):
            if 0 <= index + offset < len(values):
                values[index + offset] += weight
    return values


def _error(found, truth):
    """Median and worst distance from each true beat to the nearest found
    one, in ms, leaving out the ends."""
    out = [min(abs(t - f) for f in found) for t in truth[4:-4]]
    return statistics.median(out) * 1000.0, max(out) * 1000.0


class TestTheClock:
    def test_a_steady_grid(self):
        clock = BeatClock(0.5, 0.2)
        assert clock.number(0.2) == pytest.approx(0.0)
        assert clock.number(1.45) == pytest.approx(2.5)
        assert clock.time(3.0) == pytest.approx(1.7)
        assert clock.length(10.0) == pytest.approx(0.5)
        assert clock.tempo(10.0) == pytest.approx(120.0)
        assert clock.nearest(1.31) == pytest.approx(1.2)
        assert clock.nearest(1.31, 2) == pytest.approx(1.2)
        assert clock.nearest(1.4, 2) == pytest.approx(1.45)

    def test_a_list_of_beats_and_beyond_its_ends(self):
        clock = BeatClock(times=[1.0, 1.5, 2.0, 2.4, 2.8])
        assert clock.number(1.75) == pytest.approx(1.5)
        assert clock.number(2.6) == pytest.approx(3.5)
        assert clock.time(3.5) == pytest.approx(2.6)
        # Before the first and after the last, at the end beats' length.
        assert clock.number(0.5) == pytest.approx(-1.0)
        assert clock.number(3.6) == pytest.approx(6.0)
        assert clock.time(-1.0) == pytest.approx(0.5)
        assert clock.length(1.2) == pytest.approx(0.5)
        assert clock.length(2.5) == pytest.approx(0.4)
        # Number and time undo each other.
        for when in (0.3, 1.1, 1.99, 2.41, 3.3):
            assert clock.time(clock.number(when)) == pytest.approx(when)

    def test_the_bar_counts_from_its_first_beat(self):
        clock = BeatClock(0.5, 0.0, downbeat=1.5)
        assert clock.in_bar(3.0) == pytest.approx(0.0)
        assert clock.is_downbeat(7) and not clock.is_downbeat(8)
        assert BeatClock(0.5, 0.0).is_downbeat(4)

    def test_no_tempo_is_no_clock(self):
        assert not BeatClock()
        assert not BeatClock(times=[1.0])
        assert BeatClock(0.5)


class TestFollowingATempoThatMoves:
    RATE = 60.0

    def test_a_steady_track_keeps_its_one_grid(self):
        import trackstyle

        beat = 60.0 / 128.0
        truth = [0.3 + i * beat for i in range(int(90 / beat))]
        assert trackstyle.follow(_onsets(truth, 92.0), self.RATE, 128.0,
                                 0.3) is None

    def test_a_change_of_tempo_is_followed(self):
        import trackstyle

        truth = _changing()
        found = trackstyle.follow(_onsets(truth, 86.0), self.RATE, 120.0,
                                  0.25)
        assert found is not None, "the change was not noticed"
        before = [t for t in truth if t < 36.0]
        after = [t for t in truth if t > 46.0]
        for part in (before, after):
            median, worst = _error(found, part)
            assert median < 6.0 and worst < 12.0, (median, worst)

    def test_a_tempo_that_drifts_up_is_followed(self):
        import trackstyle

        truth, at, bpm = [], 0.2, 120.0
        while at < 70.0:
            truth.append(at)
            bpm = 120.0 + 12.0 * min(1.0, at / 60.0)
            at += 60.0 / bpm
        found = trackstyle.follow(_onsets(truth, 71.0), self.RATE, 126.0,
                                  0.2)
        assert found is not None
        median, worst = _error(found, truth)
        assert median < 10.0 and worst < 60.0, (median, worst)

    def test_a_rhythm_changing_is_not_a_tempo_changing(self):
        """Half the stretches folding best at two thirds of the tempo is a
        part played in triplets, not a slower song."""
        import trackstyle

        beat = 60.0 / 120.0
        truth = []
        at = 0.25
        while at < 90.0:
            truth.append(at)
            # Every other bar, a hit a third of the way through each beat.
            if int(at / (beat * 8)) % 2 == 1:
                truth.append(at + beat / 3.0)
            at += beat
        assert trackstyle.follow(_onsets(sorted(truth), 92.0), self.RATE,
                                 120.0, 0.25) is None

    def test_the_drums_say_where_the_beats_are(self):
        from types import SimpleNamespace

        import trackstyle

        truth = _changing(change=30.0, length=70.0)
        kick = _onsets(truth, 70.0)
        quiet = [0.0] * len(kick)
        kit = {"Kick": SimpleNamespace(
                   flux=kick, rate=self.RATE,
                   beats=[SimpleNamespace(at=t) for t in truth]),
               "Snare": SimpleNamespace(flux=quiet, rate=self.RATE, beats=[]),
               "Hats": SimpleNamespace(flux=quiet, rate=self.RATE, beats=[])}
        found = trackstyle.rhythm_of(kit, tempo=120.0)
        assert found is not None and found.get("beats"), found
        median, _worst = _error(found["beats"], [t for t in truth if t < 26.0])
        assert median < 6.0


class TestThePaneCountsOnThem:
    def test_every_scene_s_pulse_follows_the_beats(self, qapp):
        import attachment_widgets

        times = _changing(change=10.0, length=20.0)
        pane = attachment_widgets.Spectrum()
        try:
            pane.set_rhythm({"tempo": 120.0, "phase": 0.25, "beats": times})
            state = pane._state
            for when in (times[3], times[25], times[30] + 0.0001):
                pane._now = when
                pane._clock(state)
                assert state.beat_at == pytest.approx(0.0, abs=1e-3) or (
                    state.beat_at == pytest.approx(1.0, abs=1e-3)), when
            # Half way between two beats of the faster part.
            pane._now = (times[30] + times[31]) / 2.0
            pane._clock(state)
            assert state.beat_at == pytest.approx(0.5, abs=1e-3)
            assert state.tempo == pytest.approx(140.0, abs=0.5)
        finally:
            pane.deleteLater()


class TestTheRoadCountsOnThem:
    def test_the_road_reaches_each_beat_when_it_is_played(self):
        """However the beats are spaced, the road is at the start of beat n
        exactly when beat n is played."""
        import visualizers

        times = _changing(change=8.0, length=16.0)
        scene = visualizers.Rider()
        scene._clock = BeatClock(times=times)
        scene._beat = 0.5
        scene._energy = tuple([0.2] * 40 + [0.9] * 40)
        scene._every = 5.0
        scene._decide_lunges(0, 0.5)
        for number in range(0, 20):
            scene._decide_lunges(number, 0.5)
            at = scene._world(times[number])
            assert at == pytest.approx(scene.beat_on_road(number), abs=1e-9)
        # And a block on any of them is level with the craft then.
        scene._at = scene._world(times[12])
        assert scene._where(times[12]) == pytest.approx(scene.RIDER_AT)

    def test_figures_land_on_the_beats_that_were_played(self):
        import visualizers

        times = _changing(change=6.0, length=14.0)
        scene = visualizers.Rider()
        scene._clock = BeatClock(times=times)
        for when in (times[5] + 0.03, times[20] - 0.04, times[25] + 0.01):
            snapped = scene._snap(when)
            assert min(abs(snapped - t) for t in times) < 1e-9
