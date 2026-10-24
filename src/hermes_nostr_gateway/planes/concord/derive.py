"""Concord key derivations (CORD-02 Appendix A).

The HKDF chain is Concord's own protocol — CORD fixes the salt, label and counter
layout — but every curve operation comes from rust-nostr: the stream public key is
derived by the binding, and the NIP-44 conversation key is
`sdk.nip44_conversation_key(secret, public)`. Concord's "conversation key" is the
self-ECDH of the derived stream keypair, so no elliptic-curve maths lives here.

The released 0.45.1 wheel does not expose that call. A wheel without it is a hard
error (see `crypto.nip44_conversation_key`), never a reason to reimplement ECDH.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Optional

from ... import crypto

# secp256k1 group order: a curve parameter, used to reject an HKDF output that is
# not a valid scalar. Not an implementation of anything.
CURVE_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141

DEFAULT_COMMUNITY_RELAYS = ("wss://relay.ditto.pub", "wss://relay.dreamith.to",
                            "wss://jskitty.com/nostr", "wss://asia.vectorapp.io/nostr")


class ConcordError(RuntimeError):
    pass


def _hkdf_prk(secret: bytes) -> bytes:
    """HKDF-Extract with a zero-length salt (CORD A.1: salt = ∅)."""
    return hmac.new(b"", secret, hashlib.sha256).digest()


def _hkdf_expand32(prk: bytes, info: bytes) -> bytes:
    """One block of HKDF-Expand -- exactly the 32 bytes CORD asks for."""
    return hmac.new(prk, info + b"\x01", hashlib.sha256).digest()


def _info(label: str, id32: bytes, epoch: Optional[int]) -> bytes:
    info = label.encode() + b"\x00" + id32
    if epoch is not None:
        info += int(epoch).to_bytes(8, "big")
    return info


def hkdf(secret: bytes, label: str, id32: bytes, epoch: Optional[int]) -> bytes:
    return _hkdf_expand32(_hkdf_prk(secret), _info(label, id32, epoch))


def scalar_normalize(secret: bytes, label: str, id32: bytes, epoch: Optional[int]) -> int:
    """CORD A.3: retry with an appended counter byte if the seed is not a valid scalar."""
    prk = _hkdf_prk(secret)
    base = _info(label, id32, epoch)
    for counter in (None, *range(256)):
        info = base if counter is None else base + bytes([counter])
        candidate = int.from_bytes(_hkdf_expand32(prk, info), "big")
        if 0 < candidate < CURVE_ORDER:
            return candidate
    raise ConcordError("scalar_normalize exhausted its retry counter")


@dataclass(frozen=True)
class GroupKey:
    """A Concord stream address: the derived scalar, its x-only key, and the NIP-44 key."""

    sk: int
    pk: str          # x-only hex, the stream address
    conv_key: bytes  # NIP-44 self-ECDH, from the binding

    @property
    def sk_hex(self) -> str:
        return f"{self.sk:064x}"

    def encrypt(self, plaintext: str) -> str:
        """NIP-44 with this stream keypair -- the binding derives the same conv key."""
        return crypto.nip44_encrypt(self.sk_hex, self.pk, plaintext)

    def decrypt(self, payload: str) -> str:
        return crypto.nip44_decrypt(self.sk_hex, self.pk, payload)


def group_key(label: str, secret: bytes, id32: bytes, epoch: Optional[int]) -> GroupKey:
    sk = scalar_normalize(secret, label, id32, epoch)
    sk_hex = f"{sk:064x}"
    pk = crypto.pubkey_from_secret(sk_hex)
    conv_key = crypto.nip44_conversation_key(sk_hex, pk)
    return GroupKey(sk=sk, pk=pk, conv_key=conv_key)
