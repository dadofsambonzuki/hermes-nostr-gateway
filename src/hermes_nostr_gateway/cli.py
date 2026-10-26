"""`hng` — the dogfood CLI. Same core the socket server will expose."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from .bots import BotRegistry
from .crypto import SdkCrypto
from .health import (
    BAD,
    human_age,
    relay_verdicts,
    render,
    reports_event_times,
    running_but_unconfigured,
    stale_rows,
)
from .media import MediaError, media_config, resolve_attachment
from .planes.nip17 import Nip17Plane
from .policy import Policy
from .publisher import publish
from .relay import OneShot, RelayWorker
from .store import Store

DEFAULT_CONFIG = Path("config.yaml")


def load_config(path: str | None = None) -> dict[str, Any]:
    candidates = [path, os.environ.get("HNG_CONFIG"), str(DEFAULT_CONFIG),
                  str(Path.home() / ".config/hermes-nostr-gateway/config.yaml")]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            with open(candidate) as fh:
                return yaml.safe_load(fh) or {}
    raise SystemExit("no config found: pass --config or create config.yaml")


def secret_from(cfg: dict[str, Any]) -> str:
    if cfg.get("identity", {}).get("secret_env"):
        return os.environ[cfg["identity"]["secret_env"]]
    path = Path(os.path.expanduser(cfg["identity"]["secret_file"]))
    data = json.loads(path.read_text())
    return data.get("secret_key_hex") or data["secret_key"]


def _decode_npub(npub: str) -> str:
    import subprocess
    out = subprocess.run(["nak", "decode", npub], capture_output=True, text=True).stdout.strip()
    return out or npub


def normalise_groups(cfg: dict[str, Any]) -> dict[str, Any]:
    """Groups are authored as npubs but events carry hex, so normalise once, here.

    Keeping this in the CLI (not in Policy) leaves the policy engine dependency-free
    and unit-testable without nak.
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
    return crypto, store, policy, Nip17Plane(crypto, store, policy)


def to_hex(who: str) -> str:
    if who.startswith("npub"):
        import subprocess
        return subprocess.run(["nak", "decode", who], capture_output=True, text=True).stdout.strip()
    return who


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
    # `relays.read` may pin a per-relay kind set ({url, kinds}); listen only speaks
    # NIP-17, so the pin is respected but legacy kind 4 is not a listen default.
    relays, relay_kinds = relay_read_plan(cfg)

    if args.once:
        since = None
        import time
        if args.since_hours:
            since = int(time.time()) - args.since_hours * 3600

        async def run_once() -> int:
            fetch = OneShot(plane.me, limit=args.limit, since=since)
            total = 0
            for relay in relays:
                try:
                    events = await fetch.fetch(relay)
                except Exception as exc:  # noqa: BLE001 - report, do not crash
                    store.note_relay(relay, error=str(exc)[:200])
                    continue
                store.note_relay(relay, events=len(events))
                for event in events:
                    msg = plane.handle(relay, event)
                    if msg:
                        print_message(msg, args.json)
                        total += 1
            return total

        count = asyncio.run(run_once())
        print(f"-- {count} new message(s) across {len(relays)} relays", file=sys.stderr)
        return 0

    async def stream() -> None:
        secret = secret_from(cfg)
        # `-p me` is not optional: a kind-1059 subscription without it is a firehose of
        # every wrap on the relay, and the identity secret must reach the child so NIP-42
        # authenticates as us rather than as nak's machine key.
        workers = [RelayWorker(r, plane.me, kinds=relay_kinds.get(r) or (1059,),
                               p_tag=plane.me, secret=secret) for r in relays]
        tasks = [asyncio.create_task(w.run(lambda relay, event: found(plane, relay, event, args)))
                 for w in workers]
        try:
            await asyncio.gather(*tasks)
        except KeyboardInterrupt:
            for w in workers:
                w.stop()
            for t in tasks:
                t.cancel()

    try:
        asyncio.run(stream())
    except KeyboardInterrupt:
        pass
    return 0


def recipient_relays(cfg: dict[str, Any], peer: str) -> list[str]:
    import subprocess
    relays = list(cfg["relays"].get("publish", []))
    try:
        out = subprocess.run(
            ["nak", "req", "-k", "10050", "-a", peer, "-l", "1", *relays[:2]],
            capture_output=True, text=True, timeout=25,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            event = json.loads(line)
            for tag in event.get("tags", []):
                if tag and tag[0] == "relay" and tag[1] not in relays:
                    relays.append(tag[1])
            break
    except Exception:  # noqa: BLE001 - absence of 10050 is not fatal
        pass
    return relays[:6]


def cmd_send(args) -> int:
    cfg = load_config(args.config)
    crypto, store, policy, plane = build(cfg)
    secret = secret_from(cfg)
    peer = to_hex(args.to)
    rumor = crypto.build_rumor(args.text, peer, reply_to=args.reply_to)
    wraps = {"peer": crypto.wrap(rumor, peer), "self": crypto.wrap(rumor, plane.me)}
    relays = recipient_relays(cfg, peer)
    ok_peer = False
    for label, wrap in wraps.items():
        # Durable before attempted, retried from the stored event rather than re-wrapped,
        # and published through the same function the daemon uses so NIP-42 signs as us
        # instead of as nak's machine-default key (spec §3, §7).
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
            "invites": stats["invites"],
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
    print(f"invites:  {stats['invites']} concord invite(s)")
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
        print(f"daemon:   up ({socket_path}, {len(frame.get('workers') or [])} worker(s))")
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

    from .gateway import Gateway

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = load_config(args.config)
    gw = Gateway(cfg, socket_path=args.socket)

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

    p_send = sub.add_parser("send", help="send a NIP-17 DM")
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

    p_serve = sub.add_parser("serve", help="run the gateway daemon (socket + relays)")
    p_serve.add_argument("--socket", default=None, help="socket path override")
    p_serve.set_defaults(func=cmd_serve)

    p_recent = sub.add_parser("recent", help="recent messages")
    p_recent.add_argument("-n", "--number", type=int, default=20)
    p_recent.set_defaults(func=cmd_recent)

    p_media = sub.add_parser("media", help="fetch a Blossom blob an inbound message references")
    p_media.add_argument("ref", help="the blob's sha256, or the imeta url")
    p_media.add_argument("--json", action="store_true")
    p_media.set_defaults(func=cmd_media)

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
