"""A short, deterministic slice of tools/stress.py, run on every commit.

The full campaign is tens of thousands of inputs and takes minutes; this is a
fixed seed and a few hundred, so a regression in any parser shows up in the
ordinary test run rather than waiting for somebody to think of running the
fuzzer. Every failure it has ever found is also pinned as its own test below.

    python tools/stress.py --rounds 20000     # the real thing
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import stress  # noqa: E402


@pytest.mark.parametrize("suite", stress.SUITES)
def test_nothing_raises_and_nothing_hangs(suite):
    """A fixed seed, so a failure here is reproducible exactly."""
    runner = stress.Runner(seed=20260911, rounds=120, verbose=False)
    getattr(runner, f"stress_{suite}")()
    assert runner.failures == [], runner.failures[0] if runner.failures else ""
    assert runner.checked > 0
    assert runner.slowest[0] < stress.BUDGET, runner.slowest


class TestWhatFuzzingFound:
    """Each of these crashed the app until the fuzzer walked into it."""

    def test_infinity_where_an_integer_was_expected(self):
        """OverflowError, straight past an `except TypeError`."""
        from config import Settings
        assert Settings.from_dict({"batch_size": float("inf")}).batch_size >= 1
        assert Settings.from_dict({"max_messages": float("-inf")}).max_messages >= 1
        assert Settings.from_dict({"concurrency": float("nan")}).concurrency >= 1

    def test_a_dict_where_a_string_was_expected(self):
        """AttributeError from .strip() inside __post_init__."""
        from config import Settings
        for value in ({}, [], {"a": {"b": 1}}, [{}], 12345, None, True):
            settled = Settings.from_dict({"folder_root": value})
            assert isinstance(settled.folder_root, str)
            assert settled.folder_root

    def test_a_settings_file_from_a_stranger_never_crashes(self):
        """Settings, Import opens a file somebody else may have written."""
        from config import Settings
        hostile = {
            "provider": {"nested": True}, "confidence_threshold": float("inf"),
            "max_messages": [], "mailboxes": "not a list",
            "topics": [{"x": 1}], "reply_rules": "nope",
            "folder_root": {"a": 1}, "appearance_mode": 42,
            "hidden_columns": {"not": "a list"}, "ruleset": None,
        }
        settled = Settings.from_dict(hostile)
        assert settled.provider in ("rules", "anthropic", "gemini", "openai",
                                    "ollama")
        assert 0.5 <= settled.confidence_threshold <= 1.0

    def test_unclosed_tags_do_not_stall(self):
        """35 seconds for a 195 KB body, before the brackets were defused."""
        import time
        import html_utils
        began = time.perf_counter()
        html_utils.html_to_text("<div" * 50_000)
        assert time.perf_counter() - began < 2.0

    def test_a_stray_bracket_does_not_eat_a_real_link(self):
        """The first of four hundred claimed an anchor's ">" a KB later."""
        import html_utils
        result = html_utils.html_to_text(
            ("<div" * 400) + '<a href="http://y.example">link</a>')
        assert "http://y.example" in result.links

    def test_a_host_that_merely_starts_with_a_private_range(self):
        """127.0.0.1.evil.com passed a prefix check and would have got
        message bodies over plain HTTP."""
        from providers import _is_local
        for host in ("127.0.0.1.evil.com", "10.0.0.1.attacker.net",
                     "192.168.1.1.example.com"):
            assert _is_local(host) is False

    def test_a_folder_name_that_climbs(self):
        from models import sanitize_folder_component
        for evil in ("../../etc/passwd", "..", "....", "  ..  x  ", ".ssh"):
            got = sanitize_folder_component(evil)
            assert "/" not in got and not got.startswith(".")
