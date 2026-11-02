"""Advertising the identity: a kind-0 profile and the kind-10304 command manifest.

An Armada client renders a bot's slash commands only from a replaceable kind-10304
manifest, and only for conversation members whose kind 0 carries ``bot: true``. Neither
existed here, so the commands Hermes already dispatches on every platform were invisible
in that client. Nothing about invocation changes: typing ``/name args`` is an ordinary
message, and Hermes' gateway already dispatches it.

What this module owns is the *content* -- the host's gateway-available commands filtered
to what this plane can offer, bounded by the client's fail-closed limits, plus the profile
the discovery step looks for. Publishing is ``publisher.py``'s job; remembering whether the
stored copy still differs is the store's.

The catalogue is an **input**, not something this package can assemble: the gateway does
not import Hermes (``profiles.py`` makes the same call for profile names), so the
Hermes-side plugin writes the host's registry to a JSON file and this module validates it.
A file also means ``hng profile publish`` works from a shell with Hermes stopped.

Placeholders only: no real identity, relay or host belongs in this module or its tests.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .crypto import sign_event
from .publisher import is_permanent_refusal

log = logging.getLogger("hng.advertise")

#: The profile Armada reads the ``bot`` flag from, and the replaceable manifest kind it
#: queries for this author. Both are one-per-pubkey: an identity advertises one catalogue
#: everywhere, which is why there is no per-channel variant of this module.
PROFILE_KIND = 0
MANIFEST_KIND = 10304
MANIFEST_VERSION = 1

#: The client's limits, all fail-closed: one bad entry and Armada renders nothing at all,
#: so the builder drops what cannot be represented instead of emitting it and hoping.
MAX_COMMANDS = 64
MAX_ARGS = 8
MAX_DESCRIPTION = 200        # bytes, UTF-8
MAX_CHOICES = 32
MAX_CHOICE = 32              # bytes, UTF-8
MAX_MANIFEST = 32768         # bytes, UTF-8
NAME_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
ARG_TYPES = frozenset({"string", "int", "number", "bool", "user", "choice"})

#: Commands whose effect is a platform affordance this plane cannot render. A *deny*-list,
#: never an allow-list: a command Hermes adds tomorrow is advertised by default, which is
#: the point of reading the host's catalogue rather than hardcoding one here. The reason
#: travels with the name so `hng profile publish` can say why a menu is short.
NOT_ADVERTISED: dict[str, str] = {
    "topic": "Telegram DM topic sessions; this plane has no topics",
    "voice": "voice mode needs an audio surface this plane does not render",
    "save": "writes a file on the host; a chat reply cannot carry it back",
}

#: Names that survive the 64-entry cap first, in this order, then the rest of the host
#: catalogue in its own order. The same shape Telegram's menu uses: a rank, not a
#: catalogue -- anything not named here is still advertised when the cap allows.
MENU_PRIORITY: tuple[str, ...] = (
    "help", "new", "stop", "status", "resume", "sessions", "model", "commands",
    "whoami", "profile", "usage", "queue", "steer", "bg", "btw", "approve", "deny",
    "reasoning", "yolo", "approvals", "restart", "platform", "version", "update",
)


class AdvertiseError(RuntimeError):
    """The advertisement cannot be built or published as configured."""


class CatalogueError(AdvertiseError):
    """The host catalogue file is missing or is not the shape this module validates."""


def canonical_json(content: Mapping[str, Any]) -> str:
    """One spelling per content: sorted keys, no incidental whitespace.

    The hash stored for "have we published this?" is taken over this string, so a dict
    rebuilt in another order is the same advertisement rather than a republish.
    """
    return json.dumps(content, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(content: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _clip(text: str, limit: int) -> str:
    """Clamp to *limit* UTF-8 bytes without splitting a codepoint."""
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    cut = data[: max(0, limit - 3)]
    while cut:
        try:
            return cut.decode("utf-8") + "..."
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _collapse(text: Any) -> str:
    return " ".join(str(text or "").split())


@dataclass(frozen=True)
class Arg:
    """One manifest argument. The host catalogue supplies none; a curated config may."""

    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class MenuCommand:
    name: str
    description: str
    args: tuple[Arg, ...] = ()


@dataclass
class Manifest:
    """A manifest that has passed the client's limits, and what it had to drop."""

    content: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return canonical_json(self.content)

    @property
    def digest(self) -> str:
        return content_hash(self.content)

    @property
    def names(self) -> list[str]:
        return [command["name"] for command in self.content["commands"]]


def _arg_from(payload: Any) -> Arg:
    """One argument, or ValueError naming what the client would refuse."""
    if not isinstance(payload, Mapping):
        raise ValueError("argument is not an object")
    name = str(payload.get("name") or "").strip().lower()
    if not NAME_RE.match(name):
        raise ValueError(f"argument name {name!r} does not match {NAME_RE.pattern}")
    kind = str(payload.get("type") or "string").strip().lower()
    if kind not in ARG_TYPES:
        raise ValueError(f"argument {name!r} has type {kind!r}, not one of "
                         f"{', '.join(sorted(ARG_TYPES))}")
    description = _clip(_collapse(payload.get("description")), MAX_DESCRIPTION)
    choices = tuple(str(choice) for choice in payload.get("choices") or ())
    if kind == "choice":
        if not 1 <= len(choices) <= MAX_CHOICES:
            raise ValueError(f"argument {name!r} is a choice with {len(choices)} options; "
                             f"the client wants 1-{MAX_CHOICES}")
        for choice in choices:
            if len(choice.encode("utf-8")) > MAX_CHOICE:
                raise ValueError(f"argument {name!r} has a choice over {MAX_CHOICE} bytes")
    elif choices:
        raise ValueError(f"argument {name!r} is {kind!r} but names choices")
    return Arg(name=name, type=kind, description=description,
               required=bool(payload.get("required", False)), choices=choices)


def _command_from(payload: MenuCommand | Mapping[str, Any]) -> tuple[MenuCommand, list[str]]:
    """One command normalised to the client's limits; warnings are clamping, not refusal."""
    warnings: list[str] = []
    if isinstance(payload, MenuCommand):
        raw_name, raw_description, raw_args = payload.name, payload.description, payload.args
    elif isinstance(payload, Mapping):
        raw_name = payload.get("name")
        raw_description = payload.get("description")
        raw_args = payload.get("args") or ()
    else:
        raise ValueError("command is not an object")
    name = str(raw_name or "").strip().lstrip("/").lower()
    if not NAME_RE.match(name):
        raise ValueError(f"name {name!r} does not match {NAME_RE.pattern}")
    description = _collapse(raw_description)
    if not description:
        raise ValueError(f"command {name!r} has no description")
    if len(description.encode("utf-8")) > MAX_DESCRIPTION:
        warnings.append(f"/{name}: description clamped to {MAX_DESCRIPTION} bytes")
        description = _clip(description, MAX_DESCRIPTION)
    args = tuple(_arg_from(arg) for arg in raw_args)
    if len(args) > MAX_ARGS:
        raise ValueError(f"command {name!r} has {len(args)} arguments; the client allows "
                         f"{MAX_ARGS}")
    seen_optional = False
    for arg in args:
        if arg.required and seen_optional:
            raise ValueError(f"command {name!r} puts a required argument after an optional "
                             "one; the client refuses that manifest")
        seen_optional = seen_optional or not arg.required
    return MenuCommand(name=name, description=description, args=args), warnings


def build_manifest(commands: Iterable[MenuCommand | Mapping[str, Any]]) -> Manifest:
    """The kind-10304 content for *commands*, valid by construction.

    Anything the client would refuse is dropped with a warning rather than emitted: the
    client fails closed, so one unrepresentable name would hide every command behind it.
    """
    warnings: list[str] = []
    kept: list[MenuCommand] = []
    seen: set[str] = set()
    for entry in commands:
        try:
            command, notes = _command_from(entry)
        except ValueError as exc:
            warnings.append(f"dropped: {exc}")
            continue
        if command.name in seen:
            warnings.append(f"dropped /{command.name}: duplicate name")
            continue
        seen.add(command.name)
        warnings.extend(notes)
        kept.append(command)
    if len(kept) > MAX_COMMANDS:
        warnings.append(f"dropped {len(kept) - MAX_COMMANDS} command(s) beyond the client's "
                        f"{MAX_COMMANDS}-entry menu")
        kept = kept[:MAX_COMMANDS]

    while True:
        content = {
            "v": MANIFEST_VERSION,
            "commands": [
                {
                    "name": command.name,
                    "description": command.description,
                    "args": [
                        {
                            "name": arg.name,
                            "type": arg.type,
                            "description": arg.description,
                            "required": arg.required,
                            **({"choices": list(arg.choices)} if arg.type == "choice" else {}),
                        }
                        for arg in command.args
                    ],
                }
                for command in kept
            ],
        }
        size = len(canonical_json(content).encode("utf-8"))
        if size <= MAX_MANIFEST or not kept:
            return Manifest(content=content, warnings=warnings)
        warnings.append(f"dropped /{kept[-1].name}: manifest over {MAX_MANIFEST} bytes")
        kept = kept[:-1]


def build_profile(name: str, fields: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The kind-0 content: configured fields plus the claim discovery depends on.

    ``bot`` is forced true -- an advertisement that a client does not recognise as a bot
    is the one failure this module exists to prevent -- and ``name`` is required because a
    profile with no name is a worse discovery result than no republish.
    """
    clean = _collapse(name)
    if not clean:
        raise AdvertiseError("advertise.name is required when the kind-0 profile is published")
    content: dict[str, Any] = dict(fields or {})
    content["name"] = clean
    content["bot"] = True
    return content


def select_menu(entries: Iterable[Mapping[str, Any]]) -> tuple[list[MenuCommand], list[str]]:
    """The host catalogue as this plane advertises it: deny-list, rank, then host order."""
    warnings: list[str] = []
    commands: list[MenuCommand] = []
    for entry in entries:
        name = str(entry.get("name") or "").strip().lstrip("/").lower()
        if not name:
            continue
        reason = NOT_ADVERTISED.get(name)
        if reason:
            warnings.append(f"not advertised /{name}: {reason}")
            continue
        description = _collapse(entry.get("description"))
        hint = _collapse(entry.get("args_hint"))
        if hint:
            description = f"{description} (usage: /{name} {hint})"
        raw_args = entry.get("args") or ()
        try:
            args = tuple(arg if isinstance(arg, Arg) else _arg_from(arg) for arg in raw_args)
        except ValueError as exc:
            warnings.append(f"dropped /{name}: {exc}")
            continue
        commands.append(MenuCommand(name=name, description=description, args=args))
    rank = {name: index for index, name in enumerate(MENU_PRIORITY)}
    commands.sort(key=lambda command: (rank.get(command.name, len(rank)),))
    # `sort` is stable, so everything unranked keeps the host catalogue's own order after
    # the ranked names.
    return commands, warnings


def load_catalogue(path: str | Path) -> tuple[list[MenuCommand], list[str]]:
    """Read the Hermes-side catalogue file: ``{"commands": [...]}`` or a bare list."""
    location = Path(os.path.expanduser(str(path)))
    try:
        raw = json.loads(location.read_text())
    except FileNotFoundError as exc:
        raise CatalogueError(
            f"no command catalogue at {location}: the Hermes plugin writes it when Hermes "
            "starts (or point advertise.catalogue_file at one)") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogueError(f"{location}: cannot read the command catalogue: {exc}") from exc
    entries = raw.get("commands") if isinstance(raw, Mapping) else raw
    if not isinstance(entries, list):
        raise CatalogueError(f"{location}: expected a list of commands or {{'commands': [...]}}")
    clean = [entry for entry in entries if isinstance(entry, Mapping)]
    if len(clean) != len(entries):
        log.warning("%s: ignored %d non-object catalogue entr(ies)",
                    location, len(entries) - len(clean))
    return select_menu(clean)


@dataclass(frozen=True)
class AdvertisementConfig:
    """What ``advertise:`` in the config asks this daemon to advertise.

    Off by default. ``publish: true`` advertises both events; a list narrows it, which is
    what lets a deployment publish the command manifest without taking over a kind 0 that
    another process on the same key owns.
    """

    targets: tuple[int, ...] = ()
    profile: dict[str, Any] | None = None
    catalogue_file: Path = field(default_factory=lambda: Path("commands.json"))
    relays: tuple[str, ...] = ()

    @property
    def enabled(self) -> bool:
        return bool(self.targets)

    @property
    def publishes_profile(self) -> bool:
        return PROFILE_KIND in self.targets

    @property
    def publishes_commands(self) -> bool:
        return MANIFEST_KIND in self.targets

    @classmethod
    def from_config(cls, block: Mapping[str, Any] | None, *,
                    socket_path: str | Path | None = None) -> AdvertisementConfig:
        block = block or {}
        raw = block.get("publish", False)
        aliases = {"profile": PROFILE_KIND, "kind0": PROFILE_KIND, "0": PROFILE_KIND,
                   "commands": MANIFEST_KIND, "manifest": MANIFEST_KIND,
                   "10304": MANIFEST_KIND}
        if raw is True:
            targets: tuple[int, ...] = (PROFILE_KIND, MANIFEST_KIND)
        elif raw is False or raw is None:
            targets = ()
        else:
            if isinstance(raw, str):
                wanted: Sequence[Any] = [raw]
            elif isinstance(raw, Sequence):
                wanted = raw
            else:
                raise AdvertiseError(
                    f"advertise.publish: expected a boolean or a list, got {raw!r}")
            resolved: list[int] = []
            for item in wanted:
                key = str(item).strip().lower()
                if key not in aliases:
                    raise AdvertiseError(
                        f"advertise.publish: {item!r} is not one of "
                        f"{', '.join(sorted(aliases))}")
                if aliases[key] not in resolved:
                    resolved.append(aliases[key])
            targets = tuple(resolved)

        profile = None
        if PROFILE_KIND in targets:
            profile = build_profile(str(block.get("name") or ""), block.get("fields") or {})
        catalogue = block.get("catalogue_file")
        if catalogue:
            path = Path(os.path.expanduser(str(catalogue)))
        elif socket_path:
            path = Path(os.path.expanduser(str(socket_path))).with_name("commands.json")
        else:
            path = Path.home() / ".local/state/hermes-nostr-gateway/commands.json"
        relays = tuple(str(relay).strip() for relay in block.get("relays") or () if str(relay))
        return cls(targets=targets, profile=profile, catalogue_file=path, relays=relays)

    def describe(self) -> str:
        if not self.enabled:
            return "off"
        names = [("profile" if kind == PROFILE_KIND else "commands") for kind in self.targets]
        return f"{', '.join(names)} -> {self.catalogue_file}"


@dataclass
class KindReport:
    """What happened to one event kind: the id, the receipts and the read-back."""

    kind: int
    content_hash: str
    status: str                      # published | partial | failed | unchanged
    event_id: str | None = None
    published: dict[str, tuple[bool, str | None]] = field(default_factory=dict)
    readback: dict[str, str] = field(default_factory=dict)
    refused: dict[str, str] = field(default_factory=dict)
    stored: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def transient(self) -> dict[str, str]:
        """Relays that failed for a reason a retry could change; a refusal is not one."""
        return {relay: reason or "unknown"
                for relay, (ok, reason) in self.published.items()
                if not ok and relay not in self.refused}

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "event_id": self.event_id,
            "content_hash": self.content_hash,
            "status": self.status,
            "published": {relay: {"ok": ok, "error": error}
                          for relay, (ok, error) in self.published.items()},
            "readback": self.readback,
            "refused": self.refused,
            "stored": self.stored,
            "warnings": self.warnings,
        }


def _verdict(event: Mapping[str, Any] | None, expected_id: str,
             created_at: int) -> str:
    """Read-back is about what a client would *serve*, not what the relay accepted."""
    if not event:
        return "absent"
    if str(event.get("id") or "") == expected_id:
        return "retained"
    try:
        other = int(event.get("created_at") or 0)
    except (TypeError, ValueError):
        other = 0
    return "superseded" if other > created_at else "stale"


async def resolve_relays(publish: Sequence[str], extra: Sequence[str] = (), *,
                         me: str | None = None, secret: str | None = None) -> list[str]:
    """Where an advertisement goes: the publish set, configured extras, our 10002 writes.

    A client reads a bot's manifest over the conversation's relays plus its own, so the
    identity's NIP-65 write relays belong in the answer. The rest of a read set does not:
    a relay kept for its inbox or a themed read path may refuse public kinds, and a
    refusal there would make the advertisement look broken.
    """
    relays = [str(relay).strip() for relay in publish]
    relays.extend(str(relay).strip() for relay in extra)
    if me:
        from .fetch import fetch_first

        try:
            event = await fetch_first(relays=relays[:2], kinds=[10002], author=me, limit=1,
                                      timeout=8.0, secret=secret)
        except Exception:  # noqa: BLE001 - a missing NIP-65 list is not a failed publish
            log.debug("advertise: could not read our 10002 write relays", exc_info=True)
            event = None
        for tag in (event or {}).get("tags") or []:
            # `["r", url]` and `["r", url, "write"]` are both write relays; "read" is not.
            if len(tag) >= 2 and tag[0] == "r" and (len(tag) < 3 or tag[2] != "read"):
                relays.append(str(tag[1]))
    return list(dict.fromkeys(relay for relay in relays if relay))


class Advertiser:
    """Signs the two events once and publishes them; never re-signs a retry.

    ``reader`` is the read-back query (one relay, one kind), injectable so the publish path
    can be exercised without a relay. It is not optional in production: a receipt is not
    retention, and the report has to say which relay actually serves the event.
    """

    def __init__(self, *, crypto: Any, publisher: Any, store: Any, secret: str | None = None,
                 reader: Callable[[str, int], Awaitable[dict | None]] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.crypto = crypto
        self.publisher = publisher
        self.store = store
        self.secret = secret
        self._reader = reader
        self._clock = clock

    async def _read(self, relay: str, kind: int) -> dict | None:
        if self._reader is not None:
            return await self._reader(relay, kind)
        from .fetch import fetch_first

        return await fetch_first(relays=[relay], kinds=[kind], author=self.crypto.our_pubkey(),
                                 limit=1, timeout=8.0, secret=self.secret)

    async def publish(self, contents: Mapping[int, Mapping[str, Any]],
                      relays: Sequence[str], *, force: bool = False) -> list[KindReport]:
        """Publish each content whose stored copy differs, then read every relay back."""
        targets = list(dict.fromkeys(str(relay).strip() for relay in relays if str(relay)))
        reports: list[KindReport] = []
        for kind, content in contents.items():
            digest = content_hash(content)
            stored = self.store.advertised(kind)
            if stored and stored.get("content_hash") == digest and not force:
                reports.append(KindReport(kind=kind, content_hash=digest, status="unchanged",
                                          event_id=stored.get("event_id"), stored=True))
                continue
            if not targets:
                reports.append(KindReport(kind=kind, content_hash=digest, status="failed",
                                          warnings=["no relays to publish to"]))
                continue
            created_at = int(self._clock())
            event = sign_event({"kind": kind, "content": canonical_json(content), "tags": [],
                                "created_at": created_at}, self.crypto.keys)
            results = await self.publisher.publish(event, targets)
            accepted = [relay for relay, (ok, _error) in results.items() if ok]
            refused = {relay: reason or "refused"
                       for relay, (ok, reason) in results.items()
                       if not ok and is_permanent_refusal(reason)}
            report = KindReport(kind=kind, content_hash=digest, status="failed",
                                event_id=event["id"], published=results, refused=refused)
            if not accepted:
                reports.append(report)
                continue
            verdicts = await asyncio.gather(*(self._read(relay, kind) for relay in accepted))
            report.readback = {
                relay: _verdict(found, event["id"], created_at)
                for relay, found in zip(accepted, verdicts, strict=True)
            }
            report.status = "published" if len(accepted) == len(results) else "partial"
            # A relay that refused this kind has made a decision, not had an outage: record
            # the copy so the next start does not publish into the same refusal. A transient
            # failure is deliberately not stored, so the next start retries those relays.
            if not report.transient:
                self.store.record_advertised(kind, event["id"], digest, created_at,
                                             refused=refused)
                report.stored = True
            reports.append(report)
        return reports
