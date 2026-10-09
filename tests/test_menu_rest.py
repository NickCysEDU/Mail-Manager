"""A menu row the pointer rests on opens, as in the Mac's own menus.

Qt's menus on the Mac lost the row under the pointer after a visit to
another row's submenu: rested on, it stayed shut until the pointer moved
again, so the model menu seemed not to open some of its rows.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def menu(qapp):
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QMenu

    made = QMenu()
    first = made.addMenu("First")
    first.addAction("One")
    second = made.addMenu("Second")
    second.addAction("Two")
    made.addSeparator()
    plain = made.addAction("Plain")
    off = made.addMenu("Off")
    off.addAction("Three")
    off.menuAction().setEnabled(False)
    made.popup(QPoint(100, 100))
    qapp.processEvents()
    yield made, first, second, plain, off
    for each in (first, second, off, made):
        each.close()
    made.deleteLater()


def _rest_on(rest, menu, action):
    rest._at = menu.mapToGlobal(menu.actionGeometry(action).center())
    rest._rested()


class TestARowRestedOn:
    def test_opens_its_submenu_in_place_of_anothers(self, qapp, menu, qtbot):
        import widgets

        made, first, second, _plain, _off = menu
        made.setActiveAction(first.menuAction())
        qtbot.waitUntil(first.isVisible, timeout=2000)
        rest = widgets.RestingOpensMenus()
        _rest_on(rest, made, second.menuAction())
        assert made.activeAction() is second.menuAction()
        qtbot.waitUntil(second.isVisible, timeout=2000)
        assert not first.isVisible()

    def test_leaves_alone_what_has_no_submenu_or_is_off(self, qapp, menu,
                                                        qtbot):
        import widgets

        made, first, _second, plain, off = menu
        made.setActiveAction(first.menuAction())
        qtbot.waitUntil(first.isVisible, timeout=2000)
        rest = widgets.RestingOpensMenus()
        for action in (plain, off.menuAction()):
            _rest_on(rest, made, action)
            assert made.activeAction() is first.menuAction()
        assert not off.isVisible()

    def test_only_a_still_pointer_counts(self, qapp, menu, qtbot):
        """Every move puts the wait back to the start: a pointer on its way
        to a submenu, over the rows between, never rests on them."""
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        import widgets

        made, _first, second, _plain, _off = menu
        rest = widgets.RestingOpensMenus()
        at = made.actionGeometry(second.menuAction()).center()
        move = QMouseEvent(QEvent.Type.MouseMove, QPointF(at),
                           QPointF(made.mapToGlobal(at)),
                           Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
                           Qt.KeyboardModifier.NoModifier)
        assert rest.eventFilter(made, move) is False
        assert rest._rest.isActive()
        assert rest._at == made.mapToGlobal(at)
        qtbot.wait(100)
        rest.eventFilter(made, move)
        assert rest._rest.remainingTime() > 100, "a move did not start the wait again"
        rest._rest.stop()
        assert rest.eventFilter(made, QEvent(QEvent.Type.Show)) is False
        assert not rest._rest.isActive()

    def test_where_menus_overlap_the_one_in_front_is_rested_on(
            self, qapp, menu, monkeypatch):
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication, QMenu

        import widgets

        made, *_rest = menu
        front = QMenu()
        front.addAction("Over")
        front.popup(made.mapToGlobal(QPoint(10, 10)))
        qapp.processEvents()
        try:
            point = front.mapToGlobal(front.rect().center())
            assert made.geometry().contains(point)
            # The one behind listed first.
            monkeypatch.setattr(QApplication, "topLevelWidgets",
                                staticmethod(lambda: [made, front]))
            assert widgets._menu_at(point) is front
        finally:
            front.close()


def test_the_app_installs_it():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    called = {node.func.attr for node in ast.walk(tree)
              if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Attribute)}
    assert "install_resting_opens_menus" in called


@pytest.mark.skipif(sys.platform != "darwin", reason="the Mac's menus")
@pytest.mark.timeout(180)
def test_the_model_menu_opens_the_row_come_back_to(qapp):
    """The way it happened: onto one backend, into its models, back out
    onto another backend, and still. Driven through the window system's own
    path for pointer events, on the Mac's own platform; the pointer itself
    is never moved."""
    from test_gpu_canvas import real_platform_or_skip

    real_platform_or_skip()
    script = textwrap.dedent(f"""
        import json, os, sys, tempfile, time
        os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
        sys.path.insert(0, {str(ROOT)!r})
        from PySide6.QtCore import QTimer
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QMenu
        app = QApplication([])
        import widgets
        widgets.install_resting_opens_menus(app)
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow
        window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                            InMemoryCredentialStore(), demo=True)
        window.show()
        out = {{"states": []}}
        state = {{}}
        app.applicationStateChanged.connect(
            lambda changed: out["states"].append(changed.name))

        def move_to(goal, steps=10):
            start = state.get("at", goal)
            for step in range(1, steps + 1):
                at = start + (goal - start) * step / steps
                under = QApplication.widgetAt(at) or QApplication.activePopupWidget()
                top = under.window()
                QTest.mouseMove(top.windowHandle(), top.mapFromGlobal(at))
                app.processEvents()
            state["at"] = goal

        def wait_for(check, ms):
            ends = time.monotonic() + ms / 1000
            while not check() and time.monotonic() < ends:
                app.processEvents()
                time.sleep(0.01)
            return check()

        def go():
            menu = window.model_menu
            rows = [a for a in menu.actions() if a.menu() is not None]
            centre = lambda a: menu.mapToGlobal(menu.actionGeometry(a).center())
            first, other = rows[2], rows[4]
            state["at"] = menu.mapToGlobal(menu.rect().center())
            move_to(centre(first))
            out["first"] = wait_for(first.menu().isVisible, 1500)
            sub = first.menu()
            item = [a for a in sub.actions() if not a.isSeparator()][0]
            move_to(sub.mapToGlobal(sub.actionGeometry(item).center()))
            wait_for(lambda: False, 300)
            # Nothing here closes it: closed now, something outside did.
            out["still_open"] = menu.isVisible()
            move_to(centre(other))
            out["other"] = wait_for(other.menu().isVisible, 1500)
            out["active"] = menu.activeAction() is other
            while QApplication.activePopupWidget():
                QApplication.activePopupWidget().close()
            print(json.dumps(out), flush=True)
            os._exit(0)

        QTimer.singleShot(500, lambda: (QTimer.singleShot(400, go),
                                        window.model_button.showMenu()))
        QTimer.singleShot(20000, lambda: (print(json.dumps(out), flush=True),
                                          os._exit(1)))
        app.exec()
    """)
    env = {key: value for key, value in os.environ.items()
           if key != "QT_QPA_PLATFORM"}
    # An app another test worker brings forward closes the menu. Such a run
    # says nothing either way, so it is tried again; a menu still open that
    # does not open the row is the fault this test is for.
    interrupted = []
    for _attempt in range(3):
        done = subprocess.run([sys.executable, "-c", script], capture_output=True,
                              text=True, timeout=50, env=env, cwd=str(ROOT))
        lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
        assert done.returncode == 0 and lines, done.stderr[-2000:]
        result = json.loads(lines[-1])
        assert result["first"], "the first row's models never opened"
        if not result["still_open"]:
            interrupted.append(result)
            continue
        assert result["other"] and result["active"], result
        return
    pytest.skip(f"something outside closed the menu on every try: {interrupted}")
