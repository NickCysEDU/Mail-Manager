"""The briefing, on screen.

A window rather than a panel: it is read once, in one pass, and closed.
Clicking anything under "Needs you" selects that message in the table
behind, so reading the briefing and acting on it are one motion.

:mod:`briefing` decides what the sections say and this arranges them, so the
window and what "Copy" puts on the clipboard are the same object rendered
twice.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFrame, QGridLayout,
                               QLabel, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

import briefing as _briefing
import widgets
from briefing import Briefing, Urgency

#: The marker down the left of a line, and the colour of its text: a marker and
#: coloured words rather than filled buttons, which in a row read as a warning
#: label.
#:
#: Only the two that mean somebody is waiting on you are marked; a line the
#: sorter was unsure about is unfinished, not urgent.
MARK = {
    Urgency.ACT_NOW: ("●", widgets.ACCENT_RED),
    Urgency.ANSWER: ("●", widgets.ACCENT_BLUE),
    Urgency.UNSURE: ("○", ""),
}


class _Card(QFrame):
    """One section: a heading and the lines under it.

    Three columns, the same in every card: a count, the line, and where it
    is bound for. The lines have to share edges: a clickable line (a flat
    button) and a plain one (a label) have the same padding and height, a
    count sits at the top of a wrapped line rather than centred on it, and
    the right-hand column has a fixed width, so no folder name moves the end
    of the middle column or takes the line's room.
    """

    #: The two outer columns, which are the same width in every card so that
    #: the middle one starts and ends in the same place down the page.
    COUNT_WIDTH = 34
    FOLDER_WIDTH = 150

    def __init__(self, title: str, blurb: str = "", parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("briefingCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(16, 14, 16, 14)
        self._column.setSpacing(6)

        heading = QLabel(title)
        font = QFont(heading.font())
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF() + 1.0)
        heading.setFont(font)
        self._column.addWidget(heading)
        if blurb:
            note = QLabel(blurb)
            note.setWordWrap(True)
            note.setProperty("dim", "true")
            self._column.addWidget(note)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(12)
        self._grid.setVerticalSpacing(4)
        self._grid.setColumnStretch(1, 1)
        self._grid.setColumnMinimumWidth(0, self.COUNT_WIDTH)
        self._column.addLayout(self._grid)
        self._rows = 0

    def add(self, count: str, text: str, folder: str = "",
            urgency: int = Urgency.NOTE, tooltip: str = "",
            on_click=None, dim: bool = False) -> None:
        number = QLabel(count)
        # Top, not centre: a count centred against a wrapped line floats away
        # from it.
        number.setAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignTop)
        number.setProperty("dim", "true")
        number.setFixedWidth(self.COUNT_WIDTH)

        marker, colour = MARK.get(urgency, ("", ""))
        if marker and not count:
            number.setText(marker)
            if colour:
                # Colour only: padding or a radius would make this line a
                # different height from the ones around it.
                number.setStyleSheet(f"color: {colour};")

        body: QWidget
        if on_click is not None:
            button = QPushButton(text)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A link, not a button: setFlat only drops the raised look, and the
            # theme still draws a border. No padding and no minimum height of
            # its own: the theme gives every button one height so that mixed
            # controls line up, which made the clickable lines taller than the
            # plain ones.
            rule = ("text-align: left; padding: 0px; margin: 0px; "
                    "min-height: 0px; background: none; border: none;")
            if colour:
                rule += f" color: {colour};"
            button.setStyleSheet(rule)
            button.clicked.connect(on_click)
            body = button
        else:
            label = QLabel(text)
            label.setWordWrap(True)
            # Inside the label as well as in the grid.
            #
            # A wrapped label sizes itself from a width it is told before the
            # grid settles, and comes out a line taller than its text (45
            # pixels for two 15-pixel lines), with the spare shared above and
            # below. Anchored at the top, the text starts level with its count.
            label.setAlignment(Qt.AlignmentFlag.AlignLeft
                               | Qt.AlignmentFlag.AlignTop)
            if dim:
                label.setProperty("dim", "true")
            body = label
        body.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        if tooltip:
            body.setToolTip(tooltip)

        self._grid.addWidget(number, self._rows, 0,
                             Qt.AlignmentFlag.AlignRight
                             | Qt.AlignmentFlag.AlignTop)
        # Top, like its count: a single-line label given spare room centres its
        # text, while the fixed-height count beside it stays at the top.
        self._grid.addWidget(body, self._rows, 1,
                             Qt.AlignmentFlag.AlignTop)
        if folder:
            where = QLabel()
            where.setProperty("dim", "true")
            where.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignTop)
            where.setToolTip(f"Bound for {folder}")
            where.setFixedWidth(self.FOLDER_WIDTH)
            where.setText(where.fontMetrics().elidedText(
                folder, Qt.TextElideMode.ElideLeft, self.FOLDER_WIDTH))
            # Once a card has a folder in it, the column is that wide whatever
            # the names, so the middle column ends in the same place on every
            # line.
            self._grid.setColumnMinimumWidth(2, self.FOLDER_WIDTH)
            self._grid.addWidget(where, self._rows, 2,
                                 Qt.AlignmentFlag.AlignRight
                                 | Qt.AlignmentFlag.AlignTop)
        self._rows += 1


class BriefingDialog(QDialog):
    """What the last scan found, in the order somebody wants to be told it."""

    #: Room between the cards and the scroll bar, matching the margin on the
    #: other side: at 700x520 the cards ended a pixel from the bar and had
    #: eleven to the left edge.
    BAR_GAP = 11

    #: A row in the table the reader wants to look at.
    show_row = Signal(int)

    def __init__(self, report: Briefing, parent=None) -> None:
        super().__init__(parent)
        self.report = report
        self.setWindowTitle("Briefing")
        self.setMinimumSize(660, 520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        headline = QLabel(report.headline.sentence())
        headline.setWordWrap(True)
        font = QFont(headline.font())
        font.setPointSizeF(font.pointSizeF() + 3.0)
        font.setBold(True)
        headline.setFont(font)
        layout.addWidget(headline)

        filing = report.headline.filing()
        if filing:
            note = QLabel(filing)
            note.setWordWrap(True)
            note.setProperty("dim", "true")
            layout.addWidget(note)

        inner = QWidget()
        column = QVBoxLayout(inner)
        column.setContentsMargins(0, 0, self.BAR_GAP, 0)
        column.setSpacing(12)
        for card in self._cards(report):
            column.addWidget(card)
        column.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(inner)
        layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox()
        copy = QPushButton("Copy")
        copy.setToolTip("Put the whole briefing on the clipboard as text.")
        copy.clicked.connect(self._copy)
        buttons.addButton(copy, QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        close.clicked.connect(self.accept)
        layout.addWidget(buttons)

    # -- the sections -----------------------------------------------------
    def _cards(self, report: Briefing) -> List[QWidget]:
        found: List[QWidget] = []
        if report.is_empty:
            card = _Card("Nothing arrived",
                         "Either the window is too narrow or it really has "
                         "been quiet. Widen it from the toolbar and scan "
                         "again if that seems wrong.")
            found.append(card)
            if report.quiet:
                card.add("", "Nothing at all from " + ", ".join(report.quiet))
            return found

        if report.attention:
            card = _Card(
                "Needs you",
                "Ranked by what it costs to miss rather than when it "
                "arrived. Click one to find it in the table.")
            for line in report.attention:
                card.add("", line.label, line.folder, line.urgency,
                         tooltip=line.detail,
                         on_click=self._jump(line.row))
                if line.detail:
                    card.add("", line.detail, dim=True)
            found.append(card)

        if report.arrivals:
            card = _Card("What came in", "Every message, counted by what it is.")
            for line in report.arrivals:
                card.add(str(line.count), line.label, line.folder, line.urgency)
            found.append(card)

        if report.folders:
            card = _Card("Where it is going",
                         "What pressing Apply would do, by folder.")
            for line in report.folders:
                card.add(str(line.count), line.label)
            found.append(card)

        if report.senders:
            card = _Card("Who wrote", "Anyone who sent more than one.")
            for line in report.senders:
                card.add(str(line.count), line.label)
            found.append(card)

        if report.waiting or report.quiet:
            card = _Card("Still waiting")
            for line in report.waiting:
                card.add("", line.label, urgency=line.urgency)
            if report.quiet:
                card.add("", "Nothing at all from "
                         + ", ".join(report.quiet))
            found.append(card)
        return found

    def _jump(self, row: int):
        if row < 0:
            return None

        def go() -> None:
            self.show_row.emit(row)
            self.accept()
        return go

    def _copy(self) -> None:
        clipboard = QGuiApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(self.report.as_text())



def show(items, parent=None, *, window_start=None, window_end=None,
         mailboxes=(), on_row=None) -> Optional[BriefingDialog]:
    """Build a briefing from rows and put it on screen: the window has no
    business knowing how a briefing is assembled.
    """
    report = _briefing.build(items, window_start=window_start,
                             window_end=window_end, mailboxes=mailboxes)
    dialog = BriefingDialog(report, parent=parent)
    if on_row is not None:
        dialog.show_row.connect(on_row)
    dialog.exec()
    return dialog
