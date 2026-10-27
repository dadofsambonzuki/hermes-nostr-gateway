"""Control-plane writes: the staff write key, editions, and Channel creation.

Reading the Control Plane needs only the `community_root` every member holds. *Writing* needs
the staff-held `control_root`: the plane's wrap is signed by a keypair derived from it, so a
member-signed wrap fails the address check and is dropped by relays and readers alike
(CORD-02 §2/§5). A staff Grant delivers that secret in its own `control_wrap` — the current
epoch and the 32-byte root, NIP-44-encrypted under the granter↔member pairwise key and
base64-encoded (CORD-04 §3).

Editions are kind-3308 rumors in a *plaintext* seal (kind 20014, the rumor's serialized JSON as
the seal's content) inside a kind-1059 wrap encrypted under the community_root-derived control
read key. Each carries `vsk` (entity type), `eid` (entity id), `ev` (version) and, for a
non-owner actor, a `vac` citing the Grant edition whose rank the action claims.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from typing import Any, Iterable, Optional

import nostr_sdk as sdk

from ... import crypto
from .derive import ConcordError, group_key
from .read import Invite
from .write import publish

#: Entity types on the Control Plane (CORD-02 Appendix B).
VSK_COMMUNITY = 0
VSK_ROLE = 1
VSK_CHANNEL = 2
VSK_GRANT = 3
VSK_BANLIST = 4

EDITION_LABEL = b"vector-community/v1/edition"


# ── the staff write key ─────────────────────────────────────────────────────

def parse_control_wrap(*, secret_hex: str, granter_hex: str, blob: str) -> tuple[int, bytes]:
    """`(epoch, control_root)` out of a Grant's `control_wrap`.

    The plaintext is base64(`epoch_be[8] || control_root[32]`). Adoption is the caller's
    decision: the secret is only ours if it derives to the `control_pk` we hold (CORD-04 §3).
    """
    plain = crypto.nip44_decrypt(secret_hex, granter_hex, blob)
    try:
        raw = base64.b64decode(plain, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise ConcordError(f"control_wrap plaintext is not base64: {exc}") from exc
    if len(raw) != 40:
        raise ConcordError(f"control_wrap is {len(raw)} bytes, expected 40")
    return int.from_bytes(raw[:8], "big"), raw[8:]


def control_signer(control_root: bytes, community_id: bytes, epoch: int) -> sdk.Keys:
    """The keypair whose public half is the plane's address — it signs the wraps."""
    gk = group_key("concord/control-signer", control_root, community_id, epoch)
    return sdk.Keys.parse(gk.sk_hex)


def check_control_root(control_root: bytes, invite: Invite, epoch: int) -> None:
    """Refuse a secret that does not derive the `control_pk` this invite names (fail closed)."""
    gk = group_key("concord/control-signer", control_root, invite.community_id, epoch)
    if gk.pk != invite.bundle["control_pk"]:
        raise ConcordError(
            f"control_root derives {gk.pk}, not the control_pk {invite.bundle['control_pk']} "
            f"for epoch {epoch}; not adopting it")


# ── editions ────────────────────────────────────────────────────────────────

def _len64(value: bytes) -> bytes:
    return len(value).to_bytes(8, "big")


def edition_hash(entity_id: bytes, version: int, prev: Optional[bytes], content: str) -> str:
    """The identity a later edition cites in `ep`, and a `vac` cites for its Grant.

    Domain-separated, length-prefixed and fixed-width so two clients holding the same edition
    always compute the same hash, and a compaction re-wrap keeps it (the content is hashed as
    the bytes on the wire, never re-serialized).
    """
    preimage = _len64(EDITION_LABEL) + EDITION_LABEL + entity_id + int(version).to_bytes(8, "big")
    preimage += (b"\x01" + prev) if prev else (b"\x00" + b"\x00" * 32)
    body = content.encode("utf-8")
    preimage += _len64(body) + body
    return hashlib.sha256(preimage).hexdigest()


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def build_edition(*, keys: sdk.Keys, invite: Invite, control_root: bytes, epoch: int,
                  vsk: int, entity_id: bytes, version: int, content: Any,
                  prev: Optional[bytes] = None,
                  vac: Optional[tuple[str, str, str]] = None,
                  created_at: Optional[int] = None) -> dict[str, Any]:
    """One control edition as a signed wrap, ready to publish."""
    me = keys.public_key().to_hex()
    now = int(created_at or time.time())
    content_json = content if isinstance(content, str) else _compact(content)
    tags: list[list[str]] = [["vsk", str(vsk)], ["eid", entity_id.hex()], ["ev", str(version)]]
    if prev:
        tags.append(["ep", prev.hex()])
    if vac:
        tags.append(["vac", vac[0], str(vac[1]), vac[2]])
    rumor = {"kind": 3308, "pubkey": me, "content": content_json, "tags": tags,
             "created_at": now}
    rumor["id"] = crypto.event_id(rumor)

    # Plaintext seal: the rumor's serialized JSON is the seal's content, so a compaction can
    # re-wrap the signed rumor into a new epoch with its signature intact (CORD-02 §5).
    seal = crypto.sign_event({"kind": 20014, "content": json.dumps(rumor, separators=(",", ":")),
                              "tags": [], "created_at": now}, keys)
    read_key = group_key("concord/control", invite.community_root, invite.community_id, epoch)
    wrap = crypto.sign_event({
        "kind": 1059,
        "content": read_key.encrypt(json.dumps(seal, separators=(",", ":"))),
        "tags": [["p", secrets.token_hex(32)]],
        "created_at": now,
    }, control_signer(control_root, invite.community_id, epoch))
    return {"wrap": wrap, "rumor": rumor, "seal": seal, "read_key": read_key}


def verify_edition(built: dict[str, Any], invite: Invite, epoch: int) -> None:
    """Cheap pre-flight: the wrap verifies at the plane's address and the seal is the author's."""
    wrap = built["wrap"]
    if wrap["pubkey"] != invite.bundle["control_pk"]:
        raise ConcordError("wrap author is not the community's control_pk")
    if not crypto.verify_event(wrap):
        raise ConcordError("wrap signature does not verify")
    seal = json.loads(built["read_key"].decrypt(wrap["content"]))
    if int(seal.get("kind", 0)) != 20014:
        raise ConcordError("control wrap does not carry a plaintext seal")
    if not crypto.verify_event(seal):
        raise ConcordError("seal signature does not verify")
    back = json.loads(seal["content"])
    if back.get("id") != built["rumor"]["id"]:
        raise ConcordError("rumor did not round-trip out of the seal")


def grant_citation(member_hex: str, editions: Iterable[dict[str, Any]]) -> tuple[str, str, str]:
    """`vac` tuple for the newest Grant naming `member_hex`, from parsed control rumors.

    Each item is `{"eid", "ev", "content"}` where `content` is the edition's content string
    exactly as published, which is what the hash is taken over.
    """
    best: Optional[dict[str, Any]] = None
    for edition in editions:
        if edition.get("vsk") != VSK_GRANT:
            continue
        try:
            body = json.loads(edition["content"])
        except (ValueError, KeyError):
            continue
        if str(body.get("member") or "") != member_hex:
            continue
        if best is None or int(edition["ev"]) > int(best["ev"]):
            best = edition
    if best is None:
        raise ConcordError(f"no Grant edition for {member_hex[:12]}… in the control stream")
    digest = edition_hash(bytes.fromhex(best["eid"]), int(best["ev"]), None, best["content"])
    return best["eid"], str(best["ev"]), digest


def build_channel(*, keys: sdk.Keys, invite: Invite, control_root: bytes, epoch: int,
                  name: str, private: bool = False, channel_id: Optional[bytes] = None,
                  vac: Optional[tuple[str, str, str]] = None,
                  created_at: Optional[int] = None) -> dict[str, Any]:
    """A new Channel: its first metadata edition (vsk 2, ev 1, no `ep`)."""
    return build_edition(keys=keys, invite=invite, control_root=control_root, epoch=epoch,
                         vsk=VSK_CHANNEL, entity_id=channel_id or secrets.token_bytes(32),
                         version=1, content={"name": name, "private": bool(private)},
                         vac=vac, created_at=created_at)


async def publish_edition(built: dict[str, Any], relays: Iterable[str]) -> dict[str, str]:
    return await publish(built["wrap"], relays)
