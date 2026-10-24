"""The Marmot plane end to end: wn-agent in, one socket frame out, one send out.

The unit tests in `test_marmot.py` prove the wire and the normalisation; this file
proves the thing the queue's acceptance criterion asks for — a Mamot group read
through the gateway with the same frames every other plane uses, and wn-agent
untouched (the fake stands in for it, so nothing here can mutate the real one).
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.gateway import Gateway
from test_marmot import ACCOUNT, GROUP, POLICY, FakeWnAgent, wait_until


def config_for(tmp_path, *, marmot: dict | None = None) -> dict:
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": POLICY["groups"],
        "policies": POLICY["policies"],
        "defaults": POLICY["defaults"],
    }
    if marmot is not None:
        cfg["marmot"] = marmot
    return cfg


async def call(path, frame: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 3))
    finally:
        writer.close()
        await writer.wait_closed()


async def subscribe(path) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    writer.write(b'{"op":"subscribe"}\n')
    await writer.drain()
    ack = json.loads(await asyncio.wait_for(reader.readline(), 3))
    assert ack["type"] == "subscribed"
    return reader, writer


async def next_message(reader: asyncio.StreamReader) -> dict:
    while True:
        frame = json.loads(await asyncio.wait_for(reader.readline(), 3))
        if frame.get("type") == "message":
            return frame


@pytest.fixture
def no_nak(monkeypatch):
    # Contact-list discovery shells out to nak; this file tests the plane, not the
    # feed bootstrap, and a missing binary must not fail it.
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


async def test_a_marmot_message_reaches_a_socket_subscriber(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(agent.path),
                                                   "account_id_hex": ACCOUNT}))
    try:
        await gateway.start()
        reader, writer = await subscribe(gateway.socket_path)
        await wait_until(lambda: gateway.marmot_worker.connected)
        await agent.push(text="through the gateway", reply_to="77" * 32)

        frame = await next_message(reader)
        assert frame["plane"] == "marmot"
        assert frame["conversation"] == GROUP
        assert frame["content"] == "through the gateway"
        assert frame["reply_to"] == "77" * 32
        # Family rule matched on a Marmot message, so it carries instructions.
        assert (frame["reach"], frame["authority"]) == ("immediate", "instructions")

        status = await gateway.status()
        assert "marmot" in status["planes"]
        assert status["marmot"]["worker"]["connected"] is True
        assert status["marmot"]["worker"]["events"] == 1
        writer.close()
        await writer.wait_closed()
    finally:
        await gateway.stop()
        await agent.stop()


async def test_send_op_routes_a_marmot_target_through_wn_agent(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(agent.path),
                                                   "account_id_hex": ACCOUNT}))
    try:
        await gateway.start()
        frame = await call(gateway.socket_path,
                           {"op": "send", "to": f"marmot:{GROUP}", "text": "on my way",
                            "id": "c9"})
        assert frame["ok"] is True and frame["id"] == "c9"
        assert frame["plane"] == "marmot" and frame["conversation"] == GROUP
        sent = [r for r in agent.requests if r.get("type") == "send_final"]
        assert len(sent) == 1 and sent[0]["text"] == "on my way"
        # The correlation id doubles as the idempotency key, so an adapter retry
        # cannot post the same message twice.
        assert sent[0]["idempotency_key"] == "c9"
        # The group is proven before the send, once.
        assert len([r for r in agent.requests if r.get("type") == "group_info"]) == 1

        history = await call(gateway.socket_path, {"op": "history", "limit": 5})
        assert history["messages"][0]["plane"] == "marmot"
        assert history["messages"][0]["authority"] == "self"
        assert history["messages"][0]["content"] == "on my way"
    finally:
        await gateway.stop()
        await agent.stop()


async def test_a_bare_group_id_is_also_a_marmot_target(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(agent.path),
                                                   "account_id_hex": ACCOUNT}))
    try:
        await gateway.start()
        frame = await call(gateway.socket_path,
                           {"op": "send", "to": GROUP, "text": "bare id"})
        assert frame["ok"] is True and frame["conversation"] == GROUP
    finally:
        await gateway.stop()
        await agent.stop()


async def test_group_and_timeline_ops_read_through_the_plane(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    agent.timeline_rows = [{
        "message_id_hex": "88" * 32,
        "sender": {"account_id_hex": ACCOUNT, "is_self": True},
        "direction": "sent", "kind": 9, "recorded_at": 1_790_000_500,
        "text": "earlier message", "reply_to_message_id_hex": None,
    }]
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(agent.path),
                                                   "account_id_hex": ACCOUNT}))
    try:
        await gateway.start()
        group = await call(gateway.socket_path, {"op": "marmot.group", "group": GROUP})
        assert group["type"] == "marmot_group"
        assert group["member_count"] == 2 and group["is_direct"] is True

        page = await call(gateway.socket_path,
                          {"op": "marmot.timeline", "group": GROUP, "limit": 5})
        assert page["type"] == "marmot_timeline"
        assert page["messages"][0]["content"] == "earlier message"
        assert page["messages"][0]["is_self"] is True
    finally:
        await gateway.stop()
        await agent.stop()


async def test_an_unknown_group_is_a_readable_error_not_a_silent_send(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    agent.group_info_fail = True
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(agent.path),
                                                   "account_id_hex": ACCOUNT}))
    try:
        await gateway.start()
        frame = await call(gateway.socket_path,
                           {"op": "send", "to": f"marmot:{GROUP}", "text": "into nothing"})
        assert frame["ok"] is False and "group_not_found" in frame["error"]
        assert not [r for r in agent.requests if r.get("type") == "send_final"]
    finally:
        await gateway.stop()
        await agent.stop()


async def test_without_a_socket_the_plane_is_disabled_and_says_so(tmp_path, no_nak):
    gateway = Gateway(config_for(
        tmp_path, marmot={"socket": str(tmp_path / "absent.sock"),
                          "home": str(tmp_path / "nowhere"), "account_id_hex": ACCOUNT}))
    try:
        status = await gateway.status()
        assert "marmot" not in status["planes"]
        assert status["marmot"]["enabled"] is False
        frame = await gateway.send(to=f"marmot:{GROUP}", text="x", cid="c1")
        assert frame["ok"] is False and "not enabled" in frame["error"]
    finally:
        await gateway.stop()


async def test_a_configured_but_unusable_plane_is_reported_not_hidden(tmp_path, no_nak):
    # Socket file present, but no wn-agent account to attribute requests to: the
    # plane must disable itself *loudly* and leave the other planes alone.
    socket = tmp_path / "wn-agent.sock"
    socket.write_text("not really a socket")
    gateway = Gateway(config_for(tmp_path, marmot={"socket": str(socket),
                                                   "home": str(tmp_path / "empty-home")}))
    try:
        assert gateway.marmot is None
        status = await gateway.status()
        assert "no wn-agent account" in status["marmot"]["error"]
        assert "nip17" in status["planes"]
    finally:
        await gateway.stop()
