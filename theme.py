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
from typing import Dict, Tuple

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPalette

MODES: Tuple[Tuple[str, str], ...] = (
    ("system", "Match macOS"),
    ("light", "Always light"),
    ("dark", "Always dark"),
)

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
    window="#F4F5F7", surface="#FFFFFF", surface_alt="#F7F8FA",
    text="#15181D", text_dim="#5A626E", border="#D2D7DE",
    accent="#2F6FE0", accent_text="#FFFFFF",
    ok="#1F7A4C", warn="#9A6412", danger="#B23B32",
    selection="#2F6FE0", selection_text="#FFFFFF",
)

DARK = Palette(
    window="#1B1D21", surface="#232629", surface_alt="#26292D",
    text="#ECEEF1", text_dim="#A2AAB5", border="#3A3F45",
    accent="#4A86EE", accent_text="#0B1220",
    ok="#4BBE84", warn="#E0A63C", danger="#E4756B",
    selection="#3D74D6", selection_text="#FFFFFF",
)

#: High contrast keeps the hues but pushes the ends apart and darkens the
#: supporting colours until they pass against their own background.
LIGHT_HIGH = Palette(
    window="#FFFFFF", surface="#FFFFFF", surface_alt="#EFEFEF",
    text="#000000", text_dim="#2E2E2E", border="#6B6B6B",
    accent="#0B4FBF", accent_text="#FFFFFF",
    ok="#0A5C34", warn="#6E4407", danger="#8E1F17",
    selection="#0B4FBF", selection_text="#FFFFFF",
)

DARK_HIGH = Palette(
    window="#000000", surface="#0A0C0F", surface_alt="#15181C",
    text="#FFFFFF", text_dim="#D6DAE0", border="#8A929C",
    accent="#7FB2FF", accent_text="#000000",
    ok="#6FE3A6", warn="#FFC963", danger="#FF9A8E",
    selection="#7FB2FF", selection_text="#000000",
)

#: Maximum is monochrome by design: no colour carries meaning on its own, and
#: every pairing is black on white or white on black. It is deliberately plain
#: rather than pretty, because that is the point of it.
LIGHT_MAX = Palette(
    window="#FFFFFF", surface="#FFFFFF", surface_alt="#FFFFFF",
    text="#000000", text_dim="#000000", border="#000000",
    accent="#000000", accent_text="#FFFFFF",
    ok="#000000", warn="#000000", danger="#000000",
    selection="#000000", selection_text="#FFFFFF",
)

DARK_MAX = Palette(
    window="#000000", surface="#000000", surface_alt="#000000",
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


def stylesheet(colours: Palette, readable: bool = False, base_point: float = 13.0) -> str:
    """The parts a palette cannot express: spacing, borders, focus rings."""
    pad = "10px 18px" if readable else "8px 15px"
    radius = 7
    row_pad = "8px" if readable else "6px"
    border = 2 if colours.dark or readable else 1
    focus = 3 if readable else 2
    # Steppers and drop-downs sized to be hit rather than aimed at. Apple's own
    # guidance puts the smallest comfortable target at 28 points; Qt's defaults
    # for these are closer to twelve.
    control_height = 30 if readable else 26
    stepper = 26 if readable else 22
    stepper_half = (control_height + 2) // 2
    arrow = 5 if readable else 4
    drop_width = 30 if readable else 26

    return f"""
    QWidget {{ color: {colours.text}; }}
    QMainWindow, QDialog, QWidget#page {{ background: {colours.window}; }}

    QLabel {{ color: {colours.text}; }}
    QLabel[dim="true"] {{ color: {colours.text_dim}; }}

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
        min-height: {control_height}px;
    }}

    /* Steppers. Qt's default arrows are about six pixels tall, which is a
       hard thing to hit and a harder thing to see. */
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QDateEdit::up-button, QTimeEdit::up-button {{
        subcontrol-origin: border; subcontrol-position: top right;
        width: {stepper}px; height: {stepper_half}px;
        border-left: {border}px solid {colours.border};
        border-bottom: {border}px solid {colours.border};
        border-top-right-radius: {radius}px;
        background: {colours.window};
    }}
    QSpinBox::down-button, QDoubleSpinBox::down-button,
    QDateEdit::down-button, QTimeEdit::down-button {{
        subcontrol-origin: border; subcontrol-position: bottom right;
        width: {stepper}px; height: {stepper_half}px;
        border-left: {border}px solid {colours.border};
        border-bottom-right-radius: {radius}px;
        background: {colours.window};
    }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background: {colours.accent};
    }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow, QDateEdit::up-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: {arrow}px solid transparent;
        border-right: {arrow}px solid transparent;
        border-bottom: {arrow}px solid {colours.text};
    }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow, QDateEdit::down-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: {arrow}px solid transparent;
        border-right: {arrow}px solid transparent;
        border-top: {arrow}px solid {colours.text};
    }}

    /* Room for the arrow, and room around it. */
    QComboBox {{ padding-right: {drop_width + 8}px; }}
    QComboBox::drop-down {{
        subcontrol-origin: padding; subcontrol-position: center right;
        width: {drop_width}px;
        border-left: {border}px solid {colours.border};
        border-top-right-radius: {radius}px;
        border-bottom-right-radius: {radius}px;
    }}
    QComboBox::down-arrow {{
        image: none;
        width: 0; height: 0;
        border-left: {arrow}px solid transparent;
        border-right: {arrow}px solid transparent;
        border-top: {arrow}px solid {colours.text};
    }}
    QComboBox::down-arrow:on {{ margin-top: {arrow}px; }}
    QComboBox QAbstractItemView {{
        border: {border}px solid {colours.border};
        background: {colours.surface};
        padding: 4px;
    }}

    /* The same for a tool button that opens a menu, so the caret is not
       jammed against the label. */
    QToolButton::menu-indicator {{
        subcontrol-origin: padding; subcontrol-position: center right;
        width: {drop_width}px;
    }}

    QAbstractItemView {{
        alternate-background-color: {colours.surface_alt};
        gridline-color: {colours.border};
        outline: none;
    }}
    QAbstractItemView::item {{ padding: {row_pad} 6px; }}
    QAbstractItemView::item:selected {{
        background: {colours.selection}; color: {colours.selection_text};
    }}

    QHeaderView::section {{
        background: {colours.window};
        color: {colours.text};
        border: 0;
        border-right: 1px solid {colours.border};
        border-bottom: {border}px solid {colours.border};
        padding: {row_pad} 8px;
        font-weight: {700 if readable else 600};
    }}

    QPushButton, QToolButton {{
        background: {colours.surface};
        color: {colours.text};
        border: {border}px solid {colours.border};
        border-radius: {radius}px;
        padding: {pad};
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
    QScrollBar:vertical, QScrollBar:horizontal {{ background: {colours.window}; }}
    QScrollBar::handle {{ background: {colours.border}; border-radius: 5px; }}
    """


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
          readable: bool = False) -> Palette:
    """Paint the whole application. Returns the palette that was used."""
    colours = resolve(app, mode, contrast)
    app.setPalette(build_palette(colours))
    app.setFont(base_font(app, readable))
    app.setStyleSheet(stylesheet(colours, readable))
    return colours
