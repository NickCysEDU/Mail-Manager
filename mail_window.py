"""A message in a window of its own, and the window a message is written in.

The message window shows one row of the table as a letter: who, when, the
message as sent, what is attached, and everything that can be done with
it. The compose window is where a reply, a forward or a new message is
written, with the formatting a letter wants and nothing it does not. Both
hand the work - sending, filing, flagging - back to the main window, which
owns the connections.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEvent, QSize, Qt, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QFont, QKeySequence,
                           QTextCharFormat, QTextCursor, QTextListFormat)
from PySide6.QtWidgets import (QComboBox, QCompleter,
                               QFormLayout, QHBoxLayout, QLabel, QMainWindow,
                               QMenu, QMessageBox, QPushButton, QSizePolicy,
                               QTextEdit, QToolBar, QToolButton, QVBoxLayout,
                               QWidget)

import helpmode
import icons
import outgoing
import widgets
from format_bar import DEFAULT_SIZE, FormatBar, tip
from widgets import _html, _paint_button

#: The Touch Bar's picture names, as the buttons' own icons.
_ICONS = {
    "arrowshape.turn.up.left": "reply", "arrowshape.turn.up.left.2": "reply-all",
    "arrowshape.turn.up.right": "forward", "envelope.badge": "unread", "flag": "flag",
    "archivebox": "archive", "xmark.bin": "junk", "trash": "trash",
    "chevron.up": "previous", "chevron.down": "next", "paperplane": "send",
    "tray.and.arrow.down": "draft", "paperclip": "attach",
}


def _ink(widget) -> str:
    """The colour the icons are drawn in: the text's."""
    from PySide6.QtGui import QPalette

    return widget.palette().color(QPalette.ColorRole.WindowText).name()

def _nothing_to_read(attachments) -> str:
    """What to show for a message with no text: what it carries."""
    names = [name for name in (attachments or ()) if name]
    if not names:
        return "This message has no text."
    listed = "\n".join(f"  \u2022 {name}" for name in names)
    return (f"This message has no text. It carries {len(names)} "
            f"attachment{'' if len(names) == 1 else 's'}:\n{listed}")


def _action(parent, text: str, shortcut: str = "", slot=None,
            checkable: bool = False, icon: str = "") -> QAction:
    action = QAction(text, parent)
    if shortcut:
        action.setShortcut(QKeySequence(shortcut))
    if checkable:
        action.setCheckable(True)
    if slot is not None:
        action.triggered.connect(slot)
    if icon:
        action.setData(icon)
        name = _ICONS.get(icon)
        if name:
            action.setIcon(icons.icon(name, _ink(parent)))
    return action


def _app_menus(window, owner) -> None:
    """The menus every window of the app carries on a Mac, where the menu
    bar belongs to the active window: Settings, About and Quit in the app
    menu, and a Window menu with the way back to the main window. Without
    them the main window's bar would stay up, and its shortcuts would fire
    from here."""
    bar = window.menuBar()
    app_menu = bar.addMenu("&File")
    settings = _action(window, "Settings…", "Ctrl+,",
                       lambda: owner.open_settings())
    settings.setMenuRole(QAction.MenuRole.PreferencesRole)
    app_menu.addAction(settings)
    about = _action(window, "About Mail Manager", "", lambda: owner._about())
    about.setMenuRole(QAction.MenuRole.AboutRole)
    app_menu.addAction(about)
    quit_action = _action(window, "Quit", "Ctrl+Q", lambda: owner.quit_app())
    quit_action.setMenuRole(QAction.MenuRole.QuitRole)
    app_menu.addAction(quit_action)
    window_menu = bar.addMenu("&Window")
    window_menu.addAction(_action(window, "Minimize", "", window.showMinimized))
    window_menu.addAction(_action(
        window, "Zoom", "",
        lambda: window.showNormal() if window.isMaximized() else window.showMaximized()))
    window_menu.addSeparator()
    window_menu.addAction(_action(window, "Mail Manager", "Ctrl+0",
                                  lambda: owner._reveal()))
    window.app_menu = app_menu
    window.window_menu = window_menu


class MessageWindow(QMainWindow):
    """One message, read in a window of its own, with everything that can
    be done to it along the top."""

    #: Emitted with the row shown, when the window moves to another row.
    rowChanged = Signal(int)

    def __init__(self, owner, row: int, parent=None) -> None:
        super().__init__(parent)
        from triage_table import message_view

        self._owner = owner
        self._row = row
        #: Which message this is, so a table that changes under the window
        #: (a scan, a sort) is searched for it rather than trusted.
        self._key = None
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        self.heading = QLabel()
        self.heading.setWordWrap(True)
        self.heading.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.who = QLabel()
        self.who.setWordWrap(True)
        self.who.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self.who.setProperty("dim", "true")
        self.view = message_view()
        self.view.set_pictures(bool(owner.settings.show_images))
        self.view.anchorClicked.connect(self._link)
        self.attachments_button = QPushButton("Attachments")
        self.attachments_button.clicked.connect(
            lambda: self._owner._open_attachments(self._row))

        top = QVBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.addWidget(self.heading)
        top.addWidget(self.who)
        foot = QHBoxLayout()
        foot.setContentsMargins(0, 0, 0, 0)
        foot.addWidget(self.attachments_button)
        foot.addStretch(1)
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.addLayout(top)
        layout.addWidget(self.view, 1)
        layout.addLayout(foot)
        self.setCentralWidget(body)
        self._build_actions()
        # Wide enough for its own bar, whatever the font: a larger text
        # setting, or another platform's metrics, would otherwise push the
        # last buttons into the overflow menu.
        self.resize(max(980, self.toolbar.sizeHint().width() + 24), 720)
        self.show_row(row)

    # -- What can be done -------------------------------------------------

    def _build_actions(self) -> None:
        owner = self._owner
        self.reply_action = _action(self, "Reply", "Ctrl+R",
                                    lambda: owner.compose("reply", self._row),
                                    icon="arrowshape.turn.up.left")
        self.reply_all_action = _action(
            self, "Reply All", "Ctrl+Shift+R",
            lambda: owner.compose("reply_all", self._row),
            icon="arrowshape.turn.up.left.2")
        self.forward_action = _action(
            self, "Forward", "Ctrl+Shift+F",
            lambda: owner.compose("forward", self._row),
            icon="arrowshape.turn.up.right")
        self.read_action = _action(self, "Mark as Unread", "Ctrl+Shift+U",
                                   self._toggle_read, icon="envelope.badge")
        self.flag_action = _action(self, "Flag", "Ctrl+Shift+L",
                                   self._toggle_flag, checkable=True,
                                   icon="flag")
        self.archive_action = _action(self, "Archive", "Ctrl+E",
                                      lambda: self._act("archive"),
                                      icon="archivebox")
        self.delete_action = _action(self, "Delete", "Ctrl+Backspace",
                                     lambda: self._act("delete"), icon="trash")
        self.junk_action = _action(self, "Junk", "Ctrl+Shift+J",
                                   lambda: self._act("junk"), icon="xmark.bin")
        self.previous_action = _action(self, "Previous Message", "Ctrl+Up",
                                       lambda: self._step(-1), icon="chevron.up")
        self.next_action = _action(self, "Next Message", "Ctrl+Down",
                                   lambda: self._step(1), icon="chevron.down")
        # The bar says less than the menu, so it fits.
        self.previous_action.setIconText("Previous")
        self.next_action.setIconText("Next")
        self.close_action = _action(self, "Close", "Ctrl+W", self.close)
        self.close_action.setShortcuts([QKeySequence("Ctrl+W"), QKeySequence("Esc")])
        self.move_button = QToolButton()
        self.move_button.setText("Move to")
        self.move_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.move_button.setProperty("menu", "true")
        self.move_menu = QMenu(self.move_button)
        self.move_menu.aboutToShow.connect(self._fill_move_menu)
        self.move_button.setMenu(self.move_menu)

        bar = QToolBar("Message")
        bar.setMovable(False)
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        self.move_button.setIcon(icons.icon("move", _ink(self)))
        self.move_button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        for action in (self.reply_action, self.reply_all_action,
                       self.forward_action):
            bar.addAction(action)
        bar.addSeparator()
        bar.addAction(self.archive_action)
        bar.addWidget(self.move_button)
        bar.addAction(self.junk_action)
        bar.addAction(self.delete_action)
        bar.addSeparator()
        for action in (self.read_action, self.flag_action):
            bar.addAction(action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        bar.addAction(self.previous_action)
        bar.addAction(self.next_action)
        # The buttons are icons: their names come up on a long rest, or at
        # once with help on.
        self.setProperty(helpmode.PATIENT, True)
        self.help_button = helpmode.button_for(owner)
        bar.addWidget(self.help_button)
        self.addToolBar(bar)
        self.toolbar = bar
        for action in (self.close_action,):
            self.addAction(action)
        # Icons alone on the bar, so each says its name and its key when
        # rested on.
        for action, keys, what in (
                (self.reply_action, "Ctrl+R", "Answers the sender."),
                (self.reply_all_action, "Ctrl+Shift+R",
                 "Answers everyone on the message."),
                (self.forward_action, "Ctrl+Shift+F",
                 "Sends it on to somebody else."),
                (self.archive_action, "Ctrl+E", "Files it in Archive."),
                (self.junk_action, "Ctrl+Shift+J", "Moves it to Junk."),
                (self.delete_action, "Ctrl+Backspace", "Moves it to the Bin."),
                (self.flag_action, "Ctrl+Shift+L", "Marks it to come back to."),
                (self.previous_action, "Ctrl+Up", ""),
                (self.next_action, "Ctrl+Down", "")):
            action.setToolTip(tip(action.text(), keys, what))
        self.move_button.setToolTip(tip("Move to", "", "A folder to file it in."))
        self._build_menus()
        self._give_touch_bar()

    def _build_menus(self) -> None:
        _app_menus(self, self._owner)
        menu = self.menuBar().addMenu("&Message")
        for action in (self.reply_action, self.reply_all_action, self.forward_action):
            menu.addAction(action)
        menu.addSeparator()
        for action in (self.read_action, self.flag_action):
            menu.addAction(action)
        menu.addSeparator()
        for action in (self.archive_action, self.junk_action, self.delete_action):
            menu.addAction(action)
        menu.addSeparator()
        for action in (self.previous_action, self.next_action, self.close_action):
            menu.addAction(action)
        self.message_menu = menu

    def _give_touch_bar(self) -> None:
        import touchbar

        buttons = {action: self.toolbar.widgetForAction(action)
                   for action in (self.reply_action, self.reply_all_action,
                                  self.forward_action, self.archive_action,
                                  self.junk_action, self.delete_action,
                                  self.previous_action, self.next_action)}
        if any(widget is None for widget in buttons.values()):
            return
        items = [
            touchbar.Button("reply", "Reply", buttons[self.reply_action],
                            image="arrowshape.turn.up.left", role="primary",
                            priority="high"),
            touchbar.Button("reply-all", "Reply All",
                            buttons[self.reply_all_action],
                            image="arrowshape.turn.up.left.2"),
            touchbar.Button("forward", "Forward", buttons[self.forward_action],
                            image="arrowshape.turn.up.right"),
            touchbar.Space("small"),
            touchbar.Toggle("flag", "Flag", self.toolbar.widgetForAction(self.flag_action),
                            image="flag"),
            touchbar.Button("archive", "Archive", buttons[self.archive_action],
                            image="archivebox"),
            touchbar.Button("junk", "Junk", buttons[self.junk_action],
                            title="", image="xmark.bin", priority="low"),
            touchbar.Button("delete", "Delete", buttons[self.delete_action],
                            image="trash", role="danger"),
            touchbar.Space("flexible"),
            touchbar.Button("previous", "Previous", buttons[self.previous_action],
                            title="", image="chevron.up", priority="low"),
            touchbar.Button("next", "Next", buttons[self.next_action],
                            title="", image="chevron.down", priority="low"),
        ]
        touchbar.give(self, items, "message")

    # -- The message ------------------------------------------------------

    @property
    def row(self) -> int:
        return self._row

    @staticmethod
    def _key_of(item):
        return (item.email.account_id, item.email.uid)

    def same_message(self, item) -> bool:
        return self._key is not None and self._key_of(item) == self._key

    def item(self):
        """The row's item, or the same message wherever the table has it
        now, or None once it is gone."""
        items = self._owner.model.items
        if 0 <= self._row < len(items) and (
                self._key is None or self._key_of(items[self._row]) == self._key):
            return items[self._row]
        for row, found in enumerate(items):
            if self._key_of(found) == self._key:
                self._row = row
                return found
        return None

    def show_row(self, row: int) -> None:
        self._row = row
        self._key = None
        item = self.item()
        if item is None:
            self.close()
            return
        self._key = self._key_of(item)
        message = item.email
        self._owner.read_whole(item)
        self.setWindowTitle(message.subject_display or "(no subject)")
        self.heading.setText(f"<h2 style='margin:0'>{_html(message.subject_display)}</h2>")
        lines = [f"<b>From:</b> {_html(message.sender_display)}"]
        if message.to:
            lines.append(f"<b>To:</b> {_html(message.to)}")
        if getattr(message, "cc", ""):
            lines.append(f"<b>Cc:</b> {_html(message.cc)}")
        lines.append(f"<b>Date:</b> {_html(message.date_full())}")
        if message.account_label:
            lines.append(f"<b>Mailbox:</b> {_html(message.account_label)}")
        self.who.setText("<br>".join(lines))
        names = message.attachments or ()
        missing = self._owner.missing_from(item)
        if message.body_html and (message.body_text.strip()
                                  or "<img" in message.body_html.lower()):
            self.view.show_message(message.body_html, missing)
        elif message.body_text.strip():
            self.view.setPlainText(message.body_text
                                   + (f"\n\n[{missing}]" if missing else ""))
        else:
            # An HTML shell with nothing in it, or no body at all: say so,
            # and what the message carries instead, rather than a blank page.
            self.view.setPlainText(_nothing_to_read(names))
        self.attachments_button.setVisible(bool(names))
        self.attachments_button.setText(
            f"Attachments ({len(names)})" if names else "Attachments")
        flags = {f.lower() for f in (message.flags or ())}
        self.read_action.setText("Mark as Unread" if "\\seen" in flags
                                 else "Mark as Read")
        self.read_action.setToolTip(tip(self.read_action.text(), "Ctrl+Shift+U"))
        self.read_action.setIcon(icons.icon(
            "unread" if "\\seen" in flags else "read", _ink(self)))
        self.flag_action.blockSignals(True)
        self.flag_action.setChecked("\\flagged" in flags)
        self.flag_action.blockSignals(False)
        self.previous_action.setEnabled(self._owner.neighbour_row(row, -1) is not None)
        self.next_action.setEnabled(self._owner.neighbour_row(row, 1) is not None)
        self.rowChanged.emit(row)

    def _step(self, by: int) -> None:
        """The message before or after this one, as the table shows them."""
        wanted = self._owner.neighbour_row(self._row, by)
        if wanted is not None:
            self.show_row(wanted)
            self._owner.select_row(wanted)

    def _toggle_read(self) -> None:
        item = self.item()
        if item is None:
            return
        unread = "\\seen" not in {f.lower() for f in (item.email.flags or ())}
        self._owner.act_on_rows("read" if unread else "unread", [self._row])

    def _toggle_flag(self) -> None:
        self._owner.act_on_rows(
            "flag" if self.flag_action.isChecked() else "unflag", [self._row])

    def _act(self, what: str) -> None:
        """Archive, junk or delete, and move on to the next message as the
        table shows them, or the one before, or close when it was alone."""
        row = self._row
        following = self._owner.neighbour_row(row, 1)
        if following is None:
            following = self._owner.neighbour_row(row, -1)
        self._owner.act_on_rows(what, [row])
        items = self._owner.model.items
        if following is None or not (0 <= following < len(items)):
            self.close()
            return
        self.show_row(following)
        self._owner.select_row(following)

    def _fill_move_menu(self) -> None:
        self.move_menu.clear()
        for folder in self._owner.folder_choices():
            action = self.move_menu.addAction(folder)
            action.triggered.connect(
                lambda checked=False, where=folder: self._owner.act_on_rows(
                    "move", [self._row], where))
        if self.move_menu.isEmpty():
            self.move_menu.addAction("No folders yet").setEnabled(False)

    def _link(self, url: QUrl) -> None:
        self._owner.open_link(url.toString())

    def refresh(self) -> None:
        """The message again, after a flag or a move elsewhere, or a scan
        that put the table back together; closed if it has gone."""
        if self.item() is None:
            self.close()
        else:
            self.show_row(self._row)


class RichEditor(QTextEdit):
    """The letter being written, with the formatting a letter wants."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptRichText(True)
        self.setTabChangesFocus(False)
        font = QFont(self.font())
        font.setPointSizeF(DEFAULT_SIZE)
        self.setFont(font)
        self.document().setDefaultFont(font)

    def _merge(self, fmt: QTextCharFormat) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.WordUnderCursor)
        cursor.mergeCharFormat(fmt)
        self.mergeCurrentCharFormat(fmt)

    def set_bold(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Weight.Bold if on else QFont.Weight.Normal)
        self._merge(fmt)

    def set_italic(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontItalic(on)
        self._merge(fmt)

    def set_underline(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontUnderline(on)
        self._merge(fmt)

    def set_strike(self, on: bool) -> None:
        fmt = QTextCharFormat()
        fmt.setFontStrikeOut(on)
        self._merge(fmt)

    def set_size(self, points: float) -> None:
        fmt = QTextCharFormat()
        fmt.setFontPointSize(points)
        self._merge(fmt)

    def set_colour(self, colour: QColor) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(colour)
        self._merge(fmt)

    def clear_colour(self) -> None:
        """Back to the colour the page gives the text, which is also no
        colour at all in what is sent: a reader's own dark page keeps its
        own text colour."""
        from PySide6.QtGui import QBrush

        fmt = QTextCharFormat()
        fmt.setForeground(QBrush(Qt.BrushStyle.NoBrush))
        self._merge(fmt)

    def current_size(self) -> float:
        return self.currentCharFormat().fontPointSize() or self.default_size()

    def default_size(self) -> float:
        return self.document().defaultFont().pointSizeF() or DEFAULT_SIZE

    def set_list(self, style) -> None:
        """A list of ``style`` on the current paragraph: the same kind again
        takes the list off, another kind changes it."""
        cursor = self.textCursor()
        current = cursor.currentList()
        if current is not None and current.format().style() == style:
            block = cursor.block()
            current.remove(block)
            fmt = block.blockFormat()
            fmt.setIndent(0)
            cursor.setBlockFormat(fmt)
            return
        if current is not None:
            fmt = current.format()
            fmt.setStyle(style)
            current.setFormat(fmt)
            return
        cursor.createList(style)

    def make_list(self, numbered: bool) -> None:
        self.set_list(QTextListFormat.Style.ListDecimal if numbered
                      else QTextListFormat.Style.ListDisc)

    def indent(self, by: int) -> None:
        cursor = self.textCursor()
        fmt = cursor.blockFormat()
        fmt.setIndent(max(0, fmt.indent() + by))
        cursor.setBlockFormat(fmt)

    def align(self, how) -> None:
        self.setAlignment(how)

    def insert_link(self, url: str, words: str = "") -> None:
        cursor = self.textCursor()
        text = words or (cursor.selectedText() if cursor.hasSelection() else url)
        fmt = QTextCharFormat()
        fmt.setAnchor(True)
        fmt.setAnchorHref(url)
        fmt.setForeground(QColor("#2F6FE0"))
        fmt.setFontUnderline(True)
        cursor.insertText(text, fmt)
        plain = QTextCharFormat()
        self.setCurrentCharFormat(plain)

    def insert_picture(self, path: str) -> bool:
        """A picture from disk, carried inside the message."""
        import base64
        import mimetypes

        mime = mimetypes.guess_type(path)[0] or ""
        if not mime.startswith("image/"):
            return False
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return False
        if len(data) > 5 * 1024 * 1024:
            return False
        source = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        self.textCursor().insertHtml(f'<img src="{source}" />')
        return True

    def clear_formatting(self) -> None:
        cursor = self.textCursor()
        if not cursor.hasSelection():
            cursor.select(QTextCursor.SelectionType.Document)
        cursor.setCharFormat(QTextCharFormat())
        self.setCurrentCharFormat(QTextCharFormat())

    def html(self) -> str:
        return self.toHtml()

    def fragment(self) -> str:
        """What is written, as HTML to put inside another document: the body
        of Qt's, without the document around it. Empty when nothing is
        written and no picture is placed."""
        import re

        if not self.toPlainText().strip() and "<img" not in self.toHtml():
            return ""
        whole = self.toHtml()
        found = re.search(r"<body[^>]*>(.*)</body>", whole, re.S | re.I)
        return (found.group(1) if found else whole).strip()

    def text(self) -> str:
        return self.toPlainText()


class ComposeWindow(QMainWindow):
    """Where a message is written: a reply, a forward or a new one."""

    #: Emitted once the message has gone, with the draft that went.
    sent = Signal(object)

    def __init__(self, owner, draft: outgoing.Draft, accounts, account=None,
                 quoted_html: str = "", quoted_text: str = "",
                 answering=None, parent=None) -> None:
        super().__init__(parent)
        self._owner = owner
        self._draft = draft
        self._accounts = list(accounts)
        self._sending = False
        self._saved = False
        self._attachments = list(draft.attachments)
        #: The message this answers - ``(uid, mailbox, account id)`` - so
        #: it can be marked answered once this has gone.
        self.answering = answering
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(900, 700)
        self.setWindowTitle(draft.subject or "New Message")

        from widgets import ClearingLineEdit

        self.from_box = QComboBox()
        for found in self._accounts:
            self.from_box.addItem(found.describe() if hasattr(found, "describe")
                                  else found.address, found.id)
        if account is not None:
            index = self.from_box.findData(account.id)
            if index >= 0:
                self.from_box.setCurrentIndex(index)
        self.to_edit = ClearingLineEdit()
        self.to_edit.setPlaceholderText("name@example.com, another@example.com")
        self.cc_edit = ClearingLineEdit()
        self.bcc_edit = ClearingLineEdit()
        self.subject_edit = ClearingLineEdit()
        self.subject_edit.setPlaceholderText("Subject")
        self.to_edit.setText(", ".join(draft.to))
        self.cc_edit.setText(", ".join(draft.cc))
        self.bcc_edit.setText(", ".join(draft.bcc))
        self.subject_edit.setText(draft.subject)
        known = owner.known_addresses() if hasattr(owner, "known_addresses") else []
        if known:
            for edit in (self.to_edit, self.cc_edit, self.bcc_edit):
                completer = QCompleter(known, edit)
                completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
                completer.setFilterMode(Qt.MatchFlag.MatchContains)
                edit.setCompleter(completer)
        self.subject_edit.textChanged.connect(
            lambda text: self.setWindowTitle(text or "New Message"))

        form = QFormLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        if len(self._accounts) > 1:
            form.addRow("From", self.from_box)
        form.addRow("To", self.to_edit)
        self.cc_label = QLabel("Cc")
        self.bcc_label = QLabel("Bcc")
        form.addRow(self.cc_label, self.cc_edit)
        form.addRow(self.bcc_label, self.bcc_edit)
        form.addRow("Subject", self.subject_edit)
        self.form = form
        self._show_copies(bool(draft.cc or draft.bcc))

        self.editor = RichEditor()
        opening = draft.html or _html(draft.text).replace("\n", "<br>")
        quote = quoted_html or _html(quoted_text).replace("\n", "<br>")
        signature = (owner.signature_html(replying=bool(quote))
                     if hasattr(owner, "signature_html") else "")
        self.editor.setHtml(opening + signature + (f"<br>{quote}" if quote else ""))
        cursor = self.editor.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.Start)
        if not opening and (signature or quote):
            # Two empty lines to write on, above the signature and the
            # quote, the way every mail client opens a reply.
            cursor.insertBlock()
            cursor.insertBlock()
            cursor.movePosition(QTextCursor.MoveOperation.Start)
        self.editor.setTextCursor(cursor)
        self._quoted_text = quoted_text
        #: The message as it opened, so closing one nobody touched - a
        #: reply with its quote and signature, say - asks nothing.
        self._opened = self._state()

        self.attachment_row = QHBoxLayout()
        self.attachment_row.setContentsMargins(0, 0, 0, 0)
        self.attachment_row.setSpacing(6)
        self.attachment_holder = QWidget()
        self.attachment_holder.setLayout(self.attachment_row)
        self._show_attachments()

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.addLayout(form)
        layout.addWidget(self._formatting_bar())
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.attachment_holder)
        self.setCentralWidget(body)
        self._build_actions()
        self.editor.setFocus() if draft.to else self.to_edit.setFocus()

    # -- The bar of formatting ----------------------------------------------

    #: The formatting actions, which the window's menus and Touch Bar share
    #: with the bar.
    FORMAT_ACTIONS = ("bold_action", "italic_action", "underline_action",
                      "strike_action", "colour_action", "bullets_action",
                      "numbers_action", "outdent_action", "indent_action",
                      "left_action", "centre_action", "right_action",
                      "justify_action", "link_action", "picture_action",
                      "plain_action", "grow_action", "shrink_action")

    def _formatting_bar(self) -> QWidget:
        """The formatting controls, laid out as a word processor's (see
        format_bar), in a row that wraps so a narrow window keeps every one
        of them rather than hiding the end of the row."""
        self.formatting = FormatBar(self.editor, self)
        for name in self.FORMAT_ACTIONS:
            setattr(self, name, getattr(self.formatting, name))
        self.size_box = self.formatting.size_box
        return self.formatting

    def format_button(self, action) -> Optional[QToolButton]:
        """The button on the formatting row for one of its actions."""
        return self.formatting.button(action)

    # -- Sending and keeping ------------------------------------------------

    def _build_actions(self) -> None:
        self.send_action = _action(self, "Send", "Ctrl+Return", self.send,
                                   icon="paperplane")
        self.send_action.setShortcuts([QKeySequence("Ctrl+Return"),
                                       QKeySequence("Ctrl+Shift+D")])
        self.draft_action = _action(self, "Save Draft", "Ctrl+S", self.save_draft,
                                    icon="tray.and.arrow.down")
        self.draft_action.setToolTip(tip("Save Draft", "Ctrl+S",
                                         "Keeps it in Drafts to finish later."))
        self.attach_action = _action(self, "Attach", "Ctrl+Shift+A", self.attach,
                                     icon="paperclip")
        self.attach_action.setToolTip(tip("Attach Files", "Ctrl+Shift+A"))
        self.copies_action = _action(self, "Cc/Bcc", "", self._toggle_copies,
                                     checkable=True)
        self.copies_action.setIcon(icons.icon("cc", _ink(self)))
        self.copies_action.setToolTip(tip("Cc/Bcc", "",
                                          "Shows the Cc and Bcc lines."))
        self.copies_action.setChecked(not self.cc_label.isHidden())
        self.close_action = _action(self, "Close", "Ctrl+W", self.close)
        self.addAction(self.close_action)

        bar = QToolBar("Message")
        bar.setMovable(False)
        bar.setIconSize(QSize(18, 18))
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        # Send is a proper button, painted as the one thing to press.
        self.send_button = QPushButton("Send")
        self.send_button.setIcon(icons.icon("send", "#ffffff"))
        self.send_button.setToolTip("Send it (⌘↩).")
        self.send_button.clicked.connect(self.send)
        _paint_button(self.send_button, "primary")
        bar.addWidget(self.send_button)
        bar.addAction(self.draft_action)
        bar.addAction(self.attach_action)
        bar.addAction(self.copies_action)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        bar.addWidget(spacer)
        # The buttons are icons: their names come up on a long rest, or at
        # once with help on.
        self.setProperty(helpmode.PATIENT, True)
        self.help_button = helpmode.button_for(self._owner)
        bar.addWidget(self.help_button)
        self.addToolBar(bar)
        self.toolbar = bar
        self._build_menus()
        self._give_touch_bar()

    def _build_menus(self) -> None:
        _app_menus(self, self._owner)
        bar = self.menuBar()
        message = bar.addMenu("&Message")
        for action in (self.send_action, self.draft_action, self.attach_action,
                       self.copies_action):
            message.addAction(action)
        message.addSeparator()
        message.addAction(self.close_action)
        # Editing keys reach the field being typed in; these are for the
        # menu, and act on whatever has the focus.
        edit = bar.addMenu("&Edit")
        for label, keys, name in (("Undo", "Ctrl+Z", "undo"), ("Redo", "Ctrl+Shift+Z", "redo"),
                                  ("Cut", "Ctrl+X", "cut"), ("Copy", "Ctrl+C", "copy"),
                                  ("Paste", "Ctrl+V", "paste"),
                                  ("Select All", "Ctrl+A", "selectAll")):
            edit.addAction(_action(self, label, keys,
                                   lambda checked=False, n=name: self._edit(n)))
        fmt = bar.addMenu("F&ormat")
        for action in (self.bold_action, self.italic_action, self.underline_action,
                       self.strike_action):
            fmt.addAction(action)
        fmt.addSeparator()
        sizes = fmt.addMenu("Size")
        for action in self.formatting.size_actions():
            sizes.addAction(action)
        fmt.addAction(self.grow_action)
        fmt.addAction(self.shrink_action)
        fmt.addAction(self.colour_action)
        fmt.addSeparator()
        for action in (self.bullets_action, self.numbers_action, self.outdent_action,
                       self.indent_action):
            fmt.addAction(action)
        fmt.addSeparator()
        for action in (self.left_action, self.centre_action, self.right_action,
                       self.justify_action):
            fmt.addAction(action)
        fmt.addSeparator()
        for action in (self.link_action, self.picture_action, self.plain_action):
            fmt.addAction(action)
        self.message_menu, self.edit_menu, self.format_menu = message, edit, fmt

    def _edit(self, name: str) -> None:
        from PySide6.QtWidgets import QApplication

        target = QApplication.focusWidget()
        doing = getattr(target, name, None)
        if callable(doing):
            doing()

    def note(self, text: str, for_ms: int = 0) -> None:
        """A line along the bottom: what is happening to the message."""
        self.statusBar().showMessage(text, for_ms)

    def _give_touch_bar(self) -> None:
        import touchbar

        def widget(action):
            return self.toolbar.widgetForAction(action) or self.format_button(action)

        pieces = {name: widget(action) for name, action in (
            ("attach", self.attach_action),
            ("bold", self.bold_action), ("italic", self.italic_action),
            ("underline", self.underline_action), ("bullets", self.bullets_action),
            ("numbers", self.numbers_action), ("link", self.link_action))}
        if any(piece is None for piece in pieces.values()):
            return
        items = [
            touchbar.Button("send", "Send", self.send_button, image="paperplane",
                            role="primary", priority="high"),
            touchbar.Button("attach", "Attach", pieces["attach"], title="",
                            image="paperclip"),
            touchbar.Space("small"),
            touchbar.Toggle("bold", "Bold", pieces["bold"], image="bold"),
            touchbar.Toggle("italic", "Italic", pieces["italic"], image="italic"),
            touchbar.Toggle("underline", "Underline", pieces["underline"],
                            image="underline"),
            touchbar.Choice("size", "Size", self.size_box, style="popover",
                            named=True),
            touchbar.Button("bullets", "Bullets", pieces["bullets"], title="",
                            image="list.bullet"),
            touchbar.Button("numbers", "Numbers", pieces["numbers"], title="",
                            image="list.number"),
            touchbar.Button("link", "Link", pieces["link"], title="", image="link"),
        ]
        touchbar.give(self, items, "compose")

    def _show_copies(self, shown: bool) -> None:
        for widget in (self.cc_label, self.cc_edit, self.bcc_label, self.bcc_edit):
            widget.setVisible(shown)

    def _toggle_copies(self, on: bool) -> None:
        self._show_copies(on)

    def attach(self) -> None:
        paths, _ = widgets.open_files(self, "Attach files", "")
        self.attach_paths(paths)

    def attach_paths(self, paths) -> None:
        import mimetypes
        from pathlib import Path

        found = []
        for name in paths:
            path = Path(name)
            try:
                data = path.read_bytes()
            except OSError:
                continue
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            found.append(outgoing.Attachment(path.name, mime, data))
        self.add_attachments(found)

    def add_attachments(self, found) -> None:
        """Attachments from elsewhere: what a forwarded message carried."""
        self._attachments.extend(found)
        self._show_attachments()

    def _show_attachments(self) -> None:
        while self.attachment_row.count():
            taken = self.attachment_row.takeAt(0)
            if taken.widget() is not None:
                taken.widget().deleteLater()
        for index, item in enumerate(self._attachments):
            chip = QToolButton()
            chip.setText(f"{item.name}  ×")
            chip.setToolTip(f"{len(item.data) // 1024} KB. Click to take it off.")
            chip.clicked.connect(lambda checked=False, at=index: self._drop_attachment(at))
            self.attachment_row.addWidget(chip)
        self.attachment_row.addStretch(1)
        self.attachment_holder.setVisible(bool(self._attachments))

    def _drop_attachment(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            del self._attachments[index]
            self._show_attachments()

    def account(self):
        wanted = self.from_box.currentData()
        for found in self._accounts:
            if found.id == wanted:
                return found
        return self._accounts[0] if self._accounts else None

    def draft(self) -> outgoing.Draft:
        """What is in the window now, as a message."""
        account = self.account()
        return outgoing.Draft(
            from_address=account.address if account else self._draft.from_address,
            from_name=self._owner.sender_name() if hasattr(self._owner, "sender_name") else "",
            to=outgoing.parse_addresses(self.to_edit.text()),
            cc=outgoing.parse_addresses(self.cc_edit.text()),
            bcc=outgoing.parse_addresses(self.bcc_edit.text()),
            subject=self.subject_edit.text().strip(),
            text=self.editor.text(),
            html=self.editor.html(),
            attachments=list(self._attachments),
            in_reply_to=self._draft.in_reply_to,
            references=self._draft.references)

    def send(self) -> None:
        if self._sending:
            return
        draft = self.draft()
        problems = draft.problems()
        if problems:
            QMessageBox.warning(self, "Not yet", "\n".join(problems))
            return
        self._sending = True
        self.send_action.setEnabled(False)
        self.send_button.setEnabled(False)
        self.note("Sending…")
        self._owner.send_mail(self.account(), draft, self._sent, self._failed,
                              answering=self.answering)

    def _sent(self) -> None:
        self._sending = False
        self._saved = True
        self.sent.emit(self._draft)
        self.close()

    def _failed(self, detail: str) -> None:
        self._sending = False
        self.send_action.setEnabled(True)
        self.send_button.setEnabled(True)
        self.note("")
        QMessageBox.warning(self, "Not sent", detail)

    def save_draft(self) -> None:
        draft = self.draft()
        self.note("Saving the draft…")
        self._owner.save_draft(self.account(), draft,
                               lambda: self._draft_saved(),
                               lambda detail: self._failed(detail))

    def _draft_saved(self) -> None:
        self._saved = True
        self.note("Saved to Drafts.", 4000)

    def _state(self) -> tuple:
        return (self.to_edit.text().strip(), self.cc_edit.text().strip(),
                self.bcc_edit.text().strip(), self.subject_edit.text().strip(),
                self.editor.text().strip(), len(self._attachments))

    def is_dirty(self) -> bool:
        """Whether anything was written since the window opened and has
        not been sent or saved."""
        return self._state() != self._opened and not self._saved

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt's name
        if self._sending:
            event.ignore()
            return
        if self.is_dirty():
            answer = self._ask_to_keep()
            if answer == "cancel":
                event.ignore()
                return
            if answer == "save":
                self.save_draft()
        super().closeEvent(event)

    def _ask_to_keep(self) -> str:
        """Whether a message half written is kept: "save", "discard" or
        "cancel". Nothing pressed is cancel: the message stays."""
        box = QMessageBox(self)
        box.setWindowTitle("Keep this message?")
        box.setText("This message has not been sent.")
        keep = box.addButton("Save Draft", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.exec()
        pressed = box.clickedButton()
        if pressed is keep:
            return "save"
        if pressed is discard:
            return "discard"
        return "cancel"

    def event(self, incoming) -> bool:
        # Dropped files attach themselves.
        if incoming.type() == QEvent.Type.DragEnter:
            if incoming.mimeData().hasUrls():
                incoming.acceptProposedAction()
                return True
        if incoming.type() == QEvent.Type.Drop and incoming.mimeData().hasUrls():
            self.attach_paths([url.toLocalFile() for url in incoming.mimeData().urls()
                               if url.isLocalFile()])
            incoming.acceptProposedAction()
            return True
        return super().event(incoming)
