# Store writes: WAL, batching, and why the loop never waits for the disk

`store.add` used to run its whole transaction inside the relay worker's read callback.
That callback is the event loop, so a burst of events was a burst of stalls: socket
frames waited, heartbeats slipped, digests sealed late, and the other relays' reads sat
behind the same loop. At SQLite's default durability setting (`synchronous=FULL`) each
message also paid an fsync — twice, once for the WAL and once for the directory.

P6 moved that work aside in three steps. Measured on this host (500 events, `--repeat 2`):

```
legacy-paced   events=500   p99=   9.20ms  mean= 4.57ms  total= 879.8ms
writer-paced   events=500   p99=   1.07ms  mean= 0.33ms  total=  46.0ms
legacy-burst   events=500   samples=0  loop held for   986.3ms (never yielded)
writer-burst   events=500   p99=   0.21ms  mean= 0.17ms  total=  20.5ms
```

`legacy` is the pre-P6 shape: `store.add` inline, `synchronous=FULL`. `paced` yields to
the loop between events, the way a relay reader does; `burst` does not, the way a replay
or a relay dump arrives. Reproduce it with:

```sh
python benchmarks/store_burst.py            # 500 events, both paths, both arrivals
python benchmarks/store_burst.py --path writer --arrival paced
```

## What changed

1. **WAL with `synchronous=NORMAL`.** WAL was already on; `synchronous` was not, so every
   commit synced. `NORMAL` in WAL is still crash-safe against `kill -9`: the commit
   returns only after the frame is written to the WAL, and recovery replays it. It is
   *not* power-loss safe — that needs `FULL` and a slower disk path — and the daemon says
   so here rather than implying otherwise.
2. **One transaction per batch.** `Store.add_many` inserts a list of rumors in a single
   `BEGIN IMMEDIATE … COMMIT`, assigning cursors from one read of `delivery_cursor`.
   `Store.wrappers_seen` and `Store.note_relays` batch the same way, and the writer sums
   relay counters per flush instead of upserting once per event.
3. **A dedicated writer thread.** `StoreWriter` (writer.py) holds a deque, and a flusher
   task hands a drained batch to a single-thread executor. The loop only ever pays for an
   append; the transaction runs on the writer's own SQLite connection. WAL is what makes
   that safe: readers on the loop never block on the writer, and `busy_timeout` covers the
   brief moment a checkpoint needs the file.

## What it costs

`add` now answers from memory: True means *accepted*, and the row lands within `tick`
(default 20 ms). Dedupe does not move — the decision is still made against the database
(a primary-key lookup and one indexed range scan, read-only), so a restart's replay of
the last three days is suppressed exactly as before, and two planes carrying the same
text within two seconds are still one message. Only the write is deferred.

Two places close the window between "accepted" and "on disk":

- **An immediate frame waits for its flush before it is broadcast.** The spec's promise
  is that a frame is delivered when it is inserted (§7); `Gateway._deliver` keeps that
  true instead of letting the adapter see a message the store has not committed. The
  wait happens in a task, so the loop stays free.
- **A socket read op flushes first.** `history`, `status`, `media.get` and `ack` all read
  what the daemon accepted, so `Gateway.flush_writes` settles the queue before they
  answer — otherwise an `ack` arriving inside the tick would update nothing and the
  message would replay.

On a clean stop the writer drains what is queued (`Gateway.stop` → `StoreWriter.stop`),
and `kill -9` can lose at most the rows queued since the last flush, which is what
`tests/test_store_writes.py::test_a_queued_row_is_not_durable_until_the_flush` pins down.

## Cursors

A batched writer cannot afford a `delivery_cursor` write per message, and an in-memory
counter alone would race an `hng` command writing to the same file. So the writer reserves
a **block** of cursors in one transaction (`Store.reserve_cursors`, 512 by default) and
hands them out from memory. Cursors stay monotonic and unique; a block reserved but unused
when the process exits is skipped, which is legal — the spec promises a cursor never moves
backwards, not that it never skips. `tests/test_store_writes.py` asserts uniqueness across
a block boundary.

## Configuration

```yaml
store_write:
  tick_ms: 20        # write latency, and the kill -9 exposure, together
  batch_size: 512    # rows per transaction
```

Two knobs, because there are only two decisions: how long a row may sit queued, and how
many rows one commit carries. Shortening `tick_ms` trades throughput for a smaller window;
nothing else is worth exposing.

## Reading the counters

`hng status` carries a `store_write` block: `queued` (rows waiting), `rumors` (rows
committed), `batches`, `duplicates`, `failed`, `dropped`, `last_batch`, `last_flush_ms`.

- `queued` growing without bound means the writer cannot keep up with the disk.
- `last_flush_ms` climbing with a stable batch size is the same signal earlier.
- `failed`/`dropped` non-zero means a batch was given up on. The writer retries once; two
  failures are logged, counted, and the frame is still delivered with a warning, because
  losing a message silently is worse than delivering one the store then has to be asked
  about.
