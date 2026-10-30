"""The PR label: five words, one namespace, published on change and retracted on change.

The shapes here are the measured ones (see `ngit_labels`): a kind-1985 with `L=#t`, a `l`
value in that namespace, and both the repository's `a` and the PR's `e`. Placeholders only.
"""

from __future__ import annotations

import pytest

from hermes_nostr_gateway import ngit_labels as labels

PR = "ab" * 32
ADDRESS = f"30617:{'ee' * 32}:example-repo"
RELAY = "wss://relay.example"


# -- the vocabulary ------------------------------------------------------------

def test_the_vocabulary_is_five_words_in_one_namespace():
    assert labels.LABELS == ("review-required", "in-review", "approved",
                             "changes-requested", "stale")
    assert labels.NAMESPACE == "#t"


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

def test_a_pr_with_no_review_says_so_from_the_start():
    assert labels.review_labels() == (labels.REVIEW_REQUIRED,)
    assert labels.review_labels(task_status="ready") == (labels.REVIEW_REQUIRED,)
    assert labels.review_labels(task_status="blocked") == (labels.REVIEW_REQUIRED,)


def test_a_reviewer_at_work_is_in_review():
    assert labels.review_labels(task_status="running") == (labels.IN_REVIEW,)


@pytest.mark.parametrize("verdict", [labels.APPROVED, labels.CHANGES_REQUESTED])
def test_a_verdict_is_the_label(verdict):
    assert labels.review_labels(verdict=verdict, task_status="done",
                                tip="aa", verdict_tip="aa") == (verdict,)


def test_a_revision_after_a_verdict_needs_review_again():
    assert labels.review_labels(verdict=labels.APPROVED, task_status="done",
                                tip="new", verdict_tip="old") == (
        labels.REVIEW_REQUIRED, labels.STALE)


def test_a_revision_after_changes_requested_is_not_stale():
    """`stale` exists for the approval a reader could otherwise act on; changes are open."""
    assert labels.review_labels(verdict=labels.CHANGES_REQUESTED, task_status="done",
                                tip="new", verdict_tip="old") == (labels.REVIEW_REQUIRED,)


def test_a_merge_leaves_the_label_alone():
    assert labels.review_labels(verdict=labels.APPROVED, merged=True) is None
    assert labels.review_labels(merged=True) is None


def test_a_verdict_with_no_known_tip_is_not_made_stale_by_one():
    assert labels.review_labels(verdict=labels.APPROVED, tip="aa", verdict_tip="") == (
        labels.APPROVED,)


# -- the verdict, as reviewers write it -----------------------------------------

def comment(body: str, comment_id: int = 1) -> dict:
    return {"id": comment_id, "body": body}


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
    assert labels.parse_verdict([comment(body)]) == (expected, 1)


@pytest.mark.parametrize("body", [
    "",
    "Reviewed the diff and ran the gates. All green.",
    "This is not approved until the second hunk is fixed.",  # prose, not a verdict line
])
def test_a_comment_without_a_verdict_line_states_no_verdict(body):
    assert labels.parse_verdict([comment(body)]) == ("", 0)


def test_a_verdict_we_cannot_read_states_no_verdict_but_stops_the_search():
    assert labels.parse_verdict([comment("Verdict: I have some thoughts")]) == ("", 0)


def test_the_newest_verdict_wins_and_a_later_merge_line_does_not_erase_it():
    bodies = [comment("## Verdict: changes needed", 1), comment("fixed the two findings", 2),
              comment("## Verdict: approve", 3),
              comment("PR abababab merged as dededede: the review is closed by the merge.", 4)]
    assert labels.parse_verdict(bodies) == (labels.APPROVED, 3)


def test_a_newer_verdict_we_cannot_read_does_not_fall_back_to_an_older_one():
    """Publishing a stale approval over a verdict nobody parsed is the wrong direction."""
    bodies = [comment("## Verdict: approve", 1), comment("## Verdict: I have some thoughts", 2)]
    assert labels.parse_verdict(bodies) == ("", 0)


def test_the_comment_id_tells_two_identical_verdicts_apart():
    """A re-review approves in the same words; the id is what says it is a new verdict."""
    first = labels.parse_verdict([comment("## Verdict: approved", 7)])
    second = labels.parse_verdict([comment("## Verdict: approved", 7),
                                   comment("## Verdict: approved", 9)])
    assert first == (labels.APPROVED, 7)
    assert second == (labels.APPROVED, 9)


def test_no_verdict_at_all_is_empty():
    assert labels.parse_verdict([]) == ("", 0)


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


def test_two_labels_are_two_events():
    publish = Recorder()
    publisher = labels.LabelPublisher([RELAY], secret="00" * 32,
                                      publish=publish, sign=signer)
    result = publisher(PR, ADDRESS, (labels.REVIEW_REQUIRED, labels.STALE))
    assert result.ok and len(result.events) == 2
    assert [event["kind"] for event in publish.events] == [1985, 1985]


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
