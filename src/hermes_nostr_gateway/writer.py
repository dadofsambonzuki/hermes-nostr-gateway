"""Store writes, off the event loop.

`Store.add` used to run its whole transaction inside the relay worker's read callback —
which *is* the event loop — so a burst stalled everything else with it: socket frames,
heartbeats, the other relays' reads, the digest timer. At ``synchronous=FULL`` (the
default) every message also paid two fsyncs.

Three changes move that work aside. This module is the third:

* WAL with ``synchronous=NORMAL`` (see ``store.py``) drops the per-commit fsync while
  keeping a committed row safe from ``kill -9``.
* Rows are inserted in batches: one transaction per flush instead of one per message.
* The flush runs on its own thread, so even a slow disk delays the *next* flush, never a
  frame.

What the queue costs
--------------------
``Store.add`` now answers from memory: True means accepted, and the row lands a few
milliseconds later. Dedupe does **not** move: ``offer`` still asks the database (a
primary-key lookup and one indexed range scan, reads only) before it accepts anything,
so a restart's replay of the last three days is suppressed exactly as it was before.
Only the *write* is deferred.

The window is bounded by ``tick`` (default 20 ms). ``stop()`` drains it, so a clean
shutdown loses nothing; a ``kill -9`` can lose at most the rows queued since the last
flush. The daemon closes even that for anything the agent is about to see: an immediate
frame is broadcast only after a flush (``Gateway._deliver``), which keeps the spec's
"a frame is delivered when it is inserted".

The writer is an optimisation, never the only check: the rumor and wrapper tables keep
their own uniqueness, and ``add_many`` re-runs the same dedupe inside its transaction.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from .store import Inbound, Store

log = logging.getLogger("hng.store")


class StoreWriter:
    """Batches store writes onto one thread, so the loop never waits for the disk."""

    #: Longest a queued row waits for a flush. The loop only pays for the append and the
    #: thread hand-off, so a short tick is cheap; it is also the kill -9 exposure.
    DEFAULT_TICK = 0.02
    #: Rows per transaction. A burst is a handful of transactions, a trickle is one row.
    DEFAULT_BATCH = 512
    #: Cursors reserved per `delivery_cursor` write. Purely a rate: a gap of up to this
    #: many is skipped if the process exits mid-block (cursors never move backwards).
    DEFAULT_CURSOR_BLOCK = 512
    #: How many recent ids to remember for the in-memory fast path.
    DEFAULT_RECENT = 20_000

    def __init__(self, store: Store, *, tick: float = DEFAULT_TICK,
                 batch_size: int = DEFAULT_BATCH,
                 cursor_block: int = DEFAULT_CURSOR_BLOCK,
                 recent: int = DEFAULT_RECENT,
                 executor: ThreadPoolExecutor | None = None):
        self.store = store
        self.tick = max(0.001, float(tick))
        self.batch_size = max(1, int(batch_size))
        self.cursor_block = max(1, int(cursor_block))
        self.recent = max(1, int(recent))
        self._executor = executor
        self._owns_executor = executor is None
        self._rumors: deque[Inbound] = deque()
        self._wrappers: deque[tuple[str, str]] = deque()
        self._relay_events: dict[str, int] = {}
        self._relay_errors: dict[str, str] = {}
        self._ids: OrderedDict[str, None] = OrderedDict()
        self._wrapper_ids: OrderedDict[str, None] = OrderedDict()
        #: `(sender, content) -> newest created_at` for rows queued but not yet
        #: committed. Without it the cross-plane dedupe would miss a message that is
        #: still in flight, because the store's own check can only see committed rows.
        self._texts: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._next_cursor: int | None = None
        self._cursors_left = 0
        self._counters: dict[str, Any] = {
            "rumors": 0, "wrappers": 0, "batches": 0, "duplicates": 0,
            "failed": 0, "dropped": 0, "last_batch": 0, "last_flush_ms": 0.0,
        }

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        if self._executor is None:
            self._executor = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="hng-store")
        self._stopping = False
        # Reserve the first cursor block while nothing else is writing, so the first
        # burst does not pay for it one message at a time.
        self._take_cursor()
        self._task = asyncio.create_task(self._run(), name="hng-store-writer")

    async def stop(self) -> None:
        """Stop the flusher, writing everything already queued."""
        self._stopping = True
        self._wake.set()
        task, self._task = self._task, None
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("store writer did not stop in 10s; cancelling the flusher")
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await self._drain()
        else:
            await self._drain()
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=True)
            self._executor = None

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.tick)
            except (asyncio.TimeoutError, TimeoutError):
                pass
            self._wake.clear()
            try:
                await self._drain()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - a bad batch must not kill the flusher
                log.exception("store flush failed")
        await self._drain()

    # -- queue (event-loop side, no I/O) --------------------------------
    def offer(self, msg: Inbound) -> bool:
        """Queue one rumor: True when it is new, False when it is a duplicate.

        The duplicate check is a read (`Store.duplicate_of`), which WAL lets us do
        while the writer thread is mid-transaction. Nothing is written here.
        """
        if msg.id in self._ids or self.store.duplicate_of(msg):
            self._counters["duplicates"] += 1
            return False
        key = (msg.sender, msg.content)
        if msg.authority != "self":
            last = self._texts.get(key)
            if last is not None and abs(int(msg.created_at) - int(last)) <= 2:
                # Already queued, so not yet visible to the store's own check.
                self._counters["duplicates"] += 1
                return False
        if msg.cursor is None:
            msg.cursor = self._take_cursor()
        self._remember(self._ids, msg.id)
        if msg.authority != "self":
            self._texts[key] = int(msg.created_at)
            while len(self._texts) > self.recent:
                self._texts.popitem(last=False)
        self._rumors.append(msg)
        self._wake_now()
        return True

    def note_wrapper(self, wrapper_id: str, relay: str = "") -> bool:
        """Queue a wrapper id; True when this process has already seen it.

        The database read that makes a restart's replay harmless happens in
        `Store.wrapper_seen`; this only covers the window between queue and commit,
        where a second relay can deliver the same wrapper.
        """
        if wrapper_id in self._wrapper_ids:
            return True
        self._remember(self._wrapper_ids, wrapper_id)
        self._wrappers.append((wrapper_id, relay))
        self._wake_now()
        return False

    def note_relay(self, relay: str, *, events: int = 0, error: str | None = None) -> None:
        """Queue a relay counter change; applied once per flush, summed per relay."""
        if events:
            self._relay_events[relay] = self._relay_events.get(relay, 0) + int(events)
        if error:
            self._relay_errors[relay] = str(error)[:300]
        if events or error:
            self._wake_now()

    async def flush(self) -> int:
        """Write everything queued now and return the number of rumors committed."""
        return await self._drain()

    def stats(self) -> dict[str, Any]:
        return {
            "queued": len(self._rumors),
            "wrappers_queued": len(self._wrappers),
            "relays_queued": len(self._relay_events) + len(self._relay_errors),
            "running": self._task is not None and not self._task.done(),
            "tick_ms": round(self.tick * 1000, 1),
            "batch_size": self.batch_size,
            **{key: (round(value, 2) if isinstance(value, float) else value)
               for key, value in self._counters.items()},
        }

    # -- internals ------------------------------------------------------
    @property
    def failures(self) -> int:
        """Rows a failed write gave up on. Reported, never swallowed."""
        return int(self._counters["failed"])

    def _wake_now(self) -> None:
        self._wake.set()

    def _remember(self, index: OrderedDict[str, None], key: str) -> None:
        index[key] = None
        while len(index) > self.recent:
            index.popitem(last=False)

    def _take_cursor(self) -> int:
        if self._cursors_left <= 0:
            first = self.store.reserve_cursors(self.cursor_block)
            self._next_cursor = first
            self._cursors_left = self.cursor_block
        assert self._next_cursor is not None  # set by the reservation above
        cursor = self._next_cursor
        self._next_cursor = cursor + 1
        self._cursors_left -= 1
        return cursor

    async def _drain(self) -> int:
        """Commit everything queued, and do not return until it is on disk.

        The lock is held across the executor hop on purpose. A second caller (an
        immediate frame waiting for its row) must not find the queue empty while a batch
        is still being written — it would broadcast a frame whose row is not there yet,
        and the ack that followed would update nothing.
        """
        async with self._lock:
            if not (self._rumors or self._wrappers
                    or self._relay_events or self._relay_errors):
                return 0
            rumors = list(self._rumors)
            self._rumors.clear()
            wrappers = list(self._wrappers)
            self._wrappers.clear()
            relay_events, self._relay_events = self._relay_events, {}
            relay_errors, self._relay_errors = self._relay_errors, {}
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="hng-store")
                self._owns_executor = True
            executor = self._executor
            started = time.perf_counter()
            loop = asyncio.get_running_loop()
            written = await loop.run_in_executor(
                executor, self._write, rumors, wrappers, relay_events, relay_errors)
            self._counters["last_flush_ms"] = (time.perf_counter() - started) * 1000
            return written

    def _write(self, rumors: list[Inbound], wrappers: list[tuple[str, str]],
               relay_events: dict[str, int], relay_errors: dict[str, str]) -> int:
        """Commit one drained batch. Runs on the writer thread, never on the loop."""
        written = 0
        if rumors:
            for start in range(0, len(rumors), self.batch_size):
                chunk = rumors[start:start + self.batch_size]
                accepted = self._insert(chunk)
                if accepted is None:
                    continue
                written += sum(1 for ok in accepted if ok)
                self._counters["duplicates"] += sum(1 for ok in accepted if not ok)
                self._counters["batches"] += 1
                self._counters["last_batch"] = len(chunk)
        if wrappers:
            if self._attempt(self.store.wrappers_seen, wrappers) is not None:
                self._counters["wrappers"] += len(wrappers)
        if relay_events or relay_errors:
            rows = []
            for relay in set(relay_events) | set(relay_errors):
                rows.append({"relay": relay, "events": relay_events.get(relay, 0),
                             "error": relay_errors.get(relay)})
            self._attempt(self.store.note_relays, rows)
        self._counters["rumors"] += written
        return written

    def _insert(self, chunk: list[Inbound]) -> list[bool] | None:
        """`add_many` with one retry; None when the batch was given up on."""
        return self._attempt(self.store.add_many, chunk)

    def _attempt(self, function: Any, chunk: Any) -> Any:
        """Run one store call, retrying once: a momentary lock must not lose a batch."""
        try:
            return function(chunk)
        except sqlite3.OperationalError as exc:
            log.warning("store write retrying after %s: %s", type(exc).__name__, exc)
        except Exception:  # noqa: BLE001 - never let a batch take the writer down
            log.exception("store write failed (%d row(s))", len(chunk))
            self._counters["failed"] += len(chunk)
            return None
        time.sleep(0.05)
        try:
            return function(chunk)
        except Exception:  # noqa: BLE001
            log.exception("store write failed twice (%d row(s)); dropped", len(chunk))
            self._counters["failed"] += len(chunk)
            self._counters["dropped"] += len(chunk)
            return None
