"""A help mode that puts tooltips back where they belong.

Qt shows a tooltip after a fixed delay whether or not anybody wanted one, and
on a dense window that is mostly noise. This makes it deliberate: a circled
question mark in the corner, filled when it is on, and while it is on every
control explains itself after a short hover.

The toggle also lengthens the delay and the time on screen, because the point
of turning it on is that you are reading rather than working.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QObject, QRect, Qt
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import QToolButton, QToolTip, QWidget

#: How long a hover has to last before an explanation appears, in milliseconds.
HOVER_DELAY = 600
#: How long it stays there. Long enough to finish a sentence twice.
VISIBLE_FOR = 20000


def circle_in(rect: QRect) -> QRect:
    """The largest sensible circle inside a rect, centred.

    Its own function because the button is not always the size it asks for:
    it requests 26 by 26, the shared stylesheet gives every control a minimum
    height, and it arrives 26 by 28. Painting into the whole rect drew an
    oval. Taking a square off the shorter side means no stylesheet can
    squash it, and it can be checked without a widget that refuses to resize.
    """
    side = max(8, min(rect.width(), rect.height()) - 6)
    box = QRect(0, 0, side, side)
    box.moveCenter(rect.center())
    return box


class HelpButton(QToolButton):
    """A question mark in a circle: outlined when off, filled when on."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setObjectName("helpButton")   # excluded from the shared control height
        self.setFixedSize(26, 26)
        self.setText("")
        self._sync_text()
        self.toggled.connect(lambda _on: self._sync_text())

    def _sync_text(self) -> None:
        on = self.isChecked()
        self.setToolTip(
            "Help is on. Hover anything for a moment and it will explain "
            "itself. Click to turn it off."
            if on else
            "Turn on help. Hovering anything then explains what it does."
        )
        self.setAccessibleName("Help" + (" (on)" if on else ""))
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        colour = self.palette().color(
            self.palette().ColorRole.Highlight if self.isChecked()
            else self.palette().ColorRole.WindowText
        )
        box = circle_in(self.rect())

        if self.isChecked():
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(colour)
            painter.drawEllipse(box)
            ink = self.palette().color(self.palette().ColorRole.HighlightedText)
        else:
            pen = QPen(colour, 1.6)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(box)
            ink = colour

        font = self.font()
        font.setPointSizeF(max(9.0, font.pointSizeF()))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(ink)
        painter.drawText(box, int(Qt.AlignmentFlag.AlignCenter), "?")
        painter.end()


class HelpFilter(QObject):
    """While help is on, show a widget's tooltip after a deliberate hover."""

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.enabled = False

    def set_enabled(self, on: bool) -> None:
        self.enabled = bool(on)
        if not self.enabled:
            QToolTip.hideText()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802
        """Tooltips appear only while help is on.

        Qt shows any tooltip a widget happens to carry, which on a window this
        dense means explanations arriving unasked while somebody is working.
        With help off this swallows them; with help on it also lends a widget
        its parent's wording when it has none of its own, and leaves it up long
        enough to finish reading.
        """
        if event.type() != QEvent.Type.ToolTip:
            return False
        if not self.enabled:
            QToolTip.hideText()
            return True                     # eaten: nothing asked for it
        widget = watched if isinstance(watched, QWidget) else None
        if widget is None:
            return False
        text = widget.toolTip()
        if not text:
            parent = widget.parentWidget()
            text = parent.toolTip() if parent is not None else ""
        if not text:
            return False
        QToolTip.showText(event.globalPos(), text, widget,
                          widget.rect(), VISIBLE_FOR)
        return True


def install(app, on: bool) -> HelpFilter:
    """Attach the filter to an application and set the hover delay."""
    existing = getattr(app, "_help_filter", None)
    if existing is None:
        existing = HelpFilter(app)
        app.installEventFilter(existing)
        app._help_filter = existing
    existing.set_enabled(on)
    return existing
