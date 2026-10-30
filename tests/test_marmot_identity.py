"""One account key, two copies: drift is refused instead of signed through.

A headless host has no Secret Service, so the same key sits in wn-agent's own
`accounts/<label>/secret.json` and in the copy this gateway reads (`secret_file`, or a
systemd `LoadCredential=` source). Each file is individually valid, so a restore or a
re-bootstrap that touches one and not the other is invisible: wn-agent signs group
traffic as the new npub while the gateway signs DMs as the old one, and nothing in either
process notices.

`Gateway._setup_marmot` now compares wn-agent's account id with `self.me` before the
plane attaches. The comparison is deliberately *plane-scoped*: a mismatched Marmot
identity disables the Marmot plane with the reason in `status`, exactly as a missing
account file does, because the NIP-17 planes are this process's main job and a broken
Marmot home must not take them down. See docs/decisions.md, "One key, two copies, and
the check that makes drift loud".

The id comes from wn-agent's own `accounts/<label>/account.json` whenever that home holds
one; `marmot.account_id_hex` is a fallback for a home with no file yet, and a configured
value that contradicts the file is refused rather than preferred — comparing the config
value instead of the file is how a re-bootstrapped wn-agent attaches while signing as a new
npub (`test_a_configured_account_id_cannot_stand_in_for_wn_agents_file`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.gateway import Gateway, marmot_identity_error
from test_marmot import ACCOUNT, POLICY, FakeWnAgent


def config_for(tmp_path, marmot: Callable[[str], dict[str, Any]]) -> tuple[dict, str]:
    """A gateway config whose secret we know, so the account file can agree or not.

    `marmot` is given the gateway's own public key, because every case here is about
    whether wn-agent's account id equals it.
    """
    keys = sdk.Keys.generate()
    me = keys.public_key().to_hex()
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
        "marmot": {"hermes_platform": False, **marmot(me)},
    }
    return cfg, me


def wn_agent_home(tmp_path, account_id_hex: str, *, label: str = "hermes-agent") -> str:
    home = tmp_path / "wn-agent-home"
    account_dir = home / "accounts" / label
    account_dir.mkdir(parents=True)
    (account_dir / "account.json").write_text(json.dumps({"account_id_hex": account_id_hex}))
    return str(home)


@pytest.fixture
def no_nak(monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())


async def test_a_mismatched_account_disables_the_plane_and_names_both_keys(tmp_path, no_nak):
    """The whole point: one key file replaced and not the other must not sign quietly."""
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, ACCOUNT)},
        )
        assert ACCOUNT != me, "the fixture must hold two different keys"
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is None and gateway.marmot_worker is None
            said = gateway.marmot_error or ""
            # Both sides, so the operator can tell which copy to fix.
            assert ACCOUNT[:12] in said and me[:12] in said
            assert "different identities" in said
            assert "docs/decisions.md" in said
            # Never connected: a plane that cannot be right is not half-attached.
            assert agent.requests == []

            status = await gateway.status()
            assert status["marmot"]["enabled"] is False
            assert status["marmot"]["error"] == said
            # The rest of the gateway is untouched by a Marmot identity problem.
            assert "nip17" in status["planes"]
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_the_account_file_agreeing_attaches_the_plane(tmp_path, no_nak):
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, me)},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is not None and gateway.marmot_worker is not None
            assert gateway.marmot_error is None
            assert gateway.marmot.account_id_hex == me
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_a_configured_account_id_cannot_stand_in_for_wn_agents_file(tmp_path, no_nak):
    """The override must not narrow the guard.

    `marmot.account_id_hex` copied across from Hermes' `platforms.marmot.extra` is the
    gateway's own key, so comparing it with `self.me` agrees by construction while
    wn-agent's file names somebody else. Before the fix this attached with
    `marmot_error is None` — the drift was silent in the one direction that matters.
    """
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, ACCOUNT), "account_id_hex": me},
        )
        assert ACCOUNT != me, "the fixture must hold two different keys"
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is None and gateway.marmot_worker is None
            said = gateway.marmot_error or ""
            # Both ids, and which one came from where.
            assert ACCOUNT[:12] in said and me[:12] in said
            assert "account.json" in said and "marmot.account_id_hex" in said
            assert "docs/decisions.md" in said
            # Still never connected: the refusal happens where the file is read.
            assert agent.requests == []
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_a_configured_account_id_that_matches_the_file_is_harmless(tmp_path, no_nak):
    """An id that agrees with wn-agent's own file must not turn into a refusal."""
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, me), "account_id_hex": me},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is not None and gateway.marmot_error is None
            assert gateway.marmot.account_id_hex == me
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_the_compare_is_hex_not_text(tmp_path, no_nak):
    """Hex is case-insensitive, and both sources are normalised before they are compared.

    `account.json` and the config key can each be written in upper case by a hand edit;
    neither is a different account, and refusing them would be a false alarm on a plane
    that is correct.
    """
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, me.upper()),
                        "account_id_hex": ("  " + me.upper() + "\n")},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is not None and gateway.marmot_error is None
            assert gateway.marmot.account_id_hex == me
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_the_health_note_carries_the_refusal(tmp_path, no_nak):
    """`health` is where an operator looks first; the reason has to be in the notes."""
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": wn_agent_home(tmp_path, ACCOUNT)},
        )
        gateway = Gateway(cfg)
        try:
            said = gateway.marmot_error or ""
            assert said, "the plane must have been refused for this test to mean anything"
            report = await gateway.health()
            assert f"marmot plane not attached: {said}" in report["notes"]
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_a_corrupt_account_file_is_not_patched_over_by_the_config_value(
        tmp_path, no_nak):
    """A file that exists but cannot be read is drift, not an install in progress.

    `resolve_account_id` reports both as `no_account`, and only the absent case may fall
    back to `marmot.account_id_hex`: reading the config over a corrupt file is the same
    "compare the copy, not the daemon" mistake in a different disguise.
    """
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        home = wn_agent_home(tmp_path, ACCOUNT)
        (Path(home) / "accounts" / "hermes-agent" / "account.json").write_text("{not json")
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": home, "account_id_hex": me},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is None
            assert "cannot read" in (gateway.marmot_error or "")
            assert agent.requests == []
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_a_configured_id_is_the_fallback_when_the_home_has_no_account_file(
        tmp_path, no_nak):
    """A home with no account file is an install in progress, not a disagreement.

    This is the only case the config key exists for, so it has to keep working: the plane
    attaches on the configured id, and the file is re-read (and the id becomes redundant)
    as soon as wn-agent writes one.
    """
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path),
                        "home": str(empty_home), "account_id_hex": me},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is not None and gateway.marmot_error is None
            assert gateway.marmot.account_id_hex == me
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


async def test_a_missing_account_file_stays_an_install_error_not_a_mismatch(tmp_path, no_nak):
    """Mid-install must keep working: no account file is not a disagreement about a key."""
    agent = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    try:
        empty_home = tmp_path / "empty-home"
        empty_home.mkdir()
        cfg, me = config_for(
            tmp_path,
            lambda me: {"enabled": True, "socket": str(agent.path), "home": str(empty_home)},
        )
        gateway = Gateway(cfg)
        try:
            assert gateway.marmot is None
            assert "no wn-agent account" in (gateway.marmot_error or "")
            assert me[:12] not in (gateway.marmot_error or "")
        finally:
            await gateway.stop()
    finally:
        await agent.stop()


def test_the_refusal_is_one_string_for_the_log_status_and_this_test():
    said = marmot_identity_error("aa" * 32, "bb" * 32)
    assert "aa" * 6 in said and "bb" * 6 in said
    assert "accounts/<label>/secret.json" in said
    assert "docs/decisions.md" in said
