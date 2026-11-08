"""A card workspace cannot push master; the operator's checkout still can.

The guard is a `pre-push` hook in the clone's shared hooks directory, and `install` writes the
versioned script there unchanged. These tests build a throwaway clone with a card worktree under
`.worktrees/`, push for real, and read the result off the origin — so what is under test is the
file git actually runs, not a re-implementation of it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "worker_push_guard.sh"
MERGE = REPO / "scripts" / "merge_pr.sh"

#: The dispatcher's markers. The test process is itself a kanban worker, so every child that
#: should look like an operator has to be scrubbed of them or the guard refuses its push too.
MARKERS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in MARKERS}
    env.update(
        {
            "GIT_AUTHOR_NAME": "Guard Test",
            "GIT_AUTHOR_EMAIL": "guard@example.invalid",
            "GIT_COMMITTER_NAME": "Guard Test",
            "GIT_COMMITTER_EMAIL": "guard@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    env.update(extra)
    return env


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    result = subprocess.run(
        ["git", *args], cwd=cwd, env=env or clean_env(), capture_output=True, text=True, timeout=120
    )
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result


def guard(cwd: Path, *args: str, env: dict[str, str] | None = None, stdin: str | None = None):
    return subprocess.run(
        ["/bin/sh", str(GUARD), *args],
        cwd=cwd,
        env=env or clean_env(),
        capture_output=True,
        text=True,
        input=stdin,
        timeout=120,
    )


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def commit(worktree: Path, name: str = "change.txt") -> str:
    (worktree / name).write_text(name, encoding="utf-8")
    git(worktree, "add", name)
    git(worktree, "commit", "-qm", f"add {name}")
    return head(worktree)


def make_repo(tmp_path: Path, *, card: bool = True) -> SimpleNamespace:
    """A bare origin, a checkout level with it, and (optionally) a card worktree under it."""
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-b", "master", str(repo))
    git(repo, "remote", "add", "origin", str(origin))
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "one")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
    card_path = None
    if card:
        card_path = repo / ".worktrees" / "t_card"
        git(repo, "worktree", "add", "-q", str(card_path), "-b", "card-branch")
    return SimpleNamespace(origin=origin, repo=repo, card=card_path)


@pytest.fixture
def sandbox(tmp_path: Path) -> SimpleNamespace:
    """A throwaway clone with the guard installed into its shared hooks directory."""
    made = make_repo(tmp_path)
    installed = guard(made.repo, "install")
    assert installed.returncode == 0, installed.stderr
    made.hook = made.repo / ".git" / "hooks" / "pre-push"
    return made


def test_the_hook_install_writes_the_versioned_script(sandbox: SimpleNamespace) -> None:
    """What runs is the file in the repository — no copy that can drift from the tests."""
    assert sandbox.hook.read_bytes() == GUARD.read_bytes()
    assert os.access(sandbox.hook, os.X_OK)


def test_a_card_worktree_cannot_push_master(sandbox: SimpleNamespace) -> None:
    before = git(sandbox.origin, "rev-parse", "master").stdout.strip()
    tip = commit(sandbox.card)
    assert tip != before

    push = git(sandbox.card, "push", "origin", "HEAD:refs/heads/master", check=False)

    assert push.returncode != 0
    assert "REFUSED: a card workspace cannot push refs/heads/master" in push.stderr
    assert "scripts/merge_pr.sh" in push.stderr
    assert "docs/working-on-this-repo.md" in push.stderr
    assert git(sandbox.origin, "rev-parse", "master").stdout.strip() == before


def test_the_operators_checkout_can_push_master(sandbox: SimpleNamespace) -> None:
    """The same push, one directory over, is a merge the operator is allowed to make."""
    before = git(sandbox.origin, "rev-parse", "master").stdout.strip()
    tip = commit(sandbox.repo)
    assert tip != before

    push = git(sandbox.repo, "push", "origin", "HEAD:refs/heads/master", check=False)

    assert push.returncode == 0, push.stderr
    assert git(sandbox.origin, "rev-parse", "master").stdout.strip() == tip


def test_a_card_worktree_can_still_push_its_own_branch(sandbox: SimpleNamespace) -> None:
    tip = commit(sandbox.card)

    push = git(sandbox.card, "push", "origin", "HEAD:refs/heads/pr/probe", check=False)

    assert push.returncode == 0, push.stderr
    assert git(sandbox.origin, "rev-parse", "refs/heads/pr/probe").stdout.strip() == tip


def test_deleting_master_from_a_card_worktree_is_refused(sandbox: SimpleNamespace) -> None:
    push = git(sandbox.card, "push", "origin", ":refs/heads/master", check=False)

    assert push.returncode != 0
    assert "REFUSED" in push.stderr
    assert git(sandbox.origin, "rev-parse", "master").returncode == 0


def test_main_is_refused_as_well_as_master(sandbox: SimpleNamespace) -> None:
    line = f"HEAD {head(sandbox.card)} refs/heads/main {'0' * 40}\n"

    decision = guard(sandbox.card, "check", stdin=line)

    assert decision.returncode == 1
    assert "refs/heads/main" in decision.stderr


def test_the_dispatchers_marker_refuses_a_worktree_outside_dot_worktrees(
    sandbox: SimpleNamespace,
) -> None:
    """The path convention is not the only signal: the dispatcher's env is enough on its own."""
    elsewhere = sandbox.repo.parent / "scratch" / "t_card"
    git(sandbox.repo, "worktree", "add", "-q", str(elsewhere), "-b", "elsewhere")
    commit(elsewhere)

    push = git(
        elsewhere,
        "push",
        "origin",
        "HEAD:refs/heads/master",
        env=clean_env(HERMES_KANBAN_WORKSPACE=str(elsewhere)),
        check=False,
    )

    assert push.returncode != 0
    assert "HERMES_KANBAN_WORKSPACE" in push.stderr


def test_a_worktree_with_no_marker_is_the_operators(sandbox: SimpleNamespace) -> None:
    """Stated so the boundary is visible: the guard keys the marker, not the directory name.

    A worktree the dispatcher did not make, pushed from a shell it did not mark, is an
    operator's — which is also why a worker that builds its own worktree and unsets the
    markers is making the merge a deliberate act rather than an accident.
    """
    elsewhere = sandbox.repo.parent / "scratch" / "t_plain"
    git(sandbox.repo, "worktree", "add", "-q", str(elsewhere), "-b", "plain")
    tip = commit(elsewhere)

    push = git(elsewhere, "push", "origin", "HEAD:refs/heads/master", check=False)

    assert push.returncode == 0, push.stderr
    assert git(sandbox.origin, "rev-parse", "master").stdout.strip() == tip


def test_install_refuses_to_replace_a_hook_it_does_not_own(tmp_path: Path) -> None:
    made = make_repo(tmp_path, card=False)
    hooks = made.repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    foreign = hooks / "pre-push"
    foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    installed = guard(made.repo, "install")

    assert installed.returncode == 3
    assert "not the worker push guard" in installed.stderr
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


def test_uninstall_removes_its_own_hook_and_leaves_a_foreign_one(sandbox: SimpleNamespace) -> None:
    removed = guard(sandbox.repo, "uninstall")
    assert removed.returncode == 0
    assert not sandbox.hook.exists()

    sandbox.hook.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    refused = guard(sandbox.repo, "uninstall")

    assert refused.returncode == 3
    assert sandbox.hook.exists()


def test_operator_only_passes_for_the_operator_and_refuses_a_card(sandbox: SimpleNamespace) -> None:
    assert guard(sandbox.repo, "operator-only").returncode == 0

    refused = guard(sandbox.card, "operator-only")

    assert refused.returncode == 1
    assert "REFUSED" in refused.stderr
    assert "scripts/merge_pr.sh" in refused.stderr


def test_status_names_the_hook_and_the_shell(sandbox: SimpleNamespace) -> None:
    in_card = guard(sandbox.card, "status")
    assert in_card.returncode == 0
    assert "installed" in in_card.stdout
    assert "card workspace" in in_card.stdout

    as_operator = guard(sandbox.repo, "status")
    assert as_operator.returncode == 0
    assert "operator" in as_operator.stdout


def test_status_calls_a_stale_install_stale(sandbox: SimpleNamespace) -> None:
    """An installed copy that has drifted from the script must not read as current."""
    sandbox.hook.write_text(
        sandbox.hook.read_text(encoding="utf-8") + "\n# an older install\n", encoding="utf-8"
    )

    status = guard(sandbox.repo, "status")

    assert status.returncode == 0
    assert "STALE" in status.stdout


def test_merge_pr_help_does_not_print_the_shell_line(tmp_path: Path) -> None:
    help_text = subprocess.run(
        ["bash", str(MERGE), "--help"], capture_output=True, text=True, timeout=60
    )

    assert help_text.returncode == 0
    assert "Apply a PR and gate it" in help_text.stdout
    assert "set -uo pipefail" not in help_text.stdout


def test_install_from_another_directory_writes_the_shared_hook(tmp_path: Path) -> None:
    """`install <repo>` from elsewhere must not create `<cwd>/.git/hooks` or miss the real one."""
    made = make_repo(tmp_path, card=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    installed = guard(elsewhere, "install", str(made.repo))

    assert installed.returncode == 0, installed.stderr
    assert (made.repo / ".git" / "hooks" / "pre-push").exists()
    assert not (elsewhere / ".git").exists()


def test_status_fails_where_the_guard_is_not_installed(tmp_path: Path) -> None:
    made = make_repo(tmp_path, card=False)

    status = guard(made.repo, "status")

    assert status.returncode == 1
    assert "NOT installed" in status.stdout


def test_merge_pr_refuses_a_card_workspace(tmp_path: Path) -> None:
    """The documented merge route is not a way around the push guard.

    Run with bash because that is the script's shebang: `/bin/sh` is dash on the CI image and
    rejects `set -o pipefail`, which would test the shell rather than the guard.
    """
    made = make_repo(tmp_path, card=False)
    scripts = made.repo / "scripts"
    scripts.mkdir()
    shutil.copy2(MERGE, scripts / "merge_pr.sh")
    shutil.copy2(GUARD, scripts / "worker_push_guard.sh")

    refused = subprocess.run(
        ["bash", str(scripts / "merge_pr.sh"), "deadbeef"],
        cwd=made.repo,
        env=clean_env(HERMES_KANBAN_WORKSPACE=str(made.repo)),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert refused.returncode == 4, refused.stdout + refused.stderr
    assert "REFUSED" in refused.stderr
    assert "operator checkout" in refused.stderr
