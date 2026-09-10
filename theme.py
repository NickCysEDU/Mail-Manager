"""Appearance: light and dark, contrast, and a mode built for reading.

The app used to take whatever palette macOS handed it, which meant its
readability was somebody else's decision. This module owns that instead: it
builds a palette and a stylesheet from three settings, and nothing else in the
app hard-codes a colour that a person has to read text against.

Three separate axes, deliberately:

``mode``      follow the system, or pin light or dark
``contrast``  normal, or pushed to the ends of the range
``readable``  larger type, more air, and stronger separators

They compose. High contrast is about telling foreground from background;
readable is about the eye finding its place on a dense table. Somebody who
needs one often does not need the other.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Tuple

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import (
    QColor, QFont, QImage, QPainter, QPainterPath, QPalette, QPen,
)
from PySide6.QtWidgets import QProxyStyle, QStyle

MODES: Tuple[Tuple[str, str], ...] = (
    ("system", "Match macOS"),
    ("light", "Always light"),
    ("dark", "Always dark"),
)

#: How much room the window gives things. One axis, about space only - type
#: size and weight are the separate reading toggle, so the two compose: dense
#: spacing with larger type is a reasonable thing to want.
DENSITIES: Tuple[Tuple[str, str, str], ...] = (
    ("comfortable", "Comfortable",
     "The default. Room to read, and to hit things without aiming."),
    ("compact", "Compact",
     "Tighter padding and shorter rows. Roughly a third more messages on "
     "screen, and every control still a comfortable size to click."),
    ("dense", "Very compact",
     "As much as will fit. Single-line rows, minimal padding, and the preview "
     "pane starts closed. Best on a large display, or when you already know "
     "what you are looking for."),
)


@dataclass(frozen=True)
class Density:
    """The measurements one density choice implies."""

    name: str
    #: Padding above and below the text inside a control.
    control_pad: int
    #: Space between the window edge and its contents, and between rows.
    margin: int
    spacing: int
    #: Lines of wrapped text in a table row.
    row_lines: int
    #: Padding inside a table cell.
    cell_pad: int
    #: How much of the window the message preview takes, before it is dragged.
    preview_share: float
    #: Whether the preview starts open at all.
    preview_open: bool = True


_DENSITIES: Dict[str, Density] = {
    "comfortable": Density("comfortable", control_pad=7, margin=10, spacing=8,
                           row_lines=3, cell_pad=6, preview_share=0.32),
    "compact": Density("compact", control_pad=4, margin=6, spacing=5,
                       row_lines=2, cell_pad=3, preview_share=0.26),
    "dense": Density("dense", control_pad=2, margin=3, spacing=3,
                     row_lines=1, cell_pad=1, preview_share=0.22,
                     preview_open=False),
}


def density(name: str) -> Density:
    """The measurements for a density, falling back to the default."""
    return _DENSITIES.get((name or "").strip().lower(), _DENSITIES["comfortable"])


CONTRASTS: Tuple[Tuple[str, str], ...] = (
    ("normal", "Normal"),
    ("high", "High contrast"),
    ("maximum", "Maximum contrast"),
)


@dataclass(frozen=True)
class Palette:
    """Every colour the app draws, named by what it is for."""

    window: str
    surface: str            # tables, text areas
    surface_alt: str        # the other stripe
    text: str
    text_dim: str
    border: str
    accent: str             # the primary action, and the app's own colour
    accent_text: str
    ok: str
    warn: str
    danger: str
    selection: str
    selection_text: str

    @property
    def dark(self) -> bool:
        return QColor(self.window).lightness() < 128


#: Normal light. The accent is the Scan & Analyze blue, which is also the
#: icon's colour, so the app reads as one thing.
LIGHT = Palette(
    window="#F4F5F7", surface="#FFFFFF", surface_alt="#EFF2F6",
    text="#15181D", text_dim="#5A626E", border="#D2D7DE",
    accent="#2F6FE0", accent_text="#FFFFFF",
    ok="#1F7A4C", warn="#9A6412", danger="#B23B32",
    selection="#2F6FE0", selection_text="#FFFFFF",
)

DARK = Palette(
    window="#1B1D21", surface="#232629", surface_alt="#2C3034",
    text="#ECEEF1", text_dim="#A2AAB5", border="#3A3F45",
    accent="#4A86EE", accent_text="#0B1220",
    ok="#4BBE84", warn="#E0A63C", danger="#E4756B",
    selection="#3D74D6", selection_text="#FFFFFF",
)

#: High contrast keeps the hues but pushes the ends apart and darkens the
#: supporting colours until they pass against their own background.
LIGHT_HIGH = Palette(
    window="#FFFFFF", surface="#FFFFFF", surface_alt="#E4E4E4",
    text="#000000", text_dim="#2E2E2E", border="#6B6B6B",
    accent="#0B4FBF", accent_text="#FFFFFF",
    ok="#0A5C34", warn="#6E4407", danger="#8E1F17",
    selection="#0B4FBF", selection_text="#FFFFFF",
)

DARK_HIGH = Palette(
    window="#000000", surface="#0A0C0F", surface_alt="#1E2228",
    text="#FFFFFF", text_dim="#D6DAE0", border="#8A929C",
    accent="#7FB2FF", accent_text="#000000",
    ok="#6FE3A6", warn="#FFC963", danger="#FF9A8E",
    selection="#7FB2FF", selection_text="#000000",
)

#: Maximum is monochrome by design: no colour carries meaning on its own, and
#: every pairing is black on white or white on black. It is deliberately plain
#: rather than pretty, because that is the point of it.
LIGHT_MAX = Palette(
    window="#FFFFFF", surface="#FFFFFF", surface_alt="#E8E8E8",
    text="#000000", text_dim="#000000", border="#000000",
    accent="#000000", accent_text="#FFFFFF",
    ok="#000000", warn="#000000", danger="#000000",
    selection="#000000", selection_text="#FFFFFF",
)

DARK_MAX = Palette(
    window="#000000", surface="#000000", surface_alt="#1C1C1C",
    text="#FFFFFF", text_dim="#FFFFFF", border="#FFFFFF",
    accent="#FFFFFF", accent_text="#000000",
    ok="#FFFFFF", warn="#FFFFFF", danger="#FFFFFF",
    selection="#FFFFFF", selection_text="#000000",
)

_TABLE: Dict[Tuple[bool, str], Palette] = {
    (False, "normal"): LIGHT, (False, "high"): LIGHT_HIGH, (False, "maximum"): LIGHT_MAX,
    (True, "normal"): DARK, (True, "high"): DARK_HIGH, (True, "maximum"): DARK_MAX,
}


def system_is_dark(app) -> bool:
    """Whether the platform is currently in a dark appearance."""
    try:
        scheme = app.styleHints().colorScheme()
        return scheme == Qt.ColorScheme.Dark
    except (AttributeError, TypeError):       # pragma: no cover - older Qt
        return app.palette().color(QPalette.ColorRole.Window).lightness() < 128


def resolve(app, mode: str = "system", contrast: str = "normal") -> Palette:
    """The palette to draw with, given the settings and the platform."""
    contrast = contrast if contrast in dict(CONTRASTS) else "normal"
    if mode == "light":
        dark = False
    elif mode == "dark":
        dark = True
    else:
        dark = system_is_dark(app)
    return _TABLE[(dark, contrast)]


def contrast_ratio(foreground: str, background: str) -> float:
    """WCAG relative contrast, so the palettes can be checked rather than eyeballed."""
    def luminance(value: str) -> float:
        colour = QColor(value)
        channels = []
        for raw in (colour.redF(), colour.greenF(), colour.blueF()):
            channels.append(raw / 12.92 if raw <= 0.04045
                            else ((raw + 0.055) / 1.055) ** 2.4)
        return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]

    first, second = luminance(foreground), luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


def build_palette(colours: Palette) -> QPalette:
    palette = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    palette.setColor(role.Window, QColor(colours.window))
    palette.setColor(role.WindowText, QColor(colours.text))
    palette.setColor(role.Base, QColor(colours.surface))
    palette.setColor(role.AlternateBase, QColor(colours.surface_alt))
    palette.setColor(role.Text, QColor(colours.text))
    palette.setColor(role.Button, QColor(colours.window))
    palette.setColor(role.ButtonText, QColor(colours.text))
    palette.setColor(role.ToolTipBase, QColor(colours.surface))
    palette.setColor(role.ToolTipText, QColor(colours.text))
    palette.setColor(role.PlaceholderText, QColor(colours.text_dim))
    palette.setColor(role.Highlight, QColor(colours.selection))
    palette.setColor(role.HighlightedText, QColor(colours.selection_text))
    palette.setColor(role.Link, QColor(colours.accent))
    palette.setColor(role.Mid, QColor(colours.border))
    palette.setColor(role.Dark, QColor(colours.border))
    palette.setColor(role.Light, QColor(colours.surface_alt))

    disabled = QColor(colours.text_dim)
    disabled.setAlpha(150)
    for disabled_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(group.Disabled, disabled_role, disabled)
    return palette


def _lift(colour: str, factor: float) -> str:
    """A lighter or darker shade of one colour, for hover and pressed."""
    source = QColor(colour)
    hue, sat, light, alpha = source.getHsl()
    return QColor.fromHsl(
        hue, sat, max(0, min(255, int(light * factor))), alpha).name()


def stylesheet(colours: Palette, readable: bool = False,
               base_point: float = 13.0, spacing: str = "comfortable") -> str:
    """The parts a palette cannot express: spacing, borders, focus rings."""
    room = density(spacing)
    # One vertical padding for every control. Height is content plus padding
    # plus border, so controls only line up if all three agree - a minimum
    # height on its own leaves each widget type at whatever its own padding
    # makes it.
    vpad = room.control_pad + (2 if readable else 0)
    pad = f"{vpad}px {18 if readable else 15}px"
    radius = 7
    row_pad = f"{vpad}px"
    cell_pad = room.cell_pad + (2 if readable else 0)
    border = 2 if colours.dark or readable else 1
    focus = 3 if readable else 2
    # Steppers and drop-downs sized to be hit rather than aimed at. Apple's own
    # guidance puts the smallest comfortable target at 28 points; Qt's defaults
    # for these are closer to twelve.
    control_height = 24 if readable else 20
    stepper = 22 if readable else 18
    # Two of these plus their margins have to fit inside the field. Sized from
    # the field's own height rather than guessed, or the top one is clipped
    # away and the control ends up with a single arrow.
    stepper_half = max(8, (control_height - 6) // 2)
    scroll = 14 if readable else 12
    drop_width = 30 if readable else 26
    arrow_px = 14 if readable else 12
    up_arrow = arrow_image(colours.text, "up", arrow_px)
    down_arrow = arrow_image(colours.text, "down", arrow_px)
    right_arrow = arrow_image(colours.text, "right", arrow_px)
    dim_arrow = arrow_image(colours.text_dim, "down", arrow_px)

    return f"""
    QWidget {{ color: {colours.text}; }}
    QMainWindow, QDialog, QWidget#page {{ background: {colours.window}; }}

    QLabel {{ color: {colours.text}; }}
    QLabel[dim="true"] {{ color: {colours.text_dim}; }}
    QLabel[tone="warn"] {{ color: {colours.warn}; }}
    QLabel[tone="ok"] {{ color: {colours.ok}; }}

    QAbstractItemView, QTextEdit, QPlainTextEdit, QLineEdit, QSpinBox,
    QDoubleSpinBox, QComboBox, QDateEdit {{
        background: {colours.surface};
        color: {colours.text};
        border: {border}px solid {colours.border};
        border-radius: {radius}px;
        selection-background-color: {colours.selection};
        selection-color: {colours.selection_text};
    }}
    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {{
        padding: {row_pad} 10px;
    }}

    /* Steppers: no boxes, no dividing lines. Two chevrons in the field's own
       margin, which is quiet until it is wanted. Qt's defaults are about six
       pixels tall, which is hard to hit and harder to see. */
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QDateEdit::up-button, QTimeEdit::up-button {{
        subcontrol-origin: padding; subcontrol-position: top right;
        width: {stepper}px; height: {stepper_half}px;
        margin: 2px 3px 0 0;
        border: none; border-radius: 4px;
        background: transparent;
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button,
    QDateEdit::down-button, QTimeEdit::down-button {{
        subcontrol-origin: padding; subcontrol-position: bottom right;
        width: {stepper}px; height: {stepper_half}px;
        margin: 0 3px 2px 0;
        border: none; border-radius: 4px;
        background: transparent;
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover,
    QDateEdit::up-button:hover, QDateEdit::down-button:hover {{
        background: {colours.surface_alt};
    }}
    QSpinBox, QDoubleSpinBox, QDateEdit, QTimeEdit {{
        padding-right: {stepper + 8}px;
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateEdit::up-arrow,
    QTimeEdit::up-arrow {{
        image: url("{up_arrow}");
        width: {arrow_px}px; height: {arrow_px}px;
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateEdit::down-arrow,
    QTimeEdit::down-arrow {{
        image: url("{down_arrow}");
        width: {arrow_px}px; height: {arrow_px}px;
    }}
    QSpinBox::up-arrow:disabled, QSpinBox::down-arrow:disabled,
    QDoubleSpinBox::up-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
        image: url("{dim_arrow}");
    }}
    QComboBox::down-arrow {{
        image: url("{down_arrow}");
        width: {arrow_px}px; height: {arrow_px}px;
    }}
    QComboBox::down-arrow:on {{ image: url("{up_arrow}"); }}
    QToolButton::menu-indicator {{ image: url("{down_arrow}"); }}
    QHeaderView::down-arrow {{ image: url("{down_arrow}"); width: 12px; height: 12px; }}
    QHeaderView::up-arrow {{ image: url("{up_arrow}"); width: 12px; height: 12px; }}
    QMenu::right-arrow {{ image: url("{right_arrow}"); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{
        border: {border}px solid {colours.border};
        background: {colours.surface};
        padding: 4px;
    }}

    /* The same for a tool button that opens a menu, so the caret is not
       jammed against the label. */
    QToolButton::menu-indicator {{
        subcontrol-origin: padding; subcontrol-position: center right;
        width: {drop_width}px; height: {arrow_px}px;
    }}
    /* Room for the caret on the right, and the same vertical padding as
       everything else so the button is the same height as its neighbours. */
    QToolButton[popupMode="0"], QToolButton[popupMode="2"] {{
        padding-right: {drop_width + 6}px;
    }}

    QAbstractItemView {{
        alternate-background-color: {colours.surface_alt};
        gridline-color: {colours.border};
        outline: none;
    }}
    QAbstractItemView::item {{ padding: {cell_pad}px 6px; }}
    QAbstractItemView::item:selected {{
        background: {colours.selection}; color: {colours.selection_text};
    }}

    QHeaderView::section {{
        background: {colours.window};
        color: {colours.text};
        border: 0;
        border-right: 1px solid {colours.border};
        border-bottom: {border}px solid {colours.border};
        padding: {cell_pad + 2}px 8px;
        font-weight: {700 if readable else 600};
    }}

    /* One height and one radius for everything a person clicks, so a row of
       mixed controls lines up instead of stepping. */
    QPushButton, QToolButton, QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox,
    QDateEdit, QTimeEdit {{
        min-height: {control_height}px;
    }}
    /* The help button is a round icon and is deliberately not this size. */
    QToolButton#helpButton {{ min-height: 0; max-height: none; padding: 0; }}
    /* Nor are the small square buttons that add and remove a line: the
       standard padding would push the one character they hold outside them. */
    /* Four buttons read as one segmented control. The standard padding is
       for a lone button carrying a phrase; here it is dead width. */
    QToolButton[segment="true"] {{
        padding-left: {8 if readable else 7}px; padding-right: {8 if readable else 7}px;
    }}
    QToolButton[compact="true"] {{
        min-height: 0; min-width: 0; padding: 0; margin: 0;
        border: {border}px solid transparent;
        background: transparent;
        font-size: {base_point + 2}pt;
    }}
    QToolButton[compact="true"]:hover {{
        background: {colours.surface_alt};
        border-color: {colours.border};
    }}
    QToolButton[compact="true"]:pressed {{ background: {colours.selection};
                                           color: {colours.selection_text}; }}
    QToolButton[compact="true"]:disabled {{ color: {colours.text_dim};
                                            background: transparent; }}
    QPushButton, QToolButton {{
        background: {colours.surface};
        color: {colours.text};
        border: {border}px solid {colours.border};
        border-radius: {radius}px;
        padding: {pad};
    }}
    /* A tool button reserves room for its menu caret above and below the
       label as well as beside it, so the same padding makes it taller than
       everything else in the row. */
    QToolButton {{ padding-top: {vpad - 1}px; padding-bottom: {vpad - 1}px; }}
    /* Roles. Filled means it acts on your mailbox; outlined means it changes
       what you are looking at; plain means everything else. */
    QPushButton[role="primary"] {{
        background: {colours.accent}; color: {colours.accent_text};
        border-color: {colours.accent}; font-weight: 600;
    }}
    QPushButton[role="primary"]:hover:!disabled {{ background: {_lift(colours.accent, 1.12)}; }}
    QPushButton[role="primary"]:pressed {{ background: {_lift(colours.accent, 0.88)}; }}
    QPushButton[role="confirm"] {{
        background: {colours.ok}; color: #FFFFFF;
        border-color: {colours.ok}; font-weight: 600;
    }}
    QPushButton[role="confirm"]:hover:!disabled {{ background: {_lift(colours.ok, 1.12)}; }}
    QPushButton[role="confirm"]:pressed {{ background: {_lift(colours.ok, 0.88)}; }}
    QPushButton[role="danger"] {{
        background: {colours.danger}; color: #FFFFFF;
        border-color: {colours.danger}; font-weight: 600;
    }}
    QPushButton[role="danger"]:hover:!disabled {{ background: {_lift(colours.danger, 1.12)}; }}
    QPushButton[role="danger"]:pressed {{ background: {_lift(colours.danger, 0.88)}; }}
    QPushButton[role="destructive"] {{
        color: {colours.danger}; border-color: {colours.danger};
        background: transparent;
    }}
    QPushButton[role="destructive"]:hover:!disabled {{
        background: {colours.danger}; color: #FFFFFF;
    }}
    QPushButton[role="primary"]:disabled, QPushButton[role="confirm"]:disabled,
    QPushButton[role="danger"]:disabled {{
        background: {colours.border}; border-color: {colours.border};
        color: {colours.text_dim};
    }}
    QPushButton:hover, QToolButton:hover {{ border-color: {colours.accent}; }}
    QPushButton:disabled, QToolButton:disabled {{ color: {colours.text_dim}; }}
    QToolButton:checked {{
        background: {colours.accent}; color: {colours.accent_text};
        border-color: {colours.accent};
    }}

    /* A focus ring that is actually visible, on every focusable thing. */
    *:focus {{
        border: {focus}px solid {colours.accent};
        border-radius: {radius}px;
    }}

    QProgressBar {{
        border: {border}px solid {colours.border};
        border-radius: {radius}px;
        background: {colours.surface};
        color: {colours.text};
        text-align: center;
        padding: 1px;
    }}
    QProgressBar::chunk {{ background: {colours.accent}; border-radius: {radius - 2}px; }}

    QMenu {{
        background: {colours.surface}; color: {colours.text};
        border: {border}px solid {colours.border};
    }}
    QMenu::item:selected {{ background: {colours.selection}; color: {colours.selection_text}; }}

    QGroupBox {{
        border: {border}px solid {colours.border};
        border-radius: {radius}px;
        margin-top: 14px;
        padding-top: 10px;
        font-weight: {700 if readable else 600};
    }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; }}

    QTabBar::tab {{
        padding: {pad};
        border: {border}px solid {colours.border};
        border-bottom: 0;
        border-top-left-radius: {radius}px;
        border-top-right-radius: {radius}px;
        background: {colours.window};
    }}
    QTabBar::tab:selected {{
        background: {colours.surface};
        color: {colours.text};
        font-weight: {700 if readable else 600};
    }}
    QToolTip {{
        background: {colours.surface}; color: {colours.text};
        border: {border}px solid {colours.border}; padding: 6px;
    }}
    QSplitter::handle {{ background: {colours.border}; }}

    /* Scroll bars without stepper buttons, the way macOS draws them. Styling
       the handle but leaving the buttons to the default style put the handle
       on top of them, and left their arrows pointing whichever way the base
       style happened to choose. */
    QScrollBar:vertical {{
        background: transparent; width: {scroll}px; margin: 0;
        border: none;
    }}
    QScrollBar:horizontal {{
        background: transparent; height: {scroll}px; margin: 0;
        border: none;
    }}
    QScrollBar::handle:vertical {{
        background: {colours.border}; border-radius: {scroll // 2 - 2}px;
        min-height: 32px; margin: 2px;
    }}
    QScrollBar::handle:horizontal {{
        background: {colours.border}; border-radius: {scroll // 2 - 2}px;
        min-width: 32px; margin: 2px;
    }}
    QScrollBar::handle:hover {{ background: {colours.text_dim}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{
        height: 0; width: 0; border: none; background: none;
    }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
    """


class ArrowStyle(QProxyStyle):
    """Draws the little arrows, instead of leaving them to the stylesheet.

    A stylesheet can only describe an arrow as a border triangle or a bitmap.
    The triangle renders as a filled rectangle in a spin box, and a bitmap
    cannot follow the palette or the display's scale factor. Drawing them is
    both smaller and better: a chevron, in the current text colour, crisp at
    any size.
    """

    ARROWS = {
        QStyle.PrimitiveElement.PE_IndicatorArrowDown: 180,
        QStyle.PrimitiveElement.PE_IndicatorArrowUp: 0,
        QStyle.PrimitiveElement.PE_IndicatorArrowLeft: 90,
        QStyle.PrimitiveElement.PE_IndicatorArrowRight: 270,
        QStyle.PrimitiveElement.PE_IndicatorSpinDown: 180,
        QStyle.PrimitiveElement.PE_IndicatorSpinUp: 0,
    }

    def drawPrimitive(self, element, option, painter, widget=None):  # noqa: N802
        angle = self.ARROWS.get(element)
        if angle is None:
            super().drawPrimitive(element, option, painter, widget)
            return

        rect = option.rect
        if rect.width() <= 2 or rect.height() <= 2:
            return
        # A chevron rather than a filled triangle: lighter, and it matches the
        # rest of the system's iconography.
        side = min(rect.width(), rect.height()) * 0.42
        centre = QPointF(rect.center()) + QPointF(0.5, 0.5)

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(centre)
        painter.rotate(angle)

        colour = option.palette.color(QPalette.ColorRole.ButtonText)
        if not (option.state & QStyle.StateFlag.State_Enabled):
            colour.setAlpha(110)
        pen = QPen(colour, max(1.3, side * 0.30))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)

        half = side * 0.62
        path = QPainterPath()
        path.moveTo(-half, -half * 0.42)
        path.lineTo(0.0, half * 0.46)
        path.lineTo(half, -half * 0.42)
        painter.drawPath(path)
        painter.restore()


#: Rendered chevrons, cached by colour and size. A stylesheet can only point
#: at an image for a sub-control it has styled - it will not call back into
#: the style - so the arrows on spin boxes and combo boxes have to exist as
#: files. They are drawn here rather than shipped so they follow the palette.
_ARROW_CACHE: Dict[Tuple[str, str, int], str] = {}


def arrow_image(colour: str, direction: str = "down", size: int = 16) -> str:
    """Path to a chevron of this colour and direction, drawn on first use."""
    key = (colour, direction, size)
    cached = _ARROW_CACHE.get(key)
    if cached and Path(cached).is_file():
        return cached

    from PySide6.QtCore import QStandardPaths

    scale = 2                                  # crisp on a Retina display
    pixels = size * scale
    image = QImage(pixels, pixels, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.translate(pixels / 2.0, pixels / 2.0)
    painter.rotate({"down": 180, "up": 0, "left": 90, "right": 270}.get(direction, 180))

    pen = QPen(QColor(colour), max(1.4, pixels * 0.115))
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    reach = pixels * 0.27
    path = QPainterPath()
    path.moveTo(-reach, reach * 0.45)
    path.lineTo(0.0, -reach * 0.5)
    path.lineTo(reach, reach * 0.45)
    painter.drawPath(path)
    painter.end()

    cache_root = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.CacheLocation) or "/tmp"
    folder = Path(cache_root) / "arrows"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{direction}-{colour.lstrip('#')}-{size}.png"
    image.save(str(target), "PNG")
    _ARROW_CACHE[key] = str(target)
    return str(target)


def base_font(app, readable: bool) -> QFont:
    """Type large enough to read without leaning in."""
    font = QFont(app.font())
    size = font.pointSizeF()
    if size <= 0:
        size = 13.0
    font.setPointSizeF(size + 2.0 if readable else size)
    if readable:
        # A hair more tracking; the default is tight at larger sizes.
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 101.0)
    return font


def apply(app, mode: str = "system", contrast: str = "normal",
          readable: bool = False, spacing: str = "comfortable") -> Palette:
    """Paint the whole application. Returns the palette that was used."""
    colours = resolve(app, mode, contrast)
    if not isinstance(app.style(), ArrowStyle):
        app.setStyle(ArrowStyle(app.style()))
    app.setPalette(build_palette(colours))
    app.setFont(base_font(app, readable))
    app.setStyleSheet(stylesheet(colours, readable, spacing=spacing))
    return colours
