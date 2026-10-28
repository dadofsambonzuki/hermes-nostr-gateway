# Hermes Nostr Gateway

A Nostr transport hub for Hermes agents: every Nostr surface — DMs, public mentions, Concord
communities, Marmot groups — arrives on one local socket as one event stream, and one policy
decides what is worth waking an agent for.

Hermes has no Nostr platform of its own. This gives it one without pretending Nostr is a single
protocol: a NIP-17 DM, a legacy NIP-04 DM, a Concord channel message and a Marmot group post all
become the same `message` frame, carrying the same sender, conversation, reach and authority
fields.

## What runs

- **One daemon per identity.** `hng serve` holds the socket and one subscription per relay.
- **One socket.** NDJSON over a 0600 Unix socket, no network listener. A connection is checked
  with `SO_PEERCRED`, so it answers only the uid the daemon runs as.
- **One policy.** Reach (`immediate`, `digest(<interval>)`, `drop`) and authority
  (`instructions`, `none`) are decided per message from sender, plane and kind — in one place,
  not per transport. The two are independent: a stranger can reach the agent and still not
  instruct it.
- **One adapter.** `integrations/hermes-plugin/` is the Hermes platform plugin. It maps frames
  onto Hermes' platform contract and `authority` onto `source.role_authorized`, and it owns the
  outbound replay guard, so a reply Hermes redelivers is not published twice.
- **Durability.** A message is stored before it is delivered, replayed if the adapter never
  acknowledged it, and retried per relay by republishing the same signed event — never a re-wrap,
  which would change its id and show the peer a duplicate.
- **Coverage that is measured.** Reads span `relays.read` plus the kind-10050 inbox relays of
  every peer in `groups`; per-relay counts and last errors are in `hng status`, because "we read
  all relays" is a claim a status line should be able to prove.

## Planes

| plane | carries |
|---|---|
| `nip17` | DMs: 1059 giftwrap → 13 seal → 14 rumor, with reactions and NIP-92 media |
| `nip04` | legacy DMs (kind 4) |
| `nip01` | kind-1 mentions, and the authors and keywords in `feeds` |
| `concord` | serverless end-to-end encrypted communities: invites, channels, reactions |
| `marmot` | MLS groups, proxied to `wn-agent`'s control socket |

A digest batch arrives as an ordinary `message` frame on plane `digest`.

## Quick start

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). One process serves one identity.

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[dev]'
cp config.example.yaml config.yaml
$EDITOR config.yaml                     # identity.secret_file, relays, policies
.venv/bin/hng --config config.yaml status
.venv/bin/hng --config config.yaml serve
```

Install `requirements.txt` before the editable install: it is the only path that carries the
`nostr-sdk` wheel's URL and hash. The release of the same version does not expose
`nip44_v2_conversation_key`, which Concord derives its channel keys from, so a resolver that
picks the release up by name fails only at runtime.

`deploy/hermes-nostr-gateway.service` is the systemd unit; it reads the identity key from a
systemd credential rather than a file the service user can open. The Hermes plugin finds the
socket through `NOSTR_GATEWAY_SOCKET` or its `socket_path` setting; the daemon's default is
`$XDG_RUNTIME_DIR/hermes-nostr-gateway.sock`.

## Running it

`hng status` is about relays. For each relay in `relays.read` it asks the running daemon what
that worker is doing and prints one verdict — `connected`, `authenticating`, `refusing`, `down`,
`no worker`, `no events for 47m`, or `unknown` when the daemon is not running. `--json` gives the
same facts for a script, `--check` exits non-zero on an unhealthy relay or an unreachable daemon,
and `--quiet-minutes` sets the silence window.

`hng health` is about traffic. Per plane it reports the last event and its age, the undelivered
backlog, the policy blocks, and whether the plane is degraded and why. It degrades on a fault — a
stale backlog, an outbound retry whose TTL expired, every read relay down — not on a quiet night
and not on `blocked`, because a blocked message is the policy working. `--json` prints the whole
frame, which is what a cron narrates from; `--check` exits 1 when degraded.

The rest answer one question each: `hng recent`, `hng drops`, `hng listen --once`,
`hng send --to <conversation> --text <text>`, `hng media <sha256|url>`, `hng policy-test`,
`hng concord`, `hng ngit sync`, `hng projects`, `hng profiles`, `hng route`, `hng control`,
`hng replay-skip`.

### The socket

Ops in: `subscribe`, `ack`, `mark_read`, `send`, `status`, `health`, `history`, `drops`, `peers`,
`media.get`, `marmot.group`, `marmot.timeline`, `replay.skip`, `ping`.

Frames out: `message`, `blocked`, `delivery`, `reaction`, `concord_invite`, `media`, plus a reply
envelope per op (`status`, `health`, `history`, `drops`, `peers`, `ok`, `pong`, `error`). A
`blocked` frame carries a `reason` (`rate_limited`, `not_allowlisted`, `digest_quota`,
`route_key_missing`, `route_unresolved`) and never looks like chat.

## What it refuses

- **Untrusted senders instruct nothing.** The text still arrives; `authority: none` reaches the
  agent as data, never as a command.
- **A flood degrades to `blocked`.** One token bucket per sender, shared by every plane, so
  switching transport cannot earn a fresh burst. The message is still stored — the record
  matters — and never delivered. See `rate_limit` in the config.
- **A bot peer is not a person.** A peer whose kind-0 metadata says `bot` is held to digest reach,
  has its echo of our own text dropped, and gets a bounded number of messages an hour (`bots`).
- **`/queue` and `/guide` are privileges.** A leading `/queue <text>` holds a message for the next
  turn and `/guide <text>` steers the running one. Both are read only for a sender with
  `authority: instructions` and `reach: immediate`; anyone else's prefix stays literal text.
- **Nothing is fetched beyond an attachment a stored message names.** NIP-92 `imeta` is passed on
  as a claim; bytes are fetched only when the adapter asks with `media.get`, verified against the
  sha256, sniffed for type and capped (`media`).
- **Content is not interpreted.** No reply policy, no read receipts, no link previews.

## Where work and answers go

A Concord channel is a context, not a repository, so `concord.projects` maps a channel to a
checkout (`path`), a Hermes project (`project`) and a kanban board (`board`) — three names with
three jobs, none inferred from another. The same mapping drives `hng ngit sync`: issues on the
channel's NIP-34 repository become tasks on its board, and its pull requests are announced in the
channel. A trusted author queues work; anyone else's issue is held until a trusted npub answers
with `hng ngit answer`. See [`docs/ngit-bridge.md`](docs/ngit-bridge.md).

`profiles` decides which Hermes profile a conversation's turn runs as, and `routing` which
provider, model and *named* environment variable it uses. Both resolve `channel > plane >
default`, and both refuse a route they cannot honour rather than falling back to one.

`control` is where a confirmation is asked (`control.concord`, `control.nip17`,
`control.marmot`) and whose word needs none (`trusted_npubs`, global plus per plane, a union of
the two). A plane with no destination is reported as unconfigured, never guessed.

## Two rules that will bite you

**Marmot has one owner.** MLS is `wn-agent`'s job; the plane proxies its control socket. Two
readers on that socket deliver every group message twice, so `hng serve` refuses to start when
`marmot.enabled` is true and Hermes' own Marmot platform is also enabled —
`marmot.hermes_platform: auto` reads that from Hermes' config rather than copying the flag.
[`docs/marmot-ownership.md`](docs/marmot-ownership.md) has the flip and its rollback.

**A Concord invite joins only on a trusted word or a human's.** An invite from a
`control.trusted_npubs` key is accepted and the Guestbook join published; anyone else's is held,
the question is posted into `control.concord`, and `hng concord <invite-id>` is the human's yes.
Nothing joins silently, and a held invite stays visible even when there is nowhere to ask.

## Tests and CI

```sh
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests scripts benchmarks
```

Neither needs a relay or a secret. `.ngit/act/workflows/test.yml` runs both on every push and
every Nostr pull request through the ngit CI coordinator, and asserts that the installed
`nostr-sdk` really exposes `nip44_v2_conversation_key`, so a swapped resolver fails a named step
instead of Concord at runtime. Work lands as a `pr/<slug>` branch; the operator merges.

## Container

`Dockerfile` builds the same package with no transport binary. There is no network listener, so
the state directory is the interface: mount a volume at `/var/lib/hermes-nostr-gateway` holding
the config, store, media cache and socket, and the identity secret at
`/run/secrets/nostr-secret.json`. Build with `--build-arg UID=$(id -u) --build-arg GID=$(id -g)`
when the host user that owns those files is not 1000. The entrypoint is `hng`, so
`docker run <image> status` is a one-off check and the default argument is `serve`.

## Docs

| | |
|---|---|
| [`docs/gateway-spec.md`](docs/gateway-spec.md) | planes, socket API and trust model — the design |
| [`docs/decisions.md`](docs/decisions.md) | calls already made, and why |
| [`docs/ngit-bridge.md`](docs/ngit-bridge.md) | issues as work, PRs signalled in a channel |
| [`docs/marmot-ownership.md`](docs/marmot-ownership.md) | the one-owner rule and the flip |
| [`docs/publish.md`](docs/publish.md) | the write path and its retry guarantees |
| [`docs/transport-pool.md`](docs/transport-pool.md) | the read path: one client, one subscription per filter |
| [`docs/store-writes.md`](docs/store-writes.md) | WAL, batching, and the measured latency |
| [`docs/digest-cursor.md`](docs/digest-cursor.md) | digest batching and the ack cursor |

Host-side operations — the service unit, relay web-of-trust gates, key handling — are kept with
the host's own infrastructure notes, not in this repository.

## Credits

`wn-agent`/mdk for the Marmot plane and the agent-connector shape it follows; Armada as a
reference client for NIP-17 and Concord; the NIP authors.

MIT — see [`LICENSE`](LICENSE).
