"""Concord: key derivation against the NIP-44 vectors, envelopes in both directions,
and the rule that an invite is routed to the plane but never becomes a message."""

from __future__ import annotations

import hashlib
import json
import os

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import crypto
from hermes_nostr_gateway.crypto import SdkCrypto
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord import write
from hermes_nostr_gateway.planes.concord.derive import group_key
from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.planes.nip17 import Nip17Plane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

# NIP-44 official test vector (v2.get_conversation_key[0]).
NIP44_VECTOR = {
    "sec1": "315e59ff51cb9209768cf7da80791ddcaae56ac9775eb25b6dee1234bc5d2268",
    "pub2": "c2f9d9948dc8c7c38321e4b85c8558872eafa0641cd269db76848a6073e69133",
    "conversation_key": "3dfef0ce2a4d80a25e7a328accf73448ef67096f65f79588e358d9a0eb9013f1",
}

DROP_ALL = {"groups": {}, "policies": [], "defaults": {"reach": "drop", "authority": "none"}}


def make_invite(owner: sdk.Keys, *, channel_id: str | None = None,
                channel_key: str | None = None) -> c.Invite:
    owner_hex = owner.public_key().to_hex()
    salt = os.urandom(32)
    root = os.urandom(32)
    community_id = hashlib.sha256(b"concord/community" + bytes.fromhex(owner_hex) + salt).digest()
    channel_id = channel_id or os.urandom(32).hex()
    bundle = {
        "owner": owner_hex,
        "owner_salt": salt.hex(),
        "community_id": community_id.hex(),
        "community_root": root.hex(),
        "root_epoch": 0,
        "control_pk": os.urandom(32).hex(),
        "name": "Test Community",
        "relays": ["wss://relay.example"],
        "channels": [{"id": channel_id, "key": channel_key or os.urandom(32).hex(),
                      "epoch": 0, "name": "general"}],
    }
    return c.Invite(bundle=bundle, inviter=owner_hex, relay="", wrap_id="w")


def test_conversation_key_matches_the_nip44_vector():
    derived = crypto.nip44_conversation_key(NIP44_VECTOR["sec1"], NIP44_VECTOR["pub2"])
    assert derived.hex() == NIP44_VECTOR["conversation_key"]
    assert len(derived) == 32


def test_conversation_key_is_symmetric():
    one, two = sdk.Keys.generate(), sdk.Keys.generate()
    a = crypto.nip44_conversation_key(one.secret_key().to_hex(), two.public_key().to_hex())
    b = crypto.nip44_conversation_key(two.secret_key().to_hex(), one.public_key().to_hex())
    assert a == b


def test_nip44_round_trip_on_an_arbitrary_pair():
    one, two = sdk.Keys.generate(), sdk.Keys.generate()
    payload = crypto.nip44_encrypt(one.secret_key().to_hex(), two.public_key().to_hex(), "hello")
    assert crypto.nip44_decrypt(two.secret_key().to_hex(), one.public_key().to_hex(),
                                payload) == "hello"


def test_event_id_is_the_bindings():
    author = "aa" * 32
    rumor = crypto.build_rumor(author, "bb" * 32, "same id twice")
    assert rumor["id"] == crypto.event_id(rumor)


def test_verify_bundle_self_certification():
    owner = sdk.Keys.generate()
    invite = make_invite(owner)
    c.verify_bundle(invite.bundle)
    invite.bundle["community_id"] = "00" * 32
    with pytest.raises(c.ConcordError):
        c.verify_bundle(invite.bundle)


def test_group_key_is_deterministic():
    secret = b"root" * 8
    ident = b"id" * 16
    one = group_key("concord/channel", secret, ident, 3)
    two = group_key("concord/channel", secret, ident, 3)
    assert one.pk == two.pk
    assert one.conv_key == two.conv_key
    assert one.encrypt("x") != one.encrypt("x")  # random nonce, same key
    assert one.decrypt(one.encrypt("x")) == "x"


def test_channel_message_round_trips_through_envelope():
    owner = sdk.Keys.generate()
    invite = make_invite(owner)
    channel = invite.bundle["channels"][0]
    built = write.build_channel_message(owner, invite, channel["id"], "hello channel",
                                       channel_key=channel["key"])
    gk = c.channel_keys(invite, channel["id"], channel["key"], 0)
    write.verify_envelope(built, gk)
    opened = c.open_wrap(gk, built["wrap"])
    assert opened["content"] == "hello channel"
    assert opened["pubkey"] == owner.public_key().to_hex()


def test_channel_message_is_readable_as_a_message():
    owner = sdk.Keys.generate()
    invite = make_invite(owner)
    channel = invite.bundle["channels"][0]
    quoted = ("ab" * 32, "cd" * 32)
    built = write.build_channel_message(owner, invite, channel["id"], "read me",
                                       channel_key=channel["key"], quote=quoted)
    gk = c.channel_keys(invite, channel["id"], channel["key"], 0)
    message = c.parse_channel_wrap(gk, built["wrap"], channel["id"])
    assert message is not None
    assert message.text == "read me"
    assert message.author == owner.public_key().to_hex()
    assert message.id == built["rumor"]["id"]
    assert message.quote == quoted


def test_guestbook_join_round_trips():
    owner = sdk.Keys.generate()
    invite = make_invite(owner)
    built = write.build_guestbook_join(owner, invite)
    gk = c.guestbook_key(invite)
    write.verify_envelope(built, gk)
    opened = c.open_wrap(gk, built["wrap"])
    assert opened["kind"] == 3306
    assert opened["content"] == "join"


def test_tampered_seal_fails_verification():
    owner = sdk.Keys.generate()
    invite = make_invite(owner)
    built = write.build_guestbook_join(owner, invite)
    gk = c.guestbook_key(invite)
    built["wrap"]["content"] = gk.encrypt("{}")
    with pytest.raises(c.ConcordError):
        write.verify_envelope(built, gk)


def test_invite_wrapper_is_routed_to_concord_and_stored(tmp_path):
    alice, bob = SdkCrypto(sdk.Keys.generate().secret_key().to_hex()), \
        SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    invite = make_invite(sdk.Keys.parse(alice.keys.secret_key().to_hex()))
    rumor = crypto.build_rumor(alice.our_pubkey(), bob.our_pubkey(),
                               json.dumps(invite.bundle), kind=3313)
    wrap = alice.wrap(rumor, bob.our_pubkey())

    with Store(tmp_path / "invites.db") as store:
        plane = Nip17Plane(bob, store, Policy(DROP_ALL),
                           on_invite=ConcordPlane(store).handle_invite)
        assert plane.handle("wss://relay.example", wrap) is None
        rows = store.concord_invites()
        assert len(rows) == 1
        assert rows[0]["bundle"]["name"] == "Test Community"
        assert rows[0]["inviter"] == alice.our_pubkey()
        # never a message, never delivered
        assert store.recent(10) == []


def test_bad_invite_is_not_stored(tmp_path):
    alice, bob = SdkCrypto(sdk.Keys.generate().secret_key().to_hex()), \
        SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    rumor = crypto.build_rumor(alice.our_pubkey(), bob.our_pubkey(), "{}", kind=3313)
    wrap = alice.wrap(rumor, bob.our_pubkey())

    with Store(tmp_path / "bad.db") as store:
        plane = Nip17Plane(bob, store, Policy(DROP_ALL),
                           on_invite=ConcordPlane(store).handle_invite)
        assert plane.handle("wss://relay.example", wrap) is None
        assert store.concord_invites() == []


async def test_load_community_lists_granted_channels_without_the_network(monkeypatch):
    owner = sdk.Keys.generate()
    invite = make_invite(owner)

    async def no_events(relays, filters, **kwargs):
        return []

    monkeypatch.setattr(c, "fetch_events", no_events)
    community = await c.load_community(invite)
    assert community.name == "Test Community"
    assert [ch.id for ch in community.channels] == [invite.bundle["channels"][0]["id"]]
