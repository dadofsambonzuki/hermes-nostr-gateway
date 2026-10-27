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
  Marmot groups proxied to `wn-agent`. Every connection is checked with `SO_PEERCRED`, so the
  socket answers only the uid the daemon runs as.
- **One policy.** Every message, whatever the plane, is classified for *reach* — wake the agent
  now, or fold it into a digest — and for *authority* — instructions, or data it may read but
  not obey. Sender trust is decided once, centrally, not per transport.
- **One adapter.** `integrations/hermes-plugin/` maps frames onto Hermes' platform contract and
  maps `authority` onto `source.role_authorized`, so an untrusted sender can reach the agent as
  text and never as a command.
- **Durability by default.** A message is stored before it is delivered, re-offered if the
  adapter never acknowledged it, and retried per relay by republishing the *same* signed event —
  re-wrapping would change its id and show the peer a duplicate. Writes are batched onto a
  dedicated thread (WAL, `synchronous=NORMAL`) so a burst cannot stall the event loop; the
  measurements and the durability trade are in [`docs/store-writes.md`](docs/store-writes.md).
- **Coverage that is measured, not assumed.** Reads span the identity's follows, its NIP-65
  write relays and the sender app-relay defaults; per-relay event counts and last errors are in
  `hng status`, because "we read all relays" is a claim a status line should be able to prove.
- **Marmot stays Marmot — and has one owner.** MLS is `wn-agent`'s job; the gateway speaks its
  control socket as a proxy plane rather than reimplementing a protocol it does not own. The
  gateway is the owner of that socket and Hermes' own Marmot plugin is being retired
  ([`docs/marmot-ownership.md`](docs/marmot-ownership.md)); until the flip the plugin is the
  carrier and the plane attaches only when `marmot.enabled: true`. Both readers switched on at
  once is a startup error, never a warning (below). `wn-agent` remains the transport underneath
  either way.

The authoritative design is [`docs/gateway-spec.md`](docs/gateway-spec.md); host-side operations
are kept with the host's own infrastructure documentation rather than in this repository.
Where the current build differs from this
target is tracked in [`QUEUE.md`](QUEUE.md), not here — a README that doubles as a progress log
goes stale between commits.

## Pieces

- **Host notes** — the service unit, its socket and the relay gates that explain refusals are
  host-specific: they live with the host's infrastructure documentation, not here.
- **Conformance probes** — `scripts/probe_nip17_conformance.py` executes the NIP-17 traps this
  design has to respect: a wrap's `p` tag is the recipient identity key, wraps are backdated up
  to two days against a three-day read window, the watermark belongs on the inner rumor, NIP-42
  must sign as the identity, and the self-copy is never surfaced. `--live` adds the spec §13.1
  wire probe: a throwaway recipient, a real relay, read back and decrypted, so no probe message
  reaches the household inbox. `tests/test_conformance.py` exercises it.
- **Hermes plugin** — `integrations/hermes-plugin/` (manifest, adapter, `register(ctx)` shim), kept
  in step with the live copy under `~/.hermes/plugins/nostr/`.
- **Publish path** — [`docs/publish.md`](docs/publish.md): one signed event sent per relay
  through the bindings client, and what the retry guarantees.
- **Queue gate** — `scripts/queue_next.py` and `deploy/queue-gate.sh`: the cron queue worker's
  monitor source, so an idle tick costs no agent run (see "Working the queue" below).
- **Prior art** — `wn-agent`/mdk and Armada, credited below.

## Working the queue

`QUEUE.md` is where the remaining work is listed and what happened to it; the 20-minute Hermes cron
job works its first unchecked, non-BLOCKED item and leaves a commit behind as the receipt. The job
is monitor-gated, not prompt-only: `scripts/queue_next.py` runs *before* the agent and prints either
one idle line — `queue empty — nothing pending` — or the next item with the pending/blocked counts
and the whole hours since `QUEUE.md` last changed. Hermes suppresses the agent run while that output
is byte-identical to the last run that woke it, so an empty queue costs one script execution, no
model call and no tool attempts; the `waited:` hour bucket is also the retry valve for an item whose
run died without editing the file.

`hermes cron` only accepts monitor scripts from its own scripts directory, so
`deploy/queue-gate.sh` is the source of truth and `~/.hermes/scripts/queue-gate.sh` is a
pass-through copy (`cp` is the whole update step); the repo path is `QUEUE_REPO` with this
checkout as the default. `tests/test_queue_next.py` pins the parse, the one-line idle output, the
rules-text trap and wrapper/module equivalence. This is project tooling, not gateway surface — `hng`
is not involved.

## Quick start (five minutes)

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). One process, one identity; the systemd
unit is `deploy/hermes-nostr-gateway.service`. From a checkout:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[dev]'
cp config.example.yaml config.yaml
$EDITOR config.yaml                      # key: identity.secret_file (or a systemd credential)
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

`hng health` is the one call for anything that *reports* rather than investigates. Per
plane (`nip17`, `nip04`, `nip01`, `concord`, plus `marmot` when that plane is attached)
it answers with the last event and its age, the undelivered backlog, the policy blocks,
and whether the plane is degraded — with the reasons. `status` is about relays; `health`
is about traffic, and it degrades only on a fault: a backlog older than
`--pending-stale-minutes` (default 10), an outbound wrap whose retry TTL expired, or
every read relay down. It does not degrade on a quiet night, and it does not degrade on
`blocked`, because a blocked message is the policy working. `--json` prints the whole
frame — that is what the overnight cron narrates from, one call, no log parsing —
`--check` exits 1 when degraded or when the daemon is not running, and with no daemon the
counters still come from the store, labelled `down` and `history, not live state`.

Tests and lint need no relay and no secret:

```sh
.venv/bin/python -m pytest -q            # full suite, ~4 s
.venv/bin/ruff check src tests scripts
```

### The `nostr-sdk` build

Concord derives its stream keys from the NIP-44 v2 conversation key. That function is now
**upstream**: `nip44_v2_conversation_key`, merged in
[`nostrdevkit/nostr-sdk-ffi#96`](https://github.com/nostrdevkit/nostr-sdk-ffi/pull/96) at
`0f230cb` (the maintainer renamed it from the name it was proposed under). We consume that
build and carry **no local patch**: if the installed bindings lack the function, the first
Concord derivation raises `CryptoError` naming the required build rather than deriving ECDH
locally.

| | |
|---|---|
| Function | `nip44_v2_conversation_key(secret_key, public_key) -> bytes` |
| Upstream | `nostr-sdk-ffi` `0f230cb`, PR #96, merged 26 Oct 2026 |
| Wheel | built from that commit and pinned by direct URL in `requirements.txt` |
| sha256 | `d8fb8b5d5bd087976f4b888f192543c871e6ca455a3f839625c403dd5bba9626` |

The version number stays `0.45.1`, so `pip install nostr-sdk` or a resolver that installs the
dependency by name *succeeds* and quietly swaps in the PyPI release. That is why the
dependency is a direct URL with a hash: a clean venv cannot silently lose the function. If you
rebuild, update the URL and the hash in `requirements.txt` together.

## Container

`Dockerfile` builds the same package with no external transport binary: reads and
publishes both run on the `nostr-sdk` client inside the process, so the image downloads
nothing but Python packages at build time and carries no relay CLI. The image runs as
`gateway`, UID/GID 1000 by default; build with `--build-arg UID=$(id -u) --build-arg
GID=$(id -g)` when the host user that owns the mounted files is not 1000.

The read transport — one client, one subscription per filter set, NIP-42 in-process —
is described in `docs/transport-pool.md`.

There is no network listener, so the socket is published through a volume, not a port:

```sh
docker build -t hermes-nostr-gateway .

mkdir -p state
cat > state/config.yaml <<'YAML'
identity:
  secret_file: /run/secrets/nostr-secret.json
store: /var/lib/hermes-nostr-gateway/gateway.db
socket: /var/lib/hermes-nostr-gateway/gateway.sock
media:
  dir: /var/lib/hermes-nostr-gateway/media     # on the state volume, not the layer
relays:
  read: [wss://relay.example.com/chat, wss://nos.lol]
  publish: [wss://relay.example.com/chat, wss://nos.lol]
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
ruff check src tests scripts benchmarks
```

`pytest` includes `benchmarks/store_burst.py`'s claim as a test (a 500-event burst keeps
p99 loop lag under 50 ms); the script itself is there to re-measure it by hand on a
different disk.

`.ngit/act/workflows/test.yml` runs both on every push to `master` and every Nostr pull
request through the ngit-ci coordinator. It installs `requirements.txt` first and then
asserts that the installed `nostr-sdk` really exposes `nip44_v2_conversation_key`, so a
resolver that quietly swaps in the PyPI release of the same version fails a named step
instead of Concord at runtime. The repository's canonical home is Nostr, so there is no
`.github/workflows` copy for a mirror to drift from.

## Concord

Concord is a serverless, end-to-end encrypted community protocol: no server, no moderator
daemon, membership is keyed and a channel is a stream of sealed envelopes. The gateway treats
it as one more plane. An invite arrives inside a kind-1059 wrap whose inner kind is `3313`, is
recorded in `concord_invites`, and never surfaces as a message; channel traffic is decrypted
with the stream key derived from the NIP-44 v2 conversation key (`nip44_v2_conversation_key`).
A channel message is stored under `concord:<community>:<channel>`, and a reply to it is
published back into that channel: the send path routes on the conversation, so an answer never
lands in the DM of whoever happened to speak. `hng send --to concord:<community>:<channel>`
hands the reply to the running daemon, which is the process that holds the community's keys.

It is in production use, not experimental: it runs wherever this gateway runs, against the
same relays as every other plane. `status`, the startup log and `hng status` report it as one
of the attached planes like any other.

### Writing to the Control Plane

Reading the Control Plane takes only the `community_root` every member holds. Writing takes the
staff-held `control_root`, because the plane's wraps are signed by a keypair derived from it: a
member-signed wrap fails the address check and is dropped by relays and readers alike
(CORD-02 §2/§5). A staff Grant delivers that secret in its `control_wrap` — `epoch_be[8] ‖
control_root[32]`, NIP-44 under the granter↔member pairwise key, base64 — and it is adopted only
if it derives the `control_pk` the invite names for that epoch, so a stale secret fails closed
instead of minting wraps nobody can open.

`planes/concord/control.py` implements the write side: `parse_control_wrap` →
`check_control_root` → `build_channel`, with `verify_edition` proving the wrap verifies at the
plane's address and that the plaintext seal round-trips the rumor before anything is published.
An edition is a kind-3308 rumor in a kind-20014 **plaintext** seal — the rumor's serialized JSON
as the seal's content, which is what lets a compaction re-wrap the signed rumor with its
signature intact — and it carries `vsk` (entity type), `eid`, `ev`, and, when the actor is not
the owner, a `vac` citing the Grant edition whose rank the action claims (entity, version, and
`edition_hash` over the content as published).

Authority is still the Roster, not the key: holding `control_root` is a flood gate, and every
edition is judged by the sealed actor's rank. A demoted staffer whose Grant revoked their Roles
keeps only the ability to publish things everyone drops — which is why a Community that distrusts
a former staffer rotates the root rather than trusting the fold alone.

### Channel → project mapping

A channel is a context, not a repository. What tells an agent which checkout a channel's work
belongs to is a lookup the gateway does before it hands over the message:

```yaml
concord:
  projects:
    - channel: Ops                      # channel name, or its hex id
      path: ~/Projects/hermes-nostr-gateway
      name: hermes-nostr-gateway        # optional; defaults to the directory name
      community: f56b6914…              # optional; restrict to one community
```

- A channel matches on its id or its current name; an id match wins, so renaming a channel in
  the client does not move its project.
- An entry with `community` only matches that community; one without matches any.
- A path that does not exist is kept and reported, never silently ignored: an unmounted or
  moved checkout is a fact to see, not a config edit to guess at.

`hng projects` prints the map with each checkout's state (present, a git tree, whether it
carries an `AGENTS.md`); `hng projects --json` is the same for a script. `ConcordPlane.project_for()`
returns `{"name", "path"}` for a channel — the field an inbound channel message will carry once
channel ingestion is wired. Today the resolver and its admin surface exist; invites, not
messages, are what the plane reads.

## Relays and kinds

One subscription per relay, because each extra connection counts against the relay's
connection limiter. A Nostr filter is all-or-nothing at the relay, though: a REQ naming a kind
the relay refuses is closed whole (`filter validation failed: kind not allowed: 4`), so a relay
that rejects NIP-04 kind 4 would otherwise lose its kind-1059 DMs along with it. Hence a kind
set per relay:

- the default DM subscription is `1059` + NIP-04 `4`;
- `relays.read` may pin a relay's set — `- {url: wss://relay.example, kinds: [1059]}` — for a
  relay that refuses a kind rather than for one that simply has less traffic;
- if a relay still refuses a kind at runtime, the worker drops that kind and resubscribes
  immediately on the rest: never a kind-less filter, never a reconnect loop that cannot succeed.
  `hng status` shows the narrowed set in that relay's detail, and reports `refusing` when
  nothing is left to read.

**Peer inbox coverage.** A fixed read list cannot see a peer who publishes only into their own
kind-10050 inbox relays, and the miss is silent (spec §3). So the gateway also resolves the
10050 list of every pubkey named in `groups` — at startup, then every
`relays.peer_inbox.refresh_minutes` — and adds those relays to the DM read set:

```yaml
relays:
  peer_inbox:
    enabled: true          # the gate; on by default because coverage is correctness
    max_extra: 4           # hard cap on relays added, for the process lifetime
    refresh_minutes: 60    # 0 = resolve once at startup
    # lookup: [wss://relay.example.com/chat, wss://nos.lol]   # default: first 4 of read + publish
```

The bounds matter as much as the lookup:

- additions are deduplicated against `relays.read` on a normalised URL, so a relay already
  read costs nothing;
- `max_extra` caps the total for the life of the process. A relay is added once and never
  re-added; a relay the cap refused is named in the socket `status` op
  (`peer_inbox.skipped_by_cap`) rather than silently dropped;
- each added relay gets **one** DM worker, not the DM/feed/mention trio the configured read
  set uses, so the cap really is the connection bound;
- only `wss://` URLs are accepted, and the White Noise relays below are never added;
- a lookup that fails is recorded (`peer_inbox.errors`) and changes nothing: coverage never
  shrinks because a relay was slow or a peer rotated their list;
- a peer relay that refuses a kind negotiates exactly like any other relay: the kind is
  dropped and the subscription continues on the rest. A relay that refuses every kind is
  reported as a dead inbox and is not resubscribed.

`status` reports `peer_inbox.added`, `.dead`, `.skipped_by_cap`, `.errors` and `.last_refresh`,
and each worker carries a `purpose` (`dm`, `feed`, `mentions`, `peer-inbox`) so an extra
connection can be traced to a decision.

**White Noise relays belong to the Marmot plane.** `relay.eu.whitenoise.chat` and
`relay.us.whitenoise.chat` exist to carry White Noise's own groups, which `wn-agent` speaks for
us; they are not a general NIP-17 inbox, they refuse public kinds, and the gateway does not put
them in the DM or feed read set. They are named under `relays.whitenoise` for the Marmot plane
to use through `wn-agent`. The trade is deliberate: a DM that exists *only* there is not fetched
by the DM workers, and if one ever goes missing this is the first thing to check.

## Publishing

An outgoing message is signed once, in this process, and then sent to each relay through
the same `nostr-sdk` client that does the reading. `publisher.py` never re-wraps: the daemon
stores the signed wrap *before* the first attempt, and every retry — in the same call, or
from the outbox on a five-minute pass — resends those exact bytes. A relay that comes back
therefore sees the event it missed, and the peer sees one message, not two (spec §7).

What that buys beyond one less process per message per relay:

- NIP-42 is answered in-process by an authenticator holding our own key, so a restricted
  relay accepts the write without the secret visiting a child's environment;
- each relay's answer is recorded per relay and lands in `outbox_wraps`, which is what
  `status.retry.wraps` counts;
- a relay that has not answered within the OK timeout is sent the same event once more in
  the same call. That is also how the AUTH race resolves: a restricted relay sends its
  challenge around the first EVENT, so the second send is the one it accepts;
- the client's relay set is bounded (24, least-recently-used pruned, configured publish
  relays pinned), because a persistent client that keeps a socket per peer it has ever
  messaged runs out of file descriptors;
- `status.publish` names the transport (`mode: bindings`) and counts what it has sent and
  what failed.

Every publish goes through that client, in this process. There is no second publisher and
no config flag that selects one: the per-relay `nak event --auth` child is deleted, along
with the read-side `nak` workers before it. The design and the wire-level tests are in
[`docs/publish.md`](docs/publish.md).

## Marmot: one socket, one owner

The `marmot` plane is a proxy to `wn-agent`'s control socket. **The gateway is the owner of
that socket**: it holds the subscription, normalises group messages into the same frames as
every other plane, and sends out through the same `wn-agent` the phone talks to. Hermes' own
Marmot platform plugin is being retired once the gateway has carried the traffic cleanly for a
week — the decision, the flip and its rollback are one config line each way in
[`docs/marmot-ownership.md`](docs/marmot-ownership.md).

Two readers on one socket is not a second opinion, it is duplicate work: one group message
becomes two agent runs, two replies, and doubled side effects. So a configuration with both
switched on refuses to start:

```
refusing to start: marmot.enabled: true while Hermes' Marmot platform is still enabled
(platforms.marmot.enabled: true in /home/ubuntu/.hermes/config.yaml). Both read the same
wn-agent control socket, so every group message would reach the agent twice, …
```

The guard knows because `marmot.hermes_platform: auto` (the default) reads
`platforms.marmot.enabled` from Hermes' own config — `$HERMES_HOME/config.yaml`, default
`~/.hermes/config.yaml`, or `marmot.hermes_config` for a different Hermes home. `true`/`false`
pins the answer where there is no Hermes config to read (a container); a missing file is
*unknown* and never blocks a start. The socket's `status` op reports
`marmot.hermes_platform: {enabled, source}` — what the guard concluded and where it read it.

Until the flip the plugin is the carrier and `marmot.enabled` stays `false`; the plane then
does **not** attach, whatever socket is present. An idle plane is not a silent one: startup
logs

```
marmot plane idle: opt-in only: set marmot.enabled: true to attach it. …
```

and the socket's `status` op carries the same text under `marmot.reason` with
`marmot.enabled: false`. An explicit `marmot:<group>` send fails with that explanation rather
than looking like a bad target. If the flag *is* set and the plane is still absent, the reason
changes to `no wn-agent control socket at <path>` — that is a daemon or `home:` problem, not an
opt-in one.

`status` answers "what is attached?" from evidence, not from what happens to have been resolved
since the last start. On every (re)connect the plane takes the group ids of the Marmot messages
in its own store, re-verifies each with `group_info`, and reports them under
`marmot.conversations` (`group`, `messages`, `last_at`, `verified`, `member_count`); the
`marmot.enumeration` block records when that ran and names any group it could not verify, with
the error, so history for a group we have left is visible rather than dropped. That is also the
limit: wn-agent 0.10.4's control protocol has no group-list request (an unknown `group_list`
frame is not even answered), so a group this gateway has never exchanged a message with cannot
be listed until its first message arrives — the `source` line says so instead of leaving an
empty list to be misread.

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

## Media

Attachments are not a separate event kind. A sender puts NIP-92 `imeta` tags on the text
rumor — one per file — and the message frame carries the same metadata:

```json
{"type":"message","plane":"nip17","id":"…","content":"look at this",
 "attachments":[{"url":"https://blossom.example/<sha256>","sha256":"…","mime":"image/jpeg",
                 "size":184320,"alt":"the garden","status":"available"}]}
```

The gateway hands a message over with the sender's `imeta` claims attached and no bytes:
nothing is downloaded on receipt, so a hostile sender cannot spend our bandwidth by sending
a message. Delivery is where the agent's side joins in: the Hermes adapter calls
`media.get` for every attachment on a message it is about to hand the agent — before the
turn starts, because a turn that begins without the picture cannot go back for it — and
puts the resulting file in Hermes' cache as `media_urls` / `media_types`. A refused
attachment is named in the message text (`[attachment not delivered: <code>]`), never
silently dropped. `NOSTR_MEDIA=0` turns the fetch off for a deployment that does not want
attachments on disk — the message still arrives, noting `media_disabled`.

```json
{"op":"media.get","sha256":"<64 hex>"}     // or {"url": "…"}, or {"rumor_id": "…"}
```

```json
{"type":"media","op":"media.get","sha256":"…","path":"/…/media/…","mime":"image/jpeg",
 "size":184320,"cached":false,"url":"https://blossom.example/…","rumor_id":"…"}
```

`path` is a 0600 file in the cache (default `~/.local/state/hermes-nostr-gateway/media`,
which is inside the service unit's writable path); the bytes are
verified against `sha256` before the file is written, and a second call returns
`cached: true` without a request. `hng media <sha256|url>` takes the same path from a shell.

Caps live under `media:` in the config: 25 MiB per blob, four per message, a MIME allowlist
(`image/*`, `audio/*`, `video/mp4`, `application/pdf`, `text/plain`), and 512 MiB of cache
with the oldest blobs evicted first. The type is **sniffed from the bytes, never taken from
`m`**: a JPEG labelled `text/plain` is still a JPEG, and bytes that are not a recognised
type are refused rather than guessed (SVG is not recognised, so it arrives as `text/plain`
and is never presented as an image). Refusals are an error frame with a code:

```json
{"type":"error","op":"media.get","code":"too_large","error":"the sender declares 31457280 bytes, over the 26214400 byte cap"}
```

| code | meaning |
|---|---|
| `unknown_blob` | no message we hold references that sha256/url |
| `unverifiable` | the `imeta` tag carried no usable sha256 |
| `too_large` | declared size, `Content-Length`, or the streamed body is over the cap |
| `too_many_files` | that attachment is beyond the four-per-message cap |
| `type_unknown`, `type_not_allowed` | the bytes are not a type we hand to an agent |
| `hash_mismatch` | the server served bytes that are not the blob |
| `insecure_url`, `bad_url`, `host_not_allowed` | not a hash-addressed public https blob url |
| `not_found`, `fetch_failed` | every candidate failed; the message names the reason |
| `media_disabled` | `media.enabled: false` |

A peer's Blossom server is used only when the url ends in the blob's sha256 and resolves to
a public address, with redirects refused; configured servers are trusted as configured (a
local one may be http) and are tried by hash after the message's own url. That is what stops
the socket being a general-purpose fetcher while still reading attachments from the servers
peers actually use — and because the hash is checked, a hostile server can only fail to
serve the blob, never substitute content for it.

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
- **A bot peer is not a person.** Kind-0 `bot: true` caps a peer at digest reach, refuses our
  own text echoed back, and allows a handful of messages an hour — the three rules that stop
  two agents answering each other for ever (spec §17, "Bot peers").
- **Relay coverage is a correctness property.** Reading only the identity's `10050` misses DMs
  that exist only on the sender's app relays — measured, not theorised (spec §3).
- **The gateway never interprets content.** No fetching beyond a Blossom blob a stored
  message references and addresses by sha256 (lazy, capped, verified — see "Media"), no
  command parsing, no reply policy, no read receipts.

## Credits

- **Marmot / `wn-agent`** (`github.com/marmot-protocol/mdk`) — the agent-connector shape this
  follows, and the Marmot plane itself.
- **Armada** (soapbox.pub) — reference client for NIP-17 and Concord.
- The NIP authors, obviously.

## Licence

MIT — see [`LICENSE`](LICENSE).
