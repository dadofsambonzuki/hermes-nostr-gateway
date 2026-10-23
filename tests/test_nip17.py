"""Plane behaviour: the rules that keep a gift wrap from becoming an agent run."""

import pytest

from hermes_nostr_gateway.planes.nip17 import Nip17Plane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

ME = "aa" * 32
HAL = "bb" * 32
STRANGER = "cc" * 32

CFG = {
    "groups": {"hal": [HAL]},
    "policies": [
        {"name": "hal-dms", "match": {"group": "hal", "dm": True}, "reach": "immediate",
         "authority": "instructions"},
        {"name": "unknown-dms", "match": {"group": "unknown", "dm": True},
         "reach": "digest(1h)", "authority": "none"},
    ],
    "defaults": {"reach": "drop", "authority": "none"},
}


class FakeCrypto:
    def __init__(self, rumors):
        self.rumors = rumors

    def our_pubkey(self):
        return ME

    def unwrap(self, wrap):
        return self.rumors.get(wrap["id"])


def wrap(wid):
    return {"id": wid, "kind": 1059}


def rumor(rid, sender, kind=14, content="hi", recipient=ME, eid=None):
    tags = [["p", recipient]] if recipient else []
    if eid:
        tags.append(["e", eid])
    return {"id": rid, "pubkey": sender, "kind": kind, "created_at": 1_700_000_000,
            "content": content, "tags": tags}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def build(store, rumors):
    return Nip17Plane(FakeCrypto(rumors), store, Policy(CFG))


def test_authorised_dm_becomes_an_immediate_message(store):
    plane = build(store, {"w1": rumor("r1", HAL)})
    msg = plane.handle("relay", wrap("w1"))
    assert msg and msg.sender == HAL
    assert (msg.reach, msg.authority) == ("immediate", "instructions")
    assert msg.conversation == HAL


def test_stranger_dm_is_delivered_without_authority(store):
    msg = build(store, {"w1": rumor("r1", STRANGER)}).handle("relay", wrap("w1"))
    assert (msg.reach, msg.authority) == ("digest", "none")


def test_invite_is_not_a_message(store):
    plane = build(store, {"w1": rumor("r1", HAL, kind=3313)})
    assert plane.handle("relay", wrap("w1")) is None


def test_unknown_inner_kind_is_dropped(store):
    plane = build(store, {"w1": rumor("r1", HAL, kind=4242)})
    assert plane.handle("relay", wrap("w1")) is None


def test_self_copy_is_ignored(store):
    plane = build(store, {"w1": rumor("r1", ME)})
    assert plane.handle("relay", wrap("w1")) is None


def test_dm_for_someone_else_is_ignored(store):
    plane = build(store, {"w1": rumor("r1", HAL, recipient=STRANGER)})
    assert plane.handle("relay", wrap("w1")) is None


def test_same_wrapper_is_processed_once(store):
    plane = build(store, {"w1": rumor("r1", HAL)})
    assert plane.handle("relay", wrap("w1")) is not None
    assert plane.handle("relay", wrap("w1")) is None


def test_same_rumor_via_a_different_wrapper_is_deduped(store):
    rumors = {"w1": rumor("r1", HAL), "w2": rumor("r1", HAL)}
    plane = build(store, rumors)
    assert plane.handle("relay-a", wrap("w1")) is not None
    assert plane.handle("relay-b", wrap("w2")) is None  # same message, second relay


def test_unreadable_wrap_is_not_fatal(store):
    plane = build(store, {})
    assert plane.handle("relay", wrap("nope")) is None


def test_reply_threading_is_preserved(store):
    plane = build(store, {"w1": rumor("r1", HAL, eid="parent")})
    msg = plane.handle("relay", wrap("w1"))
    assert msg.reply_to == "parent"
