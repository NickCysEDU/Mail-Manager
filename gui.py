"""PySide6 user interface for iCloud Mail Job Triage."""

from __future__ import annotations

import csv
import json
import logging
import re
import subprocess
import sys
import weakref
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import shiboken6
from PySide6.QtCore import (
    QEvent,
    QObject,
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
    QAbstractItemView,
    QStackedWidget,
    QApplication,
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
import touchbar
import widgets
from config import (
    CredentialError,
    CredentialStore,
    Settings,
    log_dir,
)
from imap_engine import MovePlan, MoveReport
from models import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    OTHER_COLOR,
    Disposition,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
    TriageSummary,
    clock,
    message_key,
    one_row_each,
    resolve_window,
)
from flowlayout import FlowLayout, Spacer
from widgets import (
    ACCENT_AMBER, ACCENT_BLUE, ACCENT_GREEN, AdaptiveLineEdit, DateField,
    ElidingLabel, RoomyCombo, VersionLabel, _abandon, _chip, _format_duration, _html,
    _mono_font,
    _paint_button, _stored_date, _swatch, describe, menu_text, selectable,
    EMPTY_STATE, NOTHING_FOUND, SHOW_ALL, SHOW_JOB_ONLY, SHOW_OTHER_ONLY,
    SHOW_SELECTED, _one_of)
import helpmode
import theme
from settings_dialog import SettingsDialog
from triage_table import (
    NOT_SORTED, CategoryDelegate, ConfidenceDelegate, PreviewPane,
    TriageFilterProxy, TriageTableModel, WrapDelegate, category_color,
    mark_moves)
from sidebar import INBOX, MailboxList, Place, accounts_having
from menubar import MenuBarController
from welcome import SetupWizard
from workers import (AttachmentWorker,
    AttachmentFetchWorker,
    FolderMoveWorker,
    WholeMessageWorker,
    DraftWorker,
    FlagWorker,
    ListWorker,
    MailboxesWorker,
    MoveWorker,
    SendWorker,
    ReplyWorker,
    ApplyWorker,
    ScanOutcome,
    ScanWorker,
    build_move_plans,
    required_folders,
)

log = logging.getLogger(__name__)

#: How many filings stay undoable: enough for a session of filing in passes,
#: few enough that the oldest still describes the mail as it is.
UNDO_DEPTH = 10

#: What each choice means, said plainly; the enum labels are only short enough
#: to fit in a menu.
_ROUTING_HELP = {
    NonJobRouting.LEAVE:
        "Other mail stays in your inbox and cannot be ticked.",
    NonJobRouting.REVIEW:
        "Non-job mail is gathered into Job Search / Needs Review so you can "
        "look through it in one place.",
    NonJobRouting.FILE:
        "Other mail is filed by topic into Sorted Mail, and can be ticked "
        "like anything else.",
}


@dataclass
class UndoBatch:
    """One completed filing, and how to put it back."""

    plans: List[MovePlan]
    when: datetime

class DockReopen(QObject):
    """Brings the window back when the app is activated with it hidden: a
    click on the Dock icon, with the window closed, did nothing."""

    def __init__(self, window, parent=None) -> None:
        super().__init__(parent)
        self._window = window

    def eventFilter(self, watched, event) -> bool:      # noqa: N802 - Qt
        if (event.type() == QEvent.Type.ApplicationActivate
                and not self._window.isVisible()
                and QApplication.activeModalWidget() is None):
            self._window._reveal()
        return False


def bring_forward(window: QWidget) -> None:
    """A window to the front, minimised or not, as it was: full screen or
    zoomed stays so."""
    if not shiboken6.isValid(window):
        return
    window.show()
    if window.isMinimized():
        window.setWindowState(window.windowState() & ~Qt.WindowState.WindowMinimized)
    window.raise_()
    window.activateWindow()


class OpenWindows(QObject):
    """Every window that is open, at the top of the Dock icon's menu, as a
    Mac lists an app's windows there: the one in front ticked, and choosing
    one brings it forward. Kept in step as windows open, close, are
    minimised and change their titles.

    One for the application, made by the first menu it serves: one per
    window would put every event through a filter for each window ever
    made.

    It lists the windows the app made, as it sees them shown, rather than
    ask Qt for every top-level widget: that walk met a widget whose memory
    was no longer one, now and then, and took the process down with it."""

    CHANGES = (QEvent.Type.Show, QEvent.Type.Hide, QEvent.Type.Close,
               QEvent.Type.WindowTitleChange, QEvent.Type.WindowStateChange,
               QEvent.Type.WindowActivate, QEvent.Type.WindowDeactivate)

    @classmethod
    def serve(cls, menu: QMenu, before: QAction) -> "OpenWindows":
        """Keep ``menu``'s list of windows, above ``before``."""
        app = QApplication.instance()
        found = getattr(app, "_open_windows", None)
        if found is None or not shiboken6.isValid(found):
            found = cls(app)
            app._open_windows = found
            app.installEventFilter(found)
        # Weakly: a menu goes with its window, and is not kept for this.
        found._menus.append((weakref.ref(menu), weakref.ref(before), []))
        found._again.start()
        return found

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        #: (menu, the entry the windows go above, the entries put there),
        #: the first two weakly.
        self._menus: List[tuple] = []
        #: The windows the app made, as each was shown; weakly too, so a
        #: window closed and let go is not kept for this.
        self._seen: "weakref.WeakSet[QWidget]" = weakref.WeakSet()
        self._shown = 0
        self._again = QTimer(self)
        self._again.setSingleShot(True)
        self._again.setInterval(0)
        self._again.timeout.connect(self.refresh)

    def eventFilter(self, watched, event) -> bool:      # noqa: N802 - Qt
        # Every event in the application passes through here: the cheapest
        # test first.
        if event.type() not in self.CHANGES or not isinstance(watched, QWidget):
            return False
        if watched.isWindow() and not isinstance(watched, QMenu):
            if event.type() == QEvent.Type.Show:
                if watched.property("openedAs") is None:
                    self._shown += 1
                    watched.setProperty("openedAs", self._shown)
                if shiboken6.createdByPython(watched):
                    self._seen.add(watched)
            self._again.start()
        return False

    #: The longest a window's title is shown in the Dock's menu.
    TITLE_MOST = 60

    @classmethod
    def entry_for(cls, title: str) -> str:
        """A window's title as its entry: a message's window is called by
        its subject, which its sender wrote. One line, not too long, and an
        ampersand shown as itself rather than taken for a menu's shortcut
        mark."""
        title = " ".join(title.split())
        if len(title) > cls.TITLE_MOST:
            title = title[:cls.TITLE_MOST - 1].rstrip() + "…"
        return title.replace("&", "&&")

    def windows(self) -> List[QWidget]:
        """The windows to list, in the order they were opened."""
        found = [widget for widget in list(self._seen)
                 if shiboken6.isValid(widget) and widget.isVisible()
                 and widget.windowType() in (Qt.WindowType.Window,
                                             Qt.WindowType.Dialog)
                 and widget.windowTitle().strip()]
        return sorted(found, key=lambda widget: widget.property("openedAs") or 0)

    def refresh(self) -> None:
        def alive(ref):
            found = ref()
            return found is not None and shiboken6.isValid(found)

        self._menus = [entry for entry in self._menus
                       if alive(entry[0]) and alive(entry[1])]
        windows = self.windows()
        front = QApplication.activeWindow()
        for menu_ref, before_ref, listed in self._menus:
            menu, before = menu_ref(), before_ref()
            for action in listed:
                menu.removeAction(action)
                action.deleteLater()
            listed.clear()
            for window in windows:
                action = QAction(self.entry_for(window.windowTitle()), menu)
                action.setCheckable(True)
                action.setChecked(window is front)
                action.triggered.connect(
                    lambda _checked=False, shown=window: bring_forward(shown))
                menu.insertAction(before, action)
                listed.append(action)
            if windows:
                listed.append(menu.insertSeparator(before))


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
        #: Each completed filing, newest last, so filing done in passes can be
        #: undone pass by pass.
        self._undo_stack: List[UndoBatch] = []
        #: Every background thread this window has started and not yet reaped.
        self._workers: List[QThread] = []
        self.folder_plan: Optional[FolderPlan] = settings.folder_plan()
        #: Set only by an explicit Quit, so closeEvent can tell hiding from
        #: quitting.
        self._quitting = False
        #: Whether confirm_quit has been answered for this leaving: quit_app
        #: asks, and the close it then makes must not ask again.
        self._quit_confirmed = False
        #: The Settings window while it is open, so Quit can deal with it.
        self._settings_dialog = None
        #: What the primary button currently does, so it can be rewired
        #: without disconnecting slots that were never attached.
        self._scan_button_action = None
        #: Whether the row height was chosen by hand; until then it follows the
        #: density.
        self._row_lines_chosen = False
        #: Whether the preview was opened deliberately: the densest setting
        #: starts it closed, but should not keep closing it.
        self._preview_opened = False
        #: Whether the preview has been given its share of the window yet,
        #: and the density that share last followed.
        self._preview_split = False
        self._density_shown = None
        #: Mailboxes shown, and whether "all" is in force. Kept apart so
        #: unticking the last mailbox empties the table rather than meaning
        #: every mailbox.
        self._view_accounts: List[str] = []
        self._view_all = True
        #: Which providers have a key in the Keychain. See store_has_key.
        self._key_present: Dict[str, bool] = {}
        self._prompt_cache: Dict[str, str] = {}
        self._prompt_engine: Optional[llm_engine.LLMEngine] = None
        self._prompt_engine_key: Optional[tuple] = None
        #: The update check while it runs, and the window offering one.
        self._update_look = None
        self._update_dialog = None
        self._attachment_window = None
        self._visualiser_window = None
        #: Every message and compose window opened from here. They stand on
        #: their own, so the main window closing to the menu bar leaves
        #: them; quitting closes them.
        self._mail_windows: list = []
        #: The batch an undo is putting back, while it runs.
        self._undoing: Optional[UndoBatch] = None
        #: The inbox, by message, as listed when the window opened: rows the
        #: sorter has not read. Then the last scan's rows. The table shows
        #: the second over the first; see _inbox_rows.
        self._listed: Dict[tuple, TriageItem] = {}
        self._sorted: List[TriageItem] = []
        #: The rows of every other mailbox looked at, by place and then by
        #: message, as listed.
        self._elsewhere: Dict[Place, Dict[tuple, TriageItem]] = {}
        #: Every UID a folder held at its last look, listed or outside the
        #: period, by place, account and folder: a look again reads the rest.
        self._present: Dict[tuple, set] = {}
        #: Listings running, by place, and the workers listing each account's
        #: folders for the sidebar.
        self._listings: Dict[Place, List[QThread]] = {}
        self._mailbox_workers: List[QThread] = []
        #: Messages whose whole text is being read, has been read, or could
        #: not be, and why and when: a scan reads only the start of each
        #: (see read_whole).
        self._reading_whole: set = set()
        self._whole_read: set = set()
        self._whole_failed: Dict[tuple, tuple] = {}
        #: Whether the mailboxes have been opened: once, when the window
        #: first shows. And whether a scan at launch waits for the inbox.
        self._mailboxes_opened = False
        self._scan_after_listing = False
        #: Auto scan is a launch's: tried once, window or no window.
        self._scanned_at_launch = False
        #: The period the last scan covered, for the briefing.
        self._scanned_window = (None, None)
        self._replies_prompted = True
        self._status_text = ""
        self._window_wordings = ()
        self.schedule_actions: Dict[int, QAction] = {}
        self.density_actions: Dict[int, QAction] = {}
        self.preview_actions: Dict[str, QAction] = {}
        self.menu_bar: Optional[MenuBarController] = None

        self.setWindowTitle(APP_DISPLAY_NAME)
        # 580 tall: between the toolbars and the status bar sit the table and
        # the preview, and the preview has a floor of its own (a header, a
        # filing row, and two halves with controls and text). At 560 they
        # needed 319 px of a 310 px splitter, and the preview drew past its
        # bottom.
        self.setMinimumSize(760, 580)

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
        # Built before the controller existed, so it is handed the current
        # choice now.
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
        self._give_touch_bar()

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
        # Room for a row or two, not Qt's default. On an 800x560 window the
        # splitter has about 310 px and the preview needs 215; the table's own
        # minimum of 76 left the two 8 px over.
        self.table.setMinimumHeight(40)
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
        # A double-click, or Return, opens the message in a window of its
        # own, as it does in every mail client.
        self.table.doubleClicked.connect(self._open_index)
        self.table.activated.connect(self._open_index)

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

        #: Whether a scan has finished this session: "nothing scanned yet"
        #: against "scanned, and nothing there".
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
        self.model.modelReset.connect(self._refresh_mail_windows)
        # A filter emptying the view is just as much a reason to swap pages as
        # the model emptying, and only the proxy knows when that happens.
        self.proxy.rowsInserted.connect(self._sync_table_stack)
        self.proxy.rowsRemoved.connect(self._sync_table_stack)
        self.proxy.layoutChanged.connect(self._sync_table_stack)

        self.preview = PreviewPane()
        self.preview.missing_from = self.missing_from
        self.preview.overrideChanged.connect(self._override_changed)
        self.preview.attachmentsRequested.connect(self._open_attachments)
        self.preview.linkRequested.connect(self._open_link)
        self.preview.sortNonJobRequested.connect(
            lambda: self._switch_routing(NonJobRouting.FILE))
        self.preview.composeRequested.connect(self.compose)
        self.preview.openRequested.connect(self.open_message)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(_mono_font())
        self.log_view.setVisible(self.settings.show_log_panel)

        # Two splitters: the inner holds the table and the preview, beside or
        # stacked as preferred; the outer holds the log, always along the
        # bottom.
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

        # The mailboxes down the left, as in Mail: under the toolbar, beside
        # the messages.
        self.sidebar = MailboxList()
        self.sidebar.chosen.connect(self._chosen)
        self.sidebar.moveRequested.connect(self._move_folder)
        self.sidebar.set_accounts(self.settings.accounts)
        self.sidebar.setVisible(self.settings.show_sidebar)
        self.side_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.side_splitter.addWidget(self.sidebar)
        self.side_splitter.addWidget(self.outer_splitter)
        self.side_splitter.setCollapsible(0, False)
        self.side_splitter.setCollapsible(1, False)
        self.side_splitter.setStretchFactor(0, 0)
        self.side_splitter.setStretchFactor(1, 1)
        width = self.settings.sidebar_width or MailboxList.WIDTH
        self.side_splitter.setSizes([width, 1000])
        layout.addWidget(self.side_splitter, 1)
        self._apply_preview_position(self.settings.preview_position)

        self.setCentralWidget(central)

        self.status_label = ElidingLabel("Ready.")
        self.status_label.setMinimumWidth(120)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.usage_label = QLabel("")
        self.usage_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # The build, in the corner. During development every change between
        # releases shares a version number, so the commit is what makes a bug
        # report answerable. Click to copy.
        self.version_label = VersionLabel()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.usage_label)
        self.statusBar().addPermanentWidget(self.version_label)
        self.statusBar().setSizeGripEnabled(True)

        self._name_controls()

    def _name_controls(self) -> None:
        """Name every control for a screen reader, in one list so anything
        missing shows.
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
        # Summary has the stretch and is the column people read across, so the
        # others leave it room. Reasoning gives up the most: it is in full in
        # the pane below.
        TriageTableModel.COL_REASONING: 170,
        TriageTableModel.COL_ACCOUNT: 175,
    }

    def _reset_columns(self) -> None:
        for column, width in self.COLUMN_WIDTHS.items():
            self.table.setColumnWidth(column, width)
        self._sync_account_column()

    def _sync_account_column(self) -> None:
        """The mailbox column, shown only when there is more than one mailbox.
        A column hidden by hand stays hidden either way.
        """
        column = TriageTableModel.COL_ACCOUNT
        if column in self.settings.hidden_columns:
            self.table.setColumnHidden(column, True)
            return
        multi = self.settings.multi_account
        self.table.setColumnHidden(column, not multi)
        # The model's last column would sit off the right edge of a table this
        # wide, so it is moved to the front visually; the model's indices are
        # untouched.
        header = self.table.horizontalHeader()
        wanted = 1 if multi else header.count() - 1
        current = header.visualIndex(column)
        if current != -1 and current != wanted:
            header.moveSection(current, wanted)
        if multi:
            # Wide enough for the longest address on screen: an elided address
            # loses the part that says which mailbox it is.
            metrics = QFontMetrics(self.table.font())
            longest = max(
                (metrics.horizontalAdvance(i.email.mailbox_display)
                 for i in self.model.items if i.email.mailbox_display),
                default=0,
            )
            self.table.setColumnWidth(
                column, min(300, max(self.COLUMN_WIDTHS[column], longest + 24)))

    #: Below this the summary is a word and an ellipsis.
    MIN_SUMMARY_WIDTH = 160

    def _heal_column_widths(self) -> None:
        """Undo a saved layout that leaves the summary too narrow to read.

        Widths are remembered, but a layout saved on a narrower window, or
        before a column was added, can need more than the window has, and
        the stretch column gives way. Past a point that is nobody's choice,
        so it goes back to the defaults.
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
        # Rows that exist keep their height, so otherwise the change would show
        # only on the next scan. The header counts the rows shown.
        for row in range(self.proxy.rowCount()):
            header.resizeSection(row, height)
        # Made once and told the new count after: a fresh set each time piled
        # up under the table for the life of the window.
        if not getattr(self, "_wrap_delegates", None):
            self._wrap_delegates = []
            for column in (TriageTableModel.COL_SENDER, TriageTableModel.COL_SUBJECT,
                           TriageTableModel.COL_SUMMARY, TriageTableModel.COL_REASONING,
                           TriageTableModel.COL_FOLDER):
                delegate = WrapDelegate(lines, self.table)
                self.table.setItemDelegateForColumn(column, delegate)
                self._wrap_delegates.append(delegate)
            self.confidence_delegate = ConfidenceDelegate(
                self.settings.confidence_threshold, self.table)
            self.table.setItemDelegateForColumn(
                TriageTableModel.COL_CONFIDENCE, self.confidence_delegate)
            self.table.setItemDelegateForColumn(
                TriageTableModel.COL_CATEGORY, CategoryDelegate(self.table))
        for delegate in self._wrap_delegates:
            delegate.lines = lines
        self.table.viewport().update()
        for value, action in self.density_actions.items():
            action.setChecked(value == lines)

    def _build_action_bar(self) -> QWidget:
        frame = QFrame()
        frame.setFrameShape(QFrame.Shape.NoFrame)
        row = FlowLayout(frame, margin=0, spacing=6, vertical_spacing=6)
        self.action_bar_layout = row

        self.sidebar_button = QToolButton()
        self.sidebar_button.setCheckable(True)
        self.sidebar_button.setChecked(self.settings.show_sidebar)
        self.sidebar_button.setToolTip(
            "Show or hide your mailboxes: the inbox, Drafts, Sent and every "
            "folder (\u2303\u2318S).")
        self.sidebar_button.toggled.connect(self._show_sidebar)
        self._paint_sidebar_button()
        row.addWidget(self.sidebar_button)

        self.window_buttons: Dict[TimeWindow, QToolButton] = {}
        for window in TimeWindow:
            button = QToolButton()
            # These four read as one control and are sized as one: the theme's
            # button padding, four times over, pushed Apply onto a row of its
            # own.
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
        # QCalendarWidget, about a tenth of a second per field.
        # _sync_range_visibility turns it on when the fields first show.
        self.start_date = DateField()
        self.start_date.setDisplayFormat("d MMM yy")
        self.start_date.setToolTip("The first day to read, included.")
        self.end_date = DateField()
        self.end_date.setDisplayFormat("d MMM yy")
        self.end_date.setToolTip("The last day to read, included.")
        today = QDate.currentDate()
        self.start_date.setDate(_stored_date(self.settings.custom_start, today.addDays(-7)))
        self.end_date.setDate(_stored_date(self.settings.custom_end, today))
        self.start_date.dateChanged.connect(self._refresh_window_label)
        self.end_date.dateChanged.connect(self._refresh_window_label)

        # The dates replace the label rather than sit beside it: beside it they
        # added 367 points to a full row, and the toolbar wrapped onto a second
        # line.
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
        # The slot is as wide as the label's longest wording; without this the
        # two fields stretch apart.
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

        # The model is the most consequential setting, so it has a control in
        # the window, not only in Settings.
        self.model_button = QToolButton()
        self.model_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.model_button.setToolTip("Switch the model backend (⌘M)")
        self.model_menu = QMenu(self)
        self.model_button.setMenu(self.model_menu)
        self.model_button.setProperty("menu", "true")
        row.addWidget(self.model_button)

        # What gets sorted, and what happens to the rest: in the window, where
        # somebody looks when a row will not tick.
        self.sorting_button = QToolButton()
        self.sorting_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.sorting_menu = QMenu(self)
        self.sorting_button.setMenu(self.sorting_menu)
        self.sorting_button.setProperty("menu", "true")
        row.addWidget(self.sorting_button)

        # Only worth the space once there is more than one mailbox.
        self.account_button = QToolButton()
        self.account_button.setToolTip(
            "Which mailboxes the next scan reads. Only shown when you have "
            "more than one.")
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_menu = QMenu(self)
        self.account_button.setMenu(self.account_menu)
        self.account_button.setProperty("menu", "true")
        row.addWidget(self.account_button)

        self.scan_button = QPushButton("Scan && Analyze")
        # Pinned to the wider of its two labels so the toolbar does not jump
        # when it becomes Stop.
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

    def _rebuild_model_menu(self) -> None:
        """Every backend and model, one click away from the main window."""
        # The old entries go once what they set off is over: a model chosen
        # here, or Settings opened from here, rebuilds the menu from inside
        # the entry's own signal. Made by the window, they were never freed.
        for action in self.model_menu.actions():
            self.model_menu.removeAction(action)
            (action.menu() or action).deleteLater()
        current = (self.settings.provider, self.settings.model)
        for name, label, _blurb in providers.provider_choices():
            spec = providers.provider_class(name)
            section = self.model_menu.addMenu(menu_text(label))
            for choice in spec.models:
                action = QAction(menu_text(choice.label), section)
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
                missing = QAction("No API key stored. Open Settings…", section)
                missing.triggered.connect(self.open_settings)
                section.addAction(missing)
        self.model_menu.addSeparator()
        rules_menu = self.model_menu.addMenu("Local rule set (field)")
        for name, label, blurb in rulesets.choices():
            action = QAction(menu_text(label), rules_menu)
            action.setCheckable(True)
            action.setChecked(name == self.settings.ruleset)
            action.setStatusTip(blurb)
            action.setToolTip(blurb)
            action.triggered.connect(lambda checked=False, r=name: self._switch_ruleset(r))
            rules_menu.addAction(action)
        self.model_menu.addSeparator()
        more = QAction("Model settings…", self.model_menu)
        more.setShortcut(QKeySequence("Ctrl+M"))
        more.triggered.connect(lambda: self.open_settings(tab=1))
        self.model_menu.addAction(more)
        self._refresh_model_button()
        touchbar.refresh(self)

    #: The Show filter's entries, in the few words the Touch Bar has room for.
    SHOW_SHORT = {"Show: everything": "All", "Show: job mail only": "Job mail",
                  "Show: everything but job mail": "Not job",
                  "Show: ticked only": "Ticked"}

    def _give_touch_bar(self) -> None:
        """Scan, Apply and Undo, the filters, and two panels: the rest of
        the window's commands, and the quick settings."""
        busy = self.stop_action.isEnabled
        ticks = [touchbar.Button(f"ticks-{what}", label,
                                 lambda w=what: self._ticks(w))
                 for label, what in (("All shown", "all"),
                                     ("Confident", "confident"),
                                     ("None", "none"),
                                     ("Suggested", "suggested"))]
        more = [
            touchbar.Button("visualise", "Visualiser",
                            self._visualise_a_file, image="waveform"),
            touchbar.Button("briefing", "Briefing", self._show_briefing,
                            image="list.bullet.rectangle"),
            touchbar.Button("find", "Find", self._focus_search, title="",
                            image="magnifyingglass"),
            touchbar.Button("new-message", "New message",
                            lambda: self.compose("new"), image="square.and.pencil"),
            touchbar.Button("open-message", "Open message", self._open_selected,
                            image="envelope.open"),
            touchbar.Button("links", "Links", self.preview.links_button,
                            follow=False, image="link"),
            touchbar.Button("clear", "Clear filters", self._clear_filters,
                            image="line.3.horizontal.decrease.circle"),
            touchbar.Button("rescan", "Re-analyse all",
                            self.rescan_everything, priority="low",
                            image="arrow.triangle.2.circlepath"),
            touchbar.Button("replies", "Reply rules", self.draft_replies,
                            priority="low", image="arrowshape.turn.up.left"),
        ]
        options = [
            touchbar.Choice("period", "Period",
                            list(self.window_buttons.values())),
            touchbar.Choice("model", "Model", self._model_actions,
                            style="list", width=360),
            touchbar.Toggle("help", "Hover help", self.help_button),
            touchbar.Button("settings", "Settings", self.open_settings,
                            title="", image="gearshape"),
        ]
        touchbar.give(self, [
            touchbar.Button(
                "scan", "Scan", self.scan_button,
                title=lambda: ("Stop" if busy() else
                               "Reload" if self.demo else "Scan"),
                image=lambda: "stop.fill" if busy() else "arrow.clockwise",
                role=lambda: "danger" if busy() else "primary",
                watch=[self.stop_action.changed], priority="high"),
            touchbar.Button("apply", "Apply", self.apply_button,
                            title=self._apply_title, follow=False,
                            image="tray.and.arrow.down",
                            role=lambda: ("confirm" if self._inbox_summary().approved
                                          else None),
                            watch=[self.model.selectionChanged],
                            priority="high"),
            touchbar.Button("undo", "Undo", self.undo_action, title="",
                            image="arrow.uturn.backward", follow=False,
                            priority="low"),
            touchbar.Space("small"),
            touchbar.Choice("show", "Show", self.show_combo,
                            short=self.SHOW_SHORT, priority="high"),
            touchbar.Choice("category", "Category", self.category_filter,
                            style="popover"),
            touchbar.Popover("ticks", "Ticks", ticks,
                             image="checkmark.circle"),
            touchbar.Space("flexible"),
            touchbar.Popover("more", "More", more, title="",
                             image="ellipsis.circle"),
            touchbar.Popover("options", "Options", options, title="",
                             image="slider.horizontal.3"),
        ], "main", customizable=True)

    def _apply_title(self) -> str:
        approved = self._inbox_summary().approved
        return f"Apply {approved}" if approved else "Apply"

    def _model_actions(self) -> List[QAction]:
        """The model menu's backends and models, before the rule sets."""
        found: List[QAction] = []
        for action in self.model_menu.actions():
            if action.isSeparator():
                break
            if action.menu() is not None:
                found += [a for a in action.menu().actions() if a.isCheckable()]
        return found

    def _rebuild_sorting_menu(self) -> None:
        """Everything that decides where mail goes, in one menu, in the order
        the questions come: what is sorted, what happens to the rest, and
        which of the rest is worth a folder.
        """
        self.sorting_menu.clear()

        # addSection, not a disabled action, which is drawn like a choice
        # nobody may make.
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

        # Topics only matter when non-job mail is filed, so they are offered
        # only then.
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
        touchbar.refresh(self)

    def _refresh_sorting_button(self) -> None:
        """Say what the current arrangement is, on the button itself."""
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
        """Re-point every row without re-analysing: where a message goes is
        routing, not classification, so the change is instant.
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

        # Tick any number. Unticking the last one means all of them: scanning
        # nothing is never what somebody meant.
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
        # Named for what it does: the viewer has a picker of its own, and two
        # controls reading "All mailboxes" would be confusing.
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

        Cached: reading the Keychain is a system call, and the first read
        also imports ``keyring``. With `probe` false the Keychain is left
        alone and an unknown provider is assumed set up, keeping that cost
        off the way to the first window.
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
        if self.menu_bar is not None:
            self.menu_bar.set_model(
                self.settings.provider, self.settings.model, self.settings.ruleset)

    def _refresh_model_button(self) -> None:
        self.preview.set_backend_label(self.settings.provider_label.split(" (")[0])
        self.preview.set_pictures(self.settings.show_images)
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
            + ("No API key stored for this backend.\n" if warn else "")
            + "Click to switch backend or model (⌘M)"
        )
        self._sync_menu_bar_model()

    def apply_appearance(self) -> None:
        """Repaint everything from the current appearance settings."""
        app = QApplication.instance()
        theme.apply(app, self.settings.appearance_mode, self.settings.contrast,
                    self.settings.readable, self.settings.density)
        self._apply_spacing()
        helpmode.install(app, self.settings.help_mode)
        if self.help_button.isChecked() != self.settings.help_mode:
            self.help_button.blockSignals(True)
            self.help_button.setChecked(self.settings.help_mode)
            self.help_button.blockSignals(False)
        self._apply_density(self.settings.effective_row_lines)
        self._reset_columns()
        self._paint_sidebar_button()
        self.sidebar.set_accounts(self.settings.accounts)
        self.table.viewport().update()

    def _apply_spacing(self) -> None:
        """Push the density into what a stylesheet cannot reach: the margins
        between toolbar rows, the preview's share of the window, and whether
        it is open.
        """
        room = theme.density(self.settings.density)
        outer = self.centralWidget().layout()
        outer.setContentsMargins(room.margin, room.margin, room.margin, room.margin)
        outer.setSpacing(room.spacing)
        for layout in (self.action_bar_layout, self.filter_bar_layout):
            layout.setSpacing(max(4, room.spacing))
            layout.setVerticalSpacing(max(3, room.spacing - 2))
        self.metrics_bar.setContentsMargins(
            room.margin, max(2, room.cell_pad), room.margin, max(2, room.cell_pad))

        # Row height follows the density unless the user has said otherwise.
        self.settings.row_lines = self.settings.effective_row_lines
        self._apply_density(self.settings.row_lines)
        # The preview's share follows a change of density, and a first window
        # with no split saved; given on every save of the settings, as it
        # was, a preview dragged bigger was back at the density's share.
        before, self._density_shown = self._density_shown, room.name
        if before is None:
            if not self.settings.splitter_state:
                self._apply_preview_share(room)
        elif before != room.name:
            self._apply_preview_share(room)

    def _apply_preview_share(self, room) -> None:
        """Give the table everything the preview is not using. Skipped while
        the splitter has no height (during construction); showEvent runs it
        again.
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
        """Re-file the inbox's rows under the new folder plan."""
        items = self._inbox_rows()
        if not items:
            return
        for item in items:
            item.folders = self.folder_plan
            item.non_job_routing = self.settings.routing
        if self._showing_inbox():
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
                    f"{self.settings.provider_label} has no stored key. "
                    "The running scan is continuing on the previous backend.",
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
        self.search_edit.textChanged.connect(self.proxy.set_text_filter)
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.search_edit)

        self.category_filter = RoomyCombo(every=True)
        self.category_filter.setToolTip(
            "Show only one category at a time. The list is built from what "
            "this scan actually found.")
        self.category_filter.addItem("All categories", None)
        self.category_filter.currentIndexChanged.connect(
            lambda: self.proxy.set_category_filter(self.category_filter.currentData())
        )
        row.addWidget(self.category_filter)

        # One "Show" menu instead of a row of competing checkboxes.
        self.show_combo = RoomyCombo(every=True)
        self.show_combo.setToolTip(
            "Narrow the table to job mail, or to the rows you have ticked.")
        self.show_combo.addItem("Show: everything", SHOW_ALL)
        self.show_combo.addItem("Show: job mail only", SHOW_JOB_ONLY)
        self.show_combo.addItem("Show: everything but job mail",
                                SHOW_OTHER_ONLY)
        self.show_combo.addItem("Show: ticked only", SHOW_SELECTED)
        self.show_combo.setCurrentIndex(1 if self.settings.hide_non_job else 0)
        self.show_combo.currentIndexChanged.connect(self._show_filter_changed)
        row.addWidget(self.show_combo)
        self._show_filter_changed()

        # Reading one mailbox is a different question from scanning one, so it
        # has its own control.
        self.view_button = QToolButton()
        self.view_button.setToolTip(
            "Which mailboxes' messages are shown in the table.")
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_button.setProperty("menu", "true")
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
        self.columns_button.setProperty("menu", "true")
        row.addWidget(self.columns_button)
        # The table does not exist yet; both menus are filled in once it does.

        # One menu, and every entry acts on the rows the table is showing, and
        # says so.
        self.ticks_button = QToolButton()
        self.ticks_button.setText("Ticks")
        self.ticks_button.setToolTip(
            "Tick or untick the rows on screen. Changes what Apply will "
            "do.")
        self.ticks_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup)
        self.ticks_menu = QMenu(self)
        self.ticks_button.setMenu(self.ticks_menu)
        self.ticks_button.setProperty("menu", "true")
        row.addWidget(self.ticks_button)
        self._build_ticks_menu()

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

    def set_help(self, on: bool) -> None:
        """Help on or off from another window's ?, as if this one's were
        pressed: the setting is kept here."""
        self.help_button.setChecked(bool(on))

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

    def _rebuild_view_menu(self) -> None:
        """Every linked mailbox, each tickable, plus all and none. Shown
        whenever the app knows a mailbox, even one, so it is always in the
        same place.
        """
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
            # Ticked before the signal is attached: setChecked emits toggled,
            # which rebuilds this menu, which would tick it again, forever.
            action.setChecked(account_id in showing)
            action.toggled.connect(
                lambda checked, a=account_id: self._toggle_view_account(a, checked))
            self.view_menu.addAction(action)
            self._account_actions[account_id] = action
        self._refresh_view_button()

    def _linked_mailboxes(self):
        """(id, label, messages on screen) for every mailbox the app knows:
        every configured account, found in this scan or not, and any mailbox
        with messages that the settings no longer mention, so nothing is
        unreachable.
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
        # An empty filter means everything to the proxy, so a deliberate none
        # is a filter nothing matches.
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
        with_mail = [label for account, label, count in linked
                     if count and account in showing]
        if self._view_all:
            # Named when only one mailbox was scanned, so the button says
            # whose mail this is rather than "all".
            if len(linked) == 1:
                name = _mailbox_short(linked[0][1])
            elif len(with_mail) == 1:
                name = _mailbox_short(with_mail[0])
            else:
                name = "All"
        elif not showing:
            name = "None"
        elif len(showing) == 1:
            only = next((l for a, l, _n in linked if a in showing), "One")
            name = _mailbox_short(only)
        else:
            name = f"{len(showing)} of {len(linked)}"
        self.view_button.setText(menu_text(f"Mailbox: {name}"))
        self.view_button.setToolTip("Whose mail is in the table.")

    def _rebuild_columns_menu(self) -> None:
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
        """Record an explicit choice rather than reading back the table: the
        mailbox column hides itself while there is one mailbox, and reading
        that back would keep it hidden after a second is added.
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
    def _shown_rows(self) -> List[int]:
        """Source rows the table is currently showing, in view order."""
        proxy = self.proxy
        return [proxy.mapToSource(proxy.index(row, 0)).row()
                for row in range(proxy.rowCount())]

    def _ticks(self, what: str) -> None:
        """Every tick operation, scoped to what is on screen."""
        rows = self._shown_rows()
        if what == "all":
            changed = self.model.set_approved(rows, True)
            said = f"Ticked {changed} of {len(rows)} shown"
        elif what == "confident":
            self.model.set_approved(rows, False)
            changed = self.model.set_approved(
                rows, True, only_high_confidence=True)
            said = f"Ticked {changed} confident of {len(rows)} shown"
        elif what == "none":
            changed = self.model.set_approved(rows, False)
            said = f"Unticked {changed} of {len(rows)} shown"
        else:
            changed = self.model.restore_suggested_rows(rows)
            said = f"Reset {changed} of {len(rows)} shown to suggestions"
        self._set_status(said)

    def _build_ticks_menu(self) -> None:
        """Written as what it does to what you can see."""
        self.ticks_menu.clear()
        for label, what, shortcut in (
                ("Tick everything shown", "all", "Ctrl+A"),
                ("Tick only the confident ones shown", "confident",
                 "Ctrl+Shift+A"),
                ("Untick everything shown", "none", "Ctrl+D"),
                (None, None, None),
                ("Reset ticks to suggestions", "suggested", None)):
            if label is None:
                self.ticks_menu.addSeparator()
                continue
            action = QAction(label, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutContext(
                    Qt.ShortcutContext.ApplicationShortcut)
                self.addAction(action)
            action.triggered.connect(lambda _=False, w=what: self._ticks(w))
            self.ticks_menu.addAction(action)

    def _show_filter_changed(self) -> None:
        mode = self.show_combo.currentData()
        self.proxy.set_hide_non_job(mode == SHOW_JOB_ONLY)
        self.proxy.set_hide_job(mode == SHOW_OTHER_ONLY)
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
        # Not Ctrl+Shift+R, which is Reply: Qt fires neither of two duplicate
        # shortcuts.
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
        # Not Ctrl+R, which is Scan: Qt fires neither of two duplicates.
        reply_action.setShortcut(QKeySequence("Ctrl+Shift+R"))
        reply_action.setToolTip(
            "Run the auto-reply rules over what was scanned: draft, file, tick, "
            "flag or mark read, whatever they say. A rule never sends anything."
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

        file_menu.addSeparator()

        brief_action = QAction("&Briefing…", self)
        brief_action.setShortcut(QKeySequence("Ctrl+B"))
        brief_action.setStatusTip(
            "What the last scan found, in the order worth reading it: what "
            "needs you, what arrived, and where it is all going.")
        brief_action.triggered.connect(self._show_briefing)
        file_menu.addAction(brief_action)
        file_menu.addSeparator()

        clear_action = QAction("Clear out mail…", self)
        clear_action.setStatusTip(
            "Delete mail by sender, subject or age. The server gives a "
            "count before anything goes.")
        clear_action.triggered.connect(self._clear_out_mail)
        file_menu.addAction(clear_action)

        empty_action = QAction("Empty a folder…", self)
        empty_action.setStatusTip(
            "Delete everything in one folder on the server.")
        empty_action.triggered.connect(self._empty_a_folder)
        file_menu.addAction(empty_action)
        file_menu.addSeparator()

        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.setMenuRole(QAction.MenuRole.QuitRole)
        # Not QApplication.quit, which skips asking about an unapplied scan and
        # an open Settings.
        quit_action.triggered.connect(lambda: self.quit_app())
        file_menu.addAction(quit_action)

        edit_menu = menubar.addMenu("&Edit")
        # The same four things the Ticks button offers, worded and scoped the
        # same way.
        for label, what in (
            ("Tick everything shown", "all"),
            ("Tick only the confident ones shown", "confident"),
            ("Untick everything shown", "none"),
            ("Reset ticks to suggestions", "suggested"),
        ):
            action = QAction(label, self)
            action.triggered.connect(lambda _=False, w=what: self._ticks(w))
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

        # Its words say what it will do, as in Mail, which keeps the same
        # keys: Control-Command-S.
        self.sidebar_action = QAction(
            "Hide Sidebar" if self.settings.show_sidebar else "Show Sidebar", self)
        self.sidebar_action.setShortcut(QKeySequence("Meta+Ctrl+S"))
        self.sidebar_action.setStatusTip(
            "Your mailboxes down the left: the inbox, Drafts, Sent and every folder.")
        self.sidebar_action.triggered.connect(
            lambda: self._show_sidebar(not self.settings.show_sidebar))
        view_menu.addAction(self.sidebar_action)
        self.new_mail_action = QAction("Get New Mail", self)
        self.new_mail_action.setShortcut(QKeySequence("Ctrl+Shift+N"))
        self.new_mail_action.setStatusTip(
            "Look again at the mailbox on screen, and at every account's folders.")
        self.new_mail_action.triggered.connect(self._get_new_mail)
        view_menu.addAction(self.new_mail_action)
        view_menu.addSeparator()

        visualise_action = QAction("Visualise an audio file…", self)
        visualise_action.setShortcut(QKeySequence("Ctrl+Shift+V"))
        visualise_action.setStatusTip(
            "Open any sound file from this machine and watch it. Nothing "
            "is sent anywhere and nothing is kept.")
        visualise_action.triggered.connect(self._visualise_a_file)
        view_menu.addAction(visualise_action)
        view_menu.addSeparator()

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

        self._build_message_menu(menubar)

        window_menu = menubar.addMenu("&Window")
        minimise = QAction("Minimize", self)
        minimise.triggered.connect(self.showMinimized)
        window_menu.addAction(minimise)
        zoom = QAction("Zoom", self)
        zoom.triggered.connect(lambda: self.showNormal() if self.isMaximized()
                               else self.showMaximized())
        window_menu.addAction(zoom)
        window_menu.addSeparator()
        # Back from wherever it went: the menu bar item, a closed window.
        self.reveal_action = QAction("Mail Manager", self)
        self.reveal_action.setShortcut(QKeySequence("Ctrl+0"))
        self.reveal_action.triggered.connect(self._reveal)
        window_menu.addAction(self.reveal_action)
        self._give_dock_menu()

        help_menu = menubar.addMenu("&Help")
        # Named for what people come looking for: linking another mailbox is
        # ordinary, not a re-run of setup.
        setup = QAction("Add or Link &Mailboxes…", self)
        setup.setToolTip("The setup wizard: link another mailbox, or change "
                         "which folders get created.")
        setup.triggered.connect(self.run_setup)
        help_menu.addAction(setup)

        shortcuts = QAction("Keyboard &Shortcuts", self)
        shortcuts.setShortcut(QKeySequence("Ctrl+/"))
        shortcuts.triggered.connect(self._show_shortcuts)
        help_menu.addAction(shortcuts)

        check = QAction("Check for &Updates…", self)
        check.setMenuRole(QAction.MenuRole.ApplicationSpecificRole)
        check.triggered.connect(lambda: self._look_for_updates(by_hand=True))
        help_menu.addAction(check)

        about = QAction(f"About {APP_DISPLAY_NAME}", self)
        about.setMenuRole(QAction.MenuRole.AboutRole)
        about.triggered.connect(self._about)
        help_menu.addAction(about)

    def _build_message_menu(self, menubar) -> None:
        """What can be done to the selected messages, as any mail client
        offers it: written to, marked, and moved. Ticking and filing for
        the next Apply stay in the Edit menu and on the table."""
        import icons

        ink = self.palette().color(self.palette().ColorRole.WindowText).name()
        menu = menubar.addMenu("&Message")
        new_message = QAction("&New Message", self)
        new_message.setIcon(icons.icon("compose", ink))
        new_message.setShortcut(QKeySequence("Ctrl+N"))
        new_message.triggered.connect(lambda: self.compose("new"))
        menu.addAction(new_message)
        self.open_message_action = QAction("&Open Message", self)
        self.open_message_action.setIcon(icons.icon("open", ink))
        self.open_message_action.setShortcut(QKeySequence("Ctrl+O"))
        self.open_message_action.setStatusTip(
            "Read the selected message in a window of its own. A double-click "
            "or Return on the row does the same.")
        self.open_message_action.triggered.connect(self._open_selected)
        menu.addAction(self.open_message_action)
        menu.addSeparator()
        self.compose_actions: Dict[str, QAction] = {}
        for label, mode in (("&Reply", "reply"), ("Reply &All", "reply_all"),
                            ("&Forward", "forward")):
            action = QAction(label, self)
            action.setIcon(icons.icon(mode.replace("_", "-"), ink))
            action.triggered.connect(lambda checked=False, m=mode: self._compose_selected(m))
            menu.addAction(action)
            self.compose_actions[mode] = action
        menu.addSeparator()
        self.mark_read_action = QAction("Mark as Read", self)
        self.mark_read_action.setIcon(icons.icon("read", ink))
        self.mark_read_action.setShortcut(QKeySequence("Ctrl+Shift+U"))
        self.mark_read_action.triggered.connect(lambda: self._act_on_selected("read"))
        menu.addAction(self.mark_read_action)
        self.mark_flag_action = QAction("Flag", self)
        self.mark_flag_action.setIcon(icons.icon("flag", ink))
        self.mark_flag_action.setShortcut(QKeySequence("Ctrl+Shift+L"))
        self.mark_flag_action.triggered.connect(lambda: self._act_on_selected("flag"))
        menu.addAction(self.mark_flag_action)
        menu.addSeparator()
        self.quick_actions: Dict[str, QAction] = {}
        for label, what, keys in (("Archive", "archive", "Ctrl+E"),
                                  ("Move to Junk", "junk", "Ctrl+Shift+J"),
                                  ("Delete", "delete", "Ctrl+Backspace")):
            action = QAction(label, self)
            action.setIcon(icons.icon({"delete": "trash"}.get(what, what), ink))
            action.setShortcut(QKeySequence(keys))
            action.triggered.connect(lambda checked=False, w=what: self._act_on_selected(w))
            menu.addAction(action)
            self.quick_actions[what] = action
        # Kept in step with the selection, not only when the menu opens: a
        # shortcut never opens the menu, and a disabled action ignores it.
        menu.aboutToShow.connect(self._sync_message_menu)
        self.table.selectionModel().selectionChanged.connect(
            lambda *_: self._sync_message_menu())
        self.model.modelReset.connect(self._sync_message_menu)
        self.message_menu = menu
        self._sync_message_menu()

    def _sync_message_menu(self) -> None:
        """Enable what the selection allows, and word the toggles for it."""
        rows = self._selected_rows()
        items = [self.model.item_at(row) for row in rows]
        items = [item for item in items if item is not None]
        one = len(items) == 1
        self.open_message_action.setEnabled(one)
        for action in self.compose_actions.values():
            action.setEnabled(one)
        live = [item for item in items if not item.moved]
        for action in (self.mark_read_action, self.mark_flag_action,
                       *self.quick_actions.values()):
            action.setEnabled(bool(live))
        unread = any("\\seen" not in _flags_of(item) for item in live)
        self.mark_read_action.setText("Mark as Read" if unread or not live
                                      else "Mark as Unread")
        flagged = live and all("\\flagged" in _flags_of(item) for item in live)
        self.mark_flag_action.setText("Unflag" if flagged else "Flag")

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
        # A header saved while there was one mailbox would keep the Mailbox
        # column hidden after a second is added.
        self._restore_hidden_columns()
        self._sync_account_column()
        for column in range(self.model.columnCount()):
            if not self.table.isColumnHidden(column) and self.table.columnWidth(column) <= 0:
                self.table.setColumnWidth(
                    column, self.COLUMN_WIDTHS.get(column, 140))
        self._heal_column_widths()

    def showEvent(self, event) -> None:  # noqa: N802
        """Lay out what needs a height, and the first time only, run the
        first-run prompt and start the key probe and the update check.

        Tied to the show event rather than a timer queued at construction,
        which could fire after the window is gone.
        """
        super().showEvent(event)
        # The splitter only has a height once the window has been laid out.
        # A split that was saved stands; the density's share is for a window
        # that has none yet, and given every time it made the preview small
        # again on every launch.
        if not self.settings.splitter_state:
            QTimer.singleShot(0, self, lambda: self._apply_preview_share(
                theme.density(self.settings.density)))
        QTimer.singleShot(0, self, self._heal_column_widths)
        if not self._first_run_checked:
            self._first_run_checked = True
            QTimer.singleShot(0, self, self._first_run_check)
            QTimer.singleShot(0, self, self._probe_api_keys)
            QTimer.singleShot(0, self, self._open_mailboxes)
            # A little after the window is up, so it never holds it up.
            QTimer.singleShot(5000, self, self._look_for_updates)

    def _unfinished_work(self) -> str:
        """Results that quitting would lose, phrased for a person: only an
        approved scan not yet applied. A running task stops cleanly on the
        way out and costs nothing to start again.
        """
        if self.demo or self.dry_run:
            return ""
        pending = sum(
            1 for item in self._rows_in_reach()
            if item.approved and not item.moved
            and item.disposition is Disposition.MOVE
        )
        if not pending:
            return ""
        return (f"{pending} message{'s' if pending != 1 else ''} ticked and "
                "ready to file")

    def confirm_quit(self) -> bool:
        """Ask before throwing away a scan that has not been applied, or a
        message half written: its window asks, and Cancel there stops the
        quit."""
        for window in self._live_mail_windows():
            window.close()
            if shiboken6.isValid(window) and window.isVisible():
                return False
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
        if (self._quitting and not self._quit_confirmed
                and not self.confirm_quit()):
            self._quitting = False
            event.ignore()
            return
        if not self._quitting and self._hides_to_menu_bar():
            # The menu bar item stays, so closing the window puts it away; Quit
            # (menu bar, app menu or Cmd-Q) leaves.
            event.ignore()
            self._put_away()
            return
        if not self._quitting and not self.confirm_quit():
            event.ignore()
            return
        self._close_attachment_window()
        self._close_mail_windows()
        self.shutdown()
        self._save_layout()
        super().closeEvent(event)

    def _put_away(self) -> None:
        """Hide the window, leaving full screen first.

        Hiding a window that owns a full-screen space leaves the display on
        an empty desktop with no way back, which looks like a crash. Leaving
        full screen is animated, so the hide waits for it.
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
        """Remember the window's shape, closing or hiding. Not while full
        screen: the next launch would start in a full-screen space, which is
        rarely what was meant and hard to leave if anything goes wrong.
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
        if self.settings.show_sidebar and self.side_splitter.sizes()[0] > 0:
            self.settings.sidebar_width = self.side_splitter.sizes()[0]
        self.settings.hide_non_job = self.show_combo.currentData() == SHOW_JOB_ONLY
        try:
            self.settings.save()
        except OSError as exc:
            log.warning("Could not save settings: %s", exc)

    def shutdown(self) -> None:
        """Leave no thread attached to this window.

        Idempotent, and safe from ``aboutToQuit``. Anything that will not
        stop in time is detached, not terminated: see :func:`_abandon`.
        """
        self.schedule_timer.stop()
        self.menu_bar.hide()
        for worker in list(self._workers) + list(self._mailbox_workers):
            if worker.isRunning() and not worker.stop(3000):
                _abandon(worker)
        self._workers.clear()
        self._mailbox_workers.clear()
        # The update check and an update's download are threads too, parented
        # to this window: destroyed while running, Qt aborts the process.
        dialog = self._update_dialog
        fetch = dialog._fetch if dialog is not None else None
        if fetch is not None and shiboken6.isValid(fetch) and fetch.isRunning():
            fetch.installer.cancelled = True
            if not fetch.wait(3000):
                _abandon(fetch)
        look = self._update_look
        if look is not None and shiboken6.isValid(look) and look.isRunning():
            if not look.wait(3000):
                _abandon(look)

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
        for value, action in self.schedule_actions.items():
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
        """Bring the window back, whether it was minimised, hidden or closed,
        as it was: full screen stays full screen."""
        bring_forward(self)

    def _give_dock_menu(self) -> None:
        """The Dock icon's own menu, on a Mac: the windows that are open,
        then the window back, a scan and Settings, with the window closed or
        not."""
        if not hasattr(QMenu, "setAsDockMenu"):
            return
        self.dock_menu = QMenu()
        show = self.dock_menu.addAction("Show Mail Manager")
        show.triggered.connect(self._reveal)
        self.open_windows = OpenWindows.serve(self.dock_menu, show)
        scan = self.dock_menu.addAction("Scan Now")
        scan.triggered.connect(lambda: (self._reveal(), self.scan_button.click()))
        settings = self.dock_menu.addAction("Settings…")
        settings.triggered.connect(lambda: (self._reveal(), self.open_settings()))
        write = self.dock_menu.addAction("New Message")
        write.triggered.connect(lambda: self.compose("new"))
        self.dock_menu.setAsDockMenu()

    def quit_app(self, before=None) -> None:
        """Leave for good, rather than hiding to the menu bar. ``before`` is
        done once leaving is certain."""
        if not self._close_settings_first():
            return
        if not self.confirm_quit():
            return
        if before is not None:
            before()
        self._quitting = True
        # Answered. Closing the window below asked the same question again
        # when a scan was ready to file.
        self._quit_confirmed = True
        if self.isFullScreen():
            self.setWindowState(
                self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.showNormal()
        self.close()
        QApplication.quit()

    def _close_settings_first(self) -> bool:
        """Deal with an open Settings window before leaving. False = stay.

        Settings is modal and owns the keyboard, so Cmd-Q never reaches this
        window. Rather than ignore it, ask whether to keep the changes, and
        carry that out.
        """
        dialog = self._settings_dialog
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
                "Files high confidence job mail only. Needs Review and "
                "everything else waits for you."
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
                                sample_items=self._inbox_rows())
        if tab:
            dialog.tabs.setCurrentIndex(tab)
        # Remembered so Quit can deal with it: with modal Settings open, Cmd-Q
        # goes to the dialog and the app looks hung.
        self._settings_dialog = dialog
        try:
            outcome = dialog.exec()
        finally:
            self._settings_dialog = None
            # Parented to the window, the dialog would otherwise outlive every
            # visit (about 370 widgets each), and apply_appearance restyles
            # every live widget, so each copy slows every later repaint.
            # deleteLater only queues the deletion, so the code below can still
            # read the dialog.
            dialog.deleteLater()
        if outcome != QDialog.DialogCode.Accepted:
            # Undo a live preview, if there was one: restyling the whole app
            # for nothing made every Cancel take a moment.
            if dialog.previewed:
                self.apply_appearance()
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
        mailboxes_were = self._mailbox_shape()
        self.settings = new_settings
        try:
            self.settings.save()
        except OSError as exc:
            QMessageBox.warning(self, "Settings", f"Could not save settings: {exc}")

        self.folder_plan = self.settings.folder_plan()
        # Previewed while the dialog was open, and applied here too: anything
        # without a preview, such as row height, would otherwise be saved and
        # ignored.
        self.apply_appearance()
        self.confidence_delegate.threshold = self.settings.confidence_threshold
        self.table.viewport().update()
        self._rebuild_model_menu()
        self._rebuild_account_menu()
        self._rebuild_view_menu()
        self._sync_account_column()
        self._mailboxes_changed(mailboxes_were)
        self._append_log("Settings saved.")
        self._update_status()

    def _mailbox_shape(self) -> tuple:
        """What the mailboxes listed depend on: each account's server, the
        mailbox sorted and whether it has a password, and the period."""
        def has_password(account) -> bool:
            try:
                return bool(self.store.get_mailbox_password(account.address))
            except CredentialError:
                return False

        return (tuple((account.id, account.address, account.host, account.port,
                       account.source_mailbox, has_password(account))
                      for account in self.settings.accounts),
                self.settings.open_with_inbox, self.settings.inbox_days)

    def _mailboxes_changed(self, before: tuple) -> None:
        """After Settings: read again what the change touched. A mailbox
        added or taken away, a password stored, or a new period, lists the
        folders and the inbox again; the scan at launch is not run again."""
        self.sidebar.set_accounts(self.settings.accounts)
        if not self._mailboxes_opened or self.demo or self._mailbox_shape() == before:
            return
        known = {account.id for account in self.settings.accounts}
        for running in self._listings.values():
            for worker in running:
                worker.cancel()
        self._listings.clear()
        self._listed.clear()
        self._present.clear()
        self._sorted = [row for row in self._sorted if row.email.account_id in known]
        self._elsewhere.clear()
        passwords = self._stored_passwords()
        for account in self.settings.accounts:
            if account.id in passwords:
                self._list_mailboxes(account, passwords[account.id])
        if self.settings.open_with_inbox:
            self._list(INBOX, passwords)
        if self._showing_inbox():
            self._show_inbox()
        else:
            self._go_to(INBOX)

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

        # Three phrasings, longest first: the toolbar is full, and this part
        # can lose words without becoming unclear.
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
        wordings = self._window_wordings
        if not wordings:
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
                    or widget is self.range_stack):
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

    @Slot()
    def rescan_everything(self) -> None:
        """Scan without reusing anything kept from a previous run: for a change
        on the provider's side that the recipe hash cannot see.
        """
        self._begin_scan(reuse_verdicts=False)

    @Slot()
    def start_scan(self) -> None:
        # No parameters: clicked and triggered both emit a bool, which would
        # arrive as the first argument and turn the cache off.
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
                "Add one in Settings, or pick On this Mac or Local rules "
                "from the ⚙︎ button.",
            )
            self.open_settings(tab=1)
            return

        start, end = self._current_window()
        if not self._showing_inbox():
            self._go_to(INBOX)
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
        self._sorted = list(items)
        self.sidebar.choose(INBOX)
        self.model.set_addressed(False)
        self.model.set_items(self._inbox_rows())
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
        # Kept for the briefing, which says what window it reports on.
        self._scanned_window = (outcome.window_start, outcome.window_end)
        # Over the listing: messages outside this scan's window stay in the
        # table as they were listed.
        self._sorted = list(outcome.items)
        if self._showing_inbox():
            self.model.set_addressed(False)
            self.model.set_items(self._inbox_rows())
            self._view_accounts = []
            self._view_all = True
            self.sidebar.choose(INBOX)
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
        summary = self._inbox_summary()
        if not outcome.items:
            self._set_status("No messages found in this window.")
        else:
            self._set_status(summary.describe())
            if self._showing_inbox():
                self._select_first_row()

        if outcome.warnings:
            QMessageBox.information(self, "Scan notes", "\n\n".join(outcome.warnings))
        self._update_status()

        # The rules run after a scan, queued so the table is on screen before
        # they start.
        if outcome.items and self.settings.replies_armed:
            QTimer.singleShot(0, lambda: self.draft_replies(prompted=False))

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
        batch = self._undoing
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
        """Run the reply rules over what was scanned. ``prompted`` is False
        when a scan started this, which keeps the result in the status line
        rather than a box.
        """
        self._replies_prompted = prompted
        if self._busy():
            return
        if not self.settings.replies_armed:
            QMessageBox.information(
                self, "Auto reply is off",
                "No reply rules are switched on.\n\nSettings → Auto "
                "Reply has rules to start from.",
            )
            self.open_settings(tab=3)
            return
        items = [i for i in self._sorted if not i.classification.error]
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
        if self._replies_prompted:
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
        items = self._rows_in_reach()
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
        message = self._record_moves(report)
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

    def _record_moves(self, report: MoveReport, among=None) -> str:
        """Carry a move report into the table and the undo stack; returns
        the one-line account of it."""
        # Where everything came from, so it can be put back: undo is what makes
        # moving real mail safe to try.
        plans: List[MovePlan] = []
        held = self._rows_in_reach() if among is None else list(among)
        for item in held:
            account_id, home = self._home_of(item)
            filed_to, _why, landed = report.about(account_id, home,
                                                  item.email.uid)
            # The UID the message has now, after the COPY. Without the server's
            # receipt it cannot be named, so it is left out rather than pointed
            # at whatever holds that number.
            if not filed_to or not landed:
                continue
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
        mark_moves(held, report, where=self._home_of)
        # Gone from the inbox: never listed again as if still there.
        for item in held:
            if item.moved:
                self._listed.pop(message_key(item), None)
        self.model.refresh()
        self._refresh_folder_choices()
        self._refresh_mail_windows()
        message = f"Filed {report.moved_count} message(s)."
        if report.failed:
            message += f" {report.failed_count} could not be moved."
        self._append_log(message)
        return message

    def _home_of(self, item: TriageItem) -> Tuple[str, str]:
        """The mailbox and folder a row's message is filed from, named as
        the filing worker names them."""
        account = (self.settings.account_by_id(item.email.account_id)
                   or self.settings.primary_account)
        if account is None:
            return item.email.account_id, item.email.source_folder or "INBOX"
        return account.id, item.email.source_folder or account.source_mailbox

    def _inbox_rows(self) -> List[TriageItem]:
        """The inbox, one row per message: the last scan's reading of each
        message it reached, and the listing of the rest."""
        return one_row_each(self._listed.values(), self._sorted)

    def _rows_in_reach(self) -> List[TriageItem]:
        """Every row the window holds: the inbox's, and the table's when it
        shows another mailbox. A row from elsewhere is never fileable, so
        filing over all of them files the inbox's alone; a move made from
        the table is found wherever it was made."""
        rows = self._inbox_rows()
        seen = {id(row) for row in rows}
        return rows + [row for row in self.model.items if id(row) not in seen]

    def _inbox_summary(self) -> TriageSummary:
        return TriageSummary.build(self._rows_in_reach())

    def _showing_inbox(self) -> bool:
        return self.sidebar.place().is_inbox

    def _show_inbox(self) -> None:
        """Put the inbox in the table, filtered to the account chosen in
        the sidebar if one is."""
        place = self.sidebar.place()
        self.model.set_addressed(False)
        self.model.set_items(self._inbox_rows())
        self._set_view_accounts([place.account_id] if place.account_id else [])
        self._after_rows_changed()

    def _after_rows_changed(self) -> None:
        self._sync_account_column()
        self._rebuild_view_menu()
        self._refresh_category_filter()
        self._refresh_folder_choices()
        self._sync_table_stack()
        self._update_status()

    @Slot(object)
    def _chosen(self, place: Place) -> None:
        """A mailbox chosen by hand: shown as last listed, then looked at
        again for what has come and gone, as Mail does."""
        self._go_to(place)
        self._refresh(place)

    def _go_to(self, place: Place) -> None:
        """Show one mailbox as it was last listed."""
        self.sidebar.choose(place)
        self.preview.clear()
        if place.is_inbox:
            self._show_inbox()
            self._select_first_row()
            return
        self.model.set_addressed(place.kind in ("sent", "drafts"))
        self.model.set_items(list(self._elsewhere.get(place, {}).values()))
        self._set_view_accounts([])
        self._after_rows_changed()
        self._select_first_row()

    def _move_folder(self, account_id: str, folder: str, into: str) -> None:
        """Move a folder, with everything in it, into another or to the top
        level, on the server, as dragging it does in Mail."""
        if self.demo:
            self._set_status("The demo's mailboxes are not real: nothing was moved.")
            return
        if self._busy():
            return
        account = next((a for a in self.settings.accounts if a.id == account_id),
                       None)
        if account is None:
            return
        password = self._password_for(account)
        if password is None:
            return
        worker = FolderMoveWorker(account, password, folder, into, parent=self)
        worker.moved.connect(lambda old, new, a=account, p=password:
                             self._folder_moved(a, p, old, new))
        worker.failed.connect(self._on_failed)
        self._register(worker)
        self._set_status(f"Moving “{folder}”…")
        worker.start()

    def _folder_moved(self, account, password: str, old: str, new: str) -> None:
        """A folder is somewhere else now: what the window kept under its
        old name is let go, the sidebar is listed again, and a folder on
        show is followed to where it went."""
        self._append_log(f"{account.label}: moved “{old}” to “{new}”.")
        self._set_status(f"Moved “{old}” to “{new}”.")
        delimiter = self.sidebar.delimiter_of(account.id)

        def renamed(name: str) -> Optional[str]:
            if name == old or name.startswith(old + delimiter):
                return new + name[len(old):]
            return None

        for place in [p for p in self._elsewhere
                      if p.kind == "folder" and p.account_id == account.id
                      and renamed(p.folder) is not None]:
            del self._elsewhere[place]
        shown = self.sidebar.place()
        follow = (Place("folder", account.id, renamed(shown.folder))
                  if shown.kind == "folder" and shown.account_id == account.id
                  and renamed(shown.folder) is not None else None)
        worker = MailboxesWorker(account, password, parent=self)
        worker.finished_ok.connect(self.sidebar.set_mailboxes)
        if follow is not None:
            worker.finished_ok.connect(lambda _found, place=follow: self._chosen(place))
        worker.failed.connect(lambda _title, detail, a=account: self._append_log(
            f"{a.label}: could not list its mailboxes ({detail})."))
        self._read_quietly(worker)

    def _refresh(self, place: Optional[Place] = None) -> None:
        """Look at a mailbox again: only what is new is read, and what has
        gone is taken out. Not before the window has opened its mailboxes,
        and never in the demo."""
        place = place or self.sidebar.place()
        if self._mailboxes_opened and not self.demo and place not in self._listings:
            self._list(place)

    def _get_new_mail(self) -> None:
        """Get New Mail: the mailbox on screen again, and every account's
        folders."""
        if not self._mailboxes_opened or self.demo:
            return
        passwords = self._stored_passwords()
        for account in self.settings.accounts:
            if account.id in passwords:
                self._list_mailboxes(account, passwords[account.id])
        place = self.sidebar.place()
        if place not in self._listings:
            self._list(place, passwords)

    def _known_uids(self, place: Place, account_id: str, folder: str) -> List[str]:
        """The UIDs one folder of one account held at the last look, and any
        listed since: a look again need not read them."""
        rows = (self._listed.values() if place.is_inbox
                else self._elsewhere.get(place, {}).values())
        listed = {row.email.uid for row in rows
                  if row.email.account_id == account_id
                  and (row.email.source_folder or "INBOX") == folder}
        return sorted(listed | self._present.get(
            (self._listing_of(place), account_id, folder), set()))

    @staticmethod
    def _listing_of(place: Place):
        """Which listing a place shows: every inbox place shows the one."""
        return INBOX if place.is_inbox else place

    def _paint_sidebar_button(self) -> None:
        import icons
        from PySide6.QtGui import QPalette

        self.sidebar_button.setIcon(icons.icon(
            "sidebar", self.palette().color(QPalette.ColorRole.ButtonText).name(), 18))

    def _show_sidebar(self, on: bool) -> None:
        """Show or hide the mailboxes, from the View menu or the toolbar."""
        on = bool(on)
        self.sidebar.setVisible(on)
        self.sidebar_action.setText("Hide Sidebar" if on else "Show Sidebar")
        self.sidebar_button.blockSignals(True)
        self.sidebar_button.setChecked(on)
        self.sidebar_button.blockSignals(False)
        if self.settings.show_sidebar != on:
            self.settings.show_sidebar = on
            try:
                self.settings.save()
            except OSError as exc:
                log.warning("Could not save settings: %s", exc)

    def _stored_passwords(self) -> Dict[str, str]:
        """Every account's app password, read without asking anything: an
        account without one, or a Keychain that will not say, is left out
        and the log says so."""
        found: Dict[str, str] = {}
        for account in self.settings.accounts:
            try:
                secret = self.store.get_mailbox_password(account.address)
            except CredentialError as exc:
                self._append_log(f"{account.label}: the Keychain would not give "
                                 f"its password ({exc}).")
                continue
            if secret:
                found[account.id] = secret
            else:
                self._append_log(f"{account.label}: no app password is stored; "
                                 "it is not listed.")
        return found

    def _open_mailboxes(self) -> None:
        """Once, when the window first shows: each account's folders for the
        sidebar, the inbox for the table, then a scan if one is wanted at
        launch. Nothing here asks anything; what is missing is said in the
        log and the status bar."""
        if self._mailboxes_opened or self.demo:
            return
        self._mailboxes_opened = True
        self.sidebar.set_accounts(self.settings.accounts)
        if not self.settings.is_configured():
            return
        passwords = self._stored_passwords()
        for account in self.settings.accounts:
            if account.id in passwords:
                self._list_mailboxes(account, passwords[account.id])
        if self.settings.open_with_inbox and self._list(INBOX, passwords):
            # The scan waits for the listing: both read the same messages,
            # and the listing is what fills the window.
            self._scan_after_listing = self.settings.scan_on_open
        elif self.settings.scan_on_open:
            self._scan_on_open()

    def _list_mailboxes(self, account, password: str) -> None:
        worker = MailboxesWorker(account, password, parent=self)
        worker.finished_ok.connect(self.sidebar.set_mailboxes)
        worker.failed.connect(lambda _title, detail, a=account: self._append_log(
            f"{a.label}: could not list its mailboxes ({detail})."))
        self._read_quietly(worker)

    def _read_quietly(self, worker: QThread) -> None:
        """Start a worker that only reads: it never makes the window busy,
        so Scan is never refused because of it; quitting still stops it."""
        self._mailbox_workers.append(worker)

        def ended(w=worker) -> None:
            if w in self._mailbox_workers:
                self._mailbox_workers.remove(w)
            w.deleteLater()

        worker.finished.connect(ended)
        worker.start()

    def _inbox_since(self) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=self.settings.inbox_days)

    def _list(self, place: Place, passwords: Optional[Dict[str, str]] = None) -> bool:
        """List what a place holds over the inbox period, account by account,
        filling the table as the rows come. Returns whether anything was
        started."""
        if passwords is None:
            passwords = self._stored_passwords()
        reach = [(account, folder) for account, folder in
                 accounts_having(self.sidebar, place, self.settings.accounts)
                 if account.id in passwords]
        if not reach:
            return False
        since = self._inbox_since()
        plan = self.folder_plan or self.settings.folder_plan()
        running: List[QThread] = []
        if not place.is_inbox:
            self._elsewhere.setdefault(place, {})
        for account, folder in reach:
            worker = ListWorker(account, passwords[account.id], folder, since,
                                self.settings, plan, fileable=place.is_inbox,
                                known=self._known_uids(place, account.id, folder),
                                parent=self)
            worker.arrived.connect(
                lambda rows, p=place, w=worker: self._on_listed(p, rows, w))
            worker.finished_ok.connect(
                lambda listing, p=place: self._on_listing_done(p, listing))
            worker.failed.connect(
                lambda _title, detail, p=place, a=account:
                    self._on_listing_failed(p, a, detail))
            worker.finished.connect(
                lambda p=place, w=worker: self._listing_ended(p, w))
            running.append(worker)
        self._listings[place] = running
        self._set_status(f"Reading {self.sidebar.title_of(place)}…")
        for worker in running:
            self._read_quietly(worker)
        self._sync_table_stack()
        return True

    def _on_listed(self, place: Place, rows: List[TriageItem],
                   worker: Optional[QThread] = None) -> None:
        """A batch of a listing: kept, and shown at once if its mailbox is
        the one on screen and the message is not already there. A listing
        replaced since says nothing."""
        if worker is not None and worker not in self._listings.get(place, []):
            return
        if place.is_inbox:
            for row in rows:
                self._listed[message_key(row)] = row
        else:
            kept = self._elsewhere.setdefault(place, {})
            for row in rows:
                kept[message_key(row)] = row
        if place.is_inbox != self._showing_inbox() or (
                not place.is_inbox and self.sidebar.place() != place):
            return
        shown = {message_key(item) for item in self.model.items}
        fresh = [row for row in rows if message_key(row) not in shown]
        if fresh:
            self.model.add_items(fresh)
            if len(shown) == 0:
                self._after_rows_changed()
                self._select_first_row()

    def _on_listing_done(self, place: Place, listing) -> None:
        """A listing of one folder of one account finished: what is no longer
        there goes, from the list kept and from the table."""
        present = set(listing.present)
        self._present[(self._listing_of(place), listing.account_id,
                       listing.folder)] = present

        def gone(row) -> bool:
            return (row.email.account_id == listing.account_id
                    and (row.email.source_folder or "INBOX") == listing.folder
                    and row.email.uid not in present)

        if place.is_inbox:
            vanished = {key for key, row in self._listed.items() if gone(row)}
            for key in vanished:
                del self._listed[key]
            if vanished and self._showing_inbox():
                # A message a scan read stays as the scan read it.
                self.model.remove_where(
                    lambda row: not row.analysed and message_key(row) in vanished)
        else:
            self._elsewhere[place] = {
                key: row for key, row in self._elsewhere.get(place, {}).items()
                if not gone(row)}
            if self.sidebar.place() == place:
                self.model.remove_where(gone)
        account = self.settings.account_by_id(listing.account_id)
        name = account.label if account else listing.account_id
        note = (f"{name}: listed {len(listing.rows)} message(s) in "
                f"{listing.folder} from the last {self._period_words()}.")
        if listing.capped:
            note += " The period holds more; these are the newest."
        self._append_log(note)

    def _on_listing_failed(self, place: Place, account, detail: str) -> None:
        self._append_log(f"{account.label}: could not read "
                         f"{self.sidebar.title_of(place)} ({detail}).")
        self._set_status(f"Could not read {self.sidebar.title_of(place)} in "
                         f"{account.label}: {detail}")

    def _listing_ended(self, place: Place, worker: QThread) -> None:
        running = self._listings.get(place)
        if running is None or worker not in running:
            return      # replaced by a newer listing, which speaks for itself
        running.remove(worker)
        if running:
            return
        self._listings.pop(place, None)
        if self._showing(place):
            self._after_rows_changed()
        if place.is_inbox and self._scan_after_listing:
            self._scan_after_listing = False
            self._scan_on_open()

    def _showing(self, place: Place) -> bool:
        current = self.sidebar.place()
        return current == place or (current.is_inbox and place.is_inbox)

    def _period_words(self) -> str:
        from config import INBOX_PERIODS

        return dict(INBOX_PERIODS).get(self.settings.inbox_days,
                                       f"{self.settings.inbox_days} days")

    def _why_not_scan_on_open(self) -> str:
        """Why a scan at launch would have to ask something first, or empty
        when it would not."""
        if not self.settings.is_configured():
            return "no mailbox is set up yet"
        try:
            missing = self._mailboxes_missing_a_password()
            key = self.store.get_provider_key(self.settings.provider)
        except CredentialError as exc:
            return f"the Keychain would not answer ({exc})"
        if missing:
            return "no app password is stored for " + ", ".join(missing)
        if self.settings.needs_api_key and not key:
            return f"no {self.settings.provider_label} API key is stored"
        return ""

    def _scan_on_open(self) -> None:
        """Auto scan: Scan & Analyze as the app opens, when nothing needs
        asking first. Once a launch, whether it starts with the window or in
        the menu bar with the window opened later."""
        if self._scanned_at_launch:
            return
        self._scanned_at_launch = True
        why = self._why_not_scan_on_open()
        if why:
            note = f"Auto scan did not run: {why}."
            self._append_log(note)
            self._set_status(note)
            return
        if self.running_workers():
            return
        self._append_log("Auto scan: scanning as the app opens.")
        self._begin_scan()

    def _register(self, worker: QThread) -> QThread:
        """Track a worker and reap it when it finishes. Otherwise finished
        QThreads pile up for the session, and one still running at quit is
        orphaned.
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
        """Stop every background task and drop its connections. Returns how
        many were running; safe with nothing running, and safe twice.
        """
        running = self.running_workers()
        # The quiet readers too: Stop All means all. They are not tasks to
        # count or wait for, only to end.
        for reader in list(self._mailbox_workers):
            reader.cancel()
        self._scan_after_listing = False
        if not running:
            self._set_status("Nothing is running.")
            return 0

        names = ", ".join(sorted({w.task_name for w in running}))
        self._append_log(f"Stopping {len(running)} task(s): {names}…")

        # Tell every worker to stop before waiting on any, and paint the
        # stopped state at once: a progress bar still moving during the wait
        # would contradict Stop.
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
        self.progress.setFormat(self._fitted_progress(message, False))
        self.progress.setToolTip(message)
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
        # The primary button becomes Stop while work runs, so it is always in
        # the same place and never greyed out when it is wanted most.
        self._set_scan_button(busy)
        self.apply_button.setEnabled(not busy and self._inbox_summary().approved > 0)
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
    def _fitted_progress(self, message: str, counts: bool) -> str:
        """As much of ``message`` as the progress bar can hold.

        Qt does not cut text inside a progress bar; it centres it and lets
        it run past the edges, and these messages are about twice the bar's
        320 px. The counts are expanded at paint time, so their room is
        measured from the numbers rather than "%v".
        """
        metrics = QFontMetrics(self.progress.font())
        head = "%v / %m - " if counts else ""
        spent = metrics.horizontalAdvance(
            head.replace("%v", str(self.progress.value()))
                .replace("%m", str(self.progress.maximum())))
        room = max(24, self.progress.width() - 16 - spent)
        return head + metrics.elidedText(
            message, Qt.TextElideMode.ElideRight, room)

    def _on_progress(self, done: int, total: int, message: str) -> None:
        if not self.running_workers():
            return                      # a late signal from a stopped worker
        if total > 0:
            self.progress.setRange(0, total)
            self.progress.setValue(min(done, total))
            self.progress.setFormat(self._fitted_progress(message, True))
        else:
            self.progress.setRange(0, 0)
            self.progress.setFormat(self._fitted_progress(message, False))
        self.progress.setToolTip(message)
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
        self.read_whole(item)
        self.preview.show_item(row, item, self._prompt_for(item))

    @staticmethod
    def _whole_key(message) -> tuple:
        return (message.account_id, message.source_folder, message.uid)

    #: How long after failing to read a message's whole text it is tried
    #: again, when it is shown again, in seconds.
    WHOLE_RETRY = 60.0

    def read_whole(self, item) -> None:
        """Read again a message a scan read only the start of, when it is
        shown, so what is on screen is all of it: the scan's partial read
        stops partway through a long message's HTML. Once a message: and
        after a failure, not again for a while."""
        import time

        message = item.email
        key = self._whole_key(message)
        if (not message.truncated or self.demo or key in self._reading_whole
                or key in self._whole_read):
            return
        failed = self._whole_failed.get(key)
        if failed is not None and time.monotonic() - failed[1] < self.WHOLE_RETRY:
            return
        account = next((a for a in self.settings.accounts
                        if a.id == message.account_id), None) or next(
            iter(self.settings.accounts), None)
        password = ""
        if account is not None:
            try:
                password = self.store.get_mailbox_password(account.address)
            except CredentialError as exc:
                self._whole_failed[key] = (str(exc), time.monotonic())
                return
        if account is None or not password:
            self._whole_failed[key] = ("no password is saved for its mailbox",
                                       time.monotonic())
            return
        worker = WholeMessageWorker(account, password, message, parent=self)
        worker.arrived.connect(
            lambda whole, k=key, m=message: self._whole_arrived(k, m, whole))
        worker.failed.connect(
            lambda _title, detail, k=key: self._whole_missing(k, detail))
        self._reading_whole.add(key)
        self._read_quietly(worker)

    def missing_from(self, item) -> str:
        """What a message on screen lacks, in words to put under it."""
        message = item.email
        if not message.truncated:
            return ""
        key = self._whole_key(message)
        if key in self._reading_whole:
            return "This is the start of the message. The rest is on its way."
        if key in self._whole_failed:
            return ("This is only the start of the message: the rest could "
                    f"not be read ({self._whole_failed[key][0]}).")
        return "This is only the start of the message."

    def _whole_arrived(self, key: tuple, asked, whole) -> None:
        self._reading_whole.discard(key)
        self._whole_failed.pop(key, None)
        if whole is None:
            self._whole_missing(key, "the server no longer has it")
            return
        shown = [item for item in self._rows_in_reach()
                 if self._whole_key(item.email) == key]
        for message in {id(m): m for m in [asked] + [i.email for i in shown]}.values():
            message.body_text = whole.body_text
            message.body_html = whole.body_html
            message.links = whole.links
            if whole.attachments:
                message.attachments = whole.attachments
            message.truncated = whole.truncated
        self._whole_read.add(key)
        self._show_again(key)

    def _whole_missing(self, key: tuple, detail: str) -> None:
        import time

        self._reading_whole.discard(key)
        self._whole_failed[key] = (detail, time.monotonic())
        self._append_log(f"Could not read the rest of a message: {detail}")
        self._show_again(key)

    def _show_again(self, key: tuple) -> None:
        """A message drawn again wherever it is on show."""
        from mail_window import MessageWindow

        current = self.preview._item
        if current is not None and self._whole_key(current.email) == key:
            self.preview.refresh_body(current)
        for window in self._live_mail_windows():
            if isinstance(window, MessageWindow):
                item = window.item()
                if item is not None and self._whole_key(item.email) == key:
                    window.show_row(window.row)

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
        """Right-click on the grid: act on everything selected.

        Built here and shown by the caller, so a test can read the menu
        without a native modal loop.
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

        if not many:
            open_action = menu.addAction("Open in a Window")
            open_action.triggered.connect(lambda: self.open_message(rows[0]))
            for label, mode in (("Reply", "reply"), ("Reply All", "reply_all"),
                                ("Forward", "forward")):
                action = menu.addAction(label)
                action.triggered.connect(
                    lambda checked=False, m=mode: self.compose(m, rows[0]))
            menu.addSeparator()
        live = [item for item in items if not item.moved]
        unread = any("\\seen" not in _flags_of(item) for item in live)
        read = menu.addAction("Mark as Read" if unread else "Mark as Unread")
        read.triggered.connect(
            lambda: self.act_on_rows("read" if unread else "unread", rows))
        flagged = bool(live) and all("\\flagged" in _flags_of(item) for item in live)
        flag = menu.addAction("Unflag" if flagged else "Flag")
        flag.triggered.connect(
            lambda: self.act_on_rows("unflag" if flagged else "flag", rows))
        quick = []
        for label, what in (("Archive", "archive"), ("Move to Junk", "junk"),
                            ("Delete", "delete")):
            action = menu.addAction(f"{label} now" if label != "Move to Junk"
                                    else "Move to Junk now")
            action.triggered.connect(
                lambda checked=False, w=what: self.act_on_rows(w, rows))
            quick.append(action)
        for action in (read, flag, *quick):
            action.setEnabled(bool(live))
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
        """Right-click on the header: show, hide and reset the columns. The
        reset matters most: dragging a column to nothing takes one careless
        movement.
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
        """Record that this sender's mail belongs elsewhere.

        Best effort: failing to record it is not worth interrupting triage,
        and the correction still applies to the message in front of them.
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
        # A row not sorted yet carries an empty verdict, which names no
        # category it has.
        read = [item for item in self.model.items if item.analysed]
        labels = sorted({item.classification.category_label for item in read})
        colors = {
            item.classification.category_label: category_color(
                item.classification, item)
            for item in read
        }
        self.category_filter.blockSignals(True)
        self.category_filter.clear()
        self.category_filter.addItem("All categories", None)
        for label in labels:
            self.category_filter.addItem(_swatch(colors.get(label, OTHER_COLOR)), label, label)
        if len(read) < len(self.model.items):
            self.category_filter.addItem(_swatch(OTHER_COLOR), NOT_SORTED, NOT_SORTED)
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
        """Refresh the apply button and the status bar. ``message`` pins a
        line, such as an apply result, that the counts would otherwise
        overwrite.
        """
        summary = self._inbox_summary()
        running = bool(self.running_workers())
        self.apply_button.setEnabled(summary.approved > 0 and not running)
        # Short enough to keep the toolbar on one row; the full wording is the
        # tooltip.
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
        self._set_scan_button(running)
        self.stop_action.setEnabled(running)
        if message is not None:
            self._set_status(message)
        elif not running and not self._showing_inbox():
            place = self.sidebar.place()
            count = self.model.rowCount()
            reading = " (still reading)" if place in self._listings else ""
            self._set_status(
                f"{self.sidebar.title_of(place)}: {count} message"
                f"{'s' if count != 1 else ''} from the last "
                f"{self._period_words()}{reading}.")
        elif summary.total and not running:
            self._set_status(summary.describe())

    def _set_density(self, lines: int) -> None:
        self._apply_density(lines)
        try:
            self.settings.save()
        except OSError:
            pass
        # The rows are only drawn taller or shorter. A bare layoutChanged here
        # told the filter the rows had moved without warning it first: it
        # freed its map and left the table's current row pointing into it,
        # and the next click on a message crashed the app.

    @Slot()
    def _sync_table_stack(self) -> None:
        """Show the grid, or why it is empty: nothing scanned, nothing found,
        or everything filtered away. Only the last has a fix to press, so it
        gets a button.
        """
        if self.proxy.rowCount():
            self.table_stack.setCurrentIndex(1)
            return
        self.table_stack.setCurrentIndex(0)
        if not self.model.rowCount():
            self.empty_label.setText(self._empty_words())
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

    def _empty_words(self) -> str:
        """Why the table is empty, for the mailbox on screen."""
        place = self.sidebar.place()

        def say(title: str, detail: str) -> str:
            return ("<div style='text-align:center;line-height:170%'>"
                    f"<span style='font-size:15px'><b>{_html(title)}</b></span><br>"
                    f"<span style='opacity:0.7'>{_html(detail)}</span></div>")

        name = self.sidebar.title_of(place)
        if any(self._showing(listed) for listed in self._listings):
            return say(f"Reading {name}\u2026",
                       f"The last {self._period_words()} of it, newest first. "
                       "Nothing is marked as read.")
        if not place.is_inbox:
            return say(f"No mail in {name}",
                       f"Nothing in the last {self._period_words()}.")
        if self._has_scanned:
            return NOTHING_FOUND
        if self._mailboxes_opened and self.settings.open_with_inbox \
                and self.settings.is_configured():
            return say("Your inbox is empty",
                       f"Nothing arrived in the last {self._period_words()}.")
        return EMPTY_STATE

    def _set_status(self, message: str) -> None:
        """Show as much of `message` as fits; the rest lives in the tooltip.

        The label does the cutting, and does it again whenever the layout
        changes its width - see ``ElidingLabel``.
        """
        self._status_text = message
        self.status_label.setText(message)
        self.status_label.setToolTip(message)

    def resizeEvent(self, event) -> None:  # noqa: N802
        # All three re-fit something to the new width; as two resizeEvents,
        # only the second ran.
        super().resizeEvent(event)
        self._fit_window_label()
        if self._status_text:
            self._set_status(self._status_text)
        self._apply_preview_position(self.settings.preview_position)

    def _toggle_log(self, visible: bool) -> None:
        self.log_view.setVisible(visible)
        if visible:
            sizes = self.outer_splitter.sizes()
            if sizes[-1] < 60:
                self.outer_splitter.setSizes([max(240, sizes[0] - 140), 140])

    #: How wide the window must be for the preview to sit beside the table. At
    #: 800 px the preview would get about 306, too little for a folder path and
    #: a button on one line. The setting is kept either way, so widening the
    #: window puts the preview back.
    BESIDE_NEEDS = 1100

    def _apply_preview_position(self, position: str) -> None:
        """Put the preview under the table or beside it: under on a laptop,
        where height is scarce; beside on a wide screen, where the table
        cannot use the width.
        """
        beside = position == "right" and self.width() >= self.BESIDE_NEEDS
        wanted = Qt.Orientation.Horizontal if beside else Qt.Orientation.Vertical
        turned = self.splitter.orientation() != wanted
        self.splitter.setOrientation(wanted)
        span = self.splitter.width() if beside else self.splitter.height()
        # A fresh share when the preview changes sides, or has had none yet
        # and none was saved. Given on every resize, as it was, a preview
        # dragged bigger was back at this share a moment later.
        if span > 1 and (turned or not (self._preview_split
                                        or self.settings.splitter_state)):
            share = 0.42 if beside else 0.40
            self.splitter.setSizes(
                [int(span * (1 - share)), int(span * share)])
            self._preview_split = True
        for value, action in self.preview_actions.items():
            # The choice, not where it ended up: a narrow window puts the
            # preview below whatever was chosen.
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
        path, _ = widgets.save_file(self, "Export results", suggested, filter_text)
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
        """Put every filter back to everything. The mailbox filter lives only
        in the proxy (set from the View menu), so the proxy is told
        separately.
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
            ("⌘N", "New message"),
            ("⌘O", "Open the selected message in its own window"),
            ("⌘E / ⌘⌫", "Archive / delete the selected messages"),
            ("⌘⇧U / ⌘⇧L", "Mark as read or unread / flag or unflag"),
            ("In a message window", ""),
            ("⌘R / ⌘⇧R / ⌘⇧F", "Reply / reply all / forward"),
            ("⌘↑ / ⌘↓", "Previous / next message"),
            ("While writing", ""),
            ("⌘↩", "Send"),
            ("⌘S", "Save as a draft"),
            ("⌘B / ⌘I / ⌘U", "Bold / italic / underline"),
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
    def _visualise_a_file(self) -> None:
        """The visualiser window, for the person's own music. One at a time,
        never alongside an attachment window: the same class, and two would
        fight over the audio device and the keyboard.
        """
        from PySide6.QtWidgets import QMessageBox

        from attachment_view import AttachmentViewer

        existing = self._visualiser_window
        if existing is not None and shiboken6.isValid(existing):
            existing.show()
            existing.raise_()
            existing.activateWindow()
            return

        attached = self._attachment_window
        if attached is not None and shiboken6.isValid(attached) and attached.isVisible():
            QMessageBox.information(
                self, "Attachments are open",
                "Close the attachment window first. Both play sound, and "
                "two of them at once fight over the speakers.")
            attached.raise_()
            return

        window = AttachmentViewer([], "", self, library=True)
        self._visualiser_window = window
        window.finished.connect(lambda *_: self._forget_visualiser())
        window.show()
        window.raise_()
        window.activateWindow()
        # Once the window is in front: a file panel opened before then came
        # up with its sidebar dead.
        window.add_tracks_when_ready()

    def _forget_visualiser(self) -> None:
        self._visualiser_window = None

    def _show_briefing(self) -> None:
        """What the last scan found, as a briefing: counted from the rows on
        screen, so it costs nothing and agrees with the table.
        """
        import briefing_dialog

        items = list(self._sorted)
        start, end = self._scanned_window
        briefing_dialog.show(
            items, parent=self, window_start=start, window_end=end,
            mailboxes=[a.label or a.address
                       for a in self.settings.scan_accounts],
            on_row=lambda index, shown=items: self._reveal_item(
                shown[index] if 0 <= index < len(shown) else None))

    def _reveal_item(self, wanted: Optional[TriageItem]) -> None:
        """Select a row of the inbox, going back to the inbox for it."""
        if wanted is None:
            return
        if not self._showing_inbox():
            self._go_to(INBOX)
        for row, item in enumerate(self.model.items):
            if item is wanted:
                self._reveal_row(row)
                return

    def _reveal_row(self, row: int) -> None:
        """Select a row the briefing pointed at, first clearing any filter
        hiding it.
        """
        items = self.model.items
        if not (0 <= row < len(items)):
            return
        if not self.proxy.mapFromSource(self.model.index(row, 0)).isValid():
            self._clear_filters()
        self._select_rows([row])
        index = self.proxy.mapFromSource(self.model.index(row, 0))
        if index.isValid():
            self.table.scrollTo(index)
            self.table.setFocus()

    def _clear_out_mail(self) -> None:
        """Open the window for deleting mail in bulk.

        The rows on screen go with it, so it can suggest the piles the last
        scan noticed, only as text in its filters; what it deletes is what
        the server matches, counted and confirmed by number. Senders the app
        has been taught about are protected: correcting a sender is the
        clearest sign their mail matters.
        """
        from PySide6.QtWidgets import QMessageBox

        from cleanup_dialog import ClearOutDialog

        account = self.settings.primary_account
        if account is None:
            QMessageBox.information(
                self, "No mailbox", "Add a mailbox in Settings first.")
            return
        password = self._mailbox_passwords().get(account.id, "")
        if not password:
            QMessageBox.information(
                self, "No password",
                f"There is no saved password for {account.label}. Add one in "
                "Settings, then try again.")
            return

        protected = []
        try:
            import corrections
            protected = [c.sender for c in corrections.Memory.load().entries]
        except Exception as exc:      # noqa: BLE001 - a missing file, not a stop
            log.info("Could not read the corrections memory (%s).", exc)

        dialog = ClearOutDialog(account, password,
                                items=self._inbox_rows(),
                                protected=protected, parent=self)
        dialog.cleared.connect(
            lambda removed: self._set_status(
                f"Cleared out {removed:,} message(s)."))
        dialog.exec()

    def _empty_a_folder(self) -> None:
        """Clear one folder on the server, after saying how many messages that
        is.

        A handful of commands whatever the number, against a round trip per
        message by hand. The folder is typed or chosen, the count is read
        from the server, and the number is in the question.
        """
        from PySide6.QtWidgets import QInputDialog, QMessageBox

        account = self.settings.primary_account
        if account is None:
            QMessageBox.information(self, "No mailbox",
                                    "Add a mailbox in Settings first.")
            return
        # The bin is offered because it exists to be emptied: rules put mail
        # there so it can go in one command.
        plan = self.folder_plan or self.settings.folder_plan()
        folder, said = QInputDialog.getText(
            self, "Empty a folder",
            "Which folder should be emptied?\n"
            "Everything in it is deleted from the server. The folder stays.",
            text=plan.bin_folder if plan else "")
        folder = (folder or "").strip()
        if not said or not folder:
            return

        passwords = self._mailbox_passwords()
        password = passwords.get(account.id, "")
        answer = QMessageBox.warning(
            self, "Empty this folder?",
            f"Everything in “{folder}” on {account.label} will be deleted "
            "from the server.\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if answer != QMessageBox.StandardButton.Yes:
            return

        from workers import EmptyFolderWorker

        worker = EmptyFolderWorker(account, password, folder, parent=self)
        self._empty_worker = worker
        worker.progress.connect(
            lambda done, total, text: self._set_status(text))
        worker.failed.connect(
            lambda title, detail: QMessageBox.warning(self, title, detail))
        worker.finished_ok.connect(
            lambda removed: self._set_status(
                f"Removed {removed} message(s) from “{folder}”."))
        worker.start()

    def _look_for_updates(self, by_hand: bool = False) -> None:
        """Ask GitHub for the latest release, if it is time to - or now, when
        asked from the menu. See updates."""
        import updates
        import update_dialog

        if self.demo or self.dry_run:
            return
        if not by_hand and not updates.due(self.settings):
            return
        if self._update_look is not None:
            return
        look = update_dialog.Look(self)
        look.found.connect(lambda release: self._update_found(release, by_hand))
        look.failed.connect(lambda why: self._update_failed(why, by_hand))
        look.finished.connect(self._update_looked)
        self._update_look = look
        look.start()

    def _update_looked(self) -> None:
        look, self._update_look = self._update_look, None
        if look is not None:
            look.deleteLater()

    def _update_found(self, release, by_hand: bool) -> None:
        import time

        import updates
        from update_dialog import UpdateDialog

        self.settings.update_checked = time.time()
        self.settings.save()
        skipped = "" if by_hand else self.settings.skipped_version
        if updates.offered(release, APP_VERSION, skipped):
            dialog = UpdateDialog(release, updates.running_app(), self)
            dialog.skipped.connect(self._skip_version)
            dialog.restart.connect(self._restart_to_update)
            self._update_dialog = dialog
            dialog.open()
        elif by_hand:
            QMessageBox.information(
                self, "Up to date",
                f"{APP_DISPLAY_NAME} {APP_VERSION} is the newest version.")

    def _update_failed(self, why: str, by_hand: bool) -> None:
        if by_hand:
            QMessageBox.warning(self, "Could not check for updates", why)

    def _skip_version(self, version: str) -> None:
        self.settings.skipped_version = version
        self.settings.save()

    def _restart_to_update(self, installer, script: str) -> None:
        """Quit, and have the new version put in place and opened."""
        from pathlib import Path

        self.quit_app(before=lambda: installer.launch(Path(script)))

    # -- Reading and writing mail ---------------------------------------------
    #
    # A message opens in a window of its own; a reply, a forward or a new
    # message is written in another. Both windows hand everything back to
    # here - what to do to a message, where it goes, who sends it - because
    # this window holds the accounts, the passwords and the table.

    #: The quick marks, by what the windows and menus ask for.
    FLAG_ACTIONS = {"read": ("seen", True), "unread": ("seen", False),
                    "flag": ("flagged", True), "unflag": ("flagged", False),
                    "answered": ("answered", True)}
    #: The quick moves, by what the windows and menus ask for.
    MOVE_ACTIONS = {"archive": "archive", "delete": "trash", "junk": "junk",
                    "move": "folder"}

    def _live_mail_windows(self) -> list:
        # A closed window is deleted on the next turn of the loop, and is
        # no longer a window to count until then either.
        self._mail_windows = [w for w in self._mail_windows
                              if shiboken6.isValid(w) and w.isVisible()]
        return list(self._mail_windows)

    def _account_id_of(self, message) -> str:
        """Which mailbox a message belongs to. A single-mailbox scan leaves
        the field empty, which means the primary one."""
        return message.account_id or self.settings.primary_account.id

    def _adopt_mail_window(self, window) -> None:
        self._mail_windows.append(window)
        window.show()
        window.raise_()
        window.activateWindow()

    def _close_mail_windows(self) -> None:
        for window in self._live_mail_windows():
            window.close()
        self._mail_windows = []

    def _refresh_mail_windows(self) -> None:
        """After the table changed - a flag, a move, a scan - every message
        window looks at its message again, and closes if it has gone."""
        from mail_window import MessageWindow

        for window in self._live_mail_windows():
            if isinstance(window, MessageWindow):
                window.refresh()

    @Slot(int)
    def open_message(self, row: int) -> None:
        """One message in a window of its own, marked read the way opening
        it anywhere else would. A second opening of the same message
        brings its window forward rather than making another."""
        from mail_window import MessageWindow

        item = self.model.item_at(row)
        if item is None:
            return
        for window in self._live_mail_windows():
            if isinstance(window, MessageWindow) and window.same_message(item):
                window.show_row(row)
                window.show()
                window.raise_()
                window.activateWindow()
                return
        window = MessageWindow(self, row)
        self._adopt_mail_window(window)
        if "\\seen" not in _flags_of(item):
            self.act_on_rows("read", [row])

    def _open_index(self, index) -> None:
        """A double-click or Return on a row. Not on the tick box: two quick
        clicks there are two ticks, not a window."""
        if not index.isValid() or index.column() == TriageTableModel.COL_SELECT:
            return
        self.open_message(self.proxy.mapToSource(index).row())

    def _open_selected(self) -> None:
        rows = self._selected_rows()
        if rows:
            self.open_message(rows[0])

    def _compose_selected(self, mode: str) -> None:
        rows = self._selected_rows()
        if rows:
            self.compose(mode, rows[0])

    def _act_on_selected(self, what: str) -> None:
        rows = self._selected_rows()
        if rows:
            self.act_on_rows(what, rows)

    def neighbour_row(self, row: int, by: int) -> Optional[int]:
        """The source row ``by`` places from ``row`` in the table as it is
        shown - sorted and filtered - or None at either end."""
        shown = self.proxy.mapFromSource(self.model.index(row, 0))
        if not shown.isValid():
            return None
        wanted = shown.row() + by
        if not 0 <= wanted < self.proxy.rowCount():
            return None
        return self.proxy.mapToSource(self.proxy.index(wanted, 0)).row()

    def select_row(self, row: int) -> None:
        """Select one source row and bring it into view."""
        self._select_rows([row])
        shown = self.proxy.mapFromSource(self.model.index(row, 0))
        if shown.isValid():
            self.table.scrollTo(shown)

    def folder_choices(self) -> List[str]:
        return self._folder_choices()

    def open_link(self, url: str) -> None:
        self._open_link(url)

    def sender_name(self) -> str:
        """The name on what goes out: the one replies are signed with."""
        return (self.settings.reply_signature or "").strip()

    def signature_html(self, replying: bool = False) -> str:
        """The sign-off a new message, or a reply or forward, opens with:
        the one written on the Signature page, else the name alone, or
        nothing where the page says not to."""
        settings = self.settings
        wanted = (settings.signature_in_replies if replying
                  else settings.signature_in_new)
        if not wanted:
            return ""
        rich = (settings.signature_html or "").strip()
        if rich:
            return rich
        name = self.sender_name()
        return f"<p>{_html(name)}</p>" if name else ""

    def known_addresses(self) -> List[str]:
        """Who the To field can offer: the mailboxes here, and everyone
        who wrote to them."""
        import outgoing

        found: Dict[str, str] = {}
        for account in self._sending_accounts():
            if account.address:
                found[account.address.lower()] = account.address
        for item in self._inbox_rows() + list(self.model.items):
            message = item.email
            if message.sender_email:
                found.setdefault(message.sender_email.lower(),
                                 outgoing.format_address(message.sender_name,
                                                         message.sender_email))
        return sorted(found.values(), key=str.lower)

    def _sending_accounts(self) -> list:
        accounts = [a for a in self.settings.accounts if a.is_configured]
        if not accounts and self.settings.primary_account.is_configured:
            accounts = [self.settings.primary_account]
        return accounts

    def compose(self, mode: str = "new", row: int = -1) -> None:
        """Write a message: a new one, or a reply, a reply to all or a
        forward of the row's message, in a window of its own."""
        import outgoing
        from mail_window import ComposeWindow

        accounts = self._sending_accounts()
        if not accounts:
            QMessageBox.information(
                self, "No mailbox",
                "Add a mailbox in Settings first. Mail goes out through it.")
            return
        item = self.model.item_at(row) if row >= 0 else None
        message = item.email if item is not None else None
        account = None
        if message is not None:
            account = next((a for a in accounts if a.id == message.account_id), None)
        account = account or accounts[0]
        draft = outgoing.Draft(from_address=account.address,
                               from_name=self.sender_name())
        quoted_html = quoted_text = ""
        answering = None
        if message is not None and mode in ("reply", "reply_all"):
            draft.to, draft.cc = outgoing.reply_addresses(
                message, [a.address for a in accounts], everyone=(mode == "reply_all"))
            draft.subject = outgoing.reply_subject(message.subject)
            draft.in_reply_to, draft.references = outgoing.thread_headers(message)
            quoted_html = outgoing.quoted_html(message)
            quoted_text = outgoing.quoted_text(message)
            answering = (message.uid, message.source_folder or "INBOX",
                         self._account_id_of(message))
        elif message is not None and mode == "forward":
            draft.subject = outgoing.reply_subject(message.subject, forward=True)
            quoted_html = outgoing.forward_html(message)
            quoted_text = outgoing.forward_text(message)
        window = ComposeWindow(self, draft, accounts, account, quoted_html,
                               quoted_text, answering=answering)
        self._adopt_mail_window(window)
        if message is not None and mode == "forward" and message.attachments:
            self._carry_attachments(window, message, account)

    def _password_for(self, account) -> Optional[str]:
        """The account's password, or None with the reason already shown."""
        try:
            password = self.store.get_mailbox_password(account.address)
        except CredentialError as exc:
            QMessageBox.warning(self, "Keychain", str(exc))
            return None
        if not password:
            QMessageBox.information(
                self, "Not connected",
                f"{account.label} has no password saved. Add it in Settings "
                "under Mailboxes, then try again.")
            return None
        return password

    def _carry_attachments(self, window, message, account) -> None:
        """A forward takes the attachments with it: fetched in the
        background, and added to the window as they arrive."""
        if self.demo:
            window.note("The demo's attachments are not real, so they stay behind.")
            return
        password = self._password_for(account)
        if password is None:
            return
        count = len(message.attachments)
        window.note(f"Fetching {count} attachment{'' if count == 1 else 's'}…")
        worker = AttachmentFetchWorker(account, password, message, self)

        def arrived(found, left_out) -> None:
            if not shiboken6.isValid(window):
                return
            window.carry(found)
            if left_out:
                window.note(f"Too big to carry: {', '.join(left_out)}")
            else:
                window.note("", 0)

        def failed(_title: str, detail: str) -> None:
            if shiboken6.isValid(window):
                window.note(f"The attachments could not be fetched: {detail}")

        worker.ready.connect(arrived)
        worker.failed.connect(failed)
        self._register(worker)
        worker.start()

    def send_mail(self, account, draft, done, failed, answering=None) -> None:
        """Send what the compose window holds, from ``account``. ``done``
        and ``failed`` are the window's, called on this thread."""
        if self.demo:
            failed("This is the demo. Nothing is sent from it.")
            return
        if self.dry_run:
            failed("Dry run: nothing is sent.")
            return
        password = self._password_for(account)
        if password is None:
            failed(f"{account.label} has no password saved.")
            return
        mine = None
        if answering and answering[2] == account.id:
            mine = (answering[0], answering[1])
        worker = SendWorker(account, password, draft, answering=mine, parent=self)

        def sent(note: str) -> None:
            self._after_send(draft, note, answering)
            done()

        worker.sent.connect(sent)
        worker.failed.connect(failed)
        self._register(worker)
        worker.start()
        self._set_status("Sending…")

    def _after_send(self, draft, note: str, answering) -> None:
        count = len(draft.recipients)
        self._append_log(f"Sent a message to {count} recipient{'' if count == 1 else 's'}.")
        self._set_status(note or "Sent.")
        if answering:
            uid, _mailbox, account_id = answering
            rows = [row for row, item in enumerate(self.model.items)
                    if item.email.uid == uid
                    and self._account_id_of(item.email) == account_id]
            if rows:
                self._mark_locally([(row, self.model.items[row]) for row in rows],
                                   "answered", True)

    def save_draft(self, account, draft, done, failed) -> None:
        """Put what the compose window holds into the account's Drafts."""
        if self.demo:
            failed("This is the demo. Nothing is saved from it.")
            return
        password = self._password_for(account)
        if password is None:
            failed(f"{account.label} has no password saved.")
            return
        worker = DraftWorker(account, password, draft, self)
        worker.saved.connect(lambda where: done())
        worker.failed.connect(lambda _title, detail: failed(detail))
        self._register(worker)
        worker.start()

    def act_on_rows(self, what: str, rows: Sequence[int], where: Optional[str] = None) -> None:
        """Do one thing to some messages, now: mark them read or unread,
        flag or unflag them, or move them - to Archive, Trash or Junk, or
        to a folder by name. The table changes at once and the server
        follows in the background; a move is undoable like any filing.
        The message window, the Message menu and the table's menu all
        come here."""
        items = [(row, self.model.item_at(row)) for row in rows]
        items = [(row, item) for row, item in items
                 if item is not None and not item.moved]
        if not items:
            return
        if what in self.FLAG_ACTIONS:
            flag, add = self.FLAG_ACTIONS[what]
            self._mark_locally(items, flag, add)
            if not self.demo:
                self._mark_on_server(items, flag, add)
            return
        kind = self.MOVE_ACTIONS.get(what)
        if kind is None:
            raise ValueError(f"not something that can be done to a message: {what!r}")
        if kind == "folder" and not where:
            return
        if self.dry_run:
            QMessageBox.information(
                self, "Dry run", "Nothing moves in dry-run mode.")
            return
        landing = where if kind == "folder" else {
            "archive": "Archive", "trash": "Trash", "junk": "Junk"}[kind]
        if self.demo:
            self._record_moves(MoveReport(
                moved={item.email.uid: landing for _row, item in items}),
                among=[item for _row, item in items])
            self._update_status(f"Demo: marked {len(items)} message(s) as moved to {landing}.")
            return
        if self._busy():
            return
        by_account: Dict[str, list] = {}
        for _row, item in items:
            by_account.setdefault(item.email.account_id, []).append(item)
        started = 0
        for account_id, group in by_account.items():
            account = (self.settings.account_by_id(account_id)
                       or self.settings.primary_account)
            password = self._password_for(account)
            if password is None:
                continue
            plans = [MovePlan(uid=item.email.uid, target_folder=where or "",
                              subject=item.email.subject_display,
                              account_id=item.email.account_id,
                              source_folder=item.email.source_folder or "")
                     for item in group]
            worker = MoveWorker(account, password, plans, kind, self)
            worker.done.connect(self._on_quick_move)
            worker.failed.connect(self._on_failed)
            self._register(worker)
            worker.start()
            started += len(plans)
        if started:
            self._set_status(f"Moving {started} message{'' if started == 1 else 's'} "
                             f"to {landing}…")

    @Slot(object)
    def _on_quick_move(self, report: MoveReport) -> None:
        message = self._record_moves(report)
        if report.failed:
            sample = list(report.failed.items())[:5]
            detail = "\n".join(f"UID {uid}: {error}" for uid, error in sample)
            QMessageBox.warning(self, "Could not move the message", detail)
        self._update_status(message.replace("Filed", "Moved", 1))

    def _mark_locally(self, items, flag: str, add: bool) -> None:
        """The flag in the table, at once; the server is told separately."""
        name = {"seen": "\\Seen", "flagged": "\\Flagged", "answered": "\\Answered"}[flag]
        for _row, item in items:
            flags = [f for f in item.email.flags if f.lower() != name.lower()]
            if add:
                flags.append(name)
            item.email.flags = tuple(flags)
        self.model._refresh_all()
        self._refresh_mail_windows()
        self._selection_changed()

    def _mark_on_server(self, items, flag: str, add: bool) -> None:
        groups: Dict[Tuple[str, str], list] = {}
        for _row, item in items:
            key = (item.email.account_id, item.email.source_folder or "INBOX")
            groups.setdefault(key, []).append(item.email.uid)
        for (account_id, folder), uids in groups.items():
            account = (self.settings.account_by_id(account_id)
                       or self.settings.primary_account)
            try:
                password = self.store.get_mailbox_password(account.address)
            except CredentialError as exc:
                self._set_status(f"Keychain: {exc}")
                continue
            if not password:
                self._set_status(f"{account.label} has no password saved, so the "
                                 "mark stays in the table only.")
                continue
            worker = FlagWorker(account, password, uids, flag, add, folder, self)
            worker.failed.connect(
                lambda title, detail: (self._append_log(f"✗ {title}: {detail}"),
                                       self._set_status(f"{title}: {detail}")))
            self._register(worker)
            worker.start()

    def _open_link(self, url: str) -> None:
        """A link from a message: where it goes is said first, unless the
        person has asked not to be told. See link_open."""
        import link_open

        _opened, never = link_open.open_link(
            url, self, warn=self.settings.warn_on_links)
        if never:
            self.settings.warn_on_links = False
            self.settings.save()

    def _open_attachments(self, row: int) -> None:
        """Fetch one message's attachments, then show them. A scan keeps only
        the start of each message's text, so this asks the server again for
        that message.
        """
        import attachments as _attachments

        items = self.model.items
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
                "This mailbox is not connected. Add its password in "
                "Settings and scan again.")
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
        """Open the viewer as a window, not modal: a modal viewer swallowed
        Quit, and a viewer is something left open beside the window anyway.
        """
        from attachment_view import AttachmentViewer

        existing = self._attachment_window
        if existing is not None:
            existing.close()
        viewer = AttachmentViewer(found, subject, self,
                                  fetch=source.fetch if source else None)
        viewer.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._attachment_window = viewer

        def finished(*_args) -> None:
            if source is not None:
                source.close()
            if self._attachment_window is viewer:
                self._attachment_window = None

        viewer.finished.connect(finished)
        viewer.show()
        viewer.raise_()
        viewer.activateWindow()

    def _close_attachment_window(self) -> None:
        """Called on the way out, so a viewer never outlives the window."""
        viewer = self._attachment_window
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


def _flags_of(item) -> set:
    return {flag.lower() for flag in (item.email.flags or ())}


def _mailbox_short(label: str) -> str:
    """The address out of ``Account.describe()``, which is what tells one
    mailbox from another."""
    return label.split(" · ")[-1] if " · " in label else label


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
