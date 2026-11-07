"""A recovered turn's role grant is re-asked of the daemon, never remembered.

Hermes stores a turn's source with `role_authorized` set from the daemon's verdict at
arrival, then refuses to let that flag vouch for itself: on recovery it asks the connector
again, with adapter delegation switched off. So the connector has to be able to answer, and
the answer has to come from *live* policy and *live* channel membership — a narrowed rule,
a removed trust entry or a channel this daemon no longer watches must revoke a grant that
was good when the message arrived.

These tests pin that answer (`Gateway.authorize`) and the socket op that carries it, which
is what a Hermes `RuntimeStoreError('permission_denied')` on a channel reply traces back to.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.server import SocketServer

SENDER = "ab" * 32
COMMUNITY = "cd" * 32
CHANNEL_ID = "ef" * 32
CHANNEL = f"concord:{COMMUNITY}:{CHANNEL_ID}"

DM_RULES = [
    {"name": "any-dms", "match": {"group": "any", "kinds": [14], "dm": True},
     "reach": "immediate", "authority": "instructions"},
]
CHANNEL_RULES = [
    {"name": "any-channel", "match": {"group": "any", "kinds": [9], "mention": True},
     "reach": "immediate", "authority": "instructions"},
]


def gateway_config(tmp_path, *, policies=None) -> dict:
    import nostr_sdk as sdk

    (tmp_path / "secret.json").write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    return {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": policies or [], "defaults": {},
        "marmot": {"enabled": False},
    }


def build(tmp_path, *, policies=None) -> Gateway:
    return Gateway(gateway_config(tmp_path, policies=policies),
                   socket_path=tmp_path / "gateway.sock")


def watched(gateway: Gateway) -> None:
    """Point `CHANNEL` at a stream this daemon serves, without minting a community."""
    stream = SimpleNamespace(chat_id=CHANNEL, community_id=COMMUNITY, name="ops",
                             label="#ops")
    gateway.concord.channel_for_conversation = lambda conversation: (object(), stream)


def test_a_dm_from_a_sender_the_rules_still_trust_is_authorised(tmp_path):
    reply = build(tmp_path, policies=DM_RULES).authorize(pubkey=SENDER, conversation=SENDER)

    assert reply["type"] == "authorized"
    assert reply["authorized"] is True
    assert reply["authority"] == "instructions"


def test_a_dm_from_a_sender_no_rule_trusts_is_refused(tmp_path):
    """The fresh answer is the point: an empty policy is a no even for a known peer."""
    reply = build(tmp_path).authorize(pubkey=SENDER, conversation=SENDER)

    assert reply["authorized"] is False
    assert reply["authority"] == "none"


def test_a_narrowed_rule_revokes_a_grant_that_used_to_hold(tmp_path):
    """Same sender, same conversation, a rule that no longer covers their kind."""
    trust_the_old_kind = build(tmp_path, policies=[
        {"name": "only-kind-4", "match": {"group": "any", "kinds": [4], "dm": True},
         "reach": "immediate", "authority": "instructions"}])
    assert trust_the_old_kind.authorize(
        pubkey=SENDER, conversation=SENDER, kind=4)["authorized"] is True
    assert trust_the_old_kind.authorize(
        pubkey=SENDER, conversation=SENDER, kind=14)["authorized"] is False


def test_a_watched_channel_is_answered_by_the_rules_that_govern_it(tmp_path):
    gateway = build(tmp_path, policies=CHANNEL_RULES)
    watched(gateway)

    reply = gateway.authorize(pubkey=SENDER, conversation=CHANNEL)

    assert reply["authorized"] is True
    assert reply["why"] == "addressed in #ops", "the answer has to name the room it judged"


def test_a_channel_this_daemon_no_longer_watches_is_refused(tmp_path):
    gateway = build(tmp_path, policies=CHANNEL_RULES)
    gateway.concord.channel_for_conversation = lambda conversation: None

    reply = gateway.authorize(pubkey=SENDER, conversation=CHANNEL)

    assert reply["authorized"] is False
    assert "not a channel this daemon watches" in reply["why"]


def test_a_blank_pubkey_is_never_authorised(tmp_path):
    reply = build(tmp_path, policies=DM_RULES).authorize(pubkey="", conversation=SENDER)

    assert reply["authorized"] is False
    assert reply["why"] == "no pubkey"


async def call(path, *frames) -> list[dict]:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        for frame in frames:
            writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        return [json.loads(await asyncio.wait_for(reader.readline(), 5)) for _ in frames]
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


async def test_the_socket_serves_authorize_with_the_requested_kind(tmp_path):
    seen: list[dict] = []

    class Handler:
        def authorize(self, *, pubkey, conversation, kind=None):
            seen.append({"pubkey": pubkey, "conversation": conversation, "kind": kind})
            return {"type": "authorized", "authorized": True, "authority": "instructions",
                    "why": "dm"}

    server = SocketServer(tmp_path / "s.sock", Handler())
    await server.start()
    try:
        (reply,) = await call(server.path, {"op": "authorize", "pubkey": SENDER,
                                            "conversation": SENDER, "kind": 14})
    finally:
        await server.stop()

    assert reply["type"] == "authorized" and reply["authorized"] is True
    assert seen == [{"pubkey": SENDER, "conversation": SENDER, "kind": 14}]


async def test_a_daemon_without_authorize_says_so_by_code(tmp_path):
    """An older daemon must not read as "denied", which would be a wrong diagnosis."""

    class OldHandler:
        def peers(self):  # the op existed; authorize did not
            return {"peers": []}

    server = SocketServer(tmp_path / "old.sock", OldHandler())
    await server.start()
    try:
        (reply,) = await call(server.path, {"op": "authorize", "pubkey": SENDER,
                                            "conversation": SENDER})
    finally:
        await server.stop()

    assert reply["type"] == "error" and reply["code"] == "unsupported"
