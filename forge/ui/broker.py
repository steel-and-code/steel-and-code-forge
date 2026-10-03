"""Fan-out of observational events to any number of SSE subscribers.

The problem this solves is a thread boundary. Events are emitted from the
worker thread that runs :class:`~forge.agents.runtime.AgentRuntime` -- that is
where the work happens -- and consumed by ``async`` generators serving HTTP
responses on the event loop. ``asyncio.Queue`` is not thread-safe, and
``loop.call_soon_threadsafe`` is the sanctioned way across that boundary, so the
broker owns the loop reference and does the hop itself.

Three properties are load-bearing, and each is a decision rather than a default:

*   **A slow subscriber must never slow the run.** Every subscriber has its own
    bounded queue. When one is full its events are dropped and counted, and the
    drop is visible to that subscriber as a gap rather than being hidden -- a
    browser that silently missed an ``approval_required`` would show an
    operator a run that appears to have stalled for no reason. Dropping an
    observation is acceptable; being wrong about what was observed is not.

*   **Memory is bounded by construction.** Per-subscriber queue depth is fixed,
    the replay ring is fixed, and event payloads are already clamped by
    :class:`forge.events.Event`. There is no unbounded list anywhere in this
    module, so a client that connects and never reads cannot grow the process.

*   **Replay is in-memory and small.** A browser that reconnects sends the last
    sequence number it saw and receives the tail from a fixed-size ring. This
    is a convenience for a refresh, not a durable log: Forge keeps no session
    history, and a subscriber that was away longer than the ring is told so
    explicitly instead of being handed a stream with a hole in it.

The broker is not an event sink of its own concern -- it implements
:meth:`emit`, so it *is* an :class:`~forge.events.EventSink` and is passed to the
runtime and dispatcher as one.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
from collections import deque
from dataclasses import dataclass

from ..events import Event

__all__ = [
    "SseBroker",
    "Subscriber",
    "MAX_SUBSCRIBER_QUEUE",
    "REPLAY_RING",
]

#: Events buffered per subscriber before that subscriber starts losing them.
#: Sized for a burst of activity in one run, not for a client that is not
#: reading -- the latter is handled by dropping, not by growing.
MAX_SUBSCRIBER_QUEUE = 256

#: Events retained for reconnect replay. A refresh reconnects within a second
#: or two, so this only needs to cover a short disconnection.
REPLAY_RING = 200

#: Heartbeat interval. Long enough not to be traffic, short enough that an
#: intermediary or a sleeping laptop does not silently break the stream.
HEARTBEAT_SECONDS = 15.0


@dataclass
class Subscriber:
    """One connected browser stream.

    Attributes:
        queue: Bounded at construction from the broker's ``max_queue``. Filled by
            the broker on the event loop, drained by the SSE generator.
        dropped: How many events this subscriber missed because its queue was
            full. Reported in the ``stream.dropped`` notice so the gap is
            explicit.
        identifier: Stable handle used for unsubscribe and logging. Sequential
            within a broker, which is enough -- nothing depends on it being
            unpredictable.
    """

    identifier: int
    queue: asyncio.Queue
    dropped: int = 0

    def take_dropped(self) -> int:
        """Read and reset the drop counter."""
        count = self.dropped
        self.dropped = 0
        return count


class SseBroker:
    """Delivers events to SSE subscribers, across the thread boundary.

    Args:
        max_queue: Per-subscriber queue depth.
        replay: Events retained for reconnect.
    """

    def __init__(
        self,
        *,
        max_queue: int = MAX_SUBSCRIBER_QUEUE,
        replay: int = REPLAY_RING,
    ) -> None:
        self._max_queue = max_queue
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subscribers: dict[int, Subscriber] = {}
        self._ring: deque[tuple[int, Event]] = deque(maxlen=replay)
        self._ids = itertools.count(1)
        self._seq = itertools.count(1)
        self._lock = threading.Lock()
        self._published = 0

    # -- lifecycle -------------------------------------------------------

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Record the event loop that delivers to subscribers.

        Called once at application startup. Without it the broker still accepts
        events and keeps its replay ring -- they just reach no subscriber, so a
        ``forge workbench`` that failed to bind serves the page and silently
        shows nothing. Better that binding is explicit and visible.
        """
        self._loop = loop

    # -- publishing ------------------------------------------------------

    def emit(self, event: Event) -> None:
        """:class:`~forge.events.EventSink`. Called from the worker thread.

        Sequence numbering, ring append, and subscriber lookup all happen here,
        under a lock, on the calling thread; only the queue put is deferred to
        the loop. Doing the bookkeeping on the worker keeps the loop free and
        makes the ordering of events match the order they were produced.
        """
        with self._lock:
            seq = next(self._seq)
            self._ring.append((seq, event))
            self._published += 1
            targets = [
                subscriber
                for subscriber in self._subscribers.values()
                if not subscriber.queue.full()
            ]
            full = [
                subscriber
                for subscriber in self._subscribers.values()
                if subscriber.queue.full()
            ]
            for subscriber in full:
                subscriber.dropped += 1

        loop = self._loop
        if loop is None:
            return
        try:
            loop.call_soon_threadsafe(self._deliver, seq, event, targets)
        except RuntimeError:
            # The loop is closed -- the server is shutting down. The run is
            # still running, and it should finish; there is simply nowhere left
            # to deliver to.
            return

    def _deliver(
        self,
        seq: int,
        event: Event,
        targets: list[Subscriber],
    ) -> None:
        """Put one event into each subscriber queue. Runs on the event loop."""
        for subscriber in targets:
            try:
                subscriber.queue.put_nowait((seq, event))
            except asyncio.QueueFull:  # pragma: no cover - race guard
                subscriber.dropped += 1

    # -- subscribing -----------------------------------------------------

    def subscribe(self) -> Subscriber:
        """Register a new stream. Returns the handle the generator drains.

        Not thread-safe against :meth:`emit`'s subscriber scan, and does not
        need to be: subscription happens on the event loop from a request
        handler, while ``emit`` runs on the worker thread and takes the lock.
        The lock is taken here too, so a subscription concurrent with a publish
        is ordered rather than racy.
        """
        subscriber = Subscriber(
            identifier=next(self._ids), queue=asyncio.Queue(self._max_queue)
        )
        with self._lock:
            self._subscribers[subscriber.identifier] = subscriber
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        """Remove a stream. Safe to call twice.

        Called from a generator's ``finally``, which runs on disconnect, on
        cancellation, and on an error -- so idempotence is required rather than
        merely tidy.
        """
        with self._lock:
            self._subscribers.pop(subscriber.identifier, None)

    @property
    def subscriber_count(self) -> int:
        """Currently connected streams."""
        with self._lock:
            return len(self._subscribers)

    @property
    def published(self) -> int:
        """Events published since start. Exposed for tests and diagnostics."""
        return self._published

    # -- replay ----------------------------------------------------------

    def replay_since(self, last_seq: int) -> tuple[list[tuple[int, Event]], bool]:
        """Events after ``last_seq``, and whether the ring had moved past them.

        Returns:
            ``(events, complete)``. ``complete`` is ``False`` when the oldest
            retained event is newer than ``last_seq + 1``, meaning the gap is
            larger than the ring and the returned list does not tell the whole
            story. The caller is expected to say so rather than present a
            partial stream as if it were whole.
        """
        with self._lock:
            entries = list(self._ring)
        if not entries:
            return [], True
        pending = [(seq, event) for seq, event in entries if seq > last_seq]
        oldest = entries[0][0]
        return pending, oldest <= last_seq + 1