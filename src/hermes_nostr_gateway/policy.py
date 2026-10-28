"""Attention policy: who reaches the agent, when, and with what authority.

Two independent dimensions, deliberately not collapsed into one "trusted" flag:

    reach      immediate | digest(interval) | drop
    authority  instructions | none

A public bot needs strangers to reach it (so it can answer) without letting them
instruct it (so they cannot drive tools). Conflating these is how agent bots get
prompt-injected. See docs/gateway-spec.md §17.

A peer that *is itself a bot* (kind 0 `bot: true`, read by `bots.BotRegistry`) gets
three extra rules: it can never reach the agent immediately, our own text arriving back
from it is not a message, and it gets a bounded number of messages per hour — which is a
bound on our replies. They live here because they are attention decisions like any
other; only the flag lives in `bots`.
"""

from __future__ import annotations

import re
import time
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable

REACH_PRIORITY = {"drop": 0, "digest": 1, "immediate": 2}
_INTERVAL = re.compile(r"^\s*(\d+)\s*([smh]?)\s*$", re.I)
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600}
BOT_WINDOW_S = 3600


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


def normalise_text(value: str | None) -> str:
    """The form two messages are compared in: no surrounding or repeated whitespace.

    Deliberately not lowercased and not stripped of punctuation: an echo is our own
    sentence coming back, and "OK" returning as "ok" is somebody typing, not a loop.
    """
    return " ".join((value or "").split())


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
    blocked_reason: str | None = None

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
            "blocked_reason": self.blocked_reason,
        }


@dataclass(frozen=True)
class BotRules:
    """What a peer that is itself a bot may not do. Safe defaults, tuned by `bots:`.

    `max_replies_per_hour` bounds how many of a bot's messages reach the agent per hour,
    which is the same thing as bounding our replies: every delivered message is one the
    agent will answer. The cap is on delivery so a loop costs nothing before it is
    refused. A limit of 0 means this gateway does not talk to bots at all.
    """

    enabled: bool = True
    max_replies_per_hour: int = 6
    echo_window_s: int = 900
    digest_interval_s: int = 900


def _bot_rules(raw: dict[str, Any] | None) -> BotRules:
    raw = raw or {}
    limit = int(raw.get("max_replies_per_hour", 6))
    echo = parse_interval(raw.get("echo_window", "15m"))
    digest = parse_interval(raw.get("digest_interval", "15m"))
    if limit < 0:
        raise ValueError(f"bots.max_replies_per_hour must be >= 0, got {limit}")
    if echo is None or echo < 0:
        raise ValueError("bots.echo_window must be a duration >= 0")
    if digest is None or digest <= 0:
        raise ValueError("bots.digest_interval must be a positive duration")
    return BotRules(
        enabled=bool(raw.get("enabled", True)),
        max_replies_per_hour=limit,
        echo_window_s=int(echo),
        digest_interval_s=int(digest),
    )


@dataclass
class _TokenBucket:
    """Per-sender token bucket: burst at once, then refill at `per_hour`.

    Per *sender*, not per rule: a peer must not earn a fresh burst by tripping a
    different rule, and an allowlisted contact that starts flooding has to degrade
    to `blocked` frames rather than a queue that grows (spec §10.4). In-memory on
    purpose — a restart forgiving a flood is preferable to a durable table nobody
    reads, and the durable record is the blocked rows themselves.
    """

    burst: float
    per_hour: float
    clock: Callable[[], float]
    _tokens: dict[str, float] = field(default_factory=dict)
    _stamp: dict[str, float] = field(default_factory=dict)

    def allow(self, sender: str) -> bool:
        now = self.clock()
        tokens = self._tokens.get(sender, self.burst)
        last = self._stamp.get(sender, now)
        if self.per_hour > 0:
            tokens = min(self.burst, tokens + (now - last) * (self.per_hour / 3600))
        self._stamp[sender] = now
        if tokens < 1:
            self._tokens[sender] = tokens
            return False
        self._tokens[sender] = tokens - 1
        return True


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

    def __init__(self, config: dict[str, Any], clock: Callable[[], float] = time.time, *,
                 bot_lookup: Callable[[str], bool] | None = None,
                 echo_lookup: Callable[[str, int], Iterable[str]] | None = None):
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
        limits = config.get("rate_limit") or {}
        self.limiter: _TokenBucket | None = None
        if limits.get("enabled", True):
            burst = int(limits.get("burst", 10))
            per_hour = int(limits.get("per_hour", 30))
            if burst < 1 or per_hour < 0:
                raise ValueError(f"bad rate_limit: burst={burst}, per_hour={per_hour}")
            self.limiter = _TokenBucket(burst=burst, per_hour=per_hour, clock=clock)
        # Bot peers: the flag comes from kind-0 (bots.BotRegistry), the rules from the
        # `bots:` block. The cap is a per-sender window of deliveries, in memory for the
        # same reason the token bucket is: a restart forgiving one flood is better than a
        # durable table nobody prunes, and the refused rows are their own record.
        self.bot_rules = _bot_rules(config.get("bots"))
        self._bot_lookup = bot_lookup
        self._echo_lookup = echo_lookup
        self._bot_hits: dict[str, deque[float]] = {}
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

    def _match(self, *, group: str, kind: int, mention: bool, dm: bool) -> "_Rule | None":
        """The first rule matching this event, or None when only `defaults` apply."""
        return next(
            (rule for rule in self._rules
             if rule.matches(group=group, kind=kind, mention=mention, dm=dm)),
            None,
        )

    def authority_for(self, *, sender: str, kind: int, mention: bool = False,
                      dm: bool = False) -> str:
        """What *sender* may do with an event of this kind here: `instructions` or `none`.

        A reaction never reaches the agent as a message, but it can answer a gate, and that
        question — may this author instruct in this conversation? — is the same one a
        message asks. Only the attention half is skipped: `reach`, rule quotas and the
        per-sender bucket decide whether something is shown, not whether its author may
        instruct, and a read-only probe must not spend a delivery's allowance to ask.
        """
        rule = self._match(group=self.group_of(sender), kind=kind, mention=mention, dm=dm)
        return rule.authority if rule is not None else self.default.authority

    # -- decision -------------------------------------------------------
    def decide(
        self,
        *,
        sender: str,
        kind: int,
        mention: bool = False,
        dm: bool = False,
        event_id: str | None = None,
        text: str | None = None,
    ) -> Decision:
        now = self._clock()
        group = self.group_of(sender)
        bot = self._is_bot(sender)
        matched = self._match(group=group, kind=kind, mention=mention, dm=dm)

        # Dedupe first: the same event arriving on a second relay or a second plane
        # is one delivery, and must not burn a rate token or turn into a blocked replay.
        if event_id is not None:
            prior = self._seen.get(event_id)
            if prior is not None:
                candidate = self._decide(matched, sender, group, now, consume=False,
                                         text=text, bot=bot)
                if REACH_PRIORITY[candidate.reach] > REACH_PRIORITY[prior.reach]:
                    self._seen[event_id] = candidate
                    return replace(candidate, duplicate=True)
                return replace(prior, duplicate=True)

        chosen = self._decide(matched, sender, group, now, consume=True, text=text, bot=bot)
        if event_id is not None:
            self._seen[event_id] = chosen
        return chosen

    def _is_bot(self, sender: str) -> bool:
        if not self.bot_rules.enabled or self._bot_lookup is None:
            return False
        try:
            return bool(self._bot_lookup(sender))
        except Exception:  # noqa: BLE001 - a profile cache must not break delivery
            return False

    def _echoed(self, sender: str, text: str, now: float) -> bool:
        """Whether `text` is our own recent outbound message coming back to us."""
        if self._echo_lookup is None or not self.bot_rules.echo_window_s:
            return False
        wanted = normalise_text(text)
        if not wanted:
            return False
        try:
            sent = self._echo_lookup(sender, int(now - self.bot_rules.echo_window_s))
        except Exception:  # noqa: BLE001 - the store is evidence, not a dependency
            return False
        return any(normalise_text(item) == wanted for item in sent)

    def _bot_over_cap(self, sender: str, now: float, *, consume: bool) -> bool:
        """Count this delivery against the bot's hourly allowance, or refuse it."""
        limit = self.bot_rules.max_replies_per_hour
        hits = self._bot_hits.setdefault(sender, deque())
        while hits and now - hits[0] > BOT_WINDOW_S:
            hits.popleft()
        if len(hits) >= limit:
            return True
        if consume:
            hits.append(now)
        return False

    def _decide(self, rule: "_Rule | None", sender: str, group: str, now: float,
                *, consume: bool, text: str | None = None, bot: bool = False) -> Decision:
        """Turn a matched rule (or the default) into a decision, consuming quota once.

        A sender over its bucket loses the message to a `blocked` frame with
        `reason: rate_limited`; it never sits in a queue waiting for the window to
        roll, because a flood that is retried later is still a flood (spec §10.4).

        A bot peer is refused or downgraded *before* the bucket: an echo and a capped
        bot are not traffic we want spending the peer's allowance, and a bounce must not
        be able to exhaust it and then be treated as a victim of rate limiting.
        """
        if rule is not None and rule.over_quota(now):
            return Decision(
                reach="drop",
                interval_s=None,
                authority="none",
                rule=rule.name,
                why=f"quota {rule.limit}/h exceeded",
                blocked_reason="rate_limited",
            )
        if rule is None:
            base = self.default
        else:
            base = Decision(
                reach=rule.reach,
                interval_s=rule.interval_s,
                authority=rule.authority,
                rule=rule.name,
                why=f"matched {rule.name} (group {group})",
            )
        if bot:
            base = self._refine_for_bot(base, sender, now, text=text, consume=consume)
            if base.blocked_reason is not None:
                return base
        if consume:
            if self.limiter is not None and not self.limiter.allow(sender):
                return Decision(
                    reach="drop",
                    interval_s=None,
                    authority="none",
                    rule=base.rule,
                    why=f"per-sender rate limit exceeded ({base.rule})",
                    blocked_reason="rate_limited",
                )
            if rule is not None:
                rule.hit(now)
        return base

    def _refine_for_bot(self, base: Decision, sender: str, now: float,
                        *, text: str | None, consume: bool) -> Decision:
        """Echo, cap, then the digest floor — in that order, because each is cheaper.

        An echo is checked first: it is the tightest loop and the one that produced the
        3,370-row incident, so it should be named as an echo rather than as a cap.
        """
        if text and self._echoed(sender, text, now):
            return replace(
                base, reach="drop", interval_s=None,
                why=f"echo: identical to our own message within "
                    f"{self.bot_rules.echo_window_s}s",
                blocked_reason="echo",
            )
        if self._bot_over_cap(sender, now, consume=consume):
            return replace(
                base, reach="drop", interval_s=None,
                why=f"bot reply cap {self.bot_rules.max_replies_per_hour}/h reached",
                blocked_reason="bot_reply_cap",
            )
        if base.reach == "immediate":
            # A bot's mention must never jump the queue: it is a machine that can
            # repeat itself for ever, and the agent's attention is the scarce thing.
            return replace(
                base, reach="digest", interval_s=self.bot_rules.digest_interval_s,
                why=f"{base.why}; bot peer: immediate capped to digest",
            )
        return base

    def bot_status(self) -> dict[str, Any]:
        """The rules in force, and how many bot peers are inside the current window."""
        window = BOT_WINDOW_S
        now = self._clock()
        active = 0
        for hits in self._bot_hits.values():
            while hits and now - hits[0] > window:
                hits.popleft()
            active += bool(hits)
        return {
            "enabled": self.bot_rules.enabled,
            "max_replies_per_hour": self.bot_rules.max_replies_per_hour,
            "echo_window_s": self.bot_rules.echo_window_s,
            "digest_interval_s": self.bot_rules.digest_interval_s,
            "peers_counted": active,
        }

    def digest_due(self, interval_s: int | None, last_sent: float | None, now: float | None = None) -> bool:
        """Whether a digest window has closed. Callers own the batching; this owns the clock."""
        if interval_s is None:
            return False
        now = self._clock() if now is None else now
        return last_sent is None or (now - last_sent) >= interval_s
