# hermes-nostr-nip17 — phase 0 spike

A NIP-17 (private DM) wrapper for the Hermes gateway, built as leverage rather than a
from-scratch gateway: `nak` (already installed) supplies NIP-44 + NIP-59 + relay I/O, and the
Hermes side will be the catalog `bridge-adapter` plugin, which exchanges JSON files with a
wrapper. This directory is the wrapper plus its tests.

## What exists now (phase 0)

| Piece | State |
|---|---|
| `nip17_wrapper.py send <text> --to <hex>` | Works. Kind 14 rumor → kind 13 seal → kind 1059 wrap to the peer **and** to ourselves (NIP-17 self-copy), published per relay with per-relay results. |
| `nip17_wrapper.py listen` | Works. Long-lived `nak req --stream` subscription for wraps addressed to our npub, unwrap, allowlist check, dedupe by rumor id, catch-up cursor, one JSON file per accepted message in `--inbox`. |
| `spike_sim.py` | Throwaway-identity inbound simulator (tests the receive path without a second human). |
| `spike_inbound_test.py` | End-to-end receive test: blocked branch and allowed branch, against a local `nak serve` relay. |
| Gateway integration | **Not done** — that is phase 1 (`bridge-adapter` + registry manifest + systemd unit). |

## Run it

```bash
# outbound
python3 nip17_wrapper.py send "hello" --to <peer hex pubkey>

# inbound (long-running; writes inbox/<timestamp>-<id>.json)
python3 nip17_wrapper.py listen --state ~/.nip17-wrapper/state.json --inbox ~/.nip17-wrapper/inbox

# tests
python3 spike_inbound_test.py          # starts its own local relay on :8777
```

Config: `config.json` (identity + relays + `dm_policy` + `allow_from`). `--config` must precede
the subcommand; `NIP17_CONFIG` overrides the path for a service unit. State defaults to
`~/.nip17-wrapper/`.

## Pitfalls found the hard way (all four cost real debugging time)

1. **`nak` writes status lines to stderr, only the event JSON to stdout.** Checking
   `"success" in stdout` reports every successful publish as a failure. Ask for both streams.
2. **`nak gift wrap` silently uses a NIP-4E decoupled key when it has one stored.** The wrap's
   `p` tag then stops being the recipient's identity pubkey, and a standard NIP-17 client
   (which subscribes on `#p = <their identity key>`) never sees the message. Always pass
   `--use-our-identity-key --use-their-identity-key`, and assert the `p` tag before publishing.
3. **Wrap timestamps are randomised up to two days into the past** (NIP-59, anti-timing). A
   tight `since` filter — even 120 seconds — hides messages published seconds ago. Any
   subscription window must exceed two days; the wrapper defaults to three.
4. **`nak dekey list` is not read-only.** It generated and stored a decoupled key for our
   identity and tried to publish a kind 10044 (the White Noise relays reject that kind). Do not
   run it casually.

Also: our relay refuses writes from unlisted keys (`blocked: you must be in the web of trust`,
`restricted: you must be whitelisted`), which is why the inbound tests use a local `nak serve`
relay instead of a public one — testing our code, not a relay's spam policy.

## What is proven, and what is not

Proven: outbound wraps publish to both `relay.day.ag` paths with correct `p` tags and unwrap
back to the original text; the live subscription catches a wrap within seconds of publication;
history catch-up replays older wraps; own self-copies are recognised and not delivered; a
non-allowlisted sender is blocked and an allowlisted one produces a well-formed inbox JSON.

Not proven: a real message from Nathan's client in, and a reply landing in his client. That
needs his client pointed at these relays (kind 10050) and is the phase 0 acceptance test.

## Decisions locked in

Same npub as Holly's White Noise identity (one Holly); our own relays
(`wss://relay.day.ag/chat`, `/inbox`); allowlist = Nathan, plus Sam once we have his npub.
