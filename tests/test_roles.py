"""The role registry: which identities a comment can name, and what naming one does."""

from __future__ import annotations

from hermes_nostr_gateway import roles
from hermes_nostr_gateway.roles import Role

GATEWAY_HEX = "11" * 32
REVIEW_HEX = "22" * 32
DEPS_HEX = "33" * 32


def npub(hex_key: str) -> str:
    return roles._npub(hex_key)


def test_the_roles_are_the_keys_config_already_names():
    """One registry, derived: the gateway's own key, the reviewer's, the deps signer's."""
    cfg = {
        "ngit": {"review_assignee": "review", "review_secret_file": "/etc/review.json"},
        "deps": {"signer": npub(DEPS_HEX)},
    }

    built = roles.roles_from_config(cfg, self_npub=GATEWAY_HEX, reviewers=(REVIEW_HEX,))

    assert [role.name for role in built] == [roles.GATEWAY, roles.REVIEW, roles.DEPS]
    gateway, review, deps = built
    assert gateway.key == GATEWAY_HEX
    assert review.key == REVIEW_HEX and review.profile == "review"
    assert review.action == roles.REVIEW_ACTION
    assert review.secret_file == "/etc/review.json"
    # The Deps Bot has no profile: its answer is a run, and the role says which.
    assert deps.key == DEPS_HEX and not deps.profile and "hng deps run" in deps.how


def test_a_role_named_twice_is_one_role():
    """The reviewer key is also the gateway key in a self-reviewing deployment: one entry."""
    cfg = {"ngit": {"review_secret_file": "/etc/review.json"}}

    built = roles.roles_from_config(cfg, self_npub=REVIEW_HEX, reviewers=(REVIEW_HEX,))

    assert [role.name for role in built] == [roles.GATEWAY]


def test_a_key_that_is_not_a_key_is_skipped_not_guessed():
    cfg = {"ngit": {"review_assignee": "review"}, "deps": {"signer": "not-a-key"}}

    built = roles.roles_from_config(cfg, self_npub=GATEWAY_HEX, reviewers=("also-bad",))

    assert [role.name for role in built] == [roles.GATEWAY]


def test_a_comment_is_addressed_by_tag_or_by_the_words():
    """A dropped `p` tag still leaves the `nostr:npub...` reference; both count."""
    built = roles.roles_from_config({}, self_npub=GATEWAY_HEX, reviewers=(REVIEW_HEX,))
    review = built[1]

    tagged = {"tags": [["p", REVIEW_HEX]], "content": "look at this"}
    words = {"tags": [], "content": f"nostr:{npub(REVIEW_HEX)} - re-review pls."}
    neither = {"tags": [], "content": "just a thought"}

    assert roles.addressed(tagged, built) == review
    assert roles.addressed(words, built) == review
    assert roles.addressed(neither, built) is None


def test_the_gateway_answers_a_comment_that_names_two_of_ours():
    """Order decides, and the general profile is the safe default: gateway, then bots."""
    built = roles.roles_from_config(
        {"ngit": {"review_secret_file": "/etc/review.json"}},
        self_npub=GATEWAY_HEX, reviewers=(REVIEW_HEX,))

    both = {"tags": [["p", REVIEW_HEX], ["p", GATEWAY_HEX]], "content": "both of you"}

    assert roles.addressed(both, built) == built[0]
    assert [role.name for role in roles.named(both, built)] == [roles.GATEWAY, roles.REVIEW]


def test_a_role_is_described_with_what_addressing_it_does():
    built = roles.roles_from_config(
        {"ngit": {"review_assignee": "review", "review_secret_file": "/etc/review.json"},
         "deps": {"signer": npub(DEPS_HEX)}},
        self_npub=GATEWAY_HEX, reviewers=(REVIEW_HEX,))

    lines = roles.describe(built)

    assert len(lines) == 3
    assert lines[1].startswith("review") and "-> review" in lines[1]
    assert lines[2].startswith("deps")


def test_a_role_without_a_key_is_a_name_only():
    """Hand-built roles are allowed (a plane may know one); matching needs the key."""
    role = Role(name="review", key="", npub="")

    assert roles.addressed({"tags": [["p", REVIEW_HEX]], "content": ""}, (role,)) is None
