"""The ngit bridge: issues as work, PRs announced, and the things that must not happen.

Placeholders only: keys, channel ids, repo names and board slugs here are invented.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import time
from pathlib import Path

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import cli, ngit
from hermes_nostr_gateway.control import ControlConfig
from hermes_nostr_gateway.kanban import Anchor, Board
from hermes_nostr_gateway.store import Store

COMMUNITY = "ab" * 32
CHANNEL = "cd" * 32
REPO_OWNER = "ee" * 32
ADDRESS = f"30617:{REPO_OWNER}:example-repo"
BOARD = "example-board"
PROJECT = "example-project"
TRUSTED_HEX = "11" * 32
STRANGER_HEX = "22" * 32
OTHER_HEX = "33" * 32
CHANNEL_ID = "cd" * 32


def npub(hex_key: str) -> str:
    return ngit.npub_of(hex_key)


def event_id(seed: str) -> str:
    return (seed * 64)[:64]


def nevent(hex_id: str, kind: int = ngit.KIND_ISSUE, author: str = REPO_OWNER) -> str:
    return ngit.nevent_of({"id": hex_id, "pubkey": author, "kind": kind})


def make_issue(seed: str, *, author: str = TRUSTED_HEX, subject: str = "Fix the thing",
               body: str = "It is broken.", comments: tuple = (),
               status: str = "open") -> ngit.Issue:
    identifier = event_id(seed)
    return ngit.Issue(id=nevent(identifier), hex=identifier, author=npub(author),
                      subject=subject, body=body, comments=comments,
                      comment_count=len(comments), status=status)


def make_comment(seed: str, *, author: str = STRANGER_HEX, body: str = "Same here.",
                 created_at: int = 1) -> ngit.Comment:
    identifier = event_id(seed)
    return ngit.Comment(id=nevent(identifier, kind=ngit.KIND_ISSUE_COMMENT),
                        hex=identifier, author=npub(author), body=body,
                        created_at=created_at)


def make_pr(seed: str, *, author: str = TRUSTED_HEX, subject: str = "Add a widget",
            branch: str = "pr/widget", created_at: int = 1000) -> ngit.PullRequest:
    identifier = event_id(seed)
    return ngit.PullRequest(id=identifier, author=npub(author), subject=subject,
                            branch=branch, created_at=created_at,
                            nevent=nevent(identifier, kind=ngit.KIND_PR, author=author))


BOARD_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, idempotency_key TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
    last_failure_error TEXT, current_run_id INTEGER,
    block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
"""


@pytest.fixture()
def home(tmp_path: Path) -> Path:
    """A miniature Hermes home: a project registry and a board bound to that project."""
    base = tmp_path / "hermes-home"
    where = base / "kanban" / "boards" / BOARD
    where.mkdir(parents=True)
    (where / "board.json").write_text(json.dumps(
        {"slug": BOARD, "name": "Example Board", "project_id": "p_example"}))
    registry = sqlite3.connect(base / "projects.db")
    try:
        registry.execute(
            "create table projects (id text primary key, slug text not null unique,"
            " name text not null, board_slug text, primary_path text,"
            " created_at integer not null, archived integer not null default 0)")
        registry.execute("insert into projects values (?,?,?,?,?,?,0)",
                         ("p_example", PROJECT, "Example", BOARD,
                          str(tmp_path / "example-checkout"), 0))
        registry.commit()
    finally:
        registry.close()
    board_db = sqlite3.connect(where / "kanban.db")
    try:
        board_db.executescript(BOARD_SCHEMA)
        board_db.commit()
    finally:
        board_db.close()
    return base


@pytest.fixture()
def store(tmp_path: Path):
    handle = Store(tmp_path / "gateway.db", identity="test")
    yield handle
    handle.close()


class Harness:
    """A sync with every external collaborator replaced by something a test can see."""

    def __init__(self, home: Path, store: Store, *, issues=(), trusted=(TRUSTED_HEX,),
                 assignee: str = "default", anchor: Anchor | None = None,
                 notifier=None, prs=((), ())):
        self.home = home
        self.store = store
        self.board = Board(home / "kanban" / "boards" / BOARD / "kanban.db")
        self.messages: list[tuple[str, str]] = []
        self.issues = list(issues)
        self.details: dict[str, ngit.Issue] = {}
        self.detail_calls: list[str] = []
        self.prs_data = prs
        self.since_seen: list[int] = []
        self.control = ControlConfig.from_config(
            {"trusted_npubs": [npub(key) for key in trusted]})
        self.config = ngit.BridgeConfig(assignee=assignee, lookback_hours=0)
        self.anchor = anchor
        self.notifier = notifier or (lambda who, text: self.messages.append((who, text)))
        self.repo = home.parent / "checkout"
        self.repo.mkdir(exist_ok=True)
        self.target = ngit.Target(conversation=f"concord:{COMMUNITY}:{CHANNEL}",
                                  repo=self.repo, board=BOARD, project=PROJECT,
                                  address=ADDRESS)

    def detail(self, ref: ngit.Issue) -> ngit.Issue:
        self.detail_calls.append(ref.hex)
        return self.details.get(ref.hex, ref)

    def prs(self, since: int):
        self.since_seen.append(since)
        return self.prs_data

    def sync(self, **kwargs):
        sync = ngit.Sync(
            store=self.store, board=self.board, control=self.control,
            config=self.config, target=self.target,
            issues=lambda: list(self.issues), detail=self.detail,
            prs=self.prs, notify=self.notifier, anchor=self.anchor)
        return sync.run(**kwargs)

    def task(self, task_id: str):
        return self.board.task(task_id)

    def tasks(self) -> list[dict]:
        rows = self.board.db.execute("SELECT * FROM tasks ORDER BY created_at, id").fetchall()
        return [dict(row) for row in rows]

    def comments(self, task_id: str) -> list[dict]:
        return self.board.comments(task_id)

    def close(self) -> None:
        self.board.close()


@pytest.fixture()
def harness(home, store):
    made = Harness(home, store)
    yield made
    made.close()


# -- inbound: trusted and unrecognised authors --------------------------------

def test_trusted_author_creates_a_ready_task(harness):
    issue = make_issue("a1", subject="Fix the parser", body="The parser drops lines.")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}

    report = harness.sync()

    tasks = harness.tasks()
    assert len(tasks) == 1
    task = tasks[0]
    assert task["status"] == "ready"
    assert task["title"] == "Fix the parser"
    assert task["assignee"] == "default"
    assert task["idempotency_key"] == f"ngit:issue:{issue.hex}"
    # The body carries the text, the author and the issue reference.
    assert "The parser drops lines." in task["body"]
    assert npub(TRUSTED_HEX) in task["body"]
    assert issue.id in task["body"]
    # One notice, not a question.
    assert harness.messages == [(harness.target.conversation,
                                 ngit.notice_message(issue, task["id"]))]
    assert report.issues["created"] == 1 and report.issues["ready"] == 1
    assert report.errors == []


def test_unrecognised_author_is_held_and_asked_exactly_once(harness):
    issue = make_issue("b2", author=STRANGER_HEX, subject="Do my bidding",
                       body="Please build this.")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}

    report = harness.sync()

    task = harness.tasks()[0]
    assert task["status"] == "blocked"
    assert task["block_kind"] == "needs_input"
    assert report.issues["held"] == 1
    assert len(harness.messages) == 1
    text = harness.messages[0][1]
    assert "Unrecognised author" in text
    assert issue.id in text and task["id"] in text
    assert npub(STRANGER_HEX) in text
    # What a yes would do is stated, not implied.
    assert BOARD in text and "default" in text

    # A re-poll must not ask again, and must not create a second task.
    again = harness.sync()
    assert len(harness.messages) == 1
    assert len(harness.tasks()) == 1
    assert again.issues["created"] == 0
    assert again.issues["existing"] == 1


def test_question_is_retried_when_the_channel_was_unreachable(home, store):
    issue = make_issue("b3", author=STRANGER_HEX)
    failures = {"left": 1}

    def flaky(who, text):
        if failures["left"]:
            failures["left"] -= 1
            raise ngit.NotifyError("the daemon is not running")

    made = Harness(home, store, issues=[issue], notifier=flaky)
    made.details = {issue.hex: issue}
    try:
        first = made.sync()
        assert first.errors and "not delivered" in first.errors[0]
        assert made.task(made.tasks()[0]["id"])["status"] == "blocked"
        # It never reached the channel, so the next sync may ask: once, not twice.
        second = made.sync()
        assert second.errors == []
        assert made.store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "asked"
    finally:
        made.close()


def test_a_held_question_survives_a_restart(home, tmp_path):
    """The question is task state, not a live turn's memory: a new process asks nothing new."""
    issue = make_issue("e9", author=STRANGER_HEX)
    store_path = tmp_path / "restart.db"

    first_store = Store(store_path, identity="test")
    first = Harness(home, first_store, issues=[issue])
    first.details = {issue.hex: issue}
    try:
        first.sync()
        assert len(first.messages) == 1
    finally:
        first.close()
        first_store.close()

    second_store = Store(store_path, identity="test")
    second = Harness(home, second_store, issues=[issue])
    second.details = {issue.hex: issue}
    try:
        report = second.sync()
        assert second.messages == []
        assert report.issues["existing"] == 1
        assert len(second.tasks()) == 1
        assert second_store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "asked"
    finally:
        second.close()
        second_store.close()


def test_yes_promotes_with_an_audit_comment(home, store):
    issue = make_issue("c4", author=STRANGER_HEX, subject="Worth doing")
    made = Harness(home, store, issues=[issue])
    made.details = {issue.hex: issue}
    try:
        made.sync()
        task_id = made.tasks()[0]["id"]

        result = ngit.answer(store, made.control, ref=task_id, yes=True,
                             by=npub(TRUSTED_HEX), home=home)

        assert result["changed"] is True and result["status"] == "ready"
        assert made.task(task_id)["status"] == "ready"
        audit = [c["body"] for c in made.comments(task_id)]
        assert any("Authorised by" in body and npub(TRUSTED_HEX) in body for body in audit)
        assert store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "authorised"
        # Answering again changes nothing and writes no second audit line.
        again = ngit.answer(store, made.control, ref=issue.id, yes=True,
                            by=npub(TRUSTED_HEX), home=home)
        assert again["changed"] is False
        assert len(made.comments(task_id)) == len(audit)
    finally:
        made.close()


def test_no_leaves_it_blocked_with_the_reason(home, store):
    issue = make_issue("d5", author=STRANGER_HEX)
    made = Harness(home, store, issues=[issue])
    made.details = {issue.hex: issue}
    try:
        made.sync()
        task_id = made.tasks()[0]["id"]

        result = ngit.answer(store, made.control, ref=issue.id, yes=False,
                             by=npub(TRUSTED_HEX), reason="not our roadmap", home=home)

        assert result["status"] == "blocked"
        assert made.task(task_id)["status"] == "blocked"
        assert any("not our roadmap" in c["body"] for c in made.comments(task_id))
        assert store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "declined"
        # A declined issue is never asked about again.
        made.sync()
        assert len(made.messages) == 1
    finally:
        made.close()


def test_an_untrusted_npub_cannot_answer(home, store):
    issue = make_issue("d6", author=STRANGER_HEX)
    made = Harness(home, store, issues=[issue])
    made.details = {issue.hex: issue}
    try:
        made.sync()
        task_id = made.tasks()[0]["id"]
        with pytest.raises(ngit.NgitError, match="not a trusted npub"):
            ngit.answer(store, made.control, ref=task_id, yes=True, by=npub(STRANGER_HEX),
                        home=home)
        assert made.task(task_id)["status"] == "blocked"
    finally:
        made.close()


def test_a_plane_only_trust_is_not_a_concord_trust(home, store):
    issue = make_issue("d7", author=OTHER_HEX)
    control = ControlConfig.from_config(
        {"planes": {"nip17": {"trusted_npubs": [npub(OTHER_HEX)]}}})
    made = Harness(home, store, issues=[issue], trusted=())
    made.control = control
    made.details = {issue.hex: issue}
    try:
        made.sync()
        assert made.tasks()[0]["status"] == "blocked"
    finally:
        made.close()


def test_concord_plane_trust_is_enough(home, store):
    issue = make_issue("d8", author=OTHER_HEX)
    made = Harness(home, store, issues=[issue], trusted=())
    made.control = ControlConfig.from_config(
        {"planes": {"concord": {"trusted_npubs": [npub(OTHER_HEX)]}}})
    made.details = {issue.hex: issue}
    try:
        made.sync()
        assert made.tasks()[0]["status"] == "ready"
    finally:
        made.close()


# -- comments ----------------------------------------------------------------

def test_comments_append_in_order_and_only_once(harness):
    later = make_comment("e2", body="Second thought.", created_at=200)
    earlier = make_comment("e1", body="First thought.", created_at=100)
    issue = make_issue("e0", comments=(later, earlier))
    harness.issues = [issue]
    harness.details = {issue.hex: issue}

    harness.sync()
    task_id = harness.tasks()[0]["id"]
    bodies = [c["body"] for c in harness.comments(task_id)]
    assert bodies == ["First thought.", "Second thought."]
    assert harness.comments(task_id)[0]["author"] == npub(STRANGER_HEX)

    # A new comment arrives; only it is appended, after the ones already there.
    fresh = make_comment("e3", body="Third thought.", created_at=300)
    updated = make_issue("e0", comments=(later, earlier, fresh))
    harness.issues = [updated]
    harness.details = {updated.hex: updated}
    report = harness.sync()
    assert report.issues["comments"] == 1
    assert [c["body"] for c in harness.comments(task_id)] == [
        "First thought.", "Second thought.", "Third thought."]

    # Nothing new: no detail call, no duplicate.
    report = harness.sync()
    assert report.issues["comments"] == 0
    assert [c["body"] for c in harness.comments(task_id)][-1] == "Third thought."


# -- re-running ---------------------------------------------------------------

def test_a_second_sync_changes_nothing(harness):
    issue = make_issue("f1")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.sync()
    before = harness.tasks()
    messages = list(harness.messages)

    report = harness.sync()

    assert harness.tasks() == before
    assert harness.messages == messages
    assert report.issues["created"] == 0
    assert report.posted == 0


def test_one_issue_producing_a_second_task_is_not_prevented(harness):
    """Issue -> task is many-to-many by provenance; the issue id is a dedupe key, not an
    identity. A human filing a second task for the same issue is not the bridge's business.
    """
    issue = make_issue("f2")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.sync()
    task_id = harness.tasks()[0]["id"]
    harness.board.create(title="Follow-up on the same issue", body="manual",
                         assignee="default", status="ready",
                         idempotency_key=f"ngit:issue:{issue.hex}", anchor=harness.anchor)
    # The second create returns the first task: the idempotency key is what dedupes.
    assert [t["id"] for t in harness.tasks()] == [task_id]


def test_a_deleted_task_does_not_resurrect(harness):
    issue = make_issue("f3")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.sync()
    task_id = harness.tasks()[0]["id"]
    harness.board.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

    report = harness.sync()

    assert harness.tasks() == []
    assert report.issues["deleted"] == 1
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "deleted"
    # And it stays gone on every later poll.
    harness.sync()
    assert harness.tasks() == []


def test_recreate_brings_a_deleted_task_back(harness):
    comment = make_comment("f7")
    issue = make_issue("f4", comments=(comment,))
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.sync()
    task_id = harness.tasks()[0]["id"]
    harness.board.db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
    harness.sync()

    report = harness.sync(recreate=True)

    assert report.issues["created"] == 1
    tasks = harness.tasks()
    assert len(tasks) == 1 and tasks[0]["id"] != task_id
    # The new task carries the issue's history again, not a record of a task that is gone.
    assert [c["body"] for c in harness.comments(tasks[0]["id"])] == ["Same here."]


def test_only_max_new_issues_are_created_per_poll(home, store):
    first = make_issue("f5")
    second = make_issue("f6", subject="Second issue")
    made = Harness(home, store, issues=[first, second])
    made.config = ngit.BridgeConfig(assignee="default", lookback_hours=0, max_new=1)
    made.details = {first.hex: first, second.hex: second}
    try:
        first_run = made.sync()
        assert first_run.issues["created"] == 1
        assert first_run.issues["deferred"] == 1
        assert len(made.tasks()) == 1
        # Nothing was dropped: the next poll picks the backlog up.
        second_run = made.sync()
        assert second_run.issues["created"] == 1
        assert len(made.tasks()) == 2
    finally:
        made.close()


def test_only_max_prs_are_announced_per_poll(home, store):
    prs = tuple(make_pr(f"c{index}", created_at=1000 + index) for index in range(3))
    made = Harness(home, store, prs=(list(prs), ()))
    made.config = ngit.BridgeConfig(assignee="default", lookback_hours=0, max_prs=2)
    try:
        first = made.sync()
        assert first.prs["signalled"] == 2
        assert first.prs["deferred"] == 1
        # A deferred PR is not behind the cursor: the next poll must still see it.
        assert made.store.ngit_cursor(ADDRESS, "prs") is None
        second = made.sync()
        assert second.prs["signalled"] == 1
        assert len(made.messages) == 3
        assert made.store.ngit_cursor(ADDRESS, "prs") is not None
    finally:
        made.close()


# -- outbound: PRs ------------------------------------------------------------

def test_pr_signal_is_posted_once_and_never_twice(harness):
    pr = make_pr("a7")
    harness.prs_data = ([pr], [])
    harness.sync()
    assert len(harness.messages) == 1
    text = harness.messages[0][1]
    assert pr.subject in text and pr.author in text and pr.branch in text
    assert pr.nevent in text
    assert "ready for review" in text

    # The same PR comes back with the cursor's overlap: no second message.
    harness.sync()
    assert len(harness.messages) == 1


def test_merged_pr_posts_one_follow_up(harness):
    pr = make_pr("a8", created_at=1000)
    harness.prs_data = ([pr], [])
    harness.sync()
    assert len(harness.messages) == 1

    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1100)
    harness.prs_data = ([pr], [merge])
    report = harness.sync()

    assert report.prs["merged"] == 1
    assert len(harness.messages) == 2
    follow_up = harness.messages[1][1]
    assert "merged as " + "d" * 40 in follow_up
    assert pr.subject in follow_up and pr.author in follow_up

    # Neither the merge nor the PR is reported again.
    harness.sync()
    assert len(harness.messages) == 2


def test_a_pr_first_seen_merged_gets_one_message(harness):
    pr = make_pr("a9", created_at=1000)
    merge = ngit.Merge(pr_id=pr.id, commit="e" * 40, created_at=1050)
    harness.prs_data = ([pr], [merge])

    harness.sync()

    assert len(harness.messages) == 1
    text = harness.messages[0][1]
    assert "merged as " + "e" * 40 in text
    assert "ready for review" not in text


def test_a_merge_for_a_pr_this_bridge_never_announced_is_not_replayed(harness):
    merge = ngit.Merge(pr_id=event_id("b0"), commit="f" * 40, created_at=1000)
    harness.prs_data = ([], [merge])

    report = harness.sync()

    assert harness.messages == []
    assert report.prs["merged"] == 0


def test_a_pr_signal_that_could_not_be_sent_is_retried_once(home, store):
    pr = make_pr("b2")
    failures = {"left": 1}

    def flaky(who, text):
        if failures["left"]:
            failures["left"] -= 1
            raise ngit.NotifyError("the daemon is not running")

    made = Harness(home, store, prs=([pr], []), notifier=flaky)
    try:
        first = made.sync()
        assert first.errors and "not delivered" in first.errors[0]
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "pending"
        second = made.sync()
        assert second.errors == []
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "signalled"
        # The failed attempt sent nothing, so the retry is the first message that landed.
        assert second.posted == 1
    finally:
        made.close()


def test_the_cursor_moves_so_the_next_poll_asks_only_for_new_events(harness):
    pr = make_pr("b1", created_at=5_000)
    harness.prs_data = ([pr], [])
    harness.sync()
    assert harness.since_seen == [0]

    harness.prs_data = ([], [])
    harness.sync()
    assert harness.since_seen[1] == 5_000 - ngit.CURSOR_OVERLAP_S
    assert harness.since_seen[1] > 0


def test_since_can_be_given_explicitly(harness):
    harness.prs_data = ([], [])
    harness.sync(since=1234)
    assert harness.since_seen == [1234]


def test_the_first_poll_has_a_lookback_floor(harness):
    harness.config = ngit.BridgeConfig(assignee="default", lookback_hours=24)
    harness.prs_data = ([], [])
    before = int(time.time())
    harness.sync()
    assert before - 24 * 3600 - 2 <= harness.since_seen[0] <= before - 24 * 3600 + 2


# -- the board anchor ---------------------------------------------------------

def test_a_project_board_gives_the_task_a_worktree_and_a_branch(home, store):
    anchor = Anchor.for_board(home, BOARD)
    made = Harness(home, store, issues=[make_issue("c1")], anchor=anchor)
    issue = made.issues[0]
    made.details = {issue.hex: issue}
    try:
        made.sync()
        task = made.tasks()[0]
        assert task["workspace_kind"] == "worktree"
        assert task["project_id"] == "p_example"
        assert task["workspace_path"].endswith(f"/.worktrees/{task['id']}")
        assert task["branch_name"].startswith(f"{PROJECT}/{task['id']}")
    finally:
        made.close()


# -- resolving the channel ----------------------------------------------------

def test_resolve_target_reads_the_one_mapping(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "path": str(repo), "project": PROJECT, "board": BOARD},
    ]}}

    target = ngit.resolve_target(cfg, channel=f"concord:{COMMUNITY}:{CHANNEL}")

    assert target.repo == repo and target.board == BOARD and target.project == PROJECT


def test_resolve_target_matches_an_entry_keyed_by_the_conversation(tmp_path):
    """A cron job has the conversation and nothing else, so an entry may carry exactly that."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    conversation = f"concord:{COMMUNITY}:{CHANNEL}"
    cfg = {"concord": {"projects": [
        {"channel": conversation, "path": str(repo), "project": PROJECT, "board": BOARD},
    ]}}

    target = ngit.resolve_target(cfg, channel=conversation)

    assert target.repo == repo and target.board == BOARD


def test_resolve_target_refuses_an_unmapped_channel():
    with pytest.raises(ngit.NgitError, match="no concord.projects entry"):
        ngit.resolve_target({"concord": {"projects": []}},
                            channel=f"concord:{COMMUNITY}:{CHANNEL}")


def test_resolve_target_refuses_a_channel_with_no_board(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = {"concord": {"projects": [{"channel": CHANNEL, "path": str(repo)}]}}
    with pytest.raises(ngit.NgitError, match="declares no board"):
        ngit.resolve_target(cfg, channel=f"concord:{COMMUNITY}:{CHANNEL}")


def test_overrides_win_over_the_mapping(tmp_path):
    repo, other = tmp_path / "checkout", tmp_path / "other"
    repo.mkdir()
    other.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "path": str(repo), "project": PROJECT, "board": BOARD}]}}
    target = ngit.resolve_target(cfg, channel=f"concord:{COMMUNITY}:{CHANNEL}",
                                 repo=str(other), board="other-board")
    assert target.repo == other and target.board == "other-board"


# -- parsing what ngit and the relays say -------------------------------------

def test_issue_list_json_is_read_as_ngit_prints_it():
    identifier = event_id("d1")
    issue = ngit.issue_from_json({
        "author": npub(TRUSTED_HEX), "comment_count": 2, "id": nevent(identifier),
        "labels": ["bug"], "status": "open", "subject": "A subject"})

    assert issue.hex == identifier and issue.id == nevent(identifier)
    assert issue.author == npub(TRUSTED_HEX) and issue.comment_count == 2
    assert issue.subject == "A subject" and issue.body == ""


def test_issue_view_json_carries_the_body_and_comments():
    ref = ngit.issue_from_json({"author": npub(TRUSTED_HEX), "id": nevent(event_id("d2")),
                                "subject": "A subject", "comment_count": 1})
    detail = ngit.issue_detail_from_json({
        "author": npub(TRUSTED_HEX), "id": ref.id, "subject": "A subject",
        "description": "The body.", "comment_count": 1, "status": "open",
        "comments": [{"author": npub(STRANGER_HEX), "body": "A comment",
                      "created_at": 7, "id": nevent(event_id("d3"), ngit.KIND_ISSUE_COMMENT),
                      "reply_to": None}],
    }, ref)

    assert detail.body == "The body."
    assert detail.comments[0].body == "A comment"
    assert detail.comments[0].author == npub(STRANGER_HEX)
    assert detail.comments[0].hex == event_id("d3")


def test_pull_request_event_is_read_from_its_tags():
    pr = ngit.pull_request_from_event({
        "id": event_id("d4"), "pubkey": TRUSTED_HEX, "kind": ngit.KIND_PR,
        "created_at": 10, "content": "The description.",
        "tags": [["a", ADDRESS], ["subject", "A PR subject"], ["branch-name", "pr/x"]]})

    assert pr.id == event_id("d4") and pr.author == npub(TRUSTED_HEX)
    assert pr.subject == "A PR subject" and pr.branch == "pr/x"
    assert pr.nevent.startswith("nevent1")


def test_applied_event_names_the_root_pr_and_the_merge_commit():
    merge = ngit.merge_from_event({
        "id": event_id("d5"), "pubkey": REPO_OWNER, "kind": ngit.KIND_APPLIED,
        "created_at": 20, "content": "",
        "tags": [["e", event_id("d6"), "", "root"], ["merge-commit", "a" * 40]]})

    assert merge is not None
    assert merge.pr_id == event_id("d6") and merge.commit == "a" * 40


def test_an_applied_event_without_a_root_reference_is_ignored():
    merge = ngit.merge_from_event({"id": event_id("d7"), "kind": ngit.KIND_APPLIED,
                                  "tags": [["a", ADDRESS]]})
    assert merge is None


# -- reading through the ngit CLI ---------------------------------------------

def test_run_ngit_parses_stdout_and_appends_the_json_flag(monkeypatch, tmp_path):
    seen = {}

    def fake_run(command, **kwargs):
        seen["command"], seen["cwd"] = command, kwargs["cwd"]
        return subprocess.CompletedProcess(command, 0, stdout='[{"id": "x"}]', stderr="")

    monkeypatch.setattr(ngit.subprocess, "run", fake_run)
    assert ngit.run_ngit(tmp_path, ["issue", "list"]) == [{"id": "x"}]
    assert seen["command"] == ["ngit", "issue", "list", "--json"]
    assert seen["cwd"] == str(tmp_path)


def test_run_ngit_reports_the_stderr_line_not_a_traceback(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 2, stdout="",
                                          stderr="not a git repository\nsecond line")

    monkeypatch.setattr(ngit.subprocess, "run", fake_run)
    with pytest.raises(ngit.NgitError, match="second line"):
        ngit.run_ngit(tmp_path, ["issue", "list"])


def test_run_ngit_says_so_when_the_binary_is_missing(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("ngit")

    monkeypatch.setattr(ngit.subprocess, "run", fake_run)
    with pytest.raises(ngit.NgitError, match="ngit is not installed"):
        ngit.run_ngit(tmp_path, ["issue", "list"])


def test_run_ngit_rejects_output_that_is_not_one_document(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="not json", stderr="")

    monkeypatch.setattr(ngit.subprocess, "run", fake_run)
    with pytest.raises(ngit.NgitError, match="one JSON document"):
        ngit.run_ngit(tmp_path, ["issue", "list"])


def test_the_repo_address_comes_from_the_announcement_coordinate(tmp_path):
    # An naddr made from the placeholder address, so the test carries no real repo's key.
    coordinate = sdk.Nip19Coordinate(sdk.Coordinate.parse(ADDRESS),
                                     [sdk.RelayUrl.parse("wss://relay.example")]).to_bech32()

    def fake_run(repo, args, *, timeout=120.0):
        return {"is_nostr_repo": True, "coordinate": coordinate,
                "identifier": "example-repo"}

    repository = ngit.Ngit(tmp_path, runner=fake_run).repository()
    assert repository.address == ADDRESS
    # The read set for its issues and PRs comes from the announcement's own hints.
    assert repository.relays == ("wss://relay.example",)


def test_a_checkout_with_no_nostr_remote_has_no_address(tmp_path):
    def fake_run(repo, args, *, timeout=120.0):
        return {"is_nostr_repo": False}

    assert ngit.Ngit(tmp_path, runner=fake_run).repository().address == ""


def test_a_failing_repo_read_does_not_stop_the_sync(tmp_path):
    def fake_run(repo, args, *, timeout=120.0):
        raise ngit.NgitError("not a git repository")

    assert ngit.Ngit(tmp_path, runner=fake_run).repository().address == ""


# -- the CLI wiring -----------------------------------------------------------

def write_config(tmp_path: Path, projects: list[dict], *,
                 assignee: str | None = "default") -> str:
    path = tmp_path / "config.yaml"
    document: dict = {
        "identity": {"secret_env": "HNG_TEST_SECRET"},
        "store": str(tmp_path / "gateway.db"),
        "concord": {"projects": projects},
        "control": {"trusted_npubs": [npub(TRUSTED_HEX)]},
    }
    if assignee is not None:
        document["ngit"] = {"assignee": assignee}
    path.write_text(json.dumps(document))
    return str(path)


def test_cli_refuses_to_queue_work_with_no_assignee(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    config = write_config(tmp_path, [{"channel": CHANNEL, "path": str(tmp_path),
                                      "project": PROJECT, "board": BOARD}],
                          assignee=None)
    code = cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])
    assert code == 2
    assert "ngit.assignee" in capsys.readouterr().err


def test_cli_refuses_an_unmapped_channel(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    config = write_config(tmp_path, [])
    code = cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])
    assert code == 2
    assert "no concord.projects entry" in capsys.readouterr().err


def test_cli_refuses_a_board_that_is_not_there(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    config = write_config(tmp_path, [{"channel": CHANNEL, "path": str(tmp_path),
                                      "project": PROJECT, "board": "no-such-board"}])
    code = cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])
    assert code == 2
    assert "no board database" in capsys.readouterr().err
