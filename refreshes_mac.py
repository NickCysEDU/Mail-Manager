"""The refreshes of the screen a window is on, as macOS hands them out.

A view on macOS 14 and later makes a display link: an object that calls its
target on the main thread's run loop at each refresh of whichever screen the
view is on, with the moment of that refresh by the screen's own clock. The
visualiser starts a frame there, so each is drawn and handed over with most
of the refresh to spare. ctypes rather than pyobjc, as in touchbar_mac.

Every message sent here is checked once, before anything is made, against
the classes on this Mac: a message a class does not understand raises an
exception the process cannot survive. The target every link calls lives as
long as the process, so a refresh that arrives after its pane has gone
finds nothing rather than freed memory, and stops its link; a link is let
go of with whatever asked for it, too, since a window taken away sends
none.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import weakref
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

_id = ctypes.c_void_p

#: Every message sent, by class; "+" for messages to the class itself.
NEEDED = {
    "NSView": ("displayLinkWithTarget:selector:",),
    "CADisplayLink": ("addToRunLoop:forMode:", "invalidate", "timestamp"),
    "+NSRunLoop": ("mainRunLoop",),
    "NSObject": ("retain", "release", "autorelease", "init"),
    "+NSObject": ("alloc",),
}

_runtime = None
_checked: Optional[bool] = None
_modes: Optional[int] = None
#: The target every link calls, and the Python function behind its one
#: method, made once a process: AppKit holds pointers to both.
_target = None
_REFRESHED = ctypes.CFUNCTYPE(None, _id, _id, _id)
_imp = None
#: Link -> the Refreshes it belongs to.
_links: Dict[int, "weakref.ReferenceType"] = {}
#: The link whose call to its target is under way, if any.
_calling = 0


def _rt():
    global _runtime
    if _runtime is None:
        from touchbar_mac import _Runtime

        _runtime = _Runtime()
    return _runtime


def available() -> bool:
    """Whether the screen's refreshes can be had here: a Mac, Qt on its own
    platform rather than offscreen, and every message this sends answered.
    """
    global _checked
    if _checked is None:
        _checked = False
        from PySide6.QtGui import QGuiApplication

        if (sys.platform == "darwin"
                and QGuiApplication.platformName() == "cocoa"):
            try:
                gaps = _rt().missing(NEEDED)
            except Exception:      # noqa: BLE001 - the timer keeps the frames
                log.exception("The screen's refreshes could not be had")
                gaps = ["the Objective-C runtime"]
            if gaps:
                log.info("The visualiser keeps its own time: this Mac "
                         "lacks %s", ", ".join(gaps[:6]))
            _checked = not gaps
    return _checked


def _common_modes() -> int:
    """The run loop modes a link is added in: all the main thread runs,
    including while a window is resized or a menu is open."""
    global _modes
    if _modes is None:
        foundation = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/Foundation.framework/Foundation")
        _modes = _id.in_dll(foundation, "NSRunLoopCommonModes").value
    return _modes


def _refreshed(_self, _cmd, link) -> None:
    """A refresh, from AppKit: handed to the Refreshes that made the link,
    and a link nobody has any more stopped."""
    global _calling
    _calling = link
    owner = None
    try:
        found = _links.get(link)
        owner = found() if found is not None else None
        if owner is None:
            if found is not None:
                _drop(link)
            else:
                _rt().send(link, "invalidate", restype=None)
            return
        owner._refreshed(link)
    except Exception:      # noqa: BLE001 - nothing may unwind into AppKit
        # Stopped, or it fails sixty times a second; the pane's timer
        # keeps its frames, and asks again.
        log.exception("A refresh of the screen could not be taken")
        try:
            if owner is not None:
                owner.stop()
            else:
                _drop(link)
        except Exception:      # noqa: BLE001
            log.exception("A link to the screen could not be stopped")
    finally:
        _calling = 0


def _make_target() -> int:
    global _target, _imp
    if _target is None:
        rt = _rt()
        name, number = "MMRefreshTarget", 1
        while rt.objc.objc_getClass(name.encode()):
            number += 1
            name = f"MMRefreshTarget{number}"
        made = rt.objc.objc_allocateClassPair(rt.cls("NSObject"),
                                              name.encode(), 0)
        if not made:
            raise RuntimeError("could not make the refresh target's class")
        imp = _REFRESHED(_refreshed)
        if not rt.objc.class_addMethod(made, rt.sel("refreshed:"),
                                       ctypes.cast(imp, _id), b"v@:@"):
            raise RuntimeError("could not add refreshed:")
        rt.objc.objc_registerClassPair(made)
        _imp = imp
        _target = rt.send(rt.send(made, "alloc"), "init")
    return _target


def _drop(link: int) -> None:
    """Stop a link and let go of it, once: at once, or once AppKit has
    finished with it when this is inside the link's own call."""
    if _links.pop(link, None) is None:
        return
    rt = _rt()
    rt.send(link, "invalidate", restype=None)
    rt.send(link, "autorelease" if link == _calling else "release",
            restype=None)


class Refreshes:
    """The refreshes of the screen a widget's window is on, each handed to
    ``refreshed`` with its moment in seconds, by the screen's clock, on the
    main thread. ``refreshed`` is held weakly, so a pane let go of is not
    kept by its link; the next refresh finds it gone and stops."""

    def __init__(self, refreshed: Callable[[float], None]) -> None:
        self._to = weakref.WeakMethod(refreshed)
        self._link = 0
        self._view = 0
        self._let_go = None

    @property
    def running(self) -> bool:
        return bool(self._link)

    def follow(self, widget) -> bool:
        """Take the refreshes of the screen ``widget``'s window is on, from
        the view of that window; those of a window before stop. Whether
        they come."""
        global _checked
        if not available():
            return False
        top = widget.window()
        view = int(top.winId()) if top.windowHandle() is not None else 0
        if self._link and view == self._view:
            return True
        self.stop()
        if not view:
            return False
        rt = _rt()
        try:
            if not rt.objc.class_respondsToSelector(
                    rt.objc.object_getClass(view),
                    rt.sel("displayLinkWithTarget:selector:")):
                return False
            with rt.pool():
                link = rt.send(view, "displayLinkWithTarget:selector:",
                               _make_target(), rt.sel("refreshed:"),
                               argtypes=[_id, _id])
                if not link:
                    return False
                rt.send(link, "retain")
                rt.send(link, "addToRunLoop:forMode:",
                        rt.send(rt.cls("NSRunLoop"), "mainRunLoop"),
                        _common_modes(), restype=None, argtypes=[_id, _id])
        except Exception:      # noqa: BLE001 - the timer keeps the frames
            log.exception("The screen's refreshes could not be had")
            _checked = False
            return False
        self._link, self._view = link, view
        _links[link] = weakref.ref(self)
        # Let go of with this too: a pane taken away with its window gets
        # no more refreshes to find it gone.
        self._let_go = weakref.finalize(self, _drop, link)
        self._let_go.atexit = False
        return True

    def stop(self) -> None:
        let_go, self._let_go = self._let_go, None
        self._link = self._view = 0
        if let_go is not None:
            let_go()

    def _refreshed(self, link: int) -> None:
        to = self._to()
        if to is None or link != self._link:
            if link == self._link:
                self.stop()
            else:
                _drop(link)
            return
        to(_rt().send(link, "timestamp", restype=ctypes.c_double))
