"""The mailboxes down the left of the main window, listed the way Mail lists
them: the inbox and the mailboxes every account has, each gathering every
account's, then each account's own folders. Choosing one shows its mail in
the table; the window decides what that takes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QFrame, QTreeWidget,
                               QTreeWidgetItem)

import icons

#: What each kind of mailbox is called and drawn as, in the order listed.
KINDS = (("inbox", "Inbox", "inbox"), ("drafts", "Drafts", "draft"),
         ("sent", "Sent", "send"), ("junk", "Junk", "junk"),
         ("trash", "Trash", "trash"), ("archive", "Archive", "archive"))
NAMES = {kind: name for kind, name, _icon in KINDS}


@dataclass(frozen=True)
class Place:
    """What the table shows: one kind of mailbox, of one account or of
    every account (an empty ``account_id``), or one folder of one account.
    """

    kind: str
    account_id: str = ""
    folder: str = ""

    @property
    def is_inbox(self) -> bool:
        return self.kind == "inbox"


INBOX = Place("inbox")

_PLACE = Qt.ItemDataRole.UserRole


class MailboxList(QTreeWidget):
    """The sidebar itself. ``chosen`` carries the Place picked by hand;
    :meth:`choose` moves the selection without saying so."""

    chosen = Signal(object)

    #: Width when nothing has been chosen, and the narrowest it may get.
    WIDTH = 210
    LEAST = 150

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(1)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setIconSize(QSize(16, 16))
        self.setMinimumWidth(self.LEAST)
        self.setAccessibleName("Mailboxes")
        self.setToolTip("Your mailboxes. Choose one to see its mail.")
        #: (id, label, the mailbox sorted) of every account, in order.
        self._accounts: List[tuple] = []
        #: account id -> workers.Mailboxes, as each arrives.
        self._found: Dict[str, object] = {}
        self._place = INBOX
        self.itemSelectionChanged.connect(self._picked)
        self._rebuild()

    def set_accounts(self, accounts) -> None:
        """Every account set up, as Account objects. Forgets the folders of
        any no longer there."""
        accounts = list(accounts)
        self._accounts = [(account.id, heading(account, accounts),
                           account.source_mailbox) for account in accounts]
        known = {account_id for account_id, _label, _source in self._accounts}
        self._found = {key: value for key, value in self._found.items()
                       if key in known}
        self._rebuild()

    def set_mailboxes(self, found) -> None:
        """One account's folders, from a MailboxesWorker."""
        if any(found.account_id == account_id
               for account_id, _label, _source in self._accounts):
            self._found[found.account_id] = found
            self._rebuild()

    def folder_of(self, place: Place, account_id: str) -> Optional[str]:
        """The folder a place means in one account: its inbox, its Sent and
        so on, or the folder named. None where that account has none."""
        if place.kind == "folder":
            return place.folder if place.account_id == account_id else None
        if place.is_inbox:
            for known_id, _label, source in self._accounts:
                if known_id == account_id:
                    return source
            return None
        found = self._found.get(account_id)
        return found.special.get(place.kind) if found is not None else None

    def place(self) -> Place:
        return self._place

    def choose(self, place: Place) -> None:
        """Select ``place`` without announcing it: the window has already
        acted on it. A selection of the place already current is never
        announced (see _picked)."""
        self._place = place
        self._select(place)

    def title_of(self, place: Place) -> str:
        """What a place is called, in the status bar and the log."""
        label = next((label for account_id, label, _source in self._accounts
                      if account_id == place.account_id), "")
        if place.kind == "folder":
            leaf = place.folder
            found = self._found.get(place.account_id)
            if found is not None and found.delimiter:
                leaf = place.folder.rsplit(found.delimiter, 1)[-1]
            return f"{leaf} ({label})" if label else leaf
        name = NAMES.get(place.kind, place.kind.title())
        return f"{name} ({label})" if label and len(self._accounts) > 1 else name

    def _section(self, title: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(self, [title])
        item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        font = QFont(self.font())
        font.setPointSizeF(max(9.0, font.pointSizeF() - 1.5))
        font.setWeight(QFont.Weight.DemiBold)
        item.setFont(0, font)
        item.setForeground(0, self.palette().color(QPalette.ColorRole.PlaceholderText))
        item.setExpanded(True)
        return item

    def _entry(self, parent, title: str, place: Optional[Place],
               glyph: str) -> QTreeWidgetItem:
        item = QTreeWidgetItem(parent, [title])
        item.setIcon(0, icons.icon(glyph, self.palette().color(
            QPalette.ColorRole.Text).name(), 16))
        if place is None:
            item.setFlags(Qt.ItemFlag.ItemIsEnabled)
        else:
            item.setData(0, _PLACE, place)
            item.setToolTip(0, self.title_of(place))
        return item

    def _rebuild(self) -> None:
        self.clear()
        several = len(self._accounts) > 1
        top = self._section("Mailboxes")
        for kind, name, glyph in KINDS:
            having = [(account_id, label)
                      for account_id, label, _source in self._accounts
                      if kind == "inbox" or self.folder_of(Place(kind), account_id)]
            if kind != "inbox" and not having:
                continue
            gathered = self._entry(top, name, Place(kind), glyph)
            if several:
                for account_id, label in having:
                    self._entry(gathered, label, Place(kind, account_id), glyph)
                gathered.setExpanded(kind == "inbox")
        for account_id, label, source in self._accounts:
            found = self._found.get(account_id)
            if found is None:
                continue
            self._folders(self._section(label), account_id, source, found)
        self._select(self._place)

    def _folders(self, section, account_id: str, source: str, found) -> None:
        """Every other folder of one account, nested as the server nests
        them. The inbox and the special ones are listed above."""
        listed_above = {source.lower(), "inbox"} | {
            name.lower() for name in found.special.values()}
        delimiter = found.delimiter or "/"
        made: Dict[str, QTreeWidgetItem] = {}
        openable = set(found.folders)
        for name in sorted(found.folders, key=lambda folder: folder.lower()):
            if name.lower() in listed_above:
                continue
            parts = name.split(delimiter)
            parent = section
            for depth in range(1, len(parts) + 1):
                path = delimiter.join(parts[:depth])
                if path not in made:
                    place = (Place("folder", account_id, path)
                             if path in openable and path.lower() not in listed_above
                             else None)
                    made[path] = self._entry(parent, parts[depth - 1], place, "folder")
                parent = made[path]
        for item in made.values():
            item.setExpanded(True)

    def _select(self, place: Place) -> None:
        for item in self._walk():
            if item.data(0, _PLACE) == place:
                self.setCurrentItem(item)
                return
        if place != INBOX:
            # Gone with an account, or not listed yet: the inbox stands in.
            self._place = INBOX
            self._select(INBOX)

    def _walk(self):
        stack = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        while stack:
            item = stack.pop(0)
            yield item
            stack = [item.child(i) for i in range(item.childCount())] + stack

    def _picked(self) -> None:
        items = self.selectedItems()
        place = items[0].data(0, _PLACE) if items else None
        if place is None or place == self._place:
            return
        self._place = place
        self.chosen.emit(place)

    def places(self) -> List[Place]:
        """Every place listed, top to bottom."""
        return [item.data(0, _PLACE) for item in self._walk()
                if item.data(0, _PLACE) is not None]

    def sizeHint(self) -> QSize:      # noqa: N802 - Qt's name
        return QSize(self.WIDTH, super().sizeHint().height())


def _provider(account):
    """The mail provider an account is with: its preset, else its server."""
    import accounts as hosts

    spec = hosts.host_for(account.preset)
    return spec if spec.name != "custom" else hosts.host_for_server(account.host)


def heading(account, accounts: Sequence) -> str:
    """What an account is called here, as Mail calls it: the name given to
    it, else its provider ("iCloud"), else, where two share a provider or
    it has none known, its address."""
    if account.label and account.label != account.default_label():
        return account.label
    spec = _provider(account)
    if spec.name != "custom" and sum(
            _provider(other).name == spec.name for other in accounts) == 1:
        return spec.label
    return account.address or account.label


def accounts_having(sidebar: MailboxList, place: Place,
                    accounts: Sequence) -> List[tuple]:
    """(account, folder) for every account a place reaches: one for a folder
    or one account's mailbox, every account that has it for a gathered one."""
    chosen = [account for account in accounts
              if not place.account_id or account.id == place.account_id]
    out = []
    for account in chosen:
        folder = sidebar.folder_of(place, account.id)
        if folder:
            out.append((account, folder))
    return out
