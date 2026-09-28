"""A whole written record, ridden through the real game, frame by frame.

songkit writes a record's drums, sections and loudness; this hands them
to Music rider the way the pane would - one state object, the same chart
and shape every frame, the playhead moving on a clock this owns - and
keeps a log of every figure the game laid and every hit it took.
"""

from __future__ import annotations

import time as _time
from typing import Dict, List, Optional

import songkit


class Log:
    def __init__(self) -> None:
        self.figures: List[tuple] = []
        self.blocks: Dict[int, list] = {}
        #: (time, how fast the road moved) every frame.
        self.speeds: List[tuple] = []
        #: (the moment a block is for, when it crossed the rider) for
        #: every block that did.
        self.crossings: List[tuple] = []


def kit_for(chart: dict, length: float) -> dict:
    """The kit maps the analysis would hand over: the hits, and the onset
    strength they were found from."""
    import beatmap
    import trackstyle

    out = {}
    for name, times in chart.items():
        flux = tuple(trackstyle.envelope_from(times, 60.0, length))
        out[name] = beatmap.BeatMap(
            beats=tuple(beatmap.Beat(at=at, strength=1.0) for at in times),
            flux=flux if name in beatmap.KEPT_FLUX else (), rate=60.0)
    return out


def ride(style: str = "house", mode: str = "Mono", fps: int = 30,
         shape=songkit.DANCE, offset: float = 0.0, harmony: Optional[dict]
         = None, steer=None, seconds: Optional[float] = None,
         nudge: float = 0.0, drums_at: float = 0.0,
         difficulty: str = "Normal"):
    """Ride a written record of ``style`` from end to end. ``steer`` is
    called with the scene every frame, if given, to play it. ``nudge``
    changes the record's loudness by that much everywhere - a different
    record of the same kind, for anything asking whether two records
    ride alike. ``drums_at`` is how far in the drums' own reading arrives,
    as it does on a real record, a few seconds after the picture."""
    import trackstyle
    import visualizers
    from attachment_widgets import SpectrumState

    chart, contour, beat, truth = songkit.chart(style, shape=shape,
                                                offset=offset)
    if nudge:
        contour = dict(contour, loud=[min(1.0, v + nudge * ((i % 7) - 3) / 3)
                                      for i, v in enumerate(contour["loud"])])
    length = len(contour["loud"]) / contour["rate"]
    kit = kit_for(chart, length)
    counted = visualizers.folded_tempo(60.0 / beat)
    # What the drums worker hands over. At the pane's own counting, as it
    # would be - drum and bass at 87 - and read from there.
    rhythm = trackstyle.rhythm_of(kit, tempo=counted)
    state = SpectrumState()
    state.levels = [0.5] * 27
    state.bass = state.mid = state.high = 0.5
    state.kit = {}
    state.chart = chart
    state.contour = contour
    state.flux = {name: (found.flux, found.rate) for name, found in kit.items()
                  if found.flux}
    state.rhythm = rhythm if drums_at <= 0.0 else None
    state.harmony = harmony
    state.tempo = counted
    scene = visualizers.Rider()
    scene.set_mode(mode)
    scene.set_difficulty(difficulty)
    log = Log()
    laid = scene._shape

    def shape_logged(pattern, when, grey=True, mirrored=False):
        log.figures.append((round(when, 3), pattern, grey, mirrored))
        return laid(pattern, when, grey=grey, mirrored=mirrored)

    scene._shape = shape_logged
    scene._ridden_state = state
    now = [1000.0]
    was = _time.monotonic
    visualizers.time.monotonic = lambda: now[0]
    side = {}
    try:
        end = min(length, seconds) if seconds else length
        for frame in range(int(end * fps)):
            now[0] += 1.0 / fps
            at = offset + frame / fps
            state.at = at
            state.beat_at = (at / (60.0 / counted)) % 1.0
            if drums_at > 0.0 and at >= drums_at:
                state.rhythm = rhythm
            if steer is not None:
                steer(scene)
            scene._step(state)
            log.speeds.append((at, scene._speed))
            for block in scene._blocks:
                log.blocks.setdefault(id(block), block)
                # Which side of the rider it is, as the playtest measures
                # it: a crossing is the frame it goes from ahead to behind.
                ahead = scene._where(block[0]) - scene.RIDER_AT
                if side.get(id(block), 0.0) > 0.0 >= ahead:
                    log.crossings.append((block[0], scene._heard))
                side[id(block)] = ahead
    finally:
        visualizers.time.monotonic = was
    return scene, log, truth, beat
