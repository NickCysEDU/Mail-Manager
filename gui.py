"""PySide6 user interface for iCloud Mail Job Triage."""

from __future__ import annotations

import csv
import json
import re
import logging
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QThread,
    QByteArray,
    QDate,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QFontMetrics,
    QKeySequence,
)
from PySide6.QtWidgets import (
    QFileDialog,
    QAbstractItemView,
    QStackedWidget,
    QApplication,
    QComboBox,
    QDateEdit,
    QDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QTableView,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import corrections
import llm_engine
import profiles
import providers
import rulesets
import scheduler
from config import (
    CredentialError,
    CredentialStore,
    Settings,
    log_dir,
)
from imap_engine import MovePlan, MoveReport
from models import (
    APP_DISPLAY_NAME,
    OTHER_COLOR,
    Disposition,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
    clock,
    resolve_window,
)
from flowlayout import FlowLayout, Spacer
from widgets import (  # noqa: F401 - re-exported; gui was the home of these
    ACCENT_AMBER, ACCENT_BLUE, ACCENT_GREEN, ACCENT_RED, AdaptiveLineEdit,
    SelectableMessages, VersionLabel, WrappingList, _abandon, _chip,
    _compact_button, _confidence_rgb, _draw_wrapped, _format_duration, _html,
    _is_dark, _mono_font, _one_line, _paint_button, _scrollable, _separator,
    _shade, _stored_date, _swatch, _tint, _wrap, _wrap_lines,
    describe, install_selectable_messages, menu_text,
    remove_selectable_messages,
    say, selectable, system_font,
    EMPTY_STATE, NOTHING_FOUND, SHOW_ALL, SHOW_JOB_ONLY, SHOW_SELECTED,
    _ABANDONED, _one_of)
import helpmode
import theme
from settings_dialog import (  # noqa: F401 - re-exported; gui was their home
    ActionRow, ConditionRow, ModelsDialog, SettingsDialog, _RuleRow)
from triage_table import (  # noqa: F401 - re-exported; gui was their home
    CategoryDelegate, ConfidenceDelegate, PreviewPane, TriageFilterProxy,
    TriageTableModel, WrapDelegate, _disposition_badge, _reasoning_html,
    category_color, LEAVE_IN_PLACE)
from menubar import MenuBarController
from welcome import SetupWizard
from workers import (AttachmentWorker,
    ReplyWorker,
    ApplyWorker,
    ScanOutcome,
    ScanWorker,
    build_move_plans,
    required_folders,
)

log = logging.getLogger(__name__)

#: How many filings to keep undoable. Deep enough that a session of ticking,
#: filing, looking again and filing again stays reversible; shallow enough
#: that the stack never describes mail from a scan two hours ago that has
#: since been touched elsewhere.
UNDO_DEPTH = 10

#: What each choice actually means, said plainly. The enum labels are short
#: enough to fit in a menu; these are what somebody needs to choose between
#: them, and they are the difference between a setting and a decision.
_ROUTING_HELP = {
    NonJobRouting.LEAVE:
        "Nothing that is not job mail is touched. It stays in your inbox and "
        "cannot be ticked - which is why those rows look inert.",
    NonJobRouting.REVIEW:
        "Non-job mail is gathered into Job Search / Needs Review so you can "
        "look through it in one place.",
    NonJobRouting.FILE:
        "Non-job mail is filed by topic into Sorted Mail - receipts with "
        "receipts, travel with travel - and can be ticked like anything else.",
}


@dataclass
class UndoBatch:
    """One completed filing, and how to put it back."""

    plans: List[MovePlan]
    when: datetime

class MainWindow(QMainWindow):
    """The application window."""

    def __init__(
        self,
        settings: Settings,
        store: CredentialStore,
        parent=None,
        demo: bool = False,
        dry_run: bool = False,
    ) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = store
        #: Populate from bundled sample data; never touch the network.
        self.demo = bool(demo)
        #: Scan for real, but refuse to move anything.
        self.dry_run = bool(dry_run)
        self.scan_worker: Optional[ScanWorker] = None
        self.apply_worker: Optional[ApplyWorker] = None
        self.reply_worker = None
        self.undo_worker = None
        #: The last batch of moves, so they can be reversed.
        #: One entry per completed filing, newest last. A stack rather than
        #: a single slot because filing is done in passes - tick the obvious
        #: ones, file, look again, file again - and undoing only the last pass
        #: leaves you stuck with the one before it.
        self._undo_stack: List[UndoBatch] = []
        #: Every background thread this window has started and not yet reaped.
        self._workers: List[QThread] = []
        self.folder_plan: Optional[FolderPlan] = settings.folder_plan()
        #: Set only by an explicit Quit, so closeEvent can tell "put this
        #: away" apart from "stop the app".
        self._quitting = False
        #: The Settings window while it is open, so Quit can deal with it.
        self._settings_dialog = None
        #: What the primary button currently does, so it can be rewired
        #: without disconnecting slots that were never attached.
        self._scan_button_action = None
        #: Whether the row height was chosen by hand. Until it is, it follows
        #: the density, which is what somebody picking "compact" expects.
        self._row_lines_chosen = False
        #: Whether the preview has been opened deliberately. The densest
        #: setting starts it closed, but should not keep closing it.
        self._preview_opened = False
        #: Mailboxes whose messages are shown, and whether "all" is in force.
        #: The two are kept apart so that unticking the last mailbox means an
        #: empty table rather than silently meaning every mailbox.
        self._view_accounts: List[str] = []
        self._view_all = True
        #: Which providers have a key in the Keychain. See store_has_key.
        self._key_present: Dict[str, bool] = {}
        self._prompt_cache: Dict[str, str] = {}
        self._prompt_engine: Optional[llm_engine.LLMEngine] = None
        self._prompt_engine_key: Optional[tuple] = None

        self.setWindowTitle(APP_DISPLAY_NAME)
        self.setMinimumSize(760, 520)

        self._build_ui()
        self._build_menus()
        self._rebuild_model_menu()
        self._restore_geometry()
        self._apply_mode()
        self._update_status()
        self._first_run_checked = False

        self.menu_bar = MenuBarController(self)
        self.menu_bar.openRequested.connect(self._reveal)
        self.menu_bar.settingsRequested.connect(lambda: self.open_settings())
        self.menu_bar.quickScanRequested.connect(self._quick_scan)
        self.menu_bar.scheduleChanged.connect(self.set_schedule)
        self.menu_bar.modelChanged.connect(self._switch_model)
        self.menu_bar.rulesetChanged.connect(self._switch_ruleset)
        self.menu_bar.quitRequested.connect(self.quit_app)
        # The window was built before the controller existed, so hand it the
        # current choice now rather than waiting for the first change.
        self._sync_menu_bar_model()
        helpmode.install(QApplication.instance(), self.settings.help_mode)
        self._apply_spacing()

        self.schedule_timer = QTimer(self)
        self.schedule_timer.setSingleShot(False)
        self.schedule_timer.timeout.connect(self._scheduled_scan)

        if self.settings.menu_bar_icon and not self.demo:
            self.menu_bar.show(self.settings.schedule_minutes)
        if self.settings.schedule_minutes and not self.demo:
            self._start_timer(self.settings.schedule_minutes)

    # -- construction ----------------------------------------------------
    def _build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(8)

        # The model/proxy pair must exist before the bars that filter it.
        self.model = TriageTableModel(self)
        self.model.selectionChanged.connect(self._update_status)
        self.proxy = TriageFilterProxy(self)
        self.proxy.setSourceModel(self.model)

        self.metrics_bar = QLabel()
        self.metrics_bar.setVisible(False)
        self.metrics_bar.setTextFormat(Qt.TextFormat.RichText)
        self.metrics_bar.setContentsMargins(10, 4, 10, 4)
        self.metrics_bar.setStyleSheet(
            "background: rgba(128,128,128,0.13); border-radius: 6px;"
        )

        self.banner = QLabel()
        self.banner.setVisible(False)
        self.banner.setWordWrap(True)
        self.banner.setTextFormat(Qt.TextFormat.RichText)
        self.banner.setContentsMargins(10, 6, 10, 6)
        layout.addWidget(self.banner)

        layout.addWidget(self._build_action_bar())
        layout.addWidget(self.metrics_bar)
        layout.addWidget(self._build_filter_bar())

        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.table.sortByColumn(TriageTableModel.COL_DATE, Qt.SortOrder.DescendingOrder)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)

        self._apply_density(self.settings.row_lines)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)      # otherwise the last column fights every resize
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(TriageTableModel.COL_SELECT, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TriageTableModel.COL_DATE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TriageTableModel.COL_CONFIDENCE, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(TriageTableModel.COL_SUMMARY, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(TriageTableModel.COL_ACCOUNT, QHeaderView.ResizeMode.Fixed)
        header.setTextElideMode(Qt.TextElideMode.ElideRight)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._header_menu)
        self._reset_columns()
        self._restore_hidden_columns()
        self._rebuild_columns_menu()
        self._rebuild_view_menu()

        # A blank grid on first launch tells the user nothing; this does.
        #: Whether a scan has finished this session. Distinguishes "nothing
        #: scanned yet" from "scanned, and there was nothing there".
        self._has_scanned = False
        self.empty_label = QLabel(EMPTY_STATE)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setTextFormat(Qt.TextFormat.RichText)
        self.empty_label.setWordWrap(True)
        self.clear_filters_button = QPushButton("Clear filters")
        self.clear_filters_button.setToolTip(
            "Show every message again: clear the search box, the category "
            "filter and the mailbox filter.")
        self.clear_filters_button.setVisible(False)
        self.clear_filters_button.clicked.connect(self._clear_filters)

        empty_page = QWidget()
        empty_layout = QVBoxLayout(empty_page)
        empty_layout.addStretch(1)
        empty_layout.addWidget(self.empty_label)
        empty_row = QHBoxLayout()
        empty_row.addStretch(1)
        empty_row.addWidget(self.clear_filters_button)
        empty_row.addStretch(1)
        empty_layout.addLayout(empty_row)
        empty_layout.addStretch(1)

        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(empty_page)         # index 0
        self.table_stack.addWidget(self.table)         # index 1
        self.model.modelReset.connect(self._sync_table_stack)
        # A filter emptying the view is just as much a reason to swap pages as
        # the model emptying, and only the proxy knows when that happens.
        self.proxy.rowsInserted.connect(self._sync_table_stack)
        self.proxy.rowsRemoved.connect(self._sync_table_stack)
        self.proxy.layoutChanged.connect(self._sync_table_stack)

        self.preview = PreviewPane()
        self.preview.overrideChanged.connect(self._override_changed)
        self.preview.attachmentsRequested.connect(self._open_attachments)
        self.preview.sortNonJobRequested.connect(
            lambda: self._switch_routing(NonJobRouting.FILE))

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(_mono_font())
        self.log_view.setVisible(self.settings.show_log_panel)

        # Two splitters rather than one. The inner pair is the table and the
        # preview, whose arrangement is a preference - beside each other on a
        # wide screen, stacked on a tall one - and the outer one is the log,
        # which is always along the bottom.
        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table_stack)
        self.splitter.addWidget(self.preview)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([420, 300])

        self.outer_splitter = QSplitter(Qt.Orientation.Vertical)
        self.outer_splitter.addWidget(self.splitter)
        self.outer_splitter.addWidget(self.log_view)
        self.outer_splitter.setStretchFactor(0, 5)
        self.outer_splitter.setSizes([720, 0])
        layout.addWidget(self.outer_splitter, 1)
        self._apply_preview_position(self.settings.preview_position)

        self.setCentralWidget(central)

        self.status_label = QLabel("Ready.")
        self.status_label.setMinimumWidth(120)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.usage_label = QLabel("")
        self.usage_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Which build this is, in the corner. A version number alone does not
        # identify one during development - every change between releases
        # carries the same one - so it is the commit that makes a bug report
        # answerable. Click it to copy the lot.
        self.version_label = VersionLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.usage_label)
        self.statusBar().addPermanentWidget(self.version_label)
        self.statusBar().setSizeGripEnabled(True)

        self._name_controls()

    def _name_controls(self) -> None:
        """Name every control for a screen reader.

        In one place rather than scattered through the layout code, because
        the useful question about accessibility labels is "is anything
        missing", and that is only answerable if they are all in a list.
        """
        described = {
            self.table: ("Message triage table",
                         "Every scanned message, one per row. Space ticks the "
                         "selected row; right-click for bulk actions."),
            self.search_edit: ("Search messages",
                               "Filters the table by sender, subject, summary "
                               "or folder."),
            self.category_filter: ("Filter by category", ""),
            self.show_combo: ("Filter by what to show", ""),
            self.scan_button: ("Scan and analyze", ""),
            self.apply_button: ("Apply approved folder moves", ""),
            self.clear_filters_button: ("Clear every filter", ""),
            self.progress: ("Scan progress", ""),
            self.model_button: ("Analysis backend", ""),
            self.account_button: ("Mailboxes to scan", ""),
            self.start_date: ("Custom range: first day", ""),
            self.end_date: ("Custom range: last day", ""),
            self.log_view: ("Activity log", ""),
            self.status_label: ("Status", ""),
            self.usage_label: ("Model usage and cost", ""),
            self.version_label: ("Build version",
                                 "Click to copy the version and commit."),
            self.preview: ("Message preview",
                           "The selected message, its reasoning, and the "
                           "folder it will go to."),
        }
        for widget, (name, hint) in described.items():
            describe(widget, name, hint)
        for button in self.window_buttons.values():
            describe(button, f"Time window: {button.text()}")

    #: Default column widths, also used by View -> Reset column widths.
    COLUMN_WIDTHS = {
        TriageTableModel.COL_SELECT: 34,
        TriageTableModel.COL_SENDER: 150,
        TriageTableModel.COL_SUBJECT: 230,
        TriageTableModel.COL_DATE: 108,
        TriageTableModel.COL_CATEGORY: 150,
        TriageTableModel.COL_FOLDER: 118,
        TriageTableModel.COL_CONFIDENCE: 84,
        # Summary is the column people read across, and it has the stretch, so
        # everything beside it is sized to leave it room. Reasoning is here in
        # one line and in full in the pane below, so it gives up the most.
        TriageTableModel.COL_REASONING: 170,
        TriageTableModel.COL_ACCOUNT: 175,
    }

    def _reset_columns(self) -> None:
        for column, width in self.COLUMN_WIDTHS.items():
            self.table.setColumnWidth(column, width)
        self._sync_account_column()

    def _sync_account_column(self) -> None:
        """The mailbox column earns its space only when there is a choice.

        A column the user hid stays hidden either way; this only decides the
        one they have not expressed an opinion about.
        """
        if not hasattr(self, "table"):
            return
        column = TriageTableModel.COL_ACCOUNT
        if column in self.settings.hidden_columns:
            self.table.setColumnHidden(column, True)
            return
        multi = self.settings.multi_account
        self.table.setColumnHidden(column, not multi)
        # It is the last column in the model, which puts it off the right edge
        # of a table this wide - a column you have to go looking for does not
        # answer "where did this come from?". Moved to the front visually; the
        # model's own indices are untouched, so nothing else has to care.
        header = self.table.horizontalHeader()
        wanted = 1 if multi else header.count() - 1
        current = header.visualIndex(column)
        if current != -1 and current != wanted:
            header.moveSection(current, wanted)
        if multi:
            # Wide enough for the longest address on screen. A column that
            # elides to "firstname.lastname@ic…" has dropped the one part
            # that says which mailbox it is.
            metrics = QFontMetrics(self.table.font())
            longest = max(
                (metrics.horizontalAdvance(i.email.mailbox_display)
                 for i in self.model.items if i.email.mailbox_display),
                default=0,
            )
            self.table.setColumnWidth(
                column, min(300, max(self.COLUMN_WIDTHS[column], longest + 24)))

    #: Below this the summary is a word and an ellipsis, which is no use to
    #: anybody. It is the column people read across, and it has the stretch.
    MIN_SUMMARY_WIDTH = 160

    def _heal_column_widths(self) -> None:
        """Undo a saved layout that leaves the summary too narrow to read.

        Column widths are remembered, which is right - somebody who widened a
        column meant it. But a layout saved on a narrower window, or before a
        column was added, can add up to more than the window has, and the
        stretch column is the one that gives way. Past a certain point that is
        not a layout anybody chose, so it goes back to the defaults.
        """
        summary = TriageTableModel.COL_SUMMARY
        if self.table.isColumnHidden(summary):
            return
        if self.table.columnWidth(summary) >= self.MIN_SUMMARY_WIDTH:
            return
        available = self.table.viewport().width()
        if available <= 0:
            return
        for column, width in self.COLUMN_WIDTHS.items():
            if not self.table.isColumnHidden(column):
                self.table.setColumnWidth(column, width)

    def _restore_hidden_columns(self) -> None:
        for column in self.settings.hidden_columns:
            if 1 <= column < self.model.columnCount():
                self.table.setColumnHidden(column, True)

    def _apply_density(self, lines: int) -> None:
        """Switch between one-line rows and wrapped multi-line rows."""
        lines = max(1, min(6, int(lines)))
        self.settings.row_lines = lines
        metrics = QFontMetrics(self.table.font())
        height = metrics.lineSpacing() * lines + 12
        header = self.table.verticalHeader()
        header.setDefaultSectionSize(height)
        # Rows that already exist keep whatever height they were given, so the
        # change would otherwise only show up on the next scan.
        for row in range(self.model.rowCount()):
            header.resizeSection(row, height)
        for column in (TriageTableModel.COL_SENDER, TriageTableModel.COL_SUBJECT,
                       TriageTableModel.COL_SUMMARY, TriageTableModel.COL_REASONING,
                       TriageTableModel.COL_FOLDER):
            self.table.setItemDelegateForColumn(column, WrapDelegate(lines, self.table))
        self.table.setItemDelegateForColumn(
            TriageTableModel.COL_CONFIDENCE,
            ConfidenceDelegate(self.settings.confidence_threshold, self.table),
        )
        self.table.setItemDelegateForColumn(
            TriageTableModel.COL_CATEGORY, CategoryDelegate(self.table)
        )
        if hasattr(self, "density_actions"):
            for value, action in self.density_actions.items():
                action.setChecked(value == lines)

    def _build_action_bar(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        row = FlowLayout(frame, margin=0, spacing=6, vertical_spacing=6)
        self.action_bar_layout = row

        self.window_buttons: Dict[TimeWindow, QToolButton] = {}
        for window in TimeWindow:
            button = QToolButton()
            # These four are read as one control, so they are sized as one:
            # the theme's button padding is meant for a lone button with a
            # sentence on it, and four of them side by side wasted enough
            # width to push Apply onto a row of its own.
            button.setProperty("segment", "true")
            button.setText(window.label)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.clicked.connect(lambda checked, w=window: self._window_selected(w))
            button.setToolTip(
                f"Read mail from the last {window.label.lower()}. Nothing is "
                "marked as read, and nothing moves until you tick it.")
            self.window_buttons[window] = button
            row.addWidget(button)
        self.window_buttons[self.settings.window].setChecked(True)

        self.window_label = QLabel()
        self.window_label.setToolTip("The period the next scan will cover.")

        # The calendar popup is not built here: setCalendarPopup constructs a
        # full QCalendarWidget, which is about a tenth of a second per field.
        # _sync_range_visibility turns it on the first time the fields show.
        self.start_date = QDateEdit()
        self.start_date.setDisplayFormat("d MMM yy")
        self.start_date.setToolTip("The first day to read, included.")
        self.end_date = QDateEdit()
        self.end_date.setDisplayFormat("d MMM yy")
        self.end_date.setToolTip("The last day to read, included.")
        today = QDate.currentDate()
        self.start_date.setDate(_stored_date(self.settings.custom_start, today.addDays(-7)))
        self.end_date.setDate(_stored_date(self.settings.custom_end, today))
        self.start_date.dateChanged.connect(self._refresh_window_label)
        self.end_date.dateChanged.connect(self._refresh_window_label)

        # The dates take the label's place rather than being added beside it.
        # Shown alongside, they put another 367 points into a row that is
        # already full, so picking "Custom" wrapped the toolbar onto a second
        # line and everything after it jumped.
        dates = QWidget()
        dates_row = QHBoxLayout(dates)
        dates_row.setContentsMargins(0, 0, 0, 0)
        dates_row.setSpacing(4)
        separator = QLabel("–")
        separator.setProperty("dim", "true")
        for field in (self.start_date, self.end_date):
            field.setSizePolicy(QSizePolicy.Policy.Fixed,
                                QSizePolicy.Policy.Preferred)
        dates_row.addWidget(self.start_date)
        dates_row.addWidget(separator)
        dates_row.addWidget(self.end_date)
        # The slot is as wide as the longest wording of the label, so without
        # this the two fields stretch to fill it and drift apart.
        dates_row.addStretch(1)
        self.range_widgets = [self.start_date, separator, self.end_date]

        self.range_stack = QStackedWidget()
        self.range_stack.addWidget(self.window_label)
        self.range_stack.addWidget(dates)
        # One width for both, so switching moves nothing either way.
        self.range_stack.setSizePolicy(QSizePolicy.Policy.Preferred,
                                       QSizePolicy.Policy.Fixed)
        row.addWidget(self.range_stack)

        row.addWidget(Spacer(16))

        self.progress = QProgressBar()
        self.progress.setMinimumWidth(180)
        self.progress.setMaximumWidth(320)
        self.progress.setTextVisible(True)
        self.progress.setVisible(False)
        row.addWidget(self.progress)

        # The model is the single most consequential setting, so it gets a
        # control in the window rather than only a page inside Settings.
        self.model_button = QToolButton()
        self.model_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.model_button.setToolTip("Switch the model backend (⌘M)")
        self.model_menu = QMenu(self)
        self.model_button.setMenu(self.model_menu)
        row.addWidget(self.model_button)

        # What gets sorted, and what happens to everything else. This used
        # to be a submenu inside the model button - a menu about which AI to
        # use - and a combo box on the Folders tab of Settings. Neither is
        # where somebody looks when they are staring at a row that will not
        # tick and wondering why.
        self.sorting_button = QToolButton()
        self.sorting_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sorting_menu = QMenu(self)
        self.sorting_button.setMenu(self.sorting_menu)
        row.addWidget(self.sorting_button)

        # Only worth the space once there is more than one mailbox.
        self.account_button = QToolButton()
        self.account_button.setToolTip(
            "Which mailboxes the next scan reads. Only shown when you have "
            "more than one.")
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_menu = QMenu(self)
        self.account_button.setMenu(self.account_menu)
        row.addWidget(self.account_button)

        self.scan_button = QPushButton("Scan && Analyze")
        # Width is pinned to the wider of its two labels so the toolbar does
        # not jump when it turns into Stop.
        self.scan_button.setMinimumWidth(
            self.scan_button.fontMetrics().horizontalAdvance("Scan & Analyze") + 34)
        self._set_scan_button(False)
        row.addWidget(self.scan_button)

        self.apply_button = QPushButton("Apply Approved Folder Moves")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_moves)
        _paint_button(self.apply_button, "confirm")
        row.addWidget(self.apply_button)

        self._sync_range_visibility()
        self._rebuild_account_menu()
        self._rebuild_sorting_menu()
        return frame

    # -- model switcher --------------------------------------------------
    def _rebuild_model_menu(self) -> None:
        """Every backend and model, one click away from the main window."""
        self.model_menu.clear()
        current = (self.settings.provider, self.settings.model)
        for name, label, _blurb in providers.provider_choices():
            spec = providers.provider_class(name)
            section = self.model_menu.addMenu(menu_text(label))
            for choice in spec.models:
                action = QAction(menu_text(choice.label), self)
                action.setCheckable(True)
                action.setChecked((name, choice.value) == current)
                rate = spec.pricing.get(choice.value)
                if spec.on_device:
                    action.setStatusTip("free - runs on this Mac")
                elif rate:
                    action.setStatusTip(f"${rate[0]:.2f} in / ${rate[1]:.2f} out per Mtok")
                action.triggered.connect(
                    lambda checked=False, p=name, m=choice.value: self._switch_model(p, m)
                )
                section.addAction(action)
            if spec.needs_api_key and not self.store_has_key(name, probe=False):
                section.addSeparator()
                missing = QAction("No API key stored - open Settings…", self)
                missing.triggered.connect(self.open_settings)
                section.addAction(missing)
        self.model_menu.addSeparator()
        rules_menu = self.model_menu.addMenu("Local rule set (field)")
        for name, label, blurb in rulesets.choices():
            action = QAction(menu_text(label), self)
            action.setCheckable(True)
            action.setChecked(name == self.settings.ruleset)
            action.setStatusTip(blurb)
            action.setToolTip(blurb)
            action.triggered.connect(lambda checked=False, r=name: self._switch_ruleset(r))
            rules_menu.addAction(action)
        self.model_menu.addSeparator()
        more = QAction("Model settings…", self)
        more.setShortcut(QKeySequence("Ctrl+M"))
        more.triggered.connect(lambda: self.open_settings(tab=1))
        self.model_menu.addAction(more)
        self._refresh_model_button()

    # -- what gets sorted -------------------------------------------------
    def _rebuild_sorting_menu(self) -> None:
        """Everything that decides where mail goes, in one menu.

        Three questions, in the order somebody actually asks them: what am I
        sorting, what happens to the rest, and which of "the rest" is worth a
        folder. They were previously in three different places, one of them a
        submenu of the model picker, and the third had no interface at all.
        """
        self.sorting_menu.clear()

        # addSection rather than a disabled action. A disabled action is
        # drawn exactly like a greyed-out option, so the heading read as a
        # choice nobody was allowed to make.
        self.sorting_menu.addSection("What to sort")
        for name, label, blurb in profiles.choices():
            action = QAction(menu_text(label), self)
            action.setCheckable(True)
            action.setChecked(name == self.settings.sort_profile)
            action.setStatusTip(blurb)
            action.setToolTip(blurb)
            action.triggered.connect(
                lambda checked=False, p=name: self._switch_profile(p))
            self.sorting_menu.addAction(action)

        self.sorting_menu.addSection("Everything that is not job mail")
        for member in NonJobRouting:
            action = QAction(menu_text(member.label), self)
            action.setCheckable(True)
            action.setChecked(member is self.settings.routing)
            action.setToolTip(_ROUTING_HELP.get(member, ""))
            action.setStatusTip(_ROUTING_HELP.get(member, ""))
            action.triggered.connect(
                lambda checked=False, r=member: self._switch_routing(r))
            self.sorting_menu.addAction(action)

        # The topic list only means anything when non-job mail is being
        # filed, so it is only offered then. An empty submenu that does
        # nothing is worse than no submenu.
        if self.settings.routing is NonJobRouting.FILE:
            self.sorting_menu.addSeparator()
            topics_menu = self.sorting_menu.addMenu("Which topics get a folder")
            chosen = {t for t in self.settings.chosen_topics}
            for topic in profiles.ALL_TOPICS:
                action = QAction(menu_text(topic.label), self)
                action.setCheckable(True)
                action.setChecked(topic in chosen)
                action.setToolTip(
                    f"Mail about {topic.label.lower()} gets a "
                    f"\u201c{topic.leaf}\u201d folder of its own. Unticked, it "
                    "goes to Other.")
                action.triggered.connect(
                    lambda checked, t=topic: self._toggle_topic(t, checked))
                topics_menu.addAction(action)
            topics_menu.addSeparator()
            every = topics_menu.addAction("Tick every topic")
            every.triggered.connect(lambda: self._set_topics(profiles.ALL_TOPICS))
            essentials = topics_menu.addAction("Just the essentials")
            essentials.triggered.connect(
                lambda: self._set_topics(profiles.ESSENTIAL_TOPICS))

        self.sorting_menu.addSeparator()
        more = QAction("Folder settings\u2026", self)
        more.triggered.connect(lambda: self.open_settings(tab=2))
        self.sorting_menu.addAction(more)
        self._refresh_sorting_button()

    def _refresh_sorting_button(self) -> None:
        """Say what the current arrangement is, on the button itself."""
        if not hasattr(self, "sorting_button"):
            return
        profile = self.settings.profile
        routing = self.settings.routing
        self.sorting_button.setText(menu_text(f"Sorting: {profile.label}"))
        rest = {
            NonJobRouting.LEAVE: "everything else is left where it is",
            NonJobRouting.REVIEW: "everything else goes to Needs Review",
            NonJobRouting.FILE: "everything else is filed by topic",
        }[routing]
        self.sorting_button.setToolTip(
            f"<b>{_html(profile.label)}</b><br>{_html(profile.blurb)}"
            f"<br><br>Right now, {rest}.<br><br>"
            "Click to change what gets sorted and where the rest goes.")

    @Slot(object)
    def _switch_routing(self, routing) -> None:
        """Change what happens to mail that is not job related."""
        if routing is self.settings.routing:
            return
        self.settings.non_job_routing = routing.value
        self._reroute_rows()
        self._rebuild_sorting_menu()
        self._append_log(f"Non-job mail: {routing.label}.")
        self._set_status(f"Non-job mail: {routing.label.lower()}.")

    def _toggle_topic(self, topic, wanted: bool) -> None:
        chosen = [t for t in self.settings.chosen_topics]
        if wanted and topic not in chosen:
            chosen.append(topic)
        elif not wanted and topic in chosen:
            chosen.remove(topic)
        self._set_topics(chosen)

    def _set_topics(self, topics) -> None:
        """Which topics earn a folder. Empty means the profile decides."""
        wanted = [t for t in profiles.ALL_TOPICS if t in set(topics)]
        self.settings.topics = [t.value for t in wanted]
        self._reroute_rows()
        self._rebuild_sorting_menu()

    def _reroute_rows(self) -> None:
        """Re-point every row without re-analyzing anything.

        Where a message goes is a routing decision, not a classification one,
        so changing it is instant: no mailbox, no model, no waiting. Making
        that obvious is half the point of putting the control in the window.
        """
        self.settings = self.settings.normalized()
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        self.folder_plan = self.settings.folder_plan(
            self.folder_plan.delimiter if self.folder_plan else "/")
        self._retarget_items()
        self._refresh_folder_choices()
        self._refresh_category_filter()
        self._selection_changed()

    # -- mailbox switcher ------------------------------------------------
    def _set_scan_button(self, busy: bool) -> None:
        """Scan when idle, Stop when not. One button, never disabled."""
        self.scan_button.setEnabled(True)
        wanted = self.stop_all if busy else self.start_scan
        if self._scan_button_action is wanted:
            return
        if self._scan_button_action is not None:
            self.scan_button.clicked.disconnect(self._scan_button_action)
        self._scan_button_action = wanted
        if busy:
            self.scan_button.setText("Stop")
            self.scan_button.setDefault(False)
            _paint_button(self.scan_button, "danger")
            self.scan_button.setToolTip(
                "Stop everything now: the mailbox fetch, the model requests and "
                "the local sorter, and close the connections they are using (⌘.)"
            )
            self.scan_button.clicked.connect(wanted)
        else:
            # Demo mode renames it, and that name should survive the morph.
            self.scan_button.setText(
                "Reload Sample Data" if self.demo else "Scan && Analyze")
            self.scan_button.setDefault(True)
            _paint_button(self.scan_button, "primary")
            self.scan_button.setToolTip(
                "Read the selected mailboxes over the chosen period and sort "
                "what is found."
            )
            self.scan_button.clicked.connect(wanted)

    def _rebuild_account_menu(self) -> None:
        """Which mailboxes the next scan reads: any of them, or all of them."""
        if not hasattr(self, "account_menu"):
            return
        self.account_menu.clear()
        mailboxes = self.settings.enabled_accounts
        self.account_button.setVisible(len(mailboxes) > 1)
        if len(mailboxes) <= 1:
            return

        every = QAction("All mailboxes", self)
        every.setCheckable(True)
        every.setChecked(self.settings.scans_every_mailbox)
        every.triggered.connect(lambda: self._select_accounts([]))
        self.account_menu.addAction(every)
        self.account_menu.addSeparator()

        # Tick as many as you like. Unticking the last one means all of them,
        # because scanning nothing is never what somebody meant.
        self._account_actions = {}
        for account in mailboxes:
            action = QAction(menu_text(f"{account.label}  ({account.address})"), self)
            action.setCheckable(True)
            action.setChecked(
                self.settings.scans_every_mailbox
                or account.id in self.settings.active_accounts
            )
            action.toggled.connect(
                lambda checked, a=account.id: self._toggle_account(a, checked))
            self.account_menu.addAction(action)
            self._account_actions[account.id] = action

        self.account_menu.addSeparator()
        manage = QAction("Mailboxes…", self)
        manage.triggered.connect(lambda: self.open_settings(tab=0))
        self.account_menu.addAction(manage)
        self._refresh_account_button()

    def _toggle_account(self, account_id: str, checked: bool) -> None:
        chosen = list(self.settings.active_accounts)
        if not chosen:
            # "All" was in force, so start from every mailbox and take one out.
            chosen = [a.id for a in self.settings.enabled_accounts]
        if checked and account_id not in chosen:
            chosen.append(account_id)
        elif not checked and account_id in chosen:
            chosen.remove(account_id)
        if len(chosen) >= len(self.settings.enabled_accounts):
            chosen = []
        self._select_accounts(chosen)

    def _refresh_account_button(self) -> None:
        chosen = self.settings.scan_accounts
        if self.settings.scans_every_mailbox:
            name = "All mailboxes"
        elif len(chosen) == 1:
            name = chosen[0].label
        else:
            name = f"{len(chosen)} mailboxes"
        # Named for what it does, since the viewer has a picker of its own and
        # two controls both reading "All mailboxes" is worse than none.
        self.account_button.setText(menu_text(f"Scan: {name}"))
        self.account_button.setToolTip(
            "Which mailboxes the next scan reads.\n"
            + ", ".join(a.address for a in chosen)
        )

    def _select_accounts(self, account_ids) -> None:
        self.settings.active_accounts = list(account_ids)
        self.settings = self.settings.normalized()
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        chosen = self.settings.scan_accounts
        self._append_log(
            "Next scan will read every mailbox."
            if self.settings.scans_every_mailbox
            else "Next scan will read " + ", ".join(a.label for a in chosen) + "."
        )
        self._rebuild_account_menu()
        self._sync_account_column()

    def store_has_key(self, provider: str, probe: bool = True) -> bool:
        """Whether a key is stored for `provider`.

        Answers are cached: reading the Keychain is a system call, and the
        first one also pays for importing ``keyring``. With `probe` false the
        Keychain is left alone and an unknown provider is assumed to be set up,
        which keeps that cost off the path between launch and a visible window.
        """
        cached = self._key_present.get(provider)
        if cached is not None:
            return cached
        if not probe:
            return True
        try:
            cached = bool(self.store.get_provider_key(provider))
        except CredentialError:
            cached = False
        self._key_present[provider] = cached
        return cached

    def _probe_api_keys(self) -> None:
        """Fill the key cache once the window is up, and show any warning then."""
        before = dict(self._key_present)
        for name, _label, _blurb in providers.provider_choices():
            if providers.provider_class(name).needs_api_key:
                self.store_has_key(name)
        if self._key_present != before:
            self._rebuild_model_menu()

    def forget_key_cache(self, provider: Optional[str] = None) -> None:
        """Drop what store_has_key remembered, after a key is added or removed."""
        if provider is None:
            self._key_present.clear()
        else:
            self._key_present.pop(provider, None)

    def _mailbox_passwords(self) -> Dict[str, str]:
        """One password per mailbox the next task will touch."""
        return {
            account.id: self.store.get_mailbox_password(account.address)
            for account in self.settings.scan_accounts
        }

    def _mailboxes_missing_a_password(self) -> List[str]:
        return [a.label for a in self.settings.scan_accounts
                if not self.store.get_mailbox_password(a.address)]

    def _sync_menu_bar_model(self) -> None:
        if hasattr(self, "menu_bar"):
            self.menu_bar.set_model(
                self.settings.provider, self.settings.model, self.settings.ruleset)

    def _refresh_model_button(self) -> None:
        if hasattr(self, "preview"):
            self.preview.set_backend_label(self.settings.provider_label.split(" (")[0])
        spec = self.settings.provider_class
        pretty = next(
            (c.label for c in spec.models if c.value == self.settings.model),
            self.settings.model,
        )
        warn = spec.needs_api_key and not self.store_has_key(
            self.settings.provider, probe=False)
        self.model_button.setText(menu_text(f"⚙︎  {pretty}") + ("  ⚠︎" if warn else ""))
        self.model_button.setToolTip(
            f"{spec.label} · {self.settings.model}\n"
            + ("No API key stored for this backend - click to fix.\n" if warn else "")
            + "Click to switch backend or model (⌘M)"
        )
        self._sync_menu_bar_model()

    def apply_appearance(self) -> None:
        """Repaint everything from the current appearance settings."""
        app = QApplication.instance()
        if app is None:
            return
        theme.apply(app, self.settings.appearance_mode, self.settings.contrast,
                    self.settings.readable, self.settings.density)
        self._apply_spacing()
        helpmode.install(app, self.settings.help_mode)
        if hasattr(self, "help_button") and \
                self.help_button.isChecked() != self.settings.help_mode:
            self.help_button.blockSignals(True)
            self.help_button.setChecked(self.settings.help_mode)
            self.help_button.blockSignals(False)
        self._apply_density(self.settings.effective_row_lines)
        self._reset_columns()
        self.table.viewport().update()

    def _apply_spacing(self) -> None:
        """Push the chosen density into the parts a stylesheet cannot reach.

        Margins between the toolbar rows, how much of the window the message
        preview takes, and whether it is open at all. A stylesheet can set
        padding inside a widget but not the space a layout leaves around it.
        """
        room = theme.density(self.settings.density)
        central = self.centralWidget()
        if central is not None and central.layout() is not None:
            central.layout().setContentsMargins(
                room.margin, room.margin, room.margin, room.margin)
            central.layout().setSpacing(room.spacing)
        for bar in ("action_bar_layout", "filter_bar_layout"):
            layout = getattr(self, bar, None)
            if layout is not None:
                layout.setSpacing(max(4, room.spacing))
                layout.setVerticalSpacing(max(3, room.spacing - 2))
        if hasattr(self, "metrics_bar"):
            self.metrics_bar.setContentsMargins(
                room.margin, max(2, room.cell_pad), room.margin, max(2, room.cell_pad))

        # Row height follows the density unless the user has said otherwise.
        self.settings.row_lines = self.settings.effective_row_lines
        if hasattr(self, "table"):
            self._apply_density(self.settings.row_lines)

        if hasattr(self, "splitter"):
            self._apply_preview_share(room)

    def _apply_preview_share(self, room) -> None:
        """Give the table everything the preview is not using.

        Skipped while the splitter has no height of its own, which is the case
        during construction; showEvent runs it again once it has.
        """
        total = self.splitter.height()
        if total <= 1:
            return
        if not room.preview_open and not self._preview_opened:
            self.splitter.setSizes([total, 0])
            return
        preview = int(total * room.preview_share)
        self.splitter.setSizes([total - preview, preview])

    def _switch_profile(self, name: str) -> None:
        """Change what gets a folder of its own, and rebuild the folder plan."""
        if name == self.settings.sort_profile:
            return
        chosen = profiles.get(name)
        self.settings.sort_profile = chosen.name
        self.settings.non_job_routing = chosen.non_job_routing.value
        self.settings = self.settings.normalized()
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        self.folder_plan = self.settings.folder_plan(
            self.folder_plan.delimiter if self.folder_plan else "/")
        self._append_log(f"Sorting profile: {chosen.label}. {chosen.blurb}")
        self._retarget_items()
        self._refresh_folder_choices()
        self._rebuild_model_menu()
        self._rebuild_sorting_menu()

    def _retarget_items(self) -> None:
        """Re-file the rows already on screen under the new folder plan."""
        items = list(self.model.items)
        if not items:
            return
        for item in items:
            item.folders = self.folder_plan
            item.non_job_routing = self.settings.routing
        self.model.set_items(items)
        self._rebuild_view_menu()
        self._update_status()

    def _switch_ruleset(self, name: str) -> None:
        """Pick the field-specific vocabulary the offline rules engine uses."""
        self.settings.ruleset = name
        self.settings = self.settings.normalized()
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        label = rulesets.get(name).label
        self._append_log(f"Local rule set: {label}.")
        self._rebuild_model_menu()
        worker = self.scan_worker
        if worker is not None and worker.isRunning():
            worker.switch_ruleset(name)
            self._set_status(f"Rule set switched to {label} for the rest of this scan.")

    def _switch_model(self, provider: str, model: str) -> None:
        self.settings.provider = provider
        self.settings.model = model
        self.settings = self.settings.normalized()
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)
        self._append_log(f"Model backend set to {self.settings.provider_label} · {model}.")
        self._rebuild_model_menu()

        worker = self.scan_worker
        if worker is not None and worker.isRunning():
            try:
                key = self.store.get_provider_key(provider)
            except CredentialError:
                key = ""
            if self.settings.needs_api_key and not key:
                QMessageBox.warning(
                    self, "API key needed",
                    f"{self.settings.provider_label} has no stored key, so the running "
                    "scan is continuing on the previous backend.",
                )
                return
            switched = worker.switch_model(
                provider=provider, model=model, api_key=key,
                base_url=self.settings.base_url, ruleset=self.settings.ruleset,
            )
            if switched:
                self._set_status(
                    f"Switched to {self.settings.provider_label} · {model} "
                    "for the rest of this scan."
                )
            else:
                self._set_status(
                    f"{self.settings.provider_label} · {model} will apply to the next scan."
                )
            return

        if self.settings.needs_api_key and not self.store_has_key(provider):
            QMessageBox.information(
                self, "API key needed",
                f"{self.settings.provider_label} needs an API key before it can be used.\n\n"
                "Settings opens on the Analysis tab, where you can paste one.",
            )
            self.open_settings(tab=1)

    def _build_filter_bar(self) -> QWidget:
        frame = QFrame()
        row = FlowLayout(frame, margin=0, spacing=6, vertical_spacing=6)
        self.filter_bar_layout = row

        self.search_edit = AdaptiveLineEdit(
            "Filter by sender, subject, summary or reasoning…",
            "Filter by sender, subject or summary…",
            "Filter messages…",
            "Filter…",
        )
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.proxy.set_text_filter)
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.search_edit)

        self.category_filter = QComboBox()
        self.category_filter.setToolTip(
            "Show only one category at a time. The list is built from what "
            "this scan actually found.")
        self.category_filter.addItem("All categories", None)
        self.category_filter.currentIndexChanged.connect(
            lambda: self.proxy.set_category_filter(self.category_filter.currentData())
        )
        row.addWidget(self.category_filter)

        # One "Show" menu instead of a row of competing checkboxes.
        self.show_combo = QComboBox()
        self.show_combo.setToolTip(
            "Narrow the table to job mail, or to the rows you have ticked.")
        self.show_combo.addItem("Show: everything", SHOW_ALL)
        self.show_combo.addItem("Show: job mail only", SHOW_JOB_ONLY)
        self.show_combo.addItem("Show: ticked only", SHOW_SELECTED)
        self.show_combo.setCurrentIndex(1 if self.settings.hide_non_job else 0)
        self.show_combo.currentIndexChanged.connect(self._show_filter_changed)
        row.addWidget(self.show_combo)
        self._show_filter_changed()

        # Reading one mailbox at a time is a different question from scanning
        # one at a time, so it gets its own control rather than reusing the
        # scan picker.
        self.view_button = QToolButton()
        self.view_button.setToolTip(
            "Which mailboxes' messages are shown in the table.")
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_menu = QMenu(self)
        self.view_button.setMenu(self.view_menu)
        row.addWidget(self.view_button)

        self.columns_button = QToolButton()
        self.columns_button.setText("Columns")
        self.columns_button.setToolTip(
            "Show or hide columns. Right-clicking the table header does this "
            "too, and can reset the widths.")
        self.columns_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.columns_menu = QMenu(self)
        self.columns_button.setMenu(self.columns_menu)
        row.addWidget(self.columns_button)
        # The table does not exist yet; both menus are filled in once it does.

        self.select_high_button = QPushButton("Tick high confidence")
        self.select_high_button.setToolTip(
            "Tick every message the analysis was confident about (⌘⇧A)"
        )
        self.select_high_button.clicked.connect(
            lambda: self.model.set_all_approved(True, only_high_confidence=True)
        )
        row.addWidget(self.select_high_button)

        self.deselect_button = QPushButton("Clear ticks")
        self.deselect_button.setToolTip(
            "Untick every message. Nothing has moved, so this only changes "
            "what Apply would do.")
        self.deselect_button.clicked.connect(lambda: self.model.set_all_approved(False))
        row.addWidget(self.deselect_button)

        row.addWidget(Spacer(8))
        self.help_button = helpmode.HelpButton()
        self.help_button.setChecked(self.settings.help_mode)
        self.help_button.toggled.connect(self._toggle_help)
        row.addWidget(self.help_button)

        return frame

    def _density_changed(self) -> None:
        """Say what the choice does, and show it straight away."""
        name = self.density_combo.currentData() or "comfortable"
        blurb = next((b for n, _l, b in theme.DENSITIES if n == name), "")
        self.density_note.setText(blurb)
        self._preview_appearance()

    def _toggle_help(self, on: bool) -> None:
        """Turn the explanations on or off, and remember which."""
        self.settings.help_mode = bool(on)
        helpmode.install(QApplication.instance(), on)
        QApplication.instance().setStyleSheet(
            QApplication.instance().styleSheet())     # repaint the button
        self._append_log(
            "Help is on. Hover anything for a moment and it will explain itself."
            if on else "Help is off."
        )
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    # -- reading one mailbox at a time -----------------------------------
    def _rebuild_view_menu(self) -> None:
        """Every linked mailbox, each one tickable, plus all and none.

        Shown whenever the app knows about a mailbox, even a single one: a
        control that appears and disappears depending on how many accounts you
        have is harder to find than one that is always in the same place.
        """
        if not hasattr(self, "view_menu") or not hasattr(self, "proxy"):
            return
        self.view_menu.clear()
        linked = self._linked_mailboxes()
        self.view_button.setVisible(bool(linked))
        if not linked:
            self.proxy.set_account_filter(())
            return

        select_all = QAction("Select all", self)
        select_all.setToolTip("Show messages from every mailbox")
        select_all.triggered.connect(
            lambda: self._set_view_accounts([a for a, _l, _n in linked]))
        self.view_menu.addAction(select_all)

        select_none = QAction("Select none", self)
        select_none.setToolTip("Hide every mailbox, leaving the table empty")
        select_none.triggered.connect(lambda: self._set_view_accounts([], empty=True))
        self.view_menu.addAction(select_none)
        self.view_menu.addSeparator()

        showing = self._showing_accounts()
        self._account_actions = {}
        for account_id, label, count in linked:
            action = QAction(menu_text(f"{label}   ({count})" if count else label), self)
            action.setCheckable(True)
            # Ticked before the signal is attached. setChecked emits toggled,
            # and toggled rebuilds this menu, so connecting first turns
            # rebuilding the menu into rebuilding it forever.
            action.setChecked(account_id in showing)
            action.toggled.connect(
                lambda checked, a=account_id: self._toggle_view_account(a, checked))
            self.view_menu.addAction(action)
            self._account_actions[account_id] = action
        self._refresh_view_button()

    def _linked_mailboxes(self):
        """(id, label, messages on screen) for every mailbox the app knows.

        Every configured account is listed whether or not this scan found
        anything in it, so the menu describes the app's accounts rather than
        the last scan's results. A mailbox that turned up messages the settings
        no longer mention is listed too, so nothing is unreachable.
        """
        counts: Dict[str, int] = {}
        for item in self.model.items:
            key = item.email.account_id or ""
            counts[key] = counts.get(key, 0) + 1

        listed = []
        seen = set()
        for account in self.settings.accounts:
            listed.append((account.id, account.describe(), counts.get(account.id, 0)))
            seen.add(account.id)
        for item in self.model.items:
            key = item.email.account_id or ""
            if key not in seen:
                seen.add(key)
                listed.append((key, item.email.account_label or "This mailbox",
                               counts.get(key, 0)))
        return listed

    def _showing_accounts(self) -> set:
        """Which mailbox ids are currently visible."""
        if self._view_all:
            return {a for a, _l, _n in self._linked_mailboxes()}
        return set(self._view_accounts)

    def _toggle_view_account(self, account_id: str, checked: bool) -> None:
        chosen = set(self._showing_accounts())
        chosen.add(account_id) if checked else chosen.discard(account_id)
        every = {a for a, _l, _n in self._linked_mailboxes()}
        self._set_view_accounts(sorted(chosen), empty=not chosen and every)

    def _set_view_accounts(self, account_ids, empty: bool = False) -> None:
        """Show these mailboxes. `empty` distinguishes none from all."""
        chosen = list(account_ids)
        every = [a for a, _l, _n in self._linked_mailboxes()]
        self._view_all = bool(not empty and (not chosen or set(chosen) >= set(every)))
        self._view_accounts = [] if self._view_all else chosen
        # An empty filter means "everything" to the proxy, so a deliberate
        # none is expressed as a filter nothing can match.
        if self._view_all:
            self.proxy.set_account_filter(())
        elif not self._view_accounts:
            self.proxy.set_account_filter(("\u0000none",))
        else:
            self.proxy.set_account_filter(self._view_accounts)
        self._rebuild_view_menu()
        self._update_status()

    def _refresh_view_button(self) -> None:
        linked = self._linked_mailboxes()
        showing = self._showing_accounts()
        if self._view_all:
            name = "All mailboxes" if len(linked) > 1 else "Mailbox"
        elif not showing:
            name = "No mailboxes"
        elif len(showing) == 1:
            only = next((l for a, l, _n in linked if a in showing), "One mailbox")
            name = only.split(" · ")[-1] if " · " in only else only
        else:
            name = f"{len(showing)} of {len(linked)} mailboxes"
        self.view_button.setText(menu_text(f"Show: {name}"))
        self.view_button.setToolTip(
            "Which mailboxes' messages are shown in the table. Separate from "
            "which ones get scanned - you can pull several in and read them "
            "one at a time."
        )

    # -- which columns are on screen --------------------------------------
    def _rebuild_columns_menu(self) -> None:
        if not hasattr(self, "columns_menu") or not hasattr(self, "table"):
            return
        self.columns_menu.clear()
        for column in range(1, self.model.columnCount()):
            header = self.model.HEADERS[column]
            action = QAction(menu_text(header), self)
            action.setCheckable(True)
            action.setChecked(not self.table.isColumnHidden(column))
            action.toggled.connect(
                lambda checked, c=column: self._set_column_visible(c, checked))
            self.columns_menu.addAction(action)
        self.columns_menu.addSeparator()
        reset = QAction("Show all columns", self)
        reset.triggered.connect(self._show_all_columns)
        self.columns_menu.addAction(reset)

    def _set_column_visible(self, column: int, visible: bool) -> None:
        """Record an explicit choice, rather than reading back the table.

        The mailbox column hides itself when there is only one mailbox. Reading
        the table's state back would file that away as something the user asked
        for, and they would never see it again once they added a second one.
        """
        self.table.setColumnHidden(column, not visible)
        if visible and self.table.columnWidth(column) <= 0:
            self.table.setColumnWidth(column, self.COLUMN_WIDTHS.get(column, 140))
        hidden = set(self.settings.hidden_columns)
        hidden.discard(column) if visible else hidden.add(column)
        self.settings.hidden_columns = sorted(hidden)

    def _show_all_columns(self) -> None:
        self.settings.hidden_columns = []
        for column in range(1, self.model.columnCount()):
            self.table.setColumnHidden(column, False)
            if self.table.columnWidth(column) <= 0:
                self.table.setColumnWidth(column, self.COLUMN_WIDTHS.get(column, 140))
        self._sync_account_column()
        self._rebuild_columns_menu()

    @Slot()
    def _show_filter_changed(self) -> None:
        mode = self.show_combo.currentData()
        self.proxy.set_hide_non_job(mode == SHOW_JOB_ONLY)
        self.proxy.set_only_selected(mode == SHOW_SELECTED)
        self.settings.hide_non_job = mode == SHOW_JOB_ONLY

    def _build_menus(self) -> None:
        menubar = self.menuBar()

        file_menu = menubar.addMenu("&File")
        scan_action = QAction("&Scan && Analyze", self)
        scan_action.setShortcut(QKeySequence("Ctrl+R"))
        scan_action.triggered.connect(self.start_scan)
        file_menu.addAction(scan_action)

        rescan_action = QAction("Scan && &Re-analyze Everything", self)
        # Not Ctrl+Shift+R, which is Reply. Qt resolves a duplicate shortcut
        # by firing neither, so a clash here would silently break both.
        rescan_action.setShortcut(QKeySequence("Ctrl+Alt+R"))
        rescan_action.setToolTip(
            "Scan without reusing any verdict kept from an earlier run.")
        rescan_action.triggered.connect(self.rescan_everything)
        file_menu.addAction(rescan_action)

        apply_action = QAction("&Apply Approved Folder Moves", self)
        apply_action.setShortcut(QKeySequence("Ctrl+Return"))
        apply_action.triggered.connect(self.apply_moves)
        file_menu.addAction(apply_action)

        self.undo_action = QAction("Undo Last Filing", self)
        self.undo_action.setShortcut(QKeySequence("Ctrl+Z"))
        self.undo_action.setEnabled(False)
        self.undo_action.setToolTip(
            "Move the messages from the last Apply back to the mailbox they "
            "came from."
        )
        self.undo_action.triggered.connect(self.undo_last_apply)
        file_menu.addAction(self.undo_action)

        reply_action = QAction("Run Reply &Rules…", self)
        # Not Ctrl+R: that is Scan, and Qt resolves a duplicate by firing
        # neither. Shift makes it the deliberate second action it is.
        reply_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        reply_action.setToolTip(
            "Run the auto-reply rules over what was scanned: draft, file, tick, "
            "flag or mark read, whatever they say. Nothing is ever sent."
        )
        reply_action.triggered.connect(self.draft_replies)
        file_menu.addAction(reply_action)

        self.stop_action = QAction("Stop &All Tasks", self)
        self.stop_action.setShortcut(QKeySequence("Ctrl+."))
        self.stop_action.setEnabled(False)
        self.stop_action.triggered.connect(self.stop_all)
        file_menu.addAction(self.stop_action)
        file_menu.addSeparator()

        export_csv = QAction("Export results as &CSV…", self)
        export_csv.triggered.connect(lambda: self._export("csv"))
        file_menu.addAction(export_csv)
        export_json = QAction("Export results as &JSON…", self)
        export_json.triggered.connect(lambda: self._export("json"))
        file_menu.addAction(export_json)
        file_menu.addSeparator()

        settings_action = QAction("&Settings…", self)
        settings_action.setShortcut(QKeySequence.StandardKey.Preferences)
        settings_action.setMenuRole(QAction.MenuRole.PreferencesRole)
        settings_action.triggered.connect(self.open_settings)
        file_menu.addAction(settings_action)

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        # Not QApplication.quit: that leaves without asking about a scan you
        # have not applied, and without noticing that Settings is open.
        quit_action.triggered.connect(self.quit_app)
        file_menu.addAction(quit_action)

        edit_menu = menubar.addMenu("&Edit")
        for label, slot, shortcut in (
            ("Select all movable", lambda: self.model.set_all_approved(True), "Ctrl+A"),
            ("Select high confidence only",
             lambda: (self.model.set_all_approved(False), self.model.set_all_approved(True, True)),
             "Ctrl+Shift+A"),
            ("Deselect all", lambda: self.model.set_all_approved(False), "Ctrl+D"),
            ("Reset to AI suggestions", self.model.reset_to_defaults, None),
        ):
            action = QAction(label, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            edit_menu.addAction(action)

        schedule_menu = menubar.addMenu("&Schedule")
        self.schedule_actions = {}
        for minutes, label in scheduler.INTERVALS:
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(minutes == self.settings.schedule_minutes)
            action.triggered.connect(lambda checked=False, m=minutes: self.set_schedule(m))
            schedule_menu.addAction(action)
            self.schedule_actions[minutes] = action
        schedule_menu.addSeparator()

        self.agent_action = QAction("Keep scanning when the app is closed", self)
        self.agent_action.setCheckable(True)
        self.agent_action.setChecked(self.settings.background_agent)
        self.agent_action.toggled.connect(self._toggle_agent)
        schedule_menu.addAction(self.agent_action)

        self.autofile_action = QAction("File high-confidence mail automatically", self)
        self.autofile_action.setCheckable(True)
        self.autofile_action.setChecked(self.settings.auto_file_background)
        self.autofile_action.toggled.connect(self._toggle_auto_file)
        schedule_menu.addAction(self.autofile_action)
        schedule_menu.addSeparator()

        status_action = QAction("Last background run", self)
        status_action.triggered.connect(self._show_agent_status)
        schedule_menu.addAction(status_action)

        view_menu = menubar.addMenu("&View")

        self.menu_bar_action = QAction("Show in the menu bar", self)
        self.menu_bar_action.setCheckable(True)
        self.menu_bar_action.setChecked(self.settings.menu_bar_icon)
        self.menu_bar_action.toggled.connect(self._toggle_menu_bar)
        view_menu.addAction(self.menu_bar_action)
        view_menu.addSeparator()

        find_action = QAction("&Find…", self)
        find_action.setShortcut(QKeySequence.StandardKey.Find)
        find_action.triggered.connect(self._focus_search)
        view_menu.addAction(find_action)

        clear_filters = QAction("Clear filters", self)
        clear_filters.setShortcut(QKeySequence("Esc"))
        clear_filters.triggered.connect(self._clear_filters)
        view_menu.addAction(clear_filters)
        view_menu.addSeparator()

        for index, window in enumerate(TimeWindow, start=1):
            action = QAction(window.label, self)
            action.setShortcut(QKeySequence(f"Ctrl+{index}"))
            action.triggered.connect(
                lambda checked=False, w=window: self._select_window(w)
            )
            view_menu.addAction(action)
        view_menu.addSeparator()

        density_menu = view_menu.addMenu("Row height")
        self.density_actions = {}
        for lines, label in ((1, "Compact - one line"), (2, "Cosy - two lines"),
                             (3, "Comfortable - three lines"), (5, "Roomy - five lines")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(lines == self.settings.row_lines)
            action.triggered.connect(lambda checked=False, n=lines: self._set_density(n))
            density_menu.addAction(action)
            self.density_actions[lines] = action

        preview_menu = view_menu.addMenu("Preview pane")
        self.preview_actions = {}
        for value, label in (("below", "Below the table"),
                             ("right", "Beside the table")):
            action = QAction(label, self)
            action.setCheckable(True)
            action.setChecked(value == self.settings.preview_position)
            action.triggered.connect(
                lambda checked=False, where=value: self.set_preview_position(where))
            preview_menu.addAction(action)
            self.preview_actions[value] = action

        reset_columns = QAction("Reset column widths", self)
        reset_columns.triggered.connect(self._reset_columns)
        view_menu.addAction(reset_columns)
        view_menu.addSeparator()

        self.log_action = QAction("Show activity &log", self)
        self.log_action.setCheckable(True)
        self.log_action.setShortcut(QKeySequence("Ctrl+L"))
        self.log_action.setChecked(self.settings.show_log_panel)
        self.log_action.toggled.connect(self._toggle_log)
        view_menu.addAction(self.log_action)

        open_logs = QAction("Open log folder", self)
        open_logs.triggered.connect(lambda: _reveal(log_dir()))
        view_menu.addAction(open_logs)

        help_menu = menubar.addMenu("&Help")
        # Named for what people come looking for. "Run setup again" reads as
        # something you would only do after a disaster; linking a second
        # mailbox is an ordinary Tuesday.
        setup = QAction("Add or Link &Mailboxes…", self)
        setup.setToolTip("The setup wizard: link another mailbox, or change "
                         "which folders get created.")
        setup.triggered.connect(self.run_setup)
        help_menu.addAction(setup)

        shortcuts = QAction("Keyboard &Shortcuts", self)
        shortcuts.setShortcut(QKeySequence("Ctrl+/"))
        shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts)

        about = QAction(f"About {APP_DISPLAY_NAME}", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    # -- window / settings ----------------------------------------------
    def _restore_geometry(self) -> None:
        if self.settings.window_geometry:
            self.restoreGeometry(QByteArray.fromBase64(self.settings.window_geometry.encode()))
        if self.settings.splitter_state:
            self.splitter.restoreState(QByteArray.fromBase64(self.settings.splitter_state.encode()))
        if self.settings.log_splitter_state:
            self.outer_splitter.restoreState(
                QByteArray.fromBase64(self.settings.log_splitter_state.encode()))
        if self.settings.table_state:
            self.table.horizontalHeader().restoreState(
                QByteArray.fromBase64(self.settings.table_state.encode())
            )
        # A saved header remembers the world as it was. Adding a second mailbox
        # would otherwise leave the Mailbox column hidden for good, because the
        # state saved when there was only one said to hide it.
        self._restore_hidden_columns()
        self._sync_account_column()
        for column in range(self.model.columnCount()):
            if not self.table.isColumnHidden(column) and self.table.columnWidth(column) <= 0:
                self.table.setColumnWidth(
                    column, self.COLUMN_WIDTHS.get(column, 140))
        self._heal_column_widths()

    def showEvent(self, event) -> None:  # noqa: N802
        """Run the first-run prompt the first time the window actually appears.

        A queued timer would be simpler but can fire after the window is gone,
        or between two unrelated operations; tying it to the show event means it
        happens exactly once, at the only moment it makes sense.
        """
        super().showEvent(event)
        # The splitter only has a height once the window has been laid out.
        QTimer.singleShot(0, self, lambda: self._apply_preview_share(
            theme.density(self.settings.density)))
        QTimer.singleShot(0, self, self._heal_column_widths)
        if not self._first_run_checked:
            self._first_run_checked = True
            QTimer.singleShot(0, self, self._first_run_check)
            QTimer.singleShot(0, self, self._probe_api_keys)

    def _unfinished_work(self) -> str:
        """Results that would be lost by quitting, phrased for a person.

        Deliberately only about a scan that has been approved and not applied.
        A task that is still running is stopped cleanly on the way out and
        costs nothing to start again, so it is not worth a question.
        """
        if self.demo or self.dry_run:
            return ""
        pending = sum(
            1 for item in self.model.items
            if item.approved and not item.moved
            and item.disposition is Disposition.MOVE
        )
        if not pending:
            return ""
        return (f"{pending} message{'s' if pending != 1 else ''} ticked and "
                "ready to file")

    def confirm_quit(self) -> bool:
        """Ask before throwing away a scan that has not been applied."""
        outstanding = self._unfinished_work()
        if not outstanding:
            return True
        answer = QMessageBox.question(
            self, "Quit Mail Manager?",
            f"You have {outstanding}.\n\n"
            "Nothing has been moved in your mailbox yet. Quitting now loses "
            "the scan, and you would have to run it again.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Discard,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Discard

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._quitting and not self.confirm_quit():
            self._quitting = False
            event.ignore()
            return
        if not self._quitting and self._hides_to_menu_bar():
            # The menu bar item is still there, so closing the window means
            # "put it away", not "stop working". Quit from the menu bar, the
            # app menu, or Cmd-Q to actually leave.
            event.ignore()
            self._put_away()
            return
        if not self._quitting and not self.confirm_quit():
            event.ignore()
            return
        self._close_attachment_window()
        self.shutdown()
        self._save_layout()
        super().closeEvent(event)

    def _put_away(self) -> None:
        """Hide the window, leaving full screen first if it is in it.

        Hiding a window that owns a macOS full-screen space leaves the space
        behind: the display stays on the empty desktop with no window and no
        way back, which reads as the app having crashed. Leaving full screen is
        animated, so the hide waits for it rather than racing it.
        """
        if self.isFullScreen():
            self.setWindowState(
                self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.showNormal()
            self._save_layout()
            QTimer.singleShot(700, self.hide)
            return
        self._save_layout()
        self.hide()

    def _save_layout(self) -> None:
        """Remember the window's shape, whether it is closing or just hiding.

        Not while it is full screen: restoring that on the next launch means
        starting into a full-screen space, which is rarely what was meant and
        is hard to get out of if anything then goes wrong.
        """
        if not self.isFullScreen():
            self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.splitter_state = bytes(self.splitter.saveState().toBase64()).decode()
        self.settings.log_splitter_state = bytes(
            self.outer_splitter.saveState().toBase64()).decode()
        self.settings.table_state = bytes(
            self.table.horizontalHeader().saveState().toBase64()
        ).decode()
        self.settings.show_log_panel = self.log_view.isVisible()
        self.settings.hide_non_job = self.show_combo.currentData() == SHOW_JOB_ONLY
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def shutdown(self) -> None:
        """Leave no thread attached to this window.

        Idempotent, and safe to call from ``aboutToQuit``. Anything that will
        not stop in time is detached rather than terminated - see
        :func:`_abandon` for why killing it would be worse.
        """
        self.schedule_timer.stop()
        self.menu_bar.hide()
        for worker in list(self._workers):
            if worker.isRunning() and not worker.stop(3000):
                _abandon(worker)
        self._workers.clear()

    def _apply_mode(self) -> None:
        """Show the banner and adjust the window title for demo / dry-run."""
        if self.demo:
            self.setWindowTitle(f"{APP_DISPLAY_NAME} - DEMO")
            self._set_banner(
                "#8a6d1f", "#fdf3d3",
                "<b>Demo mode.</b> These messages are bundled samples. No mailbox was "
                "opened, no API call was made, and nothing here can move real mail - "
                "“Apply” only marks the rows. Quit and relaunch without "
                "<code>--demo</code> to use your own inbox.",
            )
            self.scan_button.setText("Reload Sample Data")
        elif self.dry_run:
            self.setWindowTitle(f"{APP_DISPLAY_NAME} - DRY RUN")
            self._set_banner(
                "#1f4f8a", "#dbeafe",
                "<b>Dry run.</b> Your real mailbox is scanned and analyzed, but folder "
                "moves are disabled. Relaunch without <code>--dry-run</code> to file "
                "anything.",
            )

    def _set_banner(self, fg: str, bg: str, html: str) -> None:
        self.banner.setText(html)
        self.banner.setStyleSheet(
            f"color:{fg};background:{bg};border:1px solid {fg}44;border-radius:6px;"
        )
        self.banner.setVisible(True)

    # -- unattended scanning ---------------------------------------------
    def _start_timer(self, minutes: int) -> None:
        self.schedule_timer.stop()
        if minutes > 0:
            self.schedule_timer.start(int(minutes) * 60_000)

    @Slot(int)
    def set_schedule(self, minutes: int) -> None:
        """Set the automatic scan interval, and the background agent with it."""
        minutes = max(0, int(minutes))
        self.settings.schedule_minutes = minutes
        self._start_timer(minutes)
        message = scheduler.interval_label(minutes)

        if self.settings.background_agent:
            ok, detail = (
                scheduler.install_agent(minutes) if minutes
                else scheduler.remove_agent()
            )
            message = detail or message
            if not ok:
                QMessageBox.warning(self, "Background scanning", detail)
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

        self.menu_bar.set_schedule(minutes)
        self.menu_bar.refresh_status()
        for value, action in getattr(self, "schedule_actions", {}).items():
            action.setChecked(value == minutes)
        self._append_log(
            "Automatic scanning is off." if minutes == 0
            else f"Automatic scanning: {scheduler.interval_label(minutes).lower()}."
        )
        self._set_status(message)

    @Slot()
    def _scheduled_scan(self) -> None:
        if self.running_workers():
            return          # a scan is already in flight; skip this tick
        self._append_log("Starting the scheduled scan.")
        self.start_scan()

    @Slot(object)
    def _quick_scan(self, window) -> None:
        self._reveal()
        button = self.window_buttons.get(window)
        if button is not None:
            button.setChecked(True)
        self._window_selected(window)
        self.start_scan()

    @Slot()
    def _reveal(self) -> None:
        """Bring the window back, whether it was minimised, hidden or closed."""
        self.show()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self) -> None:
        """Leave for good, rather than hiding to the menu bar."""
        if not self._close_settings_first():
            return
        if not self.confirm_quit():
            return
        self._quitting = True
        if self.isFullScreen():
            self.setWindowState(
                self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.showNormal()
        self.close()
        QApplication.quit()

    def _close_settings_first(self) -> bool:
        """Deal with an open Settings window before leaving. False = stay.

        Settings is modal, so it owns the keyboard while it is up and Cmd-Q
        never reaches the main window. Rather than ignore the request, ask
        the question that is actually being asked - keep these changes or
        not - and carry it out.
        """
        dialog = getattr(self, "_settings_dialog", None)
        if dialog is None or not dialog.isVisible():
            return True
        answer = QMessageBox.question(
            dialog, "Save your settings?",
            "Settings is open. Do you want to save your changes before "
            "quitting?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if answer == QMessageBox.StandardButton.Cancel:
            return False
        if answer == QMessageBox.StandardButton.Save:
            dialog.accept()          # the same path the Save button takes
        else:
            dialog.reject()
        QApplication.processEvents()  # let exec() unwind before we close
        return True

    def _hides_to_menu_bar(self) -> bool:
        return (
            self.settings.close_to_menu_bar
            and self.settings.menu_bar_icon
            and self.menu_bar.visible()
        )

    def _toggle_agent(self, on: bool) -> None:
        """Keep scanning after the window closes, via a launchd agent."""
        self.settings.background_agent = bool(on)
        if on and self.settings.schedule_minutes:
            ok, detail = scheduler.install_agent(self.settings.schedule_minutes)
        elif on:
            ok, detail = True, "Pick an interval in the Schedule menu to start it."
        else:
            ok, detail = scheduler.remove_agent()
        try:
            self.settings.save()
        except OSError:
            pass
        self._append_log(detail)
        self._set_status(detail)
        if not ok:
            QMessageBox.warning(self, "Background scanning", detail)

    def _toggle_auto_file(self, on: bool) -> None:
        if on:
            confirm = QMessageBox(self)
            confirm.setWindowTitle("File automatically")
            confirm.setIcon(QMessageBox.Icon.Question)
            confirm.setText("Let background scans file mail without asking?")
            confirm.setInformativeText(
                "Only messages the app would have pre-ticked are filed: high "
                "confidence, job related, and never anything it sends to Needs "
                "Review. Everything else waits for you."
            )
            confirm.setStandardButtons(
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok
            )
            confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
            if confirm.exec() != QMessageBox.StandardButton.Ok:
                self.autofile_action.setChecked(False)
                return
        self.settings.auto_file_background = bool(on)
        try:
            self.settings.save()
        except OSError:
            pass

    def _show_agent_status(self) -> None:
        record = scheduler.read_status()
        QMessageBox.information(
            self, "Background scanning",
            f"{record.describe()}\n\n"
            f"Agent registered: {'yes' if scheduler.agent_installed() else 'no'}\n"
            f"Interval: {scheduler.interval_label(self.settings.schedule_minutes)}\n"
            f"Backend: {record.backend or self.settings.provider_label}",
        )

    def _toggle_menu_bar(self, on: bool) -> None:
        self.settings.menu_bar_icon = bool(on)
        if on:
            if not self.menu_bar.show(self.settings.schedule_minutes):
                QMessageBox.information(
                    self, "Menu bar",
                    "This Mac does not offer a menu bar area for apps to use.",
                )
        else:
            self.menu_bar.hide()
        try:
            self.settings.save()
        except OSError:
            pass

    def _first_run_check(self) -> None:
        if self.demo or self.settings.is_configured():
            return
        self.run_setup()

    @Slot()
    def run_setup(self) -> None:
        """The first-run wizard. Also reachable from the Help menu."""
        wizard = SetupWizard(self.settings, self.store, self)
        if wizard.exec() != QDialog.DialogCode.Accepted:
            return
        preserved = (
            self.settings.window_geometry,
            self.settings.splitter_state,
            self.settings.table_state,
        )
        self.settings = wizard.save()
        (self.settings.window_geometry, self.settings.splitter_state,
         self.settings.table_state) = preserved
        self.settings.save()
        self.folder_plan = self.settings.folder_plan()
        self.apply_appearance()
        self._rebuild_model_menu()
        self._toggle_menu_bar(self.settings.menu_bar_icon)
        self.set_schedule(self.settings.schedule_minutes)
        self._append_log("Setup finished.")

    @Slot()
    def open_settings(self, tab: int = 0) -> None:
        dialog = SettingsDialog(self.settings, self.store, self,
                                sample_items=self.model.items)
        if tab:
            dialog.tabs.setCurrentIndex(tab)
        # Remembered so that Quit can deal with it. Settings is modal, so a
        # Cmd-Q while it is open goes to the dialog and the app simply does
        # not leave - which reads as a hang, not as a refusal.
        self._settings_dialog = dialog
        try:
            outcome = dialog.exec()
        finally:
            self._settings_dialog = None
            # The dialog is parented to the window, so without this it lives
            # as long as the window does and every visit leaves another copy
            # behind - about three hundred and seventy widgets a time. That
            # is not only memory: apply_appearance sets a stylesheet on the
            # application, and Qt restyles every live widget when it does, so
            # each abandoned copy makes every later repaint slower.
            # deleteLater only queues the deletion, so the code below can
            # still read the dialog it just closed.
            dialog.deleteLater()
        if outcome != QDialog.DialogCode.Accepted:
            self.apply_appearance()      # undo any live preview
            return
        new_settings = dialog.collect()
        try:
            dialog.persist_credentials(new_settings)
        except CredentialError as exc:
            QMessageBox.warning(self, "Keychain", str(exc))
        # Keys may have just been added or cleared.
        self.forget_key_cache()

        preserved = (
            self.settings.window_geometry,
            self.settings.splitter_state,
            self.settings.table_state,
        )
        new_settings.window_geometry, new_settings.splitter_state, new_settings.table_state = preserved
        self.settings = new_settings
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not save settings: {exc}")

        self.folder_plan = self.settings.folder_plan()
        # Appearance was previewed while the dialog was open but never applied
        # when it was accepted, so anything without a preview - the row height
        # in particular - was collected, saved, and then ignored.
        self.apply_appearance()
        self.table.setItemDelegateForColumn(
            TriageTableModel.COL_CONFIDENCE,
            ConfidenceDelegate(self.settings.confidence_threshold, self.table),
        )
        self._rebuild_model_menu()
        self._rebuild_account_menu()
        self._rebuild_view_menu()
        self._sync_account_column()
        self._append_log("Settings saved.")
        self._update_status()

    # -- time window -----------------------------------------------------
    def _window_selected(self, window: TimeWindow) -> None:
        self.settings.last_window = window.name
        self._sync_range_visibility()

    def _sync_range_visibility(self) -> None:
        custom = self.settings.window is TimeWindow.CUSTOM
        if custom and not self.start_date.calendarPopup():
            for field in (self.start_date, self.end_date):
                field.setCalendarPopup(True)
        self.range_stack.setCurrentIndex(1 if custom else 0)
        self._refresh_window_label()

    def _refresh_window_label(self) -> None:
        """Spell out the period the next scan covers, before it runs."""
        if not hasattr(self, "window_label"):
            return
        try:
            start, end = self._current_window()
        except ValueError:
            self.window_label.setText("")
            return
        start, end = start.astimezone(), end.astimezone()
        same_year = start.year == end.year == datetime.now().year
        fmt = "%-d %b" if same_year else "%-d %b %Y"

        def spell(moment) -> str:
            return f"{moment.strftime(fmt)}, {clock(moment)}"

        # Three phrasings, longest first. The toolbar has to fit a row of
        # buttons, two menus and two actions, and this is the part that can
        # give up words without anything becoming unclear.
        brief = f"{start.strftime(fmt)} – {end.strftime(fmt)}"
        wordings = (
            f"<span style='opacity:0.7'>covering</span> <b>{spell(start)}</b> "
            f"<span style='opacity:0.7'>to</span> <b>{spell(end)}</b>",
            f"<span style='opacity:0.7'>covering</span> <b>{brief}</b>",
            f"<b>{brief}</b>",
        )
        self.window_label.setTextFormat(Qt.TextFormat.RichText)
        self._window_wordings = wordings
        self.window_label.setText(wordings[0])
        self.window_label.setToolTip(
            f"The next scan covers {spell(start)} to {spell(end)}.")
        self._fit_window_label()
        self.window_label.setTextFormat(Qt.TextFormat.RichText)

    def _fit_window_label(self) -> None:
        """Pick the longest wording that fits beside everything else."""
        wordings = getattr(self, "_window_wordings", ())
        if not wordings or not hasattr(self, "action_bar_layout"):
            return
        bar = self.action_bar_layout.geometry().width()
        if bar <= 0:
            return
        others = 0
        for index in range(self.action_bar_layout.count()):
            item = self.action_bar_layout.itemAt(index)
            widget = item.widget()
            # The slot the label lives in is what we are measuring for, so
            # it must not also count as something to fit around.
            if (widget is None or widget.isHidden()
                    or widget is getattr(self, "range_stack", self.window_label)):
                continue
            others += item.sizeHint().width() + self.action_bar_layout.spacing()
        room = bar - others
        metrics = self.window_label.fontMetrics()
        for wording in wordings:
            plain = re.sub(r"<[^>]+>", "", wording)
            if metrics.horizontalAdvance(plain) <= room or wording is wordings[-1]:
                if self.window_label.text() != wording:
                    self.window_label.setText(wording)
                return


    def _current_window(self):
        window = self.settings.window
        if window is not TimeWindow.CUSTOM:
            return resolve_window(window)
        start = self.start_date.date().toPython()
        end = self.end_date.date().toPython()
        start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
        end_dt = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
        self.settings.custom_start = start_dt.isoformat()
        self.settings.custom_end = end_dt.isoformat()
        return resolve_window(TimeWindow.CUSTOM, custom_start=start_dt, custom_end=end_dt)

    # -- scanning --------------------------------------------------------
    @Slot()
    def rescan_everything(self) -> None:
        """Scan without reusing anything kept from a previous run.

        The escape hatch for the case the cache cannot detect on its own:
        something changed on the provider's side that the recipe hash has no
        way of seeing.
        """
        self._begin_scan(reuse_verdicts=False)

    @Slot()
    def start_scan(self) -> None:
        # No parameters, deliberately. This is connected to clicked and to
        # triggered, both of which emit a bool - which would arrive as the
        # first positional argument and quietly turn the cache off.
        self._begin_scan()

    def _begin_scan(self, reuse_verdicts: Optional[bool] = None) -> None:
        if self._busy():
            return
        if self.demo:
            self._load_demo_data()
            return
        if not self.settings.is_configured():
            self.open_settings()
            if not self.settings.is_configured():
                return
        try:
            passwords = self._mailbox_passwords()
            missing = self._mailboxes_missing_a_password()
            api_key = self.store.get_provider_key(self.settings.provider)
        except CredentialError as exc:
            QMessageBox.critical(self, "Keychain", str(exc))
            return
        if missing:
            QMessageBox.warning(
                self, "Missing password",
                "No app password is stored for "
                + ", ".join(missing)
                + ". Add one in Settings.",
            )
            self.open_settings()
            return
        if self.settings.needs_api_key and not api_key:
            QMessageBox.warning(
                self, "Missing API key",
                f"No {self.settings.provider_label} API key is stored.\n\n"
                "Add one in Settings, or switch to a backend that needs no key "
                "(On this Mac, or Local rules) from the ⚙︎ button.",
            )
            self.open_settings(tab=1)
            return

        start, end = self._current_window()
        self._prompt_cache.clear()
        self.preview.clear()
        self._set_busy(True, "Scanning…")
        self._append_log(
            f"Scanning {self.settings.source_mailbox} from "
            f"{start.astimezone():%Y-%m-%d %H:%M} to {end.astimezone():%Y-%m-%d %H:%M}."
        )

        self.scan_worker = ScanWorker(
            settings=self.settings,
            mailbox_password=passwords,
            api_key=api_key,
            window_start=start,
            window_end=end,
            parent=self,
            reuse_verdicts=reuse_verdicts,
        )
        self._register(self.scan_worker)
        self.scan_worker.progress.connect(self._on_progress)
        self.scan_worker.metrics.connect(self._on_metrics)
        self.scan_worker.log_message.connect(self._append_log)
        self.scan_worker.failed.connect(self._on_failed)
        self.scan_worker.finished_ok.connect(self._on_scan_done)
        self.scan_worker.finished.connect(lambda: self._set_busy(False))
        self.scan_worker.start()

    def _load_demo_data(self) -> None:
        """Populate the window from bundled samples. No I/O of any kind."""
        import demo_data

        plan = self.settings.folder_plan()
        self.folder_plan = plan
        self._prompt_cache.clear()
        self.preview.clear()
        items = demo_data.demo_items(
            folders=plan,
            threshold=self.settings.confidence_threshold,
            non_job_routing=self.settings.routing,
            auto_approve_non_job=self.settings.auto_approve_non_job,
        )
        self.model.set_items(items)
        self._refresh_category_filter()
        self._refresh_folder_choices()
        self.usage_label.setText("demo data · no API calls · $0.00")
        self._append_log(f"Loaded {len(items)} sample message(s).")
        self._select_first_row()
        self._update_status()

    @Slot(object)
    def _on_scan_done(self, outcome: ScanOutcome) -> None:
        if outcome.folder_plan is not None:
            self.folder_plan = outcome.folder_plan
        self._has_scanned = True
        self.model.set_items(outcome.items)
        self._view_accounts = []
        self._view_all = True
        self._sync_account_column()
        self._rebuild_view_menu()
        self._refresh_category_filter()
        self._refresh_folder_choices()
        self.usage_label.setText(outcome.usage_text)

        if outcome.created_folders:
            self._append_log("Created: " + ", ".join(outcome.created_folders))
        for warning in outcome.warnings:
            self._append_log(f"⚠︎ {warning}")

        self.metrics_bar.setVisible(False)
        summary = self.model.summary()
        if not outcome.items:
            self._set_status("No messages found in this window.")
        else:
            self._set_status(summary.describe())
            self._select_first_row()

        if outcome.warnings:
            QMessageBox.information(self, "Scan notes", "\n\n".join(outcome.warnings))
        self._update_status()

        # “Run these rules after a scan” means exactly that. Queued rather than
        # called, so this handler finishes and the table is on screen before
        # the rules start touching it.
        if outcome.items and self.settings.replies_armed:
            QTimer.singleShot(0, lambda: self.draft_replies(prompted=False))

    # -- applying --------------------------------------------------------
    @Slot()
    def undo_last_apply(self) -> None:
        """Move the most recent batch back where it came from."""
        if self._busy() or not self._undo_stack:
            return
        batch = self._undo_stack[-1]
        count = len(batch.plans)
        remaining = len(self._undo_stack) - 1
        question = (f"Move {count} message{'s' if count != 1 else ''} back to "
                    "the mailbox they came from?")
        if remaining:
            question += (f"\n\nThis is the most recent of {len(self._undo_stack)} "
                         f"filings; {remaining} earlier "
                         f"{'one' if remaining == 1 else 'ones'} can be undone "
                         "after it.")
        if QMessageBox.question(
            self, "Undo filing", question,
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            passwords = self._mailbox_passwords()
        except CredentialError as exc:
            QMessageBox.critical(self, "Keychain", str(exc))
            return

        self._undoing = batch
        self._set_busy(True, "Putting messages back…")
        self.undo_worker = ApplyWorker(
            settings=self.settings, mailbox_password=passwords,
            plans=batch.plans, extra_folders=(), parent=self,
        )
        self._register(self.undo_worker)
        self.undo_worker.progress.connect(self._on_progress)
        self.undo_worker.log_message.connect(self._append_log)
        self.undo_worker.failed.connect(self._on_failed)
        self.undo_worker.finished_ok.connect(self._on_undo_done)
        self.undo_worker.start()

    @Slot(object)
    def _on_undo_done(self, report: MoveReport) -> None:
        self._set_busy(False)
        batch = getattr(self, "_undoing", None)
        self._undoing = None
        if batch is not None and batch in self._undo_stack:
            self._undo_stack.remove(batch)
        self._sync_undo_action()
        message = f"Put {report.moved_count} message(s) back."
        if report.failed:
            message += f" {report.failed_count} could not be moved back."
        self._append_log(message)
        QMessageBox.information(self, "Undo filing", message)
        self._update_status(message)

    def _sync_undo_action(self) -> None:
        """Say what pressing undo would actually do."""
        if not hasattr(self, "undo_action"):
            return
        self.undo_action.setEnabled(bool(self._undo_stack))
        if not self._undo_stack:
            self.undo_action.setText("Undo Last Filing")
            self.undo_action.setToolTip(
                "Nothing has been filed yet in this session.")
            return
        batch = self._undo_stack[-1]
        count = len(batch.plans)
        self.undo_action.setText(
            f"Undo Filing of {count} Message{'s' if count != 1 else ''}")
        depth = len(self._undo_stack)
        tip = f"Filed at {batch.when:%H:%M}."
        if depth > 1:
            tip += f" {depth - 1} earlier filing(s) can be undone after it."
        self.undo_action.setToolTip(tip)

    @Slot()
    def draft_replies(self, prompted: bool = True) -> None:
        """Run the reply rules over what was scanned.

        ``prompted`` is False when a scan started this off rather than a
        person, and it keeps the result in the status line instead of a box.
        """
        self._replies_prompted = prompted
        if self._busy():
            return
        if not self.settings.replies_armed:
            QMessageBox.information(
                self, "Auto reply is off",
                "No reply rules are switched on.\n\nSettings → Auto Reply has "
                "ready-made rules to start from, and you can build your own out "
                "of any conditions you like. Tick the ones you want and turn on "
                "\u201cRun these rules after a scan\u201d.",
            )
            self.open_settings(tab=3)
            return
        items = [i for i in self.model.items if not i.classification.error]
        if not items:
            QMessageBox.information(self, "Nothing to reply to",
                                    "Run a scan first.")
            return
        try:
            passwords = self._mailbox_passwords()
            api_key = self.store.get_provider_key(self.settings.provider)
        except CredentialError as exc:
            QMessageBox.critical(self, "Keychain", str(exc))
            return

        self._set_busy(True, "Running reply rules…")
        self.reply_worker = ReplyWorker(
            settings=self.settings, mailbox_password=passwords,
            api_key=api_key, items=items, parent=self,
        )
        self._register(self.reply_worker)
        self.reply_worker.progress.connect(self._on_progress)
        self.reply_worker.log_message.connect(self._append_log)
        self.reply_worker.failed.connect(self._on_failed)
        self.reply_worker.finished_ok.connect(self._on_rules_run)
        self.reply_worker.start()

    @Slot(object)
    def _on_rules_run(self, run) -> None:
        """Show what the rules did, and put their table changes on screen."""
        self._set_busy(False)
        if not run.outcomes:
            self._set_status("No message matched a reply rule.")
            return


        filed, ticked = self._apply_outcomes(run)
        drafts = run.drafts
        written = [d for d in drafts if d.ok]
        failed = [d for d in drafts if not d.ok]

        lines = [f"{run.matched} message"
                 f"{'' if run.matched == 1 else 's'} matched a reply rule."]
        if drafts:
            lines.append(f"{len(written)} draft{'' if len(written) == 1 else 's'} "
                         f"saved to your Drafts mailbox. Nothing has been sent.")
        if filed:
            lines.append(f"{filed} pointed at a different folder. Nothing has "
                         "moved yet. Press Apply when you are happy.")
        if ticked:
            lines.append(f"{ticked} ticked or unticked.")
        if run.marked_read:
            lines.append(f"{run.marked_read} marked as read.")
        if run.flagged:
            lines.append(f"{run.flagged} flagged.")
        if failed:
            lines.append("")
            lines.append(f"{len(failed)} could not be written:")
            lines.extend(f"  · {d.subject}: {d.error}" for d in failed[:5])
        if getattr(self, "_replies_prompted", True):
            QMessageBox.information(self, "Reply rules", "\n".join(lines))
        else:
            self._append_log(" ".join(line for line in lines if line))
        self._set_status(lines[0] + (f" {lines[1]}" if len(lines) > 1 else ""))

    def _apply_outcomes(self, run) -> Tuple[int, int]:
        """Carry the filing and ticking decisions into the table."""
        filed = ticked = 0
        for item, outcome in run.outcomes:
            if outcome.leave:
                if item.override_folder is not None:
                    item.override_folder = None
                    filed += 1
                item.approved = False
            elif outcome.file_into and outcome.file_into != item.target_folder:
                item.override_folder = outcome.file_into
                filed += 1
            if outcome.tick is not None and item.approved != outcome.tick:
                item.approved = outcome.tick
                ticked += 1
        if filed or ticked:
            self.model._refresh_all()
            self.model.selectionChanged.emit()
            self._update_status()
        return filed, ticked

    def apply_moves(self) -> None:
        if self._busy():
            return
        if self.dry_run:
            QMessageBox.information(
                self, "Dry run",
                "Folder moves are disabled in dry-run mode. Relaunch without "
                "--dry-run to file messages for real.",
            )
            return
        items = self.model.items
        plans = build_move_plans(items)
        if not plans:
            QMessageBox.information(
                self, "Nothing selected",
                "Tick at least one message before applying folder moves.",
            )
            return

        by_folder: Dict[str, int] = {}
        for plan in plans:
            by_folder[plan.target_folder] = by_folder.get(plan.target_folder, 0) + 1
        lines = "\n".join(f"    {count:>3} → {folder}" for folder, count in sorted(by_folder.items()))

        low_confidence = sum(
            1 for item in items
            if item.approved and item.is_actionable and not item.is_high_confidence
        )
        warning = ""
        if low_confidence:
            warning = (
                f"\n\n{low_confidence} of these are below your "
                f"{self.settings.confidence_threshold * 100:.0f}% confidence threshold."
            )

        confirm = QMessageBox(self)
        confirm.setWindowTitle("Apply folder moves")
        confirm.setIcon(QMessageBox.Icon.Question)
        confirm.setText(f"Move {len(plans)} message(s) out of {self.settings.source_mailbox}?")
        confirm.setInformativeText(
            f"{lines}{warning}\n\nEach message is copied to its folder first; the original is "
            "only removed after the copy is confirmed."
        )
        confirm.setStandardButtons(QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Ok)
        confirm.setDefaultButton(QMessageBox.StandardButton.Cancel)
        if confirm.exec() != QMessageBox.StandardButton.Ok:
            return

        if self.demo:
            self.model.apply_report(
                MoveReport(moved={plan.uid: plan.target_folder for plan in plans})
            )
            self._append_log(f"Demo: marked {len(plans)} message(s) as filed.")
            self._update_status(f"Demo: marked {len(plans)} message(s) as filed.")
            return

        try:
            passwords = self._mailbox_passwords()
        except CredentialError as exc:
            QMessageBox.critical(self, "Keychain", str(exc))
            return

        self._set_busy(True, "Filing messages…")
        self.apply_worker = ApplyWorker(
            settings=self.settings,
            mailbox_password=passwords,
            plans=plans,
            extra_folders=required_folders(items, self.folder_plan),
            parent=self,
        )
        self._register(self.apply_worker)
        self.apply_worker.progress.connect(self._on_progress)
        self.apply_worker.metrics.connect(self._on_metrics)
        self.apply_worker.log_message.connect(self._append_log)
        self.apply_worker.failed.connect(self._on_failed)
        self.apply_worker.finished_ok.connect(self._on_apply_done)
        self.apply_worker.finished.connect(lambda: self._set_busy(False))
        self.apply_worker.start()

    @Slot(object)
    def _on_apply_done(self, report: MoveReport) -> None:
        # Remember where everything came from, so it can be put back. The app
        # moves real mail; being able to undo that is what makes it safe to
        # try rather than something to be careful with.
        plans: List[MovePlan] = []
        for item in self.model.items:
            filed_to = report.moved.get(item.email.uid)
            if not filed_to:
                continue
            # The UID the message has *now*, which a COPY changed. Without
            # the server's receipt there is no way to name it, so it is left
            # out rather than pointed at whatever else holds that number.
            landed = report.new_uids.get(item.email.uid)
            if not landed:
                continue
            account = self.settings.account_by_id(item.email.account_id)
            home = account.source_mailbox if account else self.settings.source_mailbox
            plans.append(MovePlan(
                uid=landed,
                target_folder=home,
                subject=item.email.subject_display,
                account_id=item.email.account_id,
                source_folder=filed_to,
            ))
        if plans:
            self._undo_stack.append(UndoBatch(plans=plans, when=datetime.now()))
            del self._undo_stack[:-UNDO_DEPTH]
        self._sync_undo_action()
        self.model.apply_report(report)
        self._refresh_folder_choices()
        message = f"Filed {report.moved_count} message(s)."
        if report.failed:
            message += f" {report.failed_count} could not be moved."
        self._append_log(message)

        details = [message]
        if report.created_folders:
            details.append("Created: " + ", ".join(report.created_folders))
        if report.warnings:
            details.extend(report.warnings)
        if report.failed:
            sample = list(report.failed.items())[:5]
            details.append(
                "Failures:\n" + "\n".join(f"  UID {uid}: {error}" for uid, error in sample)
            )
        icon = QMessageBox.Icon.Warning if report.failed else QMessageBox.Icon.Information
        box = QMessageBox(icon, "Folder moves", "\n\n".join(details), parent=self)
        selectable(box)
        box.exec()
        self._update_status(message)

    # -- shared UI plumbing ----------------------------------------------
    def _register(self, worker: QThread) -> QThread:
        """Track a worker and reap it when it finishes.

        Without this the window accumulates finished QThread children for the
        life of the session, and a thread still running at quit becomes an
        orphaned process.
        """
        self._workers.append(worker)
        worker.finished.connect(lambda w=worker: self._reap(w))
        return worker

    def _reap(self, worker: QThread) -> None:
        if worker in self._workers:
            self._workers.remove(worker)
        if worker is self.scan_worker:
            self.scan_worker = None
        if worker is self.apply_worker:
            self.apply_worker = None
        worker.deleteLater()
        self._update_status()

    def running_workers(self) -> List[QThread]:
        return [w for w in self._workers if w.isRunning()]

    @Slot()
    def stop_all(self) -> int:
        """Stop every background task and drop its network connections.

        Returns the number of tasks that were running. Safe to call when
        nothing is running, and safe to call twice.
        """
        running = self.running_workers()
        if not running:
            self._set_status("Nothing is running.")
            return 0

        names = ", ".join(sorted({getattr(w, "task_name", "task") for w in running}))
        self._append_log(f"Stopping {len(running)} task(s): {names}…")

        # Tell every worker to stop *before* blocking on any of them, and paint
        # the stopped state immediately: a still-animating progress bar during
        # the wait is exactly what "Stop" is supposed to disprove.
        for worker in running:
            worker.cancel()
        self.stop_action.setEnabled(False)
        self._freeze_progress("Stopping…")
        self._set_status(f"Stopping {len(running)} task(s)…")
        QApplication.processEvents()

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            stubborn = [worker for worker in running if not worker.stop(4000)]
        finally:
            QApplication.restoreOverrideCursor()

        for worker in stubborn:
            _abandon(worker)
            self._workers.remove(worker) if worker in self._workers else None
            if worker is self.scan_worker:
                self.scan_worker = None
            if worker is self.apply_worker:
                self.apply_worker = None

        stopped = len(running)
        if stubborn:
            note = (
                f"{len(stubborn)} task(s) did not stop in time and were detached; "
                "they will end on their own and can no longer affect this window."
            )
            self._append_log(f"⚠︎ {note}")
        self._append_log(f"Stopped {stopped} task(s).")
        self._set_busy(False)
        self.metrics_bar.setVisible(False)
        self._update_status(f"Stopped {stopped} task(s).")
        return stopped

    def _freeze_progress(self, message: str) -> None:
        """Take the progress bar out of its indeterminate animation at once."""
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setFormat(message)
        self.progress.setVisible(False)

    def _busy(self) -> bool:
        if self.running_workers():
            QMessageBox.information(
                self, "Busy",
                "A task is already running. Press “Stop All” first if you want to start over.",
            )
            return True
        return False

    def _set_busy(self, busy: bool, message: str = "") -> None:
        # The primary button becomes the stop button while work is running:
        # the thing you want during a scan is always in the same place, and it
        # cannot be greyed out at the moment you most want to press it.
        self._set_scan_button(busy)
        self.apply_button.setEnabled(not busy and self.model.summary().approved > 0)
        self.progress.setVisible(busy)
        self.stop_action.setEnabled(busy)
        for button in self.window_buttons.values():
            button.setEnabled(not busy)
        if busy:
            self.progress.setRange(0, 0)
            self._set_status(message)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.metrics_bar.setVisible(False)
            self._update_status()


    @Slot(int, int, str)
    def _on_progress(self, done: int, total: int, message: str) -> None:
        if not self.running_workers():
            return                      # a late signal from a stopped worker
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
            self.progress.setFormat(f"%v / %m - {message}")
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(message)
        self._set_status(message)

    @Slot(dict)
    def _on_metrics(self, metrics: dict) -> None:
        if not self.running_workers():
            self.metrics_bar.setVisible(False)
            return
        self.metrics_bar.setText(_metrics_html(metrics))
        self.metrics_bar.setVisible(True)

    @Slot(str, str)
    def _on_failed(self, title: str, detail: str) -> None:
        self._append_log(f"✗ {title}: {detail}")
        box = QMessageBox(QMessageBox.Icon.Critical, title, detail, parent=self)
        selectable(box)
        box.exec()
        self._set_status(title)

    @Slot()
    def _selection_changed(self) -> None:
        indexes = self.table.selectionModel().selectedRows()
        if not indexes:
            self.preview.clear()
            return
        source_index = self.proxy.mapToSource(indexes[0])
        row = source_index.row()
        item = self.model.item_at(row)
        if item is None:
            self.preview.clear()
            return
        self.preview.show_item(row, item, self._prompt_for(item))

    def _selected_rows(self) -> List[int]:
        """Source rows for every selected line, in view order."""
        return [self.proxy.mapToSource(index).row()
                for index in self.table.selectionModel().selectedRows()]

    @Slot(object)
    def _table_menu(self, point) -> None:
        menu = self.build_table_menu()
        if menu is not None:
            menu.exec(self.table.viewport().mapToGlobal(point))

    def build_table_menu(self) -> Optional[QMenu]:
        """Right-click on the grid: act on everything selected, not just one.

        Re-filing forty rejections one at a time is the kind of thing that
        makes people stop using a tool, and the table has always allowed a
        multiple selection - there was simply nothing to do with one.

        Built here and shown by the caller, so a test can read the menu
        without a native modal loop that nothing can interrupt.
        """
        rows = self._selected_rows()
        if not rows:
            return None
        items = [self.model.item_at(row) for row in rows]
        items = [item for item in items if item is not None]
        if not items:
            return None

        menu = QMenu(self)
        many = len(items) > 1
        count = f"{len(items)} messages" if many else "this message"

        tick = menu.addAction(f"Tick {count}")
        tick.triggered.connect(lambda: self._set_approved(rows, True))
        untick = menu.addAction(f"Untick {count}")
        untick.triggered.connect(lambda: self._set_approved(rows, False))
        menu.addSeparator()

        folders = menu.addMenu(f"File {count} in…")
        for folder in self._folder_choices():
            leaf = folder.rsplit(self.folder_plan.delimiter if self.folder_plan
                                else "/", 1)[-1]
            action = folders.addAction(leaf)
            action.setToolTip(folder)
            action.triggered.connect(
                lambda _checked=False, target=folder: self._refile(rows, target))
        if folders.isEmpty():
            folders.setEnabled(False)

        revert = menu.addAction("Use the suggested folder")
        revert.setEnabled(any(item.override_folder for item in items))
        revert.triggered.connect(lambda: self._refile(rows, None))

        menu.addSeparator()
        thread = sorted({index for row in rows
                         for index in self.model.conversation_of(row)})
        if len(thread) > len(rows):
            select = menu.addAction(
                f"Select the whole conversation ({len(thread)} messages)")
            select.triggered.connect(lambda: self._select_rows(thread))
            file_thread = menu.addAction(
                "File the whole conversation together")
            folders = self._folder_choices()
            sub = QMenu(menu)
            for folder in folders:
                leaf = folder.rsplit(self.folder_plan.delimiter
                                     if self.folder_plan else "/", 1)[-1]
                action = sub.addAction(leaf)
                action.setToolTip(folder)
                action.triggered.connect(
                    lambda _c=False, t=folder: self._refile(thread, t))
            file_thread.setMenu(sub)
            file_thread.setEnabled(bool(folders))
            menu.addSeparator()

        copy = menu.addAction("Copy sender address"
                              if not many else "Copy sender addresses")
        copy.triggered.connect(lambda: self._copy_senders(items))
        return menu

    def _select_rows(self, rows: Sequence[int]) -> None:
        """Select these source rows, ignoring any the filter is hiding."""
        from PySide6.QtCore import QItemSelection, QItemSelectionModel
        last = self.model.columnCount() - 1
        selection = QItemSelection()
        for row in rows:
            top = self.proxy.mapFromSource(self.model.index(row, 0))
            end = self.proxy.mapFromSource(self.model.index(row, last))
            if top.isValid() and end.isValid():
                selection.select(top, end)
        if selection.isEmpty():
            return
        self.table.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows)

    def _folder_choices(self) -> List[str]:
        plan = self.folder_plan or FolderPlan()
        folders: List[str] = list(plan.leaf_folders)
        if self.settings.routing is NonJobRouting.FILE:
            folders += [plan.for_other_category(c) for c in OtherCategory
                        if c is not OtherCategory.NOT_APPLICABLE]
        return folders

    def _set_approved(self, rows: Sequence[int], approved: bool) -> None:
        self.model.set_approved(rows, approved)
        self._update_status()

    def _refile(self, rows: Sequence[int], folder: Optional[str]) -> None:
        """Send every selected row to one folder, learning from each."""
        for row in rows:
            item = self.model.item_at(row)
            self.model.set_override(row, folder)
            if folder and item is not None:
                self._learn_from(item, folder)
        self._update_status()
        self._selection_changed()

    def _copy_senders(self, items) -> None:
        addresses = []
        for item in items:
            address = item.email.sender_email
            if address and address not in addresses:
                addresses.append(address)
        if not addresses:
            return
        QApplication.clipboard().setText("\n".join(addresses))
        self._set_status(f"Copied {len(addresses)} address(es).")

    @Slot(object)
    def _header_menu(self, point) -> None:
        self.build_header_menu().exec(
            self.table.horizontalHeader().mapToGlobal(point))

    def build_header_menu(self) -> QMenu:
        """Right-click on the header: show, hide and reset the columns.

        The reset is the one that matters. Dragging a column to nothing is a
        single careless movement, and until now the only way back was to find
        the same invisible edge again.
        """
        menu = QMenu(self)
        for action in self.columns_menu.actions():
            menu.addAction(action)
        menu.addSeparator()
        reset = menu.addAction("Reset column widths")
        reset.triggered.connect(self._reset_columns)
        show_all = menu.addAction("Show every column")
        show_all.triggered.connect(self._show_all_columns)
        return menu

    def _prompt_builder(self) -> llm_engine.LLMEngine:
        """A no-network engine reused purely to re-render prompts for the preview."""
        settings_key = (self.settings.model, self.settings.max_body_chars)
        if self._prompt_engine is None or self._prompt_engine_key != settings_key:
            self._prompt_engine = llm_engine.LLMEngine(
                api_key="preview-only",
                model=self.settings.model,
                max_body_chars=self.settings.max_body_chars,
            )
            self._prompt_engine_key = settings_key
            self._prompt_cache.clear()
        return self._prompt_engine

    def _prompt_for(self, item: TriageItem) -> str:
        uid = item.email.uid
        if uid not in self._prompt_cache:
            try:
                self._prompt_cache[uid] = self._prompt_builder().build_prompt(item.email)
            except Exception as exc:  # pragma: no cover - display only
                self._prompt_cache[uid] = f"(could not rebuild the prompt: {exc})"
        return self._prompt_cache[uid]

    @Slot(int, object)
    def _override_changed(self, row: int, folder: Optional[str]) -> None:
        item = self.model.item_at(row)
        self.model.set_override(row, folder)
        if folder and item is not None:
            self._learn_from(item, folder)
        self._update_status()

    def _learn_from(self, item: TriageItem, folder: str) -> None:
        """Write down that this sender's mail belongs somewhere else.

        Best-effort throughout. Being unable to record a correction is not
        worth interrupting somebody mid-triage over, and the correction they
        just made still applies to the message in front of them either way.
        """
        if not self.settings.learn_from_corrections:
            return
        try:
            memory = corrections.Memory.load()
            recorded = memory.remember_move(
                item.email.sender_email, folder,
                suggested=item.suggested_folder or "",
                was_job_related=item.classification.is_job_related,
                subject=item.email.subject)
            if recorded:
                memory.save()
        except Exception as exc:  # noqa: BLE001 - never interrupt triage
            log.warning("Could not record that correction (%s).", exc)
            return
        if not recorded:
            return
        hit = memory.lookup(item.email.sender_email)
        if hit is not None and hit.strength == 1 and hit.scope == "address":
            leaf = folder.rsplit(item.folders.delimiter, 1)[-1]
            self._set_status(f"Noted - mail from "
                             f"{item.email.sender_email} will go to {leaf}.")

    def _select_first_row(self) -> None:
        if self.proxy.rowCount() > 0:
            self.table.selectRow(0)

    def _refresh_category_filter(self) -> None:
        current = self.category_filter.currentData()
        labels = sorted({item.classification.category_label for item in self.model.items})
        colors = {
            item.classification.category_label: category_color(
                item.classification, item)
            for item in self.model.items
        }
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", None)
        for label in labels:
            self.category_filter.addItem(_swatch(colors.get(label, OTHER_COLOR)), label, label)
        index = self.category_filter.findData(current)
        self.category_filter.setCurrentIndex(index if index >= 0 else 0)
        self.category_filter.blockSignals(False)
        self.proxy.set_category_filter(self.category_filter.currentData())

    def _refresh_folder_choices(self) -> None:
        plan = self.folder_plan or FolderPlan()
        folders: List[str] = list(plan.leaf_folders)
        if self.settings.routing is NonJobRouting.FILE:
            folders += [plan.for_other_category(c) for c in OtherCategory if c is not OtherCategory.NOT_APPLICABLE]
        for item in self.model.items:
            target = item.target_folder
            if target and target not in folders:
                folders.append(target)
        self.preview.set_folder_choices(folders)

    def _update_status(self, message: Optional[str] = None) -> None:
        """Refresh the apply button and the status bar.

        ``message`` pins a specific line (such as an apply result) instead of
        the generic counts, which would otherwise overwrite it immediately.
        """
        summary = self.model.summary()
        running = bool(self.running_workers())
        self.apply_button.setEnabled(summary.approved > 0 and not running)
        # Short enough not to push the toolbar onto a second row, which cost
        # forty pixels of height and left thirteen hundred of empty space
        # beside it. The full wording is the tooltip.
        self.apply_button.setText(
            f"Apply {summary.approved} Move{'s' if summary.approved != 1 else ''}"
            if summary.approved else "Apply Moves"
        )
        self.apply_button.setToolTip(
            f"Move the {summary.approved} ticked message"
            f"{'s' if summary.approved != 1 else ''} into their folders."
            if summary.approved
            else "Tick the messages you want filed, then press this."
        )
        if hasattr(self, "scan_button"):
            self._set_scan_button(running)
        if hasattr(self, "stop_action"):
            self.stop_action.setEnabled(running)
        if message is not None:
            self._set_status(message)
        elif summary.total and not running:
            self._set_status(summary.describe())

    def _set_density(self, lines: int) -> None:
        self._apply_density(lines)
        try:
            self.settings.save()
        except OSError:
            pass
        self.model.layoutChanged.emit()

    @Slot()
    def _sync_table_stack(self) -> None:
        """Show the grid, or the reason there is nothing in it.

        Three different empty states look identical on screen and mean
        completely different things: nothing scanned, nothing found, and
        everything found but filtered away. Only the third one has a fix the
        user can press, so it gets a button.
        """
        if self.proxy.rowCount():
            self.table_stack.setCurrentIndex(1)
            return
        self.table_stack.setCurrentIndex(0)
        if not self.model.rowCount():
            self.empty_label.setText(
                EMPTY_STATE if not self._has_scanned else NOTHING_FOUND)
            self.clear_filters_button.setVisible(False)
            return
        filters = self.proxy.active_filters()
        total = self.model.rowCount()
        reason = _one_of(filters)
        self.empty_label.setText(
            "<div style='text-align:center;line-height:170%'>"
            "<span style='font-size:15px'><b>Nothing matches</b></span><br>"
            f"<span style='opacity:0.7'>All {total} message(s) are hidden by "
            f"{reason}.</span></div>")
        self.clear_filters_button.setVisible(True)

    def _set_status(self, message: str) -> None:
        """Show as much of `message` as fits; the rest lives in the tooltip."""
        self._status_text = message
        available = max(80, self.status_label.width() - 8)
        metrics = QFontMetrics(self.status_label.font())
        self.status_label.setText(
            metrics.elidedText(message, Qt.TextElideMode.ElideRight, available)
        )
        self.status_label.setToolTip(message)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # Both of these re-fit text to the new width. They were once two
        # separate resizeEvents, which meant only the second one ran.
        super().resizeEvent(event)
        self._fit_window_label()
        if getattr(self, "_status_text", None):
            self._set_status(self._status_text)

    def _toggle_log(self, visible: bool) -> None:
        self.log_view.setVisible(visible)
        if visible:
            sizes = self.outer_splitter.sizes()
            if sizes[-1] < 60:
                self.outer_splitter.setSizes([max(240, sizes[0] - 140), 140])

    def _apply_preview_position(self, position: str) -> None:
        """Put the preview under the table or beside it.

        Under is right on a laptop, where height is what there is least of in
        a table and most of everywhere else. Beside is right on a wide screen,
        where the table has more width than it can use and the preview would
        otherwise be reading a paragraph across sixteen hundred pixels.
        """
        beside = position == "right"
        self.splitter.setOrientation(
            Qt.Orientation.Horizontal if beside else Qt.Orientation.Vertical)
        span = self.splitter.width() if beside else self.splitter.height()
        if span > 1:
            share = 0.42 if beside else 0.40
            self.splitter.setSizes(
                [int(span * (1 - share)), int(span * share)])
        if hasattr(self, "preview_actions"):
            for value, action in self.preview_actions.items():
                action.setChecked(value == position)

    @Slot(str)
    def set_preview_position(self, position: str) -> None:
        if position not in ("below", "right"):
            return
        if position == self.settings.preview_position:
            return
        self.settings.preview_position = position
        self._apply_preview_position(position)

    @Slot(str)
    def _append_log(self, message: str) -> None:
        self.log_view.appendPlainText(f"{datetime.now():%H:%M:%S}  {message}")

    def _export(self, fmt: str) -> None:
        items = self.model.items
        if not items:
            QMessageBox.information(self, "Nothing to export", "Run a scan first.")
            return
        suggested = str(
            Path.home() / "Downloads" / f"job-triage-{datetime.now():%Y%m%d-%H%M}.{fmt}"
        )
        filter_text = "CSV (*.csv)" if fmt == "csv" else "JSON (*.json)"
        path, _ = QFileDialog.getSaveFileName(self, "Export results", suggested, filter_text)
        if not path:
            return
        try:
            rows = [_export_row(item) for item in items]
            if fmt == "csv":
                with open(path, "w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
                    writer.writeheader()
                    writer.writerows(rows)
            else:
                Path(path).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        except OSError as exc:
            QMessageBox.critical(self, "Export failed", str(exc))
            return
        self._append_log(f"Exported {len(rows)} rows to {path}")

    @Slot()
    def _focus_search(self) -> None:
        self.search_edit.setFocus()
        self.search_edit.selectAll()

    @Slot()
    def _clear_filters(self) -> None:
        """Put every filter control back to "everything".

        The combo boxes and the proxy are told separately because the mailbox
        filter lives only in the proxy - it is set from the View menu, which
        has no single control to reset.
        """
        self.search_edit.clear()
        self.category_filter.setCurrentIndex(0)
        self.show_combo.setCurrentIndex(0)
        self._view_accounts = []
        self._view_all = True
        self.proxy.clear_filters()
        self._rebuild_view_menu()
        self._sync_table_stack()

    def _select_window(self, window: TimeWindow) -> None:
        """Pick a time window from the keyboard, keeping the buttons in sync."""
        button = self.window_buttons.get(window)
        if button is not None:
            button.setChecked(True)
        self._window_selected(window)

    def _show_shortcuts(self) -> None:
        rows = [
            ("⌘R", "Scan &amp; Analyze"),
            ("⌘↩", "Apply approved folder moves"),
            ("⌘.", "Stop all running tasks"),
            ("⌘F", "Jump to the filter box"),
            ("Esc", "Clear every filter"),
            ("⌘1 - ⌘4", "Past 24 hours / 3 days / 7 days / custom range"),
            ("⌘A", "Tick every movable message"),
            ("⌘⇧A", "Tick only the high-confidence ones"),
            ("⌘D", "Clear all ticks"),
            ("⌘L", "Show or hide the activity log"),
            ("⌘,", "Settings"),
        ]
        body = "".join(
            f"<tr><td style='padding:3px 18px 3px 0'><b>{key}</b></td>"
            f"<td style='padding:3px 0'>{label}</td></tr>"
            for key, label in rows
        )
        box = QMessageBox(self)
        box.setWindowTitle("Keyboard Shortcuts")
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(f"<table style='font-size:13px'>{body}</table>")
        box.exec()

    @Slot(int)
    def _open_attachments(self, row: int) -> None:
        """Fetch what was attached to one message, then show it.

        The bytes are not already here. A scan downloads the first part of
        each message and keeps the text, which is the right trade for sorting
        a hundred of them and the wrong one for looking at a photograph, so
        this asks the server again for that one message.
        """
        import attachments as _attachments
        from attachment_view import AttachmentViewer

        items = getattr(self.model, "items", [])
        item = items[row] if 0 <= row < len(items) else None
        if item is None:
            return
        subject = item.email.subject_display

        if self.demo:
            self._present_attachments(
                _attachments.demo_attachments(item.email), subject, None)
            return
        if self._busy():
            return

        wanted = item.email.account_id or ""
        account = next((a for a in self.settings.scan_accounts if a.id == wanted),
                       None) or next(iter(self.settings.scan_accounts), None)
        password = ""
        if account is not None:
            try:
                password = self.store.get_mailbox_password(account.address)
            except CredentialError as exc:
                QMessageBox.warning(self, "Keychain", str(exc))
                return
        if account is None or not password:
            QMessageBox.information(
                self, "Attachments",
                "The mailbox this message came from is not connected, so its "
                "attachments cannot be fetched. Add its password in Settings "
                "and scan again.")
            return

        self._set_status(f"Fetching what is attached to \u201c{subject[:40]}\u201d...")
        worker = AttachmentWorker(account, password, item.email, self)

        def show(source) -> None:
            if not source.found:
                self._set_status("Nothing came back for that message.")
                source.close()
                return
            self._set_status("")
            self._present_attachments(source.found, subject, source)

        def failed(detail: str) -> None:
            self._set_status("")
            QMessageBox.warning(self, "Attachments", detail)

        worker.ready.connect(show)
        worker.failed.connect(failed)
        self._register(worker)
        worker.start()

    def _present_attachments(self, found, subject: str, source) -> None:
        """Open the viewer as a window rather than a trap.

        It used to be modal, which meant Quit did nothing while it was up:
        the menu item fired and the application-modal dialog swallowed it. A
        viewer is something you leave open beside the window anyway.
        """
        from attachment_view import AttachmentViewer

        existing = getattr(self, "_attachment_window", None)
        if existing is not None:
            existing.close()
        viewer = AttachmentViewer(found, subject, self,
                                  fetch=source.fetch if source else None)
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._attachment_window = viewer

        def finished(*_args) -> None:
            if source is not None:
                source.close()
            if getattr(self, "_attachment_window", None) is viewer:
                self._attachment_window = None

        viewer.finished.connect(finished)
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def _close_attachment_window(self) -> None:
        """Called on the way out, so a viewer never outlives the window."""
        viewer = getattr(self, "_attachment_window", None)
        if viewer is not None:
            self._attachment_window = None
            viewer.close()

    def _about(self) -> None:
        """What this is, where the mail goes, and who to tell when it breaks."""
        from about import AboutDialog

        dialog = AboutDialog(self.settings, self.store, self)
        dialog.exec()
        dialog.deleteLater()




def _metrics_html(metrics: dict) -> str:
    """The live readout shown while a scan or apply is in flight."""
    phase = metrics.get("phase", "")
    done, total = int(metrics.get("done", 0)), int(metrics.get("total", 0))
    parts = []

    heading = {"fetch": "Fetching", "analyze": "Analyzing", "apply": "Filing"}.get(phase, "Working")
    parts.append(_chip(heading, f"{done:,} / {total:,}", ACCENT_BLUE))

    if phase == "analyze":
        parts.append(_chip("job-related", f"{int(metrics.get('job_related', 0)):,}"))
        parts.append(_chip("to file", f"{int(metrics.get('to_file', 0)):,}", ACCENT_GREEN))
        parts.append(_chip("needs review", f"{int(metrics.get('needs_review', 0)):,}", ACCENT_AMBER))
        requests = int(metrics.get("requests", 0))
        tokens = int(metrics.get("input_tokens", 0)) + int(metrics.get("output_tokens", 0))
        parts.append(_chip("requests", f"{requests:,}"))
        parts.append(_chip("tokens", f"{tokens:,}"))
        if metrics.get("on_device"):
            parts.append(_chip("cost", "free", ACCENT_GREEN))
        else:
            parts.append(_chip("cost", f"${float(metrics.get('cost', 0.0)):,.4f}"))
        if metrics.get("fallbacks"):
            parts.append(_chip("local fallback", f"{int(metrics['fallbacks']):,}", ACCENT_AMBER))

    rate = float(metrics.get("rate", 0.0))
    if rate > 0:
        unit = "msg/s" if rate >= 1 else "s/msg"
        value = f"{rate:.1f}" if rate >= 1 else f"{1 / rate:.1f}"
        parts.append(_chip("rate", f"{value} {unit}"))
    eta = float(metrics.get("eta", 0.0))
    if eta > 0 and done < total:
        parts.append(_chip("remaining", _format_duration(eta)))
    parts.append(_chip("elapsed", _format_duration(float(metrics.get("elapsed", 0.0)))))
    if metrics.get("model"):
        parts.append(_chip("model", _html(str(metrics["model"]))))
    separator = "&nbsp;&nbsp;<span style='opacity:0.35'>|</span>&nbsp;&nbsp;"
    return "<div style='font-size:12px'>" + separator.join(parts) + "</div>"


def _export_row(item: TriageItem) -> dict:
    classification = item.classification
    return {
        "uid": item.email.uid,
        "date": item.email.date.isoformat() if item.email.date else "",
        "sender_name": item.email.sender_name,
        "sender_email": item.email.sender_email,
        "subject": item.email.subject,
        "summary": classification.summary,
        "is_job_related": classification.is_job_related,
        "category": classification.category.value,
        "other_category": classification.other_category.value,
        "confidence": round(classification.confidence_score, 4),
        "disposition": item.disposition.value,
        "target_folder": item.target_folder or "",
        "approved": item.approved,
        "moved": item.moved,
        "reasoning": classification.reasoning,
        "adjustments": "; ".join(classification.adjustments),
        "error": classification.error or "",
        "model": classification.model,
    }


def _reveal(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:  # pragma: no cover
        from PySide6.QtCore import QUrl

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
