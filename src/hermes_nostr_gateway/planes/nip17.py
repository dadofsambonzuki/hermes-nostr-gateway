"""NIP-17 plane: gift wrap -> seal -> rumor, routed by *inner* kind.

Kind 1059 is a transport, not a message type: it carries DMs (14), reactions (7) and
Concord invites (3313). Anything unrecognised is dropped with a reason, never surfaced
to the agent.
"""

from __future__ import annotations

from typing import Protocol

from ..policy import Policy
from ..store import Inbound, Store

DM_KINDS = {14, 7}
INVITE_KINDS = {3313}
ROUTED_KINDS = DM_KINDS | INVITE_KINDS


class Crypto(Protocol):
    def our_pubkey(self) -> str: ...
    def unwrap(self, wrap: dict) -> dict | None: ...


class Nip17Plane:
    def __init__(self, crypto: Crypto, store: Store, policy: Policy):
        self.crypto = crypto
        self.store = store
        self.policy = policy
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
            return None  # Concord invite: handled by the concord plane, never a DM

        tags = rumor.get("tags", [])
        reply_to = next((t[1] for t in tags if t and t[0] == "e"), None)
        recipient = next((t[1] for t in tags if t and t[0] == "p"), None)
        if recipient and recipient != self.me:
            return None  # addressed to someone else

        decision = self.policy.decide(sender=sender, kind=kind, dm=True, event_id=rumor.get("id"))
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
        )
        if decision.duplicate:
            return None
        return msg if self.store.add(msg) else None
