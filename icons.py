"""The small pictures on the mail buttons: an arrow back for a reply, two
for everyone, one forward, a flag, a box, a bin, and so on; and on the
formatting bar, the letters and marks a word processor uses. Drawn here
with a pen rather than shipped as files, in the colour of the text they
sit beside, so they follow the theme and every size of screen.

Each is drawn once per colour and size and kept; a button asks for its
icon by name.
"""

from __future__ import annotations

from typing import Dict, Tuple

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QIcon, QPainter, QPainterPath, QPen,
                           QPixmap)

#: The names a button can ask for.
NAMES = ("reply", "reply-all", "forward", "flag", "archive", "junk", "trash",
         "previous", "next", "attach", "send", "draft", "open", "compose",
         "read", "unread", "move", "inbox", "folder", "sidebar",
         "bold", "italic", "underline", "strikethrough", "text-colour",
         "bullets", "numbers", "outdent", "indent", "align-left",
         "align-centre", "align-right", "align-justify", "link", "picture",
         "clear-format", "grow", "shrink", "cc")

_made: Dict[Tuple[str, str, int], QIcon] = {}


def icon(name: str, colour: str = "#d6d6d6", size: int = 18,
         second: str = "") -> QIcon:
    """The icon by name, in a colour, ``size`` points square. ``second`` is
    the one other colour an icon can carry: the bar under the text-colour
    A."""
    key = (name, colour, size, second)
    found = _made.get(key)
    if found is None:
        found = _draw(name, QColor(colour), size,
                      QColor(second) if second else None)
        _made[key] = found
    return found


def _draw(name: str, colour: QColor, size: int, second=None) -> QIcon:
    scale = 2
    pixmap = QPixmap(size * scale, size * scale)
    pixmap.setDevicePixelRatio(scale)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    pen = QPen(colour)
    pen.setWidthF(size / 11.0)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    drawer = _DRAWERS.get(name)
    if drawer is not None:
        if name == "text-colour":
            drawer(painter, size, colour, second)
        else:
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


def _inbox(painter, s, colour):
    m = s * 0.16
    tray = QPainterPath()
    tray.moveTo(m + s * 0.06, m + s * 0.08)
    tray.lineTo(m, s * 0.56)
    tray.lineTo(m, s - m)
    tray.lineTo(s - m, s - m)
    tray.lineTo(s - m, s * 0.56)
    tray.lineTo(s - m - s * 0.06, m + s * 0.08)
    tray.closeSubpath()
    painter.drawPath(tray)
    slot = QPainterPath()
    slot.moveTo(m, s * 0.56)
    slot.lineTo(s * 0.36, s * 0.56)
    slot.lineTo(s * 0.40, s * 0.67)
    slot.lineTo(s * 0.60, s * 0.67)
    slot.lineTo(s * 0.64, s * 0.56)
    slot.lineTo(s - m, s * 0.56)
    painter.drawPath(slot)


def _folder(painter, s, colour):
    m = s * 0.14
    body = QPainterPath()
    body.moveTo(m, m + s * 0.10)
    body.lineTo(s * 0.40, m + s * 0.10)
    body.lineTo(s * 0.48, m + s * 0.20)
    body.lineTo(s - m, m + s * 0.20)
    body.lineTo(s - m, s - m - s * 0.06)
    body.lineTo(m, s - m - s * 0.06)
    body.closeSubpath()
    painter.drawPath(body)


def _sidebar(painter, s, colour):
    m = s * 0.14
    painter.drawRoundedRect(QRectF(m, m + s * 0.06, s - 2 * m, s - 2 * m - s * 0.12),
                            s * 0.08, s * 0.08)
    painter.drawLine(QPointF(s * 0.40, m + s * 0.06), QPointF(s * 0.40, s - m - s * 0.06))
    for y in (0.38, 0.50):
        painter.drawLine(QPointF(m + s * 0.08, s * y), QPointF(s * 0.32, s * y))


# -- The formatting bar's ------------------------------------------------

def _letter(painter: QPainter, s: float, text: str, bold: bool = True,
            italic: bool = False, underline: bool = False,
            strike: bool = False, share: float = 0.74, box=None) -> None:
    """One letter filling the box, in the pen's colour, as a word processor
    draws its B, I, U and S."""
    font = QFont(painter.font())
    font.setPointSizeF(s * share)
    font.setBold(bold)
    font.setItalic(italic)
    font.setUnderline(underline)
    font.setStrikeOut(strike)
    painter.setFont(font)
    painter.drawText(box or QRectF(0, 0, s, s), Qt.AlignmentFlag.AlignCenter,
                     text)


def _bold(painter, s, colour):
    _letter(painter, s, "B")


def _italic(painter, s, colour):
    _letter(painter, s, "I", bold=False, italic=True, share=0.80)


def _underline(painter, s, colour):
    _letter(painter, s, "U", bold=False, underline=True)


def _strikethrough(painter, s, colour):
    _letter(painter, s, "S", bold=False, strike=True)


def _text_colour(painter, s, colour, second=None):
    """An A over a bar of the colour it would apply."""
    _letter(painter, s, "A", share=0.66, box=QRectF(0, -s * 0.06, s, s * 0.84))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(second if second is not None else colour)
    painter.drawRoundedRect(QRectF(s * 0.14, s * 0.80, s * 0.72, s * 0.14),
                            s * 0.04, s * 0.04)


def _lines(painter, s, rows=(0.28, 0.50, 0.72), left=0.40, right=0.86):
    for y in rows:
        painter.drawLine(QPointF(s * left, s * y), QPointF(s * right, s * y))


def _bullets(painter, s, colour):
    _lines(painter, s)
    painter.setBrush(colour)
    painter.setPen(Qt.PenStyle.NoPen)
    for y in (0.28, 0.50, 0.72):
        painter.drawEllipse(QPointF(s * 0.22, s * y), s * 0.055, s * 0.055)


def _numbers(painter, s, colour):
    _lines(painter, s, left=0.44)
    font = QFont(painter.font())
    font.setPointSizeF(s * 0.30)
    font.setBold(True)
    painter.setFont(font)
    for number, y in enumerate((0.28, 0.50, 0.72), start=1):
        painter.drawText(QRectF(s * 0.08, s * (y - 0.14), s * 0.26, s * 0.28),
                         Qt.AlignmentFlag.AlignCenter, str(number))


def _indent_lines(painter, s):
    painter.drawLine(QPointF(s * 0.14, s * 0.22), QPointF(s * 0.86, s * 0.22))
    painter.drawLine(QPointF(s * 0.50, s * 0.42), QPointF(s * 0.86, s * 0.42))
    painter.drawLine(QPointF(s * 0.50, s * 0.60), QPointF(s * 0.86, s * 0.60))
    painter.drawLine(QPointF(s * 0.14, s * 0.80), QPointF(s * 0.86, s * 0.80))


def _indent(painter, s, colour):
    _indent_lines(painter, s)
    head = QPainterPath()
    head.moveTo(s * 0.16, s * 0.38)
    head.lineTo(s * 0.34, s * 0.51)
    head.lineTo(s * 0.16, s * 0.64)
    painter.drawPath(head)


def _outdent(painter, s, colour):
    _indent_lines(painter, s)
    head = QPainterPath()
    head.moveTo(s * 0.34, s * 0.38)
    head.lineTo(s * 0.16, s * 0.51)
    head.lineTo(s * 0.34, s * 0.64)
    painter.drawPath(head)


def _aligned(painter, s, how: str):
    rows = ((0.22, 1.0), (0.41, 0.62), (0.60, 1.0), (0.79, 0.62))
    for y, share in rows:
        full = s * 0.72
        wide = full * (1.0 if how == "justify" else share)
        if how == "left" or how == "justify":
            left = s * 0.14
        elif how == "right":
            left = s * 0.86 - wide
        else:
            left = s * 0.50 - wide / 2.0
        painter.drawLine(QPointF(left, s * y), QPointF(left + wide, s * y))


def _align_left(painter, s, colour):
    _aligned(painter, s, "left")


def _align_centre(painter, s, colour):
    _aligned(painter, s, "centre")


def _align_right(painter, s, colour):
    _aligned(painter, s, "right")


def _align_justify(painter, s, colour):
    _aligned(painter, s, "justify")


def _link(painter, s, colour):
    """Two links of a chain."""
    for dx, dy in ((-1, -1), (1, 1)):
        path = QPainterPath()
        cx, cy = s * 0.5 + dx * s * 0.11, s * 0.5 + dy * s * 0.11
        path.addRoundedRect(QRectF(cx - s * 0.26, cy - s * 0.11, s * 0.40,
                                   s * 0.22), s * 0.11, s * 0.11)
        painter.save()
        painter.translate(cx, cy)
        painter.rotate(-45)
        painter.translate(-cx, -cy)
        painter.drawPath(path)
        painter.restore()


def _picture(painter, s, colour):
    m = s * 0.16
    painter.drawRoundedRect(QRectF(m, m + s * 0.04, s - 2 * m, s - 2 * m - s * 0.08),
                            s * 0.06, s * 0.06)
    hills = QPainterPath()
    hills.moveTo(m + s * 0.04, s - m - s * 0.12)
    hills.lineTo(s * 0.40, s * 0.52)
    hills.lineTo(s * 0.54, s * 0.66)
    hills.lineTo(s * 0.64, s * 0.56)
    hills.lineTo(s - m - s * 0.04, s - m - s * 0.12)
    painter.drawPath(hills)
    painter.setBrush(colour)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(QPointF(s * 0.64, s * 0.36), s * 0.06, s * 0.06)


def _clear_format(painter, s, colour):
    """An eraser over a small A."""
    _letter(painter, s, "A", share=0.52, box=QRectF(0, s * 0.30, s * 0.62, s * 0.62))
    rubber = QPainterPath()
    rubber.moveTo(s * 0.50, s * 0.46)
    rubber.lineTo(s * 0.72, s * 0.24)
    rubber.lineTo(s * 0.90, s * 0.42)
    rubber.lineTo(s * 0.68, s * 0.64)
    rubber.closeSubpath()
    painter.drawPath(rubber)
    painter.drawLine(QPointF(s * 0.60, s * 0.36), QPointF(s * 0.78, s * 0.54))


def _grow(painter, s, colour):
    _letter(painter, s, "A", share=0.70, box=QRectF(0, s * 0.10, s * 0.66, s * 0.84))
    painter.drawLine(QPointF(s * 0.80, s * 0.14), QPointF(s * 0.80, s * 0.42))
    painter.drawLine(QPointF(s * 0.66, s * 0.28), QPointF(s * 0.94, s * 0.28))


def _shrink(painter, s, colour):
    _letter(painter, s, "A", share=0.52, box=QRectF(0, s * 0.22, s * 0.62, s * 0.72))
    painter.drawLine(QPointF(s * 0.66, s * 0.28), QPointF(s * 0.94, s * 0.28))


def _cc(painter, s, colour):
    _letter(painter, s, "Cc", bold=False, share=0.56)


_DRAWERS = {
    "reply": _reply, "reply-all": _reply_all, "forward": _forward, "flag": _flag,
    "archive": _archive, "junk": _junk, "trash": _trash, "previous": _previous,
    "next": _next, "attach": _attach, "send": _send, "draft": _draft,
    "open": _open, "compose": _compose, "read": _read, "unread": _unread,
    "move": _move, "inbox": _inbox, "folder": _folder, "sidebar": _sidebar,
    "bold": _bold, "italic": _italic, "underline": _underline,
    "strikethrough": _strikethrough, "text-colour": _text_colour,
    "bullets": _bullets, "numbers": _numbers, "outdent": _outdent,
    "indent": _indent, "align-left": _align_left, "align-centre": _align_centre,
    "align-right": _align_right, "align-justify": _align_justify,
    "link": _link, "picture": _picture, "clear-format": _clear_format,
    "grow": _grow, "shrink": _shrink, "cc": _cc,
}
