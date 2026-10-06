"""Music rider's sounds, in the record's key. See rider_sound.

A note for every block taken, climbing as the run goes on; a thump with the
music ducking under it for a hit. The notes are the record's own, so they
never fight it: the chord under the moment, in the record's tuning, or no
notes at all where there is no key.
"""

from __future__ import annotations

import math
import wave

import pytest

import rider_sound

#: A record in C major at A = 440, with C major for four seconds and then
#: A minor, as the harmony pass would describe it.
C_MAJOR = {"key": {"tonic": 0, "mode": "major", "confidence": 0.3,
                   "name": "C"},
           "tuning": 0.0, "tonal": 0.9,
           "chords": [[0.0, 4.0, 0, "maj"], [4.0, 8.0, 9, "min"],
                      [8.0, 12.0, -1, "none"]],
           "lead": [], "rate": 4.0}


def _midi(name):
    """The MIDI note a note's name is, or None for a fixed sound."""
    voice = rider_sound.voice_of(name)
    if voice is None:
        return None
    return int(name[len(voice):len(voice) + 3].rstrip("+-"))


def _pitch_classes(names):
    """The pitch class of each note played, from its name."""
    return [_midi(name) % 12 for name in names if _midi(name) is not None]


def _power(samples, freq, start=0.02, end=0.12):
    """How much of ``freq`` there is in a sound - both sides together -
    between ``start`` and ``end`` seconds."""
    samples = rider_sound.mono(samples)
    window = samples[int(start * rider_sound.RATE):int(end * rider_sound.RATE)]
    re = im = 0.0
    for index, value in enumerate(window):
        angle = math.tau * freq * index / rider_sound.RATE
        re += value * math.cos(angle)
        im += value * math.sin(angle)
    return re * re + im * im


class TestTheSoundsThemselves:
    @pytest.mark.parametrize("name", sorted(rider_sound.FIXED) + [
        rider_sound.note_name("pluck", 72, 0),
        rider_sound.note_name("pluck", 96, 35),
        rider_sound.note_name("soft", 72, -20),
        rider_sound.note_name("soft", 91, 0)])
    def test_every_one_is_short_clean_and_below_full_scale(self, name):
        """No clip, no click at the end, no offset, and over within a
        second - a pickup sound that rang on would be over the next one."""
        samples = rider_sound.make(name)
        peak = max(abs(v) for v in samples) / 32767.0
        assert 0.3 < peak <= 0.9, f"{name} peaks at {peak:.2f}"
        assert max(abs(v) for v in samples[-10:]) / 32767.0 < 0.01, (
            f"{name} ends on a click")
        assert abs(sum(samples) / len(samples)) / 32767.0 < 0.01
        longest = 2.0 if name == "swell" else 1.0
        assert len(rider_sound.mono(samples)) / rider_sound.RATE <= longest

    @pytest.mark.parametrize("voice", ["pluck", "soft"])
    @pytest.mark.parametrize("midi, cents", [(69, 0), (76, 30), (81, -25)])
    def test_a_note_is_its_note_in_its_tuning(self, voice, midi, cents):
        """The strongest thing in it is the note its name says, tuned as
        its name says - measured off the samples: a tuning a record is in
        is a tuning worth being in too."""
        samples = rider_sound.make(rider_sound.note_name(voice, midi, cents))
        want = rider_sound.hertz(midi + cents / 100.0)
        at = _power(samples, want)
        for off in (want * 2 ** (-1 / 12), want * 2 ** (1 / 12)):
            assert at > _power(samples, off) * 3, (
                f"a semitone off {want:.0f} Hz is as strong as the note")
        # And tuned: the note as named beats the same note at A = 440.
        if abs(cents) >= 25:
            assert at > _power(samples, rider_sound.hertz(midi)) * 1.3

    @staticmethod
    def _line(samples, start=0.0, end=0.08):
        """How far the strongest semitone from A4 up two octaves stands over
        its neighbours: a note is a line in the spectrum, thousands of times
        over them, while noise, a sweep or metal is a spread, a few times at
        most. Against its neighbours rather than the range's average, which
        a brightly filtered sound fails for its slope."""
        import statistics

        power = {k: _power(samples, 110.0 * 2 ** (k / 12), start, end)
                 for k in range(21, 51)}
        return max(power[k] / (statistics.median(
            [power[j] for j in range(k - 3, k + 4) if j != k]) or 1e-12)
            for k in range(24, 48))

    @pytest.mark.parametrize("name", [f"tick{i}" for i in range(8)]
                             + [f"shake{i}" for i in range(8)]
                             + ["hit", "glass", "landing", "sweep"])
    def test_nothing_unpitched_has_a_note_in_it(self, name):
        """A tick, a shaker and a hit are the sounds a record with no key
        gets, and the hit, the glass and the rest are played over every
        record: none may have a steady pitch for a melody to clash with."""
        assert self._line(rider_sound.make(name)) < 40.0, name

    @staticmethod
    def _high_share(samples, note):
        """How much of a sound is more than three times its note's
        frequency: a bright, plucky sound is mostly up there, a soft one
        mostly at its note."""
        total = sum(_power(samples, note * k / 4.0) for k in range(2, 40))
        high = sum(_power(samples, note * k / 4.0) for k in range(13, 40))
        return high / total if total else 0.0

    @staticmethod
    def _attack(samples):
        """Seconds from a tenth of the loudest to nine tenths of it."""
        level = [abs(v) for v in rider_sound.mono(samples)]
        top = max(level)
        window = 64
        smooth = [max(level[i:i + window]) for i in range(0, len(level), window)]
        first = next(i for i, v in enumerate(smooth) if v >= top * 0.1)
        nine = next(i for i, v in enumerate(smooth) if v >= top * 0.9)
        return (nine - first) * window / rider_sound.RATE

    @pytest.mark.parametrize("midi", [72, 84, 91])
    def test_a_coin_is_a_soft_hit_not_a_pluck(self, midi):
        """Most of a coin is at its note, and it comes up with no edge."""
        samples = rider_sound.make(rider_sound.note_name("soft", midi, 0))
        note = rider_sound.hertz(midi)
        assert self._high_share(samples, note) < 0.12
        assert self._attack(samples) >= 0.005

    def test_which_is_a_measure_that_hears_a_note(self):
        for name in (rider_sound.note_name("pluck", 72, 0),
                     rider_sound.note_name("soft", 76, 0)):
            assert self._line(rider_sound.make(name), 0.02, 0.12) > 400.0

    def test_a_run_climbs_and_then_keeps_climbing_round_the_top(self):
        """Up a note a pickup, and then round the top half rather than
        back to the bottom, which would sound like the run had broken."""
        steps = [rider_sound.climb(i, 7) for i in range(30)]
        assert steps[:7] == list(range(7))
        assert min(steps[7:]) >= 4, "a long run fell back to the bottom"

    def test_a_ladder_is_the_classes_it_is_asked_for(self):
        notes = rider_sound.ladder((0, 4, 7), 72, 96)
        assert notes[0] == 72 and {n % 12 for n in notes} == {0, 4, 7}
        assert notes == sorted(notes) and notes[-1] <= 96


class TestTheyAreKeptInTheCache:
    def test_written_once_and_named_for_the_version(self, tmp_path):
        path = rider_sound.write("tick0", tmp_path)
        assert f"-{rider_sound.VERSION}-" in path.name
        with wave.open(str(path)) as handle:
            assert handle.getframerate() == rider_sound.RATE
            assert handle.getnchannels() == rider_sound.CHANNELS == 2
        stamp = path.stat().st_mtime_ns
        assert rider_sound.write("tick0", tmp_path) == path
        assert path.stat().st_mtime_ns == stamp, "it was made again"

    def test_they_are_made_in_another_process_when_missing(self, tmp_path,
                                                           monkeypatch):
        import multiprocessing

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
        notes = rider_sound.notes_for(15)
        rider_sound.make_elsewhere(tmp_path, notes)
        assert started and started[0][0] is rider_sound.make_all
        assert list(started[0][1][1]) == notes
        # And not when they are all there already.
        started.clear()
        rider_sound.make_all(tmp_path, notes[:3])
        rider_sound.make_elsewhere(tmp_path, notes[:3])
        assert started == []

    def test_old_builds_and_old_tunings_are_cleared_out(self, tmp_path):
        (tmp_path / "rider-3-prize0.wav").write_bytes(b"x" * 100)
        for cents in (0, 10, 20, 30, 40, 50):
            rider_sound.write(rider_sound.note_name("pluck", 72, cents),
                              tmp_path)
        rider_sound.write("tick0", tmp_path)
        rider_sound.prune(tmp_path, keep_cents=(0,))
        left = {path.name for path in tmp_path.iterdir()}
        assert "rider-3-prize0.wav" not in left, "an older build's sound stayed"
        tunings = [name for name in left if "pluck72" in name]
        assert len(tunings) <= rider_sound.TUNINGS_KEPT + 1
        assert any("pluck72+00" in name for name in tunings), (
            "the tuning in use was cleared")
        assert f"rider-{rider_sound.VERSION}-tick0.wav" in left


class TestWhatIsPlayed:
    """The sounds answer the scene's own record of what happened - the
    same pops the picture answers - so a sound and a flash can never
    disagree about whether something was taken."""

    @staticmethod
    def _board(tmp_path, harmony=None, ducked=None, delays=None):
        board = rider_sound.SoundBoard(
            tmp_path, volume=lambda: 0.7,
            duck=(lambda depth, seconds: ducked.append((depth, seconds)))
            if ducked is not None else None,
            later=(lambda seconds, action: (delays.append(seconds),
                                            action()))
            if delays is not None else None)
        board._harmony = harmony
        board._cents = rider_sound.cents_of(harmony)
        played = []
        board.play = lambda name, loud=1.0: played.append(name) or True
        return board, played

    @staticmethod
    def _scene(**state):
        import visualizers

        scene = visualizers.Rider()
        for key, value in state.items():
            setattr(scene, key, value)
        return scene

    def _pickups(self, board, scene, count, kind="prize", at=1.0):
        for chain in range(1, count + 1):
            scene._chain = chain
            scene._coin_run = chain
            scene._heard = at
            scene._pops = [[kind, 0.0, 1.0, None, 0.9, ""]]
            board.listen(scene)

    def test_a_run_is_the_chord_going_up(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR)
        self._pickups(board, self._scene(), 5)
        assert _pitch_classes(played) == [0, 4, 7, 0, 4], played
        midi = [_midi(name) for name in played]
        assert midi == sorted(midi), "the run does not climb"

    def test_and_follows_the_chord_when_it_changes(self, tmp_path):
        """Over A minor the same run is A, C and E: never a C major third
        against an A minor chord."""
        board, played = self._board(tmp_path, C_MAJOR)
        self._pickups(board, self._scene(), 4, at=5.0)
        assert set(_pitch_classes(played)) <= {9, 0, 4}, played

    def test_where_no_chord_is_heard_it_is_the_key(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR)
        self._pickups(board, self._scene(), 5, at=9.0)
        assert set(_pitch_classes(played)) <= {0, 2, 4, 7, 9}, played

    def test_a_minor_key_climbs_its_own_pentatonic(self, tmp_path):
        minor = dict(C_MAJOR, chords=[],
                     key={"tonic": 9, "mode": "minor", "confidence": 0.3})
        board, played = self._board(tmp_path, minor)
        self._pickups(board, self._scene(), 6)
        assert set(_pitch_classes(played)) <= {9, 0, 2, 4, 7}, played

    def test_in_the_record_s_tuning(self, tmp_path):
        """A record a quarter-tone flat gets notes a quarter-tone flat."""
        flat = dict(C_MAJOR, tuning=-0.23)
        board, played = self._board(tmp_path, flat)
        self._pickups(board, self._scene(), 2)
        assert all(name.endswith("-25") for name in played), played

    @pytest.mark.parametrize("harmony", [
        None,
        dict(C_MAJOR, key={"tonic": 0, "mode": "major", "confidence": 0.01}),
        dict(C_MAJOR, key={"tonic": 0, "mode": "major", "confidence": 0.04})])
    def test_no_key_to_be_in_is_no_notes(self, tmp_path, harmony):
        """Unheard, or a key the analysis is not sure of: a tick for a
        pickup and a shaker for a coin, which have no note to clash
        with."""
        board, played = self._board(tmp_path, harmony)
        scene = self._scene()
        self._pickups(board, scene, 3)
        self._pickups(board, scene, 2, kind="coin")
        assert played == ["tick0", "tick1", "tick2", "shake0", "shake1"]

    def test_a_key_that_is_sure_enough_is_played_in(self, tmp_path):
        """Just over the line, on a record the in-tune measure calls all
        drums: that measure scored nothing on real mixes whose key came out
        right, and between 0.03 and 0.06 of confidence no key read against
        a DJ program's was worse than a fifth out."""
        barely = dict(C_MAJOR, tonal=0.02,
                      key={"tonic": 0, "mode": "major", "confidence": 0.06})
        board, played = self._board(tmp_path, barely)
        self._pickups(board, self._scene(), 3)
        assert len(_pitch_classes(played)) == len(played) == 3, played
        assert set(_pitch_classes(played)) <= {0, 4, 7}, played

    def test_coins_climb_the_chord_softly(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR)
        self._pickups(board, self._scene(), 3, kind="coin")
        assert all(name.startswith("soft") for name in played)
        assert _pitch_classes(played) == [0, 4, 7]

    def test_a_hit_thumps_and_the_music_ducks(self, tmp_path):
        ducked = []
        board, played = self._board(tmp_path, C_MAJOR, ducked)
        scene = self._scene()
        scene._pops = [["hit", 0.0, 1.0, 0.0, 0.95, ""]]
        board.listen(scene)
        assert played == ["hit"]
        assert ducked and 0.2 < ducked[0][0] < 0.8, ducked

    def test_a_milestone_is_the_chord_arpeggiated_on_time(self, tmp_path):
        delays = []
        board, played = self._board(tmp_path, C_MAJOR, delays=delays)
        scene = self._scene(_heard=1.0)
        scene._pops = [["milestone", 0.0, 1.0, None, 0.9, ""]]
        board.listen(scene)
        assert set(_pitch_classes(played)) <= {0, 4, 7} and len(played) >= 4
        spaced = sorted(delays)
        assert all(b - a == pytest.approx(board.SPREAD, abs=1e-6)
                   for a, b in zip(spaced, spaced[1:])), delays

    def test_the_end_goes_home(self, tmp_path):
        """The finish is the key's own chord, whatever chord the track
        ended on."""
        board, played = self._board(tmp_path, C_MAJOR, delays=[])
        scene = self._scene(_heard=5.0)     # over A minor
        scene._pops = [["finish", 0.0, 1.0, None, 0.9, ""]]
        board.listen(scene)
        assert set(_pitch_classes(played)) == {0, 4, 7}

    def test_the_shield_rings_in_the_chord(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR, delays=[])
        scene = self._scene(_heard=5.0)
        scene._pops = [["shatter", 0.0, 1.0, 0.55, 0.3, "SHIELD"]]
        board.listen(scene)
        assert played[0] == "glass"
        assert set(_pitch_classes(played[1:])) <= {9, 0, 4} and played[1:]

    def test_what_happened_is_answered_once(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR)
        scene = self._scene()
        pop = ["hit", 0.0, 1.0, 0.55, 0.3, ""]
        scene._pops = [pop]
        board.listen(scene)
        board.listen(scene)
        assert played == ["hit"]

    def test_the_puzzle_climbs_as_the_grid_fills(self, tmp_path):
        board, played = self._board(tmp_path, None)
        scene = self._scene()
        scene._mode = "Puzzle"
        scene._cells = [[0], [1, 1], []]
        scene._pops = [["prize", 0.0, 1.0, None, 0.9, ""]]
        board.listen(scene)
        assert played == ["tick3"]

    def test_a_note_taken_echoes_on_the_record_s_grid(self, tmp_path):
        """A dotted eighth and a dotted quarter later, quieter each time,
        timed from the drums' beat: the delay a trance lead is run
        through, so a run rings on in time with the music."""
        delays, levels = [], []
        board = rider_sound.SoundBoard(
            tmp_path, later=lambda seconds, action: (delays.append(seconds),
                                                     action()))
        board._harmony, board._cents = C_MAJOR, 0
        board.play = lambda name, loud=1.0: levels.append((name, loud)) or True
        scene = self._scene(_beat=0.5, _heard=1.0, _chain=1)
        scene._pops = [["prize", 0.0, 1.0, None, 0.9, ""]]
        board.listen(scene)
        assert delays == pytest.approx([0.375, 0.75]), delays
        names = {name for name, _loud in levels}
        assert len(names) == 1 and rider_sound.voice_of(names.pop()) == "pluck"
        louds = [loud for _name, loud in levels]
        assert louds[0] > louds[1] > louds[2] > 0.0, louds

    @pytest.mark.parametrize("beat, kind", [(0.0, "prize"), (2.0, "prize"),
                                            (0.5, "hit"), (0.5, "milestone"),
                                            (0.5, "finish")])
    def test_and_only_a_note_taken_in_a_tempo(self, tmp_path, beat, kind):
        """No echo without a tempo to be in time with; none on a hit - a
        thump twice is two hits - and none on an arpeggio or the finish,
        whose notes are already a run of their own."""
        def delays_at(tempo):
            delays = []
            board = rider_sound.SoundBoard(
                tmp_path,
                later=lambda seconds, action: delays.append(seconds))
            board._harmony, board._cents = C_MAJOR, 0
            board.play = lambda name, loud=1.0: True
            scene = self._scene(_beat=tempo, _heard=1.0, _chain=1)
            scene._pops = [[kind, 0.0, 1.0, None, 0.9, ""]]
            board.listen(scene)
            return delays

        # Nothing more than the same moment with no tempo at all.
        assert delays_at(beat) == delays_at(0.0)

    def test_the_preview_is_a_pickup_in_the_key(self, tmp_path):
        board, played = self._board(tmp_path, C_MAJOR)
        assert board.preview(self._scene(_heard=1.0)) == played[-1]
        assert _pitch_classes(played) and set(_pitch_classes(played)) <= {
            0, 4, 7}
        keyless, played = self._board(tmp_path, None)
        assert keyless.preview() == "tick3" == played[-1]

    def test_the_level_is_a_share_of_the_music_s(self, tmp_path):
        """Turning the music down turns them down with it: the balance
        somebody set stays set."""
        set_to = []

        class Effect:
            def setVolume(self, value):      # noqa: N802 - Qt's name
                set_to.append(value)

            def play(self):
                pass

        music = [0.8]
        board = rider_sound.SoundBoard(tmp_path, volume=lambda: music[0],
                                       level=lambda: 0.25)
        board._voice = lambda name: Effect()
        board.play("hit")
        music[0] = 0.4
        board.play("hit", 0.5)
        assert set_to == pytest.approx([0.2, 0.05])

    def test_switched_off_it_is_silent(self, tmp_path):
        board = rider_sound.SoundBoard(tmp_path)
        board.enabled = False
        made = []
        board._voice = lambda name: made.append(name)
        board.play("hit")
        assert made == []

    def test_a_machine_that_cannot_play_them_goes_on_without(self, tmp_path):
        board = rider_sound.SoundBoard(tmp_path)

        def broken(name):
            raise RuntimeError("no audio device")

        board._voice = broken
        board.play("hit")
        board.play("hit")
        assert board._broken is True

    def test_a_note_not_made_yet_is_not_made_here(self, tmp_path):
        """The thread that plays them is the thread drawing the picture, so a
        note that is not ready is skipped rather than made on the spot: that
        was a forty millisecond frame the first time any pickup was taken."""
        board = rider_sound.SoundBoard(tmp_path)
        name = rider_sound.note_name("pluck", 72, 0)
        assert board.play(name) is False
        assert not rider_sound.path_for(name, tmp_path).exists()

    def test_the_notes_are_loaded_a_few_at_a_time_once_made(self, tmp_path):
        board = rider_sound.SoundBoard(tmp_path)
        board._harmony, board._cents = C_MAJOR, 0
        loaded = []
        board._voice = lambda name: (loaded.append(name),
                                     board._voices.setdefault(name, [1]))
        notes = rider_sound.notes_for(0)
        rider_sound.make_all(tmp_path, notes[:5])
        assert board.tend() == board.LOAD_EACH
        assert board.tend() == 2
        assert board.tend() == 0, "it loaded notes that were not made"
        assert loaded == notes[:5]


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
            assert pane.effects_box.isVisible()
            assert pane.spectrum._listener is not None
            pane.scene_box.setCurrentText("Rave")
            qapp.processEvents()
            assert not pane.sound_box.isVisible()
            assert not pane.effects_box.isVisible()
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
            # Off means nothing is played, whatever the game does.
            played = []

            class Board:
                def listen(self, scene):
                    played.append(scene)

                def prepare(self):
                    pass

                def set_harmony(self, harmony):
                    pass

            pane._board = Board()
            pane._listen_to_game(pane.spectrum._scene)
            assert played == [], "the sounds were asked for while off"
            assert pane.vj("sounds") is True
            pane._listen_to_game(pane.spectrum._scene)
            assert played, "and not asked for once back on"
            assert pane.sound_box.isChecked()
            pane.scene_box.setCurrentText("Rave")
            assert pane.vj("sounds") is False, (
                "X did something in a scene with no sounds")
        finally:
            pane.deleteLater()

    def test_the_board_hears_the_key_and_forgets_it_for_the_next_track(
            self, qapp, tmp_path, monkeypatch):
        """Made before or after the harmony lands, the board plays in it;
        a new track puts it back to no key until that one is heard."""
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr(rider_sound, "make_elsewhere",
                            lambda folder, names=None: None)
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.spectrum.set_harmony(C_MAJOR)
            board = pane._sound_board()
            assert board.in_key, "a board made after the key did not get it"
            pane.stop()
            assert not board.in_key, "the last track's key outlived it"
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


class TestTheEffectsSlider:
    """The slider for the game's sound effects."""

    @staticmethod
    def _pane():
        from attachment_view import AudioPane

        return AudioPane()

    def test_it_sets_the_board_s_level(self, qapp, monkeypatch):
        monkeypatch.setattr(rider_sound, "make_elsewhere",
                            lambda folder, names=None: None)
        pane = self._pane()
        try:
            board = pane._sound_board()
            pane.effects.setValue(30)
            assert board._level() == pytest.approx(0.30)
            pane.effects.setValue(0)
            assert board._level() == 0.0
        finally:
            pane.deleteLater()

    def test_it_starts_at_half_and_is_remembered(self, qapp):
        from attachment_view import AudioPane

        pane = self._pane()
        try:
            assert pane.effects.value() == AudioPane.EFFECTS == 50
            pane.effects.setValue(22)
            # Kept a moment after the last move, by itself.
            import time

            from attachment_view import _viewer_prefs

            end = time.monotonic() + 3.0
            while (_viewer_prefs().get(AudioPane.EFFECTS_PREF) != 22
                   and time.monotonic() < end):
                qapp.processEvents()
                time.sleep(0.02)
            assert _viewer_prefs().get(AudioPane.EFFECTS_PREF) == 22
        finally:
            pane.deleteLater()
        again = self._pane()
        try:
            assert again.effects.value() == 22
        finally:
            again.deleteLater()

    @pytest.mark.parametrize("kept", ['{"effects_level": 500}', "not json",
                                      '["effects_level"]',
                                      '{"effects_level": "loud"}',
                                      '{"effects": 57}'])
    def test_a_file_that_makes_no_sense_is_the_default(self, qapp, kept):
        import config
        from attachment_view import VIEWER_PREFS, AudioPane

        path = config.app_support_dir() / VIEWER_PREFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(kept)
        pane = self._pane()
        try:
            assert pane.effects.value() == AudioPane.EFFECTS
        finally:
            pane.deleteLater()

    def test_a_double_click_puts_it_back_to_half(self, qapp):
        from PySide6.QtCore import QPoint, Qt
        from PySide6.QtTest import QTest

        from attachment_view import AudioPane

        pane = self._pane()
        pane.resize(900, 700)
        pane.show()
        try:
            pane.enable_box.setChecked(True)
            pane.scene_box.setCurrentText("Music rider")
            qapp.processEvents()
            assert pane.effects.isVisible()
            pane.effects.setValue(90)
            QTest.mouseDClick(pane.effects, Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier,
                              QPoint(5, pane.effects.height() // 2))
            assert pane.effects.value() == AudioPane.EFFECTS
        finally:
            pane.close()
            pane.deleteLater()

    def test_letting_go_plays_one_to_hear_it_by(self, qapp):
        pane = self._pane()
        heard = []

        class Board:
            def preview(self, scene=None):
                heard.append(scene)

        try:
            pane._board = Board()
            pane.effects.sliderReleased.emit()
            assert len(heard) == 1
            pane.sound_box.setChecked(False)
            pane.effects.sliderReleased.emit()
            assert len(heard) == 1, "a preview with the sounds switched off"
        finally:
            pane._board = None
            pane.deleteLater()

    def test_full_screen_has_it_for_the_game(self, qapp):
        pane = self._pane()
        pane.scene_box.setCurrentText("Music rider")
        pane._go_full_screen()
        window = pane._full
        try:
            label, slider = pane._full_effects
            assert slider.isVisibleTo(window) and label.isVisibleTo(window)
            # Called what it is called in the window, not "fx".
            from PySide6.QtWidgets import QLabel
            assert label.text() == pane.sound_box.text()
            captions = {found.text() for found in window.findChildren(QLabel)}
            assert "Volume" in captions and "Vol" not in captions
            slider.setValue(71)
            assert pane.effects.value() == 71
            pane.effects.setValue(12)
            assert slider.value() == 12
            # Off is off on both, and on again on both.
            box = pane.effects_box
            pane.sound_box.setChecked(False)
            assert not slider.isEnabled()
            assert not pane.effects.isEnabledTo(box)
            pane.sound_box.setChecked(True)
            assert slider.isEnabled() and pane.effects.isEnabledTo(box)
            pane.scene_box.setCurrentText("Rave")
            assert not slider.isVisibleTo(window)
        finally:
            window.close()
            pane.deleteLater()


class TestTheyAreReallyPlayedLater:
    """The notes of an arpeggio and a note's echoes are put off with the pane's
    own timer, and every other test hands the board a stand-in for it. The
    real one raised the first time a note echoed, and the game went silent
    for the rest of the session; these run the real one."""

    @staticmethod
    def _wait(seconds):
        from PySide6.QtCore import QEventLoop, QTimer

        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()

    def test_the_pane_s_timer_does_it_on_time(self, qapp):
        import time

        from attachment_view import AudioPane

        pane = AudioPane()
        fired = []
        try:
            asked = time.monotonic()
            pane._later(0.08, lambda: fired.append(time.monotonic() - asked))
            assert fired == [], "it did not wait"
            # A loaded machine fires late. What must hold is never early,
            # once, and not by a lot.
            deadline = time.monotonic() + 1.5
            while not fired and time.monotonic() < deadline:
                self._wait(0.05)
            assert len(fired) == 1, fired
            assert 0.07 <= fired[0] < 1.0, fired
        finally:
            pane.deleteLater()

    def test_a_note_taken_echoes_through_the_real_pane(self, qapp, tmp_path,
                                                      monkeypatch):
        """From the frame the game hands over to the echoes being played:
        the listener stays, and the note sounds three times."""
        monkeypatch.setattr(rider_sound, "make_elsewhere",
                            lambda folder, names=None: None)
        import visualizers
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            pane.spectrum.set_harmony(C_MAJOR)
            board = pane._sound_board()
            played = []
            board.play = lambda name, loud=1.0: played.append(name) or True
            scene = visualizers.Rider()
            scene._beat, scene._heard, scene._chain = 0.4, 1.0, 1
            scene._pops = [["prize", 0.0, 1.0, None, 0.9, ""]]
            pane.spectrum.set_listener(pane._listen_to_game)
            pane.spectrum._scene = scene
            pane.spectrum._tell_listener()
            assert pane.spectrum._listener is not None, (
                "the pane let go of the game's sounds")
            assert len(played) == 1, (
                f"the echoes did not wait for their beat: {played}")
            self._wait(0.8)
            assert len(played) == 3 and len(set(played)) == 1, played
        finally:
            pane.deleteLater()

    def test_a_timer_that_fails_plays_it_at_once(self, tmp_path):
        """Late rather than never - and the rest of the game's sounds
        kept."""
        def broken(seconds, action):
            raise TypeError("no timer here")

        board = rider_sound.SoundBoard(tmp_path, later=broken)
        played = []
        board.play = lambda name, loud=1.0: played.append(name) or True
        board._play_at(0.3, "hit", 1.0)
        board._play_at(0.3, "glass", 1.0)
        assert played == ["hit", "glass"]


class TestTheNotesAreInTheKey:
    """A pickup's note is from the chord only when the chord is the key's
    own, and from the key where the song is - which can move."""

    KEY = {"tonic": 0, "mode": "major", "confidence": 0.5}

    def test_a_chord_out_of_the_key_is_not_followed(self):
        import rider_sound

        found = {"key": self.KEY, "chords": [[0.0, 10.0, 3, "maj"]]}
        assert sorted(rider_sound.classes_at(found, 5.0)) == [0, 2, 4, 7, 9]

    def test_a_chord_of_the_key_is(self):
        import rider_sound

        found = {"key": self.KEY, "chords": [[0.0, 10.0, 5, "maj"]]}
        assert sorted(rider_sound.classes_at(found, 5.0)) == [0, 5, 9]

    def test_a_song_that_changes_key_takes_its_notes_with_it(self):
        import rider_sound

        found = {"key": self.KEY, "chords": [],
                 "keys": [[0.0, 20.0, 0, "major"], [20.0, 40.0, 2, "major"]]}
        assert sorted(rider_sound.classes_at(found, 5.0)) == [0, 2, 4, 7, 9]
        assert sorted(rider_sound.classes_at(found, 30.0)) == [2, 4, 6, 9, 11]
        # A chord of the old key, misheard in the new one, is not used.
        found["chords"] = [[25.0, 35.0, 5, "maj"]]
        assert 5 not in rider_sound.classes_at(found, 30.0)
