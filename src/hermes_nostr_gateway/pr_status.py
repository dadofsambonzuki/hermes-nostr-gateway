"""The applied/resolved (kind-1631) and closed (kind-1632) records a merge has to publish.

Why a merge helper has to publish anything: `ngit pr apply` replays a proposal's patches
as **new** commits, so the proposal's own commits never become ancestors of the target and
ngit's inference ("the proposal is in master") never fires. The content is in, and the PR
still reads `open`. Only a maintainer kind-1631 that names the resulting commit moves it
to `applied` -- measured on ngit 3.0.5 -- which is why `scripts/merge_pr.sh` publishes one
after the push.

The same kind does the other half. An issue that began life as a card's work is *resolved*
by a kind-1631 naming the issue root: NIP-34 spells one status kind with two readings, so
`resolved_event` is `applied_event` with the root being an issue and the `alt` saying so.
The merge route writes it because the merge is the fact; a commit keyword depends on a
worker remembering one, and `ngit pr apply` replays patches, so a `Closes` line in a PR
description never reaches the branch a reader looks at.

The tags are decided by two readers, and both are written here:

* `merge_from_event` (and `ngit pr view`) reads the PR root from `e` marked `root` and the
  commit from `merge-commit-id` (ngit's spelling) or `merge-commit` (NIP-34's). Both carry
  the same full 40-character commit, so either reader sees the same fact.
* `RelayPrs.events` reads statuses from a repository's relay feed filtered by its NIP-34
  address, which a relay matches against the `a` tag. A status without that tag is
  invisible to the bridge however correct the rest of it is -- a hand-published close
  (`e`/`p`/`alt` only) is how that was learned, and it is why both builders emit `a`.

A close is the other half: a proposal that will not land is closed with a 1632, and the
cards it owns are blocked for a person rather than completed. `ngit pr close` writes a
1632 with no `a` tag, so `hng ngit closed` is the command a maintainer uses here.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

import nostr_sdk as sdk

from .crypto import parse_secret, sign_event
from .ngit import NgitError, run_ngit
from .publisher import publish as publish_events

log = logging.getLogger("hng.pr-status")

KIND_APPLIED = 1631
KIND_CLOSED = 1632

#: What `alt` says: NIP-31's human-readable summary, which is what a client that does not
#: know the kind shows instead of an empty row. Both spellings match what readers expect.
APPLIED_ALT = "git proposal merged / applied"
CLOSED_ALT = "PR closed"
RESOLVED_ALT = "issue resolved"

#: A full commit id. A short one is refused at the door: a status naming `60fc1107` is a
#: status naming nothing, and the failure reads like a PR that never applied.
_COMMIT = re.compile(r"^[0-9a-f]{40}$")

#: `30617:<owner-hex>:<identifier>`, the repository's NIP-34 coordinate.
_ADDRESS = re.compile(r"^30617:([0-9a-f]{64}):(.+)$")


def address_parts(address: str) -> tuple[str, str]:
    """A repository coordinate as (owner hex, identifier). Empty when it is not one."""
    found = _ADDRESS.match(str(address or "").strip().lower())
    if not found:
        return "", ""
    return found.group(1), found.group(2)


def full_commit(value: str) -> str:
    """A 40-character commit id, or raise with the reason it is not one.

    Short ids are the common mistake: they are accepted by every git command around this
    one, and a status that carries one names no commit at all.
    """
    text = str(value or "").strip().lower()
    if not text:
        raise NgitError("a commit id is required: pass the full 40-character sha")
    if not _COMMIT.match(text):
        raise NgitError(f"{text!r} is not a full 40-character commit id: the applied"
                        " record has to name a commit a reader can resolve")
    return text


def _root_tags(pr_id: str, relay_hint: str) -> list[list[str]]:
    """The tags that make a status refer to one proposal, in both NIP-34 spellings.

    `e` with the `root` marker is what `merge_from_event` reads; the `E` copy and the `q`
    quote are what an ngit-published status carries, and a NIP-22 client follows those. A
    relay hint is optional, and an empty slot is still a four-element `e` tag, so the
    marker stays where a reader looks for it.
    """
    return [
        ["e", pr_id, relay_hint, "root"],
        ["E", pr_id],
        ["q", pr_id],
    ]


def _a_tag(address: str, relay_hint: str) -> list[str]:
    """The repository coordinate: what a relay filters a repository's feed on."""
    return ["a", address, relay_hint] if relay_hint else ["a", address]


def _people(address: str, authors: Iterable[str]) -> list[list[str]]:
    """One `p` per person worth notifying: the proposal's author, then the repository owner.

    Dedupe is by key, not by position: the signer is often the owner, and an author can be
    both -- a record that names the same person twice reads as two people.
    """
    owner, _identifier = address_parts(address)
    out: list[list[str]] = []
    for who in (*tuple(authors), owner):
        key = str(who or "").strip().lower()
        if key and ["p", key] not in out:
            out.append(["p", key])
    return out


def applied_content(commit: str) -> str:
    """What the applied record says, in words a reader who has no board can act on."""
    return (f"Applied on master as {commit} (`ngit pr apply`: the proposal's commits are"
            " re-signed, so this status names the resulting commit).")


def applied_event(*, pr_id: str, address: str, merge_commit: str, root_commit: str = "",
                  authors: Sequence[str] = (), relay_hint: str = "",
                  created_at: Optional[int] = None) -> dict[str, Any]:
    """An unsigned kind-1631 saying which proposal landed as which commit.

    `merge_commit` is the commit on the target branch the push left; `root_commit` is the
    repository's first commit, which is the `r` tag an ngit-published status carries
    alongside the merge. Both are full 40-character ids: `full_commit` refuses anything
    else, because a short one parses and resolves to nothing.
    """
    pid = str(pr_id or "").strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", pid):
        raise NgitError(f"{pid[:16]!r} is not a full event id: the applied record names"
                        " the proposal root by its 64-character id")
    coordinate = str(address or "").strip()
    if not all(address_parts(coordinate)):
        raise NgitError(f"{coordinate!r} is not a NIP-34 repository address"
                        " (30617:<owner-hex>:<identifier>)")
    commit = full_commit(merge_commit)
    tags = [
        *_root_tags(pid, relay_hint),
        _a_tag(coordinate, relay_hint),
        *_people(coordinate, authors),
        ["r", commit],
    ]
    root = str(root_commit or "").strip().lower()
    if root and _COMMIT.match(root) and root != commit:
        tags.append(["r", root])
    tags += [
        ["merge-commit-id", commit],
        ["merge-commit", commit],
        ["alt", APPLIED_ALT],
    ]
    return {
        "kind": KIND_APPLIED,
        "content": applied_content(commit),
        "created_at": int(created_at if created_at is not None else time.time()),
        "tags": tags,
    }


def resolved_content(commit: str, pr_id: str = "") -> str:
    """What a resolved record says, in words a reader who has no board can act on."""
    by = f" (PR {pr_id[:8]})" if pr_id else ""
    return (f"Resolved on master as {commit}{by}. The work that answered this issue is"
            " merged; `ngit pr apply` re-signs a proposal's commits, so this status names"
            " the resulting commit.")


def resolved_event(*, issue_id: str, address: str, merge_commit: str, root_commit: str = "",
                   pr_id: str = "", authors: Sequence[str] = (), relay_hint: str = "",
                   created_at: Optional[int] = None) -> dict[str, Any]:
    """An unsigned kind-1631 saying which issue was answered by which commit.

    The root is the issue, not a PR: `_root_tags` writes the `e`/`E`/`q` a NIP-22 reader
    follows, `a` is what a repository feed filters on, and `merge-commit-id`/`merge-commit`
    hold the same full commit so ngit's reader and NIP-34's see the same fact. `pr_id` is
    the proposal that landed the work, named in the content only -- the record exists to
    answer the issue, and a proposal that was replayed as new commits is not resolvable
    from the issue's thread.

    Both ids are enforced at the door: a short issue id names no issue a reader can load,
    and a short commit names no commit at all.
    """
    iid = str(issue_id or "").strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", iid):
        raise NgitError(f"{iid[:16]!r} is not a full event id: a resolved record names"
                        " the issue root by its 64-character id")
    coordinate = str(address or "").strip()
    if not all(address_parts(coordinate)):
        raise NgitError(f"{coordinate!r} is not a NIP-34 repository address"
                        " (30617:<owner-hex>:<identifier>)")
    commit = full_commit(merge_commit)
    tags = [
        *_root_tags(iid, relay_hint),
        _a_tag(coordinate, relay_hint),
        *_people(coordinate, authors),
        ["r", commit],
    ]
    root = str(root_commit or "").strip().lower()
    if root and _COMMIT.match(root) and root != commit:
        tags.append(["r", root])
    tags += [
        ["merge-commit-id", commit],
        ["merge-commit", commit],
        ["alt", RESOLVED_ALT],
    ]
    proposal = str(pr_id or "").strip().lower()
    return {
        "kind": KIND_APPLIED,
        "content": resolved_content(commit, proposal if re.match(r"^[0-9a-f]{64}$", proposal)
                                    else ""),
        "created_at": int(created_at if created_at is not None else time.time()),
        "tags": tags,
    }


def closed_event(*, pr_id: str, address: str, reason: str = "",
                 authors: Sequence[str] = (), relay_hint: str = "",
                 created_at: Optional[int] = None) -> dict[str, Any]:
    """An unsigned kind-1632 closing a proposal that will not land.

    The `a` tag is the point: without it the close is invisible to `RelayPrs.events` and to
    every other repository reader, so a card waits for a merge that is never coming. Say
    *why* in `reason` -- `closed` on its own reads as "rejected", and a proposal whose
    content is already in master was not rejected.
    """
    pid = str(pr_id or "").strip().lower()
    if not re.match(r"^[0-9a-f]{64}$", pid):
        raise NgitError(f"{pid[:16]!r} is not a full event id: a close names the proposal"
                        " root by its 64-character id")
    coordinate = str(address or "").strip()
    if not all(address_parts(coordinate)):
        raise NgitError(f"{coordinate!r} is not a NIP-34 repository address"
                        " (30617:<owner-hex>:<identifier>)")
    return {
        "kind": KIND_CLOSED,
        "content": str(reason or "").strip(),
        "created_at": int(created_at if created_at is not None else time.time()),
        "tags": [
            *_root_tags(pid, relay_hint),
            _a_tag(coordinate, relay_hint),
            *_people(coordinate, authors),
            ["alt", CLOSED_ALT],
        ],
    }


def signed(event: Mapping[str, Any], secret: str) -> dict[str, Any]:
    """One signed event: id, pubkey and signature from the key that did the merge."""
    keys = sdk.Keys.parse(parse_secret(secret or ""))
    return sign_event(dict(event), keys)


def signer_pubkey(secret: str) -> str:
    """The hex pubkey a secret signs as, so the record can name its own author."""
    return sdk.Keys.parse(parse_secret(secret or "")).public_key().to_hex()


@dataclass(frozen=True)
class Published:
    """What one publish attempt did, per relay, in the terms the caller reports.

    A status that reached one relay is published (the bridge reads all of them and takes
    what it finds); one that reached none is not, and the reasons are named rather than
    summarised, because the fix differs per relay (auth, whitelist, down).
    """

    event_id: str = ""
    relays: tuple[str, ...] = ()
    delivered: tuple[str, ...] = ()
    refused: tuple[tuple[str, str], ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.delivered)

    def line(self) -> str:
        """"event <id> on <n>/<m> relays", or the reason nothing landed."""
        if self.error:
            return self.error
        if not self.ok:
            why = "; ".join(f"{relay}: {reason or 'refused'}" for relay, reason in self.refused)
            return f"no relay took the status ({why or 'no relays configured'})"
        return f"event {self.event_id} on {len(self.delivered)}/{len(self.relays)} relays"


def publish_status(event: Mapping[str, Any], relays: Sequence[str], secret: str, *,
                   publish: Callable[..., Mapping[str, tuple[bool, Optional[str]]]]
                   = publish_events) -> Published:
    """Sign one status and send it to every relay in the repository's read set."""
    targets = tuple(dict.fromkeys(str(relay).strip() for relay in relays
                                   if str(relay or "").strip()))
    if not targets:
        return Published(error="no relays to publish a status to: the repository"
                               " announcement named none and ngit.relays is empty")
    try:
        event = signed(event, secret)
    except Exception as exc:  # noqa: BLE001 - this runs from a shell script
        raise NgitError(f"could not sign the status: {type(exc).__name__}: {exc}") from exc
    try:
        answers = publish(event, list(targets), secret=secret) or {}
    except Exception as exc:  # noqa: BLE001 - a transport failure is a report line, not a crash
        return Published(event_id=str(event.get("id") or ""), relays=targets,
                         error=f"publish failed: {type(exc).__name__}: {exc}"[:200])
    delivered = tuple(relay for relay, (ok, _why) in answers.items() if ok)
    refused = tuple((relay, str(why or "")) for relay, (ok, why) in answers.items() if not ok)
    return Published(event_id=str(event.get("id") or ""), relays=targets,
                     delivered=delivered, refused=refused)


def root_commit(repo: Path) -> str:
    """The repository's first commit, for the `r` tag ngit's own statuses carry.

    Best effort: a shallow or empty checkout simply does not get the tag, which no reader
    requires.
    """
    try:
        done = subprocess.run(["git", "-C", str(repo), "rev-list", "--max-parents=0", "HEAD"],
                              capture_output=True, text=True, timeout=30.0)
    except (OSError, subprocess.SubprocessError):
        return ""
    roots = (done.stdout or "").split()
    return roots[-1].lower() if done.returncode == 0 and roots else ""


def pr_detail(repo: Path, ref: str) -> dict[str, Any]:
    """`ngit pr view <ref> --json` for one proposal: its full id, author and status.

    The id a caller has is often a short prefix or a nevent; ngit resolves it, and the
    nevent it prints decodes to the full 64-character root (`ngit.event_hex`).
    """
    data = run_ngit(repo, ["pr", "view", str(ref)])
    if not isinstance(data, dict) or not data.get("id"):
        raise NgitError(f"ngit pr view {ref} returned no proposal")
    return data
