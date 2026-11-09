"""A routed conversation's turn runs as its profile -- or it does not run at all.

The gateway decides the profile (`profiles:` in its config); this adapter's whole half is one
field on the `SessionSource`. These tests pin the three things that matter at this end: the
field reaches Hermes, a profile Hermes does not have is refused rather than silently run as
the default, and an approval prompt on a routed channel is keyed to the identity the turn ran
under -- otherwise its gate sits in `agent:main` and no `/approve` can reach it.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports (and
Hermes' own profile lookup).
"""

import asyncio
import logging
from collections import OrderedDict

from adapter_harness import ApprovalPrompt, Kw, load_adapter, stub_profiles

adapter = load_adapter("nostr_adapter_profiles")

PEER = "ab" * 32
COMMUNITY = "cd" * 32
CHANNEL = "ef" * 32
ROOM = f"concord:{COMMUNITY}:{CHANNEL}"


def _frame(**overrides):
    frame = {
        "type": "message",
        "plane": "concord",
        "id": "0123456789abcdef",
        "sender": PEER,
        "conversation": ROOM,
        "kind": 9,
        "created_at": 1761400000,
        "content": "the deploy is green",
        "reply_to": None,
        "reach": "immediate",
        "authority": "instructions",
        "why": "policy",
    }
    frame.update(overrides)
    return frame


def _bare_adapter():
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._home_hex = ""
    inst._config = None
    inst._chat_profiles = OrderedDict()
    inst._approval_prompts = OrderedDict()
    inst._delivered_ids = adapter.deque(maxlen=512)
    inst.acked = []
    inst.delivered = []
    inst.sent = []

    async def _request(payload, timeout=30.0):
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
        elif payload.get("op") == "send":
            inst.sent.append(payload)
            return {"type": "ok", "ok": True, "message_id": "sent-1"}
        return {"type": "ok"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


def test_a_routed_frame_runs_as_its_profile():
    stub_profiles({"default", "ops"})
    inst = _bare_adapter()
    asyncio.run(inst._deliver(_frame(profile="ops")))
    event = inst.delivered[0]
    assert event.source.profile == "ops"
    assert event.source.chat_id == ROOM, "the destination does not change, only the identity"
    assert inst.acked == ["0123456789abcdef"]


def test_an_unrouted_frame_leaves_the_profile_to_the_receiver():
    stub_profiles({"default", "ops"})
    inst = _bare_adapter()
    asyncio.run(inst._deliver(_frame()))
    assert inst.delivered[0].source.profile is None


def test_a_profile_hermes_does_not_have_is_refused_not_run_as_default(caplog):
    """The one outcome worse than a dropped message: it runs in another agent's memory."""
    stub_profiles({"default"})
    inst = _bare_adapter()
    with caplog.at_level(logging.ERROR):
        asyncio.run(inst._deliver(_frame(profile="ghost")))
    assert inst.delivered == []
    assert inst.acked == ["0123456789abcdef"], "a refusal is final; do not replay it forever"
    assert any("ghost" in record.message and "does not have" in record.message
               for record in caplog.records)


def test_the_default_profile_is_a_real_profile_not_a_missing_one():
    stub_profiles({"default"})
    inst = _bare_adapter()
    asyncio.run(inst._deliver(_frame(profile="default")))
    assert inst.delivered[0].source.profile == "default"


def _keyed_adapter():
    """An adapter whose session-key builder shows the profile it was given."""
    inst = _bare_adapter()
    inst.build_source = lambda **kwargs: Kw(**kwargs)
    inst._source_session_key = lambda source: (
        f"agent:{getattr(source, 'profile', None) or 'main'}:nostr:dm:{source.chat_id}")
    return inst


def test_an_approval_prompt_on_a_routed_channel_keys_its_gate_with_the_profile():
    """A host that names no session key leaves the fallback to do it; without this the gate is
    registered in `agent:main` and no reaction can resolve it."""
    stub_profiles({"default", "ops"})
    inst = _keyed_adapter()
    asyncio.run(inst._deliver(_frame(profile="ops")))

    asyncio.run(inst._send_exec_approval_prompt(
        ApprovalPrompt(ROOM, "", request_id="req-1")))

    _chat, session_key, _request = inst._approval_prompts["sent-1"]
    assert session_key == f"agent:ops:nostr:dm:{ROOM}"


def test_a_prompt_on_an_unrouted_channel_keeps_the_default_namespace():
    stub_profiles({"default"})
    inst = _keyed_adapter()
    asyncio.run(inst._deliver(_frame(conversation=PEER)))
    asyncio.run(inst._send_exec_approval_prompt(
        ApprovalPrompt(PEER, "", request_id="req-1")))
    _chat, session_key, _request = inst._approval_prompts["sent-1"]
    assert session_key == f"agent:main:nostr:dm:{PEER}"


def test_the_hosts_own_session_key_is_used_as_given():
    """The gateway builds the key for the turn it is blocking; asking it again would only
    re-derive what it already knows."""
    stub_profiles({"default", "ops"})
    inst = _keyed_adapter()
    inst._peers.add(ROOM)
    asyncio.run(inst._send_exec_approval_prompt(
        ApprovalPrompt(ROOM, "agent:ops:nostr:concord:x", request_id="req-1")))

    assert inst._approval_prompts["sent-1"][1] == "agent:ops:nostr:concord:x"


def test_a_route_change_does_not_move_the_next_turn_into_the_old_profile():
    """The profile is per frame, from the gateway: a changed route is a changed field, and
    Hermes keys the session by it, so the old session stays where it was."""
    stub_profiles({"default", "ops", "work"})
    inst = _bare_adapter()
    asyncio.run(inst._deliver(_frame(profile="ops")))
    asyncio.run(inst._deliver(_frame(id="fedcba9876543210", profile="work")))
    assert [event.source.profile for event in inst.delivered] == ["ops", "work"]
    assert inst._chat_profiles[ROOM] == "work", "the newest frame decides the chat's keys"


def test_a_reaction_reaches_the_gate_of_a_routed_channel(monkeypatch):
    """End to end at this end: deliver a routed frame, hand the prompt to the adapter, react."""
    stub_profiles({"default", "ops"})
    inst = _keyed_adapter()
    asyncio.run(inst._deliver(_frame(profile="ops")))
    asyncio.run(inst._send_exec_approval_prompt(
        ApprovalPrompt(ROOM, "", request_id="req-1")))
    session_key = inst._approval_prompts["sent-1"][1]

    seen = {}

    def resolve_gateway_approval(key, choice, request_id=None):
        seen["resolved"] = (key, choice, request_id)
        return 1

    monkeypatch.setitem(__import__("sys").modules, "tools", type("m", (), {})())
    monkeypatch.setitem(__import__("sys").modules, "tools.approval", type("m", (), {
        "resolve_gateway_approval": staticmethod(resolve_gateway_approval),
    })())
    asyncio.run(inst._deliver_reaction(
        {"type": "reaction", "sender": PEER, "reply_to": "sent-1",
         "content": "\U0001f44d", "authority": "instructions"},
        "reaction-id"))
    assert session_key == f"agent:ops:nostr:dm:{ROOM}"
    assert seen["resolved"] == (session_key, "once", "req-1")


# -- the question itself: asked of the host, answered on its public surface ---------------

def test_a_named_profile_is_live_when_its_directory_is():
    stub_profiles({"default", "ops"})
    assert adapter.profile_missing("ops") is False


def test_a_ghost_directory_that_is_not_a_live_profile_is_missing():
    """A directory a delete tombstoned is not a profile the host would run a turn in."""
    stub_profiles({"default"}, tombstoned={"ghost"})
    assert adapter.profile_missing("ghost") is True


def test_the_id_grammar_is_the_hosts():
    """Anything `-p` would refuse cannot name a profile, whatever is on disk."""
    stub_profiles({"default", "ops"})
    for name in ("../ops", "Ops!", "a" * 65, ".hidden", "/abs"):
        assert adapter.profile_missing(name) is True, name


def test_a_name_is_matched_the_way_the_host_matches_it():
    stub_profiles({"default", "ops"})
    assert adapter.profile_missing("  OPS  ") is False   # normalized, like `-p OPS`
    assert adapter.profile_missing("default") is False
    assert adapter.profile_missing("DEFAULT") is False


def test_an_empty_or_non_string_name_is_missing():
    stub_profiles({"default"})
    assert adapter.profile_missing("") is True
    assert adapter.profile_missing("   ") is True
    assert adapter.profile_missing(None) is True
