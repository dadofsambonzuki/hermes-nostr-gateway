#!/usr/bin/env python3
"""NIP-17 (private DM) wrapper for the Hermes bridge adapter — phase 0 spike.

Two jobs, deliberately small:

  listen  subscribe to kind-1059 gift wraps addressed to our npub, unwrap them,
          and hand NIP-17 kind-14 chat messages to the Hermes bridge contract
          (one JSON file per message in the inbox directory).
  send    build a kind-14 rumor, seal + gift-wrap it to the peer AND to
          ourselves (NIP-17 self-copy), publish to our relays.

Crypto and relay I/O are delegated to `nak` (NIP-44 + NIP-59 + relay streaming),
so this box needs no new Rust/Node dependencies. Every nak call gets the key
explicitly: nak silently falls back to the *machine default key* otherwise, and
a signature by the wrong identity is refused by the relay with no obvious hint.

Phase 0 scope: text only, allowlist policy, dedupe by rumor id, persisted
catch-up cursor, at-least-once inbox writes deduped on read.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time

BRIDGE_NAME = "nostr-nip17"
# Gift-wrap timestamps are deliberately randomised up to TWO DAYS into the past
# (NIP-59, to defeat timing analysis), so a "since" window narrower than that
# silently hides messages that were published seconds ago. Our lookback must
# exceed the randomisation window; 3 days is the margin.
LOOKBACK_DEFAULT = 259200  # seconds of history replayed on start
CURSOR_MAX_FUTURE_SKEW = 300  # refuse to trust a timestamp further ahead than this


def nak_binary() -> str:
    found = shutil.which("nak")
    if found:
        return found
    fallback = pathlib.Path.home() / ".local/bin/nak"
    if fallback.exists():
        return str(fallback)
    sys.exit("nak not found on PATH (see the `nostr` skill for installation)")


def load_config(path: pathlib.Path) -> dict:
    cfg = json.loads(path.read_text())
    cfg.setdefault("dm_policy", "allowlist")
    cfg.setdefault("allow_from", [])
    cfg.setdefault("lookback_seconds", LOOKBACK_DEFAULT)
    return cfg


def secret_hex(cfg: dict) -> str:
    """Read the identity secret. Never logged, never printed."""
    env = os.environ.get("NIP17_SECRET_HEX")
    if env:
        return env.strip()
    src = cfg.get("identity", {}).get("secret_file")
    if not src:
        sys.exit("config.identity.secret_file missing (or set NIP17_SECRET_HEX)")
    data = json.loads(pathlib.Path(os.path.expanduser(src)).read_text())
    for key in ("secret_key_hex", "secret_key", "nsec"):
        if data.get(key):
            return str(data[key]).strip()
    sys.exit(f"no secret key found in {src}")


def our_pubkey(nak: str, secret: str) -> str:
    out = subprocess.run([nak, "key", "public", secret], capture_output=True, text=True, check=True)
    return out.stdout.strip().splitlines()[-1].strip()


def nak_run(nak: str, args: list[str], stdin: str | None = None, secret: str | None = None) -> str:
    env = dict(os.environ)
    if secret:
        env["NOSTR_SECRET_KEY"] = secret  # explicit: no machine-default fallback
    proc = subprocess.run(
        [nak, *args],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"nak {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    # nak writes status lines — including "publishing to ... success." — to STDERR,
    # and only the event JSON to stdout. Return both: callers that parse JSON
    # filter on lines starting with "{", callers that check for success need stderr.
    return proc.stdout + proc.stderr


def json_lines(text: str) -> list[dict]:
    """nak mixes status lines with event JSON; keep only the JSON objects."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


class State:
    """Dedupe + catch-up cursor. The cursor is clamped so one hostile or skewed
    created_at cannot park the subscription in the future and silently stop
    message reception (the failure mode that bites the MLS clients)."""

    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data = {"cursor": 0, "seen": []}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text()))
            except json.JSONDecodeError:
                pass
        self.seen = set(self.data.get("seen", []))

    def since(self, lookback: int) -> int:
        cursor = int(self.data.get("cursor") or 0)
        now = int(time.time())
        if cursor > now + CURSOR_MAX_FUTURE_SKEW:
            return max(0, now - lookback)  # corrupt cursor: replay instead of stalling
        return max(0, cursor - lookback) if cursor else max(0, now - lookback)

    def note(self, rumor_id: str, created_at: int) -> bool:
        """Record a message. False when we have already handled it."""
        if rumor_id in self.seen:
            return False
        self.seen.add(rumor_id)
        now = int(time.time())
        candidate = min(int(created_at or now), now + CURSOR_MAX_FUTURE_SKEW)
        self.data["cursor"] = max(int(self.data.get("cursor") or 0), candidate)
        return True

    def save(self) -> None:
        self.data["seen"] = sorted(self.seen)[-5000:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data))
        os.replace(tmp, self.path)


def allowlisted(cfg: dict, sender_hex: str) -> bool:
    policy = cfg.get("dm_policy", "allowlist")
    if policy == "open":
        return True
    if policy == "disabled":
        return False
    allowed = {h.lower() for h in cfg.get("allow_from", [])}
    return sender_hex.lower() in allowed


def write_inbox(inbox_dir: pathlib.Path, rumor: dict, sender_hex: str) -> pathlib.Path:
    """Bridge-adapter inbox contract: one JSON file per inbound message."""
    msg = {
        "bridge": BRIDGE_NAME,
        "id": rumor["id"],
        "sender": sender_hex,
        "sender_name": None,
        "text": rumor.get("content", ""),
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rumor.get("created_at") or time.time())),
        "chat": {"id": sender_hex, "type": "dm", "name": None},
        "attachments": [],
    }
    inbox_dir.mkdir(parents=True, exist_ok=True)
    path = inbox_dir / f"{msg['timestamp'].replace(':', '')}-{rumor['id'][:16]}.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(msg, indent=1))
    os.replace(tmp, path)  # atomic: the adapter polls this directory
    return path


def cmd_send(cfg: dict, args: argparse.Namespace) -> int:
    nak = nak_binary()
    secret = secret_hex(cfg)
    me = our_pubkey(nak, secret)
    peer = args.to.strip().lower()
    if peer == me:
        sys.exit("refusing to DM our own npub (self-copies are handled automatically)")
    # Signed event -> nak strips it back to an unsigned rumor -> seal -> wrap.
    # --use-*-identity-key is mandatory: without it nak silently encrypts to a
    # NIP-4E *decoupled* key when it has one stored, and the wrap's `p` tag then
    # stops being the recipient's identity pubkey — so a standard NIP-17 client
    # (Amethyst/Damus/0xChat subscribe on #p = their identity key) never sees it.
    rumor = nak_run(nak, ["event", "-k", "14", "-c", args.text, "-p", peer], secret=secret)
    identity_flags = ["--use-our-identity-key", "--use-their-identity-key"]
    wraps = {
        "peer": nak_run(nak, ["gift", "wrap", "-p", peer, *identity_flags], stdin=rumor, secret=secret),
        "self": nak_run(nak, ["gift", "wrap", "-p", me, *identity_flags], stdin=rumor, secret=secret),
    }
    relays = cfg["relays"]["publish"]
    results: dict[str, dict] = {}
    for label, wrap in wraps.items():
        event = json_lines(wrap)[-1]
        expected = peer if label == "peer" else me
        addressed = [t[1] for t in event.get("tags", []) if t and t[0] == "p"]
        if addressed != [expected]:
            sys.exit(f"{label} wrap is addressed to {addressed} but should be {[expected]} — refusing to publish")
        per_relay = {}
        for relay in relays:
            try:
                out = nak_run(nak, ["event", "--auth", relay], stdin=json.dumps(event), secret=secret)
                per_relay[relay] = "success" in out.lower()
                if not per_relay[relay]:
                    print(f"    {relay}: {out.strip().splitlines()[-1][:120]}", file=sys.stderr)
            except RuntimeError as exc:
                per_relay[relay] = False
                print(f"    {relay}: {exc}", file=sys.stderr)
        results[label] = {
            "event_id": event["id"],
            "created_at": event["created_at"],
            "to": expected,
            "relays": per_relay,
        }
        ok = any(per_relay.values())
        published_to = [r for r, v in per_relay.items() if v]
        print(f"  {label:5} wrap {event['id'][:16]}… -> {'success' if ok else 'FAILED'} "
              f"({', '.join(published_to) or 'no relay'})")
    ok = all(any(r["relays"].values()) for r in results.values())
    print(f"sent to {peer[:12]}… ; verify with:\n"
          f"  nak req -k 1059 -p {peer} {' '.join(relays)} | head -1 | "
          f"nak gift unwrap --sec <peer key>")
    return 0 if ok else 1


def unwrap(nak: str, secret: str, wrap: dict) -> dict | None:
    try:
        out = nak_run(nak, ["gift", "unwrap"], stdin=json.dumps(wrap), secret=secret)
    except RuntimeError:
        return None  # not for us / undecryptable
    events = json_lines(out)
    return events[-1] if events else None


def cmd_listen(cfg: dict, args: argparse.Namespace) -> int:
    nak = nak_binary()
    secret = secret_hex(cfg)
    me = our_pubkey(nak, secret)
    state = State(pathlib.Path(os.path.expanduser(args.state)))
    inbox = pathlib.Path(os.path.expanduser(args.inbox))
    since = state.since(int(cfg.get("lookback_seconds", LOOKBACK_DEFAULT)))
    relays = cfg["relays"]["read"]
    print(f"listening as {me[:12]}… on {', '.join(relays)} (since {since}, "
          f"policy {cfg.get('dm_policy')}, {len(cfg.get('allow_from', []))} allowed)")
    cmd = [nak, "req", "--stream", "--auth", "-k", "1059", "-p", me, "-s", str(since), *relays]
    env = dict(os.environ, NOSTR_SECRET_KEY=secret)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
    stats = {"wraps": 0, "own": 0, "dropped": 0, "delivered": 0, "dup": 0}
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if not line.startswith("{"):
                print(line.rstrip())
                continue
            try:
                wrap = json.loads(line)
            except json.JSONDecodeError:
                continue
            if wrap.get("kind") != 1059:
                continue
            stats["wraps"] += 1
            rumor = unwrap(nak, secret, wrap)
            if not rumor:
                continue
            sender = str(rumor.get("pubkey") or "").lower()
            text = (rumor.get("content") or "").strip()
            if sender == me:
                stats["own"] += 1
                print(f"  [self-copy] {text[:70]}")
                continue
            if not state.note(rumor["id"], rumor.get("created_at") or 0):
                stats["dup"] += 1
                continue
            if not allowlisted(cfg, sender):
                stats["dropped"] += 1
                print(f"  [blocked] {sender[:12]}… {text[:60]}")
                continue
            path = write_inbox(inbox, rumor, sender)
            stats["delivered"] += 1
            print(f"  [inbox] {sender[:12]}… {text[:70]} -> {path.name}")
            state.save()
    except KeyboardInterrupt:
        pass
    finally:
        proc.terminate()
        state.save()
        print(f"stats: {json.dumps(stats)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="NIP-17 wrapper (phase 0 spike)")
    # Root-level flags must precede the subcommand (argparse); NIP17_CONFIG lets a
    # service unit point at its config without argument-order games.
    ap.add_argument("--config",
                    default=os.environ.get("NIP17_CONFIG") or str(pathlib.Path(__file__).with_name("config.json")))
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_send = sub.add_parser("send", help="DM a peer npub/hex")
    p_send.add_argument("text")
    p_send.add_argument("--to", required=True, help="peer hex pubkey")
    p_listen = sub.add_parser("listen", help="subscribe and deliver inbound DMs")
    # Belong to the subcommand, not the root parser: argparse only accepts root
    # options *before* the subcommand, which makes `listen --state …` fail.
    p_listen.add_argument("--state", default="~/.nip17-wrapper/state.json")
    p_listen.add_argument("--inbox", default="~/.nip17-wrapper/inbox")
    args = ap.parse_args()
    cfg = load_config(pathlib.Path(os.path.expanduser(args.config)))
    if args.cmd == "send":
        return cmd_send(cfg, args)
    return cmd_listen(cfg, args)


if __name__ == "__main__":
    sys.exit(main())
