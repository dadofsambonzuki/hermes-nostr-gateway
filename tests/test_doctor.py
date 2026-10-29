"""`hng doctor`: one preflight, every failing check naming its fix, exit 1 when it fails.

Placeholders only: no real community id, repository, board or host name belongs here.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway import cli, doctor
from hermes_nostr_gateway.ngit import NGIT_FLOOR

COMMUNITY = "ab" * 32
CHANNEL_ID = "cd" * 32
PROJECT = "example-project"
BOARD = "example-board"
MAINTAINER = "ef" * 32
IDENTIFIER = "example-repo"
CONVERSATION = f"concord:{COMMUNITY}:{CHANNEL_ID}"

#: A stand-in `ngit` that answers the two calls this repo makes and nothing else.
NGIT_STUB = """#!/bin/sh
if [ "$1" = "--version" ]; then
  echo "ngit {version}"
  exit 0
fi
if [ "$1" = "repo" ]; then
  echo '{{"is_nostr_repo": true, "selected_maintainer": "{maintainer}", "identifier": "{identifier}"}}'
  exit 0
fi
echo "stub: unexpected $@" >&2
exit 2
"""


def make_secret(tmp_path: Path) -> Path:
    from nostr_sdk import Keys

    path = tmp_path / "secret.json"
    path.write_text(json.dumps({"secret_key_hex": Keys.generate().secret_key().to_hex()}))
    return path


def make_checkout(tmp_path: Path) -> Path:
    repo = tmp_path / "example-checkout"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / "AGENTS.md").write_text("# notes\n")
    return repo


def hermes_home(tmp_path: Path, *, registry: bool = True, board: bool = True,
                bound: bool = True, name: str = "hermes-home",
                slug: str = PROJECT) -> Path:
    """A miniature Hermes home: the project registry row and the board it names."""
    home = tmp_path / name
    home.mkdir(parents=True, exist_ok=True)
    if registry and not (home / "projects.db").exists():
        conn = sqlite3.connect(home / "projects.db")
        try:
            conn.execute(
                "create table projects (id text primary key, slug text not null unique,"
                " name text not null, board_slug text, primary_path text,"
                " created_at integer not null, archived integer not null default 0)")
            conn.execute("insert into projects values (?,?,?,?,?,?,0)",
                         ("p_example", slug, "Example Project",
                          BOARD if bound else None, "", 0))
            conn.commit()
        finally:
            conn.close()
    if board:
        where = home / "kanban" / "boards" / BOARD
        where.mkdir(parents=True, exist_ok=True)
        (where / "board.json").write_text(json.dumps({
            "slug": BOARD, "name": "Example Board",
            "project_id": "p_example" if bound else "p_other"}))
    return home


def write_config(tmp_path: Path, home: Path, checkout: Path, *, identity: Path | None = None,
                 projects: list[dict] | None = None, ngit: dict | None = None,
                 store: Path | None = None) -> Path:
    cfg = {
        "identity": {"secret_file": str(identity or make_secret(tmp_path))},
        "store": str(store or tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "relays": {"read": ["wss://relay.example"],
                   "publish": ["wss://relay.example"]},
        "concord": {"projects": projects if projects is not None else [{
            "channel": CHANNEL_ID, "community": COMMUNITY, "path": str(checkout),
            "project": PROJECT, "board": BOARD}]},
        "ngit": ngit if ngit is not None else {"assignee": "default"},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return path


def fake_ngit(tmp_path: Path, monkeypatch, *, version: str = NGIT_FLOOR) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "ngit"
    script.write_text(NGIT_STUB.format(version=version, maintainer=MAINTAINER,
                                       identifier=IDENTIFIER))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir))
    monkeypatch.delenv("HNG_NGIT", raising=False)
    return script


def live_daemon(*, alive: bool = True) -> dict:
    return {
        "type": "status", "workers": [],
        "ngit_watch": {"enabled": True, "alive": alive,
                       "targets": [{"channel": CONVERSATION}],
                       "relays": ["wss://relay.example"]},
    }


def stub_daemon(monkeypatch, reply: dict | None, problem: str = "") -> None:
    def fake(path, *, timeout: float = 3.0):
        return reply, (problem or ("" if reply is not None else "no socket"))

    monkeypatch.setattr(doctor, "probe_daemon", fake)


def stub_cron(monkeypatch, *, covered: bool = True) -> None:
    line = (f"*/10 * * * * PATH=/opt/hng/bin:$PATH hng --config /tmp/config.yaml "
            f"ngit sync --channel {CONVERSATION} --json")
    monkeypatch.setattr(doctor, "cron_sources",
                        lambda: [("crontab", line if covered else
                                  f"# ... ngit sync --channel {CONVERSATION}")])


def run_doctor(monkeypatch, tmp_path: Path, config: Path, home: Path | None = None) -> int:
    monkeypatch.setenv("HERMES_HOME", str(home or (tmp_path / "hermes-home")))
    return cli.main(["--config", str(config), "doctor", "--json"])


def checks_by_name(capsys) -> dict[str, dict]:
    data = json.loads(capsys.readouterr().out)
    return {check["name"]: check for check in data["checks"]}


@pytest.fixture()
def good(tmp_path, monkeypatch):
    """Every check can pass: real key, real checkout, registry, stub daemon, stub cron, ngit."""
    home = hermes_home(tmp_path)
    checkout = make_checkout(tmp_path)
    config = write_config(tmp_path, home, checkout)
    fake_ngit(tmp_path, monkeypatch)
    stub_daemon(monkeypatch, live_daemon())
    stub_cron(monkeypatch)
    return tmp_path, home, checkout, config


def test_a_correct_config_exits_zero(good, monkeypatch, capsys):
    tmp_path, home, checkout, config = good
    assert run_doctor(monkeypatch, tmp_path, config) == 0
    checks = checks_by_name(capsys)
    assert all(check["ok"] for check in checks.values()), checks
    assert checks["ngit"]["detail"].startswith(NGIT_FLOOR)


def test_bad_identity_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, checkout,
                          identity=tmp_path / "missing-secret.json")
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    identity = checks_by_name(capsys)["identity"]
    assert not identity["ok"]
    assert identity["fix"], "a failing check must name its fix"
    assert "secret_file" in identity["fix"]
    assert "missing-secret.json" in identity["detail"]


def test_missing_board_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    home = hermes_home(tmp_path, board=False, name="no-board-home")
    config = write_config(tmp_path, home, checkout)
    assert run_doctor(monkeypatch, tmp_path, config, home) == 1
    board = checks_by_name(capsys)["boards"]
    assert not board["ok"]
    assert "no board" in board["detail"]
    assert "boards create" in board["fix"]


def test_unbound_board_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    home = hermes_home(tmp_path, bound=False, name="unbound-home")
    config = write_config(tmp_path, home, checkout)
    assert run_doctor(monkeypatch, tmp_path, config, home) == 1
    board = checks_by_name(capsys)["boards"]
    assert not board["ok"]
    assert "not bound" in board["detail"]
    assert "bind-board" in board["fix"]


def test_unmapped_entry_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, checkout, projects=[{
        "channel": CHANNEL_ID, "community": COMMUNITY, "path": str(checkout),
        "board": BOARD}])
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    checkouts = checks_by_name(capsys)["checkouts"]
    assert not checkouts["ok"]
    assert "without a project" in checkouts["detail"]
    assert checkouts["fix"]


def test_missing_checkout_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, tmp_path / "not-there")
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    checkouts = checks_by_name(capsys)["checkouts"]
    assert not checkouts["ok"]
    assert "does not exist" in checkouts["detail"]


def test_a_store_path_that_is_a_directory_is_named(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    store = tmp_path / "store-is-a-dir"
    store.mkdir()
    config = write_config(tmp_path, home, checkout, store=store)
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    check = checks_by_name(capsys)["store"]
    assert not check["ok"]
    assert "directory" in check["detail"]
    assert "store:" in check["fix"]


def test_missing_project_is_named_with_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    home = hermes_home(tmp_path, slug="some-other-project", name="no-registry-home")
    config = write_config(tmp_path, home, checkout)
    assert run_doctor(monkeypatch, tmp_path, config, home) == 1
    project = checks_by_name(capsys)["projects"]
    assert not project["ok"]
    assert "no project" in project["detail"]
    assert "project create" in project["fix"]


def test_watcher_alive_matters(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    stub_daemon(monkeypatch, live_daemon(alive=False))
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    watcher = checks_by_name(capsys)["watcher"]
    assert not watcher["ok"]
    assert "no relay subscription" in watcher["detail"]


def test_ngit_off_path_names_the_binary_and_the_floor(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    ngit = checks_by_name(capsys)["ngit"]
    assert not ngit["ok"]
    assert "not found on PATH" in ngit["detail"]
    assert NGIT_FLOOR in ngit["detail"]
    assert "install ngit" in ngit["fix"]


def test_old_ngit_names_the_version_and_the_floor(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    fake_ngit(tmp_path, monkeypatch, version="3.0.2")
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    ngit = checks_by_name(capsys)["ngit"]
    assert "3.0.2" in ngit["detail"]
    assert NGIT_FLOOR in ngit["detail"]


def test_missing_cron_pass_is_named_with_an_example(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    stub_cron(monkeypatch, covered=False)
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    cron = checks_by_name(capsys)["cron"]
    assert not cron["ok"]
    assert CHANNEL_ID in cron["detail"]
    assert "ngit sync --channel" in cron["fix"]


def test_daemon_down_fails_the_socket_and_names_the_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    stub_daemon(monkeypatch, None, "no socket at /tmp/example.sock")
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    checks = checks_by_name(capsys)
    assert not checks["socket"]["ok"]
    assert "serve" in checks["socket"]["fix"]
    assert not checks["watcher"]["ok"]
    assert checks["watcher"]["fix"]


def test_unparsable_config_exits_one_and_says_so(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("identity: [unclosed\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cli.main(["--config", str(path), "doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is False
    check = data["checks"][0]
    assert check["name"] == "config" and not check["ok"] and check["fix"]


def test_missing_config_exits_one_and_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert cli.main(["--config", str(tmp_path / "absent.yaml"), "doctor", "--json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["checks"][0]["name"] == "config"
    assert data["checks"][0]["detail"]


def test_every_failing_check_carries_a_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, checkout,
                          identity=tmp_path / "missing-secret.json")
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    stub_cron(monkeypatch, covered=False)
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    data = json.loads(capsys.readouterr().out)
    failing = [check for check in data["checks"] if not check["ok"]]
    assert failing
    for check in failing:
        assert check["fix"], f"{check['name']} failed without a fix"


