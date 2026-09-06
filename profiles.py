"""What the app is being used for, and therefore where mail should end up.

The sorter was built around one job: find job-search mail and file it. But the
machinery underneath was never that narrow - alongside the job categories it
already scores twelve everyday topics, and the folder layer already knows how
to file them. What was missing was a way to say which of those two you care
about.

A profile answers that. It does not change how anything is classified; every
message is still scored the same way. It changes which distinctions are worth
a folder of their own, and that is entirely a routing decision, so a profile is
a small bundle of settings rather than a second engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from models import (
    DEFAULT_FOLDER_ROOT,
    DEFAULT_OTHER_ROOT,
    NonJobRouting,
    OtherCategory,
)

#: Everyday topics, in the order a person would skim them.
ALL_TOPICS: Tuple[OtherCategory, ...] = (
    OtherCategory.SECURITY,
    OtherCategory.FINANCE,
    OtherCategory.RECEIPT,
    OtherCategory.SHIPPING,
    OtherCategory.TRAVEL,
    OtherCategory.EVENT,
    OtherCategory.WORK,
    OtherCategory.PERSONAL,
    OtherCategory.SOCIAL,
    OtherCategory.NEWSLETTER,
    OtherCategory.PROMOTION,
    OtherCategory.SPAM,
)

#: The few that most people would want out of the inbox and nothing else.
ESSENTIAL_TOPICS: Tuple[OtherCategory, ...] = (
    OtherCategory.SECURITY,
    OtherCategory.FINANCE,
    OtherCategory.RECEIPT,
    OtherCategory.TRAVEL,
    OtherCategory.NEWSLETTER,
    OtherCategory.PROMOTION,
)


@dataclass(frozen=True)
class Profile:
    """A named answer to "what should get its own folder?"."""

    name: str
    label: str
    blurb: str
    #: False files everything job related into one folder instead of seven.
    #: The table still shows the exact category either way.
    detailed_job_folders: bool = True
    #: Topics worth a folder. Anything outside this lands in "Other".
    topics: Tuple[OtherCategory, ...] = ()
    folder_root: str = DEFAULT_FOLDER_ROOT
    other_root: str = DEFAULT_OTHER_ROOT
    non_job_routing: NonJobRouting = NonJobRouting.LEAVE

    @property
    def sorts_everything(self) -> bool:
        return self.non_job_routing is NonJobRouting.FILE


PROFILES: Tuple[Profile, ...] = (
    Profile(
        name="job_search",
        label="Job search",
        blurb="Files job-search mail into Job Search folders and leaves the "
              "rest of your inbox alone.",
        detailed_job_folders=True,
        topics=(),
        non_job_routing=NonJobRouting.LEAVE,
    ),
    Profile(
        name="everything",
        label="Job search and everyday mail",
        blurb="Keeps the seven Job Search folders and also files receipts, "
              "travel, security notices and the rest into Sorted Mail.",
        detailed_job_folders=True,
        topics=ALL_TOPICS,
        non_job_routing=NonJobRouting.FILE,
    ),
    Profile(
        name="everyday",
        label="Everyday mail",
        blurb="For when you are not job hunting. Sorts the whole inbox by "
              "topic; anything job related goes to a single folder.",
        detailed_job_folders=False,
        topics=ALL_TOPICS,
        non_job_routing=NonJobRouting.FILE,
    ),
    Profile(
        name="essentials",
        label="Essentials only",
        blurb="A short list: security, finance, receipts, travel, newsletters "
              "and promotions. Everything else stays where it is.",
        detailed_job_folders=False,
        topics=ESSENTIAL_TOPICS,
        non_job_routing=NonJobRouting.FILE,
    ),
)

_BY_NAME: Dict[str, Profile] = {entry.name: entry for entry in PROFILES}

#: What the app has always done, and what it falls back to.
DEFAULT_PROFILE = "job_search"


def get(name: str) -> Profile:
    """The profile called `name`, or the job-search one."""
    return _BY_NAME.get((name or "").strip().lower(), _BY_NAME[DEFAULT_PROFILE])


def exists(name: str) -> bool:
    return (name or "").strip().lower() in _BY_NAME


def choices() -> Tuple[Tuple[str, str, str], ...]:
    """(name, label, blurb) for a menu, in the order defined above."""
    return tuple((p.name, p.label, p.blurb) for p in PROFILES)


def names() -> Tuple[str, ...]:
    return tuple(_BY_NAME)
