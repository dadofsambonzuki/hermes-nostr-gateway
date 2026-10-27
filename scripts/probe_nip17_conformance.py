#!/usr/bin/env python3
"""NIP-17 conformance probes — the traps the retired phase-0 wrapper pinned down.

`nip17_wrapper.py` was a `nak`-driven spike whose job was to prove the NIP-17 wire path
before the gateway existed. `hng send` / `hng listen` and the daemon replaced it, but its
findings are protocol rules, not spike trivia: they are encoded in spec §4 and §7, and this
script makes them executable. Retiring the wrapper then retires code, not coverage.

Offline by default, no network: each probe cites the rule it guards. `--live` adds the
§13.1 wire probe — wrap to a throwaway key, publish to real relays, read the wrap back with
the same filter a client uses, decrypt as the throwaway. It never addresses the household
identity, so it leaves no inbox behind.

    .venv/bin/python scripts/probe_nip17_conformance.py
    .venv/bin/python scripts/probe_nip17_conformance.py --live --config config.yaml

Exit status is 0 only when every probe passes.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:  # runnable without `pip install -e .`
    sys.path.insert(0, str(_ROOT / "src"))

import nostr_sdk as sdk  # noqa: E402

from hermes_nostr_gateway import crypto as crypto_mod  # noqa: E402
from hermes_nostr_gateway.crypto import CryptoError, SdkCrypto  # noqa: E402
from hermes_nostr_gateway.planes.nip17 import Nip17Plane  # noqa: E402
from hermes_nostr_gateway.policy import Policy  # noqa: E402
from hermes_nostr_gateway.pool import PoolWorker, RelayPool  # noqa: E402
from hermes_nostr_gateway.store import Store  # noqa: E402

# NIP-59 backdates gift-wrap timestamps uniformly over the two days before publication, so a
# read window narrower than that silently hides messages published seconds ago (spec §3).
BACKDATE_MAX_S = 2 * 86400
WINDOW_SLACK_S = 300  # clock skew allowance on top of the backdate bound


@dataclass
class Probe:
    name: str
    spec: str
    fn: Callable[[], None]


def _keypair() -> tuple[SdkCrypto, SdkCrypto]:
    return (SdkCrypto(sdk.Keys.generate().secret_key().to_hex()),
            SdkCrypto(sdk.Keys.generate().secret_key().to_hex()))


def _p_tags(event: dict) -> list[str]:
    return [tag[1] for tag in event.get("tags", []) if isinstance(tag, list) and tag[0] == "p"]


# ── probes ───────────────────────────────────────────────────────────────────

def wrap_p_is_the_identity() -> None:
    """§4.2 / §13.1: the wrap's `p` tag is the recipient's identity key, and only they read it."""
    alice, bob = _keypair()
    _, stranger = _keypair()
    text = f"conformance: wrap p tag {time.time():.0f}"
    rumor = alice.build_rumor(text, bob.our_pubkey())
    wrap = alice.wrap(rumor, bob.our_pubkey())

    assert wrap["kind"] == 1059, f"expected kind 1059, got {wrap['kind']}"
    assert _p_tags(wrap) == [bob.our_pubkey()], (
        f"wrap addressed to {_p_tags(wrap)}, not the recipient identity {bob.our_pubkey()}")
    # The wrap is keyed by a throwaway (that is the point of NIP-59) but must still be a
    # binding-signed event: a re-signed or hand-built wrap is how a client misses it silently.
    assert wrap["pubkey"] != alice.our_pubkey(), "wrap is signed by the sender identity key"
    assert crypto_mod.verify_event(wrap), "wrap signature does not verify"

    opened = bob.unwrap(wrap)
    assert opened is not None, "recipient cannot open the wrap"
    assert opened["id"] == rumor["id"], "opened rumor id does not match the built rumor"
    assert opened["content"] == text, "opened content differs from what was sent"
    assert opened["pubkey"] == alice.our_pubkey(), "seal author is not the sending identity"
    assert stranger.unwrap(wrap) is None, "a third key decrypted a wrap addressed to someone else"


def misaddressed_wrap_is_refused() -> None:
    """§4.2: a wrap whose `p` tag is not the recipient must be refused before publish.

    A NIP-4E decoupled key changes the `p` tag silently and every standard client misses the
    message with no error, so the check in `SdkCrypto.wrap` is the only thing standing between
    a config mistake and an undeliverable DM. Simulate the bad binding and prove it refuses.
    """
    alice, bob = _keypair()
    _, wrong_recipient = _keypair()
    rumor = alice.build_rumor("must not be published", bob.our_pubkey())
    real = sdk.nip59_make_gift_wrap

    def to_the_wrong_key(keys, recipient, unsigned):  # what a decoupled-key bug looks like
        return real(keys, sdk.PublicKey.parse(wrong_recipient.our_pubkey()), unsigned)

    sdk.nip59_make_gift_wrap = to_the_wrong_key
    try:
        try:
            alice.wrap(rumor, bob.our_pubkey())
        except CryptoError as exc:
            assert "addressed to" in str(exc), f"refused for the wrong reason: {exc}"
        else:
            raise AssertionError("a wrap addressed to another key was built without complaint")
    finally:
        sdk.nip59_make_gift_wrap = real

    assert bob.unwrap(alice.wrap(rumor, bob.our_pubkey())) is not None, "restore failed"


def backdated_wrap_fits_the_read_window() -> None:
    """§3 / §4.1: wraps are backdated up to 2 days, so the subscription window must exceed it."""
    alice, bob = _keypair()
    now = int(time.time())
    wraps = [alice.wrap(alice.build_rumor(f"probe {i}", bob.our_pubkey()), bob.our_pubkey())
             for i in range(8)]

    ages = [now - int(wrap["created_at"]) for wrap in wraps]
    assert all(age >= -WINDOW_SLACK_S for age in ages), f"wrap stamped in the future: {ages}"
    assert all(age <= BACKDATE_MAX_S + WINDOW_SLACK_S for age in ages), (
        f"wrap stamped before the NIP-59 backdate bound: {ages}")
    assert any(age > 60 for age in ages), (
        f"no wrap was backdated; NIP-59 randomisation looks disabled: {ages}")

    me, _ = _keypair()
    pool = RelayPool("11" * 32)
    worker = PoolWorker(pool, "wss://relay.example", me.our_pubkey(), p_tag=me.our_pubkey())
    spec = json.loads(worker.filter().as_json())
    assert 1059 in spec["kinds"], "the read subscription does not filter on kind 1059"
    assert spec.get("#p") == [me.our_pubkey()], "the read subscription is not `#p`-scoped"
    since = int(spec["since"])
    assert since <= now - BACKDATE_MAX_S, (
        f"read window starts at {since} ({now - since}s back), inside the "
        f"{BACKDATE_MAX_S}s NIP-59 backdate window: messages would be hidden")
    assert since >= now - 4 * 86400, f"read window is absurdly wide: {now - since}s"
    assert abs(since - (int(time.time()) - worker.since_days * 86400)) <= 60, (
        "read window is not derived from wall clock; a stored timestamp may have parked it")


def watermark_is_the_inner_rumor() -> None:
    """§3: the watermark is the inner rumor's time, never the wrapper's backdated stamp."""
    sender, receiver = _keypair()
    now = int(time.time())
    rumor_created = now - 7200  # two hours ago; the wrapper will claim something older
    rumor = crypto_mod.build_rumor(sender.our_pubkey(), receiver.our_pubkey(),
                                   "watermark probe", created_at=rumor_created)
    wrap = sender.wrap(rumor, receiver.our_pubkey())
    assert int(wrap["created_at"]) != rumor_created, "wrap was not backdated; cannot distinguish"

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "probe.db")
        policy = Policy({
            "groups": {"sender": [sender.our_pubkey()]},
            "policies": [{"name": "sender-dm", "match": {"group": "sender", "dm": True},
                          "reach": "immediate", "authority": "instructions"}],
            "defaults": {"reach": "drop", "authority": "none"},
        })
        plane = Nip17Plane(receiver, store, policy)
        assert plane.handle("wss://relay.example", wrap) is not None, "message was not accepted"

        row = store.recent(1)[0]
        assert row["created_at"] == rumor_created, (
            f"row watermark is {row['created_at']}, not the inner rumor's {rumor_created} "
            f"(wrap said {wrap['created_at']})")
        assert row["received_at"] >= now - 5, "arrival time is not wall clock"
        store.close()


def read_path_signs_as_the_identity() -> None:
    """§3: the read path authenticates as us, and an empty identity is never a firehose.

    NIP-42 is answered in process by a signer built from our own key — there is no child
    process, so the secret has nowhere to leak to and nothing to inherit it. An
    unauthenticated client on a web-of-trust relay is closed as `restricted` while the
    client only reports "failed to connect", so the key is what makes the read work.
    """
    me, _ = _keypair()
    secret = "22" * 32
    pool = RelayPool(secret)
    assert pool.keys.public_key().to_hex() == SdkCrypto(secret).our_pubkey(), (
        "the read transport's NIP-42 signer is not built from the configured identity")
    worker = PoolWorker(pool, "wss://relay.example", me.our_pubkey(), p_tag=me.our_pubkey())
    assert json.loads(worker.filter().as_json()).get("#p") == [me.our_pubkey()], (
        "the subscription is not `#p`-scoped to us")

    for empty in ("", None, "  "):
        try:
            PoolWorker(pool, "wss://relay.example", empty)  # type: ignore[arg-type]
        except ValueError:
            continue
        raise AssertionError(f"PoolWorker accepted an empty identity: {empty!r}")


def self_copy_is_not_a_message() -> None:
    """§4.1 / §13.9: both copies wrap the same rumor; our own copy must never reach the agent."""
    sender, receiver = _keypair()
    rumor = sender.build_rumor(f"self-copy probe {time.time():.0f}", receiver.our_pubkey())
    peer_wrap = sender.wrap(rumor, receiver.our_pubkey())
    self_wrap = sender.wrap(rumor, sender.our_pubkey())

    assert _p_tags(peer_wrap) == [receiver.our_pubkey()]
    assert _p_tags(self_wrap) == [sender.our_pubkey()], "self-copy is not addressed to us"
    peer_rumor = receiver.unwrap(peer_wrap)
    assert peer_rumor is not None and peer_rumor["id"] == rumor["id"]
    self_rumor = sender.unwrap(self_wrap)
    assert self_rumor is not None and self_rumor["id"] == rumor["id"], (
        "self-copy does not open as the same rumor")

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "probe.db")
        policy = Policy({"groups": {}, "policies": [], "defaults": {"reach": "immediate",
                                                                   "authority": "instructions"}})
        plane = Nip17Plane(sender, store, policy)
        assert plane.handle("wss://relay.example", self_wrap) is None, (
            "our own self-copy surfaced as a message to the agent")
        store.close()


PROBES: list[Probe] = [
    Probe("wrap-p-is-the-identity", "§4.2, §13.1", wrap_p_is_the_identity),
    Probe("misaddressed-wrap-is-refused", "§4.2", misaddressed_wrap_is_refused),
    Probe("backdated-wrap-fits-the-read-window", "§3, §4.1", backdated_wrap_fits_the_read_window),
    Probe("watermark-is-the-inner-rumor", "§3", watermark_is_the_inner_rumor),
    Probe("read-path-signs-as-the-identity", "§3", read_path_signs_as_the_identity),
    Probe("self-copy-is-not-a-message", "§4.1, §13.9", self_copy_is_not_a_message),
]


# ── running them ─────────────────────────────────────────────────────────────

def run_offline(stream=None) -> int:
    out = stream or sys.stdout
    failures = 0
    for probe in PROBES:
        try:
            probe.fn()
        except Exception as exc:  # noqa: BLE001 - a probe's failure is the output
            failures += 1
            print(f"FAIL {probe.name} ({probe.spec}): {exc}", file=out)
        else:
            print(f"PASS {probe.name} ({probe.spec})", file=out)
    print(f"{len(PROBES) - failures}/{len(PROBES)} offline probes passed", file=out)
    return 0 if failures == 0 else 1


def _read_back(relay: str, peer_hex: str, wrap_id: str, timeout: int = 30) -> dict | None:
    """Read the wrap the way a client would, with the filter that client would use."""
    proc = subprocess.run(
        ["nak", "req", "-k", "1059", "-p", peer_hex, "-i", wrap_id, "-l", "1", relay],
        capture_output=True, text=True, timeout=timeout,
    )
    for line in proc.stdout.splitlines():
        if line.strip().startswith("{"):
            return json.loads(line)
    return None


def live_probe(config: str | None, relays: list[str], text: str) -> int:
    """§13.1 on the wire: send to a throwaway key, read back, decrypt as the throwaway.

    Deliberately not addressed to the household identity: a probe should not leave an
    unknown-sender DM in the live inbox for the digest to pick up.
    """
    from hermes_nostr_gateway.cli import load_config, secret_from
    from hermes_nostr_gateway.publisher import publish

    cfg = load_config(config)
    identity_secret = secret_from(cfg)
    me = SdkCrypto(identity_secret)
    peer = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    rumor = me.build_rumor(text, peer.our_pubkey())
    wrap = me.wrap(rumor, peer.our_pubkey())
    targets = relays or list(cfg["relays"]["publish"])[:3]
    print(f"live: {me.our_pubkey()[:12]}… -> throwaway {peer.our_pubkey()[:12]}… via {targets}")

    results = publish(wrap, targets, secret=identity_secret)
    accepted = [relay for relay, (ok, _) in results.items() if ok]
    for relay, (ok, error) in results.items():
        print(f"  publish  {relay}  {'ok' if ok else 'FAILED'}" + (f": {error}" if error else ""))
    if not accepted:
        print("live: no relay accepted the wrap")
        return 1

    retained = 0
    for relay in accepted:
        try:
            event = _read_back(relay, peer.our_pubkey(), wrap["id"])
        except Exception as exc:  # noqa: BLE001 - report, do not crash the run
            print(f"  readback {relay}  error: {exc}")
            continue
        if not event:
            print(f"  readback {relay}  not retained (a publish receipt is not retention)")
            continue
        opened = peer.unwrap(event)
        ok = bool(opened and opened["id"] == rumor["id"] and opened["content"] == text)
        print(f"  readback {relay}  {'retained and decrypts' if ok else 'RETAINED BUT WRONG'}")
        retained += int(ok)
    print(f"live: {retained}/{len(accepted)} accepting relay(s) retained a decryptable wrap")
    return 0 if retained else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NIP-17 conformance probes")
    parser.add_argument("--config", help="config.yaml for --live (identity and relays)")
    parser.add_argument("--live", action="store_true", help="also do a real relay round trip")
    parser.add_argument("--relay", action="append", default=[], help="relay for --live (repeatable)")
    parser.add_argument("--text", default=None, help="text for the live probe")
    args = parser.parse_args(argv)

    status = run_offline()
    if args.live:
        stamp = time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())
        status |= live_probe(args.config, args.relay, args.text or f"hng conformance probe {stamp}")
    return status


if __name__ == "__main__":
    sys.exit(main())
