"""P6: store writes off the event loop — batching, durability, and the burst claim.

The claim this file pins down (plan-v2 §P6): a 500-event burst keeps p99 loop lag under
50 ms, rows committed are enough to survive ``kill -9``, and a burst costs a handful of
transactions rather than one per message. The measurement is also runnable by hand —
``benchmarks/store_burst.py`` — so the number can be reproduced rather than taken on
trust.
"""

from __future__ import annotations

import asyncio
import signal
import subprocess
import sys
import textwrap
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
async def test_a_500_event_burst_keeps_the_loop_responsive(tmp_path):
    """p99 loop lag under 50 ms while 500 events are accepted, as the plan requires."""
    store = Store(tmp_path / "lag.db")
    writer = StoreWriter(store)
    await writer.start()
    store.attach_writer(writer)
    lags: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        while not done.is_set():
            started = loop.time()
            await asyncio.sleep(0.002)
            lags.append(max(0.0, loop.time() - started - 0.002))

    beat = asyncio.create_task(heartbeat())
    try:
        for i in range(500):
            assert store.add(inbound(i)) is True
            # The relay reader yields to the loop between events; this is that yield.
            await asyncio.sleep(0)
        await store.flush()
    finally:
        done.set()
        await beat
        await writer.stop()

    try:
        assert store.stats()["total"] == 500
        assert lags, "the heartbeat never sampled: the loop was blocked"
        p99 = sorted(lags)[min(len(lags) - 1, int(len(lags) * 0.99))]
        assert p99 < 0.050, f"p99 loop lag {p99 * 1000:.1f} ms during a 500-event burst"
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
