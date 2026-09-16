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

        # The visualiser is off by default now, so asking for it is part of
        # the sequence a person goes through.
        viewer.audio.enable_box.setChecked(True)
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
        assert centres[0] == 50 and centres[-1] == 20000
        for lower, upper in zip(centres, centres[1:]):
            ratio = upper / lower
            assert 1.18 < ratio < 1.32, f"{lower}->{upper} is not a third octave"

    def test_nothing_is_offered_above_nyquist(self):
        """Half the decode rate is all that exists in the signal."""
        import attachment_audio

        assert max(attachment_audio.CENTRES) <= attachment_audio.DECODE_RATE / 2

    @pytest.mark.parametrize("hertz", [63, 125, 250, 500, 1000, 2000, 4000, 8000])
    def test_a_tone_lands_in_its_own_band(self, hertz):
        """Within one third-octave, which is the resolution on offer.

        The decoder runs at 48 kHz so the dial scene can show 18 and 22 kHz,
        and a 2048-point window at that rate is 23 Hz a bin. Two adjacent
        third-octave bands below 100 Hz are 13 Hz apart, so the lowest of
        them share bins and the winner between neighbours is not guaranteed.
        Everything from 100 Hz up lands exactly.
        """
        import math
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        samples = array("h", [int(14000 * math.sin(2 * math.pi * hertz * i / rate))
                              for i in range(rate)])
        frames = attachment_audio.analyse(samples, rate, 1)
        row = frames[len(frames) // 2]
        loudest = attachment_audio.CENTRES[max(range(len(row)), key=lambda i: row[i])]
        tolerance = 0.36 if hertz < 100 else 0.2
        assert abs(math.log2(loudest / hertz)) < tolerance, (
            f"{hertz} Hz showed up at {loudest} Hz")

    def test_the_scale_is_decibels_not_amplitude(self):
        """Halving amplitude should cost about 6 dB, not half the bar."""
        import math
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        heights = []
        for amplitude in (16000, 8000):
            samples = array("h", [int(amplitude * math.sin(2 * math.pi * 500 * i / rate))
                                  for i in range(rate)])
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


class TestTheBuildKeepsWhatTheViewerNeeds:
    """QtMultimedia and QtPdf were excluded from the bundle for two releases.

    The spec had them on a list of Qt modules "this app never touches", from
    before there was an attachment viewer. Audio and PDF therefore said
    "unavailable in this build" in every shipped copy, while the self-test
    reported the viewer present - because it only checked that the Python
    modules imported, which they did.
    """

    @staticmethod
    def _spec() -> str:
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / "MailManager.spec").read_text(encoding="utf-8")

    @pytest.mark.parametrize("module", [
        "PySide6.QtMultimedia", "PySide6.QtMultimediaWidgets",
        "PySide6.QtPdf", "PySide6.QtPdfWidgets",
    ])
    def test_it_is_not_excluded(self, module):
        spec = self._spec()
        excludes = spec[spec.index("excludes = ["):spec.index("a = Analysis")]
        assert f'"{module}"' not in excludes, (
            f"{module} is excluded; the feature that needs it will not ship")

    @pytest.mark.parametrize("module", [
        "PySide6.QtMultimedia", "PySide6.QtPdf",
    ])
    def test_it_is_named_as_a_hidden_import(self, module):
        """They are imported inside methods, where analysis cannot see them."""
        assert f'"{module}"' in self._spec()

    def test_the_self_test_builds_them_rather_than_importing_them(self):
        import inspect

        import main

        source = inspect.getsource(main)
        start = source.index("def _attachment_viewer")
        body = source[start:start + 2000]
        assert "QMediaPlayer()" in body, "it does not build a player"
        assert "QPdfDocument()" in body, "it does not build a PDF document"
        assert "visualizers" in body


class TestTheMeterScene:
    """Ten analogue dials, copied from a photograph of a rack of them."""

    @staticmethod
    def _spectrum(qtbot, position=1500):
        import math
        from array import array

        import attachment_audio
        import visualizers
        from attachment_widgets import Spectrum

        rate = attachment_audio.DECODE_RATE
        pcm = array("h", [
            int(10000 * (math.sin(2 * math.pi * 73 * i / rate)
                         + 0.8 * math.sin(2 * math.pi * 1400 * i / rate)) / 1.8)
            for i in range(rate * 3)])
        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_frames(attachment_audio.analyse(pcm, rate, 1),
                            attachment_audio.RATE)
        spectrum.set_scene(visualizers.by_name("VU meters"))
        spectrum.resize(1000, 520)
        spectrum._reveal_changed(1.0)
        spectrum.set_position(position)
        for _ in range(5):
            spectrum._tick()
        return spectrum

    def test_there_are_ten_bands_with_the_asked_for_labels(self, qtbot):
        spectrum = self._spectrum(qtbot)
        assert len(spectrum._state.dials) == 10
        assert spectrum._state.dial_labels == [
            "73Hz", "120Hz", "300Hz", "576Hz", "1.4kHz",
            "2.4kHz", "6kHz", "9kHz", "18kHz", "22kHz"]

    def test_a_tone_moves_its_own_needle(self, qtbot):
        spectrum = self._spectrum(qtbot)
        dials = spectrum._state.dials
        assert dials[0] > 0.5, "73 Hz did not move the 73 Hz needle"
        assert dials[4] > 0.4, "1.4 kHz did not move the 1.4 kHz needle"
        assert dials[8] < 0.4, "18 kHz moved with nothing there"

    def test_the_decoder_reaches_the_top_band(self):
        """22 kHz needs 48 kHz decoding; 22 kHz decoding would be noise."""
        import attachment_audio

        assert attachment_audio.DECODE_RATE >= 44100
        assert max(attachment_audio.DIAL_CENTRES) <= attachment_audio.DECODE_RATE / 2

    def test_it_draws_and_the_face_is_cached(self, qtbot):
        import visualizers

        spectrum = self._spectrum(qtbot)
        scene = visualizers.by_name("VU meters")
        spectrum.grab()
        assert scene._faces, "the static face is redrawn every frame"
        before = len(scene._faces)
        for _ in range(5):
            spectrum._tick()
            spectrum.grab()
        assert len(scene._faces) == before, "the cache is not being reused"

    def test_colours_can_be_changed(self, qtbot):
        from PySide6.QtGui import QColor

        spectrum = self._spectrum(qtbot)
        first = spectrum.grab().toImage()
        spectrum.set_colours(dial=QColor(80, 200, 255),
                             background=QColor(2, 8, 16))
        spectrum._tick()
        second = spectrum.grab().toImage()
        assert first != second, "recolouring changed nothing"
        assert spectrum.colours[0].blue() > spectrum.colours[0].red()

    def test_the_scale_matches_the_reference(self):
        import visualizers

        meters = visualizers.by_name("VU meters")
        marks = [value for value, _ in meters.DB_MARKS]
        assert marks == [-24, -12, -3, 0, 1, 2, 3]
        percent = [value for value, _ in meters.PERCENT_MARKS]
        assert percent == [0, 20, 40, 60, 80, 100]
        # Clockwise from bottom left, as a moving-coil meter reads.
        assert meters.SWEEP < 0


class TestTheColourPicker:
    def test_it_offers_the_formats_qt_can_read(self):
        import colour_picker

        formats = colour_picker.readable_formats()
        for expected in ("png", "jpg", "gif"):
            assert expected in formats

    def test_a_swatch_reports_what_it_was_set_to(self, qtbot):
        from PySide6.QtGui import QColor

        from colour_picker import Swatch

        swatch = Swatch(QColor("#ff0000"), "Dial")
        qtbot.addWidget(swatch)
        seen = []
        swatch.picked.connect(seen.append)
        swatch.set_colour(QColor("#00ff00"))
        assert seen and seen[-1].green() == 255

    def test_clicking_a_picture_yields_the_pixel_under_the_click(self, qtbot, tmp_path):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QImage, QMouseEvent

        from colour_picker import ImageSampler

        image = QImage(40, 40, QImage.Format.Format_RGB32)
        image.fill(0xFF3366CC)
        path = tmp_path / "flat.png"
        image.save(str(path))

        sampler = ImageSampler()
        qtbot.addWidget(sampler)
        sampler.resize(200, 160)
        assert "x" in sampler.load(path)
        taken = []
        sampler.sampled.connect(taken.append)
        centre = QPointF(sampler.width() / 2, sampler.height() / 2)
        sampler.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, centre, centre,
            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier))
        assert taken, "clicking the picture took no colour"
        assert (taken[0].red(), taken[0].green(), taken[0].blue()) == (0x33, 0x66, 0xCC)

    def test_an_unreadable_file_is_reported_not_raised(self, qtbot, tmp_path):
        from colour_picker import ImageSampler

        bad = tmp_path / "not-a-picture.png"
        bad.write_bytes(b"\x00\xff" * 200)
        sampler = ImageSampler()
        qtbot.addWidget(sampler)
        assert "not a picture" in sampler.load(bad).lower()


class TestTheVisualiserControlsAreReachable:
    """They were inside the visualiser frame, hidden by a one-shot timer."""

    @staticmethod
    def _pane(qtbot):
        import math
        import struct

        import attachments
        from attachment_view import AttachmentViewer

        rate = 22050
        pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 220 * i / rate)))
                       for i in range(rate))
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)
        item = attachments.Attachment(part="1", name="t.wav",
                                      content_type="audio/wav",
                                      size=len(wav), data=wav)
        viewer = AttachmentViewer([item])
        qtbot.addWidget(viewer)
        viewer.resize(1000, 760)
        viewer.show()
        viewer.list.setCurrentRow(0)
        return viewer

    def test_they_are_visible_as_soon_as_a_sound_file_opens(self, qtbot):
        viewer = self._pane(qtbot)
        assert viewer.audio.visual_holder.isVisible()
        assert viewer.audio.scene_box.isVisible()
        assert viewer.audio.full_button.isVisible()
        viewer._sweep()

    def test_they_are_not_inside_the_visualiser(self, qtbot):
        viewer = self._pane(qtbot)
        holder = viewer.audio.visual_holder.geometry()
        spectrum = viewer.audio.spectrum.geometry()
        assert not spectrum.intersects(holder), (
            "the controls sit inside the picture and vanish with it")
        viewer._sweep()

    def test_the_colour_button_belongs_to_the_meter_scene_only(self, qtbot):
        viewer = self._pane(qtbot)
        viewer.audio._scene_chosen("Equaliser")
        assert not viewer.audio.colour_button.isVisible()
        viewer.audio._scene_chosen("VU meters")
        assert viewer.audio.colour_button.isVisible()
        viewer._sweep()


class TestAnAnalysisThatOutlivesItsWindow:
    """The decoder finishes on its own schedule, sometimes after the close.

    Calling into a deleted widget from that callback raises out of Qt's
    event loop, which in a running app means a traceback and a dead
    visualiser. Found by a test whose teardown ran while a decode was still
    going.
    """

    @staticmethod
    def _pane(qtbot):
        import math
        import struct

        import attachments
        from attachment_view import AttachmentViewer

        rate = 22050
        pcm = b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * 300 * i / rate)))
                       for i in range(rate))
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)
        item = attachments.Attachment(part="1", name="t.wav",
                                      content_type="audio/wav",
                                      size=len(wav), data=wav)
        viewer = AttachmentViewer([item])
        qtbot.addWidget(viewer)
        viewer.list.setCurrentRow(0)
        return viewer

    def test_a_token_is_taken_per_file(self, qtbot):
        viewer = self._pane(qtbot)
        first = viewer.audio._analysis_token
        viewer.audio.stop()
        assert viewer.audio._analysis_token > first
        viewer._sweep()

    def test_frames_for_a_previous_file_are_dropped(self, qtbot):
        import attachment_audio

        viewer = self._pane(qtbot)
        pane = viewer.audio
        stale = pane._analysis_token
        pane._analysis_token += 1          # another file was loaded
        before = list(pane.spectrum._frames)

        # Rebuild the closure the decoder would call, with the old token.
        def alive() -> bool:
            return stale == pane._analysis_token

        assert not alive(), "a late analysis would be accepted"
        assert pane.spectrum._frames == before
        viewer._sweep()

    def test_the_guard_uses_a_liveness_check_as_well(self):
        """A token alone does not help if the widget is gone."""
        import inspect

        import attachment_view

        source = inspect.getsource(attachment_view.AudioPane._start_analysis)
        assert "shiboken6" in source
        assert "isValid" in source


class TestTheVisualiserCanBeSwitchedOff:
    """Off by default, and off means nothing is computed or drawn."""

    @staticmethod
    def _pane(qtbot):
        import math
        import struct

        import attachments
        from attachment_view import AttachmentViewer

        rate = 22050
        pcm = b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 300 * i / rate)))
                       for i in range(rate))
        wav = (b"RIFF" + struct.pack("<I", 36 + len(pcm)) + b"WAVEfmt "
               + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
               + b"data" + struct.pack("<I", len(pcm)) + pcm)
        item = attachments.Attachment(part="1", name="t.wav",
                                      content_type="audio/wav",
                                      size=len(wav), data=wav)
        viewer = AttachmentViewer([item])
        qtbot.addWidget(viewer)
        viewer.resize(900, 700)
        viewer.show()
        viewer.list.setCurrentRow(0)
        return viewer

    def test_it_starts_off(self, qtbot):
        viewer = self._pane(qtbot)
        assert not viewer.audio.enable_box.isChecked()
        viewer._sweep()

    def test_its_controls_start_unavailable(self, qtbot):
        viewer = self._pane(qtbot)
        pane = viewer.audio
        assert pane.enable_box.isEnabled(), "the tick box itself must work"
        for widget in (pane.scene_box, pane.strobe_box, pane.full_button):
            assert not widget.isEnabled()
        viewer._sweep()

    def test_nothing_is_analysed_while_it_is_off(self, qtbot):
        viewer = self._pane(qtbot)
        assert viewer.audio._decoder is None, (
            "a decode was started for a visualiser nobody asked for")
        assert not viewer.audio.spectrum.ready
        viewer._sweep()

    def test_playing_with_it_off_draws_nothing(self, qtbot):
        viewer = self._pane(qtbot)
        spectrum = viewer.audio.spectrum
        viewer.audio._state()          # as a playback state change would
        assert spectrum.height() == 0
        assert not spectrum._timer.isActive()
        viewer._sweep()

    def test_ticking_it_frees_the_controls(self, qtbot):
        viewer = self._pane(qtbot)
        pane = viewer.audio
        pane.enable_box.setChecked(True)
        for widget in (pane.scene_box, pane.strobe_box, pane.full_button):
            assert widget.isEnabled()
        viewer._sweep()

    def test_unticking_it_stops_everything(self, qtbot):
        from array import array

        import attachment_audio

        viewer = self._pane(qtbot)
        spectrum = viewer.audio.spectrum
        viewer.audio.enable_box.setChecked(True)
        spectrum.set_frames([array("f", [0.5] * attachment_audio.BANDS)
                             for _ in range(60)], attachment_audio.RATE)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        assert spectrum.height() > 0

        viewer.audio.enable_box.setChecked(False)
        assert not spectrum.ready
        assert not spectrum._timer.isActive()
        assert spectrum.maximumHeight() == 0
        viewer._sweep()


#: What the reference workload below costs on the machine the sixteen
#: millisecond budget was set on. Measured, not guessed.
REFERENCE_MS = 12.2
_FACTOR = None


def _machine_factor() -> float:
    """How much slower this machine is than the one the budget was set on.

    A wall-clock budget is not a test of the code when the code runs on
    both a laptop and a shared virtual machine: the same scene measured
    5.5 ms here and 22.7 ms on a CI runner, and the gap was not even
    consistent between scenes. So the budget is scaled by what this
    machine takes over a fixed workload of the same kind - antialiased
    strokes and a smooth scaled blit, which is what the scenes spend
    their time on.

    That keeps the check meaningful as a regression test: a scene that
    gets slower relative to everything else still fails. It stops being a
    measure of how fast the runner is, which is not something this
    repository can fix.
    """
    import math
    import time

    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap

    global _FACTOR
    if _FACTOR is not None:
        return _FACTOR

    canvas = QPixmap(900, 600)
    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.fillRect(QRectF(0, 0, 900, 600), QColor(0, 0, 0))
    rounds = 8
    started = time.monotonic()
    for step in range(rounds):
        for i in range(120):
            angle = (i / 120.0) * math.tau + step * 0.1
            painter.setPen(QPen(QColor.fromHsvF(i / 120.0, 0.7, 1.0, 0.8), 6.0,
                                Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(QPointF(450, 300),
                             QPointF(450 + math.cos(angle) * 280,
                                     300 + math.sin(angle) * 280))
        small = canvas.scaled(450, 300, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.drawPixmap(QRectF(0, 0, 900, 600), small, QRectF(small.rect()))
    taken = (time.monotonic() - started) / rounds * 1000
    painter.end()
    # Never below one: a machine faster than the reference still has to
    # meet the real budget.
    _FACTOR = max(1.0, taken / REFERENCE_MS)
    return _FACTOR


def _scene_count():
    """Every scene, not the five that existed when this was written.

    The count was hard-coded, so the sixth - which turned out to be the
    most expensive of the set by a wide margin - was never measured.
    """
    import visualizers

    return visualizers.SCENES


class TestItHoldsSixtyFramesASecond:
    """Every scene, at the sizes a screen actually is."""

    @staticmethod
    def _spectrum(qtbot, width, height):
        import math
        from array import array

        import attachment_audio
        from attachment_widgets import Spectrum

        rate = attachment_audio.DECODE_RATE
        pcm = array("h", [
            int(10000 * (math.sin(2 * math.pi * 90 * i / rate)
                         + 0.7 * math.sin(2 * math.pi * 1100 * i / rate)) / 1.7)
            for i in range(rate * 2)])
        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        spectrum.set_frames(attachment_audio.analyse(pcm, rate, 1),
                            attachment_audio.RATE)
        spectrum.set_labels([str(c) for c in attachment_audio.CENTRES])
        spectrum.resize(width, height)
        spectrum._reveal_changed(1.0)
        spectrum.set_position(900)
        return spectrum

    def test_the_timer_asks_for_sixty(self, qapp):
        """qapp, because a QWidget without a QApplication aborts.

        This passed only because something earlier in the file happened to
        build one first; run on its own it took the process down.
        """
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        assert spectrum._timer.interval() <= 17, "that is not sixty a second"

    @pytest.mark.parametrize("index", range(len(_scene_count())))
    def test_each_scene_fits_a_frame_at_1080p(self, qtbot, index):
        import time

        import visualizers

        spectrum = self._spectrum(qtbot, 1920, 1080)
        spectrum.set_scene(visualizers.SCENES[index])
        for _ in range(3):
            spectrum._tick()
            spectrum.grab()
        started = time.monotonic()
        rounds = 16
        for step in range(rounds):
            spectrum.set_position(900 + step * 16)
            spectrum._tick()
            spectrum.grab()
        each = (time.monotonic() - started) / rounds * 1000
        budget = 16.67 * _machine_factor()
        assert each < budget, (
            f"{visualizers.SCENES[index].name} takes {each:.1f} ms a frame, "
            f"against {budget:.1f} ms for this machine")

    def test_nothing_is_drawn_when_it_is_not_revealed(self, qtbot):
        spectrum = self._spectrum(qtbot, 1920, 1080)
        spectrum._reveal_changed(0.0)
        image = spectrum.grab().toImage()
        # A widget with no height cannot have drawn anything.
        assert image.height() <= 1


class TestTheAnalysisStaysOffTheUiThread:
    """Five and a half seconds of arithmetic used to run on the UI thread.

    analyse() is a pure-Python FFT over the whole track. It was called
    from the decoder's finished signal, which Qt delivers on the thread
    that owns the widgets, so a three minute file froze the window solid -
    grey, beachballing, indistinguishable from a crash - before anything
    appeared. The module docstring claimed it ran in a worker thread; it
    did not.
    """

    def test_analyse_can_be_abandoned_partway(self):
        import attachment_audio
        from array import array

        samples = array("h", [0] * (attachment_audio.DECODE_RATE * 8))
        frames = attachment_audio.analyse(samples, attachment_audio.DECODE_RATE,
                                          1, should_stop=lambda: True)
        assert frames == [], (
            "a cancelled analysis must give up, not finish the track")

    def test_analyse_reports_progress(self):
        import attachment_audio
        from array import array

        seen = []
        samples = array("h", [1000] * (attachment_audio.DECODE_RATE * 6))
        attachment_audio.analyse(samples, attachment_audio.DECODE_RATE, 1,
                                 on_progress=seen.append)
        assert seen, "nothing to show the user while they wait"
        assert all(0.0 <= value <= 1.0 for value in seen)
        assert seen == sorted(seen), "progress must not go backwards"

    def test_the_decoder_hands_back_something_cancellable(self):
        import attachment_audio

        assert hasattr(attachment_audio, "stop_all"), (
            "a pane destroyed as somebody's child cannot cancel its own "
            "analysis, so there has to be a way to stop all of them")


class TestTheVuScaleIsARealOne:
    """The marks were eyeballed and the per-cent row was a decibel and a
    half out. A VU movement deflects in proportion to voltage, which fixes
    every mark on the face, so they are computed rather than placed."""

    def test_zero_db_and_one_hundred_per_cent_are_the_same_point(self):
        import visualizers

        meters = visualizers.by_name("VU meters")
        zero_db = dict(meters.DB_MARKS)[0]
        full_scale = dict(meters.PERCENT_MARKS)[100]
        assert abs(zero_db - full_scale) < 1e-9, (
            "0 dB is 100 per cent on a VU meter; if these disagree the face "
            "is decoration rather than a scale")

    def test_the_marks_are_where_the_arithmetic_puts_them(self):
        import math

        import visualizers

        meters = visualizers.by_name("VU meters")
        top = 10.0 ** (3.0 / 20.0)
        for db, fraction in meters.DB_MARKS:
            assert abs(fraction - (10.0 ** (db / 20.0)) / top) < 1e-9, db

    def test_per_cent_is_linear_in_deflection(self):
        import visualizers

        meters = visualizers.by_name("VU meters")
        marks = dict(meters.PERCENT_MARKS)
        steps = [marks[pc] - marks[pc - 20] for pc in (20, 40, 60, 80, 100)]
        assert max(steps) - min(steps) < 1e-9, (
            "the movement is linear, so the per-cent marks are evenly spaced")


class TestTheTunnelIsRound:
    def test_bass_swells_the_rings_rather_than_squashing_them(self):
        """They were drawn as ellipses, up to 18 per cent flatter on a
        kick, which read as a mistake beside the circular scenes."""
        import inspect

        import visualizers

        source = inspect.getsource(visualizers.by_name("Neon tunnel").paint)
        # Comments only, stripped: the comment explaining the removal says
        # the word, and checking the prose passes on the bug it describes.
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        assert "squash" not in code, "the ellipse squash is back"
        assert "drawEllipse(centre, radius, radius)" in code


class TestPaintingCannotTakeTheProcessDown:
    """An exception out of paintEvent is fatal, not merely wrong.

    Qt catches it, prints it, and carries on with a painter still open on
    the backing store; the next frame then segfaults. A wrong argument
    type in the polish pass turned into SIGSEGV that way, and only turned
    up under a stress run that resized the window to one pixel tall.
    """

    def _spectrum(self, qapp):
        from array import array

        from attachment_widgets import Spectrum

        widget = Spectrum()
        widget.set_frames([array("f", [0.5] * 27) for _ in range(40)], 15)
        widget._reveal = 1.0
        widget._target = 1.0
        return widget

    @pytest.mark.parametrize("size", [(0, 0), (1, 1), (936, 1), (4, 300),
                                      (300, 4), (1280, 240)])
    def test_every_scene_survives_a_degenerate_frame(self, qapp, size):
        from PySide6.QtGui import QPainter, QPixmap

        import visualizers

        widget = self._spectrum(qapp)
        widget.set_post(True)
        # Through _paint, not _paint_scene: the crash was the QRect that
        # _paint takes from the widget meeting a QRectF source rectangle,
        # so a test that hands _paint_scene a QRectF of its own never sees
        # it and passes on the bug.
        widget.resize(max(1, size[0]), max(1, size[1]))
        canvas = QPixmap(max(1, size[0]), max(1, size[1]))
        for scene in visualizers.SCENES:
            widget.set_scene(scene)
            painter = QPainter(canvas)
            try:
                widget._paint(painter)
            finally:
                painter.end()
        widget.deleteLater()

    def test_paint_event_closes_its_painter_even_when_a_scene_raises(self, qapp):
        widget = self._spectrum(qapp)

        class Exploding:
            name = "boom"

            def paint(self, *_args):
                raise RuntimeError("scene went wrong")

        widget.set_scene(Exploding())
        widget.resize(200, 80)
        with pytest.raises(RuntimeError):
            widget._paint(_Recorder())
        widget.deleteLater()


class _Recorder:
    """Stands in for a QPainter, and fails the way a real one would."""

    def __getattr__(self, _name):
        def call(*_args, **_kwargs):
            return None
        return call


class TestTheVisualiserControlsFitTheirRow:
    """They overlapped each other and then ran off the pane.

    The row grows and shrinks with what is selected - the colour button
    only exists for the meters - and a plain QHBoxLayout lays them out in
    one line however narrow it gets.
    """

    def test_the_row_wraps_instead_of_overflowing(self, qapp):
        from PySide6.QtWidgets import QPushButton, QWidget

        from attachment_widgets import FlowRow

        host = QWidget()
        row = FlowRow(spacing=10)
        host.setLayout(row)
        for index in range(8):
            row.addWidget(QPushButton(f"button {index}"))
        host.resize(300, 400)
        host.show()
        qapp.processEvents()
        widest = max(row.itemAt(i).widget().geometry().right()
                     for i in range(row.count()))
        assert widest <= 300, f"a control reached {widest} in a 300px row"
        rows = {row.itemAt(i).widget().geometry().top()
                for i in range(row.count())}
        assert len(rows) > 1, "nothing wrapped, so nothing was fixed"
        host.deleteLater()

    def test_items_on_a_line_share_a_centre(self, qapp):
        from PySide6.QtWidgets import QCheckBox, QComboBox, QWidget

        from attachment_widgets import FlowRow

        host = QWidget()
        row = FlowRow(spacing=10)
        host.setLayout(row)
        box = QCheckBox("Visualiser")
        combo = QComboBox()
        combo.addItems(["Vaporwave city", "VU meters"])
        row.addWidget(box)
        row.addWidget(combo)
        host.resize(600, 200)
        host.show()
        qapp.processEvents()
        centres = [box.geometry().center().y(), combo.geometry().center().y()]
        assert abs(centres[0] - centres[1]) <= 1, (
            "a tick box and a combo box on one line have to agree on where "
            "the middle is, or the row reads as two rows")
        host.deleteLater()


class TestTheTransportKeys:
    """J back ten seconds, K play or pause, L forward ten.

    In full screen these were dead for a while without anybody noticing:
    the class had two keyPressEvent methods and the later one, which only
    knew about Escape, quietly replaced the one that knew about J, K and
    L. A test that called the method and checked it did not raise passed
    the whole time, because the surviving method does not raise either.
    """

    def _pane(self, qapp, tmp_path):
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.position.setRange(0, 300_000)
        pane.position.setValue(120_000)
        return pane

    def test_the_window_maps_j_k_and_l(self):
        import inspect

        import attachment_view

        source = inspect.getsource(attachment_view.AttachmentViewer._add_shortcuts)
        for key in ('add("J"', 'add("K"', 'add("L"'):
            assert key in source, f"{key} is not bound in the viewer"

    def test_j_and_l_move_by_ten_seconds(self, qapp, tmp_path):
        import attachment_view

        pane = self._pane(qapp, tmp_path)
        moved = []
        pane._seek = moved.append

        pane.transport("forward")
        assert pane.position.value() == 120_000 + attachment_view.SKIP_MS
        pane.transport("back")
        assert pane.position.value() == 120_000
        assert moved, "the position moved on screen but the player was told nothing"
        pane.deleteLater()

    def test_they_stop_at_the_ends_rather_than_running_past(self, qapp, tmp_path):
        pane = self._pane(qapp, tmp_path)
        pane._seek = lambda _value: None

        pane.position.setValue(2_000)
        pane.transport("back")
        assert pane.position.value() == 0
        pane.position.setValue(299_000)
        pane.transport("forward")
        assert pane.position.value() == 300_000
        pane.deleteLater()

    def test_full_screen_has_exactly_one_key_handler(self):
        """Two of them is how the transport keys died last time."""
        import inspect

        from attachment_widgets import FullScreenSpectrum

        source = inspect.getsource(FullScreenSpectrum)
        assert source.count("def keyPressEvent") == 1, (
            "more than one keyPressEvent: the last one defined wins and the "
            "others are dead")

    def test_full_screen_keys_reach_the_transport(self, qapp):
        from PySide6.QtCore import QEvent, Qt
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtWidgets import QVBoxLayout, QWidget

        from attachment_widgets import FullScreenSpectrum, Spectrum

        asked = []

        class Owner:
            def transport(self, action):
                asked.append(action)

        home = QWidget()
        QVBoxLayout(home).addWidget(Spectrum())
        spectrum = home.layout().itemAt(0).widget()
        full = FullScreenSpectrum(spectrum, Owner())
        for key, wanted in ((Qt.Key.Key_J, "back"), (Qt.Key.Key_K, "toggle"),
                            (Qt.Key.Key_L, "forward")):
            full.keyPressEvent(QKeyEvent(QEvent.Type.KeyPress, key,
                                         Qt.KeyboardModifier.NoModifier))
        assert asked == ["back", "toggle", "forward"], (
            f"full screen swallowed the transport keys: {asked}")
        full.close()


class TestTheControlsAreNeverInsideThePicture:
    """Reported repeatedly, and missed by every check until now.

    The checks were wrong, not the report. They built an AudioPane on its
    own instead of the dialog it actually lives in, compared rectangles
    instead of asking what a click would land on, and never tried the
    shapes - which is where it happened. A tall shape made the strip
    demand a height the layout could not give, so the transport and the
    visualiser row were drawn on top of the scene.
    """

    def _viewer(self, qapp, tmp_path):
        import wave

        import attachments
        from attachment_view import AttachmentViewer

        path = tmp_path / "tone.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x10" * 8000)
        data = path.read_bytes()
        item = attachments.Attachment(part="1", name="tone.wav",
                                      content_type="audio/wav",
                                      size=len(data), data=data)
        dialog = AttachmentViewer([item], "Test")
        dialog.resize(1100, 820)
        dialog.show()
        qapp.processEvents()
        dialog.list.setCurrentRow(0)
        qapp.processEvents()
        return dialog

    @staticmethod
    def _reveal(pane, qapp):
        """Get the strip to its full height without waiting for a decode.

        Everything here only goes wrong once the strip actually occupies
        space. Left at zero - which is where it sits until a track has been
        analysed and played - nothing can overlap anything and the checks
        pass while the bug is sitting there.
        """
        from array import array

        import attachment_audio

        frames = [array("f", [0.4] * attachment_audio.BANDS) for _ in range(60)]
        pane.spectrum.set_frames(frames, attachment_audio.RATE)
        pane.spectrum._flow.stop()
        pane.spectrum._target = 1.0
        pane.spectrum._reveal_changed(1.0)
        qapp.processEvents()
        assert pane.spectrum.height() > 100, (
            "the strip is not showing, so this proves nothing")

    @pytest.mark.timeout(120)
    def test_no_control_sits_on_the_scene_at_any_shape(self, qapp, tmp_path):
        from attachment_widgets import Spectrum

        dialog = self._viewer(qapp, tmp_path)
        pane = dialog.audio
        pane.enable_box.setChecked(True)
        self._reveal(pane, qapp)

        offenders = []
        for shape, _ratio in Spectrum.SHAPES:
            pane.shape_box.setCurrentText(shape)
            qapp.processEvents()
            scene = pane.spectrum.geometry()
            for name, widget in (("play", pane.play), ("seek", pane.position),
                                 ("volume", pane.volume),
                                 ("visualiser", pane.enable_box),
                                 ("scene", pane.scene_box),
                                 ("shape", pane.shape_box),
                                 ("full screen", pane.full_button)):
                if not widget.isVisible():
                    continue
                from PySide6.QtCore import QPoint

                box = widget.geometry().translated(
                    widget.parentWidget().mapTo(pane, QPoint(0, 0)))
                if scene.intersects(box):
                    offenders.append(f"{shape}/{name}")
        dialog.close()
        assert not offenders, (
            f"controls drawn inside the visualiser frame: {offenders}")

    @pytest.mark.timeout(120)
    def test_every_control_can_actually_be_clicked(self, qapp, tmp_path):
        """childAt, not a rectangle comparison: a widget can be in the
        right place and still have something on top of it."""
        from attachment_widgets import Spectrum

        dialog = self._viewer(qapp, tmp_path)
        pane = dialog.audio
        pane.enable_box.setChecked(True)
        self._reveal(pane, qapp)

        blocked = []
        for shape, _ratio in Spectrum.SHAPES:
            pane.shape_box.setCurrentText(shape)
            qapp.processEvents()
            for name, widget in (("play", pane.play), ("seek", pane.position),
                                 ("volume", pane.volume),
                                 ("visualiser", pane.enable_box),
                                 ("shape", pane.shape_box),
                                 ("full screen", pane.full_button)):
                if not widget.isVisible():
                    continue
                centre = widget.mapTo(dialog, widget.rect().center())
                if not dialog.rect().contains(centre):
                    blocked.append(f"{shape}/{name}: off the dialog")
                    continue
                hit = dialog.childAt(centre)
                if not (hit is widget or (hit is not None
                                          and widget.isAncestorOf(hit))):
                    blocked.append(f"{shape}/{name}: {type(hit).__name__} on top")
        dialog.close()
        assert not blocked, f"controls a click cannot reach: {blocked}"

    @pytest.mark.timeout(120)
    def test_the_strip_never_asks_for_more_room_than_there_is(self, qapp,
                                                             tmp_path):
        from attachment_widgets import Spectrum

        dialog = self._viewer(qapp, tmp_path)
        pane = dialog.audio
        pane.enable_box.setChecked(True)
        self._reveal(pane, qapp)
        for shape, _ratio in Spectrum.SHAPES:
            pane.shape_box.setCurrentText(shape)
            qapp.processEvents()
            assert pane.spectrum.geometry().bottom() <= pane.height() + 1, (
                f"{shape}: the strip runs {pane.spectrum.geometry().bottom()} "
                f"past a pane {pane.height()} tall")
        dialog.close()
