"""`history` scoped to one conversation and to the page ending before a trigger.

An unscoped `history` is the operator's store view: newest first, every plane. The
adapter needs the other shape — a room's own lines that end *before* the message it is
answering — and the anchor has to be exact, or the agent reads the trigger twice or
reads messages it has not been sent yet. The echo on the reply is part of the contract:
it is how a newer adapter can tell a scoped answer from an older daemon's unscoped one.
"""

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.store import Inbound

COMMUNITY = "ab" * 32
CHANNEL = "cd" * 32
ROOM = f"concord:{COMMUNITY}:{CHANNEL}"
OTHER_ROOM = f"concord:{COMMUNITY}:{'ee' * 32}"
SENDER = "11" * 32
ANCHOR = "a0" * 32
ANCHOR_AT = 1_700_000_400


def _gateway(tmp_path) -> Gateway:
    keys = sdk.Keys.generate()
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps({"secret_key_hex": keys.secret_key().to_hex()}))
    return Gateway({
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
    })


@pytest.fixture
def no_nak(monkeypatch):
    # Contact-list discovery shells out to nak; this file tests one read op.
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


async def call(path, frame: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 3))
    finally:
        writer.close()
        await writer.wait_closed()


def _add(store, ident: str, *, at: int, content: str, conversation: str = ROOM,
         authority: str = "instructions") -> None:
    assert store.add(Inbound(
        id=ident, wrapper_id=ident, sender=SENDER, kind=9, created_at=at,
        content=content, conversation=conversation, reply_to=None, plane="concord",
        reach="immediate", authority=authority, rule="room", why="mention in the room",
    ))


@pytest.fixture
def room(tmp_path, no_nak):
    gateway = _gateway(tmp_path)
    _add(gateway.store, "b1" * 32, at=ANCHOR_AT - 300, content="first")
    _add(gateway.store, "b2" * 32, at=ANCHOR_AT - 200, content="second")
    _add(gateway.store, "b3" * 32, at=ANCHOR_AT - 100, content="third")
    # Another room, interleaved in time: a page must never mix conversations.
    _add(gateway.store, "c1" * 32, at=ANCHOR_AT - 150, content="other room",
         conversation=OTHER_ROOM)
    # The trigger and a later message, both stored and both outside the page.
    _add(gateway.store, ANCHOR, at=ANCHOR_AT, content="Holly, what now?")
    _add(gateway.store, "d1" * 32, at=ANCHOR_AT + 100, content="after the trigger")
    return gateway


async def test_the_page_excludes_the_anchor_and_every_later_row(room):
    await room.start()
    try:
        reply = await call(room.socket_path, {
            "op": "history", "conversation": ROOM, "before_at": ANCHOR_AT,
            "before_id": ANCHOR, "limit": 10,
        })
    finally:
        await room.stop()

    assert reply["type"] == "history" and reply["scoped"] is True
    assert reply["conversation"] == ROOM
    assert reply["before_at"] == ANCHOR_AT and reply["before_id"] == ANCHOR
    assert [row["content"] for row in reply["messages"]] == ["first", "second", "third"]
    assert [row["id"] for row in reply["messages"]] == ["b1" * 32, "b2" * 32, "b3" * 32]
    assert reply["messages"][0]["created_at"] < reply["messages"][-1]["created_at"], (
        "oldest first: the adapter renders the page in the order it reads it"
    )
    ids = {row["id"] for row in reply["messages"]}
    assert ANCHOR not in ids and "d1" * 32 not in ids
    assert all(row["sender"] == SENDER and row["authority"] == "instructions"
               and "received_at" in row for row in reply["messages"])


async def test_limit_bounds_the_page_from_the_anchor_backwards(room):
    await room.start()
    try:
        reply = await call(room.socket_path, {
            "op": "history", "conversation": ROOM, "before_at": ANCHOR_AT,
            "before_id": ANCHOR, "limit": 2,
        })
    finally:
        await room.stop()

    assert [row["content"] for row in reply["messages"]] == ["second", "third"], (
        "the rows just before the trigger, not the room's opening"
    )


async def test_a_row_in_the_same_second_is_ordered_by_id_and_cannot_readmit_the_anchor(
        room):
    """`(created_at, id)` is the anchor: a same-second tie is resolved, not guessed."""
    _add(room.store, "9f" * 32, at=ANCHOR_AT, content="same second, smaller id")
    _add(room.store, "f0" * 32, at=ANCHOR_AT, content="same second, larger id")
    await room.start()
    try:
        reply = await call(room.socket_path, {
            "op": "history", "conversation": ROOM, "before_at": ANCHOR_AT,
            "before_id": ANCHOR, "limit": 10,
        })
    finally:
        await room.stop()

    contents = [row["content"] for row in reply["messages"]]
    assert contents[-1] == "same second, smaller id", "sorted before the anchor by id"
    assert "same second, larger id" not in contents
    assert "Holly, what now?" not in contents


async def test_the_unscoped_op_is_unchanged(room):
    await room.start()
    try:
        reply = await call(room.socket_path, {"op": "history", "limit": 5})
    finally:
        await room.stop()

    assert reply["type"] == "history" and "scoped" not in reply
    assert len(reply["messages"]) == 5, "the operator's view is still newest-first, capped"
    assert reply["messages"][0]["content"] == "after the trigger"
    assert reply["messages"][0]["plane"] == "concord"
