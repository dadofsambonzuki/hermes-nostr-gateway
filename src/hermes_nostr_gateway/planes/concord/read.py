"""Concord (Armada/Vector) private-stream reader for CORD-01/02/03/05.

Restored from Hal's extraction (`pr/extract-bitfest-plugin`, review item in
QUEUE.md). It reads only: nothing here signs, publishes or joins; the write path
is a separate module on purpose.

Every wrap is decrypted through a :class:`~.derive.GroupKey`, whose NIP-44
conversation key comes from rust-nostr. The reader never handles raw scalar
multiplication or a hand-rolled cipher.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import nostr_sdk as sdk
from websockets.asyncio.client import connect as ws_connect

from ... import crypto
from .derive import (
    CURVE_ORDER,
    DEFAULT_COMMUNITY_RELAYS,
    ConcordError,
    GroupKey,
    group_key,
    hkdf,
    scalar_normalize,
)

__all__ = [
    "CURVE_ORDER", "DEFAULT_COMMUNITY_RELAYS", "ConcordError", "GroupKey", "group_key",
    "hkdf", "scalar_normalize", "Invite", "verify_bundle", "own_inbox_relays",
    "discover_invite", "open_wrap", "control_keys", "channel_keys", "Channel",
    "Community", "load_community", "Message", "Member", "guestbook_key",
    "read_guestbook", "read_channel", "parse_channel_wrap", "fetch_events",
]


# ── relay IO ─────────────────────────────────────────────────────────────────

async def fetch_events(relays: Iterable[str], filters: list[dict[str, Any]],
                       per_relay_timeout: float = 8.0,
                       total_limit: int = 5000) -> list[dict[str, Any]]:
    """Collect matching events from every reachable relay, de-duplicated by id."""
    seen: dict[str, dict[str, Any]] = {}

    async def one(relay: str) -> None:
        try:
            async with ws_connect(relay, ping_interval=20, max_size=8 * 1024 * 1024,
                                  open_timeout=10) as ws:
                await ws.send(json.dumps(["REQ", "concord", *filters]))
                deadline = time.time() + per_relay_timeout
                while time.time() < deadline and len(seen) < total_limit:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.4, deadline - time.time()))
                    except asyncio.TimeoutError:
                        break
                    try:
                        msg = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(msg, list) or not msg:
                        continue
                    if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                        seen[msg[2]["id"]] = msg[2]
                    elif msg[0] in ("EOSE", "CLOSED"):
                        break
        except Exception:  # noqa: BLE001 - a missing relay is a per-relay failure
            return

    await asyncio.gather(*(one(relay) for relay in relays))
    return list(seen.values())


# ── invite (CORD-05 §6) ──────────────────────────────────────────────────────

@dataclass
class Invite:
    bundle: dict[str, Any]
    inviter: str
    relay: str
    wrap_id: str

    @property
    def relays(self) -> list[str]:
        return list(self.bundle.get("relays") or DEFAULT_COMMUNITY_RELAYS)

    @property
    def community_id(self) -> bytes:
        return bytes.fromhex(self.bundle["community_id"])

    @property
    def community_root(self) -> bytes:
        return bytes.fromhex(self.bundle["community_root"])

    @property
    def root_epoch(self) -> int:
        return int(self.bundle.get("root_epoch") or 0)


def verify_bundle(bundle: dict[str, Any]) -> None:
    """community_id must self-certify the owner (CORD-02 A.4)."""
    owner = bytes.fromhex(bundle["owner"])
    salt = bytes.fromhex(bundle["owner_salt"])
    expected = hashlib.sha256(b"concord/community" + owner + salt).hexdigest()
    if expected != bundle["community_id"]:
        raise ConcordError("invite bundle fails its owner self-certification")


async def own_inbox_relays(secret: str, seed_relays: Iterable[str]) -> list[str]:
    """Relays where a Direct Invite would land: NIP-17 inbox (10050) then NIP-65 read (10002).

    CORD-05 §6 delivers an invite to the recipient's giftwrap inbox, NOT the community's
    relays, so discovery that only queries community relays silently misses new invites.
    """
    me = crypto.pubkey_from_secret(secret)
    events = await fetch_events(seed_relays, [{"kinds": [10050, 10002], "authors": [me], "limit": 6}])
    out: list[str] = []
    for event in sorted(events, key=lambda e: e.get("created_at", 0), reverse=True):
        for tag in event.get("tags") or []:
            if not isinstance(tag, list) or len(tag) < 2:
                continue
            kind = int(event.get("kind", 0))
            if kind == 10050 and tag[0] == "relay":
                out.append(tag[1])
            elif kind == 10002 and tag[0] == "r" and (len(tag) < 3 or tag[2] != "write"):
                out.append(tag[1])
    return [r for r in dict.fromkeys(out) if str(r).startswith("ws")]


async def discover_invite(keys: sdk.Keys, relays: Iterable[str],
                          include_inbox: bool = True,
                          merge_channels: bool = True) -> Optional["Invite"]:
    secret = keys.secret_key().to_hex()
    me = keys.public_key().to_hex()
    relays = list(dict.fromkeys(relays))
    if include_inbox:
        relays = list(dict.fromkeys(relays + await own_inbox_relays(secret, relays)))
    events = await fetch_events(relays, [{
        "kinds": [1059], "#p": [me], "#k": ["3313"], "limit": 20}])
    candidates: list[dict[str, Any]] = []
    for event in sorted(events, key=lambda e: e.get("created_at", 0), reverse=True):
        try:
            rumor = crypto.unwrap_gift(keys, event)
        except crypto.CryptoError:
            continue
        if int(rumor.get("kind", 0)) != 3313:
            continue
        try:
            bundle = json.loads(rumor.get("content") or "{}")
            verify_bundle(bundle)
        except (ValueError, KeyError, TypeError, ConcordError):
            continue
        candidates.append({"bundle": bundle, "event": event,
                           "inviter": rumor.get("pubkey", "")})
    if not candidates:
        return None

    head = candidates[0]
    bundle = dict(head["bundle"])

    # One owner action mints ONE invite per room it grants, so the newest invite is not the
    # richest: picking it alone silently drops the keys of every earlier room. Union the
    # channel grants (newest first) while keeping the newest bundle's roots/relays.
    if merge_channels:
        granted: dict[str, dict[str, Any]] = {}
        for candidate in candidates:
            for channel in candidate["bundle"].get("channels") or []:
                if channel.get("id"):
                    granted.setdefault(channel["id"], channel)
        if granted:
            bundle["channels"] = list(granted.values())

    return Invite(bundle=bundle, inviter=head["inviter"], relay="", wrap_id=head["event"]["id"])


# ── stream reading (CORD-01) ─────────────────────────────────────────────────

def open_wrap(gk: GroupKey, wrap: dict[str, Any], verify: bool = True) -> dict[str, Any]:
    """kind 1059 -> seal (20013 encrypted / 20014 plaintext) -> inner rumor."""
    seal = json.loads(gk.decrypt(wrap["content"]))
    kind = int(seal.get("kind", 0))
    if verify:
        # The wrap proves only that a keyholder published it; the seal signature is
        # what attributes the message to a real author.
        if not crypto.verify_event(seal):
            raise ConcordError("seal signature does not verify")
    if kind == 20014:
        inner_raw = seal["content"]
        if not isinstance(inner_raw, str):
            raise ConcordError("plaintext seal content must be a JSON string")
        rumor = json.loads(inner_raw)
    elif kind == 20013:
        rumor = json.loads(gk.decrypt(seal["content"]))
    else:
        raise ConcordError(f"unsupported seal kind {kind}")
    rumor.setdefault("pubkey", seal.get("pubkey"))
    if rumor.get("pubkey") != seal.get("pubkey"):
        raise ConcordError("seal author mismatch")
    return rumor


def control_keys(invite: Invite) -> tuple[str, GroupKey]:
    """(control_pk address from the bundle, community_root-derived read key)."""
    gk = group_key("concord/control", invite.community_root, invite.community_id,
                   invite.root_epoch)
    return invite.bundle["control_pk"], gk


def channel_keys(invite: Invite, channel_id_hex: str, channel_key_hex: Optional[str] = None,
                 epoch: Optional[int] = None) -> GroupKey:
    secret = bytes.fromhex(channel_key_hex) if channel_key_hex else invite.community_root
    return group_key("concord/channel", secret, bytes.fromhex(channel_id_hex),
                     invite.root_epoch if epoch is None else epoch)


# ── community view ───────────────────────────────────────────────────────────

@dataclass
class Channel:
    id: str
    name: str
    private: bool
    key: Optional[str] = None
    epoch: Optional[int] = None


@dataclass
class Community:
    invite: Invite
    name: str
    channels: list[Channel] = field(default_factory=list)
    relays_seen: int = 0
    control_events: int = 0
    skipped_wraps: int = 0


async def load_community(invite: Invite, *, include_control: bool = True) -> Community:
    name = invite.bundle.get("name") or "Community"
    community = Community(invite=invite, name=name)
    granted = {c["id"]: c for c in invite.bundle.get("channels") or []}

    if include_control:
        control_pk, gk = control_keys(invite)
        wraps = await fetch_events(invite.relays, [
            {"kinds": [1059], "authors": [control_pk], "limit": 500}])
        community.control_events = len(wraps)
        editions: dict[str, list[dict[str, Any]]] = {}
        for wrap in wraps:
            try:
                rumor = open_wrap(gk, wrap)
            except (ConcordError, crypto.CryptoError, ValueError, KeyError):
                community.skipped_wraps += 1
                continue
            if int(rumor.get("kind", 0)) != 3308:
                continue
            tags = {t[0]: t[1:] for t in rumor.get("tags") or [] if isinstance(t, list) and t}
            entity = (tags.get("eid") or [None])[0]
            version = int((tags.get("ev") or ["0"])[0])
            if not entity:
                continue
            editions.setdefault(entity, []).append(
                {"vsk": (tags.get("vsk") or [""])[0], "version": version,
                 "content": rumor.get("content") or "", "id": rumor.get("id")})
        for entity, candidates in editions.items():
            head = max(candidates, key=lambda e: e["version"])
            if head["vsk"] != "2":       # vsk 2 = ChannelMetadata (CORD-02 App. B)
                continue
            try:
                meta = json.loads(head["content"] or "{}")
            except ValueError:
                continue
            if meta.get("deleted"):
                continue
            grant = granted.get(entity)
            community.channels.append(Channel(
                id=entity, name=str(meta.get("name") or entity[:8]),
                private=bool(meta.get("private")),
                key=(grant or {}).get("key"),
                epoch=(grant or {}).get("epoch")))

    # Channels granted in the bundle but absent from Control (private ones still carry a key).
    known = {c.id for c in community.channels}
    for cid, grant in granted.items():
        if cid in known:
            continue
        community.channels.append(Channel(id=cid, name=grant.get("name") or cid[:8],
                                          private=True, key=grant.get("key"),
                                          epoch=grant.get("epoch")))
    community.channels.sort(key=lambda c: c.name.lower())
    return community


@dataclass
class Message:
    id: str
    author: str
    text: str
    created_at: int
    ms: int = 0
    kind: int = 9
    channel_id: str = ""
    quote: Optional[tuple[str, str]] = None   # (quoted rumor id, quoted author)

    @property
    def when(self) -> float:
        return self.created_at + self.ms / 1000


# ── guestbook / membership (CORD-02 §5) ──────────────────────────────────────

@dataclass
class Member:
    pubkey: str
    state: str = "join"          # join | leave | kick
    since: float = 0.0
    last_text: str = ""


def guestbook_key(invite: Invite) -> GroupKey:
    return group_key("concord/guestbook", invite.community_root, invite.community_id,
                     invite.root_epoch)


async def read_guestbook(invite: Invite, *, verify: bool = True) -> list[Member]:
    """Fold Joins/Leaves/Kicks (+ snapshots) into one state per npub."""
    gk = guestbook_key(invite)
    wraps = await fetch_events(invite.relays, [{
        "kinds": [1059], "authors": [gk.pk], "limit": 1000}])
    entries: dict[str, list[dict[str, Any]]] = {}
    snapshots: list[dict[str, Any]] = []
    for wrap in wraps:
        try:
            rumor = open_wrap(gk, wrap, verify=verify)
        except (ConcordError, crypto.CryptoError, ValueError, KeyError):
            continue
        kind = int(rumor.get("kind", 0))
        tags = {t[0]: t[1:] for t in rumor.get("tags") or [] if isinstance(t, list) and t}
        ms = int((tags.get("ms") or ["0"])[0] or 0)
        when = float(int(rumor.get("created_at") or 0)) + ms / 1000
        if kind == 3306:
            verb = (rumor.get("content") or "").strip().lower()
            if verb not in ("join", "leave"):
                continue
            entries.setdefault(rumor.get("pubkey", ""), []).append(
                {"state": verb, "when": when, "id": rumor.get("id", "")})
        elif kind == 3309:
            target = (tags.get("p") or [""])[0]
            if target:
                entries.setdefault(target, []).append(
                    {"state": "kick", "when": when, "id": rumor.get("id", "")})
        elif kind == 3312:
            snapshots.append({"when": when, "content": rumor.get("content") or ""})

    members: dict[str, Member] = {}
    for pubkey, states in entries.items():
        latest = max(states, key=lambda s: (s["when"], s["id"]))
        if latest["state"] == "join":
            members[pubkey] = Member(pubkey=pubkey, state="join", since=latest["when"])
    # A snapshot seeds members who never published their own Join (secondhand attestation).
    for snapshot in sorted(snapshots, key=lambda s: s["when"]):
        try:
            listed = json.loads(snapshot["content"])
        except ValueError:
            continue
        for pubkey in listed if isinstance(listed, list) else []:
            members.setdefault(str(pubkey), Member(pubkey=str(pubkey), state="join",
                                                   since=snapshot["when"]))
    return sorted(members.values(), key=lambda m: (m.since, m.pubkey))


# ── chat plane (CORD-03) ─────────────────────────────────────────────────────

def parse_channel_wrap(gk: GroupKey, wrap: dict[str, Any], channel_id: str,
                       *, verify: bool = True) -> Optional[Message]:
    """One channel wrap -> a Message, or None when it is not chat for this stream."""
    try:
        rumor = open_wrap(gk, wrap, verify=verify)
    except (ConcordError, crypto.CryptoError, ValueError, KeyError):
        return None
    kind = int(rumor.get("kind", 0))
    if kind not in (9, 1111):
        return None
    tags = {t[0]: t[1:] for t in rumor.get("tags") or [] if isinstance(t, list) and t}
    ms = int((tags.get("ms") or ["0"])[0] or 0)
    content = rumor.get("content") or ""
    q = tags.get("q") or []
    quote = None
    if len(q) >= 3 and q[2]:
        quote = (q[0], q[2])
    elif len(q) == 2 and q[1]:
        quote = (q[0], q[1])
    return Message(
        id=rumor.get("id", wrap["id"]), author=rumor.get("pubkey", ""), text=content,
        created_at=int(rumor.get("created_at") or wrap.get("created_at") or 0), ms=ms,
        kind=kind, channel_id=(tags.get("channel") or [channel_id])[0], quote=quote)


async def read_channel(invite: Invite, channel: Channel, *, limit: int = 200,
                       verify: bool = True) -> list[Message]:
    if channel.private and not channel.key:
        raise ConcordError(f"channel {channel.name} is private and its key was not granted")
    if channel.private:
        gk = channel_keys(invite, channel.id, channel.key, channel.epoch)
    else:
        gk = channel_keys(invite, channel.id)
    wraps = await fetch_events(invite.relays, [{
        "kinds": [1059, 21059], "authors": [gk.pk], "limit": limit}])
    messages: list[Message] = []
    for wrap in wraps:
        message = parse_channel_wrap(gk, wrap, channel.id, verify=verify)
        if message is not None:
            messages.append(message)
    messages.sort(key=lambda m: m.when)
    return messages
