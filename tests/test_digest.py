"""Digest batching: many stored items, one frame, and only once."""

import pytest

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.store import Inbound, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "d.db")


def add(store, i, age=0):
    import time
    msg = Inbound(id=f"r{i}", wrapper_id=f"w{i}", sender="aa" * 32, kind=1,
                  created_at=int(time.time()) - age, content=f"note {i}",
                  conversation="feed", reply_to=None, plane="nip01", reach="digest",
                  authority="none", rule="watch", why="feed:watch")
    store.add(msg)
    if age:
        store.db.execute("UPDATE rumors SET received_at = received_at - ? WHERE id = ?", (age, f"r{i}"))


def test_only_items_past_the_window_are_digested(store):
    add(store, 1, age=1200)   # 20 min old
    add(store, 2, age=10)     # too fresh
    rows = store.pending_digest(900, 20)
    assert [r["id"] for r in rows] == ["r1"]


def test_digest_frame_is_one_message_carrying_many_items(store):
    for i in range(3):
        add(store, i, age=1200)
    rows = store.pending_digest(900, 20)
    frame = Gateway._digest_frame(None, rows)
    assert frame["type"] == "message" and frame["plane"] == "digest"
    assert frame["authority"] == "none"
    assert "3 item(s)" in frame["content"]
    assert frame["content"].count("- ") == 3


def test_delivered_items_are_not_digested_twice(store):
    add(store, 1, age=1200)
    rows = store.pending_digest(900, 20)
    store.mark_delivered([r["id"] for r in rows])
    assert store.pending_digest(900, 20) == []
