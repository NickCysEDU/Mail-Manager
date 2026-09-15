"""Attachments, which are the one part of a message that is a file.

Everything here assumes the sender chose the filename, the content type and
the bytes, and that none of the three agrees with the others.
"""

from __future__ import annotations

import email.message
from pathlib import Path

import pytest

import attachments


class _Signal:
    """The smallest thing that answers .connect(), for stand-in widgets."""

    def connect(self, *_args, **_kwargs):
        pass


class TestAFilenameIsASuggestion:
    """A name in a message is a string a stranger wrote."""

    @pytest.mark.parametrize("hostile, forbidden", [
        ("../../.ssh/authorized_keys", "/"),
        ("../../../etc/passwd", ".."),
        ("/etc/passwd", "/"),
        ("C:\\Windows\\System32\\evil.exe", "\\"),
        ("subdir/file.txt", "/"),
    ])
    def test_it_cannot_name_a_directory(self, hostile, forbidden):
        safe = attachments.safe_name(hostile)
        assert forbidden not in safe
        assert not Path(safe).is_absolute()
        assert safe not in ("", ".", "..")

    def test_a_null_byte_cannot_truncate_the_extension(self):
        """good.pdf\\0.exe is written by some tools as good.pdf."""
        assert "\x00" not in attachments.safe_name("good.pdf\x00.exe")

    def test_a_leading_dot_cannot_make_a_config_file(self):
        assert not attachments.safe_name(".bash_profile").startswith(".")
        assert not attachments.safe_name("...hidden").startswith(".")

    def test_an_empty_name_still_produces_one(self):
        assert attachments.safe_name("") == "attachment"
        assert attachments.safe_name("   ") == "attachment"
        assert attachments.safe_name("....") == "attachment"

    def test_a_long_name_fits_a_filesystem(self):
        made = attachments.safe_name("x" * 5000 + ".pdf")
        assert len(made.encode("utf-8")) <= attachments.MAX_NAME_BYTES

    def test_the_right_to_left_trick_is_defused(self):
        """U+202E makes photo<RLO>gnp.exe render as photo exe.png."""
        shown = attachments.display_name("photo\u202egnp.exe")
        assert "\u202e" not in shown
        assert shown.endswith(".exe"), "the label must not hide what it is"

    def test_a_label_cannot_draw_a_path(self):
        assert "/" not in attachments.display_name("../../.ssh/authorized_keys")

    def test_newlines_cannot_forge_a_second_line(self):
        shown = attachments.display_name("invoice.pdf\nSigned: your bank")
        assert "\n" not in shown


class TestTheBytesDecideWhatItIs:
    """A part declaring image/png that starts MZ is not a PNG."""

    @pytest.mark.parametrize("head, expected", [
        (b"MZ\x90\x00", "program"),
        (b"\x7fELF\x02\x01", "program"),
        (b"\xcf\xfa\xed\xfe", "program"),
        (b"#!/bin/sh\n", "program"),
    ])
    def test_a_program_is_never_called_an_image(self, head, expected):
        kind, _ = attachments.sniff(head + b"\x00" * 30, "image/png", "photo.png")
        assert kind == expected

    @pytest.mark.parametrize("head, expected", [
        (b"\x89PNG\r\n\x1a\n", "image"),
        (b"GIF89a", "image"),
        (b"\xff\xd8\xff\xe0", "image"),
        (b"%PDF-1.7", "pdf"),
        (b"ID3\x04", "audio"),
        (b"OggS\x00", "audio"),
        (b"fLaC\x00", "audio"),
        (b"PK\x03\x04", "archive"),
    ])
    def test_real_formats_are_recognised(self, head, expected):
        kind, _ = attachments.sniff(head + b"\x00" * 30)
        assert kind == expected

    def test_riff_is_split_between_wav_and_webp(self):
        assert attachments.sniff(b"RIFF\x00\x00\x00\x00WAVE")[0] == "audio"
        assert attachments.sniff(b"RIFF\x00\x00\x00\x00WEBP")[0] == "image"

    def test_an_unknown_binary_is_not_guessed_at(self):
        kind, _ = attachments.sniff(b"\x01\x02\x03\x00\xff" * 8)
        assert kind == "other"


class TestWhatTheViewerWillDoWithIt:
    def _one(self, name, content_type, data):
        return attachments.Attachment(part="1", name=name,
                                      content_type=content_type,
                                      size=len(data), data=data)

    def test_a_disguised_program_reports_itself(self):
        item = self._one("holiday.png", "image/png", b"MZ\x90\x00" + b"\x00" * 40)
        assert item.kind == "program"
        assert item.executable
        assert not item.viewable

    def test_an_archive_is_never_viewable(self):
        item = self._one("photos.zip", "application/zip", b"PK\x03\x04" + b"\x00" * 20)
        assert item.archive
        assert not item.viewable

    def test_a_signature_part_is_recognised(self):
        item = self._one("smime.p7s", "application/pkcs7-signature", b"\x30\x82\x00")
        assert item.signature

    @pytest.mark.parametrize("ext", ["app", "exe", "sh", "command", "pkg", "dmg"])
    def test_executable_extensions_warn_even_with_harmless_bytes(self, ext):
        item = self._one(f"thing.{ext}", "application/octet-stream", b"hello")
        assert item.executable

    def test_a_real_image_is_viewable(self):
        item = self._one("a.png", "image/png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
        assert item.kind == "image" and item.viewable

    def test_sizes_read_like_sizes(self):
        assert self._one("a", "x", b"x" * 500).human_size() == "500 bytes"
        assert "KB" in self._one("a", "x", b"x" * 5000).human_size()


class TestSavingDoesNotOverwrite:
    def test_a_collision_gets_a_number(self, tmp_path):
        (tmp_path / "report.pdf").write_bytes(b"first")
        second = attachments.unique_path(tmp_path, "report.pdf")
        assert second.name == "report (2).pdf"
        second.write_bytes(b"second")
        third = attachments.unique_path(tmp_path, "report.pdf")
        assert third.name == "report (3).pdf"
        assert (tmp_path / "report.pdf").read_bytes() == b"first"

    def test_a_name_without_an_extension_still_works(self, tmp_path):
        (tmp_path / "README").write_bytes(b"x")
        assert attachments.unique_path(tmp_path, "README").name == "README (2)"


class TestParsingARealMessage:
    @staticmethod
    def _message(parts):
        message = email.message.EmailMessage()
        message["From"] = "sender@example.example"
        message["Subject"] = "with attachments"
        message.set_content("the body")
        for name, maintype, subtype, data in parts:
            message.add_attachment(data, maintype=maintype, subtype=subtype,
                                   filename=name)
        return message.as_bytes()

    def test_every_part_comes_back_with_its_bytes(self):
        import imap_engine

        raw = self._message([
            ("a.png", "image", "png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20),
            ("b.pdf", "application", "pdf", b"%PDF-1.4" + b"\x00" * 20),
        ])
        found = imap_engine.attachments_of(raw)
        assert [a.filename for a in found] == ["a.png", "b.pdf"]
        assert all(a.data for a in found)
        assert found[0].kind == "image" and found[1].kind == "pdf"

    def test_a_traversing_name_is_safe_by_the_time_it_is_used(self):
        import imap_engine

        raw = self._message([
            ("../../.ssh/authorized_keys", "text", "plain", b"ssh-rsa AAAA"),
        ])
        found = imap_engine.attachments_of(raw)
        assert found[0].filename == "authorized_keys"
        assert "/" not in found[0].shown

    def test_a_broken_message_yields_nothing_rather_than_raising(self):
        import imap_engine

        assert imap_engine.attachments_of(b"") == []
        assert imap_engine.attachments_of(b"\x00\xff" * 500) == []

    def test_a_message_with_no_attachments_is_empty(self):
        import imap_engine

        assert imap_engine.attachments_of(self._message([])) == []


class TestDemoModeInventsItsOwn:
    """The repository ships no message data, so demo parts are generated."""

    def test_it_makes_something_for_each_name(self):
        class Message:
            attachments = ("offer.pdf", "photo.png", "notes.txt")

        made = attachments.demo_attachments(Message())
        assert [a.shown for a in made] == list(Message.attachments)
        assert made[0].kind == "pdf"
        assert made[1].kind == "image"
        assert made[2].kind == "text"

    def test_the_generated_pdf_and_png_are_real(self):
        class Message:
            attachments = ("x.pdf", "y.png")

        made = attachments.demo_attachments(Message())
        assert made[0].data.startswith(b"%PDF-")
        assert made[1].data.startswith(b"\x89PNG\r\n\x1a\n")


class TestTheViewerPicksASafePane:
    """Which pane opens is a security decision, not a convenience."""

    @staticmethod
    def _one(name, content_type, data):
        return attachments.Attachment(part="1", name=name,
                                      content_type=content_type,
                                      size=len(data), data=data)

    @staticmethod
    def _png(width=8, height=8):
        import struct
        import zlib

        def chunk(tag, body):
            piece = tag + body
            return (struct.pack(">I", len(body)) + piece
                    + struct.pack(">I", zlib.crc32(piece)))

        rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
        return (b"\x89PNG\r\n\x1a\n"
                + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))

    def _pane_for(self, qtbot, item):
        from attachment_view import AttachmentViewer

        viewer = AttachmentViewer([item])
        qtbot.addWidget(viewer)
        viewer.list.setCurrentRow(0)
        name = type(viewer.stack.currentWidget()).__name__
        warning = viewer.warning.text()
        viewer._sweep()
        return name, warning

    def test_a_program_dressed_as_a_png_never_reaches_the_decoder(self, qtbot):
        pane, warning = self._pane_for(
            qtbot, self._one("holiday.png", "image/png", b"MZ\x90\x00" + b"\x00" * 60))
        assert pane != "ImagePane"
        assert "program" in warning.lower()

    def test_svg_is_shown_as_text(self, qtbot):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>x</script></svg>'
        pane, warning = self._pane_for(qtbot, self._one("a.svg", "image/svg+xml", svg))
        assert pane == "TextPane"
        assert "remote" in warning.lower()

    def test_html_is_shown_as_text(self, qtbot):
        page = b"<html><body><img src='http://tracker.example/x.gif'></body></html>"
        pane, _ = self._pane_for(qtbot, self._one("a.html", "text/html", page))
        assert pane == "TextPane"

    def test_an_archive_is_described_not_opened(self, qtbot):
        pane, warning = self._pane_for(
            qtbot, self._one("a.zip", "application/zip", b"PK\x03\x04" + b"\x00" * 40))
        assert pane not in ("ImagePane", "TextPane", "PdfPane")
        assert "archive" in warning.lower()

    def test_a_real_image_opens_in_the_image_pane(self, qtbot):
        pane, warning = self._pane_for(
            qtbot, self._one("a.png", "image/png", self._png()))
        assert pane == "ImagePane"
        assert warning == ""

    def test_an_empty_part_does_not_crash(self, qtbot):
        pane, _ = self._pane_for(qtbot, self._one("a.png", "image/png", b""))
        assert pane is not None

    def test_a_giant_canvas_is_refused_rather_than_allocated(self, qtbot):
        """A few kilobytes of PNG can ask for a gigabyte of pixels."""
        from attachment_view import ImagePane

        pane = ImagePane()
        qtbot.addWidget(pane)
        import struct
        import zlib

        def chunk(tag, body):
            piece = tag + body
            return (struct.pack(">I", len(body)) + piece
                    + struct.pack(">I", zlib.crc32(piece)))

        # 30000 x 30000 = 900 megapixels declared in the header.
        header = struct.pack(">IIBBBBB", 30000, 30000, 8, 2, 0, 0, 0)
        bomb = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
                + chunk(b"IDAT", zlib.compress(b"\x00" * 1000)) + chunk(b"IEND", b""))
        status = pane.show_bytes(bomb)
        assert "refused" in status or "not a readable image" in status

    def test_closing_removes_what_it_wrote(self, qtbot, tmp_path):
        """Audio and PDF need a path on disk; it must not outlive the window."""
        from attachment_view import AttachmentViewer

        viewer = AttachmentViewer([self._one("a.mp3", "audio/mpeg",
                                             b"ID3\x04\x00" + b"\x00" * 200)])
        qtbot.addWidget(viewer)
        viewer.list.setCurrentRow(0)
        written = list(viewer._written)
        temp = viewer._temp
        assert written and all(p.exists() for p in written)
        assert all(p.stat().st_mode & 0o077 == 0 for p in written), \
            "a temp copy of somebody's attachment is not group or world readable"
        viewer._sweep()
        assert not any(p.exists() for p in written)
        assert not temp.exists()

    def test_nothing_in_the_viewer_opens_a_file_with_the_system(self):
        """No QDesktopServices, no subprocess, no os.startfile. Ever."""
        import inspect

        import attachment_view

        source = inspect.getsource(attachment_view)
        for forbidden in ("QDesktopServices", "startfile", "subprocess.Popen",
                          "subprocess.run", "os.system", "webbrowser"):
            assert forbidden not in source, f"{forbidden} would run an attachment"


class TestTheWindowOffersThem:
    """The button, and what it is wired to."""

    @staticmethod
    def _window(qtbot, demo=True):
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        window = MainWindow(Settings(icloud_email="you@icloud.example"),
                            InMemoryCredentialStore(), demo=demo)
        qtbot.addWidget(window)
        window._load_demo_data()
        return window

    def test_the_button_counts_what_is_attached(self, qtbot):
        window = self._window(qtbot)
        rows = [i for i, item in enumerate(window.model.items)
                if item.email.attachments]
        assert rows, "the sample data has no attachments to offer"
        window.table.selectRow(rows[0])
        expected = len(window.model.items[rows[0]].email.attachments)
        assert window.preview.attachments_button.isEnabled()
        assert f"({expected})" in window.preview.attachments_button.text()

    def test_it_is_off_when_nothing_is_attached(self, qtbot):
        window = self._window(qtbot)
        rows = [i for i, item in enumerate(window.model.items)
                if not item.email.attachments]
        assert rows
        window.table.selectRow(rows[0])
        assert not window.preview.attachments_button.isEnabled()

    def test_pressing_it_asks_the_window_rather_than_the_server(self, qtbot):
        """The pane has no connection of its own, and should not get one."""
        window = self._window(qtbot)
        rows = [i for i, item in enumerate(window.model.items)
                if item.email.attachments]
        window.table.selectRow(rows[0])
        seen = []
        window.preview.attachmentsRequested.connect(seen.append)
        window.preview.attachments_button.click()
        assert seen, "the button emitted nothing"

    def test_demo_mode_never_reaches_for_a_mailbox(self, qtbot, monkeypatch):
        import workers

        def explode(*_args, **_kwargs):
            raise AssertionError("demo mode tried to open a connection")

        monkeypatch.setattr(workers, "AttachmentWorker", explode)
        window = self._window(qtbot, demo=True)
        rows = [i for i, item in enumerate(window.model.items)
                if item.email.attachments]
        window.table.selectRow(rows[0])
        shown = {}
        import attachment_view

        class Fake:
            """Stands in for the viewer, which is a window now, not a modal.

            It has to answer the handful of things _present_attachments does
            to it: the viewer is shown rather than exec'd so that Quit is not
            swallowed while it is up.
            """

            finished = _Signal()

            def __init__(self, found, subject="", parent=None, fetch=None):
                shown["found"] = found
                shown["subject"] = subject
                shown["fetch"] = fetch

            def setAttribute(self, *_args):      # noqa: N802 - Qt's name
                pass

            def show(self):
                shown["shown"] = True

            def raise_(self):
                pass

            def activateWindow(self):            # noqa: N802 - Qt's name
                pass

            def close(self):
                pass

        monkeypatch.setattr(attachment_view, "AttachmentViewer", Fake)
        window._open_attachments(rows[0])
        assert shown.get("found"), "demo mode produced no attachments"
        assert all(a.data for a in shown["found"])
        assert shown.get("shown"), "the viewer was never shown"
        assert shown.get("fetch") is None, "demo mode passed a fetcher"

    def test_without_a_password_it_says_so_instead_of_hanging(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QMessageBox

        window = self._window(qtbot, demo=False)
        rows = [i for i, item in enumerate(window.model.items)
                if item.email.attachments]
        window.table.selectRow(rows[0])
        told = {}
        monkeypatch.setattr(QMessageBox, "information",
                            lambda *a, **k: told.setdefault("said", a[2] if len(a) > 2 else ""))
        window._open_attachments(rows[0])
        assert "not connected" in told.get("said", "").lower()


class TestTheWorkerUsesTheRealAccountFields:
    """It asked for account.imap_host, which Account has never had.

    Nothing caught it: the GUI test covers the path where no password is
    stored, which returns before the worker is built, and no test had ever
    started one. On a configured mailbox the button raised AttributeError
    inside the thread and the window showed nothing at all.
    """

    def test_account_has_the_fields_the_worker_reads(self):
        import inspect

        from accounts import Account
        import workers

        source = inspect.getsource(workers.AttachmentWorker)
        assert "imap_host" not in source and "imap_port" not in source, (
            "Account has host and port; imap_host is Settings' name for a "
            "different thing")
        for field in ("host", "port", "address", "source_mailbox"):
            assert hasattr(Account(), field), field

    def test_it_reaches_the_engine_with_the_account_host(self, monkeypatch):
        """Run it far enough to prove the attributes resolve."""
        import workers
        from accounts import Account

        asked = {}

        class FakeEngine:
            def __init__(self, host="", port=0):
                asked["host"] = host
                asked["port"] = port

            def connect(self, address, password):
                asked["address"] = address
                asked["password"] = password

            def select(self, mailbox, readonly=True):
                asked["mailbox"] = mailbox

            def describe_attachments(self, uid):
                asked["uid"] = uid
                raise RuntimeError("stop here, the point is already made")

        monkeypatch.setattr(workers, "IMAPEngine", FakeEngine)

        class Message:
            uid = "1"
            source_folder = "INBOX"

        account = Account(label="x", address="someone@example.example",
                          host="imap.example.example", port=993)
        worker = workers.AttachmentWorker(account, "secret", Message())
        failures = []
        worker.failed.connect(failures.append)
        worker.run()
        assert asked.get("host") == "imap.example.example"
        assert asked.get("port") == 993
        assert asked.get("address") == "someone@example.example", (
            "the worker read the wrong field off Account")
        assert asked.get("mailbox") == "INBOX"
        assert asked.get("uid") == "1"
        assert failures, "a failure should be reported, not swallowed"
        # The first version of this test only asked that *a* failure arrived,
        # and passed while the worker was raising AttributeError on a field
        # Account does not have. A failure is only acceptable if it is the one
        # the fake engine raised on purpose.
        assert "stop here" in failures[0], (
            f"the worker failed for its own reasons, not the test's: {failures[0]}")
        assert "AttributeError" not in failures[0]
        assert "secret" not in failures[0], "the password must not be in the message"


class TestTheServerSPartListIsBelievedOverTheTruncatedFetch:
    """A scan downloads 64 KB. A message can be six megabytes.

    Counting attachments from the bytes that arrived means counting the ones
    that happened to begin inside the first 64 KB - which for a real message
    reported one of three. BODYSTRUCTURE describes every part without
    sending any of them, and costs nothing extra in the same FETCH.
    """

    # A real-shaped reply: multipart/mixed holding text, an image, and a PDF.
    REPLY = (
        b'1 (UID 101 RFC822.SIZE 6000000 BODYSTRUCTURE ('
        b'("text" "plain" ("charset" "UTF-8") NIL NIL "7bit" 2 1 NIL NIL NIL NIL)'
        b'("image" "jpeg" ("name" "photo.jpeg") NIL NIL "base64" 1400000 NIL '
        b'("inline" ("filename" "photo.jpeg")) NIL NIL)'
        b'("application" "pdf" ("name" "paper.pdf") NIL NIL "base64" 1000000 NIL '
        b'("attachment" ("filename" "paper.pdf")) NIL NIL)'
        b' "mixed" ("boundary" "xyz") NIL NIL NIL))'
    )

    def test_every_leaf_part_is_described(self):
        import imap_engine

        parts = imap_engine.parse_bodystructure(self.REPLY)
        assert len(parts) == 3
        assert [p["content_type"] for p in parts] == [
            "text/plain", "image/jpeg", "application/pdf"]
        assert [p["size"] for p in parts] == [2, 1400000, 1000000]

    def test_filenames_come_off_the_disposition(self):
        import imap_engine

        parts = imap_engine.parse_bodystructure(self.REPLY)
        assert parts[1]["name"] == "photo.jpeg"
        assert parts[2]["name"] == "paper.pdf"
        assert parts[1]["disposition"] == "inline"
        assert parts[2]["disposition"] == "attachment"

    def test_the_body_is_not_counted_as_an_attachment(self):
        import imap_engine

        names = imap_engine.attachment_names(
            imap_engine.parse_bodystructure(self.REPLY))
        assert names == ("photo.jpeg", "paper.pdf")

    def test_a_nested_multipart_is_numbered_the_way_imap_numbers_it(self):
        import imap_engine

        nested = (
            b'BODYSTRUCTURE (('
            b'("text" "plain" NIL NIL NIL "7bit" 10 1 NIL NIL NIL NIL)'
            b'("text" "html" NIL NIL NIL "7bit" 20 1 NIL NIL NIL NIL)'
            b' "alternative" NIL NIL NIL NIL)'
            b'("application" "zip" ("name" "a.zip") NIL NIL "base64" 999 NIL '
            b'("attachment" ("filename" "a.zip")) NIL NIL)'
            b' "mixed" NIL NIL NIL NIL)'
        )
        parts = imap_engine.parse_bodystructure(nested)
        assert [p["part"] for p in parts] == ["1.1", "1.2", "2"]
        assert imap_engine.attachment_names(parts) == ("a.zip",)

    @pytest.mark.parametrize("junk", [
        b"", b"BODYSTRUCTURE", b"BODYSTRUCTURE (", b"BODYSTRUCTURE ((((",
        b"BODYSTRUCTURE NIL", b"\x00\xff" * 200,
        b'BODYSTRUCTURE ("text" "plain" NIL NIL NIL "7bit" notanumber 1)',
    ])
    def test_a_broken_reply_yields_nothing_rather_than_raising(self, junk):
        import imap_engine

        assert isinstance(imap_engine.parse_bodystructure(junk), list)

    def test_a_literal_filename_is_read(self):
        """Servers send long or non-ASCII names as {n}-prefixed literals."""
        import imap_engine

        reply = (
            b'BODYSTRUCTURE (("application" "pdf" ("name" {11}\r\nquarter.pdf) '
            b'NIL NIL "base64" 100 NIL ("attachment" ("filename" {11}\r\n'
            b'quarter.pdf)) NIL NIL) "mixed" NIL NIL NIL NIL)'
        )
        parts = imap_engine.parse_bodystructure(reply)
        assert parts and parts[0]["name"] == "quarter.pdf"

    def test_a_hostile_filename_is_still_safe_once_displayed(self):
        import imap_engine

        reply = (
            b'BODYSTRUCTURE (("application" "octet-stream" '
            b'("name" "../../.ssh/authorized_keys") NIL NIL "base64" 10 NIL '
            b'("attachment" ("filename" "../../.ssh/authorized_keys")) NIL NIL)'
            b' "mixed" NIL NIL NIL NIL)'
        )
        names = imap_engine.attachment_names(imap_engine.parse_bodystructure(reply))
        assert names and "/" not in names[0]
