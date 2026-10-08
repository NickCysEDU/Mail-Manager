"""Crashes seen in use, each reproduced in a process of its own: a crash in
Qt takes the whole process with it, so the suite could not report it from
inside."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*parts: str) -> subprocess.CompletedProcess:
    """Run the parts as one script, each dedented on its own."""
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    script = "".join(textwrap.dedent(part) for part in parts)
    return subprocess.run([sys.executable, "-c", script],
                          capture_output=True, text=True, timeout=50,
                          env=env, cwd=str(ROOT))


WINDOW = f"""
    import os, sys, tempfile
    os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, {str(ROOT)!r})
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    app = QApplication([])
    from config import InMemoryCredentialStore, Settings
    from gui import MainWindow
    window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                        InMemoryCredentialStore(), demo=True)
    window._load_demo_data()
    window.resize(1200, 800)
    window.show()
    app.processEvents()
"""


class TestAClickAfterTheRowHeightChanged:
    """View → Row height told the filter the rows had moved without warning
    it first; it freed its map, the table's current row still pointed into
    it, and the next click on a message crashed the app."""

    def test_it_does_not_crash(self):
        done = _run(WINDOW, """
            window.table.selectRow(3)
            window.table.setFocus()
            window.table.sortByColumn(1, Qt.SortOrder.AscendingOrder)
            app.processEvents()
            for lines in (1, 2, 3, 5):
                window.preview.setFocus()
                window._set_density(lines)
                app.processEvents()
                rect = window.table.visualRect(window.proxy.index(1, 2))
                QTest.mouseClick(window.table.viewport(), Qt.MouseButton.LeftButton,
                                 Qt.KeyboardModifier.NoModifier, rect.center())
                app.processEvents()
            print("survived", flush=True)
        """)
        assert done.returncode == 0, done.stderr[-2000:]
        assert "survived" in done.stdout
        assert "wrong model" not in done.stderr, done.stderr[-2000:]

    def test_the_rows_still_change_height(self, qapp, tmp_path, monkeypatch):
        from config import InMemoryCredentialStore, Settings
        from gui import MainWindow

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        window = MainWindow(Settings(icloud_email="you@icloud.example").normalized(),
                            InMemoryCredentialStore(), demo=True)
        try:
            window._load_demo_data()
            window._set_density(1)
            short = window.table.rowHeight(0)
            window._set_density(5)
            assert window.table.rowHeight(0) > short
            assert all(delegate.lines == 5 for delegate in window._wrap_delegates)
            delegates = len(window.table.findChildren(type(window._wrap_delegates[0])))
            window._set_density(2)
            assert len(window.table.findChildren(
                type(window._wrap_delegates[0]))) == delegates, "a fresh set each time"
        finally:
            window.close()
            window.deleteLater()


def test_no_model_says_its_rows_moved_without_warning_first():
    """layoutChanged without layoutAboutToBeChanged leaves every persistent
    index of a proxy over the model pointing at freed memory."""
    offenders = []
    for path in sorted(ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            emitted = {node.func.value.attr
                       for node in ast.walk(function)
                       if isinstance(node, ast.Call)
                       and isinstance(node.func, ast.Attribute)
                       and node.func.attr == "emit"
                       and isinstance(node.func.value, ast.Attribute)}
            if "layoutChanged" in emitted and "layoutAboutToBeChanged" not in emitted:
                offenders.append(f"{path.name}:{function.lineno} {function.name}")
    assert offenders == []


class TestTheModelMenu:
    """Choosing a model, or Model settings, rebuilds the menu from inside
    the chosen entry's own signal; the old entries, made by the window, were
    never freed, and freed at once they would be freed under that signal."""

    def test_choosing_from_it_neither_crashes_nor_leaves_entries_behind(self):
        done = _run(WINDOW, """
            from PySide6.QtCore import QCoreApplication, QEvent, QTimer
            from PySide6.QtGui import QAction
            from PySide6.QtWidgets import QMenu

            def dismiss():
                modal = QApplication.activeModalWidget()
                if modal is not None:
                    modal.reject() if hasattr(modal, "reject") else modal.close()

            closer = QTimer()
            closer.timeout.connect(dismiss)
            closer.start(30)

            def count():
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                return (len(window.findChildren(QAction)),
                        len(window.findChildren(QMenu)))

            before = count()
            for _ in range(2):
                # Read again after each: a choice makes the entries anew.
                for index in range(len(window._model_actions())):
                    window._model_actions()[index].trigger()
                    app.processEvents()
                settings = [a for a in window.model_menu.actions()
                            if a.text().startswith("Model settings")]
                settings[0].trigger()
                app.processEvents()
            print("counts", before, count(), flush=True)
            print("survived", flush=True)
        """)
        assert done.returncode == 0, done.stderr[-2000:]
        assert "survived" in done.stdout
        line = next(l for l in done.stdout.splitlines() if l.startswith("counts"))
        before, after = line[len("counts "):].split(") (")
        assert before + ")" == "(" + after, line
