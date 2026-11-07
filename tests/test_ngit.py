"""The ngit bridge: issues as work, PRs announced, and the things that must not happen.

Placeholders only: keys, channel ids, repo names and board slugs here are invented.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import subprocess
import threading
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
    return ngit.Comment(id=nevent(identifier, kind=ngit.KIND_COMMENT),
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
    block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0,
    model_override TEXT, provider_override TEXT
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
        self.notifier = notifier or (lambda who, text, key="": self.messages.append((who, text)))
        self.repo = home.parent / "checkout"
        self.repo.mkdir(exist_ok=True)
        self.target = ngit.Target(conversation=f"concord:{COMMUNITY}:{CHANNEL}",
                                  repo=self.repo, board=BOARD, project=PROJECT,
                                  address=ADDRESS, default_branch="master")

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

    def flaky(who, text, key=""):
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
    # And its own notice: the first one named a task that no longer exists.
    assert len(harness.messages) == 2
    assert tasks[0]["id"] in harness.messages[1][1]
    assert task_id not in harness.messages[1][1]


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
        # The cursor stops short of the deferred PR, so the next poll still reads it.
        assert made.store.ngit_cursor(ADDRESS, "prs") == str(1001 - ngit.CURSOR_OVERLAP_S)
        second = made.sync()
        assert second.prs["signalled"] == 1
        assert len(made.messages) == 3
        assert made.store.ngit_cursor(ADDRESS, "prs") == str(1002 - ngit.CURSOR_OVERLAP_S)
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

    def flaky(who, text, key=""):
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


# -- the outbox: a busy daemon is a delay, not a loss -------------------------

class Recorder:
    """A channel that can be busy or refuse, so a pass can be driven through both.

    `busy` is how many sends go unanswered (a turn holds the daemon); `fail` is an exception
    every send raises until it is cleared. What it *took* is `messages`, and `keys` is what
    the daemon would have deduplicated on.
    """

    def __init__(self, *, busy: int = 0, fail: Exception | None = None):
        self.messages: list[tuple[str, str]] = []
        self.keys: list[str] = []
        self.attempts = 0
        self.busy = busy
        self.fail = fail

    def __call__(self, who: str, text: str, key: str = "") -> None:
        self.attempts += 1
        self.keys.append(key)
        if self.busy:
            self.busy -= 1
            raise ngit.NotifyBusy("the daemon did not answer within 20s")
        if self.fail is not None:
            raise self.fail
        self.messages.append((who, text))


def test_a_busy_daemon_leaves_the_notice_queued_without_an_error(home, store):
    """The acceptance case: a turn holds the daemon, and the notice waits instead of dying."""
    pr = make_pr("d1", created_at=5000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        busy = made.sync()
        assert busy.posted == 0
        assert busy.prs["pending"] == 1
        # A busy daemon is not an outage: the notice is queued, so nothing was lost.
        assert busy.errors == [] and busy.failed == 0
        assert any("queued for the next pass" in note for note in busy.notes)
        assert channel.messages == []
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "pending"

        quiet = made.sync()
        assert quiet.posted == 1
        assert quiet.prs["signalled"] == 1
        assert quiet.prs["pending"] == 0
        assert quiet.errors == []
        assert len(channel.messages) == 1 and pr.subject in channel.messages[0][1]

        again = made.sync()
        assert again.posted == 0
        assert again.prs["existing"] == 1
        assert len(channel.messages) == 1
    finally:
        made.close()


def test_a_retry_carries_the_same_key_so_a_landed_notice_is_not_reposted(home, store):
    """The daemon may have posted it and lost the acknowledgement: the key is what stops
    the retry becoming a second message."""
    pr = make_pr("d2", created_at=5000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        made.sync()
        made.sync()
        assert len(channel.keys) == 2
        assert channel.keys[0] == channel.keys[1]
        # The key names the queued notice, so the daemon's receipt can be traced back to it.
        assert channel.keys[0].startswith("ngit:")
        assert pr.id[:12] in channel.keys[0]
    finally:
        made.close()


def test_a_queued_notice_is_delivered_when_the_relay_has_nothing_new(home, store):
    """The cursor moves past a PR whose notice is only queued: the queue, not the relay
    window, is what a later pass delivers from."""
    pr = make_pr("d5", created_at=5000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        first = made.sync()
        assert first.prs["pending"] == 1
        assert made.store.ngit_cursor(ADDRESS, "prs") == str(5000 - ngit.CURSOR_OVERLAP_S)

        made.prs_data = ([], ())
        second = made.sync()
        assert made.since_seen[-1] == 5000 - ngit.CURSOR_OVERLAP_S
        assert second.posted == 1 and second.prs["pending"] == 0
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "signalled"
        assert len(channel.messages) == 1
    finally:
        made.close()


def test_a_busy_pass_waits_on_one_notice_and_leaves_the_rest(home, store):
    """Three notices, one timeout: the daemon has already ignored one for the whole
    timeout, so the others stay queued rather than each costing another."""
    prs = [make_pr(f"e{index}", created_at=1000 + index) for index in range(3)]
    channel = Recorder(busy=99)
    made = Harness(home, store, prs=(prs, ()), notifier=channel)
    try:
        report = made.sync()
        assert channel.attempts == 1
        assert report.prs["pending"] == 3
        assert report.errors == []
    finally:
        made.close()


def test_a_merge_supersedes_a_review_line_that_never_went_out(home, store):
    pr = make_pr("d4", created_at=1000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], []), notifier=channel)
    try:
        made.sync()
        assert made.store.ngit_notice_count(ADDRESS, kind=ngit.KIND_PR) == 1

        merge = ngit.Merge(pr_id=pr.id, commit="a" * 40, created_at=1100)
        made.prs_data = ([pr], [merge])
        report = made.sync()

        assert report.prs["merged"] == 1
        assert len(channel.messages) == 1
        assert "merged as " + "a" * 40 in channel.messages[0][1]
        assert "ready for review" not in channel.messages[0][1]
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "merged"
    finally:
        made.close()


def test_a_refusal_is_an_error_and_the_notice_is_retried_after_the_fix(home, store):
    pr = make_pr("d6", created_at=5000)
    channel = Recorder(fail=ngit.NotifyError("no readable Concord channel for 'concord:x:y'"))
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        refused = made.sync()
        assert refused.failed == 1 and refused.errors
        assert "not delivered" in refused.errors[0]
        assert refused.prs["pending"] == 1
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "pending"

        channel.fail = None
        fixed = made.sync()
        assert fixed.posted == 1 and fixed.errors == []
        assert len(channel.messages) == 1
    finally:
        made.close()


def test_a_busy_daemon_does_not_lose_an_issue_notice(home, store):
    """The same rule on the inbound half: the task is created, and its notice waits."""
    issue = make_issue("d7")
    channel = Recorder(busy=1)
    made = Harness(home, store, issues=[issue], notifier=channel)
    made.details = {issue.hex: issue}
    try:
        first = made.sync()
        assert first.issues["created"] == 1
        assert first.errors == []
        assert channel.messages == []

        second = made.sync()
        assert second.posted == 1
        assert len(channel.messages) == 1
        assert "->" in channel.messages[0][1]
        assert made.store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "ready"
    finally:
        made.close()


def test_a_relay_failure_does_not_hold_back_a_queued_notice(home, store):
    """Delivery is its own step: a relay that will not answer cannot keep a notice back."""
    pr = make_pr("d9", created_at=5000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        made.sync()

        def broken(since):
            raise ngit.NgitError("relay read failed")

        made.prs = broken
        report = made.sync()

        assert "prs: relay read failed" in report.errors
        assert report.posted == 1 and len(channel.messages) == 1
        assert report.prs["pending"] == 0
    finally:
        made.close()


def test_a_dry_run_reports_the_queue_and_posts_nothing(home, store):
    pr = make_pr("da", created_at=5000)
    channel = Recorder(busy=1)
    made = Harness(home, store, prs=([pr], ()), notifier=channel)
    try:
        made.sync()
        report = made.sync(dry_run=True)
        assert report.posted == 0
        assert report.prs["pending"] == 1
        assert channel.messages == []
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "pending"
        # A dry run leaves the queue as it found it.
        assert made.store.ngit_notice_count(ADDRESS, kind=ngit.KIND_PR) == 1
    finally:
        made.close()


def test_a_pending_link_with_no_notice_is_requeued(home, store):
    """`pending` with nothing in the queue is a promise no pass could keep: make it again."""
    pr = make_pr("db", created_at=5000)
    channel = Recorder()
    made = Harness(home, store, prs=((), ()), notifier=channel)
    try:
        made.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr.id, board=BOARD, state="pending",
                                  detail={"nevent": pr.nevent, "subject": pr.subject,
                                          "author": pr.author, "branch": pr.branch})
        report = made.sync()

        assert report.posted == 1
        assert len(channel.messages) == 1 and pr.subject in channel.messages[0][1]
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "signalled"
        assert any("requeued" in note for note in report.notes)
    finally:
        made.close()


def test_a_deleted_cursor_starts_from_now_not_from_the_lookback(home, store):
    """A reset must not replay a week of PRs into the channel."""
    made = Harness(home, store, prs=([make_pr("d8", created_at=5000)], ()))
    made.config = ngit.BridgeConfig(assignee="default", lookback_hours=168)
    try:
        made.sync()
        before = int(time.time())
        assert before - 168 * 3600 - 2 <= made.since_seen[0] <= before - 168 * 3600 + 2

        made.store.db.execute("DELETE FROM ngit_cursors WHERE repo = ? AND stream = 'prs'",
                              (ADDRESS,))
        made.prs_data = ([], ())
        made.sync()
        assert before - ngit.CURSOR_OVERLAP_S - 2 <= made.since_seen[-1] <= before + 2
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


# -- the notifier: what a busy daemon looks like from here --------------------

class FakeDaemon:
    """A socket that answers like a daemon: busy (holds), refusing, or working, in turn."""

    def __init__(self, path: Path, *, holds: list[float] | None = None,
                 replies: list[dict | None] | None = None):
        self.path = Path(path)
        self.holds = list(holds or [])
        self.replies = list(replies or [])
        self.requests: list[dict] = []
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._thread: threading.Thread | None = None

    def start(self) -> "FakeDaemon":
        self._listener.bind(str(self.path))
        self._listener.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self) -> None:
        """One thread an accept: a held request must not block the next one."""
        for index in range(max(len(self.holds), len(self.replies)) or 1):
            conn, _ = self._listener.accept()
            threading.Thread(target=self._handle, args=(conn, index), daemon=True).start()

    def _handle(self, conn: socket.socket, index: int) -> None:
        try:
            self.requests.append(json.loads(conn.recv(1 << 16).decode().strip()))
            hold = self.holds[index] if index < len(self.holds) else 0.0
            if hold:
                time.sleep(hold)
            reply = self.replies[index] if index < len(self.replies) else None
            if reply is not None:
                conn.sendall((json.dumps(reply) + "\n").encode())
        except OSError:
            pass          # the caller gave up first, which is the point of a hold
        finally:
            conn.close()


def test_a_daemon_that_does_not_answer_is_busy_not_a_failure(tmp_path):
    """A turn holds the daemon; the timeout has to read as `busy` or every turn is an outage."""
    daemon = FakeDaemon(tmp_path / "busy.sock", holds=[1.0]).start()
    with pytest.raises(ngit.NotifyBusy) as caught:
        ngit.DaemonNotifier(daemon.path, timeout=0.2)("concord:ab:cd", "notice", "ngit:7")
    assert "did not answer" in str(caught.value)


def test_a_missing_daemon_is_a_failure_not_a_delay(tmp_path):
    with pytest.raises(ngit.NotifyError) as caught:
        ngit.DaemonNotifier(tmp_path / "absent.sock", timeout=0.2)("concord:ab:cd", "notice")
    assert not isinstance(caught.value, ngit.NotifyBusy)


def test_a_refusal_carries_what_the_daemon_said(tmp_path):
    daemon = FakeDaemon(tmp_path / "no.sock",
                        replies=[{"type": "error", "error": "no readable Concord channel"}]).start()
    with pytest.raises(ngit.NotifyError) as caught:
        ngit.DaemonNotifier(daemon.path, timeout=2.0)("concord:ab:cd", "notice")
    assert "no readable Concord channel" in str(caught.value)


def test_the_notice_key_travels_as_the_frame_id(tmp_path):
    """The daemon dedupes on the frame's `id`; the bridge has to put the key there."""
    daemon = FakeDaemon(tmp_path / "ok.sock",
                        replies=[{"type": "delivery", "ok": True, "message_id": "r1"}]).start()
    ngit.DaemonNotifier(daemon.path, timeout=2.0)("concord:ab:cd", "notice", "ngit:7:abcdef:ready")
    assert daemon.requests == [{"op": "send", "to": "concord:ab:cd", "text": "notice",
                                "id": "ngit:7:abcdef:ready"}]


def test_the_acceptance_run_against_a_real_socket(home, store, tmp_path):
    """Busy mid-turn, then quiet: one pending notice, one delivery, then nothing.

    The same three passes as the Recorder test, but through the real notifier and a real
    socket, because the classification of a timeout is the part that was wrong.
    """
    pr = make_pr("dc", created_at=5000)
    daemon = FakeDaemon(tmp_path / "live.sock", holds=[0.6, 0.0],
                        replies=[None, {"type": "delivery", "ok": True,
                                        "message_id": "r1"}]).start()
    made = Harness(home, store, prs=([pr], ()),
                   notifier=ngit.DaemonNotifier(daemon.path, timeout=0.2))
    try:
        busy = made.sync()
        assert busy.posted == 0 and busy.prs["pending"] == 1 and busy.errors == []

        quiet = made.sync()
        assert quiet.posted == 1 and quiet.prs["signalled"] == 1

        again = made.sync()
        assert again.posted == 0 and again.prs["existing"] == 1
        # Two sends in three passes, and the retry carried the first one's key.
        assert len(daemon.requests) == 2
        assert daemon.requests[0]["id"] == daemon.requests[1]["id"]
    finally:
        made.close()


# -- a PR lands with a review task --------------------------------------------

def review_tasks(harness) -> list[dict]:
    return [task for task in harness.tasks()
            if str(task["idempotency_key"] or "").startswith("ngit:pr:")]


def test_a_new_pr_gets_one_review_task_and_one_message(harness):
    pr = make_pr("a7")
    harness.prs_data = ([pr], [])

    report = harness.sync()

    tasks = review_tasks(harness)
    assert len(tasks) == 1
    task = tasks[0]
    assert task["idempotency_key"] == f"ngit:pr:{pr.id}"
    assert task["title"] == f"Review PR {pr.id[:8]}: {pr.subject}"
    assert task["status"] == "ready"
    assert task["assignee"] == "default"
    # The body names the PR and where it comes from, so a worker needs nothing else.
    for expected in (pr.id, pr.nevent, ADDRESS, pr.author, pr.branch, "master"):
        assert expected in task["body"], expected
    # ... and carries the brief: read the diff, run the gates, report, never merge.
    assert "ngit pr checkout" in task["body"]
    assert "git diff master...HEAD" in task["body"]
    assert "ruff check" in task["body"] and "pytest" in task["body"]
    assert "Do not merge" in task["body"]
    # One message, and it names the task instead of describing it twice.
    assert len(harness.messages) == 1
    text = harness.messages[0][1]
    assert "ready for review" in text and pr.nevent in text
    assert f"review task {task['id']}" in text
    assert report.prs["signalled"] == 1 and report.prs["tasks"] == 1
    assert report.errors == []
    # The link points at the task, which is what a merge later closes.
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["task_id"] == task["id"]


def test_a_second_pass_makes_neither_a_task_nor_a_message(harness):
    pr = make_pr("a7")
    harness.prs_data = ([pr], [])
    harness.sync()
    tasks, messages = harness.tasks(), list(harness.messages)

    report = harness.sync()

    assert harness.tasks() == tasks
    assert harness.messages == messages
    assert report.prs["tasks"] == 0 and report.posted == 0


def test_the_review_task_goes_to_the_review_profile_and_priority(home, store):
    pr = make_pr("b3")
    made = Harness(home, store, prs=([pr], []))
    made.config = ngit.BridgeConfig(assignee="default", lookback_hours=0,
                                    review_assignee="reviewer", priority=7)
    try:
        made.sync()
        task = review_tasks(made)[0]
        assert task["assignee"] == "reviewer"
        assert task["priority"] == 7
    finally:
        made.close()


def test_a_review_task_carries_no_model_of_its_own(harness):
    """The bridge names the profile and stops: the board's default, then the profile, decides.

    A per-task override is the first thing the dispatcher honours, so writing one here would
    outrank both -- and a review pinned to a model that hits a quota wall becomes a requeue
    loop nobody is watching.
    """
    pr = make_pr("b4")
    harness.prs_data = ([pr], [])
    harness.sync()

    task = review_tasks(harness)[0]
    assert task["model_override"] is None
    assert task["provider_override"] is None
    # Still the review task, still on its profile: only the model knob is gone.
    assert task["assignee"] == "default"


def test_review_config_defaults():
    default = ngit.BridgeConfig.from_config({})
    assert default.review_assignee == "default"
    assert default.reviews is True

    chosen = ngit.BridgeConfig.from_config({"review_assignee": "reviewer"})
    assert chosen.review_assignee == "reviewer"


def test_a_merge_completes_the_review_task(harness):
    pr = make_pr("b6", created_at=1000)
    harness.prs_data = ([pr], [])
    harness.sync()
    task = review_tasks(harness)[0]

    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1100)
    harness.prs_data = ([pr], [merge])
    report = harness.sync()

    done = harness.task(task["id"])
    assert done["status"] == "done" and done["completed_at"]
    assert "merged as " + "d" * 40 in str(done["result"])
    comment = [c["body"] for c in harness.comments(task["id"])][-1]
    assert "merged as " + "d" * 40 in comment and pr.subject in comment
    # The merge line is still posted, and the task did not cost it.
    assert len(harness.messages) == 2
    assert "merged as " + "d" * 40 in harness.messages[1][1]
    assert report.prs["merged"] == 1

    # A merge that arrives for a task already completed is a no-op.
    before = [c["body"] for c in harness.comments(task["id"])]
    harness.sync()
    assert len(harness.messages) == 2
    assert [c["body"] for c in harness.comments(task["id"])] == before
    assert harness.task(task["id"])["status"] == "done"


def test_a_merge_that_could_not_close_the_task_closes_it_on_the_next_poll(harness):
    pr = make_pr("c4", created_at=1000)
    harness.prs_data = ([pr], [])
    harness.sync()
    task = review_tasks(harness)[0]

    real_complete = harness.board.complete
    failures = {"left": 1}

    def flaky_complete(task_id, **kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise ngit.BoardError("could not write to the board: database is locked")
        return real_complete(task_id, **kwargs)

    harness.board.complete = flaky_complete
    merge = ngit.Merge(pr_id=pr.id, commit="f" * 40, created_at=1100)
    harness.prs_data = ([pr], [merge])

    first = harness.sync()
    # The channel still gets the merge line; the failure is reported, not swallowed.
    assert "merged as " + "f" * 40 in harness.messages[-1][1]
    assert first.prs["review_errors"] == 1
    assert any("database is locked" in error for error in first.errors)
    assert harness.task(task["id"])["status"] != "done"

    second = harness.sync()
    assert second.prs["review_errors"] == 0
    assert harness.task(task["id"])["status"] == "done"
    # ... and the merge line was not posted a second time.
    assert sum("merged as" in text for _, text in harness.messages) == 1


def test_a_pr_first_seen_merged_gets_one_message_and_no_review_task(harness):
    pr = make_pr("b7", created_at=1000)
    merge = ngit.Merge(pr_id=pr.id, commit="e" * 40, created_at=1050)
    harness.prs_data = ([pr], [merge])

    harness.sync()

    # There is nothing to review: it is already merged, and nobody is asked to restate it.
    assert review_tasks(harness) == []
    assert len(harness.messages) == 1
    assert "merged as " + "e" * 40 in harness.messages[0][1]


def test_a_task_that_cannot_be_made_still_posts_the_notice(home, store):
    pr = make_pr("b8")
    made = Harness(home, store, prs=([pr], []))
    real_create = made.board.create
    failures = {"left": 1}

    def flaky_create(**kwargs):
        if failures["left"]:
            failures["left"] -= 1
            raise ngit.BoardError("could not create a task on /boards/x: database is locked")
        return real_create(**kwargs)

    made.board.create = flaky_create
    try:
        report = made.sync()

        # The channel still hears about the PR, and why no task came with it.
        assert len(made.messages) == 1
        text = made.messages[0][1]
        assert "ready for review" in text and pr.nevent in text
        assert "no review task" in text and "database is locked" in text
        assert review_tasks(made) == []
        assert report.prs["review_errors"] == 1
        assert any("database is locked" in error for error in report.errors)
        # Nothing was stepped over: the cursor may not move past a PR still missing its task.
        assert made.store.ngit_cursor(ADDRESS, "prs") is None

        # The next poll makes the task -- and does not repeat the message it already sent.
        again = made.sync()
        assert len(made.messages) == 1
        task = review_tasks(made)[0]
        assert again.prs["tasks"] == 1 and again.prs["review_errors"] == 0
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["task_id"] == task["id"]
    finally:
        made.close()


def test_a_dry_run_writes_no_review_task(harness):
    pr = make_pr("c2")
    harness.prs_data = ([pr], [])

    harness.sync(dry_run=True)

    assert harness.tasks() == []
    assert harness.messages == []


def test_reviews_can_be_turned_off_without_losing_the_pr_message(home, store):
    pr = make_pr("c3")
    made = Harness(home, store, prs=([pr], []))
    made.config = ngit.BridgeConfig(assignee="default", lookback_hours=0, reviews=False)
    try:
        made.sync()
        assert review_tasks(made) == []
        assert len(made.messages) == 1
        assert "ready for review" in made.messages[0][1]
    finally:
        made.close()


def test_board_task_done_is_written_once(home):
    board = Board(home / "kanban" / "boards" / BOARD / "kanban.db")
    try:
        task_id = board.create(title="Review PR", body="", assignee="default",
                               status="ready")
        assert board.complete(task_id, author="ngit", comment="merged as abc") is True
        row = board.task(task_id)
        assert row["status"] == "done" and row["completed_at"] and row["result"]
        assert [c["body"] for c in board.comments(task_id)] == ["merged as abc"]

        assert board.complete(task_id, author="ngit", comment="merged again") is False
        assert [c["body"] for c in board.comments(task_id)] == ["merged as abc"]
    finally:
        board.close()


def test_default_branch_comes_from_the_checkout(tmp_path):
    repo = tmp_path / "checkout"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    # What a clone knows: the remote's HEAD names the branch PRs target.
    subprocess.run(["git", "-C", str(repo), "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/trunk"], check=True)
    assert ngit.default_branch(repo) == "trunk"


def test_default_branch_says_nothing_where_git_cannot(tmp_path):
    assert ngit.default_branch(tmp_path) == ""


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


def test_resolve_target_finds_a_scoped_entry_by_the_bare_channel_id(tmp_path):
    """A timer instance is named by the id alone, so the id must resolve a scoped entry."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "community": COMMUNITY, "path": str(repo),
         "project": PROJECT, "board": BOARD},
    ]}}

    target = ngit.resolve_target(cfg, channel=CHANNEL)

    assert target.repo == repo and target.board == BOARD
    # A notice is addressed by the whole conversation, so the id is expanded before the
    # target is returned: the pass was never told the community, the entry was.
    assert target.conversation == f"concord:{COMMUNITY}:{CHANNEL}"


def test_resolve_target_refuses_a_bare_id_that_cannot_name_its_conversation(tmp_path):
    """No `community:` and only an id: the notice could not be addressed, so say so first."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "path": str(repo), "project": PROJECT, "board": BOARD},
    ]}}

    with pytest.raises(ngit.NgitError, match="cannot address the channel"):
        ngit.resolve_target(cfg, channel=CHANNEL)


def test_resolve_target_keeps_the_community_scoping_for_a_conversation(tmp_path):
    """A conversation naming another community must not borrow this entry by its id."""
    repo = tmp_path / "checkout"
    repo.mkdir()
    cfg = {"concord": {"projects": [
        {"channel": CHANNEL, "community": COMMUNITY, "path": str(repo),
         "project": PROJECT, "board": BOARD},
    ]}}

    with pytest.raises(ngit.NgitError, match="no concord.projects entry"):
        ngit.resolve_target(cfg, channel=f"concord:{'ef' * 32}:{CHANNEL}")


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
                      "created_at": 7, "id": nevent(event_id("d3"), ngit.KIND_COMMENT),
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


def test_a_merged_pr_is_read_from_the_tag_ngit_publishes():
    """A kind-1631 as relay.ngit.dev carries it: alt, the root `e`, one `q`, a `p` per
    maintainer and author, an `a` per maintainer, the repository's `r`, then ngit's own
    `merge-commit-id` and the merge commit as a second `r` -- NIP-34's `merge-commit`
    spelling is never published by ngit 3.0.4."""
    merge_commit = "992291e33c9c1159ea53a52e9994a78f03ab80ee"
    merge = ngit.merge_from_event({
        "id": event_id("d8"), "pubkey": REPO_OWNER, "kind": ngit.KIND_APPLIED,
        "created_at": 30, "content": "",
        "tags": [
            ["alt", "git proposal merged / applied"],
            ["e", event_id("d9"), "wss://relay.ngit.dev", "root"],
            ["q", event_id("da")],
            ["p", REPO_OWNER], ["p", TRUSTED_HEX], ["p", STRANGER_HEX],
            ["a", ADDRESS],
            ["a", f"30617:{TRUSTED_HEX}:example-repo"],
            ["a", f"30617:{STRANGER_HEX}:example-repo"],
            ["r", "f" * 40],
            ["merge-commit-id", merge_commit],
            ["r", merge_commit],
        ]})

    assert merge is not None
    assert merge.pr_id == event_id("d9")
    assert merge.commit == merge_commit


def test_an_applied_patch_names_the_first_commit_it_was_replayed_as():
    """ngit switches the tag to `applied-as-commits` when it replays a patch under fresh
    commit ids; there is no merge commit to name, and the first commit is the one used."""
    merge = ngit.merge_from_event({
        "id": event_id("db"), "pubkey": REPO_OWNER, "kind": ngit.KIND_APPLIED,
        "created_at": 40, "content": "",
        "tags": [["e", event_id("dc"), "", "root"],
                 ["applied-as-commits", "1" * 40, "2" * 40],
                 ["r", "1" * 40], ["r", "2" * 40]]})

    assert merge is not None
    assert merge.pr_id == event_id("dc") and merge.commit == "1" * 40


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


def test_run_ngit_names_the_binary_and_the_floor_when_it_is_missing(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        raise FileNotFoundError("ngit")

    monkeypatch.setattr(ngit.subprocess, "run", fake_run)
    with pytest.raises(ngit.NgitError) as raised:
        ngit.run_ngit(tmp_path, ["issue", "list"])
    message = str(raised.value)
    # "ngit is not installed" is not actionable: name the binary, the floor and the fix.
    assert ngit.NGIT_FLOOR in message
    assert "ngit" in message
    assert "PATH" in message
    assert "is not installed" not in message


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


# -- the host is described by the test, never measured -----------------------
#
# CI has no `ngit` on PATH, so a test about the preflight's message, its exit code or its
# ordering stubs `probe_ngit` and leaves the host out of it. The suite must pass on a
# machine with no `ngit` at all.

def below_floor() -> str:
    """One patch release under the floor, derived so a floor bump does not rot this."""
    parts = ngit.parse_version(ngit.NGIT_FLOOR)
    return ".".join(str(part) for part in (*parts[:-1], parts[-1] - 1))


def sync_with_probe(tmp_path, monkeypatch, probe: ngit.NgitProbe) -> int:
    """`hng ngit sync` with a valid config and the host lookup replaced by `probe`."""
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setattr(ngit, "probe_ngit", lambda binary: probe)
    config = write_config(tmp_path, [{"channel": CHANNEL, "path": str(tmp_path),
                                      "project": PROJECT, "board": BOARD}])
    return cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])


def test_cli_refuses_a_host_with_no_ngit_at_all(home, tmp_path, monkeypatch, capsys):
    assert sync_with_probe(tmp_path, monkeypatch, ngit.NgitProbe()) == 2
    err = capsys.readouterr().err
    assert "`ngit`" in err
    assert f"ngit >= {ngit.NGIT_FLOOR}" in err
    assert ngit.NGIT_INSTALL_HINT in err


def test_cli_refuses_an_ngit_below_the_floor(home, tmp_path, monkeypatch, capsys):
    old = below_floor()
    probe = ngit.NgitProbe(path="/opt/ngit/bin/ngit", stdout=f"ngit {old}\n")
    assert sync_with_probe(tmp_path, monkeypatch, probe) == 2
    err = capsys.readouterr().err
    assert f"ngit {old}" in err
    assert f">= {ngit.NGIT_FLOOR}" in err
    assert ngit.NGIT_INSTALL_HINT in err


def test_cli_preflight_names_the_binary_and_the_floor_on_a_real_host(
        home, tmp_path, monkeypatch, capsys):
    """No stub at all: PATH holds no `ngit`, so this is the message a real host shows."""
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    monkeypatch.delenv("HNG_NGIT", raising=False)
    config = write_config(tmp_path, [{"channel": CHANNEL, "path": str(tmp_path),
                                      "project": PROJECT, "board": BOARD}])
    code = cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])
    assert code == 2
    err = capsys.readouterr().err
    assert "`ngit` was not found on PATH" in err
    assert f"ngit >= {ngit.NGIT_FLOOR}" in err
    assert "cargo install ngit" in err and "ngit update" in err
    assert "not installed" not in err


@pytest.mark.parametrize("projects, assignee, expected", [
    ([{"channel": CHANNEL, "path": "", "project": PROJECT, "board": BOARD}], None,
     "ngit.assignee"),
    ([], "default", "no concord.projects entry"),
    ([{"channel": CHANNEL, "path": "", "project": PROJECT, "board": "no-such-board"}],
     "default", "no board database"),
])
def test_cli_validates_the_config_before_it_reads_the_host(
        home, tmp_path, monkeypatch, capsys, projects, assignee, expected):
    """A bad config is the operator's to fix: the host is not consulted at all."""

    def forbidden(binary):
        raise AssertionError("the host was probed before the config was validated")

    monkeypatch.setattr(ngit, "probe_ngit", forbidden)
    monkeypatch.setenv("HNG_TEST_SECRET", "44" * 32)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    for project in projects:
        project["path"] = str(tmp_path)
    config = write_config(tmp_path, projects, assignee=assignee)
    code = cli.main(["--config", config, "ngit", "sync",
                     "--channel", f"concord:{COMMUNITY}:{CHANNEL}"])
    assert code == 2
    assert expected in capsys.readouterr().err


def test_ngit_status_is_one_judgement_on_the_probe(monkeypatch):
    monkeypatch.setattr(ngit, "probe_ngit",
                        lambda binary: ngit.NgitProbe(path="/opt/ngit/bin/ngit",
                                                      stdout=f"ngit {ngit.NGIT_FLOOR}\n"))
    status = ngit.ngit_status()
    assert status.usable
    assert status.path == "/opt/ngit/bin/ngit"
    assert status.version == ngit.NGIT_FLOOR


@pytest.mark.parametrize("probe, expected", [
    (ngit.NgitProbe(path="/opt/ngit/bin/ngit", error="Permission denied"),
     "could not be run"),
    (ngit.NgitProbe(path="/opt/ngit/bin/ngit", stdout="ngit\n"), "did not name a version"),
    (ngit.NgitProbe(path="/opt/ngit/bin/ngit", stdout="ngit 3\n", stderr="boom", code=1),
     "did not name a version"),
])
def test_ngit_status_names_the_fix_when_the_binary_is_unusable(monkeypatch, probe, expected):
    monkeypatch.setattr(ngit, "probe_ngit", lambda binary: probe)
    status = ngit.ngit_status()
    assert not status.usable
    assert expected in status.problem
    assert ngit.NGIT_FLOOR in status.problem
    assert ngit.NGIT_INSTALL_HINT in status.problem

