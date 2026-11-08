"""Closing a duplicate proposal names the successor that replaced it.

Why this module exists
----------------------
`ngit pr close` publishes a kind-1632 whose `content` is the reason, and the reason is
optional. An empty close is not a close a reader can act on: it reads as a withdrawal, and
the proposal that survived is named nowhere on the dead one. Two real closes on this
repository show the difference -- one said "superseded by <id> (tip <sha>)", the other was
empty -- and the empty one left a reviewer with no successor to follow.

The duplicate shape it exists for is the re-push. A branch is amended and re-pushed, ngit
publishes a second kind-1618 for the same branch, and the older proposal stays open with its
content already on master. The successor is the other open proposal whose branch matches
(`ngit pr view` prints the proposal's short id as a `(<8hex>)` suffix on `branch`), and its
tip is what `ngit pr view --json` reports in `ci.runs[0].commit`.

`successors` is read-only: the merge route uses it to name the successor in its "applies no
commits" refusal. `close` is the writer's route: it refuses to publish a close that cannot
name a successor, builds the reason, runs `ngit pr close --reason`, and reads the status
back -- a successful publish is not a successful close.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from .ngit import event_hex

#: `ngit pr view` prints the proposal's short id as a suffix on the branch it names.
_BRANCH_SUFFIX = re.compile(r"\(([0-9a-f]{8})\)$")

#: The words a close carries when the caller did not add any. The reason has to say what
#: happened, not only that something did; this is the shape the surviving closes use.
DEFAULT_WHY = "a re-push published a second proposal for the same branch"

#: Statuses a proposal can be closed from. A draft is a proposal too.
OPEN = ("open", "draft")

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_UNREADABLE = 3


class CloseError(RuntimeError):
    """The close could not be read, or the checkout cannot answer; exit 3."""


class CloseRefused(CloseError):
    """The close must not be published; the message is the reason, and it exits 2."""


@dataclass(frozen=True)
class Successor:
    """An open proposal that shares a duplicate's branch: what a close must name."""

    id: str = ""          # full hex event id
    short: str = ""       # the eight characters people quote
    nevent: str = ""      # the reference ngit printed, when it printed one
    tip: str = ""         # the commit the proposal carries, when ngit named one
    subject: str = ""

    @property
    def label(self) -> str:
        return self.short or self.id[:8] or "?"


# -- reading ngit --------------------------------------------------------------

def _ngit(repo: Path, *args: str, timeout: float = 120.0) -> str:
    """Run one ngit command in the checkout, or raise with what it said."""
    try:
        result = subprocess.run(["ngit", *args], cwd=str(repo), capture_output=True,
                                text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CloseError(f"ngit {' '.join(args)} could not run: {exc}") from exc
    if result.returncode != 0:
        why = (result.stderr or result.stdout or "").strip() or "no output"
        raise CloseError(f"ngit {' '.join(args)} failed: {why}")
    return result.stdout


def _json(repo: Path, *args: str) -> Any:
    text = _ngit(repo, *args)
    try:
        return json.loads(text)
    except ValueError as exc:
        raise CloseError(f"ngit {' '.join(args)} printed no JSON") from exc


def branch_base(branch: str) -> str:
    """The branch without ngit's `(<8hex>)` proposal suffix.

    `ngit pr view` names the branch `pr/<name>(<short-id>)`; two proposals for one branch
    differ only in that suffix, which is the whole signal a duplicate is matched on.
    """
    return _BRANCH_SUFFIX.sub("", str(branch or "")).strip()


def pr_detail(repo: Path, ref: str) -> dict[str, Any]:
    """One proposal as `ngit pr view <ref> --json` reports it."""
    detail = _json(repo, "pr", "view", str(ref), "--json")
    if not isinstance(detail, dict):
        raise CloseError(f"ngit pr view {ref} did not describe a proposal")
    return detail


def tip_of(detail: dict[str, Any]) -> str:
    """The commit a proposal carries, from the CI run ngit recorded for its tip.

    Empty when ngit named none -- a proposal that never ran CI. The caller then has to pass
    the tip, because a close that says "tip ?" names nothing a reader can resolve.
    """
    ci = detail.get("ci") or {}
    for run in ci.get("runs") or ():
        if isinstance(run, dict) and str(run.get("commit") or ""):
            return str(run["commit"])
    return ""


def _successor_of(item: dict[str, Any], detail: dict[str, Any]) -> Successor:
    ident = event_hex(str(item.get("id") or "")) or event_hex(str(detail.get("id") or ""))
    return Successor(
        id=ident,
        short=ident[:8],
        nevent=str(item.get("id") or detail.get("id") or ""),
        tip=tip_of(detail),
        subject=str(detail.get("subject") or item.get("subject") or ""),
    )


def successors(repo: Path, ref: str) -> list[Successor]:
    """Every other open proposal that shares this proposal's branch.

    Read-only. A branch normally has one open proposal, so the answer is normally one
    entry; the list is kept because a third proposal is possible and naming only one of
    them would be a guess.
    """
    mine = pr_detail(repo, ref)
    base = branch_base(str(mine.get("branch") or ""))
    if not base:
        raise CloseError(f"ngit pr view {ref} named no branch, so no successor can be found")
    my_id = event_hex(str(mine.get("id") or "")) or event_hex(str(ref))
    found: list[Successor] = []
    for item in _json(repo, "pr", "list", "--json") or ():
        if not isinstance(item, dict):
            continue
        if branch_base(str(item.get("branch") or "")) != base:
            continue
        ident = event_hex(str(item.get("id") or ""))
        if ident and my_id and ident == my_id:
            continue
        detail = pr_detail(repo, str(item.get("id") or ""))
        found.append(_successor_of(item, detail))
    return found


# -- the reason and the close --------------------------------------------------

def close_reason(successor: Successor, why: str = "") -> str:
    """The reason a close must carry: the successor's id and tip, then what happened.

    This is the sibling close's wording, kept because it is the shape a reader already
    recognises: `Superseded by 7cf03694 (tip 8a3053a5): a re-push ...`.
    """
    if not successor.label:
        raise CloseError("a close reason needs a successor id")
    if not successor.tip:
        raise CloseError("a close reason needs the successor's tip")
    text = (why or DEFAULT_WHY).strip().rstrip(".")
    return f"Superseded by {successor.label} (tip {successor.tip}): {text}."


def close(repo: Path, ref: str, successor_ref: str, *, why: str = "", tip: str = "",
          run=_ngit) -> tuple[Successor, str]:
    """Close one duplicate, naming the successor and its tip; verify the read-back.

    Refuses before publishing when the duplicate is not open, when the successor is not an
    open proposal, or when the successor's tip cannot be named. Returns the successor and
    the reason that was published.
    """
    duplicate = pr_detail(repo, ref)
    status = str(duplicate.get("status") or "").strip().lower()
    if status not in OPEN:
        raise CloseRefused(f"PR {ref} is {status or 'in an unknown state'}, not open:"
                           " a close is for a proposal that stopped without landing")
    detail = pr_detail(repo, successor_ref)
    successor = _successor_of({"id": successor_ref}, detail)
    if str(detail.get("status") or "").strip().lower() not in OPEN:
        raise CloseRefused(f"successor {successor.label} is {detail.get('status') or 'unknown'},"
                           " not an open proposal: a close must point at the one that survived")
    if tip:
        successor = dataclasses.replace(successor, tip=str(tip).strip())
    if not successor.tip:
        raise CloseRefused(f"successor {successor.label} names no tip in ngit's record;"
                           " pass --tip <full sha> so the close names one")
    reason = close_reason(successor, why)
    run(repo, "pr", "close", str(ref), "--reason", reason)
    after = pr_detail(repo, ref)
    if str(after.get("status") or "").strip().lower() != "closed":
        raise CloseError(f"the close was published but {ref} still reads"
                         f" {after.get('status') or 'unknown'}; read it back before trusting it")
    return successor, reason


# -- the command ---------------------------------------------------------------

def _repo(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_dir():
        raise CloseError(f"{value!r} is not a directory")
    return path


def _print_successors(ref: str, found: Sequence[Successor]) -> None:
    """The lines the merge route prints: the successor, its tip, and the close command."""
    for successor in found:
        subject = f' -- "{successor.subject}"' if successor.subject else ""
        if successor.tip:
            print(f"other open proposal on this branch: {successor.label}"
                  f" at tip {successor.tip}{subject}")
            print(f"  ngit pr close {ref} --reason '{close_reason(successor)}'")
        else:
            # The close still has to name a tip; ngit has none, so say which one to pass.
            print(f"other open proposal on this branch: {successor.label}"
                  f" (ngit names no tip){subject}")
            print(f"  ngit pr close {ref} --reason 'Superseded by {successor.label}"
                  f" (tip <sha>): {DEFAULT_WHY}.'  # pass --tip <sha>")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ngit_close",
        description="Close a duplicate proposal with a reason that names its successor.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_succ = sub.add_parser("successors", help="the other open proposals on this branch")
    p_succ.add_argument("pr", help="the duplicate: event id, prefix or nevent")
    p_succ.add_argument("--repo", default=".", help="checkout to read (default: .)")

    p_close = sub.add_parser("close", help="close a duplicate, naming its successor")
    p_close.add_argument("pr", help="the duplicate: event id, prefix or nevent")
    p_close.add_argument("--successor", required=True,
                         help="the open proposal that replaced it")
    p_close.add_argument("--reason", default="",
                         help="what happened, after the successor id and tip")
    p_close.add_argument("--tip", default="",
                         help="the successor's tip, when ngit names none")
    p_close.add_argument("--repo", default=".", help="checkout to read (default: .)")

    args = parser.parse_args(argv)
    try:
        repo = _repo(args.repo)
        if args.cmd == "successors":
            found = successors(repo, args.pr)
            if not found:
                print(f"no other open proposal shares {args.pr}'s branch;"
                      " name the successor in the reason anyway:")
                print(f"  ngit pr close {args.pr} --reason 'Superseded by <id> (tip <sha>): <why>'")
                return EXIT_OK
            _print_successors(args.pr, found)
            return EXIT_OK
        successor, reason = close(repo, args.pr, args.successor, why=args.reason,
                                  tip=args.tip)
        print(f"closed {args.pr} | {reason} | read back: closed")
        return EXIT_OK
    except CloseRefused as exc:
        print(f"ngit close: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except CloseError as exc:
        print(f"ngit close: {exc}", file=sys.stderr)
        return EXIT_UNREADABLE


if __name__ == "__main__":  # pragma: no cover - the shell route's entry point
    raise SystemExit(main())
