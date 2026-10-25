"""Relay health as a live verdict, not a scrap of a child's stderr.

`relay_stats.last_error` holds whatever `nak` last wrote on stderr. For a healthy relay
that is `connecting to nos.lol... ok.`; for a gated one it is an auth notice; after a
restart it is the failure from three attempts ago. None of those answer "is this relay
delivering now?", and reading the column as if they did once sent someone chasing a
relay that was fine.

The daemon can answer it: its `status` frame lists the live workers, what each one is
subscribed to, what the relay refused, whether the child is running, and (since the
per-worker timestamps were added) when an event last arrived. The verdicts below are
pure functions over that frame plus the configured read set, so they are testable
without a relay, a clock or a daemon.

Two rules keep them honest:

* A missing fact is "unknown", never "healthy". A daemon that cannot say leaves the
  verdict saying so.
* The store's per-relay row is history. It is shown only as history, never as the
  reason a relay is or is not working.

Why "worker alive" is evidence of a connection: `nak req --stream` exits within a
second when it cannot reach the relay (`failed to connect to any of the given
relays.`), so a child still holding the subscription is a child that connected.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Iterable

#: nak prints `authenticating to <relay> as …` before the relay's AUTH completes and
#: never prints a matching success line, so the notice must expire instead of pinning
#: a healthy relay at "authenticating" forever.
AUTH_GRACE_S = 120

#: A worker spawned this recently has not had time to connect yet; "connecting" is the
#: truth, not a fault.
CONNECTING_GRACE_S = 20

#: Default window after which a connected relay with no events is reported as quiet.
DEFAULT_QUIET_MINUTES = 30

OK, WARN, BAD = "ok", "warn", "bad"


@dataclass(frozen=True)
class Verdict:
    """One relay's health, as of the frame it was computed from."""

    relay: str
    state: str
    label: str
    detail: str = ""
    events: int = 0
    last_event_at: int | None = None
    age_s: int | None = None
    severity: str = OK
    #: "live" (daemon worker state), "config" (daemon not running) or "history".
    source: str = "live"

    @property
    def healthy(self) -> bool:
        return self.severity == OK

    def as_dict(self) -> dict[str, Any]:
        return {
            "relay": self.relay,
            "state": self.state,
            "verdict": self.label,
            "detail": self.detail,
            "events": self.events,
            "last_event_at": self.last_event_at,
            "age_s": self.age_s,
            "severity": self.severity,
            "source": self.source,
        }


def human_age(seconds: float | int | None) -> str:
    """`45s`, `12m`, `3h`, `2d` — coarse on purpose; this is a status line."""
    if seconds is None:
        return "-"
    total = int(max(0, seconds))
    if total < 90:
        return f"{total}s"
    minutes = total // 60
    if minutes < 90:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _latest(workers: Iterable[dict[str, Any]], key: str) -> float | None:
    stamps = [_float(w.get(key)) for w in workers]
    stamps = [stamp for stamp in stamps if stamp is not None]
    return max(stamps) if stamps else None


def _kinds(workers: Iterable[dict[str, Any]], key: str) -> set[int]:
    found: set[int] = set()
    for worker in workers:
        for kind in worker.get(key) or []:
            try:
                found.add(int(kind))
            except (TypeError, ValueError):
                continue
    return found


def _kind_list(kinds: Iterable[int]) -> str:
    return ", ".join(str(kind) for kind in sorted(kinds))


def _verdict(relay: str, workers: list[dict[str, Any]], *, now: float,
             quiet_minutes: int, daemon_up: bool) -> Verdict:
    if not daemon_up:
        return Verdict(relay, "unknown", "unknown",
                       "daemon not running: no live worker state to read",
                       severity=WARN, source="config")
    if not workers:
        return Verdict(relay, "no-worker", "no worker",
                       "configured, but the running daemon has no worker for it "
                       "(restart after a config change?)",
                       severity=BAD, source="config")

    events = 0
    for worker in workers:
        try:
            events += int(worker.get("events") or 0)
        except (TypeError, ValueError):
            continue

    active = _kinds(workers, "kinds")
    refused = _kinds(workers, "refused_kinds")
    # Only the daemon that sends per-worker timestamps can answer "when"; an older
    # frame leaves the age unknown rather than borrowing the store's row.
    has_live_times = any("last_event_at" in worker for worker in workers)
    last_event_at = _latest(workers, "last_event_at")
    connected_at = _latest(workers, "connected_at")
    alive = [worker for worker in workers if worker.get("alive", True)]
    age_s = int(now - last_event_at) if last_event_at else None

    # A relay that refused every kind we asked for cannot deliver on that filter; that
    # is a configuration/negotiation fault, not a transport blip.
    if not active and refused:
        return Verdict(relay, "refusing", "refusing",
                       f"refused kind(s) {_kind_list(refused)}; nothing left to read",
                       events=events, last_event_at=_stamp(last_event_at), age_s=age_s,
                       severity=BAD)

    if not alive:
        # Deliberately no stderr text here: a child's last words are diagnostics, not a
        # verdict. `hng status -v` shows them under a heading that says so.
        return Verdict(relay, "down", "down", "every child process has exited",
                       events=events, last_event_at=_stamp(last_event_at),
                       age_s=age_s, severity=BAD)

    # Mid-AUTH: recent notice, and no event has arrived since it was printed.
    auths = []
    for worker in alive:
        auth_at = _float(worker.get("auth_at"))
        if auth_at is None or now - auth_at > AUTH_GRACE_S:
            continue
        seen_since = _float(worker.get("last_event_at")) or 0
        if seen_since < auth_at:
            auths.append(auth_at)
    if auths:
        newest = max(auths)
        return Verdict(relay, "authenticating", "authenticating",
                       f"since {human_age(now - newest)} ago",
                       events=events, last_event_at=_stamp(last_event_at),
                       age_s=age_s, severity=WARN)

    evidence = bool(last_event_at or connected_at or events)
    if not evidence:
        started = _latest(workers, "attempt_started_at") or _latest(workers, "started_at")
        if started is not None and now - started < CONNECTING_GRACE_S:
            return Verdict(relay, "connecting", "connecting",
                           f"spawned {human_age(now - started)} ago",
                           events=events, severity=WARN)
        if not has_live_times and connected_at is None:
            # A daemon from before per-worker timestamps: all we know is that the child
            # is running. Say what that means and what it does not.
            return Verdict(relay, "connected", "connected",
                           "worker running; this daemon reports no connect times",
                           events=events)
        return Verdict(relay, "connecting", "connecting",
                       "no connect or event seen yet", events=events, severity=WARN)

    if quiet_minutes and last_event_at and now - last_event_at >= quiet_minutes * 60:
        return Verdict(relay, "quiet", f"no events for {human_age(now - last_event_at)}",
                       "", events=events, last_event_at=_stamp(last_event_at),
                       age_s=age_s, severity=WARN)

    detail = ""
    if refused and active:
        detail = f"relay refuses kind(s) {_kind_list(refused)}; still reading " \
                 f"{_kind_list(active)}"
    return Verdict(relay, "connected", "connected", detail, events=events,
                   last_event_at=_stamp(last_event_at), age_s=age_s)


def _stamp(value: float | None) -> int | None:
    return int(value) if value is not None else None


def relay_verdicts(status: dict[str, Any] | None, *, relays: Iterable[str] | None = None,
                   now: float | None = None,
                   quiet_minutes: int = DEFAULT_QUIET_MINUTES) -> list[Verdict]:
    """A verdict per configured read relay, from the daemon's live status frame.

    `status` is the frame `{"op": "status"}` returned, or None when the daemon could
    not be reached. `relays` defaults to what the daemon says it reads; the CLI passes
    the config's read set so a relay added to config but not yet picked up by the
    daemon is reported instead of being invisible.
    """
    now = time.time() if now is None else float(now)
    frame = status or {}
    workers: dict[str, list[dict[str, Any]]] = {}
    for worker in frame.get("workers") or []:
        relay = str(worker.get("relay") or "")
        if relay:
            workers.setdefault(relay, []).append(worker)

    wanted = list(relays) if relays is not None else list(
        frame.get("relays_read") or sorted(workers))
    return [_verdict(relay, workers.get(relay) or [], now=now,
                     quiet_minutes=quiet_minutes, daemon_up=status is not None)
            for relay in wanted]


def reports_event_times(status: dict[str, Any] | None) -> bool:
    """Whether this daemon's workers report when they last saw an event.

    The field is new: an older daemon answers `status` without it, so the CLI says why
    the ages are missing instead of inventing them from the store.
    """
    workers = (status or {}).get("workers") or []
    return bool(workers) and all("last_event_at" in worker for worker in workers)


def stale_rows(status: dict[str, Any] | None,
               configured: Iterable[str]) -> list[dict[str, Any]]:
    """Store rows for relays the configuration no longer names.

    These are history: a removed relay keeps its counters forever, and printing them
    among the live relays is how `wss://relay.eu.whitenoise.chat` stayed on the board
    after it was dropped from `relays.read`.
    """
    known = {str(relay) for relay in configured}
    return [row for row in (status or {}).get("relay_stats") or []
            if str(row.get("relay") or "") not in known]


def running_but_unconfigured(status: dict[str, Any] | None,
                             configured: Iterable[str]) -> list[str]:
    """Relays the daemon still reads that the config no longer lists (restart needed)."""
    known = {str(relay) for relay in configured}
    running = {str(worker.get("relay") or "") for worker in (status or {}).get("workers") or []}
    return sorted(relay for relay in running if relay and relay not in known)


def render(verdicts: Iterable[Verdict], *, indent: str = "  ") -> list[str]:
    """Aligned relay lines: relay, verdict, counters, then any detail in parentheses."""
    rows = list(verdicts)
    if not rows:
        return [f"{indent}(no read relays configured)"]
    width = max(len(row.relay) for row in rows)
    label_width = max(len(row.label) for row in rows)
    lines = []
    for row in rows:
        # No live frame means no counter either: `events=0` would read as "nothing
        # arrived" when the truth is "nothing was asked".
        counted = f"events={row.events:<5}" if row.source == "live" else "events=-    "
        line = f"{indent}{row.relay:<{width}}  {row.label:<{label_width}}  {counted}"
        if row.last_event_at is not None and row.state not in ("quiet", "unknown"):
            line += f" last event {human_age(row.age_s)} ago"
        if row.detail:
            line += f"  ({row.detail})"
        lines.append(line.rstrip())
    return lines
