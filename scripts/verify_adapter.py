#!/usr/bin/env python3
"""Verify the Hermes plugin against a live gateway socket, without Hermes.

Runs three checks and prints PASS/FAIL for each:
  1. connect + subscribe
  2. status op over the adapter's transport
  3. inbound delivery from a throwaway key, asserting authority=none -> role_authorized=False

Usage:
  PYTHONPATH=<hermes workspace>:<plugins dir> python3 scripts/verify_adapter.py
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

SOCKET = Path(os.environ.get("NOSTR_GATEWAY_SOCKET",
                             "~/.local/state/hermes-nostr-gateway/gateway.sock")).expanduser()
ME = os.environ.get("NOSTR_ME", "740fcd907b10beedb9530aecf8cde87b274edd4706eafce108aa4fbb45cd2e29")


def nak(args, stdin=None, sec=None):
    env = dict(os.environ)
    if sec:
        env["NOSTR_SECRET_KEY"] = sec
    proc = subprocess.run(["nak", *args], input=stdin, capture_output=True, text=True, env=env)
    return [line for line in proc.stdout.splitlines() if line.startswith("{")]


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

    stranger = subprocess.run(["nak", "key", "generate"], capture_output=True,
                              text=True).stdout.strip().splitlines()[-1]
    rumor = json.loads(nak(["event", "-k", "14", "-p", ME, "-c",
                            "Adapter verification message from a throwaway key."], sec=stranger)[-1])
    wrap = json.loads(nak(["gift", "wrap", "-p", ME, "--use-our-identity-key",
                           "--use-their-identity-key"], stdin=json.dumps(rumor), sec=stranger)[-1])
    subprocess.run(["nak", "event", "wss://nos.lol"], input=json.dumps(wrap),
                   capture_output=True, text=True)

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
