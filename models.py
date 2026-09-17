"""Domain models for the iCloud Mail Job Triage application.

This module is deliberately free of Qt, IMAP and Anthropic imports so that the
routing rules that make up the "zero-misclassification protocol" can be unit
tested in isolation and reasoned about on their own.

The protocol has three layers:

1. **Prompt layer** (``llm_engine.SYSTEM_PROMPT``) - strict category definitions
   and an explicit instruction to fall back to ``UNCLASSIFIED_OTHER``.
2. **Validation layer** (:meth:`Classification.from_payload`) - deterministic
   repair of anything the model returns that is internally inconsistent.
3. **Routing layer** (:class:`TriageItem`) - a pure function from a validated
   classification to a disposition. Nothing is ever moved into a category
   folder unless the model was both confident *and* self-consistent.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

APP_NAME = "Mail Manager"
APP_DISPLAY_NAME = "Mail Manager"
APP_BUNDLE_ID = "com.mailmanager.icloudjobtriage"
APP_VERSION = "1.2.0"

#: Confidence at or above which the app is willing to pre-approve a move.
DEFAULT_CONFIDENCE_THRESHOLD = 0.95

#: Root mailbox that holds the four triage folders.
DEFAULT_FOLDER_ROOT = "Job Search"

_MONTH_ABBR = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


class Category(str, Enum):
    """The exact enum the LLM is constrained to emit."""

    INTERVIEW = "INTERVIEW"
    NEXT_STEPS = "NEXT_STEPS"
    OFFER = "OFFER"
    APPLICATION_RECEIVED = "APPLICATION_RECEIVED"
    NETWORKING = "NETWORKING"
    NOT_INTERESTED = "NOT_INTERESTED"
    UNSOLICITED = "UNSOLICITED"
    UNCLASSIFIED_OTHER = "UNCLASSIFIED_OTHER"

    @classmethod
    def parse(cls, value: Any) -> Optional["Category"]:
        """Best-effort parse. Returns ``None`` when the value is not valid."""
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            return None
        normalised = value.strip().upper().replace(" ", "_").replace("-", "_")
        try:
            return cls(normalised)
        except ValueError:
            return None

    @property
    def label(self) -> str:
        return {
            Category.INTERVIEW: "Interview",
            Category.NEXT_STEPS: "Next Steps",
            Category.OFFER: "Offer",
            Category.APPLICATION_RECEIVED: "Application Received",
            Category.NETWORKING: "Networking",
            Category.NOT_INTERESTED: "Rejected",
            Category.UNSOLICITED: "Unsolicited",
            Category.UNCLASSIFIED_OTHER: "Unclassified / Other",
        }[self]

    @property
    def color(self) -> str:
        """Hex colour used for this category's chip, bar and row tint."""
        return CATEGORY_COLORS[self]


class Disposition(str, Enum):
    """What the app intends to do with a message."""

    #: Confident and job related - route to the matching category folder.
    MOVE = "MOVE"
    #: Uncertain, inconsistent, or errored - route to ``Needs Review``.
    REVIEW = "REVIEW"
    #: Confidently not job related - leave the message exactly where it is.
    LEAVE = "LEAVE"

    @property
    def label(self) -> str:
        return {
            Disposition.MOVE: "Move",
            Disposition.REVIEW: "Needs review",
            Disposition.LEAVE: "Leave in place",
        }[self]


#: Leaf mailbox name for each category.
CATEGORY_LEAF: Dict[Category, str] = {
    Category.INTERVIEW: "Interview",
    Category.NEXT_STEPS: "Next Steps",
    Category.OFFER: "Offers",
    Category.APPLICATION_RECEIVED: "Received",
    Category.NETWORKING: "Networking",
    Category.NOT_INTERESTED: "Not Interested",
    Category.UNSOLICITED: "Unsolicited",
    Category.UNCLASSIFIED_OTHER: "Needs Review",
}

#: Category colours. Chosen to stay legible on both light and dark backgrounds:
#: mid-tone, similar luminance, and distinguishable without relying on hue
#: alone (each is paired with a distinct label everywhere it appears).
CATEGORY_COLORS: Dict[Category, str] = {
    Category.OFFER: "#B347C4",              # magenta - the rare, important one
    Category.INTERVIEW: "#2E9E63",          # green - a person wants to talk
    Category.NEXT_STEPS: "#3B7DD8",         # blue - you must do something
    Category.APPLICATION_RECEIVED: "#0E9488",  # teal - informational
    Category.NETWORKING: "#7C6BE8",         # violet - a conversation, not a process
    Category.NOT_INTERESTED: "#A8564C",     # muted red - closed
    Category.UNSOLICITED: "#7A8290",        # grey - noise
    Category.UNCLASSIFIED_OTHER: "#D08A1E", # amber - needs a human
}

#: The neutral, for a topic this configuration is not sorting. It is meant to
#: recede: a grey chip says "the app is not doing anything with this".
OTHER_COLOR = "#6B7A8F"

#: A colour per everyday topic, used only when that topic is one the current
#: settings actually file into.
#:
#: Colouring all of them all the time would be thirteen more things to learn
#: for no gain, since with non-job mail left alone none of them lead anywhere.
#: Colouring none of them - which is what happened before - made a mailbox
#: being sorted into thirteen folders look like a wall of identical grey.
#: The rule is the honest one: a chip is coloured when the app is going to
#: act on it.
TOPIC_COLORS: Dict["OtherCategory", str] = {}      # filled in below

REVIEW_LEAF = "Needs Review"

#: Where job mail goes when a profile does not want seven separate folders.
COLLAPSED_JOB_LEAF = "Job Search"

#: Root mailbox for filed non-job mail (only used when the user opts in).
DEFAULT_OTHER_ROOT = "Sorted Mail"
#: The leaf of the folder rules bin things into. "To Delete" rather than
#: "Trash": Trash is the server's own, some providers empty it on a
#: schedule, and mail the app put somewhere should not disappear on a timer
#: nobody set here.
BIN_LEAF = "To Delete"


class OtherCategory(str, Enum):
    """Second-level classification for mail that is *not* job related.

    The job pipeline answers "where in Job Search does this belong?". This enum
    answers the follow-up question for everything else, so that non-job mail is
    described rather than dumped into a single opaque bucket.
    """

    NOT_APPLICABLE = "NOT_APPLICABLE"
    PERSONAL = "PERSONAL"
    WORK = "WORK"
    FINANCE = "FINANCE"
    RECEIPT = "RECEIPT"
    SHIPPING = "SHIPPING"
    SECURITY = "SECURITY"
    NEWSLETTER = "NEWSLETTER"
    PROMOTION = "PROMOTION"
    SOCIAL = "SOCIAL"
    EVENT = "EVENT"
    TRAVEL = "TRAVEL"
    #: Mail from a church or place of worship: services, rotas, small groups,
    #: giving, prayer. It overlaps Events, Newsletters and Personal by shape
    #: and is none of them by subject, which is exactly why it earns its own.
    CHURCH = "CHURCH"
    SPAM = "SPAM"
    OTHER = "OTHER"

    @classmethod
    def parse(cls, value: Any) -> Optional["OtherCategory"]:
        if isinstance(value, cls):
            return value
        if not isinstance(value, str):
            return None
        normalised = value.strip().upper().replace(" ", "_").replace("-", "_")
        try:
            return cls(normalised)
        except ValueError:
            return None

    @property
    def label(self) -> str:
        return _OTHER_LABELS[self]

    @property
    def leaf(self) -> str:
        return _OTHER_LEAF[self]


_OTHER_LABELS: Dict["OtherCategory", str] = {
    OtherCategory.NOT_APPLICABLE: "-",
    OtherCategory.PERSONAL: "Personal",
    OtherCategory.WORK: "Work",
    OtherCategory.FINANCE: "Finance & Bills",
    OtherCategory.RECEIPT: "Receipts & Orders",
    OtherCategory.SHIPPING: "Shipping",
    OtherCategory.SECURITY: "Security & Account",
    OtherCategory.NEWSLETTER: "Newsletters",
    OtherCategory.PROMOTION: "Promotions",
    OtherCategory.SOCIAL: "Social",
    OtherCategory.EVENT: "Events",
    OtherCategory.TRAVEL: "Travel",
    OtherCategory.CHURCH: "Church",
    OtherCategory.SPAM: "Spam & Phishing",
    OtherCategory.OTHER: "Other",
}

TOPIC_COLORS.update({
    OtherCategory.SECURITY: "#C0453B",     # red - an alert, read it now
    OtherCategory.FINANCE: "#2F7D4F",      # green - money owed or moving
    # A deeper teal than Application Received's, which is the same family and
    # was for a while the same value: two meanings sharing a colour in the
    # one column that shows both.
    OtherCategory.RECEIPT: "#0B6E66",      # deep teal - money already spent
    OtherCategory.SHIPPING: "#B5701F",     # brown - a parcel
    OtherCategory.TRAVEL: "#2D8FC4",       # sky - somewhere to be
    OtherCategory.EVENT: "#8A5BD6",        # purple - a date in a diary
    OtherCategory.CHURCH: "#5B63C4",       # indigo
    OtherCategory.WORK: "#4A6FA5",         # slate - the job you have
    OtherCategory.PERSONAL: "#C25E8A",     # warm pink - a person
    OtherCategory.SOCIAL: "#9A4FD0",       # violet - a network
    OtherCategory.NEWSLETTER: "#7A8C3A",   # olive - subscribed to
    OtherCategory.PROMOTION: "#C98A14",    # amber - sold to
    OtherCategory.SPAM: "#8A4A4A",         # dull red - unwanted
    OtherCategory.OTHER: OTHER_COLOR,      # the absence of an answer
    OtherCategory.NOT_APPLICABLE: OTHER_COLOR,
})

_OTHER_LEAF: Dict["OtherCategory", str] = {
    OtherCategory.NOT_APPLICABLE: "Other",
    OtherCategory.PERSONAL: "Personal",
    OtherCategory.WORK: "Work",
    OtherCategory.FINANCE: "Finance",
    OtherCategory.RECEIPT: "Receipts",
    OtherCategory.SHIPPING: "Shipping",
    OtherCategory.SECURITY: "Security",
    OtherCategory.NEWSLETTER: "Newsletters",
    OtherCategory.PROMOTION: "Promotions",
    OtherCategory.SOCIAL: "Social",
    OtherCategory.EVENT: "Events",
    OtherCategory.TRAVEL: "Travel",
    OtherCategory.CHURCH: "Church",
    OtherCategory.SPAM: "Junk",
    OtherCategory.OTHER: "Other",
}


class NonJobRouting(Enum):
    """What to do with mail the model is confident is *not* job related."""

    LEAVE = ("LEAVE", "Leave in place (recommended)")
    REVIEW = ("REVIEW", "File under Job Search / Needs Review")
    FILE = ("FILE", "File by topic into the Sorted Mail folders")

    def __init__(self, value: str, label: str) -> None:
        self._value_ = value
        self._label = label

    @property
    def label(self) -> str:
        return self._label

    @classmethod
    def parse(cls, value: Any, default: "NonJobRouting" = None) -> "NonJobRouting":
        default = default or cls.LEAVE
        if isinstance(value, cls):
            return value
        if isinstance(value, str):
            for member in cls:
                if member.value == value.strip().upper():
                    return member
        return default


def sanitize_folder_component(name: str) -> str:
    """Strip characters that are illegal or ambiguous in an IMAP mailbox name.

    Also refuses the two names that mean "here" and "the level above". Plenty
    of IMAP servers still store each mailbox as a directory, so a mailbox
    called ".." is somewhere between confusing and a way out of the mail root,
    and no legitimate folder is called that.
    """
    cleaned = re.sub(r'[\x00-\x1f\x7f"\\/]+', " ", str(name))
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if set(cleaned) <= {".", " "} and cleaned:
        return ""
    # Leading dots go, and go repeatedly: "../../etc" arrives here as
    # ".. .. etc" once the slashes are spaces, and stripping once left the
    # second pair in place. A mailbox called ".." is nobody's mailbox.
    while cleaned.startswith(".") or cleaned.startswith(" "):
        cleaned = cleaned.lstrip(". ").strip()
    return cleaned or ""


@dataclass(frozen=True)
class FolderPlan:
    """Resolves category -> IMAP mailbox path using the server's delimiter."""

    root: str = DEFAULT_FOLDER_ROOT
    delimiter: str = "/"
    other_root: str = DEFAULT_OTHER_ROOT
    #: False collapses every job category into one folder. The table still
    #: shows the exact category; only the filing is coarser.
    detailed_job_folders: bool = True
    #: Topics worth a folder of their own. Empty means all of them; anything
    #: left out is filed under "Other" rather than given an empty mailbox.
    topics: Tuple["OtherCategory", ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", sanitize_folder_component(self.root) or DEFAULT_FOLDER_ROOT)
        object.__setattr__(self, "delimiter", self.delimiter or "/")
        object.__setattr__(
            self, "other_root", sanitize_folder_component(self.other_root) or DEFAULT_OTHER_ROOT
        )

    @property
    def job_root(self) -> str:
        """Where job mail is filed. Always the job root.

        This used to move the whole tree under the non-job root when the
        detailed folders were switched off, on the reasoning that job search
        was then one topic among many. In a mailbox it read as a mistake:
        "Sorted Mail/Job Search" and "Sorted Mail/Needs Review" sitting beside
        a "Job Search" tree left over from the previous setting, with no way
        to tell which was meant. Collapsing changes the shape of the tree, not
        where it lives.
        """
        return self.root

    def path(self, leaf: str) -> str:
        return f"{self.job_root}{self.delimiter}{sanitize_folder_component(leaf)}"

    def other_path(self, leaf: str) -> str:
        return f"{self.other_root}{self.delimiter}{sanitize_folder_component(leaf)}"

    def for_category(self, category: Category) -> str:
        if not self.detailed_job_folders and category is not Category.UNCLASSIFIED_OTHER:
            # The root itself, rather than a folder of the same name inside
            # it: "Job Search/Job Search" is nobody's idea of a tidy mailbox.
            return self.job_root
        return self.path(CATEGORY_LEAF[category])

    def for_other_category(self, category: "OtherCategory") -> str:
        if self.topics and category not in self.topics:
            return self.other_path(OtherCategory.OTHER.leaf)
        return self.other_path(category.leaf)

    @property
    def review_folder(self) -> str:
        return self.path(REVIEW_LEAF)

    @property
    def bin_folder(self) -> str:
        """Where mail goes when a rule decides it is not worth keeping.

        A folder rather than a delete. Rules are written by people, people
        write them wrong the first time, and a rule that deleted straight
        off the server would turn a typo into lost mail. Everything a rule
        bins is still there to be looked at until somebody empties the
        folder, and emptying it is one command however much is in it.
        """
        return self.other_path(BIN_LEAF)

    @property
    def leaf_folders(self) -> Tuple[str, ...]:
        seen: List[str] = []
        for category in CATEGORY_LEAF:
            path = self.for_category(category)
            if path not in seen:
                seen.append(path)
        return tuple(seen)

    @property
    def all_folders(self) -> Tuple[str, ...]:
        """Root first, so parents are created before children.

        Deduplicated, because with the detailed folders collapsed the root is
        also where job mail is filed, and asking a server to create the same
        mailbox twice is a needless round trip and a needless warning.
        """
        ordered = [self.job_root]
        for folder in self.leaf_folders:
            if folder not in ordered:
                ordered.append(folder)
        return tuple(ordered)

    def other_folders(self, categories: Iterable["OtherCategory"] = ()) -> Tuple[str, ...]:
        """Root plus one leaf per supplied category, parents first.

        Only the categories actually needed are returned - the app never
        litters an account with a dozen empty mailboxes.
        """
        leaves: List[str] = []
        for category in categories:
            path = self.for_other_category(category)
            if path not in leaves:
                leaves.append(path)
        if not leaves:
            return ()
        return (self.other_root,) + tuple(leaves)


class TimeWindow(Enum):
    """Quick-preset scan windows offered in the action bar."""

    # "Past" is implied by the range spelled out beside these, and four
    # buttons carrying a redundant word cost about a hundred and fifty pixels
    # of a toolbar that has to fit a great deal else.
    LAST_24_HOURS = ("24 hours", 1)
    LAST_3_DAYS = ("3 days", 3)
    LAST_7_DAYS = ("7 days", 7)
    CUSTOM = ("Custom", None)

    def __init__(self, label: str, days: Optional[int]) -> None:
        self._label = label
        self._days = days

    @property
    def label(self) -> str:
        return self._label

    @property
    def days(self) -> Optional[int]:
        return self._days

    @classmethod
    def from_name(cls, name: str, default: "TimeWindow" = None) -> "TimeWindow":
        for member in cls:
            if member.name == name:
                return member
        return default if default is not None else cls.LAST_24_HOURS


def resolve_window(
    preset: TimeWindow,
    now: Optional[datetime] = None,
    custom_start: Optional[datetime] = None,
    custom_end: Optional[datetime] = None,
) -> Tuple[datetime, datetime]:
    """Return an inclusive-start/exclusive-end UTC datetime pair for a preset."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if preset is TimeWindow.CUSTOM:
        if custom_start is None:
            raise ValueError("A custom range requires a start date.")
        start = _as_utc(custom_start)
        end = _as_utc(custom_end) if custom_end is not None else now
        if end < start:
            start, end = end, start
        return start, end
    return now - timedelta(days=preset.days), now


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def imap_since_date(value: datetime) -> str:
    """Format a datetime as an IMAP ``SINCE``/``BEFORE`` date (locale independent).

    ``strftime('%d-%b-%Y')`` is locale sensitive, which silently breaks IMAP
    search on non-English systems, so the month table is spelled out here.
    """
    return f"{value.day:02d}-{_MONTH_ABBR[value.month - 1]}-{value.year}"


@dataclass
class EmailMessage:
    """A single fetched message, already reduced to plain text."""

    uid: str
    #: Which mailbox this came from. Empty on a single-account setup, where
    #: there is nothing to disambiguate.
    account_id: str = ""
    account_label: str = ""
    account_address: str = ""
    subject: str = ""
    sender_name: str = ""
    sender_email: str = ""
    date: Optional[datetime] = None
    body_text: str = ""
    message_id: str = ""
    #: What this message is answering. Kept because threading is exact when
    #: these are present and guesswork when they are not.
    in_reply_to: str = ""
    references: str = ""
    to: str = ""
    reply_to: str = ""
    list_unsubscribe: str = ""
    #: RFC 3834's header, set by anything that answered automatically, and
    #: the older conventions that do the same job. Kept so a reply rule can
    #: recognise another machine and say nothing - two autoresponders
    #: talking to each other is the classic way this feature goes wrong.
    auto_submitted: str = ""
    precedence: str = ""
    x_auto_response_suppress: str = ""
    size: int = 0
    flags: Tuple[str, ...] = ()
    links: Tuple[str, ...] = ()
    attachments: Tuple[str, ...] = ()
    truncated: bool = False
    original_length: int = 0
    source_folder: str = "INBOX"

    @property
    def sender_display(self) -> str:
        if self.sender_name and self.sender_email:
            return f"{self.sender_name} <{self.sender_email}>"
        return self.sender_name or self.sender_email or "(unknown sender)"

    @property
    def sender_short(self) -> str:
        return self.sender_name or self.sender_email or "(unknown sender)"

    @property
    def subject_display(self) -> str:
        return self.subject or "(no subject)"

    def local_date(self) -> Optional[datetime]:
        if self.date is None:
            return None
        return self.date.astimezone()

    @property
    def mailbox_display(self) -> str:
        """What the Mailbox column shows.

        The domain is the part that tells one account from another at a
        glance, so it is always there; the name is only worth the width when
        it says something the address does not.
        """
        address = (self.account_address or "").strip()
        label = (self.account_label or "").strip()
        if not address:
            return label
        local, _, domain = address.partition("@")
        if not domain:
            return address
        if not label or label.lower() == local.lower():
            return address
        return f"{label} · @{domain}"

    def date_display(self, fmt: str = "%Y-%m-%d %H:%M") -> str:
        local = self.local_date()
        return local.strftime(fmt) if local else "(no date)"

    def date_human(self, now: Optional[datetime] = None) -> str:
        """A date a person can read at a glance.

        Recent mail is what a triage scan is mostly about, so the closer it is
        the less of the date is spelled out: "14:53" today, a weekday this
        week, a date this year, a full date beyond that.
        """
        local = self.local_date()
        if local is None:
            return "-"
        now = (now or datetime.now(timezone.utc)).astimezone(local.tzinfo)
        today = now.date()
        day = local.date()
        delta = (today - day).days
        if delta == 0:
            return f"Today  {clock(local)}"
        if delta == 1:
            return f"Yesterday  {clock(local)}"
        if 1 < delta < 7:
            return f"{local:%a}  {clock(local)}"
        if day.year == today.year:
            return f"{local.day} {local:%b}  {clock(local)}"
        return f"{local.day} {local:%b} {local.year}"

    def date_full(self) -> str:
        """The unambiguous form, used for tooltips."""
        local = self.local_date()
        if local is None:
            return "No date on this message"
        return f"{local:%A %d %B %Y}, {clock(local)} {local:%Z}".strip()


def clock(moment: datetime) -> str:
    """Wall-clock time, 12 hour with AM or PM.

    Built by hand rather than with %I and %p: those follow the C locale, and
    this needs to read the same way wherever the app runs.
    """
    return f"{moment.hour % 12 or 12}:{moment:%M} {'AM' if moment.hour < 12 else 'PM'}"


@dataclass
class Classification:
    """A validated, self-consistent classification result."""

    summary: str = ""
    is_job_related: bool = False
    category: Category = Category.UNCLASSIFIED_OTHER
    other_category: OtherCategory = OtherCategory.NOT_APPLICABLE
    confidence_score: float = 0.0
    reasoning: str = ""
    model: str = ""
    adjustments: Tuple[str, ...] = ()
    error: Optional[str] = None
    input_tokens: int = 0
    output_tokens: int = 0
    #: The phrases that actually fired, when the sorter can name them. The
    #: rules engine always can; a model backend never does, and leaves this
    #: empty rather than inventing one.
    signals: Tuple[str, ...] = ()
    #: What every category scored, so the runners-up can be shown. A verdict
    #: that beat its nearest rival by a hair is a different kind of 90% from
    #: one that beat it by five points, and only this says which.
    scores: Dict[str, float] = field(default_factory=dict)

    @property
    def confidence_percent(self) -> float:
        return round(self.confidence_score * 100.0, 1)

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def category_label(self) -> str:
        """Human label combining both levels of classification."""
        if self.is_job_related:
            return self.category.label
        if self.other_category is OtherCategory.NOT_APPLICABLE:
            return "Not job related"
        return f"Other · {self.other_category.label}"

    @classmethod
    def failure(cls, message: str, model: str = "") -> "Classification":
        return cls(
            summary="Could not be analyzed.",
            is_job_related=False,
            category=Category.UNCLASSIFIED_OTHER,
            other_category=OtherCategory.NOT_APPLICABLE,
            confidence_score=0.0,
            reasoning=message,
            model=model,
            error=message,
        )

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any], model: str = "") -> "Classification":
        """Validate and repair a raw JSON payload from the model.

        The structured-output schema already constrains shape, but this layer
        assumes nothing: it exists so that a schema regression, a proxy, or a
        future model change can never produce a confident-looking result that
        the routing layer would act on.
        """
        adjustments: List[str] = []

        if not isinstance(payload, Mapping):
            return cls.failure(f"Model returned {type(payload).__name__}, expected a JSON object.", model)

        summary = _clean_text(payload.get("summary"))
        reasoning = _clean_text(payload.get("reasoning"))

        raw_job_related = payload.get("is_job_related")
        if isinstance(raw_job_related, bool):
            is_job_related = raw_job_related
        else:
            is_job_related = _coerce_bool(raw_job_related)
            adjustments.append(
                f"is_job_related was {raw_job_related!r} (not a boolean); coerced to {is_job_related}."
            )

        category = Category.parse(payload.get("category"))
        if category is None:
            adjustments.append(
                f"category {payload.get('category')!r} is not in the allowed enum; "
                "forced to UNCLASSIFIED_OTHER."
            )
            category = Category.UNCLASSIFIED_OTHER

        other_category = OtherCategory.parse(payload.get("other_category"))
        if other_category is None:
            if payload.get("other_category") is not None:
                adjustments.append(
                    f"other_category {payload.get('other_category')!r} is not in the allowed "
                    "enum; forced to OTHER."
                )
            other_category = OtherCategory.OTHER if not is_job_related else OtherCategory.NOT_APPLICABLE

        confidence, rescale_note = _coerce_confidence(payload.get("confidence_score"))
        if confidence is None:
            adjustments.append(
                f"confidence_score {payload.get('confidence_score')!r} is not a number in [0, 1]; "
                "forced to 0.0."
            )
            confidence = 0.0
        elif rescale_note:
            adjustments.append(rescale_note)

        # Consistency guard: a message that is not job related cannot carry a
        # job-specific category. Trust the boolean and demote the category.
        if not is_job_related and category is not Category.UNCLASSIFIED_OTHER:
            adjustments.append(
                f"is_job_related=false contradicts category={category.value}; "
                "forced to UNCLASSIFIED_OTHER."
            )
            category = Category.UNCLASSIFIED_OTHER

        # The two levels are mutually exclusive: job mail carries no topic
        # bucket, and non-job mail always carries one.
        if is_job_related and other_category is not OtherCategory.NOT_APPLICABLE:
            adjustments.append(
                f"is_job_related=true contradicts other_category={other_category.value}; "
                "cleared to NOT_APPLICABLE."
            )
            other_category = OtherCategory.NOT_APPLICABLE
        elif not is_job_related and other_category is OtherCategory.NOT_APPLICABLE:
            adjustments.append(
                "is_job_related=false but no other_category was supplied; defaulted to OTHER."
            )
            other_category = OtherCategory.OTHER

        # A confident UNCLASSIFIED_OTHER is still, by definition, unclassified.
        # Cap it so it can never be presented as a confident routing decision.
        if category is Category.UNCLASSIFIED_OTHER and is_job_related and confidence >= DEFAULT_CONFIDENCE_THRESHOLD:
            adjustments.append(
                "UNCLASSIFIED_OTHER cannot be a high-confidence routing decision; "
                "confidence capped below the auto-approval threshold."
            )
            confidence = min(confidence, DEFAULT_CONFIDENCE_THRESHOLD - 0.01)

        if not summary:
            summary = "(the model returned no summary)"
            adjustments.append("summary was empty.")
        if not reasoning:
            reasoning = "(the model returned no reasoning)"
            adjustments.append("reasoning was empty.")

        usage = payload.get("_usage") if isinstance(payload.get("_usage"), Mapping) else {}

        raw_signals = payload.get("signals")
        signals = tuple(str(s) for s in raw_signals[:12]) \
            if isinstance(raw_signals, (list, tuple)) else ()
        raw_scores = payload.get("scores")
        scores: Dict[str, float] = {}
        if isinstance(raw_scores, Mapping):
            for name, value in raw_scores.items():
                try:
                    scores[str(name)] = float(value)
                except (TypeError, ValueError):
                    continue

        return cls(
            summary=summary,
            is_job_related=is_job_related,
            category=category,
            other_category=other_category,
            confidence_score=confidence,
            reasoning=reasoning,
            model=model,
            adjustments=tuple(adjustments),
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            signals=signals,
            scores=scores,
        )


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return re.sub(r"[ \t]+\n", "\n", value).strip()


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "y"}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    return False


def _coerce_confidence(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """Coerce a confidence to [0, 1]. Returns ``(value, adjustment_note)``.

    A value in (1, 100] is read as a percentage rather than clamped to 1.0.
    Clamping would round *up* to maximum confidence, which is exactly the
    direction this application must never guess in.
    """
    if isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip().rstrip("%").strip())
        except ValueError:
            return None, None
    else:
        return None, None

    if math.isnan(number) or math.isinf(number):
        return None, None

    note = None
    if number > 1.0:
        # A clear percentage (2..100, or a whole number) is rescaled. A small
        # overshoot like 1.4 is not: it could equally be a percentage or a
        # sloppy 1.0, and guessing "maximum confidence" is the one direction
        # this application must never guess in. Treat it as unusable instead,
        # which routes the message to Needs Review.
        if number <= 100.0 and (number >= 2.0 or float(number).is_integer()):
            note = (
                f"confidence_score {number:g} looked like a percentage; "
                f"rescaled to {number / 100.0:.2f}."
            )
            number /= 100.0
        else:
            return None, None
    return max(0.0, min(1.0, number)), note


@dataclass
class TriageItem:
    """One row in the approval table: an email plus its routing decision."""

    email: EmailMessage
    classification: Classification
    folders: FolderPlan = field(default_factory=FolderPlan)
    threshold: float = DEFAULT_CONFIDENCE_THRESHOLD
    non_job_routing: NonJobRouting = NonJobRouting.LEAVE
    auto_approve_non_job: bool = False
    approved: Optional[bool] = None
    override_folder: Optional[str] = None
    moved: bool = False
    move_error: Optional[str] = None
    #: Set when the folder above came from a past correction rather than from
    #: the sorter. A ``corrections.Learned``, kept as ``object`` so models has
    #: no import to make - it is only ever read for its ``.because`` sentence.
    learned_from: Optional[object] = None
    #: Set when a rule the user wrote decided where this goes.
    rule_name: str = ""
    #: Which conversation this belongs to, and how many messages are in it.
    thread_key: str = ""
    thread_size: int = 1

    @property
    def in_a_conversation(self) -> bool:
        return self.thread_size > 1

    def __post_init__(self) -> None:
        if self.approved is None:
            self.approved = self.default_approved

    @property
    def learned_because(self) -> str:
        """Why this row is where it is, when a correction put it there."""
        return getattr(self.learned_from, "because", "")

    # -- routing ---------------------------------------------------------
    @property
    def disposition(self) -> Disposition:
        """Where this message goes: filed, held for review, or left alone.

        Whether it is job mail is decided before how sure the sorter is, which
        is the other way round from how this used to read. Needs Review is a
        folder inside the job-search tree, and it means "this is part of your
        job search and I cannot tell which part". A promotion the sorter is
        only 88% sure about is not that. It is simply not job mail, and the
        right place for it is where it already is.
        """
        cls_ = self.classification
        if cls_.error is not None:
            return Disposition.REVIEW

        if not cls_.is_job_related:
            if self.non_job_routing is NonJobRouting.REVIEW:
                return Disposition.REVIEW
            if (self.non_job_routing is NonJobRouting.FILE
                    and cls_.confidence_score >= self.threshold):
                return Disposition.MOVE
            # Either the user wants non-job mail left alone, or the topic is
            # not certain enough to file. Both mean: leave it in the inbox.
            return Disposition.LEAVE

        if cls_.confidence_score < self.threshold:
            return Disposition.REVIEW
        if cls_.category is Category.UNCLASSIFIED_OTHER:
            return Disposition.REVIEW
        return Disposition.MOVE

    @property
    def suggested_folder(self) -> Optional[str]:
        """Folder the app itself suggests, ignoring any manual override."""
        disposition = self.disposition
        if disposition is Disposition.MOVE:
            if self.classification.is_job_related:
                return self.folders.for_category(self.classification.category)
            return self.folders.for_other_category(self.classification.other_category)
        if disposition is Disposition.REVIEW:
            return self.folders.review_folder
        return None

    @property
    def target_folder(self) -> Optional[str]:
        if self.override_folder:
            return self.override_folder
        return self.suggested_folder

    @property
    def default_approved(self) -> bool:
        """High-confidence, self-consistent, job-related mail is pre-checked.

        Non-job mail is never pre-checked unless the user explicitly opts in:
        misfiling a bank alert is a worse outcome than leaving it in the inbox.
        """
        if self.disposition is not Disposition.MOVE:
            return False
        if self.classification.is_job_related:
            return True
        return self.auto_approve_non_job

    @property
    def is_high_confidence(self) -> bool:
        return self.classification.ok and self.classification.confidence_score >= self.threshold

    @property
    def is_actionable(self) -> bool:
        """Can this row be moved at all?"""
        return bool(self.target_folder) and not self.moved

    @property
    def source_label(self) -> str:
        """Where the message already is, shown when nothing will move it."""
        return self.email.source_folder or "Inbox"

    @property
    def override_note(self) -> str:
        """Why this row is not going where the sorter said.

        "Learned" and "manual" both mean the folder was overridden, but they
        are worth telling apart: one is something you did to this message, the
        other is something you did to a previous one.
        """
        if not self.override_folder:
            return ""
        if self.rule_name:
            return f"rule: {self.rule_name}"
        return "learned" if self.learned_from is not None else "manual"

    @property
    def folder_display(self) -> str:
        folder = self.target_folder
        if folder is None:
            return self.source_label
        note = self.override_note
        return f"{folder}  ({note})" if note else folder

    @property
    def folder_short(self) -> str:
        """Just the leaf, so the column is readable at a glance."""
        folder = self.target_folder
        if folder is None:
            return self.source_label
        leaf = folder.rsplit(self.folders.delimiter, 1)[-1]
        note = self.override_note
        return f"{leaf}  ({note})" if note else leaf

    @property
    def status_display(self) -> str:
        if self.move_error:
            return f"Failed: {self.move_error}"
        if self.moved:
            return "Moved"
        if self.classification.error:
            return "Analysis failed"
        if self.left_because_not_job:
            # "Leave in place" reads as a fact about the message. It is a
            # setting, and somebody staring at a row that will not tick
            # deserves to be told which one.
            return "Not job mail - not sorted"
        if self.held_back_by_confidence:
            return "Not sure enough to file"
        return self.disposition.label

    @property
    def topic_is_sorted(self) -> bool:
        """Whether this configuration files mail on this topic anywhere.

        Job mail always is. An everyday topic is only when non-job mail is
        being filed *and* that topic has a folder of its own - a topic left
        out of the list goes to Other, and the app is not really acting on it.
        """
        if self.classification.is_job_related:
            return True
        if self.non_job_routing is not NonJobRouting.FILE:
            return False
        topics = self.folders.topics
        return not topics or self.classification.other_category in topics

    @property
    def left_because_not_job(self) -> bool:
        """Whether this row is inert only because non-job mail is left alone.

        The distinction that matters to a person: there is nothing wrong with
        this message and nothing wrong with the analysis. It is sitting still
        because of a choice, and the choice can be changed in one click.
        """
        return (self.disposition is Disposition.LEAVE
                and not self.classification.is_job_related
                and self.classification.error is None
                and self.non_job_routing is NonJobRouting.LEAVE)

    @property
    def held_back_by_confidence(self) -> bool:
        """Non-job mail that would be filed if the sorter were surer.

        The second reason a row sits still, and the one that looks most like
        a bug: the setting says file it, and it is not being filed.
        """
        return (self.disposition is Disposition.LEAVE
                and not self.classification.is_job_related
                and self.classification.error is None
                and self.non_job_routing is NonJobRouting.FILE
                and self.classification.confidence_score < self.threshold)

    @property
    def why_not_actionable(self) -> str:
        """One sentence on why this row cannot be ticked, or empty."""
        if self.is_actionable or self.moved:
            return ""
        # A failed analysis is not one of these: it routes to Needs Review,
        # which is a folder, so such a row can be ticked like any other.
        if self.left_because_not_job:
            return ("This is not job mail, and non-job mail is being left "
                    "where it is. Change that under Sorting.")
        if self.held_back_by_confidence:
            return (f"The sorter was {self.classification.confidence_percent:.0f}% "
                    f"sure and files non-job mail at "
                    f"{self.threshold * 100:.0f}%. Move it by hand below, or "
                    "lower the threshold in Settings.")
        return "There is no folder for this message."


@dataclass(frozen=True)
class TriageSummary:
    """Aggregate counts shown in the status bar."""

    total: int = 0
    job_related: int = 0
    to_move: int = 0
    needs_review: int = 0
    leave_in_place: int = 0
    approved: int = 0
    errors: int = 0
    moved: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @classmethod
    def build(cls, items: Sequence["TriageItem"]) -> "TriageSummary":
        counts = {
            Disposition.MOVE: 0,
            Disposition.REVIEW: 0,
            Disposition.LEAVE: 0,
        }
        job_related = approved = errors = moved = 0
        input_tokens = output_tokens = 0
        for item in items:
            counts[item.disposition] += 1
            if item.classification.is_job_related:
                job_related += 1
            if item.approved:
                approved += 1
            if item.classification.error:
                errors += 1
            if item.moved:
                moved += 1
            input_tokens += item.classification.input_tokens
            output_tokens += item.classification.output_tokens
        return cls(
            total=len(items),
            job_related=job_related,
            to_move=counts[Disposition.MOVE],
            needs_review=counts[Disposition.REVIEW],
            leave_in_place=counts[Disposition.LEAVE],
            approved=approved,
            errors=errors,
            moved=moved,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )

    def describe(self) -> str:
        parts = [
            f"{self.total} message{'s' if self.total != 1 else ''}",
            f"{self.job_related} job-related",
            f"{self.to_move} ready to file",
            f"{self.needs_review} need review",
        ]
        if self.leave_in_place:
            parts.append(f"{self.leave_in_place} left in place")
        if self.errors:
            parts.append(f"{self.errors} failed")
        if self.moved:
            parts.append(f"{self.moved} moved")
        parts.append(f"{self.approved} selected")
        return "  ·  ".join(parts)
