"""Durable state: what we have seen, what we have delivered, and why.

Dedupe keys on the *rumor* id, not the wrapper: the same message arrives once per relay
with a different wrapper id each time, so wrapper-level dedupe misses duplicates across
relays (spec §14).

v1 is one identity per process. ``rumors`` therefore carries no identity column; the
``delivery_cursor`` row is keyed by the local npub so digest tables remain ready for a
multi-identity daemon later. See ``docs/digest-cursor.md``.

Writes (docs/store-writes.md)
-----------------------------
A connection is per thread, and every connection opens the file with
``journal_mode=WAL``, a busy timeout and ``synchronous=NORMAL`` (the default; ``FULL``
is selectable so the benchmark can measure the old cost). WAL is what lets the daemon
read on the event loop while the writer thread commits, without either blocking the
other. ``NORMAL`` is the durability setting that matters here: a committed transaction
is in the WAL before the commit returns, so ``kill -9`` loses nothing, while the
per-commit fsync that ``FULL`` adds is exactly what used to stall the loop. It is *not*
power-loss durable — that needs ``FULL`` and a slower write path.

The batch entry points (``add_many``, ``wrappers_seen``, ``note_relays``) exist so a
burst costs one transaction instead of one per row; ``StoreWriter`` (writer.py) calls
them from its own thread. ``add``, ``wrapper_seen`` and ``note_relay`` route to an
attached writer when there is one and write directly when there is not — which is how
the CLI, the tests and a ``kill -9`` recovery script keep their simple behaviour.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .writer import StoreWriter

#: One name for the store, the same one writer.py logs under: the two halves of the same
#: subsystem, and two names for one of them only splits the journal.
log = logging.getLogger("hng.store")

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
    -- `queue` or `steer` when the sender asked for one (see addressing.py) and the policy
    -- allowed it; `normal` otherwise, including for every row written before this column. A
    -- row written before `/steer` was the name still says `guide`, read as `steer`.
    mode          TEXT NOT NULL DEFAULT 'normal',
    authority     TEXT,
    rule          TEXT,
    why           TEXT,
    received_at   INTEGER NOT NULL,
    delivered_at  INTEGER,
    blocked_reason TEXT
);
CREATE INDEX IF NOT EXISTS rumors_pending ON rumors (delivered_at, received_at);
CREATE INDEX IF NOT EXISTS rumors_sender ON rumors (sender, created_at);
-- Echo suppression reads our own recent text per conversation; without this the query
-- is a full scan on every message from a bot peer.
CREATE INDEX IF NOT EXISTS rumors_conversation ON rumors (conversation, received_at);

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

-- What this identity last advertised: its kind-0 profile and its kind-10304 command
-- manifest. The content hash is the decision -- a restart republishes only when the
-- stored copy differs -- and the event id is what a per-relay read-back compares against.
CREATE TABLE IF NOT EXISTS advertised (
    kind         INTEGER PRIMARY KEY,
    event_id     TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    published_at INTEGER NOT NULL,
    -- Relays that refused the kind outright, as JSON {relay: reason}. A refusal is a
    -- decision, so it is recorded rather than retried on every start.
    refused      TEXT NOT NULL DEFAULT '{}'
);

-- Relays still owed an advertisement: the last attempt failed for a *condition* (a rate
-- limit, an outage), not a judgement, so the copy is not a refusal and must be retried --
-- but with a backoff, and only so many times. One row per kind and relay; `attempts` is
-- the bound and `next_at` the earliest moment a retry is due. Recording this separately
-- from `advertised.refused` is the whole point: a rate-limited relay is not a refusal.
CREATE TABLE IF NOT EXISTS advertised_retry (
    kind     INTEGER NOT NULL,
    relay    TEXT NOT NULL,
    reason   TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 1,
    next_at  INTEGER NOT NULL,
    PRIMARY KEY (kind, relay)
);

-- Kind-0 claims we have read, so a restart does not refetch every peer and a peer that
-- is a bot stays one. `bot` is 0/1, never NULL: a profile that said nothing about `bot`
-- is a human by omission, which is different from a peer we have never looked up (no row).
CREATE TABLE IF NOT EXISTS profiles (
    pubkey     TEXT PRIMARY KEY,
    name       TEXT,
    bot        INTEGER NOT NULL DEFAULT 0,
    event_id   TEXT,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS profiles_bot ON profiles (bot);

-- An invite is recorded the moment it verifies and is *never* a message. What happened to
-- it afterwards is a column, not a guess: `held` (recorded, waiting for a human), `joining`
-- (a Guestbook join is in flight), `joined` (a relay accepted it) or `failed` (none did).
CREATE TABLE IF NOT EXISTS concord_invites (
    id           TEXT PRIMARY KEY,
    inviter      TEXT NOT NULL,
    community_id TEXT NOT NULL,
    name         TEXT,
    relays       TEXT,
    bundle       TEXT NOT NULL,
    received_at  INTEGER NOT NULL,
    status       TEXT NOT NULL DEFAULT 'held',
    why          TEXT,
    decided_at   INTEGER,
    decided_by   TEXT,
    announced_at INTEGER
);

-- Every Concord channel id this gateway has already announced to the control pane, one
-- row per community and channel. The id is the dedupe: a restart re-reads the same grants
-- and finds the row, so it stays silent. The name is what the notice called the channel --
-- a diagnostic, not an identity (a rename is the same channel and needs no second notice).
CREATE TABLE IF NOT EXISTS concord_announced (
    community_id TEXT NOT NULL,
    channel_id   TEXT NOT NULL,
    name         TEXT,
    announced_at INTEGER NOT NULL,
    PRIMARY KEY (community_id, channel_id)
);

-- The ngit bridge: what one Nostr issue or PR event became, and how far a repo has been
-- read. `state` is the bridge's lifecycle, not a task's status -- a `deleted` row is what
-- stops a hand-deleted task being recreated, and `asked` is what stops a question being
-- asked twice. `detail` is the bridge's own note about the source, as JSON.
CREATE TABLE IF NOT EXISTS ngit_links (
    repo       TEXT NOT NULL,     -- the repo's NIP-34 address, or its path when it has none
    kind       INTEGER NOT NULL,  -- 1621 issue, 1618 pull request
    source_id  TEXT NOT NULL,     -- the source event id, hex
    task_id    TEXT,
    board      TEXT,
    state      TEXT NOT NULL,
    detail     TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, kind, source_id)
);
CREATE INDEX IF NOT EXISTS ngit_links_task ON ngit_links (task_id);
CREATE INDEX IF NOT EXISTS ngit_links_source ON ngit_links (kind, source_id);

-- One row per comment already copied onto a task, so a re-poll appends each exactly once
-- and in the order the issue carries them.
CREATE TABLE IF NOT EXISTS ngit_comments (
    repo       TEXT NOT NULL,
    source_id  TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (repo, source_id, comment_id)
);

-- One row per comment that mentioned this gateway, so a re-poll cannot file the same reply
-- twice. `task_id` is empty when the mention was seen and deliberately not answered (an
-- untrusted author), which is a decision worth remembering: a later pass must not re-decide it.
CREATE TABLE IF NOT EXISTS ngit_mentions (
    repo       TEXT NOT NULL,
    comment_id TEXT NOT NULL,
    task_id    TEXT,
    detail     TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (repo, comment_id)
);

-- Per repo and stream, the last event time a poll reached: a poll asks only for what is
-- newer, and a catch-up after downtime does not re-read a week of history.
CREATE TABLE IF NOT EXISTS ngit_cursors (
    repo       TEXT NOT NULL,
    stream     TEXT NOT NULL,
    value      TEXT NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (repo, stream)
);

-- Notices the bridge has decided to post, and has not had confirmed yet. The queue is what
-- makes a busy daemon a delay instead of a loss: a poll hands a notice over, and anything
-- the daemon did not take stays `queued` for the next one. One row per PR and notice, so
-- deciding the same thing twice cannot queue it twice.
CREATE TABLE IF NOT EXISTS ngit_outbox (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    repo         TEXT NOT NULL,
    kind         INTEGER NOT NULL,  -- 1621 issue, 1618 pull request
    source_id    TEXT NOT NULL,   -- the event id, hex
    notice       TEXT NOT NULL,   -- 'issue', 'question', 'ready' or 'merged'
    conversation TEXT NOT NULL,
    text         TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'queued',   -- 'queued' until the daemon takes it
    attempts     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    created_at   INTEGER NOT NULL,
    updated_at   INTEGER NOT NULL,
    UNIQUE (repo, source_id, notice)
);

-- A caller's idempotency key -> the message it posted. `send` records one when it is given
-- a key and answers a repeat with that message id instead of publishing again: a caller
-- that lost the acknowledgement cannot turn its retry into a second message in the channel.
CREATE TABLE IF NOT EXISTS send_keys (
    key          TEXT PRIMARY KEY,
    message_id   TEXT NOT NULL,
    conversation TEXT,
    created_at   INTEGER NOT NULL
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
    #: The provider/model a turn was asked for, as JSON: names only, never a credential. On a
    #: reply row it is what the adapter reported using, which is the audit trail.
    "route": "TEXT",
    #: A row from before the prefixes existed asked for nothing: `normal` is the truth, and
    #: NOT NULL means a reader never has to distinguish "no mode" from "normal".
    "mode": "TEXT NOT NULL DEFAULT 'normal'",
    #: The id of the newer message in the same plane and conversation that took this
    #: row's place in a shaped replay. Empty when the row was aged out instead (there is
    #: no single newer message then). The marker is durable: a shaped row must not become
    #: a turn merely because the row that superseded it was acked first.
    "superseded_by": "TEXT",
    #: Why a delivery ended without a turn: `superseded` when the adapter filed a shaped
    #: replay row as context and acked it. Deliberately not `blocked_reason`: that means
    #: policy refused the message and feeds `drops`; a superseded message was accepted,
    #: is still in `history`, and must not read as a refusal.
    "settled_reason": "TEXT",
}

#: The states an invite can be in. `held` is the default, and the truth for every row
#: recorded before the join path existed: an invite nobody has joined is waiting, not joined.
INVITE_STATES = ("held", "joining", "joined", "failed")

#: Columns `concord_invites` gained with the join path. Additive, like `_RUMOR_COLUMNS`, so
#: an existing store keeps its invites and reads them as `held` until something decides one.
_INVITE_COLUMNS = {
    "status": "TEXT NOT NULL DEFAULT 'held'",
    "why": "TEXT",
    "decided_at": "INTEGER",
    "decided_by": "TEXT",
    "announced_at": "INTEGER",
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


def _invite_row(row: sqlite3.Row) -> dict[str, Any]:
    """One stored invite with its JSON columns decoded, as every caller wants it."""
    item = dict(row)
    item["bundle"] = json.loads(item.get("bundle") or "{}")
    item["relays"] = json.loads(item.get("relays") or "[]")
    return item


def _json_or_none(raw: Any) -> Any:
    """A JSON column as the object it holds, or None when it is empty or unreadable."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


#: Why a replay row was settled without a turn (`settled_reason`). Closed on purpose:
#: a crash, a policy refusal and a digest each have their own lifecycle and must not
#: borrow this name.
SETTLED_SUPERSEDED = "superseded"
SETTLED_REASONS = frozenset({SETTLED_SUPERSEDED})

#: A typed answer to an approval gate. Never superseded: a gate answered by a message
#: filed as context is a lost decision. The boundary keeps `/approvals` (Hermes' listing
#: command) from reading as an answer.
_APPROVAL_REPLY = re.compile(r"^/(approve|deny)(?:\s|$)", re.IGNORECASE)


def is_approval_reply(text: str | None) -> bool:
    """True when *text* is a typed `/approve` or `/deny` answer, not ordinary chat."""
    return bool(_APPROVAL_REPLY.match((text or "").strip()))


def _replay_group(row: dict[str, Any]) -> tuple[str, str]:
    """The unit shaping may collapse: one conversation on one plane.

    Planes never supersede each other — the same peer on NIP-17 and NIP-04 is two
    transports, not one message replacing another — and a row with no conversation is
    its own group rather than sharing an empty key with every other sender-less row.
    """
    plane = str(row.get("plane") or "")
    conversation = str(row.get("conversation") or "")
    if conversation:
        return (plane, conversation)
    return (plane, f"id:{row.get('id') or ''}")


def _media_keys(row: dict[str, Any]) -> set[str]:
    """The attachments a row carries, by digest (or url when it claims no digest)."""
    keys: set[str] = set()
    for item in row.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        digest = str(item.get("sha256") or "").strip().lower()
        if digest:
            keys.add("sha256:" + digest)
        elif item.get("url"):
            keys.add("url:" + str(item["url"]))
    return keys


def shape_replay(rows: list[dict[str, Any]], *, max_messages: int = 5,
                 max_age_secs: int = 600, now: int | None = None) -> list[dict[str, Any]]:
    """Decide which replayed rows become turns, and which are context.

    A reconnect is not a history lesson: a peer who wrote six lines into a dead stream
    should cost one turn, not six. Working newest-first over the batch (the caller
    supplies arrival order, oldest first), a row is marked ``superseded`` when a newer
    *deliverable* row exists in the same plane and conversation, when it is older than
    ``max_age_secs``, or when ``max_messages`` deliverable turns are already kept in
    this batch. Superseded rows are never deleted: their content stays in the store and
    `history` shows it; the adapter acks them as context instead of turning them into a
    run.

    Three exceptions protect work the batch must not silently swallow:

    * a reaction (kind 7) and a typed ``/approve``/``/deny`` reply are a decision, not a
      turn to save — they are always deliverable and never count against the cap;
    * a row carrying media is never superseded unless a newer deliverable row carries
      the same media, so a photo cannot be replaced by a later caption that lacks it;
      such a row is delivered even past the cap, because losing the picture is worse
      than one extra turn;
    * a row already marked in the store as superseded stays that way. The marker is
      written by `Store.replay_batch` so a failed ack cannot resurrect an old message as
      a turn once the newer row has been acked and left the batch.

    ``0`` disables a bound: ``max_messages=0`` keeps every deliverable row (the old
    behaviour apart from the age horizon), ``max_age_secs=0`` removes the horizon.
    Both at ``0`` is exactly the pre-shaping replay.
    """
    if not rows:
        return rows
    stamp = int(time.time() if now is None else now)
    cap = max(0, int(max_messages))
    horizon = max(0, int(max_age_secs))
    cutoff = stamp - horizon if horizon else None

    newest: dict[tuple[str, str], str] = {}
    covered: dict[tuple[str, str], set[str]] = {}
    kept = 0

    def mark(row: dict[str, Any], by: str | None) -> None:
        row["superseded"] = True
        # Keep the first decision: a row already marked in the store must not be
        # re-pointed at whichever newer row happens to be left in this batch.
        existing = row.get("superseded_by")
        row["superseded_by"] = existing if isinstance(existing, str) else (by or "")

    def deliver(row: dict[str, Any], group: tuple[str, str]) -> None:
        nonlocal kept
        kept += 1
        newest[group] = str(row.get("id") or "")
        covered.setdefault(group, set()).update(_media_keys(row))

    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        group = _replay_group(row)
        if row.get("kind") == 7 or is_approval_reply(row.get("content")):
            # A decision, not chat: it must reach the adapter even if everything around
            # it is collapsed, and it does not cost one of the replay's turns. It is
            # also not a *newer message* that can supersede an older one — an old line
            # of text must not lose its turn to a 👍 — so it does not become the group's
            # newest deliverable.
            continue
        if cutoff is not None and int(row.get("received_at") or 0) < cutoff:
            mark(row, newest.get(group))
            continue
        if row.get("superseded_by") is not None:
            mark(row, row.get("superseded_by") if isinstance(row.get("superseded_by"), str)
                 else "")
            continue
        newer = newest.get(group)
        if newer is not None:
            if not (_media_keys(row) - covered.get(group, set())):
                mark(row, newer)
                continue
            # Else: media the newer deliverable row does not carry. Fall through and
            # deliver it — that is the one thing a replay must not lose.
        elif cap and kept >= cap:
            if not (_media_keys(row) - covered.get(group, set())):
                mark(row, "")
                continue
        deliver(row, group)
    return rows


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
    #: `normal`, `queue` or `steer`: what the sender asked of this message, when the policy
    #: let them ask it (addressing.py). Kept on the row so a replay carries it too.
    mode: str = "normal"
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
    #: The provider/model a turn is asked to use, names only (`key` is a variable's name).
    #: On a reply row: what the adapter reported the turn actually used.
    route: dict[str, Any] | None = None

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
            "mode": self.mode,
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


#: The window every plane shares (spec §10.5). It lives in the store because that is the
#: one place every path writes a rumor: a relay in the read set can replay an old message
#: from a trusted sender, and a row refused here can never be offered as pending again.
MAX_MESSAGE_AGE_S = 3 * 86400
MAX_FUTURE_S = 300


def _out_of_window(created_at: int, now: int) -> bool:
    return (created_at < now - MAX_MESSAGE_AGE_S
            or created_at > now + MAX_FUTURE_S)


class Store:
    #: Applied to every connection. `busy_timeout` is not optional once two threads
    #: hold the file: without it a reader that arrives mid-checkpoint gets SQLITE_BUSY
    #: instead of waiting the few milliseconds the writer needs.
    PRAGMAS = (
        "PRAGMA journal_mode=WAL",
        "PRAGMA busy_timeout=5000",
    )
    #: Durability levels we accept. `NORMAL` in WAL survives `kill -9` without an fsync
    #: per commit; `FULL` is the setting the daemon used before P6, kept so the burst
    #: benchmark can measure what the fsync actually cost.
    SYNCHRONOUS = ("OFF", "NORMAL", "FULL", "EXTRA")

    def __init__(self, path: str | Path = "gateway.db", *, identity: str = "",
                 synchronous: str = "NORMAL"):
        self.path = str(path)
        level = str(synchronous).upper()
        if level not in self.SYNCHRONOUS:
            raise ValueError(f"bad synchronous level: {synchronous!r}")
        self.synchronous = level
        # A store that did not exist a moment ago owns no undelivered history: any
        # pending rows it is given were answered by whoever held the messages before
        # the import copy. Start the replay floor at now for that case only — a normal
        # restart of an existing store must still replay a message that crashed
        # mid-delivery.
        fresh = not Path(self.path).exists()
        self.identity = identity
        #: Set when writes belong to a `StoreWriter` (writer.py): every write method
        #: below then answers from the writer's in-memory index and hands it the row.
        self._writer: StoreWriter | None = None
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._conn_lock = threading.Lock()
        #: Write transactions started. A burst's cost is the count, not a guess: the
        #: batching claim in docs/store-writes.md is measured against this.
        self.transactions = 0
        self.db.executescript(SCHEMA)
        self._migrate()
        if fresh:
            self.set_replay_floor()

    # -- connections ----------------------------------------------------
    @property
    def db(self) -> sqlite3.Connection:
        """The connection for this thread, opened on first use.

        One connection per thread, not one shared: SQLite serialises writers at the
        file level anyway, and a shared connection would make the event loop's reads
        wait behind the writer thread's transaction in *Python* as well as in SQLite.
        """
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._open()
            self._local.conn = conn
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=5.0)
        conn.row_factory = sqlite3.Row
        for pragma in self.PRAGMAS:
            conn.execute(pragma)
        conn.execute(f"PRAGMA synchronous={self.synchronous}")
        with self._conn_lock:
            self._connections.append(conn)
        return conn

    def _begin(self) -> None:
        self.transactions += 1
        self.db.execute("BEGIN IMMEDIATE")

    # -- writer handoff --------------------------------------------------
    def attach_writer(self, writer: StoreWriter) -> None:
        """Route writes to `writer`. Reads keep using this thread's connection."""
        self._writer = writer

    def detach_writer(self) -> None:
        self._writer = None

    @property
    def writer(self) -> StoreWriter | None:
        return self._writer

    async def flush(self) -> int:
        """Wait for the writer to commit everything queued. 0 when nothing is attached."""
        if self._writer is None:
            return 0
        return await self._writer.flush()

    def writer_stats(self) -> dict[str, Any]:
        return self._writer.stats() if self._writer is not None else {}

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

        invite_cols = {
            row["name"] for row in self.db.execute("PRAGMA table_info(concord_invites)")
        }
        for name, kind in _INVITE_COLUMNS.items():
            if name not in invite_cols:
                self.db.execute(f"ALTER TABLE concord_invites ADD COLUMN {name} {kind}")

        advert_cols = {
            row["name"] for row in self.db.execute("PRAGMA table_info(advertised)")
        }
        if "refused" not in advert_cols:
            # A store written before the column existed still holds the event id and hash;
            # the refusals are the only part it could not have recorded.
            self.db.execute(
                "ALTER TABLE advertised ADD COLUMN refused TEXT NOT NULL DEFAULT '{}'"
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
    def wrappers_seen(self, rows: list[tuple[str, str]]) -> list[bool]:
        """Insert wrapper ids in one transaction; True where the id was already known.

        Same contract per row as `wrapper_seen`, batched: a relay's replay after a
        restart arrives as a burst, and one commit for it is the difference between a
        stall and a hiccup.
        """
        if not rows:
            return []
        stamp = int(time.time())
        out: list[bool] = []
        self._begin()
        try:
            for wrapper_id, relay in rows:
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO wrappers (id, relay, seen_at) VALUES (?, ?, ?)",
                    (wrapper_id, relay, stamp),
                )
                out.append(cur.rowcount == 0)
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return out

    def _wrapper_known(self, wrapper_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM wrappers WHERE id = ? LIMIT 1", (wrapper_id,)
        ).fetchone()
        return row is not None

    def wrapper_seen(self, wrapper_id: str, relay: str = "") -> bool:
        """True when this wrapper has been seen before (so the event is a duplicate).

        With a writer attached the *read* still happens here: it is a primary-key
        lookup with no fsync, and it is what makes a restart's replay of the last three
        days harmless. Only the insert is handed over.
        """
        if self._writer is not None:
            if self._wrapper_known(wrapper_id):
                return True
            return self._writer.note_wrapper(wrapper_id, relay)
        return self.wrappers_seen([(wrapper_id, relay)])[0]

    def note_relays(self, rows: list[dict[str, Any]]) -> None:
        """Apply relay counters in one transaction: `{"relay", "events", "error"}`."""
        if not rows:
            return
        stamp = int(time.time())
        self._begin()
        try:
            for row in rows:
                events = int(row.get("events") or 0)
                self.db.execute(
                    "INSERT INTO relay_stats (relay, events, last_event, last_error)"
                    " VALUES (?, ?, ?, ?) ON CONFLICT(relay) DO UPDATE SET"
                    " events = events + excluded.events,"
                    " last_event = COALESCE(excluded.last_event, last_event),"
                    " last_error = COALESCE(excluded.last_error, last_error)",
                    (str(row.get("relay")), events,
                     stamp if events else None, row.get("error")),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

    def note_relay(self, relay: str, *, events: int = 0, error: str | None = None) -> None:
        if self._writer is not None:
            self._writer.note_relay(relay, events=events, error=error)
            return
        self.note_relays([{"relay": relay, "events": events, "error": error}])

    def relay_report(self) -> list[dict[str, Any]]:
        rows = self.db.execute("SELECT * FROM relay_stats ORDER BY relay").fetchall()
        return [dict(r) for r in rows]

    def plane_report(self) -> list[dict[str, Any]]:
        """Per-plane counters: what arrived, when, and what is not yet delivered.

        One row per plane that has ever held a rumor, so a plane with no traffic has
        no row here — `health.plane_health` fills the gap with zeros rather than
        pretending the plane does not exist. `oldest_pending_at` is the age of the
        oldest message still waiting for the adapter: that, not the total, is what
        says whether delivery is running behind.
        """
        rows = self.db.execute(
            "SELECT plane,"
            " COUNT(*) AS events,"
            " SUM(delivered_at IS NULL AND blocked_reason IS NULL) AS pending,"
            " SUM(blocked_reason IS NOT NULL) AS blocked,"
            " SUM(authority = 'self') AS sent,"
            " MAX(created_at) AS last_event_at,"
            " MAX(received_at) AS last_received_at,"
            " MIN(CASE WHEN delivered_at IS NULL AND blocked_reason IS NULL"
            "     THEN received_at END) AS oldest_pending_at"
            " FROM rumors GROUP BY plane ORDER BY plane"
        ).fetchall()
        return [dict(row) for row in rows]

    def wrap_report_by_plane(self) -> dict[str, dict[str, int]]:
        """Outbound-wrap rows per plane, by status: pending, sent, failed, expired.

        Joins the wrap to its rumor because the wrap itself does not record a plane and
        the rumor always does. A plane whose wraps expired is a plane where we accepted
        a message and then gave up publishing it, which `health` must be able to say.
        """
        rows = self.db.execute(
            "SELECT r.plane AS plane, w.status AS status, COUNT(*) AS n"
            " FROM outbound_wraps w JOIN rumors r ON r.id = w.rumor_id"
            " GROUP BY r.plane, w.status"
        ).fetchall()
        out: dict[str, dict[str, int]] = {}
        for row in rows:
            plane = str(row["plane"] or "")
            if not plane:
                continue
            out.setdefault(plane, {})[str(row["status"])] = int(row["n"])
        return out

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
    def duplicate_of(self, msg: Inbound) -> bool:
        """Whether this rumor is already stored: same id, or same sender and text ±2s.

        A sender may write the same text over NIP-17 and NIP-04; ids differ per plane,
        so both checks are needed. Read-only, so the writer can ask it on the event loop
        while a commit is in flight elsewhere.
        """
        if self.db.execute("SELECT 1 FROM rumors WHERE id = ? LIMIT 1",
                           (msg.id,)).fetchone() is not None:
            return True
        if msg.authority == "self":
            return False
        row = self.db.execute(
            "SELECT id FROM rumors WHERE sender = ? AND created_at BETWEEN ? AND ? "
            "AND content = ? LIMIT 1",
            (msg.sender, msg.created_at - 2, msg.created_at + 2, msg.content),
        ).fetchone()
        return row is not None

    def add(self, msg: Inbound, *, now: int | None = None) -> bool:
        """Store one rumor; True when it is new.

        With a writer attached this only appends to its queue (and answers a duplicate
        id from memory): the transaction happens off the event loop, on the writer's
        thread, and `now` is then the batch's arrival stamp rather than the caller's.
        Without one it writes here and now, which is what the CLI and the tests want.
        """
        if self._writer is not None:
            return self._writer.offer(msg)
        return self.add_many([msg], now=now)[0]

    def block_rumor(self, rumor_id: str, reason: str) -> bool:
        """Refuse a stored rumor after the fact, and say why.

        The age check runs in the daemon after the plane has stored the message: a row
        that is refused for its timestamp must stop being replayable, not merely go
        undelivered this time. `blocked_reason` is exactly what `pending_immediate`
        excludes, so this is the durable half of that refusal.
        """
        cur = self.db.execute(
            "UPDATE rumors SET blocked_reason = ? WHERE id = ? AND blocked_reason IS NULL",
            (str(reason)[:200], str(rumor_id)),
        )
        return cur.rowcount > 0

    def reserve_cursors(self, count: int) -> int:
        """Reserve a block of cursors and return the first one.

        A writer hands cursors out from a reserved block, so a burst costs one
        `delivery_cursor` write per block instead of one per message — and, because the
        reservation is a transaction on the file, no two processes (the daemon, an `hng`
        command) can be handed the same number. Cursors reserved but unused when the
        process exits are skipped: cursors are monotonic and never move backwards, and a
        gap is legal (spec §7).
        """
        count = max(1, int(count))
        self._begin()
        try:
            self._ensure_cursor()
            row = self.db.execute(
                "SELECT next_rumor FROM delivery_cursor WHERE identity = ?", (self.identity,)
            ).fetchone()
            first = int(row["next_rumor"])
            self.db.execute(
                "UPDATE delivery_cursor SET next_rumor = ? WHERE identity = ?",
                (first + count, self.identity),
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return first

    def add_many(self, items: list[Inbound], *, now: int | None = None) -> list[bool]:
        """Insert a batch of rumors in **one** transaction, in list order.

        One commit for N rows is the point: the old path paid a transaction — and, at
        `synchronous=FULL`, an fsync — per message, inside the relay read callback, so a
        burst stalled the whole event loop. Each item's outcome is reported in order;
        False means a duplicate (the same id, or the same sender and text within two
        seconds), exactly as `add` would have said.

        `now` stamps the whole batch: rows in one batch arrive together.
        """
        if not items:
            return []
        stamp = int(time.time()) if now is None else int(now)
        results = [False] * len(items)
        self._begin()
        try:
            self._ensure_cursor()
            cursor_row = self.db.execute(
                "SELECT next_rumor FROM delivery_cursor WHERE identity = ?",
                (self.identity,),
            ).fetchone()
            next_rumor = int(cursor_row["next_rumor"])
            max_used = next_rumor - 1
            for position, msg in enumerate(items):
                if self.duplicate_of(msg):
                    continue
                # A pre-assigned cursor (the writer reserved a block) is used as given;
                # otherwise take the next free number.
                cursor = int(msg.cursor) if msg.cursor is not None else max(
                    next_rumor, max_used + 1)
                digest_rule = msg.rule if msg.reach in ("digest", "drop") and msg.rule else None
                digest_state = None
                if _out_of_window(int(msg.created_at or 0), stamp):
                    # Refuse it before it is written, not after: the row's blocked_reason
                    # is what keeps `pending_immediate` from offering it after a restart,
                    # and the writer owns the insert, so a post-hoc update could miss it.
                    msg.reach = "drop"
                    msg.blocked_reason = msg.blocked_reason or "stale_or_future"
                if msg.reach == "digest":
                    digest_state = "queued"
                elif msg.reach == "drop" and msg.interval_s is not None:
                    digest_state = "dropped"
                cur = self.db.execute(
                    "INSERT OR IGNORE INTO rumors (id, wrapper_id, sender, kind, created_at,"
                    " conversation, content, reply_to, plane, reach, authority, rule, why,"
                    " received_at, cursor, digest_rule, digest_state, digest_batch,"
                    " blocked_reason, route, mode)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        msg.id, msg.wrapper_id, msg.sender, msg.kind, msg.created_at,
                        msg.conversation, msg.content, msg.reply_to, msg.plane,
                        msg.reach, msg.authority, msg.rule, msg.why, stamp,
                        cursor, digest_rule, digest_state, None, msg.blocked_reason,
                        json.dumps(msg.route) if msg.route else None, msg.mode,
                    ),
                )
                if cur.rowcount == 0:
                    continue  # the id was already stored: a duplicate, not an error
                results[position] = True
                max_used = max(max_used, cursor)
                if msg.cursor is None:
                    next_rumor = max(next_rumor, cursor + 1)
                msg.cursor = cursor
                msg.digest_rule = digest_rule
                msg.digest_state = digest_state
                if digest_rule and (msg.reach == "digest" or digest_state == "dropped"):
                    self._ensure_digest_rule(digest_rule, int(msg.interval_s or 900), stamp)
                # Attachments are part of the message, so they share its transaction: a
                # message that survives without its attachment metadata would show the
                # agent a photo it can never ask for.
                for attachment_position, entry in enumerate(msg.attachments or []):
                    self.db.execute(
                        "INSERT INTO attachments (rumor_id, position, sha256, url, mime,"
                        " size, dim, alt) VALUES (?,?,?,?,?,?,?,?)"
                        " ON CONFLICT(rumor_id, position) DO NOTHING",
                        (
                            msg.id, attachment_position,
                            (entry.get("sha256") or None), entry.get("url"),
                            entry.get("mime"), entry.get("size"), entry.get("dim"),
                            entry.get("alt"),
                        ),
                    )
            new_next = max(next_rumor, max_used + 1)
            if new_next != int(cursor_row["next_rumor"]):
                self.db.execute(
                    "UPDATE delivery_cursor SET next_rumor = ? WHERE identity = ?",
                    (new_next, self.identity),
                )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return results

    def _ensure_digest_rule(self, rule: str, interval_s: int, now: int) -> None:
        interval_s = max(1, int(interval_s))
        window_start = (now // interval_s) * interval_s
        self.db.execute(
            "INSERT INTO digest_rules (identity, rule, interval_s, window_start)"
            " VALUES (?,?,?,?) ON CONFLICT(identity, rule) DO UPDATE SET"
            " interval_s = excluded.interval_s",
            (self.identity, rule, interval_s, window_start),
        )

    def mark_delivered(self, ids: list[str], *, reason: str | None = None) -> None:
        """Mark individual rumors delivered, and whole digest batches when the id is one.

        An adapter that knows only ``type: message`` acks ``id``; for a digest frame
        that id is ``digests.id``, so the batch is the unit of delivery. The batch row is
        closed only once every ``batched`` rumor in it is delivered — a partial id-level
        ack leaves it open, so replay can still offer what is left. A second ack is a
        no-op: ``COALESCE`` keeps the first timestamp.

        ``reason`` records a delivery that ended without a turn (`superseded`, when the
        adapter filed a shaped replay row as context). It is a closed vocabulary and is
        kept apart from ``blocked_reason``: a superseded message was accepted and remains
        in `history`, so it must not appear in `drops` as a refusal. A reason outside the
        vocabulary is discarded — the row is still delivered, with no ``settled_reason`` —
        and named at DEBUG, so a mis-acked reason is visible instead of silent.
        """
        ids = [i for i in ids if i]
        if not ids:
            return
        settled = str(reason) if reason in SETTLED_REASONS else None
        if reason is not None and settled is None:
            # The vocabulary is closed on purpose: an unknown reason cannot label a row, and
            # a typo must not turn a delivery into a refusal. Saying so at DEBUG is what keeps
            # the discard deliberate rather than invisible.
            log.debug(
                "ack reason %r is not a settled reason: recording %s delivered with"
                " settled_reason NULL", reason, ", ".join(ids))
        now = int(time.time())
        whole: set[str] = set()
        touched: set[str] = set()
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.db.executemany(
                "UPDATE rumors SET delivered_at = COALESCE(delivered_at, ?),"
                " settled_reason = COALESCE(settled_reason, ?) WHERE id = ?",
                [(now, settled, i) for i in ids],
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

    def _replay_rows(self, limit: int, max_age_minutes: int, *,
                     newest_first: bool = False,
                     now: int | None = None) -> list[dict[str, Any]]:
        """Undelivered immediate rows inside the replay window, in replay order.

        One query for both answers. `pending_immediate` wants the oldest page, which is
        the order a replay has always promised; `replay_batch` wants the newest page and
        reverses it, because shaping must collapse a backlog to its recent end rather
        than spend the batch cap on its oldest messages.
        """
        stamp = int(time.time() if now is None else now)
        cutoff = stamp - max(int(max_age_minutes), 0) * 60
        order = "received_at DESC, rowid DESC" if newest_first else "received_at, rowid"
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE delivered_at IS NULL AND reach = 'immediate'"
            " AND authority != 'self' AND blocked_reason IS NULL"
            " AND received_at >= ? AND received_at >= ?"
            f" ORDER BY {order} LIMIT ?",
            (cutoff, self.replay_floor(), limit),
        ).fetchall()
        return [dict(row) for row in rows]

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

        This is the raw query: it says what is undelivered, not what should be a turn.
        `replay_batch` is the shaped answer a `subscribe` actually offers.
        """
        items = self._replay_rows(limit, max_age_minutes)
        self._with_attachments(items)
        return items

    def replay_batch(self, limit: int = 20, max_age_minutes: int = 120, *,
                     max_messages: int = 5, max_age_secs: int = 600,
                     now: int | None = None) -> list[dict[str, Any]]:
        """The rows a `subscribe` may replay, shaped so a batch costs few turns.

        `pending_immediate` answers "which rows are undelivered"; this answers "which of
        them should become a turn". It takes the *newest* `limit` rows inside the replay
        window and returns them oldest-first, because a backlog larger than the window
        must collapse to its recent end, not to its oldest page. `shape_replay` marks the
        ones a newer message, the age horizon or the batch cap leaves as context.

        A superseded decision is written to the row (`superseded_by`) in the same call.
        The decision, not the batch, is durable: if the adapter acks the newer row and
        then loses the ack of an older one, the older row is still context on the next
        `subscribe` instead of becoming a stale turn out of order.
        """
        stamp = int(time.time() if now is None else now)
        items = list(reversed(self._replay_rows(
            limit, max_age_minutes, newest_first=True, now=stamp)))
        self._with_attachments(items)
        shape_replay(items, max_messages=max_messages, max_age_secs=max_age_secs, now=stamp)
        self._record_superseded(items)
        return items

    def _record_superseded(self, rows: list[dict[str, Any]]) -> None:
        """Persist the shaping decision so it survives an ack order or a reconnect."""
        writes = [(str(row.get("superseded_by") or ""), str(row["id"]))
                  for row in rows
                  if row.get("superseded") and row.get("superseded_by") is not None]
        if not writes:
            return
        self._begin()
        try:
            self.db.executemany(
                "UPDATE rumors SET superseded_by = COALESCE(superseded_by, ?) WHERE id = ?",
                writes,
            )
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise

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

    # -- advertisement (kind 0 + kind 10304) ------------------------------
    def advertised(self, kind: int) -> dict[str, Any] | None:
        """The stored copy of one advertised kind, or None when it was never published."""
        row = self.db.execute(
            "SELECT * FROM advertised WHERE kind = ?", (int(kind),)).fetchone()
        if row is None:
            return None
        item = dict(row)
        try:
            parsed = json.loads(item.get("refused") or "{}")
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        item["refused"] = parsed if isinstance(parsed, dict) else {}
        return item

    def record_advertised(self, kind: int, event_id: str, content_hash: str,
                          published_at: int, refused: dict[str, str] | None = None) -> None:
        self.db.execute(
            "INSERT INTO advertised (kind, event_id, content_hash, published_at, refused)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(kind) DO UPDATE SET event_id = excluded.event_id,"
            " content_hash = excluded.content_hash, published_at = excluded.published_at,"
            " refused = excluded.refused",
            (int(kind), str(event_id), str(content_hash), int(published_at),
             json.dumps(dict(refused or {}), sort_keys=True)),
        )

    def advertised_retries(self, kind: int) -> dict[str, dict[str, Any]]:
        """Relays still owed this kind, with the backoff state of each.

        A row here means the last attempt failed for a *condition* -- a rate limit, an
        outage -- so this is a debt, never a refusal: the copy is retried once `next_at`
        has passed, and `attempts` bounds how often.
        """
        rows = self.db.execute(
            "SELECT relay, reason, attempts, next_at FROM advertised_retry WHERE kind = ?"
            " ORDER BY relay", (int(kind),)).fetchall()
        return {str(row["relay"]): {"reason": str(row["reason"]),
                                   "attempts": int(row["attempts"]),
                                   "next_at": int(row["next_at"])}
                for row in rows}

    def record_advertised_retry(self, kind: int, relay: str, reason: str,
                                attempts: int, next_at: int) -> None:
        """Remember one relay's transient failure and when its next attempt is due."""
        self.db.execute(
            "INSERT INTO advertised_retry (kind, relay, reason, attempts, next_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(kind, relay) DO UPDATE SET reason = excluded.reason,"
            " attempts = excluded.attempts, next_at = excluded.next_at",
            (int(kind), str(relay), str(reason), max(1, int(attempts)), int(next_at)),
        )

    def clear_advertised_retry(self, kind: int, relay: str | None = None) -> None:
        """Drop a relay's debt -- because it accepted the copy, or was refused outright."""
        if relay is None:
            self.db.execute("DELETE FROM advertised_retry WHERE kind = ?", (int(kind),))
        else:
            self.db.execute("DELETE FROM advertised_retry WHERE kind = ? AND relay = ?",
                            (int(kind), str(relay)))

    def advertised_all(self) -> list[dict[str, Any]]:
        rows = []
        for row in self.db.execute("SELECT * FROM advertised ORDER BY kind"):
            item = dict(row)
            try:
                parsed = json.loads(item.get("refused") or "{}")
            except (TypeError, json.JSONDecodeError):
                parsed = {}
            item["refused"] = parsed if isinstance(parsed, dict) else {}
            # Relays still owed this copy: a transient failure, not a refusal, so a reader
            # that only looked at `refused` would call a rate-limited relay done for good.
            item["retrying"] = self.advertised_retries(item["kind"])
            rows.append(item)
        return rows

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
        # `route` is stored as JSON text; a reader wants the mapping, not a string that has to
        # be decoded again in every caller (history, `hng recent`, a future status frame).
        for item in items:
            item["route"] = _json_or_none(item.get("route"))
        if with_attachments:
            self._with_attachments(items)
        return items

    def preceding(self, conversation: str, before_at: int, before_id: str,
                  limit: int = 20) -> list[dict[str, Any]]:
        """One conversation's messages that ended before an anchor, oldest first.

        The anchor is the triggering message itself, named as `(created_at, id)`: a
        strict total order, so the predicate excludes that row and every row after it
        even when two messages were stamped in the same second. `limit` bounds the page
        from the anchor backwards, which is the end a reader wants — the lines just
        before the trigger, not the room's first week.

        Age and authority are the caller's questions: a room's history is evidence like
        any other stored row. The conversation is the `concord:<community>:<channel>` id
        the plane wrote, never a sender, so a room's scrollback cannot leak into a DM.
        """
        rows = self.db.execute(
            "SELECT * FROM rumors WHERE conversation = ?"
            " AND (created_at < ? OR (created_at = ? AND id < ?))"
            " ORDER BY created_at DESC, id DESC LIMIT ?",
            (str(conversation), int(before_at), int(before_at), str(before_id),
             max(0, int(limit))),
        ).fetchall()
        items = [dict(row) for row in rows]
        # Same reason as `recent`: `route` is stored as JSON text and every reader wants
        # the mapping, not a string it has to decode again.
        for item in items:
            item["route"] = _json_or_none(item.get("route"))
        items.reverse()
        return items

    def rumor(self, event_id: str) -> dict[str, Any] | None:
        """One stored row by id, or None.

        A reply names the message it answers; the row is what knows which wrapper that
        message arrived in, and a Concord quote tag carries the wrapper id, not the rumor's.
        """
        row = self.db.execute(
            "SELECT * FROM rumors WHERE id = ?", (str(event_id or ""),)
        ).fetchone()
        return dict(row) if row is not None else None

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

    def planes_for_conversation(self, conversation: str) -> list[str]:
        """Which planes hold rows for *conversation*, most rows first.

        `hng route` walks in with a string and no live message, and a Marmot group id looks
        exactly like a peer pubkey: the plane is read from the evidence the store already has
        rather than guessed from the shape of the string. Empty means "never seen".
        """
        rows = self.db.execute(
            "SELECT plane, COUNT(*) AS n FROM rumors WHERE conversation = ?"
            " GROUP BY plane ORDER BY n DESC, plane ASC",
            (str(conversation or "").strip(),),
        ).fetchall()
        return [str(row["plane"]) for row in rows]

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

    # -- profiles (kind-0 `bot` claims) ----------------------------------
    def record_profile(self, *, pubkey: str, name: str | None = None, is_bot: bool = False,
                       event_id: str | None = None, now: int | None = None) -> None:
        """Remember what a peer's kind 0 claims, replacing an older claim.

        `bot` is stored as 0/1 rather than NULL so "read, and it said no" is a fact:
        the registry must not refetch a peer whose profile simply has no `bot` field.
        """
        stamp = int(time.time()) if now is None else int(now)
        self.db.execute(
            "INSERT INTO profiles (pubkey, name, bot, event_id, updated_at) VALUES (?,?,?,?,?)"
            " ON CONFLICT(pubkey) DO UPDATE SET name = COALESCE(excluded.name, name),"
            " bot = excluded.bot, event_id = COALESCE(excluded.event_id, event_id),"
            " updated_at = excluded.updated_at",
            (pubkey.strip().lower(), name, 1 if is_bot else 0, event_id, stamp),
        )

    def profile(self, pubkey: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM profiles WHERE pubkey = ?", (pubkey.strip().lower(),)
        ).fetchone()
        return dict(row) if row is not None else None

    def bot_peers(self) -> list[dict[str, Any]]:
        """Every peer we have read as a bot, newest claim first."""
        rows = self.db.execute(
            "SELECT * FROM profiles WHERE bot = 1 ORDER BY updated_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def profile_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) AS n FROM profiles").fetchone()["n"])

    def recent_outbound(self, conversation: str, since: int, *,
                        limit: int = 50) -> list[dict[str, Any]]:
        """Our own text to one conversation since `since`, newest first.

        Echo suppression reads this rather than a process-local buffer: the same loop
        can continue across a restart, and `hng send` writes the same rows as the
        daemon, so both are covered by one rule.
        """
        rows = self.db.execute(
            "SELECT id, content, received_at FROM rumors WHERE authority = 'self'"
            " AND reach = 'sent' AND conversation = ? AND received_at >= ?"
            " ORDER BY received_at DESC LIMIT ?",
            (conversation, int(since), max(1, int(limit))),
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

    def concord_invites(self, limit: int = 20, *,
                        status: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM concord_invites"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY received_at DESC LIMIT ?"
        params.append(max(1, int(limit)))
        rows = self.db.execute(sql, params).fetchall()
        return [_invite_row(row) for row in rows]

    def concord_invite(self, ident: str) -> dict[str, Any] | None:
        """One invite, by its wrap id or by its community id (the newest for that community)."""
        row = self.db.execute("SELECT * FROM concord_invites WHERE id = ?",
                              (str(ident),)).fetchone()
        if row is None:
            row = self.db.execute(
                "SELECT * FROM concord_invites WHERE community_id = ?"
                " ORDER BY received_at DESC LIMIT 1", (str(ident),)
            ).fetchone()
        return _invite_row(row) if row is not None else None

    def concord_joined(self, community_id: str) -> bool:
        """Have we joined this community? Then the next invite needs nobody's word.

        Only `joined` counts: a row left `joining` by a crash says we tried and cannot say
        the join landed, so the next invite for that community may publish one.
        """
        row = self.db.execute(
            "SELECT 1 FROM concord_invites WHERE community_id = ? AND status = 'joined'"
            " LIMIT 1", (str(community_id),),
        ).fetchone()
        return row is not None

    def decide_invite(self, invite_id: str, *, status: str, why: str = "",
                      decided_by: str = "") -> bool:
        """Record what happened to an invite: joined, failed, joining or held.

        An update, never an insert: the row is written when the invite verifies, so a
        decision for an id this store never recorded is a caller's bug and answers False
        rather than inventing an invite.
        """
        if status not in INVITE_STATES:
            raise ValueError(f"bad invite status: {status!r}")
        cur = self.db.execute(
            "UPDATE concord_invites SET status = ?, why = ?, decided_at = ?, decided_by = ?"
            " WHERE id = ?",
            (status, why, int(time.time()), decided_by, str(invite_id)),
        )
        return cur.rowcount > 0

    def note_invite_announced(self, invite_id: str) -> bool:
        """The question reached the control pane, so a restart does not ask it again."""
        cur = self.db.execute("UPDATE concord_invites SET announced_at = ? WHERE id = ?",
                              (int(time.time()), str(invite_id)))
        return cur.rowcount > 0

    # -- Concord channels the operator has been told about --------------
    def claim_concord_channels(self, community_id: str,
                               channels: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
        """Claim the channel ids in this community that have not been announced yet.

        One row per (community, channel id) is the record a restart is checked against: the
        Control plane is re-read, the same ids come back, the row is found, and the notice
        stays unsent. Claiming *before* the send is what keeps two refreshes racing each
        other to one notice; `release_concord_channels` gives a failed send's claims back,
        so the next refresh retries instead of losing the notice.
        """
        now = int(time.time())
        claimed: list[tuple[str, str]] = []
        for channel_id, name in channels:
            ident = str(channel_id or "").strip()
            if not ident:
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO concord_announced"
                " (community_id, channel_id, name, announced_at) VALUES (?,?,?,?)",
                (str(community_id), ident, str(name or ""), now),
            )
            if cur.rowcount > 0:
                claimed.append((ident, str(name or "")))
        return claimed

    def release_concord_channels(self, community_id: str,
                                 channel_ids: Iterable[str]) -> None:
        """Give back claims whose notice never left: the next refresh announces them."""
        ids = [str(ident) for ident in channel_ids if str(ident or "").strip()]
        if not ids:
            return
        marks = ",".join("?" for _ in ids)
        self.db.execute(
            "DELETE FROM concord_announced WHERE community_id = ?"
            f" AND channel_id IN ({marks})",
            (str(community_id), *ids),
        )

    # -- the ngit bridge -------------------------------------------------

    def ngit_link(self, repo: str, kind: int, source_id: str) -> dict[str, Any] | None:
        """What this issue or PR became, or None when the bridge has never seen it."""
        row = self.db.execute(
            "SELECT * FROM ngit_links WHERE repo = ? AND kind = ? AND source_id = ?",
            (str(repo), int(kind), str(source_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def ngit_link_for_task(self, task_id: str, kind: int | None = None
                           ) -> dict[str, Any] | None:
        """The newest link this card holds, optionally of one kind.

        A card can hold more than one: a work card began as an issue and can also be a
        PR's review card, so the newest row is not necessarily the one a caller means.
        `kind` says which; without it the newest is what a caller that wants "the card's
        link" gets.
        """
        sql = "SELECT * FROM ngit_links WHERE task_id = ?"
        args: list[Any] = [str(task_id)]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(int(kind))
        sql += " ORDER BY updated_at DESC LIMIT 1"
        row = self.db.execute(sql, tuple(args)).fetchone()
        return dict(row) if row is not None else None

    def ngit_link_for_source(self, kind: int, source_id: str) -> dict[str, Any] | None:
        row = self.db.execute(
            "SELECT * FROM ngit_links WHERE kind = ? AND source_id = ?"
            " ORDER BY updated_at DESC LIMIT 1", (int(kind), str(source_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def ngit_links(self, repo: str, kind: int) -> list[dict[str, Any]]:
        """Every link this repo has for one kind, oldest first.

        The label walk needs the PRs a poll did not carry: a review task can be dispatched,
        and finish, between two passes, and the PR's own events may all predate both.
        """
        rows = self.db.execute(
            "SELECT * FROM ngit_links WHERE repo = ? AND kind = ? ORDER BY created_at, source_id",
            (str(repo), int(kind)),
        ).fetchall()
        return [dict(row) for row in rows]

    def ngit_link_save(self, repo: str, kind: int, source_id: str, *, state: str,
                       task_id: str | None = None, board: str = "",
                       detail: dict[str, Any] | None = None) -> None:
        """Record the link, or move an existing one to a new state.

        A new row keeps the state it is given; an existing row only has its state and note
        replaced when one is supplied, so a caller that knows the task id cannot lose it.
        """
        now = int(time.time())
        note = json.dumps(detail, separators=(",", ":"), sort_keys=True) if detail else None
        existing = self.ngit_link(repo, kind, source_id)
        if existing is None:
            self.db.execute(
                "INSERT INTO ngit_links (repo, kind, source_id, task_id, board, state,"
                " detail, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (str(repo), int(kind), str(source_id), task_id, board or None, str(state),
                 note, now, now),
            )
            return
        self.db.execute(
            "UPDATE ngit_links SET state = ?, task_id = COALESCE(?, task_id),"
            " board = COALESCE(NULLIF(?, ''), board),"
            " detail = COALESCE(?, detail), updated_at = ?"
            " WHERE repo = ? AND kind = ? AND source_id = ?",
            (str(state), task_id, board, note, now, str(repo), int(kind), str(source_id)),
        )

    def ngit_link_state(self, repo: str, kind: int, source_id: str, state: str) -> bool:
        cur = self.db.execute(
            "UPDATE ngit_links SET state = ?, updated_at = ?"
            " WHERE repo = ? AND kind = ? AND source_id = ?",
            (str(state), int(time.time()), str(repo), int(kind), str(source_id)),
        )
        return cur.rowcount > 0

    def ngit_roots(self, repo: str) -> tuple[str, ...]:
        """Every issue and PR event id the bridge has linked for this repository.

        The watcher's comment subscription cannot name a repository -- a NIP-22 comment
        carries no `a` tag, only the root event it replies to -- so these ids are how a
        comment is attributed to a channel. Read-only, and deliberately so: the pass owns
        the table, this only asks it what a comment could refer to.
        """
        rows = self.db.execute(
            "SELECT source_id FROM ngit_links WHERE repo = ? AND kind IN (?, ?)",
            (str(repo), 1621, 1618),
        ).fetchall()
        return tuple(str(row[0]) for row in rows if row and row[0])

    def ngit_mention(self, repo: str, comment_id: str) -> dict[str, Any] | None:
        """What this gateway has already decided about one comment that mentioned it."""
        row = self.db.execute(
            "SELECT * FROM ngit_mentions WHERE repo = ? AND comment_id = ?",
            (str(repo), str(comment_id)),
        ).fetchone()
        return dict(row) if row is not None else None

    def ngit_mention_save(self, repo: str, comment_id: str, *, task_id: str = "",
                          detail: Any = None) -> bool:
        """Remember one mention. `task_id` empty records a mention deliberately not answered."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO ngit_mentions (repo, comment_id, task_id, detail,"
            " created_at) VALUES (?,?,?,?,?)",
            (str(repo), str(comment_id), str(task_id or ""),
             json.dumps(detail or {}, sort_keys=True), int(time.time())),
        )
        return cur.rowcount > 0

    def ngit_comment_seen(self, repo: str, source_id: str, comment_id: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM ngit_comments WHERE repo = ? AND source_id = ? AND comment_id = ?",
            (str(repo), str(source_id), str(comment_id)),
        ).fetchone()
        return row is not None

    def ngit_note_comment(self, repo: str, source_id: str, comment_id: str,
                          task_id: str) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO ngit_comments (repo, source_id, comment_id, task_id,"
            " created_at) VALUES (?,?,?,?,?)",
            (str(repo), str(source_id), str(comment_id), str(task_id), int(time.time())),
        )
        return cur.rowcount > 0

    def ngit_comment_count(self, repo: str, source_id: str) -> int:
        row = self.db.execute(
            "SELECT COUNT(*) AS n FROM ngit_comments WHERE repo = ? AND source_id = ?",
            (str(repo), str(source_id)),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def ngit_forget_comments(self, repo: str, source_id: str) -> int:
        """Drop the copied-comment record for one issue: its task was recreated, so the
        comments have to be copied onto the new one."""
        cur = self.db.execute(
            "DELETE FROM ngit_comments WHERE repo = ? AND source_id = ?",
            (str(repo), str(source_id)),
        )
        return cur.rowcount

    def ngit_cursor(self, repo: str, stream: str) -> str | None:
        row = self.db.execute(
            "SELECT value FROM ngit_cursors WHERE repo = ? AND stream = ?",
            (str(repo), str(stream)),
        ).fetchone()
        return str(row["value"]) if row is not None else None

    def ngit_set_cursor(self, repo: str, stream: str, value: str | int) -> None:
        self.db.execute(
            "INSERT INTO ngit_cursors (repo, stream, value, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(repo, stream) DO UPDATE SET value = excluded.value,"
            " updated_at = excluded.updated_at",
            (str(repo), str(stream), str(value), int(time.time())),
        )

    # -- the ngit outbox -------------------------------------------------

    def ngit_notice_put(self, repo: str, kind: int, source_id: str, notice: str, *,
                        conversation: str, text: str) -> bool:
        """Queue one notice. False when it was already queued or already sent: deciding
        the same thing twice must not put it in the channel twice."""
        now = int(time.time())
        cur = self.db.execute(
            "INSERT OR IGNORE INTO ngit_outbox (repo, kind, source_id, notice, conversation,"
            " text, state, created_at, updated_at) VALUES (?,?,?,?,?,?,'queued',?,?)",
            (str(repo), int(kind), str(source_id), str(notice), str(conversation), str(text),
             now, now),
        )
        return cur.rowcount > 0

    def ngit_notice_due(self, repo: str, limit: int) -> list[dict[str, Any]]:
        """Queued notices in the order they were decided, oldest first."""
        rows = self.db.execute(
            "SELECT * FROM ngit_outbox WHERE repo = ? AND state = 'queued'"
            " ORDER BY id ASC LIMIT ?", (str(repo), max(0, int(limit))),
        ).fetchall()
        return [dict(row) for row in rows]

    def ngit_notice_queued(self, repo: str, source_id: str) -> bool:
        """Whether this event still has a notice waiting for the daemon."""
        row = self.db.execute(
            "SELECT 1 FROM ngit_outbox WHERE repo = ? AND source_id = ? AND state = 'queued'"
            " LIMIT 1", (str(repo), str(source_id)),
        ).fetchone()
        return row is not None

    def ngit_stranded(self, repo: str, kind: int) -> list[dict[str, Any]]:
        """Links that say `pending` with no notice anywhere: the queue lost the row, so the
        notice has to be made again or the event is stuck for ever."""
        rows = self.db.execute(
            "SELECT l.* FROM ngit_links l WHERE l.repo = ? AND l.kind = ? AND l.state = 'pending'"
            " AND NOT EXISTS (SELECT 1 FROM ngit_outbox o"
            "                 WHERE o.repo = l.repo AND o.source_id = l.source_id)",
            (str(repo), int(kind)),
        ).fetchall()
        return [dict(row) for row in rows]

    def ngit_notice_count(self, repo: str, *, kind: int | None = None) -> int:
        sql = "SELECT COUNT(*) AS n FROM ngit_outbox WHERE repo = ? AND state = 'queued'"
        args: list[Any] = [str(repo)]
        if kind is not None:
            sql += " AND kind = ?"
            args.append(int(kind))
        row = self.db.execute(sql, args).fetchone()
        return int(row["n"] if row is not None else 0)

    def ngit_watched(self, repo: str) -> bool:
        """Whether the bridge has dealt with this repo before.

        A cursor missing from a repo the bridge has already written to was deleted by hand:
        that is a reset, and starting it from the lookback floor would announce a week of
        PRs the channel never heard about. A repo with nothing in it has never been read,
        and there the configured floor is what an operator asked for.
        """
        for table in ("ngit_links", "ngit_outbox"):
            row = self.db.execute(
                f"SELECT 1 FROM {table} WHERE repo = ? LIMIT 1", (str(repo),)).fetchone()
            if row is not None:
                return True
        return False

    def ngit_notice_sent(self, repo: str, kind: int, source_id: str, notice: str, *,
                         state: str) -> None:
        """The daemon took the notice: record the receipt and move the link together.

        One transaction, because a receipt with a link that still says `pending` is a row
        the next poll would try to queue again, and the unique key would silently refuse it.

        `state` empty is a notice that says nothing about the PR's lifecycle -- a verdict
        line: the event it describes is already on the PR, and the link state it would write
        (`signalled`) belongs to the announcement, not to a message about a review.
        """
        now = int(time.time())
        with self.db:
            self.db.execute(
                "UPDATE ngit_outbox SET state = 'sent', attempts = attempts + 1,"
                " error = NULL, updated_at = ? WHERE repo = ? AND source_id = ? AND notice = ?",
                (now, str(repo), str(source_id), str(notice)),
            )
            if not str(state or ""):
                return
            self.db.execute(
                "UPDATE ngit_links SET state = ?, updated_at = ?"
                " WHERE repo = ? AND kind = ? AND source_id = ?",
                (str(state), now, str(repo), int(kind), str(source_id)),
            )

    def ngit_notice_failed(self, repo: str, source_id: str, notice: str, error: str) -> int:
        """Count one attempt that did not land and remember why. The row stays queued."""
        cur = self.db.execute(
            "UPDATE ngit_outbox SET attempts = attempts + 1, error = ?, updated_at = ?"
            " WHERE repo = ? AND source_id = ? AND notice = ?",
            (str(error)[:300], int(time.time()), str(repo), str(source_id), str(notice)),
        )
        return cur.rowcount

    def ngit_notice_drop(self, repo: str, source_id: str) -> int:
        """Forget this PR's unsent notices: a merge makes an unsent review line redundant."""
        cur = self.db.execute(
            "DELETE FROM ngit_outbox WHERE repo = ? AND source_id = ? AND state = 'queued'",
            (str(repo), str(source_id)),
        )
        return cur.rowcount

    def ngit_notice_forget(self, repo: str, source_id: str) -> int:
        """Drop every notice for one event, sent or not.

        A task recreated by hand is a different task with a different id, so the notice that
        named the old one has to go: leaving the receipt behind would make the new notice a
        duplicate of a message about something that no longer exists.
        """
        cur = self.db.execute(
            "DELETE FROM ngit_outbox WHERE repo = ? AND source_id = ?",
            (str(repo), str(source_id)),
        )
        return cur.rowcount

    # -- send idempotency -------------------------------------------------

    def send_key(self, key: str) -> dict[str, Any] | None:
        """The message a caller's idempotency key already produced, if it produced one."""
        if not key:
            return None
        row = self.db.execute("SELECT * FROM send_keys WHERE key = ?", (str(key),)).fetchone()
        return dict(row) if row is not None else None

    def note_send_key(self, key: str, message_id: str, conversation: str = "") -> None:
        """Remember that this key posted that message, so a repeat of the request is a
        repeat of the answer rather than a second message."""
        if not key or not message_id:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO send_keys (key, message_id, conversation, created_at)"
            " VALUES (?,?,?,?)",
            (str(key), str(message_id), str(conversation), int(time.time())),
        )

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
        invites = self.db.execute(
            "SELECT COUNT(*) AS n, SUM(status = 'held') AS held FROM concord_invites"
        ).fetchone()
        media = self.db.execute(
            "SELECT COUNT(*) AS n,"
            " COUNT(DISTINCT CASE WHEN local_path IS NOT NULL THEN sha256 END) AS cached"
            " FROM attachments"
        ).fetchone()
        return {"total": row["total"], "pending": row["pending"] or 0,
                "blocked": row["blocked"] or 0,
                "invites": invites["n"], "invites_held": invites["held"] or 0,
                "relays": self.relay_report(),
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
        """Close every connection this Store opened, in any thread.

        A writer's thread is stopped (and its queued rows flushed) before this is
        called: closing a connection another thread is mid-transaction on would lose
        that transaction.
        """
        with self._conn_lock:
            connections, self._connections = self._connections, []
        for conn in connections:
            try:
                conn.close()
            except sqlite3.Error:  # pragma: no cover - already closed
                pass
        self._local = threading.local()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def dumps(frame: dict[str, Any]) -> str:
        return json.dumps(frame, separators=(",", ":"), sort_keys=True)
