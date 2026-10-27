"""NIP-17 plane: gift wrap -> seal -> rumor, routed by *inner* kind.

Kind 1059 is a transport, not a message type: it carries DMs (14), reactions (7) and
Concord invites (3313). Anything unrecognised is dropped with a reason, never surfaced
to the agent.
"""

from __future__ import annotations

import logging
from typing import Callable, Protocol

from ..media import imeta_attachments
from ..policy import Policy
from ..store import Inbound, Store

log = logging.getLogger("hng.plane.nip17")

DM_KINDS = {14, 7}
INVITE_KINDS = {3313}
ROUTED_KINDS = DM_KINDS | INVITE_KINDS

# An unwrapped invite rumor goes here; the Concord plane owns parsing and storage.
# The gift wrap's own id comes with it: the inner rumor is unsigned and has no id, so the
# wrapper is the only stable identity an invite has (and the row's primary key).
InviteSink = Callable[[str, dict, str], object]


class Crypto(Protocol):
    def our_pubkey(self) -> str: ...
    def unwrap(self, wrap: dict) -> dict | None: ...


class Nip17Plane:
    def __init__(self, crypto: Crypto, store: Store, policy: Policy,
                 on_invite: InviteSink | None = None):
        self.crypto = crypto
        self.store = store
        self.policy = policy
        self.on_invite = on_invite
        self.me = crypto.our_pubkey()

    def handle(self, relay: str, wrap: dict) -> Inbound | None:
        if wrap.get("kind") != 1059:
            return None
        wrapper_id = wrap.get("id", "")
        if self.store.wrapper_seen(wrapper_id, relay):
            return None

        rumor = self.crypto.unwrap(wrap)
        if not rumor:
            self.store.note_relay(relay, error="unwrap failed")
            return None

        sender = rumor.get("pubkey", "")
        kind = int(rumor.get("kind", 0))
        if sender == self.me:
            return None  # our own self-copy
        if kind not in ROUTED_KINDS:
            return None  # unknown payload in a wrapper: not ours to interpret
        if kind in INVITE_KINDS:
            # A Concord invite shares the 1059 wrapper with DMs; it is never chat and
            # must never reach the agent as a message (spec §14). Hand it to the
            # Concord plane, which verifies and records it.
            if self.on_invite is not None:
                try:
                    self.on_invite(relay, rumor, wrapper_id)
                except Exception:  # noqa: BLE001 - a bad invite must not kill the relay
                    log.exception("concord invite sink failed")
            return None

        tags = rumor.get("tags", [])
        reply_to = next((t[1] for t in tags if t and t[0] == "e"), None)
        recipient = next((t[1] for t in tags if t and t[0] == "p"), None)
        if recipient and recipient != self.me:
            return None  # addressed to someone else

        decision = self.policy.decide(sender=sender, kind=kind, dm=True,
                                      event_id=rumor.get("id"),
                                      text=rumor.get("content", ""))
        msg = Inbound(
            id=rumor["id"],
            wrapper_id=wrapper_id,
            sender=sender,
            kind=kind,
            created_at=int(rumor.get("created_at", 0)),
            content=rumor.get("content", ""),
            conversation=sender,
            reply_to=reply_to,
            reach=decision.reach,
            authority=decision.authority,
            rule=decision.rule,
            why=decision.why,
            blocked_reason=decision.blocked_reason,
            interval_s=decision.interval_s,
            # NIP-92 attachments are metadata on the text rumor, not a separate kind;
            # the bytes stay remote until the adapter asks for them (spec §4b).
            attachments=imeta_attachments(tags) or None,
        )
        if decision.duplicate:
            return None
        return msg if self.store.add(msg) else None
