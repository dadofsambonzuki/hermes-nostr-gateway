"""A merge route that knows which card it is landing.

Two halves, one rule. Every commit a worker pushes carries `Card: t_xxxxxxxx`, written by a
`commit-msg` hook (`scripts/card_trailer.sh`) so `git log` answers "which card wrote this line"
without the board; and a merge is refused while the card is still work in progress, read from
the bridge's `ngit_links` row and the board's `tasks.status`.

The merge tests run **the real script** against a fake `ngit`: a status answer, the PR view the
authorship check reads, and an `apply` that commits the messages it is given the way `git am`
does. Everything else — the worktree, the trailer walk, the board read, the gate, the exit code
— is the code under test.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
MERGE = REPO / "scripts" / "merge_pr.sh"
GUARD = REPO / "scripts" / "worker_push_guard.sh"
CHECKOUT = REPO / "scripts" / "live_checkout_guard.sh"
TRAILER = REPO / "scripts" / "card_trailer.sh"

#: The dispatcher's markers. The test process is itself a kanban worker, so every child that
#: should look like an operator has to be scrubbed of them.
MARKERS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")

#: What ngit prints for the PR id; the fake answers whatever the script asks about.
PR = "2207969b" + "0" * 56
OTHER_PR = "f" * 64


def clean_env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in MARKERS}
    env.update(
        {
            "GIT_AUTHOR_NAME": "Merge Test",
            "GIT_AUTHOR_EMAIL": "merge@example.invalid",
            "GIT_COMMITTER_NAME": "Merge Test",
            "GIT_COMMITTER_EMAIL": "merge@example.invalid",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = str(REPO / "src")
    env.update(extra)
    return env


def git(cwd: Path, *args: str, env: dict[str, str] | None = None, check: bool = True):
    result = subprocess.run(["git", *args], cwd=cwd, env=env or clean_env(),
                            capture_output=True, text=True, timeout=120)
    if check and result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{result.stderr}")
    return result


def script(path: Path, *args: str, cwd: Path, env: dict[str, str] | None = None):
    """Run one of the shell scripts as git would: through the interpreter its shebang names."""
    return subprocess.run(["/bin/sh", str(path), *args], cwd=cwd, env=env or clean_env(),
                          capture_output=True, text=True, timeout=300)


def commit(repo: Path, name: str = "change.txt", message: str | None = None) -> str:
    (repo / name).write_text(name + "\n", encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", message or f"add {name}")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def trailer_of(repo: Path) -> str:
    return git(repo, "log", "-1", "--format=%(trailers:key=Card,valueonly)").stdout.strip()


# -- the hook ------------------------------------------------------------------


def hook_repo(tmp_path: Path, *, install: bool = True) -> SimpleNamespace:
    """A throwaway clone whose shared hooks directory carries the trailer hook."""
    origin, repo = tmp_path / "origin.git", tmp_path / "hook-repo"
    git(tmp_path, "init", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-b", "master", str(repo))
    git(repo, "remote", "add", "origin", str(origin))
    commit(repo, "a.txt")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
    if install:
        installed = script(TRAILER, "install", str(repo), cwd=repo)
        assert installed.returncode == 0, installed.stderr
    return SimpleNamespace(origin=origin, repo=repo)


def test_a_card_worktrees_commit_names_the_card_in_its_path(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    card = made.repo / ".worktrees" / "t_0123abcd-the-card-slug"
    git(made.repo, "worktree", "add", "-q", str(card), "-b", "card-branch")

    commit(card, "b.txt")

    assert trailer_of(card) == "t_0123abcd"


def test_the_dispatchers_marker_wins_even_outside_dot_worktrees(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    elsewhere = tmp_path / "scratch" / "t_89abcdef"
    git(made.repo, "worktree", "add", "-q", str(elsewhere), "-b", "marked")

    (elsewhere / "c.txt").write_text("c\n", encoding="utf-8")
    git(elsewhere, "add", "c.txt")
    git(elsewhere, "commit", "-qm", "add c",
        env=clean_env(HERMES_KANBAN_WORKSPACE=str(elsewhere)))

    assert trailer_of(elsewhere) == "t_89abcdef"


def test_the_operators_commit_says_operator(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)

    commit(made.repo, "b.txt")

    assert trailer_of(made.repo) == "operator"
    assert "Card: operator" in git(made.repo, "log", "-1", "--format=%B").stdout


def test_a_message_that_already_names_a_card_is_left_alone(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)

    (made.repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(made.repo, "add", "b.txt")
    git(made.repo, "commit", "-qm", "add b", "--trailer", "Card: t_deadbeef")

    assert trailer_of(made.repo) == "t_deadbeef"
    assert git(made.repo, "log", "-1", "--format=%B").stdout.count("Card:") == 1


def test_a_card_workspace_that_cannot_name_its_card_refuses_the_commit(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    (made.repo / "b.txt").write_text("b\n", encoding="utf-8")
    git(made.repo, "add", "b.txt")

    refused = git(made.repo, "commit", "-qm", "add b",
                  env=clean_env(HERMES_KANBAN_TASK="not-a-card"), check=False)

    assert refused.returncode != 0
    assert "REFUSED" in refused.stderr
    assert "t_xxxxxxxx" in refused.stderr
    assert git(made.repo, "log", "--oneline").stdout.count("\n") == 1


def test_the_hook_is_idempotent_on_amend(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    commit(made.repo, "b.txt")

    git(made.repo, "commit", "--amend", "-qm", "add b, better")

    assert git(made.repo, "log", "-1", "--format=%B").stdout.count("Card:") == 1


def test_status_names_the_value_this_shell_would_write(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    card = made.repo / ".worktrees" / "t_0badf00d-slug"
    git(made.repo, "worktree", "add", "-q", str(card), "-b", "card")

    status = script(TRAILER, "status", str(made.repo), cwd=made.repo)

    assert status.returncode == 0
    assert "installed" in status.stdout
    assert "card: operator" in status.stdout


def test_status_calls_a_stale_install_stale(tmp_path: Path) -> None:
    made = hook_repo(tmp_path)
    installed = made.repo / ".git" / "hooks" / "commit-msg"
    installed.write_text(installed.read_text(encoding="utf-8") + "\n# older\n", encoding="utf-8")

    status = script(TRAILER, "status", str(made.repo), cwd=made.repo)

    assert status.returncode == 0
    assert "STALE" in status.stdout


def test_install_refuses_to_replace_a_hook_it_does_not_own(tmp_path: Path) -> None:
    made = hook_repo(tmp_path, install=False)
    hooks = made.repo / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    foreign = hooks / "commit-msg"
    foreign.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")

    installed = script(TRAILER, "install", str(made.repo), cwd=made.repo)

    assert installed.returncode == 3
    assert "not the card trailer" in installed.stderr
    assert foreign.read_text(encoding="utf-8") == "#!/bin/sh\nexit 0\n"


# -- the merge route -----------------------------------------------------------


FAKE_NGIT = """#!/bin/sh
# A stand-in for ngit, enough for scripts/merge_pr.sh: answer a status, answer the PR view the
# authorship check reads, and apply the commits in $FAKE_PR_DIR the way `git am` does --
# verbatim messages, and no commit-msg hook.
case "${1:-} ${2:-}" in
  "pr view")
    if [ "${4:-}" = "--json" ]; then
      file="${FAKE_PR_JSON_DIR:-}/${3:-}.json"
      if [ -f "$file" ]; then
        cat "$file"
      else
        cat "${FAKE_PR_JSON:-/dev/null}"
      fi
    else
      printf 'Status:   %s\\n' "${FAKE_PR_STATUS:-open}"
      printf 'Branch:   %s\\n' "${FAKE_PR_BRANCH:-pr/test}"
    fi
    ;;
  "pr list")
    [ -n "${FAKE_PR_LIST_JSON:-}" ] && [ -f "$FAKE_PR_LIST_JSON" ] || exit 1
    cat "$FAKE_PR_LIST_JSON"
    ;;
  "pr apply")
    dir=${FAKE_PR_DIR:-}
    [ -n "$dir" ] || exit 1
    for file in "$dir"/commit-*.txt; do
      [ -f "$file" ] || continue
      name=$(basename "$file" .txt)
      printf '%s\\n' "$name" >> applied.txt || exit 1
      git add applied.txt || exit 1
      git commit -q --no-verify -F "$file" || exit 1
    done
    ;;
  *)
    exit 1
    ;;
esac
"""


def make_bridge(tmp_path: Path, repo: Path, *, cards: dict[str, str],
                links: list[tuple[str, str]] | None = None) -> SimpleNamespace:
    """A gateway store with `ngit_links`, a `gateway` board, and a config naming both."""
    home = tmp_path / "hermes"
    boards = home / "kanban" / "boards" / "gateway"
    boards.mkdir(parents=True, exist_ok=True)
    board = sqlite3.connect(boards / "kanban.db")
    board.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, status TEXT,"
                  " idempotency_key TEXT)")
    board.execute("CREATE TABLE task_comments (id INTEGER PRIMARY KEY, task_id TEXT, body TEXT)")
    board.executemany("INSERT INTO tasks VALUES (?,?,?,?)",
                      [(task_id, f"card {task_id}", status, None)
                       for task_id, status in cards.items()])
    board.commit()
    board.close()

    store = sqlite3.connect(tmp_path / "gateway.db")
    store.execute("CREATE TABLE ngit_links (repo TEXT, kind INTEGER, source_id TEXT,"
                  " task_id TEXT, board TEXT, state TEXT, detail TEXT, created_at INTEGER,"
                  " updated_at INTEGER, PRIMARY KEY (repo, kind, source_id))")
    store.executemany(
        "INSERT INTO ngit_links VALUES (?,?,?,?,?,?,?,?,?)",
        [(f"30617:{'a' * 64}:{repo.name}", 1618, pr_id, task_id, "gateway", "signalled",
          None, 1, 1) for pr_id, task_id in (links or [])],
    )
    store.commit()
    store.close()

    config = tmp_path / "config.yaml"
    config.write_text(
        f"store: {tmp_path / 'gateway.db'}\n"
        "ngit:\n"
        f"  reviewers: [{'2' * 64}]\n"
        "concord:\n"
        "  projects:\n"
        f"    - channel: {'c' * 64}\n"
        "      project: under-test\n"
        "      board: gateway\n"
        f"      path: {repo}\n"
        f"      community: {'d' * 64}\n",
        encoding="utf-8",
    )
    return SimpleNamespace(config=config, home=home,
                           env={"HNG_CONFIG": str(config), "HERMES_HOME": str(home)})


def make_repo(tmp_path: Path) -> SimpleNamespace:
    """A clone carrying the merge route's scripts, a fake `ngit`, and a fake gate."""
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-b", "master", str(origin))
    git(tmp_path, "init", "-b", "master", str(repo))
    git(repo, "remote", "add", "origin", str(origin))
    commit(repo, "a.txt")
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/master")
    # The proposal's branch and its tip: the merge route reads the head from the remote, and
    # the approved verdict has to name that sha for the guard to let the merge through.
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/pr/test")

    scripts = repo / "scripts"
    scripts.mkdir()
    for source in (MERGE, GUARD, CHECKOUT, TRAILER):
        shutil.copy2(source, scripts / source.name)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ngit").write_text(FAKE_NGIT, encoding="utf-8")
    (bindir / "ngit").chmod(0o755)
    #: The PR's thread as the approval check reads it: one verdict, from a key the config's
    #: `ngit.reviewers` names, covering the branch head -- so the merge route is past that
    #: check and on to what a test is about.
    (bindir / "pr.json").write_text(json.dumps({
        "author": "1" * 64,
        "comments": [{"id": "c" * 64, "author": "2" * 64,
                      "body": f"Verdict: approved\n\nTip: {tip}", "created_at": 1}],
    }), encoding="utf-8")

    # The gate is not this test's subject: a ruff that passes, and a pytest that says so --
    # or, when FAKE_GATE_LOG is set, one that records that it ran at all.
    venv = repo / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "ruff").write_text(
        "#!/bin/sh\n"
        'if [ -n "${FAKE_GATE_LOG:-}" ]; then echo ruff >> "$FAKE_GATE_LOG"; fi\n'
        "exit 0\n", encoding="utf-8")
    (venv / "bin" / "python").write_text(
        "#!/bin/sh\n"
        'if [ -n "${FAKE_GATE_LOG:-}" ]; then echo pytest >> "$FAKE_GATE_LOG"; fi\n'
        "echo '1241 passed'\n", encoding="utf-8")
    for fake in ("ruff", "python"):
        (venv / "bin" / fake).chmod(0o755)

    messages = tmp_path / "pr-messages"
    messages.mkdir()
    return SimpleNamespace(origin=origin, repo=repo, scripts=scripts, bindir=bindir,
                           venv=venv, messages=messages, base=tip)


def pr_commits(made: SimpleNamespace, *messages: str) -> None:
    for index, message in enumerate(messages, start=1):
        (made.messages / f"commit-{index}.txt").write_text(message + "\n", encoding="utf-8")


def merge_env(made: SimpleNamespace, bridge: SimpleNamespace | None = None,
              **extra: str) -> dict[str, str]:
    env = clean_env(PATH=f"{made.bindir}:{os.environ.get('PATH', '')}",
                    PYTHON=sys.executable, TMPDIR=str(made.repo.parent),
                    FAKE_PR_DIR=str(made.messages), FAKE_PR_JSON=str(made.bindir / "pr.json"))
    if bridge is not None:
        env.update(bridge.env)
    env.update(extra)
    return env


def merge(made: SimpleNamespace, *args: str, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(made.scripts / "merge_pr.sh"), *args],
                          cwd=made.repo, env=env, capture_output=True, text=True, timeout=300)


def master(made: SimpleNamespace) -> str:
    return git(made.origin, "rev-parse", "master").stdout.strip()


def test_a_pr_with_no_card_trailer_is_refused(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "done"},
                         links=[(PR, "t_a1b2c3d4")])

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 6, refused.stdout + refused.stderr
    assert "carry no `Card:` trailer" in refused.stderr
    assert "add a thing" in refused.stderr
    assert "scripts/card_trailer.sh install" in refused.stderr
    assert master(made) == made.base


def test_every_commit_needs_one_not_just_the_tip(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "first\n\nCard: t_a1b2c3d4", "second")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "review"})

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 6, refused.stdout + refused.stderr
    assert "second" in refused.stderr
    assert "first" not in refused.stderr.split("REFUSED", 1)[1]


def test_a_trailer_that_names_no_card_is_refused(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: bob")
    bridge = make_bridge(tmp_path, made.repo, cards={}, links=[(PR, "t_a1b2c3d4")])

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 6, refused.stdout + refused.stderr
    assert "'bob'" in refused.stderr
    assert "Card: operator" in refused.stderr


def test_a_pr_whose_card_is_running_is_refused_and_named(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_b1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_b1b2c3d4": "running"},
                         links=[(PR, "t_b1b2c3d4")])

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert "t_b1b2c3d4" in refused.stderr
    assert "running" in refused.stderr
    assert "review or done" in refused.stderr
    assert master(made) == made.base


def test_the_linked_card_is_checked_even_when_the_commits_say_operator(tmp_path: Path) -> None:
    """`Card: operator` is how operator work says so; it does not release a worker's card."""
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: operator")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_c1b2c3d4": "running"},
                         links=[(PR, "t_c1b2c3d4")])

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert "t_c1b2c3d4" in refused.stderr
    assert "this PR's card" in refused.stderr


def test_operator_work_with_no_linked_card_lands(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "a typo\n\nCard: operator")
    bridge = make_bridge(tmp_path, made.repo, cards={})

    allowed = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "would apply" in allowed.stdout
    assert master(made) == made.base


def test_a_card_in_review_lands(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_a1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "review"},
                         links=[(PR, "t_a1b2c3d4")])

    allowed = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "would apply" in allowed.stdout
    assert master(made) == made.base


def test_the_gate_does_not_run_for_a_refused_pr(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_b1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_b1b2c3d4": "running"},
                         links=[(PR, "t_b1b2c3d4")])
    log = tmp_path / "gate.log"

    refused = merge(made, PR, "--check", env=merge_env(made, bridge, FAKE_GATE_LOG=str(log)))

    assert refused.returncode == 7
    assert "ruff" not in (log.read_text(encoding="utf-8") if log.exists() else "")


def test_a_blocked_work_card_is_refused(tmp_path: Path) -> None:
    """A work card that stopped is not finished work, whatever a merge would like."""
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_d1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_d1b2c3d4": "blocked"})

    refused = merge(made, OTHER_PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert "t_d1b2c3d4" in refused.stderr
    assert "blocked" in refused.stderr


def test_a_blocked_review_card_is_allowed(tmp_path: Path) -> None:
    """The review brief parks a reviewed PR's card with `kanban_block`; that is not in progress.

    `done` requires applied, so a reviewer cannot complete the card while the PR is open --
    refusing its own parked card would refuse every PR that went through the review lane.
    """
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_a1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "blocked"},
                         links=[(PR, "t_a1b2c3d4")])

    allowed = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert allowed.returncode == 0, allowed.stdout + allowed.stderr
    assert "would apply" in allowed.stdout


def test_a_blocked_work_card_still_refuses_while_the_review_card_is_parked(
        tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_d1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo,
                         cards={"t_d1b2c3d4": "blocked", "t_a1b2c3d4": "blocked"},
                         links=[(PR, "t_a1b2c3d4")])

    refused = merge(made, PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert "t_d1b2c3d4" in refused.stderr
    assert "t_a1b2c3d4" not in refused.stderr


def test_a_pr_the_bridge_has_not_seen_still_checks_its_trailer_card(tmp_path: Path) -> None:
    made = make_repo(tmp_path)
    pr_commits(made, "add a thing\n\nCard: t_e1b2c3d4")
    bridge = make_bridge(tmp_path, made.repo, cards={"t_e1b2c3d4": "ready"})

    refused = merge(made, OTHER_PR, "--check", env=merge_env(made, bridge))

    assert refused.returncode == 7, refused.stdout + refused.stderr
    assert "t_e1b2c3d4" in refused.stderr
    assert "ready" in refused.stderr


def test_a_rebased_proposal_names_the_successor_that_replaced_it(tmp_path: Path) -> None:
    """A proposal that applies no commits is a duplicate: its close names the survivor.

    The merge route refuses the stale proposal, but an operator who closes it by hand needs
    the successor's id and tip in the reason -- an empty kind-1632 leaves a reviewer no
    pointer to the proposal that actually landed.
    """
    made = make_repo(tmp_path)
    successor = "b" * 64
    tip = "3bbb86b5" + "0" * 32
    jsondir = tmp_path / "pr-json"
    jsondir.mkdir()
    (jsondir / f"{PR}.json").write_text(json.dumps({
        "id": PR, "branch": "pr/duplicate(2207969b)", "status": "open",
        "subject": "the stale proposal",
        "comments": [{"id": "c" * 64, "author": "2" * 64,
                      "body": f"Verdict: approved\n\nTip: {made.base}", "created_at": 1}],}), encoding="utf-8")
    (jsondir / f"{successor}.json").write_text(json.dumps({
        "id": successor, "branch": "pr/duplicate(bbbbbbbb)", "status": "open",
        "subject": "the proposal that survived",
        "ci": {"runs": [{"commit": tip}]}}), encoding="utf-8")
    prlist = tmp_path / "pr-list.json"
    prlist.write_text(json.dumps([
        {"id": PR, "branch": "pr/duplicate(2207969b)", "status": "open"},
        {"id": successor, "branch": "pr/duplicate(bbbbbbbb)", "status": "open"},
    ]), encoding="utf-8")
    bridge = make_bridge(tmp_path, made.repo, cards={})

    refused = merge(made, PR, "--check", env=merge_env(
        made, bridge, FAKE_PR_JSON_DIR=str(jsondir), FAKE_PR_LIST_JSON=str(prlist)))

    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert "applies no commits onto origin/master" in refused.stdout
    assert "bbbbbbbb" in refused.stdout
    assert tip in refused.stdout
    assert "--reason 'Superseded by bbbbbbbb (tip " in refused.stdout
    assert master(made) == made.base


def test_a_rebased_proposal_with_no_sibling_still_asks_for_a_named_successor(
        tmp_path: Path) -> None:
    """No other proposal on the branch is not licence for an empty close."""
    made = make_repo(tmp_path)
    jsondir = tmp_path / "pr-json"
    jsondir.mkdir()
    (jsondir / f"{PR}.json").write_text(json.dumps({
        "id": PR, "branch": "pr/only(2207969b)", "status": "open",
        "comments": [{"id": "c" * 64, "author": "2" * 64,
                      "body": f"Verdict: approved\n\nTip: {made.base}", "created_at": 1}]}), encoding="utf-8")
    prlist = tmp_path / "pr-list.json"
    prlist.write_text(json.dumps([
        {"id": PR, "branch": "pr/only(2207969b)", "status": "open"},
    ]), encoding="utf-8")
    bridge = make_bridge(tmp_path, made.repo, cards={})

    refused = merge(made, PR, "--check", env=merge_env(
        made, bridge, FAKE_PR_JSON_DIR=str(jsondir), FAKE_PR_LIST_JSON=str(prlist)))

    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert "no other open proposal shares" in refused.stdout
    assert "ngit pr close" in refused.stdout
    assert "--reason 'Superseded by <id> (tip <sha>)" in refused.stdout


# -- the module the script calls -----------------------------------------------


def config_of(bridge: SimpleNamespace) -> dict:
    """The config the shell route would load, so the module reads the same mapping."""
    import yaml

    return yaml.safe_load(bridge.config.read_text(encoding="utf-8"))


def test_a_short_pr_id_resolves_against_the_store(tmp_path: Path) -> None:
    """`ngit pr list` prints eight characters; the store keys the whole event id."""
    from hermes_nostr_gateway import ngit_merge

    made = make_repo(tmp_path)
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "review"},
                         links=[(PR, "t_a1b2c3d4")])
    config = config_of(bridge)

    verdict = ngit_merge.check(PR[:8], config=config, repo=str(made.repo), home=bridge.home)

    assert verdict.ok, verdict.why
    assert [card.status for card in verdict.cards] == ["review"]


def test_the_same_pr_as_an_nevent_resolves_too(tmp_path: Path) -> None:
    import nostr_sdk as sdk

    from hermes_nostr_gateway import ngit_merge

    made = make_repo(tmp_path)
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "done"},
                         links=[(PR, "t_a1b2c3d4")])
    nevent = sdk.Nip19Event(sdk.EventId.parse(PR)).to_bech32()

    verdict = ngit_merge.check(nevent, config=config_of(bridge), repo=str(made.repo),
                               home=bridge.home)

    assert verdict.ok, verdict.why
    assert [card.task_id for card in verdict.cards] == ["t_a1b2c3d4"]


def test_a_card_the_board_does_not_have_is_refused(tmp_path: Path) -> None:
    from hermes_nostr_gateway import ngit_merge

    made = make_repo(tmp_path)
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "review"})

    verdict = ngit_merge.check(OTHER_PR, ["t_ffffffff"], config=config_of(bridge),
                               repo=str(made.repo), home=bridge.home)

    assert not verdict.ok
    assert not verdict.unreadable
    assert "t_ffffffff" in verdict.why
    assert "not on board gateway" in verdict.why


def test_with_no_board_and_no_link_the_answer_is_unreadable(tmp_path: Path) -> None:
    """A box that cannot tell which board owns the card says so, rather than passing it."""
    from hermes_nostr_gateway import ngit_merge

    verdict = ngit_merge.check(OTHER_PR, ["t_a1b2c3d4"], config={}, repo="")

    assert not verdict.ok
    assert verdict.unreadable
    assert "no board is mapped" in verdict.why


def test_operator_never_releases_a_linked_card(tmp_path: Path) -> None:
    from hermes_nostr_gateway import ngit_merge

    made = make_repo(tmp_path)
    bridge = make_bridge(tmp_path, made.repo, cards={"t_a1b2c3d4": "running"},
                         links=[(PR, "t_a1b2c3d4")])

    verdict = ngit_merge.check(PR, ["operator"], config=config_of(bridge),
                               repo=str(made.repo), home=bridge.home)

    assert not verdict.ok
    assert [card.source for card in verdict.cards] == ["link"]
    assert "t_a1b2c3d4" in verdict.why
