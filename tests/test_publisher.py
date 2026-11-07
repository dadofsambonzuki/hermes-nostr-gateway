"""Publish through the bindings (plan P2): one signed event, per-relay truth.

The relay here is a real WebSocket speaking just enough NIP-01 to accept an EVENT and
answer OK, because the claims worth proving are wire facts: the *same bytes* on a retry,
one result per relay, a NIP-42 challenge answered in-process, and no `nak` child.

`nak event --auth` is gone from the default path, so a test that lets `subprocess` run is
a test that cannot tell the two transports apart. Every publish test here fails loudly if
something shells out.
"""

from __future__ import annotations

import asyncio
import json
import subprocess

import nostr_sdk as sdk
import pytest
import websockets

from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.publisher import (
    BindingsPublisher,
    Outcome,
    classify,
    is_already_held,
    is_permanent_refusal,
    make_publisher,
)

PEER = "ab" * 32


def test_an_answer_is_a_judgement_a_condition_or_already_held():
    """The classification the advertisement and the outbox both act on.

    A rate limit used to land in the refusal set, which meant a throttled relay was never
    retried for that kind. These are the words NIP-01 and this client actually produce.
    """
    for transient in ("rate-limited: slow down", "rate-limit: 30/min", "error: internal",
                      "service unavailable", "busy, try again later", "timeout",
                      "authentication failed", "a phrase nobody has seen before", ""):
        assert classify(transient) is Outcome.TRANSIENT, transient

    for refused in ("blocked: not in the web of trust", "invalid: bad signature",
                    "restricted: members only", "not allowed: chat kinds only",
                    "pow: difficulty 20 required", "auth-required: authenticate"):
        assert classify(refused) is Outcome.REFUSED, refused

    for held in ("duplicate: already have this event", "already exists"):
        assert classify(held) is Outcome.HELD, held

    assert is_permanent_refusal("rate-limited: slow down") is False
    assert is_permanent_refusal("blocked: no") is True
    assert is_already_held("duplicate: already have this event") is True
    assert is_already_held("rate-limited: slow down") is False


class LocalRelay:
    """A minimal NIP-01 write endpoint: records EVENT frames, answers OK.

    `challenge` makes it require NIP-42 (it sends AUTH on connect and refuses an
    EVENT that arrives before the client has answered). `silent_first` swallows the
    first EVENT without an OK, which is how a transport failure looks to a client.
    """

    def __init__(self, *, challenge: str | None = None, silent_first: bool = False,
                 reject: str | None = None):
        self.challenge = challenge
        self.silent_first = silent_first
        self.reject = reject
        self.frames: list[str] = []
        self.events: list[dict] = []
        self.auth_events: list[dict] = []
        self.connections = 0
        self._server: websockets.Server | None = None
        self.url = ""

    async def start(self) -> LocalRelay:
        self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handler(self, ws) -> None:
        self.connections += 1
        authed = self.challenge is None
        if self.challenge is not None:
            await ws.send(json.dumps(["AUTH", self.challenge]))
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                self.auth_events.append(msg[1])
                authed = True
                continue
            if msg[0] != "EVENT":
                continue
            self.frames.append(raw)
            self.events.append(msg[1])
            if self.silent_first and len(self.events) == 1:
                continue                      # no OK: the send times out
            if self.reject:
                await ws.send(json.dumps(["OK", msg[1]["id"], False, self.reject]))
                continue
            if not authed:
                await ws.send(json.dumps(
                    ["OK", msg[1]["id"], False, "auth-required: authenticate first"]))
                continue
            await ws.send(json.dumps(["OK", msg[1]["id"], True, ""]))


@pytest.fixture
async def relay_factory():
    started: list[LocalRelay] = []

    async def make(**kwargs) -> LocalRelay:
        relay = await LocalRelay(**kwargs).start()
        started.append(relay)
        return relay

    try:
        yield make
    finally:
        for relay in started:
            await relay.stop()


@pytest.fixture
def no_subprocess(monkeypatch):
    """Any shell-out during a publish is the failure, not a detail of one."""
    def boom(*args, **kwargs):
        raise AssertionError(f"publishing shelled out: {args[:1]}")

    async def boom_async(*args, **kwargs):
        raise AssertionError(f"publishing spawned a child: {args[:1]}")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom_async)


def signed_note(keys: sdk.Keys, text: str = "hello") -> dict:
    builder = sdk.EventBuilder(sdk.Kind(1059), text).tags([sdk.Tag.parse(["p", PEER])])
    unsigned = builder.finalize_unsigned(keys.public_key()).ensure_id()
    return json.loads(keys.sign_event(unsigned).as_json())


def _gateway_config(tmp_path, member_hex: str) -> dict:
    """A daemon config with no relays of its own: the test supplies the publisher."""
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    return {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {"family": [member_hex]},
        "policies": [{"name": "family-dms",
                      "match": {"group": "family", "kinds": [14, 1059], "dm": True},
                      "reach": "immediate", "authority": "instructions"}],
        "defaults": {"reach": "drop", "authority": "none"},
        # Never attach to a real wn-agent socket from a test.
        "marmot": {"enabled": False},
    }


async def test_a_signed_event_reaches_every_relay_unchanged(relay_factory, no_subprocess):
    """One signature, N sends: each relay gets the bytes the plane produced."""
    keys = sdk.Keys.generate()
    first = await relay_factory()
    second = await relay_factory()
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=2)
    try:
        results = await publisher.publish(event, [first.url, second.url])
    finally:
        await publisher.aclose()

    assert results == {first.url: (True, None), second.url: (True, None)}
    for relay in (first, second):
        assert len(relay.events) == 1, "one event per relay, not one per attempt"
        assert relay.events[0]["id"] == event["id"]
        assert relay.events[0]["sig"] == event["sig"], "the relay saw a re-signed event"
        assert json.loads(relay.frames[0])[1] == event, "the wire event differs from ours"


async def test_a_transport_failure_resends_the_same_event(relay_factory, no_subprocess):
    """A retry is a resend, never a re-wrap: the relay must see one id and one signature."""
    keys = sdk.Keys.generate()
    flaky = await relay_factory(silent_first=True)
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=0.3,
                                  attempts=2, retry_delay=0.05)
    try:
        results = await publisher.publish(event, [flaky.url])
    finally:
        await publisher.aclose()

    assert results == {flaky.url: (True, None)}, results
    assert len(flaky.frames) == 2, "the failed send should have been retried once"
    assert flaky.frames[0] == flaky.frames[1], "a retry changed the event bytes"
    assert {frame_event["id"] for frame_event in flaky.events} == {event["id"]}


async def test_a_relay_rejection_is_not_retried(relay_factory, no_subprocess):
    """`blocked` is a decision: sending it again only spends the relay's patience."""
    keys = sdk.Keys.generate()
    strict = await relay_factory(reject="blocked: not in the web of trust")
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=1,
                                  attempts=3, retry_delay=0.01)
    try:
        results = await publisher.publish(event, [strict.url])
    finally:
        await publisher.aclose()

    ok, error = results[strict.url]
    assert not ok
    assert "blocked" in (error or "")
    assert len(strict.frames) == 1, "a rejection was retried"


async def test_a_rate_limited_relay_is_retried_with_the_same_bytes(relay_factory, no_subprocess):
    """A rate limit is a condition, not a judgement: one more send of the same event."""
    keys = sdk.Keys.generate()
    busy = await relay_factory(reject="rate-limited: slow down")
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=1,
                                  attempts=2, retry_delay=0.01)
    try:
        results = await publisher.publish(event, [busy.url])
    finally:
        await publisher.aclose()

    ok, error = results[busy.url]
    assert not ok
    assert "rate-limit" in (error or "")
    assert len(busy.frames) == 2, "a rate limit was treated as a refusal"
    assert busy.frames[0] == busy.frames[1], "a retry changed the event bytes"


async def test_a_restricted_relay_authenticates_in_process(relay_factory, no_subprocess):
    """NIP-42 is signed here: an AUTH event with our key, no secret in a child's env."""
    keys = sdk.Keys.generate()
    restricted = await relay_factory(challenge="challenge-abc")
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=1.5,
                                  attempts=2, retry_delay=0.1)
    try:
        results = await publisher.publish(event, [restricted.url])
    finally:
        await publisher.aclose()

    assert results == {restricted.url: (True, None)}, results
    assert restricted.auth_events, "the client never answered the AUTH challenge"
    auth = restricted.auth_events[0]
    assert auth["kind"] == 22242
    assert auth["pubkey"] == keys.public_key().to_hex()
    tags = {tag[0]: tag[1] for tag in auth["tags"]}
    assert tags["challenge"] == "challenge-abc"
    assert tags["relay"] == restricted.url


async def test_an_event_that_does_not_verify_is_never_sent(relay_factory, no_subprocess):
    """A bad signature must fail here, not at a relay or a peer."""
    keys = sdk.Keys.generate()
    relay = await relay_factory()
    event = signed_note(keys)
    event["content"] = "tampered"

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=1)
    try:
        results = await publisher.publish(event, [relay.url])
    finally:
        await publisher.aclose()

    ok, error = results[relay.url]
    assert not ok
    assert "verification" in (error or "")
    assert relay.connections == 0, "an unverifiable event opened a connection"


async def test_the_publish_relay_set_is_bounded(relay_factory, no_subprocess):
    """A persistent client must not keep a socket per peer forever."""
    keys = sdk.Keys.generate()
    pinned = await relay_factory()
    transient = await relay_factory()
    event = signed_note(keys)

    publisher = BindingsPublisher(keys.secret_key().to_hex(), ok_timeout=1,
                                  max_relays=1, pinned=[pinned.url])
    try:
        await publisher.publish(event, [pinned.url])
        await publisher.publish(event, [transient.url])
        assert publisher.stats()["relays"] == 2, "a pinned relay was pruned"
        await publisher.publish(event, [pinned.url])
        assert publisher.stats()["relays"] == 1, "the transient relay outlived its use"
    finally:
        await publisher.aclose()


async def test_no_relays_means_no_client_and_no_send(no_subprocess):
    keys = sdk.Keys.generate()
    publisher = BindingsPublisher(keys.secret_key().to_hex())
    try:
        assert await publisher.publish(signed_note(keys), []) == {}
        assert publisher._client is None, "an empty publish built a client"
    finally:
        await publisher.aclose()


def test_make_publisher_has_no_transport_switch():
    """One publisher: the config's publish relays are pinned, a stale flag is ignored."""
    cfg = {"relays": {"publish": ["wss://relay.example"]}}
    publisher = make_publisher(cfg, "00" * 32)
    assert isinstance(publisher, BindingsPublisher)
    assert publisher.pinned == {"wss://relay.example"}

    stale = dict(cfg, transport={"legacy": True})
    rolled = make_publisher(stale, "00" * 32)
    assert isinstance(rolled, BindingsPublisher), "a deleted transport came back by config"
    assert rolled.stats()["mode"] == "bindings"


async def test_a_gateway_dm_reaches_every_publish_relay_without_a_child(
        tmp_path, monkeypatch, relay_factory, no_subprocess):
    """The whole path: wraps built once, sent to both relays, two copies per relay."""
    keys = sdk.Keys.generate()
    first = await relay_factory()
    second = await relay_factory()
    cfg = _gateway_config(tmp_path, keys.public_key().to_hex())
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setattr(Gateway, "_recipient_relays",
                        lambda self, peer: [first.url, second.url])

    gateway = Gateway(cfg, socket_path=tmp_path / "gateway.sock")
    await gateway.publisher.aclose()
    # Replace only the transport client: the wraps, the store and the policy stay real.
    gateway.publisher = BindingsPublisher(gateway._secret, ok_timeout=2)
    try:
        result = await gateway.send(to=keys.public_key().to_hex(), text="hello there")
    finally:
        await gateway.publisher.aclose()
        gateway.store.close()

    assert result["ok"] is True, result
    for relay in (first, second):
        # A NIP-17 DM is two wraps: the recipient copy and our own copy.
        assert len(relay.events) == 2, (relay.url, relay.events)
        assert {event["kind"] for event in relay.events} == {1059}
        assert len({event["id"] for event in relay.events}) == 2
    for label in ("peer", "self"):
        assert result["relays"][label] == {first.url: True, second.url: True}
