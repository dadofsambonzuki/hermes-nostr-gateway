"""Concord write path (CORD-01/02/03): Channel and Guestbook publishes.

Restored from Hal's extraction (`pr/extract-bitfest-plugin`, QUEUE.md). The
identity is passed in as `nostr_sdk.Keys` instead of being read from the
environment inside each build function (spec §18 review item 2). The reader
stays read-only; everything that signs lives here.

Envelope shape (CORD-01):
  wrap(1059, signed by the plane's derived stream key)
    content = nip44(plane_conv_key, seal)
    seal(20013, signed by the author's real key)
      content = nip44(plane_conv_key, rumor)

The plane conversation key is obtained by encrypting through the binding with
the derived stream keypair -- the same NIP-44 key CORD derives, with no local
cipher or ECDH.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from typing import Any, Iterable, Optional

import nostr_sdk as sdk
from websockets.asyncio.client import connect as ws_connect

from ... import crypto
from . import read as c
from .derive import GroupKey


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _seal_and_wrap(rumor: dict[str, Any], gk: GroupKey, keys: sdk.Keys) -> dict[str, Any]:
    seal = crypto.sign_event({
        "kind": 20013,
        "content": gk.encrypt(_compact(rumor)),
        "tags": [],
        "created_at": rumor["created_at"],
    }, keys)
    stream_keys = sdk.Keys.parse(gk.sk_hex)
    return crypto.sign_event({
        "kind": 1059,
        "content": gk.encrypt(_compact(seal)),
        "tags": [["p", secrets.token_hex(32)]],
        "created_at": rumor["created_at"],
    }, stream_keys)


def build_channel_message(keys: sdk.Keys, invite: c.Invite, channel_id: str, text: str,
                          channel_key: Optional[str] = None, epoch: Optional[int] = None,
                          quote: Optional[tuple[str, str]] = None,
                          created_at: Optional[int] = None) -> dict[str, Any]:
    """Kind 9 message at a Channel's address. ``quote`` is (rumor_id, author_hex)."""
    me = keys.public_key().to_hex()
    gk = c.channel_keys(invite, channel_id, channel_key, epoch)
    now = int(created_at or time.time())
    ch_epoch = int(invite.root_epoch if epoch is None else epoch)
    tags = [["channel", channel_id], ["epoch", str(ch_epoch)],
            ["ms", str(secrets.randbelow(1000))]]
    if quote:
        tags.append(["q", quote[0], "", quote[1]])
    rumor = {"kind": 9, "pubkey": me, "content": text, "tags": tags, "created_at": now}
    rumor["id"] = crypto.event_id(rumor)
    wrap = _seal_and_wrap(rumor, gk, keys)
    return {"wrap": wrap, "rumor": rumor, "stream_pk": gk.pk}


def build_guestbook_join(keys: sdk.Keys, invite: c.Invite, creator_hex: str = "",
                         label: str = "", created_at: Optional[int] = None) -> dict[str, Any]:
    """Kind 3306 join at the Guestbook address (self-signed; the seal is the authority)."""
    me = keys.public_key().to_hex()
    gk = c.guestbook_key(invite)
    now = int(created_at or time.time())
    tags = [["ms", str(secrets.randbelow(1000))]]
    if creator_hex:
        tags.append(["invite", creator_hex, label])
    rumor = {"kind": 3306, "pubkey": me, "content": "join", "tags": tags, "created_at": now}
    rumor["id"] = crypto.event_id(rumor)
    return {"wrap": _seal_and_wrap(rumor, gk, keys),
            "rumor": rumor, "stream_pk": gk.pk}


def verify_envelope(built: dict[str, Any], gk: GroupKey) -> None:
    """Cheap pre-flight: signatures verify and the rumor round-trips out of the wrap."""
    wrap, rumor = built["wrap"], built["rumor"]
    if wrap["pubkey"] != built["stream_pk"]:
        raise c.ConcordError("wrap author is not the plane's stream key")
    if not crypto.verify_event(wrap) or not crypto.verify_event(
            json.loads(gk.decrypt(wrap["content"]))):
        raise c.ConcordError("wrap or seal signature does not verify")
    back = c.open_wrap(gk, wrap)
    if back.get("id") != rumor["id"] or back.get("content") != rumor["content"]:
        raise c.ConcordError("rumor did not round-trip out of the wrap")


async def publish(event: dict[str, Any], relays: Iterable[str]) -> dict[str, str]:
    """Publish one signed event; returns {relay: outcome}."""
    results: dict[str, str] = {}

    async def one(relay: str) -> None:
        try:
            async with ws_connect(relay, open_timeout=12, ping_interval=20) as ws:
                await ws.send(json.dumps(["EVENT", event]))
                while True:
                    msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=12))
                    if msg[0] == "OK" and msg[1] == event["id"]:
                        results[relay] = "ok" if msg[2] else f"rejected: {msg[3][:120]}"
                        return
                    if msg[0] in ("NOTICE", "CLOSED", "AUTH"):
                        results[relay] = "auth-required" if msg[0] == "AUTH" else \
                            f"{msg[0]}: {str(msg[1])[:120]}"
                        return
        except Exception as exc:                     # noqa: BLE001 - per-relay failure
            results[relay] = f"{type(exc).__name__}: {exc}"

    await asyncio.gather(*(one(r) for r in relays))
    return results
