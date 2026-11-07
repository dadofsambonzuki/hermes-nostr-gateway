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

This is the only write path. The `nak event --auth` child is deleted, so there is no
second publisher for a config flag to select and no rollback to keep in step.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import enum
import json
import logging
from typing import Any, Iterable, Protocol

import nostr_sdk as sdk

from . import sdkloop
from .crypto import parse_secret

log = logging.getLogger("hng.publish")

#: How long one relay gets to answer with an OK. The send is per relay, so a dead relay
#: costs this once -- not once per relay in the set, because the sends run concurrently.
OK_TIMEOUT = 10.0

#: Relay answers that are a *judgement* on the event or on this key. The same signed bytes
#: meet the same answer however often they are sent, so there is nothing a retry can change
#: and an advertisement records one instead of trying again. `auth-required` is a refusal
#: here because this client answers AUTH in-process: an answer that survives that is about
#: the key, not about the challenge. (`authentication failed` is *not* here -- it is the
#: client saying the relay wants AUTH, which the next send answers.)
_REFUSALS = ("blocked", "invalid", "restricted", "not allowed", "banned", "pow:",
             "auth-required")

#: Relay answers that are a *condition*: the relay is throttling, busy or shutting down and
#: the same bytes are expected to land once it clears. A retry costs a backoff, never a new
#: event -- and these must never be recorded as refusals, which is how a rate-limited relay
#: used to stop being retried for good.
_TRANSIENT = ("rate-limit", "rate limited", "error:", "unavailable", "busy",
              "temporarily", "overloaded", "timeout", "try again")

#: The relay already stores this exact event id (NIP-01 `duplicate:`): an idempotent
#: success, not a failure -- the event is there, which is all the caller wanted.
_HELD = ("duplicate", "already have", "already exists")


def _norm(relay: str) -> str:
    """Compare relay URLs the way the library does: no trailing slash, lowercase."""
    return str(relay).strip().rstrip("/").lower()


class Outcome(enum.Enum):
    """What one relay's answer to one event means for the next attempt."""

    HELD = "held"            # already stored: nothing left to do at this relay
    REFUSED = "refused"      # a judgement: this key or event will never be accepted
    TRANSIENT = "transient"  # a condition: the same bytes are worth another send


def classify(reason: str | None) -> Outcome:
    """Classify a *failed* relay answer: a refusal, a transient condition, or already held.

    Checked in that order deliberately -- `duplicate` first, because being already stored is
    a success however the relay words it, and `rate-limit` never means "no". An answer that
    matches nothing is transient: a dropped socket or a new relay phrase is likelier than a
    judgement, and mis-reading one as a refusal cannot be undone (the event is never tried
    again), while mis-reading a refusal as transient only costs a bounded retry.
    """
    text = (reason or "").strip().lower()
    if any(word in text for word in _HELD):
        return Outcome.HELD
    if any(word in text for word in _TRANSIENT):
        return Outcome.TRANSIENT
    if any(word in text for word in _REFUSALS):
        return Outcome.REFUSED
    return Outcome.TRANSIENT


def _retryable(reason: str | None) -> bool:
    return classify(reason) is Outcome.TRANSIENT


def is_permanent_refusal(reason: str | None) -> bool:
    """True when a relay's answer is a *judgement*, not a condition.

    `blocked`, `invalid`, `restricted`, `not allowed`, a failed AUTH and friends will not
    change on a retry. For an advertisement that means this relay will never carry this
    kind, which the caller records instead of retrying on every start. A rate limit is not
    one of these: it is a condition, and recording it as a refusal is what stopped those
    relays from ever being retried (see `docs/decisions.md`).
    """
    return classify(reason) is Outcome.REFUSED


def is_already_held(reason: str | None) -> bool:
    """True when the relay answered that it already stores this event id."""
    return classify(reason) is Outcome.HELD


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
        event loop" and a restricted relay closes the write.

        `ensure`, not a bare set: a cold fetch in another thread may have left the
        process-global binding on a loop it has since closed (see `sdkloop`).
        """
        sdkloop.ensure()

    async def _ensure(self, relay: str) -> Any:
        """Add and connect a relay once, then reuse its connection."""
        key = _norm(relay)
        url = self._urls.get(key)
        if url is not None:
            return url
        sdkloop.ensure()
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
        sdkloop.ensure()
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


def make_publisher(cfg: dict[str, Any], secret: str | None) -> Publisher:
    """Build the publisher: one client, with the configured publish relays pinned.

    There is no transport switch. A `transport` block left in an older config file is
    ignored -- the `nak` publisher it used to select is deleted -- so an upgrade does not
    need a config edit before the daemon starts.
    """
    relays = list((cfg.get("relays") or {}).get("publish") or [])
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

    return sdkloop.run_isolated(run())
