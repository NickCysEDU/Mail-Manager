"""The things that stop auto-reply becoming the reason to turn auto-reply off.

A rule that matches is only half the question. The other half is whether
anything should be said at all: whether a machine sent the message, whether
this person was already written to, whether it is three in the morning.
Everything here is about that second half, because every one of these is a
way a working autoresponder embarrasses somebody.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import autoreply
import reply_log
from autoreply import Action, Condition, Rule, is_automated
from models import Category, Classification, EmailMessage, OtherCategory


def message(sender="dana@northwind.example", subject="Hello", to="",
            auto_submitted="", precedence="", suppress="",
            attachments=()) -> EmailMessage:
    return EmailMessage(uid="1", sender_email=sender, sender_name="A Person",
                        subject=subject, body_text="Some words.", to=to,
                        auto_submitted=auto_submitted, precedence=precedence,
                        x_auto_response_suppress=suppress,
                        attachments=tuple(attachments))


def verdict(job=True, category=Category.INTERVIEW) -> Classification:
    return Classification(summary="", is_job_related=job, category=category,
                          other_category=OtherCategory.NOT_APPLICABLE,
                          confidence_score=0.97, model="test")


def drafting_rule(**kwargs) -> Rule:
    return Rule(name=kwargs.pop("name", "Answer it"), enabled=True,
                conditions=[Condition(field="sender", operator="contains",
                                      value="@")],
                actions=[Action("draft", "Thanks, I will be in touch.")],
                skip_bulk=False, **kwargs)


class TestItKnowsAMachineWhenItSeesOne:
    @pytest.mark.parametrize("sender", [
        "no-reply@shop.example",
        "noreply@shop.example",
        "NoReply@Shop.Example",
        "do-not-reply@shop.example",
        "donotreply@shop.example",
        "mailer-daemon@shop.example",
        "postmaster@shop.example",
        "bounces@shop.example",
        "bounces+tag-1234@shop.example",
        "no-reply-4821@shop.example",
    ])
    def test_these_addresses_are_machines(self, sender):
        assert is_automated(message(sender=sender)), sender

    @pytest.mark.parametrize("sender", [
        "dana@northwind.example",
        "replies@northwind.example",
        "noreplygroup@northwind.example",
        "jane.norepeat@example.test",
    ])
    def test_these_are_people(self, sender):
        assert not is_automated(message(sender=sender)), sender

    def test_the_header_a_robot_sets_is_honoured(self):
        """RFC 3834 exists so one autoresponder can recognise another."""
        assert is_automated(message(auto_submitted="auto-replied"))
        assert is_automated(message(auto_submitted="auto-generated"))
        assert not is_automated(message(auto_submitted="no"))

    def test_bulk_precedence_is_a_machine(self):
        assert is_automated(message(precedence="bulk"))
        assert is_automated(message(precedence="list"))
        assert not is_automated(message(precedence="normal"))

    def test_the_suppress_header_is_honoured(self):
        assert is_automated(message(suppress="OOF, AutoReply"))

    def test_a_rule_never_drafts_to_one(self):
        rule = drafting_rule()
        outcome = autoreply.apply_rules(
            [rule], message(sender="no-reply@shop.example"), verdict())
        assert outcome is None or outcome.draft is None
        if outcome is not None:
            assert any("machine" in reason for reason in outcome.held)

    def test_and_it_cannot_be_turned_off(self):
        """There is no setting for this. An autoresponder answering a
        bounce produces another bounce, and the loop only stops when
        somebody notices."""
        rule = drafting_rule()
        rule.skip_bulk = False
        outcome = autoreply.apply_rules(
            [rule], message(sender="mailer-daemon@shop.example"), verdict())
        assert outcome is None or outcome.draft is None

    def test_it_still_files_and_flags_a_machine_s_mail(self):
        """Only speaking is held back. Filing a receipt into a folder has
        not said anything to anybody."""
        rule = Rule(name="File it", enabled=True,
                    conditions=[Condition(field="sender", operator="contains",
                                          value="@")],
                    actions=[Action("file_into", "Receipts")],
                    skip_bulk=False)
        outcome = autoreply.apply_rules(
            [rule], message(sender="no-reply@shop.example"), verdict())
        assert outcome is not None
        assert outcome.file_into == "Receipts"


class TestItWritesToSomebodyOnce:
    def test_a_second_message_in_the_window_gets_nothing(self):
        log = reply_log.ReplyLog()
        rule = drafting_rule(once_per_sender_days=7)
        context = {"reply_log": log}

        first = autoreply.apply_rules([rule], message(), verdict(),
                                      context=context)
        assert first is not None and first.draft is not None
        log.remember(first.drafted_to, first.drafted_by)

        second = autoreply.apply_rules([rule], message(), verdict(),
                                       context=context)
        assert second is None or second.draft is None
        assert any("already wrote" in r for r in (second.held if second else []))

    def test_once_the_window_is_past_it_writes_again(self):
        log = reply_log.ReplyLog()
        rule = drafting_rule(once_per_sender_days=7)
        long_ago = datetime.now(timezone.utc) - timedelta(days=8)
        log.remember("dana@northwind.example", "Answer it", now=long_ago)
        outcome = autoreply.apply_rules([rule], message(), verdict(),
                                        context={"reply_log": log})
        assert outcome is not None and outcome.draft is not None

    def test_no_window_means_no_limit(self):
        log = reply_log.ReplyLog()
        log.remember("dana@northwind.example", "Answer it")
        rule = drafting_rule(once_per_sender_days=0)
        outcome = autoreply.apply_rules([rule], message(), verdict(),
                                        context={"reply_log": log})
        assert outcome is not None and outcome.draft is not None

    def test_two_rules_do_not_silence_each_other(self):
        """A rule acknowledging an interview and a rule declining a
        recruiter are two different conversations."""
        log = reply_log.ReplyLog()
        log.remember("dana@northwind.example", "Some other rule")
        rule = drafting_rule(name="Answer it", once_per_sender_days=7)
        outcome = autoreply.apply_rules([rule], message(), verdict(),
                                        context={"reply_log": log})
        assert outcome is not None and outcome.draft is not None

    def test_without_a_log_it_simply_does_not_limit(self):
        """The log is state, and state can be missing. A rule with a
        window and nothing to consult writes the draft rather than
        refusing to run."""
        rule = drafting_rule(once_per_sender_days=7)
        outcome = autoreply.apply_rules([rule], message(), verdict())
        assert outcome is not None and outcome.draft is not None


class TestItDoesNotWriteAtThreeInTheMorning:
    @staticmethod
    def _at(hour: int) -> datetime:
        return datetime.now().astimezone().replace(
            hour=hour, minute=0, second=0, microsecond=0)

    def test_outside_the_hours_it_says_nothing(self):
        rule = drafting_rule(active_from=9, active_to=18)
        outcome = autoreply.apply_rules(
            [rule], message(), verdict(), context={"now": self._at(3)})
        assert outcome is None or outcome.draft is None

    def test_inside_them_it_writes(self):
        rule = drafting_rule(active_from=9, active_to=18)
        outcome = autoreply.apply_rules(
            [rule], message(), verdict(), context={"now": self._at(11)})
        assert outcome is not None and outcome.draft is not None

    def test_the_default_is_any_time(self):
        rule = drafting_rule()
        for hour in (0, 3, 12, 23):
            outcome = autoreply.apply_rules(
                [rule], message(), verdict(), context={"now": self._at(hour)})
            assert outcome is not None and outcome.draft is not None, hour

    def test_a_window_that_ends_before_it_starts_means_any_time(self):
        """Nobody means "never", so it cannot be what that says."""
        rule = drafting_rule(active_from=18, active_to=9)
        assert rule.active_from == 0 and rule.active_to == 24

    def test_days_can_be_chosen(self):
        rule = drafting_rule(active_days=(0, 1, 2, 3, 4))
        monday = datetime(2026, 9, 14, 11, 0).astimezone()
        sunday = datetime(2026, 9, 20, 11, 0).astimezone()
        assert rule.may_draft(message(), {"now": monday})[0]
        assert not rule.may_draft(message(), {"now": sunday})[0]

    def test_filing_is_never_held_for_the_clock(self):
        rule = Rule(name="File it", enabled=True, active_from=9, active_to=18,
                    conditions=[Condition(field="sender", operator="contains",
                                          value="@")],
                    actions=[Action("file_into", "Somewhere")],
                    skip_bulk=False)
        outcome = autoreply.apply_rules(
            [rule], message(), verdict(), context={"now": self._at(3)})
        assert outcome is not None
        assert outcome.file_into == "Somewhere"


class TestItSaysWhyItSaidNothing:
    def test_the_reason_comes_back_in_words(self):
        rule = drafting_rule()
        outcome = autoreply.apply_rules(
            [rule], message(sender="no-reply@x.example"), verdict())
        assert outcome is not None
        assert outcome.held, "it held the draft back and said nothing about it"
        assert "loop" in " ".join(outcome.held)

    def test_a_reason_is_not_repeated_for_one_message(self):
        rule = Rule(name="Answer it", enabled=True,
                    conditions=[Condition(field="sender", operator="contains",
                                          value="@")],
                    actions=[Action("draft", "One"), Action("draft", "Two")],
                    skip_bulk=False)
        outcome = autoreply.apply_rules(
            [rule], message(sender="no-reply@x.example"), verdict())
        assert len(outcome.held) == 1


class TestTheNewConditions:
    def test_it_can_tell_mail_sent_to_you_from_mail_sent_to_a_list(self):
        rule = Condition(field="to_me_directly", operator="is_true")
        mine = message(to="you@icloud.example, someone@else.test")
        theirs = message(to="list@groups.example")
        context = {"me": "you@icloud.example"}
        assert rule.matches(mine, verdict(), context)
        assert not rule.matches(theirs, verdict(), context)

    def test_with_no_address_to_compare_it_says_no(self):
        rule = Condition(field="to_me_directly", operator="is_true")
        assert not rule.matches(message(to="anyone@x.test"), verdict(), {})

    def test_attachments_can_be_matched_by_name(self):
        rule = Condition(field="attachment_name", operator="contains",
                         value=".pdf")
        assert rule.matches(message(attachments=("offer.pdf",)), verdict())
        assert not rule.matches(message(attachments=("photo.png",)), verdict())

    def test_the_to_line_can_be_matched(self):
        rule = Condition(field="recipients", operator="contains",
                         value="jobs@")
        assert rule.matches(message(to="jobs@example.test"), verdict())

    def test_a_machine_can_be_matched_on_purpose(self):
        """So somebody can write "if it is a robot, file it and stop"."""
        rule = Condition(field="is_automated", operator="is_true")
        assert rule.matches(message(sender="no-reply@x.test"), verdict())
        assert not rule.matches(message(), verdict())


class TestTheLogItself:
    def test_it_survives_a_round_trip(self, tmp_path):
        path = tmp_path / "replies.json"
        log = reply_log.ReplyLog(path=path)
        log.remember("a@x.test", "Rule one")
        assert log.save(path)
        again = reply_log.ReplyLog.load(path)
        assert len(again) == 1
        assert again.last_to("a@x.test") is not None

    def test_a_missing_file_is_an_empty_log(self, tmp_path):
        assert len(reply_log.ReplyLog.load(tmp_path / "nothing.json")) == 0

    def test_damage_is_an_empty_log_and_not_an_error(self, tmp_path):
        path = tmp_path / "replies.json"
        path.write_text("{ this is not json")
        assert len(reply_log.ReplyLog.load(path)) == 0

    def test_the_address_is_folded(self):
        log = reply_log.ReplyLog()
        log.remember("Dana@Northwind.Example", "r")
        assert log.last_to("dana@northwind.example") is not None

    def test_it_does_not_grow_forever(self):
        log = reply_log.ReplyLog()
        ancient = datetime.now(timezone.utc) - timedelta(days=400)
        log.remember("old@x.test", "r", now=ancient)
        log.remember("new@x.test", "r")
        assert log.forget_old() == 1
        assert len(log) == 1

    def test_an_entry_with_no_date_is_kept_rather_than_guessed_at(self):
        log = reply_log.ReplyLog([reply_log.Sent("a@x.test", "r", "nonsense")])
        log.forget_old()
        assert len(log) == 1
        assert log.last_to("a@x.test") is None
