"""The live plugin copy against this build's adapter: the hashes, the install, the warning.

Every test writes both files: nothing here reads this host's plugin directory or asks its
systemd anything. Placeholders only -- no npubs, hostnames, community ids or personal names.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway import adapter_copy, cli

#: The stand-in adapter both copies hold. It must compile: `install` refuses one that does not.
ADAPTER = b'"""A stand-in adapter for the copy tests."""\n\nNAME = "example"\n'

#: A fixed instant, so the backup name is asserted rather than pattern-matched.
NOW = datetime(2026, 11, 8, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "20261108T120000Z"


def make_copies(tmp_path: Path, monkeypatch, *, live_bytes: bytes | None = None,
                started: tuple[float | None, str] = (None, "")) -> tuple[Path, Path]:
    """A repository copy, a live copy, and the two seams that must not be the host's."""
    repo = tmp_path / "checkout" / "integrations" / "hermes-plugin" / "adapter.py"
    repo.parent.mkdir(parents=True)
    repo.write_bytes(ADAPTER)
    live = tmp_path / "hermes-home" / "plugins" / "nostr" / "adapter.py"
    live.parent.mkdir(parents=True)
    live.write_bytes(ADAPTER if live_bytes is None else live_bytes)
    monkeypatch.setattr(adapter_copy, "source_path", lambda: repo)
    monkeypatch.setattr(adapter_copy, "unit_started", lambda *args, **kwargs: started)
    monkeypatch.setenv("NOSTR_PLUGIN_PATH", str(live))
    return repo, live


def write_config(tmp_path: Path, **settings) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(settings))
    return path


def test_equal_copies_are_ok_and_the_verdict_names_both_files(tmp_path, monkeypatch):
    repo, live = make_copies(tmp_path, monkeypatch)

    state = adapter_copy.status({})

    assert state.ok and state.equal
    assert state.repo_hash == state.live_hash == adapter_copy.digest(repo)
    assert str(repo) in state.detail() and str(live) in state.detail()
    assert state.detail() == (f"{live} matches {repo} (sha256 {state.live_hash})")
    assert state.as_dict()["equal"] is True


def test_one_byte_difference_is_not_ok_and_names_both_hashes(tmp_path, monkeypatch):
    repo, live = make_copies(tmp_path, monkeypatch,
                             live_bytes=ADAPTER.replace(b"example", b"examplf"))

    state = adapter_copy.status({})

    assert not state.ok
    assert state.repo_hash == adapter_copy.digest(repo) != adapter_copy.digest(live)
    assert state.live_hash == adapter_copy.digest(live)
    assert f"(sha256 {state.live_hash})" in state.detail()
    assert f"(sha256 {state.repo_hash})" in state.detail()
    assert "differs from" in state.detail()


def test_a_missing_live_copy_is_a_failure_naming_the_path(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch)
    live.unlink()

    state = adapter_copy.status({})

    assert not state.ok
    assert f"no live copy at {live}" in state.detail()
    assert state.live_hash == ""


def test_an_unreadable_live_copy_is_a_problem_and_not_an_exception(tmp_path, monkeypatch):
    """A copy that exists but cannot be read fails the check: `status` never raises."""
    _, live = make_copies(tmp_path, monkeypatch)
    live.chmod(0o000)
    if os.access(live, os.R_OK):  # root opens a 000 file regardless; nothing to prove here
        pytest.skip("this process can read a 000 file, so the read cannot fail")

    state = adapter_copy.status({})

    assert not state.ok
    assert state.live_hash == ""
    assert "the live copy is unreadable" in state.detail()
    assert str(live) in state.detail()
    assert any(line.startswith("problem:") for line in state.lines())


def test_an_unreadable_repository_copy_is_a_problem_and_not_an_exception(tmp_path, monkeypatch):
    repo, _ = make_copies(tmp_path, monkeypatch)
    repo.chmod(0o000)
    if os.access(repo, os.R_OK):
        pytest.skip("this process can read a 000 file, so the read cannot fail")

    state = adapter_copy.status({})

    assert not state.ok
    assert state.repo_hash == ""
    assert "the repository copy is unreadable" in state.detail()
    assert str(repo) in state.detail()


def test_a_read_error_from_either_digest_is_a_problem_not_a_raised_error(tmp_path, monkeypatch):
    """Whatever the host does to the read, `status` answers with a problem and exits a failure."""
    repo, live = make_copies(tmp_path, monkeypatch)

    def refuse(path):
        raise adapter_copy.AdapterError(f"{path}: cannot read it (Permission denied)")

    monkeypatch.setattr(adapter_copy, "digest", refuse)

    state = adapter_copy.status({})

    assert not state.ok
    assert state.repo_hash == state.live_hash == ""
    assert str(repo) in state.detail() and str(live) in state.detail()
    assert state.detail().count("cannot read it") == 2


def test_the_status_verb_reports_an_unreadable_copy_and_exits_one(tmp_path, monkeypatch, capsys):
    _, live = make_copies(tmp_path, monkeypatch)
    config = write_config(tmp_path, socket=str(tmp_path / "gateway.sock"))
    live.chmod(0o000)
    if os.access(live, os.R_OK):
        pytest.skip("this process can read a 000 file, so the read cannot fail")

    assert cli.main(["--config", str(config), "adapter", "status"]) == 1

    out = capsys.readouterr().out
    assert "problem: the live copy is unreadable" in out
    assert "(unreadable)" in out, "the hash line must not call an unreadable copy missing"


def test_a_build_with_no_checkout_is_reported_and_not_a_failure(tmp_path, monkeypatch):
    make_copies(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter_copy, "source_path", lambda: None)

    state = adapter_copy.status({})

    assert state.ok and not state.applicable and not state.equal
    assert "wheel install" in state.detail()


def test_the_status_verb_exits_one_and_prints_both_hashes(tmp_path, monkeypatch, capsys):
    repo, live = make_copies(tmp_path, monkeypatch, live_bytes=ADAPTER + b"# local edit\n")
    config = write_config(tmp_path, socket=str(tmp_path / "gateway.sock"))

    assert cli.main(["--config", str(config), "adapter", "status"]) == 1

    out = capsys.readouterr().out
    assert adapter_copy.digest(repo) in out
    assert adapter_copy.digest(live) in out
    assert "hng adapter install" in out


def test_the_status_verb_exits_zero_when_the_copies_match(tmp_path, monkeypatch, capsys):
    make_copies(tmp_path, monkeypatch)
    config = write_config(tmp_path, socket=str(tmp_path / "gateway.sock"))

    assert cli.main(["--config", str(config), "adapter", "status", "--json"]) == 0

    data = json.loads(capsys.readouterr().out)
    assert data["ok"] is True and data["equal"] is True
    assert data["repo_hash"] == data["live_hash"]
    assert data["repo_mtime"] and data["live_mtime"]


def test_the_live_path_is_the_override_then_the_environment_then_the_config(
        tmp_path, monkeypatch):
    repo, live = make_copies(tmp_path, monkeypatch)
    configured = tmp_path / "configured" / "adapter.py"
    override = tmp_path / "override" / "adapter.py"

    monkeypatch.setenv("NOSTR_PLUGIN_PATH", str(live))
    assert adapter_copy.live_path({"adapter": {"path": str(configured)}}) == live
    assert adapter_copy.live_path({"adapter": {"path": str(configured)}}, override=override) == override

    monkeypatch.delenv("NOSTR_PLUGIN_PATH")
    assert adapter_copy.live_path({"adapter": {"path": str(configured)}}) == configured
    # A bare string under `adapter:` is the same setting spelled shorter.
    assert adapter_copy.live_path({"adapter": str(configured)}) == configured
    assert adapter_copy.source_path() == repo


def test_a_directory_in_the_environment_names_the_file_inside_it(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch)
    monkeypatch.setenv("NOSTR_PLUGIN_PATH", str(live.parent))

    assert adapter_copy.live_path({}) == live


def test_the_default_lives_under_hermes_home_not_this_users_home(tmp_path, monkeypatch):
    """A caller with another `HERMES_HOME` is measured there, never against `$HOME`."""
    monkeypatch.delenv("NOSTR_PLUGIN_PATH", raising=False)
    home = tmp_path / "another-hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert adapter_copy.live_path({}) == home / "plugins" / "nostr" / "adapter.py"


def test_install_backs_the_old_copy_up_and_leaves_the_hashes_equal(tmp_path, monkeypatch):
    repo, live = make_copies(tmp_path, monkeypatch)  # written below, then edited
    live.write_bytes(ADAPTER + b"# an edit only this host has\n")

    result = adapter_copy.install({}, now=NOW)

    backup = live.with_name(f"adapter.py.bak-{STAMP}")
    assert result.action == "installed"
    assert result.backup == backup
    assert backup.is_file()
    assert backup.read_bytes() == ADAPTER + b"# an edit only this host has\n"
    assert live.read_bytes() == repo.read_bytes()
    assert result.before == adapter_copy.digest(backup)
    assert result.after == adapter_copy.digest(repo)
    state = adapter_copy.status({})
    assert state.ok and state.equal


def test_install_is_idempotent_and_leaves_one_backup(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch)
    live.write_bytes(ADAPTER + b"# edited\n")
    adapter_copy.install({}, now=NOW)

    again = adapter_copy.install({}, now=datetime(2026, 11, 8, 12, 0, 1, tzinfo=timezone.utc))

    assert again.action == "current"
    assert again.backup is None
    assert [path.name for path in live.parent.glob("adapter.py.bak-*")] == [f"adapter.py.bak-{STAMP}"]


def test_two_installs_in_the_same_second_do_not_overwrite_a_backup(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch)
    live.write_bytes(ADAPTER + b"# first edit\n")
    adapter_copy.install({}, now=NOW)
    live.write_bytes(ADAPTER + b"# second edit\n")

    second = adapter_copy.install({}, now=NOW)

    assert second.backup == live.with_name(f"adapter.py.bak-{STAMP}-2")
    assert second.backup.read_bytes() == ADAPTER + b"# second edit\n"


def test_install_creates_the_plugins_directory_on_a_fresh_home(tmp_path, monkeypatch):
    repo, _ = make_copies(tmp_path, monkeypatch)
    target = tmp_path / "fresh" / "plugins" / "nostr" / "adapter.py"

    result = adapter_copy.install({}, live=target, now=NOW)

    assert result.action == "installed" and result.backup is None  # nothing to back up
    assert target.read_bytes() == repo.read_bytes()


def test_install_never_runs_a_process(tmp_path, monkeypatch):
    """The restart is the operator's step: `install` must not run `systemctl` (or anything)."""
    _, live = make_copies(tmp_path, monkeypatch)
    live.write_bytes(ADAPTER + b"# edited\n")
    ran: list[tuple] = []
    monkeypatch.setattr(adapter_copy.subprocess, "run", lambda *a, **k: ran.append(a))

    adapter_copy.install({}, now=NOW)

    assert ran == []


def test_install_refuses_a_source_that_does_not_compile(tmp_path, monkeypatch):
    repo, live = make_copies(tmp_path, monkeypatch)
    live.write_bytes(ADAPTER + b"# an edit only this host has\n")
    repo.write_bytes(b"def broken(:\n")

    with pytest.raises(adapter_copy.AdapterError, match="does not compile"):
        adapter_copy.install({}, now=NOW)

    assert live.read_bytes() == ADAPTER + b"# an edit only this host has\n"
    assert not list(live.parent.glob("adapter.py.bak-*"))


def test_install_with_no_repository_copy_says_so(tmp_path, monkeypatch):
    make_copies(tmp_path, monkeypatch)
    monkeypatch.setattr(adapter_copy, "source_path", lambda: None)

    with pytest.raises(adapter_copy.AdapterError, match="wheel install"):
        adapter_copy.install({}, now=NOW)


def test_a_copy_written_after_the_unit_started_is_a_stale_import(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch, started=(1_700_000_000.0, ""))
    os.utime(live, (1_700_000_100, 1_700_000_100))

    state = adapter_copy.status({})

    assert state.ok, "the hashes match: a stale import is evidence, not a failed check"
    assert state.stale is True
    assert "restart" in state.detail()
    assert state.as_dict()["stale_import"] is True


def test_a_copy_written_before_the_unit_started_is_not_stale(tmp_path, monkeypatch):
    _, live = make_copies(tmp_path, monkeypatch, started=(1_700_000_100.0, ""))
    os.utime(live, (1_700_000_000, 1_700_000_000))

    state = adapter_copy.status({})

    assert state.stale is False
    assert "restart" not in state.detail()


def test_an_unreadable_unit_timestamp_is_a_note_not_a_failure(tmp_path, monkeypatch):
    make_copies(tmp_path, monkeypatch,
                started=(None, "hermes-gateway: systemd refused the query (not authorised)"))

    state = adapter_copy.status({})

    assert state.ok, "the files matching is the invariant; the process age is evidence"
    assert state.stale is None
    assert "systemd refused the query" in state.detail()


def _stub_systemctl(tmp_path: Path, monkeypatch, body: str) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    script = bindir / "systemctl"
    script.write_text("#!/bin/sh\n" + body)
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")


def test_the_unit_timestamp_reads_the_unix_form(tmp_path, monkeypatch):
    _stub_systemctl(tmp_path, monkeypatch,
                    'case "$*" in *--timestamp=unix*) echo "ActiveEnterTimestamp=@1700000000";; '
                    '*) echo "ActiveEnterTimestamp=Thu 2023-11-14 22:13:20 UTC";; esac\n')

    assert adapter_copy.unit_started("hermes-gateway") == (1_700_000_000.0, "")


def test_the_unit_timestamp_falls_back_to_the_rendered_form(tmp_path, monkeypatch):
    """systemd older than 246 refuses `--timestamp=unix`; the rendered line still answers."""
    _stub_systemctl(tmp_path, monkeypatch,
                    'case "$*" in *--timestamp=unix*) exit 1;; '
                    '*) echo "ActiveEnterTimestamp=Thu 2023-11-14 22:13:20 UTC";; esac\n')

    started, note = adapter_copy.unit_started("hermes-gateway")

    assert note == "" and started is not None


def test_a_refused_unit_query_is_reported_with_its_reason(tmp_path, monkeypatch):
    _stub_systemctl(tmp_path, monkeypatch,
                    'echo "Failed to connect to bus: Permission denied" >&2; exit 1\n')

    started, note = adapter_copy.unit_started("hermes-gateway")

    assert started is None
    assert "refused the query" in note and "Permission denied" in note


def test_a_stopped_unit_has_no_start_timestamp(tmp_path, monkeypatch):
    _stub_systemctl(tmp_path, monkeypatch, 'echo "ActiveEnterTimestamp="\n')

    started, note = adapter_copy.unit_started("hermes-gateway")

    assert started is None and "not running" in note
