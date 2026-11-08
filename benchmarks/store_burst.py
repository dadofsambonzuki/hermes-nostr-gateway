"""A 500-event burst, measured on the machine you run it on: loop lag, and loop CPU.

The P6 claim is that a burst no longer blocks the event loop. This measures it the only
way that means anything — a heartbeat task sampling its own scheduling delay while
events are accepted — and prints the host in the banner, so a pasted number carries the
machine it came from. The pre-P6 path is measured beside it, so the difference is visible
rather than asserted.

    python benchmarks/store_burst.py                   # 500 events, both paths
    python benchmarks/store_burst.py --events 2000     # a bigger burst
    python benchmarks/store_burst.py --path writer     # just the daemon's path
    python benchmarks/store_burst.py --max-p99-ms 50   # hold the plan's claim here

The plan's acceptance (plan-v2 §P6) is a p99 loop lag under 50 ms while a 500-event burst
is accepted. That number is a property of the host as much as of the code, so it belongs
here and not in the CI gate: the same commit measured a p99 of 0.2-1 ms on the quiet host
`docs/store-writes.md` quotes, and 64.0 ms and 317.3 ms on the shared CI runner, where
the runner deschedules the whole process while the heartbeat sleeps. Pass
`--max-p99-ms 50` on a machine you have named to hold the claim to it; the CI gate
(`tests/test_store_writes.py`) asserts the mechanism instead — no row written by the
event-loop thread, and bounded loop-thread CPU — because neither of those can be
faked by a busy runner.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import platform
import socket
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


def banner() -> str:
    """Who ran this: a p99 without a machine is not a measurement."""
    load = ", ".join(f"{value:.2f}" for value in os.getloadavg())
    return (f"host={socket.gethostname()} platform={platform.platform()} "
            f"cpu={os.cpu_count()} load={load}")


async def measure(events: int, *, writer_path: bool, burst: bool,
                  db: Path) -> tuple[list[float], float, float, float]:
    """Feed `events` messages and return their loop lag samples (ms), the wall seconds
    they took, the total CPU milliseconds the loop thread spent in `store.add`, and the
    worst single call.

    The legacy path is the pre-P6 shape: `store.add` writes inline on the loop, at
    `synchronous=FULL`, so every message pays its own transaction and its own fsync.

    `burst=True` enqueues the whole batch without yielding, which is what a replay or a
    relay dump looks like arriving at once: the loop is stalled for as long as accepting
    the events takes, in one piece. `burst=False` yields between events, which is the
    steady-state shape of a relay reader.

    The CPU figures are `time.thread_time()` deltas around each `store.add`, so they
    count the work the loop thread actually did and stop whenever the process is
    descheduled: unlike the lag samples, they compare across hosts.
    """
    store = Store(db, identity="me", synchronous="NORMAL" if writer_path else "FULL")
    writer = StoreWriter(store) if writer_path else None
    if writer is not None:
        await writer.start()
        store.attach_writer(writer)

    lags: list[float] = []
    cpu: list[float] = []
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
            before = time.thread_time()
            store.add(inbound(i))
            cpu.append((time.thread_time() - before) * 1000)
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
    return lags, elapsed, sum(cpu), max(cpu, default=0.0)


def report(label: str, lags: list[float], elapsed: float, events: int,
           cpu_total: float, cpu_max: float) -> float | None:
    """Print one run; return its p99 lag in ms, or None when the heartbeat never ran."""
    if not lags:
        # The heartbeat never got a turn: the loop was held for the whole burst, which
        # is the worst case the writer exists to remove.
        print(f"{label:<14} events={events:<5} samples=0    "
              f"loop held for {elapsed * 1000:8.1f}ms (no sample: never yielded)  "
              f"loop_cpu={cpu_total:7.1f}ms max={cpu_max:5.2f}ms")
        return None
    ordered = sorted(lags)
    p50 = ordered[len(ordered) // 2]
    p99 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.99))]
    print(f"{label:<14} events={events:<5} samples={len(lags):<4} "
          f"p50={p50:7.2f}ms p99={p99:8.2f}ms max={ordered[-1]:8.2f}ms "
          f"mean={statistics.fmean(lags):6.2f}ms  total={elapsed * 1000:7.1f}ms  "
          f"loop_cpu={cpu_total:7.1f}ms max={cpu_max:5.2f}ms")
    return p99


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=500)
    parser.add_argument("--path", choices=["writer", "legacy", "both"], default="both")
    parser.add_argument("--arrival", choices=["paced", "burst", "both"], default="both")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument(
        "--max-p99-ms", type=float, default=None,
        help="fail (exit 1) when the writer path's p99 lag exceeds this, in ms; the "
             "plan's number is 50, and it is a statement about one named machine")
    args = parser.parse_args()

    print(banner())
    arrivals = {"paced": [False], "burst": [True], "both": [False, True]}[args.arrival]
    paths = {"legacy": [False], "writer": [True], "both": [False, True]}[args.path]
    writer_p99: list[tuple[str, float | None]] = []
    for run in range(args.repeat):
        for burst in arrivals:
            with tempfile.TemporaryDirectory() as tmp:
                for writer_path in paths:
                    lags, elapsed, cpu_total, cpu_max = asyncio.run(measure(
                        args.events, writer_path=writer_path, burst=burst, db=Path(tmp) /
                        f"{'writer' if writer_path else 'legacy'}-{'burst' if burst else 'paced'}.db"))
                    path = "writer" if writer_path else "legacy"
                    arrival = "burst" if burst else "paced"
                    label = f"{path}-{arrival}"
                    p99 = report(label, lags, elapsed, args.events, cpu_total, cpu_max)
                    if writer_path:
                        writer_p99.append((label, p99))
        if run + 1 < args.repeat:
            print("-" * 96)

    if args.max_p99_ms is not None:
        over = [item for item in writer_p99
                if item[1] is None or item[1] > args.max_p99_ms]
        for label, p99 in over:
            state = ("no sample: the loop was held for the whole burst" if p99 is None
                     else f"p99 {p99:.1f} ms")
            print(f"FAIL {label}: {state} > {args.max_p99_ms:g} ms on {socket.gethostname()}")
        if over:
            return 1
        print(f"p99 < {args.max_p99_ms:g} ms held on {socket.gethostname()} "
              f"({len(writer_p99)} writer-path runs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
