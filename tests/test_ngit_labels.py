"""The PR label: three words, one namespace, published on change and retracted on change.

The shapes here are the measured ones (see `ngit_labels`): a kind-1985 with `L=#t`, a `l`
value in that namespace, and both the repository's `a` and the PR's `e`. Placeholders only.
The verdict side is a NIP-22 kind-1111 comment on the PR root, which is where a review
publishes it and what the pass reads.
"""

from __future__ import annotations

import pytest

from hermes_nostr_gateway import ngit_labels as labels

PR = "ab" * 32
ADDRESS = f"30617:{'ee' * 32}:example-repo"
RELAY = "wss://relay.example"
OTHER = "wss://other.example"


# -- the vocabulary ------------------------------------------------------------

def test_the_vocabulary_is_three_words_in_one_namespace():
    assert labels.LABELS == ("review-required", "changes-requested", "approved")
    assert labels.NAMESPACE == "#t"
    assert not hasattr(labels, "IN_REVIEW")
    assert not hasattr(labels, "STALE")


def test_a_label_event_names_the_pr_and_its_repository():
    event = labels.label_event(pr_id=PR, address=ADDRESS, label=labels.APPROVED,
                               created_at=1700000000)

    assert event["kind"] == 1985
    assert event["created_at"] == 1700000000
    assert ["L", "#t"] in event["tags"]
    # The value carries the namespace: a reader filters on the pair, not the value alone.
    assert ["l", "approved", "#t"] in event["tags"]
    assert ["a", ADDRESS] in event["tags"]
    assert ["e", PR] in event["tags"]
    assert ["alt", "review label: approved"] in event["tags"]
    # The content says why, for a client that reads the event without the CLI.
    assert event["content"] == labels.CONTENT[labels.APPROVED]


def test_a_retraction_names_the_label_event_it_removes():
    event = labels.retraction_event("cd" * 32, created_at=1700000001)

    assert event["kind"] == 5
    assert event["tags"] == [["e", "cd" * 32], ["k", "1985"]]
    assert event["created_at"] == 1700000001


# -- the state -----------------------------------------------------------------

def verdict(label: str, event_id: str = "v" * 64, created_at: int = 1000,
            relay: str = RELAY, tip: str = "") -> labels.VerdictAnchor:
    return labels.VerdictAnchor(label=label, event_id=event_id, created_at=created_at,
                                relay=relay, tip=tip)


def test_no_verdict_and_no_review_card_is_no_label():
    """Not `review-required`: there is nothing on the board saying a review is coming."""
    assert labels.review_labels() == ()
    assert labels.review_labels(tip="aa") == ()


def test_a_review_card_with_no_verdict_yet_needs_a_review():
    assert labels.review_labels(review_card=True) == (labels.REVIEW_REQUIRED,)


def test_a_labelled_pr_with_no_readable_verdict_is_left_alone():
    """The one state a failed read can produce must not withdraw a verdict it did not see."""
    assert labels.review_labels(review_card=True, labelled=True) is None


def test_a_verdict_is_the_whole_label():
    for word in (labels.APPROVED, labels.CHANGES_REQUESTED):
        assert labels.review_labels(verdict=verdict(word), tip="aa") == (word,)


def test_a_verdict_replaces_the_previous_one_never_adds_to_it():
    assert labels.review_labels(verdict=verdict(labels.APPROVED), tip="aa",
                                labelled=True) == (labels.APPROVED,)


def test_a_revision_after_a_verdict_needs_review_again():
    assert labels.review_labels(verdict=verdict(labels.APPROVED, tip="old"),
                                tip="new") == (labels.REVIEW_REQUIRED,)
    assert labels.review_labels(verdict=verdict(labels.CHANGES_REQUESTED, tip="old"),
                                tip="new") == (labels.REVIEW_REQUIRED,)


def test_a_verdict_with_no_known_tip_is_not_made_stale_by_one():
    assert labels.review_labels(verdict=verdict(labels.APPROVED, tip=""), tip="aa") == (
        labels.APPROVED,)


def test_a_verdict_older_than_the_tip_event_does_not_cover_it():
    """First sight of both, and the tip event is newer: the reviewer read the earlier one."""
    assert labels.review_labels(verdict=verdict(labels.APPROVED, created_at=1000),
                                tip="new", tip_at=2000) == (labels.REVIEW_REQUIRED,)


def test_a_republished_tip_that_is_the_same_commit_keeps_the_verdict():
    """A re-push with a later timestamp is not a revision: the commit is the same one."""
    assert labels.review_labels(verdict=verdict(labels.APPROVED, created_at=1000, tip="beef"),
                                tip="beef", tip_at=2000) == (labels.APPROVED,)


def test_a_merge_leaves_the_label_alone():
    assert labels.review_labels(verdict=verdict(labels.APPROVED), merged=True) is None
    assert labels.review_labels(merged=True) is None


# -- the verdict, as reviewers publish it on the PR root ----------------------

def comment(body: str, comment_id: str = "c" * 64, created_at: int = 1000,
            relay: str = RELAY, kind: int = 1111) -> dict:
    return {"id": comment_id, "kind": kind, "content": body, "created_at": created_at,
            "relay": relay, "tags": []}


@pytest.mark.parametrize("body, expected", [
    ("## Verdict: approve", labels.APPROVED),
    ("Verdict: approved", labels.APPROVED),
    ("**Verdict:** approved on substance (merge already happened)", labels.APPROVED),
    ("## Verdict: merge - approved at cdd60c2, with one follow-up", labels.APPROVED),
    ("VERDICT: lgtm", labels.APPROVED),
    ("## Verdict: changes needed at a9c6139", labels.CHANGES_REQUESTED),
    ("## Verdict: changes needed (3 small items) - the mechanism is right",
     labels.CHANGES_REQUESTED),
    ("Verdict: changes-requested", labels.CHANGES_REQUESTED),
    ("Verdict: request changes on the second hunk", labels.CHANGES_REQUESTED),
])
def test_a_verdict_line_is_read_in_the_spellings_reviewers_use(body, expected):
    found = labels.newest_verdict([comment(body)])
    assert found is not None and found.label == expected and found.readable
    # The comment's own words travel with it: the card that answers the verdict quotes the
    # findings rather than pointing a worker at a relay it may not read.
    assert found.body == body


@pytest.mark.parametrize("body", [
    "",
    "Reviewed the diff and ran the gates. All green.",
    "This is not approved until the second hunk is fixed.",  # prose, not a verdict line
])
def test_a_comment_without_a_verdict_line_states_no_verdict(body):
    assert labels.newest_verdict([comment(body)]) is None


def test_a_verdict_we_cannot_read_stops_the_search_and_is_marked_unreadable():
    found = labels.newest_verdict([comment("Verdict: I have some thoughts")])
    assert found is not None and not found.readable and found.label == ""


def test_the_newest_verdict_wins_and_a_later_merge_line_does_not_erase_it():
    events = [comment("## Verdict: changes needed", "1" * 64, 1000),
              comment("fixed the two findings", "2" * 64, 1100),
              comment("## Verdict: approve", "3" * 64, 1200),
              comment("PR abababab merged as dededede.", "4" * 64, 1300)]
    found = labels.newest_verdict(events)
    assert found is not None and found.label == labels.APPROVED
    assert found.event_id == "3" * 64


def test_a_newer_verdict_we_cannot_read_does_not_fall_back_to_an_older_one():
    """Publishing a stale approval over a verdict nobody parsed is the wrong direction."""
    events = [comment("## Verdict: approve", "1" * 64, 1000),
              comment("## Verdict: I have some thoughts", "2" * 64, 2000)]
    found = labels.newest_verdict(events)
    assert found is not None and not found.readable


def test_the_event_id_tells_two_identical_verdicts_apart():
    """A re-review approves in the same words; the event id is what says it is new."""
    first = labels.newest_verdict([comment("## Verdict: approved", "7" * 64, 1000)])
    second = labels.newest_verdict([comment("## Verdict: approved", "7" * 64, 1000),
                                    comment("## Verdict: approved", "9" * 64, 1000)])
    assert first is not None and second is not None
    assert first.event_id == "7" * 64
    assert second.event_id == "9" * 64


def test_only_kind_1111_is_read_as_a_verdict():
    assert labels.newest_verdict([comment("Verdict: approved", kind=1622)]) is None


def test_the_relay_that_served_the_verdict_is_carried_with_it():
    found = labels.newest_verdict([comment("Verdict: approved", relay=OTHER)])
    assert found is not None and found.relay == OTHER


# -- the anchor ----------------------------------------------------------------

def test_a_new_verdict_anchors_to_the_tip_current_when_it_was_published():
    found = labels.Verdict(label=labels.APPROVED, event_id="a" * 64, created_at=2000)
    anchor = labels.anchor_verdict({}, found, tip="beef", tip_at=1500)
    assert anchor is not None and anchor.tip == "beef"


def test_a_verdict_older_than_the_tip_does_not_claim_it():
    """The reviewer read an earlier tip; the current one is a revision it never saw."""
    found = labels.Verdict(label=labels.APPROVED, event_id="a" * 64, created_at=1000)
    anchor = labels.anchor_verdict({}, found, tip="beef", tip_at=2000)
    assert anchor is not None and anchor.tip == ""


def test_the_same_verdict_event_keeps_the_tip_it_was_anchored_to():
    stored = {"verdict": labels.APPROVED, "verdict_event": "a" * 64, "verdict_at": 1000,
              "verdict_relay": RELAY, "verdict_tip": "old"}
    found = labels.Verdict(label=labels.APPROVED, event_id="a" * 64, created_at=1000)
    anchor = labels.anchor_verdict(stored, found, tip="new", tip_at=2000)
    assert anchor is not None and anchor.tip == "old"


def test_a_re_review_in_the_same_words_re_anchors_to_the_new_tip():
    """The old bug: an identical second verdict kept the first tip and read stale for ever."""
    stored = {"verdict": labels.APPROVED, "verdict_event": "a" * 64, "verdict_at": 1000,
              "verdict_relay": RELAY, "verdict_tip": "old"}
    found = labels.Verdict(label=labels.APPROVED, event_id="b" * 64, created_at=2000)
    anchor = labels.anchor_verdict(stored, found, tip="new", tip_at=2000)
    assert anchor is not None and anchor.tip == "new"


def test_an_unreadable_verdict_anchors_nothing():
    found = labels.Verdict(label="", event_id="a" * 64, created_at=1000, readable=False)
    assert labels.anchor_verdict({}, found, tip="beef", tip_at=500) is None
    assert labels.anchor_verdict({}, None, tip="beef", tip_at=500) is None


def test_anchor_detail_round_trips_the_fields_the_next_pass_reads():
    anchor = labels.VerdictAnchor(label=labels.APPROVED, event_id="a" * 64, created_at=1000,
                                  relay=RELAY, tip="beef")
    detail = labels.anchor_detail(anchor)
    assert set(detail) == set(labels.ANCHOR_KEYS)
    assert labels.anchor_verdict(detail, labels.Verdict(
        label=labels.APPROVED, event_id="a" * 64, created_at=1000), tip="", tip_at=0) == anchor


# -- the labels a relay holds ---------------------------------------------------

def label_event(value: str, event_id: str = "l" * 64, *, namespace: str = "#t",
                kind: int = 1985) -> dict:
    return {"id": event_id, "kind": kind, "created_at": 1000,
            "tags": [["L", namespace], ["l", value, namespace], ["e", PR]]}


def test_a_live_label_is_read_from_the_t_namespace():
    found = labels.live_label(label_event("in-review", "1" * 64))
    assert found is not None and found.value == "in-review" and found.event_id == "1" * 64


def test_a_label_in_another_namespace_is_not_ours():
    assert labels.live_label(label_event("approved", namespace="#other")) is None
    assert labels.live_label(label_event("approved", kind=1984)) is None


def test_every_live_label_outside_the_desired_set_is_unmanaged():
    held = [labels.Label("in-review", "1" * 64), labels.Label("approved", "2" * 64),
            labels.Label("stale", "3" * 64)]
    assert [(label.value, label.event_id) for label in
            labels.unmanaged(held, (labels.APPROVED,))] == [("in-review", "1" * 64),
                                                           ("stale", "3" * 64)]


# -- publishing ----------------------------------------------------------------

class Recorder:
    """A publish function: per relay, accepted or the reason it was not."""

    def __init__(self, accept=("wss://relay.example",), reason="blocked: nope"):
        self.accept = set(accept)
        self.reason = reason
        self.events: list[dict] = []
        self.relays_seen: list[tuple[str, ...]] = []

    def __call__(self, event, relays, secret=None):
        self.events.append(event)
        self.relays_seen.append(tuple(relays))
        return {relay: (relay in self.accept, None if relay in self.accept else self.reason)
                for relay in relays}


def signer(event):
    """Stand in for the identity: a stable id per tag list, so a retraction can name it."""
    signed = dict(event)
    signed["id"] = (str(abs(hash(tuple(tuple(tag) for tag in event["tags"])))) * 64)[:64]
    return signed


def test_publishing_a_label_signs_one_event_per_label_and_retracts_what_it_replaces():
    publish = Recorder()
    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer, now=lambda: 1700000000)

    result = publisher(PR, ADDRESS, (labels.APPROVED,), ("12" * 32,))

    assert result.ok and result.retracted and result.relays == 1
    assert len(result.events) == 1
    kinds = [event["kind"] for event in publish.events]
    assert kinds == [1985, 5]
    assert publish.events[1]["tags"] == [["e", "12" * 32], ["k", "1985"]]
    # Every event went to the read set: a label a reader cannot fetch is not published.
    assert publish.relays_seen == [(RELAY,), (RELAY,)]


def test_retracting_a_leftover_without_publishing_a_new_label_is_a_success():
    """`no label` is a state: a PR with no verdict and no card carries none."""
    publish = Recorder()
    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)

    result = publisher(PR, ADDRESS, (), ("12" * 32,))

    assert result.ok and result.retracted and result.events == ()
    assert [event["kind"] for event in publish.events] == [5]


def test_nothing_to_publish_and_nothing_to_retract_is_a_no_op():
    publish = Recorder()
    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)
    assert publisher(PR, ADDRESS, ()).ok is True
    assert publish.events == []


def test_a_label_no_relay_keeps_is_a_failure_and_is_not_recorded():
    publish = Recorder(accept=())
    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)

    result = publisher(PR, ADDRESS, (labels.APPROVED,))

    assert result.ok is False
    assert result.events == ()
    assert "blocked: nope" in result.error


def test_a_partial_relay_batch_still_counts_and_says_how_many():
    publish = Recorder(accept=("wss://a.example",))
    publisher = labels.LabelPublisher(["wss://a.example", "wss://b.example"],
                                      secret="00" * 32, publish=publish, sign=signer)

    result = publisher(PR, ADDRESS, (labels.APPROVED,))

    assert result.ok and result.relays == 1


def test_a_retraction_that_did_not_land_is_reported_but_does_not_undo_the_label():
    """The new label is what readers should see; a lingering old one is a note, not a redo."""

    def publish(event, relays, secret=None):
        # The label lands, the retraction is refused.
        return {RELAY: (event["kind"] == 1985, None if event["kind"] == 1985 else "restricted")}

    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)
    result = publisher(PR, ADDRESS, (labels.APPROVED,), ("12" * 32,))

    assert result.ok is True and result.retracted is False
    assert "restricted" in result.error


def test_publishing_with_no_relays_is_a_failure_not_a_silent_success():
    publisher = labels.LabelPublisher([], secret="00" * 32, publish=Recorder(), sign=signer)
    result = publisher(PR, ADDRESS, (labels.APPROVED,))
    assert result.ok is False
    assert "no relays" in result.error


def test_a_publish_that_raises_is_reported_not_propagated():
    def publish(event, relays, secret=None):
        raise OSError("relay hung up")

    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)
    result = publisher(PR, ADDRESS, (labels.APPROVED,))

    assert result.ok is False
    assert "OSError" in result.error
