"""The live checkout must be on master, and the box must say when it is not.

The daemon serves `~/Projects/hermes-nostr-gateway`, so the branch that clone was last left on
is the code the box runs. These tests build real clones and move them: the verdict under test is
what git answers about HEAD, not a mock of it.
"""

from __future__ import annotations

import json

import yaml

from git_sandbox import Sandbox, advance_origin, commit, git, make_clone
from hermes_nostr_gateway import checkout, cli, doctor


def write_config(tmp_path) -> str:
    from nostr_sdk import Keys

    secret = Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "missing.sock"),
        "groups": {}, "policies": [], "defaults": {},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def park_on_a_proposal(made: Sandbox, name: str = "pr/blackhole-unauthorised") -> None:
    git(made.repo, "switch", "-q", "-c", name)
    commit(made.repo, "proposal.txt")


# -- the question ------------------------------------------------------------

def test_a_checkout_on_master_is_the_live_checkout(tmp_path) -> None:
    made = make_clone(tmp_path)

    state = checkout.inspect(made.repo)

    assert state.ok, state.describe()
    assert state.branch == "master"
    assert (state.behind, state.ahead) == (0, 0)
    assert state.status_line() == f"checkout: master @ {made.short} (0 behind master)"


def test_being_behind_master_is_reported_but_is_not_a_failure(tmp_path) -> None:
    """A stale master is merged code; the distance is said so an operator can act on it."""
    made = make_clone(tmp_path)
    advance_origin(made, tmp_path)

    state = checkout.inspect(made.repo)

    assert state.ok, state.describe()
    assert state.behind == 1
    assert state.status_line() == f"checkout: master @ {made.short} (1 behind master)"
    assert "1 behind master" in state.describe()


def test_a_proposal_branch_fails_and_names_the_branch_and_commit(tmp_path) -> None:
    made = make_clone(tmp_path)
    park_on_a_proposal(made)

    state = checkout.inspect(made.repo)

    assert not state.ok
    assert state.branch == "pr/blackhole-unauthorised"
    assert "pr/blackhole-unauthorised" in state.describe()
    assert made.short in state.describe()
    assert "git checkout master && git pull" in state.fix()
    assert state.status_line().startswith(
        "checkout: pr/blackhole-unauthorised @ ")


def test_a_detached_checkout_fails(tmp_path) -> None:
    """The case an operator reaches by testing one commit: HEAD is not on any branch."""
    made = make_clone(tmp_path)
    advance_origin(made, tmp_path)
    git(made.repo, "checkout", "-q", "--detach", "origin/master")

    state = checkout.inspect(made.repo)

    assert not state.ok
    assert state.detached
    assert "(detached)" in state.describe()
    assert "git checkout master && git pull" in state.fix()


def test_master_with_local_commits_fails(tmp_path) -> None:
    """Nothing merged: the box would be serving a commit no one else has."""
    made = make_clone(tmp_path)
    commit(made.repo, "local.txt")

    state = checkout.inspect(made.repo)

    assert not state.ok
    assert state.ahead == 1
    assert "master" in state.describe()
    assert "1 commit(s)" in state.describe()
    assert "git branch pr/<slug>" in state.fix()


def test_a_missing_origin_branch_is_not_silence(tmp_path) -> None:
    made = make_clone(tmp_path)
    git(made.repo, "update-ref", "-d", "refs/remotes/origin/master")

    state = checkout.inspect(made.repo)

    assert not state.ok
    assert "no origin/master" in state.describe()
    assert "git fetch origin master" in state.fix()


def test_repo_root_walks_up_from_the_package(tmp_path) -> None:
    made = make_clone(tmp_path)
    inside = made.repo / "src" / "hermes_nostr_gateway"
    inside.mkdir(parents=True)

    assert checkout.repo_root(inside) == made.repo
    assert checkout.repo_root(tmp_path / "site-packages" / "hermes_nostr_gateway") is None


def test_a_card_worktree_is_not_the_live_checkout(tmp_path, monkeypatch) -> None:
    """A card is expected to sit on a proposal branch; doctor must not call that a failure."""
    made = make_clone(tmp_path)
    card = made.repo / ".worktrees" / "t_card"
    git(made.repo, "worktree", "add", "-q", str(card), "-b", "pr/card")

    assert checkout.is_card_workspace(card)
    assert not checkout.is_card_workspace(made.repo)

    elsewhere = tmp_path / "scratch" / "t_marked"
    git(made.repo, "worktree", "add", "-q", str(elsewhere), "-b", "pr/marked")
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACE", str(elsewhere))
    assert checkout.is_card_workspace(elsewhere)


# -- hng status ---------------------------------------------------------------

def test_status_prints_the_branch_and_the_distance(tmp_path, monkeypatch, capsys) -> None:
    made = make_clone(tmp_path)
    advance_origin(made, tmp_path)
    config = write_config(tmp_path)
    monkeypatch.setattr(cli, "inspect_checkout", lambda: checkout.inspect(made.repo))

    assert cli.main(["--config", config, "status"]) == 0

    printed = capsys.readouterr().out.splitlines()
    assert f"checkout: master @ {made.short} (1 behind master)" in printed


def test_status_json_carries_the_checkout(tmp_path, monkeypatch, capsys) -> None:
    made = make_clone(tmp_path)
    park_on_a_proposal(made)
    config = write_config(tmp_path)
    monkeypatch.setattr(cli, "inspect_checkout", lambda: checkout.inspect(made.repo))

    assert cli.main(["--config", config, "status", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["checkout"]["branch"] == "pr/blackhole-unauthorised"
    assert data["checkout"]["ahead"] == 1
    assert data["checkout"]["ok"] is False


# -- hng doctor ---------------------------------------------------------------

def test_doctor_passes_on_master_and_names_the_distance(tmp_path, monkeypatch, capsys) -> None:
    made = make_clone(tmp_path)
    advance_origin(made, tmp_path)
    monkeypatch.setattr(doctor, "live_checkout_path", lambda: made.repo)

    check = doctor._checkout_check()

    assert check.ok, check.detail
    assert "master" in check.detail
    assert "1 behind master" in check.detail
    assert check.fix == ""


def test_doctor_fails_on_a_proposal_branch_with_the_fix(tmp_path, monkeypatch) -> None:
    made = make_clone(tmp_path)
    park_on_a_proposal(made)
    monkeypatch.setattr(doctor, "live_checkout_path", lambda: made.repo)

    check = doctor._checkout_check()

    assert not check.ok
    assert "pr/blackhole-unauthorised" in check.detail
    assert made.short in check.detail
    assert check.fix == f"cd {made.repo} && git checkout master && git pull"


def test_doctor_passes_a_card_workspace(tmp_path, monkeypatch) -> None:
    made = make_clone(tmp_path)
    card = made.repo / ".worktrees" / "t_card"
    git(made.repo, "worktree", "add", "-q", str(card), "-b", "pr/card")
    monkeypatch.setattr(doctor, "live_checkout_path", lambda: card)

    check = doctor._checkout_check()

    assert check.ok
    assert "card workspace" in check.detail


def test_doctor_has_nothing_to_check_without_a_clone(tmp_path, monkeypatch) -> None:
    """A wheel install has no checkout to keep on master; that is not a failed preflight."""
    monkeypatch.setattr(doctor, "live_checkout_path", lambda: None)

    check = doctor._checkout_check()

    assert check.ok
    assert "not running from a git checkout" in check.detail
