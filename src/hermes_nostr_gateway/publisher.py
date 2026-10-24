"""Publishing: one function, per-relay truth.

Retries republish the *same* signed event. Re-wrapping would produce a new event id and
the peer's client would show the message twice (spec §7), so wraps are persisted first
and retried from storage rather than regenerated.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Iterable


def publish(event: dict, relays: Iterable[str], secret: str | None = None) -> dict[str, tuple[bool, str | None]]:
    nak = shutil.which("nak") or "nak"
    out: dict[str, tuple[bool, str | None]] = {}
    import os
    payload = json.dumps(event)
    env = dict(os.environ, **({"NOSTR_SECRET_KEY": secret} if secret else {}))
    for relay in relays:
        proc = subprocess.run(
            [nak, "event", "--auth", relay], input=payload, capture_output=True, text=True,
            env=env,
        )
        text = (proc.stdout + proc.stderr).lower()
        ok = "success" in text
        out[relay] = (ok, None if ok else (proc.stderr or proc.stdout).strip().splitlines()[-1][:200]
                      if (proc.stderr or proc.stdout).strip() else "no response")
    return out
