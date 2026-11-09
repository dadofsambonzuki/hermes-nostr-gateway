"""A gated Concord turn carries a bounded page of the room it is answering.

A message in a busy channel used to reach the agent as the trigger alone, so the reply
answered a discussion it had never seen. Hermes' `channel_context` is the host's
user-role slot for exactly that material: it is prepended to the trigger and never
becomes a second trigger. These tests pin the adapter's half — which turns get the page,
what the page looks like, and that every way the lookup can fail still delivers the
message.

The daemon's half lives in `tests/test_history_scoped.py` (the store query) and
`tests/test_socket.py` (the op's echo, which is what an older daemon fails).
"""

import asyncio
import logging
import types
from collections import deque

import pytest

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_channel_context")

COMMUNITY = "ab" * 32
CHANNEL = "cd" * 32
ROOM = f"concord:{COMMUNITY}:{CHANNEL}"
PEER = "11" * 32
OTHER = "22" * 32
BOT = "33" * 32
TRIGGER_ID = "e1" * 32
TRIGGER_AT = 1_700_100_000


def _row(ident: str, sender: str, content: str, *, at: int,
         authority: str = "instructions") -> dict:
    return {"id": ident, "sender": sender, "content": content, "authority": authority,
            "created_at": at, "received_at": at, "plane": "concord",
            "conversation": ROOM, "route": None}


def _page():
    return [
        _row("a1" * 32, PEER, "the deploy is red", at=TRIGGER_AT - 600),
        _row("a2" * 32, OTHER, "it is the\nmigration step", at=TRIGGER_AT - 300),
        _row("a3" * 32, BOT, "I am looking at the logs", at=TRIGGER_AT - 60,
             authority="self"),
    ]


def _frame(**overrides) -> dict:
    frame = {
        "type": "message", "plane": "concord", "id": TRIGGER_ID, "sender": PEER,
        "conversation": ROOM, "kind": 9, "created_at": TRIGGER_AT,
        "content": "Holly, what should we do?", "reach": "immediate",
        "authority": "instructions", "why": "mention in #projects",
    }
    frame.update(overrides)
    return frame


def _config(**context):
    extra = {"channel_context": context} if context else {}
    return types.SimpleNamespace(extra=extra)


def _bare_adapter(*, rows=None, error=None, config=None, reply=None):
    """The adapter's delivery half, with the socket replaced by a recording double."""
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._socket = "/tmp/does-not-matter.sock"
    inst._peers = set()
    inst._home_hex = ""
    inst._config = config
    inst._routes = {}
    inst._chat_profiles = {}
    inst._approval_prompts = {}
    inst._delivered_ids = deque(maxlen=512)
    inst._channel_context_warned = False
    inst.requests = []
    inst.delivered = []
    inst.acked = []
    inst.sent = []

    async def _request(payload, timeout=30.0):
        inst.requests.append(payload)
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
            return {"type": "ok"}
        if payload.get("op") == "history":
            if error is not None:
                raise error
            if reply is not None:
                return reply
            return {"type": "history", "scoped": True,
                    "conversation": payload["conversation"],
                    "before_at": payload["before_at"], "before_id": payload["before_id"],
                    "messages": _page() if rows is None else rows}
        return {"type": "ok"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


def _history_requests(inst):
    return [payload for payload in inst.requests if payload.get("op") == "history"]


def test_a_concord_trigger_carries_the_rooms_preceding_lines():
    inst = _bare_adapter()

    asyncio.run(inst._deliver(_frame()))

    assert len(inst.delivered) == 1
    event = inst.delivered[0]
    # The trigger stays the message; the page is context, not the message.
    assert event.text == "Holly, what should we do?"
    context = event.channel_context
    assert context.startswith(adapter.CHANNEL_CONTEXT_HEADER)
    lines = context.splitlines()[1:]
    assert "the deploy is red" in lines[0]
    assert "it is the migration step" in lines[1], "a stored newline must be flattened"
    assert lines[2].startswith(f"[bot] {BOT[:12]}"), "our own lines are marked as the bot's"
    assert lines[0].index(PEER[:12]) == 0
    assert context.index("deploy is red") < context.index("migration step")
    assert inst.acked == [TRIGGER_ID]

    (lookup,) = _history_requests(inst)
    assert lookup == {"op": "history", "conversation": ROOM,
                      "before_at": TRIGGER_AT, "before_id": TRIGGER_ID, "limit": 20}


def test_a_dm_turn_gets_no_channel_context():
    """A DM's history is scrollback, not a room: no lookup, no context, same delivery."""
    inst = _bare_adapter()

    asyncio.run(inst._deliver(_frame(conversation=PEER, why="dm")))

    assert len(inst.delivered) == 1
    assert inst.delivered[0].channel_context is None
    assert _history_requests(inst) == [], "a DM must not ask the daemon for room history"


def test_a_slash_command_trigger_gets_the_context():
    """A command frame is already through the gate; it is not re-decided here.

    The daemon delivers only what its policy admitted: a mention, a solo room, or a
    message its rule allows. The adapter adds context to any such turn and never
    second-guesses the gate.
    """
    inst = _bare_adapter()

    asyncio.run(inst._deliver(_frame(content="/status anything red?",
                                     why=f"{CHANNEL[:8]}… has 9 member(s): mention required")))

    assert len(inst.delivered) == 1
    assert inst.delivered[0].channel_context is not None
    assert inst.delivered[0].text == "/status anything red?"


def test_a_failed_lookup_still_delivers_the_trigger():
    inst = _bare_adapter(error=OSError("daemon socket is gone"))

    assert asyncio.run(inst._deliver(_frame())) is None

    assert len(inst.delivered) == 1
    assert inst.delivered[0].channel_context is None
    assert inst.acked == [TRIGGER_ID], "the turn ran, so the frame is still acked"


def test_an_older_daemon_degrades_to_no_context_with_one_warning(caplog):
    """A daemon that ignores `conversation` must not be read as a window of this room."""
    elsewhere = [dict(_row("b1" * 32, OTHER, "another room entirely", at=TRIGGER_AT - 30),
                      conversation=f"concord:{COMMUNITY}:{'ee' * 32}")]
    inst = _bare_adapter(reply={"type": "history", "messages": elsewhere})

    with caplog.at_level(logging.WARNING, logger=adapter.logger.name):
        asyncio.run(inst._deliver(_frame()))
        asyncio.run(inst._deliver(_frame(id="e2" * 32, created_at=TRIGGER_AT + 10)))

    assert [event.channel_context for event in inst.delivered] == [None, None]
    assert len(inst.acked) == 2, "both triggers were still delivered and acked"
    warnings = [record for record in caplog.records
                if record.levelno == logging.WARNING
                and "channel_context is unavailable" in record.getMessage()]
    assert len(warnings) == 1, "the compatibility warning is said once, not per turn"


def test_an_error_reply_still_delivers_the_trigger():
    inst = _bare_adapter(reply={"type": "error", "op": "history", "code": "unsupported",
                                "error": "history is not available on this daemon"})

    asyncio.run(inst._deliver(_frame()))

    assert len(inst.delivered) == 1
    assert inst.delivered[0].channel_context is None
    assert inst.acked == [TRIGGER_ID]


def test_the_caps_bound_lines_and_characters():
    """Fifty stored lines yield at most max_messages lines and max_chars characters."""
    rows = [_row(f"{index:02d}" * 16, PEER, f"message number {index} " + "x" * 40,
                 at=TRIGGER_AT - 1000 + index) for index in range(50)]
    inst = _bare_adapter(rows=rows,
                         config=_config(max_messages=5, max_chars=300))

    asyncio.run(inst._deliver(_frame()))

    context = inst.delivered[0].channel_context
    assert context is not None
    lines = context.splitlines()
    assert len(lines) <= 1 + 5, "header plus at most max_messages lines"
    assert len(context) <= 300, "the character cap bounds the whole block"
    assert "message number 49" in context, "the newest line is the one kept"
    assert "message number 0" not in context, "oldest lines are dropped first"


def test_only_rows_inside_the_age_window_are_used():
    rows = [
        _row("f1" * 32, PEER, "stale argument from last night", at=TRIGGER_AT - 7201),
        _row("f2" * 32, OTHER, "fresh context", at=TRIGGER_AT - 7200),
    ]
    inst = _bare_adapter(rows=rows, config=_config(max_age_secs=7200))

    asyncio.run(inst._deliver(_frame()))

    context = inst.delivered[0].channel_context
    assert "fresh context" in context
    assert "stale argument" not in context


def test_an_empty_page_is_not_context_and_is_not_a_failure():
    inst = _bare_adapter(rows=[])

    asyncio.run(inst._deliver(_frame()))

    assert inst.delivered[0].channel_context is None
    assert inst.acked == [TRIGGER_ID]


def test_the_feature_can_be_switched_off():
    inst = _bare_adapter(config=_config(enabled=False))

    asyncio.run(inst._deliver(_frame()))

    assert inst.delivered[0].channel_context is None
    assert _history_requests(inst) == []


def test_a_bare_false_setting_is_the_off_switch():
    inst = _bare_adapter(config=types.SimpleNamespace(extra={"channel_context": False}))

    asyncio.run(inst._deliver(_frame()))

    assert inst.delivered[0].channel_context is None
    assert _history_requests(inst) == []


@pytest.mark.parametrize("bad", [None, "", "not-a-number"])
def test_the_cap_parser_falls_back_rather_than_refusing_turns(bad):
    settings = adapter.channel_context_settings(
        types.SimpleNamespace(extra={"channel_context": {"max_messages": bad}}))
    assert settings.max_messages == adapter.DEFAULT_CHANNEL_CONTEXT_MESSAGES
