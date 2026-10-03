"""Who has already been written to, so nobody is written to twice.

A drafting rule has no memory: somebody who writes four times in a morning
gets four drafts, a mailing list one per post, and scanning the same window
twice writes them all again. So every draft is written down here (to whom,
by which rule, when), and a rule with a once-per-sender window asks first.

**The address is the key, folded to lower case**: display names change and
addresses do not.

**Nothing is remembered forever.** Entries older than the longest window a
rule can have are dropped, so this is a cooldown timer, not an address book.

**A missing or damaged file is an empty log, never an error**: that costs a
duplicate draft, where refusing to run would cost the feature.

**It is written where everything else is**: Application Support, through the
same vault, 0600, and never leaves the machine.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import vault

log = logging.getLogger(__name__)

FILENAME = "replies.json"

#: The longest cooldown a rule can have, and so how long an entry is kept: a
#: year.
MAX_DAYS = 366

#: At most this many: a mailbox that matches a drafting rule thousands of times
#: has a rule problem, and the log should not become a second one.
MAX_ENTRIES = 5000


@dataclass(frozen=True)
class Sent:
    """One draft that was written, and to whom."""

    address: str
    rule: str
    when: str

    def moment(self) -> Optional[datetime]:
        try:
            parsed = datetime.fromisoformat(self.when)
        except (TypeError, ValueError):
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def to_dict(self) -> Dict[str, str]:
        return {"address": self.address, "rule": self.rule, "when": self.when}

    @classmethod
    def from_dict(cls, raw) -> Optional["Sent"]:
        if not isinstance(raw, dict):
            return None
        address = str(raw.get("address", "")).strip().lower()
        if not address:
            return None
        return cls(address=address, rule=str(raw.get("rule", "")),
                   when=str(raw.get("when", "")))


def key_for(address: str) -> str:
    """The address as this file keys on it."""
    return (address or "").strip().lower()


class ReplyLog:
    """Every draft written, read once and asked per message."""

    def __init__(self, entries: Optional[Sequence[Sent]] = None,
                 path: Optional[Path] = None) -> None:
        self._entries: List[Sent] = list(entries or ())
        self._path = path

    # -- disk --------------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        import config

        return config.app_support_dir() / FILENAME

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "ReplyLog":
        """Read the file. A damaged or missing one is an empty log; never
        raises.
        """
        path = path or cls.default_path()
        try:
            raw = vault.shared().read(path)
        except Exception as exc:      # noqa: BLE001 - a log, not the mail
            log.info("Could not read the reply log (%s).", exc)
            return cls(path=path)
        if raw is None:
            return cls(path=path)
        rows = raw.get("replies") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return cls(path=path)
        found = [s for s in (Sent.from_dict(row) for row in rows) if s]
        return cls(found, path=path)

    def save(self, path: Optional[Path] = None) -> bool:
        """Write it back, oldest first, pruned. Returns whether it worked."""
        path = path or self._path or self.default_path()
        self._path = path
        self.forget_old()
        kept = self._entries[-MAX_ENTRIES:]
        try:
            return bool(vault.shared().write(
                path, {"replies": [s.to_dict() for s in kept]}))
        except Exception as exc:      # noqa: BLE001
            log.info("Could not write the reply log (%s).", exc)
            return False

    # -- asking ------------------------------------------------------------
    def last_to(self, address: str, rule: str = "") -> Optional[datetime]:
        """When this address was last written to, by this rule or any.

        Per rule, because two rules saying different things are two
        conversations: acknowledging an interview should not silence
        declining a recruiter.
        """
        wanted = key_for(address)
        if not wanted:
            return None
        best: Optional[datetime] = None
        for entry in self._entries:
            if entry.address != wanted:
                continue
            if rule and entry.rule and entry.rule != rule:
                continue
            moment = entry.moment()
            if moment is not None and (best is None or moment > best):
                best = moment
        return best

    def too_soon(self, address: str, rule: str, days: int,
                 now: Optional[datetime] = None) -> bool:
        """Whether a draft to this address would be inside the cooldown."""
        if days <= 0:
            return False
        last = self.last_to(address, rule)
        if last is None:
            return False
        now = now or datetime.now(timezone.utc)
        return (now.astimezone(timezone.utc)
                - last.astimezone(timezone.utc)) < timedelta(days=days)

    # -- telling -----------------------------------------------------------
    def remember(self, address: str, rule: str = "",
                 now: Optional[datetime] = None) -> bool:
        """Write down that a draft went to this address. Not saved yet."""
        wanted = key_for(address)
        if not wanted:
            return False
        moment = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self._entries.append(
            Sent(address=wanted, rule=rule, when=moment.isoformat()))
        return True

    def forget_old(self, now: Optional[datetime] = None) -> int:
        """Drop anything past the longest window a rule could ask for."""
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        cutoff = now - timedelta(days=MAX_DAYS)
        before = len(self._entries)
        kept = []
        for entry in self._entries:
            moment = entry.moment()
            if moment is None or moment.astimezone(timezone.utc) >= cutoff:
                kept.append(entry)
        self._entries = kept
        return before - len(self._entries)

    def clear(self) -> None:
        self._entries = []

    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> tuple:
        return tuple(self._entries)
