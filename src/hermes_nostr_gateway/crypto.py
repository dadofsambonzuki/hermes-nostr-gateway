"""Crypto backend seam.

The bindings give us NIP-44 (`Keys.nip44_encrypt/decrypt`) and gift-wrap *unwrapping*
(`UnwrappedGift.from_gift_wrap`) but no wrap builder, so M1 shells out to `nak` for the
two operations it lacks. `NakCrypto` is therefore an interim backend, not the design:
`NativeCrypto` (seal/wrap built on the binding's NIP-44) replaces it without touching
callers. The trap this encodes, learned the hard way:

  * `--use-our-identity-key --use-their-identity-key` is mandatory — without it nak may
    encrypt to a NIP-4E decoupled key, the wrap's `p` tag stops being the recipient's
    identity pubkey, and standard NIP-17 clients never see the message.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Protocol


class CryptoError(RuntimeError):
    pass


class CryptoBackend(Protocol):
    def our_pubkey(self) -> str: ...
    def build_rumor(self, content: str, peer: str, reply_to: str | None = None) -> dict: ...
    def wrap(self, rumor: dict, recipient: str) -> dict: ...
    def unwrap(self, wrap: dict) -> dict | None: ...


class NakCrypto:
    """Interim backend: the proven `nak` calls, behind the interface the daemon wants."""

    def __init__(self, secret_hex: str, nak: str | None = None):
        self._secret = secret_hex
        self._nak = nak or shutil.which("nak") or ""
        if not self._nak:
            raise CryptoError("nak not found on PATH")
        self._pubkey: str | None = None

    # -- plumbing -------------------------------------------------------
    def _run(self, args: list[str], stdin: Any = None) -> str:
        env = dict(os.environ, NOSTR_SECRET_KEY=self._secret)
        proc = subprocess.run(
            [self._nak, *args], input=stdin, capture_output=True, text=True, env=env
        )
        if proc.returncode != 0:
            raise CryptoError(f"nak {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
        return proc.stdout

    @staticmethod
    def _last_json(text: str) -> dict:
        for line in reversed(text.strip().splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
        raise CryptoError(f"no event in nak output: {text[:200]}")

    # -- interface ------------------------------------------------------
    def our_pubkey(self) -> str:
        if self._pubkey is None:
            self._pubkey = self._run(["key", "public"]).strip()
        return self._pubkey

    def build_rumor(self, content: str, peer: str, reply_to: str | None = None) -> dict:
        args = ["event", "-k", "14", "-c", content, "-p", peer]
        if reply_to:
            args += ["-e", reply_to]
        return self._last_json(self._run(args))

    def wrap(self, rumor: dict, recipient: str) -> dict:
        out = self._run(
            [
                "gift",
                "wrap",
                "-p",
                recipient,
                "--use-our-identity-key",
                "--use-their-identity-key",
            ],
            stdin=json.dumps(rumor),
        )
        event = self._last_json(out)
        addressed = [t[1] for t in event.get("tags", []) if t and t[0] == "p"]
        if addressed != [recipient]:
            raise CryptoError(
                f"wrap addressed to {addressed} not {[recipient]} — refusing to publish"
            )
        return event

    def unwrap(self, wrap: dict) -> dict | None:
        try:
            out = self._run(["gift", "unwrap"], stdin=json.dumps(wrap))
        except CryptoError:
            return None
        if not out.strip():
            return None
        return self._last_json(out)
