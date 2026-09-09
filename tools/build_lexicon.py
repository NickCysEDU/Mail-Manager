#!/usr/bin/env python3
"""Build the world-knowledge lexicon the sorter checks senders against.

    python tools/build_lexicon.py            # rebuild data/lexicon.json.gz
    python tools/build_lexicon.py --report   # say what is in the current one

A phrase list can tell you a message says "your flight". It cannot tell you
that ryanair.com is an airline, that STN is an airport, or that argos.co.uk
sells things - and those are what a person uses to read a message that never
says what it is.

Two public sources, both fetched at build time and committed as one file so
the app never needs the network:

  OurAirports   IATA codes for every large and medium airport. Public domain.
  Wikidata      Companies with an industry and an official website, which is
                what turns a sending domain into "this is a bank". CC0.

The result is deliberately small. Only domains are kept, only for sectors
that map to something the sorter can act on, and only the registrable part -
"delta.com", not "es.delta.com" - so one row covers every country's site.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import io
import json
import ssl
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Dict, Iterable, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

OUT = ROOT / "data" / "lexicon.json.gz"
AGENT = "MailManager/1.0 (offline mail sorter; dataset build)"
AIRPORTS = "https://davidmegginson.github.io/ourairports-data/airports.csv"
WIKIDATA = "https://query.wikidata.org/sparql"

#: Wikidata classes worth knowing about, and what each tells the sorter.
#: The label is the sector the app reasons with, not Wikidata's wording.
SECTORS: Tuple[Tuple[str, str, str], ...] = (
    ("airline",    "Q46970",   "airlines"),
    ("bank",       "Q22687",   "banks"),
    ("bank",       "Q2143354", "insurers"),
    ("retail",     "Q507619",  "retail chains"),
    ("retail",     "Q180846",  "supermarket chains"),
    ("telecom",    "Q2401749", "telecoms operators"),
    ("utility",    "Q1326624", "electric utilities"),
    ("social",     "Q3220391", "social networking services"),
    ("news",       "Q11032",   "newspapers"),
    # Looked up from what DHL, FedEx and Royal Mail actually are, rather
    # than from a class that sounded right and returned nothing.
    ("courier",    "Q1529128", "postal services"),
    ("courier",    "Q1447463", "package delivery"),
    # Hotels and restaurants were dropped after measuring what they held: the
    # chains anybody actually gets mail from - Marriott, Hilton, Hyatt,
    # Novotel, Premier Inn - were already claimed as retail, so the class
    # contributed 11,044 individual small hotels ("101starsmotel",
    # "11thavenuehostel") that will never be a sender, and a third of the
    # file's size along with them.
)

#: Nothing is taken from the "business" class: it has millions of members
#: and says nothing about what a company does.
BROAD: Set[str] = set()


def _context() -> ssl.SSLContext:
    import certifi

    return ssl.create_default_context(cafile=certifi.where())


def _get(url: str, timeout: float = 120.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=_context()) as reply:
        return reply.read()


def registrable(url: str) -> str:
    """"https://es.delta.com/path" -> "delta.com".

    Not a public-suffix parser: a two-letter last label with a short one
    before it is treated as a country pair (co.uk, com.au), which covers the
    shapes that actually appear here.
    """
    host = urllib.parse.urlsplit(url.strip()).hostname or ""
    host = host.lower().strip(".")
    if not host or host.replace(".", "").isdigit():
        return ""
    parts = host.split(".")
    if len(parts) < 2:
        return ""
    if (len(parts) >= 3 and len(parts[-1]) == 2
            and parts[-2] in {"co", "com", "org", "net", "ac", "gov", "edu"}):
        parts = parts[-3:]
    else:
        parts = parts[-2:]
    return ".".join(parts)


def fetch_airports() -> Dict[str, str]:
    """IATA code -> the town it serves, for large and medium airports."""
    raw = _get(AIRPORTS).decode("utf-8", "replace")
    found: Dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(raw)):
        code = (row.get("iata_code") or "").strip().upper()
        if len(code) != 3 or not code.isalpha():
            continue
        if row.get("type") not in ("large_airport", "medium_airport"):
            continue
        found[code] = (row.get("municipality") or row.get("name") or "").strip()[:40]
    return found


def fetch_sector(class_id: str, limit: int = 40000) -> Set[str]:
    """Registrable domains of everything in one Wikidata class."""
    extra = ""
    if class_id in BROAD:
        # Businesses at large is millions of rows; only shops are wanted.
        extra = ("?item wdt:P452 ?industry . "
                 "VALUES ?industry { wd:Q900482 wd:Q3736076 wd:Q1639378 } ")
    query = f"""
    SELECT DISTINCT ?site WHERE {{
      ?item wdt:P31/wdt:P279* wd:{class_id} .
      {extra}
      ?item wdt:P856 ?site .
    }} LIMIT {limit}
    """
    url = f"{WIKIDATA}?format=json&query={urllib.parse.quote(query)}"
    payload = json.loads(_get(url))
    found: Set[str] = set()
    for row in payload["results"]["bindings"]:
        domain = registrable(row["site"]["value"])
        if domain:
            found.add(domain)
    return found


#: Which suffix to believe when one brand name appears under several. The
#: canonical site carries the real answer and the oddities are noise:
#: amazon.com is a shop and amazon.jobs was filed under airlines;
#: royalmail.com is a courier and royalmail.com.au a hotel.
TLD_RANK: Tuple[str, ...] = (
    "com", "co.uk", "org", "net", "de", "fr", "es", "it", "nl", "com.au",
)

#: Brand names too short to be worth matching. "24", "3", "go": a two- or
#: three-letter token collides with far too much to be evidence of anything.
MIN_BRAND = 4


def brand_index(domains: Dict[str, str]) -> Dict[str, str]:
    """Second-level name -> sector, resolved where a name is claimed twice.

    Matching the brand rather than the whole domain is what makes this work
    on real mail: a shop writes from argos.co.uk, email.argos.co.uk and
    argos-mail.com, and all three are Argos.
    """
    claims: Dict[str, Dict[str, str]] = {}
    for domain, sector in domains.items():
        token, _, suffix = domain.partition(".")
        if len(token) < MIN_BRAND or not token.isalnum():
            continue
        claims.setdefault(token, {})[suffix] = sector

    def rank(suffix: str) -> Tuple[int, int]:
        try:
            return (0, TLD_RANK.index(suffix))
        except ValueError:
            return (1, len(suffix))

    resolved: Dict[str, str] = {}
    for token, by_suffix in claims.items():
        best = min(by_suffix, key=rank)
        resolved[token] = by_suffix[best]
    return resolved


def build() -> dict:
    print("==> Airports", flush=True)
    airports = fetch_airports()
    print(f"    {len(airports)} large and medium airports with an IATA code")

    domains: Dict[str, str] = {}
    counts: Counter = Counter()
    for sector, class_id, description in SECTORS:
        started = time.perf_counter()
        try:
            found = fetch_sector(class_id)
        except Exception as exc:  # noqa: BLE001 - one source failing is not fatal
            print(f"    {description}: FAILED ({str(exc)[:70]})", flush=True)
            continue
        fresh = 0
        for domain in found:
            # First sector to claim a domain keeps it: the list is ordered so
            # the more specific classes come first.
            if domain not in domains:
                domains[domain] = sector
                fresh += 1
        counts[sector] += fresh
        print(f"    {description}: {len(found)} domains, {fresh} new "
              f"({time.perf_counter() - started:.1f}s)", flush=True)

    brands = brand_index(domains)
    print(f"    brand names, ambiguity resolved: {len(brands)}")
    return {
        "built": time.strftime("%Y-%m-%d"),
        "sources": ["OurAirports (public domain)", "Wikidata (CC0)"],
        "airports": airports,
        "domains": domains,
        "brands": brands,
        "counts": dict(counts),
    }


def report() -> int:
    if not OUT.exists():
        print(f"No lexicon at {OUT}", file=sys.stderr)
        return 1
    with gzip.open(OUT, "rt", encoding="utf-8") as handle:
        data = json.load(handle)
    print(f"{OUT.name}  built {data.get('built', '?')}  "
          f"{OUT.stat().st_size / 1024:.0f} KB on disk")
    print(f"  airports {len(data.get('airports', {})):,}")
    print(f"  domains  {len(data.get('domains', {})):,}")
    print(f"  brands   {len(data.get('brands', {})):,}")
    for sector, count in sorted(data.get("counts", {}).items(),
                                key=lambda pair: -pair[1]):
        print(f"    {sector:10} {count:,}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args(argv)
    if args.report:
        return report()

    data = build()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), sort_keys=True)
    print(f"==> Wrote {OUT} ({OUT.stat().st_size / 1024:.0f} KB)")
    return report()


if __name__ == "__main__":
    raise SystemExit(main())
