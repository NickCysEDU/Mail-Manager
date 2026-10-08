"""The visualiser's frames, kept to a sixtieth of a second.

A timer of whole milliseconds at sixteen ran four per cent fast against
the screen, and every twenty-fifth frame met a refresh twice: Vaporwave,
Rave and Ambience hitched. The clock here sets the timer afresh each frame
for the moment the next tick is due, and a frame that comes late is given
the ticks it missed, so what moves keeps its speed. Measured on the card,
the share of frames shown a refresh apart went from 88 to 99.6 per cent at
full screen.
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
