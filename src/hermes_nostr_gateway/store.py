"""Durable state: what we have seen, what we have delivered, and why.

Dedupe keys on the *rumor* id, not the wrapper: the same message arrives once per relay
with a different wrapper id each time, so wrapper-level dedupe misses duplicates across
relays (Hal's finding, spec §14).

v1 is one identity per process. ``rumors`` therefore carries no identity column; the
``delivery_cursor`` row is keyed by the local npub so digest tables remain ready for a
multi-identity daemon later. See ``docs/digest-cursor.md``.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
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
    def record_wrap(self, event: dict, rumor_id: str, recipient: str, relays: list[str]) -> None:
        for relay in relays:
            self.db.execute(
                "INSERT INTO outbound_wraps (id, relay, event, rumor_id, recipient) "
                "VALUES (?,?,?,?,?) ON CONFLICT(id, relay) DO NOTHING",
                (event["id"], relay, json.dumps(event), rumor_id, recipient),
            )

    def mark_wrap(self, wrap_id: str, relay: str, *, ok: bool, error: str | None = None) -> None:
        self.db.execute(
            "UPDATE outbound_wraps SET status = ?, attempts = attempts + 1, last_attempt = ?,"
            " last_error = ? WHERE id = ? AND relay = ?",
            ("sent" if ok else "failed", int(time.time()), error, wrap_id, relay),
        )

    def pending_wraps(self, max_attempts: int = 6) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM outbound_wraps WHERE status != 'sent' AND attempts < ? "
            "ORDER BY attempts, last_attempt IS NULL DESC",
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
        self.db.executemany(
            "UPDATE rumors SET delivered_at = ? WHERE id = ?",
            [(int(time.time()), i) for i in ids],
        )

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND blocked_reason IS NULL"
            " ORDER BY received_at, rowid LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def pending_digest(self, older_than_s: int, limit: int = 20) -> list[dict[str, Any]]:
        """Digest-class messages that have waited out the window and not been sent."""
        cutoff = int(time.time()) - older_than_s
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND reach = 'digest'"
            " AND blocked_reason IS NULL AND received_at <= ? ORDER BY received_at, rowid LIMIT ?",
            (cutoff, limit),
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
        return [dict(r) for r in rows]

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

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM rumors ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

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
        row = self.db.execute(
            "SELECT COUNT(*) AS total,"
            " SUM(delivered_at IS NULL AND blocked_reason IS NULL) AS pending,"
            " SUM(blocked_reason IS NOT NULL) AS blocked FROM rumors"
        ).fetchone()
        invites = self.db.execute("SELECT COUNT(*) AS n FROM concord_invites").fetchone()
        return {"total": row["total"], "pending": row["pending"] or 0,
                "blocked": row["blocked"] or 0,
                "invites": invites["n"], "relays": self.relay_report()}

    # -- digests --------------------------------------------------------
    def seal_digests(self, now: int | float | None = None, *, top_n: int = 5) -> list[dict[str, Any]]:
        """Seal every due rule window, oldest first, and return the digest frames."""
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
                rows = self.db.execute(
                    "SELECT * FROM rumors WHERE digest_rule = ? AND digest_state IN"
                    " ('queued','dropped') AND received_at >= ? AND received_at < ?"
                    " ORDER BY cursor ASC",
                    (rule, window_start, window_end),
                ).fetchall()
                queued = [r for r in rows if r["digest_state"] == "queued"]
                dropped = [r for r in rows if r["digest_state"] == "dropped"]

                if queued:
                    digest_id = uuid.uuid4().hex
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
                        " AND digest_state = 'dropped' AND received_at >= ? AND received_at < ?",
                        (digest_id, rule, window_start, window_end),
                    )
                    frames.append(self.digest_frame(digest_id))
                elif dropped:
                    # Nothing to send; leave dropped rows without a batch. Cursor range
                    # remains contiguous on the next real batch.
                    pass

                self.db.execute(
                    "UPDATE digest_rules SET window_start = ?, last_batch_at = ?"
                    " WHERE identity = ? AND rule = ?",
                    (window_end, stamp, self.identity, rule),
                )
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
        row = self.db.execute("SELECT * FROM digests WHERE id = ?", (digest_id,)).fetchone()
        if row is None:
            raise KeyError(digest_id)
        items = self.db.execute(
            "SELECT * FROM rumors WHERE digest_batch = ? ORDER BY cursor ASC", (digest_id,)
        ).fetchall()
        by_id = {str(r["id"]): r for r in items}
        top_ids = json.loads(row["top_ids"])
        return {
            "type": "digest",
            "frame": int(row["frame_seq"]),
            "identity": row["identity"],
            "rule": row["rule"],
            "plane": row["plane"],
            "window_start": int(row["window_start"]),
            "window_end": int(row["window_end"]),
            "count": int(row["item_count"]),
            "delivered": int(row["delivered_count"]),
            "dropped": int(row["dropped_count"]),
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
                for r in (by_id.get(i) for i in top_ids) if r is not None
            ],
            "truncated": int(row["delivered_count"]) < int(row["item_count"]),
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
            "UPDATE rumors SET delivered_at = COALESCE(delivered_at, ?) WHERE digest_batch = ?",
            (now, row["id"]),
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
