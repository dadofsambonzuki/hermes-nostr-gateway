"""Acceptance tests from spec §13 that only the assembled daemon can prove.

§13.10 — a crash between insert and ack must replay, never lose: the consumer
reconnects and gets exactly what it did not ack, once, while acked frames stay acked.

§13.8 / §10.4 — a sender over its per-sender bucket degrades to a `blocked` frame
with `reason: rate_limited`. It must not surface as a message (which the adapter
would turn into an agent run) and must not sit in a queue that grows.
"""

from __future__ import annotations

import asyncio
import json
import time

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.crypto import build_rumor, unsigned_event
from hermes_nostr_gateway.gateway import Gateway

RELAY = "wss://acceptance.test"


def config_for(tmp_path, sender_hex: str, *, rate_limit: dict | None = None) -> dict:
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()})
    )
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {"family": [sender_hex]},
        "policies": [
            {"name": "family-dms",
             "match": {"group": "family", "kinds": [14, 1059], "dm": True},
             "reach": "immediate", "authority": "instructions"},
        ],
        "defaults": {"reach": "drop", "authority": "none"},
        # Never attach to a real wn-agent socket from a test.
        "marmot": {"enabled": False},
    }
    if rate_limit is not None:
        cfg["rate_limit"] = rate_limit
    return cfg


def wrap_for(sender_keys: sdk.Keys, recipient_hex: str, text: str, offset: int = 0) -> dict:
    author = sender_keys.public_key().to_hex()
    rumor = build_rumor(author, recipient_hex, text, created_at=int(time.time()) + offset)
    unsigned = unsigned_event(rumor, author)
    return json.loads(
        sdk.nip59_make_gift_wrap(sender_keys, sdk.PublicKey.parse(recipient_hex), unsigned)
        .as_json()
    )


async def subscribe(path):
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"op":"subscribe"}\n')
    await writer.drain()
    frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert frame["type"] == "subscribed"
    return reader, writer


async def read_frame(reader, timeout: float = 2.0) -> dict:
    return json.loads(await asyncio.wait_for(reader.readline(), timeout))


async def request(path, payload: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(payload) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 2))
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


def build_gateway(tmp_path, sender_hex: str, *, rate_limit: dict | None = None) -> Gateway:
    return Gateway(config_for(tmp_path, sender_hex, rate_limit=rate_limit),
                   socket_path=tmp_path / "gateway.sock")


async def test_crash_between_insert_and_ack_replays_from_the_ack_boundary(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    gw = build_gateway(tmp_path, sender.public_key().to_hex())
    await gw.start()
    try:
        reader, writer = await subscribe(gw.socket_path)
        gw._on_event(RELAY, wrap_for(sender, gw.me, "first"))
        gw._on_event(RELAY, wrap_for(sender, gw.me, "second"))
        first = await read_frame(reader)
        second = await read_frame(reader)
        assert (first["type"], first["content"]) == ("message", "first")
        assert (second["type"], second["content"]) == ("message", "second")
        assert first["id"] != second["id"]

        # The consumer durably took the first frame and died before acking the second.
        acked = await request(gw.socket_path, {"op": "ack", "ids": [first["id"]]})
        assert acked["type"] == "ok" and acked["acked"] == [first["id"]]
        writer.close()
        await writer.wait_closed()

        # Reconnect: exactly the unacked message comes back, exactly once.
        reader2, writer2 = await subscribe(gw.socket_path)
        replay = await read_frame(reader2)
        assert (replay["id"], replay["content"]) == (second["id"], "second")
        with pytest.raises(asyncio.TimeoutError):
            await read_frame(reader2, timeout=0.25)
        await request(gw.socket_path, {"op": "ack", "ids": [second["id"]]})
        writer2.close()
        await writer2.wait_closed()

        # Everything acked: a later reconnect replays nothing, so no duplicate.
        reader3, writer3 = await subscribe(gw.socket_path)
        with pytest.raises(asyncio.TimeoutError):
            await read_frame(reader3, timeout=0.25)
        writer3.close()
        await writer3.wait_closed()

        assert gw.store.pending_immediate() == []
    finally:
        await gw.stop()


async def test_flooding_sender_degrades_to_blocked_not_a_message(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    sender_hex = sender.public_key().to_hex()
    gw = build_gateway(tmp_path, sender_hex,
                       rate_limit={"burst": 10, "per_hour": 30})
    await gw.start()
    try:
        reader, writer = await subscribe(gw.socket_path)
        for i in range(11):
            gw._on_event(RELAY, wrap_for(sender, gw.me, f"flood {i}", offset=i))

        frames = [await read_frame(reader) for _ in range(11)]
        messages = [f for f in frames if f["type"] == "message"]
        blocked = [f for f in frames if f["type"] == "blocked"]

        assert len(messages) == 10, frames
        assert len(blocked) == 1, frames
        assert blocked[0]["reason"] == "rate_limited"
        assert blocked[0]["from"] == sender_hex
        assert blocked[0]["rumor_id"] not in {m["id"] for m in messages}
        assert "content" not in blocked[0], "a blocked frame must not carry chat text"

        # Stored for the record (§13.3), never queued for delivery or replay.
        rows = gw.store.recent(20)
        assert len(rows) == 11
        assert sum(r["blocked_reason"] == "rate_limited" for r in rows) == 1
        assert len(gw.store.pending_immediate()) == 10
        assert gw.store.stats()["blocked"] == 1

        # A refusal is now first-class: counted in `status`, listed by `drops`, aged by
        # the TTL. Before this, a blocked row was only visible by querying sqlite.
        assert (await gw.status())["drops"] == {
            "total": 1, "live": 1, "expired": 0, "ttl_hours": 168.0}
        listed = gw.drops()
        assert listed["total"] == 1
        assert listed["drops"][0]["reason"] == "rate_limited"
        assert listed["drops"][0]["sender"] == sender_hex

        writer.close()
        await writer.wait_closed()
    finally:
        await gw.stop()


async def test_retry_pass_expires_a_wrap_past_the_ttl_without_republishing(
        tmp_path, monkeypatch):
    """Spec's outbox: failures retry, but a dead relay's backlog must age out."""
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    sender = sdk.Keys.generate()
    cfg = config_for(tmp_path, sender.public_key().to_hex())
    cfg["retry"] = {"ttl_hours": 24}
    gw = Gateway(cfg, socket_path=tmp_path / "gateway.sock")
    relay = "wss://down.example"
    now = int(time.time())
    stale = {"id": "e" * 64, "kind": 1059}
    fresh = {"id": "f" * 64, "kind": 1059}

    class RecordingPublisher:
        """Stands in for the transport: what matters is which events get republished."""

        def __init__(self):
            self.published: list[str] = []

        async def publish(self, event, relays):
            self.published.append(event["id"])
            return {url: (True, None) for url in relays}

        async def aclose(self):
            return None

    gw.publisher = RecordingPublisher()
    try:
        gw.store.record_wrap(stale, "rumor-1", "aa" * 32, [relay], now=now - 25 * 3600)
        gw.store.mark_wrap(stale["id"], relay, ok=False, error="connection refused")
        gw.store.record_wrap(fresh, "rumor-2", "aa" * 32, [relay], now=now)
        gw.store.mark_wrap(fresh["id"], relay, ok=False, error="503")

        assert gw.retry_ttl_seconds == 24 * 3600
        assert await gw.retry_failed() == 1

        assert gw.publisher.published == [fresh["id"]], (
            "an expired wrap must not be republished")
        rows = {row["id"]: row for row in gw.store.db.execute("SELECT * FROM outbound_wraps")}
        assert rows[stale["id"]]["status"] == "expired"
        assert rows[fresh["id"]]["status"] == "sent"
        assert gw.store.wrap_counts() == {"expired": 1, "sent": 1}
    finally:
        gw.store.close()


def test_a_retry_ttl_must_be_positive(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    cfg = config_for(tmp_path, sdk.Keys.generate().public_key().to_hex())
    cfg["retry"] = {"ttl_hours": 0}
    try:
        Gateway(cfg, socket_path=tmp_path / "gateway.sock")
    except ValueError as exc:
        assert "retry.ttl_hours" in str(exc)
    else:
        raise AssertionError("a zero retry TTL would expire every wrap at birth")
