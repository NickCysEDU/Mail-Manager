"""Where the inbox-derived fixtures live, when they are present at all.

Two of the evaluation sets were built from a real mailbox. They are
anonymised, but anonymising a hundred real messages is not a job with a
provable end - four passes over them each found something the one before had
missed - so they are not in the repository. They sit in
``tests/fixtures/private``, which is ignored by git, on the machines that
have them.

Everything that needs one asks here and skips when the answer is None. A
checkout without them runs the whole suite except the handful of tests that
score against real mail, and ``./dev eval`` says so plainly rather than
failing with a missing-file traceback.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

FIXTURES = Path(__file__).resolve().parent / "fixtures"

#: An override, so the sets can live outside the tree entirely.
ENV_VAR = "MAIL_MANAGER_PRIVATE_FIXTURES"

#: The sets that are not in the repository.
PRIVATE = ("labelled.json", "acknowledgements.json")

WHY = (
    "built from a real inbox and kept out of the repository; see "
    "tests/private_fixtures.py"
)


def directory() -> Path:
    override = os.environ.get(ENV_VAR)
    return Path(override) if override else FIXTURES / "private"


def path(name: str) -> Optional[Path]:
    """The fixture, wherever it is, or None if this checkout has not got it."""
    here = FIXTURES / name
    if here.is_file():
        return here
    there = directory() / name
    return there if there.is_file() else None


def every(include_private: bool = True) -> list:
    """Every fixture available here, public first."""
    found = sorted(p for p in FIXTURES.glob("*.json"))
    if include_private and directory().is_dir():
        found += sorted(p for p in directory().glob("*.json"))
    return found
