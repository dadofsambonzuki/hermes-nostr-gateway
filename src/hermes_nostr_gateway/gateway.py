"""The gateway: relays in, policy applied, socket out. One process, one identity."""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from .crypto import SdkCrypto
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


def default_marmot_home() -> Path:
    return Path(os.environ.get("MARMOT_HOME") or Path.home() / ".marmot-agents/hermes")


def default_socket_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        return Path(runtime) / "hermes-nostr-gateway.sock"
    return Path.home() / ".local/state/hermes-nostr-gateway/gateway.sock"


class Gateway:
    def __init__(self, cfg: dict[str, Any], *, socket_path: str | Path | None = None):
        from .cli import normalise_groups, secret_from

        self.cfg = cfg
        self._secret = secret_from(cfg)
        self.crypto = SdkCrypto(self._secret)
        self.me = self.crypto.our_pubkey()
        store_path = Path(os.path.expanduser(cfg.get("store", "gateway.db")))
        store_path.parent.mkdir(parents=True, exist_ok=True)
        self.store = Store(store_path)
        self.policy = Policy({
            "groups": normalise_groups(cfg),
            "policies": cfg.get("policies"),
            "defaults": cfg.get("defaults"),
        })
        self.concord = ConcordPlane(self.store)
        self.plane = Nip17Plane(self.crypto, self.store, self.policy,
                                on_invite=self.concord.handle_invite)
        self.nip01 = Nip01Plane(self.store, self.policy, self.me, feed_rules(cfg.get("feeds")))
        # The Marmot plane proxies wn-agent rather than speaking MLS, so it is
        # attached only when that daemon is actually reachable (see _setup_marmot).
        self.marmot: MarmotPlane | None = None
        self.marmot_resolver: MarmotResolver | None = None
        self.marmot_worker: MarmotWorker | None = None
        self.marmot_error: str | None = None
        self._setup_marmot(cfg.get("marmot") or {})
        self.follows: tuple[str, ...] = self._load_follows()
        self.relays_read = list(cfg["relays"]["read"])
        self.relays_publish = list(cfg["relays"].get("publish", []))
        self.socket_path = Path(socket_path) if socket_path else Path(
            os.path.expanduser(cfg.get("socket") or default_socket_path())
        )
        self.server = SocketServer(self.socket_path, self)
        self.workers: list[RelayWorker] = []
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()

    def _setup_marmot(self, cfg: dict[str, Any]) -> None:
        """Attach the Marmot proxy iff wn-agent's control socket is actually there.

        A missing socket is "not configured" and stays quiet. A socket that exists
        but cannot be used (no account file, two of them) disables only this plane
        and records why in `marmot_error` — the NIP-17 planes must not go down
        because a second daemon is mid-install, and a silent disable would look
        exactly like a working plane with nothing to say.
        """
        if cfg.get("enabled") is False:
            log.info("marmot plane disabled by config")
            return
        home = Path(os.path.expanduser(str(cfg.get("home") or default_marmot_home())))
        socket = Path(os.path.expanduser(str(
            cfg.get("socket") or cfg.get("socket_path") or home / "dev/wn-agent.sock"
        )))
        if not socket.exists():
            log.info("marmot plane idle: no control socket at %s", socket)
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
        self.marmot_worker = MarmotWorker(plane, group_id_hex=cfg.get("group"))
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
            restricted = relay.rstrip("/").endswith("/chat") or "whitenoise" in relay
            kinds = (1059, 4) if restricted else (1059, 4)
            worker = RelayWorker(relay, self.me, kinds=kinds, p_tag=self.me,
                                 secret=self._secret)
            self.workers.append(worker)
            self._tasks.append(asyncio.create_task(self._guarded(worker)))
            # Feeds: kind 1 from people WE FOLLOW, never the global firehose.
            authors = tuple(sorted(set(self.follows) | set(self.nip01.authors)))
            if authors and not restricted:
                feed = RelayWorker(relay, self.me, kinds=(1,), authors=authors,
                                   secret=self._secret)
                self.workers.append(feed)
                self._tasks.append(asyncio.create_task(self._guarded(feed)))
            # Mentions of us by strangers: a small #p filter, separate from follows.
            if not restricted:
                mentions = RelayWorker(relay, self.me, kinds=(1,), p_tag=self.me,
                                       secret=self._secret)
                self.workers.append(mentions)
                self._tasks.append(asyncio.create_task(self._guarded(mentions)))
        self._tasks.append(asyncio.create_task(self._retry_loop()))
        self._tasks.append(asyncio.create_task(self._digest_loop()))
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

    async def _guarded(self, worker: RelayWorker) -> None:
        try:
            await worker.run(self._on_event)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one relay must not take the daemon down
            log.exception("relay worker %s died", worker.relay)
        finally:
            self.store.note_relay(worker.relay, error=worker.last_error)

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
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.server.stop()
        self.store.close()

    async def run_forever(self) -> None:
        await self.start()
        await self._stopping.wait()

    # -- inbound --------------------------------------------------------
    def _on_event(self, relay: str, event: dict) -> None:
        kind = event.get("kind")
        if kind == 1059:
            msg = self.plane.handle(relay, event)
        elif kind == 4:
            msg = self._handle_nip04(relay, event)
        else:
            msg = self.nip01.handle(relay, event)
        if msg is None:
            return
        self.store.note_relay(relay, events=1)
        asyncio.create_task(self.server.broadcast(msg.to_frame()))

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
        asyncio.create_task(self.server.broadcast(msg.to_frame()))


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
                                      event_id=str(event.get("id") or ""))
        if decision.duplicate:
            return None
        msg = Inbound(
            id=str(event.get("id") or ""), wrapper_id=str(event.get("id") or ""),
            sender=sender, kind=4, created_at=int(event.get("created_at") or 0),
            content=text, conversation=sender, reply_to=None, plane="nip04",
            reach=decision.reach, authority=decision.authority,
            rule=decision.rule, why=decision.why,
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
                detail = f": {self.marmot_error}" if self.marmot_error else ""
                return {"type": "delivery", "id": cid, "ok": False,
                        "error": f"marmot plane is not enabled{detail}"}
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
        for label, event in wraps.items():
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
        # Retry a few at a time: a burst of replays trips our own relay's connection
        # limiter and turns one failure into a hundred.
        pending = self.store.pending_wraps()[:5]
        if not pending:
            return 0
        loop = asyncio.get_running_loop()
        sent = 0
        for row in pending:
            event = _json.loads(row["event"])
            ok, error = await loop.run_in_executor(
                None, lambda: list(publish(event, [row["relay"]], secret=self._secret).values())[0]
            )
            self.store.mark_wrap(row["id"], row["relay"], ok=ok, error=error)
            sent += int(ok)
            import asyncio as _asyncio
            await _asyncio.sleep(1.5)  # stay under the relay's connection limiter
        if sent:
            log.info("retried %d wrap(s)", sent)
        return sent


    # -- digests --------------------------------------------------------
    async def _digest_loop(self) -> None:
        """Batch digest-class messages into one frame instead of trickling them.

        Emitted as an ordinary message frame (plane 'digest'), so every adapter that
        already handles messages gets digests without a protocol change.
        """
        cfg = self.cfg.get("digest") or {}
        interval = int(cfg.get("interval_minutes", 15)) * 60
        max_items = int(cfg.get("max_items", 20))
        while not self._stopping.is_set():
            try:
                rows = self.store.pending_digest(interval, max_items)
                # Never mark a digest delivered when nobody is listening: that would
                # silently discard the batch.
                if rows and self.server.has_subscribers:
                    frame = self._digest_frame(rows)
                    await self.server.broadcast(frame)
                    self.store.mark_delivered([r["id"] for r in rows])
            except Exception:  # noqa: BLE001 - a bad batch must not kill the loop
                log.exception("digest pass failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

    def _digest_frame(self, rows: list[dict]) -> dict:
        import hashlib as _hash
        import time as _time
        lines = []
        for row in rows[:20]:
            who = (row.get("sender") or "")[:10]
            text = (row.get("content") or "").replace("\n", " ")[:140]
            lines.append(f"- {who}… ({row.get('rule') or 'feed'}): {text}")
        stamp = int(_time.time())
        digest_id = "digest-" + _hash.sha256(f"{stamp}:{len(rows)}".encode()).hexdigest()[:16]
        return {
            "type": "message",
            "plane": "digest",
            "id": digest_id,
            "sender": "digest",
            "conversation": "digest",
            "kind": 1,
            "created_at": stamp,
            "reply_to": None,
            "reach": "digest",
            "authority": "none",
            "why": f"{len(rows)} items awaited the digest window",
            "content": (f"{len(rows)} item(s) since the last digest:\n" + "\n".join(lines)),
        }

    # -- socket ops -----------------------------------------------------
    async def status(self) -> dict[str, Any]:
        stats = self.store.stats()
        return {
            "identity": self.me,
            "socket": str(self.socket_path),
            "relays_read": self.relays_read,
            "planes": ["nip17", "nip01", "nip04", "concord"]
                      + (["marmot"] if self.marmot else []),
            "relays_publish": self.relays_publish,
            "rumors": stats["total"],
            "pending": stats["pending"],
            "invites": stats["invites"],
            "relay_stats": stats["relays"],
            # Instrument the plane rather than inferring it from `ps`: a worker that
            # reconnects every 60s and one that died at startup look identical from
            # outside, and only the counters tell them apart.
            "marmot": (
                {"worker": self.marmot_worker.stats(),
                 "conversations": self.marmot_resolver.conversations()}
                if self.marmot and self.marmot_worker and self.marmot_resolver
                else {"enabled": False, "error": self.marmot_error}
            ),
            "workers": [
                {"relay": w.relay, "kinds": list(w.kinds), "p_tag": bool(w.p_tag),
                 "authors": len(w.authors), "events": w.events, "last_error": w.last_error}
                for w in self.workers
            ],
            "uptime_s": None,
        }

    def history(self, limit: int) -> dict[str, Any]:
        return {"messages": self.store.recent(limit)}

    def peers(self) -> dict[str, Any]:
        seen: dict[str, int] = {}
        for row in self.store.recent(500):
            if row["authority"] != "self":
                seen[row["sender"]] = max(seen.get(row["sender"], 0), row["created_at"])
        return {"peers": [{"pubkey": k, "last_seen": v} for k, v in
                          sorted(seen.items(), key=lambda kv: kv[1], reverse=True)]}

    def replay_pending(self, limit: int = 20) -> list[dict[str, Any]]:
        frames = []
        for row in self.store.pending_immediate(limit):
            frames.append({
                "type": "message", "plane": row["plane"], "id": row["id"],
                "sender": row["sender"], "conversation": row["conversation"],
                "kind": row["kind"], "created_at": row["created_at"],
                "content": row["content"], "reply_to": row["reply_to"],
                "reach": row["reach"], "authority": row["authority"], "why": row["why"],
            })
        return frames

    def ack(self, ids: list[str]) -> dict[str, Any]:
        self.store.mark_delivered(ids)
        return {"acked": ids}

    # -- Marmot reads (over the same socket, same normalised shapes) ------
    async def marmot_group(self, group: str) -> dict[str, Any]:
        if self.marmot is None or self.marmot_resolver is None:
            return {"type": "error", "op": "marmot.group",
                    "error": self.marmot_error or "marmot plane not enabled"}
        try:
            info = await self.marmot_resolver.resolve(group)
        except MarmotError as exc:
            return {"type": "error", "op": "marmot.group", "code": exc.code,
                    "error": str(exc)[:300]}
        return {"type": "marmot_group", **info}

    async def marmot_timeline(self, group: str, limit: int = 20) -> dict[str, Any]:
        if self.marmot is None or self.marmot_resolver is None:
            return {"type": "error", "op": "marmot.timeline",
                    "error": self.marmot_error or "marmot plane not enabled"}
        try:
            await self.marmot_resolver.resolve(group)
            page = await self.marmot.timeline(group, limit=limit)
        except MarmotError as exc:
            return {"type": "error", "op": "marmot.timeline", "code": exc.code,
                    "error": str(exc)[:300]}
        return {"type": "marmot_timeline", **page}

    def mark_read(self, ids: list[str]) -> dict[str, Any]:
        return self.ack(ids)
