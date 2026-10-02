"""The key, the tuning, the chords and the lead of a track. See harmony.

Written songs rather than recordings: songkit puts every chord and every
note where it says, in a key and a tuning it chooses, so the answer is
known exactly and nothing anybody owns is in the repository.
"""

from __future__ import annotations

import pytest

import harmony
import songkit

C_MAJOR = [(0, "maj"), (5, "maj"), (7, "maj"), (0, "maj")]


def _heard(chords, **options):
    pcm, rate, truth = songkit.song(chords, **options)
    return harmony.analyse(pcm, rate, 1), truth


class TestTheKey:
    def test_a_song_in_c_is_in_c(self):
        found, _truth = _heard(C_MAJOR)
        assert (found["key"]["tonic"], found["key"]["mode"]) == (0, "major")
        assert found["key"]["confidence"] >= harmony.SURE

    @pytest.mark.parametrize("tonic", [2, 7, 10])
    def test_and_moved_up_it_moves_with_it(self, tonic):
        moved = [((root + tonic) % 12, quality) for root, quality in C_MAJOR]
        found, _truth = _heard(moved)
        assert found["key"]["tonic"] == tonic, found["key"]["name"]

    def test_a_minor_song_is_minor(self):
        found, _truth = _heard([(9, "min"), (2, "min"), (4, "min"), (9, "min")])
        assert (found["key"]["tonic"], found["key"]["mode"]) == (9, "minor")


class TestTheKeyThroughTheTrack:
    """A song that changes key is followed into it; one that does not is
    left in its own."""

    PROGRESSION = [(0, "maj"), (5, "maj"), (7, "maj"), (9, "min")]

    @classmethod
    def _song(cls, *tonics):
        from array import array

        pcm = array("h")
        rate = 8000
        for tonic in tonics:
            part, rate, _truth = songkit.song(
                [((root + tonic) % 12, quality)
                 for root, quality in cls.PROGRESSION], repeats=6, rate=8000)
            pcm.extend(part)
        return harmony.analyse(pcm, rate, 1)

    def test_a_song_that_goes_up_a_tone_is_followed_there(self):
        found = self._song(0, 2)
        assert harmony.key_at(found, 10.0) == (0, "major")
        assert harmony.key_at(found, 85.0) == (2, "major")
        changes = [start for start, _end, _tonic, _mode in found["keys"][1:]]
        assert len(changes) == 1 and 36.0 < changes[0] < 60.0, found["keys"]

    def test_a_song_in_one_key_stays_in_it(self):
        found = self._song(0, 0)
        assert [k[2:] for k in found["keys"]] == [[0, "major"]]

    def test_no_key_to_follow_without_a_key(self):
        assert harmony.keys([], None, 0.25) == []
        assert harmony.key_at(None, 3.0) is None
        assert harmony.key_at({"key": {"tonic": 4, "mode": "minor"}},
                              3.0) == (4, "minor")


class TestTheTuning:
    @pytest.mark.parametrize("cents", [-40.0, -15.0, 0.0, 22.0, 45.0])
    def test_it_is_heard_to_a_few_cents(self, cents):
        """A record off A = 440 has every note off by the same amount,
        and read against 440 half its notes would land in the wrong class.
        """
        found, _truth = _heard(C_MAJOR, cents=cents)
        assert abs(found["tuning"] * 100.0 - cents) < 4.0, found["tuning"]
        assert found["key"]["tonic"] == 0

    def test_a_quarter_tone_off_still_finds_the_chords(self):
        found, truth = _heard(C_MAJOR, cents=45.0)
        roots = [root for _s, _e, root, _q in found["chords"] if root >= 0]
        assert roots[:4] == [0, 5, 7, 0], roots


class TestTheChords:
    def test_each_chord_is_found_where_it_is(self):
        """The right chord, changing within a reading of where it really
        changes - timed from the middle of each reading rather than its
        start, which put every change a quarter of a second early."""
        found, truth = _heard([(9, "min"), (5, "maj"), (0, "maj"), (7, "maj")])
        for start, end, root, quality in truth["chords"]:
            middle = (start + end) / 2.0
            assert harmony.chord_at(found, middle) == (root, quality), middle
        changes = [start for start, _e, _r, _q in found["chords"][1:]]
        for start, _e, _r, _q in truth["chords"][1:]:
            nearest = min(changes, key=lambda at: abs(at - start))
            assert abs(nearest - start) < 0.2, (start, nearest)

    def test_they_do_not_flicker(self):
        """A chord held for a bar is one chord, not a dozen."""
        found, truth = _heard(C_MAJOR)
        assert len(found["chords"]) <= len(truth["chords"]) + 2

    @pytest.mark.parametrize("note_beats", [0.5, 1.0])
    def test_an_arpeggio_is_its_chords(self, note_beats):
        """A chord played one note at a time, with no bass under it - a
        trance breakdown. A reading a quarter of a second long hears two of
        its three notes, which fit another chord as well as this one, and
        the chord changed nearly every reading: 50 chords heard for 8, right
        a third of the time. Heard over the readings around it as well, the
        whole chord is there."""
        progression = [(0, "maj"), (7, "maj"), (9, "min"), (5, "maj")]
        pcm, rate, truth = songkit.arpeggio(progression,
                                            note_beats=note_beats)
        found = harmony.analyse(pcm, rate, 1)
        right = steps = 0
        at = 0.05
        while at < truth["seconds"] - 0.05:
            want = next((r, q) for a, b, r, q in truth["chords"] if a <= at < b)
            right += harmony.chord_at(found, at) == want
            steps += 1
            at += 0.1
        assert right / steps >= 0.65, right / steps
        assert len(found["chords"]) <= 2 * len(truth["chords"]), (
            len(found["chords"]))

    def test_between_chords_there_is_no_chord(self):
        assert harmony.chord_at(None, 1.0) is None
        assert harmony.chord_at({"chords": [[0.0, 1.0, -1, "none"]]},
                                0.5) is None
        assert harmony.chord_at({"chords": [[0.0, 1.0, 4, "min"]]},
                                2.0) is None


class TestTheLead:
    def test_the_melody_is_the_top_voice(self):
        """The highest strong note, not the loudest: between two notes of
        a melody the loudest thing in its range is the pad under it."""
        lead = [78, 81, 85, 83, 81, 78, 76, 73]
        found, truth = _heard([(6, "min"), (2, "maj"), (9, "maj"), (4, "maj")],
                              lead=lead)
        right = total = 0
        for start, end, midi in truth["lead"]:
            middle = (start + end) / 2.0
            index = int(round((middle - found["lead_from"]) * found["rate"]))
            if 0 <= index < len(found["lead"]) and found["lead"][index]:
                total += 1
                right += abs(found["lead"][index] - midi) < 0.5
        assert total and right / total >= 0.85, (right, total)


class TestAWholeTrack:
    def test_drums_alone_have_hardly_a_pitch(self):
        """A record with nothing pitched in it: almost none of its peaks
        sit in tune. (Whether its sounds are notes is decided by how sure
        the key is - see rider_sound.pitched - and a drum track with notes
        in whatever key its drums lean to has no melody to clash with.)"""
        from drumkit import styled

        import rider_sound

        # Rock, because it is the one written with no bass line under it:
        # the others' subs are notes, and a key is right to be heard in them.
        pcm, _truth = styled("rock", seconds=12.0)
        found = harmony.analyse(pcm, 48000, 2)
        assert found is not None
        assert found["tonal"] < 0.1, found["tonal"]
        del rider_sound

    def test_it_is_quick(self):
        """Seven and a half minutes is about six seconds in a process of its
        own; a minute of it here, at the rate the app decodes at."""
        import time

        pcm, rate, _truth = songkit.song(C_MAJOR, repeats=8, rate=8000)
        started = time.perf_counter()
        harmony.analyse(pcm, rate, 1)
        assert time.perf_counter() - started < 3.0

    def test_nothing_to_hear_is_nothing(self):
        from array import array

        assert harmony.analyse(array("h"), 48000, 2) is None
        assert harmony.analyse(array("h", [0] * 1000), 48000, 2) is None

    def test_one_channel_or_two_the_same_answer(self):
        from array import array

        pcm, rate, _truth = songkit.song(C_MAJOR)
        stereo = array("h")
        for value in pcm:
            stereo.append(value)
            stereo.append(value)
        one = harmony.analyse(pcm, rate, 1)
        two = harmony.analyse(stereo, rate, 2)
        assert one["key"]["name"] == two["key"]["name"]
        assert abs(one["tuning"] - two["tuning"]) < 0.02


class TestInAWorker:
    def test_the_harmony_comes_back_from_a_process_of_its_own(self):
        """The same worker the rest of the analysis runs in, asked for the
        harmony: what the app does, not what the module does."""
        import multiprocessing
        import time
        from array import array

        import attachment_audio

        pcm, rate, _truth = songkit.song(C_MAJOR, rate=8000)
        # At the rate the app decodes at, which is what the worker is sent.
        stereo = array("h")
        for value in pcm:
            for _ in range(6):
                stereo.append(value)
                stereo.append(value)
        context = multiprocessing.get_context("spawn")
        ours, theirs = context.Pipe()
        process = context.Process(target=attachment_audio._worker,
                                  args=(theirs, 48000, 2, "harmony"),
                                  daemon=True)
        process.start()
        theirs.close()
        try:
            ours.send_bytes(stereo)
            deadline = time.monotonic() + 60.0
            while time.monotonic() < deadline:
                if ours.poll(0.2):
                    kind, payload = ours.recv()
                    assert kind == "harmony", (kind, payload)
                    assert payload["key"]["tonic"] == 0
                    return
            pytest.fail("the worker did not answer")
        finally:
            if process.is_alive():
                process.terminate()
            process.join(2.0)


class TestAKeyAndItsRelative:
    """A minor and C major are the same seven notes, and fit a track's
    chroma nearly equally; when they do, the chords decide."""

    @staticmethod
    def _key(fit=0.80, relative_fit=0.77):
        return {"tonic": 0, "mode": "major", "fit": fit,
                "relative": [9, "minor"], "relative_fit": relative_fit,
                "confidence": 0.2, "name": "C"}

    def test_the_chord_heard_longer_wins(self):
        chords = [[0, 4, 9, "min"], [4, 6, 5, "maj"], [6, 12, 9, "min"],
                  [12, 14, 0, "maj"]]
        key = harmony.settle_relative(self._key(), chords)
        assert (key["tonic"], key["mode"], key["name"]) == (9, "minor", "Am")
        assert key["relative"] == [0, "major"]

    def test_even_the_track_s_first_chord_decides(self):
        """Am F C G, round and round: as much C as Am, and it starts on
        Am - the loop dance music is made of, and it is in A minor."""
        loop = []
        for bar in range(4):
            for step, (root, quality) in enumerate(
                    ((9, "min"), (5, "maj"), (0, "maj"), (7, "maj"))):
                start = (bar * 4 + step) * 2.0
                loop.append([start, start + 2.0, root, quality])
        assert harmony.settle_relative(self._key(), loop)["mode"] == "minor"
        # And the same loop from the C is in C.
        turned = loop[2:] + loop[:2]
        assert harmony.settle_relative(self._key(), turned)["mode"] == "major"

    def test_a_clear_fit_is_not_second_guessed(self):
        chords = [[0, 12, 9, "min"]]
        key = harmony.settle_relative(self._key(fit=0.9, relative_fit=0.6),
                                      chords)
        assert key["mode"] == "major"


class TestInThePane:
    @staticmethod
    def _callbacks(monkeypatch):
        import attachment_audio

        told = {}

        def decode(path, done, failed, progress, kit, bands, harmonised,
                   beaten, rhythmic):
            told.update(done=done, bands=bands, harmonised=harmonised,
                        beaten=beaten, rhythmic=rhythmic)
            return object()

        monkeypatch.setattr(attachment_audio, "decode", decode)
        return told

    def test_the_harmony_reaches_the_picture_and_the_board(self, qapp,
                                                            monkeypatch):
        from pathlib import Path

        import rider_sound
        from attachment_view import AudioPane

        monkeypatch.setattr(rider_sound, "make_elsewhere",
                            lambda folder, names=None: None)
        told = self._callbacks(monkeypatch)
        pane = AudioPane()
        try:
            board = pane._sound_board()
            pane._start_analysis(Path("song"))
            found = {"key": {"tonic": 7, "mode": "major", "confidence": 0.3},
                     "tuning": 0.1, "tonal": 0.8, "chords": [], "lead": [],
                     "rate": 4.0}
            told["harmonised"](found)
            assert pane.spectrum.harmony() is found
            assert board.in_key and board._cents == 10
            # And into what the game is handed each frame.
            state = pane.spectrum._state
            pane.spectrum._clock(state)
            assert state.harmony is found
            pane.stop()
            pane.spectrum._clock(state)
            assert state.harmony is None, "a new track kept the old key"
        finally:
            pane._decoder = None
            pane.deleteLater()

    def test_the_same_beats_twice_are_set_once(self, qapp, monkeypatch):
        """Setting them again starts the strobe's count over mid-song."""
        from pathlib import Path

        import beatmap
        from attachment_view import AudioPane

        told = self._callbacks(monkeypatch)
        pane = AudioPane()
        try:
            pane._start_analysis(Path("song"))
            maps = {"Kick": beatmap.BeatMap(beats=(beatmap.Beat(at=1.0,
                                                                strength=1.0),),
                                            bpm=120.0, locked=True)}
            times = []
            real = pane.spectrum.set_beats
            pane.spectrum.set_beats = lambda given: (times.append(given),
                                                     real(given))
            told["beaten"](maps)
            same = {"Kick": beatmap.BeatMap(beats=(beatmap.Beat(at=1.0,
                                                                strength=1.0),),
                                            bpm=120.0, locked=True)}
            told["done"](([], [], [], {}, same))
            assert len(times) == 1, "the same beats were set twice"
            other = {"Kick": beatmap.BeatMap(beats=(beatmap.Beat(at=2.0,
                                                                 strength=1.0),),
                                             bpm=120.0, locked=True)}
            told["done"](([], [], [], {}, other))
            assert len(times) == 2, "different beats at the end were ignored"
        finally:
            pane._decoder = None
            pane.deleteLater()

    def test_the_drums_own_beat_reaches_the_game(self, qapp, monkeypatch):
        """The drums worker's tempo and phase, which the road is laid on,
        handed to the picture and on to the game each frame - and gone
        with the track."""
        from pathlib import Path

        from attachment_view import AudioPane

        told = self._callbacks(monkeypatch)
        pane = AudioPane()
        try:
            pane._start_analysis(Path("song"))
            found = {"tempo": 174.0, "phase": 0.12, "faster": 2.0,
                     "measured": {}, "spans": []}
            told["rhythmic"](found)
            assert pane.spectrum.rhythm() is found
            state = pane.spectrum._state
            pane.spectrum._clock(state)
            assert state.rhythm is found
            pane.stop()
            pane.spectrum._clock(state)
            assert state.rhythm is None, "a new track kept the old beat"
        finally:
            pane._decoder = None
            pane.deleteLater()
