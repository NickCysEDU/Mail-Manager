"""Small widgets and helpers the windows are built from.

Split out of gui.py, which had grown to six and a half thousand lines - a
third of the whole application in one file. Nothing here knows anything about
mail: these are the pieces that make a window look like this application
rather than a default Qt one, and they are used by every part of it.

A pure move. Every definition is exactly as it was.
"""

from __future__ import annotations

import html as html_module
import re
import textwrap
from pathlib import Path
from typing import List, Optional, Sequence

from PySide6.QtCore import QDate, QEvent, QObject, QRect, QSize, Qt
from PySide6.QtGui import (QColor, QFont, QFontDatabase, QFontMetrics, QIcon,
                           QPainter, QPalette, QPixmap)
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QLineEdit,
                               QListWidget, QMessageBox, QScrollArea,
                               QSizePolicy, QStyle, QStyleOptionViewItem,
                               QToolButton, QWidget)
from PySide6.QtCore import QThread

import buildinfo
import config

log = __import__("logging").getLogger(__name__)


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

#: Shown after a scan that found nothing, which is not the same thing as
#: never having scanned - and the advice for it is different.
NOTHING_FOUND = (
    "<div style='text-align:center;line-height:170%'>"
    "<span style='font-size:15px'><b>No messages in this window</b></span><br>"
    "<span style='opacity:0.7'>Nothing arrived in the time range you picked. "
    "Try a wider one,<br>or check that the right mailbox is selected."
    "</span></div>"
)


def describe(widget, name: str, hint: str = "") -> None:
    """Give a control a name a screen reader can read out.

    VoiceOver falls back to the visible text, which is fine for a button
    labelled "Scan & Analyze" and useless for a magnifying-glass icon, a bare
    combo box, or a table nobody has labelled. Where a tooltip already says
    the right thing it doubles as the description, so the two cannot drift.
    """
    widget.setAccessibleName(name)
    hint = hint or widget.toolTip()
    if hint:
        widget.setAccessibleDescription(hint)


def _one_of(names: Sequence[str]) -> str:
    """Join a list the way a person would say it out loud."""
    names = [n for n in names if n]
    if not names:
        return "a filter"
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


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
class VersionLabel(QLabel):
    """The build, bottom right. Click to copy it for a bug report."""

    def __init__(self, parent=None) -> None:
        super().__init__(buildinfo.short(), parent)
        self.setProperty("dim", "true")
        self.setToolTip(f"{buildinfo.full()}\n\nClick to copy.")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        QApplication.clipboard().setText(buildinfo.full())
        window = self.window()
        if hasattr(window, "_set_status"):
            window._set_status("Build details copied.")
        super().mouseReleaseEvent(event)


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


class SelectableMessages(QObject):
    """Makes the text in every message box selectable, as it is shown.

    Qt labels are not selectable by default, which is right for "are you
    sure?" and wrong for an error: the one thing anybody wants to do with a
    failure is paste it into a search or a bug report. Doing it here rather
    than at each of the thirty-odd call sites means the ones written later
    are covered too, including the boxes Qt raises itself.
    """

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        # Every event in the application passes through here, so the cheapest
        # possible test comes first and the work happens at most once per box.
        if event.type() != QEvent.Type.Show:
            return False
        if not isinstance(watched, QMessageBox):
            return False
        if watched.property("selectableDone"):
            return False
        watched.setProperty("selectableDone", True)
        selectable(watched)
        return False


def install_selectable_messages(app) -> "SelectableMessages":
    """Attach the filter once. Attaching twice would double every check."""
    existing = getattr(app, "_selectable_messages", None)
    if existing is None:
        existing = SelectableMessages(app)
        app.installEventFilter(existing)
        app._selectable_messages = existing
    return existing


def remove_selectable_messages(app) -> None:
    """Take it off again. Used by tests, so one does not leak into the next."""
    existing = getattr(app, "_selectable_messages", None)
    if existing is not None:
        app.removeEventFilter(existing)
        app._selectable_messages = None


def selectable(box: "QMessageBox") -> "QMessageBox":
    """Let the text in a message box be selected and copied.

    Qt makes label text unselectable by default, which is fine for "are you
    sure?" and useless for an error: the one thing anybody wants to do with a
    failure message is paste it somewhere. Cmd-C copies the whole box either
    way; this makes the visible text behave like text.
    """
    box.setTextInteractionFlags(
        Qt.TextInteractionFlag.TextSelectableByMouse
        | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    for label in box.findChildren(QLabel):
        label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        label.setCursor(Qt.CursorShape.IBeamCursor)
    return box


def say(parent, icon, title: str, text: str, detail: str = "") -> "QMessageBox":
    """A message box whose text can be selected, shown and returned.

    Errors go through here so that every one of them can be copied.
    """
    box = QMessageBox(icon, title, text, parent=parent)
    if detail:
        box.setDetailedText(detail)
    selectable(box)
    box.exec()
    return box


def _separator() -> QFrame:
    line = QFrame()
    line.setFrameShape(QFrame.Shape.HLine)
    line.setFrameShadow(QFrame.Shadow.Sunken)
    return line


