"""The mail client in the main window: a message opened in its own window,
written back to, marked and moved, and the workers and server calls under
all of it."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import gui as gui_module
import outgoing
import workers as workers_module
from config import InMemoryCredentialStore, Settings
from gui import MainWindow
from imap_engine import IMAPEngine, IMAPError, MailboxInfo, MovePlan, MoveReport
from models import (Category, Classification, EmailMessage, FolderPlan,
                    OtherCategory, TriageItem)


def item(uid="1", hour=12, flags=("\\Seen",), **fields):
    base = dict(uid=uid, subject=f"Subject {uid}", sender_name="Dana Reyes",
                sender_email="dana@northwind.example", to="you@icloud.example",
                message_id=f"<{uid}@northwind.example>",
                date=datetime(2026, 10, 6, hour, 0, tzinfo=timezone.utc),
                body_text=f"Body {uid}", body_html=f"<p>Body <b>{uid}</b></p>",
                flags=flags)
    base.update(fields)
    return TriageItem(
        email=EmailMessage(**base),
        classification=Classification(
            summary=f"Summary {uid}", is_job_related=True, category=Category.INTERVIEW,
            other_category=OtherCategory.NOT_APPLICABLE, confidence_score=0.95,
            reasoning="Because.", model="test"),
        folders=FolderPlan())


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore())
    win.folder_plan = FolderPlan()
    win._has_scanned = True
    win.model.set_items([item("1", hour=9), item("2", hour=11, flags=()),
                         item("3", hour=10, attachments=("cv.pdf",))])
    yield win
    win._close_mail_windows()
    win.close()
    win.deleteLater()


@pytest.fixture
def demo_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore(), demo=True)
    win.folder_plan = FolderPlan()
    win.model.set_items([item("1"), item("2")])
    yield win
    win._close_mail_windows()
    win.close()
    win.deleteLater()


def message_windows(win):
    from mail_window import MessageWindow

    return [w for w in win._live_mail_windows() if isinstance(w, MessageWindow)]


def compose_windows(win):
    from mail_window import ComposeWindow

    return [w for w in win._live_mail_windows() if isinstance(w, ComposeWindow)]


class _Recorder:
    """A worker that remembers how it was built and never starts."""

    made: list = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        type(self).made.append(self)

    def start(self):
        pass


def recorder_for(monkeypatch, name):
    real = getattr(workers_module, name)

    class Recorder(real):
        made = []

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.args = args
            self.kwargs = kwargs
            type(self).made.append(self)

        def start(self):        # never touch the network
            pass

    monkeypatch.setattr(gui_module, name, Recorder)
    return Recorder


class TestOpeningAMessage:
    def test_a_double_click_opens_it_in_a_window_and_marks_it_read(self, window):
        row = window.proxy.mapFromSource(window.model.index(1, 0))
        assert "\\Seen" not in window.model.items[1].email.flags
        window._open_index(window.proxy.index(row.row(), 2))
        opened = message_windows(window)
        assert len(opened) == 1 and opened[0].windowTitle() == "Subject 2"
        assert "\\Seen" in window.model.items[1].email.flags
        assert opened[0].read_action.text() == "Mark as Unread"

    def test_the_tick_column_does_not_open_it(self, window):
        window._open_index(window.proxy.index(0, 0))
        assert message_windows(window) == []

    def test_opening_it_again_brings_the_same_window(self, window):
        window.open_message(0)
        window.open_message(0)
        assert len(message_windows(window)) == 1
        window.open_message(2)
        assert len(message_windows(window)) == 2

    def test_the_menu_opens_the_selected_one(self, window):
        window._select_rows([2])
        window.open_message_action.trigger()
        assert [w.windowTitle() for w in message_windows(window)] == ["Subject 3"]

    def test_neighbours_follow_the_table_as_shown(self, window):
        # Sorted by date, newest first: uid 2 (11:00), 3 (10:00), 1 (09:00).
        shown = [window.proxy.mapToSource(window.proxy.index(r, 0)).row()
                 for r in range(window.proxy.rowCount())]
        assert [window.model.items[r].email.uid for r in shown] == ["2", "3", "1"]
        assert window.neighbour_row(shown[0], 1) == shown[1]
        assert window.neighbour_row(shown[1], -1) == shown[0]
        assert window.neighbour_row(shown[0], -1) is None
        assert window.neighbour_row(shown[2], 1) is None
        window.proxy.set_text_filter("subject 2")
        assert window.neighbour_row(shown[1], 1) is None, "a hidden row has no place"
        assert window.neighbour_row(shown[0], 1) is None

    def test_next_in_the_window_selects_the_row_in_the_table(self, window):
        first = window.proxy.mapToSource(window.proxy.index(0, 0)).row()
        window.open_message(first)
        opened = message_windows(window)[0]
        opened.next_action.trigger()
        assert opened.windowTitle() == "Subject 3"
        assert window._selected_rows() == [opened.row]

    def test_a_scan_that_drops_the_message_closes_its_window(self, window):
        window.open_message(0)
        window.open_message(1)
        kept = window.model.items[1]
        window.model.set_items([item("9"), kept])
        titles = [w.windowTitle() for w in message_windows(window)]
        assert titles == ["Subject 2"], "the other message is gone, so is its window"
        assert message_windows(window)[0].row == 1

    def test_quitting_asks_about_a_half_written_message(self, window, monkeypatch):
        window.compose("new")
        writing = compose_windows(window)[0]
        writing.to_edit.setText("dana@northwind.example")
        assert writing.is_dirty()
        monkeypatch.setattr(writing, "_ask_to_keep", lambda: "cancel")
        assert window.confirm_quit() is False
        assert writing.isVisible()
        monkeypatch.setattr(writing, "_ask_to_keep", lambda: "discard")
        for row in window.model.items:
            row.approved = False            # nothing else to ask about
        assert window.confirm_quit() is True
        assert compose_windows(window) == []


class TestTheMessageMenu:
    def test_it_has_the_usual_keys(self, window):
        actions = {a.text(): a for a in window.message_menu.actions() if a.text()}
        assert actions["&New Message"].shortcut().toString() == "Ctrl+N"
        assert actions["&Open Message"].shortcut().toString() == "Ctrl+O"
        assert window.quick_actions["archive"].shortcut().toString() == "Ctrl+E"
        assert window.quick_actions["delete"].shortcut().toString() == "Ctrl+Backspace"
        assert window.quick_actions["junk"].shortcut().toString() == "Ctrl+Shift+J"
        assert window.mark_read_action.shortcut().toString() == "Ctrl+Shift+U"
        assert window.mark_flag_action.shortcut().toString() == "Ctrl+Shift+L"

    def test_it_follows_the_selection(self, window):
        window.table.clearSelection()
        window._sync_message_menu()
        assert not window.open_message_action.isEnabled()
        assert not window.compose_actions["reply"].isEnabled()
        assert not window.quick_actions["archive"].isEnabled()
        window._select_rows([1])            # unread
        window._sync_message_menu()
        assert window.open_message_action.isEnabled()
        assert window.mark_read_action.text() == "Mark as Read"
        assert window.mark_flag_action.text() == "Flag"
        window._select_rows([0])            # read
        window._sync_message_menu()
        assert window.mark_read_action.text() == "Mark as Unread"
        window.model.items[0].email.flags = ("\\Seen", "\\Flagged")
        window._sync_message_menu()
        assert window.mark_flag_action.text() == "Unflag"
        window._select_rows([0, 1])
        window._sync_message_menu()
        assert not window.compose_actions["reply"].isEnabled(), "one reply, one message"
        assert window.quick_actions["archive"].isEnabled()

    def test_the_table_menu_offers_the_quick_actions(self, window):
        window._select_rows([1])
        labels = [a.text() for a in window.build_table_menu().actions()]
        for wanted in ("Open in a Window", "Reply", "Reply All", "Forward",
                       "Mark as Read", "Flag", "Archive now", "Delete now",
                       "Move to Junk now"):
            assert wanted in labels, wanted
        window._select_rows([0, 1])
        labels = [a.text() for a in window.build_table_menu().actions()]
        assert "Open in a Window" not in labels and "Reply" not in labels
        assert "Archive now" in labels

    def test_the_menu_acts_on_the_selection(self, window):
        window._select_rows([1])
        window.mark_read_action.trigger()
        assert "\\Seen" in window.model.items[1].email.flags
        window._select_rows([2])
        window.compose_actions["forward"].trigger()
        assert compose_windows(window)[0].subject_edit.text() == "Fwd: Subject 3"

    def test_the_cheat_sheet_lists_them(self, window, dialog_calls):
        window._show_shortcuts()
        text = dialog_calls[-1][2]
        for key in ("⌘N", "⌘O", "⌘↩", "⌘⇧R"):
            assert key in text


class TestWritingBack:
    def test_a_reply_is_addressed_with_the_quote_and_the_thread(self, window):
        window.compose("reply", 0)
        writing = compose_windows(window)[0]
        assert writing.to_edit.text() == "Dana Reyes <dana@northwind.example>"
        assert writing.subject_edit.text() == "Re: Subject 1"
        draft = writing.draft()
        assert draft.in_reply_to == "<1@northwind.example>"
        assert draft.from_address == "you@icloud.example"
        assert "wrote:" in writing.editor.text() and "Body 1" in writing.editor.text()
        assert writing.answering == ("1", "INBOX", window.settings.primary_account.id)

    def test_reply_all_copies_everyone_but_us(self, window):
        window.model.items[0].email.to = "you@icloud.example, Sam <sam@acme.example>"
        window.model.items[0].email.cc = "pat@acme.example"
        window.compose("reply_all", 0)
        writing = compose_windows(window)[0]
        assert writing.cc_edit.text() == "Sam <sam@acme.example>, pat@acme.example"
        assert not writing.cc_edit.isHidden()

    def test_a_forward_has_nobody_yet_and_carries_the_text(self, window):
        window.compose("forward", 1)
        writing = compose_windows(window)[0]
        assert writing.to_edit.text() == ""
        assert writing.subject_edit.text() == "Fwd: Subject 2"
        assert "Forwarded message" in writing.editor.text()

    def test_a_new_message_is_empty(self, window):
        window.compose("new")
        writing = compose_windows(window)[0]
        assert writing.to_edit.text() == "" and writing.subject_edit.text() == ""
        assert writing.draft().from_address == "you@icloud.example"
        assert writing.windowTitle() == "New Message"

    def test_without_a_mailbox_it_says_so(self, qapp, tmp_path, monkeypatch, dialog_calls):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        bare = MainWindow(Settings(icloud_email=""), InMemoryCredentialStore())
        try:
            bare.compose("new")
            assert compose_windows(bare) == []
            assert dialog_calls[-1][1] == "No mailbox"
        finally:
            bare.close()
            bare.deleteLater()

    def test_sending_needs_a_password(self, window, dialog_calls):
        said = []
        account = window.settings.primary_account
        window.send_mail(account, outgoing.Draft(from_address=account.address),
                         lambda: said.append("ok"), said.append)
        assert said and "no password saved" in said[0]
        assert dialog_calls[-1][1] == "Not connected"

    def test_the_demo_sends_and_saves_nothing(self, demo_window):
        said = []
        account = demo_window.settings.primary_account
        draft = outgoing.Draft(from_address=account.address)
        demo_window.send_mail(account, draft, lambda: said.append("ok"), said.append)
        demo_window.save_draft(account, draft, lambda: said.append("ok"), said.append)
        assert said == ["This is the demo. Nothing is sent from it.",
                        "This is the demo. Nothing is saved from it."]

    def test_a_send_goes_to_a_worker_and_back(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "SendWorker")
        account = window.settings.primary_account
        draft = outgoing.Draft(from_address=account.address, to=["a@b.example"],
                               subject="Hello")
        done = []
        window.send_mail(account, draft, lambda: done.append(True), done.append,
                         answering=("2", "INBOX", account.id))
        worker = Recorder.made[-1]
        assert worker.account is account and worker.draft is draft
        assert worker.password == "app-specific"
        assert worker.answering == ("2", "INBOX")
        worker.sent.emit("")
        assert done == [True]
        assert "\\Answered" in window.model.items[1].email.flags
        assert window._status_text.startswith("Sent")

    def test_an_answer_from_another_mailbox_marks_nothing(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "SendWorker")
        account = window.settings.primary_account
        window.send_mail(account, outgoing.Draft(from_address=account.address),
                         lambda: None, lambda d: None, answering=("2", "INBOX", "other"))
        assert Recorder.made[-1].answering is None

    def test_a_draft_goes_to_a_worker(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "DraftWorker")
        account = window.settings.primary_account
        said = []
        window.save_draft(account, outgoing.Draft(from_address=account.address),
                          lambda: said.append("saved"), said.append)
        worker = Recorder.made[-1]
        worker.saved.emit("Drafts")
        assert said == ["saved"]
        worker.failed.emit("Could not save the draft", "no Drafts mailbox")
        assert said[-1] == "no Drafts mailbox"

    def test_a_forward_carries_the_attachments(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "AttachmentFetchWorker")
        window.compose("forward", 2)
        writing = compose_windows(window)[0]
        assert writing.statusBar().currentMessage() == "Fetching 1 attachment…"
        worker = Recorder.made[-1]
        assert worker.message is window.model.items[2].email
        worker.ready.emit([outgoing.Attachment("cv.pdf", "application/pdf", b"%PDF")],
                          ["huge.zip"])
        assert [a.name for a in writing.draft().attachments] == ["cv.pdf"]
        assert writing.statusBar().currentMessage() == "Too big to carry: huge.zip"
        worker.failed.emit("Could not fetch the attachments", "timed out")
        assert "timed out" in writing.statusBar().currentMessage()

    def test_the_demo_forwards_without_attachments(self, demo_window):
        demo_window.model.items[0].email.attachments = ("cv.pdf",)
        demo_window.compose("forward", 0)
        writing = compose_windows(demo_window)[0]
        assert writing.draft().attachments == []
        assert "demo" in writing.statusBar().currentMessage()

    def test_known_addresses_are_the_mailboxes_and_the_senders(self, window):
        window.model.items[1].email.sender_name = "Reyes, Dana"
        found = window.known_addresses()
        assert "you@icloud.example" in found
        assert any(a.startswith('"Reyes, Dana" <') or a.startswith("Dana Reyes <")
                   for a in found)
        assert len([a for a in found if "northwind" in a]) == 1

    def test_the_name_and_signature_come_from_settings(self, window):
        assert window.sender_name() == "" and window.signature_html() == ""
        window.settings.reply_signature = "  Sam & Co "
        assert window.sender_name() == "Sam & Co"
        assert window.signature_html() == "<p>Sam &amp; Co</p>"


class TestQuickActions:
    def test_marking_changes_the_table_at_once(self, window):
        window.act_on_rows("read", [1])
        assert "\\Seen" in window.model.items[1].email.flags
        window.act_on_rows("unread", [1])
        assert "\\Seen" not in window.model.items[1].email.flags
        window.act_on_rows("flag", [0, 1])
        assert all("\\Flagged" in window.model.items[r].email.flags for r in (0, 1))
        window.act_on_rows("unflag", [0])
        assert "\\Flagged" not in window.model.items[0].email.flags
        assert "no password saved" in window._status_text

    def test_marking_with_a_password_tells_the_server(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "FlagWorker")
        window.act_on_rows("flag", [0, 2])
        worker = Recorder.made[-1]
        assert worker.uids == ["1", "3"] and worker.flag == "flagged" and worker.add
        assert worker.mailbox == "INBOX"
        window.act_on_rows("read", [1])
        assert Recorder.made[-1].flag == "seen"

    def test_the_demo_archives_in_the_table_alone(self, demo_window):
        demo_window.act_on_rows("archive", [0])
        assert demo_window.model.items[0].moved
        assert "Demo" in demo_window._status_text
        assert demo_window._undo_stack == []

    def test_a_real_archive_goes_to_a_worker_and_is_undoable(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "MoveWorker")
        window.act_on_rows("archive", [0, 1])
        worker = Recorder.made[-1]
        assert worker.kind == "archive"
        assert [p.uid for p in worker.plans] == ["1", "2"]
        assert "Moving 2 messages to Archive" in window._status_text
        window._on_quick_move(MoveReport(moved={"1": "Archive", "2": "Archive"},
                                         new_uids={"1": "9001", "2": "9002"}))
        assert window.model.items[0].moved and window.model.items[1].moved
        assert [p.uid for p in window._undo_stack[-1].plans] == ["9001", "9002"]
        assert window.undo_action.isEnabled()
        assert window._status_text.startswith("Moved 2")

    def test_a_delete_and_a_junk_name_their_kind(self, window, monkeypatch):
        window.store.set_mailbox_password("you@icloud.example", "app-specific")
        Recorder = recorder_for(monkeypatch, "MoveWorker")
        window.act_on_rows("delete", [0])
        window.act_on_rows("junk", [1])
        window.act_on_rows("move", [2], "Job Search/Interview")
        assert [w.kind for w in Recorder.made] == ["trash", "junk", "folder"]
        assert Recorder.made[-1].plans[0].target_folder == "Job Search/Interview"

    def test_a_failed_move_is_said(self, window, dialog_calls):
        window._on_quick_move(MoveReport(failed={"1": "no such folder"}))
        assert dialog_calls[-1][1] == "Could not move the message"
        assert not window.model.items[0].moved

    def test_moved_rows_and_a_dry_run_are_left_alone(self, qapp, tmp_path, monkeypatch,
                                                    dialog_calls, window):
        window.model.items[0].moved = True
        window.act_on_rows("flag", [0])
        assert "\\Flagged" not in window.model.items[0].email.flags
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        dry = MainWindow(Settings(icloud_email="you@icloud.example"),
                         InMemoryCredentialStore(), dry_run=True)
        try:
            dry.model.set_items([item("1")])
            dry.act_on_rows("archive", [0])
            assert dialog_calls[-1][1] == "Dry run"
            assert not dry.model.items[0].moved
        finally:
            dry.close()
            dry.deleteLater()

    def test_a_wrong_word_is_refused(self, window):
        with pytest.raises(ValueError):
            window.act_on_rows("shred", [0])
        window.act_on_rows("move", [0])         # nowhere to go: nothing happens
        assert not window.model.items[0].moved


# -- The workers, against the fake server -----------------------------------

@pytest.fixture
def server_engine(fake_imap_factory, monkeypatch):
    """A fake server, and the engine every worker builds pointed at it."""
    def build(**kwargs):
        server = fake_imap_factory(**kwargs)
        monkeypatch.setattr(
            workers_module, "IMAPEngine",
            lambda host=None, port=None: IMAPEngine(connection_factory=lambda h, p: server))
        return server

    return build


@pytest.fixture
def account():
    from accounts import Account

    return Account(address="you@icloud.example", preset="icloud")


def caught(signal):
    seen = []
    signal.connect(lambda *args: seen.append(args))
    return seen


class TestTheWorkers:
    def test_flags_are_set_on_the_server(self, qapp, server_engine, account):
        server = server_engine(messages={"7": b"raw"})
        worker = workers_module.FlagWorker(account, "app-specific", ["7"], "seen")
        done, failed = caught(worker.done), caught(worker.failed)
        worker.run()
        assert done == [(1,)] and failed == []
        assert "\\Seen" in server.flags["7"]
        assert server.readonly is False
        assert server.logged_out

    def test_a_refused_password_is_reported(self, qapp, server_engine, account):
        server_engine()
        worker = workers_module.FlagWorker(account, "wrong", ["7"], "seen")
        failed = caught(worker.failed)
        worker.run()
        assert failed and failed[0][0] == "Could not mark the message"

    def test_archive_goes_to_the_archive_folder(self, qapp, server_engine, account):
        server = server_engine(messages={"7": b"raw"})
        plan = MovePlan(uid="7", target_folder="", subject="x")
        worker = workers_module.MoveWorker(account, "app-specific", [plan], "archive")
        done = caught(worker.done)
        worker.run()
        report = done[0][0]
        assert report.moved == {"7": "Archive"}
        assert report.new_uids == {"7": "9001"}
        assert ("7", "Archive") in server.copies and "7" in server.expunged

    def test_archive_makes_the_folder_when_there_is_none(self, qapp, server_engine, account):
        server = server_engine(folders=["INBOX"], messages={"7": b"raw"})
        worker = workers_module.MoveWorker(account, "app-specific",
                                           [MovePlan(uid="7", target_folder="")], "archive")
        done = caught(worker.done)
        worker.run()
        assert "Archive" in server.folders and done[0][0].moved == {"7": "Archive"}

    def test_trash_and_junk_are_found_under_their_names(self, qapp, server_engine, account):
        server = server_engine(folders=["INBOX", "Deleted Messages", "Junk"],
                               messages={"7": b"raw", "8": b"raw"})
        worker = workers_module.MoveWorker(account, "app-specific",
                                           [MovePlan(uid="7", target_folder="")], "trash")
        done = caught(worker.done)
        worker.run()
        assert done[0][0].moved == {"7": "Deleted Messages"}
        worker = workers_module.MoveWorker(account, "app-specific",
                                           [MovePlan(uid="8", target_folder="")], "junk")
        done = caught(worker.done)
        worker.run()
        assert done[0][0].moved == {"8": "Junk"}
        assert server.expunged == ["7", "8"]

    def test_no_trash_folder_is_said_in_words(self, qapp, server_engine, account):
        server_engine(folders=["INBOX"], messages={"7": b"raw"})
        worker = workers_module.MoveWorker(account, "app-specific",
                                           [MovePlan(uid="7", target_folder="")], "trash")
        failed = caught(worker.failed)
        worker.run()
        assert failed and "no Trash folder" in failed[0][1]

    def test_a_folder_by_name_is_made_with_its_parents(self, qapp, server_engine, account):
        server = server_engine(folders=["INBOX"], messages={"7": b"raw"})
        plan = MovePlan(uid="7", target_folder="Job Search/Interview", source_folder="INBOX")
        worker = workers_module.MoveWorker(account, "app-specific", [plan], "folder")
        done = caught(worker.done)
        worker.run()
        assert server.folders == ["INBOX", "Job Search", "Job Search/Interview"]
        assert done[0][0].moved == {"7": "Job Search/Interview"}

    def test_a_draft_lands_in_drafts(self, qapp, server_engine, account):
        server = server_engine(folders=["INBOX", "Drafts"])
        draft = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="Later")
        worker = workers_module.DraftWorker(account, "app-specific", draft)
        saved = caught(worker.saved)
        worker.run()
        assert saved == [("Drafts",)]
        mailbox, flags, raw = server.appended[0]
        assert mailbox == "Drafts" and "\\Draft" in flags and b"Subject: Later" in raw

    def test_sending_keeps_a_copy_and_marks_the_answer(self, qapp, server_engine, account,
                                                       monkeypatch):
        server = server_engine(folders=["INBOX", "Sent Messages"], messages={"7": b"raw"})
        handed = []
        monkeypatch.setattr(outgoing, "send",
                            lambda raw, host, address, password, sender, recipients:
                            handed.append((host, address, password, sender, list(recipients))))
        draft = outgoing.Draft(from_address="you@icloud.example", from_name="You",
                               to=["Dana <dana@northwind.example>"], subject="Re: x")
        worker = workers_module.SendWorker(account, "app-specific", draft,
                                           answering=("7", "INBOX"))
        sent, failed = caught(worker.sent), caught(worker.failed)
        worker.run()
        assert failed == [] and sent == [("",)]
        assert handed == [(outgoing.SMTP_HOSTS["icloud"], "you@icloud.example",
                           "app-specific", "you@icloud.example",
                           ["Dana <dana@northwind.example>"])]
        mailbox, flags, raw = server.appended[0]
        assert mailbox == "Sent Messages" and "\\Seen" in flags
        assert b"Subject: Re: x" in raw
        assert "\\Answered" in server.flags["7"]

    def test_gmail_keeps_its_own_copy(self, qapp, server_engine, monkeypatch):
        from accounts import Account

        server = server_engine(folders=["INBOX", "[Gmail]/Sent Mail"])
        monkeypatch.setattr(outgoing, "send", lambda *a, **k: None)
        draft = outgoing.Draft(from_address="you@gmail.example", to=["a@b.example"],
                               subject="x")
        worker = workers_module.SendWorker(
            Account(address="you@gmail.example", preset="gmail"), "p", draft)
        sent = caught(worker.sent)
        worker.run()
        assert sent == [("",)] and server.appended == [] and not server.logged_in

    def test_a_refusal_stops_everything(self, qapp, server_engine, account, monkeypatch):
        server = server_engine(folders=["INBOX", "Sent Messages"])

        def refuse(*args, **kwargs):
            raise outgoing.SendError("The server refused the password.")

        monkeypatch.setattr(outgoing, "send", refuse)
        draft = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="x")
        worker = workers_module.SendWorker(account, "app-specific", draft)
        sent, failed = caught(worker.sent), caught(worker.failed)
        worker.run()
        assert sent == [] and failed == [("The server refused the password.",)]
        assert server.appended == []

    def test_a_lost_copy_is_noted_not_fatal(self, qapp, server_engine, account, monkeypatch):
        server_engine(folders=["INBOX"])         # no Sent mailbox
        monkeypatch.setattr(outgoing, "send", lambda *a, **k: None)
        draft = outgoing.Draft(from_address="you@icloud.example", to=["a@b.example"],
                               subject="x")
        worker = workers_module.SendWorker(account, "app-specific", draft)
        sent = caught(worker.sent)
        worker.run()
        assert sent and sent[0][0].startswith("Sent. The copy")

    def test_attachments_are_fetched_for_a_forward(self, qapp, monkeypatch, account):
        import attachments

        class Stub:
            def __init__(self, *args, **kwargs):
                self.selected = None

            def connect(self, address, password):
                pass

            def select(self, mailbox, readonly=True):
                self.selected = (mailbox, readonly)

            def fetch_attachments(self, uid):
                return [
                    attachments.Attachment(part="2", name="cv.pdf",
                                           content_type="application/pdf; name=cv.pdf",
                                           data=b"%PDF"),
                    attachments.Attachment(part="3", name="smime.p7s",
                                           content_type="application/pkcs7-signature",
                                           data=b"sig"),
                    attachments.Attachment(part="4", name="huge.bin",
                                           data=b"0" * (outgoing.MOST_BYTES + 1)),
                ]

            def logout(self):
                pass

        monkeypatch.setattr(workers_module, "IMAPEngine", Stub)
        message = EmailMessage(uid="7", source_folder="INBOX")
        worker = workers_module.AttachmentFetchWorker(account, "p", message)
        ready = caught(worker.ready)
        worker.run()
        carried, left_out = ready[0]
        assert [(a.name, a.mime) for a in carried] == [("cv.pdf", "application/pdf")]
        assert left_out == ["huge.bin"]


# -- The server side --------------------------------------------------------

class TestTheEngine:
    def test_special_mailboxes_are_found_by_flag_then_by_name(self, monkeypatch):
        engine = IMAPEngine()
        listed = [MailboxInfo("INBOX", "/"),
                  MailboxInfo("Gesendet", "/", ("\\HasNoChildren", "\\Sent")),
                  MailboxInfo("Sent Messages", "/"),
                  MailboxInfo("Trash", "/", ("\\Noselect", "\\Trash")),
                  MailboxInfo("Deleted Messages", "/"),
                  MailboxInfo("[Gmail]/All Mail", "/", ("\\All",))]
        monkeypatch.setattr(engine, "list_folders", lambda: listed)
        assert engine.special_mailbox("sent") == "Gesendet"
        assert engine.special_mailbox("trash") == "Deleted Messages", "not the unselectable one"
        assert engine.special_mailbox("archive") == "[Gmail]/All Mail"
        assert engine.special_mailbox("junk") is None
        assert engine.drafts_mailbox() is None

    def test_special_mailboxes_survive_a_failed_listing(self, monkeypatch):
        engine = IMAPEngine()

        def broken():
            raise IMAPError("gone")

        monkeypatch.setattr(engine, "list_folders", broken)
        assert engine.special_mailbox("sent") is None

    def test_a_sent_copy_is_appended_read(self, fake_imap_factory):
        server = fake_imap_factory(folders=["INBOX", "Sent Messages"])
        engine = IMAPEngine(connection_factory=lambda host, port: server)
        engine.connect("you@icloud.example", "app-specific")
        assert engine.save_sent(b"raw") == "Sent Messages"
        assert server.appended == [("Sent Messages", "(\\Seen)", b"raw")]
        bare = fake_imap_factory(folders=["INBOX"])
        engine = IMAPEngine(connection_factory=lambda host, port: bare)
        engine.connect("you@icloud.example", "app-specific")
        with pytest.raises(IMAPError, match="no Sent mailbox"):
            engine.save_sent(b"raw")

    def test_cc_is_read_from_the_message(self):
        from imap_engine import parse_message
        from tests.conftest import build_mime

        raw = build_mime(extra_headers={"Cc": "Pat <pat@acme.example>"})
        assert parse_message(raw, "1").cc == "Pat <pat@acme.example>"
        assert parse_message(build_mime(), "2").cc == ""


class TestTheAccount:
    def test_outgoing_server_fields_are_kept_and_cleaned(self):
        from accounts import Account

        account = Account(address="a@corp.example", preset="custom", host="imap.corp.example",
                          smtp_host=" out.corp.example ", smtp_port="465")
        assert (account.smtp_host, account.smtp_port) == ("out.corp.example", 465)
        again = Account.from_dict(account.to_dict())
        assert (again.smtp_host, again.smtp_port) == ("out.corp.example", 465)
        assert outgoing.smtp_for(again) == outgoing.SmtpHost("out.corp.example", 465, False)
        odd = Account(address="a@corp.example", smtp_port="lots")
        assert odd.smtp_port == 0
        assert Account(address="a@corp.example", smtp_port=70000).smtp_port == 0
        old = Account.from_dict({"address": "a@icloud.example", "preset": "icloud"})
        assert (old.smtp_host, old.smtp_port) == ("", 0)

    def test_the_settings_form_edits_them(self, qapp):
        from settings_dialog import SettingsDialog

        settings = Settings(icloud_email="you@icloud.example")
        dialog = SettingsDialog(settings, InMemoryCredentialStore())
        try:
            dialog.smtp_host_edit.setText("out.corp.example")
            dialog.smtp_port_spin.setValue(465)
            account = dialog._accounts[dialog._account_index]
            assert (account.smtp_host, account.smtp_port) == ("out.corp.example", 465)
            assert dialog.smtp_port_spin.specialValueText() == "usual"
            dialog._show_account(dialog._account_index)
            assert dialog.smtp_host_edit.text() == "out.corp.example"
            assert dialog.advanced_box.isChecked(), "an outgoing server of its own shows"
        finally:
            dialog.deleteLater()


class TestThePreviewPane:
    def test_the_reply_button_writes_back_and_opens(self, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        try:
            assert not pane.reply_button.isEnabled()
            asked = []
            pane.composeRequested.connect(lambda mode, row: asked.append((mode, row)))
            pane.openRequested.connect(lambda row: asked.append(("open", row)))
            pane.show_item(4, item("1"))
            assert pane.reply_button.isEnabled()
            labels = [a.text() for a in pane.reply_menu.actions() if a.text()]
            assert labels == ["Reply", "Reply All", "Forward", "Open in a Window"]
            for action in pane.reply_menu.actions():
                if action.text():
                    action.trigger()
            assert asked == [("reply", 4), ("reply_all", 4), ("forward", 4), ("open", 4)]
            pane.clear()
            assert not pane.reply_button.isEnabled()
        finally:
            pane.deleteLater()

    def test_the_header_says_unread_and_flagged(self, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        try:
            pane.show_item(0, item("1", flags=()))
            assert "Unread" in pane.header.text() and "Flagged" not in pane.header.text()
            pane.show_item(0, item("1", flags=("\\Seen", "\\Flagged")))
            assert "Unread" not in pane.header.text() and "⚑ Flagged" in pane.header.text()
        finally:
            pane.deleteLater()


class TestThePreviewOfAnEmptyMessage:
    def test_an_html_shell_shows_as_text_naming_its_attachments(self, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        try:
            empty = item("1", body_html="<html><body><div><br></div></body></html>",
                         body_text="", attachments=("notes.md",))
            pane.show_item(0, empty)
            assert pane.body_stack.currentWidget() is pane.body_view
            assert "This message has no text" in pane.body_view.toPlainText()
            assert "notes.md" in pane.body_view.toPlainText()
            full = item("2")
            pane.show_item(1, full)
            assert pane.body_stack.currentWidget() is pane.rich_view
        finally:
            pane.deleteLater()
