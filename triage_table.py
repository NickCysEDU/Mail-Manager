"""The middle of the window: the table of messages, and the preview beside it.

Split out of gui.py. The model holds the triage items and answers Qt's
questions about them; the proxy filters and sorts; the three delegates paint
the confidence bar, the wrapped subject, and the category chip; the preview
pane shows whichever row is selected.

A pure move: every class is exactly as it was in gui.py, and gui re-exports
them so nothing that imports from there has to change.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSize,
                            QSortFilterProxyModel, Qt, Signal, Slot)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import (QApplication, QComboBox, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTextBrowser, QToolButton, QVBoxLayout, QWidget)

import conversations
from imap_engine import MoveReport
from models import (CATEGORY_COLORS, OTHER_COLOR, TOPIC_COLORS, Category,
                    Disposition, TriageItem, TriageSummary)
from widgets import (ACCENT_BLUE, ACCENT_RED, _attr_url, _confidence_rgb,
                     _draw_wrapped, _html, _is_dark, _mono_font, _one_line,
                     _tint, _wrap, system_font)


#: What the folder box shows when a message is to stay where it is.
LEAVE_IN_PLACE = "- leave in place -"

def category_color(classification, item=None) -> str:
    """The accent colour for a row.

    A job category always has one. An everyday topic gets one only when the
    current settings actually file that topic somewhere; otherwise it stays
    grey, which is the honest signal that the app is not going to act on it.

    ``item`` carries the routing and the topic list. Without it - which is how
    the older callers ask - every non-job topic is grey, as before.
    """
    if classification.is_job_related:
        return CATEGORY_COLORS.get(classification.category, OTHER_COLOR)
    if item is not None and item.topic_is_sorted:
        return TOPIC_COLORS.get(classification.other_category, OTHER_COLOR)
    return OTHER_COLOR


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
            return category_color(classification, item)

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
                return _tint(category_color(classification, item), 26)
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
                    return QColor(category_color(classification, item))
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
    def conversation_of(self, row: int) -> List[int]:
        """Every row in the same conversation as this one, including it."""
        item = self.item_at(row)
        if item is None or not item.thread_key:
            return [row] if item is not None else []
        return [index for index, other in enumerate(self._items)
                if other.thread_key == item.thread_key]

    def set_approved(self, rows: Sequence[int], approved: bool) -> int:
        """Tick or untick a set of rows. Returns how many actually changed.

        Rows that cannot be actioned - nothing to move them to, or already
        moved - are skipped rather than refused, so a selection that mixes
        the two does the sensible thing with the half that can.
        """
        changed = 0
        for row in rows:
            item = self.item_at(row)
            if item is None or not item.is_actionable:
                continue
            if item.approved != approved:
                item.approved = approved
                changed += 1
        if changed:
            self._refresh_column(self.COL_SELECT)
            self.selectionChanged.emit()
        return changed

    def set_all_approved(self, approved: bool, only_high_confidence: bool = False) -> None:
        """Tick or untick everything, optionally only what the sorter is sure of.

        "Sure of" means the confidence bar, not ``default_approved``. Those
        are two different questions and conflating them made the button lie:
        non-job mail is deliberately never *pre*-ticked, because misfiling a
        bank alert is worse than leaving it alone - but somebody pressing a
        button labelled "tick every message the analysis was confident about"
        has asked for it, and a 99%-confident receipt that is on its way to a
        folder is exactly what they meant.
        """
        if not self._items:
            return
        for item in self._items:
            if not item.is_actionable:
                continue
            if only_high_confidence and not item.is_high_confidence:
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

    def active_filters(self) -> List[str]:
        """Which filters are hiding rows, phrased for a person.

        An empty grid with a filter on looks exactly like an empty grid with
        nothing in it, and the difference matters enormously: one means "no
        such mail", the other means "you have a search box filled in".
        """
        names = []
        if self._text:
            names.append(f"the search for \u201c{self._text}\u201d")
        if self._category:
            names.append(f"the {self._category} category")
        if self._hide_non_job:
            names.append("showing job mail only")
        if self._only_selected:
            names.append("showing ticked rows only")
        if self._accounts:
            names.append("the mailbox filter")
        return names

    def clear_filters(self) -> None:
        """Undo every one of them at once."""
        self._text = ""
        self._category = None
        self._hide_non_job = False
        self._only_selected = False
        self._accounts = set()
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


class PreviewPane(QWidget):
    """Side-by-side message text and the backend's reasoning."""

    overrideChanged = Signal(int, object)  # source row, folder or None
    #: "Sort this mail too" - the window turns non-job routing on.
    sortNonJobRequested = Signal()
    attachmentsRequested = Signal(int)   # source row

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

        self.attachments_button = QPushButton("Attachments")
        self.attachments_button.setEnabled(False)
        self.attachments_button.setToolTip(
            "Open what was attached. Images, audio, PDFs and text are shown "
            "here; anything else can be saved. Nothing is ever run.")
        self.attachments_button.clicked.connect(self._open_attachments)

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
        self.folder_combo.setToolTip(
            "Where this message will go when you press Apply. Change it to "
            "override the suggestion - the app remembers, and files the next "
            "message from this sender the same way.")
        self.folder_combo.setEditable(True)
        self.folder_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.folder_combo.setMinimumWidth(280)
        self.folder_combo.currentTextChanged.connect(self._folder_changed)

        self.reset_button = QToolButton()
        self.reset_button.setText("Use AI suggestion")
        self.reset_button.setToolTip(
            "Undo your override for this message and go back to where the "
            "analysis wanted to put it.")
        self.reset_button.clicked.connect(self._reset_override)

        # Why a row cannot be ticked, and the button that changes it. A row
        # that sits there inert with no explanation is the single most
        # confusing thing the window can show, and it is the default state
        # for every message that is not job mail.
        self.inert_note = QLabel("")
        self.inert_note.setWordWrap(True)
        self.inert_note.setTextFormat(Qt.TextFormat.RichText)
        self.inert_note.setProperty("dim", "true")
        self.sort_these_button = QToolButton()
        self.sort_these_button.setText("Sort this mail too")
        self.sort_these_button.setToolTip(
            "File non-job mail by topic into Sorted Mail, instead of leaving "
            "it in the inbox.")
        self.sort_these_button.clicked.connect(self.sortNonJobRequested)
        self.inert_row = QWidget()
        inert_layout = QHBoxLayout(self.inert_row)
        inert_layout.setContentsMargins(0, 0, 0, 0)
        inert_layout.setSpacing(8)
        inert_layout.addWidget(self.inert_note, 1)
        inert_layout.addWidget(self.sort_these_button)
        self.inert_row.setVisible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("Source:"))
        mode_row.addWidget(self.body_mode, 1)
        mode_row.addWidget(self.attachments_button)
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
        layout.addWidget(self.inert_row)
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
        self.inert_row.setVisible(False)
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
        self._sync_attachments(item)
        self._prompt_text = prompt_text
        message = item.email

        reason = item.why_not_actionable
        self.inert_row.setVisible(bool(reason))
        if reason:
            self.inert_note.setText(_html(reason))
            # The button only helps for the one cause it can actually fix.
            self.sort_these_button.setVisible(item.left_because_not_job)

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
                text += "\n\n--- ATTACHMENTS ---\n" + "\n".join(
                    f"• {_attachment_line(a)}" for a in message.attachments)
            self.body_view.setPlainText(text)

    def _sync_attachments(self, item) -> None:
        """The button says how many, because "Attachments" alone is a guess."""
        names = getattr(item.email, "attachments", ()) or ()
        self.attachments_button.setEnabled(bool(names))
        self.attachments_button.setText(
            f"Attachments ({len(names)})" if names else "Attachments")

    @Slot()
    def _open_attachments(self) -> None:
        """Ask whoever owns this pane to fetch and show them.

        The pane has no mailbox connection of its own, and it should not: the
        bytes are not in the message this pane was handed, because a scan
        only ever downloads the first part of each message.
        """
        if self._item is not None:
            self.attachmentsRequested.emit(self._row or 0)

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


def _runners_up(classification) -> str:
    """The categories that did not win, and how close they came.

    A verdict that beat its nearest rival by a tenth of a point is a
    different thing from one that beat it by five, and the confidence number
    alone does not distinguish them.

    The winner is excluded by name rather than by being the top score,
    because it is not always the top score: precedence can hand the decision
    to a lower-scoring category, and listing the winner as its own runner-up
    is how that bug read on screen.
    """
    scores = {name: value for name, value in (classification.scores or {}).items()
              if value > 0}
    won = (classification.category.value if classification.is_job_related
           else classification.other_category.value)
    rivals = {name: value for name, value in scores.items() if name != won}
    if not rivals:
        return ""
    top = max(scores.values()) or 1.0
    ranked = sorted(rivals.items(), key=lambda pair: -pair[1])
    parts = []
    for name, value in ranked[:3]:
        share = value / top
        label = _html(name.replace("_", " ").title())
        if value > scores.get(won, 0.0):
            # It outscored the winner and lost on precedence. Saying so is
            # the difference between an explanation and a puzzle.
            parts.append(f"{label} <span style='opacity:0.7'>(scored higher; "
                         "outranked)</span>")
        else:
            parts.append(f"{label} <span style='opacity:0.7'>"
                         f"({share * 100:.0f}% of the winner)</span>")
    return "<br>".join(parts)


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
    if classification.signals:
        # What actually fired, in the sorter's own words. A verdict with a
        # reason you can read is one you can argue with; "confidence 0.91" is
        # not something anybody can act on.
        shown = [f"• {_html(signal)}" for signal in classification.signals[:8]]
        extra = len(classification.signals) - len(shown)
        if extra > 0:
            shown.append(f"<span style='opacity:0.6'>…and {extra} more</span>")
        rows.append(("Matched", "<br>".join(shown)))
    runners = _runners_up(classification)
    if runners:
        rows.append(("Runners-up", runners))
    if item.in_a_conversation:
        rows.append(("Conversation",
                     _html(conversations.describe(item.thread_size).capitalize())))
    if item.rule_name:
        rows.append(("Rule", f"<span style='color:{ACCENT_BLUE}'>"
                             f"{_html(item.rule_name)}</span>"))
    if item.learned_because:
        # Directly under the decision, because it is the reason for it.
        rows.append(("Learned",
                     f"<span style='color:{ACCENT_BLUE}'>"
                     f"{_html(item.learned_because)}</span>"))
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
            f'• <a href="{_attr_url(link)}">{_html(link[:110])}</a>' for link in item.email.links[:12]
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

def _attachment_line(name: str) -> str:
    """Name, and what kind of file the name says it is.

    The list is built from the server's description of the message, so it is
    there before anything is downloaded. Saying "photo.heic - image" beats
    saying "photo.heic" to somebody deciding whether to bother opening it.
    """
    import attachments

    shown = attachments.display_name(name)
    ext = attachments.extension(name)
    kinds = {
        "image": ("png jpg jpeg gif webp heic heif tif tiff bmp svg avif"),
        "audio": ("mp3 m4a aac wav flac ogg oga opus aiff aif wma"),
        "video": ("mp4 mov m4v avi mkv webm wmv"),
        "document": ("pdf doc docx rtf odt pages txt md csv tsv xls xlsx "
                     "numbers ppt pptx key epub"),
        "archive": ("zip tar gz tgz bz2 xz 7z rar"),
        "program": ("app exe dmg pkg sh command scpt jar msi"),
    }
    for label, extensions in kinds.items():
        if ext in extensions.split():
            return f"{shown}  -  {label}"
    return f"{shown}  -  {ext or 'file'}"
