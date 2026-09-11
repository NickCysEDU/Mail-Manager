"""What the sorter knows about the world outside the message.

A phrase list can tell you a message says "your flight". It cannot tell you
that ryanair.com is an airline, that STN is an airport, or that argos.co.uk
sells things, and those are exactly what a person uses to read a message
that never says what it is:

    "Seat 14C — FR7712 STN to DUB, Tuesday. Bags close 40 minutes before."

Two public datasets, fetched by ``tools/build_lexicon.py`` and committed as
one 330 KB file so nothing here ever touches the network: every large and
medium airport's IATA code (OurAirports, public domain) and 44,000 company
domains with the sector each belongs to (Wikidata, CC0).

Matching is on the brand name rather than the whole domain, because one shop
writes from argos.co.uk, email.argos.co.uk and argos-mail.com and all three
are Argos.

Loaded on first use and never again. Everything here answers in constant time
and never raises: a missing or damaged file leaves the sorter exactly as
capable as it was before this existed.
"""

from __future__ import annotations

import gzip
import json
import re
import sys

import lexicon_blob
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

#: What a sector is worth knowing for. A sector is a hint about the sender,
#: never a statement about the message: a bank sends security codes and
#: marketing alike, so these feed the soft-evidence path and are capped well
#: below the threshold that would move anybody's mail.
SECTOR_TOPICS: Dict[str, Tuple[str, float]] = {
    "airline": ("TRAVEL", 1.6),
    "bank": ("FINANCE", 1.4),
    "telecom": ("FINANCE", 1.2),
    "utility": ("FINANCE", 1.4),
    "retail": ("RECEIPT", 1.0),
    "courier": ("SHIPPING", 2.0),
    "social": ("SOCIAL", 1.6),
    "news": ("NEWSLETTER", 1.2),
}

#: Words that are somebody's brand and also ordinary English. Matching these
#: as brands costs more than it earns.
_TOO_COMMON = frozenset({
    "email", "mail", "news", "post", "group", "team", "online", "digital",
    "media", "global", "world", "today", "daily", "times", "press", "first",
    "national", "united", "american", "british", "royal", "central", "city",
    "metro", "star", "sun", "mirror", "independent", "guardian", "observer",
    "standard", "record", "herald", "journal", "review", "week", "month",
    "home", "house", "office", "work", "life", "living", "people", "family",
    "health", "care", "service", "services", "solutions", "systems", "tech",
    "data", "cloud", "network", "connect", "direct", "express", "prime",
    "plus", "pro", "max", "next", "open", "smart", "shop", "store", "market",
})


def _root() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


def _candidates(name: str):
    """Where a bundled data file might be, frozen app or source tree."""
    here = Path(__file__).resolve().parent
    seen = set()
    for base in (_root(), here):
        path = base / "data" / name
        if path not in seen:
            seen.add(path)
            yield path


@lru_cache(maxsize=1)
def _blob():
    """The memory-mapped form, if this build ships one.

    Preferred because opening it costs nothing that depends on how much is in
    it: no decompression, no parse, no dictionary of thirty thousand strings.
    The tables only ever grow, and this is the form whose cost does not.
    """
    for candidate in _candidates("lexicon.bin"):
        opened = lexicon_blob.open_blob(candidate)
        if opened is not None and opened.tables.get("brands"):
            return opened
    return None


@lru_cache(maxsize=1)
def _data() -> Dict[str, dict]:
    """The tables, however they are stored. An empty answer if there are none.

    The blob and the JSON both answer ``get`` and ``in``, which is all the
    lookups below use, so the rest of this module cannot tell which it got.
    """
    blob = _blob()
    if blob is not None:
        meta = blob.table("meta")
        return {
            "airports": blob.table("airports"),
            "brands": blob.table("brands"),
            "domains": blob.table("domains"),
            "built": (meta.get("built", "") if meta else ""),
        }
    for candidate in _candidates("lexicon.json.gz"):
        try:
            with gzip.open(candidate, "rt", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            return {
                "airports": payload.get("airports") or {},
                "brands": payload.get("brands") or {},
                "domains": payload.get("domains") or {},
                "built": payload.get("built", ""),
            }
    return {"airports": {}, "brands": {}, "domains": {}, "built": ""}


def reset() -> None:
    """Forget both cached forms.

    There are two - the mapped file and the parsed JSON - and anything that
    changes where they are read from has to clear both, or the answer comes
    from whichever was warmed first. One function so that cannot be got wrong.
    """
    blob = _blob.cache_info().currsize and _blob()
    if blob:
        blob.close()
    _blob.cache_clear()
    _data.cache_clear()
    _sector_of_host.cache_clear()


def available() -> bool:
    return len(_data()["brands"]) > 0


def describe() -> str:
    data = _data()
    if not len(data["brands"]):
        return "no lexicon bundled"
    how = "mapped" if _blob() is not None else "parsed"
    return (f"{len(data['brands']):,} brands, {len(data['airports']):,} airports "
            f"({how}, built {data['built']})")


_HOST = re.compile(r"[a-z0-9][a-z0-9.-]*\.[a-z]{2,}")


def _host_of(sender: str) -> str:
    text = (sender or "").strip().lower()
    if "@" in text:
        text = text.rsplit("@", 1)[-1]
    text = text.strip("<>() \t").rstrip(".")
    found = _HOST.search(text)
    return found.group(0) if found else ""


def sector_of(sender: str) -> Tuple[str, str]:
    """The sector a sender belongs to, and the name that matched.

    Tries the exact domain first, then every brand-shaped label in the host,
    so mail from ``email.argos.co.uk`` and ``argos-mail.com`` both land on
    Argos. Returns ("", "") when nothing is known, which is the common case.
    """
    return _sector_of_host(_host_of(sender))


@lru_cache(maxsize=4096)
def _sector_of_host(host: str) -> Tuple[str, str]:
    """The same question, asked once per host rather than once per message.

    An inbox has far fewer senders than messages - a newsletter writes weekly
    from the same address for years - and the mapped tables answer by binary
    search rather than by hash, so repeating the search is the one cost worth
    avoiding here.
    """
    if not host:
        return "", ""
    data = _data()
    domains, brands = data["domains"], data["brands"]
    if not len(brands):
        return "", ""

    parts = host.split(".")
    for start in range(len(parts) - 1):
        candidate = ".".join(parts[start:])
        sector = domains.get(candidate)
        if sector:
            return sector, candidate

    # No exact domain. Try the brand names inside it, longest first, so
    # "britishairways" is preferred over "british".
    seen = set()
    for label in parts:
        for token in re.split(r"[^a-z0-9]+", label):
            if len(token) >= 4 and token not in _TOO_COMMON:
                seen.add(token)
    for token in sorted(seen, key=len, reverse=True):
        sector = brands.get(token)
        if sector:
            return sector, token
    return "", ""


def is_airport(code: str) -> bool:
    """Whether three letters are a real airport, not any three letters."""
    return (code or "").strip().upper() in _data()["airports"]


def airport_pair(text: str) -> Optional[Tuple[str, str]]:
    """The first real airport pair in some text, if there is one.

    "STN to DUB" is a flight. "PDF to DOC" is not, and telling them apart is
    the whole reason for carrying four and a half thousand airport codes.
    """
    airports = _data()["airports"]
    if not len(airports):
        return None
    for match in re.finditer(r"\b([A-Z]{3})\s*(?:to|-|–|>|/)\s*([A-Z]{3})\b",
                             text or ""):
        first, second = match.group(1), match.group(2)
        if first != second and first in airports and second in airports:
            return first, second
    return None
