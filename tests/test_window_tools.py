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
