"""The ngit bridge: a channel's repo issues become work, its PRs are announced in it.

One mapping, two directions. `concord.projects` already says which checkout a channel's
work belongs to; it now also says which board that work queues on (see `projects.py`), so
the same lookup serves the inbound and the outbound half of this module:

    inbound   kind-1621 issue    -> a task on the channel's board
              kind-1111 comment  -> a task comment, in the order the issue carries them
    outbound  kind-1618 PR       -> one "ready for review" message in the channel, and one
                                   review task on that board (a verdict, never a merge:
                                   merging is the operator's)
              kind-1619 revision -> reopens that one task with the new tip, as one comment;
                                   a revision never makes a second card
              a verdict comment  -> finishes the review task's role: approved completes it
                                   with the verdict as its result, changes-requested queues
                                   one fix card (keyed on the verdict event) for the
                                   implementer, with the findings; the review card stays
                                   parked on the verdict and a revision reopens it
              a verdict already behind the cursor -> `hng ngit reconcile --channel <id>`
                                   reads the PR root again and applies it to the card parked
                                   on it; a dry run names the cards and the transitions first
              kind-1631 applied  -> one "merged as <commit>" follow-up, which also completes
                                   every card the PR owns, naming the merge commit
              kind-1632 closed   -> blocks the PR's cards for a person, naming the event:
                                   a close is not a merge and cannot finish a card

**`applied` has two witnesses.** The status record is one: a kind-1631 names the commit.
The other is the checkout itself, because `ngit pr apply` replays a proposal's patches as
**new** commits -- the tip the PR event names never becomes an ancestor of the branch it
landed on -- and the kind-1631 is published by a merge route that may not have run. So a
PR the record leaves open is asked about directly: is every patch it adds already on the
base branch (`git patch-id`, `applied.py`)? If it is, the PR is applied, its cards are
finished by the same path a recorded merge takes, and the comment says the content is the
proof. If it is not, the refusal says what was compared, so "open at <tip>" is never said
about a proposal whose content is already in.

**`done` requires `applied`, or an approval.** A PR card whose PR is still open cannot be
completed by a worker -- the agent's tool, `hermes kanban complete`, or `--force` -- because
the bridge installs a guard on the board itself (`ngit_guard`). A merge releases it, a
reviewer's approving verdict releases it, and a close keeps it refusing. A worker finishes at
`review`; the verdict or the merge finishes the card.

A review task is a task like any other: the bridge names the PR, says which profile it is
dispatched to, and stops there. The model is the board's to decide -- the board's default,
then the assignee profile's own model -- so reviewing on a different model is the operator's
call (`hermes kanban set-model`), not a second knob in this config.

Authorisation is the control pane's, and it is expressed on the repository. An issue is queued
only when a trusted npub (`control.trusted_npubs` or the concord plane's own list) has published
the configured `ready` label on it: until then the issue is recorded `waiting`, with no card and
one line in the channel, so filing is not the same thing as asking for work. The label is
published where the contributor can see it, which a channel question is not. `ready_label: ""`
restores the author-keyed rule (a trusted author's issue is queued on sight; anyone else's is
held `blocked` and asked about once). A comment that @mentions this gateway is the other thing
addressed to the bot rather than the repo, and becomes one reply task for a trusted author.

State (the `ngit_*` tables in `store.py`): one link per issue and per PR root, carrying the
task id and the lifecycle the bridge reached, plus a per-repo high-water mark so a poll asks
only for what is new. A task deleted by hand leaves its `deleted` link behind, so the next
sync does not silently recreate it; `--recreate` is how an operator asks for it back.

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
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

import nostr_sdk as sdk

from . import ngit_guard
from .applied import Checkout as AppliedCheckout
from .applied import Evidence
from .control import ControlConfig, as_hex
from .fetch import fetch_many_blocking, fetch_tagged_blocking
from .kanban import (
    BLOCKED,
    DONE,
    FIXED_MARK,
    READY,
    REVIEW,
    VERDICT_MARK,
    Anchor,
    Board,
    BoardError,
    fix_key,
)
from .ngit_labels import (
    ANCHOR_KEYS,
    APPROVED,
    CHANGES_REQUESTED,
    KIND_LABEL,
    Label,
    Labeler,
    Verdict,
    VerdictAnchor,
    anchor_detail,
    anchor_verdict,
    live_label,
    newest_verdict,
    reconcile_labels,
    review_labels,
)
from .ngit_verdict import VerdictPublisherFn, card_verdict
from .projects import ProjectMap, board_dir, hermes_home
from .store import Store

log = logging.getLogger("hng.ngit")

#: NIP-34 kinds this bridge reads. 1630-1633 are the status kinds; only `applied` is news.
KIND_ISSUE = 1621
KIND_PR = 1618
#: A revision: `E` names the PR it updates, `c` the new tip. It is how a PR grows commits,
#: so it is the only event that can tell the bridge a review's verdict no longer covers the
#: tip (`ngit_labels`, `stale`).
KIND_PR_REVISION = 1619
KIND_APPLIED = 1631
#: A kind-1632 `closed` status: the proposal was withdrawn, or a rebase orphaned it and a
#: maintainer closed it. It is not a merge, so it cannot finish a card -- it hands the card
#: to a person with the closing event named.
KIND_CLOSED = 1632

#: A comment is NIP-22, not NIP-34: ngit 3.0.5 publishes `ngit issue comment` and
#: `ngit pr comment` as kind 1111 (`src/lib/git_events.rs`, `KIND_COMMENT`), and no ngit
#: release ever wrote 1622 -- the 1622s this bridge used to look for were hand-published.
#: Reading is kind-agnostic (`ngit issue view --comments` returns them either way); this
#: constant is for the watcher's subscription, which has to name a kind.
KIND_COMMENT = 1111

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
#: `held`/`asked`/`declined` are the author gate: a PR whose author nobody recognises sits
#: on one of them until a trusted person answers, and none of the three is authorised work.
ISSUE_STATES = frozenset({"ready", "held", "asked", "authorised", "declined", "deleted",
                          "waiting"})
PR_STATES = frozenset({"pending", "signalled", "held", "asked", "declined",
                       "merged", "closed"})

#: Guard states the pass must never walk back. A PR does not un-merge, a closed proposal does
#: not reopen (a re-push is a second root, not a revival), and an approving verdict stays an
#: approval -- so `applied`, `closed` and `approved` are final on the board. This matters
#: because the PR feed is **incremental**: a pass whose window held no news about a PR has not
#: learned that it is open, only that nothing arrived, and relay delivery can put a status
#: event behind the cursor for good. Rewriting `open` on that silence is what pinned a closed
#: PR's card in place; on an `approved` guard it would re-make the refusal trigger on a card
#: the verdict already finished (see docs/queue-and-prs.md, "The guard is monotonic").
#:
#: `approved` carries one caveat the other two do not: it is final only for the tip it was
#: given for. The approval *released* the trigger, so an approval left standing over a newer
#: tip is not merely stale -- it leaves the card `_revisions` just reopened with nothing to
#: refuse its completion. `_guard_covers_tip` is where that is decided.
TERMINAL_GUARD_STATES = frozenset({ngit_guard.APPLIED, ngit_guard.APPROVED, ngit_guard.CLOSED})

#: Seconds of overlap on a relay cursor. Relays apply `since` inclusively and clocks drift;
#: the link table, not the cursor, is what stops a replayed event being reported twice.
CURSOR_OVERLAP_S = 300

#: How many linked PRs one `#e` status read asks about. A status cannot change anything for
#: a PR the board has already finished, so the set is the open links; the cap bounds a board
#: that never closed anything, and the newest links are the ones news is about.
MAX_STATUS_PRS = 50

#: How many status events one PR's filter keeps. A PR has one applied or one closed status;
#: twenty is deep enough for a relay that also replays retried publishes.
STATUS_READ_AHEAD = 20

#: How far back every pass re-reads the two PR status kinds (`Sync._sweep`). The cursor is a
#: watermark on what a poll *reached*, not proof of what it delivered: a kind-1631/1632 that
#: lands at a relay after the poll whose window covered its timestamp sits behind the cursor for
#: good, and the link stays `signalled` while the board keeps obeying it. Re-reading the status
#: kinds over a trailing window is cheap -- a repository publishes a handful a week -- and every
#: transition they drive is idempotent. `0` turns the sweep off.
DEFAULT_SWEEP_HOURS = 24

#: The cursor row the sweep keeps: when it last read through. Separate from `prs` on purpose --
#: the sweep must never move the cursor proposals are read with, because an event it reported is
#: not an event the incremental poll delivered.
SWEEP_CURSOR = "prs_reconcile"

#: How long one notice waits for the daemon. A turn can hold the daemon for longer than a
#: socket round trip takes, so the timeout is generous -- but it still has to end: the notice
#: is queued, and giving up on it is what lets the poll finish and the next one retry.
NOTIFY_TIMEOUT_S = 20.0

#: Seconds the daemon's watcher waits after the last event for a channel before running a
#: pass. Long enough that a PR revision and its status event coalesce, short enough that
#: "within a minute" holds. See docs/ngit-bridge.md, "Waking the pass".
DEFAULT_WATCH_DEBOUNCE_S = 30.0

#: Roots one mention pass may ask a relay for. One REQ per relay for the batch, so this is
#: the size of a filter set rather than of a message: a repository with thousands of linked
#: PRs would otherwise build a REQ no relay will answer.
MAX_MENTION_ROOTS = 200

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
    """One NIP-22 kind-1111 reply on an issue, as `ngit issue view --comments --json`
    reports it."""

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
    """A kind-1631 applied/resolved status for a PR, with whatever commit it names.

    `evidence` is set only for a merge the pass established from the checkout rather than
    from a status event: the PR is applied, no record says so, and this is the sentence that
    says why (`its patch is already on master as decdff5`). An event-driven merge leaves it
    empty, because the event *is* the evidence.
    """

    pr_id: str         # hex of the root PR event
    commit: str = ""
    created_at: int = 0
    #: The status event itself, so two reads of one relay count it once.
    event_id: str = ""
    #: Why the pass believes it is applied, for a merge read from the checkout.
    evidence: str = ""
    #: The key that published the status. A merge is a maintainer's act, not anyone's.
    author: str = ""


@dataclass(frozen=True)
class Closed:
    """A kind-1632 closed status for a PR: no merge, so no card can be finished by it."""

    pr_id: str         # hex of the root PR event
    event_id: str = ""
    created_at: int = 0
    #: The key that published the close; the PR's own author may close it too.
    author: str = ""


@dataclass(frozen=True)
class Revision:
    """A PR update: the PR root it belongs to and the tip it moved the PR to.

    NIP-34's own spelling is kind 1619 (`E` names the PR, `c` the new tip). A client that
    republishes a proposal can also send the same tags on a second kind 1618, so the reader
    decides on the tags, not on the kind alone.
    """

    pr_id: str         # hex of the PR root event its `E` tag names
    commit: str = ""
    created_at: int = 0
    event_id: str = ""
    #: The key that published the revision; only the PR's author may move its tip.
    author: str = ""


@dataclass(frozen=True)
class PrFeed:
    """One poll's PR events: proposals, their revisions, and the merges that closed them.

    A revision is not a second proposal, so it is not folded into `prs`: the PR keeps its
    identity, its subject and its review task, and only its tip moves. That is also why
    the review task is keyed on the root: one card per PR, for the life of the PR.
    """

    prs: list[PullRequest] = field(default_factory=list)
    merges: list[Merge] = field(default_factory=list)
    revisions: list[Revision] = field(default_factory=list)
    #: Kind-1632 closes: a PR that stopped without landing. They cannot finish a card.
    closed: list[Closed] = field(default_factory=list)

    def newest_tips(self) -> dict[str, tuple[str, int]]:
        """Each PR's tip from this poll, with the time of the event that named it."""
        tips: dict[str, tuple[str, int]] = {}
        for pr in self.prs:
            if pr.commit:
                tips[pr.id] = (pr.commit, pr.created_at)
        for revision in self.revisions:
            if not revision.commit:
                continue
            seen = tips.get(revision.pr_id)
            if seen is None or revision.created_at >= seen[1]:
                tips[revision.pr_id] = (revision.commit, revision.created_at)
        return tips


def newest_revisions(revisions: Sequence[Revision]) -> list[Revision]:
    """One revision per PR: the newest tip the poll carried.

    An older one replayed by a relay, or re-read because of the cursor's overlap, must
    not move the tip back and invent a revision that never happened.
    """
    newest: dict[str, Revision] = {}
    for revision in revisions:
        if not revision.commit:
            continue
        seen = newest.get(revision.pr_id)
        if seen is None or revision.created_at >= seen.created_at:
            newest[revision.pr_id] = revision
    return list(newest.values())


def _newer_merge(candidate: Merge, current: Merge) -> bool:
    """Whether one status should stand over another for the same PR.

    A status that names a commit is the evidence a card can be finished on, so it outranks
    one that names none; between two of the same shape the newer event wins.
    """
    return (bool(candidate.commit), candidate.created_at) >= (
        bool(current.commit), current.created_at)


def merge_feeds(*feeds: PrFeed) -> PrFeed:
    """Fold several reads of the same relays into one feed, each event counted once.

    A status can arrive through both reads -- the repository's `#a` filter and the per-PR
    `#e` one -- and a relay can replay an event across passes. The fold is by event id, and
    for a PR that more than one status names, the newest record wins: a replayed older
    status must not turn the clock back on a merge or a close the board has moved past.
    """
    prs: dict[str, PullRequest] = {}
    revisions: dict[str, Revision] = {}
    merges: dict[str, Merge] = {}
    closed: dict[str, Closed] = {}
    seen: set[str] = set()
    for feed in feeds:
        for pr in feed.prs:
            if pr.id not in seen:
                seen.add(pr.id)
                prs[pr.id] = pr
        for revision in feed.revisions:
            key = revision.event_id or (f"{revision.pr_id}:{revision.commit}:"
                                        f"{revision.created_at}")
            if key not in seen:
                seen.add(key)
                revisions[key] = revision
        for merge in feed.merges:
            key = merge.event_id or f"{merge.pr_id}:{merge.commit}:{merge.created_at}"
            if key in seen:
                continue
            seen.add(key)
            current = merges.get(merge.pr_id)
            if current is None or _newer_merge(merge, current):
                merges[merge.pr_id] = merge
        for stopped in feed.closed:
            key = stopped.event_id or f"{stopped.pr_id}:{stopped.created_at}"
            if key in seen:
                continue
            seen.add(key)
            current_closed = closed.get(stopped.pr_id)
            if current_closed is None or stopped.created_at >= current_closed.created_at:
                closed[stopped.pr_id] = stopped
    return PrFeed(prs=list(prs.values()), merges=list(merges.values()),
                  revisions=list(revisions.values()), closed=list(closed.values()))

def with_statuses(feed: PrFeed, statuses: PrFeed) -> PrFeed:
    """The poll's feed with the trailing sweep's statuses folded in.

    One entry per PR and the newer event wins, so an event the incremental poll happened to
    carry is not replaced by an older copy of it. Only `merges` and `closed` come from the
    sweep: `prs` and `revisions` stay exactly what the poll delivered, which is what keeps the
    sweep out of the review-task and notice windows.
    """
    merges = {merge.pr_id: merge for merge in feed.merges}
    for merge in statuses.merges:
        seen = merges.get(merge.pr_id)
        if seen is None or merge.created_at > seen.created_at:
            merges[merge.pr_id] = merge
    closed = {stopped.pr_id: stopped for stopped in feed.closed}
    for stopped in statuses.closed:
        seen = closed.get(stopped.pr_id)
        if seen is None or stopped.created_at > seen.created_at:
            closed[stopped.pr_id] = stopped
    return PrFeed(prs=feed.prs, merges=list(merges.values()),
                  revisions=feed.revisions, closed=list(closed.values()))


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
    #: The trailing window the status sweep re-reads on every pass (`Sync._sweep`). Cheap
    #: because it names two kinds, and it is what heals a status event that landed late.
    sweep_hours: int = DEFAULT_SWEEP_HOURS
    issues: bool = True
    prs: bool = True
    priority: int = 0
    #: Keys whose kind-1631/1632 status events are accepted. Empty accepts none; a config
    #: entry is unioned with the repository announcement's own maintainers, which is where
    #: the bridge normally gets them.
    maintainers: tuple[str, ...] = ()
    #: Keys whose kind-1111 comment can state a verdict. Empty accepts none: a verdict is
    #: the review role's word, and "anyone who tags the PR" is the vulnerability this key
    #: removes.
    reviewers: tuple[str, ...] = ()
    #: The review role's signing key, when it is stored where the bridge can read it. It
    #: derives `reviewers` when that list is empty, so one source of truth, and it is the
    #: key a published verdict is signed with: a path outside every checkout (mode 0600),
    #: never a value, because this config is tracked.
    review_secret_file: str = ""
    #: Whether a verdict is read and published at all. False is the same posture as an empty
    #: reviewer list, and it keeps a deployment that wants labels only from acting on a
    #: comment; it also turns off publishing the review card's verdict on the PR as a NIP-22
    #: kind-1111 comment (inert until `review_secret_file` names the review role's key).
    verdicts: bool = True
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
    #: Whether each PR's review state is published on the PR as a NIP-34 label. On by
    #: default -- the LABELS column is what a reader looks at -- and off for a deployment
    #: that does not want the extra events on its repository's relays.
    labels: bool = True
    #: The label that queues an issue, published by a trusted npub. Empty turns the gate off
    #: (a trusted author's issue becomes a task on sight, the old behaviour). A label is the
    #: authorisation: the issue's own author is not consulted once a trusted person has said
    #: "work on this", which is what keeps a roadside issue from becoming a card.
    ready_label: str = "ready"
    #: Whether an @mention of this gateway in an issue or PR comment becomes a reply task.
    #: On by default: being mentioned is the one request in a repo that is aimed at the bot.
    mentions: bool = True
    #: The profile a mention reply task is dispatched to. Empty means `assignee`.
    mention_assignee: str = ""
    #: The nsec/hex file a mention reply is signed with, so the reply is the bot's own words
    #: in its own voice (the gateway identity). Empty means the deployment cannot publish a
    #: reply and the task says so.
    reply_secret_file: str = ""
    #: The npubs this instance speaks for. A changes-requested verdict queues a fix card,
    #: and only the instance that answers for the PR's author queues it: two instances
    #: watching one repository must not both hand the same fix to their own implementer, and
    #: an external author's PR stays theirs. Empty is the default *filter* -- the gateway's
    #: own identity, filled where the key is known -- and a deployment with no identity at
    #: all acts on what it reads, which is how this pass behaved before the key existed.
    owns: tuple[str, ...] = ()
    #: The profile a changes-requested verdict's fix card is dispatched to. Empty is
    #: `assignee`: the work a verdict asks for belongs to whoever a new issue would go to
    #: unless an operator says otherwise.
    review_fix_assignee: str = ""

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

        def keys(name: str) -> tuple[str, ...]:
            """A list of npubs/hex keys from the block, skipping anything unreadable."""
            value = block.get(name)
            if isinstance(value, str):
                value = [value]
            if not isinstance(value, (list, tuple)):
                return ()
            out: list[str] = []
            for entry in value:
                text = str(entry or "").strip()
                if text and as_hex(text):
                    out.append(text)
                elif text:
                    log.warning("ngit.%s: ignoring an unreadable key %r", name, text[:24])
            return tuple(dict.fromkeys(out))
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
        # 0 is a legitimate value here -- it is how a deployment turns the sweep off -- so the
        # absent key falls back to the default and a bad one falls back with a line.
        try:
            sweep_hours = max(0, int(block.get("sweep_hours", DEFAULT_SWEEP_HOURS)))
        except (TypeError, ValueError):
            log.warning("ngit: sweep_hours is not a number; using %dh", DEFAULT_SWEEP_HOURS)
            sweep_hours = DEFAULT_SWEEP_HOURS
        return cls(
            assignee=str(block.get("assignee") or "").strip(),
            reviews=bool(block.get("reviews", True)),
            review_assignee=str(block.get("review_assignee") or "default").strip(),
            relays=cleaned,
            lookback_hours=int(block.get("lookback_hours", 168) or 0),
            sweep_hours=sweep_hours,
            issues=bool(block.get("issues", True)),
            prs=bool(block.get("prs", True)),
            priority=int(block.get("priority", 0) or 0),
            max_new=max(1, int(block.get("max_new", 20) or 1)),
            max_prs=max(1, int(block.get("max_prs", 10) or 1)),
            maintainers=keys("maintainers"),
            reviewers=keys("reviewers"),
            review_secret_file=str(block.get("review_secret_file") or "").strip(),
            verdicts=bool(block.get("verdicts", True)),
            watch=None if watch is None else bool(watch),
            watch_debounce_s=debounce,
            labels=bool(block.get("labels", True)),
            ready_label=str(block.get("ready_label", "ready") or "").strip(),
            mentions=bool(block.get("mentions", True)),
            mention_assignee=str(block.get("mention_assignee") or "").strip(),
            reply_secret_file=str(block.get("reply_secret_file") or "").strip(),
            owns=keys("owns"),
            review_fix_assignee=str(block.get("review_fix_assignee") or "").strip(),
        )


def owns_from(config: BridgeConfig, identity: str) -> tuple[str, ...]:
    """The npubs this instance answers for: the configured list, else its own key.

    The default is the identity the bridge already writes as -- never "everybody". Queueing
    a fix is work for the queue that can push to the PR's branch, so an instance that did not
    open the PR should not queue it, however many repositories it watches. An instance with
    no identity at all has nothing to compare against, and says so by acting on what it reads
    (the behaviour before this key existed).
    """
    return tuple(config.owns) or ((str(identity),) if identity else ())


@dataclass
class Report:
    """What one sync did, in a shape a cron can read (`--json`) and a human can scan."""

    channel: str
    repo: str
    board: str
    assignee: str
    address: str = ""
    dry_run: bool = False
    #: The trailing window the status sweep re-read this pass, or 0 when there is no sweep. It
    #: is here, not only in the counts, so an operator can see the sweep *ran* even when it
    #: found nothing -- the whole point is that a quiet pass is not the same as a blind one.
    sweep_hours: int = 0
    issues: dict[str, int] = field(default_factory=lambda: {
        "seen": 0, "created": 0, "ready": 0, "held": 0, "existing": 0,
        "comments": 0, "comments_skipped": 0, "deleted": 0, "deferred": 0,
        "would_create": 0, "waiting": 0, "would_wait": 0, "would_post": 0})
    mentions: dict[str, int] = field(default_factory=lambda: {
        "seen": 0, "created": 0, "existing": 0, "ignored": 0, "deferred": 0,
        "would_create": 0})
    prs: dict[str, int] = field(default_factory=lambda: {
        "seen": 0, "signalled": 0, "merged": 0, "existing": 0, "deferred": 0,
        "pending": 0, "would_post": 0, "tasks": 0, "review_errors": 0, "held": 0,
        "labels": 0, "label_errors": 0, "revisions": 0, "reopened": 0, "would_reopen": 0,
        "approved": 0, "changes": 0, "applied": 0, "ignored": 0,
        "completed": 0, "unblocked": 0, "blocked": 0, "would_close": 0, "closed": 0,
        "verdicts": 0, "verdict_errors": 0, "would_verdict": 0,
        #: Status events the trailing sweep re-read: applied/closed only, never a proposal.
        "swept": 0})
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
                "mentions": dict(self.mentions), "sweep_hours": self.sweep_hours,
                "posted": self.posted, "failed": self.failed,
                "errors": list(self.errors), "notes": list(self.notes)}

    def lines(self) -> list[str]:
        out = [f"channel: {self.channel}", f"repo:    {self.repo}"
               + (f" ({self.address})" if self.address else " (not a nostr repo)"),
               f"board:   {self.board}  (assignee {self.assignee})"]
        i, p = self.issues, self.prs
        out.append(f"issues:  {i['seen']} seen, {i['created']} created ({i['ready']} ready,"
                   f" {i['held']} held), {i['existing']} known, {i['comments']} comment(s)"
                   + (f", {i['waiting']} waiting for a ready label" if i["waiting"] else "")
                   + (f", {i['deleted']} deleted by hand" if i["deleted"] else "")
                   + (f", {i['deferred']} waiting for the next poll" if i["deferred"] else "")
                   + (f", {i['would_wait']} would wait for a ready label"
                      if self.dry_run and i["would_wait"] else "")
                   + (f", {i['would_create']} would be created" if self.dry_run else ""))
        m = self.mentions
        if any(m.values()):
            out.append(f"mentions: {m['seen']} seen, {m['created']} reply task(s)"
                       + (f", {m['existing']} known" if m["existing"] else "")
                       + (f", {m['ignored']} from an untrusted author" if m["ignored"] else "")
                       + (f", {m['deferred']} waiting for the next poll" if m["deferred"] else "")
                       + (f", {m['would_create']} would create a task"
                          if self.dry_run and m["would_create"] else ""))
        out.append(f"prs:     {p['seen']} seen, {p['signalled']} signalled,"
                   f" {p['merged']} merged, {p['existing']} known"
                   + (f", {p['held']} held for an answer" if p["held"] else "")
                   + (f", {p['ignored']} ignored from an unauthorised author"
                      if p["ignored"] else "")
                   + (f", {p['closed']} closed" if p["closed"] else "")
                   + (f", {p['pending']} pending (queued for the next pass)"
                      if p["pending"] else "")
                   + (f", {p['tasks']} review task(s)" if p["tasks"] else "")
                   + (f", {p['revisions']} revision(s)" if p["revisions"] else "")
                   + (f", {p['reopened']} card(s) reopened" if p["reopened"] else "")
                   + (f", {p['approved']} card(s) finished by an approval"
                      if p["approved"] else "")
                   + (f", {p['changes']} fix card(s) queued" if p["changes"] else "")
                   + (f", {p['applied']} PR(s) applied without a status event"
                      if p["applied"] else "")
                   + (f", {p['completed']} card(s) completed by a merge" if p["completed"] else "")
                   + (f", {p['unblocked']} card(s) released by a merge" if p["unblocked"] else "")
                   + (f", {p['blocked']} card(s) blocked by a close" if p["blocked"] else "")
                   + (f", {p['would_close']} would close" if self.dry_run else "")
                   + (f", {p['would_reopen']} would reopen" if self.dry_run else "")
                   + (f", {p['review_errors']} review failure(s)" if p["review_errors"] else "")
                   + (f", {p['labels']} label(s) published" if p["labels"] else "")
                   + (f", {p['label_errors']} label failure(s)" if p["label_errors"] else "")
                   + (f", {p['verdicts']} verdict(s) published" if p["verdicts"] else "")
                   + (f", {p['verdict_errors']} verdict failure(s)" if p["verdict_errors"] else "")
                   + (f", {p['would_verdict']} verdict(s) would be published"
                      if self.dry_run and p["would_verdict"] else "")
                   + (f", {p['deferred']} waiting for the next poll" if p["deferred"] else "")
                   + (f", {p['would_post']} would be posted" if self.dry_run else ""))
        if self.sweep_hours:
            out.append(f"sweep:   {p['swept']} applied/closed event(s) re-read over"
                       f" {self.sweep_hours}h (status kinds only)")
        out.append(f"posted:  {self.posted} channel message(s)"
                   + (f", {self.failed} not delivered" if self.failed else "")
                   + (" (dry run: nothing written)" if self.dry_run else ""))
        for error in self.errors:
            out.append(f"error:   {error}")
        for note in self.notes:
            out.append(f"note:    {note}")
        return out


@dataclass
class ReconcileRow:
    """One verdict-parked card, and the transition its verdict earns (or would).

    `status` is the card's status when the pass found it, `to` the one it lands in, `who`
    the profile it lands on -- the implementer for a changes request, the reviewer for a
    verdict that a newer tip has superseded. `why` is the sentence an operator reads.
    """

    task_id: str
    pr_id: str
    status: str
    verdict: str = ""
    verdict_event: str = ""
    to: str = ""
    who: str = ""
    why: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"card": self.task_id, "pr": self.pr_id, "from": self.status,
                "to": self.to, "assignee": self.who, "verdict": self.verdict,
                "verdict_event": self.verdict_event, "why": self.why}

    def line(self) -> str:
        return (f"{self.task_id}  PR {self.pr_id[:8]}  {self.status} -> {self.to or '?'}"
                + (f" ({self.who})" if self.who else "") + f": {self.why}")


@dataclass
class ReconcileReport:
    """What one reconcile did: the cards it moved (or would), the reasons, the failures.

    One row per card rather than counters, because the question this pass answers is
    "which cards, and where to" -- an operator runs it once, reads four lines and knows
    the blast radius before agreeing to it. `--json` is the same rows.
    """

    channel: str
    repo: str
    board: str
    address: str = ""
    dry_run: bool = False
    cards: list[ReconcileRow] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"channel": self.channel, "repo": self.repo, "board": self.board,
                "repo_address": self.address, "dry_run": self.dry_run,
                "cards": [card.as_dict() for card in self.cards],
                "errors": list(self.errors), "notes": list(self.notes)}

    def lines(self) -> list[str]:
        out = [f"channel: {self.channel}",
               f"repo:    {self.repo}"
               + (f" ({self.address})" if self.address else " (not a nostr repo)"),
               f"board:   {self.board}"]
        verb = "would move" if self.dry_run else "moved"
        for card in self.cards:
            out.append(f"{verb}: {card.line()}")
        out.append(f"{len(self.cards)} card(s) {'would move' if self.dry_run else 'moved'}"
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


def looks_commit(value: str) -> bool:
    """A git object id: a full 40- or 64-character hex commit, nothing else.

    A `c` tag is written into review briefs, fix briefs and the merge record. It is also
    attacker-controlled on any event the bridge did not write, so a value that is not a
    commit is not a tip and is never stored or quoted as one.
    """
    text = str(value or "").strip().lower()
    return len(text) in (40, 64) and set(text) <= _HEX


def is_future(created_at: int, now: float, *, skew_s: int = 300) -> bool:
    """True for a timestamp beyond the clock skew the read set tolerates.

    A relay replaying an event cannot move a card if the event claims a time that has not
    happened yet; the newest-wins rules are all `created_at` comparisons.
    """
    return int(created_at or 0) > int(now) + int(skew_s)


def merge_from_event(event: dict[str, Any],
                     maintainers: Sequence[str] | None = None) -> Optional[Merge]:
    """A kind-1631 that refers to a PR. Resolved issues come through here too; the caller
    only asks about PR ids it already knows, so an issue's resolution cannot post a merge.

    The commit comes from whichever spelling the publisher used: NIP-34's `merge-commit` or
    ngit's `merge-commit-id`. Only when neither names one is `applied-as-commits` read, which
    is what a proposal applied as patches carries -- there is no merge commit to name then.
    A commit that is not hex is dropped rather than quoted, and the card then stays open for
    want of evidence.

    `maintainers` is the status allowlist. `None` means the caller configured none at this
    layer -- a reader built only to parse, where the pass's own check is the gate -- while an
    empty sequence is an explicit "trust nobody": a merge is the maintainer's act, and the
    bridge must not finish cards on a stranger's word.
    """
    author = as_hex(str(event.get("pubkey") or ""))
    if maintainers is not None:
        allowed = {as_hex(who) for who in maintainers if str(who or "").strip()}
        if not allowed or author not in allowed:
            return None
    pr_id = _root_id(event)
    if not event_hex(pr_id):
        return None
    commit = ""
    for name in MERGE_COMMIT_TAGS:
        commit = _tag(event, name).strip()
        if commit:
            break
    if not commit:
        applied = _tags(event, APPLIED_COMMIT_TAG)
        commit = applied[0].strip() if applied else ""
    if commit and not looks_commit(commit):
        commit = ""
    return Merge(pr_id=event_hex(pr_id), commit=commit,
                 created_at=int(event.get("created_at") or 0),
                 event_id=str(event.get("id") or ""), author=author)


def closed_from_event(event: dict[str, Any]) -> Optional[Closed]:
    """A kind-1632 closed status: the PR it names and the event that closed it.

    A close is not a merge and cannot finish a card, but it is not noise either: the card
    must stop waiting for a merge that will never come and hand itself to a person, with the
    closing event named so the reason can be read.

    The author travels with it. A maintainer may close any proposal; the PR's own author
    may withdraw theirs. Anyone else's close is checked and ignored by the caller, which is
    where the PR's author is known.
    """
    pr_id = event_hex(_root_id(event))
    if not pr_id:
        return None
    return Closed(pr_id=pr_id, event_id=str(event.get("id") or ""),
                  created_at=int(event.get("created_at") or 0),
                  author=as_hex(str(event.get("pubkey") or "")))


def revision_from_event(event: dict[str, Any]) -> Optional[Revision]:
    """A kind-1619 PR update: the PR its `E` tag names, and the tip its `c` tag moves to.

    NIP-34 spells the reference `E` (NIP-22); an event without one cannot be attached to a
    PR and is ignored rather than guessed at. The commit must be a hex object id: the tip is
    written into review briefs and compared with an approval, so a free-text `c` tag is not
    one. The author travels with it, and the caller accepts it only from the PR's author.
    """
    pr_id = event_hex(_tag(event, "E"))
    commit = _tag(event, "c").strip()
    if not pr_id or not looks_commit(commit):
        return None
    return Revision(pr_id=pr_id, commit=commit,
                    created_at=int(event.get("created_at") or 0),
                    event_id=str(event.get("id") or ""),
                    author=as_hex(str(event.get("pubkey") or "")))


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
    #: The announcement's maintainers, as hex. Status events (a merge, a close) are theirs
    #: to publish; a stranger who tags the repository is not a maintainer.
    maintainers: tuple[str, ...] = ()


def _relay_url(host: str) -> str:
    """A hosting server as a relay URL: `ngit repo --json` names them without a scheme."""
    value = (host or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    return f"wss://{value}"


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
        # The servers that host this repository are the relays its events live on -- and a
        # retracted label has to reach every one of them, so they are read from the checkout
        # rather than left to the announcement's hints alone.
        hosts = tuple(_relay_url(str(host)) for host in _as_list(data.get("grasp_servers")))
        # Maintainers come from the announcement, not from whoever tagged it. ngit spells
        # the resolved set `confirmed_maintainers` and the raw set `maintainers`; the
        # confirmed list is the one that has been through its signature check.
        who = _as_list(data.get("confirmed_maintainers")) or _as_list(data.get("maintainers"))
        maintainers = tuple(dict.fromkeys(
            as_hex(str(key)) for key in who if as_hex(str(key or ""))))
        coordinate = str(data.get("coordinate") or "").strip()
        if coordinate:
            try:
                parsed = sdk.Nip19Coordinate.from_bech32(coordinate)
                found = parsed.coordinate()
                address = (f"{found.kind()}:{found.public_key().to_hex()}:"
                           f"{found.identifier()}")
                # The naddr carries the announcement's relay hints, which is exactly the
                # read set for its issues and PRs (NIP-34); the hosting servers are the
                # rest of it.
                hints = tuple(str(url) for url in parsed.relays())
                return RepoRef(address=address,
                               relays=tuple(dict.fromkeys((*hints, *[h for h in hosts if h]))),
                               maintainers=maintainers)
            except Exception:  # noqa: BLE001 - fall through to the explicit fields
                log.debug("ngit: could not decode repo coordinate %s", coordinate[:24])
        owner = as_hex(str(data.get("selected_maintainer") or ""))
        identifier = str(data.get("identifier") or "").strip()
        address = f"30617:{owner}:{identifier}" if owner and identifier else ""
        return RepoRef(address=address, relays=tuple(h for h in hosts if h),
                       maintainers=maintainers)

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
    """PR events for one repository address, read from relays.

    Two reads, because NIP-34 spells a status twice over. Kinds 1618/1619/1631/1632 are
    fetched under the repository's `#a` filter -- a proposal with no `a` cannot be
    attributed to a repository, so attributing one would mean reading every ngit repo's
    PRs. Kinds 1631/1632 alone are also fetched by `#e` over the PR ids the caller already
    knows, because `ngit pr close` publishes a status with `e`/`alt`/`p` and no `a`: the
    `#a` read can never return it, and the relay's `e` index can. Attribution of a status
    is by id already (`merge_from_event`/`closed_from_event` resolve the root the caller
    asks about), so the wider read cannot act on a PR the board does not link.

    The trust rule is per kind, and it is a second split on top of that. Proposals and
    revisions are read from everyone -- a stranger's PR must be seen so it can be *held*,
    not silently dropped -- while a merge status is read only from the repository's
    maintainers, and is checked again when parsed. An empty maintainer set accepts no
    status at all.
    """

    def __init__(self, relays: Sequence[str], address: str, *,
                 fetcher: Callable[..., list[dict[str, Any]]] = fetch_many_blocking,
                 status_fetcher: Callable[..., list[Any]] = fetch_tagged_blocking,
                 secret: str | None = None, maintainers: Sequence[str] | None = None,
                 now: Callable[[], float] = time.time):
        self.relays = tuple(str(url) for url in relays if url)
        self.address = address
        self._fetch = fetcher
        self._fetch_statuses = status_fetcher
        self.secret = secret
        #: `None` is "no allowlist at this layer": a bare reader parses every status and the
        #: pass's own check is the gate. A sequence -- including an empty one -- is an
        #: explicit allowlist, and an empty one accepts no merge at all.
        self.maintainers = (None if maintainers is None else
                            tuple(as_hex(who) for who in maintainers
                                  if str(who or "").strip()))
        self._now = now

    def events(self, since: int) -> PrFeed:
        """Every PR, revision, applied and closed-status event newer than `since`."""
        return self._feed(since, kinds=(KIND_PR, KIND_PR_REVISION, KIND_APPLIED, KIND_CLOSED))

    def _feed(self, since: int, *, kinds: Sequence[int]) -> PrFeed:
        if not self.relays:
            raise NgitError("ngit.relays (or relays.read) is empty: nowhere to read PR events")
        if not self.address:
            raise NgitError("this checkout has no NIP-34 address: no PRs to read")
        wanted = {int(kind) for kind in kinds}
        raw: list[Any] = []
        if self.maintainers is None:
            # No allowlist at this layer: one read under the repository's address, every
            # kind asked for. The pass's own check is the gate, and a bare reader (a test,
            # a probe) parses what the relay answers.
            raw = self._fetch(relays=self.relays, kinds=list(kinds), a_tag=self.address,
                              since=int(since), limit=500, secret=self.secret) or []
            return self._fold(raw, wanted=wanted)
        # Proposals and revisions are read from everyone: a stranger's PR is read so it can
        # be held, not silently dropped.
        public = [kind for kind in kinds if int(kind) in (KIND_PR, KIND_PR_REVISION)]
        if public:
            raw += self._fetch(relays=self.relays, kinds=public, a_tag=self.address,
                               since=int(since), limit=500, secret=self.secret) or []
        # A merge status is a maintainer's act: narrowed at the relay, and checked again in
        # `_fold`. An empty allowlist reads no merge status at all.
        if KIND_APPLIED in wanted and self.maintainers:
            raw += self._fetch(relays=self.relays, kinds=[KIND_APPLIED],
                               authors=self.maintainers, a_tag=self.address,
                               since=int(since), limit=500, secret=self.secret) or []
        # A close may come from a maintainer or from the PR's own author, and the author is
        # only known per PR. It is read broadly and decided per PR by the fold and `Sync`.
        if KIND_CLOSED in wanted:
            raw += self._fetch(relays=self.relays, kinds=[KIND_CLOSED],
                               a_tag=self.address, since=int(since), limit=500,
                               secret=self.secret) or []
        return self._fold(raw, wanted=wanted)

    def statuses(self, pr_ids: Sequence[str]) -> PrFeed:
        """Kinds 1631/1632 that name these PRs by `e`, whether or not they carry an `a`.

        Unbounded by the cursor on purpose: a status that landed behind it is exactly the
        one a `#a` read has stopped looking for, and a PR's status set is small enough to
        re-read while the link is still open. The caller passes the ids it links, and the
        fold drops a status for any other root, so nothing here can act on an unknown PR.
        """
        ids = [pr for pr in (event_hex(str(item)) for item in pr_ids) if pr]
        if not ids:
            return PrFeed()
        if not self.relays:
            raise NgitError("ngit.relays (or relays.read) is empty: "
                            "nowhere to read a PR status")
        raw = self._fetch_statuses(relays=self.relays, kinds=[KIND_APPLIED, KIND_CLOSED],
                                   tag="e", values=ids, limit=STATUS_READ_AHEAD,
                                   secret=self.secret)
        # `fetch_tagged` names the relay that answered each event; a reader that just
        # returns events is fine too, since a status is attributed by its own root id.
        events = [item[1] if isinstance(item, tuple) else item for item in raw or ()]
        return self._fold(events, wanted={KIND_APPLIED, KIND_CLOSED})

    def sweep(self, since: int) -> PrFeed:
        """The applied/closed statuses newer than `since`: what the trailing sweep reads.

        Proposals and revisions are deliberately not asked for. The sweep exists to heal a
        link whose status event landed behind the cursor, and a proposal it re-read would be a
        second chance to announce a PR the channel may never have heard about -- a second card
        and a second message for something that is old news or was never news at all.
        """
        return self._feed(since, kinds=(KIND_APPLIED, KIND_CLOSED))

    def _fold(self, raw: Sequence[Any], *, wanted: set[int] | None = None) -> PrFeed:
        """One relay answer, split into proposals, revisions, merges and closes."""
        now = float(self._now())
        prs: dict[str, PullRequest] = {}
        merges: dict[str, Merge] = {}
        revisions: dict[str, Revision] = {}
        closed: dict[str, Closed] = {}
        seen_ids: set[str] = set()
        for event in raw:
            if not isinstance(event, dict) or not event.get("id"):
                continue
            event_id = str(event.get("id") or "")
            if event_id in seen_ids:
                continue
            seen_ids.add(event_id)
            if is_future(int(event.get("created_at") or 0), now):
                log.warning("ngit: ignored a future-dated event %s…", event_id[:12])
                continue
            kind = int(event.get("kind") or 0)
            if wanted is not None and kind not in wanted:
                # A relay may answer with more than it was asked for. The sweep's promise is
                # that it reads status kinds only, so the filter is here as well as in the
                # request: a proposal arriving anyway must not become a second announcement.
                continue
            if kind in (KIND_PR, KIND_PR_REVISION):
                # A `c` tag with an `E` that names another event is an update, whatever
                # kind the client published it as: NIP-34 spells it 1619, and a
                # republished proposal can carry the same tags on a 1618. Only a 1618
                # with no root reference is a new PR.
                revision = revision_from_event(event)
                if revision is not None and revision.pr_id != str(event.get("id") or ""):
                    seen = revisions.get(revision.pr_id)
                    if seen is None or revision.created_at >= seen.created_at:
                        revisions[revision.pr_id] = revision
                    continue
                if kind == KIND_PR_REVISION:
                    # A 1619 that names no root cannot be attached to a PR; guessing at
                    # one would move a review to the wrong record.
                    continue
                pr = pull_request_from_event(event)
                # A re-publish can repeat a subject; the id is the PR.
                prs[pr.id] = pr
            elif kind == KIND_APPLIED:
                merge = merge_from_event(event, self.maintainers)
                if merge is not None:
                    merges[merge.pr_id] = merge
            elif kind == KIND_CLOSED:
                stopped = closed_from_event(event)
                if stopped is not None:
                    seen = closed.get(stopped.pr_id)
                    if seen is None or stopped.created_at >= seen.created_at:
                        closed[stopped.pr_id] = stopped
        return PrFeed(prs=list(prs.values()), merges=list(merges.values()),
                      revisions=list(revisions.values()), closed=list(closed.values()))


#: How many events one tagged read asks a relay for. A verdict is the newest comment that
#: states one; fifty comments back is deep enough for a review thread that had a discussion
#: after the verdict, and shallow enough that a relay is not asked for a PR's whole history.
READ_AHEAD = 50

#: What `Sync._apply_verdict` did, in the terms its callers report: an approval finished the
#: card, a changes request returned it, or (`""`) nothing was written. The card's own status
#: is the state -- this is only what the report says the card became.
APPLIED_APPROVED = "approved"
APPLIED_CHANGES = "changes"


@dataclass(frozen=True)
class PrRead:
    """What the relays said about one PR: its newest verdict and the labels it carries.

    `readings` is one entry per relay that held a verdict comment: the pass uses it to say
    which relay it read and where the relays disagreed. Two relays can hold different newest
    verdicts (one of them missed an event); the newest wins, and the others are named rather
    than silently dropped.
    """

    verdict: Optional[Verdict] = None
    labels: tuple[Label, ...] = ()
    readings: tuple[tuple[str, str, int], ...] = ()

    @property
    def relay(self) -> str:
        """The relay the newest verdict came from, or "" when the PR has no verdict."""
        return self.verdict.relay if self.verdict is not None else ""

    def disagreement(self) -> str:
        """The relays that saw a different verdict from the one that won."""
        if len(self.readings) < 2 or len({label for _, label, _ in self.readings}) < 2:
            return ""
        return "; ".join(f"{relay} saw {label or 'an unreadable verdict'}"
                         for relay, label, _ in self.readings)


@dataclass(frozen=True)
class ReadState:
    """One pass's relay read: what each PR carries, and what went wrong reading it."""

    prs: dict[str, PrRead] = field(default_factory=dict)
    relays: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


#: `state(pr_ids) -> ReadState`.
StateSource = Callable[[Sequence[str]], ReadState]


def _tag_value(event: Mapping[str, Any], letter: str) -> str:
    """The first value of one tag letter on an event, or ""."""
    for tag in event.get("tags") or ():
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and str(tag[0]) == letter:
            return str(tag[1])
    return ""


class RelayLabels:
    """Read a PR root's verdict and its live labels straight from the relays.

    One REQ per relay per kind, each carrying one filter per PR: the relay that answered is
    part of the answer. That is what lets the pass say which relay's verdict it read, and
    what makes two relays holding different newest verdicts a report line instead of an
    invisible flicker.

    Comments are matched by their NIP-22 root (`E` = the PR), which is what "the verdict on
    the PR root" means; a reply inside the thread carries the same root and counts too, in
    the order it was published. Labels are matched by `e` (the PR) and the repository's `a`,
    so another repository's label on the same event id cannot be mistaken for this one's.
    """

    def __init__(self, relays: Sequence[str], address: str = "", *,
                 fetcher: Callable[..., list[tuple[str, dict[str, Any]]]] = fetch_tagged_blocking,
                 secret: str | None = None, reviewers: Sequence[str] = ()):
        self.relays = tuple(str(url) for url in relays if str(url or "").strip())
        self.address = str(address or "")
        self._fetch = fetcher
        self.secret = secret
        #: The only keys whose kind-1111 comment can state a verdict. Empty accepts none:
        #: the relay filter and the parser both apply it, so a forged `Verdict:` line
        #: neither changes a card nor publishes a label.
        self.reviewers = tuple(str(who) for who in reviewers if str(who or "").strip())

    def read(self, pr_ids: Sequence[str]) -> ReadState:
        ids = [str(pr) for pr in pr_ids if str(pr or "").strip()]
        if not ids:
            return ReadState()
        if not self.relays:
            return ReadState(errors=("ngit.relays (or relays.read) is empty: "
                                     "nowhere to read a verdict from",))
        try:
            comments = self._fetch(relays=self.relays, kinds=[KIND_COMMENT], tag="E",
                                   values=ids, limit=READ_AHEAD, secret=self.secret,
                                   authors=self.reviewers) if self.reviewers else []
            carried = self._fetch(relays=self.relays, kinds=[KIND_LABEL], tag="e",
                                  values=ids, limit=READ_AHEAD, a_tag=self.address,
                                  secret=self.secret)
        except Exception as exc:  # noqa: BLE001 - a read that failed is a report line
            return ReadState(errors=(f"relay read failed: {type(exc).__name__}: {exc}"[:200],))

        roots: dict[str, list[dict[str, Any]]] = {pr: [] for pr in ids}
        for _relay, event in comments or ():
            root = _tag_value(event, "E")
            if root in roots:
                roots[root].append(event)
        labels: dict[str, list[Label]] = {pr: [] for pr in ids}
        seen: set[tuple[str, str]] = set()
        for _relay, event in carried or ():
            label = live_label(event)
            root = _tag_value(event, "e")
            if label is None or root not in labels:
                continue
            if (root, label.event_id) in seen:
                continue
            seen.add((root, label.event_id))
            labels[root].append(label)
        answered = tuple(dict.fromkeys(
            str(relay) for relay, _ in [*(comments or ()), *(carried or ())]))

        prs: dict[str, PrRead] = {}
        for pr in ids:
            readings = []
            for relay in self.relays:
                on_relay = [event for event in roots[pr]
                            if str(event.get("relay") or "") == relay]
                found = newest_verdict(on_relay, self.reviewers)
                if found is not None:
                    readings.append((relay, found.label, found.created_at))
            prs[pr] = PrRead(verdict=newest_verdict(roots[pr], self.reviewers),
                             labels=tuple(labels[pr]),
                             readings=tuple(readings))
        return ReadState(prs=prs, relays=answered)

    #: The pass calls the reader; `read` is the name a caller that has the object uses.
    __call__ = read

    def issue_labels(self, ids: Sequence[str]) -> dict[str, tuple[Label, ...]]:
        """Every label each issue carries, whichever NIP-32 namespace wrote it.

        `read` folds labels through `live_label`, which keeps only the `#t` namespace the
        review column displays. The issue gate asks a different question — does this issue
        carry a label called `ready`, and who published it — and the editor a maintainer
        uses is not necessarily the review writer, so no namespace is assumed. The target
        scopes it: an `e` tag naming *this* issue, plus this repository's `a` when the event
        carries one at all.
        """
        wanted = [str(root) for root in ids if str(root or "").strip()]
        if not wanted:
            return {}
        if not self.relays:
            raise NgitError("no relays configured: labels cannot be read")
        events = self._fetch(relays=self.relays, kinds=[KIND_LABEL], tag="e",
                             values=wanted, limit=READ_AHEAD,
                             a_tag=self.address or None, secret=self.secret)
        found: dict[str, list[Label]] = {root: [] for root in wanted}
        seen: set[tuple[str, str]] = set()
        for _relay, event in events or ():
            root = _tag_value(event, "e")
            if root not in found:
                continue
            addresses = _tags(event, "a")
            if self.address and addresses and self.address not in addresses:
                continue
            event_id = str(event.get("id") or "")
            for tag in event.get("tags") or ():
                if not isinstance(tag, (list, tuple)) or len(tag) < 2 or tag[0] != "l":
                    continue
                value = str(tag[1] or "").strip()
                if not value or (root, f"{event_id}:{value}") in seen:
                    continue
                seen.add((root, f"{event_id}:{value}"))
                found[root].append(Label(
                    value=value, event_id=event_id,
                    created_at=int(event.get("created_at") or 0),
                    relay=str(event.get("relay") or ""),
                    author=npub_of(str(event.get("pubkey") or ""))))
        return {root: tuple(labels) for root, labels in found.items()}

    def comments(self, ids: Sequence[str]) -> tuple[dict[str, list[dict[str, Any]]],
                                                    tuple[str, ...]]:
        """Raw kind-1111 comments per root, and the relays that answered any of them.

        `read` keeps only what a verdict needs, and a verdict does not need the `p` tags. A
        mention *is* those tags (NIP-27: a mentioned pubkey is `p`), so the mention pass
        reads the same events without folding them first. One REQ per relay for the whole
        batch, as `read` does.
        """
        wanted = [str(root) for root in ids if str(root or "").strip()]
        if not wanted:
            return {}, ()
        if not self.relays:
            raise NgitError("no relays configured: comments cannot be read")
        events = self._fetch(relays=self.relays, kinds=[KIND_COMMENT], tag="E",
                             values=wanted, limit=READ_AHEAD, secret=self.secret)
        grouped: dict[str, list[dict[str, Any]]] = {}
        seen: set[str] = set()
        for _relay, event in events or ():
            event_id = str(event.get("id") or "")
            if not event_id or event_id in seen:
                continue
            seen.add(event_id)
            root = _tag_value(event, "E")
            if root in wanted:
                grouped.setdefault(root, []).append(event)
        answered = tuple(dict.fromkeys(str(relay) for relay, _ in events or ()))
        return grouped, answered


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


def waiting_message(issue: Issue, label: str) -> str:
    """The one line an issue gets while it waits for a trusted `ready` label.

    It names no task on purpose: there is none, and saying so is what stops a reader
    wondering whether the bridge lost it. The label is the authorisation, and the repo is
    where it is written, so the message points at the issue rather than at a reply here.
    """
    return "\n".join([
        f'issue "{issue.subject}" by {issue.author}',
        issue.id,
        f"no task yet: a trusted author adds the `{label}` label to queue it",
    ])


def mention_title(kind: str, root_subject: str, author: str) -> str:
    """A reply task's title: short, and unambiguous about which thread it answers."""
    subject = (root_subject or "(no subject)").strip()
    if len(subject) > 80:
        subject = subject[:77].rstrip() + "..."
    return f"Reply to {author[:14]}'s mention on {kind} \"{subject}\""


def mention_body(*, kind: str, root: str, root_nevent: str, comment: dict[str, Any],
                 target: Target, secret_file: str) -> str:
    """What a reply needs: the words, the thread, and the exact command that publishes it.

    The wording is the worker's to write -- this is a person asking the bot a question in
    public, and a canned paragraph is worse than no reply. The command is not: `--reply-to`
    keeps the answer in the thread, and the key file is the only identity whose reply reads
    as the bot's own. Writing it here means the worker does not have to rediscover ngit's
    spelling of either.
    """
    body = str(comment.get("content") or "").strip() or "(the comment has no words)"
    author = npub_of(str(comment.get("pubkey") or ""))
    comment_id = str(comment.get("id") or "")
    command = "pr" if kind == "PR" else "issue"
    if secret_file:
        publish = (f"ngit {command} comment {root_nevent} --body-file <path to your reply> "
                   f"--reply-to {comment_id} --nsec-file {secret_file}")
    else:
        publish = ("no reply key is configured (`ngit.reply_secret_file`): answer in the "
                   "channel instead and say why")
    return "\n".join([
        f"{author} mentioned this gateway on a {kind} in {target.address or target.repo}.",
        "",
        "Their comment:",
        "",
        *[f"> {line}" for line in body.splitlines()],
        "",
        "---",
        f"Root: {root}",
        f"Root nevent: {root_nevent}",
        f"Comment: {comment_id}",
        f"Author: {author}",
        "",
        "Write one reply as this gateway (its own voice, not a summary of the repo) and publish",
        "it in the thread:",
        "",
        f"  {publish}",
        "",
        "Then add a one-line comment on this task naming what you posted, and complete it.",
        "Do not change code, labels, or the issue/PR state, and do not reply with anything the",
        "comment did not ask for.",
    ])


def pr_question_message(pr: PullRequest, task_id: str, target: Target,
                        assignee: str) -> str:
    """The one question an unrecognised PR author gets: no review runs until a human says yes.

    A review task checks a proposal out and executes its tests, so an unauthorised PR is not
    a message to announce -- it is a person's decision, asked once, with the command that
    records the answer.
    """
    where = f" on {pr.branch}" if pr.branch else ""
    return "\n".join([
        f'Unrecognised author for PR "{pr.subject}"',
        f"author: {pr.author}{where}",
        f"PR: {pr.id}",
        f"nevent: {pr.nevent}",
        f"task: {task_id or '(none)'} (held, not queued)",
        "",
        "Nothing reviews or runs this proposal until someone trusts its author.",
        f"Reply yes to queue it on board {target.board} for {assignee}, or no to leave it out.",
        f"`hng ngit answer {task_id or '<task>'} --yes --by <npub>` records either answer.",
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
        "   Run them in this card's worktree only -- never in the deploy checkout -- and do",
        "   not give the PR's code the daemon's keys or its live config.",
        "3. Report the verdict on this task with the reasons -- merge, or changes needed --",
        "   and say what the gates could not catch: behaviour, tests that do not exercise the",
        "   change, and anything the PR leaves out.",
        "4. Start that comment with exactly one of these lines:",
        "",
        "       Verdict: approved",
        "       Verdict: changes-requested",
        "",
        "   The bridge publishes this comment on the PR, signed by the review identity, so",
        "   write it as the text a reader of the PR should see: it is published verbatim, not",
        "   summarised. The PR's label is derived from the newest kind-1111 verdict on the PR",
        "   root, and the merge guard reads the same event; a verdict recorded only here is",
        "   neither, and cannot count as a second signature.",
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
        "The verdict ends this review, and the bridge applies it: `approved` completes the",
        "card -- the merge is still a person's -- and `changes-requested` queues a fix card",
        "for the implementer, carrying your findings verbatim, and leaves this card parked on",
        "the verdict until the fix arrives as a revision. So write the verdict on this card as",
        "a comment, naming the tip it covers, then park the card with `kanban_block`, putting",
        "`Verdict: approved` or `Verdict: changes-requested` first in the reason and the tip",
        "after it. That park is a hand-off, not a question: the bridge publishes the comment",
        "on the PR and reads the verdict back from the PR root, and it acts on the card on",
        "its next pass. **Do not call kanban_complete** -- the board",
        "refuses it while the PR is open, and a verdict recorded only on this card leaves",
        "the PR's label where it was.",
        "",
        *steps,
    ])


def fix_title(pr_id: str, subject: str) -> str:
    """The fix card's title: which PR it fixes, in the shape the review card uses."""
    short = str(pr_id or "")[:8]
    return f"Fix PR {short}: {subject}" if subject else f"Fix PR {short}"


def fix_body(pr: PullRequest, target: Target, *, tip: str, findings: str,
             event_id: str, review_task: str = "") -> str:
    """What a worker needs to make a changes-requested verdict go away.

    A fix is its own unit of work, so this is its own card: the verdict's own words, quoted
    rather than summarised (a worker that has to open a relay to learn what is wrong is a
    worker that fixes what it guessed), the branch and the tip the verdict covered, and the
    one thing that ends it -- a revision on that same branch, which is what reopens the PR's
    review card.
    """
    branch = pr.branch or "(the PR names no branch)"
    into = target.default_branch or "<the repository's default branch>"
    base = f"origin/{into}" if target.default_branch else "<the target branch>"
    return "\n".join([
        f'Changes requested on PR "{pr.subject}" -- fix them on its own branch and push a',
        "revision. This card is the fix; the PR's review card stays parked until the revision",
        "moves the tip.",
        "",
        f"PR:         {pr.id}",
        f"nevent:     {pr.nevent}",
        f"Repository: {target.address or target.repo}",
        f"Branch:     {branch}",
        *([f"Tip:        {tip}"] if tip else []),
        f"Verdict:    comment {event_id or '(unknown)'}",
        *([f"Review:     {review_task}"] if review_task else []),
        "",
        "---",
        "",
        "The findings, as the reviewer published them:",
        "",
        findings.strip() or "(the verdict comment names them; read it on the PR)",
        "",
        "---",
        "",
        "Do not merge, and do not review this tip: the work is the fix.",
        "",
        f"1. Check the PR out: `ngit pr checkout {pr.nevent or pr.id}` -- this card's worktree,",
        "   never the deploy checkout.",
        f"2. Fix the findings on its own branch (`{branch}`), on top of the tip"
        + (f" the verdict covers ({tip[:12]})" if tip else " the verdict covers") + ".",
        "3. Run the repository's own gates from that tree: `ruff check src tests scripts",
        "   benchmarks` and `pytest -q`. Say what ran and what it printed, including anything",
        "   that was already failing.",
        "4. Publish the fix as a revision of this PR rather than a second one -- the full patch",
        f"   set, never a delta: `ngit pr send {base}..HEAD --base $(git rev-parse {base})"
        f" --in-reply-to {pr.id}`.",
        "   The revision reopens the PR's review card for the new tip, and the merge route",
        "   then needs a fresh verdict on the fixed tip.",
        "5. Complete this card with the new tip as the result. If you cannot complete it, park",
        "   it with `Revision: <tip>` as the first word of the reason and the bridge finishes it.",
    ])


def revision_note(note: dict[str, Any], revision: Revision) -> str:
    """The one comment a revision puts on the PR's existing card.

    It names the tip and the event that moved it, and says what that does to a review that
    was already recorded: a reviewer opening the card after a reopen has to be able to see
    which commit is current without reading the channel.
    """
    subject = str(note.get("subject") or "")
    where = f'"{subject}"' if subject else f"PR {revision.pr_id[:8]}"
    lines = [f"Revision: {where} tip is now {revision.commit}"]
    if revision.event_id:
        lines[0] += f" (event {revision.event_id})"
    nevent = str(note.get("nevent") or "")
    if nevent:
        lines.append(nevent)
    lines.append("Review this tip: a verdict recorded before it covers the earlier revision.")
    return "\n".join(lines)


def applied_how(merge: Merge) -> str:
    """How the PR landed, in the words the channel and the card both use.

    A status event says `merged as <commit>`. A merge the pass established from the checkout
    says `applied as <commit>` and gives the evidence, because no record claims a merge: the
    patch itself is what landed, and a reader who checks the commit can see it.
    """
    if merge.commit:
        how = f"merged as {merge.commit}"
    else:
        how = "merged"
    if merge.evidence:
        how = f"applied as {merge.commit}" if merge.commit else "applied"
        how += f" ({merge.evidence})"
    return how


def merged_message(pr: PullRequest | None, merge: Merge, nevent: str = "") -> str:
    how = applied_how(merge)
    if pr is None:
        return f"PR {nevent or merge.pr_id} {how}"
    return f'PR "{pr.subject}" by {pr.author} {how}\n{pr.nevent or nevent}'


def review_verdict_message(pr_id: str, verdict: str, task_id: str) -> str:
    """The one line a published verdict puts in the channel.

    The PR's own event id, not its subject: the line is read next to the label and the card,
    and the id is what all three have in common. The card is named because that is where the
    reasoning lives, and the verdict word is the label's word.
    """
    return f"review {pr_id} -> {verdict} (task {task_id})"


def closed_message(pr: PullRequest, stopped: Closed) -> str:
    """One line for a PR that stopped without landing: not a merge, and not silence.

    The channel was told the PR was ready for review; leaving that as the last word would
    make a withdrawn proposal read as one still waiting. The closing event is named so a
    reader can see who closed it and why.
    """
    by = f" (event {stopped.event_id})" if stopped.event_id else ""
    return (f'PR "{pr.subject}" by {pr.author} was closed without a merge{by}\n'
            f"{pr.nevent}")


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
PrSource = Callable[[int], PrFeed]
#: `statuses(pr_ids) -> PrFeed`: the applied/closed statuses that name a known PR by `e`,
#: whether or not the event carries the repository's `a` (see `RelayPrs.statuses`).
StatusSource = Callable[[Sequence[str]], PrFeed]
#: `notify(conversation, text, key)`: post one channel message, and let the daemon answer a
#: repeated `key` with the message it already published instead of posting it twice.
Notifier = Callable[[str, str, str], None]


class AppliedSource(Protocol):
    """What the sync asks the checkout: is this proposal's content already on the base?

    `applied.Checkout` is the real one -- git, and `git patch-id` -- and the tests use a
    stand-in. Two methods rather than one function because `head()` is what lets a pass
    stamp an answer: an unmoved tip on an unmoved base is not probed again. See
    `applied.py` for why the status record alone is not enough on this repository.
    """

    def head(self) -> str:
        """The base branch's tip, or "" when the checkout cannot say."""
        ...

    def probe(self, tip: str) -> Evidence:
        """Whether the proposal at `tip` is already on the base branch, and why."""
        ...


def notice_state(kind: int, notice: str) -> str:
    """The link state a notice earns once the daemon has taken it.

    A verdict notice earns none: the verdict event is already on the PR, and the link row's
    state is the PR's lifecycle (`pending`, `signalled`, `merged`), not a message's. "" is
    "the receipt is the whole change".
    """
    if int(kind) == KIND_PR:
        if notice == "merged":
            return "merged"
        if notice == "closed":
            return "closed"
        if notice == "question":
            return "asked"
        if notice.startswith("review:"):
            return ""
        return "signalled"
    if notice == "question":
        return "asked"
    if notice == "waiting":
        # The issue is still not authorised: the channel has been told, and the link must
        # keep saying `waiting` so the next pass re-reads the label instead of assuming a
        # question was asked (or that a hand-deleted task was decided).
        return "waiting"
    return "ready"


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
                 statuses: StatusSource | None = None, sweep: PrSource | None = None,
                 notify: Notifier | None = None, anchor: Optional[Anchor] = None,
                 labels: Optional[Labeler] = None, state: Optional[StateSource] = None,
                 applied: Optional[AppliedSource] = None, self_npub: str = "",
                 verdicts: Optional[VerdictPublisherFn] = None,
                 now: Callable[[], float] = time.time):
        self.store = store
        self.board = board
        self.control = control
        self.config = config
        self.target = target
        #: Status events and verdicts are allowlisted by key. The config carries both; the
        #: checkout's announcement contributes its maintainers before this point.
        self.maintainers = {as_hex(who) for who in config.maintainers if str(who or "").strip()}
        self.reviewers = ({as_hex(who) for who in config.reviewers if str(who or "").strip()}
                          if config.verdicts else set())
        self.issues = issues
        self.detail = detail
        self.prs = prs
        #: The second read: statuses for the PR ids the store already links, by `#e`.
        self.statuses = statuses
        #: The trailing status sweep's source (`RelayPrs.sweep`): the same relays, two
        #: kinds. Injected and optional so a pass with no PR source, or a test, has no sweep.
        self.sweep_source = sweep
        self.notify = notify
        self.anchor = anchor or Anchor()
        self.labels = labels
        self.state = state
        # The checkout is asked only when the record is silent about a PR, and only about a
        # PR the bridge links: `applied.py` has the reason the record can be silent (an
        # `ngit pr apply` merge publishes no status of its own) and what it compares instead.
        self.applied: Optional[AppliedSource] = applied or AppliedCheckout(
            target.repo, target.default_branch or "master")
        #: This gateway's own key, hex: a mention is a `p` tag naming it, and a comment that
        #: carries it is the one thing in a repo addressed to the bot rather than the repo.
        self.self_npub = str(self_npub or "").lower()
        #: The keys this instance answers for, hex. A changes-requested verdict queues work,
        #: and the queue that can push to a PR's branch is the one that opened it: an
        #: external author's PR is left to them (and to whoever else watches the repo).
        #: Empty is no filter -- a deployment with no identity to compare against acts on
        #: what it reads.
        speaks_for = owns_from(config, self.self_npub)
        self.owns = frozenset(key for key in (as_hex(candidate) for candidate in speaks_for)
                              if key)
        for candidate in speaks_for:
            if not as_hex(candidate):
                log.warning("ngit: owns: %r is not a pubkey; this instance does not speak for"
                            " it", str(candidate)[:24])
        self.verdicts = verdicts
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
            if not self.maintainers:
                report.notes.append(
                    "no maintainers known: merge/close statuses are ignored (add "
                    "ngit.maintainers or announce the repository with maintainers)")
            if not self.reviewers:
                report.notes.append(
                    "no reviewers configured: verdicts are read from nobody and no card is "
                    "finished by one (set ngit.reviewers)")
            try:
                self._read_prs(report, since=since, dry_run=dry_run)
            except NgitError as exc:
                report.errors.append(f"prs: {exc}")
        # Mentions last: the roots it reads are the links the two passes above have just
        # written, so a comment on an issue first seen this pass is already attributable.
        if self.config.mentions:
            try:
                self._mentions(report, dry_run=dry_run)
            except NgitError as exc:
                report.errors.append(f"mentions: {exc}")
        # Delivery is its own step, after both directions have decided what to say: what is
        # in the queue is what gets sent, whether it was decided this pass or three passes
        # ago, and the relay being down cannot hold a queued notice back.
        self._deliver(report, dry_run=dry_run)
        return report

    def reconcile(self, *, dry_run: bool = False) -> ReconcileReport:
        """Apply the verdicts a pass can no longer see, to the cards that are parked on them.

        A verdict is read from the PR root by the label pass, which walks what the poll
        carried -- so a verdict already behind the cursor is never read again. A card parked
        before the bridge applied verdicts is therefore stranded for ever, and every later
        fix to that path is unhealable in production: the cards it was written for are
        exactly the ones the pass will never see.

        This pass asks the PR root again, for every card that is `blocked` with the bridge's
        own `Verdict:` hand-off. Nothing else is a candidate: a card blocked on a person's
        question matches no marker, so it is not listed and not moved.

        What the verdict earns is the rule the label pass already uses (`review_labels`):

        * in force and `approved` -- the card is finished, the verdict as its result; the
          merge stays the operator's;
        * in force and `changes-requested` -- the fix card its verdict asks for is queued
          (or found), and the review card stays parked on the verdict it answered;
        * readable but not in force, because the PR carries a newer tip than the one
          reviewed -- the card goes back to the review queue for the tip the PR carries,
          which is what a revision reopens it for. Finishing it on a superseded verdict, or
          re-requesting a fix the implementer already pushed, would both be wrong.

        Idempotent by construction: every outcome is a card that is `done` or `ready`, and
        neither is a candidate on the next run. A dry run decides every row and writes
        nothing -- no status, no comment, no link note.
        """
        report = ReconcileReport(channel=self.target.conversation, repo=str(self.target.repo),
                                 board=self.target.board, address=self.target.address,
                                 dry_run=dry_run)
        if self.state is None:
            report.errors.append(
                "no relay reader is configured, so a verdict cannot be read"
                " (the bridge reads verdicts only when it publishes PR labels)")
            return report
        candidates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            pr_id = str(link.get("source_id") or "")
            if str(link.get("state") or "") in ("merged", "closed") or _detail(link).get("merge"):
                continue  # applied or closed is final: nothing to reconcile
            task_id = self._review_anchor(link)
            card = self.board.task(task_id) if task_id else None
            if card is None or str(card.get("status") or "") != BLOCKED:
                continue
            if self.board.park_marker(task_id) != VERDICT_MARK:
                continue  # a person's question, or a fix's own park: not this pass's
            candidates.append((link, card))
        if not candidates:
            report.notes.append("no card is parked on a verdict")
            return report
        pr_ids = [str(link.get("source_id") or "") for link, _ in candidates]
        try:
            reads = self.state(pr_ids)
        except Exception as exc:  # noqa: BLE001 - a reader may raise; the pass may not die
            report.errors.append(f"verdict read: {type(exc).__name__}: {exc}"[:200])
            return report
        for problem in reads.errors:
            report.errors.append(f"verdict read: {problem}"[:200])
        for link, card in candidates:
            pr_id = str(link.get("source_id") or "")
            try:
                self._reconcile_card(link, card, reads.prs.get(pr_id), report, dry_run=dry_run)
            except (BoardError, sqlite3.Error) as exc:
                report.errors.append(
                    f"card {card.get('id')} for PR {pr_id[:8]}: {exc}")
        return report

    def _reconcile_card(self, link: dict[str, Any], card: dict[str, Any],
                        read: Optional[PrRead], report: ReconcileReport, *,
                        dry_run: bool) -> None:
        """One parked card: read the verdict again, decide, and (unless a dry run) apply.

        The same anchor the label pass builds, from the same stored tip, so the two cannot
        disagree about whether a verdict covers the tip the PR carries. The card's own
        status is the state: a `Verdict:` park is the only thing this touches, and every
        transition leaves a status nothing here reads again.
        """
        pr_id = str(link.get("source_id") or "")
        task_id = str(card.get("id") or "")
        detail = _detail(link)
        verdict = read.verdict if read is not None else None
        if verdict is None:
            report.notes.append(
                f"card {task_id} (PR {pr_id[:8]}): the PR root carries no verdict; left parked")
            return
        if not verdict.readable:
            report.notes.append(
                f"card {task_id} (PR {pr_id[:8]}): the newest verdict"
                f" ({verdict.event_id or '?'}) states a verdict that cannot be read;"
                " left parked")
            return
        tip = str(detail.get("tip") or detail.get("commit") or "")
        tip_at = int(detail.get("tip_at") or 0)
        anchor = anchor_verdict(detail, verdict, tip=tip, tip_at=tip_at)
        if anchor is None:
            report.notes.append(
                f"card {task_id} (PR {pr_id[:8]}): no verdict could be anchored; left parked")
            return
        desired = review_labels(verdict=anchor, review_card=True, tip=tip, tip_at=tip_at,
                                labelled=bool(detail.get("label") or ()))
        in_force = tuple(desired or ()) == (anchor.label,)
        short = anchor.event_id[:8] if anchor.event_id else "an unnamed event"
        where = f" at {tip[:12]}" if tip else ""
        if in_force and anchor.label == APPROVED:
            row = ReconcileRow(
                task_id=task_id, pr_id=pr_id, status=BLOCKED, verdict=anchor.label,
                verdict_event=anchor.event_id, to=DONE,
                why=f"approved by comment {short}{where}; the merge stays the operator's")
        elif in_force:
            queued = self._fix_queued(pr_id, anchor.event_id)
            if queued is not None:
                # The verdict has already been carried out: the review card stays parked on
                # it (a revision reopens it) and parking it again is not news.
                report.notes.append(
                    f"card {task_id} (PR {pr_id[:8]}): changes requested by comment {short}"
                    f" are already queued as {queued}; nothing to move")
                return
            row = ReconcileRow(
                task_id=task_id, pr_id=pr_id, status=BLOCKED, verdict=anchor.label,
                verdict_event=anchor.event_id, to="fix card",
                who=(self.config.review_fix_assignee or self.config.assignee or ""),
                why=f"changes requested by comment {short}{where};"
                    " the fix is queued as its own card and the review card waits for the"
                    " revision")
        else:
            row = ReconcileRow(
                task_id=task_id, pr_id=pr_id, status=BLOCKED, verdict=anchor.label,
                verdict_event=anchor.event_id, to=READY,
                who=str(card.get("assignee") or self.config.review_assignee or ""),
                why=f"the verdict ({anchor.label}, comment {short}) was given for an earlier"
                    f" tip and the PR now carries {tip[:12] or 'a newer tip'};"
                    " back to the review queue for the tip it carries")
        if dry_run:
            report.cards.append(row)
            return
        if in_force:
            outcome, _wrote = self._apply_verdict(pr_id, card, verdict, anchor, detail,
                                                  link=link, report=report)
            if outcome != (APPLIED_APPROVED if anchor.label == APPROVED else APPLIED_CHANGES):
                report.notes.append(
                    f"card {task_id} (PR {pr_id[:8]}): the board did not take the"
                    " transition; the next run decides from what it finds")
                return
        else:
            brief = self._review_brief(card, detail, pr_id=pr_id, tip=tip)
            if not self.board.reopen(
                    task_id, author="ngit", statuses=(BLOCKED,), body=brief,
                    comment=f"Verdict: {anchor.label} (comment {short}) was given for an"
                            f" earlier tip. The PR now carries {tip[:12] or 'a newer tip'},"
                            " so this card is back in the review queue for the tip it carries."):
                report.notes.append(
                    f"card {task_id} (PR {pr_id[:8]}): the card did not move;"
                    " the next run decides from what it finds")
                return
        # The anchor is what says which verdict the card answered -- the same record the
        # label pass keeps, written here so a later pass cannot re-read this one as pending.
        detail["tip"], detail["tip_at"] = tip, tip_at
        detail.update(anchor_detail(anchor))
        self._save_detail(link, detail, task_id=task_id)
        report.cards.append(row)
        log.info("ngit: reconciled %s for PR %s: %s -> %s", task_id, pr_id[:8],
                 row.status, row.to)

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
        # A waiting link is a decision, not a lost task: the issue was seen and no trusted
        # `ready` label was on it. It must fall through to the label check, not to the
        # deleted-by-hand branch.
        waiting = bool(link is not None and task is None and link.get("state") == "waiting")

        if link is not None and task is None and not waiting:
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
        # -- the ready gate -------------------------------------------------
        # A card is created when a trusted npub has labelled the issue `ready`, not when the
        # issue's own author is trusted. The label is the authorisation even for a trusted
        # author's issue: filing is not asking for work, and "add it to the board" is a thing
        # a person says once, in the repo, where the contributor can see it.
        if self.config.ready_label:
            if not self._ready(issue, report, dry_run=dry_run):
                if waiting:
                    report.issues["existing"] += 1
                    return "waiting"
                if not allow_create:
                    report.issues["deferred"] += 1
                    return "deferred"
                if dry_run:
                    report.issues["would_wait"] += 1
                    return "would_wait"
                # The link, not a task: this row is how the next pass remembers the issue has
                # been seen and is waiting, so the channel is told once and not every ten
                # minutes. Its comments are copied when (if) the card is created.
                self.store.ngit_link_save(
                    key, KIND_ISSUE, issue.hex, board=self.target.board, state="waiting",
                    detail={"nevent": issue.id, "subject": issue.subject,
                            "author": issue.author})
                self._enqueue(KIND_ISSUE, issue.hex, "waiting",
                              waiting_message(issue, self.config.ready_label))
                report.issues["waiting"] += 1
                return "waiting"
            trusted = True
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

    def _ready(self, issue: Issue, report: Report, *, dry_run: bool) -> bool:
        """Whether a trusted npub has labelled this issue with `ngit.ready_label`.

        The label is read from the repo's relays, not from `ngit issue view`: the CLI shows a
        label's value and not who wrote it, and "who" is the whole question. A relay that
        could not be read is a `no` with an error line -- queueing on a read that did not
        happen would be the one failure this gate cannot recover from.
        """
        if self.state is None:
            report.errors.append(
                "ready label: no relays to read labels from (ngit.relays or relays.read)")
            return False
        try:
            reader = getattr(self.state, "issue_labels", None)
            if callable(reader):
                labels = reader([issue.hex]).get(issue.hex, ())
            else:  # a reader that only answers verdicts: use the `#t` labels it does carry
                read = self.state([issue.hex])
                for error in read.errors:
                    report.errors.append(f"issue {issue.id[:12]}: {error}")
                labels = read.prs.get(issue.hex, PrRead()).labels
        except Exception as exc:  # noqa: BLE001 - a read failure is a report line
            report.errors.append(f"issue {issue.id[:12]}: labels unreadable ({exc})"[:200])
            return False
        wanted = self.config.ready_label
        for carried in labels:
            if carried.value != wanted:
                continue
            if self.control.is_trusted(carried.author, TRUSTED_PLANE):
                return True
        return False

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
        issue_author = as_hex(issue.author)
        for comment in sorted(issue.comments, key=lambda c: (c.created_at, c.hex or c.id)):
            if not comment.hex or self.store.ngit_comment_seen(key, ref.hex, comment.hex):
                continue
            author = as_hex(comment.author)
            # A comment is copied onto the card as instruction-bearing text, so it comes
            # from a trusted npub or from the issue's own author -- the two people the
            # issue's authorisation actually covers. Anyone else's words stay on the relay.
            if not author or (author != issue_author
                              and not self.control.is_trusted(comment.author, TRUSTED_PLANE)):
                report.issues["comments_skipped"] += 1
                if not dry_run:
                    self.store.ngit_note_comment(key, ref.hex, comment.hex, task_id)
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

    # -- mentions --------------------------------------------------------

    def _mentions(self, report: Report, *, dry_run: bool) -> None:
        """Turn a comment that names this gateway into one reply task.

        A mention is a NIP-27 `p` tag naming this key on a kind-1111 comment whose root is an
        issue or PR the bridge has linked -- the same read `read` uses for verdicts, kept
        unfolded because the `p` tags are the whole signal. Only a trusted npub's mention
        becomes work: a stranger can already file an issue, and a stranger's mention must not
        be a second way to spend a worker turn. Every mention is remembered, refused ones
        included, so the decision is made once and a re-poll cannot re-litigate it.
        """
        key = self.target.key
        info: dict[str, tuple[str, dict[str, Any]]] = {}
        for row in self.store.ngit_links(key, KIND_ISSUE):
            info[str(row["source_id"])] = ("issue", _detail(row))
        for row in self.store.ngit_links(key, KIND_PR):
            info[str(row["source_id"])] = ("PR", _detail(row))
        roots = list(info)[:MAX_MENTION_ROOTS]
        if not roots or not self.self_npub:
            return
        if self.state is None:
            report.notes.append("mentions: no relays to read comments from")
            return
        read_comments = getattr(self.state, "comments", None)
        if not callable(read_comments):
            report.notes.append("mentions: this reader cannot supply raw comments")
            return
        try:
            grouped, _relays = read_comments(roots)
        except Exception as exc:  # noqa: BLE001 - a read failure is a report line
            report.errors.append(f"mentions: {type(exc).__name__}: {exc}"[:200])
            return
        budget = max(1, int(self.config.max_new or 1))
        me = npub_of(self.self_npub)
        for root, events in grouped.items():
            kind, note = info.get(root, ("issue", {}))
            for event in sorted(events, key=lambda e: (int(e.get("created_at") or 0),
                                                       str(e.get("id") or ""))):
                comment_id = str(event.get("id") or "")
                if not comment_id or not self._mentions_me(event):
                    continue
                report.mentions["seen"] += 1
                author = npub_of(str(event.get("pubkey") or ""))
                if not author or author == me:
                    continue                    # our own comment is not a mention to answer
                if self.store.ngit_mention(key, comment_id) is not None:
                    report.mentions["existing"] += 1
                    continue
                if not self.control.is_trusted(author, TRUSTED_PLANE):
                    if not dry_run:
                        self.store.ngit_mention_save(key, comment_id,
                                                     detail={"author": author, "root": root,
                                                             "why": "untrusted author"})
                    report.mentions["ignored"] += 1
                    continue
                if budget <= 0:
                    report.mentions["deferred"] += 1
                    continue
                if dry_run:
                    report.mentions["would_create"] += 1
                    continue
                task_id = self.board.create(
                    title=mention_title(kind, str(note.get("subject") or ""), author),
                    body=mention_body(kind=kind, root=root,
                                      root_nevent=str(note.get("nevent") or root),
                                      comment=event, target=self.target,
                                      secret_file=self.config.reply_secret_file),
                    assignee=self.config.mention_assignee or self.config.assignee,
                    status="ready", idempotency_key=f"ngit:mention:{comment_id}",
                    anchor=self.anchor, priority=self.config.priority)
                self.store.ngit_mention_save(key, comment_id, task_id=task_id,
                                             detail={"author": author, "root": root,
                                                     "kind": kind})
                budget -= 1
                report.mentions["created"] += 1

    def _mentions_me(self, event: Mapping[str, Any]) -> bool:
        """True when a comment names this gateway: a `p` tag, or its key in the words.

        The tag is the NIP-27 spelling and what gitworkshop writes for an `@npub` mention.
        The text fallback exists because a client that dropped the tag still leaves the
        `nostr:npub…` reference in the body, and answering a person's question matters more
        than insisting on their client's correctness.
        """
        for tag in event.get("tags") or ():
            if (isinstance(tag, (list, tuple)) and len(tag) >= 2 and str(tag[0]) == "p"
                    and str(tag[1]).lower() == self.self_npub):
                return True
        body = str(event.get("content") or "").lower()
        return bool(body) and (self.self_npub in body or npub_of(self.self_npub) in body)

    # -- pull requests ---------------------------------------------------

    def _pr_author_trusted(self, author: str) -> bool:
        """Whether this PR's author may have a review card without a human's yes.

        Two keys qualify: one the control pane trusts, and one the repository itself lists
        as a maintainer. The second is not a widening for its own sake -- a maintainer can
        merge the repository, and the ordinary worker PR is authored by the bot that holds
        that membership -- while a stranger who merely tags the repository is neither.
        """
        who = as_hex(author)
        if not who:
            return False
        return who in self.maintainers or self.control.is_trusted(author, TRUSTED_PLANE)

    def _read_prs(self, report: Report, *, since: int | None, dry_run: bool) -> None:
        """Decide what the channel should hear about, and queue it.

        Deciding and sending are separate on purpose: a poll's job is to get the news into
        the queue, and the queue survives the poll. That is what lets the cursor move past
        events whose notice has not gone out yet -- the notice is not lost, it is waiting.
        """
        if self.prs is None:
            raise NgitError("no PR source configured")
        start = self._since(since)
        feed = self.prs(start)
        # A status that names the PR by `e` and carries no repository `a` -- what
        # `ngit pr close` publishes -- cannot pass the `#a` filter above. Read the open
        # PRs' statuses by id and fold them in: attribution is by id already, so a status
        # for a PR this board does not link changes nothing.
        feed = self._with_statuses(feed, report)
        # What the incremental poll itself carried. Only these events may move the `prs`
        # cursor: the sweep's are older by construction, and stepping the cursor over an
        # event the poll never read is the very defect the sweep exists to heal.
        polled_merges = {merge.pr_id for merge in feed.merges}
        polled_closed = {stopped.pr_id for stopped in feed.closed}
        swept = self._sweep(report, dry_run=dry_run)
        if swept is not None:
            feed = with_statuses(feed, swept)
        # Then the trust rule, on everything the pass is about to act on: an applied status
        # is a maintainer's, a close a maintainer's or the PR author's, a revision the
        # author's alone. A relay is not a trust boundary, so this is checked here as well
        # as in the filter. Refused events are returned as timestamps: they are dealt with,
        # as refusals, so the cursor may move past them.
        feed, refused = self._authorised_feed(feed, report)
        merges = feed.merges
        applied = {merge.pr_id: merge for merge in merges}
        closed = {stopped.pr_id: stopped for stopped in feed.closed}
        # The record first: a PR a kind-1631 names is applied and nothing else is asked. For
        # the rest the checkout is asked whether the proposal's content is already on the
        # base branch, because `ngit pr apply` replays its patches as new commits: the tip a
        # status would have named is never an ancestor, and on this repository the status is
        # often never published at all. A PR found this way joins `merges`, so the notice,
        # the card completion and the guard release take the same path a recorded merge does.
        found, open_why = self._applied_by_content(applied, feed.prs, report, dry_run=dry_run)
        if found:
            merges = [*merges, *found]
            applied.update({merge.pr_id: merge for merge in found})
        # A re-push that a client published as a second proposal is the same work, not a
        # second PR: fold it onto the root the bridge already has, as a revision. The feed
        # the rest of the pass sees then has one entry per PR root, which is what makes
        # "one card per PR" enforceable at all.
        prs, repeats = self._fold_repeats(feed.prs, applied,
                                          {stopped.pr_id for stopped in feed.closed})
        feed = PrFeed(prs=prs, merges=merges,
                      revisions=newest_revisions([*feed.revisions, *repeats]),
                      closed=feed.closed)
        budget = max(1, int(self.config.max_prs or 1))
        if not dry_run:
            self._regate_ready_prs(report)
            self._requeue_stranded(report)
        report.prs["seen"] += len(feed.prs) + len(repeats)
        #: Events this poll dealt with (reported, queued, or deliberately passed over), and
        #: ones it only saw. The cursor may move past the first set and never the second.
        settled: list[int] = list(refused)
        deferred: list[int] = []

        for pr in sorted(feed.prs, key=lambda p: (p.created_at, p.id)):
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
            stopped = closed.get(pr.id)
            budget -= 1
            settled.append(pr.created_at)
            if dry_run:
                report.prs["would_post"] += 1
                continue
            trusted = self._pr_author_trusted(pr.author)
            # The task first, the message second: the message names it. A board that cannot
            # be written must not cost the channel its notice, so the failure comes back as
            # a line in the notice and is retried -- without a second message -- later.
            task_id, problem = ("", "")
            if merge is None and stopped is None and self.config.reviews:
                task_id, problem = self._review_task(pr, report, dry_run=dry_run,
                                                     trusted=trusted)
            if merge is not None:
                text = merged_message(pr, merge)
            elif stopped is not None:
                text = closed_message(pr, stopped)
            elif trusted:
                text = ready_message(pr, review_task=task_id, problem=problem)
            else:
                report.prs["held"] += 1
                text = pr_question_message(pr, task_id, self.target, self.config.assignee)
            # `pending` is the lifecycle of a notice that has not been confirmed yet; the row
            # is its claim, and only a daemon receipt moves the link past it. The review
            # outcome is written here, with the claim: saving the link again afterwards would
            # drop `task_error` and lose the retry that makes the task.
            self.store.ngit_link_save(
                self.target.key, KIND_PR, pr.id, board=self.target.board, state="pending",
                task_id=task_id or None, detail=pr_detail(pr, task_error=problem))
            notice = ("merged" if merge is not None else
                      "closed" if stopped is not None else
                      "ready" if trusted else "question")
            self._enqueue(KIND_PR, pr.id, notice, text)

        for merge in sorted(merges, key=lambda m: (m.created_at, m.pr_id)):
            link = self.store.ngit_link(self.target.key, KIND_PR, merge.pr_id)
            if link is None:
                # A merge for a PR this bridge never announced is not replayed into the
                # channel: it narrates what it watched, not the repository's history. The
                # sweep re-reads the same status every pass, so it never moves the cursor.
                if merge.pr_id in polled_merges:
                    settled.append(merge.created_at)
                continue
            if budget <= 0:
                report.prs["deferred"] += 1
                if merge.pr_id in polled_merges:
                    deferred.append(merge.created_at)
                continue
            known = next((pr for pr in feed.prs if pr.id == merge.pr_id), None)
            note = _detail(link)
            nevent = str(note.get("nevent") or "")
            if known is None and note.get("subject"):
                known = PullRequest(id=merge.pr_id, author=str(note.get("author") or ""),
                                    subject=str(note.get("subject") or ""),
                                    branch=str(note.get("branch") or ""), nevent=nevent,
                                    commit=str(note.get("commit") or ""))
            text = merged_message(known, merge, nevent)
            budget -= 1
            if merge.pr_id in polled_merges:
                settled.append(merge.created_at)
            if dry_run:
                report.prs["would_post"] += 1
                continue
            # The merge is written on the row before the notice is queued: `applied` is
            # final, and a revision that arrives while the notice is still undelivered
            # must not reopen the card the merge has just closed.
            if merge.commit and str(note.get("merge") or "") != merge.commit:
                note["merge"] = merge.commit
                self._save_detail(link, note, task_id=str(link.get("task_id") or ""))
            self._enqueue(KIND_PR, merge.pr_id, "merged", text)
            # The merge closes the loop: every card about this PR is done, with the merge
            # named on it. A task already completed is a no-op, and this runs even for a
            # link already `merged`, so one that could not be closed on a locked board
            # closes later.
            self._close_pr_cards(known, merge, link, report, dry_run=dry_run)

        # A close is not a merge: the card cannot be finished by it. It is blocked for a
        # person with the closing event named, and its guard refuses a completion that
        # would otherwise read as work delivered.
        for stopped in sorted(feed.closed, key=lambda c: (c.created_at, c.pr_id)):
            link = self.store.ngit_link(self.target.key, KIND_PR, stopped.pr_id)
            if link is None:
                if stopped.pr_id in polled_closed:
                    settled.append(stopped.created_at)
                continue
            note = _detail(link)
            if str(link.get("state") or "") == "merged" or note.get("merge"):
                # Applied is final: a late or stale close cannot undo a merge that landed.
                if stopped.pr_id in polled_closed:
                    settled.append(stopped.created_at)
                continue
            if budget <= 0:
                report.prs["deferred"] += 1
                if stopped.pr_id in polled_closed:
                    deferred.append(stopped.created_at)
                continue
            budget -= 1
            if stopped.pr_id in polled_closed:
                settled.append(stopped.created_at)
            if dry_run:
                report.prs["would_close"] += 1
                continue
            # A queued "ready for review" line for a PR that has since been closed is
            # noise: nothing is being hidden, the line never landed.
            self.store.ngit_notice_drop(self.target.key, stopped.pr_id)
            self.store.ngit_link_state(self.target.key, KIND_PR, stopped.pr_id, "closed")
            self._close_pr_cards_closed(stopped, link, report, dry_run=dry_run)

        # A revision reopens the PR's card, one comment naming the new tip. It runs after
        # the merge loop so an applied PR is never reopened, and before the labels so the
        # label walk reads the card in the state this pass leaves it.
        self._revisions(feed, report, dry_run=dry_run)
        settled.extend(revision.created_at for revision in feed.revisions)

        # The other end of the fix hand-off: a worker that pushed and parked says where it
        # left the branch, and the card goes back to the review queue. Before the guards so
        # the reopened card is the one they see.
        self._released_fixes(report, dry_run=dry_run)

        # The guard follows the link: open PRs refuse completion, applied ones allow it,
        # closed ones refuse it and name the closing event. Last, so it sees the state this
        # pass actually left the cards in -- and `open_why` so its refusal names the evidence.
        self._guards(feed, report, dry_run=dry_run, why=open_why)

        # The verdict goes on the PR before the label is read: the label is derived from the
        # PR root's newest verdict, so publishing first is what lets one pass carry a card's
        # verdict all the way to the label where the relays let it; the next pass does when
        # they do not, because the card is still there to read.
        self._verdicts(report, dry_run=dry_run)

        # Labels last: every task the poll just made or closed is state the label walk has
        # to see, and a label that cannot be published must not hold the cursor -- the next
        # poll retries it from the link, which still records the label it last published.
        if not dry_run:
            self._review_states(feed, report)

        # The cursor promises that everything before it has been dealt with. A queued notice
        # counts: it is durable, and the next poll delivers it. A deferred event does not --
        # it was seen and not decided on -- so the cursor stops short of the earliest one and
        # the next poll reads it again. A review task that could not be made holds the cursor
        # for the same reason: the retry is what makes the task, not a later event.
        if not dry_run and settled and not report.prs["review_errors"] and not report.failed:
            edge = min(max(settled), *deferred) if deferred else max(settled)
            if edge - CURSOR_OVERLAP_S > start:
                self.store.ngit_set_cursor(self.target.key, "prs", edge - CURSOR_OVERLAP_S)

    def _with_statuses(self, feed: PrFeed, report: Report) -> PrFeed:
        """Fold in the applied/closed statuses that name a linked PR by `e` alone.

        A failure here must not cost the pass its ordinary read: the `#a` feed still
        carries every status that has a repository tag, and the retry needs no cursor,
        because this read is not bounded by one.
        """
        if self.statuses is None:
            return feed
        known = self._status_pr_ids()
        if not known:
            return feed
        try:
            extra = self.statuses(known)
        except Exception as exc:  # noqa: BLE001 - a read that failed is a report line
            report.errors.append(
                f"pr statuses: {type(exc).__name__}: {exc}"[:200])
            return feed
        return merge_feeds(feed, extra)

    def _status_pr_ids(self) -> list[str]:
        """The PR ids whose status is still open to change: linked, applied and not closed.

        Applied and closed are final for the board, so a status about a finished PR can
        change nothing and is not asked for. Rows come oldest first; a board with more open
        PRs than the cap asks about the newest, which are the ones news is about.
        """
        ids: list[str] = []
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            if str(link.get("state") or "") in ("merged", "closed"):
                continue
            if _detail(link).get("merge"):
                continue
            source = str(link.get("source_id") or "")
            if event_hex(source):
                ids.append(event_hex(source))
        return ids[-MAX_STATUS_PRS:]

    def _sweep(self, report: Report, *, dry_run: bool) -> PrFeed | None:
        """Re-read the two PR status kinds over a trailing window. Best effort, every pass.

        The cursor is a watermark on what a poll *reached*, not proof that every event before
        it was delivered. A kind-1631/1632 that reaches a relay after the poll whose window
        covered its timestamp sits behind the cursor for good: no later pass reads it, the link
        stays `signalled`, and the card waits for a merge or a close the bridge can never again
        observe. Reading the status kinds again over a trailing window is cheap -- a repository
        publishes a handful a week -- and every transition they drive is idempotent: a close on
        a `merged` link is skipped, a second close writes the same state, and a card already
        blocked is not blocked again.

        Status kinds only, and `prs`/`revisions` are never taken from here: the sweep must not
        become a second chance to announce a PR, because an old proposal would then produce a
        second card and a second channel message.

        A slow or refusing relay costs the pass a report line, not its news, so this never
        raises. Its own cursor (`SWEEP_CURSOR`) records when it last read through -- that is
        what covers a pass that was down for longer than the window -- and it is deliberately
        not the `prs` cursor: an event the sweep reported is not an event the poll delivered.
        """
        if self.sweep_source is None:
            return None
        hours = max(0, int(self.config.sweep_hours or 0))
        if hours <= 0:
            return None
        now = int(self._now())
        report.sweep_hours = hours
        floor = now - hours * 3600
        stored = self.store.ngit_cursor(self.target.key, SWEEP_CURSOR)
        if stored and str(stored).isdigit():
            floor = min(floor, int(stored))
        try:
            feed = self.sweep_source(max(0, floor))
        except Exception as exc:  # noqa: BLE001 - best effort: a report line, not an exception
            message = str(exc) if isinstance(exc, NgitError) else f"{type(exc).__name__}: {exc}"
            report.notes.append(f"sweep: {message}"[:200])
            log.warning("ngit: status sweep failed: %s", message)
            return None
        report.prs["swept"] += len(feed.merges) + len(feed.closed)
        if not dry_run:
            self.store.ngit_set_cursor(self.target.key, SWEEP_CURSOR, now)
        return PrFeed(merges=feed.merges, closed=feed.closed)

    def _authorised_feed(self, feed: PrFeed, report: Report) -> tuple[PrFeed, list[int]]:
        """Drop status and revision events whose author is not allowed to publish them.

        The relay read is already narrowed where it can be, but a relay is not a trust
        boundary and a test or another reader may hand the pass an unfiltered feed. This is
        the second check, at the point the event's own `pubkey` is attached to the object
        the rest of the pass acts on:

        * an applied status (1631) is a maintainer's;
        * a closed status (1632) is a maintainer's or the PR author's;
        * a revision (1619) is the PR author's alone.

        Refused events are returned as timestamps so the cursor may move past them: they
        are dealt with (as refusals), not unseen. A note names each refusal once.
        """
        refused: list[int] = []

        def author_of(pr_id: str) -> str:
            for pr in feed.prs:
                if pr.id == pr_id:
                    return as_hex(pr.author)
            link = self.store.ngit_link(self.target.key, KIND_PR, pr_id)
            return as_hex(str(_detail(link).get("author") or "")) if link else ""

        def refuse(kind: str, pr_id: str, author: str, created_at: int) -> None:
            report.prs["ignored"] += 1
            report.notes.append(
                f"ignored a {kind} for PR {pr_id[:8]} from {npub_of(as_hex(author)) or author}"
                ": the author is not allowed to publish it")
            refused.append(int(created_at or 0))

        merges: list[Merge] = []
        for merge in feed.merges:
            author = as_hex(merge.author)
            if author and author in self.maintainers:
                merges.append(merge)
            else:
                refuse("merge status", merge.pr_id, merge.author, merge.created_at)
        closed: list[Closed] = []
        for stopped in feed.closed:
            author = as_hex(stopped.author)
            owner = author_of(stopped.pr_id)
            if author and (author in self.maintainers or (owner and author == owner)):
                closed.append(stopped)
            else:
                refuse("close status", stopped.pr_id, stopped.author, stopped.created_at)
        revisions: list[Revision] = []
        for revision in feed.revisions:
            author = as_hex(revision.author)
            owner = author_of(revision.pr_id)
            if author and owner and author == owner:
                revisions.append(revision)
            else:
                refuse("revision", revision.pr_id, revision.author, revision.created_at)
        return replace(feed, merges=merges, closed=closed, revisions=revisions), refused

    def _regate_ready_prs(self, report: Report) -> None:
        """Hold a PR card that was queued before its author was checked.

        A deployment that upgrades into the author gate can still hold cards made by the
        old one. This is the conservative migration: only a card nobody has started
        (`ready`) is moved to `blocked` and asked about, and only when its author is not
        trusted. A running card is a worker mid-review -- interrupting it would be a
        different decision -- and a card already in review/done is left to its verdict and
        the merge route.
        """
        if not self.config.reviews:
            return
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            if str(link.get("state") or "") != "signalled":
                continue
            note = _detail(link)
            pr_id = str(link.get("source_id") or "")
            author = str(note.get("author") or "")
            if not pr_id or not author or self._pr_author_trusted(author):
                continue
            task_id = self._review_anchor(link)
            task = self.board.task(task_id) if task_id else None
            if task is None or str(task.get("status") or "") != "ready":
                continue
            if self.store.ngit_notice_queued(self.target.key, pr_id):
                continue
            try:
                self.board.block(
                    task_id, kind="dependency", author="ngit",
                    comment=f"Held: {author} is not a trusted author. Nothing reviews or"
                            " runs this proposal until a trusted npub authorises it"
                            f" (`hng ngit answer {task_id} --yes --by <npub>`).")
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"author gate on {task_id}: {exc}")
                continue
            self.store.ngit_link_state(self.target.key, KIND_PR, pr_id, "held")
            pr = pr_from_note(pr_id, note)
            if not self._enqueue(KIND_PR, pr_id, "question",
                                 pr_question_message(pr, task_id, self.target,
                                                     self.config.assignee)):
                # The queue could not take it; leave the link where a later pass will try
                # the same gate again rather than a held row nobody will ask about.
                self.store.ngit_link_state(self.target.key, KIND_PR, pr_id, "signalled")
                report.errors.append(f"author gate on {task_id}: the question could not be queued")
                continue
            report.prs["held"] += 1
            log.info("ngit: held existing review task %s for PR %s (author not trusted)",
                     task_id, pr_id[:8])

    def _fold_repeats(self, prs: Sequence[PullRequest],
                      applied: dict[str, Merge],
                      closed: set[str] | None = None) -> tuple[list[PullRequest], list[Revision]]:
        """Split proposals into new PRs and repeats of a PR the bridge already has.

        A proposal's identity is its event id, so a re-push that a client published as a
        *second* 1618 -- a rebased branch read as a fresh proposal -- would otherwise be a
        second PR, a second card and a second notice. The branch is the work's identity:
        one author cannot have two live proposals from the same source branch. A proposal
        whose (author, branch) an existing, unapplied link already names is folded onto
        that root as a revision; the oldest link wins, because the 1618 that opened the PR
        is the record. A proposal with no tip names no revision and is left as a PR.

        The key is the branch **as published**: the 1618's `branch-name`, cut by ngit to 60
        characters. A push made from the branch `ngit pr checkout` created publishes
        `<branch>-(<8 hex root id>)` when the source branch is 49 characters or fewer. That
        suffix is stripped, but only when the hex is the event id of the root the branch
        names -- the decoration ngit itself writes -- so a branch genuinely called
        `<branch>-(deadbeef)` stays distinct. The incident's branch was 74 characters, so the
        cut removed its suffix and its two roots carry one name; the short one is pinned by
        `test_a_repush_from_the_checked_out_branch_makes_one_card`.
        """
        closed = closed or set()
        known: dict[tuple[str, str], str] = {}
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            source = str(link.get("source_id") or "")
            if (not source or source in applied or source in closed
                    or str(link.get("state") or "") in ("merged", "closed")):
                # Applied and closed are final: the same author pushing the same branch
                # again afterwards is a new proposal, not a revision of a finished one.
                continue
            note = _detail(link)
            author, branch = str(note.get("author") or ""), str(note.get("branch") or "")
            if author and branch:
                known.setdefault((author, branch), source)
        # The poll's own proposals are folded among themselves too, oldest first: a first
        # poll that carries both a re-push and the proposal it repeats must still make one
        # card, not two that the next poll can no longer untangle.
        for pr in sorted(prs, key=lambda p: (p.created_at, p.id)):
            if pr.branch and pr.commit:
                known.setdefault((pr.author, pr.branch), pr.id)
        fresh: list[PullRequest] = []
        repeats: list[Revision] = []
        for pr in prs:
            root = (_fold_root(known, pr.author, pr.branch, pr_id=pr.id)
                    if pr.branch and pr.commit else None)
            if root and root != pr.id:
                repeats.append(Revision(pr_id=root, commit=pr.commit,
                                        created_at=pr.created_at, event_id=pr.id,
                                        author=as_hex(pr.author)))
                continue
            fresh.append(pr)
        return fresh, repeats

    def _revisions(self, feed: PrFeed, report: Report, *, dry_run: bool) -> None:
        """Reopen the PR's card for a new tip; a revision never creates a card.

        The card is the queue and the PR is the record: one PR root has one card for the
        life of the PR, so a revision re-queues that card (`review`/`done` -> `ready`) with
        one comment naming the new tip and leaves the link row -- and its task id -- where
        they are. A card that is already queued or running is left in place: it is about
        to read, or is reading, the tip the comment names. `applied` is final, and a
        revision older than the tip the row already knows is a replay, not news.
        """
        if not feed.revisions:
            return
        applied = {merge.pr_id for merge in feed.merges}
        closed = {stopped.pr_id for stopped in feed.closed}
        for revision in sorted(feed.revisions, key=lambda r: (r.created_at, r.pr_id)):
            link = self.store.ngit_link(self.target.key, KIND_PR, revision.pr_id)
            if link is None:
                continue  # never announced: the channel was not told, so nothing reopens
            note = _detail(link)
            if (str(link.get("state") or "") in ("merged", "closed")
                    or revision.pr_id in applied or revision.pr_id in closed
                    or note.get("merge")):
                continue  # applied or closed: a late update cannot reopen a finished PR
            tip = str(revision.commit or "")
            if not tip or tip == str(note.get("tip") or ""):
                continue
            if revision.created_at < int(note.get("tip_at") or 0):
                continue  # a replayed older revision must not move the tip back
            report.prs["revisions"] += 1
            note["tip"], note["tip_at"] = tip, int(revision.created_at)
            task_id = self._review_anchor(link)
            task = self.board.task(task_id) if task_id else None
            status = str(task.get("status") or "") if task else ""
            # A card this bridge parked is waiting for the next event, not for a person:
            # `verdict:` is a review the bridge had not finished when the tip moved, and
            # `revision:` is a fix whose worker said where it left it. Any other block is a
            # question, and a revision is not an answer to one.
            reopen = status in (REVIEW, DONE) or (
                status == BLOCKED and self.board.parked_by_bridge(task_id))
            if dry_run:
                if reopen:
                    report.prs["would_reopen"] += 1
                continue
            if task_id and (reopen or status in ("ready", "running")):
                text = revision_note(note, revision)
                brief = self._review_brief(task, note, pr_id=revision.pr_id, tip=tip)
                try:
                    if reopen:
                        if self.board.reopen(task_id, author="ngit", comment=text,
                                             statuses=(REVIEW, DONE, BLOCKED), body=brief):
                            report.prs["reopened"] += 1
                            log.info("ngit: reopened %s for PR %s at %s",
                                     task_id, revision.pr_id[:8], tip[:12])
                        else:
                            # The card moved under us (a worker finished it, a person
                            # archived it, a block turned out to be a question). The tip
                            # stays unrecorded so the next pass decides from the state it
                            # actually finds.
                            continue
                    else:
                        # Queued or running: the card keeps its claim, and the brief moves
                        # back to review because the tip it was told to fix is gone -- the
                        # revision is the fix. A card the bridge did not write keeps its
                        # body: it is not the bridge's instruction to rewrite.
                        if brief:
                            self.board.set_body(task_id, brief)
                        self.board.comment(task_id, "ngit", text)
                except (BoardError, sqlite3.Error) as exc:
                    # The tip is not recorded, so the next pass tries the card again
                    # instead of believing a comment that never landed.
                    report.prs["review_errors"] += 1
                    report.errors.append(f"revision of PR {revision.pr_id[:8]}: {exc}")
                    continue
            # No card to reopen (a PR announced with reviews off, or a task deleted by
            # hand), or one waiting on a human: the tip is still recorded, so the label
            # and any later pass read it.
            self._save_detail(link, note, task_id=task_id)

    def _released_fixes(self, report: Report, *, dry_run: bool) -> None:
        """Put a fixed card back in the review queue: the worker's park is the news.

        A fix round ends with the worker pushing (which publishes the revision) and then
        saying where it left the branch. The push usually lands before the park, so by the
        time the park arrives the revision is already recorded and `_revisions` has nothing
        new to act on. This is the other end of the same hand-off: the PR's review card goes
        back for review, with the review brief, and the next claim reviews the tip the PR
        carries.

        Either card can be the one parked. A fix card parked with `Revision:` is the fix
        itself, done -- it is completed here, because the bridge is the one writer that
        knows a revision answered it -- and the review card is reopened from the same news.
        A review card parked with `Revision:` is the older shape, where the fix and the
        re-review shared one card. Only `FIXED_MARK` qualifies: a `Verdict:` park is healed
        by the verdict pass and by a revision, and anything else is a person's question.
        """
        if dry_run:
            return
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            if str(link.get("state") or "") in ("merged", "closed"):
                continue
            pr_id = str(link.get("source_id") or "")
            note = _detail(link)
            if note.get("merge"):
                continue  # applied is final: a park after the merge is a person's
            tip = str(note.get("tip") or note.get("commit") or "")
            # A fix card the worker parked instead of completing: the revision is the news,
            # and the review card goes back for it even when the park was the only event
            # this pass saw.
            pushed = self._finish_fix_parks(pr_id, tip, report)
            task_id = self._review_anchor(link)
            task = self.board.task(task_id) if task_id else None
            if task is None or str(task.get("status") or "") != BLOCKED:
                continue
            marker = self.board.park_marker(task_id)
            if marker != FIXED_MARK and not (pushed and marker == VERDICT_MARK):
                continue
            brief = self._review_brief(task, note, pr_id=pr_id, tip=tip)
            try:
                if self.board.reopen(
                        task_id, author="ngit", statuses=(BLOCKED,), body=brief,
                        comment="Fix pushed"
                                + (f" at {tip}" if tip else "")
                                + ": this card is back in the review queue for the tip"
                                " the PR carries."):
                    report.prs["reopened"] += 1
                    log.info("ngit: reopened %s after its fix was pushed", task_id)
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"fixed card {task_id} for PR {pr_id[:8]}: {exc}")

    def _finish_fix_parks(self, pr_id: str, tip: str, report: Report) -> bool:
        """Complete every fix card whose worker parked it with `Revision: <tip>`.

        The fix brief asks for `kanban_complete` with the new tip; a worker that could not
        complete parks instead, and the park is the same news. This is the bridge's half of
        that hand-off -- the card's work (a pushed revision) is done, so nothing may claim
        it again. Returns whether any fix was pushed, which is what reopens the review card.
        """
        pushed = False
        for card in self.board.fix_cards(pr_id):
            task_id = str(card.get("id") or "")
            if not task_id or str(card.get("status") or "") != BLOCKED:
                continue
            if self.board.park_marker(task_id) != FIXED_MARK:
                continue
            try:
                if self.board.complete(
                        task_id, author="ngit",
                        result=f"The fix is pushed at {tip or 'a new tip'}; the revision"
                               " reopened the PR's review card.",
                        comment="Fix pushed"
                                + (f" at {tip}" if tip else "")
                                + ": this card's work is the revision, so it is finished."):
                    report.prs["completed"] += 1
                    pushed = True
                    log.info("ngit: completed fix card %s after its fix was pushed", task_id)
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"fix card {task_id} for PR {pr_id[:8]}: {exc}")
        return pushed

    def _review_task(self, pr: PullRequest, report: Report, *, dry_run: bool,
                     trusted: bool = True) -> tuple[str, str]:
        """One review task for this PR, or the reason there is none.

        Keyed `ngit:pr:<event id>`, so a re-poll, a retry after a failed send and a retry
        after a failed create all land on the same task. Never raises: the caller still has
        a channel message to post, and "no task because ..." is a better message than none.

        `trusted` is the author gate. A trusted author's card is `ready`; anyone else's is
        created `blocked`, so the dispatcher cannot start a worker on a proposal whose code
        nobody has authorised. The card still carries the review brief: `hng ngit answer
        --yes` releases it into the same review lane.
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
                assignee=self.config.review_assignee or "default",
                status="ready" if trusted else "blocked",
                idempotency_key=key, anchor=self.anchor, priority=self.config.priority)
        except (BoardError, sqlite3.Error) as exc:
            report.prs["review_errors"] += 1
            report.errors.append(f"review task for PR {pr.id[:8]}: {exc}")
            log.warning("ngit: no review task for %s: %s", pr.id[:8], exc)
            return "", str(exc)[:200]
        if not trusted:
            try:
                self.board.comment(
                    task_id, "ngit",
                    f"Held: {pr.author} is not a trusted author. Nothing reviews or runs"
                    " this proposal until a trusted npub authorises it"
                    f" (`hng ngit answer {task_id} --yes --by <npub>`).")
            except BoardError as exc:
                report.errors.append(f"hold note on {task_id}: {exc}")
            report.prs["tasks"] += 1
            log.info("ngit: review task %s for PR %s is held for an author answer",
                     task_id, pr.id[:8])
            return task_id, ""
        # The card is the PR's, and its PR is open: from this moment completion refuses
        # until a merge is applied. Guarding at creation (rather than only on the next
        # pass) closes the window in which a worker could finish the card first. A guard
        # that cannot be written is reported, not fatal: the card still exists, and the
        # next pass installs the guard from the link.
        try:
            self.board.guard_pr(task_id, pr_id=pr.id, state="open", tip=pr.commit)
        except BoardError as exc:
            report.errors.append(f"guard for review task {task_id}: {exc}")
        report.prs["tasks"] += 1
        log.info("ngit: review task %s for PR %s", task_id, pr.id[:8])
        return task_id, ""

    def _note_review(self, pr: PullRequest, link: Optional[dict[str, Any]], task_id: str,
                     problem: str) -> None:
        """Record the task (or why there is none) on the link, so a later poll can retry.

        Merged into what the row already says, not over it: the same blob carries the label
        state, and replacing it here would drop the event ids a retraction needs.
        """
        detail = {**_detail(link or {}), **pr_detail(pr, task_error=problem)}
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
        trusted = self._pr_author_trusted(pr.author)
        task_id, problem = self._review_task(pr, report, dry_run=dry_run, trusted=trusted)
        if dry_run:
            return
        self._note_review(pr, link, task_id, problem)
        if task_id:
            log.info("ngit: late review task %s for PR %s", task_id, pr.id[:8])

    def _review_anchor(self, link: dict[str, Any]) -> str:
        """The review task that speaks for this PR: the newest one the board holds.

        The link names the first, and every later review is a card the bridge did not make
        -- hand-queued in this deployment, under the title and the brief the bridge writes.
        Reading the stored id alone would keep the first verdict in force for ever, so the
        board is asked which review task is the current one.
        """
        found = self.board.review_task(str(link.get("source_id") or ""))
        return str(found["id"]) if found is not None else str(link.get("task_id") or "")

    def _pr_cards(self, link: dict[str, Any], pr_id: str,
                  nevent: str = "") -> list[dict[str, Any]]:
        """The PR's cards: what the board's own identities find, plus the link's anchor.

        `pr_cards` matches by the bridge's key and the brief's title; the link is the
        authority on which card speaks for the PR even if a person renamed it, so its task
        id (and the newest review card) are unioned in. Archived cards are not returned.
        """
        cards = self.board.pr_cards(pr_id, nevent)
        seen = {str(card.get("id") or "") for card in cards}
        for extra in (self._review_anchor(link), str(link.get("task_id") or "")):
            if not extra or extra in seen:
                continue
            task = self.board.task(extra)
            if task is not None and str(task.get("status") or "") != "archived":
                cards.append(task)
                seen.add(extra)
        return cards

    def _close_pr_cards(self, pr: PullRequest | None, merge: Merge, link: dict[str, Any],
                        report: Report, *, dry_run: bool) -> None:
        """Finish every card this PR owns, naming the merge. Idempotent.

        `done` requires `applied`: the guard is released on each card (the merge commit is
        the proof), and only then is the card completed. A 1631 that names no commit is not
        enough -- a card that says "merged" without saying *what* landed cannot be audited
        -- so the card stays open and the pass says why.

        An applied-without-a-status merge (`Merge.evidence`) says so: the comment names the
        commit, the evidence, and the fact that no status event claims the merge, so a reader
        can check the commit rather than take the board's word for it.

        The set is `_pr_cards`, not the newest review task: one PR root can still have more
        than one card on a board with history (a re-review filed by hand under the brief's
        title, a duplicate from before one-card-per-root), and a merge finishes all of them.
        """
        if dry_run:
            return
        nevent = str(_detail(link).get("nevent") or "")
        if not merge.commit:
            report.notes.append(
                f"PR {merge.pr_id[:8]} is applied without a commit id: its card stays open")
            return
        how = applied_how(merge)
        proof = ("The merge was published as status " + merge.pr_id
                 if not merge.evidence else
                 "No applied status names it: " + merge.evidence)
        what = f'"{pr.subject}"' if pr is not None and pr.subject else f"PR {merge.pr_id}"
        cards = self._pr_cards(link, merge.pr_id, nevent)
        for card in cards:
            task_id = str(card.get("id") or "")
            status = str(card.get("status") or "")
            if not task_id or status in (DONE, "archived"):
                continue
            try:
                self.board.release_guard(task_id, merge_commit=merge.commit,
                                         evidence=merge.evidence)
                if self.board.complete(
                    task_id, author="ngit",
                    comment=f"{what} {how}" + (f" ({nevent})" if nevent else "")
                            + f"\n{proof}; this card is closed by it.",
                    result=f"PR {merge.pr_id} {how}: the card is closed by the merge.",
                ):
                    report.prs["completed"] += 1
                    log.info("ngit: completed %s for merged PR %s", task_id, merge.pr_id[:8])
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"PR card {task_id} for merged PR {merge.pr_id[:8]}: {exc}")
        # A fix card queued by a verdict is work for a PR that has just landed: nobody should
        # claim it, so the merge finishes it here. It is not a PR card -- `pr_cards` excludes
        # it, and it carries no guard -- so it is its own short loop.
        for card in self.board.fix_cards(merge.pr_id):
            task_id = str(card.get("id") or "")
            if not task_id or str(card.get("status") or "") in (DONE, "archived"):
                continue
            try:
                if self.board.complete(
                    task_id, author="ngit",
                    comment=f"{what} {how}; the fix this card queued is moot."
                            + (f" ({nevent})" if nevent else ""),
                    result=f"PR {merge.pr_id} {how}: the fix this card queued is moot.",
                ):
                    report.prs["completed"] += 1
                    log.info("ngit: completed fix card %s for merged PR %s", task_id,
                             merge.pr_id[:8])
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"fix card {task_id} for merged PR {merge.pr_id[:8]}: {exc}")
        self._unblock_premise_cards(merge, nevent, report)

    def _unblock_premise_cards(self, merge: Merge, nevent: str, report: Report) -> None:
        """Release cards whose own words say they were waiting for this PR's merge.

        A card filed "after PR X merges" is blocked on a fact, not on a person: once the
        merge exists the fact is true, and leaving it blocked hides work that is now ready.
        The match is the card's own premise text (`after PR <short> merges`, `blocked on …
        PR <short>`), never a bare mention of the PR: a card that merely talks about it is
        not waiting for it.
        """
        how = applied_how(merge)
        for card in self._premise_cards(merge.pr_id, nevent):
            task_id = str(card.get("id") or "")
            try:
                if self.board.release(
                    task_id, author="ngit",
                    comment=f"PR {merge.pr_id[:8]} {how}: the merge this card waited for is"
                            " applied. The card is released; any human condition in its body"
                            " still stands.",
                ):
                    report.prs["unblocked"] += 1
                    log.info("ngit: released %s after merge of PR %s", task_id,
                             merge.pr_id[:8])
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"premise card {task_id} for PR {merge.pr_id[:8]}: {exc}")

    def _premise_cards(self, pr_id: str, nevent: str = "") -> list[dict[str, Any]]:
        """Blocked cards whose body names this PR as the thing they wait for.

        Two spellings, both the card's own: `after PR <short> merges`, and a `blocked on …`
        line that names the PR *and* a merge. A bare mention of the PR is not a premise --
        a card can talk about a PR it is not waiting for -- so the line has to say both.
        """
        short = re.escape(str(pr_id or "")[:8])
        if not short:
            return []
        after = re.compile(rf"(?i)after\s+PR\s+{short}\b")
        waiting = re.compile(rf"(?i)blocked\s+on[^\n]*PR\s+{short}\b")
        found: list[dict[str, Any]] = []
        for card in self.board.blocked_cards():
            task_id = str(card.get("id") or "")
            # A card whose own work was never authorised is not waiting on a fact -- it is
            # waiting on a person, and its title and body are the stranger's text. Releasing
            # it here would turn an unanswered trust question into queued work (the issue
            # gate's own bypass). Only a link that reached an accepted state may premise.
            link = self.store.ngit_link_for_task(task_id) if task_id else None
            if link is not None and str(link.get("state") or "") in (
                    "pending", "held", "asked", "declined"):
                continue
            text = f"{card.get('title') or ''}\n{card.get('body') or ''}"
            for line in text.splitlines():
                lower = line.lower()
                if "merge" not in lower:
                    continue
                if after.search(line) or (nevent and "after" in lower and nevent in line):
                    found.append(card)
                    break
                if waiting.search(line) or (nevent and "blocked on" in lower
                                            and nevent in line):
                    found.append(card)
                    break
        return found

    def _close_pr_cards_closed(self, stopped: Closed, link: dict[str, Any],
                               report: Report, *, dry_run: bool) -> None:
        """A PR closed without a merge: block its cards for a human, naming the event.

        Decision: a close does **not** complete the card as `closed`. Completion means the
        work landed; a close means it did not, and a person has to decide what happens to
        the work (drop it, reopen the proposal, or file it differently). The guard keeps
        refusing completion with the closing event named.
        """
        if dry_run:
            return
        nevent = str(_detail(link).get("nevent") or "")
        cards = self._pr_cards(link, stopped.pr_id, nevent)
        for card in cards:
            task_id = str(card.get("id") or "")
            status = str(card.get("status") or "")
            if not task_id or status in (DONE, "archived"):
                continue
            try:
                self.board.close_guard(task_id, event_id=stopped.event_id)
                if status == BLOCKED:
                    continue  # already waiting for a person; the guard says why now
                self.board.block(
                    task_id, kind="dependency", author="ngit",
                    comment=f"PR {stopped.pr_id[:8]} was closed without a merge"
                            + (f" (event {stopped.event_id})" if stopped.event_id else "")
                            + ".\nNothing landed, so this card cannot be completed by it."
                            " A person decides: reopen the proposal, re-file the work, or"
                            " drop the card.",
                )
                report.prs["blocked"] += 1
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"PR card {task_id} for closed PR {stopped.pr_id[:8]}: {exc}")
        # A fix card whose PR was closed without a merge: the fix has nowhere to land, so it
        # is a person's decision like every other card on a closed PR -- not queue work.
        for card in self.board.fix_cards(stopped.pr_id):
            task_id = str(card.get("id") or "")
            if not task_id or str(card.get("status") or "") in (DONE, "archived", BLOCKED):
                continue
            try:
                self.board.block(
                    task_id, kind="dependency", author="ngit",
                    comment=f"PR {stopped.pr_id[:8]} was closed without a merge"
                            + (f" (event {stopped.event_id})" if stopped.event_id else "")
                            + ". The fix this card queued has nowhere to land; a person"
                            " decides whether the work is re-filed or dropped.",
                )
                report.prs["blocked"] += 1
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"fix card {task_id} for closed PR {stopped.pr_id[:8]}: {exc}")

    def _applied_by_content(self, record: Mapping[str, Merge],
                            prs: Sequence[PullRequest], report: Report,
                            *, dry_run: bool = False) -> tuple[list[Merge], dict[str, str]]:
        """PRs the record is silent about, whose content the checkout says is already in.

        `ngit pr apply` replays a proposal's patches as **new** commits, so the tip its
        status would name is never an ancestor of the branch it landed on -- and the status
        itself is only published when someone publishes it. Asking the status record alone
        therefore leaves a landed PR reading `open` for ever, and its card refusing for ever.
        So for every PR the record does not finish -- a linked one, or one this poll is
        seeing for the first time -- the checkout is asked (`applied.py`): an applied answer
        becomes a `Merge` with `evidence` set, and a no becomes the sentence the guard's
        refusal prints. The record still wins: nothing here second-guesses a kind-1631.

        The answer is stamped on the link (`applied_check`) with the tip and base it was read
        at, so a pass with no news costs one `rev-parse`, not a probe per PR. A close is not
        an applied record and does not stop the question: a proposal whose content is on the
        branch was applied, and a close that arrived afterwards does not undo it. What a close
        *does* finish is a PR with nothing on the branch -- that stays a person's decision.
        """
        source = self.applied
        if source is None:
            return [], {}
        try:
            head = str(source.head() or "")
        except Exception as exc:  # noqa: BLE001 - a probe may raise; the pass may not die
            report.errors.append(f"applied check: the checkout could not be read: {exc}")
            return [], {}
        found: list[Merge] = []
        why: dict[str, str] = {}
        links = {str(link.get("source_id") or ""): link
                 for link in self.store.ngit_links(self.target.key, KIND_PR)}
        # A PR this poll is seeing for the first time has no link yet; its tip is on the
        # event. Both are asked about, so a proposal that landed before the bridge ever
        # announced it gets the merged line on the first pass instead of a review card.
        candidates: list[tuple[str, str, dict[str, Any] | None]] = []
        for pr_id, link in links.items():
            note = _detail(link)
            if pr_id and pr_id not in record and not note.get("merge"):
                candidates.append((pr_id, str(note.get("tip") or note.get("commit") or ""), link))
        for pr in prs:
            if pr.id and pr.id not in record and pr.id not in links:
                candidates.append((pr.id, str(pr.commit or ""), None))
        for pr_id, tip, link in candidates:
            if not tip:
                why[pr_id] = "no applied status names it, and the PR names no tip to check"
                continue
            stamp = _detail(link).get("applied_check") if link is not None else None
            if (isinstance(stamp, dict) and head
                    and str(stamp.get("tip") or "") == tip
                    and str(stamp.get("base") or "") == head
                    and not stamp.get("applied")):
                why[pr_id] = str(stamp.get("why") or "")
                continue
            try:
                evidence = source.probe(tip)
            except Exception as exc:  # noqa: BLE001 - a probe may raise; the pass may not die
                why[pr_id] = (f"no applied status names it, and its patch could not be"
                              f" checked ({exc})")
                continue
            if evidence.applied and evidence.commit:
                found.append(Merge(pr_id=pr_id, commit=evidence.commit, created_at=0,
                                   evidence=evidence.why))
                report.prs["applied"] += 1
                report.notes.append(
                    f"PR {pr_id[:8]} is applied without a status event: {evidence.why}")
                continue
            why[pr_id] = ("no applied status names it, and " + evidence.why.rstrip(".")
                          if evidence.why else "no applied status names it")
            if link is None or dry_run:
                continue  # nothing to stamp, or a dry run: this pass writes nothing
            note = _detail(link)
            note["applied_check"] = {"tip": tip, "base": head, "applied": False,
                                     "why": why[pr_id]}
            try:
                self._save_detail(link, note, task_id=str(link.get("task_id") or ""))
            except (BoardError, sqlite3.Error) as exc:
                report.errors.append(f"applied check for PR {pr_id[:8]}: {exc}")
        return found, why

    def _guards(self, feed: PrFeed, report: Report, *, dry_run: bool,
                why: Optional[Mapping[str, str]] = None) -> None:
        """Keep the board's `done requires applied` guard in step with the link table.

        One row and one trigger per open PR card; released on a merge, closed on a 1632,
        pruned when the card is gone or finished. `why` is what `_applied_by_content` compared
        before leaving a PR open (or a close with nothing on the branch), and it goes into the
        refusal so the board says *what it checked*, not just that it is refusing. A dry run
        writes nothing.
        """
        if dry_run:
            return
        why = why or {}
        applied = {merge.pr_id: merge for merge in feed.merges}
        closed = {stopped.pr_id: stopped for stopped in feed.closed}
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            pr_id = str(link.get("source_id") or "")
            note = _detail(link)
            nevent = str(note.get("nevent") or "")
            state = str(link.get("state") or "")
            merge = applied.get(pr_id)
            from_note = False
            if merge is None and note.get("merge"):
                # The merge is on the link's own note, not in this poll's feed: the pass that
                # saw it may have failed to release the guard or finish a card (a locked
                # board), and the event is now behind the cursor. The note is the same record,
                # so repair from it rather than leave a card refusing with no event in sight.
                merge = Merge(pr_id=pr_id, commit=str(note.get("merge") or ""))
                from_note = True
            stopped = closed.get(pr_id)
            evidence = str(why.get(pr_id) or "")
            try:
                cards = self._pr_cards(link, pr_id, nevent)
                if merge is not None and merge.commit:
                    if from_note:
                        self._close_pr_cards(None, merge, link, report, dry_run=False)
                    else:
                        for card in cards:
                            self.board.release_guard(str(card["id"]),
                                                     merge_commit=merge.commit,
                                                     evidence=merge.evidence)
                elif stopped is not None or state == "closed":
                    event_id = (stopped.event_id if stopped is not None
                                else str(note.get("closed") or ""))
                    for card in cards:
                        self.board.close_guard(str(card["id"]), event_id=event_id,
                                               evidence=evidence)
                elif state != "merged" and not note.get("merge"):
                    tip = str(note.get("tip") or note.get("commit") or "")
                    for card in cards:
                        if str(card.get("status") or "") in (DONE, "archived"):
                            continue  # finished by a merge or by an approval; prune drops it
                        if str(card.get("status") or "") == BLOCKED \
                                and not self.board.parked_by_bridge(str(card["id"])):
                            # A question, not a hand-off (the author gate): it is not work
                            # in progress, so an open-PR guard here would say the wrong
                            # thing about a card nobody has authorised.
                            continue
                        held = self.board.guard_state(str(card["id"])) or {}
                        if _guard_covers_tip(held, tip):
                            # No news in this window is not evidence the PR is open: the
                            # feed is incremental and a status event can sit behind the
                            # cursor for good. A terminal guard stays terminal -- except an
                            # approval, which is final only while it covers this tip.
                            continue
                        self.board.guard_pr(str(card["id"]), pr_id=pr_id, state="open", tip=tip,
                                            evidence=evidence)
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"guard for PR {pr_id[:8]}: {exc}")
        try:
            self.board.prune_guards()
        except (BoardError, sqlite3.Error) as exc:
            report.errors.append(f"guard prune: {exc}")

    # -- verdicts ---------------------------------------------------------

    def _verdicts(self, report: Report, *, dry_run: bool) -> None:
        """Publish each review card's verdict on its PR, once per verdict comment.

        The card is the queue; the PR is the record. A reviewer writes the verdict on the
        card, and until it is also a kind-1111 comment on the PR, `ngit pr view --comments`
        says nothing about the review, the label pass has nothing to derive a label from, and
        the merge guard has no second signature to check. This is the one writer of that
        event, and it publishes the comment's words exactly as the reviewer wrote them.

        Idempotent on the card and the comment. The link row remembers which comment of which
        card was published as which event, so a quiet poll does nothing; and the published
        event's timestamp is the comment's own, so a retry after a lost acknowledgement signs
        the same event id rather than a second comment. A newer verdict comment on the same
        card is a new statement and is published -- the words are the record.

        A review that was already published by hand is left alone: the publisher reads the
        PR's thread first and treats the same text from the same key as done.
        """
        if self.verdicts is None or not self.config.verdicts:
            return
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            pr_id = str(link.get("source_id") or "")
            detail = _detail(link)
            if (not pr_id or str(link.get("state") or "") in ("merged", "closed")
                    or detail.get("merge")):
                # Applied is final: a verdict after the merge is somebody else's business,
                # and a closed PR has nothing left to approve. The merge may have been
                # recorded earlier in this same pass, before the link state moved.
                continue
            task_id = self._review_anchor(link)
            if not task_id:
                continue
            found = card_verdict(task_id, self.board.comments(task_id))
            if found is None:
                continue
            published = detail.get("verdict_published") or {}
            if (str(published.get("task") or "") == task_id
                    and int(published.get("comment") or 0) == found.comment_id):
                continue
            if dry_run:
                report.prs["would_verdict"] += 1
                continue
            try:
                result = self.verdicts(pr_id, found.body, created_at=found.created_at,
                                       author=str(detail.get("author") or ""),
                                       covers_after=int(detail.get("tip_at") or 0))
            except Exception as exc:  # noqa: BLE001 - a publisher may raise; the pass may not die
                report.prs["verdict_errors"] += 1
                report.errors.append(
                    f"verdict {pr_id[:8]} (task {task_id}): {type(exc).__name__}: {exc}"[:200])
                continue
            if not result.ok:
                report.prs["verdict_errors"] += 1
                report.errors.append(
                    f"verdict {pr_id[:8]} (task {task_id}): {result.error}"[:200])
                continue
            detail["verdict_published"] = {"task": task_id, "comment": found.comment_id,
                                           "event": result.event}
            self._save_detail(link, detail, task_id=task_id)
            report.prs["verdicts"] += 1
            self._enqueue(KIND_PR, pr_id, f"review:{task_id}",
                          review_verdict_message(pr_id, found.label, task_id))
            log.info("ngit: PR %s verdict published from %s as %s",
                     pr_id[:8], task_id, (result.event or "")[:8])

    # -- the verdict's consequence, and the label ---------------------------------

    def _review_states(self, feed: PrFeed, report: Report) -> None:
        """Act on each PR's newest verdict, and publish the one label it carries.

        Two consequences of one read, in the order that keeps them consistent:

        * **the verdict's consequence is work or an end** -- `approved` completes the review
          card; `changes-requested` opens the one fix card its verdict event asks for, on the
          implementer's desk, and leaves the review card parked on the verdict it answered (a
          revision reopens it). The fix belongs to the queue that can push to the PR's branch,
          so a PR authored outside `ngit.owns` queues nothing and the pass says so;
        * the PR carries **one label**, derived from the same verdict: it is the public
          half, and a reader who never opens the board has to be able to check it.

        Both use one rule for "this verdict is in force" (`review_labels`): the anchor's
        tip is the PR's tip. They run together so the label and the card cannot disagree
        about which verdict they answered.

        This pass is the only writer of both. It reads the PR root's newest verdict and the
        labels the PR currently carries off the relays, publishes the label that belongs
        there and retracts every other one -- including a label an earlier writer left,
        which no link row here names. The link row only remembers what this pass published,
        so a poll with no news writes nothing, and a leftover is corrected in one pass
        rather than by hand.
        """
        if self.state is None:
            if self._labeler() is not None:
                report.notes.append(
                    "labels: no relay reader configured, so no label was changed")
            return
        tips = feed.newest_tips()
        # Every proposal the poll carried counts, including one with no `c` tag: the docs
        # promise a new PR gets its label, and a proposal whose branch has no tip named is
        # still a PR waiting for a review.
        active = {pr.id for pr in feed.prs} | {merge.pr_id for merge in feed.merges}
        finished = ({merge.pr_id for merge in feed.merges}
                    | {item.pr_id for item in feed.closed})
        wired = self._labeler() is not None
        pending: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for link in self.store.ngit_links(self.target.key, KIND_PR):
            pr_id = str(link.get("source_id") or "")
            detail = _detail(link)
            if str(link.get("state") or "") in ("merged", "closed", "held", "asked",
                                                "declined"):
                # A merge or a close leaves the label it was decided on; a PR held for an
                # author answer has no review state to publish and acting on a verdict
                # would finish work nobody authorised.
                continue
            if str(link.get("state") or "") == "pending":
                # The first pass decides and delivers in one tick, and the label walk runs
                # before delivery. A pending *trusted* PR still gets its label now; a
                # pending card waiting on an author question does not.
                task_id = self._review_anchor(link)
                task = self.board.task(task_id) if task_id else None
                if task is not None and str(task.get("status") or "") == BLOCKED \
                        and not self.board.parked_by_bridge(str(task.get("id") or "")):
                    continue
            published = detail.get("label") or ()
            if pr_id not in active and not published and not detail.get("label_pending"):
                # A PR announced before the bridge published labels. Retrofitting a label
                # onto every one of them is a burst of events nobody asked for; a revision
                # puts it back in the window. With labels off the verdict is still worth
                # reading, but only for a PR the bridge has a card for.
                if wired or not link.get("task_id"):
                    continue
            pending.append((link, detail))
        if not pending:
            return
        reads = self._read_labels([str(link.get("source_id") or "") for link, _ in pending],
                                  report)
        for link, detail in pending:
            pr_id = str(link.get("source_id") or "")
            try:
                self._one_review_state(pr_id, link, detail, tips, reads, report,
                                       merged=pr_id in finished)
            except (BoardError, sqlite3.Error) as exc:
                report.prs["review_errors"] += 1
                report.errors.append(f"review state {pr_id[:8]}: {exc}")

    def _labeler(self) -> Optional[Labeler]:
        """The publisher this deployment labels with, or None when it publishes none.

        The verdict's consequence runs either way: the card is local state, and a deployment
        that asked for no label events still wants its review cards finished.
        """
        if self.labels is None or not self.config.labels or not self.target.address:
            return None
        return self.labels

    def _apply_verdict(self, pr_id: str, card: dict[str, Any], verdict: Verdict,
                       anchor: VerdictAnchor, detail: dict[str, Any],
                       link: Optional[dict[str, Any]] = None,
                       report: "Report | ReconcileReport | None" = None) -> tuple[str, bool]:
        """Finish the review card on an approval, or open the fix it asks for.

        The verdict is the review's end. `approved` completes the card -- the merge is still
        a person's, and the merge route reads the approval's tip from the link
        (`verdict_tip`) before it applies anything. `changes-requested` **queues the fix**:
        one card per verdict event, carrying the findings, on the implementer's desk. The
        review card itself is left where the reviewer parked it -- it is still the PR's
        review, and a revision reopens it for the tip the fix leaves -- because `done` on an
        open PR is the one thing the guard refuses and a second claim on the same findings
        is the duplication a separate card exists to stop.

        Only a card the bridge parked is touched: `blocked` with one of `PARK_MARKERS`, or
        `review`. A `running` card is a reviewer still working -- its verdict is read again
        on the next pass -- and a card blocked on a question is never answered by a verdict.

        Returns `(outcome, wrote)`: the outcome the caller counts, and whether this pass was
        the one that wrote it. A second pass over the same verdict finds the fix card by its
        key and reports `(APPLIED_CHANGES, False)`, so the count and the note are the pass
        that acted and not every pass that looked.
        """
        task_id = str(card.get("id") or "")
        status = str(card.get("status") or "")
        if not task_id or status not in (BLOCKED, REVIEW):
            return "", False
        if status == BLOCKED and not self.board.parked_by_bridge(task_id):
            return "", False
        if not self.reviewers or as_hex(verdict.author) not in self.reviewers:
            # Defence in depth: the reader already filters, but a verdict object from
            # another source must not finish a card either. No configured reviewer means
            # no verdict is in force at all.
            note = (f"verdict on PR {pr_id[:8]} ignored: its author is not a configured "
                    "reviewer")
            if report is not None:
                report.notes.append(note)
            log.warning("ngit: %s", note)
            return "", False
        tip = str(anchor.tip or detail.get("tip") or "")
        if anchor.label == APPROVED:
            result = (f"Verdict: approved -- comment {anchor.event_id or '(unknown)'}"
                      f" on PR {pr_id[:8]}"
                      + (f", covering tip {tip}" if tip else ""))
            body = str(verdict.body or "").strip()
            if body:
                result += f"\n\n{body}"
            self.board.approve_guard(task_id, verdict=anchor.event_id)
            if self.board.complete(
                    task_id, author="ngit", result=result,
                    comment=f"Approved by comment {anchor.event_id or '(unknown)'} on"
                            f" PR {pr_id[:8]}"
                            + (f" at {tip}" if tip else "")
                            + ". The merge is the operator's."):
                return APPLIED_APPROVED, True
            return "", False
        if anchor.label == CHANGES_REQUESTED:
            return self._fix_card(pr_id, card, verdict, anchor, detail, tip=tip, link=link,
                                  report=report)
        return "", False

    def _fix_card(self, pr_id: str, card: dict[str, Any], verdict: Verdict,
                  anchor: VerdictAnchor, detail: dict[str, Any], *, tip: str,
                  link: Optional[dict[str, Any]] = None,
                  report: "Report | ReconcileReport | None" = None) -> tuple[str, bool]:
        """Open the one card a changes-requested verdict asks for, or find the one it has.

        Keyed on the verdict event (`Board.fix_key`), so the idempotency is the statement
        that queued the work: a re-read of one verdict lands on its card, and a second review
        of the same PR -- the fix pushed, reviewed again, changes asked for again -- is a
        second statement and earns a second card.

        Ownership decides whether this instance queues anything at all: the fix belongs to
        the queue that can push to the PR's branch, so a PR authored outside `ngit.owns` is
        left to its author, and the pass says once that it skipped it. Nothing here merges
        or reviews: the strongest thing a verdict does is put work in the queue.
        """
        if not anchor.event_id:
            # No event id, no key: a card that cannot be idempotent is worse than none.
            if report is not None:
                report.notes.append(
                    f"fix for PR {pr_id[:8]}: the verdict names no event, so nothing was"
                    " queued")
            return "", False
        author = as_hex(str(detail.get("author") or ""))
        if self.owns and author not in self.owns:
            if report is not None:
                note = (f"fix for PR {pr_id[:8]}: changes requested, and the PR's author"
                        f" ({npub_of(author) if author else 'unknown'}) is not one this"
                        " instance speaks for, so no card was queued")
                if str(detail.get("fix_skipped") or "") != anchor.event_id:
                    # Once per verdict, not once per poll: the PR stays open, the verdict
                    # stays in force, and a note every ten minutes is noise in a cron log.
                    report.notes.append(note)
                    detail["fix_skipped"] = anchor.event_id
                    if link is not None:
                        self._save_detail(link, detail,
                                          task_id=str(card.get("id") or ""))
                log.info("ngit: %s", note)
            return "", False
        assignee = (self.config.review_fix_assignee or self.config.assignee or "").strip()
        if not assignee:
            if report is not None:
                report.notes.append(
                    f"fix for PR {pr_id[:8]}: no ngit.assignee (or review_fix_assignee) to"
                    " dispatch it to, so no card was queued")
            return "", False
        key = fix_key(pr_id, anchor.event_id)
        if self._fix_queued(pr_id, anchor.event_id) is not None:
            return APPLIED_CHANGES, False
        findings = str(verdict.body or "").strip() or (
            f"The verdict comment {anchor.event_id} on PR {pr_id[:8]} asks for changes;"
            " read it on the PR.")
        pr = pr_from_note(pr_id, detail, tip=tip)
        task_id = self.board.create(
            title=fix_title(pr_id, str(detail.get("subject") or "")),
            body=fix_body(pr, self.target, tip=tip, findings=findings,
                          event_id=anchor.event_id,
                          review_task=str(card.get("id") or "")),
            assignee=assignee, status="ready", idempotency_key=key,
            anchor=self.anchor, priority=self.config.priority)
        if report is not None:
            report.notes.append(
                f"fix card {task_id} for PR {pr_id[:8]}: changes requested by comment"
                f" {anchor.event_id[:8]}, queued for {assignee}")
        log.info("ngit: fix card %s for PR %s (verdict %s)", task_id, pr_id[:8],
                 anchor.event_id[:8])
        return APPLIED_CHANGES, True

    def _fix_queued(self, pr_id: str, event_id: str) -> Optional[str]:
        """The card this verdict event already queued, or None.

        The consequence is the fix card and the fix card is keyed on the verdict event, so
        asking the board is asking whether anything was written. That is what makes a second
        pass over one verdict, and a reconcile of a card parked before the rule, quiet.
        """
        if not event_id:
            return None
        found = self.board.by_idempotency(fix_key(pr_id, event_id))
        return str(found["id"]) if found is not None else None

    def _review_brief(self, task: Optional[dict[str, Any]], note: Mapping[str, Any], *,
                      pr_id: str, tip: str = "") -> str:
        """The review brief for a card this bridge wrote, or "" for one it did not.

        The body is the instruction the next worker reads, so it follows the work: a fix
        round replaces the review brief, and a revision puts it back for the new tip. A
        re-review a person filed under the brief's own title has a body of theirs, and
        replacing it would be the bridge editing someone else's instruction.
        """
        if task is None or not str(task.get("idempotency_key") or "").startswith("ngit:pr:"):
            return ""
        return review_body(pr_from_note(pr_id, note, tip=tip), self.target)

    def _read_labels(self, pr_ids: Sequence[str], report: Report) -> ReadState:
        """One relay read for the whole pass, or the reason there is none."""
        if self.state is None:
            return ReadState()
        try:
            state = self.state(list(pr_ids))
        except Exception as exc:  # noqa: BLE001 - a reader may raise; the pass may not die
            report.prs["label_errors"] += 1
            report.errors.append(f"label read: {type(exc).__name__}: {exc}"[:200])
            return ReadState()
        for problem in state.errors:
            report.prs["label_errors"] += 1
            report.errors.append(f"label read: {problem}"[:200])
        return state

    def _one_review_state(self, pr_id: str, link: dict[str, Any],
                          detail: dict[str, Any], tips: dict[str, tuple[str, int]],
                          reads: ReadState, report: Report, *, merged: bool) -> None:
        """One PR: what its verdict does to its card, then what it does to the PR's label.

        The verdict comes from the PR root, and so does the label set: `read.labels` is what
        the relays say the PR currently carries, including anything an earlier writer left.
        Everything not in the desired set is retracted by the same call that publishes the
        label that belongs there, which is what makes one pass enough to correct a PR.
        """
        tip, tip_at = tips.get(pr_id, (str(detail.get("tip") or ""),
                                      int(detail.get("tip_at") or 0)))
        detail["tip"], detail["tip_at"] = tip, tip_at
        task_id = self._review_anchor(link)
        card = self.board.task(task_id) if task_id else None
        if card is not None and str(card.get("status") or "") == "archived":
            card = None
        read = reads.prs.get(pr_id)
        if read is None:
            # No answer for this PR (a reader failure, or none configured). A label states
            # a verdict, and without the verdict the only safe thing to say is nothing: the
            # row keeps what readers already see and the next pass asks again.
            report.notes.append(
                f"review state {pr_id[:8]}: no verdict could be read;"
                " the label is left as it is and no card is touched")
            self._save_detail(link, detail, task_id=task_id)
            return
        verdict = read.verdict
        if verdict is not None and not verdict.readable:
            report.notes.append(
                f"review state {pr_id[:8]}: the newest verdict (event {verdict.event_id or '?'}"
                f" on {verdict.relay or 'a relay'}) states a verdict that cannot be read;"
                " the label is left as it is and no card is touched")
            self._save_detail(link, detail, task_id=task_id)
            return
        anchor = anchor_verdict(detail, verdict, tip=tip, tip_at=tip_at)
        if anchor is None:
            # No verdict on the PR (or none any more). An anchor no event backs must not
            # decide a label: the next pass reads the root again and starts from there.
            for key in ANCHOR_KEYS:
                detail.pop(key, None)
        else:
            detail.update(anchor_detail(anchor))
        desired = review_labels(verdict=anchor, review_card=card is not None, tip=tip,
                                tip_at=tip_at, labelled=bool(detail.get("label") or ()),
                                merged=merged)
        if anchor is not None and verdict is not None and card is not None \
                and tuple(desired or ()) == (anchor.label,):
            # The verdict is in force: it decided the label, so it decides the card too.
            outcome, wrote = self._apply_verdict(pr_id, card, verdict, anchor, detail,
                                                 link=link, report=report)
            if outcome == APPLIED_APPROVED and wrote:
                report.prs["approved"] += 1
                report.notes.append(
                    f"card {card.get('id')} for PR {pr_id[:8]}: approved, and finished")
            elif outcome == APPLIED_CHANGES and wrote:
                # The note naming the new card is the fix writer's: it knows the id and the
                # profile, and it is the only pass that creates one.
                report.prs["changes"] += 1
                log.info("ngit: PR %s changes requested; the fix is its own card", pr_id[:8])
        if desired is None:
            self._save_detail(link, detail, task_id=task_id)
            return
        labeler = self._labeler()
        if labeler is None:
            return
        # One label per PR, and the relays decide which event carries it. The row only says
        # what this pass published last: a second event carrying the same word is invisible
        # to a value test, so the survivor is chosen first and every other live event goes.
        # A row whose own event is no longer live publishes the label again rather than
        # leaving the PR with none.
        row_events = [str(event) for event in (detail.get("label_events") or ()) if event]
        keep, stale = reconcile_labels(read.labels, desired, row_events)
        add = tuple(desired) if desired and keep is None else ()
        retract = [label.event_id for label in stale]
        for event_id in row_events:
            if keep is not None and event_id == keep.event_id:
                continue
            if event_id not in retract:
                retract.append(event_id)
        if not add and not retract:
            self._save_detail(link, detail, task_id=task_id)
            return
        try:
            result = labeler(pr_id, self.target.address, add, retract)
        except Exception as exc:  # noqa: BLE001 - a publisher may raise; the pass may not die
            report.prs["label_errors"] += 1
            report.errors.append(f"label {pr_id[:8]}: {type(exc).__name__}: {exc}"[:200])
            return
        if not result.ok:
            # Nothing was published, so the link still says what readers see: the next poll
            # tries again rather than believing a label that never landed. `label_pending`
            # is what keeps that promise when the PR's own events are no longer in the poll.
            report.prs["label_errors"] += 1
            report.errors.append(
                f"label {pr_id[:8]} ({', '.join(desired) or 'none'}): {result.error}"[:200])
            detail["label_pending"] = list(desired)
            self._save_detail(link, detail, task_id=task_id)
            return
        detail["label"] = list(desired)
        if add:
            detail["label_events"] = list(result.events)
        elif keep is not None:
            # A correction pass: the event that stayed is the label, and the row names it so
            # the next pass keeps choosing the same one.
            detail["label_events"] = [keep.event_id]
        else:
            # A retraction-only pass: the PR carries no label now, and the row must say so
            # or every later pass would treat it as one that never published.
            detail["label_events"] = []
        if desired:
            detail["label_relay"] = read.relay
        else:
            detail.pop("label_relay", None)
        if add:
            report.prs["labels"] += 1
            log.info("ngit: PR %s labelled %s", pr_id[:8], ", ".join(desired))
            where = f" from {read.relay}" if read.relay else ""
            report.notes.append(f"label {pr_id[:8]}: {', '.join(desired)}{where}")
            if read.disagreement():
                report.notes.append(
                    f"label {pr_id[:8]}: relays disagreed ({read.disagreement()});"
                    " the newest verdict won")
        elif desired:
            # The label was live and stayed; the pass only took a duplicate -- or a leftover
            # from another writer -- off the PR.
            report.notes.append(
                f"label {pr_id[:8]}: {', '.join(desired)}; retracted {len(retract)}"
                " other live label event(s)")
        else:
            report.notes.append(
                f"label {pr_id[:8]}: removed {len(retract)} label(s) that did not belong")
        detail.pop("label_pending", None)
        if not result.retracted:
            report.notes.append(
                f"label {pr_id[:8]}: the superseded label could not be retracted"
                f" ({result.error}); a reader may see both")
        self._save_detail(link, detail, task_id=task_id)

    def _save_detail(self, link: dict[str, Any], detail: dict[str, Any], *,
                     task_id: str = "") -> None:
        """Write the link's note, and the task it anchors to, when either changed.

        A poll with no news writes nothing. The task id is written when the anchor moved: a
        review card the bridge did not make becomes the PR's review task as soon as a label
        pass has read it, so the row names the task whose verdict is in force -- and the one
        a merge completes.
        """
        repoint = bool(task_id) and task_id != str(link.get("task_id") or "")
        if not repoint and (json.dumps(detail, sort_keys=True)
                            == json.dumps(_detail(link), sort_keys=True)):
            return
        self.store.ngit_link_save(self.target.key, KIND_PR, str(link.get("source_id") or ""),
                                  state=str(link.get("state") or "pending"),
                                  task_id=task_id or None, detail=detail)

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
        landed, so nothing is being hidden. A close supersedes it for the same reason.
        """
        if notice in ("merged", "closed"):
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
            if int(row["kind"]) == KIND_PR and state:
                report.prs[state if state in ("merged", "closed") else "signalled"] += 1


def _detail(link: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(link.get("detail") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


# `ngit pr checkout <root>` checks out `pr/<branch>-(<root event id, first 8 hex>)`; a push
# from that branch publishes the name with the `pr/` stripped, so the suffix reaches the
# 1618's `branch-name` whenever the whole string fits the writer's 60-character cut.
_PR_CHECKOUT_SUFFIX = re.compile(r"-\(([0-9a-f]{8})\)$")


def _fold_root(known: Mapping[tuple[str, str], str], author: str, branch: str, *,
               pr_id: str = "") -> str | None:
    """The root a proposal belongs to: the exact (author, branch), or its checkout name.

    A decorated name folds only when its hex is the event id of the root it names -- what
    `ngit pr checkout` appends -- so a branch genuinely called `topic-(deadbeef)` is a
    different branch unless `deadbeef` happens to open the root of `topic`. `pr_id` is the
    proposal itself, which the caller has already seeded into `known`: an exact match on it
    names no root, so the decorated lookup still has to run.
    """
    root = known.get((author, branch))
    if root is not None and root != pr_id:
        return root
    match = _PR_CHECKOUT_SUFFIX.search(branch)
    if match is None:
        return None
    candidate = known.get((author, branch[:match.start()]))
    if candidate is None or candidate == pr_id:
        return None
    return candidate if candidate.startswith(match.group(1)) else None


def _guard_covers_tip(held: Mapping[str, Any], tip: str) -> bool:
    """Whether a terminal guard still means what it says for the PR's current tip.

    `applied` and `closed` are final however the feed moves: a PR does not un-merge, and a
    closed proposal is not revived by a late event. `approved` is final only for the tip the
    reviewer saw. That is the one terminal state that *released* the refusal trigger, and the
    one a revision exists to make stale -- `_revisions` reopens the card for a newer tip
    precisely because the old approval does not cover it. Keeping the row `approved` there
    would leave the reopened card with no trigger and no approval over the tip a worker's
    completion would claim. The caller re-arms the guard at the new tip instead.
    """
    state = str(held.get("state") or "")
    if state not in TERMINAL_GUARD_STATES:
        return False
    if state != ngit_guard.APPROVED:
        return True
    return str(held.get("tip") or "") == str(tip or "")


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


def pr_from_note(pr_id: str, note: Mapping[str, Any], *, tip: str = "") -> PullRequest:
    """A PR as the link row remembers it, for a brief a later pass rewrites.

    A revision and a verdict can both arrive on a poll that no longer carries the PR's own
    event, so the row's note is what the new brief is built from. `tip` is the commit the
    brief is written for -- the new one on a revision, the reviewed one on a fix.
    """
    return PullRequest(id=str(pr_id), author=str(note.get("author") or ""),
                       subject=str(note.get("subject") or ""),
                       branch=str(note.get("branch") or ""),
                       nevent=str(note.get("nevent") or ""), commit=str(tip or ""))


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
    """Promote (yes) or decline (no) a held issue's or PR's task, recording who decided.

    The answer is a person's, so it must come from a trusted npub: a stranger who filed the
    issue or opened the PR must not also be able to authorise it. The decision is written as
    a task comment first and the link state second, so an audit line exists even if the later
    write fails.
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
        kind = int(link.get("kind") or 0)
        noun = "PR" if kind == KIND_PR else "issue"
        try:
            if yes:
                promoted = board.release(
                    task_id, author="ngit",
                    comment=f"Authorised by {npub_of(who)} at {stamp}: {noun} {issue_nevent}"
                            " -> ready")
                if not promoted:
                    raise NgitError(f"task {task_id} changed state while answering; "
                                    "nothing was recorded, try again")
                store.ngit_link_state(link["repo"], kind, link["source_id"],
                                      "signalled" if kind == KIND_PR else "authorised")
                return {"task": task_id, "status": "ready", "changed": True,
                        "issue": issue_nevent, "by": npub_of(who)}
            board.comment(task_id, "ngit",
                          f"Declined by {npub_of(who)} at {stamp}: "
                          f"{reason.strip() or 'no reason given'}; the task stays blocked.")
            store.ngit_link_state(link["repo"], kind, link["source_id"], "declined")
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
        if link is None:
            link = store.ngit_link_for_source(KIND_PR, event_hex(text))
    if link is None or int(link.get("kind") or 0) not in (KIND_ISSUE, KIND_PR):
        raise NgitError(f"no ngit issue or PR is linked to {text!r}")
    return link
