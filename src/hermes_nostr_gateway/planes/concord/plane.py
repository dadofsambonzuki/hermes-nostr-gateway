"""The Concord plane's inbound half: invites are parsed and recorded, never messages.

Spec §14: inner kind 3313 shares the kind-1059 wrapper with DMs, so it must be
routed by *inner* kind. A Concord invite is not chat and must never reach the
agent as a message or trigger a run; this plane verifies the bundle, persists it,
and leaves joining to an explicit call.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

from ...store import Store
from .derive import ConcordError
from .read import Invite, verify_bundle

log = logging.getLogger("hng.plane.concord")


class ConcordPlane:
    def __init__(self, store: Store):
        self.store = store

    def handle_invite(self, relay: str, rumor: dict[str, Any]) -> Optional[Invite]:
        """Route an unwrapped kind-3313 rumor here. Returns the parsed invite or None."""
        try:
            bundle = json.loads(rumor.get("content") or "{}")
            if not isinstance(bundle, dict):
                raise ValueError("invite content is not a bundle object")
            verify_bundle(bundle)
        except (ValueError, KeyError, TypeError, ConcordError) as exc:
            log.info("concord invite rejected: %s", exc)
            self.store.note_relay(relay, error=f"concord invite: {exc}"[:200])
            return None
        invite = Invite(bundle=bundle, inviter=str(rumor.get("pubkey") or ""),
                        relay=relay, wrap_id=str(rumor.get("id") or ""))
        self.store.record_invite(
            id=invite.wrap_id, inviter=invite.inviter,
            community_id=invite.community_id.hex(),
            name=str(bundle.get("name") or ""), relays=invite.relays, bundle=bundle)
        log.info("concord invite: %r from %s…", invite.bundle.get("name"),
                 invite.inviter[:12])
        return invite

    def invites(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.concord_invites(limit)
