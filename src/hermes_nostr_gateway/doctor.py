"""`hng doctor`: one preflight over everything the other verbs assume.

The knowledge used to live in six commands run in a remembered order: is the config there,
does the key read, can the store be written, does the daemon answer, is `ngit` new enough,
does every mapped checkout carry a NIP-34 address, are the project and board really there,
is there a cron pass per channel, and what is the watcher doing? Each check here is one of
those questions, reports the answer, and names what to change when it fails.

`run` never raises for a bad host: a broken config, a missing key or a missing `ngit` is a
failing check with a fix, which is what `--json` carries and what makes the exit code
useful in a script. Only a bug in this module would raise.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ngit import NGIT_FLOOR, NGIT_INSTALL_HINT, Ngit, NgitStatus, ngit_status
from .projects import (
    Project,
    ProjectMap,
    checks_for,
    hermes_home,
    load_registry,
)

#: How long the daemon gets to answer `status` before the socket check calls it absent.
DAEMON_TIMEOUT_S = 3.0


@dataclass(frozen=True)
class Check:
    """One preflight question, its answer, and the fix when the answer is no."""

    name: str
    ok: bool
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix}


@dataclass(frozen=True)
class Report:
    """Every check's answer, in the order a human would ask the questions."""

    config: str
    checks: tuple[Check, ...] = ()

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok]

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "config": self.config,
                "checks": [check.as_dict() for check in self.checks]}

    def lines(self) -> list[str]:
        width = max((len(check.name) for check in self.checks), default=4)
        out: list[str] = []
        for check in self.checks:
            out.append(f"  {'ok  ' if check.ok else 'FAIL'}  {check.name:<{width}}  "
                       f"{check.detail}")
            if not check.ok and check.fix:
                out.append(f"        {'':<{width}}  fix: {check.fix}")
        passed = len(self.checks) - len(self.failed)
        out.append(f"{passed}/{len(self.checks)} check(s) passed"
                   + ("" if self.ok else f"; {len(self.failed)} need attention"))
        return out


# -- the checks --------------------------------------------------------------

def _identity_check(cfg: dict[str, Any]) -> Check:
    """The key, read through the same code `hng serve` uses, so nothing is re-derived."""
    from .cli import secret_from

    try:
        secret_from(cfg)
    except SystemExit as exc:
        message = str(exc) or "the identity key could not be read"
        return Check(
            "identity", False, message,
            "set identity.secret_file to a readable key file (or identity.secret_credential "
            'under systemd); the file holds {"secret_key_hex": "..."} or the hex key alone')
    return Check("identity", True, "identity key readable")


def _store_check(cfg: dict[str, Any]) -> Check:
    """Writable now, or creatable by the daemon: a fresh install has no store file yet."""
    path = Path(os.path.expanduser(str(cfg.get("store") or "gateway.db")))
    if path.is_dir():
        return Check("store", False, f"{path} is a directory, not the store file",
                     "point store: at a file path")
    if path.exists():
        try:
            handle = os.open(path, os.O_RDWR)
        except OSError as exc:
            return Check("store", False, f"{path} is not writable ({exc.strerror})",
                         f"give the gateway's user write access to {path}, or point store: "
                         "at a writable path")
        os.close(handle)
        return Check("store", True, f"{path} is writable")
    parent = path.parent
    if not parent.is_dir():
        return Check("store", False, f"{path} and its directory {parent} do not exist",
                     f"create {parent} and give the gateway's user write access; the daemon "
                     "creates the store file on its first write")
    try:
        with tempfile.NamedTemporaryFile(dir=parent, prefix=".hng-doctor-"):
            pass
    except OSError as exc:
        return Check("store", False, f"{parent} is not writable ({exc.strerror})",
                     f"give the gateway's user write access to {parent}, or point store: at "
                     "a writable path")
    return Check("store", True,
                 f"{parent} is writable (the store file is created on first write)")


def socket_path(cfg: dict[str, Any], override: str | Path | None = None) -> Path:
    """The socket a client must connect to, expanded exactly as the daemon expands it."""
    from .gateway import default_socket_path

    raw = override or cfg.get("socket") or default_socket_path()
    return Path(os.path.expanduser(os.path.expanduser(str(raw))))


def probe_daemon(path: Path, *, timeout: float = DAEMON_TIMEOUT_S) -> tuple[dict | None, str]:
    """One `status` request. Returns `(reply, "")` or `(None, why_it_did_not_answer)`."""
    from .server import request

    if not Path(path).exists():
        return None, f"no socket at {path}"
    try:
        reply = asyncio.run(request(path, {"op": "status"}, timeout=timeout))
    except (OSError, TimeoutError, ValueError) as exc:
        return None, str(exc) or exc.__class__.__name__
    if reply.get("type") != "status":
        return None, f"unexpected reply {reply.get('type')!r}"
    return reply, ""


def _socket_check(path: Path, reply: dict | None, problem: str) -> Check:
    if reply is None:
        return Check("socket", False, f"{path}: {problem}",
                     f"start the daemon: `hng --config <config> serve` (it binds {path}); "
                     "run it under the same user that owns the store")
    workers = len(reply.get("workers") or [])
    return Check("socket", True, f"daemon answers at {path} ({workers} worker(s))")


def _ngit_check(status: NgitStatus) -> Check:
    """The floor is one constant, read by the preflight of `ngit sync` too."""
    if not status.usable:
        if not status.path:
            detail = f"`{status.binary}` not found on PATH; need >= {NGIT_FLOOR}"
        elif status.version:
            detail = f"found {status.version} at {status.path}; need >= {NGIT_FLOOR}"
        else:
            detail = f"{status.path} did not answer `--version`; need >= {NGIT_FLOOR}"
        return Check("ngit", False, detail, NGIT_INSTALL_HINT)
    return Check("ngit", True, f"{status.version} at {status.path} (floor {NGIT_FLOOR})")


def _checkouts_check(mapping: ProjectMap, status: NgitStatus) -> Check:
    """Every mapped checkout is a git repo whose NIP-34 address the bridge can read."""
    if not mapping:
        return Check("checkouts", True,
                     "concord.projects is empty: no channel resolves to a checkout")
    problems: list[str] = []
    fine: list[str] = []
    for project in mapping.all():
        if project.config_error:
            problems.append(f"{project.channel}: {project.config_error}")
            continue
        if project.problem:
            problems.append(f"{project.channel}: {project.path} {project.problem}")
            continue
        if not project.is_git:
            problems.append(f"{project.channel}: {project.path} is not a git repository")
            continue
        if not status.usable:
            fine.append(f"{project.channel}: {project.name} (address not checked: no ngit)")
            continue
        try:
            repository = Ngit(project.path).repository()
        except Exception as exc:  # noqa: BLE001 - one checkout must not hide the rest
            problems.append(f"{project.channel}: cannot read {project.path}: {exc}")
            continue
        if not repository.address:
            problems.append(f"{project.channel}: {project.path} carries no NIP-34 address")
        else:
            fine.append(f"{project.channel}: {repository.address}")
    detail = "; ".join(problems) if problems else "; ".join(fine)
    fix = ("point `path:` at the checkout (a git repository) and announce it on Nostr "
           "(`ngit repo --init`) so the bridge can read its issues and PRs")
    return Check("checkouts", not problems, detail, fix if problems else "")


def _declared(mapping: ProjectMap) -> list[Project]:
    return [project for project in mapping.all()
            if not project.config_error and (project.project or project.board)]


def _registry_checks(mapping: ProjectMap) -> tuple[Check, Check]:
    """The project and the board each entry declares: both exist, and are bound."""
    declared = _declared(mapping)
    if not declared:
        nothing = "no usable concord.projects entry declares a project or a board"
        return Check("projects", True, nothing), Check("boards", True, nothing)
    registry = load_registry()
    project_problems: list[str] = []
    project_fine: list[str] = []
    board_problems: list[str] = []
    board_fine: list[str] = []
    for project in declared:
        for check in checks_for(project, registry):
            if check.name == "project":
                (project_fine if check.ok else project_problems).append(
                    f"{project.channel}: {check.detail}")
            elif check.name == "board":
                (board_fine if check.ok else board_problems).append(
                    f"{project.channel}: {check.detail}")
    project_check = Check(
        "projects", not project_problems,
        "; ".join(project_problems or project_fine) or "no entry declares a project",
        (f"register each project (`hermes project create \"<name>\" <path> --slug <slug>`), "
         f"or fix the project: key in concord.projects. Registry read from {registry.db}")
        if project_problems else "")
    board_check = Check(
        "boards", not board_problems,
        "; ".join(board_problems or board_fine) or "no entry declares a board",
        ("create each board and bind it: `hermes kanban boards create <board> "
         "--default-workdir <path>` then `hermes project bind-board <project> <board>`; "
         "a board without a project is a config error, not a queue")
        if board_problems else "")
    return project_check, board_check


def _crontab_text() -> str:
    """The user crontab, or "" when there is none (a missing crontab is not a failure)."""
    try:
        done = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _hermes_cron_text() -> str:
    """Every enabled Hermes cron job's text: a pass can live there instead of crontab."""
    path = hermes_home() / "cron" / "jobs.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    jobs = data.get("jobs") if isinstance(data, dict) else data
    if not isinstance(jobs, list):
        return ""
    out: list[str] = []
    for job in jobs:
        if not isinstance(job, dict) or job.get("enabled") is False:
            continue
        out.append(" ".join(str(job.get(key) or "")
                            for key in ("name", "prompt", "script", "command")))
    return "\n".join(out)


def cron_sources() -> list[tuple[str, str]]:
    """Where a pass can be scheduled, as (name, text) pairs. Both are read, either counts."""
    sources: list[tuple[str, str]] = []
    crontab = _crontab_text()
    if crontab.strip():
        sources.append(("crontab", crontab))
    hermes = _hermes_cron_text()
    if hermes.strip():
        sources.append(("hermes cron", hermes))
    return sources


def _channel_tokens(project: Project) -> set[str]:
    """Strings a scheduled pass may name this channel by, as the mapping spells it.

    A pass can only resolve what the entry is keyed by: an entry keyed by the id is not
    matched by a pass that names the friendly name (and the other way round), so the check
    looks for the configured spelling and the whole conversation -- nothing looser.
    """
    tokens = {project.channel.strip().lower()}
    if project.community:
        tokens.add(f"concord:{project.community}:{project.channel}".lower())
    return {token for token in tokens if token}


def _cron_check(mapping: ProjectMap) -> Check:
    """One enabled pass per channel that queues work; cron is the floor, not the watcher."""
    queued = [project for project in mapping.all()
              if project.board and not project.config_error]
    if not queued:
        return Check("cron", True,
                     "no mapped channel queues work, so no bridge pass is needed")
    sources = cron_sources()
    missing: list[str] = []
    covered: list[str] = []
    for project in queued:
        tokens = _channel_tokens(project)
        found = ""
        for name, text in sources:
            pass_line = None
            for line in text.splitlines():
                stripped = line.lstrip()
                if stripped.startswith("#") or "ngit sync" not in line.lower():
                    continue
                if any(token in line.lower() for token in tokens):
                    pass_line = line
                    break
            if pass_line is not None:
                found = name
                break
        if found:
            covered.append(f"{project.channel} ({found})")
        else:
            missing.append(project.channel)
    if missing:
        example = ("*/10 * * * * cd <checkout> && PATH=<dir holding hng>:<dir holding ngit>:"
                   "$PATH hng --config <config> ngit sync --channel "
                   "concord:<community-id>:<channel-id> --json")
        return Check("cron", False, "no enabled pass names: " + ", ".join(missing),
                     f"add one line per channel to crontab (or a Hermes cron job): {example}. "
                     "Key the concord.projects entry by the channel id so the pass resolves it")
    return Check("cron", True, "a pass covers " + ", ".join(covered))


def _watcher_check(cfg: dict[str, Any], reply: dict | None, problem: str) -> Check:
    """The watcher's own report: wanted and alive, or off for a stated reason."""
    from .ngit_watch import NgitWatchConfig

    wanted = NgitWatchConfig.from_config(cfg)
    if not wanted.enabled:
        return Check("watcher", True, f"off ({wanted.reason}); cron passes are the floor")
    if reply is None:
        return Check("watcher", False, f"cannot read watcher state: {problem}",
                     "start the daemon (`hng --config <config> serve`); the cron passes "
                     "keep the bridge moving until it is up")
    watch = reply.get("ngit_watch")
    if not isinstance(watch, dict):
        return Check("watcher", False,
                     "the daemon does not report ngit_watch (an older build is running)",
                     "restart the daemon on this build so its status reports the watcher; "
                     "cron passes are the floor meanwhile")
    if not watch.get("enabled"):
        return Check("watcher", False,
                     f"not watching ({watch.get('reason') or 'no reason given'})",
                     "fix the reported reason, then restart the daemon; cron passes are the "
                     "floor meanwhile")
    if not watch.get("alive"):
        return Check("watcher", False, "enabled, but no relay subscription is up yet",
                     "check relay reachability and restart the daemon; cron passes are the "
                     "floor meanwhile")
    targets = watch.get("targets") or []
    relays = watch.get("relays") or []
    return Check("watcher", True,
                 f"watching {len(targets)} channel repo(s) on {len(relays)} relay(s)")


def run(cfg: dict[str, Any], *, config_path: str | Path, socket: str | Path | None = None
        ) -> Report:
    """Ask every preflight question once, in the order an operator would."""
    status = ngit_status()
    path = socket_path(cfg, socket)
    reply, problem = probe_daemon(path)
    # One map for every check: each build logs its own warnings, and the operator needs
    # each mapping problem said once, not once per question asked about it.
    mapping = ProjectMap.from_config(cfg.get("concord") or {})
    projects_check, boards_check = _registry_checks(mapping)
    checks = (
        Check("config", True, f"{config_path} parses"),
        _identity_check(cfg),
        _store_check(cfg),
        _socket_check(path, reply, problem),
        _ngit_check(status),
        _checkouts_check(mapping, status),
        projects_check,
        boards_check,
        _cron_check(mapping),
        _watcher_check(cfg, reply, problem),
    )
    return Report(config=str(config_path), checks=checks)
