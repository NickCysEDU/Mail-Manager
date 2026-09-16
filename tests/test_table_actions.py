"""The grid: bulk actions, the header menu, and what an empty view says."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from config import InMemoryCredentialStore, Settings
from gui import MainWindow
from models import (Category, Classification, EmailMessage, FolderPlan,
                    OtherCategory, TriageItem)


def item(uid="1", sender="somebody@acme.example", job=True,
         category=Category.INTERVIEW) -> TriageItem:
    return TriageItem(
        email=EmailMessage(uid=uid, sender_email=sender,
                           subject=f"Message {uid}",
                           sender_name="A Person"),
        classification=Classification(
            summary=f"Summary {uid}", is_job_related=job, category=category,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=0.95, reasoning="Because.", model="test"),
        folders=FolderPlan())


@pytest.fixture
def window(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    win = MainWindow(Settings(icloud_email="you@icloud.example"),
                     InMemoryCredentialStore())
    win.folder_plan = FolderPlan()
    yield win
    win.close()
    # close() only hides it. Without deleteLater the window and every
    # widget under it stay alive for the rest of the session, and
    # setStyleSheet restyles all of them on every theme change - which
    # is what made this file take minutes instead of seconds.
    win.deleteLater()


@pytest.fixture
def filled(window):
    window._has_scanned = True
    window.model.set_items([item(uid=str(i)) for i in range(5)])
    window._refresh_folder_choices()
    return window


def select(window, rows):
    """Select several rows at once, the way a shift-click would."""
    from PySide6.QtCore import QItemSelection, QItemSelectionModel
    selection = QItemSelection()
    last = window.model.columnCount() - 1
    for row in rows:
        selection.select(window.proxy.index(row, 0),
                         window.proxy.index(row, last))
    window.table.selectionModel().select(
        selection,
        QItemSelectionModel.SelectionFlag.ClearAndSelect
        | QItemSelectionModel.SelectionFlag.Rows)


class TestBulkActions:
    def test_selecting_several_rows_reports_all_of_them(self, filled):
        select(filled, [0, 1, 2])
        assert sorted(filled._selected_rows()) == [0, 1, 2]

    def test_ticking_a_selection_ticks_every_row(self, filled):
        filled.model.set_all_approved(False)
        filled._set_approved([0, 2, 4], True)
        assert [i.approved for i in filled.model.items] == [
            True, False, True, False, True]

    def test_unticking_a_selection_unticks_every_row(self, filled):
        filled.model.set_all_approved(True)
        filled._set_approved([1, 3], False)
        assert [i.approved for i in filled.model.items] == [
            True, False, True, False, True]

    def test_the_count_returned_is_what_actually_changed(self, filled):
        filled.model.set_all_approved(True)
        assert filled.model.set_approved([0, 1], True) == 0
        assert filled.model.set_approved([0, 1], False) == 2

    def test_refiling_a_selection_moves_every_row(self, filled):
        suggested = filled.model.items[0].suggested_folder
        target = next(f for f in filled._folder_choices() if f != suggested)
        filled._refile([0, 1, 2], target)
        assert [i.target_folder for i in filled.model.items[:3]] == [target] * 3

    def test_refiling_to_none_goes_back_to_the_suggestion(self, filled):
        suggested = filled.model.items[0].suggested_folder
        target = next(f for f in filled._folder_choices() if f != suggested)
        filled._refile([0], target)
        assert filled.model.items[0].override_folder == target
        filled._refile([0], None)
        assert filled.model.items[0].override_folder is None

    def test_refiling_in_bulk_is_learned_from(self, filled, tmp_path):
        import corrections
        # A folder the sorter would not have picked, or there is no
        # disagreement to learn from.
        suggested = filled.model.items[0].suggested_folder
        target = next(f for f in filled._folder_choices() if f != suggested)
        filled._refile([0, 1], target)
        memory = corrections.Memory.load(tmp_path / corrections.FILENAME)
        assert len(memory) == 2

    def test_copying_senders_deduplicates(self, filled):
        filled.model.set_items([
            item(uid="1", sender="a@acme.example"),
            item(uid="2", sender="a@acme.example"),
            item(uid="3", sender="b@acme.example")])
        filled._copy_senders(filled.model.items)
        assert QApplication.clipboard().text() == "a@acme.example\nb@acme.example"

    def test_the_menu_names_the_size_of_the_selection(self, filled):
        select(filled, [0, 1])
        labels = [a.text() for a in filled.build_table_menu().actions()]
        assert any("2 messages" in label for label in labels)

    def test_one_row_is_spoken_about_in_the_singular(self, filled):
        select(filled, [0])
        labels = [a.text() for a in filled.build_table_menu().actions()]
        assert any("this message" in label for label in labels)
        assert not any("1 messages" in label for label in labels)

    def test_the_menu_offers_every_folder(self, filled):
        select(filled, [0])
        submenus = [a.menu() for a in filled.build_table_menu().actions()
                    if a.menu() is not None]
        assert submenus, "there should be a File in... submenu"
        offered = {a.toolTip() for a in submenus[0].actions()}
        assert set(filled._folder_choices()) <= offered

    def test_there_is_no_menu_without_a_selection(self, filled):
        filled.table.clearSelection()
        assert filled.build_table_menu() is None

    def test_revert_is_offered_only_when_something_was_overridden(self, filled):
        select(filled, [0])
        revert = [a for a in filled.build_table_menu().actions()
                  if a.text() == "Use the suggested folder"][0]
        assert not revert.isEnabled()

        suggested = filled.model.items[0].suggested_folder
        filled._refile([0], next(f for f in filled._folder_choices()
                                 if f != suggested))
        select(filled, [0])
        revert = [a for a in filled.build_table_menu().actions()
                  if a.text() == "Use the suggested folder"][0]
        assert revert.isEnabled()


class TestTheHeaderMenu:
    def test_it_offers_a_reset(self, window):
        labels = [a.text() for a in window.build_header_menu().actions()]
        assert "Reset column widths" in labels
        assert "Show every column" in labels

    def test_it_also_lists_the_columns(self, window):
        labels = [a.text() for a in window.build_header_menu().actions()]
        assert len(labels) > 3

    def test_reset_restores_a_column_dragged_down(self, window):
        from triage_table import TriageTableModel
        column = TriageTableModel.COL_SENDER
        window.table.setColumnWidth(column, 1)   # clamped to the minimum
        assert window.table.columnWidth(column) < window.COLUMN_WIDTHS[column]
        window._reset_columns()
        assert window.table.columnWidth(column) == window.COLUMN_WIDTHS[column]

    def test_show_every_column_unhides_them(self, window):
        from triage_table import TriageTableModel
        column = TriageTableModel.COL_REASONING
        window.table.setColumnHidden(column, True)
        window.settings.hidden_columns = [column]
        window._show_all_columns()
        assert not window.table.isColumnHidden(column)
        assert window.settings.hidden_columns == []


class TestWhatAnEmptyGridSays:
    def test_before_any_scan(self, window):
        window._sync_table_stack()
        assert window.table_stack.currentIndex() == 0
        assert "Nothing scanned yet" in window.empty_label.text()
        assert not window.clear_filters_button.isVisible()

    def test_after_a_scan_that_found_nothing(self, window):
        window._has_scanned = True
        window.model.set_items([])
        window._sync_table_stack()
        assert "No messages in this window" in window.empty_label.text()

    def test_when_a_filter_hid_everything(self, filled):
        filled.search_edit.setText("nothing will ever match this")
        filled._sync_table_stack()
        assert filled.table_stack.currentIndex() == 0
        text = filled.empty_label.text()
        assert "Nothing matches" in text
        assert "All 5 message(s)" in text
        assert "nothing will ever match this" in text

    def test_it_names_every_filter_that_is_on(self, filled):
        filled.search_edit.setText("zzz")
        filled.proxy.set_hide_non_job(True)
        filled._sync_table_stack()
        text = filled.empty_label.text()
        assert "zzz" in text and "job mail only" in text

    def test_clearing_the_filters_brings_the_rows_back(self, filled):
        filled.search_edit.setText("zzz")
        filled._sync_table_stack()
        assert filled.proxy.rowCount() == 0

        filled._clear_filters()
        assert filled.proxy.rowCount() == 5
        assert filled.table_stack.currentIndex() == 1

    def test_the_button_only_appears_when_a_filter_is_the_reason(self, window, filled):
        filled.search_edit.setText("zzz")
        filled._sync_table_stack()
        assert filled.clear_filters_button.isVisibleTo(filled.table_stack)

        filled._clear_filters()
        filled.model.set_items([])
        filled._sync_table_stack()
        assert not filled.clear_filters_button.isVisibleTo(filled.table_stack)


class TestAccessibility:
    def test_every_main_control_has_a_name(self, window):
        for widget in (window.table, window.search_edit, window.scan_button,
                       window.apply_button, window.preview, window.log_view,
                       window.progress, window.category_filter,
                       window.show_combo, window.model_button,
                       window.account_button):
            assert widget.accessibleName(), widget

    def test_a_tooltip_doubles_as_the_description(self, window):
        assert window.version_label.accessibleDescription()

    def test_the_time_window_buttons_are_named(self, window):
        for button in window.window_buttons.values():
            assert button.accessibleName().startswith("Time window:")
