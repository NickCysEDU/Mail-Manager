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
