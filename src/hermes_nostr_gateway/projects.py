"""Channel -> project mapping: which checkout a channel's work belongs to, and where it queues.

A Concord channel is a context, not a repository. What actually links an inbound message to
a project is a lookup the gateway can do *before* the agent sees it: community + channel ->
checkout path. The agent then starts in the right tree with the right `AGENTS.md`, instead of
guessing from a channel name or being told every time.

Configuration, under `concord.projects`:

```yaml
concord:
  projects:
    - channel: example-channel          # channel name, or its hex id
      path: ~/Projects/example          # the checkout
      name: example                     # optional; defaults to the directory name
      community: <community id>         # optional; restrict to one community id
      project: example                  # optional; Hermes project: anchors the worktree
      board: example-channel            # optional; kanban board this channel queues on
```

Three names, three jobs, and none of them is inferred from another:

- `path` is the checkout: where the code is;
- `project` is the Hermes project: per-profile state that anchors a worktree and its branch
  convention, so it is what a piece of queued work needs in order to land anywhere;
- `board` is the queue: which kanban board the channel's work is filed on.

A channel may point at a checkout with no board (manual work). A `board` without a `project`
is a config error: nothing anchors the worktree, so the entry cannot be honoured and is
reported rather than half-used. Neither is ever invented -- no board exists because a channel
exists, and the daemon never creates state (the integration does, idempotently).

Rules, in order of precedence:

- an entry with a `community` only matches that community; one without matches any;
- a channel matches on its id (hex, case-insensitive) or its current name (case-insensitive);
- an id match beats a name match, so a renamed channel keeps its project;
- the first entry wins a tie, and the duplicate is logged at load time.

A mapping whose `path` is missing is kept and reported (see `hng projects`) rather than
dropped: an unmounted or moved checkout is a fact to show, not a silent config edit.

`hng projects --check` then asks Hermes whether each declared project is registered and each
declared board exists and is bound to that project. That read is read-only (`$HERMES_HOME`,
then `~/.hermes`) and reports what it cannot see instead of guessing: a broken mapping must
never stop the gateway reading messages, so startup only logs the gaps.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("hng.projects")

#: A project or board slug names one row or one directory, so it is one path segment and
#: nothing else: `board: ../../etc` must not become a read outside the board directory.
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class Project:
    """One channel -> checkout mapping, and what that mapping says should drive its work."""

    channel: str
    path: Path
    name: str
    community: str = ""
    #: Hermes project slug: worktree + branch anchoring. Empty = not project-driven.
    project: str = ""
    #: Kanban board slug: where this channel's work is queued. Empty = not kanban-driven.
    board: str = ""
    #: The checkout is not usable (missing, or not a directory).
    problem: str = ""
    #: The entry contradicts itself (`board` with no `project`), so it is never resolved.
    config_error: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem and not self.config_error

    @property
    def is_git(self) -> bool:
        return (self.path / ".git").exists()

    @property
    def has_agents_md(self) -> bool:
        """The convention that makes the mapping useful: the repo documents itself."""
        return (self.path / "AGENTS.md").exists()

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "community": self.community,
            "name": self.name,
            "path": str(self.path),
            "project": self.project,
            "board": self.board,
            "ok": self.ok,
            "problem": self.problem,
            "config_error": self.config_error,
            "git": self.is_git,
            "agents_md": self.has_agents_md,
        }


@dataclass(frozen=True)
class Check:
    """One thing a channel's mapping declares, and whether it is actually there."""

    name: str
    ok: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "ok": self.ok, "detail": self.detail}


@dataclass(frozen=True)
class Registry:
    """What Hermes holds for projects and boards, read-only.

    Never creates either: a project is registered and a board is made by Hermes' own tooling,
    and this object only reports what it found so a mapping can be checked against it.
    """

    home: Path
    projects: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""

    @property
    def db(self) -> Path:
        return self.home / "projects.db"

    def project(self, slug: str) -> Optional[dict[str, Any]]:
        return self.projects.get((slug or "").strip().lower())

    def by_id(self, ident: str) -> Optional[dict[str, Any]]:
        """The project with this id, for a board that records the link from its own side."""
        wanted = str(ident or "").strip()
        if not wanted:
            return None
        for record in self.projects.values():
            if str(record.get("id") or "") == wanted:
                return record
        return None

    def board(self, slug: str) -> Optional[dict[str, Any]]:
        """The board's own record, or None when the board is not there."""
        name = (slug or "").strip()
        if not name:
            return None
        path = board_dir(self.home, name) / "board.json"
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return record if isinstance(record, dict) else None


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(str(value))).absolute()


def hermes_home(home: Optional[str | Path] = None) -> Path:
    """Where Hermes keeps its own state: an explicit path, `$HERMES_HOME`, then `~/.hermes`."""
    if home:
        return Path(os.path.expanduser(str(home))).absolute()
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")).absolute()


def board_dir(home: Path, slug: str) -> Path:
    return Path(home) / "kanban" / "boards" / slug


def load_registry(home: Optional[str | Path] = None) -> Registry:
    """Read the project registry Hermes keeps. A missing or unreadable one is reported."""
    base = hermes_home(home)
    db = base / "projects.db"
    if not db.is_file():
        return Registry(home=base, error=f"no {db}")
    try:
        # Read-only and short-timeout: this must never wait on, or write to, a live Hermes.
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "select id, slug, name, board_slug, primary_path, archived from projects"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Registry(home=base, error=f"{db}: {exc}")
    projects: dict[str, dict[str, Any]] = {}
    for row in rows:
        slug = str(row[1] or "").strip().lower()
        if slug:
            projects[slug] = {
                "id": row[0],
                "slug": row[1],
                "name": row[2],
                "board_slug": row[3] or "",
                "primary_path": row[4] or "",
                "archived": bool(row[5]),
            }
    return Registry(home=base, projects=projects)


def _slug_error(raw: str, *, field_name: str) -> str:
    return f"{field_name} {raw!r} is not a slug (letters, digits, '.', '_' and '-')"


def _project_from(entry: dict[str, Any], *, base: Optional[Path] = None) -> Optional[Project]:
    channel = str(entry.get("channel") or "").strip()
    raw_path = str(entry.get("path") or "").strip()
    if not channel or not raw_path:
        log.warning("concord project entry needs `channel` and `path`: %r", entry)
        return None
    path = _expand(raw_path)
    if base is not None and not path.is_absolute():  # unreachable after _expand, kept explicit
        path = base / path
    name = str(entry.get("name") or path.name).strip()
    project_slug = str(entry.get("project") or "").strip()
    board_slug = str(entry.get("board") or "").strip()

    # A board is a queue of work that needs somewhere to land; without a project nothing
    # anchors the worktree, so the entry is reported and never resolved rather than used
    # for half of what it says.
    config_error = ""
    if project_slug and not _SLUG.match(project_slug):
        config_error = _slug_error(project_slug, field_name="project")
    elif board_slug and not _SLUG.match(board_slug):
        config_error = _slug_error(board_slug, field_name="board")
    elif board_slug and not project_slug:
        config_error = (
            f"sets board {board_slug!r} without a project: nothing anchors the worktree, "
            "so the entry is ignored")
    if config_error:
        log.warning("concord.projects: channel %r %s", channel, config_error)

    problem = "" if path.is_dir() else "path does not exist"
    # Nothing a rejected entry declares is in force: the error says what it asked for, and
    # no integration reads a board off a mapping that is not honoured.
    return Project(channel=channel, path=path, name=name,
                   community=str(entry.get("community") or "").strip(),
                   project=project_slug if not config_error else "",
                   board=board_slug if not config_error else "",
                   problem=problem, config_error=config_error)


def registry_checks(project: Project, registry: Registry) -> list[Check]:
    """The project and board a channel declares, and whether Hermes actually has them.

    Empty when the entry declares neither: a checkout with no queue is manual work, not a
    gap. A config error is already reported at load, so it is not repeated here.
    """
    slug, board = project.project, project.board
    if project.config_error or not (slug or board):
        return []
    checks: list[Check] = []
    registered: Optional[dict[str, Any]] = None
    if slug:
        registered = registry.project(slug)
        if registry.error:
            checks.append(Check("project", False,
                                f"cannot read the project registry ({registry.error})"))
        elif registered is None:
            checks.append(Check("project", False,
                                f"no project {slug!r} registered in {registry.db}"))
        elif registered.get("archived"):
            checks.append(Check("project", False, f"project {slug!r} is archived"))
        else:
            checks.append(Check("project", True,
                                f"{registered.get('name') or slug} ({registered.get('id')})"))
    if board:
        if registered is None:
            why = (f"cannot read the project registry ({registry.error})" if registry.error
                   else f"no project {slug!r} registered to anchor it")
            checks.append(Check("board", False, why))
            return checks
        record = registry.board(board)
        if record is None:
            where = board_dir(registry.home, board)
            checks.append(Check("board", False, f"no board {board!r} at {where}"))
        elif _bound(board, registered, record):
            checks.append(Check("board", True, f"bound to project {slug!r}"))
        else:
            checks.append(Check("board", False,
                                f"board {board!r} is not bound to project {slug!r}"))
    return checks


def _bound(board: str, registered: dict[str, Any], record: dict[str, Any]) -> bool:
    """Hermes records the link from either side; either one naming the other is the binding."""
    if str(registered.get("board_slug") or "").strip().lower() == board.strip().lower():
        return True
    return bool(registered.get("id")) and str(record.get("project_id") or "") == str(
        registered.get("id"))


def checks_for(project: Project, registry: Registry) -> list[Check]:
    """Everything one channel declares: its checkout, its project, its board."""
    checks: list[Check] = []
    if project.config_error:
        checks.append(Check("config", False, project.config_error))
    checks.append(Check("checkout", not project.problem,
                        project.problem or f"{project.path}"))
    checks.extend(registry_checks(project, registry))
    return checks


class ProjectMap:
    """Resolves a channel to a checkout. Empty config resolves nothing, never guesses."""

    def __init__(self, projects: Iterable[Project] = ()):
        self._projects = list(projects)

    @classmethod
    def from_config(cls, concord_cfg: Optional[dict[str, Any]]) -> "ProjectMap":
        entries = (concord_cfg or {}).get("projects") or []
        if isinstance(entries, dict):
            # Tolerate the map form for a one-liner: {channel: path}.
            entries = [{"channel": k, "path": v} for k, v in entries.items()]
        if not isinstance(entries, list):
            log.warning("concord.projects must be a list of entries, got %r", type(entries))
            entries = []
        projects: list[Project] = []
        seen: dict[tuple[str, str], Project] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                log.warning("concord project entry is not a mapping: %r", entry)
                continue
            project = _project_from(entry)
            if project is None:
                continue
            key = (project.community.lower(), project.channel.lower())
            if key in seen:
                log.warning("concord.projects: %r is mapped twice; keeping the first (%s)",
                            project.channel, seen[key].path)
                continue
            seen[key] = project
            if project.problem:
                log.warning("concord.projects: %r points at %s, which %s",
                            project.channel, project.path, project.problem)
            projects.append(project)
        return cls(projects)

    def all(self) -> list[Project]:
        return list(self._projects)

    def __bool__(self) -> bool:
        return bool(self._projects)

    def __len__(self) -> int:
        return len(self._projects)

    def gaps(self, registry: Optional[Registry] = None) -> list[tuple[Project, Check]]:
        """Declared projects and boards that Hermes does not have, per channel.

        This is what startup logs: the mapping keeps working (a channel with no usable
        board is simply not kanban-driven), and the gap is visible instead of fatal.
        """
        if registry is None:
            # Nothing declares a project or a board: there is no Hermes state to consult.
            if not any(project.project or project.board for project in self._projects):
                return []
            registry = load_registry()
        return [(project, check)
                for project in self._projects
                for check in registry_checks(project, registry)
                if not check.ok]

    def _match(self, *, community_id: str, channel_id: str, channel_name: str,
               usable_only: bool) -> Optional[Project]:
        """The mapping for this channel: an id match beats a name match, first entry wins."""
        community = (community_id or "").strip().lower()
        ident = (channel_id or "").strip().lower()
        name = (channel_name or "").strip().lower()

        def eligible(project: Project) -> bool:
            if usable_only and not project.ok:
                return False
            return not project.community or project.community.strip().lower() == community

        for project in self._projects:
            if eligible(project) and ident and project.channel.strip().lower() == ident:
                return project
        for project in self._projects:
            if eligible(project) and name and project.channel.strip().lower() == name:
                return project
        return None

    def resolve(self, *, community_id: str = "", channel_id: str = "",
                channel_name: str = "") -> Optional[Project]:
        """The checkout for this channel, or None (unmapped or unusable path).

        Id matches are tried before name matches, across the whole map, so renaming a
        channel cannot silently steal another channel's project.
        """
        return self._match(community_id=community_id, channel_id=channel_id,
                           channel_name=channel_name, usable_only=True)

    def identify(self, *, community_id: str = "", channel_id: str = "",
                 channel_name: str = "") -> Optional[Project]:
        """The mapping's project for this channel, by identity alone — no path check.

        Routing asks which *project* a channel belongs to, not where its work starts: a
        checkout that is moved, unmounted or briefly missing is a fact about the tree, and a
        route that fell back to the plane's model and key because a directory was gone would
        be exactly the silent substitution routing exists to prevent. The match is the same
        one `resolve` makes (id before name, community scoping, first entry wins), minus the
        path check; an entry that declares no project is not a project route and returns
        None.
        """
        found = self._match(community_id=community_id, channel_id=channel_id,
                            channel_name=channel_name, usable_only=False)
        return found if found is not None and found.project else None

    def resolve_id(self, channel_id: str) -> Optional[Project]:
        """The usable entry keyed by this channel id, across every community.

        A pass scheduled as a systemd timer is named by the channel id alone -- the unit
        instance -- and was never told a community, so applying the entry's `community:`
        would make every such pass unroutable. The id is the unambiguous key; `community`
        still scopes an inbound message, which always carries one.
        """
        ident = (channel_id or "").strip().lower()
        if not ident:
            return None
        for project in self._projects:
            if project.ok and project.channel.strip().lower() == ident:
                return project
        return None


#: A Concord conversation names its channel: `concord:<community>:<channel id or name>`.
_CONVERSATION_PREFIX = "concord:"

#: A channel id: 64 hex characters. Anything else is a name, which an entry may use too.
_CHANNEL_ID = re.compile(r"^[0-9a-f]{64}$")


def project_for_conversation(conversation: str, mapping: "ProjectMap",
                             known_channels: Iterable[Any] = ()) -> str:
    """The Hermes project a `concord:` conversation belongs to, or "" when it has none.

    Routing pins a key per project, so something has to turn a conversation into a project
    before the routing table is asked. That lookup is this map's business, not routing's:
    `mapping` is `concord.projects`, and `known_channels` is whatever the caller holds
    (objects with `community_id`, `channel_id` and `name`) -- which is how a conversation
    naming a channel by id reaches an entry that names it by name, and the other way round.

    Only a Concord conversation has a project, and only the conversation's shape is read: a
    DM peer is a hex string too, so anything that is not `concord:` returns "" rather than a
    guessed project. "" also covers a channel mapped to no project (manual work), an entry
    whose `project` a config error cleared, and a channel this map does not know.
    """
    text = str(conversation or "").strip()
    if not text.lower().startswith(_CONVERSATION_PREFIX):
        return ""
    community, _, token = text[len(_CONVERSATION_PREFIX):].partition(":")
    community, token = community.strip().lower(), token.strip()
    if not community or not token:
        return ""
    ident = token.lower() if _CHANNEL_ID.match(token.lower()) else ""
    name = "" if ident else token
    for channel in known_channels or ():
        known_id = str(getattr(channel, "channel_id", "") or "").strip()
        known_name = str(getattr(channel, "name", "") or "").strip()
        if (ident and known_id.lower() == ident) or (
                name and known_name.lower() == name.lower()):
            ident, name = ident or known_id.lower(), name or known_name
            community = str(getattr(channel, "community_id", "") or community).strip().lower()
            break
    found = mapping.identify(community_id=community, channel_id=ident, channel_name=name)
    return found.project if found is not None else ""
