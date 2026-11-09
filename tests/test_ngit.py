"""The ngit bridge: issues as work, PRs announced, and the things that must not happen.

Placeholders only: keys, channel ids, repo names and board slugs here are invented.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import cli, ngit, ngit_labels, pr_status
from hermes_nostr_gateway.applied import Evidence
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
SELF_HEX = "44" * 32
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


def make_comment(seed: str, *, author: str = TRUSTED_HEX, body: str = "Same here.",
                 created_at: int = 1) -> ngit.Comment:
    identifier = event_id(seed)
    return ngit.Comment(id=nevent(identifier, kind=ngit.KIND_COMMENT),
                        hex=identifier, author=npub(author), body=body,
                        created_at=created_at)


def make_pr(seed: str, *, author: str = TRUSTED_HEX, subject: str = "Add a widget",
            branch: str = "pr/widget", created_at: int = 1000,
            commit: str = "") -> ngit.PullRequest:
    identifier = event_id(seed)
    return ngit.PullRequest(id=identifier, author=npub(author), subject=subject,
                            branch=branch, created_at=created_at, commit=commit,
                            nevent=nevent(identifier, kind=ngit.KIND_PR, author=author))


def bridge_config(**kwargs) -> ngit.BridgeConfig:
    """A BridgeConfig with the allowlists these fixtures assume, unless a test overrides."""
    kwargs.setdefault("maintainers", (TRUSTED_HEX,))
    kwargs.setdefault("reviewers", (TRUSTED_HEX,))
    return ngit.BridgeConfig(**kwargs)


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


class FakeApplied:
    """A checkout stand-in for `applied.Checkout`: an answer per tip, and what was asked.

    `head` is the base tip the pass stamps its answers with; a test that wants a second
    question answered moves it (which is what a merge does) rather than reaching into the
    link row.
    """

    def __init__(self, answers: dict | None = None, *, base: str = "base0001",
                 error: Exception | None = None):
        self.answers = dict(answers or {})
        self.base = base
        self.error = error
        self.probes: list[str] = []
        self.heads = 0

    def head(self) -> str:
        self.heads += 1
        if self.error is not None:
            raise self.error
        return self.base

    def probe(self, tip: str):
        self.probes.append(tip)
        if self.error is not None:
            raise self.error
        return self.answers.get(
            tip, Evidence(False, "no commit on master carries its patch"
                                 " (1 compared against 2 since 9b52622)"))

class _Reader:
    """The relay read the way production supplies it: callable, and able to serve comments.

    `RelayLabels` is both (`__call__` = `read`, plus `comments` for the raw events a mention
    needs). One object here keeps the harness honest about that shape.
    """

    def __init__(self, harness: "Harness"):
        self.harness = harness

    def __call__(self, ids):
        return self.harness._read(ids)

    def read(self, ids):
        return self.harness._read(ids)

    def issue_labels(self, ids):
        return self.harness._issue_labels(ids)

    def comments(self, ids):
        return self.harness._comments(ids)


class Harness:
    """A sync with every external collaborator replaced by something a test can see."""

    def __init__(self, home: Path, store: Store, *, issues=(), trusted=(TRUSTED_HEX,),
                 assignee: str = "default", anchor: Anchor | None = None,
                 notifier=None, prs=((), ()), statuses=None, labeler=None, state=None,
                 applied: FakeApplied | None = None, sweep=None):
        self.home = home
        self.store = store
        self.board = Board(home / "kanban" / "boards" / BOARD / "kanban.db")
        self.messages: list[tuple[str, str]] = []
        self.issues = list(issues)
        self.details: dict[str, ngit.Issue] = {}
        self.detail_calls: list[str] = []
        self.prs_data = prs
        #: The second read: statuses for the PR ids the store already links, by `#e`.
        self.statuses = statuses
        self.since_seen: list[int] = []
        #: The trailing status sweep's source. Empty unless a test says otherwise; an
        #: exception instance makes it raise, which is how the best-effort half is pinned.
        self.sweep_data = sweep if sweep is not None else ngit.PrFeed()
        self.sweep_since: list[int] = []
        self.label_calls: list[tuple[str, str, tuple[str, ...], tuple[str, ...]]] = []
        self.labeler = labeler or self._label
        #: The checkout's answer about a proposal whose status record says nothing.
        self.applied = applied or FakeApplied()
        #: What the relays say about a PR: the verdict comment and the labels it carries.
        self.verdicts: dict[str, ngit_labels.Verdict] = {}
        self.carried: dict[str, tuple[ngit_labels.Label, ...]] = {}
        self.readings: dict[str, tuple[tuple[str, str, int], ...]] = {}
        self.read_calls: list[tuple[str, ...]] = []
        #: Issues with no trusted `ready` label, and issues whose label set is spelled out.
        self.unready: set[str] = set()
        self.ready_labels: dict[str, tuple[ngit_labels.Label, ...]] = {}
        #: Raw comments per root: what a mention pass reads.
        self.comments_by_root: dict[str, list[dict]] = {}
        self.comment_reads: list[tuple[str, ...]] = []
        self.issue_label_reads: list[tuple[str, ...]] = []
        self.state = state or _Reader(self)
        self.control = ControlConfig.from_config(
            {"trusted_npubs": [npub(key) for key in trusted]})
        self.config = bridge_config(assignee=assignee, lookback_hours=0,
                                    reply_secret_file="/etc/example/reply.json")
        self.anchor = anchor
        self.self_npub = SELF_HEX
        self.notifier = notifier or (lambda who, text, key="": self.messages.append((who, text)))
        self.repo = home.parent / "checkout"
        self.repo.mkdir(exist_ok=True)
        self.target = ngit.Target(conversation=f"concord:{COMMUNITY}:{CHANNEL}",
                                  repo=self.repo, board=BOARD, project=PROJECT,
                                  address=ADDRESS, default_branch="master")

    def detail(self, ref: ngit.Issue) -> ngit.Issue:
        self.detail_calls.append(ref.hex)
        return self.details.get(ref.hex, ref)

    @staticmethod
    def _signed(item, owner: str = ""):
        """Give a fixture status/revision the author the gate expects, when it names none.

        The fixtures are about the *consequence* of an event, not its signature; a test
        about a forged author sets one explicitly. Blank would otherwise mean "from
        nobody", which the production rule rightly refuses. A revision/close defaults to
        the PR's own author, because that is the only key allowed to move or withdraw it.
        """
        if isinstance(item, ngit.Merge) and not item.author:
            return replace(item, author=TRUSTED_HEX)
        if isinstance(item, (ngit.Closed, ngit.Revision)) and not item.author:
            return replace(item, author=owner or TRUSTED_HEX)
        return item

    def prs(self, since: int):
        self.since_seen.append(since)
        # Tests set `prs_data` either as a PrFeed or as the older (prs, merges) pair; the
        # harness is the one place the shape is allowed to be loose.
        feed = self.prs_data if isinstance(self.prs_data, ngit.PrFeed) \
            else ngit.PrFeed(*self.prs_data)
        owners = {pr.id: pr.author for pr in feed.prs}
        return replace(feed, merges=[self._signed(m) for m in feed.merges],
                       closed=[self._signed(c, owners.get(c.pr_id, "")) for c in feed.closed],
                       revisions=[self._signed(r, owners.get(r.pr_id, ""))
                                  for r in feed.revisions])

    def sweep(self, since: int):
        """The trailing sweep's source: whatever `sweep_data` holds, or the exception to raise."""
        self.sweep_since.append(since)
        if isinstance(self.sweep_data, BaseException):
            raise self.sweep_data
        feed = self.sweep_data
        # The same author default as `prs()`: a status fixture names the consequence, not
        # its signature, and a swept feed carries no proposal to borrow an author from.
        return replace(feed, merges=[self._signed(m) for m in feed.merges],
                       closed=[self._signed(c) for c in feed.closed])

    def _issue_labels(self, ids):
        """What a label read returns for issues: the default trusted `ready`, or a test's set."""
        out = {}
        for issue_id in ids:
            labels = self.ready_labels.get(issue_id)
            if labels is None and issue_id not in self.unready:
                labels = (ngit_labels.Label(
                    value=self.config.ready_label or "ready", event_id="c" * 64,
                    author=TRUSTED_HEX),)
            out[issue_id] = labels or ()
        self.issue_label_reads.append(tuple(ids))
        return out

    def _read(self, pr_ids):
        """The relay read: whatever the test put in `verdicts`, `carried` and `readings`.

        An issue reads as carrying a `ready` label from the trusted npub unless the test says
        otherwise (`unready`, or an explicit `ready_labels` entry): the gate is the default
        behaviour, and a harness that had to opt in to it would make every old test pass for
        the wrong reason.
        """
        self.read_calls.append(tuple(pr_ids))
        issue_ids = {issue.hex for issue in self.issues} | set(self.details)
        issues = self._issue_labels([pr for pr in pr_ids if pr in issue_ids])
        prs = {}
        for pr in pr_ids:
            labels = self.carried.get(pr)
            if labels is None and pr in issues:
                labels = issues[pr]
            verdict = self.verdicts.get(pr)
            if verdict is not None and not verdict.author:
                # A verdict's author decides whether it counts, so the fixture's default is
                # the trusted key rather than nobody.
                verdict = replace(verdict, author=TRUSTED_HEX)
            prs[pr] = ngit.PrRead(verdict=verdict,
                                  labels=labels or (),
                                  readings=self.readings.get(pr, ()))
        return ngit.ReadState(prs=prs, relays=("wss://relay.example",))

    def _comments(self, ids):
        """Raw kind-1111 comments per root, as `RelayLabels.comments` returns them."""
        self.comment_reads.append(tuple(ids))
        grouped = {root: list(self.comments_by_root.get(root, ())) for root in ids
                   if self.comments_by_root.get(root)}
        return grouped, ("wss://relay.example",)

    def _label(self, pr_id: str, address: str, add, retract=()):
        """Record what the sync asked to publish, apply it to the fake relays, and land it.

        A published label has to become one the next read serves, and a retraction has to
        take its event away: the pass now decides from what the relays hold, so a fake that
        forgot its own writes would make every poll republish.
        """
        self.label_calls.append((pr_id, address, tuple(add), tuple(retract)))
        gone = set(retract)
        held = [label for label in self.carried.get(pr_id, ()) if label.event_id not in gone]
        events = []
        for value in add:
            event = f"l{len(self.label_calls):03d}{len(events)}".ljust(64, "0")
            events.append(event)
            held.append(ngit_labels.Label(value=value, event_id=event,
                                          created_at=2000 + len(self.label_calls),
                                          relay="wss://relay.example"))
        self.carried[pr_id] = tuple(held)
        return ngit_labels.LabelResult(ok=True, events=tuple(events), retracted=True, relays=2)

    def no_gate(self) -> None:
        """Turn the ready-label gate off, for the `held`/`asked` lifecycle it replaced."""
        self.config = ngit.BridgeConfig(assignee=self.config.assignee,
                                        lookback_hours=self.config.lookback_hours,
                                        ready_label="")

    def sync(self, **kwargs):
        return self._sync().run(**kwargs)

    def reconcile(self, **kwargs):
        return self._sync().reconcile(**kwargs)

    def _sync(self):
        return ngit.Sync(
            store=self.store, board=self.board, control=self.control,
            config=self.config, target=self.target,
            issues=lambda: list(self.issues), detail=self.detail,
            prs=self.prs, statuses=self.statuses, sweep=self.sweep,
            notify=self.notifier, anchor=self.anchor,
            labels=self.labeler, state=self.state, applied=self.applied,
            self_npub=self.self_npub)

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
    harness.no_gate()
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
    made.no_gate()
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
    first.no_gate()
    first.details = {issue.hex: issue}
    try:
        first.sync()
        assert len(first.messages) == 1
    finally:
        first.close()
        first_store.close()

    second_store = Store(store_path, identity="test")
    second = Harness(home, second_store, issues=[issue])
    second.no_gate()
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
    made.no_gate()
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
    made.no_gate()
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
    made.no_gate()
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
    """A `ready` label only authorises when its publisher is trusted in the concord plane."""
    issue = make_issue("d7", author=OTHER_HEX)
    control = ControlConfig.from_config(
        {"planes": {"nip17": {"trusted_npubs": [npub(OTHER_HEX)]}}})
    made = Harness(home, store, issues=[issue], trusted=())
    made.control = control
    made.details = {issue.hex: issue}
    made.ready_labels[issue.hex] = (ngit_labels.Label(
        value="ready", event_id="d" * 64, author=OTHER_HEX),)
    try:
        report = made.sync()
        assert made.tasks() == []
        assert report.issues["waiting"] == 1
    finally:
        made.close()


def test_concord_plane_trust_is_enough(home, store):
    issue = make_issue("d8", author=OTHER_HEX)
    made = Harness(home, store, issues=[issue], trusted=())
    made.control = ControlConfig.from_config(
        {"planes": {"concord": {"trusted_npubs": [npub(OTHER_HEX)]}}})
    made.details = {issue.hex: issue}
    made.ready_labels[issue.hex] = (ngit_labels.Label(
        value="ready", event_id="e" * 64, author=OTHER_HEX),)
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
    assert harness.comments(task_id)[0]["author"] == npub(TRUSTED_HEX)

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
    made.config = bridge_config(assignee="default", lookback_hours=0, max_new=1)
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
    made.config = bridge_config(assignee="default", lookback_hours=0, max_prs=2)
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
    made.config = bridge_config(assignee="default", lookback_hours=168)
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
    harness.config = bridge_config(assignee="default", lookback_hours=24)
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
    made.config = bridge_config(assignee="default", lookback_hours=0,
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


# -- `done` requires `applied` ------------------------------------------------

def guard_triggers(harness) -> list[str]:
    """The per-card completion triggers the bridge installed on the board."""
    rows = harness.board.db.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
        " AND name LIKE 'ngit_pr_done_%' ORDER BY name").fetchall()
    return [str(row[0]) for row in rows]


def test_an_open_pr_card_refuses_completion_at_the_board(harness):
    """The board itself refuses: the CLI and the agent's tool both write this UPDATE."""
    pr, task_id = pr_with_review_task(harness)

    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))

    message = str(refused.value)
    assert pr.id[:8] in message and "open at c0ffee" in message
    # The refusal names both ways a PR card is finished, so a reviewer is not told to park
    # the card as if the block were its end.
    assert "approving verdict" in message and "merge completes" in message
    assert harness.task(task_id)["status"] == "ready"


def test_a_card_with_no_pr_link_is_completed_as_before(harness):
    """Decision: ops and docs cards carry no link row, so the guard does not touch them."""
    pr_with_review_task(harness)  # a guard is installed for that PR's card
    plain = harness.board.create(title="Write the note", body="No PR here.",
                                 assignee="default", status="ready")

    assert harness.board.complete(plain, author="worker", result="written") is True
    assert harness.task(plain)["status"] == "done"


# -- `applied` has two witnesses: the record, and the content ------------------

#: What a pass says when the status record is silent and the patch is on the branch: the
#: shape `applied.Checkout` returns for an `ngit pr apply` merge.
APPLIED_BY_PATCH = Evidence(
    True, "its patch is already on origin/master as decdff5 (patch-id 25b99bf6)", commit="d" * 40)


def test_a_pr_applied_as_patches_completes_its_card_without_a_status_event(harness):
    """`ngit pr apply` re-signs the commits: no status names them, and the content is in.

    The card is finished by the same path a recorded merge takes, with the evidence on it --
    this is the case that used to refuse for ever (`PR <short> is open at <tip>`).
    """
    pr, task_id = pr_with_review_task(harness)
    # The merge moved master; that is what makes the question worth asking again.
    harness.applied.base = "base-after-merge"
    harness.applied.answers["c0ffee"] = APPLIED_BY_PATCH
    harness.prs_data = ([], [])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done"
    assert "applied as " + "d" * 40 in str(row["result"])
    assert guard_triggers(harness) == []
    assert report.prs["applied"] == 1
    assert any("applied without a status event" in note for note in report.notes)
    comment = next(c["body"] for c in harness.comments(task_id) if "applied as" in c["body"])
    assert "No applied status names it" in comment and "patch-id 25b99bf6" in comment
    # One line to the channel, naming what landed -- not a second "ready for review".
    assert len([text for _, text in harness.messages if "applied as" in text]) == 1

    # The evidence is recorded, so the next pass neither probes nor posts again.
    harness.prs_data = ([], [])
    before = len(harness.applied.probes)
    harness.sync()
    assert len(harness.applied.probes) == before
    assert len([text for _, text in harness.messages if "applied as" in text]) == 1


def test_an_open_pr_card_names_the_evidence_it_checked(harness):
    """The refusal says what was compared, so "open" is never a bare assertion."""
    pr, task_id = pr_with_review_task(harness)
    harness.applied.base = "base-after-nothing"
    harness.applied.answers["c0ffee"] = Evidence(
        False, "no commit on master carries its patch (1 compared against 4 since 9b52622)")
    harness.prs_data = ([], [])
    harness.sync()

    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))

    message = str(refused.value)
    assert pr.id[:8] in message and "open at c0ffee" in message
    assert "no applied status names it" in message
    assert "no commit on master carries its patch" in message
    guard = harness.board.guard_state(task_id)
    assert guard["state"] == "open"
    assert "no commit on master carries its patch" in str(guard["evidence"])


def test_an_applied_check_is_not_repeated_while_the_tip_and_base_stand_still(harness):
    """A pass with no news costs one `rev-parse`, not a probe per open PR."""
    pr, _task = pr_with_review_task(harness)
    # The first pass asks before it has a link to stamp the answer on.
    assert harness.applied.probes == ["c0ffee"]

    harness.prs_data = ([], [])
    harness.sync()  # this answer is stamped on the link
    assert harness.applied.probes == ["c0ffee", "c0ffee"]

    harness.sync()  # nothing moved: nothing to ask
    assert harness.applied.probes == ["c0ffee", "c0ffee"]

    # A new tip is a new question -- asked on the pass after the one that recorded it,
    # because the revision is written to the link after this check has run.
    harness.prs_data = ngit.PrFeed(
        revisions=[ngit.Revision(pr_id=pr.id, commit="c0ffe1", created_at=2000)])
    harness.sync()
    harness.sync()
    assert harness.applied.probes == ["c0ffee", "c0ffee", "c0ffe1"]


def test_a_dry_run_asks_the_checkout_and_writes_nothing(harness):
    """`--dry-run` may read the object graph; it may not stamp the answer on the link."""
    pr, _task = pr_with_review_task(harness)
    before = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["detail"]
    harness.prs_data = ([], [])

    harness.sync(dry_run=True)

    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["detail"] == before
    assert harness.applied.probes == ["c0ffee", "c0ffee"]  # asked, not remembered


def test_a_close_does_not_overrule_content_that_is_on_the_branch(harness):
    """A 1632 that arrives after the landing cannot un-apply it; the content is the proof."""
    pr, task_id = pr_with_review_task(harness)
    harness.applied.base = "base-after-merge"
    harness.applied.answers["c0ffee"] = APPLIED_BY_PATCH
    stopped = ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1200)
    harness.prs_data = ngit.PrFeed(closed=[stopped])

    harness.sync()

    assert harness.task(task_id)["status"] == "done"
    assert "closed without a merge" not in str(harness.task(task_id)["result"])


def test_a_closed_pr_with_nothing_on_master_still_needs_a_person(harness):
    """The close guard survives: a proposal that did not land is a decision, not a completion."""
    pr, task_id = pr_with_review_task(harness)
    harness.applied.base = "base-after-nothing"
    stopped = ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1200)
    harness.prs_data = ngit.PrFeed(closed=[stopped])

    harness.sync()

    assert harness.task(task_id)["status"] == "blocked"
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    message = str(refused.value)
    assert "closed without a merge" in message and "a human must decide" in message
    assert "9" * 16 in message  # the closing event, shortened as every id in a refusal is
    assert "no commit on master carries its patch" in message


def test_a_checkout_that_cannot_answer_does_not_stop_the_pass(harness):
    """An unreadable checkout is a sentence in the refusal, not a pass that dies."""
    pr, task_id = pr_with_review_task(harness)
    harness.applied = FakeApplied(error=OSError("the checkout is gone"))
    harness.prs_data = ([], [])

    report = harness.sync()

    assert harness.task(task_id)["status"] == "ready"
    assert guard_triggers(harness) == [f"ngit_pr_done_{task_id}"]
    assert any("the checkout could not be read" in error for error in report.errors)


def test_a_guard_table_from_before_the_evidence_column_is_migrated(tmp_path: Path):
    """The live board holds rows written by the previous shape: a guard call must still work."""
    path = tmp_path / "board.db"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(BOARD_SCHEMA)
        conn.execute(
            "CREATE TABLE ngit_pr_guards (task_id TEXT PRIMARY KEY, pr_id TEXT NOT NULL,"
            " state TEXT NOT NULL, tip TEXT, merge_commit TEXT, detail TEXT,"
            " updated_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO ngit_pr_guards VALUES ('t_old', ?, 'open', 'c0ffee', '', '', 1)",
                     ("f" * 64,))
        conn.execute("INSERT INTO tasks (id, title, status, created_at)"
                     " VALUES ('t_old', 'an old card', 'ready', 1)")
        conn.commit()
    finally:
        conn.close()

    board = Board(path)
    try:
        board.guard_pr("t_old", pr_id="f" * 64, state="open", tip="c0ffee",
                       evidence="no applied status names it")
        row = board.guard_state("t_old")
        assert row is not None and row["evidence"] == "no applied status names it"
        with pytest.raises(sqlite3.IntegrityError) as refused:
            board.db.execute("UPDATE tasks SET status = 'done' WHERE id = 't_old'")
        assert "no applied status names it" in str(refused.value)
    finally:
        board.close()


def test_a_link_whose_note_records_the_merge_repairs_its_stuck_guard(harness):
    """A merge seen once, then behind the cursor: the link's own note is the record."""
    pr, task_id = pr_with_review_task(harness)
    # A pass that saw the merge wrote it on the row, then could not finish the card (a
    # locked board); the event is now behind the cursor, so only the row knows.
    link = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)
    detail = json.loads(link["detail"])
    detail["merge"] = "a" * 40
    harness.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr.id, board=BOARD,
                                 state="signalled", task_id=task_id, detail=detail)
    assert guard_triggers(harness) == [f"ngit_pr_done_{task_id}"]

    harness.prs_data = ([], [])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done" and "a" * 40 in str(row["result"])
    assert guard_triggers(harness) == []
    assert report.prs["completed"] == 1


def test_a_merge_releases_the_guard_and_names_the_commit(harness):
    pr, task_id = pr_with_review_task(harness)
    assert guard_triggers(harness) == [f"ngit_pr_done_{task_id}"]

    merge = ngit.Merge(pr_id=pr.id, commit="a" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done"
    assert "a" * 40 in str(row["result"])
    # Nothing is left to refuse: the guard and its trigger are gone.
    assert guard_triggers(harness) == []
    assert report.prs["completed"] == 1


def test_the_applied_record_the_merge_script_publishes_releases_the_guard(harness):
    """The real event shape, not a hand-built Merge: `scripts/merge_pr.sh` publishes it.

    `ngit pr apply` replays a proposal as new commits, so nothing else can tell the bridge
    what landed. The feed is built the way the bridge reads it -- `RelayPrs.events`, whose
    filter is the repository's `a` coordinate -- and then handed to the pass, whose guard
    release is the thing that was missing in production.
    """
    pr, task_id = pr_with_review_task(harness)
    commit = "f" * 40
    # A merge status is a maintainer's: sign it as the key the allowlist names, not a
    # throwaway one, or the gate refuses the record this test is about.
    signer = sdk.Keys.generate()
    harness.config = replace(harness.config,
                             maintainers=(signer.public_key().to_hex(),))
    event = pr_status.signed(
        pr_status.applied_event(
            pr_id=pr.id, address=ADDRESS, merge_commit=commit, authors=(TRUSTED_HEX,),
            relay_hint="wss://relay.example", created_at=1100),
        signer.secret_key().to_hex())

    def fetch(**kwargs):
        assert kwargs["a_tag"] == ADDRESS
        return [event]

    feed = ngit.RelayPrs(("wss://relay.example",), ADDRESS, fetcher=fetch).events(0)
    assert [merge.commit for merge in feed.merges] == [commit]

    harness.prs_data = feed
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done"
    assert commit in str(row["result"])
    assert guard_triggers(harness) == []
    assert report.prs["completed"] == 1


def test_a_close_the_tooling_publishes_is_seen_and_blocks_the_card(harness):
    """The same filter, for the other status: without the `a` tag the close never arrives."""
    pr, task_id = pr_with_review_task(harness)
    # A close is a maintainer's or the PR author's act; sign it as a maintainer.
    signer = sdk.Keys.generate()
    harness.config = replace(harness.config,
                             maintainers=(signer.public_key().to_hex(),))
    event = pr_status.signed(
        pr_status.closed_event(
            pr_id=pr.id, address=ADDRESS, reason="Superseded by PR 1234abcd.",
            authors=(TRUSTED_HEX,), relay_hint="wss://relay.example", created_at=1100),
        signer.secret_key().to_hex())

    def fetch(**kwargs):
        assert kwargs["a_tag"] == ADDRESS
        return [event]

    feed = ngit.RelayPrs(("wss://relay.example",), ADDRESS, fetcher=fetch).events(0)
    assert [stopped.pr_id for stopped in feed.closed] == [pr.id]

    harness.prs_data = feed
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "blocked" and row["block_kind"] == "dependency"
    assert any(str(event["id"]) in comment["body"] for comment in harness.comments(task_id))
    assert report.prs["blocked"] == 1


def test_a_merge_completes_every_card_for_the_pr_root(harness):
    """Including a card a person filed: the merge finishes the PR's cards, not one anchor."""
    pr, first = pr_with_review_task(harness)
    second = hand_queued_review_task(harness, pr)

    merge = ngit.Merge(pr_id=pr.id, commit="b" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    report = harness.sync()

    assert harness.task(first)["status"] == "done"
    assert harness.task(second)["status"] == "done"
    assert "b" * 40 in str(harness.task(second)["result"])
    assert report.prs["completed"] == 2


def test_a_merge_completes_a_parked_review_card(harness):
    """A reviewer parks the card (the only terminal action the guard leaves it); the merge closes it."""
    pr, task_id = pr_with_review_task(harness)
    harness.board.comment(task_id, "review", "Verdict: approved")
    assert harness.board.block(task_id, kind="dependency", author="review",
                               comment="approved at c0ffee; waiting for the merge") is True

    merge = ngit.Merge(pr_id=pr.id, commit="e" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done" and "e" * 40 in str(row["result"])


def test_a_merge_unblocks_a_card_that_waited_on_it(harness):
    """`after PR X merges` is a fact, not a person: the merge releases the card."""
    pr, _task = pr_with_review_task(harness)
    waiting = harness.board.create(
        title=f"After PR {pr.id[:8]} merges: mirror the guard",
        body="Stage the same shape the PR ships.", assignee="default", status="blocked")

    merge = ngit.Merge(pr_id=pr.id, commit="c" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    report = harness.sync()

    assert harness.task(waiting)["status"] == "ready"
    assert any("merged as " + "c" * 40 in c["body"] for c in harness.comments(waiting))
    assert report.prs["unblocked"] == 1


def test_a_merge_leaves_a_blocked_card_that_only_mentions_the_pr_alone(harness):
    pr, _task = pr_with_review_task(harness)
    other = harness.board.create(
        title="Something else",
        body=f"Related to PR {pr.id[:8]}, but blocked on a person's decision.",
        assignee="default", status="blocked")

    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    harness.sync()

    assert harness.task(other)["status"] == "blocked"


def test_a_merge_without_a_commit_leaves_the_card_open(harness):
    """`applied` without a merge commit is not evidence: the card cannot name what landed."""
    pr, task_id = pr_with_review_task(harness)
    merge = ngit.Merge(pr_id=pr.id, commit="", created_at=1100)
    harness.prs_data = ([], [merge])
    report = harness.sync()

    assert harness.task(task_id)["status"] != "done"
    assert any("without a commit" in note for note in report.notes)


def test_a_closed_pr_blocks_the_card_with_the_closing_event(harness):
    """A close is not a merge: the card waits for a person, and says which event closed it."""
    pr, task_id = pr_with_review_task(harness)
    stopped = ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1200)
    harness.prs_data = ngit.PrFeed(closed=[stopped])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "blocked" and row["block_kind"] == "dependency"
    assert any("9" * 64 in c["body"] for c in harness.comments(task_id))
    assert report.prs["blocked"] == 1
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert "closed without a merge" in str(refused.value)


def test_a_close_after_a_merge_does_not_undo_it(harness):
    """Applied is final: a stale 1632 cannot reopen or block a card a merge finished."""
    pr, task_id = pr_with_review_task(harness)
    merge = ngit.Merge(pr_id=pr.id, commit="f" * 40, created_at=1100)
    harness.prs_data = ([], [merge])
    harness.sync()
    assert harness.task(task_id)["status"] == "done"

    stopped = ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1200)
    harness.prs_data = ngit.PrFeed(closed=[stopped])
    harness.sync()

    assert harness.task(task_id)["status"] == "done"
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "merged"


# -- statuses that name the PR by `e` and carry no repository `a` -------------
#
# `ngit pr close` publishes a kind-1631/1632 whose only reference to the PR is the `e`
# tag: no `a`, so a read filtered by the repository address never returns it, and two PRs
# wedged their cards until an operator hand-published a signed status. These read the
# widening through the real `RelayPrs`: one fetch by `#a` (unchanged), one by `#e` over
# the PR ids the store already links.

class _RecordingRelay:
    """A relay that answers a filter the way a relay does, and records what it was asked."""

    def __init__(self, events: list[dict]):
        self.events = [dict(event) for event in events]
        self.asks: list[dict] = []

    def many(self, *, relays, kinds, a_tag=None, since=0, limit=500, secret=None):
        """The `#a` read: the repository's proposals, revisions and statuses."""
        self.asks.append({"kinds": list(kinds), "a_tag": a_tag})
        return [event for event in self.events
                if int(event["kind"]) in kinds
                and (not a_tag or ["a", a_tag] in event["tags"])]

    def tagged(self, *, relays, kinds, tag, values, limit=50, a_tag=None, secret=None):
        """The `#e` read: one filter per PR id, answered by a relay's own tag index."""
        self.asks.append({"kinds": list(kinds), "tag": tag, "values": list(values)})
        wanted = set(values)
        return [("wss://relay.example", event) for event in self.events
                if int(event["kind"]) in kinds
                and any(t[0] == tag and t[1] in wanted for t in event["tags"])]


def _proposal_event(seed: str, *, tip: str = "a" * 40) -> dict:
    return {"id": event_id(seed), "kind": ngit.KIND_PR, "pubkey": TRUSTED_HEX,
            "created_at": 1000,
            "tags": [["a", ADDRESS], ["subject", "Add a widget"],
                     ["branch-name", "pr/widget"], ["c", tip]]}


def _applied_event(seed: str, pr_id: str, commit: str, *, created_at: int = 2000,
                   a_tag: str = "") -> dict:
    """`ngit pr close`'s shape: `e` names the PR, and there is no repository `a`."""
    tags = [["e", pr_id], ["merge-commit-id", commit], ["alt", "PR applied"]]
    if a_tag:
        tags.append(["a", a_tag])
    return {"id": event_id(seed), "kind": ngit.KIND_APPLIED, "pubkey": TRUSTED_HEX,
            "created_at": created_at, "tags": tags}


def _closed_event(seed: str, pr_id: str, *, created_at: int = 2000) -> dict:
    return {"id": event_id(seed), "kind": ngit.KIND_CLOSED, "pubkey": TRUSTED_HEX,
            "created_at": created_at,
            "tags": [["e", pr_id], ["alt", "PR closed"]]}


def _relay_reader(relay: _RecordingRelay) -> ngit.RelayPrs:
    return ngit.RelayPrs(["wss://relay.example"], ADDRESS,
                         fetcher=relay.many, status_fetcher=relay.tagged)


def test_an_applied_status_with_no_repo_tag_completes_the_pr_card(home, store):
    """The wedge: `ngit pr close` published a 1631 with `e` and no `a`; nothing saw it."""
    pr = make_pr("c1", commit="a" * 40)
    relay = _RecordingRelay([_proposal_event("c1"),
                             _applied_event("c2", pr.id, "b" * 40)])
    reader = _relay_reader(relay)
    made = Harness(home, store, prs=reader.events(0), statuses=reader.statuses)
    try:
        made.sync()                                   # the PR is announced and linked
        task_id = review_tasks(made)[0]["id"]
        assert made.task(task_id)["status"] == "ready"

        report = made.sync()                          # the status read reaches it by `e`

        row = made.task(task_id)
        assert row["status"] == "done" and "b" * 40 in str(row["result"])
        assert report.prs["completed"] == 1
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "merged"
        # The second fetch asked the relay about this PR and no other; the `#a` read stands.
        assert relay.asks[0]["a_tag"] == ADDRESS
        assert relay.asks[1] == {"kinds": [ngit.KIND_APPLIED, ngit.KIND_CLOSED],
                                 "tag": "e", "values": [pr.id]}
    finally:
        made.close()


def test_a_closed_status_with_no_repo_tag_blocks_the_pr_card(home, store):
    """A 1632 with `e` and no `a` must still hand the card to a person, event named."""
    pr = make_pr("c3", commit="a" * 40)
    close = _closed_event("c4", pr.id)
    relay = _RecordingRelay([_proposal_event("c3"), close])
    reader = _relay_reader(relay)
    made = Harness(home, store, prs=reader.events(0), statuses=reader.statuses)
    try:
        made.sync()
        task_id = review_tasks(made)[0]["id"]

        report = made.sync()

        row = made.task(task_id)
        assert row["status"] == "blocked" and row["block_kind"] == "dependency"
        assert any(close["id"] in comment["body"] for comment in made.comments(task_id))
        assert report.prs["blocked"] == 1
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "closed"
        with pytest.raises(sqlite3.IntegrityError) as refused:
            made.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
        assert "closed without a merge" in str(refused.value)
    finally:
        made.close()


def test_a_status_naming_an_unknown_pr_changes_nothing(home, store):
    """The widened read is scoped by id: a status for a PR this board never linked is inert."""
    pr = make_pr("c5", commit="a" * 40)
    unknown = event_id("c6")
    relay = _RecordingRelay([
        _proposal_event("c5"),
        # An issue's resolution shares the repository `a`, so this one reaches the feed;
        # the bridge must not act on a root it never announced.
        _applied_event("c7", unknown, "b" * 40, a_tag=ADDRESS),
        # An `e`-only close for a PR the store does not link is never even asked for.
        _closed_event("c8", unknown),
    ])
    reader = _relay_reader(relay)
    made = Harness(home, store, prs=reader.events(0), statuses=reader.statuses)
    try:
        made.sync()
        task_id = review_tasks(made)[0]["id"]

        report = made.sync()

        assert made.task(task_id)["status"] == "ready"
        assert report.prs["completed"] == 0 and report.prs["blocked"] == 0
        assert [task["idempotency_key"] for task in made.tasks()] == [f"ngit:pr:{pr.id}"]
        assert [ask["values"] for ask in relay.asks if ask.get("tag") == "e"] == [[pr.id]]
    finally:
        made.close()


def test_a_status_returned_by_both_reads_is_counted_once():
    """The `#a` and `#e` reads overlap; a replayed older status must not undo a newer one."""
    pr_id = event_id("c9")
    merge = ngit.Merge(pr_id=pr_id, commit="b" * 40, created_at=2000,
                       event_id=event_id("ca"))
    older = ngit.Merge(pr_id=pr_id, commit="a" * 40, created_at=1000,
                       event_id=event_id("cb"))

    both = ngit.merge_feeds(ngit.PrFeed(merges=[merge]), ngit.PrFeed(merges=[merge]))
    assert both.merges == [merge]
    # A status that names a commit is the evidence a card finishes on: an older one, or a
    # newer one with no commit, does not replace it.
    assert ngit.merge_feeds(ngit.PrFeed(merges=[merge]),
                            ngit.PrFeed(merges=[older])).merges == [merge]
    assert ngit.merge_feeds(
        ngit.PrFeed(merges=[merge]),
        ngit.PrFeed(merges=[ngit.Merge(pr_id=pr_id, created_at=3000)])).merges == [merge]

# -- the trailing status sweep ------------------------------------------------
#
# The live shape (2026-11-08): the PR feed's cursor had reached 21:46:50 and the close had
# been published at 15:42:03, so every incremental read started *after* the close. Nothing
# re-read it, the link stayed `signalled`, and the board kept obeying an open PR. The sweep
# re-reads the two status kinds over a trailing window, every pass, and is the only reader
# that can still see that close.

def test_a_close_behind_the_cursor_heals_on_the_next_pass(harness):
    """Acceptance (1): the sweep sees a close the incremental poll has moved past."""
    pr, task_id = pr_with_review_task(harness)
    reached = 1794174410                                     # 2026-11-08 21:46:50Z
    stopped = ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1794152523)
    harness.store.ngit_set_cursor(ADDRESS, "prs", reached)
    # The poll carries nothing: its window starts long after the close was published.
    harness.prs_data = ngit.PrFeed()
    harness.sweep_data = ngit.PrFeed(closed=[stopped])

    report = harness.sync()

    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "closed"
    row = harness.task(task_id)
    assert row["status"] == "blocked" and row["block_kind"] == "dependency"
    assert any("9" * 64 in comment["body"] for comment in harness.comments(task_id))
    assert report.prs["blocked"] == 1 and report.prs["swept"] == 1
    # The sweep read a window that reaches back past the close ...
    assert harness.sweep_since[-1] <= stopped.created_at
    # ... and left the cursor the proposals are read with exactly where it was.
    assert harness.store.ngit_cursor(ADDRESS, "prs") == str(reached)
    # The guard refuses the completion, naming the close: the card cannot be finished by it.
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert "closed without a merge" in str(refused.value)


# -- a close is a close, whatever it says ------------------------------------

#: Two real kind-1632 closes from 2026-11-08, copied verbatim off relay.ngit.dev. Same
#: author, same relay set, same tags -- one names its reason, the other carries nothing.
#: The empty one was mistaken for an event the bridge drops, so it is pinned here: the
#: reader decides on kind and root tag, and `content` never enters into it.
CLOSES_FIXTURE = Path(__file__).parent / "fixtures" / "ngit-closes-2026-11-08.json"


def real_closes() -> list[dict]:
    return json.loads(CLOSES_FIXTURE.read_text(encoding="utf-8"))["events"]


def root_tag(event: dict) -> str:
    return next(tag[1] for tag in event["tags"] if tag[0] == "e" and tag[3] == "root")


def test_a_close_with_no_content_closes_its_pr_just_the_same():
    """`content` is the reason, not the signal: an empty close is still a close."""
    reasoned, silent = real_closes()
    assert reasoned["content"] and not silent["content"]  # the fixture's whole point
    for event in (reasoned, silent):
        stopped = ngit.closed_from_event(event)
        assert stopped is not None
        assert stopped.event_id == event["id"]
        assert stopped.pr_id == root_tag(event)


def test_the_relay_reader_keeps_a_close_with_empty_content():
    """The feed the pass actually reads: the empty close survives the fetch and the fold."""
    _reasoned, silent = real_closes()
    feed = ngit.RelayPrs(["wss://relay.example"], ADDRESS,
                         fetcher=lambda **_: [silent]).events(0)

    assert [stopped.event_id for stopped in feed.closed] == [silent["id"]]
    assert [stopped.pr_id for stopped in feed.closed] == [root_tag(silent)]


def test_a_pass_with_no_news_does_not_reopen_a_released_guard(harness):
    """Silence is not evidence: an incremental window must not undo a terminal state.

    A status event can reach the relays after the pass whose window covered its timestamp,
    which puts it behind the cursor for good. The pass then sees nothing at all about the
    PR -- and rewriting `open` on that silence is what pinned a card whose content had
    already landed on master.
    """
    pr, task_id = pr_with_review_task(harness)
    harness.board.guard_pr(task_id, pr_id=pr.id, state="applied", tip="c0ffee",
                           merge_commit="a" * 40, detail="content landed as a" + "a" * 39)

    harness.prs_data = ([], [])  # this poll carried no news about the PR
    report = harness.sync()

    assert harness.board.guard_state(task_id)["state"] == "applied"
    assert guard_triggers(harness) == []
    assert report.prs["review_errors"] == 0
    # And the card is still completable: the guard has nothing left to refuse.
    assert harness.board.db.execute(
        "UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,)).rowcount == 1


def test_a_pass_with_no_news_does_not_reopen_a_closed_guard(harness):
    pr, task_id = pr_with_review_task(harness)
    harness.board.close_guard(task_id, event_id="9" * 64)

    harness.prs_data = ([], [])
    harness.sync()

    assert harness.board.guard_state(task_id)["state"] == "closed"
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert "closed without a merge" in str(refused.value)


def test_a_close_behind_the_cursor_is_invisible_without_the_sweep(harness):
    """The same pass without the sweep: this is the state the bridge was stuck in for hours."""
    pr, task_id = pr_with_review_task(harness)
    harness.store.ngit_set_cursor(ADDRESS, "prs", 1794174410)
    harness.prs_data = ngit.PrFeed()

    report = harness.sync()

    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "signalled"
    assert harness.task(task_id)["status"] == "ready"
    assert report.prs["blocked"] == 0


def test_the_sweep_never_re_announces_a_pr(harness):
    """Acceptance (2): status kinds only -- no second card, no second channel message."""
    pr, task_id = pr_with_review_task(harness)
    assert len(harness.tasks()) == 1 and len(harness.messages) == 1

    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1100)
    harness.prs_data = ngit.PrFeed()
    harness.sweep_data = ngit.PrFeed(merges=[merge])
    harness.sync()

    assert harness.task(task_id)["status"] == "done"
    assert len(harness.tasks()) == 1          # one card, still
    assert len(harness.messages) == 2         # the PR's announcement, then its merge
    assert harness.messages[-1][1].count("merged") == 1

    # The same window, read again on the next pass: the merge is decided once, not twice.
    report = harness.sync()
    assert len(harness.tasks()) == 1
    assert len(harness.messages) == 2
    assert report.posted == 0


def test_the_sweep_does_not_narrate_a_pr_the_bridge_never_announced(harness):
    """A status for an unknown PR root is not news: the bridge narrates what it watched."""
    harness.sweep_data = ngit.PrFeed(
        merges=[ngit.Merge(pr_id="7" * 64, commit="e" * 40, created_at=1100)],
        closed=[ngit.Closed(pr_id="8" * 64, event_id="9" * 64, created_at=1200)])

    report = harness.sync()

    assert harness.tasks() == [] and harness.messages == []
    assert report.prs["swept"] == 2 and report.posted == 0


def test_the_report_carries_the_sweeps_own_count(harness):
    """Acceptance (3): `--dry-run --json` shows the sweep ran, and it writes nothing."""
    pr, task_id = pr_with_review_task(harness)
    harness.sweep_data = ngit.PrFeed(
        closed=[ngit.Closed(pr_id=pr.id, event_id="9" * 64, created_at=1200)])
    before = harness.store.ngit_cursor(ADDRESS, ngit.SWEEP_CURSOR)

    report = harness.sync(dry_run=True)
    payload = report.as_dict()

    assert payload["sweep_hours"] == ngit.DEFAULT_SWEEP_HOURS == 24
    assert payload["prs"]["swept"] == 1 and payload["prs"]["would_close"] == 1
    assert any(line.startswith("sweep:   1 applied/closed") for line in report.lines())
    # A dry run writes nothing, the sweep's cursor included, and the card is untouched.
    assert harness.store.ngit_cursor(ADDRESS, ngit.SWEEP_CURSOR) == before
    assert harness.task(task_id)["status"] == "ready"


def test_a_failing_sweep_is_a_report_line_not_a_lost_pass(harness):
    """A relay that will not answer for the status kinds must not cost the pass its news."""
    harness.prs_data = ([make_pr("c9")], [])
    harness.sweep_data = ngit.NgitError("relay timed out")

    report = harness.sync()

    assert report.posted == 1 and report.prs["signalled"] == 1
    assert report.errors == []                       # best effort: not a failed pass
    assert any("sweep: relay timed out" in note for note in report.notes)
    # Nothing was read, so nothing claims the window was covered.
    assert harness.store.ngit_cursor(ADDRESS, ngit.SWEEP_CURSOR) is None


def test_the_sweep_window_extends_over_the_gap_since_the_last_sweep(harness):
    """A pass that was down for longer than the window still covers what it missed."""
    harness.store.ngit_set_cursor(ADDRESS, ngit.SWEEP_CURSOR, 1794000000)
    before = int(time.time())

    harness.sync()

    assert harness.sweep_since[0] == 1794000000
    assert int(harness.store.ngit_cursor(ADDRESS, ngit.SWEEP_CURSOR)) >= before


def test_sweep_hours_zero_turns_the_sweep_off(harness):
    harness.config = ngit.BridgeConfig(assignee="default", lookback_hours=0, sweep_hours=0)

    report = harness.sync()

    assert harness.sweep_since == []
    assert report.sweep_hours == 0 and report.prs["swept"] == 0
    assert harness.store.ngit_cursor(ADDRESS, ngit.SWEEP_CURSOR) is None


def test_the_sweep_source_asks_for_the_two_status_kinds_only():
    """The relay read names applied and closed, with the repository address, and nothing else."""
    seen: dict = {}
    events = [
        {"id": "a" * 64, "kind": ngit.KIND_APPLIED, "pubkey": REPO_OWNER, "created_at": 1000,
         "tags": [["a", ADDRESS], ["e", "b" * 64], ["merge-commit", "c" * 40]]},
        {"id": "d" * 64, "kind": ngit.KIND_CLOSED, "pubkey": REPO_OWNER, "created_at": 1100,
         "tags": [["a", ADDRESS], ["e", "b" * 64]]},
        # A proposal in the same answer is ignored: the sweep's source never reads one.
        {"id": "e" * 64, "kind": ngit.KIND_PR, "pubkey": REPO_OWNER, "created_at": 1200,
         "tags": [["a", ADDRESS], ["subject", "Old news"], ["branch-name", "pr/old"],
                  ["c", "f" * 40]]},
    ]

    def fetcher(**kwargs):
        seen.update(kwargs)
        return list(events)

    feed = ngit.RelayPrs(["wss://relay.example"], ADDRESS, fetcher=fetcher).sweep(500)

    assert seen["kinds"] == [ngit.KIND_APPLIED, ngit.KIND_CLOSED]
    assert seen["a_tag"] == ADDRESS and seen["since"] == 500
    assert [(merge.pr_id, merge.commit) for merge in feed.merges] == [("b" * 64, "c" * 40)]
    assert [stopped.pr_id for stopped in feed.closed] == ["b" * 64]
    assert feed.prs == [] and feed.revisions == []


def test_a_swept_status_never_replaces_a_newer_one_the_poll_carried():
    """Overlap is normal; the newer event wins, so a replayed older copy cannot undo it."""
    newer = ngit.Merge(pr_id="a" * 64, commit="b" * 40, created_at=2000)
    older = ngit.Merge(pr_id="a" * 64, commit="c" * 40, created_at=1000)

    merged = ngit.with_statuses(ngit.PrFeed(merges=[newer]), ngit.PrFeed(merges=[older]))

    assert [(merge.commit, merge.created_at) for merge in merged.merges] == [("b" * 40, 2000)]


def test_a_pass_with_no_news_does_not_reopen_an_approved_guard(harness):
    """An approval is terminal too: no-news silence must not re-make the refusal.

    A reviewer's approving verdict releases the guard so the card can be finished. The row
    stays `approved` -- and a pass that learned nothing about the PR must leave it alone.
    Rewriting `open` would re-make the trigger and refuse a completion the verdict earned,
    which is the exact loop cdc7550 exists to close.
    """
    pr, task_id = pr_with_review_task(harness)
    harness.board.approve_guard(task_id, verdict="a" * 64)

    harness.prs_data = ([], [])
    harness.sync()

    assert harness.board.guard_state(task_id)["state"] == "approved"
    assert guard_triggers(harness) == []
    # The card is still completable: an approval has nothing left to refuse.
    assert harness.board.db.execute(
        "UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,)).rowcount == 1


def test_a_revision_after_an_approval_reopens_the_card_and_the_guard(harness):
    """An approval covers the tip it saw; a newer tip must not inherit its release.

    The approving verdict finishes the card and drops the refusal trigger, which is the
    intended path. A revision then arrives at a new tip, and `_revisions` reopens the card
    precisely because the old verdict is stale -- so the guard must stop saying `approved`
    at the old tip. Otherwise the reopened card is unguarded: nothing re-makes the trigger
    the approval dropped, and a worker's word finishes a PR no approval covers.
    """
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    park_with_verdict(harness, task_id, "approved")
    report = harness.sync()

    # The approval's own path: the card is finished, the trigger is gone.
    assert report.prs["approved"] == 1
    assert harness.task(task_id)["status"] == "done"
    assert harness.board.guard_state(task_id)["state"] == "approved"
    assert guard_triggers(harness) == []

    revision = revision_of(pr, "b" * 40, created_at=3000, event="2")
    harness.prs_data = ngit.PrFeed(revisions=[revision])
    report = harness.sync()

    assert report.prs["reopened"] == 1
    assert harness.task(task_id)["status"] == "ready"
    held = harness.board.guard_state(task_id) or {}
    assert held.get("state") == "open", held
    assert held.get("tip") == "b" * 40, held
    assert guard_triggers(harness) == [f"ngit_pr_done_{task_id}"]
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert "open at " + "b" * 12 in str(refused.value)


def test_a_pass_with_no_news_still_holds_an_open_guard(harness):
    """The control: monotonic does not mean toothless -- an open guard keeps refusing."""
    pr, task_id = pr_with_review_task(harness)

    harness.prs_data = ([], [])
    harness.sync()

    assert harness.board.guard_state(task_id)["state"] == "open"
    assert guard_triggers(harness) == [f"ngit_pr_done_{task_id}"]
    with pytest.raises(sqlite3.IntegrityError) as refused:
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))
    assert "open at c0ffee" in str(refused.value)


def test_the_cli_refuses_an_open_pr_card(harness):
    """The acceptance command, run for real against the harness board."""
    hermes = shutil.which("hermes")
    if not hermes:
        pytest.skip("the hermes CLI is not installed on this host")
    pr, task_id = pr_with_review_task(harness)
    env = {key: value for key, value in os.environ.items()
           if key not in ("HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK",
                          "HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HERMES_KANBAN_HOME")}
    env["HERMES_KANBAN_HOME"] = str(harness.home)

    done = subprocess.run(
        [hermes, "kanban", "--board", BOARD, "complete", task_id, "--summary", "probe"],
        capture_output=True, text=True, env=env, timeout=120)

    assert done.returncode != 0
    assert pr.id[:8] in done.stderr and "open at c0ffee" in done.stderr
    assert harness.task(task_id)["status"] == "ready"


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
    made.config = bridge_config(assignee="default", lookback_hours=0, reviews=False)
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


# -- the label the PR carries -------------------------------------------------

def label_detail(harness, pr_id: str) -> dict:
    """What the link row says about the label: the last set published and its events."""
    link = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr_id) or {}
    return json.loads(link.get("detail") or "{}")


def set_review_status(harness, task_id: str, status: str, *, force: bool = False) -> None:
    """Move a card the way the board would, except where a test is simulating history.

    `done` on a PR card is refused by the guard now (`done` requires `applied`), so the
    label tests use `review` -- the state a finished review actually sits in until its
    merge lands. `force=True` drops that card's trigger first, for the one test that
    simulates a card completed before the guard existed.
    """
    if force:
        harness.board.db.execute(f"DROP TRIGGER IF EXISTS ngit_pr_done_{task_id}")
    harness.board.db.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))


def hand_queued_review_task(harness, pr, *, subject: str = "re-review") -> str:
    """A review card the bridge did not make: what a reviewer queues for a revision."""
    return harness.board.create(
        title=f"Review PR {pr.id[:8]}: {subject}",
        body=f"Re-review of PR `{pr.id}`: review the tip, then report a verdict.",
        assignee="review", status="ready", created_by="holly")


def review_verdict(harness, pr_id: str, label: str, *, event_id: str = "",
                   created_at: int = 2000, relay: str = "wss://relay.example",
                   body: str = "") -> str:
    """One verdict comment on the PR root, as a reviewer publishes it."""
    event_id = event_id or (label[:1] or "v").ljust(64, "0")
    text = body or f"Verdict: {label}\n\nThe reviewer's reasons."
    harness.verdicts[pr_id] = ngit_labels.Verdict(label=label, event_id=event_id,
                                                  created_at=created_at, relay=relay,
                                                  body=text)
    return event_id


def carry_label(harness, pr_id: str, value: str, event_id: str, *,
                relay: str = "wss://relay.example") -> None:
    """A label event already on a relay: what an earlier writer left behind."""
    harness.carried[pr_id] = (*harness.carried.get(pr_id, ()),
                              ngit_labels.Label(value=value, event_id=event_id,
                                                created_at=1000, relay=relay))


def pr_with_review_task(harness, seed: str = "e1", *, commit: str = "c0ffee") -> tuple:
    pr = make_pr(seed, commit=commit)
    harness.prs_data = ([pr], [])
    harness.sync()
    return pr, review_tasks(harness)[0]["id"]


def test_a_new_pr_is_labelled_review_required_exactly_once(harness):
    pr, _task = pr_with_review_task(harness)

    assert harness.label_calls == [(pr.id, ADDRESS, ("review-required",), ())]
    detail = label_detail(harness, pr.id)
    assert detail["label"] == ["review-required"]
    assert len(detail["label_events"]) == 1

    # Two polls, one event: the label is on the PR, not republished in a loop.
    harness.prs_data = ([], [])
    report = harness.sync()
    assert len(harness.label_calls) == 1
    assert report.prs["labels"] == 0


def test_a_new_verdict_replaces_the_previous_label_rather_than_adding_to_it(harness):
    """changes-requested -> approved: the PR carries exactly {approved} afterwards."""
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "changes-requested", event_id="c" * 64, created_at=1000)
    harness.prs_data = ([], [])
    harness.sync()
    assert label_detail(harness, pr.id)["label"] == ["changes-requested"]
    changes = tuple(label_detail(harness, pr.id)["label_events"])

    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, ("approved",), changes)
    assert set(label_detail(harness, pr.id)["label"]) == {"approved"}
    assert report.prs["labels"] == 1


def test_a_label_left_over_from_a_previous_writer_is_corrected_by_one_pass(harness):
    """The old writer's `in-review` is in the link row and on the relay; one pass fixes it."""
    pr, _task = pr_with_review_task(harness)
    old = "o" * 64
    # The relays hold the previous writer's label and nothing of ours: this is the PR as a
    # bridge that has only ever read it sees.
    harness.carried[pr.id] = ()
    carry_label(harness, pr.id, "in-review", old)
    detail = label_detail(harness, pr.id)
    detail["label"] = ["in-review"]
    detail["label_events"] = [old]
    harness.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr.id, state="signalled",
                                 board=BOARD, detail=detail)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)

    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, ("approved",), (old,))
    assert set(label_detail(harness, pr.id)["label"]) == {"approved"}
    assert report.prs["labels"] == 1


def test_a_label_the_link_row_never_recorded_is_still_retracted(harness):
    """A label event only the relay knows about is what a different writer leaves behind."""
    pr, _task = pr_with_review_task(harness)
    foreign = "s" * 64
    carry_label(harness, pr.id, "stale", foreign)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)

    harness.sync()

    retracted = harness.label_calls[-1][3]
    assert foreign in retracted
    assert set(label_detail(harness, pr.id)["label"]) == {"approved"}


def test_a_second_live_label_with_the_same_value_is_retracted(harness):
    """Two publishes of `approved` leave two live events: the older one goes, one stays."""
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    survivor = tuple(label_detail(harness, pr.id)["label_events"])
    assert len(survivor) == 1
    # A second pass published the same word: both events are live, and a value test cannot
    # see the pair.
    duplicate = "8" * 64
    carry_label(harness, pr.id, "approved", duplicate)

    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, (), (duplicate,))
    detail = label_detail(harness, pr.id)
    assert detail["label"] == ["approved"]
    assert tuple(detail["label_events"]) == survivor
    # Nothing was published: the counter says "labels published", and this pass removed one.
    assert report.prs["labels"] == 0
    assert any("retracted 1 other live label event" in note for note in report.notes)


def test_a_duplicate_neither_the_row_nor_the_relay_names_keeps_the_newest(harness):
    """The row names an event that is gone; two live duplicates remain. Newest survives."""
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    lost = tuple(label_detail(harness, pr.id)["label_events"])
    older, newer = "7" * 64, "8" * 64
    harness.carried[pr.id] = (
        ngit_labels.Label("approved", older, created_at=1000, relay="wss://relay.example"),
        ngit_labels.Label("approved", newer, created_at=3000, relay="wss://relay.example"))

    harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, (), (older, *lost))
    detail = label_detail(harness, pr.id)
    assert detail["label"] == ["approved"]
    assert detail["label_events"] == [newer]


def test_a_row_that_says_approved_over_a_stale_relay_label_republishes_it(harness):
    """The row is not evidence the label is live: publish when no live event carries it."""
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    lost = tuple(label_detail(harness, pr.id)["label_events"])
    stale = "9" * 64
    harness.carried[pr.id] = (
        ngit_labels.Label("in-review", stale, created_at=500, relay="wss://relay.example"),)

    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, ("approved",), (stale, *lost))
    detail = label_detail(harness, pr.id)
    assert detail["label"] == ["approved"]
    assert len(detail["label_events"]) == 1 and detail["label_events"][0] not in lost
    assert report.prs["labels"] == 1


def test_no_verdict_and_no_review_card_carries_no_label(harness):
    """Reviews turned off: no card, no verdict, no label -- and a leftover is removed."""
    harness.config = bridge_config(assignee="default", lookback_hours=0, reviews=False)
    pr = make_pr("f1", commit="c1")
    harness.prs_data = ([pr], [])
    leftover = "r" * 64
    carry_label(harness, pr.id, "review-required", leftover)

    report = harness.sync()

    assert harness.label_calls == [(pr.id, ADDRESS, (), (leftover,))]
    assert label_detail(harness, pr.id)["label"] == []
    assert report.prs["labels"] == 0


def test_no_verdict_and_no_review_card_publishes_nothing_when_there_is_nothing_to_remove(harness):
    harness.config = bridge_config(assignee="default", lookback_hours=0, reviews=False)
    pr = make_pr("f2", commit="c2")
    harness.prs_data = ([pr], [])

    harness.sync()

    assert harness.label_calls == []


def test_a_revision_after_a_verdict_needs_review_not_a_second_label(harness):
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.prs_data = ([], [])
    harness.sync()
    approved = tuple(label_detail(harness, pr.id)["label_events"])

    revision = ngit.Revision(pr_id=pr.id, commit="d1ff" * 10, created_at=3000,
                             event_id="1" * 64)
    harness.prs_data = ngit.PrFeed(revisions=[revision])
    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, ("review-required",), approved)
    assert label_detail(harness, pr.id)["label"] == ["review-required"]
    assert report.prs["labels"] == 1


def test_a_re_review_verdict_moves_the_label_and_the_anchor(harness):
    """The newest verdict comment is the one in force, not the first one published."""
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "changes-requested", event_id="c" * 64, created_at=2000)
    harness.sync()
    changes = tuple(label_detail(harness, pr.id)["label_events"])

    review_verdict(harness, pr.id, "approved", event_id="b" * 64, created_at=4000)
    harness.prs_data = ([], [])
    report = harness.sync()

    assert harness.label_calls[-1] == (pr.id, ADDRESS, ("approved",), changes)
    assert label_detail(harness, pr.id)["label"] == ["approved"]
    assert report.prs["labels"] == 1


def test_relay_disagreement_newest_verdict_wins_and_the_pass_names_the_relay(harness):
    pr, _task = pr_with_review_task(harness)
    harness.readings[pr.id] = (("wss://relay.example", "changes-requested", 1000),
                               ("wss://other.example", "approved", 2000))
    harness.verdicts[pr.id] = ngit_labels.Verdict(
        label="approved", event_id="a" * 64, created_at=2000, relay="wss://other.example")

    report = harness.sync()

    assert harness.label_calls[-1][2] == ("approved",)
    assert label_detail(harness, pr.id)["label_relay"] == "wss://other.example"
    notes = "\n".join(report.notes)
    assert "wss://other.example" in notes and "disagreed" in notes


def test_an_unreadable_newest_verdict_leaves_the_label_alone_and_says_why(harness):
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    published = len(harness.label_calls)

    harness.verdicts[pr.id] = ngit_labels.Verdict(
        label="", event_id="u" * 64, created_at=3000, relay="wss://relay.example",
        readable=False)
    report = harness.sync()

    assert len(harness.label_calls) == published
    assert label_detail(harness, pr.id)["label"] == ["approved"]
    notes = "\n".join(report.notes)
    assert "u" * 64 in notes and "cannot be read" in notes


def test_a_pr_the_relay_read_did_not_answer_for_keeps_its_label(harness):
    pr, _task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    published = len(harness.label_calls)

    harness.state = lambda pr_ids: ngit.ReadState(errors=("relay read failed",))
    report = harness.sync()

    assert len(harness.label_calls) == published
    assert label_detail(harness, pr.id)["label"] == ["approved"]
    assert any("left as it is" in note for note in report.notes)


def test_the_relay_state_is_read_once_for_the_whole_pass(harness):
    pr, _task = pr_with_review_task(harness)
    other = make_pr("f3", commit="c3", branch="pr/other")
    harness.prs_data = ([other], [])
    harness.read_calls.clear()

    harness.sync()

    assert harness.read_calls == [(pr.id, other.id)]


def test_a_merge_leaves_the_last_verdict_in_place_and_closes_the_task(harness):
    pr, task = pr_with_review_task(harness)
    review_verdict(harness, pr.id, "approved", event_id="a" * 64, created_at=2000)
    harness.sync()
    published = len(harness.label_calls)

    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=3000)
    harness.prs_data = ([pr], [merge])
    report = harness.sync()

    assert harness.task(task)["status"] == "done"
    assert report.prs["merged"] == 1
    # No new label: a merge does not rewrite the verdict it was decided on.
    assert len(harness.label_calls) == published
    assert label_detail(harness, pr.id)["label"] == ["approved"]


def test_a_label_that_could_not_be_published_is_retried_not_believed(home, store):
    failures = {"left": 1}

    def flaky(pr_id, address, add, retract=()):
        if failures["left"]:
            failures["left"] -= 1
            return ngit_labels.LabelResult(ok=False, error="blocked: not today")
        return ngit_labels.LabelResult(ok=True, events=("ab" * 32,), retracted=True, relays=1)

    made = Harness(home, store, labeler=flaky)
    pr = make_pr("e3", commit="c1")
    made.prs_data = ([pr], [])
    try:
        report = made.sync()
        assert report.prs["label_errors"] == 1
        assert any("blocked: not today" in error for error in report.errors)
        # Nothing was recorded as published, so the next poll tries again.
        assert "label" not in label_detail(made, pr.id)

        made.prs_data = ([], [])
        again = made.sync()
        assert again.prs["labels"] == 1
        assert label_detail(made, pr.id)["label"] == ["review-required"]
    finally:
        made.close()


def test_a_pr_announced_before_labels_existed_is_not_retrofitted(home, store):
    made = Harness(home, store)
    pr = make_pr("e4", commit="c2")
    made.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr.id, state="signalled", board=BOARD,
                              detail={"subject": pr.subject})
    made.prs_data = ([], [])
    try:
        made.sync()
        assert made.label_calls == []
    finally:
        made.close()


def test_labels_can_be_turned_off(home, store):
    made = Harness(home, store)
    made.config = bridge_config(assignee="default", lookback_hours=0, labels=False)
    pr = make_pr("e5", commit="c3")
    made.prs_data = ([pr], [])
    try:
        report = made.sync()
        assert made.label_calls == []
        assert report.prs["labels"] == 0 and report.prs["label_errors"] == 0
        assert "label" not in label_detail(made, pr.id)
    finally:
        made.close()


def test_a_dry_run_publishes_no_label(harness):
    pr = make_pr("e6", commit="c4")
    harness.prs_data = ([pr], [])
    report = harness.sync(dry_run=True)

    assert harness.label_calls == []
    assert report.prs["would_post"] == 1


def test_a_pr_first_seen_already_merged_gets_no_label(harness):
    """`review-required` on a PR that is already merged would be a lie in the LABELS column."""
    pr = make_pr("e7", commit="c5")
    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1100)
    harness.prs_data = ([pr], [merge])

    harness.sync()

    assert harness.label_calls == []


def test_a_proposal_with_no_tip_tag_is_still_labelled(harness):
    """A kind-1618 that names no commit is still a PR waiting for a review."""
    pr = make_pr("ea", commit="")
    harness.prs_data = ([pr], [])

    harness.sync()

    assert harness.label_calls == [(pr.id, ADDRESS, ("review-required",), ())]


def test_a_late_review_task_does_not_drop_the_label_state(harness):
    """`_note_review` merges into the link's detail; replacing it would lose the event ids."""
    pr = make_pr("eb", commit="c6")
    harness.prs_data = ([pr], [])
    harness.sync()
    assert label_detail(harness, pr.id)["label"] == ["review-required"]
    # Now the state a link from before review tasks existed is in: signalled, no task,
    # and a recorded failure to make one. `ngit_link_save` keeps a task id it is not given
    # (`COALESCE`), so the row is cut back by hand -- otherwise `_late_review_task` returns
    # on the id the previous poll wrote and the merge is never exercised.
    link = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)
    detail = label_detail(harness, pr.id)
    detail["task_error"] = "database is locked"
    harness.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr.id, state="signalled",
                                 board=BOARD, detail=detail)
    harness.board.db.execute("UPDATE tasks SET idempotency_key = NULL WHERE id = ?",
                             (link["task_id"],))
    harness.store.db.execute(
        "UPDATE ngit_links SET task_id = NULL WHERE repo = ? AND kind = ? AND source_id = ?",
        (ADDRESS, ngit.KIND_PR, pr.id))
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["task_id"] is None

    harness.prs_data = ([pr], [])
    harness.sync()

    after = label_detail(harness, pr.id)
    late = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["task_id"]
    assert late and late != link["task_id"]
    assert after["label"] == ["review-required"]
    assert after["label_events"] == detail["label_events"]
    assert harness.task(link["task_id"]) is not None


def test_the_label_vocabulary_is_a_config_default():
    assert ngit.BridgeConfig.from_config({}).labels is True
    assert ngit.BridgeConfig.from_config({"labels": False}).labels is False


# -- one card per PR: a revision reopens it, never duplicates -----------------

def pr_links(harness) -> list[dict]:
    """Every PR link the bridge holds for the one repository, regardless of the PR."""
    return harness.store.ngit_links(ADDRESS, ngit.KIND_PR)


def revision_of(pr, tip: str, *, created_at: int = 2000, event: str = "1") -> ngit.Revision:
    return ngit.Revision(pr_id=pr.id, commit=tip, created_at=created_at,
                         event_id=event * 64)


def test_two_passes_over_two_revisions_keep_one_card_and_one_link(harness):
    """The card is the queue, the PR is the record: two revisions, still one of each."""
    pr = make_pr("f1", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]

    for number, tip in enumerate(("b" * 40, "c" * 40), start=2):
        # The worker finished its review and handed the card back.
        set_review_status(harness, task_id, "review")
        harness.prs_data = ngit.PrFeed(revisions=[
            revision_of(pr, tip, created_at=1000 * number, event=str(number))])
        report = harness.sync()

        assert report.prs["revisions"] == 1 and report.prs["reopened"] == 1
        assert harness.task(task_id)["status"] == "ready"
        assert tip in harness.comments(task_id)[-1]["body"]
        assert len(harness.tasks()) == 1
        assert len(pr_links(harness)) == 1

    # The link still names the one card, and a quiet pass changes neither count.
    link = pr_links(harness)[0]
    assert link["task_id"] == task_id
    harness.prs_data = ([], [])
    report = harness.sync()
    assert report.prs["reopened"] == 0 and report.prs["revisions"] == 0
    assert len(harness.tasks()) == 1 and len(pr_links(harness)) == 1


def test_a_revision_naming_the_same_tip_again_changes_nothing(harness):
    """A relay replays the window on every reconnect; a replay is not a second revision."""
    pr = make_pr("f2", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "review")
    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    harness.sync()
    comments = len(harness.comments(task_id))

    report = harness.sync()

    assert report.prs["revisions"] == 0 and report.prs["reopened"] == 0
    assert harness.task(task_id)["status"] == "ready"
    assert len(harness.comments(task_id)) == comments


def test_a_second_1618_that_names_its_root_is_read_as_a_revision():
    """ngit can publish a republished proposal as another 1618 carrying `E`/`P`."""
    root, update = event_id("d1"), event_id("d2")
    events = [
        {"id": root, "kind": ngit.KIND_PR, "pubkey": REPO_OWNER, "created_at": 1000,
         "tags": [["a", ADDRESS], ["subject", "Add a widget"],
                  ["branch-name", "pr/widget"], ["c", "a" * 40]]},
        {"id": update, "kind": ngit.KIND_PR, "pubkey": REPO_OWNER, "created_at": 2000,
         "tags": [["a", ADDRESS], ["E", root], ["P", REPO_OWNER], ["c", "b" * 40],
                  ["alt", "git Pull Request Update"]]},
    ]
    feed = ngit.RelayPrs(["wss://relay.example"], ADDRESS,
                         fetcher=lambda **kwargs: events).events(0)

    assert [pr.id for pr in feed.prs] == [root]
    assert [(revision.pr_id, revision.commit) for revision in feed.revisions] == [
        (root, "b" * 40)]


def test_one_poll_carrying_a_proposal_and_its_republish_makes_one_card(harness):
    """The first poll must not need a link to know the two events are one PR."""
    root = make_pr("aa", commit="a" * 40, branch="pr/widget", created_at=1000)
    repeat = make_pr("ab", commit="b" * 40, branch="pr/widget", created_at=2000)
    harness.prs_data = ([root, repeat], [])

    report = harness.sync()

    assert len(harness.tasks()) == 1 and len(pr_links(harness)) == 1
    assert report.prs["tasks"] == 1 and report.prs["revisions"] == 1
    task = review_tasks(harness)[0]
    assert task["idempotency_key"] == f"ngit:pr:{root.id}"
    assert "b" * 40 in harness.comments(task["id"])[-1]["body"]


#: The two kind-1618 roots ngit 3.0.5 published for **one** branch, 108 seconds apart, when a
#: second commit was pushed and `grasp.day.ag` answered the ref update with 404, so the client
#: could not see the branch it had already proposed: `5e1f9dbe` and `08baeaaf`.
#:
#: Everything the bridge reads is the published value. Both roots carry the same `branch-name`
#: -- the branch as typed, cut to 60 characters -- and each names its own tip; the second one's
#: `merge-base` is the first one's tip, which is what a re-push (rather than a rebase) looks
#: like. `ngit pr view --json` prints `pr/<branch>-(5e1f9dbe)` and `...-(08baeaaf)`: that
#: suffix is the client's decoration to tell two roots apart on screen, and it is not on the
#: wire, so the fold's key matches. Do not make a fold or a test depend on it.
#:
#: Only `a` is this test's repository (the pass asks relays for one address, so an event naming
#: another could never reach it); `r`, `clone`, `p` and `alt` are dropped as unread here.
_NGIT_REPUSH_BRANCH = "hermes-nostr-gateway/t_fc8bf1c9-merge-route-the-rest-of-the-"
_NGIT_REPUSH_TIPS = ("1d7aa4ad88d10ff1fa0c724667fc7c91537c5cf7",
                     "0ad9f8dad061e7505f9acad6d7f2bf9b6c6a6c6a")
_NGIT_REPUSH = (
    {"id": "5e1f9dbe4280cb804d5d0197472e5c5d6914d68e3df2127b07d5d48aaad63051",
     "kind": ngit.KIND_PR,
     "pubkey": "740fcd907b10beedb9530aecf8cde87b274edd4706eafce108aa4fbb45cd2e29",
     "created_at": 1794152305,
     "content": "The authorship half of the merge route had one part: a verdict signed by a "
                "different npub.",
     "tags": [["a", ADDRESS],
              ["subject", "Merge route: a commit names its card, and a merge needs the card "
                          "finished"],
              ["branch-name", _NGIT_REPUSH_BRANCH],
              ["c", _NGIT_REPUSH_TIPS[0]],
              ["merge-base", "9b52622679dccf34e7fe646da8ac9fe70beaf112"]]},
    {"id": "08baeaaf6792bd3d1361ceed725d03271f3a2ad77d284963a41dbb93a5451b63",
     "kind": ngit.KIND_PR,
     "pubkey": "740fcd907b10beedb9530aecf8cde87b274edd4706eafce108aa4fbb45cd2e29",
     "created_at": 1794152413,
     "content": "The rules are in `working-on-this-repo.md` and `queue-and-prs.md`; this is the "
                "record of what was chosen.",
     "tags": [["a", ADDRESS],
              ["subject", "Docs: the two authorship decisions, and why the trailer is not "
                          "written at merge"],
              ["branch-name", _NGIT_REPUSH_BRANCH],
              ["c", _NGIT_REPUSH_TIPS[1]],
              ["merge-base", _NGIT_REPUSH_TIPS[0]]]},
)


def test_the_two_roots_ngit_publishes_for_one_repush_name_the_same_branch():
    """The published `branch-name` has no `(<pr-id>)`: it is the branch, cut to 60 characters."""
    feed = ngit.RelayPrs(["wss://relay.example"], ADDRESS,
                         fetcher=lambda **kwargs: list(_NGIT_REPUSH)).events(0)
    first, second = sorted(feed.prs, key=lambda pr: pr.created_at)

    assert (first.id, second.id) == (_NGIT_REPUSH[0]["id"], _NGIT_REPUSH[1]["id"])
    assert first.branch == second.branch == _NGIT_REPUSH_BRANCH
    assert len(first.branch) == 60 and "(" not in first.branch
    assert (first.commit, second.commit) == _NGIT_REPUSH_TIPS


def test_one_poll_carrying_both_roots_of_a_repush_makes_one_card(home, store):
    """The incident: one branch, two roots, two review cards. It is one card and one link."""
    # The captured event's author is a real contributor; this test is about the fold, so
    # the deployment's trusted list names that key rather than rewriting the event.
    harness = Harness(home, store,
                      trusted=(TRUSTED_HEX, _NGIT_REPUSH[0]["pubkey"]))
    harness.prs_data = ngit.RelayPrs(
        ["wss://relay.example"], ADDRESS,
        fetcher=lambda **kwargs: list(_NGIT_REPUSH)).events(0)

    report = harness.sync()

    tasks = review_tasks(harness)
    assert len(tasks) == 1 and len(pr_links(harness)) == 1
    assert tasks[0]["idempotency_key"] == f"ngit:pr:{_NGIT_REPUSH[0]['id']}"
    assert report.prs["tasks"] == 1 and report.prs["revisions"] == 1
    # The second root is folded as a revision: its tip is on the one card, and the channel
    # hears one announcement, not two.
    assert _NGIT_REPUSH_TIPS[1] in harness.comments(tasks[0]["id"])[-1]["body"]
    assert len(harness.messages) == 1


def test_a_repush_published_as_a_second_root_reopens_the_one_card(harness):
    """A pass that already has the root folds the re-push onto its card instead of a new one."""
    harness.prs_data = ngit.RelayPrs(
        ["wss://relay.example"], ADDRESS,
        fetcher=lambda **kwargs: [_NGIT_REPUSH[0]]).events(0)
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "review")

    harness.prs_data = ngit.RelayPrs(
        ["wss://relay.example"], ADDRESS,
        fetcher=lambda **kwargs: [_NGIT_REPUSH[1]]).events(0)
    report = harness.sync()

    assert len(harness.tasks()) == 1 and len(pr_links(harness)) == 1
    assert harness.task(task_id)["status"] == "ready"
    assert report.prs["reopened"] == 1 and report.prs["tasks"] == 0
    assert _NGIT_REPUSH_TIPS[1] in harness.comments(task_id)[-1]["body"]
    assert len(harness.messages) == 1


def test_a_republished_proposal_reopens_the_one_card(harness):
    """A rebased branch read as a fresh proposal is the same work, not a second PR."""
    pr = make_pr("f3", commit="a" * 40, branch="pr/widget")
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "review")

    repeat = make_pr("f4", commit="b" * 40, branch="pr/widget")
    harness.prs_data = ([repeat], [])
    report = harness.sync()

    assert len(harness.tasks()) == 1 and len(pr_links(harness)) == 1
    assert harness.task(task_id)["status"] == "ready"
    assert report.prs["reopened"] == 1 and report.prs["tasks"] == 0
    assert "b" * 40 in harness.comments(task_id)[-1]["body"]
    # No second channel message: the PR was announced when it opened.
    assert len(harness.messages) == 1


def test_a_republished_proposal_of_another_author_is_a_new_pr(harness):
    """Two authors' `pr/widget` branches are two proposals; only one author's is a repeat."""
    pr = make_pr("f5", commit="a" * 40, branch="pr/widget")
    harness.prs_data = ([pr], [])
    harness.sync()
    set_review_status(harness, review_tasks(harness)[0]["id"], "review")

    other = make_pr("f6", author=STRANGER_HEX, commit="b" * 40, branch="pr/widget")
    harness.prs_data = ([other], [])
    report = harness.sync()

    assert len(harness.tasks()) == 2 and len(pr_links(harness)) == 2
    assert report.prs["tasks"] == 1 and report.prs["reopened"] == 0


def test_a_republished_proposal_of_a_merged_pr_is_a_new_pr(harness):
    """Applied is final: the branch being pushed again is new work, not a revision."""
    pr = make_pr("f7", commit="a" * 40, branch="pr/widget")
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1500)
    harness.prs_data = ([pr], [merge])
    harness.sync()
    assert harness.task(task_id)["status"] == "done"

    repeat = make_pr("f8", commit="b" * 40, branch="pr/widget", created_at=2000)
    harness.prs_data = ([repeat], [])
    report = harness.sync()

    assert len(harness.tasks()) == 2 and len(pr_links(harness)) == 2
    assert report.prs["tasks"] == 1 and report.prs["revisions"] == 0


def test_a_revision_does_not_edit_a_card_that_is_still_running(harness):
    """A worker reading the old tip is told the tip moved; its claim is not taken away."""
    pr = make_pr("f9", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "running")

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    report = harness.sync()

    assert harness.task(task_id)["status"] == "running"
    assert report.prs["revisions"] == 1 and report.prs["reopened"] == 0
    assert "b" * 40 in harness.comments(task_id)[-1]["body"]


def test_a_revision_does_not_release_a_held_card(harness):
    """A blocked card waits for a person's authorisation, not for a revision."""
    pr = make_pr("fa", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "blocked")

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    report = harness.sync()

    assert harness.task(task_id)["status"] == "blocked"
    assert report.prs["reopened"] == 0
    # The tip is still recorded: the label and the next pass read the revision.
    assert label_detail(harness, pr.id)["tip"] == "b" * 40


# -- a verdict finishes its card ----------------------------------------------
#
# The reviewer publishes the verdict on the PR and parks the card as its hand-off. The
# verdict's consequence is the bridge's: approved finishes the card, changes-requested puts
# it back on the implementer's desk. Neither leaves it parked, because a verdict is not a
# question and only a question is a person's.

def task_events(harness, task_id: str, kind: str = "") -> list[dict]:
    """One card's events, oldest first, with the payload already parsed."""
    sql = "SELECT * FROM task_events WHERE task_id = ?"
    params: list = [task_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    rows = harness.board.db.execute(sql + " ORDER BY id", tuple(params)).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload"] or "{}")
        out.append({"kind": row["kind"], "run_id": row["run_id"], "payload": payload})
    return out


def park_with_verdict(harness, task_id: str, label: str, *, tip: str = "c0ffee") -> None:
    """What the review brief tells a reviewer to do at the end of its run."""
    assert harness.board.block(
        task_id, kind="needs_input", author="review",
        comment=f"Verdict: {label} -- comment {'a' * 64} on the PR root, covering tip {tip}.")


def test_an_approved_verdict_finishes_the_review_card(harness):
    """Approved -> `done`, the verdict as the result, and the merge still the operator's."""
    pr, task_id = pr_with_review_task(harness)
    event = review_verdict(
        harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64, created_at=2000,
        body="Verdict: approved\n\nGates green at c0ffee; the docs claims check out.")
    park_with_verdict(harness, task_id, "approved")

    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "done"
    result = str(row["result"])
    assert "Verdict: approved" in result and event in result
    assert "Gates green at c0ffee" in result  # the verdict's own words, not a summary
    # The public half says the same thing, and the guard is gone: nothing is left to refuse.
    assert harness.label_calls[-1][2] == ("approved",)
    assert guard_triggers(harness) == []
    assert report.prs["approved"] == 1
    assert any("approved, and finished" in note for note in report.notes)
    # Finished is not merged: no merge event was invented for it.
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] != "merged"


def test_an_approval_is_one_completion_however_many_passes_see_it(harness):
    """A poll after the pass that acted writes nothing: the card's status is the state."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64, created_at=2000)
    park_with_verdict(harness, task_id, "approved")
    harness.sync()
    comments = len(harness.comments(task_id))

    harness.prs_data = ([], [])
    report = harness.sync()

    assert harness.task(task_id)["status"] == "done"
    assert len(harness.comments(task_id)) == comments
    assert report.prs["approved"] == 0
    assert len([e for e in task_events(harness, task_id, "completed")]) == 1


def test_a_changes_requested_verdict_returns_the_card_to_its_implementer(harness):
    """The card goes back to the queue that wrote the PR, `ready`, with the findings on it."""
    pr, task_id = pr_with_review_task(harness)
    event = review_verdict(
        harness, pr.id, ngit_labels.CHANGES_REQUESTED, event_id="c" * 64, created_at=2000,
        body="Verdict: changes-requested\n\nThe second claim is not what the code does.")
    park_with_verdict(harness, task_id, "changes-requested")

    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "ready"
    assert row["assignee"] == "default"           # the implementer, not the reviewer
    assert row["block_kind"] is None
    # The brief is a fix brief now: the next claim must not review the same tip again.
    assert "changes-requested" in row["body"] and "fix" in row["body"].lower()
    assert "The second claim is not what the code does." in row["body"]
    assert any("The second claim is not what the code does." in c["body"]
               for c in harness.comments(task_id))
    changes = task_events(harness, task_id, "changes_requested")
    assert len(changes) == 1
    assert changes[0]["payload"]["verdict"] == event
    assert changes[0]["payload"]["implementer"] == "default"
    assert report.prs["changes"] == 1
    # The PR is still open, so nothing may complete the card by hand.
    with pytest.raises(sqlite3.IntegrityError):
        harness.board.db.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (task_id,))


def test_a_verdict_does_not_answer_a_card_blocked_on_a_question(harness):
    """A person's question is not a verdict's to settle, however green the verdict is."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64, created_at=2000)
    assert harness.board.block(
        task_id, kind="needs_input", author="holly",
        comment="The card's premise is disproved -- a person decides whether it is re-filed.")

    report = harness.sync()

    assert harness.task(task_id)["status"] == "blocked"
    assert report.prs["approved"] == 0
    # The label is still the verdict's: the PR is approved even though the card waits.
    assert harness.label_calls[-1][2] == ("approved",)


def test_a_revision_reopens_a_verdict_parked_card(harness):
    """Belt and braces: a card parked by the old brief is not stranded by a re-push.

    No verdict is readable in this pass (the relay has not served the comment), so the
    revision is the only thing that can move the card -- and it must, or the fix nobody
    saw lands on a card nobody runs.
    """
    pr, task_id = pr_with_review_task(harness)
    park_with_verdict(harness, task_id, "changes-requested")
    assert harness.task(task_id)["status"] == "blocked"

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=3000)])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "ready"
    assert report.prs["reopened"] == 1
    assert len(harness.tasks()) == 1 and len(pr_links(harness)) == 1
    assert len(task_events(harness, task_id, "review_reopened")) == 1
    # The brief is the review brief again, for the new tip: this claim reviews, it does not fix.
    assert "b" * 40 in row["body"] and "Review this PR" in row["body"]


def test_a_revision_leaves_a_card_blocked_on_a_question_alone(harness):
    """The marker is what separates the bridge's park from a person's: no marker, no reopen."""
    pr, task_id = pr_with_review_task(harness)
    assert harness.board.block(
        task_id, kind="needs_input", author="holly",
        comment="Which of the two incompatible claims is the intended behaviour? A person.")

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=3000)])
    report = harness.sync()

    assert harness.task(task_id)["status"] == "blocked"
    assert report.prs["reopened"] == 0
    assert task_events(harness, task_id, "review_reopened") == []


def test_a_pushed_fix_puts_the_card_back_in_the_review_queue(harness):
    """The fix round's other end: `Revision:` is a pushed fix, and the next claim reviews it."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.CHANGES_REQUESTED, event_id="c" * 64,
                   created_at=2000)
    park_with_verdict(harness, task_id, "changes-requested")
    harness.sync()
    assert harness.task(task_id)["status"] == "ready"

    # The implementer's run: it pushes the fix (the revision lands first) and then says so.
    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=3000)])
    harness.sync()
    assert harness.board.block(
        task_id, kind="needs_input", author="default",
        comment="Revision: bbbb -- the fix is pushed and the gates are green.")

    harness.prs_data = ([], [])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "ready"
    assert report.prs["reopened"] == 1
    # The claim reviews the tip the PR carries, not the fix that already happened.
    assert "Review this PR" in row["body"] and "b" * 40 in row["body"]
    assert any("Fix pushed" in c["body"] for c in harness.comments(task_id))


# -- reconcile: a verdict behind the pass's cursor ----------------------------
#
# The pass that reads a verdict walks what the poll carried, so a verdict already consumed
# is never read again — which made every card parked before the bridge applied verdicts
# unhealable, and every later fix to that path unhealable in production too.
# `hng ngit reconcile` asks the PR root again, for the cards parked on a verdict and only
# those: a person's block matches no marker and is never listed, let alone moved.

def record_tip(harness, pr_id: str, tip: str, *, at: int) -> None:
    """The link's note as a revision that landed would have left it (the event was consumed)."""
    link = harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr_id)
    detail = json.loads(link["detail"] or "{}")
    detail["tip"], detail["tip_at"] = tip, at
    harness.store.ngit_link_save(ADDRESS, ngit.KIND_PR, pr_id,
                                 state=str(link["state"]), detail=detail)


def test_reconcile_finishes_a_card_parked_on_a_verdict_the_pass_never_saw(harness):
    """The card the fix was written for: approved, and the poll no longer carries the verdict."""
    pr, task_id = pr_with_review_task(harness)
    event = review_verdict(harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64,
                           created_at=2000)
    park_with_verdict(harness, task_id, "approved")
    harness.prs_data = ([], [])
    label_calls = len(harness.label_calls)

    report = harness.reconcile()

    row = harness.task(task_id)
    assert row["status"] == "done"
    assert "Verdict: approved" in str(row["result"]) and event in str(row["result"])
    assert [(card.task_id, card.to) for card in report.cards] == [(task_id, "done")]
    assert report.errors == []
    # Finished is not merged, and the label stays the next sync's to write.
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] != "merged"
    assert len(harness.label_calls) == label_calls
    # The anchor records which verdict the card answered.
    assert label_detail(harness, pr.id)["verdict_event"] == event


def test_a_reconcile_dry_run_names_the_card_and_writes_nothing(harness):
    """The blast radius, before it lands: the card, the transition, and not one byte written."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64, created_at=2000)
    park_with_verdict(harness, task_id, "approved")
    harness.prs_data = ([], [])
    comments = len(harness.comments(task_id))
    detail = dict(label_detail(harness, pr.id))

    report = harness.reconcile(dry_run=True)

    assert [(card.task_id, card.status, card.to) for card in report.cards] == [
        (task_id, "blocked", "done")]
    assert report.cards[0].verdict == "approved" and report.cards[0].verdict_event == "a" * 64
    assert harness.task(task_id)["status"] == "blocked"
    assert len(harness.comments(task_id)) == comments
    assert task_events(harness, task_id, "completed") == []
    assert label_detail(harness, pr.id) == detail
    printed = "\n".join(report.lines())
    assert task_id in printed and pr.id[:8] in printed and "would move" in printed


def test_a_second_reconcile_changes_nothing(harness):
    """Idempotent by construction: the first run leaves a `ready` card, which is no candidate."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.CHANGES_REQUESTED, event_id="c" * 64,
                   created_at=2000)
    park_with_verdict(harness, task_id, "changes-requested")
    harness.prs_data = ([], [])
    harness.reconcile()
    comments = len(harness.comments(task_id))
    changes = len(task_events(harness, task_id, "changes_requested"))

    report = harness.reconcile()

    assert report.cards == []
    assert any("no card is parked on a verdict" in note for note in report.notes)
    assert harness.task(task_id)["status"] == "ready"
    assert len(harness.comments(task_id)) == comments
    assert len(task_events(harness, task_id, "changes_requested")) == changes


def test_reconcile_returns_a_changes_requested_card_to_its_implementer(harness):
    """The other half of the four: back on the implementer's desk with the findings."""
    pr, task_id = pr_with_review_task(harness)
    event = review_verdict(
        harness, pr.id, ngit_labels.CHANGES_REQUESTED, event_id="c" * 64, created_at=2000,
        body="Verdict: changes-requested\n\nThe second claim is not what the code does.")
    park_with_verdict(harness, task_id, "changes-requested")
    harness.prs_data = ([], [])

    report = harness.reconcile()

    row = harness.task(task_id)
    assert row["status"] == "ready" and row["assignee"] == "default"
    assert row["block_kind"] is None
    # The brief is the fix brief: the next claim fixes the tip, it does not review it again.
    assert "The second claim is not what the code does." in row["body"]
    assert "changes-requested" in row["body"]
    assert [(card.task_id, card.to, card.who) for card in report.cards] == [
        (task_id, "ready", "default")]
    changes = task_events(harness, task_id, "changes_requested")
    assert len(changes) == 1 and changes[0]["payload"]["verdict"] == event


def test_reconcile_leaves_a_card_blocked_on_a_question_alone(harness):
    """A person's question is not a verdict's to settle, however green the verdict is."""
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.APPROVED, event_id="a" * 64, created_at=2000)
    assert harness.board.block(
        task_id, kind="needs_input", author="holly",
        comment="Which of the two incompatible claims is intended? A person decides.")

    report = harness.reconcile()

    assert report.cards == []
    assert harness.task(task_id)["status"] == "blocked"
    assert task_events(harness, task_id, "completed") == []
    assert any("no card is parked on a verdict" in note for note in report.notes)


def test_reconcile_leaves_a_card_parked_on_a_verdict_it_cannot_read(harness):
    """Nothing to apply is not a reason to guess: the card stays where the reviewer left it."""
    _pr, task_id = pr_with_review_task(harness)
    park_with_verdict(harness, task_id, "approved")
    harness.prs_data = ([], [])

    report = harness.reconcile()

    assert report.cards == []
    assert harness.task(task_id)["status"] == "blocked"
    assert any("no verdict" in note for note in report.notes)


def test_a_verdict_a_newer_tip_has_superseded_returns_the_card_to_review(harness):
    """Finishing on a superseded verdict, or re-requesting a fix already pushed, are both wrong.

    The revision that moved the tip was consumed too, so the card is still parked on a
    verdict that covers the tip before it. The card goes back to the review queue for the
    tip the PR carries — the transition a revision writes.
    """
    pr, task_id = pr_with_review_task(harness)
    review_verdict(harness, pr.id, ngit_labels.CHANGES_REQUESTED, event_id="c" * 64,
                   created_at=2000)
    park_with_verdict(harness, task_id, "changes-requested")
    record_tip(harness, pr.id, "b" * 40, at=3000)
    # The review role's own profile, as a deployment with a separate review bot has it: the
    # card must go back to that desk, not to the implementer's.
    harness.config = ngit.BridgeConfig(assignee="default", lookback_hours=0,
                                       review_assignee="review")
    harness.board.db.execute("UPDATE tasks SET assignee = 'review' WHERE id = ?", (task_id,))

    report = harness.reconcile()

    row = harness.task(task_id)
    assert row["status"] == "ready" and row["assignee"] == "review"
    assert "Review this PR" in row["body"] and "b" * 40 in row["body"]
    assert [(card.task_id, card.to, card.who) for card in report.cards] == [
        (task_id, "ready", "review")]
    assert "earlier tip" in report.cards[0].why and "b" * 12 in report.cards[0].why
    assert task_events(harness, task_id, "completed") == []
    assert len(task_events(harness, task_id, "review_reopened")) == 1
    assert any("back in the review queue" in c["body"] for c in harness.comments(task_id))


def test_a_done_card_on_a_pr_that_never_merged_reopens(harness):
    """A card finished before the guard existed is still reopened by a new tip.

    The `done` state here is written past the guard on purpose: it is history, not a move a
    worker can make now.
    """
    pr, task_id = pr_with_review_task(harness)
    harness.board.comment(task_id, "review", "Verdict: approved")
    set_review_status(harness, task_id, "done", force=True)
    harness.board.db.execute("UPDATE tasks SET completed_at = 1 WHERE id = ?", (task_id,))

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    report = harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "ready" and row["completed_at"] is None
    assert report.prs["reopened"] == 1
    assert "b" * 40 in harness.comments(task_id)[-1]["body"]


def test_reopening_keeps_the_model_pin_and_resets_the_failure_count(harness):
    """A revision is not a channel-config change, and it is a fresh attempt."""
    pr = make_pr("fb", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "review")
    harness.board.db.execute(
        "UPDATE tasks SET model_override = ?, provider_override = ?,"
        " consecutive_failures = 3, last_failure_error = 'boom' WHERE id = ?",
        ("deepseek-v4.1-flash", "opencode-go", task_id))

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    harness.sync()

    row = harness.task(task_id)
    assert row["status"] == "ready"
    assert row["model_override"] == "deepseek-v4.1-flash"
    assert row["provider_override"] == "opencode-go"
    assert row["consecutive_failures"] == 0
    assert row["last_failure_error"] is None


def test_a_late_update_against_an_applied_pr_changes_nothing(harness):
    pr = make_pr("fc", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1500)
    harness.prs_data = ([pr], [merge])
    harness.sync()
    assert harness.task(task_id)["status"] == "done"
    before = [comment["body"] for comment in harness.comments(task_id)]

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    report = harness.sync()

    assert harness.task(task_id)["status"] == "done"
    assert [comment["body"] for comment in harness.comments(task_id)] == before
    assert report.prs["reopened"] == 0 and report.prs["revisions"] == 0


def test_a_revision_cannot_reopen_while_the_merge_notice_is_still_queued(home, store):
    """The merge is on the row before the notice lands, so the card cannot be revived."""
    def down(who, text, key=""):
        raise ngit.NotifyError("the daemon is not running")

    made = Harness(home, store, notifier=down)
    try:
        pr = make_pr("fd", commit="a" * 40)
        made.prs_data = ([pr], [])
        made.sync()
        task_id = review_tasks(made)[0]["id"]
        merge = ngit.Merge(pr_id=pr.id, commit="d" * 40, created_at=1500)
        made.prs_data = ([pr], [merge])
        made.sync()
        assert made.task(task_id)["status"] == "done"
        assert made.store.ngit_link(ADDRESS, ngit.KIND_PR, pr.id)["state"] == "pending"

        made.prs_data = ngit.PrFeed(revisions=[
            revision_of(pr, "b" * 40, created_at=2000)])
        report = made.sync()

        assert made.task(task_id)["status"] == "done"
        assert report.prs["reopened"] == 0
    finally:
        made.close()


def test_a_revision_of_a_pr_this_bridge_never_announced_creates_nothing(harness):
    pr = make_pr("fe", commit="a" * 40)
    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])

    report = harness.sync()

    assert harness.tasks() == [] and pr_links(harness) == []
    assert report.prs["revisions"] == 0 and report.prs["reopened"] == 0


def test_a_dry_run_counts_a_reopen_and_writes_nothing(harness):
    pr = make_pr("ff", commit="a" * 40)
    harness.prs_data = ([pr], [])
    harness.sync()
    task_id = review_tasks(harness)[0]["id"]
    set_review_status(harness, task_id, "review")
    comments = len(harness.comments(task_id))

    harness.prs_data = ngit.PrFeed(revisions=[revision_of(pr, "b" * 40, created_at=2000)])
    report = harness.sync(dry_run=True)

    assert report.prs["revisions"] == 1 and report.prs["would_reopen"] == 1
    assert harness.task(task_id)["status"] == "review"
    assert len(harness.comments(task_id)) == comments
    assert label_detail(harness, pr.id).get("tip") != "b" * 40


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
        "tags": [["e", event_id("d6"), "", "root"], ["merge-commit", "a" * 40]]},
        [REPO_OWNER])

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
        ]}, [REPO_OWNER])

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
                 ["r", "1" * 40], ["r", "2" * 40]]}, [REPO_OWNER])

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


def test_the_hosting_servers_are_part_of_the_read_set(tmp_path):
    """A retracted label must reach every server that hosts the repo, not just the hint."""
    coordinate = sdk.Nip19Coordinate(sdk.Coordinate.parse(ADDRESS),
                                     [sdk.RelayUrl.parse("wss://relay.example")]).to_bech32()

    def fake_run(repo, args, *, timeout=120.0):
        return {"is_nostr_repo": True, "coordinate": coordinate,
                "grasp_servers": ["relay.example", "other.example", "relay.example",
                                  "wss://third.example"]}

    repository = ngit.Ngit(tmp_path, runner=fake_run).repository()

    assert repository.relays == ("wss://relay.example", "wss://other.example",
                                 "wss://third.example")


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


# -- the relay read the label pass is derived from ----------------------------

LABEL_PR = "ab" * 32


def relay_comment(relay: str, *, event_id: str, body: str, created_at: int,
                  pubkey: str = TRUSTED_HEX) -> tuple[str, dict]:
    return (relay, {"id": event_id, "kind": ngit.KIND_COMMENT, "content": body,
                    "created_at": created_at, "relay": relay, "pubkey": pubkey,
                    "tags": [["E", LABEL_PR], ["K", "1618"]]})


def relay_label(relay: str, *, value: str, event_id: str, pr_id: str = LABEL_PR,
                namespace: str = "#t") -> tuple[str, dict]:
    return (relay, {"id": event_id, "kind": ngit_labels.KIND_LABEL, "content": "",
                    "created_at": 1000, "relay": relay,
                    "tags": [["L", namespace], ["l", value, namespace], ["e", pr_id]]})


def test_the_relay_read_takes_the_newest_verdict_and_keeps_each_relay_s_answer():
    calls: list[dict] = []

    def fetch(**kwargs):
        calls.append(kwargs)
        if kwargs["kinds"] == [ngit.KIND_COMMENT]:
            return [relay_comment("wss://a.example", event_id="c" * 64,
                                  body="Verdict: changes-requested", created_at=1000),
                    relay_comment("wss://b.example", event_id="d" * 64,
                                  body="Verdict: approved", created_at=2000)]
        return [relay_label("wss://a.example", value="in-review", event_id="l" * 64)]

    reader = ngit.RelayLabels(["wss://a.example", "wss://b.example"], ADDRESS, fetcher=fetch,
                              reviewers=[TRUSTED_HEX])
    read = reader.read([LABEL_PR]).prs[LABEL_PR]

    assert read.verdict is not None
    assert (read.verdict.label, read.verdict.relay) == ("approved", "wss://b.example")
    assert read.relay == "wss://b.example"
    assert [label.value for label in read.labels] == ["in-review"]
    assert "wss://a.example" in read.disagreement()
    # One filter per PR, one request per relay per kind: the relay that answered is kept.
    assert calls[0]["tag"] == "E" and calls[0]["values"] == [LABEL_PR]
    assert calls[1]["tag"] == "e" and calls[1]["a_tag"] == ADDRESS


def test_a_label_for_another_pr_or_another_namespace_is_not_this_pr_s():
    def fetch(**kwargs):
        if kwargs["kinds"] == [ngit.KIND_COMMENT]:
            return []
        return [relay_label("wss://a.example", value="approved", event_id="l" * 64,
                            pr_id="cd" * 32),
                relay_label("wss://a.example", value="approved", event_id="m" * 64,
                            namespace="#other")]

    reader = ngit.RelayLabels(["wss://a.example"], ADDRESS, fetcher=fetch)
    read = reader.read([LABEL_PR]).prs[LABEL_PR]

    assert read.labels == ()


def test_a_read_with_no_relays_is_an_error_not_an_empty_answer():
    read = ngit.RelayLabels([], ADDRESS).read([LABEL_PR])
    assert read.prs == {}
    assert any("empty" in problem for problem in read.errors)


def test_a_read_that_finds_nothing_is_an_answer_not_an_error():
    """A repo whose open PRs have no comments yet is quiet, not failing."""
    def fetch(**kwargs):
        return []

    read = ngit.RelayLabels(["wss://a.example"], ADDRESS, fetcher=fetch).read([LABEL_PR])
    assert read.errors == ()
    assert read.prs[LABEL_PR].verdict is None and read.prs[LABEL_PR].labels == ()


def test_a_reader_that_raises_is_an_error_not_an_exception():
    def fetch(**kwargs):
        raise OSError("relay hung up")

    read = ngit.RelayLabels(["wss://a.example"], ADDRESS, fetcher=fetch).read([LABEL_PR])
    assert read.prs == {}
    assert any("OSError" in problem for problem in read.errors)



# -- the ready-label gate -----------------------------------------------------

def test_an_issue_waits_until_a_trusted_ready_label_is_added(harness):
    """Filing is not asking for work: the card appears when a trusted npub says so."""
    issue = make_issue("f1", subject="Add a flag")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.unready.add(issue.hex)

    first = harness.sync()

    assert harness.tasks() == []
    assert first.issues["waiting"] == 1 and first.issues["created"] == 0
    assert harness.store.ngit_link(ADDRESS, ngit.KIND_ISSUE, issue.hex)["state"] == "waiting"
    assert len(harness.messages) == 1
    assert "ready" in harness.messages[0][1] and issue.id in harness.messages[0][1]

    # The same issue on the next poll says nothing new: the link is the memory.
    again = harness.sync()
    assert again.issues["existing"] == 1 and len(harness.messages) == 1

    # A trusted author labels it ready: now the card is made, with the usual notice.
    harness.unready.discard(issue.hex)
    third = harness.sync()

    tasks = harness.tasks()
    assert len(tasks) == 1 and tasks[0]["status"] == "ready"
    assert tasks[0]["idempotency_key"] == f"ngit:issue:{issue.hex}"
    assert third.issues["created"] == 1 and third.issues["ready"] == 1
    assert len(harness.messages) == 2
    assert tasks[0]["id"] in harness.messages[1][1]


def test_a_ready_label_from_an_untrusted_npub_does_not_queue(home, store):
    issue = make_issue("f2")
    made = Harness(home, store, issues=[issue], trusted=())
    made.details = {issue.hex: issue}
    made.ready_labels[issue.hex] = (ngit_labels.Label(
        value="ready", event_id="a" * 64, author=STRANGER_HEX),)
    try:
        report = made.sync()
        assert made.tasks() == []
        assert report.issues["waiting"] == 1
    finally:
        made.close()


def test_the_gate_can_be_turned_off(harness):
    harness.no_gate()
    issue = make_issue("f3", author=STRANGER_HEX)
    harness.issues = [issue]
    harness.details = {issue.hex: issue}

    report = harness.sync()

    assert harness.tasks()[0]["status"] == "blocked"
    assert report.issues["held"] == 1


# -- mentions ----------------------------------------------------------------

def mention_event(seed: str, *, author: str = TRUSTED_HEX, root: str,
                  content: str = "what does this do?", tags: list | None = None) -> dict:
    """A NIP-22 comment, the way gitworkshop writes an @npub mention."""
    return {
        "id": event_id(seed),
        "pubkey": author,
        "kind": ngit_labels.KIND_COMMENT,
        "created_at": 100,
        "content": content,
        "tags": tags if tags is not None else [
            ["E", root, "", REPO_OWNER], ["K", "1621"], ["P", REPO_OWNER],
            ["e", root, "", REPO_OWNER], ["k", "1621"], ["p", SELF_HEX],
        ],
    }


def test_a_comment_that_mentions_the_gateway_becomes_one_reply_task(harness):
    issue = make_issue("a1", subject="How do I configure this?")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.comments_by_root[issue.hex] = [mention_event("a2", root=issue.hex)]

    report = harness.sync()

    assert report.mentions["created"] == 1 and report.mentions["seen"] == 1
    reply = [task for task in harness.tasks() if task["idempotency_key"].startswith(
        "ngit:mention:")][0]
    assert reply["status"] == "ready" and reply["assignee"] == "default"
    assert "mentioned this gateway" in reply["body"]
    assert issue.id in reply["body"]              # the thread, as the nevent people use
    assert "ngit issue comment" in reply["body"] and "--reply-to" in reply["body"]
    assert harness.store.ngit_mention(ADDRESS, event_id("a2"))["task_id"] == reply["id"]

    # A re-poll reads the same comment and files nothing again.
    again = harness.sync()
    assert again.mentions["created"] == 0 and again.mentions["existing"] == 1
    assert len([task for task in harness.tasks()
                if task["idempotency_key"].startswith("ngit:mention:")]) == 1


def test_a_mention_from_an_untrusted_npub_is_remembered_and_ignored(harness):
    issue = make_issue("b1")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.comments_by_root[issue.hex] = [
        mention_event("b2", author=STRANGER_HEX, root=issue.hex)]

    report = harness.sync()

    assert report.mentions["ignored"] == 1 and report.mentions["created"] == 0
    assert not [task for task in harness.tasks()
                if task["idempotency_key"].startswith("ngit:mention:")]
    row = harness.store.ngit_mention(ADDRESS, event_id("b2"))
    assert row is not None and not row["task_id"]


def test_a_comment_that_names_somebody_else_is_not_a_mention(harness):
    issue = make_issue("c1")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.comments_by_root[issue.hex] = [
        mention_event("c2", root=issue.hex, content="just a thought",
                      tags=[["E", issue.hex], ["K", "1621"], ["p", OTHER_HEX]])]

    report = harness.sync()

    assert report.mentions["seen"] == 0 and report.mentions["created"] == 0


def test_the_npub_in_the_words_counts_when_the_tag_is_missing(harness):
    """A client that drops the `p` tag still leaves the reference in the body."""
    issue = make_issue("d1")
    harness.issues = [issue]
    harness.details = {issue.hex: issue}
    harness.comments_by_root[issue.hex] = [
        mention_event("d2", root=issue.hex,
                      content=f"nostr:{npub(SELF_HEX)} can you look?",
                      tags=[["E", issue.hex], ["K", "1621"]])]

    report = harness.sync()

    assert report.mentions["created"] == 1


def test_a_mention_on_a_pull_request_uses_the_pr_command(harness):
    pr = make_pr("e1", subject="Add a widget")
    harness.prs_data = ngit.PrFeed(prs=(pr,), merges=(), revisions=(), closed=())
    harness.comments_by_root[pr.id] = [mention_event(
        "e2", root=pr.id, content="why this approach?",
        tags=[["E", pr.id], ["K", "1618"], ["p", SELF_HEX]])]

    harness.sync()

    reply = [task for task in harness.tasks() if task["idempotency_key"].startswith(
        "ngit:mention:")][0]
    assert "on PR" in reply["title"] and "ngit pr comment" in reply["body"]


def test_issue_labels_accepts_any_namespace_and_keeps_the_author():
    """The issue gate asks "who said ready", not which display namespace wrote it."""
    issue_id = "f" * 64

    def fetch(**kwargs):
        return [("wss://a.example", {
            "id": "a" * 64, "kind": ngit_labels.KIND_LABEL, "pubkey": TRUSTED_HEX,
            "created_at": 10,
            "tags": [["L", "ngit.tools"], ["l", "ready", "ngit.tools"],
                     ["a", ADDRESS], ["e", issue_id]],
            "content": "ok",
        })]

    labels = ngit.RelayLabels(["wss://a.example"], ADDRESS, fetcher=fetch).issue_labels(
        [issue_id])
    assert [(label.value, label.author) for label in labels[issue_id]] == [
        ("ready", npub(TRUSTED_HEX))]


def test_issue_labels_ignores_a_label_for_another_repository():
    issue_id = "e" * 64

    def fetch(**kwargs):
        return [("wss://a.example", {
            "id": "b" * 64, "kind": ngit_labels.KIND_LABEL, "pubkey": TRUSTED_HEX,
            "created_at": 10,
            "tags": [["l", "ready"], ["a", "30617:" + "99" * 32 + ":other"],
                     ["e", issue_id]],
        })]

    labels = ngit.RelayLabels(["wss://a.example"], ADDRESS, fetcher=fetch).issue_labels(
        [issue_id])
    assert labels[issue_id] == ()
