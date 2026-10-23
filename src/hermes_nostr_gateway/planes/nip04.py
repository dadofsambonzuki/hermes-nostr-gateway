"""NIP-04: legacy kind-4 DMs, allowlist-gated and never dual-sent by default."""

from __future__ import annotations

import time
from typing import Any

import nostr_sdk as sdk

from .. import crypto


class Nip04Error(RuntimeError):
    pass


def build_event(keys: sdk.Keys, recipient: str, text: str,
                created_at: int | None = None) -> dict[str, Any]:
    peer = crypto.normalize_pubkey(recipient)
    try:
        ciphertext = crypto.nip04_encrypt(keys, peer, text)
    except Exception as exc:  # noqa: BLE001
        raise Nip04Error(f"cannot encrypt kind 4: {exc}") from exc
    return crypto.sign_event({
        "kind": 4,
        "content": ciphertext,
        "created_at": int(created_at or time.time()),
        "tags": [["p", peer]],
    }, keys)


def open_event(keys: sdk.Keys, event: dict[str, Any]) -> tuple[str, str]:
    sender = str(event.get("pubkey") or "")
    try:
        return sender, crypto.nip04_decrypt(keys, sender, event["content"])
    except Exception as exc:  # noqa: BLE001
        raise Nip04Error(f"cannot decrypt kind 4: {exc}") from exc
