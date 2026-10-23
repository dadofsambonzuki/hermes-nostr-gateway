# Hermes Nostr Gateway

A Nostr transport hub for Hermes agents: one local socket, several protocol *planes*, and an
attention policy that decides what is worth waking an agent for.

Hermes has no Nostr platform surface of its own. This project gives it one without pretending
Nostr is a single protocol: DMs arrive over NIP-17 or legacy NIP-04, community traffic over
Concord, group chat over Marmot, and public life over kind 1. The agent should not have to care
which — it reads one normalised event stream.

## Status

- **Design: approved.** The authoritative spec is [`docs/gateway-spec.md`](docs/gateway-spec.md).
- **M1 running**: the gateway is a systemd service (`deploy/`), speaking NDJSON over a 0600 Unix
  socket, with the policy engine, rumor store, relay workers and NIP-17 plane in place.
- **Hermes plugin**: `integrations/hermes-plugin/` — a platform adapter that turns socket frames
  into MessageEvents and replies into `send` ops. It maps the gateway's `authority` onto
  `source.role_authorized`, so untrusted senders reach the agent as data, never as instructions.
- Prior art and the live-testing handover this design leans on: [`docs/prior-art/`](docs/prior-art/).

## Shape

```
agent runtime (Hermes plugin)
        │  NDJSON over a Unix socket, 0600
        ▼
   hermes-nostr-gateway
        ├── nip17   DMs: 1059 giftwrap → 13 seal → 14 rumor, media, reactions
        ├── nip04   legacy DMs
        ├── concord serverless E2EE communities (invites, chat, rekey)
        ├── nip01   feeds and mentions, digested by policy
        └── marmot  proxied to wn-agent — MLS stays theirs
```

- **Split by protocol, never by app.** Armada, Amethyst, Damus and 0xChat all speak the NIPs; a
  gateway written to one client bakes in its quirks.
- **Reach and authority are separate.** A public bot can hear strangers without letting them
  instruct it; see spec §17.
- **Relay coverage is a correctness property.** Reading only the identity's `10050` misses DMs
  that exist only on the sender's app relays — measured, not theorised (spec §3).
- **The gateway never interprets content.** No fetching beyond configured Blossom servers, no
  command parsing, no reply policy, no read receipts.

## Credits

- **Marmot / `wn-agent`** (`github.com/marmot-protocol/mdk`) — the agent-connector shape this
  follows, and the Marmot plane itself.
- **Armada** (soapbox.pub) — reference client for NIP-17 and Concord.
- **Hal**, the BitFest bot (`npub19etsn3sn…`) — the relay-coverage finding and testing discipline
  in [`docs/prior-art/hal-whitenoise-dm-handover.md`](docs/prior-art/hal-whitenoise-dm-handover.md).
- The NIP authors, obviously.

## Licence

MIT — see [`LICENSE`](LICENSE).
