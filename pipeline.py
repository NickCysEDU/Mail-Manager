"""Fetching and classifying at the same time.

A scan used to be two phases end to end: download everything from every
mailbox, then hand the lot to the classifier. That is one machine waiting for
another twice over. The network is idle for the whole of the second half, and
the classifier - a model on a server, a model on this Mac, or the rules engine
- is idle for the whole of the first.

So the fetch runs on its own thread and hands finished messages over as they
land. The classifier takes whatever has arrived and gets on with it, which
means the last message is being classified moments after it is downloaded
rather than after everything else has been.

Three things this deliberately does not change:

**The order.** Messages come off the wire in whatever order the connections
finish, and the caller still gets them sorted the way it always was. Order is
restored at the end rather than enforced during, because enforcing it during
would mean waiting - which is the thing being removed.

**Failures.** An exception on the fetch thread is re-raised on the consuming
thread, at the point where the next batch would have been read. From the
caller's point of view a fetch failure looks exactly as it did when the fetch
was a plain function call.

**Cancellation.** Both halves watch the same event. Setting it stops the
producer at its next batch boundary and the consumer at its next group.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Iterator, List, Optional, Sequence

log = logging.getLogger(__name__)

#: Sent down the queue when the producer has nothing more to give.
_DONE = object()

#: How many batches may sit unclaimed before the producer waits. Small, on
#: purpose: a deep queue would let the fetch race ahead and rebuild exactly
#: the memory spike this exists to avoid.
QUEUE_DEPTH = 4


class Pipeline:
    """Runs a producer on its own thread and yields what it produces.

    Deliberately not a general-purpose worker pool. It is one producer, one
    consumer, and a queue between them, because that is the whole shape of the
    problem and anything more would be harder to reason about at the point
    where a scan goes wrong.
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
        """Stop caring about the rest, without leaving the producer blocked.

        Used when the consumer gives up - a cancellation, or an error of its
        own. The producer is watching the same cancel event and will stop on
        its own; this just makes sure it is never stuck waiting for room in a
        queue nobody is reading.
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

    The fetch threads call :meth:`offer` with each group of messages they
    finish. This thread takes them, waits until it has a worthwhile number,
    and classifies them - so the model is working on the first fifty messages
    while the mailbox is still handing over the next fifty.

    It holds results by ``id`` of the message object rather than by position,
    because position stops meaning anything once several connections are
    finishing at once. :func:`in_original_order` puts them back.
    """

    def __init__(self, classify: Callable[[List], List], *,
                 batch_size: int = 25,
                 cancel: Optional[threading.Event] = None,
                 on_done: Optional[Callable[[int], None]] = None) -> None:
        self._classify = classify
        #: Wait for at least this many before starting, so the first call is
        #: not a batch of one. The remainder is always flushed at the end, so
        #: nothing is left behind by the threshold.
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
