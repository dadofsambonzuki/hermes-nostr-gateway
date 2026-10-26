"""The gateway: relays in, policy applied, socket out. One process, one identity."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, cast

from .bots import BotRegistry, nak_fetcher
from .crypto import SdkCrypto
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
from .publisher import publish
from .relay import RelayWorker
from .server import SocketServer
from .store import Store

log = logging.getLogger("hng.gateway")

#: The DM subscription asks for NIP-17 wraps and legacy NIP-04 in one REQ. A relay
#: that refuses one kind closes the whole filter, so this is only the default:
#: `relays.read` can pin a narrower set per relay, and a refusal is negotiated at
#: runtime (RelayWorker.note_refusals) rather than costing the relay entirely.
DM_KINDS: tuple[int, ...] = (1059, 4)


def restricted_relay(relay: str) -> bool:
    """Relays that carry only the private path, never public feeds."""
    return relay.rstrip("/").endswith("/chat") or "whitenoise" in relay


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
        "so both attached deliver every group message to the agent twice"
    )

    def __init__(self, cfg: dict[str, Any], *, socket_path: str | Path | None = None,
                 peer_resolver: InboxResolver | None = None):
        from .cli import normalise_groups, secret_from

        self.cfg = cfg
        self._secret = secret_from(cfg)
        self.crypto = SdkCrypto(self._secret)
        self.me = self.crypto.our_pubkey()
        store_path = Path(os.path.expanduser(cfg.get("store", "gateway.db")))
        store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(store_path, identity=self.me)
        # Media (spec §4b): attachments are recorded with the message and fetched only
        # when the adapter asks, so this is configuration plus a cache — nothing here
        # touches the network at startup.
        self.media_cfg: MediaConfig = media_config(cfg.get("media"))
        self.media = self.media_cfg.fetcher()
        # Who is a bot (kind-0 `bot: true`). The registry is created before the policy
        # because the policy asks it on every decision; its fetch resolves the publish
        # set lazily, so a relay list that is parsed a few lines below is still the one
        # used at fetch time.
        self.bots = BotRegistry(self.store, fetcher=nak_fetcher(lambda: self.relays_publish))
        groups = normalise_groups(cfg)
        self.policy = Policy({
            "groups": groups,
            "policies": cfg.get("policies"),
            "defaults": cfg.get("defaults"),
            "rate_limit": cfg.get("rate_limit"),
            "bots": cfg.get("bots"),
        }, bot_lookup=self.bots.is_bot, echo_lookup=self._echo_lookup)
        self.concord = ConcordPlane(self.store)
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
        self.server = SocketServer(self.socket_path, self)
        self.workers: list[RelayWorker] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    def _setup_marmot(self, cfg: dict[str, Any]) -> None:
        """Attach the Marmot proxy only when the operator opted in *and* wn-agent is there.

        Opt-in, not opt-out. The gateway is not the only reader of wn-agent's
        control socket: Hermes' Marmot platform plugin subscribes to the same
        account, so a gateway plane attached by default hands the agent a second
        copy of every group message. `marmot.enabled: true` is the operator saying
        this process is the transport. Once opted in, a missing socket still means
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
            worker = RelayWorker(relay, self.me, kinds=kinds, p_tag=self.me,
                                 secret=self._secret, on_refusal=self._note_refusal)
            self.workers.append(worker)
            self._tasks.append(asyncio.create_task(self._guarded(worker)))
            # Feeds: kind 1 from people WE FOLLOW, never the global firehose.
            authors = tuple(sorted(set(self.follows) | set(self.nip01.authors)))
            if authors and not restricted:
                feed = RelayWorker(relay, self.me, kinds=(1,), authors=authors,
                                   secret=self._secret, purpose="feed")
                self.workers.append(feed)
                self._tasks.append(asyncio.create_task(self._guarded(feed)))
            # Mentions of us by strangers: a small #p filter, separate from follows.
            if not restricted:
                mentions = RelayWorker(relay, self.me, kinds=(1,), p_tag=self.me,
                                       secret=self._secret, purpose="mentions")
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
        import json as _json
        import subprocess
        try:
            out = subprocess.run(
                ["nak", "req", "-k", "3", "-a", self.me, "-l", "1", *self.relays_publish[:2]],
                capture_output=True, text=True, timeout=20,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                tags = _json.loads(line).get("tags", [])
                return tuple(sorted({t[1] for t in tags if t and t[0] == "p"}))
        except Exception:  # noqa: BLE001 - no contact list means no feed workers
            pass
        return ()

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
        worker = RelayWorker(relay, self.me, kinds=self.relay_kinds.get(relay) or DM_KINDS,
                             p_tag=self.me, secret=self._secret,
                             on_refusal=self._note_refusal, purpose="peer-inbox")
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
        await self.server.stop()
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
            asyncio.create_task(self.server.broadcast(self.frame_for(msg)))

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
        asyncio.create_task(self.server.broadcast(self.frame_for(msg)))


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

        results = await loop.run_in_executor(None, self._publish_all, wraps, relays)
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

    def _publish_all(self, wraps: dict, relays: list[str]) -> dict:
        return {label: publish(event, relays, secret=self._secret)
                for label, event in wraps.items()}

    def _recipient_relays(self, peer: str) -> list[str]:
        import json as _json
        import subprocess
        relays = list(self.relays_publish)
        try:
            out = subprocess.run(
                ["nak", "req", "-k", "10050", "-a", peer, "-l", "1", *relays[:2]],
                capture_output=True, text=True, timeout=25,
            ).stdout
            for line in out.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                for tag in _json.loads(line).get("tags", []):
                    if tag and tag[0] == "relay" and tag[1] not in relays:
                        relays.append(tag[1])
                break
        except Exception:  # noqa: BLE001 - no 10050 just means the floor set
            pass
        return relays[:6]

    @staticmethod
    def _hex(who: str) -> str:
        if who.startswith("npub"):
            import subprocess
            return subprocess.run(["nak", "decode", who], capture_output=True, text=True).stdout.strip()
        return who

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
        loop = asyncio.get_running_loop()
        sent = 0
        for row in pending:
            event = _json.loads(row["event"])
            # Bind both loop variables as defaults: run_in_executor schedules the
            # callable, and a late lambda would otherwise publish a later row.
            ok, error = await loop.run_in_executor(
                None,
                lambda event=event, row=row: list(
                    publish(event, [row["relay"]], secret=self._secret).values()
                )[0],
            )
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
            "planes": ["nip17", "nip01", "nip04", "concord"]
                      + (["marmot"] if self.marmot else []),
            "relays_publish": self.relays_publish,
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
                 "enumeration": self.marmot_resolver.enumeration()}
                if self.marmot and self.marmot_worker and self.marmot_resolver
                else {"enabled": False, "error": self.marmot_error,
                      "reason": self.marmot_idle}
            ),
            "workers": [self._worker_state(w) for w in self.workers],
            "uptime_s": None,
        }

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
