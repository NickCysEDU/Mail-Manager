"""Rules the user wrote, running at the end of a scan."""

from __future__ import annotations

import pytest

import autoreply
from autoreply import Action, Condition, Rule
from config import Settings
from models import (Category, Classification, EmailMessage, FolderPlan,
                    OtherCategory, TriageItem)


def rule(*actions, name="Test rule", field="sender_domain", value="acme.example",
         enabled=True) -> Rule:
    return Rule(name=name, enabled=enabled,
                conditions=[Condition(field=field, operator="contains",
                                      value=value)],
                actions=list(actions))


def item(sender="somebody@acme.example", subject="Hello") -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid="1", sender_email=sender, subject=subject,
                           sender_name="A Person"),
        classification=Classification(
            summary="s", is_job_related=True, category=Category.INTERVIEW,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=0.9, reasoning="r", model="test"),
        folders=FolderPlan())


class TestWhichRulesSort:
    def test_filing_and_ticking_only(self):
        assert rule(Action("file_into", "Somewhere")).sorts_only
        assert rule(Action("tick"), Action("untick")).sorts_only
        assert rule(Action("leave")).sorts_only

    def test_drafting_is_not_sorting(self):
        assert not rule(Action("draft", "Hello")).sorts_only
        assert not rule(Action("draft_ai", "be polite")).sorts_only

    def test_touching_the_server_is_not_sorting(self):
        """Flagging and marking read both need the mailbox opening."""
        assert not rule(Action("flag")).sorts_only
        assert not rule(Action("mark_read")).sorts_only

    def test_a_mixed_rule_is_not_sorting(self):
        assert not rule(Action("file_into", "X"), Action("flag")).sorts_only

    def test_a_rule_that_does_nothing_is_not_sorting(self):
        assert not Rule(name="empty", enabled=True).sorts_only


class TestWhichRulesAreCollected:
    def test_settings_offers_only_the_sorting_ones(self):
        settings = Settings()
        settings.set_rules([
            rule(Action("file_into", "Filed"), name="files"),
            rule(Action("draft", "Hi"), name="drafts"),
            rule(Action("file_into", "Off"), name="off", enabled=False),
        ])
        names = [r.name for r in settings.sorting_rules]
        assert names == ["files"]

    def test_the_switch_turns_them_all_off(self):
        settings = Settings(apply_sorting_rules=False)
        settings.set_rules([rule(Action("file_into", "Filed"))])
        assert settings.sorting_rules == []

    def test_they_do_not_need_replies_to_be_armed(self):
        """Filing touches nobody's mailbox, so it has nothing to arm."""
        settings = Settings(auto_reply=False)
        settings.set_rules([rule(Action("file_into", "Filed"))])
        assert settings.replies_armed is False
        assert len(settings.sorting_rules) == 1


class TestRunningThem:
    @pytest.fixture
    def worker(self, tmp_path, monkeypatch):
        import workers
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))

        def build(*rules_):
            settings = Settings()
            settings.set_rules(list(rules_))
            return workers.ScanWorker(
                settings=settings, mailbox_password="", api_key="",
                window_start=None, window_end=None)
        return build

    def test_a_rule_points_a_row_at_a_folder(self, worker):
        scan = worker(rule(Action("file_into", "Somewhere Else")))
        rows = [item()]
        scan._apply_sorting_rules(rows)
        assert rows[0].target_folder == "Somewhere Else"
        assert rows[0].rule_name == "Test rule"
        assert "rule: Test rule" in rows[0].folder_display

    def test_a_rule_that_does_not_match_leaves_it_alone(self, worker):
        scan = worker(rule(Action("file_into", "Somewhere Else")))
        rows = [item(sender="nobody@elsewhere.example")]
        before = rows[0].target_folder
        scan._apply_sorting_rules(rows)
        assert rows[0].target_folder == before
        assert rows[0].rule_name == ""

    def test_a_rule_can_tick_and_untick(self, worker):
        scan = worker(rule(Action("untick")))
        rows = [item()]
        rows[0].approved = True
        scan._apply_sorting_rules(rows)
        assert rows[0].approved is False

    def test_leave_it_alone_clears_the_folder(self, worker):
        scan = worker(rule(Action("leave")))
        rows = [item()]
        rows[0].override_folder = "Wherever"
        scan._apply_sorting_rules(rows)
        assert rows[0].override_folder is None
        assert rows[0].approved is False

    def test_binning_points_the_row_at_the_bin_and_ticks_it(self, worker):
        """One action, both halves. A rule that filed but did not tick
        looked like it had worked and did nothing when you pressed Apply."""
        scan = worker(rule(Action("bin_it")))
        rows = [item()]
        rows[0].approved = False
        scan._apply_sorting_rules(rows)
        assert rows[0].target_folder == rows[0].folders.bin_folder
        assert rows[0].approved is True
        assert rows[0].rule_name == "Test rule"

    def test_the_bin_comes_from_the_account_not_the_rule(self, worker):
        """So one rule works across mailboxes whose roots are named
        differently, which is why bin_it takes no folder to type."""
        from dataclasses import replace
        scan = worker(rule(Action("bin_it")))
        rows = [item()]
        rows[0].folders = replace(rows[0].folders, other_root="Filed Away")
        scan._apply_sorting_rules(rows)
        assert rows[0].target_folder.startswith("Filed Away")

    def test_leave_beats_bin_it(self, worker):
        """Later rules win, and "leave it alone" has to mean it."""
        scan = worker(rule(Action("bin_it")), rule(Action("leave"),
                                                   name="Second"))
        rows = [item()]
        scan._apply_sorting_rules(rows)
        assert rows[0].override_folder is None
        assert rows[0].approved is False

    def test_binning_is_a_sorting_action_so_it_runs_after_a_scan(self):
        assert rule(Action("bin_it")).sorts_only

    def test_nothing_is_deleted_by_a_rule(self, worker):
        """The bin is a folder. A rule that deleted from the server would
        turn a mistyped condition into lost mail, with no way back."""
        scan = worker(rule(Action("bin_it")))
        rows = [item()]
        scan._apply_sorting_rules(rows)
        assert rows[0].moved is False
        assert "delete" not in rows[0].target_folder.lower() or (
            rows[0].target_folder.endswith("To Delete"))

    def test_a_drafting_rule_never_runs_here(self, worker):
        scan = worker(rule(Action("draft", "Hello there")))
        rows = [item()]
        scan._apply_sorting_rules(rows)
        assert rows[0].rule_name == ""

    def test_a_rule_that_raises_does_not_stop_the_scan(self, worker, monkeypatch):
        scan = worker(rule(Action("file_into", "Somewhere")))

        def boom(*_a, **_k):
            raise RuntimeError("a rule with a bad regex")

        monkeypatch.setattr(autoreply, "apply_rules", boom)
        rows = [item()]
        scan._apply_sorting_rules(rows)      # must not raise
        assert rows[0].rule_name == ""

    def test_a_rule_beats_the_sorter_and_the_memory(self, worker):
        """Somebody sat down and wrote it; that is the clearest intent there is."""
        import corrections
        scan = worker(rule(Action("file_into", "What The Rule Says")))
        rows = [item()]
        memory = corrections.Memory()
        memory.remember_move("somebody@acme.example", "What The Memory Says")
        corrections.apply_to(rows, memory)
        assert rows[0].target_folder == "What The Memory Says"

        scan._apply_sorting_rules(rows)
        assert rows[0].target_folder == "What The Rule Says"

    def test_no_rules_is_not_an_error(self, worker):
        scan = worker()
        rows = [item()]
        scan._apply_sorting_rules(rows)
        assert rows[0].rule_name == ""


class TestTheSettingsTab:
    def test_the_switch_round_trips(self, qapp, tmp_path, monkeypatch):
        from config import InMemoryCredentialStore
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        dialog = SettingsDialog(Settings(), InMemoryCredentialStore())
        try:
            dialog.sorting_rules_check.setChecked(False)
            assert dialog.collect().apply_sorting_rules is False
            dialog.sorting_rules_check.setChecked(True)
            assert dialog.collect().apply_sorting_rules is True
        finally:
            dialog.deleteLater()

    def test_the_tab_is_called_rules(self, qapp, tmp_path, monkeypatch):
        from config import InMemoryCredentialStore
        from settings_dialog import SettingsDialog
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        dialog = SettingsDialog(Settings(), InMemoryCredentialStore())
        try:
            labels = [dialog.tabs.tabText(i) for i in range(dialog.tabs.count())]
            assert "Rules" in labels
        finally:
            dialog.deleteLater()
