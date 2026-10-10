"""A change of look: what it builds again, and what it must keep drawing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def looked(qapp):
    """An application with the look applied, as the app starts it."""
    import theme

    theme.apply(qapp, "dark", "normal", False)
    return qapp


class TestAChangeOfLookKeepsTheArrows:
    def test_the_arrows_style_is_built_once(self, looked):
        """Asked only by type, each change of look built a new ArrowStyle and
        restyled every widget for it, on top of the restyle the look needs:
        Settings previews a look as it is chosen."""
        import theme

        first = looked._arrow_style
        for mode in ("light", "dark", "system", "light"):
            theme.apply(looked, mode, "maximum", True)
        assert looked._arrow_style is first, "a change of look built it again"
        assert theme._arrows_in_place(looked)

    def test_the_arrows_are_still_its_own_after_many_changes(self):
        """In a process of its own, from where the app starts. A test that
        puts back an empty stylesheet leaves the application as the app never
        is, and a look laid over that draws arrows without the arrows' style,
        now as before."""
        script = textwrap.dedent(f"""
            import json, os, sys, tempfile
            os.environ["ICLOUD_TRIAGE_HOME"] = tempfile.mkdtemp()
            sys.path.insert(0, {str(ROOT)!r})
            from PySide6.QtCore import QRect, Qt
            from PySide6.QtGui import QImage, QPainter
            from PySide6.QtWidgets import (QApplication, QStyle, QStyleOption,
                                           QToolButton)
            app = QApplication([])
            import theme
            for mode in ("dark", "light", "system", "dark", "light"):
                theme.apply(app, mode, "normal", False)
            drawn = []
            real = theme.ArrowStyle.drawPrimitive

            def watched(self, element, option, painter, widget=None):
                drawn.append(element.name)
                return real(self, element, option, painter, widget)

            theme.ArrowStyle.drawPrimitive = watched
            image = QImage(24, 24, QImage.Format.Format_ARGB32_Premultiplied)
            painter = QPainter(image)
            option = QStyleOption()
            option.rect = QRect(0, 0, 24, 24)
            option.palette = app.palette()
            option.state = QStyle.StateFlag.State_Enabled
            app.style().drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorArrowDown,
                                      option, painter, None)
            painter.end()
            asked = list(drawn)
            button = QToolButton()
            button.setArrowType(Qt.ArrowType.DownArrow)
            button.resize(28, 28)
            button.grab()
            print(json.dumps({{"asked": asked, "button": drawn[len(asked):]}}))
        """)
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        done = subprocess.run([sys.executable, "-c", script],
                              capture_output=True, text=True, timeout=60,
                              env=env, cwd=str(ROOT))
        lines = [line for line in done.stdout.splitlines()
                 if line.startswith("{")]
        assert done.returncode == 0 and lines, done.stderr[-2000:]
        got = json.loads(lines[-1])
        assert "PE_IndicatorArrowDown" in got["asked"], (
            "the application's style does not hand its arrows to the arrows' "
            "style")
        assert "PE_IndicatorArrowDown" in got["button"], (
            "a tool button's arrow is not drawn by the app's own style")
