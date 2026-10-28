"""`hng` — the dogfood CLI. Same core the socket server will expose."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .bots import BotRegistry
from .control import ControlConfig
from .crypto import CryptoError, SdkCrypto, parse_secret
from .fetch import fetch_many_blocking, stream_events
from .health import (
    BAD,
    default_planes,
    health_report,
    human_age,
    relay_verdicts,
    render,
    render_health,
    reports_event_times,
    running_but_unconfigured,
    stale_rows,
)
from .media import MediaError, media_config, resolve_attachment
from .planes.concord.channels import is_conversation as is_concord_conversation
from .planes.nip17 import Nip17Plane
from .policy import Policy
from .profiles import (
    PLANES,
    ProfilesConfigError,
    channels_from_invites,
    hermes_profile_state,
    load_profiles,
)
from .projects import Project, ProjectMap, checks_for, load_registry
from .publisher import publish
from .store import Store, drops_ttl_seconds

DEFAULT_CONFIG = Path("config.yaml")


def load_config(path: str | None = None) -> dict[str, Any]:
    candidates = [path, os.environ.get("HNG_CONFIG"), str(DEFAULT_CONFIG),
                  str(Path.home() / ".config/hermes-nostr-gateway/config.yaml")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            with open(candidate) as fh:
                return yaml.safe_load(fh) or {}
    raise SystemExit("no config found: pass --config or create config.yaml")


def read_secret_file(path: str | Path) -> str:
    """Read a key file: the account JSON, or a file holding the key itself.

    A systemd credential is the source file copied byte for byte, so both shapes arrive
    here; a raw hex file usually has a trailing newline from `echo`/`install`, which is
    stripped. Never include the value in an error.
    """
    location = Path(path)
    try:
        text = location.read_text().strip()
    except OSError as exc:
        raise SystemExit(f"{location}: cannot read the identity key: {exc.strerror}") from None
    if not text:
        raise SystemExit(f"{location}: the identity key file is empty")
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{location}: not valid JSON ({exc.msg})") from None
        secret = data.get("secret_key_hex") or data.get("secret_key")
        if not secret:
            raise SystemExit(f"{location}: no secret_key_hex or secret_key in the JSON")
        return str(secret).strip()
    return text


def credential_path(name: str) -> Path:
    """Where systemd puts `LoadCredential=<name>:<source>` for this service.

    `$CREDENTIALS_DIRECTORY` is a per-unit tmpfs that systemd mounts for a service with at
    least one LoadCredential=; the value never reaches the environment, which is the point.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        raise SystemExit(
            f"identity.secret_credential: {name!r} needs $CREDENTIALS_DIRECTORY, which "
            "systemd sets for a unit with LoadCredential=. Add "
            f"LoadCredential={name}:<root-owned file> to the unit and start it under "
            "systemd, or set identity.secret_file instead"
        )
    path = Path(directory) / name
    if not path.exists():
        raise SystemExit(
            f"identity.secret_credential: {name!r} is not in $CREDENTIALS_DIRECTORY "
            f"({directory}); check the unit's LoadCredential= line"
        )
    return path


def _checked_secret(secret: str, source: str) -> str:
    """Normalise to hex, and name the source when it is not a key at all."""
    try:
        return parse_secret(secret)
    except CryptoError as exc:
        raise SystemExit(f"{source}: {exc}") from None


def secret_from(cfg: dict[str, Any]) -> str:
    """The identity key, from the first source the config names.

    `secret_credential` is what a service should use: systemd copies the key into
    `$CREDENTIALS_DIRECTORY`, mode 0400, and it appears in neither `systemctl show` nor
    the process environment — so no child process inherits it. `secret_env` and
    `secret_file` remain for a hand-run CLI and for the container.
    """
    identity = cfg.get("identity") or {}
    if identity.get("secret_credential"):
        name = str(identity["secret_credential"])
        path = credential_path(name)
        return _checked_secret(read_secret_file(path), f"credential {name!r} ({path})")
    if identity.get("secret_env"):
        name = str(identity["secret_env"])
        try:
            value = os.environ[name]
        except KeyError:
            raise SystemExit(f"identity.secret_env: ${name} is not set") from None
        return _checked_secret(value, f"environment ${name}")
    if not identity.get("secret_file"):
        raise SystemExit("identity: set secret_credential, secret_env or secret_file")
    path = os.path.expanduser(str(identity["secret_file"]))
    return _checked_secret(read_secret_file(path), f"secret_file {path}")


def _decode_npub(npub: str) -> str:
    from .fetch import hex_of

    return hex_of(npub)


def normalise_groups(cfg: dict[str, Any]) -> dict[str, Any]:
    """Groups are authored as npubs but events carry hex, so normalise once, here.

    Keeping this in the CLI (not in Policy) leaves the policy engine dependency-free
    and unit-testable without a decode backend.
    """
    groups = cfg.get("groups") or {}
    return {name: [_decode_npub(m) if str(m).startswith("npub") else m for m in members]
            for name, members in groups.items()}

def build(cfg: dict[str, Any]) -> tuple[SdkCrypto, Store, Policy, Nip17Plane]:
    crypto = SdkCrypto(secret_from(cfg))
    store_path = Path(os.path.expanduser(cfg.get("store", "gateway.db")))
    store_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(store_path, identity=crypto.our_pubkey())
    # The CLI reads bot flags from the store but never fetches one: a one-shot command
    # should not spend a child process on a profile, and the daemon owns discovery.
    policy = Policy(
        {"groups": normalise_groups(cfg), "policies": cfg.get("policies"),
         "defaults": cfg.get("defaults"), "rate_limit": cfg.get("rate_limit"),
         "bots": cfg.get("bots")},
        bot_lookup=BotRegistry(store).is_bot,
        echo_lookup=lambda peer, since: [row["content"] or ""
                                         for row in store.recent_outbound(peer, since)],
    )
    # `hng listen` records a verified invite exactly as the daemon does, and decides
    # nothing: a join is a relay write, and the control pane in front of it lives in the
    # daemon (`decide_invite`) or in `hng concord <id>`. Without this the invites a
    # one-shot listener saw would be dropped unrecorded.
    from .planes.concord.plane import ConcordPlane

    concord = ConcordPlane(store, control=ControlConfig.from_config(cfg.get("control")))
    return crypto, store, policy, Nip17Plane(crypto, store, policy,
                                             on_invite=concord.handle_invite)


def to_hex(who: str) -> str:
    from .fetch import hex_of

    return hex_of(who)


def print_message(msg, as_json: bool) -> None:
    if as_json:
        print(json.dumps(msg.to_frame(), separators=(",", ":")))
        return
    import datetime
    when = datetime.datetime.fromtimestamp(msg.created_at, datetime.UTC).strftime("%d %b %H:%M")
    print(f"[{when}] {msg.sender[:12]}… ({msg.reach}/{msg.authority}) {msg.content}")


async def found(plane: Nip17Plane, relay: str, event: dict, args) -> None:
    msg = plane.handle(relay, event)
    if not msg:
        return
    if msg.reach == "drop":
        # A blocked message has no business looking like chat in a dogfood CLI.
        print(f"[blocked] {msg.sender[:12]}… reason={msg.blocked_reason or 'not_allowlisted'}",
              file=sys.stderr)
        return
    print_message(msg, args.json)
    if args.mark_read:
        plane.store.mark_delivered([msg.id])


def cmd_listen(args) -> int:
    from .gateway import relay_read_plan

    cfg = load_config(args.config)
    crypto, store, policy, plane = build(cfg)
    secret = secret_from(cfg)
    # `relays.read` may pin a per-relay kind set ({url, kinds}); listen only speaks
    # NIP-17, so the pin is respected but NIP-04 kind 4 is not a listen default.
    relays, relay_kinds = relay_read_plan(cfg)

    def kinds_for(relay: str) -> tuple[int, ...]:
        return relay_kinds.get(relay) or (1059,)

    if args.once:
        since = int(time.time()) - args.since_hours * 3600 if args.since_hours else None
        total = 0
        # One bounded read per relay rather than one REQ across all of them: the
        # per-relay count is the row `hng status` shows as history, and a relay that
        # refuses a kind must not cost the others their results.
        for relay in relays:
            try:
                events = fetch_many_blocking(relays=[relay], kinds=kinds_for(relay),
                                             p_tag=plane.me, limit=args.limit, since=since,
                                             secret=secret)
            except Exception as exc:  # noqa: BLE001 - report, do not crash
                store.note_relay(relay, error=str(exc)[:200])
                continue
            store.note_relay(relay, events=len(events))
            for event in events:
                msg = plane.handle(relay, event)
                if msg:
                    print_message(msg, args.json)
                    total += 1
        print(f"-- {total} new message(s) across {len(relays)} relays", file=sys.stderr)
        return 0

    async def deliver(relay: str, event: dict) -> None:
        await found(plane, relay, event, args)

    async def stream() -> None:
        # `-p me` is not optional: a kind-1059 subscription without it is a firehose of
        # every wrap on the relay. `stream_events` scopes it and answers NIP-42 in
        # process as our identity, so the key is never in an argv or a child's env.
        # A pinned kind set is a per-relay filter, and a REQ is all-or-nothing at the
        # relay: group the read set by kind set, one stream per group, so no relay is
        # asked for a kind it refuses.
        groups: dict[tuple[int, ...], list[str]] = {}
        for relay in relays:
            groups.setdefault(kinds_for(relay), []).append(relay)
        tasks = [asyncio.create_task(stream_events(
                     relays=group, kinds=kinds, p_tag=plane.me, on_event=deliver,
                     secret=secret))
                 for kinds, group in groups.items()]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            for task in tasks:
                task.cancel()

    try:
        asyncio.run(stream())
    except KeyboardInterrupt:
        pass
    return 0


def recipient_relays(cfg: dict[str, Any], peer: str) -> list[str]:
    from .fetch import fetch_first_blocking

    relays = list(cfg["relays"].get("publish", []))
    event = fetch_first_blocking(relays=relays[:2], kinds=[10050], author=peer)
    for tag in (event or {}).get("tags", []):
        if tag and tag[0] == "relay" and len(tag) > 1 and tag[1] not in relays:
            relays.append(tag[1])
    return relays[:6]


def send_to_channel(args, cfg: dict[str, Any], conversation: str) -> int:
    """Hand a channel reply to the daemon, which already holds the community.

    A one-shot CLI would have to fetch the community and derive the channel's keys — the
    read path over again. The running gateway has both, and `send` is the same op a DM
    takes, so a reply into a channel has one implementation and one signer.
    """
    from .gateway import default_socket_path
    from .server import request

    socket_path = Path(os.path.expanduser(
        cfg.get("socket") or str(default_socket_path())))
    try:
        frame = asyncio.run(request(socket_path, {
            "op": "send", "to": conversation, "text": args.text,
            "reply_to": args.reply_to}))
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"a channel reply needs the running daemon: {exc}", file=sys.stderr)
        return 1
    if frame.get("type") == "error":
        print(f"  {conversation} -> FAILED ({frame.get('error') or 'refused'})",
              file=sys.stderr)
        return 1
    errors = frame.get("errors") or {}
    for per_relay in (frame.get("relays") or {}).values():
        for relay, ok in per_relay.items():
            print(f"    {relay}: {'ok' if ok else errors.get(relay, 'failed')}")
    ok = bool(frame.get("ok"))
    detail = f" ({frame['error']})" if not ok and frame.get("error") else ""
    print(f"  {conversation} -> {'sent' if ok else 'FAILED'}{detail}")
    return 0 if ok else 1


def cmd_send(args) -> int:
    cfg = load_config(args.config)
    if is_concord_conversation(args.to):
        return send_to_channel(args, cfg, str(args.to).strip())
    crypto, store, policy, plane = build(cfg)
    secret = secret_from(cfg)
    peer = to_hex(args.to)
    rumor = crypto.build_rumor(args.text, peer, reply_to=args.reply_to)
    wraps = {"peer": crypto.wrap(rumor, peer), "self": crypto.wrap(rumor, plane.me)}
    relays = recipient_relays(cfg, peer)
    ok_peer = False
    for label, wrap in wraps.items():
        # Durable before attempted, retried from the stored event rather than re-wrapped,
        # and published through the same function the daemon uses, so NIP-42 signs as us
        # in this process rather than as whatever key a child would have held (spec §3, §7).
        recipient = peer if label == "peer" else plane.me
        store.record_wrap(wrap, rumor["id"], recipient, relays)
        results = publish(wrap, relays, secret=secret)
        for relay, (ok, error) in results.items():
            store.mark_wrap(wrap["id"], relay, ok=ok, error=error)
            if not ok:
                print(f"    {relay}: {error}", file=sys.stderr)
        published = [relay for relay, (ok, _) in results.items() if ok]
        print(f"  {label:5} {wrap['id'][:12]}… -> {', '.join(published) or 'FAILED'}")
        if label == "peer":
            ok_peer = bool(published)
    if ok_peer:
        from .store import Inbound
        store.add(Inbound(
            id=rumor["id"], wrapper_id=wraps["peer"]["id"], sender=plane.me,
            kind=rumor.get("kind", 14), created_at=int(rumor["created_at"]),
            content=args.text, conversation=peer, reply_to=args.reply_to,
            reach="sent", authority="self", rule="outbound", why="sent by us",
        ))
        store.mark_delivered([rumor["id"]])
    return 0 if ok_peer else 1


def cmd_status(args) -> int:
    from dataclasses import replace

    from .gateway import default_socket_path, relay_read_plan
    from .server import request

    cfg = load_config(args.config)
    crypto, store, policy, plane = build(cfg)
    stats = store.stats()
    drops_ttl = drops_ttl_seconds(cfg.get("drops"))
    drops = {**store.drop_counts(drops_ttl), "ttl_hours": drops_ttl / 3600}
    now = time.time()
    relays, _ = relay_read_plan(cfg)
    publish = [str(relay) for relay in (cfg.get("relays") or {}).get("publish") or []]
    socket_path = Path(os.path.expanduser(
        args.socket or cfg.get("socket") or str(default_socket_path())))

    # The verdict comes from the running daemon, never from the store: relay_stats rows
    # are history (see health.py). A daemon that does not answer leaves the relays
    # "unknown", which is the truth, rather than borrowing the last stderr line of a
    # child process and calling it an error.
    frame: dict[str, Any] | None = None
    problem = "no socket"
    try:
        reply = asyncio.run(request(socket_path, {"op": "status"}))
        if reply.get("type") == "status":
            frame = reply
        else:
            problem = f"unexpected reply {reply.get('type')!r}"
    except (OSError, TimeoutError, ValueError) as exc:
        problem = str(exc) or exc.__class__.__name__

    verdicts = relay_verdicts(frame, relays=relays, now=now,
                              quiet_minutes=args.quiet_minutes)
    if frame is None:
        # No daemon, no verdict. The store row is printed as history so the operator
        # still has the last recorded counters without them pretending to be health.
        history = {str(row.get("relay")): row for row in stats["relays"]}
        verdicts = [
            replace(v, detail=f"{v.detail}; history: "
                              f"{_history_text(history.get(v.relay), now)}")
            for v in verdicts
        ]

    stale = stale_rows({"relay_stats": stats["relays"]}, set(relays) | set(publish))

    if args.json:
        print(json.dumps({
            "identity": plane.me,
            "store": str(store.path),
            "socket": str(socket_path),
            "daemon": frame is not None,
            "event_times": reports_event_times(frame),
            "rumors": stats["total"],
            "pending": stats["pending"],
            "blocked": stats["blocked"],
            "drops": drops,
            "invites": stats["invites"],
            "invites_held": stats["invites_held"],
            "media": stats.get("media") or {},
            "wraps": store.wrap_counts(),
            "relays": [verdict.as_dict() for verdict in verdicts],
            "not_in_config": stale,
        }, indent=2))
        return _status_exit(frame, verdicts, args)

    print(f"identity: {plane.me}")
    print(f"store:    {store.path}")
    print(f"rumors:   {stats['total']} ({stats['pending']} pending delivery, "
          f"{stats['blocked']} blocked)")
    print(f"drops:    {drops['total']} refused ({drops['live']} live, "
          f"{drops['expired']} expired; ttl {drops['ttl_hours']:.0f}h)")
    print(f"invites:  {stats['invites']} concord invite(s), {stats['invites_held']} held")
    media = stats.get("media") or {}
    print(f"media:    {media.get('attachments', 0)} attachment(s), "
          f"{media.get('cached', 0)} cached")
    wraps = store.wrap_counts()
    print(f"retry:    {wraps.get('pending', 0) + wraps.get('failed', 0)} wrap(s) retryable, "
          f"{wraps.get('expired', 0)} expired, {wraps.get('sent', 0)} sent")
    if frame is None:
        print(f"daemon:   not running ({socket_path}: {problem})")
        print("relays:   (no live worker state; the daemon answers this question)")
    else:
        workers = len(frame.get("workers") or [])
        uptime = frame.get("uptime_s")
        ago = f", up {human_age(uptime)}" if uptime is not None else ""
        print(f"daemon:   up ({socket_path}, {workers} worker(s){ago})")
        print("relays:   (live worker state)")
    for line in render(verdicts):
        print(line)
    if frame is not None and not reports_event_times(frame):
        print("note:     this daemon reports no per-worker event times "
              "(restart it for \"no events for Nm\" verdicts)")

    if stale:
        width = max(len(str(row.get("relay"))) for row in stale)
        print("not in config (history only, not read):")
        for row in stale:
            age = (human_age(now - row["last_event"]) if row.get("last_event") else "-")
            print(f"  {row['relay']:<{width}}  events={row['events']:<5} "
                  f"last event {age}{' ago' if age != '-' else ''}")

    extra = running_but_unconfigured(frame, set(relays) | set(publish))
    if extra:
        print("daemon still reads relays the config no longer lists (restart to apply):")
        for relay in extra:
            print(f"  {relay}")

    if args.verbose and frame is not None:
        print("diagnostics (child stderr, not a verdict):")
        for worker in frame.get("workers") or []:
            said = worker.get("last_stderr") or worker.get("last_error")
            if said:
                print(f"  {worker.get('relay')}  {said}")

    return _status_exit(frame, verdicts, args)


def cmd_health(args) -> int:
    """One call, one frame: what the gateway did per plane and what is degraded.

    The overnight narrative is generated from this, so it must not need a second
    command and must not be assembled from log lines. When the daemon is not running
    the answer is still produced — from the store, labelled history, and degraded.
    """
    from .gateway import default_socket_path, relay_read_plan
    from .server import request

    cfg = load_config(args.config)
    _crypto, store, _policy, plane = build(cfg)
    relays, _ = relay_read_plan(cfg)
    socket_path = Path(os.path.expanduser(
        args.socket or cfg.get("socket") or str(default_socket_path())))

    frame: dict[str, Any] | None = None
    problem: str | None = None
    try:
        reply = asyncio.run(request(socket_path, {
            "op": "health",
            "quiet_minutes": args.quiet_minutes,
            "pending_stale_minutes": args.pending_stale_minutes,
        }))
        if reply.get("type") == "health":
            frame = reply
        elif reply.get("code") == "unsupported":
            problem = str(reply.get("error") or "this daemon has no health op")
        else:
            problem = (f"unexpected reply {reply.get('type')!r}"
                       + (f": {reply['error']}" if reply.get("error") else ""))
    except (OSError, TimeoutError, ValueError) as exc:
        problem = str(exc) or exc.__class__.__name__

    if frame is None:
        now = time.time()
        frame = health_report(
            planes=default_planes(
                marmot=(cfg.get("marmot") or {}).get("enabled") is True),
            plane_rows=store.plane_report(),
            verdicts=relay_verdicts(None, relays=relays, now=now,
                                    quiet_minutes=args.quiet_minutes),
            wraps=store.wrap_report_by_plane(),
            daemon=False,
            daemon_error=problem,
            identity=plane.me,
            socket_path=str(socket_path),
            now=now,
            pending_stale_minutes=args.pending_stale_minutes,
            notes=["plane counters are from the store: with no daemon they are "
                   "history, not live state"],
        )

    if args.json:
        print(json.dumps(frame, indent=2))
    else:
        for line in render_health(frame):
            print(line)
    if args.check and frame.get("verdict") != "ok":
        return 1
    return 0


def _history_text(row: dict[str, Any] | None, now: float) -> str:
    if not row:
        return "no row in the store"
    age = human_age(now - row["last_event"]) if row.get("last_event") else None
    return (f"events={row.get('events', 0)}, last event {age} ago" if age
            else f"events={row.get('events', 0)}, no event recorded")


def _status_exit(frame: dict[str, Any] | None, verdicts, args) -> int:
    if not getattr(args, "check", False):
        return 0
    return 1 if frame is None or any(v.severity == BAD for v in verdicts) else 0


def cmd_replay_skip(args) -> int:
    """Record a handover: the pending backlog was answered by the previous adapter."""
    cfg = load_config(args.config)
    _crypto, store, _policy, _plane = build(cfg)
    skipped = store.skip_pending()
    print(f"replay floor: {store.replay_floor()} "
          f"({skipped} pending message(s) marked delivered)")
    return 0


def cmd_recent(args) -> int:
    cfg = load_config(args.config)
    crypto, store, policy, plane = build(cfg)
    for row in reversed(store.recent(args.number)):
        direction = "out" if row["authority"] == "self" else "in "
        print(f"[{row['created_at']}] {direction} {row['sender'][:10]}… {row['reach']:<9} "
              f"{(row['content'] or '')[:90]}")
    return 0


def _projects_line(project: Project) -> str:
    """Second line of a listing: the state of the checkout, then what drives its work."""
    state = project.config_error or project.problem or "ok"
    markers = ", ".join(m for m in ("git" if project.is_git else "",
                                    "AGENTS.md" if project.has_agents_md else "") if m)
    driven = f"project={project.project or '-'} board={project.board or '-'}"
    return f"{state}{' · ' + markers if markers else ''} · {driven}"


def cmd_projects(args) -> int:
    """The channel -> checkout map, and what each channel's work depends on.

    `--check` turns the map into a question: is the checkout there, is the project
    registered, does the board exist and is it bound to that project? A gap is reported
    per channel and the exit code says whether there was one -- never a crash, because a
    broken mapping must not stop the gateway reading messages.
    """
    cfg = load_config(args.config)
    mapping = ProjectMap.from_config(cfg.get("concord") or {})
    checking = bool(getattr(args, "check", False))
    if checking:
        registry = load_registry()
        rows = [(project, checks_for(project, registry)) for project in mapping.all()]
        if args.json:
            print(json.dumps([dict(project.as_dict(),
                                   checks=[check.as_dict() for check in checks])
                              for project, checks in rows], indent=2))
            return 1 if any(not check.ok for _, checks in rows for check in checks) else 0
        if not mapping:
            print("concord.projects is empty: no channel resolves to a checkout")
            return 0
        failed = 0
        for project, checks in rows:
            print(f"{project.channel}  ({project.community or 'any community'})  "
                  f"{project.path}")
            for check in checks:
                if not check.ok:
                    failed += 1
                print(f"  {check.name:<9}{'ok' if check.ok else 'MISSING':<9}{check.detail}")
        if failed:
            print(f"{failed} piece(s) missing across {len(mapping)} channel(s)")
        return 1 if failed else 0
    if args.json:
        print(json.dumps([project.as_dict() for project in mapping.all()], indent=2))
        return 0
    if not mapping:
        print("concord.projects is empty: no channel resolves to a checkout")
        return 0
    for project in mapping.all():
        print(f"{project.channel:<18} {(project.community[:12] or '(any)'):<14} "
              f"{project.name:<24} {project.path}")
        print(f"{'':<18} {_projects_line(project)}")
    return 0


def _held_concord_channels(cfg: dict[str, Any]) -> list[Any] | None:
    """The channels the stored Concord invites grant, or None when there is nothing to read.

    `hng profiles --check` has no live plane, so this reads the same grants the daemon's
    refresh unions from. None means "cannot say", which skips the existence check rather than
    refusing a route on a guess.
    """
    path = Path(os.path.expanduser(str(cfg.get("store") or "gateway.db")))
    if not path.exists():
        return None
    store = Store(path)
    try:
        rows = store.concord_invites(limit=50)
    finally:
        store.close()
    return channels_from_invites(rows) or None


def cmd_profiles(args) -> int:
    """Which Hermes profile each plane and channel runs as, and whether Hermes can run it.

    `hng profiles` prints the table. `--check` is the load gate: non-zero when a rule names a
    profile Hermes does not have (or one that opted out of the host gateway), an unknown plane,
    a duplicate channel rule, or a non-multiplexed Hermes -- the cases in which a routed turn
    would quietly run as the default profile. Warnings (a parked profile, an unset
    `gateway.multiplex_profiles`) are printed and do not fail the check: parking is temporary
    and the flag defaults on.
    """
    cfg = load_config(args.config)
    router = load_profiles(cfg)
    state = hermes_profile_state(cfg)
    known = _held_concord_channels(cfg) if router.channels else None

    if getattr(args, "check", False):
        problems = list(router.problems)
        if router.channels and known is None:
            # Not checked is not missing: an empty list would say "holds nothing", and every
            # channel rule would be reported stale on a host whose first invite has not landed.
            print("profiles: no stored Concord invite, so channel rules were not checked "
                  "against a channel list", file=sys.stderr)
        if known is not None:
            problems.extend(router.channel_problems(known))
        warnings = list(router.warnings)
        if args.json:
            print(json.dumps({"ok": not problems, "problems": problems, "warnings": warnings,
                              "configured": router.configured,
                              "multiplex": state.get("multiplex"),
                              "source": state.get("source")}, indent=2))
        else:
            for problem in problems:
                print(f"config: {problem}", file=sys.stderr)
            for warning in warnings:
                print(f"warning: {warning}", file=sys.stderr)
            if not problems and not router.configured:
                print("profiles is not configured: every turn runs as the gateway's own profile")
            elif not problems:
                print("profiles are usable: every route names a profile Hermes can run a "
                      "turn as")
        return 1 if problems else 0

    if args.json:
        print(json.dumps(router.as_dict(), indent=2))
        return 0 if (not router.configured or router.usable) else 1
    for line in router.describe():
        print(line)
    return 0 if (not router.configured or router.usable) else 1


def _plane_for(cfg: dict[str, Any], conversation: str, explicit: str) -> tuple[str, str]:
    """The plane a conversation belongs to, and how that was decided.

    Asked for, then read from the shape (`concord:` is unambiguous), then from the store's own
    rows. A DM peer can have rows on two planes and a Marmot group id is a hex string like any
    peer key, so evidence beats shape and an ambiguity is reported rather than resolved by a
    guess: a route applied to the wrong plane is a turn under the wrong identity.
    """
    chosen = (explicit or "").strip().lower()
    if chosen:
        return chosen, "asked for with --plane"
    if conversation.strip().lower().startswith("concord:"):
        return "concord", "the conversation's shape"
    path = Path(os.path.expanduser(str(cfg.get("store") or "gateway.db")))
    if not path.exists():
        return "", "unknown (no store to read)"
    store = Store(path)
    try:
        planes = store.planes_for_conversation(conversation)
    finally:
        store.close()
    if len(planes) == 1:
        return planes[0], "the store"
    if not planes:
        return "", "unknown (the store has never seen it)"
    return "", f"ambiguous ({', '.join(planes)} hold it); pass --plane"


def _config_path(explicit: str | None = None) -> Path | None:
    """The file `load_config` would read, for a caller that needs to name it.

    `hng route` hands it to the model-route block, whose key lookup defaults to a `.env` beside
    the config. The search order is `load_config`'s, so the two cannot disagree about which
    file is in force.
    """
    for candidate in (explicit, os.environ.get("HNG_CONFIG"), str(DEFAULT_CONFIG),
                      str(Path.home() / ".config/hermes-nostr-gateway/config.yaml")):
        if candidate and Path(candidate).exists():
            return Path(candidate)
    return None


def _model_route_lines(cfg: dict[str, Any], config_path: Any, plane: str,
                       conversation: str) -> list[str]:
    """The model route for one conversation, when model routing is in this build.

    Model routing is its own block with its own card (`pr/route-model`): it decides provider,
    model and credential where this decides identity. It is imported rather than assumed, so
    `hng route` works in a build that has one and not the other and prints both when it has
    both -- which is the point of one verb.
    """
    try:
        routing = importlib.import_module(f"{__package__}.routing")
    except ImportError:
        return []
    if not cfg.get("routing"):
        return []
    try:
        router = routing.Router.from_config(cfg.get("routing"), config_path=config_path)
        resolution = router.resolve(plane, conversation)
        data = resolution.as_dict()
    except Exception as exc:  # noqa: BLE001 - an older module must not break `hng route`
        return [f"  model      unavailable ({type(exc).__name__}: {exc})"[:200]]
    if data.get("error"):
        return [f"  model      UNRESOLVED {data['error']}: {data.get('detail', '')}"[:200]]
    return [f"  model      {data.get('provider', '')}/{data.get('model', '')}"
            f"  ({data.get('rule', 'none')})"]


def cmd_route(args) -> int:
    """Which profile -- and, when that block exists, which model -- one conversation uses.

    `hng route <conversation>` answers the question an operator actually has: this channel's
    answers come out on the wrong identity or the wrong model, so which rule won, what did it
    beat, and is it usable. With no conversation it prints the whole profiles table.
    """
    cfg = load_config(args.config)
    config_path = _config_path(args.config)
    router = load_profiles(cfg)
    conversation = (args.conversation or "").strip()
    plane, how = _plane_for(cfg, conversation, args.plane)

    if not conversation:
        if args.json:
            print(json.dumps(router.as_dict(), indent=2))
            return 0 if (not router.configured or router.usable) else 1
        print("profiles")
        for line in router.describe():
            print(line)
        model = _model_route_lines(cfg, config_path, "", "")
        if model:
            print("model")
            for line in model:
                print(line)
        return 0 if (not router.configured or router.usable) else 1

    known = _held_concord_channels(cfg) if (router.channels and plane == "concord") else None
    resolution = router.resolve(plane, conversation, known_channels=known)
    # With no plane in hand, a plane rule may be exactly what decides this conversation, and
    # printing `profiles.default` would answer a question that was not decidable.
    undecided = (resolution.ok and not plane and bool(router.planes)
                 and resolution.rule == "default")
    model = _model_route_lines(cfg, config_path, plane, conversation)
    if args.json:
        data = {**resolution.as_dict()}
        if undecided:
            data.update(error="plane_unknown", ok=False, detail=how)
        print(json.dumps({"conversation": conversation, "plane": plane, "plane_from": how,
                          **data, "model": model}, indent=2))
        return 1 if (undecided or not resolution.ok) else 0
    print(f"{conversation}  ({plane or 'unknown plane'}; {how})")
    if undecided:
        print(f"  profile    UNRESOLVED plane_unknown: {how}; "
              f"profiles.planes.{'/'.join(sorted(router.planes))} may decide it")
        for line in model:
            print(line)
        return 1
    if resolution.error:
        print(f"  profile    UNRESOLVED {resolution.error}: {resolution.detail}")
        if resolution.why:
            print(f"  rule       {resolution.why}")
        for line in model:
            print(line)
        return 1
    home = router.homes.get(resolution.profile) if router.configured else None
    print(f"  profile    {resolution.profile or '(not routed: profiles is not configured)'}")
    if home is not None:
        print(f"  home       {home.path}")
    print(f"  rule       {resolution.why}")
    for line in model:
        print(line)
    return 0


def cmd_control(args) -> int:
    """Where a confirmation goes per plane, and who is trusted enough not to need one."""
    cfg = load_config(args.config)
    control = ControlConfig.from_config(cfg.get("control"))
    if args.json:
        print(json.dumps({
            "targets": control.targets,
            "trusted_npubs": sorted(control.trusted),
            "planes": {plane: sorted(keys) for plane, keys in control.per_plane.items()},
            "problems": list(control.problems),
        }, indent=2))
        return 0
    for problem in control.problems:
        print(f"config: {problem}", file=sys.stderr)
    print("control pane")
    for line in control.describe():
        print(f"  {line}")
    return 0


def cmd_concord(args) -> int:
    """Concord invites: what is held, and accepting one (the human's yes).

    `hng concord` lists what this gateway recorded, with the state each invite is in.
    `hng concord <id>` publishes the Guestbook join the daemon would have published for a
    trusted inviter, so an invite held for a human can be released without editing config
    or waiting for the daemon to restart. Both read and write the same row, so the daemon's
    next refresh sees the community as joined.
    """
    from .planes.concord import read as concord_read
    from .planes.concord import write as concord_write

    cfg = load_config(args.config)
    crypto, store, _policy, _plane = build(cfg)
    if not args.invite:
        return list_invites(store, args)

    row = store.concord_invite(args.invite)
    if row is None:
        print(f"no invite {args.invite!r}: `hng concord` lists what this gateway holds",
              file=sys.stderr)
        return 1
    label = row["name"] or row["community_id"][:12] + "..."
    if row["status"] == "joined" or store.concord_joined(row["community_id"]):
        print(f"{label}: already joined (invite {row['id']})")
        return 0
    invite = concord_read.Invite(bundle=row["bundle"], inviter=row["inviter"],
                                 relay="", wrap_id=row["id"])
    try:
        results = asyncio.run(concord_write.join_guestbook(crypto.keys, invite))
    except Exception as exc:  # noqa: BLE001 - report, do not traceback at an operator
        store.decide_invite(row["id"], status="failed",
                            why=f"join raised {type(exc).__name__}: {exc}"[:300],
                            decided_by="cli")
        print(f"{label}: join FAILED ({type(exc).__name__}: {exc})"[:300], file=sys.stderr)
        return 1
    ok = any(outcome == "ok" for outcome in results.values())
    store.decide_invite(row["id"], status="joined" if ok else "failed",
                        why="accepted by hand" if ok else "no relay accepted the join",
                        decided_by="cli")
    if args.json:
        print(json.dumps({"id": row["id"], "community": row["community_id"],
                          "name": row["name"], "status": "joined" if ok else "failed",
                          "relays": results}, indent=2))
        return 0 if ok else 1
    for relay, outcome in results.items():
        print(f"    {relay}: {outcome}")
    print(f"{label}: {'joined' if ok else 'FAILED'}")
    return 0 if ok else 1


def list_invites(store: Store, args) -> int:
    rows = store.concord_invites(args.limit, status="held" if args.held else None)
    if args.json:
        # The bundle is key material, not a listing's business: every other field is.
        print(json.dumps({"invites": [
            {key: value for key, value in row.items() if key != "bundle"} for row in rows
        ]}, indent=2))
        return 0
    if not rows:
        print("no concord invites recorded")
        return 0
    held = sum(1 for row in rows if row["status"] == "held")
    print(f"{len(rows)} concord invite(s), {held} held")
    for row in rows:
        print(f"  {row['status']:<8} {row['name'] or '(unnamed)'}  {row['id']}")
        print(f"           community {row['community_id']}  from {row['inviter']}")
        if row.get("why"):
            print(f"           {row['why']}")
        if row["status"] == "held":
            print(f"           accept: hng concord {row['id']}")
    return 0


def cmd_drops(args) -> int:
    """Refused messages: what policy dropped, why, and when — the `drops` op's client."""
    import datetime

    cfg = load_config(args.config)
    _crypto, store, _policy, _plane = build(cfg)
    ttl_s = drops_ttl_seconds(cfg.get("drops"))
    since = args.since
    if args.since_hours:
        since = int(time.time()) - args.since_hours * 3600
    result = store.drops(plane=args.plane, reason=args.reason, since=since,
                         limit=args.limit, ttl_s=ttl_s)
    if args.json:
        print(json.dumps(result, indent=2))
        return 0
    print(f"{result['total']} drop(s): {result['live']} live, {result['expired']} expired"
          f" (ttl {ttl_s // 3600}h)")
    for row in result["drops"]:
        when = datetime.datetime.fromtimestamp(
            row["received_at"], datetime.UTC).strftime("%d %b %H:%M")
        mark = "  [expired]" if row["expired"] else ""
        print(f"[{when}] {row['plane']:<7} {row['sender'][:12]}… {row['reason']}"
              f"  {row['why']}{mark}")
    return 0


def cmd_media(args) -> int:
    """Fetch one blob an inbound message references — the same path `media.get` takes."""
    cfg = load_config(args.config)
    _crypto, store, _policy, _plane = build(cfg)
    media_cfg = media_config(cfg.get("media"))
    if not media_cfg.enabled:
        print("media is disabled: set media.enabled: true", file=sys.stderr)
        return 1
    ref = args.ref.strip()
    try:
        entry = (resolve_attachment(store, url=ref)
                 if ref.startswith(("http://", "https://"))
                 else resolve_attachment(store, sha256=ref.lower()))
        result = media_cfg.fetcher().fetch(entry)
    except MediaError as exc:
        print(f"{exc.code}: {exc}", file=sys.stderr)
        return 1
    store.mark_media_fetched(result["sha256"], result["path"])
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        cached = " (cached)" if result["cached"] else ""
        print(f"{result['path']}  {result['mime']}  {result['size']} bytes{cached}")
    return 0


def cmd_policy_test(args) -> int:
    cfg = load_config(args.config)
    store = Store(Path(os.path.expanduser(cfg.get("store", "gateway.db"))),
                  identity="policy-test")
    try:
        policy = Policy(
            {"groups": normalise_groups(cfg), "policies": cfg.get("policies"),
             "defaults": cfg.get("defaults"), "rate_limit": cfg.get("rate_limit"),
             "bots": cfg.get("bots")},
            bot_lookup=(lambda _sender: True) if args.bot else None,
            echo_lookup=lambda peer, since: [row["content"] or ""
                                             for row in store.recent_outbound(peer, since)],
        )
        d = policy.decide(sender=to_hex(args.sender), kind=args.kind,
                          mention=args.mention, dm=args.dm, event_id=args.event_id,
                          text=args.text)
    finally:
        store.close()
    print(json.dumps(d.to_frame(), indent=2))
    return 0



STOP_BUDGET_S = 8.0


def cmd_serve(args) -> int:
    import logging
    import os
    import signal

    from .gateway import Gateway, MarmotOwnershipError

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    try:
        gw = Gateway(cfg, socket_path=args.socket)
    except MarmotOwnershipError as exc:
        # The guard working, not a crash: one line with both ways out, and a non-zero
        # exit so systemd records a failed start instead of a daemon that runs wrong.
        print(f"refusing to start: {exc}", file=sys.stderr)
        return 2
    except ProfilesConfigError as exc:
        # A route that names a profile Hermes cannot run a turn as is a turn under the wrong
        # identity, so this is also a start that must not happen. `hng profiles --check`
        # reports the same problems without starting anything.
        print(f"refusing to start: profiles: {exc}", file=sys.stderr)
        return 2

    async def run() -> None:
        await gw.start()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except NotImplementedError:  # pragma: no cover
                pass
        await stop.wait()
        log = logging.getLogger("hng")
        log.info("stop requested; shutting down")
        # A clean stop, but never an unbounded one: something in the worker/executor
        # teardown can hold the loop past systemd's TimeoutStopSec, and the resulting
        # SIGKILL loses in-flight publishes. Give it a budget, then exit on our terms.
        try:
            await asyncio.wait_for(gw.stop(), STOP_BUDGET_S)
            log.info("shutdown complete")
        except (asyncio.TimeoutError, TimeoutError):
            log.warning("shutdown exceeded %ss; exiting anyway", STOP_BUDGET_S)
        logging.shutdown()
        os._exit(0)

    asyncio.run(run())
    return 0

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hng", description="Hermes Nostr Gateway")
    parser.add_argument("--config", help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_listen = sub.add_parser("listen", help="stream inbound DMs")
    p_listen.add_argument("--once", action="store_true", help="one-shot fetch, then exit")
    p_listen.add_argument("--limit", type=int, default=50)
    p_listen.add_argument("--since-hours", type=int, default=None)
    p_listen.add_argument("--json", action="store_true")
    p_listen.add_argument("--mark-read", action="store_true")
    p_listen.set_defaults(func=cmd_listen)

    p_send = sub.add_parser("send", help="send a NIP-17 DM, or a Concord channel message")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--text", required=True)
    p_send.add_argument("--reply-to", default=None)
    p_send.set_defaults(func=cmd_send)

    p_status = sub.add_parser("status", help="identity, store and live relay health")
    p_status.add_argument("--socket", default=None, help="daemon socket override")
    p_status.add_argument("--quiet-minutes", type=int, default=30,
                          help="report a connected relay with no events for this long "
                               "(0 disables; default 30)")
    p_status.add_argument("--json", action="store_true", help="machine-readable verdicts")
    p_status.add_argument("--check", action="store_true",
                          help="exit 1 when the daemon is unreachable or a relay is unhealthy")
    p_status.add_argument("-v", "--verbose", action="store_true",
                          help="also show each worker's last stderr line (diagnostics)")
    p_status.set_defaults(func=cmd_status)

    p_health = sub.add_parser("health", help="one call: per-plane health for a cron to read")
    p_health.add_argument("--socket", default=None, help="daemon socket override")
    p_health.add_argument("--quiet-minutes", type=int, default=30,
                          help="silence window before a relay reads quiet (default 30)")
    p_health.add_argument("--pending-stale-minutes", type=int, default=10,
                          help="undelivered backlog older than this is degraded (default 10)")
    p_health.add_argument("--json", action="store_true", help="the whole frame, for a cron")
    p_health.add_argument("--check", action="store_true",
                          help="exit 1 when degraded or when the daemon is not running")
    p_health.set_defaults(func=cmd_health)

    p_serve = sub.add_parser("serve", help="run the gateway daemon (socket + relays)")
    p_serve.add_argument("--socket", default=None, help="socket path override")
    p_serve.set_defaults(func=cmd_serve)

    p_recent = sub.add_parser("recent", help="recent messages")
    p_recent.add_argument("-n", "--number", type=int, default=20)
    p_recent.set_defaults(func=cmd_recent)

    p_drops = sub.add_parser("drops", help="messages policy refused, with reason and time")
    p_drops.add_argument("--plane", default=None,
                         help="nip17, nip01, nip04, marmot or concord")
    p_drops.add_argument("--reason", default=None,
                         help="not_allowlisted, rate_limited or digest_quota")
    p_drops.add_argument("--since", type=int, default=None, help="epoch seconds")
    p_drops.add_argument("--since-hours", type=int, default=None,
                         help="only drops received in the last N hours")
    p_drops.add_argument("-n", "--limit", type=int, default=50)
    p_drops.add_argument("--json", action="store_true")
    p_drops.set_defaults(func=cmd_drops)

    p_media = sub.add_parser("media", help="fetch a Blossom blob an inbound message references")
    p_media.add_argument("ref", help="the blob's sha256, or the imeta url")
    p_media.add_argument("--json", action="store_true")
    p_media.set_defaults(func=cmd_media)

    p_projects = sub.add_parser("projects", help="channel -> checkout mapping (concord.projects)")
    p_projects.add_argument("--json", action="store_true")
    p_projects.add_argument("--check", action="store_true",
                            help="exit 1 unless every declared checkout, project and board "
                                 "is there")
    p_projects.set_defaults(func=cmd_projects)

    p_profiles = sub.add_parser(
        "profiles", help="which Hermes profile each plane and channel runs as")
    p_profiles.add_argument("--check", action="store_true",
                            help="exit 1 unless every route names a profile Hermes can run a "
                                 "turn as")
    p_profiles.add_argument("--json", action="store_true")
    p_profiles.set_defaults(func=cmd_profiles)

    p_route = sub.add_parser(
        "route", help="which profile (and, when configured, model) a turn uses")
    p_route.add_argument("conversation", nargs="?", default="",
                         help="a conversation: concord:<community>:<channel>, a peer hex/npub "
                              "or a marmot group id; omit for the whole table")
    p_route.add_argument("--plane", default="", choices=("", *PLANES),
                         help="ask for a plane directly, when the store cannot say")
    p_route.add_argument("--json", action="store_true")
    p_route.set_defaults(func=cmd_route)

    p_control = sub.add_parser("control", help="where confirmations go, and who is trusted")
    p_control.add_argument("--json", action="store_true")
    p_control.set_defaults(func=cmd_control)

    p_concord = sub.add_parser(
        "concord", help="concord invites: what is held, and accepting one")
    p_concord.add_argument("invite", nargs="?", default=None,
                           help="invite id (or community id) to accept; omit to list")
    p_concord.add_argument("--held", action="store_true",
                           help="list only the invites waiting for a human")
    p_concord.add_argument("-n", "--limit", type=int, default=20)
    p_concord.add_argument("--json", action="store_true")
    p_concord.set_defaults(func=cmd_concord)

    sub.add_parser("replay-skip",
                   help="record a handover: the pending backlog is already answered"
                   ).set_defaults(func=cmd_replay_skip)

    p_policy = sub.add_parser("policy-test", help="ask the policy engine what it would do")
    p_policy.add_argument("--sender", required=True)
    p_policy.add_argument("--kind", type=int, default=14)
    p_policy.add_argument("--mention", action="store_true")
    p_policy.add_argument("--dm", action="store_true")
    p_policy.add_argument("--event-id", default=None)
    p_policy.add_argument("--text", default=None,
                          help="message text, so the echo rule can be exercised")
    p_policy.add_argument("--bot", action="store_true",
                          help="judge the sender as a bot peer (kind-0 bot: true)")
    p_policy.set_defaults(func=cmd_policy_test)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
