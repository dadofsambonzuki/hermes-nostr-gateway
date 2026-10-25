# Hermes Nostr Gateway

A Nostr transport hub for Hermes agents: one local socket, several protocol *planes*, and an
attention policy that decides what is worth waking an agent for.

Hermes has no Nostr platform surface of its own. This project gives it one without pretending
Nostr is a single protocol: DMs arrive over NIP-17 or legacy NIP-04, community traffic over
Concord, group chat over Marmot, and public life over kind 1. The agent should not have to care
which — it reads one normalised event stream.

## Target state

One long-running service per identity, one local socket, one adapter — and an agent that never
has to know which protocol a message arrived on. When it is finished, this is what runs:

- **One socket.** NDJSON over a 0600 Unix socket, no network listener, carrying every plane:
  DMs (NIP-17, legacy NIP-04), public feeds and mentions (NIP-01), Concord communities, and
  Marmot groups proxied to `wn-agent`.
- **One policy.** Every message, whatever the plane, is classified for *reach* — wake the agent
  now, or fold it into a digest — and for *authority* — instructions, or data it may read but
  not obey. Sender trust is decided once, centrally, not per transport.
- **One adapter.** `integrations/hermes-plugin/` maps frames onto Hermes' platform contract and
  maps `authority` onto `source.role_authorized`, so an untrusted sender can reach the agent as
  text and never as a command.
- **Durability by default.** A message is stored before it is delivered, re-offered if the
  adapter never acknowledged it, and retried per relay by republishing the *same* signed event —
  re-wrapping would change its id and show the peer a duplicate.
- **Coverage that is measured, not assumed.** Reads span the identity's follows, its NIP-65
  write relays and the sender app-relay defaults; per-relay event counts and last errors are in
  `hng status`, because "we read all relays" is a claim a status line should be able to prove.
- **Marmot stays Marmot.** MLS is `wn-agent`'s job; the gateway speaks its control socket as a
  proxy plane rather than reimplementing a protocol it does not own. The gateway is the single
  entry point, and `wn-agent` remains the transport underneath.

The authoritative design is [`docs/gateway-spec.md`](docs/gateway-spec.md); host-side operations
are in [`docs/ops-notes.md`](docs/ops-notes.md). Where the current build differs from this
target is tracked in [`QUEUE.md`](QUEUE.md), not here — a README that doubles as a progress log
goes stale between commits.

## Pieces

- **Operating notes** — [`docs/ops-notes.md`](docs/ops-notes.md): the service, its socket and the
  host-side gates (`relay.day.ag`'s web-of-trust depth and rate limits) that explain its
  refusals. Mirrored for the host at `~/Projects/infra/ops/hermes-nostr-gateway.md`.
- **Conformance probes** — `scripts/probe_nip17_conformance.py` executes the NIP-17 traps this
  design has to respect: a wrap's `p` tag is the recipient identity key, wraps are backdated up
  to two days against a three-day read window, the watermark belongs on the inner rumor, NIP-42
  must sign as the identity, and the self-copy is never surfaced. `--live` adds the spec §13.1
  wire probe: a throwaway recipient, a real relay, read back and decrypted, so no probe message
  reaches the household inbox. `tests/test_conformance.py` exercises it.
- **Hermes plugin** — `integrations/hermes-plugin/` (manifest, adapter, `register(ctx)` shim), kept
  in step with the live copy under `~/.hermes/plugins/nostr/`.
- **Prior art** — [`docs/prior-art/`](docs/prior-art/) holds the handover this design leans on.

## Quick start (five minutes)

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). One process, one identity; the systemd
unit is `deploy/hermes-nostr-gateway.service`. From a checkout:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[media,dev]'
cp config.example.yaml config.yaml
$EDITOR config.yaml                      # point identity.secret_file at a 0600 JSON file
.venv/bin/hng --config config.yaml status
.venv/bin/hng --config config.yaml serve
```

`hng status` is the five-second check, and it answers a live question: for each relay
in `relays.read` it asks the running daemon what that relay's workers are doing right
now (`{"op":"status"}` on the socket) and prints one verdict — `connected`,
`authenticating`, `no events for 47m`, `refusing`, `down`, `no worker`, or `unknown`
when the daemon is not running. The child process's stderr is not a verdict and is no
longer shown as one; `-v` prints it under a heading that calls it diagnostics. Store
rows for relays the config no longer lists appear under `not in config (history only,
not read)`. `--json` gives the same facts machine-readable and `--check` exits non-zero
when the daemon is unreachable or a relay is unhealthy; `--quiet-minutes N` sets the
silence window (default 30, 0 disables it). `serve` then holds the socket and the relay
workers; the agent side reads the same socket (`NOSTR_GATEWAY_SOCKET` overrides its
path). `config.yaml` is gitignored, so a local edit cannot be committed by accident —
`config.example.yaml` is the template.

Tests and lint need no relay and no secret:

```sh
.venv/bin/python -m pytest -q            # full suite, ~4 s
.venv/bin/ruff check src tests scripts
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

## Container

`Dockerfile` builds the same package with a pinned `nak` (the relay transport is a `nak`
child per relay, so it is part of the image). The image runs as `gateway`, UID/GID 1000 by
default; build with `--build-arg UID=$(id -u) --build-arg GID=$(id -g)` when the host user
that owns the mounted files is not 1000.

There is no network listener, so the socket is published through a volume, not a port:

```sh
docker build -t hermes-nostr-gateway .

mkdir -p state
cat > state/config.yaml <<'YAML'
identity:
  secret_file: /run/secrets/nostr-secret.json
store: /var/lib/hermes-nostr-gateway/gateway.db
socket: /var/lib/hermes-nostr-gateway/gateway.sock
relays:
  read: [wss://relay.day.ag/chat, wss://nos.lol]
  publish: [wss://relay.day.ag/chat, wss://nos.lol]
YAML

docker run -d --name hermes-nostr-gateway --restart unless-stopped \
  -v "$PWD/state:/var/lib/hermes-nostr-gateway" \
  -v "$PWD/state/config.yaml:/etc/hermes-nostr-gateway/config.yaml:ro" \
  -v "$HOME/.marmot-agents/hermes/accounts/hermes-agent/secret.json:/run/secrets/nostr-secret.json:ro" \
  hermes-nostr-gateway
```

Point the adapter at `NOSTR_GATEWAY_SOCKET=$PWD/state/gateway.sock`. `docker logs` is the
daemon's journal; `docker run --rm` with the same mounts replaces the default `serve` with
`status` for a one-off check (the entrypoint is `hng`, so the argument is the subcommand).

## Tests and CI

pytest and ruff are the two gates:

```sh
python -m pytest -q                       # full suite
ruff check src tests scripts
```

`.ngit/act/workflows/test.yml` runs both on every push to `master` and every Nostr pull
request through the ngit-ci coordinator. It installs `requirements.txt` first and then
asserts that the installed `nostr-sdk` really exposes `nip44_conversation_key`, so a
resolver that quietly swaps in the PyPI release of the same version fails a named step
instead of Concord at runtime. The repository's canonical home is Nostr, so there is no
`.github/workflows` copy for a mirror to drift from.

## Relays and kinds

One subscription per relay, because each extra connection counts against the relay's
connection limiter. A Nostr filter is all-or-nothing at the relay, though: a REQ naming a kind
the relay refuses is closed whole (`filter validation failed: kind not allowed: 4`), so a relay
that rejects legacy kind 4 would otherwise lose its kind-1059 DMs along with it. Hence a kind
set per relay:

- the default DM subscription is `1059` + legacy `4`;
- `relays.read` may pin a relay's set — `- {url: wss://relay.example, kinds: [1059]}` — for a
  relay that refuses a kind rather than for one that simply has less traffic;
- if a relay still refuses a kind at runtime, the worker drops that kind and resubscribes
  immediately on the rest: never a kind-less filter, never a reconnect loop that cannot succeed.
  `hng status` shows the narrowed set in that relay's detail, and reports `refusing` when
  nothing is left to read.

**White Noise relays belong to the Marmot plane.** `relay.eu.whitenoise.chat` and
`relay.us.whitenoise.chat` exist to carry White Noise's own groups, which `wn-agent` speaks for
us; they are not a general NIP-17 inbox, they refuse public kinds, and the gateway does not put
them in the DM or feed read set. They are named under `relays.whitenoise` for the Marmot plane
to use through `wn-agent`. The trade is deliberate: a DM that exists *only* there is not fetched
by the DM workers, and if one ever goes missing this is the first thing to check.

## Rate limits and blocked frames

One token bucket per sender, shared by every plane (NIP-17, NIP-04, Marmot), so
switching transport cannot earn a fresh burst: 10 messages at once by default, then
30/hour. Over the bucket the message is still stored — the record matters — but it is
never delivered to the adapter. The socket emits a `blocked` frame instead of a
`message`:

```json
{"type":"blocked","plane":"nip17","from":"<hex>","reason":"rate_limited","rumor_id":"<id>"}
```

The same frame shape carries policy drops (`reason: "not_allowlisted"`), so a flooding
peer degrades to blocked rather than a queue that grows, and no blocked message can
look like chat to a consumer. `rate_limit: {burst: 10, per_hour: 30}` in the config
overrides the defaults; `enabled: false` turns it off.

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
