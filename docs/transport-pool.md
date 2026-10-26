# The read transport: one client, one subscription per filter set

Plan P1. This records what the read path is now, what it was, and how to go back.

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

| Concern | Legacy (`transport.legacy: true`) | Pool (default) |
| --- | --- | --- |
| One relay's socket | `nak` child, one per subscription | one `Client`, one connection per relay |
| NIP-42 | `NOSTR_SECRET_KEY` in the child's env | `SignerAuthenticator` over our `Keys`, in-process |
| Refused kind | regex on the child's stderr, then respawn | `RelayMessage.CLOSED` → drop the kind, resubscribe |
| Reconnect | spawn loop with our own backoff | the library's reconnect (default on, 30s retry) |
| Dedupe on replay | none | `ClientNotification.NEW_EVENT` is first-seen only |
| Shutdown | kill each child, wait, hope | `Client.shutdown()` |

## What stays the same

`PoolWorker` exposes `RelayWorker`'s observable surface: `relay`, `purpose`,
`requested_kinds`, `refused`, `kinds`, `events`, `last_error`, `started_at`,
`attempts`, `connected_at`, `auth_at`, `last_event_at`, `live_state()`, `run()` and
`stop()`. `gateway._new_worker()` picks the class from config, so `status`, `health`,
`_guarded` and the peer-inbox logic do not branch on transport.

Two additions to `live_state()`: `subscription_id` (how a notification finds its
worker) and `transport` (`"pool"`), plus `rate_limited_at` when a relay answers a
CLOSED frame with a 429 or "rate limit". The legacy worker never set those, so their
absence elsewhere is not ambiguity.

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

- **Publish** (`publisher.py`) — P2 replaces the per-relay `nak event --auth` children.
- **Bootstrap and ad-hoc fetches** — `Gateway._load_follows()` (one kind-3 lookup at
  startup, before any worker exists) and `hng --once` / `OneShot` still shell out to
  `nak`. They are one-shot reads, not workers; moving them is a separate change.

So `ps` on a running gateway shows no `nak req --stream` children; a startup `nak req`
for the contact list can still appear for a second, and `hng --once` still uses it.

## Rollback

One config line:

```yaml
transport:
  legacy: true
```

The subprocess worker code is untouched (`relay.py`), the wiring is shared, and
`status.transport.mode` says which one is running. The flag is meant to last one
release: after that the legacy class is deleted.

## How to verify

- `hng status` → `transport: {mode: pool, subscriptions: N, relays: M, events: …}`;
  `mode: legacy` means the flag is set somewhere.
- `ps -ef | grep 'nak req'` while the daemon is up: nothing (outside `hng --once`).
- Per-relay event counts and `last_event_at` still arrive per relay, in
  `status.workers[]` and in the `relay_stats` table.
- A relay that refuses kind 4: `status.relay_stats` carries the same
  "refuses kind(s) 4; reading 1059" line the legacy path recorded.
