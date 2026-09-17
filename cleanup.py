"""Clearing out a mailbox without reading it first.

Deleting a few thousand messages is the one job an email client is reliably
bad at. The usual shape of it is: scroll, select a page, delete, wait, scroll
again - and the waiting is not the user being slow, it is one network round
trip per message. Five thousand messages is five thousand round trips, and
that is why it takes an afternoon.

Two things make that fast, and this module is the first of them.

**Let the server do the finding.** An IMAP server already has an index of who
sent what and when. ``UID SEARCH FROM "noreply@shop.example" BEFORE
01-Jan-2026`` is one command, and it answers in about the same time whether
the mailbox holds two hundred messages or two hundred thousand. The
alternative - downloading every header and filtering here - is the thousands
of round trips again, just moved somewhere less visible. Nothing in this file
fetches a message body, and nothing needs the mailbox to have been scanned.

**Then delete in batches**, which :meth:`imap_engine.IMAPEngine.delete_uids`
does: a hundred UIDs per ``STORE``, then ``UID EXPUNGE``. Five thousand
messages is around a hundred commands rather than five thousand.

The other half of the problem is *deciding* what to delete, and that is what
:func:`suggest` is for. It reads rows the app has already scanned - no network
at all - and groups them into the handful of "you have four hundred of these
and have never wanted one" piles that account for most of a full mailbox.
Suggestions are only ever suggestions: each one comes back with its count and
the criteria it would use, and nothing is deleted until somebody has seen the
number and said yes to it.

Three rules hold everywhere in here, because deleting mail is not undoable:

**Empty criteria never match.** :meth:`Criteria.is_armed` is false when
nothing has been asked for, and every caller refuses to run an unarmed one.
"Delete everything in this folder" is a different, explicit operation
(``empty_folder``), so that it can never be something you arrived at by
clearing a text box.

**Count before delete, always.** The count is a separate SEARCH against the
same criteria, so the number in the confirmation is the server's answer to
the same question, not an estimate made here.

**Job mail is never suggested.** Not by sender, not by age, not in bulk. The
whole point of the app is that some of this mail matters, and a suggestion
engine that offers to bin an interview invitation is worse than no suggestion
engine.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

__all__ = [
    "Criteria",
    "Suggestion",
    "imap_date",
    "quote_search",
    "suggest",
]

#: Months as IMAP spells them. Not ``strftime("%b")``: that follows the
#: locale, and a server handed ``01-janv.-2026`` rejects the whole command.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

#: Kinds of non-job mail that pile up and are safe to offer. Finance,
#: security and receipts are deliberately absent: people need those later,
#: and "I deleted my bank's fraud alert" is not a recoverable mistake.
DISPOSABLE = frozenset({
    "NEWSLETTER", "PROMOTION", "SOCIAL", "SPAM",
})

#: A sender has to have sent at least this many before it is worth offering
#: as a pile. Below it the suggestion list becomes a list of everyone you
#: have ever corresponded with, which is not a suggestion.
MIN_PILE = 3

#: Never offer more than this many suggestions. The list is ranked by how
#: much each one clears, so the tail is a long line of three-message piles
#: that cost more to read than they save.
MAX_SUGGESTIONS = 12


def imap_date(when: datetime) -> str:
    """A date as IMAP wants it: ``17-Sep-2026``, locale-independent."""
    return f"{when.day:02d}-{_MONTHS[when.month - 1]}-{when.year}"


def quote_search(value: str) -> str:
    """One IMAP quoted string, with the two characters that need escaping.

    RFC 3501 quoted strings escape backslash and double quote and nothing
    else. A subject containing either is rare and entirely legal, and
    getting it wrong does not fail cleanly - it shifts the rest of the
    command along by one token, so the server searches for something nobody
    asked for.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _or(keys: Sequence[Sequence[str]]) -> List[str]:
    """Fold search keys into IMAP's prefix ``OR``.

    ``OR`` takes exactly two arguments, so three keys are ``OR a OR b c``.
    Written as a fold rather than by hand because the off-by-one version of
    this - one ``OR`` too few - is still a valid command that searches for
    the wrong thing.
    """
    keys = [list(k) for k in keys if k]
    if not keys:
        return []
    folded = list(keys[-1])
    for key in reversed(keys[:-1]):
        folded = ["OR", *key, *folded]
    return folded


def _clean(values: Iterable[str]) -> Tuple[str, ...]:
    seen: List[str] = []
    for value in values or ():
        text = (value or "").strip()
        if text and text not in seen:
            seen.append(text)
    return tuple(seen)


@dataclass(frozen=True)
class Criteria:
    """What to clear out, in terms a server can answer in one command.

    Everything here is a filter, and they combine with "and": senders *and*
    older than *and* already read. Within one field the parts combine with
    "or", because "from any of these three shops" is the question people
    actually have.
    """

    folder: str = "INBOX"
    #: Substrings matched against the From header. An address matches, so
    #: does a domain, because ``FROM`` is a substring search on the server.
    senders: Tuple[str, ...] = ()
    #: Substrings matched against Subject.
    subjects: Tuple[str, ...] = ()
    #: Only messages older than this many days. 0 means no age filter.
    older_than_days: int = 0
    #: Only messages already read. The unread ones are the ones you have not
    #: decided about yet, so this is on by default.
    only_seen: bool = True
    #: Only messages carrying a List-Unsubscribe header - that is, mail sent
    #: to a list rather than to you.
    only_bulk: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "folder", (self.folder or "INBOX").strip() or "INBOX")
        object.__setattr__(self, "senders", _clean(self.senders))
        object.__setattr__(self, "subjects", _clean(self.subjects))
        object.__setattr__(self, "older_than_days", max(0, int(self.older_than_days or 0)))

    # -- what it is ------------------------------------------------------
    @property
    def is_armed(self) -> bool:
        """Whether this asks for anything at all.

        An unarmed ``Criteria`` matches every message in the folder, which is
        the one outcome nobody arrives at by accident on purpose. Callers
        refuse it; emptying a whole folder is its own explicit command.

        ``only_seen`` alone does not arm it. It is on by default, so a
        criteria with the text boxes empty would otherwise mean "delete
        everything you have read", which is most of a mailbox.
        """
        return bool(self.senders or self.subjects
                    or self.older_than_days or self.only_bulk)

    def describe(self) -> str:
        """One sentence for the confirmation, in the user's words."""
        parts: List[str] = []
        if self.senders:
            parts.append("from " + _one_of(self.senders))
        if self.subjects:
            parts.append("with " + _one_of(self.subjects) + " in the subject")
        if self.only_bulk:
            parts.append("sent to a mailing list")
        if self.older_than_days:
            parts.append(f"older than {self.older_than_days} day"
                         f"{'' if self.older_than_days == 1 else 's'}")
        if not parts:
            # Nothing was asked for, so "already read" is not a filter, it
            # is a decoration on "everything". Saying it would make an
            # unarmed criteria read like a narrow one.
            return f"every message in {self.folder}"
        if self.only_seen:
            parts.append("already read")
        if len(parts) == 1:
            return f"messages in {self.folder} {parts[0]}"
        if len(parts) == 2:
            return f"messages in {self.folder} {parts[0]} and {parts[1]}"
        return (f"messages in {self.folder} "
                + ", ".join(parts[:-1]) + f", and {parts[-1]}")

    # -- what the server is asked ----------------------------------------
    def search_tokens(self, today: Optional[datetime] = None) -> List[str]:
        """The SEARCH command's arguments, as tokens.

        Returned as a list rather than a string so that the engine can hand
        them to imaplib one at a time, which is what keeps a quoted subject
        with a space in it from being read as two search keys.
        """
        tokens: List[str] = []
        if self.senders:
            tokens += _or([["FROM", quote_search(s)] for s in self.senders])
        if self.subjects:
            tokens += _or([["SUBJECT", quote_search(s)] for s in self.subjects])
        if self.only_bulk:
            # A zero-length string in a HEADER search means "has this header
            # at all", which is exactly the question: bulk mail is mail that
            # came with a way to unsubscribe from it.
            tokens += ["HEADER", "List-Unsubscribe", '""']
        if self.older_than_days:
            when = (today or datetime.now()) - timedelta(days=self.older_than_days)
            tokens += ["BEFORE", imap_date(when)]
        if self.only_seen:
            tokens += ["SEEN"]
        return tokens or ["ALL"]

    def with_folder(self, folder: str) -> "Criteria":
        return Criteria(folder=folder, senders=self.senders,
                        subjects=self.subjects,
                        older_than_days=self.older_than_days,
                        only_seen=self.only_seen, only_bulk=self.only_bulk)

    def to_dict(self) -> Dict[str, object]:
        return {
            "folder": self.folder,
            "senders": list(self.senders),
            "subjects": list(self.subjects),
            "older_than_days": self.older_than_days,
            "only_seen": self.only_seen,
            "only_bulk": self.only_bulk,
        }

    @classmethod
    def from_dict(cls, raw) -> "Criteria":
        raw = raw or {}
        return cls(
            folder=str(raw.get("folder", "INBOX") or "INBOX"),
            senders=tuple(raw.get("senders") or ()),
            subjects=tuple(raw.get("subjects") or ()),
            older_than_days=int(raw.get("older_than_days") or 0),
            only_seen=bool(raw.get("only_seen", True)),
            only_bulk=bool(raw.get("only_bulk", False)),
        )


def _one_of(values: Sequence[str]) -> str:
    values = list(values)
    if len(values) == 1:
        return f"“{values[0]}”"
    if len(values) == 2:
        return f"“{values[0]}” or “{values[1]}”"
    return f"“{values[0]}” or {len(values) - 1} others"


@dataclass
class Suggestion:
    """One pile of mail worth clearing, and the reason it was offered."""

    #: "sender" or "kind".
    kind: str
    #: The address or the category name.
    value: str
    label: str
    reason: str
    count: int
    #: The most recent one, so the user can see whether this is still live.
    newest: Optional[datetime] = None
    senders: Tuple[str, ...] = ()
    bulk_only: bool = False

    def criteria(self, folder: str = "INBOX", older_than_days: int = 0,
                 only_seen: bool = True) -> Criteria:
        """What to ask the server for, if this suggestion is taken.

        A suggestion counted *scanned* rows; the criteria it produces asks
        the server about the whole folder, which is normally a much larger
        number. That is the point - the scan found the pattern, the server
        knows how far back it goes - and it is why the dialog counts again
        before deleting anything.
        """
        return Criteria(folder=folder, senders=self.senders,
                        older_than_days=older_than_days,
                        only_seen=only_seen, only_bulk=self.bulk_only)


_ADDRESS = re.compile(r"[^@\s]+@[^@\s]+")


def _kind_of(item) -> str:
    """The non-job category as its bare name, never the enum's repr.

    ``str()`` on a ``str``-mixin enum gives ``OtherCategory.NEWSLETTER`` on
    one Python and ``NEWSLETTER`` on another, and the difference is silent:
    every membership test just stops matching.
    """
    category = getattr(getattr(item, "classification", None),
                       "other_category", None)
    return str(getattr(category, "value", category) or "")


def _address_of(email) -> str:
    address = (getattr(email, "sender_email", "") or "").strip().lower()
    if address:
        return address
    found = _ADDRESS.search(getattr(email, "sender_name", "") or "")
    return found.group(0).lower() if found else ""


def suggest(items: Sequence[object],
            protected: Iterable[str] = ()) -> List[Suggestion]:
    """Piles worth clearing, biggest first, from rows already scanned.

    No network, no model, no guessing at content: this reads the verdicts the
    scan already produced. A sender is offered when every message the app has
    seen from them is non-job mail of a disposable kind - newsletters,
    promotions, social notifications, spam - and there are at least
    :data:`MIN_PILE` of them.

    ``protected`` is addresses never to offer, which the window fills from
    the corrections memory: a sender you have taken the trouble to teach the
    app about is not one it should propose deleting.

    One message from a sender that is job-related disqualifies the whole
    sender. That is deliberately harsh, and it is the right way round: the
    cost of missing a pile is scrolling, and the cost of a wrong suggestion
    is a deleted interview invitation.
    """
    protected = {p.strip().lower() for p in protected if (p or "").strip()}
    by_sender: Dict[str, List[object]] = {}
    disqualified: set = set()
    kinds: Counter = Counter()
    kind_senders: Dict[str, set] = {}

    for item in items or ():
        email = getattr(item, "email", None)
        classification = getattr(item, "classification", None)
        if email is None or classification is None:
            continue
        address = _address_of(email)
        if not address or address in protected:
            continue
        kind = _kind_of(item)
        job = bool(getattr(classification, "is_job_related", False))
        if job or kind not in DISPOSABLE:
            disqualified.add(address)
            continue
        by_sender.setdefault(address, []).append(item)
        kinds[kind] += 1
        kind_senders.setdefault(kind, set()).add(address)

    found: List[Suggestion] = []
    for address, group in by_sender.items():
        if address in disqualified or len(group) < MIN_PILE:
            continue
        bulk = all((getattr(i.email, "list_unsubscribe", "") or "").strip()
                   for i in group)
        found.append(Suggestion(
            kind="sender", value=address, senders=(address,),
            label=address,
            reason=_sender_reason(group, bulk),
            count=len(group),
            newest=_newest(group),
            bulk_only=False,
        ))

    for kind, count in kinds.items():
        senders = tuple(sorted(a for a in kind_senders.get(kind, ())
                               if a not in disqualified))
        if count < MIN_PILE or len(senders) < 2:
            # A category with one sender in it is already offered above as
            # that sender, and saying it twice makes the list look longer
            # than it is.
            continue
        found.append(Suggestion(
            kind="kind", value=kind, senders=senders,
            label=_KIND_LABELS.get(kind, kind.title()),
            reason=f"{count} scanned, from {len(senders)} senders",
            count=count,
            newest=None,
            bulk_only=kind in ("NEWSLETTER", "PROMOTION"),
        ))

    found.sort(key=lambda s: (-s.count, s.label))
    return found[:MAX_SUGGESTIONS]


_KIND_LABELS = {
    "NEWSLETTER": "Newsletters",
    "PROMOTION": "Promotions",
    "SOCIAL": "Social notifications",
    "SPAM": "Spam",
}


def _sender_reason(group: Sequence[object], bulk: bool) -> str:
    kinds = Counter(_kind_of(i) for i in group)
    common = kinds.most_common(1)[0][0] if kinds else ""
    name = _KIND_LABELS.get(common, "")
    said = f"{len(group)} scanned"
    if name:
        said += f", mostly {name.lower()}"
    if bulk:
        said += ", all bulk"
    return said


def _newest(group: Sequence[object]) -> Optional[datetime]:
    dates = [getattr(i.email, "date", None) for i in group]
    dates = [d for d in dates if d is not None]
    return max(dates) if dates else None
