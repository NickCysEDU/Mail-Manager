"""What Music rider sounds like, under the music.

"Xxx xxxxx xxxx xx xxxxx xxxxx xx xxxxxxx: xxxxxxx xxx xxxxx, xxxxxxx xxx
xxxxxxxx, xxxxxxx xxxx xxxxxx xx xxxxx xx xxxxxxx xxx xxxxxxxxx." A game
you only watch is half a game. Audiosurf answers every block you take with
a note, and the note climbs as your grid fills - so a run is something you
hear building, in the key of nothing in particular but always in tune with
itself, and a pickup lands on the beat because the block did.

Everything here is made rather than recorded: a few hundred milliseconds
of arithmetic per sound, written once as a short WAV in the app's cache
and played through Qt's sound effects. No file ships with the app, and
nothing leaves the machine.

A hit is the one sound that is not a note. It is a thump under the music
and a crunch over it, and the music itself ducks for a moment - which is
the part that is felt rather than heard.
"""

from __future__ import annotations

import logging
import math
import random
import wave
from array import array
from pathlib import Path
from typing import Callable, Dict, List, Optional

log = logging.getLogger(__name__)

RATE = 44100
#: Bumped whenever a sound is changed, so a cache from an older build is
#: not played instead of it.
VERSION = 3

#: The steps a run climbs through: a major pentatonic, two octaves, which
#: sounds like a tune whichever way it is walked and never like a wrong
#: note against the music underneath it.
PENTATONIC = (0, 2, 4, 7, 9, 12, 14, 16, 19, 21, 24)
#: Where the pickups start: E5.
PRIZE_ROOT = 659.25
#: And the coins, a brighter octave and a half up.
COIN_ROOT = 1318.5


# ==========================================================================
# Synthesis
# ==========================================================================
def _silence(seconds: float) -> List[float]:
    return [0.0] * int(seconds * RATE)


def _bell(out: List[float], start: float, freq: float, length: float,
          loud: float = 1.0, bright: float = 2.2, ratio: float = 2.0) -> None:
    """A struck bell by frequency modulation: a carrier and a modulator
    at ``ratio`` times it, the modulation dying faster than the note, so
    it starts bright and rings out pure."""
    first = int(start * RATE)
    count = int(length * RATE)
    for index in range(count):
        at = first + index
        if at >= len(out):
            break
        t = index / RATE
        attack = min(1.0, t / 0.003)
        env = attack * math.exp(-t / (length * 0.28))
        index_env = bright * math.exp(-t / (length * 0.10))
        out[at] += loud * env * math.sin(
            math.tau * freq * t
            + index_env * math.sin(math.tau * freq * ratio * t))


def _sine(out: List[float], start: float, freq: float, length: float,
          loud: float = 1.0, drop: float = 0.0, decay: float = 0.3) -> None:
    """A sine, optionally falling in pitch by ``drop`` of itself."""
    first = int(start * RATE)
    phase = 0.0
    for index in range(int(length * RATE)):
        at = first + index
        if at >= len(out):
            break
        t = index / RATE
        f = freq * (1.0 - drop * min(1.0, t / length))
        phase += math.tau * f / RATE
        env = min(1.0, t / 0.002) * math.exp(-t / (length * decay))
        out[at] += loud * env * math.sin(phase)


def _noise(out: List[float], start: float, length: float, loud: float,
           smooth: float, rng: random.Random, high: bool = False,
           decay: float = 0.25) -> None:
    """Noise through a one-pole filter: low-passed, or what the low-pass
    took out, for the high end."""
    first = int(start * RATE)
    held = 0.0
    for index in range(int(length * RATE)):
        at = first + index
        if at >= len(out):
            break
        t = index / RATE
        raw = rng.uniform(-1.0, 1.0)
        held += (raw - held) * smooth
        value = raw - held if high else held
        env = min(1.0, t / 0.001) * math.exp(-t / (length * decay))
        out[at] += loud * env * value


def _finish(samples: List[float], peak: float = 0.7) -> array:
    """To 16-bit, the loudest point at ``peak`` of full scale, with a few
    milliseconds of fade at the end so nothing clicks."""
    top = max((abs(v) for v in samples), default=0.0) or 1.0
    scale = peak / top
    fade = int(0.004 * RATE)
    out = array("h")
    count = len(samples)
    for index, value in enumerate(samples):
        tail = min(1.0, (count - index) / max(1, fade))
        out.append(int(max(-1.0, min(1.0, value * scale * tail)) * 32767))
    return out


def prize(step: int) -> array:
    """A pickup: a bell, pitched ``step`` notes up the scale."""
    freq = PRIZE_ROOT * 2 ** (PENTATONIC[step % len(PENTATONIC)] / 12.0)
    out = _silence(0.42)
    _bell(out, 0.0, freq, 0.42, 1.0, bright=2.4)
    _bell(out, 0.0, freq * 2.0, 0.20, 0.25, bright=1.2, ratio=3.0)
    _sine(out, 0.0, freq / 2.0, 0.22, 0.30)
    return _finish(out, 0.55)


def coin(step: int) -> array:
    """A coin: a short bright ting, climbing along a row."""
    freq = COIN_ROOT * 2 ** (PENTATONIC[step % len(PENTATONIC)] / 12.0)
    out = _silence(0.26)
    _bell(out, 0.0, freq, 0.26, 1.0, bright=1.4, ratio=1.5)
    _bell(out, 0.035, freq * 1.5, 0.20, 0.6, bright=1.0, ratio=2.0)
    return _finish(out, 0.45)


def hit() -> array:
    """A thump under the music, a crunch over it, and a stab that does
    not belong in any key."""
    rng = random.Random(7)
    out = _silence(0.55)
    _sine(out, 0.0, 120.0, 0.34, 1.0, drop=0.62, decay=0.45)
    _noise(out, 0.0, 0.16, 0.9, 0.35, rng, decay=0.30)
    _noise(out, 0.0, 0.10, 0.35, 0.10, rng, high=True, decay=0.25)
    for freq in (185.0, 196.0, 277.2):
        for harmonic in (1, 2, 3):
            _sine(out, 0.01, freq * harmonic, 0.42, 0.22 / harmonic,
                  decay=0.35)
    return _finish(out, 0.85)


def shatter() -> array:
    """The shield going: glass, bright and falling."""
    rng = random.Random(11)
    out = _silence(0.5)
    _noise(out, 0.0, 0.35, 0.8, 0.25, rng, high=True, decay=0.25)
    for index in range(9):
        _bell(out, rng.uniform(0.0, 0.08), rng.uniform(2600.0, 6200.0),
              rng.uniform(0.15, 0.35), 0.35, bright=0.8, ratio=2.7)
    _sine(out, 0.0, 90.0, 0.2, 0.5, drop=0.4)
    return _finish(out, 0.6)


def power() -> array:
    """A power block: a rising sweep and a bright fifth on top."""
    out = _silence(0.62)
    phase = 0.0
    count = int(0.42 * RATE)
    for index in range(count):
        t = index / RATE
        freq = 220.0 * 2 ** (3.2 * t / 0.42)
        phase += math.tau * freq / RATE
        env = math.sin(math.pi * min(1.0, t / 0.42))
        out[index] += 0.45 * env * (math.sin(phase) + 0.3 * math.sin(phase * 3))
    _bell(out, 0.34, PRIZE_ROOT * 2, 0.28, 0.9, bright=2.0)
    _bell(out, 0.34, PRIZE_ROOT * 3, 0.28, 0.6, bright=1.4)
    return _finish(out, 0.55)


def milestone() -> array:
    """A chain milestone: a quick rising arpeggio that rings."""
    out = _silence(0.9)
    for index, step in enumerate((0, 4, 7, 12, 16)):
        freq = PRIZE_ROOT * 2 ** (step / 12.0)
        _bell(out, index * 0.055, freq, 0.6, 0.8, bright=2.0)
    return _finish(out, 0.55)


def landing() -> array:
    """Back on the road after a jump: a soft thump and a hiss."""
    rng = random.Random(3)
    out = _silence(0.3)
    _sine(out, 0.0, 95.0, 0.22, 1.0, drop=0.35)
    _noise(out, 0.0, 0.18, 0.4, 0.08, rng, decay=0.3)
    return _finish(out, 0.55)


def clear() -> array:
    """A cluster cleared in the puzzle: a chord."""
    out = _silence(0.7)
    for step in (0, 4, 7, 11):
        _bell(out, 0.0, PRIZE_ROOT * 2 ** (step / 12.0), 0.6, 0.6, bright=1.6)
    return _finish(out, 0.55)


def climb(step: int) -> int:
    """Which note of the scale the ``step``th pickup of a run is.

    Up the scale one note a pickup, and then round the top octave rather
    than back to the bottom: a run of forty should go on sounding like a
    run, not start again as if it had been broken.
    """
    top = len(PENTATONIC)
    if step < top:
        return max(0, step)
    return 5 + (step - top) % (top - 5)


#: Everything there is to make, by name.
SOUNDS: Dict[str, Callable[[], array]] = {
    "hit": hit, "shatter": shatter, "power": power,
    "milestone": milestone, "landing": landing, "clear": clear,
}
for _step in range(len(PENTATONIC)):
    SOUNDS[f"prize{_step}"] = (lambda step=_step: prize(step))
for _step in range(8):
    SOUNDS[f"coin{_step}"] = (lambda step=_step: coin(step))


def write(name: str, folder: Path) -> Path:
    """The named sound as a WAV in ``folder``, made if it is not there."""
    path = folder / f"rider-{VERSION}-{name}.wav"
    if path.exists() and path.stat().st_size > 44:
        return path
    samples = SOUNDS[name]()
    folder.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    with wave.open(str(partial), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    partial.replace(path)
    return path


def make_all(folder) -> None:
    """Every sound, written into ``folder``. What a worker process runs."""
    for name in SOUNDS:
        write(name, Path(folder))


def make_elsewhere(folder) -> None:
    """Make the sounds in a process of their own, if they are not made.

    A few hundred milliseconds of arithmetic, the first time the rider is
    chosen on a machine and never again - but on the thread drawing the
    picture it would be the moment the scene appeared, which is the worst
    moment for it. Anything not made by the time it is wanted is made
    there and then instead.
    """
    folder = Path(folder)
    if all((folder / f"rider-{VERSION}-{name}.wav").exists() for name in SOUNDS):
        return
    try:
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        context.Process(target=make_all, args=(str(folder),),
                        name="mail-manager-sounds", daemon=True).start()
    except Exception as exc:      # noqa: BLE001 - made when wanted instead
        log.info("Making the rider's sounds when they are wanted (%s).", exc)


# ==========================================================================
# Playing them
# ==========================================================================
class SoundBoard:
    """The game's sounds, answering what the game did.

    Reads the scene's own record of what happened - the same pops the
    picture answers - so a sound and a flash can never disagree about
    whether something was taken. ``volume`` says how loud the player is
    set, and a sound goes under the music at a share of that; ``duck``
    is told to pull the music down for a moment on a hit.
    """

    #: How much of the player's own volume the effects are played at.
    LEVEL = 0.55
    #: How many of each sound can be going at once. A row of coins is
    #: three in a third of a second, and restarting one sound cuts the
    #: last one off.
    VOICES = 3

    def __init__(self, folder: Path, volume: Callable[[], float] = lambda: 0.7,
                 duck: Optional[Callable[[float, float], None]] = None) -> None:
        self.folder = Path(folder)
        self._volume = volume
        self._duck = duck
        self._voices: Dict[str, list] = {}
        self._next: Dict[str, int] = {}
        self._seen: set = set()
        self.enabled = True
        self._broken = False

    def _voice(self, name: str):
        voices = self._voices.get(name)
        if voices is None:
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

            path = write(name, self.folder)
            voices = []
            for _ in range(self.VOICES):
                effect = QSoundEffect()
                effect.setSource(QUrl.fromLocalFile(str(path)))
                voices.append(effect)
            self._voices[name] = voices
            self._next[name] = 0
        index = self._next[name]
        self._next[name] = (index + 1) % len(voices)
        return voices[index]

    def play(self, name: str, loud: float = 1.0) -> None:
        if not self.enabled or self._broken:
            return
        try:
            effect = self._voice(name)
            effect.setVolume(max(0.0, min(1.0, self._volume() * self.LEVEL
                                          * loud)))
            effect.play()
        except Exception as exc:      # noqa: BLE001 - a game without sound
            self._broken = True
            log.info("The rider's sounds are off: %s", exc)

    def prepare(self) -> None:
        """Load every sound already made, before any is wanted: Qt loads
        a sound effect in the background, and the first pickup should not
        be the one that goes unheard while it does."""
        if self._broken:
            return
        for name in SOUNDS:
            path = self.folder / f"rider-{VERSION}-{name}.wav"
            if name not in self._voices and path.exists():
                try:
                    self._voice(name)
                except Exception as exc:      # noqa: BLE001
                    self._broken = True
                    log.info("The rider's sounds are off: %s", exc)
                    return

    def warm(self) -> None:
        """Make every sound now, before anything is played, so the first
        pickup is not the moment a file is written."""
        for name in SOUNDS:
            try:
                write(name, self.folder)
            except OSError as exc:
                log.info("Could not make the rider's sounds: %s", exc)
                self._broken = True
                return

    def listen(self, scene) -> List[str]:
        """What the scene did since last asked, played. Returns the names
        played, for whoever wants to know."""
        pops = list(getattr(scene, "_pops", ()) or ())
        alive = {id(pop) for pop in pops}
        self._seen &= alive
        played = []
        for pop in pops:
            if id(pop) in self._seen:
                continue
            self._seen.add(id(pop))
            name = self.sound_for(scene, pop[0])
            if name is None:
                continue
            self.play(name, min(1.4, float(pop[2])))
            played.append(name)
            if pop[0] == "hit" and self._duck is not None:
                # The music goes under for a moment: felt more than heard.
                self._duck(0.45, 0.28)
        return played

    @staticmethod
    def sound_for(scene, kind: str) -> Optional[str]:
        """Which sound answers ``kind``, pitched by how the run is going."""
        if kind == "prize":
            if getattr(scene, "_mode", "") == "Puzzle":
                filled = sum(len(pile) for pile in getattr(scene, "_cells", ()))
                return f"prize{min(len(PENTATONIC) - 1, filled)}"
            return f"prize{climb(int(getattr(scene, '_chain', 1)) - 1)}"
        if kind == "coin":
            run = int(getattr(scene, "_coin_run", 1))
            return f"coin{min(7, max(0, run - 1))}"
        return {"hit": "hit", "shatter": "shatter", "power": "power",
                "milestone": "milestone", "air": "landing",
                "clear": "clear"}.get(kind)
