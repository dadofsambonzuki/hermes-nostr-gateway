# Operating notes

What the daemon is, where its socket lives, and the two host-side gates that make
`relay.day.ag` look like it is refusing us. Checked against the live host and against
haven `v1.2.2` on 2026-10-25.

This is the source copy. The ops-side mirror lives at
`~/Projects/infra/ops/hermes-nostr-gateway.md` (the file Nathan chose on 2026-10-24 instead of
an `infra/AGENTS.md` entry): change this copy, commit it, then re-copy the body across so the
two differ only in their headers.

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

- **Web-of-trust gate, depth 3** — the graph is seeded from `OWNER_NPUB` plus every npub in
  `/opt/haven/whitelisted_npubs.json`, walked to depth 3, held in memory and rebuilt at boot
  (about 4 s; 36k pubkeys from 534 import relays on the last start). NIP-42 auth is checked
  **against that graph** on reads and writes. Depth, follower floor and refresh are sized by
  `WOT_DEPTH` / `WOT_MINIMUM_FOLLOWERS` / `WOT_REFRESH_INTERVAL` — **not** by the
  `CHAT_RELAY_*` names. Read *What the web of trust actually gates* below before changing
  anything here.
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
- **`kind not allowed` is the filter, not the key.** A DM subscription asks for `1059` *and*
  legacy `4` in one REQ; a relay that refuses kind 4 closes the whole subscription, so its
  1059 DMs went with it. `/chat` tolerates the pair; `relay.eu.whitenoise.chat` does not
  (`CLOSED: ERROR: bad req: filter validation failed: kind not allowed: 4`, seen 2026-10-24),
  which left that relay with no DM subscription at all. The kind set is now per relay:
  `relays.read` pins whitenoise to `[1059]`, a worker refused a kind drops it and
  resubscribes on the rest at once, and a relay that refuses every kind stops with an error
  rather than ever sending a filterless REQ. `hng status` records both the refusal and what
  is still being read.
- **`CHAT_RELAY_MINIMUM_FOLLOWERS=3` is not a knob haven 1.2.2 reads.** The name in this
  build is `WOT_MINIMUM_FOLLOWERS`, and it is unset here, so the boot log reports
  `minFollowers=0`; the depth-3 graph is what is enforced. Do not edit the `CHAT_RELAY_*`
  WoT names and restart expecting a change — see below.

## What the web of trust actually gates

Checked on 2026-10-25 against haven `v1.2.2` (`github.com/barrydeen/haven`, tag `v1.2.2`,
commit `45d835f`) and against the running relay. Each claim is marked **[source]** (read from
that tag) or **[measured]** (this box's log or a probe); nothing here is inferred from `.env`.
The deployed binary is a local build of that tag with a patched khatru 0.19.1, sha256
`a3cc6ef9…` — NIP-11 says `version: (devel)`, so identify the release by hash, not that field.

- **The roots are the relay's owner plus its whitelist, not the connecting client's follows.**
  `loadConfig` inserts `OWNER_NPUB` into the whitelist map itself (`config.go:125`), and at
  depth 3 the WoT builder seeds from that whole map: it fetches kind 3 for every key in
  `WhitelistedPubKeys` (`pkg/wot/simple_in_memory.go:84-100`, wired at `main.go:80-88`).
  **[source]** On this box the file holds 5 npubs and the boot log says
  `Number of whitelisted pubkeys: 6` — the owner is the sixth — and Holly's npub is one of
  the five. Her follows are roots *because she is whitelisted*, not because the gateway is
  doing the reading. A client's own follow list counts for nothing unless that client is in
  the seed set. **[measured]**
- **Depth 3 means seeds + their follows + the follows of those, minus the follower floor.**
  Membership is a property of a key against the seed set; the direction of some other pair's
  follow edge is irrelevant unless it leaves a seed. So "A follows B, therefore A can read",
  "the peer doesn't follow the bot, therefore the bot can't read" and "the two bots follow
  each other, so they are in the web" are all invalid inferences. The floor counts mentions
  across the follow lists haven actually fetched (seeds and one-hop), not a global follower
  count, and it is applied only at depth 3. **[source]**
- **The WoT names are `WOT_*`; the `CHAT_RELAY_*` WoT names are inert.** The only WoT keys in
  `config.go:112-115` are `WOT_DEPTH` (default 3), `WOT_MINIMUM_FOLLOWERS` (default **0** in
  code), `WOT_FETCH_TIMEOUT_SECONDS` (default 30) and `WOT_REFRESH_INTERVAL` (Go duration
  string, default `24h`). Levels: 0 off, 1 owner + whitelist only, 2 plus their direct
  follows, 3 plus follows-of-follows. **[source]** `.env` instead carries
  `CHAT_RELAY_WOT_DEPTH=3`, `CHAT_RELAY_WOT_REFRESH_INTERVAL_HOURS=24` and
  `CHAT_RELAY_MINIMUM_FOLLOWERS=3`, none of which is read; its only `WOT_*` entry is
  `WOT_FETCH_TIMEOUT_SECONDS=60`. The running model is therefore the code default: depth 3,
  floor 0, 24 h refresh. **[measured]**
- **`CHAT_RELAY_MINIMUM_FOLLOWERS` — and `CHAT_RELAY_WOT_DEPTH` /
  `CHAT_RELAY_WOT_REFRESH_INTERVAL_HOURS` — are read by nothing in this build.** v1.2.2 never
  references them (`grep -rn CHAT_RELAY_MINIMUM_FOLLOWERS` over the tag: nothing), and
  `strings /opt/haven/haven | grep -c CHAT_RELAY_MINIMUM_FOLLOWERS` returns **0** while the
  same binary contains `WOT_MINIMUM_FOLLOWERS`. **[source + measured]** With
  `CHAT_RELAY_MINIMUM_FOLLOWERS=3` in `.env` and no `WOT_MINIMUM_FOLLOWERS`, the
  2026-10-25 15:46:51 start logged
  `Web of Trust Level 3 -> Connection of Connections (owner, follows, and their follows) with minFollowers=0`
  and then a pruner line reading `minimum=0 kept=36656 eliminated=0` (the emoji-prefixed
  fields are `minimum`, `kept`, `eliminated`). Editing those names and restarting changes
  nothing. **[measured]**
- **The trap: the defaults happen to match the intent.** `.env` asks for depth 3, three
  common followers and a 24 h refresh; the code's defaults are 3, 0 and 24 h, so the log
  agrees with the config on two of three while reading none of it. Upstream's own
  `docs/wot.md` says `WOT_MINIMUM_FOLLOWERS` defaults to 3, but `config.go:113` defaults it
  to 0 — the code wins, so an unset key means 0, not 3. Confirm which key drives a value
  before "fixing" it, and read the model line back after any restart. **[source]**
- **`/chat` reads require NIP-42 and WoT membership.** The read chain is `MustAuth` then
  `MustBeInWoTToQuery` (`init.go:185`), with `khatru.RequestAuth` on connect (`init.go:207`);
  writes add `MustNotBeBlacklistedToPost`, `MustBeInWoTToPost` and `EventMustBeChatRelated`
  (`init.go:186-195`). A whitelist entry is a write grant on the owner-only paths, but here
  it also puts that npub's graph into the read model, so the two are not independent.
  **[source]**
- **A refused query is a `CLOSED`, never an `EOSE` — and that is the "silent" failure.**
  Measured against `wss://relay.day.ag/chat` on 2026-10-25:
  - no auth: `CLOSED: auth-required: all requests must be authenticated`, about 0.3 s
  - authed as a key outside the web:
    `CLOSED: restricted: you must be in the web of trust to query this relay`, about 0.3 s

  There is no EOSE on either path, by design: khatru writes `CLOSED` and returns before the
  EOSE goroutine is reached. A client that waits only for EOSE or events, and does not
  surface `CLOSED`, sees a subscription that delivers nothing and never reaches EOSE — no
  error, no EOSE. A hung REQ therefore proves the client ignored a close frame; it does not
  prove the relay dropped the query, and it says nothing about the write path.
  **[measured + source]**
- **NIP-42 auth is validated against the public URL, so loopback probes cannot authenticate.**
  `chatRelay.ServiceURL` is `https://<RELAY_URL>/chat` (`init.go:177`) and NIP-42 compares
  scheme, host **and** path. A REQ to `ws://127.0.0.1:3355/chat` signs
  `relay=ws://127.0.0.1:3355/chat`, fails validation, and comes back `auth-required` even
  though `nak --auth` printed `authenticating …`. The failed AUTH is not reported as an auth
  error, which is the other way this looks silent. Probe `wss://relay.day.ag/chat` with the
  identity you are asking about. **[measured + source]**

Diagnosing "is X in the web?", in order:

1. Read the model the process built, not `.env`:
   `journalctl -u haven | grep -E 'Web of Trust Level|pruned pubkeys' | tail -2`. The
   `minFollowers=` and `minimum=` values there are what is enforced.
2. Enumerate the seeds: `OWNER_NPUB` from `/opt/haven/.env` plus every entry in
   `whitelisted_npubs.json` (those entries plus the owner are the log's count).
3. Fetch those seeds' kind 3 from the import relays (`relays_import.json`) — not necessarily
   from this relay — and check X against the seeds themselves, their follows (level 2 and
   up), and the follows of those (level 3 only).
4. A new owner follow or whitelist edit is not live until the graph rebuilds: restart haven,
   wait for the listener, then re-probe. `is-active` says `active` throughout the rebuild.

## Pushing this repo is not the relay

Observed 2026-10-25 while pushing `pr/haven-wot-notes`: commit data went to `gitnostr.com` and
`git.shakespeare.diy`, while `grasp.day.ag` answered `unexpected http status code: 404` for
the same data. `git ls-remote` also 404s on the coordinate ngit builds for that host —
`https://grasp.day.ag/npub19ets…/hermes-nostr-gateway.git`, which decodes to `2e5709c6…`, one
of the announced maintainers, not the repo owner `npub1cn670…`. The push itself succeeded:
the branch and its PR event published (4 relays) and `git ls-remote` resolves the branch tip
from the other servers. Treat a grasp.day.ag 404 as a missing or mis-scoped coordinate
on that one cache, not a failed push; the exact cause is **unconfirmed**.
