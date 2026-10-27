"""Channel → project mapping: which checkout a community channel's work belongs to.

A Concord channel is a context, not a repository. What actually links an inbound message to
a project is a lookup the gateway can do *before* the agent sees it: community + channel ->
checkout path. The agent then starts in the right tree with the right `AGENTS.md`, instead of
guessing from a channel name or being told every time.

Configuration, under `concord.projects`:

```yaml
concord:
  projects:
    - channel: Ops                      # channel name, or its hex id
      path: ~/Projects/hermes-nostr-gateway
      name: hermes-nostr-gateway        # optional; defaults to the directory name
      community: f56b6914…              # optional; restrict to one community id
```

Rules, in order of precedence:

- an entry with a `community` only matches that community; one without matches any;
- a channel matches on its id (hex, case-insensitive) or its current name (case-insensitive);
- an id match beats a name match, so a renamed channel keeps its project;
- the first entry wins a tie, and the duplicate is logged at load time.

A mapping whose `path` is missing is kept and reported (see `hng projects`) rather than
dropped: an unmounted or moved checkout is a fact to show, not a silent config edit.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("hng.projects")


@dataclass(frozen=True)
class Project:
    """One channel → checkout mapping, and whether the checkout is actually there."""

    channel: str
    path: Path
    name: str
    community: str = ""
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem

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
            "ok": self.ok,
            "problem": self.problem,
            "git": self.is_git,
            "agents_md": self.has_agents_md,
        }


def _expand(value: str) -> Path:
    return Path(os.path.expanduser(str(value))).absolute()


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
    problem = "" if path.is_dir() else "path does not exist"
    return Project(channel=channel, path=path, name=name,
                   community=str(entry.get("community") or "").strip(), problem=problem)


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
            if not project.ok:
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

    def resolve(self, *, community_id: str = "", channel_id: str = "",
                channel_name: str = "") -> Optional[Project]:
        """The checkout for this channel, or None (unmapped or unusable path).

        Id matches are tried before name matches, across the whole map, so renaming a
        channel cannot silently steal another channel's project.
        """
        community = (community_id or "").strip().lower()
        ident = (channel_id or "").strip().lower()
        name = (channel_name or "").strip().lower()

        def scoped(project: Project) -> bool:
            return not project.community or project.community.strip().lower() == community

        for project in self._projects:
            if not project.ok or not scoped(project):
                continue
            if ident and project.channel.strip().lower() == ident:
                return project
        for project in self._projects:
            if not project.ok or not scoped(project):
                continue
            if name and project.channel.strip().lower() == name:
                return project
        return None
