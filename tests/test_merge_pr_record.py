"""`scripts/merge_pr.sh` publishes the applied record, and says so when it cannot.

The merge itself is covered elsewhere (`test_live_checkout_guard.py`); this is about the step
after the push. The script is run for real against a sandbox clone with a stand-in `ngit` that
applies one commit, and a stand-in `hng` that records the arguments the script gave it. What
is asserted is the contract that was missing in production: the record is published for the
commit the push left, and a merge whose record did not publish exits 8 with the retry named.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from git_sandbox import Sandbox, clean_env, git, make_clone

REPO = Path(__file__).resolve().parents[1]
MERGE = REPO / "scripts" / "merge_pr.sh"
GUARD = REPO / "scripts" / "live_checkout_guard.sh"
PUSH_GUARD = REPO / "scripts" / "worker_push_guard.sh"

#: A stand-in ngit: the two calls the script makes, and a patch application that leaves a
#: commit behind -- which is what `ngit pr apply` does to the worktree (`-d` is detached).
#: The commit carries a `Card:` trailer because the merge route now refuses one that does not
#: (master's exit 6), and this fixture is about the step after that refusal.
NGIT = """#!/bin/sh
case "$1 $2" in
  "pr view") echo "Status:   open"; exit 0 ;;
  "pr apply") echo applied > applied.txt; git add applied.txt
              git commit -qm "apply the proposal" -m "Card: operator" || exit 1; exit 0 ;;
esac
echo "stub ngit: unexpected $*" >&2
exit 99
"""

#: A stand-in hng: record the command line, then answer like the real one.
HNG = """#!/bin/sh
printf '%s\\n' "$*" >> "$HNG_LOG"
if [ "${HNG_RECORD_STATUS:-0}" != 0 ]; then
  echo "applied-record FAILED deadbeef: no relay took the status" >&2
  exit 1
fi
echo "applied-record deadbeef -> 0123456789abcdef | event aaaaaaaa on 2/2 relays"
"""


def write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def prepare(tmp_path: Path) -> tuple[Sandbox, dict[str, str], Path]:
    """A sandbox clone with the three scripts, a config, and the two stand-ins on PATH."""
    made = make_clone(tmp_path)
    scripts = made.repo / "scripts"
    scripts.mkdir()
    for script in (MERGE, PUSH_GUARD, GUARD):
        shutil.copy2(script, scripts / script.name)
    # `hng` resolves config.yaml from the checkout; the stand-in ignores its contents.
    (made.repo / "config.yaml").write_text("ngit:\n  assignee: default\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    write_executable(bindir / "ngit", NGIT)

    venv = tmp_path / "venv" / "bin"
    venv.mkdir(parents=True)
    write_executable(venv / "hng", HNG)
    write_executable(venv / "ruff", "#!/bin/sh\nexit 0\n")
    write_executable(venv / "python", """#!/bin/sh
case "$*" in
  *pytest*) echo "1 passed in 0.01s" ;;
esac
exit 0
""")
    log = tmp_path / "hng.log"
    env = clean_env(PATH=f"{bindir}:{os.environ.get('PATH', '')}", VENV=str(tmp_path / "venv"),
                    HNG_LOG=str(log))
    return made, env, log


def run_merge(made: Sandbox, env: dict[str, str], *args: str):
    return subprocess.run(["bash", str(made.repo / "scripts" / "merge_pr.sh"), *args],
                          cwd=made.repo, env=env, capture_output=True, text=True, timeout=180)


def test_a_merge_publishes_the_applied_record_for_the_commit_it_pushed(tmp_path) -> None:
    made, env, log = prepare(tmp_path)

    result = run_merge(made, env, "deadbeef")

    assert result.returncode == 0, result.stdout + result.stderr
    pushed = git(made.repo, "rev-parse", "origin/master").stdout.strip()
    recorded = log.read_text(encoding="utf-8")
    # The record names the proposal and the commit master now points at -- the worktree's
    # HEAD after `ngit pr apply`, which is what the push moved.
    assert "ngit applied deadbeef" in recorded
    assert f"--commit {pushed}" in recorded
    assert f"--repo {made.repo}" in recorded
    assert "applied-record" in result.stdout


def test_a_merge_whose_record_did_not_publish_exits_8_and_names_the_retry(tmp_path) -> None:
    made, env, log = prepare(tmp_path)
    env = {**env, "HNG_RECORD_STATUS": "1"}

    result = run_merge(made, env, "deadbeef")

    # The content is in; the bookkeeping is not. That is a distinct outcome, not a failed
    # merge and not a success: the PR still reads open and a guarded card refuses completion.
    # Exit 8, not 6/7: those are the merge route's two authorship refusals.
    assert result.returncode == 8, result.stdout + result.stderr
    assert "content is in master" in result.stdout
    assert "the applied record is not published" in result.stdout
    assert "ngit applied deadbeef --commit" in result.stdout
    pushed = git(made.repo, "rev-parse", "origin/master").stdout.strip()
    assert pushed in result.stdout


def test_the_checked_merge_publishes_nothing(tmp_path) -> None:
    made, env, log = prepare(tmp_path)

    result = run_merge(made, env, "deadbeef", "--check")

    assert result.returncode == 0, result.stdout + result.stderr
    assert not log.exists()
    assert "would apply" in result.stdout
