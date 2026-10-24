import pytest
from hermes_nostr_gateway.policy import Policy, parse_interval

FAM = "npub1family"
HAL = "npub1hal"
STRANGER = "npub1stranger"

CONFIG = {
    "groups": {"family": [FAM], "hal": [HAL]},
    "policies": [
        {"name": "family-mentions", "match": {"group": "family", "kinds": [1], "mention": True},
         "reach": "immediate", "authority": "instructions"},
        {"name": "any-mentions", "match": {"group": "any", "kinds": [1], "mention": True},
         "reach": "digest(15m)", "authority": "none"},
        {"name": "hal-feed", "match": {"group": "hal", "kinds": [1]},
         "reach": "digest(60m)", "authority": "instructions"},
        {"name": "unknown-dms", "match": {"group": "unknown", "kinds": [1059], "dm": True},
         "reach": "digest(1h)", "authority": "none"},
    ],
    "defaults": {"reach": "digest(15m)", "authority": "none"},
}


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def policy(clock):
    return Policy(CONFIG, clock=clock)


def test_family_mention_is_immediate_with_instructions(policy):
    d = policy.decide(sender=FAM, kind=1, mention=True)
    assert (d.reach, d.authority, d.rule) == ("immediate", "instructions", "family-mentions")
    assert d.interval_s is None


def test_stranger_mention_digests_without_authority(policy):
    d = policy.decide(sender=STRANGER, kind=1, mention=True)
    assert (d.reach, d.authority) == ("digest", "none")
    assert d.interval_s == 900
    assert "any-mentions" in d.why


def test_named_group_feed_digests_with_authority(policy):
    d = policy.decide(sender=HAL, kind=1, mention=False)
    assert (d.reach, d.interval_s, d.authority) == ("digest", 3600, "instructions")


def test_unknown_sender_dm_digests_without_authority(policy):
    d = policy.decide(sender=STRANGER, kind=1059, dm=True)
    assert (d.reach, d.authority, d.rule) == ("digest", "none", "unknown-dms")


def test_unmatched_falls_to_defaults(policy):
    d = policy.decide(sender=STRANGER, kind=7)
    assert d.rule == "defaults"
    assert (d.reach, d.authority) == ("digest", "none")


def test_first_match_wins_so_specific_rules_must_come_first():
    reversed_cfg = dict(CONFIG)
    reversed_cfg["policies"] = list(reversed(CONFIG["policies"]))
    d = Policy(reversed_cfg).decide(sender=FAM, kind=1, mention=True)
    assert d.rule == "unknown-dms" or d.rule != "family-mentions"


def test_group_of_is_unknown_when_not_listed(policy):
    assert policy.group_of(STRANGER) == "unknown"
    assert policy.group_of(FAM) == "family"


def test_quota_exceeded_drops_rather_than_floods(clock):
    cfg = {"groups": {"loud": ["npub1loud"]},
           "policies": [{"name": "loud", "match": {"group": "loud"}, "reach": "immediate",
                         "authority": "instructions", "limit": 2}],
           "defaults": {"reach": "drop"}}
    p = Policy(cfg, clock=clock)
    assert p.decide(sender="npub1loud", kind=1).reach == "immediate"
    assert p.decide(sender="npub1loud", kind=1).reach == "immediate"
    third = p.decide(sender="npub1loud", kind=1)
    assert third.reach == "drop"
    assert third.blocked_reason == "rate_limited"
    assert "quota" in third.why
    clock.t += 3601  # window rolls
    assert p.decide(sender="npub1loud", kind=1).reach == "immediate"


def test_per_sender_bucket_degrades_to_blocked(clock):
    cfg = {"groups": {"loud": ["npub1loud"]},
           "policies": [{"name": "loud", "match": {"group": "loud"}, "reach": "immediate"}],
           "defaults": {"reach": "immediate"},
           "rate_limit": {"burst": 10, "per_hour": 30}}
    p = Policy(cfg, clock=clock)
    decisions = [p.decide(sender="npub1loud", kind=1, event_id=f"e{i}") for i in range(11)]
    assert [d.reach for d in decisions[:10]] == ["immediate"] * 10
    assert decisions[10].reach == "drop"
    assert decisions[10].blocked_reason == "rate_limited"
    # The bucket refills continuously: a full hour later the same sender is welcome.
    clock.t += 3600
    assert p.decide(sender="npub1loud", kind=1, event_id="later").reach == "immediate"


def test_bucket_is_per_sender_and_duplicates_do_not_burn_tokens(clock):
    cfg = {"policies": [], "defaults": {"reach": "immediate"},
           "rate_limit": {"burst": 2, "per_hour": 30}}
    p = Policy(cfg, clock=clock)
    # One event seen three times (three relays) is one delivery, not three tokens.
    for _ in range(3):
        assert p.decide(sender=STRANGER, kind=1, event_id="evt").reach == "immediate"
    assert p.decide(sender=STRANGER, kind=1, event_id="e2").reach == "immediate"
    assert p.decide(sender=STRANGER, kind=1, event_id="e3").reach == "drop"
    # A different sender has their own bucket.
    assert p.decide(sender=FAM, kind=1, event_id="f1").reach == "immediate"


def test_duplicate_event_is_delivered_once_and_upgrades_to_best_rule(policy):
    first = policy.decide(sender=STRANGER, kind=1, mention=True, event_id="evt1")
    assert first.reach == "digest" and not first.duplicate
    again = policy.decide(sender=STRANGER, kind=1, mention=True, event_id="evt1")
    assert again.duplicate
    # a family rule scoring the same event later upgrades the decision
    upgraded = policy.decide(sender=FAM, kind=1, mention=True, event_id="evt1")
    assert upgraded.duplicate and upgraded.reach == "immediate"


def test_digest_interval_arithmetic(clock):
    p = Policy({"defaults": {"reach": "digest(15m)"}}, clock=clock)
    assert not p.digest_due(900, last_sent=clock.t)
    clock.t += 901
    assert p.digest_due(900, last_sent=clock.t - 901)
    assert p.digest_due(900, last_sent=None)


@pytest.mark.parametrize("raw,seconds", [("15m", 900), ("1h", 3600), ("90s", 90), ("45", 45), (120, 120)])
def test_parse_interval(raw, seconds):
    assert parse_interval(raw) == seconds


def test_bad_interval_rejected():
    with pytest.raises(ValueError):
        parse_interval("soon")
