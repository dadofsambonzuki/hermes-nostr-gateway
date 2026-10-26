# Marmot ownership: the gateway owns the wn-agent socket

**Status:** decided 26 Oct 2026; enforced in code from this commit, not yet flipped. Hermes'
Marmot platform plugin is still the carrier. The flip — and its rollback — is one config
line in each direction. Plan item: P0 in [`plan-v2.md`](plan-v2.md).

## The decision

1. **This process owns `wn-agent`'s control socket.** For the identity whose Marmot
   traffic the agent answers, `hermes-nostr-gateway` is the reader of that socket: it holds
   the subscription, normalises group messages into the same frames as every other plane,
   and sends out through the same `wn-agent` the phone talks to.
2. **Hermes' own Marmot platform plugin retires.** `platforms.marmot` in
   `~/.hermes/config.yaml` (plus the `marmot` entry in `plugins.enabled`) stops being the
   transport. It is not a second, harmless reader and is not kept "just in case".
3. **The retirement is conditional on a clean week.** The plugin stays installed and stays
   the carrier until the gateway has carried real group traffic for seven days without the
   failures listed below. If the week is not clean, the rollback is one line and the plugin
   stays.

The reason the decision had to be made at all is that the gateway's Marmot plane is a
*proxy*, not an implementation — MLS, group state and the final encrypted send are
`wn-agent`'s. Two proxies over one account is not a load-balancing story; it is two agent
runs per message.

## Why one owner, not two

The socket is a subscription, not a message queue with fan-out semantics. Hermes' plugin
and the gateway both subscribe to the same account, so one group message arrives at the
agent twice:

- two agent runs for one message, and therefore two replies in the group;
- duplicate side effects from anything the agent does in response (a tool call paid for
  twice, a file written twice, a DM sent twice);
- two independently-aging subscriptions, so a resend after a reconnect can show the group
  history the agent already answered.

There is no config in which both are useful. The only question is which one is the
transport, and that is what this document answers.

## What the code does now

`Gateway._guard_marmot_ownership` (`src/hermes_nostr_gateway/gateway.py`) runs before the
secret is read, before the store is opened and before any socket is bound:

- `marmot.enabled: true` **and** Hermes' Marmot platform enabled → `MarmotOwnershipError`.
  `hng serve` prints `refusing to start: <reason>` and exits `2`, so systemd records a
  failed start instead of a daemon that runs wrong.
- `marmot.hermes_platform: auto` (the default) reads `platforms.marmot.enabled` out of
  Hermes' own config — `$HERMES_HOME/config.yaml`, default `~/.hermes/config.yaml`, or
  `marmot.hermes_config` when this process sees a different Hermes home (a container).
  Reading the real setting beats keeping a copy of it in our config, because the copy
  drifts exactly when it matters.
- `hermes_platform: true` / `false` pins the answer for a host with no Hermes config to
  read. A missing or unreadable file is *unknown*, and unknown never refuses a start: a
  relay box with no Hermes on it has no second subscriber.
- The socket's `status` op carries `marmot.hermes_platform: {enabled, source}` — what the
  guard concluded and where it read it — in both the attached and the idle form.

The refusal names both ways out, so the operator does not have to find this document from
the error alone.

## Today's state, before the flip

The plugin is the carrier and the gateway is idle on this plane:

```yaml
marmot:
  enabled: false          # true = this gateway is the Marmot transport
```

`hng status` reports `marmot.enabled: false` with the opt-in reason, and an explicit
`marmot:<group>` send fails with that reason rather than looking like a bad target. This is
the safe state, and the guard does not fire in it: one reader is on, the other is off.

## The flip

Prerequisites: the gateway is deployed at the current master, `hng status` shows the NIP-17
planes healthy, and `wn-agent` is running (`systemctl --user is-active wn-agent-hermes.service`).

1. **Disable the plugin first.** Order matters — the other order duplicates messages for as
   long as both are up:

   ```sh
   hermes config set platforms.marmot.enabled false
   hermes plugins disable marmot
   hermes gateway restart
   ```

2. **Give the gateway the plane**, one line, and restart it:

   ```sh
   # config.yaml
   marmot:
     enabled: true
   ```

   ```sh
   sudo systemctl restart hermes-nostr-gateway
   journalctl -u hermes-nostr-gateway -n 20 | grep -i marmot   # expect "marmot plane via ..."
   ```

3. **Prove it in one message.** From the phone, send one group message, then:

   ```sh
   hng status --json | jq .marmot          # worker.connected true, events 1
   hng recent -n 5                         # one row, plane marmot, then our one reply
   ```

   Exactly one reply, and one `history` row for it. Two of either means the plugin is still
   attached — the guard cannot see a Hermes restart that happened after ours.

**Rollback** is step 2 reversed, plus step 1 undone: set `marmot.enabled: false`, restart
the service, re-enable `platforms.marmot.enabled: true` with the `marmot` plugin entry, and
restart Hermes. Nothing else changes: the store is shared, and group history lives in
`wn-agent`, not in either reader.

## What "a clean week" means

Seven days of real group traffic with the plugin off, and all of:

- every inbound group message produced **exactly one** agent run — no duplicate replies in
  the group and no doubled tool work;
- an outbound reply reached the group through `wn-agent` (not just stored locally);
- a gateway restart resumed inbound delivery without re-offering a message the agent had
  already answered, and `status` then listed the group from stored evidence, verified
  against `group_info` (`marmot.conversations[].verified: true`);
- no `marmot worker died` / `marmot event rejected` in the journal beyond a single bad
  event being skipped by design.

Any failure means: roll back (one line), keep the plugin, and file the failure as its own
queue item. The plugin does not retire on a promise.

## After the clean week

- Remove `platforms.marmot` from `~/.hermes/config.yaml` and the `marmot` entry from
  `plugins.enabled`; a disabled-but-installed plugin is one `hermes config set` away from
  coming back, which is the drift this decision exists to stop.
- Update the README and `docs/ops-notes.md` to describe the gateway as the only reader.
- Keep `marmot.hermes_platform` in the guard. It costs nothing, and it catches a reinstall
  or a copied config on a second host — the two ways the second reader comes back.

## What this does not decide

- **MLS itself.** `wn-agent` owns group protocol, key packages and the encrypted send. The
  gateway never reimplements it (spec §16).
- **The relay set.** White Noise relays stay Marmot-only and are never part of the DM or
  feed read set (see `QUEUE.md`, "Decisions").
- **Who may instruct the agent.** That is the attention policy, per sender group, on every
  plane — unchanged by which process reads this socket.
