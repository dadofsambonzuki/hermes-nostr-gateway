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
- **Marmot plane**: a proxy over `wn-agent`'s control socket — group messages arrive as ordinary
  frames (`plane: "marmot"`), `send` reaches them with `to: "marmot:<group id>"`, and
  `marmot.group` / `marmot.timeline` read through the same socket. MLS stays wn-agent's; the
  plane attaches only when that daemon's socket exists.
- **Hermes plugin**: `integrations/hermes-plugin/` — a platform adapter that turns socket frames
  into MessageEvents and replies into `send` ops. It maps the gateway's `authority` onto
  `source.role_authorized`, so untrusted senders reach the agent as data, never as instructions.
- Prior art and the live-testing handover this design leans on: [`docs/prior-art/`](docs/prior-art/).

## Install

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). One process, one identity; the systemd unit
is `deploy/hermes-nostr-gateway.service`.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[media]'
.venv/bin/hng --help
```

### The patched `nostr-sdk` wheel

`requirements.txt` and `pyproject.toml` install `nostr-sdk` from a patched build, never from
PyPI. Concord derives its stream keys from the NIP-44 v2 conversation key, and the released
bindings do not expose `nip44_conversation_key` — a 16-line addition to
`src/protocol/nips/nip44.rs`:

| | |
|---|---|
| Fork | [`dadofsambonzuki/nostr-sdk-ffi`](https://github.com/dadofsambonzuki/nostr-sdk-ffi), branch `feat/nip44-conversation-key` |
| Source commit | `eddb08d` — *feat(python): expose NIP-44 conversation key* |
| Upstream PR | [`nostrdevkit/nostr-sdk-ffi#96`](https://github.com/nostrdevkit/nostr-sdk-ffi/pull/96) |
| Wheel | `https://github.com/dadofsambonzuki/nostr-sdk-ffi/releases/download/wheel-0.45.1-nip44/nostr_sdk-0.45.1-cp39-abi3-linux_x86_64.whl` |
| sha256 | `2a728769109d4d2f42cf3de37c591e2f24487471f0e5971ddefa5b638d261de6` (mirrored content-addressed at `relay.day.ag/2a7287…whl`) |

The version number is unchanged (0.45.1), so `pip install nostr-sdk` or an install that resolves
the dependency by name *succeeds* and quietly swaps in the release wheel. That is why the
dependency is a direct URL with a hash: a clean venv cannot silently lose the patch. The failure
is not silent at runtime either — the first Concord derivation raises `CryptoError` naming the
patched wheel, and `tests/test_concord.py` checks the derivation against a NIP-44 vector. If you
rebuild from the fork branch, update the URL and the hash in `requirements.txt` together.

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
