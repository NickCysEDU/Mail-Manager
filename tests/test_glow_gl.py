"""The glow's blur, worked out before anything reaches the card.

The card reads two neighbouring texels as one, between them, so each pair
of the Gaussian's weights is one read at an offset. Here the pairs are put
back into texels, as the card's linear smoothing does, and compared with
the Gaussian itself. Drawing with it is tested on the card: see
test_gpu_canvas.TestTheGlowOnTheCard.
"""

from __future__ import annotations

import math

import pytest

import glow_gl


def _texels(centre: float, pairs) -> dict:
    """The weight each texel gets, from the middle out, when every pair is
    read at its offset with linear smoothing."""
    found = {0: centre}
    for offset, weight in pairs:
        near = math.floor(offset)
        share = offset - near
        found[near] = found.get(near, 0.0) + weight * (1.0 - share)
        found[near + 1] = found.get(near + 1, 0.0) + weight * share
    return found


class TestTheBlursWeights:
    @pytest.mark.parametrize("sigma", [0.87, 1.0, 1.94, 3.0])
    def test_read_in_pairs_they_are_the_gaussian(self, sigma):
        centre, pairs = glow_gl.taps(sigma)
        texels = _texels(centre, pairs)
        reach = max(texels)
        wanted = [math.exp(-(k * k) / (2.0 * sigma * sigma))
                  for k in range(reach + 1)]
        total = wanted[0] + 2.0 * sum(wanted[1:])
        for k in range(reach + 1):
            assert texels.get(k, 0.0) == pytest.approx(wanted[k] / total,
                                                       abs=1e-9), k
        # And far enough out that what is left off is not seen.
        assert math.exp(-((reach + 1) ** 2) / (2.0 * sigma * sigma)) < 0.02

    @pytest.mark.parametrize("sigma", [0.0, 0.87, 1.94, 3.0, 40.0])
    def test_they_add_up_to_the_light_there_was(self, sigma):
        centre, pairs = glow_gl.taps(sigma)
        assert centre + 2.0 * sum(w for _o, w in pairs) == pytest.approx(1.0)

    def test_a_blur_too_narrow_to_reach_a_neighbour_reads_none(self):
        assert glow_gl.taps(0.1) == (1.0, ())

    def test_a_wide_one_stops_at_what_the_shader_reads(self):
        centre, pairs = glow_gl.taps(40.0)
        assert len(pairs) == glow_gl.MOST_PAIRS
