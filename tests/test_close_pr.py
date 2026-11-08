"""The close route refuses an empty reason: a duplicate names the proposal that survived.

`scripts/close_pr.sh` is the writer's close. It reads the successor's tip from ngit, builds
`Superseded by <id> (tip <sha>): <why>`, publishes it with `ngit pr close --reason`, and
reads the status back before reporting success. These tests run the real script against a
fake `ngit` that records the reason it was given and can pretend the read-back did not
converge.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
CLOSE = REPO / "scripts" / "close_pr.sh"

DUP = "a" * 64
SUCCESSOR = "b" * 64
TIP = "3bbb86b5" + "0" * 32
WHY = "a re-push published a second proposal for the same branch"

FAKE_NGIT = """#!/bin/sh
state=${FAKE_NGIT_STATE:?}
case "${1:-} ${2:-}" in
  "pr view")
    [ "${4:-}" = "--json" ] || exit 1
    file="$state/${3:-}.json"
    [ -f "$file" ] || exit 1
    if [ "${3:-}" = "${FAKE_DUPLICATE:-}" ] && [ -f "$state/closed" ]; then
      sed 's/"status": "open"/"status": "closed"/' "$file"
    else
      cat "$file"
    fi
    ;;
  "pr list")
    [ -f "$state/list.json" ] || exit 1
    cat "$state/list.json"
    ;;
  "pr close")
    [ "${3:-}" = "${FAKE_DUPLICATE:-}" ] || exit 1
    shift 3
    while [ $# -gt 0 ]; do
      case "$1" in
        --reason) printf '%s' "${2:-}" > "$state/reason"; shift 2 ;;
        *) shift ;;
      esac
    done
    if [ -z "${FAKE_NO_CLOSE_MARK:-}" ]; then printf 'closed' > "$state/closed"; fi
    ;;
  *)
    exit 1
    ;;
esac
"""

#: The dispatcher's markers, scrubbed so the script cannot think it is a card's workspace.
MARKERS = ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")


def make_close(tmp_path: Path) -> SimpleNamespace:
    """A clone carrying the close script, a fake `ngit`, and the state it reads and writes."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy2(CLOSE, repo / "scripts" / "close_pr.sh")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ngit").write_text(FAKE_NGIT, encoding="utf-8")
    (bindir / "ngit").chmod(0o755)

    state = tmp_path / "state"
    state.mkdir()
    return SimpleNamespace(repo=repo, bindir=bindir, state=state)


def close_env(made: SimpleNamespace, **extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in MARKERS}
    env.update({
        "PATH": f"{made.bindir}:{os.environ.get('PATH', '')}",
        "PYTHON": sys.executable,
        "PYTHONPATH": str(REPO / "src"),
        "FAKE_NGIT_STATE": str(made.state),
        "FAKE_DUPLICATE": DUP,
    })
    env.update(extra)
    return env


def close(made: SimpleNamespace, *args: str, **extra: str) -> subprocess.CompletedProcess:
    # The script's shebang is bash and it uses `set -o pipefail`, which dash does not have;
    # run it through the interpreter its shebang names, as merge_pr's tests do.
    return subprocess.run(["bash", str(made.repo / "scripts" / "close_pr.sh"), *args],
                          cwd=made.repo, env=close_env(made, **extra),
                          capture_output=True, text=True, timeout=120)


def proposal(made: SimpleNamespace, ident: str, **fields) -> None:
    (made.state / f"{ident}.json").write_text(
        json.dumps({"id": ident, **fields}), encoding="utf-8")


def reason_of(made: SimpleNamespace) -> str:
    path = made.state / "reason"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def duplicate_and_successor(made: SimpleNamespace, *, successor: dict | None = None) -> None:
    proposal(made, DUP, branch="pr/duplicate(aaaaaaaa)", status="open",
             subject="the stale proposal")
    fields = {"branch": "pr/duplicate(bbbbbbbb)", "status": "open",
              "subject": "the proposal that survived", "ci": {"runs": [{"commit": TIP}]}}
    fields.update(successor or {})
    proposal(made, SUCCESSOR, **fields)


# -- the reason ----------------------------------------------------------------

def test_the_branch_suffix_is_the_only_difference_between_duplicates() -> None:
    from hermes_nostr_gateway.ngit_close import branch_base

    assert branch_base("pr/duplicate(5e1f9dbe)") == "pr/duplicate"
    assert branch_base("pr/duplicate(08baeaaf)") == "pr/duplicate"
    assert branch_base("pr/plain") == "pr/plain"
    assert branch_base("") == ""


def test_the_reason_always_names_the_successor_and_its_tip() -> None:
    from hermes_nostr_gateway.ngit_close import Successor, close_reason

    successor = Successor(id=SUCCESSOR, short="bbbbbbbb", tip=TIP)

    assert close_reason(successor) == f"Superseded by bbbbbbbb (tip {TIP}): {WHY}."
    assert close_reason(successor, "the amended tip moved") == (
        f"Superseded by bbbbbbbb (tip {TIP}): the amended tip moved.")


def test_a_reason_without_a_tip_is_refused() -> None:
    import pytest

    from hermes_nostr_gateway.ngit_close import CloseError, Successor, close_reason

    with pytest.raises(CloseError):
        close_reason(Successor(id=SUCCESSOR, short="bbbbbbbb"))


# -- the close -----------------------------------------------------------------

def test_a_close_names_the_successor_and_its_tip(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)

    closed = close(made, DUP, "--successor", SUCCESSOR)

    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert reason_of(made) == f"Superseded by bbbbbbbb (tip {TIP}): {WHY}."
    assert "read back: closed" in closed.stdout


def test_a_close_carries_the_callers_reason_after_the_pointer(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)

    closed = close(made, DUP, "--successor", SUCCESSOR,
                   "--reason", "the amended tip moved")

    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert reason_of(made) == f"Superseded by bbbbbbbb (tip {TIP}): the amended tip moved."


def test_a_close_without_a_successor_never_publishes(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)

    refused = close(made, DUP)

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "successor" in refused.stderr
    assert reason_of(made) == ""


def test_a_close_refuses_a_successor_that_is_not_open(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made, successor={"status": "applied"})

    refused = close(made, DUP, "--successor", SUCCESSOR)

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "not an open proposal" in refused.stderr
    assert reason_of(made) == ""


def test_a_close_refuses_a_duplicate_that_is_not_open(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)
    proposal(made, DUP, branch="pr/duplicate(aaaaaaaa)", status="applied")

    refused = close(made, DUP, "--successor", SUCCESSOR)

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "not open" in refused.stderr
    assert reason_of(made) == ""


def test_a_successor_with_no_tip_needs_one_passed(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)
    proposal(made, SUCCESSOR, branch="pr/duplicate(bbbbbbbb)", status="open")

    refused = close(made, DUP, "--successor", SUCCESSOR)

    assert refused.returncode == 2, refused.stdout + refused.stderr
    assert "--tip" in refused.stderr
    assert reason_of(made) == ""

    closed = close(made, DUP, "--successor", SUCCESSOR, "--tip", TIP)

    assert closed.returncode == 0, closed.stdout + closed.stderr
    assert reason_of(made) == f"Superseded by bbbbbbbb (tip {TIP}): {WHY}."


def test_a_close_that_does_not_read_back_closed_is_a_failure(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)

    unconfirmed = close(made, DUP, "--successor", SUCCESSOR, FAKE_NO_CLOSE_MARK="1")

    assert unconfirmed.returncode == 3, unconfirmed.stdout + unconfirmed.stderr
    assert "still reads" in unconfirmed.stderr


# -- the merge route's lookup --------------------------------------------------

def test_successors_names_the_other_proposal_on_the_branch(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)
    (made.state / "list.json").write_text(json.dumps([
        {"id": DUP, "branch": "pr/duplicate(aaaaaaaa)", "status": "open"},
        {"id": SUCCESSOR, "branch": "pr/duplicate(bbbbbbbb)", "status": "open"},
    ]), encoding="utf-8")

    found = close(made, "successors", DUP)

    assert found.returncode == 0, found.stdout + found.stderr
    assert "other open proposal on this branch: bbbbbbbb" in found.stdout
    assert TIP in found.stdout
    assert f"ngit pr close {DUP} --reason 'Superseded by bbbbbbbb (tip {TIP}):" in found.stdout


def test_successors_says_so_when_no_other_proposal_shares_the_branch(tmp_path: Path) -> None:
    made = make_close(tmp_path)
    duplicate_and_successor(made)
    (made.state / "list.json").write_text(json.dumps([
        {"id": DUP, "branch": "pr/duplicate(aaaaaaaa)", "status": "open"},
    ]), encoding="utf-8")

    found = close(made, "successors", DUP)

    assert found.returncode == 0, found.stdout + found.stderr
    assert "no other open proposal shares" in found.stdout
    assert "--reason 'Superseded by <id> (tip <sha>)" in found.stdout
