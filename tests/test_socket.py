"""Socket contract: NDJSON ops, broadcast to subscribers, one bad op kills nothing."""

import asyncio
import json
import os
import socket
import struct

import pytest
import pytest_asyncio

from hermes_nostr_gateway import server as socket_server
from hermes_nostr_gateway.server import SocketServer


class FakeHandler:
    async def status(self):
        return {"identity": "me", "rumors": 1}

    async def send(self, *, to, text, reply_to, cid):
        return {"type": "delivery", "id": cid, "ok": True, "message_id": "r1"}

    def history(self, limit):
        return {"messages": [{"id": "r1"}] * min(limit, 1)}

    def peers(self):
        return {"peers": []}

    def ack(self, ids=None, *, rule=None, frame=None):
        return {"acked": ids or []}

    def replay_digests(self, since_frame=0):
        return []

    def mark_read(self, ids):
        return {"acked": ids}

    def replay_pending(self, limit=20):
        return []

    def replay_skip(self):
        return {"skipped": 3, "replay_floor": 1234}


class DigestHandler(FakeHandler):
    def replay_digests(self, since_frame=0):
        return [{"type": "message", "plane": "digest", "id": "d:me:unknown-dms:60",
                 "frame": 2, "rule": "unknown-dms", "items": [{"rumor_id": "r1"}]}]

    def ack(self, ids=None, *, rule=None, frame=None):
        return {"rule": rule, "frame": frame, "acked": 1}


class StoreHandler(FakeHandler):
    """The parts of Gateway a socket test needs: replay and ack, over a real Store."""

    def __init__(self, store):
        self.store = store

    def replay_digests(self, since_frame=0):
        return self.store.replay_digests(since_frame)

    def ack(self, ids=None, *, rule=None, frame=None):
        if rule is not None and frame is not None:
            return self.store.ack_digest(rule, int(frame))
        self.store.mark_delivered(ids or [])
        return {"acked": ids or []}


async def call(path, *frames):
    reader, writer = await asyncio.open_unix_connection(str(path))
    out = []
    for frame in frames:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        out.append(json.loads(await asyncio.wait_for(reader.readline(), 2)))
    writer.close()
    await writer.wait_closed()
    return out


@pytest_asyncio.fixture
async def server(tmp_path):
    srv = SocketServer(tmp_path / "s.sock", FakeHandler())
    await srv.start()
    yield srv
    await srv.stop()


async def test_status_and_history(server, tmp_path):
    status, history = await call(tmp_path / "s.sock",
                                 {"op": "status"}, {"op": "history", "limit": 5})
    assert status["type"] == "status" and status["identity"] == "me"
    assert history["type"] == "history" and len(history["messages"]) == 1


async def test_send_round_trip(server, tmp_path):
    (frame,) = await call(tmp_path / "s.sock",
                          {"op": "send", "to": "npub1x", "text": "hi", "id": "c1"})
    assert frame["ok"] and frame["id"] == "c1" and frame["message_id"] == "r1"


async def test_unknown_op_reports_error_without_closing(server, tmp_path):
    a, b = await call(tmp_path / "s.sock", {"op": "wat"}, {"op": "ping"})
    assert a["type"] == "error" and "wat" in a["error"]
    assert b["type"] == "pong"


async def test_bad_json_does_not_kill_the_connection(server, tmp_path):
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "s.sock"))
    writer.write(b"not json\n")
    await writer.drain()
    frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert frame["type"] == "error"
    writer.write(b'{"op":"ping"}\n')
    await writer.drain()
    assert json.loads(await asyncio.wait_for(reader.readline(), 2))["type"] == "pong"
    writer.close()
    await writer.wait_closed()


async def test_subscriber_receives_broadcast(server, tmp_path):
    reader, writer = await asyncio.open_unix_connection(str(tmp_path / "s.sock"))
    writer.write(b'{"op":"subscribe"}\n')
    await writer.drain()
    assert json.loads(await asyncio.wait_for(reader.readline(), 2))["type"] == "subscribed"
    await server.broadcast({"type": "message", "id": "r9", "content": "hi"})
    frame = json.loads(await asyncio.wait_for(reader.readline(), 2))
    assert frame["type"] == "message" and frame["id"] == "r9"
    writer.close()
    await writer.wait_closed()


async def test_replay_skip_records_a_handover(server, tmp_path):
    (frame,) = await call(tmp_path / "s.sock", {"op": "replay.skip"})
    assert frame["type"] == "ok"
    assert frame["skipped"] == 3 and frame["replay_floor"] == 1234

class FakePeerSocket:
    """Just the call `peer_uid` makes, with the kernel's answer stubbed in."""

    def __init__(self, uid: int, pid: int = 4321, gid: int = 1000):
        self.payload = struct.pack("3i", pid, uid, gid)

    def getsockopt(self, level, option, size):
        assert (level, option) == (socket.SOL_SOCKET, socket.SO_PEERCRED)
        assert size == len(self.payload)
        return self.payload


def test_peer_uid_is_the_kernel_credential():
    assert socket_server.peer_uid(FakePeerSocket(uid=1001)) == 1001


def test_peer_uid_unwraps_the_asyncio_transport_socket():
    class TransportSocket:  # what transport.get_extra_info("socket") may hand back
        _sock = FakePeerSocket(uid=1002)

    assert socket_server.peer_uid(TransportSocket()) == 1002


def test_peer_uid_is_unknown_rather_than_wrong():
    class Unsupported:
        def getsockopt(self, *args):
            raise OSError(95, "Operation not supported")

    assert socket_server.peer_uid(Unsupported()) is None
    assert socket_server.peer_uid(None) is None


async def test_the_live_path_reports_this_uid_and_not_none(tmp_path):
    """`get_extra_info("socket")` must really answer, or the check is a silent no-op."""
    seen: list[int | None] = []

    class Recording(SocketServer):
        async def _handle(self, reader, writer):
            seen.append(socket_server.peer_uid(writer.get_extra_info("socket")))
            await super()._handle(reader, writer)

    srv = Recording(tmp_path / "uid.sock", FakeHandler())
    await srv.start()
    try:
        (frame,) = await call(tmp_path / "uid.sock", {"op": "ping"})
        assert frame["type"] == "pong"
    finally:
        await srv.stop()
    assert seen == [os.getuid()], "peer_uid saw no credential on a real connection"


async def test_a_connection_from_another_uid_is_closed_without_a_reply(tmp_path, monkeypatch):
    """0600 is checked at connect(); this is the check that survives a passed descriptor."""
    monkeypatch.setattr(socket_server, "peer_uid", lambda sock: os.getuid() + 1)
    srv = SocketServer(tmp_path / "refused.sock", FakeHandler())
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "refused.sock"))
        writer.write(b'{"op":"ping"}\n')
        await writer.drain()
        # No reply, whether as a clean EOF or an abortive close: the daemon never reads the
        # frame, so the unread bytes make Linux answer RST rather than FIN.
        try:
            assert await asyncio.wait_for(reader.readline(), 2) == b"", \
                "a connection from another uid was answered instead of closed"
        except ConnectionResetError:
            pass
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionResetError:
            pass  # the RST can also land on the close, not just the read
    finally:
        await srv.stop()


async def test_a_same_uid_connection_is_answered(server, tmp_path):
    (frame,) = await call(tmp_path / "s.sock", {"op": "ping"})
    assert frame["type"] == "pong"


async def test_subscribe_replays_digest_and_ack_uses_frame(tmp_path):
    srv = SocketServer(tmp_path / "d.sock", DigestHandler())
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "d.sock"))
        writer.write(b'{"op":"subscribe","since":1}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 2))["type"] == "subscribed"
        digest = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert digest["type"] == "message" and digest["frame"] == 2

        writer.write(b'{"op":"ack","rule":"unknown-dms","frame":2}\n')
        await writer.drain()
        ack = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert ack == {"type": "ok", "rule": "unknown-dms", "frame": 2, "acked": 1}
        writer.close()
        await writer.wait_closed()
    finally:
        await srv.stop()


async def test_legacy_adapter_acks_a_digest_by_frame_id_and_reconnect_replays_nothing(tmp_path):
    """Done-condition for the digest contract: an adapter that knows only
    `type: message` receives the digest, acks its `id`, and a reconnect is quiet."""
    from hermes_nostr_gateway.store import Inbound, Store

    me, peer = "aa" * 32, "bb" * 32
    store = Store(tmp_path / "legacy.db", identity=me)
    for i in range(2):
        assert store.add(Inbound(
            id=f"r{i}", wrapper_id=f"w{i}", sender=peer, kind=14, created_at=100 + i,
            content=f"note {i}", conversation=peer, reply_to=None, plane="nip17",
            reach="digest", authority="none", rule="unknown-dms", why="test",
            interval_s=60,
        ), now=100 + i)
    sent = store.seal_digests(now=160)[0]
    assert sent["type"] == "message" and sent["plane"] == "digest"

    srv = SocketServer(tmp_path / "legacy.sock", StoreHandler(store))
    await srv.start()
    try:
        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "legacy.sock"))
        writer.write(b'{"op":"subscribe"}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 2))["type"] == "subscribed"
        received = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert received["type"] == "message" and received["id"] == sent["id"]
        assert received["content"].startswith("2 item(s) in the unknown-dms digest:")

        writer.write((json.dumps({"op": "ack", "ids": [received["id"]]}) + "\n").encode())
        await writer.drain()
        ack = json.loads(await asyncio.wait_for(reader.readline(), 2))
        assert ack == {"type": "ok", "acked": [sent["id"]]}
        writer.close()
        await writer.wait_closed()

        reader, writer = await asyncio.open_unix_connection(str(tmp_path / "legacy.sock"))
        writer.write(b'{"op":"subscribe"}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 2))["type"] == "subscribed"
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(reader.readline(), 0.2)
        writer.close()
        await writer.wait_closed()
    finally:
        await srv.stop()
