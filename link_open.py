"""Links from mail: a list of them, and where each goes before it opens.

A link's words and its address need not agree, and opening one leaves the
app for the browser. So the address is shown, host first, before anything
opens - unless the person has said not to be asked. Only web and mail
addresses are opened at all.
"""

from __future__ import annotations

from typing import Callable, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox,
                               QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QVBoxLayout,
                               QWidget)

#: The kinds of address that are opened. Anything else - a file on this
#: machine, a script - is not.
OPENED = ("http", "https", "mailto")


def parts(url: str) -> Optional[Tuple[str, str]]:
    """Where ``url`` goes, as the site (or the mail address) and the rest
    of it; None for an address that is not opened."""
    try:
        split = urlsplit((url or "").strip())
    except ValueError:
        return None
    scheme = split.scheme.lower()
    if scheme not in OPENED:
        return None
    if scheme == "mailto":
        return (split.path, "") if split.path else None
    host = (split.hostname or "").lower()
    if not host:
        return None
    rest = split.path or ""
    if split.query:
        rest += "?" + split.query
    return host, rest


def shown_host(host: str) -> str:
    """The host as it reads, and as it is spelled where that differs: a
    name in letters from another alphabet can look like a familiar one."""
    try:
        spelled = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return host
    return host if spelled == host else f"{host}  ({spelled})"


def opens(url: str) -> bool:
    """Whether ``url`` is a kind of address that is opened."""
    return parts(url) is not None


def _primary(button) -> None:
    """The window's one main action, drawn as the theme draws it."""
    from widgets import _paint_button

    _paint_button(button, "primary", bold=False)


class LinkWarning(QDialog):
    """Where a link goes, before it is opened."""

    def __init__(self, url: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Open link")
        host, _rest = parts(url) or (url, "")
        mail = url.lower().startswith("mailto:")
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.addWidget(QLabel(
            "Write to this address in your mail app?" if mail
            else "Open this page in your browser?"))
        where = QLabel(shown_host(host) if not mail else host)
        font = where.font()
        font.setPointSizeF(font.pointSizeF() * 1.25)
        font.setBold(True)
        where.setFont(font)
        where.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(where)
        if not mail:
            whole = QLabel(url)
            whole.setWordWrap(True)
            whole.setProperty("dim", "true")
            whole.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            whole.setMaximumWidth(520)
            layout.addWidget(whole)
            note = QLabel("A link can lead somewhere other than its words "
                          "say. This is where this one goes.")
            note.setWordWrap(True)
            note.setProperty("dim", "true")
            note.setMaximumWidth(520)
            layout.addWidget(note)
        self.never = QCheckBox("Don't show this again")
        self.never.setToolTip("Turn it back on in Settings, under "
                              "Appearance.")
        layout.addWidget(self.never)
        buttons = QDialogButtonBox()
        buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        go = buttons.addButton("Open", QDialogButtonBox.ButtonRole.AcceptRole)
        go.setDefault(True)
        _primary(go)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


def open_link(url: str, parent=None, warn: bool = True,
              ask: Optional[Callable] = None) -> Tuple[bool, bool]:
    """Open ``url`` in the browser, first saying where it goes if ``warn``.

    ``(opened, never)``: whether it was opened, and whether the person
    asked not to be told again. An address that is not a web or mail one
    is never opened. ``ask`` stands in for the dialog, for tests.
    """
    if not opens(url):
        return False, False
    never = False
    if warn:
        if ask is not None:
            go, never = ask(url)
        else:
            dialog = LinkWarning(url, parent)
            go = dialog.exec() == QDialog.DialogCode.Accepted
            never = go and dialog.never.isChecked()
        if not go:
            return False, False
    QDesktopServices.openUrl(QUrl(url))
    return True, never


class LinkList(QDialog):
    """Every link in a message, by where it goes."""

    #: A link to open, as its address.
    chosen = Signal(str)

    def __init__(self, links: Sequence[str], subject: str = "",
                 parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Links")
        self._links = [link for link in links if opens(link)]
        layout = QVBoxLayout(self)
        heading = QLabel(subject or "This message")
        heading.setWordWrap(True)
        heading.setProperty("dim", "true")
        layout.addWidget(heading)
        self.list = QListWidget()
        self.list.setUniformItemSizes(False)
        for link in self._links:
            host, rest = parts(link)
            row = QWidget()
            lines = QVBoxLayout(row)
            lines.setContentsMargins(8, 6, 8, 6)
            lines.setSpacing(2)
            name = QLabel(shown_host(host))
            font = name.font()
            font.setBold(True)
            name.setFont(font)
            lines.addWidget(name)
            if rest and rest != "/":
                path = QLabel(rest)
                path.setProperty("dim", "true")
                path.setTextFormat(Qt.TextFormat.PlainText)
                lines.addWidget(path)
            entry = QListWidgetItem()
            entry.setToolTip(link)
            # A little over what the labels ask for: at exactly that, the
            # tails of the letters on the first line were cut off.
            entry.setSizeHint(row.sizeHint() + QSize(0, 6))
            self.list.addItem(entry)
            self.list.setItemWidget(entry, row)
        self.list.itemActivated.connect(lambda _item: self._open())
        layout.addWidget(self.list, 1)
        if not self._links:
            layout.addWidget(QLabel("No links to open."))
        row = QHBoxLayout()
        self.copy_button = QPushButton("Copy link")
        self.copy_button.clicked.connect(self._copy)
        self.open_button = QPushButton("Open…")
        self.open_button.setDefault(True)
        _primary(self.open_button)
        self.open_button.clicked.connect(self._open)
        close = QPushButton("Close")
        close.clicked.connect(self.reject)
        row.addWidget(self.copy_button)
        row.addStretch(1)
        row.addWidget(close)
        row.addWidget(self.open_button)
        layout.addLayout(row)
        if self._links:
            self.list.setCurrentRow(0)
        for button in (self.copy_button, self.open_button):
            button.setEnabled(bool(self._links))
        # As tall as the first several links, and no taller.
        rows = sum(self.list.sizeHintForRow(index)
                   for index in range(min(8, self.list.count())))
        self.list.setMinimumHeight(max(60, rows + 2 * self.list.frameWidth()
                                       + 4))
        self.resize(560, self.sizeHint().height())

    def current(self) -> Optional[str]:
        row = self.list.currentRow()
        return self._links[row] if 0 <= row < len(self._links) else None

    def _open(self) -> None:
        link = self.current()
        if link:
            self.chosen.emit(link)

    def _copy(self) -> None:
        link = self.current()
        if link:
            QGuiApplication.clipboard().setText(link)
