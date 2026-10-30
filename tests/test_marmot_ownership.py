"""P0: one wn-agent socket, one owner — enforced at startup, not in a README.

Hermes' Marmot platform plugin and this gateway's `marmot` plane read the same control
socket. Both attached means every group message reaches the agent twice: two runs, two
replies, duplicate side effects. The rule now lives in `docs/marmot-ownership.md` and in
`Gateway._guard_marmot_ownership`, which refuses to build when both readers are on.

The tests pin both directions of the same one-line flip, plus the `auto` mode that reads
Hermes' own `platforms.marmot.enabled` — because a copy of that flag in our config would
drift out of step exactly when it matters.
"""

from __future__ import annotations

import json

import nostr_sdk as sdk
import pytest
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.gateway import (
    Gateway,
    MarmotOwnershipError,
    hermes_config_path,
    hermes_marmot_state,
)
from test_marmot import ACCOUNT, GROUP, POLICY, FakeWnAgent


def config_for(tmp_path, *, marmot: dict | None = None) -> dict:
    keys = sdk.Keys.generate()
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps({"secret_key_hex": keys.secret_key().to_hex()}))
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": POLICY["groups"],
        "policies": POLICY["policies"],
        "defaults": POLICY["defaults"],
    }
    if marmot is not None:
        # Default the account to the key this gateway signs with: an id that does not
        # match is a refused plane since the identity guard landed (docs/decisions.md,
        # "One key, two copies"), and these tests are about the ownership flip, not
        # about that refusal.
        cfg["marmot"] = {"account_id_hex": keys.public_key().to_hex(), **marmot}
    return cfg


def hermes_config(tmp_path, *, marmot: dict | None = None) -> str:
    """A Hermes-shaped config file: the key the guard reads, nothing else it needs."""
    path = tmp_path / "hermes-config.yaml"
    data: dict = {"plugins": {"enabled": ["nostr"]}}
    if marmot is not None:
        data["platforms"] = {"marmot": marmot}
    path.write_text(yaml.safe_dump(data))
    return str(path)


@pytest.fixture
def no_nak(monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


def test_both_readers_on_is_a_startup_error(tmp_path):
    """The whole point: this configuration must not become a running process."""
    cfg = config_for(tmp_path, marmot={"enabled": True, "hermes_platform": True})
    with pytest.raises(MarmotOwnershipError) as caught:
        Gateway(cfg)
    said = str(caught.value)
    assert "marmot.enabled: true" in said
    assert "platforms.marmot.enabled" in said
    assert "twice" in said
    # Both ways out are named, or the operator has to go looking.
    assert "platforms.marmot.enabled: false" in said
    assert "docs/marmot-ownership.md" in said


def test_auto_reads_hermes_own_config_and_refuses(tmp_path):
    cfg = config_for(tmp_path, marmot={
        "enabled": True, "hermes_config": hermes_config(tmp_path, marmot={"enabled": True})})
    with pytest.raises(MarmotOwnershipError) as caught:
        Gateway(cfg)
    # The refusal says which file it read, so the operator can check it themselves.
    assert str(tmp_path / "hermes-config.yaml") in str(caught.value)


def test_auto_with_the_plugin_off_lets_the_gateway_own_it(tmp_path, no_nak):
    """The flip: plugin off, `marmot.enabled: true` — one config line, no other edit."""
    cfg = config_for(tmp_path, marmot={
        "enabled": True, "hermes_config": hermes_config(tmp_path, marmot={"enabled": False}),
        "socket": str(tmp_path / "absent.sock"), "home": str(tmp_path / "nowhere"),
        "account_id_hex": ACCOUNT})
    gateway = Gateway(cfg)
    assert gateway.hermes_marmot["enabled"] is False
    assert "no wn-agent control socket" in gateway.marmot_idle


def test_auto_without_a_hermes_config_is_unknown_not_a_refusal(tmp_path, no_nak):
    """A container has no Hermes config at all; that is not a second subscriber."""
    cfg = config_for(tmp_path, marmot={
        "enabled": True, "hermes_config": str(tmp_path / "nothing-here.yaml"),
        "socket": str(tmp_path / "absent.sock"), "home": str(tmp_path / "nowhere"),
        "account_id_hex": ACCOUNT})
    state = hermes_marmot_state(cfg["marmot"])
    assert state["enabled"] is None and "no Hermes config" in state["source"]
    gateway = Gateway(cfg)
    assert gateway.hermes_marmot["enabled"] is None


def test_a_hermes_config_without_the_platform_block_is_off(tmp_path):
    path = tmp_path / "hermes-config.yaml"
    path.write_text(yaml.safe_dump({"plugins": {"enabled": ["nostr"]}}))
    state = hermes_marmot_state({"hermes_config": str(path)})
    assert state == {"enabled": False, "source": str(path)}


def test_the_plugin_alone_is_todays_arrangement_not_a_conflict(tmp_path, no_nak):
    """Until the flip the plugin is the carrier, so this must keep starting."""
    cfg = config_for(tmp_path, marmot={"enabled": False, "hermes_platform": True})
    gateway = Gateway(cfg)
    assert gateway.marmot is None
    assert "marmot.enabled: true" in gateway.marmot_idle
    assert gateway.hermes_marmot == {"enabled": True, "source": "marmot.hermes_platform"}


def test_the_flag_can_be_pinned_against_a_stale_hermes_config(tmp_path, no_nak):
    cfg = config_for(tmp_path, marmot={
        "enabled": True, "hermes_platform": False,
        "hermes_config": hermes_config(tmp_path, marmot={"enabled": True}),
        "socket": str(tmp_path / "absent.sock"), "home": str(tmp_path / "nowhere"),
        "account_id_hex": ACCOUNT})
    gateway = Gateway(cfg)
    assert gateway.hermes_marmot["enabled"] is False
    assert gateway.hermes_marmot["source"] == "marmot.hermes_platform"


def test_a_mistyped_flag_is_a_config_error_not_a_silent_false(tmp_path):
    with pytest.raises(ValueError, match="hermes_platform"):
        hermes_marmot_state({"hermes_platform": "sometimes"})


def test_hermes_home_is_the_default_lookup(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    assert hermes_config_path({}) == tmp_path / "hermes-home" / "config.yaml"
    # An explicit path still wins over the environment.
    assert hermes_config_path({"hermes_config": "~/custom.yaml"}).name == "custom.yaml"


async def test_status_reports_the_other_reader(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    gateway = Gateway(config_for(tmp_path, marmot={
        "enabled": True, "hermes_platform": False, "socket": str(agent.path)}))
    try:
        status = await gateway.status()
        assert "marmot" in status["planes"]
        assert status["marmot"]["hermes_platform"] == {
            "enabled": False, "source": "marmot.hermes_platform"}
    finally:
        await gateway.stop()
        await agent.stop()


def test_serve_refuses_with_a_usable_message_and_a_non_zero_exit(tmp_path, capsys):
    """`hng serve` is where "refuse to start" becomes visible to systemd."""
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps(
        {"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "marmot": {"enabled": True, "hermes_platform": True},
    }))

    assert cli.main(["--config", str(config), "serve"]) == 2
    said = capsys.readouterr().err
    assert said.startswith("refusing to start:")
    assert "platforms.marmot.enabled" in said and "docs/marmot-ownership.md" in said


async def test_a_group_message_still_reaches_the_agent_after_the_flip(tmp_path, no_nak):
    """The flip's whole risk is that the plane stops working: prove it still delivers."""
    from test_marmot import wait_until

    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    gateway = Gateway(config_for(tmp_path, marmot={
        "enabled": True, "hermes_platform": False, "socket": str(agent.path)}))
    try:
        await gateway.start()
        await wait_until(lambda: gateway.marmot_worker.connected)
        delivered = []
        gateway.server.broadcast = _collect(delivered)
        await agent.push(text="after the flip")
        await wait_until(lambda: delivered and delivered[0].get("content") == "after the flip")
        assert delivered[0]["plane"] == "marmot" and delivered[0]["conversation"] == GROUP
    finally:
        await gateway.stop()
        await agent.stop()


def _collect(sink: list):
    async def broadcast(frame: dict) -> None:
        sink.append(frame)
    return broadcast
