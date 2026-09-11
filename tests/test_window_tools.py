"""The window's newer controls: views, columns, help, undo and drafting."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox  # noqa: E402

import accounts as accounts_module  # noqa: E402
import autoreply  # noqa: E402
import helpmode  # noqa: E402
import providers  # noqa: E402
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


def filed(window, folder="Job Search/Interview", count=1, first_uid=900):
    """Report that the first `count` rows were filed, with fresh UIDs."""
    items = window.model.items[:count]
    return MoveReport(
        moved={item.email.uid: folder for item in items},
        new_uids={item.email.uid: str(first_uid + n)
                  for n, item in enumerate(items)})


class TestUndoingTheLastFiling:
    def test_undo_is_off_until_something_has_been_filed(self, window):
        assert window.undo_action.isEnabled() is False

    def test_a_completed_apply_arms_undo_with_the_right_journey(self, window):
        window._load_demo_data()
        window._on_apply_done(filed(window))
        assert window.undo_action.isEnabled() is True
        plan = window._undo_stack[-1].plans[0]
        # It goes back to the inbox, starting from where it was filed.
        assert plan.source_folder == "Job Search/Interview"
        assert plan.target_folder == window.settings.source_mailbox

    def test_it_uses_the_uid_the_copy_assigned(self, window):
        """A COPY gives the message a new UID; the old one names other mail."""
        window._load_demo_data()
        original = window.model.items[0].email.uid
        window._on_apply_done(filed(window, first_uid=900))
        plan = window._undo_stack[-1].plans[0]
        assert plan.uid == "900"
        assert plan.uid != original

    def test_a_message_the_server_gave_no_receipt_for_is_left_alone(self, window):
        """Guessing at its UID would move whatever else holds that number."""
        window._load_demo_data()
        uid = window.model.items[0].email.uid
        window._on_apply_done(MoveReport(moved={uid: "Somewhere"}))
        assert window._undo_stack == []
        assert window.undo_action.isEnabled() is False

    def test_undo_disarms_once_it_has_run(self, window):
        window._load_demo_data()
        window._on_apply_done(filed(window))
        window.undo_action.trigger  # armed
        window._undoing = window._undo_stack[-1]
        window._on_undo_done(MoveReport(moved={"900": "INBOX"}))
        assert window.undo_action.isEnabled() is False
        assert window._undo_stack == []


class TestUndoingMoreThanOnce:
    """Filing is done in passes, so undo has to be too."""

    def test_each_filing_is_its_own_step(self, window):
        window._load_demo_data()
        window._on_apply_done(filed(window, "First", count=1, first_uid=900))
        window._on_apply_done(filed(window, "Second", count=2, first_uid=910))
        assert len(window._undo_stack) == 2

    def test_undo_takes_the_newest_first(self, window):
        window._load_demo_data()
        window._on_apply_done(filed(window, "First", count=1, first_uid=900))
        window._on_apply_done(filed(window, "Second", count=2, first_uid=910))

        newest = window._undo_stack[-1]
        assert [p.source_folder for p in newest.plans] == ["Second"] * 2

        window._undoing = newest
        window._on_undo_done(MoveReport(moved={}))
        assert len(window._undo_stack) == 1
        assert window._undo_stack[-1].plans[0].source_folder == "First"
        assert window.undo_action.isEnabled() is True

    def test_the_menu_says_how_many_are_left(self, window):
        window._load_demo_data()
        window._on_apply_done(filed(window, "First", count=1, first_uid=900))
        window._on_apply_done(filed(window, "Second", count=2, first_uid=910))
        assert "2 Messages" in window.undo_action.text()
        assert "1 earlier filing" in window.undo_action.toolTip()

    def test_the_stack_has_a_floor(self, window):
        import gui
        window._load_demo_data()
        for n in range(gui.UNDO_DEPTH + 5):
            window._on_apply_done(filed(window, f"F{n}", first_uid=900 + n))
        assert len(window._undo_stack) == gui.UNDO_DEPTH
        # The oldest went, not the newest.
        assert window._undo_stack[-1].plans[0].source_folder == \
            f"F{gui.UNDO_DEPTH + 4}"


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
            # Named for everything rules do, not just the drafting half.
            assert "Rules" in titles
            assert dialog.auto_reply_check.isChecked() is False
        finally:
            dialog.deleteLater()

    def test_editing_a_rule_survives_being_collected(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog.auto_reply_check.setChecked(True)
            dialog.rule_name_edit.setText("My rule")
            condition = dialog._condition_rows[-1]
            condition.field_combo.setCurrentIndex(
                condition.field_combo.findData("confidence"))
            condition.operator_combo.setCurrentIndex(
                condition.operator_combo.findData("at_least"))
            condition._value_widget.setText("0.97")
            dialog.rule_enabled.setChecked(True)
            collected = dialog.collect()
            rule = collected.rules[0]
            assert collected.auto_reply is True
            assert rule.enabled and rule.name == "My rule"
            assert rule.conditions[-1].field == "confidence"
            assert rule.conditions[-1].value == "0.97"
            assert collected.replies_armed is True
        finally:
            dialog.deleteLater()

    def test_a_rule_can_be_built_out_of_nothing(self, window):
        """Add a rule, give it a condition and an action, and it is ready."""
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            before = len(dialog._rules)
            dialog._add_rule()
            dialog.rule_name_edit.setText("Flag anything from Dana")
            condition = dialog._condition_rows[0]
            condition.field_combo.setCurrentIndex(
                condition.field_combo.findData("sender"))
            condition._value_widget.setText("dana@northwind.example")
            action = dialog._action_rows[0]
            action.kind_combo.setCurrentIndex(action.kind_combo.findData("flag"))
            dialog.rule_enabled.setChecked(True)
            dialog._capture_rule()
            built = dialog._rules[-1]
            assert len(dialog._rules) == before + 1
            assert built.ready and built.enabled
            assert built.describe() == (
                "Sender contains \u201cdana@northwind.example\u201d \u2192 flag it")
        finally:
            dialog.deleteLater()

    def test_a_half_written_rule_says_what_is_missing(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._add_rule()
            dialog._capture_rule()
            dialog._describe_rule()
            assert "Not ready" in dialog.rule_summary.text()
            assert not dialog._rules[-1].ready
        finally:
            dialog.deleteLater()

    def test_rules_can_be_reordered_duplicated_and_removed(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            names = [r.name for r in dialog._rules]
            dialog.rule_list.setCurrentRow(1)
            dialog._move_rule(-1)
            assert [r.name for r in dialog._rules][:2] == [names[1], names[0]]
            count = len(dialog._rules)
            dialog._duplicate_rule()
            assert len(dialog._rules) == count + 1
            assert dialog._rules[1].name.endswith("(copy)")
            assert dialog._rules[1].enabled is False, "a copy starts switched off"
            dialog._remove_rule()
            assert len(dialog._rules) == count
        finally:
            dialog.deleteLater()

    def test_the_last_rule_cannot_be_removed(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            while len(dialog._rules) > 1:
                dialog._remove_rule()
            dialog._remove_rule()
            assert len(dialog._rules) == 1
        finally:
            dialog.deleteLater()

    def test_trying_a_rule_with_nothing_scanned_says_so(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._try_rule()
            assert "Run a scan" in dialog.try_rule_result.text()
        finally:
            dialog.deleteLater()

    def test_trying_a_rule_reports_what_it_would_do(self, window):
        from models import (Category, Classification, EmailMessage, FolderPlan,
                            OtherCategory, TriageItem)
        item = TriageItem(
            email=EmailMessage(uid="1", subject="Interview invitation",
                               sender_email="dana@northwind.example"),
            classification=Classification(
                summary="s", reasoning="r", is_job_related=True,
                category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.99),
            folders=FolderPlan())
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window,
                                sample_items=[item])
        try:
            dialog._add_rule()
            condition = dialog._condition_rows[0]
            condition.field_combo.setCurrentIndex(
                condition.field_combo.findData("subject"))
            condition._value_widget.setText("interview")
            action = dialog._action_rows[0]
            action.kind_combo.setCurrentIndex(action.kind_combo.findData("tick"))
            dialog.rule_enabled.setChecked(True)
            dialog._try_rule()
            assert "1 of 1 matched" in dialog.try_rule_result.text()
            assert "tick it" in dialog.try_rule_result.text()
        finally:
            dialog.deleteLater()

    def test_the_ticks_in_the_list_switch_rules_on(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            entry = dialog.rule_list.item(2)
            entry.setCheckState(Qt.CheckState.Checked)
            assert dialog._rules[2].enabled is True
            entry.setCheckState(Qt.CheckState.Unchecked)
            assert dialog._rules[2].enabled is False
        finally:
            dialog.deleteLater()

    def test_switching_rules_keeps_what_was_typed(self, window):
        """The classic way an editor loses work: click away from it."""
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog.rule_name_edit.setText("Renamed while editing")
            dialog.rule_list.setCurrentRow(2)
            dialog.rule_list.setCurrentRow(0)
            assert dialog._rules[0].name == "Renamed while editing"
            assert dialog.rule_name_edit.text() == "Renamed while editing"
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
        assert EmailMessage(uid="1", account_label="me",
                            account_address="me@icloud.com").mailbox_display \
            == "me@icloud.com"
        # A name that says something the address does not is worth keeping,
        # but the domain still has to be there.
        assert "@gmail.com" in EmailMessage(
            uid="1", account_label="Work",
            account_address="other@gmail.com").mailbox_display


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


class TestTheAccountEditorCannotCorruptAMailbox:
    """Each of these describes damage the previous editor actually did."""

    @pytest.fixture
    def dialog(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_mailbox_password("me@icloud.com", "icloud-app-password")
        settings = Settings(icloud_email="me@icloud.com").normalized()
        window = MainWindow(settings, store)
        subject = SettingsDialog(settings, store, window)
        yield subject, store
        window.close()

    def test_changing_provider_cannot_leave_a_mismatched_address(self, dialog):
        """An iCloud address on Gmail's server connects to nothing, silently."""
        subject, _store = dialog
        subject.preset_combo.setCurrentIndex(subject.preset_combo.findData("gmail"))
        collected = subject.collect()
        for account in collected.accounts:
            if account.address:
                guessed = accounts_module.host_for_address(account.address)
                if not guessed.is_custom:
                    assert account.host == guessed.host, \
                        f"{account.address} pointed at {account.host}"

    def test_a_password_cannot_be_saved_against_another_mailbox(self, dialog):
        """This is what destroyed a real iCloud app-specific password.

        The address on screen was iCloud's while the provider said Gmail, so a
        Gmail password was written to the iCloud Keychain entry and the
        original was gone.
        """
        subject, store = dialog
        subject.preset_combo.setCurrentIndex(subject.preset_combo.findData("gmail"))
        # Whatever is typed now belongs to the Gmail mailbox being set up.
        subject.email_edit.setText("me@gmail.com")
        subject._address_entered()
        subject.password_edit.setText("gmail-app-password")
        collected = subject.collect()
        subject.persist_credentials(collected)
        assert store.get_mailbox_password("me@gmail.com") == "gmail-app-password"
        assert store.get_mailbox_password("me@icloud.com") != "gmail-app-password"

    def test_a_new_mailbox_survives_being_saved(self, dialog):
        subject, store = dialog
        subject._add_account()
        subject.preset_combo.setCurrentIndex(subject.preset_combo.findData("gmail"))
        subject.email_edit.setText("work@gmail.com")
        subject._address_entered()
        subject.password_edit.setText("another-app-password")
        collected = subject.collect()
        subject.persist_credentials(collected)
        assert [a.address for a in collected.accounts] == \
            ["me@icloud.com", "work@gmail.com"]
        assert store.get_mailbox_password("work@gmail.com") == "another-app-password"

    def test_an_abandoned_blank_mailbox_is_dropped(self, dialog):
        subject, _store = dialog
        subject._add_account()
        assert len(subject.collect().accounts) == 1

    def test_the_list_says_what_each_mailbox_still_needs(self, dialog):
        subject, _store = dialog
        subject._add_account()
        assert "no address" in subject.account_list.item(1).text()
        subject.email_edit.setText("new@fastmail.com")
        subject._address_entered()
        assert "no password" in subject.account_list.item(1).text()
        subject.password_edit.setText("x")
        assert "ready" in subject.account_list.item(1).text()

    def test_unticking_a_mailbox_keeps_it_but_stops_scanning_it(self, dialog):
        from PySide6.QtCore import Qt as _Qt
        subject, _store = dialog
        subject.account_list.item(0).setCheckState(_Qt.CheckState.Unchecked)
        collected = subject.collect()
        assert len(collected.accounts) == 1
        assert collected.accounts[0].enabled is False
        assert collected.enabled_accounts == []


class TestFullScreenClose:
    def test_leaving_full_screen_before_hiding(self, qapp, tmp_path, monkeypatch):
        """Hiding a window that owns a full-screen space leaves a black desktop."""
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(
            Settings(icloud_email="you@icloud.example", menu_bar_icon=True).normalized(),
            InMemoryCredentialStore())
        window.show()
        window.showFullScreen()
        try:
            assert window.isFullScreen()
            window._put_away()
            assert not window.isFullScreen()
        finally:
            window._quitting = True
            window.close()

    def test_a_full_screen_shape_is_not_saved_for_next_time(self, qapp, tmp_path,
                                                            monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                            InMemoryCredentialStore())
        window.show()
        window.resize(1100, 700)
        window._save_layout()
        normal = window.settings.window_geometry
        window.showFullScreen()
        window._save_layout()
        try:
            assert window.settings.window_geometry == normal
        finally:
            window._quitting = True
            window.close()


class TestHelpToggleIsInBothPlaces:
    def test_settings_and_the_corner_agree(self, qapp, tmp_path, monkeypatch):
        from PySide6.QtWidgets import QDialog
        import gui as gui_module
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                            InMemoryCredentialStore())
        window.show()
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        dialog.help_check.setChecked(True)
        monkeypatch.setattr(SettingsDialog, "exec",
                            lambda self: QDialog.DialogCode.Accepted)
        monkeypatch.setattr(gui_module, "SettingsDialog", lambda *a, **k: dialog)
        try:
            window.open_settings()
            assert window.settings.help_mode is True
            assert window.help_button.isChecked() is True
        finally:
            window._quitting = True
            window.close()


class TestTheRuleListNeverCutsAName:
    """The rule names are longer than the column, at every font size."""

    def _elides(self, dialog) -> list:
        """Names whose wrapped text will not fit the row it was given."""
        from PySide6.QtCore import QRect
        listing = dialog.rule_list
        metrics = listing.fontMetrics()
        cramped = []
        for index in range(listing.count()):
            item = listing.item(index)
            room = max(40, listing.viewport().width() - 40)
            needed = metrics.boundingRect(
                QRect(0, 0, room, 0), int(Qt.TextFlag.TextWordWrap),
                item.text()).height()
            if item.sizeHint().height() < needed:
                cramped.append(item.text())
        return cramped

    @pytest.mark.parametrize("width", [640, 760, 1100])
    def test_every_name_gets_the_room_it_needs(self, window, width):
        dialog = SettingsDialog(Settings(), InMemoryCredentialStore(), window)
        try:
            dialog.tabs.setCurrentIndex(3)
            dialog.resize(width, 700)
            dialog.show()
            QApplication.processEvents()
            assert self._elides(dialog) == []
        finally:
            dialog.close()
            dialog.deleteLater()

    @pytest.mark.parametrize("point_size", [11, 15, 20])
    def test_a_bigger_font_re_measures_rather_than_cutting(self, window, point_size):
        """Turning on “increase readability” must not clip the names."""
        from PySide6.QtGui import QFont
        dialog = SettingsDialog(Settings(), InMemoryCredentialStore(), window)
        try:
            dialog.tabs.setCurrentIndex(3)
            dialog.show()
            QApplication.processEvents()
            bigger = QFont(dialog.rule_list.font())
            bigger.setPointSize(point_size)
            dialog.rule_list.setFont(bigger)      # fires the font-change hook
            QApplication.processEvents()
            assert self._elides(dialog) == []
        finally:
            dialog.close()
            dialog.deleteLater()

    def test_a_very_long_name_still_fits(self, window):
        dialog = SettingsDialog(Settings(), InMemoryCredentialStore(), window)
        try:
            dialog.tabs.setCurrentIndex(3)
            dialog.show()
            QApplication.processEvents()
            dialog.rule_name_edit.setText(
                "File anything from the recruiting team at a company with a "
                "very long name indeed into the folder for later")
            dialog._rule_renamed()
            QApplication.processEvents()
            assert self._elides(dialog) == []
            assert dialog.rule_list.item(0).sizeHint().height() > 60
        finally:
            dialog.close()
            dialog.deleteLater()


class TestRulesRunAfterAScan:
    """“Run these rules after a scan” has to actually do that."""

    def _outcome(self, items):
        from workers import ScanOutcome
        return ScanOutcome(items=list(items))

    def _item(self):
        from models import (Category, Classification, EmailMessage, FolderPlan,
                            OtherCategory, TriageItem)
        return TriageItem(
            email=EmailMessage(uid="1", subject="Interview invitation"),
            classification=Classification(
                summary="s", reasoning="r", is_job_related=True,
                category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.99),
            folders=FolderPlan())

    def _armed_settings(self):
        rule = autoreply.Rule(
            name="tick interviews", enabled=True,
            conditions=[autoreply.Condition("subject", "contains", "interview")],
            actions=[autoreply.Action("tick")])
        return Settings(auto_reply=True, reply_rules=[rule.to_dict()])

    def test_it_is_started_when_armed(self, window, monkeypatch):
        window.settings = self._armed_settings()
        assert window.settings.replies_armed
        called = []
        monkeypatch.setattr(window, "draft_replies",
                            lambda prompted=True: called.append(prompted))
        window._on_scan_done(self._outcome([self._item()]))
        QApplication.processEvents()
        assert called == [False], "the rules did not run, or asked to be prompted"

    def test_it_is_not_started_when_the_setting_is_off(self, window, monkeypatch):
        window.settings = Settings(auto_reply=False)
        called = []
        monkeypatch.setattr(window, "draft_replies",
                            lambda prompted=True: called.append(prompted))
        window._on_scan_done(self._outcome([self._item()]))
        QApplication.processEvents()
        assert called == []

    def test_it_is_not_started_when_no_rule_is_finished(self, window, monkeypatch):
        half = autoreply.Rule(name="half", enabled=True,
                              actions=[autoreply.Action("draft", "")])
        window.settings = Settings(auto_reply=True, reply_rules=[half.to_dict()])
        called = []
        monkeypatch.setattr(window, "draft_replies",
                            lambda prompted=True: called.append(prompted))
        window._on_scan_done(self._outcome([self._item()]))
        QApplication.processEvents()
        assert called == []

    def test_an_empty_scan_starts_nothing(self, window, monkeypatch):
        window.settings = self._armed_settings()
        called = []
        monkeypatch.setattr(window, "draft_replies",
                            lambda prompted=True: called.append(prompted))
        window._on_scan_done(self._outcome([]))
        QApplication.processEvents()
        assert called == []

    def test_an_unprompted_run_does_not_raise_a_box(self, window, monkeypatch):
        """Nobody asked, so the result goes to the log, not in front of them."""
        from workers import ReplyRun
        boxes = []
        monkeypatch.setattr(QMessageBox, "information",
                            lambda *a, **k: boxes.append(a))
        window.model.set_items([self._item()])
        window._replies_prompted = False
        run = ReplyRun()
        run.add(window.model.items[0], autoreply.Outcome(
            rule_names=["tick interviews"], tick=True))
        window._on_rules_run(run)
        assert boxes == []
        assert window.model.items[0].approved is True

    def test_a_prompted_run_does_raise_one(self, window, monkeypatch):
        from workers import ReplyRun
        boxes = []
        monkeypatch.setattr(QMessageBox, "information",
                            lambda *a, **k: boxes.append(a))
        window.model.set_items([self._item()])
        window._replies_prompted = True
        run = ReplyRun()
        run.add(window.model.items[0], autoreply.Outcome(
            rule_names=["tick interviews"], tick=True))
        window._on_rules_run(run)
        assert len(boxes) == 1

    def test_filing_and_leaving_reach_the_table(self, window):
        from workers import ReplyRun
        window.model.set_items([self._item()])
        item = window.model.items[0]
        run = ReplyRun()
        run.add(item, autoreply.Outcome(rule_names=["r"],
                                        file_into="Sorted Mail/Work", tick=True))
        window._replies_prompted = False
        window._on_rules_run(run)
        assert item.override_folder == "Sorted Mail/Work"
        assert item.approved is True

        leave = ReplyRun()
        leave.add(item, autoreply.Outcome(rule_names=["r"], leave=True))
        window._on_rules_run(leave)
        assert item.override_folder is None
        assert item.approved is False


class TestTheOnDevicePanelNeverFreezes:
    """Installing Ollama used to block the UI thread for the whole install."""

    def _drain(self, dialog, limit=12.0):
        """Pump the event loop until the worker finishes; report the worst stall.

        The queue is flushed first. Every test before this one leaves deferred
        deletions behind, and the first processEvents pays for all of them,
        measured at 0.7s under the full suite and 0ms on every iteration
        after. Timing that backlog says nothing about whether this worker
        blocks the window.
        """
        for _ in range(3):
            QApplication.processEvents()
        started = time.perf_counter()
        worst = 0.0
        while dialog._ollama_worker is not None and \
                time.perf_counter() - started < limit:
            tick = time.perf_counter()
            QApplication.processEvents()
            worst = max(worst, time.perf_counter() - tick)
            time.sleep(0.01)
        return worst

    def test_the_ui_thread_keeps_running_throughout(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step(
                "pull", ["/bin/sh", "-c", "echo start; sleep 1.2; echo done"])
            worst = self._drain(dialog)
            # A frame is 16ms. A tenth of a second is already a visible stutter
            # and this used to be the whole length of a Homebrew install.
            assert worst < 0.1, f"the UI thread stalled for {worst * 1000:.0f} ms"
        finally:
            dialog.deleteLater()

    def test_progress_is_reported_as_it_happens(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            seen = []
            script = (r'printf "pulling manifest\n"; '
                      r'for p in 10 50 90; do printf "pulling ab12cd34ef56... $p%% |#|\r"; '
                      r'sleep 0.15; done; printf "\nsuccess\n"')
            dialog._begin_ollama_step("pull", ["/bin/sh", "-c", script])
            dialog._ollama_worker.progress.connect(
                lambda done, total, msg: seen.append(done))
            self._drain(dialog)
            assert seen, "no progress was reported at all"
            assert max(seen) == 100
            assert seen == sorted(seen), f"the bar went backwards: {seen}"
        finally:
            dialog.deleteLater()

    def test_stopping_actually_stops_it(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step("install",
                                      ["/bin/sh", "-c", "echo start; sleep 60"])
            QApplication.processEvents()
            time.sleep(0.3)
            QApplication.processEvents()
            started = time.perf_counter()
            dialog._stop_ollama_step()
            self._drain(dialog)
            assert time.perf_counter() - started < 6.0
            assert "Stopped" in dialog.status.text()
            assert dialog.ollama_button.isEnabled()
            assert not dialog.ollama_stop.isVisible()
        finally:
            dialog.deleteLater()

    def test_a_failure_is_shown_rather_than_swallowed(self, window, monkeypatch):
        warned = []
        monkeypatch.setattr(QMessageBox, "warning",
                            lambda *a, **k: warned.append(a))
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step(
                "install", ["/bin/sh", "-c", "echo 'Error: no such cask' >&2; exit 1"])
            self._drain(dialog)
            assert "did not work" in dialog.status.text()
            assert warned, "a failed install said nothing"
        finally:
            dialog.deleteLater()

    def test_a_missing_binary_is_reported(self, window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step("pull", ["/nope/ollama", "pull", "x"])
            self._drain(dialog)
            assert "not on this Mac" in dialog.status.text()
        finally:
            dialog.deleteLater()

    def test_two_presses_do_not_start_two_installs(self, window):
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step("pull", ["/bin/sh", "-c", "sleep 1"])
            first = dialog._ollama_worker
            dialog._do_ollama_step()
            assert dialog._ollama_worker is first
            self._drain(dialog)
        finally:
            dialog.deleteLater()

    def test_closing_mid_install_asks_first(self, window, monkeypatch):
        """Killing Homebrew part-way through is not something to do silently."""
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(a), QMessageBox.StandardButton.Cancel)[1])
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog._begin_ollama_step("install", ["/bin/sh", "-c", "sleep 30"])
            QApplication.processEvents()
            dialog.done(0)
            assert asked, "it closed without asking"
            assert dialog._ollama_worker is not None, "it stopped anyway"
            monkeypatch.setattr(
                QMessageBox, "question",
                lambda *a, **k: QMessageBox.StandardButton.Discard)
            dialog.done(0)
            assert dialog._ollama_worker is None
        finally:
            dialog.deleteLater()

    def test_the_probe_does_not_block_the_panel(self, window):
        """An endpoint that drops packets costs the full timeout."""
        dialog = SettingsDialog(window.settings, InMemoryCredentialStore(), window)
        try:
            dialog.base_url_edit.setText("http://10.255.255.1:11434")
            started = time.perf_counter()
            dialog._refresh_ollama_panel(providers.provider_class("ollama"))
            assert time.perf_counter() - started < 0.5
        finally:
            if dialog._ollama_probe is not None:
                dialog._ollama_probe.stop(3000)
            dialog.deleteLater()


class TestTheVersionInTheCorner:
    def test_it_is_shown_bottom_right(self, window):
        import buildinfo

        assert window.version_label.text() == buildinfo.short()
        # Permanent widgets are the right-hand side of a status bar.
        assert window.statusBar().isAncestorOf(window.version_label)

    def test_the_tooltip_carries_enough_for_a_bug_report(self, window):
        import buildinfo

        tip = window.version_label.toolTip()
        assert buildinfo.full() in tip
        assert "Python" in tip

    def test_clicking_copies_it(self, window):
        from PySide6.QtTest import QTest
        import buildinfo

        QApplication.clipboard().setText("")
        QTest.mouseClick(window.version_label, Qt.MouseButton.LeftButton)
        assert QApplication.clipboard().text() == buildinfo.full()


class TestTheModelsDialog:
    """What is installed, with status, and a way to remove it."""

    def _dialog(self, window, models, error=""):
        from gui import ModelsDialog

        dialog = ModelsDialog(parent=window)
        # Answer the background probe by hand rather than needing a server.
        dialog._show_models((models, error))
        return dialog

    def _model(self, **kwargs):
        import ondevice

        base = dict(name="llama3.2:3b", size=2_019_393_189, parameters="3.2B",
                    quantisation="Q4_K_M", modified="2026-09-08", loaded=True)
        base.update(kwargs)
        return ondevice.Model(**base)

    def test_each_model_shows_its_size_and_status(self, window):
        dialog = self._dialog(window, [self._model()])
        try:
            assert dialog.listing.count() == 1
            text = dialog.listing.item(0).text()
            assert "llama3.2:3b" in text
            assert "2 GB" in text and "in memory, ready" in text
            assert "2 GB of disk in total" in dialog.status.text()
        finally:
            dialog.done(0)

    def test_an_unloaded_model_says_so(self, window):
        dialog = self._dialog(window, [self._model(loaded=False)])
        try:
            assert "on disk" in dialog.listing.item(0).text()
        finally:
            dialog.done(0)

    def test_nothing_installed_says_what_to_do(self, window):
        dialog = self._dialog(window, [])
        try:
            assert "No models yet" in dialog.status.text()
            assert not dialog.remove_button.isEnabled()
        finally:
            dialog.done(0)

    def test_a_server_that_is_not_answering_says_that_instead(self, window):
        dialog = self._dialog(window, [], error="connection refused")
        try:
            assert "not answering" in dialog.status.text()
            assert not dialog.remove_button.isEnabled()
        finally:
            dialog.done(0)

    def test_removing_asks_before_deleting_gigabytes(self, window, monkeypatch):
        asked = []
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *a, **k: (asked.append(True),
                             QMessageBox.StandardButton.Cancel)[1])
        started = []
        dialog = self._dialog(window, [self._model()])
        monkeypatch.setattr(dialog, "_run",
                            lambda *a, **k: started.append(a))
        try:
            dialog.listing.setCurrentRow(0)
            dialog._remove_selected()
            assert asked, "it deleted without asking"
            assert not started, "Cancel should have stopped it"
        finally:
            dialog.done(0)

    def test_confirming_runs_the_remove(self, window, monkeypatch):
        monkeypatch.setattr(QMessageBox, "question",
                            lambda *a, **k: QMessageBox.StandardButton.Yes)
        started = []
        dialog = self._dialog(window, [self._model()])
        monkeypatch.setattr(dialog, "_run", lambda step, command, saying:
                            started.append((step, command)))
        try:
            dialog.listing.setCurrentRow(0)
            dialog._remove_selected()
            assert started and started[0][0] == "remove"
            assert started[0][1][-1] == "llama3.2:3b"
        finally:
            dialog.done(0)


class TestTheModelFieldIsADropdownForLocalModels:
    """Typing a name is right for a hosted backend and wrong for a local one.

    A hosted backend releases models faster than a bundled list can follow,
    Gemini's pinned ids went stale and started answering 404. A local backend's
    valid names are exactly the models on this Mac, so a typo there is a scan
    that fails on every single message.
    """

    def test_a_local_backend_offers_a_plain_dropdown(self, window):
        dialog = SettingsDialog(Settings(provider="ollama"),
                                InMemoryCredentialStore(), window)
        try:
            assert dialog.model_combo.isEditable() is False
        finally:
            dialog.deleteLater()

    @pytest.mark.parametrize("provider", ["anthropic", "gemini"])
    def test_a_hosted_backend_still_takes_a_typed_name(self, window, provider):
        dialog = SettingsDialog(Settings(provider=provider),
                                InMemoryCredentialStore(), window)
        try:
            assert dialog.model_combo.isEditable() is True
        finally:
            dialog.deleteLater()

    def test_switching_to_a_local_backend_stops_it_being_editable(self, window):
        dialog = SettingsDialog(Settings(provider="anthropic"),
                                InMemoryCredentialStore(), window)
        try:
            assert dialog.model_combo.isEditable() is True
            index = dialog.provider_combo.findData("ollama")
            dialog.provider_combo.setCurrentIndex(index)
            assert dialog.model_combo.isEditable() is False
        finally:
            dialog.deleteLater()

    def test_an_installed_model_that_is_not_in_the_list_is_kept(self, window):
        """Choosing it once must not lose it the next time Settings opens."""
        dialog = SettingsDialog(Settings(provider="ollama", model="mistral:7b"),
                                InMemoryCredentialStore(), window)
        try:
            assert dialog._chosen_model() == "mistral:7b"
        finally:
            dialog.deleteLater()


class TestErrorTextCanBeCopied:
    def test_a_message_box_becomes_selectable_when_shown(self, window):
        import gui
        from PySide6.QtCore import Qt as QtNS

        app = QApplication.instance()
        gui.install_selectable_messages(app)
        box = QMessageBox(QMessageBox.Icon.Critical, "Connection failed",
                          "Could not reach Ollama at http://127.0.0.1:11434.",
                          parent=window)
        try:
            box.show()
            QApplication.processEvents()
            labels = [l for l in box.findChildren(QLabel) if l.text()]
            assert labels, "no text in the box"
            assert all(l.textInteractionFlags()
                       & QtNS.TextInteractionFlag.TextSelectableByMouse
                       for l in labels), "the error text cannot be selected"
        finally:
            box.close()
            box.deleteLater()
            # An application-wide filter must not outlive the test that
            # wanted it: it runs on every event of every test after this one.
            gui.remove_selectable_messages(app)
