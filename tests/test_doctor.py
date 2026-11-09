"""`hng doctor`: one preflight, every failing check naming its fix, exit 1 when it fails.

Placeholders only: no real community id, repository, board or host name belongs here.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import yaml

from git_sandbox import commit, git, make_clone
from hermes_nostr_gateway import cli, doctor
from hermes_nostr_gateway.ngit import NGIT_FLOOR, NgitError, resolve_target

COMMUNITY = "ab" * 32
CHANNEL_ID = "cd" * 32
PROJECT = "example-project"
BOARD = "example-board"
MAINTAINER = "ef" * 32
IDENTIFIER = "example-repo"
CONVERSATION = f"concord:{COMMUNITY}:{CHANNEL_ID}"

#: The file a fresh install copies, read the way the preflight reads a live config.
EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

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


def write_config(tmp_path: Path, home: Path, checkout: Path, *,
                 identity: Path | dict | None = None,
                 projects: list[dict] | None = None, ngit: dict | None = None,
                 store: Path | None = None,
                 relays: dict | None = None) -> Path:
    ident = identity if isinstance(identity, dict) else {
        "secret_file": str(identity or make_secret(tmp_path))}
    cfg = {
        "identity": ident,
        "store": str(store or tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "relays": relays if relays is not None else {
            "read": ["wss://relay.example"],
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
    # Prepend, do not replace: the stub must win `which ngit`, but `git` and the rest of the
    # host still have to be reachable — the checkout checks shell out to git.
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("HNG_NGIT", raising=False)
    return script


def live_daemon(*, alive: bool = True) -> dict:
    now = int(time.time())
    return {
        "type": "status",
        "workers": [{
            "relay": "wss://relay.example", "purpose": "dm", "kinds": [1059, 4],
            "refused_kinds": [], "events": 3, "alive": True, "auth_refusals": 0,
            "connected_at": now - 600, "last_event_at": now - 60,
        }],
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
    # No unit directory by default: the check must not read this host's /etc, so a test's
    # verdict is decided by the sources the test writes.
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIRS", ())


def stub_units(monkeypatch, tmp_path: Path) -> Path:
    """A systemd unit directory the check reads instead of the host's, plus its path."""
    units = tmp_path / "systemd"
    units.mkdir(exist_ok=True)
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIRS", (units,))
    return units


def want_instance(units: Path, channel: str) -> Path:
    """`systemctl enable` on the template: a symlink in timers.target.wants, named for the id."""
    template = units / "hermes-nostr-gateway-ngit@.timer"
    template.write_text("[Timer]\nOnCalendar=*:0/10\nPersistent=true\n")
    wants = units / "timers.target.wants"
    wants.mkdir(exist_ok=True)
    instance = wants / f"hermes-nostr-gateway-ngit@{channel}.timer"
    instance.symlink_to(template)
    return instance


def run_doctor(monkeypatch, tmp_path: Path, config: Path, home: Path | None = None) -> int:
    monkeypatch.setenv("HERMES_HOME", str(home or (tmp_path / "hermes-home")))
    return cli.main(["--config", str(config), "doctor", "--json"])


def checks_by_name(capsys) -> dict[str, dict]:
    data = json.loads(capsys.readouterr().out)
    return {check["name"]: check for check in data["checks"]}


def live_clone(tmp_path: Path, name: str = "live-clone") -> Path:
    """The checkout doctor reads: a real clone on master, never this host's tree."""
    root = tmp_path / name
    root.mkdir()
    return make_clone(root).repo


def use_live_checkout(monkeypatch, repo: Path | None) -> None:
    """Point the check at `repo`; `run` must not measure the machine it is running on."""
    monkeypatch.setattr(doctor, "live_checkout_path", lambda: repo)


def stub_adapter(monkeypatch, tmp_path: Path, *, equal: bool = True,
                 started: tuple[float | None, str] = (None, "")) -> Path:
    """The live plugin copy a test points the check at, never this host's plugins directory.

    The repository half is the real file this build carries (there is one in every checkout),
    copied to the live path; `equal=False` appends a line so the hashes differ.
    """
    from hermes_nostr_gateway import adapter_copy

    source = adapter_copy.source_path()
    data = source.read_bytes() if source is not None else b'"""stand-in adapter."""\n'
    live = tmp_path / "hermes-home" / "plugins" / "nostr" / "adapter.py"
    live.parent.mkdir(parents=True, exist_ok=True)
    live.write_bytes(data if equal else data + b"\n# an edit only this host has\n")
    monkeypatch.setenv("NOSTR_PLUGIN_PATH", str(live))
    monkeypatch.setattr(adapter_copy, "unit_started", lambda *args, **kwargs: started)
    return live


@pytest.fixture()
def good(tmp_path, monkeypatch):
    """Every check can pass: real key, real checkout, registry, stub daemon, stub cron, ngit."""
    home = hermes_home(tmp_path)
    checkout = make_checkout(tmp_path)
    config = write_config(tmp_path, home, checkout)
    fake_ngit(tmp_path, monkeypatch)
    stub_daemon(monkeypatch, live_daemon())
    stub_cron(monkeypatch)
    use_live_checkout(monkeypatch, live_clone(tmp_path))
    stub_adapter(monkeypatch, tmp_path)
    return tmp_path, home, checkout, config


def test_a_correct_config_exits_zero(good, monkeypatch, capsys):
    tmp_path, home, checkout, config = good
    assert run_doctor(monkeypatch, tmp_path, config) == 0
    checks = checks_by_name(capsys)
    assert all(check["ok"] for check in checks.values()), checks
    assert "config-keys" not in checks, "a config of read keys has nothing to advise"
    assert checks["ngit"]["detail"].startswith(NGIT_FLOOR)
    assert "behind master" in checks["checkout"]["detail"]


def test_unread_config_keys_are_one_advisory_line_and_never_fail_the_run(good, monkeypatch,
                                                                       capsys):
    """Residue is named, with the tolerated key's own reason, and the exit code ignores it."""
    tmp_path, home, checkout, config = good
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    cfg["digest"] = {"interval_minutes": 15, "max_items": 20}
    cfg["transport"] = {"legacy": False}
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    check = checks_by_name(capsys)["config-keys"]
    assert check["ok"] and check["advisory"]
    assert not check["fix"], "an advisory names no fix"
    assert "digest" in check["detail"] and "transport" in check["detail"]
    assert "no transport switch" in check["detail"], "the allowlist keeps its reason"


def test_the_advisory_renders_as_one_note_line(good, monkeypatch, capsys):
    """One line naming both, marked `note`, and the summary counts it separately."""
    tmp_path, home, checkout, config = good
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    cfg["digest"] = {"interval_minutes": 15}
    cfg["transport"] = {"legacy": False}
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(home))
    assert cli.main(["--config", str(config), "doctor"]) == 0
    out = capsys.readouterr().out
    named = [line for line in out.splitlines() if "digest" in line]
    assert len(named) == 1, named
    assert named[0].lstrip().startswith("note")
    assert "transport" in named[0]
    assert "check(s) passed; 1 advisory" in out
    assert "FAIL" not in out


def test_the_shipped_example_config_has_no_unread_keys():
    """What a fresh install copies never produces the advisory."""
    cfg = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    assert doctor.unread_config_keys(cfg) == []


def test_a_key_the_code_reads_is_not_reported_even_absent_from_the_example():
    """Residue is judged against the code, not the example: `socket:` is read but undocumented."""
    assert doctor.unread_config_keys({"socket": "/run/hng.sock", "routing": {}}) == []


def test_a_checkout_parked_on_a_proposal_branch_is_a_failed_check(good, monkeypatch, capsys,
                                                                  tmp_path):
    """The failure that went unnoticed: a clean PR branch served as if it were merged."""
    tmp_path, home, checkout, config = good
    live = live_clone(tmp_path, "proposal-clone")
    git(live, "switch", "-q", "-c", "pr/blackhole-unauthorised")
    commit(live, "proposal.txt")
    use_live_checkout(monkeypatch, live)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    check = checks_by_name(capsys)["checkout"]
    assert not check["ok"]
    assert "pr/blackhole-unauthorised" in check["detail"]
    assert "git checkout master && git pull" in check["fix"]


def test_a_checkout_that_is_not_a_repo_is_not_the_live_checkout(good, monkeypatch, capsys,
                                                                tmp_path):
    """A wheel install has nothing to keep on master; that is not a failed preflight."""
    tmp_path, home, checkout, config = good
    use_live_checkout(monkeypatch, None)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    check = checks_by_name(capsys)["checkout"]
    assert check["ok"]
    assert "not running from a git checkout" in check["detail"]


def test_the_adapter_check_is_named_and_passes(good, monkeypatch, capsys):
    """The half `checkout` cannot see: Hermes loads a copy, so the copy is compared by name."""
    from hermes_nostr_gateway import adapter_copy

    tmp_path, home, checkout, config = good
    live = tmp_path / "hermes-home" / "plugins" / "nostr" / "adapter.py"

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    adapter = checks_by_name(capsys)["adapter"]
    assert adapter["ok"]
    assert str(live) in adapter["detail"]
    assert adapter_copy.digest(live) in adapter["detail"]


def test_a_live_adapter_that_differs_fails_the_run_with_both_hashes(good, monkeypatch, capsys):
    from hermes_nostr_gateway import adapter_copy

    tmp_path, home, checkout, config = good
    live = stub_adapter(monkeypatch, tmp_path, equal=False)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    adapter = checks_by_name(capsys)["adapter"]
    assert not adapter["ok"]
    assert adapter["fix"], "a failing check must name its fix"
    assert "hng adapter install" in adapter["fix"]
    assert "differs from" in adapter["detail"]
    assert adapter_copy.digest(live) in adapter["detail"]
    repo = adapter_copy.source_path()
    if repo is not None:  # a wheel build has no repository half to hash
        assert adapter_copy.digest(repo) in adapter["detail"]


def test_a_stale_import_is_a_warning_on_a_passing_check(good, monkeypatch, capsys):
    """The files matching is the invariant; an older process start is evidence, not a failure."""
    tmp_path, home, checkout, config = good
    live = stub_adapter(monkeypatch, tmp_path, started=(1_700_000_000.0, ""))
    os.utime(live, (1_700_000_100, 1_700_000_100))

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    adapter = checks_by_name(capsys)["adapter"]
    assert adapter["ok"]
    assert "restart" in adapter["detail"]


def test_a_missing_live_adapter_fails_the_run(good, monkeypatch, capsys):
    tmp_path, home, checkout, config = good
    live = stub_adapter(monkeypatch, tmp_path)
    live.unlink()

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    adapter = checks_by_name(capsys)["adapter"]
    assert not adapter["ok"]
    assert "no live copy" in adapter["detail"]
    assert "hng adapter install" in adapter["fix"]


def test_an_unreadable_live_adapter_fails_the_check_without_losing_the_report(
        good, monkeypatch, capsys):
    """`run` never raises for a bad host: an unreadable copy is one failing check, not no report."""
    tmp_path, home, checkout, config = good
    live = stub_adapter(monkeypatch, tmp_path)
    assert run_doctor(monkeypatch, tmp_path, config) == 0
    reported = set(checks_by_name(capsys))
    live.chmod(0o000)
    if os.access(live, os.R_OK):
        pytest.skip("this process can read a 000 file, so the read cannot fail")

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    checks = checks_by_name(capsys)
    assert set(checks) == reported, "the preflight still reports every check"
    adapter = checks["adapter"]
    assert not adapter["ok"]
    assert "cannot read" in adapter["detail"]
    assert str(live) in adapter["detail"]
    assert adapter["fix"], "a failing check must name its fix"


def test_the_adapter_check_is_not_a_failure_without_a_checkout(good, monkeypatch, capsys):
    """A wheel build carries no repository copy: named as not applicable, not failed."""
    from hermes_nostr_gateway import adapter_copy

    tmp_path, home, checkout, config = good
    monkeypatch.setattr(adapter_copy, "source_path", lambda: None)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    adapter = checks_by_name(capsys)["adapter"]
    assert adapter["ok"]
    assert "no repository copy" in adapter["detail"]


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


def test_a_credential_is_checked_through_the_unit_that_loads_it(good, monkeypatch, capsys,
                                                                 tmp_path):
    """A hand-run doctor cannot read a credential; it checks the wiring that can."""
    tmp_path, home, checkout, _ = good
    units = stub_units(monkeypatch, tmp_path)
    source = tmp_path / "identity.json"
    source.write_text('{"secret_key_hex": "' + "11" * 32 + '"}')
    (units / "hermes-nostr-gateway.service").write_text(
        f"LoadCredential=nostr_secret:{source}\n")
    config = write_config(tmp_path, home, checkout,
                          identity={"secret_credential": "nostr_secret"})
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    identity = checks_by_name(capsys)["identity"]
    assert identity["ok"]
    assert "hermes-nostr-gateway.service" in identity["detail"]
    assert str(source) in identity["detail"]


def test_a_credential_no_unit_loads_is_named(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    stub_units(monkeypatch, tmp_path)
    config = write_config(tmp_path, home, checkout,
                          identity={"secret_credential": "nostr_secret"})
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    identity = checks_by_name(capsys)["identity"]
    assert not identity["ok"]
    assert "not loaded by any installed" in identity["detail"]
    assert "LoadCredential=" in identity["fix"]


def test_a_credential_source_that_is_missing_is_named(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, _ = good
    units = stub_units(monkeypatch, tmp_path)
    source = tmp_path / "not-there.json"
    (units / "hermes-nostr-gateway.service").write_text(
        f"LoadCredential=nostr_secret:{source}\n")
    config = write_config(tmp_path, home, checkout,
                          identity={"secret_credential": "nostr_secret"})
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    identity = checks_by_name(capsys)["identity"]
    assert not identity["ok"]
    assert "which is not there" in identity["detail"]
    assert "create" in identity["fix"]


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


def test_missing_pass_is_named_with_an_example(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    stub_cron(monkeypatch, covered=False)
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    schedule = checks_by_name(capsys)["schedule"]
    assert not schedule["ok"]
    assert CHANNEL_ID in schedule["detail"]
    assert "ngit sync --channel" in schedule["fix"]
    assert "systemctl enable --now" in schedule["fix"]


def test_a_crontab_line_counts_as_a_pass_and_is_named(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    assert run_doctor(monkeypatch, tmp_path, config) == 0
    schedule = checks_by_name(capsys)["schedule"]
    assert schedule["ok"]
    assert f"{CHANNEL_ID} (crontab)" in schedule["detail"]


def test_a_systemd_timer_counts_as_a_pass_and_is_named(good, monkeypatch, capsys, tmp_path):
    """The cutover verdict: no crontab line, one enabled timer instance, the channel is a pass."""
    tmp_path, home, checkout, config = good
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    units = stub_units(monkeypatch, tmp_path)
    unit = f"hermes-nostr-gateway-ngit@{CHANNEL_ID}.timer"
    (units / unit).write_text("[Timer]\nOnCalendar=*:0/10\nPersistent=true\n")

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    schedule = checks_by_name(capsys)["schedule"]
    assert schedule["ok"]
    assert f"{CHANNEL_ID} (systemd timer {unit})" in schedule["detail"]
    assert "crontab" not in schedule["detail"]


def test_an_enabled_instance_is_read_through_the_template_symlink(good, monkeypatch, capsys,
                                                                   tmp_path):
    """`systemctl enable` leaves a symlink to the template; its name is the channel."""
    tmp_path, home, checkout, config = good
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    units = stub_units(monkeypatch, tmp_path)
    instance = want_instance(units, CHANNEL_ID)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    schedule = checks_by_name(capsys)["schedule"]
    assert schedule["ok"]
    assert f"systemd timer {instance.name}" in schedule["detail"]


def test_the_template_alone_is_not_a_pass_and_the_fix_names_the_instance(good, monkeypatch,
                                                                         capsys, tmp_path):
    """A template schedules nothing: it has to name the instance an operator must enable."""
    tmp_path, home, checkout, config = good
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    units = stub_units(monkeypatch, tmp_path)
    (units / "hermes-nostr-gateway-ngit@.timer").write_text(
        "[Timer]\nOnCalendar=*:0/10\nPersistent=true\n")

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    schedule = checks_by_name(capsys)["schedule"]
    assert not schedule["ok"]
    assert CHANNEL_ID in schedule["detail"]
    assert (f"systemctl enable --now hermes-nostr-gateway-ngit@{CHANNEL_ID}.timer"
            in schedule["fix"])


def test_a_timer_for_another_channel_does_not_cover_this_one(good, monkeypatch, capsys,
                                                             tmp_path):
    tmp_path, home, checkout, config = good
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    units = stub_units(monkeypatch, tmp_path)
    want_instance(units, "ab" * 32)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    schedule = checks_by_name(capsys)["schedule"]
    assert not schedule["ok"]
    assert CHANNEL_ID in schedule["detail"]


def test_a_timer_is_no_pass_for_an_id_keyed_entry_without_a_community(good, monkeypatch,
                                                                     capsys, tmp_path):
    """The check and the pass must agree: `resolve_target` refuses what a timer names.

    An id-keyed entry with no `community:` cannot address a notice, and a timer instance is
    the id and nothing else, so counting that instance as coverage is green while every pass
    exits non-zero. The failure has to name `community:`, the one thing that fixes it.
    """
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, checkout, projects=[{
        "channel": CHANNEL_ID, "path": str(checkout), "project": PROJECT, "board": BOARD}])
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    want_instance(stub_units(monkeypatch, tmp_path), CHANNEL_ID)

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    schedule = checks_by_name(capsys)["schedule"]
    assert not schedule["ok"]
    assert CHANNEL_ID in schedule["detail"]
    assert "community:" in schedule["fix"]
    # Named, but not addressable: not the same failure as having no pass at all.
    assert "no enabled pass names" not in schedule["detail"]

    # The pass that timer would run, on the config the doctor refused to call covered.
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    with pytest.raises(NgitError, match="no community"):
        resolve_target(cfg, channel=CHANNEL_ID)


def test_a_timer_is_a_pass_once_the_entry_names_its_community(good, monkeypatch, capsys,
                                                              tmp_path):
    """The other side of the same rule, proven through the same call the pass makes."""
    tmp_path, home, checkout, config = good
    monkeypatch.setattr(doctor, "cron_sources", lambda: [])
    want_instance(stub_units(monkeypatch, tmp_path), CHANNEL_ID)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    schedule = checks_by_name(capsys)["schedule"]
    assert schedule["ok"]
    assert f"{CHANNEL_ID} (systemd timer" in schedule["detail"]

    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert resolve_target(cfg, channel=CHANNEL_ID).conversation == CONVERSATION


def test_a_crontab_pass_counts_only_when_it_names_the_whole_conversation(good, monkeypatch,
                                                                        capsys, tmp_path):
    """Cron is the other scheduler, and on the same entry: the id alone cannot address it,
    `concord:<community>:<channel>` can -- which is why a crontab line may still count."""
    tmp_path, home, checkout, _ = good
    config = write_config(tmp_path, home, checkout, projects=[{
        "channel": CHANNEL_ID, "path": str(checkout), "project": PROJECT, "board": BOARD}])
    monkeypatch.setattr(doctor, "SYSTEMD_UNIT_DIRS", ())
    prefix = f"*/10 * * * * cd {checkout} && hng --config {config} ngit sync --channel "

    monkeypatch.setattr(doctor, "cron_sources",
                        lambda: [("crontab", f"{prefix}{CHANNEL_ID} --json")])
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    schedule = checks_by_name(capsys)["schedule"]
    assert not schedule["ok"]
    assert "community:" in schedule["fix"]

    monkeypatch.setattr(doctor, "cron_sources",
                        lambda: [("crontab", f"{prefix}{CONVERSATION} --json")])
    assert run_doctor(monkeypatch, tmp_path, config) == 0
    schedule = checks_by_name(capsys)["schedule"]
    assert schedule["ok"]
    assert f"{CHANNEL_ID} (crontab)" in schedule["detail"]
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert resolve_target(cfg, channel=CONVERSATION).conversation == CONVERSATION


def test_daemon_down_fails_the_socket_and_names_the_fix(good, monkeypatch, capsys, tmp_path):
    tmp_path, home, checkout, config = good
    stub_daemon(monkeypatch, None, "no socket at /tmp/example.sock")
    assert run_doctor(monkeypatch, tmp_path, config) == 1
    checks = checks_by_name(capsys)
    assert not checks["socket"]["ok"]
    assert "serve" in checks["socket"]["fix"]
    assert not checks["watcher"]["ok"]
    assert checks["watcher"]["fix"]
    assert checks["relays"]["ok"], "no daemon is the socket check's failure, not the relays'"
    assert "not checked" in checks["relays"]["detail"]


def test_the_relays_check_counts_refused_apart_from_down(good, monkeypatch, capsys):
    """`8 relays: 4 refused` is one line, and it does not fail the preflight.

    A relay refusing our filters is a fact about the read set; four of them used to read
    as four warnings and a degraded verdict nobody could act on. A relay with no worker
    is still a failure: the gateway claims to read it and is not.
    """
    tmp_path, home, checkout, config = good
    config = write_config(tmp_path, home, checkout, relays={
        "read": ["wss://relay.example", "wss://relay.example/refused"],
        "publish": ["wss://relay.example"],
        "keep": [{"url": "wss://relay.example/archive", "why": "history only"}],
    })
    reply = live_daemon()
    reply["workers"].append({
        "relay": "wss://relay.example/refused", "purpose": "dm", "kinds": [],
        "refused_kinds": [1059, 4], "events": 0, "alive": False, "auth_refusals": 0,
    })
    stub_daemon(monkeypatch, reply)

    assert run_doctor(monkeypatch, tmp_path, config) == 0
    relays = checks_by_name(capsys)["relays"]
    assert relays["ok"]
    assert "2 read relay(s)" in relays["detail"]
    assert "1 connected, 1 refused" in relays["detail"]
    assert "1 kept for another purpose" in relays["detail"]
    assert "move it to `relays.keep`" in relays["detail"]


def test_the_relays_check_fails_when_a_read_relay_has_no_worker(good, monkeypatch, capsys):
    tmp_path, home, checkout, config = good
    config = write_config(tmp_path, home, checkout, relays={
        "read": ["wss://relay.example", "wss://relay.example/gone"],
        "publish": ["wss://relay.example"],
    })
    stub_daemon(monkeypatch, live_daemon())

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    relays = checks_by_name(capsys)["relays"]
    assert not relays["ok"]
    assert "wss://relay.example/gone" in relays["detail"]
    assert relays["fix"]


def test_a_relay_both_read_and_kept_fails_the_preflight(good, monkeypatch, capsys):
    """The read set and the kept set are one decision; naming both is a config error."""
    tmp_path, home, checkout, config = good
    config = write_config(tmp_path, home, checkout, relays={
        "read": ["wss://relay.example"],
        "publish": ["wss://relay.example"],
        "keep": [{"url": "wss://relay.example/", "why": "wants it both ways"}],
    })

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    relays = checks_by_name(capsys)["relays"]
    assert not relays["ok"]
    assert "both read and kept" in relays["detail"]
    assert relays["fix"]


def test_a_socket_path_over_the_kernel_limit_names_the_limit_and_the_fix(
        good, monkeypatch, capsys, tmp_path):
    """Doctor is the first thing an operator runs: "start the daemon" is not the fix
    for a path the kernel refuses every time, so the length is checked before the probe."""
    tmp_path, home, checkout, config = good
    cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
    cfg["socket"] = str(tmp_path / ("d" * 100) / "gateway.sock")
    config.write_text(yaml.safe_dump(cfg), encoding="utf-8")

    assert run_doctor(monkeypatch, tmp_path, config) == 1
    check = checks_by_name(capsys)["socket"]
    assert not check["ok"]
    assert "108" in check["detail"] and "bytes" in check["detail"]
    assert "socket:" in check["fix"] and "XDG_RUNTIME_DIR" in check["fix"]


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


