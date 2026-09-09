"""What the sorter knows about the world outside the message.

A phrase list can tell you a message says "your flight". It cannot tell you
that ryanair.com is an airline, that STN is an airport, or that argos.co.uk
sells things — and those are exactly what a person uses to read a message
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
from functools import lru_cache
from pathlib import Path
from typing import Dict, Optional, Tuple

#: What a sector is worth knowing for. A sector is a hint about the sender,
#: never a statement about the message: a bank sends security codes and
#: marketing alike, so these feed the soft-evidence path and are capped well
#: below the threshold that would move anybody's mail.
SECTOR_TOPICS: Dict[str, Tuple[str, float]] = {
    "airline": ("TRAVEL", 1.6),
    "hotel": ("EVENT", 1.0),
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


@lru_cache(maxsize=1)
def _data() -> Dict[str, dict]:
    """The whole file, read once. An empty answer if it is not there."""
    for candidate in (_root() / "data" / "lexicon.json.gz",
                      Path(__file__).resolve().parent / "data" / "lexicon.json.gz"):
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


def available() -> bool:
    return bool(_data()["brands"])


def describe() -> str:
    data = _data()
    if not data["brands"]:
        return "no lexicon bundled"
    return (f"{len(data['brands']):,} brands, {len(data['airports']):,} airports "
            f"(built {data['built']})")


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
    Argos. Returns ("", "") when nothing is known, which is the common case
    and costs one dictionary lookup.
    """
    host = _host_of(sender)
    if not host:
        return "", ""
    data = _data()
    domains, brands = data["domains"], data["brands"]
    if not brands:
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
    if not airports:
        return None
    for match in re.finditer(r"\b([A-Z]{3})\s*(?:to|-|–|>|/)\s*([A-Z]{3})\b",
                             text or ""):
        first, second = match.group(1), match.group(2)
        if first != second and first in airports and second in airports:
            return first, second
    return None
