"""Drafting replies, and deciding which messages deserve one.

Deliberately conservative in two ways.

Nothing is ever sent. A draft is written to the account's Drafts mailbox with
the right headers to thread correctly, and a person presses send. An app that
answers a stranger's mail on your behalf, using a model, without you reading it
first, is not a feature anybody asked for twice.

And a rule has to match on something specific. The default rules cover the
cases where the correct reply is nearly mechanical - acknowledging an interview
invitation, answering a request for availability - and every one of them is off
until it is switched on.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from email.message import EmailMessage as MimeMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from models import Category, EmailMessage, OtherCategory

#: What a rule can do when it matches.
ACTIONS: Tuple[Tuple[str, str], ...] = (
    ("draft", "Save a draft reply for me to review"),
    ("draft_ai", "Write a draft with the model, for me to review"),
    ("none", "Do nothing (off)"),
)

MAX_DRAFT_WORDS = 180


@dataclass
class Rule:
    """One "when this arrives, draft that" rule."""

    name: str = "New rule"
    enabled: bool = False
    #: Job categories this applies to, by name. Empty means any.
    categories: List[str] = field(default_factory=list)
    #: Everyday topics this applies to, by name. Empty means any.
    topics: List[str] = field(default_factory=list)
    #: A phrase that must appear in the subject or body. Optional.
    contains: str = ""
    #: Only from senders whose address matches this. Optional, substring.
    sender_matches: str = ""
    #: Never reply to bulk mail. On by default, and it is why this is safe.
    skip_bulk: bool = True
    #: Below this the message is not confidently understood, so it is left.
    min_confidence: float = 0.90
    action: str = "draft"
    #: Used verbatim by "draft", and as guidance by "draft_ai".
    template: str = ""
    #: Extra instruction for the model, when the action is draft_ai.
    guidance: str = ""

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip() or "New rule"
        self.action = self.action if self.action in dict(ACTIONS) else "draft"
        try:
            self.min_confidence = min(1.0, max(0.0, float(self.min_confidence)))
        except (TypeError, ValueError):
            self.min_confidence = 0.90
        self.categories = [str(c) for c in (self.categories or [])]
        self.topics = [str(t) for t in (self.topics or [])]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Rule":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})

    # -- matching --------------------------------------------------------
    def matches(self, message: EmailMessage, classification) -> Tuple[bool, str]:
        """Whether this rule applies, and why not when it does not."""
        if not self.enabled or self.action == "none":
            return False, "the rule is off"
        if self.skip_bulk and (message.list_unsubscribe or "").strip():
            return False, "it is bulk mail"
        if classification.confidence_score < self.min_confidence:
            return False, (f"confidence {classification.confidence_score:.2f} is "
                           f"below the rule's {self.min_confidence:.2f}")
        if self.categories:
            name = classification.category.value if classification.is_job_related else ""
            if name not in self.categories:
                return False, "the category does not match"
        if self.topics:
            name = "" if classification.is_job_related else classification.other_category.value
            if name not in self.topics:
                return False, "the topic does not match"
        if self.sender_matches:
            haystack = f"{message.sender_email} {message.sender_name}".lower()
            if self.sender_matches.strip().lower() not in haystack:
                return False, "the sender does not match"
        if self.contains:
            haystack = f"{message.subject} {message.body_text}".lower()
            if self.contains.strip().lower() not in haystack:
                return False, "the phrase was not found"
        return True, ""


#: Shipped switched off. Each one is a case where the right reply is nearly
#: mechanical, which is the only kind worth automating.
def default_rules() -> List[Rule]:
    return [
        Rule(
            name="Acknowledge an interview invitation",
            categories=[Category.INTERVIEW.value],
            action="draft",
            min_confidence=0.92,
            template=(
                "Hello {first_name},\n\n"
                "Thank you for the invitation. I would be glad to meet.\n\n"
                "I am free on the times you suggested, and happy to work around "
                "whatever suits the panel.\n\n"
                "Best wishes,\n{me}"
            ),
        ),
        Rule(
            name="Reply to a request for documents or availability",
            categories=[Category.NEXT_STEPS.value],
            action="draft_ai",
            min_confidence=0.92,
            guidance=(
                "Answer what was actually asked for. If the message asks for "
                "documents, say which are attached. If it asks for times, offer "
                "three across two working days. Do not invent facts about the "
                "sender, the role, or the writer's history."
            ),
        ),
        Rule(
            name="Thank a recruiter and decline politely",
            categories=[Category.UNSOLICITED.value],
            action="draft",
            min_confidence=0.94,
            template=(
                "Hello {first_name},\n\n"
                "Thank you for getting in touch. I am not looking to move at "
                "the moment, but I am glad to stay in contact for the future.\n\n"
                "Best wishes,\n{me}"
            ),
        ),
    ]


SYSTEM_PROMPT = """\
You draft replies to email. You are writing on behalf of the person whose \
mailbox this is, and they will read and edit what you write before anything is \
sent.

Rules you do not break:
- Never invent a fact. Not a date, not a name, not a piece of the writer's \
history, not a qualification. If something is needed and you do not have it, \
leave a short bracketed note such as [confirm date] for the writer to fill in.
- Never agree to anything specific on the writer's behalf. Offering to meet is \
fine; committing to a salary, a start date or a deliverable is not.
- Match the register of the message you are answering.
- Be brief. Under 180 words, and usually far less.
- Write only the body. No subject line, no signature block beyond the name, no \
commentary about what you have written.
"""

DRAFT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "body": {
            "type": "string",
            "description": "The reply body, plain text, under 180 words.",
        },
        "needs_from_writer": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Anything the writer must fill in before sending.",
        },
    },
    "required": ["body", "needs_from_writer"],
    "additionalProperties": False,
}


@dataclass
class Draft:
    """A reply that has been written but not sent."""

    message_uid: str
    account_id: str
    to: str
    subject: str
    body: str
    rule_name: str = ""
    needs_from_writer: List[str] = field(default_factory=list)
    generated_by: str = "template"
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.body.strip())


def _first_name(message: EmailMessage) -> str:
    name = (message.sender_name or "").strip()
    if not name:
        local = (message.sender_email or "").split("@")[0]
        name = re.sub(r"[._-]+", " ", local).strip()
    first = name.split()[0] if name else ""
    # "Careers", "no-reply" and similar are not names.
    if first.lower() in {"no", "noreply", "no-reply", "careers", "recruiting",
                         "talent", "hr", "team", "info", "hello", "donotreply"}:
        return "there"
    return first or "there"


def reply_subject(subject: str) -> str:
    subject = (subject or "").strip()
    return subject if subject[:3].lower() == "re:" else f"Re: {subject}".strip()


def reply_to_address(message: EmailMessage) -> str:
    """Where a reply should go: Reply-To if there is one, else the sender."""
    candidate = (message.reply_to or "").strip() or (message.sender_email or "").strip()
    parsed = getaddresses([candidate])
    return parsed[0][1] if parsed and parsed[0][1] else candidate


def render_template(template: str, message: EmailMessage, me: str) -> str:
    """Fill a template. Unknown fields are left alone rather than exploding."""
    values = {
        "first_name": _first_name(message),
        "sender": message.sender_short,
        "subject": message.subject_display,
        "me": me or "",
    }

    def replace(match: "re.Match") -> str:
        key = match.group(1)
        return values.get(key, match.group(0))

    return re.sub(r"\{(\w+)\}", replace, template or "")


def build_mime(draft: Draft, from_address: str, from_name: str = "",
               in_reply_to: str = "", references: str = "") -> bytes:
    """A reply, threaded correctly, ready to be appended to Drafts."""
    mime = MimeMessage()
    mime["From"] = formataddr((from_name or "", from_address))
    mime["To"] = draft.to
    mime["Subject"] = draft.subject
    mime["Date"] = formatdate(localtime=True)
    mime["Message-ID"] = make_msgid()
    if in_reply_to:
        mime["In-Reply-To"] = in_reply_to
        mime["References"] = (references + " " + in_reply_to).strip()
    body = draft.body.rstrip()
    if draft.needs_from_writer:
        body += "\n\n--\nBefore sending, check: " + "; ".join(draft.needs_from_writer)
    mime.set_content(body + "\n")
    return mime.as_bytes()


def choose_rule(rules: Sequence[Rule], message: EmailMessage,
                classification) -> Tuple[Optional[Rule], str]:
    """The first rule that applies, and the reason none did when none do."""
    reasons = []
    for rule in rules:
        ok, why = rule.matches(message, classification)
        if ok:
            return rule, ""
        if rule.enabled and rule.action != "none":
            reasons.append(f"{rule.name}: {why}")
    return None, "; ".join(reasons[:3])


def draft_for(rule: Rule, message: EmailMessage, classification,
              me: str = "", engine=None) -> Draft:
    """Write the reply this rule calls for. Never raises."""
    draft = Draft(
        message_uid=message.uid,
        account_id=message.account_id,
        to=reply_to_address(message),
        subject=reply_subject(message.subject),
        body="",
        rule_name=rule.name,
    )
    if not draft.to:
        draft.error = "No address to reply to."
        return draft

    if rule.action == "draft" or engine is None:
        draft.body = render_template(rule.template, message, me).strip()
        draft.generated_by = "template"
        if not draft.body:
            draft.error = "The rule has no template to fill in."
        # A template with a bracketed gap still needs the writer.
        draft.needs_from_writer = re.findall(r"\[([^\]]{2,60})\]", draft.body)
        return draft

    try:
        payload = engine.draft_reply(message, classification, rule, me)
    except Exception as exc:  # noqa: BLE001 - reported, never raised
        draft.error = f"The model could not draft a reply: {exc}"
        return draft
    draft.body = str(payload.get("body", "")).strip()
    draft.needs_from_writer = [
        str(item) for item in (payload.get("needs_from_writer") or [])
    ][:6]
    draft.generated_by = "model"
    if not draft.body:
        draft.error = "The model returned an empty reply."
    return draft
