# Gateway v2 — from a collection of adapters to one entry point

Written after an end-to-end critical review (26 Oct 2026). The code is healthy; the
architecture has one unresolved decision and one piece of inherited debt. This plan is the
order of work, deliberately as small PRs that can each be reviewed and reverted alone.

## The two things this fixes

1. **The hub promise is still false.** This channel is carried by the Marmot *plugin*; the
   gateway's marmot plane is an opt-in proxy that is off, because both attached means every
   group message is delivered twice.
2. **The transport is a subprocess fleet.** One `nak req --stream` child per relay for reads,
   one `nak event --auth` child per publish. That choice now costs us NIP-42 lifecycle,
   subscription control, our own event verification, batching, and a clean shutdown path.

## P0 — Record the Marmot ownership decision (docs only)

Decide, in writing, that **the gateway owns the wn-agent socket** and the Hermes Marmot
plugin is retired once the gateway has carried it cleanly for a week. The plane stops being
opt-in in the same change, and the double-delivery guard becomes a startup error rather than
a log line: if `platforms.marmot` is enabled *and* `marmot.enabled` is true, refuse to start.

- Files: `docs/marmot-ownership.md`, `README.md`, `config.example.yaml`, `gateway.py`.
- Acceptance: the rule is enforced in code and documented; the flip/rollback is one config
  line either way.
- Risk: none to running traffic — the plugin stays the carrier until the flip flag changes.

## P1 — Read transport on `nostr-sdk`'s relay pool

Replace `relay.py`'s per-relay `nak` subprocesses with one `Client`, one subscription per
kinds-set, and in-process NIP-42. Keep the old workers behind `transport.legacy: true` for
one release so a relay that behaves differently can be compared side by side.

- Acceptance: per-relay event counts match the legacy path within a day; no `nak` child
  processes in `ps`; a killed relay reconnects without replaying history; a 429 backs off.
- Risk: medium — this is the load-bearing change. Rollback is the legacy flag.

## P2 — Publish through the bindings

`publisher.py` builds and signs the event once, then sends it to each relay via the same
client; per-relay results are recorded per relay, and retries resend the *same* signed event
(never re-wrap). Delete the `nak event --auth` path.

- Acceptance: a DM reaches all publish relays with zero subprocesses; a failed relay is
  retried with a byte-identical event id; `nak` is only used by `hng` ad-hoc commands.
- Risk: medium; rollback is a config flag selecting the old publisher.

## P3 — Make drops first-class

46 rows sit blocked forever with no way to see them. Add a `drops` socket op (filters by
plane, reason, since), a TTL, and a `drops` count in `status`.

- Acceptance: `drops` lists the reason and time for each; rows older than the TTL are
  reported as expired rather than silently retained; a test covers both.

## P4 — Bot and echo policy

Peers that are agents need their own rules. Read `bot` from kind-0 (cached), and when a peer
is a bot: cap autonomous replies per hour, suppress identical content echoed within a window,
and never let a bot's message promote a digest to immediate.

- Acceptance: a simulated bounce (same text both directions) stops after the cap; a human
  peer is unaffected; the 3,370-row incident is a regression test.

## P5 — Truthful status, and a `health` op

`hng status` currently prints `err=connecting to nos.lol... ok.` and lists relays that are no
longer configured, because `relay_stats` is historical and never aged. Show live workers
separately from history, fix the concatenated error string, and add `health`: per plane, last
event time, pending count, blocked count, degraded flag. The overnight cron reads `health`
instead of parsing logs.

- Acceptance: status of a removed relay is absent from the live section; `health` answers in
  one call; the cron's narrative is generated from it.

## P6 — Store on the loop

`store.add` writes synchronously from the worker callback; a burst blocks the event loop.
Enable WAL, batch inserts per tick, and move writes off the loop. Measure with a 500-event
burst.

- Acceptance: p99 loop lag under 50 ms during a burst; no rows lost on kill -9 (WAL);
  a benchmark is committed so the claim is testable.

## P7 — Concord is a supported plane (corrected)

An earlier draft called this plane experimental and proposed switching it off by default.
That was wrong: Concord is in production on more than one deployment, so the plane stays
attached and documented like any other. The only thing owed is accuracy in the docs — and,
eventually, a recorded real invite as a test vector so the parser is exercised by something
other than our own fixtures. That vector is a nice-to-have, not a gate.

## P8 — Security hardening

- `SO_PEERCRED` on accept: the socket answers only the same uid.
- Key via systemd `LoadCredential`, not `Environment` (out of `systemctl show` and child env).
- the host's operating notes (kept outside this repo) updated with both.

## P9 — Attachments reach the agent

`media.get` exists server-side, but the adapter never calls it. On an attachment frame, fetch
the blob and attach it to the `MessageEvent` so the agent sees the image, not a URL.

- Acceptance: an inbound DM with an image yields bytes in the adapter; oversize/unknown types
  are refused with a code.

## Working agreement

- One PR per item, branch `pr/<slug>`, opened with `ngit pr create`; **no direct commits to
  master**.
- Items are cards on the host's Hermes kanban board, and the board's dispatcher runs each one in
  its own worktree.
- Merge only after the operator confirms. A card that needs a decision is blocked on the board
  with the question, and the worker moves on to the next card.

## Out of scope

New planes; a web UI; multi-identity support; replacing the policy model (it is the best
thing here).
