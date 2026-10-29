"""`hng status` tells the truth about relay health.

The old column was relay_stats.last_error: the last stderr line of a `nak` child. A
healthy relay showed `connecting to nos.lol... ok.`, a gated one showed `authenticating
to relay.example.com/chat as …`, and a relay dropped from the config kept its row forever.
That is not a health verdict, and it once sent a person chasing a relay that was fine.

These tests pin the replacement: a per-relay verdict from the running daemon's live
worker state, with the store's rows demoted to history and flagged when the config no
longer mentions the relay.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.health import (
    BAD,
    OK,
    WARN,
    relay_verdicts,
    render,
    reports_event_times,
    running_but_unconfigured,
    stale_rows,
)
from hermes_nostr_gateway.store import Store

NOW = 1_800_000_000
RELAY = "wss://relay.example.com/chat"
OTHER = "wss://nos.lol"
REMOVED = "wss://relay.eu.whitenoise.chat"

#: What a healthy relay's child prints, and what used to be shown as its "error".
CONNECT_OK = "connecting to relay.example.com... ok."
AUTH_NOTICE = "authenticating to relay.example.com/chat as 740fcd907b10beed…"
CONNECT_FAILED = "failed to connect to any of the given relays."


def worker(relay: str = RELAY, **overrides) -> dict:
    """A live worker entry shaped like the daemon's `status` frame."""
    entry = {
        "relay": relay,
        "kinds": [1059, 4],
        "requested_kinds": [1059, 4],
        "refused_kinds": [],
        "events": 12,
        "alive": True,
        "attempts": 1,
        "started_at": NOW - 3600,
        "attempt_started_at": NOW - 3600,
        "connected_at": NOW - 3600,
        "auth_at": None,
        "last_event_at": NOW - 60,
        "last_stderr": CONNECT_OK,
        "last_error": CONNECT_OK,
    }
    entry.update(overrides)
    return entry


def frame(*workers: dict, relays: list[str] | None = None) -> dict:
    return {
        "type": "status",
        "identity": "me",
        "workers": list(workers),
        "relays_read": relays if relays is not None else sorted({w["relay"] for w in workers}),
        "relay_stats": [],
    }


def verdict_for(entry: dict, **kwargs):
    (verdict,) = relay_verdicts(frame(entry), relays=[entry["relay"]], now=NOW, **kwargs)
    return verdict


# -- the verdict ------------------------------------------------------------

def test_a_healthy_relay_is_connected_even_though_its_stderr_says_otherwise():
    verdict = verdict_for(worker(last_error=CONNECT_FAILED, last_stderr=AUTH_NOTICE))

    assert verdict.state == "connected"
    assert verdict.severity == OK
    line = render([verdict])[0]
    assert RELAY in line and "connected" in line
    assert "failed to connect" not in line
    assert "authenticating" not in line


def test_a_fresh_auth_notice_is_authenticating_and_expires():
    fresh = verdict_for(worker(auth_at=NOW - 5, last_event_at=NOW - 600,
                               connected_at=NOW - 600, events=0))
    assert fresh.state == "authenticating" and fresh.severity == WARN
    assert "authenticating" in render([fresh])[0]

    stale = verdict_for(worker(auth_at=NOW - 600, last_event_at=NOW - 600,
                               connected_at=NOW - 600, events=0))
    assert stale.state == "connected", "an old auth notice must not pin a healthy relay"


def test_a_relay_that_refuses_every_kind_is_refusing():
    verdict = verdict_for(worker(kinds=[], refused_kinds=[1059, 4], alive=False,
                                 last_error=CONNECT_FAILED))

    assert verdict.state == "refusing"
    assert verdict.severity == BAD
    assert "nothing left to read" in verdict.detail
    assert "4, 1059" in verdict.detail


def test_a_relay_that_authenticates_and_still_refuses_is_named_not_authenticating():
    """The reported stuck state: `authenticating` for ever, because every retry auths.

    A relay that answers AUTH and closes the filter anyway has decided something about
    this key. Reporting it as a handshake in progress hides the blind spot behind a WARN
    that never expires; it is a refusal and it is BAD.
    """
    verdict = verdict_for(worker(
        events=0, last_event_at=None, connected_at=NOW - 600, alive=False,
        auth_at=NOW - 5,                 # the challenge for the latest attempt
        auth_refusals=4, auth_refused_at=NOW - 5,
        last_error="auth-required after authenticating (4x): requested filter ..."))

    assert verdict.state == "refusing", "a permanent auth refusal read as a handshake"
    assert verdict.severity == BAD
    assert "answered NIP-42" in verdict.detail
    assert "4x" in verdict.detail
    line = render([verdict])[0]
    assert "authenticating" not in line


def test_a_relay_that_delivered_then_refuses_auth_is_not_called_a_refusal():
    """A relay that has delivered is a different fault; its history must not be erased."""
    verdict = verdict_for(worker(events=9, auth_refusals=3, auth_refused_at=NOW - 5,
                                 auth_at=NOW - 5))

    assert verdict.state != "refusing"


def test_a_partial_refusal_stays_connected_and_says_what_is_still_read():
    verdict = verdict_for(worker(kinds=[1059], refused_kinds=[4]))

    assert verdict.state == "connected"
    assert "refuses kind(s) 4" in verdict.detail
    assert "still reading 1059" in verdict.detail


def test_a_connected_relay_with_no_events_is_reported_as_quiet():
    verdict = verdict_for(worker(last_event_at=NOW - 47 * 60, connected_at=NOW - 3 * 3600))

    assert verdict.state == "quiet"
    assert verdict.label == "no events for 47m"
    assert verdict.severity == WARN
    assert "no events for 47m" in render([verdict])[0]


def test_the_quiet_window_is_policy_not_a_constant():
    quiet = verdict_for(worker(last_event_at=NOW - 20 * 60))
    assert quiet.state == "connected", "20m is inside the default 30m window"

    with_flag = verdict_for(worker(last_event_at=NOW - 20 * 60), quiet_minutes=10)
    assert with_flag.state == "quiet"

    disabled = verdict_for(worker(last_event_at=NOW - 40 * 3600), quiet_minutes=0)
    assert disabled.state == "connected", "0 must disable the quiet verdict"


def test_a_dead_child_is_down_not_merely_quiet():
    verdict = verdict_for(worker(alive=False, last_error=CONNECT_FAILED))

    assert verdict.state == "down" and verdict.severity == BAD
    assert "failed to connect" not in render([verdict])[0], (
        "a child's stderr must not become the status column")


def test_a_worker_that_has_not_connected_yet_is_connecting():
    verdict = verdict_for(worker(events=0, last_event_at=None, connected_at=None,
                                 auth_at=None, attempt_started_at=NOW - 5,
                                 started_at=NOW - 5))

    assert verdict.state == "connecting" and verdict.severity == WARN


def test_the_busiest_worker_speaks_for_the_relay():
    quiet_feed = worker(kinds=[1], events=0, last_event_at=None, connected_at=NOW - 3600)
    busy_dm = worker(events=40, last_event_at=NOW - 120)
    (verdict,) = relay_verdicts(frame(quiet_feed, busy_dm), relays=[RELAY], now=NOW)

    assert verdict.state == "connected"
    assert verdict.events == 40


# -- config vs daemon -------------------------------------------------------

def test_a_configured_relay_with_no_worker_is_reported():
    (verdict,) = relay_verdicts(frame(worker(relay=OTHER)), relays=[RELAY], now=NOW)

    assert verdict.state == "no-worker" and verdict.severity == BAD
    assert "no worker" in render([verdict])[0]


def test_a_daemon_reading_a_relay_the_config_dropped_is_flagged():
    assert running_but_unconfigured(frame(worker(relay=REMOVED)), [RELAY]) == [REMOVED]


def test_store_rows_for_relays_no_longer_configured_are_stale_not_health():
    status = {"relay_stats": [{"relay": REMOVED, "events": 11, "last_event": NOW - 3600},
                              {"relay": RELAY, "events": 4, "last_event": NOW - 60}]}

    stale = stale_rows(status, [RELAY])

    assert [row["relay"] for row in stale] == [REMOVED]


def test_no_daemon_means_unknown_never_a_borrowed_verdict():
    (verdict,) = relay_verdicts(None, relays=[RELAY], now=NOW)

    assert verdict.state == "unknown"
    assert verdict.source == "config"
    assert verdict.last_event_at is None
    assert "daemon not running" in verdict.detail


def test_a_daemon_without_per_worker_times_is_declared_as_such():
    old = worker(events=0)
    old.pop("last_event_at")
    old.pop("connected_at")
    assert reports_event_times(frame(old)) is False

    # Still no invented age: the verdict is connected and says why it has no timestamp.
    verdict = verdict_for(old)
    assert verdict.state == "connected"
    assert verdict.last_event_at is None
    assert "no connect times" in verdict.detail
    assert reports_event_times(frame(worker())) is True


# -- the command ------------------------------------------------------------

class StubDaemon(threading.Thread):
    """A one-shot `{"op":"status"}` server on a unix socket, in its own thread."""

    def __init__(self, path, reply: dict):
        super().__init__(daemon=True)
        self.path = str(path)
        self.reply = reply
        self.requests: list[dict] = []
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(1)

    def run(self) -> None:
        conn, _ = self._sock.accept()
        with conn:
            self.requests.append(json.loads(conn.recv(65536).decode().splitlines()[0]))
            conn.sendall((json.dumps(self.reply) + "\n").encode())
        self._sock.close()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.join(timeout=5)


def make_config(tmp_path, relays: list[str], socket_path) -> str:
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": "11" * 32}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": relays, "publish": relays[:1]},
        "store": str(tmp_path / "status.db"),
        "socket": str(socket_path),
        "groups": {}, "policies": [], "defaults": {},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def seed_store(tmp_path, *rows) -> None:
    store = Store(tmp_path / "status.db")
    for relay, kwargs in rows:
        store.note_relay(relay, **kwargs)
    store.close()


def test_status_reports_the_daemons_live_verdicts_and_not_the_stores_stderr(tmp_path, capsys):
    path = make_config(tmp_path, [RELAY, OTHER], tmp_path / "gw.sock")
    # History says the relay's child complained; live state says it is healthy.
    seed_store(tmp_path,
               (RELAY, {"error": CONNECT_FAILED}),
               (REMOVED, {"events": 11}))
    reply = {
        "type": "status", "identity": "me", "relay_stats": [],
        "relays_read": [RELAY, OTHER],
        "workers": [worker(relay=RELAY, events=9, last_event_at=int(time.time()) - 120)],
    }

    with StubDaemon(tmp_path / "gw.sock", reply) as daemon:
        assert cli.main(["--config", path, "status"]) == 0

    out = capsys.readouterr().out
    assert daemon.requests == [{"op": "status"}], "status did not ask the daemon"
    assert "daemon:   up" in out
    assert f"{RELAY}" in out and "connected" in out
    assert f"{OTHER}" in out and "no worker" in out
    assert "err=" not in out, "the stderr column is back"
    assert CONNECT_FAILED not in out, "history was presented as health"

    head, _, tail = out.partition("not in config")
    assert REMOVED in tail and REMOVED not in head, "a removed relay stayed in the table"


def test_status_without_a_daemon_says_so_and_shows_history_only(tmp_path, capsys):
    path = make_config(tmp_path, [RELAY], tmp_path / "gone.sock")
    seed_store(tmp_path, (RELAY, {"events": 7, "error": CONNECT_FAILED}))

    assert cli.main(["--config", path, "status"]) == 0

    out = capsys.readouterr().out
    assert "daemon:   not running" in out
    assert "unknown" in out
    assert "history:" in out and "events=7" in out
    assert CONNECT_FAILED not in out and "err=" not in out


def test_check_exits_non_zero_when_the_daemon_is_unreachable(tmp_path, capsys):
    path = make_config(tmp_path, [RELAY], tmp_path / "gone.sock")

    assert cli.main(["--config", path, "status", "--check"]) == 1
    capsys.readouterr()


def test_check_exits_non_zero_for_a_refusing_relay_but_plain_status_does_not(tmp_path, capsys):
    path = make_config(tmp_path, [RELAY], tmp_path / "gw.sock")
    reply = {"type": "status", "identity": "me", "relay_stats": [],
             "relays_read": [RELAY],
             "workers": [worker(kinds=[], refused_kinds=[1059, 4], alive=False)]}

    with StubDaemon(tmp_path / "gw.sock", reply):
        assert cli.main(["--config", path, "status"]) == 0
    assert "refusing" in capsys.readouterr().out

    with StubDaemon(tmp_path / "gw.sock", reply):
        assert cli.main(["--config", path, "status", "--check"]) == 1
    capsys.readouterr()


def test_status_json_carries_the_verdicts_and_the_stale_rows(tmp_path, capsys):
    path = make_config(tmp_path, [RELAY], tmp_path / "gw.sock")
    seed_store(tmp_path, (REMOVED, {"events": 11}))
    reply = {"type": "status", "identity": "me",
             "relay_stats": [{"relay": REMOVED, "events": 11, "last_event": NOW - 3600}],
             "relays_read": [RELAY], "workers": [worker()]}

    with StubDaemon(tmp_path / "gw.sock", reply):
        assert cli.main(["--config", path, "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["daemon"] is True and payload["event_times"] is True
    assert [row["state"] for row in payload["relays"]] == ["connected"]
    assert [row["relay"] for row in payload["not_in_config"]] == [REMOVED]


def test_peer_inbox_additions_are_not_reported_as_config_drift():
    """A relay discovered from a peer's 10050 is an addition, not a stale config entry."""
    frame = {"workers": [
        {"relay": "wss://relay.example.com/chat", "purpose": "dm"},
        {"relay": "wss://relay.damus.io", "purpose": "peer-inbox"},
        {"relay": "wss://stale.example", "purpose": "dm"},
    ]}
    extra = running_but_unconfigured(frame, {"wss://relay.example.com/chat"})
    assert extra == ["wss://stale.example"]
