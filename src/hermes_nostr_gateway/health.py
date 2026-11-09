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

Three rules keep them honest:

* A missing fact is "unknown", never "healthy". A daemon that cannot say leaves the
  verdict saying so.
* The store's per-relay row is history. It is shown only as history, never as the
  reason a relay is or is not working.
* A refusal is a state, not a fault. `refused` is counted apart from `down`, because a
  relay that will not serve our filters says the read set is wrong, not that the
  gateway is, and one such policy must not make every health call read degraded.

Why "worker alive" is evidence of a connection: `nak req --stream` exits within a
second when it cannot reach the relay (`failed to connect to any of the given
relays.`), so a child holding the subscription is a child that connected.

`health_report` is the same evidence in one frame, per plane: last event, undelivered
backlog, policy blocks, and whether anything is degraded. It exists so the overnight
cron can write its narrative from one call instead of parsing `status` text or the
journal, where a quiet plane and a broken one read the same.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
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


def _count(worker: dict[str, Any], key: str) -> int:
    """An integer counter from a worker entry; a missing or odd one reads as 0."""
    try:
        return int(worker.get(key) or 0)
    except (TypeError, ValueError):
        return 0


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
    # is a configuration/negotiation fault, not a transport blip. It is `refused`, not
    # `down`: the relay is answering, it just will not serve us, and that is a fact about
    # the read set rather than a fault the whole gateway inherits.
    if not active and refused:
        return Verdict(relay, "refused", "refused",
                       f"kind(s) {_kind_list(refused)} not allowed; nothing left to read",
                       events=events, last_event_at=_stamp(last_event_at), age_s=age_s,
                       severity=WARN)

    # A relay that answered AUTH and closed the filter anyway is deciding something
    # about this key, not finishing a handshake. "authenticating" would be a permanent
    # lie — every retry earns a fresh challenge, so the state never expires — and the
    # blind spot would never be named. Checked before the "down" and mid-AUTH states,
    # because a refusal streak is the *cause* of both: a worker waiting to re-subscribe
    # is not a dead child, and what it is waiting on is known. Only when nothing has ever
    # been delivered: a relay that delivered and refuses now is a different, later fault.
    auth_refused = [worker for worker in workers if _count(worker, "auth_refusals") >= 2]
    if auth_refused and not events:
        count = max(_count(worker, "auth_refusals") for worker in auth_refused)
        stamps = [_float(worker.get("auth_refused_at")) for worker in auth_refused]
        newest = max([stamp for stamp in stamps if stamp is not None], default=None)
        detail = f"auth-required after NIP-42 ({count}x)"
        if newest is not None:
            detail += f", last {human_age(now - newest)} ago"
        return Verdict(relay, "refused", "refused", detail,
                       events=events, severity=WARN)

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


def relay_counts(verdicts: Iterable[Verdict]) -> dict[str, Any]:
    """How the read set divides up, by state and by severity.

    `refused` and `down` are counted apart on purpose: "8 relays, 4 refused" says the
    read set is wrong for this host, while "8 relays, 4 down" says the transport is. The
    two want different fixes, so they must not share a number.
    """
    rows = list(verdicts)
    states: dict[str, int] = {}
    for row in rows:
        states[row.state] = states.get(row.state, 0) + 1
    return {
        "total": len(rows),
        "ok": sum(1 for row in rows if row.severity == OK),
        "warn": sum(1 for row in rows if row.severity == WARN),
        "bad": sum(1 for row in rows if row.severity == BAD),
        "refused": states.get("refused", 0),
        "down": states.get("down", 0),
        "states": states,
    }


#: The order a counts line reads in: what is working, then what is not, then what is
#: unknown. Anything new sorts after these rather than inventing a position.
STATE_ORDER = ("connected", "quiet", "connecting", "authenticating",
               "refused", "down", "no-worker", "unknown")


def counts_text(counts: dict[str, Any]) -> str:
    """`8 read relay(s): 2 connected, 5 quiet, 1 refused` — the one-line read set."""
    total = int(counts.get("total") or 0)
    if not total:
        return "no read relay(s)"
    states = counts.get("states") or {}
    order = list(STATE_ORDER) + sorted(set(states) - set(STATE_ORDER))
    parts = [f"{states[state]} {state.replace('-', ' ')}"
             for state in order if states.get(state)]
    return f"{total} read relay(s): " + ", ".join(parts)


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
    # Peer-inbox workers are additions discovered from a peer's kind 10050, not config
    # entries: reporting them as "config no longer lists" would be a false alarm.
    running = {
        str(worker.get("relay") or "")
        for worker in (status or {}).get("workers") or []
        if str(worker.get("purpose") or "dm") != "peer-inbox"
    }
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


# -- one call for the overnight cron ---------------------------------------
#
# `status` answers "is each relay working", which is the wrong question at 04:00: the
# cron wants to say what the gateway did, for which plane, and whether anything is
# stuck. Parsing `status` text or the journal for that is how a narrative becomes a
# guess. `health` is one frame, built from the same evidence: per plane the last event,
# the undelivered backlog, the policy blocks, and a degraded flag with its reasons.

#: A message the gateway accepted and the adapter has not acked within this window is
#: stuck, not in flight: a healthy adapter acks in milliseconds.
DEFAULT_PENDING_STALE_MINUTES = 10

#: The planes a gateway attaches without being asked. `marmot` is opt-in, so it joins
#: the list only when that plane is actually attached.
BASE_PLANES = ("nip17", "nip04", "nip01", "concord")


def default_planes(*, marmot: bool = False) -> list[str]:
    """The planes to report on, in a stable order."""
    return list(BASE_PLANES) + (["marmot"] if marmot else [])


@dataclass(frozen=True)
class PlaneHealth:
    """One plane's traffic, from the store rows that describe it."""

    plane: str
    events: int = 0
    pending: int = 0
    blocked: int = 0
    sent: int = 0
    last_event_at: int | None = None
    age_s: int | None = None
    oldest_pending_s: int | None = None
    wraps: dict[str, int] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plane": self.plane,
            "events": self.events,
            "pending": self.pending,
            "blocked": self.blocked,
            "sent": self.sent,
            "last_event_at": self.last_event_at,
            "age_s": self.age_s,
            "oldest_pending_s": self.oldest_pending_s,
            "wraps": dict(self.wraps),
            "degraded": self.degraded,
            "reasons": list(self.reasons),
        }


def plane_health(rows: Iterable[dict[str, Any]], *, planes: Iterable[str] | None = None,
                 now: float | None = None,
                 pending_stale_minutes: int = DEFAULT_PENDING_STALE_MINUTES,
                 wraps: dict[str, dict[str, int]] | None = None,
                 transport: str | None = None) -> list[PlaneHealth]:
    """A per-plane verdict over `store.plane_report()` rows.

    What makes a plane degraded, and what deliberately does not:

    * **stuck backlog** — the oldest undelivered message is older than
      `pending_stale_minutes`. The adapter is not consuming; the messages are not lost
      but nobody is being told about them.
    * **expired wraps** — we accepted a message and the retry TTL ran out before any
      relay took it. That is a message the sender believes was delivered. `failed`
      wraps are not counted: they are still being retried.
    * **no transport** — `transport` is set by the caller when no read relay is
      delivering, which leaves every plane silent for a reason that is not the plane's.
    * **`blocked` never degrades.** A blocked row is the policy working — a stranger,
      or a bot over its cap. It is reported because a sudden rise is worth seeing, but
      a gateway that never blocked anything would be misconfigured, not healthy.
    """
    now = time.time() if now is None else float(now)
    by_plane: dict[str, dict[str, Any]] = {}
    for row in rows:
        name = str(row.get("plane") or "")
        if name:
            by_plane[name] = row
    wanted = list(planes) if planes is not None else sorted(by_plane)
    out: list[PlaneHealth] = []
    for plane in wanted:
        row = by_plane.get(plane) or {}
        pending = int(row.get("pending") or 0)
        oldest = _float(row.get("oldest_pending_at"))
        oldest_s = int(now - oldest) if oldest is not None else None
        plane_wraps = dict((wraps or {}).get(plane) or {})
        reasons: list[str] = []
        if (pending and oldest_s is not None and pending_stale_minutes
                and oldest_s >= pending_stale_minutes * 60):
            reasons.append(f"{pending} message(s) pending for {human_age(oldest_s)}")
        expired = int(plane_wraps.get("expired") or 0)
        if expired:
            reasons.append(f"{expired} outbound wrap(s) expired un-sent")
        if transport:
            reasons.append(transport)
        last_event_at = _stamp(_float(row.get("last_event_at")))
        out.append(PlaneHealth(
            plane=plane,
            events=int(row.get("events") or 0),
            pending=pending,
            blocked=int(row.get("blocked") or 0),
            sent=int(row.get("sent") or 0),
            last_event_at=last_event_at,
            age_s=int(now - last_event_at) if last_event_at else None,
            oldest_pending_s=oldest_s,
            wraps=plane_wraps,
            reasons=tuple(reasons),
        ))
    return out


def health_report(*, planes: Iterable[str], plane_rows: Iterable[dict[str, Any]] = (),
                  verdicts: Iterable[Verdict] = (), wraps: dict[str, dict[str, int]] | None = None,
                  daemon: bool = True, daemon_error: str | None = None,
                  identity: str = "", socket_path: str = "",
                  uptime_s: float | None = None, now: float | None = None,
                  pending_stale_minutes: int = DEFAULT_PENDING_STALE_MINUTES,
                  notes: Iterable[str] = ()) -> dict[str, Any]:
    """The `health` frame: one call, every fact an overnight narrative needs.

    `daemon` is whether the socket answered. It is not a detail: with no daemon the
    planes are read from the store, which is history, so the verdict is `down` and the
    per-plane ages are the last the store saw rather than the last the gateway saw.
    """
    now = time.time() if now is None else float(now)
    verdicts = list(verdicts)
    counts = relay_counts(verdicts)
    bad = [verdict for verdict in verdicts if verdict.severity == BAD]
    warned = [verdict for verdict in verdicts if verdict.severity == WARN]
    ok_relays = [verdict for verdict in verdicts if verdict.severity == OK]
    refused = [verdict for verdict in verdicts if verdict.state == "refused"]

    # Every relay refused or died: the planes are silent for a reason that belongs to
    # the transport, not to them. Saying it per plane stops four identical mysteries.
    transport = ("no read relay is delivering"
                 if daemon and verdicts and not ok_relays else None)

    planes_report = plane_health(plane_rows, planes=planes, now=now,
                                 pending_stale_minutes=pending_stale_minutes,
                                 wraps=wraps, transport=transport)

    reasons: list[str] = []
    if not daemon:
        reasons.append("daemon not running" + (f": {daemon_error}" if daemon_error else ""))
    reasons += [f"{verdict.relay}: {verdict.label}" for verdict in bad]
    reasons += [f"{health.plane}: {reason}" for health in planes_report
                for reason in health.reasons]

    verdict = "down" if not daemon else ("degraded" if reasons else "ok")
    pending_total = sum(health.pending for health in planes_report)
    blocked_total = sum(health.blocked for health in planes_report)
    events_total = sum(health.events for health in planes_report)
    sent_total = sum(health.sent for health in planes_report)
    wrap_totals: dict[str, int] = {}
    for plane_wraps in (wraps or {}).values():
        for status, count in plane_wraps.items():
            wrap_totals[status] = wrap_totals.get(status, 0) + int(count)

    return {
        "type": "health",
        "verdict": verdict,
        "degraded": bool(reasons),
        "reasons": reasons,
        "summary": _summary(verdict, planes_report, counts, ok_relays,
                            pending_total, blocked_total, uptime_s),
        "identity": identity,
        "socket": socket_path,
        "daemon": daemon,
        "daemon_error": daemon_error,
        "uptime_s": int(uptime_s) if uptime_s is not None else None,
        "planes": {health.plane: health.as_dict() for health in planes_report},
        "counts": {"events": events_total, "pending": pending_total,
                   "blocked": blocked_total, "sent": sent_total,
                   "wraps": wrap_totals},
        "relays": {
            "total": len(verdicts),
            "ok": len(ok_relays),
            "warn": len(warned),
            "bad": len(bad),
            # A refused relay is its own count, not a bad one: it is not delivering, but
            # the gateway is not failing, the read set is wrong for this host.
            "refused": len(refused),
            "refused_relays": [verdict.as_dict() for verdict in refused],
            "unhealthy": [verdict.relay for verdict in bad],
            "warnings": [f"{verdict.relay}: {verdict.label}" for verdict in warned],
            "counts": counts,
            "list": [verdict.as_dict() for verdict in verdicts],
        },
        "notes": list(notes),
    }


def _summary(verdict: str, planes_report: list[PlaneHealth], counts: dict[str, Any],
             ok_relays: list[Verdict], pending: int, blocked: int,
             uptime_s: float | None) -> str:
    """One sentence, built from the same numbers — never a second reading of them."""
    live = [health for health in planes_report if health.last_event_at]
    bits = [f"gateway {verdict}"]
    if uptime_s is not None:
        bits.append(f"up {human_age(uptime_s)}")
    bits.append(f"{len(planes_report)} plane(s)")
    if live:
        newest = max(live, key=lambda health: health.last_event_at or 0)
        bits.append(f"{newest.plane} last event {human_age(newest.age_s)} ago")
    elif planes_report:
        bits.append("no events in the store")
    bits.append(f"{pending} pending")
    bits.append(f"{blocked} blocked")
    if counts.get("total"):
        bits.append(f"{len(ok_relays)}/{counts['total']} relay(s) ok")
        if counts.get("refused"):
            bits.append(f"{counts['refused']} refused")
    return ", ".join(bits)


def render_health(report: dict[str, Any], *, indent: str = "  ") -> list[str]:
    """The human form of the `health` frame: verdict, planes, relays, then reasons."""
    reasons = list(report.get("reasons") or [])
    head = report.get("verdict", "unknown")
    suffix = f" — {reasons[0]}" if reasons else ""
    lines = [f"{head}{suffix}" + ("" if len(reasons) <= 1 else f" (+{len(reasons) - 1} more)")]
    if report.get("summary"):
        lines.insert(0, str(report["summary"]))
    planes = report.get("planes") or {}
    if planes:
        names = list(planes)
        width = max(len(name) for name in names)
        for name in names:
            plane = planes[name]
            state = "degraded" if plane.get("degraded") else "ok"
            last = ("no events" if plane.get("last_event_at") is None
                    else f"last event {human_age(plane.get('age_s'))} ago")
            lines.append(f"{indent}{name:<{width}}  {state:<8}  {last:<22}"
                         f"  pending={plane.get('pending', 0)}"
                         f"  blocked={plane.get('blocked', 0)}")
    relays = report.get("relays") or {}
    if relays.get("total"):
        line = (f"{indent}relays: {relays.get('ok', 0)}/{relays['total']} ok")
        if relays.get("refused"):
            line += f", {relays['refused']} refused"
        if relays.get("unhealthy"):
            line += f", unhealthy: {', '.join(relays.get('unhealthy') or [])}"
        lines.append(line)
        # A refusal is reported once, with the reason, here — not as four warnings on the
        # per-plane lines and not as a degraded verdict the whole gateway inherits.
        for row in relays.get("refused_relays") or []:
            lines.append(f"{indent}! refused {row.get('relay')} — "
                         f"{row.get('detail') or 'no reason given'}")
    for reason in reasons[1:]:
        lines.append(f"{indent}! {reason}")
    for note in report.get("notes") or []:
        lines.append(f"{indent}note: {note}")
    return lines

