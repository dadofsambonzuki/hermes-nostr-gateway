"""Durable state: what we have seen, what we have delivered, and why.

Dedupe keys on the *rumor* id, not the wrapper: the same message arrives once per relay
with a different wrapper id each time, so wrapper-level dedupe misses duplicates across
relays (Hal's finding, spec §14).
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

CREATE TABLE IF NOT EXISTS concord_invites (
    id           TEXT PRIMARY KEY,
    inviter      TEXT NOT NULL,
    community_id TEXT NOT NULL,
    name         TEXT,
    relays       TEXT,
    bundle       TEXT NOT NULL,
    received_at  INTEGER NOT NULL
);
"""


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
        return frame


class Store:
    def __init__(self, path: str | Path = "gateway.db"):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Add columns a live store predates; `CREATE TABLE IF NOT EXISTS` won't."""
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(rumors)")}
        if "blocked_reason" not in columns:
            self.db.execute("ALTER TABLE rumors ADD COLUMN blocked_reason TEXT")

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
    def add(self, msg: Inbound) -> bool:
        # A sender may write the same text over NIP-17 and NIP-04; ids differ per plane,
        # so also dedupe on (sender, created_at, content). Without this the agent sees
        # the same message twice.
        if msg.authority != "self":
            row = self.db.execute(
                "SELECT id FROM rumors WHERE sender = ? AND created_at BETWEEN ? AND ? "
                "AND content = ? LIMIT 1",
                (msg.sender, msg.created_at - 2, msg.created_at + 2, msg.content),
            ).fetchone()
            if row is not None:
                return False
        try:
            self.db.execute(
                "INSERT INTO rumors (id, wrapper_id, sender, kind, created_at, conversation,"
                " content, reply_to, plane, reach, authority, rule, why, received_at,"
                " blocked_reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.id, msg.wrapper_id, msg.sender, msg.kind, msg.created_at,
                    msg.conversation, msg.content, msg.reply_to, msg.plane,
                    msg.reach, msg.authority, msg.rule, msg.why, int(time.time()),
                    msg.blocked_reason,
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False

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

    def pending_immediate(self, limit: int = 20, max_age_days: int = 2) -> list[dict[str, Any]]:
        """Messages that never reached an adapter (refused, or crashed mid-delivery).

        Replayed on subscribe so a reconnect cannot lose a message that was accepted
        but never handed over. Ordering is by arrival and then insertion: `received_at`
        has second resolution, and a replay has to keep the order the frames first
        appeared in or a cursor-taking consumer would see it jump backwards.
        """
        cutoff = int(time.time()) - max_age_days * 86400
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND reach = 'immediate'"
            " AND authority != 'self' AND blocked_reason IS NULL AND received_at >= ?"
            " ORDER BY received_at, rowid LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        return [dict(r) for r in rows]

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

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def dumps(frame: dict[str, Any]) -> str:
        return json.dumps(frame, separators=(",", ":"), sort_keys=True)
