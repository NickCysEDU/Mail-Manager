"""Settings dialog, preview rendering, export and the apply confirmation."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QMessageBox  # noqa: E402

import gui as gui_module  # noqa: E402
from config import InMemoryCredentialStore, Settings  # noqa: E402
from gui import MainWindow, SettingsDialog  # noqa: E402
from triage_table import _disposition_badge, _reasoning_html  # noqa: E402
from imap_engine import MoveReport  # noqa: E402
from models import (  # noqa: E402
    CATEGORY_COLORS,
    Category,
    Classification,
    EmailMessage,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
)
from workers import ScanOutcome  # noqa: E402

UTC = timezone.utc


@pytest.fixture(scope="session")
def qapp():
    return QApplication.instance() or QApplication([])


def make_item(uid="1", **overrides):
    classification_kwargs = overrides.pop("classification", {})
    classification = Classification(**{
        "summary": "A recruiter invited you to interview. Book a slot.",
        "reasoning": "Calendly link.\nRunner-up NEXT_STEPS rejected.",
        "model": "claude-opus-5", "input_tokens": 1200, "output_tokens": 180,
        "is_job_related": True, "category": Category.INTERVIEW,
        "other_category": OtherCategory.NOT_APPLICABLE, "confidence_score": 0.98,
        **classification_kwargs,
    })
    message = EmailMessage(
        uid=uid, subject="Interview invitation", sender_name="Dana Reyes",
        sender_email="dana@x.com", date=datetime(2026, 9, 4, 12, 0, tzinfo=UTC),
        body_text="Please pick a time.", links=("https://calendly.com/x",),
    )
    return TriageItem(message, classification, FolderPlan(), **overrides)


class TestPreviewRendering:
    def test_badge_shows_category_confidence_and_folder(self):
        html = _disposition_badge(make_item())
        assert "Interview" in html
        assert "98% confident" in html
        assert "Job Search/Interview" in html

    def test_badge_marks_a_moved_row(self):
        item = make_item()
        item.moved = True
        assert "<b>moved</b>" in _disposition_badge(item)

    def test_badge_shows_a_move_error(self):
        item = make_item()
        item.move_error = "COPY failed"
        assert "COPY failed" in _disposition_badge(item)

    def test_reasoning_table_covers_the_decision(self):
        html = _reasoning_html(make_item())
        for label in ("Summary", "Reasoning", "Decision", "Confidence", "Model", "Tokens", "Links found"):
            assert label in html
        assert "claude-opus-5" in html
        assert "calendly.com" in html

    def test_safety_adjustments_are_surfaced_to_the_user(self):
        item = make_item(classification={"adjustments": ("category was not in the enum",)})
        html = _reasoning_html(item)
        assert "Safety adjustments" in html
        assert "category was not in the enum" in html

    def test_errors_are_surfaced(self):
        item = TriageItem(
            EmailMessage(uid="1"), Classification.failure("API exploded"), FolderPlan()
        )
        assert "API exploded" in _reasoning_html(item)

    def test_html_in_a_summary_cannot_inject_markup(self):
        item = make_item(classification={"summary": "<script>alert(1)</script>"})
        html = _reasoning_html(item)
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


class TestSettingsDialog:
    @pytest.fixture
    def dialog(self, qapp):
        settings = Settings(icloud_email="you@icloud.example", provider="anthropic",
                            model="claude-haiku-4-5")
        store = InMemoryCredentialStore()
        store.set_icloud_password("you@icloud.example", "abcd-efgh")
        store.set_anthropic_key("sk-ant-stored")
        dialog = SettingsDialog(settings, store)
        yield dialog, store
        dialog.deleteLater()

    def test_loads_existing_values_including_secrets(self, dialog):
        subject, _ = dialog
        assert subject.email_edit.text() == "you@icloud.example"
        assert subject.password_edit.text() == "abcd-efgh"
        assert subject.api_key_edit.text() == "sk-ant-stored"

    def test_secrets_are_masked_by_default(self, dialog):
        from PySide6.QtWidgets import QLineEdit

        subject, _ = dialog
        assert subject.password_edit.echoMode() == QLineEdit.EchoMode.Password
        assert subject.api_key_edit.echoMode() == QLineEdit.EchoMode.Password

    def test_every_backend_is_offered(self, dialog):
        import providers

        subject, _ = dialog
        offered = {subject.provider_combo.itemData(i)
                   for i in range(subject.provider_combo.count())}
        assert offered == set(providers.PROVIDERS_BY_NAME)

    def test_switching_backend_repopulates_the_model_list(self, dialog):
        import providers

        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("gemini"))
        models = {subject.model_combo.itemData(i) for i in range(subject.model_combo.count())}
        assert models == {c.value for c in providers.GeminiProvider.models}
        assert subject.model_combo.currentData() == "gemini-flash-lite-latest"

    def test_the_local_backend_hides_the_key_field(self, dialog):
        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("ollama"))
        assert subject.key_row_widget.isHidden() is True
        assert subject.base_url_edit.isHidden() is False
        assert "Ollama" in subject.test_model_button.text()

    def test_reasoning_effort_is_only_shown_for_claude(self, dialog):
        subject, _ = dialog
        assert subject.effort_combo.isHidden() is False
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("gemini"))
        assert subject.effort_combo.isHidden() is True

    def test_each_backend_shows_its_own_stored_key(self, dialog):
        subject, store = dialog
        store.set_provider_key("gemini", "AIza-stored")
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("gemini"))
        assert subject.api_key_edit.text() == "AIza-stored"
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("anthropic"))
        assert subject.api_key_edit.text() == "sk-ant-stored"

    def test_saving_a_key_only_touches_that_backend(self, dialog):
        subject, store = dialog
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("gemini"))
        subject.api_key_edit.setText("AIza-new")
        collected = subject.collect()
        subject.persist_credentials(collected)
        assert store.get_provider_key("gemini") == "AIza-new"
        assert store.get_provider_key("anthropic") == "sk-ant-stored"

    def test_the_model_note_reports_price_or_freedom(self, dialog):
        subject, _ = dialog
        assert "per million tokens" in subject.model_note.text()
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("ollama"))
        assert "nothing leaves this Mac" in subject.model_note.text()

    def test_collect_carries_the_backend_choice(self, dialog):
        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("ollama"))
        subject.base_url_edit.setText("http://192.168.1.9:11434")
        collected = subject.collect()
        assert collected.provider == "ollama"
        assert collected.model == "llama3.2:3b"
        assert collected.base_url == "http://192.168.1.9:11434"
        assert collected.needs_api_key is False

    def test_a_typed_model_name_survives_on_a_hosted_backend(self, dialog):
        """Hosted backends release models faster than a bundled list follows.

        Gemini's pinned 2.x ids went stale during development and began
        answering 404, so typing a name has to keep working there.
        """
        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(
            subject.provider_combo.findData("gemini"))
        assert subject.model_combo.isEditable() is True
        subject.model_combo.setEditText("gemini-9.9-flash")
        assert subject.collect().model == "gemini-9.9-flash"

    def test_a_local_model_outside_the_list_is_still_offered(self, dialog):
        """A model pulled by hand must be selectable even though the dropdown
        cannot be typed into. It is added when it is found installed."""
        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(
            subject.provider_combo.findData("ollama"))
        assert subject.model_combo.isEditable() is False

        import ondevice
        subject._ollama_state = ondevice.Status(
            binary="/usr/local/bin/ollama", running=True,
            models=["llama3.2:3b", "mistral:7b"])
        subject._refresh_installed_models()
        index = subject.model_combo.findData("mistral:7b")
        assert index >= 0, "an installed model was not offered"
        subject.model_combo.setCurrentIndex(index)
        assert subject.collect().model == "mistral:7b"

    def test_a_selected_model_uses_its_id_not_its_label(self, dialog):
        subject, _ = dialog
        subject.provider_combo.setCurrentIndex(subject.provider_combo.findData("ollama"))
        index = subject.model_combo.findData("qwen2.5:7b")
        subject.model_combo.setCurrentIndex(index)
        assert subject.model_combo.currentText() == "Qwen 2.5 7B"   # the label
        assert subject.collect().model == "qwen2.5:7b"              # the id

    def test_collect_round_trips_every_edit(self, dialog):
        subject, _ = dialog
        subject.email_edit.setText("other@icloud.com")
        subject.mailbox_edit.setText("Archive")
        subject.effort_combo.setCurrentText("high")
        subject.threshold_slider.setValue(90)
        subject.concurrency_spin.setValue(8)
        subject.root_edit.setText("Hunt")
        subject.routing_combo.setCurrentIndex(
            subject.routing_combo.findData(NonJobRouting.FILE.value)
        )
        subject.auto_non_job_check.setChecked(True)

        collected = subject.collect()
        assert collected.icloud_email == "other@icloud.com"
        assert collected.source_mailbox == "Archive"
        assert collected.effort == "high"
        assert collected.confidence_threshold == pytest.approx(0.90)
        assert collected.concurrency == 8
        assert collected.folder_root == "Hunt"
        assert collected.routing is NonJobRouting.FILE
        assert collected.auto_approve_non_job is True

    def test_collect_normalises_bad_input(self, dialog):
        subject, _ = dialog
        subject.email_edit.setText("   spaced@icloud.com  ")
        subject.root_edit.setText("   ")
        collected = subject.collect()
        assert collected.icloud_email == "spaced@icloud.com"
        assert collected.folder_root == "Job Search"

    def test_persisting_credentials_writes_to_the_store(self, dialog):
        subject, store = dialog
        subject.email_edit.setText("new@icloud.com")
        subject.password_edit.setText("wxyz-1234")
        subject.api_key_edit.setText("sk-ant-new")
        collected = subject.collect()
        subject.persist_credentials(collected)
        assert store.get_icloud_password("new@icloud.com") == "wxyz-1234"
        assert store.get_anthropic_key() == "sk-ant-new"

    def test_topic_folder_fields_are_disabled_unless_filing(self, dialog):
        subject, _ = dialog
        subject.routing_combo.setCurrentIndex(
            subject.routing_combo.findData(NonJobRouting.LEAVE.value)
        )
        assert subject.other_root_edit.isEnabled() is False
        subject.routing_combo.setCurrentIndex(
            subject.routing_combo.findData(NonJobRouting.FILE.value)
        )
        assert subject.other_root_edit.isEnabled() is True

    def test_folder_preview_tracks_the_root_name(self, dialog):
        subject, _ = dialog
        subject.root_edit.setText("Hunt")
        assert "Hunt/Interview" in subject.folders_preview.text()
        assert "Hunt/Received" in subject.folders_preview.text()

    def test_folder_preview_mentions_topic_folders_when_filing(self, dialog):
        subject, _ = dialog
        subject.routing_combo.setCurrentIndex(
            subject.routing_combo.findData(NonJobRouting.FILE.value)
        )
        assert "one subfolder per topic" in subject.folders_preview.text()

    def test_every_routing_option_is_offered(self, dialog):
        subject, _ = dialog
        offered = {subject.routing_combo.itemData(i) for i in range(subject.routing_combo.count())}
        assert offered == {member.value for member in NonJobRouting}


class TestSettingsLeaveTheLookAlone:
    """Settings restyled the whole app as it filled itself in, up to three
    times, and once more on Cancel, for a look that had not changed: about
    a second every time it was opened, and every time a model without a key
    was chosen."""

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        # The window paints the whole application in its look, which every
        # later test in this process would otherwise be measured in.
        was = (qapp.font(), qapp.palette(), qapp.styleSheet())
        # Not the first density, and readable: each control changes as the
        # dialog fills in.
        settings = Settings(icloud_email="you@icloud.example",
                            density="compact", readable=True)
        window = MainWindow(settings, InMemoryCredentialStore())
        yield window
        window.close()
        qapp.setFont(was[0])
        qapp.setPalette(was[1])
        qapp.setStyleSheet(was[2])

    @pytest.fixture
    def restyles(self, monkeypatch):
        import theme

        calls = []
        real = theme.apply

        def counted(app, *look):
            calls.append(look)
            return real(app, *look)

        monkeypatch.setattr(theme, "apply", counted)
        return calls

    def _open(self, window, monkeypatch, then):
        """Open Settings, do ``then`` while it is up, and Cancel."""
        def shown(dialog):
            then(dialog)
            return QDialog.DialogCode.Rejected

        monkeypatch.setattr(QDialog, "exec", shown)
        window.open_settings()

    def test_opening_and_cancelling_restyle_nothing(self, window, restyles,
                                                     monkeypatch):
        import theme

        notes = []
        self._open(window, monkeypatch,
                   lambda dialog: notes.append(dialog.density_note.text()))
        assert restyles == []
        # What the density does is still said.
        assert notes == [next(blurb for name, _label, blurb in theme.DENSITIES
                              if name == "compact")]

    def test_a_look_tried_in_settings_is_put_back_on_cancel(
            self, window, restyles, monkeypatch):
        def try_dense(dialog):
            dialog.density_combo.setCurrentIndex(
                dialog.density_combo.findData("dense"))

        self._open(window, monkeypatch, try_dense)
        assert restyles[0][3] == "dense"
        assert restyles[-1] == ("system", "normal", True, "compact")
        assert window.settings.density == "compact"

    def test_help_switched_on_in_settings_goes_off_again_on_cancel(
            self, window, qapp, monkeypatch):
        import helpmode

        helpmode.install(qapp, False)
        during = []

        def switch_on(dialog):
            dialog.help_button.setChecked(True)
            during.append(qapp._help_filter.enabled)

        self._open(window, monkeypatch, switch_on)
        assert during == [True]
        assert qapp._help_filter.enabled is False
        assert not window.help_button.isChecked()


class TestColorCoding:
    def test_every_job_category_gets_its_own_colour(self):
        from gui import category_color

        seen = {
            category_color(make_item(classification={"category": category}).classification)
            for category in Category
        }
        assert len(seen) == len(Category)

    def test_non_job_mail_uses_the_neutral_colour(self):
        from gui import OTHER_COLOR, category_color

        item = make_item(classification={
            "is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
            "other_category": OtherCategory.FINANCE,
        })
        assert category_color(item.classification) == OTHER_COLOR

    def test_shade_lightens_and_darkens(self):
        from widgets import _shade

        assert _shade("#808080", 1.5) == "#C0C0C0"
        assert _shade("#808080", 0.5) == "#404040"
        assert _shade("#FFFFFF", 2.0) == "#FFFFFF"   # clamped

    def test_the_model_exposes_a_colour_for_each_row(self, qapp):
        from PySide6.QtCore import Qt

        from gui import TriageTableModel

        model = TriageTableModel()
        model.set_items([make_item("1")])
        colour = model.data(model.index(0, TriageTableModel.COL_CATEGORY),
                            Qt.ItemDataRole.UserRole + 1)
        assert colour == CATEGORY_COLORS[Category.INTERVIEW]

    def test_rows_that_will_move_are_tinted(self, qapp):
        from PySide6.QtCore import Qt

        from gui import TriageTableModel

        model = TriageTableModel()
        model.set_items([
            make_item("1"),
            make_item("2", classification={
                "is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.NEWSLETTER, "confidence_score": 0.99,
            }),
        ])
        moving = model.data(model.index(0, 1), Qt.ItemDataRole.BackgroundRole)
        leaving = model.data(model.index(1, 1), Qt.ItemDataRole.BackgroundRole)
        assert moving is not None and moving.alpha() < 60   # a hint, not a highlight
        assert leaving is None

    def test_a_swatch_icon_is_produced(self, qapp):
        from gui import _swatch

        assert not _swatch("#2E9E63").isNull()


class TestMainWindowFlows:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"), InMemoryCredentialStore())
        yield window
        window.close()

    def test_preset_windows_resolve(self, window):
        for preset, days in (
            (TimeWindow.LAST_24_HOURS, 1), (TimeWindow.LAST_3_DAYS, 3), (TimeWindow.LAST_7_DAYS, 7)
        ):
            window._window_selected(preset)
            start, end = window._current_window()
            assert (end - start) == timedelta(days=days)

    def test_custom_window_uses_the_date_pickers(self, window):
        from PySide6.QtCore import QDate

        window._window_selected(TimeWindow.CUSTOM)
        window.start_date.setDate(QDate(2026, 8, 1))
        window.end_date.setDate(QDate(2026, 8, 3))
        start, end = window._current_window()
        assert start == datetime(2026, 8, 1, tzinfo=UTC)
        assert end == datetime(2026, 8, 4, tzinfo=UTC)   # end date is inclusive
        assert window.settings.custom_start.startswith("2026-08-01")

    def test_custom_range_widgets_toggle_with_the_preset(self, window):
        window.show()
        window._window_selected(TimeWindow.CUSTOM)
        assert window.start_date.isVisible()
        window._window_selected(TimeWindow.LAST_7_DAYS)
        assert not window.start_date.isVisible()

    def test_scan_results_populate_the_window(self, window, dialog_calls):
        outcome = ScanOutcome(
            items=[make_item("1"), make_item("2", classification={"confidence_score": 0.3})],
            folder_plan=FolderPlan(),
            created_folders=["Job Search"],
            warnings=["Only the 3 most recent were fetched."],
            usage_text="2 API calls",
        )
        window._on_scan_done(outcome)
        assert window.model.rowCount() == 2
        assert window.usage_label.text() == "2 API calls"
        assert any("most recent" in line for line in window.log_view.toPlainText().splitlines())
        # An incomplete scan must be announced, not buried in the log.
        assert any(kind == "information" and "Scan notes" in title for kind, title, _ in dialog_calls)

    def test_an_empty_scan_says_so(self, window):
        window._on_scan_done(ScanOutcome(folder_plan=FolderPlan()))
        assert "No messages found" in window.status_label.toolTip()

    def test_apply_results_update_the_rows(self, window):
        window.model.set_items([make_item("1"), make_item("2")])
        window._on_apply_done(MoveReport(moved={"1": "Job Search/Interview"}, failed={"2": "nope"}))
        assert window.model.item_at(0).moved is True
        assert window.model.item_at(1).move_error == "nope"
        assert "Filed 1 message" in window.status_label.text()

    def test_apply_with_nothing_selected_is_refused(self, window, dialog_calls):
        window.model.set_items([make_item("1", classification={"confidence_score": 0.2})])
        window.apply_moves()
        assert any("Tick at least one message" in text for _, _, text in dialog_calls)
        assert window.apply_worker is None

    def test_apply_can_be_cancelled_at_the_confirmation(self, window):
        """The guard fixture answers every confirmation with Cancel."""
        window.model.set_items([make_item("1")])
        window.apply_moves()
        assert window.apply_worker is None

    def test_the_confirmation_warns_about_low_confidence_rows(self, window, monkeypatch):
        captured = {}

        def fake_exec(self):
            captured["text"] = self.text()
            captured["informative"] = self.informativeText()
            return QMessageBox.StandardButton.Cancel

        monkeypatch.setattr(QMessageBox, "exec", fake_exec)
        item = make_item("1", classification={"confidence_score": 0.4})
        item.override_folder = "Job Search/Interview"
        item.approved = True   # the user ticked it despite the low confidence
        window.model.set_items([item])
        window.apply_moves()
        assert "Move 1 message(s)" in captured["text"]
        assert "below your 95% confidence threshold" in captured["informative"]
        assert "copied to its folder first" in captured["informative"]

    def test_scan_without_credentials_opens_settings(self, window, monkeypatch, dialog_calls):
        opened = []
        monkeypatch.setattr(MainWindow, "open_settings", lambda self: opened.append(True))
        window.start_scan()
        assert opened
        assert any("Missing password" in title for _, title, _ in dialog_calls)
        assert window.scan_worker is None

    def test_settings_are_saved_and_reapplied(self, window, monkeypatch):
        def fake_exec(self):
            self.root_edit.setText("Hunt")
            self.threshold_slider.setValue(80)
            return QDialog.DialogCode.Accepted

        monkeypatch.setattr(SettingsDialog, "exec", fake_exec)
        window.open_settings()
        assert window.settings.folder_root == "Hunt"
        assert window.settings.confidence_threshold == pytest.approx(0.80)
        assert window.folder_plan.root == "Hunt"
        assert Settings.load().folder_root == "Hunt"

    def test_cancelling_settings_changes_nothing(self, window, monkeypatch):
        monkeypatch.setattr(SettingsDialog, "exec", lambda self: QDialog.DialogCode.Rejected)
        window.open_settings()
        assert window.settings.folder_root == "Job Search"


class TestExport:
    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example"), InMemoryCredentialStore())
        window.model.set_items([
            make_item("1"),
            make_item("2", classification={
                "is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
                "other_category": OtherCategory.FINANCE, "confidence_score": 0.99,
            }),
        ])
        yield window
        window.close()

    def test_csv_export(self, window, tmp_path, monkeypatch):
        target = tmp_path / "out.csv"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
        )
        window._export("csv")
        rows = list(csv.DictReader(target.open(encoding="utf-8")))
        assert len(rows) == 2
        assert rows[0]["category"] == "INTERVIEW"
        assert rows[1]["other_category"] == "FINANCE"
        assert rows[1]["target_folder"] == ""

    def test_json_export(self, window, tmp_path, monkeypatch):
        target = tmp_path / "out.json"
        monkeypatch.setattr(
            QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(target), ""))
        )
        window._export("json")
        rows = json.loads(target.read_text(encoding="utf-8"))
        assert [r["uid"] for r in rows] == ["1", "2"]
        assert rows[0]["disposition"] == "MOVE"

    def test_cancelling_the_save_dialog_writes_nothing(self, window, tmp_path):
        """The guard fixture answers the save dialog with an empty path."""
        window._export("csv")
        assert list(tmp_path.glob("*.csv")) == []

    def test_export_with_no_results_is_refused(self, qapp, tmp_path, monkeypatch, dialog_calls):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(), InMemoryCredentialStore())
        window._export("csv")
        window.close()
        assert any("Run a scan first" in text for _, _, text in dialog_calls)


class TestScanWiring:
    """The Scan button once passed a keyword the worker no longer accepted, so
    it raised TypeError on click. Nothing caught it, because every test either
    mocked the worker or stopped at the missing-credentials guard."""

    @pytest.fixture
    def window(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_icloud_password("you@icloud.example", "app-specific")
        store.set_provider_key("gemini", "AIza-test")
        settings = Settings(icloud_email="you@icloud.example", provider="gemini")
        window = MainWindow(settings, store)
        yield window, store
        window.close()

    def test_the_scan_button_builds_a_real_worker(self, window, monkeypatch):
        """Constructs the actual ScanWorker: a renamed argument fails here."""
        import workers

        captured = {}
        real = workers.ScanWorker

        class Recorder(real):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**kwargs)

            def start(self):        # never touch the network
                pass

        monkeypatch.setattr(gui_module, "ScanWorker", Recorder)
        subject, _ = window
        subject.start_scan()
        assert captured, "start_scan did not construct a worker"
        assert captured["api_key"] == "AIza-test"
        # One password per mailbox now, keyed by account id.
        assert list(captured["mailbox_password"].values()) == ["app-specific"]
        assert captured["settings"].provider == "gemini"
        assert captured["window_start"] < captured["window_end"]

    def test_the_key_check_follows_the_selected_backend(self, window, monkeypatch, dialog_calls):
        """An Anthropic-shaped check used to block a Gemini scan."""
        subject, store = window
        assert store.get_provider_key("anthropic") == ""     # deliberately absent
        import workers

        captured = {}

        class Recorder(workers.ScanWorker):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**kwargs)

            def start(self):
                pass

        monkeypatch.setattr(gui_module, "ScanWorker", Recorder)
        subject.start_scan()
        assert captured, "a Gemini scan must not be blocked by a missing Anthropic key"
        assert not any("Anthropic" in text for _, _, text in dialog_calls)

    def test_a_missing_key_names_the_selected_backend(self, qapp, tmp_path, monkeypatch,
                                                      dialog_calls):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_icloud_password("you@icloud.example", "app-specific")
        settings = Settings(icloud_email="you@icloud.example", provider="gemini")
        subject = MainWindow(settings, store)
        monkeypatch.setattr(MainWindow, "open_settings", lambda self, tab=0: None)
        try:
            subject.start_scan()
            texts = " ".join(text for _, _, text in dialog_calls)
            assert "Gemini" in texts
            assert "Anthropic" not in texts
            assert subject.scan_worker is None
        finally:
            subject.close()

    def test_a_keyless_backend_scans_without_a_key(self, qapp, tmp_path, monkeypatch):
        """Ollama and the rule set need no key, so nothing may block on one."""
        import workers

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        store = InMemoryCredentialStore()
        store.set_icloud_password("you@icloud.example", "app-specific")
        settings = Settings(icloud_email="you@icloud.example", provider="rules")
        subject = MainWindow(settings, store)
        captured = {}

        class Recorder(workers.ScanWorker):
            def __init__(self, **kwargs):
                captured.update(kwargs)
                super().__init__(**kwargs)

            def start(self):
                pass

        monkeypatch.setattr(gui_module, "ScanWorker", Recorder)
        try:
            subject.start_scan()
            assert captured, "a keyless backend must not be blocked"
            assert captured["api_key"] == ""
        finally:
            subject.close()


class TestNoHardcodedBackend:
    def test_the_ui_never_names_one_backend_in_fixed_text(self):
        """Every user-visible mention must follow the selected backend."""
        import pathlib

        for name in ("gui.py", "workers.py", "main.py"):
            source = pathlib.Path(__file__).resolve().parents[1] / name
            for number, line in enumerate(source.read_text().splitlines(), start=1):
                if "claude-" in line or "AnthropicProvider" in line:
                    continue          # model ids and the class name are fine
                assert "Claude" not in line, f"{name}:{number}: {line.strip()}"
                assert "Anthropic API key" not in line, f"{name}:{number}"

    def test_the_preview_names_the_backend(self, qapp, tmp_path, monkeypatch):
        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="a@b.com", provider="gemini"),
                            InMemoryCredentialStore())
        try:
            assert "Gemini" in window.preview.analysis_label.text()
            assert "Gemini" in window.preview.body_mode.itemText(2)
        finally:
            window.close()


class TestTheMessageIsShownAsSent:
    """The preview draws the HTML a message came with, fetching nothing, and
    keeps the plain text a click away."""

    @staticmethod
    def _items():
        import demo_data
        from config import Settings

        items = demo_data.demo_items(folders=Settings().folder_plan())
        return {item.email.uid: item for item in items}

    @staticmethod
    def _pane(qtbot, width=900, height=600):
        from triage_table import PreviewPane

        pane = PreviewPane()
        qtbot.addWidget(pane)
        pane.resize(width, height)
        pane.show()
        return pane

    def test_formatted_mail_is_drawn_by_default(self, qtbot):
        pane = self._pane(qtbot)
        item = self._items()["1001"]
        assert item.email.body_html
        pane.show_item(0, item)
        assert pane.body_mode.currentIndex() == 0
        assert pane.body_stack.currentWidget() is pane.rich_view
        shown = pane.rich_view.toPlainText()
        assert "Senior Platform Engineer" in shown and "Pick a time" in shown
        assert "<" not in shown

    def test_plain_mail_shows_its_text(self, qtbot):
        pane = self._pane(qtbot)
        item = self._items()["1008"]
        assert not item.email.body_html
        pane.show_item(0, item)
        assert pane.body_stack.currentWidget() is pane.body_view
        assert item.email.body_text[:30] in pane.body_view.toPlainText()

    def test_the_plain_text_is_a_click_away(self, qtbot):
        pane = self._pane(qtbot)
        pane.show_item(0, self._items()["1001"])
        pane.body_mode.setCurrentIndex(1)
        assert pane.body_stack.currentWidget() is pane.body_view
        assert "Pick a time" in pane.body_view.toPlainText()
        pane.body_mode.setCurrentIndex(0)
        assert pane.body_stack.currentWidget() is pane.rich_view

    def test_the_view_fetches_nothing(self, qtbot):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument
        from triage_table import MailView

        view = MailView()
        qtbot.addWidget(view)
        kind = QTextDocument.ResourceType.ImageResource
        assert view.loadResource(kind, QUrl("file:///etc/hosts")) is None
        assert view.loadResource(kind, QUrl("https://t.example/pixel.gif")) is None
        view.setHtml('<p>x</p><img src="file:///etc/hosts" width="200">')
        view.document().documentLayout().documentSize()
        # The document itself reads a local file when the view declines, so
        # it is asked directly.
        assert view.document().resource(kind, QUrl("file:///etc/hosts")) is None
        assert view.document().resource(kind, QUrl("https://t.example/a.png")) is None

    def test_an_embedded_image_still_draws(self, qtbot):
        import base64

        from PySide6.QtCore import QBuffer, QByteArray, QUrl
        from PySide6.QtGui import QColor, QImage, QTextDocument
        from triage_table import MailView

        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor("red"))
        raw = QByteArray()
        buffer = QBuffer(raw)
        buffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        url = "data:image/png;base64," + base64.b64encode(bytes(raw)).decode()
        view = MailView()
        qtbot.addWidget(view)
        got = view.document().resource(QTextDocument.ResourceType.ImageResource,
                                       QUrl(url))
        assert got is not None and not getattr(got, "isNull", lambda: False)()

    def test_a_tracking_pixel_leaves_no_trace(self, qtbot):
        pane = self._pane(qtbot)
        pane.show_item(0, self._items()["1007"])
        assert "careersdigest.example/open.gif" not in pane.rich_view.toHtml()
        assert "[image" not in pane.rich_view.toPlainText()


class TestTheWholeSubjectIsShown:
    @staticmethod
    def _item(subject):
        from dataclasses import replace

        import demo_data
        from config import Settings

        item = demo_data.demo_items(folders=Settings().folder_plan())[0]
        item.email = replace(item.email, subject=subject)
        return item

    def test_a_long_subject_gets_the_room_a_tall_pane_has(self, qtbot, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        qtbot.addWidget(pane)
        pane.resize(700, 640)
        pane.show()
        qapp.processEvents()
        pane.show_item(0, self._item("Re: Fwd: " + "a very long subject line " * 14))
        qapp.processEvents()
        needed = pane.header.heightForWidth(pane.header.width())
        assert needed > pane._header_lines(3), "the subject did not wrap enough to test"
        assert pane.header.maximumHeight() >= needed
        assert pane.header.height() >= needed - 1, (
            f"header {pane.header.height()} px for {needed} px of text")

    def test_a_short_pane_still_keeps_three_lines(self, qtbot, qapp):
        from triage_table import PreviewPane

        pane = PreviewPane()
        qtbot.addWidget(pane)
        pane.resize(700, pane.MIN_TALL)
        pane.show()
        qapp.processEvents()
        pane.show_item(0, self._item("a very long subject line " * 14))
        assert pane.header.maximumHeight() == pane._header_lines(3)


class TestFileIntoIsAList:
    """A folder is picked from the list, or named on request; the box is not
    a text field."""

    @staticmethod
    def _pane(qtbot):
        import demo_data
        from config import Settings
        from triage_table import PreviewPane

        pane = PreviewPane()
        qtbot.addWidget(pane)
        plan = Settings().folder_plan()
        pane.set_folder_choices(list(plan.all_folders))
        item = demo_data.demo_items(folders=plan)[0]
        pane.show_item(0, item)
        return pane, item

    def test_the_box_is_not_editable_and_ends_with_other(self, qtbot):
        pane, _item = self._pane(qtbot)
        assert not pane.folder_combo.isEditable()
        assert pane.folder_combo.itemText(pane.folder_combo.count() - 1) == pane.OTHER_FOLDER

    def test_other_asks_for_a_name_and_files_there(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        pane, item = self._pane(qtbot)
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("Sorted Mail/Receipts", True)))
        heard = []
        pane.overrideChanged.connect(lambda row, folder: heard.append(folder))
        pane.folder_combo.setCurrentIndex(pane.folder_combo.count() - 1)
        assert pane.folder_combo.currentText() == "Sorted Mail/Receipts"
        assert heard == ["Sorted Mail/Receipts"]

    def test_cancelling_goes_back_to_what_was_chosen(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QInputDialog

        pane, item = self._pane(qtbot)
        was = pane.folder_combo.currentText()
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("", False)))
        heard = []
        pane.overrideChanged.connect(lambda row, folder: heard.append(folder))
        pane.folder_combo.setCurrentIndex(pane.folder_combo.count() - 1)
        assert pane.folder_combo.currentText() == was
        assert not heard

    def test_a_rule_picks_its_folder_the_same_way(self, qtbot, monkeypatch):
        from PySide6.QtWidgets import QComboBox, QInputDialog

        import autoreply
        from settings_dialog import OTHER_FOLDER, ActionRow

        row = ActionRow(autoreply.Action("file_into", "Sorted Mail/Custom"),
                        folders=["Job Search/Interviews"])
        qtbot.addWidget(row)
        box = row._value_widget
        assert isinstance(box, QComboBox) and not box.isEditable()
        assert row._value_text() == "Sorted Mail/Custom"
        assert box.itemText(box.count() - 1) == OTHER_FOLDER
        monkeypatch.setattr(QInputDialog, "getText",
                            staticmethod(lambda *a, **k: ("Archive/2026", True)))
        box.setCurrentIndex(box.count() - 1)
        assert row._value_text() == "Archive/2026"



class TestThePicturesInAMessage:
    """With pictures wanted the view fetches each one the message shows and
    hands it to the sealed document; without, they are placeholders and
    nothing is asked for."""

    HTML = ('<p>Offer</p><img src="https://pictures.example/coin.png" alt="coin" '
            'width="40" height="40"><img src="https://t.example/open.gif" width="1" height="1">')

    @staticmethod
    def _png() -> bytes:
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QColor, QImage

        image = QImage(4, 4, QImage.Format.Format_RGB32)
        image.fill(QColor(200, 30, 30))
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        return bytes(buffer.data())

    def _pane(self, qtbot, monkeypatch, wanted=True):
        from dataclasses import replace

        from triage_table import MailView, PreviewPane

        asked = []
        monkeypatch.setattr(MailView, "_fetch", lambda self, url: asked.append(url))
        pane = PreviewPane()
        qtbot.addWidget(pane)
        pane.set_pictures(wanted)
        item = next(iter(TestTheMessageIsShownAsSent._items().values()))
        item.email = replace(item.email, body_html=self.HTML)
        pane.show_item(0, item)
        return pane, asked

    def test_each_picture_is_asked_for_once_and_no_pixel(self, qtbot, monkeypatch):
        pane, asked = self._pane(qtbot, monkeypatch)
        assert asked == ["https://pictures.example/coin.png"]
        assert pane.rich_view.pictures_wanted() == []
        assert "t.example" not in pane.rich_view._html

    def test_a_picture_that_arrives_is_drawn_in_place(self, qtbot, monkeypatch):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument

        pane, _asked = self._pane(qtbot, monkeypatch)
        view = pane.rich_view
        url = "https://pictures.example/coin.png"
        view.add_picture(url, self._png())
        found = view.document().resource(QTextDocument.ResourceType.ImageResource, QUrl(url))
        assert found is not None and not found.isNull()
        assert "coin.png" in view.document().toHtml()

    def test_a_late_picture_for_another_message_is_dropped(self, qtbot, monkeypatch):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument

        pane, _asked = self._pane(qtbot, monkeypatch)
        view = pane.rich_view
        view.add_picture("https://elsewhere.example/x.png", self._png())
        assert view.document().resource(
            QTextDocument.ResourceType.ImageResource,
            QUrl("https://elsewhere.example/x.png")) is None

    def test_a_broken_or_outsized_picture_is_ignored(self, qtbot, monkeypatch):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument

        pane, _asked = self._pane(qtbot, monkeypatch)
        view = pane.rich_view
        url = "https://pictures.example/coin.png"
        view.add_picture(url, b"not a picture")
        view.add_picture(url, b"\x89PNG" + b"\x00" * (view.PICTURE_MOST + 1))
        assert view.document().resource(
            QTextDocument.ResourceType.ImageResource, QUrl(url)) is None

    def test_unwanted_nothing_is_asked_for(self, qtbot, monkeypatch):
        pane, asked = self._pane(qtbot, monkeypatch, wanted=False)
        assert asked == []
        assert "[image: coin]" in pane.rich_view._html

    def test_the_document_still_fetches_nothing_itself(self, qtbot, monkeypatch):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QTextDocument

        pane, _asked = self._pane(qtbot, monkeypatch)
        document = pane.rich_view.document()
        for name in ("https://pictures.example/coin.png", "file:///etc/hosts"):
            assert document.loadResource(
                QTextDocument.ResourceType.ImageResource, QUrl(name)) is None

    def test_the_setting_goes_through_the_dialog(self, qtbot):
        from config import InMemoryCredentialStore, Settings
        from settings_dialog import SettingsDialog

        settings = Settings(icloud_email="you@icloud.example", show_images=False).normalized()
        dialog = SettingsDialog(settings, InMemoryCredentialStore())
        qtbot.addWidget(dialog)
        assert not dialog.images_check.isChecked()
        dialog.images_check.setChecked(True)
        assert dialog.collect().show_images
