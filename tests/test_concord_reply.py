"""Concord replies go back to the channel they came from.

The read path stores every channel message under `concord:<community>:<channel>`, so a
reply needs no guessing: the conversation names its own destination. These tests hold the
rule the queue item asks for — a reply is built with the identity the send path already
holds, published as a channel wrap, readable in that channel and nowhere else, while a peer
conversation still takes the DM route unchanged.
"""

from __future__ import annotations

import hashlib
import json
import os
import time

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import cli, crypto, server
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord import write
from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.store import Inbound, Store
from test_cli import make_config
from test_concord_channels import policy_for

OPS = "11" * 32
GENERAL = "22" * 32
RELAYS = ["wss://relay.example", "wss://relay2.example"]


def make_invite(owner_pubkey: str) -> tuple[c.Invite, list[c.Channel]]:
    """A community with a private channel (granted key) and a public one (root-derived)."""
    salt, root = os.urandom(32), os.urandom(32)
    ops_key = os.urandom(32).hex()
    community_id = hashlib.sha256(
        b"concord/community" + bytes.fromhex(owner_pubkey) + salt).digest()
    invite = c.Invite(bundle={
        "owner": owner_pubkey, "owner_salt": salt.hex(), "community_id": community_id.hex(),
        "community_root": root.hex(), "root_epoch": 0, "control_pk": os.urandom(32).hex(),
        "name": "Test Community", "relays": list(RELAYS),
        "channels": [{"id": OPS, "key": ops_key, "epoch": 0, "name": "ops"},
                     {"id": GENERAL, "epoch": 0, "name": "general"}],
    }, inviter=owner_pubkey, relay="", wrap_id="w")
    return invite, [c.Channel(id=OPS, name="ops", private=True, key=ops_key, epoch=0),
                    c.Channel(id=GENERAL, name="general", private=False, epoch=0)]


def captured_publish(monkeypatch, *, outcome: str = "ok") -> dict:
    """Stand in for the relay write: record what would go out, answer as a relay would."""
    sent: dict = {"events": []}

    async def fake_publish(event, relays):
        sent["events"].append(event)
        sent["relays"] = list(relays)
        return {relay: outcome for relay in relays}

    monkeypatch.setattr(write, "publish", fake_publish)
    return sent


def gateway_for(tmp_path, identity: sdk.Keys) -> Gateway:
    secret_file = tmp_path / "identity.json"
    secret_file.write_text(json.dumps({"secret_key_hex": identity.secret_key().to_hex()}))
    return Gateway({
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": False, "hermes_platform": False},
    }, socket_path=tmp_path / "gateway.sock")


@pytest.fixture
def no_follows(monkeypatch):
    # Contact-list discovery shells out; these tests are the send path, not the bootstrap.
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


def chat_for(invite: c.Invite, channel_id: str) -> str:
    return ch.chat_id(invite.community_id.hex(), channel_id)


# ── the resolver ────────────────────────────────────────────────────────────

def test_a_conversation_names_its_invite_and_channel(tmp_path):
    owner = sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex())

    with Store(tmp_path / "resolve.db") as store:
        plane = ConcordPlane(store)
        assert plane.channel_for_conversation(chat_for(invite, GENERAL)) is None

        plane.set_streams(invite, channels, {owner.public_key().to_hex()})
        found = plane.channel_for_conversation(chat_for(invite, GENERAL))
        assert found is not None
        got_invite, general = found
        assert got_invite is invite
        assert (general.channel_id, general.name, general.key) == (GENERAL, "general", None)

        _, ops = plane.channel_for_conversation("  " + chat_for(invite, OPS).upper())
        assert ops.channel_id == OPS
        assert ops.private is True and ops.key == invite.bundle["channels"][0]["key"]

        # A channel we never learned, an empty string and a peer id are all "not a channel".
        assert plane.channel_for_conversation(chat_for(invite, "33" * 32)) is None
        assert plane.channel_for_conversation("") is None
        assert plane.channel_for_conversation("ab" * 32) is None


def test_the_prefix_is_what_marks_a_channel_target():
    assert ch.is_conversation("concord:" + "ab" * 32 + ":" + GENERAL) is True
    assert ch.is_conversation("  CONCORD:AB:" + GENERAL + " ") is True
    assert ch.is_conversation("c4f5e7a75a8ce3683d529cff06368439c529e5243c6b125ba68789198856cac7") is False
    assert ch.is_conversation("npub1ws8umyrmzzlwmw2nptk03n0g0vn5ah28qm40ecgg4f8mk3wd9c5suds6x6") is False
    assert ch.is_conversation("") is False


def test_a_bare_channel_stream_defaults_to_no_grant():
    # `ChannelStream` grew its grant rather than a second lookup; a stream built by hand
    # still exists, so the default is the public case.
    stream = ch.ChannelStream(community_id="ab" * 32, channel_id=GENERAL, name="general",
                             private=False, epoch=0, address="cd" * 32,
                             chat_id=ch.chat_id("ab" * 32, GENERAL))
    assert stream.key is None


# ── the send path ───────────────────────────────────────────────────────────

async def test_a_reply_is_published_into_the_channel_it_came_from(tmp_path, monkeypatch,
                                                                 no_follows):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex())
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    try:
        gateway.concord.set_streams(invite, channels, {owner.public_key().to_hex()})
        _, general = gateway.concord.channel_for_conversation(chat_for(invite, GENERAL))

        frame = await gateway.send(to=general.chat_id, text="on it")

        assert frame["ok"] is True, frame
        assert frame["conversation"] == general.chat_id
        assert frame["relays"] == {"channel": {relay: True for relay in RELAYS}}
        assert sent["relays"] == RELAYS, "a channel reply goes to the community's relays"

        wrap = sent["events"][0]
        # A channel wrap is authored by the channel's stream address, not by an ephemeral
        # DM key, and its inner author is us.
        assert wrap["pubkey"] == general.address
        opened = c.open_wrap(ch.stream_key(invite, general), wrap)
        assert opened["content"] == "on it"
        assert opened["pubkey"] == me.public_key().to_hex()
        assert ["channel", GENERAL] in opened["tags"]

        # Readable in the right channel and nowhere else: the other channel's key fails,
        # and the DM plane has no wrap to unwrap.
        _, ops = gateway.concord.channel_for_conversation(chat_for(invite, OPS))
        with pytest.raises(crypto.CryptoError):
            c.open_wrap(ch.stream_key(invite, ops, ops.key), wrap)
        assert gateway.plane.handle(RELAYS[0], wrap) is None
    finally:
        gateway.store.close()


async def test_a_reply_is_readable_back_through_the_read_path(tmp_path, monkeypatch,
                                                             no_follows):
    """The reply opens with the key a channel's readers use, not just with its own.

    `verify_envelope` only proves a wrap is self-consistent; this reads the published wrap
    the way the daemon does — a fresh plane, a fresh store, `handle_channel` — so the check
    is that the room can read the answer, not that the builder agrees with itself.
    """
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    owner_hex = owner.public_key().to_hex()
    invite, channels = make_invite(owner_hex)
    # No grant carries an epoch: the live shape of a community whose channels come from
    # Control rather than from the invite bundle.
    ungranted = [c.Channel(id=OPS, name="ops", private=True,
                           key=invite.bundle["channels"][0]["key"]),
                 c.Channel(id=GENERAL, name="general", private=False)]
    assert all(channel.epoch is None for channel in ungranted)
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    try:
        gateway.concord.set_streams(invite, ungranted, {owner_hex})
        frame = await gateway.send(to=chat_for(invite, GENERAL), text="read me back")
        assert frame["ok"] is True, frame

        wrap = sent["events"][0]
        # Read it as a freshly started daemon would: its own plane, its own store, no row
        # from the send in the way.
        with Store(tmp_path / "reader.db") as reader_store:
            reader = ConcordPlane(reader_store)
            reader.set_streams(invite, ungranted, {owner_hex})
            read_back = reader.handle_channel(
                RELAYS[0], wrap, policy_for(owner_hex),
                me_hex=owner_hex, me_npub=owner.public_key().to_bech32())
            assert read_back is not None, "the reply did not decrypt with the read key"
            assert read_back.content == "read me back"
            assert read_back.conversation == chat_for(invite, GENERAL)
            assert read_back.wrapper_id == wrap["id"]
            assert read_back.sender == me.public_key().to_hex()
    finally:
        gateway.store.close()


async def test_a_private_channel_reply_uses_its_grant(tmp_path, monkeypatch, no_follows):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex())
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    try:
        gateway.concord.set_streams(invite, channels, {owner.public_key().to_hex()})
        _, ops = gateway.concord.channel_for_conversation(chat_for(invite, OPS))

        frame = await gateway.send(to=ops.chat_id, text="ops work goes here")
        assert frame["ok"] is True, frame

        wrap = sent["events"][0]
        assert wrap["pubkey"] == ops.address
        opened = c.open_wrap(ch.stream_key(invite, ops, ops.key), wrap)
        assert opened["content"] == "ops work goes here"
        # The root-derived key would be the wrong key for this channel: the grant is what
        # makes a private channel readable, and the reply must use the same one.
        with pytest.raises(crypto.CryptoError):
            c.open_wrap(ch.stream_key(invite, ops, None), wrap)
    finally:
        gateway.store.close()


async def test_an_in_thread_reply_quotes_the_wrapper_it_answers(tmp_path, monkeypatch,
                                                               no_follows):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    owner_hex = owner.public_key().to_hex()
    invite, channels = make_invite(owner_hex)
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    chat = chat_for(invite, GENERAL)
    now = int(time.time())
    try:
        gateway.concord.set_streams(invite, channels, {owner_hex})
        _, general = gateway.concord.channel_for_conversation(chat)
        # What the read path stored: the inner rumor id the agent was handed, and the wrap
        # id the room actually references.
        gateway.store.add(Inbound(
            id="ab" * 32, wrapper_id="cd" * 32, sender=owner_hex, kind=9, created_at=now,
            content="status?", conversation=chat, reply_to=None, plane="concord",
            reach="immediate", authority="instructions", rule="family", why="mention",
        ))

        await gateway.send(to=chat, text="done", reply_to="ab" * 32)
        opened = c.open_wrap(ch.stream_key(invite, general), sent["events"][-1])
        quotes = [tag for tag in opened["tags"] if tag[0] == "q"]
        assert quotes == [["q", "cd" * 32, "", owner_hex]]

        # An id this store never saw goes out as given, with no author hint: the wrapper id
        # is the part that matters, and a reply to something we did not read cannot name
        # its author.
        await gateway.send(to=chat, text="and again", reply_to="ef" * 32)
        opened = c.open_wrap(ch.stream_key(invite, general), sent["events"][-1])
        assert [tag for tag in opened["tags"] if tag[0] == "q"] == [["q", "ef" * 32, "", ""]]

        await gateway.send(to=chat, text="no quote here")
        opened = c.open_wrap(ch.stream_key(invite, general), sent["events"][-1])
        assert [tag for tag in opened["tags"] if tag[0] == "q"] == []
    finally:
        gateway.store.close()


async def test_the_sent_row_is_ours_and_not_pending(tmp_path, monkeypatch, no_follows):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex())
    captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    chat = chat_for(invite, GENERAL)
    now = int(time.time())
    try:
        gateway.concord.set_streams(invite, channels, {owner.public_key().to_hex()})
        frame = await gateway.send(to=chat, text="recorded", reply_to="ef" * 32)

        row = gateway.store.rumor(frame["message_id"])
        assert row is not None
        assert row["conversation"] == chat
        assert row["plane"] == "concord"
        assert row["authority"] == "self" and row["reach"] == "sent"
        assert row["sender"] == me.public_key().to_hex()
        assert row["wrapper_id"] and len(row["wrapper_id"]) == 64
        # A sent row is history, not work in progress, and the echo rule can see it.
        assert gateway.store.stats()["pending"] == 0
        outbound = gateway.store.recent_outbound(chat, now - 60)
        assert [item["content"] for item in outbound] == ["recorded"]
    finally:
        gateway.store.close()


async def test_a_channel_we_do_not_hold_fails_loudly(tmp_path, monkeypatch, no_follows):
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, sdk.Keys.generate())
    try:
        frame = await gateway.send(to="concord:deadbeef:" + "99" * 32, text="hello")
        assert frame["ok"] is False
        assert "no readable Concord channel" in frame["error"]
        assert sent["events"] == [], "nothing may be published for a channel we do not hold"
    finally:
        gateway.store.close()


async def test_a_refused_publish_is_not_history(tmp_path, monkeypatch, no_follows):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex())
    captured_publish(monkeypatch, outcome="rejected: blocked")
    gateway = gateway_for(tmp_path, me)
    try:
        gateway.concord.set_streams(invite, channels, {owner.public_key().to_hex()})
        frame = await gateway.send(to=chat_for(invite, GENERAL), text="anyone there?")

        assert frame["ok"] is False
        assert frame["errors"] == {relay: "rejected: blocked" for relay in RELAYS}
        assert gateway.store.recent(5) == []
    finally:
        gateway.store.close()


async def test_a_peer_conversation_still_takes_the_dm_route(tmp_path, monkeypatch, no_follows):
    me = sdk.Keys.generate()
    peer = sdk.Keys.generate().public_key().to_hex()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)
    rumor = {"id": "aa" * 32, "kind": 14, "created_at": 1, "pubkey": me.public_key().to_hex(),
             "content": "private"}
    wraps = {"peer": {"id": "bb" * 32, "kind": 1059}, "self": {"id": "cc" * 32, "kind": 1059}}
    monkeypatch.setattr(gateway, "_build_wraps", lambda text, peer, reply_to: (rumor, wraps))
    monkeypatch.setattr(gateway, "_recipient_relays", lambda peer: ["wss://relay.example"])

    async def fake_publish_all(wraps, relays):
        return {label: {relay: (True, None) for relay in relays} for label in wraps}

    monkeypatch.setattr(gateway, "_publish_all", fake_publish_all)
    try:
        frame = await gateway.send(to=peer, text="private")

        assert frame["ok"] is True
        assert frame["message_id"] == rumor["id"]
        assert sent["events"] == [], "a peer conversation must not reach the Concord write path"
        row = gateway.store.rumor(rumor["id"])
        assert row["plane"] == "nip17" and row["conversation"] == peer
    finally:
        gateway.store.close()


# ── the CLI ─────────────────────────────────────────────────────────────────

def test_hng_send_hands_a_channel_conversation_to_the_daemon(tmp_path, monkeypatch, capsys):
    path, _ = make_config(tmp_path)
    chat = "concord:" + "ab" * 32 + ":" + GENERAL
    seen: dict = {}

    async def fake_request(socket_path, frame, **kwargs):
        seen.update(frame)
        return {"type": "delivery", "ok": True, "conversation": frame["to"],
                "relays": {"channel": {"wss://relay.example": True}}}

    monkeypatch.setattr(server, "request", fake_request)
    assert cli.main(["--config", path, "send", "--to", chat, "--text", "hi",
                     "--reply-to", "ab" * 32]) == 0

    assert seen["op"] == "send"
    assert seen["to"] == chat and seen["text"] == "hi" and seen["reply_to"] == "ab" * 32
    assert "-> sent" in capsys.readouterr().out


def test_hng_send_reports_a_channel_refusal(tmp_path, monkeypatch, capsys):
    path, _ = make_config(tmp_path)

    async def fake_request(socket_path, frame, **kwargs):
        return {"type": "delivery", "ok": False, "conversation": frame["to"],
                "error": "no readable Concord channel for 'concord:deadbeef:99'",
                "relays": {"channel": {}}, "errors": {}}

    monkeypatch.setattr(server, "request", fake_request)
    code = cli.main(["--config", path, "send", "--to", "concord:deadbeef:" + "99" * 32,
                     "--text", "hello"])
    assert code == 1
    assert "FAILED" in capsys.readouterr().out
