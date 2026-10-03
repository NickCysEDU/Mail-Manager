"""Nothing personal may end up in the source tree.

The sorter is tuned against real mail and the shipped lexicon was built from
public data, and both can leak: a phrase copied from a real email into a
signal table, an address pasted into a fixture, a domain that is somebody's
employer rather than a household name. This file reads every tracked file
the app ships and fails on any address that could belong to a real person.

The two halves of an address are judged differently.

*The domain.* Reserved names (anything under ``.example``, plus ``.test``,
``.invalid`` and ``localhost``) are safe by construction: RFC 2606 set them
aside. Single-letter stand-ins like ``b.com`` are nobody. Everything else is
a real domain somebody owns.

*The local part.* ``no-reply@`` and ``careers@`` at a real company are
published addresses; a first name at the same domain is a person, and a
person's address must never be committed.

If this fails on something harmless, add it to an allow-list on purpose
rather than loosening the pattern.
"""

from __future__ import annotations

import gzip
import json
import re

import hashlib

#: SHA-256 of handles that must never appear: digests, so the repository does
#: not carry what it checks for. A failure names the fixture rather than the
#: handle, for the same reason.
FORBIDDEN_HANDLES = {
    "9285665e35ffb099ebf8efd1babb23207b2fd57f69c5ea21b249bdbcd0ce4581",
    "f315793b62a69f02975fcb56b091e69d7621186b2cc5e8f3c7ae10fa0ef6f471",
    "2ffabac92a962e1a58fd5a389fbc4a354e91153e703e4766f38151aeaa9e5c5f",
}

#: What a handle can be made of, and what separates one from the next.
_TOKEN = re.compile(r"[a-z0-9][a-z0-9._+-]*")
_PARTS = re.compile(r"[._+-]")


def _handle_candidates(blob: str):
    """Every handle the blob could be said to contain: a whole token, one part
    of a dotted one, or a run of parts, so the digest comparison sees what a
    substring search would.
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

from conftest import git_check_ignore, git_lines

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

#: Local parts that are published addresses rather than people.
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

#: Mail providers, where the domain proves nothing: tests name them because the
#: app recognises a provider by them. At these the local part is checked, and
#: only these placeholder handles pass.
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
            # to prove it is nobody.
            if local.lower() in PLACEHOLDER_HANDLES:
                continue
            found.append(address)
            continue
        if domain in KNOWN_DOMAINS or _role(local):
            continue
        found.append(address)
    return sorted(set(found))


def tracked(*patterns: str) -> list:
    return [line for line in git_lines("ls-files", *patterns)
            if line not in EXEMPT]


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


#: Platforms that appear in everybody's mail. The sorter recognises them by
#: name, and one in a fixture says nothing about whose mailbox it came from.
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

#: The invented organisations the fixtures are written around. Adding one says
#: the name is made up; a name on neither list fails until somebody looks at
#: it.
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

#: Postcodes made up for the fixtures. A postcode on neither list is one nobody
#: has vouched for.
INVENTED_POSTCODES = {"41022", "41025", "41088", "41107"}

#: Real places, as digests of their normalised names, for the same reason as
#: the handles. A fixture may not name one: a town and a street number is
#: somebody's address, and a region and an employer names the employer.
REAL_PLACES = {
    "0a50c50f4e6ef1208e4508a0a84ecb98ec1bc2ce2bbbb1d96f5b1dcf834dab34",
    "198becaf9c45016fec5d9bcd2e8d748de6b44a26cd4cc35ea72b670e665dff79",
    "1a2290470e0aa7549ab1e04b2453274374149ffee517a57715e5206e4142c233",
    "2b7b5dd4587e8545cc153c9c739bef276f32afe028ab2e46e73087d6a2c1eb32",
    "3151a8f227c0e11fd9a7fd1aa24ebfee734503dea095c22c3b2fae09d62eeb25",
    "35439e40a0dcce876f9885ccba67769b4b3f021659ebfe7cd6b38261c848811e",
    "3c66157844fa8ce7e9b67b0022383d7709ba2b30f8306d3c9b2eceb2cd91e4dc",
    "402eed114f0a583fb72bce76196539c9a25688cc8840c7fa44d54f811ac5ea32",
    "484f4c1577130fdb27d8c586d3033e777695750da8bbd3d4f9c592e60152a426",
    "4c6eb87b502e3e019acbd4b1e579bd1566104abc0914f5186df63e4833c993c2",
    "52c279ad597187db0cdc6246fd652bfd0ad9b299bfae17faca29906fe3523a6a",
    "56fe43f748e258de06b4955e2b8978bbc1c28ff9ba53917387d6dca8fb920018",
    "6aa006809ea4f9c949f90b00bfd937a1d3ab3045e17a6a0907d66f3007b25df3",
    "6f3d359b22fc37936263e600ce63cde96474ee3fadcc5c75350c20fdcf25cfc7",
    "701392d6e9b9065cd6b1a0bdce93e0e5b6c07bf28349560408cd07f2510143ce",
    "81b8c84b83a8f5dbd68b02528503bae8629bb13726eab5e68612014d21a9f78c",
    "8400a073ca06ffa7c07cd46c7aedddec4db916dd0b32ecf78e279be88e4a02ec",
    "8818439acbcf3df08a17b89b37053f0f0aa399150df725274364d597bcdf4116",
    "a2470c9d137c1c5d3567d1180a64cb43a9269c4d6f1ff13ac8cdbaf6fc5df3b7",
    "ba06d6c4c9d0191b41ff3759d13d94ff5778256d35b532bf48b7d9b067952135",
    "bd732730bd39834d83bf92a114960180d3bd4a6f1309307165e6f30ed9846fdd",
    "c4fdd12ee15f8fbd050c6083a70d4f7191b35cb23a1284f5ebf7c7bd6288f91a",
    "c7c1319276e936c8d64f1d5ed80cd8a0cf54e6dea7b0125533eb4163e03a2c11",
    "d3ecc5b7fe38ffd3397473362f2c42321fb82deb23083ed13cf6f20320ab6c92",
    "de19c97d557a0e8a8cb2eb074915a22154e5f7606bd248aaf4ae6d24782f1409",
    "e82ff084c039c952d986b0844bf67733dbcc03e1f32c094bedb9cda01c534015",
    "ed2891817314563f01329d59e48b1b7f3bfc3efc568945b217591fd074148c48",
    "f49c6320e08eb5ed523dc99e8c512888e2718ec6020201997d01b41754a61502",
    "fa2115f8d576a6ab722956697fc759c31d1cd6b93c8336bfebf73ed5cba2ff49",
}


@pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
class TestNobodysAddressIsInAFixture:
    """A letter is addressed to somebody, at their house. A postcode cannot be
    written off as coincidence, so it is checked here, with the towns that
    would place somebody.
    """

    def test_no_postcode_that_is_not_public_boilerplate(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        # "Town, ST 12345" or "Town, Statename 12345". A bare five-digit run is
        # a requisition number far more often than a postcode, so the comma and
        # the place before it are what make this a postcode.
        found = set(re.findall(
            r"\b[A-Z][A-Za-z]+,\s+(?:[A-Z]{2}|[A-Z][a-z]+)\s+(\d{5})\b", blob))
        unexplained = found - PUBLIC_POSTCODES - INVENTED_POSTCODES
        assert not unexplained, (
            f"{name} contains postcode(s) {sorted(unexplained)}. A postcode "
            "belongs to a real place; invent one, or add it to "
            "PUBLIC_POSTCODES if it is a company's own published footer.")

    def test_no_real_town_or_region(self, name):
        import json
        blob = json.dumps(fixture_rows(name))
        named = sorted(phrase for phrase in set(_phrases(blob))
                       if hashlib.sha256(phrase.encode()).hexdigest()
                       in REAL_PLACES)
        assert not named, (
            f"{name} names real place(s) {named}. Together with an employer "
            "that identifies the employer; together with a street number it "
            "identifies a person. Invent the geography.")


@pytest.mark.parametrize("name", sorted(p.name for p in _all_fixtures()))
class TestNoRealOrganisationIsNamed:
    """The hosts of links in the bodies, not only addresses: a link can point
    at the website of somewhere a person applied. Every host has to be
    reserved, invented on purpose, or a platform in everybody's mail.
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
    """The labelled set came from real mail, which is what makes it worth
    measuring against, and every name in it had to go before this repository
    could be public. tools/anonymise.py does that; these check it stayed
    done.
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
        """Whoever built a fixture must not be identifiable from it. The
        handles are compared as digests because this file is public.
        """
        import json
        blob = json.dumps(fixture_rows(name)).lower()
        found = FORBIDDEN_HANDLES & {
            hashlib.sha256(c.encode()).hexdigest()
            for c in _handle_candidates(blob)}
        assert not found, f"a handle belonging to the fixture's owner is in {name}"

    def test_no_bereavement_record_survives(self, name):
        """A death notice names the dead, their family and where they died.

        tools/anonymise.py flags these rather than rewriting them, since
        finding a name in running prose is not a job for a regular
        expression; this checks that no new one arrives unread.
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
    """SECURITY.md makes two promises about the log; these check them."""

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

    The app writes its data under ~/Library/Application Support and the
    tuning tool refuses to write inside the repository, but
    ICLOUD_TRIAGE_HOME can point anywhere, and pointing it at "." must not
    leave an inbox one `git add .` away from public.
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
        """git check-ignore, rather than reading the file: gitignore has no
        trailing comments (a "#" after a pattern becomes part of it), and
        two of these once matched nothing.
        """
        path = ROOT / name
        existed = path.exists()
        if not existed:
            path.touch()
        try:
            assert git_check_ignore(name), f"{name} is not ignored"
        finally:
            if not existed:
                path.unlink()

    def test_the_log_directory_is_ignored(self, tmp_path):
        directory = ROOT / "Logs"
        existed = directory.exists()
        if not existed:
            directory.mkdir()
        try:
            assert git_check_ignore("Logs/")
        finally:
            if not existed:
                directory.rmdir()

    def test_nothing_sensitive_is_tracked_right_now(self):
        tracked = git_lines("ls-files")
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
    """An anonymiser leaves tells, and tells are a leak of their own.

    "Acme 47" is no real name, and a doubled prefix like "St. St Alban's" is
    a rewrite that ran twice. Both say the corpus was processed.
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
        """A rewrite that ran twice: "Dear Alex Alex", "St. St Alban's"."""
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

#: Organisations that must not come back, as digests of their normalised names:
#: a list of real employers would record where somebody applied, which is what
#: it exists to remove. Universal consumer platforms are not here (the sorter
#: needs them), nor are the anonymiser's invented stand-ins, which may come
#: back when tools/anonymise.py runs again.
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

    On screen that is fine; a log file outlives the scan, and SECURITY.md
    promises message bodies are never written there.
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


class TestNoEvaluationDataIsTracked:
    """Ignored is not the same as untracked: git mv stages a file at its new
    path, ignore rules or not.
    """

    @staticmethod
    def _tracked():
        return git_lines("ls-files")

    def test_no_fixture_json_is_tracked(self):
        tracked = self._tracked()
        offenders = [f for f in tracked
                     if f.startswith("tests/fixtures/") and f.endswith(".json")]
        assert not offenders, (
            f"evaluation data is tracked: {offenders}. It is ignored, which "
            "does nothing once a path is in the index - git rm --cached it.")

    def test_the_private_directory_is_ignored(self):
        probe = FIXTURES / "private" / "labelled.json"
        assert git_check_ignore(probe), (
            "tests/fixtures/private is not ignored; a stray add would publish "
            "a hundred real messages")

    def test_the_resolver_knows_which_sets_are_private(self):
        import private_fixtures

        assert set(private_fixtures.PRIVATE) == {
            "labelled.json", "acknowledgements.json", "adversarial.json",
            "holdout.json", "meetings.json"}


class TestNobodyElsesMediaIsPublished:
    """Music and pictures dropped in the working folder to test against: not
    credentials or personal data, but nobody's to give away.
    """

    #: Things somebody would reasonably drop in the folder while working:
    #: music, pictures, documents, mail, archives.
    DROPPED = [
        # Sound and pictures
        "Some Album - Track 01.mp3", "recording.wav", "loop.aif",
        "take.aiff", "master.flac", "voice.m4a", "stem.ogg", "cut.opus",
        "reference.jpg", "photo.jpeg", "grab.heic", "sketch.gif",
        "scan.tif", "shot.webp", "clip.mp4", "screen.mov", "take.m4v",
        # PNG too: ./dev playtest --save writes out a frame of the scene while
        # somebody's music plays, a picture of their music.
        "reference.png", "frame.png", "rider.png",
        # Documents
        "CV.pdf", "offer letter.docx", "notes.rtf", "budget.xlsx",
        "contacts.csv", "deck.pptx", "plan.pages", "figures.numbers",
        "minutes.odt",
        # Mail, calendars and contacts
        "saved.eml", "archive.mbox", "message.emlx", "outlook.msg",
        "backup.pst", "invite.ics", "people.vcf",
        # Archives, which hide what is inside them
        "inbox.zip", "dump.tar", "old.tgz", "things.7z", "stuff.rar",
        # Databases
        "mail.sqlite", "cache.db",
        # Keys and credentials beyond the ones already covered
        "apple.p8", "bundle.pfx", "store.jks", "id_rsa", "id_ed25519",
        ".netrc", "credentials.json", "client_secret_123.json",
        "service-account-prod.json",
        # Crash reports, which carry paths and sometimes memory
        "Python-2026-01-01-000000.ips", "app.crash",
        # Leftovers
        "config.bak", "notes.orig", ".DS_Store",
        # And wherever they land
        "tests/MP3_Section Whatever.mp3",
        "tests/fixtures/sample.mp3",
        "tools/last frame.png",
        "docs/secret plans.pdf",
        "tools/inbox.mbox",
    ]

    @pytest.mark.parametrize("name", DROPPED)
    def test_it_is_ignored_wherever_it_lands(self, name):
        path = ROOT / name
        existed = path.exists()
        if not existed:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        try:
            assert git_check_ignore(name), f"{name} would be committed"
        finally:
            if not existed:
                path.unlink()

    def test_nothing_of_that_kind_is_tracked_right_now(self):
        tracked = git_lines("ls-files")
        bad = [f for f in tracked
               if re.search(r"\.(mp3|m4a|aac|wav|aif|aiff|flac|ogg|oga|"
                            r"opus|wma|mp4|mov|m4v|avi|mkv|jpg|jpeg|heic|"
                            r"heif|gif|bmp|tif|webp|pdf|docx?|rtf|odt|"
                            r"pages|numbers|xlsx?|ods|pptx?|csv|tsv|eml|"
                            r"emlx|mbox|mbx|msg|pst|ost|olm|ics|vcf|zip|"
                            r"tar|tgz|7z|rar|sqlite3?|db|p8|pfx|jks|"
                            r"keystore|ppk|kdbx|keychain|ips|crash|bak|"
                            r"orig)$", f, re.IGNORECASE)]
        assert bad == [], f"in the repository: {bad}"

    def test_the_icons_and_documentation_pictures_still_ship(self):
        """The rule is broad, so the things that are meant to be there
        have to be excepted - and checked, or the next person to add an
        icon finds it silently missing from the build."""
        tracked = set(git_lines("ls-files"))
        for wanted in ("assets/icon.png", "assets/dmg-background.png",
                       "docs/screenshot.png", "docs/assets/favicon-32.png"):
            assert wanted in tracked, f"{wanted} stopped being committed"

    def test_nothing_enormous_is_tracked(self):
        """Whatever it is, a big binary in a source repository is
        something nobody meant to put there."""
        big = []
        for name in git_lines("ls-files"):
            path = ROOT / name
            if path.is_file() and path.stat().st_size > 4 * 1024 * 1024:
                big.append((name, path.stat().st_size // 1024 // 1024))
        assert not big, f"files over 4 MB: {big}"
