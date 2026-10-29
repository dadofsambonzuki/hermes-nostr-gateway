"""Two words a sender may put in front of a message: `/queue` and `/steer`.

A message that lands while the agent is mid-turn used to have one meaning — another
instruction, arriving when the agent was least able to take it. These prefixes let the
sender say what they mean instead:

    /queue <text>, /q <text>   hold it until the current turn finishes, then run it as its own turn
    /steer <text>, /s <text>   steer it into the turn that is already running, not a new request

The words are Hermes', not this gateway's. Hermes' own command table spells the same two
behaviours `/queue` (`/q`) and `/steer` (`/s`), each with a mid-run handler — "hold for the next
turn" and "inject after the next tool call" — while `display.busy_input_mode` sets what a
*plain* message does when the agent is busy. An agent and a person should not have to learn two
vocabularies for one idea, so the prefixes reuse the names and the short forms, and the adapter
hands the behaviour to Hermes rather than keeping a second queue here.

`/guide` was this gateway's own coinage for steering, invented before anyone checked the host's
vocabulary. It is retired: still read as a deprecated alias, so a message written before the
rename is not silently delivered as text, but never presented as the name. There is no `/g` —
the short form of `/steer` is `/s`, because that is Hermes' own short form.

The command is a *routing* decision, not content. It is read here, before the message is
stored, and stripped from the text before the frame is built; the row keeps `mode` so a
replay cannot lose it and the adapter never re-parses what a person wrote.

It is honoured only for a sender the policy has already judged — `authority: instructions`
*and* `reach: immediate` — which is why the two halves are separate calls: `split` says what
the sender asked for, the policy decides, and `resolve` applies it. A stranger's
`/queue ...` is stored exactly as typed and reaches the agent as data; it can neither queue
nor steer anything, because the prefix is read before the decision and applied after it.
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
MODE_STEER = "steer"

#: The value `/guide` used to name, and the one rows written before the rename carry.
LEGACY_MODE_GUIDE = "guide"

#: Every mode, for a caller that validates rather than trusts (a hand-edited store, config).
#: `guide` is not one of them: it was a spelling of `steer`, never a third behaviour.
MODES: tuple[str, ...] = (MODE_NORMAL, MODE_QUEUE, MODE_STEER)


def canonical(mode: str | None) -> str:
    """`mode` in today's words: the retired `guide` reads as `steer`, absent reads as `normal`.

    A row written before the rename keeps its old value, and a daemon a release behind can
    still put it on a frame. Normalising at that boundary is what keeps a replayed steer a
    steer instead of an unknown mode the adapter would deliver as ordinary text.
    """
    if not mode:
        return MODE_NORMAL
    return MODE_STEER if mode == LEGACY_MODE_GUIDE else mode


#: The whole grammar: a leading command, one whitespace run, then words. Two behaviours, four
#: current spellings — `/q` shares `/queue`'s mode and `/s` shares `/steer`'s — plus the retired
#: `/guide`, an entry in the same table rather than a second parser, so no alias can drift from
#: its word's gate or semantics. Anything else is text the sender typed: `/steer` alone
#: (nothing to steer), `/steerx`, `/Steer` (a sentence opening with a path), `/g` (not a word
#: here — Hermes' short form is `/s`), or a command mentioned mid-sentence.
_COMMAND = re.compile(r"^/(queue|q|steer|s|guide)\s+(\S.*)$", re.DOTALL)
_MODE_OF = {
    "queue": MODE_QUEUE, "q": MODE_QUEUE,
    "steer": MODE_STEER, "s": MODE_STEER,
    "guide": MODE_STEER,  # retired spelling, kept readable for a release; never the name
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
    because a stranger's text is data and `/steer` in it is a word, not a verb. `reach:
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
