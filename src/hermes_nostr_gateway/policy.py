"""Attention policy: who reaches the agent, when, and with what authority.

Two independent dimensions, deliberately not collapsed into one "trusted" flag:

    reach      immediate | digest(interval) | drop
    authority  instructions | none

A public bot needs strangers to reach it (so it can answer) without letting them
instruct it (so they cannot drive tools). Conflating these is how agent bots get
prompt-injected. See docs/gateway-spec.md §17.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

REACH_PRIORITY = {"drop": 0, "digest": 1, "immediate": 2}
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$", re.I)
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}


def parse_interval(value: str | int | None) -> int | None:
    """'15m' -> 900, '1h' -> 3600, 90 -> 90, None -> None."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    m = _INTERVAL.match(value)
    if not m:
        raise ValueError(f"bad interval: {value!r}")
    return int(m.group(1)) * _UNIT[m.group(2).lower()]


def _parse_reach(value: Any) -> tuple[str, int | None]:
    """'immediate' | 'drop' | 'digest' | 'digest(15m)' | {'mode':...} -> (mode, seconds)."""
    if isinstance(value, dict):
        mode = str(value.get("mode", "digest")).lower()
        return mode, parse_interval(value.get("interval"))
    text = str(value).strip()
    m = re.match(r"^([a-z]+)\s*(?:\(([^)]*)\))?$", text, re.I)
    if not m:
        raise ValueError(f"bad reach: {value!r}")
    mode = m.group(1).lower()
    if mode not in REACH_PRIORITY:
        raise ValueError(f"unknown reach mode: {mode!r}")
    interval = parse_interval(m.group(2)) if m.group(2) else None
    if mode == "digest" and interval is None:
        interval = 900  # default 15m, never an unbounded digest
    return mode, interval


@dataclass(frozen=True)
class Decision:
    reach: str
    interval_s: int | None
    authority: str
    rule: str
    why: str
    duplicate: bool = False

    @property
    def deliverable(self) -> bool:
        return self.reach != "drop"

    def to_frame(self) -> dict[str, Any]:
        return {
            "reach": self.reach,
            "interval_s": self.interval_s,
            "authority": self.authority,
            "rule": self.rule,
            "why": self.why,
            "duplicate": self.duplicate,
        }


@dataclass
class _Rule:
    name: str
    group: str
    kinds: set[int] | None
    mention: bool | None
    dm: bool | None
    reach: str
    interval_s: int | None
    authority: str
    limit: int | None = None
    _hits: deque[float] = field(default_factory=deque)

    def matches(self, *, group: str, kind: int, mention: bool, dm: bool) -> bool:
        if self.group != "any" and self.group != group:
            return False
        if self.kinds is not None and kind not in self.kinds:
            return False
        if self.mention is not None and self.mention != mention:
            return False
        if self.dm is not None and self.dm != dm:
            return False
        return True

    def over_quota(self, now: float, window_s: int = 3600) -> bool:
        if self.limit is None:
            return False
        while self._hits and now - self._hits[0] > window_s:
            self._hits.popleft()
        return len(self._hits) >= self.limit

    def hit(self, now: float) -> None:
        self._hits.append(now)


class Policy:
    """Ordered rules, first match wins; anything unmatched falls to `defaults`."""

    def __init__(self, config: dict[str, Any], clock: Callable[[], float] = time.time):
        self._clock = clock
        self.groups: dict[str, set[str]] = {
            name: set(members or []) for name, members in (config.get("groups") or {}).items()
        }
        if "any" in self.groups:
            raise ValueError("'any' is a built-in group and cannot be defined")
        self._rules: list[_Rule] = []
        for i, raw in enumerate(config.get("policies") or []):
            match = raw.get("match") or {}
            reach, interval = _parse_reach(
                raw.get("reach", raw.get("deliver", "digest(15m)"))
            )
            authority = str(raw.get("authority", "none")).lower()
            if authority not in ("instructions", "none"):
                raise ValueError(f"bad authority: {authority!r}")
            kinds = match.get("kinds", match.get("kind"))
            if kinds is not None and not isinstance(kinds, (list, tuple, set)):
                kinds = [kinds]
            self._rules.append(
                _Rule(
                    name=raw.get("name") or f"rule[{i}]",
                    group=str(match.get("group", "any")),
                    kinds={int(k) for k in kinds} if kinds is not None else None,
                    mention=match.get("mention"),
                    dm=match.get("dm"),
                    reach=reach,
                    interval_s=interval,
                    authority=authority,
                    limit=raw.get("limit"),
                )
            )
        defaults = config.get("defaults") or {}
        d_reach, d_interval = _parse_reach(defaults.get("reach", "digest(15m)"))
        self.default = Decision(
            reach=d_reach,
            interval_s=d_interval,
            authority=str(defaults.get("authority", "none")).lower(),
            rule="defaults",
            why="no rule matched",
        )
        self._seen: dict[str, Decision] = {}

    # -- groups ---------------------------------------------------------
    def named_groups(self, npub: str) -> set[str]:
        return {name for name, members in self.groups.items() if npub in members}

    def group_of(self, npub: str) -> str:
        """The group a rule refers to: the first named group holding them, else 'unknown'."""
        named = self.named_groups(npub)
        if named:
            return sorted(named)[0]
        return "unknown"

    # -- decision -------------------------------------------------------
    def decide(
        self,
        *,
        sender: str,
        kind: int,
        mention: bool = False,
        dm: bool = False,
        event_id: str | None = None,
    ) -> Decision:
        now = self._clock()
        group = self.group_of(sender)

        chosen: Decision | None = None
        for rule in self._rules:
            if not rule.matches(group=group, kind=kind, mention=mention, dm=dm):
                continue
            if rule.over_quota(now):
                chosen = Decision(
                    reach="drop",
                    interval_s=None,
                    authority="none",
                    rule=rule.name,
                    why=f"quota {rule.limit}/h exceeded",
                )
                break
            rule.hit(now)
            chosen = Decision(
                reach=rule.reach,
                interval_s=rule.interval_s,
                authority=rule.authority,
                rule=rule.name,
                why=f"matched {rule.name} (group {group})",
            )
            break

        if chosen is None:
            chosen = self.default

        # Dedupe across rules: one event, one delivery, best rule wins.
        if event_id is not None:
            prior = self._seen.get(event_id)
            if prior is not None:
                if REACH_PRIORITY[chosen.reach] > REACH_PRIORITY[prior.reach]:
                    self._seen[event_id] = chosen
                    return Decision(**{**chosen.__dict__, "duplicate": True})
                return Decision(**{**prior.__dict__, "duplicate": True})
            self._seen[event_id] = chosen
        return chosen

    def digest_due(self, interval_s: int | None, last_sent: float | None, now: float | None = None) -> bool:
        """Whether a digest window has closed. Callers own the batching; this owns the clock."""
        if interval_s is None:
            return False
        now = self._clock() if now is None else now
        return last_sent is None or (now - last_sent) >= interval_s
