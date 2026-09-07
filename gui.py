"""PySide6 user interface for iCloud Mail Job Triage."""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QAbstractTableModel,
    QThread,
    QByteArray,
    QDate,
    QModelIndex,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtCore import QPointF  # noqa: E402  (grouped with Qt imports below)
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFontMetrics,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QStackedWidget,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableView,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import config
import llm_engine
import profiles
import providers
import rulesets
import scheduler
from config import (
    EFFORT_LEVELS,
    CredentialError,
    CredentialStore,
    Settings,
    log_dir,
)
from imap_engine import MoveReport
from models import (
    APP_DISPLAY_NAME,
    APP_VERSION,
    CATEGORY_COLORS,
    OTHER_COLOR,
    Category,
    Disposition,
    FolderPlan,
    NonJobRouting,
    OtherCategory,
    TimeWindow,
    TriageItem,
    TriageSummary,
    clock,
    resolve_window,
)
from flowlayout import FlowLayout, Spacer
import theme
from menubar import MenuBarController
from welcome import SetupWizard
from workers import (
    ApplyWorker,
    ConnectionTestWorker,
    ScanOutcome,
    ScanWorker,
    build_move_plans,
    required_folders,
)

log = logging.getLogger(__name__)

LEAVE_IN_PLACE = "- leave in place -"

#: Accent colours. Mid-tone and paired with white text, so the same value reads
#: correctly in both light and dark mode without a second palette.
def menu_text(label: str) -> str:
    """Escape a label for use in a menu.

    Qt reads a single ampersand as a keyboard mnemonic and swallows it, so
    "Software & Data" renders as "Software  Data" - which reads as a stray
    double space rather than as a missing character.
    """
    return (label or "").replace("&", "&&")


ACCENT_BLUE = "#2F6FE0"     # the primary action
ACCENT_GREEN = "#2E9E63"    # a safe, confirmed action
ACCENT_RED = "#C4534A"      # stop / destructive
ACCENT_AMBER = "#D08A1E"    # needs a human

#: "Show" filter modes.
SHOW_ALL = "all"
SHOW_JOB_ONLY = "job"
SHOW_SELECTED = "selected"

EMPTY_STATE = (
    "<div style='text-align:center;line-height:170%'>"
    "<span style='font-size:15px'><b>Nothing scanned yet</b></span><br>"
    "<span style='opacity:0.7'>Pick a time window above, then press "
    "<b>Scan &amp; Analyze</b>.<br>"
    "Messages are read without being marked as read, and nothing moves "
    "until you tick it.</span></div>"
)


# ==========================================================================
# Table model
# ==========================================================================
class TriageTableModel(QAbstractTableModel):
    """Approval table backed by a list of :class:`~models.TriageItem`."""

    COL_SELECT = 0
    COL_SENDER = 1
    COL_SUBJECT = 2
    COL_DATE = 3
    COL_SUMMARY = 4
    COL_CATEGORY = 5
    COL_FOLDER = 6
    COL_CONFIDENCE = 7
    COL_REASONING = 8
    COL_ACCOUNT = 9

    #: Short enough to survive a narrow column; the long form is the tooltip.
    HEADERS = (
        "",
        "Sender",
        "Subject",
        "Received",
        "Summary",
        "Category",
        "Folder",
        "Confidence",
        "Reasoning",
        "Mailbox",
    )
    HEADER_TOOLTIPS = (
        "Tick to include this message when you apply folder moves.",
        "Who the message is from.",
        "The subject line.",
        "When the message arrived. Hover a cell for the exact date.",
        "The model's two-sentence summary.",
        "The category, and for non-job mail the topic.",
        "Where it will be filed. Hover for the full path.",
        "How confident the model is. Below the threshold it goes to Needs Review.",
        "Why it decided that. The full text is in the preview below.",
        "Which mailbox this arrived in. Hidden unless more than one is set up.",
    )

    selectionChanged = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._items: List[TriageItem] = []
        #: Collapsed one-line summary/reasoning per row. These never change
        #: once a scan lands, and recomputing them on every repaint of a
        #: 400-row table is pure waste.
        self._one_line_cache: List[Tuple[str, str]] = []

    # -- data plumbing ---------------------------------------------------
    @property
    def items(self) -> List[TriageItem]:
        return self._items

    def set_items(self, items: Sequence[TriageItem]) -> None:
        self.beginResetModel()
        self._items = list(items)
        self._one_line_cache = [
            (_one_line(item.classification.summary), _one_line(item.classification.reasoning))
            for item in self._items
        ]
        self.endResetModel()
        self.selectionChanged.emit()

    def item_at(self, row: int) -> Optional[TriageItem]:
        if 0 <= row < len(self._items):
            return self._items[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._items)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.HEADERS)

    def headerData(self, section: int, orientation, role=Qt.ItemDataRole.DisplayRole):  # noqa: N802
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return self.HEADERS[section]
        if role == Qt.ItemDataRole.ToolTipRole:
            return self.HEADER_TOOLTIPS[section]
        return None

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemFlag.NoItemFlags
        base = Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        item = self.item_at(index.row())
        if index.column() == self.COL_SELECT and item is not None and item.is_actionable:
            base |= Qt.ItemFlag.ItemIsUserCheckable
        return base

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):  # noqa: C901
        if not index.isValid():
            return None
        item = self.item_at(index.row())
        if item is None:
            return None
        column = index.column()
        classification = item.classification

        if role == Qt.ItemDataRole.CheckStateRole and column == self.COL_SELECT:
            if not item.is_actionable:
                return None
            return Qt.CheckState.Checked if item.approved else Qt.CheckState.Unchecked

        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.EditRole):
            if column == self.COL_ACCOUNT:
                return item.email.account_label
            if column == self.COL_SENDER:
                return item.email.sender_short
            if column == self.COL_SUBJECT:
                return item.email.subject_display
            if column == self.COL_DATE:
                return item.email.date_human()
            if column == self.COL_SUMMARY:
                return classification.summary
            if column == self.COL_CATEGORY:
                return classification.category_label
            if column == self.COL_FOLDER:
                return item.folder_short
            if column == self.COL_CONFIDENCE:
                return f"{classification.confidence_percent:.0f}%"
            if column == self.COL_REASONING:
                return classification.reasoning
            return None

        if role == Qt.ItemDataRole.UserRole + 1:  # accent colour for this row
            return category_color(classification)

        if role == Qt.ItemDataRole.UserRole:  # sort key
            if column == self.COL_SELECT:
                return (1 if item.approved else 0, item.classification.confidence_score)
            if column == self.COL_DATE:
                return (item.email.date or datetime.min.replace(tzinfo=timezone.utc)).timestamp()
            if column == self.COL_CONFIDENCE:
                return classification.confidence_score
            if column == self.COL_CATEGORY:
                return classification.category_label
            value = self.data(index, Qt.ItemDataRole.DisplayRole)
            return (value or "").lower() if isinstance(value, str) else value

        if role == Qt.ItemDataRole.ToolTipRole:
            if column == self.COL_CONFIDENCE:
                return (
                    f"{classification.confidence_percent:.1f}% confident.\n"
                    f"Threshold for automatic filing: {item.threshold * 100:.0f}%."
                )
            if column in (self.COL_SUMMARY, self.COL_REASONING, self.COL_SUBJECT):
                text = {
                    self.COL_SUMMARY: classification.summary,
                    self.COL_REASONING: classification.reasoning,
                    self.COL_SUBJECT: item.email.subject_display,
                }[column]
                return _wrap(text)
            if column == self.COL_SENDER:
                return item.email.sender_display
            if column == self.COL_DATE:
                return item.email.date_full()
            if column == self.COL_FOLDER:
                return f"{item.status_display}\n{item.folder_display}"
            if column == self.COL_CATEGORY:
                return classification.category_label
            return None

        if role == Qt.ItemDataRole.BackgroundRole:
            # A whisper of the category colour, so the eye can group rows
            # without the table turning into a paint chart.
            if item.moved:
                return None
            if item.disposition is Disposition.MOVE:
                return _tint(category_color(classification), 26)
            if item.disposition is Disposition.REVIEW:
                return _tint(CATEGORY_COLORS[Category.UNCLASSIFIED_OTHER], 22)
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            if item.moved:
                return QColor(120, 120, 120)
            if classification.error or item.move_error:
                return QColor(ACCENT_RED)
            if column == self.COL_FOLDER:
                if item.disposition is Disposition.LEAVE:
                    return QColor(140, 140, 140)
                if not item.override_folder:
                    return QColor(category_color(classification))
            return None

        if role == Qt.ItemDataRole.FontRole and column == self.COL_SUBJECT:
            if item.disposition is Disposition.MOVE and item.classification.is_job_related:
                font = system_font()
                font.setWeight(QFont.Weight.DemiBold)
                return font
            return None

        if role == Qt.ItemDataRole.TextAlignmentRole and column == self.COL_CONFIDENCE:
            return int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignHCenter)

        return None

    def setData(self, index: QModelIndex, value, role=Qt.ItemDataRole.EditRole) -> bool:  # noqa: N802
        if not index.isValid() or role != Qt.ItemDataRole.CheckStateRole:
            return False
        if index.column() != self.COL_SELECT:
            return False
        item = self.item_at(index.row())
        if item is None or not item.is_actionable:
            return False
        item.approved = Qt.CheckState(value) == Qt.CheckState.Checked
        self.dataChanged.emit(index, index, [Qt.ItemDataRole.CheckStateRole])
        self.selectionChanged.emit()
        return True

    # -- bulk operations -------------------------------------------------
    def set_all_approved(self, approved: bool, only_high_confidence: bool = False) -> None:
        if not self._items:
            return
        for item in self._items:
            if not item.is_actionable:
                continue
            if only_high_confidence and not item.default_approved:
                continue
            item.approved = approved
        self._refresh_column(self.COL_SELECT)
        self.selectionChanged.emit()

    def reset_to_defaults(self) -> None:
        for item in self._items:
            item.approved = item.default_approved
        self._refresh_column(self.COL_SELECT)
        self.selectionChanged.emit()

    def set_override(self, row: int, folder: Optional[str]) -> None:
        item = self.item_at(row)
        if item is None:
            return
        item.override_folder = folder
        if folder and not item.moved:
            item.approved = True
        elif folder is None and item.suggested_folder is None:
            item.approved = False
        top = self.index(row, 0)
        bottom = self.index(row, self.columnCount() - 1)
        self.dataChanged.emit(top, bottom)
        self.selectionChanged.emit()

    def apply_report(self, report: MoveReport) -> None:
        for item in self._items:
            uid = item.email.uid
            if uid in report.moved:
                item.moved = True
                item.approved = False
                item.move_error = None
            elif uid in report.failed:
                item.move_error = report.failed[uid]
        self._refresh_all()
        self.selectionChanged.emit()

    def _refresh_column(self, column: int) -> None:
        if not self._items:
            return
        self.dataChanged.emit(
            self.index(0, column), self.index(len(self._items) - 1, column)
        )

    def _refresh_all(self) -> None:
        if not self._items:
            return
        self.dataChanged.emit(
            self.index(0, 0), self.index(len(self._items) - 1, self.columnCount() - 1)
        )

    def summary(self) -> TriageSummary:
        return TriageSummary.build(self._items)


class TriageFilterProxy(QSortFilterProxyModel):
    """Free-text search plus category and disposition filters."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSortRole(Qt.ItemDataRole.UserRole)
        self.setDynamicSortFilter(True)
        self._text = ""
        self._category: Optional[str] = None
        self._hide_non_job = False
        self._only_selected = False

    def set_text_filter(self, text: str) -> None:
        self._text = (text or "").strip().lower()
        self.invalidate()

    def set_category_filter(self, category: Optional[str]) -> None:
        self._category = category
        self.invalidate()

    def set_hide_non_job(self, hide: bool) -> None:
        self._hide_non_job = bool(hide)
        self.invalidate()

    def set_only_selected(self, only: bool) -> None:
        self._only_selected = bool(only)
        self.invalidate()

    def filterAcceptsRow(self, source_row: int, parent: QModelIndex) -> bool:  # noqa: N802
        model = self.sourceModel()
        if not isinstance(model, TriageTableModel):
            return True
        item = model.item_at(source_row)
        if item is None:
            return False
        if self._hide_non_job and not item.classification.is_job_related:
            return False
        if self._only_selected and not item.approved:
            return False
        if self._category and item.classification.category_label != self._category:
            return False
        if self._text:
            haystack = " ".join(
                (
                    item.email.sender_display,
                    item.email.subject_display,
                    item.classification.summary,
                    item.classification.reasoning,
                    item.classification.category_label,
                    item.folder_display,
                )
            ).lower()
            if self._text not in haystack:
                return False
        return True


class ConfidenceDelegate(QStyledItemDelegate):
    """Draws the confidence column as a labelled bar."""

    def __init__(self, threshold: float = 0.95, parent=None) -> None:
        super().__init__(parent)
        self.threshold = threshold

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        model = index.model()
        value = model.data(index, Qt.ItemDataRole.UserRole)
        if not isinstance(value, (int, float)):
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)

        rect = option.rect.adjusted(8, 6, -8, -6)
        bar_height = 6
        bar_rect = rect.adjusted(0, rect.height() - bar_height, 0, 0)
        bar_rect.setHeight(bar_height)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        track = option.palette.color(QPalette.ColorRole.Mid)
        track.setAlpha(90)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(bar_rect, 3, 3)

        ratio = max(0.0, min(1.0, float(value)))
        fill = QColor(*_confidence_rgb(ratio, self.threshold))
        filled = bar_rect.adjusted(0, 0, -int(bar_rect.width() * (1.0 - ratio)), 0)
        if filled.width() > 0:
            painter.setBrush(fill)
            painter.drawRoundedRect(filled, 3, 3)

        text_rect = rect.adjusted(0, -2, 0, -bar_height - 2)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(
            option.palette.color(
                QPalette.ColorRole.HighlightedText if selected else QPalette.ColorRole.Text
            )
        )
        font = painter.font()
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1))
        painter.setFont(font)
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            f"{ratio * 100:.0f}%",
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(84, 34)


class WrapDelegate(QStyledItemDelegate):
    """Draws wrapped, multi-line text clipped to a fixed number of lines.

    A single elided line turns a 139-character summary into "AmeriSave Mortgage
    Corp. sen…", which tells the reader nothing. Wrapping to three lines shows
    the whole thing for most messages and a genuinely useful prefix for the rest.
    """

    def __init__(self, lines: int = 3, parent=None) -> None:
        super().__init__(parent)
        self.lines = max(1, int(lines))

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        text = index.data(Qt.ItemDataRole.DisplayRole)
        if not text:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        option.text = ""
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, option, painter, option.widget)

        rect = option.rect.adjusted(8, 5, -8, -5)
        painter.save()
        painter.setClipRect(option.rect)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        colour = index.data(Qt.ItemDataRole.ForegroundRole)
        painter.setPen(
            option.palette.color(QPalette.ColorRole.HighlightedText) if selected
            else (colour if isinstance(colour, QColor) else option.palette.color(QPalette.ColorRole.Text))
        )
        font = index.data(Qt.ItemDataRole.FontRole) or option.font
        painter.setFont(font)
        _draw_wrapped(painter, rect, str(text), self.lines, font)
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        metrics = QFontMetrics(option.font)
        return QSize(120, metrics.lineSpacing() * self.lines + 10)


def _wrap_lines(text: str, width: int, metrics, max_lines: int) -> List[str]:
    """Break `text` into at most `max_lines` lines that fit `width`.

    Done by hand rather than with QTextLayout: the layout's own line iteration
    made it easy to draw only the final line, which is precisely the bug this
    replaces. Word-first, falling back to character breaks for a single word
    longer than the column.
    """
    words = text.split()
    lines: List[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if metrics.horizontalAdvance(candidate) <= width or not current:
            if metrics.horizontalAdvance(candidate) > width and not current:
                # A single word wider than the column: break it.
                piece = word
                while metrics.horizontalAdvance(piece) > width and len(piece) > 1:
                    piece = piece[:-1]
                lines.append(piece)
                current = word[len(piece):]
                if len(lines) >= max_lines:
                    break
                continue
            current = candidate
        else:
            lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    return lines[:max_lines], len(" ".join(lines[:max_lines])) < len(text.rstrip())


def _draw_wrapped(painter: QPainter, rect, text: str, max_lines: int, font) -> None:
    """Draw `text` wrapped into `rect`, eliding the final visible line."""
    metrics = QFontMetrics(font)
    collapsed = " ".join(text.split())
    lines, overflowed = _wrap_lines(collapsed, rect.width(), metrics, max_lines)
    if not lines:
        return
    if overflowed:
        lines[-1] = metrics.elidedText(
            collapsed[len(" ".join(lines[:-1])):].strip(),
            Qt.TextElideMode.ElideRight,
            rect.width(),
        )
    y = rect.y() + metrics.ascent()
    for line in lines:
        painter.drawText(int(rect.x()), int(y), line)
        y += metrics.lineSpacing()


class CategoryDelegate(QStyledItemDelegate):
    """Draws the category as a coloured chip so a scan is readable at a glance."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        color_name = index.data(Qt.ItemDataRole.UserRole + 1)
        label = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if not color_name:
            super().paint(painter, option, index)
            return

        self.initStyleOption(option, index)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(color_name)

        rect = option.rect.adjusted(8, 6, -8, -6)
        dot = rect.adjusted(0, 0, 0, 0)
        dot.setWidth(9)
        dot.setHeight(9)
        dot.moveTop(rect.center().y() - 4)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color)
        painter.drawEllipse(dot)

        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        painter.setPen(
            option.palette.color(QPalette.ColorRole.HighlightedText)
            if selected
            else color.lighter(125) if _is_dark(option.palette) else color.darker(105)
        )
        text_rect = rect.adjusted(16, 0, 0, 0)
        metrics = painter.fontMetrics()
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            metrics.elidedText(str(label), Qt.TextElideMode.ElideRight, text_rect.width()),
        )
        painter.restore()

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        return QSize(140, 34)


def _is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.ColorRole.Window).lightness() < 128


def _confidence_rgb(ratio: float, threshold: float) -> tuple:
    if ratio >= threshold:
        return (56, 158, 96)
    if ratio >= threshold - 0.20:
        return (214, 158, 46)
    return (198, 91, 78)


# ==========================================================================
# Preview pane
# ==========================================================================
class PreviewPane(QWidget):
    """Side-by-side message text and the backend's reasoning."""

    overrideChanged = Signal(int, object)  # source row, folder or None

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._row: Optional[int] = None
        self._item: Optional[TriageItem] = None
        self._prompt_text = ""
        self._updating = False

        self.header = QLabel("Select a message to see its text and the analysis beside it.")
        self.header.setWordWrap(True)
        self.header.setTextFormat(Qt.TextFormat.RichText)
        self.header.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)

        self.body_mode = QComboBox()
        self.body_mode.addItems(["Message text", "Exactly what the model was sent"])
        self.body_mode.currentIndexChanged.connect(self._render_body)
        self.body_mode.setToolTip(
            "Switch between the readable message and the verbatim payload sent to the model."
        )

        self.body_view = QPlainTextEdit()
        self.body_view.setReadOnly(True)
        self.body_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body_view.setFont(_mono_font())

        self.reasoning_view = QTextBrowser()
        self.reasoning_view.setOpenExternalLinks(True)

        self.folder_combo = QComboBox()
        self.folder_combo.setEditable(True)
        self.folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.folder_combo.setMinimumWidth(280)
        self.folder_combo.currentTextChanged.connect(self._folder_changed)

        self.reset_button = QToolButton()
        self.reset_button.setText("Use AI suggestion")
        self.reset_button.clicked.connect(self._reset_override)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Source:"))
        mode_row.addWidget(self.body_mode, 1)
        left_layout.addLayout(mode_row)
        left_layout.addWidget(self.body_view, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        self.analysis_label = QLabel("Analysis")
        right_layout.addWidget(self.analysis_label)
        right_layout.addWidget(self.reasoning_view, 1)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setSizes([560, 460])

        folder_row = QHBoxLayout()
        folder_row.addWidget(QLabel("File into:"))
        folder_row.addWidget(self.folder_combo, 1)
        folder_row.addWidget(self.reset_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addWidget(self.header)
        layout.addLayout(folder_row)
        layout.addWidget(self.splitter, 1)

        self.set_folder_choices([])
        self.clear()

    def set_backend_label(self, label: str) -> None:
        """Name the backend that produced the reasoning shown on the right."""
        self.analysis_label.setText(f"{label} analysis" if label else "Analysis")
        self.body_mode.setItemText(
            1, f"Exactly what {label} was sent" if label else "Exactly what the model was sent"
        )

    def set_folder_choices(self, folders: Sequence[str]) -> None:
        current = self.folder_combo.currentText()
        self._updating = True
        self.folder_combo.clear()
        self.folder_combo.addItem(LEAVE_IN_PLACE)
        for folder in folders:
            self.folder_combo.addItem(folder)
        if current:
            index = self.folder_combo.findText(current)
            if index >= 0:
                self.folder_combo.setCurrentIndex(index)
            else:
                self.folder_combo.setEditText(current)
        self._updating = False

    def clear(self) -> None:
        self._row = None
        self._item = None
        self._prompt_text = ""
        self.header.setText(
            "<i>Select a message above to compare its text against the analysis.</i>"
        )
        self.body_view.setPlainText("")
        self.reasoning_view.setHtml("")
        self.folder_combo.setEnabled(False)
        self.reset_button.setEnabled(False)

    def show_item(self, row: int, item: TriageItem, prompt_text: str = "") -> None:
        self._row = row
        self._item = item
        self._prompt_text = prompt_text
        message = item.email
        classification = item.classification

        badge = _disposition_badge(item)
        self.header.setText(
            f"<div style='line-height:150%'>"
            f"<b>{_html(message.subject_display)}</b><br>"
            f"<span style='opacity:0.85'>{_html(message.sender_display)}</span> · "
            f"{_html(message.date_display('%A %d %B %Y, %H:%M'))}<br>"
            f"{badge}</div>"
        )

        self._updating = True
        self.folder_combo.setEnabled(not item.moved)
        self.reset_button.setEnabled(not item.moved and item.override_folder is not None)
        target = item.target_folder or LEAVE_IN_PLACE
        index = self.folder_combo.findText(target)
        if index >= 0:
            self.folder_combo.setCurrentIndex(index)
        else:
            self.folder_combo.setEditText(target)
        self._updating = False

        self._render_body()
        self.reasoning_view.setHtml(_reasoning_html(item))

    @Slot()
    def _render_body(self) -> None:
        if self._item is None:
            return
        if self.body_mode.currentIndex() == 1 and self._prompt_text:
            self.body_view.setPlainText(self._prompt_text)
        else:
            message = self._item.email
            text = message.body_text or "(this message had no readable text body)"
            if message.links:
                text += "\n\n--- LINKS ---\n" + "\n".join(f"• {link}" for link in message.links)
            if message.attachments:
                text += "\n\n--- ATTACHMENTS ---\n" + "\n".join(f"• {a}" for a in message.attachments)
            self.body_view.setPlainText(text)

    @Slot(str)
    def _folder_changed(self, text: str) -> None:
        if self._updating or self._row is None or self._item is None:
            return
        text = (text or "").strip()
        if not text or text == LEAVE_IN_PLACE:
            folder = None
        else:
            folder = text
        if folder == self._item.suggested_folder:
            folder = None
        self.reset_button.setEnabled(folder is not None)
        self.overrideChanged.emit(self._row, folder)

    @Slot()
    def _reset_override(self) -> None:
        if self._row is None or self._item is None:
            return
        self.overrideChanged.emit(self._row, None)
        self._updating = True
        target = self._item.suggested_folder or LEAVE_IN_PLACE
        index = self.folder_combo.findText(target)
        if index >= 0:
            self.folder_combo.setCurrentIndex(index)
        else:
            self.folder_combo.setEditText(target)
        self._updating = False
        self.reset_button.setEnabled(False)


def _disposition_badge(item: TriageItem) -> str:
    classification = item.classification
    colors = {
        Disposition.MOVE: "#2e9e63",
        Disposition.REVIEW: "#d69e2e",
        Disposition.LEAVE: "#8a8a8a",
    }
    color = colors[item.disposition]
    if classification.error:
        color = "#c65b4e"
    parts = [
        f"<span style='color:{color}'>&#9679;</span> "
        f"<b>{_html(classification.category_label)}</b>",
        f"{classification.confidence_percent:.0f}% confident",
        _html(item.folder_display),
    ]
    if item.moved:
        parts.append("<b>moved</b>")
    if item.move_error:
        parts.append(f"<span style='color:#c65b4e'>{_html(item.move_error)}</span>")
    return " &nbsp;·&nbsp; ".join(parts)


def _reasoning_html(item: TriageItem) -> str:
    classification = item.classification
    rows = [
        ("Summary", _html(classification.summary)),
        ("Reasoning", _html(classification.reasoning).replace("\n", "<br>")),
        ("Decision", _html(f"{item.disposition.label} → {item.folder_display}")),
        (
            "Confidence",
            f"{classification.confidence_percent:.1f}% "
            f"(threshold {item.threshold * 100:.0f}%)",
        ),
    ]
    if classification.model:
        rows.append(("Model", _html(classification.model)))
    if classification.input_tokens or classification.output_tokens:
        rows.append(
            ("Tokens", f"{classification.input_tokens:,} in / {classification.output_tokens:,} out")
        )
    if classification.adjustments:
        adjustments = "<br>".join(f"• {_html(a)}" for a in classification.adjustments)
        rows.append(("Safety adjustments", f"<span style='color:#d69e2e'>{adjustments}</span>"))
    if classification.error:
        rows.append(("Error", f"<span style='color:#c65b4e'>{_html(classification.error)}</span>"))
    if item.email.links:
        links = "<br>".join(
            f"• <a href='{_html(link)}'>{_html(link[:110])}</a>" for link in item.email.links[:12]
        )
        rows.append(("Links found", links))

    body = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;vertical-align:top;white-space:nowrap;'>"
        f"<b>{label}</b></td><td style='padding:4px 0'>{value}</td></tr>"
        for label, value in rows
    )
    return f"<table style='font-size:13px'>{body}</table>"


# ==========================================================================
# Settings dialog
# ==========================================================================
class SettingsDialog(QDialog):
    """Credentials, model, routing and folder configuration."""

    def __init__(self, settings: Settings, store: CredentialStore, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(640, 480)
        self.resize(760, 680)
        self._settings = settings
        self._store = store
        self._worker: Optional[ConnectionTestWorker] = None
        self._loading_models = False
        self._provider_seen = ""

        self.tabs = QTabWidget()
        # Each tab scrolls, so no amount of text can be cut off at any size.
        self.tabs.addTab(_scrollable(self._build_account_tab()), "Account")
        self.tabs.addTab(_scrollable(self._build_ai_tab()), "Analysis")
        self.tabs.addTab(_scrollable(self._build_folders_tab()), "Folders")
        self.tabs.addTab(_scrollable(self._build_appearance_tab()), "Appearance")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._load_values()

    # -- tabs ------------------------------------------------------------
    def _build_account_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@icloud.com")

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("app-specific password (xxxx-xxxx-xxxx-xxxx)")
        reveal = QToolButton()
        reveal.setText("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda on: self.password_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        password_row = QHBoxLayout()
        password_row.addWidget(self.password_edit, 1)
        password_row.addWidget(reveal)

        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.mailbox_edit = QLineEdit()

        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 8)
        self.connections_spin.setToolTip(
            "Parallel IMAP connections used while downloading. iCloud spends about "
            "the same server time per message whatever its size, and that cost "
            "parallelises: four connections fetch roughly 2.6× faster than one."
        )
        self.fetch_kb_spin = QSpinBox()
        self.fetch_kb_spin.setRange(8, 4096)
        self.fetch_kb_spin.setSingleStep(16)
        self.fetch_kb_spin.setSuffix(" KB")
        self.fetch_kb_spin.setToolTip(
            "How much of each message to download. Attachments sit after the text "
            "in every real MIME layout, so a partial fetch keeps what is read and "
            "skips the payload. Raise it if long messages look cut off."
        )

        self.test_imap_button = QPushButton("Test iCloud connection")
        self.test_imap_button.clicked.connect(lambda: self._run_test("imap"))
        test_row = QHBoxLayout()
        test_row.addWidget(self.test_imap_button)
        test_row.addStretch(1)

        form.addRow("iCloud email", self.email_edit)
        form.addRow("App-specific password", password_row)
        form.addRow(_separator())
        form.addRow("IMAP host", self.host_edit)
        form.addRow("IMAP port", self.port_spin)
        form.addRow("Mailbox to scan", self.mailbox_edit)
        form.addRow("Parallel connections", self.connections_spin)
        form.addRow("Download per message", self.fetch_kb_spin)
        form.addRow(test_row)

        note = QLabel(
            "Secrets are stored in the macOS Keychain (service “iCloud Job Triage”), "
            "never in a file. Generate an app-specific password at "
            "<a href='https://account.apple.com'>account.apple.com</a>, then Sign-In and Security; "
            "iCloud rejects your normal Apple ID password over IMAP."
        )
        note.setWordWrap(True)
        note.setOpenExternalLinks(True)
        form.addRow(note)
        return page

    def _build_ai_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.provider_combo = QComboBox()
        for name, label, _blurb in providers.provider_choices():
            self.provider_combo.addItem(label, name)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)

        self.provider_blurb = QLabel()
        self.provider_blurb.setWordWrap(True)
        self.provider_blurb.setStyleSheet("opacity:0.8")

        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        self.model_combo.setMinimumWidth(280)
        self.model_combo.currentIndexChanged.connect(self._model_changed)
        self.model_note = QLabel()
        self.model_note.setWordWrap(True)
        self.model_note.setStyleSheet("opacity:0.8")

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        reveal_key = QToolButton()
        reveal_key.setText("Show")
        reveal_key.setCheckable(True)
        reveal_key.toggled.connect(
            lambda on: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        self.key_link = QLabel()
        self.key_link.setOpenExternalLinks(True)
        key_row = QHBoxLayout()
        key_row.addWidget(self.api_key_edit, 1)
        key_row.addWidget(reveal_key)
        key_row.addWidget(self.key_link)
        self.key_row_widget = QWidget()
        self.key_row_widget.setLayout(key_row)
        key_row.setContentsMargins(0, 0, 0, 0)
        self.key_label = QLabel("API key")

        self.base_url_edit = QLineEdit()
        self.base_url_edit.setPlaceholderText("leave blank for the default endpoint")
        self.base_url_label = QLabel("Endpoint")

        self.test_model_button = QPushButton("Test this model")
        self.test_model_button.clicked.connect(lambda: self._run_test("claude"))
        _paint_button(self.test_model_button, ACCENT_BLUE)
        self.refresh_models_button = QPushButton("Refresh model list")
        self.refresh_models_button.setToolTip(
            "Ask the service which models it currently serves. Providers retire "
            "model ids without warning, so a built-in list goes stale."
        )
        self.refresh_models_button.clicked.connect(self._refresh_models)
        model_test_row = QHBoxLayout()
        model_test_row.addWidget(self.test_model_button)
        model_test_row.addWidget(self.refresh_models_button)
        model_test_row.addStretch(1)
        model_test_widget = QWidget()
        model_test_widget.setLayout(model_test_row)
        model_test_row.setContentsMargins(0, 0, 0, 0)

        self.effort_combo = QComboBox()
        self.effort_combo.addItems(list(EFFORT_LEVELS))
        self.effort_label = QLabel("Reasoning effort")

        self.threshold_spin = QDoubleSpinBox()
        self.threshold_spin.setRange(0.50, 1.00)
        self.threshold_spin.setSingleStep(0.01)
        self.threshold_spin.setDecimals(2)
        self.threshold_spin.setToolTip(
            "Messages below this confidence are routed to Needs Review and are never pre-checked."
        )

        self.concurrency_spin = QSpinBox()
        self.concurrency_spin.setRange(1, 16)
        self.concurrency_spin.setToolTip("How many requests to run in parallel.")

        self.batch_spin = QSpinBox()
        self.batch_spin.setRange(1, 25)
        self.batch_spin.setToolTip(
            "Emails per request. The instructions are ~2,800 tokens and are re-sent "
            "with every request, so batching is the single biggest saving available. "
            "Larger batches are cheaper but give each email less attention. "
            "Set to 1 to send one email per request."
        )

        self.body_chars_spin = QSpinBox()
        self.body_chars_spin.setRange(1000, 200000)
        self.body_chars_spin.setSingleStep(1000)
        self.body_chars_spin.setGroupSeparatorShown(True)
        self.body_chars_spin.setToolTip(
            "Longer bodies are trimmed at a paragraph boundary. The model is told when this "
            "happens and lowers its confidence accordingly."
        )

        self.max_messages_spin = QSpinBox()
        self.max_messages_spin.setRange(1, 5000)
        self.max_messages_spin.setSingleStep(50)
        self.max_messages_spin.setGroupSeparatorShown(True)

        self.fallback_check = QCheckBox(
            "If the backend is unreachable, classify locally with the built-in rules"
        )
        self.fallback_check.setToolTip(
            "Keeps a scan useful when the network, the key or the quota fails. "
            "Rows classified this way say so, and are held to the same confidence bar."
        )

        form.addRow("Model backend", self.provider_combo)
        form.addRow("", self.provider_blurb)
        form.addRow("Model", self.model_combo)
        form.addRow("", self.model_note)
        form.addRow(self.key_label, self.key_row_widget)
        form.addRow(self.base_url_label, self.base_url_edit)
        form.addRow("", model_test_widget)
        form.addRow(_separator())
        form.addRow(self.effort_label, self.effort_combo)
        form.addRow("Auto-file confidence", self.threshold_spin)
        form.addRow("Emails per request", self.batch_spin)
        form.addRow("Parallel requests", self.concurrency_spin)
        form.addRow("Max characters per email", self.body_chars_spin)
        form.addRow("Max messages per scan", self.max_messages_spin)
        form.addRow("", self.fallback_check)

        note = QLabel(
            "The confidence threshold does the real safety work here, not the model: "
            "anything the backend is unsure about goes to Needs Review whichever one you "
            "pick. A small local model is a perfectly reasonable choice - it will simply "
            "send more mail to Needs Review."
        )
        note.setWordWrap(True)
        form.addRow(note)
        return page

    def _provider_changed(self) -> None:
        """Repopulate the model list and show only the fields this backend uses."""
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)

        self.provider_blurb.setText(spec.blurb)

        previous = self.model_combo.currentText().strip()
        self._loading_models = True
        self.model_combo.clear()
        for choice in spec.models:
            self.model_combo.addItem(choice.label, choice.value)
        self._loading_models = False

        stored = self._settings.model if self._settings.provider == name else ""
        wanted = stored or (previous if self._provider_seen == name else "") or spec.default_model
        index = self.model_combo.findData(wanted)
        if index >= 0:
            self.model_combo.setCurrentIndex(index)
        else:
            self.model_combo.setEditText(wanted)
        self._provider_seen = name
        self._model_changed()

        needs_key = bool(spec.needs_api_key)
        self.key_label.setVisible(needs_key)
        self.key_row_widget.setVisible(needs_key)
        # A short label keeps the form's label column narrow; the backend is
        # already named in the dropdown directly above.
        self.key_label.setText("API key")
        self.api_key_edit.setPlaceholderText(spec.key_hint or "API key")
        self.key_link.setText(
            f"<a href='{spec.key_url}'>get one</a>" if spec.key_url else ""
        )
        if needs_key:
            try:
                self.api_key_edit.setText(self._store.get_provider_key(name))
            except CredentialError:
                self.api_key_edit.clear()
        else:
            self.api_key_edit.clear()

        self.base_url_label.setVisible(bool(spec.supports_base_url))
        self.base_url_edit.setVisible(bool(spec.supports_base_url))
        if spec.on_device:
            self.base_url_edit.setPlaceholderText("http://localhost:11434")
        else:
            self.base_url_edit.setPlaceholderText("leave blank for the default endpoint")

        # Reasoning effort is an Anthropic concept; hide it elsewhere.
        anthropic_selected = name == providers.AnthropicProvider.name
        self.effort_label.setVisible(anthropic_selected)
        self.effort_combo.setVisible(anthropic_selected)

        is_rules = name == providers.FALLBACK_PROVIDER
        self.batch_spin.setEnabled(not spec.on_device)
        self.batch_spin.setToolTip(
            "Not used: local backends gain nothing from batching, and small local "
            "models handle it badly."
            if spec.on_device else
            "Emails per request. The instructions are ~2,800 tokens and are re-sent "
            "with every request, so batching is the single biggest saving available."
        )
        self.fallback_check.setEnabled(not is_rules)
        if is_rules:
            self.fallback_check.setToolTip(
                "Not applicable: the rules engine is itself the fallback."
            )
        self.test_model_button.setText(
            "Check Ollama is running" if spec.on_device and not is_rules
            else ("Try the rule set" if is_rules else "Test this model")
        )
        self.refresh_models_button.setVisible(bool(spec.can_list_models))

    def _chosen_model(self) -> str:
        """The selected model id, or whatever the user typed instead.

        ``currentData()`` still points at the last *selected* item after the
        user types a name of their own, so the visible text has to be checked
        against that item's label before the id can be trusted.
        """
        text = self.model_combo.currentText().strip()
        index = self.model_combo.currentIndex()
        if index >= 0 and text == self.model_combo.itemText(index):
            return str(self.model_combo.itemData(index) or text)
        return text

    @Slot()
    def _refresh_models(self) -> None:
        """Replace the dropdown with what the service actually offers."""
        settings = self.collect()
        spec = providers.provider_class(settings.provider)
        if not spec.can_list_models:
            self.status.setText(f"{spec.label} does not publish a model list.")
            return
        self.refresh_models_button.setEnabled(False)
        self.status.setText("Asking the service for its model list…")
        QApplication.processEvents()
        provider = None
        try:
            provider = providers.build_provider(
                settings.provider,
                api_key=self.api_key_edit.text().strip(),
                model=settings.model,
                base_url=settings.base_url,
                timeout=30.0,
            )
            found = provider.list_models()
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self.status.setText(
                f"<span style='color:{ACCENT_RED}'>Could not fetch the model list: "
                f"{_html(str(exc)[:300])}</span>"
            )
            return
        finally:
            self.refresh_models_button.setEnabled(True)
            if provider is not None:
                provider.close()

        if not found:
            self.status.setText("The service returned no usable models.")
            return

        wanted = self._chosen_model()
        known = {choice.value: choice for choice in spec.models}
        self._loading_models = True
        self.model_combo.clear()
        for value in found:
            choice = known.get(value)
            self.model_combo.addItem(choice.label if choice else value, value)
        self._loading_models = False
        index = self.model_combo.findData(wanted)
        self.model_combo.setCurrentIndex(index if index >= 0 else 0)
        self._model_changed()
        self.status.setText(
            f"<span style='color:{ACCENT_GREEN}'>{spec.label} currently serves "
            f"{len(found)} model(s); the list above is now live.</span>"
        )

    def _model_changed(self) -> None:
        if getattr(self, "_loading_models", False):
            return
        name = self.provider_combo.currentData() or providers.DEFAULT_PROVIDER
        spec = providers.provider_class(name)
        value = self._chosen_model()
        note = next((c.note for c in spec.models if c.value == value), "")
        rate = spec.pricing.get(value)
        if spec.on_device:
            price = "free, and nothing leaves this Mac"
        elif rate:
            price = f"about ${rate[0]:.2f} in / ${rate[1]:.2f} out per million tokens"
        else:
            price = ""
        self.model_note.setText(" · ".join(part for part in (note, price) if part))

    def _build_appearance_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.mode_combo = QComboBox()
        for value, label in theme.MODES:
            self.mode_combo.addItem(label, value)
        form.addRow("Appearance", self.mode_combo)

        self.contrast_combo = QComboBox()
        for value, label in theme.CONTRASTS:
            self.contrast_combo.addItem(label, value)
        form.addRow("Contrast", self.contrast_combo)
        contrast_note = QLabel(
            "High contrast darkens the supporting colours until every one of "
            "them passes against its own background. Maximum goes further and "
            "drops colour altogether: black on white, or white on black, with "
            "nothing depending on hue."
        )
        contrast_note.setWordWrap(True)
        contrast_note.setProperty("dim", "true")
        form.addRow("", contrast_note)

        self.readable_check = QCheckBox("Tune the layout for reading")
        form.addRow("", self.readable_check)
        readable_note = QLabel(
            "Larger type with a little more tracking, taller rows, heavier "
            "column headings, a wider focus ring, and more space inside every "
            "control. Independent of contrast - it changes the spacing rather "
            "than the colours."
        )
        readable_note.setWordWrap(True)
        readable_note.setProperty("dim", "true")
        form.addRow("", readable_note)

        self.rows_spin = QSpinBox()
        self.rows_spin.setRange(1, 6)
        self.rows_spin.setSuffix(" lines per row")
        form.addRow("Row height", self.rows_spin)

        # Applied as they are changed: a colour choice you cannot see until you
        # press OK is a colour choice made blind.
        for widget in (self.mode_combo, self.contrast_combo):
            widget.currentIndexChanged.connect(self._preview_appearance)
        self.readable_check.toggled.connect(self._preview_appearance)
        return page

    def _preview_appearance(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        theme.apply(app,
                    self.mode_combo.currentData() or "system",
                    self.contrast_combo.currentData() or "normal",
                    self.readable_check.isChecked())

    def _build_folders_tab(self) -> QWidget:
        page = QWidget()
        form = QFormLayout(page)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.root_edit = QLineEdit()
        self.other_root_edit = QLineEdit()

        self.routing_combo = QComboBox()
        for member in NonJobRouting:
            self.routing_combo.addItem(member.label, member.value)
        self.routing_combo.currentIndexChanged.connect(self._routing_changed)

        self.auto_non_job_check = QCheckBox(
            "Pre-tick confidently classified non-job mail as well"
        )
        self.subscribe_check = QCheckBox("Subscribe to folders this app creates")

        self.folders_preview = QLabel("")
        self.folders_preview.setWordWrap(True)
        self.folders_preview.setTextFormat(Qt.TextFormat.RichText)
        self.root_edit.textChanged.connect(self._update_folder_preview)
        self.other_root_edit.textChanged.connect(self._update_folder_preview)

        form.addRow("Job Search folder", self.root_edit)
        form.addRow("Non-job mail", self.routing_combo)
        form.addRow("Sorted mail folder", self.other_root_edit)
        form.addRow("", self.auto_non_job_check)
        form.addRow("", self.subscribe_check)
        form.addRow(_separator())
        form.addRow("Will use", self.folders_preview)
        return page

    # -- values ----------------------------------------------------------
    def _load_values(self) -> None:
        settings = self._settings
        self.email_edit.setText(settings.icloud_email)
        self.host_edit.setText(settings.imap_host)
        self.port_spin.setValue(settings.imap_port)
        self.mailbox_edit.setText(settings.source_mailbox)
        self.connections_spin.setValue(settings.imap_connections)
        self.fetch_kb_spin.setValue(max(8, settings.fetch_bytes // 1024))

        provider_index = self.provider_combo.findData(settings.provider)
        self.provider_combo.setCurrentIndex(max(0, provider_index))
        self.base_url_edit.setText(settings.base_url)
        self._provider_changed()
        self.effort_combo.setCurrentText(settings.effort)
        self.threshold_spin.setValue(settings.confidence_threshold)
        self.concurrency_spin.setValue(settings.concurrency)
        self.batch_spin.setValue(settings.batch_size)
        self.body_chars_spin.setValue(settings.max_body_chars)
        self.max_messages_spin.setValue(settings.max_messages)
        self.fallback_check.setChecked(settings.fallback_to_rules)

        self.root_edit.setText(settings.folder_root)
        self.other_root_edit.setText(settings.other_folder_root)
        routing_index = self.routing_combo.findData(settings.routing.value)
        self.routing_combo.setCurrentIndex(max(0, routing_index))
        self.auto_non_job_check.setChecked(settings.auto_approve_non_job)
        self.subscribe_check.setChecked(settings.subscribe_new_folders)

        self.mode_combo.setCurrentIndex(
            max(0, self.mode_combo.findData(settings.appearance_mode)))
        self.contrast_combo.setCurrentIndex(
            max(0, self.contrast_combo.findData(settings.contrast)))
        self.readable_check.setChecked(settings.readable)
        self.rows_spin.setValue(settings.row_lines)

        try:
            self.password_edit.setText(self._store.get_icloud_password(settings.icloud_email))
            self.status.setText(f"Keychain backend: {self._store.backend_name()}")
        except CredentialError as exc:
            self.status.setText(f"<span style='color:{ACCENT_RED}'>{_html(str(exc))}</span>")

        self._routing_changed()
        self._update_folder_preview()

    def _routing_changed(self) -> None:
        routing = NonJobRouting.parse(self.routing_combo.currentData())
        filing = routing is NonJobRouting.FILE
        self.other_root_edit.setEnabled(filing)
        self.auto_non_job_check.setEnabled(filing)
        self._update_folder_preview()

    def _update_folder_preview(self) -> None:
        plan = FolderPlan(
            root=self.root_edit.text() or "Job Search",
            other_root=self.other_root_edit.text() or "Sorted Mail",
        )
        lines = [f"• {_html(folder)}" for folder in plan.leaf_folders]
        routing = NonJobRouting.parse(self.routing_combo.currentData())
        if routing is NonJobRouting.FILE:
            lines.append(
                f"• {_html(plan.other_root)}/… - one subfolder per topic, created only when used"
            )
        self.folders_preview.setText("<br>".join(lines))

    def collect(self) -> Settings:
        data = asdict(self._settings)
        data.update(
            icloud_email=self.email_edit.text().strip(),
            imap_host=self.host_edit.text().strip(),
            imap_port=self.port_spin.value(),
            source_mailbox=self.mailbox_edit.text().strip() or "INBOX",
            imap_connections=self.connections_spin.value(),
            fetch_bytes=self.fetch_kb_spin.value() * 1024,
            provider=self.provider_combo.currentData() or providers.DEFAULT_PROVIDER,
            model=self._chosen_model(),
            base_url=self.base_url_edit.text().strip(),
            effort=self.effort_combo.currentText(),
            appearance_mode=self.mode_combo.currentData() or "system",
            contrast=self.contrast_combo.currentData() or "normal",
            readable=self.readable_check.isChecked(),
            row_lines=self.rows_spin.value(),
            confidence_threshold=self.threshold_spin.value(),
            concurrency=self.concurrency_spin.value(),
            batch_size=self.batch_spin.value(),
            max_body_chars=self.body_chars_spin.value(),
            max_messages=self.max_messages_spin.value(),
            fallback_to_rules=self.fallback_check.isChecked(),
            folder_root=self.root_edit.text().strip() or "Job Search",
            other_folder_root=self.other_root_edit.text().strip() or "Sorted Mail",
            non_job_routing=NonJobRouting.parse(self.routing_combo.currentData()).value,
            auto_approve_non_job=self.auto_non_job_check.isChecked(),
            subscribe_new_folders=self.subscribe_check.isChecked(),
        )
        return Settings(**data).normalized()

    def persist_credentials(self, settings: Settings) -> None:
        self._store.set_icloud_password(settings.icloud_email, self.password_edit.text())
        if settings.needs_api_key:
            self._store.set_provider_key(settings.provider, self.api_key_edit.text())

    # -- tests -----------------------------------------------------------
    def _run_test(self, mode: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        settings = self.collect()
        if mode == "imap" and not settings.icloud_email:
            self.status.setText("Enter your iCloud email address first.")
            return
        self.test_imap_button.setEnabled(False)
        self.test_model_button.setEnabled(False)
        self.status.setText("Testing…")

        self._worker = ConnectionTestWorker(
            mode=mode,
            settings=settings,
            mailbox_password=self.password_edit.text(),
            api_key=self.api_key_edit.text(),
            parent=self,
        )
        self._worker.finished_ok.connect(self._test_succeeded)
        self._worker.failed.connect(self._test_failed)
        self._worker.finished.connect(self._test_finished)
        self._worker.start()

    @Slot(object)
    def _test_succeeded(self, result: dict) -> None:
        if result.get("mode") == "imap":
            folders = result.get("folders") or []
            job_folders = [f for f in folders if f.lower().startswith(self.root_edit.text().lower())]
            message = (
                f"Connected to {result['host']}. {result['folder_count']} mailboxes, "
                f"{result['inbox_messages']} messages in INBOX, hierarchy delimiter "
                f"“{result['delimiter']}”, UIDPLUS {'yes' if result['uidplus'] else 'no'}."
            )
            if job_folders:
                message += f" Existing triage folders: {', '.join(job_folders)}."
        else:
            cost = result.get("cost") or 0.0
            price = "free (on this Mac)" if result.get("on_device") else f"≈${cost:.5f} for this call"
            message = (
                f"{result.get('provider_label', 'The model')} · {result['model']} replied in "
                f"{result['seconds']}s - the test email was classified as "
                f"{result['category']} at {result['confidence'] * 100:.0f}% confidence "
                f"({result['input_tokens']:,} in / {result['output_tokens']:,} out, {price})."
            )
            for note in result.get("degradations") or ():
                message += f"\nNote: {note}"
        self.status.setText(f"<span style='color:{ACCENT_GREEN}'>{_html(message)}</span>")

    @Slot(str, str)
    def _test_failed(self, title: str, detail: str) -> None:
        self.status.setText(
            f"<span style='color:{ACCENT_RED}'><b>{_html(title)}</b><br>{_html(detail)}</span>"
        )

    @Slot()
    def _test_finished(self) -> None:
        self.test_imap_button.setEnabled(True)
        self.test_model_button.setEnabled(True)

    def _stop_test_worker(self) -> None:
        worker, self._worker = self._worker, None
        if worker is not None and worker.isRunning() and not worker.stop(3000):
            _abandon(worker)

    def done(self, result: int) -> None:  # noqa: N802
        """Qt funnels OK, Cancel, Escape and the close box through here."""
        self._stop_test_worker()
        super().done(result)

    def closeEvent(self, event) -> None:  # noqa: N802
        self._stop_test_worker()
        super().closeEvent(event)


# ==========================================================================
# Main window
# ==========================================================================
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
        #: Every background thread this window has started and not yet reaped.
        self._workers: List[QThread] = []
        self.folder_plan: Optional[FolderPlan] = settings.folder_plan()
        #: Set only by an explicit Quit, so closeEvent can tell "put this
        #: away" apart from "stop the app".
        self._quitting = False
        #: What the primary button currently does, so it can be rewired
        #: without disconnecting slots that were never attached.
        self._scan_button_action = None
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
        self._reset_columns()

        # A blank grid on first launch tells the user nothing; this does.
        self.empty_label = QLabel(EMPTY_STATE)
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setTextFormat(Qt.TextFormat.RichText)
        self.empty_label.setWordWrap(True)
        self.table_stack = QStackedWidget()
        self.table_stack.addWidget(self.empty_label)   # index 0
        self.table_stack.addWidget(self.table)         # index 1
        self.model.modelReset.connect(self._sync_table_stack)

        self.preview = PreviewPane()
        self.preview.overrideChanged.connect(self._override_changed)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setFont(_mono_font())
        self.log_view.setVisible(self.settings.show_log_panel)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.splitter.addWidget(self.table_stack)
        self.splitter.addWidget(self.preview)
        self.splitter.addWidget(self.log_view)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)
        self.splitter.setSizes([420, 300, 0])
        layout.addWidget(self.splitter, 1)

        self.setCentralWidget(central)

        self.status_label = QLabel("Ready.")
        self.status_label.setMinimumWidth(120)
        self.status_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.usage_label = QLabel("")
        self.usage_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.usage_label)
        self.statusBar().setSizeGripEnabled(True)

    #: Default column widths, also used by View -> Reset column widths.
    COLUMN_WIDTHS = {
        TriageTableModel.COL_SELECT: 34,
        TriageTableModel.COL_SENDER: 165,
        TriageTableModel.COL_SUBJECT: 250,
        TriageTableModel.COL_DATE: 112,
        TriageTableModel.COL_CATEGORY: 172,
        TriageTableModel.COL_FOLDER: 128,
        TriageTableModel.COL_CONFIDENCE: 92,
        # The reasoning is shown in full in the preview pane, so the summary -
        # which is the column people actually read across - gets the stretch.
        TriageTableModel.COL_REASONING: 210,
        TriageTableModel.COL_ACCOUNT: 120,
    }

    def _reset_columns(self) -> None:
        for column, width in self.COLUMN_WIDTHS.items():
            self.table.setColumnWidth(column, width)
        self._sync_account_column()

    def _sync_account_column(self) -> None:
        """The mailbox column earns its space only when there is a choice."""
        if not hasattr(self, "table"):
            return
        self.table.setColumnHidden(
            TriageTableModel.COL_ACCOUNT, not self.settings.multi_account)

    def _apply_density(self, lines: int) -> None:
        """Switch between one-line rows and wrapped multi-line rows."""
        lines = max(1, min(6, int(lines)))
        self.settings.row_lines = lines
        metrics = QFontMetrics(self.table.font())
        self.table.verticalHeader().setDefaultSectionSize(
            metrics.lineSpacing() * lines + 12
        )
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

        self.window_buttons: Dict[TimeWindow, QToolButton] = {}
        for window in TimeWindow:
            button = QToolButton()
            button.setText(window.label)
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setMinimumHeight(28)
            button.clicked.connect(lambda checked, w=window: self._window_selected(w))
            self.window_buttons[window] = button
            row.addWidget(button)
        self.window_buttons[self.settings.window].setChecked(True)

        self.window_label = QLabel()
        self.window_label.setToolTip("The period the next scan will cover.")
        row.addWidget(self.window_label)

        # The calendar popup is not built here: setCalendarPopup constructs a
        # full QCalendarWidget, which is about a tenth of a second per field.
        # _sync_range_visibility turns it on the first time the fields show.
        self.start_date = QDateEdit()
        self.start_date.setDisplayFormat("d MMM yyyy")
        self.end_date = QDateEdit()
        self.end_date.setDisplayFormat("d MMM yyyy")
        today = QDate.currentDate()
        self.start_date.setDate(_stored_date(self.settings.custom_start, today.addDays(-7)))
        self.end_date.setDate(_stored_date(self.settings.custom_end, today))
        self.start_date.dateChanged.connect(self._refresh_window_label)
        self.end_date.dateChanged.connect(self._refresh_window_label)
        self.range_widgets = [QLabel("from"), self.start_date, QLabel("to"), self.end_date]
        for widget in self.range_widgets:
            row.addWidget(widget)

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
        self.model_button.setMinimumHeight(30)
        self.model_button.setStyleSheet("QToolButton { padding: 4px 22px 4px 10px; }")
        self.model_menu = QMenu(self)
        self.model_button.setMenu(self.model_menu)
        row.addWidget(self.model_button)

        # Only worth the space once there is more than one mailbox.
        self.account_button = QToolButton()
        self.account_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.account_button.setMinimumHeight(30)
        self.account_button.setStyleSheet("QToolButton { padding: 4px 22px 4px 10px; }")
        self.account_menu = QMenu(self)
        self.account_button.setMenu(self.account_menu)
        row.addWidget(self.account_button)

        self.stop_button = QPushButton("Stop All")
        self.stop_button.setMinimumHeight(30)
        self.stop_button.setEnabled(False)
        _paint_button(self.stop_button, ACCENT_RED)
        self.stop_button.setToolTip(
            "Stop every running task and close its network connections (⌘.)"
        )
        self.stop_button.clicked.connect(self.stop_all)
        row.addWidget(self.stop_button)

        self.scan_button = QPushButton("Scan && Analyze")
        self.scan_button.setMinimumHeight(30)
        # Width is pinned to the wider of its two labels so the toolbar does
        # not jump when it turns into Stop.
        self.scan_button.setMinimumWidth(
            self.scan_button.fontMetrics().horizontalAdvance("Scan & Analyze") + 34)
        self._set_scan_button(False)
        row.addWidget(self.scan_button)

        self.apply_button = QPushButton("Apply Approved Folder Moves")
        self.apply_button.setMinimumHeight(30)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_moves)
        _paint_button(self.apply_button, ACCENT_GREEN)
        row.addWidget(self.apply_button)

        self._sync_range_visibility()
        self._rebuild_account_menu()
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
        profile_menu = self.model_menu.addMenu("What to sort")
        for name, label, blurb in profiles.choices():
            action = QAction(menu_text(label), self)
            action.setCheckable(True)
            action.setChecked(name == self.settings.sort_profile)
            action.setStatusTip(blurb)
            action.setToolTip(blurb)
            action.triggered.connect(
                lambda checked=False, p=name: self._switch_profile(p))
            profile_menu.addAction(action)

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
            _paint_button(self.scan_button, ACCENT_RED)
            self.scan_button.setToolTip(
                "Stop everything now: the mailbox fetch, the model requests and "
                "the local sorter, and close the connections they are using (⌘.)"
            )
            self.scan_button.clicked.connect(wanted)
        else:
            self.scan_button.setText("Scan && Analyze")
            self.scan_button.setDefault(True)
            _paint_button(self.scan_button, ACCENT_BLUE)
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
        self.account_button.setText(menu_text(f"✉︎  {name}"))
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
                    self.settings.readable)
        # Reading mode wants taller rows as well as larger type; the two only
        # help together.
        if self.settings.readable and self.settings.row_lines < 3:
            self.settings.row_lines = 3
        self._apply_density(self.settings.row_lines)
        self._reset_columns()
        self.table.viewport().update()

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
        self._rebuild_model_menu()

    def _retarget_items(self) -> None:
        """Re-file the rows already on screen under the new folder plan."""
        items = list(self.model.items)
        if not items:
            return
        for item in items:
            item.folders = self.folder_plan
            item.non_job_routing = self.settings.routing
        self.model.set_items(items)
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

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by sender, subject, summary or reasoning…")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.textChanged.connect(self.proxy.set_text_filter)
        self.search_edit.setMinimumWidth(220)
        self.search_edit.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self.search_edit)

        self.category_filter = QComboBox()
        self.category_filter.addItem("All categories", None)
        self.category_filter.currentIndexChanged.connect(
            lambda: self.proxy.set_category_filter(self.category_filter.currentData())
        )
        row.addWidget(self.category_filter)

        # One "Show" menu instead of a row of competing checkboxes.
        self.show_combo = QComboBox()
        self.show_combo.addItem("Show: everything", SHOW_ALL)
        self.show_combo.addItem("Show: job mail only", SHOW_JOB_ONLY)
        self.show_combo.addItem("Show: ticked only", SHOW_SELECTED)
        self.show_combo.setCurrentIndex(1 if self.settings.hide_non_job else 0)
        self.show_combo.currentIndexChanged.connect(self._show_filter_changed)
        row.addWidget(self.show_combo)
        self._show_filter_changed()

        self.select_high_button = QPushButton("Tick high confidence")
        self.select_high_button.setToolTip(
            "Tick every message the analysis was confident about (⌘⇧A)"
        )
        self.select_high_button.clicked.connect(
            lambda: self.model.set_all_approved(True, only_high_confidence=True)
        )
        row.addWidget(self.select_high_button)

        self.deselect_button = QPushButton("Clear ticks")
        self.deselect_button.clicked.connect(lambda: self.model.set_all_approved(False))
        row.addWidget(self.deselect_button)

        return frame

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

        apply_action = QAction("&Apply Approved Folder Moves", self)
        apply_action.setShortcut(QKeySequence("Ctrl+Return"))
        apply_action.triggered.connect(self.apply_moves)
        file_menu.addAction(apply_action)

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
        quit_action.triggered.connect(QApplication.quit)
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
        setup = QAction("Run setup again...", self)
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
        if self.settings.table_state:
            self.table.horizontalHeader().restoreState(
                QByteArray.fromBase64(self.settings.table_state.encode())
            )

    def showEvent(self, event) -> None:  # noqa: N802
        """Run the first-run prompt the first time the window actually appears.

        A queued timer would be simpler but can fire after the window is gone,
        or between two unrelated operations; tying it to the show event means it
        happens exactly once, at the only moment it makes sense.
        """
        super().showEvent(event)
        if not self._first_run_checked:
            self._first_run_checked = True
            QTimer.singleShot(0, self, self._first_run_check)
            QTimer.singleShot(0, self, self._probe_api_keys)

    def closeEvent(self, event) -> None:  # noqa: N802
        if not self._quitting and self._hides_to_menu_bar():
            # The menu bar item is still there, so closing the window means
            # "put it away", not "stop working". Quit from the menu bar, the
            # app menu, or Cmd-Q to actually leave.
            self._save_layout()
            self.hide()
            event.ignore()
            return
        self.shutdown()
        self._save_layout()
        super().closeEvent(event)

    def _save_layout(self) -> None:
        """Remember the window's shape, whether it is closing or just hiding."""
        self.settings.window_geometry = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.splitter_state = bytes(self.splitter.saveState().toBase64()).decode()
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
        self._quitting = True
        self.close()
        QApplication.quit()

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
        dialog = SettingsDialog(self.settings, self.store, self)
        if tab:
            dialog.tabs.setCurrentIndex(tab)
        if dialog.exec() != QDialog.DialogCode.Accepted:
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
        self.table.setItemDelegateForColumn(
            TriageTableModel.COL_CONFIDENCE,
            ConfidenceDelegate(self.settings.confidence_threshold, self.table),
        )
        self._rebuild_model_menu()
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
        for widget in self.range_widgets:
            widget.setVisible(custom)
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

        self.window_label.setText(
            f"<span style='opacity:0.7'>covering</span> "
            f"<b>{spell(start)}</b> "
            f"<span style='opacity:0.7'>to</span> <b>{spell(end)}</b>"
        )
        self.window_label.setTextFormat(Qt.TextFormat.RichText)

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
    def start_scan(self) -> None:
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
        self.model.set_items(outcome.items)
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

    # -- applying --------------------------------------------------------
    @Slot()
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
        self.stop_button.setEnabled(False)
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
        self.stop_button.setEnabled(busy)
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
        self.model.set_override(row, folder)
        self._update_status()

    def _select_first_row(self) -> None:
        if self.proxy.rowCount() > 0:
            self.table.selectRow(0)

    def _refresh_category_filter(self) -> None:
        current = self.category_filter.currentData()
        labels = sorted({item.classification.category_label for item in self.model.items})
        colors = {
            item.classification.category_label: category_color(item.classification)
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
        self.apply_button.setText(
            f"Apply {summary.approved} Approved Folder Move{'s' if summary.approved != 1 else ''}"
            if summary.approved
            else "Apply Approved Folder Moves"
        )
        self.stop_button.setEnabled(running)
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
        self.table_stack.setCurrentIndex(1 if self.model.rowCount() else 0)

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
        super().resizeEvent(event)
        if getattr(self, "_status_text", None):
            self._set_status(self._status_text)

    def _toggle_log(self, visible: bool) -> None:
        self.log_view.setVisible(visible)
        if visible:
            sizes = self.splitter.sizes()
            if sizes[-1] < 60:
                self.splitter.setSizes([sizes[0], max(160, sizes[1] - 140), 140])

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
        self.search_edit.clear()
        self.category_filter.setCurrentIndex(0)
        self.show_combo.setCurrentIndex(0)

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

    def _about(self) -> None:
        QMessageBox.about(
            self,
            f"About {APP_DISPLAY_NAME}",
            f"<h3>{APP_DISPLAY_NAME} {APP_VERSION}</h3>"
            "<p>Scans iCloud Mail over IMAP, summarises and categorises job-search email "
            "with your chosen model backend, and files it only after you approve each move.</p>"
            f"<p>Backend: <code>{_html(self.settings.provider_label)}</code><br>"
            f"Model: <code>{_html(self.settings.model)}</code><br>"
            f"Confidence threshold: {self.settings.confidence_threshold * 100:.0f}%<br>"
            f"Keychain: <code>{_html(self.store.backend_name())}</code></p>",
        )


# ==========================================================================
# Helpers
# ==========================================================================
#: QThreads that outlived their window. Referenced so Qt never destroys a
#: running QThread (which aborts the process), and so Python cannot collect it.
_ABANDONED: List[QThread] = []


def _abandon(worker: QThread) -> None:
    """Detach a thread that will not stop in time.

    ``QThread.terminate()`` is not an option: these threads run Python, so
    killing one can leave the GIL held and deadlock the whole application -
    exactly the failure this is meant to prevent. Instead the worker is cut
    loose: its signals are disconnected so it can never touch the UI again, and
    a reference is kept so Qt does not abort on destroying a running thread.

    Every operation the workers perform is bounded (IMAP and HTTP both carry
    timeouts, and stopping closes the HTTP pool), so an abandoned thread ends
    on its own shortly afterwards and the process exits normally.
    """
    name = getattr(worker, "task_name", "task")
    log.warning("Detaching a %s thread that did not stop in time.", name)
    try:
        worker.disconnect()
    except (RuntimeError, TypeError):  # no connections left
        pass
    worker.setParent(None)
    if worker not in _ABANDONED:
        _ABANDONED.append(worker)


def _format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def _chip(label: str, value: str, color: str = "") -> str:
    """One `label value` pair.

    Spacing uses non-breaking spaces rather than CSS margins: Qt's rich-text
    subset silently ignores `margin` on an inline span, which runs every chip
    into the next one.
    """
    shade = f" style='color:{color}'" if color else ""
    return f"<span style='opacity:0.6'>{label}</span>&nbsp;<b{shade}>{value}</b>"


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


def _swatch(color: str, size: int = 12) -> "QIcon":
    """A small round colour chip, used in the category dropdown."""
    from PySide6.QtGui import QIcon, QPixmap

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(color))
    painter.drawEllipse(1, 1, size - 2, size - 2)
    painter.end()
    return QIcon(pixmap)


def _paint_button(button, color: str, bold: bool = True) -> None:
    """Give a button a solid accent colour that works in both themes.

    Weight is set on the QFont rather than in the stylesheet: a ``font-weight``
    rule makes Qt re-resolve the family and quietly drop the macOS system font
    for a synthesised fallback.
    """
    if bold:
        font = button.font()
        font.setWeight(QFont.Weight.DemiBold)
        button.setFont(font)
    button.setStyleSheet(
        f"""
        QPushButton {{
            background-color: {color};
            color: #FFFFFF;
            border: none;
            border-radius: 6px;
            padding: 6px 14px;
        }}
        QPushButton:hover:!disabled {{ background-color: {_shade(color, 1.12)}; }}
        QPushButton:pressed        {{ background-color: {_shade(color, 0.88)}; }}
        QPushButton:disabled {{
            background-color: rgba(140, 140, 140, 0.16);
            color: rgba(140, 140, 140, 0.95);
        }}
        """
    )


def _shade(hex_color: str, factor: float) -> str:
    """Lighten (factor > 1) or darken (factor < 1) a #rrggbb colour."""
    value = hex_color.lstrip("#")
    channels = [int(value[i:i + 2], 16) for i in (0, 2, 4)]
    scaled = [max(0, min(255, round(channel * factor))) for channel in channels]
    return "#" + "".join(f"{channel:02X}" for channel in scaled)


def _tint(hex_color: str, alpha: int) -> QColor:
    """A translucent version of a colour, for row backgrounds."""
    color = QColor(hex_color)
    color.setAlpha(alpha)
    return color


def category_color(classification) -> str:
    """The accent colour for a row: its job category, or the neutral 'other'."""
    if not classification.is_job_related:
        return OTHER_COLOR
    return CATEGORY_COLORS.get(classification.category, OTHER_COLOR)


def _stored_date(value: str, fallback: QDate) -> QDate:
    """Read a persisted ISO date, falling back when it is absent or unusable."""
    parsed = config.parse_iso(value)
    if parsed is None:
        return fallback
    date = QDate(parsed.year, parsed.month, parsed.day)
    return date if date.isValid() else fallback


def _one_line(text: str, limit: int = 300) -> str:
    collapsed = " ".join((text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def _wrap(text: str, width: int = 96) -> str:
    import textwrap

    return "\n".join(textwrap.wrap(" ".join((text or "").split()), width=width)) or ""


def _html(text: str) -> str:
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def system_font() -> QFont:
    """The macOS UI font (SF Pro), asked for the way Apple intends."""
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)


def _mono_font() -> QFont:
    """The macOS system monospace font.

    Asked for by role rather than by name: probing for "SF Mono" forces Qt to
    populate its font-alias table on every launch, which costs ~85 ms and then
    falls back to Menlo anyway, because SF Mono ships with Terminal rather than
    as a general system family.
    """
    font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
    font.setStyleHint(QFont.StyleHint.Monospace)
    font.setPointSize(12)
    return font


def _scrollable(page: QWidget) -> QScrollArea:
    """Wrap a settings page so it scrolls rather than clipping."""
    area = QScrollArea()
    area.setWidget(page)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    return area


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


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
