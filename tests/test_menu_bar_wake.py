"""The menu bar of the app run from source. Started from a terminal, macOS
does not finish making the process a foreground app until it loses the
focus and gets it back: the menu bar had no File or Edit, and a file
panel's sidebar was greyed out, until the window was clicked away from and
back to. macname.wake_menu_bar does that once at the start."""

from __future__ import annotations

import gc
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeAppKit:
    """What the round trip asks of AppKit, written down."""

    def __init__(self, dock_takes=True, dock_front=True):
        self.calls = []
        self.dock_takes = dock_takes
        self.dock_front = dock_front

    def activate_dock(self):
        self.calls.append("dock")
        return self.dock_takes

    def dock_is_front(self):
        self.calls.append("front?")
        return self.dock_front

    def activate_self(self):
        self.calls.append("back")


def _active():
    from PySide6.QtCore import Qt

    return Qt.ApplicationState.ApplicationActive


def _inactive():
    from PySide6.QtCore import Qt

    return Qt.ApplicationState.ApplicationInactive


@pytest.fixture
def from_source(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.delattr(sys, "frozen", raising=False)


def _wait(qtbot, ms):
    qtbot.wait(ms)


class TestTheRoundTrip:
    def test_it_goes_to_the_dock_and_comes_back_once(self, qapp, qtbot, from_source):
        import macname

        appkit = FakeAppKit()
        waker = macname.wake_menu_bar(qapp, appkit)
        assert waker is not None and waker.stage == "waiting"
        waker.changed(_active())
        assert appkit.calls == ["dock"] and waker.stage == "away"
        waker.changed(_inactive())
        qtbot.waitUntil(lambda: waker.stage == "done", timeout=1000)
        assert appkit.calls == ["dock", "front?", "back"]
        # Never again, however often the app comes and goes.
        waker.changed(_active())
        waker.changed(_inactive())
        _wait(qtbot, macname.BACK_AFTER_MS * 3)
        assert appkit.calls == ["dock", "front?", "back"]

    def test_the_focus_is_not_taken_back_from_another_app(self, qapp, qtbot, from_source):
        """Gone to another app in the moment between: the focus is theirs."""
        import macname

        appkit = FakeAppKit(dock_front=False)
        waker = macname.wake_menu_bar(qapp, appkit)
        waker.changed(_active())
        waker.changed(_inactive())
        qtbot.waitUntil(lambda: waker.stage == "done", timeout=1000)
        assert "back" not in appkit.calls

    def test_it_gives_up_when_the_dock_never_takes_the_focus(self, qapp, qtbot, from_source):
        import macname

        appkit = FakeAppKit()
        waker = macname.wake_menu_bar(qapp, appkit)
        waker.changed(_active())
        qtbot.waitUntil(lambda: waker.stage == "done",
                        timeout=macname.ROUND_TRIP_MS + 1000)
        waker.changed(_inactive())
        _wait(qtbot, macname.BACK_AFTER_MS * 3)
        assert appkit.calls == ["dock"], "it came back long after"

    def test_nothing_when_the_dock_will_not_go(self, qapp, from_source):
        import macname

        appkit = FakeAppKit(dock_takes=False)
        waker = macname.wake_menu_bar(qapp, appkit)
        waker.changed(_active())
        assert waker.stage == "done" and appkit.calls == ["dock"]

    def test_a_built_app_needs_none_of_it(self, qapp, monkeypatch):
        import macname

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert macname.wake_menu_bar(qapp, FakeAppKit()) is None
        monkeypatch.delattr(sys, "frozen")
        monkeypatch.setattr(sys, "platform", "linux")
        assert macname.wake_menu_bar(qapp, FakeAppKit()) is None

    def test_the_application_keeps_it_alive(self, qapp, qtbot, from_source):
        """main.py keeps no reference of its own."""
        import macname

        appkit = FakeAppKit()
        macname.wake_menu_bar(qapp, appkit)
        gc.collect()
        qapp.applicationStateChanged.emit(_active())
        assert appkit.calls == ["dock"]
        qapp.applicationStateChanged.emit(_inactive())
        qtbot.waitUntil(lambda: "back" in appkit.calls, timeout=1000)

    def test_main_asks_for_it_only_from_source(self):
        source = (ROOT / "main.py").read_text()
        at = source.index("macname.wake_menu_bar(app)")
        guard = source.rindex("if not getattr(sys, \"frozen\", False):", 0, at)
        assert at - guard < 300


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit is macOS only")
def test_on_the_real_platform_it_ends_active_with_its_window_key():
    """The real thing: the app goes to the Dock and comes back, and ends in
    front with its window key."""
    from test_gpu_canvas import real_platform_or_skip

    real_platform_or_skip()
    import macname

    if macname._AppKit.load() is None:
        pytest.skip("AppKit here lacks a message the round trip needs")
    script = textwrap.dedent(f"""
        import json, os, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        app = QApplication([])
        app.setApplicationName("Mail Manager")
        import macname
        seen = []
        started = time.monotonic()
        app.applicationStateChanged.connect(
            lambda state: seen.append(state.name))
        macname.wake_menu_bar(app)
        window = QWidget()
        window.setWindowTitle("Mail Manager - test of the menu bar")
        window.resize(320, 200)
        window.show()
        window.raise_()
        window.activateWindow()
        kit = macname._AppKit.load()
        def report():
            front = kit.send(kit.send(kit._class("NSWorkspace"), "sharedWorkspace"),
                             "frontmostApplication")
            import ctypes
            pid = kit.send(front, "processIdentifier", restype=ctypes.c_int) if front else -1
            print(json.dumps({{"seen": seen, "front_is_me": pid == os.getpid(),
                               "active": window.isActiveWindow()}}))
            sys.stdout.flush()
            os._exit(0)
        QTimer.singleShot(2000, report)
        app.exec()
    """)
    env = {key: value for key, value in os.environ.items()
           if key != "QT_QPA_PLATFORM"}
    # The round trip is checked on every attempt. Whether the app is still in
    # front two seconds later is up to whatever else is running: in the full
    # suite another worker's window can take the focus, which the waker
    # rightly never takes back. So a run where the focus went again after it
    # came back is tried again, and only the end state is left unchecked if
    # every attempt was interrupted.
    interrupted = []
    for _attempt in range(3):
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=60, env=env, cwd=str(ROOT))
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        assert lines, done.stderr[-2000:]
        found = json.loads(lines[-1])
        if found["seen"][:1] != ["ApplicationActive"]:
            pytest.skip(f"the app never became active here: {found['seen']}")
        assert found["seen"][:3] == ["ApplicationActive", "ApplicationInactive",
                                     "ApplicationActive"], found["seen"]
        if len(found["seen"]) > 3 and not found["front_is_me"]:
            interrupted.append(found)
            continue
        assert found["front_is_me"] and found["active"], found
        return
    pytest.skip(f"another app took the focus after every round trip: {interrupted}")
