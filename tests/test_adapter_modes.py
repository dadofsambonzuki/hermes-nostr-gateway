"""The adapter's half of `/queue` and `/steer`: re-address the words to Hermes' own commands.

The gateway has already decided that the sender may ask, and stripped the prefix; the text
that arrives is only the words. Hermes owns the holding, so this adapter maps the frame's
`mode` onto the two commands Hermes already implements — `/queue` for "next turn", `/steer`
for "into the turn that is running" — and never inspects the text itself. Both commands fall
back to a plain turn when nothing is running, which is what a `/steer` with no turn to steer
is supposed to do; that behaviour lives in Hermes, so what is pinned here is the mapping.
"""

import asyncio
from collections import deque

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_modes")

PEER = "ab" * 32
OWNER = "c4f5e7a75a8ce3683d529cff06368439c529e5243c6b125ba68789198856cac7"


def _frame(**overrides):
    frame = {
        "type": "message",
        "plane": "nip17",
        "id": "rumor-0123456789abcdef",
        "sender": PEER,
        "conversation": PEER,
        "kind": 14,
        "created_at": 1761400000,
        "content": "read the log",
        "reply_to": None,
        "reach": "immediate",
        "mode": "normal",
        "authority": "instructions",
        "why": "matched family-dms",
    }
    frame.update(overrides)
    return frame


def _bare_adapter():
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._home_hex = ""
    inst._config = None
    inst._delivered_ids = deque(maxlen=512)
    inst.acked = []
    inst.delivered = []
    inst.sent = []

    async def _request(payload, timeout=30.0):
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
        elif payload.get("op") == "send":
            inst.sent.append(payload)
        return {"type": "ok"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


def _deliver(frame, monkeypatch):
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    inst = _bare_adapter()
    asyncio.run(inst._deliver(frame))
    assert len(inst.delivered) == 1, "the message must still reach the agent"
    return inst, inst.delivered[0]


def test_a_queued_message_is_readdressed_to_hermes_queue(monkeypatch):
    inst, event = _deliver(_frame(mode="queue"), monkeypatch)

    assert event.text == "/queue read the log"
    assert event.raw_message["mode"] == "queue"
    assert event.source.role_authorized is True
    assert inst.acked == [_frame()["id"]]


def test_a_steer_is_readdressed_to_hermes_steer(monkeypatch):
    _, event = _deliver(_frame(mode="steer", content="stop, wrong file"), monkeypatch)

    assert event.text == "/steer stop, wrong file"


def test_a_frame_from_a_gateway_a_release_behind_still_steers(monkeypatch):
    """`guide` was the gateway's old name for steer: an upgrade on one side must not lose it."""
    _, event = _deliver(_frame(mode="guide", content="stop, wrong file"), monkeypatch)

    assert event.text == "/steer stop, wrong file"


def test_a_normal_message_is_delivered_as_written(monkeypatch):
    _, event = _deliver(_frame(mode="normal"), monkeypatch)

    assert event.text == "read the log"


def test_a_frame_without_a_mode_is_delivered_as_written(monkeypatch):
    """An older gateway, or a plane that never sets one: nothing is invented."""
    frame = _frame()
    del frame["mode"]

    _, event = _deliver(frame, monkeypatch)

    assert event.text == "read the log"


def test_an_unknown_mode_is_not_a_command(monkeypatch):
    _, event = _deliver(_frame(mode="shout"), monkeypatch)

    assert event.text == "read the log"


def test_the_command_stays_first_so_an_attachment_note_does_not_hide_it(monkeypatch):
    """A queued photo keeps its command first, so Hermes reads it as a command."""
    monkeypatch.setenv("NOSTR_MEDIA", "0")
    _, event = _deliver(_frame(mode="queue", content="look at this", attachments=[{
        "url": "https://blossom.example/" + "a" * 64, "sha256": "a" * 64,
        "mime": "image/jpeg", "status": "available"}]), monkeypatch)

    assert event.text.splitlines()[0] == "/queue look at this"
    assert event.text.endswith("[attachment not delivered: media_disabled]")


def test_a_mode_with_no_words_is_not_a_bare_command(monkeypatch):
    """The gateway never sends this, but a bare `/queue` would be answered with usage."""
    monkeypatch.setenv("NOSTR_MEDIA", "0")
    _, event = _deliver(_frame(mode="queue", content="", attachments=[{
        "url": "https://blossom.example/" + "a" * 64, "sha256": "a" * 64,
        "mime": "image/jpeg", "status": "available"}]), monkeypatch)

    assert event.text == "[attachment not delivered: media_disabled]"


def test_a_digest_batch_is_never_readdressed(monkeypatch):
    """A digest is data about many senders: a stray mode must not turn it into a command."""
    monkeypatch.setenv("NOSTR_HOME_CHANNEL", OWNER)
    inst = _bare_adapter()

    asyncio.run(inst._deliver(_frame(plane="digest", sender="digest", conversation="digest",
                                     mode="queue", content="3 item(s) since the last digest")))

    event = inst.delivered[0]
    assert event.text == "3 item(s) since the last digest"
    assert event.source.role_authorized is False
