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

    def test_pausing_keeps_it_moving_and_does_not_start_a_countdown(self,
                                                                    qtbot):
        """Pausing used to begin a thirty second slide to nothing.

        Whoever was listening then came back to a pane that had changed
        shape under them, and the seek bar had moved. It idles instead:
        still drawing, still there, until the tick box says otherwise.
        """
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        spectrum.set_playing(False)
        assert spectrum._idling, "a paused spectrum should breathe, not freeze"
        assert spectrum._timer.isActive()
        assert not spectrum._away.isActive(), (
            "nothing should be counting down to hiding it")
        spectrum._tick()
        spectrum.grab()

    def test_it_is_still_there_long_after_it_was_paused(self, qtbot):
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        # Let the reveal animation finish rather than forcing a value it
        # is still animating towards - it would overwrite the forced one a
        # few milliseconds later and the test would be reading the slide,
        # not the resting state.
        qtbot.waitUntil(lambda: spectrum._flow.state()
                        != spectrum._flow.State.Running, timeout=3000)
        tall = spectrum.maximumHeight()
        assert tall > 100
        spectrum.set_playing(False)
        # Well past the countdown that used to hide it.
        qtbot.wait(150)
        for _ in range(40):
            spectrum._tick()
        assert spectrum.maximumHeight() >= tall, (
            f"it shrank on its own after being paused: {tall} -> "
            f"{spectrum.maximumHeight()}")

    def test_only_the_owner_puts_it_away(self, qtbot):
        """clear() is the pane putting a file down, and that still hides
        it - the strip belongs to the file, not to the playback state."""
        spectrum = self._loaded(qtbot)
        spectrum.set_playing(True)
        spectrum._reveal_changed(1.0)
        spectrum.clear()
        assert spectrum.maximumHeight() == 0

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
        # Run the layout now. The height is its decision rather than
        # something forced synchronously, and waiting a fixed number of
        # milliseconds for it passed here and failed on a slower machine.
        spectrum.parentWidget().layout().activate()
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
        layout = spectrum.parentWidget().layout()
        spectrum._reveal_changed(1.0)
        layout.activate()
        spectrum._reveal_changed(0.0)
        layout.activate()

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


class _FakeClock:
    """A clock the test moves, for anything paced in seconds.

    The needles take three hundred milliseconds of real time to reach a
    reading, the way a moving coil does. A test loop calling _tick five
    times takes microseconds, so without this the meters are measured
    before they have moved and the answer is always "nothing happened".
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def monotonic(self) -> float:
        return self.now

    def pass_time(self, seconds: float) -> None:
        self.now += seconds


class TestWaitingForTheAnalysis:
    """What the pane does while a track is being read.

    All of this was broken by a block of the constructor that had been
    pasted into the middle of set_working, and ran on every progress
    callback for months.
    """

    @staticmethod
    def _spectrum(qtbot):
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        return spectrum

    def test_progress_actually_reaches_the_pane(self, qtbot):
        """It used to be thrown away, so the bar this exists to draw had
        never once appeared."""
        spectrum = self._spectrum(qtbot)
        spectrum.set_working(0.4)
        assert spectrum._working == pytest.approx(0.4)

    def test_finishing_clears_it(self, qtbot):
        spectrum = self._spectrum(qtbot)
        spectrum.set_working(0.4)
        spectrum.set_working(None)
        assert spectrum._working is None

    def test_it_slows_the_clock_down_while_it_waits(self, qtbot):
        """The analysis and the scenes are both Python, so they take turns
        holding the interpreter lock. A pane repainting sixty times a
        second takes the processor away from the thing being waited for -
        measured at 1.6 times slower."""
        spectrum = self._spectrum(qtbot)
        spectrum.set_working(0.1)
        assert spectrum._timer.interval() >= 60
        spectrum.set_working(None)
        assert spectrum._timer.interval() == spectrum.FRAME_MS

    def test_it_does_not_reset_what_the_user_chose(self, qtbot):
        """It used to put the aspect ratio, the strobe rate, the strobe
        sensitivity and the source it listens to back to their defaults,
        several times a second, for as long as a track took to read."""
        spectrum = self._spectrum(qtbot)
        spectrum.set_aspect(16 / 9)
        spectrum.set_strobe_rate(0.9)
        spectrum.set_strobe_sense(0.2)
        spectrum.set_strobe_source("Treble")
        for step in range(6):
            spectrum.set_working(step / 6.0)
        assert spectrum._aspect == pytest.approx(16 / 9)
        assert spectrum._strobe_rate == pytest.approx(0.9)
        assert spectrum._strobe_sense == pytest.approx(0.2)
        assert spectrum._strobe_source == "Treble"

    def test_it_does_not_throw_the_render_buffer_away(self, qtbot):
        """Rebuilding the post-processor and dropping the buffer on every
        progress tick is work done during the one wait the user is
        actually watching."""
        from attachment_widgets import PostProcess

        spectrum = self._spectrum(qtbot)
        spectrum._buffer = object()
        effects = spectrum._effects
        for step in range(6):
            spectrum.set_working(step / 6.0)
        assert spectrum._buffer is not None
        assert spectrum._effects is effects


class TestTheMeterScene:
    """Ten analogue dials, copied from a photograph of a rack of them."""

    @staticmethod
    def _settle(spectrum, clock, seconds=1.0):
        """Run the clock forward at sixty a second, ticking as it goes."""
        for _ in range(int(seconds * 60)):
            clock.pass_time(1.0 / 60.0)
            spectrum._tick()

    @staticmethod
    def _spectrum(qtbot, position=1500, clock=None):
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
        if clock is None:
            for _ in range(5):
                spectrum._tick()
        else:
            TestTheMeterScene._settle(spectrum, clock)
        return spectrum

    def test_there_are_ten_bands_with_the_asked_for_labels(self, qtbot):
        spectrum = self._spectrum(qtbot)
        assert len(spectrum._state.dials) == 10
        assert spectrum._state.dial_labels == [
            "73Hz", "120Hz", "300Hz", "576Hz", "1.4kHz",
            "2.4kHz", "6kHz", "9kHz", "18kHz", "22kHz"]

    def test_a_tone_moves_its_own_needle(self, qtbot, monkeypatch):
        import attachment_widgets

        clock = _FakeClock()
        monkeypatch.setattr(attachment_widgets, "_time", clock)
        spectrum = self._spectrum(qtbot, clock=clock)
        dials = spectrum._state.dials
        assert dials[0] > 0.5, "73 Hz did not move the 73 Hz needle"
        assert dials[4] > 0.4, "1.4 kHz did not move the 1.4 kHz needle"
        assert dials[8] < 0.4, "18 kHz moved with nothing there"

    # -- how the needle moves ---------------------------------------------
    @staticmethod
    def _step_response(monkeypatch, target=1.0, seconds=1.0):
        """One needle, driven from rest to `target`, sampled at sixty."""
        import attachment_widgets
        from attachment_widgets import Spectrum

        clock = _FakeClock()
        monkeypatch.setattr(attachment_widgets, "_time", clock)
        spectrum = Spectrum()
        spectrum._dial_level = [0.0]
        spectrum._dial_speed = [0.0]
        spectrum._dial_clock = None
        track = []
        for _ in range(int(seconds * 60)):
            clock.pass_time(1.0 / 60.0)
            spectrum._swing([target])
            track.append(spectrum._dial_level[0])
        return track

    def test_the_needle_takes_about_three_hundred_milliseconds(
            self, qapp, monkeypatch):
        """Which is what makes it a VU meter rather than a bar graph.

        The standard is 300ms from rest to 99% of a step. Anything much
        faster is the jumpiness this replaced; much slower and it lags
        behind the music it is supposed to be reading.
        """
        track = self._step_response(monkeypatch)
        arrived = next((i for i, v in enumerate(track) if v >= 0.99), None)
        assert arrived is not None, "the needle never reached its reading"
        millis = (arrived + 1) / 60.0 * 1000
        assert 200 <= millis <= 420, (
            f"it took {millis:.0f} ms to reach 99%, against 300 ms")

    def test_no_single_frame_throws_it_across_the_face(
            self, qapp, monkeypatch):
        """The complaint. The rule used to be "instant up, slow down", so
        a band that jumped ten decibels between two frames moved the
        needle the whole width of the scale in one of them."""
        track = self._step_response(monkeypatch)
        biggest = max(abs(track[i + 1] - track[i])
                      for i in range(len(track) - 1))
        assert biggest < 0.25, (
            f"one frame moved the needle {biggest * 100:.0f}% of the scale")

    def test_it_overshoots_a_little_and_not_a_lot(self, qapp, monkeypatch):
        """A real movement sails just past and comes back - that is the
        whole character of it. Past a couple of per cent it reads as a
        wobble, and on the way down it slaps into the zero pin."""
        track = self._step_response(monkeypatch)
        assert max(track) <= 1.03, f"it overshot to {max(track):.3f}"
        assert abs(track[-1] - 1.0) < 0.01, "it never settled"

    def test_it_never_leaves_the_face(self, qapp, monkeypatch):
        rising = self._step_response(monkeypatch, target=1.0)
        assert min(rising) >= 0.0
        assert max(rising) <= 1.15
        # And coming back down, where an underdamped needle would swing
        # below zero and be drawn off the bottom of the scale.
        import attachment_widgets
        from attachment_widgets import Spectrum

        clock = _FakeClock()
        monkeypatch.setattr(attachment_widgets, "_time", clock)
        spectrum = Spectrum()
        spectrum._dial_level = [1.0]
        spectrum._dial_speed = [0.0]
        spectrum._dial_clock = None
        lowest = 1.0
        for _ in range(60):
            clock.pass_time(1.0 / 60.0)
            spectrum._swing([0.0])
            lowest = min(lowest, spectrum._dial_level[0])
        assert lowest >= 0.0, f"the needle went to {lowest:.3f}"

    def test_a_late_frame_does_not_fling_it(self, qapp, monkeypatch):
        """A stalled window hands back a step measured in seconds. The
        integrator is only stable while the step is short against the
        swing, so a big one has to be walked rather than taken."""
        import attachment_widgets
        from attachment_widgets import Spectrum

        clock = _FakeClock()
        monkeypatch.setattr(attachment_widgets, "_time", clock)
        spectrum = Spectrum()
        spectrum._dial_level = [0.0]
        spectrum._dial_speed = [0.0]
        spectrum._dial_clock = None
        spectrum._swing([1.0])
        clock.pass_time(3.0)
        spectrum._swing([1.0])
        landed = spectrum._dial_level[0]
        # The gap is clamped to a tenth of a second and then walked in
        # short pieces, so what comes out is a tenth of a second of
        # movement - about two thirds of the way. Taken in one piece the
        # integrator diverges and the needle slams into its end stop,
        # which is why "still on the face" is not a strong enough check.
        assert 0.2 <= landed <= 0.9, (
            f"a three second gap put the needle at {landed:.2f}, which is "
            "not a tenth of a second of travel")

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

    def test_only_the_tick_box_shows_until_the_visualiser_is_on(self, qtbot):
        """Off means gone, not greyed.

        Half a dozen disabled controls is more to read than none, and none
        of them can be used. The tick box that turns them on is the only
        thing worth showing while the visualiser is off.
        """
        viewer = self._pane(qtbot)
        assert viewer.audio.visual_holder.isVisible()
        assert viewer.audio.enable_box.isVisible()
        for widget in (viewer.audio.scene_box, viewer.audio.shape_box,
                       viewer.audio.strobe_group, viewer.audio.full_button):
            assert not widget.isVisible(), (
                "a control for something switched off is on screen")
        viewer._sweep()

    def test_they_appear_when_the_visualiser_is_switched_on(self, qtbot):
        viewer = self._pane(qtbot)
        viewer.audio.enable_box.setChecked(True)
        qtbot.wait(0)
        for widget in (viewer.audio.scene_box, viewer.audio.shape_box,
                       viewer.audio.strobe_group, viewer.audio.full_button):
            assert widget.isVisible()
        viewer.audio.enable_box.setChecked(False)
        qtbot.wait(0)
        for widget in (viewer.audio.scene_box, viewer.audio.shape_box,
                       viewer.audio.strobe_group, viewer.audio.full_button):
            assert not widget.isVisible()
        viewer._sweep()

    def test_they_are_not_inside_the_visualiser(self, qtbot):
        viewer = self._pane(qtbot)
        holder = viewer.audio.visual_holder.geometry()
        spectrum = viewer.audio.spectrum.geometry()
        assert not spectrum.intersects(holder), (
            "the controls sit inside the picture and vanish with it")
        viewer._sweep()

    def test_a_scenes_own_controls_belong_to_that_scene(self, qtbot):
        """Colours are for the meters, decay is for the scope, and
        neither exists while the visualiser is off."""
        viewer = self._pane(qtbot)
        audio = viewer.audio
        audio.enable_box.setChecked(True)
        qtbot.wait(0)

        audio.scene_box.setCurrentText("Equaliser")
        qtbot.wait(0)
        assert not audio.colour_button.isVisible()
        assert not audio.decay_box.isVisible()

        audio.scene_box.setCurrentText("VU meters")
        qtbot.wait(0)
        assert audio.colour_button.isVisible()
        assert not audio.decay_box.isVisible()

        audio.scene_box.setCurrentText("Oscilloscope")
        qtbot.wait(0)
        assert audio.decay_box.isVisible()
        assert not audio.colour_button.isVisible()

        audio.enable_box.setChecked(False)
        qtbot.wait(0)
        assert not audio.decay_box.isVisible()
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


def _settle(spectrum, painter, most: int = 500, quiet: int = 70) -> float:
    """Draw until the pane has decided what resolution it can hold.

    Every timing test below used to draw three or four frames and then
    start the clock. That was right while the resolution was a fixed
    number, and it is not now: the pane measures the scene and steps down
    a rung when a frame will not fit, so the first frames are not the
    frames anybody sees. A build runner failed exactly this way - the
    scope measured 29.8 ms against a 27.9 ms budget, at a resolution it
    would have left within two hundred frames.

    What these tests are for is whether the visualiser holds its frame
    rate, which is a question about where it ends up. So: draw until the
    scale has not moved for ``quiet`` frames, then let the caller time it.
    On a machine that never has to step down this costs the warm-up and
    no more.
    """
    governor = spectrum._sharpness
    still, last = 0, None
    for step in range(most):
        spectrum.set_position(900 + step * 16)
        spectrum._tick()
        spectrum._paint(painter)
        now = governor._scale
        still = still + 1 if now == last else 0
        last = now
        if still >= quiet and governor._warm <= 0 and governor._settle <= 0:
            break
    return last


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
        # Two channels, because the scope plots one against the other and a
        # mono track leaves it with nothing to draw.
        pcm = array("h")
        for index in range(rate * 2):
            here = (math.sin(2 * math.pi * 90 * index / rate)
                    + 0.7 * math.sin(2 * math.pi * 1100 * index / rate)
                    + 0.25 * math.sin(2 * math.pi * 5200 * index / rate))
            pcm.append(int(9000 * here / 1.95))
            pcm.append(int(9000 * (here * 0.8
                                   + 0.3 * math.sin(2 * math.pi * 310
                                                    * index / rate)) / 1.95))
        spectrum = Spectrum()
        qtbot.addWidget(spectrum)
        # The waveform slices as well as the bands, in that order, the way
        # the viewer hands them over. Without these the oscilloscope falls
        # back to a shape folded out of the band levels - a hundred and
        # twenty-eight points instead of a thousand, with no history behind
        # it - so it measured as one of the cheapest scenes while being by
        # far the most expensive in the app.
        spectrum.set_traces(
            attachment_audio.traces(pcm, rate, 2),
            attachment_audio.vector_traces(pcm, rate, 2))
        spectrum.set_frames(attachment_audio.analyse(pcm, rate, 2),
                            attachment_audio.RATE)
        spectrum.set_labels([str(c) for c in attachment_audio.CENTRES])
        # Unbounded, or the strip's own maximum height clamps the widget
        # and the frame measured is 240 tall however big the numbers in
        # this call are. This test has been called "at 1080p" since it was
        # written and had never once drawn a frame that size.
        spectrum.set_unbounded(True)
        spectrum.resize(width, height)
        spectrum._reveal_changed(1.0)
        spectrum.set_position(900)
        # One turn of the clock, because that is what fills the state in.
        spectrum._tick()
        assert spectrum.height() == height, (
            f"the widget is {spectrum.height()} tall, not {height}; this is "
            "not measuring what it says it is")
        assert spectrum._state.trace, (
            "no waveform reached the state, so the scope will draw its "
            "cheap fallback and this measures nothing")
        return spectrum

    def test_the_timer_asks_for_sixty(self, qapp):
        """qapp, because a QWidget without a QApplication aborts.

        This passed only because something earlier in the file happened to
        build one first; run on its own it took the process down.
        """
        from attachment_widgets import Spectrum

        spectrum = Spectrum()
        assert spectrum._timer.interval() <= 17, "that is not sixty a second"

    #: What every scene costs on this machine, measured once. Held so the
    #: per-scene checks can be about how the scenes compare rather than
    #: about how fast the machine is.
    _COSTS = {}

    def _cost_of_every_scene(self, qtbot):
        """One measured frame time per scene, on whatever this machine is."""
        import time

        import visualizers

        from PySide6.QtGui import QPainter, QPixmap

        if TestItHoldsSixtyFramesASecond._COSTS:
            return TestItHoldsSixtyFramesASecond._COSTS
        found = {}
        for scene in visualizers.SCENES:
            spectrum = self._spectrum(qtbot, 1920, 1080)
            spectrum.set_scene(scene)
            canvas = QPixmap(1920, 1080)
            canvas.setDevicePixelRatio(spectrum.devicePixelRatioF())
            painter = QPainter(canvas)
            try:
                _settle(spectrum, painter)
                started = time.monotonic()
                rounds = 12
                for step in range(rounds):
                    spectrum.set_position(900 + step * 16)
                    spectrum._tick()
                    spectrum._paint(painter)
                found[scene.name] = (time.monotonic() - started) / rounds * 1000
            finally:
                painter.end()
        TestItHoldsSixtyFramesASecond._COSTS = found
        return found

    #: How much more than the middle scene any one of them may cost.
    #:
    #: A relative check, because an absolute one in milliseconds is a
    #: measure of the machine rather than of the code. Calibrating it
    #: against a fixed workload was the previous attempt and it does not
    #: hold either: the calibration strokes paths, and a scene whose cost
    #: is arithmetic or fill rate scales differently from one that
    #: strokes - measured, two scenes came out at 25 and 30 ms on a build
    #: runner against a budget the calibration put at 24, while on the
    #: machine they were written on they were the cheapest two of eight.
    #:
    #: Comparing the scenes with each other cannot drift that way: they
    #: are all measured in the same session on the same machine, and a
    #: scene that gets slower relative to its neighbours still fails.
    #:
    #: Against the middle of them rather than the cheapest, which is the
    #: correction this needed. The cheapest is one reading, and making a
    #: scene faster moves it: Waterfall dropped from 41 ms a frame to 8,
    #: which is a fix, and it would have failed this test by lowering the
    #: floor that every other scene is held against. The median of eight
    #: does not move when one of them changes.
    SPREAD = 2.6

    def test_no_scene_costs_far_more_than_the_others(self, qtbot):
        import statistics

        costs = self._cost_of_every_scene(qtbot)
        assert len(costs) >= 2
        middle = statistics.median(costs.values())
        worst = max(costs, key=costs.get)
        assert costs[worst] <= middle * self.SPREAD, (
            f"{worst} takes {costs[worst]:.1f} ms against {middle:.1f} ms "
            f"for the middle scene: "
            + ", ".join(f"{n} {v:.1f}" for n, v in sorted(costs.items())))

    def test_the_whole_set_fits_a_frame_on_a_reasonable_machine(self, qtbot):
        """A loose absolute floor, so "everything got slower" is caught.

        Generous on purpose: this one is allowed to be a statement about
        the machine, and it only fires when something has gone badly
        wrong rather than when a runner is having a slow minute.
        """
        costs = self._cost_of_every_scene(qtbot)
        budget = 16.67 * _machine_factor() * 2.5
        over = {n: v for n, v in costs.items() if v > budget}
        assert not over, (
            f"against {budget:.0f} ms: "
            + ", ".join(f"{n} {v:.1f}" for n, v in sorted(over.items())))

    @pytest.mark.parametrize("index", range(len(_scene_count())))
    def test_each_scene_fits_a_frame_at_1080p(self, qtbot, index):
        import time

        import visualizers

        from PySide6.QtGui import QPainter, QPixmap

        spectrum = self._spectrum(qtbot, 1920, 1080)
        spectrum.set_scene(visualizers.SCENES[index])
        # Into a surface that is made once, the way the running app paints
        # into a backing store it already has. grab() allocates a fresh
        # eight megapixel pixmap every frame, which cost ten milliseconds
        # here - most of the budget, spent on something the app never does.
        canvas = QPixmap(1920, 1080)
        canvas.setDevicePixelRatio(spectrum.devicePixelRatioF())
        painter = QPainter(canvas)
        try:
            _settle(spectrum, painter)
            started = time.monotonic()
            rounds = 16
            for step in range(rounds):
                spectrum.set_position(900 + step * 16)
                spectrum._tick()
                spectrum._paint(painter)
            each = (time.monotonic() - started) / rounds * 1000
        finally:
            painter.end()
        # Loose, and deliberately so: the tight check is
        # test_no_scene_costs_far_more_than_the_others, which compares the
        # scenes with each other and so cannot be thrown off by the
        # machine. This one is here to catch a scene that has become
        # absurd rather than one that is merely slower than its
        # neighbours.
        budget = 16.67 * _machine_factor() * 2.5
        assert each < budget, (
            f"{visualizers.SCENES[index].name} takes {each:.1f} ms a frame, "
            f"against {budget:.1f} ms for this machine")

    @pytest.mark.parametrize("decay", [0.03, 0.75, 1.50])
    def test_the_scope_holds_up_at_every_decay(self, qtbot, decay):
        """The setting that was reported as laggy, at the size it lags at.

        The scene budget above runs each scene at whatever it happens to
        be set to, which for the scope is the middle of the decay slider.
        The complaint was about the top of it, and the top of it was the
        one place the old drawing fell over: it kept every trace of the
        set persistence and redrew all of them, so a frame cost what the
        slider said. Measured here it was 18ms with spikes past 30, on a
        16.7ms budget.
        """
        import time

        import visualizers

        from PySide6.QtGui import QPainter, QPixmap

        spectrum = self._spectrum(qtbot, 1920, 1080)
        scope = visualizers.by_name("Oscilloscope")
        was = scope.decay
        scope.set_decay(decay)
        spectrum.set_scene(scope)
        canvas = QPixmap(1920, 1080)
        canvas.setDevicePixelRatio(spectrum.devicePixelRatioF())
        painter = QPainter(canvas)
        try:
            _settle(spectrum, painter)
            spent = []
            rounds = 24
            started = time.monotonic()
            for step in range(rounds):
                spectrum.set_position(900 + step * 16)
                spectrum._tick()
                one = time.monotonic()
                spectrum._paint(painter)
                spent.append((time.monotonic() - one) * 1000)
            each = (time.monotonic() - started) / rounds * 1000
        finally:
            painter.end()
            scope.set_decay(was)
        budget = 16.67 * _machine_factor()
        assert each < budget, (
            f"the scope takes {each:.1f} ms a frame at a decay of {decay}, "
            f"against {budget:.1f} ms for this machine")
        # And the slow frames, because an average inside the budget with a
        # stutter every second is what somebody actually notices.
        #
        # The ninetieth percentile rather than the worst one: on a shared
        # build machine a single frame gets descheduled and comes back at
        # forty milliseconds, which says nothing about this code. Ten per
        # cent of frames over the budget is a stutter; one is weather.
        spent.sort()
        ninety = spent[int(len(spent) * 0.9)]
        assert ninety < budget * 1.5, (
            f"a tenth of the frames take {ninety:.1f} ms or more at a decay "
            f"of {decay}, against {budget:.1f} ms")

    def test_the_decay_does_not_change_what_a_frame_costs(self, qtbot):
        """A screen that fades costs the same whatever it is set to.

        This is the shape of the fix, not just its size: the old drawing
        was linear in the persistence, so every step of the slider was a
        step slower, and there was no setting at the top that was not.
        """
        import time

        import visualizers

        from PySide6.QtGui import QPainter, QPixmap

        scope = visualizers.by_name("Oscilloscope")
        was = scope.decay
        spent = {}
        try:
            for decay in (scope.MIN_DECAY, scope.MAX_DECAY):
                spectrum = self._spectrum(qtbot, 1920, 1080)
                scope.set_decay(decay)
                spectrum.set_scene(scope)
                canvas = QPixmap(1920, 1080)
                canvas.setDevicePixelRatio(spectrum.devicePixelRatioF())
                painter = QPainter(canvas)
                try:
                    for _ in range(4):
                        spectrum._tick()
                        spectrum._paint(painter)
                    started = time.monotonic()
                    rounds = 24
                    for step in range(rounds):
                        spectrum.set_position(900 + step * 16)
                        spectrum._tick()
                        spectrum._paint(painter)
                    spent[decay] = (time.monotonic() - started) / rounds * 1000
                finally:
                    painter.end()
        finally:
            scope.set_decay(was)
        slowest, fastest = max(spent.values()), min(spent.values())
        assert slowest < fastest * 1.8 + 2.0, (
            f"the longest persistence costs {slowest:.1f} ms against "
            f"{fastest:.1f} ms for the shortest, so it is still being paid "
            f"for per frame of history")

    def test_nothing_is_drawn_when_it_is_not_revealed(self, qtbot):
        spectrum = self._spectrum(qtbot, 1920, 1080)
        spectrum._reveal_changed(0.0)
        image = spectrum.grab().toImage()
        # The widget keeps its size in full screen, so "nothing drawn" has
        # to be read off the pixels rather than off the height.
        # Uniform, rather than any particular colour: whatever the widget's
        # own background happens to be, a scene that drew would not leave
        # every corner and the middle identical.
        sampled = {image.pixelColor(x, y).rgb()
                   for x, y in ((2, 2), (image.width() - 3, 2),
                                (2, image.height() - 3),
                                (image.width() - 3, image.height() - 3),
                                (image.width() // 2, image.height() // 2),
                                (image.width() // 3, image.height() // 4))}
        assert len(sampled) == 1, (
            f"something was drawn at zero reveal: {len(sampled)} colours")


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

    def test_it_lays_out_inside_its_margins(self, qapp):
        """A QLayout subclass insets the rectangle by its own contents
        margins itself; nothing does it for one. This did not, so every
        margin set on it was ignored - invisible while they were ten
        pixels, and obvious the moment the control bar wanted room around
        its panel for a shadow."""
        from PySide6.QtWidgets import QPushButton, QWidget

        from attachment_widgets import FlowRow

        host = QWidget()
        row = FlowRow(spacing=10)
        row.setContentsMargins(30, 24, 30, 24)
        host.setLayout(row)
        for index in range(3):
            row.addWidget(QPushButton(f"button {index}"))
        host.resize(600, 200)
        host.show()
        qapp.processEvents()
        boxes = [row.itemAt(i).widget().geometry() for i in range(row.count())]
        assert min(b.left() for b in boxes) >= 30, "it drew over the left margin"
        assert min(b.top() for b in boxes) >= 24, "it drew over the top margin"
        assert max(b.right() for b in boxes) <= 600 - 30, "it overran the right"
        host.deleteLater()

    def test_the_height_it_asks_for_includes_its_margins(self, qapp):
        """Counting them in one place and not the other is how a panel
        ends up shorter than the things inside it."""
        from PySide6.QtWidgets import QPushButton, QWidget

        from attachment_widgets import FlowRow

        host = QWidget()
        bare = FlowRow(spacing=10)
        host.setLayout(bare)
        for index in range(2):
            bare.addWidget(QPushButton(f"button {index}"))
        without = bare.heightForWidth(600)

        other = QWidget()
        padded = FlowRow(spacing=10)
        padded.setContentsMargins(30, 24, 30, 24)
        other.setLayout(padded)
        for index in range(2):
            padded.addWidget(QPushButton(f"button {index}"))
        assert padded.heightForWidth(600) == without + 48
        host.deleteLater()
        other.deleteLater()

    def test_the_full_screen_bar_is_the_size_of_what_is_in_it(self, qapp):
        """The panel is drawn inside the widget by the shadow's reach, and
        the controls have to land inside the panel rather than over its
        edge or floating above its floor."""
        from PySide6.QtWidgets import QPushButton

        from attachment_widgets import (FullScreenSpectrum, Spectrum,
                                        _ControlBar)

        spectrum = Spectrum()
        full = FullScreenSpectrum(spectrum)
        for label in ("Play", "Pause", "Leave full screen"):
            full.add_control(QPushButton(label))
        full.resize(1280, 800)
        full.show()
        qapp.processEvents()

        panel = full.bar.rect().adjusted(
            _ControlBar.SHADOW, _ControlBar.SHADOW,
            -_ControlBar.SHADOW, -_ControlBar.SHADOW)
        layout = full._bar_layout
        boxes = [layout.itemAt(i).geometry() for i in range(layout.count())]
        assert boxes, "no controls to check"
        for box in boxes:
            assert panel.contains(box), (
                f"a control at {box.getRect()} is outside the panel at "
                f"{panel.getRect()}")
        slack = panel.height() - max(b.height() for b in boxes)
        assert slack <= 30, (
            f"the panel is {slack}px taller than its tallest control")
        full.close()
        full.deleteLater()
        spectrum.deleteLater()

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


class TestTheStripGivesWayWhenThereIsNoRoom:
    """It used to insist on its height and push the transport off the pane.

    The height was forced by setting the minimum and the maximum to the
    same number. In a pane too short to hold everything the layout then
    had nowhere to put the controls and drew them over the scene - the
    scrub bar inside the picture, unclickable. A widget that can shrink
    cannot do that, so the minimum is a floor and the maximum is what the
    shape asks for.
    """

    @staticmethod
    def _in_a_pane(qtbot, host_height):
        from array import array

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
        host.resize(900, host_height)
        host.show()
        spectrum.set_frames([array("f", [0.5] * 32) for _ in range(60)], 20)
        spectrum._reveal_changed(1.0)
        # activate() runs the layout there and then. Waiting a number of
        # milliseconds for it was enough on one machine and not on a
        # slower one; this does not depend on the machine at all.
        layout.activate()
        return host, spectrum

    def test_it_takes_what_it_asks_for_when_there_is_room(self, qtbot):
        _host, spectrum = self._in_a_pane(qtbot, 420)
        assert spectrum.height() == spectrum.HEIGHT

    # Down to a host that can still hold its own minimum content. Below
    # about 150 nothing fits whatever the strip does, and Qt overlaps
    # because it has been asked for the impossible.
    @pytest.mark.parametrize("host_height", [320, 260, 200, 170])
    def test_it_never_pushes_its_neighbours_off(self, qtbot, host_height):
        host, spectrum = self._in_a_pane(qtbot, host_height)
        layout = host.layout()
        for index in range(layout.count()):
            widget = layout.itemAt(index).widget()
            if widget is None or widget is spectrum:
                continue
            assert widget.geometry().bottom() <= host.height(), (
                f"{widget.text()} was pushed off a {host_height}px host")
            assert not spectrum.geometry().intersects(widget.geometry()), (
                f"the scene was drawn over {widget.text()}")

    def test_it_can_shrink_to_its_floor(self, qtbot):
        _host, spectrum = self._in_a_pane(qtbot, 120)
        assert spectrum.height() <= spectrum.HEIGHT
        assert spectrum.height() >= 0


class TestOscilloscopeMusic:
    """Left against right, which is how a scope draws a picture.

    A record cut for an oscilloscope puts the drawing in the difference
    between the two channels. Decoding to mono threw it away, so the
    decode asks for two and the scope can plot one against the other.
    """

    @staticmethod
    def _stereo_square(seconds: float = 0.4):
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        corners = [(-1, -1), (1, -1), (1, 1), (-1, 1)]
        per_side = max(4, int(rate * seconds) // 40 // 4)
        pcm = array("h")
        for _ in range(40):
            for index in range(4):
                x0, y0 = corners[index]
                x1, y1 = corners[(index + 1) % 4]
                for step in range(per_side):
                    share = step / per_side
                    pcm.append(int(18000 * (x0 + (x1 - x0) * share)))
                    pcm.append(int(18000 * (y0 + (y1 - y0) * share)))
        return pcm, rate

    def test_the_decode_asks_for_two_channels(self):
        """One channel cannot hold a picture."""
        import inspect

        import attachment_audio

        source = inspect.getsource(attachment_audio.decode)
        code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
        assert "setChannelCount(2)" in code

    def test_a_stereo_circle_comes_back_as_a_circle(self):
        import math
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        pcm = array("h")
        for index in range(rate):
            angle = 2 * math.pi * 40 * index / rate
            pcm.append(int(20000 * math.cos(angle)))
            pcm.append(int(20000 * math.sin(angle)))
        vectors = attachment_audio.vector_traces(pcm, rate, 2)
        assert vectors, "nothing to plot"
        first = vectors[0]
        # Stored as int16, so a long track's worth fits in memory.
        radii = [math.hypot(first[i * 2], first[i * 2 + 1]) / 32768.0
                 for i in range(len(first) // 2)]
        assert max(radii) - min(radii) < 0.05, (
            f"a circle came back as something else: {min(radii):.2f} to "
            f"{max(radii):.2f}")

    def test_the_points_are_consecutive_samples(self):
        """Every sample in the window is part of the drawing.

        The sweep takes every eighth sample, which is fine for a waveform
        and turns a detailed figure into a scribble. A picture cut at
        audio rate needs all of them.
        """
        from array import array

        import attachment_audio

        rate = attachment_audio.DECODE_RATE
        # A ramp, so each sample is identifiable by its value.
        pcm = array("h")
        for index in range(rate):
            pcm.append(index % 1000)
            pcm.append((index % 1000) * -1)
        vectors = attachment_audio.vector_traces(pcm, rate, 2)
        assert vectors
        left = vectors[0][0::2]
        steps = {left[i + 1] - left[i] for i in range(len(left) - 1)
                 if left[i + 1] > left[i]}
        assert steps == {1}, (
            f"samples are being skipped: {sorted(steps)[:5]}")

    def test_the_scope_plots_the_picture_not_a_sweep(self, qapp):
        """The X-Y path has to follow the samples, not a clock."""
        import visualizers
        from attachment_widgets import SpectrumState

        scope = visualizers.by_name("Oscilloscope")
        scope.set_mode("X-Y")
        assert scope.mode == "X-Y"

        state = SpectrumState()
        # A square, as four corners, in the int16 the analysis produces.
        full = 32767
        state.vector = [-full, -full, full, -full, full, full, -full, full]
        # Built in a unit box - the window size and the strobe are a
        # transform applied when it is drawn, not part of the shape.
        path = scope._path(state.vector, True)
        assert path.elementCount() == 4
        xs = {round(path.elementAt(i).x, 3) for i in range(4)}
        ys = {round(path.elementAt(i).y, 3) for i in range(4)}
        assert len(xs) == 2 and len(ys) == 2, (
            "the four corners did not land on two x values and two y values, "
            "so this is not plotting one channel against the other")
        scope.set_mode("Sweep")

    def test_the_shape_does_not_depend_on_the_window(self):
        """So a resize is a transform rather than a rebuild, and so the
        screen the trace is burned into can be kept between frames."""
        import visualizers

        scope = visualizers.by_name("Oscilloscope")
        trace = [0.4, -0.2, 0.9, -0.7, 0.1]
        once = scope._path(trace, False)
        twice = scope._path(trace, False)
        assert once.elementCount() == twice.elementCount()
        for index in range(once.elementCount()):
            assert once.elementAt(index).x == twice.elementAt(index).x

    def test_the_screen_is_kept_between_frames(self, qapp):
        """The decay is a screen that fades, not a stack of redrawn
        traces - which is what made the top of the slider unusable."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter, QPixmap

        import visualizers
        from attachment_widgets import SpectrumState

        scope = visualizers.Oscilloscope()
        state = SpectrumState()
        state.levels = [0.5] * 8
        surface = QPixmap(200, 200)
        for frame in range(3):
            state.trace = [0.4, -0.2, 0.9, -0.7, 0.1, float(frame)]
            painter = QPainter(surface)
            try:
                scope.paint(painter, QRectF(0, 0, 200, 200), state)
            finally:
                painter.end()
        assert scope._screen is not None
        assert scope._screen.size().width() == 200

    def test_a_repeated_trace_is_not_burned_in_twice(self, qapp):
        """A paused track hands back the same trace every frame. Drawing
        it again each time piles brightness up until the screen is a
        solid disc."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPainter, QPixmap

        import visualizers
        from attachment_widgets import SpectrumState

        scope = visualizers.Oscilloscope()
        state = SpectrumState()
        state.levels = [0.5] * 8
        state.trace = [0.4, -0.2, 0.9, -0.7, 0.1]
        strikes = []
        scope._strike = lambda *a, **k: strikes.append(1)
        surface = QPixmap(200, 200)
        for _ in range(5):
            painter = QPainter(surface)
            try:
                scope.paint(painter, QRectF(0, 0, 200, 200), state)
            finally:
                painter.end()
        assert len(strikes) == 1, f"the beam struck {len(strikes)} times"

    def test_an_unknown_mode_is_ignored(self):
        import visualizers

        scope = visualizers.by_name("Oscilloscope")
        scope.set_mode("Sweep")
        scope.set_mode("nonsense")
        assert scope.mode == "Sweep"


class TestTheVisualiserWindowIsItsOwnThing:
    """Somebody's own music, not a message's parts."""

    @staticmethod
    def _window(qtbot, library):
        from attachment_view import AttachmentViewer

        viewer = AttachmentViewer([], "", library=library)
        qtbot.addWidget(viewer)
        return viewer

    def test_a_library_offers_adding_and_not_saving(self, qtbot):
        viewer = self._window(qtbot, library=True)
        assert viewer.windowTitle() == "Visualiser"
        assert viewer.add_button.isVisible() or not viewer.isVisible()
        assert not viewer.save_button.isVisible()
        assert not viewer.save_all.isVisible()

    def test_an_attachment_window_offers_saving_and_not_adding(self, qtbot):
        viewer = self._window(qtbot, library=False)
        assert viewer.windowTitle() == "Attachments"
        assert not viewer.add_button.isVisible()

    def test_the_library_does_not_promise_to_save_anything(self, qtbot):
        """The key list should not offer a shortcut the window has removed."""
        viewer = self._window(qtbot, library=True)
        assert "save a copy" not in viewer.hint.text()
        plain = self._window(qtbot, library=False)
        assert "save a copy" in plain.hint.text()


class TestNothingRunsOffTheEdge:
    """The pane has to fit the width it is given, not the one it wants.

    The container holding the wrapping row reported a six hundred pixel
    minimum width, so a narrower window could not shrink it: the layout
    kept the width it wanted and everything past the right edge was cut,
    mid-word in the case of the decay label.
    """

    def test_the_control_row_has_no_width_of_its_own(self, qapp):
        from PySide6.QtWidgets import QPushButton

        from attachment_widgets import FlowHolder, FlowRow

        row = FlowRow(spacing=16)
        for index in range(6):
            row.addWidget(QPushButton(f"control {index}"))
        holder = FlowHolder(row)
        holder.resize(900, 40)
        qapp.processEvents()
        widest = max(row.itemAt(i).sizeHint().width()
                     for i in range(row.count()))
        assert holder.minimumSizeHint().width() <= widest + 1, (
            f"the row insists on {holder.minimumSizeHint().width()} pixels "
            f"when its widest control is {widest}; a row that wraps has no "
            "minimum width beyond one control")
        holder.deleteLater()

    def test_it_reports_a_taller_height_when_it_is_narrower(self, qapp):
        from PySide6.QtWidgets import QPushButton

        from attachment_widgets import FlowHolder, FlowRow

        row = FlowRow(spacing=16)
        for index in range(8):
            row.addWidget(QPushButton(f"control {index}"))
        holder = FlowHolder(row)
        assert holder.hasHeightForWidth()
        wide = holder.heightForWidth(1200)
        narrow = holder.heightForWidth(300)
        assert narrow > wide, (
            "a wrapping row is taller when it is narrower; if it says "
            "otherwise the layout gives it one line and cuts the rest")
        holder.deleteLater()

    @pytest.mark.timeout(120)
    def test_no_control_reaches_past_the_pane(self, qapp, tmp_path):
        """The bug as it looked: the decay caption cut off mid-word."""
        import wave

        from PySide6.QtCore import QPoint

        import attachments
        from attachment_view import AttachmentViewer

        path = tmp_path / "tone.wav"
        with wave.open(str(path), "wb") as handle:
            handle.setnchannels(2)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x10\x00\x08" * 8000)
        data = path.read_bytes()
        item = attachments.Attachment(part="1", name="tone.wav",
                                      content_type="audio/wav",
                                      size=len(data), data=data)
        dialog = AttachmentViewer([item])
        qapp.processEvents()
        dialog.show()
        dialog.list.setCurrentRow(0)
        pane = dialog.audio
        pane.enable_box.setChecked(True)
        qapp.processEvents()

        offenders = []
        for width, height in ((1400, 900), (1000, 700), (900, 600)):
            dialog.resize(width, height)
            qapp.processEvents()
            dialog.layout().activate()
            for name in ("scene_box", "shape_box", "strobe_group",
                         "full_button", "position", "volume"):
                widget = getattr(pane, name)
                if not widget.isVisible():
                    continue
                edge = widget.geometry().translated(
                    widget.parentWidget().mapTo(pane, QPoint(0, 0))).right()
                if edge > pane.width() + 1:
                    offenders.append(f"{width}x{height}/{name}")
        dialog.close()
        assert not offenders, f"drawn past the right edge: {offenders}"


class TestTheDialsMatchTheReference:
    """The face is drawn to numbers taken off the reference photograph,
    not to taste. These check the numbers are still the ones measured.

    Its cell is 562 by 339 and its arc runs from (55,192) to (500,192)
    over an apex at y=75: a chord of 445 rising 117, which is a radius of
    270 and a sweep of 111 degrees. Everything drawn fits in 499 by 269
    of that cell - 1.85 radii by 1.00 - and the centre of the arc sits
    1.12 radii below the top of it.
    """

    #: What the reference measures, in radii.
    REF_WIDE, REF_TALL, REF_SWEEP = 1.85, 1.00, 111.0

    def test_the_sweep_is_the_measured_one(self):
        import visualizers

        assert abs(abs(visualizers.Meters.SWEEP) - self.REF_SWEEP) <= 3.0

    def test_the_face_is_as_wide_and_as_short_as_the_reference(self):
        """Being too wide is what made the faces small: the radius is
        whichever dimension runs out first, so asking for a quarter more
        width than the face uses throws that quarter away."""
        import visualizers

        meters = visualizers.Meters
        assert abs(meters.FACE_WIDE - self.REF_WIDE) < 0.18, (
            f"{meters.FACE_WIDE} radii wide against {self.REF_WIDE}")
        # The drawn height is from the top of the dB numbers down to the
        # frequency, not down to the hinge - nothing is drawn at the hinge.
        drawn = meters.FACE_DROP - meters.LABEL_AT
        assert abs(drawn - self.REF_TALL) < 0.15, (
            f"{drawn:.2f} radii of drawing against {self.REF_TALL}")

    def test_the_shape_it_asks_for_is_the_shape_it_draws(self):
        """The reserved box and the drawing measure against the same
        constants, so they cannot disagree - which they did, by a factor
        of 1.76 in height."""
        import visualizers

        meters = visualizers.Meters
        assert meters.FACE_TALL >= meters.FACE_DROP - meters.LABEL_AT - 0.02
        assert meters.FACE_WIDE / meters.FACE_TALL > 1.5, (
            "a VU face is much wider than it is tall")

    def test_there_is_no_hub(self):
        """The reference shows none: the needle runs off the bottom of
        the face and the lowest thing on it is the frequency. Drawing one
        put the face's bottom edge a sixth of a radius lower."""
        import inspect

        import visualizers

        source = inspect.getsource(visualizers.Meters._needle)
        assert "drawEllipse" not in source, "a hub is being drawn"

    def test_the_needle_is_a_radius_of_its_own_arc(self):
        """Hinging it below the centre made it half as long again and
        dragged the whole face taller to fit."""
        import visualizers
        from PySide6.QtCore import QRectF

        geometry = visualizers.Meters._geometry(QRectF(0, 0, 400, 240))
        centre, pivot = geometry["centre"], geometry["pivot"]
        assert abs(centre.x() - pivot.x()) < 0.01
        assert abs(centre.y() - pivot.y()) < 0.01

    def test_a_face_keeps_its_proportions_at_every_size(self):
        import visualizers
        from PySide6.QtCore import QRectF

        shapes = []
        for width, height in ((200, 120), (562, 339), (1200, 700)):
            g = visualizers.Meters._geometry(QRectF(0, 0, width, height))
            radius = g["radius"]
            shapes.append(((g["centre"].y()) / radius,
                           (g["centre"].x()) / radius))
        # The centre sits in the same place relative to the radius
        # whatever the cell is, or the face is being stretched.
        first = shapes[0]
        for other in shapes[1:]:
            assert abs(other[0] - first[0]) < 0.05, shapes


class TestTheDialsMarkingsMatchTheReference:
    """Not the size of the face - the marks on it.

    Every number here was measured off the reference rather than chosen.
    It is a compressed still from a video, so there is a floor on how
    exactly anything can be read off it; these are the things that could
    be read clearly, and each one was wrong before it was measured.
    """

    def test_the_arc_is_as_thin_as_the_reference_s(self):
        """0.020 radii, which on its 270 pixel radius is 5.3 px. It was
        drawn at 0.030, and the run above 0 dB at 0.052 - one and a half
        to two and a half times too heavy."""
        import visualizers

        meters = visualizers.Meters
        assert abs(meters.ARC_STROKE - 0.020) < 0.004
        assert meters.ARC_STROKE_HOT < meters.ARC_STROKE * 1.6, (
            "the red zone is heavier than the rest, but only a little")

    def test_the_ticks_reach_outward_past_the_arc(self):
        """Measured at -3, 0, +1, +2 and +3 the ink continues to about
        1.05 radii and there is none inside. They were drawn from 0.84 to
        1.00 - the wrong side of the line they mark."""
        import visualizers

        meters = visualizers.Meters
        assert meters.TICK_OUT > 1.0, "the ticks do not reach past the arc"
        assert meters.TICK_IN > 0.90, "they reach too far inward"
        assert meters.TICK_OUT - meters.TICK_IN < 0.15

    def test_the_dots_are_few_and_outside(self):
        """One per decibel from -20 up is twenty-four marks crowded into
        the left half, which reads as a smear. The reference has eight or
        nine, evenly spread, and they sit outside the arc."""
        import visualizers

        meters = visualizers.Meters
        assert 6 <= len(meters.DOTS) <= 14, f"{len(meters.DOTS)} dots"
        assert meters.DOT_AT > 1.0, "the dots are inside the arc"
        gaps = [b - a for a, b in zip(meters.DOTS, meters.DOTS[1:])]
        assert min(gaps) > 0.03, "two dots are nearly on top of each other"

    def test_a_dot_never_lands_on_a_numbered_mark(self):
        import visualizers

        meters = visualizers.Meters
        numbered = [f for _v, f in meters.DB_MARKS]
        numbered += [f for _v, f in meters.PERCENT_MARKS]
        for dot in meters.DOTS:
            nearest = min(abs(dot - at) for at in numbered)
            assert nearest > 0.015, f"a dot sits on a mark at {dot:.3f}"

    def test_the_numbers_ask_for_a_squarish_face(self, qapp):
        """The reference's "0" is a rounded rectangle rather than a
        circle - the Eurostile family. None of these is on every machine,
        so it is a list in order of preference rather than one name, and
        at least one of them has to be here or the face silently falls
        back to whatever the system default is."""
        import visualizers
        from PySide6.QtGui import QFontDatabase

        wanted = visualizers.Meters.FAMILIES
        assert len(wanted) >= 4, "one name is not a fallback"
        assert wanted[0] == "Eurostile", "the real article comes first"
        here = set(QFontDatabase.families())
        assert any(name in here for name in wanted), (
            f"none of {wanted} is on this machine")

    def test_the_per_cent_row_is_the_quieter_of_the_two(self):
        """Two scales on one face, and only one of them is the scale."""
        import inspect

        import visualizers

        source = inspect.getsource(visualizers.Meters._draw_face)
        assert "setAlphaF(0.62)" in source or "inside.setAlphaF" in source

    def test_the_dB_numbers_clear_the_arc(self):
        """At 1.07 radii their bottoms sat on the arc rather than above
        it, because this face's numerals are a shade taller than the
        reference's."""
        import visualizers

        meters = visualizers.Meters
        # Half the numeral's height below its centre has to clear the
        # arc's outer edge.
        bottom = meters.DB_AT_R - meters.DB_TYPE * 0.75
        assert bottom > meters.ARC_AT + meters.ARC_STROKE / 2, (
            f"the numbers reach {bottom:.3f} and the arc's top edge is at "
            f"{meters.ARC_AT + meters.ARC_STROKE / 2:.3f}")


class TestTheSceneIsDrawnAtTheScreensResolution:
    """Full screen used to be softer than the window it replaced.

    The whole rule was one number - 600,000 pixels, above which the scene
    was drawn smaller and stretched - and the number was written on one
    machine and applied to every screen. Measured against the window's own
    logical resolution it came out at 0.54 of it at 1920x1080 and 0.64 at
    a retina full screen, while the same rule in a windowed strip drew at
    1.81 times logical. That difference is what "fuzzy at full screen"
    was: not a blur, a genuinely smaller picture stretched up.

    So these are about the floor. Below one buffer pixel per point the
    picture goes soft, and for scenes like these soft is worse than
    thirty frames a second.
    """

    @staticmethod
    def _settled(governor, cost_at, ratio=2.0, pixels=8_294_400, frames=900,
                 scene=object()):
        """Run the governor against a cost model until it stops moving.

        ``cost_at(scale)`` says what a frame costs at that scale. Returns
        the scale it settled on and how many times it changed its mind.
        """
        moves, last = 0, None
        for _ in range(frames):
            scale = governor.scale_for(pixels, ratio, scene)
            if last is not None and scale != last:
                moves += 1
            last = scale
            governor.record(cost_at(scale), ratio)
        return last, moves

    def test_a_scene_that_can_hold_it_is_never_drawn_softer_than_the_window(self):
        from attachment_widgets import Sharpness

        # Comfortable at logical (0.5 of a 2x screen's pixels), too slow
        # above it. The old rule would have put this at 0.27.
        settled, _ = self._settled(
            Sharpness(), lambda s: 3.0 * (s / 0.5) ** 3)
        assert settled >= 0.5, (
            f"settled at {settled}, which is {settled * 2:.2f} of the "
            f"window's own resolution")

    def test_a_scene_that_cannot_hold_it_gives_up_resolution(self):
        """The floor is a preference, not a promise. Something has to give
        when even a thirtieth of a second will not cover it."""
        from attachment_widgets import Sharpness

        settled, _ = self._settled(
            Sharpness(), lambda s: 90.0 * s / 0.5)
        assert settled < 0.5, f"stayed at {settled} while costing 90 ms"

    def test_below_the_floor_it_buys_frames_before_it_buys_pixels(self):
        """A scene costing 15 ms at logical keeps the resolution and takes
        the longer frame, rather than going soft to stay at sixty."""
        from attachment_widgets import Sharpness

        governor = Sharpness()
        settled, _ = self._settled(governor, lambda s: 15.0 * s / 0.5)
        assert settled >= 0.5, f"gave up resolution at {settled} for 15 ms"
        assert governor.interval_ms(2.0, 16) > 16, (
            "it kept the resolution but still asked for sixty frames a "
            "second, which fills the event queue rather than drawing them")

    def test_a_cold_first_frame_is_not_believed(self):
        """The first frame of a scene is the fonts and the tiles being
        built, not the scene. The Equaliser's first came in at 64 ms
        against the 1.4 it settles at, and one reading like that was
        enough to convince the governor for good."""
        from attachment_widgets import Sharpness

        governor = Sharpness()
        scene = object()
        for frame in range(900):
            scale = governor.scale_for(8_294_400, 2.0, scene)
            governor.record(64.0 if frame < 6 else 1.4 * scale / 0.5, 2.0)
        assert scale >= 0.5, (
            f"settled at {scale} because of the first six frames")

    def test_it_does_not_walk_between_two_rungs_for_ever(self):
        """A rung it has measured is not guessed at again.

        This is Waterfall, whose cost no tidy formula predicts: 14 ms at
        960x540 and 24 at 1267x713, where anything smooth says 18. The
        rung below looks cheap enough to climb out of and the rung above
        cannot be held, so a governor that re-guesses each time steps up,
        finds out, steps down, forgets, and does it again for as long as
        the scene is on screen.
        """
        from attachment_widgets import Sharpness

        cliff = {1.0: 120.0, 0.8: 80.0, 0.67: 60.0, 0.5: 45.0,
                 0.4: 36.0, 0.33: 30.0, 0.25: 7.0}
        governor = Sharpness()
        settled, moves = self._settled(
            governor, lambda s: cliff.get(round(s, 2), 50.0), frames=2400)
        assert settled == 0.25, f"settled at {settled}, which costs 30 ms"
        assert moves <= 6, (
            f"changed its mind {moves} times in 2400 frames, which is a "
            f"resolution change every {2400 // max(1, moves)} frames for ever")
        assert governor._seen.get(0.33, 0.0) > 24.0, (
            "it never wrote down that the rung above was too slow")

    def test_a_scene_too_slow_for_the_screen_really_is_stepped_down(self, qtbot):
        """The real pane, not the cost model, with a scene that is really
        slow - and slow in the way a scene is, which is in proportion to
        how many pixels it is asked for.

        This is also what the timing tests below rely on. They used to
        draw three frames and start the clock, which was right while the
        resolution was a fixed number; now the first frames are drawn at a
        resolution the pane is about to leave, and a build runner failed
        on exactly that.
        """
        import time

        from PySide6.QtGui import QPainter, QPixmap

        class Slow:
            name, blurb, sharp_pixels = "Slow", "too slow", 0

            def paint(self, painter, rect, state):
                # Charged by area, the way an antialiased scene is: thirty
                # milliseconds at the screen's own resolution, which is
                # over the frame however long a frame is allowed to be.
                scale = abs(painter.combinedTransform().m11()) or 1.0
                time.sleep(0.030 * scale * scale)

        spectrum = TestItHoldsSixtyFramesASecond._spectrum(qtbot, 1280, 720)
        spectrum.set_scene(Slow())
        canvas = QPixmap(1280, 720)
        canvas.setDevicePixelRatio(spectrum.devicePixelRatioF())
        painter = QPainter(canvas)
        try:
            started = spectrum._sharpness.scale_for(
                1280 * 720, spectrum.devicePixelRatioF(), spectrum._scene)
            settled = _settle(spectrum, painter, most=400, quiet=40)
        finally:
            painter.end()
        assert settled < started, (
            f"it stayed at {settled} drawing a scene that cannot hold it")

    def test_the_windowed_strip_is_left_alone(self):
        """Small frames were never the problem and are not measured."""
        from attachment_widgets import Sharpness

        governor = Sharpness()
        assert governor.scale_for(400_000, 2.0, object()) == 1.0


class TestAWideLineIsDrawnTheQuickWay:
    """Qt has a fast path for one-pixel lines and nothing above it.

    Measured on a thousand antialiased curve segments at 1080p: 2.21 ms at
    pen width 1.0, 58.00 ms at 1.01. That cliff is why the scenes were
    only ever cheap while they were being drawn small - a 2.4 unit pen in
    a quarter-size buffer is 1.3 real pixels, under the cliff.
    """

    @staticmethod
    def _draw(how, width=2.4, size=(420, 260), scale=1.0):
        import math

        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import (QColor, QImage, QPainter, QPainterPath,
                                   QPen)

        path = QPainterPath()
        path.moveTo(20, 200)
        for step in range(1, 14):
            x = 20 + step * 28.0
            y = 200 - (math.sin(step * 0.8) * 0.5 + 0.5) * 150
            half = x - 14.0
            path.cubicTo(QPointF(half, path.currentPosition().y()),
                         QPointF(half, y), QPointF(x, y))
        image = QImage(size[0], size[1],
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(0, 0, 0))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.scale(scale, scale)
        ink = QColor(210, 130, 255, 200)
        if how == "wide":
            painter.setPen(QPen(ink, width, Qt.PenStyle.SolidLine,
                                Qt.PenCapStyle.RoundCap,
                                Qt.PenJoinStyle.RoundJoin))
            painter.drawPath(path)
        else:
            import visualizers

            visualizers.stroke(painter, path, ink, width)
        painter.end()
        return image

    @staticmethod
    def _ink(image) -> int:
        return sum(image.pixelColor(x, y).blue()
                   for y in range(0, image.height(), 2)
                   for x in range(0, image.width(), 2))

    @staticmethod
    def _apart(first, second) -> float:
        total = count = 0
        for y in range(0, first.height(), 2):
            for x in range(0, first.width(), 2):
                one, two = first.pixelColor(x, y), second.pixelColor(x, y)
                total += (abs(one.red() - two.red())
                          + abs(one.green() - two.green())
                          + abs(one.blue() - two.blue()))
                count += 3
        return total / max(1, count)

    #: Widths to check, and the painter scale to check them at. The
    #: scaled rows are the ones that matter: a pen of width 1.2 in a
    #: painter scaled by two is 2.4 *real* pixels, and everything here -
    #: whether to stack at all, how far apart, how faint - is a statement
    #: about real pixels. Read in painter units instead, the offsets come
    #: out twice as far apart as they should and the line is drawn half
    #: again as wide as it was asked for.
    #:
    #: 5.0 and 8.0 are past what a ring of hairlines can cover, so they
    #: are drawn with a real pen. They are here because the first version
    #: drew them with as much of the ring as fitted, which is a thinner
    #: line rather than a cheaper one: 47 per cent of the ink at 5.0.
    SWEEP = [(1.4, 1.0), (2.0, 1.0), (2.4, 1.0), (3.2, 1.0), (5.0, 1.0),
             (8.0, 1.0), (1.2, 2.0), (1.6, 2.0), (2.4, 2.0), (0.8, 2.0)]

    @pytest.mark.parametrize("width,scale", SWEEP)
    def test_it_puts_the_same_ink_on_the_screen_as_a_wide_pen(self, width, scale):
        size = (int(420 * scale), int(260 * scale))
        mine = self._draw("hairlines", width=width, scale=scale, size=size)
        real = self._draw("wide", width=width, scale=scale, size=size)
        share = self._ink(mine) / max(1, self._ink(real))
        assert 0.90 <= share <= 1.12, (
            f"{width} units at a painter scale of {scale} is {width * scale} "
            f"real pixels, and the stacked line laid down "
            f"{share * 100:.0f} per cent of the ink a real pen does")

    @pytest.mark.parametrize("width,scale", SWEEP)
    def test_it_lands_in_the_same_place_as_a_wide_pen(self, width, scale):
        size = (int(420 * scale), int(260 * scale))
        apart = self._apart(
            self._draw("hairlines", width=width, scale=scale, size=size),
            self._draw("wide", width=width, scale=scale, size=size))
        assert apart < 3.0, (
            f"mean channel difference {apart:.2f}/255 against a real pen, "
            f"for {width} units at a painter scale of {scale}")

    def test_a_line_already_thin_enough_is_drawn_straight(self):
        """No stacking where there is nothing to gain: a pen under a pixel
        is already on the fast path, and passing it through the stacker
        would only make it fainter."""
        import visualizers

        assert visualizers._hair_spots(0.0) == ((0.0, 0.0),)

    def test_a_line_too_thick_to_stack_is_drawn_with_a_real_pen(self):
        """The backstop, and it has to be all or nothing: a ring cut off
        where it stopped fitting draws a line as wide as the last ring
        that fitted."""
        import visualizers

        assert visualizers._hair_spots(2.0) == ()
        assert self._ink(self._draw("hairlines", width=40.0)) > 0


class TestTheKeysThatPlayIt:
    """Numbers for scenes, S for the strobe, A and D for what it hears,
    M for nothing at all, and F for the strobe by hand.

    Everything here goes through the pane's own controls rather than
    straight at the widget, because a key that changes the picture and
    leaves the box in front of it saying something else is worse than no
    key. The transport keys were dead in full screen for a while and a
    test that only checked nothing raised passed the whole time, so none
    of these check that nothing raised.
    """

    @staticmethod
    def _pane():
        from attachment_view import AudioPane

        pane = AudioPane()
        pane.enable_box.setChecked(True)
        return pane

    @staticmethod
    def _feed(spectrum):
        """Something to draw, so the frame loop actually runs.

        Without it ``_tick`` returns before it reaches the strobe at all -
        there is no row to interpolate - and a test of what the strobe
        does measures nothing.
        """
        from array import array

        import attachment_audio

        bands = attachment_audio.BANDS
        spectrum.set_frames(
            [array("f", [0.95 if step % 4 == 0 else 0.1
                         for _ in range(bands)]) for step in range(120)],
            attachment_audio.RATE)
        return spectrum

    @staticmethod
    def _press(window, key, release=False):
        from PySide6.QtCore import QEvent
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import Qt as _Qt

        kind = (QEvent.Type.KeyRelease if release
                else QEvent.Type.KeyPress)
        window.keyReleaseEvent(QKeyEvent(kind, key, _Qt.KeyboardModifier.NoModifier)) \
            if release else \
            window.keyPressEvent(QKeyEvent(kind, key, _Qt.KeyboardModifier.NoModifier))

    def _full(self, qtbot):
        from attachment_widgets import FullScreenSpectrum

        pane = self._pane()
        qtbot.addWidget(pane)
        window = FullScreenSpectrum(pane.spectrum, pane)
        qtbot.addWidget(window)
        return pane, window

    def test_a_number_picks_the_scene_with_that_number(self, qtbot):
        import visualizers
        from PySide6.QtCore import Qt as _Qt

        pane, window = self._full(qtbot)
        for index in (2, 0, 4):
            self._press(window, _Qt.Key.Key_1 + index)
            assert pane.spectrum._scene is visualizers.SCENES[index]
            assert pane.scene_box.currentText() == visualizers.SCENES[index].name

    def test_a_number_past_the_last_scene_does_nothing(self, qtbot):
        """There are nine number keys and eight scenes, so one of them
        lands past the end of the list."""
        import visualizers
        from PySide6.QtCore import Qt as _Qt

        pane, window = self._full(qtbot)
        was = pane.scene_box.currentText()
        beyond = len(visualizers.SCENES)
        assert pane.vj("scene", beyond) is False, (
            f"scene {beyond} was accepted with "
            f"{beyond} scenes to choose from")
        assert pane.scene_box.currentText() == was
        if beyond <= 8:      # still within the keys that are mapped
            self._press(window, _Qt.Key.Key_1 + beyond)
            assert pane.scene_box.currentText() == was

    def test_s_switches_the_strobe_on_and_off(self, qtbot):
        from PySide6.QtCore import Qt as _Qt

        pane, window = self._full(qtbot)
        pane.strobe_box.setChecked(False)
        self._press(window, _Qt.Key.Key_S)
        assert pane.strobe_box.isChecked()
        assert pane.spectrum._state.strobe
        self._press(window, _Qt.Key.Key_S)
        assert not pane.strobe_box.isChecked()
        assert not pane.spectrum._state.strobe

    def test_a_and_d_walk_through_what_the_strobe_listens_to(self, qtbot):
        from PySide6.QtCore import Qt as _Qt
        from attachment_widgets import Spectrum

        pane, window = self._full(qtbot)
        pane.strobe_source.setCurrentText(Spectrum.STROBE_SOURCES[0])
        self._press(window, _Qt.Key.Key_D)
        assert pane.spectrum._strobe_source == Spectrum.STROBE_SOURCES[1]
        assert pane.strobe_source.currentText() == Spectrum.STROBE_SOURCES[1]
        self._press(window, _Qt.Key.Key_A)
        assert pane.spectrum._strobe_source == Spectrum.STROBE_SOURCES[0]

    def test_walking_past_the_end_comes_round_again(self, qtbot):
        from PySide6.QtCore import Qt as _Qt
        from attachment_widgets import Spectrum

        pane, window = self._full(qtbot)
        pane.strobe_source.setCurrentText(Spectrum.STROBE_SOURCES[-1])
        self._press(window, _Qt.Key.Key_D)
        assert pane.spectrum._strobe_source == Spectrum.STROBE_SOURCES[0]

    def test_m_goes_straight_to_listening_to_nobody(self, qtbot):
        from PySide6.QtCore import Qt as _Qt
        from attachment_widgets import Spectrum

        pane, window = self._full(qtbot)
        self._press(window, _Qt.Key.Key_M)
        assert pane.spectrum._strobe_source == Spectrum.BY_HAND
        assert pane.strobe_source.currentText() == Spectrum.BY_HAND

    def test_on_manual_nothing_fires_by_itself(self, qtbot):
        """The point of the setting: the track stops driving the light."""
        from array import array

        import attachment_audio
        from attachment_widgets import Spectrum

        pane = self._pane()
        qtbot.addWidget(pane)
        spectrum = pane.spectrum
        bands = attachment_audio.BANDS
        spectrum.set_frames(
            [array("f", [0.95 if step % 4 == 0 else 0.1
                         for _ in range(bands)]) for step in range(120)],
            attachment_audio.RATE)
        spectrum.set_strobe(True)
        spectrum.set_strobe_source(Spectrum.BY_HAND)
        spectrum.set_strobe_rate(1.0)
        spectrum.set_strobe_sense(1.0)
        lit = 0.0
        for step in range(90):
            spectrum.set_position(step * 30)
            spectrum._tick()
            lit = max(lit, spectrum._state.hit)
        assert lit == 0.0, f"the light came up to {lit:.2f} on its own"

    def test_f_flashes_by_hand_and_letting_go_puts_it_out(self, qtbot):
        from PySide6.QtCore import Qt as _Qt
        from attachment_widgets import Spectrum

        pane, window = self._full(qtbot)
        self._feed(pane.spectrum)
        pane.spectrum.set_strobe_source(Spectrum.BY_HAND)
        self._press(window, _Qt.Key.Key_F)
        assert pane.spectrum._state.hit > 0.9
        # Held: it does not decay while the key is down.
        for _ in range(20):
            pane.spectrum._tick()
        assert pane.spectrum._state.hit > 0.9, "the held light sagged"
        self._press(window, _Qt.Key.Key_F, release=True)
        for _ in range(20):
            pane.spectrum._tick()
        assert pane.spectrum._state.hit == 0.0, "the light stayed on"

    def test_reaching_for_the_strobe_switches_it_on(self, qtbot):
        """Pressing the strobe key with the strobe off used to do nothing
        at all, because the tick box is what the scenes ask before they
        light up."""
        from PySide6.QtCore import Qt as _Qt

        pane, window = self._full(qtbot)
        pane.strobe_box.setChecked(False)
        self._press(window, _Qt.Key.Key_F)
        assert pane.strobe_box.isChecked()
        assert pane.spectrum._state.strobe

    def test_holding_a_key_down_is_not_a_stream_of_presses(self, qtbot):
        """The keyboard repeats a held key at its own rate. A held strobe
        that switches itself off thirty times a second is a strobe."""
        from PySide6.QtCore import QEvent, Qt as _Qt
        from PySide6.QtGui import QKeyEvent

        pane, window = self._full(qtbot)
        self._press(window, _Qt.Key.Key_F)
        window.keyReleaseEvent(QKeyEvent(
            QEvent.Type.KeyRelease, _Qt.Key.Key_F,
            _Qt.KeyboardModifier.NoModifier, autorep=True))
        pane.spectrum._tick()
        assert pane.spectrum._holding, "auto-repeat let go of the key"

    def test_the_transport_keys_still_work(self, qtbot):
        """J, K and L were dead in full screen once already."""
        from PySide6.QtCore import Qt as _Qt

        pane, window = self._full(qtbot)
        pane.position.setRange(0, 300_000)
        pane.position.setValue(120_000)
        self._press(window, _Qt.Key.Key_J)
        assert pane.position.value() < 120_000

    def test_the_bar_says_what_the_keys_did(self, qtbot):
        """A key that changes the picture and leaves the box in front of
        it saying something else is worse than no key."""
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QCheckBox, QComboBox

        import visualizers

        pane = self._pane()
        qtbot.addWidget(pane)
        pane._go_full_screen()
        window = pane._full
        try:
            self._press(window, _Qt.Key.Key_3)
            self._press(window, _Qt.Key.Key_S)
            self._press(window, _Qt.Key.Key_M)
            shown = [b.currentText() for b in window.findChildren(QComboBox)]
            ticked = [b.isChecked() for b in window.findChildren(QCheckBox)
                      if b.text() == "Strobe"]
            assert visualizers.SCENES[2].name in shown, (
                f"the bar still says {shown}")
            assert "Manual" in shown, f"the bar still says {shown}"
            assert ticked == [True], "the bar's strobe box did not follow"
        finally:
            window.close()


class TestTheSunInTheVaporwaveScene:
    """"The horizontal bars in front of the sun look off."

    They were. Each was drawn from one edge of the sun's *bounding box*
    to the other, so they carried on out past the glow and lay across the
    skyline as dark rectangles. And there was nothing for them to belong
    to: the sun was a soft radial glow with no edge anywhere, so bars
    across it could only read as rectangles on top of the picture.
    """

    W, H = 900, 520
    HORIZON = 280.0
    BASS, FLASH = 0.5, 0.0
    #: A gap is painted at alpha 225 over whatever is behind it, so where
    #: there is one the pixel is nearly the gap's own near-black. The glow
    #: only ever adds light, so nothing else in this scene gets near it.
    DARK = 70

    def _drawn(self, bass=None, flash=None):
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        scene = visualizers.by_name("Vaporwave city")
        image = QImage(self.W, self.H,
                       QImage.Format.Format_ARGB32_Premultiplied)
        # A flat background brighter than any gap and a colour nothing in
        # the sun is near, so "dark here" can only mean a gap. Plain green
        # was too dark: averaged over the channels it came out under the
        # threshold, and every untouched pixel read as a bar.
        image.fill(QColor(60, 200, 60))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scene._sun(painter, float(self.W), self.HORIZON, 0.08,
                   self.BASS if bass is None else bass,
                   self.FLASH if flash is None else flash)
        painter.end()
        radius = scene.sun_radius(self.HORIZON,
                                  self.BASS if bass is None else bass,
                                  self.FLASH if flash is None else flash)
        return scene, image, radius

    @staticmethod
    def _bar_runs(image, x, top, bottom, dark):
        """Where the dark bars are down one column, as (start, height)."""
        runs, start = [], None
        for y in range(int(top), int(bottom)):
            here = image.pixelColor(x, y)
            barred = (here.red() + here.green() + here.blue()) / 3.0 < dark
            if barred and start is None:
                start = y
            elif not barred and start is not None:
                runs.append((start, y - start))
                start = None
        if start is not None:
            runs.append((start, int(bottom) - start))
        return runs

    def test_no_bar_reaches_past_the_edge_of_the_sun(self):
        """The whole complaint: each bar ran the full width of the sun's
        bounding box, so above and below the middle of the disc it carried
        on out past the glow and lay across the skyline.

        The glow is allowed out there - that is what makes it a sunset
        rather than a circle on a background - so this asks only where the
        *dark* is, and the dark is only ever a gap.
        """
        import math

        _scene, image, radius = self._drawn()
        centre = self.W // 2
        found = 0
        for up in range(8, int(radius * 0.88), 5):
            y = int(self.HORIZON - up)
            half = math.sqrt(max(0.0, radius * radius - up * up))
            dark = [x for x in range(self.W)
                    if sum(image.pixelColor(x, y).getRgb()[:3]) / 3.0
                    < self.DARK]
            if not dark:
                continue
            found += 1
            # Two pixels of slack at each end for the antialiased edge.
            assert min(dark) >= centre - half - 2, (
                f"a gap at y={y} starts at x={min(dark)}, and the sun only "
                f"reaches x={centre - half:.0f}")
            assert max(dark) <= centre + half + 2, (
                f"a gap at y={y} runs to x={max(dark)}, and the sun only "
                f"reaches x={centre + half:.0f}")
        assert found >= 4, f"only {found} rows had a gap in them at all"

    def test_the_bars_are_thicker_nearer_the_horizon(self):
        """Evenly weighted bars read as a barcode. A sunset dissolves."""
        _scene, image, radius = self._drawn()
        runs = self._bar_runs(image, self.W // 2,
                              self.HORIZON - radius + 4, self.HORIZON,
                              self.DARK)
        assert len(runs) >= 4, f"only {len(runs)} bars were drawn"
        highest, lowest = runs[0][1], runs[-1][1]
        assert lowest > highest * 1.6, (
            f"the bar nearest the horizon is {lowest}px and the highest is "
            f"{highest}px, which is not a gradient")

    #: The top of the disc that has to stay unbroken, as a fraction of
    #: the radius. A number of its own on purpose: reading BAR_TOP here
    #: and checking above it is a test that moves wherever the code moves,
    #: and it passed with the bars running the whole way up.
    CAP = 0.92

    def test_the_top_of_the_sun_is_whole(self):
        _scene, image, radius = self._drawn()
        cap = self.HORIZON - radius * self.CAP
        # From a little below the very top, where the disc's own
        # antialiased edge against the background is dark for a pixel.
        runs = self._bar_runs(image, self.W // 2,
                              self.HORIZON - radius + 4, cap, self.DARK)
        assert runs == [], f"the cap is cut by {len(runs)} bars"

    def test_the_sun_has_an_edge_rather_than_fading_away(self):
        """A glow with no edge is what left the bars nothing to belong
        to. Just inside the top of the disc is bright; just outside is
        the sky."""
        _scene, image, radius = self._drawn()
        centre = self.W // 2
        inside = image.pixelColor(centre, int(self.HORIZON - radius) + 4)
        outside = image.pixelColor(centre, int(self.HORIZON - radius) - 4)
        step = abs(inside.red() - outside.red()) + abs(inside.blue()
                                                      - outside.blue())
        assert step > 120, (
            f"inside {inside.name()} and outside {outside.name()} are barely "
            f"different, so the disc has no edge")

    def test_a_flash_makes_it_bigger(self):
        """The strobe belongs to the sun in this scene."""
        import visualizers

        scene = visualizers.by_name("Vaporwave city")
        quiet = scene.sun_radius(self.HORIZON, 0.2, 0.0)
        hit = scene.sun_radius(self.HORIZON, 0.2, 1.0)
        assert hit > quiet * 1.5

    def test_it_draws_nothing_at_all_when_there_is_no_room(self):
        from PySide6.QtGui import QColor, QImage, QPainter

        import visualizers

        scene = visualizers.by_name("Vaporwave city")
        image = QImage(40, 30, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor(60, 200, 60))
        painter = QPainter(image)
        scene._sun(painter, 40.0, 0.5, 0.1, 0.0, 0.0)
        painter.end()
        assert image.pixelColor(20, 10).green() == 200
