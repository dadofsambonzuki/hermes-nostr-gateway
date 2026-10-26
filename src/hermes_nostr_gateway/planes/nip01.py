"""nip01 plane: public mentions and watched feeds.

The failure mode here is a firehose, not a missing message: kind 1 at volume into an
LLM costs money and attention. So this plane knows *why* an event is interesting —
mentioned, watched author, keyword, hashtag — and hands that reason to the attention
policy, which decides immediate, digest or drop (spec §17).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..policy import Policy
from ..store import Inbound, Store

MENTION_KINDS = {1}
NOTE_KIND = 1


@dataclass
class FeedRule:
    name: str
    authors: set[str]
    keywords: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    reach: str | None = None


def feed_rules(cfg: dict[str, Any] | list[dict[str, Any]] | None) -> list[FeedRule]:
    """Accept either the whole config or just the feeds list."""
    raw_rules = cfg.get("feeds") if isinstance(cfg, dict) else cfg
    rules = []
    for raw in raw_rules or []:
        rules.append(
            FeedRule(
                name=raw.get("name") or "feed",
                authors={a for a in raw.get("authors") or []},
                keywords=tuple(k.lower() for k in raw.get("keywords") or []),
                hashtags=tuple(h.lower().lstrip("#") for h in raw.get("hashtags") or []),
                reach=raw.get("reach"),
            )
        )
    return rules


class Nip01Plane:
    def __init__(self, store: Store, policy: Policy, me: str, rules: Iterable[FeedRule] = ()):
        self.store = store
        self.policy = policy
        self.me = me
        self.rules = list(rules)
        self.authors = sorted({a for r in self.rules for a in r.authors})

    # -- matching -------------------------------------------------------
    def mentions_me(self, event: dict) -> bool:
        return any(tag and tag[0] == "p" and len(tag) > 1 and tag[1] == self.me
                   for tag in event.get("tags", []))

    def matching_rule(self, event: dict) -> FeedRule | None:
        author = event.get("pubkey", "")
        content = (event.get("content") or "").lower()
        tags = event.get("tags") or []
        hashtags = {t[1].lower() for t in tags if t and t[0] == "t" and len(t) > 1}
        for rule in self.rules:
            if author in rule.authors:
                return rule
            if rule.keywords and any(k in content for k in rule.keywords):
                return rule
            if rule.hashtags and hashtags & set(rule.hashtags):
                return rule
        return None

    def why(self, event: dict) -> str | None:
        if self.mentions_me(event):
            return "mention"
        rule = self.matching_rule(event)
        return f"feed:{rule.name}" if rule else None

    # -- handling -------------------------------------------------------
    def handle(self, relay: str, event: dict) -> Inbound | None:
        if event.get("kind") not in MENTION_KINDS:
            return None
        sender = event.get("pubkey", "")
        if sender == self.me:
            return None
        reason = self.why(event)
        if reason is None:
            return None
        event_id = event.get("id") or ""
        decision = self.policy.decide(
            sender=sender, kind=event["kind"], mention=reason == "mention", event_id=event_id,
            text=event.get("content", ""),
        )
        if decision.duplicate:
            return None
        msg = Inbound(
            id=event_id,
            wrapper_id=event_id,
            sender=sender,
            kind=event["kind"],
            created_at=int(event.get("created_at", 0)),
            content=event.get("content", ""),
            conversation=reason,
            reply_to=next((t[1] for t in event.get("tags", []) if t and t[0] == "e"), None),
            plane="nip01",
            reach=decision.reach,
            authority=decision.authority,
            rule=decision.rule,
            why=f"{decision.why}; {reason}",
            blocked_reason=decision.blocked_reason,
            interval_s=decision.interval_s,
        )
        return msg if self.store.add(msg) else None
