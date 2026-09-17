"""The briefing, on screen.

A window rather than a panel in the main one, for the same reason a morning
paper is not printed in the margin of a spreadsheet: it is read once, in one
pass, and then closed. It is deliberately not interactive except in the one
way that matters - clicking anything under "Needs you" selects that message in
the table behind, so reading the briefing and acting on it are the same
motion.

Nothing here computes anything. :mod:`briefing` decides what the sections say
and this arranges them, so what the window shows and what "Copy" puts on the
clipboard cannot drift apart - they are the same object rendered twice.
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

#: The marker down the left of a line, and the colour of its text. A
#: marker and coloured words rather than a filled button: eight of these
#: in a row, each a solid block of red or blue, is a warning label rather
#: than a list, and a list that shouts at every line is not ranked.
#:
#: Only the two that mean "somebody is waiting on you" are marked at all.
#: A line the sorter was unsure about is not urgent, it is unfinished.
MARK = {
    Urgency.ACT_NOW: ("●", widgets.ACCENT_RED),
    Urgency.ANSWER: ("●", widgets.ACCENT_BLUE),
    Urgency.UNSURE: ("○", ""),
}


class _Card(QFrame):
    """One section: a heading and the lines under it."""

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
        self._column.addLayout(self._grid)
        self._rows = 0

    def add(self, count: str, text: str, folder: str = "",
            urgency: int = Urgency.NOTE, tooltip: str = "",
            on_click=None, dim: bool = False) -> None:
        number = QLabel(count)
        number.setAlignment(Qt.AlignmentFlag.AlignRight
                            | Qt.AlignmentFlag.AlignVCenter)
        number.setProperty("dim", "true")
        number.setFixedWidth(34)

        marker, colour = MARK.get(urgency, ("", ""))
        if marker and not count:
            number.setText(marker)
            if colour:
                # Colour only. A stylesheet that also carried padding or a
                # radius would make this line a different height from the
                # ones around it, which is how a list stops being a list.
                number.setStyleSheet(f"color: {colour};")

        body: QWidget
        if on_click is not None:
            button = QPushButton(text)
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            # A link, not a button. setFlat only drops the raised look;
            # the theme still draws a border, and eight bordered boxes
            # stacked up read as a form to fill in rather than a list to
            # read.
            rule = ("text-align: left; padding: 1px 2px; background: none; "
                    "border: none;")
            if colour:
                rule += f" color: {colour};"
            button.setStyleSheet(rule)
            button.clicked.connect(on_click)
            body = button
        else:
            label = QLabel(text)
            label.setWordWrap(True)
            if dim:
                label.setProperty("dim", "true")
            body = label
        body.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Preferred)
        if tooltip:
            body.setToolTip(tooltip)

        self._grid.addWidget(number, self._rows, 0)
        self._grid.addWidget(body, self._rows, 1)
        if folder:
            where = QLabel(folder)
            where.setProperty("dim", "true")
            where.setAlignment(Qt.AlignmentFlag.AlignRight
                               | Qt.AlignmentFlag.AlignVCenter)
            where.setToolTip(f"Bound for {folder}")
            self._grid.addWidget(where, self._rows, 2)
        self._rows += 1


class BriefingDialog(QDialog):
    """What the last scan found, in the order somebody wants to be told it."""

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
        column.setContentsMargins(0, 0, 0, 0)
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
    """Build a briefing from rows and put it on screen.

    One call, because the window has no business knowing how a briefing is
    assembled - only that it wants one for these rows.
    """
    report = _briefing.build(items, window_start=window_start,
                             window_end=window_end, mailboxes=mailboxes)
    dialog = BriefingDialog(report, parent=parent)
    if on_row is not None:
        dialog.show_row.connect(on_row)
    dialog.exec()
    return dialog
