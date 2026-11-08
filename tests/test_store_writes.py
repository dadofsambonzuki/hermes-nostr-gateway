"""P6: store writes off the event loop — batching, durability, and the burst claim.

The claim this file pins down (plan-v2 §P6): a 500-event burst costs the event loop an
append, not a transaction — rows land off the loop, committed rows survive ``kill -9``,
and a burst costs a handful of transactions rather than one per message. The plan's
wall-clock half of that claim, p99 loop lag under 50 ms, is measured by
``benchmarks/store_burst.py`` against the machine named there: it is a property of the
host as much as of the code (the same commit measures a p99 of 0.2-4 ms on this host
and 64-317 ms on the shared CI runner), so it is not asserted by the CI gate.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from hermes_nostr_gateway.store import Inbound, Store
from hermes_nostr_gateway.writer import StoreWriter

SRC = Path(__file__).resolve().parents[1] / "src"


def inbound(index: int, *, sender: str = "aa" * 32, text: str | None = None,
            reach: str = "immediate", rule: str = "test") -> Inbound:
    return Inbound(
        id=f"r{index:05d}", wrapper_id=f"w{index:05d}", sender=sender, kind=14,
        created_at=1_700_000_000 + index, content=text if text is not None else f"burst {index}",
        conversation=sender, reply_to=None, plane="nip17", reach=reach,
        authority="instructions", rule=rule, why="test",
    )


# -- batching -----------------------------------------------------------
def test_add_many_is_one_transaction_for_the_whole_batch(tmp_path):
    with Store(tmp_path / "batch.db") as store:
        before = store.transactions
        results = store.add_many([inbound(i) for i in range(200)])

        assert results == [True] * 200
        assert store.transactions - before == 1, "a batch must not commit row by row"
        assert store.stats()["total"] == 200
        # Cursors are contiguous because this batch allocated them in its own
        # transaction, and the order of the numbers is the order of the list.
        cursors = [row["cursor"] for row in store.recent(200)]
        assert sorted(cursors) == list(range(1, 201))


def test_a_burst_through_the_writer_costs_a_handful_of_transactions(tmp_path):
    async def burst() -> tuple[int, Store]:
        store = Store(tmp_path / "burst.db")
        writer = StoreWriter(store)
        await writer.start()
        store.attach_writer(writer)
        try:
            for i in range(500):
                assert store.add(inbound(i)) is True
            await store.flush()
        finally:
            await writer.stop()
        return store.transactions, store

    transactions, store = asyncio.run(burst())
    try:
        assert store.stats()["total"] == 500
        # Two cursor blocks at most, plus a flush or two: the point is that this is
        # bounded by flushes, not by messages (500 would be the old cost).
        assert transactions <= 10, f"{transactions} transactions for 500 messages"
    finally:
        store.close()


def test_duplicate_ids_never_enter_the_queue(tmp_path):
    async def run() -> tuple[list[bool], Store]:
        store = Store(tmp_path / "dup.db")
        writer = StoreWriter(store)
        await writer.start()
        store.attach_writer(writer)
        try:
            first = store.add(inbound(1))
            again = store.add(inbound(1))
            await store.flush()
            return [first, again], store
        finally:
            await writer.stop()

    results, store = asyncio.run(run())
    try:
        assert results == [True, False]
        assert store.stats()["total"] == 1
    finally:
        store.close()


def test_same_text_across_planes_is_still_one_message(tmp_path):
    """The dedupe that lives in the transaction must not move to the writer's memory."""
    async def run() -> Store:
        store = Store(tmp_path / "cross.db")
        writer = StoreWriter(store)
        await writer.start()
        store.attach_writer(writer)
        try:
            first = inbound(7, text="Agreed on both calls.")
            second = inbound(8, text="Agreed on both calls.")
            second.plane = "nip04"
            second.created_at = first.created_at + 1
            assert store.add(first) is True
            assert store.add(second) is False
            await store.flush()
        finally:
            await writer.stop()
        return store

    store = asyncio.run(run())
    try:
        assert store.stats()["total"] == 1
    finally:
        store.close()


def test_relay_counters_are_summed_once_per_flush(tmp_path):
    async def run() -> Store:
        store = Store(tmp_path / "relays.db")
        writer = StoreWriter(store)
        await writer.start()
        store.attach_writer(writer)
        try:
            for _ in range(25):
                store.note_relay("wss://a.test", events=1)
            await store.flush()
        finally:
            await writer.stop()
        return store

    store = asyncio.run(run())
    try:
        rows = {row["relay"]: row for row in store.relay_report()}
        assert rows["wss://a.test"]["events"] == 25
    finally:
        store.close()


# -- the burst claim ----------------------------------------------------
def test_the_loop_connection_counts_inline_writes(tmp_path):
    """The burst gate's instrument: without the writer, the loop's connection does write.

    `test_a_500_event_burst_keeps_the_loop_responsive` reads an unchanged row counter on
    the event-loop thread's own connection as "the loop did not write". That only means
    something if the counter is the right instrument, so this pins it against the pre-P6
    shape: `store.add` with no writer attached inserts on the loop thread's connection,
    and a rejected duplicate changes nothing.
    """
    store = Store(tmp_path / "inline.db")
    try:
        before = store.db.total_changes
        assert store.add(inbound(1)) is True
        after_first = store.db.total_changes
        assert after_first > before, "an inline add changed no rows on the loop's connection"
        assert store.add(inbound(1)) is False
        assert store.db.total_changes == after_first, "a rejected duplicate changed a row"
    finally:
        store.close()


async def test_a_500_event_burst_keeps_the_loop_responsive(tmp_path):
    """The writer's own work, not the runner's scheduling: no row written by the loop.

    This is the CI gate for the P6 claim, and it asks the question the plan cares about —
    is the work still off the loop? — rather than how the CI runner felt about
    scheduling:

    * during the 500 accepts the event-loop thread's own SQLite connection changes no
      rows: the inserts happen on the writer's connection, on its thread;
    * the loop thread spends bounded wall time accepting them, measured around each
      ``store.add`` and asserted as a **sum** (35-46 ms here against a 500 ms bound; the
      pre-P6 shape spent 500+ ms). A sum, not a p99 or a max: a *wait* is the shape of
      the original defect — the per-message fsync was I/O wait, which costs wall time and
      almost no CPU — so the sum catches a wait, while a runner descheduling the process
      inflates only the call it interrupts instead of failing the gate;
    * the heartbeat sampled, so the loop was never held for the whole burst;
    * all 500 rows are committed — the work happened, off the loop.

    Wall-clock p99 loop lag is deliberately not asserted here: it measures the host. The
    same commit that measures a p99 of 0.2-4 ms on a quiet machine measured 64.0 ms and
    317.3 ms on the shared CI runner — a burst yields only a handful of heartbeat samples,
    so the "p99" was really a max, and one deschedule failed it. The plan's 50 ms claim is
    reproduced by ``benchmarks/store_burst.py``, which prints the machine it ran on.
    """
    store = Store(tmp_path / "lag.db")
    # One cursor block covers the whole burst: the reservation is a transaction, and the
    # one the writer legitimately makes on the loop (`reserve_cursors`) must not land
    # inside the window this test measures. 1024 > 500 keeps that out of the count
    # whatever the default block size becomes.
    writer = StoreWriter(store, cursor_block=1024)
    await writer.start()
    store.attach_writer(writer)
    lags: list[float] = []
    wall: list[float] = []
    cpu: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        while not done.is_set():
            started = loop.time()
            await asyncio.sleep(0.002)
            lags.append(max(0.0, loop.time() - started - 0.002))

    beat = asyncio.create_task(heartbeat())
    # The loop thread's connection, opened by the cursor reservation in `start()`.
    loop_rows = store.db.total_changes
    try:
        for i in range(500):
            started, cpu_started = time.perf_counter(), time.thread_time()
            assert store.add(inbound(i)) is True
            cpu.append(time.thread_time() - cpu_started)
            wall.append(time.perf_counter() - started)
            # The relay reader yields to the loop between events; this is that yield.
            await asyncio.sleep(0)
        await store.flush()
    finally:
        done.set()
        await beat
        await writer.stop()

    try:
        assert store.stats()["total"] == 500
        assert lags, "the heartbeat never sampled: the loop was held for the whole burst"
        written = store.db.total_changes - loop_rows
        assert written == 0, (
            f"the event-loop thread's connection changed {written} row(s) while the burst "
            "was accepted: the writes belong on the writer's thread")
        # 35-46 ms of loop wall time for 500 events here; the pre-P6 shape, one transaction
        # and one fsync per message, spent 500+ ms. 500 ms leaves a runner room to
        # deschedule one call by hundreds of milliseconds and still pass, and cannot pass a
        # per-message wait being put back on the loop.
        waited = sum(wall)
        assert waited < 0.500, (
            f"the event-loop thread spent {waited * 1000:.0f} ms accepting 500 events "
            f"({waited / 500 * 1000:.3f} ms each; {sum(cpu) * 1000:.0f} ms of it CPU, "
            f"worst single call {max(wall) * 1000:.0f} ms): the accept path is waiting on "
            "work that belongs off the loop. If the CPU figure is near the total the loop "
            "is doing the work itself; if it is tens of milliseconds the host descheduled "
            "the loop, and the wall-clock claim belongs to benchmarks/store_burst.py")
    finally:
        store.close()


# -- durability ---------------------------------------------------------
def test_committed_rows_survive_kill_9(tmp_path):
    """WAL plus `synchronous=NORMAL`: a commit is on disk before it returns."""
    db = tmp_path / "durable.db"
    script = tmp_path / "write_then_die.py"
    script.write_text(textwrap.dedent(f"""
        import os
        import signal
        import sys

        sys.path.insert(0, {str(SRC)!r})
        from hermes_nostr_gateway.store import Inbound, Store

        store = Store({str(db)!r}, identity="me")
        for index in range(50):
            store.add(Inbound(
                id=f"k{{index}}", wrapper_id=f"k{{index}}", sender="aa" * 32, kind=14,
                created_at=1_700_000_000 + index, content=f"kept {{index}}",
                conversation="aa" * 32, reply_to=None, plane="nip17", reach="immediate",
                authority="instructions", rule="t", why="t",
            ))
        print(store.stats()["total"], flush=True)
        os.kill(os.getpid(), signal.SIGKILL)
    """))
    result = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                            timeout=120)

    assert result.returncode == -signal.SIGKILL, result.stderr
    assert result.stdout.strip() == "50"
    with Store(db) as reopened:
        assert reopened.stats()["total"] == 50


def test_a_queued_row_is_not_durable_until_the_flush(tmp_path):
    """The documented window: `offer` answers from memory, the row lands on a flush.

    This is the trade P6 makes, stated as a test rather than a footnote: a `kill -9`
    between the append and the flush loses those rows, which is why the daemon flushes
    before it delivers an immediate frame and drains on a clean stop.
    """
    async def run() -> None:
        store = Store(tmp_path / "window.db")
        writer = StoreWriter(store, tick=60)  # a tick this test will never wait for
        await writer.start()
        store.attach_writer(writer)
        try:
            assert store.add(inbound(1)) is True
            # No flush: a crash here loses the row. Open a second handle to prove it.
            with Store(tmp_path / "window.db", identity="me") as inspector:
                assert inspector.stats()["total"] == 0
            await store.flush()
        finally:
            await writer.stop()
            store.close()

    asyncio.run(run())


# -- lifecycle ----------------------------------------------------------
async def test_stopping_the_writer_commits_what_is_queued(tmp_path):
    store = Store(tmp_path / "stop.db")
    writer = StoreWriter(store, tick=60)
    await writer.start()
    store.attach_writer(writer)
    for i in range(10):
        assert store.add(inbound(i)) is True
    await writer.stop()
    try:
        assert store.stats()["total"] == 10
    finally:
        store.detach_writer()
        store.close()


async def test_a_restarts_replay_is_still_suppressed(tmp_path):
    """A wrapper seen before the restart must not deliver twice after it.

    The wrapper insert is queued, but the *lookup* stays in the database: it is the
    only thing that makes nak's three-day replay harmless across a restart.
    """
    db = tmp_path / "wrappers.db"
    store = Store(db, identity="me")
    writer = StoreWriter(store)
    await writer.start()
    store.attach_writer(writer)
    try:
        assert store.wrapper_seen("wrap-1", "wss://a.test") is False
        assert store.wrapper_seen("wrap-1", "wss://a.test") is True  # same process
        await store.flush()
    finally:
        await writer.stop()
        store.close()

    with Store(db, identity="me") as reopened:
        assert reopened.wrapper_seen("wrap-1", "wss://a.test") is True
        assert reopened.wrapper_seen("wrap-2", "wss://a.test") is False


async def test_cursors_stay_unique_across_reservation_blocks(tmp_path):
    """Cursors come from a reserved block; crossing one must not repeat a number."""
    store = Store(tmp_path / "cursors.db")
    writer = StoreWriter(store, cursor_block=64)
    await writer.start()
    store.attach_writer(writer)
    try:
        for i in range(200):
            assert store.add(inbound(i)) is True
        await store.flush()
    finally:
        await writer.stop()
        store.close()

    with Store(tmp_path / "cursors.db") as reopened:
        cursors = [row["cursor"] for row in reopened.recent(200)]
        assert len(cursors) == 200
        assert len(set(cursors)) == 200, "a reserved cursor was handed out twice"
        assert cursors == sorted(cursors, reverse=True), "cursor order must follow arrival"


@pytest.mark.parametrize("count", [0, 1, 3])
def test_add_many_handles_short_batches(tmp_path, count):
    with Store(tmp_path / f"short-{count}.db") as store:
        results = store.add_many([inbound(i) for i in range(count)])
        assert results == [True] * count
        assert store.stats()["total"] == count
