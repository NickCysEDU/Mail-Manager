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


class TestPickingTheKitApart:
    """Kick, snare and hats, told from one another.

    The thresholds come from a kit played to a known pattern; these check
    they hold on patterns they were not fitted to, because a classifier
    tuned until one recording passes is a lookup table.
    """

    @staticmethod
    def _play(bpm, pattern, seconds=12, seed=7, kick_hz=52, snare_hz=190):
        import math
        import random
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        shake = random.Random(seed)
        total = int(rate * seconds)
        pcm = array("h", [0]) * (total * 2)
        six = 60.0 / bpm / 4
        want = {name: [] for name in pattern}

        def add(at, make, length):
            start = int(at * rate)
            for step in range(int(rate * length)):
                if not 0 <= start + step < total:
                    continue
                here = (start + step) * 2
                value = int(make(step))
                pcm[here] = max(-32768, min(32767, pcm[here] + value))
                pcm[here + 1] = pcm[here]

        bar = 0.0
        while bar < seconds:
            for step in range(16):
                at = bar + step * six
                if at >= seconds:
                    break
                if step in pattern.get("kick", []):
                    add(at, lambda i: 22000 * math.exp(-i / (rate * 0.05))
                        * math.sin(2 * math.pi
                                   * (kick_hz + 40 * math.exp(-i / (rate * 0.02)))
                                   * i / rate), 0.16)
                    want["kick"].append(at)
                if step in pattern.get("snare", []):
                    add(at, lambda i: (9000 * math.exp(-i / (rate * 0.09))
                                       * shake.uniform(-1, 1)
                                       + 9000 * math.exp(-i / (rate * 0.05))
                                       * math.sin(2 * math.pi * snare_hz * i / rate)),
                        0.12)
                    want["snare"].append(at)
                if step in pattern.get("hats", []):
                    add(at, lambda i: 6000 * math.exp(-i / (rate * 0.006))
                        * shake.uniform(-1, 1), 0.03)
                    want["hats"].append(at)
            bar += six * 16
        return pcm, want

    @staticmethod
    def _found(pcm):
        import attachment_audio

        fine = attachment_audio.onset_frames(
            pcm, attachment_audio.DECODE_RATE, 2)
        assert fine, "the onset pass produced nothing"
        return beatmap.elements(fine, attachment_audio.ONSET_RATE)

    @staticmethod
    def _score(found, name, truth, tol=0.10):
        got = [b.at for b in found[name].beats]
        hit = sum(1 for t in truth if any(abs(t - g) < tol for g in got))
        spurious = sum(1 for g in got
                       if not any(abs(t - g) < tol for t in truth))
        return hit / max(1, len(truth)), spurious

    def test_a_kick_is_found_and_nothing_else_is_called_one(self):
        pcm, want = self._play(120, {"kick": [0, 4, 8, 12],
                                     "hats": [2, 6, 10, 14]})
        found = self._found(pcm)
        recall, spurious = self._score(found, "Kick", want["kick"])
        assert recall > 0.85, f"only found {recall:.0%} of the kicks"
        assert spurious <= 2, f"{spurious} things called a kick that were not"

    def test_hats_alone_are_never_called_kicks(self):
        pcm, want = self._play(120, {"hats": list(range(16))})
        found = self._found(pcm)
        assert len(found["Kick"].beats) <= 2
        assert len(found["Snare"].beats) <= 2
        recall, _ = self._score(found, "Hats", want["hats"])
        assert recall > 0.85

    def test_kicks_alone_are_never_called_hats(self):
        """The one that decided where the hat threshold sits: with the
        band detector trusted on its own, every kick came back as a hat
        as well, and the two channels were the same channel."""
        pcm, want = self._play(120, {"kick": [0, 4, 8, 12]})
        found = self._found(pcm)
        assert len(found["Hats"].beats) <= 3, (
            f"{len(found['Hats'].beats)} hats in a track with no hats")

    def test_a_kick_and_a_snare_do_not_land_on_each_other(self):
        pcm, want = self._play(120, {"kick": [0, 8], "snare": [4, 12],
                                     "hats": [2, 6, 10, 14]})
        found = self._found(pcm)
        kicks = [b.at for b in found["Kick"].beats]
        snares = [b.at for b in found["Snare"].beats]
        assert kicks and snares
        together = sum(1 for k in kicks if any(abs(k - s) < 0.06
                                               for s in snares))
        assert together == 0, (
            f"{together} of {len(kicks)} kicks were also called snares")

    def test_a_snare_is_found_at_a_pitch_it_was_not_tuned_at(self):
        pcm, want = self._play(120, {"kick": [0, 8], "snare": [4, 12]},
                               snare_hz=250)
        found = self._found(pcm)
        recall, spurious = self._score(found, "Snare", want["snare"])
        assert recall > 0.8, f"only found {recall:.0%} of the snares"

    def test_the_bands_are_read_linearly_not_in_decibels(self):
        """Decibels compress a hundred-to-one difference into twenty units
        of seventy, so every band rises together and a kick, a snare and a
        hat come out looking the same - measured, they agreed to within
        four per cent and nothing could be told apart."""
        import attachment_audio

        pcm, _ = self._play(120, {"kick": [0, 4, 8, 12]})
        fine = attachment_audio.onset_frames(
            pcm, attachment_audio.DECODE_RATE, 2)
        loudest = max(max(row) for row in fine)
        quietest = min(min(row) for row in fine)
        # Linear values span orders of magnitude; dB-scaled ones would sit
        # in a narrow band near the top.
        assert loudest > 0.9
        assert quietest < 0.02

    def test_the_finer_pass_can_see_the_bottom_of_the_range(self):
        """At a 512-point window the lowest band a transform can report
        starts at 93 Hz, which is above where a kick lives."""
        import attachment_audio

        bins = attachment_audio.ONSET_WINDOW // 2
        hz = attachment_audio.DECODE_RATE / attachment_audio.ONSET_WINDOW
        low, high = attachment_audio._onset_edges(bins)[0]
        assert low * hz < 60.0, (
            f"the lowest band starts at {low * hz:.0f} Hz, above a kick")


class TestTheStrobeRunsOnAHeldNote:
    """A held note is not a beat. On a grid it gets one flash and then
    nothing until the next bar, which is the opposite of what a room does
    under a sustained bass line.
    """

    @staticmethod
    def _held(seconds=12, bpm=128, hold=(4.0, 8.0)):
        """Kicks throughout, and a bass note held through the middle."""
        import math
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        total = rate * seconds
        pcm = array("h", [0]) * (total * 2)
        beat = 60.0 / bpm
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
            at += beat
        for index in range(int(hold[0] * rate), int(hold[1] * rate)):
            here = index * 2
            value = int(15000 * math.sin(2 * math.pi * 55 * index / rate))
            pcm[here] = max(-32768, min(32767, pcm[here] + value))
            pcm[here + 1] = pcm[here]
        return pcm

    @staticmethod
    def _flashes(qapp, knob_rate, knob_sense, seconds=12):
        import attachment_audio
        from attachment_widgets import Spectrum

        pcm = TestTheStrobeRunsOnAHeldNote._held(seconds=seconds)
        frames = attachment_audio.analyse(
            pcm, attachment_audio.DECODE_RATE, 2)
        spectrum = Spectrum()
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.set_beats(beatmap.build(frames, attachment_audio.RATE))
        spectrum.set_strobe(True)
        spectrum.set_strobe_source("Bass")
        spectrum.set_strobe_rate(knob_rate)
        spectrum.set_strobe_sense(knob_sense)
        out = []
        was = 0.0
        for step in range(seconds * 60):
            spectrum.set_position(int(step / 60.0 * 1000))
            spectrum._tick()
            now = spectrum._state.hit
            # The start of a flash, not every frame it is still lit: the
            # glow decays over two or three and counting samples doubles
            # every number.
            if now > 0.65 and now > was:
                out.append(step / 60.0)
            was = now
        during = len([t for t in out if 4.3 < t < 7.9]) / 3.6
        rest = len([t for t in out if t < 4.0 or t > 8.2]) / 8.0
        return during, rest

    def test_both_knobs_up_runs_it_through_the_note(self, qapp):
        during, rest = self._flashes(qapp, 1.0, 1.0)
        assert during > rest * 3, (
            f"{during:.1f} a second during the held note against "
            f"{rest:.1f} elsewhere")

    def test_it_speeds_up_as_the_knobs_go_up(self, qapp):
        gentle, _ = self._flashes(qapp, 0.7, 0.7)
        hard, _ = self._flashes(qapp, 1.0, 1.0)
        assert hard > gentle * 1.5, f"{gentle:.1f} then {hard:.1f}"

    def test_one_knob_alone_does_not_reach_it(self, qapp):
        """It is the loudest thing the visualiser does. Nobody should
        arrive at it by nudging one slider."""
        only_rate, _ = self._flashes(qapp, 1.0, 0.3)
        only_sense, _ = self._flashes(qapp, 0.3, 1.0)
        both, _ = self._flashes(qapp, 1.0, 1.0)
        assert only_rate < both / 3
        assert only_sense < both / 3

    def test_it_is_capped(self, qapp):
        """Photosensitive epilepsy is provoked most reliably between
        fifteen and twenty flashes a second. This stops well short, and
        the control says so."""
        from attachment_widgets import Spectrum

        during, _ = self._flashes(qapp, 1.0, 1.0)
        assert during <= Spectrum.RAPID_CEILING + 1.0, (
            f"{during:.1f} flashes a second against a cap of "
            f"{Spectrum.RAPID_CEILING}")
        assert Spectrum.RAPID_CEILING <= 12.0

    def test_the_middle_of_the_sliders_is_just_the_beat(self, qapp):
        during, rest = self._flashes(qapp, 0.5, 0.5)
        assert during < 3.0, (
            f"{during:.1f} a second with both sliders centred, which is "
            "not something anybody asked for")


class TestEachSceneGetsAStrobeThatSuitsIt:
    def test_switching_scene_sets_it_up(self, qapp):
        import visualizers
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        spectrum.set_scene(visualizers.by_name("Rave"))
        loud = (spectrum._strobe_rate, spectrum._strobe_sense)
        spectrum.set_scene(visualizers.by_name("VU meters"))
        quiet = (spectrum._strobe_rate, spectrum._strobe_sense)
        assert loud != quiet, "every scene got the same strobe"
        assert loud > quiet, "the rave scene should be the eager one"

    def test_it_stops_once_the_user_has_chosen(self, qapp):
        """A setting that springs back whenever you change something else
        is not a setting."""
        import visualizers
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        spectrum.set_scene(visualizers.by_name("Rave"))
        spectrum.set_strobe_rate(0.9)
        spectrum.set_strobe_sense(0.15)
        spectrum.set_scene(visualizers.by_name("VU meters"))
        assert spectrum._strobe_rate == pytest.approx(0.9)
        assert spectrum._strobe_sense == pytest.approx(0.15)

    def test_every_scene_has_one_and_it_is_a_real_source(self, qapp):
        import visualizers
        from attachment_widgets import Spectrum

        for scene in visualizers.SCENES:
            setup = visualizers.strobe_setup(scene)
            assert setup, f"{scene.name} has no strobe setting"
            source, rate, sense = setup
            assert source in Spectrum.STROBE_SOURCES, (
                f"{scene.name} listens to {source}, which is not a source")
            assert 0.0 <= rate <= 1.0 and 0.0 <= sense <= 1.0

    def test_no_scene_starts_in_the_rapid_range(self, qapp):
        """Reaching it has to be something somebody did."""
        import visualizers
        from attachment_widgets import Spectrum

        for scene in visualizers.SCENES:
            _source, rate, sense = visualizers.strobe_setup(scene)
            assert not (rate >= Spectrum.RAPID_KNOB
                        and sense >= Spectrum.RAPID_KNOB), (
                f"{scene.name} starts with the fast strobe already on")


class TestEveryKindOfSnare:
    """"Improve instrument detection for strobe too. Snare isn't too
    accurate."

    It was not. On a real track it found thirteen snares a minute against
    ninety-one kicks, which is not a drummer anybody has ever heard.

    The reason is in tests/drumkit.py, which writes drum tracks with every
    hit at a known time so this can be measured rather than guessed at.
    "Snare" is not one sound, and the profile that decided what one was
    had been measured off a single kit playing a single pattern.
    """

    KINDS = ("bright", "tight", "clap", "rim")

    @staticmethod
    def _kit(kind, melody=False):
        import attachment_audio
        import drumkit

        pcm, truth = drumkit.track(kind, melody=melody)
        frames = attachment_audio.onset_frames(pcm, drumkit.RATE, 2)
        found = beatmap.elements(frames, attachment_audio.ONSET_RATE)
        return frames, truth, found

    @pytest.mark.parametrize("kind", KINDS)
    def test_every_kind_of_snare_is_found(self, kind):
        """A clap and a rimshot both scored nothing at all before this."""
        import drumkit

        _frames, truth, found = self._kit(kind)
        hits = [b.at for b in found["Snare"].beats]
        _n, recall, precision = drumkit.score(hits, truth["Snare"])
        assert recall >= 0.95, (
            f"found {recall * 100:.0f} per cent of the {kind} snares")
        assert precision >= 0.90, (
            f"{precision * 100:.0f} per cent of what it called a {kind} "
            f"snare was one")

    @pytest.mark.parametrize("kind", KINDS)
    def test_the_kick_is_not_lost_in_the_bargain(self, kind):
        import drumkit

        _frames, truth, found = self._kit(kind)
        hits = [b.at for b in found["Kick"].beats]
        _n, recall, precision = drumkit.score(hits, truth["Kick"])
        assert recall >= 0.90 and precision >= 0.90, (
            f"kick recall {recall * 100:.0f}%, precision "
            f"{precision * 100:.0f}% on the {kind} track")

    @pytest.mark.parametrize("house", [False, True])
    def test_the_kick_is_found_under_a_bassline(self, house):
        """Four to the floor with a sub running under it and a limiter on
        the whole thing - the pattern this was reported as missing."""
        import attachment_audio
        import drumkit

        pcm, truth = drumkit.track("tight", house=house)
        frames = attachment_audio.onset_frames(pcm, drumkit.RATE, 2)
        found = beatmap.elements(frames, attachment_audio.ONSET_RATE)
        hits = [b.at for b in found["Kick"].beats]
        _n, recall, precision = drumkit.score(hits, truth["Kick"])
        assert recall >= 0.90, (
            f"found {recall * 100:.0f} per cent of the kicks")
        assert precision >= 0.80, (
            f"{precision * 100:.0f} per cent of what it called a kick was")

    def test_a_kick_is_what_the_bottom_leads_not_what_it_owns(self):
        """The bound that was wrong, and why it cannot be a bound.

        It asked for 0.70 of a frame's whole rise to be in the bottom
        third. In a mix something else is nearly always happening on the
        beat, so a kick owns well under two thirds of the moment while
        being unmistakably there: on a real track that threw away 227 of
        309 candidates and left 44 kicks a minute where the pulse says
        about 140.

        A number cannot express this, because the number that lets a
        kick through under a hat lets a bass note through on its own.
        What is asked is that the bottom *leads*.
        """
        assert beatmap.PROFILE["Kick"].get("leads") == "bottom"
        # A kick with a hat over it: two thirds bottom, and still a kick.
        assert beatmap.fits(beatmap.PROFILE["Kick"], 0.62, 0.30, 0.08)
        # A kick under a stab: the middle takes more than the bottom.
        assert not beatmap.fits(beatmap.PROFILE["Kick"], 0.40, 0.55, 0.05)
        # A cymbal is not a kick however little middle it has.
        assert not beatmap.fits(beatmap.PROFILE["Kick"], 0.55, 0.05, 0.40)

    def test_the_ceiling_on_the_sizzle_sits_between_a_clap_and_a_hat(self):
        """The one number that was wrong, and the gap it has to sit in.

        A clap puts 0.21 of its rise in the top and the dimmest hat puts
        0.29, so anything from about 0.22 to 0.28 tells them apart. It was
        0.20 - one hundredth under the brightest snare - so every clap
        went to the hats.

        Both halves are checks on a number rather than on behaviour, and
        that is a deliberate choice in each case. The ceiling's behaviour
        is already guarded, by test_hats_alone_are_never_called_kicks,
        which fails outright at 0.45. The floor's is not guardable here:
        the evidence for it is a real track that cannot be checked into a
        public repository, where taking it away doubled the snares from 71
        a minute to 151 and every extra one was a synth. On written tracks
        it makes no difference either way, so the number is the only thing
        left to hold on to.
        """
        low, high = beatmap.PROFILE["Snare"]["top"]
        assert 0.22 <= high <= 0.28, (
            f"the ceiling is {high}, and the gap between a clap at 0.21 "
            f"and a hat at 0.29 is where it belongs")
        assert low > 0.0, (
            "the floor asks a snare to have some air over it, which is "
            "what a pitched note in the same place does not")

    def test_nothing_fires_in_the_silence_between_the_hits(self):
        """The threshold everything else uses is the local median plus a
        multiple of the local spread, and in silence that is zero plus
        zero. Measured before the floor was added: forty-three of the
        sixty-six snares found were in the gaps, on rises a hundredth the
        size of a real one."""
        _frames, truth, found = self._kit("bright")
        real = sorted(truth["Kick"] + truth["Snare"] + truth["Hats"])
        stray = []
        for name in ("Kick", "Snare"):
            for hit in found[name].beats:
                if min(abs(hit.at - w) for w in real) > 0.09:
                    stray.append((name, hit.at))
        assert not stray, (
            f"{len(stray)} hits landed where nothing was played: "
            + ", ".join(f"{n} at {t:.2f}s" for n, t in stray[:6]))

    def test_an_instruments_signature_is_its_attack_not_one_frame(self):
        """A kick's fundamental arrives before its harmonics, so one frame
        into it the bottom has stopped rising and only the middle is
        moving - which reads as a perfect rimshot. Every kick produced
        one, and the snare map came back with twice as many hits as there
        were snares."""
        assert beatmap.ATTACK >= 1, (
            "reading a single frame calls the second half of every kick a "
            "snare")

    def test_the_written_tracks_do_not_click(self):
        """The test of the test, and it earned its place.

        A sample that stops while it is still moving is a step, and a step
        is a broadband click that a detector is right to call a hit. Mine
        stopped at about 1.5 per cent of full height, which put one 150 ms
        after every kick; the false positives looked exactly like rimshots
        and very nearly became a change to the detector.
        """
        import drumkit

        for name, sample in (("kick", drumkit.kick()),
                             ("hat", drumkit.hat()),
                             ("note", drumkit.note(330.0))):
            assert abs(sample[-1]) < 1e-9, (
                f"the {name} stops at {sample[-1]:.4f}, which is a click, "
                f"which is an onset")
        for kind in self.KINDS:
            sample = drumkit.snare(kind)
            assert abs(sample[-1]) < 1e-9, (
                f"the {kind} snare stops at {sample[-1]:.4f}")
