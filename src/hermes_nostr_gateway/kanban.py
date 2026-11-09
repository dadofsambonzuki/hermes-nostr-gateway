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
from typing import Any, Optional, Sequence

from . import ngit_guard
from .projects import load_registry

#: The branch the kernel derives for a project-linked task: `<slug>/<task-id>[-<title>]`.
#: Duplicated rather than imported because the kernel is a separate distribution; the
#: characters are the same set `hermes_cli.projects_db` allows.
_BRANCH_SAFE = re.compile(r"[^a-z0-9._-]+")

#: Statuses the default workflow defines, plus the archive column.
READY = "ready"
BLOCKED = "blocked"
REVIEW = "review"
DONE = "done"

#: How a block reason says the *bridge* parked the card rather than a person: a verdict it
#: could not act on yet, or a fix it handed back. A revision may reopen a card parked this
#: way -- the next event is what it waits for. A reason beginning any other way is a human
#: question, and a revision does not answer questions.
#:
#: This is a hand-off tag, not a state machine: once the bridge acts on a verdict it finishes
#: the card instead of parking it, so the only cards that carry a marker are the ones parked
#: before that rule existed. The marker is what lets those heal; it is not how the loop runs.
PARK_MARKERS = ("verdict:", "revision:")
#: The one marker a fix worker writes: "the fix is pushed, at this tip". The bridge reopens
#: the card for review on its own, because the push usually lands before the worker parks.
FIXED_MARK = "revision:"
#: The one marker a review worker writes: "the verdict is on the PR, at this tip". It is what
#: `hng ngit reconcile` looks for: a card parked this way is waiting for the bridge to read a
#: verdict, not for a person, so a verdict the pass's cursor has already gone past can still
#: be applied to it.
VERDICT_MARK = "verdict:"


class BoardError(RuntimeError):
    """The file is not a board, or a write could not be made."""


def new_task_id() -> str:
    """`t_` + 4 hex bytes, the kernel's own shape (idempotency is the key's job)."""
    return "t_" + secrets.token_hex(4)


def branch_name(slug: str, task_id: str, title: str = "") -> str:
    base = f"{slug or 'project'}/{task_id}"
    tail = _BRANCH_SAFE.sub("-", (title or "").strip().lower()).strip("-")[:40].strip("-")
    return f"{base}-{tail}" if tail else base


#: What makes a key a *fix* card's: the PR root, then the verdict that asked for the fix.
#: It is a strict extension of `ngit:pr:<root>`, which is why every query that means "the
#: PR's review card" excludes it -- a fix card talks about a PR without being its review.
FIX_KEY_INFIX = ":changes:"


def fix_key(pr_id: str, verdict_event: str) -> str:
    """The idempotency key of the card a changes-requested verdict queues.

    Keyed on the verdict event, not on the PR: a second review of the same PR is a second
    statement about it and earns a second card, while a re-read of one verdict lands on the
    card it already has. That is the whole of the idempotency -- no flag, no event marker.
    """
    return f"ngit:pr:{str(pr_id or '')}{FIX_KEY_INFIX}{str(verdict_event or '')}"


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

        A fix card is a strict extension of the same prefix and is excluded on purpose: it
        is the work a verdict queued, not the PR's review, and answering a verdict against
        it would put a second writer on one card.

        Newest first, because a verdict covers the revision it was given for: a later card
        is a later review of the same PR, and its verdict is the one in force.
        """
        pr_id = str(pr_id or "")
        if not pr_id:
            return None
        row = self.db.execute(
            "SELECT * FROM tasks WHERE status != 'archived'"
            " AND ((idempotency_key = ?"
            "       OR (idempotency_key LIKE ? AND idempotency_key NOT LIKE ?))"
            "      OR (title LIKE ? AND body LIKE ?))"
            " ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (f"ngit:pr:{pr_id}", f"ngit:pr:{pr_id}:%", f"%{FIX_KEY_INFIX}%",
             f"Review PR {pr_id[:8]}:%", f"%{pr_id}%"),
        ).fetchone()
        return dict(row) if row is not None else None

    def pr_cards(self, pr_id: str, nevent: str = "") -> list[dict[str, Any]]:
        """Every live card that *is* this PR's card, oldest first.

        One PR root has one card for the life of the PR, but the board can hold more than
        one when history got there first: cards filed before one-card-per-root, or a
        re-review queued by hand under the brief's own title. A merge finishes all of them,
        so this returns the whole set rather than the newest anchor.

        The identities are the ones `review_task` already trusts: the bridge's idempotency
        key (bare or per-tip), or the review brief's title with the PR's event id / nevent in
        the body. A bare mention of the PR elsewhere is deliberately not a match -- a card
        that talks about a PR is not necessarily finished when that PR lands. A fix card is
        excluded for the same reason it is not a review card: it is the work a verdict
        queued, and the merge that finishes the PR finishes the review, not the fix.
        """
        pr_id = str(pr_id or "")
        if not pr_id:
            return []
        clauses = ["idempotency_key = ?",
                   "(idempotency_key LIKE ? AND idempotency_key NOT LIKE ?)",
                   "(title LIKE ? AND body LIKE ?)"]
        params: list[Any] = [f"ngit:pr:{pr_id}", f"ngit:pr:{pr_id}:%", f"%{FIX_KEY_INFIX}%",
                             f"Review PR {pr_id[:8]}:%", f"%{pr_id}%"]
        if nevent:
            clauses.append("(title LIKE ? AND body LIKE ?)")
            params.extend([f"Review PR {pr_id[:8]}:%", f"%{nevent}%"])
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status != 'archived' AND ("
            + " OR ".join(clauses) + ") ORDER BY created_at, rowid",
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]

    def fix_cards(self, pr_id: str) -> list[dict[str, Any]]:
        """Every live card a changes-requested verdict queued for this PR, oldest first.

        Not the PR's review card, and not found by `pr_cards`: a fix is keyed on the verdict
        event that asked for it, so one PR can have several over its life and each is its
        own unit of work. The bridge finishes them when the PR lands -- a fix queued for a
        merged PR is work nobody should claim -- and nothing else here reads them.
        """
        pr_id = str(pr_id or "")
        if not pr_id:
            return []
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status != 'archived' AND idempotency_key LIKE ?"
            " ORDER BY created_at, rowid", (f"ngit:pr:{pr_id}{FIX_KEY_INFIX}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def blocked_cards(self) -> list[dict[str, Any]]:
        """Every blocked card, for a caller that matches its own premise text."""
        rows = self.db.execute(
            "SELECT * FROM tasks WHERE status = 'blocked' ORDER BY created_at, rowid"
        ).fetchall()
        return [dict(row) for row in rows]

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
                " consecutive_failures = 0, block_kind = NULL, block_recurrences = 0"
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

    def reopen(self, task_id: str, *, author: str = "ngit", comment: str = "",
               statuses: Sequence[str] = (REVIEW, DONE), body: str = "") -> bool:
        """Put a reviewed or finished card back in the queue, with the reason on it.

        A revision is news about a PR that already has a card, so that card is reopened
        instead of a second one being made. `ready` is the state the dispatcher claims: the
        kernel's own review reopen does the same (`review` -> `ready`), and writing the
        literal `running` would leave a card no worker had claimed and nobody would spawn.

        The operator's per-task pin is deliberately untouched: `model_override` /
        `provider_override` were set on this card by a person, and a revision is not a
        channel-config change -- the profile's model moves every card that was never pinned,
        which is the right place for it. `consecutive_failures` is reset, because a reopen
        is a fresh attempt at the same work; that is the opposite of the kernel's review
        reopen (which treats review as "not a success signal"), so this is the bridge's
        own decision and is recorded as one.

        `blocked` is in the set only for a card this bridge parked itself (`parked_by_bridge`):
        a verdict or a fix it could not act on at the time, waiting for the next event rather
        than for a person. A card blocked on a question is not in the revision's gift.

        `body` replaces the card's brief when the new tip needs a different instruction --
        a revision after a fix round puts the review brief back. Empty keeps what is there.

        False when the card was not in one of `statuses`: a card already queued or running
        is left to finish the tip it started on, and a blocked card waiting for a human is
        not released by a revision.
        """
        now = int(time.time())
        allowed = tuple(dict.fromkeys(str(status) for status in statuses))
        if not allowed:
            return False
        marks = ", ".join("?" * len(allowed))
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute(
                "SELECT status FROM tasks WHERE id = ?", (str(task_id),)).fetchone()
            if row is None or str(row["status"]) not in allowed:
                self.db.execute("ROLLBACK")
                return False
            prior = str(row["status"])
            if prior == BLOCKED and not self.parked_by_bridge(task_id):
                self.db.execute("ROLLBACK")
                return False
            sets = ("status = ?, completed_at = NULL, claim_lock = NULL,"
                    " claim_expires = NULL, current_run_id = NULL, worker_pid = NULL,"
                    " consecutive_failures = 0, last_failure_error = NULL,"
                    " block_kind = NULL")
            params: list[Any] = [READY]
            if body:
                sets += ", body = ?"
                params.append(str(body))
            self.db.execute(
                f"UPDATE tasks SET {sets} WHERE id = ? AND status IN ({marks})",
                (*params, str(task_id), *allowed),
            )
            if comment:
                self.db.execute(
                    "INSERT INTO task_comments (task_id, author, body, created_at)"
                    " VALUES (?,?,?,?)", (str(task_id), author or "ngit", comment, now))
            self.db.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?,?,?,?,?)",
                (str(task_id), None, "review_reopened",
                 json.dumps({"status": READY, "from": prior, "by": author or "ngit"},
                            separators=(",", ":")), now))
            self.db.execute("COMMIT")
            return True
        except sqlite3.Error:
            self.db.execute("ROLLBACK")
            raise

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

    def block(self, task_id: str, *, kind: str = "dependency", author: str = "ngit",
              comment: str = "") -> bool:
        """`* -> blocked`, with the reason in the same transaction.

        The kernel's own `block` writes `block_kind` so a re-block loop can be detected; this
        does the same, and takes the comment as the record of *why* (the CLI has no
        `--reason` flag either). False when the card is already terminal.

        The reason is written into the event payload as well as the comment, the shape the
        kernel's own block writes: that payload is what `parked_by_bridge` reads, and a
        board written by only one of the two writers would make the same card look parked
        and un-parked depending on who blocked it.
        """
        now = int(time.time())
        self.db.execute("BEGIN IMMEDIATE")
        try:
            cur = self.db.execute(
                "UPDATE tasks SET status = ?, block_kind = ?, completed_at = NULL"
                " WHERE id = ? AND status NOT IN ('done', 'archived')",
                (BLOCKED, str(kind or "dependency"), str(task_id)),
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
                (str(task_id), None, "blocked",
                 json.dumps({"kind": str(kind or "dependency"), "reason": comment,
                             "by": author or "ngit"}, separators=(",", ":")), now),
            )
            self.db.execute("COMMIT")
            return True
        except sqlite3.Error:
            self.db.execute("ROLLBACK")
            raise

    def parked_by_bridge(self, task_id: str) -> bool:
        """Whether the newest block on this card is the bridge's own hand-off.

        The reason is the only thing a block carries, so this is the one place a reason is
        read as a fact -- and it is read as a *tag*, not a state: the bridge's briefs write
        one of `PARK_MARKERS` as the first word, and anything else is a person's question.
        A card with no block event (status moved by hand, a board from another tool) has no
        marker and is never the bridge's to reopen.
        """
        return bool(self.park_marker(task_id))

    def park_marker(self, task_id: str) -> str:
        """Which `PARK_MARKERS` word the newest block starts with, or "" for a question."""
        row = self.db.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'blocked'"
            " ORDER BY id DESC LIMIT 1", (str(task_id),)).fetchone()
        if row is None:
            return ""
        try:
            reason = str((json.loads(row["payload"] or "{}") or {}).get("reason") or "")
        except ValueError:
            return ""
        head = reason.strip().lower()
        return next((mark for mark in PARK_MARKERS if head.startswith(mark)), "")

    def set_body(self, task_id: str, body: str) -> bool:
        """Replace the card's brief. False for a card that is gone or archived.

        The body is the instruction the dispatcher hands the next worker, so it is rewritten
        when the same card is queued for a fresh review of a new tip. It is not a status:
        nothing reads it back.
        """
        if not body:
            return False
        cur = self.db.execute(
            "UPDATE tasks SET body = ? WHERE id = ? AND status != 'archived'",
            (str(body), str(task_id)))
        return cur.rowcount == 1

    # -- the `done` requires `applied` guard -------------------------------

    def guard_pr(self, task_id: str, *, pr_id: str, state: str, tip: str = "",
                 merge_commit: str = "", detail: str = "", evidence: str = "") -> None:
        """Record a card's PR state on the board, and make completion refuse while it holds.

        The guard is a trigger on `tasks`, so both completion paths -- the agent's
        `kanban_complete` tool and the CLI's `hermes kanban complete` -- are refused without
        either of them knowing this bridge exists. See `ngit_guard`. `evidence` is what the
        pass compared before deciding, and it is part of the sentence a person reads.
        """
        try:
            ngit_guard.guard(self.db, task_id, pr_id=pr_id, state=state, tip=tip,
                             merge_commit=merge_commit, detail=detail, evidence=evidence)
        except sqlite3.Error as exc:
            raise BoardError(f"could not guard {task_id} on {self.path}: {exc}") from exc

    def release_guard(self, task_id: str, *, merge_commit: str = "",
                      evidence: str = "") -> None:
        """The PR is applied: the card may now be completed."""
        try:
            ngit_guard.release(self.db, task_id, merge_commit=merge_commit, evidence=evidence)
        except sqlite3.Error as exc:
            raise BoardError(f"could not release {task_id} on {self.path}: {exc}") from exc

    def approve_guard(self, task_id: str, *, verdict: str = "") -> None:
        """The reviewer approved the PR: the verdict, not the merge, finishes this card.

        A review card's `done` is the review's end, and an approval is the review's end. The
        merge stays a person's -- the card is finished, the PR is not landed, and the bridge
        records the reviewed tip on the link (`verdict_tip`) for the merge route to compare
        before it applies anything. Releasing rather than
        removing the trigger keeps the guard row, so the pass can say what it saw.
        """
        try:
            ngit_guard.approve(self.db, task_id, verdict=verdict)
        except sqlite3.Error as exc:
            raise BoardError(f"could not approve {task_id} on {self.path}: {exc}") from exc

    def close_guard(self, task_id: str, *, event_id: str = "", evidence: str = "") -> None:
        """The PR was closed without a merge: completion stays refused, naming the event."""
        try:
            ngit_guard.close(self.db, task_id, event_id=event_id, evidence=evidence)
        except sqlite3.Error as exc:
            raise BoardError(f"could not close {task_id} on {self.path}: {exc}") from exc

    def guard_state(self, task_id: str) -> Optional[dict[str, Any]]:
        """The guard on one card, or None when the board does not guard it."""
        return ngit_guard.state_of(self.db, task_id)

    def prune_guards(self) -> int:
        """Drop guards whose card is gone or finished; returns how many."""
        try:
            return ngit_guard.prune(self.db)
        except sqlite3.Error:
            return 0

    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Board":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
