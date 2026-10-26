"""Crypto: everything delegates to rust-nostr through the official bindings.

There is deliberately **no hand-rolled cryptography in this module**. NIP-44, NIP-59,
NIP-04, event ids and signing all come from `nostr-sdk`, so we inherit the upstream
implementation and its review. If something cannot be expressed through the bindings,
it does not go in this file — it goes upstream as a binding gap.

NIP-59 detail this wraps: the gift wrap's `p` tag must be the recipient's *identity*
key, and its `created_at` is randomised into the past (the binding does that), which is
why inbound reads need a multi-day `since` window.
"""

from __future__ import annotations

import json
import time
from typing import Any, Optional, Protocol

import nostr_sdk as sdk


class CryptoError(RuntimeError):
    pass


class CryptoBackend(Protocol):
    def our_pubkey(self) -> str: ...
    def build_rumor(self, content: str, peer: str, reply_to: str | None = None) -> dict: ...
    def wrap(self, rumor: dict, recipient: str) -> dict: ...
    def unwrap(self, wrap: dict) -> dict | None: ...


# ── keys and npubs (bindings only) ───────────────────────────────────────────

def normalize_pubkey(value: str) -> str:
    """npub / nprofile / 64-hex -> 64-hex, via the binding's NIP-19 decoder."""
    value = (value or "").strip()
    if not value:
        raise CryptoError("empty pubkey")
    if value.startswith("npub") or value.startswith("nprofile"):
        try:
            return sdk.PublicKey.parse(value).to_hex()
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"invalid npub: {value[:12]}…") from exc
    if len(value) != 64:
        raise CryptoError("pubkey must be npub or 64-char hex")
    return value.lower()


def npub_encode(hex_pubkey: str) -> str:
    return sdk.PublicKey.parse(hex_pubkey).to_bech32()


def npub_decode(value: str) -> str:
    return normalize_pubkey(value)


def parse_secret(value: str) -> str:
    """Accept nsec or 64-char hex; never echo the value back in an error."""
    value = (value or "").strip()
    if value.startswith("nsec"):
        try:
            return sdk.SecretKey.parse(value).to_hex()
        except Exception as exc:  # noqa: BLE001
            raise CryptoError("invalid nsec") from exc
    if len(value) == 64 and all(c in "0123456789abcdefABCDEF" for c in value):
        return value.lower()
    raise CryptoError("secret key must be nsec or 64-char hex")


def pubkey_from_secret(hex_secret: str) -> str:
    try:
        return sdk.Keys.parse(hex_secret).public_key().to_hex()
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("invalid secret key") from exc


# ── events (ids and signatures from the binding) ────────────────────────────

def sign_event(event: dict[str, Any], keys: sdk.Keys) -> dict[str, Any]:
    payload = {
        "kind": int(event["kind"]),
        "content": event.get("content", ""),
        "tags": event.get("tags", []),
        "created_at": int(event.get("created_at") or time.time()),
    }
    unsigned = unsigned_event(payload, keys.public_key().to_hex())
    return json.loads(keys.sign_event(unsigned).as_json())


def unsigned_event(event: dict[str, Any], author_hex: str) -> sdk.UnsignedEvent:
    builder = sdk.EventBuilder(sdk.Kind(int(event["kind"])), event.get("content", ""))
    if event.get("tags"):
        builder = builder.tags([sdk.Tag.parse([str(p) for p in tag]) for tag in event["tags"]])
    if event.get("created_at"):
        builder = builder.custom_created_at(sdk.Timestamp.from_secs(int(event["created_at"])))
    unsigned = builder.finalize_unsigned(sdk.PublicKey.parse(author_hex))
    # ensure_id returns the instance with the id populated; the original stays None.
    return unsigned.ensure_id()


def verify_event(event: dict[str, Any]) -> bool:
    try:
        return sdk.Event.from_json(json.dumps(event)).verify()
    except Exception:  # noqa: BLE001
        return False


def build_rumor(author_hex: str, recipient_hex: str, text: str, kind: int = 14,
                tags: Optional[list[list[str]]] = None,
                created_at: Optional[int] = None) -> dict[str, Any]:
    """An unsigned NIP-17 rumor, id from the binding (the dedupe key)."""
    payload = {
        "kind": int(kind),
        "content": text,
        "tags": [["p", recipient_hex], *(tags or [])],
        "created_at": int(created_at or time.time()),
        "pubkey": author_hex,
    }
    unsigned = unsigned_event(payload, author_hex)
    rumor = json.loads(unsigned.as_json())
    rumor.setdefault("pubkey", author_hex)
    # The id comes from the binding, so dedupe keys always match what the seal reports.
    rumor.setdefault("id", unsigned.id().to_hex())
    return rumor


# ── NIP-04 (legacy DMs) ──────────────────────────────────────────────────────

def nip04_encrypt(keys: sdk.Keys, peer: str, plaintext: str) -> str:
    return sdk.nip04_encrypt(keys.secret_key(), sdk.PublicKey.parse(normalize_pubkey(peer)), plaintext)


def nip04_decrypt(keys: sdk.Keys, peer: str, payload: str) -> str:
    return sdk.nip04_decrypt(keys.secret_key(), sdk.PublicKey.parse(normalize_pubkey(peer)), payload)


# ── NIP-44 with an arbitrary key pair (Concord's stream keys, not just identities) ──

def _secret_key(hex_secret: str) -> sdk.SecretKey:
    return sdk.SecretKey.parse(parse_secret(hex_secret))


def nip44_v2_conversation_key(secret_hex: str, peer_hex: str) -> bytes:
    """NIP-44 v2 conversation key for *any* pair, derived by the binding.

    Concord derives its stream keys from this value, so it has to come from
    `nostr-sdk` rather than a local ECDH: an unreviewed second implementation of
    the curve maths is exactly what this project refuses to ship. The function is
    upstream now (`nip44_v2_conversation_key`, merged in nostrdevkit/nostr-sdk-ffi
    #96), so anything older than that build fails loudly here instead of silently
    falling back.
    """
    derive = getattr(sdk, "nip44_v2_conversation_key", None)
    if derive is None:
        raise CryptoError(
            "this nostr-sdk build does not expose nip44_v2_conversation_key "
            "(upstream in nostrdevkit/nostr-sdk-ffi #96); install the pinned wheel "
            "rather than deriving ECDH locally")
    try:
        return bytes(derive(_secret_key(secret_hex),
                            sdk.PublicKey.parse(normalize_pubkey(peer_hex))))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"cannot derive NIP-44 conversation key: {exc}") from exc


def nip44_encrypt(secret_hex: str, peer_hex: str, plaintext: str) -> str:
    """NIP-44 encrypt for any pair. For a stream key's own pair this is its conv key."""
    try:
        return sdk.nip44_encrypt(_secret_key(secret_hex),
                                 sdk.PublicKey.parse(normalize_pubkey(peer_hex)),
                                 plaintext, sdk.Nip44Version.V2)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"cannot NIP-44 encrypt: {exc}") from exc


def nip44_decrypt(secret_hex: str, peer_hex: str, payload: str) -> str:
    try:
        return sdk.nip44_decrypt(_secret_key(secret_hex),
                                 sdk.PublicKey.parse(normalize_pubkey(peer_hex)),
                                 payload)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"cannot NIP-44 decrypt: {exc}") from exc


def event_id(event: dict[str, Any]) -> str:
    """NIP-01 id from the binding (never a local hash: dedupe keys must match the seal)."""
    author = str(event.get("pubkey") or "")
    if not author:
        raise CryptoError("event has no pubkey; cannot compute its id")
    return unsigned_event(event, author).id().to_hex()


# ── NIP-17 / NIP-59 ──────────────────────────────────────────────────────────

def unwrap_gift(keys: sdk.Keys, wrap: dict[str, Any]) -> dict[str, Any]:
    """kind 1059 -> seal -> rumor. Raises on integrity problems."""
    try:
        event = sdk.Event.from_json(json.dumps(wrap))
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("malformed gift wrap") from exc
    if int(event.kind().as_u16()) != 1059:
        raise CryptoError("not a gift wrap event")
    try:
        unwrapped = sdk.UnwrappedGift.from_gift_wrap(keys, event)
    except Exception as exc:  # noqa: BLE001
        raise CryptoError(f"cannot unwrap gift: {exc}") from exc
    rumor = json.loads(unwrapped.rumor().as_json())
    if rumor.get("pubkey") != unwrapped.sender().to_hex():
        raise CryptoError("rumor author does not match seal author")
    return rumor


class SdkCrypto:
    """NIP-17 send/receive through rust-nostr. No subprocess, no local primitives."""

    def __init__(self, secret_hex: str):
        try:
            self.keys = sdk.Keys.parse(parse_secret(secret_hex))
        except Exception as exc:  # noqa: BLE001
            raise CryptoError("invalid identity secret") from exc
        self._pubkey = self.keys.public_key().to_hex()
        if len(self._pubkey) != 64:
            raise CryptoError("cannot derive our pubkey from the configured secret")

    def our_pubkey(self) -> str:
        return self._pubkey

    def build_rumor(self, content: str, peer: str, reply_to: str | None = None) -> dict:
        peer_hex = normalize_pubkey(peer)
        tags = [["e", reply_to]] if reply_to else []
        return build_rumor(self._pubkey, peer_hex, content, kind=14, tags=tags)

    def wrap(self, rumor: dict, recipient: str) -> dict:
        recipient_hex = normalize_pubkey(recipient)
        unsigned = unsigned_event(rumor, self._pubkey)
        try:
            event = json.loads(
                sdk.nip59_make_gift_wrap(self.keys, sdk.PublicKey.parse(recipient_hex),
                                         unsigned).as_json())
        except Exception as exc:  # noqa: BLE001
            raise CryptoError(f"cannot build gift wrap: {exc}") from exc
        addressed = [tag[1] for tag in event.get("tags", [])
                     if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p"]
        if addressed != [recipient_hex]:
            raise CryptoError(
                f"wrap addressed to {addressed} not {[recipient_hex]} — refusing to publish")
        return event

    def unwrap(self, wrap: dict) -> dict | None:
        try:
            return unwrap_gift(self.keys, wrap)
        except Exception:  # noqa: BLE001 - unreadable wrap is not fatal
            return None
