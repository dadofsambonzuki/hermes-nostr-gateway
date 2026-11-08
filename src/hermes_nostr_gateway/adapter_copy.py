"""The live Hermes plugin copy, and proving it is this build's adapter.

`integrations/hermes-plugin/adapter.py` is the canonical adapter. Hermes does not import it:
it loads a *copy* from its plugins directory, and nothing in either process compares the two.
So a merged adapter change is not a deployed one -- the daemon's checkout can be on `master`
while the running gateway still holds the module it imported at its last restart. That is the
same failure as "a merge is not a deployment", one level up, and this module is the comparison
that makes it loud:

- `status()` hashes both files and prints both mtimes. It also reads `hermes-gateway`'s
  `ActiveEnterTimestamp`, so a live copy written *after* the process started is reported as a
  stale import -- the case a matching hash cannot see. That unit query is best-effort: a
  refusal is a note, never an exception, because the hashes are the invariant and the process
  age is evidence.
- `install()` backs the live copy up to `adapter.py.bak-<UTC stamp>` beside the target, writes
  this build's adapter, hashes the written file back and stops. The restart is the operator's:
  a deployment is a decision, and a copy is not one. Nothing here restarts a unit.

Only `adapter.py` is compared. `__pycache__` beside it holds bytecode from whichever
interpreter last imported the file, so a tree comparison would report a mismatch that no
restart can fix.

Path resolution never assumes `$HOME`: `--live`, then `NOSTR_PLUGIN_PATH`, then `adapter.path`
in the config, then `$HERMES_HOME/plugins/nostr/adapter.py` with `~/.hermes` only as the last
resort -- the same rule `hng skill install` uses.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .checkout import repo_root
from .projects import hermes_home

#: The one file this module compares and installs.
NAME = "adapter.py"

#: The plugin's directory name under Hermes' plugins tree, and the platform it registers.
PLUGIN = "nostr"

#: The unit whose import of the live copy `status` reports on. Hermes' own gateway, not this
#: daemon: this daemon holds no adapter, it holds the socket the adapter connects to.
UNIT = "hermes-gateway"

#: The canonical file, relative to the checkout this build runs from.
REPO_RELATIVE = Path("integrations") / "hermes-plugin" / NAME

#: What an operator types after a mismatch, in the one place every surface reads it.
INSTALL_FIX = ("run `hng adapter install` to write this build's adapter over the live copy, "
               "then restart the gateway (`sudo systemctl restart hermes-gateway`); the "
               "install never restarts it")

#: How long `systemctl show` gets. A preflight must not hang on a bus that is not answering.
SYSTEMCTL_TIMEOUT_S = 10.0

#: Slack in the stale-import comparison: `ActiveEnterTimestamp` has whole-second resolution,
#: so a copy written in the same second a unit entered the active state is not evidence of a
#: stale import.
STALE_SLACK_S = 1.0


class AdapterError(RuntimeError):
    """The copy cannot be compared or made; the message says what to change."""


@dataclass(frozen=True)
class Status:
    """Both copies as they now stand, and what the running process can have imported.

    `applicable` is False for a build with no checkout to compare with (a wheel): the question
    does not apply, which is reported and not a failure. `problems` are the hard answers --
    a missing or unreadable file -- because a copy that cannot be read cannot be proven equal.
    """

    live: Path
    repo: Path | None = None
    repo_hash: str = ""
    live_hash: str = ""
    repo_mtime: float | None = None
    live_mtime: float | None = None
    unit: str = UNIT
    started: float | None = None
    started_note: str = ""
    problems: tuple[str, ...] = ()
    reason: str = ""

    @property
    def applicable(self) -> bool:
        """False when this build carries no repository copy to compare against."""
        return self.repo is not None

    @property
    def equal(self) -> bool:
        return bool(self.repo_hash) and self.repo_hash == self.live_hash

    @property
    def stale(self) -> bool | None:
        """True when the live copy was written after the unit started -- a stale import.

        None when the unit's start could not be read: an unread fact is not a stale import.
        """
        if self.started is None or self.live_mtime is None:
            return None
        return self.live_mtime > self.started + STALE_SLACK_S

    @property
    def ok(self) -> bool:
        return not self.problems and (not self.applicable or self.equal)

    def detail(self) -> str:
        """One sentence: the verdict, both paths, and the hashes when they are the evidence."""
        if not self.applicable:
            return self.reason
        parts: list[str] = ["; ".join(self.problems)] if self.problems else []
        if self.equal:
            parts.append(f"{self.live} matches {self.repo} (sha256 {self.live_hash})")
        elif self.repo_hash and self.live_hash:
            parts.append(f"{self.live} (sha256 {self.live_hash}) differs from {self.repo} "
                         f"(sha256 {self.repo_hash})")
        if self.stale:
            parts.append(f"{self.unit} started {_iso(self.started)}, before the live copy was "
                         f"written, so the running process still holds the previous module: "
                         f"restart it (`sudo systemctl restart {self.unit}`)")
        elif self.started is None and self.started_note:
            parts.append(self.started_note)
        return "; ".join(parts)

    def lines(self) -> list[str]:
        """The same facts as `hng adapter status` prints them: two files, then the verdict."""
        if not self.applicable:
            return [self.reason]
        out = [
            f"repo: {self.repo}",
            f"  sha256 {self.repo_hash or '(unreadable)'}  mtime {_iso(self.repo_mtime)}",
            f"live: {self.live}",
            f"  sha256 {self.live_hash or '(missing)'}  mtime {_iso(self.live_mtime)}",
        ]
        out.extend(f"problem: {problem}" for problem in self.problems)
        if self.ok:
            out.append("verdict: the copies are identical")
        else:
            out.append(f"verdict: the live copy is not this build's adapter -- {INSTALL_FIX}")
        if self.started is not None:
            out.append(f"{self.unit}: active since {_iso(self.started)}")
        elif self.started_note:
            out.append(self.started_note)
        if self.stale:
            out.append(f"warning: the live copy was written after {self.unit} started, so the "
                       f"running process still holds the previous module -- restart it "
                       f"(`sudo systemctl restart {self.unit}`); nothing here restarts it")
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "applicable": self.applicable,
            "equal": self.equal,
            "stale_import": self.stale,
            "repo": str(self.repo) if self.repo else None,
            "live": str(self.live),
            "repo_hash": self.repo_hash,
            "live_hash": self.live_hash,
            "repo_mtime": _iso(self.repo_mtime) if self.repo_mtime else None,
            "live_mtime": _iso(self.live_mtime) if self.live_mtime else None,
            "unit": self.unit,
            "unit_started": _iso(self.started) if self.started else None,
            "unit_note": self.started_note,
            "problems": list(self.problems),
            "detail": self.detail(),
        }


@dataclass(frozen=True)
class Install:
    """What an install did, in a shape the CLI prints and a test asserts."""

    source: Path
    target: Path
    action: str            # "installed" (bytes written) or "current" (nothing to do)
    before: str            # the live copy's hash before, "" when there was none
    after: str             # the live copy's hash now, re-read from disk
    backup: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "source": str(self.source),
            "target": str(self.target),
            "before": self.before,
            "after": self.after,
            "backup": str(self.backup) if self.backup else None,
        }


def source_path() -> Path | None:
    """The repository's canonical adapter, or None when this build carries no checkout.

    Resolved from the package's own location -- the build that is running -- so a comparison
    can never be made against a tree this process is not serving. A wheel install has no
    repository copy, and `status` says so instead of inventing a failure.
    """
    root = repo_root()
    if root is None:
        return None
    candidate = root / REPO_RELATIVE
    return candidate if candidate.is_file() else None


def _configured(cfg: Mapping[str, Any] | None) -> str:
    """`adapter.path` as written, accepting a mapping block or a bare string."""
    if not cfg:
        return ""
    block = cfg.get("adapter")
    if isinstance(block, Mapping):
        value = block.get("path")
    elif isinstance(block, str):
        value = block
    else:
        value = None
    return str(value).strip() if value else ""


def live_path(cfg: Mapping[str, Any] | None = None,
              override: str | Path | None = None) -> Path:
    """Where Hermes loads the plugin from: `--live`, `$NOSTR_PLUGIN_PATH`, config, default.

    `NOSTR_PLUGIN_PATH` may name the file or the directory holding it: a value that is an
    existing directory gets `adapter.py` appended, which is what an operator who exported the
    plugins directory meant. Expanding `~` and `$VARS` here is the same rule the socket path
    follows, and `$HERMES_HOME` wins over `$HOME` so a caller in another home is not measured
    against this one.
    """
    for candidate in (override, os.environ.get("NOSTR_PLUGIN_PATH"), _configured(cfg)):
        if not candidate:
            continue
        path = Path(os.path.expandvars(os.path.expanduser(str(candidate)))).absolute()
        return path / NAME if path.is_dir() else path
    return hermes_home() / "plugins" / PLUGIN / NAME


def digest(path: Path) -> str:
    """The file's SHA-256 as hex. Streamed: the adapter is small, but this is a general file."""
    hasher = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                hasher.update(block)
    except OSError as exc:
        raise AdapterError(f"{path}: cannot read it ({exc.strerror or exc})") from None
    return hasher.hexdigest()


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def _iso(timestamp: float | None) -> str:
    if timestamp is None:
        return "(unknown)"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def unit_started(unit: str = UNIT) -> tuple[float | None, str]:
    """`(epoch, "")` when the unit's `ActiveEnterTimestamp` reads, `(None, why)` otherwise.

    Best-effort by construction: this is a preflight, and the operator running it may not be
    able to ask systemd at all. A refusal or an unparsable answer is a note on a passing
    check, not a failure -- the copies matching is what the check is for.

    `--timestamp=unix` is asked for first because it removes the local-time rendering from the
    comparison; systemd older than 246 refuses the flag, and the rendered form is parsed then.
    """
    request = ["systemctl", "show", unit, "--property=ActiveEnterTimestamp", "--no-pager"]
    try:
        done = subprocess.run([*request, "--timestamp=unix"], capture_output=True, text=True,
                              timeout=SYSTEMCTL_TIMEOUT_S)
        if done.returncode != 0:
            done = subprocess.run(request, capture_output=True, text=True,
                                  timeout=SYSTEMCTL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{unit}: `systemctl show` could not be run ({exc})"
    if done.returncode != 0:
        said = (done.stderr or done.stdout or "").strip().splitlines()
        return None, (f"{unit}: systemd refused the query"
                      + (f" ({said[0]})" if said else ""))
    text = ""
    for line in (done.stdout or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "ActiveEnterTimestamp":
            text = value.strip()
    if not text or text in {"n/a", "0"}:
        return None, f"{unit}: no ActiveEnterTimestamp, so it is not running"
    if text.startswith("@"):
        try:
            return float(int(text[1:])), ""
        except ValueError:
            pass
    for shape in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, shape).timestamp(), ""
        except ValueError:
            continue
    return None, f"{unit}: ActiveEnterTimestamp did not parse ({text!r})"


def status(cfg: Mapping[str, Any] | None = None, *, live: str | Path | None = None,
           unit: str = UNIT) -> Status:
    """Hash both copies, and ask the unit when it started. Never raises for a bad host."""
    repo = source_path()
    target = live_path(cfg, live)
    problems: list[str] = []
    repo_hash = ""
    live_hash = ""
    if repo is None:
        reason = ("this build carries no integrations/hermes-plugin/adapter.py (a wheel "
                  "install), so there is no repository copy to compare")
    else:
        reason = ""
        if not repo.is_file():
            problems.append(f"the repository copy {repo} is not a file")
        else:
            repo_hash = digest(repo)
    if not target.is_file():
        problems.append(f"no live copy at {target} (Hermes loads the plugin from there)")
    else:
        live_hash = digest(target)
    started, note = unit_started(unit)
    return Status(
        live=target, repo=repo, repo_hash=repo_hash, live_hash=live_hash,
        repo_mtime=_mtime(repo) if repo else None, live_mtime=_mtime(target),
        unit=unit, started=started, started_note=note, problems=tuple(problems),
        reason=reason,
    )


def _backup_path(target: Path, now: datetime | None) -> Path:
    """`<name>.bak-<UTC stamp>`, numbered when two installs land in the same second."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    candidate = target.with_name(f"{target.name}.bak-{stamp}")
    counter = 1
    while candidate.exists():
        counter += 1
        candidate = target.with_name(f"{target.name}.bak-{stamp}-{counter}")
    return candidate


def install(cfg: Mapping[str, Any] | None = None, *, live: str | Path | None = None,
            now: datetime | None = None) -> Install:
    """Back the live copy up, write this build's adapter, and verify what landed.

    Idempotent: a live copy that already matches writes nothing and leaves no backup. The
    source is compiled before anything is written, so a build whose adapter does not parse
    cannot half-install; the bytes that were compiled are the bytes written; and the target is
    re-read and hashed, so a short write is an error rather than a copy that only looks done.

    The backup is `copy2`, so it keeps the previous file's mtime -- the record of when the copy
    it replaced was written is part of what makes a stale import readable afterwards.

    Nothing here restarts a unit or talks to the daemon: `hng adapter install` prints the
    restart as the operator's step, and that is on purpose.
    """
    source = source_path()
    if source is None:
        raise AdapterError(
            "this build carries no integrations/hermes-plugin/adapter.py (a wheel install), "
            "so there is no repository copy to install; run it from the checkout")
    target = live_path(cfg, live)
    try:
        wanted = digest(source)
    except AdapterError as exc:
        raise AdapterError(f"the repository copy is unreadable: {exc}") from None
    before = ""
    if target.is_file():
        try:
            before = digest(target)
        except AdapterError as exc:
            raise AdapterError(f"the live copy is unreadable: {exc}") from None
        if before == wanted:
            return Install(source, target, "current", before, wanted)
    elif target.exists():
        raise AdapterError(f"{target} exists and is not a file")
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise AdapterError(f"{source}: cannot read it ({exc.strerror or exc})") from None
    try:
        compile(data, str(source), "exec")
    except SyntaxError as exc:
        raise AdapterError(f"{source} does not compile ({exc}); nothing was installed") from None
    backup: Path | None = None
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if before:
            backup = _backup_path(target, now)
            shutil.copy2(target, backup)
        target.write_bytes(data)
    except OSError as exc:
        raise AdapterError(f"{target}: cannot be written ({exc.strerror or exc})") from None
    try:
        after = digest(target)
    except AdapterError as exc:
        raise AdapterError(f"the copy landed but cannot be read back: {exc}") from None
    if after != wanted:
        raise AdapterError(
            f"{target} was written but hashes {after}, not {wanted}; the copy is not the "
            f"source file" + (f" (the previous copy is {backup})" if backup else ""))
    return Install(source, target, "installed", before, after, backup)
