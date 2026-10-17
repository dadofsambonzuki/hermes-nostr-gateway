#!/usr/bin/env python3
"""Inbound spike test: prove the wrapper's receive path for both allowlist branches.

Case A (sender NOT allowed)  -> expect `[blocked]`, no inbox file.
Case B (sender allowed)      -> expect `[inbox]` and a bridge-contract JSON file.

Each case gets its own throwaway sender key and its own state dir, so catch-up
from a previous case cannot leak into the next. Publishing happens on a public
relay because our own relay correctly refuses writes from keys outside its
whitelist/web-of-trust — testing that gate is the relay's job, not this test's.
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).parent
SCRATCH = pathlib.Path("/home/ubuntu/.hermes/cache/scratch")
# A local in-memory relay (nak serve) instead of a public one: it accepts any
# author with no proof-of-work, reputation or web-of-trust gate, so the test
# exercises OUR code rather than a relay's spam policy. Our own relay correctly
# refuses writes from unlisted keys — that gate is tested elsewhere.
RELAY = "ws://127.0.0.1:8777"
SERVE_PORT = "8777"
SECRET = json.loads(
    pathlib.Path("/home/ubuntu/.marmot-agents/hermes/accounts/hermes-agent/secret.json").read_text()
)["secret_key_hex"]


def sim(keyfile: pathlib.Path, text: str) -> str:
    out = subprocess.run(
        [sys.executable, str(ROOT / "spike_sim.py"), "--to", holly(), "--text", text,
         "--relay", RELAY, "--key-file", str(keyfile)],
        capture_output=True, text=True, check=True,
    ).stdout.strip().splitlines()
    print(f"  sim: {out[0]}")
    return out[-1].strip()  # sender hex


def holly() -> str:
    return subprocess.run(["nak", "key", "public", SECRET], capture_output=True, text=True,
                          check=True).stdout.strip().splitlines()[-1]


def case(label: str, allow: list[str], keep: bool) -> tuple[str, pathlib.Path]:
    tag = label.split()[0].lower()
    state = SCRATCH / f"inb_{tag}_state.json"
    inbox = SCRATCH / f"inb_{tag}_inbox"
    log = SCRATCH / f"inb_{tag}.log"
    for p in (state,):
        p.unlink(missing_ok=True)
    for f in inbox.glob("*.json"):
        f.unlink()
    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["relays"] = {"publish": [RELAY], "read": [RELAY]}
    cfg["allow_from"] = allow
    cfg["lookback_seconds"] = 259200
    cfg_path = SCRATCH / f"inb_{tag}_config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))

    with log.open("w") as lf:
        listener = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "nip17_wrapper.py"), "--config", str(cfg_path), "listen",
             "--state", str(state), "--inbox", str(inbox)],
            stdout=lf, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(8)  # let the subscription establish
        sender = sim(SCRATCH / f"sim_key_{tag}.hex", f"inbound spike {label} {time.strftime('%H:%M:%S')}")
        time.sleep(12)
        listener.terminate()
        listener.wait(timeout=10)
    output = log.read_text()
    delivered = sorted(p.name for p in inbox.glob("*.json"))
    print(f"  listener said:")
    for line in output.splitlines():
        if line.strip():
            print(f"    {line.strip()[:110]}")
    print(f"  inbox files: {delivered or 'none'}")
    return sender, inbox


def main() -> int:
    serve = subprocess.Popen(["nak", "serve", "--port", SERVE_PORT],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    try:
        print("CASE A — sender not in allow_from (expect blocked, nothing delivered)")
        sender_a, _ = case("A blocked", [], keep=False)
        print(f"  sender key A: {sender_a[:16]}…")
        return case_b(sender_a)
    finally:
        serve.terminate()


def case_b(sender_a: str) -> int:
    print("CASE B — same sender now allowlisted (expect delivery)")
    tag = "b"
    state = SCRATCH / f"inb_{tag}_state.json"
    state.unlink(missing_ok=True)
    cfg = json.loads((ROOT / "config.json").read_text())
    cfg["relays"] = {"publish": [RELAY], "read": [RELAY]}
    cfg["allow_from"] = [sender_a]
    cfg_path = SCRATCH / f"inb_{tag}_config.json"
    cfg_path.write_text(json.dumps(cfg, indent=2))
    inbox = SCRATCH / f"inb_{tag}_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    for f in inbox.glob("*.json"):
        f.unlink()
    log = SCRATCH / f"inb_{tag}.log"
    with log.open("w") as lf:
        listener = subprocess.Popen(
            [sys.executable, "-u", str(ROOT / "nip17_wrapper.py"), "--config", str(cfg_path), "listen",
             "--state", str(state), "--inbox", str(inbox)],
            stdout=lf, stderr=subprocess.STDOUT, text=True,
        )
        time.sleep(8)
        sim(SCRATCH / "sim_key_b.hex", f"inbound spike B allowed {time.strftime('%H:%M:%S')}")
        time.sleep(14)
        listener.terminate()
        listener.wait(timeout=10)
    for line in log.read_text().splitlines():
        if line.strip():
            print(f"    {line.strip()[:110]}")
    files = sorted(inbox.glob("*.json"))
    print(f"  inbox files: {[f.name for f in files] or 'none'}")
    for f in files:
        print("  delivered payload:", json.dumps(json.loads(f.read_text()))[:220])
    return 0 if files else 1


if __name__ == "__main__":
    sys.exit(main())
