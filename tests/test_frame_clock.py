"""The visualiser's frames, kept to a sixtieth of a second.

A timer of whole milliseconds at sixteen ran four per cent fast against
the screen, and every twenty-fifth frame met a refresh twice: Vaporwave,
Rave and Ambience hitched. The clock here sets the timer afresh each frame
for the moment the next tick is due, and a frame that comes late is given
the ticks it missed, so what moves keeps its speed. Measured on the card,
the share of frames shown a refresh apart went from 88 to 99.6 per cent at
full screen. Where the Mac hands out the screen's refreshes, they keep the
frames instead, and the timer only stands in for them.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


class _Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def pane(qapp, monkeypatch):
    """A pane that keeps the clock (as on the card), with a hand-moved
    clock and its ticks counted."""
    from PySide6.QtWidgets import QWidget

    import attachment_widgets
    from attachment_widgets import Spectrum

    clock = _Clock()
    monkeypatch.setattr(attachment_widgets._time, "monotonic", clock)
    made = Spectrum()
    # Stands in for the card's canvas: all the clock asks is that there is
    # one.
    made._canvas = QWidget(made)
    ticks = []
    made._tick = lambda: ticks.append(clock.now)
    made._timer.start()
    yield made, clock, ticks
    made._timer.stop()
    made._canvas = None
    made.deleteLater()


def _frames(made, clock, gaps_ms):
    for gap in gaps_ms:
        clock.now += gap / 1000.0
        made._on_timer()


class TestTheClock:
    def test_a_frame_a_tick_at_sixty_a_second(self, pane):
        made, clock, ticks = pane
        made._on_timer()
        before = len(ticks)
        _frames(made, clock, [1000.0 / 60.0] * 120)
        assert len(ticks) - before == 120

    def test_the_timer_is_set_for_the_next_tick(self, pane):
        """Not a fixed sixteen: the next tick's moment, from where the
        clock has got to, so the frames average a sixtieth exactly."""
        from PySide6.QtCore import Qt

        made, clock, _ticks = pane
        assert made._timer.timerType() == Qt.TimerType.PreciseTimer
        made._on_timer()
        intervals = []
        for _ in range(60):
            clock.now += made._timer.interval() / 1000.0
            made._on_timer()
            intervals.append(made._timer.interval())
        assert set(intervals) <= {16, 17}
        assert abs(sum(intervals) / len(intervals) - 1000.0 / 60.0) < 0.2

    def test_a_late_frame_is_given_the_ticks_it_missed(self, pane):
        made, clock, ticks = pane
        made._on_timer()
        before = len(ticks)
        _frames(made, clock, [1000.0 / 30.0])
        assert len(ticks) - before == 2, "what moves would have slowed"
        _frames(made, clock, [1000.0 / 20.0])
        assert len(ticks) - before == 5

    def test_after_a_long_stop_it_starts_again_rather_than_racing(self, pane):
        made, clock, ticks = pane
        made._on_timer()
        before = len(ticks)
        _frames(made, clock, [2000.0])
        assert len(ticks) - before == 1

    def test_an_early_timer_waits_for_the_tick(self, pane):
        made, clock, ticks = pane
        made._on_timer()
        before = len(ticks)
        _frames(made, clock, [6.0])
        assert len(ticks) == before
        assert made._timer.interval() in (10, 11)

    def test_off_the_card_the_timer_ticks_as_it_always_did(self, pane):
        made, clock, ticks = pane
        made._canvas = None
        before = len(ticks)
        _frames(made, clock, [3.0, 3.0, 3.0])
        assert len(ticks) - before == 3
        assert made._tick_clock is None

    def test_frames_too_dear_for_a_sixtieth_leave_the_mouse_its_time(
            self, pane):
        """A clock that asked again at once after a frame of thirty
        milliseconds would keep the pane drawing and nothing else: the
        timer waits at least as long as such frames take."""
        made, clock, ticks = pane
        made._paced_ms = lambda: 30
        made._on_timer()
        assert made._timer.interval() >= 30
        # And a tick driven some other way slows the timer too.
        made._timer.setInterval(13)
        made._pace()
        assert made._timer.interval() == 30
        before = len(ticks)
        _frames(made, clock, [30.0])
        assert len(ticks) - before == 2, "and what moves keeps its speed"

    def test_the_pace_leaves_the_clocks_timer_alone(self, pane):
        made, clock, _ticks = pane
        made._on_timer()
        made._timer.setInterval(13)
        made._pace()
        assert made._timer.interval() == 13


class _Link:
    """Stands in for the screen's refreshes: what the pane asked of them."""

    def __init__(self) -> None:
        self.following = 0
        self.stopped = 0
        self.running = False

    def follow(self, widget) -> bool:
        self.following += 1
        self.running = True
        return True

    def stop(self) -> None:
        self.stopped += 1
        self.running = False


@pytest.fixture
def refreshing(pane):
    """The pane above, shown, with the screen's refreshes standing by."""
    made, clock, ticks = pane
    link = _Link()
    made._refreshes = link
    made.show()
    yield made, clock, ticks, link
    made.hide()


def _refreshes(made, clock, gaps_s, stamp=50.0, late=()):
    """A refresh after each gap, by the screen's clock; the call to the pane
    comes as late after it as ``late`` says, a main thread being busy."""
    for i, gap in enumerate(gaps_s):
        stamp += gap
        clock.now += gap
        lag = late[i % len(late)] if late else 0.0
        clock.now += lag
        made._refreshed(stamp)
        clock.now -= lag
    return stamp


def _steady(made, clock, stamp=50.0):
    """Refreshes at sixty until they keep the frames, the last at
    ``stamp``."""
    count = made.REFRESH_STEADY + 1
    stamp = _refreshes(made, clock, [1.0 / 60.0] * count,
                       stamp - count / 60.0)
    assert made._refreshing()
    return stamp


class TestTheScreensRefreshes:
    """On the card each frame starts at a refresh of the screen. Timed by
    the timer, frames fell wherever its phase put them in the refresh, and
    near the moment the screen took its picture one refresh in three showed
    a frame twice or skipped one."""

    def test_the_timer_asks_for_them(self, refreshing):
        made, _clock, _ticks, link = refreshing
        made._on_timer()
        assert link.following, "the frames never asked for the refreshes"

    def test_each_refresh_of_a_screen_at_sixty_is_a_tick(self, refreshing):
        """Counted by the refreshes' own moments, so a call that comes late
        or early, as a busy main thread makes it, is still one tick."""
        made, clock, ticks, _link = refreshing
        stamp = _steady(made, clock)
        before = len(ticks)
        each = []
        for lag in [0.0, 0.006, 0.0, 0.009, 0.002] * 24:
            count = len(ticks)
            stamp = _refreshes(made, clock, [1.0 / 60.0], stamp, late=[lag])
            each.append(len(ticks) - count)
            # The timer, firing meanwhile, adds nothing.
            clock.now += 0.004
            made._on_timer()
            clock.now -= 0.004
        assert len(ticks) - before == 120
        assert set(each) == {1}

    def test_a_refresh_missed_is_made_up_and_a_long_gap_starts_again(
            self, refreshing):
        made, clock, ticks, _link = refreshing
        stamp = _steady(made, clock)
        before = len(ticks)
        stamp = _refreshes(made, clock, [2.0 / 60.0], stamp)
        assert len(ticks) - before == 2, "what moves would have slowed"
        # A second without a refresh: the timer has had the frames, and the
        # refreshes have it again only once they come steadily.
        clock.now += 1.0
        made._on_timer()
        before = len(ticks)
        stamp = _refreshes(made, clock, [1.0], stamp)
        assert len(ticks) == before, "it raced through a second"

    @pytest.mark.parametrize("rate", [120.0, 59.94, 50.0])
    def test_another_screen_gets_sixty_ticks_a_second(self, refreshing, rate):
        """At a hundred and twenty, a tick every other refresh; within a
        tenth of sixty, one at every refresh; otherwise, by the time."""
        made, clock, ticks, _link = refreshing
        stamp = _refreshes(made, clock, [1.0 / rate] * 10)
        assert made._refreshing()
        before = len(ticks)
        each = []
        for _ in range(int(rate * 10)):
            count = len(ticks)
            stamp = _refreshes(made, clock, [1.0 / rate], stamp)
            each.append(len(ticks) - count)
        made_ticks = len(ticks) - before
        if rate == 120.0:
            assert made_ticks == 600 and set(each) == {0, 1}
            assert all(a != b for a, b in zip(each, each[1:])), (
                "two refreshes in a row went by with the same number")
        elif rate == 59.94:
            assert set(each) == {1}
        else:
            assert abs(made_ticks - 600) <= 1
            assert set(each) == {1, 2}

    def test_meanwhile_the_timer_only_watches(self, refreshing):
        made, clock, _ticks, _link = refreshing
        _steady(made, clock)
        assert made._timer.interval() == made._watching_ms() == 50

    def test_they_keep_the_frames_only_once_they_come_steadily(
            self, refreshing):
        """A screen that sends refreshes now and then (a hosted runner's
        sent two in a second and a half) took the frames at each, and left
        the timer's watch with nothing drawn; the timer keeps them now."""
        made, clock, ticks, _link = refreshing
        made._on_timer()
        stamp = 50.0
        each = []
        for _ in range(4):
            # The timer's frames for most of a second, a refresh among them.
            count = len(ticks)
            for _ in range(15):
                clock.now += 1.0 / 60.0
                made._on_timer()
            stamp += 0.25
            made._refreshed(stamp)
            assert not made._refreshing()
            each.append(len(ticks) - count)
        assert set(each) == {15}, (
            f"a quarter of a second drew {each} ticks: a stray refresh "
            f"stopped the timer's frames")
        assert made._timer.interval() < made._watching_ms()
        # Three in a row at the screen's pace, and they have them.
        for _ in range(made.REFRESH_STEADY):
            stamp = _refreshes(made, clock, [1.0 / 60.0], stamp)
        assert made._refreshing()

    def test_when_they_stop_the_timer_takes_the_frames_back(self, refreshing):
        made, clock, ticks, _link = refreshing
        _steady(made, clock)
        before = len(ticks)
        clock.now += made.TICK_S * (made.REFRESH_LOST - 0.5)
        made._on_timer()
        assert len(ticks) == before, "the timer drew while they came"
        clock.now += made.TICK_S
        made._on_timer()
        assert 1 <= len(ticks) - before <= made.CATCH_UP
        assert made._timer.interval() < made._watching_ms()

    @pytest.mark.parametrize("why", ["off the card", "analysing",
                                     "frames too dear", "timer stopped",
                                     "hidden"])
    def test_the_timer_keeps_the_frames_when(self, refreshing, why):
        made, clock, ticks, link = refreshing
        _steady(made, clock)
        if why == "off the card":
            made._canvas = None
        elif why == "analysing":
            made._working = 0.5
        elif why == "frames too dear":
            made._paced_ms = lambda: 30
        elif why == "timer stopped":
            made._timer.stop()
        else:
            made.hide()
        before = len(ticks)
        stopped = link.stopped
        _refreshes(made, clock, [1.0 / 60.0], 99.0)
        assert len(ticks) == before
        assert link.stopped > stopped, "the refreshes were left running"
        assert not made._refreshing()

    def test_a_hidden_pane_lets_them_go(self, refreshing):
        made, clock, _ticks, link = refreshing
        _steady(made, clock)
        made.hide()
        assert link.stopped and not link.running
        assert made._timer.interval() == made.FRAME_MS


class TestWhatWaitsForAFrame:
    """See Spectrum.after_the_frame."""

    def test_it_waits_while_frames_come_and_not_otherwise(self, refreshing):
        made, _clock, _ticks, _link = refreshing
        ran = []
        made.after_the_frame("a", lambda: ran.append("first"))
        made.after_the_frame("a", lambda: ran.append("second"))
        assert ran == []
        made._frame_shown()
        assert ran == ["second"], "the later call takes the earlier's place"
        made._timer.stop()
        made.after_the_frame("a", lambda: ran.append("at once"))
        assert ran[-1] == "at once"

    def test_a_track_put_down_forgets_it(self, refreshing):
        made, _clock, _ticks, _link = refreshing
        ran = []
        made.after_the_frame("a", lambda: ran.append(1))
        made.clear()
        made._frame_shown()
        assert ran == []

    def test_a_card_given_up_on_runs_it(self, refreshing):
        """No frame will be shown on it again."""
        made, _clock, _ticks, _link = refreshing
        ran = []
        made.after_the_frame("a", lambda: ran.append(1))
        made._drop_canvas()
        assert ran == [1]


class TestTheMusicsClockOnTheCard:
    """Run on by the time since the last tick, the music's clock took the
    timer's unevenness with it, and a road driven by it moved anywhere from
    nothing to 30 ms of the song a frame."""

    @staticmethod
    def _playing(qapp, monkeypatch):
        from array import array

        import attachment_audio
        import attachment_widgets
        from attachment_widgets import Spectrum
        from PySide6.QtWidgets import QWidget

        clock = _Clock()
        monkeypatch.setattr(attachment_widgets._time, "monotonic", clock)
        made = Spectrum()
        made.set_frames(
            [array("f", [0.4] * attachment_audio.BANDS) for _ in range(3000)],
            attachment_audio.RATE)
        made._canvas = QWidget(made)
        made.set_playing(True)
        player = {"at": 0.0}
        # A player that says where it is every 50 ms, as Qt's does.
        made.follow(lambda: int(player["at"] * 1000) // 50 * 50)
        # Where the music's clock was after each tick.
        made.heard = []
        real = made._tick

        def tick():
            real()
            made.heard.append(made._heard_now)

        made._tick = tick
        return made, clock, player

    @staticmethod
    def _play(made, clock, player, gaps_ms):
        made.heard.clear()
        for gap in gaps_ms:
            clock.now += gap / 1000.0
            player["at"] += gap / 1000.0
            made._on_timer()
        return list(made.heard)

    def test_each_tick_moves_it_a_sixtieth(self, qapp, monkeypatch):
        """Frames late and early by a few milliseconds, as a busy machine
        makes them: each still moves the music on by a sixtieth, leaning
        on the player by at most an eighth of that."""
        made, clock, player = self._playing(qapp, monkeypatch)
        try:
            self._play(made, clock, player, [1000.0 / 60.0] * 120)
            uneven = [16.7 + 5.0, 16.7 - 5.0, 16.7 + 2.0, 16.7 - 2.0] * 60
            heard = self._play(made, clock, player, uneven)
            steps = [b - a for a, b in zip(heard, heard[1:])]
            assert len(steps) > 200
            assert min(steps) >= made.TICK_S - made.LEAN - 1e-9
            assert max(steps) <= made.TICK_S + made.LEAN + 1e-9
            assert abs(heard[-1] - player["at"]) < 0.06, (
                "and it still keeps up with the music")
        finally:
            made._canvas = None
            made.deleteLater()

    def test_a_report_out_of_step_is_leaned_on_not_followed(
            self, qapp, monkeypatch):
        """A player says where it is when it can; one report 60 ms out of
        step moves the clock towards it by an eighth of a tick at most."""
        made, clock, player = self._playing(qapp, monkeypatch)
        try:
            self._play(made, clock, player, [1000.0 / 60.0] * 120)
            made.follow(lambda: int(player["at"] * 1000) // 50 * 50 + 60)
            heard = self._play(made, clock, player, [1000.0 / 60.0] * 3)
            made.follow(lambda: int(player["at"] * 1000) // 50 * 50)
            heard += self._play(made, clock, player, [1000.0 / 60.0] * 30)
            steps = [b - a for a, b in zip(heard, heard[1:])]
            assert max(steps) > made.TICK_S + made.LEAN * 0.9, (
                "the report never pulled at the clock")
            assert max(steps) <= made.TICK_S + made.LEAN + 1e-9
            assert min(steps) >= made.TICK_S - made.LEAN - 1e-9
        finally:
            made._canvas = None
            made.deleteLater()

    def test_time_the_frames_skipped_is_not_lost(self, qapp, monkeypatch):
        """A frame held up a tenth of a second starts the frames' clock
        again; the music's clock, run on a sixtieth a tick, fell that far
        behind and took a second to catch up, the blocks off the beat."""
        made, clock, player = self._playing(qapp, monkeypatch)
        try:
            self._play(made, clock, player, [1000.0 / 60.0] * 120)
            heard = self._play(made, clock, player, [150.0])
            # Where the music was before the player's last report moved on.
            assert abs(heard[-1] - player["at"]) < 0.05 + made.TICK_S
            heard = self._play(made, clock, player, [1000.0 / 60.0] * 6)
            assert abs(heard[-1] - player["at"]) < 0.05
        finally:
            made._canvas = None
            made.deleteLater()

    def test_nor_when_the_refreshes_keep_the_frames(self, qapp, monkeypatch):
        """The same held-up frame with the screen's refreshes keeping the
        frames: the timer takes them back, and the music is where the
        player is; then the refreshes have them again."""
        made, clock, player = self._playing(qapp, monkeypatch)
        made._refreshes = _Link()
        made._timer.start()
        made.show()
        try:
            stamp = 80.0

            def refresh(gap, lag=0.0):
                nonlocal stamp
                stamp += gap
                clock.now += gap + lag
                player["at"] += gap + lag
                made._refreshed(stamp)
                clock.now -= lag
                player["at"] -= lag

            for _ in range(120):
                refresh(1.0 / 60.0)
            assert made._refreshing()
            clock.now += 0.15
            player["at"] += 0.15
            made._on_timer()
            assert abs(made._heard_now - player["at"]) < 0.05 + made.TICK_S
            for _ in range(6):
                refresh(1.0 / 60.0)
            assert made._refreshing()
            assert abs(made._heard_now - player["at"]) < 0.05
            # A refresh whose call came late, then one on time three ticks
            # on: the refreshes still have the frames, and the ticks they
            # missed are made up in the music too.
            refresh(1.0 / 60.0, lag=0.03)
            refresh(4.5 / 60.0)
            assert made._refreshing()
            assert abs(made._heard_now - player["at"]) < 0.05
        finally:
            made.hide()
            made._timer.stop()
            made._canvas = None
            made.deleteLater()

    def test_and_the_controls_no_longer_slow_the_frames(self, qapp):
        """Halved while the full-screen controls were up, the frames ran at
        thirty a second whenever the mouse moved. On the card a frame
        leaves the controls most of the sixtieth."""
        from attachment_widgets import Spectrum
        from PySide6.QtWidgets import QWidget

        made = Spectrum()
        try:
            made._canvas = QWidget(made)
            ordinary = made._paced_ms()
            made.set_giving_way(True)
            assert made._paced_ms() == ordinary
            made._canvas = None
            made.set_giving_way(False)
            ordinary = made._paced_ms()
            made.set_giving_way(True)
            assert made._paced_ms() == int(ordinary * made.GIVE_WAY), (
                "off the card it still gives way")
        finally:
            made._canvas = None
            made.deleteLater()


class TestTheFieldBehindAmbience:
    def test_it_is_worked_out_every_frame_behind_ambience(self):
        import visualizers

        assert visualizers.Ambience()._plasma.every == 1
        assert visualizers.Plasma().every == visualizers.Plasma.EVERY == 2

    def test_it_moves_as_fast_however_often_it_is_worked_out(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        import visualizers
        from attachment_widgets import SpectrumState

        state = SpectrumState()
        state.bass, state.mid, state.high = 0.6, 0.4, 0.3
        image = QImage(64, 40, QImage.Format.Format_ARGB32_Premultiplied)
        drifts = []
        for every in (1, 2):
            field = visualizers.Plasma(every=every)
            painter = QPainter(image)
            for _ in range(12):
                field.paint(painter, QRectF(0, 0, 64, 40), state)
            painter.end()
            drifts.append((field._drift_a, field._drift_b, field._drift_c))
        for one, two in zip(*drifts):
            assert one == pytest.approx(two, rel=1e-9)


class TestTheBloomIsFineEnough:
    def test_a_window_glows_as_finely_as_full_screen(self, qapp):
        """An eighth of the window's frame was so coarse that, stretched
        back, the glow lay in blotches between the rave's lines."""
        from attachment_widgets import PostProcess

        window = PostProcess.halo_size(1580, 834)
        full = PostProcess.halo_size(2880, 1800)
        assert full.width() == 2880 // PostProcess.BLOOM_DIVISOR
        assert window.width() >= max(PostProcess.BLOOM_LEAST, full.width())
        # By halves, which the card's shrink averages exactly.
        assert (window.width(), window.height()) == (1580 // 4, 834 // 4)
        tiny = PostProcess.halo_size(100, 40)
        assert (tiny.width(), tiny.height()) == (50, PostProcess.BLOOM_MIN)
