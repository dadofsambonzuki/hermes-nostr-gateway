"""The cards an off-box merge landed, read from the commits a status names.

A merge made somewhere else -- a maintainer pressing GitWorkshop's merge button -- publishes
the proposal's kind-1631 applied status and nothing about the issue the work answered. The
issue stays `open` because the join between an issue and the work that answers it is this
box's: `ngit_links` holds it, and the tool that merged had no board to consult.

The join is recoverable from what the merge did publish. Every commit a worker pushes carries
`Card: t_xxxxxxxx` (written by `scripts/card_trailer.sh`), so the status's own commit, and the
commits it says landed, name the cards. The pass then publishes, per linked issue, the record
`scripts/merge_pr.sh` would have written -- the same event, the same key, the same relay set.

Three questions, answered in this order, and each one refuses rather than guesses:

1. **Which commit** the status names. `merge_from_event` has already resolved it: NIP-34's
   `merge-commit`, ngit's `merge-commit-id`, then the first `applied-as-commits`. A status
   that names none is not evidence and this module says so.
2. **Which commits** that merge landed. A true merge commit carries the branch as its second
   parent, so `git log <merge>^1..<merge>^2` is exactly what it brought in. A replay has no
   merge commit (`ngit pr apply` re-signs the patches), so the status's own `c`/
   `applied-as-commits` list is the set -- and that list is also read for a true merge, since
   a publisher may name both.
3. **Which cards** those commits name. `Card: operator` is not a card, and neither is a value
   that is not `t_xxxxxxxx`: a trailer naming something else is reported and skipped, never
   resolved against the board.

The checkout may not hold the commit yet -- a merge pushed to a grasp server does not move
this clone. One best-effort `git fetch origin` is tried, and if the commit is still absent the
pass gets an error line instead of a resolution: a record naming a commit nobody can resolve
is worse than no record, because the next pass can still heal a missing one.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .applied import GIT_TIMEOUT_S, GitError, run_git

#: A board task id, which is the only thing a `Card:` trailer may name here.
CARD = re.compile(r"^t_[0-9a-f]{8}$")

#: The trailer value that says "this is operator work", which names no card to resolve.
OPERATOR = "operator"

#: Git's own trailer parser, matched to `scripts/merge_pr.sh` so both read one definition of
#: what a trailer is: a line in the trailer block, not a sentence that happens to start with
#: `Card:` in the middle of a message.
_TRAILERS = "--format=%(trailers:key=Card,valueonly)"


@dataclass(frozen=True)
class Landed:
    """What one applied status says landed, and what could be read of it.

    `commit` is the commit the status names -- the one a resolved record carries -- and
    `error` is the sentence a pass prints when it cannot be used. `parents` is 2 for a true
    merge (which is what decides how the landed commits are read) and `fetched` says a fetch
    ran, which is the ordinary case for a merge made on another box.
    """

    commit: str = ""
    parents: int = 0
    commits: tuple[str, ...] = ()
    cards: tuple[str, ...] = ()
    fetched: bool = False
    notes: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def short(value: str) -> str:
    """The first 12 characters, how this repository names a commit in a sentence."""
    return str(value or "")[:12] or "unknown"


def cards_from(values: Sequence[str]) -> tuple[str, ...]:
    """The card ids a run of trailer values names, deduped, in the order git printed them."""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text == OPERATOR or not CARD.match(text):
            continue
        if text not in out:
            out.append(text)
    return tuple(out)


def unnamed(values: Sequence[str]) -> tuple[str, ...]:
    """The trailer values that name neither a card nor the operator."""
    out: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text == OPERATOR or CARD.match(text):
            continue
        if text not in out:
            out.append(text)
    return tuple(out)


def _has(repo: Path, commit: str, runner: Callable[..., str], timeout: float) -> bool:
    """Whether the checkout holds this commit as an object."""
    try:
        runner(repo, ["cat-file", "-e", f"{commit}^{{commit}}"], timeout=timeout)
    except GitError:
        return False
    return True


def fetch_origin(repo: str | Path, *, timeout: float = GIT_TIMEOUT_S) -> str:
    """One `git fetch origin`, and the reason it failed as one line ("\"\" when it worked).

    Its own call, not `run_git`'s, because a fetch's failure is a paragraph -- git prints a
    `fatal:` first and advice after it -- and `run_git` reports the last line. An operator
    reads the first one ("fatal: 'origin' does not appear to be a git repository"), not
    "and the repository exists."
    """
    command = ["git", "-C", str(repo), "fetch", "--quiet", "origin"]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env={**os.environ,
                                                             "GIT_TERMINAL_PROMPT": "0"})
    except FileNotFoundError:
        return "git is not on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"git fetch could not run: {exc}"
    if done.returncode == 0:
        return ""
    lines = [line.strip() for line in (done.stderr or done.stdout or "").splitlines()
             if line.strip()]
    for line in lines:
        if line.startswith(("fatal:", "error:")):
            return line[:200]
    return (lines[0] if lines else f"exit {done.returncode}")[:200]


def _parents(repo: Path, commit: str, runner: Callable[..., str], timeout: float) -> int:
    """How many parents the commit has: 2 is a true merge, 1 a replay or a squash."""
    text = runner(repo, ["rev-list", "--parents", "-n", "1", commit], timeout=timeout)
    return max(0, len(str(text or "").split()) - 1)


def _trailers(repo: Path, args: Sequence[str], runner: Callable[..., str],
              timeout: float) -> list[str]:
    """The `Card:` trailer values `git log` prints for these commits, one per line."""
    text = runner(repo, ["log", "--no-color", _TRAILERS, *args], timeout=timeout)
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def read(repo: str | Path, *, commit: str, listed: Sequence[str] = (),
         runner: Callable[..., str] = run_git,
         fetcher: Optional[Callable[..., str]] = fetch_origin,
         timeout: float = GIT_TIMEOUT_S) -> Landed:
    """The cards the merge a status names landed, or why they could not be read.

    `commit` is what the status named (a merge commit, or the tip of a replayed set) and
    `listed` is every other commit it named. A merge commit is read through its second
    parent, because that is the branch it brought in; anything else is read through the list
    and its own message, because a replay has no merge to walk. Both are read when a
    publisher named both -- the union costs one git call and cannot invent a card.

    `fetcher` is `git fetch origin`; `None` refuses to fetch at all, which is how a caller
    that must not touch the network (a test, a read-only checkout) says so.
    """
    where = Path(repo)
    tip = str(commit or "").strip().lower()
    if not tip:
        return Landed(error="the status names no commit, so the cards it landed cannot be read")
    fetched = False
    if not _has(where, tip, runner, timeout):
        if fetcher is None:
            return Landed(commit=tip, error=f"commit {short(tip)} is not in the checkout, so"
                                            " the cards it landed cannot be read")
        problem = fetcher(where, timeout=timeout)
        if problem:
            return Landed(
                commit=tip,
                error=f"commit {short(tip)} is not in the checkout and `git fetch origin`"
                      f" failed ({problem}), so the cards it landed cannot be read")
        fetched = True
        if not _has(where, tip, runner, timeout):
            return Landed(
                commit=tip, fetched=True,
                error=f"commit {short(tip)} is not in the checkout even after `git fetch"
                      " origin`, so the cards it landed cannot be read")
    try:
        parents = _parents(where, tip, runner, timeout)
    except GitError as exc:
        return Landed(commit=tip, fetched=fetched,
                      error=f"commit {short(tip)} could not be read ({exc})")
    named: list[str] = []
    for candidate in (tip, *(str(item or "").strip().lower() for item in listed)):
        if candidate and candidate not in named:
            named.append(candidate)
    present = [item for item in named if item == tip or _has(where, item, runner, timeout)]
    missing = [item for item in named if item not in present]
    notes: list[str] = []
    if missing:
        notes.append("commit " + ", ".join(short(item) for item in missing)
                     + " is not in the checkout, so its trailer was not read")
    values: list[str] = []
    try:
        if parents >= 2:
            # The branch the merge brought in, then the merge's own message: a forge that
            # puts the proposal's text in the merge commit names the cards there too.
            values += _trailers(where, ["--no-merges", f"{tip}^1..{tip}^2"], runner, timeout)
            values += _trailers(where, ["--no-walk", tip], runner, timeout)
        else:
            values += _trailers(where, ["--no-walk", *present], runner, timeout)
    except GitError as exc:
        return Landed(commit=tip, parents=parents, commits=tuple(present), fetched=fetched,
                      notes=tuple(notes),
                      error=f"commit {short(tip)} could not be read ({exc})")
    strange = unnamed(values)
    if strange:
        notes.append("ignored a Card trailer " + ", ".join(repr(item) for item in strange)
                     + ": it names no card")
    return Landed(commit=tip, parents=parents, commits=tuple(present),
                  cards=cards_from(values), fetched=fetched, notes=tuple(notes))


__all__ = ["CARD", "OPERATOR", "Landed", "cards_from", "fetch_origin", "read", "short",
           "unnamed"]
