#!/usr/bin/env python3
"""Draw the background for the installer window.

Finder sizes the window from the background image's pixel dimensions, so the
image is drawn at 1x and 2x and combined into a single TIFF with both
representations. That is what keeps it sharp on a Retina display without the
window coming out twice the size it should be.

    python tools/make_dmg_background.py
"""

from __future__ import annotations

import math
import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
)
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

#: Window size in points. The icons are placed against these coordinates in
#: build_dmg.sh, so the two have to agree.
WIDTH, HEIGHT = 660, 420
APP_SPOT = (170, 214)
APPS_SPOT = (490, 214)

INK = QColor("#15181D")
DIM = QColor("#5A626E")
ACCENT = QColor("#2F6FE0")
GROUND_TOP = QColor("#FBFCFD")
GROUND_BOTTOM = QColor("#EDF1F7")


def draw(scale: int = 1) -> QImage:
    width, height = WIDTH * scale, HEIGHT * scale
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.white)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(scale, scale)

    # -- ground ----------------------------------------------------------
    ground = QLinearGradient(0, 0, 0, HEIGHT)
    ground.setColorAt(0.0, GROUND_TOP)
    ground.setColorAt(1.0, GROUND_BOTTOM)
    painter.fillRect(QRectF(0, 0, WIDTH, HEIGHT), QBrush(ground))

    # A single hairline under the header, rather than a box around everything.
    painter.setPen(QPen(QColor(0, 0, 0, 22), 1))
    painter.drawLine(QPointF(48, 96), QPointF(WIDTH - 48, 96))

    # -- header ----------------------------------------------------------
    title = QFont()
    title.setPointSizeF(25)
    title.setWeight(QFont.Weight.DemiBold)
    painter.setFont(title)
    painter.setPen(INK)
    painter.drawText(QRectF(0, 34, WIDTH, 34),
                     int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                     "Mail Manager")

    caption = QFont()
    caption.setPointSizeF(12.5)
    painter.setFont(caption)
    painter.setPen(DIM)
    painter.drawText(QRectF(0, 63, WIDTH, 24),
                     int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                     "Sorts your mail into folders. Runs on your Mac.")

    # -- the instruction, which is the whole point of this window ---------
    start = QPointF(APP_SPOT[0] + 62, APP_SPOT[1])
    end = QPointF(APPS_SPOT[0] - 62, APP_SPOT[1])
    painter.setPen(QPen(ACCENT, 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.drawLine(start, end)

    head = QPainterPath()
    head.moveTo(end.x() + 13, end.y())
    head.lineTo(end.x() - 7, end.y() - 8)
    head.lineTo(end.x() - 7, end.y() + 8)
    head.closeSubpath()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(ACCENT))
    painter.drawPath(head)

    label = QFont()
    label.setPointSizeF(12)
    label.setWeight(QFont.Weight.Medium)
    painter.setFont(label)
    painter.setPen(ACCENT)
    # Above the icons rather than beside them: a 128 point icon centred on
    # APP_SPOT reaches up to y=150, and Finder writes each icon's name
    # underneath it, so both of those bands have to be left clear.
    painter.drawText(QRectF(0, 118, WIDTH, 22),
                     int(Qt.AlignmentFlag.AlignCenter), "Drag to install")

    # -- footer ------------------------------------------------------------
    footer = QFont()
    footer.setPointSizeF(10.5)
    painter.setFont(footer)
    painter.setPen(QColor(90, 98, 110, 210))
    painter.drawText(QRectF(0, HEIGHT - 46, WIDTH, 20),
                     int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                     "No API key needed  ·  Nothing leaves your Mac by default")
    painter.drawText(QRectF(0, HEIGHT - 28, WIDTH, 18),
                     int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                     "github.com/NickCysEDU/Mail-Manager")

    painter.end()
    return image


def build() -> int:
    QApplication.instance() or QApplication(sys.argv[:1])
    ASSETS.mkdir(parents=True, exist_ok=True)

    one = ASSETS / "dmg-background.png"
    two = ASSETS / "dmg-background@2x.png"
    draw(1).save(str(one), "PNG")
    draw(2).save(str(two), "PNG")
    print(f"wrote {one.relative_to(ROOT)}  ({WIDTH}x{HEIGHT})")
    print(f"wrote {two.relative_to(ROOT)}  ({WIDTH * 2}x{HEIGHT * 2})")

    combined = ASSETS / "dmg-background.tiff"
    if shutil.which("tiffutil"):
        result = subprocess.run(
            ["tiffutil", "-cathidpicheck", str(one), str(two), "-out", str(combined)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"wrote {combined.relative_to(ROOT)}  (1x and 2x in one file)")
        else:
            print(f"tiffutil failed: {result.stderr.strip()}", file=sys.stderr)
            return 1
    else:  # pragma: no cover - not macOS
        print("tiffutil not found; the PNGs are still usable at 1x.")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
