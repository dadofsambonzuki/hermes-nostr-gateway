"""A replay is shaped before delivery: superseded rows are context, not turns.

The daemon owns the shape because the daemon owns the store. `Store.replay_batch`
takes the newest rows inside the replay window, asks `shape_replay` which of them
should become a turn, and writes the `superseded` decision onto the row so a failed ack
cannot turn old context into a late, out-of-order turn. `Gateway.replay_pending` carries
the flag on the frame; the adapter acks it without calling `handle_message`.

These tests are the daemon half. The adapter's half (a superseded frame is acked and
never becomes a turn) lives beside the reader-loop harness in
`test_adapter_pending_replay.py`.
"""

from __future__ import annotations

import json
import logging
import time

import nostr_sdk as sdk

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.store import Inbound, Store, is_approval_reply, shape_replay


def _store(tmp_path, name: str) -> Store:
    """A store with no handover floor: these tests date their own rows."""
    store = Store(tmp_path / name)
    store.set_replay_floor(0)
    return store


def _add(store: Store, ident: str, *, conversation: str = "cc" * 32,
         sender: str = "cc" * 32, content: str | None = None, plane: str = "nip17",
         kind: int = 14, received_at: int | None = None,
         attachments: list[dict] | None = None, reach: str = "immediate",
         authority: str = "instructions") -> None:
    now = int(time.time())
    assert store.add(Inbound(
        id=ident, wrapper_id="w-" + ident, sender=sender, kind=kind,
        created_at=now, content=content or f"message {ident}",
        conversation=conversation, reply_to=None, plane=plane, reach=reach,
        authority=authority, rule="t", why="t", attachments=attachments,
    ))
    if received_at is not None:
        store.db.execute("UPDATE rumors SET received_at = ? WHERE id = ?",
                         (received_at, ident))


def _split(rows: list[dict]) -> tuple[list[str], list[str]]:
    """The batch as (context ids, turn ids), preserving the replay's order."""
    return ([r["id"] for r in rows if r.get("superseded")],
            [r["id"] for r in rows if not r.get("superseded")])


# -- the shape ------------------------------------------------------------

def test_three_pending_rows_in_one_conversation_are_one_turn_and_two_context_rows(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "three.db") as store:
        _add(store, "one", received_at=now - 3)
        _add(store, "two", received_at=now - 2)
        _add(store, "three", received_at=now - 1)

        rows = store.replay_batch(now=now)
        assert [r["id"] for r in rows] == ["one", "two", "three"], (
            "a replay still arrives oldest-first, whatever shaping decides"
        )
        superseded, turns = _split(rows)
        assert superseded == ["one", "two"]
        assert turns == ["three"]
        assert rows[0]["superseded_by"] == "three"
        assert rows[1]["superseded_by"] == "three"
        # The decision is on the row, not only in this batch's memory.
        assert store.rumor("one")["superseded_by"] == "three"

        # An ack is what makes a row terminal, and the reason is what says it ended
        # without a turn. The text is still history, and a superseded row is not a drop.
        store.mark_delivered(["one", "two"], reason="superseded")
        assert [r["id"] for r in store.pending_immediate()] == ["three"]
        history = {r["id"]: r for r in store.recent(5)}
        assert history["one"]["content"] == "message one"
        assert history["one"]["settled_reason"] == "superseded"
        assert history["one"]["delivered_at"] is not None
        assert store.drops()["total"] == 0, (
            "a superseded message was accepted: `drops` is refusals, not context"
        )


def test_an_ack_reason_outside_the_closed_vocabulary_still_delivers(tmp_path, caplog):
    """The store decides against an unknown reason: delivered, not refused.

    `server.py` forwards `reason` only when the key is present and `Gateway.ack` passes it
    through unread, so an unknown string reaches `mark_delivered` intact. The vocabulary stays
    closed — the row is delivered with no `settled_reason`, and so is not a `drop` — but the
    discard is named at DEBUG, with the ids and the string it refused, instead of vanishing.
    """
    with _store(tmp_path, "unknown-reason.db") as store:
        _add(store, "one")
        with caplog.at_level(logging.DEBUG, logger="hng.store"):
            store.mark_delivered(["one"], reason="nonsense")

        row = store.rumor("one")
        assert row is not None
        assert row["delivered_at"] is not None, "an unknown reason is not a refusal"
        assert row["settled_reason"] is None, (
            "settled_reason is a closed vocabulary: an unknown string must not label the row"
        )
        assert store.drops()["total"] == 0, (
            "the row was delivered: `drops` lists refusals, not acks we chose not to name"
        )
        assert "nonsense" in caplog.text and "one" in caplog.text, (
            "the discarded reason is visible at DEBUG rather than silently dropped"
        )

        # And the line is conditional: a plain ack carries no reason to report.
        caplog.clear()
        _add(store, "two")
        with caplog.at_level(logging.DEBUG, logger="hng.store"):
            store.mark_delivered(["two"])
        assert "not a settled reason" not in caplog.text


def test_a_shaped_row_stays_context_when_the_newer_row_is_acked_first(tmp_path):
    """The failure this pins: a lost ack must not resurrect old text as a late turn."""
    now = int(time.time())
    with _store(tmp_path, "ack-order.db") as store:
        _add(store, "one", received_at=now - 2)
        _add(store, "two", received_at=now - 1)
        store.replay_batch(now=now)

        # The newer row is acked; the older row's ack failed. A reconnect must not
        # decide the older row is now the newest in its conversation.
        store.mark_delivered(["two"])
        remaining = store.replay_batch(now=now)
        assert [r["id"] for r in remaining] == ["one"]
        assert remaining[0]["superseded"] is True
        assert remaining[0]["superseded_by"] == "two"


def test_the_age_horizon_files_a_stale_message_as_context(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "age.db") as store:
        _add(store, "stale", conversation="aa" * 32, received_at=now - 3_600)
        _add(store, "fresh", conversation="bb" * 32, received_at=now - 30)

        rows = store.replay_batch(now=now)
        assert [(r["id"], bool(r.get("superseded"))) for r in rows] == [
            ("stale", True), ("fresh", False)]

    # A horizon of 0 removes the bound; the outer replay window still applies. A fresh
    # store, because the first call wrote the decision onto the row — a shaped row is not
    # made a turn again by a config change.
    with _store(tmp_path, "age-off.db") as store:
        _add(store, "stale", conversation="aa" * 32, received_at=now - 3_600)
        _add(store, "fresh", conversation="bb" * 32, received_at=now - 30)
        rows = store.replay_batch(max_age_secs=0, now=now)
        assert _split(rows) == ([], ["stale", "fresh"])


def test_a_digest_row_is_never_in_a_shaped_replay(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "digest.db") as store:
        _add(store, "batch", reach="digest")
        _add(store, "direct", received_at=now - 1)
        assert [r["id"] for r in store.replay_batch(now=now)] == ["direct"], (
            "digest frames are batches, replayed whole by their own path"
        )


# -- decisions the shape must not break -----------------------------------

def test_the_newest_per_conversation_is_the_rule_across_conversations(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "rooms.db") as store:
        _add(store, "a1", conversation="aa" * 32, received_at=now - 6)
        _add(store, "b1", conversation="bb" * 32, received_at=now - 5)
        _add(store, "a2", conversation="aa" * 32, received_at=now - 4)
        _add(store, "b2", conversation="bb" * 32, received_at=now - 3)
        rows = store.replay_batch(now=now)
        assert _split(rows) == (["a1", "b1"], ["a2", "b2"])


def test_the_same_conversation_on_two_planes_never_supersedes_across_planes(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "planes.db") as store:
        _add(store, "nip17", plane="nip17", received_at=now - 2)
        _add(store, "nip04", plane="nip04", received_at=now - 1)
        rows = store.replay_batch(now=now)
        assert _split(rows) == ([], ["nip17", "nip04"])


def test_a_photo_is_not_superseded_by_a_later_message_without_it(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "media.db") as store:
        _add(store, "photo", received_at=now - 2,
             attachments=[{"sha256": "ab" * 32, "mime": "image/jpeg"}])
        _add(store, "caption", content="look at this", received_at=now - 1)
        rows = store.replay_batch(now=now)
        assert _split(rows) == ([], ["photo", "caption"]), (
            "the newer message does not carry the photo, so the photo must still run"
        )


def test_the_same_media_in_a_newer_message_does_supersede(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "same-media.db") as store:
        _add(store, "photo1", received_at=now - 2,
             attachments=[{"sha256": "cd" * 32, "mime": "image/jpeg"}])
        _add(store, "photo2", received_at=now - 1,
             attachments=[{"sha256": "cd" * 32, "mime": "image/jpeg"}])
        rows = store.replay_batch(now=now)
        assert _split(rows) == (["photo1"], ["photo2"])


def test_a_typed_approval_answer_is_never_superseded(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "approval.db") as store:
        _add(store, "answer", content="/approve once", received_at=now - 2)
        _add(store, "later", content="carry on", received_at=now - 1)
        rows = store.replay_batch(now=now)
        assert _split(rows) == ([], ["answer", "later"]), (
            "a gate answered by a superseded message is a lost decision"
        )


def test_an_approval_answer_does_not_supersede_the_message_before_it(tmp_path):
    """A command is not a newer message: the text it answers must still be read."""
    now = int(time.time())
    with _store(tmp_path, "approval-order.db") as store:
        _add(store, "instruction", content="run the deploy", received_at=now - 2)
        _add(store, "answer", content="/approve session", received_at=now - 1)
        rows = store.replay_batch(now=now)
        assert _split(rows) == ([], ["instruction", "answer"])


def test_a_reaction_is_never_superseded_and_does_not_supersede(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "reaction.db") as store:
        _add(store, "instruction", content="run the deploy", received_at=now - 2)
        _add(store, "reaction", kind=7, content="👍", received_at=now - 1)
        rows = store.replay_batch(now=now)
        assert _split(rows) == ([], ["instruction", "reaction"])


def test_only_a_typed_approve_or_deny_is_an_approval_answer():
    assert is_approval_reply("/approve once")
    assert is_approval_reply("/deny")
    assert is_approval_reply("  /APPROVE session ")
    assert not is_approval_reply("/approvals")
    assert not is_approval_reply("please /approve this")
    assert not is_approval_reply("/approved")


# -- the two bounds -------------------------------------------------------

def test_the_cap_keeps_the_newest_and_supersedes_the_rest(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "cap.db") as store:
        for index, ident in enumerate(("one", "two", "three", "four")):
            _add(store, ident, conversation=ident * 16, received_at=now - 4 + index)
        rows = store.replay_batch(max_messages=2, now=now)
        assert _split(rows) == (["one", "two"], ["three", "four"])


def test_max_messages_zero_is_the_old_replay(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "cap-off.db") as store:
        for index, ident in enumerate(("one", "two", "three", "four")):
            _add(store, ident, conversation=ident * 16, received_at=now - 4 + index)
        rows = store.replay_batch(max_messages=0, now=now)
        assert _split(rows) == ([], ["one", "two", "three", "four"]), (
            "0 disables the cap: every row is offered exactly as before"
        )


def test_the_cap_never_drops_media_to_make_its_number(tmp_path):
    now = int(time.time())
    with _store(tmp_path, "cap-media.db") as store:
        _add(store, "photo", received_at=now - 2,
             attachments=[{"sha256": "ef" * 32, "mime": "image/jpeg"}])
        _add(store, "newest", received_at=now - 1)
        rows = store.replay_batch(max_messages=1, now=now)
        assert _split(rows) == ([], ["photo", "newest"]), (
            "a cap is a cost bound, not a reason to lose an attachment"
        )


def test_the_batch_is_a_cap_on_turns_not_a_page_of_history(tmp_path):
    """A backlog bigger than `limit` must collapse to its newest end, not its oldest."""
    now = int(time.time())
    with _store(tmp_path, "backlog.db") as store:
        for index in range(30):
            _add(store, f"m{index:02d}", conversation=f"{index:02d}" * 16,
                 received_at=now - 60 + index)
        rows = store.replay_batch(limit=10, max_messages=2, now=now)
        assert [r["id"] for r in rows] == [f"m{i:02d}" for i in range(20, 30)]
        assert _split(rows) == ([f"m{i:02d}" for i in range(20, 28)],
                                ["m28", "m29"])


# -- the frame ------------------------------------------------------------

def _gateway(tmp_path, **replay) -> Gateway:
    tmp_path.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "identity.json"
    secret.write_text(json.dumps(
        {"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    config = {
        "identity": {"secret_file": str(secret)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": False, "hermes_platform": False},
        "replay": dict(replay),
    }
    gateway = Gateway(config, socket_path=tmp_path / "gateway.sock")
    gateway.store.set_replay_floor(0)  # these tests date their own rows
    return gateway


def test_a_superseded_frame_carries_the_flag_and_the_newer_id(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    gateway = _gateway(tmp_path)
    try:
        now = int(time.time())
        for index, ident in enumerate(("one", "two", "three")):
            _add(gateway.store, ident, received_at=now - 3 + index)

        frames = gateway.replay_pending()
        assert [frame["id"] for frame in frames] == ["one", "two", "three"]
        assert all(frame["replay"] is True for frame in frames)
        assert frames[0]["superseded"] is True
        assert frames[0]["superseded_by"] == "three"
        assert "superseded" not in frames[2], (
            "the turn is a plain message frame; `final` keeps its blocked-frame meaning"
        )
        assert "final" not in frames[2]

        gateway.ack(["one", "two"], reason="superseded")
        history = {row["id"]: row for row in gateway.store.recent(5)}
        assert history["one"]["settled_reason"] == "superseded"
        assert history["one"]["content"] == "message one"
        assert [r["id"] for r in gateway.store.pending_immediate()] == ["three"], (
            "the superseded rows are terminal; the turn is still waiting for its own ack"
        )
    finally:
        gateway.store.close()


def test_replay_config_is_read_at_construction(tmp_path, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    gateway = _gateway(tmp_path, max_messages=2, max_age_secs=0)
    try:
        assert gateway.replay_max_messages == 2
        assert gateway.replay_max_age_secs == 0
    finally:
        gateway.store.close()
    # And the defaults are the shipped shape, not a disabled one.
    gateway = _gateway(tmp_path / "defaults")
    try:
        assert gateway.replay_max_messages == 5
        assert gateway.replay_max_age_secs == 600
    finally:
        gateway.store.close()


def test_shaping_an_empty_batch_is_a_no_op():
    assert shape_replay([]) == []
