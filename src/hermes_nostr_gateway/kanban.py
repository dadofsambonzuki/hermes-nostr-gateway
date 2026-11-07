"""A board file, written directly: create a task, comment on it, promote it.

The ngit bridge runs outside the Hermes process (`hng ngit sync` under cron), so it cannot
call the kanban tools and does not import the kernel. What it needs is small and exact:
create a task the dispatcher will pick up, append a comment in order, and release a held
task. This module writes the same rows the kernel writes -- the same ``t_`` + 8 hex ids,
the documented statuses, and the project-anchored worktree and branch a project-scoped
board gives a task -- and nothing else. The proof that it matches is a test that opens the
file with sqlite and reads the row back.

Every method is idempotent against the database, never against memory: a cron run that
dies halfway leaves state the next run can see, and no run decides anything from a value
it holds only in this process.

A board is not created here. Hermes makes a board; a bridge that invented one would be a
second owner of the same state, and its ``board.json`` would be a guess.
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .projects import load_registry

#: The branch the kernel derives for a project-linked task: `<slug>/<task-id>[-<title>]`.
#: Duplicated rather than imported because the kernel is a separate distribution; the
#: characters are the same set `hermes_cli.projects_db` allows.
_BRANCH_SAFE = re.compile(r"[^a-z0-9._-]+")

#: Statuses the default workflow defines, plus the archive column.
READY = "ready"
BLOCKED = "blocked"
DONE = "done"


class BoardError(RuntimeError):
    """The file is not a board, or a write could not be made."""


def new_task_id() -> str:
    """`t_` + 4 hex bytes, the kernel's own shape (idempotency is the key's job)."""
    return "t_" + secrets.token_hex(4)


def branch_name(slug: str, task_id: str, title: str = "") -> str:
    base = f"{slug or 'project'}/{task_id}"
    tail = _BRANCH_SAFE.sub("-", (title or "").strip().lower()).strip("-")[:40].strip("-")
    return f"{base}-{tail}" if tail else base


@dataclass(frozen=True)
class Anchor:
    """What a project-bound board gives a task: a repository, a branch, a project id."""

    project_id: str = ""
    slug: str = ""
    repo: str = ""

    @property
    def worktree(self) -> bool:
        return bool(self.project_id and self.repo)

    @classmethod
    def for_board(cls, home: Path, board_slug: str, registry=None) -> "Anchor":
        """The project a board is bound to, read from Hermes' own state.

        An unbound board answers an empty anchor, and the task is created scratch: better a
        task the dispatcher can still run than one pointing at a worktree nobody will make.
        """
        reg = registry if registry is not None else load_registry(home)
        record = reg.board(board_slug)
        if not record:
            return cls()
        project = reg.by_id(str(record.get("project_id") or ""))
        if project is None:
            return cls()
        return cls(project_id=str(project.get("id") or ""),
                   slug=str(project.get("slug") or ""),
                   repo=str(project.get("primary_path") or ""))


class Board:
    """One board database, opened for the writes this bridge needs."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise BoardError(f"no board database at {self.path}")
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=5.0)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=5000")
        names = {row["name"] for row in self.db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
        missing = {"tasks", "task_comments"} - names
        if missing:
            raise BoardError(f"{self.path} has no {', '.join(sorted(missing))}: "
                             "not a kanban board")

    # -- reads -----------------------------------------------------------

    def task(self, task_id: str) -> Optional[dict[str, Any]]:
        if not task_id:
            return None
        row = self.db.execute("SELECT * FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
        return dict(row) if row is not None else None

    def by_idempotency(self, key: str) -> Optional[dict[str, Any]]:
        if not key:
            return None
        row = self.db.execute(
            "SELECT * FROM tasks WHERE idempotency_key = ? AND status != 'archived'"
            " ORDER BY created_at DESC LIMIT 1", (str(key),),
        ).fetchone()
        return dict(row) if row is not None else None

    def review_task(self, pr_id: str) -> Optional[dict[str, Any]]:
        """The newest task on this board that reviews this PR, or None.

        Two things identify one. The bridge's own tasks carry `ngit:pr:<event id>` (and a
        per-tip key would extend it), which is exact. A re-review card made by hand is not
        the bridge's to key, so it is recognised by the shape the review brief gives it: a
        title of `Review PR <first 8>: ...` and the PR's event id somewhere in the body,
        which is the one line the brief cannot leave out.

        Newest first, because a verdict covers the revision it was given for: a later card
        is a later review of the same PR, and its verdict is the one in force.
        """
        pr_id = str(pr_id or "")
        if not pr_id:
            return None
        row = self.db.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"
            " AND (idempotency_key = ? OR idempotency_key LIKE ?"
            "      OR (title LIKE ? AND body LIKE ?))"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (f"ngit:pr:{pr_id}", f"ngit:pr:{pr_id}:%",
             f"Review PR {pr_id[:8]}:%", f"%{pr_id}%"),
        ).fetchone()
        return dict(row) if row is not None else None

    def comments(self, task_id: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT * FROM task_comments WHERE task_id = ? ORDER BY created_at ASC, id ASC",
            (str(task_id),),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- writes ----------------------------------------------------------

    def create(self, *, title: str, body: str, assignee: str, status: str,
               idempotency_key: str = "", created_by: str = "ngit",
               anchor: Optional[Anchor] = None, priority: int = 0) -> str:
        """Insert one task and return its id.

        A task that already carries this idempotency key is returned instead of duplicated,
        which is what makes a re-poll harmless. Status is written as given: the dispatcher
        claims `ready` ones, and a `blocked` task is waiting for a human, not for a crash.

        Nothing is written to `model_override` / `provider_override`: the bridge does not
        choose a model for the work it files. The board's default, then the assignee
        profile's own model, decides -- and an operator who wants a single task elsewhere
        says so on that task (`hermes kanban set-model`).
        """
        key = (idempotency_key or "").strip()
        if key:
            found = self.by_idempotency(key)
            if found is not None:
                return str(found["id"])
        if status not in (READY, BLOCKED, "todo", "triage"):
            raise BoardError(f"refusing to create a task in status {status!r}")

        now = int(time.time())
        spot = anchor or Anchor()
        workspace_kind, workspace_path = "scratch", None
        project_id = None
        if spot.worktree:
            # The same anchoring the kernel applies, so the dispatcher finds the worktree
            # and branch it expects instead of inventing a fallback for a project task.
            workspace_kind = "worktree"
            workspace_path = str(Path(spot.repo) / ".worktrees" / "{id}")
            project_id = spot.project_id
        block_kind = "needs_input" if status == BLOCKED else None

        row: dict[str, Any] = {
            "id": None, "title": title.strip()[:500], "body": body,
            "assignee": assignee or None, "status": status, "priority": int(priority or 0),
            "created_by": created_by, "created_at": now, "workspace_kind": workspace_kind,
            "workspace_path": None, "branch_name": None, "project_id": project_id,
            "idempotency_key": key or None, "block_kind": block_kind,
        }
        statement = (f"INSERT INTO tasks ({', '.join(row)})"
                     f" VALUES ({', '.join(['?'] * len(row))})")

        for _ in range(2):
            task_id = new_task_id()
            row["id"] = task_id
            row["workspace_path"] = (workspace_path.format(id=task_id)
                                     if workspace_path else None)
            row["branch_name"] = (branch_name(spot.slug, task_id, title)
                                  if spot.worktree else None)
            try:
                self.db.execute(statement, tuple(row.values()))
            except sqlite3.IntegrityError:
                continue
            except sqlite3.Error as exc:
                # A locked or read-only board is the operator's to fix; it is a BoardError
                # so the caller can report it and still do the other half of its work.
                raise BoardError(f"could not create a task on {self.path}: {exc}") from exc
            self._event(task_id, "created", {"assignee": assignee or None, "status": status,
                                            "source": created_by, "workspace_kind": workspace_kind})
            return task_id
        raise BoardError("could not allocate a task id")

    def complete(self, task_id: str, *, author: str = "ngit", comment: str = "",
                 result: str = "") -> bool:
        """`done`, with the reason written in the same transaction.

        False when the task was already done (or archived), so a second sighting of the same
        merge closes nothing twice and writes no second comment. Like `release`, the status
        and the line that explains it are one write: a completion nobody can account for is
        the silent action this file exists to avoid.

        The claim columns are cleared because the task is finished: a dispatcher that woke
        up to a `done` task must not find it still claimed.
        """
        now = int(time.time())
        summary = (result or comment).strip()[:2000]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE tasks SET status = ?, completed_at = ?, result = ?,"
                " claim_lock = NULL, claim_expires = NULL, current_run_id = NULL,"
                " consecutive_failures = 0"
                " WHERE id = ? AND status NOT IN ('done', 'archived')",
                (DONE, now, summary or None, str(task_id)),
            )
            if cur.rowcount != 1:
                self.db.execute("ROLLBACK")
                return False
            if comment:
                self.db.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at)"
                    " VALUES (?,?,?,?)", (str(task_id), author or "ngit", comment, now))
            self.db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (str(task_id), None, "completed",
                 json.dumps({"result_len": len(summary), "summary": summary[:200],
                             "by": author or "ngit"}, separators=(",", ":")), now))
            self.db.execute("COMMIT")
            return True
        except sqlite3.Error:
            self.db.execute("ROLLBACK")
            raise

    def comment(self, task_id: str, author: str, body: str) -> int:
        """Append one comment. Raises when the task is gone, so a caller cannot write
        history onto nothing."""
        if self.task(task_id) is None:
            raise BoardError(f"unknown task {task_id}")
        cur = self.db.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
            (str(task_id), str(author or "ngit"), body, int(time.time())),
        )
        rowid = int(cur.lastrowid or 0)
        self._event(task_id, "commented", {"author": author, "len": len(body)})
        return rowid

    def release(self, task_id: str, *, author: str = "", comment: str = "") -> bool:
        """`blocked` -> `ready`, with the decision recorded in the same transaction.

        The audit line and the promotion are one write on purpose: a promotion without the
        line that says who authorised it is exactly the silent action the gate exists to
        prevent, and an audit line without the promotion is a false record.
        False when the task was not blocked (already answered).
        """
        now = int(time.time())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE tasks SET status = ?, current_run_id = NULL, consecutive_failures = 0,"
                " last_failure_error = NULL WHERE id = ? AND status = 'blocked'",
                (READY, str(task_id)),
            )
            if cur.rowcount != 1:
                self.db.execute("ROLLBACK")
                return False
            if comment:
                self.db.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at)"
                    " VALUES (?,?,?,?)", (str(task_id), author or "ngit", comment, now))
            self.db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (str(task_id), None, "unblocked",
                 json.dumps({"status": READY, "by": author or "ngit"},
                            separators=(",", ":")), now))
            self.db.execute("COMMIT")
            return True
        except sqlite3.Error:
            self.db.execute("ROLLBACK")
            raise

    def _event(self, task_id: str, kind: str, payload: Optional[dict[str, Any]]) -> None:
        try:
            self.db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (str(task_id), None, kind,
                 json.dumps(payload, separators=(",", ":")) if payload else None,
                 int(time.time())),
            )
        except sqlite3.Error:  # noqa: BLE001 - an event is a nicety, not the write itself
            pass

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
