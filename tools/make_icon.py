#!/usr/bin/env python3
"""Draw the application icon and every export that ships with it.

Everything is vector work done with QPainter, so there is no image library to
install - PySide6 is already a dependency and it gives real antialiasing.

    python tools/make_icon.py

Outputs:
    assets/icon.png                 1024 master
    assets/icon.icns                the macOS bundle icon
    docs/assets/icon-512.png        README and download page
    docs/assets/icon-128.png
    docs/assets/favicon-32.png      GitHub Pages tab icon
    docs/assets/apple-touch-icon.png
    docs/assets/social-preview.png  1280x640, for the GitHub repository card
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
    QRadialGradient,
)
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
DOCS_ASSETS = ROOT / "docs" / "assets"

SIZE = 1024

# macOS draws app icons on a 1024 grid with the artwork inset to 824 and a
# shadow in the margin. Matching that is what makes an icon sit correctly
# next to the system ones in the Dock and in Finder.
# 814 inset by 105 is what /System/Applications/Mail.app measures at, and
# matching it is what makes the icon sit level with the system ones.
BODY = QRectF(105, 105, 814, 814)

# The macOS shape is a rectangle with straight edges and a smoothed corner, not
# a superellipse. Measured off Mail.app: the edge stays straight to 60% of the
# half-width, then turns through a corner that fits an exponent of 1.8. Those
# two numbers reproduce its outline to within half a percent at every angle.
CORNER_SPAN = 0.40
CORNER_N = 1.8

# The same blue as the Scan & Analyze button, so the app, its icon and its
# primary action are visibly one thing. Two shades of it and white; anything
# more starts to look busy at the sizes this actually gets seen at.
SKY = QColor("#4A86EE")
DEEP = QColor("#2F6FE0")
PAPER = QColor("#FFFFFF")


def _corner(a: float, b: float, c: float, sx: int, sy: int, samples: int):
    """One corner, running from the horizontal edge round to the vertical one."""
    points = []
    for i in range(samples + 1):
        v = i / samples
        u = (1.0 - v ** CORNER_N) ** (1.0 / CORNER_N)
        points.append((sx * ((a - c) + c * v), sy * ((b - c) + c * u)))
    return points


def squircle(rect: QRectF, samples: int = 96) -> QPainterPath:
    """The rounded shape macOS uses for app icons."""
    cx, cy = rect.center().x(), rect.center().y()
    a, b = rect.width() / 2.0, rect.height() / 2.0
    c = CORNER_SPAN * min(a, b)

    outline = [(-(a - c), -b)]
    outline += _corner(a, b, c, 1, -1, samples)              # top right
    outline += reversed(_corner(a, b, c, 1, 1, samples))     # bottom right
    outline += _corner(a, b, c, -1, 1, samples)              # bottom left
    outline += reversed(_corner(a, b, c, -1, -1, samples))   # top left

    path = QPainterPath()
    for i, (x, y) in enumerate(outline):
        point = QPointF(cx + x, cy + y)
        path.moveTo(point) if i == 0 else path.lineTo(point)
    path.closeSubpath()
    return path


def _draw_shadow(painter: QPainter) -> None:
    """Stacked outlines standing in for a blur, which keeps this dependency free."""
    painter.setPen(Qt.PenStyle.NoPen)
    layers = 20
    for i in range(layers, 0, -1):
        t = i / layers                       # 1.0 is the outermost, faintest ring
        grown = BODY.adjusted(-9 * t, -2 * t, 9 * t, 12 * t).translated(0, 9 * t)
        painter.setBrush(QBrush(QColor(30, 32, 62, max(1, int(5 * (1.0 - t) + 1)))))
        painter.drawPath(squircle(grown, 256))


def _draw_cycle(painter: QPainter, tiny: bool) -> None:
    """An arrow coming back round on itself: this happens without you.

    A full circle around the envelope rather than an arc behind it, so the two
    shapes stay legible as two shapes. Dropped below 96 points, where the ring
    closes up into a smudge and the envelope alone says more.
    """
    if tiny:
        return
    stroke = 38.0
    ring = QRectF(0, 0, 596, 596)
    ring.moveCenter(BODY.center())

    pen = QPen(PAPER, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    # Open at the top, centred. The gap used to run from 38 to 98 degrees,
    # which put its middle at 68 - up and to the right - and the whole mark
    # read as lopsided. Centring the opening on twelve o'clock puts the
    # arrowhead and the tail the same distance either side of the middle.
    #
    # Qt measures from three o'clock anticlockwise, so half the gap either
    # side of 90 is where the arc starts and ends.
    gap_deg = 60.0
    # The arrowhead is a triangle sticking out past the end of the arc, into
    # the opening, so centring the *arc's* gap on twelve o'clock still left
    # the visible opening off to the right by half the head's length. Both
    # ends rotate by half that, which puts the tail and the tip of the head
    # the same distance either side of vertical.
    radius = ring.width() / 2.0
    head_deg = math.degrees((stroke * 2.3) / radius)
    start_deg = 90.0 - gap_deg / 2.0 + head_deg / 2.0
    span_deg = -(360.0 - gap_deg)
    painter.drawArc(ring, int(start_deg * 16), int(span_deg * 16))

    # The head goes where the travel arrives, not where it sets off. It was
    # on the near end pointing back into the arc, which is why it read as a
    # lump rather than as direction. A plain triangle, longer than it is
    # wide: anything cleverer turns into a bird at small sizes.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(PAPER))
    angle = math.radians(start_deg + span_deg)
    base = QPointF(ring.center().x() + radius * math.cos(angle),
                   ring.center().y() - radius * math.sin(angle))
    # Clockwise motion at that point, in screen coordinates.
    along = QPointF(math.sin(angle), math.cos(angle))
    across = QPointF(along.y(), -along.x())

    length = stroke * 2.3
    half = stroke * 1.08

    def at(forward: float, sideways: float) -> QPointF:
        return QPointF(base.x() + along.x() * forward + across.x() * sideways,
                       base.y() + along.y() * forward + across.y() * sideways)

    head = QPainterPath()
    head.moveTo(at(length, 0.0))               # the point
    head.lineTo(at(-length * 0.34, half))      # one corner
    head.lineTo(at(-length * 0.34, -half))     # the other
    head.closeSubpath()
    painter.drawPath(head)


def _draw_envelope(painter: QPainter, body: QPainterPath, tiny: bool) -> None:
    """One white envelope. The flap is cut out of it rather than drawn on top,
    so the whole mark is two colours and stays crisp all the way down."""
    width = 560.0 if tiny else 366.0
    height = width * 0.6875                  # a 16:11 envelope, near enough to real
    envelope = QRectF(0, 0, width, height)
    envelope.moveCenter(BODY.center())
    radius = 34.0 if not tiny else 46.0

    painter.save()
    painter.setClipPath(body)
    _draw_cycle(painter, tiny)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(PAPER))
    painter.drawRoundedRect(envelope, radius, radius)

    # The flap, painted back in the body colour. Clipping it to the envelope
    # keeps the cut inside the rounded corners.
    clip = QPainterPath()
    clip.addRoundedRect(envelope, radius, radius)
    painter.setClipPath(clip, Qt.ClipOperation.IntersectClip)

    inset = height * 0.115
    flap = QPainterPath()
    flap.moveTo(envelope.left(), envelope.top())
    flap.lineTo(envelope.right(), envelope.top())
    flap.lineTo(envelope.right(), envelope.top() + inset)
    flap.lineTo(envelope.center().x(), envelope.top() + height * 0.60)
    flap.lineTo(envelope.left(), envelope.top() + inset)
    flap.closeSubpath()

    pen = QPen(SKY, 0.0)                     # only the join needs softening
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(QBrush(SKY))
    painter.drawPath(flap)
    painter.restore()


def draw_icon(size: int = SIZE) -> QImage:
    """Render at `size`. Small renders drop the shadow and grow the mark, since
    at 16 points a margin is just wasted pixels."""
    tiny = size < 96

    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.scale(size / 1024.0, size / 1024.0)

    body = squircle(BODY)

    if not tiny:
        _draw_shadow(painter)

    if tiny:
        painter.setBrush(QBrush(SKY))
    else:
        gradient = QLinearGradient(BODY.topLeft(), BODY.bottomLeft())
        gradient.setColorAt(0.0, SKY)
        gradient.setColorAt(1.0, DEEP)
        painter.setBrush(QBrush(gradient))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawPath(body)

    _draw_envelope(painter, body, tiny)

    painter.end()
    return image


def draw_social_preview(width: int = 1280, height: int = 640) -> QImage:
    """The card GitHub shows when the repository is linked somewhere."""
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    painter.fillRect(0, 0, width, height, QColor("#161726"))

    art = draw_icon(320)
    painter.drawImage(120, (height - 320) // 2, art)

    left = 520
    painter.setPen(QColor("#FFFFFF"))
    title = QFont()
    title.setPointSizeF(64)
    title.setWeight(QFont.Weight.Bold)
    painter.setFont(title)
    painter.drawText(QRectF(left, 214, width - left - 90, 90),
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                     "Mail Manager")

    painter.setPen(QColor("#B9BFE0"))
    body = QFont()
    body.setPointSizeF(27)
    painter.setFont(body)
    painter.drawText(QRectF(left, 304, width - left - 90, 130),
                     int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
                         | Qt.TextFlag.TextWordWrap),
                     "Sorts your job search mail into folders. Runs on your Mac, "
                     "offline by default.")

    painter.end()
    return image


def build() -> int:
    # Named and then used, because QPainter and QPixmap need a live
    # application object and letting this one be collected takes the process
    # down with it. Setting the name is the cheapest way to say so out loud.
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("Mail Manager icon builder")
    ASSETS.mkdir(parents=True, exist_ok=True)
    DOCS_ASSETS.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []

    master = ASSETS / "icon.png"
    draw_icon(SIZE).save(str(master), "PNG")
    written.append(master)

    for path, pixels in (
        (DOCS_ASSETS / "icon-512.png", 512),
        (DOCS_ASSETS / "icon-128.png", 128),
        (DOCS_ASSETS / "apple-touch-icon.png", 180),
        (DOCS_ASSETS / "favicon-32.png", 32),
    ):
        draw_icon(pixels).save(str(path), "PNG")
        written.append(path)

    social = DOCS_ASSETS / "social-preview.png"
    draw_social_preview().save(str(social), "PNG")
    written.append(social)

    iconset = ASSETS / "icon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    for base in (16, 32, 128, 256, 512):          # the exact set iconutil wants
        for scale in (1, 2):
            suffix = "" if scale == 1 else "@2x"
            draw_icon(base * scale).save(
                str(iconset / f"icon_{base}x{base}{suffix}.png"), "PNG")

    icns = ASSETS / "icon.icns"
    if shutil.which("iconutil"):
        result = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            print(f"iconutil failed: {result.stderr.strip()}", file=sys.stderr)
            return 1
        shutil.rmtree(iconset)
        written.append(icns)
    else:  # pragma: no cover - not macOS
        print("iconutil not found; leaving the .iconset directory in place.")

    for path in written:
        print(f"wrote {path.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(build())
