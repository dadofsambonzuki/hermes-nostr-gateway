"""A real Concord invite, minted by Armada, parsed by this reader.

Every other Concord test builds its own bundle with the same derivations the reader
then checks, so a shared mistake is invisible: producer and consumer agree because they
are the same implementation. This fixture is different. It was produced by Armada's
own code (`mintCommunity`, `sealDirectInvite`, `wrapDirectInvite` in its Concord lib),
from a throwaway key, for a throwaway community whose relays are unroutable by design.
Nothing here touches a relay, and the keys protect nothing.

What it pins:

- `verify_bundle` accepts a bundle another client produced, including the community id
  it derived from owner + salt and the `control_pk` it derived from its control root.
- our channel-key derivation lands on the same stream address Armada computed for
  `#general` - the one number that has to agree for two clients to read the same room.
- a real NIP-59 gift wrap of a kind-3313 invite opens with the recipient's key and the
  inner rumor carries the bundle.

Regenerate with `mint-vector.ts` in Armada's tree; do not hand-edit the fixture, or it
stops being evidence of anything but itself.
"""

from __future__ import annotations

import json
from pathlib import Path

import nostr_sdk as sdk

from hermes_nostr_gateway import crypto
from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c

FIXTURE = Path(__file__).parent / "fixtures" / "concord-armada-invite.json"


def vector() -> dict:
    return json.loads(FIXTURE.read_text())


def test_a_bundle_minted_by_another_client_verifies():
    bundle = vector()["bundle"]
    c.verify_bundle(bundle)          # raises on failure; the point is that it does not

    invite = c.Invite(bundle=bundle, inviter="", relay="", wrap_id="")
    assert invite.community_id.hex() == bundle["community_id"]
    assert invite.root_epoch == bundle["root_epoch"]


def test_our_channel_key_derivation_agrees_with_theirs():
    v = vector()
    bundle = v["bundle"]
    channel = bundle["channels"][0]
    invite = c.Invite(bundle=bundle, inviter="", relay="", wrap_id="")

    ours = c.channel_keys(invite, channel["id"], channel.get("key") or None,
                          channel.get("epoch"))
    assert ours.pk == v["channel_pk"], (
        "the stream address must be the one Armada derived, or the two clients are "
        "reading different rooms"
    )


def test_a_real_gift_wrapped_invite_opens_and_carries_the_bundle():
    v = vector()
    keys = sdk.Keys.parse(v["recipient_secret"])

    rumor = crypto.unwrap_gift(keys, v["invite_wrap"])

    assert int(rumor["kind"]) == 3313
    assert json.loads(rumor["content"]) == v["bundle"]


def test_the_channels_of_the_fixture_are_readable_by_the_plane():
    """The plane's own view: one readable channel, addressed where the fixture says."""
    v = vector()
    bundle = v["bundle"]
    invite = c.Invite(bundle=bundle, inviter="", relay="", wrap_id="")
    channel = c.Channel(id=bundle["channels"][0]["id"], name=bundle["channels"][0]["name"],
                        private=False, key=None, epoch=None)

    streams = ch.streams(invite, [channel])

    assert len(streams) == 1
    assert streams[0].address == v["channel_pk"]
    assert streams[0].chat_id == ch.chat_id(bundle["community_id"], channel.id)
