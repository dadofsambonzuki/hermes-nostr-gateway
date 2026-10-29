"""The spellings a sender may put in front of a message: `/queue`/`/q`, `/guide`/`/steer`/`/s`.

A message that lands while the agent is mid-turn used to have one meaning — another
instruction, arriving when the agent was least able to take it. These prefixes let the
sender say what they mean instead:

    /queue <text>, /q <text>                 hold it until the current turn finishes
    /guide <text>, /steer <text>, /s <text>  steer it into the turn that is already running

An alias is the same command under another spelling, never a second implementation: `/q`
resolves to `queue`, `/steer` and `/s` resolve to `guide`, and nothing downstream of this
module can tell which spelling was typed. The short forms are what the issue asked for
(`/queue` `/q` and `/steer` `/s`); `/guide` predates them and stays as the name of the mode.

The command is a *routing* decision, not content. It is read here, before the message is
stored, and stripped from the text before the frame is built; the row keeps `mode` so a
replay cannot lose it and the adapter never re-parses what a person wrote.

It is honoured only for a sender the policy has already judged — `authority: instructions`
*and* `reach: immediate` — which is why the two halves are separate calls: `split` says what
the sender asked for, the policy decides, and `resolve` applies it. A stranger's
`/queue ...` is stored exactly as typed and reaches the agent as data; it can neither queue
nor steer anything, because the prefix is read before the decision and applied after it.

The names are this gateway's, not Hermes': Hermes spells the same two behaviours `/queue`
and `/steer`, two commands with their own mid-run handlers — "hold for the next turn" and
"inject after the next tool call" — while `display.busy_input_mode` sets what a *plain*
message does when the agent is busy. The adapter maps one onto the other, so the hold and the
steer are Hermes' own implementation rather than a second queue kept here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .policy import Decision

#: What a stored row or a frame says when the sender asked for nothing in particular.
MODE_NORMAL = "normal"
#: Hold until the current turn ends, then deliver as its own turn.
MODE_QUEUE = "queue"
#: Steer into the turn that is already running.
MODE_GUIDE = "guide"

#: Every mode, for a caller that validates rather than trusts (a hand-edited store, config).
MODES: tuple[str, ...] = (MODE_NORMAL, MODE_QUEUE, MODE_GUIDE)

#: The whole grammar: a leading command, one whitespace run, then words. Five spellings, two
#: behaviours: an alias shares its long form's mode, so `/q` is `queue` and `/steer` and `/s`
#: are `guide` — the regex recognises the word and `_MODE_OF` names the behaviour. Anything
#: else is text the sender typed — `/queue` alone (nothing to hold), `/queuex`, `/Queue` (a
#: sentence opening with a path), or a command mentioned mid-sentence, which is not a command.
_COMMAND = re.compile(r"^/(queue|q|guide|steer|s)\s+(\S.*)$", re.DOTALL)
_MODE_OF = {
    "queue": MODE_QUEUE, "q": MODE_QUEUE,
    "guide": MODE_GUIDE, "steer": MODE_GUIDE, "s": MODE_GUIDE,
}


def split(text: str) -> tuple[str, str]:
    """`(mode, words)` — the mode the message asks for, and the words without its command.

    Deliberately forgiving about leading whitespace and nothing else: the command must be the
    first thing in the message, in lowercase, and must be followed by words. The rest keeps
    its own formatting, newlines included, because it is somebody's message.
    """
    body = (text or "").lstrip()
    match = _COMMAND.match(body)
    if match is None:
        return MODE_NORMAL, text or ""
    return _MODE_OF[match.group(1)], match.group(2)


def permitted(asked: str, decision: Decision) -> str:
    """The mode the policy allows: a prefix directs nothing unless the sender may instruct now.

    Two conditions, both the policy's rather than this module's. `authority: instructions`
    because a stranger's text is data and `/guide` in it is a word, not a verb. `reach:
    immediate` because a digest row is delivered as a batch after the turn it would have
    steered has ended — so a loop-tier sender's `/queue` stays a digest row, with its text as
    typed, rather than becoming an immediate turn on the way out of the batch.
    """
    if asked == MODE_NORMAL:
        return MODE_NORMAL
    if decision.authority != "instructions" or decision.reach != "immediate":
        return MODE_NORMAL
    return asked


def resolve(text: str, decision: Decision) -> tuple[str, str]:
    """`(mode, content)` to store and deliver: the command is stripped only when honoured.

    The prefix is read again here rather than taken back from the caller, so a plane cannot
    hand this a mode the policy never saw. An unhonoured prefix leaves the text untouched, so
    the row and the agent see what the sender actually wrote — the honest record of a request
    that was not granted.
    """
    asked, words = split(text)
    mode = permitted(asked, decision)
    if mode == MODE_NORMAL:
        return MODE_NORMAL, text or ""
    return mode, words
