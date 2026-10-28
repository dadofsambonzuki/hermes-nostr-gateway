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

A review task is a task like any other, except that it may be worked by a stronger model:
`ngit.review_model` is written to the task's `model_override` (the column
`hermes kanban set-model` uses), which the dispatcher already honours. That is how review
runs on a different model without a second profile.

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

#: The plane whose trusted list decides an issue's author. Concord, because that is the
#: plane a channel belongs to; `trusted_in` unions it with the global list.
TRUSTED_PLANE = "concord"

#: States a link can hold. `held` means the question has not reached the channel yet (a
#: retry is correct); `asked` means it did (asking again is not). For a PR, `pending` is the
#: same idea: it is known, the channel message has not been confirmed yet.
ISSUE_STATES = frozenset({"ready", "held", "asked", "authorised", "declined", "deleted"})
PR_STATES = frozenset({"pending", "signalled", "merged"})

#: Seconds of overlap on a relay cursor. Relays apply `since` inclusively and clocks drift;
#: the link table, not the cursor, is what stops a replayed event being reported twice.
CURSOR_OVERLAP_S = 300


class NgitError(RuntimeError):
    """A sync or an answer cannot proceed; the message says what the operator must fix."""


class NotifyError(RuntimeError):
    """The channel message did not reach the daemon, so it never reached the channel."""


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
    #: This channel's review model, from its `concord.projects` entry. Beats the bridge's
    #: own `ngit.review_model`; empty means the bridge's, then the profile's.
    review_model: str = ""
    review_provider: str = ""

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
    review_model: str = ""
    review_provider: str = ""
    relays: tuple[str, ...] = ()
    lookback_hours: int = 168
    issues: bool = True
    prs: bool = True
    priority: int = 0
    #: How many new tasks one poll may create. A backlog (or a spam run of issues from an
    #: open relay) is spread over polls instead of filling the board and the channel in one
    #: go; nothing is dropped, and the report says how much is still waiting.
    max_new: int = 20
    #: How many PR messages one poll may post. The first run (or a catch-up after downtime)
    #: must not dump a month of merged PRs into a channel; the rest follow on later polls.
    max_prs: int = 10

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
        review_model = str(block.get("review_model") or "").strip()
        review_provider = str(block.get("review_provider") or "").strip()
        if review_provider and not review_model:
            # The dispatcher resolves a provider only alongside a model; a provider alone
            # would be read as one and quietly change nothing.
            log.warning("ngit: review_provider is set without review_model; ignoring it")
            review_provider = ""
        return cls(
            assignee=str(block.get("assignee") or "").strip(),
            reviews=bool(block.get("reviews", True)),
            review_assignee=str(block.get("review_assignee") or "default").strip(),
            review_model=review_model,
            review_provider=review_provider,
            relays=cleaned,
            lookback_hours=int(block.get("lookback_hours", 168) or 0),
            issues=bool(block.get("issues", True)),
            prs=bool(block.get("prs", True)),
            priority=int(block.get("priority", 0) or 0),
            max_new=max(1, int(block.get("max_new", 20) or 1)),
            max_prs=max(1, int(block.get("max_prs", 10) or 1)),
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
        "would_post": 0, "tasks": 0, "review_errors": 0})
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
    only asks about PR ids it already knows, so an issue's resolution cannot post a merge."""
    pr_id = _root_id(event)
    if not event_hex(pr_id):
        return None
    commit = _tag(event, "merge-commit")
    if not commit:
        applied = _tags(event, "applied-as-commits")
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
    command = ["ngit", *args, "--json"]
    try:
        done = subprocess.run(command, cwd=str(repo), capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise NgitError("ngit is not installed: `hng ngit sync` reads issues through it"
                        ) from exc
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
    """

    def __init__(self, socket_path: str | Path, *, timeout: float = 8.0):
        self.socket_path = Path(socket_path)
        self.timeout = timeout

    def __call__(self, conversation: str, text: str) -> None:
        from .server import request

        def call() -> dict[str, Any]:
            return asyncio.run(request(self.socket_path,
                                       {"op": "send", "to": conversation, "text": text},
                                       timeout=self.timeout))

        try:
            frame = _in_thread_if_running(call)
        except (OSError, ConnectionError, TimeoutError, ValueError) as exc:
            raise NotifyError(f"the daemon did not answer ({exc})") from exc
        if frame.get("type") == "error" or not frame.get("ok"):
            raise NotifyError(str(frame.get("error") or "the send was refused")[:200])


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


def _looks_hex(value: str) -> bool:
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
                              channel_id=channel_ref if _looks_hex(channel_ref) else "",
                              channel_name="" if _looks_hex(channel_ref) else channel_ref)
    if project is None:
        # An entry may also be keyed by the whole conversation, which is the one string a
        # cron job has: `channel: concord:<community>:<channel>`.
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
    return Target(conversation=conversation, repo=path, board=slug, project=project.project,
                  review_model=project.review_model, review_provider=project.review_provider)


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
Notifier = Callable[[str, str], None]


class Sync:
    """One pass over a channel's repo: issues in, PR news out.

    Every collaborator is injected, so the tests drive the whole state machine without a
    relay, a daemon or an `ngit` child process, and the real ones are thin.
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
                self._pull_requests(report, since=since, dry_run=dry_run)
            except NgitError as exc:
                report.errors.append(f"prs: {exc}")
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
            # at a task that no longer exists.
            self.store.ngit_forget_comments(key, issue.hex)
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
            self._post(notice_message(issue, task_id), report, dry_run=dry_run)
        else:
            self._ask(issue, task_id, report, dry_run=dry_run)
        return "created"

    def _ask(self, issue: Issue, task_id: str, report: Report, *, dry_run: bool) -> bool:
        """Claim the question, post it, and only keep the claim when it landed."""
        key = self.target.key
        if dry_run:
            report.issues["would_post"] += 1
            return False
        self.store.ngit_link_state(key, KIND_ISSUE, issue.hex, "asked")
        if not self._post(question_message(issue, task_id, self.target, self.config.assignee),
                          report, dry_run=dry_run):
            # Nothing reached the channel, so the next sync may ask: exactly one question,
            # eventually, and never two.
            self.store.ngit_link_state(key, KIND_ISSUE, issue.hex, "held")
            report.errors.append(f"question for {issue.id} not delivered; the task stays held")
            return False
        log.info("ngit: asked about %s (task %s)", issue.id, task_id)
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

    def _pull_requests(self, report: Report, *, since: int | None, dry_run: bool) -> None:
        if self.prs is None:
            raise NgitError("no PR source configured")
        start = self._since(since)
        prs, merges = self.prs(start)
        newest = start
        applied = {merge.pr_id: merge for merge in merges}
        budget = max(1, int(self.config.max_prs or 1))

        for pr in sorted(prs, key=lambda p: (p.created_at, p.id)):
            newest = max(newest, pr.created_at)
            report.prs["seen"] += 1
            link = self.store.ngit_link(self.target.key, KIND_PR, pr.id)
            if link is not None and link["state"] != "pending":
                report.prs["existing"] += 1
                self._late_review_task(pr, link, report, dry_run=dry_run)
                continue
            if budget <= 0:
                # Nothing is forgotten: it is reported as deferred and picked up next poll.
                report.prs["deferred"] += 1
                continue
            merge = applied.get(pr.id)
            if dry_run:
                report.prs["would_post"] += 1
                budget -= 1
                continue
            budget -= 1
            if link is None:
                # `pending` is a claim, not a report: it is what lets a send that failed
                # (or was interrupted) be retried next poll instead of vanishing.
                self.store.ngit_link_save(
                    self.target.key, KIND_PR, pr.id, board=self.target.board,
                    state="pending", detail=pr_detail(pr))
            # The task first, the message second: the message names it. A board that cannot
            # be written must not cost the channel its notice, so the failure comes back as
            # a line in the notice and is retried -- without a second message -- later.
            task_id, problem = ("", "")
            if merge is None and self.config.reviews:
                task_id, problem = self._review_task(pr, report, dry_run=dry_run)
                self._note_review(pr, link, task_id, problem)
            text = (merged_message(pr, merge) if merge is not None
                    else ready_message(pr, review_task=task_id, problem=problem))
            if not self._post(text, report, dry_run=dry_run):
                continue
            state = "merged" if merge is not None else "signalled"
            self.store.ngit_link_state(self.target.key, KIND_PR, pr.id, state)
            report.prs["merged" if state == "merged" else "signalled"] += 1

        for merge in sorted(merges, key=lambda m: (m.created_at, m.pr_id)):
            newest = max(newest, merge.created_at)
            link = self.store.ngit_link(self.target.key, KIND_PR, merge.pr_id)
            if link is None:
                # A merge for a PR this bridge never announced is not replayed into the
                # channel; it narrates what it watched, not the repository's history.
                continue
            known = next((pr for pr in prs if pr.id == merge.pr_id), None)
            note = _detail(link)
            nevent = str(note.get("nevent") or "")
            if known is None and note.get("subject"):
                known = PullRequest(id=merge.pr_id, author=str(note.get("author") or ""),
                                    subject=str(note.get("subject") or ""),
                                    branch=str(note.get("branch") or ""), nevent=nevent,
                                    commit=str(note.get("commit") or ""))
            if link["state"] != "merged":
                if budget <= 0:
                    continue
                text = merged_message(known, merge, nevent)
                if dry_run:
                    report.prs["would_post"] += 1
                    budget -= 1
                    continue
                budget -= 1
                if not self._post(text, report, dry_run=dry_run):
                    continue
                self.store.ngit_link_state(self.target.key, KIND_PR, merge.pr_id, "merged")
                report.prs["merged"] += 1
            # The merge closes the loop: the review task is done, with the merge named on it.
            # A task already completed is a no-op, so a merge seen twice writes nothing. It
            # runs even for a link already `merged`, so a poll that could not close the task
            # (a locked board) closes it on the next one instead of leaving it open for ever.
            self._close_review(known, merge, link, report, dry_run=dry_run)

        # The cursor is a promise that everything before it has been dealt with. Advancing
        # it past a deferred PR (or one whose message never landed, or whose review task
        # could not be made) would skip that PR for ever, so it only moves when this poll
        # left nothing behind.
        if not dry_run and newest > start and not report.prs["deferred"] \
                and not report.prs["review_errors"] and not report.failed:
            self.store.ngit_set_cursor(self.target.key, "prs",
                                       max(0, newest - CURSOR_OVERLAP_S))

    def _review_model(self) -> tuple[str, str]:
        """The (model, provider) a review task carries: the channel's entry, then the block.

        A `concord.projects` entry's own `review_model` wins, because a channel is where a
        different kind of work lives; `ngit.review_model` is the default for every channel.
        Empty means the assignee profile's model, which is what the dispatcher does anyway.
        """
        if self.target.review_model:
            return self.target.review_model, self.target.review_provider
        return self.config.review_model, self.config.review_provider

    def _review_task(self, pr: PullRequest, report: Report, *, dry_run: bool) -> tuple[str, str]:
        """One review task for this PR, or the reason there is none.

        Keyed `ngit:pr:<event id>`, so a re-poll, a retry after a failed send and a retry
        after a failed create all land on the same task. Never raises: the caller still has
        a channel message to post, and "no task because ..." is a better message than none.
        """
        if dry_run:
            return "", ""
        key = f"ngit:pr:{pr.id}"
        model, provider = self._review_model()
        try:
            existing = self.board.by_idempotency(key)
            if existing is not None:
                return str(existing["id"]), ""
            task_id = self.board.create(
                title=review_title(pr), body=review_body(pr, self.target),
                assignee=self.config.review_assignee or "default", status="ready",
                idempotency_key=key, anchor=self.anchor, priority=self.config.priority,
                model_override=model, provider_override=provider)
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
        if override is not None:
            return max(0, int(override))
        stored = self.store.ngit_cursor(self.target.key, "prs")
        if stored and str(stored).isdigit():
            return int(stored)
        hours = max(0, int(self.config.lookback_hours or 0))
        return max(0, int(self._now()) - hours * 3600) if hours else 0

    # -- posting ---------------------------------------------------------

    def _post(self, text: str, report: Report, *, dry_run: bool) -> bool:
        if dry_run:
            report.prs["would_post"] += 1
            return False
        if self.notify is None:
            report.delivery_failed("no channel notifier configured")
            return False
        try:
            self.notify(self.target.conversation, text)
        except NotifyError as exc:
            report.delivery_failed(str(exc))
            return False
        report.posted += 1
        return True


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
