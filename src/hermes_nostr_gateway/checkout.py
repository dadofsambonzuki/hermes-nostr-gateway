"""Where the live checkout stands, and whether it is serving merged code.

The daemon runs from this repository (`ExecStart=.venv/bin/hng serve`, an editable install
with `PYTHONPATH=<repo>/src`), so the tree it serves is whatever branch that clone was last
left on. A worker that checks a proposal out there to try it makes the proposal live: the box
then serves code nobody merged while `master` moves on, and `hng health` stays green because a
PR branch that passes its own gates is a working gateway.

The question that notices is about HEAD, not about cleanliness:

    is HEAD a commit that exists on origin/master?

`hng doctor` fails when it is not (`_checkout_check`), `hng status` prints the same fact as
one line, and `scripts/live_checkout_guard.sh` keeps the habit for shell scripts: a script
that needs a branch takes a worktree, never the operator's clone.

The checkout is found from the package's own location -- the build that is running -- so there
is no configuration to drift and no way to check a tree the daemon is not serving. A build
installed from a wheel has no clone to check, which is reported and not a failure.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: The branch the live checkout must be on. This repository's default; a clone whose
#: `origin/HEAD` names another branch is checked against that instead.
DEFAULT_BRANCH = "master"

#: Everything here reads local refs, so a slow answer means a broken host, not a slow fetch.
GIT_TIMEOUT_S = 10.0

#: What an operator types to return the clone to the code the box should be serving.
CHECKOUT_FIX = "git checkout master && git pull"

#: The answer when there is no clone to check: a wheel install, or a container build.
NOT_A_CHECKOUT = "this build is not running from a git checkout: nothing to check"


def _git(repo: Path, *args: str) -> tuple[int, str]:
    """Run one read-only git question in `repo`; (exit code, stripped stdout)."""
    try:
        done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                              text=True, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return done.returncode, (done.stdout or "").strip()


def repo_root(start: Path | None = None) -> Path | None:
    """The git checkout this build runs from, or None when it is not running from one.

    The package's own file is the honest starting point: an editable install resolves into
    the tree systemd starts the daemon from, and a run under `PYTHONPATH=<repo>/src` resolves
    into the tree the caller is actually executing. Walking up finds the clone in both
    `src/hermes_nostr_gateway` and an installed `site-packages/hermes_nostr_gateway`; a wheel
    install outside any clone finds nothing, and there is then nothing to check.
    """
    candidate = Path(start) if start is not None else Path(__file__).resolve().parent
    for _ in range(5):
        # `.git` is a directory in a clone and a file in a worktree; both count.
        if (candidate / ".git").exists():
            return candidate
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    return None


def is_card_workspace(path: Path) -> bool:
    """True when `path` is a card's worktree rather than the operator's live checkout.

    The same convention `scripts/worker_push_guard.sh` uses: a checkout under `.worktrees/`,
    or the one the dispatcher named in `HERMES_KANBAN_WORKSPACE`. A card is *expected* to be
    on a proposal branch, so doctor must not call that a failure -- it is simply not the tree
    the daemon serves.
    """
    try:
        resolved = Path(path).resolve()
    except OSError:
        return False
    if ".worktrees" in resolved.parts:
        return True
    workspace = os.environ.get("HERMES_KANBAN_WORKSPACE") or ""
    if not workspace:
        return False
    try:
        return Path(workspace).resolve() == resolved
    except OSError:
        return False


def _default_branch(repo: Path) -> str:
    """The branch this clone's origin calls its default, or `master` when it does not say.

    `origin/HEAD` is what a clone records at fetch time; a checkout made with `git init` and
    a `remote add` never records it, which is why the fallback is the repository's own name
    rather than an empty string.
    """
    code, name = _git(repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if code == 0 and name:
        return name.removeprefix("origin/")
    return DEFAULT_BRANCH


def _count(repo: Path, left: str, right: str) -> int | None:
    """`git rev-list --count left..right`: commits reachable from right and not from left."""
    code, out = _git(repo, "rev-list", "--count", f"{left}..{right}")
    if code != 0:
        return None
    try:
        return int(out)
    except ValueError:
        return None


@dataclass(frozen=True)
class CheckoutState:
    """What the live checkout has checked out, and how far that is from origin's branch.

    `behind` is what `master` has that HEAD does not (a stale checkout, which is allowed);
    `ahead` is what HEAD has that `master` does not (local commits, which is not);
    `note` says why there was nothing to check, and `problem` says why the question could
    not be answered at all. A state with a `note` is `ok`: a wheel install has no clone.
    """

    path: Path | None = None
    branch: str = ""
    commit: str = ""
    default_branch: str = DEFAULT_BRANCH
    behind: int = 0
    ahead: int = 0
    detached: bool = False
    note: str = ""
    problem: str = ""
    #: The fix when it is not the usual `git checkout master && git pull` (a missing remote
    #: branch has its own). Empty means `fix()` works it out from the state.
    fix_text: str = ""

    @property
    def ok(self) -> bool:
        """The daemon's code is merged code: on the default branch, nothing only local."""
        if self.note:
            return True
        if self.problem or self.path is None or self.detached:
            return False
        return self.branch == self.default_branch and self.ahead == 0

    def label(self) -> str:
        """How the checkout is named in one line: its branch, or `(detached)`."""
        if self.detached:
            return "(detached)"
        return self.branch or "?"

    def status_line(self) -> str:
        """The `hng status` line: what is checked out and how far behind it is."""
        if self.path is None:
            return "checkout: - (this build is not running from a git checkout)"
        if self.problem:
            return f"checkout: unknown ({self.problem})"
        ahead = f", {self.ahead} ahead" if self.ahead else ""
        return (f"checkout: {self.label()} @ {self.commit} "
                f"({self.behind} behind {self.default_branch}{ahead})")

    def describe(self) -> str:
        """The doctor sentence: the state, and the fact that decides ok."""
        if self.path is None:
            return self.note
        if self.problem:
            return self.problem
        state = f"{self.label()} @ {self.commit} at {self.path}"
        if self.ok:
            return f"{state} ({self.behind} behind {self.default_branch})"
        if self.detached:
            return f"{state}: detached, so it serves whatever commit was last checked out"
        if self.branch != self.default_branch:
            extra = f", {self.ahead} commit(s) not on {self.default_branch}" if self.ahead else ""
            return (f"{state} is a proposal branch, not {self.default_branch} "
                    f"({self.behind} behind{extra})")
        return (f"{state} has {self.ahead} commit(s) {self.default_branch} does not, so the "
                f"box is serving code nobody else has")

    def fix(self) -> str:
        """What to type to make it right; empty when it already is."""
        if self.fix_text:
            return self.fix_text
        if self.ok or self.path is None or self.problem:
            return ""
        where = f"cd {self.path} && "
        if self.detached:
            if self.ahead:
                return (f"{where}git switch -c pr/<slug>  # those commits exist only here, "
                        f"then git checkout {self.default_branch} && git pull")
            return where + CHECKOUT_FIX
        if self.branch != self.default_branch:
            return where + CHECKOUT_FIX
        return (f"{where}git branch pr/<slug> && git reset --hard origin/{self.default_branch}"
                f"  # the commit(s) stay on pr/<slug>")

    def as_dict(self) -> dict[str, object]:
        return {
            "path": str(self.path) if self.path else None,
            "branch": self.branch,
            "commit": self.commit,
            "default_branch": self.default_branch,
            "behind": self.behind,
            "ahead": self.ahead,
            "detached": self.detached,
            "ok": self.ok,
            "note": self.note,
            "problem": self.problem,
        }


def inspect(repo: Path | None = None) -> CheckoutState:
    """Read one checkout's HEAD and measure it against `origin/<default>`.

    Read-only, and honest about what it cannot see: the comparison uses the local
    `origin/<default>` tracking ref, so a clone that has not fetched is measured against what
    it last knew. `problem` is set when there is no HEAD or no remote branch to compare with --
    both are states an operator must fix, not silence.
    """
    path = Path(repo) if repo is not None else repo_root()
    if path is None:
        return CheckoutState(note=NOT_A_CHECKOUT)

    code, head = _git(path, "rev-parse", "HEAD")
    if code == 127:
        return CheckoutState(
            path=path, problem=f"git is not on PATH: cannot read HEAD in {path}",
            fix_text="install git: the checkout cannot be read without it")
    if code != 0 or not head:
        return CheckoutState(path=path,
                             problem=f"cannot read HEAD in {path}: is it a git checkout?",
                             fix_text=f"cd {path} && git status")
    _code, short = _git(path, "rev-parse", "--short", "HEAD")
    code, ref = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    detached = code != 0 or ref == "HEAD"
    default = _default_branch(path)

    code, _remote = _git(path, "rev-parse", "--verify", "--quiet", f"origin/{default}")
    if code != 0:
        return CheckoutState(
            path=path, branch="" if detached else ref, commit=short or head[:7],
            default_branch=default, detached=detached,
            problem=f"{path} has no origin/{default}: `git fetch origin {default}`",
            fix_text=f"cd {path} && git fetch origin {default}")

    behind = _count(path, "HEAD", f"origin/{default}")
    ahead = _count(path, f"origin/{default}", "HEAD")
    if behind is None or ahead is None:
        return CheckoutState(
            path=path, branch="" if detached else ref, commit=short or head[:7],
            default_branch=default, detached=detached,
            problem=f"cannot compare HEAD with origin/{default} in {path}",
            fix_text=f"cd {path} && git fetch origin {default}")

    return CheckoutState(path=path, branch="" if detached else ref, commit=short or head[:7],
                         default_branch=default, behind=behind, ahead=ahead, detached=detached)
