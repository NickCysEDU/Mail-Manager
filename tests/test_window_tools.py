"""The window's newer controls: views, columns, help, undo and drafting."""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import autoreply  # noqa: E402
import helpmode  # noqa: E402
from accounts import Account  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from gui import MainWindow, SettingsDialog, TriageTableModel  # noqa: E402
from imap_engine import MovePlan, MoveReport  # noqa: E402


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    settings = Settings(icloud_email="you@icloud.example").normalized()
    subject = MainWindow(settings, InMemoryCredentialStore())
    yield subject
    subject.close()


@pytest.fixture
def two_mailbox_window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    settings = Settings(icloud_email="you@icloud.example")
    settings.mailboxes = [
        Account(address="you@icloud.example", host="imap.mail.me.com", label="Personal"),
        Account(address="work@elsewhere.example", host="imap.work.example", label="Work"),
    ]
    subject = MainWindow(settings.normalized(), InMemoryCredentialStore())
    subject.show()
    yield subject
    subject.close()


class TestColumnsCanBeTurnedOff:
    def test_every_column_but_the_tick_box_is_offered(self, window):
        offered = [a.text() for a in window.columns_menu.actions() if a.text()]
        assert "Sender" in offered and "Reasoning" in offered
        assert "" not in offered              # the tick box is not hideable

    def test_hiding_a_column_is_remembered(self, window):
        window._set_column_visible(TriageTableModel.COL_REASONING, False)
        assert window.table.isColumnHidden(TriageTableModel.COL_REASONING)
        assert TriageTableModel.COL_REASONING in window.settings.hidden_columns

    def test_the_auto_hidden_mailbox_column_is_not_a_preference(self, window):
        """One mailbox hides it; that must not be filed away as a choice."""
        assert window.table.isColumnHidden(TriageTableModel.COL_ACCOUNT)
        assert TriageTableModel.COL_ACCOUNT not in window.settings.hidden_columns

    def test_show_all_brings_everything_back(self, window):
        for column in (2, 4, 8):
            window._set_column_visible(column, False)
        window._show_all_columns()
        assert window.settings.hidden_columns == []
        assert not window.table.isColumnHidden(4)

    def test_a_hidden_column_survives_a_relaunch(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        settings = Settings(icloud_email="you@icloud.example",
                            hidden_columns=[TriageTableModel.COL_REASONING]).normalized()
        subject = MainWindow(settings, InMemoryCredentialStore())
        try:
            assert subject.table.isColumnHidden(TriageTableModel.COL_REASONING)
        finally:
            subject.close()


class TestReadingOneMailboxAtATime:
    def test_the_view_menu_stays_away_with_one_mailbox(self, window):
        assert window.view_button.isVisible() is False

    def test_filtering_the_view_does_not_change_what_gets_scanned(
            self, two_mailbox_window):
        """Two different questions, so two different controls."""
        subject = two_mailbox_window
        before = [a.id for a in subject.settings.scan_accounts]
        subject._set_view_accounts(["anything"])
        assert [a.id for a in subject.settings.scan_accounts] == before


class TestHelpMode:
    def test_it_starts_off(self, window):
        assert window.help_button.isChecked() is False

    def test_turning_it_on_arms_the_filter_and_is_remembered(self, window):
        window.help_button.setChecked(True)
        assert window.settings.help_mode is True
        assert QApplication.instance()._help_filter.enabled is True
        window.help_button.setChecked(False)
        assert QApplication.instance()._help_filter.enabled is False

    def test_the_button_says_which_state_it_is_in(self, window):
        off = window.help_button.toolTip()
        window.help_button.setChecked(True)
        assert window.help_button.toolTip() != off
        window.help_button.setChecked(False)


class TestUndoingTheLastFiling:
    def test_undo_is_off_until_something_has_been_filed(self, window):
        assert window.undo_action.isEnabled() is False

    def test_a_completed_apply_arms_undo_with_the_right_journey(self, window):
        window._load_demo_data()
        first = window.model.items[0]
        report = MoveReport(moved={first.email.uid: "Job Search/Interview"})
        window._on_apply_done(report)
        assert window.undo_action.isEnabled() is True
        plan = window._last_apply[0]
        # It goes back to the inbox, starting from where it was filed.
        assert plan.source_folder == "Job Search/Interview"
        assert plan.target_folder == window.settings.source_mailbox

    def test_undo_disarms_once_it_has_run(self, window):
        window._load_demo_data()
        window._on_apply_done(MoveReport(moved={window.model.items[0].email.uid: "X"}))
        window._on_undo_done(MoveReport(moved={"1": "INBOX"}))
        assert window.undo_action.isEnabled() is False
        assert window._last_apply == []


class TestApplyGroupsByWhereMessagesActuallyAre:
    def test_moves_are_grouped_by_account_and_source(self, qapp, tmp_path, monkeypatch):
        from workers import ApplyWorker
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        settings = Settings(icloud_email="a@icloud.example")
        settings.mailboxes = [
            Account(address="a@icloud.example", host="h1", label="One"),
            Account(address="b@elsewhere.example", host="h2", label="Two"),
        ]
        settings = settings.normalized()
        first, second = settings.accounts
        worker = ApplyWorker(settings, "pw", [
            MovePlan(uid="1", target_folder="X", account_id=first.id),
            MovePlan(uid="2", target_folder="X", account_id=first.id),
            MovePlan(uid="3", target_folder="INBOX", account_id=first.id,
                     source_folder="Job Search/Offers"),
            MovePlan(uid="4", target_folder="Y", account_id=second.id),
        ])
        groups = worker._grouped()
        assert len(groups) == 3, "inbox and Offers are different starting points"
        sources = sorted(source for _account, source, _plans in groups)
        assert sources == ["INBOX", "INBOX", "Job Search/Offers"]


class TestTheAutoReplyTab:
    def test_the_tab_exists_and_starts_disarmed(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            titles = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
            assert "Auto Reply" in titles
            assert dialog.auto_reply_check.isChecked() is False
        finally:
            dialog.deleteLater()

    def test_editing_a_rule_survives_being_collected(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog.auto_reply_check.setChecked(True)
            dialog.rule_enabled.setChecked(True)
            dialog.rule_name_edit.setText("My rule")
            dialog.rule_confidence.setValue(0.97)
            collected = dialog.collect()
            rule = collected.rules[0]
            assert collected.auto_reply is True
            assert rule.enabled and rule.name == "My rule"
            assert rule.min_confidence == pytest.approx(0.97)
            assert collected.replies_armed is True
        finally:
            dialog.deleteLater()

    def test_a_settings_object_with_no_rules_still_offers_the_defaults(self):
        assert len(Settings().rules) == len(autoreply.default_rules())


class TestMailboxSetup:
    def test_a_second_provider_can_be_added(self, window):
        store = InMemoryCredentialStore()
        dialog = SettingsDialog(window.settings, store, window)
        try:
            dialog._add_account()
            dialog.preset_combo.setCurrentIndex(dialog.preset_combo.findData("gmail"))
            dialog.email_edit.setText("me@gmail.com")
            dialog._address_entered()
            dialog.password_edit.setText("abcd efgh ijkl mnop")
            collected = dialog.collect()
            dialog.persist_credentials(collected)
            assert [a.host for a in collected.accounts][-1] == "imap.gmail.com"
            assert store.get_mailbox_password("me@gmail.com") == "abcd efgh ijkl mnop"
        finally:
            dialog.deleteLater()

    def test_a_mailbox_with_no_address_is_dropped_rather_than_saved(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._add_account()          # left completely blank
            assert len(dialog.collect().accounts) == 1
        finally:
            dialog.deleteLater()


class TestTheMailboxColumnIsActuallyVisible:
    """Every one of these was a real bug: the column existed and could not be seen."""

    def test_a_saved_header_cannot_hide_it_forever(self, qapp, tmp_path, monkeypatch):
        """The state saved when there was one mailbox said to hide the column."""
        from PySide6.QtCore import QByteArray
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        single = Settings(icloud_email="you@icloud.example").normalized()
        first = MainWindow(single, InMemoryCredentialStore())
        first.show()
        assert first.table.isColumnHidden(TriageTableModel.COL_ACCOUNT)
        saved = bytes(
            first.table.horizontalHeader().saveState().toBase64()).decode()
        first.close()

        # Now a second mailbox arrives, carrying that saved header with it.
        pair = Settings(icloud_email="you@icloud.example", table_state=saved)
        pair.mailboxes = [
            Account(address="you@icloud.example", host="imap.mail.me.com"),
            Account(address="work@elsewhere.example", host="imap.work.example"),
        ]
        second = MainWindow(pair.normalized(), InMemoryCredentialStore())
        second.show()
        try:
            assert not second.table.isColumnHidden(TriageTableModel.COL_ACCOUNT)
            assert second.table.columnWidth(TriageTableModel.COL_ACCOUNT) > 0
        finally:
            second.close()

    def test_it_sits_near_the_front_rather_than_off_the_edge(self, two_mailbox_window):
        """It is the last column in the model, which is off-screen on a wide table."""
        header = two_mailbox_window.table.horizontalHeader()
        assert header.visualIndex(TriageTableModel.COL_ACCOUNT) == 1

    def test_the_address_is_what_identifies_a_mailbox(self):
        from models import EmailMessage
        assert EmailMessage(uid="1", account_label="nick",
                            account_address="sam@icloud.com").mailbox_display \
            == "sam@icloud.com"
        # A name that says something the address does not is worth keeping,
        # but the domain still has to be there.
        assert "@gmail.com" in EmailMessage(
            uid="1", account_label="Work",
            account_address="sam@gmail.com").mailbox_display


class TestMenusDoNotLoop:
    def test_ticking_a_mailbox_does_not_rebuild_forever(self, two_mailbox_window):
        """setChecked emits toggled, and toggled rebuilds the menu.

        Connecting before ticking made opening the menu an infinite loop, which
        looked from outside like the app freezing.
        """
        subject = two_mailbox_window
        subject._load_demo_data()
        subject._rebuild_view_menu()
        linked = subject._linked_mailboxes()
        assert linked
        subject._set_view_accounts([linked[0][0]])
        assert subject._view_accounts == [linked[0][0]]

    def test_none_is_different_from_all(self, two_mailbox_window):
        """Unticking the last mailbox has to mean an empty table, not a full one."""
        subject = two_mailbox_window
        subject._load_demo_data()
        subject._set_view_accounts([], empty=True)
        assert subject.proxy.rowCount() == 0
        subject._set_view_accounts([a for a, _l, _n in subject._linked_mailboxes()])
        assert subject.proxy.rowCount() == subject.model.rowCount()


class TestAppearanceIsActuallyApplied:
    def test_row_height_follows_the_setting(self, window, monkeypatch):
        """It was collected and saved, and then never applied to anything."""
        from PySide6.QtWidgets import QDialog
        import gui as gui_module

        window._load_demo_data()
        before = window.table.verticalHeader().defaultSectionSize()
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        dialog.rows_spin.setValue(6)
        monkeypatch.setattr(SettingsDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(gui_module, "SettingsDialog", lambda *a, **k: dialog)
        window.open_settings()
        after = window.table.verticalHeader().defaultSectionSize()
        assert after > before
        assert window.table.rowHeight(0) == after, "rows already on screen too"


class TestSettingsTransfer:
    def test_a_round_trip_keeps_everything_that_matters(self):
        settings = Settings(icloud_email="me@icloud.com", row_lines=5,
                            contrast="high", sort_profile="everyday")
        settings.mailboxes = [Account.for_address("me@icloud.com"),
                              Account.for_address("me@gmail.com")]
        settings = settings.normalized()
        back = Settings.import_text(settings.export_text())
        assert [a.address for a in back.accounts] == \
            [a.address for a in settings.accounts]
        assert back.row_lines == 5 and back.contrast == "high"
        assert back.sort_profile == "everyday"

    def test_no_secret_can_be_in_an_export(self):
        settings = Settings(icloud_email="me@icloud.com").normalized()
        exported = settings.export_text()
        # The object has no secret fields at all; this guards that staying true.
        for field in ("password", "api_key", "secret", "token"):
            assert f'"{field}"' not in exported

    def test_the_window_layout_is_not_exported(self):
        settings = Settings(icloud_email="me@icloud.com",
                            window_geometry="AAA", table_state="BBB").normalized()
        exported = settings.export_text()
        assert "AAA" not in exported and "BBB" not in exported

    @pytest.mark.parametrize("text, expected", [
        ("", "empty"),
        ("# only a comment\n", "empty"),
        ("not json at all", "not a settings file"),
        ("[1, 2, 3]", "not a set of settings"),
        ('{"unrelated": true}', "none of the settings"),
    ])
    def test_a_bad_file_says_what_is_wrong_with_it(self, text, expected):
        with pytest.raises(ValueError, match=expected):
            Settings.import_text(text)

    def test_comments_are_ignored_rather_than_choked_on(self):
        settings = Settings(icloud_email="me@icloud.com").normalized()
        text = settings.export_text()
        assert text.lstrip().startswith("#")
        assert Settings.import_text(text).icloud_email == "me@icloud.com"


class TestHintTextIsNeverClipped:
    @pytest.mark.parametrize("width", [900, 600, 420, 300, 220, 160])
    def test_the_filter_hint_shrinks_to_fit(self, qapp, width):
        from gui import AdaptiveLineEdit
        field = AdaptiveLineEdit(
            "Filter by sender, subject, summary or reasoning…",
            "Filter by sender, subject or summary…",
            "Filter messages…", "Filter…")
        field.show()
        field.resize(width, 28)
        room = width - 34
        assert field.fontMetrics().horizontalAdvance(field.placeholderText()) <= room

    def test_the_full_wording_is_always_reachable(self, qapp):
        from gui import AdaptiveLineEdit
        field = AdaptiveLineEdit("The long one", "Short")
        assert field.toolTip() == "The long one"
