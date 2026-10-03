"""Fetching and classifying at the same time.

The fetch runs on its own thread and hands messages over as they land, and
the classifier (a hosted model, a local one, or the rules engine) works on
whatever has arrived, so neither waits for the other.

Three things it does not change:

**The order.** Messages arrive in whatever order the connections finish; the
caller still gets them in its own order, restored at the end, since
enforcing it during would mean waiting.

**Failures.** An exception on the fetch thread is re-raised on the consuming
thread where the next batch would have been read, as if the fetch were a
plain call.

**Cancellation.** Both halves watch the same event: the producer stops at
its next batch boundary and the consumer at its next group.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Iterator, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Sent down the queue when the producer has nothing more to give.
_DONE = object()

#: How many batches may sit unclaimed before the producer waits: few, so the
#: fetch cannot race ahead and rebuild the memory spike this exists to avoid.
QUEUE_DEPTH = 4


class Pipeline:
    """Runs a producer on its own thread and yields what it produces: one
    producer, one consumer and a queue between them, the whole shape of the
    problem.
    """

    def __init__(self, name: str = "fetch") -> None:
        self._queue: "queue.Queue" = queue.Queue(maxsize=QUEUE_DEPTH)
        self._thread: Optional[threading.Thread] = None
        self._error: Optional[BaseException] = None
        self._name = name

    def start(self, produce: Callable[[Callable[[object], None]], None]) -> None:
        """Run ``produce`` on a thread, giving it a function to emit with."""
        if self._thread is not None:
            raise RuntimeError("this pipeline has already been started")

        def run() -> None:
            try:
                produce(self._emit)
            except BaseException as exc:  # noqa: BLE001 - handed to the consumer
                self._error = exc
            finally:
                self._queue.put(_DONE)

        self._thread = threading.Thread(target=run, name=f"{self._name}-producer",
                                        daemon=True)
        self._thread.start()

    def _emit(self, item: object) -> None:
        self._queue.put(item)

    def __iter__(self) -> Iterator:
        """Yield each batch, then re-raise anything the producer hit."""
        if self._thread is None:
            raise RuntimeError("nothing has been started")
        while True:
            item = self._queue.get()
            if item is _DONE:
                break
            yield item
        self.join()
        if self._error is not None:
            raise self._error

    def join(self, timeout: Optional[float] = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def drain(self) -> None:
        """Stop caring about the rest without leaving the producer blocked,
        when the consumer gives up. The producer watches the same cancel
        event; this keeps it from waiting for room in a queue nobody is
        reading.
        """
        while self._thread is not None and self._thread.is_alive():
            try:
                if self._queue.get(timeout=0.1) is _DONE:
                    break
            except queue.Empty:
                continue
        self.join(timeout=5.0)


def in_original_order(originals: Sequence, produced: dict) -> List:
    """Put results back into the order the caller expects.

    ``produced`` is keyed by ``id`` of the item, which is safe here and only
    here: every object in it is alive for the whole call, held by
    ``originals``, so no id can have been recycled.
    """
    return [produced.get(id(item)) for item in originals]


class ClassifyPump:
    """Classifies messages as they arrive, on a thread of its own.

    The fetch threads :meth:`offer` each group they finish; this waits for a
    worthwhile number and classifies them, so the model works on the first
    fifty while the mailbox hands over the next fifty.

    Results are held by ``id`` of the message, since positions mean nothing
    with several connections finishing at once; :func:`in_original_order`
    puts them back.
    """

    def __init__(self, classify: Callable[[List], List], *,
                 batch_size: int = 25,
                 cancel: Optional[threading.Event] = None,
                 on_done: Optional[Callable[[int], None]] = None) -> None:
        self._classify = classify
        #: Wait for at least this many before starting, so the first call is
        #: not a batch of one; the remainder is flushed at the end.
        self._batch_size = max(1, int(batch_size))
        self._cancel = cancel
        self._on_done = on_done
        self._queue: "queue.Queue" = queue.Queue()
        self._results: dict = {}
        self._error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._count = 0
        self._lock = threading.Lock()

    # -- from the fetch threads ------------------------------------------
    def offer(self, messages: Sequence) -> None:
        """Hand over a group of messages. Called from whoever fetched them."""
        if messages:
            self._queue.put(list(messages))

    # -- from the scan thread --------------------------------------------
    def start(self) -> "ClassifyPump":
        self._thread = threading.Thread(target=self._run, name="triage-classify",
                                        daemon=True)
        self._thread.start()
        return self

    def finish(self) -> dict:
        """Say there is no more, wait, and hand back every verdict.

        Re-raises anything the classifying thread hit, at the point the caller
        would have hit it had this been an ordinary function call.
        """
        self._queue.put(_DONE)
        if self._thread is not None:
            self._thread.join()
        if self._error is not None:
            raise self._error
        return dict(self._results)

    @property
    def done_count(self) -> int:
        with self._lock:
            return self._count

    # -- the thread itself -----------------------------------------------
    def _run(self) -> None:
        waiting: List = []
        try:
            while True:
                item = self._queue.get()
                if item is _DONE:
                    self._flush(waiting)
                    return
                waiting.extend(item)
                if self._cancelled():
                    return
                # Only when enough has arrived, and only down to a multiple of
                # the batch size: the tail waits for its neighbours rather
                # than going out as a batch of three.
                while len(waiting) >= self._batch_size:
                    take, waiting = (waiting[:self._batch_size],
                                     waiting[self._batch_size:])
                    self._flush(take)
                    if self._cancelled():
                        return
        except BaseException as exc:  # noqa: BLE001 - handed to finish()
            self._error = exc

    def _cancelled(self) -> bool:
        return self._cancel is not None and self._cancel.is_set()

    def _flush(self, chunk: List) -> None:
        if not chunk:
            return
        verdicts = self._classify(chunk)
        for message, verdict in zip(chunk, verdicts):
            self._results[id(message)] = verdict
        with self._lock:
            self._count += len(chunk)
        if self._on_done is not None:
            self._on_done(self._count)
