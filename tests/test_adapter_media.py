"""P9: an attachment frame becomes a file the agent can actually open.

The gateway records the sender's `imeta` claims with the message and fetches nothing on
receipt; `media.get` is the one verified way to turn a claim into bytes. Hermes works in
local paths, so the adapter is the join: before `handle_message`, every fetchable blob is
fetched through the socket and cached for the agent — and a refusal is named in the text,
because an image the agent cannot see must not read as a message with nothing attached.

The socket is faked here; `tests/test_media.py` covers the gateway's half against a real
HTTP server, so what is under test is only the adapter's: a frame in, `media.get` out with
the digest and the message that referenced it, and a `MessageEvent` carrying the file.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections import deque
from pathlib import Path

import pytest

import adapter_harness as harness

adapter = harness.load_adapter("nostr_adapter_media")

PEER = "ab" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"
DIGEST = hashlib.sha256(PNG).hexdigest()


def _frame(**extra):
    return {"type": "message", "plane": "nip17", "id": "rumor-1", "sender": PEER,
            "conversation": PEER, "kind": 14, "created_at": 1761400000,
            "content": "look at this", "reach": "immediate",
            "authority": "instructions", **extra}


def _attachment(**overrides):
    entry = {"url": f"https://blossom.example/{DIGEST}", "sha256": DIGEST,
             "status": "available", "mime": "image/png", "size": len(PNG)}
    entry.update(overrides)
    return entry


def _adapter(tmp_path):
    """A bare adapter whose socket answers `media.get` from ``inst.media``."""
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._delivered_ids = deque(maxlen=512)
    inst._home_hex = ""
    inst._config = None
    inst.delivered = []
    inst.acked = []
    inst.requests = []
    inst.media = None  # a reply dict, or an exception to raise
    inst.blob = tmp_path / "blob"
    inst.blob.write_bytes(PNG)

    async def _request(payload, timeout=30.0):
        inst.requests.append(payload)
        if payload.get("op") == "media.get":
            if isinstance(inst.media, Exception):
                raise inst.media
            return inst.media
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
        return {"type": "ok"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


def _served(inst, **overrides):
    """The `media.get` reply the gateway sends for a verified blob already on disk."""
    reply = {"type": "media", "op": "media.get", "sha256": DIGEST, "path": str(inst.blob),
             "mime": "image/png", "size": len(PNG), "cached": False, "rumor_id": "rumor-1"}
    reply.update(overrides)
    return reply


def _gets(inst):
    return [payload for payload in inst.requests if payload.get("op") == "media.get"]


def test_an_image_attachment_reaches_the_event_as_a_local_file(tmp_path):
    inst = _adapter(tmp_path)
    inst.media = _served(inst)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    assert len(inst.delivered) == 1
    event = inst.delivered[0]
    assert event.text == "look at this", "a caption that arrives with the image is unchanged"
    assert event.message_type == harness.MessageType.PHOTO
    assert event.media_types == ["image/png"]
    assert len(event.media_urls) == 1
    assert Path(event.media_urls[0]).read_bytes() == PNG, "the agent gets the bytes, not a url"
    assert inst.acked == ["rumor-1"], "ack only once Hermes has the message, file and all"


def test_the_fetch_is_scoped_to_the_message_that_referenced_the_blob(tmp_path):
    """`media.get` is not a general fetcher: the request names the message, not just a hash."""
    inst = _adapter(tmp_path)
    inst.media = _served(inst)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    assert _gets(inst) == [{"op": "media.get", "sha256": DIGEST, "rumor_id": "rumor-1"}]


@pytest.mark.parametrize("mime, expected", [
    ("image/png", harness.MessageType.PHOTO),
    ("audio/ogg", harness.MessageType.AUDIO),
    ("video/mp4", harness.MessageType.VIDEO),
    ("application/pdf", harness.MessageType.DOCUMENT),
])
def test_the_message_type_follows_what_the_gateway_sniffed(tmp_path, mime, expected):
    inst = _adapter(tmp_path)
    inst.media = _served(inst, mime=mime)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    event = inst.delivered[0]
    assert event.media_types == [mime]
    assert event.message_type == expected


@pytest.mark.parametrize("code", ["too_large", "type_unknown", "unknown_blob", "media_disabled"])
def test_a_refusal_is_named_in_the_text_and_the_message_still_arrives(tmp_path, code):
    inst = _adapter(tmp_path)
    inst.media = {"type": "error", "op": "media.get", "code": code, "error": "refused"}

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    event = inst.delivered[0]
    assert event.media_urls == []
    assert event.text.startswith("look at this")
    assert f"[attachment not delivered: {code}]" in event.text
    assert inst.acked == ["rumor-1"], "a refused attachment must not hold up the ack"


def test_a_refused_attachment_is_not_an_empty_turn(tmp_path):
    """An image-only message whose image was refused still says so instead of arriving blank."""
    inst = _adapter(tmp_path)
    inst.media = {"type": "error", "op": "media.get", "code": "too_large", "error": "refused"}

    asyncio.run(inst._deliver(_frame(content="", attachments=[_attachment()])))

    assert inst.delivered[0].text == "[attachment not delivered: too_large]"


def test_hermes_own_cap_refuses_an_oversize_blob_before_it_is_read(tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "INBOUND_MEDIA_MAX_BYTES", len(PNG) - 1)
    inst = _adapter(tmp_path)
    inst.media = _served(inst)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    event = inst.delivered[0]
    assert event.media_urls == []
    assert "[attachment not delivered: too_large]" in event.text


def test_an_unverifiable_claim_is_never_fetched(tmp_path):
    inst = _adapter(tmp_path)
    inst.media = _served(inst)
    entry = {"url": "https://blossom.example/thing", "status": "unverifiable"}

    asyncio.run(inst._deliver(_frame(attachments=[entry])))

    assert _gets(inst) == [], "there is no hash to verify, so there is nothing to ask for"
    assert "[attachment not delivered: unverifiable]" in inst.delivered[0].text


def test_a_dead_socket_does_not_lose_the_message(tmp_path):
    inst = _adapter(tmp_path)
    inst.media = ConnectionError("gateway went away")

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    event = inst.delivered[0]
    assert event.text.startswith("look at this")
    assert "[attachment not delivered: gateway_unreachable]" in event.text
    assert event.media_urls == []
    assert inst.acked == ["rumor-1"]


def test_a_digest_frame_fetches_nothing(tmp_path, monkeypatch):
    """A digest batches many senders; an attachment in one is not worth a download."""
    monkeypatch.setenv("NOSTR_HOME_CHANNEL", PEER)
    inst = _adapter(tmp_path)
    inst.media = _served(inst)
    frame = _frame(plane="digest", sender="digest", conversation="digest",
                   authority="none", attachments=[_attachment()])

    asyncio.run(inst._deliver(frame))

    assert _gets(inst) == []
    assert inst.delivered
    assert getattr(inst.delivered[0], "media_urls", []) == []


def test_media_can_be_switched_off_with_an_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("NOSTR_MEDIA", "0")
    inst = _adapter(tmp_path)
    inst.media = _served(inst)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment()])))

    assert _gets(inst) == []
    event = inst.delivered[0]
    assert event.media_urls == []
    assert "[attachment not delivered: media_disabled]" in event.text, (
        "an opt-out still says an attachment was there: a blank turn is not an answer")


def test_a_repeated_digest_is_fetched_once_and_handed_over_once(tmp_path):
    inst = _adapter(tmp_path)
    inst.media = _served(inst)

    asyncio.run(inst._deliver(_frame(attachments=[_attachment(), _attachment()])))

    assert len(_gets(inst)) == 1
    assert len(inst.delivered[0].media_urls) == 1, "one blob must not cost two vision passes"


def test_a_fifth_attachment_is_refused_without_a_request(tmp_path):
    inst = _adapter(tmp_path)
    inst.media = _served(inst)
    entries = [_attachment(sha256=hashlib.sha256(bytes([index])).hexdigest())
               for index in range(5)]

    asyncio.run(inst._deliver(_frame(attachments=entries)))

    assert len(_gets(inst)) == 4, "the gateway's cap is four per message; the adapter's too"
    event = inst.delivered[0]
    assert len(event.media_urls) == 4
    assert event.text.count("[attachment not delivered: too_many_files]") == 1
