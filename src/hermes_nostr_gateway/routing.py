"""Which provider, model and credential a conversation's turn should use.

A gateway serves several planes and many channels, and they are not the same kind of work:
a noisy public feed does not deserve the model a work channel gets, and a channel whose
usage has to be accountable needs its own credential. This module makes that decision and
nothing else. Hermes still owns how a provider and a model are talked to; the gateway's job
is to say which one a conversation asks for, and to refuse rather than guess.

Configuration, under `routing`:

```yaml
routing:
  models:                  # the catalogue: the only model names this config may use
    - model-a
    - model-b
  default: {provider: p, model: model-a, key: GATEWAY_MODEL_KEY}
  planes:
    concord: {provider: p, model: model-b, key: CONCORD_MODEL_KEY}
    nip17: {provider: p, model: model-a}
  channels:                # Concord only: a channel id (hex) or its current name
    - community: <community id>
      channel: <channel id or name>
      provider: p
      model: model-b
      key: WORK_CHANNEL_KEY
  # env_file: ~/.config/hermes-nostr-gateway/.env   # default: a .env beside the config
```

The decisions, in the order they matter:

**Precedence is channel > plane > default, and it is total.** `routing.default` is required
once `routing` is configured, so every conversation resolves to exactly one rule and
`hng route` can say which one won and what it beat. A channel rule matches on id before
name, so a renamed channel keeps its route; an entry with a `community` only matches that
community.

**A name that is not known is an error, not a guess.** `routing.models` lists the model
names this config may use, and a rule naming anything else is a load error. The gateway has
no business carrying a copy of a provider's catalogue, and inventing one is worse than
asking for it; a typo that fell back to a default would be a turn billed to the wrong model.
For the same reason a Concord channel rule is checked against the channels this gateway
actually holds: a channel id that no longer exists resolves to *unresolved*, never to the
plane's route — a route is configuration, not protocol, so deleting a channel does not
rewrite this file, and the stale entry has to be the thing that shows.

**Secrets are references.** `key:` is the name of an environment variable, or of an entry in
`routing.env_file`, never the value. The value is read when the turn is about to run and is
never logged, never put in a frame, never part of an error string. A name that cannot be read
fails the turn: it never falls back to another rule's key, because one channel's usage billed
to another's account is worse than a turn that does not happen.

**Nothing is applied here.** A resolved route is a request. The frame carries provider, model
and the key's *name*; the adapter that runs the turn honours it and reports back what it actually
used (see docs/decisions.md, "A route travels on the frame"). A digest batch spans many
conversations, so it has no single route: digests are not routed, and the agent's own model
answers them.

`hng route` prints the table, `hng route <conversation>` the winner for one conversation and
why, and `hng route --check` exits non-zero when the block cannot be loaded.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("hng.routing")

#: The planes a route may name. This is the set the daemon attaches, and a config naming
#: anything else is a load error rather than a route that silently never applies.
PLANES: tuple[str, ...] = ("nip17", "nip04", "nip01", "concord", "marmot")

#: `concord:<community>:<channel>` — the conversation an agent sees for a channel.
CONCORD_PREFIX = "concord:"

#: A model or provider name: one token the provider recognises. Deliberately permissive
#: about the shape (vendors use `.`, `/` and `-`) and strict about the edges.
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")

#: A key is a NAME: upper-case, so a pasted value (`sk-…`, a mixed-case token) cannot pass
#: for one. Length is bounded for the same reason — real variable names are short.
_KEY_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")

#: The fields a rule may carry. Anything else is a typo worth reporting, not a field to
#: ignore: `modell:` would otherwise leave the rule with no model at all.
RULE_FIELDS: tuple[str, ...] = ("provider", "model", "key")

#: Channel-rule identity fields, on top of the rule's own.
CHANNEL_IDENTITY: tuple[str, ...] = ("community", "channel")


class RouteKeyMissing(LookupError):
    """The variable a route names holds nothing. The message never carries a value."""

    def __init__(self, name: str, env_file: str = ""):
        self.name = name
        self.env_file = env_file
        where = f"the environment or {env_file}" if env_file else "the environment"
        super().__init__(f"${name} is not set in {where}")


class RoutingConfigError(RuntimeError):
    """The routing block cannot be honoured, so no turn can be routed correctly."""


@dataclass(frozen=True)
class KnownChannel:
    """One channel a plane holds, as routing needs it: who owns it and what it is called.

    `ChannelStream` from the Concord plane satisfies this, and so does a channel read out of a
    stored invite; routing only ever asks these three questions.
    """

    community_id: str = ""
    channel_id: str = ""
    name: str = ""


def env_value(name: str, env_file: str = "") -> str:
    """The value of *name* from the environment, else from *env_file*; "" when absent.

    Read at use time on purpose: a key rotated in the environment or in the file is picked
    up by the next turn, with no restart and no second copy to keep in step. The value is
    the caller's business and nobody else's — it is never logged here.
    """
    if not name:
        return ""
    value = os.environ.get(name, "")
    if value:
        return value
    if not env_file:
        return ""
    try:
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, raw = line.partition("=")
                if key.strip().removeprefix("export ").strip() == name:
                    return _unquote(raw.strip())
    except OSError:
        return ""
    return ""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


@dataclass(frozen=True)
class Route:
    """One rule's answer: what to ask for, and where the credential's name lives."""

    provider: str = ""
    model: str = ""
    #: The NAME of an environment (or `.env`) entry. Never a value.
    key: str = ""
    #: `env_file` resolved at load, so every process reads the same file the daemon checked.
    env_file: str = ""

    def key_available(self) -> bool:
        """Is the named variable readable right now? No name means no key to read."""
        return not self.key or bool(env_value(self.key, self.env_file))

    def key_value(self) -> str:
        """The credential itself, for the moment before a turn runs.

        Raises `RouteKeyMissing` rather than returning "": an empty key is how a call ends up
        silently using whatever credential the process already had.
        """
        if not self.key:
            return ""
        value = env_value(self.key, self.env_file)
        if not value:
            raise RouteKeyMissing(self.key, self.env_file)
        return value

    def as_dict(self) -> dict[str, str]:
        """The route as configuration describes it: names, never a secret."""
        return {"provider": self.provider, "model": self.model, "key": self.key}


@dataclass(frozen=True)
class ChannelRule:
    """A Concord channel's own route, scoped to a community when one is named."""

    #: The community id, or "" for any community.
    community: str
    #: The channel's hex id or its current name; an id match beats a name match.
    channel: str
    route: Route
    #: Where this entry sits in the config, for `hng route` and the frame's audit trail.
    origin: str = ""


@dataclass(frozen=True)
class Resolution:
    """What one conversation resolves to, and why — the answer `hng route` prints.

    `error` is set when the winning rule cannot be honoured, and then `route` is empty on
    purpose: a caller must not be able to use a route that was refused.
    """

    route: Optional[Route] = None
    #: Which rule won: `channel`, `plane`, `default`, `validation` or `none`.
    rule: str = "none"
    #: The rule that won, in words, including what it beat.
    why: str = ""
    #: `route_key_missing`, `route_unresolved` or `route_invalid`; empty when usable.
    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.route is not None and not self.error

    def to_frame(self) -> dict[str, str]:
        """The frame field: the route, or the reason there is none. Never a value."""
        if self.error:
            return {"error": self.error, "detail": self.detail, "rule": self.rule}
        if self.route is None:
            return {}
        frame = {**self.route.as_dict(), "rule": self.rule, "why": self.why}
        if self.route.key and self.route.env_file:
            # The file's *path*, never a value: the process that runs the turn reads the same
            # place this daemon checked, so the two cannot disagree about where to look.
            frame["env_file"] = self.route.env_file
        return frame

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "why": self.why,
            "provider": self.route.provider if self.route else "",
            "model": self.route.model if self.route else "",
            "key": self.route.key if self.route else "",
            "key_present": bool(self.route and self.route.key_available()),
            "ok": self.ok,
            "error": self.error,
            "detail": self.detail,
        }


def _name_problem(raw: str, *, field_name: str) -> str:
    return f"{field_name} {raw!r} is not a name this gateway can use"


def _key_problem(raw: str) -> str:
    """Why *raw* cannot be a key name. Never echoes it: it may be a pasted secret."""
    if not _KEY_NAME.match(raw):
        return ("key must be the NAME of an environment variable (upper-case letters, digits "
                "and underscores), never a key value")
    # A long name with no separator is how a pasted hex/base32 token looks, and a token is
    # the one thing that must not be stored here. Refuse it without printing it.
    if len(raw) >= 40 and "_" not in raw:
        return ("key looks like a value rather than a variable name: give the name of the "
                "variable that holds it")
    return ""


def _rule_from(raw: Any, *, origin: str, models: tuple[str, ...],
               env_file: str, allowed: Iterable[str] = ()) -> tuple[Optional[Route], list[str]]:
    """One rule -> (route, problems). A rule with any problem yields no route."""
    if not isinstance(raw, dict):
        return None, [f"{origin}: expected a mapping with provider, model and key"]
    problems: list[str] = []
    unknown = sorted(set(map(str, raw)) - set(RULE_FIELDS) - set(allowed))
    if unknown:
        problems.append(f"{origin}: unknown field(s) {', '.join(unknown)} "
                        f"(expected {', '.join((*RULE_FIELDS, *allowed))})")
    provider = str(raw.get("provider") or "").strip()
    model = str(raw.get("model") or "").strip()
    key = str(raw.get("key") or "").strip()
    if not provider:
        problems.append(f"{origin}: names no provider")
    elif not _NAME.match(provider):
        problems.append(_name_problem(provider, field_name=f"{origin}: provider"))
    if not model:
        problems.append(f"{origin}: names no model")
    elif not _NAME.match(model):
        problems.append(_name_problem(model, field_name=f"{origin}: model"))
    elif models and model not in models:
        # An unusable catalogue is reported once, by `routing.models`; every rule repeating it
        # would bury the cause under its consequences.
        problems.append(f"{origin}: model {model!r} is not declared in routing.models "
                        f"({', '.join(sorted(models))})")
    if key:
        problem = _key_problem(key)
        if problem:
            problems.append(f"{origin}: {problem}")
    if problems:
        return None, problems
    return Route(provider=provider, model=model, key=key, env_file=env_file), []


def _models_from(raw: Any) -> tuple[tuple[str, ...], list[str]]:
    """`routing.models` -> the catalogue, plus what is wrong with it."""
    if raw is None:
        return (), ["routing.models is required: list every model name this config may route "
                    "to, so a typo is a load error instead of a turn on the wrong model"]
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        return (), [f"routing.models: expected a list of model names, got {type(raw).__name__}"]
    models: list[str] = []
    problems: list[str] = []
    for entry in raw:
        name = str(entry or "").strip()
        if not name or not _NAME.match(name):
            problems.append(f"routing.models: {name!r} is not a model name")
        elif name in models:
            problems.append(f"routing.models: {name!r} is listed twice")
        else:
            models.append(name)
    if not models and not problems:
        problems.append("routing.models is empty: list the model names this config may use")
    return tuple(models), problems


class Router:
    """Resolves a conversation to a route. Empty config resolves nothing, never guesses."""

    def __init__(self, *, configured: bool = False, default: Optional[Route] = None,
                 planes: Optional[dict[str, Route]] = None,
                 channels: Optional[list[ChannelRule]] = None,
                 models: tuple[str, ...] = (), env_file: str = "",
                 problems: Iterable[str] = ()):
        self.configured = configured
        self.default = default
        self.planes: dict[str, Route] = dict(planes or {})
        self.channels: list[ChannelRule] = list(channels or [])
        self.models = tuple(models)
        self.env_file = env_file
        self.problems: tuple[str, ...] = tuple(problems)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_config(cls, raw: Any, *, config_path: Optional[str | Path] = None) -> "Router":
        """Parse the `routing` block. Nothing is raised: problems are collected and reported.

        A read-only verb (`hng status`, `hng drops`) must not die because the routing block
        is broken, and `hng serve` refuses to start instead — the same split as `concord`'s
        project mapping, with the difference that a broken route is fatal to a *turn*, so
        the daemon will not run with one.
        """
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls(configured=True,
                       problems=[f"routing: expected a mapping, got {type(raw).__name__}"])
        env_file = str(raw.get("env_file") or "").strip()
        env_file = str(Path(os.path.expanduser(env_file)).absolute()) if env_file else ""
        if not env_file and config_path:
            # A `.env` beside the config is the default, because that is where the host's
            # other secrets already live. Only ever read by exact name.
            env_file = str((Path(config_path).absolute().parent / ".env"))
        problems: list[str] = []
        unknown = sorted(set(map(str, raw)) - {"models", "default", "planes", "channels",
                                               "env_file"})
        if unknown:
            problems.append(f"routing: unknown field(s) {', '.join(unknown)} (expected models, "
                            "default, planes, channels, env_file)")
        models, model_problems = _models_from(raw.get("models"))
        problems.extend(model_problems)

        default, default_problems = _rule_from(raw.get("default"), origin="routing.default",
                                               models=models, env_file=env_file)
        problems.extend(default_problems)
        if raw.get("default") is None:
            problems.append("routing.default is required once routing is configured: it is the "
                            "rule every conversation falls back to")

        planes: dict[str, Route] = {}
        raw_planes = raw.get("planes") or {}
        if not isinstance(raw_planes, dict):
            problems.append(f"routing.planes: expected a mapping, got "
                            f"{type(raw_planes).__name__}")
        else:
            for name, block in raw_planes.items():
                plane = str(name or "").strip().lower()
                origin = f"routing.planes.{plane}"
                if plane not in PLANES:
                    problems.append(f"{origin}: {name!r} is not a plane this gateway attaches "
                                    f"({', '.join(PLANES)})")
                    continue
                route, rule_problems = _rule_from(block, origin=origin, models=models,
                                                  env_file=env_file)
                problems.extend(rule_problems)
                if route is not None:
                    planes[plane] = route

        channels: list[ChannelRule] = []
        raw_channels = raw.get("channels") or []
        if isinstance(raw_channels, dict):
            problems.append("routing.channels: expected a list of entries, not a mapping")
            raw_channels = []
        if not isinstance(raw_channels, (list, tuple)):
            problems.append(f"routing.channels: expected a list, got "
                            f"{type(raw_channels).__name__}")
            raw_channels = []
        for position, entry in enumerate(raw_channels):
            origin = f"routing.channels[{position}]"
            if not isinstance(entry, dict):
                problems.append(f"{origin}: expected a mapping with community, channel, "
                                "provider and model")
                continue
            unknown = sorted(set(map(str, entry)) - set(RULE_FIELDS) - set(CHANNEL_IDENTITY))
            if unknown:
                problems.append(f"{origin}: unknown field(s) {', '.join(unknown)} "
                                f"(expected {', '.join((*CHANNEL_IDENTITY, *RULE_FIELDS))})")
            community = str(entry.get("community") or "").strip().lower()
            channel = str(entry.get("channel") or "").strip()
            if not channel:
                problems.append(f"{origin}: names no channel (a channel id or its name)")
            route, rule_problems = _rule_from(entry, origin=origin, models=models,
                                              env_file=env_file,
                                              allowed=CHANNEL_IDENTITY)
            problems.extend(rule_problems)
            if route is None or not channel:
                continue
            duplicate = next((seen for seen in channels
                              if seen.community == community
                              and seen.channel.strip().lower() == channel.lower()), None)
            if duplicate is not None:
                problems.append(f"{origin}: {channel!r} in community "
                                f"{community or '(any)'} is already routed by "
                                f"{duplicate.origin}; keeping that one")
                continue
            channels.append(ChannelRule(community=community, channel=channel,
                                        route=route, origin=origin))

        return cls(configured=True, default=default, planes=planes, channels=channels,
                   models=models, env_file=env_file, problems=problems)

    # -- questions the gateway asks -------------------------------------

    def __bool__(self) -> bool:
        return self.configured

    @property
    def usable(self) -> bool:
        """Configured and loadable: `hng serve` runs only with this true."""
        return self.configured and not self.problems and self.default is not None

    def resolve(self, plane: str = "", conversation: str = "", *,
                known_channels: Optional[Iterable[Any]] = None) -> Resolution:
        """The route for one conversation, with the rule that won and what it beat.

        `known_channels` is what the Concord plane holds right now (objects with
        `community_id`, `channel_id` and `name`). `None` means "not known yet" — the check
        is skipped rather than guessing — while an empty list means "known to hold nothing".
        """
        if not self.configured:
            return Resolution(rule="none",
                              why="routing is not configured: the turn uses the agent's own "
                                  "model and credential")
        if self.problems or self.default is None:
            return Resolution(rule="validation", error="route_invalid",
                              detail="; ".join(self.problems) or "routing.default is missing")
        plane = str(plane or "").strip().lower()

        if plane == "concord" and self.channels:
            parsed = _parse_concord(conversation)
            if parsed is not None:
                community, channel = parsed
                known = _known_channels(known_channels) if known_channels is not None else None
                if known is not None and not _holds(known, community, channel):
                    # The rule is not consulted: a route for a room this gateway does not
                    # hold is the stale entry to show, not a plane default to hide behind.
                    return Resolution(
                        rule="channel", error="route_unresolved",
                        detail=f"no channel {channel} in community {community or '(any)'} "
                               "among the channels this gateway holds")
                matched = self._match_channel(community, channel, known)
                if matched is not None:
                    return self._resolution(matched.route, "channel",
                                            f"channel rule {matched.origin} "
                                            f"{self._beaten('channel', plane)}")

        route = self.planes.get(plane)
        if route is not None:
            return self._resolution(route, "plane",
                                    f"routing.planes.{plane} {self._beaten('plane', plane)}")
        return self._resolution(self.default, "default",
                                "routing.default (no channel or plane rule applies)")

    def _resolution(self, route: Route, rule: str, why: str) -> Resolution:
        """A winning rule, unless its key cannot be read — then the turn fails closed.

        Never replaced by another rule's route: the whole point of naming a key is that the
        usage lands on that key's account.
        """
        if not route.key_available():
            return Resolution(rule=rule, why=why, error="route_key_missing",
                              detail=f"${route.key} is not set"
                                     + (f" in the environment or {route.env_file}"
                                        if route.env_file else " in the environment"))
        return Resolution(route=route, rule=rule, why=why)

    def _match_channel(self, community: str,
                       channel: str, known: Optional[dict[str, dict[str, str]]]) -> Optional[ChannelRule]:
        """The channel rule for this room: an id match beats a name match, as in projects."""
        ident = channel.strip().lower()
        name = ""
        if known is not None:
            entry = known.get(f"{community}:{ident}")
            name = (entry or {}).get("name", "").strip().lower()

        def scoped(rule: ChannelRule) -> bool:
            return not rule.community or rule.community == community

        for rule in self.channels:
            if scoped(rule) and rule.channel.strip().lower() == ident:
                return rule
        if not name:
            return None
        for rule in self.channels:
            if scoped(rule) and rule.channel.strip().lower() == name:
                return rule
        return None

    def _beaten(self, rule: str, plane: str) -> str:
        """What a winning rule outranks, so `hng route` explains itself."""
        below: list[str] = []
        if rule == "channel" and plane in self.planes:
            below.append(f"routing.planes.{plane}")
        if rule in ("channel", "plane"):
            below.append("routing.default")
        return f"beats {' and '.join(below)}" if below else "is the only rule"

    def channel_problems(self, known: Iterable[Any]) -> list[str]:
        """Channel rules that name a channel this gateway does not hold.

        `hng route --check` asks this: a rule for a room that no longer exists would otherwise
        sit there unnoticed until a turn resolved through a fallback somebody did not choose.
        """
        held = _known_channels(known)
        problems: list[str] = []
        for rule in self.channels:
            ident = rule.channel.strip().lower()
            if f"{rule.community}:{ident}" in held:
                continue
            names = {entry["name"].strip().lower() for key, entry in held.items()
                     if not rule.community or key.startswith(f"{rule.community}:")}
            if ident in names:
                continue
            where = f"community {rule.community}" if rule.community else "any community"
            problems.append(f"{rule.origin}: no channel {rule.channel!r} is held in {where}")
        return problems

    def describe(self) -> list[str]:
        """The whole table as lines, for `hng route` with no conversation."""
        if not self.configured:
            return ["routing is not configured: every turn uses the agent's own model and "
                    "credential"]
        lines: list[str] = []
        for problem in self.problems:
            lines.append(f"config: {problem}")
        if self.default is not None:
            lines.append(f"  {'default':<22} {_rule_line(self.default)}")
        for plane in sorted(self.planes):
            lines.append(f"  {'planes.' + plane:<22} {_rule_line(self.planes[plane])}")
        for rule in self.channels:
            where = f"{rule.channel} @ {rule.community or 'any community'}"
            lines.append(f"  {(rule.origin or 'channel'):<22} {where}  "
                         f"{_rule_line(rule.route)}")
        if self.env_file:
            lines.append(f"  keys are looked up in the environment, then {self.env_file}")
        return lines


def _rule_line(route: Route) -> str:
    key = f"key ${route.key} ({'set' if route.key_available() else 'NOT SET'})" if route.key \
        else "no key named: the agent's own credential"
    return f"{route.provider}/{route.model}  {key}"


def _parse_concord(conversation: str) -> Optional[tuple[str, str]]:
    """`concord:<community>:<channel>` -> (community, channel), or None for another shape."""
    text = str(conversation or "").strip()
    if not text.lower().startswith(CONCORD_PREFIX):
        return None
    parts = text[len(CONCORD_PREFIX):].split(":")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip().lower(), parts[1].strip()


def _known_channels(channels: Iterable[Any]) -> dict[str, dict[str, str]]:
    """`community:channel id` -> {name}, from the plane's channel objects.

    Duck-typed on purpose: routing knows nothing about how a plane reads a channel, only what
    one is called.
    """
    known: dict[str, dict[str, str]] = {}
    for channel in channels or ():
        community = str(getattr(channel, "community_id", "") or "").strip().lower()
        ident = str(getattr(channel, "channel_id", "") or "").strip().lower()
        if not ident:
            continue
        known[f"{community}:{ident}"] = {"name": str(getattr(channel, "name", "") or "")}
    return known


def _holds(known: dict[str, dict[str, str]], community: str, channel: str) -> bool:
    return f"{community}:{channel.strip().lower()}" in known


def conversation_plane(conversation: str) -> str:
    """The plane a conversation string names, as far as its shape says.

    `concord:...` is a channel, `marmot:...` a group, a 64-hex or `npub…` a DM peer. Anything
    else is left to the caller: guessing a plane is how a route is applied to the wrong thing.
    """
    text = str(conversation or "").strip()
    lowered = text.lower()
    if lowered.startswith(CONCORD_PREFIX):
        return "concord"
    if lowered.startswith("marmot:"):
        return "marmot"
    if len(text) == 64 and all(c in "0123456789abcdef" for c in lowered):
        return "nip17"
    if lowered.startswith("npub1"):
        return "nip17"
    return ""


def known_channels_from_invites(rows: Iterable[dict[str, Any]]) -> list[KnownChannel]:
    """The channels a set of stored Concord invites grants, unioned per community.

    The daemon learns its channel list from the Control plane at refresh; a `hng route` run has
    no live plane, so this reads the same grants out of what the invites recorded — the same
    source `refresh_concord` unions from, without a network round trip.
    """
    found: dict[tuple[str, str], KnownChannel] = {}
    for row in rows or ():
        bundle = row.get("bundle") or {}
        community = str(row.get("community_id") or bundle.get("community_id") or "").lower()
        for channel in bundle.get("channels") or []:
            ident = str((channel or {}).get("id") or "").strip()
            if not ident:
                continue
            found.setdefault((community, ident.lower()), KnownChannel(
                community_id=community, channel_id=ident,
                name=str((channel or {}).get("name") or "")))
    return list(found.values())


def load_router(raw: Any, *, config_path: Optional[str | Path] = None) -> Router:
    """`Router.from_config`, refusing rather than returning a router nothing can be routed by.

    `hng serve` uses this: a routing block that would bill a turn to the wrong model or to the
    wrong key's account is not something to start and then warn about. Read-only verbs use
    `from_config` and report `problems` instead of dying.
    """
    router = Router.from_config(raw, config_path=config_path)
    if router.problems:
        raise RoutingConfigError("; ".join(router.problems))
    return router


def reported_route(raw: Any) -> dict[str, str]:
    """A route a client says a turn ran on, reduced to what may be stored and echoed.

    The adapter reports the route it applied with its `send`, so the reply row records what the
    turn actually asked for rather than only what the gateway intended. This is untrusted input
    from a local client, so it is trimmed to known fields with bounded lengths, and a `key` that
    does not look like a name is dropped without being echoed: a value must not reach a row.
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for name in ("provider", "model"):
        value = str(raw.get(name) or "").strip()
        if value and _NAME.match(value):
            out[name] = value
    rule = str(raw.get("rule") or "").strip().lower()
    if rule in ("channel", "plane", "default"):
        out["rule"] = rule
    key = str(raw.get("key") or "").strip()
    if key and not _key_problem(key):
        out["key"] = key
    why = str(raw.get("why") or "").strip()
    if why:
        out["why"] = why[:300]
    return out
