"""Drafting replies, and deciding which messages deserve one.

Deliberately conservative in two ways.

Nothing is ever sent. A draft is written to the account's Drafts mailbox with
the right headers to thread correctly, and a person presses send. An app that
answers a stranger's mail on your behalf, using a model, without you reading it
first, is not a feature anybody asked for twice.

And a rule has to match on something specific. A rule is a list of conditions
and a list of things to do, so anybody can build one that fits their own mail
rather than picking from a fixed menu. The rules that ship cover the cases
where the correct reply is nearly mechanical - acknowledging an interview
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

MAX_DRAFT_WORDS = 180

#: How much of a message a condition reads. A rule runs over every scanned
#: message, and somebody's own regular expression is allowed to be careless.
MAX_MATCH_CHARS = 20000


# ==========================================================================
# Conditions and actions
# ==========================================================================
#: What a condition can look at. Each is (name, label, kind), where kind says
#: what sort of value the field expects and so which editor the settings page
#: shows for it.
FIELDS: Tuple[Tuple[str, str, str], ...] = (
    ("category", "Job category", "category"),
    ("topic", "Everyday topic", "topic"),
    ("sender", "Sender", "text"),
    ("sender_domain", "Sender's domain", "text"),
    ("subject", "Subject", "text"),
    ("body", "Message text", "text"),
    ("anywhere", "Subject or message", "text"),
    ("confidence", "Confidence", "number"),
    ("mailbox", "Mailbox", "mailbox"),
    ("age_days", "Age in days", "number"),
    ("is_bulk", "Bulk mail", "flag"),
    ("has_attachment", "Has an attachment", "flag"),
    ("is_reply", "Is a reply", "flag"),
)
_FIELD_KIND = {name: kind for name, _label, kind in FIELDS}

#: What each field actually looks at, for the help text beside it.
FIELD_HELP: Dict[str, str] = {
    "category": "Which part of a job search this is. Empty for everyday mail, "
                "so a job rule cannot fire on a receipt.",
    "topic": "What kind of everyday mail this is. Empty for job mail, for the "
             "same reason.",
    "sender": "The display name and the address together: “Dana Reyes "
              "dana@northwind.example”.",
    "sender_domain": "Everything after the last @. Use “ends with” to catch a "
                     "company and all its subdomains.",
    "subject": "The subject line as it arrived, decoded.",
    "body": "The message text, with quoted history and signatures already "
            "taken out. The first 20,000 characters.",
    "anywhere": "The subject and the message text together.",
    "confidence": "How sure the sorter is, from 0 to 1. Anything above 0.9 is "
                  "a strong signal; below 0.7 it is guessing.",
    "mailbox": "Which account it arrived in. Matches the address, the label "
               "or the internal id.",
    "age_days": "How long ago it arrived, counted from now rather than from "
                "the start of the scan.",
    "is_bulk": "Whether it carries an unsubscribe header: a newsletter, a "
               "mailing list, a marketing send.",
    "has_attachment": "Whether anything was attached.",
    "is_reply": "Whether the subject starts with Re:.",
}

#: What each action does, and what it does not do.
ACTION_HELP: Dict[str, str] = {
    "draft": "Fills your template in and saves it to Drafts. Nothing is sent.",
    "draft_ai": "Hands the message to the model with your guidance and saves "
                "what comes back to Drafts. Nothing is sent.",
    "file_into": "Points the row at this folder. Nothing moves until you "
                 "press Apply.",
    "tick": "Ticks the row, so Apply will file it.",
    "untick": "Unticks the row, so Apply will skip it.",
    "mark_read": "Marks it read on the server, straight away.",
    "flag": "Flags it on the server, straight away.",
    "bin_it": "Points the row at the To Delete folder and ticks it, so "
              "Apply moves it there. Nothing is deleted until you empty "
              "that folder yourself.",
    "leave": "Cancels any folder an earlier rule chose for it.",
    "stop": "Skips every later rule for this message.",
}

#: Which operators make sense for which kind of field.
OPERATORS: Tuple[Tuple[str, str, Tuple[str, ...]], ...] = (
    ("is", "is", ("category", "topic", "mailbox")),
    ("is_not", "is not", ("category", "topic", "mailbox")),
    ("contains", "contains", ("text",)),
    ("not_contains", "does not contain", ("text",)),
    ("starts_with", "starts with", ("text",)),
    ("ends_with", "ends with", ("text",)),
    ("equals", "is exactly", ("text",)),
    ("not_equals", "is not exactly", ("text",)),
    ("matches", "matches the pattern", ("text",)),
    ("at_least", "is at least", ("number",)),
    ("at_most", "is at most", ("number",)),
    ("is_true", "yes", ("flag",)),
    ("is_false", "no", ("flag",)),
)


#: An unbounded repeat: one that can try an unlimited number of lengths.
_UNBOUNDED = ("*", "+")


def _atoms(pattern: str) -> List[Tuple[str, str]]:
    """Split a pattern into (atom, quantifier) pairs, groups kept whole.

    Not a parser - it only needs to be right about where the repeats are and
    what they repeat, which is all the risk check asks of it.
    """
    found: List[Tuple[str, str]] = []
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\":
            atom, index = pattern[index:index + 2], index + 2
        elif char == "[":
            close = index + 1
            if close < len(pattern) and pattern[close] == "]":
                close += 1
            while close < len(pattern) and pattern[close] != "]":
                close += 2 if pattern[close] == "\\" else 1
            atom, index = pattern[index:close + 1], close + 1
        elif char == "(":
            depth, close = 1, index + 1
            while close < len(pattern) and depth:
                if pattern[close] == "\\":
                    close += 1
                elif pattern[close] == "(":
                    depth += 1
                elif pattern[close] == ")":
                    depth -= 1
                close += 1
            atom, index = pattern[index:close], close
        else:
            atom, index = char, index + 1

        quantifier = ""
        if index < len(pattern):
            if pattern[index] in "*+?":
                quantifier, index = pattern[index], index + 1
            elif pattern[index] == "{":
                close = pattern.find("}", index)
                if close != -1:
                    quantifier, index = pattern[index:close + 1], close + 1
        if quantifier and index < len(pattern) and pattern[index] in "?+":
            quantifier += pattern[index]     # lazy or possessive
            index += 1
        found.append((atom, quantifier))
    return found


def _is_unbounded(quantifier: str) -> bool:
    if not quantifier:
        return False
    if quantifier[0] in _UNBOUNDED:
        return True
    return quantifier.startswith("{") and quantifier.rstrip("}?+").endswith(",")


#: Representative characters, used to ask whether two branches of an
#: alternation can match the same thing.
_PROBES = "axzAZ09 _-.@/\\\n\t!#%&*+=?~é'\""


def _branches(group: str) -> List[str]:
    """The top-level alternatives inside a group, ignoring nested ones."""
    inner = group[1:-1] if group.startswith("(") and group.endswith(")") else group
    for prefix in ("?:", "?i:", "?=", "?!", "?<=", "?<!", "?P<"):
        if inner.startswith(prefix):
            inner = inner.split(">", 1)[1] if prefix == "?P<" else inner[len(prefix):]
            break
    parts, depth, current = [], 0, []
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\":
            current.append(inner[index:index + 2]); index += 2; continue
        if char == "[":
            close = inner.find("]", index + 2)
            close = len(inner) - 1 if close == -1 else close
            current.append(inner[index:close + 1]); index = close + 1; continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char == "|" and depth == 0:
            parts.append("".join(current)); current = []; index += 1; continue
        current.append(char); index += 1
    parts.append("".join(current))
    return parts


def _branches_overlap(group: str) -> bool:
    """Whether two alternatives can begin with the same character.

    ``(a|a)*`` and ``(\\d|\\w)+`` are exponential for the same reason ``(a+)+``
    is: at every position the matcher has more than one way to make progress,
    and has to try all of them before it can give up. Asking each branch's
    first atom about a handful of representative characters answers that
    without writing a regular-expression engine.
    """
    parts = [p for p in _branches(group) if p]
    if len(parts) < 2:
        return False
    firsts = []
    for part in parts[:8]:
        first = (_atoms(part) or [("", "")])[0][0]
        if not first:
            return True                     # an empty branch always overlaps
        firsts.append(first)
    # Two branches that begin with the same thing overlap by definition, and
    # saying so does not depend on a probe character happening to be listed.
    if len(set(firsts)) < len(firsts):
        return True
    reach = []
    for first in firsts:
        try:
            probe = re.compile(first)
        except re.error:
            return True                     # cannot tell, so assume it can
        reach.append({c for c in _PROBES if probe.match(c)})
    return any(reach[i] & reach[j]
               for i in range(len(reach)) for j in range(i + 1, len(reach)))


#: Answers, keyed by the pattern. Checking is linear in the length of the
#: pattern, and a rule is checked against every message in a scan.
_RISK: Dict[str, str] = {}


def pattern_risk(pattern: str) -> str:
    """Why this pattern could take an unreasonable amount of time, in words.

    Python's regular expressions backtrack, and two shapes make that
    catastrophic: a repeat inside a repeat, and two repeats of the same thing
    side by side. Neither is rare in a pattern somebody wrote quickly -
    ``.*.*x`` is what happens when you paste twice - and neither can be
    interrupted, because the matcher holds the interpreter for its whole run.
    So they are refused before they run rather than cancelled during it.
    """
    if not pattern:
        return ""
    if pattern in _RISK:
        return _RISK[pattern]
    if len(_RISK) > 200:
        _RISK.clear()
    _RISK[pattern] = found = _pattern_risk(pattern)
    return found


def _pattern_risk(pattern: str) -> str:
    try:
        atoms = _atoms(pattern)
    except Exception:  # noqa: BLE001 - a bad pattern is caught by compile
        return ""
    previous_atom, previous_repeat = "", False
    for atom, quantifier in atoms:
        unbounded = _is_unbounded(quantifier)
        # A group that repeats without limit, holding something that already
        # repeats without limit: (a+)+, (a*)* and friends.
        if unbounded and atom.startswith("("):
            inner = atom[1:-1] if atom.endswith(")") else atom[1:]
            if any(_is_unbounded(q) for _a, q in _atoms(inner)):
                return (f"“{atom}{quantifier}” repeats something that already "
                        "repeats, which can take longer than the age of the "
                        "universe on the wrong message")
            if _branches_overlap(atom):
                return (f"“{atom}{quantifier}” repeats a choice whose options "
                        "can match the same text, which is the same trap by "
                        "another name")
        # The same thing repeated twice in a row: .*.*
        if unbounded and previous_repeat and atom == previous_atom:
            return (f"“{previous_atom}{quantifier}” appears twice in a row, "
                    "which multiplies the work rather than adding to it")
        previous_atom, previous_repeat = atom, unbounded
    return ""


#: Compiled patterns, keyed by what was typed. A rule is checked against every
#: message in a scan, and re's own cache is small and shared with everything.
_COMPILED: Dict[str, Any] = {}


def _prepare(pattern: str) -> str:
    """Take off the leading and trailing “.*”, which a search does not need.

    ``.*urgent.*`` is a reasonable thing to type and a quadratic thing to run:
    the matcher takes every starting position in turn, runs to the end of the
    message and walks back looking for the word. Under ``search`` those two
    repeats say nothing the search was not already doing, and without them the
    same pattern is linear. Two seconds a message becomes half a millisecond.
    """
    trimmed = pattern
    while (trimmed.startswith(".*") and not trimmed.startswith(".*?")):
        trimmed = trimmed[2:]
    while (trimmed.endswith(".*") and not trimmed.endswith("\\.*")):
        trimmed = trimmed[:-2]
    if trimmed.endswith(".*$"):
        trimmed = trimmed[:-3]
    return trimmed or pattern


def compiled(pattern: str):
    """The compiled form of a pattern, prepared and remembered. None if bad."""
    if pattern in _COMPILED:
        return _COMPILED[pattern]
    try:
        made = re.compile(_prepare(pattern), re.IGNORECASE)
    except re.error:
        made = None
    if len(_COMPILED) > 200:
        _COMPILED.clear()
    _COMPILED[pattern] = made
    return made


def _uncapitalise(text: str) -> str:
    """Lower the first letter only, so “Sorted Mail/Work” survives."""
    return f"{text[:1].lower()}{text[1:]}" if text else text


def operators_for(field: str) -> Tuple[Tuple[str, str], ...]:
    """The operators offered for a field, in the order they are listed."""
    kind = _FIELD_KIND.get(field, "text")
    return tuple((name, label) for name, label, kinds in OPERATORS if kind in kinds)


def field_kind(field: str) -> str:
    return _FIELD_KIND.get(field, "text")


@dataclass
class Condition:
    """One test against a message."""

    field: str = "anywhere"
    operator: str = "contains"
    value: str = ""

    #: Where an operator goes when the field it was written for does not
    #: offer it - "is" on a subject means "is exactly", not "whichever
    #: operator happened to come first".
    SYNONYMS = {"is": "equals", "is_not": "not_equals",
                "equals": "is", "not_equals": "is_not",
                "at_least": "contains", "at_most": "contains",
                "is_true": "contains", "is_false": "not_contains"}

    def __post_init__(self) -> None:
        if self.field not in _FIELD_KIND:
            self.field = "anywhere"
        offered = [name for name, _label in operators_for(self.field)]
        if self.operator not in offered:
            fallback = self.SYNONYMS.get(self.operator, "")
            self.operator = (fallback if fallback in offered
                             else (offered[0] if offered else "contains"))
        self.value = "" if self.value is None else str(self.value)

    def describe(self) -> str:
        label = next((l for n, l, _k in FIELDS if n == self.field), self.field)
        operator = next((l for n, l in operators_for(self.field)
                         if n == self.operator), self.operator)
        if field_kind(self.field) == "flag":
            return f"{label}: {operator}"
        return f"{label} {operator} “{self.value}”"

    # -- evaluation ------------------------------------------------------
    def _subject_of(self, message, classification, context) -> Any:
        if self.field == "category":
            return classification.category.value if classification.is_job_related else ""
        if self.field == "topic":
            return ("" if classification.is_job_related
                    else classification.other_category.value)
        if self.field == "sender":
            return f"{message.sender_name} {message.sender_email}"
        if self.field == "sender_domain":
            return (message.sender_email or "").rpartition("@")[2]
        if self.field == "subject":
            return message.subject or ""
        if self.field == "body":
            return message.body_text or ""
        if self.field == "anywhere":
            return f"{message.subject or ''}\n{message.body_text or ''}"
        if self.field == "confidence":
            return classification.confidence_score
        if self.field == "mailbox":
            return " ".join(part for part in (message.account_id,
                                              message.account_address,
                                              message.account_label) if part)
        if self.field == "age_days":
            when = message.local_date()
            if when is None:
                return 0.0
            now = (context or {}).get("now") or datetime.now(timezone.utc)
            return max(0.0, (now.astimezone(when.tzinfo) - when).total_seconds() / 86400)
        if self.field == "is_bulk":
            return bool((message.list_unsubscribe or "").strip())
        if self.field == "has_attachment":
            return bool(message.attachments)
        if self.field == "is_reply":
            return (message.subject or "").strip()[:3].lower() == "re:"
        return ""

    def matches(self, message, classification, context=None) -> bool:
        """Whether this condition holds. Never raises on a bad value."""
        subject = self._subject_of(message, classification, context)
        operator = self.operator

        if operator in ("is_true", "is_false"):
            return bool(subject) is (operator == "is_true")

        if operator in ("at_least", "at_most"):
            try:
                threshold = float(self.value)
                actual = float(subject)
            except (TypeError, ValueError):
                return False
            return actual >= threshold if operator == "at_least" else actual <= threshold

        haystack = str(subject).lower()[:MAX_MATCH_CHARS]
        needle = str(self.value).strip().lower()
        if operator in ("is", "is_not"):
            parts = haystack.split() if self.field == "mailbox" else [haystack]
            hit = needle in parts or haystack == needle
            return hit if operator == "is" else not hit
        if not needle:
            # An empty text test would match everything, which is never what
            # somebody typing a rule meant to say.
            return False
        if operator == "contains":
            return needle in haystack
        if operator == "not_contains":
            return needle not in haystack
        if operator == "starts_with":
            return haystack.startswith(needle)
        if operator == "ends_with":
            return haystack.endswith(needle)
        if operator == "equals":
            return haystack.strip() == needle
        if operator == "not_equals":
            return haystack.strip() != needle
        if operator == "matches":
            if pattern_risk(self.value):
                return False        # refused, not run: see pattern_risk
            pattern = compiled(self.value)
            if pattern is None:
                return False
            return pattern.search(str(subject)[:MAX_MATCH_CHARS]) is not None
        return False

    def problem(self) -> str:
        """What is wrong with this condition, in words. Empty when it is fine."""
        if field_kind(self.field) == "flag":
            return ""
        if not str(self.value).strip():
            return f"{self.describe()} has nothing to compare against"
        if self.operator in ("at_least", "at_most"):
            try:
                float(self.value)
            except (TypeError, ValueError):
                return f"“{self.value}” is not a number"
        if self.operator == "matches":
            try:
                re.compile(self.value)
            except re.error as exc:
                return f"that pattern will not compile: {exc}"
            risk = pattern_risk(self.value)
            if risk:
                return f"that pattern is unsafe to run: {risk}"
        return ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Condition":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})


#: What a rule can do. Ordered as they would be applied.
ACTION_KINDS: Tuple[Tuple[str, str, str], ...] = (
    ("draft", "Draft a reply from a template", "template"),
    ("draft_ai", "Draft a reply with the model", "guidance"),
    ("file_into", "File it into a folder", "folder"),
    ("tick", "Tick it, ready to file", "none"),
    ("untick", "Leave it unticked", "none"),
    ("bin_it", "Put it in the To Delete folder", "none"),
    ("mark_read", "Mark it as read", "none"),
    ("flag", "Flag it", "none"),
    ("leave", "Leave it where it is", "none"),
    ("stop", "Stop, and skip any later rules", "none"),
)
_ACTION_INPUT = {name: kind for name, _label, kind in ACTION_KINDS}


def action_input(kind: str) -> str:
    """What a given action needs typing into it, if anything."""
    return _ACTION_INPUT.get(kind, "none")


@dataclass
class Action:
    """One thing a rule does when it matches."""

    kind: str = "draft"
    value: str = ""

    def __post_init__(self) -> None:
        # An action nobody has ever heard of is dropped by the rule rather
        # than guessed at. Guessing "draft" would have a mangled config write
        # mail; dropping it does nothing, which is the right way to be wrong.
        if self.kind not in _ACTION_INPUT:
            self.kind = ""
        self.value = "" if self.value is None else str(self.value)

    def describe(self) -> str:
        label = next((l for n, l, _k in ACTION_KINDS if n == self.kind), self.kind)
        if action_input(self.kind) == "folder" and self.value:
            return f"{label}: {self.value}"
        return label

    def problem(self) -> str:
        """What this action still needs, in words. Empty when it is fine."""
        needs = action_input(self.kind)
        if needs == "none" or str(self.value).strip():
            return ""
        return {
            "template": "the template to draft from is empty",
            "guidance": "the model has not been told what the reply should do",
            "folder": "no folder was chosen to file into",
        }.get(needs, "it is missing a value")

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Action":
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in (raw or {}).items() if k in known})

@dataclass
class Rule:
    """When these conditions hold, do these things.

    Conditions and actions are lists, so a rule is whatever somebody needs it
    to be rather than one of a fixed set. The older shape - a category, a
    phrase, a confidence floor and one action - is still read, and converted
    on the way in, so an existing configuration keeps working.
    """

    name: str = "New rule"
    enabled: bool = False
    #: "all" means every condition has to hold; "any" means one is enough.
    match: str = "all"
    conditions: List[Condition] = field(default_factory=list)
    actions: List[Action] = field(default_factory=list)
    #: Never act on bulk mail. On by default, and the reason this is safe.
    skip_bulk: bool = True
    #: Stop looking at later rules once this one has matched.
    stop_after: bool = False

    def __post_init__(self) -> None:
        self.name = (self.name or "").strip() or "New rule"
        self.match = "any" if str(self.match).lower() == "any" else "all"
        self.conditions = [
            c if isinstance(c, Condition) else Condition.from_dict(c)
            for c in (self.conditions or []) if isinstance(c, (Condition, Mapping))
        ]
        self.actions = [
            action for action in (
                a if isinstance(a, Action) else Action.from_dict(a)
                for a in (self.actions or []) if isinstance(a, (Action, Mapping))
            ) if action.kind
        ]

    #: Actions that only rearrange the table. They need no network, write
    #: nothing to the server, and can therefore run at the end of every scan
    #: rather than waiting for somebody to ask for replies.
    SORTING_ACTIONS = frozenset({"file_into", "tick", "untick", "bin_it",
                                 "leave", "stop"})

    # -- what it is -------------------------------------------------------
    @property
    def drafts_a_reply(self) -> bool:
        return any(a.kind in ("draft", "draft_ai") for a in self.actions)

    @property
    def sorts_only(self) -> bool:
        """Whether this rule just files and ticks.

        A rule that flags a message or marks it read has to open the mailbox,
        and a rule that drafts has to talk to a model; neither belongs in the
        tail of a scan. One that only points a row at a folder does.
        """
        return bool(self.actions) and all(
            a.kind in self.SORTING_ACTIONS for a in self.actions)

    @property
    def uses_the_model(self) -> bool:
        return any(a.kind == "draft_ai" for a in self.actions)

    def action(self, kind: str) -> Optional[Action]:
        return next((a for a in self.actions if a.kind == kind), None)

    def describe(self) -> str:
        """One line saying what this rule does, for the list."""
        if not self.conditions:
            return "every message"
        joiner = " and " if self.match == "all" else " or "
        conditions = joiner.join(c.describe() for c in self.conditions[:3])
        if len(self.conditions) > 3:
            conditions += f", and {len(self.conditions) - 3} more"
        doing = ", ".join(_uncapitalise(a.describe())
                          for a in self.actions[:3]) or "nothing"
        if len(self.actions) > 3:
            doing += f", and {len(self.actions) - 3} more"
        return f"{conditions} → {doing}"

    # -- matching ---------------------------------------------------------
    def matches(self, message, classification, context=None) -> Tuple[bool, str]:
        """Whether this rule applies, and why not when it does not."""
        if not self.enabled:
            return False, "the rule is off"
        if not self.actions:
            return False, "the rule does not do anything yet"
        if self.skip_bulk and (message.list_unsubscribe or "").strip():
            return False, "it is bulk mail"
        if not self.conditions:
            return False, "the rule has no conditions, so it would match everything"

        results = [c.matches(message, classification, context) for c in self.conditions]
        if self.match == "any":
            if any(results):
                return True, ""
            return False, "none of its conditions matched"
        if all(results):
            return True, ""
        missed = next((c for c, ok in zip(self.conditions, results) if not ok), None)
        if missed is None:
            return False, "it did not match"
        # The reason is read mid-sentence, after "rule name: ", so it starts
        # in lower case like the rest of them.
        said = missed.describe()
        return False, f"{said[0].lower()}{said[1:]} did not hold"

    def problems(self) -> List[str]:
        """Everything wrong with this rule, in words, worst first.

        The settings page shows these next to the rule rather than refusing to
        save it, because a half-written rule is a normal state to leave a
        rule in. A rule with problems is simply never switched on.
        """
        found: List[str] = []
        if not self.conditions:
            found.append("It has no conditions, so it would match every message.")
        if not self.actions:
            found.append("It does not do anything yet.")
        for condition in self.conditions:
            trouble = condition.problem()
            if trouble:
                found.append(trouble[0].upper() + trouble[1:] + ".")
        for action in self.actions:
            trouble = action.problem()
            if trouble:
                found.append(trouble[0].upper() + trouble[1:] + ".")
        kinds = [a.kind for a in self.actions]
        if "tick" in kinds and "untick" in kinds:
            found.append("It both ticks and unticks the message; the last one wins.")
        if "leave" in kinds and "file_into" in kinds:
            found.append("It both files the message and leaves it alone; "
                         "leaving it alone wins.")
        if sum(1 for k in kinds if k in ("draft", "draft_ai")) > 1:
            found.append("It drafts more than one reply; only the first is written.")
        return found

    @property
    def ready(self) -> bool:
        return not self.problems()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "enabled": self.enabled,
            "match": self.match,
            "conditions": [c.to_dict() for c in self.conditions],
            "actions": [a.to_dict() for a in self.actions],
            "skip_bulk": self.skip_bulk,
            "stop_after": self.stop_after,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Rule":
        raw = dict(raw or {})
        if "conditions" not in raw and "actions" not in raw:
            return cls._from_old_shape(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})

    @classmethod
    def _from_old_shape(cls, raw: Mapping[str, Any]) -> "Rule":
        """Read a rule written before conditions and actions were lists."""
        conditions: List[Condition] = []
        for value in raw.get("categories") or []:
            conditions.append(Condition("category", "is", str(value)))
        for value in raw.get("topics") or []:
            conditions.append(Condition("topic", "is", str(value)))
        if raw.get("sender_matches"):
            conditions.append(Condition("sender", "contains", str(raw["sender_matches"])))
        if raw.get("contains"):
            conditions.append(Condition("anywhere", "contains", str(raw["contains"])))
        floor = raw.get("min_confidence")
        if floor not in (None, ""):
            conditions.append(Condition("confidence", "at_least", str(floor)))

        kind = str(raw.get("action") or "draft")
        actions: List[Action] = []
        if kind == "draft_ai":
            actions.append(Action("draft_ai", str(raw.get("guidance") or "")))
        elif kind != "none":
            actions.append(Action("draft", str(raw.get("template") or "")))
        return cls(
            name=str(raw.get("name") or "New rule"),
            enabled=bool(raw.get("enabled")) and kind != "none",
            match="all",
            conditions=conditions,
            actions=actions,
            skip_bulk=bool(raw.get("skip_bulk", True)),
        )


#: Shipped switched off. Each is a case where the right response is nearly
#: mechanical, which is the only kind worth automating.
def default_rules() -> List[Rule]:
    return [
        Rule(
            name="Acknowledge an interview invitation",
            conditions=[Condition("category", "is", Category.INTERVIEW.value),
                        Condition("confidence", "at_least", "0.92")],
            actions=[Action("draft",
                            "Hello {first_name},\n\n"
                            "Thank you for the invitation. I would be glad to meet.\n\n"
                            "I am free on the times you suggested, and happy to work "
                            "around whatever suits the panel.\n\n"
                            "Best wishes,\n{me}")],
        ),
        Rule(
            name="Reply to a request for documents or availability",
            conditions=[Condition("category", "is", Category.NEXT_STEPS.value),
                        Condition("confidence", "at_least", "0.92")],
            actions=[Action("draft_ai",
                            "Answer what was actually asked for. If the message asks "
                            "for documents, say which are attached. If it asks for "
                            "times, offer three across two working days. Do not invent "
                            "facts about the sender, the role, or the writer.")],
        ),
        Rule(
            name="Thank a recruiter and decline politely",
            conditions=[Condition("category", "is", Category.UNSOLICITED.value),
                        Condition("confidence", "at_least", "0.94")],
            actions=[Action("draft",
                            "Hello {first_name},\n\n"
                            "Thank you for getting in touch. I am not looking to move "
                            "at the moment, but I am glad to stay in contact.\n\n"
                            "Best wishes,\n{me}")],
        ),
        Rule(
            name="File security notices without asking",
            conditions=[Condition("topic", "is", OtherCategory.SECURITY.value),
                        Condition("confidence", "at_least", "0.95")],
            actions=[Action("file_into", "Sorted Mail/Security"), Action("tick")],
            skip_bulk=False,
        ),
        Rule(
            name="Leave anything from a colleague alone",
            match="any",
            conditions=[Condition("sender_domain", "ends_with", "example.com")],
            actions=[Action("leave"), Action("stop")],
            skip_bulk=False,
        ),
    ]


@dataclass
class Outcome:
    """What the rules decided about one message."""

    rule_names: List[str] = field(default_factory=list)
    draft: Optional["Draft"] = None
    file_into: str = ""
    tick: Optional[bool] = None
    mark_read: bool = False
    flag: bool = False
    leave: bool = False
    #: Send it to the To Delete folder. Kept apart from ``file_into``
    #: because the folder is not known here - it depends on the account's
    #: folder plan - and because the caller needs to be able to tell "a rule
    #: chose a folder" from "a rule gave up on this message".
    bin_it: bool = False

    @property
    def rule_name(self) -> str:
        return ", ".join(self.rule_names)

    @property
    def changes_the_mailbox(self) -> bool:
        """Whether acting on this needs the account opening."""
        return bool(self.draft) or self.mark_read or self.flag

    @property
    def does_anything(self) -> bool:
        return bool(self.draft or self.file_into or self.tick is not None
                    or self.mark_read or self.flag or self.leave or self.bin_it)

    def describe(self) -> str:
        parts = []
        if self.draft is not None:
            parts.append("draft a reply")
        if self.leave:
            parts.append("leave it where it is")
        elif self.bin_it:
            parts.append("put it in To Delete")
        elif self.file_into:
            parts.append(f"file into {self.file_into}")
        if self.tick is True:
            parts.append("tick it")
        elif self.tick is False:
            parts.append("untick it")
        if self.mark_read:
            parts.append("mark it read")
        if self.flag:
            parts.append("flag it")
        return ", ".join(parts) or "nothing"


def apply_rules(rules: Sequence[Rule], message, classification, me: str = "",
                engine=None, context=None) -> Optional[Outcome]:
    """Run every rule in order and collect what they decided.

    Rules are read top to bottom and a later one can add to what an earlier one
    decided, until a rule says to stop. That ordering is the only thing anybody
    has to hold in their head, and it is the same rule every mail client has
    used for thirty years.

    Returns None when no rule wanted anything, so a caller can tell "no rule
    applied" from "a rule applied and asked for nothing".
    """
    outcome = Outcome()
    for rule in rules:
        ok, _why = rule.matches(message, classification, context)
        if not ok:
            continue
        outcome.rule_names.append(rule.name)
        stop = rule.stop_after
        for action in rule.actions:
            kind = action.kind
            if kind in ("draft", "draft_ai"):
                if outcome.draft is None:
                    outcome.draft = draft_for(
                        rule, message, classification, me,
                        engine if kind == "draft_ai" else None, action)
            elif kind == "file_into":
                outcome.file_into = action.value.strip()
                outcome.leave = False
            elif kind == "bin_it":
                outcome.bin_it = True
                outcome.tick = True
                outcome.leave = False
            elif kind == "tick":
                outcome.tick = True
            elif kind == "untick":
                outcome.tick = False
            elif kind == "mark_read":
                outcome.mark_read = True
            elif kind == "flag":
                outcome.flag = True
            elif kind == "leave":
                outcome.leave = True
                outcome.file_into = ""
                outcome.bin_it = False
                # Not just "do not file it": a tick an earlier rule put
                # there would otherwise survive and Apply would move the
                # message anyway, which is the opposite of what this says.
                outcome.tick = False
            elif kind == "stop":
                stop = True
        if stop:
            break
    return outcome if outcome.does_anything else None


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
    """The first rule that drafts a reply, and why none did when none do.

    Kept for the one caller that only wants a draft. Anything that needs the
    whole picture - filing, ticking, flagging - wants apply_rules instead.
    """
    reasons = []
    for rule in rules:
        ok, why = rule.matches(message, classification)
        if ok and not rule.drafts_a_reply:
            if rule.stop_after:
                return None, ""
            continue
        if ok:
            return rule, ""
        if rule.enabled and rule.actions:
            reasons.append(f"{rule.name}: {why}")
    return None, "; ".join(reasons[:3])


def draft_for(rule: Rule, message: EmailMessage, classification,
              me: str = "", engine=None, action: Optional[Action] = None) -> Draft:
    """Write the reply this rule calls for. Never raises."""
    if action is None:
        action = (rule.action("draft_ai") if rule.uses_the_model
                  else rule.action("draft")) or Action("draft")
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

    if action.kind == "draft" or engine is None:
        draft.body = render_template(action.value, message, me).strip()
        draft.generated_by = "template"
        if not draft.body:
            draft.error = "The rule has no template to fill in."
        # A template with a bracketed gap still needs the writer.
        draft.needs_from_writer = re.findall(r"\[([^\]]{2,60})\]", draft.body)
        return draft

    try:
        payload = engine.draft_reply(message, classification, rule, me, action.value)
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
