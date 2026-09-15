"""The viewer's newer parts: metadata, cover art, the spectrum, the seek bar.

Every parser here walks bytes a stranger sent, so the tests are mostly about
what happens when those bytes are wrong.
"""

from __future__ import annotations

import struct
import zlib

import pytest

import attachment_meta


def png(width: int = 8, height: int = 6) -> bytes:
    def chunk(tag: bytes, body: bytes) -> bytes:
        piece = tag + body
        return struct.pack(">I", len(body)) + piece + struct.pack(">I", zlib.crc32(piece))

    rows = b"".join(b"\x00" + b"\x20\x40\x80" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def id3(frames: bytes) -> bytes:
    size = len(frames)
    syncsafe = bytes([(size >> 21) & 0x7F, (size >> 14) & 0x7F,
                      (size >> 7) & 0x7F, size & 0x7F])
    return b"ID3\x03\x00\x00" + syncsafe + frames


def text_frame(tag: bytes, value: str) -> bytes:
    body = b"\x00" + value.encode("latin-1", "replace")
    return tag + struct.pack(">I", len(body)) + b"\x00\x00" + body


def apic(image: bytes) -> bytes:
    body = b"\x00" + b"image/png" + b"\x00" + b"\x03" + b"cover" + b"\x00" + image
    return b"APIC" + struct.pack(">I", len(body)) + b"\x00\x00" + body


class TestImageFacts:
    @pytest.mark.parametrize("maker, expected", [
        (lambda: png(64, 48), (64, 48)),
        (lambda: b"GIF89a" + struct.pack("<HH", 30, 20) + b"\x00" * 20, (30, 20)),
    ])
    def test_dimensions_come_off_the_header(self, maker, expected):
        facts = attachment_meta.image_facts(maker())
        assert facts.get("Dimensions") == f"{expected[0]} x {expected[1]} pixels"

    def test_gps_presence_is_reported_because_it_is_a_location(self):
        # A minimal little-endian TIFF header with one GPS IFD pointer.
        entry = struct.pack("<HHII", 0x8825, 4, 1, 26)
        ifd = struct.pack("<H", 1) + entry + struct.pack("<I", 0)
        exif = b"Exif\x00\x00" + b"II*\x00" + struct.pack("<I", 8) + ifd
        facts = attachment_meta.image_facts(b"\xff\xd8\xff\xe1" + exif + b"\x00" * 20)
        assert "Location" in facts

    @pytest.mark.parametrize("junk", [b"", b"\x89PNG", b"\xff\xd8", b"\x00" * 500,
                                      b"\x89PNG\r\n\x1a\n" + b"\xff" * 40])
    def test_broken_images_do_not_raise(self, junk):
        assert isinstance(attachment_meta.image_facts(junk), dict)


class TestAudioTagsAndCoverArt:
    def test_id3_tags_are_read(self):
        data = id3(text_frame(b"TIT2", "A Title")
                   + text_frame(b"TPE1", "An Artist")
                   + text_frame(b"TALB", "An Album"))
        tags, art = attachment_meta.audio_facts(data)
        assert tags.get("Title") == "A Title"
        assert tags.get("Artist") == "An Artist"
        assert tags.get("Album") == "An Album"
        assert art is None

    def test_cover_art_comes_back_as_bytes(self):
        image = png(16, 16)
        tags, art = attachment_meta.audio_facts(id3(apic(image) + text_frame(b"TIT2", "x")))
        assert art is not None
        assert art.startswith(b"\x89PNG\r\n\x1a\n")

    def test_a_huge_cover_is_refused_rather_than_held(self):
        big = b"\x89PNG\r\n\x1a\n" + b"\x00" * (attachment_meta.MAX_ART + 10)
        _tags, art = attachment_meta.audio_facts(id3(apic(big)))
        assert art is None

    @pytest.mark.parametrize("junk", [
        b"ID3", b"ID3\x03\x00\x00", b"ID3\x03\x00\x00\x7f\x7f\x7f\x7f",
        b"ID3\x03\x00\x00\x00\x00\x00\x10" + b"\xff" * 16,
        b"\x00\x00\x00\x18ftypM4A ", b"fLaC" + b"\xff" * 40,
    ])
    def test_broken_audio_metadata_does_not_raise(self, junk):
        tags, art = attachment_meta.audio_facts(junk)
        assert isinstance(tags, dict)

    def test_an_mp4_atom_loop_is_bounded(self):
        """A zero-length atom would spin forever without the guard."""
        data = b"\x00\x00\x00\x18ftypM4A " + b"\x00" * 8 + struct.pack(">I", 0) + b"moov"
        tags, art = attachment_meta.audio_facts(data)
        assert isinstance(tags, dict)


class TestPdfFacts:
    def test_version_and_title(self):
        data = b"%PDF-1.7\n/Title (Quarterly Report)\n/Author (Someone)\n"
        facts = attachment_meta.pdf_facts(data)
        assert facts.get("PDF version") == "1.7"
        assert facts.get("Title") == "Quarterly Report"

    def test_encryption_is_flagged(self):
        assert attachment_meta.pdf_facts(b"%PDF-1.4\n/Encrypt 5 0 R\n").get("Encrypted")

    def test_junk_does_not_raise(self):
        assert isinstance(attachment_meta.pdf_facts(b"\x00\xff" * 300), dict)


class TestTheFactsPanel:
    def test_it_says_when_nothing_is_downloaded(self):
        import attachments

        item = attachments.Attachment(part="1", name="a.pdf",
                                      content_type="application/pdf", size=10)
        rows = dict(attachment_meta.facts_for(item))
        assert rows.get("Contents") == "not downloaded yet"

    def test_it_names_the_mismatch_when_the_type_is_a_lie(self):
        import attachments

        item = attachments.Attachment(
            part="1", name="holiday.png", content_type="image/png",
            size=40, data=b"MZ\x90\x00" + b"\x00" * 36)
        rows = dict(attachment_meta.facts_for(item))
        assert "Actually" in rows
        assert "differs" in rows["Actually"]

    def test_the_checksum_is_there_so_a_file_can_be_verified(self):
        import attachments

        item = attachments.Attachment(part="1", name="a.txt",
                                      content_type="text/plain",
                                      size=5, data=b"hello")
        rows = dict(attachment_meta.facts_for(item))
        assert len(rows["SHA-256"]) == 64


class TestTheSeekBarStaysWhereItIsPut:
    """Clicking flashed to the new place and slid back. That was two bugs.

    A QSlider does not move to where you click, and a media player keeps
    reporting its old position for a moment after a seek. The first version
    of the guard here cleared as soon as one report landed near the target,
    which let the next stale one through - so a quick run of clicks still
    snapped backwards.
    """

    @staticmethod
    def _bar(qtbot):
        from attachment_widgets import SeekBar

        bar = SeekBar()
        qtbot.addWidget(bar)
        bar.setRange(0, 137_000)
        return bar

    def test_a_stale_report_cannot_move_it(self, qtbot):
        bar = self._bar(qtbot)
        bar._request(90_000)
        bar.setValue(90_000)
        bar.report(20_000)                       # in flight from before
        assert bar.value() == 90_000

    def test_the_real_report_is_followed(self, qtbot):
        bar = self._bar(qtbot)
        bar._request(90_000)
        bar.setValue(90_000)
        bar.report(90_120)
        assert abs(bar.value() - 90_120) < 500

    def test_a_stale_report_behind_a_real_one_is_still_dropped(self, qtbot):
        """The exact shape that made rapid clicking unreliable."""
        bar = self._bar(qtbot)
        bar._request(90_000)
        bar.setValue(90_000)
        bar.report(90_100)                       # the seek landed
        bar.report(31_000)                       # one more stale report
        assert bar.value() > 80_000

    def test_fourteen_rapid_seeks_never_snap_back(self, qtbot):
        bar = self._bar(qtbot)
        wrong = 0
        for index in range(14):
            target = int(137_000 * ((index * 7 % 10) / 10.0))
            bar._request(target)
            bar.setValue(target)
            bar.report(max(0, target - 30_000))
            bar.report(target + 120)
            bar.report(max(0, target - 28_000))
            if abs(bar.value() - target) > 800:
                wrong += 1
        assert wrong == 0

    def test_clicking_the_groove_asks_for_that_position(self, qtbot):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        bar = self._bar(qtbot)
        bar.resize(400, 24)
        asked = []
        bar.seeked.connect(asked.append)
        event = QMouseEvent(QMouseEvent.Type.MouseButtonPress,
                            QPointF(300, 12), QPointF(300, 12),
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.NoModifier)
        bar.mousePressEvent(event)
        assert asked, "clicking the groove did nothing"
        assert asked[0] > 137_000 * 0.4, "it did not move towards the click"

    def test_dragging_is_not_overridden_by_the_player(self, qtbot):
        bar = self._bar(qtbot)
        bar._dragging = True
        bar.setValue(50_000)
        bar.report(1_000)
        assert bar.value() == 50_000


class TestTheSpectrum:
    def test_it_draws_nothing_and_costs_nothing_when_idle(self, qtbot):
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        assert not spectrum.ready
        spectrum.set_playing(True)
        assert not spectrum._timer.isActive(), "it started a timer with no data"
        spectrum.grab()          # must not raise

    def test_frames_drive_it_and_clearing_stops_it(self, qtbot):
        from array import array

        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        frames = [array("f", [0.5] * 32) for _ in range(40)]
        spectrum.set_frames(frames, 20)
        assert spectrum.ready
        spectrum.set_position(500)
        spectrum.set_playing(True)
        assert spectrum._timer.isActive()
        spectrum._tick()
        spectrum.grab()
        spectrum.clear()
        assert not spectrum.ready
        assert not spectrum._timer.isActive()

    def test_a_position_past_the_end_does_not_raise(self, qtbot):
        from array import array

        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_frames([array("f", [0.2] * 32)], 20)
        spectrum.set_position(999_999)
        spectrum._tick()
        spectrum.grab()


class TestTheAnalysis:
    def test_silence_produces_no_energy(self):
        from array import array

        import attachment_audio

        frames = attachment_audio.analyse(array("h", [0] * 22050 * 2), 22050, 1)
        assert frames
        assert max(max(row) for row in frames) < 0.05

    def test_a_tone_lands_in_the_right_part_of_the_spectrum(self):
        import math
        from array import array

        import attachment_audio

        rate = 22050
        low = array("h", [int(12000 * math.sin(2 * math.pi * 120 * i / rate))
                          for i in range(rate * 2)])
        frames = attachment_audio.analyse(low, rate, 1)
        middle = frames[len(frames) // 2]
        assert sum(middle[:8]) > sum(middle[-8:]), "a bass tone lit the treble"

    def test_quiet_and_loud_both_fill_the_strip(self):
        import math
        from array import array

        import attachment_audio

        rate = 22050
        peaks = []
        for amplitude in (700, 24000):
            samples = array("h", [int(amplitude * math.sin(2 * math.pi * 300 * i / rate))
                                  for i in range(rate * 2)])
            frames = attachment_audio.analyse(samples, rate, 1)
            peaks.append(max(max(row) for row in frames))
        assert all(p > 0.7 for p in peaks), peaks

    @pytest.mark.parametrize("bad", [
        ("h", [], 22050), ("h", [1] * 10, 22050), ("h", [1] * 5000, 0),
    ])
    def test_nothing_usable_returns_nothing(self, bad):
        from array import array

        import attachment_audio

        kind, values, rate = bad
        assert attachment_audio.analyse(array(kind, values), rate, 1) == []


class TestTheViewerDoesNotTrapTheApp:
    """It was modal, so Quit did nothing while it was up."""

    @staticmethod
    def _one():
        import attachments

        return attachments.Attachment(part="1", name="a.txt",
                                      content_type="text/plain",
                                      size=5, data=b"hello")

    def test_it_is_a_window_not_an_application_modal(self, qtbot):
        from PySide6.QtCore import Qt

        from attachment_view import AttachmentViewer

        viewer = AttachmentViewer([self._one()])
        qtbot.addWidget(viewer)
        assert viewer.windowModality() == Qt.WindowModality.NonModal, (
            "an application-modal viewer swallows Quit")
        viewer._sweep()

    def test_the_window_closes_it_on_the_way_out(self, qtbot):
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore(), demo=True)
        qtbot.addWidget(window)
        window._load_demo_data()
        assert hasattr(window, "_close_attachment_window")
        window._close_attachment_window()      # safe with nothing open
        rows = [i for i, item in enumerate(window.model.items)
                if item.email.attachments]
        window._open_attachments(rows[0])
        assert getattr(window, "_attachment_window", None) is not None
        window._close_attachment_window()
        assert getattr(window, "_attachment_window", None) is None

    def test_fetching_happens_off_the_interface_thread(self):
        """Selecting a row used to freeze the window for a second or two."""
        import inspect

        import attachment_view

        source = inspect.getsource(attachment_view.AttachmentViewer)
        assert "_start_fetch" in source
        assert "QApplication.processEvents()" not in source, (
            "pumping events inside a click handler is the freeze, not a fix")
        assert "_FetchThread" in inspect.getsource(attachment_view)


class TestTheSpectrumArrivesAndLeaves:
    """Hidden until play, breathing when paused, gone after a while."""

    @staticmethod
    def _loaded(qtbot):
        from array import array

        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_frames([array("f", [0.4] * 32) for _ in range(120)], 20)
        return spectrum

    def test_it_is_not_there_before_anything_plays(self, qtbot):
        spectrum = self._loaded(qtbot)
        assert spectrum.maximumHeight() == 0
        assert spectrum._reveal == 0.0

    def test_playing_brings_it_in(self, qtbot):
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        assert spectrum._timer.isActive()
        assert spectrum._flow.state() != 0 or spectrum._reveal > 0

    def test_pausing_keeps_it_moving_and_starts_the_countdown(self, qtbot):
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        spectrum.set_playing(False)
        assert spectrum._idling, "a paused spectrum should breathe, not freeze"
        assert spectrum._away.isActive()
        assert spectrum._timer.isActive()
        spectrum._tick()
        spectrum.grab()

    def test_the_countdown_is_thirty_seconds(self, qtbot):
        spectrum = self._loaded(qtbot)
        assert spectrum.IDLE_SECONDS == 30
        assert spectrum._away.interval() == 30_000

    def test_concealing_stops_everything(self, qtbot):
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        spectrum.conceal()
        spectrum._reveal_changed(0.0)
        assert spectrum.maximumHeight() == 0
        assert not spectrum._timer.isActive()
        assert not spectrum._away.isActive()

    def test_the_idle_row_actually_moves(self, qtbot):
        spectrum = self._loaded(qtbot)
        spectrum._idling = True
        first = list(spectrum._idle_row())
        spectrum._drift += 1.0
        second = list(spectrum._idle_row())
        assert first != second

    def test_four_bands_drive_four_things(self, qtbot):
        """Bass, vocals, synths and cymbals are kept apart on purpose."""
        from attachment_widgets import Spectrum

        spans = [Spectrum.BASS, Spectrum.MID, Spectrum.SYNTH, Spectrum.HIGH]
        assert len({tuple(s) for s in spans}) == 4
        for lower, upper in zip(spans, spans[1:]):
            assert lower[1] <= upper[0], "the bands overlap"


class TestTheControlsExplainThemselves:
    """A first-time reader should not have to guess what a button does."""

    @staticmethod
    def _viewer(qtbot):
        import attachments
        from attachment_view import AttachmentViewer

        found = [attachments.Attachment(part="1", name="a.txt",
                                        content_type="text/plain",
                                        size=5, data=b"hello")]
        viewer = AttachmentViewer(found)
        qtbot.addWidget(viewer)
        return viewer

    def test_no_button_is_a_bare_symbol(self, qtbot):
        from PySide6.QtWidgets import QAbstractButton

        viewer = self._viewer(qtbot)
        bare = [b.text() for b in viewer.findChildren(QAbstractButton)
                if b.text().strip() and len(b.text().strip()) <= 2
                and not b.toolTip()]
        assert not bare, f"unlabelled controls: {bare}"
        viewer._sweep()

    def test_every_dropdown_fits_its_longest_option(self, qtbot):
        from PySide6.QtWidgets import QComboBox

        viewer = self._viewer(qtbot)
        viewer.resize(980, 640)
        for box in viewer.findChildren(QComboBox):
            options = [box.itemText(i) for i in range(box.count())]
            if not options:
                continue
            needed = max(box.fontMetrics().horizontalAdvance(o) for o in options)
            assert box.minimumWidth() >= needed + 20, (
                f"{options} would be clipped")
        viewer._sweep()

    def test_the_window_says_what_it_is_for(self, qtbot):
        viewer = self._viewer(qtbot)
        hint = viewer.hint.text().lower()
        assert "click" in hint
        assert "never" in hint or "nothing" in hint
        viewer._sweep()


class TestOneConnectionMeansOneRequest:
    """Two threads on one IMAP socket interleave inside TLS.

    The server answers "bad record mac" and drops the connection, which is
    not a race that fails quietly: every fetch after it fails too. Prefetch
    caused it by starting a second thread while the first was mid-request.
    """

    def test_the_source_serialises_its_fetches(self):
        import threading
        import time

        from workers import AttachmentSource

        overlapping = []
        inside = []
        lock = threading.Lock()

        class Engine:
            def fetch_part(self, uid, part, encoding=""):
                with lock:
                    inside.append(part)
                    if len(inside) > 1:
                        overlapping.append(tuple(inside))
                time.sleep(0.03)
                with lock:
                    inside.remove(part)
                return b"x" * 10

        source = AttachmentSource(Engine(), "1", [])

        class Item:
            def __init__(self, part):
                self.part = part
                self.encoding = ""

        threads = [threading.Thread(target=source.fetch, args=(Item(str(i)),))
                   for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not overlapping, f"requests overlapped: {overlapping}"

    def test_the_viewer_runs_one_worker_at_a_time(self, qtbot):
        import attachments
        from attachment_view import AttachmentViewer

        found = [attachments.Attachment(part=str(i), name=f"f{i}.bin",
                                        content_type="application/octet-stream",
                                        size=10)
                 for i in range(4)]
        viewer = AttachmentViewer(found, fetch=lambda item: b"x" * 10)
        qtbot.addWidget(viewer)
        viewer._queue = [1, 2, 3]
        viewer._workers = {9: object(), 8: object(), 7: object()}   # lanes full
        viewer._pump()
        assert viewer._queue == [1, 2, 3], "it started more than the pool holds"
        viewer._sweep()

    def test_what_is_being_looked_at_goes_to_the_front(self, qtbot):
        import attachments
        from attachment_view import AttachmentViewer

        found = [attachments.Attachment(part=str(i), name=f"f{i}.bin",
                                        content_type="application/octet-stream",
                                        size=10)
                 for i in range(4)]
        viewer = AttachmentViewer(found, fetch=lambda item: b"x" * 10)
        qtbot.addWidget(viewer)
        viewer._workers = {9: object(), 8: object(), 7: object()}   # lanes full
        viewer._queue = []
        viewer._start_fetch(3, found[3], then_show=False)
        viewer._start_fetch(2, found[2], then_show=True)
        assert viewer._queue[0] == 2, "the selected row waited behind a prefetch"
        viewer._sweep()


class TestPlayBeforeTheAnalysisArrives:
    """Analysis finishes a moment after playback starts."""

    def test_the_request_to_appear_is_remembered(self, qtbot):
        from array import array

        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_playing(True)              # nothing to show yet
        assert spectrum.maximumHeight() == 0
        assert spectrum._wanted
        spectrum.set_frames([array("f", [0.4] * 32) for _ in range(60)], 20)
        assert spectrum._timer.isActive(), "the spectrum never appeared"

    def test_frames_arriving_after_a_pause_do_not_force_it_open(self, qtbot):
        from array import array

        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_playing(True)
        spectrum.set_playing(False)
        spectrum.set_frames([array("f", [0.4] * 32) for _ in range(60)], 20)
        assert spectrum.maximumHeight() == 0


class TestTheSpectrumGetsRoomToDrawIn:
    """It appeared to work and drew nothing, twice.

    Animating only the maximum height leaves the minimum at zero and the
    size hint at -1, so a layout hands the widget whatever is spare - which
    in a full pane is nothing. Tests that resized it by hand passed while
    the real window showed an empty strip.
    """

    @staticmethod
    def _in_a_layout(qtbot):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from attachment_widgets import Spectrum

        host = QWidget()
        qtbot.addWidget(host)
        layout = QVBoxLayout(host)
        layout.addWidget(QLabel("above"))
        spectrum = Spectrum()
        layout.addWidget(spectrum)
        layout.addWidget(QLabel("below"))
        layout.addStretch(1)
        host.resize(900, 420)
        host.show()
        return host, spectrum

    def test_it_takes_no_room_before_anything_plays(self, qtbot):
        _host, spectrum = self._in_a_layout(qtbot)
        assert spectrum.height() == 0

    def test_it_takes_its_full_height_once_revealed(self, qtbot):
        from array import array

        _host, spectrum = self._in_a_layout(qtbot)
        spectrum.set_frames([array("f", [0.5] * 32) for _ in range(60)], 20)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        assert spectrum.height() == spectrum.HEIGHT, (
            "the layout gave it a height its own hint did not ask for")
        assert spectrum.sizeHint().height() == spectrum.HEIGHT

    def test_it_grows_part_way_through_the_animation(self, qtbot):
        from array import array

        _host, spectrum = self._in_a_layout(qtbot)
        spectrum.set_frames([array("f", [0.5] * 32) for _ in range(60)], 20)
        spectrum._reveal_changed(0.5)
        assert 0 < spectrum.height() < spectrum.HEIGHT

    def test_it_gives_the_room_back(self, qtbot):
        from array import array

        _host, spectrum = self._in_a_layout(qtbot)
        spectrum.set_frames([array("f", [0.5] * 32) for _ in range(60)], 20)
        spectrum._reveal_changed(1.0)
        spectrum._reveal_changed(0.0)
        assert spectrum.height() == 0

    def test_it_actually_paints_something(self, qtbot):
        """A strip with height and nothing in it is the same bug wearing a hat."""
        from array import array

        _host, spectrum = self._in_a_layout(qtbot)
        frames = [array("f", [0.2 + 0.7 * ((i + j) % 7) / 7 for j in range(32)])
                  for i in range(60)]
        spectrum.set_frames(frames, 20)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        spectrum.set_position(500)
        for _ in range(3):
            spectrum._tick()
        image = spectrum.grab().toImage()
        colours = {image.pixel(x, y)
                   for x in range(0, image.width(), 11)
                   for y in range(0, image.height(), 11)}
        assert len(colours) > 8, f"only {len(colours)} colours: nothing was drawn"

    def test_the_audio_pane_reveals_it_on_play(self, qtbot):
        """End to end: a real WAV, decoded, analysed, revealed."""
        import math
        import struct

        import attachments
        from attachment_view import AttachmentViewer

        rate, seconds = 22050, 3
        pcm = b"".join(
            struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * i / rate)))
            for i in range(rate * seconds))
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)
        item = attachments.Attachment(part="1", name="tone.wav",
                                      content_type="audio/wav",
                                      size=len(wav), data=wav)
        viewer = AttachmentViewer([item])
        qtbot.addWidget(viewer)
        viewer.resize(960, 680)
        viewer.show()
        viewer.list.setCurrentRow(0)
        spectrum = viewer.audio.spectrum
        assert spectrum.height() == 0, "it was visible before anything played"

        viewer.audio._toggle()
        qtbot.waitUntil(lambda: spectrum.ready and spectrum.height() > 40,
                        timeout=30_000)
        assert spectrum._frames
        viewer.audio.stop()
        viewer._sweep()


class TestTheBandsAreARealEqualiser:
    """Third-octave centres, so a band means a frequency."""

    def test_the_centres_are_standard_third_octave(self):
        import attachment_audio

        centres = attachment_audio.CENTRES
        assert centres[0] == 50 and centres[-1] == 10000
        for lower, upper in zip(centres, centres[1:]):
            ratio = upper / lower
            assert 1.18 < ratio < 1.32, f"{lower}->{upper} is not a third octave"

    def test_nothing_is_offered_above_nyquist(self):
        """The analysis decodes at 22 kHz, so 11 kHz is the ceiling."""
        import attachment_audio

        assert max(attachment_audio.CENTRES) <= 11025

    @pytest.mark.parametrize("hertz", [63, 125, 250, 500, 1000, 2000, 4000, 8000])
    def test_a_tone_lands_in_its_own_band(self, hertz):
        import math
        from array import array

        import attachment_audio

        rate = 22050
        samples = array("h", [int(14000 * math.sin(2 * math.pi * hertz * i / rate))
                              for i in range(rate * 2)])
        frames = attachment_audio.analyse(samples, rate, 1)
        row = frames[len(frames) // 2]
        loudest = attachment_audio.CENTRES[max(range(len(row)), key=lambda i: row[i])]
        assert abs(math.log2(loudest / hertz)) < 0.2, (
            f"{hertz} Hz showed up at {loudest} Hz")

    def test_the_scale_is_decibels_not_amplitude(self):
        """Halving amplitude should cost about 6 dB, not half the bar."""
        import math
        from array import array

        import attachment_audio

        rate = 22050
        heights = []
        for amplitude in (16000, 8000):
            samples = array("h", [int(amplitude * math.sin(2 * math.pi * 500 * i / rate))
                                  for i in range(rate * 2)])
            frames = attachment_audio.analyse(samples, rate, 1)
            heights.append(max(max(row) for row in frames))
        # Normalisation pins the loudest to about the same place, which is the
        # point: the shape is what differs, not the ceiling.
        assert all(h > 0.7 for h in heights)


class TestEveryScenePaints:
    @staticmethod
    def _spectrum(qtbot):
        from array import array

        import attachment_audio
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        bands = attachment_audio.BANDS
        frames = [array("f", [0.15 + 0.8 * ((i + j) % 7) / 7 for j in range(bands)])
                  for i in range(90)]
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.set_labels([str(c) for c in attachment_audio.CENTRES])
        spectrum.resize(760, 240)
        spectrum._reveal_changed(1.0)
        spectrum.set_position(1000)
        return spectrum

    @pytest.mark.parametrize("index", range(4))
    def test_it_draws_something(self, qtbot, index):
        import visualizers

        spectrum = self._spectrum(qtbot)
        spectrum.set_scene(visualizers.SCENES[index])
        for _ in range(4):
            spectrum._tick()
        image = spectrum.grab().toImage()
        colours = {image.pixel(x, y)
                   for x in range(0, image.width(), 17)
                   for y in range(0, image.height(), 17)}
        assert len(colours) > 8, f"{visualizers.SCENES[index].name} drew nothing"

    def test_every_scene_has_a_name_and_a_description(self):
        import visualizers

        for scene in visualizers.SCENES:
            assert scene.name and scene.name != "scene"
            assert scene.blurb and scene.blurb != "a scene"
        names = [s.name for s in visualizers.SCENES]
        assert len(set(names)) == len(names)

    def test_an_unknown_name_falls_back_rather_than_raising(self):
        import visualizers

        assert visualizers.by_name("nothing like this") is visualizers.SCENES[0]

    def test_strobe_is_off_until_asked_for(self, qtbot):
        spectrum = self._spectrum(qtbot)
        assert not spectrum._state.strobe
        spectrum.set_strobe(True)
        assert spectrum._state.strobe
        spectrum._state.hit = 1.0
        spectrum.grab()          # must not raise with the flash on


class TestItFollowsThePlayerRatherThanWaiting:
    """positionChanged does not fire until the player produces samples."""

    @staticmethod
    def _spectrum(qtbot):
        from array import array

        import attachment_audio
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        frames = [array("f", [0.1 + 0.85 * ((i * 3 + j) % 9) / 9
                              for j in range(attachment_audio.BANDS)])
                  for i in range(200)]
        spectrum.set_frames(frames, attachment_audio.RATE)
        spectrum.resize(600, 240)
        spectrum._reveal_changed(1.0)
        return spectrum

    def test_without_a_source_it_sits_still(self, qtbot):
        spectrum = self._spectrum(qtbot)
        spectrum._tick()
        before = list(spectrum._level)
        for _ in range(20):
            spectrum._tick()
        moved = sum(1 for a, b in zip(before, spectrum._level) if abs(a - b) > 0.001)
        assert moved == 0

    def test_with_one_it_moves_from_the_first_frame(self, qtbot):
        spectrum = self._spectrum(qtbot)
        clock = {"at": 0}

        def position():
            clock["at"] += 120
            return clock["at"]

        spectrum.follow(position)
        spectrum._tick()
        before = list(spectrum._level)
        for _ in range(20):
            spectrum._tick()
        moved = sum(1 for a, b in zip(before, spectrum._level) if abs(a - b) > 0.001)
        assert moved > len(before) // 3, "the display did not follow the player"

    def test_a_broken_source_does_not_stop_it(self, qtbot):
        spectrum = self._spectrum(qtbot)
        spectrum.follow(lambda: 1 / 0)
        spectrum._tick()          # must not raise


class TestFullScreenGivesTheWidgetBack:
    def test_it_borrows_and_returns_the_spectrum(self, qtbot):
        from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

        from attachment_widgets import FullScreenSpectrum, Spectrum

        host = QWidget()
        qtbot.addWidget(host)
        layout = QVBoxLayout(host)
        layout.addWidget(QLabel("above"))
        spectrum = Spectrum()
        layout.addWidget(spectrum)
        host.resize(800, 400)
        host.show()
        spectrum._reveal_changed(1.0)
        before = spectrum.maximumHeight()

        class Owner:
            _full = None

        owner = Owner()
        full = FullScreenSpectrum(spectrum, owner)
        qtbot.addWidget(full)
        assert spectrum.parentWidget() is not host
        full.close()
        assert spectrum.parentWidget() is host, "the spectrum did not come back"
        assert spectrum.maximumHeight() == before
        assert owner._full is None
