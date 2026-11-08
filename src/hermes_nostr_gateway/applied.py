"""`applied` has two witnesses: the status record, and the content on the base branch.

`ngit pr apply` replays a proposal's patches as **new** commits -- the proposal's own commits
are re-signed, so the tip a kind-1618/1619 names never becomes an ancestor of the branch it
landed on, however cleanly the change applied. A forge that fast-forwards (`git merge`,
`gh pr merge`) makes ancestry the whole answer; this repository's only merge route makes it
useless. The bridge therefore asks in order:

1. **the record** -- a kind-1631 `applied` status naming the PR. `ngit.Sync` reads it and
   never reaches this module for a PR the relay already answered;
2. **the content** -- does the base branch already carry the patches the proposal adds?
   ``git patch-id`` hashes a diff with the line numbers stripped, so a replayed, rebased or
   cherry-picked commit keeps the same id while its commit id changes;
3. **ancestry** -- the forge's answer: exact and cheapest when it holds, which is why it is
   checked first and not trusted alone.

``probe()`` answers (2) and (3); the record is the caller's. Nothing here decides policy:
``ngit.Sync`` asks, and writes what came back on the card and in the refusal.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

#: A full commit id, which is all `git patch-id` prints beside a patch id.
_OID = re.compile(r"^[0-9a-f]{40}$")

#: What one `git` call may take. Every call here is a local object-graph read; the timeout is
#: only so a wedged filesystem cannot hold a sync pass open forever.
GIT_TIMEOUT_S = 60.0


class GitError(RuntimeError):
    """A git call failed. The message is the one line an operator needs, never a traceback."""


@dataclass(frozen=True)
class Evidence:
    """What the checkout said about one proposal, and why.

    `applied` is the answer; `why` is the sentence a card or a refusal can print verbatim, so
    "is open at <tip>" is never said about a proposal whose content is already on the branch.
    `commit` is the commit on the base branch that carries the content, when one was found.
    """

    applied: bool
    why: str = ""
    commit: str = ""

    def as_dict(self) -> dict[str, object]:
        return {"applied": self.applied, "why": self.why, "commit": self.commit}


def short(value: str) -> str:
    """The first 12 characters, how this repository names a commit in a sentence."""
    return str(value or "")[:12] or "unknown"


def run_git(repo: Path, args: Sequence[str], *, stdin: Optional[str] = None,
            timeout: float = GIT_TIMEOUT_S) -> str:
    """One `git -C <repo> ...` call. Only stdout is returned.

    A non-zero exit carries the last stderr line, like `run_ngit`: this runs from a cron and
    its output is a log line, not a traceback.
    """
    command = ["git", "-C", str(repo), *args]
    try:
        done = subprocess.run(command, input=stdin, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError as exc:
        raise GitError("git could not be run: it is not on PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitError(f"git {args[0] if args else ''} could not run: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise GitError(f"git {args[0] if args else ''} failed ({done.returncode}): "
                       f"{(detail[-1] if detail else 'no output').strip()[:200]}")
    return done.stdout or ""


def parse_patch_ids(value: str) -> list[tuple[str, str]]:
    """`git patch-id` output as (patch id, commit id): one pair per commit it was given.

    A diff handed over without a `commit <oid>` header (the proposal as a whole, for a squash
    apply) prints an all-zero commit id, which is kept: the patch id is the part that matters.
    """
    found: list[tuple[str, str]] = []
    for line in str(value or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and _OID.match(parts[0]) and _OID.match(parts[1]):
            found.append((parts[0], parts[1]))
    return found


class Checkout:
    """The checkout's answer to "is this proposal already on the base branch?".

    One instance per sync pass: the base ref is resolved once (preferring `origin/<base>`,
    which is what landed, over a local branch that may not have been pulled), and `head()`
    is the tip a caller stamps its answers with so an unmoved proposal is not probed twice.
    """

    def __init__(self, repo: Path, base: str = "master", *,
                 runner: Callable[..., str] = run_git, timeout: float = GIT_TIMEOUT_S):
        self.repo = Path(repo)
        self.base = str(base or "master")
        self._run = runner
        self._timeout = timeout
        self._ref = ""
        #: What the base is called in a sentence: `origin/master` when that is what was read.
        self._name = self.base

    # -- what a sync pass asks once ---------------------------------------

    def head(self) -> str:
        """The base branch's tip, or "" when this checkout cannot answer.

        Empty is not an error here: a caller uses it only to stamp an answer, and a checkout
        that cannot name its base re-probes rather than skipping the check.
        """
        try:
            return self._base_tip(self._base_ref())
        except GitError:
            return ""

    # -- the question -----------------------------------------------------

    def probe(self, tip: str) -> Evidence:
        """Is the proposal at `tip` already on the base branch?

        The three answers this can give, in the order they are cheapest and most certain:
        the tip is an ancestor (a fast-forward forge); every patch the proposal adds is
        already on the branch (an applied patch series, a rebase, a cherry-pick); the
        proposal as one patch is on the branch (a squash). Anything else is a no, and the
        sentence says what was compared so a reader can check it.
        """
        tip = str(tip or "").strip()
        if not tip:
            return Evidence(False, "the PR names no tip to check")
        try:
            ref = self._base_ref()
            base = self._base_tip(ref)
        except GitError as exc:
            return Evidence(False, f"its patch could not be checked ({exc})")
        if not base:
            return Evidence(False, f"the checkout has no {self.base} branch to compare against")
        try:
            if not self._has(tip):
                return Evidence(
                    False, f"tip {short(tip)} is not in this checkout, so its patch could not"
                           " be checked (fetch the proposal and the next pass will)")
            if self._is_ancestor(tip, ref):
                return Evidence(True, f"tip {short(tip)} is an ancestor of {self._name}",
                                commit=tip)
            fork = self._merge_base(tip, ref)
            if not fork:
                return Evidence(
                    False, f"tip {short(tip)} shares no history with {self._name}, so its patch"
                           " could not be checked")
            mine = self._patches([tip, "^" + fork])
            if not mine:
                return Evidence(False, f"tip {short(tip)} adds no commit to compare")
            theirs = dict(self._patches([ref, "^" + fork]))
            missing = [pair for pair in mine if pair[0] not in theirs]
            if not missing:
                commit = theirs[mine[0][0]]
                if len(mine) == 1:
                    why = (f"its patch is already on {self._name} as {short(commit)}"
                           f" (patch-id {mine[0][0][:12]})")
                else:
                    why = (f"all {len(mine)} of its commits are on {self._name} by patch-id"
                           f" (newest {short(commit)})")
                return Evidence(True, why, commit=commit)
            squashed = self._combined_patch(fork, tip)
            if squashed and squashed in theirs:
                commit = theirs[squashed]
                return Evidence(
                    True, f"the proposal as one patch is on {self._name} as {short(commit)}"
                          f" (patch-id {squashed[:12]})", commit=commit)
            return Evidence(
                False, f"no commit on {self._name} carries its patch"
                       f" ({len(mine)} compared against {len(theirs)} since"
                       f" {short(fork)})")
        except GitError as exc:
            return Evidence(False, f"its patch could not be checked ({exc})")

    # -- git, one question at a time --------------------------------------

    def _base_ref(self) -> str:
        """The ref the base branch is read from: the further of the clone's two copies.

        A merge is pushed to `origin/<base>`, and this checkout is the daemon's -- it is not
        pulled after every merge, so the remote-tracking ref is normally what landed. A
        checkout whose local branch is ahead of (or has not pushed) that ref must not be read
        as behind, so when one ref contains the other the descendant wins; when they have
        diverged, `origin/<base>` is the one a merge lands on.
        """
        if self._ref:
            return self._ref
        found: list[tuple[str, str]] = []
        last: Optional[GitError] = None
        for ref, name in ((f"refs/remotes/origin/{self.base}", f"origin/{self.base}"),
                          (f"refs/heads/{self.base}", self.base), (self.base, self.base)):
            try:
                self._run(self.repo, ["rev-parse", "--verify", "--quiet", ref + "^{commit}"],
                          timeout=self._timeout)
            except GitError as exc:
                last = exc
                continue
            found.append((ref, name))
        if not found:
            raise GitError(f"the checkout has no {self.base} branch"
                           + (f" ({last})" if last is not None else ""))
        chosen = found[0]
        for candidate in found[1:]:
            if self._is_ancestor(chosen[0], candidate[0]):
                chosen = candidate
        self._ref, self._name = chosen
        return self._ref

    def _base_tip(self, ref: str) -> str:
        return str(self._run(self.repo, ["rev-parse", "--verify", "--quiet", ref],
                             timeout=self._timeout)).strip()

    def _has(self, tip: str) -> bool:
        try:
            self._run(self.repo, ["cat-file", "-e", tip + "^{commit}"], timeout=self._timeout)
        except GitError:
            return False
        return True

    def _is_ancestor(self, tip: str, ref: str) -> bool:
        try:
            self._run(self.repo, ["merge-base", "--is-ancestor", tip, ref],
                      timeout=self._timeout)
        except GitError:
            return False
        return True

    def _merge_base(self, tip: str, ref: str) -> str:
        try:
            return str(self._run(self.repo, ["merge-base", ref, tip],
                                 timeout=self._timeout)).strip()
        except GitError:
            return ""

    def _patches(self, rev_range: Sequence[str]) -> list[tuple[str, str]]:
        """(patch id, commit id) for every non-merge commit `git log -p <range>` prints.

        One `git log` piped into one `git patch-id`: a branch with fifty commits costs two
        processes, not a hundred. Merge commits are skipped -- they carry no patch of their
        own to compare, and `ngit pr apply` produces none.
        """
        diff = self._run(self.repo, ["log", "--no-merges", "--no-color", "--no-ext-diff",
                                     "--pretty=format:commit %H", "-p", *rev_range],
                         timeout=self._timeout)
        if not diff.strip():
            return []
        return parse_patch_ids(self._run(self.repo, ["patch-id", "--stable"], stdin=diff,
                                         timeout=self._timeout))

    def _combined_patch(self, fork: str, tip: str) -> str:
        """The patch id of the proposal taken as one diff, for a squashed apply."""
        diff = self._run(self.repo, ["diff", "--no-color", "--no-ext-diff", fork, tip],
                         timeout=self._timeout)
        if not diff.strip():
            return ""
        found = parse_patch_ids(self._run(self.repo, ["patch-id", "--stable"], stdin=diff,
                                          timeout=self._timeout))
        return found[0][0] if found else ""
