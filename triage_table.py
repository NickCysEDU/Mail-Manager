"""The middle of the window: the table of messages and the preview beside it.
The model holds the triage items, the proxy filters and sorts, three
delegates paint the confidence bar, the wrapped subject and the category
chip, and the preview shows the selected row. gui re-exports these.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Sequence, Tuple

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QSize, QUrl,
                            QSortFilterProxyModel, Qt, Signal, Slot)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QTextDocument
from PySide6.QtWidgets import (QApplication, QHBoxLayout, QLabel,
                               QPlainTextEdit, QPushButton, QSplitter, QStyle,
                               QStyledItemDelegate, QStyleOptionViewItem,
                               QTextBrowser, QToolButton, QVBoxLayout, QWidget,
    QInputDialog, QMenu,
    QStackedWidget)

import html_utils
import conversations
from flowlayout import FlowHolder, FlowLayout
import theme
from imap_engine import MoveReport
from models import (CATEGORY_COLORS, OTHER_COLOR, TOPIC_COLORS, Category,
                    Disposition, TriageItem, TriageSummary)
from widgets import (ACCENT_BLUE, ACCENT_RED, RoomyCombo, _attr_url,
                     _confidence_rgb, _draw_wrapped, _html, _is_dark,
                     _mono_font, _one_line, _tint, _wrap, system_font)


#: What the folder box shows when a message is to stay where it is.
LEAVE_IN_PLACE = "- leave in place -"

def category_color(classification, item=None) -> str:
    """The accent colour for a row. A job category always has one; an everyday
    topic only when the current settings file that topic somewhere,
    otherwise grey, since the app will not act on it. Without ``item``
    (older callers), every non-job topic is grey.
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
        #: Collapsed one-line summary and reasoning per row: they never change
        #: once a scan lands.
        self._one_line_cache: List[Tuple[str, str]] = []

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
            # A hint of the category colour, so the eye can group rows.
            if item.moved:
                return None
            if item.disposition is Disposition.MOVE:
                return _tint(category_color(classification, item), 26)
            if item.disposition is Disposition.REVIEW:
                return _tint(CATEGORY_COLORS[Category.UNCLASSIFIED_OTHER], 22)
            return None

        if role == Qt.ItemDataRole.ForegroundRole:
            # None means the palette's text colour, as maximum contrast wants:
            # it is monochrome by design.
            if theme.monochrome():
                return None
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

    def conversation_of(self, row: int) -> List[int]:
        """Every row in the same conversation as this one, including it."""
        item = self.item_at(row)
        if item is None or not item.thread_key:
            return [row] if item is not None else []
        return [index for index, other in enumerate(self._items)
                if other.thread_key == item.thread_key]

    def set_approved(self, rows: Sequence[int], approved: bool,
                     only_high_confidence: bool = False) -> int:
        """Tick or untick a set of rows; returns how many changed. Rows that
        cannot be actioned (nowhere to move them, or already moved) are
        skipped, not refused. Every tick comes through here with the rows
        the caller means: a second path that walked the whole mailbox once
        ticked rows the filter was hiding.
        """
        changed = 0
        for row in rows:
            item = self.item_at(row)
            if item is None or not item.is_actionable:
                continue
            if only_high_confidence and not item.is_high_confidence:
                continue
            if item.approved != approved:
                item.approved = approved
                changed += 1
        if changed:
            self._refresh_column(self.COL_SELECT)
            self.selectionChanged.emit()
        return changed

    def restore_suggested_rows(self, rows) -> int:
        """Put particular rows back to what the sorter proposed."""
        changed = 0
        for row in rows:
            item = self.item_at(row)
            if item is None:
                continue
            if item.approved != item.default_approved:
                item.approved = item.default_approved
                changed += 1
        if changed:
            self._refresh_column(self.COL_SELECT)
            self.selectionChanged.emit()
        return changed

    def set_all_approved(self, approved: bool, only_high_confidence: bool = False) -> None:
        """Tick or untick every row in the model, filter or no filter. "Sure
        of" means the confidence bar, not ``default_approved``: non-job mail
        is never pre-ticked, but someone asking for every confident row
        means a confident receipt too. Nothing the user presses calls this;
        the window passes the rows the table shows. Kept for tests and code
        that means the whole model.
        """
        self.set_approved(range(len(self._items)), approved,
                          only_high_confidence=only_high_confidence)

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
        self._hide_job = False
        self._only_selected = False
        #: Empty means every mailbox. Filtering the view is separate from
        #: choosing what to scan.
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

    def set_hide_job(self, hide: bool) -> None:
        """The other way round: everything the run did not call job mail."""
        self._hide_job = bool(hide)
        self.invalidate()

    def set_account_filter(self, account_ids) -> None:
        self._accounts = set(account_ids or ())
        self.invalidate()

    def set_only_selected(self, only: bool) -> None:
        self._only_selected = bool(only)
        self.invalidate()

    def active_filters(self) -> List[str]:
        """Which filters are hiding rows, in words: an empty grid with a search
        filled in looks like an empty mailbox.
        """
        names = []
        if self._text:
            names.append(f"the search for \u201c{self._text}\u201d")
        if self._category:
            names.append(f"the {self._category} category")
        if self._hide_non_job:
            names.append("showing job mail only")
        if self._hide_job:
            names.append("showing everything but job mail")
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
        self._hide_job = False
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
        if self._hide_job and item.classification.is_job_related:
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
    """Wrapped multi-line text clipped to a fixed number of lines: one elided
    line cuts a long summary off after a few words; three show most whole.
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
        if selected:
            ink = option.palette.color(QPalette.ColorRole.HighlightedText)
        elif theme.monochrome():
            ink = option.palette.color(QPalette.ColorRole.Text)
        elif _is_dark(option.palette):
            ink = color.lighter(125)
        else:
            ink = color.darker(105)
        painter.setPen(ink)
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


class _SealedDocument(QTextDocument):
    """A document that reads nothing from disk or the network. QTextDocument
    itself opens a local file when the view declines to, so the view's own
    refusal is not enough. Embedded ``data:`` images still draw."""

    def loadResource(self, kind, name):      # noqa: N802 - Qt's name
        if name.scheme().lower() == "data":
            return super().loadResource(kind, name)
        return None


_FAMILIES = None


def _families_here() -> frozenset:
    """The font families this machine has, in lower case, found once."""
    global _FAMILIES
    if _FAMILIES is None:
        from PySide6.QtGui import QFontDatabase

        _FAMILIES = frozenset(name.lower() for name in QFontDatabase.families())
    return _FAMILIES


class MailView(QTextBrowser):
    """The message as sent, drawn by Qt's own document engine. Nothing the
    message refers to is fetched by the document: no stylesheet, no file.
    With pictures wanted, the view fetches each picture the message shows
    itself, by http or https, and hands it to the document."""

    #: The most a picture may weigh.
    PICTURE_MOST = 8 * 1024 * 1024

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDocument(_SealedDocument(self))
        self.setOpenExternalLinks(False)
        self.setOpenLinks(False)
        self.setSearchPaths([])
        # On its own light page whatever the app's look: a message is laid
        # out for one, and its dark grey text vanished on a dark base.
        page = self.palette()
        page.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
        page.setColor(QPalette.ColorRole.Text, QColor(20, 20, 20))
        self.setPalette(page)
        self._pictures = False
        self._html = ""
        self._manager = None
        self._pending: set = set()

    def loadResource(self, kind, name):      # noqa: N802 - Qt's name
        return None

    def set_pictures(self, wanted: bool) -> None:
        self._pictures = bool(wanted)

    def show_message(self, html: str) -> None:
        self._html = html_utils.sanitise_for_view(
            html, pictures=self._pictures, families=_families_here())
        self.setHtml(self._html)
        for url in self.pictures_wanted():
            self._pending.add(url)
            self._fetch(url)

    def pictures_wanted(self) -> list:
        """What is still to fetch for the message on show."""
        document = self.document()
        return [url for url in html_utils.pictures_in(self._html)
                if url not in self._pending and not document.resource(
                    QTextDocument.ResourceType.ImageResource, QUrl(url))]

    def _fetch(self, url: str) -> None:
        from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest

        if self._manager is None:
            self._manager = QNetworkAccessManager(self)
            self._manager.setTransferTimeout(10000)
            self._manager.finished.connect(self._picture_arrived)
        request = QNetworkRequest(QUrl(url))
        request.setAttribute(
            QNetworkRequest.Attribute.RedirectPolicyAttribute,
            QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        request.setMaximumRedirectsAllowed(3)
        self._manager.get(request)

    def _picture_arrived(self, reply) -> None:
        from PySide6.QtNetwork import QNetworkReply

        url = reply.request().url().toString()
        data = (bytes(reply.readAll())
                if reply.error() == QNetworkReply.NetworkError.NoError else b"")
        reply.deleteLater()
        self.add_picture(url, data)

    def add_picture(self, url: str, data: bytes) -> None:
        """A fetched picture into the message, where it is still the one on
        show; the view keeps its place."""
        from PySide6.QtGui import QImage

        self._pending.discard(url)
        if url not in html_utils.pictures_in(self._html):
            return
        if not data or len(data) > self.PICTURE_MOST:
            return
        image = QImage.fromData(data)
        if image.isNull():
            return
        self.document().addResource(
            QTextDocument.ResourceType.ImageResource, QUrl(url), image)
        scrolled = self.verticalScrollBar().value()
        self.setHtml(self._html)
        self.verticalScrollBar().setValue(scrolled)


class PreviewPane(QWidget):
    """Side-by-side message text and the backend's reasoning."""

    #: Space either side of the splitter's handle, so nothing in either half
    #: touches it.
    GUTTER = 10

    #: How wide the pane must be before the message and the analysis sit side
    #: by side, and how narrow before they stack again. Two columns need about
    #: forty-five characters each, which 800 gives; two numbers so a pane
    #: dragged to the threshold does not flip on every pixel.
    TWO_COLUMNS = 800
    ONE_COLUMN = 760

    #: The least height a half of the pane works at: a row of controls and a
    #: couple of lines under it. This and MIN_TALL overlap on purpose: the
    #: theme decides a row's height, and with only one the layout audit failed
    #: on some themes.
    HALF_TALL = 75

    #: The least room the whole pane is any use in (see HALF_TALL): the header,
    #: the filing row and a half. Below this the halves' controls draw outside
    #: themselves. Dragging the splitter to the end still closes it.
    MIN_TALL = 215

    #: The room the two halves need to be stacked rather than side by side.
    #: Stacking suits a narrow pane and costs height; under the table on an
    #: 800x560 window the pane is wide and short, and stacking there left the
    #: message 38 px. Short and wide always gets two columns.
    MIN_STACK = 180

    overrideChanged = Signal(int, object)  # source row, folder or None
    #: "Sort this mail too" - the window turns non-job routing on.
    sortNonJobRequested = Signal()
    attachmentsRequested = Signal(int)   # source row
    #: Write back - "reply", "reply_all" or "forward" - to the row shown,
    #: or open it in a window of its own.
    composeRequested = Signal(str, int)
    openRequested = Signal(int)
    #: A link from the message to open, as its address. Whoever owns the
    #: pane says where it goes first: see link_open.
    linkRequested = Signal(str)

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
            "Open what was attached. Nothing is ever run.")
        self.attachments_button.clicked.connect(self._open_attachments)

        self.links_button = QPushButton("Links")
        self.links_button.setEnabled(False)
        self.links_button.setToolTip(
            "Every link in the message, by where it really goes.")
        self.links_button.clicked.connect(self._show_links)

        # Writing back, in one button with a menu, so the row stays short.
        self.reply_button = QToolButton()
        self.reply_button.setText("Reply")
        self.reply_button.setEnabled(False)
        self.reply_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.reply_button.setProperty("menu", "true")
        self.reply_button.setToolTip("Answer, forward or open this message.")
        self.reply_menu = QMenu(self.reply_button)
        for label, mode in (("Reply", "reply"), ("Reply All", "reply_all"),
                            ("Forward", "forward")):
            action = self.reply_menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, m=mode: self._compose(m))
        self.reply_menu.addSeparator()
        self.open_action = self.reply_menu.addAction("Open in a Window")
        self.open_action.triggered.connect(self._open_window)
        self.reply_button.setMenu(self.reply_menu)
        self._link_list = None

        self.body_mode = RoomyCombo(every=True)
        # Short enough to sit on one line beside the Attachments button, in a
        # half as small as 306 by 70 px. The caption said what the entries
        # already say, and the longer wording is in the tooltip.
        self.body_mode.addItems(["Message", "Plain text", "What was sent"])
        self.body_mode.currentIndexChanged.connect(self._render_body)
        self.body_mode.setToolTip(
            "The message as it was sent, its plain text, or exactly what "
            "was sent to the model.")

        self.rich_view = MailView()
        self.rich_view.setMinimumHeight(24)
        self.rich_view.anchorClicked.connect(
            lambda url: self.linkRequested.emit(url.toString()))
        self.body_view = QPlainTextEdit()
        # Smaller than Qt's default text box (ninety pixels square): this half
        # can be about ninety pixels tall in all, and a text view scrolls.
        self.body_view.setMinimumHeight(24)
        self.body_view.setReadOnly(True)
        self.body_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.body_view.setFont(_mono_font())
        self.body_stack = QStackedWidget()
        self.body_stack.addWidget(self.rich_view)
        self.body_stack.addWidget(self.body_view)
        self.body_stack.setMinimumHeight(24)

        self.reasoning_view = QTextBrowser()
        self.reasoning_view.setMinimumHeight(24)
        # Not opened here: a link in the analysis came from the message and
        # goes the same way as the rest.
        self.reasoning_view.setOpenExternalLinks(False)
        self.reasoning_view.setOpenLinks(False)
        self.reasoning_view.anchorClicked.connect(
            lambda url: self.linkRequested.emit(url.toString()))

        self.folder_combo = RoomyCombo()
        self.folder_combo.setToolTip(
            "Where this message goes when you press Apply. Change it and "
            "the next one from this sender follows.")
        self.folder_combo.setMinimumWidth(280)
        self._folder_was = 0
        self.folder_combo.currentIndexChanged.connect(self._folder_picked)

        self.reset_button = QToolButton()
        self.reset_button.setText("Use AI suggestion")
        self.reset_button.setToolTip(
            "Undo your override for this message and go back to where the "
            "analysis wanted to put it.")
        self.reset_button.clicked.connect(self._reset_override)

        # Why a row cannot be ticked, and the button that changes it: the
        # default state for every message that is not job mail, and confusing
        # without a word.
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
        # A gutter against the splitter's handle, so the controls do not sit
        # hard against it.
        left_layout.setContentsMargins(0, 0, self.GUTTER, 0)
        # One line, short enough to stay one line: this half can be 306 px by
        # 70, and a wrapped row needed height it has not got.
        mode_row = QHBoxLayout()
        mode_row.setContentsMargins(0, 0, 0, 0)
        mode_row.addWidget(self.body_mode, 1)
        self.mode_row = QWidget()
        self.mode_row.setLayout(mode_row)
        left_layout.addWidget(self.mode_row)
        left_layout.addWidget(self.body_stack, 1)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(self.GUTTER, 0, 0, 0)
        self.analysis_label = QLabel("Analysis")
        right_layout.addWidget(self.analysis_label)
        right_layout.addWidget(self.reasoning_view, 1)

        # Each half tall enough for its controls and a few lines of text; a
        # splitter otherwise squeezes it until the controls draw outside
        # themselves.
        left.setMinimumHeight(self.HALF_TALL)
        right.setMinimumHeight(self.HALF_TALL)
        self._left, self._right = left, right
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(left)
        self.splitter.addWidget(right)
        self.splitter.setSizes([560, 460])

        # A row that wraps: "File into:", a folder and a button need 471 px,
        # and a preview beside the table on a small screen can be 420.
        folder_row = FlowLayout(margin=0, spacing=6, vertical_spacing=6)
        folder_row.addWidget(QLabel("File into:"))
        folder_row.addWidget(self.folder_combo)
        folder_row.addWidget(self.reset_button)
        self.folder_row = FlowHolder(folder_row)

        # Attachments sits up here with the message, where the whole width is
        # free: in the text half it did not fit beside the box.
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.header, 1)
        top.addWidget(self.reply_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.links_button, 0, Qt.AlignmentFlag.AlignTop)
        top.addWidget(self.attachments_button, 0,
                      Qt.AlignmentFlag.AlignTop)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.addLayout(top)
        layout.addWidget(self.inert_row)
        layout.addWidget(self.folder_row)
        layout.addWidget(self.splitter, 1)

        # Tall enough to hold what is in it: a splitter squeezes a child to
        # nothing otherwise, and the message half got 38 px. Dragging it to the
        # end still closes it.
        self.setMinimumHeight(self.MIN_TALL)

        self.set_folder_choices([])
        self.clear()

    def resizeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        super().resizeEvent(event)
        self._fit_header()
        self._arrange(self.width())

    def _arrange(self, width: int) -> None:
        """Side by side when there is room for both, stacked when not."""
        across = self.splitter.orientation() == Qt.Orientation.Horizontal
        # The room the two halves would have, after the header and the filing
        # row take their share.
        room = self.splitter.height() or self.height()
        tall = room >= self.MIN_STACK
        if across and width < self.ONE_COLUMN and tall:
            self._stack(Qt.Orientation.Vertical, (0, self.GUTTER, 0, 0),
                        (0, 0, 0, 0))
        elif not across and (width > self.TWO_COLUMNS or not tall):
            self._stack(Qt.Orientation.Horizontal, (0, 0, self.GUTTER, 0),
                        (self.GUTTER, 0, 0, 0))

    def _stack(self, orientation, left_edge, right_edge) -> None:
        """Turn the inner splitter, and move the gutter to whichever side the
        handle is now on.
        """
        self.splitter.setOrientation(orientation)
        self._left.layout().setContentsMargins(*left_edge)
        self._right.layout().setContentsMargins(*right_edge)
        span = (self.splitter.width()
                if orientation == Qt.Orientation.Horizontal
                else self.splitter.height())
        if span > 1:
            self.splitter.setSizes([int(span * 0.55), int(span * 0.45)])

    def _header_lines(self, lines: int) -> int:
        """How tall that many lines of the header are, in this theme."""
        metrics = QFontMetrics(self.header.font())
        # 150 per cent, which is what the markup asks for.
        return int(metrics.lineSpacing() * 1.5 * lines) + 6

    def _fit_header(self) -> None:
        """The whole subject when the pane has the room for it, and never
        less than three lines: the two halves under it keep their minimum,
        and the rest is the header's."""
        spare = (self.height() - self.MIN_STACK - 40
                 - self.folder_row.sizeHint().height()
                 - (self.inert_row.sizeHint().height()
                    if self.inert_row.isVisible() else 0))
        self.header.setMaximumHeight(max(self._header_lines(3), spare))

    def set_backend_label(self, label: str) -> None:
        """Name the backend that produced the reasoning shown on the right."""
        self.analysis_label.setText(f"{label} analysis" if label else "Analysis")
        self.body_mode.setItemText(
            2, f"Exactly what {label} was sent" if label else "Exactly what the model was sent"
        )

    #: The last entry of the folder list, which asks for a folder by name.
    OTHER_FOLDER = "Other folder…"

    def set_folder_choices(self, folders: Sequence[str]) -> None:
        current = self.folder_combo.currentText()
        self._updating = True
        self.folder_combo.clear()
        self.folder_combo.addItem(LEAVE_IN_PLACE)
        for folder in folders:
            self.folder_combo.addItem(folder)
        self.folder_combo.addItem(self.OTHER_FOLDER, "other")
        if current and current != self.OTHER_FOLDER:
            self._pick_folder(current)
        self._folder_was = self.folder_combo.currentIndex()
        self._updating = False

    def _pick_folder(self, name: str) -> None:
        """Select ``name``, listing it before "Other folder…" if it is not
        offered."""
        index = self.folder_combo.findText(name)
        if index < 0:
            index = self.folder_combo.count() - 1
            self.folder_combo.insertItem(index, name)
        self.folder_combo.setCurrentIndex(index)

    @Slot(int)
    def _folder_picked(self, index: int) -> None:
        if self._updating or index < 0:
            return
        if self.folder_combo.itemData(index) == "other":
            name, ok = QInputDialog.getText(self, "File into",
                                            "Folder:")
            self._updating = True
            if ok and name.strip():
                self._pick_folder(name.strip())
            else:
                self.folder_combo.setCurrentIndex(self._folder_was)
            self._updating = False
            if not (ok and name.strip()):
                return
            index = self.folder_combo.currentIndex()
        self._folder_was = index
        self._folder_changed(self.folder_combo.itemText(index))

    def clear(self) -> None:
        self._row = None
        self._item = None
        self._prompt_text = ""
        self.inert_row.setVisible(False)
        self.header.setText(
            "<i>Select a message above to compare its text against the analysis.</i>"
        )
        self.body_view.setPlainText("")
        self.rich_view.clear()
        self.body_stack.setCurrentWidget(self.body_view)
        self.reasoning_view.setHtml("")
        self.folder_combo.setEnabled(False)
        self.reset_button.setEnabled(False)
        self.links_button.setEnabled(False)
        self.links_button.setText("Links")
        self.reply_button.setEnabled(False)

    def _compose(self, mode: str) -> None:
        if self._row is not None:
            self.composeRequested.emit(mode, self._row)

    def _open_window(self) -> None:
        if self._row is not None:
            self.openRequested.emit(self._row)

    def show_item(self, row: int, item: TriageItem, prompt_text: str = "") -> None:
        self._row = row
        self._item = item
        self._sync_attachments(item)
        self._sync_links(item)
        self.reply_button.setEnabled(True)
        self._prompt_text = prompt_text
        message = item.email

        reason = item.why_not_actionable
        self.inert_row.setVisible(bool(reason))
        if reason:
            self.inert_note.setText(_html(reason))
            # The button only helps for the one cause it can actually fix.
            self.sort_these_button.setVisible(item.left_because_not_job)

        badge = _disposition_badge(item)
        flags = {flag.lower() for flag in (message.flags or ())}
        marks = ""
        if "\\flagged" in flags:
            marks += " · <span style='color:#E0A426'>⚑ Flagged</span>"
        if "\\seen" not in flags:
            marks += " · Unread"
        # A shorter date than "Monday 20 September 2026": this label wraps, and
        # every line comes off the two halves under it.
        self.header.setText(
            f"<div style='line-height:150%'>"
            f"<b>{_html(message.subject_display)}</b><br>"
            f"<span style='opacity:0.85'>{_html(message.sender_display)}</span> · "
            f"{_html(message.date_display('%a %d %b %Y, %H:%M'))}{marks}<br>"
            f"{badge}</div>"
        )
        self.header.setToolTip(
            f"{message.subject_display}\n{message.sender_display}\n"
            f"{message.date_display('%A %d %B %Y, %H:%M')}")
        self._fit_header()

        self._updating = True
        self.folder_combo.setEnabled(not item.moved)
        self.reset_button.setEnabled(not item.moved and item.override_folder is not None)
        self._pick_folder(item.target_folder or LEAVE_IN_PLACE)
        self._folder_was = self.folder_combo.currentIndex()
        self._updating = False

        self._render_body()
        self.reasoning_view.setHtml(_reasoning_html(item))

    @Slot()
    def _render_body(self) -> None:
        if self._item is None:
            return
        mode = self.body_mode.currentIndex()
        if mode == 2 and self._prompt_text:
            self.body_view.setPlainText(self._prompt_text)
            self.body_stack.setCurrentWidget(self.body_view)
            return
        message = self._item.email
        text = message.body_text or "(this message had no readable text body)"
        if message.links:
            text += "\n\n--- LINKS ---\n" + "\n".join(f"• {link}" for link in message.links)
        if message.attachments:
            text += "\n\n--- ATTACHMENTS ---\n" + "\n".join(
                f"• {_attachment_line(a)}" for a in message.attachments)
        self.body_view.setPlainText(text)
        # The message as sent where there is HTML to draw; plain mail is its
        # text either way.
        if mode == 0 and message.body_html:
            self.rich_view.show_message(message.body_html)
            self.body_stack.setCurrentWidget(self.rich_view)
        else:
            self.body_stack.setCurrentWidget(self.body_view)

    def set_pictures(self, wanted: bool) -> None:
        """Whether a message's pictures are fetched and shown."""
        self.rich_view.set_pictures(wanted)
        self._render_body()

    def _sync_attachments(self, item) -> None:
        """The button says how many, because "Attachments" alone is a guess."""
        names = getattr(item.email, "attachments", ()) or ()
        self.attachments_button.setEnabled(bool(names))
        self.attachments_button.setText(
            f"Attachments ({len(names)})" if names else "Attachments")

    def _sync_links(self, item) -> None:
        import link_open

        links = [link for link in (getattr(item.email, "links", ()) or ())
                 if link_open.opens(link)]
        self.links_button.setEnabled(bool(links))
        self.links_button.setText(f"Links ({len(links)})" if links
                                  else "Links")

    @Slot()
    def _show_links(self) -> None:
        """The message's links, by where each goes, to open or copy."""
        import link_open

        if self._item is None:
            return
        dialog = link_open.LinkList(self._item.email.links,
                                    self._item.email.subject_display, self)
        dialog.chosen.connect(self.linkRequested)
        self._link_list = dialog
        dialog.open()

    @Slot()
    def _open_attachments(self) -> None:
        """Ask whoever owns this pane to fetch and show them: a scan only
        downloads the first part of each message, and the pane has no
        connection of its own.
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
    """The categories that did not win, and how close they came: a verdict won
    by a tenth of a point differs from one won by five. The winner is
    excluded by name, since precedence can pick a lower-scoring category.
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
            # It outscored the winner and lost on precedence; saying so
            # explains it.
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
        # What fired, in the sorter's own words: a reason you can read is one
        # you can argue with.
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



def _attachment_line(name: str) -> str:
    """The name, and what kind of file the name says it is, from the server's
    description before anything downloads: "photo.heic - image" helps
    someone decide whether to open it.
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
