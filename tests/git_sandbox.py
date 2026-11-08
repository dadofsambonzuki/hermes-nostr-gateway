"""A throwaway clone with a bare origin, for tests that ask git a question.

Not a fixture: a test that needs a checkout on `master`, on a proposal branch or detached
builds one, moves it, and then asserts against what git actually says. Global and system git
config are disabled so the host's identity, signing setup or hooks cannot change a verdict.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

#: The dispatcher's markers. A test process is itself a kanban worker, so any child that should
#: look like an operator has to be scrubbed of them.
MARKERS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in MARKERS}
    env.update(
        {
            "GIT_AUTHOR_NAME": "Sandbox Test",
            "GIT_AUTHOR_EMAIL": "sandbox@example.invalid",
            "GIT_COMMITTER_NAME": "Sandbox Test",
            "GIT_COMMITTER_EMAIL": "sandbox@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    env.update(extra)
    return env


def git(cwd: Path, *args: str, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", *args], cwd=cwd, env=env or clean_env(),
                            capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result


def commit(repo: Path, name: str = "change.txt") -> str:
    """One commit; returns its full sha."""
    (repo / name).write_text(name + "\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"add {name}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


@dataclass
class Sandbox:
    """A clone with a bare origin, and the questions a test asks about it."""

    origin: Path
    repo: Path

    @property
    def branch(self) -> str:
        return git(self.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    @property
    def head(self) -> str:
        return git(self.repo, "rev-parse", "HEAD").stdout.strip()

    @property
    def short(self) -> str:
        return git(self.repo, "rev-parse", "--short", "HEAD").stdout.strip()


def make_clone(tmp_path: Path) -> Sandbox:
    """A clone on `master` whose `origin/master` is the commit it is on."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "live"
    git(tmp_path, "init", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-b", "master", str(repo))
    git(repo, "remote", "add", "origin", str(origin))
    commit(repo, "a.txt")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
    return Sandbox(origin=origin, repo=repo)


def advance_origin(made: Sandbox, tmp_path: Path, name: str = "b.txt") -> str:
    """Move origin/master forward the way a merge does, then fetch it. Returns the new sha."""
    other = tmp_path / "someone-else"
    git(tmp_path, "clone", "-q", str(made.origin), str(other))
    tip = commit(other, name)
    git(other, "push", "-q", "origin", "HEAD:refs/heads/master")
    git(made.repo, "fetch", "-q", "origin")
    return tip
