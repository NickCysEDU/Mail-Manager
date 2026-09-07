"""Table model, filter proxy and preview behaviour (headless Qt)."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QModelIndex, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from config import InMemoryCredentialStore, Settings  # noqa: E402
from gui import (  # noqa: E402
    LEAVE_IN_PLACE,
    _stored_date,
    ConfidenceDelegate,
    MainWindow,
    TriageFilterProxy,
    TriageTableModel,
    _confidence_rgb,
    _export_row,
    _html,
    _one_line,
)
from imap_engine import MoveReport  # noqa: E402
from models import (  # noqa: E402
    Category,
    Classification,
    Disposition,
    EmailMessage,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def make_item(uid="1", subject="Subject", sender="Dana Reyes", **overrides):
    classification_kwargs = overrides.pop("classification", {})
    classification = Classification(**{
        "summary": "First sentence. Second sentence.",
        "reasoning": "Reasoning text.",
        "model": "claude-haiku-4-5",
        "is_job_related": True,
        "category": Category.INTERVIEW,
        "other_category": OtherCategory.NOT_APPLICABLE,
        "confidence_score": 0.98,
        **classification_kwargs,
    })
    message = EmailMessage(
        uid=uid, subject=subject, sender_name=sender, sender_email="d@x.com",
        date=datetime(2026, 9, 4, 12, int(uid) if uid.isdigit() else 0, tzinfo=timezone.utc),
        body_text="Body text.",
    )
    return TriageItem(message, classification, FolderPlan(), **overrides)


@pytest.fixture
def model(qapp):
    model = TriageTableModel()
    model.set_items([
        make_item("1", "Interview invitation"),
        make_item("2", "Take-home test", classification={"category": Category.NEXT_STEPS}),
        make_item("3", "Low confidence", classification={"confidence_score": 0.4}),
        make_item("4", "Newsletter", classification={
            "is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
            "other_category": OtherCategory.NEWSLETTER, "confidence_score": 0.99,
        }),
    ])
    return model


class TestTableModel:
    def test_dimensions(self, model):
        assert model.rowCount() == 4
        assert model.columnCount() == len(TriageTableModel.HEADERS)
        assert model.rowCount(model.index(0, 0)) == 0  # no children

    def test_headers(self, model):
        headers = [
            model.headerData(c, Qt.Orientation.Horizontal, Qt.ItemDataRole.DisplayRole)
            for c in range(model.columnCount())
        ]
        assert headers[1:] == [
            "Sender", "Subject", "Received", "Summary", "Category",
            "Folder", "Confidence", "Reasoning", "Mailbox",
        ]

    def test_every_header_carries_an_explanatory_tooltip(self, model):
        for column in range(model.columnCount()):
            tip = model.headerData(column, Qt.Orientation.Horizontal,
                                   Qt.ItemDataRole.ToolTipRole)
            assert tip and len(tip) > 10, f"column {column} has no tooltip"

    def test_display_values(self, model):
        def cell(row, col):
            return model.data(model.index(row, col), Qt.ItemDataRole.DisplayRole)

        assert cell(0, TriageTableModel.COL_SENDER) == "Dana Reyes"
        assert cell(0, TriageTableModel.COL_SUBJECT) == "Interview invitation"
        assert cell(0, TriageTableModel.COL_CATEGORY) == "Interview"
        # The folder column shows the leaf; the full path is in the tooltip.
        assert cell(0, TriageTableModel.COL_FOLDER) == "Interview"
        assert cell(0, TriageTableModel.COL_CONFIDENCE) == "98%"
        assert cell(3, TriageTableModel.COL_CATEGORY) == "Other · Newsletters"
        # Non-job mail names the mailbox it is staying in.
        assert cell(3, TriageTableModel.COL_FOLDER) == "INBOX"

    def test_the_folder_tooltip_carries_the_full_path(self, model):
        tip = model.data(model.index(0, TriageTableModel.COL_FOLDER),
                         Qt.ItemDataRole.ToolTipRole)
        assert "Job Search/Interview" in tip

    def test_the_summary_column_is_not_truncated_in_the_data(self):
        """Truncation is the delegate's job; the model hands over everything."""
        long = "A summary that runs well past any sensible column width. " * 4
        subject = TriageTableModel()
        subject.set_items([make_item("1", classification={"summary": long})])
        cell = subject.data(subject.index(0, TriageTableModel.COL_SUMMARY),
                            Qt.ItemDataRole.DisplayRole)
        assert cell == long
        assert "…" not in cell

    def test_dates_are_human_readable(self):
        """Pinned to a fixed midday so the test cannot straddle midnight."""
        from datetime import datetime, timedelta, timezone

        now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
        subject = TriageTableModel()
        subject.set_items([make_item("1"), make_item("2"), make_item("3")])
        subject.items[0].email.date = now - timedelta(hours=2)
        subject.items[1].email.date = now - timedelta(days=1)
        subject.items[2].email.date = now - timedelta(days=400)
        values = [item.email.date_human(now) for item in subject.items]
        assert values[0].startswith("Today")
        assert values[1].startswith("Yesterday")
        assert ":" not in values[2]          # a year-old message needs no clock

    def test_invalid_index_returns_none(self, model):
        assert model.data(QModelIndex()) is None
        assert model.item_at(99) is None

    def test_checkbox_state_mirrors_approval(self, model):
        index = model.index(0, TriageTableModel.COL_SELECT)
        assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
        low = model.index(2, TriageTableModel.COL_SELECT)
        assert model.data(low, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked

    def test_rows_that_cannot_move_have_no_checkbox(self, model):
        index = model.index(3, TriageTableModel.COL_SELECT)  # leave-in-place newsletter
        assert model.data(index, Qt.ItemDataRole.CheckStateRole) is None
        assert not (model.flags(index) & Qt.ItemFlag.ItemIsUserCheckable)

    def test_toggling_a_checkbox_updates_the_item(self, model):
        index = model.index(0, TriageTableModel.COL_SELECT)
        assert model.setData(index, Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole)
        assert model.item_at(0).approved is False

    def test_setdata_is_rejected_on_other_columns(self, model):
        index = model.index(0, TriageTableModel.COL_SUBJECT)
        assert not model.setData(index, "x", Qt.ItemDataRole.CheckStateRole)

    def test_setdata_is_rejected_for_unmovable_rows(self, model):
        index = model.index(3, TriageTableModel.COL_SELECT)
        assert not model.setData(index, Qt.CheckState.Checked.value, Qt.ItemDataRole.CheckStateRole)

    def test_sort_keys_are_typed_not_stringly(self, model):
        confidence = model.data(
            model.index(0, TriageTableModel.COL_CONFIDENCE), Qt.ItemDataRole.UserRole
        )
        date = model.data(model.index(0, TriageTableModel.COL_DATE), Qt.ItemDataRole.UserRole)
        assert isinstance(confidence, float)
        assert isinstance(date, float)

    def test_tooltips_are_provided_where_text_is_clipped(self, model):
        for column in (TriageTableModel.COL_SUMMARY, TriageTableModel.COL_REASONING,
                       TriageTableModel.COL_CONFIDENCE, TriageTableModel.COL_SENDER):
            assert model.data(model.index(0, column), Qt.ItemDataRole.ToolTipRole)

    def test_select_all_movable(self, model):
        model.set_all_approved(True)
        assert [i.approved for i in model.items] == [True, True, True, False]

    def test_select_only_high_confidence(self, model):
        model.set_all_approved(False)
        model.set_all_approved(True, only_high_confidence=True)
        assert [i.approved for i in model.items] == [True, True, False, False]

    def test_reset_to_defaults(self, model):
        model.set_all_approved(True)
        model.reset_to_defaults()
        assert [i.approved for i in model.items] == [True, True, False, False]

    def test_override_selects_the_row(self, model):
        model.set_override(2, "Job Search/Interview")
        assert model.item_at(2).target_folder == "Job Search/Interview"
        assert model.item_at(2).approved is True

    def test_clearing_an_override_on_a_leave_row_deselects_it(self, model):
        model.set_override(3, "Sorted Mail/Newsletters")
        assert model.item_at(3).approved is True
        model.set_override(3, None)
        assert model.item_at(3).approved is False

    def test_apply_report_marks_rows(self, model):
        report = MoveReport(moved={"1": "Job Search/Interview"}, failed={"2": "COPY failed"})
        model.apply_report(report)
        assert model.item_at(0).moved is True
        assert model.item_at(0).approved is False
        assert model.item_at(1).move_error == "COPY failed"

    def test_summary(self, model):
        summary = model.summary()
        assert summary.total == 4
        assert summary.to_move == 2
        assert summary.needs_review == 1
        assert summary.leave_in_place == 1

    def test_empty_model_is_safe(self, qapp):
        empty = TriageTableModel()
        assert empty.rowCount() == 0
        empty.set_all_approved(True)
        empty.reset_to_defaults()
        assert empty.summary().total == 0


class TestFilterProxy:
    @pytest.fixture
    def proxy(self, model):
        proxy = TriageFilterProxy()
        proxy.setSourceModel(model)
        return proxy

    def test_passes_everything_by_default(self, proxy):
        assert proxy.rowCount() == 4

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("interview", 2),      # subject of row 1 and category of row 3
            ("invitation", 1),     # subject only
            ("dana", 4),           # sender of every row
            ("newsletter", 1),     # category label only
            ("needs review", 1),   # suggested folder only
            ("zzz", 0),
        ],
    )
    def test_text_filter_searches_every_visible_field(self, proxy, text, expected):
        proxy.set_text_filter(text)
        assert proxy.rowCount() == expected

    def test_text_filter_is_case_insensitive(self, proxy):
        proxy.set_text_filter("INTERVIEW INVITATION")
        assert proxy.rowCount() == 1

    def test_category_filter(self, proxy):
        proxy.set_category_filter("Next Steps")
        assert proxy.rowCount() == 1

    def test_hide_non_job(self, proxy):
        proxy.set_hide_non_job(True)
        assert proxy.rowCount() == 3

    def test_only_selected(self, proxy):
        proxy.set_only_selected(True)
        assert proxy.rowCount() == 2

    def test_filters_compose(self, proxy):
        proxy.set_hide_non_job(True)
        proxy.set_only_selected(True)
        proxy.set_text_filter("take-home")
        assert proxy.rowCount() == 1

    def test_sorting_by_confidence(self, proxy):
        proxy.sort(TriageTableModel.COL_CONFIDENCE, Qt.SortOrder.AscendingOrder)
        first = proxy.data(proxy.index(0, TriageTableModel.COL_CONFIDENCE), Qt.ItemDataRole.UserRole)
        last = proxy.data(
            proxy.index(proxy.rowCount() - 1, TriageTableModel.COL_CONFIDENCE),
            Qt.ItemDataRole.UserRole,
        )
        assert first < last


class TestConfidenceDelegate:
    def test_colour_bands(self):
        green = _confidence_rgb(0.98, 0.95)
        amber = _confidence_rgb(0.80, 0.95)
        red = _confidence_rgb(0.30, 0.95)
        assert green != amber != red
        assert green[1] > green[0]  # green channel dominant
        assert red[0] > red[1]      # red channel dominant

    def test_threshold_shifts_the_bands(self):
        assert _confidence_rgb(0.90, 0.85) == _confidence_rgb(0.98, 0.95)

    def test_size_hint(self, qapp):
        from PySide6.QtWidgets import QStyleOptionViewItem

        delegate = ConfidenceDelegate(0.95)
        assert delegate.sizeHint(QStyleOptionViewItem(), QModelIndex()).width() > 0


class TestStoredDate:
    def test_reads_a_persisted_iso_date(self, qapp):
        from PySide6.QtCore import QDate

        fallback = QDate(2000, 1, 1)
        assert _stored_date("2026-09-04T00:00:00+00:00", fallback) == QDate(2026, 9, 4)

    @pytest.mark.parametrize("raw", ["", "not a date", "2026-13-45"])
    def test_falls_back_when_unusable(self, qapp, raw):
        from PySide6.QtCore import QDate

        fallback = QDate(2000, 1, 1)
        assert _stored_date(raw, fallback) == fallback


class TestHelpers:
    def test_one_line_collapses_and_truncates(self):
        assert _one_line("a\n\n  b   c") == "a b c"
        assert _one_line("x" * 500).endswith("…")
        assert len(_one_line("x" * 500)) == 300

    def test_html_escapes(self):
        assert _html('<b>&"</b>') == "&lt;b&gt;&amp;&quot;&lt;/b&gt;"

    def test_export_row_is_flat_and_serialisable(self):
        import json

        row = _export_row(make_item("1"))
        json.dumps(row)
        assert row["category"] == "INTERVIEW"
        assert row["other_category"] == "NOT_APPLICABLE"
        assert row["disposition"] == "MOVE"
        assert row["target_folder"] == "Job Search/Interview"


class TestMainWindow:
    @pytest.fixture
    def window(self, qapp, tmp_path):
        settings = Settings(icloud_email="you@icloud.example")
        window = MainWindow(settings, InMemoryCredentialStore())
        yield window
        window.close()

    def test_startup_does_not_read_the_keychain(self, qapp, tmp_path):
        """Reading the Keychain also imports keyring, which is the single
        largest avoidable cost between launching and seeing a window."""
        settings = Settings(icloud_email="you@icloud.example", provider="gemini")
        store = InMemoryCredentialStore()
        reads = []
        original = store.get_provider_key
        store.get_provider_key = lambda name: (reads.append(name), original(name))[1]

        window = MainWindow(settings, store)
        try:
            assert reads == []
            # It happens once the window is up, and the answer is then reused.
            window._probe_api_keys()
            assert reads
            before = len(reads)
            window.store_has_key(settings.provider)
            window.store_has_key(settings.provider)
            assert len(reads) == before
        finally:
            window.close()

    def test_key_cache_is_dropped_when_settings_change(self, window):
        window._key_present["gemini"] = True
        window.forget_key_cache("gemini")
        assert "gemini" not in window._key_present
        window._key_present["gemini"] = True
        window.forget_key_cache()
        assert window._key_present == {}

    def test_calendar_popup_is_built_only_when_the_custom_range_is_used(self, window):
        """QDateEdit.setCalendarPopup builds a whole QCalendarWidget, which is
        about a tenth of a second a field. Nobody should pay that on launch."""
        assert window.start_date.calendarPopup() is False
        window._window_selected(TimeWindow.CUSTOM)
        assert window.start_date.calendarPopup() is True
        assert window.end_date.calendarPopup() is True

    def test_the_scan_window_is_spelled_out_with_am_and_pm(self, window):
        """It reads "covering 4 Sep, 7:12 AM to 5 Sep, 7:12 AM" before a scan."""
        import re

        window._window_selected(TimeWindow.LAST_24_HOURS)
        plain = re.sub(r"<[^>]+>", "", window.window_label.text())
        assert plain.startswith("covering ")
        assert plain.count("AM") + plain.count("PM") == 2
        assert ", " in plain

    def test_the_menu_bar_is_told_which_model_is_selected(self, window):
        window._switch_model("rules", "rules-v1")
        assert window.menu_bar._provider == "rules"
        assert window.menu_bar._model == "rules-v1"
        window._switch_ruleset("finance")
        assert window.menu_bar._ruleset == "finance"

    def test_the_mailbox_picker_stays_out_of_the_way_until_it_is_needed(self, window):
        """One mailbox is not a choice, so it gets neither a button nor a column."""
        assert window.account_button.isVisible() is False
        assert window.table.isColumnHidden(TriageTableModel.COL_ACCOUNT)

    def test_a_second_mailbox_brings_out_the_picker(self, qapp):
        from accounts import Account
        # Explicit hosts: the example domains are deliberately not real ones,
        # so nothing here can be guessed from the address.
        settings = Settings(icloud_email="you@icloud.example")
        settings.mailboxes = [
            Account(address="you@icloud.example", host="imap.mail.me.com"),
            Account(address="work@elsewhere.example", host="imap.elsewhere.example"),
        ]
        settings = settings.normalized()
        subject = MainWindow(settings, InMemoryCredentialStore())
        subject.show()
        try:
            assert subject.account_button.isVisible() is True
            assert not subject.table.isColumnHidden(TriageTableModel.COL_ACCOUNT)
            labels = [a.text() for a in subject.account_menu.actions() if a.text()]
            assert "All mailboxes" in labels
            # Picking one narrows the next scan to it.
            chosen = subject.settings.accounts[1]
            subject._select_accounts([chosen.id])
            assert [a.id for a in subject.settings.scan_accounts] == [chosen.id]
        finally:
            subject.close()

    def test_switching_profile_rebuilds_the_folder_plan(self, window):
        assert window.folder_plan.all_folders[0] == "Job Search"
        window._switch_profile("everyday")
        folders = window.folder_plan.all_folders
        assert folders[0] == "Sorted Mail"
        assert "Sorted Mail/Job Search" in folders
        # And non-job mail is now filed rather than left where it is.
        assert window.settings.routing.name == "FILE"

    def test_switching_profile_refiles_rows_already_on_screen(self, window):
        window.model.set_items([make_item("1"), make_item("4", classification={
            "is_job_related": False, "category": Category.UNCLASSIFIED_OTHER,
            "other_category": OtherCategory.NEWSLETTER, "confidence_score": 0.99})])
        before = window.model.items[0].target_folder
        window._switch_profile("everyday")
        after = window.model.items[0].target_folder
        assert before != after
        assert after.startswith("Sorted Mail")

    def test_constructs_and_starts_empty(self, window):
        assert window.model.rowCount() == 0
        assert window.apply_button.isEnabled() is False
        assert window.apply_button.text() == "Apply Approved Folder Moves"
        assert window.scan_button.text() in ("Scan && Analyze", "Reload Sample Data")
        assert window.table_stack.currentIndex() == 0   # the empty-state page

    def test_apply_button_reflects_the_selection(self, window):
        window.model.set_items([make_item("1"), make_item("2")])
        window._update_status()
        assert window.apply_button.isEnabled() is True
        assert window.apply_button.text() == "Apply 2 Approved Folder Moves"

    def test_singular_button_label(self, window):
        window.model.set_items([make_item("1")])
        window._update_status()
        assert "1 Approved Folder Move" in window.apply_button.text()

    def test_category_filter_is_rebuilt_from_results(self, window):
        window.model.set_items([
            make_item("1"),
            make_item("2", classification={"category": Category.NEXT_STEPS}),
        ])
        window._refresh_category_filter()
        labels = [window.category_filter.itemText(i) for i in range(window.category_filter.count())]
        assert labels == ["All categories", "Interview", "Next Steps"]

    def test_folder_choices_include_the_job_tree(self, window):
        window._refresh_folder_choices()
        choices = [
            window.preview.folder_combo.itemText(i)
            for i in range(window.preview.folder_combo.count())
        ]
        assert choices[0] == LEAVE_IN_PLACE
        assert "Job Search/Interview" in choices
        assert "Job Search/Received" in choices

    def test_topic_folders_appear_only_when_filing_is_enabled(self, window):
        window._refresh_folder_choices()
        before = window.preview.folder_combo.count()
        window.settings.non_job_routing = NonJobRouting.FILE.value
        window._refresh_folder_choices()
        assert window.preview.folder_combo.count() > before

    def test_custom_range_widgets_are_hidden_for_presets(self, window):
        window._sync_range_visibility()
        assert all(not w.isVisible() for w in window.range_widgets)

    def test_preview_updates_on_selection(self, window):
        window.model.set_items([make_item("1", subject="Interview invitation")])
        window._refresh_folder_choices()
        window.table.selectRow(0)
        window._selection_changed()
        assert "Interview invitation" in window.preview.header.text()
        assert window.preview.body_view.toPlainText()

    def test_preview_can_show_the_exact_model_input(self, window):
        window.model.set_items([make_item("1")])
        window._refresh_folder_choices()
        window.table.selectRow(0)
        window._selection_changed()
        window.preview.body_mode.setCurrentIndex(1)
        text = window.preview.body_view.toPlainText()
        assert text.startswith("<email>")
        assert "<subject>" in text

    def test_override_from_the_preview_reaches_the_model(self, window):
        window.model.set_items([make_item("1", classification={"confidence_score": 0.2})])
        window._refresh_folder_choices()
        window._override_changed(0, "Job Search/Interview")
        assert window.model.item_at(0).target_folder == "Job Search/Interview"

    def test_geometry_is_persisted_on_close(self, window, tmp_path):
        window.close()
        assert window.settings.window_geometry
        assert Settings.load().window_geometry == window.settings.window_geometry
