"""The rules engine behind auto reply: conditions, actions, and order.

Written to break it. A rule is user-typed, so every test here assumes the
person writing the rule was careless, in a hurry, or actively hostile, and
asserts the app carries on regardless.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import autoreply
from autoreply import Action, Condition, Rule
from models import Category, Classification, EmailMessage, OtherCategory


def message(**overrides) -> EmailMessage:
    base = dict(
        uid="1",
        subject="Interview invitation",
        sender_name="Dana Reyes",
        sender_email="dana@northwind.example",
        body_text="We would like to schedule a technical interview.",
        date=datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc),
    )
    base.update(overrides)
    return EmailMessage(**base)


def classification(**overrides) -> Classification:
    base = dict(
        summary="s", reasoning="r", is_job_related=True,
        category=Category.INTERVIEW,
        other_category=OtherCategory.NOT_APPLICABLE,
        confidence_score=0.97,
    )
    base.update(overrides)
    return Classification(**base)


def rule(*conditions, actions=(Action("tick"),), **kwargs) -> Rule:
    kwargs.setdefault("enabled", True)
    return Rule(name=kwargs.pop("name", "test"), conditions=list(conditions),
                actions=list(actions), **kwargs)


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------
class TestConditions:
    @pytest.mark.parametrize("field, operator, value, expected", [
        ("category", "is", "INTERVIEW", True),
        ("category", "is", "REJECTION", False),
        ("category", "is_not", "REJECTION", True),
        ("subject", "contains", "INTERVIEW", True),      # case is ignored
        ("subject", "not_contains", "rejection", True),
        ("subject", "starts_with", "interview", True),
        ("subject", "ends_with", "invitation", True),
        ("subject", "equals", "interview invitation", True),
        ("subject", "matches", r"^Interview\b", True),
        ("sender", "contains", "dana", True),
        ("sender_domain", "is", "northwind.example", True),
        ("sender_domain", "ends_with", ".example", True),
        ("body", "contains", "technical", True),
        ("anywhere", "contains", "invitation", True),
        ("confidence", "at_least", "0.9", True),
        ("confidence", "at_least", "0.99", False),
        ("confidence", "at_most", "0.99", True),
        ("is_bulk", "is_false", "", True),
        ("is_reply", "is_false", "", True),
        ("has_attachment", "is_false", "", True),
    ])
    def test_each_field_and_operator(self, field, operator, value, expected):
        got = Condition(field, operator, value).matches(message(), classification())
        assert got is expected

    def test_a_topic_condition_ignores_job_mail(self):
        """A job-related message has no everyday topic, and must not borrow one."""
        condition = Condition("topic", "is", "SECURITY")
        job = classification(is_job_related=True, other_category=OtherCategory.SECURITY)
        assert condition.matches(message(), job) is False
        everyday = classification(is_job_related=False,
                                  other_category=OtherCategory.SECURITY)
        assert condition.matches(message(), everyday) is True

    def test_a_category_condition_ignores_everyday_mail(self):
        condition = Condition("category", "is", "INTERVIEW")
        assert condition.matches(message(), classification(is_job_related=False)) is False

    def test_an_empty_text_test_never_matches_everything(self):
        for operator in ("contains", "starts_with", "ends_with", "equals", "matches"):
            assert Condition("subject", operator, "").matches(
                message(), classification()) is False

    def test_a_broken_pattern_does_not_raise(self):
        condition = Condition("subject", "matches", "(unclosed[")
        assert condition.matches(message(), classification()) is False
        assert "will not compile" in condition.problem()

    def test_a_number_field_given_words_does_not_raise(self):
        condition = Condition("confidence", "at_least", "very sure")
        assert condition.matches(message(), classification()) is False
        assert "not a number" in condition.problem()

    def test_an_unknown_field_falls_back_rather_than_raising(self):
        condition = Condition("favourite_colour", "contains", "blue")
        assert condition.field == "anywhere"
        assert condition.matches(message(), classification()) is False

    def test_an_operator_the_field_does_not_offer_is_replaced(self):
        """"is at least" on a subject is nonsense, and is quietly corrected."""
        condition = Condition("subject", "at_least", "0.9")
        assert condition.operator in {n for n, _l in autoreply.operators_for("subject")}

    def test_a_missing_date_does_not_break_an_age_test(self):
        condition = Condition("age_days", "at_most", "7")
        assert condition.matches(message(date=None), classification()) is True

    def test_age_is_measured_from_now(self):
        old = datetime.now(timezone.utc) - timedelta(days=30)
        condition = Condition("age_days", "at_least", "14")
        assert condition.matches(message(date=old), classification()) is True
        assert Condition("age_days", "at_most", "14").matches(
            message(date=old), classification()) is False

    def test_a_mailbox_matches_by_address_or_label_or_id(self):
        mail = message(account_id="acct-1", account_address="me@gmail.com",
                       account_label="Personal")
        for value in ("acct-1", "me@gmail.com", "personal"):
            assert Condition("mailbox", "is", value).matches(
                mail, classification()) is True
        assert Condition("mailbox", "is", "work@icloud.com").matches(
            mail, classification()) is False

    def test_a_huge_body_is_not_read_past_the_cap(self):
        """A rule runs on every message, so it reads a bounded amount."""
        big = message(body_text="x" * 5_000_000 + "needle")
        condition = Condition("body", "contains", "needle")
        assert condition.matches(big, classification()) is False

    def test_a_condition_survives_a_message_with_nothing_in_it(self):
        bare = EmailMessage(uid="0")
        for field, _label, kind in autoreply.FIELDS:
            operator = autoreply.operators_for(field)[0][0]
            Condition(field, operator, "x").matches(bare, classification())


# --------------------------------------------------------------------------
# Rules
# --------------------------------------------------------------------------
class TestRules:
    def test_all_means_all(self):
        both = rule(Condition("subject", "contains", "interview"),
                    Condition("sender", "contains", "dana"))
        assert both.matches(message(), classification())[0] is True
        neither = rule(Condition("subject", "contains", "interview"),
                       Condition("sender", "contains", "morgan"))
        assert neither.matches(message(), classification())[0] is False

    def test_any_means_any(self):
        either = rule(Condition("subject", "contains", "nonsense"),
                      Condition("sender", "contains", "dana"), match="any")
        assert either.matches(message(), classification())[0] is True

    def test_a_rule_with_no_conditions_never_runs(self):
        """Otherwise a half-written rule acts on every message in the mailbox."""
        empty = Rule(name="empty", enabled=True, actions=[Action("tick")])
        matched, why = empty.matches(message(), classification())
        assert matched is False and "no conditions" in why

    def test_a_rule_with_no_actions_never_runs(self):
        idle = Rule(name="idle", enabled=True,
                    conditions=[Condition("subject", "contains", "interview")])
        assert idle.matches(message(), classification())[0] is False

    def test_a_rule_that_is_off_never_runs(self):
        off = rule(Condition("subject", "contains", "interview"), enabled=False)
        assert off.matches(message(), classification())[0] is False

    def test_bulk_mail_is_skipped_by_default(self):
        bulky = message(list_unsubscribe="<mailto:x@y.example>")
        assert rule(Condition("subject", "contains", "interview")).matches(
            bulky, classification())[0] is False

    def test_bulk_mail_can_be_opted_into(self):
        bulky = message(list_unsubscribe="<mailto:x@y.example>")
        opted = rule(Condition("subject", "contains", "interview"), skip_bulk=False)
        assert opted.matches(bulky, classification())[0] is True

    def test_why_it_did_not_match_names_the_condition(self):
        missed = rule(Condition("sender", "contains", "morgan"))
        _ok, why = missed.matches(message(), classification())
        assert "morgan" in why

    def test_problems_are_listed_rather_than_refused(self):
        broken = Rule(name="broken", enabled=True,
                      actions=[Action("draft", ""), Action("tick"), Action("untick")])
        problems = broken.problems()
        assert len(problems) == 3 and not broken.ready

    def test_a_finished_rule_has_no_problems(self):
        assert rule(Condition("subject", "contains", "x")).ready


# --------------------------------------------------------------------------
# Running them in order
# --------------------------------------------------------------------------
class TestApplyRules:
    def test_nothing_matching_returns_nothing(self):
        assert autoreply.apply_rules(
            [rule(Condition("subject", "contains", "zebra"))],
            message(), classification()) is None

    def test_later_rules_add_to_earlier_ones(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("tick")], name="first"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("flag")], name="second"),
        ], message(), classification())
        assert outcome.tick is True and outcome.flag is True
        assert outcome.rule_names == ["first", "second"]

    def test_stop_after_skips_what_follows(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("untick")], stop_after=True, name="first"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("flag")], name="second"),
        ], message(), classification())
        assert outcome.tick is False and outcome.flag is False
        assert outcome.rule_names == ["first"]

    def test_the_stop_action_does_the_same(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("leave"), Action("stop")], name="first"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("file_into", "Somewhere")], name="second"),
        ], message(), classification())
        assert outcome.leave is True and outcome.file_into == ""

    def test_leaving_it_alone_cancels_an_earlier_filing(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("file_into", "Sorted Mail/Security")], name="file"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("leave")], name="leave"),
        ], message(), classification())
        assert outcome.leave is True and outcome.file_into == ""

    def test_filing_after_leaving_wins_because_it_came_later(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("leave")], name="leave"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("file_into", "Archive")], name="file"),
        ], message(), classification())
        assert outcome.leave is False and outcome.file_into == "Archive"

    def test_only_the_first_draft_is_written(self):
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("draft", "First reply from {me}")], name="one"),
            rule(Condition("sender", "contains", "dana"),
                 actions=[Action("draft", "Second reply")], name="two"),
        ], message(), classification(), me="Nick")
        assert outcome.draft.body.startswith("First reply")

    def test_a_model_action_with_no_model_falls_back_to_the_template(self):
        """A draft_ai rule run without an engine must not lose the message."""
        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("draft_ai", "be brief")]),
        ], message(), classification(), me="Nick", engine=None)
        assert outcome.draft is not None
        assert outcome.draft.generated_by == "template"

    def test_a_model_that_explodes_becomes_an_error_on_the_draft(self):
        class Exploding:
            def draft_reply(self, *_a, **_k):
                raise RuntimeError("no")

        outcome = autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("draft_ai", "be brief")]),
        ], message(), classification(), me="Nick", engine=Exploding())
        assert outcome.draft.ok is False and "could not draft" in outcome.draft.error

    def test_an_outcome_that_asks_for_nothing_is_none(self):
        assert autoreply.apply_rules([
            rule(Condition("subject", "contains", "interview"),
                 actions=[Action("stop")]),
        ], message(), classification()) is None

    def test_only_drafting_and_flagging_need_the_mailbox(self):
        ticking = autoreply.apply_rules(
            [rule(Condition("subject", "contains", "interview"))],
            message(), classification())
        assert ticking.changes_the_mailbox is False
        flagging = autoreply.apply_rules(
            [rule(Condition("subject", "contains", "interview"),
                  actions=[Action("flag")])], message(), classification())
        assert flagging.changes_the_mailbox is True

    def test_a_hundred_rules_still_finish_quickly(self):
        import time
        rules = [rule(Condition("subject", "contains", f"word{n}"),
                      name=f"rule {n}") for n in range(100)]
        started = time.perf_counter()
        for _ in range(50):
            autoreply.apply_rules(rules, message(), classification())
        assert time.perf_counter() - started < 2.0


# --------------------------------------------------------------------------
# Reading and writing rules
# --------------------------------------------------------------------------
class TestPersistence:
    def test_a_rule_round_trips(self):
        original = rule(Condition("subject", "contains", "interview"),
                        actions=[Action("draft", "Hi {first_name}"), Action("tick")],
                        match="any", stop_after=True, name="mine")
        copy = Rule.from_dict(original.to_dict())
        assert copy.to_dict() == original.to_dict()

    def test_a_rule_written_before_conditions_existed_still_reads(self):
        old = {
            "name": "Acknowledge an interview", "enabled": True, "action": "draft",
            "categories": ["INTERVIEW"], "min_confidence": 0.92,
            "sender_matches": "northwind", "contains": "schedule",
            "template": "Hello {first_name},", "skip_bulk": True,
        }
        converted = Rule.from_dict(old)
        assert converted.enabled and converted.ready
        fields = [c.field for c in converted.conditions]
        assert fields == ["category", "sender", "anywhere", "confidence"]
        assert converted.actions[0].kind == "draft"
        assert converted.matches(message(sender_email="dana@northwind.example",
                                         body_text="please schedule a time"),
                                 classification())[0] is True

    def test_an_old_rule_that_did_nothing_stays_doing_nothing(self):
        converted = Rule.from_dict({"name": "off", "action": "none",
                                    "categories": ["INTERVIEW"]})
        assert converted.actions == [] and not converted.ready

    @pytest.mark.parametrize("junk", [
        {}, {"conditions": "not a list", "actions": None},
        {"conditions": [{"field": 42}], "actions": [{"kind": "explode"}]},
        {"match": "sometimes", "conditions": [], "actions": []},
        {"name": None, "enabled": "yes", "conditions": [None, 7], "actions": ["x"]},
    ])
    def test_junk_reads_as_a_harmless_rule(self, junk):
        parsed = Rule.from_dict(junk)
        assert parsed.matches(message(), classification())[0] is False
        parsed.to_dict()

    def test_an_unknown_action_kind_is_dropped_not_run(self):
        parsed = Rule.from_dict({
            "name": "hostile", "enabled": True,
            "conditions": [{"field": "subject", "operator": "contains",
                            "value": "interview"}],
            "actions": [{"kind": "delete_everything", "value": "/"}],
        })
        assert all(a.kind in dict((k, l) for k, l, _n in autoreply.ACTION_KINDS)
                   for a in parsed.actions)
        outcome = autoreply.apply_rules([parsed], message(), classification())
        assert outcome is None or not outcome.file_into


class TestShippedRules:
    def test_they_are_all_off(self):
        assert not any(r.enabled for r in autoreply.default_rules())

    def test_they_are_all_finished(self):
        for r in autoreply.default_rules():
            assert r.ready, f"{r.name}: {r.problems()}"

    def test_none_of_them_matches_until_switched_on(self):
        assert autoreply.apply_rules(
            autoreply.default_rules(), message(), classification()) is None

    def test_the_interview_rule_does_what_it_says_once_on(self):
        rules = autoreply.default_rules()
        rules[0].enabled = True
        outcome = autoreply.apply_rules(rules, message(), classification(), me="Nick")
        assert outcome.draft.ok and "Nick" in outcome.draft.body


class TestOperatorsThatDoNotFit:
    """A rule written for one field, then pointed at another."""

    def test_is_on_a_text_field_becomes_is_exactly(self):
        assert Condition("subject", "is", "hello").operator == "equals"
        assert Condition("sender_domain", "is_not", "x").operator == "not_equals"

    def test_is_exactly_on_a_category_becomes_is(self):
        assert Condition("category", "equals", "INTERVIEW").operator == "is"

    def test_the_replacement_is_the_same_every_time(self):
        """It used to come out of a set, so it varied between runs."""
        picks = {Condition("subject", "at_least", "3").operator for _ in range(50)}
        assert len(picks) == 1

    def test_is_not_exactly_actually_tests_something(self):
        assert Condition("subject", "not_equals", "Interview invitation").matches(
            message(), classification()) is False
        assert Condition("subject", "not_equals", "Something else").matches(
            message(), classification()) is True

    def test_a_folder_name_keeps_its_capitals_in_the_summary(self):
        described = rule(Condition("subject", "contains", "x"),
                         actions=[Action("file_into", "Sorted Mail/Work")]).describe()
        assert "Sorted Mail/Work" in described


class TestPatternsThatWouldHangTheApp:
    """A rule's pattern is typed by a person, and `re` cannot be interrupted.

    Python's regular expressions backtrack and hold the interpreter while they
    do it, so a pattern with the wrong shape does not slow the app down, it
    stops it, and the Stop button cannot help. These are refused before they
    run.
    """

    CATASTROPHIC = [r"(a+)+$", r".*.*.*.*x", r"(a*)*b", r"(\d+)+",
                    r"([a-z]+)*$", r"(x{1,})+", r"(a+)+(b+)+"]
    ORDINARY = [r"^Interview\b", r"\d{4}-\d{2}-\d{2}", r"(foo|bar)+", r"a*b*",
                r"^Re:\s*", r"[A-Z]{2,}\s+\w+", r".*urgent.*", r"(?i)hello",
                r"\bjob\b.*\boffer\b", r"https?://\S+", r"^(?!spam).*$",
                r"(one|two)*", r"\w+@\w+\.\w+", r"", r"plain text"]

    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_a_catastrophic_pattern_is_refused(self, pattern):
        assert autoreply.pattern_risk(pattern), "not flagged"
        condition = Condition("body", "matches", pattern)
        assert condition.matches(message(), classification()) is False
        assert "unsafe to run" in condition.problem()

    @pytest.mark.parametrize("pattern", ORDINARY)
    def test_an_ordinary_pattern_is_left_alone(self, pattern):
        assert autoreply.pattern_risk(pattern) == "", "false alarm"

    @pytest.mark.parametrize("pattern", CATASTROPHIC)
    def test_a_refused_pattern_returns_at_once(self, pattern):
        """The point of refusing it is that it costs nothing to refuse."""
        import time
        hostile = message(body_text="a b " * 20000)
        started = time.perf_counter()
        Condition("body", "matches", pattern).matches(hostile, classification())
        assert time.perf_counter() - started < 0.05

    def test_a_leading_wildcard_does_not_make_it_quadratic(self):
        """".*word.*" is a reasonable thing to type and used to take seconds."""
        import time
        hostile = message(body_text="a b " * 20000)
        started = time.perf_counter()
        Condition("body", "matches", r".*urgent.*").matches(hostile, classification())
        assert time.perf_counter() - started < 0.2

    @pytest.mark.parametrize("pattern", [
        r".*urgent.*", r".*", r".*a", r"a.*", r".*a.*b.*", r"^.*x", r".*$",
        r".*?x", r"\.*", r".*[0-9].*", r"x.*$", r".*(foo|bar).*"])
    def test_trimming_never_changes_the_answer(self, pattern):
        """Whatever it does to the pattern, a search must decide the same."""
        import re as regex
        original = regex.compile(pattern, regex.IGNORECASE)
        prepared = regex.compile(autoreply._prepare(pattern), regex.IGNORECASE)
        for text in ("", "urgent", "not here", "a", "ab", "x", ".", "...",
                     "a\nb", "foo", "12", "  ", "aXb", "\n", "urgent\nnow"):
            assert bool(original.search(text)) is bool(prepared.search(text)), (
                f"{pattern!r} disagreed on {text!r}")

    def test_a_rule_with_a_refused_pattern_is_not_ready(self):
        unsafe = rule(Condition("body", "matches", r"(a+)+$"))
        assert not unsafe.ready
        assert any("unsafe" in problem for problem in unsafe.problems())


class TestFuzzing:
    """Random rules, hostile messages. Nothing raises, nothing hangs.

    Rules are typed by a person and messages arrive from strangers, so both
    ends of this are untrusted. The bound on time matters as much as the bound
    on exceptions: a rule that takes a minute cannot be cancelled, because the
    regular-expression engine holds the interpreter while it runs.
    """

    FIELDS = [f for f, _l, _k in autoreply.FIELDS]
    OPERATORS = [o for o, _l, _k in autoreply.OPERATORS]
    KINDS = [k for k, _l, _n in autoreply.ACTION_KINDS] + ["nonsense"]
    VALUES = ["", " ", "0.9", "-1", "1e9999", "nan", "INTERVIEW", "interview",
              "(", "(a+)+$", ".*.*.*.*x", ".*urgent.*", "(a|a)*$", r"(\d|\w)+$",
              "\x00", "é" * 50, "a" * 5000, "%s", "{me}", "../../etc", "\\",
              "'", '"', "\n\n", "🙂", "[", r"\1", "(?P<x>"]
    BODIES = ["", "x" * 200000, "_" * 3000, "\x00\x01\x02", "é" * 10000,
              "<b>hi</b>" * 500, "a b " * 20000, "a" * 40 + "!" * 500]

    def _rule(self, rng):
        return Rule(
            name=rng.choice(["", "x" * 300, "rule"]),
            enabled=rng.choice([True, True, False]),
            match=rng.choice(["all", "any", "sometimes"]),
            conditions=[Condition(rng.choice(self.FIELDS),
                                  rng.choice(self.OPERATORS),
                                  rng.choice(self.VALUES))
                        for _ in range(rng.randint(0, 4))],
            actions=[Action(rng.choice(self.KINDS), rng.choice(self.VALUES))
                     for _ in range(rng.randint(0, 4))],
            skip_bulk=rng.choice([True, False]),
            stop_after=rng.choice([True, False]))

    def _message(self, rng, index):
        return message(
            uid=str(index),
            subject=rng.choice(["", "Re: " * 50, "\x00", "é" * 300, "Interview"]),
            sender_name=rng.choice(["", "a" * 500, "<script>"]),
            sender_email=rng.choice(["", "@", "a@", "@b", "a@b.example"]),
            body_text=rng.choice(self.BODIES),
            list_unsubscribe=rng.choice(["", "<mailto:x@y>"]))

    @pytest.mark.parametrize("seed", [1, 2, 3])
    def test_nothing_raises_and_nothing_hangs(self, seed):
        import random
        import time
        rng = random.Random(seed)
        worst = 0.0
        for index in range(400):
            rules = [self._rule(rng) for _ in range(rng.randint(1, 5))]
            mail = self._message(rng, index)
            started = time.perf_counter()
            outcome = autoreply.apply_rules(rules, mail, classification(), me="Nick")
            worst = max(worst, time.perf_counter() - started)
            for one in rules:
                one.problems()
                one.describe()
                assert Rule.from_dict(one.to_dict()).to_dict() == one.to_dict()
            if outcome is not None:
                outcome.describe()
        assert worst < 0.25, f"a single message took {worst:.2f}s"
