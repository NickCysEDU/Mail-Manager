"""Every scene on the beat you hear. See Spectrum._now and trackstyle.on_the_hits.

A click track whose every beat is known to the sample, through the real
analysis, into the real pane, driven by a player that moves its position every
50 ms the way the media player measurably does, and each frame's answer to each
click timed against the click.

What these cannot see is the audio device and the display: how long the
sound takes to reach the ear and the frame to reach the glass. Those are
Spectrum.ahead's, and tested there.
"""

from __future__ import annotations

import math
import statistics

import pytest

BPM = 120.0
SECONDS = 20.0
FPS = 60


@pytest.fixture(scope="module")
def clicks(qapp):
    """A kick every half second, analysed as a track is."""
    from array import array

    import attachment_audio
    import beatmap
    import trackstyle

    rate = attachment_audio.DECODE_RATE
    beat = 60.0 / BPM
    times = [1.0 + i * beat for i in range(int((SECONDS - 2.0) / beat))]
    pcm = array("h", [0]) * int(SECONDS * rate)
    for at in times:
        start = int(at * rate)
        for i in range(int(0.12 * rate)):
            value = (math.sin(2 * math.pi * 60 * (1 - 0.5 * i / (0.12 * rate))
                              * i / rate) * math.exp(-i / (0.04 * rate)))
            pcm[start + i] = int(26000 * value)
    frames = attachment_audio.analyse(pcm, rate, 1)
    kit = beatmap.elements(attachment_audio.onset_frames(pcm, rate, 1),
                           attachment_audio.ONSET_RATE)
    return {"times": times, "frames": frames,
            "maps": beatmap.build(frames, attachment_audio.RATE), "kit": kit,
            "rhythm": trackstyle.rhythm_of(kit)}


def _watch(clicks, phase=1, rhythm=True, monkeypatch=None):
    """Play it through a pane frame by frame; each frame's (moment of the
    track, bass, kick, lowest bar, beat phase)."""
    import time

    import attachment_audio
    import attachment_widgets

    pane = attachment_widgets.Spectrum()
    pane.set_frames(clicks["frames"], attachment_audio.RATE)
    pane.set_beats(clicks["maps"])
    pane.set_elements(clicks["kit"])
    if rhythm:
        pane.set_rhythm(clicks["rhythm"])
    pane.set_playing(True)
    wall = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: wall[0])
    reported = [0]
    pane.follow(lambda: reported[0])
    rows = []
    for frame in range(int(SECONDS * FPS)):
        wall[0] = 100.0 + frame / FPS
        truth = frame / FPS
        # The player's position moves every 50 ms, out of step with the
        # clicks by ``phase`` frames.
        if (frame + phase) % 3 == 0:
            reported[0] = int(truth * 1000)
        pane._tick()
        state = pane._state
        rows.append((truth, state.bass, state.kit.get("Kick", 0.0),
                     pane._level[0] if pane._level else 0.0, state.beat_at))
    pane.deleteLater()
    return rows


def _answers(clicks, rows, column, threshold):
    """For each click, how long after it the column first came up through
    ``threshold``, in ms."""
    out = []
    for click in clicks["times"][2:-2]:
        before = None
        for row in rows:
            if click - 0.2 <= row[0] <= click + 0.3:
                value = row[column]
                if before is not None and before < threshold <= value:
                    out.append((row[0] - click) * 1000.0)
                    break
                before = value
    return out


class TestEveryFrameIsOnTheBeat:
    @pytest.mark.parametrize("phase", [0, 1, 2])
    def test_the_kick_lights_in_the_frame_nearest_it(self, clicks, phase,
                                                     monkeypatch):
        """Read off the player's last word, the kick lit up to 33 ms late,
        and by how much depended on where in the player's 50 ms step the
        frame fell - on one record and not the next."""
        rows = _watch(clicks, phase, monkeypatch=monkeypatch)
        late = _answers(clicks, rows, 2, 0.3)
        assert len(late) == len(clicks["times"]) - 4
        assert all(abs(ms) <= 1000.0 / FPS / 2 + 0.5 for ms in late), late

    def test_the_beat_comes_round_on_the_kick(self, clicks, monkeypatch):
        """Every scene's pulse, from the drums' own beat - a slot early,
        and a fiftieth of a beat a minute adrift, before the grid was put
        on the hits."""
        rows = _watch(clicks, monkeypatch=monkeypatch)
        wraps = [rows[i][0] for i in range(1, len(rows))
                 if rows[i - 1][4] > 0.7 and rows[i][4] < 0.3]
        errors = [min((wrap - click for click in clicks["times"]), key=abs)
                  * 1000.0 for wrap in wraps if 1.2 < wrap < SECONDS - 1.0]
        assert len(errors) > 25
        # The first frame at or after the beat: never before it, and never
        # a whole frame after.
        assert all(-0.5 <= ms < 1000.0 / FPS for ms in errors), errors

    def test_the_bars_rise_with_the_kick(self, clicks, monkeypatch):
        """As near as fifteen frames a second of bands can be: eased
        between frames they began to rise a frame early and were half way
        up 25 ms before the kick; now a frame's sound is shown from the
        middle of its window."""
        rows = _watch(clicks, monkeypatch=monkeypatch)
        top = max(row[3] for row in rows)
        answers = _answers(clicks, rows, 3, top * 0.5)
        assert len(answers) == len(clicks["times"]) - 4
        assert abs(statistics.median(answers)) <= 20.0, answers
        assert min(answers) >= -1.0, "a bar rose before its kick"

    def test_the_bass_is_half_way_up_on_the_kick(self, clicks, monkeypatch):
        """Eased both ways, and still on the beat: the bands move fifteen
        times a second, and the bass is half way up on the frame they do."""
        rows = _watch(clicks, monkeypatch=monkeypatch)
        top = max(row[1] for row in rows)
        answers = _answers(clicks, rows, 1, top * 0.5)
        assert len(answers) == len(clicks["times"]) - 4
        assert abs(statistics.median(answers)) <= 20.0, answers


class TestNothingBeforeItIsDue:
    """In the frame nearest a hit means half a frame early at most. Taken as
    half the time since the last frame, it was half a second on a track's
    first frame and a tenth after a pause: a kick and a strobe lit for a
    beat that had not come."""

    @staticmethod
    def _pane():
        import attachment_widgets
        import beatmap
        from attachment_widgets import SpectrumState

        kicks = beatmap.BeatMap(beats=(beatmap.Beat(at=0.3, strength=1.0),
                                       beatmap.Beat(at=0.9, strength=1.0)),
                                bpm=100.0, locked=True)
        pane = attachment_widgets.Spectrum()
        pane.set_elements({"Kick": kicks})
        pane.set_strobe(True)
        pane.set_strobe_source("Kick")
        return pane, SpectrumState()

    def test_the_strobe_on_a_track_s_first_frame(self, qapp):
        pane, state = self._pane()
        pane._now = 0.0
        pane._fire_from_the_map(state)
        assert state.hit < 0.5, "flashed for a beat 300 ms off"
        pane.deleteLater()

    def test_the_strobe_after_a_gap(self, qapp):
        pane, state = self._pane()
        pane._beat_at, pane._beat_seen = 1, 0.6
        pane._now = 0.8
        pane._fire_from_the_map(state)
        assert state.hit < 0.5, "flashed for a beat 100 ms off"
        pane.deleteLater()

    def test_the_kick_after_a_gap(self, qapp):
        pane, state = self._pane()
        pane._kit_at, pane._kit_seen = {"Kick": 1}, 0.6
        pane._now = 0.8
        pane._decay_kit(state)
        assert state.kit.get("Kick", 0.0) < 0.3, "lit a kick 100 ms early"
        pane.deleteLater()


class TestOneMomentAFrame:
    def test_the_clock_is_asked_once_a_frame(self, clicks, monkeypatch):
        """Asked three times, it was pulled towards the player three times
        a frame; and everything drawn reads the one answer."""
        import attachment_audio
        import attachment_widgets

        pane = attachment_widgets.Spectrum()
        pane.set_frames(clicks["frames"], attachment_audio.RATE)
        pane.set_elements(clicks["kit"])
        pane.set_rhythm(clicks["rhythm"])
        pane.set_playing(True)
        asked = []
        real = pane._heard
        monkeypatch.setattr(pane, "_heard",
                            lambda: asked.append(1) or real())
        pane.follow(lambda: 2500)
        for _frame in range(5):
            pane._tick()
        assert len(asked) == 5
        assert pane._state.at == pytest.approx(pane._now)
        pane.deleteLater()


class TestTheEarAndTheEye:
    """What the click track cannot see: the sound takes the output
    device's latency to come out, and a frame takes a refresh or two to
    reach the glass. See av_sync."""

    def test_the_picture_is_ahead_by_the_eye_less_the_ear(self):
        import av_sync

        allowance = av_sync.Allowance(ask=lambda: 0.160)
        # Bluetooth headphones: the sound is a sixth of a second behind
        # what has been handed over, so the picture waits for it.
        assert allowance.ahead(60.0) == pytest.approx(1.5 / 60.0 - 0.160)
        allowance.trim = 0.030
        assert allowance.ahead(120.0) == pytest.approx(1.5 / 120.0 - 0.13)

    def test_the_device_is_asked_again_as_headphones_come_and_go(self):
        import av_sync

        now = [0.0]
        answers = iter([0.02, 0.18])
        allowance = av_sync.Allowance(ask=lambda: next(answers),
                                      clock=lambda: now[0])
        assert allowance.audio() == pytest.approx(0.02)
        now[0] = av_sync.ASK_EVERY / 2
        assert allowance.audio() == pytest.approx(0.02), "asked too often"
        now[0] = av_sync.ASK_EVERY + 0.1
        assert allowance.audio() == pytest.approx(0.18)

    @pytest.mark.parametrize("answer", [9.0, -1.0])
    def test_a_latency_past_belief_is_held_to_it(self, answer):
        import av_sync

        allowance = av_sync.Allowance(ask=lambda: answer)
        assert 0.0 <= allowance.audio() <= av_sync.MOST

    def test_a_device_that_will_not_say_is_not_asked_again(self):
        import av_sync

        asked = []

        def broken():
            asked.append(1)
            raise OSError("no audio")

        now = [0.0]
        allowance = av_sync.Allowance(ask=broken, clock=lambda: now[0])
        assert allowance.audio() == 0.0
        now[0] = 100.0
        assert allowance.audio() == 0.0 and len(asked) == 1

    @pytest.mark.skipif(__import__("sys").platform != "darwin",
                        reason="Core Audio is a Mac's")
    def test_this_machine_s_device_answers(self):
        import av_sync

        found = av_sync.device_latency()
        assert found is None or 0.0 <= found <= av_sync.MOST

    def test_the_pane_shows_the_music_that_is_heard(self, qapp):
        """With an allowance, a frame's moment is the clock's plus it."""
        import av_sync
        import attachment_widgets

        pane = attachment_widgets.Spectrum()
        pane.allowance = av_sync.Allowance(ask=lambda: 0.2)
        pane.follow(lambda: 5000)
        pane._tick()
        assert pane._now == pytest.approx(
            pane._heard_now + pane.allowance.ahead(
                pane.screen().refreshRate() if pane.screen() else 60.0),
            abs=1e-6)
        assert pane._now < 5.0, "the picture did not wait for the ear"
        pane.deleteLater()

    def test_a_trim_kept_under_the_old_name_is_not_used(self, qapp):
        import config
        from attachment_view import VIEWER_PREFS, AudioPane

        path = config.app_support_dir() / VIEWER_PREFS
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"sync": 237}')
        pane = AudioPane()
        try:
            assert pane.sync_trim() == 0
        finally:
            pane.deleteLater()

    def test_the_player_brings_one_and_the_trim_is_kept(self, qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        try:
            assert pane._ensure_player()
            assert pane.spectrum.allowance is not None
            assert pane.set_sync_trim(40) == 40
            assert pane.spectrum.allowance.trim == pytest.approx(0.040)
            assert pane.set_sync_trim(9999) == AudioPane.SYNC_MOST
        finally:
            pane.deleteLater()
        again = AudioPane()
        try:
            assert again.sync_trim() == AudioPane.SYNC_MOST
            again._ensure_player()
            assert again.spectrum.allowance.trim == pytest.approx(
                AudioPane.SYNC_MOST / 1000.0)
        finally:
            again.deleteLater()
