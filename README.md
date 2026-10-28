# Hermes Nostr Gateway

A Nostr transport hub for Hermes agents: every Nostr surface — DMs, public mentions, Concord
communities, Marmot groups — arrives on one local socket as one event stream, and one policy
decides what is worth waking an agent for. It also carries work the other way: a channel's
repository is reconciled into kanban tasks and channel notices on a schedule.

## Two engines

**Planes are live.** A message arrives, policy decides, a frame goes to Hermes — one message at a
time, over one socket. A digest batch arrives the same way. A plane fails **per message**: one
message can be blocked by policy, dropped, or left undelivered and replayed when the adapter
reconnects, and nothing else waits on it.

**Bridges are batch.** A cron pass reads repository events to a cursor and reconciles them into
kanban tasks and channel notices. Their durable state is their own — links, cursors, an outbox —
so a pass can stop halfway and the next one carries on. A bridge fails **per pass**: the cursor
stays behind the first event it could not deal with, an undelivered notice stays queued, and a
board that is locked or missing is reported as that pass's error rather than a silent gap.

| | **planes** | **bridges** |
|---|---|---|
| triggered by | a message arriving | a cron pass |
| rhythm | one message at a time, live | batch, up to a cursor |
| writes to | the socket (`message`, `blocked`, `delivery`) | kanban tasks, channel notices |
| state | the store, replayed on reconnect | links, cursors and an outbox of its own |
| fails | per message | per pass |

Both run on one spine: the same `identity`, the same `relays`, the same policy, the same `control`
pane, and the same `concord.projects` mapping — channel → checkout → project → board. A second
home for the bridge would mean a second identity and a second copy of that map, which is why the
two live in one repository. Why both engines belong in one repository is a decision, not an
accident: [`docs/decisions.md`](docs/decisions.md) is where this project's calls and their
reasons live.

## Planes

Hermes has no Nostr platform of its own. This gives it one without pretending Nostr is a single
protocol: a NIP-17 DM, a legacy NIP-04 DM, a Concord channel message and a Marmot group post all
become the same `message` frame, carrying the same sender, conversation, reach and authority
fields.

| plane | carries |
|---|---|
| `nip17` | DMs: 1059 giftwrap → 13 seal → 14 rumor, with reactions and NIP-92 media |
| `nip04` | legacy DMs (kind 4) |
| `nip01` | kind-1 mentions, and the authors and keywords in `feeds` |
| `concord` | serverless end-to-end encrypted communities: invites, channels, reactions |
| `marmot` | MLS groups, proxied to `wn-agent`'s control socket |

A digest batch arrives as an ordinary `message` frame on plane `digest`.

Under them:

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

### The socket

Ops in: `subscribe`, `ack`, `mark_read`, `send`, `status`, `health`, `history`, `drops`, `peers`,
`media.get`, `marmot.group`, `marmot.timeline`, `replay.skip`, `ping`.

Frames out: `message`, `blocked`, `delivery`, `reaction`, `concord_invite`, `media`, plus a reply
envelope per op (`subscribed`, `status`, `health`, `history`, `drops`, `peers`, `ok`, `pong`,
`error`). A `blocked` frame carries a `reason` (`rate_limited`, `not_allowlisted`, `digest_quota`,
`route_key_missing`, `route_unresolved`) and never looks like chat.

## Bridges: the work half

The bridge in this repository is the ngit one. `hng ngit sync --channel <conversation>` reads a
channel's NIP-34 repository to a cursor and reconciles what it finds into that channel's board and
that channel; the conversation is the only address a pass needs, because `concord.projects`
supplies the checkout and the board:

| event | becomes |
|---|---|
| kind-1621 issue | a task on the channel's board |
| kind-1622 comment | a comment on that task, in `created_at` order, once |
| kind-1618 PR | one notice in the channel, and one review task on the board |
| kind-1631 applied | a `merged as <commit>` follow-up; it completes the review task |

An issue from a trusted npub (`control.trusted_npubs`, or the concord plane's own list) is created
`ready` for the dispatcher. Anyone else's is created `blocked` and the channel gets one question,
which a trusted npub answers with `hng ngit answer <task-id|nevent> --yes|--no --by <npub>` — the
author of an issue does not thereby authorise it.

A PR's review task can run on a different model from routine work: `ngit.review_model` (with
`ngit.review_provider`) is written to the task's own model override — the column the dispatcher
already honours — and a channel's `concord.projects` entry can set its own for that channel alone.
The task ends in a verdict and a comment on the task; it never merges, and the merge event closes
it.

Notices are queued in `ngit_outbox` before they are sent, each with an idempotency key the daemon
records, so a busy daemon delays a notice instead of losing it and a lost acknowledgement cannot
post twice. The cursor advances past what was queued or delivered and stops short of what was
deferred, so a backlog is carried, not stepped over.

[`docs/ngit-bridge.md`](docs/ngit-bridge.md) has the configuration, the cron shape and every
recovery path.

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

The Hermes plugin finds the socket through `NOSTR_GATEWAY_SOCKET` or its `socket_path` setting;
the daemon's default is `$XDG_RUNTIME_DIR/hermes-nostr-gateway.sock`.

## Configuration

`config.example.yaml` carries the comments; this is the map.

- **`relays`** — the read set, the publish floor, and `peer_inbox` coverage of peers' own inbox
  relays.
- **`identity`** — `secret_file`, or `secret_credential` under systemd so the key is never a file
  the service user can open.
- **Policy** — `groups`, `policies` (match on group, kind, DM or mention; decide `reach` and
  `authority`) and `defaults`, plus the bounded exceptions `rate_limit` and `bots`.
- **`control`** — where a confirmation is asked (`control.concord`, `control.nip17`,
  `control.marmot`) and whose word needs none (`trusted_npubs`, global plus per plane, a union of
  the two). A plane with no destination is reported as unconfigured, never guessed.
- **`profiles`** — which Hermes profile a conversation's turn runs as.
- **`routing`** — which provider, model and *named* environment variable it uses. Both this and
  `profiles` resolve `channel > plane > default`, and both refuse a route they cannot honour
  rather than falling back to one.
- **`concord.projects`** — where a channel's work lives: the checkout (`path`), the Hermes project
  (`project`) and the kanban board (`board`). Three names with three jobs, none inferred from
  another; a board without a project is a config error, because nothing anchors the worktree.
- **`ngit`** — the bridge, a sibling block rather than a corner of Concord: `assignee`, `reviews`,
  `review_assignee`, `review_model` / `review_provider`, `relays`, `lookback_hours`, `issues`,
  `prs`, `priority`, `max_new`, `max_prs`.

## Operations

Two runtimes, and each has its own health.

**The daemon** is `hng serve`, one process per identity, normally under systemd
(`deploy/hermes-nostr-gateway.service`), which reads the identity key from a credential rather
than a file the service user can open. `hng status` is about relays: for each relay in
`relays.read` it asks the running daemon what that worker is doing and prints one verdict —
`connected`, `authenticating`, `refusing`, `down`, `no worker`, `no events for 47m`, or `unknown`
when the daemon is not running. `hng health` is about traffic: per plane it reports the last event
and its age, the undelivered backlog, the policy blocks, and whether the plane is degraded and
why. It degrades on a fault — a stale backlog, an outbound retry whose TTL expired, every read
relay down — not on a quiet night and not on `blocked`, because a blocked message is the policy
working. `--json` gives either as a frame for a script, `--check` exits non-zero on an unhealthy
relay, an unreachable daemon or a degraded plane, and `--quiet-minutes` sets the silence window.

**The bridge** is a cron pass per channel, staggered so two passes do not reach for the same board
at the same moment:

```cron
*/10 * * * *    cd /path/to/gateway && hng ngit sync --channel concord:<community>:<channel-a> --json >> /var/log/hng-ngit.log 2>&1
5-55/10 * * * * cd /path/to/gateway && hng ngit sync --channel concord:<community>:<channel-b> --json >> /var/log/hng-ngit.log 2>&1
```

Its health is the pass's report: exit 0 when every message was delivered or queued for the daemon,
1 when something was refused or could not be read, 2 when the configuration is wrong and the pass
touched nothing. `--dry-run` reports what would happen and writes nothing.

The rest answer one question each: `hng recent`, `hng drops`, `hng listen --once`,
`hng send --to <conversation> --text <text>`, `hng media <sha256|url>`, `hng policy-test`,
`hng concord`, `hng projects`, `hng profiles`, `hng route`, `hng control`, `hng replay-skip`.

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
| [`docs/gateway-spec.md`](docs/gateway-spec.md) | planes, relay policy, socket API and trust model — the design |
| [`docs/decisions.md`](docs/decisions.md) | calls already made, and why |
| [`docs/ngit-bridge.md`](docs/ngit-bridge.md) | the bridge: issues as work, PRs signalled in a channel |
| [`docs/marmot-ownership.md`](docs/marmot-ownership.md) | the one-owner rule and the flip |
| [`docs/publish.md`](docs/publish.md) | the write path and its retry guarantees |
| [`docs/transport-pool.md`](docs/transport-pool.md) | the read path: one client, one subscription per filter |
| [`docs/store-writes.md`](docs/store-writes.md) | WAL, batching, and the measured latency |
| [`docs/digest-cursor.md`](docs/digest-cursor.md) | digest batching and the ack cursor |

Host-side operations — relay web-of-trust gates, key handling — are kept with the host's own
infrastructure notes, not in this repository.

## Credits

`wn-agent`/mdk for the Marmot plane and the agent-connector shape it follows; Armada as a
reference client for NIP-17 and Concord; the NIP authors.

MIT — see [`LICENSE`](LICENSE).
