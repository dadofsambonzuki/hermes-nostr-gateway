"""Bot peers: the kind-0 `bot` flag, read once per peer and cached.

An agent talking to another agent has no natural stopping point — every reply is a
plausible reply — so one bounce can run all night. One did: 3,370 rows in the store for
a sentence repeated in both directions. Breaking that loop needs a fact the gateway did
not have, namely *is this peer a program*. Nostr puts it in kind 0: `{"bot": true}`.

This module owns only that fact:

* ``bot_flag`` reads the claim out of a kind-0 event (clients disagree on `true` vs
  `"true"`, and a malformed profile is a profile with no claim, not a delivery error);
* ``BotRegistry`` keeps the answers — in memory for the message path, in the store's
  `profiles` table so a restart does not refetch every peer — and fetches a peer's kind 0
  once, on first sight, because kind 0 is not in the read set.

The rules that use the flag (echo suppression, the reply cap, immediate-is-not-for-bots)
live in ``policy.Policy``, next to every other attention decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import time
from collections.abc import Callable, Sequence
from typing import Any

log = logging.getLogger("hng.bots")

#: How long a cached flag is trusted before a refresh is fetched. A kind 0 is
#: republished rarely, so a day stops one profile lookup per message while still
#: noticing a peer that changes its claim.
DEFAULT_TTL_S = 86400

_TRUE = {"true", "1", "yes", "y", "on"}
_FALSE = {"false", "0", "no", "n", "off"}


def bot_flag(event: dict[str, Any]) -> bool | None:
    """The `bot` field of a kind-0 event: True, False, or None when it claims nothing.

    None is not False: the caller still learns the profile was read (so it is not
    fetched again), but the peer is treated as a human because nothing said otherwise.
    """
    try:
        if int(event.get("kind") or 0) != 0:
            return None
    except (TypeError, ValueError):
        return None
    content = event.get("content")
    try:
        meta = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    value = meta.get("bot")
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
    return None


def profile_name(event: dict[str, Any]) -> str | None:
    """The kind-0 `name` (or `display_name`), for operators reading `status`."""
    content = event.get("content")
    try:
        meta = json.loads(content) if isinstance(content, str) else content
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(meta, dict):
        return None
    for key in ("display_name", "name", "username"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:80]
    return None


def kind0_event(pubkey: str, relays: Sequence[str], *, timeout: float = 8.0) -> dict | None:
    """One kind-0 event for `pubkey`, through the bindings.

    Cold path: once per peer we have never met, cached in memory and in the store, so a
    message never pays for this twice. It used to be a `nak req -l 1` child per peer; the
    query is identical and the binary is gone.
    """
    from .fetch import fetch_first_blocking

    return fetch_first_blocking(relays=[url for url in relays if url][:2], kinds=[0],
                                author=pubkey, limit=1, timeout=timeout)


def kind0_fetcher(relays: Callable[[], Sequence[str]], *, timeout: float = 8.0
                  ) -> Callable[[str], dict | None]:
    """Bind the kind-0 fetch to a relay list resolved at call time."""

    def fetch(pubkey: str) -> dict | None:
        return kind0_event(pubkey, list(relays()), timeout=timeout)

    return fetch


class BotRegistry:
    """Which peers are bots, cached in memory and in the store's `profiles` table.

    A peer we have never seen is **not** a bot. The first message from them is judged as
    a human's and a kind-0 fetch starts in the background, so the second is judged with
    the answer. That asymmetry is deliberate: failing open for one message costs one
    reply, while failing closed would mute a person whose profile relay is unreachable.

    ``store`` is optional so the policy can be exercised in a test without SQLite.
    """

    def __init__(self, store: Any | None = None, *,
                 fetcher: Callable[[str], dict | None] | None = None,
                 ttl_s: float = DEFAULT_TTL_S, max_in_flight: int = 2,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.fetcher = fetcher
        self.ttl_s = float(ttl_s)
        #: A mention wave from strangers must not become a fleet of `nak` children: at
        #: most this many profile fetches run at once, and the rest of the wave stays
        #: "unknown, treat as human" until a slot frees and they speak again.
        self.max_in_flight = max(1, int(max_in_flight))
        self._clock = clock
        #: pubkey -> (flag, when it was read). None means "read, no claim made".
        self._cache: dict[str, tuple[bool | None, float]] = {}
        self._in_flight: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()

    # -- reading ---------------------------------------------------------
    def ingest(self, event: dict[str, Any]) -> bool:
        """Record a kind-0 event. Returns whether it claimed anything about `bot`."""
        pubkey = str(event.get("pubkey") or "").strip().lower()
        if not pubkey:
            return False
        flag = bot_flag(event)
        is_bot = bool(flag)
        previous = self._cache.get(pubkey)
        if self.store is not None:
            self.store.record_profile(
                pubkey=pubkey, name=profile_name(event), is_bot=is_bot,
                event_id=str(event.get("id") or "") or None, now=int(self._clock()),
            )
        self._cache[pubkey] = (is_bot, self._clock())
        if previous is None or previous[0] != is_bot:
            log.info("kind-0 for %s: bot=%s", pubkey[:12], is_bot)
        return flag is not None

    def flag(self, pubkey: str) -> bool | None:
        """True/False when known, None when this peer has not been looked up yet."""
        key = (pubkey or "").strip().lower()
        if not key:
            return None
        cached = self._cache.get(key)
        if cached is not None:
            value, read_at = cached
            if value is None:
                # Looked up once and absent. Ask for the fetch again — observe() bounds
                # and de-dupes it — so a peer skipped by the in-flight cap is not stuck
                # as "unknown" for a day.
                self.observe(key)
                return None
            if self._clock() - read_at < self.ttl_s:
                return value
            # Keep the last answer and refresh it in the background: a stale `bot`
            # must not turn a loop back on while the refresh is in flight.
            self._cache[key] = (value, self._clock())
            self.observe(key, force=True)
            return value
        row = self.store.profile(key) if self.store is not None else None
        if row is None:
            self._cache[key] = (None, self._clock())
            self.observe(key)
            return None
        value = bool(row.get("bot"))
        self._cache[key] = (value, self._clock())
        return value

    def is_bot(self, pubkey: str) -> bool:
        """The flag as policy needs it: only a positive claim makes a peer a bot."""
        return self.flag(pubkey) is True

    # -- fetching --------------------------------------------------------
    def observe(self, pubkey: str, *, force: bool = False) -> None:
        """Fetch this peer's kind 0 once, in the background, unless already known.

        Called from the message path, so it must never block: the fetch is a thread for
        the child process and the store write happens back on the loop thread where the
        SQLite connection lives.
        """
        key = (pubkey or "").strip().lower()
        if not key or self.fetcher is None or key in self._in_flight:
            return
        if len(self._in_flight) >= self.max_in_flight:
            return
        known = self._cache.get(key, (None, 0.0))[0]
        if known is not None and not force:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # a CLI command with no daemon: nobody asked for a fetch
        self._in_flight.add(key)
        task = loop.create_task(self._fetch(key))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _fetch(self, pubkey: str) -> None:
        fetcher = self.fetcher
        assert fetcher is not None  # observe() only schedules when there is one
        try:
            event = await asyncio.get_running_loop().run_in_executor(None, fetcher, pubkey)
        except Exception:  # noqa: BLE001 - a profile fetch must never break delivery
            log.warning("kind-0 fetch failed for %s", pubkey[:12], exc_info=True)
            event = None
        finally:
            self._in_flight.discard(pubkey)
        if event:
            self.ingest(event)

    def cancel(self) -> None:
        """Drop in-flight profile fetches at shutdown: their answers are optional."""
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._in_flight.clear()

    # -- reporting -------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """What `status` shows: how many peers are known, and which are bots."""
        if self.store is not None:
            known = self.store.profile_count()
            flagged = [
                {"pubkey": row["pubkey"], "name": row.get("name"),
                 "since": row.get("updated_at")}
                for row in self.store.bot_peers()
            ]
        else:
            known = sum(1 for value, _ in self._cache.values() if value is not None)
            flagged = [{"pubkey": key, "name": None, "since": None}
                       for key, (value, _) in self._cache.items() if value]
        return {"known": known, "flagged": flagged, "in_flight": len(self._in_flight)}
