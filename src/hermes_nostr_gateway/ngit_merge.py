"""A merge requires the card in `review` or `done`.

`ngit_guard.py` answers one direction of the authorship rule: a card that reviews a PR cannot be
completed while the PR is open. This module is the merge route's half of it -- a PR cannot land
while the card it serves is still work in progress. Neither invents a status: the card's
`status` is the board's, and which card it is comes from the bridge's own `ngit_links` row for
the PR root, so the same two tables answer both questions.

A PR names its card in three places, and each is read for what it is:

* **the link.** `ngit_links` (kind 1618, the PR root) carries the card the bridge made for the
  PR -- for this repository the review card. It is the PR's card, and it is checked even when
  the commits say something else.
* **the trailers.** Every commit a worker pushes carries `Card: t_xxxxxxxx`, written by the
  `commit-msg` hook in `scripts/card_trailer.sh`, so a card named there is the card the work
  was done under and it has to be finished too. `scripts/merge_pr.sh` refuses a PR whose
  commits carry no trailer at all before it asks this module anything.
* **`Card: operator`.** The explicit way to say a change is nobody's card. It is not a bypass:
  a PR the bridge has linked still has to satisfy its linked card, so operator work cannot land
  a worker's PR early.

`review` and `done` are both allowed, because both mean "nobody is still working on it".
`review` is a reviewer's turn; `done` is what the bridge writes when it sees the merge, but a
card can also be finished before its PR lands and refusing that would make the rule about the
board rather than about the work.

`blocked` is the one status that means two different things, and the two are told apart by which
card it is. **The PR's own card** may be blocked: the review brief's last instruction is *park
the card with `kanban_block`*, because `done` requires applied and the reviewer may not complete
it. Refusing that would refuse every PR that went through the review lane -- the card cannot be
`done` before the merge and could never be merged at all. The person a blocked card waits for is
exactly the person who merges. **A work card** named by a trailer may not be blocked: a block
there is the work saying it is not finished, and a merge does not overrule it.

Everything else -- `running`, `ready`, `todo`, `scheduled`, `triage`, `archived` -- is a refusal
that names the card and the status it has.

Exit codes are the merge script's: 0 allowed, 3 could not read (no config, no board, an
unreadable store), 7 refused, so a caller can tell "wait for the card" from "the box is wrong".
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .kanban import Board, BoardError
from .ngit import event_hex
from .projects import ProjectMap, board_dir, hermes_home

#: The PR root's kind: the event `ngit_links` keys a PR by.
KIND_PR = 1618

#: Statuses a merge may land: the reviewer's turn, or finished. See the module docstring.
FINISHED = ("review", "done")

#: A card parked for a person. Allowed for the PR's own card only -- that is where the review
#: brief puts a reviewed PR, and the person it waits for is the one merging. A *work* card in
#: this state is work that stopped, and a merge does not overrule it.
PARKED = "blocked"

#: The trailer value that says "this is the operator's work, not a card's".
OPERATOR = "operator"

EXIT_OK = 0
EXIT_UNREADABLE = 3
EXIT_REFUSED = 7

#: Hex is all a stored event id is, so a short id can only be matched as a prefix.
_HEX = set("0123456789abcdef")


@dataclass(frozen=True)
class Card:
    """One card a PR names, and the status the board actually holds for it."""

    task_id: str
    source: str              # "link" (this PR's card) or "trailer" (named by a commit)
    status: str = ""
    title: str = ""

    @property
    def finished(self) -> bool:
        return self.status in FINISHED

    def as_dict(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "source": self.source, "status": self.status,
                "title": self.title, "finished": self.finished}


@dataclass(frozen=True)
class Verdict:
    """What the merge route asked, and the answer a caller can print verbatim."""

    ok: bool
    why: str = ""
    unreadable: bool = False
    pr: str = ""
    board: str = ""
    cards: tuple[Card, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "why": self.why, "unreadable": self.unreadable,
                "pr": self.pr, "board": self.board,
                "cards": [card.as_dict() for card in self.cards]}


def short(value: str) -> str:
    """The first 8 characters, which is how every message in this repository names a PR."""
    return str(value or "")[:8] or "unknown"


def hex_of(pr: str) -> str:
    """The PR root as hex when the caller gave an nevent or the whole id, else empty."""
    return event_hex(pr)


def prefix_of(pr: str) -> str:
    """The short hex id `ngit pr list` prints, when that is what was given; else empty."""
    text = str(pr or "").strip().lower()
    if len(text) >= 4 and set(text) <= _HEX:
        return text
    return ""


def open_store(path: Path) -> Optional[sqlite3.Connection]:
    """The bridge store, read-only. None when the bridge has never run on this box.

    A missing store is not an error: it means the bridge has never seen a PR, and a PR the
    bridge does not know has no linked card to check. A *config* that cannot be read is an
    error, and `run` says so before it gets here.
    """
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def find_link(conn: Optional[sqlite3.Connection], pr: str) -> Optional[dict[str, Any]]:
    """The bridge's row for this PR root: exact for a full id, a prefix for a short one."""
    if conn is None:
        return None
    full, short_id = hex_of(pr), prefix_of(pr)
    try:
        if full:
            row = conn.execute(
                "SELECT * FROM ngit_links WHERE kind = ? AND source_id = ?"
                " ORDER BY updated_at DESC LIMIT 1", (KIND_PR, full),
            ).fetchone()
            if row is not None:
                return dict(row)
        if short_id:
            row = conn.execute(
                "SELECT * FROM ngit_links WHERE kind = ? AND source_id LIKE ?"
                " ORDER BY updated_at DESC LIMIT 1", (KIND_PR, short_id + "%"),
            ).fetchone()
            if row is not None:
                return dict(row)
    except sqlite3.Error:
        return None
    return None


def mapped_board(config: Mapping[str, Any], repo: str) -> str:
    """The board a checkout's work is filed on, from `concord.projects` (matched by path).

    A repository's cards live on one board, and that entry is the mapping the rest of the
    bridge already trusts. It is the fallback for a PR the bridge has not linked yet, so a
    block of operator work under a card is checked rather than skipped.
    """
    if not repo:
        return ""
    try:
        wanted = Path(str(repo)).expanduser().resolve()
    except OSError:
        return ""
    for project in ProjectMap.from_config((config or {}).get("concord")).all():
        if not project.board or project.problem or project.config_error:
            continue
        try:
            if project.path.expanduser().resolve() == wanted:
                return project.board
        except OSError:
            continue
    return ""


def board_path(slug: str, *, home: Optional[str | Path] = None) -> Path:
    """One board's database, in the layout Hermes writes (`<home>/kanban/boards/<slug>`)."""
    return board_dir(hermes_home(home), slug) / "kanban.db"


def read_cards(slug: str, task_ids: Sequence[str],
               *, home: Optional[str | Path] = None) -> tuple[list[Card], str]:
    """The board's rows for these cards, and a reason when the board cannot be read."""
    path = board_path(slug, home=home)
    try:
        with Board(path) as board:
            rows = [board.task(str(task_id)) for task_id in task_ids]
    except BoardError as exc:
        return [], str(exc)
    except sqlite3.Error as exc:
        return [], f"{path}: {exc}"
    cards: list[Card] = []
    for row in rows:
        if row is None:
            continue
        cards.append(Card(task_id=str(row.get("id") or ""), source="",
                          status=str(row.get("status") or ""),
                          title=str(row.get("title") or "")))
    return cards, ""


def check(pr: str, cards: Sequence[str] = (), *, config: Mapping[str, Any],
          repo: str = "", board: str = "", store: Optional[Path] = None,
          home: Optional[str | Path] = None,
          store_conn: Optional[sqlite3.Connection] = None) -> Verdict:
    """Would a merge of this PR land work a card is still doing? The whole rule, in one call.

    `cards` is what the commits said (`Card:` trailers); `operator` is ignored here because it
    names no card. `store_conn` exists so a caller can reuse the connection; by default the
    store is opened from the config's `store:` path.

    The verdict is not asked about here. Whether an approval permits the merge -- who signed
    it, and which tip it covers -- is `ngit_verdict`'s question, answered from the PR's own
    record; the link row is a cache of that record and must never be a second opinion.
    """
    closed = False
    conn = store_conn
    if conn is None:
        where = Path(store).expanduser() if store is not None else None
        if where is None and str((config or {}).get("store") or ""):
            where = Path(str(config["store"])).expanduser()
        if where is not None:
            conn = open_store(where)
            closed = conn is not None
    try:
        link = find_link(conn, pr)
    finally:
        if closed and conn is not None:
            conn.close()

    slug = str(board or "").strip() or str((link or {}).get("board") or "") or \
        mapped_board(config, repo)
    wanted: list[tuple[str, str]] = []
    if link and link.get("task_id"):
        wanted.append((str(link["task_id"]), "link"))
    for value in cards:
        task_id = str(value or "").strip()
        if not task_id or task_id == OPERATOR:
            continue
        if all(task_id != seen for seen, _ in wanted):
            wanted.append((task_id, "trailer"))
    if not wanted:
        return Verdict(True, pr=str(pr), board=slug)
    if not slug:
        return Verdict(
            False, unreadable=True, pr=str(pr),
            why=("no board is mapped to this checkout and the PR is not linked in the bridge "
                 "store, so its cards cannot be read: set a `concord.projects` entry for the "
                 "checkout, or pass --board"))

    found, problem = read_cards(slug, [task_id for task_id, _ in wanted], home=home)
    if problem:
        return Verdict(False, unreadable=True, pr=str(pr), board=slug, why=problem)
    by_id = {card.task_id: card for card in found}
    states: list[Card] = []
    refusals: list[str] = []
    for task_id, source in wanted:
        card = by_id.get(task_id)
        if card is None:
            states.append(Card(task_id=task_id, source=source, status="missing"))
            refusals.append(f"{task_id} ({_source(source)}) is not on board {slug}")
            continue
        states.append(Card(task_id=card.task_id, source=source, status=card.status,
                           title=card.title))
        allowed = FINISHED if source == "trailer" else FINISHED + (PARKED,)
        if card.status not in allowed:
            refusals.append(f"{task_id} ({_source(source)}) is {card.status or 'unknown'}")
    if refusals:
        return Verdict(False, pr=str(pr), board=slug, cards=tuple(states),
                       why="; ".join(refusals))
    return Verdict(True, pr=str(pr), board=slug, cards=tuple(states))


def _source(source: str) -> str:
    return "this PR's card" if source == "link" else "named by a commit"


def summarise(verdict: Verdict) -> tuple[str, str]:
    """The one line a route prints on success, and the refusal when it does not."""
    cards = ", ".join(f"{card.task_id} {card.status or 'unknown'}"
                      for card in verdict.cards if card.task_id)
    if verdict.ok:
        return (f"OK   {short(verdict.pr)} {cards or 'operator work, no card'} -- "
                "a merge may land"), ""
    if verdict.unreadable:
        return "", f"FAIL {verdict.why}"
    if not verdict.cards:
        return "", f"REFUSED: {verdict.why}"
    return "", (f"REFUSED: {verdict.why}\n"
                "  A merge also requires the card in review or done: a card still running is\n"
                "  work in progress; ask its worker to finish and a reviewer to decide.\n"
                "  `Card: operator` names operator work, and does not release a card the\n"
                "  bridge linked to this PR.")


def run(config: Mapping[str, Any], pr: str, *, cards: Sequence[str] = (), repo: str = "",
        board: str = "", as_json: bool = False,
        home: Optional[str | Path] = None) -> int:
    """The CLI's route: read the config's store, check the rule, print it, return the code."""
    store_value = str((config or {}).get("store") or "")
    if not store_value:
        print("FAIL no `store:` in the config: the bridge's links cannot be read", file=sys.stderr)
        return EXIT_UNREADABLE
    try:
        verdict = check(pr, cards, config=config, repo=repo, board=board,
                        store=Path(store_value).expanduser(), home=home)
    except OSError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    if as_json:
        print(json.dumps(verdict.as_dict(), indent=2, sort_keys=True))
    else:
        out, err = summarise(verdict)
        if out:
            print(out)
        if err:
            print(err, file=sys.stderr)
    if verdict.ok:
        return EXIT_OK
    return EXIT_UNREADABLE if verdict.unreadable else EXIT_REFUSED


def main(argv: Optional[Sequence[str]] = None) -> int:
    """`python -m hermes_nostr_gateway.ngit_merge <pr>` -- what `merge_pr.sh` calls."""
    parser = argparse.ArgumentParser(
        prog="python -m hermes_nostr_gateway.ngit_merge",
        description="Check that a PR's card is in review or done, so a merge may land.")
    parser.add_argument("pr", help="the PR's event id, nevent, or the short id `ngit` prints")
    parser.add_argument("--card", action="append", default=[],
                        help="a card named by a commit trailer (repeatable)")
    parser.add_argument("--repo", default="", help="the checkout, to resolve its board")
    parser.add_argument("--board", default="", help="board slug, when it cannot be derived")
    parser.add_argument("--json", action="store_true", help="machine-readable answer")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from .cli import load_config  # lazy: the CLI imports this module for its subcommand

    try:
        config = load_config(None)
    except SystemExit as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return EXIT_UNREADABLE
    return run(config, args.pr, cards=args.card, repo=args.repo, board=args.board,
               as_json=args.json)


if __name__ == "__main__":  # pragma: no cover - the shell route's entry point
    raise SystemExit(main())
