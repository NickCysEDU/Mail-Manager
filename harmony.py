"""The harmony of a track: its key, its tuning, its chords and its lead.

Worked out once, before anything is played, in a process of its own (see
attachment_audio._worker), from the same samples the rest of the analysis
reads. Pure Python and the standard library, like the rest of it.

What it is for. Music rider plays a note of its own for every block taken,
and those notes were a pentatonic scale on a fixed root: against a record
in another key they were wrong notes over somebody's melody, which is the
one thing a sound in a music game must never be. Knowing the key puts them
in it, knowing the chord under the moment puts them in *that*, and knowing
the tuning puts them in tune with a record that is not at A = 440. Where
the harmony cannot be told - a drum track, a noise - the answer says so,
and the sounds stop being notes at all.

The lead is for the road: a melody going up can take the blocks across
the lanes with it.

How it is found:

**One mono track at 8 kHz.** Everything that decides a pitch class sits
below two kilohertz, and a quarter of a second of 8 kHz audio is 2048
samples rather than 12,000. The channels are summed and every six samples
averaged, both with ``map`` over slices so the work is done in C rather
than one sample at a time in Python.

**Peaks, not bins.** A 4096-point transform every quarter second, and only
the local maxima of each spectrum counted, each placed between its bins by
a parabola through the log magnitudes. A hi-hat is loud everywhere and a
peak nowhere, so the noise a drum track is made of barely registers.

**The tuning first.** Every peak's distance from the nearest equal-tempered
semitone, averaged round the circle and weighted by how strong the peak
was. A record mastered a quarter-tone flat has every peak a quarter-tone
off, and read against A = 440 it would put half its notes in the wrong
pitch class.

**The key** is the whole track's chroma held against the twelve rotations
of a major and a minor key profile - how much each degree of a scale is
heard - and the best fit wins. How far it wins by is the confidence. The
profiles are fitted on electronic dance music (see MAJOR); the listeners'
profiles they replaced are kept beside them. A key and its relative - A
minor and C major - fit a chroma nearly equally, because they are the same
seven notes, and when they are that close the chords decide: whichever
key's own chord is heard for longer, and if that is even, whichever the
track begins on.

**The chords** are each quarter second's chroma held against the major and
minor triads, with the bass boosting a chord whose root it is playing, a
little favour for chords that belong to the key, and a Viterbi pass that
makes changing chord cost something, so the answer is a sequence of chords
rather than a flicker between two.
"""

from __future__ import annotations

import cmath
import math
from array import array
from operator import add
from typing import List, Optional, Sequence

#: The rate everything here is measured at, and how it is reached.
RATE = 8000

#: The transform, and how far it moves between one and the next. 4096
#: points at 8 kHz is half a second and 1.95 Hz a bin, which separates two
#: semitones down to 70 Hz; a quarter second between them is two readings
#: a beat at 128 bpm.
WINDOW = 4096
HOP = 2048

#: The range read for pitch. Below 55 Hz a semitone is closer than a bin;
#: above two kilohertz is mostly the harmonics of what is below.
LOWEST = 55.0
HIGHEST = 2000.0

#: The bass, for a chord's root: the part of the range a bass line plays in.
BASS_TOP = 220.0

#: The lead, for the road: where a melody sits above the bass and the pads.
LEAD_LOW = 260.0
LEAD_HIGH = 1800.0

#: A peak further than this from any semitone, once the tuning is known,
#: is not a note - it is noise, or a bell's inharmonic partial.
IN_TUNE = 0.35

#: The most peaks counted in one reading, strongest first.
PEAKS = 24

#: Refuse to work on more than this, as the rest of the analysis does.
MAX_SECONDS = 900

#: How much each degree of a major and a minor scale is heard, from the
#: tonic up: the chroma this module measures, summed over 88 electronic
#: dance records rotated to the key a DJ program reads for each, and
#: scaled so the largest is 1.
#:
#: Fitted rather than taken from listening tests because dance music leans
#: on its bass and its fifth in a way the tests' material did not. Against
#: the DJ program's keys, left out one record at a time so no record is
#: judged by profiles fitted on itself:
#:
#:     these profiles          80% the same key   (weighted 0.87)
#:     Temperley (2005)        65%                (0.74)
#:     Krumhansl-Kessler       49%                (0.64)
#:
#: Weighted as the MIREX key task weighs: a fifth away is half right, the
#: relative key three tenths, the parallel two.
MAJOR = (1.000, 0.282, 0.638, 0.252, 0.749, 0.418, 0.264, 0.844, 0.256,
         0.553, 0.238, 0.478)
MINOR = (0.992, 0.329, 0.611, 0.617, 0.420, 0.588, 0.348, 1.000, 0.413,
         0.363, 0.582, 0.358)

#: Krumhansl and Kessler's profiles (1982), from listeners judging how well
#: each degree fits a key: the reference the fitted ones are measured
#: against, and what ``analyse`` can be handed instead.
KK_MAJOR = (6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66,
            2.29, 2.88)
KK_MINOR = (6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69,
            3.34, 3.17)

#: How close a key's relative has to fit for the chords to decide between
#: them.
RELATIVE_CLOSE = 0.08

NAMES = ("C", "C#", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B")

#: The chords tried, as intervals above the root, and how much each note
#: of the chord is worth against the others.
TRIADS = {"maj": ((0, 1.0), (4, 0.8), (7, 0.9)),
          "min": ((0, 1.0), (3, 0.8), (7, 0.9))}

#: What changing chord costs in the Viterbi pass, against the similarity a
#: chord has to a reading, which runs 0 to 1. At a tenth a chord has to
#: fit a clearly better for half a second before the answer moves to it.
CHANGE = 0.10
#: How many readings either side a chord is heard over, as well as its own:
#: an arpeggio sounds its chord one note at a time, and a reading a quarter
#: of a second long hears two of its three notes - C and E, which is C or
#: A minor, then E and G, which is C or E minor. Over a second and three
#: quarters - a bar at 128 - the whole chord is there. On a bare arpeggio
#: of four chords played twice, a note every half beat, 50 chords were
#: heard for 8 and the right one a third of the time; now 10, and 73 per
#: cent. A pad or a bass under it was right either way, 96 to 98 per cent.
CONTEXT = 3

#: How much a bass note on a chord's root is worth, and how much a chord
#: that belongs to the key is favoured over one that does not.
ROOT_BASS = 0.35
IN_KEY = 0.04

#: A reading with less than this share of the track's typical tonal
#: strength is "no chord": the drums alone, or silence.
NO_CHORD = 0.35

#: Below this confidence the key is not believed. A drum track and a noise
#: fit every key about equally; a song with a key fits one clearly.
SURE = 0.10


def _twiddles(n: int) -> List[complex]:
    return [cmath.exp(-2j * math.pi * k / n) for k in range(n // 2)]


_TWIDDLE = _twiddles(WINDOW)
_HANN = [0.5 - 0.5 * math.cos(2 * math.pi * i / (WINDOW - 1))
         for i in range(WINDOW)]


def _fft(values: List[complex]) -> List[complex]:
    """Iterative radix-2, in place. The same as attachment_audio's, for a
    transform twice the length."""
    n = len(values)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            values[i], values[j] = values[j], values[i]
    length = 2
    while length <= n:
        step = n // length
        half = length // 2
        for start in range(0, n, length):
            k = 0
            for offset in range(start, start + half):
                partner = offset + half
                temp = values[partner] * _TWIDDLE[k]
                values[partner] = values[offset] - temp
                values[offset] = values[offset] + temp
                k += step
        length <<= 1
    return values


def mono_at(samples, rate: int, channels: int) -> tuple:
    """The track as one channel at about RATE: (samples, their rate).

    Averaged in blocks of ``rate // RATE`` rather than filtered properly,
    which lets some of what is above the new half-rate fold back down -
    a hi-hat at 7 kHz lands near 1 kHz, sixteen decibels down. That is
    noise rather than a note, and the peak picking below is what makes
    noise not count. What it buys is speed: the whole of it is ``map`` over
    slices, so a seven minute track takes about a second.
    """
    channels = max(1, channels)
    factor = max(1, int(rate // RATE))
    scale = 1.0 / (32768.0 * factor * min(2, channels))
    result = array("f")
    # A second at a time: as one list of Python numbers, a seven minute
    # stereo track was twenty million objects and most of a gigabyte.
    step = channels * factor * max(1, int(rate) // factor)
    usable = len(samples) - len(samples) % (channels * factor)
    for start in range(0, usable, step):
        piece = samples[start:min(usable, start + step)]
        if channels > 1:
            mono = list(map(add, piece[0::channels], piece[1::channels]))
        else:
            mono = list(piece)
        out = mono[0::factor]
        for phase in range(1, factor):
            out = list(map(add, out, mono[phase::factor]))
        result.extend(map(scale.__mul__, out))
    return result, rate / factor


def _peaks(spectrum, rate: float) -> list:
    """The strongest local maxima between LOWEST and HIGHEST, as (the
    pitch as a MIDI number, how strong on a log scale, its power)."""
    per_bin = rate / WINDOW
    low = max(2, int(LOWEST / per_bin))
    high = min(len(spectrum) - 2, int(HIGHEST / per_bin) + 1)
    power = [v.real * v.real + v.imag * v.imag for v in spectrum[low - 1:high + 2]]
    found = []
    for index in range(1, len(power) - 1):
        here = power[index]
        if here <= power[index - 1] or here < power[index + 1] or here <= 0.0:
            continue
        # Between the bins, by a parabola through the log magnitudes.
        a = math.log(power[index - 1] + 1e-20)
        b = math.log(here + 1e-20)
        c = math.log(power[index + 1] + 1e-20)
        bend = a - 2.0 * b + c
        shift = 0.5 * (a - c) / bend if bend < 0.0 else 0.0
        freq = (low - 1 + index + shift) * per_bin
        if freq <= 0.0:
            continue
        found.append((here, freq))
    found.sort(reverse=True)
    top = found[:PEAKS]
    if not top:
        return []
    loudest = top[0][0]
    out = []
    for power_here, freq in top:
        # Forty decibels below the loudest peak of the reading is not
        # worth counting.
        if power_here < loudest * 1e-4:
            break
        midi = 69.0 + 12.0 * math.log2(freq / 440.0)
        # Strength on a log scale, so one loud bass note does not outvote
        # the chord above it - and the power as it was, for telling a
        # note's overtone from a note, which the log scale cannot.
        out.append((midi, math.log1p(power_here / (loudest * 1e-4)),
                    power_here))
    return out


def readings(mono, rate: float, should_stop=None, on_progress=None) -> list:
    """Every quarter second: its peaks, and how loud it was."""
    out = []
    total = len(mono)
    at = 0
    count = 0
    while at + WINDOW <= total:
        if should_stop is not None and count % 64 == 0 and should_stop():
            return []
        if on_progress is not None and count % 64 == 0:
            on_progress(min(0.99, at / max(1, total)))
        first = mono[at:at + WINDOW]
        second_at = at + HOP
        second = (mono[second_at:second_at + WINDOW]
                  if second_at + WINDOW <= total else None)
        if second is not None:
            packed = [complex(first[i] * _HANN[i], second[i] * _HANN[i])
                      for i in range(WINDOW)]
        else:
            packed = [complex(first[i] * _HANN[i], 0.0) for i in range(WINDOW)]
        spectrum = _fft(packed)
        spectra = []
        # Two real spectra out of one complex transform: see
        # attachment_audio._two_real_ffts.
        top = int(HIGHEST * WINDOW / rate) + 4
        a = [0j] * top
        b = [0j] * top
        for k in range(top):
            here = spectrum[k]
            there = spectrum[(WINDOW - k) % WINDOW].conjugate()
            a[k] = (here + there) * 0.5
            b[k] = (here - there) * -0.5j
        spectra.append(a)
        if second is not None:
            spectra.append(b)
        for spectrum_here, start in zip(spectra, (at, second_at)):
            # Loudness off one sample in eight, which is plenty for a level.
            sparse = mono[start:start + WINDOW:8]
            level = math.sqrt(sum(v * v for v in sparse) / max(1, len(sparse)))
            out.append((_peaks(spectrum_here, rate), level))
        at += HOP * len(spectra)
        count += 1
    return out


def tuning(frames: Sequence) -> float:
    """How far the record sits from A = 440, in semitones, -0.5 to 0.5.

    The circular mean of every peak's offset from its nearest semitone:
    round the circle, because an offset of 0.49 and one of -0.49 are the
    same tuning and their plain average is zero.
    """
    x = y = 0.0
    for peaks, _level in frames:
        for midi, weight, _power in peaks:
            angle = 2.0 * math.pi * (midi - round(midi))
            x += weight * math.cos(angle)
            y += weight * math.sin(angle)
    if x == 0.0 and y == 0.0:
        return 0.0
    return math.atan2(y, x) / (2.0 * math.pi)


#: How strong a peak has to be, against the strongest in the melody's
#: range, to be taken as the melody when it is the highest.
LEAD_SHARE = 0.6

#: How much more power a note needs than one an octave (or a twelfth, or
#: two octaves) above it for that one to be taken as its overtone.
OVERTONE = 2.0

#: A peak within this of the tuning's semitone grid is in tune. Notes held
#: by instruments land within a few cents of it; the partials of a drum and
#: the sweep of a kick land anywhere, so about three in ten of theirs fall
#: this close by chance.
TIGHT = 0.15


def tonality(frames: Sequence, tuned: float) -> float:
    """How much of a track has a pitch to it, 0 to 1.

    The share of its peaks' strength that sits in tune - within TIGHT of
    the tuning's semitones - scaled so that what chance gives is 0 and a
    record that is all notes is 1. Measured on written material: drums on
    their own 0.35, drums over a bass line 0.55 to 0.6, chords 0.98.

    Not how peaked the chroma is, which was the first measure: a drum kit
    has few peaks, so its chroma is as peaked as a chord's, and a kit on
    its own came out 0.71 tonal and got a key.
    """
    near = total = 0.0
    for peaks, _level in frames:
        for midi, weight, _power in peaks:
            place = midi - tuned
            total += weight
            if abs(place - round(place)) < TIGHT:
                near += weight
    if total <= 0.0:
        return 0.0
    return max(0.0, min(1.0, (near / total - CHANCE) / (1.0 - CHANCE - 0.1)))


#: The in-tune share chance gives, and so what counts as no pitch at all.
CHANCE = 0.38


def chroma(frames: Sequence, tuned: float) -> tuple:
    """Per reading: the twelve pitch classes, the bass's twelve, and the
    lead, or None.

    The lead is the *highest* strong note in the melody's range rather
    than the strongest: a melody is nearly always the top voice, and the
    strongest note in that range between two notes of it is the pad under
    it."""
    notes, basses, leads = [], [], []
    for peaks, _level in frames:
        row = [0.0] * 12
        low = [0.0] * 12
        candidates = []
        for midi, weight, power in peaks:
            place = midi - tuned
            nearest = round(place)
            if abs(place - nearest) > IN_TUNE:
                continue
            pc = int(nearest) % 12
            freq = 440.0 * 2.0 ** ((midi - 69.0) / 12.0)
            row[pc] += weight
            if freq < BASS_TOP:
                low[pc] += weight
            elif LEAD_LOW <= freq <= LEAD_HIGH:
                candidates.append((place, weight, power))
        lead = None
        if candidates:
            strongest = max(weight for _place, weight, _power in candidates)
            # Not a note's own overtones: the octave, the twelfth and the
            # two octaves above something with twice the power are that
            # something. Twice, in power rather than on the log scale: a
            # melody doubling a chord note an octave up is as loud as the
            # chord note or louder, and it is the melody.
            notes_here = sorted(candidates)
            real = []
            for place, weight, power in notes_here:
                overtone = any(
                    abs(place - other - interval) < 0.5
                    and other_power >= power * OVERTONE
                    for other, _w, other_power in notes_here if other < place
                    for interval in (12.0, 19.0, 24.0))
                if not overtone:
                    real.append((place, weight))
            lead = max((place for place, weight in real
                        if weight >= strongest * LEAD_SHARE), default=None)
        notes.append(row)
        basses.append(low)
        leads.append(lead)
    return notes, basses, leads


def _correlation(a: Sequence[float], b: Sequence[float]) -> float:
    n = len(a)
    ma = sum(a) / n
    mb = sum(b) / n
    top = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((y - mb) ** 2 for y in b))
    return top / (da * db) if da > 0.0 and db > 0.0 else 0.0


def key_of(total: Sequence[float], major=MAJOR, minor=MINOR) -> dict:
    """The key a chroma summed over a track is in, and how sure that is.

    Against every rotation of both profiles; the confidence is how far the
    best beats the best that is not it or its relative - a song in A minor
    fits C major nearly as well, and that is not doubt about the key.
    """
    scores = []
    for tonic in range(12):
        for mode, profile in (("major", major), ("minor", minor)):
            rotated = [profile[(pc - tonic) % 12] for pc in range(12)]
            scores.append((_correlation(total, rotated), tonic, mode))
    scores.sort(reverse=True)
    best, tonic, mode = scores[0]
    relative = ((tonic + 9) % 12, "minor") if mode == "major" else (
        (tonic + 3) % 12, "major")
    rival = next((score for score, t, m in scores[1:]
                  if (t, m) != relative), 0.0)
    relative_fit = next((score for score, t, m in scores
                         if (t, m) == relative), 0.0)
    return {"tonic": tonic, "mode": mode, "fit": best,
            "confidence": max(0.0, best - rival),
            "relative": list(relative), "relative_fit": relative_fit,
            "name": NAMES[tonic] + ("m" if mode == "minor" else "")}


def settle_relative(key: dict, found: Sequence) -> dict:
    """Between a key and its relative, when they fit about equally, the
    one whose own chord is heard for longer - or, if that is even too, the
    one the track begins on."""
    if not key or not found or key["fit"] - key["relative_fit"] > RELATIVE_CLOSE:
        return key
    home = (key["tonic"], "maj" if key["mode"] == "major" else "min")
    other_tonic, other_mode = key["relative"]
    away = (other_tonic, "maj" if other_mode == "major" else "min")
    here = there = 0.0
    for start, end, root, quality in found:
        if (root, quality) == home:
            here += end - start
        elif (root, quality) == away:
            there += end - start
    first = next(((root, quality) for _s, _e, root, quality in found
                  if root >= 0), None)
    switch = there > here * 1.15 or (
        there * 1.15 >= here and first == away)
    if not switch:
        return key
    return dict(key, tonic=other_tonic, mode=other_mode,
                relative=[key["tonic"], key["mode"]],
                fit=key["relative_fit"], relative_fit=key["fit"],
                name=NAMES[other_tonic] + ("m" if other_mode == "minor"
                                           else ""))


def _diatonic(tonic: int, mode: str) -> set:
    """The triads that belong to a key, as (root, quality)."""
    steps = (0, 2, 4, 5, 7, 9, 11) if mode == "major" else (0, 2, 3, 5, 7, 8, 10)
    scale = [(tonic + step) % 12 for step in steps]
    out = set()
    for index, root in enumerate(scale):
        third = (scale[(index + 2) % 7] - root) % 12
        fifth = (scale[(index + 4) % 7] - root) % 12
        if fifth == 7 and third in (3, 4):
            out.add((root, "maj" if third == 4 else "min"))
    return out


def chords(notes: Sequence, basses: Sequence, key: Optional[dict],
           seconds_per: float, offset: float = 0.0) -> list:
    """The chord sequence, as [start, end, root, quality] with times in
    seconds; a root of -1 is no chord. ``offset`` is where the first
    reading's share of the track begins."""
    if not notes:
        return []
    strength = [sum(row) for row in notes]
    ordered = sorted(value for value in strength if value > 0.0)
    typical = ordered[len(ordered) // 2] if ordered else 0.0
    states = [(-1, "none")] + [(root, quality) for root in range(12)
                               for quality in TRIADS]
    belongs = (_diatonic(key["tonic"], key["mode"])
               if key and key.get("confidence", 0.0) >= SURE else set())
    # Heard over CONTEXT readings either side (see there). The bass is not:
    # it holds its note and says where a chord changes to the reading.
    heard = []
    for index in range(len(notes)):
        first = max(0, index - CONTEXT)
        last = min(len(notes), index + CONTEXT + 1)
        heard.append([sum(notes[at][pc] for at in range(first, last))
                      for pc in range(12)])
    fits = []
    for row, low, total in zip(heard, basses, strength):
        here = []
        norm = math.sqrt(sum(v * v for v in row)) or 1.0
        bass_total = sum(low) or 1.0
        for root, quality in states:
            if root < 0:
                # Nothing much sounding with a pitch: no chord fits best.
                here.append(0.6 if total < typical * NO_CHORD else 0.0)
                continue
            template = [0.0] * 12
            for interval, weight in TRIADS[quality]:
                template[(root + interval) % 12] = weight
            tnorm = math.sqrt(sum(v * v for v in template))
            score = sum(a * b for a, b in zip(row, template)) / (norm * tnorm)
            score += ROOT_BASS * low[root] / bass_total
            if (root, quality) in belongs:
                score += IN_KEY
            here.append(score)
        fits.append(here)
    # Viterbi: the best path through the readings where staying costs
    # nothing and changing costs CHANGE.
    count = len(states)
    best = list(fits[0])
    back: List[List[int]] = []
    for here in fits[1:]:
        top = max(range(count), key=lambda s: best[s])
        top_score = best[top] - CHANGE
        new = []
        came = []
        for state in range(count):
            stay = best[state]
            if stay >= top_score:
                new.append(stay + here[state])
                came.append(state)
            else:
                new.append(top_score + here[state])
                came.append(top)
        best = new
        back.append(came)
    path = [max(range(count), key=lambda s: best[s])]
    for came in reversed(back):
        path.append(came[path[-1]])
    path.reverse()
    out = []
    for index, state in enumerate(path):
        root, quality = states[state]
        start = max(0.0, offset + index * seconds_per)
        if out and out[-1][2] == root and out[-1][3] == quality:
            out[-1][1] = start + seconds_per
        else:
            out.append([start, start + seconds_per, root, quality])
    return out


#: The key through the track, not only over the whole of it: a song that
#: goes up a tone for its last chorus is in another key there. How far
#: either side of a moment its key is heard, in seconds; how often it is
#: read; and what moving to another key costs against a fit of 0 to 1 a
#: reading - at 0.8 another key has to fit clearly better for most of ten
#: seconds.
KEY_REACH = 12.0
KEY_STEP = 1.0
KEY_CHANGE = 0.8
#: How much the key of the whole track is favoured a reading, so that a
#: passage that fits two keys about equally stays in the song's own.
KEY_HOME = 0.02


def keys(notes: Sequence, key: Optional[dict], seconds_per: float,
         offset: float = 0.0, profiles=None) -> list:
    """The key through the track, as [start, end, tonic, mode] with times
    in seconds, or [] where no part of it has a key worth following.

    Read as a key signature - which seven notes - rather than a key: C
    major and A minor are one set of notes, and a key and its relative
    swapping back and forth is not a change of anything a note is chosen
    from. A signature that moves is the whole key moving, so the tonic
    moves with it and the mode stays the track's. Sure or not is asked of
    each stretch rather than of the whole: a song that changes key fits no
    one key well over all of it.
    """
    if not notes:
        return []
    major, minor = profiles or (MAJOR, MINOR)
    count = len(notes)
    stride = max(1, int(round(KEY_STEP / seconds_per)))
    reach = max(1, int(round(KEY_REACH / seconds_per)))
    running = [[0.0] * 12]
    for row in notes:
        last = running[-1]
        running.append([last[pc] + row[pc] for pc in range(12)])
    points = list(range(0, count, stride))
    fits = []
    sure = []
    for at in points:
        low, high = max(0, at - reach), min(count, at + reach + 1)
        window = [running[high][pc] - running[low][pc] for pc in range(12)]
        here = []
        for signature in range(12):
            as_major = [major[(pc - signature) % 12] for pc in range(12)]
            as_minor = [minor[(pc - (signature + 9)) % 12] for pc in range(12)]
            here.append(max(_correlation(window, as_major),
                            _correlation(window, as_minor))
                        if any(window) else 0.0)
        ordered = sorted(here, reverse=True)
        sure.append(ordered[0] - ordered[1])
        fits.append(here)
    if sorted(sure)[len(sure) // 2] < SURE:
        return []
    mode = key["mode"] if key else "major"
    if key and key.get("confidence", 0.0) >= SURE:
        home = (key["tonic"] if key["mode"] == "major"
                else (key["tonic"] + 3) % 12)
    else:
        totals = [sum(row[s] for row in fits) for s in range(12)]
        home = totals.index(max(totals))
    for row in fits:
        row[home] += KEY_HOME
    best = list(fits[0])
    back: List[List[int]] = []
    for here in fits[1:]:
        top = max(range(12), key=lambda s: best[s])
        leave = best[top] - KEY_CHANGE
        new, came = [], []
        for signature in range(12):
            if best[signature] >= leave:
                new.append(best[signature] + here[signature])
                came.append(signature)
            else:
                new.append(leave + here[signature])
                came.append(top)
        best = new
        back.append(came)
    path = [max(range(12), key=lambda s: best[s])]
    for came in reversed(back):
        path.append(came[path[-1]])
    path.reverse()
    out: list = []
    span = stride * seconds_per
    for index, signature in enumerate(path):
        tonic = signature if mode == "major" else (signature + 9) % 12
        start = max(0.0, offset + points[index] * seconds_per - span / 2.0)
        end = offset + points[index] * seconds_per + span / 2.0
        if out and out[-1][2] == tonic:
            out[-1][1] = end
        else:
            out.append([start, end, tonic, mode])
    if out:
        out[0][0] = 0.0
    return out


def key_at(harmony: Optional[dict], when: float):
    """The key at ``when``: (tonic, mode), from the key through the track
    where it was read and the track's own otherwise; None with no key."""
    if not harmony or not (harmony.get("key") or harmony.get("keys")):
        return None
    for start, end, tonic, mode in harmony.get("keys") or ():
        if start <= when < end:
            return tonic, mode
    found = harmony.get("keys") or ()
    if found and when >= found[-1][1]:
        return found[-1][2], found[-1][3]
    key = harmony["key"]
    return key["tonic"], key["mode"]


def in_key(chord, tonic: int, mode: str) -> bool:
    """Whether a (root, quality) chord is one of the key's own."""
    return tuple(chord) in _diatonic(tonic, mode)


def analyse(samples, rate: int, channels: int, should_stop=None,
            on_progress=None, profiles=None) -> Optional[dict]:
    """The whole answer for a track, or None if there is nothing to read.

    ``{"key": {...}, "tuning": semitones, "chords": [...], "keys": [...],
    "lead": [...],
    "lead_from": seconds, "rate": readings a second, "tonal": 0..1}``.
    ``lead`` is one entry a reading - reading ``i`` is centred at
    ``lead_from + i / rate`` - a pitch in semitones (MIDI, tuned) or None.
    ``tonal`` is how much of the track has a pitch to it at all: a drum
    track is near 0.
    """
    if not samples or rate <= 0:
        return None
    frames_total = len(samples) // max(1, channels)
    if frames_total / rate > MAX_SECONDS:
        return None
    mono, reduced = mono_at(samples, rate, channels)
    if len(mono) < WINDOW:
        return None
    frames = readings(mono, reduced, should_stop, on_progress)
    if not frames:
        return None
    tuned = tuning(frames)
    notes, basses, leads = chroma(frames, tuned)
    total = [0.0] * 12
    for row in notes:
        for pc in range(12):
            total[pc] += row[pc]
    major, minor = profiles or (MAJOR, MINOR)
    key = key_of(total, major, minor) if any(total) else None
    per = HOP / reduced
    key_found = key
    # A reading is centred half a window into the samples it was taken
    # from, and the share of the track it speaks for runs half a step
    # either side of that. Timed from the start of its window instead,
    # every chord changed a quarter of a second early.
    centre = WINDOW / (2.0 * reduced)
    tonal = tonality(frames, tuned)
    found = chords(notes, basses, key_found, per, centre - per / 2.0)
    key = settle_relative(key_found, found)
    return {"key": key, "tuning": tuned,
            "chords": found,
            "keys": keys(notes, key, per, centre - per / 2.0, profiles),
            "lead": leads, "lead_from": centre, "rate": 1.0 / per,
            "tonal": tonal, "chroma": total}


def chord_at(harmony: Optional[dict], when: float):
    """The chord sounding at ``when``: (root, quality), or None."""
    if not harmony:
        return None
    found = harmony.get("chords") or ()
    low, high = 0, len(found)
    while low < high:
        middle = (low + high) // 2
        if found[middle][1] <= when:
            low = middle + 1
        else:
            high = middle
    if low >= len(found):
        return None
    start, _end, root, quality = found[low]
    if start > when or root < 0:
        return None
    return root, quality
