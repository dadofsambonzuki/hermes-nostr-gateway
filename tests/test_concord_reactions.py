"""A reaction in a Concord channel reaches the adapter: kind 7 is a frame, not chat.

An approval prompt is posted into a channel and the recipient answers it with 👍 or 👎. The
daemon used to drop the inner kind 7 while the wrap was still being opened (`(9, 1111)` only),
so the gate timed out with no way to answer it — the adapter's reaction code was live and
untested against a channel. These tests hold the plane's half: a reaction is read out of the
channel, never stored and never treated as a message, carries the ids and the authority the
adapter matches on, and the daemon turns it into a `reaction` frame. The last section drives
that frame through the adapter itself, which is where the gate is actually resolved.

A kind-7 wrap is built here the way a client builds one: the channel's own stream key wraps an
author-signed seal around a rumor whose `e` names the message being answered.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict

import nostr_sdk as sdk

import adapter_harness as harness
from hermes_nostr_gateway import crypto
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord import write
from hermes_nostr_gateway.planes.concord.plane import ChannelReaction, ConcordPlane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store
from test_concord_channels import CHANNEL_ID, make_invite, policy_for, room_channel
from test_concord_reply import gateway_for

RELAY = "wss://relay.example"
#: The id the adapter remembers a prompt under: the rumor the prompt was sent as.
PROMPT = "e" * 64


def _wrap(author: sdk.Keys, invite: c.Invite, channel: c.Channel, *,
          kind: int = 7, content: str = "\U0001f44d", target: str = PROMPT,
          prompt_author: str = "", tags: list[list[str]] | None = None) -> dict:
    """Seal one kind-7 rumor at the channel's address, as a member's client would.

    `target` is the rumor being answered and `prompt_author` the `p` NIP-25 puts beside it —
    for an approval prompt both point at us, which is why a reaction reads as addressed.
    """
    gk = c.channel_keys(invite, channel.id, channel.key, channel.epoch)
    rumor = {
        "kind": kind,
        "pubkey": author.public_key().to_hex(),
        "content": content,
        "tags": tags if tags is not None else
                [["channel", channel.id], ["epoch", "0"], ["e", target],
                 ["p", prompt_author], ["ms", "1"]],
        "created_at": int(time.time()),
    }
    rumor["id"] = crypto.event_id(rumor)
    return write._seal_and_wrap(rumor, gk, author)


def _read(plane: ConcordPlane, wrap: dict, policy: Policy, me: sdk.Keys):
    return plane.handle_channel(RELAY, wrap, policy, me_hex=me.public_key().to_hex(),
                               me_npub=me.public_key().to_bech32())


def _plane(tmp_path, name: str = "reactions.db"):
    """A community of two — the prompt's author and one reacting member — and its plane."""
    nathan, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite = make_invite(nathan.public_key().to_hex())
    channel = room_channel(invite)
    store = Store(tmp_path / name)
    plane = ConcordPlane(store)
    plane.set_streams(invite, [channel], {nathan.public_key().to_hex(),
                                          me.public_key().to_hex()})
    return store, plane, invite, channel, nathan, me


# ── the plane ───────────────────────────────────────────────────────────────

def test_a_channel_reaction_is_a_frame_and_never_a_message(tmp_path):
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    with store:
        wrap = _wrap(nathan, invite, channel, content="\U0001f44d",
                     prompt_author=me.public_key().to_hex())

        got = _read(plane, wrap, policy_for(nathan.public_key().to_hex()), me)

        assert isinstance(got, ChannelReaction)
        # The three ids the adapter needs: what was answered, by whom, and which gate.
        assert (got.target, got.sender, got.conversation) == (
            PROMPT, nathan.public_key().to_hex(),
            ch.chat_id(invite.community_id.hex(), CHANNEL_ID))
        assert got.emoji == "\U0001f44d"
        # The reactor is who the policy lets instruct in this room.
        assert got.authority == "instructions"
        # A reaction is not chat: nothing is stored, so nothing can be replayed as a message.
        assert store.recent(10) == []

        frame = Gateway.frame_for(got)
        assert frame["type"] == "reaction"
        assert frame["plane"] == "concord"
        assert frame["target_rumor_id"] == PROMPT
        assert frame["from"] == nathan.public_key().to_hex()
        assert frame["emoji"] == "\U0001f44d"
        assert frame["authority"] == "instructions"
        assert frame["id"] == got.id
        # Not a message frame: no kind, no content, nothing that could start an agent turn.
        assert "kind" not in frame and "content" not in frame


def test_a_replayed_reaction_is_dropped_by_its_wrapper(tmp_path):
    """A relay's replay of the same wrap must not resolve the gate a second time."""
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    policy = policy_for(nathan.public_key().to_hex())
    with store:
        wrap = _wrap(nathan, invite, channel)
        assert _read(plane, wrap, policy, me) is not None

        assert _read(plane, wrap, policy, me) is None
        # Wrapper ids are claimed per event, not per relay: a second relay's copy is the
        # same reaction.
        assert plane.handle_channel("wss://relay2.example", wrap, policy,
                                    me_hex=me.public_key().to_hex(),
                                    me_npub=me.public_key().to_bech32()) is None


def test_a_reaction_from_a_stranger_carries_no_authority(tmp_path):
    """The plane states the policy's answer and does not invent one: the adapter decides."""
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    stranger = sdk.Keys.generate()
    with store:
        found = _read(plane, _wrap(stranger, invite, channel),
                      policy_for(nathan.public_key().to_hex()), me)

        assert isinstance(found, ChannelReaction)
        assert found.authority == "none"
        assert store.recent(10) == []


def test_a_reaction_with_nothing_to_answer_is_dropped(tmp_path):
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    with store:
        untargeted = _wrap(nathan, invite, channel,
                           tags=[["channel", channel.id], ["epoch", "0"]])

        assert _read(plane, untargeted, policy_for(nathan.public_key().to_hex()), me) is None
        assert store.recent(10) == []


def test_our_own_reaction_coming_back_is_ignored(tmp_path):
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    with store:
        own = _wrap(me, invite, channel)

        assert _read(plane, own, policy_for(nathan.public_key().to_hex()), me) is None


def test_a_reaction_does_not_spend_the_senders_delivery_allowance(tmp_path):
    """Authority is a probe of the rules, not a delivery: it must not take the only token."""
    store, plane, invite, channel, nathan, me = _plane(tmp_path)
    me_hex = me.public_key().to_hex()
    with store:
        policy = Policy({
            "groups": {"family": [nathan.public_key().to_hex()]},
            "policies": [{"name": "family-mentions",
                          "match": {"group": "family", "kinds": [9], "mention": True},
                          "reach": "immediate", "authority": "instructions"}],
            "defaults": {"reach": "drop", "authority": "none"},
            # One token for this test: if a reaction spent it, the message after it is
            # refused and the gate would have been answered by an exhausted sender.
            "rate_limit": {"enabled": True, "burst": 1, "per_hour": 30},
        })
        assert isinstance(_read(plane, _wrap(nathan, invite, channel), policy, me),
                          ChannelReaction)

        message = write.build_channel_message(nathan, invite, channel.id, "still here",
                                              channel_key=channel.key, epoch=channel.epoch)
        got = plane.handle_channel(RELAY, message["wrap"], policy, me_hex=me_hex,
                                   me_npub=me.public_key().to_bech32())

        assert got is not None and got.reach == "immediate", got


# ── the daemon ──────────────────────────────────────────────────────────────

async def test_the_daemon_emits_a_reaction_frame_and_stores_no_row(tmp_path, monkeypatch):
    """The whole live path: `_on_event` -> `frame_for` -> the socket, with nothing stored."""
    nathan, me = sdk.Keys.generate(), sdk.Keys.generate()
    invite = make_invite(nathan.public_key().to_hex())
    channel = room_channel(invite)
    gateway = gateway_for(tmp_path, me)
    frames: list[dict] = []

    async def capture(frame):
        frames.append(frame)

    monkeypatch.setattr(gateway.server, "broadcast", capture)
    try:
        gateway.concord.set_streams(invite, [channel], {nathan.public_key().to_hex(),
                                                        me.public_key().to_hex()})
        gateway._on_event(RELAY, _wrap(nathan, invite, channel))
        await asyncio.gather(*gateway._deliveries)

        assert [frame["type"] for frame in frames] == ["reaction"]
        assert frames[0]["target_rumor_id"] == PROMPT
        assert gateway.store.recent(10) == []
    finally:
        gateway.store.close()


# ── the adapter ─────────────────────────────────────────────────────────────

adapter = harness.load_adapter("nostr_adapter_concord_reaction")
SESSION = "agent:main:nostr:group:concord"


def chat_id_of(invite: c.Invite) -> str:
    return ch.chat_id(invite.community_id.hex(), CHANNEL_ID)


def _frame_for(tmp_path, emoji: str, *, name: str = "adapter.db"):
    """The frame the daemon would deliver for one reaction, and the room it came from.

    Round-tripped through JSON because the socket does: a frame that cannot be written is
    not a frame, whatever the dict says.
    """
    store, plane, invite, channel, nathan, me = _plane(tmp_path, name)
    policy = policy_for(nathan.public_key().to_hex())
    with store:
        got = _read(plane, _wrap(nathan, invite, channel, content=emoji), policy, me)
        assert got is not None, "the reaction never left the plane"
        return json.loads(json.dumps(got.to_frame())), chat_id_of(invite)


def _answering_adapter(chat_id: str):
    """Just the half of the adapter a reaction goes through, with the queue faked."""
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._approval_prompts = OrderedDict()
    inst._remember_approval_prompt(PROMPT, chat_id, SESSION)
    inst.resolved = []
    inst.acked = []

    def resolve(session_key: str, choice: str) -> int:
        inst.resolved.append((session_key, choice))
        return 1

    async def ack(message_id: str) -> bool:
        inst.acked.append(message_id)
        return True

    inst._resolve_approval = resolve
    inst._ack = ack
    return inst


async def test_the_planes_frame_answers_the_gate_once(tmp_path):
    frame, chat_id = _frame_for(tmp_path, "\U0001f44d", name="up.db")
    inst = _answering_adapter(chat_id)

    await inst._deliver_reaction(frame, frame["id"])

    assert inst.resolved == [(SESSION, "once")]
    assert inst.acked == [frame["id"]]     # acked, so nothing replays it


async def test_a_thumbs_down_denies_through_the_same_frame(tmp_path):
    frame, chat_id = _frame_for(tmp_path, "\U0001f44e", name="down.db")
    inst = _answering_adapter(chat_id)

    await inst._deliver_reaction(frame, frame["id"])

    assert inst.resolved == [(SESSION, "deny")]


async def test_a_reaction_that_is_not_a_decision_leaves_the_gate_alone(tmp_path):
    frame, chat_id = _frame_for(tmp_path, "\U0001f525", name="fire.db")
    inst = _answering_adapter(chat_id)

    await inst._deliver_reaction(frame, frame["id"])

    assert inst.resolved == []
    assert inst.acked == [frame["id"]]     # consumed, not replayed into the agent
    # The gate is still answerable by a real answer afterwards.
    answer, _ = _frame_for(tmp_path, "\U0001f44d", name="fire2.db")
    await inst._deliver_reaction(answer, answer["id"])
    assert inst.resolved == [(SESSION, "once")]


async def test_a_second_reaction_on_the_same_prompt_does_nothing(tmp_path):
    first, chat_id = _frame_for(tmp_path, "\U0001f44d", name="once.db")
    inst = _answering_adapter(chat_id)

    await inst._deliver_reaction(first, first["id"])
    second, _ = _frame_for(tmp_path, "\U0001f44d", name="twice.db")
    await inst._deliver_reaction(second, second["id"])

    assert inst.resolved == [(SESSION, "once")]
    assert inst.acked == [first["id"], second["id"]]
