"""Unix-socket API: NDJSON in, NDJSON out, 0600, local only.

One connection may both subscribe and request. Ops: subscribe, status, send, history,
peers, ack, mark_read, ping, plus the Marmot reads (`marmot.group`, `marmot.timeline`)
that only exist when that plane is attached. No TCP, no HTTP in v1 — there is no auth
surface to get wrong, and the adapter is a local process by design. See
docs/gateway-spec.md §5.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("hng.socket")


async def request(path: str | Path, frame: dict[str, Any], *,
                  timeout: float = 3.0) -> dict[str, Any]:
    """One NDJSON request, one reply, over the local socket.

    `hng status` uses this to ask the running daemon what its workers are doing, so it
    reports the daemon's live state rather than re-deriving it from the store. Raises
    OSError when nothing is listening and TimeoutError when the daemon does not answer;
    callers decide what "no answer" means.
    """
    reader, writer = await asyncio.open_unix_connection(str(path), limit=1 << 20)
    try:
        writer.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout)
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass
    if not line:
        raise ConnectionError(f"{path}: daemon closed the connection without a reply")
    reply = json.loads(line)
    if not isinstance(reply, dict):
        raise ValueError(f"{path}: expected one JSON object, got {type(reply).__name__}")
    return reply


class Handler(Protocol):
    async def status(self) -> dict[str, Any]: ...
    async def send(self, *, to: str, text: str, reply_to: str | None, cid: str | None) -> dict[str, Any]: ...
    def history(self, limit: int) -> dict[str, Any]: ...
    def peers(self) -> dict[str, Any]: ...
    def ack(self, ids: list[str] | None = None, *, rule: str | None = None,
            frame: int | None = None) -> dict[str, Any]: ...
    def replay_digests(self, since_frame: int = 0) -> list[dict[str, Any]]: ...
    def mark_read(self, ids: list[str]) -> dict[str, Any]: ...
    def replay_pending(self, limit: int = 20) -> list[dict[str, Any]]: ...
    def replay_skip(self) -> dict[str, Any]: ...
    async def marmot_group(self, group: str) -> dict[str, Any]: ...
    async def marmot_timeline(self, group: str, limit: int = 20) -> dict[str, Any]: ...


class SocketServer:
    def __init__(self, path: str | Path, handler: Handler):
        self.path = Path(path)
        self.handler = handler
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600)
        log.info("socket listening at %s", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Do not await client shutdown here: a half-open test connection would make
        # stop() hang forever.
        for writer in list(self._clients):
            writer.close()
        self._clients.clear()
        if self.path.exists():
            self.path.unlink()

    @property
    def has_subscribers(self) -> bool:
        return bool(self._clients)

    async def broadcast(self, frame: dict[str, Any]) -> None:
        if not self._clients:
            return
        line = json.dumps(frame, separators=(",", ":")) + "\n"
        for writer in list(self._clients):
            try:
                writer.write(line.encode())
                await writer.drain()
            except (ConnectionError, asyncio.CancelledError):
                self._clients.discard(writer)

    # -- per connection -------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        subscribed = False
        try:
            while line := await reader.readline():
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line)
                except json.JSONDecodeError:
                    await self._send(writer, {"type": "error", "error": "bad json"})
                    continue
                op = req.get("op")
                try:
                    if op == "subscribe":
                        subscribed = True
                        self._clients.add(writer)
                        await self._send(writer, {"type": "subscribed",
                                                  "since": req.get("since")})
                        # Re-offer anything accepted but never acknowledged, so a
                        # reconnect (or a crash mid-delivery) cannot lose it. Immediate
                        # frames replay by id; digest frames replay whole, by frame
                        # sequence, until the batch itself is acked.
                        for frame in self.handler.replay_pending():
                            await self._send(writer, frame)
                        replay = getattr(self.handler, "replay_digests", None)
                        if replay is not None:
                            for frame in replay(int(req.get("since") or 0)):
                                await self._send(writer, frame)
                    elif op == "status":
                        await self._send(writer, {"type": "status", **(await self.handler.status())})
                    elif op == "send":
                        frame = await self.handler.send(
                            to=req["to"], text=req["text"],
                            reply_to=req.get("reply_to"), cid=req.get("id"),
                        )
                        await self._send(writer, frame)
                    elif op == "history":
                        await self._send(writer, {"type": "history",
                                                  **self.handler.history(int(req.get("limit", 20)))})
                    elif op == "peers":
                        await self._send(writer, {"type": "peers", **self.handler.peers()})
                    elif op == "ack":
                        if "rule" in req or "frame" in req:
                            result = self.handler.ack(
                                rule=req.get("rule"), frame=req.get("frame")
                            )
                        else:
                            result = self.handler.ack(req.get("ids", []))
                        await self._send(writer, {"type": "ok", **result})
                    elif op == "mark_read":
                        await self._send(writer, {"type": "ok",
                                                  **self.handler.mark_read(req.get("ids", []))})
                    elif op == "replay.skip":
                        # Handover: everything already pending was answered by whoever
                        # ran before. Records the fact instead of replaying it.
                        await self._send(writer, {"type": "ok", **self.handler.replay_skip()})
                    elif op == "marmot.group":
                        handler = getattr(self.handler, "marmot_group", None)
                        if handler is None:
                            await self._send(writer, {"type": "error", "op": op,
                                                      "error": "marmot plane not enabled"})
                        else:
                            await self._send(writer, await handler(req.get("group") or ""))
                    elif op == "marmot.timeline":
                        handler = getattr(self.handler, "marmot_timeline", None)
                        if handler is None:
                            await self._send(writer, {"type": "error", "op": op,
                                                      "error": "marmot plane not enabled"})
                        else:
                            await self._send(writer, await handler(
                                req.get("group") or "", int(req.get("limit", 20))))
                    elif op == "ping":
                        await self._send(writer, {"type": "pong"})
                    else:
                        await self._send(writer, {"type": "error", "error": f"unknown op {op!r}"})
                except Exception as exc:  # noqa: BLE001 - one bad request must not kill the socket
                    log.exception("op %s failed", op)
                    await self._send(writer, {"type": "error", "op": op, "error": str(exc)[:300]})
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if subscribed:
                self._clients.discard(writer)
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, frame: dict[str, Any]) -> None:
        writer.write((json.dumps(frame, separators=(",", ":")) + "\n").encode())
        await writer.drain()
