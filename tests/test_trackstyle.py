"""How a track moves and how it is put together. See trackstyle.

Charts written by songkit, where every hit is where it says - and then
the same charts buried in the kind of false hits a detector really finds
on a mix, two and three to the beat, because a reading that only works on
clean hits does not work.
"""

from __future__ import annotations

import random

import pytest

import songkit
import trackstyle

FAMILY = {"house": "steady", "techno": "steady", "trance": "steady",
          "hardstyle": "steady", "dubstep": "heavy", "trap": "heavy",
          "dnb": "broken", "garage": "swung", "hiphop": "swung",
          "ambient": "calm"}


def _kit(bpm, kick, snare, hats=(0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5), bars=48,
         bleed=0.0):
    """Onset strengths for ``bars`` bars of one pattern at ``bpm``, with the
    places in the bar in beats. ``bleed`` of the kick heard in the snare's
    bands, as it is on a real mix."""
    beat = 60.0 / bpm
    length = bars * 4 * beat + 1.0

    def curve(places):
        return trackstyle.envelope_from(
            [(bar * 4 + place) * beat for bar in range(bars)
             for place in places], 60.0, length)

    k, sn, h = curve(kick), curve(snare), curve(hats)
    if bleed:
        sn = [a + bleed * b for a, b in zip(sn, k)]
    return {"Kick": trackstyle._Kept(k, 60.0),
            "Snare": trackstyle._Kept(sn, 60.0),
            "Hats": trackstyle._Kept(h, 60.0)}


def _read(style, noise=0.0, offset=0.0, **options):
    chart, contour, beat, truth = songkit.chart(style, offset=offset)
    if noise:
        rng = random.Random(3)
        length = len(contour["loud"]) / contour["rate"]
        for name in ("Kick", "Snare", "Hats"):
            extra = [rng.uniform(0.0, length)
                     for _ in range(int(length / beat * noise))]
            chart[name] = sorted(chart[name] + extra)
    return trackstyle.read(chart, beat, offset, contour, **options), truth, beat


class TestTheStyle:
    @pytest.mark.parametrize("style", sorted(FAMILY))
    def test_each_kind_of_record_reads_as_itself(self, style):
        found, _truth, _beat = _read(style)
        assert found.family() == FAMILY[style], (style, found)

    @pytest.mark.parametrize("style", ["house", "techno", "dubstep", "trap",
                                       "dnb", "garage"])
    def test_and_still_does_under_a_detector_s_mistakes(self, style):
        """Two false hits a beat in every part of the kit, anywhere: what a
        kick detector finds on a real mix is mostly not the kick, and a
        reading from the hits themselves called every record there was
        broken. Folded on the bar, the drums stand out of their own
        noise."""
        found, _truth, _beat = _read(style, noise=2.0)
        assert found.family() == FAMILY[style], (style, found.measured)

    def test_hard_is_steady_and_fast(self):
        hard, _t, _b = _read("hardstyle")
        house, _t, _b = _read("house")
        assert hard.hard > 0.4 and house.hard == 0.0

    def test_rolls_are_rolls_and_swing_is_not(self):
        trap, _t, _b = _read("trap")
        garage, _t, _b = _read("garage")
        assert trap.rolls > 0.5 and garage.rolls == 0.0
        assert garage.swung > 0.5 and trap.swung == 0.0


class TestHalfTime:
    def test_a_kick_heard_in_the_snare_is_not_a_second_snare(self):
        """On a real mix the kick is loud in the snare's bands too, and a
        dubstep record's snare then had a second strong beat - the kick's
        own. Counting strong snare beats, thirteen dubstep and trap records
        came out with 0.83 of a second snare, and not one of them was half
        time. Where the snare is against the kick says it."""
        kit = _kit(140.0, kick=(0, 2.5), snare=(2,), bleed=0.85)
        found = trackstyle.rhythm_of(kit, tempo=140.0)
        assert found["measured"]["second_snare"] > 0.6
        assert found["measured"]["half"] >= trackstyle.HALF_SURE
        style = trackstyle.read({}, 60.0 / 140.0, 0.0, {}, length=90.0,
                                rhythm_found=found)
        assert style.family() == "heavy", style

    @pytest.mark.parametrize("bpm,kick,snare", [
        (174.0, (0, 1.75, 2.5), (1, 3)),     # drum and bass
        (124.0, (0, 1, 2, 3), (1, 3)),       # house
        (90.0, (0, 1.5, 2.75), (1, 3))])     # hip hop
    def test_a_backbeat_is_not_half_time(self, bpm, kick, snare):
        found = trackstyle.rhythm_of(_kit(bpm, kick, snare, bleed=0.85),
                                     tempo=bpm)
        assert found["measured"]["half"] < trackstyle.HALF_FROM

    def test_hip_hop_counted_at_double_is_counted_at_its_own(self):
        """Kick on one and three, snare on two and four at 88: counted at
        176 it is kick on one, snare on three - half time, to the fold -
        and the road ran at twice the speed of the music."""
        kit = _kit(88.0, kick=(0, 2), snare=(1, 3), bleed=0.5)
        found = trackstyle.rhythm_of(kit, tempo=176.0)
        assert abs(found["tempo"] - 88.0) < 0.1, found["tempo"]
        assert found["faster"] == 1.0
        # Read again at 88, where it is the backbeat it is.
        assert found["measured"]["half"] < trackstyle.HALF_FROM
        assert found["measured"]["second_snare"] > 0.9
        period = 60.0 / found["tempo"]
        # And on the kick, not the snare, now the beat is twice as long.
        off = (found["phase"] / period) % 1.0
        assert min(off, 1.0 - off) < 0.07, off

    def test_a_snare_on_the_next_beat_too_is_not_half_time(self):
        """Half time is the snare two beats from the kick and not beside
        it: a snare on the beat after the kick as well is a break."""
        kit = _kit(140.0, kick=(0,), snare=(1, 2))
        found = trackstyle.rhythm_of(kit, tempo=140.0)
        assert found["measured"]["half"] < trackstyle.HALF_FROM

    def test_no_kick_is_not_counted_again(self):
        """A snare every other beat and no kick - claps over an intro - is
        not a backbeat counted twice: there is no kick to say so."""
        kit = _kit(170.0, kick=(), snare=(2,))
        found = trackstyle.rhythm_of(kit, tempo=170.0)
        assert abs(found["tempo"] - 170.0) < 0.1, found["tempo"]

    @pytest.mark.parametrize("bpm", [140.0, 150.0])
    def test_dubstep_keeps_its_tempo(self, bpm):
        kit = _kit(bpm, kick=(0, 2.5), snare=(2,), bleed=0.5)
        found = trackstyle.rhythm_of(kit, tempo=bpm)
        assert abs(found["tempo"] - bpm) < 0.1, found["tempo"]


class TestTheSections:
    @pytest.mark.parametrize("style", ["house", "dubstep", "dnb", "garage"])
    def test_every_part_is_found_where_it_is(self, style):
        found, truth, beat = _read(style, offset=0.3)
        got = [(section.kind, section.start) for section in found.sections]
        assert [kind for kind, _ in got] == [kind for kind, _s, _e in truth]
        # The first part runs from the start of the file, before the
        # record's first beat.
        for (_kind, start), (_k, want, _e) in list(zip(got, truth))[1:]:
            assert abs(start - want) <= beat * 0.6, (start, want)

    def test_drops_are_drops_under_noise(self):
        found, truth, _beat = _read("house", noise=2.0)
        assert [s.kind for s in found.sections].count("drop") == 2

    def test_section_at(self):
        found, truth, _beat = _read("house")
        drop = truth[2]
        middle = (drop[1] + drop[2]) / 2
        assert found.section_at(middle).kind == "drop"
        assert found.section_at(-5.0) is None


class TestTheDrumsOwnGrid:
    """The beat maps are phased from the first thing they heard, and on
    real records that was a third of a beat out: every figure laid between
    two beats. The drums folded over the whole track say where the beat
    is."""

    @pytest.mark.parametrize("style", ["house", "dubstep", "garage", "hiphop"])
    def test_the_tempo_is_refined_and_the_beat_found(self, style):
        chart, contour, beat, _truth = songkit.chart(style, offset=0.137)
        length = len(contour["loud"]) / contour["rate"]
        kit = {name: trackstyle._Kept(trackstyle.envelope_from(
            chart.get(name, ()), 60.0, length), 60.0)
            for name in ("Kick", "Snare", "Hats")}
        # Started from a reading a third of a per cent out, as a beat map's
        # whole-lag reading is.
        found = trackstyle.rhythm_of(kit, hint=60.0 / beat * 1.003)
        assert abs(found["tempo"] - 60.0 / beat) < 0.05, found["tempo"]
        period = 60.0 / found["tempo"]
        off = ((found["phase"] - 0.137) / period) % 1.0
        assert min(off, 1.0 - off) < 0.07, off

    def test_drum_and_bass_counted_at_half_is_ridden_at_its_own(self):
        """Folded to 87 to suit every other scene, drum and bass rode at
        half its speed. Its snare lands on the off-beat of every beat it is
        counted in, and that says so."""
        chart, contour, beat, _truth = songkit.chart("dnb")
        found = trackstyle.read(chart, beat * 2.0, 0.0, contour)
        assert found.faster == 2.0
        assert abs(found.tempo - 60.0 / beat) < 0.5, found.tempo
        assert found.family() == "broken"

    def test_hip_hop_at_ninety_is_not_doubled(self):
        found, _t, _b = _read("hiphop")
        assert found.faster == 1.0


class TestItsOwnNumber:
    def test_the_same_record_is_the_same_seed_and_another_is_not(self):
        one, _t, _b = _read("house")
        again, _t, _b = _read("house")
        other, _t, _b = _read("techno")
        assert one.seed == again.seed and one.seed != other.seed

    def test_light_reads_no_pattern(self):
        """On the thread that draws, before the drums' own reading lands:
        the sections, and nothing that walks every reading of the kit."""
        chart, contour, beat, _truth = songkit.chart("house")
        light = trackstyle.read(chart, beat, 0.0, contour, light=True)
        assert light.sections and not light.from_drums
        assert light.steady == light.broken == light.heavy == 0.0


class TestTheBeatIsTheKick:
    def test_an_off_beat_bass_louder_than_the_kick_is_not_the_beat(self):
        """House's bass on the and of every beat lands in the kick's own
        bands, and louder than the kick it took the beat half a beat out on
        three records in twenty-four. The snare on two and four says which
        is which."""
        chart, contour, beat, _truth = songkit.chart("house", offset=0.2)
        length = len(contour["loud"]) / contour["rate"]
        kick = trackstyle.envelope_from(chart["Kick"], 60.0, length)
        bass = trackstyle.envelope_from(
            [when + beat / 2 for when in chart["Kick"]], 60.0, length)
        loud_bass = [a + 2.0 * b for a, b in zip(kick, bass)]
        kit = {"Kick": trackstyle._Kept(loud_bass, 60.0),
               "Snare": trackstyle._Kept(trackstyle.envelope_from(
                   chart["Snare"], 60.0, length), 60.0),
               "Hats": trackstyle._Kept(trackstyle.envelope_from(
                   chart["Hats"], 60.0, length), 60.0)}
        found = trackstyle.rhythm_of(kit, tempo=60.0 / beat)
        off = ((found["phase"] - 0.2) / beat) % 1.0
        assert min(off, 1.0 - off) < 0.1, off

    def test_and_where_the_bass_is_five_times_the_kick(self):
        """A soft kick under a bass five times as loud on the and of every
        beat, and the open hat's tail in the snare's bands there too: the
        kick and the snare together then peak between the beats, and it is
        the snare on two and four, half a beat from there, that says where
        the beat really is."""
        chart, contour, beat, _truth = songkit.chart("house", offset=0.2)
        length = len(contour["loud"]) / contour["rate"]
        kick = trackstyle.envelope_from(chart["Kick"], 60.0, length)
        between = trackstyle.envelope_from(
            [when + beat / 2 for when in chart["Kick"]], 60.0, length)
        snare = trackstyle.envelope_from(chart["Snare"], 60.0, length)
        kit = {"Kick": trackstyle._Kept(
                   [a + 5.0 * b for a, b in zip(kick, between)], 60.0),
               "Snare": trackstyle._Kept(
                   [a + 0.25 * b for a, b in zip(snare, between)], 60.0),
               "Hats": trackstyle._Kept(trackstyle.envelope_from(
                   chart["Hats"], 60.0, length), 60.0)}
        found = trackstyle.rhythm_of(kit, tempo=60.0 / beat)
        off = ((found["phase"] - 0.2) / beat) % 1.0
        assert min(off, 1.0 - off) < 0.1, off

    def test_one_late_accent_a_bar_is_not_swing(self):
        """Straight eighths and a percussion hit late in one beat of the
        bar, louder than the hat it follows: where the off-beat falls in
        most beats is the swing, not where it falls on average."""
        kit = _kit(124.0, kick=(0, 1, 2, 3), snare=(1, 3),
                   hats=(0, 0.5, 1, 1.5, 2, 2.5, 3, 3.5, 3.71, 3.71))
        found = trackstyle.rhythm_of(kit, tempo=124.0)
        assert found["measured"]["swing"] < 0.02, found["measured"]["swing"]
        style = trackstyle.read({}, 60.0 / 124.0, 0.0, {}, length=90.0,
                                rhythm_found=found)
        assert style.swung == 0.0

    @pytest.mark.parametrize("style", ["house", "dnb", "garage", "hiphop"])
    def test_the_grid_is_on_the_hits(self, style):
        """The fold finds the beat to a twenty-fourth of it and the tempo
        to a fiftieth of a beat a minute: a slot early, and 50 ms adrift by
        the end of five minutes. The hits are timed to the sample, and the
        line through them is the grid."""
        import ridekit

        chart, contour, beat, _truth = songkit.chart(style, offset=0.137)
        length = len(contour["loud"]) / contour["rate"]
        found = trackstyle.rhythm_of(ridekit.kit_for(chart, length),
                                     tempo=60.0 / beat)
        assert abs(found["tempo"] - 60.0 / beat) < 0.005, found["tempo"]
        period = 60.0 / found["tempo"]
        off = ((found["phase"] - 0.137) / period) % 1.0
        assert min(off, 1.0 - off) * period < 0.003, off * period
        # And still there at the end of the record, not only the start:
        # against the last kick that is on a beat.
        on_beats = [at for at in chart["Kick"]
                    if abs((at - 0.137) / beat - round((at - 0.137) / beat))
                    < 1e-6]
        last = max(on_beats)
        drift = ((last - found["phase"]) / period) % 1.0
        assert min(drift, 1.0 - drift) * period < 0.003

    def test_a_snare_roll_with_no_kick_is_not_double_time(self):
        """A build rolls its snare on every beat, and a record with no kick
        in it at all was doubled on that."""
        found, _t, beat = _read("ambient")
        assert abs(found.tempo - 60.0 / beat) < 0.5, found.tempo

    def test_a_reading_three_quarters_out_is_put_right(self):
        """The beat maps' commonest mistake after an octave: a dotted
        pattern read as the beat."""
        chart, contour, beat, _truth = songkit.chart("house")
        length = len(contour["loud"]) / contour["rate"]
        both = [a + b for a, b in zip(
            trackstyle.envelope_from(chart["Kick"], 60.0, length),
            trackstyle.envelope_from(chart["Snare"], 60.0, length))]
        tempo = 60.0 / beat
        assert abs(trackstyle.choose_tempo(both, 60.0, [tempo * 0.75])
                   - tempo) < 0.1
