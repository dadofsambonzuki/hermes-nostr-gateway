"""Which Hermes profile a conversation's turn runs as.

A Hermes profile is a whole instance, not a setting: its own config, sessions, skills and
memory, rooted at its own ``HERMES_HOME`` (``~/.hermes`` for ``default``,
``~/.hermes/profiles/<name>/`` for a named one). Some traffic is not the same agent's work as
the rest, and this module is how one plane or one Concord channel gets an identity of its own
while the same daemon serves everything else as the default profile.

**What is Hermes' job, and what is this file's.** Hermes already routes a *platform chat* to a
profile: ``gateway.profile_routes`` matches on platform + guild/chat/thread/user, and the
routed turn resolves that profile's config, skills, memory, SOUL and provider keys, writes to
``profiles/<name>/state.db``, and keys the session ``agent:<profile>:...``. An explicit route
whose target is not served is *rejected*, never run as the default profile. None of that is
re-implemented here.

What Hermes cannot key on is a **plane** or a **Concord channel by name**:
``concord:<community>:<channel>``, a Marmot group and a NIP-17 peer are names this gateway
defines, and its own config is the only place they are declared. Copying the route list into
Hermes' ``profile_routes`` would be a second copy that drifts out of step exactly when it
matters -- the same reason the Marmot ownership guard reads Hermes'
``platforms.marmot.enabled`` rather than holding its own copy.

So: this module resolves the route for this gateway's conversations, the daemon stamps the
answer on the frame, and the Hermes plugin sets it on the ``SessionSource`` -- the same field
``profile_routes`` would have set. Nothing creates, configures or supervises a profile
(``hermes profile create`` does), and every profile still replies as *this* gateway's Nostr
identity: a profile is an agent identity, not a second key.

Configuration, under ``profiles``:

```yaml
profiles:
  default: default                 # required with the block: every conversation ends here
  # root: ~/.hermes/profiles       # optional; default: profiles/ beside Hermes' config.yaml
  planes:
    marmot: work
  channels:
    - community: <community id>    # optional; restrict this rule to one community
      channel: <channel id or name>
      profile: ops
```

The decisions, in the order they matter:

**Precedence is channel > plane > default, and it is total.** ``profiles.default`` is required
once the block exists, so every conversation resolves to exactly one rule and ``hng route`` can
say which one won and what it beat. A channel rule matches on id before name, so a renamed
channel keeps its profile; a rule with ``community`` only matches that community; the first
entry wins a tie. A Concord rule is checked against the channels this gateway actually holds:
a rule for a room it no longer holds resolves to *unresolved*, never to the plane's profile --
deleting a channel does not rewrite this file, so the stale entry has to be the thing that
shows.

**A profile that is not there is a load error, not a first-message surprise.** The name must
be one Hermes accepts, the directory must be a live profile (an identity file, and no
tombstone), it must not opt out of the host gateway with ``gateway.standalone: true``, and
Hermes must be multiplexing -- a non-multiplexed gateway keeps the legacy ``agent:main``
namespace and ignores a routed profile, so a route would silently run as the default. ``hng
serve`` refuses to start with any of those; ``hng profiles --check`` reports the same without
starting anything. A parked profile is a warning, not a refusal: parking is temporary, and the
gateway must keep serving the profiles that are not parked.

**Nothing is applied here.** A resolved route is a request. The frame carries the profile's
*name*; the plugin that runs the turn honours it. If the profile disappears while the daemon
runs, the frame is refused at delivery (``profile_unknown``) rather than delivered without it,
because Hermes' own resolver would fall back to the default home with only a log line.

**A route change never migrates a session.** Hermes keys a session by profile, so pointing a
channel at another profile starts a new session there and leaves the old one where it was.
That is the isolation boundary, and it is why the daemon never needs to know about sessions.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import yaml

from .health import BASE_PLANES

log = logging.getLogger("hng.profiles")

#: Every plane a route may name: the ones the daemon attaches, plus the opt-in Marmot plane.
PLANES: tuple[str, ...] = (*BASE_PLANES, "marmot")

#: `concord:<community>:<channel>` -- the conversation an agent sees for a channel.
CONCORD_PREFIX = "concord:"

DEFAULT_PROFILE = "default"

#: Hermes' profile-id grammar (`hermes_constants.PROFILE_ID_RE`). Copied rather than imported:
#: this gateway must refuse exactly what Hermes would refuse, and it does not import Hermes.
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: The files that make a directory a profile rather than a stray directory under `profiles/`
#: (`hermes_constants._PROFILE_IDENTITY_MARKERS`, for the same reason).
_IDENTITY_MARKERS = ("config.yaml", ".env", "SOUL.md", "profile.yaml", "auth.json", "state.db")

#: Where Hermes leaves a tombstone when a profile is deleted. The directory may still exist.
_TOMBSTONE_DIR = ".deleted"

#: The Hermes key a routed profile depends on. Named once so the error, the log and `hng
#: profiles --check` all quote the same setting.
HERMES_MULTIPLEX_KEY = "gateway.multiplex_profiles"


class ProfilesConfigError(RuntimeError):
    """The profiles block cannot be honoured, so no turn can be routed correctly."""


def hermes_config_path(cfg: dict[str, Any]) -> Path:
    """Where Hermes' own config lives, as seen by this process.

    `hermes_config` wins (a gateway in a container or beside a second Hermes home needs that),
    then `$HERMES_HOME`, then the default `~/.hermes/config.yaml`. The same rule the Marmot
    guard uses, and the parent directory is where `profiles/` lives by default.
    """
    configured = cfg.get("hermes_config")
    if configured:
        return Path(os.path.expanduser(str(configured)))
    home = os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(os.path.expanduser(home)) / "config.yaml"


def _read_yaml(path: Path) -> dict[str, Any]:
    """A YAML mapping, or {} for anything that is not one. Never raises at a caller."""
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def hermes_profile_state(cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether Hermes multiplexes profiles, and how we know.

    Answers ``{multiplex, source, error?}`` where `multiplex` is True, False, or None for "no
    evidence either way". An *unset* key is not "off": Hermes treats it as a request it settles
    at boot, so it comes back as None and the caller reports it rather than assuming. Unreadable
    is unknown too -- a guard that fails open silently would be worse than no guard.
    """
    path = hermes_config_path(cfg)
    if not path.is_file():
        return {"multiplex": None, "source": f"no Hermes config at {path}"}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        return {"multiplex": None, "source": str(path), "error": str(exc)[:200]}
    if not isinstance(data, dict):
        return {"multiplex": None, "source": str(path),
                "error": f"expected a mapping, got {type(data).__name__}"}
    raw_gateway = data.get("gateway")
    gateway = raw_gateway if isinstance(raw_gateway, dict) else {}
    # The flag is also accepted at the top level for convenience (Hermes documents both).
    if "multiplex_profiles" in gateway:
        value = gateway["multiplex_profiles"]
    elif "multiplex_profiles" in data:
        value = data["multiplex_profiles"]
    else:
        return {"multiplex": None,
                "source": f"{path} sets no {HERMES_MULTIPLEX_KEY} (it defaults on)"}
    if not isinstance(value, bool):
        return {"multiplex": None, "source": str(path),
                "error": f"{HERMES_MULTIPLEX_KEY} is {value!r}, not true or false"}
    return {"multiplex": value, "source": str(path)}


@dataclass(frozen=True)
class ProfileHome:
    """One profile's `HERMES_HOME`, and everything that can stop a turn running as it."""

    name: str
    path: Path
    #: Why a turn cannot run as this profile; "" when it can.
    problem: str = ""
    #: The profile opted out of the host gateway (`gateway.standalone: true`).
    standalone: bool = False
    #: `gateway.parked` exists: served again on `hermes -p <name> gateway start`.
    parked: bool = False

    @property
    def live(self) -> bool:
        return not self.problem

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "path": str(self.path), "live": self.live,
                "problem": self.problem, "standalone": self.standalone,
                "parked": self.parked}


def profile_home(name: str, *, root: str | Path, default_home: str | Path,
                 read_config: bool = True) -> ProfileHome:
    """Resolve a profile name to its home, and say why it is unusable when it is.

    `root` is the `profiles/` directory; `default_home` is the launch home (`HERMES_HOME`
    itself), which is the `default` profile. Answers a `problem` rather than raising: a broken
    route is reported to the operator, and `hng serve` is what turns it into a refusal.

    `read_config=False` skips the profile's own `config.yaml` (the `gateway.standalone` check)
    and answers only what the filesystem says. That is the hot-path re-check, run once per
    routed message: a profile deleted while the daemon runs has to be caught, and parsing
    YAML on every message is not.
    """
    profiles = Path(os.path.expanduser(str(root))).absolute()
    if name == DEFAULT_PROFILE:
        return ProfileHome(name=name, path=Path(os.path.expanduser(str(default_home))).absolute())
    home = profiles / name
    if not _PROFILE_NAME.match(name):
        return ProfileHome(name=name, path=home, problem=(
            f"{name!r} is not a Hermes profile name (lower-case letters, digits, '_' and '-', "
            "64 characters at most)"))
    if (profiles / _TOMBSTONE_DIR / name).exists():
        return ProfileHome(name=name, path=home, problem=(
            f"profile {name!r} was deleted ({profiles / _TOMBSTONE_DIR / name}); "
            f"`hermes profile create {name}` brings a live one back"))
    if not home.is_dir():
        return ProfileHome(name=name, path=home, problem=(
            f"no profile {name!r} at {home}: `hermes profile create {name}` makes one"))
    if not any((home / marker).is_file() or (home / marker).is_symlink()
               for marker in _IDENTITY_MARKERS):
        return ProfileHome(name=name, path=home, problem=(
            f"{home} is a directory but not a Hermes profile (no "
            f"{', '.join(_IDENTITY_MARKERS)})"))
    if not read_config:
        return ProfileHome(name=name, path=home)
    gateway = _read_yaml(home / "config.yaml").get("gateway")
    standalone = bool(isinstance(gateway, dict) and gateway.get("standalone") is True)
    parked = (home / "gateway.parked").exists()
    problem = ""
    if standalone:
        # The host gateway does not serve a profile that opted out, so a routed turn could
        # only run half of it -- the home, without the config the host never loaded.
        problem = (f"profile {name!r} sets gateway.standalone: true, so the host gateway does "
                   "not serve it and a routed turn cannot run as it")
    return ProfileHome(name=name, path=home, problem=problem, standalone=standalone,
                       parked=parked)


@dataclass(frozen=True)
class ChannelRule:
    """One Concord channel's profile, scoped to a community when one is named."""

    #: The community id, or "" for any community.
    community: str
    #: The channel's hex id or its current name; an id match beats a name match.
    channel: str
    profile: str
    #: Where this entry sits in the config, for `hng profiles` and `hng route`.
    origin: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"community": self.community, "channel": self.channel,
                "profile": self.profile, "origin": self.origin}


@dataclass(frozen=True)
class Resolution:
    """What one conversation resolves to, and why -- the answer `hng route` prints.

    `error` is set when the winning rule cannot be honoured, and then `profile` is empty on
    purpose: a caller must not be able to use a route that was refused.
    """

    profile: str = ""
    #: Which rule won: `channel`, `plane`, `default`, `validation` or `none`.
    rule: str = "none"
    #: The rule that won, in words, including what it beat.
    why: str = ""
    #: `profile_unknown`, `profile_unserved`, `route_unresolved` or `profile_invalid`.
    error: str = ""
    detail: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def frame(self) -> dict[str, str]:
        """The frame field: the profile, or nothing when the turn runs as the default.

        Absent means "the receiving profile", which is what keeps the wire unchanged for every
        conversation this block does not route.
        """
        if self.error or not self.profile or self.profile == DEFAULT_PROFILE:
            return {}
        return {"profile": self.profile}

    def as_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "rule": self.rule, "why": self.why,
                "ok": self.ok, "error": self.error, "detail": self.detail}


def _parse_concord(conversation: str) -> Optional[tuple[str, str]]:
    """`concord:<community>:<channel>` -> (community, channel), or None for another shape."""
    text = str(conversation or "").strip()
    if not text.lower().startswith(CONCORD_PREFIX):
        return None
    parts = text[len(CONCORD_PREFIX):].split(":")
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    return parts[0].strip().lower(), parts[1].strip()


def _looks_like_id(value: str) -> bool:
    """A channel id: 64 hex characters. A name is anything else."""
    text = str(value or "").strip().lower()
    return len(text) == 64 and all(char in "0123456789abcdef" for char in text)


def _known_channels(channels: Iterable[Any]) -> dict[str, dict[str, str]]:
    """`community:channel id` -> {name}, from the plane's channel objects.

    Duck-typed on purpose: routing knows nothing about how a plane reads a channel, only what
    one is called (`ChannelStream` satisfies this).
    """
    known: dict[str, dict[str, str]] = {}
    for channel in channels or ():
        community = str(getattr(channel, "community_id", "") or "").strip().lower()
        ident = str(getattr(channel, "channel_id", "") or "").strip().lower()
        if not ident:
            continue
        known[f"{community}:{ident}"] = {"name": str(getattr(channel, "name", "") or "")}
    return known


@dataclass(frozen=True)
class HeldChannel:
    """One channel a stored invite grants, as a route check needs it."""

    community_id: str = ""
    channel_id: str = ""
    name: str = ""


def channels_from_invites(rows: Iterable[dict[str, Any]]) -> list[HeldChannel]:
    """The channels a set of stored Concord invites grant, unioned per community.

    The daemon learns its channel list from the Control plane at refresh; a `hng profiles`
    run has no live plane, so this reads the same grants out of what the invites recorded --
    the same source the daemon's refresh unions from, without a network round trip.
    """
    found: dict[tuple[str, str], HeldChannel] = {}
    for row in rows or ():
        bundle = row.get("bundle") or {}
        if not isinstance(bundle, dict):
            continue
        community = str(row.get("community_id") or bundle.get("community_id") or "").lower()
        for channel in bundle.get("channels") or []:
            if not isinstance(channel, dict):
                continue
            ident = str(channel.get("id") or "").strip()
            if not ident:
                continue
            found.setdefault((community, ident.lower()), HeldChannel(
                community_id=community, channel_id=ident,
                name=str(channel.get("name") or "")))
    return list(found.values())


class ProfileRouter:
    """Resolves a conversation to a profile. Empty config resolves nothing, never guesses."""

    def __init__(self, *, configured: bool = False, default: Optional[str] = None,
                 planes: Optional[dict[str, str]] = None,
                 channels: Optional[list[ChannelRule]] = None,
                 homes: Optional[dict[str, ProfileHome]] = None,
                 root: Optional[Path] = None, default_home: Optional[Path] = None,
                 multiplex: Optional[bool] = None,
                 problems: Iterable[str] = (), warnings: Iterable[str] = ()):
        self.configured = configured
        self.default = default
        self.planes: dict[str, str] = dict(planes or {})
        self.channels: list[ChannelRule] = list(channels or [])
        self.homes: dict[str, ProfileHome] = dict(homes or {})
        self.root = root
        self.default_home = default_home
        self.multiplex = multiplex
        self.problems: tuple[str, ...] = tuple(problems)
        self.warnings: tuple[str, ...] = tuple(warnings)

    # -- construction ---------------------------------------------------

    @classmethod
    def from_config(cls, raw: Any, *, hermes_home: Optional[str | Path] = None,
                    multiplex: Optional[bool] = None,
                    multiplex_source: str = "") -> "ProfileRouter":
        """Parse the `profiles` block. Nothing is raised: problems are collected and reported.

        A read-only verb (`hng status`, `hng route`) must not die because the block is broken,
        and `hng serve` refuses to start instead -- the same split as `concord`'s project
        mapping, with the difference that a broken profile route is fatal to a *turn*, so the
        daemon will not run with one.
        """
        if raw is None:
            return cls(multiplex=multiplex)
        if not isinstance(raw, dict):
            return cls(configured=True, root=None, multiplex=multiplex,
                       problems=[f"profiles: expected a mapping, got {type(raw).__name__}"])

        problems: list[str] = []
        warnings: list[str] = []
        unknown = sorted(set(map(str, raw)) - {"default", "root", "planes", "channels"})
        if unknown:
            problems.append(f"profiles: unknown field(s) {', '.join(unknown)} (expected default, "
                            "root, planes, channels)")

        base = Path(os.path.expanduser(str(hermes_home or "~/.hermes"))).absolute()
        configured_root = str(raw.get("root") or "").strip()
        root = (Path(os.path.expanduser(configured_root)).absolute() if configured_root
                else base / "profiles")
        default_home = base
        homes: dict[str, ProfileHome] = {}

        def home_for(name: str, origin: str) -> ProfileHome:
            """Check a profile once per load, and report the same problem wherever it is named."""
            home = homes.get(name)
            if home is None:
                home = homes[name] = profile_home(name, root=root, default_home=default_home)
            if not home.live:
                problems.append(f"{origin}: {home.problem}")
            elif home.parked:
                warnings.append(
                    f"{origin}: profile {name!r} is parked ({home.path / 'gateway.parked'}); "
                    f"turns routed to it stay down until `hermes -p {name} gateway start`")
            return home

        default: Optional[str] = None
        if raw.get("default") is None:
            problems.append(
                "profiles.default is required once profiles is configured: it is the profile "
                "every conversation falls back to")
        else:
            default = str(raw.get("default") or "").strip()
            if not default:
                problems.append("profiles.default names no profile")
                default = None
            else:
                home_for(default, "profiles.default")

        planes: dict[str, str] = {}
        raw_planes = raw.get("planes") or {}
        if not isinstance(raw_planes, dict):
            problems.append(f"profiles.planes: expected a mapping, got "
                            f"{type(raw_planes).__name__}")
        else:
            for key, value in raw_planes.items():
                plane = str(key or "").strip().lower()
                origin = f"profiles.planes.{plane}"
                if plane not in PLANES:
                    problems.append(f"{origin}: {key!r} is not a plane this gateway attaches "
                                    f"({', '.join(PLANES)})")
                    continue
                name = str(value or "").strip()
                if not name:
                    problems.append(f"{origin}: names no profile")
                    continue
                home_for(name, origin)
                planes[plane] = name

        channels: list[ChannelRule] = []
        raw_channels = raw.get("channels") or []
        if isinstance(raw_channels, dict):
            problems.append("profiles.channels: expected a list of entries, not a mapping")
            raw_channels = []
        if not isinstance(raw_channels, (list, tuple)):
            problems.append(f"profiles.channels: expected a list, got "
                            f"{type(raw_channels).__name__}")
            raw_channels = []
        for position, entry in enumerate(raw_channels):
            origin = f"profiles.channels[{position}]"
            if not isinstance(entry, dict):
                problems.append(f"{origin}: expected a mapping with community, channel and "
                                "profile")
                continue
            entry_unknown = sorted(set(map(str, entry)) - {"community", "channel", "profile"})
            if entry_unknown:
                problems.append(f"{origin}: unknown field(s) {', '.join(entry_unknown)} "
                                "(expected community, channel, profile)")
            community = str(entry.get("community") or "").strip().lower()
            channel = str(entry.get("channel") or "").strip()
            name = str(entry.get("profile") or "").strip()
            if not channel:
                problems.append(f"{origin}: names no channel (a channel id or its name)")
                continue
            if not name:
                problems.append(f"{origin}: names no profile")
                continue
            duplicate = next((seen for seen in channels
                              if seen.community == community
                              and seen.channel.strip().lower() == channel.lower()), None)
            if duplicate is not None:
                problems.append(f"{origin}: {channel!r} in community "
                                f"{community or '(any)'} is already routed by "
                                f"{duplicate.origin}; keeping that one")
                continue
            home_for(name, origin)
            channels.append(ChannelRule(community=community, channel=channel, profile=name,
                                        origin=origin))

        # A block that routes nothing away from the receiving profile (an explicit
        # `default: default` and nothing else) does not depend on multiplexing at all, so the
        # flag only matters when some conversation would actually change identity.
        routes_something = bool(planes or channels
                                or (default and default != DEFAULT_PROFILE))
        if multiplex is False and routes_something:
            problems.append(
                f"{HERMES_MULTIPLEX_KEY} is false ({multiplex_source or 'Hermes config'}): a "
                "non-multiplexed gateway keeps the legacy agent:main session namespace and "
                "ignores a routed profile, so every route would run as the default profile")
        elif multiplex is None and routes_something:
            warnings.append(
                "cannot tell whether Hermes multiplexes profiles "
                f"({multiplex_source or 'no Hermes config'}): a routed turn only leaves the "
                f"default profile when {HERMES_MULTIPLEX_KEY} is on (it defaults on)")

        return cls(configured=True, default=default, planes=planes, channels=channels,
                   homes=homes, root=root, default_home=default_home, multiplex=multiplex,
                   problems=problems, warnings=warnings)

    # -- questions the gateway asks -------------------------------------

    def __bool__(self) -> bool:
        return self.configured

    @property
    def usable(self) -> bool:
        """Configured and loadable: `hng serve` runs only with this true."""
        return self.configured and not self.problems and self.default is not None

    def resolve(self, plane: str = "", conversation: str = "", *,
                known_channels: Optional[Iterable[Any]] = None,
                verify: bool = False) -> Resolution:
        """The profile for one conversation, with the rule that won and what it beat.

        `known_channels` is what the Concord plane holds right now (objects with
        `community_id`, `channel_id` and `name`). `None` means "not known yet" -- the check is
        skipped rather than guessing -- while an empty list means "known to hold nothing".

        `verify` re-checks the winning profile against the filesystem. The daemon asks for it
        on every frame (a profile deleted while the process runs must not become a turn under
        the default profile) and read-only verbs do not need it (they read the disk anyway).
        """
        if not self.configured:
            return Resolution(rule="none",
                              why="profiles is not configured: the turn runs as the gateway's "
                                  "own profile")
        if self.problems or self.default is None:
            return Resolution(rule="validation", error="profile_invalid",
                              detail="; ".join(self.problems)
                                     or "profiles.default is missing")
        plane = str(plane or "").strip().lower()

        if plane == "concord" and self.channels:
            parsed = _parse_concord(conversation)
            if parsed is not None:
                community, channel = parsed
                known = (_known_channels(known_channels)
                         if known_channels is not None else None)
                if known is not None and f"{community}:{channel.strip().lower()}" not in known:
                    # A rule for a room this gateway does not hold is the stale entry to
                    # show, not a plane default to hide behind.
                    return Resolution(
                        rule="channel", error="route_unresolved",
                        detail=f"no channel {channel} in community {community or '(any)'} "
                               "among the channels this gateway holds")
                matched = self._match_channel(community, channel, known)
                if matched is not None:
                    return self._resolution(matched.profile, "channel",
                                            f"channel rule {matched.origin} "
                                            f"{self._beaten('channel', plane)}",
                                            verify=verify)
                if known is None and any(not _looks_like_id(rule.channel)
                                         for rule in self._scoped(community)):
                    # A rule written by name cannot be evaluated without the channel's name,
                    # and falling through to the plane would be a different answer to the
                    # question that was asked.
                    return Resolution(
                        rule="channel", error="route_unresolved",
                        detail="no channel list to match a channel name against (no stored "
                               "invite for that community); name the channel by id instead")

        profile = self.planes.get(plane)
        if profile:
            return self._resolution(profile, "plane",
                                    f"profiles.planes.{plane} {self._beaten('plane', plane)}",
                                    verify=verify)
        return self._resolution(self.default, "default",
                                "profiles.default (no channel or plane rule applies)",
                                verify=verify)

    def _resolution(self, profile: str, rule: str, why: str, *,
                    verify: bool = False) -> Resolution:
        """A winning rule, unless its profile cannot host a turn -- then the turn fails closed."""
        home = self.homes.get(profile)
        if home is not None and not home.live:
            return Resolution(rule=rule, why=why, error="profile_unknown",
                              detail=home.problem)
        if verify and profile != DEFAULT_PROFILE and self.root is not None:
            problem = profile_home(profile, root=self.root,
                                   default_home=self.default_home or self.root.parent,
                                   read_config=False).problem
            if problem:
                return Resolution(rule=rule, why=why, error="profile_unknown", detail=problem)
        return Resolution(profile=profile, rule=rule, why=why)

    def _scoped(self, community: str) -> list[ChannelRule]:
        """The rules that can apply to this community at all."""
        return [rule for rule in self.channels
                if not rule.community or rule.community == community]

    def _match_channel(self, community: str, channel: str,
                       known: Optional[dict[str, dict[str, str]]]) -> Optional[ChannelRule]:
        """The channel rule for this room: an id match beats a name match, as in projects."""
        ident = channel.strip().lower()
        name = ""
        if known is not None:
            entry = known.get(f"{community}:{ident}")
            name = (entry or {}).get("name", "").strip().lower()

        for rule in self._scoped(community):
            if rule.channel.strip().lower() == ident:
                return rule
        if not name:
            return None
        for rule in self._scoped(community):
            if rule.channel.strip().lower() == name:
                return rule
        return None

    def _beaten(self, rule: str, plane: str) -> str:
        """What a winning rule outranks, so `hng route` explains itself."""
        below: list[str] = []
        if rule == "channel" and plane in self.planes:
            below.append(f"profiles.planes.{plane}")
        if rule in ("channel", "plane"):
            below.append("profiles.default")
        return f"beats {' and '.join(below)}" if below else "is the only rule"

    def channel_problems(self, known: Iterable[Any]) -> list[str]:
        """Channel rules that name a channel this gateway does not hold.

        `hng profiles --check` asks this: a rule for a room that no longer exists would
        otherwise sit there unnoticed until a conversation resolved through a fallback nobody
        chose.
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
        """The whole table as lines, for `hng profiles`."""
        if not self.configured:
            return ["profiles is not configured: every turn runs as the gateway's own profile"]
        lines: list[str] = []
        for problem in self.problems:
            lines.append(f"config: {problem}")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        if self.default is not None:
            lines.append(f"  {'default':<22} {self.default:<14}{_home_state(self.homes.get(self.default))}")
        for plane in sorted(self.planes):
            name = self.planes[plane]
            lines.append(f"  {'planes.' + plane:<22} {name:<14}{_home_state(self.homes.get(name))}")
        for rule in self.channels:
            where = f"{rule.channel} @ {rule.community or 'any community'}"
            lines.append(f"  {(rule.origin or 'channel'):<22} {rule.profile:<14}"
                         f"{_home_state(self.homes.get(rule.profile))}  {where}")
        if self.root is not None:
            lines.append(f"  profiles live under {self.root}")
        return lines

    def as_dict(self) -> dict[str, Any]:
        """The block as data: every rule, and whether its profile can host a turn."""
        return {
            "configured": self.configured,
            "usable": self.usable,
            "default": self.default,
            "root": str(self.root) if self.root else "",
            "multiplex": self.multiplex,
            "problems": list(self.problems),
            "warnings": list(self.warnings),
            "planes": dict(self.planes),
            "channels": [rule.as_dict() for rule in self.channels],
            "homes": {name: home.as_dict() for name, home in self.homes.items()},
        }


def _home_state(home: Optional[ProfileHome]) -> str:
    if home is None:
        return "?"
    if not home.live:
        return f"UNAVAILABLE: {home.problem}"
    state = str(home.path)
    if home.parked:
        state += "  (parked)"
    return state


def load_profiles(cfg: dict[str, Any]) -> ProfileRouter:
    """The `profiles` block of a gateway config, with Hermes' own state read for the checks.

    Every caller gets `problems` and `warnings` rather than an exception; `require_profiles`
    is the refusing form `hng serve` uses.
    """
    state = hermes_profile_state(cfg)
    return ProfileRouter.from_config(
        cfg.get("profiles"),
        hermes_home=hermes_config_path(cfg).parent,
        multiplex=state.get("multiplex"),
        multiplex_source=state.get("source", ""),
    )


def require_profiles(cfg: dict[str, Any]) -> ProfileRouter:
    """`load_profiles`, refusing rather than returning a router nothing can be routed by.

    `hng serve` uses this: a route that would run a turn as the wrong identity is not something
    to start and then warn about.
    """
    router = load_profiles(cfg)
    if router.problems:
        raise ProfilesConfigError("; ".join(router.problems))
    return router
