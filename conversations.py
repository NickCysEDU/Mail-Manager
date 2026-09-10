"""Which messages are the same conversation.

Job mail arrives in threads. An interview invitation, your reply, the
reschedule, the confirmation - four messages, one thing happening, and a
sorter that treats them as four unrelated events will file them four
different ways. Worse, it will ask you about each one separately.

Threading is done the way mail clients have always done it, in two passes.

**By identity.** ``Message-ID``, ``In-Reply-To`` and ``References`` form a
graph: every message names the ones it is answering. Following those links
gives the true thread, and it is exact - two messages either reference each
other or they do not.

**By subject and correspondent.** Plenty of mail arrives with the references
stripped, by a mailing list, a rewriting gateway, or somebody who replied by
composing a new message with the same subject. So messages that share a
normalised subject *and* a correspondent are joined too. Both halves are
required: subject alone would merge every "Thank you for applying" ever sent,
and correspondent alone would merge a recruiter's entire correspondence into
one thread.

Nothing here decides anything on its own. It groups, and the grouping is used
to offer - file the whole conversation, select the whole conversation - never
to move mail nobody asked about.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence, Tuple

#: Reply and forward markers, in the languages this is likely to meet. Ordered
#: longest first so "antwort:" is not left as "wort:" by a shorter match.
_PREFIX = re.compile(
    r"^\s*(?:"
    # A mailing-list tag, which carries no colon of its own.
    r"\[[^\]]{1,30}\]\s*"
    r"|"
    # A reply or forward marker, which does.
    r"(?:re|res|ref|aw|antw|antwort|sv|svar|vs|vastaus|odp|odpowiedz|"
    r"fw|fwd|fwds|wg|weiterleitung|tr|rv|enc|encaminhado)"
    r"\s*(?:\[\d+\])?\s*[:：]\s*"
    r")", re.I)

#: A Message-ID, with or without its angle brackets.
_MESSAGE_ID = re.compile(r"<([^<>@\s]+@[^<>\s]+)>")


def normalise_subject(subject: str) -> str:
    """Strip reply and forward markers, repeatedly, and fold the rest.

    Repeatedly because "Re: Fw: Re: Interview" is one conversation and a
    single pass would leave two thirds of the noise behind.
    """
    text = subject or ""
    for _ in range(8):
        stripped = _PREFIX.sub("", text, count=1)
        if stripped == text:
            break
        text = stripped
    return " ".join(text.lower().split())


def message_ids(raw: str) -> List[str]:
    """Every Message-ID in a header value, in order.

    Angle brackets are required. A References header full of bare words is
    somebody's broken client, and taking those as ids would join unrelated
    threads on a shared word.
    """
    return [found.lower() for found in _MESSAGE_ID.findall(raw or "")]


def _own_id(message) -> str:
    ids = message_ids(getattr(message, "message_id", "") or "")
    return ids[0] if ids else ""


def _referenced(message) -> List[str]:
    """Everything this message says it is answering."""
    out: List[str] = []
    for header in ("in_reply_to", "references"):
        out.extend(message_ids(getattr(message, header, "") or ""))
    return out


def _correspondent(message) -> str:
    """Who the conversation is with, as far as this message says."""
    return (getattr(message, "sender_email", "") or "").strip().lower()


class _Groups:
    """Union-find over whatever keys get joined together."""

    def __init__(self) -> None:
        self._parent: Dict[str, str] = {}

    def add(self, key: str) -> None:
        self._parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        # Flatten, so a long reply chain does not cost more each time it is
        # asked about.
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def join(self, left: str, right: str) -> None:
        left_root, right_root = self.find(left), self.find(right)
        if left_root != right_root:
            # Lowest key wins, so the answer does not depend on the order
            # messages happened to be fetched in.
            low, high = sorted((left_root, right_root))
            self._parent[high] = low


def thread_keys(messages: Sequence) -> List[str]:
    """One key per message, shared by everything in the same conversation.

    Returned positionally rather than as a dict, because two messages can be
    identical in every field this looks at - a duplicate delivered twice - and
    they should still be one entry each.
    """
    groups = _Groups()
    keys: List[str] = []

    # Pass one: identity. Every message is joined to everything it references.
    for index, message in enumerate(messages):
        own = _own_id(message)
        key = own or f"#{index}"
        keys.append(key)
        groups.add(key)
        for referenced in _referenced(message):
            groups.join(key, referenced)

    # Pass two: subject and correspondent, for mail whose references were
    # stripped on the way. Only messages that agree on both are joined.
    by_pair: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for key, message in zip(keys, messages):
        subject = normalise_subject(getattr(message, "subject", "") or "")
        who = _correspondent(message)
        if subject and who:
            by_pair[(subject, who)].append(key)
    for shared in by_pair.values():
        first = shared[0]
        for other in shared[1:]:
            groups.join(first, other)

    return [groups.find(key) for key in keys]


def group(messages: Sequence) -> Dict[str, List[int]]:
    """Positions of the messages in each conversation, keyed by thread."""
    out: Dict[str, List[int]] = defaultdict(list)
    for index, key in enumerate(thread_keys(messages)):
        out[key].append(index)
    return dict(out)


def describe(size: int) -> str:
    """How to say how big a conversation is."""
    if size <= 1:
        return ""
    return f"one of {size} messages in this conversation"


def apply_to(items: Iterable) -> int:
    """Stamp each item with its thread key. Returns how many are in threads.

    The count is of messages that have company, not of conversations: a
    hundred messages that are all on their own is nothing to tell anybody
    about.
    """
    items = list(items)
    if not items:
        return 0
    keys = thread_keys([item.email for item in items])
    sizes: Dict[str, int] = defaultdict(int)
    for key in keys:
        sizes[key] += 1
    grouped = 0
    for item, key in zip(items, keys):
        item.thread_key = key
        item.thread_size = sizes[key]
        if sizes[key] > 1:
            grouped += 1
    return grouped
