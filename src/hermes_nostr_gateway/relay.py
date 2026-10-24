"""Relay I/O: one worker per relay, so coverage is visible per relay.

Reading [kinds: 1059, #p: me] from the identity's 10050 alone is not enough: DMs exist
only on the sender's app relays (ditto, dreamith, whitenoise) and the gap is silent.
So: N workers, per-relay counters, and a relay that stops delivering is reported rather
than assumed healthy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from typing import Awaitable, Callable

Handler = Callable[[str, dict], Awaitable[None] | None]


class RelayWorker:
    """A `nak req` subscription for one relay, restarted with backoff if it dies."""

    def __init__(self, relay: str, me: str, *, kinds: tuple[int, ...] = (1059,),
                 authors: tuple[str, ...] = (), p_tag: str | None = None, auth: bool = True,
                 secret: str | None = None, since_days: int = 3):
        self.relay = relay
        self.kinds = kinds
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
        self.nak = shutil.which("nak") or "nak"
        self.events = 0
        self.last_error: str | None = None
        self._stop = asyncio.Event()

    def _args(self) -> list[str]:
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
