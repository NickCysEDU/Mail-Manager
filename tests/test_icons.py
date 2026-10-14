"""The pictures on the mail buttons: every name draws something, in the
colour asked for, and the same request is the same icon."""

from __future__ import annotations

import icons


def _pixels(icon, size=18):

    image = icon.pixmap(size, size).toImage()
    return [(x, y) for x in range(image.width()) for y in range(image.height())
            if image.pixelColor(x, y).alpha() > 40], image


class TestTheIcons:
    def test_every_name_draws_something_in_its_colour(self, qapp):
        for name in icons.NAMES:
            painted, image = _pixels(icons.icon(name, "#ff0000"))
            assert len(painted) > 12, name
            x, y = painted[len(painted) // 2]
            colour = image.pixelColor(x, y)
            assert colour.red() > 200 and colour.green() < 60, name

    def test_an_unknown_name_is_empty_rather_than_an_error(self, qapp):
        painted, _image = _pixels(icons.icon("no-such-picture"))
        assert painted == []

    def test_the_same_request_is_the_same_icon(self, qapp):
        assert icons.icon("reply", "#101010") is icons.icon("reply", "#101010")
        assert icons.icon("reply", "#101010") is not icons.icon("reply", "#f0f0f0")

    def test_the_replies_point_back_and_a_forward_points_on(self, qapp):
        back, _ = _pixels(icons.icon("reply"))
        on, _ = _pixels(icons.icon("forward"))
        # The arrowhead is where the pixels bunch: left for a reply, right
        # for a forward.
        left_back = sum(1 for x, _y in back if x < 9) / len(back)
        left_on = sum(1 for x, _y in on if x < 9) / len(on)
        assert left_back > left_on

    def test_new_message_is_a_page_with_a_pencil_in_at_its_corner(self, qapp):
        """It was an L and a pencil drawn as an outline, which at the bar's
        size closed up into one stroke, and the two read as an arrow pointing
        down and to the left."""
        size = 108
        image = icons.icon("compose", "#000000", size // 2).pixmap(size, size).toImage()

        def inked(x: float, y: float) -> bool:
            return image.pixelColor(int(x * size), int(y * size)).alpha() > 128

        def along(points) -> float:
            points = list(points)
            return sum(inked(x, y) for x, y in points) / len(points)

        steps = [n / 40 for n in range(41)]
        assert along((0.22 + 0.46 * t, 0.84) for t in steps) == 1.0, "the page's foot"
        assert along((0.16, 0.36 + 0.36 * t) for t in steps) == 1.0, "its left side"
        assert along((0.28 + 0.12 * t, 0.24) for t in steps) == 1.0, "its top"
        # Where a closed page's top and right would run, clear of the pencil
        # that crosses the gap: the corner it comes in by is open.
        assert not inked(0.58, 0.24) and not inked(0.74, 0.48), "open at the corner"
        assert along((0.54 + 0.30 * t, 0.48 - 0.30 * t) for t in steps) == 1.0, (
            "the pencil, solid out past the page")
