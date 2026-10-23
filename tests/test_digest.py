"""Digest batching: per-rule windows, per-rule ack, contiguous cursors."""

import sqlite3

from hermes_nostr_gateway.store import Inbound, Store

ME = "aa" * 32
PEER = "bb" * 32


def inbound(rid, *, reach="digest", rule="unknown-dms", interval=60, now=1000,
            plane="nip17", content="hello"):
    return Inbound(
        id=rid, wrapper_id=f"w-{rid}", sender=PEER, kind=14, created_at=now,
        content=content, conversation=PEER, reply_to=None, plane=plane,
        reach=reach, authority="none", rule=rule, why="test", interval_s=interval,
    )


def test_digest_waits_for_its_window_then_emits_one_frame(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("r1"), now=1000)
    assert store.seal_digests(now=1000) == []
    frames = store.seal_digests(now=1060)
    assert len(frames) == 1
    frame = frames[0]
    assert frame["type"] == "digest" and frame["frame"] == 1
    assert frame["count"] == 1 and frame["delivered"] == 1
    assert frame["items"][0]["rumor_id"] == "r1"


def test_one_rule_ack_does_not_cover_another_rules_queued_item(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("slow", rule="slow", interval=900), now=100)
    assert store.add(inbound("fast", rule="fast", interval=60), now=101)

    frames = store.seal_digests(now=200)
    assert [f["rule"] for f in frames] == ["fast"]
    assert frames[0]["frame"] == 1

    assert store.ack_digest("fast", 1)["acked"] == 1
    slow = store.db.execute("SELECT delivered_at FROM rumors WHERE id='slow'").fetchone()
    fast = store.db.execute("SELECT delivered_at FROM rumors WHERE id='fast'").fetchone()
    assert slow["delivered_at"] is None
    assert fast["delivered_at"] is not None


def test_dropped_rows_are_counted_and_share_the_batch(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("drop", reach="drop", rule="r", interval=60), now=100)
    assert store.add(inbound("keep", reach="digest", rule="r", interval=60), now=100)
    frames = store.seal_digests(now=160)
    assert len(frames) == 1
    assert frames[0]["count"] == 2 and frames[0]["dropped"] == 1
    assert frames[0]["delivered"] == 1
    dropped = store.db.execute("SELECT digest_batch FROM rumors WHERE id='drop'").fetchone()
    kept = store.db.execute("SELECT digest_batch FROM rumors WHERE id='keep'").fetchone()
    assert dropped["digest_batch"] == kept["digest_batch"]


def test_ack_marks_truncated_items_too(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("r1"), now=100)
    assert store.add(inbound("r2"), now=101)
    frame = store.seal_digests(now=160, top_n=1)[0]
    assert frame["count"] == 2 and frame["delivered"] == 1 and frame["truncated"]
    assert store.ack_digest("unknown-dms", frame["frame"])["acked"] == 2
    rows = store.db.execute("SELECT delivered_at FROM rumors ORDER BY id").fetchall()
    assert all(r["delivered_at"] is not None for r in rows)


def test_replay_returns_unacked_frames_and_ack_removes_them(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("r1"), now=100)
    frame = store.seal_digests(now=160)[0]
    assert [f["frame"] for f in store.replay_digests()] == [frame["frame"]]
    store.ack_digest("unknown-dms", frame["frame"])
    assert store.replay_digests() == []


def test_old_rumors_table_is_backfilled_with_contiguous_cursors(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE rumors (id TEXT PRIMARY KEY, wrapper_id TEXT NOT NULL,"
        " sender TEXT NOT NULL, kind INTEGER NOT NULL, created_at INTEGER NOT NULL,"
        " conversation TEXT, content TEXT, reply_to TEXT, plane TEXT NOT NULL DEFAULT 'nip17',"
        " reach TEXT, authority TEXT, rule TEXT, why TEXT, received_at INTEGER NOT NULL,"
        " delivered_at INTEGER);"
        "INSERT INTO rumors VALUES ('old1','w1','peer',14,1,'c','one',NULL,'nip17',"
        "'immediate','none','r','why',10,NULL);"
        "INSERT INTO rumors VALUES ('old2','w2','peer',14,2,'c','two',NULL,'nip17',"
        "'immediate','none','r','why',11,NULL);"
    )
    db.commit()
    db.close()

    store = Store(path, identity=ME)
    rows = store.db.execute("SELECT id, cursor FROM rumors ORDER BY received_at").fetchall()
    assert [(r["id"], r["cursor"]) for r in rows] == [("old1", 1), ("old2", 2)]
    assert store.add(inbound("new"), now=100)
    assert store.db.execute("SELECT cursor FROM rumors WHERE id='new'").fetchone()["cursor"] == 3


def test_same_rumor_twice_does_not_burn_a_cursor(tmp_path):
    store = Store(tmp_path / "d.db", identity=ME)
    assert store.add(inbound("r1"), now=100)
    assert not store.add(inbound("r1"), now=101)
    assert store.add(inbound("r2"), now=102)
    rows = store.db.execute("SELECT id, cursor FROM rumors ORDER BY cursor").fetchall()
    assert [(r["id"], r["cursor"]) for r in rows] == [("r1", 1), ("r2", 2)]
