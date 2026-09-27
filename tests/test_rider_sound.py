"""Music rider's sounds. See rider_sound.

"Xxx xxxxx xxxx xx xxxxx xxxxx xx xxxxxxx: xxxxxxx xxx xxxxx, xxxxxxx xxx
xxxxxxxx, xxxxxxx xxxx xxxxxx xx xxxxx xx xxxxxxx xxx xxxxxxxxx." A note
for every block taken, climbing as the run goes on; a thump with the music
ducking under it for a hit. Made, not recorded, and kept in the cache.
"""

from __future__ import annotations

import math
import wave
from array import array

import pytest


class TestTheSoundsThemselves:
    @pytest.mark.parametrize("name", sorted(
        __import__("rider_sound").SOUNDS))
    def test_every_one_is_short_clean_and_below_full_scale(self, name):
        """No clip, no click at the end, no offset, and over within a
        second - a pickup sound that rang on would be over the next one."""
        import rider_sound

        samples = rider_sound.SOUNDS[name]()
        peak = max(abs(v) for v in samples) / 32767.0
        assert 0.3 < peak <= 0.9, f"{name} peaks at {peak:.2f}"
        assert max(abs(v) for v in samples[-10:]) / 32767.0 < 0.01, (
            f"{name} ends on a click")
        assert abs(sum(samples) / len(samples)) / 32767.0 < 0.01
        assert len(samples) / rider_sound.RATE <= 1.0

    def test_a_run_climbs_and_then_keeps_climbing_round_the_top(self):
        """Up the scale a note a pickup, and then round the top octave
        rather than back to the bottom, which would sound like the run
        had been broken."""
        from rider_sound import PENTATONIC, climb

        steps = [climb(i) for i in range(30)]
        top = len(PENTATONIC)
        assert steps[:top] == list(range(top))
        assert min(steps[top:]) >= 5, "a long run fell back to the bottom"

    def test_the_notes_are_different_notes(self):
        import rider_sound

        made = [bytes(rider_sound.prize(step)) for step in range(4)]
        assert len(set(made)) == 4

    def test_the_pickup_is_on_its_note(self):
        """The strongest thing in the first note is the note it says -
        measured off the samples, over the part where it rings."""
        import rider_sound

        samples = rider_sound.prize(0)
        rate = rider_sound.RATE
        want = rider_sound.PRIZE_ROOT
        window = samples[int(0.02 * rate):int(0.12 * rate)]

        def power(freq):
            re = im = 0.0
            for index, value in enumerate(window):
                angle = math.tau * freq * index / rate
                re += value * math.cos(angle)
                im += value * math.sin(angle)
            return re * re + im * im

        at = power(want)
        for off in (want * 2 ** (-2 / 12), want * 2 ** (2 / 12)):
            assert at > power(off) * 4, (
                f"a whole tone off {want:.0f} Hz is as loud as the note")


class TestTheyAreKeptInTheCache:
    def test_written_once_and_named_for_the_version(self, tmp_path):
        import rider_sound

        path = rider_sound.write("coin0", tmp_path)
        assert f"-{rider_sound.VERSION}-" in path.name
        with wave.open(str(path)) as handle:
            assert handle.getframerate() == rider_sound.RATE
            assert handle.getnchannels() == 1
        stamp = path.stat().st_mtime_ns
        assert rider_sound.write("coin0", tmp_path) == path
        assert path.stat().st_mtime_ns == stamp, "it was made again"

    def test_they_are_made_in_another_process_when_missing(self, tmp_path,
                                                           monkeypatch):
        import multiprocessing

        import rider_sound

        started = []

        class Context:
            def Process(self, target, args, name, daemon):      # noqa: N802
                started.append((target, args))

                class Started:
                    def start(self):
                        pass
                return Started()

        monkeypatch.setattr(multiprocessing, "get_context",
                            lambda how: Context())
        rider_sound.make_elsewhere(tmp_path)
        assert started and started[0][0] is rider_sound.make_all
        # And not when they are all there already.
        started.clear()
        rider_sound.make_all(tmp_path)
        rider_sound.make_elsewhere(tmp_path)
        assert started == []


class TestWhatIsPlayed:
    """The sounds answer the scene's own record of what happened - the
    same pops the picture answers - so a sound and a flash can never
    disagree about whether something was taken."""

    @staticmethod
    def _board(tmp_path, ducked=None):
        import rider_sound

        board = rider_sound.SoundBoard(
            tmp_path, volume=lambda: 0.7,
            duck=(lambda depth, seconds: ducked.append((depth, seconds)))
            if ducked is not None else None)
        played = []
        board.play = lambda name, loud=1.0: played.append(name)
        return board, played

    @staticmethod
    def _scene(**state):
        import visualizers

        scene = visualizers.Rider()
        for key, value in state.items():
            setattr(scene, key, value)
        return scene

    def test_each_pickup_is_a_note_higher(self, tmp_path):
        board, played = self._board(tmp_path)
        scene = self._scene()
        for chain in range(1, 6):
            scene._chain = chain
            scene._pops = [["prize", 0.0, 1.0, None, 0.9, ""]]
            board.listen(scene)
        assert played == [f"prize{i}" for i in range(5)]

    def test_a_row_of_coins_climbs(self, tmp_path):
        board, played = self._board(tmp_path)
        scene = self._scene()
        for run in (1, 2, 3):
            scene._coin_run = run
            scene._pops = [["coin", 0.0, 1.0, 0.13, 0.6, ""]]
            board.listen(scene)
        assert played == ["coin0", "coin1", "coin2"]

    def test_a_hit_thumps_and_the_music_ducks(self, tmp_path):
        ducked = []
        board, played = self._board(tmp_path, ducked)
        scene = self._scene()
        scene._pops = [["hit", 0.0, 1.0, 0.0, 0.95, ""]]
        board.listen(scene)
        assert played == ["hit"]
        assert ducked and 0.2 < ducked[0][0] < 0.8, ducked

    def test_what_happened_is_answered_once(self, tmp_path):
        board, played = self._board(tmp_path)
        scene = self._scene()
        pop = ["shatter", 0.0, 1.0, 0.55, 0.3, "SHIELD"]
        scene._pops = [pop]
        board.listen(scene)
        board.listen(scene)
        assert played == ["shatter"]

    def test_the_puzzle_climbs_as_the_grid_fills(self, tmp_path):
        board, played = self._board(tmp_path)
        scene = self._scene()
        scene._mode = "Puzzle"
        scene._cells = [[0], [1, 1], []]
        scene._pops = [["prize", 0.0, 1.0, None, 0.9, ""]]
        board.listen(scene)
        assert played == ["prize3"]

    def test_switched_off_it_is_silent(self, tmp_path):
        import rider_sound

        board = rider_sound.SoundBoard(tmp_path)
        board.enabled = False
        made = []
        board._voice = lambda name: made.append(name)
        board.play("hit")
        assert made == []

    def test_a_machine_that_cannot_play_them_goes_on_without(self, tmp_path):
        import rider_sound

        board = rider_sound.SoundBoard(tmp_path)

        def broken(name):
            raise RuntimeError("no audio device")

        board._voice = broken
        board.play("hit")
        board.play("hit")
        assert board._broken is True


class TestThePaneAsksAfterEveryFrame:
    def test_the_listener_hears_the_scene_each_frame(self, qapp):
        from array import array as _array

        from PySide6.QtGui import QImage, QPainter

        import visualizers
        from attachment_widgets import Spectrum

        pane = Spectrum()
        try:
            pane.set_frames([_array("f", [0.4] * 27)] * 300, 15)
            pane.set_labels([str(i) for i in range(27)])
            pane.set_scene(visualizers.by_name("Music rider"))
            pane.set_playing(True)
            pane._reveal_changed(1.0)
            pane.resize(400, 250)
            heard = []
            pane.set_listener(heard.append)
            image = QImage(400, 250, QImage.Format.Format_ARGB32_Premultiplied)
            for _ in range(3):
                pane._tick()
                painter = QPainter(image)
                pane._paint(painter)
                painter.end()
            assert len(heard) == 3 and heard[0] is pane._scene
        finally:
            pane.deleteLater()

    def test_a_listener_that_fails_is_let_go_and_the_picture_goes_on(
            self, qapp):
        from array import array as _array

        from PySide6.QtGui import QImage, QPainter

        import visualizers
        from attachment_widgets import Spectrum

        pane = Spectrum()
        try:
            pane.set_frames([_array("f", [0.4] * 27)] * 300, 15)
            pane.set_labels([str(i) for i in range(27)])
            pane.set_scene(visualizers.by_name("Music rider"))
            pane.set_playing(True)
            pane._reveal_changed(1.0)
            pane.resize(400, 250)
            calls = []

            def broken(scene):
                calls.append(1)
                raise RuntimeError("no")

            pane.set_listener(broken)
            image = QImage(400, 250, QImage.Format.Format_ARGB32_Premultiplied)
            for _ in range(3):
                painter = QPainter(image)
                pane._paint(painter)
                painter.end()
            assert calls == [1]
        finally:
            pane.deleteLater()


class TestTheSwitch:
    def test_it_is_there_only_for_the_game(self, qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.resize(900, 700)
        pane.show()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Music rider")
            qapp.processEvents()
            assert pane.sound_box.isVisible()
            assert pane.spectrum._listener is not None
            pane.scene_box.setCurrentText("Rave")
            qapp.processEvents()
            assert not pane.sound_box.isVisible()
            assert pane.spectrum._listener is None, (
                "the game's sounds are still listening to another scene")
        finally:
            pane.close()
            pane.deleteLater()

    def test_x_turns_them_off_and_on_in_the_game_only(self, qapp):
        from PySide6.QtCore import Qt

        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.scene_box.setCurrentText("Music rider")
            assert AudioPane.vj_action(Qt.Key.Key_X) == ("sounds", 0)
            assert pane.vj("sounds") is True
            assert not pane.sound_box.isChecked()
            assert pane.spectrum._listener is None
            assert pane.vj("sounds") is True
            assert pane.sound_box.isChecked()
            pane.scene_box.setCurrentText("Rave")
            assert pane.vj("sounds") is False, (
                "X did something in a scene with no sounds")
        finally:
            pane.deleteLater()

    def test_a_duck_brings_the_music_back(self, qapp):
        import time

        from attachment_view import AudioPane

        class Output:
            def __init__(self):
                self.levels = []

            def setVolume(self, value):      # noqa: N802 - Qt's name
                self.levels.append(value)

        pane = AudioPane()
        try:
            pane._audio = Output()
            pane.volume.setValue(80)
            pane._duck(0.5, 0.1)
            end = time.monotonic() + 0.6
            while time.monotonic() < end:
                qapp.processEvents()
                time.sleep(0.01)
            levels = pane._audio.levels
            assert min(levels) < 0.5, f"the music never went under: {levels}"
            assert abs(levels[-1] - 0.8) < 1e-6, (
                f"and did not come back to where it was: {levels[-3:]}")
        finally:
            pane._audio = None
            pane.deleteLater()
