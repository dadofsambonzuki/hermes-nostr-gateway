"""The gateway: relays in, policy applied, socket out. One process, one identity."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any, cast

import yaml

from .bots import BotRegistry, kind0_fetcher
from .crypto import SdkCrypto
from .health import (
    DEFAULT_PENDING_STALE_MINUTES,
    default_planes,
    health_report,
    relay_verdicts,
)
from .media import MediaConfig, MediaError, media_config, resolve_attachment
from .peer_inbox import (
    InboxResolver,
    NakInboxResolver,
    PeerInboxConfig,
    inbox_additions,
    normalise_relay,
    now_seconds,
    peer_inbox_config,
    peer_pubkeys,
)
from .planes import nip04
from .planes.concord.plane import ConcordPlane
from .planes.marmot import (
    MarmotControl,
    MarmotError,
    MarmotPlane,
    MarmotResolver,
    MarmotWorker,
    resolve_account_id,
)
from .planes.nip01 import Nip01Plane, feed_rules
from .planes.nip17 import Nip17Plane
from .policy import Policy
from .pool import PoolWorker, RelayPool
from .projects import ProjectMap
from .publisher import Publisher, make_publisher
from .relay import RelayWorker
from .server import SocketServer
from .store import Store, drops_ttl_seconds
from .writer import StoreWriter

log = logging.getLogger("hng.gateway")

#: The DM subscription asks for NIP-17 wraps and legacy NIP-04 in one REQ. A relay
#: that refuses one kind closes the whole filter, so this is only the default:
#: `relays.read` can pin a narrower set per relay, and a refusal is negotiated at
#: runtime (RelayWorker.note_refusals) rather than costing the relay entirely.
DM_KINDS: tuple[int, ...] = (1059, 4)


def restricted_relay(relay: str) -> bool:
    """Relays that carry only the private path, never public feeds."""
    return relay.rstrip("/").endswith("/chat") or "whitenoise" in relay


def store_write_options(raw: Any) -> dict[str, Any]:
    """`store_write:` config -> `StoreWriter` kwargs.

    Only two knobs earn a place here. `tick_ms` is both the write latency and the
    `kill -9` exposure — 20 ms is the default, and shortening it trades throughput for
    a smaller window. `batch_size` is rows per transaction. Everything else is a
    measurement, not a preference.
    """
    config = raw or {}
    if not isinstance(config, dict):
        raise ValueError("store_write must be a mapping")
    options: dict[str, Any] = {}
    if config.get("tick_ms") is not None:
        options["tick"] = max(1, int(config["tick_ms"])) / 1000
    if config.get("batch_size") is not None:
        options["batch_size"] = max(1, int(config["batch_size"]))
    return options


def relay_read_plan(cfg: dict[str, Any]) -> tuple[list[str], dict[str, tuple[int, ...]]]:
    """The read set, plus any per-relay kind set the config pins.

    Each `relays.read` entry is a URL, or `{url, kinds}` when that relay's filter has
    to differ from `DM_KINDS`. `wss://relay.eu.whitenoise.chat` refuses kind 4 and
    closes the whole subscription when it appears in the same REQ, so its 1059 DMs
    would otherwise go down with it (spec §3: those relays carry DMs that exist
    nowhere else).
    """
    urls: list[str] = []
    declared: dict[str, tuple[int, ...]] = {}
    for entry in (cfg.get("relays") or {}).get("read") or []:
        if isinstance(entry, str):
            urls.append(entry)
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"relays.read entry must be a URL or a mapping: {entry!r}")
        url = str(entry.get("url") or entry.get("relay") or "").strip()
        if not url:
            raise ValueError(f"relays.read entry has no url: {entry!r}")
        urls.append(url)
        if entry.get("kinds") is not None:
            kinds = tuple(dict.fromkeys(int(kind) for kind in entry["kinds"]))
            if not kinds:
                raise ValueError(f"relays.read entry has an empty kind set: {entry!r}")
            declared[url] = kinds
    return urls, declared


def _peer_exclusions(cfg: dict[str, Any]) -> tuple[str, ...]:
    """Relays a peer must never drag into the DM read set.

    White Noise relays carry the Marmot plane's groups, refuse public kinds and are not
    a general NIP-17 inbox (README, "Relays and kinds"). A peer listing one is saying
    where their own inbox is, not asking us to subscribe to a group relay.
    """
    relays = cfg.get("relays") or {}
    values = [*(cfg.get("whitenoise") or []), *(relays.get("whitenoise") or [])]
    return tuple(str(value) for value in values if str(value).strip())


def default_marmot_home() -> Path:
    return Path(os.environ.get("MARMOT_HOME") or Path.home() / ".marmot-agents/hermes")


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "hermes-nostr-gateway.sock"
    return Path.home() / ".local/state/hermes-nostr-gateway/gateway.sock"


# -- Marmot ownership (plan P0, docs/marmot-ownership.md) ----------------
#
# One wn-agent control socket, one reader. Hermes' Marmot platform plugin and this
# gateway's `marmot` plane subscribe to the same account, so both attached means the
# agent gets every group message twice: two runs, two replies, duplicate side effects.
# That used to be a paragraph in the README and a log line; it is now a startup error,
# because a rule that only lives in prose is re-broken by the next person in a hurry.

OWNERSHIP_DOC = "docs/marmot-ownership.md"

#: The Hermes config key the guard is about. Named once so the error, the log and the
#: tests all quote the same setting.
HERMES_MARMOT_KEY = "platforms.marmot.enabled"


class MarmotOwnershipError(RuntimeError):
    """Both readers of one wn-agent socket are switched on: pick a transport."""


def hermes_config_path(cfg: dict[str, Any]) -> Path:
    """Where Hermes' own config lives, as seen by this process.

    `marmot.hermes_config` wins (a gateway in a container or beside a second Hermes
    home needs that), then `$HERMES_HOME`, then the default `~/.hermes/config.yaml`.
    """
    configured = cfg.get("hermes_config")
    if configured:
        return Path(os.path.expanduser(str(configured)))
    home = os.environ.get("HERMES_HOME") or "~/.hermes"
    return Path(os.path.expanduser(home)) / "config.yaml"


def hermes_marmot_state(cfg: dict[str, Any]) -> dict[str, Any]:
    """Whether Hermes' Marmot platform is attached to the same socket, and how we know.

    Answers `{enabled, source}` where `enabled` is True, False, or None for "no
    evidence either way". `marmot.hermes_platform` can pin it (`true`/`false`); the
    default `auto` reads `platforms.marmot.enabled` out of Hermes' config, because that
    is the setting this guard exists for and a copy of it in our config would drift out
    of step exactly when it matters.

    Absence is not consent: a host with no Hermes config (a container, a relay box)
    has no second subscriber, so `auto` there returns None — reported, never refused.
    A config that exists without a `platforms.marmot` block means the platform is not
    enabled, which is Hermes' own default.
    """
    source = "marmot.hermes_platform"
    value = cfg.get("hermes_platform", "auto")
    if isinstance(value, bool):
        return {"enabled": value, "source": source}
    choice = str(value if value is not None else "auto").strip().lower()
    if choice in {"true", "yes", "on", "enabled"}:
        return {"enabled": True, "source": source}
    if choice in {"false", "no", "off", "disabled"}:
        return {"enabled": False, "source": source}
    if choice not in {"", "auto", "unknown"}:
        raise ValueError(
            f"marmot.hermes_platform must be auto, true or false, not {value!r}")

    path = hermes_config_path(cfg)
    if not path.is_file():
        return {"enabled": None, "source": f"no Hermes config at {path}"}
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        # Unreadable is unknown, not "off": a guard that fails open silently would be
        # worse than no guard, so the state says so and `status` carries the reason.
        return {"enabled": None, "source": str(path), "error": str(exc)[:200]}
    platform = (data.get("platforms") or {}).get("marmot") if isinstance(data, dict) else None
    enabled = bool(platform.get("enabled")) if isinstance(platform, dict) else False
    return {"enabled": enabled, "source": str(path)}


def marmot_ownership_error(state: dict[str, Any]) -> str:
    """The refusal, with both ways out and the doc that explains the decision."""
    source = state.get("source") or "Hermes' config"
    return (
        "marmot.enabled: true while Hermes' Marmot platform is still enabled "
        f"({source} says {HERMES_MARMOT_KEY}: true). Both read the same wn-agent "
        "control socket, so every group message would reach the agent twice, with two "
        "replies and duplicate side effects. Choose one transport: either disable the "
        "Hermes Marmot plugin (set platforms.marmot.enabled: false and `hermes plugins "
        "disable marmot`) and keep marmot.enabled: true, or keep the plugin and "
        f"set marmot.enabled: false. Pin it here with marmot.hermes_platform: false "
        f"only when the plugin is genuinely off. See {OWNERSHIP_DOC}."
    )


def _media_error(code: str, message: str) -> dict[str, Any]:
    """One shape for every `media.get` refusal: a code the adapter can branch on."""
    return {"type": "error", "op": "media.get", "code": code, "error": message[:300]}


class Gateway:
    #: Logged and reported whenever the plane is not attached for want of an opt-in.
    #: Kept as one string so the log line, `hng status` and a refused send all tell
    #: the operator the same thing.
    MARMOT_OPT_IN_REASON = (
        "opt-in only: set marmot.enabled: true to attach it. It is idle by default "
        "because Hermes' own Marmot platform plugin reads the same wn-agent socket, "
        "so both attached deliver every group message to the agent twice; see "
        "docs/marmot-ownership.md for the ownership decision"
    )

    def __init__(self, cfg: dict[str, Any], *, socket_path: str | Path | None = None,
                 peer_resolver: InboxResolver | None = None):
        from .cli import normalise_groups, secret_from

        self.cfg = cfg
        # Before the secret is read and before any store or socket is opened: a
        # configuration that would deliver every Marmot message twice is not a
        # process to start and then warn about.
        self.hermes_marmot = self._guard_marmot_ownership(cfg.get("marmot") or {})
        self._secret = secret_from(cfg)
        self.crypto = SdkCrypto(self._secret)
        # Publish transport (plan P2): one `nostr-sdk` client, one signed event per
        # message, per-relay results. `transport.legacy: true` keeps the `nak` children
        # as the rollback — the same flag that keeps `relay.RelayWorker` on reads.
        self.publisher: Publisher = make_publisher(cfg, self._secret)
        self.me = self.crypto.our_pubkey()
        store_path = Path(os.path.expanduser(cfg.get("store", "gateway.db")))
        store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(store_path, identity=self.me)
        # Store writes leave the event loop: `store.add`, `wrapper_seen` and `note_relay`
        # hand their rows to this writer, which commits them on its own thread in batches
        # (see writer.py). Attached in `start()`, so nothing is queued before the daemon
        # is actually reading relays.
        self.store_writer = StoreWriter(
            self.store, **store_write_options(cfg.get("store_write")))
        #: Frames waiting on their row to be committed. Tracked so `stop()` can let them
        #: finish instead of closing the store underneath them.
        self._deliveries: set[asyncio.Task[None]] = set()
        # Media (spec §4b): attachments are recorded with the message and fetched only
        # when the adapter asks, so this is configuration plus a cache — nothing here
        # touches the network at startup.
        self.media_cfg: MediaConfig = media_config(cfg.get("media"))
        self.media = self.media_cfg.fetcher()
        # Who is a bot (kind-0 `bot: true`). The registry is created before the policy
        # because the policy asks it on every decision; its fetch resolves the publish
        # set lazily, so a relay list that is parsed a few lines below is still the one
        # used at fetch time.
        self.bots = BotRegistry(self.store, fetcher=kind0_fetcher(lambda: self.relays_publish))
        groups = normalise_groups(cfg)
        self.policy = Policy({
            "groups": groups,
            "policies": cfg.get("policies"),
            "defaults": cfg.get("defaults"),
            "rate_limit": cfg.get("rate_limit"),
            "bots": cfg.get("bots"),
        }, bot_lookup=self.bots.is_bot, echo_lookup=self._echo_lookup)
        self.concord = ConcordPlane(
            self.store,
            projects=ProjectMap.from_config(cfg.get("concord") or {}))
        self.plane = Nip17Plane(self.crypto, self.store, self.policy,
                                on_invite=self.concord.handle_invite)
        self.nip01 = Nip01Plane(self.store, self.policy, self.me, feed_rules(cfg.get("feeds")))
        # The Marmot plane proxies wn-agent rather than speaking MLS: it attaches
        # only when opted in *and* that daemon is reachable (see _setup_marmot).
        self.marmot: MarmotPlane | None = None
        self.marmot_resolver: MarmotResolver | None = None
        self.marmot_worker: MarmotWorker | None = None
        self.marmot_error: str | None = None
        #: Why the plane is attached to nothing, when it is idle by choice or by a
        #: missing socket. `marmot_error` is for failures; this is for "not asked".
        self.marmot_idle: str | None = None
        self._setup_marmot(cfg.get("marmot") or {})
        self.follows: tuple[str, ...] = self._load_follows()
        self.relays_read, self.relay_kinds = relay_read_plan(cfg)
        # What config asked for, kept separately: `relays_read` grows with peer inboxes.
        self.relays_configured = list(self.relays_read)
        self.relays_publish = list(cfg["relays"].get("publish", []))
        # Read coverage beyond the fixed list (spec §3): the kind-10050 inbox relays of
        # the people named in `groups`, bounded by a hard cap and refreshed on an
        # interval. Additive and sticky — see refresh_peer_relays.
        self.peer_cfg: PeerInboxConfig = peer_inbox_config(cfg)
        self.peer_resolver: InboxResolver | None = peer_resolver
        self.peer_pubkeys = peer_pubkeys(groups)
        self.peer_relays: list[str] = []
        self.peer_exclusions = _peer_exclusions(cfg)
        self.peer_errors: dict[str, str] = {}
        self.peer_last_refresh: int | None = None
        self._dead_peer_relays: set[str] = set()
        self._cap_blocked: dict[str, str] = {}
        if self.peer_cfg.enabled:
            raw_members = {str(member).strip().lower()
                           for members in groups.values() for member in (members or [])}
            if len(self.peer_pubkeys) < len(raw_members):
                log.warning(
                    "peer inbox: %d group member(s) are not hex pubkeys and will not be "
                    "resolved", len(raw_members) - len(self.peer_pubkeys))
            if not self.peer_cfg.lookup and peer_resolver is None:
                log.warning("peer inbox enabled but no lookup relays: set "
                            "relays.peer_inbox.lookup (or relays.read/publish)")
        self.socket_path = Path(socket_path) if socket_path else Path(
            os.path.expanduser(cfg.get("socket") or default_socket_path())
        )
        # Replay is for "crashed mid-delivery", not "history": minutes, and a floor.
        replay = cfg.get("replay") or {}
        self.replay_limit = int(replay.get("limit", 20))
        self.replay_max_age_minutes = int(replay.get("max_age_minutes", 120))
        # Retry TTL: age bounds a dead relay's backlog, not the per-row attempt cap —
        # that only helps rows that get a turn. Expired rows are kept (not deleted) so
        # an operator can still see what was given up on.
        retry = cfg.get("retry") or {}
        ttl_hours = float(retry.get("ttl_hours", 24))
        if ttl_hours <= 0:
            raise ValueError("retry.ttl_hours must be positive")
        self.retry_ttl_seconds = int(ttl_hours * 3600)
        # Drops TTL: how long a refusal stays "live" in `drops`/`status`. Expired rows are
        # listed and counted, never deleted — the same rule as an expired wrap.
        self.drops_ttl_seconds = drops_ttl_seconds(cfg.get("drops"))
        self.server = SocketServer(self.socket_path, self)
        self.workers: list[Any] = []
        # Read transport (plan P1): one `nostr-sdk` client, one subscription per filter
        # set, NIP-42 answered in-process. `transport.legacy: true` keeps the per-relay
        # `nak req --stream` children for one release, so a relay that behaves
        # differently can be compared side by side; it is the rollback.
        self.legacy_transport = bool((cfg.get("transport") or {}).get("legacy", False))
        self.pool: RelayPool | None = None
        if not self.legacy_transport:
            self.pool = RelayPool(self._secret, on_refusal=self._note_refusal)
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        #: Set by `start()`. `status`/`health` report the age of this number, not the age
        #: of the process: a construct-only Gateway (a test, a `--once` run) has no uptime
        #: to claim, and `None` says so instead of counting from import.
        self._started_at: float | None = None

    def plane_names(self) -> list[str]:
        """The planes this process attached, in a stable order.

        `status` and `health` must agree on this: a plane missing from `health` is a
        plane the cron will never mention, which is how a whole transport goes quiet
        without anyone noticing.
        """
        return default_planes(marmot=self.marmot is not None)

    def _guard_marmot_ownership(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Refuse to start with two readers on one socket (plan P0).

        `marmot.enabled: true` is this process saying it is the Marmot transport.
        Hermes' own `platforms.marmot` plugin reading the same wn-agent account is the
        other reader, and two of them mean every group message is delivered twice.
        The decision is recorded in `docs/marmot-ownership.md`: the gateway owns the
        socket and the plugin retires, so the conflict is an error rather than a
        warning nobody reads.

        Returns the evidence for `status`, so an operator can see *why* the guard did
        or did not fire instead of guessing which config it read.
        """
        state = hermes_marmot_state(cfg)
        if cfg.get("enabled") is True and state.get("enabled") is True:
            raise MarmotOwnershipError(marmot_ownership_error(state))
        return state

    def _new_worker(self, relay: str, *, kinds: tuple[int, ...],
                    p_tag: str | None = None, authors: tuple[str, ...] = (),
                    purpose: str = "dm") -> Any:
        """Build a read worker on whichever transport is configured.

        Both classes expose the same counters and `live_state()`, which is what `status`,
        `health` and `_guarded` consume — so the rest of the gateway does not branch.
        """
        if self.pool is not None:
            return PoolWorker(self.pool, relay, self.me, kinds=kinds, p_tag=p_tag,
                              authors=authors, purpose=purpose,
                              on_refusal=self._note_refusal)
        return RelayWorker(relay, self.me, kinds=kinds, p_tag=p_tag, authors=authors,
                           secret=self._secret, purpose=purpose,
                           on_refusal=self._note_refusal)

    def _setup_marmot(self, cfg: dict[str, Any]) -> None:
        """Attach the Marmot proxy only when the operator opted in *and* wn-agent is there.

        Opt-in, not opt-out. The gateway is not the only reader of wn-agent's
        control socket: Hermes' Marmot platform plugin subscribes to the same
        account, so a gateway plane attached by default hands the agent a second
        copy of every group message. `marmot.enabled: true` is the operator saying
        this process is the transport — and `_guard_marmot_ownership` has already
        refused the configuration where both readers are on. Once opted in, a missing
        socket still means
        "nothing to attach to" and stays quiet; a socket that exists but cannot be
        used (no account file, two of them) disables only this plane and records why
        in `marmot_error` — the NIP-17 planes must not go down because a second
        daemon is mid-install, and a silent disable would look exactly like a
        working plane with nothing to say.
        """
        if cfg.get("enabled") is not True:
            self.marmot_idle = self.MARMOT_OPT_IN_REASON
            log.info("marmot plane idle: %s", self.marmot_idle)
            return
        home = Path(os.path.expanduser(str(cfg.get("home") or default_marmot_home())))
        socket = Path(os.path.expanduser(str(
            cfg.get("socket") or cfg.get("socket_path") or home / "dev/wn-agent.sock"
        )))
        if not socket.exists():
            self.marmot_idle = f"no wn-agent control socket at {socket}"
            log.info("marmot plane idle: %s", self.marmot_idle)
            return
        try:
            account = str(cfg.get("account_id_hex") or "").strip() or resolve_account_id(
                home, cfg.get("label")
            )
            control = MarmotControl(socket, request_timeout=float(cfg.get("request_timeout", 30)))
            plane = MarmotPlane(self.store, self.policy, control=control, account_id_hex=account)
        except MarmotError as exc:
            self.marmot_error = str(exc)
            log.error("marmot plane disabled: %s", exc)
            return
        self.marmot = plane
        self.marmot_resolver = MarmotResolver(plane)
        self.marmot_worker = MarmotWorker(plane, resolver=self.marmot_resolver,
                                          group_id_hex=cfg.get("group"))
        log.info("marmot plane via %s (account %s…)", socket, account[:12])

    # -- lifecycle ------------------------------------------------------
    async def start(self) -> None:
        await self.server.start()
        self._started_at = time.time()
        # Before any relay worker exists: from here on, a store write is a queue append
        # and the commit happens on the writer's thread.
        await self.store_writer.start()
        self.store.attach_writer(self.store_writer)
        # The publish set is known up front: connect it once, rather than paying a
        # handshake per relay on the first message.
        await self.publisher.warm(self.relays_publish)
        for relay in self.relays_read:
            # ONE subscription per relay for every inbound kind we care about.
            # One connection per plane is what tripped our own relay's limiter (429)
            # and multiplies reconnects across every relay.
            # DMs and mentions: wraps/notes addressed to us. Restricted relays
            # (our chat path, whitenoise) refuse public kinds outright.
            restricted = restricted_relay(relay)
            # Per relay, not per gateway: a relay that refuses one kind closes the
            # whole REQ, so `kinds` is config-pinnable and then negotiated at runtime.
            kinds = self.relay_kinds.get(relay) or DM_KINDS
            worker = self._new_worker(relay, kinds=kinds, p_tag=self.me)
            self.workers.append(worker)
            self._tasks.append(asyncio.create_task(self._guarded(worker)))
            # Feeds: kind 1 from people WE FOLLOW, never the global firehose.
            authors = tuple(sorted(set(self.follows) | set(self.nip01.authors)))
            if authors and not restricted:
                feed = self._new_worker(relay, kinds=(1,), authors=authors, purpose="feed")
                self.workers.append(feed)
                self._tasks.append(asyncio.create_task(self._guarded(feed)))
            # Mentions of us by strangers: a small #p filter, separate from follows.
            if not restricted:
                mentions = self._new_worker(relay, kinds=(1,), p_tag=self.me, purpose="mentions")
                self.workers.append(mentions)
                self._tasks.append(asyncio.create_task(self._guarded(mentions)))
        self._tasks.append(asyncio.create_task(self._retry_loop()))
        self._tasks.append(asyncio.create_task(self._digest_loop()))
        if self.peer_cfg.enabled:
            # Background, not a gate on startup: the fixed read set comes up now and
            # peer inboxes join as they resolve.
            self._tasks.append(asyncio.create_task(self._peer_relay_loop()))
        if self.marmot_worker is not None:
            self._tasks.append(asyncio.create_task(self._guard_marmot(self.marmot_worker)))
        log.info("gateway up: %s on %d relays", self.me, len(self.workers))

    def _load_follows(self) -> tuple[str, ...]:
        """Our kind-3 contact list: what 'feeds' means, as opposed to the firehose."""
        from .fetch import fetch_first_blocking

        publish = list((self.cfg.get("relays") or {}).get("publish") or [])
        event = fetch_first_blocking(relays=publish[:2], kinds=[3], author=self.me)
        if not event:
            return ()
        return tuple(sorted({tag[1] for tag in event.get("tags", [])
                             if tag and tag[0] == "p" and len(tag) > 1}))

    def _note_refusal(self, relay: str, dropped: set[int],
                      remaining: tuple[int, ...], error: str | None) -> None:
        """Persist the kind negotiation, so `hng status` shows recovery, not a dead relay.

        Without this the store's last error stays the raw CLOSED frame while the
        subscription is in fact healthy again on the kinds the relay does accept.
        """
        detail = f"relay refuses kind(s) {', '.join(str(k) for k in sorted(dropped))}"
        detail += (f"; reading {', '.join(str(k) for k in remaining)}" if remaining
                   else "; no kinds left")
        if error:
            detail += f" ({error})"
        self.store.note_relay(relay, error=detail[:300])

    # -- peer inbox coverage --------------------------------------------
    async def _peer_relay_loop(self) -> None:
        """Resolve peer inboxes at startup, then on the configured interval."""
        while not self._stopping.is_set():
            try:
                await self.refresh_peer_relays()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - coverage must not take the daemon down
                log.exception("peer inbox refresh failed")
            every = self.peer_cfg.refresh_seconds
            if every <= 0:
                return  # startup-only mode: one pass, then nothing to wait for
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=every)
            except asyncio.TimeoutError:
                continue

    def _inbox_resolver(self) -> InboxResolver | None:
        if self.peer_resolver is not None:
            return self.peer_resolver
        if not self.peer_cfg.lookup:
            return None
        self.peer_resolver = NakInboxResolver(self.peer_cfg.lookup, secret=self._secret)
        return self.peer_resolver

    def _add_peer_relay(self, relay: str) -> RelayWorker:
        """One DM worker for a peer's inbox — deliberately not a feed/mention pair.

        The configured read set costs up to three connections per relay (DM, feeds,
        mentions). A peer's inbox is a DM path only, so `max_extra` is the real bound
        on new connections rather than three times it.
        """
        worker = self._new_worker(relay,
                                  kinds=self.relay_kinds.get(relay) or DM_KINDS,
                                  p_tag=self.me, purpose="peer-inbox")
        self.relays_read.append(relay)
        self.peer_relays.append(relay)
        self.workers.append(worker)
        self._tasks.append(asyncio.create_task(self._guarded(worker)))
        return worker

    async def refresh_peer_relays(self) -> list[str]:
        """Resolve configured peers' 10050 inboxes and add new relays, up to the cap.

        Additive and sticky: a relay, once added, stays for the process lifetime. A
        lookup that times out or a peer that rotates their list must not remove a relay
        that may still hold an unread wrap, and re-adding a working relay would cost a
        reconnect for nothing. The cap is on total additions, so sticky cannot grow
        without bound; refresh only picks up relays newly advertised by a peer.
        """
        if not self.peer_cfg.enabled:
            return []
        remaining = self.peer_cfg.max_extra - len(self.peer_relays)
        if remaining <= 0 or not self.peer_pubkeys:
            return []
        resolver = self._inbox_resolver()
        if resolver is None:
            return []
        loop = asyncio.get_running_loop()
        candidates: list[str] = []
        # One peer at a time: each lookup opens a connection to every lookup relay, and
        # a burst is exactly what trips a relay's connection limiter.
        for pubkey in self.peer_pubkeys:
            try:
                found = await loop.run_in_executor(None, resolver.inbox_relays, pubkey)
            except Exception as exc:  # noqa: BLE001 - one peer must not stop the rest
                self.peer_errors[pubkey] = str(exc)[:200]
                log.warning("peer inbox lookup failed for %s…: %s", pubkey[:12], exc)
                continue
            self.peer_errors.pop(pubkey, None)
            candidates.extend(found or [])

        additions = inbox_additions(base=self.relays_read, candidates=candidates,
                                    cap=remaining, exclude=self.peer_exclusions)
        if self._stopping.is_set():
            return []
        for relay in additions:
            self._add_peer_relay(relay)
        self.peer_last_refresh = now_seconds()
        if additions:
            log.info("peer inbox: +%d relay(s) %s (%d/%d extra in the read set)",
                     len(additions), ", ".join(additions), len(self.peer_relays),
                     self.peer_cfg.max_extra)
        # A relay a peer advertises but the cap will not admit is a known blind spot:
        # say so once, and show it in the socket `status` op, instead of silently not
        # reading it.
        blocked = inbox_additions(base=self.relays_read, candidates=candidates,
                                  cap=max(len(candidates), 1), exclude=self.peer_exclusions)
        for url in blocked:
            key = normalise_relay(url)
            if key not in self._cap_blocked:
                self._cap_blocked[key] = url
                log.warning("peer inbox: cap of %d extra relay(s) reached; not reading %s",
                            self.peer_cfg.max_extra, url)
        return additions

    async def _guarded(self, worker: RelayWorker) -> None:
        try:
            await worker.run(self._on_event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one relay must not take the daemon down
            log.exception("relay worker %s died", worker.relay)
        finally:
            self.store.note_relay(worker.relay, error=worker.last_error)
            if getattr(worker, "purpose", "dm") == "peer-inbox" and not worker.kinds:
                # It refused every kind, so it can never deliver and its worker has
                # stopped. It stays in the read set (no reconnect, and refresh will not
                # add it twice); status names it as a dead inbox rather than a quiet one.
                self._dead_peer_relays.add(worker.relay)

    async def _guard_marmot(self, worker: MarmotWorker) -> None:
        """Kept separate from _guarded: the Marmot worker is a socket peer, not a
        relay, and must not appear in the per-relay counters."""
        try:
            await worker.run(self._on_marmot)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the proxy must not take the daemon down
            log.exception("marmot worker died")

    async def stop(self) -> None:
        self._stopping.set()
        for worker in self.workers:
            worker.stop()
        for task in self._tasks:
            task.cancel()
        self.bots.cancel()
        # A worker that is mid-`nak` may take a moment to unwind; it must not hold the
        # process past systemd's stop timeout, or the unit dies by SIGKILL and in-flight
        # publishes are lost with it.
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), 6)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("workers did not unwind in 6s; stopping anyway")
        # A frame already accepted must reach the adapter: it is only waiting on its
        # flush, so let it finish while the server and the writer are both still up.
        if self._deliveries:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*list(self._deliveries), return_exceptions=True), 5)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("delivery tasks did not finish in 5s; stopping anyway")
        if self.pool is not None:
            await self.pool.stop()
        await self.server.stop()
        # Queued rows are committed before the connection goes: a clean stop loses
        # nothing, and the writer thread is joined so no write outlives the store.
        await self.store_writer.stop()
        self.store.detach_writer()
        await self.publisher.aclose()
        self.store.close()

    async def run_forever(self) -> None:
        await self.start()
        await self._stopping.wait()

    # -- inbound --------------------------------------------------------
    @staticmethod
    def frame_for(msg: Any) -> dict[str, Any]:
        """A message frame, or a `blocked` frame when policy refused it.

        A blocked message is still stored (see §13.3) but must never look like chat:
        the adapter turns `type: message` into an agent run, and a `drop` reach used
        to ride that path silently. `blocked` carries the reason and no content.
        """
        if msg.reach == "drop" or msg.blocked_reason:
            return {
                "type": "blocked",
                "plane": msg.plane,
                "from": msg.sender,
                "reason": msg.blocked_reason or "not_allowlisted",
                "rumor_id": msg.id,
            }
        return msg.to_frame()

    def _echo_lookup(self, peer: str, since: int) -> list[str]:
        """Our own text to one peer since `since` — evidence for the echo rule.

        The store, not a process buffer: a bounce survives a restart, and `hng send`
        writes the same rows as the daemon, so one rule covers both senders.
        """
        return [row["content"] or "" for row in self.store.recent_outbound(peer, since)]

    def _on_event(self, relay: str, event: dict) -> None:
        kind = event.get("kind")
        if kind == 0:
            # Profile metadata: the bot flag the policy needs, never a message. We do
            # not subscribe to kind 0 today (it would cost a worker per relay for a
            # claim that changes rarely), but a relay that offers one is worth taking.
            self.bots.ingest(event)
            return
        if kind == 1059:
            msg = self.plane.handle(relay, event)
        elif kind == 4:
            msg = self._handle_nip04(relay, event)
        else:
            msg = self.nip01.handle(relay, event)
        if msg is None:
            return
        self.store.note_relay(relay, events=1)
        # Immediate is the only reach that becomes a socket frame now: digest rows wait
        # for their window. A drop or a rate-limited row still has to emit its `blocked`
        # frame, or a refusal reaches the agent as silence.
        if msg.reach == "immediate" or msg.reach == "drop" or getattr(msg, "blocked_reason", None):
            self._emit(msg)

    def _emit(self, msg: Any) -> None:
        """Hand a frame to the adapter, after its row is on disk.

        The spec's promise is that a frame is delivered when it is inserted (§7). The
        writer defers the insert by up to a tick, so the frame waits for that flush —
        in a task, never in the read callback, so the loop stays free. Rows that arrive
        while the flush runs are committed by the next one.
        """
        task = asyncio.create_task(self._deliver(self.frame_for(msg)))
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    async def _deliver(self, frame: dict[str, Any]) -> None:
        failures = self.store_writer.failures
        written = await self.store.flush()
        if self.store_writer.failures > failures:
            log.warning(
                "delivering %s even though its store write failed (flushed %d row(s))",
                frame.get("id") or frame.get("rumor_id"), written)
        await self.server.broadcast(frame)

    async def flush_writes(self) -> int:
        """Commit queued rows before a socket read op answers.

        `history`, `status`, `media.get` and an `ack` all read what the daemon has
        accepted; the writer commits a tick later, so the read waits for it rather than
        reporting a message that exists as missing. The cost is an executor hop on an op
        that is already a round trip.
        """
        return await self.store.flush()

    def _on_marmot(self, event: dict) -> None:
        """A control event from wn-agent. Same normalised frame as any other plane."""
        if self.marmot is None:
            return
        try:
            msg = self.marmot.handle(event)
        except Exception:  # noqa: BLE001 - one bad event must not drop the subscription
            log.exception("marmot event rejected")
            return
        if msg is None:
            return
        self._emit(msg)


    def _handle_nip04(self, relay: str, event: dict) -> Any:
        """Legacy kind-4 DM: decrypt with the binding, then the same policy path."""
        from .store import Inbound

        sender = str(event.get("pubkey") or "")
        if not sender or sender == self.me:
            return None
        try:
            _, text = nip04.open_event(self.crypto.keys, event)
        except Exception:  # noqa: BLE001 - undecryptable is not fatal
            return None
        decision = self.policy.decide(sender=sender, kind=4, dm=True,
                                      event_id=str(event.get("id") or ""), text=text)
        if decision.duplicate:
            return None
        msg = Inbound(
            id=str(event.get("id") or ""), wrapper_id=str(event.get("id") or ""),
            sender=sender, kind=4, created_at=int(event.get("created_at") or 0),
            content=text, conversation=sender, reply_to=None, plane="nip04",
            reach=decision.reach, authority=decision.authority,
            rule=decision.rule, why=decision.why, interval_s=decision.interval_s,
            blocked_reason=decision.blocked_reason,
        )
        return msg if self.store.add(msg) else None

    # -- outbound -------------------------------------------------------
    async def send(self, *, to: str, text: str, reply_to: str | None = None,
                   cid: str | None = None) -> dict[str, Any]:
        target = str(to or "").strip()
        explicit_marmot = target.lower().startswith("marmot:")
        if self.marmot_resolver is None:
            # An explicit Marmot target with no plane attached must say so: falling
            # through to NIP-17 would fail as "bad pubkey", which sends the caller
            # looking in the wrong place.
            if explicit_marmot:
                detail = self.marmot_error or self.marmot_idle
                suffix = f": {detail}" if detail else ""
                return {"type": "delivery", "id": cid, "ok": False,
                        "error": f"marmot plane is not enabled{suffix}"}
        else:
            group = self.marmot_resolver.parse(target)
            if group is not None:
                return await self._send_marmot(group, text, reply_to, cid)
        peer = self._hex(to)
        loop = asyncio.get_running_loop()
        try:
            rumor, wraps = await loop.run_in_executor(None, self._build_wraps, text, peer, reply_to)
        except Exception as exc:  # noqa: BLE001 - report, do not kill the socket
            return {"type": "delivery", "id": cid, "ok": False, "error": str(exc)[:300]}

        relays = self._recipient_relays(peer)
        for event in wraps.values():
            self.store.record_wrap(event, rumor["id"], peer, relays)

        results = await self._publish_all(wraps, relays)
        for label, per_relay in results.items():
            for relay, (ok, error) in per_relay.items():
                self.store.mark_wrap(wraps[label]["id"], relay, ok=ok, error=error)

        peer_ok = any(ok for ok, _ in results["peer"].values())
        if peer_ok:
            # Record what we sent, so history is not one-sided and an answer can be
            # traced back to the message that prompted it.
            from .store import Inbound as _Inbound
            self.store.add(_Inbound(
                id=rumor["id"], wrapper_id=wraps["peer"]["id"], sender=self.me,
                kind=int(rumor.get("kind", 14)), created_at=int(rumor.get("created_at", 0)),
                content=text, conversation=peer, reply_to=reply_to, plane="nip17",
                reach="sent", authority="self", rule="outbound", why="sent by us",
            ))
            # The row is queued, so `mark_delivered` has nothing to update until the
            # writer commits it: flush first, or our own message comes back as pending.
            await self.store.flush()
            self.store.mark_delivered([rumor["id"]])
        return {
            "type": "delivery",
            "id": cid,
            "ok": peer_ok,
            "message_id": rumor["id"],
            "relays": {label: {r: ok for r, (ok, _) in v.items()} for label, v in results.items()},
            "errors": {r: e for label, v in results.items() for r, (ok, e) in v.items() if not ok},
        }

    async def _send_marmot(self, group: str, text: str, reply_to: str | None,
                           cid: str | None) -> dict[str, Any]:
        """Send into a Marmot group through wn-agent, with the group proven first.

        A bad target must fail as a delivery error the caller can read, not as a
        dead socket or a message that silently went nowhere.
        """
        assert self.marmot is not None and self.marmot_resolver is not None
        try:
            await self.marmot_resolver.resolve(group)
            result = await self.marmot.send(
                group, text, reply_to=reply_to, idempotency_key=cid or None
            )
        except MarmotError as exc:
            return {"type": "delivery", "id": cid, "ok": False,
                    "error": f"{exc.code}: {exc}"[:300], "conversation": group}
        except Exception as exc:  # noqa: BLE001 - report, do not kill the socket
            return {"type": "delivery", "id": cid, "ok": False, "error": str(exc)[:300]}
        return {"type": "delivery", "id": cid, "ok": True, "plane": "marmot",
                "conversation": group, "message_id": result["message_id"],
                "message_ids": result["message_ids"], "relays": {}}

    def _build_wraps(self, text: str, peer: str, reply_to: str | None) -> tuple[dict, dict]:
        rumor = self.crypto.build_rumor(text, peer, reply_to=reply_to)
        return rumor, {"peer": self.crypto.wrap(rumor, peer), "self": self.crypto.wrap(rumor, self.me)}

    async def _publish_all(self, wraps: dict, relays: list[str]) -> dict:
        """Publish every wrap through the one client; each was signed once upstream.

        The publisher never re-wraps, so a retry — here, or in `retry_failed` — resends
        the same event id and the peer sees one message, not two (spec §7).
        """
        labels = list(wraps)
        sent = await asyncio.gather(
            *(self.publisher.publish(wraps[label], relays) for label in labels))
        return dict(zip(labels, sent, strict=True))

    def _recipient_relays(self, peer: str) -> list[str]:
        from .fetch import fetch_first_blocking

        relays = list(self.relays_publish)
        event = fetch_first_blocking(relays=relays[:2], kinds=[10050], author=peer)
        for tag in (event or {}).get("tags", []):
            if tag and tag[0] == "relay" and len(tag) > 1 and tag[1] not in relays:
                relays.append(tag[1])
        return relays[:6]

    @staticmethod
    def _hex(who: str) -> str:
        from .fetch import hex_of

        return hex_of(who)

    # -- retry ----------------------------------------------------------
    async def _retry_loop(self, every: int = 300) -> None:
        while not self._stopping.is_set():
            try:
                await self.retry_failed()
            except Exception:  # noqa: BLE001
                log.exception("retry pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=every)
            except asyncio.TimeoutError:
                continue

    async def retry_failed(self) -> int:
        import json as _json
        # Expire first: a row past the TTL must not spend a publish attempt, and a
        # backlog of them must not sit in `pending_wraps` for days.
        expired = self.store.expire_wraps(self.retry_ttl_seconds)
        if expired:
            log.info("expired %d wrap(s) older than the retry TTL (%gh)",
                     expired, self.retry_ttl_seconds / 3600)
        # Retry a few at a time: a burst of replays trips our own relay's connection
        # limiter and turns one failure into a hundred.
        pending = self.store.pending_wraps()[:5]
        if not pending:
            return 0
        sent = 0
        for row in pending:
            event = _json.loads(row["event"])
            # The stored bytes are the signed wrap: resending them is what keeps the id
            # (and the peer's view) stable across attempts.
            outcome = await self.publisher.publish(event, [row["relay"]])
            ok, error = outcome.get(row["relay"], (False, "not attempted"))
            self.store.mark_wrap(row["id"], row["relay"], ok=ok, error=error)
            sent += int(ok)
            import asyncio as _asyncio
            await _asyncio.sleep(1.5)  # stay under the relay's connection limiter
        if sent:
            log.info("retried %d wrap(s)", sent)
        return sent

    # -- digest batching -------------------------------------------------
    async def _digest_loop(self, every: int = 30) -> None:
        """Seal due rule windows and broadcast each batch as one message frame.

        Sealing is durable before the broadcast: a frame nobody receives (no
        subscribers) stays unacked and replays on the next subscribe, so silence is
        never mistaken for delivery.
        """
        while not self._stopping.is_set():
            try:
                for frame in self.store.seal_digests():
                    await self.server.broadcast(frame)
            except Exception:  # noqa: BLE001 - one bad rule must not kill the daemon
                log.exception("digest pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=every)
            except asyncio.TimeoutError:
                continue

    def replay_digests(self, since_frame: int = 0) -> list[dict[str, Any]]:
        return self.store.replay_digests(since_frame)

    # -- socket ops -----------------------------------------------------
    async def status(self) -> dict[str, Any]:
        stats = self.store.stats()
        return {
            "identity": self.me,
            "socket": str(self.socket_path),
            "relays_read": self.relays_read,
            "relays_configured": self.relays_configured,
            "planes": self.plane_names(),
            "relays_publish": self.relays_publish,
            # Which publish transport is running, and how it has fared: `mode: legacy`
            # is the `nak` rollback, so a status read cannot be mistaken.
            "publish": self.publisher.stats(),
            "peer_inbox": {
                "enabled": self.peer_cfg.enabled,
                "max_extra": self.peer_cfg.max_extra,
                "refresh_minutes": self.peer_cfg.refresh_minutes,
                "peers": len(self.peer_pubkeys),
                "added": list(self.peer_relays),
                "dead": sorted(self._dead_peer_relays),
                "skipped_by_cap": sorted(self._cap_blocked.values()),
                "errors": dict(self.peer_errors),
                "last_refresh": self.peer_last_refresh,
            },
            "rumors": stats["total"],
            "pending": stats["pending"],
            "blocked": stats["blocked"],
            "drops": {**self.store.drop_counts(self.drops_ttl_seconds),
                      "ttl_hours": self.drops_ttl_seconds / 3600},
            "invites": stats["invites"],
            "media": {
                "enabled": self.media_cfg.enabled,
                "servers": list(self.media_cfg.servers),
                "dir": str(self.media_cfg.dir),
                "max_bytes": self.media_cfg.max_bytes,
                "max_files": self.media_cfg.max_files,
                "max_cache_bytes": self.media_cfg.max_cache_bytes,
                **stats["media"],
            },
            "replay": {"limit": self.replay_limit,
                       "max_age_minutes": self.replay_max_age_minutes,
                       "floor": self.store.replay_floor()},
            # How the store is being written: a queue that keeps growing, or a flush
            # time that keeps climbing, is the first sign the disk cannot keep up.
            "store_write": self.store_writer.stats(),
            # Which read transport is running, and what it is doing: `legacy` names the
            # subprocess path so a status read cannot be mistaken for the pool.
            "transport": (self.pool.stats() if self.pool is not None
                          else {"mode": "legacy", "subscriptions": len(self.workers),
                                "events": sum(getattr(w, "events", 0)
                                              for w in self.workers)}),
            "retry": {"ttl_hours": self.retry_ttl_seconds / 3600,
                      "wraps": self.store.wrap_counts()},
            "bots": {**self.policy.bot_status(), **self.bots.status()},
            "relay_stats": stats["relays"],
            # Instrument the plane rather than inferring it from `ps`: a worker that
            # reconnects every 60s and one that died at startup look identical from
            # outside, and only the counters tell them apart.
            "marmot": (
                {"worker": self.marmot_worker.stats(),
                 "conversations": self.marmot_resolver.conversations(),
                 "enumeration": self.marmot_resolver.enumeration(),
                 "hermes_platform": dict(self.hermes_marmot)}
                if self.marmot and self.marmot_worker and self.marmot_resolver
                else {"enabled": False, "error": self.marmot_error,
                      "reason": self.marmot_idle,
                      "hermes_platform": dict(self.hermes_marmot)}
            ),
            "workers": [self._worker_state(w) for w in self.workers],
            "uptime_s": self.uptime_s(),
        }

    def uptime_s(self) -> int | None:
        """Seconds since `start()`, or None when this Gateway never started."""
        return int(time.time() - self._started_at) if self._started_at else None

    async def health(self, *, quiet_minutes: int | None = None,
                     pending_stale_minutes: int | None = None) -> dict[str, Any]:
        """One call, per plane: last event, backlog, blocks, degraded flag.

        The same live worker state the `status` op exposes goes in, so a relay that is
        down degrades every plane with one reason rather than four mysteries. The
        counters come from the store, which is the only place a "last event at" exists.
        """
        now = time.time()
        verdicts = relay_verdicts(
            {"workers": [self._worker_state(w) for w in self.workers],
             "relays_read": self.relays_read},
            relays=self.relays_configured, now=now,
            quiet_minutes=(30 if quiet_minutes is None else int(quiet_minutes)),
        )
        notes: list[str] = []
        if self.marmot is None:
            why = self.marmot_error or self.marmot_idle
            if why:
                notes.append(f"marmot plane not attached: {why}")
        return health_report(
            planes=self.plane_names(),
            plane_rows=self.store.plane_report(),
            verdicts=verdicts,
            wraps=self.store.wrap_report_by_plane(),
            daemon=True,
            identity=self.me,
            socket_path=str(self.socket_path),
            uptime_s=self.uptime_s(),
            now=now,
            pending_stale_minutes=(DEFAULT_PENDING_STALE_MINUTES
                                   if pending_stale_minutes is None
                                   else int(pending_stale_minutes)),
            notes=notes,
        )

    @staticmethod
    def _worker_state(worker: Any) -> dict[str, Any]:
        """Per-worker live state, including the fields a health verdict needs.

        Falls back to the counters for a worker that does not implement `live_state`
        (a test double, or a plane that is not a `RelayWorker`).
        """
        state = getattr(worker, "live_state", None)
        if callable(state):
            return dict(cast(dict[str, Any], state()))
        return {"relay": worker.relay, "kinds": list(worker.kinds),
                "events": getattr(worker, "events", 0),
                "last_error": getattr(worker, "last_error", None)}

    def history(self, limit: int) -> dict[str, Any]:
        return {"messages": self.store.recent(limit, with_attachments=True)}

    def drops(self, *, plane: str | None = None, reason: str | None = None,
              since: int | None = None, limit: int = 100) -> dict[str, Any]:
        """Refused messages, with why and when, aged by the drops TTL (spec §5)."""
        return self.store.drops(plane=plane, reason=reason, since=since, limit=limit,
                                ttl_s=self.drops_ttl_seconds)

    def peers(self) -> dict[str, Any]:
        seen: dict[str, int] = {}
        for row in self.store.recent(500):
            if row["authority"] != "self":
                seen[row["sender"]] = max(seen.get(row["sender"], 0), row["created_at"])
        return {"peers": [{"pubkey": k, "last_seen": v} for k, v in
                          sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]}

    def replay_pending(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Re-offer unacknowledged messages, tagged so a consumer can recognise them.

        `replay: true` is the adapter's cue: a frame it has already handed to the agent
        (the ack failed, so the row is still pending) must be acknowledged again rather
        than answered twice. The window and floor live in the store.
        """
        frames = []
        for row in self.store.pending_immediate(limit or self.replay_limit,
                                                self.replay_max_age_minutes):
            frame = {
                "type": "message", "plane": row["plane"], "id": row["id"],
                "sender": row["sender"], "conversation": row["conversation"],
                "kind": row["kind"], "created_at": row["created_at"],
                "content": row["content"], "reply_to": row["reply_to"],
                "reach": row["reach"], "authority": row["authority"], "why": row["why"],
                "replay": True,
            }
            # A replay has to carry the same attachments as the original frame, or the
            # adapter sees a photo it can no longer ask for.
            if row.get("attachments"):
                frame["attachments"] = row["attachments"]
            frames.append(frame)
        return frames

    def replay_skip(self) -> dict[str, Any]:
        """Record a handover: the current backlog was answered by the previous adapter."""
        skipped = self.store.skip_pending()
        log.info("replay floor set to %s: %d pending message(s) marked delivered",
                 self.store.replay_floor(), skipped)
        return {"skipped": skipped, "replay_floor": self.store.replay_floor()}

    def ack(self, ids: list[str] | None = None, *, rule: str | None = None,
            frame: int | None = None) -> dict[str, Any]:
        if rule is not None and frame is not None:
            return self.store.ack_digest(rule, int(frame))
        self.store.mark_delivered(ids or [])
        return {"acked": ids or []}

    # -- media (spec §4b) ------------------------------------------------
    async def media_get(self, *, sha256: str | None = None, url: str | None = None,
                        rumor_id: str | None = None) -> dict[str, Any]:
        """Resolve a stored attachment, fetch and verify it, hand back a local path.

        The download is blocking, so it runs in the executor; the store write that
        records the cache file stays on the loop thread, where the connection lives
        (`sqlite3` connections are not shared across threads here).
        """
        if not self.media_cfg.enabled:
            return _media_error("media_disabled",
                                "media is disabled: set media.enabled: true")
        try:
            entry = resolve_attachment(self.store, sha256=sha256, url=url,
                                       rumor_id=rumor_id)
        except MediaError as exc:
            return _media_error(exc.code, str(exc))
        try:
            result = await asyncio.get_running_loop().run_in_executor(
                None, self.media.fetch, entry)
        except MediaError as exc:
            return _media_error(exc.code, str(exc))
        except OSError as exc:
            # A full disk or an unwritable cache is a named refusal too: the adapter
            # should not have to treat "no code" as a special case.
            return _media_error("cache_failed", f"cannot cache the blob: {exc}")
        self.store.mark_media_fetched(result["sha256"], result["path"])
        return {"type": "media", "op": "media.get", **result}

    # -- Marmot reads (over the same socket, same normalised shapes) ------
    def _marmot_unavailable(self) -> str:
        return self.marmot_error or self.marmot_idle or "marmot plane not enabled"

    async def marmot_group(self, group: str) -> dict[str, Any]:
        if self.marmot is None or self.marmot_resolver is None:
            return {"type": "error", "op": "marmot.group",
                    "error": self._marmot_unavailable()}
        try:
            info = await self.marmot_resolver.resolve(group)
        except MarmotError as exc:
            return {"type": "error", "op": "marmot.group", "code": exc.code,
                    "error": str(exc)[:300]}
        return {"type": "marmot_group", **info}

    async def marmot_timeline(self, group: str, limit: int = 20) -> dict[str, Any]:
        if self.marmot is None or self.marmot_resolver is None:
            return {"type": "error", "op": "marmot.timeline",
                    "error": self._marmot_unavailable()}
        try:
            await self.marmot_resolver.resolve(group)
            page = await self.marmot.timeline(group, limit=limit)
        except MarmotError as exc:
            return {"type": "error", "op": "marmot.timeline", "code": exc.code,
                    "error": str(exc)[:300]}
        return {"type": "marmot_timeline", **page}

    def mark_read(self, ids: list[str]) -> dict[str, Any]:
        return self.ack(ids)
