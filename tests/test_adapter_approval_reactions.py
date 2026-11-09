"""Approvals answered by a reaction: a 👍/👎 on the prompt resolves that gate.

Hermes hands a platform that renders approval prompts an ``ExecApprovalPrompt`` through
``BasePlatformAdapter.send_exec_approval`` -- the shared card body, the choices this gate
accepts, and (on hosts that correlate answers) the ``request_id`` naming this one gate. The
adapter renders it as text and remembers the message id against that request; a kind-7
reaction on the message is then an answer to exactly that gate, resolved through the public
``tools.approval.resolve_gateway_approval``. The typed ``/approve`` / ``/deny`` path stays the
host's own and is not re-implemented here.

``tools.approval`` is faked: the real queue belongs to the host process, and its own tests own
it. The fake keeps only the one call the adapter makes and records what it was asked to resolve.
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
    """`tools.approval` as the adapter uses it: one resolve, addressed by request id."""

    def __init__(self):
        self.pending: list[dict] = []
        self.resolved: list[tuple[str, str, str | None]] = []
        self.cards: list[tuple[str, str | None, str | None, object]] = []

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

    def resolve_gateway_approval_card(self, session_key: str, choice: str,
                                      request_id: str | None, *, actor_id: str | None = None,
                                      extra=None) -> int:
        self.cards.append((session_key, request_id, actor_id, extra))
        return self.resolve_gateway_approval(session_key, choice, request_id)


@pytest.fixture
def approvals(monkeypatch):
    """Stand in for the host's approval module inside the shared process."""
    fake = FakeApprovals()
    module = types.ModuleType("tools.approval")
    module.resolve_gateway_approval = fake.resolve_gateway_approval
    module.resolve_gateway_approval_card = fake.resolve_gateway_approval_card
    tools = sys.modules.get("tools") or types.ModuleType("tools")
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.approval", module)
    monkeypatch.setattr(tools, "approval", module, raising=False)
    return fake


@pytest.fixture
def approver(monkeypatch):
    """Stand in for `gateway.approval_actor`: the host's narrowed approver list."""
    state = {"refuse": None, "calls": []}
    module = types.ModuleType("gateway.approval_actor")

    def approval_refusal(session_key, actor_id, *, request_id=None, extra=None):
        state["calls"].append((session_key, actor_id, request_id, extra))
        return state["refuse"]

    module.approval_refusal = approval_refusal
    module.refusal_text = lambda requester: f"Only {requester} can answer this approval."
    gateway = sys.modules["gateway"]
    monkeypatch.setitem(sys.modules, "gateway.approval_actor", module)
    monkeypatch.setattr(gateway, "approval_actor", module, raising=False)
    return state


def _adapter():
    """A bare adapter with the host hooks the session-key fallback is built from."""
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


async def _send_prompt(inst, message_id: str = PROMPT_ID, chat_id: str = PEER, *,
                       session_key: str = SESSION, request_id: str | None = "req-1",
                       actions=None):
    """Deliver a prompt the way the host does: through the platform's approval hook."""
    inst.next_id = message_id
    return await inst._send_exec_approval_prompt(
        harness.ApprovalPrompt(chat_id, session_key, request_id=request_id, actions=actions))


def _reaction(content="\U0001f44d", target=PROMPT_ID, sender=PEER, rumor_id=REACTION_ID):
    """The frame the NIP-17 plane builds for an inner kind-7 rumor."""
    return {"type": "message", "plane": "nip17", "id": rumor_id, "sender": sender,
            "conversation": sender, "kind": 7, "created_at": 1761400000, "content": content,
            "reply_to": target, "reach": "immediate", "authority": "instructions"}


def _pending(fake, *entries):
    """Queue gates in the fake: one by default, or the ones named."""
    for position, entry in enumerate(entries or ({},)):
        fake.pending.append({"session_key": entry.get("session_key", SESSION),
                             "request_id": entry.get("request_id", f"req-{position}"),
                             "command": entry.get("command", "rm -rf /tmp/x")})


async def test_thumbs_up_resolves_the_prompt_it_names(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == [(SESSION, "once", "req-1")]
    assert inst.delivered == []          # a reaction is never an agent turn
    assert inst.acked == [REACTION_ID]   # ... and is acknowledged, so it cannot replay


async def test_thumbs_down_resolves_deny(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44e"))

    assert approvals.resolved == [(SESSION, "deny", "req-1")]
    assert inst.delivered == []


@pytest.mark.parametrize("emoji", ["+", "\u2705", "\u2764", "\u2764\ufe0f", " \U0001f44d "])
async def test_the_other_yes_reactions_approve_once(approvals, emoji):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction(emoji))

    assert approvals.resolved == [(SESSION, "once", "req-1")], emoji


async def test_a_reaction_on_a_different_message_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d", target="9" * 64))

    assert approvals.resolved == []
    assert approvals.pending, "the gate must still be waiting"
    assert inst.acked == [REACTION_ID]


async def test_a_reaction_from_another_author_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d", sender=OTHER))

    assert approvals.resolved == []
    # A stranger's reaction neither answers the gate nor destroys the prompt's mapping:
    # the recipient can still answer it afterwards.
    await inst._deliver(_reaction("\U0001f44d", rumor_id="f" * 63 + "1"))
    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_an_emoji_that_is_not_a_decision_is_ignored(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f525"))  # 🔥

    assert approvals.resolved == []
    await inst._deliver(_reaction("\U0001f44d", rumor_id="f" * 63 + "2"))
    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_a_reaction_with_no_prompt_resolves_nothing(approvals):
    inst = _adapter()
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []
    assert approvals.pending


async def test_one_reaction_answers_a_prompt_only_once(approvals):
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})
    await inst._deliver(_reaction("\U0001f44d"))
    assert approvals.resolved == [(SESSION, "once", "req-1")]

    # The next gate this session opens must not be approved by a stale tap on the last one.
    await _send_prompt(inst, message_id="a" * 64, request_id="req-later")
    _pending(approvals, {"request_id": "req-later"})
    await inst._deliver(_reaction("\U0001f44d", target=PROMPT_ID, rumor_id="f" * 63 + "3"))

    assert approvals.resolved == [(SESSION, "once", "req-1")]
    assert approvals.pending


async def test_two_waiting_approvals_answer_only_the_prompt_reacted_to(approvals):
    """A reaction names a message, and the request id makes that name the whole answer."""
    inst = _adapter()
    await _send_prompt(inst, message_id=PROMPT_ID, request_id="req-first")
    await _send_prompt(inst, message_id="b" * 64, request_id="req-second")
    _pending(approvals, {"request_id": "req-first"}, {"request_id": "req-second"})

    await inst._deliver(_reaction("\U0001f44d", target="b" * 64))

    assert approvals.resolved == [(SESSION, "once", "req-second")]
    assert [entry["request_id"] for entry in approvals.pending] == ["req-first"]


async def test_a_host_that_names_no_request_falls_back_to_the_hosts_own_fifo(approvals):
    """Some releases call `send_exec_approval` without a request id (their own buttons are
    FIFO). The prompt is still answerable, by the same rule the host's adapters use."""
    inst = _adapter()
    await _send_prompt(inst, request_id=None)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_no_session_key_means_no_resolve(approvals):
    """Without Hermes' key builder there is no key to resolve under — and no guess."""
    inst = _adapter()
    del inst.build_source
    del inst._source_session_key
    await _send_prompt(inst, session_key="")
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []


async def test_without_an_approval_module_nothing_is_resolved(monkeypatch):
    inst = _adapter()
    await _send_prompt(inst)
    monkeypatch.setitem(sys.modules, "tools.approval", None)  # import raises

    await inst._deliver(_reaction("\U0001f44d"))  # must not raise into the reader loop

    assert inst.delivered == []
    assert inst.acked == [REACTION_ID]


async def test_the_hook_remembers_the_prompt_by_its_message_and_request(approvals):
    inst = _adapter()
    result = await _send_prompt(inst, request_id="req-42")

    assert result.success
    assert inst._approval_prompts[PROMPT_ID] == (PEER, SESSION, "req-42")


async def test_a_prompt_sent_as_plain_text_is_not_answerable(approvals):
    """Only a prompt Hermes addressed to this adapter as a card carries a request id: a text
    ``is_approval_prompt`` message (a confirmation, or the host's own fallback) is not tracked,
    so a reaction on it cannot answer somebody else's gate."""
    inst = _adapter()
    inst.next_id = "m-1"
    await inst.send(PEER, "Approved.", metadata={"is_approval_prompt": True})

    assert list(inst._approval_prompts) == []


async def test_a_prompt_that_failed_to_send_is_not_answerable(approvals):
    inst = _adapter()

    async def _refused(payload, timeout=30.0):
        return {"type": "delivery", "ok": False, "errors": {"wss://relay.example": "closed"}}

    inst._request = _refused
    result = await inst._send_exec_approval_prompt(harness.ApprovalPrompt(PEER, SESSION))

    assert not result.success
    assert list(inst._approval_prompts) == []


async def test_only_the_newest_prompts_are_remembered(approvals):
    inst = _adapter()
    ids = [f"{n:064x}" for n in range(1, adapter.MAX_TRACKED_APPROVALS + 6)]
    for message_id in ids:
        await _send_prompt(inst, message_id=message_id, request_id=f"req-{message_id[:6]}")

    assert len(inst._approval_prompts) == adapter.MAX_TRACKED_APPROVALS
    assert ids[-1] in inst._approval_prompts
    assert ids[0] not in inst._approval_prompts

    _pending(approvals, {"request_id": f"req-{ids[-1][:6]}"})
    await inst._deliver(_reaction("\U0001f44d", target=ids[0]))
    assert approvals.resolved == []      # too old to accept a reaction
    await inst._deliver(_reaction("\U0001f44d", target=ids[-1], rumor_id="f" * 63 + "4"))
    assert approvals.resolved == [(SESSION, "once", f"req-{ids[-1][:6]}")]


async def test_the_documented_reaction_frame_is_accepted_too(approvals):
    """Spec §5's `reaction` frame carries the same decision as a kind-7 message frame."""
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver({"type": "reaction", "plane": "nip17", "id": REACTION_ID,
                         "target_rumor_id": PROMPT_ID, "from": PEER,
                         "emoji": "\U0001f44d", "created_at": 1761400001,
                         "authority": "instructions"})

    assert approvals.resolved == [(SESSION, "once", "req-1")]
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
    inst._peers.add(CHANNEL)
    await _send_prompt(inst, chat_id=CHANNEL, session_key=SESSION)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d", sender=PEER))

    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_a_channel_reaction_from_an_authorityless_author_is_ignored(approvals):
    inst = _adapter()
    inst._peers.add(CHANNEL)
    await _send_prompt(inst, chat_id=CHANNEL, session_key=SESSION)
    _pending(approvals, {"request_id": "req-1"})
    frame = _reaction("\U0001f44d", sender=OTHER)
    frame["authority"] = "none"

    await inst._deliver(frame)

    assert approvals.resolved == []
    # Still answerable by someone who is allowed to instruct there.
    await inst._deliver(_reaction("\U0001f44d", sender=PEER, rumor_id="f" * 63 + "2"))
    assert approvals.resolved == [(SESSION, "once", "req-1")]


def _capturing(inst):
    """Replace the socket call with a recorder, so the sent text can be asserted."""
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": inst.next_id}

    inst._request = _request
    return sent


async def test_the_prompt_text_names_the_choices_the_gate_offers():
    """Hermes' card body carries the command, the reason and the deadline; naming the answers
    is the text surface's job, and a choice the gate refuses must not be offered."""
    inst = _adapter()
    sent = _capturing(inst)

    await inst._send_exec_approval_prompt(
        harness.ApprovalPrompt(PEER, SESSION, text="⚠️ Approval required\n\n```rm -rf /tmp/x```"))

    text = sent[0]["text"]
    assert "rm -rf /tmp/x" in text                     # the host's own body survives
    assert "\U0001f44d" in text and "\U0001f44e" in text   # 👍 👎
    for command in ("/approve once", "/approve session", "/approve always", "/deny"):
        assert command in text
    assert text.count("React ") == 1


async def test_a_smart_deny_prompt_offers_only_what_it_accepts():
    inst = _adapter()
    sent = _capturing(inst)

    await inst._send_exec_approval_prompt(harness.ApprovalPrompt(
        PEER, SESSION, text="Blocked.",
        actions=[("Approve once", "once", "primary"), ("Deny", "deny", "danger")]))

    text = sent[0]["text"]
    assert "/approve once" in text and "/deny" in text
    assert "/approve session" not in text and "/approve always" not in text


async def test_an_ordinary_message_gets_no_reaction_hint():
    inst = _adapter()
    sent = _capturing(inst)

    await inst.send(PEER, "just a message")

    assert "\U0001f44d" not in sent[0]["text"]


def test_a_deny_only_card_offers_no_approve_reaction():
    hint = adapter.approval_hint(["deny"])
    assert "\U0001f44d" not in hint and "\U0001f44e" in hint
    assert "/deny" in hint and "/approve" not in hint


async def test_a_reaction_the_approver_list_refuses_is_left_pending(approvals, approver):
    """A narrowed `approvals.approvers` names who may answer: anyone else's 👍 is told why,
    and the gate stays waiting for the person it names."""
    inst = _adapter()
    inst._peers.add(CHANNEL)
    sent = _capturing(inst)
    await _send_prompt(inst, chat_id=CHANNEL, session_key=SESSION)
    _pending(approvals, {"request_id": "req-1"})
    approver["refuse"] = "Nathan"

    await inst._deliver(_reaction("\U0001f44d", sender=PEER))

    assert approvals.resolved == [] and approvals.pending
    assert approvals.cards == []                       # refused before anything resolves
    assert any("Only Nathan can answer this approval." in str(p.get("text") or "") for p in sent)
    assert inst.delivered == []                        # refused, not replayed as chat

    approver["refuse"] = None                          # the person it names can still answer
    await inst._deliver(_reaction("\U0001f44d", sender=PEER, rumor_id="f" * 63 + "5"))
    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_an_allowed_reaction_resolves_through_the_card_api_with_the_actor(
        approvals, approver):
    """The host re-checks the clicker when it settles, so the reaction must name them."""
    inst = _adapter()
    extra = {"allow_admin_from": [PEER]}
    inst._config = types.SimpleNamespace(extra=extra)
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.cards == [(SESSION, "req-1", PEER, extra)]
    assert approvals.resolved == [(SESSION, "once", "req-1")]


async def test_an_approver_check_that_cannot_run_refuses(approvals, approver, monkeypatch):
    """A gate is never resolved on a question the adapter could not ask."""
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})

    def boom(*_args, **_kwargs):
        raise RuntimeError("secret scope gone")

    monkeypatch.setattr(sys.modules["gateway.approval_actor"], "approval_refusal", boom)

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == [] and approvals.pending
    assert approvals.cards == []


def test_nak_decode_runs_with_path_only(monkeypatch):
    """`nak` is a third-party child: it gets PATH, never the gateway's environment."""
    import os
    import subprocess

    seen = {}

    class Done:
        stdout = "ab" * 32

    def run(argv, **kwargs):
        seen["argv"] = argv
        seen["env"] = kwargs.get("env")
        return Done()

    monkeypatch.setenv("NOSTR_HOME_CHANNEL", "npub1example")
    monkeypatch.setattr(subprocess, "run", run)
    inst = _adapter()

    assert inst._home_chat() == "ab" * 32
    assert seen["argv"] == ["nak", "decode", "npub1example"]
    assert seen["env"] == {"PATH": os.environ.get("PATH", "")}


async def test_a_prompt_is_bound_to_its_own_request_id(approvals):
    """A late reaction must not approve whatever gate is open by then."""
    inst = _adapter()
    await _send_prompt(inst)                       # records req-1 for PROMPT_ID
    approvals.pending.clear()                      # that gate has been answered/cancelled
    _pending(approvals, {"request_id": "req-later"})

    await inst._deliver(_reaction("\U0001f44d"))

    assert approvals.resolved == []
    assert [entry["request_id"] for entry in approvals.pending] == ["req-later"]


async def test_a_dm_reaction_without_instructions_authority_is_ignored(approvals):
    """The peer may be the prompt's recipient, but a policy that no longer trusts them wins."""
    inst = _adapter()
    await _send_prompt(inst)
    _pending(approvals, {"request_id": "req-1"})
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
