"""Durable state: what we have seen, what we have delivered, and why.

Dedupe keys on the *rumor* id, not the wrapper: the same message arrives once per relay
with a different wrapper id each time, so wrapper-level dedupe misses duplicates across
relays (spec §14).

v1 is one identity per process. ``rumors`` therefore carries no identity column; the
``delivery_cursor`` row is keyed by the local npub so digest tables remain ready for a
multi-identity daemon later. See ``docs/digest-cursor.md``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS rumors (
    id            TEXT PRIMARY KEY,
    wrapper_id    TEXT NOT NULL,
    sender        TEXT NOT NULL,
    kind          INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    conversation  TEXT,
    content       TEXT,
    reply_to      TEXT,
    plane         TEXT NOT NULL DEFAULT 'nip17',
    reach         TEXT,
    authority     TEXT,
    rule          TEXT,
    why           TEXT,
    received_at   INTEGER NOT NULL,
    delivered_at  INTEGER,
    blocked_reason TEXT
);
CREATE INDEX IF NOT EXISTS rumors_pending ON rumors (delivered_at, received_at);
CREATE INDEX IF NOT EXISTS rumors_sender ON rumors (sender, created_at);

CREATE TABLE IF NOT EXISTS wrappers (
    id         TEXT PRIMARY KEY,
    relay      TEXT,
    seen_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_wraps (
    id          TEXT NOT NULL,
    relay       TEXT NOT NULL,
    event       TEXT NOT NULL,
    rumor_id    TEXT NOT NULL,
    recipient   TEXT NOT NULL,
    -- When the wrap was first offered to this relay. The retry TTL is measured from
    -- here, not from `last_attempt`: a row that keeps failing must not renew its own
    -- lease and churn for ever.
    created_at  INTEGER NOT NULL DEFAULT 0,
    status      TEXT NOT NULL DEFAULT 'pending',
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    last_attempt INTEGER,
    PRIMARY KEY (id, relay)
);
CREATE INDEX IF NOT EXISTS wraps_pending ON outbound_wraps (status);

CREATE TABLE IF NOT EXISTS relay_stats (
    relay      TEXT PRIMARY KEY,
    events     INTEGER NOT NULL DEFAULT 0,
    last_event INTEGER,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS concord_invites (
    id           TEXT PRIMARY KEY,
    inviter      TEXT NOT NULL,
    community_id TEXT NOT NULL,
    name         TEXT,
    relays       TEXT,
    bundle       TEXT NOT NULL,
    received_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS attachments (
    rumor_id   TEXT NOT NULL,
    position   INTEGER NOT NULL,
    sha256     TEXT,
    url        TEXT,
    mime       TEXT,
    size       INTEGER,
    dim        TEXT,
    alt        TEXT,
    local_path TEXT,
    fetched_at INTEGER,
    PRIMARY KEY (rumor_id, position)
);
CREATE INDEX IF NOT EXISTS attachments_blob ON attachments (sha256);
CREATE INDEX IF NOT EXISTS attachments_url ON attachments (url);

CREATE TABLE IF NOT EXISTS delivery_cursor (
    identity   TEXT PRIMARY KEY,
    next_rumor INTEGER NOT NULL DEFAULT 1,
    next_frame INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS digest_rules (
    identity      TEXT NOT NULL,
    rule          TEXT NOT NULL,
    interval_s    INTEGER NOT NULL,
    window_start  INTEGER NOT NULL,
    last_batch_at INTEGER,
    acked_frame   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (identity, rule)
);

CREATE TABLE IF NOT EXISTS digests (
    id            TEXT PRIMARY KEY,
    identity      TEXT NOT NULL,
    frame_seq     INTEGER NOT NULL,
    rule          TEXT NOT NULL,
    plane         TEXT NOT NULL,
    window_start  INTEGER NOT NULL,
    window_end    INTEGER NOT NULL,
    cursor_start  INTEGER NOT NULL,
    cursor_end    INTEGER NOT NULL,
    item_count    INTEGER NOT NULL,
    delivered_count INTEGER NOT NULL,
    dropped_count INTEGER NOT NULL,
    top_ids       TEXT NOT NULL DEFAULT '[]',
    created_at    INTEGER NOT NULL,
    acked_at      INTEGER,
    UNIQUE(identity, rule, window_start),
    UNIQUE(identity, frame_seq)
);
CREATE INDEX IF NOT EXISTS digests_replay ON digests (identity, frame_seq);
"""

_RUMOR_COLUMNS = {
    "cursor": "INTEGER",
    "digest_rule": "TEXT",
    "digest_state": "TEXT",
    "digest_batch": "TEXT",
}

#: How long a drop stays "live" in `drops` and in the status count. Older rows are still
#: stored and still listed — marked `expired` — because a refusal is evidence: deleting it
#: would hide that a peer is being turned away, while never ageing it makes last month's
#: spam wave look like a live problem.
DEFAULT_DROPS_TTL_HOURS = 168


def drops_ttl_seconds(cfg: dict[str, Any] | None = None) -> int:
    """`drops.ttl_hours` as seconds — one parser for the daemon and the CLI."""
    hours = float((cfg or {}).get("ttl_hours", DEFAULT_DROPS_TTL_HOURS))
    if hours <= 0:
        raise ValueError("drops.ttl_hours must be positive")
    return int(hours * 3600)


@dataclass
class Inbound:
    id: str
    wrapper_id: str
    sender: str
    kind: int
    created_at: int
    content: str
    conversation: str
    reply_to: str | None
    plane: str = "nip17"
    reach: str = "digest"
    authority: str = "none"
    rule: str = ""
    why: str = ""
    blocked_reason: str | None = None
    interval_s: int | None = None
    cursor: int | None = None
    digest_rule: str | None = None
    digest_state: str | None = None
    digest_batch: str | None = None
    #: NIP-92 `imeta` metadata, in tag order. Claims only: the hash gates the fetch.
    attachments: list[dict[str, Any]] | None = None

    def to_frame(self) -> dict[str, Any]:
        frame = {
            "type": "message",
            "plane": self.plane,
            "id": self.id,
            "sender": self.sender,
            "conversation": self.conversation,
            "kind": self.kind,
            "created_at": self.created_at,
            "content": self.content,
            "reply_to": self.reply_to,
            "reach": self.reach,
            "authority": self.authority,
            "why": self.why,
        }
        if self.attachments:
            frame["attachments"] = self.attachments
        if self.blocked_reason:
            frame["blocked_reason"] = self.blocked_reason
        if self.cursor is not None:
            frame["cursor"] = self.cursor
        return frame


class Store:
    def __init__(self, path: str | Path = "gateway.db", *, identity: str = ""):
        self.path = str(path)
        # A store that did not exist a moment ago owns no undelivered history: any
        # pending rows it is given were answered by whoever held the messages before
        # the import copy. Start the replay floor at now for that case only — a normal
        # restart of an existing store must still replay a message that crashed
        # mid-delivery.
        fresh = not Path(self.path).exists()
        self.identity = identity
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()
        if fresh:
            self.set_replay_floor()

    # -- schema migration -----------------------------------------------
    def _migrate(self) -> None:
        cols = {row["name"] for row in self.db.execute("PRAGMA table_info(rumors)")}
        if "blocked_reason" not in cols:
            self.db.execute("ALTER TABLE rumors ADD COLUMN blocked_reason TEXT")
        for name, kind in _RUMOR_COLUMNS.items():
            if name not in cols:
                self.db.execute(f"ALTER TABLE rumors ADD COLUMN {name} {kind}")

        wrap_cols = {row["name"] for row in self.db.execute("PRAGMA table_info(outbound_wraps)")}
        if "created_at" not in wrap_cols:
            self.db.execute(
                "ALTER TABLE outbound_wraps ADD COLUMN created_at INTEGER NOT NULL DEFAULT 0"
            )
        # A row from before the TTL has no creation time. Its last attempt is the
        # closest age we have for a failed row; a row never attempted gets `now`, which
        # keeps it retryable rather than expiring history we cannot date.
        self.db.execute(
            "UPDATE outbound_wraps SET created_at = COALESCE(last_attempt, ?)"
            " WHERE created_at = 0",
            (int(time.time()),),
        )

        self.db.execute(
            "INSERT OR IGNORE INTO delivery_cursor (identity) VALUES (?)", (self.identity,)
        )
        row = self.db.execute(
            "SELECT next_rumor, next_frame FROM delivery_cursor WHERE identity = ?",
            (self.identity,),
        ).fetchone()
        max_row = self.db.execute(
            "SELECT COALESCE(MAX(cursor), 0) AS m FROM rumors"
        ).fetchone()
        next_rumor = max(int(row["next_rumor"]), int(max_row["m"]) + 1)

        rows = self.db.execute(
            "SELECT id FROM rumors WHERE cursor IS NULL ORDER BY received_at, id"
        ).fetchall()
        for item in rows:
            self.db.execute(
                "UPDATE rumors SET cursor = ? WHERE id = ?", (next_rumor, item["id"])
            )
            next_rumor += 1
        if rows or int(row["next_rumor"]) != next_rumor:
            self.db.execute(
                "UPDATE delivery_cursor SET next_rumor = ? WHERE identity = ?",
                (next_rumor, self.identity),
            )
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS rumors_cursor ON rumors (cursor)")
        # `drops` filters a small subset of the table. The index has to be created here,
        # after the ALTERs: an index whose predicate names a column a pre-`blocked_reason`
        # database does not have yet would make opening that database fail outright.
        self.db.execute(
            "CREATE INDEX IF NOT EXISTS rumors_drops ON rumors (received_at)"
            " WHERE reach = 'drop' OR blocked_reason IS NOT NULL"
        )

    def _ensure_cursor(self) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO delivery_cursor (identity) VALUES (?)", (self.identity,)
        )

    # -- wrappers (transport level) -------------------------------------
    def wrapper_seen(self, wrapper_id: str, relay: str = "") -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO wrappers (id, relay, seen_at) VALUES (?, ?, ?)",
            (wrapper_id, relay, int(time.time())),
        )
        return cur.rowcount == 0

    def note_relay(self, relay: str, *, events: int = 0, error: str | None = None) -> None:
        self.db.execute(
            "INSERT INTO relay_stats (relay, events, last_event, last_error) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(relay) DO UPDATE SET "
            "events = events + excluded.events, "
            "last_event = COALESCE(excluded.last_event, last_event), "
            "last_error = COALESCE(excluded.last_error, last_error)",
            (relay, events, int(time.time()) if events else None, error),
        )

    def relay_report(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM relay_stats ORDER BY relay").fetchall()
        return [dict(r) for r in rows]

    # -- outbound wraps (retry uses the same signed event) ---------------
    def record_wrap(self, event: dict, rumor_id: str, recipient: str, relays: list[str],
                    *, now: int | None = None) -> None:
        stamp = int(time.time()) if now is None else int(now)
        for relay in relays:
            self.db.execute(
                "INSERT INTO outbound_wraps (id, relay, event, rumor_id, recipient, created_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(id, relay) DO NOTHING",
                (event["id"], relay, json.dumps(event), rumor_id, recipient, stamp),
            )

    def mark_wrap(self, wrap_id: str, relay: str, *, ok: bool, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE outbound_wraps SET status = ?, attempts = attempts + 1, last_attempt = ?,"
            " last_error = ? WHERE id = ? AND relay = ?",
            ("sent" if ok else "failed", int(time.time()), error, wrap_id, relay),
        )

    def expire_wraps(self, ttl_seconds: int, *, now: int | None = None) -> int:
        """Mark retryable wraps older than the TTL `expired`.

        The attempt cap alone does not bound a backlog: it only helps a row that gets a
        turn, and five rows a pass is days of churn for thousands of dead-relay rows.
        Age is measured from the first attempt, so retrying a row cannot renew it. Sent
        rows are already done and are left alone.
        """
        cutoff = (int(time.time()) if now is None else int(now)) - int(ttl_seconds)
        cur = self.db.execute(
            "UPDATE outbound_wraps SET status = 'expired',"
            " last_error = COALESCE(last_error, 'retry TTL exceeded')"
            " WHERE status IN ('pending', 'failed') AND created_at < ?",
            (cutoff,),
        )
        return cur.rowcount

    def wrap_counts(self) -> dict[str, int]:
        """Outbound-wrap rows per status, so `status` can show what was given up on."""
        rows = self.db.execute(
            "SELECT status, COUNT(*) AS n FROM outbound_wraps GROUP BY status"
        ).fetchall()
        return {str(row["status"]): int(row["n"]) for row in rows}

    def pending_wraps(self, max_attempts: int = 6) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM outbound_wraps WHERE status IN ('pending', 'failed')"
            " AND attempts < ? ORDER BY attempts, last_attempt IS NULL DESC",
            (max_attempts,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- rumors (application level) -------------------------------------
    def add(self, msg: Inbound, *, now: int | None = None) -> bool:
        """Insert a rumor and assign its cursor in one transaction.

        A sender may write the same text over NIP-17 and NIP-04; ids differ per plane,
        so also dedupe on (sender, created_at, content). A duplicate rolls the cursor
        allocation back, so the sequence stays contiguous.
        """
        stamp = int(time.time()) if now is None else int(now)
        digest_rule = msg.rule if msg.reach in ("digest", "drop") and msg.rule else None
        digest_state = None
        if msg.reach == "digest":
            digest_state = "queued"
        elif msg.reach == "drop" and msg.interval_s is not None:
            digest_state = "dropped"

        self.db.execute("BEGIN IMMEDIATE")
        try:
            if msg.authority != "self":
                row = self.db.execute(
                    "SELECT id FROM rumors WHERE sender = ? AND created_at BETWEEN ? AND ? "
                    "AND content = ? LIMIT 1",
                    (msg.sender, msg.created_at - 2, msg.created_at + 2, msg.content),
                ).fetchone()
                if row is not None:
                    self.db.execute("ROLLBACK")
                    return False
            self._ensure_cursor()
            row = self.db.execute(
                "SELECT next_rumor FROM delivery_cursor WHERE identity = ?",
                (self.identity,),
            ).fetchone()
            cursor = int(row["next_rumor"])
            self.db.execute(
                "INSERT INTO rumors (id, wrapper_id, sender, kind, created_at, conversation,"
                " content, reply_to, plane, reach, authority, rule, why, received_at,"
                " cursor, digest_rule, digest_state, digest_batch, blocked_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.id, msg.wrapper_id, msg.sender, msg.kind, msg.created_at,
                    msg.conversation, msg.content, msg.reply_to, msg.plane,
                    msg.reach, msg.authority, msg.rule, msg.why, stamp,
                    cursor, digest_rule, digest_state, None, msg.blocked_reason,
                ),
            )
            self.db.execute(
                "UPDATE delivery_cursor SET next_rumor = ? WHERE identity = ?",
                (cursor + 1, self.identity),
            )
            if digest_rule and (msg.reach == "digest" or digest_state == "dropped"):
                self._ensure_digest_rule(digest_rule, int(msg.interval_s or 900), stamp)
            # Attachments are part of the message, so they share its transaction: a
            # message that survives without its attachment metadata would show the agent
            # a photo it can never ask for.
            for position, entry in enumerate(msg.attachments or []):
                self.db.execute(
                    "INSERT INTO attachments (rumor_id, position, sha256, url, mime,"
                    " size, dim, alt) VALUES (?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(rumor_id, position) DO NOTHING",
                    (
                        msg.id, position,
                        (entry.get("sha256") or None), entry.get("url"),
                        entry.get("mime"), entry.get("size"), entry.get("dim"),
                        entry.get("alt"),
                    ),
                )
            self.db.execute("COMMIT")
        except sqlite3.IntegrityError:
            self.db.execute("ROLLBACK")
            return False
        except Exception:
            self.db.execute("ROLLBACK")
            raise

        msg.cursor = cursor
        msg.digest_rule = digest_rule
        msg.digest_state = digest_state
        return True

    def _ensure_digest_rule(self, rule: str, interval_s: int, now: int) -> None:
        interval_s = max(1, int(interval_s))
        window_start = (now // interval_s) * interval_s
        self.db.execute(
            "INSERT INTO digest_rules (identity, rule, interval_s, window_start)"
            " VALUES (?,?,?,?) ON CONFLICT(identity, rule) DO UPDATE SET"
            " interval_s = excluded.interval_s",
            (self.identity, rule, interval_s, window_start),
        )

    def mark_delivered(self, ids: list[str]) -> None:
        """Mark individual rumors delivered, and whole digest batches when the id is one.

        A legacy adapter knows only ``type: message`` and acks ``id``; for a digest frame
        that id is ``digests.id``, so the batch is the unit of delivery. The batch row is
        closed only once every ``batched`` rumor in it is delivered — a partial id-level
        ack leaves it open, so replay can still offer what is left. A second ack is a
        no-op: ``COALESCE`` keeps the first timestamp.
        """
        ids = [i for i in ids if i]
        if not ids:
            return
        now = int(time.time())
        whole: set[str] = set()
        touched: set[str] = set()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany(
                "UPDATE rumors SET delivered_at = COALESCE(delivered_at, ?) WHERE id = ?",
                [(now, i) for i in ids],
            )
            for start in range(0, len(ids), 200):
                chunk = ids[start:start + 200]
                placeholders = ",".join("?" * len(chunk))
                whole.update(
                    str(row["id"]) for row in self.db.execute(
                        f"SELECT id FROM digests WHERE id IN ({placeholders})", chunk
                    ).fetchall()
                )
                touched.update(
                    str(row["digest_batch"]) for row in self.db.execute(
                        f"SELECT DISTINCT digest_batch FROM rumors WHERE id IN ({placeholders})"
                        " AND digest_batch IS NOT NULL", chunk
                    ).fetchall()
                )
            for batch_id in whole | touched:
                if batch_id in whole:
                    # The frame id is the batch: one ack covers every batched rumor in it.
                    self.db.execute(
                        "UPDATE rumors SET delivered_at = COALESCE(delivered_at, ?)"
                        " WHERE digest_batch = ? AND digest_state = 'batched'",
                        (now, batch_id),
                    )
                remaining = self.db.execute(
                    "SELECT COUNT(*) AS n FROM rumors WHERE digest_batch = ?"
                    " AND digest_state = 'batched' AND delivered_at IS NULL",
                    (batch_id,),
                ).fetchone()["n"]
                if remaining == 0:
                    self.db.execute(
                        "UPDATE digests SET acked_at = COALESCE(acked_at, ?) WHERE id = ?",
                        (now, batch_id),
                    )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND blocked_reason IS NULL"
            " ORDER BY received_at, rowid LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_immediate(self, limit: int = 20, max_age_minutes: int = 120) -> list[dict[str, Any]]:
        """Messages that never reached an adapter (refused, or crashed mid-delivery).

        Replayed on subscribe so a reconnect cannot lose a message that was accepted
        but never handed over. Two bounds stop that being a history lesson: the window
        is minutes (a reconnect or an adapter restart), not days, and nothing older
        than the store's replay floor is offered, so a handover or an import cannot
        resurrect messages the previous adapter already answered. Ordering is by
        arrival and then insertion: `received_at` has second resolution, and a replay
        has to keep the order the frames first appeared in or a cursor-taking consumer
        would see it jump backwards.
        """
        cutoff = int(time.time()) - max(int(max_age_minutes), 0) * 60
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND reach = 'immediate'"
            " AND authority != 'self' AND blocked_reason IS NULL"
            " AND received_at >= ? AND received_at >= ?"
            " ORDER BY received_at, rowid LIMIT ?",
            (cutoff, self.replay_floor(), limit),
        ).fetchall()
        items = [dict(r) for r in rows]
        self._with_attachments(items)
        return items

    def skip_pending(self) -> int:
        """Declare the current backlog already answered, at a handover or an import.

        The rows were delivered by whoever ran before this store, so marking them
        delivered records a fact rather than discarding work. Moving the floor with it
        keeps later arrivals (which are genuinely undelivered) replayable.
        """
        now = int(time.time())
        cur = self.db.execute(
            "UPDATE rumors SET delivered_at = ? WHERE delivered_at IS NULL"
            " AND reach = 'immediate' AND authority != 'self' AND blocked_reason IS NULL",
            (now,),
        )
        self.set_replay_floor(now)
        return cur.rowcount

    # -- attachments (NIP-92 imeta, fetched lazily) ----------------------
    def attachment_rows(self, rumor_id: str) -> list[dict[str, Any]]:
        """Raw rows for one message, tag order included (media resolution needs it)."""
        rows = self.db.execute(
            "SELECT * FROM attachments WHERE rumor_id = ? ORDER BY position",
            (rumor_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def attachments_for(self, rumor_id: str) -> list[dict[str, Any]]:
        """Frame-shaped attachment metadata for a message, in tag order.

        Identical to what the live frame carried: the sender's claims, with the digest
        that lets the adapter ask for the blob. `status` says whether it can be asked
        for at all — `unverifiable` when the tag carried no usable sha256 — and
        `cached` whether the bytes are already on this host.
        """
        out: list[dict[str, Any]] = []
        for row in self.attachment_rows(rumor_id):
            item: dict[str, Any] = {}
            for key in ("url", "sha256", "mime", "size", "dim", "alt"):
                if row.get(key) is not None:
                    item[key] = row[key]
            item["status"] = "available" if row.get("sha256") else "unverifiable"
            if row.get("local_path"):
                item["cached"] = True
            out.append(item)
        return out

    def find_attachment(self, *, sha256: str | None = None,
                        url: str | None = None) -> dict[str, Any] | None:
        """The first row for a blob digest or url, or None when no message holds it."""
        clauses: list[str] = []
        params: list[Any] = []
        if sha256:
            clauses.append("sha256 = ?")
            params.append(sha256.lower())
        if url:
            clauses.append("url = ?")
            params.append(url)
        if not clauses:
            return None
        row = self.db.execute(
            "SELECT * FROM attachments WHERE " + " AND ".join(clauses)
            + " ORDER BY position LIMIT 1",
            params,
        ).fetchone()
        return dict(row) if row is not None else None

    def mark_media_fetched(self, sha256: str, local_path: str) -> int:
        """Record the cache file against every message that referenced the blob."""
        cur = self.db.execute(
            "UPDATE attachments SET local_path = ?, fetched_at = ? WHERE sha256 = ?",
            (local_path, int(time.time()), sha256.lower()),
        )
        return cur.rowcount

    def _with_attachments(self, items: list[dict[str, Any]]) -> None:
        """Attach `attachments` to rumor rows in place, one query for the batch."""
        ids = [str(item["id"]) for item in items if item.get("id")]
        if not ids:
            return
        found: dict[str, list[dict[str, Any]]] = {}
        for start in range(0, len(ids), 200):
            chunk = ids[start:start + 200]
            placeholders = ",".join("?" * len(chunk))
            for row in self.db.execute(
                f"SELECT DISTINCT rumor_id FROM attachments WHERE rumor_id IN ({placeholders})",
                chunk,
            ).fetchall():
                found[str(row["rumor_id"])] = self.attachments_for(str(row["rumor_id"]))
        for item in items:
            if str(item.get("id")) in found:
                item["attachments"] = found[str(item["id"])]

    # -- replay watermark ------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )

    def replay_floor(self) -> int:
        """`received_at` below which nothing is ever replayed (0 = no floor)."""
        try:
            return int(self.get_meta("replay_floor") or 0)
        except (TypeError, ValueError):
            return 0

    def set_replay_floor(self, when: int | None = None) -> int:
        moment = int(time.time()) if when is None else int(when)
        self.set_meta("replay_floor", str(moment))
        return moment

    def recent(self, limit: int = 20, *, with_attachments: bool = False) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM rumors ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        items = [dict(r) for r in rows]
        if with_attachments:
            self._with_attachments(items)
        return items

    # -- drops (refused messages) ---------------------------------------
    #: Why a row was refused, in the same words the `blocked` frame uses. A rule that
    #: says `reach: drop` stores no reason, so it reads as `not_allowlisted`; a row cut
    #: from a digest by its quota says so rather than borrowing that name.
    _REASON_SQL = (
        "CASE WHEN blocked_reason IS NOT NULL THEN blocked_reason"
        " WHEN digest_state = 'dropped' THEN 'digest_quota'"
        " ELSE 'not_allowlisted' END"
    )
    #: A drop is a message policy decided not to deliver. Both shapes count: an explicit
    #: `reach: drop` (which leaves `blocked_reason` null) and a row carrying a reason.
    _DROP_SQL = "(reach = 'drop' OR blocked_reason IS NOT NULL)"

    def _drop_filters(self, *, plane: str | None, reason: str | None,
                      since: int | None) -> tuple[str, list[Any]]:
        clauses = [self._DROP_SQL]
        params: list[Any] = []
        if plane:
            clauses.append("plane = ?")
            params.append(str(plane))
        if reason:
            clauses.append(f"{self._REASON_SQL} = ?")
            params.append(str(reason))
        if since is not None:
            clauses.append("received_at >= ?")
            params.append(int(since))
        return " AND ".join(clauses), params

    def drops(self, *, plane: str | None = None, reason: str | None = None,
              since: int | None = None, limit: int = 100, ttl_s: int | None = None,
              now: int | None = None) -> dict[str, Any]:
        """Refused messages, newest first, with why and when — the `drops` op's answer.

        `expired` marks a row older than `ttl_s`: retained, because the record of a
        refusal is evidence, but distinguishable from something refused this week.
        `total`/`live`/`expired` count the whole matching set; `drops` is one page of it.
        No `content` is selected: a refusal is metadata, and a blocked frame must never
        carry chat text (§13.3) — the same rule applies to the listing.
        """
        stamp = int(time.time()) if now is None else int(now)
        ttl = DEFAULT_DROPS_TTL_HOURS * 3600 if ttl_s is None else int(ttl_s)
        where, params = self._drop_filters(plane=plane, reason=reason, since=since)
        counts = self.db.execute(
            f"SELECT COUNT(*) AS total, SUM(received_at < ?) AS expired FROM rumors"
            f" WHERE {where}",
            [stamp - ttl, *params],
        ).fetchone()
        total = int(counts["total"] or 0)
        expired = int(counts["expired"] or 0)
        rows = self.db.execute(
            f"SELECT id, plane, sender, kind, rule, why, received_at, created_at,"
            f" {self._REASON_SQL} AS reason FROM rumors WHERE {where}"
            f" ORDER BY received_at DESC, rowid DESC LIMIT ?",
            [*params, max(1, min(int(limit), 500))],
        ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["age_s"] = max(0, stamp - int(item["received_at"]))
            item["expired"] = item["age_s"] > ttl
            items.append(item)
        return {"drops": items, "total": total, "live": total - expired,
                "expired": expired, "ttl_s": ttl}

    def drop_counts(self, ttl_s: int | None = None, *,
                    now: int | None = None) -> dict[str, int]:
        """The refusal count `status` carries: how many, how many still current."""
        ttl = DEFAULT_DROPS_TTL_HOURS * 3600 if ttl_s is None else int(ttl_s)
        stamp = int(time.time()) if now is None else int(now)
        row = self.db.execute(
            f"SELECT COUNT(*) AS total, SUM(received_at < ?) AS expired FROM rumors"
            f" WHERE {self._DROP_SQL}",
            (stamp - ttl,),
        ).fetchone()
        total = int(row["total"] or 0)
        expired = int(row["expired"] or 0)
        return {"total": total, "live": total - expired, "expired": expired}

    def conversations(self, plane: str, *, limit: int = 50) -> list[dict[str, Any]]:
        """The distinct conversations we hold messages for on `plane`, newest first.

        Evidence, not a cache: a restarted process can answer "what is this plane
        attached to?" from rows it already stored instead of from whatever happened
        to be resolved since it booted. `plane` is a parameter so every plane can
        use the same query; the bound keeps a pathological store from turning a
        status call into a full table walk.
        """
        rows = self.db.execute(
            "SELECT conversation, COUNT(*) AS messages, MAX(created_at) AS last_at"
            " FROM rumors WHERE plane = ? AND conversation IS NOT NULL"
            " AND conversation != '' GROUP BY conversation"
            " ORDER BY last_at DESC, conversation ASC LIMIT ?",
            (str(plane), max(1, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- Concord invites (verified, never delivered as messages) ---------
    def record_invite(self, *, id: str, inviter: str, community_id: str, name: str,
                      relays: list[str], bundle: dict[str, Any]) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO concord_invites (id, inviter, community_id, name,"
            " relays, bundle, received_at) VALUES (?,?,?,?,?,?,?)",
            (id, inviter, community_id, name, json.dumps(relays), json.dumps(bundle),
             int(time.time())),
        )
        return cur.rowcount > 0

    def concord_invites(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM concord_invites ORDER BY received_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["bundle"] = json.loads(item.get("bundle") or "{}")
            item["relays"] = json.loads(item.get("relays") or "[]")
            out.append(item)
        return out

    def stats(self) -> dict[str, Any]:
        # A refusal is never "pending delivery": a `reach: drop` row carries no
        # blocked_reason, so before this it sat in the pending count for ever even
        # though no path would ever deliver or replay it.
        row = self.db.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(delivered_at IS NULL AND blocked_reason IS NULL"
            "     AND COALESCE(reach, '') != 'drop') AS pending,"
            " SUM(blocked_reason IS NOT NULL OR reach = 'drop') AS blocked FROM rumors"
        ).fetchone()
        invites = self.db.execute("SELECT COUNT(*) AS n FROM concord_invites").fetchone()
        media = self.db.execute(
            "SELECT COUNT(*) AS n,"
            " COUNT(DISTINCT CASE WHEN local_path IS NOT NULL THEN sha256 END) AS cached"
            " FROM attachments"
        ).fetchone()
        return {"total": row["total"], "pending": row["pending"] or 0,
                "blocked": row["blocked"] or 0,
                "invites": invites["n"], "relays": self.relay_report(),
                "media": {"attachments": media["n"], "cached": media["cached"] or 0}}

    # -- digests --------------------------------------------------------
    def seal_digests(self, now: int | float | None = None, *, top_n: int = 5) -> list[dict[str, Any]]:
        """Seal every due rule window, oldest first, and return the digest frames.

        Each window is one transaction. The digest id is deterministic for
        ``(identity, rule, window_start)``, so a crash after the digest insert and before the
        rumor update leaves a retry that completes the same batch instead of hitting the
        unique constraint and wedging the rule forever.
        """
        stamp = int(time.time()) if now is None else int(now)
        due = self.db.execute(
            "SELECT * FROM digest_rules WHERE identity = ? AND window_start + interval_s <= ?"
            " ORDER BY window_start ASC, rule ASC",
            (self.identity, stamp),
        ).fetchall()
        frames: list[dict[str, Any]] = []
        for rule_row in due:
            rule = str(rule_row["rule"])
            interval = max(1, int(rule_row["interval_s"]))
            window_start = int(rule_row["window_start"])
            guard = 0
            while window_start + interval <= stamp and guard < 1000:
                window_end = window_start + interval
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    rows = self.db.execute(
                        "SELECT * FROM rumors WHERE digest_rule = ? AND digest_state IN"
                        " ('queued','dropped') AND received_at >= ? AND received_at < ?"
                        " ORDER BY cursor ASC",
                        (rule, window_start, window_end),
                    ).fetchall()
                    queued = [r for r in rows if r["digest_state"] == "queued"]
                    dropped = [r for r in rows if r["digest_state"] == "dropped"]

                    digest_id: str | None = None
                    if queued:
                        existing = self.db.execute(
                            "SELECT * FROM digests WHERE identity = ? AND rule = ?"
                            " AND window_start = ?",
                            (self.identity, rule, window_start),
                        ).fetchone()
                        if existing is not None:
                            digest_id = str(existing["id"])
                        else:
                            digest_id = f"d:{self.identity}:{rule}:{window_start}"
                            frame_seq = self._next_frame()
                            ordered = sorted(
                                queued, key=lambda r: (int(r["created_at"]), int(r["cursor"])),
                                reverse=True,
                            )
                            top_ids = [str(r["id"]) for r in ordered[:top_n]]
                            planes = {str(r["plane"]) for r in queued}
                            plane = planes.pop() if len(planes) == 1 else "mixed"
                            cursor_start = int(rows[0]["cursor"])
                            cursor_end = int(rows[-1]["cursor"])
                            self.db.execute(
                                "INSERT INTO digests (id, identity, frame_seq, rule, plane,"
                                " window_start, window_end, cursor_start, cursor_end, item_count,"
                                " delivered_count, dropped_count, top_ids, created_at)"
                                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                (
                                    digest_id, self.identity, frame_seq, rule, plane,
                                    window_start, window_end, cursor_start, cursor_end,
                                    len(rows), len(top_ids), len(dropped),
                                    json.dumps(top_ids), stamp,
                                ),
                            )
                        self.db.execute(
                            "UPDATE rumors SET digest_state = 'batched', digest_batch = ?"
                            " WHERE digest_rule = ? AND digest_state = 'queued'"
                            " AND received_at >= ? AND received_at < ?",
                            (digest_id, rule, window_start, window_end),
                        )
                        # Dropped rows share the batch id so the cursor range stays auditable,
                        # but keep their state: they were never delivered.
                        self.db.execute(
                            "UPDATE rumors SET digest_batch = ? WHERE digest_rule = ?"
                            " AND digest_state = 'dropped' AND received_at >= ?"
                            " AND received_at < ?",
                            (digest_id, rule, window_start, window_end),
                        )

                    self.db.execute(
                        "UPDATE digest_rules SET window_start = ?, last_batch_at = ?"
                        " WHERE identity = ? AND rule = ?",
                        (window_end, stamp, self.identity, rule),
                    )
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise

                if digest_id is not None:
                    frames.append(self.digest_frame(digest_id))
                window_start = window_end
                guard += 1

                if not rows:
                    pending = self.db.execute(
                        "SELECT MIN(received_at) AS first FROM rumors WHERE digest_rule = ?"
                        " AND digest_state IN ('queued','dropped')",
                        (rule,),
                    ).fetchone()
                    if pending and pending["first"] is not None:
                        first = int(pending["first"])
                        candidate = (first // interval) * interval
                        if candidate > window_start:
                            self.db.execute(
                                "UPDATE digest_rules SET window_start = ?"
                                " WHERE identity = ? AND rule = ?",
                                (candidate, self.identity, rule),
                            )
                            window_start = candidate
                    else:
                        candidate = (stamp // interval) * interval
                        if candidate > window_start:
                            self.db.execute(
                                "UPDATE digest_rules SET window_start = ?"
                                " WHERE identity = ? AND rule = ?",
                                (candidate, self.identity, rule),
                            )
                        break
        return frames

    def _next_frame(self) -> int:
        self._ensure_cursor()
        row = self.db.execute(
            "SELECT next_frame FROM delivery_cursor WHERE identity = ?", (self.identity,)
        ).fetchone()
        frame = int(row["next_frame"])
        self.db.execute(
            "UPDATE delivery_cursor SET next_frame = ? WHERE identity = ?",
            (frame + 1, self.identity),
        )
        return frame

    def digest_frame(self, digest_id: str) -> dict[str, Any]:
        """One digest as an ordinary message frame, rebuilt from the joined rumors.

        ``digests`` stores ids only; `content` is rendered here, at delivery and again at
        replay, so the same row always yields the same frame and no plaintext is copied
        into the digest record.
        """
        row = self.db.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if row is None:
            raise KeyError(digest_id)
        items = self.db.execute(
            "SELECT * FROM rumors WHERE digest_batch = ? ORDER BY cursor ASC", (digest_id,)
        ).fetchall()
        by_id = {str(r["id"]): r for r in items}
        top_ids = json.loads(row["top_ids"])
        top = [by_id[i] for i in top_ids if i in by_id]
        lines = []
        for r in top:
            who = str(r["sender"] or "")[:10]
            text = (r["content"] or "").replace("\n", " ")[:140]
            lines.append(f"- {who}… ({r['rule'] or row['rule']}): {text}")
        delivered = int(row["delivered_count"])
        dropped = int(row["dropped_count"])
        header = f"{delivered} item(s) in the {row['rule']} digest"
        if dropped:
            header += f" ({dropped} dropped by quota)"
        return {
            "type": "message",
            "plane": "digest",
            "id": str(row["id"]),
            "sender": "digest",
            "conversation": "digest",
            "kind": 1,
            "created_at": int(row["created_at"]),
            "reply_to": None,
            "reach": "digest",
            "authority": "none",
            "why": f"{int(row['item_count'])} item(s) awaited the "
                   f"{row['rule']} digest window",
            "content": header + ":\n" + "\n".join(lines),
            "frame": int(row["frame_seq"]),
            "rule": str(row["rule"]),
            "source_plane": str(row["plane"]),
            "identity": row["identity"],
            "window_start": int(row["window_start"]),
            "window_end": int(row["window_end"]),
            "count": int(row["item_count"]),
            "delivered": delivered,
            "dropped": dropped,
            "cursor_start": int(row["cursor_start"]),
            "cursor_end": int(row["cursor_end"]),
            "items": [
                {
                    "rumor_id": str(r["id"]),
                    "from": r["sender"],
                    "created_at": int(r["created_at"]),
                    "authority": r["authority"],
                    "why": r["why"],
                    "snippet": (r["content"] or "")[:256],
                }
                for r in top
            ],
            "truncated": delivered < int(row["item_count"]) - dropped,
        }

    def replay_digests(self, since_frame: int = 0) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT id FROM digests WHERE identity = ? AND frame_seq > ? AND acked_at IS NULL"
            " ORDER BY frame_seq ASC",
            (self.identity, int(since_frame)),
        ).fetchall()
        return [self.digest_frame(r["id"]) for r in rows]

    def ack_digest(self, rule: str, frame: int) -> dict[str, Any]:
        row = self.db.execute(
            "SELECT * FROM digests WHERE identity = ? AND rule = ? AND frame_seq <= ?"
            " ORDER BY frame_seq DESC LIMIT 1",
            (self.identity, rule, int(frame)),
        ).fetchone()
        if row is None:
            return {"rule": rule, "frame": frame, "acked": 0}
        now = int(time.time())
        self.db.execute(
            "UPDATE digest_rules SET acked_frame = MAX(acked_frame, ?)"
            " WHERE identity = ? AND rule = ?",
            (int(frame), self.identity, rule),
        )
        cur = self.db.execute(
            "UPDATE rumors SET delivered_at = COALESCE(delivered_at, ?)"
            " WHERE digest_state = 'batched' AND digest_batch IN ("
            " SELECT id FROM digests WHERE identity = ? AND rule = ? AND frame_seq <= ?)",
            (now, self.identity, rule, int(frame)),
        )
        self.db.execute(
            "UPDATE digests SET acked_at = COALESCE(acked_at, ?)"
            " WHERE identity = ? AND rule = ? AND frame_seq <= ?",
            (now, self.identity, rule, int(frame)),
        )
        return {"rule": rule, "frame": int(frame), "acked": cur.rowcount}

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def dumps(frame: dict[str, Any]) -> str:
        return json.dumps(frame, separators=(",", ":"), sort_keys=True)
