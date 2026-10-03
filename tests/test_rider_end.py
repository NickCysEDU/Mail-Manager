"""The end of a ride: the finish, the results, and the best kept.

A song that finished used to leave the road running on with nothing to
say it was over. Audiosurf ends a ride with how it went, and a score
means more against the last one.
"""

from __future__ import annotations

import json
import re

import pytest


def _state(at, length=10.0, chart=()):
    from attachment_widgets import SpectrumState

    state = SpectrumState()
    state.levels = [0.5] * 27
    state.bass = state.mid = state.high = 0.5
    state.kit = {}
    state.at = at
    state.tempo = 120.0
    state.beat_at = ((at - 0.25) / 0.5) % 1.0
    state.chart = {"Kick": tuple(chart)}
    state.contour = {"loud": [0.5] * int(length * 8), "lean": [0.0] * int(
        length * 8), "rate": 8.0}
    return state


@pytest.fixture
def clock(monkeypatch):
    import visualizers

    now = [100.0]
    monkeypatch.setattr(visualizers.time, "monotonic", lambda: now[0])
    return now


def _ride(scene, clock, start, seconds, **state):
    first = None
    for index in range(int(seconds * 60)):
        clock[0] += 1.0 / 60.0
        made = _state(start + index / 60.0, **state)
        if first is None:
            first = made
        # The same shape and the same chart every frame, as the pane
        # gives them: a new chart is a new road, laid again from here.
        made.contour, made.chart = first.contour, first.chart
        scene._step(made)


class TestTheFinish:
    def test_the_end_of_the_track_ends_the_run_once(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        _ride(scene, clock, 8.5, 1.5)
        assert scene._finished and scene._result is not None
        finishes = [pop for pop in scene._pops if pop[0] == "finish"]
        assert len(finishes) == 1
        _ride(scene, clock, 9.9, 0.3)
        assert [pop for pop in scene._pops if pop[0] == "finish"] == finishes

    def test_not_before_the_end(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        _ride(scene, clock, 2.0, 3.0)
        assert not scene._finished

    def test_back_to_the_start_is_a_new_run(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        scene.set_mode("Ninja")
        _ride(scene, clock, 8.5, 1.5)
        assert scene._finished
        scene._score = 999
        _ride(scene, clock, 0.5, 0.2)
        assert not scene._finished
        assert scene._score == 0, "the old run's score carried into the new"
        assert scene._mode == "Ninja", "and it forgot which game it was"


class TestWhatARunIsJudgedOn:
    def _collide(self, scene, blocks, lane=1):
        scene._lane = lane
        scene._lane_here = scene._lane_at(lane)
        scene._heard = 5.0
        scene._blocks = [list(block) for block in blocks]
        scene._collide()

    def test_prizes_taken_out_of_those_that_went_by(self, qapp):
        import visualizers

        scene = visualizers.Rider()
        self._collide(scene, [(4.0, 1, "block", False, False),
                              (4.1, 0, "block", False, False),
                              (4.2, 2, "block", False, False),
                              (4.3, 0, "block", False, True)])
        # The grey that went by is something dodged, not a prize missed.
        assert (scene._taken, scene._offered) == (1, 3)

    def test_the_longest_chain_is_kept_when_it_breaks(self, qapp):
        import visualizers

        scene = visualizers.Rider()
        for index in range(5):
            self._collide(scene, [(4.0 + index * 0.01, 1, "block", False,
                                   False)])
        scene._shield = 0.0
        self._collide(scene, [(4.9, 1, "block", False, True)])
        assert scene._chain == 0 and scene._chain_most == 5

    def test_a_shield_save_is_counted(self, qapp):
        import visualizers

        scene = visualizers.Rider()
        scene._shield = 1.0
        self._collide(scene, [(4.0, 1, "block", False, True)])
        assert scene._saves == 1 and scene._hits == 0


class TestTheGrade:
    @pytest.mark.parametrize("share, hits, letter", [
        (1.0, 0, "S"), (0.95, 0, "S"), (0.95, 1, "A"), (0.86, 2, "A"),
        (0.86, 3, "B"), (0.72, 5, "B"), (0.55, 9, "C"), (0.49, 0, "D"),
        (1.0, 30, "D"),
    ])
    def test_both_have_to_be_earned(self, share, hits, letter):
        import visualizers

        assert visualizers.Rider.grade(share, hits) == letter


class TestTheCard:
    def test_it_comes_up_at_the_end_and_not_before(self, qapp, clock):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        def centre_light(scene):
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            scene._results(painter, QRectF(0, 0, 640, 400))
            painter.end()
            return sum(image.pixelColor(x, y).valueF()
                       for y in range(120, 280, 4) for x in range(220, 420, 4))

        scene = visualizers.Rider()
        _ride(scene, clock, 5.0, 0.5)
        assert centre_light(scene) == 0.0, "the card is up before the end"
        _ride(scene, clock, 9.8, 1.0)
        assert centre_light(scene) > 5.0, "the card did not come up"

    def test_a_new_best_says_so(self, qapp, clock):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        scene = visualizers.Rider()
        # The whole track: a best is only kept for one.
        _ride(scene, clock, 0.2, 10.6)
        said = []
        real = QPainter.drawText

        class Recording(QPainter):
            def drawText(self, *args):      # noqa: N802 - Qt's name
                said.append(args[-1])
                return real(self, *args)

        image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        scene.new_best = True
        painter = Recording(image)
        scene._results(painter, QRectF(0, 0, 640, 400))
        painter.end()
        assert "a new best" in said
        assert any(f"grade {scene._result['grade']}" in words
                   for words in said), said


class TestTheRideAtTheEnd:
    """Under the score, the whole ride along the track's shape."""

    def test_every_block_is_logged_as_it_was_scored(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        # No shield, so a grey in the lane is a hit.
        scene.set_difficulty("Expert")
        chart = [1.0 + i * 0.25 for i in range(34)]
        _ride(scene, clock, 0.2, 10.6, chart=chart)
        result = scene._result
        assert result["hits"] and result["taken"], result
        kinds = [kind for _when, kind, _hue in scene._log]
        assert kinds.count("taken") == result["taken"]
        assert kinds.count("hit") == result["hits"]
        assert kinds.count("saved") == result["saves"]
        assert kinds.count("coin") == result["coins"]
        assert kinds.count("taken") + kinds.count("missed") == \
            result["offered"]
        times = [when for when, _kind, _hue in scene._log]
        assert all(0.0 <= when <= 10.0 for when in times)
        assert scene._ridden and scene._ridden[2] == pytest.approx(10.0)

    def test_a_new_run_starts_a_new_log(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        scene._log.append((3.0, "hit", 0.0))
        scene.reset()
        assert scene._log == [] and scene._ridden is None

    @staticmethod
    def _drawn(scene, age, size=(640, 400)):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        scene._pops = [["finish", age, 1.0, 0.0, 0.0, ""]]
        image = QImage(*size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        scene._results(painter, QRectF(0, 0, *size))
        painter.end()
        return image

    @staticmethod
    def _finished(length=10.0):
        import visualizers

        scene = visualizers.Rider()
        scene._finished = True
        scene._result = scene.result()
        scene._ridden = ([0.5] * int(length * 8), 8.0, length)
        return scene

    @staticmethod
    def _red_below(image, x):
        """Red under the strip's line near ``x``: a hit."""
        found = 0
        for column in range(max(0, x - 3), min(image.width(), x + 4)):
            for row in range(int(image.height() * 0.76),
                             int(image.height() * 0.82)):
                colour = image.pixelColor(column, row)
                if colour.red() > 180 and colour.green() < 120:
                    found += 1
        return found

    def test_a_hit_is_marked_where_it_happened(self, qapp):
        scene = self._finished()
        scene._log = [(7.5, "hit", 0.0)]
        image = self._drawn(scene, 5.0)
        left, right = 640 * 0.07, 640 * 0.93
        at = int(left + (right - left) * 0.75)
        assert self._red_below(image, at) > 0, "no mark where the hit was"
        assert self._red_below(image, int(left + (right - left) * 0.25)) == 0

    def test_it_draws_itself_across(self, qapp):
        """The ride goes by again in a moment, from the start."""
        scene = self._finished()
        scene._log = [(1.0, "hit", 0.0), (9.0, "hit", 0.0)]
        left, right = 640 * 0.07, 640 * 0.93
        early = self._drawn(scene, scene.PLAYBACK * 0.3)
        assert self._red_below(early, int(left + (right - left) * 0.1)) > 0
        assert self._red_below(early, int(left + (right - left) * 0.9)) == 0
        late = self._drawn(scene, scene.PLAYBACK)
        assert self._red_below(late, int(left + (right - left) * 0.9)) > 0


class TestTheBests:
    def test_a_track_is_known_by_its_analysis_not_its_name(self):
        from array import array

        import rider_bests

        one = [array("f", [0.1 * i] * 27) for i in range(50)]
        two = [array("f", [0.2 * i] * 27) for i in range(50)]
        assert rider_bests.fingerprint(one) == rider_bests.fingerprint(list(one))
        assert rider_bests.fingerprint(one) != rider_bests.fingerprint(two)
        assert re.fullmatch(r"[0-9a-f]{20}", rider_bests.fingerprint(one))

    def test_a_best_is_kept_beaten_and_kept_apart_by_game(self, tmp_path):
        import rider_bests

        path = tmp_path / "bests.json"
        bests = rider_bests.Bests(path)
        assert bests.offer("abc", "Mono", 500) == (None, True)
        assert bests.offer("abc", "Mono", 300) == (500, False)
        assert bests.offer("abc", "Mono", 500) == (500, False), (
            "equalling a best is not beating it")
        assert bests.offer("abc", "Mono", 800) == (500, True)
        assert bests.offer("abc", "Ninja", 100) == (None, True)
        again = rider_bests.Bests(path)
        assert again.best("abc", "Mono") == 800
        assert again.best("abc", "Ninja") == 100

    def test_the_file_holds_nothing_that_names_the_music(self, tmp_path):
        """A fingerprint and a number per game, and nothing else."""
        from array import array

        import rider_bests

        path = tmp_path / "bests.json"
        track = rider_bests.fingerprint([array("f", [0.3] * 27)] * 20)
        rider_bests.Bests(path).offer(track, "Wakeboard", 1234)
        table = json.loads(path.read_text())
        assert table == {f"{track}:Wakeboard": 1234}

    def test_nothing_scored_is_not_a_best(self, tmp_path):
        import rider_bests

        bests = rider_bests.Bests(tmp_path / "bests.json")
        assert bests.offer("abc", "Mono", 0) == (None, False), (
            "a run that scored nothing said NEW BEST")

    def test_a_damaged_file_starts_afresh(self, tmp_path):
        import rider_bests

        path = tmp_path / "bests.json"
        path.write_text("{not json")
        bests = rider_bests.Bests(path)
        assert bests.offer("abc", "Mono", 10) == (None, True)


class TestThePaneKeepsTheBest:
    def test_a_finished_run_is_offered_once_and_answered(self, qapp,
                                                         tmp_path,
                                                         monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        import visualizers
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane._track_key = "0123456789abcdef0123"
            scene = visualizers.Rider()
            scene._result = {"mode": "Mono", "worth": 700}
            pane._keep_best(scene)
            assert scene.new_best is True and scene.best_before is None
            # The listener hears the scene every frame the card is up;
            # offered again, the run would be measured against itself and
            # the card would go from NEW BEST to "best 700" a frame later.
            pane._keep_best(scene)
            assert scene.new_best is True and scene.best_before is None, (
                "the same run was offered twice")
            later = visualizers.Rider()
            later._result = {"mode": "Mono", "worth": 650}
            pane._keep_best(later)
            assert later.best_before == 700 and later.new_best is False
            assert (tmp_path / "rider-bests.json").exists()
        finally:
            pane.deleteLater()

    def test_a_track_is_known_as_soon_as_its_picture_arrives(
            self, qapp, monkeypatch):
        from array import array
        from pathlib import Path

        import attachment_audio
        import rider_bests
        from attachment_view import AudioPane

        told = {}

        def decode(path, done, failed, progress, kit, bands, *rest):
            told["bands"] = bands
            return object()

        monkeypatch.setattr(attachment_audio, "decode", decode)
        pane = AudioPane()
        try:
            pane._start_analysis(Path("song"))
            frames = [array("f", [0.1 + 0.01 * (i % 9)] * attachment_audio.BANDS)
                      for i in range(200)]
            told["bands"]((frames, None, attachment_audio.contour(frames)))
            assert pane._track_key == rider_bests.fingerprint(frames)
        finally:
            pane._decoder = None
            pane.deleteLater()

    def test_a_new_track_forgets_the_last(self, qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane._track_key = "0123456789abcdef0123"
            pane.stop()
            assert pane._track_key is None
        finally:
            pane.deleteLater()


class TestTheFinishIsNeverCrowdedOut:
    def test_a_burst_of_pickups_cannot_push_it_out(self, qapp):
        import visualizers

        scene = visualizers.Rider()
        scene._pop("finish", strength=1.4)
        for _ in range(30):
            scene._pop("prize")
        kinds = [pop[0] for pop in scene._pops]
        assert "finish" in kinds and len(kinds) == 12


def _light(image, left, top, right, bottom):
    return sum(image.pixelColor(x, y).valueF()
               for y in range(top, bottom, 4) for x in range(left, right, 4))


class TestBothPicturesShowIt:
    """The card is drawn by the flat picture and by the lit one: each is
    checked by what it puts on the screen, with the card and without."""

    @staticmethod
    def _finished(clock):
        import visualizers

        scene = visualizers.Rider()
        _ride(scene, clock, 9.8, 1.0)
        assert scene._finished
        return scene

    def test_the_flat_picture(self, qapp, clock):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        scene = self._finished(clock)
        real = scene._results

        def drawn(card):
            scene._results = real if card else (lambda painter, rect: None)
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            scene.paint(painter, QRectF(0, 0, 640, 400), _state(10.5))
            painter.end()
            return image

        without, again, card = drawn(False), drawn(False), drawn(True)
        steady = abs(_light(without, 220, 120, 420, 280)
                     - _light(again, 220, 120, 420, 280))
        shown = abs(_light(card, 220, 120, 420, 280)
                    - _light(without, 220, 120, 420, 280))
        assert shown > max(5.0, steady * 4), (
            f"the flat picture never showed the card ({shown:.1f} "
            f"against {steady:.1f} frame to frame)")

    def test_the_lit_picture(self, qapp, clock):
        from types import SimpleNamespace

        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        scene = self._finished(clock)
        real = scene._results

        def drawn(card):
            scene._results = real if card else (lambda painter, rect: None)
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            scene._hud_on_card(painter, QRectF(0, 0, 640, 400), _state(10.5),
                               SimpleNamespace(hud=None))
            painter.end()
            return image

        assert (_light(drawn(True), 220, 120, 420, 280)
                > _light(drawn(False), 220, 120, 420, 280) + 5.0), (
            "the lit picture never showed the card")


class TestTheCountIsBesideTheGrid:
    """Puzzle's grid is in the bottom left corner, and so was the count of
    what it has cleared, on top of it."""

    @pytest.mark.parametrize("size", [(906, 270), (1440, 900), (600, 900),
                                      (2880, 1800)])
    def test_the_count_is_never_on_the_grid(self, qapp, clock, size):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import rider_gl
        import visualizers

        width, height = size
        rect = QRectF(0, 0, width, height)
        scene = visualizers.Rider()
        scene.set_mode("Puzzle")
        scene._cleared = 888
        hud = rider_gl.Hud()
        image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        try:
            hud.draw(painter, rect, scene, _state(10.5))
        finally:
            painter.end()
        grid = scene.matrix_box(rect)
        for name in ("chain", "chain label"):
            placed = hud.placed[name]
            assert placed.width() > 0
            assert not placed.intersects(grid), (
                f"at {width}x{height} the {name} is drawn on the grid")
            assert rect.contains(placed), f"the {name} is off the frame"


class TestTheLiveNumbersMakeWay:
    def test_the_score_goes_when_the_card_comes(self, qapp, clock):
        """Two scores on the screen at once, one counting and one final,
        is one too many: at the end the card says it."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import rider_gl
        import visualizers

        def top_middle(scene):
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            rider_gl.Hud().draw(painter, QRectF(0, 0, 640, 400), scene,
                                _state(10.5))
            painter.end()
            # Below the progress line, which stays: it says the track is
            # all the way through.
            return _light(image, 240, 12, 400, 60)

        scene = visualizers.Rider()
        scene._score = 4321
        assert top_middle(scene) > 3.0, "no score to take away"
        scene._finished = True
        assert top_middle(scene) == 0.0, "the running score is still up"


class TestTheCleanBonusIsPaidAtTheEnd:
    """The running score showed the clean bonus in, so it fell by a
    quarter the moment a grey was touched - including when the shield
    saved you, the one time the game says you did well. It is what has
    been earned; the bonus is said beside it and paid on the card."""

    @staticmethod
    def _saved(scene):
        scene._lane = 1
        scene._lane_here = scene._lane_at(1)
        scene._heard = 5.0
        scene._shield = 1.0
        scene._blocks = [[4.0, 1, "block", False, True]]
        scene._collide()
        assert scene._saves == 1 and not scene._clean

    def test_the_lit_score_does_not_fall_when_the_shield_saves_you(
            self, qapp, monkeypatch):
        from PySide6 import QtGui
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import rider_gl
        import visualizers

        drawn = []

        class Path(QtGui.QPainterPath):
            def addText(self, *args):      # noqa: N802 - Qt's name
                drawn.append(args[-1])
                return super().addText(*args)

        monkeypatch.setattr(QtGui, "QPainterPath", Path)
        now = [100.0]

        def later():
            now[0] += 0.5
            return now[0]

        monkeypatch.setattr(visualizers.time, "monotonic", later)
        import time as real_time
        monkeypatch.setattr(real_time, "monotonic", later)
        scene = visualizers.Rider()
        scene._score = 400
        hud = rider_gl.Hud()

        def frame():
            drawn.clear()
            image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
            image.fill(QColor(0, 0, 0))
            painter = QPainter(image)
            hud.draw(painter, QRectF(0, 0, 640, 400), scene, _state(3.0))
            painter.end()
            return list(drawn)

        for _ in range(10):
            said = frame()
        assert "400" in said, said
        assert "CLEAN +30%" in said, "what a clean run adds is not said"
        self._saved(scene)
        for _ in range(10):
            said = frame()
        assert "400" in said, f"the score changed when the shield saved: {said}"
        assert not any(word.startswith("CLEAN") for word in said)

    def test_the_flat_score_is_what_was_earned(self, qapp):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        said = []
        real = QPainter.drawText

        class Recording(QPainter):
            def drawText(self, *args):      # noqa: N802 - Qt's name
                said.append(args[-1])
                return real(self, *args)

        scene = visualizers.Rider()
        scene.set_mode("Ninja")
        scene._score = 400
        image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = Recording(image)
        scene._card(painter, QRectF(0, 0, 640, 400), 0.5)
        painter.end()
        line = said[-1]
        assert line.startswith("400 "), line
        assert "clean +60%" in line, line

    def test_the_card_pays_it(self, qapp, clock):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        scene = visualizers.Rider()
        scene._score = 400
        _ride(scene, clock, 9.8, 1.0)
        result = scene._result
        assert result["clean"] and result["worth"] == int(
            result["score"] * 1.3)
        said = []
        real = QPainter.drawText

        class Recording(QPainter):
            def drawText(self, *args):      # noqa: N802 - Qt's name
                said.append(args[-1])
                return real(self, *args)

        image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = Recording(image)
        scene._results(painter, QRectF(0, 0, 640, 400))
        painter.end()
        assert f"{result['worth']:,}" in said
        assert f"{result['score']:,}" in said and "+30%" in said, said


class TestASeekIsNotARide:
    """A jump of the playhead is not riding. Forward, the game met every
    block in the stretch it skipped in one frame and took the ones in the
    craft's lane - five thousand points and a new best for skipping to the
    last three seconds. Back, the stretch it went back over was empty."""

    LENGTH = 120.0

    @classmethod
    def _track(cls):
        # One chart and one shape for the whole ride, as the pane gives
        # them: a new chart is a new road by itself, and would hide
        # whether a seek is one.
        # On the beat every half second - and one hit just off it, at
        # 90.33, which the road puts on the beat at 90.25.
        made = _state(0.0, length=cls.LENGTH,
                      chart=tuple(sorted([0.25 + i * 0.5 for i in range(240)]
                                         + [90.33])))
        return made.chart, made.contour

    @classmethod
    def _play(cls, scene, clock, track, start, seconds):
        chart, contour = track
        for index in range(max(1, int(seconds * 60))):
            clock[0] += 1.0 / 60.0
            made = _state(start + index / 60.0, length=cls.LENGTH)
            made.chart, made.contour = chart, contour
            scene._step(made)

    def test_forward_takes_nothing_it_skipped(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, 5.0, 1.0)
        before = (scene._score, scene._taken, scene._offered, scene._hits,
                  scene._coins)
        # Landing just after that beat and just before that hit: the hit
        # is put on the beat, behind the craft.
        self._play(scene, clock, track, 90.28, 1 / 60)
        assert (scene._score, scene._taken, scene._offered, scene._hits,
                scene._coins) == before, "the skipped stretch was scored"
        assert all(block[0] > scene._heard - 0.05 for block in scene._blocks), (
            "blocks from before the seek are still on the road")
        self._play(scene, clock, track, 90.3, 0.5)
        assert any(block[0] > scene._heard for block in scene._blocks), (
            "and nothing is laid after it")

    def test_back_lays_the_stretch_again(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, 60.0, 1.0)
        self._play(scene, clock, track, 30.0, 1 / 60)
        ahead = [block for block in scene._blocks
                 if 30.0 < block[0] < 30.0 + scene.READ]
        assert ahead, "the road back over is empty"

    def test_a_skipped_run_is_judged_but_kept_as_no_best(self, qapp, clock,
                                                         tmp_path,
                                                         monkeypatch):
        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, 0.2, 2.0)
        assert scene._whole
        self._play(scene, clock, track, self.LENGTH - 1.0, 1.0)
        assert scene._finished and scene._result["whole"] is False
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane._track_key = "0123456789abcdef0123"
            pane._keep_best(scene)
            assert not (tmp_path / "rider-bests.json").exists(), (
                "a skipped ride was kept as the track's best")
            assert scene.new_best is False and scene.best_before is None
        finally:
            pane.deleteLater()

    def test_a_run_started_part_way_in_is_not_whole(self, qapp, clock):
        import visualizers

        scene = visualizers.Rider()
        self._play(scene, clock, self._track(), 40.0, 0.5)
        assert scene._whole is False

    def test_a_run_from_the_top_is_whole_and_left_alone(self, qapp, clock):
        """And the first two seconds of one are not taken for a seek back
        to the start over and over."""
        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, 0.4, 3.0)
        assert scene._whole
        assert scene._heard > 3.0, f"the run stuck at {scene._heard:.2f}"

    def test_a_nudge_near_the_start_is_not_a_new_run(self, qapp, clock):
        """Back to the start is a new run when it comes from further on,
        not a jump of a fraction of a second inside the first two."""
        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, 0.2, 0.8)
        scene._score = 50
        self._play(scene, clock, track, 1.6, 0.2)
        assert scene._score == 50, "a nudge threw the run away"

    def test_back_to_the_start_part_way_through_is_a_new_run(self, qapp,
                                                             clock):
        import visualizers

        scene = visualizers.Rider()
        scene.set_mode("Ninja")
        track = self._track()
        self._play(scene, clock, track, 0.2, 1.0)
        self._play(scene, clock, track, 50.0, 1.0)
        assert scene._whole is False
        scene._score = 500
        self._play(scene, clock, track, 0.6, 1.0)
        assert scene._score == 0 and scene._whole, "the skipped run went on"
        assert scene._mode == "Ninja"

    def test_the_card_says_so(self, qapp, clock):
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        scene = visualizers.Rider()
        track = self._track()
        self._play(scene, clock, track, self.LENGTH - 1.0, 1.0)
        said = []
        real = QPainter.drawText

        class Recording(QPainter):
            def drawText(self, *args):      # noqa: N802 - Qt's name
                said.append(args[-1])
                return real(self, *args)

        image = QImage(640, 400, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = Recording(image)
        scene._results(painter, QRectF(0, 0, 640, 400))
        painter.end()
        assert any("no best" in words for words in said), said

    def test_a_corkscrew_keeps_its_power_block_when_the_road_is_laid_again(
            self, qapp, clock):
        """The drums arrive a few seconds after the picture and the road
        is laid again for them. A corkscrew already in view had had its
        power block, and never got it back."""
        import visualizers

        scene = visualizers.Rider()
        chart, contour = self._track()
        self._play(scene, clock, (chart, contour), 10.0, 0.2)
        # Just past what is laid, so that it comes into view.
        scene._twists = (scene._laid + 0.1,)
        self._play(scene, clock, (chart, contour), 10.2, 0.4)
        assert any(block[2] == "power" for block in scene._blocks)
        again = dict(chart)
        self._play(scene, clock, (again, contour), 10.6, 0.2)
        assert any(block[2] == "power" for block in scene._blocks), (
            "the power block went with the road it was laid on")


class TestEachLevelKeepsItsOwnBest:
    def test_normal_is_where_the_old_bests_are_and_the_rest_apart(self):
        from attachment_view import AudioPane

        assert AudioPane.best_kept_as({"mode": "Mono"}) == "Mono"
        assert AudioPane.best_kept_as({"mode": "Mono",
                                       "difficulty": "Normal"}) == "Mono"
        assert AudioPane.best_kept_as({"mode": "Ninja",
                                       "difficulty": "Expert"}) == (
            "Ninja Expert")

    def test_the_level_is_chosen_remembered_and_told_to_the_game(self, qapp):
        import visualizers
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.level_box.setCurrentText("Hard")
            assert visualizers.by_name("Music rider").difficulty == "Hard"
        finally:
            pane.deleteLater()
        again = AudioPane()
        try:
            assert again.level_box.currentText() == "Hard"
            again.level_box.setCurrentText("Normal")
        finally:
            again.deleteLater()

    def test_it_is_there_only_for_the_game(self, qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.resize(900, 700)
        pane.show()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Music rider")
            qapp.processEvents()
            assert pane.level_box_holder.isVisible()
            pane.scene_box.setCurrentText("Rave")
            qapp.processEvents()
            assert not pane.level_box_holder.isVisible()
        finally:
            pane.close()
            pane.deleteLater()


class TestAChoiceOutlivesARun:
    """The pane starts a scene afresh when it is picked and when a track
    loads, and afresh was the first game at the first level: every ride was
    Normal Mono whatever the boxes said. What the viewer chose is kept
    (Scene.KEPT); what a run has done is not."""

    @staticmethod
    def _back_to_how_it_was():
        import visualizers

        rider = visualizers.by_name("Music rider")
        rider.set_difficulty("Normal")
        rider.set_mode("Mono")
        scope = visualizers.by_name("Oscilloscope")
        scope.set_mode("Sweep")
        scope.set_decay(0.28)

    def test_picking_the_scene_and_loading_a_track_keep_the_level(self,
                                                                  qapp):
        import rider_layout
        import visualizers
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Music rider")
            pane.game_box.setCurrentText("Ninja")
            pane.level_box.setCurrentText("Expert")
            rider = visualizers.by_name("Music rider")
            rider._score = 999
            pane.scene_box.setCurrentText("Rave")
            pane.scene_box.setCurrentText("Music rider")
            pane.spectrum.clear()
            assert rider.difficulty == "Expert"
            assert rider.LOOK_BEATS == 2.0, "Expert's road, not Normal's"
            assert rider.PER_BEAT == pytest.approx(
                (rider.FAR - rider.RIDER_AT) / 2.0)
            assert rider._shield_back is None, "Expert has no shield"
            assert rider._score_share == pytest.approx(
                rider_layout.level("Expert")["score"])
            assert rider.mode == "Ninja"
            # And the run itself is new.
            assert rider._score == 0
        finally:
            pane.level_box.setCurrentText("Normal")
            self._back_to_how_it_was()
            pane.deleteLater()

    def test_going_back_to_the_start_keeps_it_too(self):
        import visualizers

        rider = visualizers.Rider()
        rider.set_mode("Puzzle")
        rider.set_difficulty("Hard")
        rider._finished = True
        rider._length = lambda state: 200.0

        class State:
            at = 0.5
        rider._finish(State())
        assert rider._finished is False, "not a new run"
        assert (rider.mode, rider.difficulty) == ("Puzzle", "Hard")
        assert rider.LOOK_BEATS == 2.5

    def test_the_scope_keeps_its_beam_and_glow(self, qapp):
        import visualizers
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Oscilloscope")
            pane.mode_box.setCurrentText("X-Y")
            pane.decay.setValue(90)
            pane.scene_box.setCurrentText("Equaliser")
            pane.scene_box.setCurrentText("Oscilloscope")
            pane.spectrum.clear()
            scope = visualizers.by_name("Oscilloscope")
            assert scope.mode == "X-Y"
            assert scope.decay == pytest.approx(0.90)
        finally:
            self._back_to_how_it_was()
            pane.mode_box.setCurrentText("Sweep")
            pane.deleteLater()


class TestEachGameSaysWhatItIs:
    def test_a_line_under_the_controls_for_the_game_chosen(self, qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.resize(900, 700)
        pane.show()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Music rider")
            qapp.processEvents()
            assert pane.game_about.isVisible()
            for game in ("Mono", "Ninja", "Wakeboard", "Puzzle"):
                pane.game_box.setCurrentText(game)
                assert pane.game_about.text() == AudioPane.GAME_ABOUT[game]
            assert len(set(AudioPane.GAME_ABOUT.values())) == 4
            pane.scene_box.setCurrentText("Rave")
            qapp.processEvents()
            assert not pane.game_about.isVisible()
        finally:
            pane.game_box.setCurrentText("Mono")
            pane.close()
            pane.deleteLater()


class TestAPauseIsAPause:
    """The pane's clock settles onto a paused position over half a second.
    Read as playing, that ran the road on a quarter of a second past the
    music, which then had to be met again when it started."""

    def test_the_rider_stops_where_the_music_did(self, qapp):
        import time
        from array import array

        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QImage, QPainter

        import beatmap
        import visualizers
        from attachment_widgets import Spectrum

        clock = [5000.0]
        was = time.monotonic
        time.monotonic = lambda: clock[0]
        pane = Spectrum()
        image = QImage(320, 180, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        try:
            pane.set_frames([array("f", [0.4] * 27)] * 3000, 15)
            kick = beatmap.BeatMap(
                beats=tuple(beatmap.Beat(at=i * 0.5 + 0.25, strength=1.0)
                            for i in range(400)), bpm=120.0, locked=True)
            pane.set_beats({"Kick": kick})
            pane.set_elements({"Kick": kick})
            rider = visualizers.Rider()
            pane.set_scene(rider)
            pane.set_playing(True)
            at = 20.0

            reported = [at]

            def frame(position, count):
                clock[0] += 1.0 / 60.0
                # As a media player does: a new position every 50 ms, and
                # the pane's own clock running on between them.
                if count % 3 == 0:
                    reported[0] = position
                pane.set_position(int(reported[0] * 1000))
                pane._tick()
                rider.paint(painter, QRectF(0, 0, 320, 180), pane._state)

            for count in range(121):
                at += 1.0 / 60.0
                frame(at, count)
            pane.set_playing(False)
            at = reported[0]
            road = rider._at
            for count in range(60):
                frame(at, 0)
            assert abs(rider._heard - at) < 0.03, (
                f"paused at {at:.3f}s, the rider ran on to "
                f"{rider._heard:.3f}s")
            assert rider._at - road < 2.0, (
                f"the road went on {rider._at - road:.1f} units")
        finally:
            painter.end()
            time.monotonic = was
            pane.deleteLater()
