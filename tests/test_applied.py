"""`applied` asked of the checkout: ancestry, a replayed patch, and a squash.

`ngit pr apply` replays a proposal's patches as new commits, so the tip a PR event names is
never an ancestor of the branch it landed on. These tests build that situation for real --
a scratch clone, a branch, a cherry-pick -- and check what `applied.Checkout` says about it.
"""

from __future__ import annotations

from pathlib import Path

from git_sandbox import Sandbox, advance_origin, clean_env, commit, git, make_clone
from hermes_nostr_gateway.applied import Checkout, Evidence, parse_patch_ids


def proposal(made: Sandbox, *names: str, branch: str = "pr/x") -> str:
    """A branch off `master` with one commit per name; returns its tip."""
    git(made.repo, "checkout", "-q", "-b", branch)
    tip = ""
    for name in names:
        tip = commit(made.repo, name)
    git(made.repo, "checkout", "-q", "master")
    return tip


def replay(made: Sandbox, *shas: str) -> str:
    """Apply these commits on `master` the way `ngit pr apply` does: as new commits.

    A fixed committer date keeps the new commit off the original's id, which is the whole
    point: the tree is the same and the commit is not.
    """
    for sha in shas:
        git(made.repo, "cherry-pick", sha,
            env=clean_env(GIT_AUTHOR_DATE="2021-02-03T04:05:06Z",
                          GIT_COMMITTER_DATE="2021-02-03T04:05:06Z"))
    return made.head


def test_a_fast_forward_tip_is_applied_by_ancestry(tmp_path: Path):
    """The forge's answer: exact, cheap, and still checked first."""
    made = make_clone(tmp_path)
    check = Checkout(made.repo, "master")

    evidence = check.probe(made.head)

    assert evidence.applied and evidence.commit == made.head
    assert "ancestor of" in evidence.why


def test_a_replayed_commit_is_applied_even_though_it_is_not_an_ancestor(tmp_path: Path):
    """`ngit pr apply` re-signs the commit: same patch, different id, no ancestry."""
    made = make_clone(tmp_path)
    tip = proposal(made, "p.txt")
    landed = replay(made, tip)
    assert landed != tip
    assert git(made.repo, "merge-base", "--is-ancestor", tip, "master", check=False).returncode

    evidence = Checkout(made.repo, "master").probe(tip)

    assert evidence.applied, evidence.why
    assert evidence.commit == landed
    assert "its patch is already on" in evidence.why and "patch-id" in evidence.why


def test_a_proposal_nothing_carries_is_not_applied_and_says_what_it_compared(tmp_path: Path):
    made = make_clone(tmp_path)
    tip = proposal(made, "p.txt")

    evidence = Checkout(made.repo, "master").probe(tip)

    assert not evidence.applied
    assert "no commit on" in evidence.why and "carries its patch" in evidence.why
    assert "1 compared against" in evidence.why


def test_every_commit_of_a_proposal_has_to_be_on_the_branch(tmp_path: Path):
    """One patch in and one patch missing is not applied -- half a proposal is not landed."""
    made = make_clone(tmp_path)
    tip = proposal(made, "p1.txt", "p2.txt")
    first = git(made.repo, "rev-parse", "pr/x~1").stdout.strip()
    replay(made, first)

    half = Checkout(made.repo, "master").probe(tip)
    assert not half.applied, half.why

    replay(made, tip)
    whole = Checkout(made.repo, "master").probe(tip)
    assert whole.applied, whole.why
    assert "all 2 of its commits" in whole.why


def test_a_squashed_apply_counts_as_applied(tmp_path: Path):
    """A maintainer who squashes still landed every patch the proposal added."""
    made = make_clone(tmp_path)
    tip = proposal(made, "p1.txt", "p2.txt")
    git(made.repo, "merge", "-q", "--squash", "pr/x")
    git(made.repo, "commit", "-qm", "the proposal, as one commit")

    evidence = Checkout(made.repo, "master").probe(tip)

    assert evidence.applied, evidence.why
    assert "as one patch" in evidence.why


def test_a_tip_the_checkout_does_not_have_is_not_a_match(tmp_path: Path):
    made = make_clone(tmp_path)
    check = Checkout(made.repo, "master")

    evidence = check.probe("f" * 40)

    assert not evidence.applied
    assert "not in this checkout" in evidence.why


def test_a_checkout_that_cannot_answer_says_so_instead_of_guessing(tmp_path: Path):
    where = tmp_path / "not-a-repo"
    where.mkdir()

    evidence = Checkout(where, "master").probe("a" * 40)

    assert not evidence.applied
    assert "could not be checked" in evidence.why


def test_the_base_is_read_from_origin_when_the_clone_has_one(tmp_path: Path):
    """The daemon's checkout is not pulled after every merge: `origin/master` is what landed."""
    made = make_clone(tmp_path)
    tip = proposal(made, "p.txt")
    landed = replay(made, tip)
    git(made.repo, "push", "-q", "origin", "HEAD:refs/heads/master")
    git(made.repo, "reset", "-q", "--hard", "HEAD~1")  # the local branch lags behind
    advanced = advance_origin(made, tmp_path, "later.txt")

    check = Checkout(made.repo, "master")
    assert check.head() == advanced

    evidence = check.probe(tip)
    assert evidence.applied and evidence.commit == landed
    assert "origin/master" in evidence.why


def test_head_is_empty_when_the_base_cannot_be_read(tmp_path: Path):
    where = tmp_path / "not-a-repo"
    where.mkdir()
    assert Checkout(where, "master").head() == ""


def test_patch_ids_are_read_as_pairs():
    text = ("25b99bf608e2131d3a10980ac4d88ccb4aa0e161 71423e9a9fecb86249497b621a22abaa84764c0a"
            "\n" + "0" * 40 + " " + "0" * 40 + "\nnot a line\n")
    assert parse_patch_ids(text) == [
        ("25b99bf608e2131d3a10980ac4d88ccb4aa0e161",
         "71423e9a9fecb86249497b621a22abaa84764c0a"),
        ("0" * 40, "0" * 40),
    ]


def test_evidence_round_trips_for_a_caller_that_reports_it():
    assert Evidence(True, "why", "abc").as_dict() == {
        "applied": True, "why": "why", "commit": "abc"}
