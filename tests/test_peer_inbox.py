"""Read coverage for the people we talk to: their 10050 inboxes, bounded.

Spec §3 treats relay coverage as a correctness property. A fixed `relays.read` misses
a peer who publishes only into their own inbox relays — that is how a peer's
`auth.nostr1.com` and `nip17.com` were missed until they were added by hand. These
tests pin the replacement behaviour:

* resolve the configured groups' 10050 lists and add the relays, deduplicated;
* never add more than `max_extra`, and never more than one DM worker per addition;
* refresh on an interval, but never shrink coverage on a lookup failure;
* leave kind refusal to `PoolWorker`, exactly like any other relay.
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import fetch as fetch_mod
from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway import peer_inbox as peer_mod
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.peer_inbox import (
    DEFAULT_MAX_EXTRA,
    DEFAULT_REFRESH_MINUTES,
    SdkInboxResolver,
    inbox_additions,
    peer_inbox_config,
)

PEER = "b" * 64
PEER2 = "c" * 64


# -- fixtures and fakes -----------------------------------------------------

def gateway_config(tmp_path, *, read=(), publish=(), groups=None, peer_inbox=None,
                   whitenoise=None, transport=None) -> dict:
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(
        json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()})
    )
    relays: dict = {"read": list(read), "publish": list(publish)}
    if peer_inbox is not None:
        relays["peer_inbox"] = peer_inbox
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": relays,
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": groups or {},
        "policies": [],
        "defaults": {},
        "marmot": {"enabled": False},
    }
    if transport is not None:
        cfg["transport"] = transport
    if whitenoise:
        cfg["whitenoise"] = list(whitenoise)
    return cfg


def build_gateway(monkeypatch, tmp_path, cfg, *, resolver=None) -> Gateway:
    # `_load_follows` shells out to nak; tests never touch a live relay.
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    return Gateway(cfg, socket_path=tmp_path / "gateway.sock", peer_resolver=resolver)


class FakeResolver:
    """The fake relay resolver: a mapping of pubkey -> inbox relays, plus call log."""

    def __init__(self, mapping: dict[str, list[str]] | None = None,
                 error: Exception | None = None):
        self.mapping = dict(mapping or {})
        self.error = error
        self.calls: list[str] = []

    def inbox_relays(self, pubkey: str) -> list[str]:
        self.calls.append(pubkey)
        if self.error is not None:
            raise self.error
        return list(self.mapping.get(pubkey, []))


def install_recording_worker(monkeypatch) -> list[dict]:
    """Replace the worker class with a recorder, so wiring is testable without I/O."""
    built: list[dict] = []

    class RecordingWorker:
        def __init__(self, *args, **kwargs):
            relay = next(arg for arg in args if str(arg).startswith("wss://"))
            self.relay = relay
            self.kinds = kwargs.get("kinds", ())
            self.requested_kinds = self.kinds
            self.refused: set[int] = set()
            self.purpose = kwargs.get("purpose", "dm")
            self.p_tag = kwargs.get("p_tag")
            self.authors = kwargs.get("authors", ())
            self.events = 0
            self.last_error = None
            self._stop = asyncio.Event()
            built.append({"relay": relay, "kinds": self.kinds, "purpose": self.purpose})

        async def run(self, _handler):
            await self._stop.wait()

        def stop(self):
            self._stop.set()

    monkeypatch.setattr(gateway_mod, "PoolWorker", RecordingWorker)
    return built


async def wait_for(predicate, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("condition was not reached before the timeout")
        await asyncio.sleep(0.01)


# -- what may be added ------------------------------------------------------

def test_additions_are_deduplicated_against_the_read_set():
    assert inbox_additions(
        base=["wss://relay.example.com/chat", "wss://nos.lol"],
        candidates=["wss://NOS.lol/", "wss://peer.example", "wss://peer.example/"],
        cap=4,
    ) == ["wss://peer.example"]


def test_additions_stop_at_the_cap():
    assert inbox_additions(
        base=[], candidates=["wss://one.example", "wss://two.example", "wss://three.example"],
        cap=2,
    ) == ["wss://one.example", "wss://two.example"]
    assert inbox_additions(base=[], candidates=["wss://one.example"], cap=0) == []


@pytest.mark.parametrize("candidate", [
    "http://plain.example", "file:///etc/passwd", "ws://plain.example", "", "   ", None,
])
def test_only_wss_relays_are_ever_added(candidate):
    assert inbox_additions(base=[], candidates=[candidate], cap=4) == []


def test_excluded_relays_are_never_added():
    assert inbox_additions(
        base=[], candidates=["wss://relay.eu.whitenoise.chat", "wss://peer.example"],
        cap=4, exclude=["wss://relay.eu.whitenoise.chat/"],
    ) == ["wss://peer.example"]


# -- config -----------------------------------------------------------------

def test_peer_inbox_is_on_by_default_with_a_cap_and_an_interval():
    cfg = peer_inbox_config({
        "relays": {"read": ["wss://read.example"], "publish": ["wss://publish.example"]},
    })
    assert cfg.enabled is True
    assert cfg.max_extra == DEFAULT_MAX_EXTRA == 4
    assert cfg.refresh_minutes == DEFAULT_REFRESH_MINUTES == 60
    # Lookups default to the relays we already talk to, not to the wider net.
    assert cfg.lookup == ("wss://read.example", "wss://publish.example")
    assert cfg.refresh_seconds == 3600


def test_peer_inbox_options_can_be_pinned():
    cfg = peer_inbox_config({"relays": {"peer_inbox": {
        "enabled": False, "max_extra": 2, "refresh_minutes": 0,
        "lookup": ["wss://lookup.example"],
    }}})
    assert (cfg.enabled, cfg.max_extra, cfg.refresh_minutes) == (False, 2, 0)
    assert cfg.lookup == ("wss://lookup.example",)
    assert cfg.refresh_seconds == 0


def test_lookup_defaults_are_deduplicated_across_read_and_publish():
    cfg = peer_inbox_config({"relays": {
        "read": ["wss://same.example/"], "publish": ["wss://same.example"],
    }})
    assert cfg.lookup == ("wss://same.example/",)


def test_the_default_lookup_list_is_bounded_but_an_explicit_one_is_not():
    read = [f"wss://read-{n}.example" for n in range(6)]
    assert len(peer_inbox_config({"relays": {"read": read}}).lookup) == 4
    pinned = [f"wss://pinned-{n}.example" for n in range(6)]
    cfg = peer_inbox_config({"relays": {"peer_inbox": {"lookup": pinned}}})
    assert cfg.lookup == tuple(pinned)


@pytest.mark.parametrize("peer_inbox", [
    {"max_extra": -1},
    {"max_extra": "4"},
    {"max_extra": True},
    {"refresh_minutes": -5},
    {"enabled": "yes"},
    {"lookup": "wss://lookup.example"},
    {"lookup": ["http://lookup.example"]},
    [],
])
def test_malformed_peer_inbox_config_is_an_error_not_a_silent_default(peer_inbox):
    with pytest.raises(ValueError):
        peer_inbox_config({"relays": {"peer_inbox": peer_inbox}})


# -- the inbox resolver -----------------------------------------------------

def test_the_resolver_takes_the_newest_inbox_list(monkeypatch):
    events = [
        {"kind": 10050, "created_at": 100,
         "tags": [["relay", "wss://old.example"]]},
        {"kind": 10050, "created_at": 200, "tags": [
            ["relay", "wss://new.example"],
            ["relay", "wss://new.example/"],
            ["relay", "http://plain.example"],
            ["p", "not-a-relay"],
        ]},
    ]
    calls: list[dict] = []

    def fake_fetch(**kwargs):
        calls.append(kwargs)
        return list(events)

    monkeypatch.setattr(fetch_mod, "fetch_many_blocking", fake_fetch)
    resolver = SdkInboxResolver(["wss://lookup.example"])

    # `wss://old.example` is the older event; the replaceable list that wins is the newer
    # one, deduplicated and with the non-relay tags dropped.
    assert resolver.inbox_relays(PEER) == ["wss://new.example"]
    assert calls[0]["relays"] == ("wss://lookup.example",)
    assert calls[0]["kinds"] == [10050]
    assert calls[0]["author"] == PEER
    assert calls[0]["limit"] == 1
    assert calls[0]["secret"] is None


def test_the_resolver_authenticates_as_us_when_it_has_the_secret(monkeypatch):
    seen: dict = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(fetch_mod, "fetch_many_blocking", fake_fetch)
    secret = "00" * 32
    resolver = SdkInboxResolver(["wss://relay.example.com/chat"], secret=secret)

    assert resolver.inbox_relays(PEER) == []
    assert seen["secret"] == secret


def test_the_resolver_asks_only_the_bounded_lookup_set(monkeypatch):
    seen: dict = {}

    def fake_fetch(**kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(fetch_mod, "fetch_many_blocking", fake_fetch)
    lookups = [f"wss://lookup-{n}.example" for n in range(4)]

    assert SdkInboxResolver(lookups, limit=2).inbox_relays(PEER) == []
    assert seen["relays"] == tuple(lookups[:2])


# -- gateway wiring ---------------------------------------------------------

async def test_peer_inbox_relays_join_the_dm_read_set_with_one_worker_each(
        monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({
        PEER: ["wss://peer-one.example", "wss://peer-two.example"],
        PEER2: ["wss://peer-two.example/"],
    })
    cfg = gateway_config(tmp_path, read=["wss://relay.example.com/chat"],
                         groups={"family": [PEER], "peers": [PEER2]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: len(gw.peer_relays) == 2)
        assert gw.relays_configured == ["wss://relay.example.com/chat"]
        assert gw.relays_read == ["wss://relay.example.com/chat", "wss://peer-one.example",
                                  "wss://peer-two.example"]
        # One DM worker per addition, no feed or mention subscription: the cap is the
        # connection bound, not three times it.
        assert [(w["relay"], w["kinds"], w["purpose"]) for w in built] == [
            ("wss://relay.example.com/chat", (1059, 4), "dm"),
            ("wss://peer-one.example", (1059, 4), "peer-inbox"),
            ("wss://peer-two.example", (1059, 4), "peer-inbox"),
        ]
        assert resolver.calls == [PEER, PEER2]
    finally:
        await gw.stop()


async def test_a_relay_already_in_the_read_set_is_not_added_again(monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({PEER: ["wss://nos.lol/"]})
    cfg = gateway_config(tmp_path, read=["wss://nos.lol"], groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: gw.peer_last_refresh is not None)
        assert gw.peer_relays == []
        assert not [w for w in built if w["purpose"] == "peer-inbox"]
        assert [w["purpose"] for w in built] == ["dm", "mentions"]
    finally:
        await gw.stop()


async def test_the_cap_bounds_total_additions_across_refreshes(monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({
        PEER: ["wss://one.example", "wss://two.example"],
    })
    cfg = gateway_config(tmp_path, groups={"family": [PEER]},
                         peer_inbox={"max_extra": 1})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: gw.peer_relays == ["wss://one.example"])
        # A second peer appears with a new relay; the cap is already spent, so refresh
        # does not even look it up — no connections, no subprocesses.
        resolver.mapping[PEER2] = ["wss://three.example"]
        gw.peer_pubkeys = (*gw.peer_pubkeys, PEER2)
        lookups = len(resolver.calls)
        assert await gw.refresh_peer_relays() == []
        assert len(resolver.calls) == lookups
        assert gw.peer_relays == ["wss://one.example"]
        assert len([w for w in built if w["purpose"] == "peer-inbox"]) == 1

        status = await gw.status()
        assert status["peer_inbox"]["max_extra"] == 1
        assert status["peer_inbox"]["added"] == ["wss://one.example"]
        # Relays a peer advertised but the cap refused are a named blind spot.
        assert status["peer_inbox"]["skipped_by_cap"] == ["wss://two.example"]
    finally:
        await gw.stop()


async def test_disabled_peer_inbox_never_looks_a_peer_up(monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({PEER: ["wss://peer.example"]})
    cfg = gateway_config(tmp_path, read=["wss://nos.lol"], groups={"family": [PEER]},
                         peer_inbox={"enabled": False})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await asyncio.sleep(0.05)  # long enough for a scheduled pass to have run
        assert resolver.calls == []
        assert gw.relays_read == ["wss://nos.lol"]
        assert [w["purpose"] for w in built] == ["dm", "mentions"]
    finally:
        await gw.stop()


async def test_a_whitenoise_relay_advertised_by_a_peer_is_not_read(monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({PEER: ["wss://relay.eu.whitenoise.chat",
                                    "wss://peer.example"]})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]},
                         whitenoise=["wss://relay.eu.whitenoise.chat"])
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: gw.peer_relays == ["wss://peer.example"])
        assert [w["relay"] for w in built] == ["wss://peer.example"]
    finally:
        await gw.stop()


async def test_a_lookup_failure_is_recorded_and_keeps_existing_coverage(monkeypatch, tmp_path):
    installed = install_recording_worker(monkeypatch)
    resolver = FakeResolver({PEER: ["wss://peer.example"]})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: gw.peer_relays == ["wss://peer.example"])

        resolver.error = RuntimeError("lookup timed out")
        assert await gw.refresh_peer_relays() == []
        assert gw.peer_relays == ["wss://peer.example"], "a timeout must not shrink coverage"
        assert gw.peer_errors[PEER] == "lookup timed out"
        assert gw.relays_read == ["wss://peer.example"]
        assert len(installed) == 1

        # A later successful pass clears the error and can still add a new relay.
        resolver.error = None
        resolver.mapping[PEER] = ["wss://peer.example", "wss://peer-new.example"]
        assert await gw.refresh_peer_relays() == ["wss://peer-new.example"]
        assert gw.peer_errors == {}
        assert (await gw.status())["peer_inbox"]["errors"] == {}
    finally:
        await gw.stop()


async def test_the_refresh_interval_picks_up_a_relay_advertised_later(monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({PEER: []})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)
    # The interval is a whole number of minutes in config; compress it for the test
    # rather than waiting an hour for the second pass.
    monkeypatch.setattr(peer_mod.PeerInboxConfig, "refresh_seconds",
                        property(lambda _self: 0.05))

    await gw.start()
    try:
        await wait_for(lambda: len(resolver.calls) >= 1)
        assert gw.peer_relays == []
        await wait_for(lambda: resolver.calls.count(PEER) >= 2,
                       timeout=2.0)  # the loop refreshes on its interval

        resolver.mapping[PEER] = ["wss://later.example"]
        await wait_for(lambda: gw.peer_relays == ["wss://later.example"])
        assert [w["purpose"] for w in built] == ["peer-inbox"]
    finally:
        await gw.stop()


# -- a dead inbox is reported, not churned ----------------------------------

async def test_a_peer_inbox_worker_that_lost_every_kind_is_recorded_dead(monkeypatch, tmp_path):
    """A relay that refuses every kind can never deliver: name it, keep it out of churn.

    Kind negotiation itself is `PoolWorker`'s (covered by `tests/test_pool.py`); what is
    pinned here is what the gateway does with the outcome — the relay stays in the read
    set (so a refresh will not re-add it) and `status` names it instead of leaving a
    quiet-looking relay behind.
    """
    peer = "wss://dead-inbox.example"
    resolver = FakeResolver({PEER: [peer]})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    class DeadWorker:
        relay = peer
        purpose = "peer-inbox"
        kinds: tuple[int, ...] = ()
        last_error = "relay refuses every kind we subscribed to ([1059, 4])"

        async def run(self, _handler):
            return None

    gw.relays_read.append(peer)
    gw.peer_relays.append(peer)
    await gw._guarded(DeadWorker())

    assert gw._dead_peer_relays == {peer}
    assert (await gw.status())["peer_inbox"]["dead"] == [peer]
    assert await gw.refresh_peer_relays() == [], "a dead inbox must not be re-added"
    assert gw.peer_relays == [peer]
