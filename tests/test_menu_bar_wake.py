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
    """What the round trip asks of AppKit, written down. An answer given as
    a list is given in turn, the last one for good."""

    def __init__(self, dock_takes=True, dock_front=True, panel_up=True, active=True):
        self.calls = []
        self.dock_takes = dock_takes
        self.dock_front = dock_front
        self.panel_up = panel_up
        self.active = active

    def _answer(self, name):
        value = getattr(self, name)
        if isinstance(value, list):
            return value.pop(0) if len(value) > 1 else value[0]
        return value

    def activate_dock(self):
        self.calls.append("dock")
        return self.dock_takes

    def dock_is_front(self):
        self.calls.append("front?")
        return self._answer("dock_front")

    def activate_self(self):
        self.calls.append("back")

    def is_active(self):
        self.calls.append("active?")
        return self._answer("active")

    def panel_is_up(self):
        self.calls.append("panel?")
        return self._answer("panel_up")


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


class TestTheFilePanel:
    """A file panel's sidebar is drawn by another process, and in a run from
    source it came up greyed out until the focus went away and came back.
    refocus_file_panel does that once the panel is up."""

    @staticmethod
    def _settled(macname):
        return macname.PANEL_LOOK_MS + macname.PANEL_SETTLE_MS

    def test_once_the_panel_is_up_the_focus_goes_away_and_comes_back(
            self, qapp, qtbot, from_source):
        import macname

        appkit = FakeAppKit()
        assert macname.refocus_file_panel(qapp, appkit)
        assert appkit.calls == []
        qtbot.waitUntil(lambda: bool(appkit.calls), timeout=1000)
        assert appkit.calls == ["panel?"], "it is left to draw first"
        qtbot.waitUntil(lambda: "back" in appkit.calls,
                        timeout=self._settled(macname) + 1000)
        assert appkit.calls == ["panel?", "panel?", "dock", "front?", "back"]

    def test_it_waits_for_a_panel_slow_to_appear(self, qapp, qtbot, from_source):
        """Here the panel took over half a second to come up."""
        import macname

        appkit = FakeAppKit(panel_up=[False] * 12 + [True])
        macname.refocus_file_panel(qapp, appkit)
        qtbot.waitUntil(lambda: "back" in appkit.calls, timeout=5000)
        assert appkit.calls == ["panel?"] * 14 + ["dock", "front?", "back"]

    def test_it_stops_looking_for_a_panel_that_never_came(
            self, qapp, qtbot, monkeypatch, from_source):
        import macname

        monkeypatch.setattr(macname, "PANEL_LOOKS", 4)
        appkit = FakeAppKit(panel_up=False)
        macname.refocus_file_panel(qapp, appkit)
        _wait(qtbot, macname.PANEL_LOOK_MS * 12)
        assert appkit.calls == ["panel?"] * 4

    def test_nothing_when_the_panel_closed_before_it_settled(self, qapp, qtbot,
                                                             from_source):
        import macname

        appkit = FakeAppKit(panel_up=[True, False])
        macname.refocus_file_panel(qapp, appkit)
        _wait(qtbot, self._settled(macname) + macname.BACK_AFTER_MS * 4)
        assert appkit.calls == ["panel?", "panel?"]

    def test_it_comes_back_once_the_dock_has_the_focus(self, qapp, qtbot, from_source):
        """The Dock can take a moment to come to the front."""
        import macname

        appkit = FakeAppKit(dock_front=[False, False, True])
        macname.refocus_file_panel(qapp, appkit)
        qtbot.waitUntil(lambda: "back" in appkit.calls,
                        timeout=self._settled(macname) + 1000)
        assert appkit.calls == ["panel?", "panel?", "dock", "front?", "active?",
                                "front?", "active?", "front?", "back"]

    def test_the_focus_is_not_taken_back_from_another_app(self, qapp, qtbot, from_source):
        import macname

        appkit = FakeAppKit(dock_front=False, active=False)
        macname.refocus_file_panel(qapp, appkit)
        qtbot.waitUntil(lambda: "active?" in appkit.calls,
                        timeout=self._settled(macname) + 1000)
        _wait(qtbot, macname.BACK_AFTER_MS * 4)
        assert appkit.calls == ["panel?", "panel?", "dock", "front?", "active?"]

    def test_it_gives_up_when_the_dock_never_takes_the_focus(self, qapp, qtbot,
                                                             from_source):
        import macname

        appkit = FakeAppKit(dock_front=False)
        macname.refocus_file_panel(qapp, appkit)
        _wait(qtbot, self._settled(macname) + macname.ROUND_TRIP_MS + 500)
        assert "back" not in appkit.calls
        waits = appkit.calls.count("active?")
        assert 1 < waits <= macname.ROUND_TRIP_MS // macname.BACK_AFTER_MS
        _wait(qtbot, macname.BACK_AFTER_MS * 4)
        assert appkit.calls.count("active?") == waits, "it gave up for good"

    def test_nothing_comes_back_when_the_dock_will_not_go(self, qapp, qtbot, from_source):
        import macname

        appkit = FakeAppKit(dock_takes=False)
        macname.refocus_file_panel(qapp, appkit)
        _wait(qtbot, self._settled(macname) + macname.BACK_AFTER_MS * 4)
        assert appkit.calls == ["panel?", "panel?", "dock"]

    def test_a_built_app_needs_none_of_it(self, qapp, qtbot, monkeypatch):
        import macname

        appkit = FakeAppKit()
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert not macname.refocus_file_panel(qapp, appkit)
        monkeypatch.delattr(sys, "frozen")
        monkeypatch.setattr(sys, "platform", "linux")
        assert not macname.refocus_file_panel(qapp, appkit)
        _wait(qtbot, self._settled(macname) + 100)
        assert appkit.calls == []

    def test_a_panel_qt_draws_itself_is_left_alone(self, qapp, from_source):
        """Off Cocoa (here, the tests' offscreen platform) a file panel is a
        Qt widget in this process, and AppKit is not even asked."""
        import macname

        assert qapp.platformName() != "cocoa"
        assert not macname.refocus_file_panel(qapp)

    @pytest.mark.parametrize("helper,method,args", [
        ("open_files", "getOpenFileNames", ("Add tracks",)),
        ("open_file", "getOpenFileName", ("Choose an image",)),
        ("save_file", "getSaveFileName", ("Export",)),
        ("choose_directory", "getExistingDirectory", ("Save all",)),
    ])
    def test_each_panel_asks_for_it_before_it_opens(self, monkeypatch, helper, method,
                                                     args):
        from PySide6.QtWidgets import QFileDialog

        import macname
        import widgets

        order = []
        monkeypatch.setattr(macname, "refocus_file_panel",
                            lambda *a, **k: order.append("refocus") or True)
        monkeypatch.setattr(QFileDialog, method, staticmethod(
            lambda *a, **k: order.append("panel") or ("", "")))
        getattr(widgets, helper)(None, *args)
        assert order == ["refocus", "panel"]

    def test_every_file_panel_in_the_app_opens_through_those(self):
        """A panel opened straight from QFileDialog would come up grey."""
        opened = []
        for path in sorted(ROOT.glob("*.py")):
            if path.name == "widgets.py":
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "QFileDialog.get" in line:
                    opened.append(f"{path.name}:{number}")
        assert opened == []


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


@pytest.mark.skipif(sys.platform != "darwin", reason="AppKit is macOS only")
def test_on_the_real_platform_a_file_panel_goes_away_and_comes_back_once_up():
    """The real thing: a native file panel opened through widgets.open_file.
    Once it is up the app goes to the Dock and comes back, and the panel
    ends key."""
    from test_gpu_canvas import real_platform_or_skip

    real_platform_or_skip()
    import macname

    if macname._AppKit.load() is None:
        pytest.skip("AppKit here lacks a message the round trip needs")
    script = textwrap.dedent(f"""
        import ctypes, json, os, sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QWidget
        app = QApplication([])
        app.setApplicationName("Mail Manager")
        import macname, widgets
        kit = macname._AppKit.load()
        nsapp = kit.send(kit._class("NSApplication"), "sharedApplication")
        started = time.monotonic()
        found = {{"states": [], "up_at": None, "opened": False, "away_fronts": []}}
        app.applicationStateChanged.connect(lambda state: found["states"].append(
            (time.monotonic() - started, state.name)))
        window = QWidget()
        window.setWindowTitle("Mail Manager - test of the file panel")
        window.resize(320, 200)
        window.show()
        window.raise_()
        window.activateWindow()
        def front_name():
            workspace = kit.send(kit._class("NSWorkspace"), "sharedWorkspace")
            front = kit.send(workspace, "frontmostApplication") if workspace else None
            name = kit.send(front, "bundleIdentifier") if front else None
            text = kit.send(name, "UTF8String", restype=ctypes.c_char_p) if name else None
            return (text or b"").decode()
        def tick():
            modal = kit.send(nsapp, "modalWindow")
            now = time.monotonic() - started
            if not kit.is_active():
                front = front_name()
                if front and front not in found["away_fronts"]:
                    found["away_fronts"].append(front)
            if modal and found["up_at"] is None:
                found["up_at"] = now
            if found["up_at"] is not None and now > found["up_at"] + 1.5:
                found["key"] = bool(modal) and bool(
                    kit.send(modal, "isKeyWindow", restype=ctypes.c_bool))
                found["active"] = kit.is_active()
                print(json.dumps(found))
                sys.stdout.flush()
                if modal:
                    kit.send(modal, "cancel:", None, argtypes=[ctypes.c_void_p])
                os._exit(0)
        ticker = QTimer()
        ticker.setInterval(50)
        ticker.timeout.connect(tick)
        ticker.start()
        def go():
            found["opened"] = True
            widgets.open_file(window, "Choose a file")
        QTimer.singleShot(800, go)
        QTimer.singleShot(15000, lambda: (print(json.dumps(found)), sys.stdout.flush(),
                                          os._exit(0)))
        app.exec()
    """)
    env = {key: value for key, value in os.environ.items()
           if key != "QT_QPA_PLATFORM"}
    # As above: another worker's window can take the focus in the full
    # suite, and then the focus is rightly not taken back.
    interrupted = []
    for _attempt in range(3):
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=60, env=env, cwd=str(ROOT))
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        assert lines, done.stderr[-2000:]
        found = json.loads(lines[-1])
        if not found["states"] or found["states"][0][1] != "ApplicationActive":
            pytest.skip(f"the app never became active here: {found['states']}")
        if found["up_at"] is None:
            pytest.skip(f"no file panel came up here: {found}")
        before = [name for at, name in found["states"] if at <= found["up_at"]]
        after = [name for at, name in found["states"] if at > found["up_at"]]
        if before[-1:] != ["ApplicationActive"]:
            # Another app had the focus when the panel came up, and then
            # nothing is to be done: the focus is the person's.
            interrupted.append(found)
            continue
        if after[:2] != ["ApplicationInactive", "ApplicationActive"]:
            if [name for name in found["away_fronts"] if name != "com.apple.dock"]:
                # Another app took the focus while it was away, and then it
                # rightly stays away.
                interrupted.append(found)
                continue
            assert after[:2] == ["ApplicationInactive", "ApplicationActive"], found
        if len(after) > 2 and not (found["key"] and found["active"]):
            interrupted.append(found)
            continue
        assert found["key"] and found["active"], found
        return
    pytest.skip(f"another app took the focus every time: {interrupted}")
