"""Skipping, pausing and starting a track in Music rider, through the real
pane, with a player that does what Qt's was measured doing: it says where it
is every 50 ms; after a seek it says the new place at once and holds it
while it starts again; it freezes on pause; and on resuming it holds and
then jumps ahead. See Spectrum.seek_to and Rider._advance."""

from __future__ import annotations

import statistics
import time
from array import array

import pytest

import ridekit
import songkit

FPS = 60
REPORT = 0.05
RESTART = 0.046
RESUME_AHEAD = 0.043


class Player:
    """Qt's media player as measured, on a clock this owns."""

    def __init__(self) -> None:
        self.audio = 0.0
        self.reported = 0
        self.playing = False
        self.paused = False
        self._since = 0.0
        self._waiting = 0.0
        #: Words from before the last seek still to be said, oldest first:
        #: a player whose position lags its seeks.
        self.late: list = []

    def position(self) -> int:
        if self.late:
            return self.late.pop(0)
        return self.reported

    def play(self) -> None:
        if self.paused:
            self.audio = self.reported / 1000.0 + RESUME_AHEAD
        self.playing, self.paused = True, False

    def pause(self) -> None:
        self.playing, self.paused = False, True
        self.audio = self.reported / 1000.0

    def seek(self, ms: int) -> None:
        self.reported = int(ms)
        self.audio = ms / 1000.0
        self._waiting = RESTART
        self._since = 0.0

    def advance(self, seconds: float) -> None:
        if not self.playing:
            return
        if self._waiting > 0.0:
            self._waiting -= seconds
        else:
            self.audio += seconds
        self._since += seconds
        if self._since >= REPORT:
            self._since -= REPORT
            self.reported = int(self.audio * 1000.0)


class Ride:
    """A written record in the real pane, the game on it, and the player."""

    def __init__(self, qapp, style="house", rhythm_after=None,
                 chart_after=None) -> None:
        import attachment_audio
        import beatmap
        import trackstyle
        import visualizers
        from attachment_widgets import Spectrum

        chart, contour, beat, self.truth = songkit.chart(style)
        self.length = len(contour["loud"]) / contour["rate"]
        self.kit = ridekit.kit_for(chart, self.length)
        counted = visualizers.folded_tempo(60.0 / beat)
        self.rhythm = trackstyle.rhythm_of(self.kit, tempo=counted)
        self.clock = [5000.0]
        self._was = time.monotonic
        time.monotonic = lambda: self.clock[0]
        self.player = Player()
        self.pane = Spectrum()
        self.pane.set_frames([array("f", [0.4] * 27)]
                             * int(self.length * attachment_audio.RATE),
                             attachment_audio.RATE)
        self.pane.set_contour(contour)
        kick = beatmap.BeatMap(beats=tuple(
            beatmap.Beat(at=self.rhythm["phase"] + i * 60.0 / counted,
                         strength=1.0)
            for i in range(int(self.length * counted / 60.0))),
            bpm=counted, locked=True)
        self.pane.set_beats({"Kick": kick})
        self.rider = visualizers.Rider()
        self.pane.set_scene(self.rider)
        self.pane.follow(self.player.position)
        self.at = 0.0
        self.chart_after = chart_after
        self.rhythm_after = rhythm_after
        if chart_after is None:
            self.pane.set_elements(self.kit)
        if rhythm_after is None:
            self.pane.set_rhythm(self.rhythm)
        else:
            self.pane.expect_rhythm()

    def close(self) -> None:
        time.monotonic = self._was
        self.pane.deleteLater()

    def play(self) -> None:
        self.player.play()
        self.pane.set_playing(True)

    def pause(self) -> None:
        self.player.pause()
        self.pane.set_playing(False)

    def seek(self, seconds: float) -> None:
        self.player.seek(int(seconds * 1000))
        self.pane.seek_to(int(seconds * 1000))

    def frame(self) -> dict:
        step = 1.0 / FPS
        self.clock[0] += step
        self.at += step
        self.player.advance(step)
        if self.chart_after is not None and self.at >= self.chart_after:
            self.pane.set_elements(self.kit)
            self.chart_after = None
        if self.rhythm_after is not None and self.at >= self.rhythm_after:
            self.pane.set_rhythm(self.rhythm)
            self.rhythm_after = None
        self.pane._tick()
        self.rider._step(self.pane._state)
        rider = self.rider
        return {"truth": self.player.audio, "now": self.pane._now,
                "heard": rider._heard, "road": rider._at,
                "blocks": {id(block): rider._where(block[0])
                           for block in rider._blocks
                           if block[2] != "coin"},
                "jumps": self.pane._jumps}

    def frames(self, seconds: float) -> list:
        return [self.frame() for _ in range(int(round(seconds * FPS)))]


@pytest.fixture
def ride(qapp):
    made = []

    def make(**options):
        made.append(Ride(qapp, **options))
        return made[-1]

    yield make
    for each in made:
        each.close()


def _relaid(rows) -> list:
    """The frames on which everything on the road was replaced at once."""
    out = []
    for index in range(1, len(rows)):
        before, after = rows[index - 1]["blocks"], rows[index]["blocks"]
        ahead = [key for key, where in before.items() if where > 4.0]
        if len(ahead) >= 3 and not set(ahead) & set(after):
            out.append(index)
    return out


class TestThePictureFollowsThePlayer:
    def test_after_a_seek_it_waits_for_the_player_to_move_on(self, ride):
        """Never ahead of what is playing - run on from the seek, it was
        most of a tenth of a second ahead and a second easing back - and
        exact from the player's first word after starting again."""
        made = ride()
        made.play()
        made.frames(2.0)
        made.seek(60.0)
        rows = made.frames(1.0)
        ahead = max(row["now"] - row["truth"] for row in rows)
        behind = max(row["truth"] - row["now"] for row in rows)
        assert ahead < 0.005, f"{ahead * 1000:.0f} ms ahead of the player"
        assert behind <= REPORT, f"{behind * 1000:.0f} ms behind it"
        spoke = next(index for index, row in enumerate(rows)
                     if made.pane._seek_hold is None and index > 0
                     and row["now"] > 60.0)
        assert all(abs(row["now"] - row["truth"]) < 0.005
                   for row in rows[spoke:])

    def test_a_late_report_from_before_a_seek_is_not_a_jump_back(self, ride):
        made = ride()
        made.play()
        made.frames(2.0)
        stale = made.player.reported
        made.seek(60.0)
        made.player.late = [stale, stale]
        rows = made.frames(1.0)
        assert min(row["now"] for row in rows) >= 59.99
        assert rows[-1]["jumps"] == rows[0]["jumps"]

    def test_paused_is_where_it_stopped(self, ride):
        made = ride()
        made.play()
        made.frames(3.0)
        made.pause()
        rows = made.frames(1.0)
        stopped = made.player.reported / 1000.0
        assert all(row["now"] == pytest.approx(stopped, abs=1e-9)
                   for row in rows)

    def test_starting_again_runs_on_from_where_it_stopped(self, ride):
        """Between the player's words the picture runs at the speed of
        time from the last one; started again after a pause, that word is
        where it stopped, as of now. Taken for a second-old word, the
        picture set off to catch up a quarter of a second."""
        made = ride()
        made.play()
        made.frames(3.0)
        made.pause()
        made.frames(1.0)
        stopped = made.player.reported / 1000.0
        made.player.audio = stopped
        made.play()
        made.player._since = -1.0          # no new word for a while
        rows = made.frames(0.2)
        for count, row in enumerate(rows):
            assert row["now"] == pytest.approx(stopped + count / FPS,
                                               abs=5e-4), count

    def test_a_jump_is_counted_once_and_playing_is_not_one(self, ride):
        made = ride()
        made.play()
        rows = made.frames(6.0)
        assert rows[-1]["jumps"] == rows[30]["jumps"]
        made.seek(30.0)
        assert made.frame()["jumps"] == rows[-1]["jumps"] + 1
        # And one nobody announced, made by the player alone.
        counted = made.frame()["jumps"]
        made.player.seek(90000)
        rows = made.frames(0.5)
        assert rows[-1]["jumps"] == counted + 1


class TestTheRiderFollowsThePicture:
    def test_its_clock_is_the_pane_s(self, ride):
        made = ride()
        made.play()
        rows = made.frames(4.0)
        made.pause()
        rows += made.frames(0.5)
        made.play()
        rows += made.frames(1.0)
        made.seek(50.0)
        rows += made.frames(1.0)
        assert all(row["heard"] == row["now"] for row in rows)

    def test_a_small_seek_is_a_seek(self, ride):
        """A fifth of a second back: counted as the jump it is, the road is
        laid again from there. Taken as movement, the road stood still for a
        fifth of a second and everything on it was that far off its beat."""
        made = ride()
        made.play()
        made.frames(20.0)
        made.seek(made.player.audio - 0.2)
        rider = made.rider
        for _frame in range(12):
            made.frame()
            # The road where the music is, so each block meets the craft
            # on its own moment.
            assert rider._at == pytest.approx(
                rider._world(rider._heard), abs=1e-6)

    def test_a_seek_lays_the_road_once_and_rides_on(self, ride):
        made = ride()
        made.play()
        made.frames(25.0)
        made.seek(70.0)
        rows = made.frames(3.0)
        assert _relaid(rows) == [], "laid again after the seek"
        roads = [row["road"] for row in rows]
        assert all(b >= a for a, b in zip(roads, roads[1:]))
        assert rows[-1]["blocks"], "nothing on the road after the seek"

    def test_a_pause_stops_the_road_and_it_goes_on_from_there(self, ride):
        made = ride()
        made.play()
        before = made.frames(30.0)[-60:]
        made.pause()
        paused = made.frames(1.0)
        assert len({row["road"] for row in paused}) == 1, "moved paused"
        assert paused[0]["heard"] == pytest.approx(
            made.player.reported / 1000.0)
        made.play()
        after = made.frames(1.5)
        # Never ahead of the player starting again, as a clock that took
        # the paused word for a second-old one was; and exact from its
        # first word since, which restarts it as a seek does.
        assert max(row["now"] - row["truth"] for row in after) < 0.005
        close = [index for index, row in enumerate(after)
                 if abs(row["now"] - row["truth"]) < 0.005]
        assert close and close[0] <= 20, (
            "a second and more easing towards the player's word")
        assert all(abs(row["now"] - row["truth"]) < 0.005
                   for row in after[close[0]:])
        normal = max(b["road"] - a["road"]
                     for a, b in zip(before, before[1:]))
        moves = [b["road"] - a["road"] for a, b in zip(after, after[1:])]
        assert min(moves) >= 0.0
        assert max(moves) < 1.3 * normal, "a spurt to catch up"
        assert _relaid(paused + after) == []


class TestATrackStartsClean:
    def test_nothing_is_laid_until_the_drums_beat_is_known(self, ride):
        made = ride(chart_after=0.4, rhythm_after=1.5)
        made.play()
        waiting = made.frames(1.4)
        assert all(not row["blocks"] for row in waiting)
        assert made.rider._beat_shown == 0.0

    def test_then_everything_comes_out_of_the_distance(self, ride):
        made = ride(chart_after=0.4, rhythm_after=1.5)
        made.play()
        rows = made.frames(12.0)
        first_seen = {}
        for row in rows:
            for key, where in row["blocks"].items():
                if where <= made.rider.IN_SIGHT and key not in first_seen:
                    first_seen[key] = where
        assert len(first_seen) > 10
        assert min(first_seen.values()) > made.rider.IN_SIGHT - 3.0, (
            "a block appeared part way down the road")
        assert _relaid(rows) == []
        moves = [b["road"] - a["road"] for a, b in zip(rows, rows[1:])]
        assert min(moves) >= 0.0
        assert max(moves) < 3.0 * statistics.median(moves), "the road jumped"

    def test_and_the_arches_come_up_with_the_beat(self, ride):
        made = ride(chart_after=0.4, rhythm_after=1.5)
        made.play()
        made.frames(1.6)
        assert 0.0 < made.rider._beat_shown < 0.5
        made.frames(1.0)
        assert made.rider._beat_shown == 1.0


class TestCountedAfreshInSight:
    def test_what_is_in_sight_stays_where_it_is(self, ride):
        """The drums' beat arriving after the ride began, or a record found
        to run at another tempo: the road is counted again, and every block
        in sight carries on from where it was."""
        made = ride()
        made.play()
        made.frames(20.0)
        before = made.frame()
        shifted = dict(made.rhythm, phase=made.rhythm["phase"] + 0.11)
        made.pane.set_rhythm(shifted)
        after = made.frame()
        travel = after["road"] - before["road"]
        assert travel > 0.0
        kept = [key for key in before["blocks"]
                if made.rider.RIDER_AT < before["blocks"][key]
                <= made.rider.IN_SIGHT]
        assert len(kept) >= 3
        for key in kept:
            assert key in after["blocks"]
            assert after["blocks"][key] == pytest.approx(
                before["blocks"][key] - travel, abs=1e-6)
