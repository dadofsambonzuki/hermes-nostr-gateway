#!/usr/bin/env python3
"""Verify the Hermes plugin against a live gateway socket, without Hermes.

Runs three checks and prints PASS/FAIL for each:
  1. connect + subscribe
  2. status op over the adapter's transport
  3. inbound delivery from a throwaway key, asserting authority=none -> role_authorized=False

The throwaway message is built and published with the gateway's own bindings, so the check
needs no external binary: the stranger key, the kind-14 rumor and the gift wrap come from
`nostr-sdk`, and the publish goes through the gateway's own publisher — the bytes the plugin
receives are the bytes a real sender's client would produce.

Usage:
  PYTHONPATH=<hermes workspace>:<plugins dir> \
    /path/to/hermes-nostr-gateway/.venv/bin/python scripts/verify_adapter.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "src") not in sys.path:  # runnable without `pip install -e .`
    sys.path.insert(0, str(_ROOT / "src"))

import nostr_sdk as sdk  # noqa: E402

from hermes_nostr_gateway.crypto import SdkCrypto  # noqa: E402
from hermes_nostr_gateway.publisher import publish  # noqa: E402

SOCKET = Path(os.environ.get("NOSTR_GATEWAY_SOCKET",
                             "~/.local/state/hermes-nostr-gateway/gateway.sock")).expanduser()
ME = os.environ.get("NOSTR_ME", "740fcd907b10beedb9530aecf8cde87b274edd4706eafce108aa4fbb45cd2e29")
#: A public relay the plugin reads, so the check does not depend on the household's own set.
RELAY = os.environ.get("NOSTR_VERIFY_RELAY", "wss://nos.lol")
TEXT = "Adapter verification message from a throwaway key."


def build_adapter():
    from nostr.adapter import NostrGatewayAdapter

    adapter = object.__new__(NostrGatewayAdapter)  # transport-level test: no Base wiring
    adapter._socket = SOCKET
    adapter._closing = False
    adapter._connected = asyncio.Event()
    adapter._reader_task = None
    adapter.platform = "nostr"
    adapter.enforces_own_access_policy = True
    adapter.seen = []

    async def handle(event):
        adapter.seen.append(event)

    adapter.handle_message = handle
    return adapter


def send_from_a_stranger() -> dict[str, tuple[bool, str | None]]:
    """A kind-14 rumor from a key in nobody's web of trust, wrapped and published."""
    stranger_secret = sdk.Keys.generate().secret_key().to_hex()
    stranger = SdkCrypto(stranger_secret)
    rumor = stranger.build_rumor(TEXT, ME)
    wrap = stranger.wrap(rumor, ME)
    return publish(wrap, [RELAY], secret=stranger_secret)


async def main() -> int:
    adapter = build_adapter()
    failures = 0

    if await asyncio.wait_for(adapter.connect(), 20):
        print("PASS connect + subscribe")
    else:
        print("FAIL connect + subscribe")
        return 1

    status = await adapter._request({"op": "status"}, timeout=20)
    if status.get("type") == "status" and status.get("identity"):
        print(f"PASS status op (identity {status['identity'][:12]}…)")
    else:
        print(f"FAIL status op: {status}")
        failures += 1

    results = send_from_a_stranger()
    accepted = [relay for relay, (ok, _) in results.items() if ok]
    for relay, (ok, error) in results.items():
        print(f"  publish    {relay}  {'ok' if ok else 'FAILED'}" + (f": {error}" if error else ""))
    if not accepted:
        print(f"FAIL throwaway wrap reached no relay: {results}")
        failures += 1

    for _ in range(20):
        if adapter.seen:
            break
        await asyncio.sleep(1)
    if adapter.seen and adapter.seen[0].source.role_authorized is False:
        print("PASS inbound delivery (authority=none -> role_authorized=False)")
    elif adapter.seen:
        print("FAIL inbound delivered with wrong authority")
        failures += 1
    else:
        print("FAIL inbound not observed within 20s")
        failures += 1

    await adapter.disconnect()
    print("RESULT:", "ok" if failures == 0 else f"{failures} failure(s)")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
