"""Plane behaviour: the rules that keep a gift wrap from becoming an agent run."""

import pytest

from hermes_nostr_gateway.planes.nip17 import Nip17Plane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

ME = "aa" * 32
PEER = "bb" * 32
STRANGER = "cc" * 32

CFG = {
    "groups": {"peers": [PEER]},
    "policies": [
        {"name": "peer-dms", "match": {"group": "peers", "dm": True}, "reach": "immediate",
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
    plane = build(store, {"w1": rumor("r1", PEER)})
    msg = plane.handle("relay", wrap("w1"))
    assert msg and msg.sender == PEER
    assert (msg.reach, msg.authority) == ("immediate", "instructions")
    assert msg.conversation == PEER


def test_stranger_dm_is_delivered_without_authority(store):
    msg = build(store, {"w1": rumor("r1", STRANGER)}).handle("relay", wrap("w1"))
    assert (msg.reach, msg.authority) == ("digest", "none")


def test_invite_is_not_a_message(store):
    plane = build(store, {"w1": rumor("r1", PEER, kind=3313)})
    assert plane.handle("relay", wrap("w1")) is None


def test_unknown_inner_kind_is_dropped(store):
    plane = build(store, {"w1": rumor("r1", PEER, kind=4242)})
    assert plane.handle("relay", wrap("w1")) is None


def test_self_copy_is_ignored(store):
    plane = build(store, {"w1": rumor("r1", ME)})
    assert plane.handle("relay", wrap("w1")) is None


def test_dm_for_someone_else_is_ignored(store):
    plane = build(store, {"w1": rumor("r1", PEER, recipient=STRANGER)})
    assert plane.handle("relay", wrap("w1")) is None


def test_same_wrapper_is_processed_once(store):
    plane = build(store, {"w1": rumor("r1", PEER)})
    assert plane.handle("relay", wrap("w1")) is not None
    assert plane.handle("relay", wrap("w1")) is None


def test_same_rumor_via_a_different_wrapper_is_deduped(store):
    rumors = {"w1": rumor("r1", PEER), "w2": rumor("r1", PEER)}
    plane = build(store, rumors)
    assert plane.handle("relay-a", wrap("w1")) is not None
    assert plane.handle("relay-b", wrap("w2")) is None  # same message, second relay


def test_unreadable_wrap_is_not_fatal(store):
    plane = build(store, {})
    assert plane.handle("relay", wrap("nope")) is None


def test_reply_threading_is_preserved(store):
    plane = build(store, {"w1": rumor("r1", PEER, eid="parent")})
    msg = plane.handle("relay", wrap("w1"))
    assert msg.reply_to == "parent"


def test_a_reaction_reaches_the_adapter_with_its_target_author_and_emoji(store):
    """A NIP-25 reaction is routed as kind 7, and the frame must carry all three fields.

    The Hermes adapter answers an approval prompt from a reaction on it, so it reads the
    target out of `reply_to`, the author out of `sender` and the answer out of `content`.
    A plane change that dropped any of them would disable the reaction path silently: the
    reaction would arrive as an unattributable message instead.
    """
    plane = build(store, {"w1": rumor("r1", PEER, kind=7, content="\U0001f44d",
                                      eid="target-rumor")})
    msg = plane.handle("relay", wrap("w1"))
    assert msg is not None
    frame = msg.to_frame()
    assert frame["type"] == "message"
    assert frame["kind"] == 7
    assert frame["reply_to"] == "target-rumor"
    assert frame["sender"] == PEER
    assert frame["content"] == "\U0001f44d"
