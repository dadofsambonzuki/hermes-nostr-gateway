# The read transport: one client, one subscription per filter set

Plan P1. This records what the read path is now, and what it replaced.

## The decision

Reads run on a single `nostr-sdk` `Client` (`src/hermes_nostr_gateway/pool.py`). The
per-relay `nak req --stream` children are gone from the worker path.

The old transport bought per-relay visibility at the cost of a subprocess fleet: one
child per relay for DMs, another for feeds, another for mentions. NIP-42 lived in
`nak`'s stderr and was read by regex; a reconnect meant respawning a process; a
CLOSED frame could only be parsed out of that stderr after the child died; and every
relay was a separate process group to unwind on shutdown, which is why the unit's
stop timeout was a real risk.

The client gives the same coverage with the socket inside this process:

| Concern | The old `nak` child (removed) | Pool (now) |
| --- | --- | --- |
| One relay's socket | `nak` child, one per subscription | one `Client`, one connection per relay |
| NIP-42 | `NOSTR_SECRET_KEY` in the child's env | `SignerAuthenticator` over our `Keys`, in-process |
| Refused kind | regex on the child's stderr, then respawn | `RelayMessage.CLOSED` → drop the kind, resubscribe |
| Reconnect | spawn loop with our own backoff | the library's reconnect (default on, 30s retry) |
| Dedupe on replay | none | `ClientNotification.NEW_EVENT` is first-seen only |
| Shutdown | kill each child, wait, hope | `Client.shutdown()` |

## What stays the same

`PoolWorker` exposes the read contract `status`, `health`, `_guarded` and the
peer-inbox logic all consume: `relay`, `purpose`, `requested_kinds`, `refused`, `kinds`,
`events`, `last_error`, `started_at`, `attempts`, `connected_at`, `auth_at`,
`last_event_at`, `live_state()`, `run()` and `stop()`. It is the only read transport, so
nothing branches on which one is running.

Two additions to `live_state()`: `subscription_id` (how a notification finds its
worker) and `transport` (`"pool"`), plus `rate_limited_at` when a relay answers a
CLOSED frame with a 429 or "rate limit".

## One subscription per filter set, not per relay

The kind refusal rule is unchanged: a relay closes the whole REQ when it refuses one
kind, so the kind set is negotiated. On the pool the CLOSED frame arrives as a
notification, `note_refusals()` records the drop and publishes it to the store (so
`hng status` still shows "relays refuse kind(s) 4; reading 1059"), and the worker
resubscribes at once with what is left. A relay that refuses every kind stops, exactly
as before — with a filterless REQ still impossible to construct.

Each worker keeps its own subscription id (`hng-<purpose>-<hash>`), so the DM,
feed and mentions subscriptions on one relay receive only their own events.

## What is still a subprocess

Nothing, for reads or writes. Publishing runs on a `nostr-sdk` client in this process
(`publisher.py`), and the startup contact-list lookup, the peer kind-10050 inbox resolver
and `hng listen` (both `--once` and live) read through the bindings via `fetch.py`, so
`ps` on a running gateway shows no `nak` at all.

## Rollback

There is none: the subprocess workers are deleted (`relay.py` is now the refusal parser
and the handler types, `publisher.py` has one implementation), so there is no second
implementation for a flag to select. A stale `transport:` block in an older config file
is ignored.

## How to verify

- `hng status` → `transport: {mode: pool, subscriptions: N, relays: M, events: …}`.
- `ps -ef | grep nak` while the daemon is up: nothing on a default config.
- Per-relay event counts and `last_event_at` still arrive per relay, in
  `status.workers[]` and in the `relay_stats` table.
- A relay that refuses kind 4: `status.relay_stats` carries the
  "refuses kind(s) 4; reading 1059" line the worker recorded.
