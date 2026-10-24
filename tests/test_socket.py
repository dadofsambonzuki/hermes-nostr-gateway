"""Socket contract: NDJSON ops, broadcast to subscribers, one bad op kills nothing."""

import asyncio
import json

import pytest
import pytest_asyncio

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

    def ack(self, ids):
        return {"acked": ids}

    def mark_read(self, ids):
        return {"acked": ids}

    def replay_pending(self, limit=20):
        return []


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
