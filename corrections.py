"""What the app has learned from being corrected.

Every sorter gets some mail wrong, and the useful question is not whether it
does but whether it does the same one twice. When you drag a message into a
folder the app did not suggest, that is a fact about your mail that no amount
of general-purpose language modelling was going to produce: that
``no-reply@greenhouse.io`` means interviews *to you*, that mail from your
recruiter's personal Gmail is job mail, that the newsletter the sorter keeps
filing under Applications is not one.

This module writes those facts down and reads them back on the next scan.

Two rules govern when a memory is allowed to speak:

**An exact address needs one correction.** If you filed a message from
``jane@acme.com`` into Interviews, the next message from Jane goes to
Interviews. One correction is enough because the address is specific: you
cannot mean anyone else.

**A domain needs two, from two different people.** One correction at
``acme.com`` says something about that person; two, from two colleagues, say
something about the company. Shared mail hosts - Gmail, iCloud, Outlook and
the rest - never learn at the domain level at all, because "the domain" there
is four hundred million strangers.

Corrections are also allowed to change their mind. The learned folder is
always the most recent one, and the strength behind it is how many corrections
in a row agree with it. Correct a sender three times to Applications and once
to Interviews, and the answer is Interviews - immediately, with a strength of
one. Nothing has to be un-taught by hand.

Everything lives in the user's Application Support directory, mode 0600,
alongside their settings. Nothing here ships with the app: the file starts
empty and only ever contains what the person using it put there.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

#: Where the file lives. Resolved lazily so tests can redirect the app support
#: directory without importing this module first.
FILENAME = "corrections.json"

#: How many corrections to keep. Beyond this the oldest go, which is also the
#: right policy for a memory: a folder you stopped using two years ago should
#: not still be pulling mail towards it.
MAX_ENTRIES = 4000

#: Corrections for one key beyond this are not consulted. A run of five
#: agreeing corrections is already as certain as this gets.
RECENT_PER_KEY = 8

#: An exact address is specific enough that one correction settles it.
SENDER_STRENGTH = 1

#: A domain is not. Two corrections, and they must come from two different
#: people at that domain.
DOMAIN_STRENGTH = 2

#: Mail hosts where the domain says nothing about the sender. Learning
#: "everything from gmail.com goes to Applications" would be a disaster, and
#: it is exactly what a naive domain rule would conclude from three
#: corrections in a row.
SHARED_HOSTS = frozenset({
    "aol.com", "btinternet.com", "comcast.net", "cox.net", "email.com",
    "fastmail.com", "fastmail.fm", "free.fr", "gmail.com", "gmx.com",
    "gmx.de", "gmx.net", "googlemail.com", "hey.com", "hotmail.co.uk",
    "hotmail.com", "hotmail.fr", "icloud.com", "inbox.com", "laposte.net",
    "live.co.uk", "live.com", "mac.com", "mail.com", "mail.ru", "me.com",
    "msn.com", "naver.com", "optonline.net", "orange.fr", "outlook.com",
    "outlook.co.uk", "pm.me", "proton.me", "protonmail.com", "qq.com",
    "rediffmail.com", "rocketmail.com", "sbcglobal.net", "seznam.cz",
    "sky.com", "t-online.de", "talktalk.net", "tutanota.com", "verizon.net",
    "virginmedia.com", "web.de", "yahoo.co.jp", "yahoo.co.uk", "yahoo.com",
    "yahoo.fr", "yandex.ru", "ymail.com", "zoho.com", "163.com", "126.com",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _address(raw: str) -> str:
    """Normalise an address for use as a key.

    Case is folded because mail servers do not care, and anything outside a
    plausible address is dropped rather than stored - a malformed From header
    should not become a key that never matches anything again.
    """
    text = (raw or "").strip().lower()
    if text.startswith("<") and text.endswith(">"):
        text = text[1:-1].strip()
    if text.count("@") != 1:
        return ""
    local, _, host = text.partition("@")
    if not local or not host or "." not in host:
        return ""
    return f"{local}@{host.strip('.')}"


def domain_of(address: str) -> str:
    """The host part, with a leading subdomain of a mail robot left in place.

    ``no-reply@mail.greenhouse.io`` keeps ``mail.greenhouse.io`` rather than
    being folded to ``greenhouse.io``. That is deliberate: the two are often
    different systems sending different mail, and the narrower key is the one
    that will not surprise anybody.
    """
    _, _, host = _address(address).partition("@")
    return host


def is_shared_host(domain: str) -> bool:
    """Is this a mail provider rather than an organisation?"""
    domain = (domain or "").lower().strip(".")
    if domain in SHARED_HOSTS:
        return True
    # A subdomain of a shared host is still shared.
    return any(domain.endswith("." + host) for host in SHARED_HOSTS)


@dataclass(frozen=True)
class Correction:
    """One time the user disagreed with where a message was going."""

    sender: str
    folder: str
    #: Where the app wanted to put it. Empty when it wanted to leave it alone,
    #: which is the correction worth the most - a missed message.
    suggested: str = ""
    #: Whether the app called it job-related. Kept so the memory can say
    #: "you have filed four of these, and the sorter called none of them job
    #: mail" rather than only correcting the folder.
    was_job_related: bool = False
    subject: str = ""
    when: str = field(default_factory=_now)

    @property
    def domain(self) -> str:
        return domain_of(self.sender)

    def to_dict(self) -> Dict[str, object]:
        return {
            "sender": self.sender,
            "folder": self.folder,
            "suggested": self.suggested,
            "was_job_related": self.was_job_related,
            "subject": self.subject,
            "when": self.when,
        }

    @classmethod
    def from_dict(cls, raw: object) -> Optional["Correction"]:
        if not isinstance(raw, dict):
            return None
        sender = _address(str(raw.get("sender", "")))
        folder = str(raw.get("folder", "")).strip()
        if not sender or not folder:
            return None
        return cls(
            sender=sender,
            folder=folder,
            suggested=str(raw.get("suggested", "") or ""),
            was_job_related=bool(raw.get("was_job_related", False)),
            subject=str(raw.get("subject", "") or "")[:200],
            when=str(raw.get("when", "") or _now()),
        )


@dataclass(frozen=True)
class Learned:
    """What the memory has to say about one message."""

    folder: str
    #: How many corrections in a row agree.
    strength: int
    #: "address" or "domain" - which key matched.
    scope: str
    #: The key itself, for the settings list and for forgetting.
    key: str
    #: A sentence for the reasoning column. Written here rather than in the
    #: UI so that every place that shows it says the same thing.
    because: str


class Memory:
    """The corrections file, loaded once and consulted per message."""

    def __init__(self, entries: Optional[Sequence[Correction]] = None,
                 path: Optional[Path] = None) -> None:
        self._entries: List[Correction] = list(entries or ())
        self._path = path
        self._index_dirty = True
        self._by_sender: Dict[str, List[Correction]] = {}
        self._by_domain: Dict[str, List[Correction]] = {}

    # -- disk ------------------------------------------------------------
    @staticmethod
    def default_path() -> Path:
        import config  # local: config imports nothing from here, keep it that way
        return config.app_support_dir() / FILENAME

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Memory":
        """Read the file. A damaged or missing file is an empty memory.

        Never raises. A corrections file that cannot be parsed must not stop
        someone scanning their mail - the worst case is that the app forgets
        what it was taught, which is recoverable by teaching it again.
        """
        path = path or cls.default_path()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(path=path)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning("Could not read corrections at %s (%s); starting empty.",
                        path, exc)
            return cls(path=path)
        rows = raw.get("corrections") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            return cls(path=path)
        entries = [c for c in (Correction.from_dict(row) for row in rows) if c]
        return cls(entries, path=path)

    def save(self, path: Optional[Path] = None) -> Path:
        """Atomically write the file, 0600, newest last."""
        path = path or self._path or self.default_path()
        self._path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"version": 1,
             "corrections": [c.to_dict() for c in self._entries[-MAX_ENTRIES:]]},
            indent=2)
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent),
                                        prefix=".corrections-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.write("\n")
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
        return path

    # -- writing ---------------------------------------------------------
    def remember(self, correction: Correction) -> bool:
        """Record one correction. False if there was nothing to learn."""
        if not correction.sender or not correction.folder:
            return False
        if correction.folder == correction.suggested:
            return False  # agreeing with the app teaches nothing
        self._entries.append(correction)
        if len(self._entries) > MAX_ENTRIES:
            del self._entries[:-MAX_ENTRIES]
        self._index_dirty = True
        return True

    def remember_move(self, sender: str, folder: str, *, suggested: str = "",
                      was_job_related: bool = False, subject: str = "") -> bool:
        """Convenience wrapper that normalises the address for the caller."""
        address = _address(sender)
        if not address:
            return False
        return self.remember(Correction(
            sender=address, folder=folder.strip(), suggested=suggested or "",
            was_job_related=was_job_related, subject=subject[:200]))

    def forget(self, key: str) -> int:
        """Drop every correction for one address or domain. Returns the count."""
        key = (key or "").strip().lower()
        if not key:
            return 0
        before = len(self._entries)
        if "@" in key:
            self._entries = [c for c in self._entries if c.sender != key]
        else:
            self._entries = [c for c in self._entries if c.domain != key]
        self._index_dirty = True
        return before - len(self._entries)

    def clear(self) -> None:
        self._entries = []
        self._index_dirty = True

    # -- reading ---------------------------------------------------------
    def __len__(self) -> int:
        return len(self._entries)

    @property
    def entries(self) -> Tuple[Correction, ...]:
        return tuple(self._entries)

    def _index(self) -> None:
        if not self._index_dirty:
            return
        self._by_sender = {}
        self._by_domain = {}
        for entry in self._entries:
            self._by_sender.setdefault(entry.sender, []).append(entry)
            domain = entry.domain
            if domain:
                self._by_domain.setdefault(domain, []).append(entry)
        self._index_dirty = False

    @staticmethod
    def _agreeing_run(entries: Sequence[Correction]) -> Tuple[str, List[Correction]]:
        """The newest folder, and the run of newest corrections choosing it.

        Reading backwards is what lets the memory change its mind: the moment
        a correction disagrees with the ones before it, the run stops there
        and the older ones stop counting.
        """
        recent = list(entries)[-RECENT_PER_KEY:]
        if not recent:
            return "", []
        folder = recent[-1].folder
        run: List[Correction] = []
        for entry in reversed(recent):
            if entry.folder != folder:
                break
            run.append(entry)
        return folder, run

    def lookup(self, sender: str) -> Optional[Learned]:
        """What, if anything, the memory knows about mail from this address."""
        address = _address(sender)
        if not address:
            return None
        self._index()

        folder, run = self._agreeing_run(self._by_sender.get(address, ()))
        if folder and len(run) >= SENDER_STRENGTH:
            times = "once" if len(run) == 1 else f"{len(run)} times"
            return Learned(
                folder=folder, strength=len(run), scope="address", key=address,
                because=f"you have filed mail from {address} here {times}")

        return self._domain_rule(domain_of(address))

    def _domain_rule(self, domain: str) -> Optional[Learned]:
        """The domain-level answer for one domain, if it has earned one."""
        if not domain or is_shared_host(domain):
            return None
        folder, run = self._agreeing_run(self._by_domain.get(domain, ()))
        if not folder or len(run) < DOMAIN_STRENGTH:
            return None
        people = {entry.sender for entry in run}
        if len(people) < 2:
            # One colleague's habit is not the company's. Wait for a second.
            return None
        return Learned(
            folder=folder, strength=len(run), scope="domain", key=domain,
            because=(f"you have filed mail from {len(people)} senders at "
                     f"{domain} here"))

    def summary(self) -> List[Learned]:
        """Everything the memory would act on, for the settings list.

        An address is left out when the rule for its domain already sends mail
        to the same place. Listing both is technically accurate and useless:
        the reader wants to know what the app will do, and the answer for
        everyone at that domain is one line, not four.
        """
        self._index()
        domains = {domain: rule
                   for domain, rule in ((d, self._domain_rule(d))
                                        for d in self._by_domain)
                   if rule is not None}
        learned: Dict[str, Learned] = dict(domains)
        for address in self._by_sender:
            hit = self.lookup(address)
            if hit is None or hit.key in learned:
                continue
            covering = domains.get(domain_of(address))
            if covering is not None and covering.folder == hit.folder:
                continue
            learned[hit.key] = hit
        return sorted(learned.values(),
                      key=lambda item: (-item.strength, item.key))

    def describe(self) -> str:
        """One line for the settings page."""
        if not self._entries:
            return "Nothing learned yet."
        learned = self.summary()
        if not learned:
            return (f"{len(self._entries)} correction(s) recorded, "
                    "none strong enough to act on yet.")
        senders = sum(1 for item in learned if item.scope == "address")
        domains = len(learned) - senders
        parts = []
        if senders:
            parts.append(f"{senders} sender{'s' if senders != 1 else ''}")
        if domains:
            parts.append(f"{domains} domain{'s' if domains != 1 else ''}")
        return f"Filing mail from {' and '.join(parts)} the way you corrected it."


def apply_to(items: Iterable, memory: Memory) -> int:
    """Point each item at its learned folder. Returns how many were changed.

    Only items the user has not already touched by hand are considered, and
    only when the memory disagrees with the app - so this can be run over a
    scan's results without ever overriding a person's own choice.
    """
    changed = 0
    for item in items:
        if getattr(item, "override_folder", None):
            continue
        hit = memory.lookup(item.email.sender_email)
        if hit is None or hit.folder == item.suggested_folder:
            continue
        item.override_folder = hit.folder
        item.learned_from = hit
        changed += 1
    return changed


def counts_by_folder(memory: Memory) -> Counter:
    """How many corrections point at each folder. For the settings summary."""
    return Counter(entry.folder for entry in memory.entries)
