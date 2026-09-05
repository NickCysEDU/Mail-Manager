"""Icon geometry: the app icon's outline and the menu bar item's spacing.

These check measurable properties rather than appearance - that the app icon
matches the shape macOS uses, that it stays legible once it is only a few
points across, and that the menu bar glyph is centred and the right size.
"""

from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QRectF, QSize, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from menubar import _BAR_CANVAS, _BAR_MARGIN, menu_bar_icon  # noqa: E402
from tools.make_icon import BODY, draw_icon, squircle  # noqa: E402


def ink_bounds(image: QImage, threshold: int = 40):
    """The box the drawn pixels occupy, as (left, top, right, bottom)."""
    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    xs, ys = [], []
    for y in range(image.height()):
        for x in range(image.width()):
            if (image.pixel(x, y) >> 24) & 0xFF > threshold:
                xs.append(x)
                ys.append(y)
    assert xs, "nothing was drawn"
    return min(xs), min(ys), max(xs), max(ys)


# -- the app icon -----------------------------------------------------------
def test_squircle_keeps_a_straight_edge_then_turns(qapp):
    """macOS icons are straight-sided out to about 60% before the corner starts.

    A plain superellipse curves the whole way and reads as a blob beside the
    system icons, so this pins the property that distinguishes the two. The
    figures come from measuring Mail.app's own icon.
    """
    size = 512
    image = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#000000"))
    painter.drawPath(squircle(QRectF(0, 0, size, size)))
    painter.end()

    image = image.convertToFormat(QImage.Format.Format_ARGB32)
    half = size / 2.0

    def half_width_at(t: float) -> float:
        y = int(round(half + t * (half - 1)))
        xs = [x for x in range(size) if (image.pixel(x, y) >> 24) & 0xFF > 128]
        return ((max(xs) - min(xs) + 1) / 2.0) / half

    assert half_width_at(0.30) == pytest.approx(1.0, abs=0.01)
    assert half_width_at(0.55) == pytest.approx(1.0, abs=0.01)
    assert half_width_at(0.75) == pytest.approx(0.96, abs=0.02)
    assert half_width_at(0.97) == pytest.approx(0.73, abs=0.03)


def test_app_icon_sits_on_the_macos_grid(qapp):
    """824 of 1024 with the rest left for the shadow is what the system uses."""
    assert BODY.width() == BODY.height()
    assert BODY.width() == pytest.approx(814, abs=12)
    assert BODY.left() == pytest.approx(1024 - BODY.right(), abs=2)


@pytest.mark.parametrize("size", [16, 32, 64, 128, 256, 512, 1024])
def test_app_icon_renders_at_every_bundled_size(qapp, size):
    image = draw_icon(size)
    assert image.size() == QSize(size, size)
    left, top, right, bottom = ink_bounds(image)
    # The artwork has to reach most of the way across or it looks lost in the
    # Dock, and it must not touch the edge or the corners get clipped.
    assert (right - left + 1) > size * 0.75
    assert left >= 0 and right <= size - 1


def test_small_renders_drop_the_shadow_and_grow_the_mark(qapp):
    """At 16 points a margin is wasted pixels, so the mark fills more of it."""
    def coverage(size: int) -> float:
        left, top, right, bottom = ink_bounds(draw_icon(size))
        return (right - left + 1) / size

    assert coverage(16) > coverage(512)


# -- the menu bar item ------------------------------------------------------
def test_menu_bar_icon_is_a_template(qapp):
    assert menu_bar_icon().isMask()


def test_menu_bar_icon_is_evenly_spaced(qapp):
    """Equal clear space on all four sides. An odd pixel here is visible."""
    width, height = _BAR_CANVAS
    image = menu_bar_icon().pixmap(width, height).toImage()
    left, top, right, bottom = ink_bounds(image)
    assert left == width - 1 - right
    assert top == height - 1 - bottom
    assert left == pytest.approx(_BAR_MARGIN, abs=1)
    assert top == pytest.approx(_BAR_MARGIN, abs=1)


def test_menu_bar_icon_is_large_enough_for_the_bar(qapp):
    """Drawn at 2x, so the glyph should land near 20 by 14 points on screen."""
    width, height = _BAR_CANVAS
    image = menu_bar_icon().pixmap(width, height).toImage()
    left, top, right, bottom = ink_bounds(image)
    points_wide = (right - left + 1) / 2.0
    points_high = (bottom - top + 1) / 2.0
    assert 18.0 <= points_wide <= 23.0
    assert 12.0 <= points_high <= 17.0


def test_menu_bar_icon_fits_the_status_bar(qapp):
    """Qt asks the icon for a size that fits the bar; it must not be scaled up."""
    icon = menu_bar_icon()
    for bar_pixels in (44, 48):
        actual = icon.actualSize(QSize(bar_pixels, bar_pixels))
        assert actual.width() <= bar_pixels
        assert actual.height() <= bar_pixels
