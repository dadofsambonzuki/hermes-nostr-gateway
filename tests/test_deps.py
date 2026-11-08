"""`hng deps`: the `deps:` block, the dry run, and the dispatch from the CLI.

The pipeline is exercised against a miniature repository and stub tools: a real git
checkout, a real `git diff`, and a scanner that answers with one finding and then clears it.
That is enough to prove what the command decides and — for the dry run — what it refuses to
write. Placeholders only: no real npub, host or repository belongs in this file.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.deps import (
    DepsConfig,
    DepsConfigError,
    DepsRepo,
    cleared_advisories,
    findings,
    load_deps_config,
    run_deps,
)

ROOT = Path(__file__).resolve().parents[1]
OWNER = "npub1exampleowner"
SIGNER = "npub1examplebot"
COORDINATOR = "npub1examplecoordinator"


def block(**overrides):
    raw = {
        "repositories": [{"name": "example-repo", "owner": OWNER, "branch": "main"}],
        "min_severity": 7,
        "workspace": "/opt/deps-bot",
        "state_dir": "/var/lib/nostr-deps",
        "log": "/var/log/nostr-deps.log",
        "signer": SIGNER,
        "coordinator": COORDINATOR,
        "workflow": ".ngit/act/workflows/dependencies.yml",
        "open_prs": True,
    }
    raw.update(overrides)
    return {"deps": raw}


# -- the `deps:` block --------------------------------------------------------

def test_the_whole_block_parses():
    cfg = load_deps_config(block())
    assert cfg.repositories == (DepsRepo("example-repo", OWNER, "main"),)
    assert cfg.min_severity == 7.0
    assert str(cfg.workspace) == "/opt/deps-bot"
    assert str(cfg.state_dir) == "/var/lib/nostr-deps"
    assert str(cfg.log) == "/var/log/nostr-deps.log"
    assert cfg.signer == SIGNER
    assert cfg.coordinator == COORDINATOR
    assert cfg.workflow == ".ngit/act/workflows/dependencies.yml"
    assert cfg.open_prs is True


def test_defaults_fill_in_and_the_branch_defaults_to_main():
    cfg = load_deps_config({"deps": {"repositories": [{"name": "one", "owner": OWNER}]}})
    assert cfg.repositories == (DepsRepo("one", OWNER, "main"),)
    assert cfg.min_severity == 7.0
    assert str(cfg.workspace) == "/opt/deps-bot"
    assert cfg.open_prs is True
    assert cfg.commit_name == "day.ag deps bot"
    assert cfg.commit_email == "deps-bot@day.ag"


def test_the_owner_can_be_a_bare_owner_and_the_address_is_the_nostr_url():
    repo = DepsRepo("example-repo", OWNER, "master")
    assert repo.address == f"nostr://{OWNER}/example-repo"


@pytest.mark.parametrize("cfg, message", [
    ({}, "no `deps:` block"),
    ({"deps": []}, "deps must be a mapping"),
    ({"deps": {"repositories": []}}, "non-empty list"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "min_severity": "high"}},
     "min_severity must be a number"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "min_severity": 11}},
     "CVSS score"),
    ({"deps": {"repositories": [{"name": "x", "owner": OWNER}], "open_prs": "yes"}},
     "open_prs must be true or false"),
    ({"deps": {"repositories": [{"name": "../escape", "owner": OWNER}]}},
     "plain directory name"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER},
                                {"name": "one", "owner": OWNER}]}}, "must be unique"),
    ({"deps": {"repositories": [{"owner": OWNER}]}}, "name is required"),
    ({"deps": {"repositories": [{"name": "one"}]}}, "owner is required"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER}], "repozitories": []}},
     "unknown key"),
    ({"deps": {"repositories": [{"name": "one", "owner": OWNER, "bracnh": "main"}]}},
     "unknown key"),
])
def test_a_bad_block_is_refused_by_name(cfg, message):
    with pytest.raises(DepsConfigError) as exc:
        load_deps_config(cfg)
    assert message in str(exc.value)


def test_the_example_config_documents_the_block_with_placeholders_only():
    example = yaml.safe_load((ROOT / "config.example.yaml").read_text())
    cfg = load_deps_config(example)
    assert [repo.name for repo in cfg.repositories] == ["example-repo"]
    assert cfg.repositories[0].owner.startswith("<"), cfg.repositories[0].owner
    assert cfg.signer.startswith("<"), cfg.signer
    assert cfg.coordinator.startswith("<"), cfg.coordinator
    assert cfg.open_prs is True
    # No board is named in a tracked file, and none is invented: a card is a deployment's
    # choice, and the example is not one.
    assert cfg.board == "" and not cfg.files_tasks
    assert cfg.review_assignee == "default"


# -- report helpers -----------------------------------------------------------

def report(path: Path, rows):
    import json

    data = {"results": [{"packages": [
        {"package": {"name": name, "version": version},
         "groups": [{"max_severity": score, "ids": [vid]}]}
        for name, version, score, vid in rows]}]}
    path.write_text(json.dumps(data))


def test_findings_respect_the_floor_and_count_distinct_rows(tmp_path):
    path = tmp_path / "report.json"
    report(path, [("left-pad", "1.0.0", 9.0, "GHSA-one"),
                  ("left-pad", "1.0.0", 7.5, "GHSA-two"),
                  ("left-pad", "1.0.0", 4.0, "GHSA-low")])
    assert findings(path, 7.0) == "2"
    assert findings(path, 8.0) == "1"
    assert findings(tmp_path / "missing.json", 7.0) == "?"


def test_cleared_advisories_names_what_the_bump_fixes(tmp_path):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    report(before, [("left-pad", "1.0.0", 9.0, "GHSA-one")])
    report(after, [])
    cleared = cleared_advisories(before, after, 7.0)
    assert cleared == ("GHSA-one (left-pad 1.0.0) "
                       "https://osv.dev/vulnerability/GHSA-one")


# -- the pipeline, against a real checkout and stub tools ---------------------

OSV = """#!/bin/sh
set -eu
case "$1" in
  scan)
    out=""
    while [ $# -gt 0 ]; do
      if [ "$1" = "--output-file" ]; then out="$2"; fi
      shift
    done
    if [ -f .fixed ]; then
      printf '%s' '{"results": []}' > "$out"
    else
      printf '%s' '{"results":[{"packages":[{"package":{"name":"left-pad","version":"1.0.0"},"groups":[{"max_severity":"9.0","ids":["GHSA-test-0001"]}]}]}]}' > "$out"
    fi
    ;;
  fix)
    printf '\\n// bumped\\n' >> package-lock.json
    : > .fixed
    ;;
esac
exit 0
"""

QUIET = "#!/bin/sh\nexit 0\n"


@pytest.fixture()
def checkout(tmp_path):
    """A bare origin, a seed commit, and the bot's clone under its workspace."""
    tools = tmp_path / "tools"
    tools.mkdir()
    for name, body in (("osv-scanner", OSV), ("npm", QUIET), ("npx", QUIET), ("node", QUIET)):
        script = tools / name
        script.write_text(body)
        script.chmod(0o755)

    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    subprocess.run(["git", "init", "--quiet", "--bare", str(origin)], check=True)
    # `git push -o title=…` is how a proposal carries its title; a bare repository has to be
    # told to accept push options (the real `nostr://` remote does).
    subprocess.run(["git", "-C", str(origin), "config", "receive.advertisePushOptions", "true"],
                   check=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(seed)], check=True)
    (seed / "package.json").write_text('{"name":"example","version":"1.0.0"}\n')
    (seed / "package-lock.json").write_text('{"lockfileVersion":3,"packages":{}}\n')
    env = {"GIT_AUTHOR_NAME": "seed", "GIT_AUTHOR_EMAIL": "seed@example",
           "GIT_COMMITTER_NAME": "seed", "GIT_COMMITTER_EMAIL": "seed@example"}
    subprocess.run(["git", "add", "."], cwd=seed, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", "seed"], cwd=seed, check=True, env=env)
    subprocess.run(["git", "remote", "add", "origin", str(origin)], cwd=seed, check=True)
    subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], cwd=origin, check=True)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "clone", "--quiet", str(origin), str(workspace / "example-repo")],
                   check=True)
    return tools, workspace, seed


def config(checkout, tmp_path, **overrides):
    tools, workspace, _seed = checkout
    raw = dict(
        repositories=(DepsRepo("example-repo", OWNER, "main"),),
        workspace=workspace,
        state_dir=tmp_path / "state",
        log=tmp_path / "deps.log",
        signer=SIGNER,
        coordinator=COORDINATOR,
    )
    raw.update(overrides)
    return DepsConfig(**raw)


def head(path: Path) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, check=True,
                          capture_output=True, text=True).stdout.strip()


def branches(path: Path) -> list[str]:
    done = subprocess.run(["git", "branch", "--list", "--format=%(refname:short)"],
                          cwd=path, check=True, capture_output=True, text=True)
    return [line.strip() for line in done.stdout.splitlines() if line.strip()]


def env_for(checkout) -> dict[str, str]:
    tools, _workspace, _seed = checkout
    return {"PATH": f"{tools}:/usr/bin:/bin"}


def test_a_dry_run_says_what_it_would_propose_and_writes_nothing(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    repo = cfg.workspace / "example-repo"
    before = head(repo)

    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, out=out, env=env_for(checkout)) == 0
    text = out.getvalue()

    assert "findings at or above 7: 1" in text
    assert "proposal changes 1 -> 0 findings" in text
    assert "gates passed" in text
    assert "dry run: would push pr/deps-" in text
    assert "GHSA-test-0001" in text
    # No write anywhere: the checkout is back at the origin tip, no proposal branch, no state.
    assert head(repo) == before
    assert branches(repo) == ["main", "work/deps"]
    assert not (tmp_path / "state").exists()


def test_a_dry_run_still_reports_a_proposal_it_would_not_repeat(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    repo = cfg.workspace / "example-repo"
    # The hash of the lockfile the stub scanner produces, recorded as the last proposal.
    import hashlib

    fixed = (repo / "package-lock.json").read_bytes() + b"\n// bumped\n"
    cfg.state_dir.mkdir(parents=True)
    (cfg.state_dir / "last-example-repo").write_text(hashlib.sha256(fixed).hexdigest() + "\n")

    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, out=out, env=env_for(checkout)) == 0
    assert "same proposal as the last run - not repeating it" in out.getvalue()
    assert "would push" not in out.getvalue()


def test_open_prs_false_scans_and_builds_but_publishes_nothing(checkout, tmp_path):
    cfg = config(checkout, tmp_path, open_prs=False)
    repo = cfg.workspace / "example-repo"
    before = head(repo)

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    text = out.getvalue()
    assert "deps.open_prs is false" in text
    assert "ready, not pushed" in text
    assert head(repo) == before
    assert not (tmp_path / "state").exists()


def test_an_unknown_repository_is_refused_by_name(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, only_repo="nope", out=out,
                    env=env_for(checkout)) == 2


def test_a_failed_gate_keeps_its_log_where_the_message_points(checkout, tmp_path):
    """The journal keeps only the pointer, so the pointer has to resolve after the run."""
    tools, _workspace, _seed = checkout
    (tools / "npm").write_text(
        "#!/bin/sh\necho 'npm ERR! ERESOLVE could not resolve' >&2\nexit 1\n")

    cfg = config(checkout, tmp_path)
    repo = cfg.workspace / "example-repo"
    before = head(repo)

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    text = out.getvalue()
    assert "gates FAILED at 'npm ci'" in text

    shown = re.search(r"\(see (.+)\)\s*$", text, re.MULTILINE)
    assert shown, text
    kept = Path(shown.group(1))
    assert kept == cfg.state_dir / "build-example-repo.log"
    assert kept.exists(), f"{kept} was removed with the scratch directory"
    body = kept.read_text()
    assert "ERESOLVE could not resolve" in body
    assert "example-repo" in body

    # The bump is still discarded and the checkout is left where it was found.
    assert head(repo) == before
    assert not [name for name in branches(repo) if name.startswith("pr/deps-")]


def test_a_passing_run_leaves_no_build_log_behind(checkout, tmp_path):
    cfg = config(checkout, tmp_path)
    out = io.StringIO()
    assert run_deps(cfg, dry_run=True, out=out, env=env_for(checkout)) == 0
    assert "gates passed" in out.getvalue()
    assert not (cfg.state_dir / "build-example-repo.log").exists()


# -- the board: one card per proposal, completed by the merge ------------------

BOARD = "gateway"
#: A PR event id, and the short spelling every title and `Merge #…` commit uses.
PR_HEX = "de" * 32
PR_SHORT = PR_HEX[:8]

#: The kernel's own columns, as `tests/test_ngit.py` writes them: a card only proves the
#: board can hold it if the row is written against the real schema.
BOARD_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT,
    status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT,
    created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER,
    workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT,
    branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER,
    tenant TEXT, result TEXT, idempotency_key TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER,
    last_failure_error TEXT, current_run_id INTEGER,
    block_kind TEXT, block_recurrences INTEGER NOT NULL DEFAULT 0,
    model_override TEXT, provider_override TEXT
);
CREATE TABLE task_comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
    author TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, run_id INTEGER,
    kind TEXT NOT NULL, payload TEXT, created_at INTEGER NOT NULL
);
"""

#: `ngit`, as `hng deps` calls it: the queue's list (progress line first, like the real
#: command) and a scan trigger that does nothing. The proposal is listed only once its
#: branch is in the clone -- which is what a push does -- so a run's first look is honest.
NGIT_STUB = """#!/bin/sh
case "$1 $2" in
  "pr list")
    echo "no updates"
    if git for-each-ref --format='%(refname:short)' refs/heads | grep -q '^pr/deps-'; then
      cat "@PRS@" 2>/dev/null || printf '[]'
    else
      printf '[]'
    fi
    ;;
esac
exit 0
"""


def today() -> str:
    return dt.datetime.now(dt.timezone.utc).date().isoformat()


def install_ngit(checkout, prs: Path) -> None:
    tools, _workspace, _seed = checkout
    script = tools / "ngit"
    script.write_text(NGIT_STUB.replace("@PRS@", str(prs)))
    script.chmod(0o755)


def prs_document(branch: str, *, subject: str = "Dependency bumps") -> str:
    """`ngit pr list --json` for the one proposal the bot publishes: branch `(short)`."""
    return json.dumps([{"author": SIGNER, "branch": f"{branch}({PR_SHORT})", "id": PR_HEX,
                        "labels": [], "status": "open", "subject": subject,
                        "target_branch": None}])


@pytest.fixture()
def board(tmp_path, monkeypatch):
    """A miniature Hermes home holding one board, with `HERMES_HOME` pointing at it."""
    home = tmp_path / "hermes-home"
    where = home / "kanban" / "boards" / BOARD
    where.mkdir(parents=True)
    db = sqlite3.connect(where / "kanban.db")
    try:
        db.executescript(BOARD_SCHEMA)
        db.commit()
    finally:
        db.close()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return where / "kanban.db"


def rows(db: Path, sql: str) -> list[tuple]:
    handle = sqlite3.connect(db)
    try:
        return handle.execute(sql).fetchall()
    finally:
        handle.close()


def card_count(db: Path) -> int:
    return int(rows(db, "select count(*) from tasks")[0][0])


def publishing(checkout, tmp_path, **overrides):
    """A run that publishes one proposal, with `ngit` naming it from a fixture file."""
    prs = tmp_path / "prs.json"
    branch = f"pr/deps-{today()}"
    prs.write_text(prs_document(branch))
    install_ngit(checkout, prs)
    return config(checkout, tmp_path, **overrides), prs, branch


def merge_on_origin(checkout, short: str, subject: str) -> str:
    """The commit ngit's apply route leaves on the target: `Merge #<short>: <subject>`.

    Measured against a live kind-1631: its `merge-commit-id` was exactly this commit, which
    is why a run can see a merge in the clone rather than in the relay's status (that lags).
    """
    _tools, _workspace, seed = checkout
    (seed / "merged.txt").write_text("merged\n")
    env = {"GIT_AUTHOR_NAME": "maintainer", "GIT_AUTHOR_EMAIL": "maintainer@example",
           "GIT_COMMITTER_NAME": "maintainer", "GIT_COMMITTER_EMAIL": "maintainer@example"}
    subprocess.run(["git", "add", "."], cwd=seed, check=True)
    subprocess.run(["git", "commit", "--quiet", "-m", f"Merge #{short}: {subject}"],
                   cwd=seed, check=True, env=env)
    subprocess.run(["git", "push", "--quiet", "origin", "main"], cwd=seed, check=True)
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=seed, check=True,
                          capture_output=True, text=True)
    return done.stdout.strip()


def test_the_board_keys_are_read_and_unset_means_no_card():
    cfg = load_deps_config(block())
    assert cfg.board == "" and not cfg.files_tasks
    assert cfg.review_assignee == "default"
    cfg = load_deps_config(block(board="gateway", review_assignee="review"))
    assert cfg.board == "gateway" and cfg.files_tasks
    assert cfg.review_assignee == "review"


@pytest.mark.parametrize("value", ["Gateway", "../ops", "with space", "-lead"])
def test_a_board_that_is_not_a_slug_is_refused_by_name(value):
    with pytest.raises(DepsConfigError) as exc:
        load_deps_config(block(board=value))
    assert "deps.board must be a board slug" in str(exc.value)


def test_a_proposal_files_one_review_task_on_the_board(checkout, board, tmp_path):
    cfg, _prs, branch = publishing(checkout, tmp_path, board=BOARD, review_assignee="review")
    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    assert f"pushed {branch}" in out.getvalue()

    cards = rows(board, "select id, title, body, assignee, status, idempotency_key from tasks")
    assert len(cards) == 1
    task_id, title, body, assignee, status, key = cards[0]
    assert title == f"Review PR {PR_SHORT}: dependency bumps"
    assert (assignee, status) == ("review", "ready")
    assert key == f"deps:pr:{PR_HEX}"
    # The body carries the facts a reviewer cannot get from the column: which PR, in which
    # repository, against which branch, and what the scan measured.
    for fact in (PR_HEX, f"nostr://{OWNER}/example-repo", "Target:     main", "1 -> 0"):
        assert fact in body, fact
    assert "GHSA-test-0001" in body

    # The card is the PR's, so it cannot be completed while the PR is open.
    assert rows(board, "select state, pr_id from ngit_pr_guards") == [("open", PR_HEX)]
    handle = sqlite3.connect(board)
    try:
        with pytest.raises(sqlite3.IntegrityError) as refused:
            handle.execute("update tasks set status = 'done' where id = ?", (task_id,))
    finally:
        handle.close()
    assert "is open" in str(refused.value)

    record = json.loads((cfg.state_dir / "proposal-example-repo.json").read_text())
    assert record["branch"] == branch and record["pr_id"] == PR_HEX
    assert (record["findings_before"], record["findings_after"]) == ("1", "0")
    assert record["task_id"] == task_id


def test_a_second_run_of_the_scan_files_no_second_task(checkout, board, tmp_path):
    cfg, _prs, _branch = publishing(checkout, tmp_path, board=BOARD)
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    # The stub scanner's marker: without it the second run derives the same proposal again,
    # which is the case this pins -- a pending proposal is not published, or filed, twice.
    (cfg.workspace / "example-repo" / ".fixed").unlink()

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    assert "same proposal as the last run - not repeating it" in out.getvalue()
    assert card_count(board) == 1
    assert rows(board, "select count(*) from task_comments") == [(0,)]


def test_the_merge_completes_the_task_and_names_the_commit(checkout, board, tmp_path):
    cfg, _prs, _branch = publishing(checkout, tmp_path, board=BOARD)
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    merge = merge_on_origin(checkout, PR_SHORT, "Dependency bumps")
    (cfg.workspace / "example-repo" / ".fixed").unlink()

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    assert f"merged as {merge}" in out.getvalue()

    assert rows(board, "select status from tasks") == [("done",)]
    assert merge in str(rows(board, "select result from tasks")[0][0])
    comments = rows(board, "select author, body from task_comments order by id")
    assert comments and merge in comments[-1][1]
    assert "closed by the merge" in comments[-1][1]
    # Released, not left refusing: the merge is the proof the card waited for. The row stays
    # (it is the bridge's record of what the card was guarding) but the trigger is gone.
    assert rows(board, "select state from ngit_pr_guards") == [("applied",)]
    assert rows(board, "select count(*) from sqlite_master where type = 'trigger'") == [(0,)]
    record = json.loads((cfg.state_dir / "proposal-example-repo.json").read_text())
    assert record["merged"] == merge

    # Seen again, it is old news: no second comment, no reopened card.
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    assert rows(board, "select status from tasks") == [("done",)]
    assert len(rows(board, "select id from task_comments")) == len(comments)


def test_a_withdrawn_proposal_parks_its_card_for_a_person_once(checkout, board, tmp_path):
    cfg, prs, _branch = publishing(checkout, tmp_path, board=BOARD)
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    prs.write_text("[]")  # the queue no longer lists it, and origin has no merge commit
    (cfg.workspace / "example-repo" / ".fixed").unlink()

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0
    assert "withdrawn without a merge" in out.getvalue()
    assert rows(board, "select status, block_kind from tasks") == [("blocked", "dependency")]
    body = rows(board, "select body from task_comments order by id desc limit 1")[0][0]
    assert "no longer open" in body and "no `Merge #" in body

    # One question, not one per run: a blocked card is already waiting for the person.
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    assert len(rows(board, "select id from task_comments")) == 1


def test_a_run_without_a_board_pushes_the_proposal_and_files_nothing(checkout, tmp_path,
                                                                     monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-home"))
    cfg, _prs, branch = publishing(checkout, tmp_path)
    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0, out.getvalue()
    assert f"pushed {branch}" in out.getvalue()
    assert "review task" not in out.getvalue()
    assert not (tmp_path / "no-home").exists(), "no board, so nothing should have been opened"


def test_the_open_proposal_is_adopted_when_a_run_finds_no_record(checkout, board, tmp_path):
    """A run after a lost state directory still files the card -- with the counts it measures."""
    cfg, _prs, branch = publishing(checkout, tmp_path, board=BOARD)
    assert run_deps(cfg, out=io.StringIO(), env=env_for(checkout)) == 0
    (cfg.state_dir / "proposal-example-repo.json").unlink()
    (cfg.workspace / "example-repo" / ".fixed").unlink()

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0, out.getvalue()
    assert f"adopting the open proposal {branch}" in out.getvalue()
    assert card_count(board) == 1
    assert "1 -> 0 at or above CVSS 7" in rows(board, "select body from tasks")[0][0]
    # The guard is armed on the tip the proposal published, so the refusal names it.
    tip = subprocess.run(["git", "rev-parse", branch], cwd=cfg.workspace / "example-repo",
                         check=True, capture_output=True, text=True).stdout.strip()
    assert rows(board, "select tip from ngit_pr_guards") == [(tip,)]
    record = json.loads((cfg.state_dir / "proposal-example-repo.json").read_text())
    assert record["branch"] == branch and record["pr_id"] == PR_HEX
    assert (record["findings_before"], record["findings_after"]) == ("1", "0")


def test_an_open_proposal_that_is_not_the_bots_is_left_alone(checkout, board, tmp_path):
    """`pr/deps-*` is the bot's namespace, but a branch someone else pushed is not ours."""
    prs = tmp_path / "prs.json"
    branch = f"pr/deps-{today()}"
    prs.write_text(json.dumps([{"author": "npub1someoneelse", "branch": f"{branch}({PR_SHORT})",
                                "id": PR_HEX, "labels": [], "status": "open",
                                "subject": "Dependency bumps", "target_branch": None}]))
    install_ngit(checkout, prs)
    # Publishing off: this is about the reconcile step alone, which runs before the scan.
    cfg = config(checkout, tmp_path, board=BOARD, open_prs=False)
    repo = cfg.workspace / "example-repo"
    subprocess.run(["git", "branch", branch], cwd=repo, check=True)

    out = io.StringIO()
    assert run_deps(cfg, out=out, env=env_for(checkout)) == 0, out.getvalue()
    assert "is not this bot's proposal" in out.getvalue()
    assert card_count(board) == 0


# -- the CLI ------------------------------------------------------------------

def test_hng_deps_run_dry_run_reaches_the_pipeline(monkeypatch, tmp_path):
    import hermes_nostr_gateway.deps as deps_mod

    seen = {}

    def fake_run(cfg, *, dry_run=False, only_repo=None, out=None, env=None):
        seen.update(dry_run=dry_run, only_repo=only_repo, block=cfg)
        return 0

    monkeypatch.setattr(deps_mod, "run_deps", fake_run)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(block()))

    assert cli.main(["--config", str(path), "deps", "run", "--dry-run"]) == 0
    assert seen["dry_run"] is True
    assert seen["only_repo"] is None
    assert seen["block"].signer == SIGNER


def test_hng_deps_dry_run_is_the_same_thing(monkeypatch, tmp_path):
    import hermes_nostr_gateway.deps as deps_mod

    seen = {}
    monkeypatch.setattr(deps_mod, "run_deps",
                        lambda cfg, **kw: seen.update(kw) or 0)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(block()))

    assert cli.main(["--config", str(path), "deps", "dry-run", "--repo", "example-repo"]) == 0
    assert seen["dry_run"] is True
    assert seen["only_repo"] == "example-repo"


def test_a_bad_deps_block_is_a_config_error_not_a_traceback(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"deps": {"repositories": []}}))
    assert cli.main(["--config", str(path), "deps", "run"]) == 2
    assert "deps: deps.repositories must be a non-empty list" in capsys.readouterr().err
