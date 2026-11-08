"""The scripts keep the habit: a branch is a worktree, never the operator's clone.

`scripts/merge_pr.sh` runs the whole merge inside a throwaway worktree, snapshots the clone's
HEAD before the first git command and exits 5 if it moved. `scripts/live_checkout_guard.sh` is
the helper any other script uses. These tests run the real files against a real clone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from git_sandbox import Sandbox, advance_origin, clean_env, commit, git, make_clone
from hermes_nostr_gateway import checkout

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "live_checkout_guard.sh"
MERGE = REPO / "scripts" / "merge_pr.sh"
PUSH_GUARD = REPO / "scripts" / "worker_push_guard.sh"


def guard(cwd: Path, *args: str, env: dict[str, str] | None = None):
    return subprocess.run(["/bin/sh", str(GUARD), *args], cwd=cwd, env=env or clean_env(),
                          capture_output=True, text=True, timeout=120)


def park_on_a_proposal(made: Sandbox, name: str = "pr/blackhole-unauthorised") -> None:
    git(made.repo, "switch", "-q", "-c", name)
    commit(made.repo, "proposal.txt")


def test_the_guard_status_is_the_line_hng_status_prints(tmp_path) -> None:
    """Two implementations of one line; they must not drift."""
    made = make_clone(tmp_path)
    advance_origin(made, tmp_path)

    printed = guard(tmp_path, "status", str(made.repo))
    python = checkout.inspect(made.repo)

    assert printed.returncode == 0, printed.stderr
    assert printed.stdout.strip() == python.status_line()


def test_the_guard_check_passes_on_master_and_fails_on_a_proposal(tmp_path) -> None:
    made = make_clone(tmp_path)
    assert guard(tmp_path, "check", str(made.repo)).returncode == 0

    park_on_a_proposal(made)
    refused = guard(tmp_path, "check", str(made.repo))

    assert refused.returncode == 1
    assert "pr/blackhole-unauthorised" in refused.stdout
    assert "REFUSED" in refused.stderr
    assert "git checkout master && git pull" in refused.stderr


def test_the_guard_check_fails_on_local_commits_on_master(tmp_path) -> None:
    made = make_clone(tmp_path)
    commit(made.repo, "local.txt")

    refused = guard(tmp_path, "check", str(made.repo))

    assert refused.returncode == 1
    assert "1 commit(s) origin/master does not" in refused.stderr


def test_a_detached_head_reads_as_detached(tmp_path) -> None:
    made = make_clone(tmp_path)
    git(made.repo, "checkout", "-q", "--detach", "HEAD")

    printed = guard(tmp_path, "status", str(made.repo))

    assert printed.stdout.strip().startswith("checkout: (detached) @ ")
    assert guard(tmp_path, "check", str(made.repo)).returncode == 1


def test_snapshot_and_unchanged_notice_a_switch(tmp_path) -> None:
    made = make_clone(tmp_path)
    before = guard(tmp_path, "snapshot", str(made.repo))
    assert before.returncode == 0, before.stderr
    assert guard(tmp_path, "unchanged", before.stdout.strip(),
                 str(made.repo)).returncode == 0

    park_on_a_proposal(made)
    moved = guard(tmp_path, "unchanged", before.stdout.strip(), str(made.repo))

    assert moved.returncode == 1
    assert "the live checkout moved" in moved.stderr
    assert "takes a worktree" in moved.stderr


def test_the_worktree_verb_leaves_the_clone_alone(tmp_path) -> None:
    made = make_clone(tmp_path)
    before = (made.branch, made.head)
    tree = tmp_path / "try-it"

    added = guard(tmp_path, "worktree", str(tree), "origin/master", str(made.repo))

    assert added.returncode == 0, added.stderr
    assert (tree / "a.txt").exists()
    assert (made.branch, made.head) == before


#: What `ngit pr view <pr> --json --comments` answers here: a PR whose newest verdict was
#: signed by someone other than its author. `scripts/merge_pr.sh` reads that before it applies
#: anything, so a stand-in that refuses every call would stop it at the authorship check
#: instead of at the step a test is about.
SECOND_PARTY_VIEW = json.dumps({
    "author": "1" * 64,
    "comments": [{"id": "c" * 64, "author": "2" * 64,
                  "body": "Verdict: approved", "created_at": 1}],
})

#: A stand-in ngit: answers the two reads the merge route makes, and fails everything else so
#: the merge stops at the apply without a PR.
FAKE_NGIT = """#!/bin/sh
case "$*" in
  *--json*) cat "$FAKE_PR_JSON" ;;
  "pr view"*) printf 'Status:   open\\n' ;;
  *) exit 1 ;;
esac
"""


def failing_merge_env(tmp_path: Path, bindir: Path) -> dict[str, str]:
    """The environment a merge route needs to reach the apply step in a throwaway clone.

    The authorship check is part of the route now and it shells out to python, with `$REPO/src`
    on its path; a copied script in a sandbox clone has neither, so both are given here.
    """
    view = tmp_path / "pr.json"
    view.write_text(SECOND_PARTY_VIEW, encoding="utf-8")
    (bindir / "ngit").write_text(FAKE_NGIT, encoding="utf-8")
    (bindir / "ngit").chmod(0o755)
    return clean_env(PATH=f"{bindir}:{os.environ.get('PATH', '')}",
                     PYTHON=sys.executable, PYTHONPATH=str(REPO / "src"),
                     FAKE_PR_JSON=str(view))


def test_merge_pr_leaves_the_clone_on_the_branch_it_found(tmp_path) -> None:
    """The script-driven proof: a failed merge never moves the tree the daemon serves."""
    made = make_clone(tmp_path)
    scripts = made.repo / "scripts"
    scripts.mkdir()
    for script in (MERGE, PUSH_GUARD, GUARD):
        shutil.copy2(script, scripts / script.name)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = failing_merge_env(tmp_path, bindir)
    before = (made.branch, made.head)

    result = subprocess.run(["bash", str(scripts / "merge_pr.sh"), "deadbeef"],
                            cwd=made.repo, env=env, capture_output=True, text=True,
                            timeout=180)

    assert result.returncode == 1, result.stdout + result.stderr
    assert (made.branch, made.head) == before


def test_merge_pr_leaves_a_clone_parked_on_a_proposal_branch_where_it_was(tmp_path) -> None:
    """A clone already on a proposal is not the script's to fix, and not its to make worse."""
    made = make_clone(tmp_path)
    park_on_a_proposal(made)
    scripts = made.repo / "scripts"
    scripts.mkdir()
    for script in (MERGE, PUSH_GUARD, GUARD):
        shutil.copy2(script, scripts / script.name)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    env = failing_merge_env(tmp_path, bindir)
    before = (made.branch, made.head)

    result = subprocess.run(["bash", str(scripts / "merge_pr.sh"), "deadbeef"],
                            cwd=made.repo, env=env, capture_output=True, text=True,
                            timeout=180)

    assert result.returncode == 1, result.stdout + result.stderr
    assert "checkout: pr/blackhole-unauthorised" in result.stdout
    assert (made.branch, made.head) == before
