#!/usr/bin/env python3
"""Play real records through Music rider, headless, and measure it.

The scene is a game, and a game is not finished when its tests pass. A
test says a block arrives on the beat when the chart is three evenly
spaced kicks; a record says whether it arrives on the beat when the
detector heard the kick 40 ms late, the tempo came out at 87.3 and the
chorus is twice as loud as the intro. Nearly everything worth fixing in
this scene was found here rather than in the suite, and then written
back into the suite as a test.

It drives the real pane - the same Spectrum widget the window uses, fed
by the app's own decoder, analysis and beat map - rather than a state
built by hand, so what it measures is what somebody playing the track
would see. The only things faked are the two clocks, which are stepped
by exactly one frame each frame so that a slow machine measures the same
run as a fast one.

    ./dev playtest ~/Music/*.mp3
    ./dev playtest track.mp3 --seconds 90 --mode Puzzle
    ./dev playtest track.mp3 --save frame.png

No path, no song and nothing measured here is ever written into the
repository: songs are somebody's music, and a frame of one is a picture
of somebody's music. Pass the files on the command line.

What it reports, per track, and what each one is for:

  off        How far each block was from the beat it belongs to, as a
             median and a worst case, in milliseconds. This is the whole
             point of the scene: a dodge you begin on the beat has to
             land on it.
  speed      The road's own speed in world units a second - its floor,
             its mean and its peak. A road that stops between beats
             reads as a stutter however good the timing is.
  stall      The share of frames under a tenth of the mean speed.
  back       Frames where the road moved backwards. Should be zero: a
             road may only go forwards, and a seek is the one exception.
  empty      The share of frames with nothing on the road at all.
  hits       What a player who dodges perfectly still gets hit by. Any
             hit here is the chart asking for something nobody can do.
  coins      Coins taken by that player, and the best row of them. A
             player who never goes near an obstacle takes none, so this
             is really "did the coins make anybody ride differently".
  twists     Corkscrews the track earned, at its loudest moments.
  ms         What a frame costs, mean and worst, at 1280x720.
"""

from __future__ import annotations

import argparse
import math
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

#: How long to wait for one file to decode and analyse before giving up.
#: A four minute track takes a few seconds; this is the point at which
#: something has gone wrong rather than slow.
PATIENCE = 180.0

#: How often a real media player reports where it is. The pane runs its
#: own clock between these and leans on them - see Spectrum._heard - so
#: reporting every frame would measure something the app never does.
REPORTS_A_SECOND = 4.0


def decoded(path, patience=PATIENCE):
    """The app's own analysis of a file, or None with a reason.

    Runs the real decoder on its real threads and spins a Qt event loop
    until the callbacks land, because that is the only way this data is
    ever produced.
    """
    from PySide6.QtCore import QCoreApplication, QEventLoop
    from PySide6.QtWidgets import QApplication

    import attachment_audio

    # A widgets application rather than a GUI one: the pane is a QWidget
    # and Qt refuses to make one without it.
    app = QApplication.instance() or QApplication([])
    got = {}

    def done(result):
        got["analysis"] = result

    def failed(detail):
        got["failed"] = detail

    def kit(elements):
        got["elements"] = elements

    handle = attachment_audio.decode(str(path), done, failed,
                                     on_elements=kit)
    if handle is None:
        return None, "no decoder in this build"
    started = time.monotonic()
    loop = QEventLoop()
    # Both halves: the analysis lands first and the drums a few seconds
    # after it, and the chart is what the road is built from.
    while ("analysis" not in got or "elements" not in got):
        if "failed" in got:
            return None, got["failed"]
        if time.monotonic() - started > patience:
            handle.cancel()
            return None, f"gave up after {patience:.0f}s"
        loop.processEvents(QEventLoop.ProcessEventsFlag.AllEvents, 20)
        QCoreApplication.processEvents()
    frames, shapes, vectors, calibration, beats = got["analysis"]
    return {"frames": frames, "traces": shapes, "vectors": vectors,
            "calibration": calibration, "beats": beats,
            "elements": got["elements"]}, None


def paned(got, mode="Mono", size=(1280, 720)):
    """A real pane, loaded with a real analysis, showing the rider."""
    import attachment_audio
    import visualizers
    from attachment_widgets import Spectrum

    pane = Spectrum()
    pane.resize(*size)
    pane.set_unbounded(True)
    pane.set_traces(got["traces"], got["vectors"])
    pane.set_frames(got["frames"], attachment_audio.RATE)
    pane.set_calibration(got["calibration"])
    pane.set_beats(got["beats"])
    pane.set_elements(got["elements"])
    pane.set_labels([str(c) for c in attachment_audio.CENTRES])
    pane._reveal_changed(1.0)
    rider = next(s for s in visualizers.SCENES if s.name == "Music rider")
    rider = type(rider)()
    rider._mode = mode
    pane.set_scene(rider)
    pane.set_playing(True)
    return pane, rider


class Clock:
    """The clock the pane and the scene read, stepped by hand.

    A headless run that let them read the wall clock would measure the
    machine rather than the scene: every frame would be a different
    length, and the scene's whole job is to be the same on every
    machine.

    ``time.monotonic`` itself, rather than each module's name for it.
    The pane imports it inside the methods that use it - a local
    ``import time as _time`` binds from sys.modules and walks straight
    past a module attribute somebody swapped, so patching the two module
    namespaces left the pane on the wall clock while the scene ran on
    this one. What that measured was the pane's clock standing still
    while the playhead ran, which reads exactly like the picture lagging
    a third of a beat behind the music, and is not.
    """

    def __init__(self, start=1000.0):
        self.now = start
        self._was = None

    def __enter__(self):
        import time as real

        self._was = real.monotonic
        real.monotonic = self.monotonic
        return self

    def __exit__(self, *_):
        import time as real

        real.monotonic = self._was

    def monotonic(self):
        return self.now

    def step(self, seconds):
        self.now += seconds


def dodgeable(scene, blocks):
    """Can a perfect player get through this chart untouched?

    Audiosurf's greys are avoidable, always. Three lanes and a figure
    that closes two of them leaves one, and a slalom closes them one
    after another - so the question is not "is a lane free" but "is a
    free lane *reachable* from the last one, in the time between".

    Solved rather than sampled: walk the figures in order keeping the
    set of lanes a player could be in, and if that set ever empties the
    chart has asked for something nobody can do.
    """
    greys = sorted((when, lane) for when, lane, _k, _d, grey in blocks
                   if grey)
    if not greys:
        return None
    moments = []
    for when, lane in greys:
        # Everything inside a slalom's own step is one moment.
        if moments and when - moments[-1][0] < 0.08:
            moments[-1][1].add(lane)
        else:
            moments.append((when, {lane}))
    lanes = set(range(scene.LANES))
    could = set(lanes)
    worst = None
    was = None
    for when, blocked in moments:
        if was is not None:
            # The slide is nine tenths done in about 50 ms whatever the
            # distance, so anything wider than that is reachable.
            gap = when - was
            could = set(lanes) if gap >= 0.07 else set(could)
        could = could - blocked
        room = len(lanes - blocked)
        if worst is None or room < worst[0]:
            worst = (room, round(when, 2), sorted(blocked))
        if not could:
            return {"stuck_at": round(when, 2), "blocked": sorted(blocked),
                    "tightest": worst, "moments": len(moments)}
        was = when
    return {"stuck_at": None, "tightest": worst, "moments": len(moments)}


def steer_well(scene):
    """Play it properly: take the free lane, and take what pays.

    Not an optimal player - an attentive one. It looks at the next
    figure due, moves out of a lane that figure closes, and otherwise
    goes for whatever is worth having in the lanes that are still open:
    a coin, a power block, a prize.

    Coins are the point of the exercise. They sit in the lane *beside*
    an obstacle, so a player that only avoided things would never take
    one - which is exactly the behaviour the coins exist to change, and
    exactly what this has to be able to measure.
    """
    soon = [b for b in scene._blocks
            if not b[3] and b[0] > scene._heard]
    if not soon:
        return
    due = min(b[0] for b in soon)
    here = [b for b in soon if b[0] - due < 0.12]
    shut = {b[1] for b in here if b[4]}
    worth = {b[1] for b in here if not b[4]}
    if scene._lane in shut or (worth and scene._lane not in worth):
        want = sorted(worth - shut) or sorted(
            set(range(scene.LANES)) - shut)
        if want:
            aim = min(want, key=lambda lane: abs(lane - scene._lane))
            if aim != scene._lane:
                scene.steer(1 if aim > scene._lane else -1)


def ride(got, seconds=45.0, mode="Mono", fps=60, save=None, steer=True,
         stop_from=None, stop_to=None):
    """Play the track and measure what happens. See the module docstring."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QImage, QPainter

    size = (1280, 720)
    pane, scene = paned(got, mode=mode, size=size)
    image = QImage(size[0], size[1], QImage.Format.Format_ARGB32_Premultiplied)
    rect = QRectF(0, 0, size[0], size[1])

    offs, speeds, costs = [], [], []
    backwards = empty = 0
    was_at = None
    side = {}                       # block -> which side of the rider
    still = None
    seen_blocks = {}
    reported = -1.0
    with Clock() as clock:
        painter = QPainter(image)
        try:
            for frame in range(int(seconds * fps)):
                clock.step(1.0 / fps)
                at = frame / fps
                stopped = (stop_from is not None and stop_to is not None
                           and stop_from <= at < stop_to)
                # A real player reports a few times a second, not sixty.
                if not stopped and at - reported >= 1.0 / REPORTS_A_SECOND:
                    reported = at
                    pane.set_position(int(at * 1000.0))
                if steer and not stopped:
                    steer_well(scene)
                # The pane's own frame: what the window's timer calls.
                # Everything the scene is handed is built in here, so a
                # run that only painted would paint an empty state.
                pane._tick()
                image.fill(QColor(0, 0, 0))
                started = time.perf_counter()
                pane._paint_scene(painter, rect)
                costs.append((time.perf_counter() - started) * 1000.0)

                # Every block that has been laid, kept by identity so a
                # block is measured once however long it is on the road.
                for block in scene._blocks:
                    seen_blocks[id(block)] = block
                    # Which side of the rider it is on, in road units.
                    now = scene._where(block[0]) - scene.RIDER_AT
                    key = id(block)
                    if key in side and side[key] > 0.0 >= now:
                        # It crossed the rider this frame. How far from
                        # its own beat did that happen?
                        offs.append((scene._heard - block[0]) * 1000.0)
                    side[key] = now
                if scene._speed > 0.0:
                    speeds.append(scene._speed)
                if was_at is not None and scene._at < was_at - 1e-9:
                    backwards += 1
                was_at = scene._at
                if not any(0.0 < scene._where(b[0]) < scene.FAR
                           for b in scene._blocks):
                    empty += 1
                if stopped:
                    # Nothing at all may move under a stopped track.
                    now = image.copy()
                    if still is None:
                        still = (now, 0, 0)
                    else:
                        was, same, total = still
                        alike = was == now
                        still = (now, same + (1 if alike else 0), total + 1)
                if save and frame == int(seconds * fps) - 1:
                    image.save(str(save))
        finally:
            painter.end()

    blocks = list(seen_blocks.values())
    report = scene.report()
    return {
        "bpm": (60.0 / scene._beat) if scene._beat > 0.0 else 0.0,
        "blocks": len(blocks),
        "off": (statistics.median(abs(o) for o in offs) if offs else None,
                max((abs(o) for o in offs), default=0.0)),
        "speed": ((min(speeds), statistics.fmean(speeds), max(speeds))
                  if speeds else (0.0, 0.0, 0.0)),
        "stall": (sum(1 for s in speeds
                      if s < statistics.fmean(speeds) * 0.1) / len(speeds)
                  if speeds else 0.0),
        "backwards": backwards,
        "empty": empty / max(1, int(seconds * fps)),
        "ms": (statistics.fmean(costs), max(costs)),
        "fair": dodgeable(scene, blocks),
        "score": report,
        "still": (None if still is None or still[2] == 0
                  else still[1] / still[2]),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Play real records through Music rider and measure it.")
    parser.add_argument("songs", nargs="+", type=Path)
    parser.add_argument("--seconds", type=float, default=45.0,
                        help="how much of each track to play (default 45)")
    parser.add_argument("--mode", default="Mono", choices=("Mono", "Puzzle"))
    parser.add_argument("--fps", type=int, default=60)
    parser.add_argument("--save", type=Path, default=None,
                        help="write the last frame of the first track here")
    parser.add_argument("--no-steer", action="store_true",
                        help="nobody at the controls, to see the chart raw")
    parser.add_argument("--pause-from", type=float, default=None)
    parser.add_argument("--pause-to", type=float, default=None)
    args = parser.parse_args(argv)

    print(f"{'track':26} {'bpm':>5} {'blocks':>6} {'off ms':>11} "
          f"{'speed lo/mean/hi':>20} {'back':>5} {'empty':>6} "
          f"{'hits':>5} {'coins':>7} {'twist':>5} {'ms':>11}")
    worst = 0
    for path in args.songs:
        name = path.name[:30]
        got, why = decoded(path)
        if got is None:
            print(f"{name:30} could not be analysed: {why}")
            worst = max(worst, 1)
            continue
        out = ride(got, seconds=args.seconds, mode=args.mode, fps=args.fps,
                   save=args.save if path is args.songs[0] else None,
                   steer=not args.no_steer,
                   stop_from=args.pause_from, stop_to=args.pause_to)
        median, most = out["off"]
        off = "-" if median is None else f"{median:5.0f}/{most:5.0f}"
        low, mean, high = out["speed"]
        got = out["score"]
        print(f"{name[:26]:26} {out['bpm']:5.0f} {out['blocks']:6d} "
              f"{off:>11} "
              f"{low:5.1f}/{mean:5.1f}/{high:5.1f} "
              f"{out['backwards']:5d} {out['empty']:6.1%} "
              f"{got['hits']:5d} "
              f"{got['coins']:3d}/x{got['coin_best']:<3d} "
              f"{got['twists']:5d} "
              f"{out['ms'][0]:5.1f}/{out['ms'][1]:5.1f}")
        fair = out["fair"]
        if fair and fair["stuck_at"] is not None:
            print(f"     UNFAIR: nothing to move to at {fair['stuck_at']}s, "
                  f"lanes {fair['blocked']} closed")
            worst = max(worst, 1)
        elif fair:
            room, when, blocked = fair["tightest"]
            print(f"     fair: {fair['moments']} hazards, tightest left "
                  f"{room} lane(s) open at {when}s")
        if out["still"] is not None and out["still"] < 1.0:
            print(f"     MOVING WHILE STOPPED: only "
                  f"{out['still']:.0%} of the paused frames were identical")
            worst = max(worst, 1)
        if out["backwards"]:
            print(f"     ROAD WENT BACKWARDS on {out['backwards']} frames")
            worst = max(worst, 1)
        if median is not None and median > 40.0:
            print(f"     OFF THE BEAT: the median block arrives "
                  f"{median:.0f} ms from its own beat")
            worst = max(worst, 1)
    return worst


if __name__ == "__main__":
    raise SystemExit(main())
