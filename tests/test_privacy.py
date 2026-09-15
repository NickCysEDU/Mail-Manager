"""Nothing personal is allowed to end up in the source tree.

The sorter is tuned against a real inbox, and the shipped lexicon was built
from public data about airports and companies. Both of those are processes
that leak if nobody is watching: a phrase copied out of a real email into a
signal table, an address pasted into a fixture, a domain that turned out to
be somebody's employer rather than a household name.

This file is the watch. It reads every tracked file the app ships and fails
if it finds an address that could belong to a real person.

What counts as "could belong to a real person" is deliberately narrow, and
the two halves of an address are treated differently:

*The domain.* Reserved names - anything under ``.example``, plus ``.test``,
``.invalid`` and ``localhost`` - are safe by construction: RFC 2606 set them
aside so they can never be registered. Single-letter stand-ins like ``b.com``
are obviously nobody. Everything else is a real domain somebody owns.

*The local part.* ``no-reply@`` and ``careers@`` at a real company are public
addresses printed on websites; a first name at the same domain is a person.
That distinction is the one that matters, because the fixtures are built from
real mail and it is the human correspondents in them, not the robots, whose
addresses must never be committed.

If this fails on something genuinely harmless, add it to the allow-lists on
purpose rather than loosening the pattern. Making somebody type the exception
out is the entire point.
"""

from __future__ import annotations

import gzip
import json
import re

import hashlib

#: SHA-256 of handles belonging to whoever built the fixtures. Digests, not
#: strings, so that the repository does not carry the thing it is checking
#: for. A failure names the fixture rather than the handle, for the same
#: reason - the fixture is where the fix goes.
FORBIDDEN_HANDLES = {
    "9285665e35ffb099ebf8efd1babb23207b2fd57f69c5ea21b249bdbcd0ce4581",
    "f315793b62a69f02975fcb56b091e69d7621186b2cc5e8f3c7ae10fa0ef6f471",
    "2ffabac92a962e1a58fd5a389fbc4a354e91153e703e4766f38151aeaa9e5c5f",
}

#: What a handle can be made of, and what separates one from the next.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._+-]*")
_PARTS = re.compile(r"[._+-]")


def _handle_candidates(blob: str):
    """Every handle the blob could be said to contain.

    A handle turns up as a whole token ("lastname"), as one part of a dotted
    one ("first.lastname"), or as a run of parts ("first.last@host" giving
    "first.last"). All three are generated so the digest comparison sees what
    a substring search would have seen.
    """
    for token in _TOKEN.findall(blob):
        yield token
        parts = [p for p in _PARTS.split(token) if p]
        for index, part in enumerate(parts):
            yield part
            for end in range(index + 2, min(index + 4, len(parts)) + 1):
                yield ".".join(parts[index:end])
                yield "_".join(parts[index:end])
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

ADDRESS = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

#: Reserved by RFC 2606 and RFC 6761. These can never belong to anybody.
RESERVED_SUFFIXES = (".example", ".test", ".invalid", ".localhost",
                     ".example.com", ".example.net", ".example.org",
                     ".example.edu")
RESERVED_EXACT = {"example", "test", "invalid", "localhost",
                  "example.com", "example.net", "example.org", "example.edu"}

#: Real domains that appear on purpose: applicant-tracking systems the sorter
#: has to recognise by name, and the project's own.
KNOWN_DOMAINS = {
    "greenhouse.io", "mail.greenhouse.io", "icims.com", "talent.icims.com",
    "myworkday.com", "workday.com", "lever.co", "ashbyhq.com",
    "smartrecruiters.com", "successfactors.com", "taleo.net",
    "anthropic.com", "claude.com", "github.com",
}

#: Local parts that are published addresses rather than people. A company
#: prints these on its website; nobody reads mail sent to them personally.
ROLE_ACCOUNTS = {
    "abuse", "account", "accounts", "admin", "alert", "alerts", "billing",
    "care", "career", "careers", "contact", "customercare", "do-not-reply",
    "do_not_reply", "donotreply", "help", "hello", "hi", "hr", "hr-team",
    "info", "jobs", "mail", "mailer-daemon", "marketing", "news",
    "newsletter", "no-reply", "no_reply", "noreply", "notification",
    "notifications", "opt-out", "postmaster", "recruiting", "recruitment",
    "reply", "sales", "security", "service", "support", "talent", "team",
    "track", "tracking", "notify", "auto-notify", "unsubscribe", "updates",
    "webmaster",
}

#: Mail providers, where the domain proves nothing - the accounts tests have
#: to name them because that is how the app recognises a provider. The local
#: part is what is checked at these, and only these placeholder handles pass:
#: anything that looks like somebody's actual handle fails, which is the
#: whole point, because the author's own mailbox is at one of these.
FREE_MAIL = {
    "aol.com", "fastmail.com", "gmail.com", "gmx.com", "hey.com",
    "hotmail.co.uk", "hotmail.com", "icloud.com", "live.com", "mac.com",
    "mail.com", "me.com", "outlook.co.uk", "outlook.com", "pm.me",
    "proton.me", "protonmail.com", "yahoo.co.uk", "yahoo.com", "zoho.com",
}

PLACEHOLDER_HANDLES = {
    "a", "b", "c", "d", "e", "alice", "bob", "carol", "first", "me", "new",
    "nobody", "other", "padded", "person", "second", "somebody", "someone",
    "spaced", "test", "third", "user", "work", "you",
}

#: Filename suffixes that make an ``@`` a false alarm - ``foo@2x.png`` is an
#: image, not an address.
FILE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".icns",
                 ".ico", ".pdf", ".gz", ".zip", ".py", ".js", ".css", ".html")

EXEMPT = {"tests/test_privacy.py"}


def _reserved(domain: str) -> bool:
    return domain in RESERVED_EXACT or domain.endswith(RESERVED_SUFFIXES)


def _stand_in(local: str, domain: str) -> bool:
    """``a@b.com``, ``x@mail.ryanair.com`` - nobody would mistake these."""
    return len(domain.split(".", 1)[0]) <= 2 or len(local) <= 2


def _role(local: str) -> bool:
    stem = local.split("+", 1)[0].lower()
    return stem in ROLE_ACCOUNTS


def personal_addresses(text: str) -> list:
    """Addresses in this text that could belong to a real person."""
    found = []
    for match in ADDRESS.finditer(text):
        address = match.group(0)
        if address.lower().endswith(FILE_SUFFIXES):
            continue
        local, _, domain = address.partition("@")
        domain = domain.lower().rstrip(".")
        if _reserved(domain) or _stand_in(local, domain):
            continue
        if domain in FREE_MAIL:
            # At a mail provider the domain is meaningless, so the handle has
            # to carry the whole burden of proving it is nobody.
            if local.lower() in PLACEHOLDER_HANDLES:
                continue
            found.append(address)
            continue
        if domain in KNOWN_DOMAINS or _role(local):
            continue
        found.append(address)
    return sorted(set(found))


def tracked(*patterns: str) -> list:
    out = subprocess.run(["git", "ls-files", *patterns], cwd=ROOT,
                         capture_output=True, text=True, check=True)
    return [line for line in out.stdout.splitlines()
            if line and line not in EXEMPT]


ADVICE = ("If it is a placeholder, move it to the reserved .example TLD. If it "
          "is a published company address, add it to KNOWN_DOMAINS or "
          "ROLE_ACCOUNTS in this file, on purpose.")


@pytest.mark.parametrize("relative", tracked("*.py"))
def test_no_personal_addresses_in_code(relative):
    text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
    assert personal_addresses(text) == [], f"{relative}: {ADVICE}"


@pytest.mark.parametrize("relative", tracked("*.md", "*.txt", "*.toml",
                                             "*.json", "*.cfg", "*.yaml",
                                             "*.yml", "*.plist", "*.sh"))
def test_no_personal_addresses_in_data_and_docs(relative):
    text = (ROOT / relative).read_text(encoding="utf-8", errors="replace")
    assert personal_addresses(text) == [], f"{relative}: {ADVICE}"


def test_the_guard_catches_a_real_leak():
    """The pattern above is worth nothing if it never fires."""
    assert personal_addresses("mail from jane.doe@somecompany.co.uk today")
    assert not personal_addresses("mail from jane.doe@somecompany.example")
    assert not personal_addresses("no-reply@greenhouse.io sent this")
    assert not personal_addresses("<img src='banner@2x.png'>")
    # A provider domain proves nothing either way; the handle decides.
    assert personal_addresses("rowan.something@gmail.com")
    assert not personal_addresses("me@gmail.com")


def test_nothing_the_user_teaches_the_app_is_committed():
    import corrections
    assert corrections.FILENAME not in tracked()
    # And the memory is empty until somebody fills it.
    assert len(corrections.Memory()) == 0


# ==========================================================================
# The fixtures, which were built from a real inbox
# ==========================================================================
FIXTURES = ROOT / "tests" / "fixtures"


def _all_fixtures():
    """Public sets, plus the inbox-derived ones when this machine has them."""
    import private_fixtures
    return private_fixtures.every()


def fixture_rows(name: str) -> list:
    import json
    import private_fixtures
    found = private_fixtures.path(name)
    assert found is not None, name
    return json.loads(found.read_text(encoding="utf-8"))


#: Platforms that appear in everybody's mail. The sorter recognises these by
#: name on purpose, and seeing one in a fixture says nothing about whose
#: mailbox it came from, which is the only question this file asks.
PUBLIC_PLATFORMS = {
    "linkedin.com", "indeed.com", "glassdoor.com", "ziprecruiter.com",
    "myworkday.com", "workday.com", "greenhouse.io", "lever.co",
    "icims.com", "taleo.net", "jobvite.com", "bamboohr.com", "ashbyhq.com",
    "smartrecruiters.com", "successfactors.com",
    "apple.com", "microsoft.com", "google.com", "calendly.com", "zoom.us",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "snapchat.com",
    "discord.gg", "youtube.com", "github.com", "aka.ms",
    "ups.com", "fedex.com", "usps.com", "dhl.com", "royalmail.com",
    "paypal.com", "amazon.com", "ebay.com", "substack.com", "eventbrite.com",
}

#: The invented organisations the fixtures are written around. Adding one is
#: how you say "this name is made up" - which is the point: a name that is
#: not on either list fails, and somebody has to look at it.
INVENTED = {
    "acme", "aerodell", "alderfen", "alderton", "ashcombe", "ashford",
    "ashgrove", "bellhaven", "benefitbridge", "benefitspan", "bexley",
    "blackmoor", "bramblewick", "bramblewood", "briarfield", "brightpath",
    "brunohartley", "calderbrook", "calderwood", "carrowdale", "cedarhall",
    "certwell", "chandlers", "chartline", "cipherforge", "clearmont",
    "clearpine", "clearwave", "coldstream", "copperfield", "corvane",
    "corvia", "cranleigh", "cranmore", "cresthill", "darnley", "deepwell",
    "dunhollow", "dunmore", "dunwich", "eastmarch", "ellersby", "elmgrove",
    "elmridge", "elmsworth", "elmwood", "everstead", "fairmead",
    "farlight", "farrowgate", "fenwick", "fernhollow", "foxglove",
    "garrowby", "glenmoor", "granby", "greenhollow", "halstead",
    "harborlight", "harlow", "harpenden", "harrowgate", "hartfield",
    "hazelmere", "highcross", "hirecrest", "hollowbrook", "hollowmead",
    "ironbridge", "ironvale", "kestrelworks", "kingsmere",
    "lakesidedental", "larkfield", "larkhill", "larkspur", "lexingale",
    "lindenway", "longmere", "marchmont", "marchwood", "marlow",
    "marrowby", "meadowbrook", "medbourne", "meridian", "milbourne",
    "netherford", "northbank", "northgate", "northwind", "oakhaven",
    "oakmere", "parcelo", "pendleton", "penrose", "pinecrest", "pipeworks",
    "puffin", "quarrydale", "railhop", "ravenscar", "redmayne",
    "ridgemont", "riverside", "rookhaven", "saltmarsh", "sandbourne",
    "sheffwell", "shorewood", "silverbeck", "skyfare", "skyvale",
    "stonecroft", "stonegate", "streak-link", "summerlee", "sunnymede",
    "sunpoint", "swiftmoor", "tallowmere", "tanfield", "terramap",
    "tesselate", "thornbeck", "thornbury", "thornfield", "tinsoft",
    "tollerton", "underhill", "uppingham", "vanmoor", "vantage", "varley",
    "vaughnly", "vellum", "verifield", "wardlow", "westbrook", "westmoor",
    "whitmore", "willowfen", "windermere", "wolverton", "wraysbury",
    "wrenfield", "yarborough", "yarrow"
}


def registrable(host: str) -> str:
    """The part of a host somebody had to register, roughly."""
    parts = [p for p in host.lower().rstrip(".").split(".") if p]
    if len(parts) >= 3 and parts[-2] in {"co", "com", "org", "ac", "gov", "net"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host.lower()


#: Postcodes that belong to something public and appear in its own boilerplate.
#: Apple's is in every Apple Account email anybody has ever had.
PUBLIC_POSTCODES = {"95014", "90405"}

#: Postcodes made up for the fixtures. Listing them is the point: a postcode
#: that is on neither list is one nobody has vouched for.
INVENTED_POSTCODES = {"41022", "41025", "41088", "41107"}

#: Real places. A fixture may not name one: a town plus a street number is
#: somebody's address, and a region plus an employer names the employer.
REAL_PLACES = {
    "lombard", "downers grove", "naperville", "wheaton", "palatine", "huntley",
    "illinois", "chicago", "milwaukee", "wisconsin", "new york", "rochester",
    "buffalo", "syracuse", "western new york", "finger lakes", "maryland",
    "boston", "massachusetts", "stanford", "menlo park", "san francisco",
    "seattle", "portland", "denver", "atlanta", "houston", "dallas", "austin",
}


@pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
class TestNobodysAddressIsInAFixture:
    """A rejection letter is addressed to somebody, at their house.

    One row in the labelled set carried the owner's surname in capitals and
    the street, town, state and postcode it was posted to. Nothing caught it:
    the address check wanted a street suffix and that line had none, and the
    name check was looking for a different spelling. A postcode is the part
    that cannot be written off as coincidence, so that is what is checked
    here, along with the towns that would place somebody.
    """

    def test_no_postcode_that_is_not_public_boilerplate(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        # Five digits next to a two-letter state, or on its own after a comma,
        # is a postcode rather than a requisition number.
        # "Town, ST 12345" or "Town, Statename 12345". A bare five-digit run
        # is a requisition number far more often than a postcode, so the
        # comma and the place before it are what make this a postcode.
        found = set(re.findall(
            r"\b[A-Z][A-Za-z]+,\s+(?:[A-Z]{2}|[A-Z][a-z]+)\s+(\d{5})\b", blob))
        unexplained = found - PUBLIC_POSTCODES - INVENTED_POSTCODES
        assert not unexplained, (
            f"{name} contains postcode(s) {sorted(unexplained)}. A postcode "
            "belongs to a real place; invent one, or add it to "
            "PUBLIC_POSTCODES if it is a company's own published footer.")

    def test_no_real_town_or_region(self, name):
        import json
        blob = json.dumps(fixture_rows(name)).lower()
        named = sorted(p for p in REAL_PLACES if re.search(
            r"\b" + re.escape(p) + r"\b", blob))
        # Cupertino is allowed for the same reason Apple is.
        named = [p for p in named if p not in {"cupertino"}]
        assert not named, (
            f"{name} names real place(s) {named}. Together with an employer "
            "that identifies the employer; together with a street number it "
            "identifies a person. Invent the geography.")


@pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
class TestNoRealOrganisationIsNamed:
    """Addresses were guarded; the links in the bodies were not.

    That gap is how a fixture kept pointing at the website of an agency one
    person actually applied to - a link host is not an address, so nothing
    looked at it. Every host now has to be reserved, invented on purpose, or
    a platform that appears in everybody's mail.
    """

    def test_every_link_host_is_accounted_for(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        unknown = set()
        for host in re.findall(r"https?://([A-Za-z0-9.\-]+)", blob):
            host = host.lower().rstrip(".")
            if _reserved(host) or host.endswith(RESERVED_SUFFIXES):
                continue
            base = registrable(host)
            if base in PUBLIC_PLATFORMS:
                continue
            if any(word in host for word in INVENTED):
                continue
            unknown.add(host)
        assert not unknown, (
            f"{name} links to hosts that are neither reserved, invented nor "
            f"public platforms: {sorted(unknown)}. If the name is made up, add "
            f"it to INVENTED; if it is real, it does not belong in a fixture.")


@pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
class TestNoRealMailIsCommitted:
    """The labelled set was built from one person's inbox.

    That is what makes it worth measuring against - hand-written samples do
    not contain what actually breaks a sorter - and it is why every name in
    it had to go before this repository could be public. tools/anonymise.py
    does that; these are the standing checks that it stayed done.
    """

    def test_every_address_is_reserved(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        for address in ADDRESS.findall(blob):
            domain = address.split("@", 1)[1].lower().rstrip(".")
            assert domain.endswith((".example", ".invalid")) \
                or domain in RESERVED_EXACT or domain in KNOWN_DOMAINS, address

    def test_no_telephone_numbers(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        assert not re.search(r"\b\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}\b", blob)

    def test_the_owner_is_not_named(self, name):
        """Whoever built a fixture should not be identifiable from it.

        The handles are compared as digests because this file is public.
        Writing them down here would publish exactly what the check exists
        to keep out of the repository, which is a strange way to test it.
        """
        import json
        blob = json.dumps(fixture_rows(name)).lower()
        found = FORBIDDEN_HANDLES & {
            hashlib.sha256(c.encode()).hexdigest()
            for c in _handle_candidates(blob)}
        assert not found, f"a handle belonging to the fixture's owner is in {name}"

    def test_no_bereavement_record_survives(self, name):
        """A parish notice names the dead, their family and where they died.

        tools/anonymise.py flags these rather than rewriting them, because
        finding a person's name in running prose is not something a regular
        expression does well. The two in the labelled set were rewritten by
        hand; this is the check that no new one arrives unread.
        """
        import sys
        sys.path.insert(0, str(ROOT / "tools"))
        import anonymise
        rows = fixture_rows(name)
        flagged = anonymise.needs_a_person(rows)
        for index in flagged:
            body = rows[index].get("body", "")
            # A hand-written stand-in keeps the vocabulary and drops the
            # record: no maiden name, no home, no list of relatives.
            assert "nee " not in body.lower(), (name, index)
            assert not re.search(r"\bage \d{1,3}, of [A-Z]", body), (name, index)


# ==========================================================================
# What reaches the log, and what SECURITY.md promises does not
# ==========================================================================
class TestNothingSensitiveIsLogged:
    """SECURITY.md makes two promises about the log. These are them.

    A promise in a security document that nothing checks is a promise about
    the day it was written.
    """

    def _captured(self, run) -> str:
        import io
        import logging
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        root = logging.getLogger()
        root.addHandler(handler)
        level = root.level
        root.setLevel(logging.DEBUG)
        try:
            run()
        finally:
            root.removeHandler(handler)
            root.setLevel(level)
        return buf.getvalue()

    def test_a_failed_login_never_logs_the_password(self):
        import imap_engine
        secret = "abcd-efgh-ijkl-mnop"

        def run():
            engine = imap_engine.IMAPEngine(host="127.0.0.1", port=1)
            try:
                engine.connect("you@icloud.example", secret)
            except Exception as exc:      # noqa: BLE001 - the point of the test
                assert secret not in str(exc)

        assert secret not in self._captured(run)

    def test_a_failed_provider_call_never_logs_the_key(self):
        import providers
        key = "sk-ant-not-a-real-key-000000000000"

        def run():
            try:
                backend = providers.provider_class("anthropic")(
                    api_key=key, model="claude-opus-5",
                    base_url="http://127.0.0.1:1")
                backend.complete(prompt="hello", schema=None)
            except Exception as exc:      # noqa: BLE001 - the point of the test
                assert key not in str(exc)

        assert key not in self._captured(run)

    def test_a_rule_failure_never_logs_the_subject(self):
        """The one line that used to name the message it failed on."""
        import autoreply
        import workers
        from config import Settings
        from models import (Category, Classification, EmailMessage, FolderPlan,
                            OtherCategory, TriageItem)

        subject = "A Very Distinctive Subject About A Private Matter"
        item = TriageItem(
            email=EmailMessage(uid="1", subject=subject,
                               sender_email="somebody@acme.example",
                               body_text="A very distinctive body sentence."),
            classification=Classification(
                summary="s", is_job_related=True, category=Category.INTERVIEW,
                other_category=OtherCategory.NOT_APPLICABLE,
                confidence_score=0.9, reasoning="r", model="t"),
            folders=FolderPlan())
        settings = Settings()
        settings.set_rules([autoreply.Rule(
            name="explodes", enabled=True,
            conditions=[autoreply.Condition(field="anywhere",
                                            operator="contains", value="a")],
            actions=[autoreply.Action("draft", "hello")])])

        original = autoreply.apply_rules

        def boom(*_a, **_k):
            raise RuntimeError("a rule with a bad regex")

        def run():
            autoreply.apply_rules = boom
            try:
                workers.ReplyWorker(settings=settings, mailbox_password="",
                                    api_key="", items=[item]).run()
            finally:
                autoreply.apply_rules = original

        logged = self._captured(run)
        assert "rule failed" in logged, "the failure should still be reported"
        assert subject not in logged
        assert "distinctive body sentence" not in logged


class TestNothingSensitiveCanBeCommitted:
    """The second lock on the door.

    The app writes its data into ~/Library/Application Support and the tuning
    tool refuses to write inside the repository at all. But ICLOUD_TRIAGE_HOME
    can point anywhere, and somebody who points it at "." should not be one
    `git add .` away from publishing their own inbox.
    """

    @pytest.mark.parametrize("name", [
        "settings.json",          # what is configured, and every address
        "corrections.json",       # every sender corrected, and where to
        "verdicts.json",          # a verdict per message, by mailbox and UID
        "agent-status.json",
        "triage.log",
        "secrets.txt",
        "private.pem",
        "signing.key",
        "developer.p12",
        "profile.mobileprovision",
        ".env",
        ".env.local",
        "my-set.json",            # what tools/tune.py --write produces
        "inbox-2026.json",
    ])
    def test_it_is_ignored(self, name, tmp_path):
        """git check-ignore, rather than reading the file and hoping.

        gitignore has no trailing comments - a "#" after a pattern becomes
        part of the pattern - and two of these matched nothing at all until
        this test was written.
        """
        path = ROOT / name
        existed = path.exists()
        if not existed:
            path.touch()
        try:
            out = subprocess.run(["git", "check-ignore", "-v", name],
                                 cwd=ROOT, capture_output=True, text=True)
            assert out.returncode == 0, f"{name} is not ignored"
        finally:
            if not existed:
                path.unlink()

    def test_the_log_directory_is_ignored(self, tmp_path):
        directory = ROOT / "Logs"
        existed = directory.exists()
        if not existed:
            directory.mkdir()
        try:
            out = subprocess.run(["git", "check-ignore", "-v", "Logs/"],
                                 cwd=ROOT, capture_output=True, text=True)
            assert out.returncode == 0
        finally:
            if not existed:
                directory.rmdir()

    def test_nothing_sensitive_is_tracked_right_now(self):
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT,
                                 capture_output=True, text=True).stdout.split()
        bad = [f for f in tracked
               if re.search(r"(^|/)(settings|corrections|verdicts|"
                            r"agent-status)\.json$|\.(pem|key|p12|cer|env|"
                            r"log|mobileprovision)$", f)]
        assert bad == [], bad


def test_the_lexicon_holds_no_addresses():
    """The shipped world knowledge is domains and place names, nothing else."""
    path = ROOT / "data" / "lexicon.json.gz"
    if not path.exists():
        pytest.skip("lexicon.json.gz is not built in this checkout")
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        blob = json.load(handle)
    assert "@" not in json.dumps(blob), "the lexicon must contain no addresses"


class TestTheFixturesDoNotReadAsMachineOutput:
    """An anonymiser leaves tells, and tells are their own kind of leak.

    "Acme 47" is not a name anybody's inbox contains, and a doubled prefix
    like "St. St Alban's" is a rewrite that ran twice. Both say the corpus
    was processed, and both were in the published fixtures.
    """

    @pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
    def test_no_numbered_placeholder_companies(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        # A job title with a number in it is ordinary ("Engineer 3"). A
        # filler company word with a number stuck on it is not a name, it is
        # the anonymiser counting.
        suspects = sorted(set(re.findall(
            r"\b(?:Acme|Company|Corp|Corporation|Employer|Firm|Organisation|"
            r"Organization|Business|Vendor|Client|Placeholder)\s+\d{1,3}\b",
            blob, re.I)))
        assert not suspects, f"{name}: numbered placeholder names: {suspects}"

    @pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
    def test_no_doubled_proper_nouns(self, name):
        """"Dear Alex Alex" and "St. St Alban's" both shipped."""
        import json
        blob = json.dumps(fixture_rows(name))
        doubled = sorted(set(re.findall(r"\b([A-Z][a-zA-Z.']{1,14})\s+\1\b", blob)))
        assert not doubled, f"{name}: a rewrite ran twice over: {doubled}"


#: Months and weekdays, which legitimately sit next to a number.
_CALENDAR_WORDS = {
    "January","February","March","April","May","June","July","August",
    "September","October","November","December","Jan","Feb","Mar","Apr",
    "Jun","Jul","Aug","Sep","Sept","Oct","Nov","Dec","Monday","Tuesday",
    "Wednesday","Thursday","Friday","Saturday","Sunday","Mon","Tue","Wed",
    "Thu","Fri","Sat","Sun",
}

#: Organisations that were in the fixtures and should not come back, as
#: digests of their normalised names. Digests because the last version of
#: this list wrote every one of them out in full, in a public repository -
#: a denylist of real employers is a record of where somebody applied, which
#: is the thing it exists to remove. Same reason the owner's handles above
#: are hashed. Universal consumer platforms are deliberately not here: the
#: sorter needs them and everybody's mail has them. Nor are the
#: anonymiser's own stand-ins, which are invented and may legitimately
#: come back the next time tools/anonymise.py runs.
FORBIDDEN_ORGANISATIONS = {
    "ff74877a49f7202b4100be1464d6f191df378325192c3c5d61ea323b4da72e81",
    "d6db21ddecbbd0eeccb901c7fae837ca86cec4c289d7784e9a7016855f10859b",
    "5caba80563a416add5b1775fe18528c550d2254fe6a8098a554bfd88c2ed1ff9",
    "79fc1f0e250704ed6bfc82c449b02547bc17d9db96617cfabe246e8012ce5793",
    "b191c7669de838e5f429cb547c85a08f27c671f9cf345a5193612e2b5ccf9a7e",
    "17ed0451a7d0fc0a5b94a4e50d57ea826f6224e3d4e5c279e10e5d6504f2802c",
    "4f5d81bfe8c86d6d30f6e4f7f3d741bd7a33410212d25bd0ac470c5465007c0c",
    "3690e206cbe51b20ccb089d4deb34ae74e375f594e6a9c4a966f1dc8142f084f",
    "40dcf911c9c231b6cce3d14233a3c5684e4f916d9d33f69ec9eff03204e94ed3",
    "f839701fa153ba45924dc3ae9bb0972bb126ad5387fc171bc09f985d257d66dc",
    "6b1b9e9b25fb34ff48d2bfe6f7ce90789335e29a17ae8d8ec5fb84016f3f41ae",
    "0aef941fe6c5b5dda1204940b273985277e511d1686631cff1ce4a169ca886df",
    "9a89de5d07fec2754fc2f7f3bda6da821f60d523d03d895de18dd97c8d618fc3",
    "9b89025ce7a6d932b28f6e15132a70d402f723874a425e9b4c7cc3b179fa66ce",
    "24cfb44d899764efd72b7f4ca9822f9f96d5c82d7811f4abd1a838b792ac4dbf",
    "784109ed21a6f7c669eaa3d404cb2cd6ce91f3334b836bc7021b0fee26af26ed",
    "d254fa846ea7252ec95bf229075e9ac6bda6f8944f5e445e589a4d54184c931d",
    "0a50c50f4e6ef1208e4508a0a84ecb98ec1bc2ce2bbbb1d96f5b1dcf834dab34",
    "6f3d359b22fc37936263e600ce63cde96474ee3fadcc5c75350c20fdcf25cfc7",
}

#: The longest forbidden name, in words. Phrases up to this length are hashed.
_LONGEST_NAME = 4


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]+", " ", text.lower())).strip()


def _phrases(blob: str):
    """Every run of up to four words in the blob, normalised."""
    words = _normalise(blob).split()
    for size in range(1, _LONGEST_NAME + 1):
        for start in range(len(words) - size + 1):
            yield " ".join(words[start:start + size])


class TestNoRealEmployerSurvives:
    """Which companies somebody applied to is the shape of their year."""

    @pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
    def test_no_real_organisation_is_named(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        seen = {hashlib.sha256(p.encode()).hexdigest() for p in _phrases(blob)}
        assert not (FORBIDDEN_ORGANISATIONS & seen), (
            f"{name} names a real organisation that was removed from the "
            "corpus once already")


class TestAProviderCannotEchoMailIntoTheLog:
    """A 4xx body can quote the request, and the request carries the email.

    The excerpt belongs on screen, where the reader already has the mail. In
    a log file it outlives the scan, and SECURITY.md promises message bodies
    are never written there.
    """

    def test_the_log_form_drops_the_server_text(self):
        import providers

        class Rejected(Exception):
            pass

        leaked = ("invalid request: Dear Rowan, thanks for applying to "
                  "Harborlight, your interview is Tuesday at 3pm")
        failure = Rejected(leaked)
        failure.status_code = 400
        logged = providers.for_the_log(failure)
        assert "Rowan" not in logged
        assert "interview" not in logged
        assert "Harborlight" not in logged
        assert "400" in logged and "Rejected" in logged

    def test_without_a_status_it_is_still_only_the_type(self):
        import providers

        assert providers.for_the_log(ValueError("body text here")) == "ValueError"

    def test_the_batch_failure_path_uses_it(self):
        """The one call site that logs a provider exception."""
        import inspect

        import llm_engine

        source = inspect.getsource(llm_engine)
        assert "Classification failed for" in source
        window = source[source.index("Classification failed for") - 200:
                        source.index("Classification failed for") + 260]
        assert "for_the_log" in window, (
            "the batch failure log line is back to formatting the exception")
