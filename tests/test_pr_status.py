"""The status records a merge publishes, and the two readers that decide their tags.

`merge_from_event` wants a root `e` and a commit tag; `RelayPrs.events` filters by the
repository's `a` coordinate. A status that satisfies one and not the other parses in a unit
test and is invisible in production -- which is exactly the failure that made a hand-published
close (and a merge with no record at all) a board ticket. These tests drive the builders
through both readers, with a fetcher that behaves like a relay: it returns only what carries
the address it was asked for.
"""

from __future__ import annotations

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import crypto, ngit, pr_status

RELAYS = ("wss://relay.example", "wss://other.example")
OWNER = "c" * 64
AUTHOR = "d" * 64
ADDRESS = f"30617:{OWNER}:example-repo"
PR_ID = "a" * 64
COMMIT = "b" * 40
ROOT = "e" * 40
SECRET = sdk.Keys.generate().secret_key().to_hex()
#: The fixtures are dated 1_800_000_000; the reader's future guard needs a clock beside them.
NOW = 1_800_000_100


def applied() -> dict:
    """The applied record, signed: `RelayPrs.events` skips an event with no id."""
    return pr_status.signed(pr_status.applied_event(
        pr_id=PR_ID, address=ADDRESS, merge_commit=COMMIT, root_commit=ROOT,
        authors=(AUTHOR,), relay_hint=RELAYS[0], created_at=1_800_000_000), SECRET)


def closed() -> dict:
    return pr_status.signed(pr_status.closed_event(
        pr_id=PR_ID, address=ADDRESS, reason="Closed as applied: content is in master.",
        authors=(AUTHOR,), relay_hint=RELAYS[0], created_at=1_800_000_000), SECRET)


def feed_reader(events, seen):
    """A fetcher that filters the way a relay does: by the `a` tag it was asked for."""
    def fetch(**kwargs):
        seen.update(kwargs)
        wanted = str(kwargs.get("a_tag") or "")
        return [event for event in events
                if any(isinstance(tag, (list, tuple)) and len(tag) >= 2
                       and tag[0] == "a" and tag[1] == wanted
                       for tag in event.get("tags") or ())]
    return fetch


# -- the applied record ------------------------------------------------------

def test_the_applied_record_parses_to_this_pr_root_and_this_commit():
    merge = ngit.merge_from_event(applied())

    assert merge is not None
    assert merge.pr_id == PR_ID
    # The full 40 characters: a short id would parse and resolve to nothing.
    assert merge.commit == COMMIT and len(merge.commit) == 40


def test_the_applied_record_carries_every_tag_the_readers_look_for():
    tags = applied()["tags"]

    assert ["e", PR_ID, RELAYS[0], "root"] in tags        # the root merge_from_event reads
    assert ["a", ADDRESS, RELAYS[0]] in tags              # what a repository feed filters on
    assert ["merge-commit-id", COMMIT] in tags            # ngit's spelling
    assert ["merge-commit", COMMIT] in tags               # NIP-34's spelling
    assert ["r", COMMIT] in tags and ["r", ROOT] in tags
    assert ["p", AUTHOR] in tags and ["p", OWNER] in tags
    assert ["alt", pr_status.APPLIED_ALT] in tags


def test_the_bridge_feed_sees_the_applied_record_and_drops_one_without_the_address():
    # The second event is the shape a hand-publish produced: right kind, right root, no `a`.
    bare = {"kind": ngit.KIND_APPLIED, "id": "f" * 64, "pubkey": OWNER, "created_at": 5,
            "content": "", "tags": [["e", PR_ID, "", "root"], ["merge-commit-id", COMMIT]]}
    seen: dict = {}

    feed = ngit.RelayPrs(RELAYS, ADDRESS, now=lambda: NOW,
                         fetcher=feed_reader([applied(), bare], seen)).events(0)

    assert seen["a_tag"] == ADDRESS
    assert seen["kinds"] == [ngit.KIND_PR, ngit.KIND_PR_REVISION,
                             ngit.KIND_APPLIED, ngit.KIND_CLOSED]
    assert [merge.pr_id for merge in feed.merges] == [PR_ID]
    assert [merge.commit for merge in feed.merges] == [COMMIT]


# -- the close ---------------------------------------------------------------

def test_a_close_the_tooling_publishes_is_visible_to_the_repository_feed():
    seen: dict = {}

    feed = ngit.RelayPrs(RELAYS, ADDRESS, now=lambda: NOW,
                         fetcher=feed_reader([closed()], seen)).events(0)

    assert seen["a_tag"] == ADDRESS
    assert [stopped.pr_id for stopped in feed.closed] == [PR_ID]


def test_a_close_without_the_repository_address_is_invisible():
    """The tag gap as it bit: `e`/`alt`/`p` only, and the bridge never sees the close."""
    hand_published = {"kind": ngit.KIND_CLOSED, "id": "9" * 64, "pubkey": OWNER,
                      "created_at": 5, "content": "Closed.", "tags": [
                          ["e", PR_ID, "", "root"], ["alt", "PR closed"],
                          ["p", AUTHOR]]}

    feed = ngit.RelayPrs(RELAYS, ADDRESS, now=lambda: NOW,
                         fetcher=feed_reader([hand_published], {})).events(0)

    assert feed.closed == []


# -- the guards on the builders ---------------------------------------------

def test_a_short_commit_is_refused_because_it_names_no_commit():
    with pytest.raises(ngit.NgitError, match="40-character"):
        pr_status.applied_event(pr_id=PR_ID, address=ADDRESS, merge_commit=COMMIT[:8])


def test_a_prefix_is_not_an_event_id():
    with pytest.raises(ngit.NgitError, match="64-character"):
        pr_status.applied_event(pr_id=PR_ID[:8], address=ADDRESS, merge_commit=COMMIT)


def test_an_address_that_is_not_a_repository_coordinate_is_refused():
    with pytest.raises(ngit.NgitError, match="30617"):
        pr_status.closed_event(pr_id=PR_ID, address="example-repo", reason="x")


# -- publishing --------------------------------------------------------------

def test_publish_status_signs_once_and_reports_each_relay():
    seen: dict = {}

    def fake_publish(event, relays, secret=None):
        seen["event"], seen["relays"] = event, list(relays)
        return {relays[0]: (True, None), relays[1]: (False, "auth-required")}

    published = pr_status.publish_status(applied(), RELAYS, SECRET, publish=fake_publish)

    assert published.ok
    assert published.event_id == seen["event"]["id"]
    assert len(published.event_id) == 64
    assert crypto.verify_event(seen["event"])  # a reader can verify what was sent
    assert seen["relays"] == list(RELAYS)
    assert published.delivered == (RELAYS[0],)
    assert published.refused == ((RELAYS[1], "auth-required"),)
    assert "1/2 relays" in published.line()


def test_a_publish_that_reached_no_relay_says_which_and_why():
    def refused(event, relays, secret=None):
        return {relay: (False, "blocked: not on the whitelist") for relay in relays}

    published = pr_status.publish_status(applied(), RELAYS, SECRET, publish=refused)

    assert not published.ok
    assert RELAYS[0] in published.line() and "whitelist" in published.line()


def test_a_publish_with_no_relays_is_an_error_not_a_silent_success():
    published = pr_status.publish_status(applied(), (), SECRET)

    assert not published.ok
    assert "no relays" in published.line()


# -- what the CLI publishes --------------------------------------------------

def test_the_hng_applied_command_publishes_the_record_for_a_merged_pr(tmp_path, monkeypatch,
                                                                     capsys):
    """The command `merge_pr.sh` runs: it resolves the checkout, the proposal and the key."""
    from hermes_nostr_gateway import cli

    key_file = tmp_path / "key.txt"
    key_file.write_text(SECRET + "\n", encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("ngit:\n  assignee: default\n", encoding="utf-8")

    nevent = sdk.Nip19Event(sdk.EventId.parse(PR_ID), kind=sdk.Kind(ngit.KIND_PR)).to_bech32()
    monkeypatch.setattr(ngit.Ngit, "repository", lambda self: ngit.RepoRef(
        address=ADDRESS, relays=RELAYS))
    monkeypatch.setattr(pr_status, "pr_detail", lambda repo, ref: {
        "id": nevent, "author": ngit.npub_of(AUTHOR), "status": "open"})
    monkeypatch.setattr(pr_status, "root_commit", lambda repo: ROOT)
    captured: dict = {}
    monkeypatch.setattr(pr_status, "publish_status",
                        lambda event, relays, secret, publish=None: _record(
                            captured, event, relays, secret))

    code = cli.main(["--config", str(config), "ngit", "applied", PR_ID[:8],
                     "--commit", COMMIT, "--repo", str(tmp_path),
                     "--nsec-file", str(key_file)])

    out = capsys.readouterr()
    assert code == 0, out.err
    assert f"applied-record {PR_ID[:8]} -> {COMMIT}" in out.out
    assert "2/2 relays" in out.out
    event = captured["event"]
    assert event["kind"] == ngit.KIND_APPLIED
    assert ["a", ADDRESS, RELAYS[0]] in event["tags"]
    assert ngit.merge_from_event(event).commit == COMMIT


def _record(captured: dict, event: dict, relays, secret: str) -> pr_status.Published:
    """Stand-in for `publish_status` that keeps the event a test wants to inspect."""
    captured["event"], captured["relays"], captured["secret"] = event, tuple(relays), secret
    return pr_status.Published(event_id="1" * 64, relays=tuple(relays),
                               delivered=tuple(relays))
