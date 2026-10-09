"""Messages drawn by WebKit, on the Mac's own platform.

The rest of the suite runs offscreen, where Qt's own view draws messages.
These run a script in a subprocess on the real platform and ask WebKit what
it drew, through the app's own line of script in the page. Every message
here is invented.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(sys.platform != "darwin",
                                reason="WebKit is macOS's here")

HEAD = textwrap.dedent("""
    import json, os, sys, tempfile, time
    os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, {root!r})
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication, QMainWindow
    app = QApplication([])
    import ctypes as _c
    from touchbar_mac import _Runtime as _Kit
    _kit = _Kit()
    # Drawn, but never in front of whatever the person at the Mac is using:
    # brought forward, a test's window took the focus from them and from
    # other tests.
    _kit.send(_kit.send(_kit.cls("NSApplication"), "sharedApplication"),
              "setActivationPolicy:", 2, argtypes=[_c.c_long], restype=_c.c_bool)
    import webview_mac
    if not webview_mac.available():
        print(json.dumps({{"skip": "WebKit is not available here"}}))
        sys.exit(0)
    out = {{}}

    def spin(ms):
        # The run loop itself, as the app's runs: WebKit answers through it.
        loop = QEventLoop()
        QTimer.singleShot(ms, loop.quit)
        loop.exec()

    def until(check, ms=8000):
        ends = time.monotonic() + ms / 1000
        while not check() and time.monotonic() < ends:
            spin(20)
        return check()

    window = QMainWindow()
    view = webview_mac.MessageWebView()
    window.setCentralWidget(view)
    window.resize(640, 480)
    window.show()
    # Behind everything, so nobody at the Mac is shown a stray window.
    window.lower()
    clicked = []
    view.anchorClicked.connect(lambda url: clicked.append(url.toString()))
    drawn = []
    view.loaded.connect(lambda: drawn.append(1))

    def shown(load):
        before = len(drawn)
        load()
        return until(lambda: len(drawn) > before)

    def ask(script):
        got = []
        view.evaluate(script, got.append)
        until(lambda: got)
        return got[0] if got else None
""")


def _run(*parts: str) -> dict:
    from test_gpu_canvas import real_platform_or_skip

    real_platform_or_skip()
    script = HEAD.format(root=str(ROOT)) + "".join(
        textwrap.dedent(part) for part in parts) + (
        "\nprint(json.dumps(out), flush=True)\nos._exit(0)\n")
    env = {key: value for key, value in os.environ.items()
           if key != "QT_QPA_PLATFORM"}
    done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=50, env=env, cwd=str(ROOT))
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert done.returncode == 0 and lines, done.stderr[-3000:]
    result = json.loads(lines[-1])
    if "skip" in result:
        pytest.skip(result["skip"])
    return result


def test_every_message_it_sends_is_checked_first():
    """A message AppKit or WebKit does not understand ends the process, so
    each one sent is in NEEDED, which available() checks before any view is
    made; and NEEDED holds nothing else, or it checks for something never
    sent. The runtime's own string calls count as sent where they are
    used."""
    import ast

    import webview_mac

    source = (ROOT / "webview_mac.py").read_text(encoding="utf-8")
    sent = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr == "send" and len(node.args) >= 2:
            assert isinstance(node.args[1], ast.Constant), node.lineno
            sent.add(node.args[1].value)
        elif node.func.attr == "string":
            sent.add("stringWithUTF8String:")
        elif node.func.attr == "text":
            sent.add("UTF8String")
    needed = {selector for selectors in webview_mac.NEEDED.values()
              for selector in selectors}
    assert sent - needed == set(), "sent without being checked"
    assert needed - sent == set(), "checked but never sent"


def test_the_app_draws_messages_with_webkit_here():
    """And keeps nothing between messages, and opens no preview of a link
    pressed firmly: that preview is the page, loaded."""
    result = _run("""
        import ctypes
        from triage_table import PreviewPane, message_view
        out["kind"] = type(message_view()).__name__
        out["preview"] = type(PreviewPane().rich_view).__name__
        rt = webview_mac._rt()
        store = rt.send(rt.send(view._web, "configuration"), "websiteDataStore")
        out["keeps"] = bool(rt.send(store, "isPersistent", restype=ctypes.c_bool))
        out["previews"] = bool(rt.send(view._web, "allowsLinkPreview",
                                       restype=ctypes.c_bool))
    """)
    assert result == {"kind": "MessageWebView", "preview": "MessageWebView",
                      "keeps": False, "previews": False}


def test_a_message_is_drawn_as_designed():
    """A newsletter carries a phone layout and a desktop one and hides one
    with its styles; Qt's engine, which ignores display:none, drew both."""
    result = _run("""
        out["drawn"] = shown(lambda: view.show_message(
            '<div style="display:none">The phone edition of the club news</div>'
            '<table width="560" align="center"><tr><td>'
            '<h1>The club news</h1><p>Booking for the trip closes on Friday.</p>'
            '</td></tr></table>', note="This is only the start of the message."))
        out["text"] = ask("document.body.innerText")
        out["words"] = view.toPlainText()
    """)
    assert result["drawn"]
    assert "Booking for the trip closes on Friday." in result["text"]
    assert "This is only the start of the message." in result["text"]
    assert "phone edition" not in result["text"]


def test_no_script_in_a_message_runs():
    """The sanitiser takes scripts out; were one to get past it, WebKit
    runs none of the page's own: a page with no policy of its own, so the
    setting alone is what is tested."""
    result = _run("""
        raw = ('<html><body><p>Plain words.</p>'
               '<script>document.title = "ran"</script>'
               '<img src="data:image/gif;base64,R0lGODlhAQABAAAAACw=" '
               'onload="document.title = \\'ran\\'"></body></html>')
        out["drawn"] = shown(lambda: view._load(raw, False))
        spin(500)
        out["title"] = ask("document.title")
        out["text"] = ask("document.body.innerText")
    """)
    assert result["drawn"]
    assert result["title"] != "ran"
    assert "Plain words." in result["text"]


class _Recorder(BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):      # noqa: N802 - the server's name
        type(self).seen.append(self.path)
        body = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
                b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
                b"\x00\x00\x02\x02D\x01\x00;")
        self.send_response(200)
        self.send_header("Content-Type", "image/gif"
                         if self.path.endswith(".gif") else "text/css")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@pytest.fixture
def server():
    _Recorder.seen = []
    made = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=made.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{made.server_address[1]}"
    made.shutdown()
    made.server_close()


def _everything_that_could_load(base: str) -> str:
    return (
        f'<link rel="stylesheet" href="{base}/sheet.css">'
        f'<style>@import url({base}/imported.css);'
        f'@font-face{{font-family:X;src:url({base}/font.woff)}}'
        f'.b{{background:url({base}/background.gif)}}</style>'
        f'<div class="b">y</div>'
        f'<div style="background-image:url({base}/inline.gif)">x</div>'
        f'<img src="{base}/picture.gif" width="40" height="40">'
        f'<iframe src="{base}/frame.html"></iframe>'
        f'<object data="{base}/object.gif"></object>'
        f'<video poster="{base}/poster.gif"></video>'
        f'<audio src="{base}/sound.mp3"></audio>'
        f'<table background="{base}/table.gif"><tr><td>words</td></tr></table>')


def test_nothing_loads_unasked(server):
    """With pictures off nothing at all is fetched: a picture fetched is a
    sender told the message was opened. The page as WebKit gets it, past
    the sanitiser, so the policy alone is what is tested."""
    result = _run(f"""
        raw = webview_mac.document({_everything_that_could_load(server)!r},
                                   pictures=False)
        out["drawn"] = shown(lambda: view._load(raw, False))
        spin(1500)
    """)
    assert result["drawn"]
    assert _Recorder.seen == []


def test_with_pictures_on_only_pictures_load(server):
    result = _run(f"""
        view.set_pictures(True)
        out["drawn"] = shown(lambda: view.show_message(
            {_everything_that_could_load(server)!r}))
        spin(1500)
    """)
    assert result["drawn"]
    seen = set(_Recorder.seen)
    assert {"/picture.gif", "/background.gif", "/inline.gif"} <= seen
    assert not seen & {"/sheet.css", "/imported.css", "/font.woff",
                       "/frame.html", "/object.gif", "/sound.mp3"}, seen


def test_a_link_is_handed_to_the_app_and_the_page_stays():
    result = _run("""
        out["drawn"] = shown(lambda: view.show_message(
            '<p><a href="https://club.example/routes">The new routes</a></p>'
            '<p><a href="https://club.example/away" target="_blank">Away</a></p>'
            '<p><a href="mailto:secretary@club.example">Write</a></p>'
            '<p style="margin-top:2000px" id="later">Later on</p>'
            '<p><a href="#later">Down</a></p>'))
        for index in range(3):
            ask(f"document.links[{index}].click(); 1")
        spin(500)
        out["clicked"] = list(clicked)
        out["text"] = ask("document.body.innerText")
        out["url"] = ask("location.href")
    """)
    assert result["clicked"] == ["https://club.example/routes",
                                 "https://club.example/away",
                                 "mailto:secretary@club.example"]
    assert "The new routes" in result["text"]
    assert result["url"] == "about:blank"


def test_there_is_no_going_back_to_another_message():
    """Every message is drawn in the same view: were there history, going
    back would show the one before in this one's place. Each is drawn over
    the last, leaving none."""
    result = _run("""
        import ctypes
        shown(lambda: view.show_message("<p>The first message.</p>"))
        shown(lambda: view.show_message("<p>The second message.</p>"))
        out["back"] = bool(webview_mac._rt().send(
            view._web, "canGoBack", restype=ctypes.c_bool))
        out["history"] = ask("history.length")
    """)
    assert result == {"back": False, "history": "1"}


def test_a_page_whose_renderer_died_is_drawn_again():
    """WebKit draws in a process of its own, which a message may take down;
    the page was left blank."""
    result = _run("""
        import ctypes
        shown(lambda: view.show_message("<p>Still here.</p>"))
        rt = webview_mac._rt()
        out["redrawn"] = shown(lambda: rt.send(
            webview_mac._delegate, "webViewWebContentProcessDidTerminate:",
            view._web, argtypes=[ctypes.c_void_p]))
        out["text"] = ask("document.body.innerText")
        # Again, and it gives up on the page rather than looping: the words.
        out["fallback"] = shown(lambda: rt.send(
            webview_mac._delegate, "webViewWebContentProcessDidTerminate:",
            view._web, argtypes=[ctypes.c_void_p]))
        out["after"] = ask("document.body.innerText")
    """)
    assert result["redrawn"] and "Still here." in result["text"]
    assert result["fallback"] and "Still here." in result["after"]


def test_more_of_the_same_message_keeps_the_place():
    result = _run("""
        lines = "".join(f"<p>Line {n} of the club news.</p>" for n in range(200))
        shown(lambda: view.show_message(lines))
        ask("window.scrollTo(0, 900); 1")
        out["before"] = ask("window.scrollY")
        shown(lambda: view.show_message(lines + "<p>The end.</p>",
                                        keep_place=True))
        until(lambda: ask("window.scrollY") == out["before"], 3000)
        out["after"] = ask("window.scrollY")
        shown(lambda: view.show_message("<p>Another message.</p>" + lines))
        out["other"] = ask("window.scrollY")
    """)
    assert float(result["before"]) > 0
    assert result["after"] == result["before"]
    assert float(result["other"]) == 0


def test_plain_mail_keeps_its_lines():
    result = _run("""
        shown(lambda: view.setPlainText("Dear club,\\n\\n  indented <b>not bold</b>"))
        out["text"] = ask("document.body.innerText")
        out["bold"] = ask("document.querySelectorAll('b').length")
    """)
    assert "Dear club," in result["text"]
    assert "<b>not bold</b>" in result["text"]
    assert result["bold"] == "0"
