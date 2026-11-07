"""`done` requires `applied`: the board itself refuses to finish a PR card early.

`hermes kanban complete` is the worker's word, and until this module existed it was also
the only word: a card whose PR was still open could be closed by whoever held it, and the
board then reported work as finished that a human still had to merge.

The kernel has no completion hook a bridge can register, and the gateway is a separate
distribution, so the guard is a **trigger on the board's own `tasks` table** -- installed
next to the rows this bridge already writes. It is deliberately narrow:

- one row per guarded card in `ngit_pr_guards`, carrying the PR, its state, the tip it is
  open at, and the merge commit once one is seen;
- one `BEFORE UPDATE OF status` trigger per guarded card, whose message is the refusal a
  person reads (`PR <short> is open at <tip> -- a merge is the only thing ...`). SQLite's
  `RAISE(ABORT, ...)` takes a literal only, which is why the message is baked into the
  trigger and the trigger is re-made whenever the state or the tip moves;
- no trigger at all once the PR is `applied`, so the merge path completes normally.

The board is the only place both completion paths meet: the CLI's `complete_task` and the
agent's `kanban_complete` tool both write `tasks.status = 'done'`, so one trigger covers
both without patching the kernel. A trigger that aborts surfaces as a SQLite error; the
tool path renders it as a structured tool error, and the CLI exits non-zero with the
refusal on stderr.

Nothing here decides *policy*: `ngit.Sync` decides when a card is open, applied or closed,
and calls `guard` / `release` / `close`. This module only writes the rows and the trigger.
"""

from __future__ import annotations

import re
import sqlite3
import time
from typing import Any, Optional

#: The guard rows. A row is the bridge's own note, not a copy of `ngit_links`: the link is
#: the PR's lifecycle in the gateway store, this is what the board must refuse to close.
TABLE = "ngit_pr_guards"

#: Trigger names are derived from the task id (`t_` + 8 hex), which is the kernel's own
#: shape and safe to embed. A prefix keeps them findable and out of the kernel's namespace.
TRIGGER_PREFIX = "ngit_pr_done_"

OPEN = "open"
APPLIED = "applied"
CLOSED = "closed"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    task_id      TEXT PRIMARY KEY,
    pr_id        TEXT NOT NULL,
    state        TEXT NOT NULL,
    tip          TEXT,
    merge_commit TEXT,
    detail       TEXT,
    updated_at   INTEGER NOT NULL
)
"""


def _q(value: Any) -> str:
    """One SQL string literal. Everything embedded is the bridge's own hex or a task id;
    doubling a quote is the belt-and-braces rule, not a parser."""
    return "'" + str(value or "").replace("'", "''") + "'"


def _trigger(task_id: str) -> str:
    """The trigger's name: the prefix plus a SQL-identifier-safe task id.

    Task ids are the kernel's `t_` + hex, but a board can be written by hand; a name that
    cannot be spelled as an identifier would make the guard fail on a card nobody can fix.
    """
    return f"{TRIGGER_PREFIX}{re.sub(r'[^A-Za-z0-9_]', '_', str(task_id or ''))}"


def refusal(pr_id: str, state: str, tip: str = "", detail: str = "") -> str:
    """The sentence a refused completion carries: what the state actually is.

    It names the PR and the tip, because "completion refused" without either is the generic
    error this rule exists to replace. `closed` names the event that closed it: the card is
    waiting for a person, not for a merge. The last clause tells a reviewer the one terminal
    action that is still open to it -- the review lane's own tool (`kanban_complete`) is
    exactly the one this guard refuses.
    """
    short = str(pr_id or "")[:8] or "unknown"
    if state == CLOSED:
        by = f" by event {str(detail)[:16]}" if detail else ""
        return (f"PR {short} was closed without a merge{by} -- a human must decide; "
                "a closed PR does not finish a card")
    at = f" at {str(tip)[:12]}" if tip else ""
    return (f"PR {short} is open{at} -- a merge is the only thing that completes a PR card."
            " A reviewer parks the card with kanban_block; nobody else completes it.")


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_SCHEMA)


def guard(conn: sqlite3.Connection, task_id: str, *, pr_id: str, state: str,
          tip: str = "", merge_commit: str = "", detail: str = "") -> None:
    """Record the card's PR state and (re)make the trigger that enforces it.

    Called on every pass for an open PR, so a moved tip updates the refusal line. Applied
    removes the trigger instead: a merged PR has nothing left to refuse.
    """
    task_id, pr_id = str(task_id or ""), str(pr_id or "")
    if not task_id or not pr_id:
        return
    ensure_schema(conn)
    conn.execute(
        f"INSERT OR REPLACE INTO {TABLE}"
        " (task_id, pr_id, state, tip, merge_commit, detail, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (task_id, pr_id, str(state), str(tip or ""), str(merge_commit or ""),
         str(detail or ""), int(time.time())),
    )
    conn.execute(f"DROP TRIGGER IF EXISTS {_trigger(task_id)}")
    if state == APPLIED:
        return
    message = _q(refusal(pr_id, state, tip, detail))
    conn.execute(
        f"CREATE TRIGGER {_trigger(task_id)} BEFORE UPDATE OF status ON tasks"
        " WHEN NEW.status = 'done' AND OLD.status != 'done'"
        f" AND OLD.id = {_q(task_id)}"
        f" BEGIN SELECT RAISE(ABORT, {message}); END"
    )


def release(conn: sqlite3.Connection, task_id: str, *, merge_commit: str = "") -> None:
    """The PR is applied: drop the refusal so the merge path can finish the card."""
    row = _row(conn, task_id)
    if row is None:
        return
    guard(conn, task_id, pr_id=str(row["pr_id"]), state=APPLIED,
          tip=str(row["tip"] or ""), merge_commit=str(merge_commit or ""),
          detail=str(row["detail"] or ""))


def close(conn: sqlite3.Connection, task_id: str, *, event_id: str = "") -> None:
    """The PR was closed without a merge: refuse completion and name the closing event."""
    row = _row(conn, task_id)
    if row is None:
        return
    guard(conn, task_id, pr_id=str(row["pr_id"]), state=CLOSED,
          tip=str(row["tip"] or ""), detail=str(event_id or ""))


def forget(conn: sqlite3.Connection, task_id: str) -> None:
    """Remove the guard and its trigger (a card that was deleted or archived)."""
    if not task_id:
        return
    try:
        conn.execute(f"DROP TRIGGER IF EXISTS {_trigger(str(task_id))}")
        conn.execute(f"DELETE FROM {TABLE} WHERE task_id = ?", (str(task_id),))
    except sqlite3.Error:
        return


def prune(conn: sqlite3.Connection) -> int:
    """Drop guards whose card is gone or finished; returns how many were removed.

    A guard outliving its card would be a row nobody reads; a guard on a `done` card would
    re-refuse a completion the merge already allowed. Both are the pass's debris, cleared
    on the next run.
    """
    try:
        rows = conn.execute(f"SELECT task_id FROM {TABLE}").fetchall()
    except sqlite3.Error:
        return 0
    removed = 0
    for row in rows:
        task_id = str(row[0])
        found = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if found is None or str(found[0]) in ("done", "archived"):
            forget(conn, task_id)
            removed += 1
    return removed


def _row(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    try:
        ensure_schema(conn)
        row = conn.execute(f"SELECT * FROM {TABLE} WHERE task_id = ?",
                           (str(task_id),)).fetchone()
    except sqlite3.Error:
        return None
    return dict(row) if row is not None else None


def state_of(conn: sqlite3.Connection, task_id: str) -> Optional[dict[str, Any]]:
    """The guard on one card, for a caller that wants to report the state it saw."""
    return _row(conn, task_id)
