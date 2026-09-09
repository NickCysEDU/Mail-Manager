"""Fetching and classifying at the same time."""

from __future__ import annotations

import threading
import time

import pytest

from pipeline import ClassifyPump, Pipeline, in_original_order


class TestThePipeline:
    def test_it_yields_what_the_producer_emits(self):
        pipe = Pipeline()
        pipe.start(lambda emit: [emit(n) for n in range(5)])
        assert list(pipe) == [0, 1, 2, 3, 4]

    def test_a_producer_that_emits_nothing_ends_cleanly(self):
        pipe = Pipeline()
        pipe.start(lambda emit: None)
        assert list(pipe) == []

    def test_an_error_reaches_the_consumer(self):
        pipe = Pipeline()

        def produce(emit):
            emit(1)
            raise ValueError("the mailbox went away")

        pipe.start(produce)
        with pytest.raises(ValueError, match="mailbox went away"):
            list(pipe)

    def test_what_was_produced_before_the_error_still_arrives(self):
        pipe = Pipeline()

        def produce(emit):
            emit("first")
            raise RuntimeError("then it broke")

        pipe.start(produce)
        seen = []
        with pytest.raises(RuntimeError):
            for item in pipe:
                seen.append(item)
        assert seen == ["first"]

    def test_it_cannot_be_started_twice(self):
        pipe = Pipeline()
        pipe.start(lambda emit: None)
        with pytest.raises(RuntimeError):
            pipe.start(lambda emit: None)

    def test_iterating_before_starting_is_an_error(self):
        with pytest.raises(RuntimeError):
            list(Pipeline())

    def test_a_slow_consumer_does_not_lose_anything(self):
        pipe = Pipeline()
        pipe.start(lambda emit: [emit(n) for n in range(50)])
        seen = []
        for item in pipe:
            seen.append(item)
            time.sleep(0.001)
        assert seen == list(range(50))

    def test_draining_releases_a_blocked_producer(self):
        """The queue is shallow, so a producer with more to give will block."""
        pipe = Pipeline()
        started = threading.Event()

        def produce(emit):
            started.set()
            for n in range(200):
                emit(n)

        pipe.start(produce)
        started.wait(2)
        pipe.drain()      # must return rather than hang
        pipe.join(timeout=2)


class TestPuttingItBackInOrder:
    def test_it_follows_the_originals(self):
        a, b, c = object(), object(), object()
        produced = {id(c): "c", id(a): "a", id(b): "b"}
        assert in_original_order([a, b, c], produced) == ["a", "b", "c"]

    def test_a_missing_one_becomes_a_gap_not_a_shift(self):
        a, b = object(), object()
        assert in_original_order([a, b], {id(a): "a"}) == ["a", None]


class TestTheClassifyPump:
    def test_everything_offered_comes_back(self):
        pump = ClassifyPump(lambda chunk: [f"v{m}" for m in chunk],
                            batch_size=3).start()
        messages = list(range(10))
        for start in range(0, 10, 2):
            pump.offer(messages[start:start + 2])
        results = pump.finish()
        assert in_original_order(messages, results) == [f"v{n}" for n in range(10)]

    def test_the_tail_is_never_left_behind(self):
        """Seven messages with a batch of five is five then two, not five."""
        calls = []

        def classify(chunk):
            calls.append(len(chunk))
            return [None] * len(chunk)

        pump = ClassifyPump(classify, batch_size=5).start()
        pump.offer(list(range(7)))
        pump.finish()
        assert calls == [5, 2]

    def test_it_waits_for_a_worthwhile_batch(self):
        calls = []

        def classify(chunk):
            calls.append(len(chunk))
            return [None] * len(chunk)

        pump = ClassifyPump(classify, batch_size=10).start()
        for _ in range(6):
            pump.offer([object(), object()])
        pump.finish()
        # Twelve messages: ten went out together, two followed at the end.
        assert sum(calls) == 12
        assert calls[0] == 10 or calls == [12]

    def test_less_than_a_batch_still_all_goes_out(self):
        calls = []

        def classify(chunk):
            calls.append(len(chunk))
            return [None] * len(chunk)

        pump = ClassifyPump(classify, batch_size=10).start()
        pump.offer([object(), object(), object()])
        pump.finish()
        assert calls == [3]

    def test_nothing_offered_is_no_work_at_all(self):
        calls = []
        pump = ClassifyPump(lambda chunk: calls.append(chunk) or [],
                            batch_size=4).start()
        assert pump.finish() == {}
        assert calls == []

    def test_an_error_surfaces_at_finish(self):
        def classify(chunk):
            raise ValueError("the provider said no")

        pump = ClassifyPump(classify, batch_size=1).start()
        pump.offer([1])
        with pytest.raises(ValueError, match="provider said no"):
            pump.finish()

    def test_cancelling_stops_it(self):
        cancel = threading.Event()
        seen = []

        def classify(chunk):
            seen.extend(chunk)
            cancel.set()
            return [None] * len(chunk)

        pump = ClassifyPump(classify, batch_size=2, cancel=cancel).start()
        pump.offer(list(range(20)))
        pump.finish()
        assert len(seen) < 20, "it should have stopped part way"

    def test_it_counts_as_it_goes(self):
        seen = []
        pump = ClassifyPump(lambda chunk: [None] * len(chunk), batch_size=5,
                            on_done=seen.append).start()
        pump.offer(list(range(15)))
        pump.finish()
        assert seen == [5, 10, 15]
        assert pump.done_count == 15

    def test_offers_from_several_threads_all_arrive(self):
        """The parallel fetch calls offer from four connections at once."""
        pump = ClassifyPump(lambda chunk: list(chunk), batch_size=7).start()
        messages = [object() for _ in range(120)]

        def feed(shard):
            for start in range(0, len(shard), 3):
                pump.offer(shard[start:start + 3])

        threads = [threading.Thread(target=feed, args=(messages[i::4],))
                   for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        results = pump.finish()
        assert len(results) == 120
        assert in_original_order(messages, results) == messages
