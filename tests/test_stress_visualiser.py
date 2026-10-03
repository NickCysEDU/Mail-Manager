"""The visualiser, given everything it should never be given.

An exception in paintEvent does not propagate: Qt prints it and carries on
with a painter open on the backing store, which then takes the process down,
so raising and crashing are the same outcome here.

Sizes nobody would choose, half-finished data, a playhead thrown around,
scenes switched faster than frames are drawn, and the strobe at both ends of
both sliders.
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
        """An exception out of paintEvent leaves a live painter on the backing
        store, which brings the process down some frames later; the wrapper
        has to close it whatever happens."""
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
    """Qt calls qFatal when a running QThread is destroyed, aborting with
    nothing to catch.

    The analysis hands the frames over and then goes on picking out the
    drums; with the handle released when the frames arrived, a window
    closing in between did exactly that.
    """

    def test_the_handle_is_not_released_until_the_thread_ends(self, qapp):
        import attachment_audio

        source = __import__("inspect").getsource(
            attachment_audio._Analysis._finished)
        assert "_LIVE.discard" not in source

    def test_a_thread_that_will_not_stop_is_kept_rather_than_destroyed(
            self, qapp, monkeypatch):
        """Waiting can time out, and dropping the reference then aborts the
        process, so it is kept and the thread finishes on its own."""
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


class TestNothingTheAnalysisSaysCanCloseTheWindow:
    """A level is nought to one and a tempo a count of beats by construction,
    not in fact: a bad decode, a zero calibration or a tempo looked for in
    silence can put a nan or an infinity in one of these numbers.

    These scenes accumulate what they are handed (the field's drift and the
    rider's envelope followers are running sums), so one bad value spoils
    every frame after it, and a nan that reaches ``int()`` raises out of
    paint. A nan tempo closed the window on the first frame, and an infinite
    level stopped the field moving for the rest of the session.
    """

    NAN, INF = float("nan"), float("inf")

    @staticmethod
    def _state(**kw):
        from attachment_widgets import SpectrumState

        said = SpectrumState()
        said.levels = kw.pop("levels", [0.4] * 27)
        said.bass = kw.pop("bass", 0.5)
        said.mid = kw.pop("mid", 0.4)
        said.high = kw.pop("high", 0.3)
        said.synth = kw.pop("synth", 0.2)
        said.kit = kw.pop("kit", {"Kick": 0.5, "Snare": 0.2, "Hats": 0.3})
        said.at = kw.pop("at", 0.0)
        said.chart = kw.pop("chart", {})
        said.tempo = kw.pop("tempo", 120.0)
        said.beat_at = kw.pop("beat_at", 0.0)
        for name, value in kw.items():
            setattr(said, name, value)
        return said

    @classmethod
    def _run(cls, scene, frames, said_for, size=(320, 200)):
        """Paint a scene through a run of states, on a held clock."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        image = QImage(size[0], size[1],
                       QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        clock = [1000.0]
        was = visualizers.time.monotonic
        visualizers.time.monotonic = lambda: clock[0]
        try:
            for frame in range(frames):
                clock[0] += 1 / 60.0
                image.fill(QColor(0, 0, 0))
                scene.paint(painter, QRectF(0, 0, size[0], size[1]),
                            said_for(frame))
        finally:
            painter.end()
            visualizers.time.monotonic = was
        return image

    def test_a_number_is_forced_back_into_the_range_it_claims(self):
        """Written out rather than worked out: every answer is a
        literal, so this cannot pass by agreeing with the code."""
        import visualizers

        assert visualizers.bounded(0.5) == 0.5
        assert visualizers.bounded(0.0) == 0.0
        assert visualizers.bounded(1.0) == 1.0
        assert visualizers.bounded(-3.0) == 0.0
        assert visualizers.bounded(9.0) == 1.0
        assert visualizers.bounded(self.INF) == 1.0
        assert visualizers.bounded(-self.INF) == 0.0
        # A nan is an answer that was never worked out, so it comes back as the
        # floor: there is no nearest bound.
        assert visualizers.bounded(self.NAN) == 0.0
        assert visualizers.bounded(self.NAN, least=0.25) == 0.25
        assert visualizers.bounded(None) == 0.0
        assert visualizers.bounded("loud") == 0.0
        assert visualizers.bounded(174.0, most=1000.0) == 174.0
        assert visualizers.bounded(4000.0, most=1000.0) == 1000.0

    def test_a_nan_tempo_does_not_close_the_window(self, qapp):
        """A nan tempo reached ``int(round(when / self._beat / ...))`` on the
        first frame and raised out of paint. A nan is truthy, and the tempo
        was tested for truth rather than for being a tempo.
        """
        import visualizers

        scene = visualizers.Rider()
        scene._last = None
        beats = tuple(i * 0.5 for i in range(200))
        self._run(scene, 40, lambda frame: self._state(
            at=frame / 60.0, tempo=self.NAN, chart={"Kick": beats}))
        assert scene._beat == 0.0, (
            f"a nan tempo was taken for a beat of {scene._beat}")
        assert math.isfinite(scene._at), (
            f"the road ended up at {scene._at}")

    def test_an_infinite_level_does_not_stop_the_field_moving(self, qapp):
        """The field's three drifts are running sums: one infinity never comes
        back, every sine in the picture is called on it, and the scene
        raises a domain error from then on.
        """
        import visualizers

        field = visualizers.Plasma()
        self._run(field, 8, lambda frame: self._state(
            at=frame / 60.0, bass=self.INF, mid=self.INF, high=self.INF,
            hue=self.INF))
        for name in ("_drift_a", "_drift_b", "_drift_c"):
            assert math.isfinite(getattr(field, name)), (
                f"the field's {name} is {getattr(field, name)} after "
                f"eight frames of an infinite level")
        # And it still draws a field afterwards rather than one colour.
        image = self._run(field, 8, lambda frame: self._state(
            at=1.0 + frame / 60.0))
        seen = {image.pixelColor(x, y).rgb()
                for x in range(0, 320, 5) for y in range(0, 200, 5)}
        assert len(seen) > 8, (
            f"the field draws {len(seen)} colours after a bad passage")

    def test_every_scene_survives_a_bad_passage_and_comes_back(self, qapp):
        """Every scene there is, not only the one the fault was found
        in: they share the field, the flash and the levels.
        """
        import visualizers

        nasty = {
            "nan": dict(levels=[self.NAN] * 27, bass=self.NAN,
                        mid=self.NAN, high=self.NAN, synth=self.NAN,
                        tempo=self.NAN, hue=self.NAN,
                        kit={"Kick": self.NAN}),
            "inf": dict(levels=[self.INF] * 27, bass=self.INF,
                        mid=self.INF, high=self.INF, synth=self.INF,
                        tempo=self.INF, hue=self.INF,
                        kit={"Kick": self.INF}),
            "negative": dict(levels=[-3.0] * 27, bass=-3.0, mid=-3.0,
                             high=-3.0, synth=-3.0, tempo=-120.0,
                             kit={"Kick": -3.0}),
        }
        beats = tuple(i * 0.5 for i in range(80))
        blank = []

        def settled(**kw):
            """Through the boundary the pane puts every frame through."""
            said = self._state(**kw)
            said.settle()
            return said

        for made in visualizers.SCENES:
            for name, kw in nasty.items():
                scene = type(made)()
                scene._last = None
                self._run(scene, 12, lambda frame: settled(
                    at=frame / 60.0, **kw), size=(240, 160))
                image = self._run(scene, 12, lambda frame: settled(
                    at=1.0 + frame / 60.0, chart={"Kick": beats}),
                    size=(240, 160))
                seen = {image.pixelColor(x, y).rgb()
                        for x in range(0, 240, 5)
                        for y in range(0, 160, 5)}
                if len(seen) < 3:
                    blank.append(f"{made.name} after {name}: "
                                 f"{len(seen)} colour(s)")
        assert not blank, (
            "these scenes stopped drawing after a bad passage: "
            + "; ".join(blank))

    def test_the_state_forces_every_number_back_into_its_range(self, qapp):
        """Written out rather than worked out."""
        from attachment_widgets import SpectrumState

        said = SpectrumState()
        said.levels = [self.NAN, 5.0, -2.0, 0.25]
        said.peaks = [self.INF, 0.5]
        said.bass = self.INF
        said.mid = -4.0
        said.high = self.NAN
        said.hit = 9.0
        said.hue = self.NAN
        said.tempo = self.NAN
        said.at = self.INF
        said.beat_at = 4.0
        said.kit = {"Kick": self.INF, "Snare": self.NAN, "Hats": 0.5}
        # A running angle rather than a level: it is allowed to be large,
        # it is not allowed to be infinite.
        said.phase = self.INF
        said.settle()
        assert said.levels == [0.0, 1.0, 0.0, 0.25]
        # Padded to the length of the levels, because a scene draws a
        # peak over the bar it belongs to and reads the two by the same
        # index.
        assert said.peaks == [1.0, 0.5, 0.0, 0.0]
        assert (said.bass, said.mid, said.high) == (1.0, 0.0, 0.0)
        assert (said.hit, said.hue, said.beat_at) == (1.0, 0.0, 1.0)
        assert said.tempo == 0.0, "a nan tempo has to read as no tempo"
        assert said.at == 86400.0
        assert said.phase == 1e9
        assert said.kit == {"Kick": 1.0, "Snare": 0.0, "Hats": 0.5}

    def test_a_peak_is_kept_for_every_bar_there_is(self, qapp):
        """Both ways round: a short list is padded and a long one is
        cut, because the scenes that draw the two together index them
        the same."""
        from attachment_widgets import SpectrumState

        said = SpectrumState()
        said.levels = [0.5] * 6
        said.peaks = [0.9]
        said.settle()
        assert said.peaks == [0.9, 0.0, 0.0, 0.0, 0.0, 0.0]
        said.peaks = [0.9] * 10
        said.settle()
        assert said.peaks == [0.9] * 6

    def test_the_pane_settles_every_frame_before_a_scene_sees_it(self, qapp):
        """The guarantee is only worth anything if it is wired in. Read off the
        pane's own painting rather than a call count, which would pass with
        the call moved anywhere."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        from attachment_widgets import Spectrum

        pane = Spectrum()
        pane.resize(320, 200)
        pane._state.bass = self.INF
        pane._state.tempo = self.NAN
        pane._state.levels = [self.NAN] * 27
        image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        try:
            pane._paint_scene(painter, QRectF(0, 0, 320, 200))
        finally:
            painter.end()
        assert pane._state.bass == 1.0, (
            f"the pane handed a scene a bass of {pane._state.bass}")
        assert pane._state.tempo == 0.0
        assert pane._state.levels == [0.0] * 27

    def test_the_rider_stays_a_game_through_a_hostile_song(self, qapp):
        """Not "does it raise": does it stay a game. The road only goes
        forwards, the craft stays on it, the grid never holds more than
        it has room for and nothing scores below nothing - whatever the
        song says."""
        import visualizers

        beats = tuple(i * 0.5 for i in range(400))
        songs = {
            "silence": lambda frame: self._state(
                at=frame / 60.0, levels=[0.0] * 27, bass=0.0, mid=0.0,
                high=0.0, synth=0.0, kit={}, tempo=0.0),
            "clipping": lambda frame: self._state(
                at=frame / 60.0, levels=[1.0] * 27, bass=1.0, mid=1.0,
                high=1.0, synth=1.0,
                kit={"Kick": 1.0, "Snare": 1.0, "Hats": 1.0},
                chart={"Kick": beats, "Snare": beats, "Hats": beats}),
            "three hundred bpm": lambda frame: self._state(
                at=frame / 60.0, tempo=300.0,
                chart={"Kick": tuple(i * 0.2 for i in range(600))}),
            "an infinite playhead": lambda frame: self._state(
                at=self.INF, chart={"Kick": beats}),
            "a playhead that never moves": lambda frame: self._state(
                at=7.0, chart={"Kick": beats}),
        }
        faults = []
        for name, feed in songs.items():
            scene = visualizers.Rider()
            scene._last = None
            was_at = None
            for frame in range(90):
                self._run(scene, 1, feed)
                got = scene.report()
                if not math.isfinite(scene._at):
                    faults.append(f"{name}: the road is at {scene._at}")
                elif was_at is not None and scene._at < was_at - 1e-6:
                    faults.append(f"{name}: the road went backwards "
                                  f"{was_at:.3f} -> {scene._at:.3f}")
                was_at = scene._at
                edge = scene.LANE_WIDE * scene.LANES / 2.0
                if not -edge <= scene._lane_here <= edge:
                    faults.append(f"{name}: the craft is at "
                                  f"{scene._lane_here:.2f}, off a road "
                                  f"{edge * 2:.2f} wide")
                if got["score"] < 0 or got["chain"] < 0 or got["hits"] < 0:
                    faults.append(f"{name}: {got}")
                for column, pile in enumerate(got["cells"]):
                    if len(pile) > scene.CELLS_DEEP:
                        faults.append(f"{name}: column {column} holds "
                                      f"{len(pile)}")
                if not 0.0 <= got["shield"] <= 1.0:
                    faults.append(f"{name}: the bumper is at "
                                  f"{got['shield']}")
                if faults:
                    break
        assert not faults, "; ".join(faults[:4])
