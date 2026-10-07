"""The small pictures on the mail buttons: an arrow back for a reply, two
for everyone, one forward, a flag, a box, a bin, and so on. Drawn here with
a pen rather than shipped as files, in the colour of the text they sit
beside, so they follow the theme and every size of screen.

Each is drawn once per colour and size and kept; a button asks for its
icon by name.
"""

from __future__ import annotations

from typing import Dict, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

#: The names a button can ask for.
NAMES = ("reply", "reply-all", "forward", "flag", "archive", "junk", "trash",
         "previous", "next", "attach", "send", "draft", "open", "compose",
         "read", "unread", "move")

_made: Dict[Tuple[str, str, int], QIcon] = {}


def icon(name: str, colour: str = "#d6d6d6", size: int = 18) -> QIcon:
    """The icon by name, in a colour, ``size`` points square."""
    key = (name, colour, size)
    found = _made.get(key)
    if found is None:
        found = _draw(name, QColor(colour), size)
        _made[key] = found
    return found


def _draw(name: str, colour: QColor, size: int) -> QIcon:
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    pen = QPen(colour)
    pen.setWidthF(size / 11.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    drawer = _DRAWERS.get(name)
    if drawer is not None:
        drawer(painter, size, colour)
    painter.end()
    return QIcon(pixmap)


# Every drawer works in a box of ``s`` by ``s`` with a margin of about a
# sixth, so the icons weigh the same beside each other.

def _arrow_back(painter: QPainter, s: float, colour: QColor, at: float = 0.0,
                mirrored: bool = False) -> None:
    """A curved arrow pointing left (a reply), or right when mirrored."""
    m = s * 0.16
    path = QPainterPath()
    if not mirrored:
        path.moveTo(at + m + s * 0.02, s * 0.42)
        path.cubicTo(at + s * 0.45, s * 0.38, at + s * 0.75, s * 0.40,
                     at + s - m, s * 0.80)
        painter.drawPath(path)
        head = QPainterPath()
        head.moveTo(at + m + s * 0.30, s * 0.16)
        head.lineTo(at + m + s * 0.02, s * 0.42)
        head.lineTo(at + m + s * 0.30, s * 0.68)
        painter.drawPath(head)
    else:
        path.moveTo(s - at - m - s * 0.02, s * 0.42)
        path.cubicTo(s - at - s * 0.45, s * 0.38, s - at - s * 0.75, s * 0.40,
                     at + m, s * 0.80)
        painter.drawPath(path)
        head = QPainterPath()
        head.moveTo(s - at - m - s * 0.30, s * 0.16)
        head.lineTo(s - at - m - s * 0.02, s * 0.42)
        head.lineTo(s - at - m - s * 0.30, s * 0.68)
        painter.drawPath(head)


def _reply(painter, s, colour):
    _arrow_back(painter, s, colour)


def _reply_all(painter, s, colour):
    _arrow_back(painter, s, colour, at=s * 0.14)
    head = QPainterPath()
    head.moveTo(s * 0.30, s * 0.20)
    head.lineTo(s * 0.06, s * 0.42)
    head.lineTo(s * 0.30, s * 0.64)
    painter.drawPath(head)


def _forward(painter, s, colour):
    _arrow_back(painter, s, colour, mirrored=True)


def _flag(painter, s, colour):
    m = s * 0.18
    painter.drawLine(QPointF(m, m), QPointF(m, s - m))
    cloth = QPainterPath()
    cloth.moveTo(m, m + s * 0.02)
    cloth.lineTo(s - m, m + s * 0.02)
    cloth.lineTo(s - m - s * 0.18, m + s * 0.26)
    cloth.lineTo(s - m, m + s * 0.50)
    cloth.lineTo(m, m + s * 0.50)
    painter.drawPath(cloth)


def _archive(painter, s, colour):
    m = s * 0.16
    painter.drawRect(QRectF(m, m, s - 2 * m, s * 0.20))
    painter.drawRect(QRectF(m + s * 0.04, m + s * 0.20, s - 2 * m - s * 0.08,
                            s - 2 * m - s * 0.20))
    painter.drawLine(QPointF(s * 0.40, s * 0.58), QPointF(s * 0.60, s * 0.58))


def _trash(painter, s, colour):
    m = s * 0.18
    painter.drawLine(QPointF(m, m + s * 0.08), QPointF(s - m, m + s * 0.08))
    painter.drawLine(QPointF(s * 0.40, m), QPointF(s * 0.60, m))
    body = QPainterPath()
    body.moveTo(m + s * 0.06, m + s * 0.08)
    body.lineTo(m + s * 0.12, s - m)
    body.lineTo(s - m - s * 0.12, s - m)
    body.lineTo(s - m - s * 0.06, m + s * 0.08)
    painter.drawPath(body)
    painter.drawLine(QPointF(s * 0.42, s * 0.40), QPointF(s * 0.44, s * 0.72))
    painter.drawLine(QPointF(s * 0.58, s * 0.40), QPointF(s * 0.56, s * 0.72))


def _junk(painter, s, colour):
    m = s * 0.16
    painter.drawRect(QRectF(m, m + s * 0.24, s - 2 * m, s - 2 * m - s * 0.24))
    painter.drawLine(QPointF(m - s * 0.04, m + s * 0.24),
                     QPointF(s - m + s * 0.04, m + s * 0.24))
    painter.drawLine(QPointF(s * 0.38, s * 0.52), QPointF(s * 0.62, s * 0.76))
    painter.drawLine(QPointF(s * 0.62, s * 0.52), QPointF(s * 0.38, s * 0.76))


def _chevron(painter, s, up: bool):
    m = s * 0.26
    path = QPainterPath()
    if up:
        path.moveTo(m, s * 0.64)
        path.lineTo(s / 2, s * 0.34)
        path.lineTo(s - m, s * 0.64)
    else:
        path.moveTo(m, s * 0.36)
        path.lineTo(s / 2, s * 0.66)
        path.lineTo(s - m, s * 0.36)
    painter.drawPath(path)


def _previous(painter, s, colour):
    _chevron(painter, s, up=True)


def _next(painter, s, colour):
    _chevron(painter, s, up=False)


def _attach(painter, s, colour):
    clip = QPainterPath()
    clip.moveTo(s * 0.66, s * 0.30)
    clip.lineTo(s * 0.36, s * 0.62)
    clip.cubicTo(s * 0.26, s * 0.72, s * 0.40, s * 0.86, s * 0.50, s * 0.76)
    clip.lineTo(s * 0.80, s * 0.44)
    clip.cubicTo(s * 0.94, s * 0.28, s * 0.72, s * 0.08, s * 0.58, s * 0.22)
    clip.lineTo(s * 0.26, s * 0.56)
    painter.drawPath(clip)


def _send(painter, s, colour):
    m = s * 0.16
    plane = QPainterPath()
    plane.moveTo(m, s * 0.50)
    plane.lineTo(s - m, m)
    plane.lineTo(s * 0.62, s - m)
    plane.lineTo(s * 0.50, s * 0.60)
    plane.closeSubpath()
    painter.drawPath(plane)
    painter.drawLine(QPointF(s * 0.50, s * 0.60), QPointF(s - m, m))


def _draft(painter, s, colour):
    m = s * 0.18
    painter.drawRect(QRectF(m, m, s - 2 * m, s - 2 * m))
    for y in (0.40, 0.54, 0.68):
        painter.drawLine(QPointF(m + s * 0.10, s * y), QPointF(s - m - s * 0.10, s * y))


def _open(painter, s, colour):
    m = s * 0.16
    painter.drawRect(QRectF(m, m + s * 0.10, s - 2 * m, s - 2 * m - s * 0.10))
    flap = QPainterPath()
    flap.moveTo(m, m + s * 0.10)
    flap.lineTo(s / 2, s * 0.58)
    flap.lineTo(s - m, m + s * 0.10)
    painter.drawPath(flap)


def _compose(painter, s, colour):
    m = s * 0.18
    painter.drawLine(QPointF(m, s - m), QPointF(s * 0.42, s - m))
    painter.drawLine(QPointF(m, s * 0.44), QPointF(m, s - m))
    pencil = QPainterPath()
    pencil.moveTo(s * 0.36, s * 0.70)
    pencil.lineTo(s * 0.78, s * 0.28)
    pencil.lineTo(s * 0.72, s * 0.22)
    pencil.lineTo(s * 0.30, s * 0.64)
    pencil.closeSubpath()
    painter.drawPath(pencil)


def _envelope(painter, s, opened: bool):
    m = s * 0.16
    painter.drawRect(QRectF(m, m + s * 0.12, s - 2 * m, s - 2 * m - s * 0.12))
    flap = QPainterPath()
    flap.moveTo(m, m + s * 0.12)
    flap.lineTo(s / 2, s * 0.56)
    flap.lineTo(s - m, m + s * 0.12)
    painter.drawPath(flap)
    if not opened:
        painter.setBrush(painter.pen().color())
        painter.drawEllipse(QPointF(s - m - s * 0.02, m + s * 0.10), s * 0.10, s * 0.10)
        painter.setBrush(Qt.BrushStyle.NoBrush)


def _read(painter, s, colour):
    _envelope(painter, s, opened=True)


def _unread(painter, s, colour):
    _envelope(painter, s, opened=False)


def _move(painter, s, colour):
    m = s * 0.16
    painter.drawRect(QRectF(m, m + s * 0.18, s - 2 * m, s - 2 * m - s * 0.18))
    painter.drawLine(QPointF(m, m + s * 0.18), QPointF(s * 0.44, m + s * 0.18))
    painter.drawLine(QPointF(s * 0.44, m + s * 0.18), QPointF(s * 0.52, m + s * 0.04))
    painter.drawLine(QPointF(s * 0.52, m + s * 0.04), QPointF(s - m, m + s * 0.04))
    painter.drawLine(QPointF(s * 0.40, s * 0.62), QPointF(s * 0.62, s * 0.62))
    head = QPainterPath()
    head.moveTo(s * 0.54, s * 0.52)
    head.lineTo(s * 0.64, s * 0.62)
    head.lineTo(s * 0.54, s * 0.72)
    painter.drawPath(head)


_DRAWERS = {
    "reply": _reply, "reply-all": _reply_all, "forward": _forward, "flag": _flag,
    "archive": _archive, "junk": _junk, "trash": _trash, "previous": _previous,
    "next": _next, "attach": _attach, "send": _send, "draft": _draft,
    "open": _open, "compose": _compose, "read": _read, "unread": _unread,
    "move": _move,
}
