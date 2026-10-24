from __future__ import annotations

import json

import nostr_sdk as sdk

from hermes_nostr_gateway import crypto
from hermes_nostr_gateway.crypto import SdkCrypto
from hermes_nostr_gateway.planes import nip04


def keys_pair() -> tuple[SdkCrypto, SdkCrypto]:
    one = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    two = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    return one, two


def test_npub_round_trip_and_normalise():
    keys = sdk.Keys.generate()
    hex_key = keys.public_key().to_hex()
    assert crypto.npub_decode(crypto.npub_encode(hex_key)) == hex_key
    assert crypto.normalize_pubkey(crypto.npub_encode(hex_key)) == hex_key
    assert crypto.normalize_pubkey(hex_key) == hex_key


def test_sign_and_verify_rejects_tamper():
    keys = sdk.Keys.generate()
    event = crypto.sign_event({"kind": 1, "content": "hello",
                               "tags": [["p", keys.public_key().to_hex()]]}, keys)
    assert crypto.verify_event(event)
    event["content"] = "tampered"
    assert not crypto.verify_event(event)



def test_native_backend_wraps_and_unwraps():
    alice, bob = keys_pair()
    rumor = alice.build_rumor("hello bob", bob.our_pubkey())
    wrap = alice.wrap(rumor, bob.our_pubkey())
    assert wrap["kind"] == 1059
    assert [t[1] for t in wrap["tags"] if t[0] == "p"] == [bob.our_pubkey()]
    opened = bob.unwrap(wrap)
    assert opened is not None
    assert opened["id"] == rumor["id"]
    assert opened["pubkey"] == alice.our_pubkey()
    assert opened["content"] == "hello bob"


def test_native_backend_reply_tag():
    alice, bob = keys_pair()
    rumor = alice.build_rumor("reply", bob.our_pubkey(), reply_to="ab" * 32)
    opened = bob.unwrap(alice.wrap(rumor, bob.our_pubkey()))
    assert opened is not None
    assert ["e", "ab" * 32] in opened["tags"]


def test_native_backend_unwrap_returns_none_on_tamper():
    alice, bob = keys_pair()
    wrap = alice.wrap(alice.build_rumor("hello", bob.our_pubkey()), bob.our_pubkey())
    wrap["content"] = "AAAA"
    assert bob.unwrap(wrap) is None


def test_nip04_round_trip():
    alice, bob = keys_pair()
    event = nip04.build_event(alice.keys, bob.our_pubkey(), "legacy")
    sender, text = nip04.open_event(bob.keys, event)
    assert sender == alice.our_pubkey()
    assert text == "legacy"
    assert json.loads(json.dumps(event))["kind"] == 4
