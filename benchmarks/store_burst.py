"""A 500-event burst, measured: loop lag with the writer, and with the old path.

The P6 claim is that a burst no longer blocks the event loop. This measures it the only
way that means anything — a heartbeat task sampling its own scheduling delay while
events are accepted — and prints the distribution for both paths so the difference is
visible rather than asserted.

    python benchmarks/store_burst.py                 # 500 events, both paths
    python benchmarks/store_burst.py --events 2000   # a bigger burst
    python benchmarks/store_burst.py --path writer   # just the daemon's path

Nothing here is a gate: `tests/test_store_writes.py` asserts p99 < 50 ms on the writer
path. This script exists so the number can be reproduced on the host that is worrying
you, with the disk that is worrying you.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from hermes_nostr_gateway.store import Inbound, Store  # noqa: E402
from hermes_nostr_gateway.writer import StoreWriter  # noqa: E402

SAMPLE_S = 0.002


def inbound(index: int) -> Inbound:
    return Inbound(
        id=f"r{index:06d}", wrapper_id=f"w{index:06d}", sender="aa" * 32, kind=14,
        created_at=1_700_000_000 + index, content=f"burst {index}", conversation="aa" * 32,
        reply_to=None, plane="nip17", reach="immediate", authority="instructions",
        rule="bench", why="bench",
    )


async def measure(events: int, *, writer_path: bool, burst: bool,
                  db: Path) -> tuple[list[float], float]:
    """Feed `events` messages and return (loop lag samples in ms, wall seconds).

    The legacy path is the pre-P6 shape: `store.add` writes inline on the loop, at
    `synchronous=FULL`, so every message pays its own transaction and its own fsync.

    `burst=True` enqueues the whole batch without yielding, which is what a replay or a
    relay dump looks like arriving at once: the loop is stalled for as long as accepting
    the events takes, in one piece. `burst=False` yields between events, which is the
    steady-state shape of a relay reader.
    """
    store = Store(db, identity="me", synchronous="NORMAL" if writer_path else "FULL")
    writer = StoreWriter(store) if writer_path else None
    if writer is not None:
        await writer.start()
        store.attach_writer(writer)

    lags: list[float] = []
    done = asyncio.Event()

    async def heartbeat() -> None:
        loop = asyncio.get_running_loop()
        while not done.is_set():
            started = loop.time()
            await asyncio.sleep(SAMPLE_S)
            lags.append(max(0.0, (loop.time() - started - SAMPLE_S) * 1000))

    beat = asyncio.create_task(heartbeat())
    started = time.perf_counter()
    try:
        for i in range(events):
            store.add(inbound(i))
            if not burst:
                # The relay reader yields to the loop between events; so does this.
                await asyncio.sleep(0)
        await store.flush()
        elapsed = time.perf_counter() - started
    finally:
        done.set()
        await beat
        if writer is not None:
            await writer.stop()
        total = store.stats()["total"]
        store.close()
    if total != events:
        raise SystemExit(f"stored {total} of {events} events: the burst lost rows")
    return lags, elapsed


def report(label: str, lags: list[float], elapsed: float, events: int) -> None:
    if not lags:
        # The heartbeat never got a turn: the loop was held for the whole burst, which
        # is the worst case the writer exists to remove.
        print(f"{label:<14} events={events:<5} samples=0    "
              f"loop held for {elapsed * 1000:8.1f}ms (no sample: never yielded)")
        return
    ordered = sorted(lags)
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    print(f"{label:<14} events={events:<5} samples={len(lags):<4} "
          f"p50={p50:7.2f}ms p99={p99:8.2f}ms max={ordered[-1]:8.2f}ms "
          f"mean={statistics.fmean(lags):6.2f}ms  total={elapsed * 1000:7.1f}ms")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--path", choices=["writer", "legacy", "both"], default="both")
    parser.add_argument("--arrival", choices=["paced", "burst", "both"], default="both")
    parser.add_argument("--repeat", type=int, default=3)
    args = parser.parse_args()

    arrivals = {"paced": [False], "burst": [True], "both": [False, True]}[args.arrival]
    paths = {"legacy": [False], "writer": [True], "both": [False, True]}[args.path]
    for run in range(args.repeat):
        for burst in arrivals:
            with tempfile.TemporaryDirectory() as tmp:
                for writer_path in paths:
                    lags, elapsed = asyncio.run(measure(
                        args.events, writer_path=writer_path, burst=burst, db=Path(tmp) /
                        f"{'writer' if writer_path else 'legacy'}-{'burst' if burst else 'paced'}.db"))
                    path = "writer" if writer_path else "legacy"
                    arrival = "burst" if burst else "paced"
                    report(f"{path}-{arrival}", lags, elapsed, args.events)
        if run + 1 < args.repeat:
            print("-" * 96)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
