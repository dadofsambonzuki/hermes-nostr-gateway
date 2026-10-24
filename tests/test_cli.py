"""`hng send` / `hng listen` must behave like the daemon on the wire.

Both commands are what replaced the phase-0 wrapper, so they inherit its rules: a
subscription is `#p`-scoped and authenticated as the identity (never as nak's machine
key), and a send is durable before it is attempted.
"""

from __future__ import annotations

import json

import nostr_sdk as sdk
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.store import Store


def make_config(tmp_path) -> tuple[str, str]:
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    cfg = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": ["wss://relay.example"], "publish": ["wss://relay.example"]},
        "store": str(tmp_path / "cli.db"),
        "groups": {}, "policies": [], "defaults": {},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(cfg))
    return str(path), secret


def test_listen_is_scoped_to_our_inbox_and_signed_as_us(tmp_path, monkeypatch):
    path, secret = make_config(tmp_path)
    captured = []

    class FakeWorker:
        def __init__(self, relay, me, **kwargs):
            captured.append({"relay": relay, "me": me, **kwargs})

        async def run(self, handler):
            return None

        def stop(self):
            pass

    monkeypatch.setattr(cli, "RelayWorker", FakeWorker)
    assert cli.main(["--config", path, "listen"]) == 0

    assert captured, "listen started no workers"
    for worker in captured:
        assert worker["p_tag"] == worker["me"], "subscription is not `#p`-scoped to us"
        assert worker["secret"] == secret, "worker would authenticate as nak's machine key"


def test_send_publishes_with_the_identity_secret(tmp_path, monkeypatch):
    path, secret = make_config(tmp_path)
    peer = sdk.Keys.generate().public_key().to_hex()
    calls = []

    def fake_publish(event, relays, secret=None):
        calls.append({"event": event, "relays": list(relays), "secret": secret})
        return {relay: (True, None) for relay in relays}

    monkeypatch.setattr(cli, "publish", fake_publish)
    monkeypatch.setattr(cli, "recipient_relays", lambda cfg, peer: ["wss://relay.example"])

    assert cli.main(["--config", path, "send", "--to", peer, "--text", "hello"]) == 0
    assert len(calls) == 2, "expected the peer copy and the self-copy"
    assert all(call["secret"] == secret for call in calls), "NIP-42 would sign as the wrong key"
    for call in calls:
        assert call["event"]["kind"] == 1059

    store = Store(tmp_path / "cli.db")
    rows = store.recent(5)
    assert rows and rows[0]["authority"] == "self" and rows[0]["content"] == "hello"
    assert store.pending_wraps() == [], "successful sends left retryable wraps behind"
    store.close()


def test_send_keeps_a_failed_wrap_for_the_retry_loop(tmp_path, monkeypatch):
    path, _ = make_config(tmp_path)
    peer = sdk.Keys.generate().public_key().to_hex()

    def half_failing(event, relays, secret=None):
        return {"wss://relay.example": (True, None), "wss://down.example": (False, "503")}

    monkeypatch.setattr(cli, "publish", half_failing)
    monkeypatch.setattr(
        cli, "recipient_relays", lambda cfg, peer: ["wss://relay.example", "wss://down.example"])

    assert cli.main(["--config", path, "send", "--to", peer, "--text", "retry me"]) == 0

    store = Store(tmp_path / "cli.db")
    pending = store.pending_wraps()
    assert {row["relay"] for row in pending} == {"wss://down.example"}
    # The stored event is the signed wrap, so a retry republishes the same id (spec §7).
    assert json.loads(pending[0]["event"])["kind"] == 1059
    store.close()
