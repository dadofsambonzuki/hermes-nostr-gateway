"""Relay I/O: one worker per relay, so coverage is visible per relay.

Reading [kinds: 1059, #p: me] from the identity's 10050 alone is not enough: DMs exist
only on the sender's app relays (ditto, dreamith, whitenoise) and the gap is silent.
So: N workers, per-relay counters, and a relay that stops delivering is reported rather
than assumed healthy.

A filter is all-or-nothing at the relay: a REQ naming a kind the relay refuses is closed
whole (`kind not allowed: 4`), so one refused kind used to take a relay's 1059 DMs with
it. One connection per relay is still right (connection limits), so the kind set is
negotiated instead: read the refusal, drop that kind, resubscribe at once.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from typing import Awaitable, Callable

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



class RelayWorker:
    """A `nak req` subscription for one relay, restarted with backoff if it dies."""

    def __init__(self, relay: str, me: str, *, kinds: tuple[int, ...] = (1059,),
                 authors: tuple[str, ...] = (), p_tag: str | None = None, auth: bool = True,
                 secret: str | None = None, since_days: int = 3, nak: str | None = None,
                 on_refusal: RefusalHandler | None = None):
        self.relay = relay
        # Requested vs refused vs active: config (or the caller) says what we want,
        # the relay says what it accepts, and `kinds` is what the next REQ carries.
        self.requested_kinds = tuple(dict.fromkeys(int(kind) for kind in kinds))
        if not self.requested_kinds:
            raise ValueError("RelayWorker needs at least one kind: an empty filter is a firehose")
        self.refused: set[int] = set()
        self.on_refusal = on_refusal
        self.authors = authors
        self.p_tag = p_tag
        self.auth = auth
        # NIP-42 auth must sign as OUR identity. Without this the relay authenticates us
        # as nak's machine-default key, which is not in the web of trust, and closes the
        # subscription with "restricted".
        self.secret = secret
        self.since_days = since_days
        self.me = (me or "").strip()
        # An empty identity must be a hard error, never an empty filter: with no `-p`
        # value the subscription silently becomes a firehose of every wrap on the relay.
        if len(self.me) != 64:
            raise ValueError(
                "RelayWorker needs a 64-hex identity; refusing an unfiltered subscription")
        self.nak = nak or shutil.which("nak") or "nak"
        self.events = 0
        self.last_error: str | None = None
        self._stop = asyncio.Event()

    @property
    def kinds(self) -> tuple[int, ...]:
        """What the subscription asks for now: requested minus what the relay refused."""
        return tuple(kind for kind in self.requested_kinds if kind not in self.refused)

    def note_refusals(self, text: str | None) -> set[int]:
        """Learn from a relay's CLOSED text. Returns the kinds newly dropped.

        Only kinds we actually asked for count: a relay complaining about some other
        filter must not quietly narrow ours. The caller decides whether to resubscribe
        or give up when nothing is left.
        """
        dropped = refused_kinds(text) & set(self.requested_kinds) - self.refused
        if dropped:
            self.refused |= dropped
            logging.getLogger("hng.relay").warning(
                "%s refuses kind(s) %s; resubscribing for %s",
                self.relay, sorted(dropped), list(self.kinds) or "nothing")
            if self.on_refusal is not None:
                self.on_refusal(self.relay, dropped, self.kinds, self.last_error)
        return dropped

    def _args(self) -> list[str]:
        if not self.kinds:
            # Belt and braces: run() stops before this, but a filters-less REQ is a
            # firehose and must be impossible to construct by any path.
            raise ValueError(
                f"refusing to subscribe to {self.relay} with no kinds: that is a firehose")
        args = [self.nak, "req"]
        if self.auth:
            args.append("--auth")
        for kind in self.kinds:
            args += ["-k", str(kind)]

        if self.p_tag:
            args += ["-p", self.p_tag]
        # Only the recent window: without this every reconnect replays the relay's
        # entire history (NIP-59 backdates wraps up to 2 days, so 3 days is the floor).
        if self.since_days:
            args += ["-s", str(int(time.time()) - self.since_days * 86400)]
        # Keep the subscription open instead of exiting at EOSE and re-fetching.
        args.append("--stream")
        for author in self.authors:
            args += ["-a", author]
        args.append(self.relay)
        return args

    async def run(self, on_event: Handler) -> None:
        backoff = 2
        log = logging.getLogger("hng.relay")
        while not self._stop.is_set():
            try:
                env = None
                if self.secret:
                    import os as _os
                    env = dict(_os.environ, NOSTR_SECRET_KEY=self.secret)
                proc = await asyncio.create_subprocess_exec(
                    *self._args(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
            except Exception as exc:  # noqa: BLE001 - log, back off, retry
                self.last_error = f"spawn failed: {exc}"
                log.exception("cannot spawn nak for %s", self.relay)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            err = ""
            try:
                assert proc.stdout is not None
                async for raw in proc.stdout:
                    line = raw.decode().strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    self.events += 1
                    result = on_event(self.relay, event)
                    if asyncio.iscoroutine(result):
                        await result
                err = (await proc.stderr.read()).decode().strip() if proc.stderr else ""
                self.last_error = err.splitlines()[-1][:200] if err else None
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            if self._stop.is_set():
                break
            if self.note_refusals(err):
                # A refused kind is a filter problem, not a transport problem: the REQ
                # can never succeed as written, so resubscribe at once with what is
                # left rather than backing off into a loop that always fails. Bounded
                # by the number of kinds, so this cannot spin.
                if not self.kinds:
                    self.last_error = (
                        f"relay refuses every kind we subscribed to "
                        f"({list(self.requested_kinds)}): {err}"[:300])
                    log.error("%s: %s", self.relay, self.last_error)
                    break
                backoff = 2
                continue
            # A relay that answered 429 is rate-limiting us: backing off politely is the
            # fix, not retrying faster. Start at a minute and cap at ten.
            rate_limited = "429" in (self.last_error or "")
            if rate_limited:
                delay = min(max(backoff, 60) * 2, 600)
            else:
                delay = backoff
            import random
            await asyncio.sleep(delay * (0.8 + random.random() * 0.4))
            backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        self._stop.set()


class OneShot:
    """Single `nak req -l N` fetch per relay — the dogfood/`--once` path."""

    def __init__(self, me: str, *, limit: int = 50, since: int | None = None, auth: bool = True):
        self.me = me
        self.limit = limit
        self.since = since
        self.auth = auth
        self.nak = shutil.which("nak") or "nak"

    async def fetch(self, relay: str) -> list[dict]:
        args = [self.nak, "req"]
        if self.auth:
            args.append("--auth")
        args += ["-k", "1059", "-p", self.me, "-l", str(self.limit)]
        if self.since:
            args += ["-s", str(self.since)]
        args.append(relay)
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        events = []
        for line in out.decode().splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return events
