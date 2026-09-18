"""The visualiser, given everything it should never be given.

A scene runs inside paintEvent, and an exception raised there does not
propagate: Qt prints it and carries on with a painter still open on the
backing store, which then takes the process down. So "it raised" and "it
crashed" are the same outcome here, and every one of these is really the
same question - does it survive.

Sizes nobody would choose, data that arrived half-finished, a playhead
thrown around, scenes switched faster than frames are drawn, and the
strobe at both ends of both sliders.
"""

from __future__ import annotations

import math
import random
from array import array

import pytest
from PySide6.QtGui import QPainter, QPixmap

import attachment_audio
import beatmap
import visualizers
from attachment_widgets import Spectrum

#: Shapes a window can be dragged into, and a few nobody can.
SIZES = [
    (1, 1), (2, 2), (7, 3), (3, 7), (320, 12), (12, 320),
    (1920, 1080), (3840, 2160), (400, 4000), (4000, 400),
]


def painted(widget, width, height) -> None:
    """Draw one frame at this size, letting anything it raises out."""
    widget.setMinimumSize(0, 0)
    widget.setMaximumSize(16_777_215, 16_777_215)
    widget.resize(max(1, width), max(1, height))
    canvas = QPixmap(max(1, width), max(1, height))
    painter = QPainter(canvas)
    try:
        widget._paint(painter)
    finally:
        painter.end()


def loaded(seconds=6.0, seed=5):
    """Frames, traces and a kit, from a track with drums in it."""
    shake = random.Random(seed)
    rate = attachment_audio.DECODE_RATE
    total = int(rate * seconds)
    pcm = array("h", [0]) * (total * 2)
    beat = 60.0 / 128
    at = 0.0
    while at < seconds:
        start = int(at * rate)
        for step in range(int(rate * 0.12)):
            if start + step >= total:
                break
            here = (start + step) * 2
            value = int(20000 * math.exp(-step / (rate * 0.05))
                        * math.sin(2 * math.pi * 52 * step / rate))
            pcm[here] = max(-32768, min(32767, pcm[here] + value))
            pcm[here + 1] = pcm[here]
        start = int((at + beat / 2) * rate)
        for step in range(int(rate * 0.04)):
            if start + step >= total:
                break
            here = (start + step) * 2
            value = int(6000 * math.exp(-step / (rate * 0.008))
                        * shake.uniform(-1, 1))
            pcm[here] = max(-32768, min(32767, pcm[here] + value))
            pcm[here + 1] = pcm[here]
        at += beat
    frames = attachment_audio.analyse(pcm, rate, 2)
    shapes = attachment_audio.traces(pcm, rate, 2)
    vectors = attachment_audio.vector_traces(pcm, rate, 2)
    fine = attachment_audio.onset_frames(pcm, rate, 2)
    return (frames, shapes, vectors,
            beatmap.build(frames, attachment_audio.RATE),
            beatmap.elements(fine, attachment_audio.ONSET_RATE))


@pytest.fixture(scope="module")
def track():
    return loaded()


@pytest.fixture
def pane(qtbot, track):
    frames, shapes, vectors, beats, kit = track
    widget = Spectrum()
    qtbot.addWidget(widget)
    widget.set_unbounded(True)
    widget.set_traces(shapes, vectors)
    widget.set_frames(frames, attachment_audio.RATE)
    widget.set_beats(beats)
    widget.set_elements(kit)
    widget.set_labels([str(c) for c in attachment_audio.CENTRES])
    widget._reveal_changed(1.0)
    return widget


class TestEverySceneAtEverySize:
    @pytest.mark.parametrize("name", [s.name for s in visualizers.SCENES])
    def test_it_draws_at_any_shape_at_all(self, pane, name):
        pane.set_scene(visualizers.by_name(name))
        pane.set_position(1200)
        pane._tick()
        for width, height in SIZES:
            painted(pane, width, height)

    @pytest.mark.parametrize("name", [s.name for s in visualizers.SCENES])
    def test_it_draws_with_nothing_loaded(self, qtbot, name):
        widget = Spectrum()
        qtbot.addWidget(widget)
        widget.set_unbounded(True)
        widget.set_scene(visualizers.by_name(name))
        widget._reveal_changed(1.0)
        for width, height in SIZES[:6] + [(1280, 720)]:
            widget._tick()
            painted(widget, width, height)

    @pytest.mark.parametrize("name", [s.name for s in visualizers.SCENES])
    def test_it_draws_at_every_aspect_it_offers(self, pane, name):
        pane.set_scene(visualizers.by_name(name))
        pane._tick()
        for ratio in (None, 1.0, 16 / 9, 9 / 16, 4 / 3, 21 / 9, 0.05, 20.0):
            pane.set_aspect(ratio)
            painted(pane, 900, 600)
        pane.set_aspect(None)


class TestHalfFinishedData:
    """Everything arrives on its own schedule, and a scene can be asked to
    draw between any two of them."""

    def test_frames_but_no_traces(self, qtbot, track):
        frames, _shapes, _vectors, _beats, _kit = track
        widget = Spectrum()
        qtbot.addWidget(widget)
        widget.set_unbounded(True)
        widget.set_frames(frames, attachment_audio.RATE)
        widget._reveal_changed(1.0)
        for scene in visualizers.SCENES:
            widget.set_scene(scene)
            widget._tick()
            painted(widget, 800, 480)

    def test_traces_but_no_frames(self, qtbot, track):
        _frames, shapes, vectors, _beats, _kit = track
        widget = Spectrum()
        qtbot.addWidget(widget)
        widget.set_unbounded(True)
        widget.set_traces(shapes, vectors)
        widget._reveal_changed(1.0)
        for scene in visualizers.SCENES:
            widget.set_scene(scene)
            widget._tick()
            painted(widget, 800, 480)

    def test_a_kit_that_never_arrives(self, pane):
        pane.set_elements({})
        for scene in visualizers.SCENES:
            pane.set_scene(scene)
            for step in range(20):
                pane.set_position(step * 100)
                pane._tick()
                painted(pane, 700, 420)

    def test_a_kit_with_empty_maps(self, pane):
        pane.set_elements({name: beatmap.BeatMap()
                           for name in beatmap.ELEMENTS})
        pane.set_scene(visualizers.by_name("Rave"))
        for step in range(20):
            pane.set_position(step * 100)
            pane._tick()
            painted(pane, 700, 420)

    def test_a_kit_that_arrives_half_way_through(self, pane, track):
        _f, _s, _v, _b, kit = track
        pane.set_elements({})
        pane.set_scene(visualizers.by_name("Rave"))
        for step in range(20):
            pane.set_position(step * 150)
            pane._tick()
            painted(pane, 700, 420)
        pane.set_elements(kit)
        for step in range(20, 60):
            pane.set_position(step * 150)
            pane._tick()
            painted(pane, 700, 420)

    def test_frames_of_the_wrong_width(self, qtbot):
        """Rows of different lengths, which a damaged analysis could give."""
        widget = Spectrum()
        qtbot.addWidget(widget)
        widget.set_unbounded(True)
        ragged = [array("f", [0.5] * n) for n in (1, 3, 48, 2, 27, 60)]
        widget.set_frames(ragged, attachment_audio.RATE)
        widget._reveal_changed(1.0)
        for scene in visualizers.SCENES:
            widget.set_scene(scene)
            for step in range(8):
                widget.set_position(step * 90)
                widget._tick()
                painted(widget, 640, 400)


class TestThePlayheadIsThrownAround:
    def test_seeking_everywhere_including_out_of_range(self, pane):
        shake = random.Random(3)
        for scene in visualizers.SCENES:
            pane.set_scene(scene)
            for _ in range(30):
                pane.set_position(shake.randint(-50_000, 500_000))
                pane._tick()
                painted(pane, 800, 500)

    def test_running_backwards(self, pane):
        pane.set_scene(visualizers.by_name("Rave"))
        for step in range(120, 0, -1):
            pane.set_position(step * 50)
            pane._tick()
            painted(pane, 800, 500)

    def test_the_same_moment_over_and_over(self, pane):
        """A paused track. Nothing may pile up - not brightness, not a
        trail, not a list of beats waiting to fire."""
        pane.set_scene(visualizers.by_name("Oscilloscope"))
        pane.set_position(2000)
        for _ in range(240):
            pane._tick()
            painted(pane, 800, 500)
        assert pane._state.hit <= 1.0

    def test_scenes_switched_faster_than_frames_are_drawn(self, pane):
        shake = random.Random(9)
        for step in range(150):
            pane.set_scene(shake.choice(visualizers.SCENES))
            pane.set_position(step * 37)
            pane._tick()
            if step % 3 == 0:
                painted(pane, 640, 360)

    def test_resized_every_frame(self, pane):
        shake = random.Random(4)
        pane.set_scene(visualizers.by_name("Rave"))
        for step in range(60):
            pane.set_position(step * 60)
            pane._tick()
            painted(pane, shake.randint(1, 1400), shake.randint(1, 900))


class TestTheStrobeAtItsLimits:
    @pytest.mark.parametrize("rate", [0.0, 0.5, 1.0])
    @pytest.mark.parametrize("sense", [0.0, 0.5, 1.0])
    def test_both_sliders_at_every_end(self, pane, rate, sense):
        pane.set_strobe(True)
        pane.set_strobe_rate(rate)
        pane.set_strobe_sense(sense)
        pane.set_scene(visualizers.by_name("Rave"))
        for step in range(90):
            pane.set_position(step * 60)
            pane._tick()
            assert 0.0 <= pane._state.hit <= 1.0
            painted(pane, 700, 420)

    @pytest.mark.parametrize("source", list(Spectrum.STROBE_SOURCES))
    def test_every_source_can_be_listened_to(self, pane, source):
        pane.set_strobe(True)
        pane.set_strobe_source(source)
        for step in range(60):
            pane.set_position(step * 80)
            pane._tick()
            painted(pane, 700, 420)

    def test_a_source_that_does_not_exist(self, pane):
        pane.set_strobe(True)
        pane.set_strobe_source("Trombone")
        for step in range(30):
            pane.set_position(step * 80)
            pane._tick()
            painted(pane, 700, 420)

    def test_switching_source_every_frame(self, pane):
        shake = random.Random(6)
        pane.set_strobe(True)
        for step in range(120):
            pane.set_strobe_source(shake.choice(Spectrum.STROBE_SOURCES))
            pane.set_position(step * 50)
            pane._tick()
            painted(pane, 640, 380)


class TestTheScopeAtItsLimits:
    @pytest.mark.parametrize("decay", [0.0, 0.03, 0.75, 1.5, 99.0])
    def test_every_decay_including_impossible_ones(self, pane, decay):
        scope = visualizers.by_name("Oscilloscope")
        scope.set_decay(decay)
        pane.set_scene(scope)
        for step in range(40):
            pane.set_position(step * 70)
            pane._tick()
            painted(pane, 800, 500)
        scope.set_decay(0.28)

    @pytest.mark.parametrize("mode", ["Sweep", "X-Y", "nonsense"])
    def test_both_modes_and_one_that_is_not(self, pane, mode):
        scope = visualizers.by_name("Oscilloscope")
        scope.set_mode(mode)
        pane.set_scene(scope)
        for step in range(30):
            pane.set_position(step * 70)
            pane._tick()
            painted(pane, 800, 500)
        scope.set_mode("Sweep")

    def test_a_trace_of_one_sample(self, pane):
        scope = visualizers.by_name("Oscilloscope")
        pane.set_scene(scope)
        pane._state.trace = [0.5]
        pane._state.vector = [1]
        painted(pane, 700, 420)

    def test_an_empty_trace(self, pane):
        scope = visualizers.by_name("Oscilloscope")
        pane.set_scene(scope)
        pane._state.trace = []
        pane._state.vector = []
        painted(pane, 700, 420)


class TestTheMetersAtTheirLimits:
    @pytest.mark.parametrize("count", [1, 2, 3, 7, 10, 17, 64])
    def test_any_number_of_dials(self, pane, count):
        pane.set_scene(visualizers.by_name("VU meters"))
        pane._state.dials = [0.5] * count
        pane._state.dial_labels = [f"{n}Hz" for n in range(count)]
        for width, height in ((320, 200), (1280, 720), (1920, 1080),
                              (200, 900)):
            painted(pane, width, height)

    def test_readings_off_both_ends_of_the_scale(self, pane):
        pane.set_scene(visualizers.by_name("VU meters"))
        for value in (-5.0, -0.1, 0.0, 1.0, 1.15, 4.0, 1e9):
            pane._state.dials = [value] * 6
            pane._state.dial_labels = ["a"] * 6
            painted(pane, 900, 560)

    def test_labels_that_are_far_too_long(self, pane):
        pane.set_scene(visualizers.by_name("VU meters"))
        pane._state.dials = [0.4] * 4
        pane._state.dial_labels = ["x" * 400, "", "café ☕", "12345678901234"]
        painted(pane, 900, 560)


class TestItNeverLeavesAPainterOpen:
    def test_a_scene_that_raises_does_not_take_the_painter_with_it(
            self, pane, monkeypatch):
        """An exception out of paintEvent is caught by Qt, which then
        carries on with a live painter on the backing store and brings
        the process down some frames later. The wrapper has to close it
        whatever happens."""
        class Exploding:
            name = "Exploding"
            blurb = ""
            sharp_pixels = 0

            def paint(self, painter, rect, state):
                raise RuntimeError("boom")

        pane.set_scene(Exploding())
        canvas = QPixmap(400, 300)
        pane.resize(400, 300)
        # paintEvent swallows nothing; it just guarantees the painter is
        # ended. Calling it directly is the closest thing to what Qt does.
        from PySide6.QtGui import QPaintEvent
        from PySide6.QtCore import QRect

        with pytest.raises(RuntimeError):
            pane.paintEvent(QPaintEvent(QRect(0, 0, 400, 300)))
        # If the painter were still open on the widget, this would warn
        # and misbehave; it must simply work.
        painter = QPainter(canvas)
        assert painter.isActive()
        painter.end()


class TestQuittingWhileItIsStillWorking:
    """Qt calls qFatal when a running QThread is destroyed, and qFatal
    aborts the process - there is no exception to catch and no stack to
    read afterwards except a crash report.

    This is the shape of one that happened: the analysis hands the frames
    over and then goes on picking the drums out, and the window closes in
    between. The handle used to be released the moment the frames
    arrived, which was safe only while that was the last thing the thread
    did.
    """

    def test_the_handle_is_not_released_until_the_thread_ends(self, qapp):
        import attachment_audio

        source = __import__("inspect").getsource(
            attachment_audio._Analysis._finished)
        assert "_LIVE.discard" not in source

    def test_a_thread_that_will_not_stop_is_kept_rather_than_destroyed(
            self, qapp, monkeypatch):
        """Waiting can time out. Dropping the reference then is what
        aborts the process, so it is kept instead and the process exits
        while it finishes on its own."""
        import attachment_audio

        class Stubborn:
            def __init__(self):
                self.stopped = False

            def stop(self):
                self.stopped = True

            def isRunning(self):
                return True

            def wait(self, _ms):
                return False        # it did not stop

        handle = attachment_audio._Analysis.__new__(
            attachment_audio._Analysis)
        handle._stop = False
        handle._decoder = None
        thread = Stubborn()
        handle._thread = thread
        before = len(attachment_audio._ABANDONED)
        handle.cancel()
        assert thread.stopped
        assert thread in attachment_audio._ABANDONED, (
            "it let go of a thread that was still running")
        assert len(attachment_audio._ABANDONED) == before + 1
        attachment_audio._ABANDONED.remove(thread)

    def test_stopping_everything_cancels_every_live_analysis(self, qapp):
        import attachment_audio

        cancelled = []

        class Handle:
            def cancel(self):
                cancelled.append(self)

        one, two = Handle(), Handle()
        attachment_audio._LIVE.add(one)
        attachment_audio._LIVE.add(two)
        attachment_audio.stop_all()
        assert len(cancelled) == 2
        assert not attachment_audio._LIVE

    def test_shutdown_is_armed_when_an_analysis_starts(self, qapp):
        import attachment_audio

        attachment_audio._arm_shutdown()
        assert attachment_audio._ARMED
