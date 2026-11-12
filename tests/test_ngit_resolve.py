"""What an off-box merge landed, read from the commits a status names.

The pass publishes an issue's resolved record when a maintainer merged somewhere else; this is
the half that turns "the status names commit <sha>" into "these cards". Every case here runs
against a real repository, because the question is what git says, not what this module assumes
git says.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from git_sandbox import git, make_clone
from hermes_nostr_gateway import ngit_resolve

CARD_A = "t_aa11bb22"
CARD_B = "t_cc33dd44"


def _commit(repo: Path, name: str, message: str, *trailers: str) -> str:
    """One commit with `Card:` trailers, the way the commit-msg hook writes them."""
    (repo / name).write_text(name + "\n", encoding="utf-8")
    git(repo, "add", name)
    body = [message, ""]
    body += [f"Card: {value}" for value in trailers]
    git(repo, "commit", "-qm", "\n".join(body))
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _merge(repo: Path, *cards: str) -> str:
    """A true merge of a branch whose two commits carry the cards. Returns the merge commit."""
    git(repo, "checkout", "-qb", "work")
    _commit(repo, "one.txt", "the first half", cards[0])
    if len(cards) > 1:
        _commit(repo, "two.txt", "the second half", cards[1])
    git(repo, "checkout", "-q", "master")
    git(repo, "merge", "--no-ff", "-q", "-m", "Merge the proposal", "work")
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _replay(repo: Path, *cards: str) -> tuple[str, list[str]]:
    """A replay: the same content as new commits on `master`, no merge commit, tip last."""
    made: list[str] = []
    for number, card in enumerate(cards):
        made.append(_commit(repo, f"replay{number}.txt", f"the half {number}", card))
    return made[-1], made


def test_a_true_merge_is_read_through_its_second_parent(tmp_path):
    """`<merge>^1..<merge>^2` is exactly the branch the merge brought in."""
    made = make_clone(tmp_path)
    merge = _merge(made.repo, CARD_A, CARD_B)

    read = ngit_resolve.read(made.repo, commit=merge)

    assert read.ok and read.error == ""
    assert read.parents == 2
    assert read.commit == merge
    assert sorted(read.cards) == [CARD_A, CARD_B]
    assert read.fetched is False


def test_a_replay_is_read_through_the_commits_the_status_names(tmp_path):
    """`ngit pr apply` leaves no merge commit, so the listed commits are the set."""
    made = make_clone(tmp_path)
    tip, commits = _replay(made.repo, CARD_A, CARD_B)

    read = ngit_resolve.read(made.repo, commit=tip, listed=commits)

    assert read.ok and read.error == ""
    assert read.parents == 1
    assert sorted(read.cards) == [CARD_A, CARD_B]


def test_a_squashed_merge_naming_one_commit_reads_that_commit(tmp_path):
    """One commit, its own trailer: the shape a squash button leaves."""
    made = make_clone(tmp_path)
    tip = _commit(made.repo, "squashed.txt", "the whole proposal", CARD_A)

    read = ngit_resolve.read(made.repo, commit=tip)

    assert read.ok and read.cards == (CARD_A,)


def test_operator_and_a_trailer_that_names_nothing_are_not_cards(tmp_path):
    """`Card: operator` is not a card, and neither is a typo: it is reported, not resolved."""
    made = make_clone(tmp_path)
    tip = _commit(made.repo, "odd.txt", "operator work", "operator", "bob", CARD_A)

    read = ngit_resolve.read(made.repo, commit=tip)

    assert read.ok and read.cards == (CARD_A,)
    assert any("'bob'" in note for note in read.notes)
    assert not any("operator" in note for note in read.notes)


def test_no_trailer_is_no_cards_and_not_an_error(tmp_path):
    """Most commits answer no issue; a merge of them resolves nothing and says nothing."""
    made = make_clone(tmp_path)
    tip = _commit(made.repo, "plain.txt", "a change with no card")

    read = ngit_resolve.read(made.repo, commit=tip)

    assert read.ok and read.cards == () and read.notes == ()


def test_a_status_that_names_no_commit_is_refused(tmp_path):
    """No commit, no cards: the record a merge would answer has nothing to point at."""
    made = make_clone(tmp_path)

    read = ngit_resolve.read(made.repo, commit="")

    assert not read.ok
    assert "names no commit" in read.error


def test_an_absent_commit_is_an_error_line_not_a_guess(tmp_path):
    """A commit this checkout does not hold cannot be read, and is never invented."""
    made = make_clone(tmp_path)

    read = ngit_resolve.read(made.repo, commit="f" * 40, fetcher=None)

    assert not read.ok
    assert "is not in the checkout" in read.error
    assert read.cards == ()


def test_a_fetch_is_tried_once_and_its_failure_is_named(tmp_path):
    """The ordinary case is a merge this clone has not fetched; a failed fetch says why."""
    made = make_clone(tmp_path)
    asked: list[Path] = []

    def broken(repo, *, timeout=0):
        asked.append(Path(repo))
        return "fatal: 'origin' does not appear to be a git repository"

    read = ngit_resolve.read(made.repo, commit="e" * 40, fetcher=broken)

    assert asked == [made.repo]
    assert not read.ok
    assert "git fetch origin` failed (fatal: 'origin' does not appear" in read.error


def test_a_merge_that_reaches_the_checkout_by_fetch_is_read(tmp_path):
    """A merge pushed to the remote is fetched, then read: the record is not lost to a fetch."""
    made = make_clone(tmp_path)
    merge = _merge(made.repo, CARD_A)
    other = tmp_path / "elsewhere"
    git(made.repo, "clone", "-q", str(made.origin), str(other))
    git(other, "checkout", "-q", "-b", "work", "origin/master")
    _commit(other, "x.txt", "another half", CARD_B)
    git(other, "checkout", "-q", "master")
    git(other, "merge", "--no-ff", "-q", "-m", "Merge the other", "work")
    remote_merge = git(other, "rev-parse", "HEAD").stdout.strip()
    git(other, "push", "-q", "origin", "HEAD:refs/heads/master")
    git(made.repo, "reset", "-q", "--hard", merge)     # the live clone has not merged it

    read = ngit_resolve.read(made.repo, commit=remote_merge)

    assert read.ok, read.error
    assert read.fetched is True
    assert read.cards == (CARD_B,)


def test_a_listed_commit_the_checkout_lacks_is_noted_not_fatal(tmp_path):
    """A replay's list can be partly absent; what is present is read, and what is not is said."""
    made = make_clone(tmp_path)
    tip, commits = _replay(made.repo, CARD_A)

    read = ngit_resolve.read(made.repo, commit=tip, listed=[*commits, "a" * 40])

    assert read.ok and read.cards == (CARD_A,)
    assert any("aaaaaaaaaaaa is not in the checkout" in note for note in read.notes)


def test_cards_are_deduped_in_the_order_git_printed_them():
    """Two commits that name the same card are one card, once."""
    assert ngit_resolve.cards_from([CARD_A, "operator", CARD_B, CARD_A]) == (CARD_A, CARD_B)
    assert ngit_resolve.cards_from(["nonsense", "t_short", ""]) == ()


@pytest.mark.parametrize("value", ["t_0123abcd", "t_ffffffff"])
def test_a_card_id_is_eight_hex_digits(value):
    assert ngit_resolve.CARD.match(value)


@pytest.mark.parametrize("value", ["t_0123abc", "t_0123abcde", "card", "T_0123ABCD"])
def test_a_near_miss_is_not_a_card_id(value):
    assert not ngit_resolve.CARD.match(value)
