"""Publishing through the bindings: one signed event, one client, per-relay truth.

Why this exists
---------------
The first publisher spawned `nak event --auth` once per relay per message. That put
NIP-42 in another program's environment, made every result a line of stdout to regex,
made a retry a new process, and left one child per message per relay in `ps`.

What must not change is the *event*: it is signed exactly once, upstream, by the plane
(`crypto.wrap`). A retry -- here, or from `store.pending_wraps` in the daemon -- resends
those same bytes. Re-wrapping would produce a new id and show the peer a duplicate
(spec §7). So this module takes a signed event dict, parses it once, and never signs.

NIP-42 detail: the client authenticates with our own key through an `Authenticator`, so
the secret never reaches a child process's environment. A relay that requires AUTH sends
its challenge around the first EVENT and the client answers it in-process; the event is
accepted on the next send. That is why a failed relay is retried once inside the same
call, and only then left to the durable retry loop.

The `nak` child is kept for one release behind `transport.legacy: true` -- the same flag
that keeps `relay.RelayWorker` in charge of reads (plan P1) -- so one config line rolls
both transports back. Nothing else may use it.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import shutil
import subprocess
from typing import Any, Iterable, Protocol

import nostr_sdk as sdk

from .crypto import parse_secret

log = logging.getLogger("hng.publish")

#: How long one relay gets to answer with an OK. The send is per relay, so a dead relay
#: costs this once -- not once per relay in the set, because the sends run concurrently.
OK_TIMEOUT = 10.0

#: Relay answers that are a decision, not a fault. Retrying one in-process only spends
#: the relay's patience; everything else (a timeout, a dropped socket, an auth challenge
#: we had not answered yet) is worth one more send of the same event.
_DECISIONS = ("blocked", "invalid", "rate-limit", "pow:", "restricted",
              "not allowed", "banned", "duplicate", "error:")


def _norm(relay: str) -> str:
    """Compare relay URLs the way the library does: no trailing slash, lowercase."""
    return str(relay).strip().rstrip("/").lower()


def _retryable(reason: str | None) -> bool:
    text = (reason or "").lower()
    return not any(word in text for word in _DECISIONS)


class Publisher(Protocol):
    """What the gateway and the CLI need: per-relay results, never a re-wrap."""

    async def publish(self, event: dict, relays: Iterable[str]) -> dict[str, tuple[bool, str | None]]: ...

    async def warm(self, relays: Iterable[str]) -> None: ...

    async def aclose(self) -> None: ...

    def stats(self) -> dict[str, Any]: ...


class BindingsPublisher:
    """One `nostr-sdk` client; every relay send is a signed event, already in hand.

    The client is built once and kept: a message is published many times a day and a
    WebSocket handshake per relay per message is exactly the cost this change removes.
    Relays are added on demand (a recipient's 10050 list is per message) and the set is
    bounded, because a persistent client that accumulates a socket per peer eventually
    runs out of file descriptors.
    """

    mode = "bindings"

    def __init__(self, secret: str | None = None, *, client: Any | None = None,
                 ok_timeout: float = OK_TIMEOUT, attempts: int = 2,
                 retry_delay: float = 0.5, max_relays: int = 24,
                 pinned: Iterable[str] = ()):
        self._secret = secret
        self._client = client
        self._owns_client = client is None
        self.ok_timeout = dt.timedelta(seconds=max(0.1, float(ok_timeout)))
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.max_relays = max(1, int(max_relays))
        self.pinned = {_norm(relay) for relay in pinned}
        self._urls: dict[str, Any] = {}
        self._last_used: dict[str, float] = {}
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    # -- client ---------------------------------------------------------

    @property
    def client(self) -> Any:
        """The real client, built on first use so constructing a publisher touches nothing."""
        if self._client is None:
            keys = sdk.Keys.parse(parse_secret(self._secret)) if self._secret else None
            builder = sdk.ClientBuilder()
            if keys is not None:
                # NIP-42 signs as us, in this process. Without an authenticator a
                # restricted relay closes the write as unauthenticated.
                builder = builder.authenticator(sdk.SignerAuthenticator(keys))
            self._client = builder.build()
        return self._client

    def _bind_loop(self) -> None:
        """uniffi calls our authenticator from a Rust worker thread, where there is no
        running loop: bind it explicitly or every NIP-42 attempt dies with "no running
        event loop" and a restricted relay closes the write."""
        set_loop = getattr(sdk, "uniffi_set_event_loop", None)
        if set_loop is not None:
            set_loop(asyncio.get_running_loop())

    async def _ensure(self, relay: str) -> Any:
        """Add and connect a relay once, then reuse its connection."""
        key = _norm(relay)
        url = self._urls.get(key)
        if url is not None:
            return url
        url = sdk.RelayUrl.parse(relay)
        self._urls[key] = url
        await self.client.add_relay(url)
        # Non-blocking on purpose: an unreachable relay must not delay the message to
        # the others, and `send_event` reports it as a per-relay failure instead.
        await self.client.connect_relay(url)
        return url

    async def warm(self, relays: Iterable[str]) -> None:
        """The configured publish set: connected at startup, never pruned."""
        self.pinned |= {_norm(relay) for relay in relays}
        self._bind_loop()
        for relay in relays:
            try:
                await self._ensure(relay)
            except Exception as exc:  # noqa: BLE001 - warm, not required
                log.warning("could not add publish relay %s: %s", relay, exc)

    # -- publish --------------------------------------------------------

    async def _send(self, event: sdk.Event, url: Any) -> tuple[bool, str | None]:
        """One send to one relay; the event object is passed straight through."""
        try:
            out = await self.client.send_event(
                event, target=sdk.SendEventTarget.to([url]),
                ack_policy=sdk.AckPolicy.all(), ok_timeout=self.ok_timeout,
            )
        except Exception as exc:  # noqa: BLE001 - per-relay failure, never fatal
            return False, f"{type(exc).__name__}: {exc}"[:200]
        if out.success:
            return True, None
        if out.failed:
            # One target, so there is at most one entry: the relay's own reason, or
            # the client's transport reason ('timeout', 'authentication failed').
            reason = next(iter(out.failed.values()))
            return False, str(reason)[:200]
        return False, "no ack"

    async def _publish_one(self, event: sdk.Event, relay: str) -> tuple[bool, str | None]:
        url = await self._ensure(relay)
        ok, reason = await self._send(event, url)
        for attempt in range(1, self.attempts):
            if ok or not _retryable(reason):
                break
            log.debug("%s: %s; resending the same event (attempt %d/%d)",
                      relay, reason, attempt + 1, self.attempts)
            await asyncio.sleep(self.retry_delay)
            ok, reason = await self._send(event, url)
        return ok, reason

    async def publish(self, event: dict,
                      relays: Iterable[str]) -> dict[str, tuple[bool, str | None]]:
        """Send one already-signed event to each relay; the keys are the caller's strings."""
        targets = list(dict.fromkeys(str(relay).strip() for relay in relays if str(relay).strip()))
        if not targets:
            return {}
        try:
            signed = sdk.Event.from_json(json.dumps(event))
            valid = signed.verify()
        except Exception as exc:  # noqa: BLE001 - malformed input is a delivery error
            self.last_error = f"unparseable event: {exc}"[:200]
            self.failed += len(targets)
            return {relay: (False, self.last_error) for relay in targets}
        if not valid:
            # Never let a bad signature leave this process: a relay would reject it
            # anyway, and a peer would see nonsense attributed to us.
            self.last_error = "event failed local verification"
            self.failed += len(targets)
            log.error("refusing to publish an event that does not verify")
            return {relay: (False, self.last_error) for relay in targets}

        self._bind_loop()
        results = await asyncio.gather(
            *(self._publish_one(signed, relay) for relay in targets))
        out = dict(zip(targets, results, strict=True))
        for relay, (ok, reason) in out.items():
            if ok:
                self.sent += 1
            else:
                self.failed += 1
                self.last_error = f"{relay}: {reason}"[:300]
                log.warning("%s: publish failed: %s", relay, reason)
            self._last_used[_norm(relay)] = asyncio.get_running_loop().time()
        await self._prune(protect={_norm(relay) for relay in targets})
        return out

    async def _prune(self, protect: set[str] | None = None) -> None:
        """Keep the relay set bounded: drop the least recently used, never a pinned one."""
        keep = self.pinned | (protect or set())
        while len(self._urls) > self.max_relays:
            candidates = [key for key in self._urls if key not in keep]
            if not candidates:
                return
            oldest = min(candidates, key=lambda key: self._last_used.get(key, 0.0))
            url = self._urls.pop(oldest)
            self._last_used.pop(oldest, None)
            log.debug("pruning relay %s from the publish client", oldest)
            try:
                await self.client.remove_relay(url, force=True)
            except Exception as exc:  # noqa: BLE001 - pruning must not fail a send
                log.debug("could not remove relay %s: %s", oldest, exc)

    # -- bookkeeping ----------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "relays": len(self._urls), "sent": self.sent,
                "failed": self.failed, "last_error": self.last_error}

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.shutdown()
            except Exception as exc:  # noqa: BLE001 - shutdown is best effort
                log.debug("client shutdown: %s", exc)
            self._client = None
            self._urls.clear()
            self._last_used.clear()


class NakPublisher:
    """The `nak event --auth` child per relay -- the rollback, removed next release.

    Selected by `transport.legacy: true`. It is kept only so a relay that behaves
    differently under the bindings can be compared side by side; new code must not call
    it, and `tests/test_publisher.py` keeps it honest while it exists.
    """

    mode = "legacy"

    def __init__(self, secret: str | None = None, *, nak: str | None = None):
        self._secret = secret
        self._nak = nak or shutil.which("nak") or "nak"
        self.sent = 0
        self.failed = 0
        self.last_error: str | None = None

    async def warm(self, relays: Iterable[str]) -> None:
        return None

    async def publish(self, event: dict,
                      relays: Iterable[str]) -> dict[str, tuple[bool, str | None]]:
        return await asyncio.get_running_loop().run_in_executor(
            None, self._publish_blocking, event, list(relays))

    def _publish_blocking(self, event: dict,
                          relays: list[str]) -> dict[str, tuple[bool, str | None]]:
        import os
        payload = json.dumps(event)
        env = dict(os.environ, **({"NOSTR_SECRET_KEY": self._secret} if self._secret else {}))
        out: dict[str, tuple[bool, str | None]] = {}
        for relay in relays:
            proc = subprocess.run(
                [self._nak, "event", "--auth", relay], input=payload,
                capture_output=True, text=True, env=env,
            )
            text = (proc.stdout + proc.stderr).lower()
            ok = "success" in text
            if ok:
                self.sent += 1
                out[relay] = (True, None)
                continue
            detail = (proc.stderr or proc.stdout).strip()
            error = detail.splitlines()[-1][:200] if detail else "no response"
            self.failed += 1
            self.last_error = f"{relay}: {error}"[:300]
            out[relay] = (False, error)
        return out

    def stats(self) -> dict[str, Any]:
        return {"mode": self.mode, "relays": 0, "sent": self.sent,
                "failed": self.failed, "last_error": self.last_error}

    async def aclose(self) -> None:
        return None


def make_publisher(cfg: dict[str, Any], secret: str | None) -> Publisher:
    """Pick the transport: the bindings, or (one release) the `nak` children."""
    transport = cfg.get("transport") or {}
    relays = list((cfg.get("relays") or {}).get("publish") or [])
    if transport.get("legacy"):
        log.warning("transport.legacy: publishing through `nak event --auth` children; "
                    "this path is removed in the next release")
        return NakPublisher(secret)
    return BindingsPublisher(secret, pinned=relays)


def publish(event: dict, relays: Iterable[str],
            secret: str | None = None) -> dict[str, tuple[bool, str | None]]:
    """One-shot publish for `hng send` and the probes: a client for this call, then closed.

    The daemon holds a `BindingsPublisher` instead, so a message does not pay for a
    handshake per relay. Same code path, same per-relay answers.
    """
    async def run() -> dict[str, tuple[bool, str | None]]:
        publisher = BindingsPublisher(secret)
        try:
            return await publisher.publish(event, relays)
        finally:
            await publisher.aclose()

    return asyncio.run(run())
