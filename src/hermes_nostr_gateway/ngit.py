"""The ngit bridge: a channel's repo issues become work, its PRs are announced in it.

One mapping, two directions. `concord.projects` already says which checkout a channel's
work belongs to; it now also says which board that work queues on (see `projects.py`), so
the same lookup serves the inbound and the outbound half of this module:

    inbound   kind-1621 issue    -> a task on the channel's board
              kind-1622 comment  -> a task comment, in the order the issue carries them
    outbound  kind-1618 PR       -> one "ready for review" message in the channel, and one
                                   review task on that board (a verdict, never a merge:
                                   merging is the operator's)
              kind-1631 applied  -> one "merged as <commit>" follow-up, which also closes
                                   the review task

A review task is a task like any other: the bridge names the PR, says which profile it is
dispatched to, and stops there. The model is the board's to decide -- the board's default,
then the assignee profile's own model -- so reviewing on a different model is the operator's
call (`hermes kanban set-model`), not a second knob in this config.

Authorisation is the control pane's. An issue from a trusted npub (`control.trusted_npubs`
or the concord plane's own list) is queued `ready`; any other author is held `blocked` and
asked about in the channel, so a stranger cannot put work in the queue by filing an issue
and nobody's issue is silently worked. The question is durable state -- a task comment plus
the `ngit_links` row -- not a live turn's memory, so a restart does not forget it and a
re-poll does not ask twice.

State (the `ngit_*` tables in `store.py`): one link per issue and per PR, carrying the task
id and the lifecycle the bridge reached, plus a per-repo high-water mark so a poll asks only
for what is new. A task deleted by hand leaves its `deleted` link behind, so the next sync
does not silently recreate it; `--recreate` is how an operator asks for it back.

A PR notice is queued before it is posted and marked sent only when the daemon took it, so a
poll that is cut off mid-send (a turn holds the daemon past the socket timeout) leaves the
notice `pending` rather than lost. Each notice carries an idempotency key, which is what makes
retrying it safe: the daemon answers a repeated key with the message it already published.

Nothing about a particular box is compiled in: the repo path, board slug, conversation and
trusted npubs all arrive from configuration, and the one thing read from the checkout is the
repository's own NIP-34 address.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import nostr_sdk as sdk

from .control import ControlConfig, as_hex
from .fetch import fetch_many_blocking
from .kanban import Anchor, Board, BoardError
from .projects import ProjectMap, board_dir, hermes_home
from .store import Store

log = logging.getLogger("hng.ngit")

#: NIP-34 kinds this bridge reads. 1630-1633 are the status kinds; only `applied` is news.
KIND_ISSUE = 1621
KIND_ISSUE_COMMENT = 1622
KIND_PR = 1618
KIND_APPLIED = 1631

#: How a kind-1631 names the commit(s) it landed, most specific first. NIP-34 spells a merge
#: `merge-commit`; ngit 3.0.4 writes `merge-commit-id` instead -- one oid for a merge commit,
#: every commit the push added for a fast-forward -- so both are read. An applied patch (no
#: merge commit exists) carries `applied-as-commits` and is the fallback.
MERGE_COMMIT_TAGS = ("merge-commit", "merge-commit-id")
APPLIED_COMMIT_TAG = "applied-as-commits"

#: The plane whose trusted list decides an issue's author. Concord, because that is the
#: plane a channel belongs to; `trusted_in` unions it with the global list.
TRUSTED_PLANE = "concord"

#: States a link can hold. `held` means the question has not reached the channel yet (a
#: retry is correct); `asked` means it did (asking again is not). For a PR, `pending` is the
#: same idea: it is known, and its notice is either queued or posted but not yet confirmed.
ISSUE_STATES = frozenset({"ready", "held", "asked", "authorised", "declined", "deleted"})
PR_STATES = frozenset({"pending", "signalled", "merged"})

#: Seconds of overlap on a relay cursor. Relays apply `since` inclusively and clocks drift;
#: the link table, not the cursor, is what stops a replayed event being reported twice.
CURSOR_OVERLAP_S = 300

#: How long one notice waits for the daemon. A turn can hold the daemon for longer than a
#: socket round trip takes, so the timeout is generous -- but it still has to end: the notice
#: is queued, and giving up on it is what lets the poll finish and the next one retry.
NOTIFY_TIMEOUT_S = 20.0

#: Seconds the daemon's watcher waits after the last event for a channel before running a
#: pass. Long enough that a PR revision and its status event coalesce, short enough that
#: "within a minute" holds. See docs/ngit-bridge.md, "Waking the pass".
DEFAULT_WATCH_DEBOUNCE_S = 30.0

#: The oldest `ngit` the bridge will drive. Pinned because the bridge parses `--json`
#: documents and reads the `merge-commit-id` tag, and a missing or older binary must fail at
#: a preflight rather than at 03:00 in a cron log. Bump this when a document the bridge
#: reads changes shape, and keep `hng doctor` on the same constant.
NGIT_FLOOR = "3.0.4"

#: Named in every dependency error: a version, and how to get it.
NGIT_INSTALL_HINT = (
    f"install ngit >= {NGIT_FLOOR} (`cargo install ngit --version {NGIT_FLOOR}`, or "
    "`ngit update` when it is already installed) and put the directory holding it on PATH")


class NgitError(RuntimeError):
    """A sync or an answer cannot proceed; the message says what the operator must fix."""


@dataclass(frozen=True)
class NgitStatus:
    """What `ngit` is on this host: where it is, what version, or why it is unusable."""

    binary: str
    path: str = ""
    version: str = ""
    problem: str = ""

    @property
    def usable(self) -> bool:
        return not self.problem

    def as_dict(self) -> dict[str, Any]:
        return {"binary": self.binary, "path": self.path, "version": self.version,
                "floor": NGIT_FLOOR, "usable": self.usable, "problem": self.problem}


def ngit_binary() -> str:
    """The command the bridge runs: `ngit`, or `$HNG_NGIT` for a build outside PATH."""
    return os.environ.get("HNG_NGIT") or "ngit"


@dataclass(frozen=True)
class NgitProbe:
    """One look at the host: where `ngit` is, and what it said when asked.

    An empty `path` means there is no such command; `error` means it exists but could not
    be run at all; otherwise `stdout`, `stderr` and `code` are what `--version` produced.
    Nothing here judges the answer -- that is `ngit_status`'s job -- so this is the one
    structure a test has to invent to describe a host without an `ngit` on it.
    """

    path: str = ""
    stdout: str = ""
    stderr: str = ""
    code: int = 0
    error: str = ""


def probe_ngit(binary: str) -> NgitProbe:
    """Find `binary` and ask its version: the single host lookup, so it can be stubbed.

    The suite runs on machines with no `ngit` at all (CI is one), so a test that is about
    the message, the exit code or the ordering stubs this and leaves the host out of it.
    """
    found = shutil.which(binary)
    if not found:
        return NgitProbe()
    try:
        done = subprocess.run([found, "--version"], capture_output=True, text=True,
                              timeout=15.0)
    except (OSError, subprocess.SubprocessError) as exc:
        return NgitProbe(path=found, error=str(exc))
    return NgitProbe(path=found, stdout=done.stdout or "", stderr=done.stderr or "",
                     code=done.returncode)


def parse_version(text: str) -> tuple[int, ...]:
    """A version out of `ngit --version` output, dotted numeric components only.

    The line is `ngit 3.0.4`; update notices on stderr are ignored because only stdout is
    read. Returns an empty tuple when nothing parseable is there.
    """
    for token in (text or "").split():
        candidate = token.strip().lstrip("v")
        parts = candidate.split(".")
        if parts and all(part.isdigit() for part in parts if part) and len(parts) >= 2:
            return tuple(int(part) for part in parts if part)
    return ()


def ngit_status() -> NgitStatus:
    """Resolve the binary and its version once, for the preflight and for `doctor`.

    Never raises: a missing or too-old binary is an answer, not an exception, so both
    callers can report it in their own shape. The problem text names the binary, the floor
    and how to install it, because "ngit is not installed" is not actionable.

    The host is read exactly once, by `probe_ngit`, and everything here is a judgement on
    what came back. That seam is what lets the suite describe a host with no `ngit` at all.
    """
    binary = ngit_binary()
    probe = probe_ngit(binary)
    if not probe.path:
        return NgitStatus(binary=binary, problem=f"`{binary}` was not found on PATH; "
                                                 f"the bridge needs ngit >= {NGIT_FLOOR}: "
                                                 f"{NGIT_INSTALL_HINT}")
    found = probe.path
    if probe.error:
        return NgitStatus(binary=binary, path=found,
                          problem=f"`{found} --version` could not be run ({probe.error}); "
                                  f"the bridge needs ngit >= {NGIT_FLOOR}: "
                                  f"{NGIT_INSTALL_HINT}")
    version = parse_version(probe.stdout)
    if probe.code != 0 or not version:
        detail = ((probe.stderr or probe.stdout or "").strip().splitlines() or [""])[0][:200]
        return NgitStatus(binary=binary, path=found,
                          problem=f"`{binary} --version` did not name a version "
                                  f"({detail or f'exit {probe.code}'}); the bridge "
                                  f"needs ngit >= {NGIT_FLOOR}: {NGIT_INSTALL_HINT}")
    text = ".".join(str(part) for part in version)
    if version < parse_version(NGIT_FLOOR):
        return NgitStatus(binary=binary, path=found, version=text,
                          problem=f"found ngit {text} at {found}, but the bridge needs "
                                  f">= {NGIT_FLOOR}: {NGIT_INSTALL_HINT}")
    return NgitStatus(binary=binary, path=found, version=text)


def require_ngit() -> NgitStatus:
    """The preflight for anything that shells out to `ngit`: raise with the fix, or pass."""
    status = ngit_status()
    if not status.usable:
        raise NgitError(status.problem)
    return status



class NotifyError(RuntimeError):
    """The daemon was asked and refused, or could not be reached at all: a message that will
    not land until something is fixed."""


class NotifyBusy(NotifyError):
    """The daemon did not answer in time, which is what a running turn looks like from here.

    It is not a failure: the notice stays queued and the next poll tries again. Telling the
    two apart is what lets a busy gateway be a quiet delay instead of an error every ten
    minutes in the cron log.
    """


# -- what the bridge reads ---------------------------------------------------

@dataclass(frozen=True)
class Comment:
    """One kind-1622 reply on an issue, as `ngit issue view --comments --json` reports it."""

    id: str            # nevent, or the hex id when it could not be encoded
    hex: str
    author: str        # npub
    body: str
    created_at: int = 0
    reply_to: str = ""


@dataclass(frozen=True)
class Issue:
    """A kind-1621 issue. The list form carries no body; `detail()` fills it in."""

    id: str            # nevent, as ngit prints it
    hex: str           # event id, the link key
    author: str        # npub
    subject: str
    body: str = ""
    comments: tuple[Comment, ...] = ()
    comment_count: int = 0
    status: str = "open"


@dataclass(frozen=True)
class PullRequest:
    """A kind-1618 event, reduced to what the channel message and the review task need."""

    id: str            # event id, hex
    author: str        # npub
    subject: str
    branch: str = ""
    created_at: int = 0
    nevent: str = ""
    #: The tip commit the `c` tag names, when the event carries one.
    commit: str = ""


@dataclass(frozen=True)
class Merge:
    """A kind-1631 applied/resolved status for a PR, with whatever commit it names."""

    pr_id: str         # hex of the root PR event
    commit: str = ""
    created_at: int = 0


@dataclass(frozen=True)
class Target:
    """One channel's project, resolved once and used in both directions."""

    conversation: str
    repo: Path
    board: str
    project: str = ""
    #: The repo's NIP-34 address (`30617:<hex>:<identifier>`), or "" for a plain checkout.
    address: str = ""
    #: The branch a PR targets: the checkout's own. A kind-1618 event names its source
    #: branch and no target, so this is what a review task says the diff is against.
    default_branch: str = ""

    @property
    def key(self) -> str:
        """What state rows are keyed on: the Nostr repo when there is one, else the path."""
        return self.address or str(self.repo)


@dataclass(frozen=True)
class BridgeConfig:
    """The `ngit:` block. Defaults are the safe ones: nothing is trusted, nothing is sent."""

    assignee: str = ""
    reviews: bool = True
    review_assignee: str = "default"
    relays: tuple[str, ...] = ()
    lookback_hours: int = 168
    issues: bool = True
    prs: bool = True
    priority: int = 0
    #: How many new tasks one poll may create. A backlog (or a spam run of issues from an
    #: open relay) is spread over polls instead of filling the board and the channel in one
    #: go; nothing is dropped, and the report says how much is still waiting.
    max_new: int = 20
    #: How many channel notices one poll may decide on -- and, separately, how many queued
    #: ones it may hand to the daemon. The first run (or a catch-up after downtime) must not
    #: dump a month of merged PRs into a channel; the rest follow on later polls.
    max_prs: int = 10
    #: Whether the daemon listens for the mapped repositories' events and wakes a pass when
    #: one arrives. `None` is "the default": on when the bridge is configured (an assignee
    #: is set, so a pass can actually run), off otherwise. Cron stays the floor either way.
    watch: bool | None = None
    #: How long the watcher coalesces a channel's events before running its pass.
    watch_debounce_s: float = DEFAULT_WATCH_DEBOUNCE_S

    @classmethod
    def from_config(cls, raw: Any) -> "BridgeConfig":
        block = raw if isinstance(raw, dict) else {}
        if raw not in (None, {}) and not isinstance(raw, dict):
            log.warning("ngit: expected a mapping, got %r", type(raw).__name__)
        relays = block.get("relays")
        if isinstance(relays, str):
            relays = [relays]
        if not isinstance(relays, (list, tuple)):
            relays = []
        cleaned = tuple(str(url).strip() for url in relays if str(url or "").strip())
        # Tri-state on purpose: absent means "the default" (on once the bridge is
        # configured), and only an explicit false turns the watcher off.
        watch = block.get("watch")
        try:
            debounce = float(block.get("watch_debounce_s", DEFAULT_WATCH_DEBOUNCE_S)
                             or DEFAULT_WATCH_DEBOUNCE_S)
        except (TypeError, ValueError):
            log.warning("ngit: watch_debounce_s is not a number; using %gs",
                        DEFAULT_WATCH_DEBOUNCE_S)
            debounce = DEFAULT_WATCH_DEBOUNCE_S
        if debounce <= 0:
            log.warning("ngit: watch_debounce_s must be positive; using %gs",
                        DEFAULT_WATCH_DEBOUNCE_S)
            debounce = DEFAULT_WATCH_DEBOUNCE_S
        return cls(
            assignee=str(block.get("assignee") or "").strip(),
            reviews=bool(block.get("reviews", True)),
            review_assignee=str(block.get("review_assignee") or "default").strip(),
            relays=cleaned,
            lookback_hours=int(block.get("lookback_hours", 168) or 0),
            issues=bool(block.get("issues", True)),
            prs=bool(block.get("prs", True)),
            priority=int(block.get("priority", 0) or 0),
            max_new=max(1, int(block.get("max_new", 20) or 1)),
            max_prs=max(1, int(block.get("max_prs", 10) or 1)),
            watch=None if watch is None else bool(watch),
            watch_debounce_s=debounce,
        )


@dataclass
class Report:
    """What one sync did, in a shape a cron can read (`--json`) and a human can scan."""

    channel: str
    repo: str
    board: str
    assignee: str
    address: str = ""
    dry_run: bool = False
    issues: dict[str, int] = field(default_factory=lambda: {
        "seen": 0, "created": 0, "ready": 0, "held": 0, "existing": 0,
        "comments": 0, "deleted": 0, "deferred": 0, "would_create": 0,
        "would_post": 0})
    prs: dict[str, int] = field(default_factory=lambda: {
        "seen": 0, "signalled": 0, "merged": 0, "existing": 0, "deferred": 0,
        "pending": 0, "would_post": 0, "tasks": 0, "review_errors": 0})
    posted: int = 0
    #: Channel messages that did not reach the daemon. Counted, not repeated: a relay-free
    #: poll can fail twenty times for the same reason, and a cron log needs one line.
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def delivery_failed(self, reason: str) -> None:
        self.failed += 1
        note = f"message not delivered: {reason}"
        if note not in self.errors:
            self.errors.append(note)

    def delivery_pending(self, reason: str) -> None:
        """A notice the daemon has not taken yet: queued, not lost, and not an error."""
        note = f"queued for the next pass ({reason})"
        if note not in self.notes:
            self.notes.append(note)

    def as_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "repo": self.repo, "board": self.board,
                "assignee": self.assignee, "repo_address": self.address,
                "dry_run": self.dry_run, "issues": dict(self.issues), "prs": dict(self.prs),
                "posted": self.posted, "failed": self.failed,
                "errors": list(self.errors), "notes": list(self.notes)}

    def lines(self) -> list[str]:
        out = [f"channel: {self.channel}", f"repo:    {self.repo}"
               + (f" ({self.address})" if self.address else " (not a nostr repo)"),
               f"board:   {self.board}  (assignee {self.assignee})"]
        i, p = self.issues, self.prs
        out.append(f"issues:  {i['seen']} seen, {i['created']} created ({i['ready']} ready,"
                   f" {i['held']} held), {i['existing']} known, {i['comments']} comment(s)"
                   + (f", {i['deleted']} deleted by hand" if i["deleted"] else "")
                   + (f", {i['deferred']} waiting for the next poll" if i["deferred"] else "")
                   + (f", {i['would_create']} would be created" if self.dry_run else ""))
        out.append(f"prs:     {p['seen']} seen, {p['signalled']} signalled,"
                   f" {p['merged']} merged, {p['existing']} known"
                   + (f", {p['pending']} pending (queued for the next pass)"
                      if p["pending"] else "")
                   + (f", {p['tasks']} review task(s)" if p["tasks"] else "")
                   + (f", {p['review_errors']} review failure(s)" if p["review_errors"] else "")
                   + (f", {p['deferred']} waiting for the next poll" if p["deferred"] else "")
                   + (f", {p['would_post']} would be posted" if self.dry_run else ""))
        out.append(f"posted:  {self.posted} channel message(s)"
                   + (f", {self.failed} not delivered" if self.failed else "")
                   + (" (dry run: nothing written)" if self.dry_run else ""))
        for error in self.errors:
            out.append(f"error:   {error}")
        for note in self.notes:
            out.append(f"note:    {note}")
        return out


# -- parsing ----------------------------------------------------------------

_HEX = set("0123456789abcdef")


def event_hex(value: str) -> str:
    """An event id as hex, from hex or an nevent/note. Empty when it is neither."""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) == 64 and set(text.lower()) <= _HEX:
        return text.lower()
    try:
        return sdk.Nip19Event.from_bech32(text).event_id().to_hex()
    except Exception:  # noqa: BLE001 - not a NIP-19 event reference
        return ""


def npub_of(pubkey: str) -> str:
    """A key as npub, which is how a person recognizes it. Falls back to what it was given."""
    try:
        return sdk.PublicKey.parse(str(pubkey or "")).to_bech32()
    except Exception:  # noqa: BLE001
        return str(pubkey or "")


def nevent_of(event: dict[str, Any]) -> str:
    """An nevent for a raw event, so a reader can open it in a client."""
    try:
        return sdk.Nip19Event(
            sdk.EventId.parse(str(event["id"])),
            author=sdk.PublicKey.parse(str(event["pubkey"])),
            kind=sdk.Kind(int(event["kind"])),
        ).to_bech32()
    except Exception:  # noqa: BLE001 - a message with a hex id is still useful
        return str(event.get("id") or "")


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _tag(event: dict[str, Any], name: str) -> str:
    for tag in _as_list(event.get("tags")):
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == name:
            return str(tag[1])
    return ""


def _tags(event: dict[str, Any], name: str) -> list[str]:
    return [str(tag[1]) for tag in _as_list(event.get("tags"))
            if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == name]


def _root_id(event: dict[str, Any]) -> str:
    """The event a status or a revision refers to: the `root` marker wins, then the first `e`."""
    fallback = ""
    for tag in _as_list(event.get("tags")):
        if not isinstance(tag, (list, tuple)) or len(tag) < 2 or tag[0] != "e":
            continue
        marker = str(tag[3]) if len(tag) > 3 else ""
        if marker == "root":
            return str(tag[1])
        fallback = fallback or str(tag[1])
    return fallback


def issue_from_json(item: dict[str, Any]) -> Issue:
    """One row of `ngit issue list --json`."""
    identifier = str(item.get("id") or "").strip()
    return Issue(
        id=identifier,
        hex=event_hex(identifier),
        author=npub_of(str(item.get("author") or "")),
        subject=str(item.get("subject") or "").strip() or "(no subject)",
        comment_count=int(item.get("comment_count") or 0),
        status=str(item.get("status") or "open"),
    )


def issue_detail_from_json(data: dict[str, Any], ref: Issue) -> Issue:
    """`ngit issue view <id> --json --comments`, merged onto the list row we already have."""
    comments: list[Comment] = []
    for item in _as_list(data.get("comments")):
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "").strip()
        comments.append(Comment(
            id=identifier,
            hex=event_hex(identifier),
            author=npub_of(str(item.get("author") or "")),
            body=str(item.get("body") or ""),
            created_at=int(item.get("created_at") or 0),
            reply_to=str(item.get("reply_to") or ""),
        ))
    return replace(
        ref,
        subject=str(data.get("subject") or ref.subject).strip() or ref.subject,
        body=str(data.get("description") or ""),
        comments=tuple(comments),
        comment_count=int(data.get("comment_count") or len(comments)),
        status=str(data.get("status") or ref.status),
    )


def pull_request_from_event(event: dict[str, Any]) -> PullRequest:
    """A kind-1618 event. The subject tag is the title; the content is the description."""
    content = str(event.get("content") or "").strip()
    subject = _tag(event, "subject").strip()
    if not subject:
        subject = content.splitlines()[0].strip() if content else "(no subject)"
    return PullRequest(
        id=str(event.get("id") or ""),
        author=npub_of(str(event.get("pubkey") or "")),
        subject=subject[:200],
        branch=_tag(event, "branch-name"),
        created_at=int(event.get("created_at") or 0),
        nevent=nevent_of(event),
        commit=_tag(event, "c"),
    )


def merge_from_event(event: dict[str, Any]) -> Optional[Merge]:
    """A kind-1631 that refers to a PR. Resolved issues come through here too; the caller
    only asks about PR ids it already knows, so an issue's resolution cannot post a merge.

    The commit comes from whichever spelling the publisher used: NIP-34's `merge-commit` or
    ngit's `merge-commit-id`. Only when neither names one is `applied-as-commits` read, which
    is what a proposal applied as patches carries -- there is no merge commit to name then.
    """
    pr_id = _root_id(event)
    if not event_hex(pr_id):
        return None
    commit = ""
    for name in MERGE_COMMIT_TAGS:
        commit = _tag(event, name)
        if commit:
            break
    if not commit:
        applied = _tags(event, APPLIED_COMMIT_TAG)
        commit = applied[0] if applied else ""
    return Merge(pr_id=event_hex(pr_id), commit=commit,
                 created_at=int(event.get("created_at") or 0))


# -- reading a checkout through ngit -----------------------------------------

def run_ngit(repo: Path, args: Sequence[str], *, timeout: float = 120.0) -> Any:
    """One `ngit ... --json` call from inside the checkout.

    Only stdout is parsed: ngit keeps relay progress and notices on stderr, and `--json`
    promises exactly one document. A non-zero exit carries the stderr line, never a
    traceback, because this runs from cron and its output is a log line.
    """
    command = [ngit_binary(), *args, "--json"]
    try:
        done = subprocess.run(command, cwd=str(repo), capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise NgitError(ngit_status().problem
                        or f"`{ngit_binary()}` could not be run") from exc
    except subprocess.TimeoutExpired as exc:
        raise NgitError(f"ngit {args[0] if args else ''} timed out after {timeout:.0f}s"
                        ) from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise NgitError(f"ngit {' '.join(args)} failed ({done.returncode}): "
                        f"{(detail[-1] if detail else 'no output')[:300]}")
    text = (done.stdout or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise NgitError(f"ngit {' '.join(args)} did not print one JSON document: "
                        f"{text[:200]}") from exc


@dataclass(frozen=True)
class RepoRef:
    """What a checkout says about its Nostr repository: its address and where to read it."""

    address: str = ""
    #: The announcement's relay hints: where that repository's issues and PRs are published.
    relays: tuple[str, ...] = ()


class Ngit:
    """The `ngit` CLI, as this bridge uses it: a repo's address and its issues."""

    def __init__(self, repo: Path, *, runner: Callable[..., Any] = run_ngit):
        self.repo = Path(repo)
        self._run = runner

    def repository(self) -> RepoRef:
        """The repository announcement, from `ngit repo --json`.

        Empty for a checkout that is not announced on Nostr: the bridge then keys its state
        on the path and imports no PRs, rather than guessing an address. A failing call is
        not fatal here either: the issue import reports it, and aborting before that would
        turn a missing nostr remote into a sync that does nothing at all.
        """
        try:
            data = self._run(self.repo, ["repo"])
        except NgitError as exc:
            log.warning("ngit: cannot read %s's repository: %s", self.repo, exc)
            return RepoRef()
        if not isinstance(data, dict) or not data.get("is_nostr_repo"):
            return RepoRef()
        coordinate = str(data.get("coordinate") or "").strip()
        if coordinate:
            try:
                parsed = sdk.Nip19Coordinate.from_bech32(coordinate)
                found = parsed.coordinate()
                address = (f"{found.kind()}:{found.public_key().to_hex()}:"
                           f"{found.identifier()}")
                # The naddr carries the announcement's relay hints, which is exactly the
                # read set for its issues and PRs (NIP-34).
                return RepoRef(address=address,
                               relays=tuple(str(url) for url in parsed.relays()))
            except Exception:  # noqa: BLE001 - fall through to the explicit fields
                log.debug("ngit: could not decode repo coordinate %s", coordinate[:24])
        owner = as_hex(str(data.get("selected_maintainer") or ""))
        identifier = str(data.get("identifier") or "").strip()
        address = f"30617:{owner}:{identifier}" if owner and identifier else ""
        return RepoRef(address=address)

    def issues(self) -> list[Issue]:
        data = self._run(self.repo, ["issue", "list"])
        out: list[Issue] = []
        for item in _as_list(data):
            if isinstance(item, dict) and item.get("id"):
                out.append(issue_from_json(item))
        return out

    def detail(self, ref: Issue) -> Issue:
        data = self._run(self.repo, ["issue", "view", ref.id, "--comments"])
        if not isinstance(data, dict):
            raise NgitError(f"ngit issue view {ref.id} returned no issue")
        return issue_detail_from_json(data, ref)


class RelayPrs:
    """Kind-1618 and kind-1631 events for one repository address, read from relays."""

    def __init__(self, relays: Sequence[str], address: str, *,
                 fetcher: Callable[..., list[dict[str, Any]]] = fetch_many_blocking,
                 secret: str | None = None):
        self.relays = tuple(str(url) for url in relays if url)
        self.address = address
        self._fetch = fetcher
        self.secret = secret

    def events(self, since: int) -> tuple[list[PullRequest], list[Merge]]:
        """Every PR and applied-status event newer than `since`, deduped by event id."""
        if not self.relays:
            raise NgitError("ngit.relays (or relays.read) is empty: nowhere to read PR events")
        if not self.address:
            raise NgitError("this checkout has no NIP-34 address: no PRs to read")
        raw = self._fetch(relays=self.relays, kinds=[KIND_PR, KIND_APPLIED],
                          a_tag=self.address, since=int(since), limit=500,
                          secret=self.secret)
        prs: dict[str, PullRequest] = {}
        merges: dict[str, Merge] = {}
        for event in raw or []:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            kind = int(event.get("kind") or 0)
            if kind == KIND_PR:
                pr = pull_request_from_event(event)
                # A revision (1619) or a re-publish can repeat a subject; the id is the PR.
                prs[pr.id] = pr
            elif kind == KIND_APPLIED:
                merge = merge_from_event(event)
                if merge is not None:
                    merges[merge.pr_id] = merge
        return list(prs.values()), list(merges.values())


# -- posting into the channel -------------------------------------------------

class DaemonNotifier:
    """Post a channel message through the running daemon: the reply path, no new transport.

    The daemon already holds the community and the channel keys; a one-shot CLI would have
    to derive both again. `send` is the same op a reply takes, so a bridge message is
    published as us, verified, and recorded in the store like any other.

    Two failures are told apart, because they mean different things to the caller. A daemon
    that does not answer in time is busy -- a turn holds it -- and the notice can wait. A
    daemon that is not there, or that says no, will say no again, and the operator has to
    hear about it.
    """

    def __init__(self, socket_path: str | Path, *, timeout: float = NOTIFY_TIMEOUT_S):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def __call__(self, conversation: str, text: str, key: str = "") -> None:
        from .server import request

        frame: dict[str, Any] = {"op": "send", "to": conversation, "text": text}
        if key:
            # The correlation id is the idempotency key: the daemon answers a repeated one
            # with the message it already published, so a retry after a lost acknowledgement
            # cannot post the notice twice.
            frame["id"] = key

        def call() -> dict[str, Any]:
            return asyncio.run(request(self.socket_path, frame, timeout=self.timeout))

        try:
            reply = _in_thread_if_running(call)
        except TimeoutError as exc:
            raise NotifyBusy(
                f"the daemon did not answer within {self.timeout:g}s") from exc
        except ConnectionError as exc:
            # The daemon took the request and closed without answering: it restarted, or the
            # handler died. The message may or may not have gone out -- the key settles
            # that on the retry.
            raise NotifyBusy(f"the daemon closed the connection ({exc})") from exc
        except (OSError, ValueError) as exc:
            raise NotifyError(f"the daemon did not answer ({exc})") from exc
        if reply.get("type") == "error" or not reply.get("ok"):
            raise NotifyError(str(reply.get("error") or "the send was refused")[:200])


def _in_thread_if_running(call: Callable[[], Any]) -> Any:
    """Run a blocking asyncio call even when the caller already holds a loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return call()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(call).result()


# -- the messages -------------------------------------------------------------

def notice_message(issue: Issue, task_id: str) -> str:
    """The one line a trusted author's issue gets. A notice, not a question."""
    return f"issue {issue.id} -> {task_id}"


def question_message(issue: Issue, task_id: str, target: Target, assignee: str) -> str:
    """The one question an unrecognised author's issue gets, and what `yes` would do."""
    return "\n".join([
        f'Unrecognised author for "{issue.subject}"',
        f"author: {issue.author}",
        f"issue: {issue.id}",
        f"task: {task_id} (held, not queued)",
        "",
        f"Reply yes to queue it on board {target.board} for {assignee}, or no to leave it out.",
    ])


def ready_message(pr: PullRequest, *, review_task: str = "", problem: str = "") -> str:
    """The one message a new PR gets: what it is, where to read it, who reviews it.

    The review task is named here rather than in a second message: a PR is one event in the
    channel, and a reader who wants to know who is looking at it should not have to find a
    second post. When no task could be made the message says so, and why.
    """
    where = f" on {pr.branch}" if pr.branch else ""
    lines = [f'PR "{pr.subject}" by {pr.author}{where} - ready for review', pr.nevent]
    if review_task:
        lines.append(f"review task {review_task}")
    elif problem:
        lines.append(f"no review task: {problem}")
    return "\n".join(lines)


def review_title(pr: PullRequest) -> str:
    """The title: short enough to scan in a column, exact enough to know which PR it is."""
    return f"Review PR {pr.id[:8]}: {pr.subject}"


def review_body(pr: PullRequest, target: Target) -> str:
    """What a reviewer needs to judge this PR, and the one thing they must not do.

    The brief is the point of the task: "should this merge" answered with reasons, on the
    record, instead of in the channel's scrollback. Merging is left out on purpose -- it is
    the operator's decision, and a reviewed PR is evidence for it, not the decision itself.
    """
    source = pr.branch or "(the PR names no branch)"
    into = target.default_branch
    against = into or "(the repository's default branch)"
    steps = [
        f"1. Check the proposal out: `ngit pr checkout {pr.nevent or pr.id}`",
        f"   then read it against {against}:"
        f" `git diff {into or '<target>'}...HEAD` (or `git log -p {into or '<target>'}..HEAD`).",
        "2. Run the repository's own gates from that tree: `ruff check` and `pytest`. Say",
        "   what ran and what it printed, including anything that was already failing.",
        "3. Report a verdict with reasons on this task -- merge, or changes needed -- and",
        "   say what the gates could not catch: behaviour, tests that do not exercise the",
        "   change, and anything the PR leaves out.",
    ]
    return "\n".join([
        f'PR "{pr.subject}" by {pr.author}',
        "",
        f"PR:         {pr.id}",
        f"nevent:     {pr.nevent}",
        f"Repository: {target.address or target.repo}",
        f"Author:     {pr.author}",
        f"Source:     {source}",
        f"Target:     {against}",
        *([f"Tip:        {pr.commit}"] if pr.commit else []),
        "",
        "---",
        "",
        "Review this PR and report a verdict. Do not merge it: merging is the operator's,",
        "or a follow-up task. This task ends with a verdict, not a merge.",
        "",
        *steps,
    ])


def merged_message(pr: PullRequest | None, merge: Merge, nevent: str = "") -> str:
    how = f"merged as {merge.commit}" if merge.commit else "merged"
    if pr is None:
        return f"PR {nevent or merge.pr_id} {how}"
    return f'PR "{pr.subject}" by {pr.author} {how}\n{pr.nevent or nevent}'


# -- resolving a channel to its repo and board --------------------------------

def _split_conversation(value: str) -> tuple[str, str]:
    """`concord:<community>:<channel>` -> (community, channel); anything else -> ("", value)."""
    text = str(value or "").strip()
    parts = text.split(":")
    if len(parts) == 3 and parts[0].lower() == "concord":
        return parts[1].strip().lower(), parts[2].strip().lower()
    return "", text


def looks_hex(value: str) -> bool:
    """A 64-character hex id, which is how a Concord channel or community is spelled."""
    return len(value) == 64 and set(value.lower()) <= _HEX


def resolve_target(cfg: dict[str, Any], *, channel: str, repo: str | None = None,
                   board: str | None = None) -> Target:
    """The repo and board a channel's work belongs to, from the one configured mapping.

    `--repo` and `--board` override what the mapping says, never replace it: a channel with
    no `concord.projects` entry is a configuration error, because a bridge that guessed a
    checkout would file work in the wrong tree.
    """
    conversation = str(channel or "").strip()
    if not conversation:
        raise NgitError("--channel is required: the Concord conversation this repo belongs to")
    mapping = ProjectMap.from_config(cfg.get("concord") or {})
    community, channel_ref = _split_conversation(conversation)
    project = mapping.resolve(community_id=community,
                              channel_id=channel_ref if looks_hex(channel_ref) else "",
                              channel_name="" if looks_hex(channel_ref) else channel_ref)
    if project is None and not community and looks_hex(channel_ref):
        # A scheduled pass names a channel by its id alone -- the systemd timer instance
        # is the id -- and was never told a community, so an entry that declares one still
        # has to resolve. A conversation that names a community keeps its scoping.
        project = mapping.resolve_id(channel_ref)
    if project is None:
        # An entry may also be keyed by the whole conversation, which is the one string a
        # crontab pass has: `channel: concord:<community>:<channel>`.
        project = mapping.resolve(community_id=community, channel_name=conversation)
    if project is None:
        known = ", ".join(sorted(p.channel for p in mapping.all())) or "none"
        raise NgitError(f"no concord.projects entry maps {conversation} to a checkout "
                        f"(mapped channels: {known}); see `hng projects`")
    path = Path(repo).expanduser() if repo else project.path
    if not path.is_dir():
        raise NgitError(f"checkout {path} does not exist")
    slug = str(board or project.board or "").strip()
    if not slug:
        raise NgitError(f"channel {project.channel!r} declares no board: set `board:` on its"
                        " concord.projects entry or pass --board")
    address = conversation
    if community and channel_ref:
        address = f"concord:{community}:{channel_ref}"
    elif looks_hex(channel_ref):
        # A timer pass was told the channel id and nothing else, so the entry's own scope is
        # the only place the community can come from. A notice is addressed by the whole
        # conversation (`concord:<community>:<channel>`), so the id alone is not enough:
        # refuse at the preflight rather than fail every notice at delivery.
        if not project.community:
            raise NgitError(
                f"channel {channel_ref} has no community in its concord.projects entry, so a "
                "pass named by the id alone cannot address the channel; add `community:` to "
                f"the entry or pass --channel concord:<community-id>:{channel_ref}")
        address = (f"concord:{project.community.strip().lower()}:"
                   f"{project.channel.strip().lower()}")
    return Target(conversation=address, repo=path, board=slug, project=project.project)


def board_path(slug: str, *, home: str | Path | None = None) -> Path:
    return board_dir(hermes_home(home), slug) / "kanban.db"


def default_branch(repo: Path) -> str:
    """The branch this checkout's PRs target, from git rather than from the event.

    NIP-34's kind 1618 names the *source* branch (`branch-name`) and has no target tag, so
    the target is the repository's default -- what `ngit pr merge` falls back to. `origin/HEAD`
    answers it for a clone; a checkout that has no remote HEAD answers with the branch it has
    out. Empty when neither answers, and the review task then says so instead of guessing.
    """
    for args in (["symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
                 ["symbolic-ref", "--short", "HEAD"]):
        try:
            done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  text=True, timeout=10.0)
        except (OSError, subprocess.SubprocessError):
            return ""
        name = (done.stdout or "").strip()
        if done.returncode == 0 and name:
            return name[len("origin/"):] if name.startswith("origin/") else name
    return ""


# -- the sync -----------------------------------------------------------------

IssueSource = Callable[[], list[Issue]]
DetailSource = Callable[[Issue], Issue]
PrSource = Callable[[int], tuple[list[PullRequest], list[Merge]]]
#: `notify(conversation, text, key)`: post one channel message, and let the daemon answer a
#: repeated `key` with the message it already published instead of posting it twice.
Notifier = Callable[[str, str, str], None]


def notice_state(kind: int, notice: str) -> str:
    """The link state a notice earns once the daemon has taken it."""
    if int(kind) == KIND_PR:
        return "merged" if notice == "merged" else "signalled"
    return "asked" if notice == "question" else "ready"


class Sync:
    """One pass over a channel's repo: issues in, PR news out.

    Every collaborator is injected, so the tests drive the whole state machine without a
    relay, a daemon or an `ngit` child process, and the real ones are thin. The one thing
    that is not injected is the queue: a notice is written down before it is sent, and the
    record only moves when the daemon says it took it.
    """

    def __init__(self, *, store: Store, board: Board, control: ControlConfig,
                 config: BridgeConfig, target: Target, issues: IssueSource,
                 detail: DetailSource, prs: PrSource | None = None,
                 notify: Notifier | None = None, anchor: Optional[Anchor] = None,
                 now: Callable[[], float] = time.time):
        self.store = store
        self.board = board
        self.control = control
        self.config = config
        self.target = target
        self.issues = issues
        self.detail = detail
        self.prs = prs
        self.notify = notify
        self.anchor = anchor or Anchor()
        self._now = now

    # -- entry point -----------------------------------------------------

    def run(self, *, since: int | None = None, recreate: bool = False,
            dry_run: bool = False) -> Report:
        report = Report(channel=self.target.conversation, repo=str(self.target.repo),
                        board=self.target.board, assignee=self.config.assignee,
                        address=self.target.address, dry_run=dry_run)
        if self.config.issues:
            try:
                self._issues(report, recreate=recreate, dry_run=dry_run)
            except NgitError as exc:
                report.errors.append(f"issues: {exc}")
        if self.config.prs and self.prs is not None:
            try:
                self._read_prs(report, since=since, dry_run=dry_run)
            except NgitError as exc:
                report.errors.append(f"prs: {exc}")
        # Delivery is its own step, after both directions have decided what to say: what is
        # in the queue is what gets sent, whether it was decided this pass or three passes
        # ago, and the relay being down cannot hold a queued notice back.
        self._deliver(report, dry_run=dry_run)
        return report

    # -- issues ----------------------------------------------------------

    def _issues(self, report: Report, *, recreate: bool, dry_run: bool) -> None:
        budget = max(1, int(self.config.max_new or 1))
        for ref in self.issues():
            report.issues["seen"] += 1
            if not ref.hex:
                report.errors.append(f"issue {ref.id}: not an event id, skipped")
                continue
            try:
                outcome = self._one_issue(ref, report, recreate=recreate, dry_run=dry_run,
                                          allow_create=budget > 0)
            except (NgitError, BoardError) as exc:
                # One issue the bridge cannot read or write must not cost the rest of the
                # poll: the error is reported and the next issue is attempted.
                report.errors.append(f"issue {ref.id}: {exc}")
                continue
            if outcome in ("created", "would_create"):
                budget -= 1

    def _one_issue(self, ref: Issue, report: Report, *, recreate: bool,
                   dry_run: bool, allow_create: bool = True) -> str:
        key = self.target.key
        link = self.store.ngit_link(key, KIND_ISSUE, ref.hex)
        task = self.board.task(link["task_id"]) if link and link.get("task_id") else None
        recreated = False

        if link is not None and task is None:
            if not recreate:
                if link["state"] != "deleted":
                    self.store.ngit_link_state(key, KIND_ISSUE, ref.hex, "deleted")
                report.issues["deleted"] += 1
                return "deleted"
            # --recreate: the operator asked for the task back, so fall through as new
            link, task = None, None
            recreated = True

        if link is not None and task is not None:
            report.issues["existing"] += 1
            if link["state"] == "held" and not dry_run:
                # The question never reached the channel (no daemon, a refused send).
                # Ask now, before comments, so the channel order reads sensibly.
                self._ask(ref, task["id"], report, dry_run=dry_run)
            self._comments(ref, task["id"], report, dry_run=dry_run)
            return "existing"

        issue = self.detail(ref)
        trusted = self.control.is_trusted(issue.author, TRUSTED_PLANE)
        status = "ready" if trusted else "blocked"
        if not allow_create:
            # A backlog is spread over polls rather than filling the board and the channel
            # in one pass. Nothing is forgotten: the next sync sees it again.
            report.issues["deferred"] += 1
            return "deferred"
        if dry_run:
            report.issues["would_create"] += 1
            return "would_create"
        if recreated:
            # The task is new, so the comments have to be copied again: the old rows point
            # at a task that no longer exists. Its notice too -- the new message names the
            # new task id, and the old one must not be taken for a duplicate of it.
            self.store.ngit_forget_comments(key, issue.hex)
            self.store.ngit_notice_forget(key, issue.hex)
        task_id = self.board.create(
            title=issue.subject, body=task_body(issue, self.target),
            assignee=self.config.assignee, status=status,
            idempotency_key=f"ngit:issue:{issue.hex}", anchor=self.anchor,
            priority=self.config.priority)
        self.store.ngit_link_save(
            key, KIND_ISSUE, issue.hex, task_id=task_id,
            board=self.target.board, state="ready" if trusted else "held",
            detail={"nevent": issue.id, "subject": issue.subject,
                    "author": issue.author})
        report.issues["created"] += 1
        report.issues["ready" if trusted else "held"] += 1
        self._comments(issue, task_id, report, dry_run=dry_run)
        if trusted:
            if dry_run:
                report.issues["would_post"] += 1
            else:
                self._enqueue(KIND_ISSUE, issue.hex, "issue",
                              notice_message(issue, task_id))
        else:
            self._ask(issue, task_id, report, dry_run=dry_run)
        return "created"

    def _ask(self, issue: Issue, task_id: str, report: Report, *, dry_run: bool) -> bool:
        """Queue the question and leave the link `held` until the daemon takes it.

        `held` is the honest state while the question is unsent: the task is blocked either
        way, and the next pass asks again. The link only becomes `asked` in `_deliver`, so a
        question the channel never saw is never recorded as asked. The notice carries an
        idempotency key, so asking twice cannot put two questions in the channel.
        """
        if dry_run:
            report.issues["would_post"] += 1
            return False
        self._enqueue(KIND_ISSUE, issue.hex, "question",
                      question_message(issue, task_id, self.target, self.config.assignee))
        self.store.ngit_link_state(self.target.key, KIND_ISSUE, issue.hex, "held")
        return True

    def _comments(self, ref: Issue, task_id: str, report: Report, *, dry_run: bool) -> None:
        key = self.target.key
        if not ref.comments and ref.comment_count <= self.store.ngit_comment_count(
                key, ref.hex):
            return
        issue = ref if (ref.comments or ref.body) else self.detail(ref)
        for comment in sorted(issue.comments, key=lambda c: (c.created_at, c.hex or c.id)):
            if not comment.hex or self.store.ngit_comment_seen(key, ref.hex, comment.hex):
                continue
            if dry_run:
                report.issues["comments"] += 1
                continue
            try:
                self.board.comment(task_id, comment.author, comment.body)
            except BoardError as exc:
                report.errors.append(f"comment {comment.id} on {task_id}: {exc}")
                continue
            self.store.ngit_note_comment(key, ref.hex, comment.hex, task_id)
            report.issues["comments"] += 1

    # -- pull requests ---------------------------------------------------

    def _read_prs(self, report: Report, *, since: int | None, dry_run: bool) -> None:
        """Decide what the channel should hear about, and queue it.

        Deciding and sending are separate on purpose: a poll's job is to get the news into
        the queue, and the queue survives the poll. That is what lets the cursor move past
        events whose notice has not gone out yet -- the notice is not lost, it is waiting.
        """
        if self.prs is None:
            raise NgitError("no PR source configured")
        start = self._since(since)
        prs, merges = self.prs(start)
        applied = {merge.pr_id: merge for merge in merges}
        budget = max(1, int(self.config.max_prs or 1))
        if not dry_run:
            self._requeue_stranded(report)
        #: Events this poll dealt with (reported, queued, or deliberately passed over), and
        #: ones it only saw. The cursor may move past the first set and never the second.
        settled: list[int] = []
        deferred: list[int] = []

        for pr in sorted(prs, key=lambda p: (p.created_at, p.id)):
            report.prs["seen"] += 1
            link = self.store.ngit_link(self.target.key, KIND_PR, pr.id)
            if link is not None and link["state"] != "pending":
                report.prs["existing"] += 1
                # A link from before review tasks existed still gets one, without a
                # second notice: the message it already has is the record.
                self._late_review_task(pr, link, report, dry_run=dry_run)
                settled.append(pr.created_at)
                continue
            if self.store.ngit_notice_queued(self.target.key, pr.id):
                # Decided on an earlier pass and still waiting for the daemon. The queue is
                # the record now, so deciding again would only risk a second notice.
                settled.append(pr.created_at)
                continue
            if budget <= 0:
                # Nothing is forgotten: it is reported as deferred and picked up next poll.
                report.prs["deferred"] += 1
                deferred.append(pr.created_at)
                continue
            merge = applied.get(pr.id)
            budget -= 1
            settled.append(pr.created_at)
            if dry_run:
                report.prs["would_post"] += 1
                continue
            # The task first, the message second: the message names it. A board that cannot
            # be written must not cost the channel its notice, so the failure comes back as
            # a line in the notice and is retried -- without a second message -- later.
            task_id, problem = ("", "")
            if merge is None and self.config.reviews:
                task_id, problem = self._review_task(pr, report, dry_run=dry_run)
            text = (merged_message(pr, merge) if merge is not None
                    else ready_message(pr, review_task=task_id, problem=problem))
            # `pending` is the lifecycle of a notice that has not been confirmed yet; the row
            # is its claim, and only a daemon receipt moves the link past it. The review
            # outcome is written here, with the claim: saving the link again afterwards would
            # drop `task_error` and lose the retry that makes the task.
            self.store.ngit_link_save(
                self.target.key, KIND_PR, pr.id, board=self.target.board, state="pending",
                task_id=task_id or None, detail=pr_detail(pr, task_error=problem))
            self._enqueue(KIND_PR, pr.id, "merged" if merge is not None else "ready", text)

        for merge in sorted(merges, key=lambda m: (m.created_at, m.pr_id)):
            link = self.store.ngit_link(self.target.key, KIND_PR, merge.pr_id)
            if link is None:
                # A merge for a PR this bridge never announced is not replayed into the
                # channel; it narrates what it watched, not the repository's history.
                settled.append(merge.created_at)
                continue
            if budget <= 0:
                report.prs["deferred"] += 1
                deferred.append(merge.created_at)
                continue
            known = next((pr for pr in prs if pr.id == merge.pr_id), None)
            note = _detail(link)
            nevent = str(note.get("nevent") or "")
            if known is None and note.get("subject"):
                known = PullRequest(id=merge.pr_id, author=str(note.get("author") or ""),
                                    subject=str(note.get("subject") or ""),
                                    branch=str(note.get("branch") or ""), nevent=nevent,
                                    commit=str(note.get("commit") or ""))
            text = merged_message(known, merge, nevent)
            budget -= 1
            settled.append(merge.created_at)
            if dry_run:
                report.prs["would_post"] += 1
                continue
            self._enqueue(KIND_PR, merge.pr_id, "merged", text)
            # The merge closes the loop: the review task is done, with the merge named on it.
            # A task already completed is a no-op, and this runs even for a link already
            # `merged`, so one that could not be closed on a locked board closes later.
            if known is not None:
                self._close_review(known, merge, link, report, dry_run=dry_run)

        # The cursor promises that everything before it has been dealt with. A queued notice
        # counts: it is durable, and the next poll delivers it. A deferred event does not --
        # it was seen and not decided on -- so the cursor stops short of the earliest one and
        # the next poll reads it again. A review task that could not be made holds the cursor
        # for the same reason: the retry is what makes the task, not a later event.
        if not dry_run and settled and not report.prs["review_errors"] and not report.failed:
            edge = min(max(settled), *deferred) if deferred else max(settled)
            if edge - CURSOR_OVERLAP_S > start:
                self.store.ngit_set_cursor(self.target.key, "prs", edge - CURSOR_OVERLAP_S)

    def _review_task(self, pr: PullRequest, report: Report, *, dry_run: bool) -> tuple[str, str]:
        """One review task for this PR, or the reason there is none.

        Keyed `ngit:pr:<event id>`, so a re-poll, a retry after a failed send and a retry
        after a failed create all land on the same task. Never raises: the caller still has
        a channel message to post, and "no task because ..." is a better message than none.
        """
        if dry_run:
            return "", ""
        key = f"ngit:pr:{pr.id}"
        try:
            existing = self.board.by_idempotency(key)
            if existing is not None:
                return str(existing["id"]), ""
            task_id = self.board.create(
                title=review_title(pr), body=review_body(pr, self.target),
                assignee=self.config.review_assignee or "default", status="ready",
                idempotency_key=key, anchor=self.anchor, priority=self.config.priority)
        except (BoardError, sqlite3.Error) as exc:
            report.prs["review_errors"] += 1
            report.errors.append(f"review task for PR {pr.id[:8]}: {exc}")
            log.warning("ngit: no review task for %s: %s", pr.id[:8], exc)
            return "", str(exc)[:200]
        report.prs["tasks"] += 1
        log.info("ngit: review task %s for PR %s", task_id, pr.id[:8])
        return task_id, ""

    def _note_review(self, pr: PullRequest, link: Optional[dict[str, Any]], task_id: str,
                     problem: str) -> None:
        """Record the task (or why there is none) on the link, so a later poll can retry."""
        detail = pr_detail(pr, task_error=problem)
        self.store.ngit_link_save(
            self.target.key, KIND_PR, pr.id, state=(link["state"] if link else "pending"),
            task_id=task_id or None, board=self.target.board, detail=detail)

    def _late_review_task(self, pr: PullRequest, link: dict[str, Any], report: Report,
                          *, dry_run: bool) -> None:
        """A PR already announced with no task: make the task, post nothing.

        The channel was told there was no review task and why, so the retry is silent --
        the message is not repeated. Only a link that says a creation failed qualifies: a
        PR announced before this bridge made tasks is not retrofitted.
        """
        if link["state"] != "signalled" or link.get("task_id"):
            return
        if not self.config.reviews or not _detail(link).get("task_error"):
            return
        task_id, problem = self._review_task(pr, report, dry_run=dry_run)
        if dry_run:
            return
        self._note_review(pr, link, task_id, problem)
        if task_id:
            log.info("ngit: late review task %s for PR %s", task_id, pr.id[:8])

    def _close_review(self, pr: PullRequest | None, merge: Merge, link: dict[str, Any],
                      report: Report, *, dry_run: bool) -> None:
        """Complete the PR's review task, naming the merge. Idempotent."""
        task_id = str(link.get("task_id") or "")
        if not task_id or dry_run:
            return
        how = f"merged as {merge.commit}" if merge.commit else "merged"
        what = f'"{pr.subject}"' if pr is not None and pr.subject else f"PR {merge.pr_id}"
        nevent = str(_detail(link).get("nevent") or "")
        try:
            self.board.complete(
                task_id, author="ngit",
                comment=f"{what} {how}" + (f" ({nevent})" if nevent else "")
                        + f"\nThe merge was published as status {merge.pr_id}; this review"
                        " is closed by it.",
                result=f"PR {merge.pr_id} {how}: the review is closed by the merge.")
        except (BoardError, sqlite3.Error) as exc:
            report.prs["review_errors"] += 1
            report.errors.append(f"review task {task_id} for PR {merge.pr_id[:8]}: {exc}")

    def _since(self, override: int | None) -> int:
        """Where this poll starts reading.

        A cursor that was deleted is a reset, not a first run: starting from the lookback
        floor would announce a week of PRs the channel never heard about, one burst per
        poll. A reset starts from now -- the bridge narrates what it watches, and it starts
        watching now. A repo it has never read keeps the configured floor, which is what an
        operator asking for history gets.
        """
        if override is not None:
            return max(0, int(override))
        stored = self.store.ngit_cursor(self.target.key, "prs")
        if stored and str(stored).isdigit():
            return int(stored)
        now = int(self._now())
        if self.store.ngit_watched(self.target.key):
            return max(0, now - CURSOR_OVERLAP_S)
        hours = max(0, int(self.config.lookback_hours or 0))
        return max(0, now - hours * 3600) if hours else 0

    # -- the queue -------------------------------------------------------

    def _enqueue(self, kind: int, source_id: str, notice: str, text: str) -> bool:
        """Write a notice down before it is sent.

        A merge supersedes a review line that has not gone out yet: telling the channel a PR
        is ready for review after it has already merged is noise, and the review line never
        landed, so nothing is being hidden.
        """
        if notice == "merged":
            self.store.ngit_notice_drop(self.target.key, source_id)
        return self.store.ngit_notice_put(
            self.target.key, kind, source_id, notice,
            conversation=self.target.conversation, text=text)

    def _requeue_stranded(self, report: Report) -> None:
        """Make a notice again for a PR whose link says `pending` and whose queue row is gone.

        `pending` is a promise that the queue holds the notice. If a pass died between
        writing the link and queueing the notice, nothing does -- and because the cursor
        already moved past that PR, no later poll would see it. The link's own note has
        everything the message needs.
        """
        for link in self.store.ngit_stranded(self.target.key, KIND_PR):
            note = _detail(link)
            subject = str(note.get("subject") or "")
            if not subject:
                continue
            pr = PullRequest(id=str(link["source_id"]), author=str(note.get("author") or ""),
                             subject=subject, branch=str(note.get("branch") or ""),
                             nevent=str(note.get("nevent") or ""))
            if self._enqueue(KIND_PR, pr.id, "ready", ready_message(pr)):
                log.warning("ngit: requeued a notice for PR %s that had none", pr.id[:12])
                report.notes.append(f"requeued a notice for PR {pr.id[:12]}")

    def _deliver(self, report: Report, *, dry_run: bool) -> None:
        """Hand the queue to the daemon and say how many notices are still waiting.

        The count is what "pending" means in the report: notices the channel has not been
        told about yet, which the next pass will try again.
        """
        repo = self.target.key
        if not dry_run:
            if self.notify is None:
                report.delivery_failed("no channel notifier configured")
            else:
                self._drain(report, self.notify)
        report.prs["pending"] = self.store.ngit_notice_count(repo, kind=KIND_PR)

    def _drain(self, report: Report, notify: Notifier) -> None:
        """One notice at a time, in the order they were decided.

        A daemon that does not answer stops the drain. It has already ignored one notice for
        the whole timeout, so the rest are safer queued than each costing another timeout;
        the pass reports how many are waiting, and the next one tries again. A refusal stops
        it too, because it will be refused the same way, and the operator needs one error
        rather than one per notice.

        Each attempt carries the notice row's own id as its idempotency key, so a retry is
        the same message to the daemon -- and a notice queued again after a task was
        recreated is a different one.
        """
        repo = self.target.key
        for row in self.store.ngit_notice_due(repo, max(1, int(self.config.max_prs or 1))):
            key = f"ngit:{row['id']}:{str(row['source_id'])[:12]}:{row['notice']}"
            try:
                notify(row["conversation"], row["text"], key)
            except NotifyBusy as exc:
                self.store.ngit_notice_failed(repo, row["source_id"], row["notice"], str(exc))
                report.delivery_pending(str(exc))
                break
            except NotifyError as exc:
                self.store.ngit_notice_failed(repo, row["source_id"], row["notice"], str(exc))
                report.delivery_failed(str(exc))
                break
            state = notice_state(row["kind"], row["notice"])
            self.store.ngit_notice_sent(repo, row["kind"], row["source_id"], row["notice"],
                                        state=state)
            report.posted += 1
            if int(row["kind"]) == KIND_PR:
                report.prs["merged" if state == "merged" else "signalled"] += 1


def _detail(link: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(link.get("detail") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def pr_detail(pr: PullRequest, *, task_error: str = "") -> dict[str, Any]:
    """What a later poll needs about a PR whose event it may no longer be carrying."""
    detail: dict[str, Any] = {"nevent": pr.nevent, "subject": pr.subject,
                              "author": pr.author, "branch": pr.branch}
    if pr.commit:
        detail["commit"] = pr.commit
    if task_error:
        # The one thing that makes a later poll try the review task again: the channel was
        # already told there is none, so the retry posts nothing and only makes the task.
        detail["task_error"] = task_error
    return detail


def task_body(issue: Issue, target: Target) -> str:
    """The task body: the issue's own text, then who and where it came from."""
    body = (issue.body or "").strip() or "(the issue has no body)"
    return "\n".join([
        body,
        "",
        "---",
        f"Issue: {issue.id}",
        f"Author: {issue.author}",
        f"Repo: {target.address or target.repo}",
    ])


# -- answering a held issue ----------------------------------------------------

def answer(store: Store, control: ControlConfig, *, ref: str, yes: bool, by: str,
           reason: str = "", home: str | Path | None = None,
           now: Callable[[], float] = time.time) -> dict[str, Any]:
    """Promote (yes) or decline (no) a held issue's task, recording who decided and when.

    The answer is a person's, so it must come from a trusted npub: a stranger who filed the
    issue must not also be able to authorise it. The decision is written as a task comment
    first and the link state second, so an audit line exists even if the later write fails.
    """
    who = as_hex(by)
    if not who:
        raise NgitError(f"--by {by!r} is not an npub or hex key")
    if not control.is_trusted(who, TRUSTED_PLANE):
        raise NgitError(f"{npub_of(who)} is not a trusted npub: only a trusted author can "
                        "answer this question")

    link = _link_for(store, ref)
    task_id = str(link.get("task_id") or "")
    slug = str(link.get("board") or "")
    if not task_id or not slug:
        raise NgitError(f"the link for {ref} records no task on a board")
    detail = _detail(link)
    issue_nevent = str(detail.get("nevent") or ref)

    try:
        board = Board(board_path(slug, home=home))
    except BoardError as exc:
        raise NgitError(str(exc)) from exc
    try:
        task = board.task(task_id)
        if task is None:
            raise NgitError(f"task {task_id} is no longer on board {slug}")
        if task["status"] != "blocked":
            return {"task": task_id, "status": task["status"], "changed": False,
                    "issue": issue_nevent, "by": npub_of(who),
                    "note": "the task is not blocked; nothing to answer"}

        stamp = dt.datetime.fromtimestamp(int(now()), dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            if yes:
                promoted = board.release(
                    task_id, author="ngit",
                    comment=f"Authorised by {npub_of(who)} at {stamp}: issue {issue_nevent}"
                            " -> ready")
                if not promoted:
                    raise NgitError(f"task {task_id} changed state while answering; "
                                    "nothing was recorded, try again")
                store.ngit_link_state(link["repo"], KIND_ISSUE, link["source_id"],
                                      "authorised")
                return {"task": task_id, "status": "ready", "changed": True,
                        "issue": issue_nevent, "by": npub_of(who)}
            board.comment(task_id, "ngit",
                          f"Declined by {npub_of(who)} at {stamp}: "
                          f"{reason.strip() or 'no reason given'}; the task stays blocked.")
            store.ngit_link_state(link["repo"], KIND_ISSUE, link["source_id"], "declined")
            return {"task": task_id, "status": "blocked", "changed": True,
                    "issue": issue_nevent, "by": npub_of(who), "reason": reason.strip()}
        except BoardError as exc:
            raise NgitError(f"could not record the answer on {task_id}: {exc}") from exc
    finally:
        board.close()


def _link_for(store: Store, ref: str) -> dict[str, Any]:
    text = str(ref or "").strip()
    if not text:
        raise NgitError("no task or issue given")
    if text.startswith("t_"):
        link = store.ngit_link_for_task(text)
    else:
        link = store.ngit_link_for_source(KIND_ISSUE, event_hex(text))
    if link is None or int(link.get("kind") or 0) != KIND_ISSUE:
        raise NgitError(f"no ngit issue is linked to {text!r}")
    return link
