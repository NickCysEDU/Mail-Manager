"""The visualiser window: what it holds at every size, what happens while a
track is read, and how one scene gives way to the next."""

from __future__ import annotations

import json
import math
import os
import struct
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _tone(seconds: float = 1.0, rate: int = 22050, hz: float = 220.0) -> bytes:
    """A short sine wave as a WAV file."""
    count = int(rate * seconds)
    pcm = b"".join(
        struct.pack("<h", int(9000 * math.sin(2 * math.pi * hz * i / rate)))
        for i in range(count))
    return (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)


def _viewer(qtbot, library: bool = True):
    import attachments
    from attachment_view import AttachmentViewer

    data = _tone()
    item = attachments.Attachment(part="1", name="tone.wav",
                                  content_type="audio/wav", size=len(data),
                                  data=data)
    viewer = AttachmentViewer([item], library=library)
    qtbot.addWidget(viewer)
    viewer.show()
    viewer.list.setCurrentRow(0)
    return viewer


def _open(pane, qapp) -> None:
    """The picture at full height, without waiting for an analysis."""
    from array import array

    import attachment_audio

    frames = [array("f", [0.4] * attachment_audio.BANDS) for _ in range(60)]
    pane.spectrum.set_frames(frames, attachment_audio.RATE)
    pane.spectrum._flow.stop()
    pane.spectrum._target = 1.0
    pane.spectrum._reveal_changed(1.0)
    qapp.processEvents()
    qapp.processEvents()


def _in_pane(pane, widget):
    from PySide6.QtCore import QPoint

    return widget.geometry().translated(
        widget.parentWidget().mapTo(pane, QPoint(0, 0)))


def _crowding(pane) -> list:
    """Everything wrong with how the pane is laid out at its size now."""
    found = []
    holder = pane.visual_holder
    wanted = holder.heightForWidth(holder.width())
    if wanted < 0:
        wanted = holder.minimumSizeHint().height()
    if holder.height() < wanted:
        found.append(f"controls cut short: {holder.height()} of {wanted}")
    seek = pane.wave if pane.wave.isVisible() else pane.position
    line = [w for w in (pane.play, seek, pane.clock, pane.volume)
            if w.isVisible()]
    for one, other in zip(line, line[1:]):
        if _in_pane(pane, one).intersects(_in_pane(pane, other)):
            found.append(f"{type(one).__name__} over {type(other).__name__}")
    from PySide6.QtWidgets import QWidget

    for widget in [*line, *holder.findChildren(QWidget)]:
        if widget.isVisible() and _in_pane(pane, widget).right() > pane.width():
            found.append(f"{type(widget).__name__} past the right edge")
    return found


class TestEveryControlFits:
    @pytest.mark.parametrize("library", [True, False])
    def test_at_the_smallest_the_window_can_be(self, qtbot, qapp, library):
        import visualizers

        viewer = _viewer(qtbot, library)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        _open(pane, qapp)
        viewer.resize(viewer.minimumSize())
        qapp.processEvents()
        problems = []
        for scene in visualizers.SCENES:
            pane.scene_box.setCurrentText(scene.name)
            qapp.processEvents()
            qapp.processEvents()
            problems += [f"{scene.name}: {p}" for p in _crowding(pane)]
        viewer._sweep()
        assert not problems, problems

    def test_the_window_is_never_narrower_than_its_controls(self, qtbot):
        viewer = _viewer(qtbot)
        assert viewer.minimumWidth() >= viewer.minimumSizeHint().width()
        viewer._sweep()

    def test_a_size_given_before_it_is_shown_is_kept(self, qtbot):
        import attachments
        from attachment_view import AttachmentViewer

        data = _tone()
        viewer = AttachmentViewer([attachments.Attachment(
            part="1", name="tone.wav", content_type="audio/wav",
            size=len(data), data=data)], library=True)
        qtbot.addWidget(viewer)
        viewer.resize(990, 700)
        viewer.show()
        assert viewer.size().width() == 990 and viewer.size().height() == 700
        viewer._sweep()


class TestNothingMovesWhileATrackIsRead:
    def test_the_transport_is_one_height_with_slider_or_waveform(self, qtbot,
                                                                 qapp):
        from attachment_view import AudioPane

        pane = AudioPane()
        qtbot.addWidget(pane)
        pane.resize(600, 700)
        pane.show()
        heights = []
        for drawn in (False, True):
            pane._show_scrubber(drawn)
            qapp.processEvents()
            pane.layout().activate()
            heights.append(pane.layout().itemAt(3).geometry().height())
        assert heights[0] == heights[1], heights

    def test_the_picture_keeps_its_height_when_the_analysis_lands(
            self, qtbot, qapp):
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        pane.scene_box.setCurrentText("Music rider")
        _open(pane, qapp)
        viewer.resize(1000, 720)
        qapp.processEvents()
        pane._show_scrubber(False)
        pane.spectrum.set_working(0.4)
        qapp.processEvents()
        qapp.processEvents()
        reading = (pane.spectrum.height(), _in_pane(pane, pane.play).y(),
                   _in_pane(pane, pane.scene_box).y())
        pane.spectrum.set_working(None)
        pane._show_scrubber(True)
        qapp.processEvents()
        qapp.processEvents()
        read = (pane.spectrum.height(), _in_pane(pane, pane.play).y(),
                _in_pane(pane, pane.scene_box).y())
        viewer._sweep()
        assert reading == read

    def test_a_new_track_keeps_the_picture_open(self, qtbot):
        from attachment_widgets import Spectrum

        strip = Spectrum()
        qtbot.addWidget(strip)
        strip._reveal_changed(1.0)
        tall = strip.maximumHeight()
        assert tall > 0
        strip.clear(keep_open=True)
        assert strip._reveal == 1.0 and strip.maximumHeight() == tall
        strip.clear()
        assert strip._reveal == 0.0 and strip.maximumHeight() == 0

    def test_the_viewer_reads_a_new_track_with_the_picture_open(self, qtbot,
                                                                 qapp):
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        _open(pane, qapp)
        tall = pane.spectrum.maximumHeight()
        pane._start_analysis(pane._path)
        assert pane.spectrum._reveal == 1.0
        assert pane.spectrum.maximumHeight() == tall
        viewer._sweep()

    def test_progress_does_not_start_the_opening_again(self, qtbot):
        from PySide6.QtCore import QAbstractAnimation

        from attachment_widgets import Spectrum

        strip = Spectrum()
        qtbot.addWidget(strip)
        strip.reveal()
        flow = strip._flow
        assert flow.state() == QAbstractAnimation.State.Running
        qtbot.wait(120)
        along = flow.currentTime()
        assert along > 0
        strip.set_working(0.3)
        assert flow.currentTime() >= along, "the opening started again"


class TestThePictureTakesTheRoomThereIs:
    @staticmethod
    def _gap(pane) -> int:
        """The space left under the controls."""
        bottom = _in_pane(pane, pane.visual_holder).bottom()
        return pane.height() - pane.layout().contentsMargins().bottom() - bottom

    def test_after_the_window_is_made_wider(self, qtbot, qapp):
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        pane.scene_box.setCurrentText("Music rider")
        _open(pane, qapp)
        viewer.resize(viewer.minimumSize())
        qapp.processEvents()
        # Wider, so the controls take fewer lines.
        viewer.resize(1500, viewer.minimumSize().height())
        qapp.processEvents()
        qapp.processEvents()
        gap = self._gap(pane)
        viewer._sweep()
        assert gap <= 12, f"{gap} pixels left empty under the controls"

    def test_the_share_is_right_as_soon_as_the_window_is_resized(self, qtbot,
                                                                 qapp):
        """Worked out for the new width at once, not one layout later."""
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        pane.scene_box.setCurrentText("Music rider")
        _open(pane, qapp)
        viewer.resize(viewer.minimumSize())
        qapp.processEvents()
        qapp.processEvents()
        viewer.resize(1500, viewer.minimumSize().height())
        at_once = pane.spectrum._budget
        qapp.processEvents()
        qapp.processEvents()
        settled = pane.spectrum._budget
        viewer._sweep()
        assert at_once == settled

    def test_when_the_words_under_the_controls_change_length(self, qtbot,
                                                             qapp):
        """Nothing calls for the share to be worked out again here: the
        layout changing has to be enough."""
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        pane.scene_box.setCurrentText("Music rider")
        _open(pane, qapp)
        viewer.resize(viewer.minimumSize())
        qapp.processEvents()
        problems = []
        for words in ("Short.", " ".join(["A longer line of words."] * 9),
                      "Short again."):
            pane.game_about.setText(words)
            # Until the layout is quiet, not a count of passes: a timer left
            # by an earlier test can land in the middle and cost one more.
            for _ in range(300):
                if not (_crowding(pane) or self._gap(pane) > 12):
                    break
                qtbot.wait(10)
            problems += _crowding(pane)
            if self._gap(pane) > 12:
                problems.append(f"{self._gap(pane)} pixels left empty")
        viewer._sweep()
        assert not problems, problems

    def test_when_a_line_of_controls_comes_and_goes(self, qtbot, qapp):
        viewer = _viewer(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        _open(pane, qapp)
        viewer.resize(1000, 720)
        qapp.processEvents()
        gaps = []
        for scene in ("Music rider", "Equaliser", "Music rider"):
            pane.scene_box.setCurrentText(scene)
            qapp.processEvents()
            qapp.processEvents()
            gaps.append(self._gap(pane))
            assert not _crowding(pane), (scene, _crowding(pane))
        viewer._sweep()
        assert max(gaps) <= 12, gaps


class TestTheProgressBarShows:
    def test_after_a_scene_has_just_been_chosen(self, qtbot):
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers
        from attachment_widgets import Spectrum

        strip = Spectrum()
        qtbot.addWidget(strip)
        strip.set_unbounded(True)
        strip.resize(400, 240)
        strip.set_scene(visualizers.by_name("Rave"))
        strip.set_scene(visualizers.by_name("Music rider"))
        assert strip._fresh < 0.2, "the new scene is not fading in"
        strip._reveal = 1.0
        strip.set_working(0.5)
        image = QImage(400, 240, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        try:
            strip._paint(painter)
        finally:
            painter.end()
        lit = 0
        for y in range(0, 240):
            for x in range(0, 400, 2):
                colour = image.pixelColor(x, y)
                if colour.blue() > 200 and colour.red() > 110:
                    lit += 1
        assert lit > 50, "the bar is not drawn while the track is read"


class TestSceneChanges:
    @staticmethod
    def _strip(qtbot):
        from array import array

        import attachment_audio
        from attachment_widgets import Spectrum

        strip = Spectrum()
        qtbot.addWidget(strip)
        strip.set_frames([array("f", [0.4] * attachment_audio.BANDS)
                          for _ in range(60)], attachment_audio.RATE)
        strip.resize(320, 200)
        return strip

    @staticmethod
    def _frames_to_full(strip, scene) -> int:
        """Frames from choosing ``scene`` until it is at full strength."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QImage, QPainter

        strip.set_scene(scene)
        image = QImage(320, 200, QImage.Format.Format_ARGB32_Premultiplied)
        painter = QPainter(image)
        frames = 0
        try:
            while strip._fresh < 1.0 and frames < 1000:
                strip._tick()
                image.fill(QColor(0, 0, 0))
                strip._paint_scene(painter, QRectF(0, 0, 320, 200))
                frames += 1
        finally:
            painter.end()
        return frames

    def test_cut_changes_at_once(self, qtbot):
        import visualizers

        strip = self._strip(qtbot)
        strip.set_change(0.0)
        strip.set_scene(visualizers.by_name("Rave"))
        strip.set_scene(visualizers.by_name("Music rider"))
        assert strip._fresh == 1.0

    def test_each_length_takes_its_time(self, qtbot, monkeypatch):
        import visualizers
        from attachment_widgets import Spectrum

        strip = self._strip(qtbot)
        # Timed at sixty a second whatever the machine measures.
        monkeypatch.setattr(strip, "_pace", lambda: None)
        strip._timer.setInterval(Spectrum.FRAME_MS)
        scenes = [visualizers.by_name("Rave"), visualizers.by_name("Ambience")]
        for index, (name, seconds) in enumerate(Spectrum.CHANGES):
            strip.set_change(seconds)
            frames = self._frames_to_full(strip, scenes[index % 2])
            if seconds <= 0.0:
                assert frames == 0, name
                continue
            fading = frames - Spectrum.WARM_FRAMES
            expected = seconds / (Spectrum.FRAME_MS / 1000.0)
            assert abs(fading - expected) <= 2, (name, fading, expected)

    def test_the_window_offers_and_remembers_it(self, qtbot):
        from attachment_view import AudioPane
        from attachment_widgets import Spectrum

        pane = AudioPane()
        qtbot.addWidget(pane)
        names = [name for name, _ in Spectrum.CHANGES]
        assert [pane.change_box.itemText(i)
                for i in range(pane.change_box.count())] == names
        assert pane.change_box.currentText() == names[0]
        pane.change_box.setCurrentText("Slow")
        assert pane.spectrum._change == dict(Spectrum.CHANGES)["Slow"]
        again = AudioPane()
        qtbot.addWidget(again)
        assert again.change_box.currentText() == "Slow"
        assert again.spectrum._change == dict(Spectrum.CHANGES)["Slow"]


class TestThePicturePanel:
    def test_it_holds_the_pictures_settings(self, qtbot):
        from attachment_view import AudioPane

        pane = AudioPane()
        qtbot.addWidget(pane)
        panel = pane.picture_panel
        for widget in (pane.shape_box, pane.change_box, pane.strobe_box,
                       pane.strobe_source, pane.sense, pane.flash,
                       pane.timing_slider):
            assert panel.isAncestorOf(widget), widget.accessibleName()

    def test_the_timing_slider_moves_the_picture(self, qtbot):
        from attachment_view import AudioPane

        pane = AudioPane()
        qtbot.addWidget(pane)
        pane.timing_slider.setValue(40)
        assert pane.sync_trim() == 40
        assert pane.timing_said.text() == AudioPane.timing_words(40)
        again = AudioPane()
        qtbot.addWidget(again)
        again._show_timing_note()
        assert again.timing_slider.value() == 40

    def test_it_is_unavailable_while_the_visualiser_is_off(self, qtbot):
        from attachment_view import AudioPane

        pane = AudioPane()
        qtbot.addWidget(pane)
        assert not pane.picture_panel.isEnabled()
        pane.enable_box.setChecked(True)
        assert pane.picture_panel.isEnabled()
        pane.enable_box.setChecked(False)
        assert not pane.picture_panel.isEnabled()


#: A short script run on the real platform: the window at its smallest,
#: reported back as JSON. Shown nowhere on screen.
REAL = textwrap.dedent("""
    import json, math, os, struct, sys
    sys.path.insert(0, {root!r})
    from PySide6.QtCore import QEventLoop, QPoint, QTimer, Qt
    from PySide6.QtWidgets import QApplication, QWidget
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Mail Manager")
    import attachments, visualizers
    from attachment_view import AttachmentViewer

    def spin(seconds):
        loop = QEventLoop()
        QTimer.singleShot(int(seconds * 1000), loop.quit)
        loop.exec()

    rate = 22050
    pcm = b"".join(struct.pack("<h", int(9000 * math.sin(
        2 * math.pi * 220 * i / rate))) for i in range(rate * 2))
    data = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
            + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(pcm)) + pcm)
    viewer = AttachmentViewer([attachments.Attachment(
        part="1", name="tone.wav", content_type="audio/wav",
        size=len(data), data=data)], library=True)
    viewer.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    pane = viewer.audio
    pane.volume.setValue(0)
    pane.enable_box.setChecked(True)
    viewer.show()
    viewer.list.setCurrentRow(0)
    spin(0.5)
    viewer.resize(viewer.minimumSize())
    spin(3.0)

    def box(widget):
        return widget.geometry().translated(
            widget.parentWidget().mapTo(pane, QPoint(0, 0)))

    report = {{"scenes": {{}}}}
    for scene in visualizers.SCENES:
        pane.scene_box.setCurrentText(scene.name)
        spin(0.6)
        holder = pane.visual_holder
        wanted = holder.heightForWidth(holder.width())
        if wanted < 0:
            wanted = holder.minimumSizeHint().height()
        seek = pane.wave if pane.wave.isVisible() else pane.position
        line = [w for w in (pane.play, seek, pane.clock, pane.volume)
                if w.isVisible()]
        overlaps = [type(a).__name__ for a, b in zip(line, line[1:])
                    if box(a).intersects(box(b))]
        past = [type(w).__name__ for w in
                line + holder.findChildren(QWidget)
                if w.isVisible() and box(w).right() > pane.width()]
        report["scenes"][scene.name] = {{
            "picture": pane.spectrum.height(),
            "holder": holder.height(), "wanted": wanted,
            "overlaps": overlaps, "past": past}}
    report["size"] = [viewer.width(), viewer.height()]
    print(json.dumps(report))
    sys.stdout.flush()
    os._exit(0)
""").format(root=str(ROOT))


class TestOnTheRealPlatform:
    """The suite runs on the offscreen platform, whose fonts and controls
    are not the ones people see. This lays the window out on the real one."""

    @pytest.mark.timeout(180)
    def test_the_window_at_its_smallest(self):
        if sys.platform != "darwin":
            pytest.skip("the real platform here is macOS")
        from test_gpu_canvas import real_platform_or_skip

        real_platform_or_skip()
        env = dict(os.environ)
        env.pop("QT_QPA_PLATFORM", None)
        done = subprocess.run([sys.executable, "-c", REAL],
                              capture_output=True, text=True, timeout=170,
                              env=env, cwd=ROOT)
        lines = [line for line in done.stdout.splitlines()
                 if line.startswith("{")]
        assert lines, f"nothing came back (exit {done.returncode}):\n" \
                      f"{done.stderr[-3000:]}"
        report = json.loads(lines[-1])
        from attachment_widgets import Spectrum

        problems = []
        for name, seen in report["scenes"].items():
            if seen["holder"] < seen["wanted"]:
                problems.append(f"{name}: controls cut short")
            if seen["overlaps"]:
                problems.append(f"{name}: overlapping {seen['overlaps']}")
            if seen["past"]:
                problems.append(f"{name}: past the edge {seen['past']}")
            if seen["picture"] < Spectrum.FLOOR:
                problems.append(f"{name}: picture {seen['picture']} tall")
        assert not problems, (report["size"], problems)
