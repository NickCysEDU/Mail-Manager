"""Verdicts kept from one scan to the next.

Scanning the last seven days on Monday and again on Tuesday re-reads six days
of mail that has not changed and cannot change - a message is immutable once
it is sent - and pays a model to reach the same conclusion about all of it a
second time. On a paid provider that is money; on a local model it is minutes.

So verdicts are written down. A message is identified by its mailbox and UID,
which IMAP guarantees is stable and never reused, and a cached verdict is only
handed back when the *recipe* matches: the provider, the model, the effort
level, the ruleset, the profile and the body limit, hashed together. Change
any of those and every cached verdict for the old recipe stops being offered,
because it was an answer to a different question.

Three things are deliberately never cached:

**Failures.** A verdict that came back as an error should be retried, not
remembered - the network is usually why, and the network gets better.

**Fallbacks.** When the provider is unreachable the app answers with its own
rules engine and says so. Caching that would pin a stand-in answer in place
long after the provider came back.

**Anything from a mailbox whose UIDVALIDITY moved.** The server is telling you
its UIDs mean something else now, and every key for that mailbox is void.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from models import Classification

log = logging.getLogger(__name__)

FILENAME = "verdicts.json"

#: Entries beyond this go, oldest first. Twenty thousand is far more than a
#: year of ordinary mail and still a file measured in megabytes.
MAX_ENTRIES = 20000

#: A verdict older than this is dropped on load. Not because it went wrong,
#: but because the mail it describes has long since left the window anybody
#: scans, and carrying it costs a read every launch.
MAX_AGE_DAYS = 120

#: Marks a verdict the rules engine produced when the provider could not be
#: reached. :mod:`llm_engine` writes this into the model name.
FALLBACK_MARK = "local rules"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def recipe_for(settings) -> str:
    """A short hash of everything that changes what a verdict would be.

    Anything absent from this list is something that can change without
    invalidating a cached answer: the confidence threshold and the folder
    names decide what happens *to* a verdict, not what the verdict is.
    """
    parts = [
        str(getattr(settings, "provider", "")),
        str(getattr(settings, "model", "")),
        str(getattr(settings, "effort", "")),
        str(getattr(settings, "ruleset", "")),
        str(getattr(settings, "sort_profile", "")),
        ",".join(sorted(getattr(settings, "topics", []) or [])),
        str(getattr(settings, "max_body_chars", "")),
        str(getattr(settings, "base_url", "")),
    ]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:16]


def key_for(message) -> str:
    """How a message is identified across scans.

    Mailbox plus UID, because that is the pair IMAP promises is stable. The
    account is in there too: two mailboxes will happily hand out the same UID
    for entirely different mail.
    """
    account = getattr(message, "account_id", "") or ""
    folder = getattr(message, "source_folder", "") or ""
    uid = getattr(message, "uid", "") or ""
    if not uid:
        return ""
    return f"{account}\x1f{folder}\x1f{uid}"


@dataclass(frozen=True)
class Entry:
    key: str
    recipe: str
    payload: Dict[str, object]
    model: str
    when: str

    def to_dict(self) -> Dict[str, object]:
        return {"key": self.key, "recipe": self.recipe, "payload": self.payload,
                "model": self.model, "when": self.when}

    @classmethod
    def from_dict(cls, raw: object) -> Optional["Entry"]:
        if not isinstance(raw, dict):
            return None
        key = str(raw.get("key", ""))
        recipe = str(raw.get("recipe", ""))
        payload = raw.get("payload")
        if not key or not recipe or not isinstance(payload, dict):
            return None
        return cls(key=key, recipe=recipe, payload=payload,
                   model=str(raw.get("model", "") or ""),
                   when=str(raw.get("when", "") or ""))

    def age_days(self) -> float:
        try:
            stamp = datetime.fromisoformat(self.when)
        except ValueError:
            return 0.0
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (_now() - stamp).total_seconds() / 86400.0


def _classification_payload(classification: Classification) -> Dict[str, object]:
    """The parts of a verdict worth keeping, in the shape from_payload reads."""
    return {
        "summary": classification.summary,
        "is_job_related": classification.is_job_related,
        "category": classification.category.value,
        "other_category": classification.other_category.value,
        "confidence_score": classification.confidence_score,
        "reasoning": classification.reasoning,
        "adjustments": list(classification.adjustments),
        # Kept so a reused verdict can still explain itself. A row that says
        # nothing about why is worse than one the model was asked about
        # again.
        "signals": list(classification.signals),
        "scores": dict(classification.scores),
    }


class VerdictCache:
    """The verdict file, loaded once per scan."""

    def __init__(self, entries: Optional[Sequence[Entry]] = None,
                 path: Optional[Path] = None) -> None:
        self._entries: Dict[str, Entry] = {e.key: e for e in (entries or ())}
        self._path = path
        self.hits = 0
        self.misses = 0
        self._dirty = False

    @staticmethod
    def default_path() -> Path:
        import config
        return config.app_support_dir() / FILENAME

    # -- disk ------------------------------------------------------------
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "VerdictCache":
        """Read the file. Anything unreadable is an empty cache, never a raise.

        A cache is by definition something the app can do without, so there is
        no failure here worth telling anybody about: the cost of a damaged
        file is one slow scan.
        """
        path = path or cls.default_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read the verdict cache at %s (%s).", path, exc)
            return cls(path=path)
        rows = raw.get("verdicts") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return cls(path=path)
        entries = [e for e in (Entry.from_dict(row) for row in rows)
                   if e is not None and e.age_days() <= MAX_AGE_DAYS]
        return cls(entries, path=path)

    def save(self, path: Optional[Path] = None) -> Optional[Path]:
        """Write the file, unless nothing changed. Never raises."""
        if not self._dirty:
            return None
        path = path or self._path or self.default_path()
        self._path = path
        kept = sorted(self._entries.values(), key=lambda e: e.when)[-MAX_ENTRIES:]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps({"version": 1,
                                  "verdicts": [e.to_dict() for e in kept]})
            fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                            prefix=".verdicts-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp_name, path)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise
            try:
                os.chmod(path, 0o600)
            except OSError:  # pragma: no cover - best effort
                pass
        except OSError as exc:
            log.warning("Could not write the verdict cache (%s).", exc)
            return None
        self._dirty = False
        return path

    # -- using it --------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    def get(self, message, recipe: str) -> Optional[Classification]:
        key = key_for(message)
        if not key:
            return None
        entry = self._entries.get(key)
        if entry is None or entry.recipe != recipe:
            return None
        try:
            return Classification.from_payload(entry.payload, model=entry.model)
        except Exception:  # noqa: BLE001 - a bad row is a miss, not a crash
            del self._entries[key]
            self._dirty = True
            return None

    def put(self, message, recipe: str, classification: Classification) -> bool:
        """Remember one verdict. False if it was not worth remembering."""
        if classification.error is not None:
            return False
        if FALLBACK_MARK in (classification.model or ""):
            return False
        key = key_for(message)
        if not key:
            return False
        self._entries[key] = Entry(
            key=key, recipe=recipe,
            payload=_classification_payload(classification),
            model=classification.model or "",
            when=_now().isoformat(timespec="seconds"))
        self._dirty = True
        return True

    def split(self, messages: Sequence, recipe: str) -> Tuple[List, Dict[int, Classification]]:
        """Sort messages into "needs the model" and "already answered".

        Returns the messages still to classify and, separately, a map from
        each *original* index to the verdict already held - so the caller can
        put the two halves back in order without losing track of which is
        which.
        """
        pending: List = []
        known: Dict[int, Classification] = {}
        for index, message in enumerate(messages):
            hit = self.get(message, recipe)
            if hit is None:
                pending.append(message)
                self.misses += 1
            else:
                known[index] = hit
                self.hits += 1
        return pending, known

    @staticmethod
    def merge(messages: Sequence, pending: Sequence, fresh: Sequence,
              known: Dict[int, Classification]) -> List[Classification]:
        """Put the two halves back in the original message order."""
        by_id = {id(message): verdict for message, verdict in zip(pending, fresh)}
        out: List[Classification] = []
        for index, message in enumerate(messages):
            if index in known:
                out.append(known[index])
            else:
                out.append(by_id.get(id(message)) or Classification(
                    error="No verdict was produced for this message."))
        return out

    def forget_mailbox(self, account_id: str, folder: str = "") -> int:
        """Drop every entry for one mailbox. For a UIDVALIDITY change."""
        prefix = f"{account_id}\x1f{folder}" if folder else f"{account_id}\x1f"
        doomed = [key for key in self._entries if key.startswith(prefix)]
        for key in doomed:
            del self._entries[key]
        if doomed:
            self._dirty = True
        return len(doomed)

    def clear(self) -> None:
        if self._entries:
            self._dirty = True
        self._entries = {}

    def describe(self) -> str:
        if not self._entries:
            return "Nothing kept from previous scans yet."
        return (f"{len(self._entries):,} verdict(s) kept, so a repeat scan of "
                "the same window costs nothing.")
