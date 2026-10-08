"""Somebody who has never seen the app, trying everything: hundreds of
random clicks, keys, filters, resizes, looks, menus, message windows,
dialogs and the visualiser, on the demo's sample inbox, in a process of its
own so a crash is seen as one.

Every Python exception that reaches Qt, and every Qt warning that means a
fault, fails the run; so does the process ending other than cleanly. A
failure prints its seed and its last steps: ``MONKEY_SEED`` runs that one
again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Qt's warnings that say nothing is wrong: the offscreen platform's own.
HARMLESS = ("propagateSizeHints", "This plugin does not support",
            "Populating font family aliases", "QFont::setPointSizeF",
            "outside any known screen")

SCRIPT = textwrap.dedent("""
    import json, os, random, sys, tempfile, traceback
    os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, {root!r})
    faults = []
    steps = []

    def caught(kind, value, trace):
        faults.append("".join(traceback.format_exception(kind, value, trace))[-1500:])
    sys.excepthook = caught

    from PySide6.QtCore import (QCoreApplication, QEvent, QPoint, Qt, QTimer,
                                qInstallMessageHandler, QtMsgType)
    from PySide6.QtGui import QKeySequence
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMenu
    warnings = []

    def heard(kind, _context, text):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg,
                    QtMsgType.QtFatalMsg):
            warnings.append(text)
    qInstallMessageHandler(heard)
    app = QApplication([])
    import theme, widgets
    widgets.install_resting_opens_menus(app)
    from config import InMemoryCredentialStore, Settings
    from gui import MainWindow
    window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                        InMemoryCredentialStore(), demo=True)
    window._load_demo_data()
    window.resize(1200, 800)
    window.show()
    app.processEvents()

    # Whatever opens modally is answered, as a person closing it would.
    def dismiss():
        modal = QApplication.activeModalWidget()
        if modal is not None:
            modal.reject() if hasattr(modal, "reject") else modal.close()
        popup = QApplication.activePopupWidget()
        if popup is not None:
            popup.close()
    closer = QTimer()
    closer.timeout.connect(dismiss)
    closer.start(25)

    rng = random.Random({seed})
    words = ["", "a", "job", "interview", "%", "(", ".*", "\\\\", "日本語",
             "x" * 3000, "<script>", "' OR 1=1 --", "\\u202e", "🙂" * 50]

    def table_point():
        rows = window.proxy.rowCount()
        if not rows:
            return None
        index = window.proxy.index(rng.randrange(rows),
                                   rng.randrange(window.proxy.columnCount()))
        rect = window.table.visualRect(index)
        return rect.center() if rect.isValid() else None

    def click_row():
        at = table_point()
        if at is not None:
            QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, at)

    def double_click_row():
        at = table_point()
        if at is not None:
            QTest.mouseDClick(window.table.viewport(), Qt.MouseButton.LeftButton,
                              Qt.KeyboardModifier.NoModifier, at)

    def shift_click_row():
        at = table_point()
        if at is not None:
            QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.ShiftModifier, at)

    def sort():
        window.table.sortByColumn(rng.randrange(window.proxy.columnCount()),
                                  rng.choice([Qt.SortOrder.AscendingOrder,
                                              Qt.SortOrder.DescendingOrder]))

    def search():
        window.search_edit.setText(rng.choice(words))

    def combos():
        from PySide6.QtWidgets import QComboBox
        boxes = [box for box in window.findChildren(QComboBox) if box.isVisible()
                 and box.isEnabled() and box.count()]
        if boxes:
            box = rng.choice(boxes)
            box.setCurrentIndex(rng.randrange(box.count()))

    def density():
        window._set_density(rng.randint(1, 6))

    def sidebar():
        window.sidebar_action.trigger()

    def look():
        window.settings.readable = rng.random() < 0.5
        window.settings.contrast = rng.choice(["normal", "high", "maximum"])
        window.settings.appearance_mode = rng.choice(["system", "light", "dark"])
        window.settings.density = rng.choice(["comfortable", "compact", "dense"])
        window.apply_appearance()

    def keys():
        sequence = rng.choice(["Ctrl+A", "Ctrl+Shift+A", "Ctrl+D", "Ctrl+Z",
                               "Ctrl+F", "Esc", "Ctrl+1", "Ctrl+2", "Ctrl+3",
                               "Ctrl+L", "Ctrl+0", "Return", "Space", "Up",
                               "Down", "Home", "End", "Ctrl+Shift+U",
                               "Ctrl+Shift+L", "Meta+Ctrl+S", "Delete", "Tab"])
        key = QKeySequence(sequence)[0]
        target = QApplication.focusWidget() or window.table
        QTest.keyClick(target, key.key(), key.keyboardModifiers())

    def tick():
        rows = window.proxy.rowCount()
        if rows:
            from triage_table import TriageTableModel
            index = window.proxy.index(rng.randrange(rows),
                                       TriageTableModel.COL_SELECT)
            window.proxy.setData(index, rng.choice([Qt.CheckState.Checked,
                                                    Qt.CheckState.Unchecked]),
                                 Qt.ItemDataRole.CheckStateRole)

    def body_mode():
        box = window.preview.body_mode
        box.setCurrentIndex(rng.randrange(box.count()))

    def resize():
        window.resize(rng.randint(200, 1800), rng.randint(150, 1100))

    def menus():
        for menu in (window.model_menu, window.sorting_menu, window.account_menu):
            menu.popup(window.mapToGlobal(QPoint(40, 40)))
            app.processEvents()
            menu.close()

    def table_menu():
        click_row()
        made = window.build_table_menu()
        if made is not None:
            made.deleteLater()

    def message_window():
        rows = window.proxy.rowCount()
        if not rows:
            return
        row = window.proxy.mapToSource(window.proxy.index(
            rng.randrange(rows), 0)).row()
        window.open_message(row)
        app.processEvents()
        for opened in window._live_mail_windows():
            if hasattr(opened, "next_action") and rng.random() < 0.5:
                opened.next_action.trigger()
            if rng.random() < 0.6:
                opened.close()

    def settings():
        QTimer.singleShot(60, dismiss)
        window.open_settings()

    def quick_action():
        click_row()
        rows = window._selected_rows()
        if rows:
            window.act_on_rows(rng.choice(["read", "unread", "flag", "unflag"]),
                               rows)

    def visualiser():
        from attachment_view import AttachmentViewer
        viewer = AttachmentViewer([], "", window, library=True)
        viewer.show()
        app.processEvents()
        pane = viewer.audio
        for _ in range(3):
            pane.enable_box.setChecked(rng.random() < 0.7)
            pane.scene_box.setCurrentIndex(rng.randrange(pane.scene_box.count()))
            pane.strobe_box.setChecked(rng.random() < 0.3)
            app.processEvents()
        viewer.close()
        viewer.deleteLater()

    def buttons():
        for button in (window.scan_button, window.apply_button):
            if rng.random() < 0.5 and button.isEnabled():
                button.click()
        if rng.random() < 0.3:
            window.stop_action.trigger()

    def undo():
        window.undo_action.trigger() if hasattr(window, "undo_action") else None

    def compose():
        window.compose("new")
        app.processEvents()
        for opened in window._live_mail_windows():
            editor = getattr(opened, "editor", None)
            if editor is not None:
                editor.setPlainText(rng.choice(words))
            opened.close()

    def briefing():
        QTest.keyClick(window, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
        app.processEvents()
        for top in QApplication.topLevelWidgets():
            if top.isVisible() and top.windowTitle() == "Briefing":
                top.close()

    def row_extras():
        click_row()
        rows = window._selected_rows()
        if rows:
            window._open_attachments(rows[0])
            app.processEvents()
            for top in QApplication.topLevelWidgets():
                if top.isVisible() and top.windowTitle() in ("Attachments", "Links"):
                    top.close()

    def columns():
        header = window.table.horizontalHeader()
        column = rng.randrange(header.count())
        window.table.setColumnHidden(column, not window.table.isColumnHidden(column))
        if rng.random() < 0.3:
            window._reset_columns()

    def help_mode():
        window.help_button.click()

    def zoom():
        if window.isMaximized():
            window.showNormal()
        else:
            window.showMaximized()

    def hide_and_back():
        window.hide()
        app.processEvents()
        window._reveal()

    def tiny():
        window.resize(rng.randint(1, 120), rng.randint(1, 90))

    actions = [click_row, click_row, click_row, double_click_row,
               shift_click_row, sort, search, combos, density, sidebar, look,
               keys, keys, keys, tick, body_mode, resize, menus, table_menu,
               message_window, settings, quick_action, visualiser, buttons,
               undo, compose, briefing, row_extras, columns, help_mode, zoom,
               hide_and_back, tiny]
    for step in range({steps}):
        action = rng.choice(actions)
        steps.append(action.__name__)
        try:
            action()
        except Exception:
            faults.append(f"step {{step}} {{action.__name__}}: "
                          + traceback.format_exc()[-1500:])
        for _ in range(2):
            app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        if faults:
            break
    window.shutdown()
    print(json.dumps({{"faults": faults, "warnings": warnings,
                       "last": steps[-12:], "done": len(steps)}}), flush=True)
    os._exit(0)
""")


def _monkey(seed: int, steps: int) -> dict:
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT.format(root=str(ROOT), seed=seed,
                                             steps=steps)],
        capture_output=True, text=True, timeout=600, env=env, cwd=str(ROOT))
    lines = [line for line in done.stdout.splitlines() if line.startswith("{")]
    assert done.returncode == 0 and lines, (
        f"seed {seed}: the process ended {done.returncode}\n"
        f"{done.stderr[-3000:]}")
    return json.loads(lines[-1])


@pytest.mark.timeout(900)
@pytest.mark.parametrize("seed", [
    int(os.environ["MONKEY_SEED"])] if os.environ.get("MONKEY_SEED") else [1, 2, 3])
def test_trying_everything_breaks_nothing(seed):
    result = _monkey(seed, int(os.environ.get("MONKEY_STEPS", "300")))
    assert result["done"] >= 1
    assert result["faults"] == [], (seed, result["last"], result["faults"])
    meant = [text for text in result["warnings"]
             if not any(part in text for part in HARMLESS)]
    assert meant == [], (seed, result["last"], meant[:10])
