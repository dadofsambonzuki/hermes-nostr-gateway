"""Read coverage: the inbox relays of the people this gateway talks to.

Spec §3 makes relay coverage a correctness property. A fixed `relays.read` covers our own
inbox and the app-relay defaults, but a NIP-17 sender publishes to the *recipient's*
kind-10050 relays: a peer whose 10050 names relays we do not read is invisible, and the
miss is silent. That is measured, not theoretical — a real 10050 can be
on three different relays, and two of them had to be
added to the read list by hand.

So the read set is not fixed any more: the inbox relays of every pubkey named in
`groups` are resolved and added to the DM read set, on a refresh interval. Bounded on
purpose, because an unbounded read set is a connection-limit incident:

* `relays.peer_inbox.max_extra` is a hard cap on how many relays may ever be added, for
  the life of the process (a relay is added once and never re-added);
* additions are deduplicated against `relays.read` on a normalised URL, so a relay we
  already read costs nothing;
* only `wss://` URLs are accepted, and configured White Noise relays are refused: they
  carry the Marmot plane's groups and are not a general NIP-17 inbox (see README);
* the gateway gives an added relay ONE DM worker and no feed or mention subscription,
  so the cap really is the connection bound.

A relay that refuses a kind is handled exactly like any other relay: `PoolWorker`
drops that kind and resubscribes on the rest (spec §3). Nothing here special-cases the
recovery path.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Any, Iterable, Protocol
from urllib.parse import urlsplit

log = logging.getLogger("hng.peer_inbox")

#: Kind 10050: the DM relay list a peer publishes so senders can find them (NIP-17).
INBOX_KIND = 10050

#: How many relays beyond `relays.read` may be added. Four covers the app-relay tail
#: without turning one misconfigured 10050 into a fan-out.
DEFAULT_MAX_EXTRA = 4
#: Re-resolve every hour: peers republish 10050 when they move, and a stale read set is
#: a silent miss again. 0 means "resolve once at startup".
DEFAULT_REFRESH_MINUTES = 60
#: Lookups are bounded like publishes: ask a handful of relays, not the whole read set.
DEFAULT_LOOKUP_LIMIT = 4
DEFAULT_LOOKUP_TIMEOUT = 20.0

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def is_pubkey_hex(value: object) -> bool:
    """A 64-hex pubkey. Groups are normalised to hex before they reach this module."""
    return bool(_HEX64.fullmatch(str(value or "").strip().lower()))


def normalise_relay(url: object) -> str:
    """The form relay URLs are compared in: no trailing slash, case-folded.

    Comparison only. The URL we hand to a worker keeps the spelling the peer or the
    config used, because a relay's path is the relay's business.
    """
    text = str(url or "").strip()
    return text.rstrip("/").lower() if text else ""


def public_host(host: str) -> bool:
    """False for loopback, private, link-local, reserved or unresolvable names."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {str(info[4][0]).split("%")[0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:  # pragma: no cover - getaddrinfo only returns addresses
            return False
    return True


def relay_url(url: object) -> str | None:
    """A usable relay URL, or None.

    `wss://` only. A 10050 is peer-controlled input, and an entry there is not a
    licence to open a plaintext (`ws://`) or non-network (`file://`, `http://`)
    connection. An address the peer can point at our own box is the same problem in a
    different scheme -- `wss://127.0.0.1:8080` or `wss://localhost` is a blind request
    into the host's private network -- so a non-public host is refused here too.
    """
    text = str(url or "").strip()
    if not text:
        return None
    parts = urlsplit(text)
    if parts.scheme.lower() != "wss" or not parts.netloc:
        return None
    host = (parts.hostname or "").strip().lower()
    if not host or host in {"localhost", "localhost.localdomain", "ip6-localhost"} \
            or host.endswith(".local"):
        return None
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        return text
    return text if literal.is_global else None


def _unique_relays(values: Iterable[object], *, strict: bool, where: str,
                   quiet: bool = False) -> tuple[str, ...]:
    """Valid relay URLs, deduplicated on `normalise_relay`, first spelling wins."""
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        url = relay_url(value)
        if url is None:
            if strict:
                raise ValueError(f"{where} entry is not a wss:// relay URL: {value!r}")
            if not quiet:
                log.warning("%s: skipping non-relay entry %r", where, value)
            continue
        key = normalise_relay(url)
        if key in seen:
            continue
        seen.add(key)
        out.append(url)
    return tuple(out)


def _configured_urls(entries: Iterable[object] | None) -> tuple[str, ...]:
    """URLs from a `relays.read`/`relays.publish` list, string or `{url, kinds}`."""
    out: list[str] = []
    for entry in entries or []:
        if isinstance(entry, str):
            out.append(entry)
        elif isinstance(entry, dict):
            url = entry.get("url") or entry.get("relay")
            if url:
                out.append(str(url))
    return tuple(out)


@dataclass(frozen=True)
class PeerInboxConfig:
    """`relays.peer_inbox`: whether peer inboxes are read, and the bounds on them."""

    enabled: bool = True
    max_extra: int = DEFAULT_MAX_EXTRA
    refresh_minutes: int = DEFAULT_REFRESH_MINUTES
    lookup: tuple[str, ...] = ()

    @property
    def refresh_seconds(self) -> int:
        return int(self.refresh_minutes * 60)


def _count(raw: dict[str, Any], key: str, default: int) -> int:
    value = raw.get(key, default)
    # `bool` is an `int` in Python, and `max_extra: true` is not a cap.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"relays.peer_inbox.{key} must be a whole number: {value!r}")
    if value < 0:
        raise ValueError(f"relays.peer_inbox.{key} cannot be negative: {value!r}")
    return value


def peer_inbox_config(cfg: dict[str, Any],
                      default_lookup: Iterable[str] | None = None) -> PeerInboxConfig:
    """Read `relays.peer_inbox`, loudly.

    Keys (all under `relays.peer_inbox`):
      `enabled` (true)            resolve peers' inbox relays at all;
      `max_extra` (4)             hard cap on relays added over the process lifetime;
      `refresh_minutes` (60)      re-resolve interval; 0 = resolve once at startup;
      `lookup` (read+publish,      relays asked for a peer's 10050. The derived default
      first four)                  is bounded; an explicit list is taken as written.

    A malformed value is a config error, not a reason to run with a different cap than
    the operator wrote (the same rule `relay_read_plan` follows).
    """
    relays = cfg.get("relays") or {}
    raw = relays.get("peer_inbox")
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"relays.peer_inbox must be a mapping: {raw!r}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"relays.peer_inbox.enabled must be true or false: {enabled!r}")

    lookup_raw = raw.get("lookup")
    if lookup_raw is not None and not isinstance(lookup_raw, list):
        raise ValueError(f"relays.peer_inbox.lookup must be a list: {lookup_raw!r}")
    if lookup_raw:
        # An explicit list is taken as written: if it names ten relays, all ten are asked.
        lookup = _unique_relays(lookup_raw, strict=True, where="relays.peer_inbox.lookup")
    elif default_lookup is not None:
        lookup = _unique_relays(default_lookup, strict=False,
                                where="relays.peer_inbox.lookup")
    else:
        # Omitting the lookup list means "ask the relays we already talk to", which is
        # where a peer's 10050 is most likely to be found. Bounded like a publish,
        # because every lookup relay costs a connection per peer.
        derived = _unique_relays(
            [*_configured_urls(relays.get("read")),
             *_configured_urls(relays.get("publish"))],
            strict=False, where="relays.peer_inbox.lookup",
        )
        lookup = derived[:DEFAULT_LOOKUP_LIMIT]
        if len(derived) > len(lookup):
            log.info("peer inbox: lookup defaults to the first %d relay(s) of "
                     "relays.read/publish; set relays.peer_inbox.lookup to ask others",
                     DEFAULT_LOOKUP_LIMIT)

    return PeerInboxConfig(
        enabled=enabled,
        max_extra=_count(raw, "max_extra", DEFAULT_MAX_EXTRA),
        refresh_minutes=_count(raw, "refresh_minutes", DEFAULT_REFRESH_MINUTES),
        lookup=lookup,
    )


def inbox_additions(*, base: Iterable[str], candidates: Iterable[str], cap: int,
                    exclude: Iterable[str] = ()) -> list[str]:
    """The relays to add: candidate order, deduped, capped. Pure, so it can be tested.

    `base` is everything already read, `exclude` the relays that must never be added
    (White Noise, per README). `cap` is what is left of `max_extra`; a cap of zero is a
    valid answer, not an error.
    """
    if cap <= 0:
        return []
    seen = {normalise_relay(url) for url in base}
    blocked = {normalise_relay(url) for url in exclude}
    out: list[str] = []
    for candidate in candidates:
        url = relay_url(candidate)
        if url is None:
            continue
        key = normalise_relay(url)
        if key in seen or key in blocked:
            continue
        seen.add(key)
        out.append(url)
        if len(out) >= cap:
            break
    return out


class InboxResolver(Protocol):
    """What the gateway needs to know: a pubkey's inbox relays."""

    def inbox_relays(self, pubkey: str) -> list[str]:
        ...


class SdkInboxResolver:
    """Resolve a peer's kind-10050 list through the bindings.

    The event is replaceable, so the newest one wins. An empty answer leaves the relays
    this process already reads untouched — a lookup that timed out must never shrink
    coverage.

    What an empty answer *means* is deliberately not decided here: the binding reports
    the events a relay sent, not whether it answered EOSE, so "this peer advertises no
    inbox" and "the lookup relay did not answer" arrive identically. `fetch` logs the
    transport failure at debug; the coverage decision is the same either way.
    """

    def __init__(self, lookups: Iterable[str], *, limit: int | None = None,
                 timeout: float = DEFAULT_LOOKUP_TIMEOUT, secret: str | None = None):
        self.lookups = tuple(lookups)
        # None means "ask every configured lookup relay". The gateway's default
        # derivation already bounds the list (`DEFAULT_LOOKUP_LIMIT`); an explicit
        # `relays.peer_inbox.lookup` is taken as written.
        self.limit = limit
        self.timeout = timeout
        # Authenticate as our identity where the lookup relay wants NIP-42. Without one
        # relay answers "restricted" while a 10050 held only there
        # would be missed.
        self.secret = secret

    def inbox_relays(self, pubkey: str) -> list[str]:
        lookups = self.lookups if self.limit is None else self.lookups[: self.limit]
        if not lookups:
            return []
        from .fetch import fetch_many_blocking

        events = fetch_many_blocking(
            relays=lookups, kinds=[INBOX_KIND], author=pubkey, limit=1,
            timeout=self.timeout, secret=self.secret,
        )
        events = [event for event in events
                  if isinstance(event, dict) and event.get("kind", INBOX_KIND) == INBOX_KIND]
        if not events:
            return []
        newest = max(events, key=lambda event: int(event.get("created_at") or 0))
        return list(_relay_tags(newest))


def _relay_tags(event: dict) -> tuple[str, ...]:
    """`["relay", "wss://…"]` tags, valid and deduplicated, in published order."""
    return _unique_relays(
        (tag[1] for tag in event.get("tags") or []
         if isinstance(tag, list) and len(tag) >= 2 and tag[0] == "relay"),
        strict=False, where="kind 10050", quiet=True,
    )


def peer_pubkeys(groups: dict[str, list[str]]) -> tuple[str, ...]:
    """Every distinct 64-hex member of every configured group.

    A group authored as npubs is normalised before it reaches here; a member that still
    is not hex (a failed decode) is skipped rather than looked up as a bogus author.
    """
    found = {
        str(member).strip().lower()
        for members in groups.values() for member in (members or [])
    }
    return tuple(sorted(pubkey for pubkey in found if is_pubkey_hex(pubkey)))


def now_seconds() -> int:
    return int(time.time())
