"""What a relay says when it refuses part of a filter, and the handler shapes we pass.

The read transport itself is `pool.py`: one `nostr-sdk` client, one subscription per
filter set, NIP-42 answered in process. This module is what is left of the subprocess
transport — the part that was never about I/O.

A filter is all-or-nothing at the relay: a REQ naming a kind the relay will not serve is
closed whole (`kind not allowed: 4`), so one refused kind used to take a relay's 1059 DMs
with it. The recovery is to read the refusal, drop that kind and resubscribe on what is
left, and the parsing that makes it possible is the one piece worth keeping in its own
module: it is shared by every worker and is testable without a socket.
"""

from __future__ import annotations

import re
from typing import Awaitable, Callable

#: Called with `(relay, event)` for each event a read worker delivers. Async handlers are
#: awaited; a sync one is called and forgotten, which is what a plain print wants.
Handler = Callable[[str, dict], Awaitable[None] | None]
#: Called when a relay refuses a kind: (relay, dropped, what we now ask for, raw error).
RefusalHandler = Callable[[str, "set[int]", tuple[int, ...], str | None], None]

#: `CLOSED: ... kind not allowed: 4` (whitenoise), `kind 4 not allowed`, and the plural
#: form. Only kind-specific refusals are acted on: dropping a kind changes what we read,
#: so nothing else about a CLOSED frame is guessed at.
_KIND_REFUSED = re.compile(
    r"kinds?\s+(?:not\s+allowed\s*:\s*([0-9][0-9, ]*)|([0-9]+)\s+not\s+allowed)",
    re.IGNORECASE,
)


def refused_kinds(text: str | None) -> set[int]:
    """The kinds a relay's CLOSED/NOTICE named as not allowed."""
    if not text:
        return set()
    found: set[int] = set()
    for match in _KIND_REFUSED.finditer(text):
        chunk = match.group(1) or match.group(2) or ""
        found.update(int(number) for number in re.findall(r"\d+", chunk))
    return found
