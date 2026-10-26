"""`hng send` / `hng listen` must behave like the daemon on the wire.

Both commands are what replaced the phase-0 wrapper, so they inherit its rules: a
subscription is `#p`-scoped and authenticated as the identity (never as nak's machine
key), and a send is durable before it is attempted.
"""

from __future__ import annotations

import json
import time

import nostr_sdk as sdk
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.store import Inbound, Store


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


def test_listen_understands_a_pinned_relay_kind_set(tmp_path, monkeypatch):
    path, _ = make_config(tmp_path)
    cfg = yaml.safe_load(open(path))
    cfg["relays"]["read"] = ["wss://relay.example",
                             {"url": "wss://relay.eu.whitenoise.chat", "kinds": [1059]}]
    open(path, "w").write(yaml.safe_dump(cfg))

    captured = []

    class FakeWorker:
        def __init__(self, relay, me, **kwargs):
            captured.append({"relay": relay, **kwargs})

        async def run(self, handler):
            return None

        def stop(self):
            pass

    monkeypatch.setattr(cli, "RelayWorker", FakeWorker)
    assert cli.main(["--config", path, "listen"]) == 0

    assert [worker["relay"] for worker in captured] == [
        "wss://relay.example", "wss://relay.eu.whitenoise.chat"]
    assert captured[1]["kinds"] == (1059,), "a pinned kind set reached the worker as a dict"


def test_drops_command_shows_why_and_when_and_filters(tmp_path, capsys):
    """`hng drops` is how an operator sees a refusal without opening sqlite."""
    path, _ = make_config(tmp_path)
    now = int(time.time())
    peer = "aa" * 32
    store = Store(tmp_path / "cli.db")
    for ident, plane, reason, why in (
        ("r1", "nip17", "rate_limited", "per-sender rate limit exceeded"),
        ("r2", "nip01", None, "no rule matched"),
    ):
        assert store.add(Inbound(
            id=ident, wrapper_id="w" + ident, sender=peer, kind=14, created_at=now,
            content=f"refused {ident}", conversation=peer, reply_to=None, plane=plane,
            reach="drop", authority="none", rule="defaults", why=why,
            blocked_reason=reason,
        ), now=now)
    store.close()

    assert cli.main(["--config", path, "drops"]) == 0
    out = capsys.readouterr().out
    assert "2 drop(s)" in out
    assert "rate_limited" in out and "per-sender rate limit exceeded" in out
    assert "not_allowlisted" in out and "no rule matched" in out

    assert cli.main(["--config", path, "drops", "--plane", "nip01"]) == 0
    filtered = capsys.readouterr().out
    assert "1 drop(s)" in filtered and "not_allowlisted" in filtered
    assert "rate_limited" not in filtered


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
