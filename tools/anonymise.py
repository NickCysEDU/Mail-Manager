#!/usr/bin/env python3
"""Replace real people and organisations in a fixture with stand-ins.

The labelled set is the project's accuracy benchmark and it was built from a
real inbox. That is what makes it worth having - hand-written samples do not
contain the things that actually break a sorter - and it is also what stops it
being publishable: it names the people who wrote to one person, the companies
that interviewed them, the ones that turned them down, and the church they
attend.

So the names are replaced and everything else is kept. What matters to a
sorter is the *language* - "we have decided to move forward with other
candidates", "your details are with us", "this Sunday's service" - and none of
that is anybody's private business. Who sent it is.

Three rules make the result still worth measuring against:

**Consistency.** One real name maps to exactly one stand-in, everywhere it
appears - display name, address, subject line, body, signature. A sender who
wrote four times still wrote four times, which is what the threading and the
corrections memory are tested against.

**Shape.** A two-word name becomes a two-word name and a one-word company
becomes a one-word company, so nothing about length, capitalisation or
truncation changes underneath the tests.

**Vendors stay.** Workday, iCIMS, Greenhouse and the rest are products, not
people, and the sorter has signals that name them. Replacing those would be
measuring a different app.

Run it, then run tools/evaluate.py against both files. A score that moves means
a signal was keyed on somebody's name, which is worth knowing on its own.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parents[1]

def _protected_by_the_sorter() -> set:
    """Every host and sender word the rules engine actually reads.

    Taken from the engine rather than typed out here, because a hand-kept
    list drifts and the drift is invisible: rewriting a domain the sorter
    has a signal for changes the verdict, and the fixture then measures a
    different app. Deriving it means the two can never disagree.
    """
    sys.path.insert(0, str(ROOT))
    import rules_engine

    words = set()
    for host in (rules_engine.SCHEDULING_LINK_DOMAINS
                 + rules_engine.ASSESSMENT_LINK_DOMAINS):
        for label in str(host).split("."):
            if len(label) >= 3:
                words.add(label.lower())
    tables = [table for _c, table in rules_engine._CATEGORY_TABLES]
    tables += list(rules_engine.TOPIC_SIGNALS.values())
    tables += [rules_engine.JOB_CONTEXT_SIGNALS, rules_engine.NON_JOB_SIGNALS]
    for table in tables:
        for signal in table:
            if signal.field == "sender":
                for label in signal.phrase.replace("@", ".").split("."):
                    if len(label) >= 3:
                        words.add(label.lower())
    return words


#: Applicant-tracking systems, mail providers and consumer products. These are
#: public services the sorter recognises by name, so they stay.
KEEP = {
    "workday", "icims", "greenhouse", "lever", "ashby", "smartrecruiters",
    "successfactors", "taleo", "talemetry", "bamboohr", "jobvite", "myworkday",
    "linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster",
    "apple", "google", "microsoft", "amazon", "instagram", "snapchat",
    "facebook", "twitter", "paypal", "icloud", "gmail", "outlook", "yahoo",
    "coursera", "udemy", "united airlines", "fanduel", "honeywell",
}
KEEP |= _protected_by_the_sorter()

#: Stand-in people. Deliberately plain and clearly invented.
PEOPLE = [
    "Alice Fenwick", "Bruno Hartley", "Carla Nunez", "Dara Okonjo",
    "Elena Vasquez", "Farid Haddad", "Greta Lindqvist", "Hana Suzuki",
    "Ivo Petrov", "Jonas Meier", "Kira Balogun", "Liam Doherty",
    "Mira Castellano", "Noor Rahman", "Otto Brandt", "Pia Lindgren",
    "Quentin Marsh", "Rosa Delgado", "Samir Chaudhry", "Tara Whelan",
]

#: Stand-in organisations, in the same shapes as the originals.
COMPANIES = [
    "Northwind", "Brightpath", "Vellum", "Harlow Tech", "Fieldstone",
    "Meridian", "Cedarhall", "Ironbridge", "Larkspur", "Quarryfield",
    "Bramblewood", "Stonecroft", "Ashgrove", "Fernbank", "Copperline",
    "Dunmore", "Eastvale", "Foxglove", "Greenhollow", "Havenridge",
    "Inglewood", "Juniper", "Kestrel", "Longmere", "Marlowe",
    "Netherby", "Oakhaven", "Pinecrest", "Ravensmoor", "Sandhurst",
    "Thornfield", "Uppingham", "Varley", "Westmoor", "Yarrow",
]

#: Ordinary English that also turns up inside a company name. Replacing these
#: at token level would corrupt the prose the sorter is measured on, which is
#: the one thing this must not do.
COMMON = {
    "the", "and", "for", "with", "from", "group", "team", "teams", "careers",
    "career", "talent", "acquisition", "human", "resources", "recruiting",
    "recruitment", "department", "support", "systems", "system", "solutions",
    "services", "service", "technologies", "technology", "corp", "corporation",
    "inc", "llc", "ltd", "national", "regional", "health", "bank", "airlines",
    "university", "college", "school", "audio", "mission", "estore", "store",
    "trip", "snap", "chat", "workday", "candidate",
    "example", "com", "org", "net", "www", "mail", "email", "reply", "noreply",
    # Generic labels that turn up as subdomains and as ordinary words. A
    # domain label is not evidence that a word is a name, and mapping "help"
    # to a company put a company's name in the middle of a sentence.
    "help", "info", "news", "blog", "shop", "store", "login", "secure",
    "account", "accounts", "static", "cdn", "assets", "images", "media",
    "click", "link", "links", "track", "tracking", "notify", "notifications",
    "notification", "alerts", "alert", "updates", "update", "contact",
    "about", "home", "docs", "api", "web", "site", "portal", "auth", "sso",
    "events", "event", "jobs", "job", "apply", "hire", "hiring", "people",
    "here", "your", "this", "that", "with", "have", "will", "from", "more",
    "view", "open", "read", "sent", "time", "date", "name", "page", "list",
    # Months and weekdays, so a date never becomes a person. "passed onto the
    # Lord on June 12" came out of an earlier run as "on Dara 12".
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    # Books of the Bible, which are church vocabulary rather than people.
    # "John 5:24" is a citation; "John Everleigh" is somebody.
    "john", "matthew", "mark", "luke", "acts", "romans", "psalm", "psalms",
    "genesis", "exodus", "isaiah", "jeremiah", "daniel", "corinthians",
    "galatians", "ephesians", "philippians", "colossians", "thessalonians",
    "timothy", "titus", "hebrews", "james", "peter", "jude", "revelation",
    "proverbs", "ecclesiastes", "joshua", "samuel", "kings", "chronicles",
    "lord", "god", "jesus", "christ", "christian", "gospel", "bible",
}

#: Places of worship, kept recognisably so - the Church category is measured
#: against these and the vocabulary is the point.
CHURCHES = ["St Alban's Lutheran Church", "St Brendan's Parish Church",
            "Holy Cross Methodist Church"]


def _words(name: str) -> List[str]:
    return [w for w in re.split(r"[\s.,]+", name) if w]


def looks_like_a_person(name: str) -> bool:
    """Two or three capitalised words, no corporate suffix."""
    if any(k in name.lower() for k in KEEP):
        return False
    if re.search(r"\b(inc|llc|ltd|corp|corporation|group|team|careers?|"
                 r"recruiting|recruitment|talent|human resources|hr|systems?|"
                 r"solutions?|services?|technologies|department|support|bank|"
                 r"health|airlines?|university|college)\b", name, re.I):
        return False
    words = _words(name)
    if not 2 <= len(words) <= 3:
        return False
    return all(w[:1].isupper() and w[:1].isalpha() for w in words)


class Anonymiser:
    """One consistent mapping, applied everywhere."""

    def __init__(self) -> None:
        self.map: Dict[str, str] = {}
        self._people = iter(PEOPLE)
        self._companies = iter(COMPANIES)
        self._churches = iter(CHURCHES)

    def _next(self, pool, fallback: str) -> str:
        try:
            return next(pool)
        except StopIteration:
            self.map.setdefault("__overflow__", "0")
            count = int(self.map["__overflow__"]) + 1
            self.map["__overflow__"] = str(count)
            return f"{fallback} {count}"

    def stand_in(self, real: str) -> str:
        real = real.strip()
        if not real:
            return real
        key = real.lower()
        if key in self.map:
            return self.map[key]
        # A public product is not somebody's identity, and the sorter has
        # signals that name several of them. Leaving them alone is what keeps
        # the anonymised fixture measuring the same app.
        #
        # Only when the *whole* name is one, though. "Maya from Coursera"
        # and "Ironvale Systems - Workday" each contain a vendor and each
        # also contain somebody's name or employer, and keeping the whole
        # string because of the vendor left both standing.
        if self._is_all_vendor(real):
            self.map[key] = real
            return real
        variants = self._variants(real)
        for variant in variants:
            if variant.lower() in self.map:
                return self.map[variant.lower()]
        if re.search(r"\b(church|parish|lutheran|baptist|methodist|chapel|"
                     r"cathedral|diocese)\b", real, re.I):
            fake = self._next(self._churches, "St Jude's Church")
        elif looks_like_a_person(real):
            fake = self._next(self._people, "Sam Taylor")
        else:
            fake = self._next(self._companies, "Acme")
            # Keep a corporate suffix if the original had one, so the shape of
            # the string a sorter sees does not change.
            suffix = re.search(r"\b(Inc\.?|LLC|Ltd\.?|Corp\.?|Corporation|"
                               r"Group|Careers|Recruiting|Talent Acquisition|"
                               r"Human Resources|Systems|Solutions|Services|"
                               r"Technologies|Bank|Health)\b", real, re.I)
            if suffix:
                fake = f"{fake} {suffix.group(0)}"
        self.map[key] = fake
        # A name written one way in the From line turns up written another
        # way in the subject and the signature. Mapping only the form that
        # was collected leaves the others standing, which is how a real name
        # survived the first run of this.
        for variant in variants:
            self.map.setdefault(variant.lower(), self._shape(fake, variant))
        # And every distinctive word of it on its own. A surname turns up in
        # a signature without its first name, and a one-word trading name
        # turns up in the middle of a sentence; the full-string mapping
        # catches neither, and twenty-five real names survived the second run
        # of this because of it.
        self._map_tokens(real, fake)
        return fake

    def _map_tokens(self, real: str, fake: str) -> None:
        real_words = [w for w in _words(real) if len(w) >= 4
                      and w.lower() not in COMMON
                      and w.lower() not in KEEP]
        fake_words = [w for w in _words(fake) if len(w) >= 4
                      and w.lower() not in COMMON] or ["Redacted"]
        for index, word in enumerate(real_words):
            stand = fake_words[min(index, len(fake_words) - 1)]
            self.map.setdefault(word.lower(), stand)

    @staticmethod
    def _is_all_vendor(real: str) -> bool:
        words = [w.lower() for w in _words(real) if len(w) >= 3]
        meaningful = [w for w in words if w not in COMMON]
        if not meaningful:
            return True
        return all(w in KEEP for w in meaningful)

    @staticmethod
    def _variants(real: str) -> List[str]:
        """The other ways the same name gets written."""
        words = _words(real)
        out = []
        if len(words) == 3 and len(words[1].rstrip(".")) == 1:
            out.append(f"{words[0]} {words[2]}")          # drop a middle initial
        if len(words) >= 2:
            out.append(f"{words[-1]}, {words[0]}")        # "Surname, First"
        return [v for v in out if v.lower() != real.lower()]

    @staticmethod
    def _shape(fake: str, variant: str) -> str:
        """Write the stand-in the same way the variant was written."""
        parts = _words(fake)
        if "," in variant and len(parts) >= 2:
            return f"{parts[-1]}, {parts[0]}"
        return fake

    def rewrite(self, text: str) -> str:
        """Replace every mapped name, longest first so parts do not win."""
        if not text:
            return text
        for real in sorted(self.map, key=len, reverse=True):
            if real == "__overflow__":
                continue
            fake = self.map[real]
            if fake.lower() == real.lower():
                # A protected vendor maps to itself. Rewriting it anyway would
                # change nothing but the capitalisation, and that is enough:
                # link matching is case-sensitive, so "instagram.com" became
                # "Instagram.com" and stopped being recognised.
                continue
            # Word boundaries, so "Ramp" never eats the "ramp" in "ramp up"
            # and a token mapping cannot corrupt the middle of a word.
            text = re.sub(rf"\b{re.escape(real)}\b", fake, text, flags=re.I)
            # Also the run-together form a domain or handle would use.
            squashed = re.sub(r"[^a-z0-9]", "", real)
            if len(squashed) >= 6:
                text = re.sub(re.escape(squashed),
                              re.sub(r"[^A-Za-z0-9]", "", fake),
                              text, flags=re.I)
        return text


#: Names that only ever appear in the prose, never in a From line. A parish
#: writes from the secretary's own address and names itself in the body, so
#: collecting display names alone leaves the church standing - which is how
#: the third run of this still identified one.
_IN_THE_PROSE = (
    # "St John's", "Saint Brendan's" - a dedication, which names a parish
    # exactly.
    re.compile(r"\b(?:St\.?|Saint)\s+[A-Z][a-z]+(?:'s)?\b"),
    # "<Something> Lutheran Church", "<Something> Parish"
    re.compile(r"\b(?:[A-Z][A-Za-z'\-]+\s+){1,3}"
               r"(?:Church|Parish|Chapel|Cathedral|Synagogue|Temple|Mosque)\b"),
    # A denomination carrying a place name in front of it.
    re.compile(r"\b[A-Z][A-Za-z'\-]+\s+"
               r"(?:Lutheran|Baptist|Methodist|Presbyterian|Episcopal|"
               r"Anglican|Catholic|Orthodox|Pentecostal|Adventist)\b"),
)

#: Prose that names somebody who is not the sender - a bereavement notice
#: naming the deceased, their maiden name, their age and the home they died
#: in. This is flagged rather than rewritten.
#:
#: Detecting a person's name in running prose is not something a regular
#: expression does well, and both attempts proved it: an unrestricted version
#: replaced eight hundred "names" including "Software Engineer" and took the
#: fixture from 87% to 57%, and a restricted one rewrote the middle of a
#: scripture verse and still missed the name it was written for. A tool that
#: says "this one needs a person" is worth more than one that quietly does it
#: badly.
_BEREAVEMENT = re.compile(
    r"\b(?:passed away|passed onto|passed into|died peacefully|died on|"
    r"obituary|survived by|n[e\u00e9]e\s|in loving memory|"
    r"celebration of life)\b", re.I)


def collect(rows: List[dict], mapper: Anonymiser) -> None:
    """Learn every name before rewriting anything."""
    for row in rows:
        sender = row.get("sender", "")
        display = sender.split("<")[0].strip()
        if display and "@" not in display:
            mapper.stand_in(display)
    # Names that live in the prose rather than the From line.
    for row in rows:
        text = f"{row.get('subject', '')}\n{row.get('body', '')}"
        for pattern in _IN_THE_PROSE:
            for found in pattern.findall(text):
                mapper.stand_in(found.strip())

    # Domains carry identities too.
    for row in rows:
        text = " ".join([row.get("sender", ""), row.get("body", ""),
                         " ".join(row.get("links", ()) or ())])
        hosts = re.findall(r"(?:@|//)([A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,})",
                           text)
        for host in hosts:
            # Every label, not just the first - "www.stjohnslombard.org" is
            # "www" at the front, and taking that leaves the identity intact.
            for label in host.split("."):
                if len(label) >= 4 and label.lower() not in KEEP \
                        and label.lower() not in COMMON:
                    mapper.stand_in(label)


def needs_a_person(rows: List[dict]) -> List[int]:
    """Rows whose prose names somebody the mapping cannot reach."""
    return [index for index, row in enumerate(rows)
            if _BEREAVEMENT.search(f"{row.get('subject', '')}\n"
                                   f"{row.get('body', '')}")]


def anonymise(rows: List[dict]) -> tuple:
    mapper = Anonymiser()
    collect(rows, mapper)
    out = []
    for row in rows:
        clean = dict(row)
        for field in ("sender", "subject", "body"):
            if field in clean:
                clean[field] = mapper.rewrite(clean[field])
        # Every address becomes a reserved one, whatever it was - including
        # one that turned up as a display name, which is how a redacted
        # address ended up on the front of a company's name.
        clean["sender"] = re.sub(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            "sender@example.example", clean.get("sender", ""))
        clean["sender"] = re.sub(
            r"<[^>]*>", "<sender@example.example>", clean["sender"])
        if "@" in clean["sender"] and "<" not in clean["sender"]:
            clean["sender"] = "sender@example.example"
        clean["body"] = re.sub(
            r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}",
            "someone@example.example", clean.get("body", ""))
        # Links keep their hosts. A Teams invitation, a Calendly page and a
        # Greenhouse portal are products the sorter recognises by name, and
        # blanking them measures a different app - the first run of this
        # turned two interviews into Unclassified by doing exactly that. Only
        # names that were mapped are rewritten, which covers a company's own
        # careers site without touching anybody's vendor.
        if "links" in clean:
            clean["links"] = [mapper.rewrite(u) for u in clean["links"]]
        out.append(clean)
    return out, mapper.map


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="anonymise", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("source")
    parser.add_argument("--out", required=True)
    parser.add_argument("--show-map", action="store_true")
    args = parser.parse_args(argv)

    rows = json.loads(Path(args.source).read_text(encoding="utf-8"))
    cleaned, mapping = anonymise(rows)
    Path(args.out).write_text(json.dumps(cleaned, indent=2) + "\n",
                              encoding="utf-8")
    print(f"{len(rows)} row(s) -> {args.out}")
    print(f"{len([k for k in mapping if k != '__overflow__'])} name(s) replaced")
    flagged = needs_a_person(cleaned)
    if flagged:
        print(f"\n{len(flagged)} row(s) name somebody in the prose and need "
              "reading by a person before this is published:")
        for index in flagged:
            print(f"  row {index}: {cleaned[index].get('subject', '')[:60]}")
    if args.show_map:
        for real, fake in sorted(mapping.items()):
            if real != "__overflow__":
                print(f"  {real:44} -> {fake}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
