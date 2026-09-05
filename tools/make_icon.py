#!/usr/bin/env python3
"""Generate the application icon (PNG + .icns) with QPainter.

No image library needed - PySide6 is already a dependency, and Qt gives real
antialiasing and gradients. Run from the project root:

    python tools/make_icon.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QImage, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"

SIZE = 1024
BG_TOP = QColor("#3B6FF0")
BG_BOTTOM = QColor("#7A3FE0")
ENVELOPE = QColor("#FFFFFF")
ENVELOPE_LINE = QColor("#2A50B8")
BADGE = QColor("#34C759")
SHADOW = QColor(0, 0, 0, 45)


def draw_icon(size: int = SIZE) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    scale = size / 1024.0
    painter.scale(scale, scale)

    # --- rounded-square background -------------------------------------
    body = QRectF(64, 64, 896, 896)
    gradient = QLinearGradient(body.topLeft(), body.bottomRight())
    gradient.setColorAt(0.0, BG_TOP)
    gradient.setColorAt(1.0, BG_BOTTOM)
    background = QPainterPath()
    background.addRoundedRect(body, 205, 205)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(gradient))
    painter.drawPath(background)

    # A soft inner highlight keeps it from looking flat at large sizes.
    highlight = QLinearGradient(body.topLeft(), QPointF(body.left(), body.center().y()))
    highlight.setColorAt(0.0, QColor(255, 255, 255, 46))
    highlight.setColorAt(1.0, QColor(255, 255, 255, 0))
    painter.setBrush(QBrush(highlight))
    painter.drawPath(background)

    # --- envelope -------------------------------------------------------
    envelope = QRectF(232, 318, 560, 396)
    painter.setBrush(QBrush(SHADOW))
    painter.drawRoundedRect(envelope.translated(0, 14), 40, 40)
    painter.setBrush(QBrush(ENVELOPE))
    painter.drawRoundedRect(envelope, 40, 40)

    # The flap, clipped to the envelope so the strokes cannot spill out.
    painter.save()
    clip = QPainterPath()
    clip.addRoundedRect(envelope, 40, 40)
    painter.setClipPath(clip)

    flap = QPainterPath()
    flap.moveTo(envelope.left() + 26, envelope.top() + 34)
    flap.lineTo(envelope.center().x(), envelope.top() + 246)
    flap.lineTo(envelope.right() - 26, envelope.top() + 34)
    pen = QPen(ENVELOPE_LINE, 34)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(flap)
    painter.restore()

    # --- approval badge -------------------------------------------------
    badge_center = QPointF(756, 704)
    radius = 158.0
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(255, 255, 255)))
    painter.drawEllipse(badge_center, radius + 20, radius + 20)
    painter.setBrush(QBrush(BADGE))
    painter.drawEllipse(badge_center, radius, radius)

    check = QPainterPath()
    check.moveTo(badge_center.x() - 74, badge_center.y() + 4)
    check.lineTo(badge_center.x() - 18, badge_center.y() + 60)
    check.lineTo(badge_center.x() + 78, badge_center.y() - 58)
    check_pen = QPen(QColor("#FFFFFF"), 40)
    check_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    check_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(check_pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(check)

    painter.end()
    return image


def build() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])  # noqa: F841
    ASSETS.mkdir(parents=True, exist_ok=True)

    master = draw_icon(SIZE)
    png_path = ASSETS / "icon.png"
    master.save(str(png_path), "PNG")
    print(f"wrote {png_path}")

    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()

    # The exact set `iconutil` expects.
    for base in (16, 32, 128, 256, 512):
        for scale in (1, 2):
            pixels = base * scale
            suffix = "" if scale == 1 else "@2x"
            rendered = draw_icon(pixels)
            rendered.save(str(iconset / f"icon_{base}x{base}{suffix}.png"), "PNG")

    icns_path = ASSETS / "icon.icns"
    if shutil.which("iconutil"):
        result = subprocess.run(
            ["iconutil", "-c", "icns", str(iconset), "-o", str(icns_path)],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"wrote {icns_path}")
            shutil.rmtree(iconset)
        else:
            print(f"iconutil failed: {result.stderr.strip()}", file=sys.stderr)
            return 1
    else:  # pragma: no cover - non-macOS
        print("iconutil not found; left the .iconset directory in place.")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
