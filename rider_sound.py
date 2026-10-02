"""What Music rider sounds like, under the music - and in its key.

"Xxx xxxxx xxxx xx xxxxx xxxxx xx xxxxxxx: xxxxxxx xxx xxxxx, xxxxxxx xxx
xxxxxxxx, xxxxxxx xxxx xxxxxx xx xxxxx xx xxxxxxx xxx xxxxxxxxx." A game
you only watch is half a game. Audiosurf answers every block you take with
a note, and the note climbs as the run goes on - so a run is something you
hear building, and a pickup lands on the beat because the block did.

The notes used to be a pentatonic scale on E, whatever was playing, and
over a record in another key they were wrong notes on top of somebody's
melody: "the sounds clash with the melodic elements". So they are played
in the record now. The analysis hears its key, its tuning and the chord
under every moment (see harmony), and a pickup is the next note up *that
chord* - a run is an arpeggio through the song's own changes, tuned to the
record even when it is not at A = 440. Where the key cannot be told - a
noise, a record the analysis is not sure of - the pickups stop being notes
at all and become ticks and shakers, which cannot be out of key because
they have none. (A drum track gets notes in whatever key its drums lean
to, which is harmless: there is no melody for them to clash with.)

Everything here is made rather than recorded: a few hundred milliseconds
of arithmetic per sound, written once as a short WAV in the app's cache
and played through Qt's sound effects. The notes are made for each tuning
the first time a record in it is played, in a process of their own, and
never on the thread drawing the picture; until they exist the unpitched
sounds stand in. No file ships with the app, and nothing leaves the
machine.

"Xxxxxxxxxx xxx xxxx xxxxx xxxxxxxx xxxx xxxxxxxx xxxxx xx xxxxxxx": the
notes are synth voices of the kind the records themselves are made of (see
Synthesis), in stereo, and each note taken echoes twice on the record's own
beat - so the sounds sit in the music's key, its tuning and its time. How
loud they are against the music is somebody's choice (``level``).

A hit is not a note. It is an impact - a sub-bass drop under the music, a
crunch and a zap over it - and the music itself ducks for a moment, which
is the part that is felt rather than heard. It used to carry a stab "in no
key at all", and in a game that now plays in the record's key that was the
one sound left that clashed on purpose.
"""

from __future__ import annotations

import logging
import math
import random
import wave
from array import array
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

RATE = 44100
#: Bumped whenever a sound is changed, so a cache from an older build is
#: not played instead of it.
VERSION = 6

#: Where the pickups ring and where the coins do, as MIDI notes: C4 to C6,
#: and C5 to G6 - in among the music's own notes rather than above them.
PRIZE_LOW, PRIZE_HIGH = 60, 84
COIN_LOW, COIN_HIGH = 72, 91

#: The scales a pickup climbs when the chord is not known but the key is:
#: the pentatonic of the key, which has no note in it that clashes with
#: another.
MAJOR_PENTATONIC = (0, 2, 4, 7, 9)
MINOR_PENTATONIC = (0, 3, 5, 7, 10)

#: A chord's notes above its root.
CHORD_TONES = {"maj": (0, 4, 7), "min": (0, 3, 7)}

#: How finely a record's tuning is followed, in cents. Five is at the
#: edge of what anybody can hear, and it keeps the number of tunings
#: whose notes are ever made small.
TUNING_STEP = 5

#: A key is believed, and the sounds are notes, when the analysis is at
#: least this sure of it. Measured against a DJ program's keys: at 0.1 and
#: over, 58 records in 61 came out the key or its relative - the same seven
#: notes; between 0.03 and 0.06, no key that was not the one or a fifth
#: away from it, which a note chosen from the chord barely notices.
#:
#: Not how much of the record has a pitch (harmony.tonality): on a real
#: mix the drums outweigh everything else in that, and records whose key
#: came out right scored nothing on it.
SURE = 0.05

#: How many tunings' notes are kept in the cache before the oldest go.
TUNINGS_KEPT = 4


# ==========================================================================
# Synthesis
# ==========================================================================
#
# "Xxxxxxxxxx xxx xxxx xxxxx xxxxxxxx xxxx xxxxxxxx xxxxx xx xxxxxxx." The
# sounds are built the way the records they play over are: detuned saw
# waves through a resonant filter that closes as the note rings - the
# pluck every trance and house lead is - frequency-modulated glass for the
# coins, pitch sweeps and filtered noise for the hits and the power, all in
# stereo with the two sides a few cents apart, which is what makes a synth
# sound wide rather than like one speaker. The pitched ones stay on their
# note's own harmonic series, so a pickup is its note and no other; the
# unpitched ones are noise, sweeps and metal with no steady pitch for a
# melody to clash with.

#: The two voices notes are made in: a pickup's and a coin's.
VOICES = ("pluck", "soft")

#: Stereo: every sound is written as two channels, left then right.
CHANNELS = 2

#: The length of one cycle of a wavetable, in samples.
_CYCLE = 2048


def _silence(seconds: float) -> Tuple[List[float], List[float]]:
    count = int(seconds * RATE)
    return [0.0] * count, [0.0] * count


def _table(partials: Sequence[Tuple[int, float]]) -> List[float]:
    """One cycle of a wave made of ``partials``, as (harmonic, level)."""
    out = [0.0] * _CYCLE
    for harmonic, level in partials:
        step = math.tau * harmonic / _CYCLE
        for index in range(_CYCLE):
            out[index] += level * math.sin(step * index)
    return out


def _saw(highest: float) -> List[float]:
    """A sawtooth with every harmonic that stays under the top of what a
    file can hold at ``highest`` hertz - band-limited, so a high note does
    not fold its upper harmonics back down as notes that are not in it."""
    count = max(1, min(48, int(RATE * 0.45 / max(1.0, highest))))
    return _table([(k, 1.0 / k) for k in range(1, count + 1)])


def _square(highest: float) -> List[float]:
    count = max(1, min(48, int(RATE * 0.45 / max(1.0, highest))))
    return _table([(k, 1.0 / k) for k in range(1, count + 1, 2)])


def _play(table: List[float], pitch, count: int,
          phase: float = 0.0) -> List[float]:
    """``count`` samples of ``table`` at ``pitch`` hertz - a number, or a
    function of the time in seconds, for a sweep."""
    out = [0.0] * count
    size = len(table)
    steady = not callable(pitch)
    for index in range(count):
        position = phase * size
        whole = int(position)
        part = position - whole
        here = table[whole % size]
        out[index] = here + (table[(whole + 1) % size] - here) * part
        freq = pitch if steady else pitch(index / RATE)
        phase += freq / RATE
        phase -= int(phase)
    return out


def _filter(signal: List[float], cutoff, resonance: float = 0.7,
            kind: str = "low") -> List[float]:
    """A state-variable filter - the resonant filter of an analogue synth,
    in the form that stays stable at any cutoff (Zavalishin's, with the
    delay taken out of the loop). ``cutoff`` in hertz, or a function of
    the time for a sweep; ``kind`` is "low", "band" or "high"."""
    damping = 1.0 / max(0.05, resonance)
    first = second = 0.0
    out = [0.0] * len(signal)
    steady = not callable(cutoff)
    gain = (math.tan(math.pi * min(cutoff, RATE * 0.45) / RATE)
            if steady else 0.0)
    for index, value in enumerate(signal):
        if not steady:
            gain = math.tan(math.pi * min(max(20.0, cutoff(index / RATE)),
                                          RATE * 0.45) / RATE)
        a1 = 1.0 / (1.0 + gain * (gain + damping))
        a2 = gain * a1
        a3 = gain * a2
        v3 = value - second
        v1 = a1 * first + a2 * v3
        v2 = second + a2 * first + a3 * v3
        first = 2.0 * v1 - first
        second = 2.0 * v2 - second
        if kind == "low":
            out[index] = v2
        elif kind == "band":
            out[index] = v1
        else:
            out[index] = value - damping * v1 - v2
    return out


def _noise(count: int, rng: random.Random, hold: int = 1) -> List[float]:
    """White noise, each value held for ``hold`` samples - held longer, it
    is the grit of a sound played back at too few bits and samples, which
    is most of what "digital" sounds like."""
    out = [0.0] * count
    value = 0.0
    for index in range(count):
        if index % hold == 0:
            value = rng.uniform(-1.0, 1.0)
        out[index] = value
    return out


def _add(into: List[float], signal: Sequence[float], level: float = 1.0,
         envelope=None, start: float = 0.0) -> None:
    """``signal`` into ``into`` from ``start`` seconds, shaped by
    ``envelope``, a function of the time since it started."""
    first = int(start * RATE)
    for index, value in enumerate(signal):
        at = first + index
        if at >= len(into):
            break
        shape = envelope(index / RATE) if envelope is not None else 1.0
        into[at] += level * shape * value


def _sweep(start: float, end: float, over: float):
    """A pitch going from ``start`` to ``end`` hertz over ``over`` seconds,
    evenly in pitch rather than in hertz, and holding there."""
    ratio = end / start

    def at(t: float) -> float:
        return start * ratio ** min(1.0, t / over)
    return at


def _finish(left: List[float], right: List[float],
            peak: float = 0.7) -> array:
    """To 16-bit stereo, left and right interleaved, the loudest point of
    either at ``peak`` of full scale, with a few milliseconds of fade at
    the end so nothing clicks and nothing left over from a filter's
    settling as an offset."""
    count = min(len(left), len(right))
    mean_left = sum(left[:count]) / max(1, count)
    mean_right = sum(right[:count]) / max(1, count)
    left = [value - mean_left for value in left[:count]]
    right = [value - mean_right for value in right[:count]]
    top = max(max((abs(v) for v in left), default=0.0),
              max((abs(v) for v in right), default=0.0)) or 1.0
    scale = peak / top
    fade = int(0.006 * RATE)
    out = array("h")
    for index in range(count):
        tail = min(1.0, (count - index) / max(1, fade))
        for value in (left[index], right[index]):
            out.append(int(max(-1.0, min(1.0, value * scale * tail))
                           * 32767))
    return out


def mono(samples: Sequence[int]) -> List[float]:
    """A stereo sound's two sides averaged, for measuring it."""
    return [(samples[index] + samples[index + 1]) / 2.0
            for index in range(0, len(samples) - 1, CHANNELS)]


def hertz(midi: float) -> float:
    return 440.0 * 2.0 ** ((midi - 69.0) / 12.0)


def _cents(freq: float, cents: float) -> float:
    return freq * 2.0 ** (cents / 1200.0)


def pluck(midi: float) -> array:
    """A pickup: the pluck a trance lead is made of.

    Two sawtooths seven cents either side of the note, one in each ear,
    through a resonant low-pass that opens wide on the strike and closes
    to just above the note as it rings - the "zing" is the filter's
    resonance sweeping down through the harmonics. A sine an octave under
    it for weight, and a glint of glass two octaves up on the attack."""
    freq = hertz(midi)
    length = 0.5
    left, right = _silence(length)
    count = len(left)
    table = _saw(_cents(freq, 8.0))

    def opening(t: float) -> float:
        return freq * (1.4 + 4.5 * math.exp(-t / 0.05))

    def ring(t: float) -> float:
        return (min(1.0, t / 0.004)
                * (0.75 * math.exp(-t / 0.12) + 0.25 * math.exp(-t / 0.34)))

    _add(left, _filter(_play(table, _cents(freq, -7.0), count), opening,
                       0.9), 1.0, ring)
    _add(right, _filter(_play(table, _cents(freq, 7.0), count, 0.37),
                        opening, 0.9), 1.0, ring)
    under = _play(_table([(1, 1.0)]), freq / 2.0, count)
    glint = _play(_table([(1, 1.0)]), freq * 4.0, int(0.08 * RATE))
    for side in (left, right):
        _add(side, under, 0.3,
             lambda t: min(1.0, t / 0.003) * math.exp(-t / 0.11))
        _add(side, glint, 0.06, lambda t: math.exp(-t / 0.018))
    return _finish(left, right, 0.52)


def soft(midi: float) -> array:
    """A coin: a soft synth hit. Two saws a few cents apart, one in each
    ear, through a low-pass that hardly opens, a sine at the note for its
    body, and an attack slow enough to have no edge - a pad struck once,
    which sits inside the music rather than on top of it."""
    freq = hertz(midi)
    length = 0.5
    left, right = _silence(length)
    count = len(left)
    table = _saw(_cents(freq, 4.0))

    def opening(t: float) -> float:
        return freq * (2.0 + 2.2 * math.exp(-t / 0.07))

    def ring(t: float) -> float:
        rise = 0.5 - 0.5 * math.cos(math.pi * min(1.0, t / 0.012))
        return rise * (0.65 * math.exp(-t / 0.13) + 0.35 * math.exp(-t / 0.4))

    _add(left, _filter(_play(table, _cents(freq, -4.0), count), opening,
                       0.55), 1.0, ring)
    _add(right, _filter(_play(table, _cents(freq, 4.0), count, 0.31),
                        opening, 0.55), 1.0, ring)
    body = _play(_table([(1, 1.0)]), freq, count)
    for side in (left, right):
        _add(side, body, 0.5, ring)
    return _finish(left, right, 0.5)


def tick(step: int) -> array:
    """A pickup with no note in it, for a record with no key to be in: a
    burst of digital chatter, three grains of held noise flicking from
    one ear to the other, brighter as the run climbs where a note would
    have gone up."""
    rng = random.Random(100 + step)
    left, right = _silence(0.1)
    bright = 1600.0 + 650.0 * min(7, step)
    for grain in range(3):
        burst = _filter(_noise(int(0.012 * RATE), rng, hold=3), bright,
                        0.9, "high")
        side = left if grain % 2 == 0 else right
        other = right if grain % 2 == 0 else left
        start = grain * 0.011
        _add(side, burst, 1.0, lambda t: math.exp(-t / 0.004), start)
        _add(other, burst, 0.35, lambda t: math.exp(-t / 0.004), start)
    return _finish(left, right, 0.42)


def shake(step: int) -> array:
    """A coin with no note in it: a metallic shimmer, brighter along a row
    - high noise ringing in a comb a few tenths of a millisecond long,
    whose resonances are all far above any melody."""
    rng = random.Random(200 + step)
    left, right = _silence(0.09)
    bright = 5200.0 + 500.0 * min(7, step)
    for side, late in ((left, 0.0), (right, 0.004)):
        raw = _filter(_noise(int(0.07 * RATE), rng), bright, 0.8, "high")
        ringing = list(raw)
        gap = int(0.00028 * RATE)
        for index in range(gap, len(ringing)):
            ringing[index] += 0.55 * ringing[index - gap]
        _add(side, ringing, 1.0, lambda t: min(1.0, t / 0.0005)
             * math.exp(-t / 0.022), late)
    return _finish(left, right, 0.36)


def hit() -> array:
    """A hit: an impact rather than a note. A sub-bass drop under the
    music, felt more than heard; a crunch of bit-crushed noise, different
    in each ear; and a zap tearing down through the whole range and out
    of it, so there is no pitch anywhere in it for long enough to clash."""
    rng = random.Random(7)
    left, right = _silence(0.55)
    count = len(left)
    drop = _play(_table([(1, 1.0)]), _sweep(115.0, 36.0, 0.3), count)
    zap = _filter(_play(_saw(2400.0), _sweep(2400.0, 90.0, 0.17),
                        int(0.2 * RATE)), 5000.0, 0.8)
    for side, seed, late in ((left, 1, 0.0), (right, 2, 0.009)):
        _add(side, drop, 1.0, lambda t: min(1.0, t / 0.002)
             * math.exp(-t / 0.15))
        crunch = _filter(_noise(int(0.2 * RATE), random.Random(seed),
                                hold=5), 900.0, 0.7, "high")
        _add(side, crunch, 0.6, lambda t: math.exp(-t / 0.045))
        _add(side, zap, 0.3, lambda t: math.exp(-t / 0.06), late)
    _add(left, _noise(int(0.3 * RATE), rng), 0.12,
         lambda t: math.exp(-t / 0.08))
    return _finish(left, right, 0.85)


def glass() -> array:
    """The shield going: a power-down - a square wave falling away through
    the floor, its filter closing behind it - under a scatter of glass,
    grains of high noise thrown about the stereo field. The chimes in it
    are the chord's own notes, played on top - see SoundBoard."""
    rng = random.Random(11)
    left, right = _silence(0.5)
    count = len(left)
    fall = _sweep(1100.0, 70.0, 0.42)
    down = _filter(_play(_square(1100.0), fall, count),
                   lambda t: fall(t) * 3.0, 1.2)
    for side in (left, right):
        _add(side, down, 0.4, lambda t: min(1.0, t / 0.004)
             * max(0.0, 1.0 - t / 0.47) ** 1.5)
    for grain in range(9):
        side = left if rng.random() < 0.5 else right
        other = right if side is left else left
        burst = _filter(_noise(int(0.03 * RATE), rng), 4200.0, 1.0, "high")
        start = rng.uniform(0.0, 0.2)
        level = 0.9 * math.exp(-start / 0.12)
        _add(side, burst, level, lambda t: math.exp(-t / 0.008), start)
        _add(other, burst, level * 0.3, lambda t: math.exp(-t / 0.008), start)
    return _finish(left, right, 0.55)


def sweep() -> array:
    """A power block: a riser. Noise through a resonant band climbing from
    the low mids to the top of the range, and a saw climbing under it,
    both swelling in - the build before a drop in a third of a second.
    The notes that land at the top of it are the chord's."""
    left, right = _silence(0.45)
    count = int(0.42 * RATE)
    rise = _sweep(350.0, 8000.0, 0.42)
    climb = _sweep(110.0, 880.0, 0.42)
    body = _filter(_play(_saw(880.0), climb, count), 2600.0, 0.9)

    def swell(t: float) -> float:
        return math.sin(math.pi * min(1.0, t / 0.42)) ** 1.2

    for side, seed in ((left, 5), (right, 6)):
        air = _filter(_noise(count, random.Random(seed)), rise, 2.4, "band")
        _add(side, air, 1.0, swell)
        _add(side, body, 0.22, swell)
    return _finish(left, right, 0.45)


def landing() -> array:
    """Back on the road after a jump: a soft sub touchdown and a rush of
    air braking - noise whose filter falls from bright to dark."""
    left, right = _silence(0.32)
    count = len(left)
    thump = _play(_table([(1, 1.0)]), _sweep(82.0, 42.0, 0.2), count)
    brake = _sweep(4200.0, 260.0, 0.25)
    for side, seed in ((left, 3), (right, 4)):
        _add(side, thump, 1.0, lambda t: min(1.0, t / 0.002)
             * math.exp(-t / 0.09))
        air = _filter(_noise(count, random.Random(seed)), brake, 0.8)
        _add(side, air, 0.55, lambda t: math.exp(-t / 0.085))
    return _finish(left, right, 0.55)


def swell() -> array:
    """The end of a track with no key: a long riser opening out into a
    shimmer of high air."""
    left, right = _silence(1.6)
    count = len(left)
    rise = _sweep(260.0, 9000.0, 1.0)

    def opening(t: float) -> float:
        return min(1.0, t / 0.6) * math.exp(-max(0.0, t - 0.7) / 0.35)

    for side, seed in ((left, 9), (right, 10)):
        air = _filter(_noise(count, random.Random(seed)), rise, 1.6, "band")
        _add(side, air, 1.0, opening)
        shimmer = _filter(_noise(count, random.Random(seed + 20)), 6500.0,
                          0.8, "high")
        _add(side, shimmer, 0.35, lambda t: min(1.0, t / 0.9)
             * math.exp(-max(0.0, t - 0.9) / 0.25))
    return _finish(left, right, 0.55)


#: Everything with a fixed name, by name.
FIXED: Dict[str, Callable[[], array]] = {
    "hit": hit, "glass": glass, "sweep": sweep, "landing": landing,
    "swell": swell,
}
for _step in range(8):
    FIXED[f"tick{_step}"] = (lambda step=_step: tick(step))
    FIXED[f"shake{_step}"] = (lambda step=_step: shake(step))

_MAKERS = {"pluck": pluck, "soft": soft}


def note_name(voice: str, midi: int, cents: int) -> str:
    """The cache's name for one note of a voice in one tuning."""
    return f"{voice}{midi}{'+' if cents >= 0 else '-'}{abs(cents):02d}"


def notes_for(cents: int) -> List[str]:
    """Every note there is to make for a tuning."""
    return ([note_name("pluck", midi, cents)
             for midi in range(PRIZE_LOW, PRIZE_HIGH + 1)]
            + [note_name("soft", midi, cents)
               for midi in range(COIN_LOW, COIN_HIGH + 1)])


def voice_of(name: str) -> Optional[str]:
    """The voice a note's name is in, or None for a fixed sound."""
    for voice in VOICES:
        if name.startswith(voice) and name not in FIXED:
            return voice
    return None


def make(name: str) -> array:
    """The samples for a sound, by its name - fixed, or a note: stereo,
    left and right interleaved."""
    maker = FIXED.get(name)
    if maker is not None:
        return maker()
    voice = voice_of(name)
    if voice is None:
        raise KeyError(name)
    rest = name[len(voice):]
    sign = 1 if "+" in rest else -1
    midi, cents = rest.replace("-", "+").split("+")
    return _MAKERS[voice](int(midi) + sign * int(cents) / 100.0)


def path_for(name: str, folder: Path) -> Path:
    return Path(folder) / f"rider-{VERSION}-{name}.wav"


def write(name: str, folder: Path) -> Path:
    """The named sound as a WAV in ``folder``, made if it is not there."""
    path = path_for(name, folder)
    if path.exists() and path.stat().st_size > 44:
        return path
    samples = make(name)
    Path(folder).mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    with wave.open(str(partial), "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(samples.tobytes())
    partial.replace(path)
    return path


def make_all(folder, names: Optional[Sequence[str]] = None) -> None:
    """The sounds, written into ``folder``: the fixed ones, or ``names``.
    What a worker process runs."""
    for name in (names if names is not None else list(FIXED)):
        write(name, Path(folder))


def prune(folder, keep_cents: Iterable[int] = ()) -> None:
    """Everything an older build wrote, and all but the newest few
    tunings' notes."""
    folder = Path(folder)
    if not folder.is_dir():
        return
    tunings: Dict[str, float] = {}
    for path in folder.glob("rider-*.wav"):
        parts = path.stem.split("-", 2)
        if len(parts) < 3 or parts[1] != str(VERSION):
            try:
                path.unlink()
            except OSError:
                pass
            continue
        name = parts[2]
        if voice_of(name) is not None:
            tag = name[-3:]
            tunings[tag] = max(tunings.get(tag, 0.0), path.stat().st_mtime)
    kept = {f"{'+' if c >= 0 else '-'}{abs(c):02d}" for c in keep_cents}
    ordered = sorted(tunings, key=lambda tag: tunings[tag], reverse=True)
    for tag in ordered[TUNINGS_KEPT:]:
        if tag in kept:
            continue
        for voice in VOICES:
            for path in folder.glob(f"rider-{VERSION}-{voice}*{tag}.wav"):
                path.unlink(missing_ok=True)


def make_elsewhere(folder, names: Optional[Sequence[str]] = None) -> None:
    """Make sounds in a process of their own, if they are not made.

    A few hundred milliseconds of arithmetic a sound - on the thread
    drawing the picture it would be the moment the scene appeared, which
    is the worst moment for it. Nothing is made on that thread: a sound
    not made yet is simply not played until it is.
    """
    folder = Path(folder)
    wanted = list(names if names is not None else FIXED)
    if all(path_for(name, folder).exists() for name in wanted):
        return
    try:
        import multiprocessing

        context = multiprocessing.get_context("spawn")
        context.Process(target=make_all, args=(str(folder), wanted),
                        name="mail-manager-sounds", daemon=True).start()
    except Exception as exc:      # noqa: BLE001 - played when made instead
        log.info("Making the rider's sounds when they are wanted (%s).", exc)


# ==========================================================================
# Which notes
# ==========================================================================
def climb(step: int, ladder: int) -> int:
    """Which rung of a ladder of ``ladder`` notes the ``step``th pickup
    of a run is.

    Up one a pickup, and then round the top of it rather than back to the
    bottom: a run of forty should go on sounding like a run, not start
    again as if it had been broken.
    """
    if ladder <= 0:
        return 0
    if step < ladder:
        return max(0, step)
    top = max(1, ladder // 2)
    return ladder - top + (step - ladder) % top


def ladder(classes: Sequence[int], low: int, high: int) -> List[int]:
    """Every MIDI note from ``low`` to ``high`` whose pitch class is one
    of ``classes``, lowest first."""
    wanted = {pc % 12 for pc in classes}
    return [midi for midi in range(low, high + 1) if midi % 12 in wanted]


def cents_of(harmony: Optional[dict]) -> int:
    """The record's tuning, in cents, to the nearest TUNING_STEP."""
    if not harmony:
        return 0
    cents = float(harmony.get("tuning") or 0.0) * 100.0
    return int(round(cents / TUNING_STEP) * TUNING_STEP)


def pitched(harmony: Optional[dict]) -> bool:
    """Whether a record has a key worth playing in."""
    if not harmony:
        return False
    if harmony.get("keys"):
        return True
    key = harmony.get("key") or {}
    return float(key.get("confidence", 0.0)) >= SURE


def classes_at(harmony: dict, when: float) -> Tuple[int, ...]:
    """The pitch classes a pickup may be at ``when``: the chord sounding
    then if it is one of the key's own, and otherwise the key's
    pentatonic - the key there, which a song that changes key has moved."""
    import harmony as _harmony

    tonic, mode = _harmony.key_at(harmony, when)
    chord = _harmony.chord_at(harmony, when)
    if chord is not None and _harmony.in_key(chord, tonic, mode):
        root, quality = chord
        return tuple((root + step) % 12 for step in CHORD_TONES[quality])
    scale = MINOR_PENTATONIC if mode == "minor" else MAJOR_PENTATONIC
    return tuple((tonic + step) % 12 for step in scale)


# ==========================================================================
# Playing them
# ==========================================================================
class SoundBoard:
    """The game's sounds, answering what the game did, in the record's key.

    Reads the scene's own record of what happened - the same pops the
    picture answers - so a sound and a flash can never disagree about
    whether something was taken. ``volume`` says how loud the player is
    set, and ``level`` how loud the effects are against the music - a
    share of the player's volume, so turning the music down turns them
    down with it and the balance somebody chose stays chosen; ``duck`` is
    told to pull the music down for a moment on a hit; ``later`` puts off
    playing a note by some seconds, for the notes of an arpeggio and the
    echoes.
    """

    #: How loud the effects are against the music, unless somebody says.
    LEVEL = 0.5
    #: How many of each sound can be going at once. A row of coins is
    #: three in a third of a second, each echoing twice, and restarting
    #: one sound cuts the last one off.
    VOICES = 4
    #: Every note a pickup or a coin plays comes back, quieter, on the
    #: record's own grid: a dotted eighth later and again a dotted quarter
    #: later - the delay a trance lead is run through - as (beats, how
    #: loud). Timed from the drums' own beat, so a run of pickups rings on
    #: in time with the music rather than over it.
    ECHOES = ((0.75, 0.3), (1.5, 0.1))
    #: A beat outside this, in seconds, is not a tempo to echo in: under
    #: 50 beats a minute or over 240.
    ECHO_BEATS = (0.25, 1.2)
    #: The notes of an arpeggio, and of the finish, this far apart.
    SPREAD = 0.055

    def __init__(self, folder: Path, volume: Callable[[], float] = lambda: 0.7,
                 duck: Optional[Callable[[float, float], None]] = None,
                 later: Optional[Callable[[float, Callable[[], None]], None]]
                 = None, level: Optional[Callable[[], float]] = None) -> None:
        self.folder = Path(folder)
        self._volume = volume
        self._level = level if level is not None else (lambda: self.LEVEL)
        self._duck = duck
        self._later = later
        self._voices: Dict[str, list] = {}
        self._next: Dict[str, int] = {}
        self._seen: set = set()
        self.enabled = True
        self._broken = False
        self._harmony: Optional[dict] = None
        self._cents = 0

    # -- the record ---------------------------------------------------------
    def set_harmony(self, harmony: Optional[dict]) -> None:
        """The key and chords to play in; None for a new, unheard track.

        The notes for its tuning are made now, elsewhere, if they have not
        been - by the time anybody is riding, they usually are."""
        self._harmony = harmony
        self._cents = cents_of(harmony)
        if pitched(harmony):
            names = notes_for(self._cents)
            make_elsewhere(self.folder, names)
            try:
                prune(self.folder, (self._cents,))
            except OSError:
                pass

    @property
    def in_key(self) -> bool:
        return pitched(self._harmony)

    # -- playing ------------------------------------------------------------
    def _voice(self, name: str):
        """A loaded sound effect for ``name``, or None if the file is not
        made yet - it is never made here, on the thread drawing."""
        voices = self._voices.get(name)
        if voices is None:
            path = path_for(name, self.folder)
            if not path.exists():
                return None
            from PySide6.QtCore import QUrl
            from PySide6.QtMultimedia import QSoundEffect

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

    def play(self, name: str, loud: float = 1.0) -> bool:
        """Play ``name`` now; whether there was anything to play."""
        if not self.enabled or self._broken:
            return False
        try:
            effect = self._voice(name)
            if effect is None:
                return False
            effect.setVolume(max(0.0, min(1.0, self._volume()
                                          * self._level() * loud)))
            effect.play()
            return True
        except Exception as exc:      # noqa: BLE001 - a game without sound
            self._broken = True
            log.info("The rider's sounds are off: %s", exc)
            return False

    def _play_at(self, delay: float, name: str, loud: float) -> None:
        if delay <= 0.0 or self._later is None:
            self.play(name, loud)
            return
        try:
            self._later(delay, lambda: self.play(name, loud))
        except Exception as exc:      # noqa: BLE001 - late rather than never
            # Played now rather than not at all, and the rest of the game's
            # sounds kept: a failure here used to take the whole listener
            # with it, and the game was silent from then on.
            if not getattr(self, "_late_failed", False):
                self._late_failed = True
                log.warning("The rider's timed sounds are played at once "
                            "(%s).", exc)
            self.play(name, loud)

    def prepare(self) -> None:
        """Load every sound already made, before any is wanted: Qt loads a
        sound effect in the background, and the first pickup should not be
        the one that goes unheard while it does."""
        if self._broken:
            return
        names = list(FIXED)
        if self.in_key:
            names += notes_for(self._cents)
        for name in names:
            if name in self._voices:
                continue
            try:
                self._voice(name)
            except Exception as exc:      # noqa: BLE001
                self._broken = True
                log.info("The rider's sounds are off: %s", exc)
                return

    def warm(self) -> None:
        """Make the fixed sounds now, before anything is played."""
        for name in FIXED:
            try:
                write(name, self.folder)
            except OSError as exc:
                log.info("Could not make the rider's sounds: %s", exc)
                self._broken = True
                return

    # -- what to play -------------------------------------------------------
    def _note(self, voice: str, midi: int) -> str:
        return note_name(voice, midi, self._cents)

    def _ladder(self, scene, low: int, high: int) -> List[int]:
        return ladder(classes_at(self._harmony, float(
            getattr(scene, "_heard", 0.0))), low, high)

    def answer(self, scene, kind: str) -> List[Tuple[float, str, float]]:
        """What ``kind`` sounds like now, as (after, name, how loud)."""
        keyed = self.in_key
        if kind == "prize":
            if getattr(scene, "_mode", "") == "Puzzle":
                step = sum(len(pile) for pile in getattr(scene, "_cells", ()))
            else:
                step = int(getattr(scene, "_chain", 1)) - 1
            if keyed:
                notes = self._ladder(scene, PRIZE_LOW, PRIZE_HIGH)
                if notes:
                    return [(0.0, self._note(
                        "pluck", notes[climb(step, len(notes))]), 1.0)]
            return [(0.0, f"tick{min(7, max(0, step))}", 1.0)]
        if kind == "coin":
            run = max(0, int(getattr(scene, "_coin_run", 1)) - 1)
            if keyed:
                notes = self._ladder(scene, COIN_LOW, COIN_HIGH)
                if notes:
                    return [(0.0, self._note(
                        "soft", notes[climb(run, len(notes))]), 1.0)]
            return [(0.0, f"shake{min(7, run)}", 1.0)]
        if kind == "hit":
            return [(0.0, "hit", 1.0)]
        if kind == "air":
            return [(0.0, "landing", 1.0)]
        if kind == "shatter":
            out = [(0.0, "glass", 1.0)]
            if keyed:
                notes = self._ladder(scene, COIN_LOW, COIN_HIGH)[-6:]
                for index, midi in enumerate(notes[::2]):
                    out.append((0.015 + index * 0.03,
                                self._note("soft", midi), 0.6))
            return out
        if kind == "power":
            out = [(0.0, "sweep", 1.0)]
            if keyed:
                notes = self._ladder(scene, PRIZE_LOW + 12, PRIZE_HIGH)
                for midi in notes[:2]:
                    out.append((0.34, self._note("pluck", midi), 0.8))
            return out
        if kind in ("milestone", "clear"):
            if not keyed:
                return [(index * self.SPREAD, f"tick{index + 3}", 0.9)
                        for index in range(4)]
            notes = self._ladder(scene, PRIZE_LOW, PRIZE_HIGH)[:5]
            if kind == "clear":
                return [(0.0, self._note("pluck", midi), 0.7)
                        for midi in notes[:3]]
            return [(index * self.SPREAD, self._note("pluck", midi), 0.8)
                    for index, midi in enumerate(notes)]
        if kind == "finish":
            if not keyed:
                return [(0.0, "swell", 1.0)]
            # Home: up the chord of the key the track ends in, and that
            # chord to finish on.
            import harmony as _harmony

            tonic, mode = _harmony.key_at(
                self._harmony, float(getattr(scene, "_heard", 0.0)))
            tones = CHORD_TONES["min" if mode == "minor" else "maj"]
            home = ladder([(tonic + step) % 12 for step in tones],
                          PRIZE_LOW, PRIZE_HIGH)
            run = home[:7]
            out = [(index * 0.07, self._note("pluck", midi), 0.7)
                   for index, midi in enumerate(run)]
            last = 0.07 * len(run) + 0.05
            out += [(last, self._note("pluck", midi), 0.6) for midi in home[:3]]
            return out
        return []

    #: How many notes are loaded a frame once they have been made, and how
    #: many frames apart. A handful at a time, so loading the lot is never
    #: a frame nobody drew.
    LOAD_EACH = 3
    LOAD_EVERY = 10

    def tend(self) -> int:
        """Load a few of the notes that have been made since last time.
        Qt loads a sound in the background, so a note first loaded when it
        is wanted is a pickup that goes unheard. Returns how many."""
        if self._broken or not self.in_key:
            return 0
        loaded = 0
        for name in notes_for(self._cents):
            if name in self._voices:
                continue
            if not path_for(name, self.folder).exists():
                continue
            try:
                self._voice(name)
            except Exception as exc:      # noqa: BLE001
                self._broken = True
                log.info("The rider's sounds are off: %s", exc)
                return loaded
            loaded += 1
            if loaded >= self.LOAD_EACH:
                break
        return loaded

    def _echoes(self, scene) -> List[Tuple[float, float]]:
        """When a note taken now comes back, and how loud: (seconds after,
        share). Nothing without a way to play later, or a tempo to be in
        time with."""
        beat = float(getattr(scene, "_beat", 0.0) or 0.0)
        low, high = self.ECHO_BEATS
        if self._later is None or not low <= beat <= high:
            return []
        return [(beats * beat, share) for beats, share in self.ECHOES]

    def preview(self, scene=None) -> Optional[str]:
        """A pickup, at the level the effects are set to now - for somebody
        moving the effects' slider to hear where it is. In the record's
        key if there is one. Returns what was played."""
        name = "tick3"
        if self.in_key:
            notes = ladder(classes_at(self._harmony, float(
                getattr(scene, "_heard", 0.0) or 0.0)), PRIZE_LOW, PRIZE_HIGH)
            if notes:
                name = self._note("pluck", notes[min(2, len(notes) - 1)])
        return name if self.play(name) else None

    def listen(self, scene) -> List[str]:
        """What the scene did since last asked, played. Returns the names
        of what was played, for whoever wants to know."""
        self._frames = getattr(self, "_frames", 0) + 1
        if self._frames % self.LOAD_EVERY == 0:
            self.tend()
        pops = list(getattr(scene, "_pops", ()) or ())
        alive = {id(pop) for pop in pops}
        self._seen &= alive
        played = []
        for pop in pops:
            if id(pop) in self._seen:
                continue
            self._seen.add(id(pop))
            loud = min(1.4, float(pop[2]))
            echoes = (self._echoes(scene) if pop[0] in ("prize", "coin")
                      else [])
            for after, name, share in self.answer(scene, pop[0]):
                self._play_at(after, name, loud * share)
                played.append(name)
                if voice_of(name) is not None:
                    for later, quieter in echoes:
                        self._play_at(after + later, name,
                                      loud * share * quieter)
            if pop[0] == "hit" and self._duck is not None:
                # The music goes under for a moment: felt more than heard.
                self._duck(0.45, 0.28)
        return played
