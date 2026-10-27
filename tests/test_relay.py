"""Per-relay DM subscriptions: one refused kind must not cost the relay.

`wss://relay.eu.whitenoise.chat` answers a REQ that names legacy kind 4 with
`CLOSED: ... kind not allowed: 4` and closes the whole subscription, so the 1059
wraps it would have delivered went with it — on a relay that spec §3 says carries
DMs existing nowhere else. A filter is all-or-nothing at the relay, so the kind set
is per relay: pinned in config where known, negotiated at runtime where not.

The negotiation itself lives in `pool.PoolWorker` (exercised by `tests/test_pool.py`);
what stays here is the config shape that decides each relay's kind set, the refusal
text it reacts to, and where the outcome is recorded for `hng status`.
"""

from __future__ import annotations

import asyncio
import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import gateway as gateway_mod
from hermes_nostr_gateway.gateway import Gateway, relay_read_plan, restricted_relay
from hermes_nostr_gateway.relay import refused_kinds
from hermes_nostr_gateway.store import Store

CLOSED_KIND_4 = (
    "wss://relay.eu.whitenoise.chat CLOSED: ERROR: bad req: "
    "filter validation failed: kind not allowed: 4"
)


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
        """Stands in for PoolWorker: records the relay and the kinds handed to it."""

        def __init__(self, _pool, relay, _me, **kwargs):
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

    monkeypatch.setattr(gateway_mod, "PoolWorker", FakeWorker)

    gateway = Gateway(cfg, socket_path=tmp_path / "gw.sock")
    await gateway.start()
    try:
        assert built == [("wss://relay.example.com/chat", (1059, 4)),
                         ("wss://relay.eu.whitenoise.chat", (1059,))]
    finally:
        await gateway.stop()
