"""Per-relay DM subscriptions: one refused kind must not cost the relay.

`wss://relay.eu.whitenoise.chat` answers a REQ that names legacy kind 4 with
`CLOSED: ... kind not allowed: 4` and closes the whole subscription, so the 1059
wraps it would have delivered went with it — on a relay that spec §3 says carries
DMs existing nowhere else. A filter is all-or-nothing at the relay, so the kind set
is per relay: pinned in config where known, negotiated at runtime where not.
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway import relay as relay_mod
from hermes_nostr_gateway.gateway import DM_KINDS, Gateway, relay_read_plan, restricted_relay
from hermes_nostr_gateway.relay import RelayWorker, refused_kinds
from hermes_nostr_gateway.store import Store

ME = "a" * 64
CLOSED_KIND_4 = (
    "wss://relay.eu.whitenoise.chat CLOSED: ERROR: bad req: "
    "filter validation failed: kind not allowed: 4"
)


def event_id(n: str = "e") -> dict:
    return {"id": n * 64, "kind": 1059, "pubkey": "b" * 64, "created_at": 1,
            "content": "", "tags": [], "sig": "c" * 128}


# -- fake `nak req` child ---------------------------------------------------

class _Stdout:
    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self._it = iter(self._lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration from None


class _Stderr:
    """A `StreamReader`-shaped stderr: iterable by line, as the worker reads it live."""

    def __init__(self, payload: bytes):
        self._lines = [line + b"\n" for line in payload.splitlines()]

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def read(self) -> bytes:
        payload, self._lines = b"".join(self._lines), []
        return payload


class FakeProc:
    def __init__(self, *, lines=(), stderr=b"", returncode=0):
        self.stdout = _Stdout(list(lines))
        self.stderr = _Stderr(stderr)
        self.returncode = returncode

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def install_fake_nak(monkeypatch, script: list[dict]) -> list[list[str]]:
    """Answer each spawn from `script`, and record the argv we were spawned with."""
    calls: list[list[str]] = []
    steps = list(script)

    async def fake_exec(*args, **kwargs):
        calls.append([str(arg) for arg in args])
        step = steps.pop(0) if steps else {"stderr": b"", "lines": []}
        stderr = step.get("stderr", b"")
        if isinstance(stderr, str):
            stderr = stderr.encode()
        return FakeProc(lines=step.get("lines", []), stderr=stderr,
                        returncode=step.get("returncode", 0))

    monkeypatch.setattr(relay_mod.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def asked_kinds(argv: list[str]) -> list[int]:
    return [int(argv[i + 1]) for i, part in enumerate(argv) if part == "-k"]


# -- what the worker asks for ----------------------------------------------

def test_default_dm_subscription_asks_for_wraps_and_legacy_dms():
    worker = RelayWorker("wss://relay.example", ME, kinds=DM_KINDS, p_tag=ME,
                         secret="00" * 32)
    argv = worker._args()
    assert asked_kinds(argv) == [1059, 4]
    assert argv[argv.index("-p") + 1] == ME
    assert "--auth" in argv and "--stream" in argv


def test_config_pins_a_narrower_kind_set_per_relay():
    cfg = {"relays": {"read": [
        "wss://relay.example.com/chat",
        {"url": "wss://relay.eu.whitenoise.chat", "kinds": [1059]},
    ]}}
    urls, kinds = relay_read_plan(cfg)
    assert urls == ["wss://relay.example.com/chat", "wss://relay.eu.whitenoise.chat"]
    assert kinds == {"wss://relay.eu.whitenoise.chat": (1059,)}


def test_read_entry_without_a_url_is_an_error_not_a_silent_skip():
    with pytest.raises(ValueError):
        relay_read_plan({"relays": {"read": [{"kinds": [1059]}]}})
    with pytest.raises(ValueError):
        relay_read_plan({"relays": {"read": [{"url": "wss://relay.example", "kinds": []}]}})


@pytest.mark.parametrize("relay,expected", [
    ("wss://relay.example.com/chat", True),
    ("wss://relay.example.com/chat/", True),
    ("wss://relay.eu.whitenoise.chat", True),
    ("wss://nos.lol", False),
    ("wss://relay.example.com/inbox", False),
])
def test_which_relays_carry_no_public_feeds(relay, expected):
    assert restricted_relay(relay) is expected


# -- reading a relay's refusal ---------------------------------------------

@pytest.mark.parametrize("text,expected", [
    (CLOSED_KIND_4, {4}),
    ("CLOSED: kind 4 not allowed", {4}),
    ("CLOSED: kinds not allowed: 4, 6", {4, 6}),
    ("connecting to relay... ok.", set()),
    ("CLOSED: error: rate limited", set()),
    ("", set()),
    (None, set()),
])
def test_refusal_text_variants(text, expected):
    assert refused_kinds(text) == expected


def test_only_kinds_we_asked_for_are_recorded_as_refused():
    worker = RelayWorker("wss://relay.example", ME, kinds=(1059, 4))
    assert worker.note_refusals("CLOSED: kind not allowed: 7") == set()
    assert worker.refused == set()


# -- the subscription behaviour --------------------------------------------

async def test_relay_that_refuses_kind_4_keeps_its_1059_subscription(monkeypatch):
    delivered = event_id()
    calls = install_fake_nak(monkeypatch, [
        {"stderr": CLOSED_KIND_4, "returncode": 0},
        {"lines": [(json.dumps(delivered) + "\n").encode()], "stderr": "connecting... ok."},
    ])
    refusals: list[tuple] = []
    got: list[dict] = []

    worker = RelayWorker("wss://relay.eu.whitenoise.chat", ME, kinds=DM_KINDS,
                         secret="00" * 32,
                         on_refusal=lambda *args: refusals.append(args))

    def handler(_relay: str, event: dict) -> None:
        got.append(event)
        worker.stop()

    await asyncio.wait_for(worker.run(handler), timeout=5)

    assert asked_kinds(calls[0]) == [1059, 4]
    assert len(calls) >= 2, "worker never resubscribed after the refusal"
    assert asked_kinds(calls[1]) == [1059], "kind 4 must be dropped and 1059 kept"
    assert worker.refused == {4}
    assert worker.requested_kinds == DM_KINDS
    assert [event["id"] for event in got] == [delivered["id"]], "1059 delivery was lost"
    assert refusals and refusals[0][1] == {4} and refusals[0][2] == (1059,)


async def test_kind_4_is_still_requested_when_the_relay_accepts_it(monkeypatch):
    delivered = event_id()
    calls = install_fake_nak(monkeypatch, [
        {"lines": [(json.dumps(delivered) + "\n").encode()], "stderr": "connecting... ok."},
    ])
    worker = RelayWorker("wss://nos.lol", ME, kinds=DM_KINDS, secret="00" * 32)

    def handler(_relay: str, _event: dict) -> None:
        worker.stop()

    await asyncio.wait_for(worker.run(handler), timeout=5)

    assert len(calls) == 1
    assert asked_kinds(calls[0]) == [1059, 4]
    assert worker.refused == set()


async def test_a_relay_that_refuses_everything_stops_instead_of_going_filterless(monkeypatch):
    calls = install_fake_nak(monkeypatch, [
        {"stderr": "CLOSED: kind not allowed: 4"},
        {"stderr": "CLOSED: kind not allowed: 1059"},
    ])
    worker = RelayWorker("wss://relay.example", ME, kinds=DM_KINDS, secret="00" * 32)

    await asyncio.wait_for(worker.run(lambda _relay, _event: None), timeout=5)

    assert len(calls) == 2, "a third subscription was spawned with no kinds at all"
    assert all(asked_kinds(argv) for argv in calls), "a filterless REQ is a firehose"
    assert worker.kinds == ()
    assert "refuses every kind" in (worker.last_error or "")
    with pytest.raises(ValueError):
        worker._args()


def test_refusal_is_recorded_where_hng_status_reads_it(tmp_path):
    store = Store(tmp_path / "g.db")

    class _Handler:  # the only attribute _note_refusal touches
        pass

    handler = _Handler()
    handler.store = store
    Gateway._note_refusal(handler, "wss://relay.eu.whitenoise.chat", {4}, (1059,),
                          CLOSED_KIND_4)
    row = store.relay_report()[0]
    assert "refuses kind(s) 4" in row["last_error"]
    assert "reading 1059" in row["last_error"]
    assert "kind not allowed: 4" in row["last_error"]
    store.close()


# -- wiring ----------------------------------------------------------------

async def test_gateway_gives_each_relay_its_own_kind_set(tmp_path, monkeypatch):
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": ["wss://relay.example.com/chat",
                            {"url": "wss://relay.eu.whitenoise.chat", "kinds": [1059]}],
                   "publish": ["wss://relay.example.com/chat"]},
        "store": str(tmp_path / "gw.db"),
        "groups": {}, "policies": [], "defaults": {},
    }
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    # Keep the test off the live wn-agent socket: this is about relay wiring.
    monkeypatch.setattr(Gateway, "_setup_marmot", lambda self, _cfg: None)

    built: list[tuple[str, tuple[int, ...]]] = []

    class FakeWorker:
        def __init__(self, relay, me, **kwargs):
            built.append((relay, kwargs["kinds"]))
            self.relay, self.events, self.last_error = relay, 0, None
            self.kinds = kwargs.get("kinds", ())
            self.p_tag = kwargs.get("p_tag")
            self.authors = ()
            self._stop = asyncio.Event()

        async def run(self, _handler):
            await self._stop.wait()

        def stop(self):
            self._stop.set()

    monkeypatch.setattr(gateway_mod, "RelayWorker", FakeWorker)

    gateway = Gateway(cfg, socket_path=tmp_path / "gw.sock")
    await gateway.start()
    try:
        assert built == [("wss://relay.example.com/chat", (1059, 4)),
                         ("wss://relay.eu.whitenoise.chat", (1059,))]
    finally:
        await gateway.stop()


# -- what `hng status` reads from a worker -----------------------------------

async def test_worker_records_connect_auth_and_event_times_while_the_child_runs(monkeypatch):
    """The facts a verdict needs must exist *during* the run, not only after it dies."""
    delivered = event_id()
    install_fake_nak(monkeypatch, [{
        "stderr": "connecting to relay.example.com... ok.\n"
                  "authenticating to relay.example.com/chat as 740fcd907b10beed…",
        "lines": [(json.dumps(delivered) + "\n").encode()],
    }])
    worker = RelayWorker("wss://relay.example.com/chat", ME, kinds=(1059,), secret="00" * 32)

    def handler(_relay: str, _event: dict) -> None:
        worker.stop()

    await asyncio.wait_for(worker.run(handler), timeout=5)

    assert worker.connected_at is not None, "the connect notice was not recorded"
    assert worker.auth_at is not None, "the auth notice was not recorded"
    assert worker.events == 1 and worker.last_event_at is not None
    assert (worker.last_stderr or "").startswith("authenticating to")
    state = worker.live_state()
    assert state["alive"] is False, "a stopped child must not report as alive"
    assert state["attempts"] == 1
    assert state["last_event_at"] == worker.last_event_at


def test_a_connect_notice_is_not_an_auth_notice():
    worker = RelayWorker("wss://nos.lol", ME, kinds=(1059,))
    worker._note_stderr("connecting to nos.lol... ok.")

    assert worker.connected_at is not None
    assert worker.auth_at is None, "a healthy connect was mistaken for an auth in progress"


def test_live_state_starts_unknown_rather_than_healthy():
    state = RelayWorker("wss://nos.lol", ME, kinds=(1059,)).live_state()

    assert state["alive"] is False
    assert state["attempts"] == 0
    assert state["connected_at"] is None and state["last_event_at"] is None
    assert state["last_error"] is None


async def test_gateway_status_frame_carries_the_live_worker_fields(tmp_path, monkeypatch):
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": ["wss://nos.lol"], "publish": ["wss://nos.lol"]},
        "store": str(tmp_path / "gw.db"),
        "groups": {}, "policies": [], "defaults": {},
    }
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setattr(Gateway, "_setup_marmot", lambda self, _cfg: None)

    gateway = Gateway(cfg, socket_path=tmp_path / "gw.sock")
    gateway.workers = [RelayWorker("wss://nos.lol", "a" * 64, kinds=(1059,))]
    try:
        frame = await gateway.status()
    finally:
        gateway.store.close()

    (entry,) = frame["workers"]
    for field in ("alive", "last_event_at", "connected_at", "auth_at",
                  "attempts", "last_stderr", "refused_kinds"):
        assert field in entry, f"status frame cannot answer health without {field}"
