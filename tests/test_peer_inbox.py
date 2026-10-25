"""Read coverage for the people we talk to: their 10050 inboxes, bounded.

Spec §3 treats relay coverage as a correctness property. A fixed `relays.read` misses
a peer who publishes only into their own inbox relays — that is how Nathan's
`auth.nostr1.com` and `nip17.com` were missed until they were added by hand. These
tests pin the replacement behaviour:

* resolve the configured groups' 10050 lists and add the relays, deduplicated;
* never add more than `max_extra`, and never more than one DM worker per addition;
* refresh on an interval, but never shrink coverage on a lookup failure;
* leave kind refusal to `RelayWorker`, exactly like any other relay.
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway import peer_inbox as peer_mod
from hermes_nostr_gateway import relay as relay_mod
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.peer_inbox import (
    DEFAULT_MAX_EXTRA,
    DEFAULT_REFRESH_MINUTES,
    NakInboxResolver,
    inbox_additions,
    peer_inbox_config,
)

PEER = "b" * 64
PEER2 = "c" * 64


# -- fixtures and fakes -----------------------------------------------------

def gateway_config(tmp_path, *, read=(), publish=(), groups=None, peer_inbox=None,
                   whitenoise=None) -> dict:
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
    """Replace RelayWorker with a recorder, so wiring is testable without nak."""
    built: list[dict] = []

    class RecordingWorker:
        def __init__(self, relay, me, **kwargs):
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

    monkeypatch.setattr(gateway_mod, "RelayWorker", RecordingWorker)
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
        base=["wss://relay.day.ag/chat", "wss://nos.lol"],
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


# -- the nak resolver -------------------------------------------------------

class _Done:
    def __init__(self, stdout: str = "", stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_nak_resolver_takes_the_newest_inbox_list(monkeypatch):
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
    calls: list[tuple[list[str], dict]] = []

    def fake_run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        return _Done("\n".join(json.dumps(event) for event in events))

    monkeypatch.setattr(peer_mod.subprocess, "run", fake_run)
    resolver = NakInboxResolver(["wss://lookup.example"], nak="nak")

    assert resolver.inbox_relays(PEER) == ["wss://new.example"]
    argv = calls[0][0]
    assert argv[:5] == ["nak", "req", "-k", "10050", "-a"]
    assert argv[5] == PEER
    assert argv[-1] == "wss://lookup.example"
    assert "--auth" not in argv
    assert calls[0][1]["env"] is None


def test_nak_resolver_authenticates_with_our_identity_when_it_has_the_secret(monkeypatch):
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        seen["env"] = kwargs["env"]
        return _Done("")

    monkeypatch.setattr(peer_mod.subprocess, "run", fake_run)
    secret = "00" * 32
    resolver = NakInboxResolver(["wss://relay.day.ag/chat"], secret=secret, nak="nak")

    assert resolver.inbox_relays(PEER) == []
    assert "--auth" in seen["argv"]
    assert seen["env"]["NOSTR_SECRET_KEY"] == secret


# -- gateway wiring ---------------------------------------------------------

async def test_peer_inbox_relays_join_the_dm_read_set_with_one_worker_each(
        monkeypatch, tmp_path):
    built = install_recording_worker(monkeypatch)
    resolver = FakeResolver({
        PEER: ["wss://peer-one.example", "wss://peer-two.example"],
        PEER2: ["wss://peer-two.example/"],
    })
    cfg = gateway_config(tmp_path, read=["wss://relay.day.ag/chat"],
                         groups={"family": [PEER], "hal": [PEER2]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: len(gw.peer_relays) == 2)
        assert gw.relays_configured == ["wss://relay.day.ag/chat"]
        assert gw.relays_read == ["wss://relay.day.ag/chat", "wss://peer-one.example",
                                  "wss://peer-two.example"]
        # One DM worker per addition, no feed or mention subscription: the cap is the
        # connection bound, not three times it.
        assert [(w["relay"], w["kinds"], w["purpose"]) for w in built] == [
            ("wss://relay.day.ag/chat", (1059, 4), "dm"),
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


# -- refusal behaves exactly like any other relay ---------------------------

class _Stdout:
    def __init__(self, lines: list[bytes]):
        self._it = iter(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Stderr:
    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self) -> bytes:
        return self._payload


class _FakeProc:
    def __init__(self, *, lines=(), stderr=b"", returncode=0):
        self.stdout = _Stdout(list(lines))
        self.stderr = _Stderr(stderr)
        self.returncode = returncode

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def install_fake_nak(monkeypatch, script: list[dict]) -> list[list[str]]:
    calls: list[list[str]] = []
    steps = list(script)

    async def fake_exec(*args, **kwargs):
        calls.append([str(arg) for arg in args])
        step = steps.pop(0) if steps else {"stderr": b"", "lines": []}
        stderr = step.get("stderr", b"")
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return _FakeProc(lines=step.get("lines", []), stderr=stderr,
                         returncode=step.get("returncode", 0))

    monkeypatch.setattr(relay_mod.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def asked_kinds(argv: list[str]) -> list[int]:
    return [int(argv[i + 1]) for i, part in enumerate(argv) if part == "-k"]


async def test_a_peer_relay_that_refuses_kind_4_keeps_reading_wraps(monkeypatch, tmp_path):
    peer = "wss://peer-inbox.example"
    calls = install_fake_nak(monkeypatch, [
        {"stderr": "CLOSED: filter validation failed: kind not allowed: 4"},
        {"stderr": "connecting... ok."},
    ])
    resolver = FakeResolver({PEER: [peer]})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: len([c for c in calls if c[-1] == peer]) >= 2)
        peer_calls = [c for c in calls if c[-1] == peer]
        assert asked_kinds(peer_calls[0]) == [1059, 4]
        assert asked_kinds(peer_calls[1]) == [1059], "kind 4 must be dropped, wraps kept"
        worker = next(w for w in gw.workers if w.relay == peer)
        assert worker.refused == {4}
        assert worker.kinds == (1059,)
        assert worker.purpose == "peer-inbox"
    finally:
        await gw.stop()


async def test_a_peer_relay_that_refuses_every_kind_is_not_resubscribed(monkeypatch, tmp_path):
    peer = "wss://dead-inbox.example"
    calls = install_fake_nak(monkeypatch, [
        {"stderr": "CLOSED: kind not allowed: 4"},
        {"stderr": "CLOSED: kind not allowed: 1059"},
        {"stderr": "must never be spawned"},
    ])
    resolver = FakeResolver({PEER: [peer]})
    cfg = gateway_config(tmp_path, groups={"family": [PEER]})
    gw = build_gateway(monkeypatch, tmp_path, cfg, resolver=resolver)

    await gw.start()
    try:
        await wait_for(lambda: gw._dead_peer_relays == {peer})
        assert len([c for c in calls if c[-1] == peer]) == 2

        # A refresh sees the relay in the read set already: no reconnect, no churn.
        assert await gw.refresh_peer_relays() == []
        assert len([c for c in calls if c[-1] == peer]) == 2
        assert (await gw.status())["peer_inbox"]["dead"] == [peer]
    finally:
        await gw.stop()
