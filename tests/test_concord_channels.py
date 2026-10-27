"""Concord channel reads: the mention rule, the small-channel rule, and what gets stored."""

from __future__ import annotations

import hashlib
import os

import nostr_sdk as sdk

from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord import write
from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

ROOM = "General"
CHANNEL_ID = "11" * 32


def make_invite(owner_pubkey: str) -> c.Invite:
    salt, root = os.urandom(32), os.urandom(32)
    community_id = hashlib.sha256(b"concord/community" + bytes.fromhex(owner_pubkey) + salt).digest()
    return c.Invite(bundle={
        "owner": owner_pubkey, "owner_salt": salt.hex(), "community_id": community_id.hex(),
        "community_root": root.hex(), "root_epoch": 0, "control_pk": os.urandom(32).hex(),
        "name": "Test Community", "relays": ["wss://relay.example"],
        "channels": [{"id": CHANNEL_ID, "key": os.urandom(32).hex(), "epoch": 0, "name": ROOM}],
    }, inviter=owner_pubkey, relay="", wrap_id="w")


def policy_for(nathan_hex: str) -> Policy:
    return Policy({
        "groups": {"family": [nathan_hex]},
        "policies": [{"name": "family-mentions",
                      "match": {"group": "family", "kinds": [9], "mention": True},
                      "reach": "immediate", "authority": "instructions"}],
        "defaults": {"reach": "drop", "authority": "none"},
    })


def room_channel(invite: c.Invite) -> c.Channel:
    return c.Channel(id=CHANNEL_ID, name=ROOM, private=True,
                     key=invite.bundle["channels"][0]["key"], epoch=0)


def wrap_from(author: sdk.Keys, invite: c.Invite, channel: c.Channel, text: str) -> dict:
    built = write.build_channel_message(author, invite, channel.id, text,
                                        channel_key=channel.key, epoch=channel.epoch)
    return built["wrap"]


# ── the rules ───────────────────────────────────────────────────────────────

def test_a_two_person_channel_needs_no_mention():
    assert ch.mention_required(2) is False
    assert ch.mention_required(3) is True
    assert ch.mention_required(0) is True          # unknown membership: mention required


def test_mention_is_a_p_tag_or_a_nip21_mention_in_the_text():
    me = sdk.Keys.generate()
    me_hex = me.public_key().to_hex()
    me_npub = me.public_key().to_bech32()
    other = sdk.Keys.generate().public_key().to_bech32()

    assert ch.has_mention("hello", [["p", me_hex]], me_hex, me_npub) is True
    assert ch.has_mention(f"hey nostr:{me_npub}", [], me_hex, me_npub) is True
    assert ch.has_mention(f"bare {me_npub} here", [], me_hex, me_npub) is True
    assert ch.has_mention(f"hey nostr:{other}", [], me_hex, me_npub) is False
    assert ch.has_mention("no mention at all", [["p", "ff" * 32]], me_hex, me_npub) is False


def test_the_decision_reports_why():
    stream = ch.ChannelStream(community_id="ab" * 32, channel_id=CHANNEL_ID, name="ops",
                              private=False, epoch=0, address="cd" * 32,
                              chat_id=ch.chat_id("ab" * 32, CHANNEL_ID))
    addressing, why = ch.decides(stream, mentioned=False, members=2)
    assert addressing is True and "no mention needed" in why
    addressing, why = ch.decides(stream, mentioned=False, members=3)
    assert addressing is False and "mention required" in why
    addressing, why = ch.decides(stream, mentioned=True, members=9)
    assert addressing is True and "mention" in why


# ── the plane ───────────────────────────────────────────────────────────────

def test_a_channel_message_is_stored_under_its_channel_without_a_mention(tmp_path):
    nathan, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite = make_invite(nathan.public_key().to_hex())
    channel = room_channel(invite)
    wrap = wrap_from(nathan, invite, channel, "this is the gateway work now")

    with Store(tmp_path / "channels.db") as store:
        plane = ConcordPlane(store)
        plane.set_streams(invite, [channel], {nathan.public_key().to_hex(),
                                             me.public_key().to_hex()})
        msg = plane.handle_channel("wss://relay.example", wrap, policy_for(nathan.public_key().to_hex()),
                                   me_hex=me.public_key().to_hex(),
                                   me_npub=me.public_key().to_bech32())
        assert msg is not None
        assert msg.plane == "concord"
        assert msg.conversation == ch.chat_id(invite.community_id.hex(), CHANNEL_ID)
        assert msg.content == "this is the gateway work now"
        # Two members, no mention: the policy still treats it as addressed.
        assert msg.reach == "immediate" and msg.authority == "instructions"
        assert "no mention needed" in msg.why
        assert len(store.recent(10)) == 1

        # Re-delivery of the same wrap on the same relay is not a second message.
        assert plane.handle_channel("wss://relay.example", wrap,
                                    policy_for(nathan.public_key().to_hex()),
                                    me_hex=me.public_key().to_hex(),
                                    me_npub=me.public_key().to_bech32()) is None


def test_a_third_member_brings_the_mention_requirement_back(tmp_path):
    nathan, me, third = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    invite = make_invite(nathan.public_key().to_hex())
    channel = room_channel(invite)
    quiet = wrap_from(nathan, invite, channel, "no mention here")
    named = wrap_from(nathan, invite, channel,
                      f"hey nostr:{me.public_key().to_bech32()} look at this")

    with Store(tmp_path / "three.db") as store:
        plane = ConcordPlane(store)
        plane.set_streams(invite, [channel], {nathan.public_key().to_hex(),
                                             me.public_key().to_hex(),
                                             third.public_key().to_hex()})
        policy = policy_for(nathan.public_key().to_hex())
        dropped = plane.handle_channel("wss://relay.example", quiet, policy,
                                       me_hex=me.public_key().to_hex(),
                                       me_npub=me.public_key().to_bech32())
        assert dropped is not None and dropped.reach == "drop"
        assert "mention required" in dropped.why

        delivered = plane.handle_channel("wss://relay.example", named, policy,
                                         me_hex=me.public_key().to_hex(),
                                         me_npub=me.public_key().to_bech32())
        assert delivered is not None and delivered.reach == "immediate"
        assert delivered.authority == "instructions"
