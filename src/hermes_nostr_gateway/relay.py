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
from typing import Any, Awaitable, Callable

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


#: nak's stderr, which is where its liveness facts live. Both are notices, not errors:
#: a healthy relay says `connecting to <host>... ok.` and then, when the relay requires
#: NIP-42, `authenticating to <relay> as <pubkey>…`. Reading only the *last* line after
#: the child died made both look like a diagnosis of a relay that was fine.
_CONNECTED_LINE = re.compile(r"connecting to\s+\S+\.\.\.\s*ok", re.IGNORECASE)
_AUTH_LINE = re.compile(r"authenticating to\s+\S+", re.IGNORECASE)



class RelayWorker:
    """A `nak req` subscription for one relay, restarted with backoff if it dies."""

    def __init__(self, relay: str, me: str, *, kinds: tuple[int, ...] = (1059,),
                 authors: tuple[str, ...] = (), p_tag: str | None = None, auth: bool = True,
                 secret: str | None = None, since_days: int = 3, nak: str | None = None,
                 on_refusal: RefusalHandler | None = None, purpose: str = "dm"):
        self.relay = relay
        # Why this relay is in the read set (`dm`, `feed`, `mentions`, `peer-inbox`).
        # `hng status` shows it, so an extra connection can be traced to a decision
        # rather than to drift.
        self.purpose = purpose
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
        # Live facts for `hng status`. None means "not observed", never "fine":
        # a worker that has not connected yet must not read like a healthy one.
        self.started_at: float | None = None
        self.attempt_started_at: float | None = None
        self.attempts = 0
        self.connected_at: float | None = None
        self.auth_at: float | None = None
        self.last_event_at: float | None = None
        self.last_stderr: str | None = None
        self._alive = False
        self._attempt_lines: list[str] = []
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

    @property
    def alive(self) -> bool:
        """True while a `nak` child for this subscription is running."""
        return self._alive

    def live_state(self) -> dict[str, Any]:
        """What this worker is doing now: the only honest input to a health verdict.

        `last_error` is kept for diagnostics, but it is the last stderr line of a child
        that may have exited hours ago; callers must not read it as the current state.
        """
        return {
            "relay": self.relay,
            "purpose": getattr(self, "purpose", "dm"),
            "kinds": list(self.kinds),
            "requested_kinds": list(self.requested_kinds),
            "refused_kinds": sorted(self.refused),
            "p_tag": bool(self.p_tag),
            "authors": len(self.authors),
            "events": self.events,
            "alive": self._alive,
            "attempts": self.attempts,
            "started_at": self.started_at,
            "attempt_started_at": self.attempt_started_at,
            "connected_at": self.connected_at,
            "auth_at": self.auth_at,
            "last_event_at": self.last_event_at,
            "last_stderr": self.last_stderr,
            "last_error": self.last_error,
        }

    async def _drain_stderr(self, stderr: Any) -> None:
        """Keep reading the child's stderr so its live notices are not lost."""
        if stderr is None:
            return
        if hasattr(stderr, "__aiter__"):
            async for raw in stderr:
                line = raw.decode(errors="replace").strip()
                if line:
                    self._note_stderr(line)
            return
        # A stream that only offers read() — a test double, or a pipe wrapped by a
        # caller — still has to be read, or its CLOSED/auth notices are lost and a
        # refused kind never gets dropped.
        raw = await stderr.read()
        for line in (raw or b"").decode(errors="replace").splitlines():
            if line.strip():
                self._note_stderr(line.strip())

    async def _settle(self, task: "asyncio.Task[Any]") -> None:
        """Give a finished child's stderr a moment to land, never forever."""
        try:
            await asyncio.wait_for(task, timeout=5)
        except TimeoutError:
            task.cancel()
        except Exception:  # noqa: BLE001 - a broken pipe must not kill the worker
            logging.getLogger("hng.relay").debug(
                "stderr pump for %s ended badly", self.relay)

    def _note_stderr(self, line: str) -> None:
        """Record a liveness fact from nak's stderr; do not diagnose from it."""
        self.last_stderr = line[:200]
        self._attempt_lines.append(line)
        now = time.time()
        if _AUTH_LINE.search(line):
            self.auth_at = now
        elif _CONNECTED_LINE.search(line):
            self.connected_at = now

    async def run(self, on_event: Handler) -> None:
        backoff = 2
        log = logging.getLogger("hng.relay")
        while not self._stop.is_set():
            self.attempts += 1
            self.attempt_started_at = time.time()
            self._attempt_lines = []
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
            if self.started_at is None:
                self.started_at = self.attempt_started_at
            self._alive = True
            # Read stderr *while* the child runs. It used to be read only after stdout
            # closed, which hid the connect/auth notices for exactly as long as they
            # were true and left only a dead attempt's last line on the record.
            stderr_task = asyncio.create_task(self._drain_stderr(proc.stderr))
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
                    self.last_event_at = time.time()
                    result = on_event(self.relay, event)
                    if asyncio.iscoroutine(result):
                        await result
                await self._settle(stderr_task)
                err = "\n".join(self._attempt_lines).strip()
                self.last_error = err.splitlines()[-1][:200] if err else None
            finally:
                self._alive = False
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
                if not stderr_task.done():
                    stderr_task.cancel()
                try:
                    await stderr_task
                except asyncio.CancelledError:
                    pass
                except Exception:  # noqa: BLE001 - never let a pipe end the worker
                    pass
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
