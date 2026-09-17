"""Finding the beat, and the reasons the old strobe could not.

The complaint was that it was inconsistent. These tests are mostly about
that one word: the same music, louder or quieter, has to give the same
lighting, and music with no beat in it has to be admitted rather than
guessed at.
"""

from __future__ import annotations

import math
import random
from array import array

import pytest

import beatmap
from beatmap import Beat, BeatMap


def click(bpm: float, seconds: float = 16.0, gain: float = 1.0,
          jitter: float = 0.0, hats: bool = False, seed: int = 5):
    """A kick on every beat, at a known tempo, in interleaved stereo."""
    import attachment_audio

    shake = random.Random(seed)
    rate = attachment_audio.DECODE_RATE
    total = int(rate * seconds)
    pcm = array("h", [0]) * (total * 2)
    period = 60.0 / bpm
    at = 0.0
    while at < seconds:
        when = at + shake.uniform(-jitter, jitter)
        start = int(when * rate)
        for step in range(int(rate * 0.12)):
            if start + step >= total or start + step < 0:
                continue
            value = int(20000 * gain * math.exp(-step / (rate * 0.03))
                        * math.sin(2 * math.pi * 55 * step / rate))
            here = (start + step) * 2
            pcm[here] = max(-32768, min(32767, pcm[here] + value))
            pcm[here + 1] = pcm[here]
        if hats:
            start = int((when + period / 2) * rate)
            for step in range(int(rate * 0.04)):
                if start + step >= total:
                    continue
                value = int(6000 * gain * math.exp(-step / (rate * 0.008))
                            * shake.uniform(-1, 1))
                here = (start + step) * 2
                pcm[here] = max(-32768, min(32767, pcm[here] + value))
                pcm[here + 1] = pcm[here]
        at += period
    return pcm


def mapped(pcm, source: str = "Bass") -> BeatMap:
    import attachment_audio

    frames = attachment_audio.analyse(pcm, attachment_audio.DECODE_RATE, 2)
    return beatmap.build(frames, attachment_audio.RATE)[source]


class TestItFindsTheTempo:
    @pytest.mark.parametrize("bpm", [90, 120, 140])
    def test_a_click_track_comes_back_at_its_own_tempo(self, bpm):
        found = mapped(click(bpm))
        assert found.locked, f"no tempo found for {bpm} BPM"
        # Half and double are correct answers to a slightly different
        # question - "every other beat" is a real way to light a room -
        # so they count.
        ratio = found.bpm / bpm
        assert min(abs(ratio - r) for r in (0.5, 1.0, 2.0)) < 0.04, (
            f"{bpm} BPM came back as {found.bpm:.1f}")

    def test_a_tempo_between_frames_is_still_found(self):
        """120 BPM is 7.5 frames at the rate the analysis runs. An
        autocorrelation can only use whole frames, so it cannot express
        this tempo at all - it used to answer 82 BPM, every time."""
        found = mapped(click(120))
        assert abs(found.bpm - 120.0) < 3.0, f"{found.bpm:.1f} BPM"

    def test_a_human_drummer_still_locks(self):
        found = mapped(click(120, jitter=0.02))
        assert found.locked
        assert abs(found.bpm - 120.0) < 5.0

    def test_the_grid_is_perfectly_even(self):
        """Which is the point of fitting one rather than following the
        onsets: lighting that follows transients exactly looks nervous."""
        found = mapped(click(120))
        gaps = [found.beats[i + 1].at - found.beats[i].at
                for i in range(len(found.beats) - 1)]
        assert max(gaps) - min(gaps) < 0.001, "the grid is not steady"

    def test_the_grid_covers_the_whole_track(self):
        """Including the bars where nothing was hit."""
        found = mapped(click(120, seconds=16))
        assert found.beats[0].at < 1.0
        assert found.beats[-1].at > 14.0


class TestItIsTheSameWhateverTheVolume:
    """The complaint, in one class.

    Note what is *not* being claimed here. The analysis already normalises
    each track, so a recording mastered quietly does not by itself defeat
    a fixed threshold. What defeats one is a track whose dynamics change
    inside itself - a quiet verse into a loud chorus - and that is the
    case the first test below covers, because it is the one a fixed
    number cannot be chosen for.
    """


    def test_a_quiet_track_finds_the_same_beats(self):
        loud = mapped(click(120, gain=1.0))
        quiet = mapped(click(120, gain=0.03))
        assert quiet.locked, "30 dB down and it found nothing"
        assert abs(loud.bpm - quiet.bpm) < 1.0
        assert abs(len(loud.beats) - len(quiet.beats)) <= 1

    def test_the_beats_land_in_the_same_places(self):
        loud = mapped(click(120, gain=1.0))
        quiet = mapped(click(120, gain=0.03))
        pairs = list(zip(loud.beats, quiet.beats))
        assert pairs
        worst = max(abs(a.at - b.at) for a, b in pairs)
        assert worst < 0.05, f"they drifted apart by {worst * 1000:.0f} ms"


class TestItAdmitsWhenThereIsNoBeat:
    def test_noise_does_not_lock_to_a_tempo(self):
        import attachment_audio

        shake = random.Random(11)
        rate = attachment_audio.DECODE_RATE
        pcm = array("h")
        for index in range(rate * 12):
            swell = 0.5 + 0.5 * math.sin(2 * math.pi * 0.7 * index / rate)
            value = int(7000 * swell * shake.uniform(-1, 1))
            pcm.append(value)
            pcm.append(value)
        assert not mapped(pcm).locked, "it found a tempo in noise"

    def test_a_handful_of_onsets_can_never_lock(self):
        """A dozen scattered onsets bunch respectably by pure chance - the
        expected score for n of them is about sqrt(pi/4n), which for
        twelve is a third. The bar has to clear chance, not a constant."""
        assert beatmap._lock_bar(4) > 1.0
        assert beatmap._lock_bar(12) >= beatmap._lock_bar(200)
        assert beatmap._lock_bar(12) >= beatmap.LOCK_SCORE

    def test_silence_comes_back_empty(self):
        import attachment_audio

        quiet = array("h", [0]) * (attachment_audio.DECODE_RATE * 4)
        found = mapped(quiet)
        assert not found.locked
        assert found.describe()


class TestTheParts:
    def test_flux_counts_rises_and_not_falls(self):
        rising = [array("f", [0.0, 0.0]), array("f", [0.5, 0.5])]
        falling = [array("f", [0.5, 0.5]), array("f", [0.0, 0.0])]
        assert beatmap.flux(rising, 0.0, 1.0)[1] > 0.4
        assert beatmap.flux(falling, 0.0, 1.0)[1] == 0.0

    def test_flux_only_reads_the_bands_it_was_asked_for(self):
        frames = [array("f", [0.0] * 10), array("f", [0.0] * 5 + [1.0] * 5)]
        assert beatmap.flux(frames, 0.0, 0.5)[1] == 0.0
        assert beatmap.flux(frames, 0.5, 1.0)[1] > 0.9

    def test_an_onset_is_placed_between_frames(self):
        """A fifteen-a-second analysis puts a peak within 66 ms; a
        parabola through it and its neighbours puts it within about 10."""
        envelope = [0.0] * 10 + [0.2, 0.9, 0.6] + [0.0] * 10
        found = beatmap.onsets(envelope, 15.0, sensitivity=0.9)
        assert found, "no onset found at all"
        # The peak leans towards whichever neighbour is higher - here the
        # one after it - so it lands between the two, not on a frame.
        assert 11.0 / 15.0 < found[0].at < 12.0 / 15.0
        assert found[0].at * 15.0 != 11.0, "it snapped to a frame"

    def test_sensitivity_changes_how_much_is_found(self):
        shake = random.Random(3)
        envelope = [shake.random() * 0.2 for _ in range(300)]
        for at in range(10, 300, 25):
            envelope[at] = 0.5 + shake.random() * 0.5
        fussy = beatmap.onsets(envelope, 15.0, sensitivity=0.0)
        eager = beatmap.onsets(envelope, 15.0, sensitivity=1.0)
        assert len(eager) > len(fussy)

    def test_two_flashes_never_come_closer_than_the_floor(self):
        envelope = [0.0, 1.0, 0.0, 1.0, 0.0, 1.0, 0.0] * 12
        found = beatmap.onsets(envelope, 60.0, sensitivity=1.0)
        gaps = [found[i + 1].at - found[i].at for i in range(len(found) - 1)]
        assert not gaps or min(gaps) >= beatmap.FLOOR_GAP - 1e-9

    def test_it_prefers_an_ordinary_tempo_to_its_own_double(self):
        """Onsets every half second fit 120 BPM, and fit 240 exactly as
        well, and 60, and 30. Nothing in the arithmetic breaks that tie,
        so a preference for the tempos music is actually written at does
        - otherwise the strobe picks whichever end of the range it
        happened to search last."""
        beats = tuple(Beat(at=n * 0.5, strength=1.0) for n in range(40))
        bpm, score, _phase = beatmap.tempo_of(beats)
        assert score > 0.9, "it did not fit a perfectly regular series"
        assert 100.0 <= bpm <= 130.0, (
            f"a beat every 500 ms came back as {bpm:.0f} BPM")

    def test_next_after_finds_the_first_one_due(self):
        beats = tuple(Beat(at=n * 0.5, strength=1.0) for n in range(10))
        assert beatmap.next_after(beats, 0.0).at == 0.0
        assert beatmap.next_after(beats, 1.2).at == pytest.approx(1.5)
        assert beatmap.next_after(beats, 99.0) is None

    def test_a_map_can_be_filtered_by_how_hard_a_beat_was_hit(self):
        found = BeatMap(beats=(Beat(1.0, 0.2), Beat(2.0, 0.9)))
        assert len(found.at_least(0.5)) == 1

    def test_every_source_gets_a_map(self):
        import attachment_audio

        frames = attachment_audio.analyse(
            click(120), attachment_audio.DECODE_RATE, 2)
        built = beatmap.build(frames, attachment_audio.RATE)
        assert set(built) == set(beatmap.SOURCES)


class TestTheStrobeUsesIt:
    @staticmethod
    def _run(qapp, bpm=120, gain=1.0, sense=0.5, rate=1.0, seconds=12):
        import attachment_audio
        from attachment_widgets import Spectrum

        pcm = click(bpm, seconds=seconds, gain=gain)
        frames = attachment_audio.analyse(
            pcm, attachment_audio.DECODE_RATE, 2)
        spectrum = Spectrum()
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.set_beats(beatmap.build(frames, attachment_audio.RATE))
        spectrum.set_strobe(True)
        spectrum.set_strobe_source("Bass")
        spectrum.set_strobe_sense(sense)
        spectrum.set_strobe_rate(rate)
        flashes = []
        for step in range(seconds * 60):
            spectrum.set_position(int(step / 60.0 * 1000))
            spectrum._tick()
            if spectrum._state.hit > 0.9:
                flashes.append(step / 60.0)
        return flashes

    def test_it_flashes_on_the_beat(self, qapp):
        flashes = self._run(qapp)
        assert len(flashes) > 15, f"only {len(flashes)} flashes in 12 seconds"
        gaps = [flashes[i + 1] - flashes[i] for i in range(len(flashes) - 1)]
        gaps = [g for g in gaps if g > 0.05]
        average = sum(gaps) / len(gaps)
        assert abs(average - 0.5) < 0.03, (
            f"it flashed every {average * 1000:.0f} ms on a 500 ms beat")

    def test_a_quiet_track_is_lit_the_same_as_a_loud_one(self, qapp):
        """The whole complaint. Thirty decibels down used to mean the
        threshold was never crossed and the strobe simply never fired."""
        loud = self._run(qapp, gain=1.0)
        quiet = self._run(qapp, gain=0.03)
        assert quiet, "a quiet track did not set the strobe off at all"
        assert abs(len(loud) - len(quiet)) <= 2

    def test_the_rate_control_thins_it_out(self, qapp):
        every = self._run(qapp, rate=1.0)
        sparse = self._run(qapp, rate=0.25)
        assert len(sparse) < len(every) * 0.75, (
            f"{len(sparse)} flashes against {len(every)}")

    def test_it_still_works_before_the_analysis_arrives(self, qapp):
        """The map is built after the track is decoded. Until it is there,
        the old frame-to-frame rule is what there is."""
        import attachment_audio
        from attachment_widgets import Spectrum

        pcm = click(120)
        frames = attachment_audio.analyse(
            pcm, attachment_audio.DECODE_RATE, 2)
        spectrum = Spectrum()
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.set_strobe(True)
        spectrum.set_strobe_sense(0.9)
        spectrum.set_strobe_rate(1.0)
        assert spectrum.beat_map() is None
        lit = False
        for step in range(600):
            spectrum.set_position(int(step / 60.0 * 1000))
            spectrum._tick()
            lit = lit or spectrum._state.hit > 0.9
        assert lit, "with no map and no fallback, nothing ever flashes"

    def test_seeking_does_not_replay_every_beat_it_skipped(self, qapp):
        from attachment_widgets import Spectrum
        import attachment_audio

        pcm = click(120, seconds=20)
        frames = attachment_audio.analyse(
            pcm, attachment_audio.DECODE_RATE, 2)
        spectrum = Spectrum()
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.set_beats(beatmap.build(frames, attachment_audio.RATE))
        spectrum.set_strobe(True)
        spectrum.set_position(0)
        spectrum._tick()
        spectrum.set_position(15000)      # fifteen seconds forward
        spectrum._tick()
        # Thirty beats went by while nobody was looking, and every one of
        # them is still sitting in the list ahead of the cursor. Walking
        # to the new position one beat at a time fires the strobe on every
        # one of them, so a seek ends in a flash - the cursor has to be
        # found rather than walked to, and that frame lights nothing.
        assert spectrum._state.hit < 0.5, (
            "seeking set the strobe off")
        assert spectrum._beat_at > 20, (
            "the cursor did not move to where the playhead went")
        after = spectrum._beat_at
        spectrum.set_position(15016)
        spectrum._tick()
        assert spectrum._beat_at - after <= 2
