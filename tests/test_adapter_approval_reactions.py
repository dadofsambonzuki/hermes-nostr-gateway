"""Approvals answered by a reaction: a 👍/👎 on the prompt resolves the gate.

The host posts a blocking approval prompt through `adapter.send(..., metadata=
{"is_approval_prompt": True})` and waits; the typed `/approve` / `/deny` path is not the only
way in when the message is in a chat the recipient can react to. What is under test here is
only the adapter's half: remember the prompt's message id, decide whether an arriving kind-7
reaction is an *answer*, and resolve exactly that one gate through `tools.approval`.

`tools.approval` is faked: the real queue belongs to the host process, and its own tests own
it. The fake keeps the two calls the adapter makes and records what it was asked to resolve.
"""

from __future__ import annotations

import sys
import types
from collections import OrderedDict, deque

import pytest

import adapter_harness as harness

adapter = harness.load_adapter("nostr_adapter_approval")

PEER = "ab" * 32
OTHER = "cd" * 32
PROMPT_ID = "e" * 64
REACTION_ID = "f" * 64
SESSION = f"agent:main:nostr:dm:{PEER}"


class FakeApprovals:
    """`tools.approval` as the adapter uses it: a read of the queue, then one resolve."""

    def __init__(self):
        self.pending: list[dict] = []
        self.resolved: list[tuple[str, str, str | None]] = []

    def list_gateway_approvals(self, session_key: str) -> list[dict]:
        return [dict(entry) for entry in self.pending if entry.get("session_key") == session_key]

    def resolve_gateway_approval(self, session_key: str, choice: str,
                                 request_id: str | None = None, **_kwargs) -> int:
        for entry in list(self.pending):
            if entry.get("session_key") != session_key:
                continue
            if request_id and entry.get("request_id") != request_id:
                continue
            self.pending.remove(entry)
            self.resolved.append((session_key, choice, entry.get("request_id")))
            return 1
        return 0


@pytest.fixture
def approvals(monkeypatch):
    """Stand in for the host's approval module inside the shared process."""
    fake = FakeApprovals()
    module = types.ModuleType("tools.approval")
    module.list_gateway_approvals = fake.list_gateway_approvals
    module.resolve_gateway_approval = fake.resolve_gateway_approval
    module.fake = fake
    tools = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", module)
    monkeypatch.setattr(tools, "approval", module, raising=False)
    return fake


def _adapter():
    """A bare adapter with the two host hooks the session key is built from."""
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = {PEER}
    inst._delivered_ids = deque(maxlen=512)
    inst._approval_prompts = OrderedDict()
    inst._home_hex = ""
    inst._config = None
    inst.acked = []
    inst.delivered = []
    inst.next_id = PROMPT_ID
    # Hermes' base adapter: build_source() plus the same key builder the gateway uses.
    inst.build_source = lambda **kwargs: types.SimpleNamespace(**kwargs)
    inst._source_session_key = lambda source: f"agent:main:nostr:dm:{source.chat_id}"

    async def _request(payload, timeout=30.0):
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
            return {"type": "ok", "ok": True}
        return {"type": "delivery", "ok": True, "message_id": inst.next_id}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


async def _send_prompt(inst, message_id: str = PROMPT_ID, chat_id: str = PEER,
                       request_id: str = "req-0"):
    """Send a prompt the way the host does: the gate is queued before the message goes out.

    The adapter records the prompt's request id at send time, so the test has to mirror the
    host's order. `tools.approval` in this process is the fake, and appending to it first is
    exactly what `_await_gateway_decision` does before its notify callback.
    """
    module = sys.modules.get("tools.approval")
    fake = getattr(module, "fake", None)
    if fake is not None and request_id:
        if not any(str(entry.get("request_id") or "") == request_id
                   for entry in fake.pending):
            fake.pending.append({"session_key": SESSION, "request_id": request_id,
                                 "command": "rm -rf /tmp/x"})
    inst.next_id = message_id
    return await inst.send(chat_id, "Run this command?\n\n/approve or /deny",
                           metadata={"is_approval_prompt": True})


def _reaction(content="\U0001f44d", target=PROMPT_ID, sender=PEER, rumor_id=REACTION_ID):
    """The frame the NIP-17 plane builds for an inner kind-7 rumor."""
    return {"type": "message", "plane": "nip17", "id": rumor_id, "sender": sender,
            "conversation": sender, "kind": 7, "created_at": 1761400000, "content": content,
            "reply_to": target, "reach": "immediate", "authority": "instructions"}


def _pending(fake, *entries):
    """Queue gates in the fake, idempotent by request id.

    `_send_prompt` already queues the gate it is about to send (the host's order), so the
    tests that call this after it add only the entries that are not there yet.
    """
    for position, entry in enumerate(entries or ({},)):
        item = {"session_key": entry.get("session_key", SESSION),
                "request_id": entry.get("request_id", f"req-{position}"),
                "command": entry.get("command", "rm -rf /tmp/x")}
        if any(str(existing.get("request_id") or "") == item["request_id"]
               and existing.get("session_key") == item["session_key"]
               for existing in fake.pending):
            continue
        fake.pending.append(item)


async def test_thumbs_up_resolves_once(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == [(SESSION, "once", "req-0")]
    assert inst.delivered == []          # a reaction is never an agent turn
    assert inst.acked == [REACTION_ID]   # ... and is acknowledged, so it cannot replay


async def test_thumbs_down_resolves_deny(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44e"))

    assert approvals.resolved == [(SESSION, "deny", "req-0")]
    assert inst.delivered == []


@pytest.mark.parametrize("emoji", ["+", "\u2705", "\u2764", "\u2764\ufe0f", " \U0001f44d "])
async def test_the_other_yes_reactions_approve_once(approvals, emoji):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction(emoji))

    assert approvals.resolved == [(SESSION, "once", "req-0")], emoji


async def test_a_reaction_on_a_different_message_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d", target="9" * 64))

    assert approvals.resolved == []
    assert approvals.pending, "the gate must still be waiting"
    assert inst.acked == [REACTION_ID]


async def test_a_reaction_from_another_author_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d", sender=OTHER))

    assert approvals.resolved == []
    # A stranger's reaction neither answers the gate nor destroys the prompt's mapping:
    # the recipient can still answer it afterwards.
    await inst._deliver(_reaction("\U0001f44d", rumor_id="f" * 63 + "1"))
    assert approvals.resolved == [(SESSION, "once", "req-0")]


async def test_an_emoji_that_is_not_a_decision_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f525"))  # 🔥

    assert approvals.resolved == []
    await inst._deliver(_reaction("\U0001f44d", rumor_id="f" * 63 + "2"))
    assert approvals.resolved == [(SESSION, "once", "req-0")]


async def test_a_reaction_with_no_prompt_resolves_nothing(approvals):
    inst = _adapter()
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []
    assert approvals.pending


async def test_one_reaction_answers_a_prompt_only_once(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)
    await inst._deliver(_reaction("\U0001f44d"))
    assert approvals.resolved == [(SESSION, "once", "req-0")]

    # The next gate this session opens must not be approved by a stale tap on the last one.
    _pending(approvals, {"request_id": "req-later"})
    await inst._deliver(_reaction("\U0001f44d", rumor_id="f" * 63 + "3"))

    assert approvals.resolved == [(SESSION, "once", "req-0")]
    assert approvals.pending


async def test_a_second_waiting_approval_is_not_approved_by_a_reaction(approvals):
    """A reaction names a message's gate, not a queue position: only that gate resolves."""
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-0"}, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == [(SESSION, "once", "req-0")]
    assert [entry["request_id"] for entry in approvals.pending] == ["req-1"]


async def test_no_session_key_means_no_resolve(approvals):
    """Without Hermes' key builder there is no key to resolve under — and no guess."""
    inst = _adapter()
    del inst.build_source
    del inst._source_session_key
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []


async def test_without_an_approval_module_nothing_is_resolved(monkeypatch):
    inst = _adapter()
    await _send_prompt(inst)
    monkeypatch.setitem(sys.modules, "tools.approval", None)  # import raises, queue unknown

    await inst._deliver(_reaction("\U0001f44d"))  # must not raise into the reader loop

    assert inst.delivered == []
    assert inst.acked == [REACTION_ID]


async def test_send_remembers_an_approval_prompt_by_its_message_id(approvals):
    inst = _adapter()
    result = await _send_prompt(inst)

    assert result.success
    assert inst._approval_prompts[PROMPT_ID] == (PEER, SESSION, "req-0")


async def test_send_does_not_remember_ordinary_messages(approvals):
    inst = _adapter()
    inst.next_id = "m-1"
    await inst.send(PEER, "morning — sit rep please")

    assert PROMPT_ID not in inst._approval_prompts
    assert list(inst._approval_prompts) == []


async def test_a_prompt_that_failed_to_send_is_not_answerable(approvals):
    inst = _adapter()

    async def _refused(payload, timeout=30.0):
        return {"type": "delivery", "ok": False, "errors": {"wss://relay.example": "closed"}}

    inst._request = _refused
    result = await inst.send(PEER, "Run this command?", metadata={"is_approval_prompt": True})

    assert not result.success
    assert list(inst._approval_prompts) == []


async def test_only_the_newest_prompts_are_remembered(approvals):
    inst = _adapter()
    ids = [f"{n:064x}" for n in range(1, adapter.MAX_TRACKED_APPROVALS + 6)]
    for message_id in ids:
        await _send_prompt(inst, message_id=message_id)

    assert len(inst._approval_prompts) == adapter.MAX_TRACKED_APPROVALS
    assert ids[-1] in inst._approval_prompts
    assert ids[0] not in inst._approval_prompts

    _pending(approvals)
    await inst._deliver(_reaction("\U0001f44d", target=ids[0]))
    assert approvals.resolved == []      # too old to accept a reaction
    await inst._deliver(_reaction("\U0001f44d", target=ids[-1], rumor_id="f" * 63 + "4"))
    assert approvals.resolved == [(SESSION, "once", "req-0")]


async def test_the_documented_reaction_frame_is_accepted_too(approvals):
    """Spec §5's `reaction` frame carries the same decision as a kind-7 message frame."""
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals)

    await inst._deliver({"type": "reaction", "plane": "nip17", "id": REACTION_ID,
                         "target_rumor_id": PROMPT_ID, "from": PEER,
                         "emoji": "\U0001f44d", "created_at": 1761400001,
                         "authority": "instructions"})

    assert approvals.resolved == [(SESSION, "once", "req-0")]
    assert inst.acked == [REACTION_ID]


def test_reactions_are_recognised_by_shape():
    assert adapter.is_reaction_frame(_reaction())
    assert adapter.is_reaction_frame({"type": "reaction", "id": "x"})
    assert not adapter.is_reaction_frame({"type": "message", "kind": 14})
    assert not adapter.is_reaction_frame({"type": "blocked", "kind": 7})


CHANNEL = "concord:" + "1" * 64 + ":" + "2" * 64


async def test_a_channel_reaction_from_an_authorised_author_answers_the_prompt(approvals):
    """In a Concord channel the chat id names the room, not a person: the sender is a
    pubkey, so equality can never match. The author the policy lets instruct in that room
    is the one who can answer its gate."""
    inst = _adapter()
    inst._remember_approval_prompt(PROMPT_ID, CHANNEL, SESSION, "req-0")
    _pending(approvals)

    await inst._deliver(_reaction("\U0001f44d", sender=PEER))

    assert approvals.resolved == [(SESSION, "once", "req-0")]


async def test_a_channel_reaction_from_an_authorityless_author_is_ignored(approvals):
    inst = _adapter()
    inst._remember_approval_prompt(PROMPT_ID, CHANNEL, SESSION, "req-0")
    _pending(approvals)
    frame = _reaction("\U0001f44d", sender=OTHER)
    frame["authority"] = "none"

    await inst._deliver(frame)

    assert approvals.resolved == []
    # Still answerable by someone who is allowed to instruct there.
    await inst._deliver(_reaction("\U0001f44d", sender=PEER, rumor_id="f" * 63 + "2"))
    assert approvals.resolved == [(SESSION, "once", "req-0")]


async def test_a_prompt_is_bound_to_its_own_request_id(approvals):
    """A late reaction must not approve whatever gate is open by then."""
    inst = _adapter()
    await _send_prompt(inst)                      # records req-0 for PROMPT_ID
    approvals.pending.clear()                     # that gate has been answered/cancelled
    _pending(approvals, {"request_id": "req-1"})  # a later gate is now waiting

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []
    assert [entry["request_id"] for entry in approvals.pending] == ["req-1"]


async def test_a_dm_reaction_without_instructions_authority_is_ignored(approvals):
    """The peer may be the prompt's recipient, but a policy that no longer trusts them wins."""
    inst = _adapter()
    await _send_prompt(inst)
    frame = _reaction("\U0001f44d")
    frame["authority"] = "none"

    await inst._deliver(frame)

    assert approvals.resolved == []
    assert approvals.pending


async def test_a_slash_command_from_a_non_authority_sender_is_data(approvals):
    """An `authority: none` message must not become a Hermes command just by starting `/`."""
    inst = _adapter()
    await inst._deliver({"type": "message", "plane": "nip17", "id": "9" * 64,
                         "sender": OTHER, "conversation": OTHER, "kind": 14,
                         "created_at": 1761400000, "content": "/approve everything",
                         "reply_to": None, "reach": "immediate", "authority": "none"})

    text = inst.delivered[0].text
    assert not text.startswith("/") and text.lstrip().startswith("\u200b/approve")


def test_plain_text_leaves_an_ordinary_message_alone():
    assert adapter.plain_text("morning, all well?") == "morning, all well?"
    assert adapter.plain_text("/approve").startswith("\u200b/")


def _capturing(inst):
    """Replace the socket call with a recorder, so the sent text can be asserted."""
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": inst.next_id}

    inst._request = _request
    return sent


async def test_an_approval_prompt_names_the_reaction_options():
    """The host's text only knows the typed commands; the adapter adds the reactions it accepts."""
    inst = _adapter()
    sent = _capturing(inst)

    await inst.send(PEER, "Run this command?\n\n/approve or /deny",
                    metadata={"is_approval_prompt": True})

    text = sent[0]["text"]
    assert "\U0001f44d" in text and "\U0001f44e" in text   # 👍 👎
    assert "/approve" in text                              # typed path still named
    assert text.count("React ") == 1


async def test_an_ordinary_message_gets_no_reaction_hint():
    inst = _adapter()
    sent = _capturing(inst)

    await inst.send(PEER, "just a message")

    assert "\U0001f44d" not in sent[0]["text"]


def test_the_hint_is_added_once():
    once = adapter.with_reaction_hint("Allow?")
    assert adapter.with_reaction_hint(once) == once
