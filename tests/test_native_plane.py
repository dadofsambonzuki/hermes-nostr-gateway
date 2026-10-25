from __future__ import annotations

import nostr_sdk as sdk

from hermes_nostr_gateway.crypto import SdkCrypto
from hermes_nostr_gateway.planes.nip17 import Nip17Plane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store


def test_native_wrap_flows_through_the_plane(tmp_path):
    sender = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    receiver = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    cfg = {
        "groups": {"sender": [sender.our_pubkey()]},
        "policies": [{"name": "sender-dm", "match": {"group": "sender", "dm": True},
                      "reach": "immediate", "authority": "instructions"}],
        "defaults": {"reach": "drop", "authority": "none"},
    }
    store = Store(tmp_path / "state.db")
    plane = Nip17Plane(receiver, store, Policy(cfg))

    rumor = sender.build_rumor("hello through the plane", receiver.our_pubkey())
    wrap = sender.wrap(rumor, receiver.our_pubkey())
    msg = plane.handle("wss://relay.example", wrap)

    assert msg is not None
    assert msg.id == rumor["id"]
    assert msg.sender == sender.our_pubkey()
    assert (msg.reach, msg.authority) == ("immediate", "instructions")
    assert store.pending()

    # Same rumor, different wrapper: the store dedupes on the rumor id.
    second = sender.wrap(rumor, receiver.our_pubkey())
    assert plane.handle("wss://other.example", second) is None
