

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


def test_conversations_are_evidence_not_a_cache(tmp_path):
    from hermes_nostr_gateway.store import Inbound, Store

    def add(s, *, mid: str, group: str, at: int, plane: str) -> None:
        assert s.add(Inbound(
            id=mid, wrapper_id=mid, sender="dd" * 32, kind=9, created_at=at,
            content=f"text {mid}", conversation=group, reply_to=None, plane=plane,
            reach="digest", authority="none", rule="t", why="t",
        ))

    with Store(tmp_path / "conversations.db") as s:
        add(s, mid="m2", group="aa" * 16, at=1_700_000_000, plane="marmot")
        add(s, mid="m1", group="bb" * 16, at=1_700_000_100, plane="marmot")
        add(s, mid="m3", group="cc" * 32, at=1_700_000_200, plane="nip17")
        rows = s.conversations("marmot")
        assert [row["conversation"] for row in rows] == ["bb" * 16, "aa" * 16]
        assert rows[0]["messages"] == 1 and rows[0]["last_at"] == 1_700_000_100
        # Newest first and bounded, so status cannot walk an unbounded table.
        assert s.conversations("marmot", limit=1) == [rows[0]]


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


def _drop(store, ident: str, *, plane: str, sender: str, why: str,
          reason: str | None = None, at: int, interval_s: int | None = None) -> None:
    from hermes_nostr_gateway.store import Inbound

    assert store.add(Inbound(
        id=ident, wrapper_id="w-" + ident, sender=sender, kind=14, created_at=at,
        content=f"refused {ident}", conversation=sender, reply_to=None, plane=plane,
        reach="drop", authority="none", rule="test", why=why,
        blocked_reason=reason, interval_s=interval_s,
    ), now=at)


def test_drops_lists_reason_and_time_and_filters_by_plane_reason_since(tmp_path):
    """Every refusal, with why and when — the rows that used to sit invisible."""
    from hermes_nostr_gateway.store import Store

    now = 1_800_000_000
    with Store(tmp_path / "drops.db") as s:
        _drop(s, "spam", plane="nip17", sender="aa" * 32, at=now - 60,
              why="per-sender rate limit exceeded", reason="rate_limited")
        _drop(s, "stranger", plane="nip01", sender="bb" * 32, at=now - 30,
              why="no rule matched")
        # A row cut from a digest by its quota is a drop too, and says which.
        _drop(s, "quotacut", plane="nip17", sender="cc" * 32, at=now - 10,
              why="quota 3/h exceeded", interval_s=900)
        # Ordinary mail is not a drop, and is what `pending` exists to count.
        from hermes_nostr_gateway.store import Inbound
        assert s.add(Inbound(
            id="normal", wrapper_id="w-del", sender="dd" * 32, kind=14, created_at=now,
            content="hello", conversation="dd" * 32, reply_to=None, plane="nip17",
            reach="immediate", authority="instructions", rule="t", why="t",
        ), now=now)

        everything = s.drops(now=now)
        assert [row["id"] for row in everything["drops"]] == ["quotacut", "stranger", "spam"]
        assert {row["id"]: row["reason"] for row in everything["drops"]} == {
            "spam": "rate_limited", "stranger": "not_allowlisted", "quotacut": "digest_quota"}
        spam = everything["drops"][-1]
        assert spam["received_at"] == now - 60 and spam["age_s"] == 60
        assert spam["why"] == "per-sender rate limit exceeded"
        assert everything["total"] == 3 and everything["live"] == 3
        assert everything["expired"] == 0

        # Accounting: a reason-less drop is blocked, not pending for ever.
        stats = s.stats()
        assert stats["pending"] == 1, "only the ordinary row is pending"
        assert stats["blocked"] == 3 and stats["blocked"] == everything["total"]

        assert [r["id"] for r in s.drops(plane="nip01", now=now)["drops"]] == ["stranger"]
        assert [r["id"] for r in s.drops(reason="rate_limited", now=now)["drops"]] == ["spam"]
        assert [r["id"] for r in s.drops(since=now - 40, now=now)["drops"]] == [
            "quotacut", "stranger"]
        # The page is bounded; the count still describes the whole matching set.
        page = s.drops(limit=1, now=now)
        assert [r["id"] for r in page["drops"]] == ["quotacut"] and page["total"] == 3


def test_drops_past_the_ttl_are_reported_expired_not_hidden(tmp_path):
    """Retention is not deletion: an old refusal is still listed, and marked expired."""
    from hermes_nostr_gateway.store import Store

    now = 1_800_000_000
    week = 7 * 86400
    with Store(tmp_path / "ttl.db") as s:
        _drop(s, "old", plane="nip17", sender="aa" * 32, at=now - 10 * 86400,
              why="no rule matched")
        _drop(s, "fresh", plane="nip17", sender="aa" * 32, at=now - 3600,
              why="no rule matched")

        result = s.drops(ttl_s=week, now=now)
        assert [row["id"] for row in result["drops"]] == ["fresh", "old"]
        assert [row["expired"] for row in result["drops"]] == [False, True]
        assert result["total"] == 2 and result["live"] == 1 and result["expired"] == 1
        assert s.drop_counts(week, now=now) == {"total": 2, "live": 1, "expired": 1}
        # The row is still on the record; nothing was pruned behind the operator's back.
        assert s.db.execute("SELECT COUNT(*) AS n FROM rumors WHERE id = 'old'"
                            ).fetchone()["n"] == 1


def test_drops_ttl_config_is_strict_and_defaults_to_a_week():
    import pytest

    from hermes_nostr_gateway.store import DEFAULT_DROPS_TTL_HOURS, drops_ttl_seconds

    assert drops_ttl_seconds({}) == DEFAULT_DROPS_TTL_HOURS * 3600
    assert drops_ttl_seconds({"ttl_hours": 2}) == 7200
    with pytest.raises(ValueError):
        drops_ttl_seconds({"ttl_hours": 0})


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


def _immediate(store, ident, *, content=None, received_at=None, sender="ff" * 32):
    """Insert an immediate, replayable row, optionally pretending it arrived earlier."""
    import time

    from hermes_nostr_gateway.store import Inbound

    now = int(time.time())
    store.add(Inbound(id=ident, wrapper_id="w-" + ident, sender=sender, kind=14,
                      created_at=now, content=content or f"message {ident}",
                      conversation=sender, reply_to=None, plane="nip17",
                      reach="immediate", authority="instructions", rule="t", why="t"))
    if received_at is not None:
        store.db.execute("UPDATE rumors SET received_at = ? WHERE id = ?",
                         (received_at, ident))


def test_replay_window_is_minutes_not_days(tmp_path):
    import time

    from hermes_nostr_gateway.store import Store

    with Store(tmp_path / "min.db") as s:
        now = int(time.time())
        s.set_replay_floor(0)  # no handover to honour: every row is a candidate
        _immediate(s, "recent", received_at=now - 30)
        _immediate(s, "hours", content="an hour and more ago", received_at=now - 3 * 3600)

        assert [r["id"] for r in s.pending_immediate()] == ["recent"]
        # The default is a knob; the point is that two days of history is not the default.
        assert [r["id"] for r in s.pending_immediate(max_age_minutes=10_000)] == [
            "hours", "recent"]


def test_new_store_floor_stops_inherited_rows_being_replayed(tmp_path):
    """An import or handover must not re-offer messages the previous adapter answered."""
    import time

    from hermes_nostr_gateway.store import Store

    with Store(tmp_path / "floor.db") as s:
        now = int(time.time())
        assert s.replay_floor() in (now, now - 1)
        _immediate(s, "inherited", content="answered before the import",
                   received_at=now - 90)
        _immediate(s, "fresh", content="crashed mid-delivery")

        assert [r["id"] for r in s.pending_immediate()] == ["fresh"]
        # Even a window wide enough to cover the inherited row leaves it out.
        assert [r["id"] for r in s.pending_immediate(max_age_minutes=10_000)] == ["fresh"]


def test_skip_pending_records_a_handover(tmp_path):
    import time

    from hermes_nostr_gateway.store import Store

    with Store(tmp_path / "handover.db") as s:
        now = int(time.time())
        _immediate(s, "one")
        _immediate(s, "two")
        assert len(s.pending_immediate()) == 2

        assert s.skip_pending() == 2
        assert s.pending_immediate() == []
        assert s.replay_floor() >= now

        # A handover answers the backlog, not the future.
        _immediate(s, "after")
        assert [r["id"] for r in s.pending_immediate()] == ["after"]


def test_retry_ttl_expires_old_wraps_and_leaves_fresh_ones_retryable(tmp_path):
    """Age bounds a dead relay's backlog; a row that keeps failing cannot renew itself."""
    from hermes_nostr_gateway.store import Store

    now = 1_800_000_000
    day = 24 * 3600
    with Store(tmp_path / "wraps.db") as s:
        stale = {"id": "aa" * 32, "kind": 1059}
        fresh = {"id": "bb" * 32, "kind": 1059}
        never_tried = {"id": "cc" * 32, "kind": 1059}
        delivered = {"id": "dd" * 32, "kind": 1059}
        s.record_wrap(stale, "r1", "ee" * 32, ["wss://down.example"], now=now - 25 * 3600)
        s.record_wrap(fresh, "r2", "ee" * 32, ["wss://down.example"], now=now - 3600)
        s.record_wrap(never_tried, "r3", "ee" * 32, ["wss://down.example"], now=now - 25 * 3600)
        s.record_wrap(delivered, "r4", "ee" * 32, ["wss://ok.example"], now=now - 25 * 3600)
        s.mark_wrap(stale["id"], "wss://down.example", ok=False, error="connection refused")
        s.mark_wrap(fresh["id"], "wss://down.example", ok=False, error="503")
        s.mark_wrap(delivered["id"], "wss://ok.example", ok=True)

        assert s.expire_wraps(day, now=now) == 2
        rows = {row["id"]: row for row in s.db.execute("SELECT * FROM outbound_wraps")}
        assert rows[stale["id"]]["status"] == "expired"
        assert rows[stale["id"]]["last_error"] == "connection refused", \
            "the real failure is part of the record"
        assert rows[never_tried["id"]]["status"] == "expired"
        assert rows[never_tried["id"]]["last_error"] == "retry TTL exceeded"
        assert rows[fresh["id"]]["status"] == "failed", "a fresh row must stay retryable"
        assert rows[delivered["id"]]["status"] == "sent", "a delivered wrap is not expirable"

        assert [row["id"] for row in s.pending_wraps()] == [fresh["id"]]
        assert s.wrap_counts() == {"expired": 2, "failed": 1, "sent": 1}

        # Expiry is not re-applied to rows already expired.
        assert s.expire_wraps(day, now=now) == 0


def test_live_wrap_table_gains_created_at_dated_by_last_attempt(tmp_path):
    """A DB from before the TTL migrates; old failures must not look brand new."""
    import sqlite3
    import time

    from hermes_nostr_gateway.store import Store

    path = tmp_path / "old-wraps.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE outbound_wraps (id TEXT NOT NULL, relay TEXT NOT NULL,"
        " event TEXT NOT NULL, rumor_id TEXT NOT NULL, recipient TEXT NOT NULL,"
        " status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,"
        " last_error TEXT, last_attempt INTEGER, PRIMARY KEY (id, relay))"
    )
    attempted = int(time.time()) - 30 * 86400
    old.execute(
        "INSERT INTO outbound_wraps (id, relay, event, rumor_id, recipient, status,"
        " attempts, last_attempt) VALUES ('aa', 'wss://down.example', '{}', 'r', 'e',"
        " 'failed', 2, ?)",
        (attempted,),
    )
    old.execute(
        "INSERT INTO outbound_wraps (id, relay, event, rumor_id, recipient)"
        " VALUES ('bb', 'wss://down.example', '{}', 'r', 'e')"
    )
    old.commit()
    old.close()

    with Store(path) as s:
        rows = {row["id"]: row for row in s.db.execute("SELECT * FROM outbound_wraps")}
        assert rows["aa"]["created_at"] == attempted, "the last attempt is the only age known"
        assert rows["bb"]["created_at"] > attempted, "an undated pending row stays retryable"
