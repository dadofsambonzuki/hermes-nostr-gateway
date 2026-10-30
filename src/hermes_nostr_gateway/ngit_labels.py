"""A pull request's review state, published as a NIP-34 label on the PR itself.

`ngit pr list` has a LABELS column, and every proposal this bridge announced left it empty:
"waiting for a review", "approved", "changes asked for" lived in task comments that nobody
outside the board reads. This module is the vocabulary and the wire format for putting that
state where the author already is.

Five words, one namespace:

    review-required    a PR with no verdict yet (what a new PR gets)
    in-review          a reviewer is working on it
    approved           verdict recorded; merging is the operator's
    changes-requested  verdict recorded with findings
    stale              a revision landed after the verdict it was given

Three things were measured against ngit 3.0.5 rather than assumed, because each one decides
how the bridge must publish:

* **The namespace is `#t`, or nobody sees the label.** `ngit pr list` merges kind-1985
  hashtag labels with the inline `t` tags a proposal may carry. A label event published with
  `L=#t` appears in `ngit pr list --json`; the same event in a namespace of our own
  (`L=ngit.review`) does not appear at all.
* **A later label does not supersede an earlier one.** Two events with two values in the
  same namespace both show (`["probe-d", "probe-c"]`). So "publish on change" alone would
  accumulate a history in the column, and a PR whose review was superseded would still read
  `approved`. What removes a label is NIP-09: retracting the event it is replacing with a
  kind-5. Verified across the whole read set: with the deletion on every relay, a reader
  with no cached copy sees only the current label.
* **ngit's own view is cached per checkout** (`.git/nostr-cache.lmdb`), and it does not
  re-read a retracted label until that cache is refreshed, so an operator who has been
  running `ngit pr list` all along may see the previous label until it is. That is the
  reader's cache, not a second source of truth here: the label events on the relays are the
  state, and this module publishes exactly one live label per PR.

The verdict is not stored here either. It is read from the review task the bridge already
creates (its comments), and this module turns it into a label. Publishing the same label
twice is pointless -- a reader would see one event either way -- so the caller publishes on
change only, and the link row remembers what it last published.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

import nostr_sdk as sdk

from .crypto import parse_secret, sign_event
from .publisher import publish as publish_events

#: What a label is published as. NIP-32's kind.
KIND_LABEL = 1985
KIND_DELETION = 5

#: The one namespace. `#t` is what ngit displays and filters on; see the module docstring.
NAMESPACE = "#t"

REVIEW_REQUIRED = "review-required"
IN_REVIEW = "in-review"
APPROVED = "approved"
CHANGES_REQUESTED = "changes-requested"
STALE = "stale"

#: The vocabulary, in the order a reader sees a PR move through it.
LABELS = (REVIEW_REQUIRED, IN_REVIEW, APPROVED, CHANGES_REQUESTED, STALE)

#: One line of content per label: NIP-32 allows the label event to say why, and a reader who
#: sees the event without the CLI (a relay dump, another client) should not have to guess.
CONTENT = {
    REVIEW_REQUIRED: "This pull request needs a review before it can be merged.",
    IN_REVIEW: "A review of this pull request is in progress.",
    APPROVED: "A review approved this revision; merging is the maintainer's decision.",
    CHANGES_REQUESTED: "A review asked for changes; see the review comments on this proposal.",
    STALE: "A revision landed after this pull request was reviewed.",
}

#: A verdict line: `Verdict: approved`, `## Verdict: changes needed`, `**VERDICT:** ...`.
_VERDICT_LINE = re.compile(
    r"^\s*(?:#+\s*)?\**\s*verdict\s*\**\s*:?\s*\**\s*(?P<rest>.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class LabelResult:
    """What one publish attempt did, in the terms the caller has to report.

    `ok` is about the label a reader should now see; `retracted` is about the one it
    replaces. They are separate on purpose: a new label that landed while the old one was
    not retracted is a visible pair, not a failure, and saying so is more useful than
    reporting the whole publish as failed and trying it again.
    """

    ok: bool = False
    events: tuple[str, ...] = ()
    retracted: bool = True
    relays: int = 0
    error: str = ""


#: `labels(pr_id, address, add, retract) -> LabelResult`.
Labeler = Callable[[str, str, Sequence[str], Sequence[str]], LabelResult]


# -- the events ---------------------------------------------------------------

def label_event(*, pr_id: str, address: str, label: str,
                created_at: Optional[int] = None) -> dict[str, Any]:
    """One unsigned kind-1985 naming the PR and its repository.

    The `a` tag is what a repository reader filters on; the `e` tag is the PR itself. Both
    are written because NIP-32's target tags are optional individually and a client may
    follow either one.
    """
    return {
        "kind": KIND_LABEL,
        "content": CONTENT.get(label, ""),
        "created_at": int(created_at if created_at is not None else time.time()),
        "tags": [
            ["L", NAMESPACE],
            ["l", label, NAMESPACE],
            ["a", str(address)],
            ["e", str(pr_id)],
            ["alt", f"review label: {label}"],
        ],
    }


def retraction_event(event_id: str, *, created_at: Optional[int] = None) -> dict[str, Any]:
    """An unsigned kind-5 naming the label event it removes.

    NIP-09 deletion is the only thing that takes a label off a PR: a later label adds to the
    column instead of replacing what is there (see the module docstring).
    """
    return {
        "kind": KIND_DELETION,
        "content": "",
        "created_at": int(created_at if created_at is not None else time.time()),
        "tags": [["e", str(event_id)], ["k", str(KIND_LABEL)]],
    }


# -- the state ----------------------------------------------------------------

def review_labels(*, verdict: str = "", task_status: str = "", tip: str = "",
                  verdict_tip: str = "", merged: bool = False) -> Optional[tuple[str, ...]]:
    """The labels the PR should carry now, or None when it is not ours to change.

    `None` is the merged case: a merge leaves the last verdict in place, because the verdict
    is what the merge was decided on and rewriting it afterwards would lose that.

    The revision rule comes before the verdict rule on purpose: a verdict covers the tip it
    was given for, so a tip that is no longer that one leaves the PR needing review again --
    with `stale` alongside when the superseded verdict was an approval, because that is the
    case where a reader could otherwise merge something nobody reviewed.
    """
    if merged:
        return None
    if verdict and verdict_tip and tip and tip != verdict_tip:
        return (REVIEW_REQUIRED, STALE) if verdict == APPROVED else (REVIEW_REQUIRED,)
    if verdict:
        return (verdict,)
    if task_status == "running":
        return (IN_REVIEW,)
    # Ready, in the review lane, blocked on authorisation, or finished without a verdict
    # that can be read: either way nobody has recorded a verdict, and `review-required` is
    # the label that cannot be mistaken for permission to merge.
    return (REVIEW_REQUIRED,)


def _classify(text: str) -> str:
    """Read one verdict line's words. Empty when it is not a verdict we recognise."""
    head = (text or "").strip().lower()
    if not head:
        return ""
    if head.startswith(("change", "request change", "needs work", "rework", "reject")):
        return CHANGES_REQUESTED
    if head.startswith(("approve", "lgtm", "merge", "ship")):
        return APPROVED
    # A verdict that leads with something else still names one of the two outcomes.
    if any(word in head for word in ("changes needed", "changes requested", "request changes",
                                     "needs changes", "not approved", "cannot approve")):
        return CHANGES_REQUESTED
    if "approved" in head or "approve" in head:
        return APPROVED
    return ""


def _verdict_in(body: str) -> str:
    """The verdict stated in one comment, if it states one.

    Only the verdict line is read, and only its first few lines are searched: a review's
    prose contains both words sooner or later ("not approved until ..."), so a whole-body
    search would turn reasoning into a verdict.
    """
    lines = [line for line in (body or "").splitlines() if line.strip()][:6]
    for line in lines:
        found = _VERDICT_LINE.match(line)
        if found:
            return _classify(found.group("rest"))
    return ""


def parse_verdict(bodies: Iterable[str]) -> str:
    """The newest verdict stated in a review task's comments, or "".

    The task is the record: a reviewer writes the verdict as a comment, so the newest
    comment that states one is the verdict in force. Reasoning comments and the bridge's own
    merge line state none and are skipped, which is what lets the merge line land on top of
    an approval without erasing it.
    """
    for body in reversed(list(bodies)):
        verdict = _verdict_in(body)
        if verdict:
            return verdict
    return ""


# -- publishing ---------------------------------------------------------------

def _signer(secret: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Sign with the gateway's own identity, the key every other write already uses."""
    keys = sdk.Keys.parse(parse_secret(secret or ""))

    def sign(event: dict[str, Any]) -> dict[str, Any]:
        return sign_event(event, keys)

    return sign


class LabelPublisher:
    """Publish one PR's labels, retracting the ones they replace.

    Four events at most per state change (two labels in the `stale` case, plus two
    retractions), each signed once and sent to every relay in the read set -- the same set
    the bridge reads the PR from, so a reader that can see the PR can see its label. The
    retraction has to reach every one of them: a relay that kept the old label keeps showing
    it, which is the whole reason the superseded event is deleted rather than left to be
    out-ranked.
    """

    def __init__(self, relays: Sequence[str], *, secret: str = "",
                 publish: Callable[..., dict[str, tuple[bool, Optional[str]]]] = publish_events,
                 sign: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                 now: Callable[[], float] = time.time):
        self.relays = tuple(str(relay) for relay in relays if str(relay or "").strip())
        self._secret = secret
        self._publish = publish
        self._sign = sign
        self._now = now

    def _signed(self, event: dict[str, Any]) -> dict[str, Any]:
        if self._sign is None:
            self._sign = _signer(self._secret)
        return self._sign(event)

    def _send(self, event: dict[str, Any]) -> dict[str, tuple[bool, Optional[str]]]:
        if not self.relays:
            return {}
        return self._publish(event, self.relays, secret=self._secret)

    def _send_signed(self, event: dict[str, Any]) -> tuple[dict[str, Any],
                                                           dict[str, tuple[bool, Optional[str]]]]:
        signed = self._signed(event)
        signed["id"] = signed.get("id") or ""
        return signed, self._send(signed)

    def __call__(self, pr_id: str, address: str,
                 add: Sequence[str], retract: Sequence[str] = ()) -> LabelResult:
        if not self.relays:
            return LabelResult(ok=False, retracted=False,
                               error="no relays to publish a label to")
        created = int(self._now())
        kept: list[str] = []
        accepted: Optional[set[str]] = None
        errors: list[str] = []
        for label in add:
            try:
                signed, results = self._send_signed(
                    label_event(pr_id=pr_id, address=address, label=label, created_at=created))
            except Exception as exc:  # noqa: BLE001 - a publish failure is a report line
                errors.append(f"{label}: {type(exc).__name__}: {exc}"[:200])
                continue
            ok = {relay for relay, (good, _) in results.items() if good}
            if not ok:
                reason = next((why for good, why in results.values() if not good),
                              "no relay accepted it")
                errors.append(f"{label}: {reason}"[:200])
                continue
            kept.append(str(signed.get("id") or ""))
            accepted = ok if accepted is None else (accepted & ok)
        if not kept or accepted is None:
            return LabelResult(ok=False, retracted=not retract, relays=0,
                               error="; ".join(errors) or "nothing to publish")

        retracted = True
        for event_id in retract:
            if not event_id:
                continue
            try:
                _, results = self._send_signed(retraction_event(event_id, created_at=created))
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                errors.append(f"retract {event_id[:8]}: {type(exc).__name__}: {exc}"[:200])
                retracted = False
                continue
            if not any(good for good, _ in results.values()):
                retracted = False
                reason = next((why for good, why in results.values() if not good),
                              "no relay accepted it")
                errors.append(f"retract {event_id[:8]}: {reason}"[:200])
        return LabelResult(ok=True, events=tuple(kept), retracted=retracted,
                           relays=len(accepted), error="; ".join(errors))
