"""What a scan found, in the order somebody would want to be told it.

The table is the right way to check a decision and the wrong way to find out
what happened. Forty rows across four mailboxes is four hundred glances, and
the three that matter are somewhere in the middle of it. Every scan already
knows which three - it has read every message and decided what each one is -
and then throws that away in favour of a grid sorted by date.

So this is the same scan, read back as a briefing:

**Needs you.** Interviews to book, offers with a date on them, assessments
with a clock running, and anything the sorter could not place. Ranked by what
it costs to miss, not by when it arrived: an interview invitation from
Tuesday outranks a rejection from an hour ago.

**What came in.** Counts by kind, each one naming the folder it is bound for,
so "eleven application receipts, all going to Job Search/Received" is one
line instead of eleven rows.

**Where it is going.** The same messages counted by destination folder, which
is the question "what will Apply actually do" asked directly.

**Who wrote.** The senders with the most in this batch, because that is how
somebody notices that a third of their morning is one job board.

**Still waiting.** Actionable messages nobody has ticked, and the mailboxes
that had nothing at all, since "no news from that account" is a finding.

Everything here is derived from rows the app already has. Nothing in this file
opens a mailbox, calls a model, or writes anything down; hand it the same list
twice and it says the same thing twice. That matters because a briefing is
read once and trusted, so it cannot be the part that goes and does something.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from models import Category, Disposition, OtherCategory

__all__ = ["Briefing", "Headline", "Line", "Urgency", "build"]


class Urgency:
    """How badly something wants a human. Higher sorts first."""

    #: Somebody is waiting on an answer with a date attached.
    ACT_NOW = 100
    #: A decision is being asked for, without a stated deadline.
    ANSWER = 80
    #: The sorter could not place it, so nobody has looked at it yet.
    UNSURE = 60
    #: Worth knowing, nothing to do.
    NOTE = 20


#: What each job category means for the person reading. The number is the
#: urgency; the sentence is what the briefing says about it.
JOB_URGENCY: Dict[Category, Tuple[int, str]] = {
    Category.OFFER: (Urgency.ACT_NOW, "an offer to answer"),
    Category.INTERVIEW: (Urgency.ACT_NOW, "an interview to arrange"),
    Category.NEXT_STEPS: (Urgency.ANSWER, "a next step waiting on you"),
    Category.NETWORKING: (Urgency.ANSWER, "someone who wants to talk"),
    Category.APPLICATION_RECEIVED: (Urgency.NOTE, "an application acknowledged"),
    Category.NOT_INTERESTED: (Urgency.NOTE, "a rejection"),
    Category.UNSOLICITED: (Urgency.NOTE, "a recruiter getting in touch"),
    Category.UNCLASSIFIED_OTHER: (Urgency.UNSURE, "job mail it could not place"),
}

#: The non-job topics that can still want something from you. Everything
#: else is counted and not ranked.
TOPIC_URGENCY: Dict[OtherCategory, Tuple[int, str]] = {
    OtherCategory.SECURITY: (Urgency.ACT_NOW, "a security notice"),
    OtherCategory.FINANCE: (Urgency.ANSWER, "something about money"),
    OtherCategory.EVENT: (Urgency.ANSWER, "an invitation"),
    OtherCategory.PERSONAL: (Urgency.ANSWER, "a message from a person"),
    OtherCategory.WORK: (Urgency.ANSWER, "work"),
    OtherCategory.TRAVEL: (Urgency.NOTE, "travel"),
    OtherCategory.SHIPPING: (Urgency.NOTE, "a delivery"),
}

#: How many of each list is worth printing before it stops being a summary.
MOST = 8


@dataclass(frozen=True)
class Line:
    """One entry in a list: what it is, how many, and where it goes."""

    label: str
    count: int = 1
    detail: str = ""
    folder: str = ""
    #: The table row this came from, when a line is about one message, so
    #: the window can jump to it. -1 when the line is a count of many.
    row: int = -1
    urgency: int = Urgency.NOTE

    def describe(self) -> str:
        parts = [self.label]
        if self.detail:
            parts.append(self.detail)
        return " - ".join(parts)


@dataclass(frozen=True)
class Headline:
    """The first sentence: how much, from where, over what."""

    total: int = 0
    mailboxes: Tuple[str, ...] = ()
    window_start: Optional[datetime] = None
    window_end: Optional[datetime] = None
    job_related: int = 0
    to_file: int = 0
    needs_review: int = 0
    ticked: int = 0

    def sentence(self) -> str:
        if not self.total:
            return "Nothing arrived in the window you scanned."
        boxes = ""
        if len(self.mailboxes) > 1:
            boxes = f" across {len(self.mailboxes)} mailboxes"
        when = _window(self.window_start, self.window_end)
        return (f"{self.total} message{_s(self.total)}{boxes}{when}: "
                f"{self.job_related} about your job search, "
                f"{self.total - self.job_related} everything else.")

    def filing(self) -> str:
        if not self.total:
            return ""
        if not self.to_file:
            return "Nothing is set to be filed."
        return (f"{self.ticked} of {self.to_file} ready to file are ticked."
                if self.ticked != self.to_file
                else f"All {self.to_file} ready to file are ticked.")


@dataclass
class Briefing:
    """Everything the window shows, worked out once."""

    headline: Headline = field(default_factory=Headline)
    #: Ranked, most urgent first. One entry per message.
    attention: List[Line] = field(default_factory=list)
    #: Counted by what the message is.
    arrivals: List[Line] = field(default_factory=list)
    #: Counted by where it is bound.
    folders: List[Line] = field(default_factory=list)
    #: Counted by who sent it.
    senders: List[Line] = field(default_factory=list)
    #: Loose ends: actionable but unticked, and mailboxes with nothing.
    waiting: List[Line] = field(default_factory=list)
    quiet: List[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return self.headline.total == 0

    def as_text(self) -> str:
        """The whole thing as plain text, for copying out of the window."""
        out: List[str] = [self.headline.sentence()]
        filing = self.headline.filing()
        if filing:
            out.append(filing)
        for title, lines in (("Needs you", self.attention),
                             ("What came in", self.arrivals),
                             ("Where it is going", self.folders),
                             ("Who wrote", self.senders),
                             ("Still waiting", self.waiting)):
            if not lines:
                continue
            out.append("")
            out.append(title)
            for line in lines:
                count = f"{line.count} x " if line.count > 1 else ""
                folder = f"  -> {line.folder}" if line.folder else ""
                out.append(f"  {count}{line.describe()}{folder}")
        if self.quiet:
            out.append("")
            out.append("Nothing at all from: " + ", ".join(self.quiet))
        return "\n".join(out)


def _s(count: int) -> str:
    return "" if count == 1 else "s"


def _window(start: Optional[datetime], end: Optional[datetime]) -> str:
    if start is None:
        return ""
    end = end or datetime.now(tz=start.tzinfo)
    hours = max(0.0, (end - start).total_seconds() / 3600.0)
    if hours <= 1.5:
        return " in the last hour"
    if hours < 36:
        return f" in the last {round(hours)} hours"
    return f" in the last {round(hours / 24)} days"


def _urgency(item) -> Tuple[int, str]:
    """How much this one message wants a human, and why."""
    classification = item.classification
    if classification.error is not None:
        return Urgency.UNSURE, "the sorter could not read it"
    if classification.is_job_related:
        return JOB_URGENCY.get(classification.category,
                               (Urgency.NOTE, "job mail"))
    if item.disposition is Disposition.REVIEW:
        return Urgency.UNSURE, "not sure what this is"
    return TOPIC_URGENCY.get(classification.other_category,
                             (Urgency.NOTE, ""))


def _rank(item, row: int, urgency: int, why: str) -> Tuple:
    """Sort key: urgency first, then confidence, then newest.

    Confidence before date on purpose. Two interview invitations are not
    equally interesting if the sorter is sure about one and guessing at
    the other, and the one it is sure about is the one somebody should
    read first.
    """
    when = item.email.date
    stamp = when.timestamp() if when is not None else 0.0
    return (-urgency, -item.classification.confidence_score, -stamp, row)


def build(items: Sequence, *, window_start: Optional[datetime] = None,
          window_end: Optional[datetime] = None,
          mailboxes: Iterable[str] = ()) -> Briefing:
    """Read a list of scanned rows and say what happened.

    ``mailboxes`` is every account that was scanned, including ones that
    turned out to be empty - a briefing that silently omits an account
    cannot be used to answer "is there anything I have missed", which is
    the only question it is for.
    """
    items = list(items or ())
    named = [name for name in mailboxes if (name or "").strip()]

    if not items:
        return Briefing(headline=Headline(total=0, mailboxes=tuple(named),
                                          window_start=window_start,
                                          window_end=window_end),
                        quiet=list(named))

    seen: List[str] = []
    for item in items:
        label = (item.email.account_label or item.email.account_address
                 or "").strip()
        if label and label not in seen:
            seen.append(label)
    boxes = named or seen

    job = sum(1 for i in items if i.classification.is_job_related)
    filed = [i for i in items if i.is_actionable]
    ticked = sum(1 for i in filed if i.approved)
    review = sum(1 for i in items if i.disposition is Disposition.REVIEW)

    headline = Headline(
        total=len(items), mailboxes=tuple(boxes),
        window_start=window_start, window_end=window_end,
        job_related=job, to_file=len(filed), needs_review=review,
        ticked=ticked,
    )

    # -- what wants a human ------------------------------------------------
    ranked: List[Tuple[Tuple, Line]] = []
    for row, item in enumerate(items):
        urgency, why = _urgency(item)
        if urgency <= Urgency.NOTE:
            continue
        ranked.append((_rank(item, row, urgency, why), Line(
            label=item.email.subject_display,
            detail=f"{item.email.sender_short} - {why}" if why
                   else item.email.sender_short,
            folder=item.target_folder or "",
            row=row, urgency=urgency,
        )))
    ranked.sort(key=lambda pair: pair[0])
    attention = [line for _key, line in ranked[:MOST]]

    # -- what arrived, by kind ---------------------------------------------
    kinds: Counter = Counter()
    kind_folder: Dict[str, str] = {}
    kind_rank: Dict[str, int] = {}
    for item in items:
        urgency, why = _urgency(item)
        if item.classification.is_job_related:
            label = _title(item.classification.category.value)
        elif item.disposition is Disposition.REVIEW:
            label = "Needs review"
        else:
            label = _title(item.classification.other_category.value)
        kinds[label] += 1
        kind_folder.setdefault(label, item.target_folder or "")
        kind_rank[label] = max(kind_rank.get(label, 0), urgency)
    arrivals = [Line(label=label, count=count, folder=kind_folder.get(label, ""),
                     urgency=kind_rank.get(label, Urgency.NOTE))
                for label, count in kinds.most_common(MOST + 4)]

    # -- where it is going -------------------------------------------------
    going: Counter = Counter()
    for item in items:
        folder = item.target_folder
        if folder:
            going[folder] += 1
    folders = [Line(label=folder, count=count)
               for folder, count in going.most_common(MOST + 4)]

    # -- who wrote ---------------------------------------------------------
    wrote: Counter = Counter()
    for item in items:
        who = (item.email.sender_email or item.email.sender_short or "").strip()
        if who:
            wrote[who] += 1
    senders = [Line(label=who, count=count)
               for who, count in wrote.most_common(MOST)
               if count > 1]

    # -- loose ends --------------------------------------------------------
    # These lines count themselves in their own words, so the count field
    # stays at one - otherwise the list reads "5 x 5 ready to file".
    waiting: List[Line] = []
    unticked = len(filed) - ticked
    if unticked:
        waiting.append(Line(label=f"{unticked} ready to file, not ticked",
                            urgency=Urgency.ANSWER))
    if review:
        waiting.append(Line(label=f"{review} in Needs Review",
                            urgency=Urgency.UNSURE))
    failed = sum(1 for i in items if i.classification.error is not None)
    if failed:
        waiting.append(Line(label=f"{failed} the sorter could not read",
                            urgency=Urgency.UNSURE))

    # Only when the rows say which mailbox they came from. On a single
    # account nothing carries a label, so every name passed in would look
    # like a mailbox that produced nothing - which is the one line in a
    # briefing that must never be wrong, because it is read as "checked,
    # and there was nothing there".
    quiet = [name for name in named if name not in seen] if seen else []

    return Briefing(headline=headline, attention=attention, arrivals=arrivals,
                    folders=folders, senders=senders, waiting=waiting,
                    quiet=quiet)


def _title(value: str) -> str:
    """``APPLICATION_RECEIVED`` as ``Application received``."""
    words = (value or "").replace("_", " ").strip().lower()
    return words[:1].upper() + words[1:] if words else "Other"
