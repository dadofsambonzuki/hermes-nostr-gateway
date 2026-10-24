# Operating notes

What the daemon is, where its socket lives, and the two host-side gates that make
`relay.day.ag` look like it is refusing us. Checked against the live host on 2026-10-24.

This was written for `~/Projects/infra/AGENTS.md`; it lives here because a scheduled run
cannot write a file named `AGENTS.md` (protected instruction file, human approval only).
Copy the "Service and socket" and "Why `relay.day.ag` says no" sections across unchanged.

## Service and socket

`hermes-nostr-gateway.service` — `User=ubuntu`, `Restart=always`, enabled at boot,
`ExecStart=.venv/bin/hng serve` from this checkout. There is **no network listener**: the
agent talks NDJSON over a Unix socket.

| | |
|---|---|
| Socket | `~/.local/state/hermes-nostr-gateway/gateway.sock`, mode **0600** |
| State | `gateway.db` (SQLite, WAL) in the same directory |
| Client | `integrations/hermes-plugin/`; `NOSTR_GATEWAY_SOCKET` overrides the path |
| Identity | Holly's agent key `740fcd90…`; secret `~/.marmot-agents/hermes/accounts/hermes-agent/secret.json`, shared with the Marmot connector |
| Stop bound | drop-in `/etc/systemd/system/hermes-nostr-gateway.service.d/stop-timeout.conf`: `TimeoutStopSec=15` |

- **`XDG_RUNTIME_DIR` silently moves the socket.** `default_socket_path()` prefers
  `$XDG_RUNTIME_DIR/hermes-nostr-gateway.sock` and only falls back to `~/.local/state/…` when
  it is unset. The unit has no `XDG_RUNTIME_DIR`, so the service uses the documented path; a
  hand-run `hng serve` from a login shell puts the socket under `/run/user/1000/` instead, and
  the plugin reports that the socket "did not accept a subscription" while the process looks
  healthy. `config.yaml` sets `socket:` explicitly — keep it that way.
- **The unit's sandbox decides which paths are writable.** `ProtectSystem=strict` +
  `ProtectHome=read-only` with exactly one writable path,
  `ReadWritePaths=/home/ubuntu/.local/state/hermes-nostr-gateway`. A new store, socket or
  media cache anywhere else in `$HOME` fails as read-only at runtime; add it to the unit,
  `systemctl daemon-reload`, restart. `PATH` must also keep `~/.local/bin`, because every relay
  worker is a `nak` child.
- **`TimeoutStopSec=15` bounds a stop that does not finish.** The restart at 12:31 on
  2026-10-24 logged `State 'stop-sigterm' timed out. Killing.` — the drop-in converted a
  90-second wait into a 15-second one ending in SIGKILL. Treat `systemctl restart` as hard:
  in-flight publishes are lost (the wrap retry table covers them), and the next start unlinks
  the stale socket itself. If every restart starts ending this way, the SIGTERM path
  (`cmd_serve` → `Gateway.stop`) is where to look.
- **The stop-timeout drop-in is not in `deploy/`.** `deploy/hermes-nostr-gateway.service` is
  the base unit only; a rebuild from the repo loses the 15 s bound unless the drop-in is
  re-created by hand. Record that here rather than assuming the repo is the whole unit.
- **The wheel is pinned for a reason.** A reinstall must use `requirements.txt` /
  `pyproject.toml`; a plain `pip install nostr-sdk` resolves the PyPI 0.45.1 and silently
  drops `nip44_conversation_key`, after which the first Concord derivation raises
  `CryptoError` naming the patched wheel.

Day-to-day:

```bash
systemctl status hermes-nostr-gateway
journalctl -u hermes-nostr-gateway -n 50
cd ~/Projects/hermes-nostr-gateway && .venv/bin/hng status      # per-relay counters
printf '{"op":"ping"}\n' | nc -U ~/.local/state/hermes-nostr-gateway/gateway.sock
```

A healthy start logs `socket listening at …` then `gateway up: <pubkey> on N relays`. The
socket answers one JSON object per line; `subscribe` is the only op that turns the connection
into a delivery stream.

## Why `relay.day.ag` says no

`relay.day.ag` is one HAVEN process on `3355`; Caddy path-routes `/` (outbox + Blossom),
`/private`, `/chat` and `/inbox` to it. `/chat` is gated, and it is the one the gateway's DM
worker reads.

- **Web-of-trust gate, depth 3** — owner → follows → their follows
  (`CHAT_RELAY_WOT_DEPTH=3`), refreshed every 24 hours
  (`CHAT_RELAY_WOT_REFRESH_INTERVAL_HOURS=24`). The model is in memory and rebuilt from
  `relays_import.json` at boot (about 4 s; 36k pubkeys from 534 relays on the last start).
  NIP-42 auth is checked **against the WoT**; `/opt/haven/whitelisted_npubs.json` is a
  separate list and is not a substitute. A rejected subscription closes with **`restricted`**.
- **The `nak` machine-key trap.** `nak` with no key material signs NIP-42 as its machine
  default — `nak key default` → `3429e7d6…`, a key nobody follows and that is not whitelisted.
  So `nak req --auth` without `NOSTR_SECRET_KEY` is closed `restricted`, and `nak event`
  without it publishes a wrap the relay refuses as a stranger. Both gateway paths pass the
  identity secret in the child's **environment** (never argv); `hng listen`/`hng send` were
  fixed to match in `f9f3029`. Any new `nak` invocation in this repo needs the same treatment.
- **Connection rate limiter** — a token bucket per relay group on *new connections*:
  `/chat` 3 tokens per 3 s, `/private` 3 per 5 s, `/inbox` and the outbox 3 per 1 s, all with
  a 9-token burst. Starting a dozen workers at once fits; replaying a failed batch does not.
  Retry passes send at most 5 wraps 1.5 s apart, and a `429` backs off from 60 s to 600 s with
  jitter. Do not "speed up" reconnects — the relay answers by refusing connections.
- **Event limiter** is separate and per IP: `/chat` 50 events/s (burst 100), `/inbox` 10/s
  (burst 20), the outbox 10 per minute (burst 100). A publish burst from one address hits this
  before it hits the connection limiter.
- **`kind not allowed` is the filter, not the key.** Every DM worker asks for `1059` *and*
  legacy `4` in one REQ; a relay that refuses kind 4 refuses the whole subscription, so its
  1059 DMs go with it. `/chat` tolerates the pair; `relay.eu.whitenoise.chat` does not
  (`CLOSED: ERROR: bad req: filter validation failed: kind not allowed: 4`, seen 2026-10-24),
  which leaves that relay with no DM subscription at all.
- **`CHAT_RELAY_MINIMUM_FOLLOWERS=3` is set but not what runs.** The boot log reports
  `minFollowers=0`; the depth-3 WoT is what is enforced. Re-check the log after a restart
  before relying on the follower floor.
