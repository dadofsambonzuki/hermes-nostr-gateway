"""Unix-socket API: NDJSON in, NDJSON out, 0600, same uid only.

One connection may both subscribe and request. Ops: subscribe, status, health, send,
history, peers, ack, mark_read, ping, plus the Marmot reads (`marmot.group`,
`marmot.timeline`) that only exist when that plane is attached. `health` is the one-call
frame the overnight cron reads instead of parsing logs. No TCP, no HTTP in v1 — there is
no auth surface to get wrong, and the adapter is a local process by design. See
docs/gateway-spec.md §5.

The 0600 mode is necessary but not sufficient: it is checked when a process calls
`connect()` and says nothing about a descriptor already open in another process. Every
accepted connection is therefore checked with `SO_PEERCRED` — the kernel's record of the
uid that connected — before a single frame is read.
"""

from __future__ import annotations

import asyncio
import errno
import json
import logging
import os
import socket
import struct
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger("hng.socket")

#: `struct ucred` as SO_PEERCRED returns it: pid, uid, gid (Linux).
_PEERCRED = struct.Struct("3i")

#: `sockaddr_un.sun_path` is a fixed 108-byte field on Linux, the terminating NUL
#: included — so a path may be 107 bytes. The kernel counts bytes, not characters,
#: which is why the check encodes first. See `socket_path_problem`.
UNIX_SOCKET_PATH_MAX = 108

#: The two ways out of a too-long path, said once so serve, doctor and the tests agree.
SOCKET_PATH_FIX = (
    "Set `socket:` in the config (or pass `--socket`) to a shorter path, for example "
    "/run/hermes-nostr-gateway/gateway.sock; or set XDG_RUNTIME_DIR so the default socket "
    "is $XDG_RUNTIME_DIR/hermes-nostr-gateway.sock"
)


def socket_path_problem(path: str | Path) -> str | None:
    """Why `path` cannot be bound as an AF_UNIX socket, or None when it fits.

    A `$HOME`-derived default is the usual victim: with no `XDG_RUNTIME_DIR`, the
    default is `$HOME/.local/state/hermes-nostr-gateway/gateway.sock`, and a long
    `$HOME` silently exceeds the kernel's address field. Naming the size, the limit
    and the fix here is what turns a traceback into a configuration error.
    """
    size = len(os.fsencode(str(path)))
    if size < UNIX_SOCKET_PATH_MAX:
        return None
    return (
        f"the control socket path is {size} bytes; the kernel's AF_UNIX limit is "
        f"{UNIX_SOCKET_PATH_MAX} bytes including the terminating NUL ({path})"
    )


class SocketPathError(RuntimeError):
    """The control-socket path does not fit the kernel's AF_UNIX address field."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.size = len(os.fsencode(self.path))
        problem = socket_path_problem(path) or (
            f"the control socket path is too long for an AF_UNIX socket: {self.path}")
        super().__init__(f"{problem}. {SOCKET_PATH_FIX}")


def peer_uid(sock: Any) -> int | None:
    """The uid at the other end of a connected AF_UNIX socket, or None if unknown.

    The kernel fills this in when the connection is made, so it is the credential the
    peer's process actually runs under — something the peer cannot claim in a frame.
    None means "cannot say" (no SO_PEERCRED on this platform, or not a socket), never
    "same uid"; the caller decides what to do with that.
    """
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    # asyncio hands back a TransportSocket wrapper on some versions; the real socket
    # object is one attribute down.
    raw = getattr(sock, "_sock", sock)
    try:
        data = raw.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _PEERCRED.size)
    except (AttributeError, OSError):
        return None
    if len(data) != _PEERCRED.size:
        return None
    _pid, uid, _gid = _PEERCRED.unpack(data)
    return int(uid)


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
    async def health(self, *, quiet_minutes: int | None = None,
                     pending_stale_minutes: int | None = None) -> dict[str, Any]: ...
    async def send(self, *, to: str, text: str, reply_to: str | None, cid: str | None,
                   route: Any = None) -> dict[str, Any]: ...
    def history(self, limit: int, *, conversation: str | None = None,
                before_at: int | None = None,
                before_id: str | None = None) -> dict[str, Any]: ...
    def drops(self, *, plane: str | None, reason: str | None,
              since: int | None, limit: int) -> dict[str, Any]: ...
    def peers(self) -> dict[str, Any]: ...
    def ack(self, ids: list[str] | None = None, *, rule: str | None = None,
            frame: int | None = None, reason: str | None = None) -> dict[str, Any]: ...
    def replay_digests(self, since_frame: int = 0) -> list[dict[str, Any]]: ...
    def mark_read(self, ids: list[str]) -> dict[str, Any]: ...
    def replay_pending(self, limit: int = 20) -> list[dict[str, Any]]: ...
    def replay_skip(self) -> dict[str, Any]: ...
    async def marmot_group(self, group: str) -> dict[str, Any]: ...
    async def marmot_timeline(self, group: str, limit: int = 20) -> dict[str, Any]: ...
    async def media_get(self, *, sha256: str | None, url: str | None,
                        rumor_id: str | None) -> dict[str, Any]: ...


class SocketServer:
    def __init__(self, path: str | Path, handler: Handler):
        self.path = Path(path)
        self.handler = handler
        self._clients: set[asyncio.StreamWriter] = set()
        self._server: asyncio.AbstractServer | None = None

    async def start(self) -> None:
        problem = socket_path_problem(self.path)
        if problem is not None:
            raise SocketPathError(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            self.path.unlink()
        try:
            self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        except OSError as exc:
            if exc.errno == errno.ENAMETOOLONG:
                # The check above states the rule; this is the kernel stating it, kept so a
                # platform whose field is smaller still gets a named error, not a traceback.
                raise SocketPathError(self.path) from exc
            raise
        os.chmod(self.path, 0o600)
        log.info("socket listening at %s", self.path)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            # Since 3.12 wait_closed() also waits for live client handlers, and the
            # Hermes adapter holds its connection open by design — so this call was the
            # 15s SIGKILL on every restart (two for two). Bound it: closing the listener
            # is what matters, the handlers are aborted with the loop.
            try:
                await asyncio.wait_for(self._server.wait_closed(), 2)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("socket server still had open handlers at shutdown; not waiting")
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
        uid = peer_uid(writer.get_extra_info("socket"))
        if uid is not None and uid != os.getuid():
            # A foreign uid gets no reply at all: not an error frame, not `pong` — the
            # connection is closed before the first read. `uid is None` means the
            # platform cannot answer the question, and the 0600 mode is what is left.
            log.warning("socket: refused connection from uid %d; this daemon runs as %d",
                        uid, os.getuid())
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass
            return
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
                    # Reads answer from what has been accepted, and the writer commits a
                    # tick later: settle it first so an op never reports a queued row as
                    # missing (see Gateway.flush_writes).
                    settle = getattr(self.handler, "flush_writes", None)
                    if settle is not None and op != "ping":
                        await settle()
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
                    elif op == "health":
                        # One call, and it is the same frame whatever the daemon's age:
                        # an older daemon that has no `health` says so by code, so the
                        # cron reports "daemon too old" instead of an empty narrative.
                        handler = getattr(self.handler, "health", None)
                        if handler is None:
                            await self._send(writer, {"type": "error", "op": op,
                                                      "code": "unsupported",
                                                      "error": "health is not available "
                                                               "on this daemon"})
                        else:
                            minutes = req.get("pending_stale_minutes")
                            quiet = req.get("quiet_minutes")
                            await self._send(writer, await handler(
                                quiet_minutes=None if quiet is None else int(quiet),
                                pending_stale_minutes=None if minutes is None else int(minutes),
                            ))
                    elif op == "send":
                        frame = await self.handler.send(
                            to=req["to"], text=req["text"],
                            reply_to=req.get("reply_to"), cid=req.get("id"),
                            route=req.get("route"),
                        )
                        await self._send(writer, frame)
                    elif op == "history":
                        # The unscoped answer is the operator's view of the store. A scoped
                        # one is a channel client asking for the page ending at its trigger:
                        # the echo is load-bearing, because an older daemon silently ignores
                        # the extra fields and would otherwise hand back a page from other
                        # conversations that looks like a window.
                        limit = int(req.get("limit", 20))
                        conversation = str(req.get("conversation") or "")
                        before_at = req.get("before_at")
                        before_id = str(req.get("before_id") or "")
                        if conversation and before_at not in (None, "") and before_id:
                            requested_at = int(before_at)
                            await self._send(writer, {
                                "type": "history",
                                "scoped": True,
                                "conversation": conversation,
                                "before_at": requested_at,
                                "before_id": before_id,
                                **self.handler.history(limit, conversation=conversation,
                                                       before_at=requested_at,
                                                       before_id=before_id),
                            })
                        else:
                            await self._send(writer, {"type": "history",
                                                      **self.handler.history(limit)})
                    elif op == "drops":
                        since = req.get("since")
                        await self._send(writer, {"type": "drops", **self.handler.drops(
                            plane=req.get("plane") or None,
                            reason=req.get("reason") or None,
                            since=int(since) if since not in (None, "", 0) else None,
                            limit=int(req.get("limit") or 100),
                        )})
                    elif op == "peers":
                        await self._send(writer, {"type": "peers", **self.handler.peers()})
                    elif op == "authorize":
                        # A recovery re-ask, not a delivery path: Hermes re-checks a stored
                        # turn's role grant against live policy rather than trusting the
                        # flag it saved. A daemon too old to answer says so by code.
                        handler = getattr(self.handler, "authorize", None)
                        if handler is None:
                            await self._send(writer, {"type": "error", "op": op,
                                                      "code": "unsupported",
                                                      "error": "authorize is not available "
                                                               "on this daemon"})
                        else:
                            requested = req.get("kind")
                            await self._send(writer, handler(
                                pubkey=str(req.get("pubkey") or ""),
                                conversation=str(req.get("conversation") or ""),
                                kind=int(requested) if requested not in (None, "") else None))
                    elif op == "ack":
                        if "rule" in req or "frame" in req:
                            result = self.handler.ack(
                                rule=req.get("rule"), frame=req.get("frame")
                            )
                        else:
                            # `reason` records a delivery that ended without a turn: the
                            # adapter acks a shaped-away replay as `superseded` so the row
                            # is terminal (not stuck) while `history` keeps the message.
                            # Only forwarded when sent: a message-only consumer predates it.
                            extra = ({"reason": req.get("reason")}
                                     if "reason" in req else {})
                            result = self.handler.ack(req.get("ids", []), **extra)
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
                    elif op == "media.get":
                        handler = getattr(self.handler, "media_get", None)
                        if handler is None:
                            await self._send(writer, {"type": "error", "op": op,
                                                      "code": "unsupported",
                                                      "error": "media.get is not available"})
                        else:
                            await self._send(writer, await handler(
                                sha256=req.get("sha256"), url=req.get("url"),
                                rumor_id=req.get("rumor_id")))
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
