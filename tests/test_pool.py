"""The read transport on the relay pool: one client, one subscription per filter set.

These tests drive `RelayPool` through an injected client, so they never open a socket:
what matters here is the contract the gateway depends on — the subscription a worker
asks for, where an event is delivered, and what happens when a relay refuses a kind,
asks for NIP-42, or rate-limits us.
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway import sdkloop
from hermes_nostr_gateway.gateway import DM_KINDS, Gateway
from hermes_nostr_gateway.pool import PoolWorker, RelayPool

ME = "a" * 64
SECRET = sdk.Keys.generate().secret_key().to_hex()
RELAY = "wss://relay.example"


def a_signed_event(keys: sdk.Keys, kind: int = 1059) -> sdk.Event:
    unsigned = sdk.EventBuilder(sdk.Kind(kind), "wrapped").finalize_unsigned(keys.public_key())
    return keys.sign_event(unsigned)


class FakeStream:
    """The client's notification channel: tests push, the pump consumes."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()

    async def next(self):
        return await self.queue.get()

    def push(self, notification) -> None:
        self.queue.put_nowait(notification)


class FakeClient:
    """Enough of `sdk.Client` for the pool: no sockets, recorded calls."""

    def __init__(self, *, failed: dict | None = None) -> None:
        self.stream = FakeStream()
        self.relays: list[str] = []
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.connects = 0
        self.failed = failed or {}
        self.shutdowns = 0

    # -- the calls RelayPool makes ------------------------------------

    async def add_relay(self, url) -> bool:
        self.relays.append(str(url))
        return True

    async def connect(self, and_wait=None) -> None:
        self.connects += 1

    async def subscribe(self, target, id=None, close_on=None) -> sdk.SubscribeOutput:
        self.subscribed.append(id)
        failed = self.failed if len(self.subscribed) == 1 else {}
        success = [] if failed else [sdk.RelayUrl.parse(RELAY + "/")]
        return sdk.SubscribeOutput(id=id, success=success, failed=failed)

    async def unsubscribe(self, subscription_id: str) -> None:
        self.unsubscribed.append(subscription_id)

    def notifications(self) -> FakeStream:
        return self.stream


async def wait_for(predicate, timeout: float = 2.0) -> None:
    """Poll rather than sleep a fixed time: the pump is a task, not a callback."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition never became true")


def make_worker(pool: RelayPool, *, kinds=DM_KINDS, p_tag=ME, purpose="dm", **kwargs):
    return PoolWorker(pool, RELAY, ME, kinds=kinds, p_tag=p_tag, purpose=purpose, **kwargs)


# -- the subscription -------------------------------------------------------

def test_filter_asks_for_the_kinds_and_the_p_tag():
    pool = RelayPool(SECRET, client=FakeClient())
    worker = make_worker(pool)
    spec = json.loads(worker.filter().as_json())

    assert sorted(spec["kinds"]) == [4, 1059]
    assert spec["#p"] == [ME]
    # A recent window only: without it every resubscribe replays all history.
    assert spec["since"] > 0


def test_a_worker_can_be_narrowed_to_repository_addresses():
    """The watcher's filter: a kind set plus `#a` addresses, never a kind on its own."""
    pool = RelayPool(SECRET, client=FakeClient())
    worker = PoolWorker(pool, RELAY, ME, kinds=(1621, 1618),
                        a_tags=("30617:aa:one", "30617:bb:two"), purpose="ngit")
    spec = json.loads(worker.filter().as_json())

    assert sorted(spec["kinds"]) == [1618, 1621]
    assert spec["#a"] == ["30617:aa:one", "30617:bb:two"]
    assert "p" not in spec and "#p" not in spec
    assert worker.live_state()["a_tags"] == 2


def test_a_filter_with_no_kinds_is_refused_rather_than_becoming_a_firehose():
    pool = RelayPool(SECRET, client=FakeClient())
    worker = make_worker(pool, kinds=(1059,))
    worker.refused = {1059}

    assert worker.kinds == ()
    with pytest.raises(ValueError):
        worker.filter()


def test_worker_needs_a_hex_identity():
    pool = RelayPool(SECRET, client=FakeClient())
    with pytest.raises(ValueError):
        PoolWorker(pool, RELAY, "", kinds=(1059,))
    with pytest.raises(ValueError):
        PoolWorker(pool, RELAY, ME, kinds=())


def test_subscription_id_is_stable_per_relay_and_purpose():
    pool = RelayPool(SECRET, client=FakeClient())
    a = make_worker(pool)
    b = make_worker(pool)
    c = make_worker(pool, purpose="mentions")

    assert a.subscription_id == b.subscription_id
    assert a.subscription_id != c.subscription_id
    assert len(a.subscription_id) <= 64


# -- delivery ---------------------------------------------------------------

async def test_an_event_reaches_its_own_worker_and_is_counted():
    keys = sdk.Keys.generate()
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)
    got: list[tuple[str, dict]] = []

    task = asyncio.create_task(worker.run(lambda relay, event: got.append((relay, event))))
    try:
        await wait_for(lambda: worker.alive)
        client.stream.push(sdk.ClientNotification.NEW_EVENT(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            subscription_id=worker.subscription_id,
            event=a_signed_event(keys)))
        await wait_for(lambda: got)

        assert got[0][0] == RELAY
        assert got[0][1]["kind"] == 1059
        assert worker.events == 1 and worker.last_event_at is not None
        assert worker.connected_at is not None
        assert worker.live_state()["transport"] == "pool"
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


async def test_an_event_for_another_subscription_is_not_delivered():
    keys = sdk.Keys.generate()
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)
    got: list[dict] = []

    task = asyncio.create_task(worker.run(lambda _relay, event: got.append(event)))
    try:
        await wait_for(lambda: worker.alive)
        client.stream.push(sdk.ClientNotification.NEW_EVENT(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            subscription_id="hng-somewhere-else",
            event=a_signed_event(keys)))
        # A NEW_EVENT for a subscription we do not own must be ignored, not counted.
        await asyncio.sleep(0.05)
        assert got == [] and worker.events == 0
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


class BindingCheckingStream(FakeStream):
    """A stream that only works when uniffi's global loop is the one awaiting it.

    That is the production behaviour: a future built on a closed loop answers
    `got Future attached to a different loop`, and the pump retries for ever.
    """

    async def next(self):
        if sdkloop.current() is not asyncio.get_running_loop():
            raise RuntimeError("Task got Future attached to a different loop")
        return await super().next()


# -- the loop the pump rides on ---------------------------------------------

async def test_the_pump_rebinds_a_loop_a_one_shot_left_closed():
    """A cold fetch in a worker thread must not kill the daemon's pump.

    `fetch_*_blocking` runs `asyncio.run` on a thread; before the fix it left uniffi's
    process-global loop pointing at the loop it had just closed, and every `next()` on
    the pump's stream then failed — `0/8 relay(s) ok` with every relay healthy.
    """
    keys = sdk.Keys.generate()
    client = FakeClient()
    client.stream = BindingCheckingStream()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)
    got: list[dict] = []

    task = asyncio.create_task(worker.run(lambda _relay, event: got.append(event)))
    try:
        await wait_for(lambda: worker.alive)

        def one_shot_in_a_thread() -> None:
            async def inner() -> None:
                sdkloop.bind()  # what a cold read binds, then abandons when it closes

            asyncio.run(inner())

        await asyncio.to_thread(one_shot_in_a_thread)
        assert sdkloop.current().is_closed()

        client.stream.push(sdk.ClientNotification.NEW_EVENT(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            subscription_id=worker.subscription_id,
            event=a_signed_event(keys)))
        await wait_for(lambda: got)

        assert got[0]["kind"] == 1059
        assert worker.events == 1
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


# -- refusals, auth, rate limits -------------------------------------------

def test_only_kinds_the_worker_asked_for_are_recorded_as_refused():
    """A relay complaining about someone else's filter must not narrow ours."""
    pool = RelayPool(SECRET, client=FakeClient())
    worker = make_worker(pool)

    assert worker.note_refusals("CLOSED: kind not allowed: 7") == set()
    assert worker.refused == set()
    assert worker.kinds == DM_KINDS


async def test_a_refused_kind_is_dropped_and_the_worker_resubscribes_for_the_rest():
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    refusals: list[tuple] = []
    worker = make_worker(pool, on_refusal=lambda *args: refusals.append(args))

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.alive)
        client.stream.push(sdk.ClientNotification.MESSAGE(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            message=sdk.RelayMessage.closed(
                worker.subscription_id,
                "CLOSED: ERROR: bad req: filter validation failed: kind not allowed: 4")))

        await wait_for(lambda: worker.refused == {4})
        assert worker.kinds == (1059,)
        assert worker.requested_kinds == DM_KINDS
        # Resubscribed at once, once the refusal named a kind we asked for.
        await wait_for(lambda: len(client.subscribed) == 2)
        assert client.subscribed[1] == worker.subscription_id
        assert refusals and refusals[0][1] == {4} and refusals[0][2] == (1059,)
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


async def test_a_relay_that_refuses_every_kind_stops_instead_of_going_filterless():
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.alive)
        for kind in (4, 1059):
            client.stream.push(sdk.ClientNotification.MESSAGE(
                relay_url=sdk.RelayUrl.parse(RELAY + "/"),
                message=sdk.RelayMessage.closed(
                    worker.subscription_id, f"CLOSED: kind not allowed: {kind}")))
            await wait_for(lambda wanted=kind: wanted in worker.refused)

        assert worker.kinds == ()
        assert "refuses every kind" in (worker.last_error or "")
        assert client.subscribed.count(worker.subscription_id) == 2, (
            "a third subscription was sent with no kinds at all")
        with pytest.raises(ValueError):
            worker.filter()
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


async def test_a_nip42_challenge_is_recorded_as_auth_on_that_relay_only():
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)
    elsewhere = PoolWorker(pool, "wss://other.example", ME, kinds=(1059,))

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.alive)
        client.stream.push(sdk.ClientNotification.MESSAGE(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            message=sdk.RelayMessage.auth("challenge-string")))

        await wait_for(lambda: worker.auth_at is not None)
        assert elsewhere.auth_at is None, "another relay's challenge was attributed to it"
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


async def test_a_rate_limited_close_marks_backoff_and_keeps_the_subscription():
    client = FakeClient()
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.alive)
        client.stream.push(sdk.ClientNotification.MESSAGE(
            relay_url=sdk.RelayUrl.parse(RELAY + "/"),
            message=sdk.RelayMessage.closed(worker.subscription_id,
                                            "CLOSED: error: rate limited: 429")))

        await wait_for(lambda: worker.rate_limited_at is not None)
        assert "429" in (worker.last_error or "")
        # Rate limiting is not a kind refusal: nothing was dropped and no resubscribe.
        assert worker.refused == set() and worker.kinds == DM_KINDS
        assert len(client.subscribed) == 1
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


async def test_a_failed_subscription_is_reported_not_fatal():
    failed = {sdk.RelayUrl.parse(RELAY + "/"): "connection refused"}
    client = FakeClient(failed=failed)
    pool = RelayPool(SECRET, client=client)
    worker = make_worker(pool)

    task = asyncio.create_task(worker.run(lambda _relay, _event: None))
    try:
        await wait_for(lambda: worker.attempts >= 1)
        await wait_for(lambda: "connection refused" in (worker.last_error or ""))
        assert not worker.alive
        assert worker.kinds == DM_KINDS, "a transport failure must not narrow the filter"
    finally:
        worker.stop()
        await asyncio.wait_for(task, timeout=2)
        await pool.stop()


# -- wiring -----------------------------------------------------------------

def gateway_cfg(tmp_path, **extra):
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": ["wss://relay.example.com/chat"], "publish": ["wss://nos.lol"]},
        "store": str(tmp_path / "gw.db"),
        "groups": {}, "policies": [], "defaults": {},
    }
    cfg.update(extra)
    return cfg


async def test_the_gateway_wires_one_pool_and_a_stale_flag_cannot_add_a_second(
        tmp_path, monkeypatch):
    """The pool is the only read path, and a stale `transport` block cannot select another.

    An older config still carrying `transport: {legacy: true}` must start and behave
    exactly like a current one: no subprocess worker, no subprocess publisher.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setattr(Gateway, "_setup_marmot", lambda self, _cfg: None)
    built: list[str] = []

    class FakeWorker:
        def __init__(self, _pool, relay, _me, **kwargs):
            self.relay = relay
            self.kinds = kwargs.get("kinds", ())
            built.append(relay)
            self.events, self.last_error, self.purpose = 0, None, "dm"
            self._stop = asyncio.Event()

        async def run(self, _handler):
            await self._stop.wait()

        def stop(self):
            self._stop.set()

    monkeypatch.setattr(gateway_mod, "PoolWorker", FakeWorker)

    gateway = Gateway(gateway_cfg(tmp_path), socket_path=tmp_path / "a.sock")
    assert gateway.pool is not None
    await gateway.start()
    try:
        assert "wss://relay.example.com/chat" in built
        assert (await gateway.status())["transport"]["mode"] == "pool"
    finally:
        await gateway.stop()

    built.clear()
    flagged = Gateway(gateway_cfg(tmp_path, transport={"legacy": True}),
                      socket_path=tmp_path / "b.sock")
    assert flagged.publisher.stats()["mode"] == "bindings", "a stale flag selected a publisher"
    await flagged.start()
    try:
        assert "wss://relay.example.com/chat" in built, "the pool is the only read transport"
        assert (await flagged.status())["transport"]["mode"] == "pool"
    finally:
        await flagged.stop()
