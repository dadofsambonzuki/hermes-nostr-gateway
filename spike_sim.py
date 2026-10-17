#!/usr/bin/env python3
"""Simulate an inbound NIP-17 DM so the wrapper's receive path can be tested
without a second human.

Generates a throwaway identity, builds rumor -> seal -> gift wrap to the
recipient, and publishes it to a relay the wrapper is configured to read.
Use a public relay for this: our own relay gates writes by whitelist/web-of-trust,
so a throwaway key cannot publish there (that is the relay working as intended).

    python3 spike_sim.py --to <recipient hex> --text "hello" \
        --relay wss://nos.lol [--from-file state/sim_key.hex]

Prints the sender hex on the last line, which is what an allowlist test needs.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys


def nak(args: list[str], stdin: str | None = None, secret: str | None = None, check: bool = True) -> str:
    import os
    env = dict(os.environ)
    if secret:
        env["NOSTR_SECRET_KEY"] = secret
    proc = subprocess.run(["nak", *args], input=stdin, capture_output=True, text=True, env=env)
    if check and proc.returncode != 0:
        sys.exit(f"nak {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    # Status lines ("publishing to ... success.") go to stderr; the event JSON to
    # stdout. Return both — checking for success against stdout alone silently
    # reports every publish as a failure.
    return proc.stdout + proc.stderr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", required=True, help="recipient hex pubkey")
    ap.add_argument("--text", default="simulated inbound NIP-17 DM")
    ap.add_argument("--relay", default="wss://nos.lol")
    ap.add_argument("--key-file", default="", help="reuse a throwaway key (hex) instead of making one")
    args = ap.parse_args()

    if args.key_file and pathlib.Path(args.key_file).exists():
        secret = pathlib.Path(args.key_file).read_text().strip()
    else:
        secret = nak(["key", "generate"], check=False).strip().splitlines()[-1].strip()
        if args.key_file:
            p = pathlib.Path(args.key_file)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(secret + "\n")
            p.chmod(0o600)
    sender = nak(["key", "public", secret]).strip().splitlines()[-1].strip()

    rumor = nak(["event", "-k", "14", "-c", args.text, "-p", args.to], secret=secret)
    wrap = nak(["gift", "wrap", "-p", args.to], stdin=rumor, secret=secret)
    event = [json.loads(l) for l in wrap.splitlines() if l.strip().startswith("{")][-1]
    out = nak(["event", "--auth", args.relay], stdin=json.dumps(event), secret=secret, check=False)
    ok = "success" in out.lower()
    print(f"published={ok} relay={args.relay} wrap={event['id'][:16]}…")
    if not ok:
        print(out.strip()[-300:], file=sys.stderr)
    print(sender)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
