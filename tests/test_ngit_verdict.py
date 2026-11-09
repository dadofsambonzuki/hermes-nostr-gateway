"""The verdict on the PR: one NIP-22 kind-1111 comment, and the guard that reads it.

The event shape is the one ngit 3.0.5 actually publishes (`ngit pr comment`): root
`E`/`K`/`P`, parent `e`/`k`/`p`, and no `a` tag. The merge-route tests run the real
`scripts/merge_pr.sh` with a fake `ngit` on PATH, so what is under test is the file the
operator runs, not a re-implementation of it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import cli
from hermes_nostr_gateway import ngit_verdict as verdicts
from hermes_nostr_gateway.control import as_hex
from hermes_nostr_gateway.crypto import sign_event

PR = "ab" * 32
RELAY = "wss://relay.example"
AUTHOR = sdk.Keys.generate()
REVIEWER = sdk.Keys.generate()

REPO = Path(__file__).resolve().parents[1]
MERGE = REPO / "scripts" / "merge_pr.sh"
GUARD = REPO / "scripts" / "worker_push_guard.sh"


def key(keys: sdk.Keys) -> str:
    return keys.public_key().to_hex()


# -- the event ----------------------------------------------------------------

def test_a_verdict_is_a_nip22_comment_on_the_pr_root():
    event = verdicts.comment_event(pr_id=PR, body="Verdict: approved",
                                   created_at=1700000000, author=key(AUTHOR))

    assert event["kind"] == 1111
    assert event["content"] == "Verdict: approved"
    assert event["created_at"] == 1700000000
    assert event["tags"] == [
        ["E", PR], ["K", "1618"], ["P", key(AUTHOR)],
        ["e", PR], ["k", "1618"], ["p", key(AUTHOR)],
    ]
    # A NIP-22 comment carries no `a`: a reader that filters by the repository address can
    # never see one, which is why matching is by the root `E` everywhere.
    assert not any(tag[0] == "a" for tag in event["tags"])


def test_a_comment_with_no_known_pr_author_omits_the_p_tags():
    event = verdicts.comment_event(pr_id=PR, body="Verdict: approved", created_at=1)
    assert event["tags"] == [["E", PR], ["K", "1618"], ["e", PR], ["k", "1618"]]


def test_the_published_event_is_what_the_label_pass_reads_back():
    """One event, two readers: the label pass parses the same comment the guard sees."""
    from hermes_nostr_gateway import ngit_labels

    event = verdicts.comment_event(pr_id=PR, body="Verdict: approved", created_at=1700000000,
                                   author=key(AUTHOR))
    signed = sign_event(event, REVIEWER)
    signed["relay"] = RELAY

    found = ngit_labels.newest_verdict([signed])

    assert found is not None
    assert (found.label, found.event_id, found.author) == (
        "approved", signed["id"], key(REVIEWER))
    sdk.Event.from_json(json.dumps(signed)).verify()


# -- the verdict, on the card --------------------------------------------------

def a_task_comment(comment_id: int, body: str, *, created_at: int = 1000) -> dict:
    return {"id": comment_id, "body": body, "created_at": created_at, "author": "review"}


def test_the_card_s_verdict_is_the_newest_comment_that_states_one():
    found = verdicts.card_verdict("t_1", [
        a_task_comment(1, "Reviewed the diff; gates are green."),
        a_task_comment(2, "Verdict: approved\n\nOne nit, not blocking.", created_at=1100),
        a_task_comment(3, "Follow-up: published the event as 1f5b76d9."),
    ])

    assert found is not None
    assert (found.task_id, found.comment_id, found.label, found.created_at) == (
        "t_1", 2, "approved", 1100)
    assert found.body == "Verdict: approved\n\nOne nit, not blocking."


def test_a_newer_unreadable_verdict_stops_the_search():
    """An older approval must not be published over a newer verdict nobody parsed."""
    assert verdicts.card_verdict("t_1", [
        a_task_comment(1, "Verdict: approved"),
        a_task_comment(2, "Verdict: ???", created_at=1100),
    ]) is None


def test_a_card_with_no_verdict_comment_states_nothing():
    assert verdicts.card_verdict("t_1", [a_task_comment(1, "Thinking about it.")]) is None


# -- publishing ----------------------------------------------------------------

class FakeRelays:
    """A relay set that records what it was asked to hold and serves it back."""

    def __init__(self, *, accept: bool = True, serve: bool = True):
        self.events: list[dict] = []
        self.accept = accept
        self.serve = serve
        self.publish_calls = 0

    def publish(self, event: dict, relays, secret: str | None = None):
        self.publish_calls += 1
        self.events.append(event)
        return {relay: (self.accept, None if self.accept else "blocked: not allowed")
                for relay in relays}

    def read(self, pr_id: str):
        if not self.serve:
            return []
        return [(RELAY, dict(event, relay=RELAY)) for event in self.events]


def a_publisher(relays: FakeRelays, keys: sdk.Keys = REVIEWER) -> verdicts.VerdictPublisher:
    return verdicts.VerdictPublisher([RELAY], secret=keys.secret_key().to_hex(),
                                     publish=relays.publish, read=relays.read)


def test_publishing_signs_one_event_as_the_review_identity_and_reads_it_back():
    relays = FakeRelays()

    result = a_publisher(relays)(PR, "Verdict: approved", created_at=1700000000,
                                 author=AUTHOR.public_key().to_bech32())

    assert result.ok and not result.existing
    assert result.relays == (RELAY,)
    assert result.event == relays.events[0]["id"]
    event = relays.events[0]
    assert event["pubkey"] == key(REVIEWER)
    assert event["kind"] == 1111
    assert event["content"] == "Verdict: approved"
    assert event["created_at"] == 1700000000
    assert ["E", PR] in event["tags"]
    sdk.Event.from_json(json.dumps(event)).verify()


def test_the_same_verdict_comment_signs_the_same_event():
    """Idempotency is the event: same words, same second, same id -- a retry is a duplicate."""
    first, second = FakeRelays(), FakeRelays()

    a_publisher(first)(PR, "Verdict: changes-requested", created_at=1700000000)
    a_publisher(second)(PR, "Verdict: changes-requested", created_at=1700000000)

    assert first.events[0]["id"] == second.events[0]["id"]


def test_a_verdict_already_on_the_pr_from_this_key_is_not_published_twice():
    """A reviewer who published by hand is not given a second copy by the bridge."""
    relays = FakeRelays()
    already = sign_event(verdicts.comment_event(
        pr_id=PR, body="Verdict: approved", created_at=1, author=key(AUTHOR)), REVIEWER)
    relays.events.append(already)

    result = a_publisher(relays)(PR, "Verdict: approved", created_at=1700000000)

    assert result.ok and result.existing
    assert result.event == already["id"]
    assert relays.publish_calls == 0


def test_an_identical_verdict_older_than_the_tip_does_not_cover_it():
    """A re-review approves again in the same words: the old event approved an older tip."""
    relays = FakeRelays()
    older = sign_event(verdicts.comment_event(
        pr_id=PR, body="Verdict: approved", created_at=1000, author=key(AUTHOR)), REVIEWER)
    relays.events.append(older)

    result = a_publisher(relays)(PR, "Verdict: approved", created_at=2000,
                                 covers_after=1500)

    assert result.ok and not result.existing
    assert relays.publish_calls == 1
    assert relays.events[-1]["id"] != older["id"]


def test_a_relay_that_serves_nothing_back_is_a_failure():
    """A signed event is not a published one: the thread has to show it."""
    relays = FakeRelays(serve=False)

    result = a_publisher(relays)(PR, "Verdict: approved", created_at=1700000000)

    assert not result.ok
    assert "served it back" in result.error


def test_a_relay_that_refuses_the_key_is_reported():
    relays = FakeRelays(accept=False)

    result = a_publisher(relays)(PR, "Verdict: approved", created_at=1700000000)

    assert not result.ok
    assert "blocked" in result.error


def test_publishing_with_no_relays_is_a_failure_not_a_silent_success():
    result = verdicts.VerdictPublisher([], secret=REVIEWER.secret_key().to_hex())(
        PR, "Verdict: approved", created_at=1700000000)
    assert not result.ok and "no relays" in result.error


# -- the merge guard's half ----------------------------------------------------

#: The tip the merge would apply, and an earlier one: two full ids, as `ls-remote` prints them.
TIP = "a" * 40
EARLIER = "b" * 40
#: A third key: a valid npub that is not the author and not a permitted reviewer.
STRANGER = sdk.Keys.generate()


def a_view(pr_author: sdk.Keys, *comments: dict) -> dict:
    return {"author": pr_author.public_key().to_bech32(), "comments": list(comments)}


def a_comment(keys: sdk.Keys, body: str, *, created_at: int = 1000,
              event_id: str = "") -> dict:
    return {"author": keys.public_key().to_bech32(), "body": body,
            "created_at": created_at, "id": event_id or f"{created_at:064d}"}


def an_approval(keys: sdk.Keys, tip: str = TIP, **kwargs) -> dict:
    """One `Verdict: approved` comment naming the tip it covers, as the pass publishes it."""
    return a_comment(keys, f"Verdict: approved\n\nReasoning.\n\nTip: {tip}", **kwargs)


def a_guard(view: dict, *, reviewers=(REVIEWER,), tip: str = TIP):
    """The guard, called the way `main` calls it: with the allowlist and the current tip."""
    return verdicts.check_authorship(view, reviewers=[key(who) for who in reviewers], tip=tip)


def test_a_permitted_reviewer_approving_the_current_tip_permits_the_merge():
    found = a_guard(a_view(AUTHOR, an_approval(REVIEWER)))

    assert found.ok, found.why
    assert found.label == "approved"
    assert found.signer == REVIEWER.public_key().to_bech32()
    assert found.covers == TIP and found.tip == TIP
    assert found.verdict is not None and key(REVIEWER) == as_hex(found.verdict.author)


def test_a_verdict_from_outside_the_allowlist_is_refused_and_names_the_npub():
    """The label pass honours `ngit.reviewers`; so does the thing that gates the merge."""
    found = a_guard(a_view(AUTHOR, an_approval(STRANGER)))

    assert not found.ok
    assert STRANGER.public_key().to_bech32() in found.why
    assert "not a configured reviewer" in found.why and "ngit.reviewers" in found.why


def test_no_configured_reviewer_means_no_verdict_permits_a_merge():
    found = a_guard(a_view(AUTHOR, an_approval(REVIEWER)), reviewers=())

    assert not found.ok
    assert "no reviewer is configured" in found.why


def test_an_approval_of_an_earlier_tip_is_refused():
    """The revision moved after the review; the word no longer covers the work."""
    found = a_guard(a_view(AUTHOR, an_approval(REVIEWER, tip=EARLIER)))

    assert not found.ok
    assert EARLIER[:8] in found.why and TIP[:8] in found.why
    assert "review the new tip" in found.why


def test_a_re_review_of_the_new_tip_permits_the_merge():
    """The refusal is not a dead end: a reviewer approving the tip there now is enough."""
    found = a_guard(a_view(
        AUTHOR,
        an_approval(REVIEWER, tip=EARLIER, created_at=1000),
        an_approval(REVIEWER, tip=TIP, created_at=2000,
                    event_id="c" * 64),
    ))

    assert found.ok, found.why
    assert found.covers == TIP


def test_an_approval_that_names_no_tip_is_refused():
    found = a_guard(a_view(AUTHOR, a_comment(REVIEWER, "Verdict: approved")))

    assert not found.ok
    assert "does not name the tip" in found.why


def test_a_changes_request_from_a_permitted_reviewer_is_refused():
    """A verdict is not an approval: the newest word decides, and this one says no."""
    found = a_guard(a_view(
        AUTHOR,
        an_approval(REVIEWER, created_at=1000),
        a_comment(REVIEWER, f"Verdict: changes-requested\n\nTwo fixes.\n\nTip: {TIP}",
                  created_at=2000, event_id="d" * 64),
    ))

    assert not found.ok
    assert "asks for changes" in found.why


def test_the_pr_authors_own_verdict_is_refused():
    found = a_guard(a_view(AUTHOR, an_approval(AUTHOR, tip=TIP)))

    assert not found.ok
    assert "own" in found.why and "different npub" in found.why


def test_the_same_key_is_recognised_whatever_the_spelling():
    """npub and hex are the same signature; the guard compares keys, not strings."""
    view = {"author": key(AUTHOR), "comments": [an_approval(AUTHOR, tip=TIP)]}
    assert not verdicts.check_authorship(view, reviewers=[key(REVIEWER)], tip=TIP).ok


def test_no_verdict_on_the_pr_is_refused():
    found = a_guard(a_view(AUTHOR, a_comment(REVIEWER, "Looks fine.")))

    assert not found.ok
    assert "no verdict" in found.why


def test_an_unreadable_verdict_is_refused():
    found = a_guard(a_view(AUTHOR, a_comment(REVIEWER, "Verdict: ???")))

    assert not found.ok
    assert "cannot be read" in found.why


def test_the_newest_verdict_decides_who_signed_it():
    """An old second opinion does not cover a PR the author then approved themselves."""
    found = a_guard(a_view(
        AUTHOR,
        an_approval(REVIEWER, created_at=1000),
        an_approval(AUTHOR, created_at=2000, event_id="e" * 64),
    ))

    assert not found.ok
    assert "own" in found.why


# -- the tip marker ------------------------------------------------------------

def test_an_abbreviated_tip_matches_the_full_id():
    """`Tip: 45f4e8b` is how a person writes a revision, and it is enough to identify one."""
    assert verdicts.same_commit("45f4e8b", "45f4e8b" + "c" * 33)
    assert verdicts.same_commit("45f4e8b" + "c" * 33, "45f4e8b")


def test_a_marker_too_short_to_identify_a_commit_matches_nothing():
    assert not verdicts.same_commit("45f4", "45f4" + "c" * 36)
    assert not verdicts.same_commit("", TIP)


def test_the_tip_is_read_from_the_verdict_s_own_line():
    assert verdicts.verdict_tip(f"Verdict: approved\n\nTip: {TIP.upper()}") == TIP
    assert verdicts.verdict_tip("Verdict: approved\n\ntip: 45f4e8b") == "45f4e8b"
    # Prose is not a marker: "reviewed the tip at ..." says nothing a merge can compare.
    assert verdicts.verdict_tip("Verdict: approved\n\nI read the tip at 45f4e8b.") == ""


def test_the_publisher_appends_the_tip_the_reviewer_did_not_write():
    published = verdicts.with_tip("Verdict: approved\n\nLooks right.", TIP)

    assert published == f"Verdict: approved\n\nLooks right.\n\nTip: {TIP}"
    assert verdicts.verdict_tip(published) == TIP
    # Idempotent: a second pass over an already-stamped body changes nothing.
    assert verdicts.with_tip(published, TIP) == published


def test_the_reviewer_s_own_tip_line_is_left_exactly_as_written():
    body = "Verdict: approved\n\nTip: 45f4e8b\n\nScope: this covers that revision."
    assert verdicts.with_tip(body, TIP) == body


# -- the merge route -----------------------------------------------------------

def git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                   capture_output=True, text=True, timeout=120)


def clean_env(**extra: str) -> dict[str, str]:
    env = {name: value for name, value in os.environ.items()
           if name not in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKSPACE")}
    env.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_AUTHOR_NAME": "Guard Test", "GIT_AUTHOR_EMAIL": "guard@example.invalid",
                "GIT_COMMITTER_NAME": "Guard Test",
                "GIT_COMMITTER_EMAIL": "guard@example.invalid"})
    env.update(extra)
    return env


@pytest.fixture()
def merge_route(tmp_path: Path) -> SimpleNamespace:
    """A throwaway clone carrying the real scripts, a symlink to this tree's src, and a fake
    `ngit` that answers `pr view` from a JSON file the test writes.

    The clone also carries the `config.yaml` the guard reads its reviewer allowlist from, and a
    `pr/test` ref on the origin: the tip the merge would apply comes from `ls-remote`, so the
    fixture has to have one.
    """
    env = clean_env()
    origin, repo = tmp_path / "origin.git", tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-b", "master", str(origin), env=env)
    git(tmp_path, "init", "-b", "master", str(repo), env=env)
    git(repo, "remote", "add", "origin", str(origin), env=env)
    (repo / "a.txt").write_text("one\n", encoding="utf-8")
    git(repo, "add", "a.txt", env=env)
    git(repo, "commit", "-qm", "one", env=env)
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/master", env=env)
    # The PR's branch, and the tip the approval must name.
    git(repo, "checkout", "-q", "-b", "pr/test", env=env)
    (repo / "b.txt").write_text("two\n", encoding="utf-8")
    git(repo, "add", "b.txt", env=env)
    git(repo, "commit", "-qm", "two", env=env)
    git(repo, "push", "-q", "origin", "HEAD:refs/heads/pr/test", env=env)
    tip = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, env=env, check=True,
                         capture_output=True, text=True, timeout=60).stdout.strip()
    (repo / "scripts").mkdir()
    shutil.copy2(MERGE, repo / "scripts" / "merge_pr.sh")
    shutil.copy2(GUARD, repo / "scripts" / "worker_push_guard.sh")
    (repo / "src").symlink_to(REPO / "src", target_is_directory=True)
    # The one allowlist: `ngit.reviewers`, read by the same resolver the label pass uses. The
    # `store:` names a file that does not exist, so the card check has no links to read and the
    # script does not fall back to whatever config this host holds.
    (repo / "config.yaml").write_text(
        f"store: {tmp_path / 'gateway.db'}\n"
        f"ngit:\n  reviewers: [{key(REVIEWER)}]\n", encoding="utf-8")

    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "ngit"
    fake.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *--json*) cat \"$FAKE_PR_JSON\" ;;\n"
        "  *apply*) exit 9 ;;\n"
        "  *) printf 'Status: open\\nBranch: pr/test\\n' ;;\n"
        "esac\n",
        encoding="utf-8")
    fake.chmod(0o755)
    view = tmp_path / "pr.json"
    return SimpleNamespace(repo=repo, bindir=bindir, view=view, tip=tip,
                           venv=Path(sys.executable).parent.parent)


def run_merge(made: SimpleNamespace, view: dict) -> subprocess.CompletedProcess:
    made.view.write_text(json.dumps(view), encoding="utf-8")
    return subprocess.run(
        ["bash", str(made.repo / "scripts" / "merge_pr.sh"), PR, "--check"],
        cwd=made.repo,
        env=clean_env(PATH=f"{made.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
                      VENV=str(made.venv), FAKE_PR_JSON=str(made.view)),
        capture_output=True, text=True, timeout=180,
    )


def test_the_merge_route_refuses_a_self_authored_verdict(merge_route):
    refused = run_merge(merge_route, a_view(AUTHOR, an_approval(AUTHOR, tip=merge_route.tip)))

    assert refused.returncode == 9, refused.stdout + refused.stderr
    assert "REFUSED" in refused.stdout
    assert "different npub" in refused.stdout


def test_the_merge_route_refuses_a_verdict_from_outside_the_allowlist(merge_route):
    """The script's allowlist is the config's, and the refusal says whose key it refused."""
    refused = run_merge(merge_route, a_view(AUTHOR, an_approval(STRANGER,
                                                                 tip=merge_route.tip)))

    assert refused.returncode == 9, refused.stdout + refused.stderr
    assert "REFUSED" in refused.stdout
    assert STRANGER.public_key().to_bech32() in refused.stdout
    assert "not a configured reviewer" in refused.stdout


def test_the_merge_route_refuses_an_approval_of_an_earlier_tip(merge_route):
    refused = run_merge(merge_route, a_view(AUTHOR, an_approval(REVIEWER, tip=EARLIER)))

    assert refused.returncode == 9, refused.stdout + refused.stderr
    assert "the approval covers tip" in refused.stdout
    assert merge_route.tip[:8] in refused.stdout


def test_the_merge_route_lets_a_permitted_approval_of_the_current_tip_through(merge_route):
    """Past the guard it reaches the apply step -- which the fake fails, as a marker."""
    past = run_merge(merge_route, a_view(AUTHOR, an_approval(REVIEWER, tip=merge_route.tip)))

    assert "REFUSED" not in past.stdout
    assert past.returncode == 1 and "CONFLICT" in past.stdout


def guard_env(monkeypatch, reviewers=(REVIEWER,)) -> None:
    """The check's two external reads: the config, and the allowlist resolved from it."""
    monkeypatch.setattr("hermes_nostr_gateway.cli.load_config", lambda path=None: {})
    monkeypatch.setattr(verdicts, "permitted_reviewers",
                        lambda config: tuple(key(who) for who in reviewers))


def test_the_check_exits_three_when_the_pr_cannot_be_read(monkeypatch, capsys):
    guard_env(monkeypatch)

    def unreadable(*args, **kwargs):
        raise verdicts.VerdictError("ngit pr view abababab failed: relay unreachable")

    monkeypatch.setattr(verdicts, "read_view", unreadable)
    assert verdicts.main([PR, "--tip", TIP]) == verdicts.EXIT_UNREADABLE
    assert "relay unreachable" in capsys.readouterr().err


def test_the_check_exits_three_when_the_config_cannot_be_read(monkeypatch, capsys):
    def missing(path=None):
        raise SystemExit("no config found: pass --config or create config.yaml")

    monkeypatch.setattr("hermes_nostr_gateway.cli.load_config", missing)
    assert verdicts.main([PR, "--tip", TIP]) == verdicts.EXIT_UNREADABLE
    assert "no config found" in capsys.readouterr().err


def test_the_check_exits_three_when_there_is_no_tip_to_compare_against(monkeypatch, capsys):
    """A guard that cannot run is 3, not 9: nobody is being asked for a re-review."""
    guard_env(monkeypatch)
    assert verdicts.main([PR]) == verdicts.EXIT_UNREADABLE
    assert "--tip" in capsys.readouterr().err


def test_the_check_exits_zero_for_a_permitted_approval_of_the_tip(monkeypatch, capsys):
    guard_env(monkeypatch)
    monkeypatch.setattr(verdicts, "read_view",
                        lambda pr: a_view(AUTHOR, an_approval(REVIEWER)))
    assert verdicts.main([PR, "--tip", TIP]) == 0
    out = capsys.readouterr().out
    assert "OK" in out and REVIEWER.public_key().to_bech32() in out


def test_the_check_exits_nine_for_a_self_review(monkeypatch, capsys):
    guard_env(monkeypatch)
    monkeypatch.setattr(verdicts, "read_view",
                        lambda pr: a_view(AUTHOR, an_approval(AUTHOR, tip=TIP)))
    assert verdicts.main([PR, "--tip", TIP]) == verdicts.EXIT_REFUSED
    assert "REFUSED" in capsys.readouterr().err


def test_the_check_exits_nine_for_an_unpermitted_reviewer(monkeypatch, capsys):
    guard_env(monkeypatch)
    monkeypatch.setattr(verdicts, "read_view",
                        lambda pr: a_view(AUTHOR, an_approval(STRANGER, tip=TIP)))
    assert verdicts.main([PR, "--tip", TIP]) == verdicts.EXIT_REFUSED
    err = capsys.readouterr().err
    assert STRANGER.public_key().to_bech32() in err and "ngit.reviewers" in err


def test_the_merge_guard_reads_the_allowlist_the_label_pass_reads(monkeypatch, tmp_path):
    """One allowlist, one place: the guard resolves `ngit.reviewers` through `cli.reviewer_keys`.

    That function is what `cmd_ngit_sync` puts into `BridgeConfig.reviewers` before `Sync` is
    built, so the list the label pass filters verdicts with and the list the merge route
    checks them against cannot be two different answers.
    """
    from hermes_nostr_gateway import cli, ngit

    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}),
                        encoding="utf-8")
    keyfile = tmp_path / "review.hex"
    keyfile.write_text(REVIEWER.secret_key().to_hex() + "\n", encoding="utf-8")
    config = {"identity": {"secret_file": str(identity)},
              "ngit": {"review_secret_file": str(keyfile)}}

    guard = verdicts.permitted_reviewers(config)
    labels = cli.reviewer_keys(ngit.BridgeConfig.from_config(config["ngit"]))

    assert guard == labels == (key(REVIEWER),)
    # And that resolved list is exactly what the label pass is constructed from.
    bridged = ngit.BridgeConfig(reviewers=labels)
    assert {as_hex(who) for who in bridged.reviewers} == {key(REVIEWER)}


# -- the configured review identity --------------------------------------------

def a_review_config(tmp_path: Path, *, gateway: str, review: str) -> tuple[dict, object]:
    from hermes_nostr_gateway import ngit

    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"secret_key_hex": gateway}), encoding="utf-8")
    keyfile = tmp_path / "review.hex"
    keyfile.write_text(review + "\n", encoding="utf-8")
    return ({"identity": {"secret_file": str(identity)}},
            ngit.BridgeConfig(review_secret_file=str(keyfile)))


def test_a_missing_review_key_publishes_nothing_and_says_why():
    from hermes_nostr_gateway import ngit

    secret, why = cli.review_secret_from({"identity": {"secret_file": "/nonexistent"}},
                                         ngit.BridgeConfig())

    assert secret == ""
    assert "review_secret_file" in why and "not set" in why


def test_a_distinct_review_key_is_the_identity_that_signs(tmp_path):
    gateway = sdk.Keys.generate().secret_key().to_hex()
    reviewer = sdk.Keys.generate().secret_key().to_hex()

    secret, why = cli.review_secret_from(*a_review_config(tmp_path, gateway=gateway,
                                                          review=reviewer))

    assert secret == reviewer and why == ""


def test_the_gateway_s_own_key_is_refused_as_a_review_identity(tmp_path):
    gateway = sdk.Keys.generate().secret_key().to_hex()

    secret, why = cli.review_secret_from(*a_review_config(tmp_path, gateway=gateway,
                                                          review=gateway))

    assert secret == ""
    assert "self-review" in why
