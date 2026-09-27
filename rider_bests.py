"""The best each track has been ridden to, kept on this machine.

A run ends with how it went, and a score means more against the last one.
Kept by a fingerprint of the track's own analysis rather than by its name
or where it is: what is written down is a string of hex and a number per
game, nothing that says what the music was, and nothing leaves the Mac.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

#: How much of a track the fingerprint is taken from: its first few
#: minutes of analysis, which is plenty to tell one record from another
#: and does not change if the file is renamed or moved.
FRAMES = 3000


def fingerprint(frames) -> str:
    """Twenty hex characters that stand for this track's analysis."""
    digest = hashlib.sha256()
    for frame in list(frames or ())[:FRAMES]:
        try:
            digest.update(frame.tobytes())
        except AttributeError:
            digest.update(repr(tuple(frame)).encode())
    return digest.hexdigest()[:20]


class Bests:
    """Bests by track and game, in one small file."""

    def __init__(self, path) -> None:
        self.path = Path(path)
        self._table: dict = {}
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self._table = {str(k): int(v) for k, v in loaded.items()
                               if isinstance(v, (int, float))}
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            log.info("Starting the rider's bests afresh (%s).", exc)

    @staticmethod
    def _key(track: str, mode: str) -> str:
        return f"{track}:{mode}"

    def best(self, track: str, mode: str) -> Optional[int]:
        return self._table.get(self._key(track, mode))

    def offer(self, track: str, mode: str, worth: int) -> Tuple[Optional[int], bool]:
        """A finished run: the best before it, and whether it beat that."""
        before = self.best(track, mode)
        if before is not None and worth <= before:
            return before, False
        self._table[self._key(track, mode)] = int(worth)
        self._save()
        return before, worth > 0

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            partial = self.path.with_suffix(".part")
            partial.write_text(json.dumps(self._table, indent=0,
                                          sort_keys=True), encoding="utf-8")
            os.replace(partial, self.path)
        except OSError as exc:
            log.info("Could not keep the rider's best (%s).", exc)
