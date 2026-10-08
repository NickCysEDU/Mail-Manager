"""The window as a mailbox: the inbox listed when the app opens, the other
mailboxes beside it, and a scan's verdicts shown over the listing.

Every message here is invented.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402

from triage_table import (NOT_SORTED, TriageFilterProxy,  # noqa: E402
                          TriageTableModel)

COL = TriageTableModel


def _cell(model, column, role=Qt.ItemDataRole.DisplayRole, row=0):
    return model.data(model.index(row, column), role)


class TestARowNotSortedYet:
    """Listed before any scan read it: the table shows the message, and
    nothing that reads as a verdict."""

    @pytest.fixture
    def model(self, qapp, item_factory):
        made = TriageTableModel()
        made.set_items([item_factory(analysed=False, email_kwargs={
            "body_text": "Hello   there,\nthe signed forms are attached."})])
        return made

    def test_it_shows_how_the_message_begins_and_no_verdict(self, model):
        assert _cell(model, COL.COL_SUMMARY) == "Hello there, the signed forms are attached."
        for column in (COL.COL_CATEGORY, COL.COL_CONFIDENCE, COL.COL_REASONING):
            assert _cell(model, column) == "", column
        assert _cell(model, COL.COL_CATEGORY, Qt.ItemDataRole.UserRole + 1) is None
        assert _cell(model, COL.COL_SELECT, Qt.ItemDataRole.CheckStateRole) is None
        assert _cell(model, COL.COL_CONFIDENCE, Qt.ItemDataRole.ToolTipRole) == \
            NOT_SORTED + "."

    def test_it_sorts_below_every_verdict(self, model, item_factory):
        model.set_items([model.items[0], item_factory(
            classification_kwargs={"confidence_score": 0.0})])
        listed = _cell(model, COL.COL_CONFIDENCE, Qt.ItemDataRole.UserRole, row=0)
        unsure = _cell(model, COL.COL_CONFIDENCE, Qt.ItemDataRole.UserRole, row=1)
        assert listed < unsure

    def test_no_confidence_bar_is_drawn_for_it(self, model, item_factory, qtbot):
        """The pixels: a sorted row's cell has its bar and its number, a
        listed row's cell nothing at all, not even an empty track or 0%."""
        from PySide6.QtWidgets import QTableView

        from triage_table import ConfidenceDelegate

        model.set_items([model.items[0], item_factory()])
        view = QTableView()
        qtbot.addWidget(view)
        view.setModel(model)
        view.setItemDelegateForColumn(COL.COL_CONFIDENCE, ConfidenceDelegate())
        view.setAlternatingRowColors(False)
        view.resize(1200, 200)
        view.show()
        qtbot.waitExposed(view)
        image = view.viewport().grab().toImage()
        ratio = image.devicePixelRatio()

        def inked(row):
            """Pixels in the cell unlike its own background."""
            rect = view.visualRect(model.index(row, COL.COL_CONFIDENCE)).adjusted(2, 2, -2, -2)
            ground = image.pixelColor(int(rect.left() * ratio), int(rect.top() * ratio))
            count = 0
            for x in range(int(rect.left() * ratio), int(rect.right() * ratio)):
                for y in range(int(rect.top() * ratio), int(rect.bottom() * ratio)):
                    colour = image.pixelColor(x, y)
                    if (abs(colour.red() - ground.red()) + abs(colour.green() - ground.green())
                            + abs(colour.blue() - ground.blue())) > 24:
                        count += 1
            return count

        assert inked(1) > 40, "the sorted row's bar and number were not found"
        assert inked(0) == 0

    def test_the_category_filter_can_show_just_these(self, model, item_factory):
        model.set_items([model.items[0], item_factory()])
        proxy = TriageFilterProxy()
        proxy.setSourceModel(model)
        proxy.set_category_filter(NOT_SORTED)
        assert proxy.rowCount() == 1
        assert proxy.mapToSource(proxy.index(0, 0)).row() == 0
        proxy.set_category_filter(model.items[1].classification.category_label)
        assert proxy.mapToSource(proxy.index(0, 0)).row() == 1

    def test_search_reads_how_it_begins(self, model):
        proxy = TriageFilterProxy()
        proxy.setSourceModel(model)
        proxy.set_text_filter("signed forms")
        assert proxy.rowCount() == 1
        proxy.set_text_filter("Unclassified")
        assert proxy.rowCount() == 0, "an empty verdict was searched"


class TestSentMailSaysWhoItWentTo:
    def test_the_column_names_the_recipient(self, qapp, item_factory):
        model = TriageTableModel()
        model.set_items([item_factory(analysed=False, fileable=False, email_kwargs={
            "to": "Odile Farrant <odile@northwind.example>, ops@northwind.example"})])
        model.set_addressed(True)
        assert model.headerData(COL.COL_SENDER, Qt.Orientation.Horizontal) == "To"
        assert _cell(model, COL.COL_SENDER) == "Odile Farrant +1"
        model.set_addressed(False)
        assert model.headerData(COL.COL_SENDER, Qt.Orientation.Horizontal) == "Sender"
        assert _cell(model, COL.COL_SENDER) == "Dana Reyes"


class _Wired:
    """The workers' engine pointed at a fake server, and what they said."""

    def __init__(self, monkeypatch, server):
        import workers
        from imap_engine import IMAPEngine

        self.server = server
        monkeypatch.setattr(workers, "IMAPEngine", lambda host=None, port=None:
                            IMAPEngine(connection_factory=lambda h, p: server))

    @staticmethod
    def run(worker):
        heard = {"arrived": [], "done": [], "failed": []}
        if hasattr(worker, "arrived"):
            worker.arrived.connect(lambda rows: heard["arrived"].append(list(rows)))
        worker.finished_ok.connect(heard["done"].append)
        worker.failed.connect(lambda *why: heard["failed"].append(why))
        worker.run()
        return heard


def _account(settings):
    return settings.accounts[0]


@pytest.fixture
def settings(tmp_path, monkeypatch):
    from config import Settings

    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    return Settings(icloud_email="you@icloud.example").normalized()


def _since(day="01-Sep-2026"):
    from datetime import datetime, timezone

    return datetime.strptime(day, "%d-%b-%Y").replace(tzinfo=timezone.utc)


class TestListingAFolder:
    #: A mailbox numbers its messages as they arrive: the highest UID is
    #: the newest.
    MESSAGES = {
        "1": ("An old receipt", "02-Jan-2026 09:00:00 +0000"),
        "2": ("Weekly digest", "02-Sep-2026 09:00:00 +0000"),
        "3": ("Your interview on Thursday", "03-Sep-2026 09:00:00 +0000"),
    }

    @pytest.fixture
    def server(self, monkeypatch, fake_imap_factory, mime_factory):
        made = fake_imap_factory(
            folders=["INBOX", "Sent Messages"],
            messages={uid: mime_factory(subject=subject)
                      for uid, (subject, _when) in self.MESSAGES.items()},
            internaldates={uid: when for uid, (_s, when) in self.MESSAGES.items()})
        _Wired(monkeypatch, made)
        return made

    def _worker(self, settings, folder="INBOX", fileable=True):
        from workers import ListWorker

        return ListWorker(_account(settings), "app-specific", folder, _since(),
                          settings, settings.folder_plan(), fileable=fileable)

    def test_the_inbox_is_listed_unsorted_and_untouched(self, qapp, settings, server):
        heard = _Wired.run(self._worker(settings))
        assert heard["failed"] == []
        [listing] = heard["done"]
        subjects = sorted(row.email.subject for row in listing.rows)
        assert subjects == ["Weekly digest", "Your interview on Thursday"]
        for row in listing.rows:
            assert row.analysed is False and row.fileable is True
            assert row.approved is False
            assert row.email.account_id == _account(settings).id
            assert row.email.source_folder == "INBOX"
        # Nothing marked read, nothing made, nothing moved.
        names = {name for name, _args in server.commands}
        assert not names & {"UID STORE", "CREATE", "UID COPY", "UID MOVE",
                            "EXPUNGE", "SUBSCRIBE"}, names

    def test_rows_arrive_as_they_are_read_and_only_once(self, qapp, settings, server):
        heard = _Wired.run(self._worker(settings))
        streamed = [row for batch in heard["arrived"] for row in batch]
        [listing] = heard["done"]
        assert sorted(map(id, streamed)) == sorted(map(id, listing.rows))

    def test_another_folder_is_never_fileable(self, qapp, settings, server):
        heard = _Wired.run(self._worker(settings, "Sent Messages", fileable=False))
        [listing] = heard["done"]
        assert listing.rows and not any(row.fileable for row in listing.rows)
        assert {row.email.source_folder for row in listing.rows} == {"Sent Messages"}

    def test_a_fetch_that_does_not_stream_still_lists_everything(
            self, qapp, settings, server, monkeypatch):
        """Rows come as each batch arrives; whatever a fetch hands back
        without streaming is sent at the end, once."""
        from imap_engine import IMAPEngine

        whole = IMAPEngine.fetch_window

        def all_at_once(engine, *args, **kwargs):
            kwargs["on_batch"] = None
            return whole(engine, *args, **kwargs)

        monkeypatch.setattr(IMAPEngine, "fetch_window", all_at_once)
        heard = _Wired.run(self._worker(settings))
        streamed = [row for batch in heard["arrived"] for row in batch]
        [listing] = heard["done"]
        assert len(listing.rows) == 2
        assert sorted(map(id, streamed)) == sorted(map(id, listing.rows))

    def test_the_newest_are_kept_when_the_period_holds_too_many(
            self, qapp, settings, server, monkeypatch):
        import workers

        monkeypatch.setattr(workers, "LIST_MOST", 1)
        heard = _Wired.run(self._worker(settings))
        [listing] = heard["done"]
        assert listing.capped is True
        assert [row.email.subject for row in listing.rows] == [
            "Your interview on Thursday"]


class TestListingTheMailboxesOfAnAccount:
    def test_the_special_ones_are_found_and_every_folder_listed(
            self, qapp, settings, monkeypatch, fake_imap_factory):
        from workers import MailboxesWorker

        server = fake_imap_factory(folders=[
            "INBOX", "Drafts", "Sent Messages", "Junk", "Deleted Messages",
            "Archive", "Job Search", "Job Search/Interview", "Receipts"])
        _Wired(monkeypatch, server)
        heard = _Wired.run(MailboxesWorker(_account(settings), "app-specific"))
        [found] = heard["done"]
        assert found.account_id == _account(settings).id
        assert found.special == {"drafts": "Drafts", "sent": "Sent Messages",
                                 "junk": "Junk", "trash": "Deleted Messages",
                                 "archive": "Archive"}
        assert "Job Search/Interview" in found.folders and "Receipts" in found.folders
        assert found.delimiter == "/"


def _found(account_id, folders, special, delimiter="/"):
    from workers import Mailboxes

    return Mailboxes(account_id=account_id, delimiter=delimiter,
                     folders=list(folders), special=dict(special))


ICLOUD_FOLDERS = ["INBOX", "Drafts", "Sent Messages", "Junk", "Deleted Messages",
                  "Archive", "Job Search", "Job Search/Interview", "Receipts",
                  "Lists/Work"]
ICLOUD_SPECIAL = {"drafts": "Drafts", "sent": "Sent Messages", "junk": "Junk",
                  "trash": "Deleted Messages", "archive": "Archive"}


@pytest.fixture
def two_accounts(tmp_path, monkeypatch):
    from accounts import Account
    from config import Settings

    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    made = Settings(icloud_email="you@icloud.example")
    made.mailboxes = [
        Account(address="you@icloud.example", host="imap.mail.me.com", label="Personal"),
        Account(address="work@elsewhere.example", host="imap.work.example",
                label="Work", source_mailbox="INBOX"),
    ]
    return made.normalized()


class TestTheSidebar:
    def _titles(self, sidebar):
        out = []
        for item in sidebar._walk():
            depth, parent = 0, item.parent()
            while parent is not None:
                depth, parent = depth + 1, parent.parent()
            out.append("  " * depth + item.text(0))
        return out

    def test_an_account_is_called_what_mail_calls_it(self, qapp, settings,
                                                      two_accounts):
        from dataclasses import replace

        from sidebar import heading

        unnamed = _account(settings)
        assert unnamed.label == "you", "named after the address by default"
        assert heading(unnamed, [unnamed]) == "iCloud"
        first, second = two_accounts.accounts
        assert heading(first, two_accounts.accounts) == "Personal"
        assert heading(second, two_accounts.accounts) == "Work", \
            "a name given that happens to match the address is still given"
        twin = replace(unnamed, address="other@icloud.example", id="twin",
                       label="other")
        assert heading(unnamed, [unnamed, twin]) == unnamed.address
        nowhere = replace(unnamed, host="imap.unknown.example", preset="custom")
        assert heading(nowhere, [nowhere]) == nowhere.address

    def test_before_the_folders_arrive_there_is_the_inbox(self, qapp, settings):
        from sidebar import INBOX, MailboxList

        sidebar = MailboxList()
        sidebar.set_accounts(settings.accounts)
        assert sidebar.places() == [INBOX]
        assert sidebar.place() == INBOX

    def test_it_lists_what_mail_lists_and_each_folder_once(self, qapp, settings):
        from sidebar import MailboxList, Place

        sidebar = MailboxList()
        account = _account(settings)
        sidebar.set_accounts(settings.accounts)
        sidebar.set_mailboxes(_found(account.id, ICLOUD_FOLDERS, ICLOUD_SPECIAL))
        assert self._titles(sidebar) == [
            "Mailboxes", "  Inbox", "  Drafts", "  Sent", "  Junk", "  Trash",
            "  Archive", "iCloud", "  Job Search", "    Interview",
            "  Lists", "    Work", "  Receipts"]
        places = sidebar.places()
        assert Place("folder", account.id, "Job Search/Interview") in places
        assert Place("folder", account.id, "Lists/Work") in places
        # Not a folder the server lets be opened: only a heading.
        assert Place("folder", account.id, "Lists") not in places
        assert Place("folder", account.id, "Sent Messages") not in places

    def test_with_two_accounts_each_mailbox_gathers_both(self, qapp, two_accounts):
        from sidebar import MailboxList, Place

        first, second = two_accounts.accounts
        sidebar = MailboxList()
        sidebar.set_accounts(two_accounts.accounts)
        sidebar.set_mailboxes(_found(first.id, ICLOUD_FOLDERS, ICLOUD_SPECIAL))
        sidebar.set_mailboxes(_found(second.id, ["INBOX", "Sent"], {"sent": "Sent"}))
        titles = self._titles(sidebar)
        assert titles[:9] == ["Mailboxes", "  Inbox", "    Personal", "    Work",
                              "  Drafts", "    Personal", "  Sent", "    Personal",
                              "    Work"]
        assert Place("inbox", second.id) in sidebar.places()
        assert sidebar.folder_of(Place("sent"), second.id) == "Sent"
        assert sidebar.folder_of(Place("drafts"), second.id) is None
        assert sidebar.folder_of(Place("inbox"), first.id) == first.source_mailbox
        folder = Place("folder", first.id, "Receipts")
        assert sidebar.folder_of(folder, first.id) == "Receipts"
        assert sidebar.folder_of(folder, second.id) is None

    def test_where_a_gathered_mailbox_reaches(self, qapp, two_accounts):
        from sidebar import MailboxList, Place, accounts_having

        first, second = two_accounts.accounts
        sidebar = MailboxList()
        sidebar.set_accounts(two_accounts.accounts)
        sidebar.set_mailboxes(_found(first.id, ICLOUD_FOLDERS, ICLOUD_SPECIAL))
        sidebar.set_mailboxes(_found(second.id, ["INBOX"], {}))
        reach = accounts_having(sidebar, Place("drafts"), two_accounts.accounts)
        assert [(account.id, folder) for account, folder in reach] == [(first.id, "Drafts")]
        reach = accounts_having(sidebar, Place("inbox", second.id), two_accounts.accounts)
        assert [(account.id, folder) for account, folder in reach] == [(second.id, "INBOX")]

    def test_a_choice_by_hand_is_announced_and_the_windows_is_not(self, qapp, settings):
        from sidebar import INBOX, MailboxList, Place

        account = _account(settings)
        sidebar = MailboxList()
        sidebar.set_accounts(settings.accounts)
        sidebar.set_mailboxes(_found(account.id, ICLOUD_FOLDERS, ICLOUD_SPECIAL))
        heard = []
        sidebar.chosen.connect(heard.append)
        sent = next(item for item in sidebar._walk() if item.text(0) == "Sent")
        sidebar.setCurrentItem(sent)
        assert heard == [Place("sent")]
        sidebar.choose(INBOX)
        assert heard == [Place("sent")] and sidebar.place() == INBOX
        heading = next(item for item in sidebar._walk() if item.text(0) == "Mailboxes")
        sidebar.setCurrentItem(heading)
        assert heard == [Place("sent")], "a heading is not a mailbox"

    def test_a_mailbox_that_goes_hands_back_to_the_inbox(self, qapp, two_accounts):
        from sidebar import INBOX, MailboxList, Place

        first, second = two_accounts.accounts
        sidebar = MailboxList()
        sidebar.set_accounts(two_accounts.accounts)
        sidebar.set_mailboxes(_found(second.id, ["INBOX", "Projects"], {}))
        sidebar.choose(Place("folder", second.id, "Projects"))
        assert sidebar.place() == Place("folder", second.id, "Projects")
        sidebar.set_accounts([first])
        assert sidebar.place() == INBOX


def _days_ago(days):
    from datetime import datetime, timedelta, timezone

    when = datetime.now(timezone.utc) - timedelta(days=days)
    return when.strftime("%d-%b-%Y %H:%M:%S +0000")


#: An inbox over six weeks, numbered as a mailbox numbers its mail.
INBOX = {"1": ("A receipt from last season", 45),
         "2": ("Weekly digest", 10),
         "3": ("Your interview on Thursday", 2)}


@pytest.fixture
def opened(qapp, tmp_path, monkeypatch, fake_imap_factory, mime_factory):
    """A window with one account, its password stored, and a server
    holding a few weeks of inbox. Not shown: each test opens what it means
    to."""
    from config import InMemoryCredentialStore, Settings
    from gui import MainWindow

    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    settings = Settings(icloud_email="you@icloud.example", provider="rules").normalized()
    store = InMemoryCredentialStore()
    store.set_mailbox_password("you@icloud.example", "app-specific")
    server = fake_imap_factory(
        folders=ICLOUD_FOLDERS,
        messages={uid: mime_factory(subject=subject, to="Odile Farrant <odile@northwind.example>")
                  for uid, (subject, _days) in INBOX.items()},
        internaldates={uid: _days_ago(days) for uid, (_s, days) in INBOX.items()})
    _Wired(monkeypatch, server)
    window = MainWindow(settings, store)
    yield window, server
    window.shutdown()
    window.close()
    window.deleteLater()


def _settle(qtbot, window):
    qtbot.waitUntil(lambda: not window._listings and not window._mailbox_workers,
                    timeout=15000)


class TestTheWindowOpensOnItsInbox:
    def test_the_inbox_of_the_period_fills_the_table_unsorted(
            self, opened, qtbot, dialog_calls):
        window, server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        subjects = sorted(item.email.subject for item in window.model.items)
        assert subjects == ["Weekly digest", "Your interview on Thursday"]
        assert all(not item.analysed and item.fileable for item in window.model.items)
        assert window.table_stack.currentIndex() == 1
        assert window.apply_button.isEnabled() is False
        assert dialog_calls == [], "nothing may be asked at launch"
        window.table.selectRow(0)
        assert window.preview.analysis_label.text() == "Not analysed yet"
        assert "Not sorted yet" in window.preview.header.text()
        assert "Inbox" in window.preview.header.text()
        assert not window.preview.folder_row.isHidden(), "filed by hand from here"
        names = {name for name, _args in server.commands}
        assert not names & {"UID STORE", "CREATE", "UID COPY", "EXPUNGE"}
        # And the sidebar has the account's mailboxes.
        from sidebar import Place

        assert Place("sent") in window.sidebar.places()

    def test_the_period_is_the_one_chosen(self, opened, qtbot):
        window, _server = opened
        window.settings.inbox_days = 7
        window._open_mailboxes()
        _settle(qtbot, window)
        assert [item.email.subject for item in window.model.items] == [
            "Your interview on Thursday"]

    def test_it_can_be_turned_off(self, opened, qtbot):
        window, server = opened
        window.settings.open_with_inbox = False
        window._open_mailboxes()
        _settle(qtbot, window)
        assert window.model.items == []
        assert "UID FETCH" not in {name for name, _args in server.commands}

    def test_without_a_password_it_says_so_and_asks_nothing(
            self, opened, qtbot, dialog_calls):
        window, _server = opened
        window.store.set_mailbox_password("you@icloud.example", "")
        window._open_mailboxes()
        _settle(qtbot, window)
        assert window.model.items == [] and dialog_calls == []
        assert "no app password is stored" in window.log_view.toPlainText()

    def test_a_scan_shows_its_verdicts_over_the_listing(self, opened, qtbot,
                                                        item_factory):
        from models import message_key
        from workers import ScanOutcome

        window, _server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        account = window.settings.accounts[0]
        read = item_factory(email_kwargs={
            "uid": "3", "account_id": account.id, "source_folder": "INBOX",
            "subject": "Your interview on Thursday"})
        window._on_scan_done(ScanOutcome(items=[read]))
        rows = {message_key(item): item for item in window.model.items}
        assert len(rows) == 2
        assert rows[message_key(read)] is read
        others = [item for item in rows.values() if item is not read]
        assert [item.analysed for item in others] == [False]
        assert window.apply_button.isEnabled() is True, "the scan's row is ticked"

    def test_a_message_filed_leaves_the_listing(self, opened, qtbot):
        from imap_engine import MoveReport

        window, _server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        row = next(item for item in window.model.items
                   if item.email.subject == "Weekly digest")
        model_row = window.model.items.index(row)
        window.model.set_override(model_row, "Job Search/Interview")
        assert row.approved and row.is_actionable, "filed by hand from the inbox"
        account = window.settings.accounts[0]
        report = MoveReport()
        report.add(account.id, "INBOX", MoveReport(
            moved={row.email.uid: "Job Search/Interview"},
            new_uids={row.email.uid: "70"}))
        window._record_moves(report)
        assert row.moved is True
        from models import message_key

        assert message_key(row) not in window._listed
        assert window._undo_stack, "and the filing can be undone"


class TestAutoScan:
    def test_it_runs_once_the_inbox_is_listed(self, opened, qtbot, monkeypatch):
        window, _server = opened
        ran = []
        monkeypatch.setattr(window, "_begin_scan",
                            lambda *a, **k: ran.append(len(window.model.items)))
        window.settings.scan_on_open = True
        window._open_mailboxes()
        _settle(qtbot, window)
        assert ran == [2], "once, with the inbox already on screen"

    def test_it_runs_at_once_without_a_listing(self, opened, qtbot, monkeypatch):
        window, _server = opened
        ran = []
        monkeypatch.setattr(window, "_begin_scan", lambda *a, **k: ran.append(1))
        window.settings.scan_on_open = True
        window.settings.open_with_inbox = False
        window._open_mailboxes()
        assert ran == [1]

    def test_it_never_asks_and_says_what_was_missing(
            self, opened, qtbot, monkeypatch, dialog_calls):
        window, _server = opened
        ran = []
        monkeypatch.setattr(window, "_begin_scan", lambda *a, **k: ran.append(1))
        window.settings.scan_on_open = True
        window.settings.provider = "anthropic"
        window._open_mailboxes()
        _settle(qtbot, window)
        assert ran == [] and dialog_calls == []
        assert "Auto scan did not run" in window.log_view.toPlainText()
        assert "API key" in window._status_text

    def test_it_is_off_unless_chosen(self, opened, qtbot, monkeypatch):
        window, _server = opened
        ran = []
        monkeypatch.setattr(window, "_begin_scan", lambda *a, **k: ran.append(1))
        window._open_mailboxes()
        _settle(qtbot, window)
        assert ran == []


class TestOtherMailboxes:
    def _opened_at(self, qtbot, window, place):
        """Opened, then the mailbox chosen in the sidebar, as a click does."""
        window._open_mailboxes()
        _settle(qtbot, window)
        window.sidebar.chosen.emit(place)
        _settle(qtbot, window)

    def test_sent_mail_is_listed_with_who_it_went_to_and_never_filed(
            self, opened, qtbot):
        from sidebar import Place

        window, _server = opened
        self._opened_at(qtbot, window, Place("sent"))
        assert window.sidebar.place() == Place("sent")
        assert window.model.items, "nothing listed"
        assert all(not item.fileable and item.email.source_folder == "Sent Messages"
                   for item in window.model.items)
        assert window.model.addressed
        assert window.model.data(window.model.index(0, COL.COL_SENDER)) == "Odile Farrant"
        window.model.set_override(0, "Job Search/Interview")
        assert window.model.items[0].override_folder is None
        window.table.selectRow(0)
        assert window.preview.folder_combo.isEnabled() is False
        assert window.preview.folder_row.isHidden(), "nothing to file it into"
        assert window.model.items[0].status_display == "In Sent Messages"
        assert "Not sorted yet" not in window.preview.header.text()
        assert window.preview.analysis_label.text() == "Not analysed"
        window.preview.clear()
        assert not window.preview.folder_row.isHidden()
        assert window.preview.analysis_label.text().endswith("nalysis")

    def test_back_to_the_inbox_without_reading_it_again(self, opened, qtbot):
        from sidebar import INBOX, Place

        window, server = opened
        self._opened_at(qtbot, window, Place("sent"))
        fetches = len([1 for name, _a in server.commands if name == "UID FETCH"])
        window._go_to(INBOX)
        assert sorted(item.email.subject for item in window.model.items) == [
            "Weekly digest", "Your interview on Thursday"]
        assert not window.model.addressed
        assert len([1 for name, _a in server.commands if name == "UID FETCH"]) == fetches

    def test_ticks_in_the_inbox_are_filed_from_anywhere(self, opened, qtbot,
                                                        item_factory):
        """Apply counts the inbox's ticked rows, whatever the table shows,
        and never anything listed from another mailbox."""
        from sidebar import Place
        from workers import ScanOutcome, build_move_plans

        window, _server = opened
        account = window.settings.accounts[0]
        read = item_factory(email_kwargs={"uid": "3", "account_id": account.id,
                                          "source_folder": "INBOX"})
        self._opened_at(qtbot, window, Place("sent"))
        window._on_scan_done(ScanOutcome(items=[read]))
        assert window.sidebar.place() == Place("sent"), "a scan's end stays put"
        assert window.apply_button.isEnabled() is True
        assert window.apply_button.text() == "Apply 1 Move"
        plans = build_move_plans(window._inbox_rows())
        assert [(plan.uid, plan.source_folder) for plan in plans] == [("3", "INBOX")]

    def test_a_scan_starts_from_the_inbox(self, opened, qtbot, monkeypatch):
        from PySide6.QtCore import QThread, Signal

        import gui
        from sidebar import INBOX, Place

        class Scan(QThread):
            progress = Signal(int, int, str)
            metrics = Signal(dict)
            log_message = Signal(str)
            failed = Signal(str, str)
            finished_ok = Signal(object)
            task_name = "scan"

            def __init__(self, **kwargs):
                super().__init__(kwargs.get("parent"))

            def run(self):
                pass

        window, _server = opened
        self._opened_at(qtbot, window, Place("sent"))
        monkeypatch.setattr(gui, "ScanWorker", Scan)
        window._begin_scan()
        assert window.sidebar.place() == INBOX
        assert not window.model.addressed
        qtbot.waitUntil(lambda: not window.running_workers(), timeout=5000)


class TestTheSidebarCanBeHidden:
    def test_the_menu_the_button_and_the_setting_agree(self, opened):
        window, _server = opened
        assert window.sidebar_action.text() == "Hide Sidebar"
        assert window.sidebar_action.shortcut().toString() == "Meta+Ctrl+S"
        window.sidebar_action.trigger()
        assert window.sidebar.isHidden()
        assert window.sidebar_action.text() == "Show Sidebar"
        assert window.sidebar_button.isChecked() is False
        assert window.settings.show_sidebar is False
        window.sidebar_button.click()
        assert not window.sidebar.isHidden()
        assert window.sidebar_action.text() == "Hide Sidebar"
        assert window.settings.show_sidebar is True

    def test_a_hidden_sidebar_stays_hidden_next_time(self, opened, tmp_path):
        from config import Settings
        from gui import MainWindow

        window, _server = opened
        window.sidebar_action.trigger()
        again = MainWindow(Settings.load(), window.store)
        try:
            assert again.sidebar.isHidden()
            assert again.sidebar_action.text() == "Show Sidebar"
        finally:
            again.close()
            again.deleteLater()


class TestWhatTheEmptyTableSays:
    def test_reading_then_empty(self, opened, qtbot):
        window, server = opened
        server.messages.clear()
        window._open_mailboxes()
        assert "Reading Inbox" in window.empty_label.text()
        _settle(qtbot, window)
        assert "Your inbox is empty" in window.empty_label.text()
        assert "month" in window.empty_label.text()


class TestStopAll:
    def test_it_ends_the_readers_too(self, opened, qtbot):
        window, _server = opened
        window._open_mailboxes()
        readers = list(window._mailbox_workers)
        assert readers
        window.stop_all()
        assert all(reader.cancelled for reader in readers)
        _settle(qtbot, window)


class TestTheSettings:
    def test_the_period_is_one_offered(self, settings):
        from config import INBOX_PERIODS, Settings

        for asked, kept in ((30, 30), (31, 30), (100, 90), (0, 7), (5000, 365)):
            made = Settings(icloud_email="you@icloud.example", inbox_days=asked)
            assert made.normalized().inbox_days == kept, asked
        assert dict(INBOX_PERIODS)[30] == "month"

    def test_the_defaults(self, settings):
        assert settings.open_with_inbox is True
        assert settings.inbox_days == 30
        assert settings.scan_on_open is False
        assert settings.show_sidebar is True

    def test_the_sidebars_width_is_not_exported(self, settings):
        import json

        settings.sidebar_width = 240
        exported = json.loads("\n".join(
            line for line in settings.export_text().splitlines()
            if not line.startswith("#")))
        assert "sidebar_width" not in exported
        assert exported["show_sidebar"] is True
        assert exported["open_with_inbox"] is True and exported["inbox_days"] == 30


class TestTheSettingsPage:
    def test_the_controls_show_and_keep_the_choices(self, qapp, settings):
        from config import InMemoryCredentialStore
        from settings_dialog import SettingsDialog

        dialog = SettingsDialog(settings, InMemoryCredentialStore())
        try:
            assert dialog.open_inbox_check.isChecked() is True
            assert dialog.inbox_period_combo.currentData() == 30
            assert dialog.inbox_period_combo.currentText() == "month"
            assert dialog.scan_on_open_check.isChecked() is False
            assert "Auto scan" in dialog.scan_on_open_check.text()
            dialog.open_inbox_check.setChecked(False)
            assert dialog.inbox_period_combo.isEnabled() is False
            dialog.open_inbox_check.setChecked(True)
            dialog.inbox_period_combo.setCurrentIndex(
                dialog.inbox_period_combo.findData(90))
            dialog.scan_on_open_check.setChecked(True)
            kept = dialog.collect()
            assert (kept.open_with_inbox, kept.inbox_days, kept.scan_on_open) == (
                True, 90, True)
        finally:
            dialog.deleteLater()


class TestAfterSettingsChange:
    def test_a_password_stored_lists_the_mailbox(self, opened, qtbot, monkeypatch):
        window, _server = opened
        password = window.store.get_mailbox_password("you@icloud.example")
        window.store.set_mailbox_password("you@icloud.example", "")
        window._open_mailboxes()
        _settle(qtbot, window)
        assert window.model.items == []
        before = window._mailbox_shape()
        window.store.set_mailbox_password("you@icloud.example", password)
        ran = []
        monkeypatch.setattr(window, "_begin_scan", lambda *a, **k: ran.append(1))
        window.settings.scan_on_open = True
        window._mailboxes_changed(before)
        _settle(qtbot, window)
        assert len(window.model.items) == 2
        assert ran == [], "the scan at launch is not run again"

    def test_nothing_changed_reads_nothing(self, opened, qtbot):
        window, server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        fetches = [name for name, _a in server.commands if name == "UID FETCH"]
        window._mailboxes_changed(window._mailbox_shape())
        _settle(qtbot, window)
        assert [name for name, _a in server.commands if name == "UID FETCH"] == fetches

    def test_a_listing_replaced_since_is_not_heard(self, opened, item_factory):
        from PySide6.QtCore import QThread

        from sidebar import INBOX

        window, _server = opened
        stale = QThread()
        window._listings[INBOX] = [QThread()]
        window._on_listed(INBOX, [item_factory(analysed=False)], stale)
        assert window._listed == {} and window.model.items == []
        window._listing_ended(INBOX, stale)
        assert INBOX in window._listings, "the current listing is still tracked"
        window._listings.clear()


class TestAnUnreachableServerAtLaunch:
    def test_it_is_said_in_the_log_and_nothing_is_asked(
            self, qapp, qtbot, tmp_path, monkeypatch, dialog_calls):
        """No fake here: the suite refuses every real connection, as a
        server out of reach would."""
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_mailbox_password("you@icloud.example", "app-specific")
        window = MainWindow(Settings(icloud_email="you@icloud.example",
                                     provider="rules").normalized(), store)
        try:
            window._open_mailboxes()
            _settle(qtbot, window)
            assert dialog_calls == []
            assert window.model.items == []
            log = window.log_view.toPlainText()
            assert "could not read Inbox" in log and "no network in tests" in log
            assert "could not list its mailboxes" in log
        finally:
            window.shutdown()
            window.close()
            window.deleteLater()


class TestTheDemoNeverReadsAMailbox:
    def test_opening_lists_nothing(self, qapp, qtbot, tmp_path, monkeypatch,
                                   fake_imap_factory):
        """Demo mode shows its samples and never touches the network, even
        with a password stored and a server to reach."""
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        server = fake_imap_factory(folders=ICLOUD_FOLDERS)
        _Wired(monkeypatch, server)
        store = InMemoryCredentialStore()
        store.set_mailbox_password("you@icloud.example", "app-specific")
        window = MainWindow(Settings(icloud_email="you@icloud.example",
                                     provider="rules").normalized(), store, demo=True)
        try:
            window._open_mailboxes()
            assert window._mailbox_workers == [] and window._listings == {}
            assert server.commands == []
        finally:
            window.shutdown()
            window.close()
            window.deleteLater()


class TestAutoScanIsALaunchs:
    def test_a_window_opened_later_does_not_scan_again(self, opened, qtbot,
                                                        monkeypatch):
        """Started in the menu bar, Auto scan runs with no window; the
        window opened afterwards lists the inbox and scans nothing more."""
        window, _server = opened
        ran = []
        monkeypatch.setattr(window, "_begin_scan", lambda *a, **k: ran.append(1))
        window.settings.scan_on_open = True
        window._scan_on_open()
        assert ran == [1]
        window._open_mailboxes()
        _settle(qtbot, window)
        assert len(window.model.items) == 2
        assert ran == [1]

    def test_main_runs_it_when_starting_in_the_menu_bar(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "main.py").read_text()
        branch = source.index("if settings.start_in_menu_bar and window.menu_bar.visible():")
        shown = source.index("window.show()", branch)
        started = source[branch:shown]
        assert "QTimer.singleShot(0, window, window._scan_on_open)" in started
        assert "if settings.scan_on_open and not args.demo:" in started


class TestLookingAgain:
    """A mailbox is looked at again when chosen, as in Mail: only what is
    new is read, and what has gone is taken out."""

    @staticmethod
    def _fetched(server):
        return [args[0] for name, args in server.commands if name == "UID FETCH"]

    def _open_at_sent(self, qtbot, window):
        from sidebar import Place

        TestOtherMailboxes._opened_at(None, qtbot, window, Place("sent"))
        return Place("sent")

    def test_choosing_sent_again_reads_only_what_is_new(self, opened, qtbot,
                                                         mime_factory):
        from sidebar import INBOX

        window, server = opened
        sent = self._open_at_sent(qtbot, window)
        window.sidebar.chosen.emit(INBOX)
        _settle(qtbot, window)
        server.messages["4"] = mime_factory(subject="A note sent just now")
        server.internaldates["4"] = _days_ago(0)
        before = len(self._fetched(server))
        window.sidebar.chosen.emit(sent)
        _settle(qtbot, window)
        assert "A note sent just now" in [i.email.subject for i in window.model.items]
        assert self._fetched(server)[before:] == ["4"], "only the new one is read"
        assert len({id(item) for item in window.model.items}) == len(window.model.items)

    def test_mail_gone_from_a_mailbox_is_taken_out_and_the_rest_stay(
            self, opened, qtbot):
        window, server = opened
        self._open_at_sent(qtbot, window)
        keep = next(row for row, item in enumerate(window.model.items)
                    if item.email.subject == "Your interview on Thursday")
        window._select_rows([keep])
        del server.messages["2"]          # the weekly digest
        window.new_mail_action.trigger()
        _settle(qtbot, window)
        subjects = [item.email.subject for item in window.model.items]
        assert "Weekly digest" not in subjects
        assert window._selected_rows() and window.model.items[
            window._selected_rows()[0]].email.subject == "Your interview on Thursday"

    def test_get_new_mail_finds_a_new_folder(self, opened, qtbot):
        from sidebar import Place

        window, server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        server.folders.append("Projects")
        assert window.new_mail_action.shortcut().toString() == "Ctrl+Shift+N"
        window.new_mail_action.trigger()
        _settle(qtbot, window)
        account = window.settings.accounts[0]
        assert Place("folder", account.id, "Projects") in window.sidebar.places()

    def test_the_inbox_drops_what_has_gone_but_keeps_what_a_scan_read(
            self, opened, qtbot, item_factory):
        from workers import ScanOutcome

        window, server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        account = window.settings.accounts[0]
        read = item_factory(email_kwargs={
            "uid": "3", "account_id": account.id, "source_folder": "INBOX",
            "subject": "Your interview on Thursday"})
        window._on_scan_done(ScanOutcome(items=[read]))
        del server.messages["2"]
        del server.messages["3"]
        window.new_mail_action.trigger()
        _settle(qtbot, window)
        assert window.model.items == [read], "the scan's row stays as it was read"

    def test_nothing_is_looked_at_before_the_mailboxes_open(self, opened):
        from sidebar import INBOX

        window, server = opened
        window._refresh(INBOX)
        window._get_new_mail()
        assert window._listings == {} and server.commands == []

    def test_nor_ever_in_the_demo(self, opened, qtbot):
        from sidebar import INBOX

        window, server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        server.commands.clear()
        window.demo = True
        window._refresh(INBOX)
        window._get_new_mail()
        assert window._listings == {} and server.commands == []


class TestANewPeriod:
    def test_the_inbox_is_listed_afresh_for_it(self, opened, qtbot):
        window, _server = opened
        window._open_mailboxes()
        _settle(qtbot, window)
        assert len(window.model.items) == 2
        before = window._mailbox_shape()
        window.settings.inbox_days = 7
        window._mailboxes_changed(before)
        _settle(qtbot, window)
        assert [i.email.subject for i in window.model.items] == [
            "Your interview on Thursday"]
        before = window._mailbox_shape()
        window.settings.inbox_days = 30
        window._mailboxes_changed(before)
        _settle(qtbot, window)
        assert len(window.model.items) == 2
