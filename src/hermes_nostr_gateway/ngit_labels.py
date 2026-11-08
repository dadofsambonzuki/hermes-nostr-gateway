"""A pull request's review state, published as a NIP-34 label on the PR itself.

Three words, one namespace, one writer:

    review-required    no verdict yet, or a tip newer than the last verdict
    changes-requested  the newest verdict asks for changes
    approved           the newest verdict approves it

`#in-review` is gone on purpose. An open PR with a review card already *is* in review, and
a second word for "a reviewer is looking at it" says nothing a reader cannot see on the
board. `#stale` is gone with it: a tip newer than the last verdict means `review-required`,
which is the same fact in the one word a reader has to look at -- and two labels for one
PR is how the column accumulated a history nobody could read.

The verdict is read from the **PR root**, not from the board. A reviewer publishes its
verdict as a NIP-22 kind-1111 comment on the PR (root `E` = the PR id), and the newest one
wins. The board card is the queue; the PR is the record. A reader who never sees the board
still sees the verdict the label is derived from, and a verdict the reviewer recorded on
the card but never published leaves the label where it is rather than inventing a state the
PR's own readers cannot check.

Three things about the format were measured against ngit 3.0.5 rather than assumed, because
each one decides how the bridge must publish:

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

A label this bridge did not write is corrected the same way it writes its own: the pass
reads the live label events off the relays, publishes the one that belongs there and
retracts every other one -- including `in-review` and `stale` left by an earlier writer,
and including a second event carrying the *same* value. A duplicate publish (two passes
racing, or a pass that did not read the first event) leaves a pair that says one thing
twice; selecting the leftovers by value alone cannot see it. So the survivor is chosen
first, and everything else live is retracted. That is what makes the pass the only writer:
no cleanup follows it by hand.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import nostr_sdk as sdk

from .crypto import parse_secret, sign_event
from .publisher import publish as publish_events

#: What a label is published as. NIP-32's kind.
KIND_LABEL = 1985
KIND_DELETION = 5

#: A verdict is a NIP-22 comment. ngit 3.0.5 publishes `ngit pr comment` as this kind
#: (`src/lib/git_events.rs`, `KIND_COMMENT`), and no ngit release ever wrote 1622.
KIND_COMMENT = 1111

#: The one namespace. `#t` is what ngit displays and filters on; see the module docstring.
NAMESPACE = "#t"

REVIEW_REQUIRED = "review-required"
APPROVED = "approved"
CHANGES_REQUESTED = "changes-requested"

#: The vocabulary, and the whole of it. One label per PR, never two.
LABELS = (REVIEW_REQUIRED, CHANGES_REQUESTED, APPROVED)

#: One line of content per label: NIP-32 allows the label event to say why, and a reader who
#: sees the event without the CLI (a relay dump, another client) should not have to guess.
CONTENT = {
    REVIEW_REQUIRED: "This pull request needs a review of its current tip before it can be merged.",
    CHANGES_REQUESTED: "A review asked for changes; see the review comments on this proposal.",
    APPROVED: "A review approved this revision; merging is the maintainer's decision.",
}

#: A verdict line: `Verdict: approved`, `## Verdict: changes needed`, `**VERDICT:** ...`.
_VERDICT_LINE = re.compile(
    r"^\s*(?:#+\s*)?\**\s*verdict\s*\**\s*:?\s*\**\s*(?P<rest>.*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Verdict:
    """The newest verdict a PR root carries, and the event that states it.

    `readable` is separate from `label` on purpose: a comment that states a verdict in
    words this module does not recognise is newer than the last one it *could* read, and
    falling back to an older approval over it is the one direction that could publish a
    green light nobody gave. The caller leaves the label untouched and says why.

    `body` is the comment's own words -- the findings, when the verdict asks for changes.
    The card that answers the verdict carries them so the work is queued with the reasons,
    not only with a pointer to a relay a worker may not read.
    """

    label: str = ""
    event_id: str = ""
    created_at: int = 0
    relay: str = ""
    readable: bool = True
    body: str = ""


@dataclass(frozen=True)
class Label:
    """One live kind-1985 label event on a PR, as a relay served it."""

    value: str
    event_id: str
    created_at: int = 0
    relay: str = ""


@dataclass(frozen=True)
class VerdictAnchor:
    """A verdict and the tip it was given for: the state `review_labels` reads.

    `tip` empty means "the tip this verdict covers is not known", and a verdict with no
    known tip is never treated as superseded -- the conservative direction is to keep an
    approval that cannot be shown to be stale rather than to withdraw one that can't be
    shown to be current. Every verdict that arrives with a tip or a time anchors to one.
    """

    label: str
    event_id: str = ""
    created_at: int = 0
    relay: str = ""
    tip: str = ""


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


def live_label(event: Mapping[str, Any]) -> Optional[Label]:
    """One relay event as the label it carries, or None when it is not one of ours.

    Only the `#t` namespace counts: a kind-1985 in a namespace of another tool is not this
    PR's review state, and retracting it would be the bridge deleting someone else's work.
    """
    if int(event.get("kind") or 0) != KIND_LABEL:
        return None
    tags = event.get("tags") or ()
    namespaces = {tag[1] for tag in tags
                  if isinstance(tag, (list, tuple)) and len(tag) >= 2 and tag[0] == "L"}
    if NAMESPACE not in namespaces:
        return None
    value = next((str(tag[1]) for tag in tags
                  if isinstance(tag, (list, tuple)) and len(tag) >= 3
                  and tag[0] == "l" and str(tag[2]) == NAMESPACE), "")
    if not value:
        return None
    return Label(value=value, event_id=str(event.get("id") or ""),
                 created_at=int(event.get("created_at") or 0),
                 relay=str(event.get("relay") or ""))


# -- the state ----------------------------------------------------------------

def review_labels(*, verdict: Optional[VerdictAnchor] = None, review_card: bool = False,
                  tip: str = "", tip_at: int = 0, labelled: bool = False,
                  merged: bool = False) -> Optional[tuple[str, ...]]:
    """The label the PR should carry now, or None when it is not ours to change.

    `None` is "leave it alone": a merged PR keeps the label it was decided on, and a PR
    that already carries a label but whose verdict could not be read keeps that label
    rather than being rewritten from a read that did not happen.

    The revision rule comes before the verdict rule on purpose: a verdict covers the tip it
    was given for, so a tip that is no longer that one leaves the PR needing review again.
    The tip a verdict covered is known from the anchor, and when this pass is the first to
    see an old verdict next to a newer tip event, the times say the same thing.

    `()` is a *decided* state, not an absence: there is no verdict and no review card, so
    the PR carries no label at all and any label left on it is retracted.
    """
    if merged:
        return None
    if verdict is not None:
        reviewed = verdict.tip
        if reviewed and tip and reviewed == tip:
            # The tip is the one that was reviewed, whatever else has been published since.
            return (verdict.label,)
        if reviewed and tip and reviewed != tip:
            return (REVIEW_REQUIRED,)
        if tip_at and verdict.created_at and verdict.created_at < tip_at:
            # This pass saw the verdict and the newer tip for the first time: the reviewer
            # read an earlier commit, so the verdict does not cover the one on the PR now.
            return (REVIEW_REQUIRED,)
        return (verdict.label,)
    if review_card:
        # A first label for a card nobody has reviewed yet. Once a label is there and the
        # verdict cannot be read, "review-required" would be a guess about a read that did
        # not happen -- so the label stays where it is until a verdict actually lands.
        return None if labelled else (REVIEW_REQUIRED,)
    return ()


def anchor_verdict(stored: Mapping[str, Any], verdict: Optional[Verdict],
                   *, tip: str = "", tip_at: int = 0) -> Optional[VerdictAnchor]:
    """The verdict in force and the tip it covers, re-anchored to its own event.

    The event id is the anchor: two verdicts can say the same word, and a re-review of a
    revised tip approves again in exactly the words the first one used, so moving the
    anchor on the words alone would pin a fresh approval to the old tip and read `stale`
    for ever. A new event id re-anchors; the same one keeps the tip it was first anchored
    to.

    A verdict newer than the tip event we know about covers that tip. A verdict older than
    the tip event does not cover it -- an earlier tip is what the reviewer read, and this
    pass did not see it, so the anchor's tip stays unknown rather than claiming the current
    one.
    """
    if verdict is None or not verdict.readable:
        return None
    if (stored.get("verdict_event") == verdict.event_id
            and str(stored.get("verdict") or "") == verdict.label):
        return VerdictAnchor(
            label=verdict.label, event_id=verdict.event_id,
            created_at=int(stored.get("verdict_at") or verdict.created_at),
            relay=str(stored.get("verdict_relay") or verdict.relay),
            tip=str(stored.get("verdict_tip") or ""))
    covered = ""
    if not tip_at or not verdict.created_at or tip_at <= verdict.created_at:
        covered = tip
    return VerdictAnchor(label=verdict.label, event_id=verdict.event_id,
                         created_at=verdict.created_at, relay=verdict.relay, tip=covered)


#: The link-detail keys a verdict anchor is written to. One list so a caller clearing the
#: anchor for a new review round cannot miss one and leave half a verdict behind.
ANCHOR_KEYS = ("verdict", "verdict_event", "verdict_at", "verdict_relay", "verdict_tip")


def anchor_detail(anchor: VerdictAnchor) -> dict[str, Any]:
    """The link-detail fields for an anchor: what the next pass re-reads to stay quiet."""
    return {"verdict": anchor.label, "verdict_event": anchor.event_id,
            "verdict_at": anchor.created_at, "verdict_relay": anchor.relay,
            "verdict_tip": anchor.tip}


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


def _verdict_in(body: str) -> Optional[str]:
    """The verdict stated in one comment: a label, "" for a verdict we cannot read, or None.

    The three answers matter. None is "this comment is not a verdict" and the search moves
    on; "" is "this comment states a verdict and we cannot read it", which must stop the
    search -- otherwise an older verdict would be published over a newer one nobody parsed.

    Only the first few lines are searched: a review's prose contains both words sooner or
    later ("not approved until ..."), so a whole-body search turns reasoning into a verdict.
    """
    lines = [line for line in (body or "").splitlines() if line.strip()][:6]
    for line in lines:
        found = _VERDICT_LINE.match(line)
        if not found:
            continue
        verdict = _classify(found.group("rest"))
        if verdict:
            return verdict
        return ""
    return None


def newest_verdict(events: Iterable[Mapping[str, Any]]) -> Optional[Verdict]:
    """The newest verdict among a PR root's kind-1111 comments, or None for no verdict.

    Newest is by `created_at`, then by event id: a relay replaying an older comment must
    not outrank a newer one, and two comments stamped the same second need an order that
    does not depend on which relay answered first. Prose comments are skipped -- that is
    what lets a review's reasoning, and the bridge's own merge line, sit above a verdict
    without erasing it. A verdict-looking line nobody can parse stops the search and comes
    back marked unreadable.
    """
    comments = []
    for event in events or ():
        if not isinstance(event, Mapping):
            continue
        kind = event.get("kind")
        if kind is not None and int(kind or 0) != KIND_COMMENT:
            continue
        comments.append(event)
    comments.sort(key=lambda event: (int(event.get("created_at") or 0),
                                     str(event.get("id") or "")), reverse=True)
    for event in comments:
        stated = _verdict_in(str(event.get("content") or ""))
        if stated is None:
            continue
        return Verdict(label=stated, event_id=str(event.get("id") or ""),
                       created_at=int(event.get("created_at") or 0),
                       relay=str(event.get("relay") or ""), readable=bool(stated),
                       body=str(event.get("content") or "") if stated else "")
    return None


def reconcile_labels(labels: Iterable[Label], desired: Sequence[str],
                     recorded: Sequence[str] = ()) -> tuple[Optional[Label], list[Label]]:
    """The live event that *is* the label, and the ones that have to go.

    `desired` is the one value the PR should carry -- empty for "no label at all" -- and
    `recorded` the event ids the link row says it published last. Selecting the leftovers
    by value alone cannot see a second live event carrying the desired value (two passes
    racing, or one that did not read the other's publish), so the survivor is chosen first:
    the row's own event when the relays still serve it, otherwise the newest live event
    carrying the desired value. Every other live event is retracted, whatever it says.

    `keep` is None when no live event carries the desired value. A row that says the label
    is there is not evidence the relays still hold it: the caller publishes one rather than
    leaving the PR with no label at all.
    """
    live = [label for label in labels if label.event_id]
    wanted = set(desired)
    if not wanted:
        return None, live
    candidates = [label for label in live if label.value in wanted]
    if not candidates:
        return None, live
    published = {str(event) for event in recorded if event}
    ours = [label for label in candidates if label.event_id in published]
    keep = max(ours or candidates, key=lambda label: (label.created_at, label.event_id))
    return keep, [label for label in live if label.event_id != keep.event_id]


# -- publishing ---------------------------------------------------------------

def _signer(secret: str) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """Sign with the gateway's own identity, the key every other write already uses."""
    keys = sdk.Keys.parse(parse_secret(secret or ""))

    def sign(event: dict[str, Any]) -> dict[str, Any]:
        return sign_event(event, keys)

    return sign


class LabelPublisher:
    """Publish one PR's label, retracting every label event it replaces.

    Each event is signed once and sent to every relay in the read set -- the same set the
    bridge reads the PR from, so a reader that can see the PR can see its label. The
    retraction has to reach every one of them: a relay that kept the old label keeps showing
    it, which is the whole reason the superseded event is deleted rather than left to be
    out-ranked.

    `add` empty with `retract` non-empty is a real state, not a no-op: the PR should carry
    no label at all, and the leftovers are removed. A publish with nothing to do succeeds
    without sending anything.
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
        if add and not kept:
            # Nothing a reader needs landed. The old label is still there, so the row must
            # keep saying what readers see and the next pass tries again.
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
            ok = {relay for relay, (good, _) in results.items() if good}
            if not ok:
                retracted = False
                reason = next((why for good, why in results.values() if not good),
                              "no relay accepted it")
                errors.append(f"retract {event_id[:8]}: {reason}"[:200])
                continue
            accepted = ok if accepted is None else (accepted & ok)
        return LabelResult(ok=True, events=tuple(kept), retracted=retracted,
                           relays=len(accepted or ()), error="; ".join(errors))
