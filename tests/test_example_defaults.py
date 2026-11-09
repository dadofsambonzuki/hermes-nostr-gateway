"""The shipped example must refuse strangers on every plane, not just in `defaults`.

The gap this closes: setting `defaults: reach: drop` was not enough, because the example's own
rules matched a stranger first. `unknown-dms` digested their kind-14/1059 DMs and
`anyone-mentions` — `group: any` matches a stranger as readily as a colleague — digested their
kind-1 mentions. The engine was right and the shipped rules were wrong, so this test parses
`config.example.yaml` the way the daemon does and asks the policy what happens to somebody no
group names. Revert the example's `strangers` rule, move it below a wildcard rule, or restore
the digest fallback in `Policy`, and these fail.
"""

import time
from pathlib import Path

import pytest
import yaml

from hermes_nostr_gateway.cli import normalise_groups
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Inbound, Store, drops_ttl_seconds

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.yaml"

#: A key no group in the example names. The engine compares strings, so nothing here is
#: special-cased for a well-formed hex pubkey.
STRANGER = "ab" * 32


def daemon_policy(config: dict) -> Policy:
    """The construction `hng serve`, `hng policy-test` and the CLI all use.

    Copied deliberately rather than reached for through `cli.build`, which needs a secret
    file and a store: this stays the same mapping, so the test cannot pass against a policy
    the daemon would never build.
    """
    return Policy({
        "groups": normalise_groups(config),
        "policies": config.get("policies"),
        "defaults": config.get("defaults"),
        "rate_limit": config.get("rate_limit"),
        "bots": config.get("bots"),
    })


@pytest.fixture(scope="module")
def example() -> dict:
    return yaml.safe_load(EXAMPLE.read_text())


@pytest.fixture(scope="module")
def policy(example) -> Policy:
    return daemon_policy(example)


@pytest.mark.parametrize(
    ("kind", "kwargs"),
    [
        (14, {"dm": True}),      # a NIP-17 DM
        (1059, {"dm": True}),    # the gift wrap it arrives in
        (1, {"mention": True}),  # a public post naming us
        (4, {"dm": True}),       # a legacy NIP-04 DM
        (1, {}),                 # a kind-1 nobody asked for
        (9, {}),                 # a Marmot application message
        (7, {}),                 # a reaction
    ],
    ids=["kind14-dm", "kind1059-dm", "kind1-mention", "kind4-dm", "kind1-plain", "kind9", "kind7"],
)
def test_a_stranger_is_dropped(policy, kind, kwargs):
    decision = policy.decide(sender=STRANGER, kind=kind, **kwargs)
    assert decision.reach == "drop", decision.why
    assert decision.deliverable is False
    assert decision.authority == "none"


def test_a_dropped_stranger_is_still_recorded_as_not_allowlisted(example, policy, tmp_path):
    """`invisible` is about the agent's attention: the refusal itself is stored evidence.

    A `reach: drop` carries no `blocked_reason`, and the store's own CASE reads that as
    `not_allowlisted` — the reason `hng drops` shows, for `drops.ttl_hours`. So this writes
    the refused row the way the daemon does and reads it back through the op that lists it.
    """
    decision = policy.decide(sender=STRANGER, kind=14, dm=True)
    assert decision.reach == "drop"
    assert decision.blocked_reason is None

    with Store(tmp_path / "drops.db") as store:
        assert store.add(Inbound(
            id="ab" * 32, wrapper_id="cd" * 32, sender=STRANGER, kind=14,
            created_at=int(time.time()), content="let me in", conversation=STRANGER,
            reply_to=None, plane="nip17", reach=decision.reach,
            authority=decision.authority, rule=decision.rule, why=decision.why,
            blocked_reason=decision.blocked_reason,
        ))
        listed = store.drops(ttl_s=drops_ttl_seconds(example["drops"]))
    assert listed["total"] == 1
    assert listed["live"] == 1
    assert listed["drops"][0]["reason"] == "not_allowlisted"


def test_a_rule_with_no_reach_refuses_rather_than_digests():
    """The per-rule fallback in `Policy`, not just the example: an omission is not a timer.

    Reverting `policy.py`'s no-`reach` fallback to `digest(15m)` leaves every other test in
    this file green, because the shipped example states a `reach` on every rule.
    """
    decision = Policy({"policies": [{"match": {}}]}).decide(
        sender=STRANGER, kind=14, dm=True)
    assert decision.reach == "drop", decision.why
    assert decision.deliverable is False
    assert decision.authority == "none"


def test_the_example_keeps_drops_for_a_week(example):
    assert drops_ttl_seconds(example["drops"]) == 168 * 3600


def test_a_named_correspondent_still_gets_through(example, policy):
    """The fix is a refusal for strangers, not a mute: `group: any` still serves members."""
    peer = normalise_groups(example)["peer"][0]
    dm = policy.decide(sender=peer, kind=14, dm=True)
    assert (dm.reach, dm.authority, dm.rule) == ("immediate", "instructions", "peer-dms")
    mention = policy.decide(sender=peer, kind=1, mention=True)
    assert (mention.reach, mention.interval_s) == ("digest", 900)


def test_the_example_states_drop_itself(example, policy):
    assert example["defaults"]["reach"] == "drop"
    assert policy.default.reach == "drop"


def test_the_built_in_default_is_drop_when_no_block_is_written():
    """A config that never wrote `defaults:` must not digest either."""
    assert Policy({}).default.reach == "drop"


def test_every_shipped_rule_states_its_reach_and_authority(example):
    """No rule may lean on a built-in: an omission is how a rule becomes a digest timer."""
    for rule in example["policies"]:
        assert "reach" in rule, rule
        assert "authority" in rule, rule


def test_the_stranger_refusal_precedes_every_wildcard_rule(example):
    """`group: any` matches strangers, so the refusal has to stand above it."""
    rules = example["policies"]
    first = rules[0]
    assert first["match"].get("group") == "unknown", "the first rule must be the stranger refusal"
    assert first["reach"] == "drop"
    earlier = [i for i, rule in enumerate(rules) if rule["match"].get("group") == "any"]
    assert earlier and min(earlier) > 0
