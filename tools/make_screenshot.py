#!/usr/bin/env python3
"""Render the README screenshot from the app itself.

Taking one by hand means granting screen-recording permission, catching the
window at the right moment, and remembering to do it again the next time the
toolbar changes - which is why the one in the README was four features out of
date. Qt can draw a widget straight into an image with no screen involved, so
this does that instead: same window, same theme, same demo mail, every time.

    ./dev screenshot

Demo mode only. It cannot open a mailbox, and there is nothing in the picture
that belongs to anybody.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Before Qt is imported, or it picks the real screen.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: Wide enough that no column is elided and the preview reads properly.
WIDTH, HEIGHT = 1680, 1050

#: Where the README and the handbook look for it.
TARGETS = (ROOT / "docs" / "screenshot.png", ROOT / "docs" / "demo.png")


def render(width: int, height: int, dark: bool):
    from PySide6.QtCore import QCoreApplication, QEventLoop
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(sys.argv[:1])

    import config
    import theme
    from gui import MainWindow

    settings = config.Settings(icloud_email="you@icloud.example")
    settings.appearance_mode = "dark" if dark else "light"
    # Painted before the window is built, and with the arguments theme.apply
    # actually takes - handing it the settings object silently gave the
    # system theme, which on a build machine is whatever that machine is set
    # to rather than what was asked for.
    theme.apply(app, settings.appearance_mode, settings.contrast,
                settings.readable, settings.density)
    window = MainWindow(settings, config.InMemoryCredentialStore(), demo=True)
    window.resize(width, height)
    window.show()

    window._load_demo_data()
    # Select the first row so the preview has something in it - an empty pane
    # under a full table is not what the app looks like in use.
    if window.proxy.rowCount():
        window.table.selectRow(0)
    for _ in range(6):
        QCoreApplication.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 50)
    window.repaint()

    image = window.grab().toImage()
    window.close()
    return image


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="make_screenshot", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--width", type=int, default=WIDTH)
    parser.add_argument("--height", type=int, default=HEIGHT)
    parser.add_argument("--light", action="store_true",
                        help="Render the light theme instead of the dark one.")
    parser.add_argument("--out", action="append", default=[],
                        help="Where to write it. Repeatable; defaults to both "
                             "of the paths the docs use.")
    args = parser.parse_args(argv)

    image = render(args.width, args.height, dark=not args.light)
    targets = [Path(p) for p in args.out] or list(TARGETS)
    for path in targets:
        path.parent.mkdir(parents=True, exist_ok=True)
        if not image.save(str(path)):
            print(f"could not write {path}", file=sys.stderr)
            return 1
        print(f"{path}  {image.width()}x{image.height()}  "
              f"{path.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
