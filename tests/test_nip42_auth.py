"""NIP-42 through the daemon's own loop: a real signer, a real challenge, a real REQ.

`test_sdkloop.py` pins the binding's bookkeeping and `test_pool.py` pins the pool's
state machine, both with a fake client. Neither ever asks the bindings to sign an AUTH
event, so neither can catch the failure this file is named for.

The bindings resolve every call's future through one process-global loop
(`nostr_sdk.py:8035-8064`): `_uniffi_rust_call_async` reads `_UNIFFI_GLOBAL_EVENT_LOOP`
and creates its future there, then awaits it on whichever loop the caller is running.
When those are two different loops the AUTH dies with `got Future attached to a
different loop`, the relay closes the REQ as unauthenticated, and the worker reports
`authenticating` for ever. So the relay here is a real WebSocket that sends a challenge
and refuses a REQ until it has an AUTH, and the client is a real `nostr-sdk` client with
our own `KeysSigner`: the assertion is on the wire (a valid kind-22242 event naming the
challenge and the relay), not on our bookkeeping.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

import nostr_sdk as sdk
import pytest
import websockets

from hermes_nostr_gateway import sdkloop
from hermes_nostr_gateway.fetch import fetch_first_blocking
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.pool import (
    RESUBSCRIBE_DELAY,
    PoolWorker,
    RelayPool,
    resubscribe_delay,
)

ME = "b" * 64


class AuthRelay:
    """A relay that requires NIP-42 before it will serve a REQ.

    Sends its challenge on connect, records the AUTH event it is given, and closes any
    REQ that arrives before one. That is what makes a worker that cannot sign
    distinguishable from one that can: the former never gets an EOSE.
    """

    def __init__(self, *, challenge: str = "challenge-abc") -> None:
        self.challenge = challenge
        self.auth_events: list[dict] = []
        self.reqs: list[str] = []
        #: Subscription ids this relay answered with EOSE — i.e. served, which it only
        #: does for a REQ that arrives on a connection that has already authenticated.
        self.served: list[str] = []
        self._server: websockets.Server | None = None
        self.url = ""
        self._sockets: list[Any] = []

    async def start(self) -> "AuthRelay":
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        self.url = f"ws://127.0.0.1:{self._server.sockets[0].getsockname()[1]}"
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, ws) -> None:
        self._sockets.append(ws)
        authed = False
        await ws.send(json.dumps(["AUTH", self.challenge]))
        try:
            async for raw in ws:
                msg = json.loads(raw)
                if msg[0] == "AUTH":
                    self.auth_events.append(msg[1])
                    authed = True
                elif msg[0] == "REQ":
                    self.reqs.append(msg[1])
                    if authed:
                        self.served.append(msg[1])
                        await ws.send(json.dumps(["EOSE", msg[1]]))
                    else:
                        await ws.send(json.dumps(
                            ["CLOSED", msg[1],
                             "auth-required: all requests must be authenticated"]))
                elif msg[0] == "CLOSE":
                    continue
        finally:
            if ws in self._sockets:
                self._sockets.remove(ws)


@pytest.fixture
async def auth_relay():
    relays: list[AuthRelay] = []

    async def make(**kwargs) -> AuthRelay:
        relay = await AuthRelay(**kwargs).start()
        relays.append(relay)
        return relay

    try:
        yield make
    finally:
        for relay in relays:
            await relay.stop()


async def wait_for(predicate: Callable[[], Any], timeout: float = 25.0,
                   why: str = "condition never became true") -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(why)


def _worker(pool: RelayPool, relay: AuthRelay) -> PoolWorker:
    return PoolWorker(pool, relay.url, ME, kinds=(1059,), p_tag=ME, purpose="dm")


def _auth_tags(event: dict) -> dict[str, str]:
    return {tag[0]: tag[1] for tag in event.get("tags", [])}


# -- the auth path itself ---------------------------------------------------


async def test_a_restricted_relay_authenticates_through_the_daemons_loop(auth_relay):
    """A worker on a `/chat`-style relay signs its AUTH on the loop the pump runs on."""
    keys = sdk.Keys.generate()
    relay = await auth_relay()
    pool = RelayPool(keys.secret_key().to_hex())
    worker = _worker(pool, relay)

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        # Wait for the thing asserted, not for `alive`: `alive` means the REQ went out,
        # and the challenge is answered asynchronously on this same loop, so the AUTH can
        # land a few milliseconds after the subscribe returns — a window that widens on a
        # loaded machine. Asserting on `alive` read as "the relay never received an AUTH"
        # in a full run and passed alone, which is a race, not a product fault.
        await wait_for(lambda: relay.auth_events,
                       why="the relay never received an AUTH event")
        auth = relay.auth_events[0]
        assert auth["kind"] == 22242
        assert auth["pubkey"] == keys.public_key().to_hex()
        tags = _auth_tags(auth)
        assert tags["challenge"] == relay.challenge
        assert tags["relay"] == relay.url
        # `alive` is the relay's answer, not ours: it only serves a REQ it has an AUTH
        # for, and this one closes anything that arrives before one. So wait for the
        # EOSE — a REQ served, not merely sent — and for the worker to be holding it.
        await wait_for(lambda: relay.served,
                       why="the relay never served a REQ after the handshake")
        await wait_for(lambda: worker.alive,
                       why="the worker has no live subscription after authenticating")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
        await pool.stop()


async def test_no_one_shot_moves_the_binding_once_the_pump_has_a_home(auth_relay, monkeypatch):
    """A cold read or publish in a worker thread must not re-point uniffi's loop.

    `fetch_*_blocking` is the shape that used to do it: it ran `asyncio.run` on its own
    thread, bound the bindings to that loop, and closed it. With the pool's loop
    declared as home, a one-shot either runs on that loop or puts the binding back —
    never a third loop, because the AUTH that follows would then be awaited on one loop
    while its future was built on another.
    """
    keys = sdk.Keys.generate()
    relay = await auth_relay()
    pool = RelayPool(keys.secret_key().to_hex())
    worker = _worker(pool, relay)

    recorded: list[Any] = []
    real = sdk.uniffi_set_event_loop

    def spy(loop):
        recorded.append(loop)
        real(loop)

    monkeypatch.setattr(sdk, "uniffi_set_event_loop", spy)

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.alive)
        home = asyncio.get_running_loop()
        assert sdkloop.current() is home, "the pump never declared its loop"
        recorded.clear()

        # What a peer-inbox look-up does, in the thread it does it in.
        found = await asyncio.to_thread(
            fetch_first_blocking, relays=[relay.url], kinds=[10050],
            author=keys.public_key().to_hex(),
            secret=keys.secret_key().to_hex(), timeout=2)
        assert found is None, "the stub served an event it does not have"

        # And the daemon's own subscription is still working afterwards.
        #
        # The binding is this test's subject, so it is read before any further await: the
        # pump re-asserts its own loop before every await (`sdkloop.ensure`), so a re-point
        # that landed during the one-shot would be papered over by the time a wait returns.
        assert sdkloop.current() is home, "the one-shot left the bindings on another loop"
        assert recorded == [], (
            "a one-shot re-pointed the bindings' loop: "
            f"{[(id(loop), getattr(loop, 'is_closed', lambda: None)()) for loop in recorded]}")

        # Liveness is waited for, not sampled. The relay challenges on connect, so the first
        # REQ can go out before the AUTH completes and be closed `auth-required`; the worker
        # is then not `alive` until it re-REQs, which is the pump working, not one that
        # stopped. The relay's own record is the proof that matters: it only sends EOSE for
        # a REQ it has an AUTH for, so a served subscription id is one it accepted.
        await wait_for(
            lambda: worker.alive and worker.subscription_id in relay.served,
            why="the pump never re-subscribed and got served after the one-shot")
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=10)
        await pool.stop()


def _gateway_config(tmp_path) -> dict:
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    return {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gw.db"),
        "socket": str(tmp_path / "gw.sock"),
        "groups": {},
        "policies": [],
        "defaults": {},
        "marmot": {"enabled": False},
    }


async def test_the_daemon_declares_its_loop_before_it_awaits(tmp_path, monkeypatch):
    """The first thing `start()` does is say which loop the bindings belong to.

    A request that arrives while `start()` is still working (`send` is the one that
    reaches a cold fetch) must not find a daemon with no declared home: `run_isolated`
    would then give it a private loop, and the next relay auth — on the publisher's
    client, which is warmed in the same start-up — would build its future on that loop
    while awaiting it on this one.
    """
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_refresh(self) -> None:
        entered.set()
        await release.wait()

    monkeypatch.setattr(Gateway, "refresh_concord", slow_refresh)
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setattr(Gateway, "_setup_marmot", lambda self, _cfg: None)

    gateway = Gateway(_gateway_config(tmp_path), socket_path=tmp_path / "gw.sock")
    started = asyncio.create_task(gateway.start())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        assert sdkloop.current() is asyncio.get_running_loop(), (
            "mid-startup the daemon has no declared loop, so a one-shot would make one")
    finally:
        release.set()
        await asyncio.wait_for(started, timeout=10)
        await gateway.stop()


# -- a relay that authenticates and refuses anyway --------------------------


AUTH_CLOSED = "auth-required: requested filter requires authentication"


def test_a_relay_that_authenticates_and_still_refuses_backs_off_instead_of_storming():
    """The 0.2 s "auth just completed" retry is for the first close, not for every one.

    damus answers the challenge and then closes the same REQ with `auth-required`:
    once per cycle this looked like a completed handshake, so the worker re-REQed every
    0.2 s for ever. That is a request storm, and because every attempt earns a fresh
    challenge it also kept `auth_at` recent — which is why `hng status` could only ever
    say `authenticating` and never name the refusal.
    """
    pool = RelayPool(sdk.Keys.generate().secret_key().to_hex())
    worker = PoolWorker(pool, "wss://relay.example", ME, kinds=(1059,), p_tag=ME)

    assert resubscribe_delay(worker) == RESUBSCRIBE_DELAY, "no auth: the ordinary backoff"

    # A relay that closes the unauthenticated REQ, then completes the challenge.
    worker.note_auth_refusal(AUTH_CLOSED)
    worker.note_auth()
    assert resubscribe_delay(worker) == pytest.approx(0.2), (
        "the challenge just completed: the next request should be accepted")

    # It closed the retried REQ too, and answered the new challenge as well.
    worker.note_auth_refusal(AUTH_CLOSED)
    worker.note_auth()
    assert resubscribe_delay(worker) == pytest.approx(RESUBSCRIBE_DELAY), (
        "a second authenticated refusal must not get the 0.2 s retry")

    worker.note_auth_refusal(AUTH_CLOSED)
    worker.note_auth()
    assert resubscribe_delay(worker) == pytest.approx(RESUBSCRIBE_DELAY * 2)
    worker.note_auth_refusal(AUTH_CLOSED)
    assert resubscribe_delay(worker) == pytest.approx(RESUBSCRIBE_DELAY * 4)

    assert worker.auth_refusals == 4
    assert "auth-required after authenticating" in (worker.last_error or "")
    assert worker.live_state()["auth_refusals"] == 4

    # Delivery refutes it: a relay that starts delivering is not refusing.
    worker.note_event()
    assert worker.auth_refusals == 0
    assert resubscribe_delay(worker) == pytest.approx(RESUBSCRIBE_DELAY)


def test_the_streak_backoff_is_capped_at_five_minutes():
    pool = RelayPool(sdk.Keys.generate().secret_key().to_hex())
    worker = PoolWorker(pool, "wss://relay.example", ME, kinds=(1059,), p_tag=ME)
    worker.auth_refusals = 20

    assert resubscribe_delay(worker) == 300.0


async def test_a_closed_after_auth_is_counted_named_and_rescheduled():
    """The wire shape: AUTH arrives, then CLOSED(auth-required) for the same REQ."""
    keys = sdk.Keys.generate()
    pool = RelayPool(keys.secret_key().to_hex())
    worker = PoolWorker(pool, "wss://relay.example", ME, kinds=(1059,), p_tag=ME)
    pool.subscriptions[worker.subscription_id] = worker
    pool._handlers[worker.subscription_id] = lambda _relay, _event: None
    worker.note_auth()

    await pool._dispatch(sdk.ClientNotification.MESSAGE(
        relay_url=sdk.RelayUrl.parse("wss://relay.example/"),
        message=sdk.RelayMessage.closed(
            worker.subscription_id,
            "ERROR: auth-required: requested filter requires authentication")))
    try:
        assert worker.auth_refusals == 1
        assert worker.auth_at is None, "a refused handshake is not still authenticating"
        assert "auth-required after authenticating" in (worker.last_error or "")
        assert worker.subscription_id not in pool.subscriptions, (
            "a closed subscription must not report as alive")
        assert worker._resubscribe_task is not None
        assert resubscribe_delay(worker) == pytest.approx(RESUBSCRIBE_DELAY), (
            "the retry after a refusal streak must back off")
    finally:
        if worker._resubscribe_task is not None:
            worker._resubscribe_task.cancel()


async def test_a_close_about_kinds_is_not_counted_as_an_auth_refusal():
    keys = sdk.Keys.generate()
    pool = RelayPool(keys.secret_key().to_hex())
    worker = PoolWorker(pool, "wss://relay.example", ME, kinds=(1059,), p_tag=ME)
    pool.subscriptions[worker.subscription_id] = worker
    pool._handlers[worker.subscription_id] = lambda _relay, _event: None

    await pool._dispatch(sdk.ClientNotification.MESSAGE(
        relay_url=sdk.RelayUrl.parse("wss://relay.example/"),
        message=sdk.RelayMessage.closed(worker.subscription_id,
                                        "CLOSED: kind not allowed: 1059")))
    try:
        assert worker.auth_refusals == 0
        assert worker.auth_refused_at is None
        assert worker.refused == {1059}
    finally:
        if worker._resubscribe_task is not None:
            worker._resubscribe_task.cancel()


@pytest.mark.parametrize("text", [
    "auth-required: all requests must be authenticated",
    "ERROR: auth-required: requested filter requires authentication",
    "Auth Required",
    "you must authenticate first",
    "restricted: you must be authenticated to post to this relay",
])
def test_every_way_a_relay_words_an_auth_refusal_is_recognised(text):
    from hermes_nostr_gateway.pool import _auth_required

    assert _auth_required(text)


@pytest.mark.parametrize("text", [
    "kind not allowed: 4",
    "rate limited: 429",
    "error: could not connect",
    "blocked: not in the web of trust",
    None,
    "",
])
def test_a_close_that_is_not_about_auth_is_not_counted_as_one(text):
    from hermes_nostr_gateway.pool import _auth_required

    assert not _auth_required(text)
