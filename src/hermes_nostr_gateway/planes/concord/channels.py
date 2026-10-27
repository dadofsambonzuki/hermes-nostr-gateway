"""Concord channel reads: which streams to watch, who is in them, and when a message counts.

A Channel is addressed by a stream pubkey derived from its key (CORD-02 §4), so a channel wrap
is a kind-1059 event whose *author* is that address — not a DM addressed to us. Every wrap on the
stream is opened with the channel's own key, so the plane needs the channel list and its grants
before it can read anything.

Delivery rule. A message reaches the agent when it names us, or when the channel is just us and
the other member — the `#general`-with-one-other-person case where a mention is pure ceremony.
Membership is the community Guestbook (CORD-02 §5), not the channel's access list: a member who
cannot read a private channel is still a member, and counting them keeps the rule conservative —
more members means a mention is required, never fewer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Optional

from ... import crypto
from . import read as c
from .derive import GroupKey

#: `nostr:npub1…`, `nostr:nprofile1…`, or a bare hex pubkey in the text (NIP-21/NIP-27 style).
_MENTION = re.compile(r"(?:nostr:)?(npub1[0-9ac-hj-np-z]{20,}|nprofile1[0-9ac-hj-np-z]{20,}"
                      r"|[0-9a-fA-F]{64})")

#: A channel conversation is named `<prefix><community>:<channel>`, for reads and for replies.
CONVERSATION_PREFIX = "concord:"


@dataclass(frozen=True)
class ChannelStream:
    """One readable channel: where its wraps live and what the agent calls the conversation."""

    community_id: str
    channel_id: str
    name: str
    private: bool
    #: The epoch the channel's stream key was derived with (the community root's when the
    #: grant names none); the `epoch` tag carries the same number.
    epoch: int
    address: str            # the stream pubkey — the author of the channel's wraps
    chat_id: str            # `concord:<community>:<channel>`, the conversation an agent sees
    key: Optional[str] = None   # a private channel's grant; public channels derive from the root

    @property
    def label(self) -> str:
        return f"#{self.name}"


def chat_id(community_id: str, channel_id: str) -> str:
    return f"{CONVERSATION_PREFIX}{community_id}:{channel_id}"


def is_conversation(value: str) -> bool:
    """Does this send target name a channel rather than a peer?

    The prefix is the whole test, on purpose: a channel this process no longer holds must
    fail as a channel delivery error, never fall through to the DM path and come back as
    "bad pubkey", which sends the caller looking in the wrong place.
    """
    return str(value or "").strip().lower().startswith(CONVERSATION_PREFIX)


def streams(invite: c.Invite, channels: Iterable[c.Channel]) -> list[ChannelStream]:
    """The readable channels of one community, each with the address its wraps carry."""
    out: list[ChannelStream] = []
    for channel in channels:
        if channel.private and not channel.key:
            continue                       # a private channel whose key was not granted
        gk = c.channel_keys(invite, channel.id, channel.key, channel.epoch)
        epoch = int(invite.root_epoch if channel.epoch is None else channel.epoch)
        out.append(ChannelStream(community_id=invite.community_id.hex(),
                                 channel_id=channel.id, name=channel.name,
                                 private=bool(channel.private), epoch=epoch,
                                 address=gk.pk, key=channel.key,
                                 chat_id=chat_id(invite.community_id.hex(),
                                                channel.id)))
    return out


def stream_key(invite: c.Invite, stream: ChannelStream,
               key: Optional[str] = None) -> GroupKey:
    """The key a channel's wraps are opened with (public: derived from the community root)."""
    return c.channel_keys(invite, stream.channel_id, key, stream.epoch)


def has_mention(content: str, tags: Iterable[list[str]], me_hex: str,
                me_npub: str) -> bool:
    """Does this message name us — by `p` tag, or as a NIP-21 mention in the text?"""
    for tag in tags or []:
        if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "p" and tag[1] == me_hex:
            return True
    for candidate in _MENTION.findall(content or ""):
        if candidate == me_npub or candidate.lower() == me_hex:
            return True
        try:
            if crypto.normalize_pubkey(candidate) == me_hex:
                return True
        except Exception:  # noqa: BLE001 - a malformed mention is not our mention
            continue
    return False


def mention_required(members: int, *, solo_max: int = 2) -> bool:
    """A channel with `solo_max` members or fewer needs no ceremony: everything is addressed.

    Unknown membership (`members` <= 0) requires a mention: silence is the safe failure.
    """
    return members <= 0 or members > solo_max


def decides(stream: ChannelStream, *, mentioned: bool, members: int,
            solo_max: int = 2) -> tuple[bool, str]:
    """`(mention_flag_for_policy, why)` — what the plane tells the policy about this message."""
    if mentioned:
        return True, f"mention in {stream.label}"
    if not mention_required(members, solo_max=solo_max):
        return True, f"{stream.label} has {members} member(s): no mention needed"
    return False, f"{stream.label} has {members} member(s): mention required"
