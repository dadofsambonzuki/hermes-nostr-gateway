"""The verdict on the PR: the review card's comment, published as NIP-22 kind 1111.

Why this module exists
----------------------
The card is the queue; the PR is the record. A verdict used to live only in the review card's
comments, so a reader of `ngit pr view <pr> --comments` could not see that a review had
happened, and `scripts/merge_pr.sh` had no second signature to check before it merged. This
module is the one writer of that event: it reads the newest verdict comment off the review
card and publishes it **verbatim** as the review identity, and two readers then read the same
event back -- the label pass (`ngit_labels`) and the merge guard.

Two things were measured against ngit 3.0.5 rather than assumed:

* A PR comment is NIP-22 **kind 1111**, not 1622: root `E`/`K`/`P` naming the PR, its kind and
  its author, parent `e`/`k`/`p` naming what the comment replies to (the PR itself for a
  top-level comment), and **no `a` tag** -- so a `#a` filter can never find one. It is also
  why the comment is matched by its root `E`, never by the repository address.
* The body is the comment, exactly. `ngit pr comment --body` publishes the text it is given
  and nothing else: the review's own words are the record, and a summary of them would be a
  second document that can say something the reviewer did not.

Idempotency is the event, not a flag. `created_at` is the verdict comment's own timestamp, so
the same comment always signs to the same event id: a retry republishes the same bytes a relay
already holds (`duplicate:` is success), and a store that lost its row republishes nothing
new. The link row remembers only "this card's comment was published as this event", so a quiet
poll does no work; and before publishing, the PR's own thread is read once for the same text
from the same key -- a verdict the reviewer published by hand is left alone rather than
duplicated.

The merge guard uses the other half of the same read: `check_authorship` takes
`ngit pr view <pr> --json --comments` and refuses when the newest verdict is the PR author's
own. A review that the author wrote about their own work is not a second signature.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import nostr_sdk as sdk

from .control import as_hex
from .crypto import parse_secret, pubkey_from_secret, sign_event
from .fetch import fetch_tagged_blocking
from .ngit_labels import Verdict, newest_verdict, verdict_in
from .publisher import publish as publish_events

#: A comment is NIP-22. See the module docstring for why 1111 and not 1622.
KIND_COMMENT = 1111

#: The root kind a verdict comment names (`K`/`k`): a pull request.
KIND_PR = 1618

#: How many comments one read-back asks a relay for. The verdict is the newest one that
#: states a verdict; the same depth the label pass uses keeps the two reads comparable.
READ_LIMIT = 50

#: How long `ngit pr view` may take on the merge route. This runs before a merge, not on a
#: timer, so it is generous rather than tight.
NGIT_TIMEOUT_S = 120.0

#: Exit codes of `python -m hermes_nostr_gateway.ngit_verdict` (documented in merge_pr.sh).
#: 9, not 5: `scripts/merge_pr.sh` answers 5 for "the live checkout moved", 6 for a commit with
#: no card trailer, 7 for a card that is not in review or done, and 8 for a merge whose applied
#: record did not publish. A shared code would make "ask a reviewer" indistinguishable from any
#: of them. The number has moved twice while this PR was in review (master took 6 and 7, then
#: 8): the next free code is the one to take, and `merge_pr.sh` names them all in its header.
EXIT_UNREADABLE = 3
EXIT_REFUSED = 9


class VerdictError(RuntimeError):
    """The PR's thread could not be read; the message says what failed."""


# -- the event ----------------------------------------------------------------

def comment_event(*, pr_id: str, body: str, created_at: int = 0,
                  author: str = "") -> dict[str, Any]:
    """One unsigned NIP-22 kind-1111 comment on a PR root.

    `E`/`K` are the root scope and `P` its author; `e`/`k`/`p` are the parent, which for a
    top-level comment is the PR itself. No `a` tag: a NIP-22 comment carries none, and a
    reader that filters on the repository's address would never see it.
    """
    tags: list[list[str]] = [["E", str(pr_id)], ["K", str(KIND_PR)]]
    if author:
        tags.append(["P", str(author)])
    tags.extend([["e", str(pr_id)], ["k", str(KIND_PR)]])
    if author:
        tags.append(["p", str(author)])
    return {
        "kind": KIND_COMMENT,
        "content": str(body),
        "created_at": int(created_at or time.time()),
        "tags": tags,
    }


def _tag_value(event: Mapping[str, Any], letter: str) -> str:
    """The first value of one tag letter on an event, or ""."""
    for tag in event.get("tags") or ():
        if isinstance(tag, (list, tuple)) and len(tag) >= 2 and str(tag[0]) == letter:
            return str(tag[1])
    return ""


# -- the verdict, on the card --------------------------------------------------

@dataclass(frozen=True)
class CardVerdict:
    """The verdict a review card states: the comment's words, and where they live.

    `body` is the comment exactly as the reviewer wrote it -- not a reconstructed line, and
    not the review's first paragraph. `created_at` becomes the published event's timestamp,
    which is what makes a retry the same event rather than a second one.
    """

    task_id: str = ""
    comment_id: int = 0
    label: str = ""
    body: str = ""
    created_at: int = 0


def card_verdict(task_id: str, comments: Iterable[Mapping[str, Any]]) -> Optional[CardVerdict]:
    """The newest verdict comment on a review card, or None when it states none.

    The card is read the way the label pass reads the PR: newest first, prose comments
    skipped, and a comment that states a verdict nobody can parse stops the search -- an
    older verdict must not be published over a newer one that could not be read.
    """
    rows = [row for row in comments or () if isinstance(row, Mapping)]
    rows.sort(key=lambda row: (int(row.get("created_at") or 0), int(row.get("id") or 0)))
    for row in reversed(rows):
        body = str(row.get("body") or "")
        stated = verdict_in(body)
        if stated is None:
            continue
        if not stated:
            return None
        return CardVerdict(task_id=str(task_id), comment_id=int(row.get("id") or 0),
                           label=stated, body=body,
                           created_at=int(row.get("created_at") or 0))
    return None


# -- publishing ---------------------------------------------------------------

@dataclass(frozen=True)
class VerdictResult:
    """What one publish attempt did, in the terms the caller reports.

    `existing` is the idempotent case: the PR already carried this text from this key, so
    nothing was sent. `ok` alone would not say whether anything changed.
    """

    ok: bool = False
    event: str = ""
    relays: tuple[str, ...] = ()
    existing: bool = False
    error: str = ""


#: `read(pr_id) -> [(relay, event), ...]`: the PR root's comments, per relay.
Reader = Callable[[str], Sequence[tuple[str, Mapping[str, Any]]]]

#: `publish(pr_id, body, *, created_at, author, covers_after) -> VerdictResult`: what the
#: pass calls.
VerdictPublisherFn = Callable[..., "VerdictResult"]


def relay_reader(relays: Sequence[str], *, secret: str = "",
                 limit: int = READ_LIMIT) -> Reader:
    """The real reader: one `#E` REQ per relay, the relay that answered kept with the event."""
    urls = tuple(str(url) for url in relays if str(url or "").strip())

    def read(pr_id: str) -> Sequence[tuple[str, Mapping[str, Any]]]:
        return fetch_tagged_blocking(relays=urls, kinds=[KIND_COMMENT], tag="E",
                                     values=[str(pr_id)], limit=limit,
                                     secret=secret or None)

    return read


class VerdictPublisher:
    """Publish one verdict comment as the review identity, once per verdict.

    Signing is the review role's key, never the gateway's: the guard reads the author of the
    event, and a comment signed by the identity that authored the PR is not a second
    signature. The relay set is the one the pass reads PRs from, so a reader that can see the
    PR can see its verdict.
    """

    def __init__(self, relays: Sequence[str], *, secret: str,
                 publish: Callable[..., dict[str, tuple[bool, Optional[str]]]] = publish_events,
                 sign: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
                 read: Optional[Reader] = None):
        self.relays = tuple(str(relay) for relay in relays if str(relay or "").strip())
        self._secret = secret
        self._publish = publish
        self._sign = sign
        self._read = read or relay_reader(self.relays, secret=secret)
        self.pubkey = pubkey_from_secret(parse_secret(secret or "")) if secret else ""

    # -- signing and sending -------------------------------------------

    def _signed(self, event: dict[str, Any]) -> dict[str, Any]:
        if self._sign is None:
            keys = sdk.Keys.parse(parse_secret(self._secret))

            def sign(unsigned: dict[str, Any]) -> dict[str, Any]:
                return sign_event(unsigned, keys)

            self._sign = sign
        return self._sign(event)

    def _send(self, event: dict[str, Any]) -> dict[str, tuple[bool, Optional[str]]]:
        if not self.relays:
            return {}
        return self._publish(event, self.relays, secret=self._secret)

    # -- reading back ---------------------------------------------------

    def _published(self, pr_id: str, body: str, *, covers_after: int = 0) -> str:
        """The event id of an identical comment from this key already on the PR, or "".

        This is what keeps a reviewer who already published the verdict by hand from getting
        a second copy: the text and the key are the identity, and the created_at is not.

        `covers_after` is the tip the verdict has to cover. An identical comment older than
        the PR's current tip does not count -- it approved a tip that has since moved -- so a
        re-review that approves again in exactly the same words still publishes.
        """
        try:
            events = self._read(pr_id) or ()
        except Exception:  # noqa: BLE001 - a read that failed is "nothing found", not a crash
            return ""
        for _relay, event in events:
            if not isinstance(event, Mapping):
                continue
            if int(event.get("kind") or 0) != KIND_COMMENT:
                continue
            if str(event.get("pubkey") or "").lower() != self.pubkey:
                continue
            if str(event.get("content") or "") != body:
                continue
            root = _tag_value(event, "E")
            if root and root != str(pr_id):
                continue
            if covers_after and int(event.get("created_at") or 0) < int(covers_after):
                continue
            return str(event.get("id") or "")
        return ""

    def _seen(self, pr_id: str, event_id: str) -> bool:
        """Whether a relay now serves the event back: a signed event is not a published one."""
        try:
            events = self._read(pr_id) or ()
        except Exception:  # noqa: BLE001 - a read that failed is "not seen"
            return False
        return any(isinstance(event, Mapping) and str(event.get("id") or "") == event_id
                   for _relay, event in events)

    # -- the call -------------------------------------------------------

    def __call__(self, pr_id: str, body: str, *, created_at: int = 0,
                 author: str = "", covers_after: int = 0) -> VerdictResult:
        """Publish the verdict, unless it is already on the PR.

        `author` is the PR author's key, written into `P`/`p` so the thread says whose work
        the verdict is about. It is advisory metadata; the authorship rule lives in
        `check_authorship`, which reads the event's own signature.

        `covers_after` is the current tip's time: an identical verdict from this key that
        predates it does not cover the tip, so it does not stand in for this one.
        """
        if not self.relays:
            return VerdictResult(error="no relays to publish a verdict to")
        if not self._secret:
            return VerdictResult(error="no review identity to sign with")
        body = str(body)
        existing = self._published(pr_id, body, covers_after=covers_after)
        if existing:
            return VerdictResult(ok=True, event=existing, relays=self.relays, existing=True)
        try:
            signed = self._signed(comment_event(pr_id=pr_id, body=body,
                                               created_at=created_at,
                                               author=as_hex(author) or str(author or "")))
        except Exception as exc:  # noqa: BLE001 - a signing failure is a report line
            return VerdictResult(error=f"{type(exc).__name__}: {exc}"[:200])
        event_id = str(signed.get("id") or "")
        try:
            results = self._send(signed)
        except Exception as exc:  # noqa: BLE001 - a publisher may raise; the pass may not die
            return VerdictResult(error=f"{type(exc).__name__}: {exc}"[:200])
        landed = tuple(relay for relay, (good, _why) in results.items() if good)
        if not landed:
            reason = next((why for good, why in results.values() if not good),
                          "no relay accepted it")
            return VerdictResult(error=str(reason or "no relay accepted it")[:200])
        if not self._seen(pr_id, event_id):
            return VerdictResult(
                error="the verdict was signed and accepted, but no relay served it back "
                      "on the PR; the next pass asks again", relays=landed)
        return VerdictResult(ok=True, event=event_id, relays=landed)


# -- the merge guard's half ----------------------------------------------------

@dataclass(frozen=True)
class Authorship:
    """Whether the PR's newest verdict is somebody else's signature.

    `ok` is about the merge: a PR whose newest verdict came from its own author has no second
    signature, and the guard refuses it rather than treating the author's word as a review.
    """

    ok: bool = False
    why: str = ""
    author: str = ""
    verdict: Optional[Verdict] = None
    event: str = ""

    @property
    def label(self) -> str:
        return self.verdict.label if self.verdict is not None else ""


def _as_verdict_events(comments: Iterable[Any]) -> list[dict[str, Any]]:
    """`ngit pr view --json` comments as the events `newest_verdict` reads.

    The CLI spells a comment's author `author` and its text `body`; the relay event spells
    them `pubkey` and `content`. One place converts, so the guard and the label pass cannot
    disagree about what the newest verdict is.
    """
    events: list[dict[str, Any]] = []
    for comment in comments or ():
        if not isinstance(comment, Mapping):
            continue
        events.append({
            "kind": KIND_COMMENT,
            "id": str(comment.get("id") or ""),
            "pubkey": str(comment.get("author") or ""),
            "content": str(comment.get("body") or ""),
            "created_at": int(comment.get("created_at") or 0),
        })
    return events


def check_authorship(view: Mapping[str, Any]) -> Authorship:
    """Whether `ngit pr view --json --comments` carries a verdict by a different npub.

    Three refusals, each named: no verdict at all, a verdict whose words cannot be read, and
    a verdict from the PR's own author. Falling back to "the author looked at it" is the one
    direction that lets a PR merge with no second party on it, so it is never a fallback.
    """
    author = str(view.get("author") or "")
    newest = newest_verdict(_as_verdict_events(view.get("comments") or ()))
    if newest is None:
        return Authorship(why="no verdict on the PR", author=author)
    if not newest.readable:
        return Authorship(why="the newest verdict states a verdict that cannot be read",
                          author=author, verdict=newest)
    who = as_hex(newest.author)
    if not who:
        return Authorship(why=f"the verdict's author {newest.author!r} is not a key",
                          author=author, verdict=newest, event=newest.event_id)
    if who == as_hex(author):
        return Authorship(why=f"the newest verdict is the PR author's own ({_npub(author)});"
                              " a merge needs a verdict from a different npub",
                          author=author, verdict=newest, event=newest.event_id)
    return Authorship(ok=True, author=author, verdict=newest, event=newest.event_id)


def _npub(key: str) -> str:
    """A key as npub, which is how a person recognises it; the input when it is not one."""
    try:
        return sdk.PublicKey.parse(str(key or "")).to_bech32()
    except Exception:  # noqa: BLE001 - an unparseable key is printed as it came
        return str(key or "")


def short(value: str) -> str:
    """An id a person can match without counting 56 characters."""
    text = str(value or "")
    return text[:8] if len(text) > 8 else text


# -- the merge guard's entry point --------------------------------------------

def read_view(pr: str, *, runner: Callable[..., Any] = subprocess.run) -> Mapping[str, Any]:
    """One `ngit pr view --json --comments` as a mapping, or `VerdictError` saying why."""
    try:
        result = runner(["ngit", "pr", "view", str(pr), "--json", "--comments"],
                        capture_output=True, text=True, timeout=NGIT_TIMEOUT_S)
    except FileNotFoundError:
        raise VerdictError("ngit is not on PATH: the PR's thread cannot be read") from None
    except subprocess.TimeoutExpired:
        raise VerdictError(f"ngit pr view {short(pr)} did not answer within "
                           f"{NGIT_TIMEOUT_S:g}s") from None
    if int(getattr(result, "returncode", 1)) != 0:
        detail = (getattr(result, "stderr", "") or "").strip().splitlines()
        raise VerdictError(f"ngit pr view {short(pr)} failed"
                           + (f": {detail[-1]}" if detail else ""))
    try:
        data = json.loads(getattr(result, "stdout", "") or "")
    except json.JSONDecodeError:
        raise VerdictError(f"ngit pr view {short(pr)} returned no JSON") from None
    if not isinstance(data, Mapping):
        raise VerdictError(f"ngit pr view {short(pr)} returned {type(data).__name__}, "
                           "not a PR")
    return data


def main(argv: Optional[Sequence[str]] = None) -> int:
    """The merge route's check: exit 0 when the verdict is somebody else's, 9 when it is not.

    `scripts/merge_pr.sh` calls this before it applies anything. 3 is "could not read", which
    is a state error like the rest of that script's refusals; 9 is the authorship refusal, so
    a caller can tell "ask a reviewer" from "the relay is down". 9 and not 5 because the merge
    route already spends 5 on the live checkout, 6 and 7 on the card checks and 8 on a missing
    applied record: one code, one meaning, or the caller cannot tell which refusal it got.
    """
    parser = argparse.ArgumentParser(
        prog="python -m hermes_nostr_gateway.ngit_verdict",
        description="Check that a PR carries a verdict from a different npub than its author.")
    parser.add_argument("pr", help="the PR's event id or nevent")
    parser.add_argument("--json", action="store_true", help="machine-readable answer")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        view = read_view(args.pr)
    except VerdictError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    found = check_authorship(view)
    if args.json:
        print(json.dumps({"ok": found.ok, "why": found.why, "pr_author": found.author,
                          "verdict": found.label, "event": found.event}, indent=2))
    elif found.ok:
        print(f"OK   {short(args.pr)} carries a verdict from a different npub"
              f" ({_npub(found.verdict.author) if found.verdict else ''})")
    else:
        print(f"REFUSED: {found.why}", file=sys.stderr)
        print("  A merge needs a verdict from a different npub than the PR's author.",
              file=sys.stderr)
    return 0 if found.ok else EXIT_REFUSED


if __name__ == "__main__":  # pragma: no cover - the shell route's entry point
    raise SystemExit(main())
