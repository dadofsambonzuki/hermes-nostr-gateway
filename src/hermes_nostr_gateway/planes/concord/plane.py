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

    def handle_invite(self, relay: str, rumor: dict[str, Any],
                      wrapper_id: str = "") -> Optional[Invite]:
        """Route an unwrapped kind-3313 rumor here. Returns the parsed invite or None.

        ``wrapper_id`` is the gift wrap's id and is what the row is keyed on. The inner
        rumor is unsigned, so it has no id of its own: using it (the old behaviour) gave
        every invite the same empty key, and `INSERT OR IGNORE` then discarded every invite
        after the first — a re-invite granting a new channel never reached the store.
        """
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
                        relay=relay, wrap_id=wrapper_id or str(rumor.get("id") or ""))
        stored = self.store.record_invite(
            id=invite.wrap_id, inviter=invite.inviter,
            community_id=invite.community_id.hex(),
            name=str(bundle.get("name") or ""), relays=invite.relays, bundle=bundle)
        log.info("concord invite: %r from %s… (%s, %d channel(s))",
                 invite.bundle.get("name"), invite.inviter[:12],
                 "recorded" if stored else "already known",
                 len(bundle.get("channels") or []))
        return invite

    def invites(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.store.concord_invites(limit)
