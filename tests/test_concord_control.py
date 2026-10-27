"""Control-plane writes: the staff key's derivation, edition hashing, and channel creation."""

from __future__ import annotations

import base64
import hashlib
import json
import os

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import crypto
from hermes_nostr_gateway.planes.concord import control
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord.derive import group_key


def make_invite(owner_pubkey: str, control_root: bytes) -> c.Invite:
    salt, root = os.urandom(32), os.urandom(32)
    community_id = hashlib.sha256(b"concord/community" + bytes.fromhex(owner_pubkey) + salt).digest()
    control_pk = group_key("concord/control-signer", control_root, community_id, 0).pk
    bundle = {
        "owner": owner_pubkey, "owner_salt": salt.hex(), "community_id": community_id.hex(),
        "community_root": root.hex(), "root_epoch": 0, "control_pk": control_pk,
        "name": "Test Community", "relays": ["wss://relay.example"], "channels": [],
    }
    return c.Invite(bundle=bundle, inviter=owner_pubkey, relay="", wrap_id="w")


# ── the staff write key ─────────────────────────────────────────────────────

def test_control_wrap_round_trips_to_the_staff_root():
    granter, member = sdk.Keys.generate(), sdk.Keys.generate()
    root = os.urandom(32)
    plain = base64.b64encode((7).to_bytes(8, "big") + root).decode()
    blob = crypto.nip44_encrypt(granter.secret_key().to_hex(),
                                member.public_key().to_hex(), plain)
    epoch, recovered = control.parse_control_wrap(
        secret_hex=member.secret_key().to_hex(),
        granter_hex=granter.public_key().to_hex(), blob=blob)
    assert epoch == 7
    assert recovered == root


def test_control_wrap_of_the_wrong_length_is_refused():
    granter, member = sdk.Keys.generate(), sdk.Keys.generate()
    blob = crypto.nip44_encrypt(granter.secret_key().to_hex(), member.public_key().to_hex(),
                                base64.b64encode(os.urandom(31)).decode())
    with pytest.raises(c.ConcordError):
        control.parse_control_wrap(secret_hex=member.secret_key().to_hex(),
                                   granter_hex=granter.public_key().to_hex(), blob=blob)


def test_control_root_must_derive_the_published_control_pk():
    owner = sdk.Keys.generate()
    root = os.urandom(32)
    invite = make_invite(owner.public_key().to_hex(), root)
    control.check_control_root(root, invite, 0)          # the right secret is accepted
    with pytest.raises(c.ConcordError):
        control.check_control_root(os.urandom(32), invite, 0)


def test_control_root_for_a_different_epoch_must_not_be_adopted():
    owner = sdk.Keys.generate()
    root = os.urandom(32)
    invite = make_invite(owner.public_key().to_hex(), root)
    control.check_control_root(root, invite, 0)
    with pytest.raises(c.ConcordError):
        control.check_control_root(root, invite, 1)      # fails closed across an epoch


# ── editions ────────────────────────────────────────────────────────────────

def test_edition_hash_is_deterministic_and_fixed_width():
    entity = os.urandom(32)
    one = control.edition_hash(entity, 1, None, '{"name":"ops","private":false}')
    two = control.edition_hash(entity, 1, None, '{"name":"ops","private":false}')
    assert one == two and len(one) == 64


def test_edition_hash_covers_every_field():
    entity = os.urandom(32)
    base = control.edition_hash(entity, 1, None, "a")
    assert control.edition_hash(entity, 2, None, "a") != base
    assert control.edition_hash(os.urandom(32), 1, None, "a") != base
    assert control.edition_hash(entity, 1, os.urandom(32), "a") != base
    assert control.edition_hash(entity, 1, None, "b") != base


def test_edition_hash_matches_the_spec_preimage():
    """Independent construction of the documented preimage, byte for byte."""
    entity = bytes(range(32))
    prev = bytes(range(32, 64))
    content = '{"name":"ops"}'
    label = b"vector-community/v1/edition"
    pre = (len(label).to_bytes(8, "big") + label + entity + (3).to_bytes(8, "big")
           + b"\x01" + prev + len(content.encode()).to_bytes(8, "big") + content.encode())
    assert control.edition_hash(entity, 3, prev, content) == hashlib.sha256(pre).hexdigest()


def test_a_channel_edition_verifies_at_the_planes_address():
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    root = os.urandom(32)
    invite = make_invite(owner.public_key().to_hex(), root)
    vac = ("ab" * 32, "1", "cd" * 32)

    built = control.build_channel(keys=me, invite=invite, control_root=root, epoch=0,
                                  name="ops", vac=vac, created_at=1_800_000_000)
    control.verify_edition(built, invite, 0)

    rumor = built["rumor"]
    tags = {tag[0]: tag[1:] for tag in rumor["tags"]}
    assert rumor["kind"] == 3308
    assert tags["vsk"] == ["2"] and tags["ev"] == ["1"] and "ep" not in tags
    assert tags["vac"] == ["ab" * 32, "1", "cd" * 32]
    assert json.loads(rumor["content"]) == {"name": "ops", "private": False}
    # The seal is the plaintext kind the Control Plane uses, carrying the rumor verbatim.
    seal = json.loads(built["read_key"].decrypt(built["wrap"]["content"]))
    assert seal["kind"] == 20014
    assert json.loads(seal["content"])["id"] == rumor["id"]


def test_a_wrap_signed_by_a_member_instead_of_staff_is_rejected():
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    root = os.urandom(32)
    invite = make_invite(owner.public_key().to_hex(), root)
    built = control.build_channel(keys=me, invite=invite, control_root=root, epoch=0, name="ops")
    # Republish the same content signed by the author's key rather than the control signer.
    built["wrap"] = crypto.sign_event({**built["wrap"], "pubkey": me.public_key().to_hex()}, me)
    with pytest.raises(c.ConcordError):
        control.verify_edition(built, invite, 0)


def test_grant_citation_takes_the_newest_grant_for_that_member():
    member = "aa" * 32
    editions = [
        {"vsk": 3, "eid": "11" * 32, "ev": 1, "content": json.dumps({"member": member})},
        {"vsk": 3, "eid": "11" * 32, "ev": 2, "content": json.dumps({"member": member,
                                                                     "role_ids": ["r"]})},
        {"vsk": 3, "eid": "22" * 32, "ev": 9, "content": json.dumps({"member": "bb" * 32})},
    ]
    eid, ev, digest = control.grant_citation(member, editions)
    assert (eid, ev) == ("11" * 32, "2")
    assert digest == control.edition_hash(bytes.fromhex("11" * 32), 2, None,
                                          editions[1]["content"])


def test_grant_citation_without_a_grant_is_an_error():
    with pytest.raises(c.ConcordError):
        control.grant_citation("aa" * 32, [])
