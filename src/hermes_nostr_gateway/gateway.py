"""The gateway: relays in, policy applied, socket out. One process, one identity."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import nostr_sdk as sdk
import yaml

from . import addressing, sdkloop
from .bots import BotRegistry, kind0_fetcher
from .control import ControlConfig
from .crypto import SdkCrypto, normalize_pubkey
from .health import (
    DEFAULT_PENDING_STALE_MINUTES,
    default_planes,
    health_report,
    relay_verdicts,
)
from .media import MediaConfig, MediaError, media_config, resolve_attachment
from .ngit_watch import (
    COMMENT_KINDS,
    COMMENT_ROOT_KINDS,
    WATCH_KINDS,
    WATCH_SINCE_DAYS,
    NgitWatchConfig,
    NgitWatcher,
    SubprocessPass,
    watch_plan,
)
from .peer_inbox import (
    InboxResolver,
    PeerInboxConfig,
    SdkInboxResolver,
    inbox_additions,
    normalise_relay,
    now_seconds,
    peer_inbox_config,
    peer_pubkeys,
)
from .planes import nip04
from .planes.concord import write as concord_write
from .planes.concord.channels import is_conversation as is_concord_conversation
from .planes.concord.channels import stream_key as concord_stream_key
from .planes.concord.plane import ConcordPlane, InviteVerdict, npub_of
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
from .profiles import hermes_config_path, require_profiles
from .projects import ProjectMap
from .publisher import Publisher, make_publisher
from .routing import Resolution, load_router, reported_route
from .server import SocketServer
from .store import Store, drops_ttl_seconds
from .writer import StoreWriter

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from .planes.concord.read import Invite

log = logging.getLogger("hng.gateway")

#: The DM subscription asks for NIP-17 wraps and legacy NIP-04 in one REQ. A relay
#: that refuses one kind closes the whole filter, so this is only the default:
#: `relays.read` can pin a narrower set per relay, and a refusal is negotiated at
#: runtime (PoolWorker.note_refusals) rather than costing the relay entirely.
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
    a general NIP-17 inbox (docs/gateway-spec.md §3 "Relay policy"; docs/decisions.md,
    "White Noise relays are Marmot-only"). A peer listing one is saying where their own
    inbox is, not asking us to subscribe to a group relay.
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


def marmot_identity_error(account: str, me: str) -> str:
    """The refusal when wn-agent's account and this process's key are not one identity.

    Two copies of one account key exist on a headless host: wn-agent's own file and the
    copy this gateway reads (a `secret_file`, or a systemd `LoadCredential=` source). Each
    is individually valid, so a half-finished replace — a restore of one, a re-bootstrap
    of the other — would leave the gateway signing DMs as one npub while the daemon signs
    group traffic as another. Nothing else compares them; this does.
    """
    return (
        f"wn-agent serves account {account[:12]}… but this gateway signs as "
        f"{me[:12]}…: the account key and the copy this process reads have drifted "
        "apart, so a group reply and a DM would come from different identities. Make "
        "wn-agent's accounts/<label>/secret.json and this gateway's identity source "
        "(secret_file or LoadCredential=) hold the same key, restart both, and the "
        "plane attaches again. See docs/decisions.md, \"One key, two copies, and the "
        "check that makes drift loud\". The Marmot plane is disabled until they agree."
    )


def marmot_account_conflict_error(served: str, configured: str) -> str:
    """The refusal when `marmot.account_id_hex` and wn-agent's own file name different accounts.

    The config key is a *copy* of an id, and a copy is exactly what the identity guard
    exists to catch: it cannot be trusted to describe the daemon, and comparing it in place
    of the file is how a re-bootstrapped wn-agent attaches silently. Nor can the
    disagreement be resolved by preferring one side — there is no way to tell which account
    the operator meant — so it is refused with both ids in the message.
    """
    return (
        f"marmot.account_id_hex names account {configured[:12]}… but wn-agent's "
        f"accounts/<label>/account.json serves {served[:12]}…: the configured id would "
        "have this gateway address one identity while the daemon signs group traffic as "
        "another. Drop marmot.account_id_hex (wn-agent's file is the source of truth) or "
        "point it at the account that file names. See docs/decisions.md, \"One key, two "
        "copies, and the check that makes drift loud\". The Marmot plane is disabled until "
        "they agree."
    )


def _marmot_account(home: Path, cfg: dict[str, Any]) -> str:
    """The account wn-agent serves: its own file first, `account_id_hex` only as a fallback.

    Reading the file only when no id was configured inverts the guard: a re-bootstrapped
    wn-agent (new key on disk, this gateway and its config unchanged) would have the config
    value compared with this process's key, agree, and attach while the daemon signs group
    traffic as the new npub. So the file wins whenever the home holds one, and a configured
    id that disagrees with it is a refusal rather than a preference applied quietly.

    A home with no account file is an install in progress, and there the configured id is
    the operator's own answer; every other failure — two accounts without a `label`, an
    unreadable file — stays an error, because a configured id would be a guess dressed as a
    decision in those cases.
    """
    configured = str(cfg.get("account_id_hex") or "").strip().lower()
    try:
        served = resolve_account_id(home, cfg.get("label"))
    except MarmotError as exc:
        if exc.code != "no_account" or not configured:
            raise
        # `no_account` covers both "nothing written yet" and "a file that cannot be read".
        # Only the first is an install in progress; falling back over a corrupt file would
        # put the config value back in the guard's seat, which is the bug this replaces.
        if any((Path(home) / "accounts").glob("*/account.json")):
            raise
        return configured
    if configured and configured != served:
        raise MarmotError(
            marmot_account_conflict_error(served, configured), code="account_conflict")
    return served


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
                 peer_resolver: InboxResolver | None = None,
                 config_path: str | Path | None = None):
        from .cli import normalise_groups, secret_from

        self.cfg = cfg
        # Before anything is opened: a routing block that cannot be honoured would send a turn
        # to a model or an account nobody chose, so it is a reason not to start at all (the
        # same rule as Marmot ownership below).
        self.routing = load_router(cfg.get("routing"), config_path=config_path)
        # Before the secret is read and before any store or socket is opened: a
        # configuration that would deliver every Marmot message twice is not a
        # process to start and then warn about.
        self.hermes_marmot = self._guard_marmot_ownership(cfg.get("marmot") or {})
        #: Which Hermes profile each plane and channel runs as, read here rather than at the
        #: first message: a route naming a profile Hermes does not have would otherwise run
        #: that turn as the default profile, which is the one outcome this may not have.
        #: Refused before the secret, the store and the socket are touched;
        #: `hng profiles --check` reports the same without starting anything.
        self.profiles = require_profiles(cfg)
        self._secret = secret_from(cfg)
        self.crypto = SdkCrypto(self._secret)
        # Publish transport (plan P2): one `nostr-sdk` client, one signed event per
        # message, per-relay results. It is the only write path; reads have no
        # subprocess path either, so there is nothing for a flag to select.
        self.publisher: Publisher = make_publisher(cfg, self._secret)
        self.me = self.crypto.our_pubkey()
        self.me_npub = sdk.PublicKey.parse(self.me).to_bech32()
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
        #: Invite decisions waiting on a relay write: a join accepted before `stop()` must
        #: finish or be reported, not vanish with the process. The lock is created on the
        #: loop that first needs it — see `invite_lock`.
        self._invites: set[asyncio.Task[Any]] = set()
        self._invite_lock: asyncio.Lock | None = None
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
        self.cfg_concord = cfg.get("concord") or {}
        #: Where a confirmation may be asked, and whose word needs none. Config only:
        #: the plane that needs it says so through `self.control`, never a hard-coded key.
        self.control = ControlConfig.from_config(cfg.get("control"))
        #: Channel -> checkout, plus the project and board that drive its work when it
        #: declares them. Validation is `hng projects --check`; here a gap is only logged,
        #: because a broken mapping must never stop the gateway reading messages.
        self.projects = ProjectMap.from_config(self.cfg_concord)
        for mapped, gap in self.projects.gaps():
            log.warning("concord project %r: %s %s", mapped.channel, gap.name, gap.detail)
        self.concord = ConcordPlane(
            self.store,
            solo_max_members=int(self.cfg_concord.get("solo_max_members", 2)),
            projects=self.projects,
            control=self.control)
        #: The channel addresses the concord read workers are subscribed to, and their tasks.
        self._concord_tasks: list[asyncio.Task[None]] = []
        self._concord_authors: tuple[str, ...] = ()
        self.plane = Nip17Plane(self.crypto, self.store, self.policy,
                                on_invite=self._on_invite)
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
        #: Where this process was configured, so a child it starts reads the same file
        #: rather than whatever `hng` would find on its own. Set by `hng serve`.
        self.config_path = Path(config_path) if config_path else None
        #: The ngit watcher: a relay subscription that wakes `hng ngit sync` when a mapped
        #: repository's issue/PR/status event arrives. The pass stays the decision (see
        #: `ngit_watch.py`); this only shortens the wait between cron passes. Decided here
        #: from configuration, started in `start()` once the concord streams (and so the
        #: conversation each mapped channel has) are known.
        self.ngit_watch_cfg = NgitWatchConfig.from_config(cfg)
        self.ngit_watch: NgitWatcher | None = None
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
        # set, NIP-42 answered in-process. It is the only read path: the per-relay
        # `nak req --stream` children are gone, so there is no second implementation to
        # keep in step and nothing for a flag to select.
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
                    a_tags: tuple[str, ...] = (), root_kinds: tuple[str, ...] = (),
                    purpose: str = "dm", since_days: int = 3) -> PoolWorker:
        """Build a read worker: one relay's subscription set on the shared client.

        `PoolWorker` is the only read transport now. Its counters and `live_state()` are
        what `status`, `health` and `_guarded` consume, so the rest of the gateway does
        not branch on how the events arrive.
        """
        return PoolWorker(self.pool, relay, self.me, kinds=kinds, p_tag=p_tag,
                          authors=authors, a_tags=a_tags, root_kinds=root_kinds,
                          purpose=purpose, since_days=since_days,
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
        used (no account file, two of them, or an account that is not the key this
        process signs with) disables only this plane and records why
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
            # wn-agent's own file, not the config value: `account_id_hex` is a fallback for
            # a home that holds no account file, and a value that disagrees with one is
            # itself a refusal (see `_marmot_account`). Comparing the config value instead
            # is how a re-bootstrapped daemon attaches while signing as a different npub.
            account = _marmot_account(home, cfg)
            if account.lower() != self.me.lower():
                self.marmot_error = marmot_identity_error(account, self.me)
                log.error("marmot plane disabled: %s", self.marmot_error)
                return
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
    async def refresh_concord(self) -> int:
        """Re-read each community's channels and members, and republish the watched set.

        Concord is read under keys, not under an address: the channel list and the roster live
        on the Control plane and Guestbook, both of which only need the `community_root` in the
        invite. So the plane learns its streams here, and the workers subscribe by author
        (the stream addresses) rather than by `#p` — a channel wrap is not addressed to us.
        """
        from .planes.concord import read as concord_read

        by_community: dict[str, list[dict[str, Any]]] = {}
        for row in self.store.concord_invites(50):
            by_community.setdefault(row["community_id"], []).append(row)

        watched = 0
        for community_id, rows in by_community.items():
            bundle = dict(rows[0]["bundle"])
            # One owner action mints one invite per room, so union the grants across them.
            grants: dict[str, dict[str, Any]] = {}
            for row in rows:
                for channel in row["bundle"].get("channels") or []:
                    if channel.get("id"):
                        grants.setdefault(channel["id"], channel)
            if grants:
                bundle["channels"] = list(grants.values())
            invite = concord_read.Invite(bundle=bundle, inviter=rows[0]["inviter"],
                                         relay="", wrap_id=rows[0]["id"])
            try:
                community = await concord_read.load_community(invite)
                members = await concord_read.read_guestbook(invite)
            except Exception as exc:  # noqa: BLE001 - one dead community is not the daemon
                log.warning("concord: cannot refresh %s…: %s", community_id[:12], exc)
                continue
            fresh = self.concord.set_streams(invite, community.channels,
                                             {m.pubkey for m in members})
            watched += len(fresh)
        await self._sync_concord_workers()
        log.info("concord: %d channel stream(s) watched across %d community(ies)",
                 watched, len(by_community))
        return watched

    async def _sync_concord_workers(self) -> None:
        """(Re)start a read worker per relay for the channel addresses the plane knows."""
        addresses = tuple(sorted({stream.address for stream in self.concord.channels()}))
        if addresses == self._concord_authors:
            return
        for task in self._concord_tasks:
            task.cancel()
        self._concord_tasks = []
        self._concord_authors = addresses
        if not addresses:
            return
        for relay in self.relays_read:
            worker = self._new_worker(relay, kinds=(1059,), authors=addresses,
                                      purpose="concord")
            self.workers.append(worker)
            self._concord_tasks.append(asyncio.create_task(self._guarded(worker)))
        log.info("concord: subscribed %d relay(s) for %d channel stream(s)",
                 len(self.relays_read), len(addresses))

    async def _concord_refresh_loop(self) -> None:
        minutes = max(1, int((self.cfg_concord.get("refresh_minutes") or 10)))
        while True:
            await asyncio.sleep(minutes * 60)
            try:
                await self.refresh_concord()
            except Exception:  # noqa: BLE001 - a refresh must never take the daemon down
                log.exception("concord refresh failed")

    # -- the ngit watcher: wake the pass, never decide for it -----------------

    async def _start_ngit_watch(self) -> None:
        """Build the watch plan off the loop, then subscribe and coalesce.

        `ngit repo --json` is a child process per mapped checkout, so the plan is built in
        a worker thread: a slow or missing ngit must not delay the daemon's first message.
        Nothing here decides anything -- the watcher starts `hng ngit sync`, and that pass
        owns the store, the board and the cursor.
        """
        if not self.ngit_watch_cfg.enabled:
            if self.ngit_watch_cfg.explicit:
                log.warning("ngit watch idle: %s", self.ngit_watch_cfg.reason)
            else:
                log.info("ngit watch idle: %s", self.ngit_watch_cfg.reason)
            return
        try:
            plan = await asyncio.wait_for(asyncio.to_thread(
                watch_plan, self.cfg, self.projects, self.concord.channels(),
                read_relays=tuple(self.relays_read),
                known_roots=self.store.ngit_roots), timeout=120)
        except Exception as exc:  # noqa: BLE001 - the scheduled pass is still the floor
            log.warning("ngit watch: no watch plan (%s); the scheduled pass keeps the bridge "
                        "moving", exc)
            return
        for skip in plan.skips:
            log.info("ngit watch: not watching %s", skip)
        if not plan.enabled:
            log.info("ngit watch idle: %s", plan.reason or "nothing to watch")
            return
        watcher = NgitWatcher(plan, runner=SubprocessPass(
            config_path=self.config_path, socket_path=self.socket_path))
        self.ngit_watch = watcher
        log.info("ngit watch: %d channel(s), debounce %.0fs, relays %s",
                 len(plan.targets), plan.debounce_s, ", ".join(plan.relays))
        try:
            await watcher.run(self._attach_ngit_watch)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the scheduled pass is still the floor
            log.exception("ngit watch: the watcher stopped; the scheduled pass keeps the "
                          "bridge moving")

    def _attach_ngit_watch(self, addresses: tuple[str, ...],
                           handler: Any) -> list[PoolWorker]:
        """Narrowed read workers per relay: the repositories, and their comments.

        The watcher is an accelerator, so its workers are ordinary pool workers: same
        NIP-42, same reconnects, same counters in `status`/`health` -- only the filter and
        the handler differ. The handler is the watcher's own, so a repository event never
        falls through to the message planes.

        Comments are a second subscription, not another kind in the first: ngit publishes
        them as NIP-22 kind 1111, which carries no `a` tag, so a `#a` filter can never
        match one. `#K` (the root's kind) is what narrows this one to comments on issues
        and PRs; which repository that is, the watcher decides by the root event id.
        """
        watcher = self.ngit_watch
        relays = watcher.plan.relays if watcher is not None else ()
        workers: list[PoolWorker] = []
        for relay in relays:
            worker = self._new_worker(relay, kinds=WATCH_KINDS, a_tags=addresses,
                                      purpose="ngit", since_days=WATCH_SINCE_DAYS)
            self.workers.append(worker)
            self._tasks.append(asyncio.create_task(self._guarded(worker, handler)))
            workers.append(worker)
            comments = self._new_worker(relay, kinds=COMMENT_KINDS,
                                        root_kinds=COMMENT_ROOT_KINDS,
                                        purpose="ngit-comments",
                                        since_days=WATCH_SINCE_DAYS)
            self.workers.append(comments)
            self._tasks.append(asyncio.create_task(self._guarded(comments, handler)))
            workers.append(comments)
        return workers

    # -- invites: join on a trusted word, hold for a human ----------------

    def _on_invite(self, relay: str, rumor: dict[str, Any],
                   wrapper_id: str = "") -> Invite | None:
        """The NIP-17 invite sink: verify and record here, decide on a task of our own.

        `handle_invite` is synchronous because it runs inside a relay worker's read
        callback, and a join is a relay write: it waits for a task so nothing blocks the
        worker, and so a slow relay cannot make one invite delay the next message.
        """
        invite = self.concord.handle_invite(relay, rumor, wrapper_id)
        if invite is None:
            return None
        try:
            task = asyncio.create_task(self._decide_invite_quietly(invite))
        except RuntimeError:
            # Recorded with no loop (a CLI, a test): the invite exists and nothing joins.
            log.warning("concord invite %s...: recorded with no loop to decide it",
                        invite.wrap_id[:12])
            return invite
        self._invites.add(task)
        task.add_done_callback(self._invites.discard)
        return invite

    async def _decide_invite_quietly(self, invite: Invite) -> None:
        """A background decision must not die as an exception nobody collected."""
        try:
            await self.decide_invite(invite)
        except Exception:  # noqa: BLE001 - one invite must never take the daemon down
            log.exception("concord invite %s...: the decision failed", invite.wrap_id[:12])

    @property
    def invite_lock(self) -> asyncio.Lock:
        """One invite decision at a time: a re-invite must not publish a second join.

        A single owner action mints one invite per room, so a burst of invites for one
        community arrives together; the check for "already joined" and the publish have to
        be one step or every one of them publishes a join. The publish is inside the lock,
        so a slow relay delays the next invite — never a relay worker or a message. Created
        lazily because the lock binds to the loop that uses it, and the gateway is built off
        the loop.
        """
        if self._invite_lock is None:
            self._invite_lock = asyncio.Lock()
        return self._invite_lock

    async def decide_invite(self, invite: Invite) -> dict[str, Any]:
        """Accept this invite, or hold it and ask: the one place an invite becomes a join.

        Returns the same frame the CLI prints, so a caller sees the words the daemon acted
        on: `status` is `joined`, `held` or `failed`, and `why` says what decided it.
        """
        community = invite.community_id.hex()
        async with self.invite_lock:
            if self.store.concord_joined(community):
                self._record_decision(invite, status="joined",
                                      why="already a member of this community",
                                      decided_by="auto")
                log.info("concord invite %s...: already a member of %s...",
                         invite.wrap_id[:12], community[:12])
                return {**self._invite_base(invite), "status": "joined", "ok": True,
                        "why": "already a member of this community"}
            row = self.store.concord_invite(invite.wrap_id) or {}
            if row.get("announced_at"):
                # The same wrap reaches us from every relay that carries it: the question
                # is already in the pane, and asking it twice would train people to ignore
                # it. The row keeps the reason it was held.
                log.info("concord invite %s...: already asked in the control pane",
                         invite.wrap_id[:12])
                return {**self._invite_base(invite), "status": "held", "announced": True,
                        "why": str(row.get("why") or "")}
            verdict = self.concord.invite_verdict(invite)
            if verdict.accept:
                return await self._join_invite(invite, verdict)
            return await self._hold_invite(invite, verdict)

    async def _join_invite(self, invite: Invite, verdict: InviteVerdict) -> dict[str, Any]:
        """Publish the Guestbook join, and write down every way it can end.

        The row reads `joining` before the publish: a process that dies mid-write leaves
        "we tried, outcome unknown" rather than an accepted invite that still looks held.
        """
        self._record_decision(invite, status="joining", why=verdict.why, decided_by="auto")
        try:
            results = await concord_write.join_guestbook(self.crypto.keys, invite)
        except Exception as exc:  # noqa: BLE001 - a failed join is a state, not a crash
            log.exception("concord invite %s...: join failed", invite.wrap_id[:12])
            self._record_decision(invite, status="failed",
                                  why=f"join raised {type(exc).__name__}: {exc}"[:300],
                                  decided_by="auto")
            return {**self._invite_base(invite), "status": "failed", "ok": False,
                    "why": verdict.why, "error": str(exc)[:300]}
        ok = any(outcome == "ok" for outcome in results.values())
        refused = {relay: outcome for relay, outcome in results.items() if outcome != "ok"}
        why = verdict.why if ok else "no relay accepted the join: " + ", ".join(
            f"{relay} ({outcome})" for relay, outcome in refused.items())[:240]
        self._record_decision(invite, status="joined" if ok else "failed", why=why,
                              decided_by="auto")
        log.info("concord invite %s...: %r %s (%d/%d relay(s) ok)", invite.wrap_id[:12],
                 str(invite.bundle.get("name") or ""), "joined" if ok else "NOT joined",
                 sum(1 for outcome in results.values() if outcome == "ok"), len(results))
        return {**self._invite_base(invite), "status": "joined" if ok else "failed",
                "ok": ok, "why": why, "relays": results, "errors": refused}

    async def _hold_invite(self, invite: Invite, verdict: InviteVerdict) -> dict[str, Any]:
        """Keep the invite, and ask a human in the control pane — the only other answer."""
        self._record_decision(invite, status="held", why=verdict.why)
        log.info("concord invite %s...: held (%s)", invite.wrap_id[:12], verdict.why)
        destination = self.control.target("concord")
        if not destination:
            # A confirmation that cannot be delivered must not read as one that was.
            log.warning("concord invite %s... is held and there is nowhere to ask: set "
                        "control.concord to a channel, or accept it with `hng concord %s`",
                        invite.wrap_id[:12], invite.wrap_id)
            return {**self._invite_base(invite), "status": "held", "announced": False,
                    "why": verdict.why, "error": "no control destination"}
        frame = await self._send_concord(destination, self._invite_prompt(invite, verdict),
                                         None, None)
        announced = bool(frame.get("ok"))
        if announced:
            self.store.note_invite_announced(invite.wrap_id)
        else:
            log.warning("concord invite %s... is held but the control pane did not take "
                        "the question: %s", invite.wrap_id[:12],
                        frame.get("error") or frame.get("errors") or "unknown error")
        return {**self._invite_base(invite), "status": "held", "announced": announced,
                "why": verdict.why, "asked": destination,
                "error": "" if announced else str(frame.get("error") or "not announced")}

    def _invite_prompt(self, invite: Invite, verdict: InviteVerdict) -> str:
        """The question a human answers in the control pane.

        Names the inviter, the community, its channels and the exact command that accepts
        it: a prompt nobody can act on is the same as no prompt at all.
        """
        bundle = invite.bundle
        names = [f"#{channel.get('name') or channel.get('id') or '?'}"
                 for channel in bundle.get("channels") or []]
        shown = ", ".join(names[:8])
        if len(names) > 8:
            shown += f", +{len(names) - 8} more"
        return "\n".join([
            "Concord invite held: nothing joins until someone says yes.",
            f"community: {bundle.get('name') or '(unnamed)'}"
            f" ({invite.community_id.hex()})",
            f"inviter:   {npub_of(invite.inviter)} ({invite.inviter})",
            f"channels:  {len(names)} ({shown or 'none granted'})",
            f"relays:    {', '.join(invite.relays)}",
            f"why held:  {verdict.why}",
            f"accept:    hng concord {invite.wrap_id}",
        ])

    def _invite_base(self, invite: Invite) -> dict[str, Any]:
        return {"type": "concord_invite", "id": invite.wrap_id,
                "inviter": invite.inviter,
                "community": invite.community_id.hex(),
                "name": str(invite.bundle.get("name") or "")}

    def _record_decision(self, invite: Invite, *, status: str, why: str,
                         decided_by: str = "") -> None:
        if not self.store.decide_invite(invite.wrap_id, status=status, why=why,
                                        decided_by=decided_by):
            log.warning("concord invite %s...: no stored row for the %s decision",
                        invite.wrap_id[:12], status)

    async def start(self) -> None:
        # One loop for the whole process, declared before the first await. uniffi
        # resolves every call — including the NIP-42 signer it invokes from a Rust
        # worker thread — through one process-global loop, so a one-shot that runs while
        # this daemon has no declared home manufactures a second loop and closes it, and
        # the next auth attempt builds its future on the dead one (see `sdkloop`).
        # `_ensure_pump` re-asserts the same loop for a pool used on its own; declaring it
        # here first removes the window between the socket opening and the first
        # subscription, in which a request can reach a cold fetch (`send` does exactly
        # that) and that fetch would otherwise get a loop of its own.
        sdkloop.set_home()
        await self.server.start()
        self._started_at = time.time()
        # One line, because a missing destination is a configuration fact the operator
        # should see at boot rather than discover when a confirmation goes nowhere.
        for problem in self.control.problems:
            log.warning("control: %s", problem)
        log.info("control pane: %s", "; ".join(self.control.describe()))
        # One line, for the same reason: which identity a plane or channel runs as is
        # configuration, and a route that is only in a file is one nobody can check at boot.
        if self.profiles:
            for warning in self.profiles.warnings:
                log.warning("profiles: %s", warning)
            log.info("profiles: %s", "; ".join(self.profiles.describe()))
        # Before any relay worker exists: from here on, a store write is a queue append
        # and the commit happens on the writer's thread.
        await self.store_writer.start()
        self.store.attach_writer(self.store_writer)
        # Concord: learn the channel streams before the relay workers are built, so the very
        # first subscription already carries their addresses.
        try:
            await asyncio.wait_for(self.refresh_concord(), timeout=30)
        except Exception:  # noqa: BLE001 - a slack relay must not delay the daemon
            log.warning("concord: initial refresh did not finish in 30s", exc_info=True)
        self._tasks.append(asyncio.create_task(self._concord_refresh_loop()))
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
        # The ngit watcher, if configured: a narrowed subscription that wakes the cron pass.
        # Started after the concord refresh, because a wake names a channel and the channel
        # has to be one this daemon has joined for the notice to land.
        self._tasks.append(asyncio.create_task(self._start_ngit_watch()))
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
        self.peer_resolver = SdkInboxResolver(self.peer_cfg.lookup, secret=self._secret)
        return self.peer_resolver

    def _add_peer_relay(self, relay: str) -> PoolWorker:
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

    async def _guarded(self, worker: PoolWorker, handler: Any = None) -> None:
        """Run one worker's subscription, with the gateway's own handler unless given one.

        The ngit watcher brings its own handler: a repository event is not a message, and
        must not be routed through the planes that turn one into an agent run.
        """
        try:
            await worker.run(handler or self._on_event)
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
        # A worker that is mid-reconnect may take a moment to unwind; it must not hold the
        # process past systemd's stop timeout, or the unit dies by SIGKILL and in-flight
        # publishes are lost with it.
        try:
            await asyncio.wait_for(
                asyncio.gather(*self._tasks, return_exceptions=True), 6)
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("workers did not unwind in 6s; stopping anyway")
        # A frame already accepted must reach the adapter: it is only waiting on its
        # flush, so let it finish while the server and the writer are both still up. An
        # invite decision is the same: its join is a publish, and dropping it would leave
        # an accepted invite recorded as held.
        pending = [*self._deliveries, *self._invites]
        if pending:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*pending, return_exceptions=True), 5)
            except (asyncio.TimeoutError, TimeoutError):
                log.warning("delivery and invite tasks did not finish in 5s; stopping anyway")
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
    def route_for(self, plane: str, conversation: str = "") -> Resolution:
        """The route a conversation's turn is asked to use — never a credential.

        The Concord channel list is consulted only once the plane has learned it: before the
        first refresh the list is empty because nothing has been asked yet, not because every
        channel is gone, and a route that may be fine must not be refused on a not-yet.
        """
        if not self.routing:
            return Resolution(rule="none")
        known = self.concord.channels() if self.concord.refreshed else None
        return self.routing.resolve(plane, conversation, known_channels=known)

    def frame_for(self, msg: Any) -> dict[str, Any]:
        """A message frame, a `reaction` frame, or a `blocked` frame when policy refused it.

        A blocked message is still stored (see §13.3) but must never look like chat:
        the adapter turns `type: message` into an agent run, and a `drop` reach used
        to ride that path silently. `blocked` carries the reason and no content.

        A channel's kind-7 reaction arrives here as a `ChannelReaction`, which brings its
        own `type: reaction` frame and has no row behind it: the plane never stores one, so
        there is nothing to look up and nothing to replay — the adapter either matches it to
        a gate it is holding or drops it.
        """
        if msg.reach == "drop" or msg.blocked_reason:
            return {
                "type": "blocked",
                "plane": msg.plane,
                "from": msg.sender,
                "reason": msg.blocked_reason or "not_allowlisted",
                "rumor_id": msg.id,
            }
        return self._routed(msg.to_frame(), plane=getattr(msg, "plane", ""),
                            conversation=msg.conversation)

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
            # A channel wrap's author is the channel's stream address, not a peer key. The
            # Concord plane knows those addresses; anything else on 1059 is a DM.
            if self.concord.stream_for(event.get("pubkey", "")) is not None:
                msg = self.concord.handle_channel(relay, event, self.policy,
                                                  me_hex=self.me, me_npub=self.me_npub)
            else:
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
        # `frame_for` already carries the route and the profile, or the refusal: routing a
        # second time here would resolve the same rule twice and lose the first answer.
        task = asyncio.create_task(self._deliver(self.frame_for(msg)))
        self._deliveries.add(task)
        task.add_done_callback(self._deliveries.discard)

    def _routed(self, frame: dict[str, Any], *,
                plane: str = "", conversation: str = "", sender: str = "",
                message_id: str = "") -> dict[str, Any]:
        """The frame carrying the route and the profile its turn is asked for, or a refusal.

        Two decisions, one frame: which provider, model and credential the turn asks for, and
        which Hermes profile it runs as. Neither can be defaulted quietly — a turn on a model
        nobody chose, or under an identity nobody chose, is the silent substitution both blocks
        exist to prevent. Either one failing makes a `blocked` frame, which is stored and
        visible but never becomes a run, so a replay after the fix still delivers it.
        """
        if frame.get("type") != "message":
            return frame
        origin = sender or frame.get("sender") or frame.get("from") or ""
        rumor_id = message_id or frame.get("id")
        routed = dict(frame)
        if self.routing:
            resolution = self.route_for(plane, conversation)
            if resolution.error:
                log.error("no route for %s %s: %s (%s)", plane, conversation or "(none)",
                          resolution.error, resolution.detail)
                return {"type": "blocked", "plane": plane, "from": origin,
                        "reason": resolution.error, "why": resolution.detail,
                        "rumor_id": rumor_id}
            routed["route"] = resolution.to_frame()
        if self.profiles:
            known = self.concord.channels() if plane == "concord" else None
            profile = self.profiles.resolve(plane, conversation, known_channels=known,
                                            verify=True)
            if profile.error:
                log.error("profile route refused for %s/%s: %s (%s)", plane, conversation,
                          profile.error, profile.detail)
                return {"type": "blocked", "plane": plane, "from": origin,
                        "reason": profile.error, "detail": profile.detail,
                        "rumor_id": rumor_id}
            routed.update(profile.frame())
        return routed

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
        # The legacy plane gets the same two prefixes: a sender should not have to know which
        # wrapper their app picked to say "next, not now" (addressing.py).
        _, words = addressing.split(text)
        decision = self.policy.decide(sender=sender, kind=4, dm=True,
                                      event_id=str(event.get("id") or ""), text=words)
        if decision.duplicate:
            return None
        mode, content = addressing.resolve(text, decision)
        msg = Inbound(
            id=str(event.get("id") or ""), wrapper_id=str(event.get("id") or ""),
            sender=sender, kind=4, created_at=int(event.get("created_at") or 0),
            content=content, conversation=sender, reply_to=None, plane="nip04",
            reach=decision.reach, mode=mode, authority=decision.authority,
            rule=decision.rule, why=decision.why, interval_s=decision.interval_s,
            blocked_reason=decision.blocked_reason,
        )
        return msg if self.store.add(msg) else None

    # -- outbound -------------------------------------------------------

    async def send(self, *, to: str, text: str, reply_to: str | None = None,
                   cid: str | None = None, route: Any = None) -> dict[str, Any]:
        """Send one message, once per correlation id.

        `cid` is the caller's idempotency key as well as the id echoed back: a caller that
        has to survive a lost acknowledgement (the ngit bridge does -- its poll can be cut
        off by a turn before the reply arrives) sends the same id again, and gets the
        message it already published instead of a second copy. The Marmot plane has always
        treated `cid` this way; the store now remembers the answer for every plane, so the
        retry is decided here rather than by whichever transport happens to be underneath.
        """
        prior = self.store.send_key(str(cid or ""))
        if prior is not None:
            log.info("send: %s already posted as %s; not publishing again",
                     str(cid)[:60], str(prior.get("message_id"))[:16])
            return {"type": "delivery", "id": cid, "ok": True, "deduped": True,
                    "message_id": prior["message_id"],
                    "conversation": prior.get("conversation") or str(to or "")}
        frame = await self._dispatch_send(to=to, text=text, reply_to=reply_to, cid=cid,
                                          route=route)
        if cid and frame.get("ok") and frame.get("message_id"):
            self.store.note_send_key(str(cid), str(frame["message_id"]),
                                     str(frame.get("conversation") or to or ""))
        return frame

    async def _dispatch_send(self, *, to: str, text: str, reply_to: str | None = None,
                             cid: str | None = None, route: Any = None) -> dict[str, Any]:
        target = str(to or "").strip()
        # What the adapter says the turn ran on, trimmed to names. It goes on the reply's row
        # and back in the reply, so the audit trail is the route that was used rather than the
        # one the daemon hoped for. Never a credential: see routing.reported_route.
        reported = reported_route(route)
        if is_concord_conversation(target):
            # A message read in a channel is answered in that channel: the conversation it
            # was stored under already names the room, so a reply must never become a DM to
            # whoever happened to speak there.
            return await self._send_concord(target, text, reply_to, cid, reported)
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

        # A recipient's inbox relays are a network lookup (`fetch_first_blocking`), so it
        # must not run on the loop: doing so blocks every socket request and every relay
        # worker for the duration, and with sdkloop handing one-shots to this loop it
        # deadlocks outright (the lookup waits for the loop it is blocking).
        relays = await asyncio.to_thread(self._recipient_relays, peer)
        for event in wraps.values():
            self.store.record_wrap(event, rumor["id"], peer, relays)

        results = await self._publish_all(wraps, relays)
        for label, per_relay in results.items():
            for relay, (ok, error) in per_relay.items():
                self.store.mark_wrap(wraps[label]["id"], relay, ok=ok, error=error)

        peer_ok = any(ok for ok, _ in results["peer"].values())
        if peer_ok:
            # Record what we sent, so history is not one-sided and an answer can be
            # traced back to the message that prompted it. `route` is what the adapter says
            # the turn ran on: the reply carries the audit trail, not the queue.
            from .store import Inbound as _Inbound
            self.store.add(_Inbound(
                id=rumor["id"], wrapper_id=wraps["peer"]["id"], sender=self.me,
                kind=int(rumor.get("kind", 14)), created_at=int(rumor.get("created_at", 0)),
                content=text, conversation=peer, reply_to=reply_to, plane="nip17",
                reach="sent", authority="self", rule="outbound", why="sent by us",
                route=reported or None,
            ))
            # The row is queued, so `mark_delivered` has nothing to update until the
            # writer commits it: flush first, or our own message comes back as pending.
            await self.store.flush()
            self.store.mark_delivered([rumor["id"]])
        delivery = {
            "type": "delivery",
            "id": cid,
            "ok": peer_ok,
            "message_id": rumor["id"],
            "relays": {label: {r: ok for r, (ok, _) in v.items()} for label, v in results.items()},
            "errors": {r: e for label, v in results.items() for r, (ok, e) in v.items() if not ok},
        }
        if reported:
            delivery["route"] = reported
        return delivery

    async def _send_concord(self, conversation: str, text: str, reply_to: str | None,
                            cid: str | None, route: dict[str, str] | None = None) -> dict[str, Any]:
        """Publish a message back into the Concord channel a conversation came from.

        The plane is the index, not the sender: it says which invite and channel the
        conversation names, and it holds no key. The wrap is built here, with the identity
        this process already signs DMs with, so a channel message has one implementation and
        one signer.
        """
        found = self.concord.channel_for_conversation(conversation)
        if found is None:
            return {"type": "delivery", "id": cid, "ok": False,
                    "conversation": conversation,
                    "error": f"no readable Concord channel for {conversation!r}"}
        invite, stream = found
        try:
            # The same grant the read path opens this channel with, so what we publish is
            # what `handle_channel` (and every other member's client) can read back.
            built = await asyncio.to_thread(
                concord_write.build_channel_message, self.crypto.keys, invite,
                stream.channel_id, text, channel_key=stream.key, epoch=stream.epoch,
                quote=self._quote_for(reply_to))
            concord_write.verify_envelope(
                built, concord_stream_key(invite, stream, stream.key))
        except Exception as exc:  # noqa: BLE001 - report, do not kill the socket
            return {"type": "delivery", "id": cid, "ok": False,
                    "conversation": stream.chat_id,
                    "error": f"cannot build a channel message: {exc}"[:300]}

        results = await concord_write.publish(built["wrap"], invite.relays)
        ok = any(outcome == "ok" for outcome in results.values())
        rumor, wrap = built["rumor"], built["wrap"]
        if ok:
            from .store import Inbound as _Inbound
            self.store.add(_Inbound(
                id=str(rumor["id"]), wrapper_id=str(wrap["id"]), sender=self.me,
                kind=int(rumor.get("kind", 9)), created_at=int(rumor.get("created_at", 0)),
                content=text, conversation=stream.chat_id, reply_to=reply_to,
                plane="concord", reach="sent", authority="self", rule="outbound",
                why="sent by us", route=route or None,
            ))
            # Queued through the writer, so flush before marking it delivered or our own
            # message sits in `pending` as if it never went out. Bounded: the message is
            # already published, and a writer that is slow must not turn a successful send
            # into a socket request that never answers.
            try:
                await asyncio.wait_for(self.store.flush(), timeout=10.0)
            except asyncio.TimeoutError:
                log.warning("concord: store flush timed out after a channel send; "
                            "the message is published but its row may land late")
            self.store.mark_delivered([str(rumor["id"])])
        delivery = {
            "type": "delivery", "id": cid, "ok": ok, "conversation": stream.chat_id,
            "message_id": str(rumor["id"]),
            "relays": {"channel": {relay: outcome == "ok" for relay, outcome in results.items()}},
            "errors": {relay: outcome for relay, outcome in results.items()
                       if outcome != "ok"},
        }
        if route:
            delivery["route"] = route
        return delivery

    def _quote_for(self, reply_to: str | None) -> tuple[str, str] | None:
        """`(wrap id, author)` for the message a reply answers, or None when it answers none.

        Concord names a channel message by the wrap it arrived in — the wrap is what the
        room's readers see — while the frame hands the agent the inner rumor's id. The
        stored row is what turns one into the other; an id that is already a wrap id, or one
        this store never saw, goes out as given.
        """
        if not reply_to:
            return None
        row = self.store.rumor(str(reply_to)) or {}
        return (str(row.get("wrapper_id") or reply_to), str(row.get("sender") or ""))

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
            # Which publish transport is running, and how it has fared: `mode: bindings`
            # is the in-process client, so a status read cannot be mistaken for a child.
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
            "invites_held": stats["invites_held"],
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
            # Which read transport is running, and what it is doing: one client, one
            # subscription per filter set, so a relay that never received an event shows
            # as a subscription that has been quiet rather than as a healthy one.
            "transport": self.pool.stats(),
            # The ngit accelerator, not a second source of truth: what it listens to, and
            # how many passes its wakes started. `enabled: false` carries the reason, so a
            # bridge that is only on cron says so instead of looking broken.
            "ngit_watch": self._ngit_watch_status(),
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

    def _ngit_watch_status(self) -> dict[str, Any]:
        """What the watcher is doing, or why it is not running.

        Three states, all honest: the watcher is up (`enabled: true`); it is configured on
        but the plan found nothing to watch (the reason says what); or it is off by
        configuration (`ngit.watch: false`). Cron covers the bridge in the last two.
        """
        if self.ngit_watch is not None:
            return self.ngit_watch.status()
        return {
            "enabled": False,
            "debounce_s": self.ngit_watch_cfg.debounce_s,
            "reason": self.ngit_watch_cfg.reason or "not started",
            "channels": [], "relays": [], "targets": [],
            "events": 0, "passes": 0, "alive": False,
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
        # The watcher is an accelerator, so its absence is a note, not a degraded plane --
        # but an operator who asked for it should not have to read a journal to find out.
        if self.ngit_watch is not None and not self.ngit_watch.status().get("alive"):
            notes.append("ngit watch: no relay subscription is up yet; the scheduled pass "
                         "is the only trigger")
        elif (self.ngit_watch is None and self.ngit_watch_cfg.explicit
              and not self.ngit_watch_cfg.enabled
              and self.ngit_watch_cfg.reason != "ngit.watch: false"):
            notes.append(f"ngit watch: asked for but not running ({self.ngit_watch_cfg.reason})")
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
        (a test double, or a plane that is not a `PoolWorker`).
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

    def authorize(self, *, pubkey: str, conversation: str,
                  kind: int | None = None) -> dict[str, Any]:
        """Would a fresh message from `pubkey` in `conversation` still be an instruction?

        Hermes asks this when it recovers a durable turn: the frame it stored carried this
        daemon's verdict at arrival, and the adapter's role flag vouched for that verdict.
        A policy edit since then (a `trusted_npubs` entry removed, a rule narrowed) — or a
        channel that is no longer watched — has to revoke it, so the answer is computed from
        live state only and never read off the stored row.

        Addressed-ness is not re-derived: only messages that arrived addressed are asked
        about, so the channel half answers with `mention=True` and the rules decide the
        rest. A DM asks the rules the way a DM always does. `kind` is the shape of the
        message, not its verdict; a caller that cannot name one (a turn recovered after the
        adapter restarted) gets the plane's default.
        """
        sender = normalize_pubkey(str(pubkey or "")) if pubkey else ""
        if not sender:
            return {"type": "authorized", "authorized": False, "authority": "none",
                    "why": "no pubkey"}
        target = str(conversation or "").strip().lower()
        if is_concord_conversation(target):
            found = self.concord.channel_for_conversation(target)
            if found is None:
                # A channel we no longer watch is not a place anyone may instruct: an
                # unsubscribed or dissolved community must not keep a stale grant alive.
                return {"type": "authorized", "authorized": False, "authority": "none",
                        "why": f"{target[:32]}... is not a channel this daemon watches"}
            _, stream = found
            authority = self.policy.authority_for(sender=sender, kind=kind or 9,
                                                  mention=True, dm=False)
            return {"type": "authorized", "authorized": authority == "instructions",
                    "authority": authority, "why": f"addressed in {stream.label}"}
        authority = self.policy.authority_for(sender=sender, kind=kind or 14, dm=True)
        return {"type": "authorized", "authority": authority,
                "authorized": authority == "instructions", "why": "dm"}

    def replay_pending(self, limit: int | None = None) -> list[dict[str, Any]]:
        """Re-offer unacknowledged messages, tagged so a consumer can recognise them.

        `replay: true` is the adapter's cue: a frame it has already handed to the agent
        (the ack failed, so the row is still pending) must be acknowledged again rather
        than answered twice. The window and floor live in the store.

        A replay is the same frame the first delivery would have been, profile included: a
        crash between the store write and the ack must not turn a routed message into a turn
        under the default profile.
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
                # The mode is on the row, so a message held behind a turn survives a restart
                # as a held message and the adapter never has to re-read a prefix. A row
                # written before `/steer` was the name says `guide`; it replays as `steer`.
                "mode": addressing.canonical(row["mode"]),
                "replay": True,
            }
            # A replay has to carry the same attachments as the original frame, or the
            # adapter sees a photo it can no longer ask for.
            if row.get("attachments"):
                frame["attachments"] = row["attachments"]
            # And the same route: a replayed turn must run on the provider it was asked for,
            # not on whatever the agent happens to be configured with. Routing is decided
            # again here rather than stored, so a key exported since the first attempt is
            # what the retry uses -- and a route that cannot be honoured now is a `blocked`
            # frame, visible and unacknowledged, not a turn on somebody else's model.
            frames.append(self._routed(frame, plane=row["plane"],
                                       conversation=row["conversation"],
                                       sender=row["sender"], message_id=row["id"]))
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
