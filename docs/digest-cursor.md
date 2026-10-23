# Digest batching: cursor schema (design only, rev 2)

Status: proposal for review. No code in this PR.

Scope: §17 `reach: digest(interval)` for `nip01` and `nip17`, and the §5 `ack` /
`subscribe(since_frame)` contract. Immediate messages keep their existing id-based ack.

This revision addresses the review on PR `d3bae2f`:

- ack is now **per rule**; a single global ack cursor cannot mark a later rule's item delivered
  while an earlier rule's item is still queued;
- `frame_seq` is a separate monotonic sequence used only for replay;
- `rumors` has no `identity` column, so v1 is explicitly one identity per process and `rumors`
  indexes are identity-free;
- sealing walks windows oldest-first, and `digests` is unique on
  `(identity, rule, window_start)`;
- `top_ids` stores ids only; snippets are joined at frame-build time;
- dropped rows carry a cursor and a batch id, so cursor ranges stay contiguous.

## Goals

1. One digest frame per `(identity, rule)` per window; never an empty digest.
2. At-least-once: a crash between insert and `ack` replays, never loses.
3. Per-rule ack: one rule's slow window cannot mark another rule's items delivered.
4. Cross-rule dedupe: one event is assigned to its highest-priority rule once, durably.
5. Quota-dropped items are counted (`dropped_count`), not silently discarded.
6. Restart-safe windows: a restart cannot open a second digest for the same window.

## Identity and sequencing

v1 runs **one identity per process** (as M1 does today). `rumors` therefore has no `identity`
column and its indexes do not carry one. `digests` and `digest_rules` keep `identity` because
they are the records that would need it if the process later serves more than one account; the
one-identity assumption is stated here rather than implied by an index.

Two sequences exist, deliberately:

- `rumors.cursor` — insertion order for batching and contiguous cursor ranges. Assigned to
  every new rumor, including `dropped` ones. Monotonic in this process.
- `digests.frame_seq` — delivery order for `subscribe(since_frame)` replay. Assigned when a
  digest is sealed. Monotonic per identity. This is *not* an ack target.

`digest_rules.acked_frame` is per rule. A rule acknowledges only its own frames, so a 1m rule
never depends on a 15m rule's open window.

## Schema

```sql
CREATE TABLE delivery_cursor (
    identity   TEXT PRIMARY KEY,
    next_rumor INTEGER NOT NULL DEFAULT 1,
    next_frame INTEGER NOT NULL DEFAULT 1
);

-- Existing table; new columns only.
ALTER TABLE rumors ADD COLUMN cursor       INTEGER;
ALTER TABLE rumors ADD COLUMN digest_rule  TEXT;      -- policy rule name, NULL for immediate
ALTER TABLE rumors ADD COLUMN digest_state TEXT;      -- queued | batched | dropped | NULL
ALTER TABLE rumors ADD COLUMN digest_batch TEXT;      -- digests.id when batched
CREATE UNIQUE INDEX rumors_cursor ON rumors(cursor);  -- v1: one identity per process
CREATE INDEX rumors_digest_queue ON rumors(digest_rule, digest_state, cursor);

CREATE TABLE digest_rules (
    identity      TEXT NOT NULL,
    rule          TEXT NOT NULL,
    interval_s    INTEGER NOT NULL,
    window_start  INTEGER NOT NULL,
    last_batch_at INTEGER,
    acked_frame   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, rule)
);

CREATE TABLE digests (
    id            TEXT PRIMARY KEY,       -- UUID/ULID, also rumors.digest_batch
    identity      TEXT NOT NULL,
    frame_seq     INTEGER NOT NULL,       -- replay cursor, not an ack target
    rule          TEXT NOT NULL,
    plane         TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    cursor_start  INTEGER NOT NULL,       -- first rumor cursor in the window (incl. dropped)
    cursor_end    INTEGER NOT NULL,       -- last rumor cursor in the window
    item_count    INTEGER NOT NULL,
    delivered_count INTEGER NOT NULL,
    dropped_count INTEGER NOT NULL,
    top_ids       TEXT NOT NULL,          -- JSON array of rumor ids, newest first, capped
    created_at    INTEGER NOT NULL,
    acked_at      INTEGER,
    UNIQUE(identity, rule, window_start),
    UNIQUE(identity, frame_seq)
);
CREATE INDEX digests_replay ON digests(identity, frame_seq);
```

`rumors.id` is already the dedupe key; `UNIQUE(identity, rumor_id)` is deliberately not added.
`top_ids` is ids only; the frame builder joins `content`, `sender`, `authority` and `why` at
delivery time, so no plaintext snippet is duplicated in the digest record.

## Write path

1. `Nip17Plane` / `Nip01Plane` call `policy.decide(..., event_id=...)`.
2. `reach == immediate`: insert the rumor with `cursor`, no digest columns; emit immediately.
3. `reach == digest(interval)`: insert with `cursor`, `digest_rule`, `digest_state='queued'`.
   Ensure `digest_rules` row exists (window_start set on first item).
4. `reach == drop`: insert with `cursor`, `digest_rule`, `digest_state='dropped'`. It stays in
   the cursor range for `dropped_count`, but is never in `top_ids` and never causes a frame.
5. Duplicate across rules: `rumors.id` is the primary key, so the first insert wins. If a later
   evaluation assigns a higher-priority rule before the batch seals, update `digest_rule`,
   `authority` and `why`; after sealing, leave the sealed batch untouched.

All cursor assignment happens in the same transaction as `Store.add()`:

```sql
UPDATE delivery_cursor SET next_rumor = next_rumor + 1 WHERE identity = ?;
-- read next_rumor - 1 and use it as rumors.cursor
```

A fresh database has no `delivery_cursor` row, so that `UPDATE` would silently affect zero rows and
leave `next_rumor` at 1. At startup, before the first `Store.add()`:

```sql
INSERT OR IGNORE INTO delivery_cursor(identity) VALUES (?);
```

The migration backfills `cursor` for **every** existing rumor — immediate ones included — in
`received_at, id` order, then creates `UNIQUE INDEX rumors_cursor`. After backfill, a NULL cursor
is not a valid state; the insert path always assigns one.

## Window lifecycle

- `window_start = floor(now / interval_s) * interval_s`; `window_end = window_start + interval_s`.
- Sealing iterates rules **oldest first**:
  `SELECT * FROM digest_rules WHERE window_start + interval_s <= now ORDER BY window_start ASC`.
  A late seal for an older window is processed before a newer one; it never jumps to the newest
  window and files old rows under a new digest.
- For each due rule:
  1. select that rule's rows in this window, including `dropped` rows, ordered by `cursor`;
  2. `UPDATE ... SET digest_state='batched', digest_batch=?` for queued rows, and
     `UPDATE ... SET digest_batch=?` for dropped rows in the same window (their state stays
     `dropped`);
  3. allocate `frame_seq` from `delivery_cursor.next_frame`;
  4. insert `digests` with `cursor_start`/`cursor_end`, `item_count`, `delivered_count`,
     `dropped_count`, `top_ids`;
  5. set `digest_rules.window_start = window_end`, `last_batch_at = now`.
- Empty or dropped-only window: advance `window_start`, do not insert a digest.
- Restart: `subscribe(since_frame)` replays sealed, unacked digests by `frame_seq`; open windows
  resume from `digest_rules.window_start`.

## Ack

Digest frame:

```json
{"op": "ack", "rule": "unknown-dms", "frame": 42}
```

- `frame` is `digests.frame_seq`; it is deliberately not named `cursor`, which means
  `rumors.cursor` everywhere else.
- `UPDATE digest_rules SET acked_frame = MAX(acked_frame, ?) WHERE identity=? AND rule=?`.
- Mark `rumors.delivered_at` for every rumor in that `digests` row.
- `UPDATE digests SET acked_at = ... WHERE identity=? AND rule=? AND frame_seq<=?`.

Immediate frames keep `{"op":"ack","ids":["…"]}`; that path does not touch
`digest_rules.acked_frame`. `ack(ids)` remains the compatibility shim for old adapters.

## Frame

```json
{
  "type": "digest",
  "frame": 42,
  "identity": "npub1…",
  "rule": "unknown-dms",
  "plane": "nip17",
  "window_start": 1790000000,
  "window_end": 1790003600,
  "count": 12,
  "delivered": 5,
  "dropped": 7,
  "items": [
    {"rumor_id": "…", "from": "npub1…", "created_at": 1790000123,
     "authority": "none", "why": "matched unknown-dms (group unknown)",
     "snippet": "first 256 bytes…"}
  ],
  "truncated": true
}
```

`delivered` is `len(items)` capped at `top_n` (default 5); `count` is `item_count`;
`truncated` means `delivered < count`. `dropped` is `dropped_count`.

## Decisions confirmed

1. Floor-aligned windows — yes.
2. `frame_seq` for replay, per-rule `acked_frame` for ack, `ack(ids)` shim — yes.
3. Top-N by `created_at`, each item carrying `authority` — yes.
4. Dropped rows retained 7 days, purged in the existing housekeeping job — yes.
5. One identity per process in v1; `rumors` indexes carry no identity — yes.

## Non-goals

- No digest for `reach: immediate`.
- No change to `policy.decide()`; it already returns `interval_s`.
- No multi-identity `rumors` schema in v1.
