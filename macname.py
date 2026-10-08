"""Tell macOS what to call this process when it is not in an app bundle.

Running the app straight from a checkout puts "Python" in the menu bar and in
Force Quit, because macOS names a process after the bundle it came from and an
unbundled script came from the interpreter's. The built app carries its own
Info.plist and is named correctly; this is only for running from source.

Done through the Objective-C runtime with ctypes rather than by adding pyobjc:
it is one dictionary entry, and a dependency for a cosmetic fix on a
development path is a poor trade. Every step is checked, and any failure just
leaves the name as it was.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import sys

from PySide6.QtCore import QObject, Qt, QTimer

log = logging.getLogger(__name__)


def _objc():
    """The Objective-C runtime, with the signatures this needs."""
    path = ctypes.util.find_library("objc")
    if not path:
        return None
    runtime = ctypes.cdll.LoadLibrary(path)
    runtime.objc_getClass.restype = ctypes.c_void_p
    runtime.objc_getClass.argtypes = [ctypes.c_char_p]
    runtime.sel_registerName.restype = ctypes.c_void_p
    runtime.sel_registerName.argtypes = [ctypes.c_char_p]
    runtime.objc_msgSend.restype = ctypes.c_void_p
    return runtime


def set_application_name(name: str) -> bool:
    """Set CFBundleName for this process. Returns whether it worked."""
    if sys.platform != "darwin" or not name:
        return False
    try:
        runtime = _objc()
        if runtime is None:
            return False

        def send(target, selector, *args, types=()):
            runtime.objc_msgSend.argtypes = (
                [ctypes.c_void_p, ctypes.c_void_p] + list(types))
            return runtime.objc_msgSend(
                ctypes.c_void_p(target), runtime.sel_registerName(selector), *args)

        def nsstring(text: str):
            cls = runtime.objc_getClass(b"NSString")
            return send(cls, b"stringWithUTF8String:", text.encode("utf-8"),
                        types=[ctypes.c_char_p])

        bundle_class = runtime.objc_getClass(b"NSBundle")
        if not bundle_class:
            return False
        bundle = send(bundle_class, b"mainBundle")
        if not bundle:
            return False
        info = send(bundle, b"infoDictionary")
        if not info:
            return False

        key = nsstring("CFBundleName")
        value = nsstring(name)
        if not key or not value:
            return False
        # infoDictionary is immutable by contract but mutable in practice for
        # an unbundled process. If this one is not, the call is ignored and the
        # name simply stays as it was.
        send(info, b"setObject:forKey:", ctypes.c_void_p(value),
             ctypes.c_void_p(key), types=[ctypes.c_void_p, ctypes.c_void_p])

        readback = send(info, b"objectForKey:", ctypes.c_void_p(key),
                        types=[ctypes.c_void_p])
        if not readback:
            return False
        runtime.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        runtime.objc_msgSend.restype = ctypes.c_char_p
        got = runtime.objc_msgSend(
            ctypes.c_void_p(readback), runtime.sel_registerName(b"UTF8String"))
        return bool(got) and got.decode("utf-8", "replace") == name
    except Exception as exc:  # noqa: BLE001 - cosmetic; never worth raising
        log.debug("Could not set the process name: %s", exc)
        return False


# -- The menu bar of a process started from a terminal -----------------------

#: How long the round trip may take before it is given up, in milliseconds,
#: and how long after the Dock takes the focus it is taken back.
ROUND_TRIP_MS = 1000
BACK_AFTER_MS = 30

#: Every message the round trip sends, by class; ``+`` marks a class method.
#: Each is checked before anything is sent, since a message a class does not
#: answer ends the process.
_NEEDED = {
    "+NSRunningApplication": ("runningApplicationsWithBundleIdentifier:",),
    "NSRunningApplication": ("activateWithOptions:", "bundleIdentifier"),
    "NSArray": ("firstObject",),
    "+NSWorkspace": ("sharedWorkspace",),
    "NSWorkspace": ("frontmostApplication",),
    "+NSApplication": ("sharedApplication",),
    "NSApplication": ("activateIgnoringOtherApps:",),
    "+NSString": ("stringWithUTF8String:",),
    "NSString": ("UTF8String",),
}

#: NSApplicationActivateIgnoringOtherApps.
_IGNORING_OTHER_APPS = 1 << 1

_DOCK = "com.apple.dock"


class _AppKit:
    """The handful of AppKit messages the round trip needs, typed per call
    rather than by setting the one ``objc_msgSend``'s types each time."""

    def __init__(self) -> None:
        self._objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
        ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/AppKit.framework/AppKit")
        for name, result, arguments in (
                ("objc_getClass", ctypes.c_void_p, [ctypes.c_char_p]),
                ("sel_registerName", ctypes.c_void_p, [ctypes.c_char_p]),
                ("object_getClass", ctypes.c_void_p, [ctypes.c_void_p]),
                ("class_respondsToSelector", ctypes.c_bool,
                 [ctypes.c_void_p, ctypes.c_void_p])):
            function = getattr(self._objc, name)
            function.restype = result
            function.argtypes = arguments
        self._send = ctypes.cast(self._objc.objc_msgSend, ctypes.c_void_p).value
        self._shapes: dict = {}

    @classmethod
    def load(cls):
        """The runtime, or None where it or any message it needs is missing."""
        try:
            made = cls()
        except Exception as exc:  # noqa: BLE001 - nothing here is worth raising
            log.debug("No AppKit for the menu bar: %s", exc)
            return None
        gaps = made.missing()
        if gaps:
            log.debug("AppKit here lacks %s", ", ".join(gaps))
            return None
        return made

    def missing(self) -> list:
        gaps = []
        for name, selectors in _NEEDED.items():
            found = self._objc.objc_getClass(name.lstrip("+").encode())
            if not found:
                gaps.append(name.lstrip("+"))
                continue
            target = (self._objc.object_getClass(found) if name.startswith("+")
                      else found)
            gaps += [f"{name}{selector}" for selector in selectors
                     if not self._objc.class_respondsToSelector(
                         target, self._objc.sel_registerName(selector.encode()))]
        return gaps

    def send(self, receiver, selector: str, *args, restype=ctypes.c_void_p,
             argtypes=()):
        shape = (restype, tuple(argtypes))
        function = self._shapes.get(shape)
        if function is None:
            function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p,
                                        ctypes.c_void_p, *argtypes)(self._send)
            self._shapes[shape] = function
        return function(receiver, self._objc.sel_registerName(selector.encode()),
                        *args)

    def _class(self, name: str):
        return self._objc.objc_getClass(name.encode())

    def _dock(self):
        identifier = self.send(self._class("NSString"), "stringWithUTF8String:",
                               _DOCK.encode(), argtypes=[ctypes.c_char_p])
        running = self.send(self._class("NSRunningApplication"),
                            "runningApplicationsWithBundleIdentifier:",
                            identifier, argtypes=[ctypes.c_void_p])
        return self.send(running, "firstObject") if running else None

    def activate_dock(self) -> bool:
        dock = self._dock()
        if not dock:
            return False
        return bool(self.send(dock, "activateWithOptions:",
                              _IGNORING_OTHER_APPS, restype=ctypes.c_bool,
                              argtypes=[ctypes.c_ulong]))

    def dock_is_front(self) -> bool:
        workspace = self.send(self._class("NSWorkspace"), "sharedWorkspace")
        front = self.send(workspace, "frontmostApplication") if workspace else None
        if not front:
            return False
        name = self.send(front, "bundleIdentifier")
        if not name:
            return False
        text = self.send(name, "UTF8String", restype=ctypes.c_char_p)
        return (text or b"").decode("utf-8", "replace") == _DOCK

    def activate_self(self) -> None:
        app = self.send(self._class("NSApplication"), "sharedApplication")
        if app:
            self.send(app, "activateIgnoringOtherApps:", True,
                      argtypes=[ctypes.c_bool])


def wake_menu_bar(app, appkit=None):
    """Make the menu bar and the file panels work from the first moment when
    the app runs from source.

    A process started from a terminal is made a foreground app by Qt after
    it has launched, and macOS does not finish the change until the app
    loses the focus and gets it back: until then the menu bar shows no File
    or Edit, and a file panel's sidebar is greyed out. Clicking away and
    back fixed both, so this does that once, when the app first becomes
    active: the Dock takes the focus and, a moment later, the app takes it
    back. A built app is launched as a foreground app and needs none of it.

    Returns the object doing it, or None where there is nothing to do.
    """
    if sys.platform != "darwin" or getattr(sys, "frozen", False):
        return None
    appkit = appkit if appkit is not None else _AppKit.load()
    if appkit is None:
        return None
    return MenuBarWaker(app, appkit)


class MenuBarWaker(QObject):
    """The round trip, once: see wake_menu_bar."""

    def __init__(self, app, appkit) -> None:
        super().__init__(app)
        self._app = app
        self._appkit = appkit
        #: "waiting" for the first activation, "away" while the Dock has the
        #: focus, "done" after.
        self.stage = "waiting"
        self._window = None
        self._give_up = QTimer(self)
        self._give_up.setSingleShot(True)
        self._give_up.setInterval(ROUND_TRIP_MS)
        self._give_up.timeout.connect(self._finish)
        app.applicationStateChanged.connect(self.changed)

    def changed(self, state) -> None:
        active = state == Qt.ApplicationState.ApplicationActive
        if self.stage == "waiting" and active:
            from PySide6.QtWidgets import QApplication

            self._window = QApplication.activeWindow()
            if not self._appkit.activate_dock():
                self._finish()
                return
            self.stage = "away"
            self._give_up.start()
        elif self.stage == "away" and not active:
            QTimer.singleShot(BACK_AFTER_MS, self, self._back)

    def _back(self) -> None:
        if self.stage != "away":
            return
        # Only from the Dock: if the person went to another app in the
        # meantime, the focus is theirs to give.
        if self._appkit.dock_is_front():
            self._appkit.activate_self()
            window = self._window
            try:
                import shiboken6

                alive = window is not None and shiboken6.isValid(window)
            except Exception:  # noqa: BLE001 - assume it has gone
                alive = False
            if alive and window.isVisible():
                window.raise_()
                window.activateWindow()
        self._finish()

    def _finish(self) -> None:
        self.stage = "done"
        self._give_up.stop()
        try:
            self._app.applicationStateChanged.disconnect(self.changed)
        except (RuntimeError, TypeError):
            pass
