"""`hng deps`: the scheduled dependency pipeline, as a command instead of a shell script.

One run, per configured repository: refresh the bot's clone, ask OSV what is vulnerable at
or above the CVSS floor, let `osv-scanner fix` propose in-place bump, run the repository's
own gates against the result, and publish what survives as a `pr/deps-<date>` branch — the
`nostr://` remote turns that branch into a pull request. A bump that fails a gate is
discarded rather than proposed, and a proposal identical to the last one is not repeated
while it is still waiting for review.

A proposal is work, not only a branch: publishing one also files a review task on the
gateway's board, keyed `deps:pr:<event id>` so a re-run of the scan is a no-op, and the
merge -- seen on a later run as the `Merge #<short>:` commit a kind-1631 names on the
target branch -- is what completes that task.

It is deliberately **not** part of the daemon's poll loop. A run spends minutes in npm,
cargo and docker; the relay must not be able to stall behind it. `deploy/nostr-deps.service`
is a separate oneshot that runs this command, on its own timer.

The `deps:` block in `config.yaml` carries every site-specific fact: the repositories and
their owners, the CVSS floor, the workspace, the signer npub, the board a proposal is filed
on, and whether proposals are published at all. Nothing about a particular box is compiled
in.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence, TextIO

from .kanban import Board, BoardError
from .ngit import board_path, event_hex

#: Where the bot's clones live. Kept across runs so a proposal is a fetch, not a clone.
DEFAULT_WORKSPACE = "/opt/deps-bot"
#: The last proposal's lockfile hash per repository, so a pending bump is not repeated.
DEFAULT_STATE_DIR = "/var/lib/nostr-deps"
DEFAULT_LOG = "/var/log/nostr-deps.log"
#: CVSS floor: 7 is "high and above".
DEFAULT_MIN_SEVERITY = 7.0
DEFAULT_WORKFLOW = ".ngit/act/workflows/dependencies.yml"
DEFAULT_COMMIT_NAME = "day.ag deps bot"
DEFAULT_COMMIT_EMAIL = "deps-bot@day.ag"
#: Who runs a proposal's review task. A review, not an implementation: the same profile the
#: ngit bridge hands its PR reviews to.
DEFAULT_REVIEW_ASSIGNEE = "default"

#: The branch every run is prepared on; renamed to `pr/deps-<date>` only when publishing.
WORK_BRANCH = "work/deps"
#: The prefix every published proposal's branch carries, which is also what makes an open
#: proposal on a repository recognisably the bot's own.
PROPOSAL_PREFIX = "pr/deps-"

_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class DepsConfigError(ValueError):
    """The `deps:` block cannot be used as written."""


@dataclass(frozen=True)
class DepsRepo:
    """One repository a proposal may be made in: a name and who published it."""

    name: str
    owner: str
    branch: str = "main"

    @property
    def address(self) -> str:
        return f"nostr://{self.owner}/{self.name}"


@dataclass(frozen=True)
class DepsConfig:
    """Everything `hng deps` reads from `config.yaml` under `deps:`."""

    repositories: tuple[DepsRepo, ...] = ()
    min_severity: float = DEFAULT_MIN_SEVERITY
    workspace: Path = Path(DEFAULT_WORKSPACE)
    state_dir: Path = Path(DEFAULT_STATE_DIR)
    log: Path = Path(DEFAULT_LOG)
    signer: str = ""
    coordinator: str = ""
    workflow: str = DEFAULT_WORKFLOW
    open_prs: bool = True
    commit_name: str = DEFAULT_COMMIT_NAME
    commit_email: str = DEFAULT_COMMIT_EMAIL
    #: The board a proposal's review task is filed on, or "" for none. Empty is the honest
    #: default: a board is a deployment's choice, and a run that guesses one files work in
    #: the wrong place.
    board: str = ""
    #: The profile that runs the review. A review card with no assignee is never
    #: dispatched, so this is never left empty when a board is set.
    review_assignee: str = DEFAULT_REVIEW_ASSIGNEE

    @property
    def files_tasks(self) -> bool:
        """Whether a proposal gets a card: only a board makes it work, not a message."""
        return bool(self.board)


_KEYS = {
    "repositories", "min_severity", "workspace", "state_dir", "log", "signer",
    "coordinator", "workflow", "open_prs", "commit_name", "commit_email", "board",
    "review_assignee",
}
_REPO_KEYS = {"name", "owner", "branch"}


def _text(raw: dict[str, Any], key: str, default: str = "") -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise DepsConfigError(f"deps.{key} must be a string: {value!r}")
    return value.strip()


def _directory(raw: dict[str, Any], key: str, default: str) -> Path:
    value = _text(raw, key, default) or default
    return Path(os.path.expanduser(value))


def _repo(entry: Any, index: int) -> DepsRepo:
    where = f"deps.repositories[{index}]"
    if not isinstance(entry, dict):
        raise DepsConfigError(f"{where} must be a mapping with name and owner: {entry!r}")
    unknown = sorted(set(entry) - _REPO_KEYS)
    if unknown:
        raise DepsConfigError(f"{where} has unknown key(s): {', '.join(unknown)}")
    name = _text(entry, "name")
    owner = _text(entry, "owner")
    if not name:
        raise DepsConfigError(f"{where}.name is required")
    if not _REPO_NAME.match(name) or name in (".", ".."):
        raise DepsConfigError(
            f"{where}.name must be a plain directory name (letters, digits, . _ -): {name!r}")
    if not owner:
        raise DepsConfigError(f"{where}.owner is required (who published the repository)")
    return DepsRepo(name=name, owner=owner, branch=_text(entry, "branch", "main") or "main")


def load_deps_config(cfg: dict[str, Any]) -> DepsConfig:
    """Read the `deps:` block, loudly: a typo is a config error, not a silent default.

    A missing block is an error rather than an empty pipeline: `hng deps run` with nothing
    configured would be a unit that reports success forever.
    """
    raw = cfg.get("deps")
    if raw is None:
        raise DepsConfigError(
            "no `deps:` block in the config; add one (see config.example.yaml) before "
            "scheduling `hng deps run`")
    if not isinstance(raw, dict):
        raise DepsConfigError(f"deps must be a mapping: {raw!r}")
    unknown = sorted(set(raw) - _KEYS)
    if unknown:
        raise DepsConfigError(f"unknown key(s) under deps: {', '.join(unknown)}")

    entries = raw.get("repositories")
    if not isinstance(entries, list) or not entries:
        raise DepsConfigError("deps.repositories must be a non-empty list")
    repositories = tuple(_repo(entry, index) for index, entry in enumerate(entries))
    names = [repo.name for repo in repositories]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise DepsConfigError(
            f"deps.repositories names must be unique: {', '.join(duplicates)}")

    floor = raw.get("min_severity", DEFAULT_MIN_SEVERITY)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)):
        raise DepsConfigError(f"deps.min_severity must be a number: {floor!r}")
    if not 0 <= float(floor) <= 10:
        raise DepsConfigError(f"deps.min_severity is a CVSS score (0-10): {floor!r}")

    open_prs = raw.get("open_prs", True)
    if not isinstance(open_prs, bool):
        raise DepsConfigError(f"deps.open_prs must be true or false: {open_prs!r}")

    workflow = _text(raw, "workflow", DEFAULT_WORKFLOW) or DEFAULT_WORKFLOW
    board = _text(raw, "board")
    if board and not re.fullmatch(r"[a-z0-9][a-z0-9._-]*", board):
        raise DepsConfigError(
            f"deps.board must be a board slug (lowercase letters, digits, . _ -): {board!r}")
    assignee = _text(raw, "review_assignee", DEFAULT_REVIEW_ASSIGNEE) or DEFAULT_REVIEW_ASSIGNEE
    return DepsConfig(
        repositories=repositories,
        min_severity=float(floor),
        workspace=_directory(raw, "workspace", DEFAULT_WORKSPACE),
        state_dir=_directory(raw, "state_dir", DEFAULT_STATE_DIR),
        log=_directory(raw, "log", DEFAULT_LOG),
        signer=_text(raw, "signer"),
        coordinator=_text(raw, "coordinator"),
        workflow=workflow,
        open_prs=open_prs,
        commit_name=_text(raw, "commit_name", DEFAULT_COMMIT_NAME) or DEFAULT_COMMIT_NAME,
        commit_email=_text(raw, "commit_email", DEFAULT_COMMIT_EMAIL) or DEFAULT_COMMIT_EMAIL,
        board=board,
        review_assignee=assignee,
    )


# -- the pipeline -------------------------------------------------------------

@dataclass
class Result:
    """What one command returned, without ever raising at the caller."""

    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    def detail(self) -> str:
        lines = (self.stderr or self.stdout or "").strip().splitlines()
        return lines[-1][:300] if lines else "no output"


def _timestamp() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _default_path() -> str:
    """The PATH the unit documents; a hand run behaves the same as a scheduled one."""
    home = Path.home()
    return os.pathsep.join([str(home / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"])


def findings(report: str | Path, floor: float) -> str:
    """How many distinct findings are at or above `floor` in an osv-scanner JSON report.

    `"?"` when the report cannot be read: a scan that produced nothing must not read as
    "zero findings" and silently drop a repository.
    """
    try:
        data = json.loads(Path(report).read_text())
    except (OSError, ValueError):
        return "?"
    seen: set[tuple[Any, Any, Any]] = set()
    for result in data.get("results") or []:
        for entry in result.get("packages") or []:
            package = entry.get("package") or {}
            for group in entry.get("groups") or []:
                try:
                    score = float(group.get("max_severity") or 0)
                except (TypeError, ValueError):
                    score = 0.0
                if score >= floor:
                    for vid in group.get("ids") or []:
                        seen.add((package.get("name"), package.get("version"), vid))
    return str(len(seen))


def cleared_advisories(before: str | Path, after: str | Path, floor: float) -> str:
    """The advisory ids the bump clears, with links, so a proposal says what it fixes."""

    def rows(path: str | Path) -> set[tuple[Any, Any, Any]]:
        try:
            data = json.loads(Path(path).read_text())
        except (OSError, ValueError):
            return set()
        out: set[tuple[Any, Any, Any]] = set()
        for result in data.get("results") or []:
            for entry in result.get("packages") or []:
                package = entry.get("package") or {}
                for group in entry.get("groups") or []:
                    try:
                        score = float(group.get("max_severity") or 0)
                    except (TypeError, ValueError):
                        score = 0.0
                    if score < floor:
                        continue
                    for vid in group.get("ids") or []:
                        out.add((package.get("name"), package.get("version"), vid))
        return out

    cleared = sorted(rows(before) - rows(after), key=lambda row: (row[0] or "", row[2]))
    shown: list[str] = []
    seen: set[Any] = set()
    for name, version, vid in cleared:
        if vid in seen:
            continue
        seen.add(vid)
        shown.append(f"{vid} ({name} {version}) https://osv.dev/vulnerability/{vid}")
    return "; ".join(shown)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _count(value: str, word: str) -> str:
    """`1 finding`, `2 findings` — a proposal's description should read as prose."""
    return f"{value} {word}" if value == "1" else f"{value} {word}s"


# -- the proposal, and the card it becomes ------------------------------------

class DepsError(RuntimeError):
    """A step of the run could not read what it needed; the message says what to fix."""


@dataclass
class Proposal:
    """The pull request a run published: its identity, and what the scan measured.

    Written to `state_dir/proposal-<name>.json` **before** the PR is looked up, so a run that
    could not name the PR (a relay that had not caught up yet) still knows the branch and the
    counts when it tries again. The repository stays the record of the PR's state: this file
    only says which proposal the run last made.
    """

    branch: str
    pr_id: str = ""
    nevent: str = ""
    subject: str = ""
    author: str = ""
    commit: str = ""
    findings_before: str = ""
    findings_after: str = ""
    clears: str = ""
    task_id: str = ""
    #: The commit the merge landed under, once a run has seen it.
    merged: str = ""
    #: The proposal left the queue without a merge; the card was handed to a person once.
    abandoned: bool = False

    @property
    def short(self) -> str:
        return self.pr_id[:8]

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    @classmethod
    def from_dict(cls, data: Any) -> Optional["Proposal"]:
        """A record read back from disk, or None when it does not name a branch."""
        if not isinstance(data, dict):
            return None
        branch = str(data.get("branch") or "")
        if not branch:
            return None
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


def task_key(pr_id: str) -> str:
    """The dedupe key: one card per proposal event, however many runs see it."""
    return f"deps:pr:{pr_id}"


def task_title(pr_id: str) -> str:
    """`Review PR <short>: dependency bumps` — the shape a reviewer scans a column for."""
    return f"Review PR {str(pr_id or '')[:8]}: dependency bumps"


def task_body(repo: DepsRepo, proposal: Proposal, cfg: DepsConfig) -> str:
    """What a reviewer needs to judge a dependency proposal, and the one thing they must not do.

    Modelled on the ngit bridge's review brief (`ngit.review_body`): the facts in a fixed
    block, the steps, then the rule that the merge -- not the reviewer -- finishes the card.
    The findings line is the scan's own numbers, so the card says what the bump is worth
    without anyone having to re-derive it.
    """
    target = repo.branch or "(the repository's default branch)"
    source = proposal.branch or PROPOSAL_PREFIX + "<date>"
    if proposal.findings_before or proposal.findings_after:
        findings = (f"{proposal.findings_before} -> {proposal.findings_after} at or above CVSS "
                    f"{cfg.min_severity:g}")
    else:
        findings = "not recorded (filed from the proposal that was already open)"
    subject = proposal.subject or "Dependency bumps"
    lines = [
        f'PR "{subject}" by {proposal.author or "the deps bot"}',
        "",
        f"PR:         {proposal.pr_id or '(not named yet)'}",
        f"nevent:     {proposal.nevent or proposal.pr_id or '(not named yet)'}",
        f"Repository: {repo.address}",
        f"Author:     {proposal.author or 'the deps bot'}",
        f"Source:     {source}",
        f"Target:     {target}",
        f"Findings:   {findings}",
        *([f"Clears:     {proposal.clears}"] if proposal.clears else []),
        "",
        "---",
        "",
        "Review this PR and report a verdict. Do not merge it: merging is the operator's, or",
        "a follow-up task. This task ends with a verdict, not a merge.",
        "",
        "This card is finished by the merge, not by you: `done` requires the PR to be",
        "`applied`, and `hng deps run` completes the card when it sees the merge commit. **Do",
        "not call kanban_complete** -- the board will refuse it. Record the verdict as a",
        "comment, then park the card with `kanban_block`, putting the verdict and the tip it",
        "covers in the reason.",
        "",
        f"1. Check the proposal out in a scratch clone -- it belongs to {repo.address}, not to",
        f"   this board's checkout: `ngit pr checkout {proposal.nevent or proposal.pr_id}`,",
        f"   then read it against {target}: `git diff {target}...HEAD`.",
        "2. Expect the manifest and lockfile only, and expect the advisories above to be gone:",
        f"   `osv-scanner scan source -r .` at CVSS {cfg.min_severity:g} reproduces the number",
        "   in the findings line. Say what it printed.",
        "3. Run the repository's own gates from that tree. The deps run already ran its",
        "   install, typecheck, lint, tests, build and start-the-server check before proposing,",
        "   so say what you re-ran and what it printed, including anything already failing.",
        "4. Publish the verdict on the PR, then report it on this task, starting both with",
        "   exactly one of these lines:",
        "",
        "       Verdict: approved",
        "       Verdict: changes-requested",
        "",
        f"   Publish it with `ngit pr comment {proposal.nevent or proposal.pr_id} --body "
        "'Verdict: ...'`",
        "   and name the comment's event id in your note here.",
    ]
    return "\n".join(lines)


def _document(text: str) -> Any:
    """The JSON document in a command's output.

    `ngit` writes progress lines (`no updates`) to stdout before the document, so the first
    line that opens a document is where parsing starts. None when nothing parses.
    """
    lines = str(text or "").splitlines()
    for index, line in enumerate(lines):
        if line[:1] in ("[", "{"):
            try:
                return json.loads("\n".join(lines[index:]))
            except ValueError:
                return None
    return None


def open_branch(display: str) -> str:
    """`ngit pr list` spells a proposal's branch `pr/deps-<date>(<short>)`; this is the name."""
    return re.sub(r"\([0-9a-f]{8}\)$", "", str(display or "").strip())


class Pipeline:
    """One `hng deps run`: every configured repository, in order, with a shared log."""

    def __init__(self, cfg: DepsConfig, *, dry_run: bool = False,
                 only_repo: str | None = None, out: TextIO | None = None,
                 env: dict[str, str] | None = None) -> None:
        self.cfg = cfg
        self.dry_run = dry_run
        self.only_repo = only_repo
        self.out = out or sys.stdout
        self.env = dict(os.environ)
        self.env.update({"PATH": _default_path(), "NGIT_PAGER": "cat",
                         "GIT_TERMINAL_PROMPT": "0"})
        if env:
            self.env.update(env)
        self.failures: list[str] = []
        self._log_broken = False

    # -- reporting ----------------------------------------------------------

    def say(self, message: str) -> None:
        line = f"[{_timestamp()}] {message}"
        print(line, file=self.out, flush=True)
        try:
            with open(self.cfg.log, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            if not self._log_broken:
                self._log_broken = True
                print(f"[{_timestamp()}] (could not write {self.cfg.log}: {exc.strerror})",
                      file=self.out, flush=True)

    def _append_log(self, text: str) -> None:
        try:
            with open(self.cfg.log, "a", encoding="utf-8") as fh:
                fh.write(text)
        except OSError as exc:
            if not self._log_broken:
                self._log_broken = True
                self.say(f"could not write {self.cfg.log}: {exc.strerror}")

    # -- running commands ---------------------------------------------------

    def run_cmd(self, argv: Sequence[str], *, cwd: Path | None = None, timeout: float = 120,
                to_log: bool = False, extra_env: dict[str, str] | None = None,
                stdout: Any = subprocess.PIPE) -> Result:
        """Run one command; a missing binary or a timeout reads as a failed result."""
        env = dict(self.env)
        if extra_env:
            env.update(extra_env)
        try:
            done = subprocess.run(list(argv), cwd=str(cwd) if cwd else None, env=env,
                                  stdout=stdout, stderr=subprocess.PIPE if stdout is
                                  subprocess.PIPE else subprocess.STDOUT,
                                  text=True, timeout=timeout)
        except FileNotFoundError:
            return Result(127, "", f"{argv[0]}: command not found")
        except subprocess.TimeoutExpired:
            return Result(124, "", f"timed out after {timeout:.0f}s")
        except OSError as exc:
            return Result(126, "", f"{argv[0]}: {exc.strerror}")
        result = Result(done.returncode, done.stdout or "", done.stderr or "")
        if to_log:
            self._append_log(f"$ {' '.join(argv)}\n{result.stdout}{result.stderr}")
        return result

    # -- one repository -----------------------------------------------------

    def checkout(self, repo: DepsRepo, directory: Path) -> bool:
        if not (directory / ".git").is_dir():
            self.say(f"    cloning {repo.name}")
            clone = self.run_cmd(["git", "clone", "--quiet", repo.address, str(directory)],
                                 timeout=600, to_log=True)
            if not clone.ok:
                self.say(f"    clone failed ({clone.detail()}), skipping")
                self.failures.append(f"{repo.name}: clone failed")
                return False
        fetch = self.run_cmd(["git", "fetch", "--quiet", "origin"], cwd=directory,
                             timeout=300, to_log=True)
        if not fetch.ok:
            self.say(f"    fetch failed ({fetch.detail()}), skipping")
            self.failures.append(f"{repo.name}: fetch failed")
            return False
        start = self.run_cmd(["git", "checkout", "--quiet", "-B", WORK_BRANCH,
                              f"origin/{repo.branch}"], cwd=directory, timeout=120, to_log=True)
        if not start.ok:
            self.say(f"    cannot check out origin/{repo.branch} ({start.detail()})")
            self.failures.append(f"{repo.name}: checkout failed")
            return False
        return True

    def restore(self, repo: DepsRepo, directory: Path) -> None:
        """Leave the checkout as it was found: no bump, no proposal branch, no diff."""
        self.run_cmd(["git", "checkout", "--quiet", "--", "."], cwd=directory, to_log=True)
        self.run_cmd(["git", "checkout", "--quiet", "-B", WORK_BRANCH,
                      f"origin/{repo.branch}"], cwd=directory, to_log=True)

    def scan(self, directory: Path, report: Path) -> str:
        scanned = self.run_cmd(
            ["osv-scanner", "scan", "source", "-r", "--allow-no-lockfiles", "--format", "json",
             "--output-file", str(report), "."],
            cwd=directory, timeout=400, to_log=True)
        count = findings(report, self.cfg.min_severity)
        if count == "?":
            # osv-scanner exits non-zero when it *found* something, so only an unreadable
            # report is a failure; a plain "found 5" is the normal case.
            self.say(f"    scan failed ({scanned.detail()})")
        return count

    def gates(self, directory: Path, log: Path) -> str:
        """Run the repository's own gates; return the failing gate's name, or ""."""
        def gate(argv: Sequence[str], timeout: float) -> bool:
            with open(log, "a", encoding="utf-8") as sink:
                result = self.run_cmd(argv, cwd=directory, timeout=timeout, stdout=sink)
            return result.ok

        if not gate(["npm", "ci", "--no-audit", "--no-fund"], 1800):
            return "npm ci"
        if (directory / "tsconfig.app.json").exists():
            if not gate(["npx", "tsc", "-p", "tsconfig.app.json", "--noEmit"], 600):
                return "typecheck"
        if (any(directory.glob("eslint.config.*"))
                or (directory / ".eslintrc.json").exists()
                or (directory / ".eslintrc.cjs").exists()):
            if not gate(["npx", "eslint"], 600):
                return "lint"
        if any(directory.glob("vitest.config.*")) or any(directory.glob("vite.config.*")):
            if not gate(["npx", "vitest", "run"], 1800):
                return "unit tests"
        if any(directory.glob("vite.config.*")):
            if not gate(["npx", "vite", "build"], 1800):
                return "build"
        if (directory / "server.js").exists() and self._has_start_script(directory):
            if not self._serves(directory, log):
                return "server start"
        return ""

    @staticmethod
    def _has_start_script(directory: Path) -> bool:
        try:
            data = json.loads((directory / "package.json").read_text())
        except (OSError, ValueError):
            return False
        return bool((data.get("scripts") or {}).get("start"))

    def _serves(self, directory: Path, log: Path) -> bool:
        """Start the server once and ask it for a page: a bump can build and then crash."""
        port = _free_port()
        env = dict(self.env, PORT=str(port))
        try:
            with open(log, "a", encoding="utf-8") as sink:
                server = subprocess.Popen(["node", "server.js"], cwd=str(directory), env=env,
                                          stdout=sink, stderr=subprocess.STDOUT)
        except OSError as exc:
            self._append_log(f"server start: {exc}\n")
            return False
        try:
            for _ in range(8):
                time.sleep(1)
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5):
                        return True
                except (urllib.error.URLError, OSError, ValueError):
                    if server.poll() is not None:
                        return False
            return False
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)

    def _keep_build_log(self, repo: DepsRepo, scratch: Path) -> Path:
        """Put a failed gate's output somewhere the message can still be read afterwards.

        The gates write into the run's scratch directory, and that directory is removed as
        soon as `one()` returns - while the journal keeps only the message naming the file.
        A failure therefore copies the output to `state_dir/build-<name>.log` (the file the
        retired shell script kept in /tmp) before the scratch directory goes.
        """
        kept = self.cfg.state_dir / f"build-{repo.name}.log"
        try:
            body = scratch.read_bytes()
        except OSError:
            body = b""
        try:
            kept.parent.mkdir(parents=True, exist_ok=True)
            kept.write_bytes(f"=== {repo.name} at {_timestamp()} ===\n".encode() + body)
            return kept
        except OSError as exc:
            # Not fatal: the run still has to discard the bump. The run log always exists.
            self.say(f"    cannot keep the gate log at {kept} ({exc.strerror}); "
                     f"appending it to {self.cfg.log}")
            self._append_log(body.decode("utf-8", "replace"))
            return self.cfg.log

    def proposal_state(self, repo: DepsRepo, directory: Path) -> str:
        lockfile = directory / "package-lock.json"
        try:
            return hashlib.sha256(lockfile.read_bytes()).hexdigest()
        except OSError:
            return ""

    def publish(self, repo: DepsRepo, directory: Path, branch: str, title: str,
                description: str, proposal_hash: str) -> bool:
        configured = self.run_cmd(["git", "config", "nostr.signer", self.cfg.signer],
                                  cwd=directory, timeout=60, to_log=True)
        if not configured.ok:
            self.say(f"    cannot set nostr.signer ({configured.detail()})")
            self.failures.append(f"{repo.name}: signer not set")
            return False
        pushed = self.run_cmd(["git", "push", "-o", f"title={title}",
                               "-o", f"description={description}", "-u", "origin", branch],
                              cwd=directory, timeout=400, to_log=True)
        if not pushed.ok:
            self.say(f"    push failed for {branch} ({pushed.detail()})")
            self.failures.append(f"{repo.name}: push failed")
            return False
        try:
            self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
            (self.cfg.state_dir / f"last-{repo.name}").write_text(proposal_hash + "\n")
        except OSError as exc:
            self.say(f"    (could not record the proposal state: {exc.strerror})")
        return True

    # -- the card a proposal becomes ----------------------------------------

    def proposals(self, directory: Path) -> dict[str, dict[str, Any]]:
        """The repository's open proposals, keyed by the branch each was cut from.

        `ngit pr list` is the queue's own answer, so the board follows what the repository
        still holds rather than what a previous run remembers. Raises when the queue cannot
        be read: a card filed from a guess is worse than one filed on the next run.
        """
        listed = self.run_cmd(["ngit", "pr", "list", "--json"], cwd=directory, timeout=180)
        if not listed.ok:
            raise DepsError(f"ngit pr list failed ({listed.detail()})")
        data = _document(listed.stdout)
        if not isinstance(data, list):
            raise DepsError(f"ngit pr list printed no JSON ({listed.detail()})")
        found: dict[str, dict[str, Any]] = {}
        for item in data:
            if not isinstance(item, dict):
                continue
            branch = open_branch(str(item.get("branch") or ""))
            pr_id = event_hex(str(item.get("id") or ""))
            if not branch or not pr_id:
                continue
            found[branch] = {"pr_id": pr_id, "nevent": str(item.get("id") or ""),
                             "subject": str(item.get("subject") or ""),
                             "author": str(item.get("author") or ""),
                             "status": str(item.get("status") or "")}
        return found

    def merged_commit(self, repo: DepsRepo, directory: Path, short: str) -> str:
        """The commit a proposal's merge landed under, or "" while it is still open.

        A kind-1631 names a merge commit, and ngit's apply route writes that commit onto the
        target branch as `Merge #<short>: <subject>` -- measured against a live 1631, whose
        `merge-commit-id` was the same object. The branch is where a run can see the merge
        without reading the relays, and the queue's own status lags it (a proposal applied as
        patches still reads `open`), so the commit is the evidence the card turns on.
        """
        if not short:
            return ""
        found = self.run_cmd(["git", "log", f"origin/{repo.branch}", "--format=%H",
                              "--grep", f"^Merge #{short}:", "-1"], cwd=directory, timeout=120)
        if not found.ok:
            return ""
        lines = [line.strip() for line in found.stdout.splitlines() if line.strip()]
        return lines[0] if lines else ""

    def branch_tip(self, directory: Path, branch: str) -> str:
        tip = self.run_cmd(["git", "rev-parse", branch], cwd=directory, timeout=60)
        return tip.stdout.strip() if tip.ok else ""

    def proposal_record(self, repo: DepsRepo) -> Optional[Proposal]:
        """The last proposal this bot published for the repository, as the run recorded it."""
        path = self.cfg.state_dir / f"proposal-{repo.name}.json"
        try:
            return Proposal.from_dict(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None

    def save_proposal(self, repo: DepsRepo, proposal: Proposal) -> None:
        path = self.cfg.state_dir / f"proposal-{repo.name}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(proposal.as_dict(), indent=2) + "\n")
        except OSError as exc:
            self.say(f"    (could not record the proposal in {path}: {exc.strerror})")

    def file_task(self, repo: DepsRepo, proposal: Proposal) -> str:
        """The proposal's one review card, or "" when it could not be filed.

        Keyed `deps:pr:<event id>`, exactly as the ngit bridge keys a PR's card, so a re-run
        of the scan -- or a retry after a board that was locked -- lands on the same row
        instead of a second one. A guard goes with it: `done` requires the PR to be applied,
        so the card cannot be closed before the merge that finishes it.
        """
        if not self.cfg.files_tasks:
            return ""
        key = task_key(proposal.pr_id)
        try:
            with Board(board_path(self.cfg.board)) as board:
                found = board.by_idempotency(key)
                if found is not None:
                    # The card is the PR's for its life; keep its guard on the tip this run
                    # knows, so a reviewer reads the revision the verdict will cover.
                    if proposal.commit:
                        board.guard_pr(str(found["id"]), pr_id=proposal.pr_id, state="open",
                                       tip=proposal.commit)
                    return str(found["id"])
                task_id = board.create(
                    title=task_title(proposal.pr_id), body=task_body(repo, proposal, self.cfg),
                    assignee=self.cfg.review_assignee, status="ready",
                    idempotency_key=key, created_by="deps")
                board.guard_pr(task_id, pr_id=proposal.pr_id, state="open", tip=proposal.commit)
        except (BoardError, sqlite3.Error) as exc:
            self.say(f"    could not file the review task ({exc})")
            self.failures.append(f"{repo.name}: review task not filed ({exc})")
            return ""
        self.say(f"    review task {task_id} filed for PR {proposal.short}")
        return task_id

    def finish_task(self, repo: DepsRepo, proposal: Proposal, merge: str) -> bool:
        """The merge completes the card, naming the commit -- once, however often it is seen."""
        if not self.cfg.files_tasks:
            return True
        key = task_key(proposal.pr_id)
        try:
            with Board(board_path(self.cfg.board)) as board:
                card = board.by_idempotency(key) or board.task(proposal.task_id)
                if card is None:
                    self.say(f"    PR {proposal.short} merged as {merge}: its card is gone")
                    return True
                task_id = str(card["id"])
                if str(card.get("status") or "") in ("done", "archived"):
                    return True
                # The guard is the board's own refusal of an early completion; a merge is the
                # one thing that releases it, and it must happen before the completion write.
                board.release_guard(task_id, merge_commit=merge)
                board.complete(
                    task_id, author="deps",
                    comment=(f"PR {proposal.short} merged as {merge}\n"
                             f"{proposal.nevent or proposal.pr_id}\n"
                             f"The dependency bump is in {repo.branch}; this card is closed by"
                             " the merge."),
                    result=(f"PR {proposal.pr_id} merged as {merge}: the bump is in "
                            f"{repo.branch}."))
        except (BoardError, sqlite3.Error) as exc:
            self.say(f"    could not complete the card for PR {proposal.short} ({exc})")
            self.failures.append(f"{repo.name}: card for merged PR {proposal.short} not closed")
            return False
        self.say(f"    PR {proposal.short} merged as {merge}: its card is complete")
        return True

    def abandon_task(self, repo: DepsRepo, proposal: Proposal) -> bool:
        """The proposal left the queue without a merge: hand the card to a person, once."""
        if not self.cfg.files_tasks:
            return True
        key = task_key(proposal.pr_id)
        try:
            with Board(board_path(self.cfg.board)) as board:
                card = board.by_idempotency(key) or board.task(proposal.task_id)
                if card is None or str(card.get("status") or "") in ("done", "archived",
                                                                      "blocked"):
                    return True
                board.block(
                    str(card["id"]), kind="dependency", author="deps",
                    comment=(f"PR {proposal.short} is no longer open, and there is no"
                             f" `Merge #{proposal.short}:` commit on origin/{repo.branch}: it"
                             " was withdrawn rather than merged. Nothing landed, so this card"
                             " cannot be completed by it. A person decides: re-open the"
                             " proposal, or drop the card."))
        except (BoardError, sqlite3.Error) as exc:
            self.say(f"    could not park the card for PR {proposal.short} ({exc})")
            self.failures.append(
                f"{repo.name}: card for withdrawn PR {proposal.short} not parked")
            return False
        self.say(f"    PR {proposal.short} was withdrawn without a merge: its card is parked")
        return True

    @staticmethod
    def _newest_open(open_prs: dict[str, dict[str, Any]]) -> str:
        """The newest open `pr/deps-*` proposal, or "" -- the branch name carries the date."""
        return max((name for name in open_prs if name.startswith(PROPOSAL_PREFIX)), default="")

    def reconcile(self, repo: DepsRepo, directory: Path) -> Optional[Proposal]:
        """What the last proposal is now: a merge finishes its card, a close parks it.

        The repository is the record, not the state file: `ngit pr list` says which proposals
        are open, and a merged one is a `Merge #<short>:` commit on the target branch. So a
        run after a restart, a lost state directory or a week of downtime picks the thread up
        where the repository left it -- and a card whose proposal landed is closed rather
        than left waiting for a merge that already happened.

        An open proposal is returned, not filed: the card is written later in the run, with
        the findings this run's scan measured, so the counts on it are never a guess.
        """
        if not self.cfg.files_tasks:
            return None
        record = self.proposal_record(repo)
        try:
            open_prs = self.proposals(directory)
        except DepsError as exc:
            self.say(f"    cannot read the repository's proposals ({exc})")
            self.failures.append(f"{repo.name}: {exc}")
            return record
        if record is None:
            branch = self._newest_open(open_prs)
            if not branch:
                return None
            entry = open_prs[branch]
            if self.cfg.signer and entry["author"] and entry["author"] != self.cfg.signer:
                self.say(f"    {branch} is not this bot's proposal ({entry['author']});"
                         " leaving it alone")
                return None
            record = Proposal(branch=branch, pr_id=entry["pr_id"], nevent=entry["nevent"],
                              subject=entry["subject"], author=entry["author"])
            self.say(f"    adopting the open proposal {branch} (PR {record.short})")
        if record.merged:
            return None  # seen and closed on an earlier run
        merge = self.merged_commit(repo, directory, record.short)
        if merge:
            if self.finish_task(repo, record, merge):
                record.merged = merge
                self.save_proposal(repo, record)
            return None
        entry = open_prs.get(record.branch)
        if entry is None:
            if record.abandoned or not record.pr_id:
                return None
            if self.abandon_task(repo, record):
                record.abandoned = True
                self.save_proposal(repo, record)
            return None
        # Still open. The queue is fresher than the record for the PR's identity; the card
        # itself is filed later in the run, with the counts this run's scan measured.
        record.pr_id = entry["pr_id"]
        record.nevent = entry["nevent"]
        record.subject = record.subject or entry["subject"]
        record.author = record.author or entry["author"]
        if not record.commit:
            # An adopted proposal has no tip in the record; the clone's own branch is where
            # the tip the PR published still is.
            record.commit = self.branch_tip(directory, record.branch)
        self.save_proposal(repo, record)
        return record

    def record_published(self, repo: DepsRepo, directory: Path, branch: str, *,
                         before: str, after: str, clears: str) -> Optional[Proposal]:
        """Write down the proposal the run just pushed, and file the card it is owed.

        The record is saved *before* the PR is looked up: a relay that has not caught up must
        not cost the run the counts the scan measured. A lookup that fails leaves the branch
        in the record, and the next run adopts it (`reconcile`) and files the card then.
        """
        proposal = Proposal(branch=branch, commit=self.branch_tip(directory, branch),
                            findings_before=before, findings_after=after, clears=clears)
        previous = self.proposal_record(repo)
        self.save_proposal(repo, proposal)
        try:
            open_prs = self.proposals(directory)
        except DepsError as exc:
            self.say(f"    cannot read the proposal back ({exc}); its card is filed on the"
                     " next run")
            self.failures.append(f"{repo.name}: PR for {branch} not read ({exc})")
            return proposal
        entry = open_prs.get(branch)
        if entry is None:
            self.say(f"    {branch} is pushed but `ngit pr list` does not name it yet; its card"
                     " is filed on the next run")
            self.failures.append(f"{repo.name}: no PR named for {branch} after pushing it")
            return proposal
        proposal.pr_id = entry["pr_id"]
        proposal.nevent = entry["nevent"]
        proposal.subject = entry["subject"]
        proposal.author = entry["author"]
        if (self.cfg.files_tasks and previous is not None and previous.task_id
                and previous.pr_id == proposal.pr_id and previous.commit
                and previous.commit != proposal.commit):
            self.note_revision(repo, previous, proposal)
        proposal.task_id = self.file_task(repo, proposal) or (
            previous.task_id if previous is not None
            and previous.pr_id == proposal.pr_id else "")
        self.save_proposal(repo, proposal)
        return proposal

    def note_revision(self, repo: DepsRepo, previous: Proposal, proposal: Proposal) -> None:
        """One comment on the card for a re-push of the same proposal: the tip moved.

        A re-run with a different bump publishes the same PR a second time (same day, same
        branch), so the card is the same card and its counts are stale. One comment names the
        new tip and the new numbers; the guard is re-armed on the tip a reviewer will read.
        """
        key = task_key(proposal.pr_id)
        body = (f"Revision: PR {proposal.short} tip is now {proposal.commit}\n"
                f"{proposal.nevent or proposal.pr_id}\n"
                f"Findings are {proposal.findings_before} -> {proposal.findings_after} at or"
                f" above CVSS {self.cfg.min_severity:g}. Review this tip: a verdict recorded"
                " before it covers the earlier revision.")
        try:
            with Board(board_path(self.cfg.board)) as board:
                card = board.by_idempotency(key) or board.task(previous.task_id)
                if card is None or str(card.get("status") or "") in ("done", "archived"):
                    return
                task_id = str(card["id"])
                board.comment(task_id, "deps", body)
                board.guard_pr(task_id, pr_id=proposal.pr_id, state="open",
                               tip=proposal.commit)
        except (BoardError, sqlite3.Error) as exc:
            self.say(f"    (could not note the revision on the card: {exc})")

    # -- the run ------------------------------------------------------------

    def run(self) -> int:
        if self.only_repo and self.only_repo not in {r.name for r in self.cfg.repositories}:
            configured = ", ".join(r.name for r in self.cfg.repositories)
            print(f"deps: no repository named {self.only_repo!r} (configured: {configured})",
                  file=sys.stderr)
            return 2
        self.say(f"=== dependency run starting (dry-run={int(self.dry_run)}) ===")
        for repo in self.cfg.repositories:
            if self.only_repo and repo.name != self.only_repo:
                continue
            self.one(repo)
        self.say("=== dependency run finished ===")
        if self.failures:
            for failure in self.failures:
                self.say(f"    failed: {failure}")
            return 1
        return 0

    def one(self, repo: DepsRepo) -> None:
        self.say(f"--- {repo.name} ({repo.owner}/{repo.branch})")
        directory = self.cfg.workspace / repo.name
        if not self.checkout(repo, directory):
            return
        # The last proposal first: a merge that landed since the last run finishes its card,
        # a withdrawn one parks it, and an open one is returned so this run's scan can file
        # its card with the numbers it measures.
        outstanding = self.reconcile(repo, directory) if not self.dry_run else None
        with tempfile.TemporaryDirectory(prefix=f"deps-{repo.name}-") as scratch:
            work = Path(scratch)
            if not self.dry_run and self.cfg.signer and self.cfg.coordinator:
                self.trigger(repo, directory)
            before = self.scan(directory, work / "before.json")
            self.say(f"    findings at or above {self.cfg.min_severity:g}: {before}")

            if not ((directory / "package.json").exists()
                    and (directory / "package-lock.json").exists()):
                self.say("    no npm lockfile here, nothing to bump")
                self.restore(repo, directory)
                return

            fixed = self.run_cmd(
                ["osv-scanner", "fix", "-M", "package.json", "-L", "package-lock.json",
                 "--strategy", "in-place", "--min-severity",
                 f"{self.cfg.min_severity:g}"],
                cwd=directory, timeout=900, to_log=True)
            diff = self.run_cmd(["git", "diff", "--quiet", "--", "package-lock.json",
                                 "package.json"], cwd=directory, timeout=60)
            if diff.ok:
                self.say(f"    no upgrades available (osv-scanner exit {fixed.returncode})")
                self.restore(repo, directory)
                return

            after = self.scan(directory, work / "after.json")
            changed = self._numstat(directory)
            cleared = cleared_advisories(work / "before.json", work / "after.json",
                                         self.cfg.min_severity)
            self.say(f"    proposal changes {before} -> {after} findings (diff {changed})")

            build_log = work / "build.log"
            failing = self.gates(directory, build_log)
            if failing:
                kept = self._keep_build_log(repo, build_log)
                self.say(f"    gates FAILED at {failing!r} - discarding the bump "
                         f"(see {kept})")
                self.restore(repo, directory)
                return
            self.say("    gates passed (install, typecheck, lint, tests, build)")

            proposal_hash = self.proposal_state(repo, directory)
            state_file = self.cfg.state_dir / f"last-{repo.name}"
            try:
                previous = state_file.read_text().strip()
            except OSError:
                previous = ""
            if previous and previous == proposal_hash:
                self.say("    same proposal as the last run - not repeating it")
                if outstanding is not None:
                    # The proposal is still open, so this is its card: the numbers are the
                    # ones this run just measured, which are the same bump.
                    outstanding.findings_before, outstanding.findings_after = before, after
                    outstanding.clears = cleared
                    outstanding.task_id = (self.file_task(repo, outstanding)
                                           or outstanding.task_id)
                    self.save_proposal(repo, outstanding)
                self.restore(repo, directory)
                return

            branch = f"pr/deps-{dt.datetime.now(dt.timezone.utc).date().isoformat()}"
            title = f"Dependency bumps ({dt.datetime.now(dt.timezone.utc).date().isoformat()})"
            description = (f"Bump vulnerable dependencies, as reported by the scan. "
                           f"osv-scanner found {_count(before, 'finding')} at or above CVSS "
                           f"{self.cfg.min_severity:g}; this proposal leaves {after}. "
                           "Typecheck, lint, tests and build all passed before it was opened.")
            if cleared:
                description += f" Clears: {cleared}"

            if self.dry_run:
                self.say(f"    dry run: would push {branch} ({before} -> {after} findings)")
                if cleared:
                    self.say(f"    dry run: would clear: {cleared}")
                self.restore(repo, directory)
                return
            if not self.cfg.open_prs:
                self.say(f"    deps.open_prs is false: {branch} ready, not pushed")
                self.restore(repo, directory)
                return
            if not self.cfg.signer:
                self.say("    deps.signer is not set: cannot push a proposal")
                self.failures.append(f"{repo.name}: no signer")
                self.restore(repo, directory)
                return

            self.commit(repo, directory, branch, self._message(before, changed, after))
            if self.publish(repo, directory, branch, title, description, proposal_hash):
                self.say(f"    pushed {branch} ({before} -> {after} findings)")
                self.record_published(repo, directory, branch, before=before, after=after,
                                      clears=cleared)

    def trigger(self, repo: DepsRepo, directory: Path) -> None:
        """Ask for this week's signed scan, whatever the bump result turns out to be."""
        triggered = self.run_cmd(
            ["ngit", "ci", "trigger", "--signer", self.cfg.signer, "--workflow",
             self.cfg.workflow, self.cfg.coordinator, "HEAD"],
            cwd=directory, timeout=180, to_log=True)
        if triggered.ok:
            self.say("    scan triggered")
        else:
            self.say(f"    scan trigger failed ({triggered.detail()})")

    def commit(self, repo: DepsRepo, directory: Path, branch: str, message: str) -> None:
        renamed = self.run_cmd(["git", "branch", "-m", WORK_BRANCH, branch], cwd=directory,
                               timeout=60, to_log=True)
        if not renamed.ok:
            self.run_cmd(["git", "checkout", "--quiet", "-B", branch], cwd=directory,
                         timeout=60, to_log=True)
        self.run_cmd(["git", "add", "package-lock.json", "package.json"], cwd=directory,
                     timeout=60, to_log=True)
        self.run_cmd(["git", "commit", "--quiet", "-m", message], cwd=directory, timeout=120,
                     to_log=True,
                     extra_env={"GIT_AUTHOR_NAME": self.cfg.commit_name,
                                "GIT_AUTHOR_EMAIL": self.cfg.commit_email,
                                "GIT_COMMITTER_NAME": self.cfg.commit_name,
                                "GIT_COMMITTER_EMAIL": self.cfg.commit_email})

    def _message(self, before: str, changed: str, after: str) -> str:
        return (
            "Bump vulnerable dependencies\n\n"
            f"osv-scanner reports {_count(before, 'finding')} at or above CVSS "
            f"{self.cfg.min_severity:g} on this\n"
            f"repository. This changes {changed}, which leaves {after} of them outstanding, "
            "and\nwas validated against the repository's own gates before being proposed.\n")

    def _numstat(self, directory: Path) -> str:
        diff = self.run_cmd(["git", "diff", "--numstat", "--", "package-lock.json",
                             "package.json"], cwd=directory, timeout=60)
        parts = []
        for line in diff.stdout.splitlines():
            fields = line.split("\t")
            if len(fields) >= 2:
                parts.append(f"{fields[0]}+{fields[1]}")
        return ",".join(parts)


def run_deps(cfg: DepsConfig, *, dry_run: bool = False, only_repo: str | None = None,
             out: TextIO | None = None, env: dict[str, str] | None = None) -> int:
    """The command behind `hng deps run` and `hng deps dry-run`."""
    return Pipeline(cfg, dry_run=dry_run, only_repo=only_repo, out=out, env=env).run()
