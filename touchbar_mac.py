"""Draws touchbar's bars with AppKit, through the Objective-C runtime.

ctypes rather than pyobjc, as in macname: a dozen classes, and no dependency
to keep universal. Every message sent here is checked once, before anything
is drawn, against the classes on this Mac, because a message a class does not
understand raises an exception the process cannot survive. Every target and
data source is one handler that lives as long as the process, so a press that
arrives after its window has gone finds nothing rather than freed memory.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import logging
import weakref
from typing import Dict, List, Optional

from PySide6.QtCore import QTimer

import touchbar

log = logging.getLogger(__name__)

_id = ctypes.c_void_p
_bool = ctypes.c_bool
_long = ctypes.c_long
_ulong = ctypes.c_ulong
_double = ctypes.c_double
_float = ctypes.c_float


class NSSize(ctypes.Structure):
    _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]


class NSRect(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double),
                ("width", ctypes.c_double), ("height", ctypes.c_double)]


#: How AppKit ranks what to leave out of a bar that is too narrow.
PRIORITY = {"high": 1000.0, "normal": 0.0, "low": -1000.0}

#: The colour behind a button with a role.
BEZEL = {"primary": "systemBlueColor", "confirm": "systemGreenColor",
         "danger": "systemRedColor", "destructive": "systemRedColor"}

#: AppKit's own gaps, by the name touchbar.Space uses.
SPACES = {"small": "NSTouchBarItemIdentifierFixedSpaceSmall",
          "large": "NSTouchBarItemIdentifierFixedSpaceLarge",
          "flexible": "NSTouchBarItemIdentifierFlexibleSpace"}

#: A segmented control's tracking modes.
SELECT_ONE, SELECT_ANY = 0, 1

#: Height of everything on the bar, and the most a scrolling strip may take.
HEIGHT = 30.0
STRIP_MOST = 640.0

#: A slider's width when its item does not say.
SLIDER_WIDTH = 180.0

#: Every message sent, by class; "+" for messages to the class itself.
NEEDED = {
    "+NSApplication": ("sharedApplication",),
    "NSApplication": ("setAutomaticCustomizeTouchBarMenuItemEnabled:",),
    "+NSString": ("stringWithUTF8String:",),
    "NSString": ("UTF8String",),
    "+NSArray": ("arrayWithObjects:count:",),
    "+NSSet": ("setWithArray:",),
    "+NSObject": ("alloc",),
    "NSObject": ("retain", "release", "init"),
    "NSArray": ("count", "objectAtIndex:"),
    "NSView": ("window",),
    "NSWindow": ("setTouchBar:", "touchBar"),
    "NSTouchBar": ("init", "setDefaultItemIdentifiers:", "setTemplateItems:",
                   "setCustomizationIdentifier:",
                   "setCustomizationAllowedItemIdentifiers:",
                   "defaultItemIdentifiers"),
    "+NSButtonTouchBarItem": (
        "buttonTouchBarItemWithIdentifier:title:target:action:",),
    "NSButtonTouchBarItem": ("setTitle:", "setImage:", "setBezelColor:",
                             "setEnabled:", "setTarget:",
                             "setCustomizationLabel:"),
    "NSTouchBarItem": ("setVisibilityPriority:",),
    "NSCustomTouchBarItem": ("initWithIdentifier:", "setView:",
                             "setCustomizationLabel:"),
    "+NSSegmentedControl": (
        "segmentedControlWithLabels:trackingMode:target:action:",),
    "NSSegmentedControl": ("setSegmentCount:", "segmentCount",
                           "setLabel:forSegment:", "setImage:forSegment:",
                           "setSelectedSegment:", "selectedSegment",
                           "setSelected:forSegment:", "isSelectedForSegment:",
                           "setEnabled:", "setTarget:"),
    "NSPopoverTouchBarItem": ("initWithIdentifier:",
                              "setCollapsedRepresentationLabel:",
                              "setCollapsedRepresentationImage:",
                              "setPopoverTouchBar:", "setShowsCloseButton:",
                              "setPressAndHoldTouchBar:",
                              "dismissPopover:", "setCustomizationLabel:"),
    "NSSliderTouchBarItem": ("initWithIdentifier:", "slider", "setTarget:",
                             "setAction:"),
    "NSScrubber": ("initWithFrame:", "registerClass:forItemIdentifier:",
                   "setScrubberLayout:", "setDataSource:", "setDelegate:",
                   "setMode:", "setSelectionBackgroundStyle:",
                   "setSelectedIndex:", "reloadData",
                   "makeItemWithIdentifier:owner:",
                   "setShowsAdditionalContentIndicators:"),
    "NSScrubberFlowLayout": ("init", "setItemSize:", "setItemSpacing:"),
    "NSScrubberTextItemView": ("setTitle:",),
    "+NSScrubberSelectionStyle": ("roundedBackgroundStyle",),
    "NSSlider": ("initWithFrame:", "setMinValue:", "setMaxValue:",
                 "setDoubleValue:", "doubleValue", "setEnabled:",
                 "setContinuous:", "setTarget:", "setAction:",
                 "widthAnchor"),
    "+NSTextField": ("labelWithString:",),
    "NSTextField": ("setStringValue:",),
    "+NSImageView": ("imageViewWithImage:",),
    "+NSStackView": ("stackViewWithViews:",),
    "NSStackView": ("setSpacing:",),
    "NSLayoutDimension": ("constraintEqualToConstant:",),
    "NSLayoutConstraint": ("setActive:",),
    "+NSImage": ("imageWithSystemSymbolName:accessibilityDescription:",),
    "+NSColor": tuple(sorted(set(BEZEL.values()))),
}


def _fit(options) -> float:
    """One width for every entry of a strip: the widest, within reason."""
    from PySide6.QtGui import QFont, QFontMetricsF

    font = QFont()
    font.setPointSizeF(15.0)
    metrics = QFontMetricsF(font)
    widest = max((metrics.horizontalAdvance(text) for text in options),
                 default=60.0)
    return float(max(60.0, min(220.0, widest + 26.0)))


class _Runtime:
    """The Objective-C runtime, AppKit's classes, and a way to message them."""

    def __init__(self) -> None:
        path = ctypes.util.find_library("objc")
        if not path:
            raise RuntimeError("no Objective-C runtime")
        self.objc = ctypes.cdll.LoadLibrary(path)
        self.appkit = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AppKit.framework/AppKit")
        for name, result, arguments in (
                ("objc_getClass", _id, [ctypes.c_char_p]),
                ("sel_registerName", _id, [ctypes.c_char_p]),
                ("object_getClass", _id, [_id]),
                ("class_respondsToSelector", _bool, [_id, _id]),
                ("objc_allocateClassPair", _id, [_id, ctypes.c_char_p,
                                                 ctypes.c_size_t]),
                ("objc_registerClassPair", None, [_id]),
                ("class_addMethod", _bool, [_id, _id, _id, ctypes.c_char_p]),
                ("objc_getProtocol", _id, [ctypes.c_char_p]),
                ("class_addProtocol", _bool, [_id, _id]),
                ("objc_autoreleasePoolPush", _id, []),
                ("objc_autoreleasePoolPop", None, [_id])):
            function = getattr(self.objc, name)
            function.restype = result
            function.argtypes = arguments
        self._send = ctypes.cast(self.objc.objc_msgSend, _id).value
        self._shapes: Dict[tuple, object] = {}
        self._selectors: Dict[str, int] = {}
        self._classes: Dict[str, int] = {}

    def cls(self, name: str) -> int:
        found = self._classes.get(name)
        if found is None:
            found = self.objc.objc_getClass(name.encode()) or 0
            self._classes[name] = found
        return found

    def sel(self, name: str) -> int:
        found = self._selectors.get(name)
        if found is None:
            found = self.objc.sel_registerName(name.encode())
            self._selectors[name] = found
        return found

    def send(self, receiver, selector: str, *args, restype=_id,
             argtypes=()):
        """Message ``receiver``, with the C types the method takes."""
        shape = (restype, tuple(argtypes))
        function = self._shapes.get(shape)
        if function is None:
            function = ctypes.CFUNCTYPE(restype, _id, _id,
                                        *argtypes)(self._send)
            self._shapes[shape] = function
        return function(receiver, self.sel(selector), *args)

    def missing(self) -> List[str]:
        """Every needed message a class on this Mac does not answer."""
        gaps = []
        for name, selectors in NEEDED.items():
            meta = name.startswith("+")
            found = self.cls(name.lstrip("+"))
            if not found:
                gaps.append(name.lstrip("+"))
                continue
            target = self.objc.object_getClass(found) if meta else found
            gaps += [f"{name}{selector}" for selector in selectors
                     if not self.objc.class_respondsToSelector(
                         target, self.sel(selector))]
        for constant in SPACES.values():
            try:
                self.constant(constant)
            except ValueError:
                gaps.append(constant)
        return gaps

    @contextlib.contextmanager
    def pool(self):
        token = self.objc.objc_autoreleasePoolPush()
        try:
            yield
        finally:
            self.objc.objc_autoreleasePoolPop(token)

    def string(self, text: str) -> int:
        return self.send(self.cls("NSString"), "stringWithUTF8String:",
                         text.encode("utf-8"), argtypes=[ctypes.c_char_p])

    def text(self, string) -> str:
        if not string:
            return ""
        raw = self.send(string, "UTF8String")
        return ctypes.string_at(raw).decode("utf-8") if raw else ""

    def array(self, pointers) -> int:
        values = (_id * max(1, len(pointers)))(*pointers)
        return self.send(self.cls("NSArray"), "arrayWithObjects:count:",
                         ctypes.cast(values, _id), len(pointers),
                         argtypes=[_id, _ulong])

    def constant(self, name: str) -> int:
        return _id.in_dll(self.appkit, name).value


#: The handler's class and the Python functions behind its methods, made
#: once a process: AppKit holds pointers to them for as long as it likes.
_HANDLER = None
_IMPS: list = []
#: The renderer the handler reports to.
_current: Optional["Renderer"] = None

_ACT = ctypes.CFUNCTYPE(None, _id, _id, _id)
_COUNT = ctypes.CFUNCTYPE(_long, _id, _id, _id)
_VIEW = ctypes.CFUNCTYPE(_id, _id, _id, _id, _long)
_PICK = ctypes.CFUNCTYPE(None, _id, _id, _id, _long)


def _acted(_self, _cmd, sender) -> None:
    try:
        if _current is not None:
            _current._acted(sender)
    except Exception:      # noqa: BLE001 - nothing may unwind into AppKit
        log.exception("Touch Bar press failed")


def _count(_self, _cmd, scrubber) -> int:
    try:
        return _current._count(scrubber) if _current is not None else 0
    except Exception:      # noqa: BLE001
        log.exception("Touch Bar list failed")
        return 0


def _view(_self, _cmd, scrubber, index):
    try:
        return _current._view(scrubber, index) if _current is not None else None
    except Exception:      # noqa: BLE001
        log.exception("Touch Bar list failed")
        return None


def _picked(_self, _cmd, scrubber, index) -> None:
    try:
        if _current is not None:
            _current._picked(scrubber, index)
    except Exception:      # noqa: BLE001
        log.exception("Touch Bar choice failed")


def _handler(rt: _Runtime) -> int:
    """The handler object, made the first time it is asked for."""
    global _HANDLER
    if _HANDLER is not None:
        return _HANDLER
    name, number = "MMTouchBarHandler", 1
    while rt.objc.objc_getClass(name.encode()):
        number += 1
        name = f"MMTouchBarHandler{number}"
    made = rt.objc.objc_allocateClassPair(rt.cls("NSObject"), name.encode(), 0)
    if not made:
        raise RuntimeError("could not make the handler class")
    for selector, imp, types in (
            ("act:", _ACT(_acted), b"v@:@"),
            ("numberOfItemsForScrubber:", _COUNT(_count), b"q@:@"),
            ("scrubber:viewForItemAtIndex:", _VIEW(_view), b"@@:@q"),
            ("scrubber:didSelectItemAtIndex:", _PICK(_picked), b"v@:@q")):
        _IMPS.append(imp)
        if not rt.objc.class_addMethod(made, rt.sel(selector),
                                       ctypes.cast(imp, _id), types):
            raise RuntimeError(f"could not add {selector}")
    for protocol in (b"NSScrubberDataSource", b"NSScrubberDelegate"):
        found = rt.objc.objc_getProtocol(protocol)
        if found:
            rt.objc.class_addProtocol(made, found)
    rt.objc.objc_registerClassPair(made)
    _HANDLER = rt.send(rt.send(made, "alloc"), "init")
    return _HANDLER


class _Handle:
    """Everything AppKit holds for one bar."""

    def __init__(self, bar) -> None:
        self.bar = weakref.ref(bar)
        self.flat = bar.flat
        self.name = bar.name
        self.touchbar = 0
        #: key -> the item AppKit shows, and -> the control inside it.
        self.items: Dict[str, int] = {}
        self.controls: Dict[str, int] = {}
        #: key -> the popover a choice opens, and -> a popover's own bar.
        self.popovers: Dict[str, int] = {}
        self.nested: Dict[str, int] = {}
        #: key -> (kind, style), for updates.
        self.kinds: Dict[str, tuple] = {}
        #: A scrolling strip's layout, which sets how wide its entries are.
        self.layouts: Dict[int, int] = {}
        #: A slider's name, beside it.
        self.labels: Dict[str, int] = {}
        #: Everything retained here, released together.
        self.owned: List[int] = []
        self.released = False


class Renderer:
    """touchbar's renderer on AppKit."""

    TEXT_ITEM = "mm.text"

    def __init__(self) -> None:
        global _current
        self.rt = _Runtime()
        gaps = self.rt.missing()
        if gaps:
            raise RuntimeError("AppKit here lacks " + ", ".join(gaps[:6]))
        self.handler = _handler(self.rt)
        self._act = self.rt.sel("act:")
        #: A held popover's slider item -> its slider, for a press that
        #: arrives from the item rather than the slider. See _hold.
        self._held: Dict[int, int] = {}
        #: sender -> (handle, key, kind, popover to close); one for lists.
        self._targets: Dict[int, tuple] = {}
        self._lists: Dict[int, list] = {}
        self._images: Dict[str, int] = {}
        self._colours: Dict[str, int] = {}
        app = self.rt.send(self.rt.cls("NSApplication"), "sharedApplication")
        self.rt.send(app, "setAutomaticCustomizeTouchBarMenuItemEnabled:",
                     True, argtypes=[_bool])
        _current = self

    def _keep(self, handle: _Handle, pointer: int, retain: bool = True) -> int:
        if pointer:
            if retain:
                self.rt.send(pointer, "retain")
            handle.owned.append(pointer)
        return pointer

    def _ident(self, handle: _Handle, key: str) -> int:
        return self.rt.string(f"{touchbar.PREFIX}.{handle.name}.{key}")

    def _identifier(self, handle: _Handle, item) -> int:
        if item.kind == "space":
            return self.rt.constant(SPACES.get(item.size, SPACES["flexible"]))
        return self._ident(handle, item.key)

    def _symbol(self, name: Optional[str], description: str = "") -> int:
        if not name:
            return 0
        if name not in self._images:
            image = self.rt.send(
                self.rt.cls("NSImage"),
                "imageWithSystemSymbolName:accessibilityDescription:",
                self.rt.string(name), self.rt.string(description or name),
                argtypes=[_id, _id])
            if image:
                self.rt.send(image, "retain")
            self._images[name] = image or 0
        return self._images[name]

    def _colour(self, role: Optional[str]) -> int:
        name = BEZEL.get(role or "")
        if not name:
            return 0
        if name not in self._colours:
            self._colours[name] = self.rt.send(self.rt.cls("NSColor"), name)
            self.rt.send(self._colours[name], "retain")
        return self._colours[name]

    def _label(self, made: int, item) -> None:
        self.rt.send(made, "setCustomizationLabel:",
                     self.rt.string(item.label or item.key), argtypes=[_id])
        self.rt.send(made, "setVisibilityPriority:",
                     PRIORITY.get(item.priority, 0.0), argtypes=[_float])

    def _custom(self, handle: _Handle, ident: int, view: int) -> int:
        made = self._keep(handle, self.rt.send(
            self.rt.send(self.rt.cls("NSCustomTouchBarItem"), "alloc"),
            "initWithIdentifier:", ident, argtypes=[_id]), retain=False)
        self.rt.send(made, "setView:", view, argtypes=[_id])
        return made

    def _segments(self, handle: _Handle, labels, mode: int) -> int:
        control = self.rt.send(
            self.rt.cls("NSSegmentedControl"),
            "segmentedControlWithLabels:trackingMode:target:action:",
            self.rt.array([self.rt.string(text) for text in labels]), mode,
            self.handler, self._act, argtypes=[_id, _ulong, _id, _id])
        return self._keep(handle, control)

    def _scrubber(self, handle: _Handle, width: float) -> int:
        rt = self.rt
        scrubber = self._keep(handle, rt.send(
            rt.send(rt.cls("NSScrubber"), "alloc"), "initWithFrame:",
            NSRect(0.0, 0.0, width, HEIGHT), argtypes=[NSRect]), retain=False)
        rt.send(scrubber, "registerClass:forItemIdentifier:",
                rt.cls("NSScrubberTextItemView"), rt.string(self.TEXT_ITEM),
                argtypes=[_id, _id])
        layout = self._keep(handle, rt.send(rt.send(
            rt.cls("NSScrubberFlowLayout"), "alloc"), "init"), retain=False)
        rt.send(layout, "setItemSize:", NSSize(110.0, HEIGHT),
                argtypes=[NSSize])
        rt.send(layout, "setItemSpacing:", 6.0, argtypes=[_double])
        rt.send(scrubber, "setScrubberLayout:", layout, argtypes=[_id])
        handle.layouts[scrubber] = layout
        rt.send(scrubber, "setMode:", 1, argtypes=[_long])      # free
        rt.send(scrubber, "setSelectionBackgroundStyle:", rt.send(
            rt.cls("NSScrubberSelectionStyle"), "roundedBackgroundStyle"),
            argtypes=[_id])
        rt.send(scrubber, "setShowsAdditionalContentIndicators:", True,
                argtypes=[_bool])
        rt.send(scrubber, "setDataSource:", self.handler, argtypes=[_id])
        rt.send(scrubber, "setDelegate:", self.handler, argtypes=[_id])
        return scrubber

    def _popover(self, handle: _Handle, ident: int, inner: int,
                 label: str) -> int:
        made = self._keep(handle, self.rt.send(
            self.rt.send(self.rt.cls("NSPopoverTouchBarItem"), "alloc"),
            "initWithIdentifier:", ident, argtypes=[_id]), retain=False)
        self.rt.send(made, "setPopoverTouchBar:", inner, argtypes=[_id])
        self.rt.send(made, "setShowsCloseButton:", True, argtypes=[_bool])
        self.rt.send(made, "setCollapsedRepresentationLabel:",
                     self.rt.string(label), argtypes=[_id])
        return made

    def _new_bar(self, handle: _Handle, made: List[int]) -> int:
        bar = self._keep(handle, self.rt.send(
            self.rt.send(self.rt.cls("NSTouchBar"), "alloc"), "init"),
            retain=False)
        self.rt.send(bar, "setTemplateItems:", self.rt.send(
            self.rt.cls("NSSet"), "setWithArray:", self.rt.array(made),
            argtypes=[_id]), argtypes=[_id])
        return bar

    def build(self, bar) -> _Handle:
        handle = _Handle(bar)
        with self.rt.pool():
            made = [self._make(handle, item) for item in bar.items
                    if item.kind != "space"]
            handle.touchbar = self._new_bar(handle, made)
            if bar.customizable:
                every = [self._identifier(handle, item) for item in bar.items]
                every += [self.rt.constant(name) for name in SPACES.values()]
                self.rt.send(handle.touchbar, "setCustomizationIdentifier:",
                             self.rt.string(f"{touchbar.PREFIX}.{bar.name}"),
                             argtypes=[_id])
                self.rt.send(handle.touchbar,
                             "setCustomizationAllowedItemIdentifiers:",
                             self.rt.array(every), argtypes=[_id])
        return handle

    def _make(self, handle: _Handle, item) -> int:
        rt = self.rt
        ident = self._ident(handle, item.key)
        kind = item.kind
        style = getattr(item, "style", "")
        handle.kinds[item.key] = (kind, style)
        if kind == "button":
            made = self._keep(handle, rt.send(
                rt.cls("NSButtonTouchBarItem"),
                "buttonTouchBarItemWithIdentifier:title:target:action:",
                ident, rt.string(item.label), self.handler, self._act,
                argtypes=[_id, _id, _id, _id]))
            self._targets[made] = (handle, item.key, kind, 0)
        elif kind == "toggle":
            control = self._segments(handle, [item.label], SELECT_ANY)
            made = self._custom(handle, ident, control)
            handle.controls[item.key] = control
            self._targets[control] = (handle, item.key, kind, 0)
        elif kind == "choice":
            popover = 0
            if style in ("segments", "menu"):
                control = self._segments(handle, [], SELECT_ONE)
            else:
                width = item.width or (STRIP_MOST if style == "popover"
                                       else 300.0)
                control = self._scrubber(handle, width)
            handle.controls[item.key] = control
            if style in ("menu", "popover"):
                inner = self._custom(handle, self._ident(
                    handle, item.key + ".options"), control)
                nested = self._new_bar(handle, [inner])
                rt.send(nested, "setDefaultItemIdentifiers:", rt.array(
                    [self._ident(handle, item.key + ".options")]),
                    argtypes=[_id])
                made = popover = self._popover(handle, ident, nested,
                                               item.label)
                handle.popovers[item.key] = popover
            else:
                made = self._custom(handle, ident, control)
            if style in ("segments", "menu"):
                self._targets[control] = (handle, item.key, kind, popover)
            else:
                self._lists[control] = [handle, item.key, [], popover,
                                        handle.layouts[control]]
        elif kind == "slider":
            # A plain slider beside its name, not AppKit's slider item: that
            # one, given a label or a width, logs a complaint about its own
            # layout constraints.
            width = float(item.width or SLIDER_WIDTH)
            slider = self._keep(handle, rt.send(
                rt.send(rt.cls("NSSlider"), "alloc"), "initWithFrame:",
                NSRect(0.0, 0.0, width, HEIGHT), argtypes=[NSRect]),
                retain=False)
            rt.send(rt.send(rt.send(slider, "widthAnchor"),
                            "constraintEqualToConstant:", width,
                            argtypes=[_double]),
                    "setActive:", True, argtypes=[_bool])
            rt.send(slider, "setContinuous:", True, argtypes=[_bool])
            rt.send(slider, "setTarget:", self.handler, argtypes=[_id])
            rt.send(slider, "setAction:", self._act, argtypes=[_id])
            name = rt.send(rt.cls("NSTextField"), "labelWithString:",
                           rt.string(item.label), argtypes=[_id])
            views = [name] if item.label else []
            low, high = (self._symbol(symbol) for symbol in item.ends)
            if low:
                views.append(rt.send(rt.cls("NSImageView"),
                                     "imageViewWithImage:", low,
                                     argtypes=[_id]))
            views.append(slider)
            if high:
                views.append(rt.send(rt.cls("NSImageView"),
                                     "imageViewWithImage:", high,
                                     argtypes=[_id]))
            stack = rt.send(rt.cls("NSStackView"), "stackViewWithViews:",
                            rt.array(views), argtypes=[_id])
            rt.send(stack, "setSpacing:", 8.0, argtypes=[_double])
            made = self._custom(handle, ident, stack)
            handle.controls[item.key] = slider
            handle.labels[item.key] = self._keep(handle, name)
            self._targets[slider] = (handle, item.key, kind, 0)
        elif kind == "popover":
            inside = [self._make(handle, child) for child in item.items
                      if child.kind != "space"]
            nested = self._new_bar(handle, inside)
            handle.nested[item.key] = nested
            made = self._popover(handle, ident, nested, item.label)
            if getattr(item, "hold", None) is not None:
                self._hold(handle, made, item.hold)
        else:
            raise ValueError(f"no such kind of item: {kind}")
        self._label(made, item)
        handle.items[item.key] = made
        return made

    def _hold(self, handle: _Handle, popover: int, hold) -> None:
        """A bar of one slider that opens under a finger held on the popover
        and follows it as it drags, as the brightness control does. AppKit's
        own slider item, which it can hand the touch to; a plain slider in a
        custom item cannot take it.
        """
        rt = self.rt
        ident = self._ident(handle, hold.key)
        made = self._keep(handle, rt.send(
            rt.send(rt.cls("NSSliderTouchBarItem"), "alloc"),
            "initWithIdentifier:", ident, argtypes=[_id]), retain=False)
        slider = rt.send(made, "slider")
        rt.send(slider, "setContinuous:", True, argtypes=[_bool])
        # Both the item and its slider report here: whichever AppKit uses.
        rt.send(made, "setTarget:", self.handler, argtypes=[_id])
        rt.send(made, "setAction:", self._act, argtypes=[_id])
        rt.send(slider, "setTarget:", self.handler, argtypes=[_id])
        rt.send(slider, "setAction:", self._act, argtypes=[_id])
        bar = self._new_bar(handle, [made])
        rt.send(bar, "setDefaultItemIdentifiers:", rt.array([ident]),
                argtypes=[_id])
        rt.send(popover, "setPressAndHoldTouchBar:", bar, argtypes=[_id])
        handle.kinds[hold.key] = ("slider", "")
        handle.items[hold.key] = made
        handle.controls[hold.key] = slider
        # A label nothing shows: the slider item is the slider alone, and
        # an update writes the words it would have.
        handle.labels[hold.key] = self._keep(handle, rt.send(
            rt.cls("NSTextField"), "labelWithString:", rt.string(hold.label),
            argtypes=[_id]))
        self._held[made] = slider
        self._targets[made] = (handle, hold.key, "slider", 0)
        self._targets[slider] = (handle, hold.key, "slider", 0)

    def arrange(self, handle: _Handle, arranged) -> None:
        if handle.released:
            return
        with self.rt.pool():
            for key, keys in arranged.items():
                target = handle.touchbar if key == "" else handle.nested.get(key)
                if not target:
                    continue
                idents = [self._identifier(handle, handle.flat[k])
                          for k in keys]
                self.rt.send(target, "setDefaultItemIdentifiers:",
                             self.rt.array(idents), argtypes=[_id])

    def update(self, handle: _Handle, key: str, state: dict) -> None:
        if handle.released or key not in handle.kinds:
            return
        rt = self.rt
        kind, style = handle.kinds[key]
        made = handle.items[key]
        with rt.pool():
            if kind == "button":
                rt.send(made, "setTitle:", rt.string(state.get("title") or ""),
                        argtypes=[_id])
                rt.send(made, "setImage:", self._symbol(state.get("image")),
                        argtypes=[_id])
                rt.send(made, "setBezelColor:", self._colour(state.get("role")),
                        argtypes=[_id])
                rt.send(made, "setEnabled:", bool(state.get("enabled", True)),
                        argtypes=[_bool])
            elif kind == "toggle":
                control = handle.controls[key]
                rt.send(control, "setLabel:forSegment:",
                        rt.string(state.get("title") or ""), 0,
                        argtypes=[_id, _long])
                rt.send(control, "setImage:forSegment:",
                        self._symbol(state.get("image")), 0,
                        argtypes=[_id, _long])
                rt.send(control, "setSelected:forSegment:",
                        bool(state.get("on")), 0, argtypes=[_bool, _long])
                rt.send(control, "setEnabled:", bool(state.get("enabled", True)),
                        argtypes=[_bool])
            elif kind == "choice":
                self._choose(handle, key, style, state)
            elif kind == "slider":
                slider = handle.controls[key]
                rt.send(handle.labels[key], "setStringValue:",
                        rt.string(state.get("title") or ""), argtypes=[_id])
                rt.send(slider, "setMinValue:", float(state["minimum"]),
                        argtypes=[_double])
                rt.send(slider, "setMaxValue:", float(state["maximum"]),
                        argtypes=[_double])
                rt.send(slider, "setDoubleValue:", float(state["value"]),
                        argtypes=[_double])
                rt.send(slider, "setEnabled:",
                        bool(state.get("enabled", True)), argtypes=[_bool])
            elif kind == "popover":
                rt.send(made, "setCollapsedRepresentationLabel:",
                        rt.string(state.get("title") or ""), argtypes=[_id])
                image = self._symbol(state.get("image"))
                if image:
                    rt.send(made, "setCollapsedRepresentationImage:", image,
                            argtypes=[_id])

    def _choose(self, handle: _Handle, key: str, style: str, state) -> None:
        rt = self.rt
        control = handle.controls[key]
        options = list(state.get("options") or [])
        index = int(state.get("index", -1))
        if style in ("segments", "menu"):
            count = rt.send(control, "segmentCount", restype=_long)
            if count != len(options):
                rt.send(control, "setSegmentCount:", len(options),
                        argtypes=[_long])
            for number, text in enumerate(options):
                rt.send(control, "setLabel:forSegment:", rt.string(text),
                        number, argtypes=[_id, _long])
            rt.send(control, "setSelectedSegment:",
                    index if 0 <= index < len(options) else -1,
                    argtypes=[_long])
            rt.send(control, "setEnabled:", bool(state.get("enabled", True)),
                    argtypes=[_bool])
        else:
            entry = self._lists.get(control)
            if entry is not None and entry[2] != options:
                entry[2] = options
                rt.send(entry[4], "setItemSize:",
                        NSSize(_fit(options), HEIGHT), argtypes=[NSSize])
                rt.send(control, "reloadData")
            rt.send(control, "setSelectedIndex:",
                    index if 0 <= index < len(options) else -1,
                    argtypes=[_long])
        popover = handle.popovers.get(key)
        if popover:
            rt.send(popover, "setCollapsedRepresentationLabel:",
                    rt.string(state.get("title") or ""), argtypes=[_id])

    def attach(self, handle: _Handle, window) -> bool:
        if handle.released:
            return False
        qwindow = window.windowHandle()
        if qwindow is None:
            return False
        view = int(qwindow.winId())
        if not view:
            return False
        nswindow = self.rt.send(view, "window")
        if not nswindow:
            return False
        if self.rt.send(nswindow, "touchBar") != handle.touchbar:
            self.rt.send(nswindow, "setTouchBar:", handle.touchbar,
                         argtypes=[_id])
        return True

    def release(self, handle: _Handle) -> None:
        if handle.released:
            return
        handle.released = True
        rt = self.rt
        with rt.pool():
            for pointer, entry in list(self._targets.items()):
                if entry[0] is handle:
                    rt.send(pointer, "setTarget:", None, argtypes=[_id])
                    del self._targets[pointer]
                    self._held.pop(pointer, None)
            for pointer, entry in list(self._lists.items()):
                if entry[0] is handle:
                    rt.send(pointer, "setDataSource:", None, argtypes=[_id])
                    rt.send(pointer, "setDelegate:", None, argtypes=[_id])
                    del self._lists[pointer]
            for pointer in reversed(handle.owned):
                rt.send(pointer, "release")
        handle.owned.clear()
        handle.items.clear()
        handle.controls.clear()

    def _press(self, handle: _Handle, key: str, value) -> None:
        """Hand a press to the bar once AppKit's call has returned."""
        bar = handle.bar()
        if bar is None or not touchbar._alive(bar):
            return
        QTimer.singleShot(0, bar, lambda: bar.press(key, value))

    def _close(self, popover: int) -> None:
        if popover:
            self.rt.send(popover, "dismissPopover:", None, argtypes=[_id])

    def _acted(self, sender) -> None:
        entry = self._targets.get(sender)
        if entry is None:
            return
        handle, key, kind, popover = entry
        rt = self.rt
        # A held popover's slider item reports as itself; read its slider.
        sender = self._held.get(sender, sender)
        if kind == "toggle":
            value = bool(rt.send(sender, "isSelectedForSegment:", 0,
                                 restype=_bool, argtypes=[_long]))
        elif kind == "choice":
            value = int(rt.send(sender, "selectedSegment", restype=_long))
        elif kind == "slider":
            value = float(rt.send(sender, "doubleValue", restype=_double))
        else:
            value = None
        self._close(popover)
        self._press(handle, key, value)

    def _count(self, scrubber) -> int:
        entry = self._lists.get(scrubber)
        return len(entry[2]) if entry is not None else 0

    def _view(self, scrubber, index):
        entry = self._lists.get(scrubber)
        if entry is None or not 0 <= index < len(entry[2]):
            return None
        view = self.rt.send(scrubber, "makeItemWithIdentifier:owner:",
                            self.rt.string(self.TEXT_ITEM), None,
                            argtypes=[_id, _id])
        if view:
            self.rt.send(view, "setTitle:", self.rt.string(entry[2][index]),
                         argtypes=[_id])
        return view

    def _picked(self, scrubber, index) -> None:
        entry = self._lists.get(scrubber)
        if entry is None:
            return
        handle, key, _options, popover, _layout = entry
        self._close(popover)
        self._press(handle, key, int(index))

    def window_bar(self, window) -> int:
        """The bar AppKit has on a window now."""
        view = int(window.windowHandle().winId())
        return self.rt.send(self.rt.send(view, "window"), "touchBar") or 0

    def identifiers(self, bar_pointer: int) -> List[str]:
        """The identifiers a bar shows, as AppKit has them."""
        rt = self.rt
        array = rt.send(bar_pointer, "defaultItemIdentifiers")
        count = rt.send(array, "count", restype=_ulong) if array else 0
        return [rt.text(rt.send(array, "objectAtIndex:", index,
                                argtypes=[_ulong]))
                for index in range(count)]
