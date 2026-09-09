"""Verdicts kept from one scan to the next."""

from __future__ import annotations

import json

import pytest

import verdict_cache
from models import Category, Classification, EmailMessage, OtherCategory
from verdict_cache import VerdictCache, key_for, recipe_for


def message(uid="1", account="acct", folder="INBOX") -> EmailMessage:
    return EmailMessage(uid=uid, account_id=account, source_folder=folder,
                        subject="Interview on Thursday")


def verdict(**overrides) -> Classification:
    defaults = dict(summary="An interview invitation.", is_job_related=True,
                    category=Category.INTERVIEW,
                    other_category=OtherCategory.NOT_APPLICABLE,
                    confidence_score=0.94, reasoning="Calendly link.",
                    model="claude-opus-5")
    defaults.update(overrides)
    return Classification(**defaults)


class TestTheRecipe:
    def test_the_same_settings_hash_the_same(self):
        from config import Settings
        assert recipe_for(Settings()) == recipe_for(Settings())

    @pytest.mark.parametrize("field,value", [
        ("provider", "openai"), ("model", "something-else"),
        ("effort", "high"), ("ruleset", "software"),
        ("max_body_chars", 999), ("base_url", "http://elsewhere"),
    ])
    def test_anything_that_changes_the_answer_changes_the_hash(self, field, value):
        from dataclasses import replace
        from config import Settings
        base = Settings()
        assert recipe_for(base) != recipe_for(replace(base, **{field: value}))

    def test_things_that_do_not_change_the_answer_do_not(self):
        """The threshold and the folder names act on a verdict, not in it."""
        from dataclasses import replace
        from config import Settings
        base = Settings()
        for field, value in (("confidence_threshold", 0.55),
                             ("folder_root", "Elsewhere"),
                             ("auto_approve_non_job", True)):
            assert recipe_for(base) == recipe_for(replace(base, **{field: value}))


class TestTheKey:
    def test_it_is_mailbox_and_uid(self):
        assert key_for(message(uid="7")) != key_for(message(uid="8"))

    def test_two_mailboxes_can_share_a_uid(self):
        assert key_for(message(account="a")) != key_for(message(account="b"))

    def test_a_message_with_no_uid_has_no_key(self):
        assert key_for(message(uid="")) == ""


class TestHitsAndMisses:
    def test_a_stored_verdict_comes_back(self):
        cache = VerdictCache()
        cache.put(message(), "recipe-1", verdict())
        hit = cache.get(message(), "recipe-1")
        assert hit is not None
        assert hit.category is Category.INTERVIEW
        assert hit.confidence_score == pytest.approx(0.94)

    def test_a_different_recipe_is_a_miss(self):
        cache = VerdictCache()
        cache.put(message(), "recipe-1", verdict())
        assert cache.get(message(), "recipe-2") is None

    def test_a_different_message_is_a_miss(self):
        cache = VerdictCache()
        cache.put(message(uid="1"), "r", verdict())
        assert cache.get(message(uid="2"), "r") is None

    def test_a_failure_is_never_kept(self):
        """The network is usually why, and the network gets better."""
        cache = VerdictCache()
        assert cache.put(message(), "r", verdict(error="timed out")) is False
        assert cache.get(message(), "r") is None

    def test_a_fallback_is_never_kept(self):
        """Otherwise a stand-in answer outlives the outage that caused it."""
        cache = VerdictCache()
        stand_in = verdict(model="claude-opus-5 → local rules")
        assert cache.put(message(), "r", stand_in) is False
        assert cache.get(message(), "r") is None

    def test_a_message_with_no_uid_is_never_kept(self):
        assert VerdictCache().put(message(uid=""), "r", verdict()) is False


class TestSplittingAndMerging:
    def test_only_the_unknown_ones_go_to_the_model(self):
        cache = VerdictCache()
        cache.put(message(uid="2"), "r", verdict())
        messages = [message(uid=str(i)) for i in range(4)]

        pending, known = cache.split(messages, "r")
        assert [m.uid for m in pending] == ["0", "1", "3"]
        assert set(known) == {2}
        assert cache.hits == 1 and cache.misses == 3

    def test_the_halves_come_back_in_the_original_order(self):
        cache = VerdictCache()
        cache.put(message(uid="2"), "r", verdict(summary="from the cache"))
        messages = [message(uid=str(i)) for i in range(4)]
        pending, known = cache.split(messages, "r")

        fresh = [verdict(summary=f"fresh {m.uid}") for m in pending]
        merged = VerdictCache.merge(messages, pending, fresh, known)

        assert [c.summary for c in merged] == [
            "fresh 0", "fresh 1", "from the cache", "fresh 3"]

    def test_a_message_the_model_skipped_becomes_an_error_not_a_gap(self):
        messages = [message(uid="1"), message(uid="2")]
        merged = VerdictCache.merge(messages, messages, [verdict()], {})
        assert len(merged) == 2
        assert merged[1].error is not None

    def test_an_empty_cache_sends_everything(self):
        messages = [message(uid=str(i)) for i in range(3)]
        pending, known = VerdictCache().split(messages, "r")
        assert pending == messages and known == {}


class TestDisk:
    def test_round_trip(self, tmp_path):
        path = tmp_path / "verdicts.json"
        cache = VerdictCache(path=path)
        cache.put(message(), "r", verdict())
        cache.save()

        again = VerdictCache.load(path)
        assert len(again) == 1
        assert again.get(message(), "r").category is Category.INTERVIEW

    def test_saving_nothing_writes_nothing(self, tmp_path):
        path = tmp_path / "verdicts.json"
        assert VerdictCache(path=path).save() is None
        assert not path.exists()

    def test_the_file_is_private(self, tmp_path):
        path = tmp_path / "verdicts.json"
        cache = VerdictCache(path=path)
        cache.put(message(), "r", verdict())
        cache.save()
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_a_damaged_file_is_an_empty_cache(self, tmp_path):
        path = tmp_path / "verdicts.json"
        path.write_text("}{ not json")
        assert len(VerdictCache.load(path)) == 0

    def test_a_missing_file_is_an_empty_cache(self, tmp_path):
        assert len(VerdictCache.load(tmp_path / "nope.json")) == 0

    def test_junk_rows_are_dropped(self, tmp_path):
        path = tmp_path / "verdicts.json"
        path.write_text(json.dumps({"verdicts": [
            {"key": "a", "recipe": "r", "payload": {}, "when": "2099-01-01T00:00:00+00:00"},
            {"key": "", "recipe": "r", "payload": {}},
            {"key": "b", "recipe": "r"},
            "nonsense", None,
        ]}))
        assert len(VerdictCache.load(path)) == 1

    def test_stale_entries_are_dropped_on_load(self, tmp_path):
        path = tmp_path / "verdicts.json"
        path.write_text(json.dumps({"verdicts": [
            {"key": "old", "recipe": "r", "payload": {},
             "when": "2001-01-01T00:00:00+00:00"},
            {"key": "new", "recipe": "r", "payload": {},
             "when": "2099-01-01T00:00:00+00:00"},
        ]}))
        assert len(VerdictCache.load(path)) == 1

    def test_an_unwritable_path_does_not_raise(self, tmp_path):
        blocked = tmp_path / "a-file"
        blocked.write_text("not a directory")
        cache = VerdictCache(path=blocked / "verdicts.json")
        cache.put(message(), "r", verdict())
        assert cache.save() is None

    def test_it_is_capped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(verdict_cache, "MAX_ENTRIES", 5)
        path = tmp_path / "verdicts.json"
        cache = VerdictCache(path=path)
        for index in range(20):
            cache.put(message(uid=str(index)), "r", verdict())
        cache.save()
        assert len(VerdictCache.load(path)) == 5


class TestForgetting:
    def test_forget_one_mailbox(self):
        cache = VerdictCache()
        cache.put(message(uid="1", account="a"), "r", verdict())
        cache.put(message(uid="2", account="a"), "r", verdict())
        cache.put(message(uid="1", account="b"), "r", verdict())
        assert cache.forget_mailbox("a") == 2
        assert cache.get(message(uid="1", account="b"), "r") is not None

    def test_clear_empties_it(self):
        cache = VerdictCache()
        cache.put(message(), "r", verdict())
        cache.clear()
        assert len(cache) == 0


class TestInAScan:
    """The saving is the point, so measure it."""

    def test_a_repeat_scan_asks_the_model_for_nothing(self):
        from config import Settings
        settings = Settings()
        recipe = recipe_for(settings)
        messages = [message(uid=str(i)) for i in range(50)]

        first = VerdictCache()
        pending, known = first.split(messages, recipe)
        assert len(pending) == 50 and not known
        for msg in pending:
            first.put(msg, recipe, verdict())

        pending, known = first.split(messages, recipe)
        assert pending == [] and len(known) == 50

    def test_changing_the_model_asks_again(self):
        from dataclasses import replace
        from config import Settings
        settings = Settings()
        messages = [message(uid=str(i)) for i in range(10)]
        cache = VerdictCache()
        for msg in messages:
            cache.put(msg, recipe_for(settings), verdict())

        moved_on = replace(settings, model="a-different-model")
        pending, known = cache.split(messages, recipe_for(moved_on))
        assert len(pending) == 10 and not known


# ==========================================================================
# The whole scan, end to end
# ==========================================================================
class FakeIMAP:
    """Just enough IMAP to get a ScanWorker through its fetch phase."""

    delimiter = "/"

    def __init__(self, messages, **_kwargs):
        self._messages = messages

    def connect(self, *_a, **_k):
        return None

    def has_capability(self, _name):
        return True

    def folder_plan(self, root, other_root):
        from models import FolderPlan
        return FolderPlan(root=root, other_root=other_root, delimiter="/")

    def ensure_folders(self, *_a, **_k):
        return []

    def fetch_window(self, on_batch=None, **_kwargs):
        """Hand messages over in twos, the way a real fetch does."""
        from types import SimpleNamespace
        messages = list(self._messages)
        if on_batch is not None:
            for start in range(0, len(messages), 2):
                on_batch(messages[start:start + 2])
        return SimpleNamespace(messages=messages, warnings=[],
                               candidate_uids=[m.uid for m in messages])

    def logout(self):
        return None


class CountingLLM:
    """Counts what it was asked to classify, and answers everything."""

    def __init__(self, seen, **_kwargs):
        self._seen = seen
        self.model = "counting"
        self.batched_requests = 0
        self.fallback_count = 0
        self.degradations = []
        from llm_engine import LLMEngine
        self.usage = LLMEngine(api_key="k", provider="rules").usage

    def classify_many(self, messages, progress=None, cancel=None, observer=None):
        self._seen.extend(m.uid for m in messages)
        answers = [verdict(summary=f"fresh {m.uid}") for m in messages]
        if observer is not None:
            observer(answers)
        return answers

    def close(self):
        return None


@pytest.fixture
def scan_env(tmp_path, monkeypatch, qapp):
    """A ScanWorker wired to fakes, with its cache under tmp_path."""
    import workers
    from config import Settings

    monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
    mail = [message(uid=str(i)) for i in range(6)]
    seen = []
    monkeypatch.setattr(workers, "IMAPEngine",
                        lambda **kw: FakeIMAP(mail, **kw))
    monkeypatch.setattr(workers, "LLMEngine",
                        lambda **kw: CountingLLM(seen, **kw))

    def build(**overrides):
        settings = Settings(icloud_email="you@icloud.example",
                            provider="rules", **overrides)
        worker = workers.ScanWorker(
            settings=settings, mailbox_password="pw", api_key="",
            window_start=None, window_end=None)
        done = []
        worker.finished_ok.connect(done.append)
        return worker, done, seen

    return build


class TestTheWholeScan:
    def test_the_first_scan_classifies_everything(self, scan_env):
        worker, done, seen = scan_env()
        worker.run()
        assert seen == ["0", "1", "2", "3", "4", "5"]
        assert len(done[0].items) == 6

    def test_the_second_scan_classifies_nothing(self, scan_env):
        worker, done, seen = scan_env()
        worker.run()
        seen.clear()

        worker2, done2, _ = scan_env()
        worker2.run()
        assert seen == [], "a repeat scan should not reach the model at all"
        assert len(done2[0].items) == 6
        # And the rows are the same rows, in the same order.
        assert [i.email.uid for i in done2[0].items] == list("012345")
        assert [i.classification.summary for i in done2[0].items] == [
            f"fresh {u}" for u in "012345"]

    def test_turning_it_off_classifies_everything_again(self, scan_env):
        worker, _, seen = scan_env()
        worker.run()
        seen.clear()

        worker2, _, _ = scan_env(reuse_verdicts=False)
        worker2.run()
        assert seen == ["0", "1", "2", "3", "4", "5"]

    def test_rescanning_everything_bypasses_the_cache(self, scan_env):
        worker, _, seen = scan_env()
        worker.run()
        seen.clear()

        worker2, _, _ = scan_env()
        worker2.reuse_verdicts = False
        worker2.run()
        assert seen == ["0", "1", "2", "3", "4", "5"]

    def test_a_new_message_is_the_only_one_classified(self, scan_env, monkeypatch):
        import workers
        worker, _, seen = scan_env()
        worker.run()
        seen.clear()

        # A seventh message arrives.
        mail = [message(uid=str(i)) for i in range(7)]
        monkeypatch.setattr(workers, "IMAPEngine", lambda **kw: FakeIMAP(mail, **kw))
        worker2, done2, _ = scan_env()
        monkeypatch.setattr(workers, "IMAPEngine", lambda **kw: FakeIMAP(mail, **kw))
        worker2.run()
        assert seen == ["6"]
        assert len(done2[0].items) == 7

    def test_the_saving_is_reported(self, scan_env):
        worker, _, _ = scan_env()
        worker.run()
        worker2, done2, _ = scan_env()
        worker2.run()
        assert "reused" in done2[0].usage_text


class TestOverlappingTheTwoHalves:
    """The classifier must be working before the fetch has finished."""

    def test_messages_reach_the_classifier_while_the_fetch_runs(self, tmp_path,
                                                                monkeypatch, qapp):
        import threading
        import workers
        from config import Settings

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        mail = [message(uid=str(i)) for i in range(20)]
        classified_early = threading.Event()
        still_fetching = threading.Event()
        still_fetching.set()

        class SlowIMAP(FakeIMAP):
            def fetch_window(self, on_batch=None, **_kwargs):
                from types import SimpleNamespace
                for start in range(0, len(self._messages), 4):
                    if on_batch is not None:
                        on_batch(self._messages[start:start + 4])
                    # Give the classifier a moment to get going.
                    if classified_early.wait(0.5):
                        break
                still_fetching.clear()
                return SimpleNamespace(messages=list(self._messages), warnings=[],
                                       candidate_uids=[])

        class WatchingLLM(CountingLLM):
            def classify_many(self, messages, progress=None, cancel=None,
                              observer=None):
                if still_fetching.is_set():
                    classified_early.set()
                return super().classify_many(messages, progress, cancel, observer)

        seen = []
        monkeypatch.setattr(workers, "IMAPEngine", lambda **kw: SlowIMAP(mail, **kw))
        monkeypatch.setattr(workers, "LLMEngine", lambda **kw: WatchingLLM(seen, **kw))

        worker = workers.ScanWorker(
            settings=Settings(icloud_email="you@icloud.example", provider="rules",
                              batch_size=4),
            mailbox_password="pw", api_key="", window_start=None, window_end=None)
        done = []
        worker.finished_ok.connect(done.append)
        worker.run()

        assert classified_early.is_set(), \
            "classification should start before the fetch has finished"
        assert len(done[0].items) == 20
        assert sorted(int(u) for u in seen) == list(range(20))

    def test_every_message_still_gets_a_verdict_in_order(self, scan_env):
        worker, done, _ = scan_env()
        worker.run()
        items = done[0].items
        assert [i.email.uid for i in items] == list("012345")
        assert [i.classification.summary for i in items] == [
            f"fresh {u}" for u in "012345"]

    def test_a_fetch_that_streams_nothing_is_still_classified(self, tmp_path,
                                                              monkeypatch, qapp):
        """Overlap is a way of going faster, never a reason to lose a message."""
        import workers
        from config import Settings

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        mail = [message(uid=str(i)) for i in range(5)]

        class SilentIMAP(FakeIMAP):
            def fetch_window(self, on_batch=None, **_kwargs):
                from types import SimpleNamespace
                return SimpleNamespace(messages=list(self._messages),
                                       warnings=[], candidate_uids=[])

        seen = []
        monkeypatch.setattr(workers, "IMAPEngine", lambda **kw: SilentIMAP(mail, **kw))
        monkeypatch.setattr(workers, "LLMEngine", lambda **kw: CountingLLM(seen, **kw))
        worker = workers.ScanWorker(
            settings=Settings(icloud_email="you@icloud.example", provider="rules"),
            mailbox_password="pw", api_key="", window_start=None, window_end=None)
        done = []
        worker.finished_ok.connect(done.append)
        worker.run()

        assert sorted(seen) == ["0", "1", "2", "3", "4"]
        assert len(done[0].items) == 5

    def test_a_classifier_that_raises_fails_the_scan_rather_than_hanging(
            self, tmp_path, monkeypatch, qapp):
        import workers
        from config import Settings

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))
        mail = [message(uid=str(i)) for i in range(4)]

        class BrokenLLM(CountingLLM):
            def classify_many(self, *_a, **_k):
                raise RuntimeError("the provider fell over")

        monkeypatch.setattr(workers, "IMAPEngine", lambda **kw: FakeIMAP(mail, **kw))
        monkeypatch.setattr(workers, "LLMEngine", lambda **kw: BrokenLLM([], **kw))
        worker = workers.ScanWorker(
            settings=Settings(icloud_email="you@icloud.example", provider="rules"),
            mailbox_password="pw", api_key="", window_start=None, window_end=None)
        failures = []
        worker.failed.connect(lambda *a: failures.append(a))
        worker.run()
        assert failures, "the scan should report the failure, not hang"

    def test_a_fetch_that_fails_shuts_the_classifier_down(self, tmp_path,
                                                          monkeypatch, qapp):
        import workers
        from config import Settings

        monkeypatch.setenv("ICLOUD_TRIAGE_HOME", str(tmp_path))

        class BrokenIMAP(FakeIMAP):
            def fetch_window(self, **_kwargs):
                raise OSError("the connection dropped")

        monkeypatch.setattr(workers, "IMAPEngine",
                            lambda **kw: BrokenIMAP([], **kw))
        monkeypatch.setattr(workers, "LLMEngine", lambda **kw: CountingLLM([], **kw))
        worker = workers.ScanWorker(
            settings=Settings(icloud_email="you@icloud.example", provider="rules"),
            mailbox_password="pw", api_key="", window_start=None, window_end=None)
        failures = []
        worker.failed.connect(lambda *a: failures.append(a))
        worker.run()
        assert failures
        assert worker._classifier is None
