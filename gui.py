"""PySide6 user interface for iCloud Mail Job Triage."""

from __future__ import annotations

import csv
import json
import logging
import os
import subprocess
import sys
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import (
    QUrl,
    QAbstractTableModel,
    QEvent,
    QRect,
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
    QDoubleValidator,
    QFontMetrics,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPalette,
)
from PySide6.QtWidgets import (
    QSlider,
    QListWidgetItem,
    QListWidget,
    QFileDialog,
    QAbstractItemView,
    QStackedWidget,
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
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
from imap_engine import MovePlan, MoveReport, clean_secret
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
import accounts
import autoreply
import helpmode
import ondevice
import theme
from accounts import Account
from menubar import MenuBarController
from welcome import SetupWizard
from workers import (
    ReplyWorker,
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
class AdaptiveLineEdit(QLineEdit):
    """A line edit whose hint text shrinks to fit the width it is given.

    No layout can make a long sentence fit a narrow field, and eliding it into
    "Filter by sender, subj…" tells the reader less than a short phrase would.
    So several phrasings are supplied and the longest one that actually fits is
    shown. The full version is always the tooltip.
    """

    def __init__(self, *hints: str, parent=None) -> None:
        super().__init__(parent)
        self._hints = [h for h in hints if h] or [""]
        self.setToolTip(self._hints[0])
        self._choose()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._choose()

    def _choose(self) -> None:
        metrics = self.fontMetrics()
        # Room for the frame, the padding either side and the clear button.
        room = self.width() - 34 - (24 if self.isClearButtonEnabled() else 0)
        for hint in self._hints:
            if room <= 0 or metrics.horizontalAdvance(hint) <= room:
                if self.placeholderText() != hint:
                    self.setPlaceholderText(hint)
                return
        shortest = self._hints[-1]
        if self.placeholderText() != shortest:
            self.setPlaceholderText(shortest)


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
                return item.email.mailbox_display
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
        #: Empty means every mailbox. Filtering the view is separate from
        #: choosing what to scan: you can pull six mailboxes in and then read
        #: them one at a time.
        self._accounts: set = set()

    def set_text_filter(self, text: str) -> None:
        self._text = (text or "").strip().lower()
        self.invalidate()

    def set_category_filter(self, category: Optional[str]) -> None:
        self._category = category
        self.invalidate()

    def set_hide_non_job(self, hide: bool) -> None:
        self._hide_non_job = bool(hide)
        self.invalidate()

    def set_account_filter(self, account_ids) -> None:
        self._accounts = set(account_ids or ())
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
        if self._accounts and item.email.account_id not in self._accounts:
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
class WrappingList(QListWidget):
    """A list whose items wrap onto as many lines as their text needs.

    QListWidget will wrap, but it decides how many lines an item needs from a
    width measured before the scroll bar is accounted for, so an entry that is
    a few points too long is elided while its neighbours wrap. Measuring each
    item here and saying how tall it is removes the guess.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setTextElideMode(Qt.TextElideMode.ElideNone)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.measure()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self.measure()

    def changeEvent(self, event) -> None:  # noqa: N802
        """A theme or a readability setting changes the font under us."""
        super().changeEvent(event)
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange,
                            QEvent.Type.ApplicationFontChange):
            self.measure()

    def measure(self) -> None:
        """Give every item the height its wrapped text actually needs.

        The width the delegate lays text out in is asked for rather than
        guessed at: the checkbox, the margins and the frame all take their cut
        first, and guessing that cut is how an entry ends up a line short.
        """
        if not self.count():
            return
        style = self.style()
        metrics = self.fontMetrics()
        width = self.viewport().width()
        for index in range(self.count()):
            item = self.item(index)
            option = QStyleOptionViewItem()
            self.initViewItemOption(option)
            option.rect = QRect(0, 0, width, metrics.height())
            option.features |= QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
            text_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, option, self)
            room = max(40, text_rect.width() - 4)
            # The same call says how much height the style keeps for itself,
            # which is the part a hand-written guess always gets wrong.
            chrome = max(4, option.rect.height() - text_rect.height())
            bounds = metrics.boundingRect(
                QRect(0, 0, room, 0),
                int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft),
                item.text())
            item.setSizeHint(QSize(
                width, max(metrics.height(), bounds.height()) + chrome + 2))


def _compact_button(text: str, tip: str, slot) -> QToolButton:
    """A small square button for adding and removing lines.

    The theme gives every button generous padding, which is right for the ones
    people press and wrong for a column of five that only need to hold one
    character. These are sized to the character instead.
    """
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.setProperty("compact", "true")
    button.setFixedSize(26, 26)
    button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
    button.clicked.connect(slot)
    return button


class _RuleRow(QWidget):
    """One line of a rule: some combo boxes, a value, and a way to delete it.

    Conditions and actions are close enough to share the plumbing. What
    differs is which combo boxes there are and what a value looks like, and
    both subclasses answer that in `_value_kind`.
    """

    changed = Signal()
    removed = Signal(object)

    def __init__(self, mailboxes=None, folders=None, parent=None) -> None:
        super().__init__(parent)
        self._mailboxes = list(mailboxes or [])
        self._folders = list(folders or [])
        self._value_widget: Optional[QWidget] = None
        self._quiet = False

        # A tall editor - a template, some guidance - goes underneath rather
        # than in the line, or the combo boxes beside it float in the middle
        # of a hundred points of nothing.
        self.stack = QVBoxLayout(self)
        self.stack.setContentsMargins(0, 0, 0, 0)
        self.stack.setSpacing(4)
        self.row = QHBoxLayout()
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.stack.addLayout(self.row)

    # -- the value editor, which changes shape with the field --------------
    #: Value editors too tall to sit in the line with the combo boxes.
    TALL = ("template", "guidance")

    def _build_value(self, kind: str, value: str) -> None:
        """Swap in the editor this kind of value deserves."""
        if self._value_widget is not None:
            self._value_widget.setParent(None)
            self._value_widget.deleteLater()
            self._value_widget = None
        widget = self._make_value_widget(kind, value)
        self._value_widget = widget
        if widget is None:
            return
        if kind in self.TALL:
            self.stack.addWidget(widget)
        else:
            self.row.insertWidget(self.row.count() - 1, widget, 3)

    def _make_value_widget(self, kind: str, value: str) -> Optional[QWidget]:
        if kind == "none":
            spacer = QLabel("")
            spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                                 QSizePolicy.Policy.Preferred)
            return spacer
        if kind in ("category", "topic", "mailbox", "folder_pick"):
            combo = QComboBox()
            combo.setEditable(kind == "folder_pick")
            combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                QSizePolicy.Policy.Fixed)
            combo.setMinimumWidth(84)
            for item_value, label in self._choices(kind):
                combo.addItem(label, item_value)
            found = combo.findData(value)
            if found >= 0:
                combo.setCurrentIndex(found)
            elif combo.isEditable():
                combo.setEditText(value)
            combo.currentIndexChanged.connect(self._touched)
            if combo.isEditable():
                combo.editTextChanged.connect(self._touched)
            return combo
        if kind in ("template", "guidance"):
            box = QPlainTextEdit()
            box.setPlainText(value)
            box.setMinimumHeight(84)
            box.setMaximumHeight(150)
            box.setPlaceholderText(
                "Hello {first_name},\n\n…\n\nBest wishes,\n{me}"
                if kind == "template" else
                "What the reply has to do, in your own words."
            )
            box.textChanged.connect(self._touched)
            return box
        if kind == "number":
            edit = AdaptiveLineEdit("0.90")
            edit.setValidator(QDoubleValidator(0.0, 100000.0, 3))
        else:
            edit = AdaptiveLineEdit("what to look for", "text")
        edit.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        edit.setMinimumWidth(70)
        edit.setText(value)
        edit.textChanged.connect(self._touched)
        return edit

    def _choices(self, kind: str) -> List[Tuple[str, str]]:
        if kind == "category":
            return [(c.value, c.label) for c in Category
                    if c is not Category.UNCLASSIFIED_OTHER]
        if kind == "topic":
            return [(t.value, t.label) for t in profiles.ALL_TOPICS]
        if kind == "mailbox":
            return [(a.address or a.id, a.address or a.label) for a in self._mailboxes]
        return [(f, f) for f in self._folders]

    def _value_text(self) -> str:
        widget = self._value_widget
        if isinstance(widget, QComboBox):
            return (widget.currentData() if not widget.isEditable()
                    else widget.currentText()) or ""
        if isinstance(widget, QPlainTextEdit):
            return widget.toPlainText()
        if isinstance(widget, QLineEdit):
            return widget.text()
        return ""

    def _touched(self, *_args) -> None:
        if not self._quiet:
            self.changed.emit()

    def _delete_button(self) -> QToolButton:
        return _compact_button("−", "Remove this line",
                               lambda: self.removed.emit(self))


class ConditionRow(_RuleRow):
    """Field, operator, value - the shape every mail rule has ever had."""

    def __init__(self, condition, mailboxes=None, parent=None) -> None:
        super().__init__(mailboxes=mailboxes, parent=parent)
        self._quiet = True

        self.field_combo = QComboBox()
        for name, label, _kind in autoreply.FIELDS:
            self.field_combo.addItem(label, name)
        self.field_combo.setCurrentIndex(
            max(0, self.field_combo.findData(condition.field)))
        self.field_combo.setMinimumWidth(96)
        self.field_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                       QSizePolicy.Policy.Fixed)
        self.field_combo.currentIndexChanged.connect(self._field_changed)
        self.row.addWidget(self.field_combo, 3)
        self._explain_field()

        self.operator_combo = QComboBox()
        self.operator_combo.setMinimumWidth(88)
        self.operator_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Fixed)
        self.operator_combo.currentIndexChanged.connect(self._operator_changed)
        self.row.addWidget(self.operator_combo, 3)

        self.row.addWidget(self._delete_button())
        self._fill_operators(condition.operator)
        self._build_value(self._value_kind(), condition.value)
        self._quiet = False

    def _value_kind(self) -> str:
        field = self.field_combo.currentData() or "anywhere"
        kind = autoreply.field_kind(field)
        if kind == "flag":
            return "none"
        return kind

    def _fill_operators(self, wanted: str = "") -> None:
        was_quiet, self._quiet = self._quiet, True
        self.operator_combo.blockSignals(True)
        self.operator_combo.clear()
        field = self.field_combo.currentData() or "anywhere"
        for name, label in autoreply.operators_for(field):
            self.operator_combo.addItem(label, name)
        found = self.operator_combo.findData(wanted)
        self.operator_combo.setCurrentIndex(max(0, found))
        self.operator_combo.blockSignals(False)
        self._quiet = was_quiet

    def _explain_field(self) -> None:
        """Say what this field looks at, for the help switch to show."""
        field = self.field_combo.currentData() or "anywhere"
        self.field_combo.setToolTip(autoreply.FIELD_HELP.get(field, ""))

    def _field_changed(self) -> None:
        self._fill_operators()
        self._build_value(self._value_kind(), "")
        self._explain_field()
        self._touched()

    def _operator_changed(self) -> None:
        self._touched()

    def value(self):
        return autoreply.Condition(
            field=self.field_combo.currentData() or "anywhere",
            operator=self.operator_combo.currentData() or "contains",
            value=self._value_text(),
        )


class ActionRow(_RuleRow):
    """What to do, and whatever that needs typing into it."""

    def __init__(self, action, folders=None, parent=None) -> None:
        super().__init__(folders=folders, parent=parent)
        self._quiet = True

        self.kind_combo = QComboBox()
        for name, label, _needs in autoreply.ACTION_KINDS:
            self.kind_combo.addItem(label, name)
        self.kind_combo.setCurrentIndex(max(0, self.kind_combo.findData(action.kind)))
        self.kind_combo.setMinimumWidth(130)
        self.kind_combo.setSizePolicy(QSizePolicy.Policy.Ignored,
                                      QSizePolicy.Policy.Fixed)
        self.kind_combo.currentIndexChanged.connect(self._kind_changed)
        self.row.addWidget(self.kind_combo, 3)
        self._explain_kind()

        self.row.addWidget(self._delete_button())
        self._build_value(self._value_kind(), action.value)
        self._quiet = False

    def _value_kind(self) -> str:
        needs = autoreply.action_input(self.kind_combo.currentData() or "draft")
        return "folder_pick" if needs == "folder" else needs

    def _explain_kind(self) -> None:
        kind = self.kind_combo.currentData() or "draft"
        self.kind_combo.setToolTip(autoreply.ACTION_HELP.get(kind, ""))

    def _kind_changed(self) -> None:
        self._build_value(self._value_kind(), "")
        self._explain_kind()
        self._touched()

    def value(self):
        return autoreply.Action(kind=self.kind_combo.currentData() or "draft",
                                value=self._value_text())


class SettingsDialog(QDialog):
    """Credentials, model, routing and folder configuration."""

    def __init__(self, settings: Settings, store: CredentialStore, parent=None,
                 sample_items: Sequence[TriageItem] = ()) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumSize(640, 480)
        self.resize(760, 680)
        self._settings = settings
        self._store = store
        self._worker: Optional[ConnectionTestWorker] = None
        self._loading_models = False
        self._provider_seen = ""
        #: Working copies. Nothing is written until OK.
        self._accounts: List[Account] = []
        self._account_index = 0
        self._account_passwords: Dict[str, str] = {}
        self._removed_accounts: List[Account] = []
        self._row_lines_touched = False
        self._loading_rule = False
        #: What is on screen behind this dialog, so a rule can be tried on it.
        self._sample_items: List[TriageItem] = list(sample_items)

        self.tabs = QTabWidget()
        # Each tab scrolls, so no amount of text can be cut off at any size.
        self.tabs.addTab(_scrollable(self._build_account_tab()), "Mailboxes")
        self.tabs.addTab(_scrollable(self._build_ai_tab()), "Analysis")
        self.tabs.addTab(_scrollable(self._build_folders_tab()), "Folders")
        self.tabs.addTab(_scrollable(self._build_reply_tab()), "Auto Reply")
        self.tabs.addTab(_scrollable(self._build_appearance_tab()), "Appearance")

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)

        self.status = QLabel("")
        self.status.setWordWrap(True)

        # The same help switch as the window's, because most of the writing
        # that benefits from it is in here.
        self.help_button = helpmode.HelpButton()
        self.help_button.setChecked(settings.help_mode)
        self.help_button.toggled.connect(self._toggle_help)
        header = QHBoxLayout()
        header.addStretch(1)
        header.addWidget(self.help_button)

        layout = QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self.tabs)
        layout.addWidget(self.status)
        layout.addWidget(self.buttons)

        self._load_values()

    # -- tabs ------------------------------------------------------------
    def _build_account_tab(self) -> QWidget:
        """Every mailbox, in a list you can see all of at once.

        The previous version hid the list behind a dropdown and captured edits
        silently when you moved away from a field, which left no way to tell
        whether anything had been kept. This shows every mailbox, marks which
        ones are ready to scan, and writes each edit into the selected mailbox
        as it is typed - so the list is the confirmation.
        """
        page = QWidget()
        outer = QVBoxLayout(page)

        self.account_list = QListWidget()
        self.account_list.setMinimumHeight(130)
        self.account_list.setAlternatingRowColors(True)
        self.account_list.currentRowChanged.connect(self._account_selected)
        self.account_list.itemChanged.connect(self._account_ticked)
        self.account_list.setToolTip(
            "Every mailbox this app knows about. Untick one to leave it out of "
            "scans without forgetting it."
        )
        outer.addWidget(self.account_list)

        buttons = QHBoxLayout()
        self.add_account_button = QPushButton("Add mailbox")
        _paint_button(self.add_account_button, "primary", bold=False)
        self.add_account_button.setToolTip("Set up another mailbox")
        self.add_account_button.clicked.connect(self._add_account)
        buttons.addWidget(self.add_account_button)

        self.remove_account_button = QPushButton("Remove")
        _paint_button(self.remove_account_button, "destructive", bold=False)
        self.remove_account_button.setToolTip(
            "Forget the selected mailbox. Nothing in it is touched, and its "
            "password is removed from the Keychain."
        )
        self.remove_account_button.clicked.connect(self._remove_account)
        buttons.addWidget(self.remove_account_button)

        self.test_imap_button = QPushButton("Test this mailbox")
        self.test_imap_button.setToolTip(
            "Sign in to the selected mailbox now and report what happens")
        self.test_imap_button.clicked.connect(lambda: self._run_test("imap"))
        buttons.addWidget(self.test_imap_button)
        buttons.addStretch(1)
        outer.addLayout(buttons)

        self.account_summary = QLabel()
        self.account_summary.setWordWrap(True)
        self.account_summary.setProperty("dim", "true")
        outer.addWidget(self.account_summary)
        outer.addWidget(_separator())

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.preset_combo = QComboBox()
        for name, label in accounts.choices():
            self.preset_combo.addItem(label, name)
        self.preset_combo.currentIndexChanged.connect(self._preset_changed)
        form.addRow("Provider", self.preset_combo)

        self.email_edit = QLineEdit()
        self.email_edit.setPlaceholderText("you@example.com")
        self.email_edit.editingFinished.connect(self._address_entered)
        form.addRow("Email address", self.email_edit)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        reveal = QToolButton()
        reveal.setText("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda on: self.password_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
            )
        )
        self.get_password_button = QToolButton()
        self.get_password_button.setText("Get one…")
        self.get_password_button.clicked.connect(self._open_password_page)
        password_row = QHBoxLayout()
        password_row.addWidget(self.password_edit, 1)
        password_row.addWidget(reveal)
        password_row.addWidget(self.get_password_button)
        self.password_label = QLabel("App password")
        form.addRow(self.password_label, password_row)

        self.provider_note = QLabel()
        self.provider_note.setWordWrap(True)
        self.provider_note.setOpenExternalLinks(True)
        self.provider_note.setProperty("dim", "true")
        form.addRow("", self.provider_note)

        self.account_label_edit = QLineEdit()
        self.account_label_edit.setPlaceholderText("shown in the table and menus")
        form.addRow("Name", self.account_label_edit)

        self.advanced_box = QGroupBox("Server")
        self.advanced_box.setCheckable(True)
        self.advanced_box.setChecked(False)
        self.advanced_box.setToolTip(
            "Filled in from the provider. Only worth opening if your provider "
            "uses something unusual."
        )
        advanced = QFormLayout(self.advanced_box)
        self.host_edit = QLineEdit()
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.mailbox_edit = QLineEdit()
        self.mailbox_edit.setPlaceholderText("INBOX")
        self.connections_spin = QSpinBox()
        self.connections_spin.setRange(1, 8)
        advanced.addRow("IMAP host", self.host_edit)
        advanced.addRow("IMAP port", self.port_spin)
        advanced.addRow("Mailbox to scan", self.mailbox_edit)
        advanced.addRow("Parallel connections", self.connections_spin)
        form.addRow(self.advanced_box)

        self.fetch_kb_spin = QSpinBox()
        self.fetch_kb_spin.setRange(8, 4096)
        self.fetch_kb_spin.setSuffix(" KB")
        self.fetch_kb_spin.setToolTip(
            "How much of each message to download. Enough for the text; the "
            "rest is attachments, which are never read."
        )
        form.addRow("Download per message", self.fetch_kb_spin)
        outer.addLayout(form)

        # Every field writes into the selected mailbox as it changes, so the
        # list above is always showing the truth and there is nothing to press.
        for widget in (self.email_edit, self.account_label_edit, self.host_edit,
                       self.mailbox_edit):
            widget.textChanged.connect(self._capture_account)
        for widget in (self.port_spin, self.connections_spin):
            widget.valueChanged.connect(self._capture_account)
        self.password_edit.textChanged.connect(self._capture_account)

        note = QLabel(
            "Passwords go into the macOS Keychain, never into a file. Every "
            "provider here wants an app password rather than the one you sign "
            "in with, which is a good thing: it can be revoked on its own."
        )
        note.setWordWrap(True)
        note.setProperty("dim", "true")
        outer.addWidget(note)
        outer.addStretch(1)
        return page

    # -- the mailbox list -------------------------------------------------
    def _load_accounts(self, settings: Settings) -> None:
        """Take a working copy of the mailbox list, editable until OK."""
        self._accounts: List[Account] = [replace(a) for a in settings.accounts]
        if not self._accounts and settings.icloud_email:
            self._accounts = [replace(settings.primary_account)]
        self._account_index = 0 if self._accounts else -1
        self._refresh_account_list()

    def _account_status(self, account: Account) -> str:
        """What still needs doing to this mailbox, in a few words."""
        if not account.address:
            return "no address yet"
        if not accounts.valid_address(account.address):
            return "that address does not look right"
        if not account.host:
            return "no server - choose a provider, or type a host"
        if not self._password_for(account):
            return "no password yet"
        return "ready" if account.enabled else "ready, not scanned"

    def _password_for(self, account: Account) -> str:
        if account.address in self._account_passwords:
            return self._account_passwords[account.address]
        try:
            return self._store.get_mailbox_password(account.address)
        except CredentialError:
            return ""

    def _refresh_account_list(self) -> None:
        self.account_list.blockSignals(True)
        self.account_list.clear()
        for account in self._accounts:
            name = account.describe() if account.address else "New mailbox"
            item = QListWidgetItem(f"{name}   —   {self._account_status(account)}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if account.enabled else Qt.CheckState.Unchecked)
            item.setToolTip(
                f"{account.address or 'No address yet'}\n"
                f"{account.host or 'No server'}:{account.port}\n"
                f"{self._account_status(account)}"
            )
            self.account_list.addItem(item)
        if self._accounts:
            self.account_list.setCurrentRow(
                min(max(self._account_index, 0), len(self._accounts) - 1))
        self.account_list.blockSignals(False)

        ready = sum(1 for a in self._accounts if self._account_status(a).startswith("ready"))
        total = len(self._accounts)
        self.account_summary.setText(
            f"{total} mailbox{'es' if total != 1 else ''}, {ready} ready to scan."
            + ("" if ready == total else
               " Anything not ready is listed above with what it still needs.")
        )
        self.remove_account_button.setEnabled(bool(self._accounts))
        self.test_imap_button.setEnabled(bool(self._accounts))
        for widget in (self.preset_combo, self.email_edit, self.password_edit,
                       self.account_label_edit, self.advanced_box):
            widget.setEnabled(bool(self._accounts))
        if self._accounts:
            self._show_account(min(max(self._account_index, 0), len(self._accounts) - 1))

    def _account_ticked(self, item: QListWidgetItem) -> None:
        row = self.account_list.row(item)
        if 0 <= row < len(self._accounts):
            self._accounts[row].enabled = item.checkState() == Qt.CheckState.Checked
            self._refresh_account_list()

    def _show_account(self, index: int) -> None:
        if not (0 <= index < len(self._accounts)):
            return
        account = self._accounts[index]
        self._account_index = index
        editors = (self.preset_combo, self.email_edit, self.host_edit,
                   self.port_spin, self.mailbox_edit, self.connections_spin,
                   self.account_label_edit, self.password_edit)
        for widget in editors:
            widget.blockSignals(True)
        self.preset_combo.setCurrentIndex(
            max(0, self.preset_combo.findData(account.preset)))
        self.email_edit.setText(account.address)
        self.host_edit.setText(account.host)
        self.port_spin.setValue(account.port)
        self.mailbox_edit.setText(account.source_mailbox)
        self.connections_spin.setValue(account.connections)
        self.account_label_edit.setText(account.label)
        self.password_edit.setText(self._password_for(account))
        for widget in editors:
            widget.blockSignals(False)
        # Open the server box on its own when it holds something unexpected.
        spec = accounts.host_for(account.preset)
        self.advanced_box.setChecked(
            bool(account.host) and account.host != spec.host)
        self._describe_preset(account.preset)

    def _capture_account(self) -> None:
        """Fold the form back into the mailbox it belongs to.

        Called on every keystroke rather than only when the selection moves, so
        the list is always describing what has actually been entered.
        """
        if not (0 <= self._account_index < len(self._accounts)):
            return
        account = self._accounts[self._account_index]
        address = self.email_edit.text().strip()
        account.preset = self.preset_combo.currentData() or "custom"
        account.address = address
        account.host = self.host_edit.text().strip()
        account.port = self.port_spin.value()
        account.source_mailbox = self.mailbox_edit.text().strip() or "INBOX"
        account.connections = self.connections_spin.value()
        account.label = self.account_label_edit.text().strip()
        if address:
            # Cleaned on the way in as well as on the way out, so what is shown,
            # what is stored and what is sent are all the same thing.
            self._account_passwords[address] = clean_secret(self.password_edit.text())
        self._refresh_list_row(self._account_index)

    def _refresh_list_row(self, index: int) -> None:
        """Update one row in place, without rebuilding and stealing focus."""
        item = self.account_list.item(index)
        if item is None or not (0 <= index < len(self._accounts)):
            return
        account = self._accounts[index]
        name = account.describe() if account.address else "New mailbox"
        self.account_list.blockSignals(True)
        item.setText(f"{name}   —   {self._account_status(account)}")
        self.account_list.blockSignals(False)
        ready = sum(1 for a in self._accounts
                    if self._account_status(a).startswith("ready"))
        total = len(self._accounts)
        self.account_summary.setText(
            f"{total} mailbox{'es' if total != 1 else ''}, {ready} ready to scan."
        )

    def _account_selected(self, index: int) -> None:
        if index < 0 or index == self._account_index:
            return
        self._show_account(index)

    def _add_account(self) -> None:
        """Start a new mailbox, and put the cursor where typing should begin."""
        self._accounts.append(Account())
        self._account_index = len(self._accounts) - 1
        self._refresh_account_list()
        self.account_list.setCurrentRow(self._account_index)
        self.email_edit.setFocus()
        self.status.setText(
            "New mailbox: choose a provider and type its address. It is kept "
            "when you press OK."
        )

    def _remove_account(self) -> None:
        if not (0 <= self._account_index < len(self._accounts)):
            return
        going = self._accounts[self._account_index]
        if going.address and QMessageBox.question(
            self, "Remove mailbox",
            f"Stop scanning {going.address}?\n\nNothing in the mailbox is "
            "touched. Its password is removed from the Keychain.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return
        self._removed_accounts.append(going)
        del self._accounts[self._account_index]
        self._account_index = max(0, self._account_index - 1)
        self._refresh_account_list()

    def _preset_changed(self) -> None:
        """Follow the provider, the way the model page follows the backend.

        Changing the provider on a mailbox that already holds somebody else's
        address is not a change of server, it is a different mailbox. Keeping
        the old address is how an iCloud address ends up pointed at Gmail's
        server, which then quietly scans nothing.
        """
        name = self.preset_combo.currentData() or "custom"
        spec = accounts.host_for(name)
        if spec.host:
            self.host_edit.setText(spec.host)
            self.port_spin.setValue(spec.port)

        address = self.email_edit.text().strip()
        belongs_to = accounts.host_for_address(address) if address else None
        if address and belongs_to is not None and not belongs_to.is_custom \
                and belongs_to.name != name:
            self.email_edit.clear()
            self.password_edit.clear()
            self.account_label_edit.clear()
            self.status.setText(
                f"{_html(address)} is {belongs_to.label}, so it has been "
                f"cleared. Enter the {spec.label} address for this mailbox."
            )
            address = ""

        self.email_edit.setPlaceholderText(
            f"you@{spec.domains[0]}" if spec.domains else "you@example.com")
        placeholder = {label for _n, label in accounts.choices()}
        if self.account_label_edit.text().strip() in placeholder:
            self.account_label_edit.setText(address.split("@")[0] if address else "")
        self._describe_preset(name)
        self._capture_account()

    def _address_entered(self) -> None:
        """Fill in the server from the domain, unless it is already set."""
        address = self.email_edit.text().strip()
        if not address:
            return
        guessed = accounts.host_for_address(address)
        current = self.preset_combo.currentData() or "custom"
        if current == "custom" and not guessed.is_custom:
            self.preset_combo.setCurrentIndex(
                max(0, self.preset_combo.findData(guessed.name)))
        current_label = self.account_label_edit.text().strip()
        placeholder = {label for _name, label in accounts.choices()}
        if not current_label or current_label in placeholder:
            self.account_label_edit.setText(address.split("@")[0])
        self._capture_account()

    def _describe_preset(self, name: str) -> None:
        spec = accounts.host_for(name)
        self.password_label.setText(spec.secret_label)
        self.get_password_button.setVisible(bool(spec.help_url))
        self.get_password_button.setToolTip(
            f"Open {spec.help_url}" if spec.help_url else "")
        self.password_edit.setPlaceholderText(spec.secret_label.lower())
        parts = []
        if spec.note:
            parts.append(_html(spec.note))
        if spec.help_url:
            parts.append(f"<a href='{spec.help_url}'>{_html(spec.help_url)}</a>")
        self.provider_note.setText("<br>".join(parts))
        self.provider_note.setVisible(bool(parts))

    def _open_password_page(self) -> None:
        spec = accounts.host_for(self.preset_combo.currentData() or "custom")
        if spec.help_url:
            QDesktopServices.openUrl(QUrl(spec.help_url))

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
        self.provider_blurb.setProperty("dim", "true")

        # Shown only for the on-device backend, and only when it needs setting
        # up. The equivalent of "get one" beside an API key field.
        self.ollama_note = QLabel()
        self.ollama_note.setWordWrap(True)
        self.ollama_note.setOpenExternalLinks(True)
        self.ollama_note.setVisible(False)
        self.ollama_button = QPushButton("Install Ollama")
        self.ollama_button.setVisible(False)
        self.ollama_button.clicked.connect(self._do_ollama_step)

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
        _paint_button(self.test_model_button, "primary")
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

        # A probability on a two-decimal spinner reads as a number to be
        # nudged. It is really "how sure before this files itself", which is a
        # position on a range, so it is one - with the figure spelled out and
        # what it means underneath.
        self.threshold_slider = QSlider(Qt.Orientation.Horizontal)
        self.threshold_slider.setRange(50, 100)
        self.threshold_slider.setSingleStep(1)
        self.threshold_slider.setPageStep(5)
        self.threshold_slider.setTickPosition(QSlider.TickPosition.TicksBelow)
        self.threshold_slider.setTickInterval(10)
        self.threshold_slider.setMinimumWidth(200)
        self.threshold_slider.setToolTip(
            "Messages the sorter is less sure about than this are held for you "
            "to look at, and are never pre-ticked."
        )
        self.threshold_value = QLabel()
        self.threshold_value.setMinimumWidth(52)
        self.threshold_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.threshold_row = QWidget()
        threshold_layout = QHBoxLayout(self.threshold_row)
        threshold_layout.setContentsMargins(0, 0, 0, 0)
        threshold_layout.addWidget(self.threshold_slider, 1)
        threshold_layout.addWidget(self.threshold_value)
        self.threshold_note = QLabel()
        self.threshold_note.setWordWrap(True)
        self.threshold_note.setProperty("dim", "true")
        self.threshold_slider.valueChanged.connect(self._threshold_changed)

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
        form.addRow("", self.ollama_note)
        ollama_row = QHBoxLayout()
        ollama_row.addWidget(self.ollama_button)
        ollama_row.addStretch(1)
        form.addRow("", ollama_row)
        form.addRow("Model", self.model_combo)
        form.addRow("", self.model_note)
        form.addRow(self.key_label, self.key_row_widget)
        form.addRow(self.base_url_label, self.base_url_edit)
        form.addRow("", model_test_widget)
        form.addRow(_separator())
        form.addRow(self.effort_label, self.effort_combo)
        form.addRow("File it without asking", self.threshold_row)
        form.addRow("", self.threshold_note)
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

    # -- the on-device backend needs software, not a key -------------------
    def _refresh_ollama_panel(self, spec) -> None:
        """Say what is missing for the on-device backend, and offer to fix it."""
        if not hasattr(self, "ollama_note"):
            return
        if not getattr(spec, "on_device", False):
            self.ollama_note.setVisible(False)
            self.ollama_button.setVisible(False)
            return

        state = ondevice.status(self.base_url_edit.text().strip() or ondevice.DEFAULT_ENDPOINT)
        self._ollama_state = state
        self.ollama_note.setVisible(True)
        step = state.next_step()

        if not step:
            self.ollama_note.setText(
                f"{_html(state.describe())} Installed models: "
                f"{_html(', '.join(state.models[:6]))}"
            )
            self.ollama_button.setVisible(False)
            return

        if step == "install":
            command = ondevice.install_command()
            if command:
                self.ollama_note.setText(
                    "Ollama is not installed. It runs a model on this Mac, so "
                    "nothing leaves it and there is nothing to pay for. "
                    "Homebrew is available, so this can install it for you."
                )
                self.ollama_button.setText("Install Ollama")
            else:
                self.ollama_note.setText(
                    "Ollama is not installed. It runs a model on this Mac, so "
                    "nothing leaves it and there is nothing to pay for. "
                    f"Download it from <a href='{ondevice.DOWNLOAD_URL}'>"
                    f"{ondevice.DOWNLOAD_URL}</a>, then come back here."
                )
                self.ollama_button.setText("Open the download page")
        elif step == "start":
            self.ollama_note.setText(
                "Ollama is installed but its server is not answering on "
                f"{_html(self.base_url_edit.text().strip() or ondevice.DEFAULT_ENDPOINT)}."
            )
            self.ollama_button.setText("Start Ollama")
        else:
            model = self._chosen_model() or "llama3.2:3b"
            self.ollama_note.setText(
                f"Ollama is running but has no models. {_html(model)} needs to be "
                "downloaded once, which is a couple of gigabytes."
            )
            self.ollama_button.setText(f"Download {model}")
        self.ollama_button.setVisible(True)

    def _do_ollama_step(self) -> None:
        """Run whichever step the panel is currently offering."""
        state = getattr(self, "_ollama_state", None) or ondevice.status()
        step = state.next_step()
        if step == "install" and not ondevice.install_command():
            QDesktopServices.openUrl(QUrl(ondevice.DOWNLOAD_URL))
            return

        command = {
            "install": ondevice.install_command,
            "start": ondevice.start_command,
        }.get(step, lambda: ondevice.pull_command(self._chosen_model() or "llama3.2:3b"))()
        if not command:
            self.status.setText("Nothing to run for that step.")
            return

        if step == "start":
            # serve does not return, so it is launched rather than waited on.
            try:
                subprocess.Popen(command, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as exc:
                self.status.setText(f"Could not start Ollama: {exc}")
                return
            self.status.setText("Starting Ollama… give it a few seconds, then test.")
            QTimer.singleShot(4000, lambda: self._refresh_ollama_panel(
                providers.provider_class(self.provider_combo.currentData() or "ollama")))
            return

        self.ollama_button.setEnabled(False)
        self.status.setText(f"Running: {' '.join(command)} — this can take a while.")
        QApplication.setOverrideCursor(Qt.CursorShape.BusyCursor)
        try:
            ok, output = ondevice.run(command)
        finally:
            QApplication.restoreOverrideCursor()
            self.ollama_button.setEnabled(True)
        tail = output.strip().splitlines()[-1] if output.strip() else ""
        self.status.setText(("Done. " if ok else "That did not work: ") + tail[:160])
        self._refresh_ollama_panel(
            providers.provider_class(self.provider_combo.currentData() or "ollama"))

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

        self._refresh_ollama_panel(spec)

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

    def _build_reply_tab(self) -> QWidget:
        """Rules that act on matching mail. Nothing here ever sends anything."""
        page = QWidget()
        outer = QVBoxLayout(page)

        headline = QLabel(
            "<b>Replies are drafted, never sent.</b> A rule can write a reply "
            "into your Drafts mailbox, file a message, tick it, flag it or mark "
            "it read — but nothing leaves your account without you pressing send "
            "in your mail app."
        )
        headline.setWordWrap(True)
        outer.addWidget(headline)

        top = QHBoxLayout()
        self.auto_reply_check = QCheckBox("Run these rules after a scan")
        top.addWidget(self.auto_reply_check)
        top.addStretch(1)
        top.addWidget(QLabel("Sign as"))
        self.signature_edit = AdaptiveLineEdit(
            "the name to sign off with", "your name", "name")
        self.signature_edit.setMinimumWidth(120)
        self.signature_edit.setMaximumWidth(200)
        top.addWidget(self.signature_edit)
        outer.addLayout(top)
        outer.addWidget(_separator())

        body = QHBoxLayout()
        body.setSpacing(10)
        outer.addLayout(body, 1)

        # -- left: the rules, in the order they run ------------------------
        left = QVBoxLayout()
        left.setSpacing(4)
        order_note = QLabel("Rules run top to bottom.")
        order_note.setProperty("dim", "true")
        order_note.setWordWrap(True)
        left.addWidget(order_note)

        self.rule_list = WrappingList()
        self.rule_list.setMinimumWidth(150)
        self.rule_list.setMaximumWidth(230)
        self.rule_list.setSizePolicy(QSizePolicy.Policy.Preferred,
                                     QSizePolicy.Policy.Expanding)
        # Wrap rather than elide. A rule named for what it does is longer than
        # this column, and half a name is no name at all.
        self.rule_list.currentRowChanged.connect(self._rule_selected)
        self.rule_list.itemChanged.connect(self._rule_ticked)
        left.addWidget(self.rule_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        for text, tip, slot in (
            ("＋", "Add a rule", self._add_rule),
            ("⧉", "Duplicate this rule", self._duplicate_rule),
            ("−", "Remove this rule", self._remove_rule),
            ("↑", "Run this rule earlier", lambda: self._move_rule(-1)),
            ("↓", "Run this rule later", lambda: self._move_rule(1)),
        ):
            button = _compact_button(text, tip, slot)
            buttons.addWidget(button)
            if text == "−":
                self.remove_rule_button = button
        buttons.addStretch(1)
        left.addLayout(buttons)
        body.addLayout(left)

        # -- right: the rule itself ----------------------------------------
        right = QVBoxLayout()
        right.setSpacing(6)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name"))
        self.rule_name_edit = AdaptiveLineEdit("what this rule is for", "name")
        self.rule_name_edit.setMinimumWidth(120)
        self.rule_name_edit.setSizePolicy(QSizePolicy.Policy.Ignored,
                                          QSizePolicy.Policy.Fixed)
        self.rule_name_edit.editingFinished.connect(self._rule_renamed)
        name_row.addWidget(self.rule_name_edit, 1)
        self.rule_enabled = QCheckBox("On")
        self.rule_enabled.toggled.connect(self._rule_enabled_toggled)
        name_row.addWidget(self.rule_enabled)
        right.addLayout(name_row)

        match_row = QHBoxLayout()
        match_row.addWidget(QLabel("Match"))
        self.rule_match = QComboBox()
        self.rule_match.addItem("all of these conditions", "all")
        self.rule_match.addItem("any of these conditions", "any")
        self.rule_match.currentIndexChanged.connect(self._rule_edited)
        match_row.addWidget(self.rule_match)
        match_row.addStretch(1)
        add_condition = QToolButton()
        add_condition.setText("Add a condition")
        add_condition.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        add_condition.clicked.connect(self._add_condition)
        match_row.addWidget(add_condition)
        right.addLayout(match_row)

        self.conditions_box = QWidget()
        self.conditions_layout = QVBoxLayout(self.conditions_box)
        self.conditions_layout.setContentsMargins(0, 0, 0, 0)
        self.conditions_layout.setSpacing(4)
        right.addWidget(self.conditions_box)

        right.addWidget(_separator())

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("<b>Then</b>"))
        action_row.addStretch(1)
        add_action = QToolButton()
        add_action.setText("Add an action")
        add_action.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        add_action.clicked.connect(self._add_action)
        action_row.addWidget(add_action)
        right.addLayout(action_row)

        self.actions_box = QWidget()
        self.actions_layout = QVBoxLayout(self.actions_box)
        self.actions_layout.setContentsMargins(0, 0, 0, 0)
        self.actions_layout.setSpacing(4)
        right.addWidget(self.actions_box)

        self.template_note = QLabel(
            "In a template, {first_name}, {sender}, {subject} and {me} are "
            "filled in. Anything in [square brackets] is left for you to "
            "complete and is listed at the bottom of the draft."
        )
        self.template_note.setWordWrap(True)
        self.template_note.setProperty("dim", "true")
        right.addWidget(self.template_note)

        switches = QHBoxLayout()
        self.rule_skip_bulk = QCheckBox("Skip bulk mail")
        self.rule_skip_bulk.setToolTip(
            "Anything carrying an unsubscribe header. Leave this on for rules "
            "that reply: writing back to a mailing list is at best useless and "
            "at worst embarrassing."
        )
        self.rule_skip_bulk.toggled.connect(self._rule_edited)
        switches.addWidget(self.rule_skip_bulk)
        self.rule_stop_after = QCheckBox("Stop here when this matches")
        self.rule_stop_after.setToolTip(
            "Later rules are skipped for that message. Useful for an exception "
            "you put at the top of the list."
        )
        self.rule_stop_after.toggled.connect(self._rule_edited)
        switches.addWidget(self.rule_stop_after)
        switches.addStretch(1)
        right.addLayout(switches)

        self.rule_summary = QLabel("")
        self.rule_summary.setWordWrap(True)
        right.addWidget(self.rule_summary)

        try_row = QHBoxLayout()
        self.try_rule_button = QToolButton()
        self.try_rule_button.setText("Try it on the last scan")
        self.try_rule_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.try_rule_button.clicked.connect(self._try_rule)
        try_row.addWidget(self.try_rule_button)
        self.try_rule_result = QLabel("")
        self.try_rule_result.setWordWrap(True)
        try_row.addWidget(self.try_rule_result, 1)
        right.addLayout(try_row)

        right.addStretch(1)
        body.addLayout(right, 1)
        return page

    # -- reply rules ------------------------------------------------------
    def _load_rules(self, settings: Settings) -> None:
        self._rules = list(settings.rules)
        self._rule_index = 0
        self._condition_rows: List[ConditionRow] = []
        self._action_rows: List[ActionRow] = []
        self.auto_reply_check.setChecked(settings.auto_reply)
        self.signature_edit.setText(settings.reply_signature)
        self._refresh_rule_list()

    def _refresh_rule_list(self) -> None:
        """Redraw the list on the left without disturbing what is being edited."""
        self.rule_list.blockSignals(True)
        self.rule_list.clear()
        for rule in self._rules:
            entry = QListWidgetItem(self._rule_label(rule))
            entry.setFlags(entry.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            entry.setCheckState(Qt.CheckState.Checked if rule.enabled
                                else Qt.CheckState.Unchecked)
            entry.setToolTip(self._rule_tooltip(rule))
            self.rule_list.addItem(entry)
        self._rule_index = max(0, min(self._rule_index, len(self._rules) - 1))
        self.rule_list.setCurrentRow(self._rule_index)
        self.rule_list.blockSignals(False)
        self.rule_list.measure()
        self.remove_rule_button.setEnabled(len(self._rules) > 1)
        self._show_rule(self._rule_index)

    def _show_rule(self, index: int) -> None:
        if not (0 <= index < len(self._rules)):
            return
        rule = self._rules[index]
        self._rule_index = index
        self._loading_rule = True
        self.rule_enabled.setChecked(rule.enabled)
        self.rule_name_edit.setText(rule.name)
        self.rule_match.setCurrentIndex(max(0, self.rule_match.findData(rule.match)))
        self.rule_skip_bulk.setChecked(rule.skip_bulk)
        self.rule_stop_after.setChecked(rule.stop_after)
        self._rebuild_condition_rows(rule)
        self._rebuild_action_rows(rule)
        self._loading_rule = False
        self.try_rule_result.setText("")
        self._describe_rule()

    def _clear_rows(self, layout, rows: List) -> None:
        for row in rows:
            layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        rows.clear()

    def _rebuild_condition_rows(self, rule) -> None:
        self._clear_rows(self.conditions_layout, self._condition_rows)
        for condition in rule.conditions:
            self._add_condition_row(condition)
        if not rule.conditions:
            self._empty_note(self.conditions_layout,
                             "No conditions yet, so this rule never runs.")

    def _rebuild_action_rows(self, rule) -> None:
        self._clear_rows(self.actions_layout, self._action_rows)
        for action in rule.actions:
            self._add_action_row(action)
        if not rule.actions:
            self._empty_note(self.actions_layout, "No actions yet.")

    def _empty_note(self, layout, text: str) -> None:
        note = QLabel(text)
        note.setProperty("dim", "true")
        layout.addWidget(note)

    def _add_condition_row(self, condition) -> None:
        self._drop_notes(self.conditions_layout)
        row = ConditionRow(condition, mailboxes=self._accounts)
        row.changed.connect(self._rule_edited)
        row.removed.connect(self._remove_condition_row)
        self.conditions_layout.addWidget(row)
        self._condition_rows.append(row)

    def _add_action_row(self, action) -> None:
        self._drop_notes(self.actions_layout)
        row = ActionRow(action, folders=self._folder_choices())
        row.changed.connect(self._rule_edited)
        row.removed.connect(self._remove_action_row)
        self.actions_layout.addWidget(row)
        self._action_rows.append(row)

    def _drop_notes(self, layout) -> None:
        """Take away the “nothing here yet” line once there is something."""
        for index in reversed(range(layout.count())):
            widget = layout.itemAt(index).widget()
            if isinstance(widget, QLabel):
                layout.removeWidget(widget)
                widget.deleteLater()

    def _folder_choices(self) -> List[str]:
        """Folders a rule can file into: whatever this configuration creates.

        Editable, so a folder that is not in this list is still allowed - the
        list is a shortcut, not a fence.
        """
        root = (self.root_edit.text().strip() if hasattr(self, "root_edit")
                else "") or self._settings.folder_root
        other = (self.other_root_edit.text().strip()
                 if hasattr(self, "other_root_edit")
                 else "") or self._settings.other_folder_root
        plan = FolderPlan(root=root, other_root=other)
        choices = list(plan.all_folders)
        choices += [plan.for_other_category(topic) for topic in OtherCategory
                    if topic is not OtherCategory.NOT_APPLICABLE]
        seen: List[str] = []
        for folder in choices:
            if folder and folder not in seen:
                seen.append(folder)
        return seen

    def _remove_condition_row(self, row) -> None:
        if row in self._condition_rows:
            self._condition_rows.remove(row)
            self.conditions_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rule_edited()
        if not self._condition_rows:
            self._empty_note(self.conditions_layout,
                             "No conditions yet, so this rule never runs.")

    def _remove_action_row(self, row) -> None:
        if row in self._action_rows:
            self._action_rows.remove(row)
            self.actions_layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rule_edited()
        if not self._action_rows:
            self._empty_note(self.actions_layout, "No actions yet.")

    def _add_condition(self) -> None:
        self._capture_rule()
        self._add_condition_row(autoreply.Condition())
        self._rule_edited()

    def _add_action(self) -> None:
        self._capture_rule()
        self._add_action_row(autoreply.Action())
        self._rule_edited()

    def _capture_rule(self) -> None:
        """Read the editor back into the rule it is showing."""
        if getattr(self, "_loading_rule", False):
            return
        if not (0 <= self._rule_index < len(self._rules)):
            return
        rule = self._rules[self._rule_index]
        rule.enabled = self.rule_enabled.isChecked()
        rule.name = self.rule_name_edit.text().strip() or "New rule"
        rule.match = self.rule_match.currentData() or "all"
        rule.skip_bulk = self.rule_skip_bulk.isChecked()
        rule.stop_after = self.rule_stop_after.isChecked()
        rule.conditions = [row.value() for row in self._condition_rows]
        rule.actions = [row.value() for row in self._action_rows]

    def _rule_edited(self, *_args) -> None:
        if getattr(self, "_loading_rule", False):
            return
        self._capture_rule()
        self._describe_rule()
        self._refresh_current_list_item()

    def _describe_rule(self) -> None:
        """Say in one place what this rule does, and what is wrong with it."""
        if not (0 <= self._rule_index < len(self._rules)):
            return
        rule = self._rules[self._rule_index]
        drafts = rule.drafts_a_reply
        self.template_note.setVisible(drafts)
        problems = rule.problems()
        if problems:
            self.rule_summary.setText(
                "<b>Not ready:</b> " + " ".join(problems)
                + (" A rule with something missing never runs."
                   if rule.enabled else "")
            )
            self.rule_summary.setProperty("tone", "warn")
        else:
            self.rule_summary.setText(rule.describe())
            self.rule_summary.setProperty("tone", "")
        self.rule_summary.style().unpolish(self.rule_summary)
        self.rule_summary.style().polish(self.rule_summary)

    def _refresh_current_list_item(self) -> None:
        entry = self.rule_list.item(self._rule_index)
        if entry is None:
            return
        rule = self._rules[self._rule_index]
        self.rule_list.blockSignals(True)
        entry.setText(self._rule_label(rule))
        self.rule_list.measure()
        entry.setCheckState(Qt.CheckState.Checked if rule.enabled
                            else Qt.CheckState.Unchecked)
        entry.setToolTip(self._rule_tooltip(rule))
        self.rule_list.blockSignals(False)

    def _rule_label(self, rule) -> str:
        """The name, marked when the rule is not finished enough to run."""
        return ("⚠ " if rule.problems() else "") + menu_text(rule.name)

    def _rule_tooltip(self, rule) -> str:
        """The whole name, which the list is too narrow to show, and the gist."""
        problems = rule.problems()
        if problems:
            return f"{rule.name}\n\nNot ready:\n" + "\n".join(
                f"• {problem}" for problem in problems)
        return f"{rule.name}\n\n{rule.describe()}"

    def _rule_renamed(self) -> None:
        self._rule_edited()

    def _rule_enabled_toggled(self, on: bool) -> None:
        self._rule_edited()

    def _rule_ticked(self, entry) -> None:
        """The checkbox in the list, which is the fastest way to turn one off."""
        index = self.rule_list.row(entry)
        if not (0 <= index < len(self._rules)):
            return
        self._rules[index].enabled = entry.checkState() == Qt.CheckState.Checked
        if index == self._rule_index:
            self._loading_rule = True
            self.rule_enabled.setChecked(self._rules[index].enabled)
            self._loading_rule = False
            self._describe_rule()

    def _rule_selected(self, index: int) -> None:
        if index == self._rule_index:
            return
        self._capture_rule()
        self._show_rule(index)

    def _add_rule(self) -> None:
        self._capture_rule()
        self._rules.append(autoreply.Rule(
            conditions=[autoreply.Condition()], actions=[autoreply.Action()]))
        self._rule_index = len(self._rules) - 1
        self._refresh_rule_list()

    def _duplicate_rule(self) -> None:
        self._capture_rule()
        if not (0 <= self._rule_index < len(self._rules)):
            return
        copied = autoreply.Rule.from_dict(self._rules[self._rule_index].to_dict())
        copied.name = f"{copied.name} (copy)"
        copied.enabled = False
        self._rules.insert(self._rule_index + 1, copied)
        self._rule_index += 1
        self._refresh_rule_list()

    def _remove_rule(self) -> None:
        if len(self._rules) <= 1:
            return
        self._loading_rule = True
        del self._rules[self._rule_index]
        self._rule_index = max(0, self._rule_index - 1)
        self._loading_rule = False
        self._refresh_rule_list()

    def _move_rule(self, step: int) -> None:
        self._capture_rule()
        target = self._rule_index + step
        if not (0 <= target < len(self._rules)):
            return
        rules = self._rules
        rules[self._rule_index], rules[target] = rules[target], rules[self._rule_index]
        self._rule_index = target
        self._refresh_rule_list()

    def _try_rule(self) -> None:
        """Run every switched-on rule over the messages already on screen.

        Reading a rule and knowing what it will do are different things. This
        answers the second question against real mail, without touching the
        mailbox or the model - a rule that would ask the model reports that it
        matched, and nothing is drafted.
        """
        self._capture_rule()
        samples = list(self._sample_items)
        if not samples:
            self.try_rule_result.setText(
                "Nothing to try it on yet. Run a scan, then come back.")
            return
        rules = [r for r in self._rules if r.enabled and r.ready]
        if not rules:
            self.try_rule_result.setText(
                "No rule is both switched on and finished.")
            return
        hits = []
        for item in samples:
            outcome = autoreply.apply_rules(rules, item.email, item.classification)
            if outcome is not None:
                hits.append((item, outcome))
        if not hits:
            self.try_rule_result.setText(
                f"No match in the {len(samples)} message"
                f"{'' if len(samples) == 1 else 's'} on screen.")
            return
        lines = [f"<b>{len(hits)} of {len(samples)} matched.</b>"]
        for item, outcome in hits[:4]:
            lines.append(
                f"• {menu_text(item.email.subject_display[:52])} — "
                f"{outcome.describe()} ({menu_text(outcome.rule_name)})")
        if len(hits) > 4:
            lines.append(f"…and {len(hits) - 4} more.")
        self.try_rule_result.setText("<br>".join(lines))

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

        self.density_combo = QComboBox()
        for value, label, _blurb in theme.DENSITIES:
            self.density_combo.addItem(label, value)
        form.addRow("Spacing", self.density_combo)
        self.density_note = QLabel()
        self.density_note.setWordWrap(True)
        self.density_note.setProperty("dim", "true")
        form.addRow("", self.density_note)
        self.density_combo.currentIndexChanged.connect(self._density_changed)
        form.addRow(_separator())

        self.help_check = QCheckBox("Explain things on hover")
        self.help_check.setToolTip(
            "The same switch as the ? in the corner of the window. With it on, "
            "resting the pointer on anything explains what it does."
        )
        form.addRow("", self.help_check)
        help_note = QLabel(
            "Off by default, because a tooltip nobody asked for is in the way. "
            "The circled ? at the top right of the window toggles the same "
            "setting, and is filled in while it is on."
        )
        help_note.setWordWrap(True)
        help_note.setProperty("dim", "true")
        form.addRow("", help_note)
        form.addRow(_separator())

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
        self.rows_spin.valueChanged.connect(self._rows_chosen_by_hand)
        self.rows_spin.setToolTip(
            "How many lines of a summary or subject to show before it is cut "
            "off. Taller rows show more and fit fewer."
        )
        form.addRow("Row height", self.rows_spin)

        form.addRow(_separator())
        transfer = QHBoxLayout()
        export_button = QPushButton("Export settings…")
        export_button.setToolTip(
            "Write every setting to a text file you can read, keep, or move to "
            "another Mac. No passwords or keys are in it."
        )
        export_button.clicked.connect(self._export_settings)
        import_button = QPushButton("Import settings…")
        import_button.setToolTip("Read a settings file exported from this app")
        import_button.clicked.connect(self._import_settings)
        transfer.addWidget(export_button)
        transfer.addWidget(import_button)
        transfer.addStretch(1)
        form.addRow("Settings file", transfer)
        transfer_note = QLabel(
            "Plain JSON with a comment header. Passwords and API keys are not "
            "in it - they stay in the macOS Keychain and are entered again on "
            "the other Mac."
        )
        transfer_note.setWordWrap(True)
        transfer_note.setProperty("dim", "true")
        form.addRow("", transfer_note)

        # Applied as they are changed: a colour choice you cannot see until you
        # press OK is a colour choice made blind.
        for widget in (self.mode_combo, self.contrast_combo):
            widget.currentIndexChanged.connect(self._preview_appearance)
        self.readable_check.toggled.connect(self._preview_appearance)
        return page

    def _rows_chosen_by_hand(self) -> None:
        """Touching the spinner means this is now a deliberate choice."""
        self._row_lines_touched = True

    def _export_settings(self) -> None:
        """Write the current settings, including anything not yet saved."""
        default = str(Path.home() / "Downloads" / "Mail Manager settings.txt")
        path, _chosen = QFileDialog.getSaveFileName(
            self, "Export settings", default, "Text files (*.txt *.json);;All files (*)")
        if not path:
            return
        try:
            Path(path).write_text(self.collect().export_text(), encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Export failed", f"Could not write that file:\n{exc}")
            return
        self.status.setText(f"Exported to {Path(path).name}")

    def _import_settings(self) -> None:
        """Read a settings file and load it into the open dialog.

        Loaded into the form rather than applied straight away, so it can be
        looked at, adjusted and cancelled like any other change.
        """
        path, _chosen = QFileDialog.getOpenFileName(
            self, "Import settings", str(Path.home() / "Downloads"),
            "Text files (*.txt *.json);;All files (*)")
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            QMessageBox.warning(self, "Import failed", f"Could not read that file:\n{exc}")
            return
        try:
            incoming = Settings.import_text(text)
        except ValueError as exc:
            QMessageBox.warning(self, "That file cannot be used", str(exc))
            return

        # The window's own layout belongs to this Mac, not to the file.
        for field in Settings.PRIVATE_FIELDS:
            setattr(incoming, field, getattr(self._settings, field))
        self._settings = incoming
        self._load_values()
        self._preview_appearance()
        count = len(incoming.accounts)
        self.status.setText(
            f"Loaded {Path(path).name}: {count} mailbox"
            f"{'es' if count != 1 else ''}. Passwords still need entering. "
            "Press OK to keep it."
        )

    def _density_changed(self) -> None:
        """Say what the choice does, and show it straight away."""
        name = self.density_combo.currentData() or "comfortable"
        blurb = next((b for n, _l, b in theme.DENSITIES if n == name), "")
        self.density_note.setText(blurb)
        self._preview_appearance()

    def _toggle_help(self, on: bool) -> None:
        """Mirror the window's switch, and keep the checkbox in step."""
        helpmode.install(QApplication.instance(), on)
        if hasattr(self, "help_check") and self.help_check.isChecked() != on:
            self.help_check.blockSignals(True)
            self.help_check.setChecked(on)
            self.help_check.blockSignals(False)
        self.status.setText(
            "Help is on. Rest the pointer on anything to see what it does."
            if on else "Help is off."
        )

    def _threshold_changed(self) -> None:
        """Say what the number means, since a percentage on its own does not."""
        percent = self.threshold_slider.value()
        self.threshold_value.setText(f"{percent}%")
        if percent >= 97:
            describes = ("Only the clearest cases file themselves. Almost "
                         "everything waits for you.")
        elif percent >= 92:
            describes = ("The recommended setting. Confident readings file "
                         "themselves; anything arguable waits for you.")
        elif percent >= 80:
            describes = ("More gets filed without asking, and more of it will "
                         "be wrong. Worth pairing with a model backend.")
        else:
            describes = ("Almost everything files itself, including readings "
                         "the sorter is barely sure of. Rarely what you want.")
        self.threshold_note.setText(describes)

    def _preview_appearance(self) -> None:
        app = QApplication.instance()
        if app is None:
            return
        theme.apply(app,
                    self.mode_combo.currentData() or "system",
                    self.contrast_combo.currentData() or "normal",
                    self.readable_check.isChecked(),
                    self.density_combo.currentData() or "comfortable")
        window = self.parent()
        if hasattr(window, "_apply_spacing"):
            # Show the spacing on the window behind the dialog, not just here.
            was = window.settings.density
            window.settings.density = self.density_combo.currentData() or "comfortable"
            window._apply_spacing()
            window.settings.density = was

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
        self._load_accounts(settings)
        self._load_rules(settings)
        self.fetch_kb_spin.setValue(max(8, settings.fetch_bytes // 1024))

        provider_index = self.provider_combo.findData(settings.provider)
        self.provider_combo.setCurrentIndex(max(0, provider_index))
        self.base_url_edit.setText(settings.base_url)
        self._provider_changed()
        self.effort_combo.setCurrentText(settings.effort)
        self.threshold_slider.setValue(int(round(settings.confidence_threshold * 100)))
        self._threshold_changed()
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
        self.help_check.setChecked(settings.help_mode)
        self.help_check.toggled.connect(
            lambda on: self.help_button.setChecked(on))
        self.density_combo.setCurrentIndex(
            max(0, self.density_combo.findData(settings.density)))
        self._density_changed()
        self.rows_spin.setValue(settings.effective_row_lines)

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
        self._capture_account()
        self._capture_rule()
        kept = [a for a in self._accounts if a.address]
        data.update(
            mailboxes=kept,
            icloud_email=kept[0].address if kept else "",
            fetch_bytes=self.fetch_kb_spin.value() * 1024,
            provider=self.provider_combo.currentData() or providers.DEFAULT_PROVIDER,
            model=self._chosen_model(),
            base_url=self.base_url_edit.text().strip(),
            effort=self.effort_combo.currentText(),
            appearance_mode=self.mode_combo.currentData() or "system",
            contrast=self.contrast_combo.currentData() or "normal",
            readable=self.readable_check.isChecked(),
            density=self.density_combo.currentData() or "comfortable",
            help_mode=self.help_check.isChecked(),
            row_lines=self.rows_spin.value(),
            row_lines_auto=self._settings.row_lines_auto and not self._row_lines_touched,
            auto_reply=self.auto_reply_check.isChecked(),
            reply_signature=self.signature_edit.text().strip(),
            reply_rules=[r.to_dict() for r in self._rules],
            confidence_threshold=self.threshold_slider.value() / 100.0,
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
        self._capture_account()
        problems = []
        for address, secret in self._account_passwords.items():
            if not address:
                continue
            try:
                self._store.set_mailbox_password(address, secret)
            except CredentialError as exc:
                problems.append(f"{address}: {exc}")
        # A mailbox that was removed should not leave its password behind.
        for account in self._removed_accounts:
            if account.address and not any(
                    a.address == account.address for a in self._accounts):
                try:
                    self._store.set_mailbox_password(account.address, "")
                except CredentialError:
                    pass
        self._removed_accounts = []
        if settings.needs_api_key:
            try:
                self._store.set_provider_key(settings.provider, self.api_key_edit.text())
            except CredentialError as exc:
                problems.append(str(exc))
        if problems:
            raise CredentialError("; ".join(problems))

    # -- tests -----------------------------------------------------------
    def _run_test(self, mode: str) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        settings = self.collect()
        if mode == "imap" and not settings.icloud_email:
            self.status.setText("Enter the mailbox email address first.")
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
        self.reply_worker = None
        self.undo_worker = None
        #: The last batch of moves, so they can be reversed.
        self._last_apply: List[MovePlan] = []
        #: Every background thread this window has started and not yet reaped.
        self._workers: List[QThread] = []
        self.folder_plan: Optional[FolderPlan] = settings.folder_plan()
        #: Set only by an explicit Quit, so closeEvent can tell "put this
        #: away" apart from "stop the app".
        self._quitting = False
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
        self._restore_hidden_columns()
        self._rebuild_columns_menu()
        self._rebuild_view_menu()

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
            # elides to "firstname.lastname@ic…" has dropped the one part that says
            # which mailbox it is.
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
            button.setText(window.label)
            button.setCheckable(True)
            button.setAutoExclusive(True)
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
        self.model_menu = QMenu(self)
        self.model_button.setMenu(self.model_menu)
        row.addWidget(self.model_button)

        # Only worth the space once there is more than one mailbox.
        self.account_button = QToolButton()
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

        # Reading one mailbox at a time is a different question from scanning
        # one at a time, so it gets its own control rather than reusing the
        # scan picker.
        self.view_button = QToolButton()
        self.view_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.view_menu = QMenu(self)
        self.view_button.setMenu(self.view_menu)
        row.addWidget(self.view_button)

        self.columns_button = QToolButton()
        self.columns_button.setText("Columns")
        self.columns_button.setToolTip("Show or hide columns in the table")
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
        reply_action.setShortcut(QKeySequence("Ctrl+R"))
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
        if not self.confirm_quit():
            return
        self._quitting = True
        if self.isFullScreen():
            self.setWindowState(
                self.windowState() & ~Qt.WindowState.WindowFullScreen)
            self.showNormal()
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
        dialog = SettingsDialog(self.settings, self.store, self,
                                sample_items=self.model.items)
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
            if widget is None or widget.isHidden() or widget is self.window_label:
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

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._fit_window_label()

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
    @Slot()
    def undo_last_apply(self) -> None:
        """Move the last batch back where it came from."""
        if self._busy() or not self._last_apply:
            return
        count = len(self._last_apply)
        if QMessageBox.question(
            self, "Undo filing",
            f"Move {count} message{'s' if count != 1 else ''} back to the "
            "mailbox they came from?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Yes,
        ) != QMessageBox.StandardButton.Yes:
            return
        try:
            passwords = self._mailbox_passwords()
        except CredentialError as exc:
            QMessageBox.critical(self, "Keychain", str(exc))
            return

        self._set_busy(True, "Putting messages back…")
        self.undo_worker = ApplyWorker(
            settings=self.settings, mailbox_password=passwords,
            plans=self._last_apply, extra_folders=(), parent=self,
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
        self._last_apply = []
        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(False)
            self.undo_action.setText("Undo Last Filing")
        message = f"Put {report.moved_count} message(s) back."
        if report.failed:
            message += f" {report.failed_count} could not be moved back."
        self._append_log(message)
        QMessageBox.information(self, "Undo filing", message)
        self._update_status(message)

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
                         "moved yet — press Apply when you are happy.")
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
        self._last_apply = []
        for item in self.model.items:
            filed_to = report.moved.get(item.email.uid)
            if not filed_to:
                continue
            account = self.settings.account_by_id(item.email.account_id)
            home = account.source_mailbox if account else self.settings.source_mailbox
            self._last_apply.append(MovePlan(
                uid=item.email.uid,
                target_folder=home,
                subject=item.email.subject_display,
                account_id=item.email.account_id,
                source_folder=filed_to,
            ))
        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(bool(self._last_apply))
            self.undo_action.setText(
                f"Undo Filing of {len(self._last_apply)} Message"
                f"{'s' if len(self._last_apply) != 1 else ''}"
                if self._last_apply else "Undo Last Filing"
            )
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


def _paint_button(button, role: str, bold: bool = True) -> None:
    """Mark a button's role, and let the theme decide what that looks like.

    Roles rather than colours, and a property rather than a stylesheet on the
    widget. A per-widget stylesheet carries its own padding and radius, which
    is why the coloured buttons used to be a different size from the plain ones
    beside them; and it cannot follow a change of theme, because it does not
    know one happened.

        primary      the main action, and only ever one of them
        confirm      it will change your mailbox, and you meant it to
        danger       it stops or undoes something, right now
        destructive  it removes something, and is outlined rather than filled
    """
    button.setProperty("role", role)
    if bold and role in ("primary", "confirm", "danger"):
        font = button.font()
        font.setWeight(QFont.Weight.DemiBold)
        button.setFont(font)
    # A property that changes after the style was applied needs saying so.
    button.style().unpolish(button)
    button.style().polish(button)


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
