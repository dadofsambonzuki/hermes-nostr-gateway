"""`health` is the overnight cron's one call.

`status` answers "is each relay working", which is not the question at 04:00: the cron
wants to say what arrived on which plane, what is stuck, what was blocked, and whether
anything is wrong. Assembling that from `status` text or the journal is how a quiet
plane and a broken one come to read the same, so `health` is a frame of its own — per
plane last event, pending, blocked, degraded — and these tests pin the frame, the store
queries behind it, and the socket op that serves it.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time

import pytest_asyncio
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.health import (
    BAD,
    OK,
    WARN,
    Verdict,
    health_report,
    plane_health,
    render_health,
)
from hermes_nostr_gateway.server import SocketServer
from hermes_nostr_gateway.store import Inbound, Store

NOW = 1_800_000_000
RELAY = "wss://relay.example.com/chat"
OTHER = "wss://nos.lol"
SENDER = "ab" * 32


def row(plane: str, **overrides) -> dict:
    """One `store.plane_report()` row."""
    entry = {"plane": plane, "events": 0, "pending": 0, "blocked": 0, "sent": 0,
             "last_event_at": None, "last_received_at": None, "oldest_pending_at": None}
    entry.update(overrides)
    return entry


def verdict(relay: str = RELAY, severity: str = OK, label: str = "connected", **overrides):
    entry = {"relay": relay, "state": label, "label": label, "severity": severity}
    entry.update(overrides)
    return Verdict(**entry)


# -- per plane ---------------------------------------------------------------


def test_a_plane_reports_its_last_event_pending_and_blocks():
    [health] = plane_health(
        [row("nip17", events=9, pending=2, blocked=3, sent=1,
             last_event_at=NOW - 120, oldest_pending_at=NOW - 30)],
        planes=["nip17"], now=NOW,
    )
    assert (health.events, health.pending, health.blocked, health.sent) == (9, 2, 3, 1)
    assert health.last_event_at == NOW - 120
    assert health.age_s == 120, "the age is the last event's, not the row's"
    assert health.degraded is False


def test_a_plane_with_no_traffic_is_reported_with_zeros_not_left_out():
    planes = plane_health([row("nip17", events=1)], planes=["nip17", "nip04"], now=NOW)
    assert [health.plane for health in planes] == ["nip17", "nip04"]
    idle = planes[1]
    assert (idle.events, idle.pending, idle.blocked) == (0, 0, 0)
    assert idle.last_event_at is None and idle.age_s is None
    assert idle.degraded is False, "a plane nobody has written to is not broken"


def test_blocked_messages_are_reported_but_never_degrade_the_plane():
    [health] = plane_health([row("nip17", events=40, blocked=3370)], planes=["nip17"], now=NOW)
    assert health.blocked == 3370, "a policy block count must still be visible"
    assert health.degraded is False, "blocking strangers is the policy working"
    assert health.reasons == ()


def test_a_stuck_backlog_degrades_the_plane_and_a_fresh_one_does_not():
    fresh = plane_health([row("nip17", pending=3, oldest_pending_at=NOW - 30)],
                         planes=["nip17"], now=NOW)[0]
    assert fresh.degraded is False, "the adapter acks in milliseconds; 30s is in flight"

    stuck = plane_health([row("nip17", pending=3, oldest_pending_at=NOW - 3 * 3600)],
                         planes=["nip17"], now=NOW)[0]
    assert stuck.degraded is True
    assert stuck.reasons == ("3 message(s) pending for 3h",)
    assert stuck.oldest_pending_s == 3 * 3600


def test_expired_wraps_degrade_but_wraps_still_being_retried_do_not():
    retrying = plane_health([row("nip17")], planes=["nip17"], now=NOW,
                            wraps={"nip17": {"sent": 4, "failed": 2}})[0]
    assert retrying.degraded is False, "a failed wrap is retried, not lost"

    expired = plane_health([row("nip17")], planes=["nip17"], now=NOW,
                           wraps={"nip17": {"sent": 4, "expired": 2}})[0]
    assert expired.degraded is True
    assert "2 outbound wrap(s) expired un-sent" == expired.reasons[0]
    assert expired.wraps["expired"] == 2


def test_no_delivering_relay_is_one_reason_on_every_plane():
    report = health_report(
        planes=["nip17", "nip04"],
        plane_rows=[row("nip17", events=3, last_event_at=NOW - 60)],
        verdicts=[verdict(RELAY, BAD, "down"), verdict(OTHER, BAD, "down")],
        now=NOW,
    )
    assert report["verdict"] == "degraded"
    assert report["degraded"] is True
    for name in ("nip17", "nip04"):
        assert "no read relay is delivering" in report["planes"][name]["reasons"]
    assert f"{RELAY}: down" in report["reasons"]
    assert report["relays"]["unhealthy"] == [RELAY, OTHER]


# -- the frame ---------------------------------------------------------------


def test_health_without_a_daemon_is_down_and_says_why():
    report = health_report(
        planes=["nip17"],
        plane_rows=[row("nip17", events=5, last_event_at=NOW - 600)],
        daemon=False, daemon_error="[Errno 2] no such file or directory", now=NOW,
    )
    assert report["verdict"] == "down"
    assert report["daemon"] is False and report["degraded"] is True
    assert report["reasons"] == ["daemon not running: [Errno 2] no such file or directory"]
    # The counters are still there, from the store, and the age is the store's.
    assert report["planes"]["nip17"]["events"] == 5
    assert report["planes"]["nip17"]["age_s"] == 600


def test_a_clean_gateway_is_ok_and_its_summary_is_built_from_the_frame():
    report = health_report(
        planes=["nip17", "nip04"],
        plane_rows=[row("nip17", events=4, pending=1, blocked=2, last_event_at=NOW - 180),
                    row("nip04", events=1, last_event_at=NOW - 86400)],
        verdicts=[verdict()],
        uptime_s=3600, now=NOW,
    )
    assert report["verdict"] == "ok" and report["degraded"] is False
    assert report["reasons"] == []
    assert report["summary"] == (
        "gateway ok, up 60m, 2 plane(s), nip17 last event 3m ago, "
        "1 pending, 2 blocked, 1/1 relay(s) ok"
    )
    assert report["counts"] == {"events": 5, "pending": 1, "blocked": 2, "sent": 0,
                                "wraps": {}}
    assert report["uptime_s"] == 3600


def test_a_warning_relay_is_counted_without_degrading_the_gateway():
    report = health_report(planes=["nip17"], verdicts=[verdict(), verdict(OTHER, WARN, "quiet")],
                           now=NOW)
    assert report["verdict"] == "ok"
    assert report["relays"]["warn"] == 1 and report["relays"]["bad"] == 0
    assert report["relays"]["warnings"] == [f"{OTHER}: quiet"]


def test_render_health_shows_each_plane_and_the_reasons():
    report = health_report(
        planes=["nip17", "nip01"],
        plane_rows=[row("nip17", events=2, pending=4, oldest_pending_at=NOW - 7200,
                        last_event_at=NOW - 120)],
        wraps={"nip17": {"expired": 1}},
        now=NOW,
    )
    lines = render_health(report)
    assert lines[0].startswith("gateway degraded")
    assert "nip17: 4 message(s) pending for 2h (+1 more)" in lines[1]
    assert "nip17" in lines[2] and "degraded" in lines[2]
    assert "nip01" in lines[3] and "no events" in lines[3]
    assert any(line.strip().startswith("! nip17: 1 outbound wrap") for line in lines)
    assert report["verdict"] == "degraded"


# -- the store behind it -----------------------------------------------------


def add(store: Store, mid: str, *, plane: str = "nip17", at: int = NOW,
        blocked: str | None = None, authority: str = "none", reach: str = "immediate") -> None:
    store.add(Inbound(
        id=mid, wrapper_id=f"w-{mid}", sender=SENDER, kind=14, created_at=at,
        content=f"text {mid}", conversation=SENDER, reply_to=None, plane=plane,
        reach=reach, authority=authority, rule="r", why="w", blocked_reason=blocked,
    ), now=at)


def test_the_store_counts_pending_blocked_and_last_event_per_plane(tmp_path):
    with Store(tmp_path / "planes.db", identity="me") as store:
        add(store, "pending", at=NOW - 50)
        add(store, "later", at=NOW - 10)
        add(store, "spam", at=NOW - 20, blocked="not_allowlisted")
        add(store, "group", at=NOW - 30, plane="marmot")
        store.mark_delivered(["later", "group"])

        rows = {entry["plane"]: entry for entry in store.plane_report()}
    assert rows["nip17"]["events"] == 3
    assert rows["nip17"]["pending"] == 1, "only the undelivered, unblocked row waits"
    assert rows["nip17"]["blocked"] == 1
    assert rows["nip17"]["last_event_at"] == NOW - 10
    assert rows["nip17"]["oldest_pending_at"] == NOW - 50
    assert rows["marmot"]["pending"] == 0 and rows["marmot"]["events"] == 1


def test_wraps_are_counted_by_plane_through_their_rumor(tmp_path):
    with Store(tmp_path / "wraps.db", identity="me") as store:
        add(store, "lost", at=NOW - 100)
        add(store, "kept", at=NOW - 100)
        add(store, "sent", at=NOW - 100, plane="marmot")
        store.record_wrap({"id": "lost"}, "lost", SENDER, ["wss://r"], now=NOW - 100)
        store.mark_wrap("lost", "wss://r", ok=False, error="relay said no")
        store.record_wrap({"id": "kept"}, "kept", SENDER, ["wss://r"], now=NOW - 5)
        store.record_wrap({"id": "sent"}, "sent", SENDER, ["wss://r"], now=NOW - 100)
        store.mark_wrap("sent", "wss://r", ok=True)

        assert store.expire_wraps(60, now=NOW) == 1, "only the old failed wrap expires"
        report = store.wrap_report_by_plane()

    assert report["nip17"] == {"expired": 1, "pending": 1}, \
        "an expired wrap is no longer a failed one: the TTL overrides the last status"
    assert report["marmot"] == {"sent": 1}


# -- the socket op -----------------------------------------------------------


async def call(path, *frames):
    reader, writer = await asyncio.open_unix_connection(str(path))
    out = []
    for frame in frames:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        out.append(json.loads(await asyncio.wait_for(reader.readline(), 2)))
    writer.close()
    await writer.wait_closed()
    return out


class HealthHandler:
    def __init__(self, reply: dict):
        self.reply = reply
        self.calls: list[dict] = []

    async def health(self, *, quiet_minutes=None, pending_stale_minutes=None):
        self.calls.append({"quiet_minutes": quiet_minutes,
                           "pending_stale_minutes": pending_stale_minutes})
        return dict(self.reply)


class OldDaemon:
    """A daemon from before this op existed: it has no `health` attribute."""

    async def status(self):
        return {"identity": "me"}


@pytest_asyncio.fixture
async def health_server(tmp_path):
    handler = HealthHandler({"type": "health", "verdict": "ok", "planes": {}})
    srv = SocketServer(tmp_path / "h.sock", handler)
    await srv.start()
    yield srv, handler
    await srv.stop()


async def test_the_health_op_answers_in_one_request(health_server):
    srv, handler = health_server
    replies = await call(srv.path, {"op": "health", "quiet_minutes": 5,
                                    "pending_stale_minutes": 2})
    assert replies[0]["type"] == "health" and replies[0]["verdict"] == "ok"
    assert handler.calls == [{"quiet_minutes": 5, "pending_stale_minutes": 2}]


async def test_a_daemon_without_the_health_op_says_so_by_code(tmp_path):
    srv = SocketServer(tmp_path / "old.sock", OldDaemon())
    await srv.start()
    try:
        replies = await call(srv.path, {"op": "health"})
    finally:
        await srv.stop()
    assert replies[0]["type"] == "error"
    assert replies[0]["code"] == "unsupported", "the cron must not read this as an empty frame"


# -- the assembled gateway ---------------------------------------------------


def gateway_config(tmp_path, *, marmot: bool = False) -> dict:
    import nostr_sdk as sdk

    (tmp_path / "secret.json").write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    return {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": marmot},
    }


async def test_the_gateways_health_reports_every_attached_plane(tmp_path):
    from hermes_nostr_gateway.gateway import Gateway

    gateway = Gateway(gateway_config(tmp_path), socket_path=tmp_path / "gateway.sock")
    add(gateway.store, "hello", at=int(time.time()) - 60)

    frame = await gateway.health()

    assert frame["type"] == "health"
    assert frame["daemon"] is True
    assert list(frame["planes"]) == ["nip17", "nip04", "nip01", "concord"]
    assert frame["planes"]["nip17"]["pending"] == 1
    assert frame["planes"]["nip01"]["events"] == 0
    assert any("marmot plane not attached" in note for note in frame["notes"])
    assert frame["verdict"] in ("ok", "degraded")
    assert frame["uptime_s"] is None, "a Gateway that never started has no uptime to claim"


# -- the command -------------------------------------------------------------


class StubDaemon(threading.Thread):
    """A one-shot socket server in its own thread, so `hng health` can be run as-is."""

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


def config_file(tmp_path, socket_path, *, relays: list[str] | None = None) -> str:
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": "11" * 32}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": relays if relays is not None else [RELAY], "publish": [RELAY]},
        "store": str(tmp_path / "cli.db"),
        "socket": str(socket_path),
        "groups": {}, "policies": [], "defaults": {},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path)


def test_health_asks_the_daemon_once_and_prints_its_frame(tmp_path, capsys):
    path = config_file(tmp_path, tmp_path / "gw.sock")
    reply = health_report(planes=["nip17"], plane_rows=[row("nip17", events=2)],
                          uptime_s=120, now=NOW)
    with StubDaemon(tmp_path / "gw.sock", reply) as daemon:
        assert cli.main(["--config", path, "health", "--json"]) == 0

    out = capsys.readouterr().out
    assert daemon.requests == [{"op": "health", "quiet_minutes": 30,
                               "pending_stale_minutes": 10}], "health was not one call"
    frame = json.loads(out)
    assert frame["type"] == "health" and frame["daemon"] is True
    assert frame["planes"]["nip17"]["events"] == 2


def test_health_check_exits_non_zero_when_degraded(tmp_path):
    path = config_file(tmp_path, tmp_path / "gw.sock")
    reply = health_report(planes=["nip17"],
                          plane_rows=[row("nip17", pending=2,
                                          oldest_pending_at=int(time.time()) - 4 * 3600)])
    with StubDaemon(tmp_path / "gw.sock", reply):
        assert cli.main(["--config", path, "health", "--check"]) == 1

    healthy = health_report(planes=["nip17"])
    with StubDaemon(tmp_path / "gw.sock", healthy):
        assert cli.main(["--config", path, "health", "--check"]) == 0


def test_health_without_a_daemon_answers_from_the_store_and_says_so(tmp_path, capsys):
    path = config_file(tmp_path, tmp_path / "gone.sock")

    assert cli.main(["--config", path, "health"]) == 0
    out = capsys.readouterr().out
    assert "down" in out
    assert "daemon not running" in out
    assert "history, not live state" in out
    assert "nip17" in out

    assert cli.main(["--config", path, "health", "--check"]) == 1
