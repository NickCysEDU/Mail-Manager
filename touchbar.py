"""The Touch Bar on a MacBook Pro: each window's own controls, mirrored.

Every item is bound to the Qt control or action it stands for, so the bar
shows what the window shows and a press does what a click does. touchbar_mac
draws the bars on macOS. Elsewhere nothing is drawn, but the items can still
be read and pressed, which is how the tests drive them.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable, Dict, List, Optional, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, QTimer, SignalInstance
from PySide6.QtGui import QAction, QGuiApplication
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QComboBox, QDialog, QDialogButtonBox,
    QMenu, QSlider, QTabWidget, QWidget, QWizard,
)

log = logging.getLogger(__name__)

#: Every item's identifier starts with this, then the bar's name and the
#: item's key. AppKit keeps a customised bar under these, so they stay put.
PREFIX = "com.mailmanager"

#: How long a slider ignores the control it mirrors after being touched, so
#: a knob under a finger is not pulled back by the value it is changing.
HELD_FOR = 0.6

#: How often an active window's bar is checked against its controls, for
#: changes no signal reports, such as a button's text.
POLL_MS = 400

#: What draws the bars, once install() has found AppKit.
_renderer = None
_bars: List["Bar"] = []
_installed = False


def _alive(thing) -> bool:
    """Whether a Qt object is still there to be read."""
    if thing is None:
        return False
    try:
        import shiboken6

        return shiboken6.isValid(thing)
    except (ImportError, TypeError):
        return True


def _read(value):
    """A value or, if it is callable, what it returns now."""
    return value() if callable(value) else value


def plain(text: str) -> str:
    """A control's text as it reads: mnemonics dropped, "&&" back to "&"."""
    out, index = [], 0
    while index < len(text):
        char = text[index]
        if char == "&":
            if text[index + 1:index + 2] == "&":
                out.append("&")
                index += 2
                continue
            index += 1
            continue
        out.append(char)
        index += 1
    text = "".join(out).replace("…", "").replace("...", "").strip()
    # Wizards outside macOS say "< Back" and "Next >".
    return re.sub(r"^<\s+|\s+>$", "", text)


def _shown(widget: QWidget) -> bool:
    """Whether a control would be on screen when its window is."""
    window = widget.window()
    if widget is window:
        return True
    if isinstance(window, QMenu):
        # A menu hides what it holds until it opens, so only what was hidden
        # below the widget it shows counts.
        node = widget
        while node is not None and node.parentWidget() is not window:
            if node.isHidden():
                return False
            node = node.parentWidget()
        return True
    return widget.isVisibleTo(window)


class Item:
    """One thing on a bar.

    ``when`` decides whether it is there at all; ``watch`` lists signals or
    widgets whose changes can alter that. ``priority`` is high, normal,
    lower or low: what AppKit leaves out first when the bar is too narrow,
    low first. Items with ``default`` False are only offered when the bar is
    customised.
    """

    kind = ""

    def __init__(self, key: str, label: str, *, when=None,
                 watch: Sequence = (), image=None, priority: str = "normal",
                 default: bool = True) -> None:
        self.key = key
        self.label = label
        self.when = when
        self.watch = tuple(watch)
        self.image = image
        self.priority = priority
        self.default = default

    def sources(self) -> list:
        """The Qt objects this item reads and drives."""
        return []

    def present(self) -> bool:
        if not all(_alive(source) for source in self.sources()):
            return False
        return self.when is None or bool(self.when())

    def state(self) -> dict:
        return {}

    def act(self, value=None) -> None:
        """Do what pressing it does. ``value`` is what the bar says now."""

    def children(self) -> List["Item"]:
        return []


class Space(Item):
    """A gap: "small", "large" or "flexible"."""

    kind = "space"
    _made = 0

    def __init__(self, size: str = "flexible", **kwargs) -> None:
        Space._made += 1
        super().__init__(f"space-{Space._made}", "", **kwargs)
        self.size = size

    def state(self) -> dict:
        return {"size": self.size}


def _enabled(source) -> bool:
    if isinstance(source, (QAction, QWidget)):
        return source.isEnabled()
    return True


def _visible(source) -> bool:
    if isinstance(source, QAction):
        return source.isVisible()
    if isinstance(source, QWidget):
        return _shown(source)
    return True


class Button(Item):
    """Presses a QAction or a button, or calls a function.

    ``title`` and ``image`` may be callables, for a button whose words
    change. ``role`` is "primary", "confirm", "danger" or "destructive", as
    widgets._paint_button marks them; a button's own mark is used if it has
    one, and a default button counts as primary.
    """

    kind = "button"

    def __init__(self, key: str, label: str, source, *, title=None,
                 role=None, follow: bool = True, **kwargs) -> None:
        super().__init__(key, label, **kwargs)
        self.source = source
        self.title = title
        self.role = role
        #: Whether the item comes and goes with the control it mirrors.
        self.follow = follow

    def sources(self) -> list:
        return [self.source] if isinstance(self.source, QObject) else []

    def present(self) -> bool:
        if not super().present():
            return False
        return not self.follow or _visible(self.source)

    def _role(self) -> Optional[str]:
        if self.role is not None:
            return _read(self.role)
        source = self.source
        if isinstance(source, QAbstractButton):
            marked = source.property("role")
            if marked:
                return str(marked)
            if getattr(source, "isDefault", lambda: False)():
                return "primary"
        return None

    def state(self) -> dict:
        title = _read(self.title)
        if title is None:
            source = self.source
            title = (plain(source.text())
                     if isinstance(source, (QAction, QAbstractButton))
                     else self.label)
        return {"title": title, "image": _read(self.image),
                "role": self._role(), "enabled": _enabled(self.source)}

    def act(self, value=None) -> None:
        # Qt itself ignores a disabled action or button.
        source = self.source
        if isinstance(source, QAction):
            source.trigger()
        elif isinstance(source, QAbstractButton):
            source.click()
        elif callable(source):
            source()


class Toggle(Item):
    """On or off: a checkable QAction or button, such as a tick box."""

    kind = "toggle"

    def __init__(self, key: str, label: str, source, *,
                 follow: bool = True, **kwargs) -> None:
        super().__init__(key, label, **kwargs)
        self.source = source
        self.follow = follow

    def sources(self) -> list:
        return [self.source]

    def present(self) -> bool:
        return super().present() and (not self.follow
                                      or _visible(self.source))

    def state(self) -> dict:
        return {"title": self.label, "on": self.source.isChecked(),
                "image": _read(self.image), "enabled": _enabled(self.source)}

    def act(self, value=None) -> None:
        source = self.source
        wanted = (not source.isChecked()) if value is None else bool(value)
        if wanted == source.isChecked():
            return
        if isinstance(source, QAction):
            source.trigger()
        else:
            # A click rather than setChecked: it is what a person does, and
            # it sends clicked as well as toggled.
            source.click()


class Choice(Item):
    """One of several: a combo box, tabs, a group of exclusive buttons or
    checkable actions, or a function that returns such a group, for a menu
    that is rebuilt.

    ``style`` is how the bar offers it:

        segments  every option side by side
        menu      the current option, opening onto all of them
        list      a strip that scrolls, in the bar itself
        popover   the current option, opening onto a strip that scrolls

    ``short`` maps an option's text to shorter words for the bar. ``named``
    puts the label before the current option on a closed menu or popover.
    """

    kind = "choice"

    def __init__(self, key: str, label: str, source, *,
                 style: str = "segments", short=None, named: bool = False,
                 width: Optional[float] = None, before=None,
                 enabled=None, **kwargs) -> None:
        super().__init__(key, label, **kwargs)
        self.source = source
        self.style = style
        self.short = short or {}
        self.named = named
        self.width = width
        #: Called before a choice is made, as turning the visualiser on
        #: before a scene is chosen.
        self.before = before
        #: Whether it can be used, when that is not the control's own say.
        self.enabled = enabled

    def _group(self):
        """The control, or the buttons or actions, as they are now."""
        source = self.source
        if callable(source) and not isinstance(source, QObject):
            return list(source())
        return source

    def sources(self) -> list:
        source = self.source
        if callable(source) and not isinstance(source, QObject):
            return []
        if isinstance(source, (list, tuple)):
            return list(source)
        return [source]

    def _options(self) -> List[str]:
        group = self._group()
        if isinstance(group, QComboBox):
            texts = [group.itemText(i) for i in range(group.count())]
        elif isinstance(group, QTabWidget):
            texts = [plain(group.tabText(i)) for i in range(group.count())]
        else:
            texts = [plain(member.text()) for member in group]
        return [self.short.get(text, text) for text in texts]

    def _index(self) -> int:
        group = self._group()
        if isinstance(group, (QComboBox, QTabWidget)):
            return group.currentIndex()
        for index, member in enumerate(group):
            if member.isChecked():
                return index
        return -1

    def _enabled(self) -> bool:
        if self.enabled is not None:
            return bool(_read(self.enabled))
        group = self._group()
        if isinstance(group, (QComboBox, QTabWidget)):
            return group.isEnabled()
        return any(member.isEnabled() for member in group)

    def present(self) -> bool:
        if not super().present():
            return False
        group = self._group()
        if isinstance(group, (QComboBox, QTabWidget)):
            return _shown(group)
        return any(_visible(member) for member in group)

    def state(self) -> dict:
        options = self._options()
        index = self._index()
        current = options[index] if 0 <= index < len(options) else ""
        title = f"{self.label}: {current}" if self.named and current else (
            current or self.label)
        state = {"options": options, "index": index, "title": title,
                 "enabled": self._enabled()}
        if self.image is not None:
            state["image"] = _read(self.image)
        return state

    def act(self, value=None) -> None:
        if value is None or not self._enabled():
            return
        index = int(value)
        if not 0 <= index < len(self._options()) or index == self._index():
            return
        if self.before is not None:
            self.before()
        group = self._group()
        if isinstance(group, (QComboBox, QTabWidget)):
            group.setCurrentIndex(index)
        elif isinstance(group[index], QAction):
            group[index].trigger()
        else:
            group[index].click()


class Slider(Item):
    """A QSlider's value, or something a slider drives through ``change``.

    ``settle`` waits that many milliseconds after the last movement before
    acting, for a slider whose every step is expensive, as seeking is.
    ``follow`` False keeps it on the bar while its control is hidden, for a
    control that something else stands in for on screen.
    """

    kind = "slider"

    def __init__(self, key: str, label: str, source: QSlider, *,
                 change: Optional[Callable[[int], None]] = None,
                 settle: int = 0, width: Optional[float] = None,
                 ends: Sequence = (None, None), title=None,
                 follow: bool = True, **kwargs) -> None:
        super().__init__(key, label, **kwargs)
        self.source = source
        self.title = title
        self.change = change
        self.width = width
        self.follow = follow
        #: SF Symbols at the two ends, as a quiet and a loud speaker.
        self.ends = tuple(ends)
        self._held: Optional[tuple] = None
        self._settle: Optional[QTimer] = None
        if settle:
            self._settle = QTimer()
            self._settle.setSingleShot(True)
            self._settle.setInterval(settle)
            self._settle.timeout.connect(self._apply_held)

    def sources(self) -> list:
        return [self.source]

    def present(self) -> bool:
        return super().present() and (not self.follow
                                      or _shown(self.source))

    def state(self) -> dict:
        value = self.source.value()
        if self._held is not None and time.monotonic() - self._held[1] < HELD_FOR:
            value = self._held[0]
        title = _read(self.title) if self.title is not None else self.label
        return {"title": title, "minimum": self.source.minimum(),
                "maximum": self.source.maximum(), "value": value,
                "enabled": self.source.isEnabled()}

    def act(self, value=None) -> bool:
        """Take the value a finger moved the knob to. Returns whether it was
        taken: a disabled control refuses it."""
        if value is None or not self.source.isEnabled():
            return False
        wanted = int(round(max(self.source.minimum(),
                               min(self.source.maximum(), float(value)))))
        self._held = (wanted, time.monotonic())
        if self._settle is not None:
            self._settle.start()
        else:
            self._apply(wanted)
        return True

    def _apply_held(self) -> None:
        if self._held is not None and _alive(self.source):
            self._apply(self._held[0])

    def _apply(self, wanted: int) -> None:
        if self.change is not None:
            self.change(wanted)
        else:
            self.source.setValue(wanted)


class Popover(Item):
    """A button that opens onto more items. A tap opens it; nothing else
    does: a finger held on it and dragged is only a tap."""

    kind = "popover"

    def __init__(self, key: str, label: str, items: Sequence[Item], *,
                 title=None, **kwargs) -> None:
        super().__init__(key, label, **kwargs)
        self.items = list(items)
        self.title = title
        # A popover opened from inside a popover: on the Touch Bar, choosing
        # in it, or only opening it, took the bar back to its top level.
        nested = [item.key for item in self.items
                  if item.kind == "popover" or (
                      item.kind == "choice"
                      and getattr(item, "style", "") in ("menu", "popover"))]
        if nested:
            raise ValueError(f"{key}: a popover cannot open another "
                             f"({', '.join(nested)}); use segments or a list")

    def children(self) -> List[Item]:
        return list(self.items)

    def present(self) -> bool:
        return super().present() and any(item.present()
                                         for item in self.items
                                         if item.kind != "space")

    def state(self) -> dict:
        return {"title": _read(self.title) if self.title is not None
                else self.label, "image": _read(self.image)}


def only_when(items: Sequence[Item], condition: Callable[[], bool],
              watch: Sequence = ()) -> List[Item]:
    """The same items, there only while ``condition`` holds as well."""
    for item in items:
        own = item.when
        item.when = (condition if own is None else
                     (lambda own=own: condition() and own()))
        item.watch = tuple(item.watch) + tuple(watch)
    return list(items)


#: The events on a watched widget that can change what its item shows.
_WIDGET_EVENTS = (QEvent.Type.EnabledChange, QEvent.Type.ShowToParent,
                  QEvent.Type.HideToParent)


class Bar(QObject):
    """The Touch Bar of one window, kept in step with its controls."""

    def __init__(self, window: QWidget, items: Sequence[Item], name: str,
                 customizable: bool = False) -> None:
        super().__init__(window)
        self._window = window
        self.name = name
        self.items = list(items)
        self.customizable = customizable
        self.flat: Dict[str, Item] = {}
        for item in self.items:
            self._index(item)
        self._handle = None
        self._pushed: Dict[str, dict] = {}
        self._arranged = None
        self._pending = QTimer(self)
        self._pending.setSingleShot(True)
        self._pending.setInterval(0)
        self._pending.timeout.connect(self.refresh)
        #: Once a slider stops ignoring its control, a look at it again.
        self._let_go = QTimer(self)
        self._let_go.setSingleShot(True)
        self._let_go.setInterval(int(HELD_FOR * 1000) + 50)
        self._let_go.timeout.connect(self.changed)
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self.changed)
        self._watch()
        window.installEventFilter(self)
        _bars.append(self)
        self.destroyed.connect(_forget)

    def _index(self, item: Item) -> None:
        if item.key in self.flat:
            raise ValueError(f"two items on the {self.name} bar are "
                             f"called {item.key!r}")
        self.flat[item.key] = item
        for child in item.children():
            self._index(child)

    def _watch(self) -> None:
        """Hear about every change that could alter an item."""
        watched = set()
        for item in self.flat.values():
            things = list(item.sources()) + list(item.watch)
            for thing in things:
                if isinstance(thing, SignalInstance):
                    thing.connect(self.changed)
                    continue
                if not isinstance(thing, QObject) or id(thing) in watched:
                    continue
                watched.add(id(thing))
                self._signals_of(thing)
                if isinstance(thing, QWidget):
                    thing.installEventFilter(self)

    def _signals_of(self, thing: QObject) -> None:
        if isinstance(thing, QAction):
            thing.changed.connect(self.changed)
        elif isinstance(thing, QAbstractButton):
            thing.toggled.connect(self.changed)
        elif isinstance(thing, QComboBox):
            thing.currentIndexChanged.connect(self.changed)
            model = thing.model()
            for signal in (model.rowsInserted, model.rowsRemoved,
                           model.dataChanged, model.modelReset,
                           model.layoutChanged):
                signal.connect(self.changed)
        elif isinstance(thing, QSlider):
            thing.valueChanged.connect(self.changed)
            thing.rangeChanged.connect(self.changed)
        elif isinstance(thing, QTabWidget):
            thing.currentChanged.connect(self.changed)

    def arrangement(self) -> Dict[str, List[str]]:
        """The keys shown, at the top and in each popover."""
        def keys(items, top):
            if top and self.customizable:
                return [item.key for item in items if item.default]
            return [item.key for item in items
                    if item.default and item.present()]

        out = {"": keys(self.items, True)}
        for item in self.flat.values():
            if item.kind == "popover":
                out[item.key] = keys(item.items, False)
        return out

    def describe(self) -> List[dict]:
        """What the bar shows now, top level first, for tests and logs."""
        def one(item):
            entry = {"key": item.key, "kind": item.kind,
                     "default": item.default, **item.state()}
            if item.kind == "popover":
                entry["items"] = [one(child) for child in item.items
                                  if child.present()]
            return entry

        return [one(item) for item in self.items if item.present()]

    def press(self, key: str, value=None) -> bool:
        """Act on one item, as touching it does. Returns whether it acted."""
        item = self.flat.get(key)
        if item is None or not item.present():
            return False
        taken = False
        try:
            taken = item.act(value)
        except Exception:      # noqa: BLE001 - a press must never take the app down
            log.exception("Touch Bar item %s.%s failed", self.name, key)
            return False
        finally:
            shown = self._pushed.get(key)
            if item.kind == "slider" and taken and shown is not None:
                # The finger put the knob there and may still be moving it.
                # Sent back, the value arrives after the finger has moved on
                # and pulls the knob to where it was: the knob stutters.
                self._pushed[key] = dict(shown, value=item.state()["value"])
                self._let_go.start()
            else:
                # AppKit has already moved the control under the finger, so
                # its state is sent again even if the press changed nothing.
                self._pushed.pop(key, None)
            self.changed()
        return True

    def changed(self, *_args) -> None:
        """Something an item reads may have changed: look again soon."""
        if _renderer is not None and self._handle is not None:
            self._pending.start()

    def refresh(self) -> None:
        """Send AppKit whatever has changed since last time."""
        if _renderer is None or self._handle is None or not _alive(self._window):
            return
        try:
            arranged = self.arrangement()
            if arranged != self._arranged:
                _renderer.arrange(self._handle, arranged)
                self._arranged = arranged
            for key, item in self.flat.items():
                if item.kind == "space" or not all(
                        _alive(s) for s in item.sources()):
                    continue
                state = item.state()
                if state != self._pushed.get(key):
                    _renderer.update(self._handle, key, state)
                    self._pushed[key] = state
        except Exception:      # noqa: BLE001 - a bar is never worth a crash
            log.exception("Touch Bar %s could not be updated; it is off "
                          "for this window", self.name)
            self._drop()

    def start(self) -> None:
        """Build the bar on AppKit and put it on the window, if both exist."""
        if _renderer is None or not _alive(self._window):
            return
        if self._handle is None:
            try:
                self._handle = _renderer.build(self)
            except Exception:      # noqa: BLE001
                log.exception("Touch Bar %s could not be built", self.name)
                return
            self.destroyed.connect(_releaser(self._handle))
        self.attach()

    def attach(self) -> None:
        if _renderer is None or self._handle is None:
            return
        window = self._window
        if not _alive(window) or window.windowHandle() is None:
            return
        try:
            _renderer.attach(self._handle, window)
        except Exception:      # noqa: BLE001
            log.exception("Touch Bar %s could not be attached", self.name)
            return
        self._pushed.clear()
        self._arranged = None
        self.refresh()

    def _drop(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None and _renderer is not None:
            try:
                _renderer.release(handle)
            except Exception:      # noqa: BLE001
                log.exception("Touch Bar %s could not be released", self.name)

    def eventFilter(self, watched, event) -> bool:      # noqa: N802 - Qt's name
        kind = event.type()
        if watched is self._window:
            if kind in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                if _renderer is not None:
                    QTimer.singleShot(0, self, self.start)
            elif kind == QEvent.Type.WindowActivate:
                if _renderer is not None:
                    self.start()
                    self._poll.start()
            elif kind in (QEvent.Type.WindowDeactivate, QEvent.Type.Hide):
                self._poll.stop()
        if kind in _WIDGET_EVENTS:
            self.changed()
        return False


def _forget(bar) -> None:
    """A bar's window has gone: stop keeping it."""
    _bars[:] = [kept for kept in _bars if _alive(kept) and kept is not bar]


def _releaser(handle):
    """Release a bar's AppKit objects once its window has gone."""
    def release(*_args) -> None:
        if _renderer is not None:
            try:
                _renderer.release(handle)
            except Exception:      # noqa: BLE001
                log.exception("Touch Bar could not be released")
    return release


def give(window: QWidget, items: Sequence[Item], name: str,
         customizable: bool = False) -> Bar:
    """Give ``window`` a Touch Bar of ``items``, replacing any it had."""
    old = of(window)
    if old is not None:
        old._drop()
        window.removeEventFilter(old)
        old.setParent(None)
        old.deleteLater()
        _forget(old)
    bar = Bar(window, items, name, customizable)
    if _renderer is not None and window.isVisible():
        QTimer.singleShot(0, bar, bar.start)
    return bar


def of(window: QWidget) -> Optional[Bar]:
    """The bar a window was given, if any."""
    for bar in _bars:
        if _alive(bar) and bar._window is window:
            return bar
    return None


def refresh(window: QWidget) -> None:
    """Look at a window's controls again, after a change no signal reports."""
    bar = of(window)
    if bar is not None:
        bar.changed()


def _button_items(window: QWidget, controls) -> List[Item]:
    items: List[Item] = [Space("flexible")]
    for index, control in enumerate(controls):
        items.append(Button(f"button-{index}", plain(control.text()) or
                            control.accessibleName(), control,
                            role=_role_of(window, control)))
    return items


def _role_of(window: QWidget, control: QAbstractButton) -> Optional[str]:
    """How a dialog's button looks on the bar: as it is marked, blue if it
    is the default, red if its box says it destroys something."""
    marked = control.property("role")
    if marked:
        return str(marked)
    if getattr(control, "isDefault", lambda: False)():
        return "primary"
    box = control.parentWidget()
    while box is not None and box is not window:
        if isinstance(box, QDialogButtonBox):
            if (box.buttonRole(control)
                    == QDialogButtonBox.ButtonRole.DestructiveRole):
                return "destructive"
            break
        box = box.parentWidget()
    return None


def dialog_items(dialog: QDialog) -> List[Item]:
    """What a dialog with no bar of its own gets: its pages and buttons."""
    items: List[Item] = []
    tabs = [tab for tab in dialog.findChildren(QTabWidget)
            if tab.window() is dialog and not _inside_tabs(tab, dialog)]
    if len(tabs) == 1:
        items.append(Choice("pages", "Pages", tabs[0]))
    return items + button_items(dialog)


def button_items(dialog: QDialog) -> List[Item]:
    """A gap, then a dialog's buttons as it shows them."""
    controls: List[QAbstractButton] = []
    if isinstance(dialog, QWizard):
        order = (QWizard.WizardButton.BackButton,
                 QWizard.WizardButton.CancelButton,
                 QWizard.WizardButton.NextButton,
                 QWizard.WizardButton.CommitButton,
                 QWizard.WizardButton.FinishButton)
        controls = [dialog.button(which) for which in order
                    if dialog.button(which) is not None]
    else:
        for box in dialog.findChildren(QDialogButtonBox):
            if box.window() is dialog:
                controls += box.buttons()
        controls.sort(key=lambda button: button.mapTo(dialog, QPoint(0, 0)).x())
    return _button_items(dialog, controls)


def _inside_tabs(tab: QTabWidget, dialog: QWidget) -> bool:
    parent = tab.parentWidget()
    while parent is not None and parent is not dialog:
        if isinstance(parent, QTabWidget):
            return True
        parent = parent.parentWidget()
    return False


def _focus_changed(qwindow) -> None:
    """A window has come forward: make sure it has its bar."""
    if qwindow is None or _renderer is None:
        return
    widget = QApplication.activeWindow()
    if widget is None or widget.windowHandle() is not qwindow:
        widget = next((w for w in QApplication.topLevelWidgets()
                       if w.windowHandle() is qwindow), None)
    if widget is None:
        return
    bar = of(widget)
    if bar is None and isinstance(widget, QDialog):
        bar = give(widget, dialog_items(widget), "dialog")
    if bar is not None:
        bar.start()


def install(app: Optional[QApplication] = None) -> bool:
    """Draw bars from now on, if this is a Mac. Returns whether it is."""
    global _renderer, _installed
    app = app or QApplication.instance()
    if app is None:
        return False
    if _renderer is None and QGuiApplication.platformName() == "cocoa":
        try:
            import touchbar_mac

            _renderer = touchbar_mac.Renderer()
        except Exception as exc:      # noqa: BLE001 - no bar rather than no app
            log.warning("The Touch Bar is not available: %s", exc)
            _renderer = None
    if _renderer is None:
        return False
    if not _installed:
        app.focusWindowChanged.connect(_focus_changed)
        _installed = True
    for bar in list(_bars):
        if _alive(bar) and bar._window.isVisible():
            bar.start()
    return True


def use(renderer) -> None:
    """Draw with ``renderer`` instead: for tests, or None to stop."""
    global _renderer
    _renderer = renderer
