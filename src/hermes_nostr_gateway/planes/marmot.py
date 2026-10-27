"""Marmot plane: a *proxy* for wn-agent's control socket, never an MLS implementation.

MLS group state, the account key and the relay fan-out stay wn-agent's (spec §16:
"proxy, not an implementation"). This module owns only the wire between the two
daemons: NDJSON envelopes (`marmot.agent-control.v2`) over a 0600 Unix socket, an
inbound subscription that becomes ordinary `Inbound` rows, an outbound `send_final`,
and the two read calls a resolver needs (`group_info`, `timeline_list`).

What it deliberately does not do:

* **No group state writes** (create/join/leave). Two writers to one account is what
  spec §2 forbids; the invite that seeds a group has to come from a human's app.
* **No decryption.** There are no MLS keys here — wn-agent hands over plaintext DTOs.
* **No media download.** `message.media` references are dropped until the M3
  quarantine path exists for every plane, not just NIP-17.

The socket's own framing is the one thing worth stating twice: a request carries
`marmot_agent_control` (protocol id) and `id` (correlation), and every response
echoes both. A response with the wrong id is discarded rather than trusted — the
daemon multiplexes one socket and a mismatched ack is how a caller ends up reading
another request's reply.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import random
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Iterable

from .. import addressing
from ..policy import Policy
from ..store import Inbound, Store

log = logging.getLogger("hng.plane.marmot")

PROTOCOL = "marmot.agent-control.v2"
MAX_FRAME_BYTES = 1024 * 1024
# wn-agent's group handle is 16 bytes; a Nostr pubkey is 32. That difference is what
# lets a target be classified without a lookup (see MarmotResolver.parse).
GROUP_ID_HEX_LEN = 32
# White Noise application messages are inner kind 9 (kind 445 on the wire).
APP_MESSAGE_KIND = 9

MessageHandler = Callable[[dict[str, Any]], Awaitable[None] | None]


class MarmotError(RuntimeError):
    """A control-socket failure, carrying whether a retry could plausibly help."""

    def __init__(self, message: str, *, code: str = "marmot_error", retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


# -- transport -------------------------------------------------------------


class MarmotControl:
    """One request/response per connection, plus a long-lived inbound subscription.

    wn-agent answers a request and closes, so there is no pool to corrupt: a dead
    call costs a reconnect, not a desynchronised stream. The subscription is the one
    connection held open, and it is owned by exactly one worker.
    """

    def __init__(self, socket_path: str | Path, *, request_timeout: float = 30.0):
        self.socket_path = str(Path(socket_path).expanduser())
        self.request_timeout = float(request_timeout)

    async def request(
        self,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
        expected: set[str] | frozenset[str] | None = None,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        effective = self.request_timeout if timeout is None else float(timeout)
        frame = self._encode({**payload, "id": request_id})
        reader, writer = await self._connect(effective)
        try:
            if len(frame) > MAX_FRAME_BYTES:
                raise MarmotError("agent control frame is too large", code="frame_too_large")
            writer.write(frame)
            await _drain(writer, effective)
            response = await _read_envelope(reader, effective)
        finally:
            await _close(writer)
        if response is None:
            raise MarmotError("agent control socket closed", code="socket_closed", retryable=True)
        if response.get("id") != request_id:
            raise MarmotError("agent control response id mismatch", code="id_mismatch")
        _raise_if_error(response)
        if expected and response.get("type") not in expected:
            raise MarmotError(
                f"unexpected agent control response {response.get('type')!r}",
                code="unexpected_response",
            )
        return response

    async def subscribe(
        self,
        *,
        account_id_hex: str | None = None,
        group_id_hex: str | None = None,
        on_ack: Callable[[], Any] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield control events until the daemon closes the stream.

        `on_ack` fires once the subscription is acknowledged — the first material
        proof the link is up, which is *not* the same thing as a message arriving.
        """
        request_id = uuid.uuid4().hex
        frame = self._encode({
            "type": "subscribe_inbound",
            "account_id_hex": _hex_or_none(account_id_hex, "account_id_hex"),
            "group_id_hex": _hex_or_none(group_id_hex, "group_id_hex"),
            "id": request_id,
        })
        reader, writer = await self._connect(self.request_timeout)
        try:
            writer.write(frame)
            await _drain(writer, self.request_timeout)
            ack = await _read_envelope(reader, self.request_timeout)
            if ack is None:
                raise MarmotError("agent control socket closed before subscribe ack",
                                  code="socket_closed", retryable=True)
            if ack.get("id") != request_id:
                raise MarmotError("subscribe ack id mismatch", code="id_mismatch")
            _raise_if_error(ack)
            if ack.get("type") != "ack":
                raise MarmotError(f"expected subscribe ack, got {ack.get('type')!r}",
                                  code="unexpected_response")
            if on_ack is not None:
                acknowledged = on_ack()
                if inspect.isawaitable(acknowledged):
                    await acknowledged
            while True:
                # No read timeout on the stream: an idle group is healthy, and a
                # timeout here would reconnect us in a loop.
                event = await _read_envelope(reader, None)
                if event is None:
                    return
                if event.get("id") != request_id:
                    raise MarmotError("inbound event id mismatch", code="id_mismatch")
                _raise_if_error(event)
                yield event
        finally:
            await _close(writer)

    # -- typed helpers ---------------------------------------------------
    async def account_list(self) -> dict[str, Any]:
        return await self.request({"type": "account_list"}, expected={"account_list"})

    async def group_info(self, account_id_hex: str, group_id_hex: str) -> dict[str, Any]:
        return await self.request(
            {
                "type": "group_info",
                "account_id_hex": _require_hex(account_id_hex, "account_id_hex"),
                "group_id_hex": _require_hex(group_id_hex, "group_id_hex"),
            },
            expected={"group_info"},
        )

    async def timeline_list(
        self,
        account_id_hex: str,
        group_id_hex: str,
        *,
        limit: int = 20,
        before: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return await self.request(
            {
                "type": "timeline_list",
                "account_id_hex": _require_hex(account_id_hex, "account_id_hex"),
                "group_id_hex": _require_hex(group_id_hex, "group_id_hex"),
                "before": before,
                "after": None,
                "before_inclusive": False,
                "limit": max(1, min(50, int(limit))),
            },
            expected={"timeline_page"},
        )

    async def send_final(
        self,
        account_id_hex: str,
        group_id_hex: str,
        text: str,
        *,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": "send_final",
            "account_id_hex": _require_hex(account_id_hex, "account_id_hex"),
            "group_id_hex": _require_hex(group_id_hex, "group_id_hex"),
            "text": str(text or ""),
            "reply_to_message_id_hex": reply_to,
        }
        # A retry that reuses the key is deduped by the connector instead of
        # posting an unrecallable second message.
        key = str(idempotency_key or "").strip()
        if key:
            payload["idempotency_key"] = key
        return await self.request(payload, expected={"final_sent"})

    # -- plumbing --------------------------------------------------------
    def _encode(self, payload: dict[str, Any]) -> bytes:
        envelope = {"marmot_agent_control": PROTOCOL, **payload}
        try:
            return json.dumps(envelope, separators=(",", ":")).encode("utf-8") + b"\n"
        except (TypeError, ValueError) as exc:
            raise MarmotError(f"cannot encode agent control request: {exc}", code="bad_request") from exc

    async def _connect(self, timeout: float) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        try:
            return await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path, limit=MAX_FRAME_BYTES + 1),
                timeout=timeout,
            )
        except asyncio.TimeoutError as exc:
            raise MarmotError(f"timed out connecting to {self.socket_path}",
                              code="timeout", retryable=True) from exc
        except OSError as exc:
            raise MarmotError(f"cannot reach wn-agent at {self.socket_path}: {exc}",
                              code="socket_io", retryable=True) from exc


async def _read_envelope(
    reader: asyncio.StreamReader, timeout: float | None
) -> dict[str, Any] | None:
    try:
        if timeout is None:
            raw = await reader.readuntil(b"\n")
        else:
            raw = await asyncio.wait_for(reader.readuntil(b"\n"), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MarmotError("timed out reading agent control response",
                          code="timeout", retryable=True) from exc
    except asyncio.LimitOverrunError as exc:
        raise MarmotError("agent control frame is too large", code="frame_too_large") from exc
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise MarmotError("agent control frame is incomplete", code="malformed_frame") from exc
    if len(raw) > MAX_FRAME_BYTES:
        raise MarmotError("agent control frame is too large", code="frame_too_large")
    if raw in (b"\n", b""):
        raise MarmotError("agent control frame is empty", code="malformed_frame")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MarmotError("agent control frame is malformed", code="malformed_frame") from exc
    if not isinstance(envelope, dict):
        raise MarmotError("agent control frame must be a JSON object", code="malformed_frame")
    if envelope.get("marmot_agent_control") != PROTOCOL:
        raise MarmotError(
            f"wrong agent control protocol: {envelope.get('marmot_agent_control')!r}",
            code="wrong_protocol",
        )
    return envelope


async def _drain(writer: asyncio.StreamWriter, timeout: float) -> None:
    try:
        await asyncio.wait_for(writer.drain(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise MarmotError("timed out writing agent control request",
                          code="timeout", retryable=True) from exc


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
    except (asyncio.TimeoutError, OSError):
        pass


def _raise_if_error(envelope: dict[str, Any]) -> None:
    if envelope.get("type") != "error":
        return
    retryable = envelope.get("retryable")
    code = str(envelope.get("code") or "agent_control_error")
    raise MarmotError(
        str(envelope.get("message") or "agent control error"),
        code=code,
        retryable=retryable if isinstance(retryable, bool) else code == "send_in_progress",
    )


def _hex_or_none(value: str | None, field: str) -> str | None:
    return _require_hex(value, field) if value else None


def _require_hex(value: str | None, field: str) -> str:
    text = str(value or "").strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text:
        raise MarmotError(f"{field} is required", code="invalid_hex")
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise MarmotError(f"{field} must be hexadecimal", code="invalid_hex") from exc
    return text


# -- identity --------------------------------------------------------------


def resolve_account_id(home: str | Path, label: str | None = None) -> str:
    """Read the account id from wn-agent's own account file.

    There is exactly one account per daemon (one socket = one account), so a
    directory with several accounts and no `label` is an ambiguity to report, never
    a guess: guessing means signing group traffic as the wrong identity.
    """
    root = Path(home).expanduser() / "accounts"
    if label:
        candidates = [root / label / "account.json"]
    else:
        candidates = sorted(root.glob("*/account.json"))
    if not candidates:
        raise MarmotError(f"no wn-agent account under {root}", code="no_account")
    if len(candidates) > 1:
        names = ", ".join(p.parent.name for p in candidates)
        raise MarmotError(
            f"{len(candidates)} wn-agent accounts found ({names}); set marmot.label",
            code="ambiguous_account",
        )
    try:
        data = json.loads(candidates[0].read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise MarmotError(f"cannot read {candidates[0]}: {exc}", code="no_account") from exc
    account = str(data.get("account_id_hex") or "").strip().lower()
    if not account:
        raise MarmotError(f"{candidates[0]} has no account_id_hex", code="no_account")
    _require_hex(account, "account_id_hex")
    return account


# -- plane -----------------------------------------------------------------


class MarmotPlane:
    """Inbound normalisation, outbound sends and the two reads a resolver needs."""

    def __init__(
        self,
        store: Store,
        policy: Policy,
        *,
        control: MarmotControl,
        account_id_hex: str,
    ):
        self.store = store
        self.policy = policy
        self.control = control
        # An empty identity would address every request at nobody; refuse it at
        # construction rather than letting wn-agent answer for the wrong account.
        self.account_id_hex = _require_hex(account_id_hex, "account_id_hex")
        self._direct: dict[str, bool] = {}

    # -- inbound ---------------------------------------------------------
    def handle(self, event: dict[str, Any]) -> Inbound | None:
        """Normalise one `inbound_message` control event, or return None.

        Returns None for anything that must not wake the agent: our own echo, a
        duplicate id, or a non-message event. The policy still runs for strangers —
        reaching the agent as data (`authority: none`) is the point of the two
        dimensions in spec §17.
        """
        if event.get("type") != "inbound_message":
            return None
        message = event.get("message")
        sender_block = message.get("sender") if isinstance(message, dict) else None
        if not isinstance(message, dict) or not isinstance(sender_block, dict):
            log.info("marmot inbound ignored: malformed event")
            return None
        message_id = str(message.get("message_id_hex") or "").strip().lower()
        sender = str(sender_block.get("account_id_hex") or "").strip().lower()
        group = str(event.get("group_id_hex") or "").strip().lower()
        if not message_id or not sender or not group:
            log.info("marmot inbound ignored: missing ids")
            return None
        if sender_block.get("is_self") or sender == self.account_id_hex:
            return None
        text = str(message.get("text") or "")
        # A leading `/queue` or `/guide` is a routing request, not words for the policy's
        # echo and quota checks to weigh (addressing.py).
        _, words = addressing.split(text)
        decision = self.policy.decide(
            sender=sender,
            kind=APP_MESSAGE_KIND,
            mention=bool(event.get("mentions_self")),
            dm=bool(self._direct.get(group)),
            event_id=message_id,
            text=words,
        )
        if decision.duplicate:
            return None
        mode, content = addressing.resolve(text, decision)
        inbound = Inbound(
            id=message_id,
            wrapper_id=message_id,
            sender=sender,
            kind=APP_MESSAGE_KIND,
            created_at=int(message.get("recorded_at") or 0),
            content=content,
            conversation=group,
            reply_to=_reply_target(event, message),
            plane="marmot",
            reach=decision.reach,
            mode=mode,
            authority=decision.authority,
            rule=decision.rule,
            why=decision.why,
            blocked_reason=decision.blocked_reason,
            interval_s=decision.interval_s,
        )
        if not self.store.add(inbound):
            return None
        log.info("marmot inbound %s… from %s… (%s/%s)", message_id[:10], sender[:10],
                 decision.reach, decision.authority)
        return inbound

    # -- outbound --------------------------------------------------------
    async def send(
        self,
        group_id_hex: str,
        text: str,
        *,
        reply_to: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        group = _require_hex(group_id_hex, "group_id_hex")
        if not str(text or "").strip():
            raise MarmotError("nothing to send: text is empty", code="bad_request")
        key = str(idempotency_key or "").strip() or uuid.uuid4().hex
        response = await self.control.send_final(
            self.account_id_hex, group, text, reply_to=reply_to, idempotency_key=key
        )
        ids = [str(i).strip().lower() for i in response.get("message_ids_hex") or [] if str(i).strip()]
        if not ids:
            raise MarmotError("wn-agent accepted the send but returned no message id",
                              code="no_message_id")
        # Record the send next to inbound rows: a one-sided history cannot answer
        # "did my reply go out?" from evidence.
        self.store.add(Inbound(
            id=ids[0], wrapper_id=ids[0], sender=self.account_id_hex, kind=APP_MESSAGE_KIND,
            created_at=int(time.time()), content=str(text), conversation=group,
            reply_to=reply_to, plane="marmot", reach="sent", authority="self",
            rule="outbound", why="sent by us",
        ))
        self.store.mark_delivered([ids[0]])
        log.info("marmot sent %s… to %s…", ids[0][:10], group[:10])
        return {"group": group, "message_id": ids[0], "message_ids": ids,
                "idempotency_key": key}

    # -- reads -----------------------------------------------------------
    async def group(self, group_id_hex: str) -> dict[str, Any]:
        group = _require_hex(group_id_hex, "group_id_hex")
        response = await self.control.group_info(self.account_id_hex, group)
        info = {
            "group": group,
            "member_count": int(response.get("member_count") or 0),
            "is_direct": bool(response.get("is_direct")),
            "agent_created": bool(response.get("agent_created")),
        }
        # Knowing a group is a 1:1 lets the policy treat it like a DM.
        self._direct[group] = info["is_direct"]
        return info

    async def timeline(
        self, group_id_hex: str, *, limit: int = 20, before: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        group = _require_hex(group_id_hex, "group_id_hex")
        response = await self.control.timeline_list(
            self.account_id_hex, group, limit=limit, before=before
        )
        return {
            "group": group,
            "messages": [_normalise_timeline_message(row) for row in response.get("messages") or []],
        }


class MarmotResolver:
    """Turn a socket target into a group id, and prove the group exists.

    A 32-hex value cannot be a Nostr pubkey (32 bytes), so classification needs no
    lookup — but sending into a group id nobody has is a silent failure the caller
    cannot see, so a resolved target is always verified with `group_info` once and
    cached.

    A cache alone cannot answer "what is attached?" after a restart, which is why
    `conversations()` is built from the messages this gateway has stored and
    `enumerate()` re-verifies them on every connect. wn-agent 0.10.4's control
    protocol has no group-list request — an unknown `group_list` frame is not even
    answered — so a group this gateway has never exchanged a message with cannot be
    discovered before its first message. That limit is reported, not hidden behind an
    empty list.
    """

    def __init__(self, plane: MarmotPlane, *, prefix: str = "marmot:",
                 history_limit: int = 50):
        self.plane = plane
        self.prefix = prefix
        self.history_limit = max(1, int(history_limit))
        self._groups: dict[str, dict[str, Any]] = {}
        #: Groups the last enumeration could not verify, and why.
        self.errors: dict[str, str] = {}
        self.last_enumeration: int | None = None
        self.last_groups = 0
        self.last_verified = 0

    def parse(self, target: str | None) -> str | None:
        text = str(target or "").strip()
        if text.lower().startswith(self.prefix):
            value = text[len(self.prefix):].strip().lower()
            return value or None
        if len(text) == GROUP_ID_HEX_LEN and _is_hex(text):
            return text.lower()
        return None

    async def resolve(self, target: str | None) -> dict[str, Any]:
        group = self.parse(target)
        if group is None:
            raise MarmotError(
                f"{target!r} is not a marmot group (use marmot:<group id>)",
                code="invalid_target",
            )
        cached = self._groups.get(group)
        if cached is not None:
            return cached
        info = await self.plane.group(group)
        self._groups[group] = info
        return info

    def conversations(self) -> list[dict[str, Any]]:
        """Every group this plane has evidence for, newest activity first.

        Evidence is a stored Marmot row (inbound or our own send) or a group
        resolved since this process started. `verified` says whether the last
        `group_info` succeeded, so history for a group we have left is named as
        unverified instead of silently vanishing.
        """
        rows = self.plane.store.conversations("marmot", limit=self.history_limit)
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            group = str(row["conversation"])
            seen.add(group)
            out.append(self._entry(group, messages=int(row["messages"]),
                                   last_at=int(row["last_at"] or 0)))
        for group in sorted(self._groups):
            if group not in seen:
                out.append(self._entry(group, messages=0, last_at=None))
        return out

    def enumeration(self) -> dict[str, Any]:
        """What the last connect-time enumeration tried, proved and failed."""
        return {
            "at": self.last_enumeration,
            "groups": self.last_groups,
            "verified": self.last_verified,
            "errors": dict(self.errors),
            "source": (
                "stored marmot conversations, each verified with group_info; "
                "wn-agent 0.10.4 has no group-list request, so a group this gateway "
                "has never exchanged a message with cannot be listed"
            ),
        }

    async def enumerate(self, extra: Iterable[str] = ()) -> dict[str, Any]:
        """Verify every group the plane has evidence for, once per connect.

        `extra` is the worker's configured subscription group, which is attached
        even when this gateway has not stored a message from it yet. A group that
        fails `group_info` is recorded and left unverified: dropping the error
        would make a dead conversation look like a quiet one.
        """
        groups: list[str] = [
            str(row["conversation"])
            for row in self.plane.store.conversations("marmot", limit=self.history_limit)
        ]
        for value in extra:
            group = str(value or "").strip().lower()
            if group and group not in groups:
                groups.append(group)
        self.last_enumeration = int(time.time())
        self.last_groups = len(groups)
        self.last_verified = 0
        self.errors = {}
        for group in groups:
            try:
                info = await self.plane.group(group)
            except MarmotError as exc:
                detail = f"{exc.code}: {exc}"
            except Exception as exc:  # noqa: BLE001 - one bad group must not stop the rest
                detail = f"{type(exc).__name__}: {exc}"
            else:
                self._groups[group] = info
                self.last_verified += 1
                continue
            self.errors[group] = detail[:200]
            self._groups.pop(group, None)
            log.info("marmot conversation %s… unverified: %s", group[:10], detail)
        if groups:
            log.info("marmot conversations enumerated: %d/%d verified",
                     self.last_verified, self.last_groups)
        return self.enumeration()

    def _entry(self, group: str, *, messages: int,
               last_at: int | None) -> dict[str, Any]:
        cached = self._groups.get(group)
        return {
            "group": group,
            "messages": messages,
            "last_at": last_at,
            "verified": cached is not None,
            "member_count": int(cached["member_count"]) if cached else None,
            "is_direct": bool(cached["is_direct"]) if cached else None,
            "agent_created": bool(cached["agent_created"]) if cached else None,
            "error": self.errors.get(group),
        }


class MarmotWorker:
    """One long-lived inbound subscription, restarted with backoff.

    Counters are exposed through `status` because a process snapshot cannot tell a
    reconnecting worker from a dead one, and silence after a reconnect is exactly
    what a healthy link looks like.
    """

    def __init__(
        self,
        plane: MarmotPlane,
        *,
        resolver: MarmotResolver | None = None,
        group_id_hex: str | None = None,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
    ):
        self.plane = plane
        #: Optional: when present, every (re)connect re-states which groups we hold
        #: messages for, so `status` can answer before the next event arrives.
        self.resolver = resolver
        self.group_id_hex = group_id_hex
        self.base_delay = float(base_delay)
        self.max_delay = float(max_delay)
        self.events = 0
        self.reconnects = 0
        self.last_error: str | None = None
        self.connected = False
        self.connected_at: int | None = None
        self.enumerated: dict[str, Any] | None = None
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    @property
    def relay(self) -> str:
        """Label used by the gateway's task guard and status output."""
        return "marmot"

    def _established(self) -> None:
        self.connected = True
        self.connected_at = int(time.time())
        log.info("marmot inbound established (account %s…)", self.plane.account_id_hex[:12])

    async def _acknowledged(self) -> None:
        """The link is up; say which conversations are attached as well.

        Enumeration runs on the ack rather than on a timer because this is the
        moment a restarted worker has an empty cache and `status` would otherwise
        report `conversations: []` to a question the store can answer. It is status
        enrichment, not the link: a failure here must not drop the subscription.
        """
        self._established()
        if self.resolver is None:
            return
        extra = [self.group_id_hex] if self.group_id_hex else []
        try:
            self.enumerated = await self.resolver.enumerate(extra)
        except Exception:  # noqa: BLE001 - never let a status nicety kill the link
            log.exception("marmot conversation enumeration failed")

    async def run(self, on_message: MessageHandler) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                async for event in self.plane.control.subscribe(
                    account_id_hex=self.plane.account_id_hex,
                    group_id_hex=self.group_id_hex,
                    on_ack=self._acknowledged,
                ):
                    if self.connected is False:  # pragma: no cover - defensive
                        self._established()
                    if event.get("type") != "inbound_message":
                        log.debug("marmot control event %s ignored", event.get("type"))
                        continue
                    self.events += 1
                    attempt = 0
                    outcome = on_message(event)
                    if inspect.isawaitable(outcome):
                        await outcome
                self.last_error = "subscription closed by wn-agent"
            except asyncio.CancelledError:
                raise
            except MarmotError as exc:
                self.last_error = f"{exc.code}: {exc}"
                log.warning("marmot inbound failed (%s), reconnecting", exc)
            except Exception as exc:  # noqa: BLE001 - the worker must not die silently
                self.last_error = f"{type(exc).__name__}: {exc}"[:200]
                log.exception("marmot inbound worker error")
            finally:
                self.connected = False
            if self._stop.is_set():
                break
            self.reconnects += 1
            delay = min(self.max_delay, self.base_delay * (2 ** attempt))
            delay *= 0.5 + random.random() / 2  # jitter, so a restart is not a thundering herd
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
            attempt += 1

    def stats(self) -> dict[str, Any]:
        return {
            "plane": "marmot",
            "account": self.plane.account_id_hex,
            "socket": self.plane.control.socket_path,
            "group": self.group_id_hex,
            "connected": self.connected,
            "connected_at": self.connected_at,
            "events": self.events,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }


# -- helpers ---------------------------------------------------------------


def _reply_target(event: dict[str, Any], message: dict[str, Any]) -> str | None:
    reply = event.get("reply_to")
    if isinstance(reply, dict) and reply.get("message_id_hex"):
        return str(reply["message_id_hex"]).lower()
    value = message.get("reply_to_message_id_hex") or event.get("reply_to_message_id_hex")
    return str(value).lower() if value else None


def _normalise_timeline_message(row: dict[str, Any]) -> dict[str, Any]:
    raw_sender = row.get("sender")
    sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
    return {
        "id": str(row.get("message_id_hex") or ""),
        "sender": str(sender.get("account_id_hex") or ""),
        "is_self": bool(sender.get("is_self")),
        "direction": str(row.get("direction") or ""),
        "kind": int(row.get("kind") or APP_MESSAGE_KIND),
        "created_at": int(row.get("recorded_at") or 0),
        "content": str(row.get("text") or ""),
        "reply_to": row.get("reply_to_message_id_hex"),
    }


def _is_hex(text: str) -> bool:
    try:
        bytes.fromhex(text)
    except ValueError:
        return False
    return True
