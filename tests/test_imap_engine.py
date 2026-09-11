"""IMAP protocol handling, message parsing, and the move pipeline."""

from __future__ import annotations

import imaplib
import threading
from datetime import datetime, timedelta, timezone

import pytest

from conftest import build_mime
from imap_engine import (
    IMAPAuthError,
    IMAPConnectionError,
    IMAPEngine,
    IMAPError,
    MailboxInfo,
    MovePlan,
    ScanCancelled,
    decode_mutf7,
    encode_mutf7,
    extract_body,
    parse_internaldate,
    parse_list_line,
    parse_message,
    quote_mailbox,
)
from models import FolderPlan

UTC = timezone.utc


# ==========================================================================
# Modified UTF-7
# ==========================================================================
class TestModifiedUtf7:
    @pytest.mark.parametrize(
        "plain,encoded",
        [
            ("INBOX", "INBOX"),
            ("Job Search/Interview", "Job Search/Interview"),
            ("Ampersand & Co", "Ampersand &- Co"),
            ("Résumé", "R&AOk-sum&AOk-"),
            ("受信箱", "&U9dP4Xux-"),
            ("", ""),
        ],
    )
    def test_encode(self, plain, encoded):
        assert encode_mutf7(plain) == encoded

    @pytest.mark.parametrize(
        "plain",
        ["INBOX", "Job Search/Next Steps", "Résumé & Notes", "受信箱", "Ünïcödé/Ünterordner", "&&&"],
    )
    def test_round_trip(self, plain):
        assert decode_mutf7(encode_mutf7(plain)) == plain

    def test_decodes_a_real_server_name(self):
        assert decode_mutf7("&U9dP4Xux-") == "受信箱"

    def test_unterminated_shift_sequence_does_not_crash(self):
        assert decode_mutf7("Broken&AOk") == "Broken&AOk"

    def test_undecodable_sequence_is_passed_through(self):
        assert "&" in decode_mutf7("Bad&!!!!-name")


class TestQuoteMailbox:
    def test_quotes_and_escapes(self):
        assert quote_mailbox("Job Search/Interview") == '"Job Search/Interview"'
        assert quote_mailbox('Odd"Name') == '"Odd\\"Name"'
        assert quote_mailbox("Back\\slash") == '"Back\\\\slash"'

    def test_encodes_non_ascii(self):
        assert quote_mailbox("Résumé") == '"R&AOk-sum&AOk-"'


# ==========================================================================
# Response parsing
# ==========================================================================
class TestParseListLine:
    def test_quoted_name(self):
        info = parse_list_line(rb'(\HasNoChildren) "/" "Job Search/Interview"')
        assert info == MailboxInfo("Job Search/Interview", "/", ("\\HasNoChildren",))

    def test_unquoted_name(self):
        info = parse_list_line(rb'(\HasNoChildren) "/" INBOX')
        assert info.name == "INBOX"

    def test_multiple_flags(self):
        info = parse_list_line(rb'(\Noselect \HasChildren) "." "Parent"')
        assert info.flags == ("\\Noselect", "\\HasChildren")
        assert info.delimiter == "."
        assert info.selectable is False

    def test_nil_delimiter(self):
        info = parse_list_line(rb'(\Noinferiors) NIL "Flat"')
        assert info.delimiter == ""

    def test_non_ascii_name_is_decoded(self):
        info = parse_list_line(rb'(\HasNoChildren) "/" "R&AOk-sum&AOk-"')
        assert info.name == "Résumé"

    def test_escaped_quote_in_name(self):
        info = parse_list_line(rb'(\HasNoChildren) "/" "Odd\"Name"')
        assert info.name == 'Odd"Name'

    def test_literal_form(self):
        info = parse_list_line((rb'(\HasNoChildren) "/" {7}', b"Archive"))
        assert info.name == "Archive"

    def test_accepts_str_input(self):
        assert parse_list_line(r'(\HasNoChildren) "/" "INBOX"').name == "INBOX"

    @pytest.mark.parametrize("line", [None, b"", b"garbage", b"* OK something"])
    def test_unparseable_lines_return_none(self, line):
        assert parse_list_line(line) is None


class TestParseInternaldate:
    def test_converts_to_utc(self):
        blob = b'1 (UID 55 INTERNALDATE "17-Jul-2026 12:34:56 -0700" FLAGS ())'
        assert parse_internaldate(blob) == datetime(2026, 7, 17, 19, 34, 56, tzinfo=UTC)

    def test_positive_offset(self):
        blob = b'INTERNALDATE "01-Jan-2026 01:00:00 +0200"'
        assert parse_internaldate(blob) == datetime(2025, 12, 31, 23, 0, tzinfo=UTC)

    def test_single_digit_day_is_space_padded(self):
        blob = b'INTERNALDATE " 4-Sep-2026 09:00:00 +0000"'
        assert parse_internaldate(blob) == datetime(2026, 9, 4, 9, 0, tzinfo=UTC)

    @pytest.mark.parametrize(
        "blob",
        [b"", b"no date here", b'INTERNALDATE "32-Xyz-2026 00:00:00 +0000"',
         b'INTERNALDATE "31-Feb-2026 00:00:00 +0000"'],
    )
    def test_bad_input_returns_none(self, blob):
        assert parse_internaldate(blob) is None


# ==========================================================================
# Message parsing
# ==========================================================================
class TestParseMessage:
    def test_basic_headers(self):
        raw = build_mime(subject="Interview invitation", sender="Dana Reyes <dana@x.example>")
        message = parse_message(raw, uid="42")
        assert message.uid == "42"
        assert message.subject == "Interview invitation"
        assert message.sender_name == "Dana Reyes"
        assert message.sender_email == "dana@x.example"
        assert "Plain body text" in message.body_text

    def test_rfc2047_encoded_subject_is_decoded(self):
        raw = build_mime(subject="Résumé reçu — félicitations")
        assert parse_message(raw, "1").subject == "Résumé reçu — félicitations"

    def test_rfc2047_encoded_sender_name(self):
        raw = build_mime(sender='"=?utf-8?B?RGFuYSBSw6l5ZXM=?=" <dana@x.example>')
        assert parse_message(raw, "1").sender_name == "Dana Réyes"

    def test_internaldate_wins_over_the_date_header(self):
        raw = build_mime(date="Fri, 04 Sep 2026 12:34:56 -0700")
        internal = datetime(2026, 1, 1, tzinfo=UTC)
        assert parse_message(raw, "1", internaldate=internal).date == internal

    def test_date_header_is_the_fallback(self):
        raw = build_mime(date="Fri, 04 Sep 2026 12:34:56 -0700")
        assert parse_message(raw, "1").date == datetime(2026, 9, 4, 19, 34, 56, tzinfo=UTC)

    def test_missing_date_is_tolerated(self):
        import re as _re

        raw = _re.sub(rb"(?m)^Date:.*\r?\n", b"", build_mime())
        assert b"Date:" not in raw
        assert parse_message(raw, "1").date is None

    def test_html_only_message_is_reduced(self):
        raw = build_mime(
            plain=None,
            html='<html><body><p>Book a slot</p><a href="https://calendly.com/x">here</a></body></html>',
        )
        message = parse_message(raw, "1")
        assert "Book a slot" in message.body_text
        assert message.links == ("https://calendly.com/x",)

    def test_multipart_prefers_plain_text(self):
        raw = build_mime(plain="The genuine plain text body, which is long enough.", html="<p>HTML version</p>")
        assert "genuine plain text" in parse_message(raw, "1").body_text

    def test_multipart_still_harvests_links_from_the_html_part(self):
        """Plain-text alternatives routinely drop the URL that decides the category."""
        raw = build_mime(
            plain="Hi Alex, please pick a time for the interview using the link below.",
            html='<p>Pick a time</p><a href="https://calendly.com/acme">book</a>',
        )
        message = parse_message(raw, "1")
        assert "please pick a time" in message.body_text
        assert "https://calendly.com/acme" in message.links

    def test_trivial_plain_part_falls_back_to_html(self):
        raw = build_mime(plain="View online", html="<p>" + "The real content. " * 10 + "</p>")
        assert "The real content." in parse_message(raw, "1").body_text

    def test_attachment_names_are_listed_and_content_ignored(self):
        raw = build_mime(attachment=("offer-letter.pdf", b"%PDF-1.4 fake"))
        message = parse_message(raw, "1")
        assert message.attachments == ("offer-letter.pdf",)
        assert "%PDF" not in message.body_text

    def test_latin1_body_is_decoded(self):
        raw = build_mime(plain="Café au lait, sil vous plaît, encore une fois merci.", charset="iso-8859-1")
        assert "Café" in parse_message(raw, "1").body_text

    def test_unparseable_bytes_do_not_raise(self):
        message = parse_message(b"\xff\xfe not a message at all", uid="9")
        assert message.uid == "9"

    def test_flags_and_size_are_recorded(self):
        raw = build_mime()
        message = parse_message(raw, "1", flags=("\\Seen",), size=1234)
        assert message.flags == ("\\Seen",)
        assert message.size == 1234

    def test_list_unsubscribe_is_captured(self):
        raw = build_mime(extra_headers={"List-Unsubscribe": "<mailto:u@x.com>"})
        assert parse_message(raw, "1").list_unsubscribe == "<mailto:u@x.com>"


class TestExtractBody:
    def test_returns_text_and_attachments(self):
        import email

        message = email.message_from_bytes(build_mime(attachment=("cv.pdf", b"x")))
        extracted, attachments = extract_body(message)
        assert extracted.text
        assert attachments == ("cv.pdf",)


# ==========================================================================
# Engine: connection
# ==========================================================================
@pytest.fixture
def engine_factory(fake_imap_factory):
    def build(**kwargs):
        server = fake_imap_factory(**kwargs)
        engine = IMAPEngine(connection_factory=lambda host, port: server)
        return engine, server

    return build


class TestConnection:
    def test_login_and_capability_detection(self, engine_factory):
        engine, server = engine_factory()
        engine.connect("you@icloud.example", "app-specific")
        assert server.logged_in
        assert engine.has_capability("UIDPLUS")
        assert engine.has_capability("uidplus")
        assert not engine.has_capability("CONDSTORE")

    def test_capabilities_are_read_after_login_not_from_the_greeting(self, engine_factory):
        """iCloud advertises 8 capabilities before login and 21 after.

        Trusting the greeting made the app believe UIDPLUS was unavailable and
        fall back to a full EXPUNGE of the source mailbox, the destructive
        path this engine exists to avoid.
        """
        engine, server = engine_factory(
            capabilities=("IMAP4", "IMAP4REV1", "SASL-IR"),
            post_auth_capabilities=("IMAP4REV1", "UIDPLUS", "IDLE", "CONDSTORE", "NAMESPACE"),
        )
        engine.connect("you@icloud.example", "app-specific")
        assert engine.has_capability("UIDPLUS") is True
        assert engine.has_capability("IDLE") is True
        assert any(name == "CAPABILITY" for name, _ in server.commands)

    def test_the_greeting_capabilities_are_kept_as_well(self, engine_factory):
        engine, _ = engine_factory(
            capabilities=("XAPPLEPUSHSERVICE",),
            post_auth_capabilities=("IMAP4REV1", "UIDPLUS"),
        )
        engine.connect("you@icloud.example", "app-specific")
        assert engine.has_capability("XAPPLEPUSHSERVICE") is True
        assert engine.has_capability("UIDPLUS") is True

    def test_a_failed_capability_command_falls_back_to_the_greeting(self, engine_factory):
        import imaplib as _imaplib

        engine, server = engine_factory(capabilities=("IMAP4REV1", "UIDPLUS"))
        server.capability = lambda: (_ for _ in ()).throw(_imaplib.IMAP4.error(b"nope"))
        engine.connect("you@icloud.example", "app-specific")
        assert engine.has_capability("UIDPLUS") is True

    def test_uidplus_from_the_post_auth_list_drives_the_safe_expunge(self, engine_factory):
        engine, server = engine_factory(
            messages={"1": build_mime()},
            folders=["INBOX"] + list(FolderPlan().all_folders),
            capabilities=("IMAP4REV1",),                       # greeting hides it
            post_auth_capabilities=("IMAP4REV1", "UIDPLUS"),   # server really has it
        )
        engine.connect("you@icloud.example", "app-specific")
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert any(name == "UID EXPUNGE" for name, _ in server.commands)
        assert not any(name == "EXPUNGE" for name, _ in server.commands)
        assert report.warnings == []

    def test_delimiter_is_read_from_the_server_not_assumed(self, engine_factory):
        engine, _ = engine_factory(delimiter=".")
        engine.connect("you@icloud.example", "app-specific")
        assert engine.delimiter == "."
        assert engine.folder_plan("Job Search").for_category(
            __import__("models").Category.INTERVIEW
        ) == "Job Search.Interview"

    def test_bad_password_raises_a_helpful_auth_error(self, engine_factory):
        engine, _ = engine_factory()
        with pytest.raises(IMAPAuthError) as excinfo:
            engine.connect("you@icloud.example", "wrong")
        assert "app-specific password" in str(excinfo.value)

    def test_missing_email_is_rejected_before_connecting(self, engine_factory):
        engine, server = engine_factory()
        with pytest.raises(IMAPAuthError):
            engine.connect("", "app-specific")
        assert not server.logged_in

    def test_missing_password_is_rejected_before_connecting(self, engine_factory):
        engine, server = engine_factory()
        with pytest.raises(IMAPAuthError):
            engine.connect("you@icloud.example", "")
        assert not server.logged_in

    def test_network_failure_becomes_a_connection_error(self):
        def explode(host, port):
            raise OSError("Name or service not known")

        engine = IMAPEngine(connection_factory=explode)
        with pytest.raises(IMAPConnectionError):
            engine.connect("you@icloud.example", "pw")

    def test_operations_without_a_connection_fail_clearly(self):
        with pytest.raises(IMAPError, match="Not connected"):
            IMAPEngine().list_folders()

    def test_context_manager_logs_out(self, engine_factory):
        engine, server = engine_factory()
        with engine.session("you@icloud.example", "app-specific"):
            pass
        assert server.logged_out


# ==========================================================================
# Engine: folders
# ==========================================================================
class TestFolders:
    def test_creates_the_whole_tree_when_absent(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        plan = engine.folder_plan("Job Search")
        created = engine.ensure_folders(plan)
        assert created == list(plan.all_folders)
        assert "Job Search/Received" in server.folders

    def test_parent_is_created_before_children(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        engine.ensure_folders(engine.folder_plan("Job Search"))
        creates = [args[0] for name, args in server.commands if name == "CREATE"]
        assert creates[0] == "Job Search"

    def test_existing_folders_are_left_alone(self, engine_factory):
        existing = list(FolderPlan().all_folders)
        engine, server = engine_factory(folders=["INBOX"] + existing)
        engine.connect("you@icloud.example", "app-specific")
        assert engine.ensure_folders(engine.folder_plan("Job Search")) == []
        assert not any(name == "CREATE" for name, _ in server.commands)

    def test_existing_check_is_case_insensitive(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX", "job search", "job search/interview"])
        engine.connect("you@icloud.example", "app-specific")
        created = engine.ensure_folders(engine.folder_plan("Job Search"))
        assert "Job Search" not in created
        assert "Job Search/Interview" not in created

    def test_new_folders_are_subscribed(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        engine.ensure_folders(engine.folder_plan("Job Search"), subscribe=True)
        assert "Job Search/Interview" in server.subscribed

    def test_subscription_can_be_disabled(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        engine.ensure_folders(engine.folder_plan("Job Search"), subscribe=False)
        assert server.subscribed == []

    def test_already_exists_race_is_not_an_error(self, engine_factory):
        """Another client may create the folder between our LIST and our CREATE."""
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        server.folders.append("Job Search")
        server.list = lambda directory='""', pattern="*": (
            "OK", [b'(\\HasNoChildren) "/" "INBOX"']
        )
        created = engine.ensure_folder_paths(["Job Search"])
        assert created == ["Job Search"]  # reported as created, no exception raised

    def test_ensure_folder_paths_deduplicates(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX"])
        engine.connect("you@icloud.example", "app-specific")
        created = engine.ensure_folder_paths(["Sorted Mail", "Sorted Mail", "Sorted Mail/Finance"])
        assert created == ["Sorted Mail", "Sorted Mail/Finance"]

    def test_non_ascii_folders_round_trip(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX", "Résumé"])
        engine.connect("you@icloud.example", "app-specific")
        assert "Résumé" in engine.folder_names()


# ==========================================================================
# Engine: search and fetch
# ==========================================================================
def _messages(count: int = 3):
    return {
        str(i): build_mime(subject=f"Message {i}", plain=f"Body of message {i}. " * 5)
        for i in range(1, count + 1)
    }


class TestSearchAndFetch:
    def test_search_widens_the_server_side_window_by_a_day(self, engine_factory):
        engine, server = engine_factory(messages=_messages())
        engine.connect("you@icloud.example", "app-specific")
        engine.select("INBOX")
        engine.search_window(datetime(2026, 9, 4, 15, 0, tzinfo=UTC), datetime(2026, 9, 5, tzinfo=UTC))
        args = [a for name, a in server.commands if name == "UID SEARCH"][0]
        assert "03-Sep-2026" in args  # start - 1 day
        assert "07-Sep-2026" in args  # end + 2 days

    def test_fetch_uses_body_peek_so_nothing_is_marked_read(self, engine_factory):
        engine, server = engine_factory(messages=_messages())
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 9, 1, tzinfo=UTC))
        specs = [a[1] for name, a in server.commands if name == "UID FETCH"]
        assert all("BODY.PEEK[]" in spec for spec in specs)
        assert not any("BODY[]" in spec.replace("BODY.PEEK[]", "") for spec in specs)

    def test_scan_selects_the_mailbox_read_only(self, engine_factory):
        engine, server = engine_factory(messages=_messages())
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 9, 1, tzinfo=UTC))
        assert server.readonly is True

    def test_messages_outside_the_exact_window_are_filtered_client_side(self, engine_factory):
        """IMAP SINCE is day-granular; 'Past 24 Hours' has to be exact."""
        messages = _messages(3)
        engine, server = engine_factory(
            messages=messages,
            internaldates={
                "1": "04-Sep-2026 23:00:00 +0000",  # inside
                "2": "04-Sep-2026 01:00:00 +0000",  # before the start
                "3": "04-Sep-2026 20:00:00 +0000",  # inside
            },
        )
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(
            start=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
            end=datetime(2026, 9, 5, 0, 0, tzinfo=UTC),
        )
        assert {m.uid for m in result.messages} == {"1", "3"}

    def test_results_are_newest_first(self, engine_factory):
        engine, server = engine_factory(
            messages=_messages(3),
            internaldates={
                "1": "01-Sep-2026 10:00:00 +0000",
                "2": "03-Sep-2026 10:00:00 +0000",
                "3": "02-Sep-2026 10:00:00 +0000",
            },
        )
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC))
        assert [m.uid for m in result.messages] == ["2", "3", "1"]

    def test_max_messages_keeps_the_newest_and_warns(self, engine_factory):
        engine, server = engine_factory(messages=_messages(10))
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), max_messages=4)
        assert result.truncated_to_max is True
        assert len(result.candidate_uids) == 4
        assert set(result.candidate_uids) == {"10", "9", "8", "7"}
        assert result.warnings and "only the 4 most recent" in result.warnings[0]

    def test_empty_mailbox_is_not_an_error(self, engine_factory):
        engine, server = engine_factory(messages={})
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC))
        assert result.messages == []

    def test_progress_is_reported(self, engine_factory):
        engine, server = engine_factory(messages=_messages(5))
        engine.connect("you@icloud.example", "app-specific")
        seen = []
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), progress=lambda d, t, m: seen.append((d, t)))
        assert seen and seen[-1][0] == seen[-1][1]

    def test_cancellation_stops_the_scan(self, engine_factory):
        engine, server = engine_factory(messages=_messages(5))
        engine.connect("you@icloud.example", "app-specific")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ScanCancelled):
            engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), cancel=cancel)


# ==========================================================================
# Engine: moves  (the part that must never lose mail)
# ==========================================================================
class TestMoves:
    def setup_engine(self, engine_factory, folders=None):
        folders = folders or ["INBOX"] + list(FolderPlan().all_folders)
        engine, server = engine_factory(messages=_messages(3), folders=folders)
        engine.connect("you@icloud.example", "app-specific")
        return engine, server

    def test_copy_then_flag_then_expunge_in_that_order(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        order = [name for name, _ in server.commands if name.startswith("UID ")]
        assert order.index("UID COPY") < order.index("UID STORE") < order.index("UID EXPUNGE")
        assert report.moved == {"1": "Job Search/Interview"}
        assert server.copies == [("1", "Job Search/Interview")]
        assert "1" in server.expunged

    def test_the_mailbox_is_opened_writable_for_moves(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert server.readonly is False

    def test_a_failed_copy_never_deletes_the_original(self, engine_factory):
        """The single most important property in this file."""
        engine, server = self.setup_engine(engine_factory)
        server.fail_copy_to = "Job Search/Interview"
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert report.moved == {}
        assert "1" in report.failed
        assert server.deleted == set()
        assert server.expunged == []
        assert "1" in server.messages

    def test_a_failed_store_is_reported_without_losing_the_copy(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        server.fail_store = True
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert report.moved == {}
        assert "could not be removed" in report.failed["1"]
        assert server.expunged == []

    def test_one_failing_folder_does_not_block_the_others(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        server.fail_copy_to = "Job Search/Interview"
        report = engine.move_messages([
            MovePlan("1", "Job Search/Interview"),
            MovePlan("2", "Job Search/Next Steps"),
        ])
        assert report.moved == {"2": "Job Search/Next Steps"}
        assert "1" in report.failed

    def test_moves_are_grouped_into_one_command_per_folder(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        engine.move_messages([
            MovePlan("1", "Job Search/Interview"),
            MovePlan("2", "Job Search/Interview"),
            MovePlan("3", "Job Search/Next Steps"),
        ])
        copies = [a for name, a in server.commands if name == "UID COPY"]
        assert len(copies) == 2
        assert copies[0][0] == "1,2"

    def test_uid_expunge_is_preferred_when_uidplus_is_available(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert any(name == "UID EXPUNGE" for name, _ in server.commands)
        assert not any(name == "EXPUNGE" for name, _ in server.commands)
        assert report.warnings == []

    def test_without_uidplus_a_full_expunge_is_used_and_declared(self, engine_factory):
        engine, server = engine_factory(
            messages=_messages(3),
            folders=["INBOX"] + list(FolderPlan().all_folders),
            capabilities=("IMAP4REV1",),
        )
        engine.connect("you@icloud.example", "app-specific")
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert any(name == "EXPUNGE" for name, _ in server.commands)
        assert any("UIDPLUS" in warning for warning in report.warnings)

    def test_uid_expunge_only_removes_our_messages(self, engine_factory):
        """A message the user flagged \\Deleted by hand must survive."""
        engine, server = self.setup_engine(engine_factory)
        server.deleted.add("3")  # user deleted this one earlier, elsewhere
        engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert "3" in server.messages
        assert server.expunged == ["1"]

    def test_target_folders_are_quoted(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        engine.move_messages([MovePlan("1", "Job Search/Next Steps")])
        copy_args = [a for name, a in server.commands if name == "UID COPY"][0]
        assert copy_args[1] == '"Job Search/Next Steps"'

    def test_empty_plan_is_a_no_op(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        before = list(server.commands)
        report = engine.move_messages([])
        assert report.moved_count == 0
        assert server.commands == before   # not even a SELECT was issued

    def test_plans_without_a_folder_are_skipped(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        report = engine.move_messages([MovePlan("1", "")])
        assert report.moved_count == 0

    def test_large_batches_are_chunked(self, engine_factory):
        messages = {str(i): build_mime() for i in range(1, 251)}
        engine, server = engine_factory(
            messages=messages, folders=["INBOX"] + list(FolderPlan().all_folders)
        )
        engine.connect("you@icloud.example", "app-specific")
        plans = [MovePlan(str(i), "Job Search/Interview") for i in range(1, 251)]
        report = engine.move_messages(plans)
        copies = [a for name, a in server.commands if name == "UID COPY"]
        assert len(copies) == 3  # COMMAND_BATCH = 100
        assert report.moved_count == 250

    def test_progress_is_reported_during_apply(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        seen = []
        engine.move_messages(
            [MovePlan("1", "Job Search/Interview"), MovePlan("2", "Job Search/Next Steps")],
            progress=lambda d, t, m: seen.append((d, t, m)),
        )
        assert seen and seen[-1][0] == seen[-1][1]

    def test_cancellation_before_any_copy(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ScanCancelled):
            engine.move_messages([MovePlan("1", "Job Search/Interview")], cancel=cancel)
        assert server.copies == []

    def test_report_description(self, engine_factory):
        engine, server = self.setup_engine(engine_factory)
        report = engine.move_messages([MovePlan("1", "Job Search/Interview")])
        assert "1 moved" in report.describe()


class TestProbe:
    def test_probe_reports_server_facts_and_disconnects(self, engine_factory):
        engine, server = engine_factory(folders=["INBOX", "Job Search", "Job Search/Interview"])
        result = engine.probe("you@icloud.example", "app-specific")
        assert result["uidplus"] is True
        assert result["delimiter"] == "/"
        assert "Job Search/Interview" in result["folders"]
        assert server.logged_out


# ==========================================================================
# Fetch performance: partial download and parallel connections
# ==========================================================================
class TestPartialFetch:
    def test_only_the_first_n_bytes_are_requested(self, engine_factory):
        engine, server = engine_factory(messages=_messages(3))
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), max_bytes=32768)
        spec = [a[1] for name, a in server.commands if name == "UID FETCH"][0]
        assert "BODY.PEEK[]<0.32768>" in spec
        assert "BODY.PEEK[]<" in spec and "PEEK[])" not in spec

    def test_zero_means_download_everything(self, engine_factory):
        engine, server = engine_factory(messages=_messages(3))
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), max_bytes=0)
        spec = [a[1] for name, a in server.commands if name == "UID FETCH"][0]
        assert "BODY.PEEK[])" in spec

    def test_it_still_uses_peek_so_nothing_is_marked_read(self, engine_factory):
        engine, server = engine_factory(messages=_messages(3))
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), max_bytes=16384)
        assert all("BODY.PEEK" in a[1] for name, a in server.commands if name == "UID FETCH")

    def test_a_partially_downloaded_message_is_marked_truncated(self, engine_factory):
        """The classifier must not believe it saw a whole message."""
        big = build_mime(plain="x" * 50_000)
        engine, server = engine_factory(messages={"1": big})
        engine.connect("you@icloud.example", "app-specific")
        # The fake server returns the whole body, so shrink what it reports as
        # downloaded by asking for a size the real server would honour.
        message = engine._fetch_batch(["1"], "INBOX", max_bytes=0)[0]
        assert message.truncated is False
        assert message.size == len(big)

    def test_the_true_size_is_kept_even_when_partially_fetched(self, engine_factory):
        engine, server = engine_factory(messages=_messages(1))
        engine.connect("you@icloud.example", "app-specific")
        message = engine._fetch_batch(["1"], "INBOX", max_bytes=1024)[0]
        assert message.size == len(server.messages["1"])


class TestParallelFetch:
    def test_a_small_window_stays_on_one_connection(self, engine_factory):
        engine, server = engine_factory(messages=_messages(5))
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=4)
        assert len(result.messages) == 5
        assert sum(1 for name, _ in server.commands if name == "LOGIN") == 1

    def test_a_large_window_opens_extra_connections(self, fake_imap_factory):
        opened = []

        def factory(host, port):
            server = fake_imap_factory(messages=_messages(120))
            opened.append(server)
            return server

        engine = IMAPEngine(connection_factory=factory)
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=4)
        assert len(result.messages) == 120
        assert len(opened) == 4, "four connections should have been used"

    def test_every_message_arrives_exactly_once(self, fake_imap_factory):
        engine = IMAPEngine(
            connection_factory=lambda h, p: fake_imap_factory(messages=_messages(97))
        )
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=4)
        uids = [m.uid for m in result.messages]
        assert len(uids) == len(set(uids)) == 97

    def test_one_failed_connection_does_not_lose_the_scan(self, fake_imap_factory):
        servers = []

        def factory(host, port):
            server = fake_imap_factory(messages=_messages(80))
            servers.append(server)
            if len(servers) == 3:      # the second sibling refuses to fetch
                server.uid = lambda *a, **k: (_ for _ in ()).throw(
                    imaplib.IMAP4.error(b"server busy")
                )
            return server

        engine = IMAPEngine(connection_factory=factory)
        engine.connect("you@icloud.example", "app-specific")
        result = engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=4)
        assert result.messages, "the surviving shards should still return messages"
        assert any("fetch connections failed" in w for w in result.warnings)

    def test_extra_connections_are_logged_out(self, fake_imap_factory):
        servers = []

        def factory(host, port):
            server = fake_imap_factory(messages=_messages(60))
            servers.append(server)
            return server

        engine = IMAPEngine(connection_factory=factory)
        engine.connect("you@icloud.example", "app-specific")
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=3)
        assert all(s.logged_out for s in servers[1:]), "sibling connections leaked"

    def test_cancelling_stops_the_parallel_fetch(self, fake_imap_factory):
        engine = IMAPEngine(
            connection_factory=lambda h, p: fake_imap_factory(messages=_messages(120))
        )
        engine.connect("you@icloud.example", "app-specific")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(ScanCancelled):
            engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=4, cancel=cancel)

    def test_progress_reaches_the_total(self, fake_imap_factory):
        engine = IMAPEngine(
            connection_factory=lambda h, p: fake_imap_factory(messages=_messages(90))
        )
        engine.connect("you@icloud.example", "app-specific")
        seen = []
        engine.fetch_window(datetime(2026, 8, 1, tzinfo=UTC), connections=3,
                            progress=lambda d, t, m: seen.append((d, t)))
        assert seen and max(d for d, _ in seen) == 90


class TestTheCopyReceipt:
    """A COPY assigns new UIDs, and UIDPLUS servers say which."""

    def test_it_reads_a_copyuid_line(self):
        from imap_engine import copied_uids
        assert copied_uids([b"[COPYUID 1234567 5:7 100:102] (Success)"]) == {
            "5": "100", "6": "101", "7": "102"}

    def test_it_handles_a_mixed_set(self):
        from imap_engine import copied_uids
        assert copied_uids([b"[COPYUID 1 5:7,9 100:102,205] Done"]) == {
            "5": "100", "6": "101", "7": "102", "9": "205"}

    def test_a_reversed_range_still_pairs_up(self):
        from imap_engine import copied_uids
        assert copied_uids([b"[COPYUID 1 7:5 100:102] ok"]) == {
            "5": "100", "6": "101", "7": "102"}

    def test_a_server_with_no_uidplus_says_nothing(self):
        from imap_engine import copied_uids
        assert copied_uids([b"(Success)"]) == {}
        assert copied_uids([]) == {}
        assert copied_uids(None) == {}

    def test_mismatched_sets_are_refused_rather_than_guessed(self):
        from imap_engine import copied_uids
        assert copied_uids([b"[COPYUID 1 5:7 100] nope"]) == {}

    def test_junk_does_not_raise(self):
        from imap_engine import copied_uids
        for line in (b"[COPYUID]", b"[COPYUID a b c]", b"[COPYUID 1 x:y 1:2]",
                     "a string not bytes", b"\xff\xfe binary"):
            assert isinstance(copied_uids([line]), dict)

    def test_the_last_line_wins(self):
        """imaplib puts the tagged response last, which is the one with it."""
        from imap_engine import copied_uids
        assert copied_uids([b"* 1 EXISTS",
                            b"[COPYUID 1 5 100] (Success)"]) == {"5": "100"}

    def test_expanding_a_set(self):
        from imap_engine import _expand_uid_set
        assert _expand_uid_set("1:3,7,10:11") == ["1", "2", "3", "7", "10", "11"]
        assert _expand_uid_set("") == []
        assert _expand_uid_set("not,a,set") == ["not", "a", "set"]
