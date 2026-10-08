"""The formatting bar over a rich editor, laid out the way a word processor
lays out the top of its window: the type first (size, bigger and smaller,
bold, italic, underline, strike, colour), then the paragraph (lists,
indents, alignment), then what can be put in (a link, a picture), and the
eraser last. Every control is an icon with its name and its key in the
tooltip; the lists and the colour are split buttons, pressed for the usual
and opened for the choice; the alignment is one button that shows the
current one and opens onto the rest. The row wraps rather than hiding its
end.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import (QAction, QActionGroup, QColor, QIntValidator,
                           QKeySequence, QPixmap, QTextListFormat)
from PySide6.QtWidgets import (QColorDialog, QComboBox, QFrame,
                               QInputDialog, QMenu, QMessageBox, QToolButton,
                               QWidget)

import icons
import widgets
from flowlayout import FlowHolder, FlowLayout

#: The sizes offered, in points; any whole number can be typed as well.
SIZES = (8, 9, 10, 11, 12, 13, 14, 16, 18, 20, 22, 24, 28, 32, 36, 48, 72)
#: What a message is written in until a size is chosen.
DEFAULT_SIZE = 13.0
#: The most that can be typed.
LARGEST = 300

#: The colours the palette offers, as a word processor's row of them, and
#: the one the colour button applies until another is picked.
COLOURS = (
    ("Black", "#000000"), ("Dark grey", "#404040"), ("Grey", "#808080"),
    ("Red", "#C0392B"), ("Orange", "#E67E22"), ("Yellow", "#F1C40F"),
    ("Green", "#27AE60"), ("Teal", "#16A085"), ("Blue", "#2F6FE0"),
    ("Purple", "#8E44AD"), ("Pink", "#E84393"), ("White", "#FFFFFF"),
)
FIRST_COLOUR = "#C0392B"

#: The kinds of list, with the mark the menu shows for each.
BULLET_STYLES = (("Disc", "•", QTextListFormat.Style.ListDisc),
                 ("Circle", "◦", QTextListFormat.Style.ListCircle),
                 ("Square", "▪", QTextListFormat.Style.ListSquare))
NUMBER_STYLES = (("1. 2. 3.", QTextListFormat.Style.ListDecimal),
                 ("a. b. c.", QTextListFormat.Style.ListLowerAlpha),
                 ("A. B. C.", QTextListFormat.Style.ListUpperAlpha),
                 ("i. ii. iii.", QTextListFormat.Style.ListLowerRoman),
                 ("I. II. III.", QTextListFormat.Style.ListUpperRoman))

#: The alignments, with the icon and the key for each.
ALIGNMENTS = (("Align Left", "align-left", "Ctrl+{",
               Qt.AlignmentFlag.AlignLeft),
              ("Centre", "align-centre", "Ctrl+|",
               Qt.AlignmentFlag.AlignCenter),
              ("Align Right", "align-right", "Ctrl+}",
               Qt.AlignmentFlag.AlignRight),
              ("Justify", "align-justify", "",
               Qt.AlignmentFlag.AlignJustify))

#: The icons' size on the bar.
ICON = 18


def ink_of(widget: QWidget) -> str:
    """The colour the icons are drawn in: the text's."""
    from PySide6.QtGui import QPalette

    return widget.palette().color(QPalette.ColorRole.WindowText).name()


def keys_of(shortcut: str) -> str:
    """The key the way the menu shows it (a Mac shows the symbols)."""
    if not shortcut:
        return ""
    return QKeySequence(shortcut).toString(QKeySequence.SequenceFormat.NativeText)


def tip(text: str, shortcut: str = "", what: str = "") -> str:
    """A tooltip: the name, the key in brackets, and a few words."""
    keys = keys_of(shortcut)
    head = f"{text} ({keys})" if keys else text
    return f"{head}. {what}" if what else head


def size_text(points: float) -> str:
    """A size as the box shows it: whole numbers without a point."""
    return str(int(points)) if float(points).is_integer() else f"{points:g}"


class _Divider(QFrame):
    """A thin line between groups of controls, as a ribbon has."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.VLine)
        self.setFrameShadow(QFrame.Shadow.Plain)
        self.setProperty("dim", "true")
        self.setFixedWidth(9)
        self.setMinimumHeight(ICON + 6)


class FormatBar(QWidget):
    """The controls, over ``editor`` (a RichEditor). The actions are public,
    so a window can put them in its menus and on its Touch Bar as well.
    """

    def __init__(self, editor, parent=None) -> None:
        super().__init__(parent)
        self.editor = editor
        self._buttons: Dict[QAction, QToolButton] = {}
        self._size_actions: List[QAction] = []
        self._colour = QColor(FIRST_COLOUR)
        self._ink = ink_of(self)
        self._syncing = False
        self._build_actions()
        self._build_row()
        editor.currentCharFormatChanged.connect(self._format_changed)
        editor.cursorPositionChanged.connect(self._cursor_moved)
        self._cursor_moved()

    # -- What there is ------------------------------------------------------

    def _action(self, text: str, shortcut: str, slot, icon: str,
                checkable: bool = False, what: str = "") -> QAction:
        action = QAction(text, self)
        if shortcut:
            action.setShortcut(QKeySequence(shortcut))
        action.setCheckable(checkable)
        action.triggered.connect(slot)
        action.setIcon(icons.icon(icon, self._ink, ICON))
        action.setData(icon)
        action.setToolTip(tip(text, shortcut, what))
        return action

    def _build_actions(self) -> None:
        editor = self.editor
        self.grow_action = self._action(
            "Bigger", "Ctrl+Shift+.", lambda: self.grow(1), "grow",
            what="One size up.")
        self.shrink_action = self._action(
            "Smaller", "Ctrl+Shift+,", lambda: self.grow(-1), "shrink",
            what="One size down.")
        self.bold_action = self._action("Bold", "Ctrl+B", editor.set_bold,
                                        "bold", checkable=True)
        self.italic_action = self._action("Italic", "Ctrl+I",
                                          editor.set_italic, "italic",
                                          checkable=True)
        self.underline_action = self._action(
            "Underline", "Ctrl+U", editor.set_underline, "underline",
            checkable=True)
        self.strike_action = self._action(
            "Strikethrough", "Ctrl+Shift+X", editor.set_strike,
            "strikethrough", checkable=True)
        self.colour_action = self._action(
            "Text Colour", "", self._apply_colour, "text-colour",
            what="The colour shown; open the menu for another.")
        self.bullets_action = self._action(
            "Bullets", "Ctrl+Shift+8",
            lambda: editor.set_list(QTextListFormat.Style.ListDisc),
            "bullets", checkable=True,
            what="A bulleted list, or back to plain text; open the menu "
                 "for the kind of bullet.")
        self.numbers_action = self._action(
            "Numbering", "Ctrl+Shift+7",
            lambda: editor.set_list(QTextListFormat.Style.ListDecimal),
            "numbers", checkable=True,
            what="A numbered list, or back to plain text; open the menu "
                 "for letters or Roman numerals.")
        self.outdent_action = self._action(
            "Decrease Indent", "Ctrl+[", lambda: editor.indent(-1), "outdent")
        self.indent_action = self._action(
            "Increase Indent", "Ctrl+]", lambda: editor.indent(1), "indent")
        self.align_actions: List[QAction] = []
        self._align_group = QActionGroup(self)
        self._align_group.setExclusive(True)
        for text, icon, keys, how in ALIGNMENTS:
            action = self._action(text, keys,
                                  lambda checked=False, h=how: editor.align(h),
                                  icon, checkable=True)
            self._align_group.addAction(action)
            self.align_actions.append(action)
        (self.left_action, self.centre_action, self.right_action,
         self.justify_action) = self.align_actions
        self.left_action.setChecked(True)
        self.link_action = self._action("Link", "Ctrl+K", self.add_link, "link",
                                        what="Make the selected words a link.")
        self.picture_action = self._action(
            "Picture", "", self.add_picture, "picture",
            what="Put a picture from a file into the message.")
        self.plain_action = self._action(
            "Clear Formatting", "Ctrl+\\", editor.clear_formatting,
            "clear-format", what="Back to plain text.")
        for action, weight in ((self.bold_action, "bold"),
                               (self.italic_action, "italic"),
                               (self.underline_action, "underline"),
                               (self.strike_action, "strike")):
            # The menu entries wear their own formatting.
            font = action.font()
            if weight == "bold":
                font.setBold(True)
            elif weight == "italic":
                font.setItalic(True)
            elif weight == "underline":
                font.setUnderline(True)
            else:
                font.setStrikeOut(True)
            action.setFont(font)

        # The sizes, as actions too, for a menu.
        for points in SIZES:
            action = QAction(size_text(points), self)
            action.triggered.connect(
                lambda checked=False, p=float(points): self.set_size(p))
            self._size_actions.append(action)

    def size_actions(self) -> List[QAction]:
        """One action a size, for a Size menu."""
        return list(self._size_actions)

    # -- The row --------------------------------------------------------------

    def _button(self, action: QAction, menu: Optional[QMenu] = None,
                split: bool = False) -> QToolButton:
        button = QToolButton()
        button.setDefaultAction(action)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonIconOnly)
        button.setIconSize(QSize(ICON, ICON))
        button.setProperty("segment", "true")
        if menu is not None:
            button.setMenu(menu)
            if split:
                button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                button.setProperty("split", "true")
            else:
                button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
                button.setProperty("menu", "true")
        self._buttons[action] = button
        return button

    def _build_row(self) -> None:
        row = FlowLayout(margin=0, spacing=3, vertical_spacing=4)

        self.size_box = QComboBox()
        self.size_box.setEditable(True)
        self.size_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.size_box.setValidator(QIntValidator(1, LARGEST, self.size_box))
        for points in SIZES:
            self.size_box.addItem(size_text(points), float(points))
        self.size_box.setCurrentIndex(SIZES.index(int(DEFAULT_SIZE)))
        self.size_box.setToolTip(tip("Text Size", "",
                                     "In points; pick one or type one."))
        self.size_box.setMinimumContentsLength(3)
        self.size_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.size_box.setMaximumWidth(84)
        self.size_box.activated.connect(self._size_picked)
        # The Touch Bar changes the box by index, without activating it.
        self.size_box.currentIndexChanged.connect(self._size_changed)
        self.size_box.lineEdit().returnPressed.connect(self._size_typed)
        row.addWidget(self.size_box)
        row.addWidget(self._button(self.grow_action))
        row.addWidget(self._button(self.shrink_action))
        row.addWidget(_Divider())
        for action in (self.bold_action, self.italic_action,
                       self.underline_action, self.strike_action):
            row.addWidget(self._button(action))
        row.addWidget(self._button(self.colour_action, self._colour_menu(),
                                   split=True))
        row.addWidget(_Divider())
        row.addWidget(self._button(self.bullets_action, self._bullets_menu(),
                                   split=True))
        row.addWidget(self._button(self.numbers_action, self._numbers_menu(),
                                   split=True))
        row.addWidget(self._button(self.outdent_action))
        row.addWidget(self._button(self.indent_action))
        self.align_action = QAction("Alignment", self)
        self.align_action.setIcon(self.left_action.icon())
        self.align_action.setToolTip(tip("Alignment", "",
                                         "Left, centred, right or justified."))
        align_menu = QMenu(self)
        for action in self.align_actions:
            align_menu.addAction(action)
        self.align_button = self._button(self.align_action, align_menu)
        row.addWidget(self.align_button)
        row.addWidget(_Divider())
        row.addWidget(self._button(self.link_action))
        row.addWidget(self._button(self.picture_action))
        row.addWidget(_Divider())
        row.addWidget(self._button(self.plain_action))
        self.holder = FlowHolder(row)
        from PySide6.QtWidgets import QVBoxLayout

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.holder)
        self._paint_colour()

    def button(self, action: QAction) -> Optional[QToolButton]:
        """The button on the row for one of the actions."""
        return self._buttons.get(action)

    def _bullets_menu(self) -> QMenu:
        menu = QMenu(self)
        for name, mark, style in BULLET_STYLES:
            menu.addAction(f"{mark}  {name}").triggered.connect(
                lambda checked=False, s=style: self.editor.set_list(s))
        return menu

    def _numbers_menu(self) -> QMenu:
        menu = QMenu(self)
        for name, style in NUMBER_STYLES:
            menu.addAction(name).triggered.connect(
                lambda checked=False, s=style: self.editor.set_list(s))
        return menu

    def _colour_menu(self) -> QMenu:
        menu = QMenu(self)
        for name, value in COLOURS:
            action = menu.addAction(_swatch(value), name)
            action.triggered.connect(
                lambda checked=False, v=value: self.pick_colour(QColor(v)))
        menu.addSeparator()
        menu.addAction("Automatic").triggered.connect(self.editor.clear_colour)
        menu.addAction("More Colours…").triggered.connect(self._more_colours)
        return menu

    # -- What the controls do -------------------------------------------------

    def pick_colour(self, colour: QColor) -> None:
        """Apply ``colour`` and keep it as the one the button applies."""
        if not colour.isValid():
            return
        self._colour = QColor(colour)
        self._paint_colour()
        self.editor.set_colour(self._colour)

    def _apply_colour(self) -> None:
        self.editor.set_colour(self._colour)

    def _more_colours(self) -> None:
        colour = QColorDialog.getColor(self._colour, self.window(), "Text colour")
        if colour.isValid():
            self.pick_colour(colour)

    def _paint_colour(self) -> None:
        self.colour_action.setIcon(icons.icon("text-colour", self._ink, ICON,
                                              second=self._colour.name()))

    @property
    def colour(self) -> QColor:
        return QColor(self._colour)

    def set_size(self, points: float) -> None:
        points = max(1.0, min(float(LARGEST), float(points)))
        self.editor.set_size(points)
        self._show_size(points)
        self.editor.setFocus()

    def _size_picked(self, index: int) -> None:
        if 0 <= index < self.size_box.count():
            self.set_size(float(self.size_box.itemData(index)))

    def _size_changed(self, index: int) -> None:
        if not self._syncing:
            self._size_picked(index)

    def _size_typed(self) -> None:
        text = self.size_box.currentText().strip()
        try:
            self.set_size(float(text))
        except ValueError:
            self._show_size(self.editor.current_size())

    def _show_size(self, points: float) -> None:
        self._syncing = True
        try:
            at = self.size_box.findData(float(points))
            if at >= 0:
                self.size_box.setCurrentIndex(at)
            else:
                self.size_box.setCurrentIndex(-1)
            self.size_box.setEditText(size_text(points))
        finally:
            self._syncing = False

    def grow(self, by: int) -> None:
        """One size up or down the ladder from the current one."""
        now = self.editor.current_size()
        if by > 0:
            bigger = [s for s in SIZES if s > now + 0.01]
            self.set_size(bigger[0] if bigger else min(LARGEST, now + 2))
        else:
            smaller = [s for s in SIZES if s < now - 0.01]
            self.set_size(smaller[-1] if smaller else max(1.0, now - 1))

    def add_link(self) -> None:
        url, ok = QInputDialog.getText(self.window(), "Link", "Address:",
                                       text="https://")
        if ok and url.strip() and url.strip() != "https://":
            self.editor.insert_link(url.strip())

    def add_picture(self) -> None:
        path, _ = widgets.open_file(
            self.window(), "Choose a picture", "",
            "Pictures (*.png *.jpg *.jpeg *.gif *.webp)")
        if path and not self.editor.insert_picture(path):
            QMessageBox.information(
                self.window(), "Picture",
                "That picture could not be read, or is over 5 MB.")

    # -- Following the cursor -------------------------------------------------

    def _format_changed(self, fmt) -> None:
        from PySide6.QtGui import QFont

        for action, on in ((self.bold_action, fmt.fontWeight() >= QFont.Weight.Bold),
                           (self.italic_action, fmt.fontItalic()),
                           (self.underline_action, fmt.fontUnderline()),
                           (self.strike_action, fmt.fontStrikeOut())):
            action.blockSignals(True)
            action.setChecked(bool(on))
            action.blockSignals(False)
        self._show_size(fmt.fontPointSize() or self.editor.default_size())
        self._paragraph_changed()

    def _cursor_moved(self) -> None:
        self._format_changed(self.editor.currentCharFormat())

    def _paragraph_changed(self) -> None:
        cursor = self.editor.textCursor()
        listed = cursor.currentList()
        style = listed.format().style() if listed is not None else None
        bullets = style in {s for _n, _m, s in BULLET_STYLES}
        numbers = style in {s for _n, s in NUMBER_STYLES}
        for action, on in ((self.bullets_action, bullets),
                           (self.numbers_action, numbers)):
            action.blockSignals(True)
            action.setChecked(on)
            action.blockSignals(False)
        how = self.editor.alignment() & Qt.AlignmentFlag.AlignHorizontal_Mask
        for action, (_t, _i, _k, flag) in zip(self.align_actions, ALIGNMENTS):
            if how == flag:
                action.blockSignals(True)
                action.setChecked(True)
                action.blockSignals(False)
                self.align_action.setIcon(action.icon())
                break


def _swatch(value: str) -> "QPixmap":
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor(value))
    return pixmap
