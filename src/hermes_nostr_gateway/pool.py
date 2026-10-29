"""Read transport on `nostr-sdk`'s relay pool: one client, one subscription per filter.

Why this exists
---------------
The first transport was one `nak req --stream` child per relay. That gave per-relay
coverage, but the cost was a subprocess fleet: NIP-42 lifecycle lived in another
program's stderr, a reconnect meant respawning a process, a refusal could only be read
by regexing that stderr, and every relay's socket was a separate pgid to unwind.

This module keeps the same *observable* surface — per-worker counters and
`live_state()`, so `hng status` and the health verdict do not change — but the I/O is
one `Client` in this process. Per relay we open one subscription per filter set (the
DM set, the feed set, the mentions set), the client answers NIP-42 itself through an
`Authenticator` backed by our own keys, and reconnection is the library's job (relay
options default to reconnect with a 30s retry interval, which is where a 429 backs off).

It is also the only read path: the per-relay `nak req --stream` children are gone, so
there is no second implementation to compare against and no flag that selects one.

What is *not* here: publishing (`publisher.py`) and the cold or one-shot reads that
`hng listen` is built from (`fetch.py`). Those are not subscriptions.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gc
import hashlib
import inspect
import json
import logging
import time
from typing import Any, Awaitable, Callable

import nostr_sdk as sdk

from . import sdkloop
from .crypto import parse_secret
from .relay import Handler, RefusalHandler, refused_kinds

log = logging.getLogger("hng.pool")

#: How long we ask the client to wait for a relay connection before giving up on the
#: attempt. A relay that is unreachable must not hold a worker's task open forever.
CONNECT_TIMEOUT = dt.timedelta(seconds=15)

#: A subscription the relay closed for a non-filter reason (auth-required, a restart)
#: is re-requested after this pause, rather than waiting for a reconnect event.
RESUBSCRIBE_DELAY = 5.0

#: How long one REQ may take before the worker treats it as failed and retries. Without
#: this a wedged subscribe leaves the relay reporting "authenticating" forever.
REQUEST_TIMEOUT_S = 20.0


def _norm(url: str) -> str:
    """Compare relay URLs the way the library does: one trailing slash, lowercase host."""
    return str(url).strip().rstrip("/").lower()


#: A CLOSED that is about authentication, in the several ways relays word it.
_AUTH_REQUIRED = ("auth-required", "auth required", "must be authenticated",
                  "must authenticate", "you must auth")


def _auth_required(text: str | None) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _AUTH_REQUIRED)


def resubscribe_delay(worker: "PoolWorker") -> float:
    """How long to wait before re-REQing a worker whose subscription was closed.

    Three cases, in order:

    * the relay asked for NIP-42 and has closed the REQ only once since the last
      delivery — the challenge just completed, so the next request should be accepted
      and waiting is a delay for nothing;
    * it has asked for NIP-42 and refused twice or more since the last delivery — that
      is a decision about this key, so back off on the streak, not on the general
      counter that a successful subscribe keeps resetting (a relay that accepts and then
      closes would otherwise be re-REQed every 0.2 s for ever, which is also what made a
      permanent refusal read as `authenticating` in `hng status`);
    * anything else — the general backoff, which the caller doubles each time.
    """
    if worker.auth_at and worker.auth_refusals <= 1:
        return 0.2
    if worker.auth_refusals >= 2:
        return min(RESUBSCRIBE_DELAY * 2 ** (worker.auth_refusals - 2), 300.0)
    return worker._resubscribe_delay or RESUBSCRIBE_DELAY


class KeysSigner(sdk.AsyncNostrSigner):
    """NIP-42 authentication in-process: the client signs the AUTH event with our keys.

    Without an authenticator the client cannot answer a challenge, and a restricted
    relay closes the subscription. `nak` used to do this by inheriting our secret in
    `NOSTR_SECRET_KEY`; here the key never leaves this process.
    """

    def __init__(self, keys: sdk.Keys):
        self._keys = keys

    async def get_public_key_async(self) -> sdk.PublicKey:
        return self._keys.public_key()

    async def sign_event_async(self, unsigned_event: sdk.UnsignedEvent) -> sdk.Event:
        return self._keys.sign_event(unsigned_event)

    async def nip04_encrypt_async(self, public_key: sdk.PublicKey, content: str) -> str:
        return self._keys.nip04_encrypt(public_key, content)

    async def nip04_decrypt_async(self, public_key: sdk.PublicKey, encrypted_content: str) -> str:
        return self._keys.nip04_decrypt(public_key, encrypted_content)

    async def nip44_encrypt_async(self, public_key: sdk.PublicKey, content: str) -> str:
        return self._keys.nip44_encrypt(public_key, content)

    async def nip44_decrypt_async(self, public_key: sdk.PublicKey, payload: str) -> str:
        return self._keys.nip44_decrypt(public_key, payload)


class PoolWorker:
    """One relay's subscription set, on the shared client.

    The counters and `live_state()` are the contract: `gateway.py` builds these from
    config, `_guarded` treats them uniformly, and `status`/`health` read the same fields
    for every relay.
    """

    def __init__(self, pool: "RelayPool", relay: str, me: str, *,
                 kinds: tuple[int, ...] = (1059,), authors: tuple[str, ...] = (),
                 p_tag: str | None = None, a_tags: tuple[str, ...] = (), auth: bool = True,
                 since_days: int = 3,
                 on_refusal: RefusalHandler | None = None, purpose: str = "dm"):
        self.pool = pool
        self.relay = relay
        self.purpose = purpose
        self.requested_kinds = tuple(dict.fromkeys(int(kind) for kind in kinds))
        if not self.requested_kinds:
            raise ValueError("PoolWorker needs at least one kind: an empty filter is a firehose")
        self.refused: set[int] = set()
        self.on_refusal = on_refusal
        self.authors = authors
        self.p_tag = p_tag
        #: `#a` addresses: what narrows a subscription to NIP-34 repositories (an issue or
        #: PR event names its repository with an `a` tag) without asking a relay for the
        #: whole kind. Empty means "no address filter", never "match nothing".
        self.a_tags = tuple(str(value).strip() for value in a_tags
                            if str(value or "").strip())
        self.auth = auth
        self.since_days = since_days
        self.me = (me or "").strip()
        if len(self.me) != 64:
            raise ValueError(
                "PoolWorker needs a 64-hex identity; refusing an unfiltered subscription")
        self.events = 0
        self.last_error: str | None = None
        self.started_at: float | None = None
        self.attempt_started_at: float | None = None
        self.attempts = 0
        self.connected_at: float | None = None
        self.auth_at: float | None = None
        #: A relay that answered NIP-42 and then closed the REQ anyway. That is a
        #: decision about this key (damus does it for some filters), not a handshake in
        #: flight: counted, so the backoff can escalate, and stamped, so `status` can
        #: name it instead of reporting "authenticating" for ever.
        self.auth_refusals = 0
        self.auth_refused_at: float | None = None
        self.last_event_at: float | None = None
        self.last_stderr: str | None = None
        self.rate_limited_at: float | None = None
        self._resubscribe_task: asyncio.Task[None] | None = None
        self._resubscribe_delay: float = 0.0
        self._stop = asyncio.Event()

    # -- identity ------------------------------------------------------

    @property
    def subscription_id(self) -> str:
        """A stable id per (relay, purpose): how a notification finds its worker."""
        digest = hashlib.sha1(f"{_norm(self.relay)}|{self.purpose}".encode()).hexdigest()[:12]
        return f"hng-{self.purpose}-{digest}"

    @property
    def kinds(self) -> tuple[int, ...]:
        return tuple(kind for kind in self.requested_kinds if kind not in self.refused)

    @property
    def alive(self) -> bool:
        """True while this worker has a live subscription on the client."""
        return self.subscription_id in self.pool.subscriptions

    def filter(self) -> sdk.Filter:
        """The REQ for what is left of the kind set — never an empty filter."""
        if not self.kinds:
            raise ValueError(
                f"refusing to subscribe to {self.relay} with no kinds: that is a firehose")
        filt = sdk.Filter().kinds([sdk.Kind(kind) for kind in self.kinds])
        if self.p_tag:
            filt = filt.custom_tag(sdk.SingleLetterTag.from_byte(ord("p")), self.p_tag)
        if self.a_tags:
            # One `#a` array: NIP-01 reads its values as alternatives, which is what "these
            # repositories" means. One array rather than one tag entry per address, so the
            # REQ a relay receives is the one an operator would write by hand.
            filt = filt.custom_tags(sdk.SingleLetterTag.from_byte(ord("a")),
                                    list(self.a_tags))
        for author in self.authors:
            filt = filt.author(sdk.PublicKey.parse(author))
        # Only the recent window: without this every resubscribe replays the relay's
        # whole history (NIP-59 backdates wraps up to 2 days, so 3 days is the floor).
        if self.since_days:
            filt = filt.since(sdk.Timestamp.from_secs(
                int(time.time()) - self.since_days * 86400))
        return filt

    # -- state ---------------------------------------------------------

    def note_refusals(self, text: str | None) -> set[int]:
        """Learn from a CLOSED message. Returns the kinds newly dropped."""
        dropped = refused_kinds(text) & set(self.requested_kinds) - self.refused
        if dropped:
            self.refused |= dropped
            log.warning("%s refuses kind(s) %s; resubscribing for %s",
                        self.relay, sorted(dropped), list(self.kinds) or "nothing")
            if self.on_refusal is not None:
                self.on_refusal(self.relay, dropped, self.kinds, self.last_error)
        return dropped

    def note_auth(self) -> None:
        self.auth_at = time.time()

    def note_auth_refusal(self, text: str | None) -> None:
        """The relay asked for NIP-42 and closed the REQ anyway.

        A relay that answers the challenge and still refuses the filter is deciding
        something about this key, not asking for another attempt. Counting it lets the
        backoff escalate; stamping it lets `health` say "refusing" rather than
        "authenticating" — which is what a permanent refusal used to look like, because
        every retry produced a fresh challenge and refreshed `auth_at`.
        """
        self.auth_refusals += 1
        self.auth_refused_at = time.time()
        self.auth_at = None
        self.last_error = (
            f"auth-required after authenticating ({self.auth_refusals}x): "
            f"{(text or '')[:140]}")[:300]
        if self.auth_refusals == 3:
            log.error(
                "%s answers NIP-42 and still refuses this filter for our key; backing "
                "off between attempts instead of re-REQing it", self.relay)

    def note_event(self) -> None:
        self.events += 1
        self.last_event_at = time.time()
        # Delivery is the refutation: whatever refused us is not refusing now.
        self.auth_refusals = 0
        self.auth_refused_at = None

    def live_state(self) -> dict[str, Any]:
        return {
            "relay": self.relay,
            "purpose": self.purpose,
            "kinds": list(self.kinds),
            "requested_kinds": list(self.requested_kinds),
            "refused_kinds": sorted(self.refused),
            "p_tag": bool(self.p_tag),
            "a_tags": len(self.a_tags),
            "authors": len(self.authors),
            "events": self.events,
            "alive": self.alive,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "attempt_started_at": self.attempt_started_at,
            "connected_at": self.connected_at,
            "auth_at": self.auth_at,
            "auth_refusals": self.auth_refusals,
            "auth_refused_at": self.auth_refused_at,
            "last_event_at": self.last_event_at,
            "last_stderr": self.last_stderr,
            "last_error": self.last_error,
            "rate_limited_at": self.rate_limited_at,
            "subscription_id": self.subscription_id,
            "transport": "pool",
        }

    # -- lifecycle -----------------------------------------------------

    async def run(self, on_event: Handler) -> None:
        """Subscribe, then stay out of the way: the pool's pump delivers the events.

        `on_event` is registered with the pool rather than read from a pipe here, so a
        worker's whole job is to own one subscription's lifetime.
        """
        self.started_at = time.time()
        try:
            await self.pool.attach(self, on_event)
            await self._stop.wait()
        finally:
            await self.pool.detach(self)

    def stop(self) -> None:
        self._stop.set()


class RelayPool:
    """The shared client and the notification pump every `PoolWorker` rides on.

    One `Client`, one subscription per worker's filter set, one pump. Events are routed
    by subscription id, so two workers on the same relay (DM + mentions) never see each
    other's traffic — and the client's own dedupe means a reconnect that replays history
    does not hand the plane the same wrap twice.
    """

    def __init__(self, secret_hex: str, *, client: Any | None = None,
                 on_refusal: RefusalHandler | None = None):
        self.keys = sdk.Keys.parse(parse_secret(secret_hex))
        self._client = client
        self._injected_client = client is not None
        self.on_refusal = on_refusal
        self.subscriptions: dict[str, PoolWorker] = {}
        self._handlers: dict[str, Handler] = {}
        self._relay_urls: dict[str, Any] = {}
        self._pump: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._started = asyncio.Event()
        self.events = 0
        self.started_at: float | None = None
        self.last_error: str | None = None

    # -- client --------------------------------------------------------

    @property
    def client(self) -> Any:
        """The real client, built on first use so constructing a pool touches nothing."""
        if self._client is None:
            authenticator = sdk.SignerAuthenticator(KeysSigner(self.keys))
            self._client = sdk.ClientBuilder().authenticator(authenticator).build()
        return self._client

    async def _ensure_relay(self, relay: str) -> Any:
        url = self._relay_urls.get(_norm(relay))
        if url is None:
            url = sdk.RelayUrl.parse(relay)
            self._relay_urls[_norm(relay)] = url
            await self.client.add_relay(url)
        return url

    async def _ensure_pump(self) -> None:
        """Start the one pump, before the first subscription so no event is missed."""
        if self._pump is not None and not self._pump.done():
            return
        # uniffi invokes our async signer from a Rust worker thread, where
        # `asyncio.get_running_loop()` does not exist: bind the loop explicitly or every
        # NIP-42 attempt dies with "no running event loop" and a restricted relay closes
        # the subscription as unauthenticated. Declaring it as the *home* loop also gives
        # concurrent one-shot loops something correct to restore to (see sdkloop.restore).
        sdkloop.set_home()
        self.started_at = self.started_at or time.time()
        self._pump = asyncio.create_task(self._pump_notifications())
        await self._started.wait()

    # -- attach / detach ----------------------------------------------

    async def attach(self, worker: PoolWorker, handler: Handler) -> None:
        """Add a relay if needed, connect, and subscribe, retrying with backoff.

        A failure here is not fatal: the worker keeps its counters, `last_error` says
        what happened, and a reconnect is attempted until the worker is stopped or the
        relay refuses every kind it was asked for.
        """
        await self._ensure_pump()
        url = await self._ensure_relay(worker.relay)
        backoff = 2
        while not worker._stop.is_set() and not self._stop.is_set():
            worker.attempts += 1
            worker.attempt_started_at = time.time()
            # Retries can outlive the loop a cold fetch rebound; assert ours each attempt.
            sdkloop.ensure()
            try:
                await self.client.connect(and_wait=CONNECT_TIMEOUT)
                output = await self._request(worker)
            except Exception as exc:  # noqa: BLE001 - a relay must not kill the daemon
                worker.last_error = f"subscribe failed: {exc}"[:200]
                log.warning("pool: cannot subscribe %s: %s", worker.relay, exc)
            else:
                failed = {str(failed_url): str(reason) for failed_url, reason in
                          (getattr(output, "failed", None) or {}).items()}
                if not failed:
                    worker.connected_at = worker.connected_at or time.time()
                    worker.last_error = None
                    worker._resubscribe_delay = 0.0
                    self.subscriptions[worker.subscription_id] = worker
                    self._handlers[worker.subscription_id] = handler
                    log.info("pool: subscribed %s (%s) for kinds %s on %s",
                             worker.purpose, worker.subscription_id,
                             list(worker.kinds), worker.relay)
                    return
                reason = "; ".join(f"{failed_url}: {text}"
                                   for failed_url, text in failed.items())
                if any("not connected" in text.lower() for text in failed.values()):
                    # `connect()` returns before the socket is up, so the first REQ can
                    # lose the race. Ask for that relay explicitly and retry shortly.
                    worker.last_error = f"not connected yet: {reason}"[:200]
                    try:
                        await self.client.connect_relay(url)
                    except Exception as exc:  # noqa: BLE001 - retried below
                        log.debug("pool: connect_relay %s failed: %s", worker.relay, exc)
                    await asyncio.sleep(0.5)
                    continue
                worker.last_error = f"subscribe refused: {reason}"[:200]
                if _auth_required(reason):
                    # The same refusal, arriving as a failed subscribe instead of a
                    # CLOSED: count it too, or `status` cannot name it and the backoff
                    # here has no idea it is permanent.
                    worker.note_auth_refusal(reason)
                if worker.note_refusals(reason) and worker.kinds:
                    backoff = 2  # a refusal is a filter problem: resubscribe at once
                    continue
            if not worker.kinds:
                worker.last_error = (
                    f"relay refuses every kind we subscribed to "
                    f"({list(worker.requested_kinds)}): {worker.last_error}"[:300])
                log.error("%s: %s", worker.relay, worker.last_error)
                return
            delay = min(max(backoff, 60), 600) if worker.rate_limited_at else backoff
            # Wait out the backoff, but stop immediately when the worker is asked to:
            # shutdown must not sit through a 60s sleep, and the gateway gives tasks 6s.
            pause = delay * (0.8 + 0.4 * (time.time() % 1))
            try:
                await asyncio.wait_for(worker._stop.wait(), timeout=pause)
            except asyncio.TimeoutError:
                pass
            else:
                return
            backoff = min(backoff * 2, 60)

    async def detach(self, worker: PoolWorker) -> None:
        pending = worker._resubscribe_task
        if pending is not None and not pending.done():
            pending.cancel()
        self.subscriptions.pop(worker.subscription_id, None)
        self._handlers.pop(worker.subscription_id, None)
        if self._stop.is_set() or not self.subscriptions:
            return
        try:
            await self.client.unsubscribe(worker.subscription_id)
        except Exception:  # noqa: BLE001 - a closing socket is not an error worth raising
            log.debug("pool: unsubscribe %s failed", worker.subscription_id)

    def _target(self, worker: PoolWorker) -> Any:
        url = self._relay_urls[_norm(worker.relay)]
        return sdk.ReqTarget.single(url, [worker.filter()])

    async def _request(self, worker: PoolWorker) -> Any:
        """(Re)send a worker's REQ, freeing the id first.

        The client keeps a closed subscription in its own map, so a re-REQ with the same
        id fails with "subscription ID already exists" — which reads like a server error
        and is only our own bookkeeping.

        Bounded, because the alternative is a worker that never reports again: `attach`
        only retries after this returns, and `_schedule_resubscribe` declines to start a
        second task while one is live, so an await that never resolves leaves the relay
        stuck at "authenticating" with nothing in the log to say why.
        """
        try:
            await asyncio.wait_for(
                self.client.unsubscribe(worker.subscription_id), timeout=REQUEST_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            log.warning("pool: unsubscribe of %s timed out; re-REQing anyway",
                        worker.subscription_id)
        except Exception:  # noqa: BLE001 - absent is the normal case
            log.debug("pool: unsubscribe before re-REQ of %s failed",
                      worker.subscription_id, exc_info=True)
        sdkloop.ensure()
        return await asyncio.wait_for(
            self.client.subscribe(self._target(worker), id=worker.subscription_id),
            timeout=REQUEST_TIMEOUT_S,
        )

    # -- pump ----------------------------------------------------------

    async def _pump_notifications(self) -> None:
        try:
            stream = self.client.notifications()
        except Exception as exc:  # noqa: BLE001 - report, do not spin
            self.last_error = f"cannot open notification stream: {exc}"[:200]
            log.exception("pool: cannot open notification stream")
            self._started.set()
            return
        self._started.set()
        while not self._stop.is_set():
            # A one-shot fetch or publish in a worker thread rebinds uniffi's
            # process-global loop to its own and then closes it; without this the next
            # `next()` builds its future on that dead loop and every relay worker dies.
            sdkloop.ensure()
            try:
                notification = await stream.next()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - keep the pump alive
                self.last_error = f"notification stream error: {exc}"[:200]
                log.exception("pool: notification stream failed")
                await asyncio.sleep(1)
                continue
            if notification is None:
                break  # the client shut down: nothing more will arrive
            try:
                await self._dispatch(notification)
            except Exception:  # noqa: BLE001 - one bad frame must not end the pump
                log.exception("pool: dispatch failed")

    async def _dispatch(self, notification: Any) -> None:
        if notification.is_new_event():
            worker = self.subscriptions.get(notification.subscription_id)
            if worker is None:
                return
            event = json.loads(notification.event.as_json())
            worker.note_event()
            self.events += 1
            handler = self._handlers.get(notification.subscription_id)
            if handler is not None:
                result = handler(worker.relay, event)
                if inspect.isawaitable(result):
                    await result
            return
        if not notification.is_message():
            return
        message = notification.message.as_enum()
        relay = str(notification.relay_url)
        if message.is_AUTH():
            # The client answers the challenge itself through our authenticator, so
            # seeing the challenge is the evidence auth happened on this relay.
            for worker in self.subscriptions.values():
                if _norm(worker.relay) == _norm(relay):
                    worker.note_auth()
            log.info("pool: %s asked for NIP-42 auth; answered in-process", relay)
            return
        if message.is_CLOSED():
            worker = self.subscriptions.get(getattr(message, "subscription_id", ""))
            if worker is None:
                return
            text = getattr(message, "message", "") or ""
            if _auth_required(text):
                # Ask for auth, refuse anyway: a decision about this key, not a retry.
                worker.note_auth_refusal(text)
                if not worker.kinds:
                    return
                self._schedule_resubscribe(worker, text)
                return
            worker.last_error = text[:200] or "CLOSED"
            if "429" in text or "rate limit" in text.lower():
                worker.rate_limited_at = time.time()
                log.warning("pool: %s rate-limits us; backing off", relay)
                return
            if worker.note_refusals(text) and worker.kinds:
                # A refused kind is a filter problem, so resubscribe at once with what
                # is left instead of backing off.
                try:
                    await self._request(worker)
                except Exception as exc:  # noqa: BLE001 - recorded, not raised
                    worker.last_error = f"resubscribe failed: {exc}"[:200]
                    log.warning("pool: resubscribe %s failed: %s", worker.relay, exc)
            elif not worker.kinds:
                worker.last_error = (
                    f"relay refuses every kind we subscribed to "
                    f"({list(worker.requested_kinds)}): {text}"[:300])
                log.error("%s: %s", worker.relay, worker.last_error)
            else:
                # Closed for something other than a kind refusal (auth-required, a relay
                # restart): the subscription is gone, so it must not report as alive.
                # Re-REQ once the relay has had a moment, rather than waiting for a
                # reconnect that may never come.
                self._schedule_resubscribe(worker, text)

    def _schedule_resubscribe(self, worker: PoolWorker, reason: str) -> None:
        if worker._resubscribe_task is not None:
            return
        handler = self._handlers.get(worker.subscription_id)
        if handler is None or self._stop.is_set():
            return
        log.warning("pool: %s closed %s (%s); resubscribing", worker.relay,
                    worker.subscription_id, reason[:80])
        self.subscriptions.pop(worker.subscription_id, None)
        worker._resubscribe_task = asyncio.create_task(self._resubscribe(worker, handler))

    async def _resubscribe(self, worker: PoolWorker, handler: Handler) -> None:
        try:
            delay = resubscribe_delay(worker)
            # A permanent reason ("restricted: you must be in the web of trust", a relay
            # that authenticates us and refuses the filter anyway) must not turn into a
            # storm: double the pause each time, up to five minutes.
            worker._resubscribe_delay = min(delay * 2, 300.0)
            await asyncio.sleep(delay)
            if self._stop.is_set() or worker._stop.is_set():
                return
            await self.attach(worker, handler)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - attach already records its own failures
            log.exception("pool: resubscribe for %s failed", worker.relay)
        finally:
            worker._resubscribe_task = None

    # -- status / shutdown --------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            "mode": "pool",
            "subscriptions": len(self.subscriptions),
            "relays": len({_norm(worker.relay) for worker in self.subscriptions.values()}),
            "events": self.events,
            "started_at": self.started_at,
            "error": self.last_error,
        }

    async def stop(self) -> None:
        self._stop.set()
        client = self._client
        # Ask the client to shut down first: the notification stream ends by itself, so
        # the pump finishes on its own instead of being cancelled mid-await.
        if not self._injected_client and client is not None:
            try:
                await client.shutdown()
            except Exception:  # noqa: BLE001 - a shutdown error must not block stopping
                log.debug("pool: client shutdown failed", exc_info=True)
        pump, self._pump = self._pump, None
        if pump is not None:
            if not pump.done():
                pump.cancel()
            try:
                await pump
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - the pump's own errors are not stop's problem
                log.debug("pool: notification pump ended badly", exc_info=True)
        if not self._injected_client:
            # Release the client (and with it the notification stream) while the loop is
            # still alive. This build aborts at interpreter shutdown — SIGABRT, "panic in
            # a function that cannot unwind" from async-compat/tokio — if the stream is
            # dropped after the Rust runtime is gone, and an abort would make systemd
            # record a clean stop as a crash.
            self._client = None
            gc.collect()


#: The handler signature this module expects, re-exported for callers that type it.
TransportHandler = Callable[[str, dict], Awaitable[None] | None]
