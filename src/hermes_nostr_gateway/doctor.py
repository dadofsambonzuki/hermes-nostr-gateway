"""`hng doctor`: one preflight over everything the other verbs assume.

The knowledge used to live in six commands run in a remembered order: is the config there,
does the key read, can the store be written, does the daemon answer, is the read set legible
(how many relays, how many ok, how many refused), is `ngit` new enough, is the checkout the
daemon serves on `master`, is the live Hermes plugin copy the `adapter.py` this build ships,
does every mapped checkout carry a NIP-34 address, are the project and board really there, is a
pass scheduled for each channel, and what is the watcher doing? Each check here is one of those
questions, reports the answer, and names what to change when it fails.

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

from .checkout import NOT_A_CHECKOUT, CheckoutState, is_card_workspace, repo_root
from .checkout import inspect as inspect_checkout
from .ngit import (
    NGIT_FLOOR,
    NGIT_INSTALL_HINT,
    Ngit,
    NgitStatus,
    looks_hex,
    ngit_status,
)
from .projects import (
    Project,
    ProjectMap,
    checks_for,
    hermes_home,
    load_registry,
)
from .server import SOCKET_PATH_FIX, socket_path_problem

#: How long the daemon gets to answer `status` before the socket check calls it absent.
DAEMON_TIMEOUT_S = 3.0

#: Where an installed pass timer can live: system units (an operator's install and a package's
#: own drop), then the user units `systemctl --user` would read.
SYSTEMD_UNIT_DIRS: tuple[Path, ...] = (
    Path("/etc/systemd/system"),
    Path("/usr/lib/systemd/system"),
    Path.home() / ".config/systemd/user",
)

#: The unit that runs one pass. One timer instance per mapped channel id, so the instance
#: name is the channel -- which is also the one string a channel id alone can be named by.
NGIT_PASS_UNIT = "hermes-nostr-gateway-ngit"

#: Top-level config keys this build reads. The preflight uses it to tell a live key from
#: residue, so a key has to be added here with the merge that starts reading it -- otherwise
#: a config carrying it is told it is not read. The example config is a subset by test.
READ_KEYS: frozenset[str] = frozenset({
    "adapter", "advertise", "bots", "concord", "control", "defaults", "deps", "drops",
    "feeds", "groups", "hermes_config", "identity", "marmot", "media", "ngit", "policies",
    "profiles", "rate_limit", "relays", "replay", "retry", "routing", "socket", "store",
    "store_write", "whitenoise",
})

#: Keys deliberately tolerated without being read, and the reason the advisory repeats: a
#: config set before a merge must still boot, and an operator should not have to re-learn
#: which leftovers are on purpose.
TOLERATED_KEYS: dict[str, str] = {
    "transport": "ignored on purpose: there is no transport switch, so an older config still "
                 "boots",
}


@dataclass(frozen=True)
class Check:
    """One preflight question, its answer, and the fix when the answer is no."""

    name: str
    ok: bool
    detail: str
    fix: str = ""
    #: An advisory is reported, never counted: unknown config keys are residue (or a block set
    #: ahead of a merge), not a reason to refuse the host.
    advisory: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail, "fix": self.fix,
                "advisory": self.advisory}


@dataclass(frozen=True)
class Report:
    """Every check's answer, in the order a human would ask the questions."""

    config: str
    checks: tuple[Check, ...] = ()

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if not check.ok and not check.advisory]

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
            if check.advisory:
                label = "note"
            else:
                label = "ok  " if check.ok else "FAIL"
            out.append(f"  {label}  {check.name:<{width}}  {check.detail}")
            if not check.ok and not check.advisory and check.fix:
                out.append(f"        {'':<{width}}  fix: {check.fix}")
        counted = [check for check in self.checks if not check.advisory]
        advisories = len(self.checks) - len(counted)
        passed = len(counted) - len(self.failed)
        summary = f"{passed}/{len(counted)} check(s) passed"
        if advisories:
            summary += f"; {advisories} advisory"
        if self.failed:
            summary += f"; {len(self.failed)} need attention"
        out.append(summary)
        return out


# -- the checks --------------------------------------------------------------

def unread_config_keys(cfg: dict[str, Any]) -> list[str]:
    """Top-level keys this build does not read, in the order the file carries them.

    A key can be set ahead of a merge on purpose, so this answers "does this build read it",
    not "is it wrong" -- which is why it becomes an advisory and never a failing check.
    """
    return [key for key in cfg if key not in READ_KEYS]


def _config_keys_check(cfg: dict[str, Any]) -> Check | None:
    """Unread top-level keys as one advisory line; tolerated leftovers keep their reason."""
    unread = unread_config_keys(cfg)
    if not unread:
        return None
    parts: list[str] = []
    for key in unread:
        reason = TOLERATED_KEYS.get(key)
        parts.append(f"{key} ({reason or 'no code reads it: stale, or set ahead of a merge'})")
    return Check("config-keys", True,
                 "config carries key(s) this build does not read: " + "; ".join(parts),
                 advisory=True)


def credential_sources(name: str) -> list[tuple[str, Path]]:
    """Installed units that load `name`, as (unit name, source file) pairs.

    A credential is only readable inside the unit that loads it, so a hand-run `hng doctor`
    cannot read the key the daemon reads. What it can check is the other half of the same
    wiring: a gateway unit names the credential, and the file it points at is there.
    """
    found: list[tuple[str, Path]] = []
    for directory in SYSTEMD_UNIT_DIRS:
        for path in sorted(directory.glob("hermes-nostr-gateway*.service")):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for line in text.splitlines():
                key, sep, value = line.partition("=")
                if not sep or key.strip() != "LoadCredential":
                    continue
                credential, colon, source = value.partition(":")
                if colon and credential.strip() == name:
                    found.append((path.name, Path(source.strip())))
    return found


def _identity_check(cfg: dict[str, Any]) -> Check:
    """The key, read through the same code `hng serve` uses, or proven by the unit's wiring."""
    from .cli import secret_from

    identity = cfg.get("identity") or {}
    name = str(identity.get("secret_credential") or "").strip()
    if name and not os.environ.get("CREDENTIALS_DIRECTORY"):
        # A credential lives in a per-unit tmpfs: a hand-run doctor cannot read it, and
        # calling that unreadable would fail the preflight on every correctly credentialled
        # host. Check the wiring instead -- a unit loads it and its source file exists.
        sources = credential_sources(name)
        if sources:
            unit, source = sources[0]
            try:
                present = source.exists()
            except OSError:
                # A root-only directory (the usual home for the key) stops this user
                # stat-ing the file; the unit's own root can, which is what matters.
                present = None
            if present is False:
                return Check("identity", False,
                             f"credential {name!r} is loaded by {unit} from {source}, "
                             "which is not there",
                             f"create {source} (root-owned, not world-readable) "
                             f"before {unit} starts")
            if present is None:
                return Check("identity", True,
                             f"credential {name!r} is loaded by {unit}; {source} is root-only, "
                             "so this user cannot read it (the unit can)")
            return Check("identity", True,
                         f"credential {name!r} is loaded by {unit} from {source}")
        return Check(
            "identity", False,
            f"identity.secret_credential: {name!r} is not loaded by any installed "
            "hermes-nostr-gateway service",
            f"add LoadCredential={name}:/path/to/key to the unit that runs the daemon and "
            "the pass, or set identity.secret_file instead")
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
    too_long = socket_path_problem(path)
    if too_long is not None:
        # Before the daemon-down branch: "start the daemon" is the wrong remedy for a
        # path the kernel will refuse every time, and the length is knowable without it.
        return Check("socket", False, too_long, SOCKET_PATH_FIX)
    if reply is None:
        return Check("socket", False, f"{path}: {problem}",
                     f"start the daemon: `hng --config <config> serve` (it binds {path}); "
                     "run it under the same user that owns the store")
    workers = len(reply.get("workers") or [])
    return Check("socket", True, f"daemon answers at {path} ({workers} worker(s))")


def _relays_check(cfg: dict[str, Any], reply: dict | None, problem: str) -> Check:
    """The read set in one line: how many, how many ok, and how many refused.

    A relay that will not serve our filters is `refused`: counted apart from `down`,
    because it is a fact about the read set rather than a transport fault, and it does
    not fail this check. A relay with no worker or a dead child does, because then the
    gateway is not reading something it says it reads.
    """
    from .gateway import kept_relays, relay_read_plan
    from .health import BAD, counts_text, relay_counts, relay_verdicts

    try:
        read, _ = relay_read_plan(cfg)
        kept = kept_relays(cfg)
    except ValueError as exc:
        return Check("relays", False, str(exc),
                     "fix `relays.read` / `relays.keep` (see config.example.yaml)")

    if reply is None:
        return Check("relays", True,
                     f"not checked: the daemon did not answer ({problem}); "
                     "the socket check carries this")
    verdicts = relay_verdicts(reply, relays=read)
    counts = relay_counts(verdicts)
    detail = counts_text(counts)
    if kept:
        detail += f"; {len(kept)} kept for another purpose"
    if counts["refused"]:
        detail += ("; a refused relay is not a read relay: move it to `relays.keep` "
                   "with a why")
    bad = [row for row in verdicts if row.severity == BAD]
    if bad:
        return Check("relays", False,
                     detail + f"; not reading: {', '.join(row.relay for row in bad)}",
                     "check reachability and restart the daemon; remove a relay that is "
                     "gone, or move a relay that refuses us to `relays.keep` with a why")
    return Check("relays", True, detail)


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


def live_checkout_path() -> Path | None:
    """The checkout this build runs from -- one seam, so a test never reads the host's tree.

    Same rule as the ngit probe: the question a check asks about the host goes through one
    module-level function that tests can replace, or every unit test measures the machine it
    runs on. In CI the checkout is a PR branch, so a doctor test would go red for a reason
    that is not the code under test.
    """
    return repo_root()


def _checkout_check() -> Check:
    """The live checkout serves merged code: HEAD is a commit that exists on origin/master.

    Not "the tree is clean": a proposal branch that passes its own gates is clean, and serving
    it is exactly the failure this check exists for. A card worktree is expected to be on a
    proposal branch and is not the tree the daemon runs from, so it is named and passed.
    """
    path = live_checkout_path()
    if path is None:
        return Check("checkout", True, NOT_A_CHECKOUT)
    if is_card_workspace(path):
        return Check("checkout", True,
                     f"{path} is a card workspace, not the live checkout (on a proposal "
                     f"branch by design)")
    state: CheckoutState = inspect_checkout(path)
    return Check("checkout", state.ok, state.describe(), "" if state.ok else state.fix())


def _adapter_check(cfg: dict[str, Any]) -> Check:
    """The live plugin copy is this build's adapter -- the deployment `checkout` cannot see.

    `checkout` proves the daemon's tree is merged code; Hermes does not import that tree. It
    loads a copy of `adapter.py` from its plugins directory, so an adapter merge reaches a
    running gateway only when the copy is refreshed (and the process restarted). The invariant
    is therefore the two hashes; an older process start is reported on the passing check as a
    warning, because the files matching is what the check is for and the process age is
    evidence for the operator, not a second failure.
    """
    from .adapter_copy import INSTALL_FIX
    from .adapter_copy import status as adapter_status

    state = adapter_status(cfg)
    return Check("adapter", state.ok, state.detail(), "" if state.ok else INSTALL_FIX)


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


def _cron_pass(tokens: set[str], sources: list[tuple[str, str]]) -> tuple[str, str]:
    """The cron source and the line running an enabled pass naming this channel, or ("", "").

    The line comes back with the source because *how* it names the channel decides whether
    the pass can address it: `--channel concord:<community>:<channel>` can, `--channel <id>`
    cannot when the entry declares no community.
    """
    for name, text in sources:
        for line in text.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#") or "ngit sync" not in line.lower():
                continue
            if any(token in line.lower() for token in tokens):
                return name, stripped
    return "", ""


def systemd_units() -> list[str]:
    """Installed pass timers, by unit name, from every unit directory that exists.

    An instance is named for its channel (`...@<channel-id>.timer`), and `systemctl enable`
    leaves a symlink of that name in `timers.target.wants` even though the unit file beside
    it is the template, so both the directory and its `.wants` children are read. The
    template comes back too: it is ours, but it names no channel, and the check has to read
    it as a template rather than mistake `%i` for a channel.
    """
    found: dict[str, None] = {}
    patterns = (f"{NGIT_PASS_UNIT}@*.timer", f"*.wants/{NGIT_PASS_UNIT}@*.timer")
    for directory in SYSTEMD_UNIT_DIRS:
        for pattern in patterns:
            for path in sorted(directory.glob(pattern)):
                found.setdefault(path.name, None)
    return list(found)


def _unit_instance(unit: str) -> str:
    """The channel a timer instance is named for; "" when the unit is the template."""
    stem = unit.removesuffix(".timer")
    _, at, instance = stem.partition("@")
    return instance if at else ""


def _systemd_cover(tokens: set[str], units: list[str]) -> str:
    """The timer instance covering this channel, named, or "" when none is installed."""
    for unit in units:
        instance = _unit_instance(unit)
        if instance and instance.lower() in tokens:
            return f"systemd timer {unit}"
    return ""


def _systemd_template(units: list[str]) -> str:
    """The pass timer's template (`...@.timer`) when its file is installed, else ""."""
    for unit in units:
        if unit.endswith("@.timer"):
            return unit
    return ""


def _needs_a_conversation(project: Project) -> bool:
    """The entry is keyed by a channel id and declares no community, so a pass must spell it.

    A notice is addressed by `concord:<community>:<channel>`, and `ngit.resolve_target`
    refuses an id-only pass against such an entry rather than fail every notice at delivery.
    A timer instance is named for the id and nothing else, so a timer can never be that
    pass; a crontab line can, when it names the whole conversation.
    """
    return not project.community and looks_hex(project.channel)


def _names_conversation(line: str) -> bool:
    """The pass spells `concord:`, so it carries a community the entry itself does not."""
    return "concord:" in line.lower()


def _schedule_check(mapping: ProjectMap) -> Check:
    """One enabled pass per channel that queues work; either scheduler counts.

    The sources are read, not the running scheduler: an uncommented crontab line, or the
    instance `systemctl enable` leaves in `timers.target.wants`, is the same evidence a
    human would check. The detail names the scheduler that covered each channel, so a host
    mid-cutover can see which one is doing the work. A template alone is not a pass -- it
    names no channel and schedules nothing until an instance is enabled -- but it is
    recognised, and the fix then names the instance to enable.

    Covering is not enough on its own: the pass has to be able to address the channel, or
    this check is green while every pass exits non-zero. An entry keyed by a channel id that
    declares no `community:` is only addressed by a pass that names the whole conversation,
    which a timer instance never does -- so that case is reported, not counted.
    """
    queued = [project for project in mapping.all()
              if project.board and not project.config_error]
    if not queued:
        return Check("schedule", True,
                     "no mapped channel queues work, so no bridge pass is needed")
    sources = cron_sources()
    units = systemd_units()
    missing: list[str] = []
    unaddressable: list[str] = []
    covered: list[str] = []
    for project in queued:
        tokens = _channel_tokens(project)
        source, line = _cron_pass(tokens, sources)
        found = source or _systemd_cover(tokens, units)
        if found and not (_needs_a_conversation(project) and not _names_conversation(line)):
            covered.append(f"{project.channel} ({found})")
        elif found:
            unaddressable.append(project.channel)
        else:
            missing.append(project.channel)
    if missing or unaddressable:
        problems: list[str] = []
        fix = ""
        if missing:
            problems.append("no enabled pass names: " + ", ".join(missing))
            template = _systemd_template(units)
            if template:
                base = template.removesuffix("@.timer")
                enable = ", ".join(f"`systemctl enable --now {base}@{channel}.timer`"
                                   for channel in missing)
                schedule = (f"the timer template is installed; enable one instance per "
                            f"channel: {enable}")
            else:
                schedule = ("install deploy/hermes-nostr-gateway-ngit@.{service,timer} and "
                            "enable one instance per channel (`systemctl enable --now "
                            "hermes-nostr-gateway-ngit@<channel-id>.timer`)")
            example = ("*/10 * * * * cd <checkout> && PATH=<dir holding hng>:<dir holding "
                       "ngit>:$PATH hng --config <config> ngit sync --channel "
                       "concord:<community-id>:<channel-id> --json")
            fix = (f"{schedule}, or add one line per channel to crontab (or a Hermes cron "
                   f"job): {example}. Key the concord.projects entry by the channel id so "
                   "the pass resolves it")
        if unaddressable:
            problems.append("a pass names, but cannot address (the concord.projects entry "
                            "declares no `community:`): " + ", ".join(unaddressable))
            fix = (f"{fix}. " if fix else "") + (
                "add `community: <community-id>` to each of those entries, because a notice "
                "is addressed by the whole conversation and the id alone does not spell it; "
                "or name the whole conversation in the pass (`--channel "
                "concord:<community-id>:<channel-id>`). Until one of those is done every "
                "pass exits non-zero")
        return Check("schedule", False, "; ".join(problems), fix)
    return Check("schedule", True, "a pass covers " + ", ".join(covered))


def _watcher_check(cfg: dict[str, Any], reply: dict | None, problem: str) -> Check:
    """The watcher's own report: wanted and alive, or off for a stated reason."""
    from .ngit_watch import NgitWatchConfig

    wanted = NgitWatchConfig.from_config(cfg)
    if not wanted.enabled:
        return Check("watcher", True, f"off ({wanted.reason}); scheduled passes are the floor")
    if reply is None:
        return Check("watcher", False, f"cannot read watcher state: {problem}",
                     "start the daemon (`hng --config <config> serve`); the scheduled passes "
                     "keep the bridge moving until it is up")
    watch = reply.get("ngit_watch")
    if not isinstance(watch, dict):
        return Check("watcher", False,
                     "the daemon does not report ngit_watch (an older build is running)",
                     "restart the daemon on this build so its status reports the watcher; "
                     "scheduled passes are the floor meanwhile")
    if not watch.get("enabled"):
        return Check("watcher", False,
                     f"not watching ({watch.get('reason') or 'no reason given'})",
                     "fix the reported reason, then restart the daemon; scheduled passes are "
                     "the floor meanwhile")
    if not watch.get("alive"):
        return Check("watcher", False, "enabled, but no relay subscription is up yet",
                     "check relay reachability and restart the daemon; scheduled passes are "
                     "the floor meanwhile")
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
    checks: list[Check] = [Check("config", True, f"{config_path} parses")]
    keys_check = _config_keys_check(cfg)
    if keys_check is not None:
        checks.append(keys_check)
    checks.extend([
        _identity_check(cfg),
        _store_check(cfg),
        _socket_check(path, reply, problem),
        _relays_check(cfg, reply, problem),
        _ngit_check(status),
        _checkout_check(),
        _adapter_check(cfg),
        _checkouts_check(mapping, status),
        projects_check,
        boards_check,
        _schedule_check(mapping),
        _watcher_check(cfg, reply, problem),
    ])
    return Report(config=str(config_path), checks=tuple(checks))
