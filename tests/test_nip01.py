"""nip01 plane: only interesting kind-1 events, each with a stated reason."""

import time

import pytest

from hermes_nostr_gateway.planes.nip01 import FeedRule, Nip01Plane
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

ME = "aa" * 32
FRIEND = "bb" * 32
PEER = "cc" * 32
STRANGER = "dd" * 32

CFG = {
    "groups": {"friends": [FRIEND], "peers": [PEER]},
    "policies": [
        {"name": "friend-mentions", "match": {"group": "friends", "kinds": [1], "mention": True},
         "reach": "immediate", "authority": "instructions"},
        {"name": "any-mentions", "match": {"group": "any", "kinds": [1], "mention": True},
         "reach": "digest(15m)", "authority": "none"},
        {"name": "feeds", "match": {"group": "any", "kinds": [1]},
         "reach": "digest(60m)", "authority": "none"},
    ],
    "defaults": {"reach": "drop", "authority": "none"},
}


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "t.db")


def plane(store, rules=()):
    return Nip01Plane(store, Policy(CFG), ME, rules)


def note(pubkey, content="hello", p=None, hashtags=()):
    tags = [["p", p]] if p else []
    tags += [["t", h] for h in hashtags]
    return {"id": f"id-{pubkey[:4]}-{content[:6]}-{p or ''}", "pubkey": pubkey, "kind": 1,
            "created_at": int(time.time()), "content": content, "tags": tags}


def test_friend_mention_is_immediate(store):
    msg = plane(store).handle("relay", note(FRIEND, "hi", p=ME))
    assert msg and msg.why.endswith("mention")
    assert (msg.reach, msg.authority) == ("immediate", "instructions")
    assert msg.plane == "nip01"


def test_stranger_mention_digests_without_authority(store):
    msg = plane(store).handle("relay", note(STRANGER, "hi", p=ME))
    assert (msg.reach, msg.authority) == ("digest", "none")


def test_plain_note_is_ignored_without_a_rule(store):
    assert plane(store).handle("relay", note(STRANGER, "just posting")) is None


def test_author_rule_matches(store):
    p = plane(store, [FeedRule(name="peer-feed", authors={PEER})])
    msg = p.handle("relay", note(PEER, "gm"))
    assert msg and msg.why.endswith("feed:peer-feed")


def test_keyword_rule_matches_case_insensitively(store):
    p = plane(store, [FeedRule(name="nostr", authors=set(), keywords=("nostr",))])
    assert p.handle("relay", note(STRANGER, "I love NOSTR today")) is not None


def test_hashtag_rule_matches(store):
    p = plane(store, [FeedRule(name="tag", authors=set(), hashtags=("bitcoin",))])
    assert p.handle("relay", note(STRANGER, "post", hashtags=["Bitcoin"])) is not None


def test_own_notes_are_ignored(store):
    assert plane(store).handle("relay", note(ME, "my own post", p=ME)) is None


def test_non_note_kinds_are_ignored(store):
    ev = note(STRANGER, "reaction", p=ME)
    ev["kind"] = 7
    assert plane(store).handle("relay", ev) is None


def test_same_event_is_delivered_once(store):
    p = plane(store)
    ev = note(FRIEND, "hi", p=ME)
    assert p.handle("relay-a", ev) is not None
    assert p.handle("relay-b", ev) is None
