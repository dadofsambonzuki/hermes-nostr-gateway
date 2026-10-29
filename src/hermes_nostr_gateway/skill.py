"""The repository's own agent skill, and installing it where Hermes reads skills.

The skill is the generic half of what makes a fresh install fast: the verbs, the failure
vocabulary and the traps, with placeholders where a box's ids go. The repository is the
canonical copy (`skills/nostr-gateway/`); a wheel carries the same tree as package data so
`hng skill install` works from an installed build too.

Nothing here touches the operator's own skills: the install only ever writes
`<hermes home>/skills/nostr-gateway/`, or the profile's copy of it, and an existing,
different copy is refused unless `--force` is asked for.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

#: The skill's directory name, and the one `$HERMES_HOME/skills` entry it owns.
NAME = "nostr-gateway"


class SkillError(RuntimeError):
    """The skill cannot be found or installed; the message says what to change."""


def bundled_dir() -> Path:
    """Where this build keeps the skill: package data first, then the repository tree.

    A checkout has `skills/` at the root, three levels above this module's own directory;
    an installed wheel has the same files copied in beside the package (see pyproject's
    force-include), which is the copy that wins because it always exists in a build.
    """
    here = Path(__file__).resolve().parent
    candidates = [here / "skills" / NAME]
    candidates.extend(parent / "skills" / NAME for parent in here.parents)
    for candidate in candidates:
        if (candidate / "SKILL.md").is_file():
            return candidate
    raise SkillError(
        "the bundled skill is missing from this build (expected skills/nostr-gateway/"
        "SKILL.md beside the repository or the package); reinstall hermes-nostr-gateway")


def content() -> str:
    """`SKILL.md` exactly as shipped, for `hng skill install --print`."""
    try:
        return (bundled_dir() / "SKILL.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise SkillError(f"cannot read the bundled skill: {exc.strerror or exc}") from None


def hermes_home() -> Path:
    """`$HERMES_HOME`, then `~/.hermes` — the same rule the rest of the repo uses."""
    return Path(os.path.expanduser(os.environ.get("HERMES_HOME") or "~/.hermes")).absolute()


def target_dir(profile: str | None = None) -> Path:
    """Where an install goes: the shared skills tree, or one profile's own.

    A profile's skills tree is used only when `--profile` names one; the default is the
    skills directory every session of the default profile reads.
    """
    home = hermes_home()
    name = str(profile or "").strip()
    if name:
        return home / "profiles" / name / "skills" / NAME
    return home / "skills" / NAME


@dataclass(frozen=True)
class Install:
    """What an install did, in a shape the CLI can print and a test can assert."""

    path: Path
    action: str            # "installed" (files written) or "current" (nothing to do)
    written: tuple[str, ...]
    unchanged: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {"path": str(self.path), "action": self.action,
                "written": list(self.written), "unchanged": list(self.unchanged)}


def _files(root: Path) -> list[Path]:
    return sorted((path for path in root.rglob("*") if path.is_file()),
                  key=lambda path: str(path.relative_to(root)))


def install(profile: str | None = None, *, force: bool = False) -> Install:
    """Copy the bundled skill into Hermes, idempotently.

    A file that already matches is left alone (a second install writes nothing and still
    succeeds). A file that differs is refused unless `force` is set: the skill may have
    been edited in place by the operator, and silently replacing that would lose their
    edits. Refusing is all-or-nothing — nothing is written when any file conflicts.
    """
    source = bundled_dir()
    destination = target_dir(profile)
    written: list[str] = []
    unchanged: list[str] = []
    conflicts: list[str] = []
    for path in _files(source):
        relative = path.relative_to(source)
        target = destination / relative
        wanted = path.read_bytes()
        if target.is_file():
            try:
                if target.read_bytes() == wanted:
                    unchanged.append(str(relative))
                    continue
            except OSError:
                pass
            if not force:
                conflicts.append(str(relative))
                continue
        written.append(str(relative))
    if conflicts:
        raise SkillError(
            f"{destination} already holds a different {', '.join(conflicts)}; "
            "re-run with --force to replace it")
    for relative in written:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / relative, target)
    return Install(path=destination, action="installed" if written else "current",
                   written=tuple(written), unchanged=tuple(unchanged))
