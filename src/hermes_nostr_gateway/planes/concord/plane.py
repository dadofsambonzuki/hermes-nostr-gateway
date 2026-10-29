"""The Concord plane's inbound half: invites are parsed and recorded, never messages.

Spec §14: inner kind 3313 shares the kind-1059 wrapper with DMs, so it must be
routed by *inner* kind. A Concord invite is not chat and must never reach the
agent as a message or trigger a run; this plane verifies the bundle, persists it,
and leaves joining to an explicit call.

Whether that call happens without asking is the control pane's question, and this plane
answers it (`invite_verdict`): an inviter in `control.trusted_npubs`, global or the plane's
own list, joins on their word. Anyone else's invite is held until a human accepts it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

import nostr_sdk as sdk

from ... import addressing
from ...control import ControlConfig
from ...projects import ProjectMap
from ...store import Inbound, Store
from . import read as c
from .channels import ChannelStream, decides, has_mention
from .channels import streams as channel_streams
from .derive import ConcordError, GroupKey
from .read import Invite, open_wrap, verify_bundle

log = logging.getLogger("hng.plane.concord")

#: Inner kinds that are chat on a channel stream. Replies (1111) are read as messages.
CHAT_KINDS = (9, 1111)
#: NIP-25 reaction. It shares the channel and the wrapper with chat, but it is an answer
#: to a message, never a message: it must not run the instruction path or wake an agent.
REACTION_KIND = 7


def npub_of(pubkey_hex: str) -> str:
    """`npub…` for a hex key, or the hex as given when it is not one.

    A human checking a trust decision compares config (`npub…`) with what the invite
    carries (hex), so the prompt shows both rather than making them convert.
    """
    text = str(pubkey_hex or "").strip()
    try:
        return sdk.PublicKey.parse(text).to_bech32()
    except Exception:  # noqa: BLE001 - a bad key is worth showing, not raising for
        return text or "(none)"


@dataclass(frozen=True)
class InviteVerdict:
    """Whether an invite joins on the inviter's word, and why.

    `why` is stored with the invite and shown to the operator, so it says what decided it
    ("in control.trusted_npubs", "a human decides") rather than only that something did.
    """

    accept: bool
    why: str


@dataclass(frozen=True)
class ChannelReaction:
    """A NIP-25 reaction read in a channel: an answer to a message, never a message.

    Deliberately not an `Inbound`: a reaction has no row, no cursor and no digest window.
    The adapter matches `target` against the approval prompt it posted and resolves that one
    gate — or drops the frame, which is why a reaction on anything else costs nothing here.
    The gateway turns this into a `reaction` frame (spec §5); `reach` exists so the daemon's
    single emission test says yes, and is never anything but immediate.
    """

    id: str
    sender: str
    #: The rumor this reaction answers: a prompt's own rumor id when it is an approval.
    target: str
    emoji: str
    conversation: str
    created_at: int = 0
    #: What the reactor may do in this room, from the policy. The adapter is the one that
    #: decides an author may resolve a gate; this only states the rule's answer.
    authority: str = "none"
    why: str = ""
    reach: str = "immediate"
    blocked_reason: Optional[str] = None

    def to_frame(self) -> dict[str, Any]:
        """The `reaction` frame of spec §5, with the author's policy answer attached.

        `from`/`emoji`/`target_rumor_id` are the spec's names; the adapter accepts those or
        the message frame's `sender`/`content`/`reply_to`, so either shape reaches a gate.
        """
        return {
            "type": "reaction",
            "plane": "concord",
            "id": self.id,
            "from": self.sender,
            "conversation": self.conversation,
            "target_rumor_id": self.target,
            "emoji": self.emoji,
            "authority": self.authority,
            "created_at": self.created_at,
            "why": self.why,
        }


class ConcordPlane:
    def __init__(self, store: Store, *, solo_max_members: int = 2,
                 projects: Optional[ProjectMap] = None,
                 control: Optional[ControlConfig] = None):
        self.store = store
        #: A channel this small needs no mention: everything in it is addressed to us.
        self.solo_max_members = max(1, int(solo_max_members))
        self._streams: dict[str, ChannelStream] = {}
        self._keys: dict[str, GroupKey] = {}
        self._members: dict[str, set[str]] = {}
        #: community id -> the invite it was learned from. A reply needs the bundle (relays,
        #: root) and nothing else from it; the plane never signs with anything it holds.
        self._invites: dict[str, Invite] = {}
        #: channel -> checkout map. Optional: with no config nothing resolves, and the
        #: plane behaves exactly as before.
        self.projects = projects
        #: Where a confirmation goes and who needs none. Optional, and the safe default:
        #: with no control config nobody is trusted, so every invite waits for a human.
        self.control = control
        #: Set once the channel list has been read at least once. Routing asks for it before
        #: a route for a channel can be trusted: an empty list is "not asked yet" until then,
        #: and "this channel is gone" afterwards.
        self.refreshed = False

    def project_for(self, *, community_id: str = "", channel_id: str = "",
                    channel_name: str = "") -> Optional[dict[str, Any]]:
        """The checkout this channel's work belongs to, as the frame field an agent reads."""
        if self.projects is None:
            return None
        project = self.projects.resolve(community_id=community_id, channel_id=channel_id,
                                        channel_name=channel_name)
        return {"name": project.name, "path": str(project.path)} if project else None

    # -- channel reads --------------------------------------------------

    def set_streams(self, invite: Invite, channels: list[c.Channel],
                    members: set[str]) -> list[ChannelStream]:
        """Publish the readable channels of one community, and who is in it."""
        readable = [ch for ch in channels if not ch.private or ch.key]
        granted = {ch.id: ch for ch in channels}
        fresh = channel_streams(invite, channels)
        for stream in fresh:
            channel = granted.get(stream.channel_id)
            self._streams[stream.address] = stream
            self._keys[stream.address] = c.channel_keys(
                invite, stream.channel_id, None if channel is None else channel.key,
                None if channel is None else channel.epoch)
        self._members[invite.community_id.hex()] = set(members)
        # From here on an empty channel list means the community has no readable channel, not
        # that nothing has been read yet — routing depends on the difference.
        self.refreshed = True
        # Remember the invite the channels came from: a reply is addressed by conversation
        # alone, and the bundle is where its relays and root live.
        self._invites[invite.community_id.hex()] = invite
        log.info("concord: %d readable channel(s) of %d watched in %r (%d member(s))",
                 len(fresh), len(readable), invite.bundle.get("name"), len(members))
        return fresh

    def stream_for(self, address: str) -> Optional[ChannelStream]:
        return self._streams.get(str(address or "").lower())

    def member_count(self, community_id: str) -> int:
        return len(self._members.get(community_id) or ())

    def channels(self) -> list[ChannelStream]:
        return list(self._streams.values())

    def channel_for_conversation(self, conversation: str) -> Optional[tuple[Invite, ChannelStream]]:
        """The invite and channel a stored `concord:<community>:<channel>` names, or None.

        A lookup, not a builder: this plane holds no signing key, so the send path asks
        where a conversation lives and builds the wrap itself with the identity it already
        has (the same split as `Nip17Plane` and the publisher).
        """
        wanted = str(conversation or "").strip().lower()
        if not wanted:
            return None
        for stream in self._streams.values():
            if stream.chat_id.lower() != wanted:
                continue
            invite = self._invites.get(stream.community_id)
            return (invite, stream) if invite is not None else None
        return None

    def handle_channel(self, relay: str, wrap: dict[str, Any], policy: Any,
                       *, me_hex: str, me_npub: str) -> Optional[Inbound | ChannelReaction]:
        """A wrap on a channel stream -> a stored Inbound, a ChannelReaction, or None.

        Delivery: a message names us, or the channel is small enough that a mention would be
        ceremony (`solo_max_members`, default: us and one other). Both cases are presented to
        the policy as a mention, so the same allowlist rules govern a channel as a DM — the
        difference is only what counts as being addressed.

        A kind-7 reaction shares that stream and wrapper and is routed here too, but it is
        never chat: it returns a `ChannelReaction` for the gateway to emit as a `reaction`
        frame, with no row, no policy delivery decision and no agent turn. The wrapper id is
        claimed before either branch, so a relay's replay of a reaction cannot answer the same
        gate twice — the same guard a message gets.
        """
        stream = self.stream_for(wrap.get("pubkey", ""))
        if stream is None:
            return None
        wrapper_id = str(wrap.get("id") or "")
        if self.store.wrapper_seen(wrapper_id, relay):
            return None
        try:
            rumor = open_wrap(self._keys[stream.address], wrap)
        except (ConcordError, ValueError, KeyError):
            return None
        kind = int(rumor.get("kind", 0))
        if kind not in (*CHAT_KINDS, REACTION_KIND):
            return None
        sender = str(rumor.get("pubkey") or "")
        if not sender or sender == me_hex:
            return None                      # our own message echoing back
        tags = rumor.get("tags") or []
        text = rumor.get("content") or ""
        mentioned = has_mention(text, tags, me_hex, me_npub)
        members = self.member_count(stream.community_id)
        mention_flag, note = decides(stream, mentioned=mentioned, members=members,
                                      solo_max=self.solo_max_members)
        if kind == REACTION_KIND:
            return self._reaction(stream, rumor, wrapper_id, policy, sender=sender,
                                  addressing=mention_flag, note=note)
        # The policy reads the words, not a leading `/queue`/`/steer`: see addressing.py.
        _, words = addressing.split(text)
        decision = policy.decide(sender=sender, kind=9, mention=mention_flag, dm=False,
                                 event_id=rumor.get("id"), text=words)
        if decision.duplicate:
            return None
        mode, content = addressing.resolve(text, decision)
        msg = Inbound(
            id=str(rumor.get("id") or wrapper_id),
            wrapper_id=wrapper_id,
            sender=sender,
            kind=kind,
            created_at=int(rumor.get("created_at") or 0),
            content=content,
            # One conversation per channel, so an agent's history is per channel and not
            # per member — the point of a project room.
            conversation=stream.chat_id,
            reply_to=next((t[1] for t in tags if t and t[0] == "e"), None),
            plane="concord",
            reach=decision.reach,
            mode=mode,
            authority=decision.authority,
            rule=decision.rule,
            why="; ".join(part for part in (decision.why, note) if part),
            blocked_reason=decision.blocked_reason,
            interval_s=decision.interval_s,
        )
        return msg if self.store.add(msg) else None

    def _reaction(self, stream: ChannelStream, rumor: dict[str, Any], wrapper_id: str,
                  policy: Any, *, sender: str, addressing: bool,
                  note: str) -> Optional[ChannelReaction]:
        """A kind-7 rumor as a frame, or None when it answers nothing.

        Three things have to survive the trip to the adapter: the target (`e` — for a prompt,
        the id the adapter tracked it under), the author, and what that author may do here.
        Authority is asked of the same rules a message from them in this room would match:
        answering a gate is an instruction, and a reaction carries no reach of its own.
        An emoji that is not a decision never gets that far — the adapter owns that set, and
        this plane must not second-guess it or filter the frame out.
        """
        tags = rumor.get("tags") or []
        target = next((t[1] for t in tags
                       if isinstance(t, list) and len(t) > 1 and t[0] == "e"), None)
        if not target:
            return None                      # a reaction with nothing to answer
        return ChannelReaction(
            id=str(rumor.get("id") or wrapper_id),
            sender=sender,
            target=str(target),
            emoji=str(rumor.get("content") or ""),
            conversation=stream.chat_id,
            created_at=int(rumor.get("created_at") or 0),
            authority=policy.authority_for(sender=sender, kind=9, mention=addressing,
                                           dm=False),
            why=note,
        )

    # -- invites --------------------------------------------------------

    def invite_verdict(self, invite: Invite) -> InviteVerdict:
        """Join this invite on the inviter's word, or hold it for a human's.

        Trust is the *inviter's* key: the bundle self-certifies the owner, but who put the
        wrap in our inbox is the sender, and that is the key `control.trusted_npubs` names.
        With no control config nobody is trusted, so every invite waits — the same rule an
        empty list states, with no second config surface to keep in step.
        """
        who = str(invite.inviter or "")
        if self.control is None:
            return InviteVerdict(False, "no control pane configured: a human decides")
        if self.control.is_trusted(who, plane="concord"):
            return InviteVerdict(True, f"inviter {who[:12]}... is in control.trusted_npubs")
        return InviteVerdict(False, f"inviter {who[:12]}... is not in control.trusted_npubs")

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
