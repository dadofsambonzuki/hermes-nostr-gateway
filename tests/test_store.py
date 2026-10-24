

def test_same_message_over_two_planes_is_stored_once(tmp_path):
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "d.db") as s:
        base = dict(sender="aa" * 32, kind=14, created_at=1_700_000_000,
                    content="Agreed on both calls.", conversation="aa" * 32,
                    reply_to=None, reach="immediate", authority="instructions",
                    rule="test", why="test")
        assert s.add(Inbound(id="r1", wrapper_id="w1", plane="nip17", **base)) is True
        # same text, one second later, arriving over NIP-04 with a different id
        assert s.add(Inbound(id="r2", wrapper_id="w2", plane="nip04",
                             **{**base, "created_at": 1_700_000_001})) is False


def test_distinct_messages_from_same_sender_both_stored(tmp_path):
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "d2.db") as s:
        base = dict(sender="bb" * 32, kind=14, created_at=1_700_000_000,
                    conversation="bb" * 32, reply_to=None, reach="immediate",
                    authority="instructions", rule="t", why="t")
        assert s.add(Inbound(id="a", wrapper_id="a", content="one", plane="nip17", **base))
        assert s.add(Inbound(id="b", wrapper_id="b", content="two", plane="nip17", **base))


def test_pending_immediate_is_replayed_but_old_or_sent_is_not(tmp_path):
    import time
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "p.db") as s:
        now = int(time.time())
        base = dict(sender="cc" * 32, kind=14, conversation="cc" * 32, reply_to=None,
                    authority="instructions", rule="t", why="t")
        s.add(Inbound(id="fresh", wrapper_id="w1", created_at=now, content="new",
                      plane="nip17", reach="immediate", **base))
        s.add(Inbound(id="old", wrapper_id="w2", created_at=now - 3 * 86400, content="old",
                      plane="nip17", reach="immediate", **base))
        # received_at is what the replay window uses; backdate it for the stale row
        s.db.execute("UPDATE rumors SET received_at = ? WHERE id = 'old'", (now - 3 * 86400,))
        s.add(Inbound(id="digestish", wrapper_id="w3", created_at=now, content="later",
                      plane="nip01", reach="digest", **base))
        ids = [r["id"] for r in s.pending_immediate()]
        assert ids == ["fresh"]


def test_blocked_rows_are_stored_but_never_replayed_or_counted_pending(tmp_path):
    import time
    from hermes_nostr_gateway.store import Inbound, Store

    with Store(tmp_path / "b.db") as s:
        now = int(time.time())
        s.add(Inbound(id="ok", wrapper_id="w1", sender="dd" * 32, kind=14, created_at=now,
                      content="hello", conversation="dd" * 32, reply_to=None,
                      plane="nip17", reach="immediate", authority="instructions",
                      rule="family", why="matched"))
        s.add(Inbound(id="spam", wrapper_id="w2", sender="dd" * 32, kind=14, created_at=now,
                      content="spam", conversation="dd" * 32, reply_to=None,
                      plane="nip17", reach="drop", authority="none",
                      rule="family", why="rate limited",
                      blocked_reason="rate_limited"))
        assert [r["id"] for r in s.pending_immediate()] == ["ok"]
        assert s.stats()["pending"] == 1
        assert s.stats()["blocked"] == 1
        rows = {r["id"]: r for r in s.recent(5)}
        assert rows["spam"]["blocked_reason"] == "rate_limited"  # still on the record


def test_live_store_gains_the_blocked_reason_column(tmp_path):
    """A DB created before blocked frames existed must migrate on open, not fail."""
    import sqlite3

    from hermes_nostr_gateway.store import Store

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE rumors (id TEXT PRIMARY KEY, wrapper_id TEXT NOT NULL,"
        " sender TEXT NOT NULL, kind INTEGER NOT NULL, created_at INTEGER NOT NULL,"
        " conversation TEXT, content TEXT, reply_to TEXT, plane TEXT NOT NULL,"
        " reach TEXT, authority TEXT, rule TEXT, why TEXT, received_at INTEGER NOT NULL,"
        " delivered_at INTEGER)"
    )
    old.commit()
    old.close()
    with Store(path) as s:
        columns = {row["name"] for row in s.db.execute("PRAGMA table_info(rumors)")}
        assert "blocked_reason" in columns
