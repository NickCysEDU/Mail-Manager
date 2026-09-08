"""Demo mode, dry-run mode, the terminal scanner and the credential CLI."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

import demo_data  # noqa: E402
import main as main_module  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from gui import MainWindow  # noqa: E402
from models import Category, Disposition, NonJobRouting, OtherCategory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="session")
def devscan():
    spec = importlib.util.spec_from_file_location("devscan", ROOT / "tools" / "devscan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ==========================================================================
# The sample inbox
# ==========================================================================
class TestDemoData:
    def test_it_is_a_realistic_size(self):
        assert len(demo_data.DEMO_MESSAGES) >= 10

    @pytest.mark.parametrize("category", list(Category))
    def test_every_job_category_is_represented(self, category):
        found = {message.category for message in demo_data.DEMO_MESSAGES if message.is_job_related}
        found |= {Category.UNCLASSIFIED_OTHER}
        assert category in found

    @pytest.mark.parametrize(
        "topic",
        [OtherCategory.PERSONAL, OtherCategory.FINANCE, OtherCategory.NEWSLETTER,
         OtherCategory.SECURITY, OtherCategory.PROMOTION],
    )
    def test_the_common_topics_are_represented(self, topic):
        assert topic in {m.other_category for m in demo_data.DEMO_MESSAGES}

    def test_uids_are_unique(self):
        uids = [message.uid for message in demo_data.DEMO_MESSAGES]
        assert len(uids) == len(set(uids))

    def test_the_verdicts_pass_validation_untouched(self):
        """A sample that trips a safety guard would be a broken fixture."""
        for classification in demo_data.demo_classifications():
            assert classification.adjustments == ()
            assert classification.ok

    def test_emails_are_ordered_newest_first(self):
        dates = [message.date for message in demo_data.demo_emails()]
        assert dates == sorted(dates, reverse=True)

    def test_routing_covers_all_three_dispositions(self):
        dispositions = {item.disposition for item in demo_data.demo_items()}
        assert dispositions == {Disposition.MOVE, Disposition.REVIEW, Disposition.LEAVE}

    def test_the_ambiguous_samples_are_not_pre_ticked(self):
        """Nothing the sorter is unsure about is ticked, whatever it is.

        Where it goes depends on what it is: uncertain job mail waits in Needs
        Review, and uncertain post that is not job mail simply stays in the
        inbox, because Needs Review is a folder inside the job-search tree.
        """
        for item in demo_data.demo_items():
            if item.classification.confidence_score < 0.95:
                assert item.approved is False
                if item.classification.is_job_related:
                    assert item.target_folder == "Job Search/Needs Review"
                else:
                    assert item.target_folder is None      # left where it is

    def test_the_precedence_sample_files_as_interview(self):
        """A rejection that also offers a call belongs in Interview."""
        item = next(i for i in demo_data.demo_items() if i.email.uid == "1006")
        assert item.classification.category is Category.INTERVIEW
        assert item.target_folder == "Job Search/Interview"

    def test_routing_settings_are_honoured(self):
        items = demo_data.demo_items(
            non_job_routing=NonJobRouting.FILE, auto_approve_non_job=True
        )
        finance = next(i for i in items if i.classification.other_category is OtherCategory.FINANCE)
        assert finance.target_folder == "Sorted Mail/Finance"
        assert finance.approved is True

    def test_mime_round_trips_through_the_real_parser(self):
        from imap_engine import parse_message

        raw = demo_data.demo_mime()
        assert len(raw) == len(demo_data.DEMO_MESSAGES)
        parsed = parse_message(raw["1001"], uid="1001")
        assert "technical interview" in parsed.subject
        assert "calendly.com" in " ".join(parsed.links)

    def test_verdict_lookup_by_prompt(self):
        subject = demo_data.DEMO_MESSAGES[0].subject
        verdict = demo_data.verdict_for_prompt(f"...<subject>{subject}</subject>...".lower())
        assert verdict["category"] == "INTERVIEW"

    def test_verdict_lookup_misses_cleanly(self):
        assert demo_data.verdict_for_prompt("something else entirely") is None


# ==========================================================================
# Demo mode in the window
# ==========================================================================
class TestDemoMode:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(), InMemoryCredentialStore(), demo=True)
        yield window
        window.close()

    def test_it_announces_itself(self, window):
        assert window.banner.isVisibleTo(window)
        assert "Demo mode" in window.banner.text()
        assert "DEMO" in window.windowTitle()

    def test_the_scan_button_says_what_it_does(self, window):
        assert window.scan_button.text() == "Reload Sample Data"

    def test_no_first_run_prompt_without_credentials(self, window, dialog_calls):
        assert window.settings.is_configured() is False
        window._first_run_check()
        assert dialog_calls == []

    def test_scanning_loads_samples_without_touching_the_network(self, window, monkeypatch):
        import workers

        monkeypatch.setattr(
            workers, "IMAPEngine",
            lambda *a, **k: pytest.fail("demo mode must never open a connection"),
        )
        window.start_scan()
        assert window.model.rowCount() == len(demo_data.DEMO_MESSAGES)
        assert window.scan_worker is None
        assert "no API calls" in window.usage_label.text()

    def test_applying_marks_rows_without_imap(self, window, monkeypatch):
        import workers
        from PySide6.QtWidgets import QMessageBox

        monkeypatch.setattr(
            workers, "IMAPEngine",
            lambda *a, **k: pytest.fail("demo mode must never open a connection"),
        )
        # Demo mode still shows the real confirmation, so the demo is faithful.
        monkeypatch.setattr(QMessageBox, "exec", lambda self: QMessageBox.StandardButton.Ok)
        window._load_demo_data()
        approved = [i for i in window.model.items if i.approved]
        assert approved
        window.apply_moves()
        assert all(item.moved for item in approved)
        assert window.apply_worker is None
        assert "Demo" in window.status_label.text()

    def test_the_confirmation_is_still_shown(self, window, dialog_calls):
        """A demo that skipped the confirmation would misrepresent the real app."""
        window._load_demo_data()
        window.apply_moves()          # the guard fixture answers Cancel
        assert any("message(s) out of INBOX?" in text for _, _, text in dialog_calls)
        assert not any(item.moved for item in window.model.items)

    def test_it_reflects_the_configured_threshold(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(
            Settings(confidence_threshold=0.99), InMemoryCredentialStore(), demo=True
        )
        try:
            window._load_demo_data()
            assert all(item.threshold == 0.99 for item in window.model.items)
            # The 0.96 and 0.97 samples now fall below the bar.
            assert window.model.summary().to_move < 6
        finally:
            window.close()


class TestDryRunMode:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(
            Settings(icloud_email="a@b.com"), InMemoryCredentialStore(), dry_run=True
        )
        yield window
        window.close()

    def test_it_announces_itself(self, window):
        assert "Dry run" in window.banner.text()
        assert "DRY RUN" in window.windowTitle()

    def test_applying_is_refused(self, window, dialog_calls):
        window.model.set_items(demo_data.demo_items())
        window.apply_moves()
        assert window.apply_worker is None
        assert any("Dry run" in title for _, title, _ in dialog_calls)

    def test_normal_mode_shows_no_banner(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        try:
            assert window.banner.isVisible() is False
            assert window.scan_button.text() == "Scan && Analyze"
        finally:
            window.close()


# ==========================================================================
# Keyboard shortcuts
# ==========================================================================
class TestShortcuts:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_find_focuses_the_filter_box(self, window, qapp):
        window.show()
        qapp.processEvents()
        window._focus_search()
        qapp.processEvents()
        assert window.focusWidget() is window.search_edit

    def test_clear_filters_resets_every_control(self, window):
        window.search_edit.setText("interview")
        window.show_combo.setCurrentIndex(2)
        window._clear_filters()
        assert window.search_edit.text() == ""
        assert window.show_combo.currentIndex() == 0
        assert window.category_filter.currentIndex() == 0

    def test_window_presets_are_bound_to_number_keys(self, window):
        from models import TimeWindow

        window._select_window(TimeWindow.LAST_7_DAYS)
        assert window.settings.window is TimeWindow.LAST_7_DAYS
        assert window.window_buttons[TimeWindow.LAST_7_DAYS].isChecked()

    def test_the_cheat_sheet_lists_the_important_ones(self, window, dialog_calls):
        window._show_shortcuts()
        assert dialog_calls
        text = dialog_calls[-1][2]
        for key in ("⌘R", "⌘.", "⌘F", "⌘L"):
            assert key in text


# ==========================================================================
# CLI flags
# ==========================================================================
class TestCliFlags:
    def test_new_flags_parse(self):
        args = main_module.build_parser().parse_args(["--demo", "--dry-run", "--verbose"])
        assert (args.demo, args.dry_run, args.verbose) == (True, True, True)

    def test_short_verbose_flag(self):
        assert main_module.build_parser().parse_args(["-v"]).verbose is True

    def test_verbose_implies_debug_logging(self, tmp_path, monkeypatch):
        import logging

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        main_module.configure_logging("DEBUG", echo=True)
        assert logging.getLogger().level == logging.DEBUG

    def test_show_config_reports_missing_credentials(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        assert main_module.main(["--show-config"]) == 0
        output = capsys.readouterr().out
        assert "confidence_threshold" in output
        assert "MISSING" in output
        assert "./dev creds" in output

    def test_show_config_never_prints_a_secret(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-super-secret-value")
        main_module.main(["--show-config"])
        output = capsys.readouterr().out
        assert "sk-ant-super-secret-value" not in output
        assert "stored" in output

    def test_set_credentials_writes_to_the_keychain(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        # A backend that actually needs a key; the default one does not.
        Settings(provider="gemini").normalized().save()
        store = InMemoryCredentialStore()
        monkeypatch.setattr("config.CredentialStore", lambda *a, **k: store)
        monkeypatch.setattr("builtins.input", lambda prompt="": "you@icloud.example")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "secret-value")

        assert main_module.set_credentials() == 0
        assert store.get_icloud_password("you@icloud.example") == "secret-value"
        assert store.get_provider_key("gemini") == "secret-value"
        assert Settings.load().icloud_email == "you@icloud.example"
        assert "secret-value" not in capsys.readouterr().out

    def test_set_credentials_requires_an_email(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        monkeypatch.setattr("getpass.getpass", lambda prompt="": "")
        assert main_module.set_credentials() == 2

    def test_set_credentials_handles_a_cancel(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))

        def interrupt(prompt=""):
            raise KeyboardInterrupt

        monkeypatch.setattr("builtins.input", interrupt)
        assert main_module.set_credentials() == 130


# ==========================================================================
# The terminal scanner
# ==========================================================================
class TestDevScan:
    def test_fake_mode_runs_the_whole_pipeline(self, devscan, capsys):
        assert devscan.main(["--fake", "--no-colour"]) == 0
        output = capsys.readouterr().out
        assert "Job Search/Interview" in output
        assert "MOVE" in output and "REVIEW" in output and "LEAVE" in output
        assert "Nothing was moved" in output

    def test_json_output_is_valid_and_complete(self, devscan, capsys):
        assert devscan.main(["--fake", "--json"]) == 0
        rows = json.loads(capsys.readouterr().out)
        assert len(rows) == len(demo_data.DEMO_MESSAGES)
        assert {"uid", "category", "confidence", "target_folder"} <= set(rows[0])

    def test_limit(self, devscan, capsys):
        devscan.main(["--fake", "--json", "--limit", "3"])
        assert len(json.loads(capsys.readouterr().out)) == 3

    def test_uid_filter(self, devscan, capsys):
        devscan.main(["--fake", "--json", "--uid", "1001"])
        rows = json.loads(capsys.readouterr().out)
        assert [row["uid"] for row in rows] == ["1001"]

    def test_an_unknown_uid_is_an_error(self, devscan):
        assert devscan.main(["--fake", "--uid", "999999"]) == 1

    def test_prompt_mode_prints_the_real_payload(self, devscan, capsys):
        assert devscan.main(["--fake", "--uid", "1001", "--prompt"]) == 0
        output = capsys.readouterr().out
        assert output.count("<email>") == 1
        assert "<bulk_mail_header_present>" in output
        assert "LINKS FOUND IN MESSAGE" in output

    def test_full_mode_includes_the_reasoning(self, devscan, capsys):
        devscan.main(["--fake", "--no-colour", "--full", "--limit", "1"])
        assert "Reasoning:" in capsys.readouterr().out

    def test_it_never_moves_anything(self, devscan, monkeypatch):
        """devscan is read-only by construction - there is no move path in it."""
        source = (ROOT / "tools" / "devscan.py").read_text()
        assert "move_messages" not in source
        assert "ensure_folder" not in source
