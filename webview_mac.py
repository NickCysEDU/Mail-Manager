"""Messages drawn by WebKit, the engine Mail draws them with, inside the
app's own windows.

Qt draws HTML with a document engine of its own that knows only a little
CSS: a newsletter came out with its desktop and its phone layouts one under
the other, its buttons a third of their width and its badges without their
numbers. WebKit draws what the sender designed. ctypes rather than pyobjc,
as in touchbar_mac.

A message may do less in here than a page in a browser. Its HTML is
sanitised first (html_utils.sanitise_for_view); it runs no script; it loads
nothing but pictures, and those only when pictures are wanted, which a
Content-Security-Policy tells WebKit too; it keeps nothing between messages;
and every way off the page is stopped. A link clicked is handed to the app,
which shows where it goes before anything opens it.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import weakref
from html import escape
from typing import Callable, Dict, Optional

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtWidgets import QVBoxLayout, QWidget

log = logging.getLogger(__name__)

#: Every message sent, by class; "+" for messages to the class itself.
NEEDED = {
    "+NSAppearance": ("appearanceNamed:",),
    "+NSString": ("stringWithUTF8String:",),
    "NSString": ("UTF8String",),
    "NSObject": ("release", "init", "description"),
    "+NSObject": ("alloc",),
    "WKWebViewConfiguration": ("init", "defaultWebpagePreferences",
                               "setWebsiteDataStore:"),
    "WKWebpagePreferences": ("setAllowsContentJavaScript:",),
    "+WKWebsiteDataStore": ("nonPersistentDataStore",),
    "WKWebView": ("initWithFrame:configuration:", "setNavigationDelegate:",
                  "setUIDelegate:", "setAllowsLinkPreview:", "setAppearance:",
                  "loadHTMLString:baseURL:",
                  "evaluateJavaScript:completionHandler:", "stopLoading"),
    "WKNavigationAction": ("navigationType", "request"),
    "NSURLRequest": ("URL",),
    "NSURL": ("absoluteString",),
}

#: WKNavigationType: a link clicked, a form sent, back or forward, a
#: reload, a form sent again, anything else (a load of the app's own).
LINK, FORM, BACK_FORWARD, RELOAD, RESEND, OTHER = 0, 1, 2, 3, 4, -1
#: WKNavigationActionPolicy.
CANCEL, ALLOW = 0, 1

#: Where every message is, as WebKit sees it: no address of its own, so
#: nothing in it can reach a file or a site as if it belonged there.
PAGE = "about:blank"

#: How many times a page that took its renderer down is drawn again
#: before the view gives up on it: a message built to crash WebKit would
#: otherwise loop.
REDRAWS = 1

_kind_of_link = ("http://", "https://", "mailto:", "tel:")


def policy(pictures: bool) -> str:
    """The Content-Security-Policy every message is drawn under: inline
    styles, and pictures embedded in it or, when wanted, from the web."""
    images = "data: https: http:" if pictures else "data:"
    return (f"default-src 'none'; style-src 'unsafe-inline'; img-src {images}; "
            "font-src data:; base-uri 'none'; form-action 'none'")


def document(body: str, pictures: bool, note: str = "") -> str:
    """A message, already sanitised, as the page WebKit is given."""
    under = (f'<p class="mm-note">{escape(note)}</p>' if note else "")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f'<meta http-equiv="Content-Security-Policy" content="{policy(pictures)}">'
        # Before the message's own styles, so any of them wins: a message
        # that says nothing is drawn in the system's type on white.
        "<style>html{background:#fff;color:#141414}"
        "body{margin:12px;font:14px -apple-system,Helvetica,sans-serif;"
        "overflow-wrap:break-word}"
        ".mm-note{color:#6e6e73;font-style:italic}</style>"
        f"</head><body>{body}{under}</body></html>")


def plain_document(text: str) -> str:
    """Plain mail, as written: its lines kept, long ones wrapped."""
    return document(
        '<div style="white-space:pre-wrap;font:13px -apple-system,'
        f'Helvetica,sans-serif">{escape(text)}</div>', False)


class _Descriptor(ctypes.Structure):
    _fields_ = [("reserved", ctypes.c_ulong), ("size", ctypes.c_ulong)]


class _Block(ctypes.Structure):
    """The layout of an Objective-C block, enough to make and call one."""

    _fields_ = [("isa", ctypes.c_void_p), ("flags", ctypes.c_int),
                ("reserved", ctypes.c_int), ("invoke", ctypes.c_void_p),
                ("descriptor", ctypes.POINTER(_Descriptor))]


#: A global block: copying it is a no-op, and nothing frees it.
_BLOCK_IS_GLOBAL = 1 << 28

_runtime = None
_delegate = None
#: What the blocks and the delegate's methods are made of, kept for the
#: life of the process: WebKit holds pointers to them.
_kept: list = []
#: Web view -> the MessageWebView it belongs to.
_views: Dict[int, "weakref.ReferenceType"] = {}
_checked: Optional[bool] = None


def _rt():
    global _runtime
    if _runtime is None:
        from touchbar_mac import _Runtime

        ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/WebKit.framework/WebKit")
        _runtime = _Runtime()
    return _runtime


def available() -> bool:
    """Whether messages can be drawn by WebKit here: a Mac, Qt on its own
    platform rather than offscreen, and every message this sends answered.
    """
    global _checked
    if _checked is None:
        _checked = False
        if sys.platform == "darwin" and QGuiApplication.platformName() == "cocoa":
            try:
                gaps = _rt().missing(NEEDED)
            except Exception:      # noqa: BLE001 - no WebKit is no WebKit
                log.exception("WebKit could not be loaded")
                gaps = ["WebKit"]
            if gaps:
                log.warning("Messages are drawn by Qt: WebKit here lacks %s",
                            ", ".join(gaps[:6]))
            _checked = not gaps
    return _checked


_global_class = None


def _block_class() -> int:
    """Where a global block's class lives, which is what a block's isa
    points at."""
    global _global_class
    if _global_class is None:
        _global_class = ctypes.c_void_p.in_dll(
            ctypes.CDLL("/usr/lib/libSystem.B.dylib"), "_NSConcreteGlobalBlock")
    return ctypes.addressof(_global_class)


def _decide_with(handler, answer: int) -> None:
    """Answer a decision handler block, as WebKit requires, exactly once."""
    block = _Block.from_address(handler)
    ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_long)(block.invoke)(
        handler, answer)


def _owner(web) -> Optional["MessageWebView"]:
    found = _views.get(web)
    return found() if found is not None else None


def _url_of(action) -> str:
    rt = _rt()
    request = rt.send(action, "request")
    url = rt.send(request, "URL") if request else 0
    return rt.text(rt.send(url, "absoluteString")) if url else ""


def _verdict(kind: int, url: str) -> int:
    """Whether a navigation may happen: only the app's own loads of a
    message, a reload of one, and a jump within it. Everything else stays
    on the page; a link a person clicked is handed to the app as well."""
    page = url.replace("%23", "#", 1)
    if kind in (OTHER, RELOAD) and page == PAGE:
        return ALLOW
    if kind == LINK and page.startswith(PAGE + "#"):
        return ALLOW
    return CANCEL


def _decide(_self, _cmd, web, action, handler) -> None:
    answer = CANCEL
    try:
        kind = _rt().send(action, "navigationType", restype=ctypes.c_long)
        url = _url_of(action)
        answer = _verdict(kind, url)
        view = _owner(web)
        if (answer == CANCEL and kind == LINK and view is not None
                and url.lower().startswith(_kind_of_link)):
            view._clicked(url)
    except Exception:      # noqa: BLE001 - nothing may unwind into WebKit
        log.exception("A message's navigation could not be decided")
        answer = CANCEL
    finally:
        _decide_with(handler, answer)


def _finished(_self, _cmd, web, _navigation) -> None:
    try:
        view = _owner(web)
        if view is not None:
            view._drawn()
    except Exception:      # noqa: BLE001
        log.exception("A message's page could not be finished")


def _new_window(_self, _cmd, web, _configuration, action, _features):
    """A link meant for a window of its own: handed to the app, and no
    window made."""
    try:
        view = _owner(web)
        url = _url_of(action)
        if view is not None and url.lower().startswith(_kind_of_link):
            view._clicked(url)
    except Exception:      # noqa: BLE001
        log.exception("A message's link could not be handed over")
    return None


def _renderer_gone(_self, _cmd, web) -> None:
    """WebKit draws in a process of its own; when one dies, its pages are
    blank until drawn again."""
    try:
        view = _owner(web)
        if view is not None:
            view._redraw()
    except Exception:      # noqa: BLE001
        log.exception("A message could not be drawn again")


def _make_delegate() -> int:
    global _delegate
    if _delegate is not None:
        return _delegate
    rt = _rt()
    _id = ctypes.c_void_p
    methods = (
        ("webView:decidePolicyForNavigationAction:decisionHandler:",
         ctypes.CFUNCTYPE(None, _id, _id, _id, _id, _id)(_decide), b"v@:@@@?"),
        ("webView:didFinishNavigation:",
         ctypes.CFUNCTYPE(None, _id, _id, _id, _id)(_finished), b"v@:@@"),
        ("webView:createWebViewWithConfiguration:forNavigationAction:"
         "windowFeatures:",
         ctypes.CFUNCTYPE(_id, _id, _id, _id, _id, _id, _id)(_new_window),
         b"@@:@@@@"),
        ("webViewWebContentProcessDidTerminate:",
         ctypes.CFUNCTYPE(None, _id, _id, _id)(_renderer_gone), b"v@:@"),
    )
    name, number = b"MMMessageWebDelegate", 1
    while rt.objc.objc_getClass(name):
        number += 1
        name = b"MMMessageWebDelegate%d" % number
    made = rt.objc.objc_allocateClassPair(rt.cls("NSObject"), name, 0)
    if not made:
        raise RuntimeError("could not make the web view's delegate class")
    for selector, imp, types in methods:
        _kept.append(imp)
        if not rt.objc.class_addMethod(made, rt.sel(selector),
                                       ctypes.cast(imp, _id), types):
            raise RuntimeError(f"could not add {selector}")
    for protocol in (b"WKNavigationDelegate", b"WKUIDelegate"):
        found = rt.objc.objc_getProtocol(protocol)
        if found:
            rt.objc.class_addProtocol(made, found)
    rt.objc.objc_registerClassPair(made)
    _delegate = rt.send(rt.send(made, "alloc"), "init")
    return _delegate


def _make_web_view() -> int:
    """A web view that runs no script and keeps nothing: retained, once."""
    from touchbar_mac import NSRect

    rt = _rt()
    _id = ctypes.c_void_p
    with rt.pool():
        config = rt.send(rt.send(rt.cls("WKWebViewConfiguration"), "alloc"),
                         "init")
        preferences = rt.send(config, "defaultWebpagePreferences")
        rt.send(preferences, "setAllowsContentJavaScript:", False,
                argtypes=[ctypes.c_bool])
        rt.send(config, "setWebsiteDataStore:", rt.send(
            rt.cls("WKWebsiteDataStore"), "nonPersistentDataStore"),
            argtypes=[_id])
        web = rt.send(rt.send(rt.cls("WKWebView"), "alloc"),
                      "initWithFrame:configuration:",
                      NSRect(0.0, 0.0, 400.0, 300.0), config,
                      argtypes=[NSRect, _id])
        rt.send(config, "release")
        delegate = _make_delegate()
        rt.send(web, "setNavigationDelegate:", delegate, argtypes=[_id])
        rt.send(web, "setUIDelegate:", delegate, argtypes=[_id])
        # A firm press on a link opens the page in a preview: that is a
        # load nobody asked for.
        rt.send(web, "setAllowsLinkPreview:", False, argtypes=[ctypes.c_bool])
        # A message is laid out for a light page, as in Mail.
        rt.send(web, "setAppearance:", rt.send(
            rt.cls("NSAppearance"), "appearanceNamed:",
            rt.string("NSAppearanceNameAqua"), argtypes=[_id]),
            argtypes=[_id])
    return web


def _release(web: int) -> None:
    """The widget has gone: its web view stops and is let go."""
    _views.pop(web, None)
    try:
        rt = _rt()
        rt.send(web, "stopLoading")
        rt.send(web, "setNavigationDelegate:", None, argtypes=[ctypes.c_void_p])
        rt.send(web, "setUIDelegate:", None, argtypes=[ctypes.c_void_p])
        rt.send(web, "release")
    except Exception:      # noqa: BLE001 - leaving is never worth a crash
        log.exception("A message's web view could not be let go")


_EVALUATED = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.c_void_p)
#: Answers to evaluate(), by ticket: a block made once serves every call.
_answers: Dict[int, Callable] = {}
_tickets = iter(range(1, sys.maxsize))


class MessageWebView(QWidget):
    """A message, drawn by WebKit: what triage_table.MailView does with Qt's
    document engine, and in its terms, so either serves the preview and the
    message window."""

    #: A link a person clicked, for the app to show and open.
    anchorClicked = Signal(QUrl)
    #: The page is drawn.
    loaded = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._pictures = False
        #: The page on show, and what it was made from, for drawing again
        #: and for the text of it.
        self._page = ""
        self._text = ""
        self._redraws = 0
        #: Where to scroll the next page to once it is drawn.
        self._place: Optional[float] = None
        self._web = _make_web_view()
        _views[self._web] = weakref.ref(self)
        web = self._web
        self.destroyed.connect(lambda *_: _release(web))
        self._container = QWidget.createWindowContainer(
            QWindow.fromWinId(int(web)), self)
        self._container.setMinimumSize(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._container)
        self.setMinimumHeight(24)
        self.clear()

    def set_pictures(self, wanted: bool) -> None:
        self._pictures = bool(wanted)

    def show_message(self, html: str, note: str = "",
                     keep_place: bool = False) -> None:
        """``note`` goes under the message, in the view's own words: what is
        missing from it. ``keep_place`` keeps the page where it was
        scrolled to, for more of the same message."""
        import html_utils

        body = html_utils.sanitise_for_view(html, pictures=self._pictures,
                                            keep_urls=self._pictures)
        self._text = html_utils.html_to_text(html).text + (
            f"\n\n{note}" if note else "")
        self._load(document(body, self._pictures, note), keep_place)

    def setPlainText(self, text: str) -> None:      # noqa: N802 - Qt's name
        self._text = text or ""
        self._load(plain_document(self._text), False)

    def clear(self) -> None:
        self._text = ""
        self._load(document("", False), False)

    def toPlainText(self) -> str:      # noqa: N802 - Qt's name
        """The words of what is on show, as the app read them."""
        return self._text

    def evaluate(self, script: str, answer: Callable = None) -> None:
        """Run a line of the app's own script in the page, never the
        message's: what it comes to, as text, to ``answer``."""
        ticket = next(_tickets)
        if answer is not None:
            _answers[ticket] = answer
        rt = _rt()
        rt.send(self._web, "evaluateJavaScript:completionHandler:",
                rt.string(script), _answer_block(ticket),
                argtypes=[ctypes.c_void_p, ctypes.c_void_p])

    def _load(self, page: str, keep_place: bool) -> None:
        self._page = page
        self._redraws = 0
        if keep_place:
            self.evaluate("window.scrollY", self._remember_place)
        else:
            self._place = None
        rt = _rt()
        with rt.pool():
            rt.send(self._web, "loadHTMLString:baseURL:", rt.string(page), None,
                    argtypes=[ctypes.c_void_p, ctypes.c_void_p])

    def _remember_place(self, value: str) -> None:
        try:
            self._place = float(value)
        except (TypeError, ValueError):
            self._place = None

    def _drawn(self) -> None:
        place, self._place = self._place, None
        if place:
            self.evaluate(f"window.scrollTo(0, {float(place)})")
        self.loaded.emit()

    def _clicked(self, url: str) -> None:
        self.anchorClicked.emit(QUrl(url))

    def _redraw(self) -> None:
        if self._redraws >= REDRAWS:
            log.warning("A message took WebKit's renderer down twice; it is "
                        "shown as text")
            page, self._page = self._page, ""
            if page:
                self._redraws = REDRAWS
                rt = _rt()
                with rt.pool():
                    rt.send(self._web, "loadHTMLString:baseURL:", rt.string(
                        plain_document(self._text or "This message could not "
                                       "be drawn.")), None,
                        argtypes=[ctypes.c_void_p, ctypes.c_void_p])
            return
        self._redraws += 1
        rt = _rt()
        with rt.pool():
            rt.send(self._web, "loadHTMLString:baseURL:", rt.string(self._page),
                    None, argtypes=[ctypes.c_void_p, ctypes.c_void_p])


def _answer_block(ticket: int) -> int:
    """A block that hands what an evaluation came to to its ticket's
    answer. Made per ticket, kept until it is called."""
    def arrived(_block, result, _error, ticket=ticket) -> None:
        answer = _answers.pop(ticket, None)
        # Let go of the block once it has returned: freed while running, it
        # took the process down.
        QTimer.singleShot(0, lambda: _pending.pop(ticket, None))
        if answer is None:
            return
        try:
            text = _rt().text(_rt().send(result, "description")) if result else ""
            answer(text)
        except Exception:      # noqa: BLE001 - nothing may unwind into WebKit
            log.exception("A message's script answer could not be used")

    imp = _EVALUATED(arrived)
    descriptor = _Descriptor(0, ctypes.sizeof(_Block))
    block = _Block(_block_class(), _BLOCK_IS_GLOBAL, 0,
                   ctypes.cast(imp, ctypes.c_void_p), ctypes.pointer(descriptor))
    _pending[ticket] = (imp, descriptor, block)
    return ctypes.addressof(block)


#: Blocks waiting for their answer, by ticket.
_pending: Dict[int, tuple] = {}
