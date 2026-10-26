#!/usr/bin/env python3
"""Decide whether the 20-minute queue worker has anything to do — before an agent is spent.

The cron job "Gateway queue worker" is prompt-only today: every tick starts a model run, and a run
that finds nothing to do still pays for a model call and still reaches for tools the unattended
approval guard refuses, so an empty queue looks like a broken job in its own log.

This module is that job's *monitor source*. `hermes cron` runs it before the agent and suppresses
the agent run entirely while its output is byte-identical to the last agent-triggering tick, so an
empty (or fully parked) queue costs one script execution and nothing else. Output is deliberately
stable — no timestamps, no clock seconds, no set ordering — and changes exactly when the queue
changes in a way the worker should see, including when a new item is added or the box is ticked.

    next: <first unchecked, non-BLOCKED item>
    pending: <unchecked> unchecked, <blocked> blocked
    waited: <whole hours since QUEUE.md last changed>

With nothing to work it prints exactly one line and exits 0 — "nothing to do" is not a failure and
must not alert:

    queue empty — nothing pending

`waited:` is the retry valve. A run that dies without touching QUEUE.md leaves its box unchecked
and this script's output unchanged, which would park that item until someone edits the queue; the
hour bucket advances on its own, so a stranded item is retried at most once an hour instead of
never. It is omitted from the output when the caller supplies no mtime (unit tests).

Exit status: 0 when the state was rendered (empty or not), 2 when the queue is unreadable or holds
no entries at all — a real failure, which the monitor reports as an error and never as a change.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

EMPTY_LINE = "queue empty — nothing pending"

_ITEM = re.compile(r"^- \[([ xX])\] (.*)$")
_BLOCKED = re.compile(r"\bBLOCKED\b")
_DASHES = (" \u2014 ", " \u2013 ", " - ")


@dataclass(frozen=True)
class Item:
    """One queue entry: its headline, its checkbox, and whether it is parked on a decision."""

    title: str
    checked: bool
    blocked: bool


def _title(headline: str) -> str:
    """The item's name: the bold `**…**` opening if there is one, else the text before the dash."""
    text = headline.strip()
    if text.startswith("**"):
        end = text.find("**", 2)
        if end > 2:
            return " ".join(text[2:end].split())
    for dash in _DASHES:
        if dash in text:
            text = text.split(dash, 1)[0]
            break
    return " ".join(text.split())


def parse_items(text: str) -> list[Item]:
    """Every `- [ ]`/`- [x]` entry in file order, with BLOCKED read from the entry's own block.

    An entry's block runs to the next entry, and continuation lines are indented: an unindented
    line that is not a new entry ends the block, so the rules paragraph under the list can mention
    BLOCKED without marking the last item above it (there is a test for exactly that), and a
    `<!-- … -->` pointer keeps its place.
    """
    items: list[Item] = []
    current: tuple[str, bool] | None = None
    block: list[str] = []

    def flush() -> None:
        if current is None:
            return
        title, checked = current
        body = "\n".join(block)
        items.append(Item(title=title, checked=checked, blocked=bool(_BLOCKED.search(body))))

    for line in text.splitlines():
        match = _ITEM.match(line)
        if match:
            flush()
            current, block = (_title(match.group(2)), match.group(1) != " "), []
            continue
        if current is None:
            continue
        if line.strip() and not line[:1].isspace() and not line.startswith("<!--"):
            flush()
            current, block = None, []
            continue
        block.append(line)
    flush()
    return items


def _waited_hours(now: float | None, mtime: float | None) -> int | None:
    """Whole hours since the queue last changed — the bucket that retries a stranded item."""
    if now is None or mtime is None:
        return None
    return max(0, int((now - mtime) // 3600))


def render(text: str, *, now: float | None = None, mtime: float | None = None) -> str:
    """The monitor line(s) for this queue text: one line when idle, a framed block when not."""
    unchecked = [item for item in parse_items(text) if not item.checked]
    actionable = [item for item in unchecked if not item.blocked]
    blocked = len(unchecked) - len(actionable)
    if not actionable:
        if blocked:
            return f"queue empty — {blocked} blocked, waiting on Nathan"
        return EMPTY_LINE
    lines = [
        f"next: {actionable[0].title}",
        f"pending: {len(unchecked)} unchecked, {blocked} blocked",
    ]
    waited = _waited_hours(now, mtime)
    if waited is not None:
        lines.append(f"waited: {waited}h")
    return "\n".join(lines)


def state(text: str, *, now: float | None = None, mtime: float | None = None) -> dict:
    """The same facts as `render`, for a caller that would rather parse than read."""
    unchecked = [item for item in parse_items(text) if not item.checked]
    actionable = [item for item in unchecked if not item.blocked]
    return {
        "next": actionable[0].title if actionable else None,
        "unchecked": len(unchecked),
        "blocked": len(unchecked) - len(actionable),
        "waited_hours": _waited_hours(now, mtime),
        "empty": not actionable,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render the gateway queue worker's monitor line for QUEUE.md.",
    )
    parser.add_argument(
        "--queue",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "QUEUE.md",
        help="queue file to read (default: this checkout's QUEUE.md)",
    )
    parser.add_argument("--json", action="store_true", help="print the state as JSON, not text")
    args = parser.parse_args(argv)

    try:
        text = args.queue.read_text(encoding="utf-8")
        mtime = args.queue.stat().st_mtime
    except OSError as exc:
        print(f"queue-gate: cannot read {args.queue}: {exc}", file=sys.stderr)
        return 2
    if not parse_items(text) and text.strip():
        # A queue with prose but no entries is damaged, not empty: fail so the monitor alerts.
        print(f"queue-gate: no queue entries found in {args.queue}", file=sys.stderr)
        return 2

    now = time.time()
    if args.json:
        print(json.dumps(state(text, now=now, mtime=mtime), sort_keys=True))
    else:
        print(render(text, now=now, mtime=mtime))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
