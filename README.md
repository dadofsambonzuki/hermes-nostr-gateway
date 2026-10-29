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
  text and never as a command. It also owns the outbound replay guard: Hermes' delivery ledger
  re-sends a reply it could not prove landed, wrapped in a `♻️ Recovered reply` marker, so the
  adapter remembers what it actually put on the wire and drops an identical send to the same
  chat — inside a window (`NOSTR_DEDUPE_WINDOW_S`, default 60s) for ordinary chat, and for good
  for a marker-wrapped redelivery. A reply that may never have left is never recorded, so a
  genuinely lost one is still redelivered exactly once.
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

The authoritative design is [`docs/gateway-spec.md`](docs/gateway-spec.md) and the standing
decisions are in [`docs/decisions.md`](docs/decisions.md); host-side operations are kept with the
host's own infrastructure documentation rather than in this repository. Remaining work is not
tracked in this tree either — it is a task on the host's kanban board (see "Where the work
lives"), so a README that doubles as a progress log cannot go stale between commits.

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
- **Standing decisions** — [`docs/decisions.md`](docs/decisions.md): the calls already made
  (the Marmot-only relay set, no names in the tree, Concord is supported), so they are not
  re-opened by accident.
- **Prior art** — `wn-agent`/mdk and Armada, credited below.

## Where the work lives

Remaining work is not a checklist in this tree. It is a set of cards on this host's Hermes
kanban board — the `gateway` board, bound to this project — and the board's dispatcher is what
runs them: a card gets a git worktree, a branch and an agent, in parallel with its siblings, and
an empty board costs nothing at all because there is no scheduled prompt to spend.

What that means for anyone adding work:

- **One card, one scoped PR.** The card body names the gate (`ruff check src tests scripts
  benchmarks`, `python -m pytest -q` on the checkout's own venv) and the branch, `pr/<slug>`.
  Nothing lands on `master` directly, and the operator merges.
- **A question is a block, not a TODO.** A card that needs a decision is blocked on the board
  with the question in the comment thread; the answer becomes a line in
  [`docs/decisions.md`](docs/decisions.md) when it is meant to hold.
- **The card body carries the decisions it depends on.** A worker cannot see its siblings, so
  anything two cards must agree on is settled once and written into both.

This repository used to carry `QUEUE.md` and a 20-minute cron worker that walked it, with a
monitor script so an idle queue cost no model call. All three are gone: a queue file only one
host could see was a second source of truth, and the board's dispatcher already does the
gating the monitor script was written for.

## Quick start (five minutes)

Python 3.11+ and [`uv`](https://docs.astral.sh/uv/). One process, one identity; the systemd
unit is `deploy/hermes-nostr-gateway.service`. From a checkout:

```sh
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e '.[dev]'
cp config.example.yaml config.yaml
$EDITOR config.yaml                      # key: identity.secret_file (or a systemd credential)
.venv/bin/hng --config config.yaml doctor
.venv/bin/hng --config config.yaml status
.venv/bin/hng --config config.yaml serve
```

`hng doctor` is the one preflight, and it replaces six commands run in a remembered
order: the config parses, the identity key reads, the store is writable, the configured
socket path expands and the daemon answers, `ngit` is on `PATH` at or above its floor,
every `concord.projects` checkout is a git repository carrying a NIP-34 address, each
declared project and board exists and is bound, cron carries a pass for every channel that
queues work, and the watcher reports its state. Every failing check names its fix;
`--json` is the same shape for a script, and the exit code is 1 when anything failed.
`hng skill install` copies the repository's own generic skill
(`skills/nostr-gateway/SKILL.md`: the verbs, the failure vocabulary and the traps) into
`$HERMES_HOME/skills/nostr-gateway/`, so an agent on a fresh host does not have to
rediscover them; `--print` writes the same text to stdout and `--profile <name>` installs
into that profile's tree instead of the shared one.

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

### Joining a community: a trusted word, or a human's

An invite is verified and recorded the moment it arrives, and then something has to decide
whether this gateway joins. That decision is the control pane's `trusted_npubs` and nothing
else — no second list to keep in step:

- an inviter in `control.trusted_npubs` (the global list, or the `concord` plane's own list,
  which is a union) is accepted: the gateway publishes the Guestbook join and records
  `joined`;
- anyone else's invite is **held**: nothing joins, and the question — who invited, which
  community, which channels, and the exact command that accepts it — is posted into
  `control.concord`, which is how `#ops` receives it. With no `control.concord` destination
  the invite is still held and the daemon logs that there is nowhere to ask, because a
  confirmation that cannot be delivered must not look like one that was.

Nothing joins silently. Every invite row carries its state (`held`, `joining`, `joined`,
`failed`), who decided it (`auto` for a trusted inviter, `cli` for a human), why, and
whether the question reached the control pane. `hng concord` lists them and `hng status`
counts the held ones; `hng concord <invite-id>` is the human's yes, publishing the same
Guestbook join the daemon would have published and recording it as `cli`. A re-invite for a
community this gateway has already joined (one owner action mints one invite per room) is
recorded as joined without publishing a second join.

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
belongs to — and, when the channel is driven by a queue, where that work is filed — is a lookup
the gateway does before it hands over the message:

```yaml
concord:
  projects:
    - channel: example-channel          # channel name, or its hex id
      path: ~/Projects/example          # the checkout
      name: example                     # optional; defaults to the directory name
      community: <community id>         # optional; restrict to one community
      project: example-project          # optional; Hermes project (worktrees, branches)
      board: example-channel            # optional; kanban board this channel queues on
```

`path`, `project` and `board` are three names with three jobs, and none is inferred from another.
`path` is where the code is. `project` is the Hermes project: per-profile state that anchors a
worktree and its branch convention, which is what queued work needs to land anywhere. `board` is
the queue the work is filed on. A checkout with no `board` is manual work, and a channel with no
`board` is simply not kanban-driven — nothing is created because a channel exists. A `board`
without a `project` is a config error, because nothing anchors the worktree, so the entry is
reported and never resolved rather than half-used.

- A channel matches on its id or its current name; an id match wins, so renaming a channel in
  the client does not move its project.
- An entry with `community` only matches that community; one without matches any.
- A path that does not exist is kept and reported, never silently ignored: an unmounted or
  moved checkout is a fact to see, not a config edit to guess at.

`hng projects` prints the map: each checkout's state (present, a git tree, whether it carries an
`AGENTS.md`) and the project and board it declares. `hng projects --json` is the same for a
script, so an integration reads the mapping instead of carrying board slugs and repo paths of its
own. `ConcordPlane.project_for()` returns `{"name", "path"}` for a channel — the field an inbound
channel message will carry once channel ingestion is wired. Today the resolver and its admin
surface exist; invites, not messages, are what the plane reads.

`hng projects --check` asks the question the map implies, per channel: is the checkout there, is
the project registered, is there a board and is it bound to that project. It names every missing
piece and exits 1 if there is one; `--check --json` carries the same verdicts. That read is
read-only (`$HERMES_HOME`, then `~/.hermes`) and creates nothing — `hng serve` logs the same gaps
at startup and carries on, because a broken mapping must not stop the gateway reading messages.

By convention a channel bound to a project gets one board, named after the channel, with the
board's `--default-workdir` set to the checkout. Two channels that share a checkout get two
queues: a channel is a context, and one queue for two rooms hides whose work is whose.

### ngit: issues as work, PRs in the channel

A channel's checkout is usually also a NIP-34 repository, where its issues and pull requests
live on Nostr. `hng ngit sync` runs the bridge in both directions off that *one* mapping — no
second table of board slugs and repo paths:

```
inbound   kind-1621 issue    -> a task on the channel's board
          kind-1622 comment  -> a task comment, in the order the issue carries them
outbound  kind-1618 PR       -> one "ready for review" message and one review task
          kind-1631 applied  -> one "merged as <commit>" follow-up, closing that task
```

```bash
hng ngit sync --channel concord:<community>:<channel>          # repo and board from the mapping
hng ngit sync --channel ... --repo ~/Projects/example --board example-channel   # explicit
```

The bridge shells out to `ngit`, so that binary is a pinned dependency: a version at or
above **3.0.4** must be on `PATH` (the same floor `hng doctor` checks and names, with the
install hint, in its `ngit` check). A missing or older binary fails at the preflight of
`hng ngit sync`, before any pass work, rather than in a cron log at 03:00 — and after the
configuration has been checked, because a bad config does not depend on what is installed.

An issue from a **trusted npub** (`control.trusted_npubs`, or the concord plane's own list)
becomes a task `ready` for the dispatcher, and the channel gets one notice line,
`issue <nevent> -> <task id>`. Any other author is held: the task is created `blocked` and
one question is posted into the channel with the issue, the author and what a `yes` would do.
A stranger cannot put work in the queue by filing an issue, and nobody's issue is worked
silently. The answer is a verb, not a memory:

```bash
hng ngit answer <task-id|nevent> --yes --by <trusted npub>    # -> ready, with an audit comment
hng ngit answer <task-id|nevent> --no  --by <trusted npub> --reason "not our roadmap"
```

A `no` leaves the task blocked with the reason recorded, and the issue is never asked about
again. Only a trusted npub can answer: the person who filed the issue does not thereby get to
authorise it. The question is durable state — the blocked task plus a `ngit_links` row — so a
restart does not forget it and a re-poll does not ask twice.

A PR signal names the author every time, including when this gateway opened it, and each PR
is reported once: `ngit_links` dedupes by PR id, and a merge that arrives for a PR the bridge
never announced posts one "merged" line rather than replaying history. A PR also carries a
**review task** (`ngit:pr:<event id>`) so "should this merge" has an answer on the board
rather than in scrollback: the body names the PR, its branches and the brief — read the
diff, run `ruff check` and `pytest`, report a verdict with reasons, never merge — and the
merge message closes that task. `ngit.review_model` (with `ngit.review_provider`) — or a
`review_model` on the channel's `concord.projects` entry, which beats it — puts the task on
a stronger model through the board's own per-task override, without a second profile.
Closing an issue on Nostr does not close the task; the bridge imports open issues and
narrates PRs, and a board's lifecycle stays the board's.

Channel messages are queued before they are sent (`ngit_outbox`) and marked sent only when the
daemon took them, so a gateway busy with a turn delays a notice instead of losing it: the pass
reports it pending and the next pass delivers it. Each notice carries a stable idempotency key,
and the daemon answers a repeated one with the message it already published — a retry after a
lost acknowledgement cannot post twice.

State lives in the gateway store (`ngit_links`, `ngit_comments`, `ngit_cursors`,
`ngit_outbox`): what each event became, which comments are already on the task, how far each
repo has been read, and which notices are still waiting for the daemon. A task deleted by hand
keeps its `deleted` link, so the next sync does not silently recreate it; pass `--recreate`
when you want it back. `ngit.max_new` (default 20) spreads a backlog over polls instead of
filling the board and the channel in one pass. See
[docs/ngit-bridge.md](docs/ngit-bridge.md) for the config, the cron shape and the recovery
paths.

The ten-minute cron pass is the floor, not the whole story: `hng serve` also **watches** the
mapped repositories — a `#a` subscription for kinds 1621/1622/1618/1619/1631, nothing else —
and wakes `hng ngit sync` when something changes, coalescing a channel's events for 30s and
running one pass per channel at a time. It is an accelerator, not a second decision-maker: the
watcher writes no cursor, no task and no notice; the pass still reads the repository and decides.
Cron stays for restarts, relays that did not deliver and nudges lost while the daemon was down.
`ngit.watch: false` is exactly the cron-only behaviour; `hng status` shows what the watcher is
doing and what it has woken.

### Control pane

Where the gateway asks before it acts, and whose word it takes without asking. One
destination per plane, so a confirmation goes to the room that job belongs to rather than
to whichever channel happened to be open:

```yaml
control:
  concord: concord:<community>:<channel>   # e.g. the #ops channel
  nip17: npub1...                          # a DM peer that answers confirmations
  marmot: <group id>                       # a White Noise group
  trusted_npubs:                           # trusted in every plane, global list
    - npub1...
  planes:
    concord:
      trusted_npubs:                       # trusted here as well, not instead
        - npub1...
```

- A plane with no destination is reported as `not configured`: a confirmation that cannot
  be delivered must not be silently dropped, and nothing guesses a default room.
- A plane's `trusted_npubs` is a **union** with the global list, so adding a key for one
  plane never removes the keys trusted everywhere.
- Keys are normalised to hex, so an `npub` in config matches the hex a frame carries; an
  entry that is not a key is reported at load time rather than ignored.
- `hng control` prints the resolved pane per plane (and `--json` for a script); the daemon
  logs the same line at startup.
- Concord invites use both halves: a trusted inviter's invite joins without asking, anyone
  else's is held and posted into `control.concord` for a human (see *Joining a community*).

### Profiles

A Hermes profile is a whole instance — its own config, sessions, skills and memory, rooted at
its own `HERMES_HOME` — so one plane or one channel can run as a different agent identity while
the same daemon serves everything else as the default profile:

```yaml
profiles:
  default: default                 # required with the block: every conversation ends here
  planes:
    marmot: work
  channels:
    - community: <community id>    # optional; restrict this rule to one community
      channel: <channel id or name>
      profile: ops
```

Precedence is channel > plane > default, and it is total: `profiles.default` is required with
the block, so every conversation resolves to exactly one rule. A channel rule matches on its id
before its name — a renamed channel keeps its profile — and a rule with `community` only matches
that community. `hng route <conversation>` prints the rule that won, what it beat, and the home
it resolves to; `hng profiles` prints the whole table and `--check` exits 1 when a route names a
profile Hermes cannot run a turn as.

**The runtime is Hermes'**, not this daemon's, and nothing here re-implements it. Hermes already
routes a platform chat to a profile through `gateway.profile_routes`, and a routed turn gets that
profile's config, skills, memory and provider keys, its own `profiles/<name>/state.db`, and the
`agent:<profile>:...` session namespace. What Hermes cannot key on is a *plane* or a *Concord
channel by name* — those are names this gateway defines — so this block is the route for this
gateway's own conversations, and the frame carries the answer. The Hermes plugin sets it on the
`SessionSource`, which is the same field `profile_routes` stamps; a route change never migrates a
session, because the namespace is the profile's.

- A route naming a profile Hermes does not have refuses the start; a profile deleted while the
  daemon runs refuses the frame (it never falls back to the default profile). `hng profiles
  --check` reports the same without starting anything.
- Creating or configuring a profile is `hermes profile create <name>` / `hermes -p <name>
  config` — never this file, and never this gateway.
- Hermes has to be multiplexing (`gateway.multiplex_profiles` on, which is its default) and the
  profile must not opt out with `gateway.standalone: true`; `hng profiles --check` says which of
  those is wrong.
- The control pane is not re-routed: a confirmation for a routed channel still goes to the
  conversation that asked and to `control.<plane>`, and its approval prompts stay answerable
  because the prompt's session key is built for the identity the turn ran under.
- Every profile replies as this gateway's own Nostr key. A profile is an agent identity, not a
  second transport: give it its own gateway if it needs its own key.
- A digest batch is one frame across planes and has no single profile, so it runs as the default
  profile. Immediate-reach traffic — what a routed channel normally carries — always carries its
  profile.


## Routing a turn: provider, model and key

Without configuration every turn runs on whatever the agent is configured with. A `routing` block
lets one conversation ask for a specific provider, model and credential instead: a cheap model for
a noisy feed, a stronger one for a work channel, a dedicated key so one channel's usage is
accountable.

```yaml
routing:
  models:                                        # the catalogue: the only model names usable here
    - model-a
    - model-b
  default: {provider: provider-a, model: model-a, key: GATEWAY_MODEL_KEY}
  planes:
    concord: {provider: provider-a, model: model-b, key: CONCORD_MODEL_KEY}
    nip17: {provider: provider-a, model: model-a}   # no key: the agent's own credential
  channels:                                      # Concord only: a channel id, or its current name
    - community: <community id>                  # optional; restrict the rule to one community
      channel: <channel id or name>
      provider: provider-b
      model: model-b
      key: WORK_CHANNEL_KEY
  # env_file: ~/.config/hermes-nostr-gateway/.env   # default: a .env beside the config
```

Precedence is **channel > plane > default**, and it is total: `routing.default` is required once
the block exists, so every conversation resolves to exactly one rule and nothing falls through to
a guess. A channel rule matches on id before name, so renaming a channel keeps its route.

- `key` is the **name** of an environment variable (or of an entry in `routing.env_file`), never a
  value. It is read when the turn is about to run and never logged, never put in a frame and never
  in an error. A rule that looks like it holds a token is refused at load *without echoing it*. A
  name that cannot be read fails the turn; it never borrows another rule's credential.
- `routing.models` is the catalogue. A rule naming a model that is not in it is a load error, not a
  fallback: a turn billed to the wrong model is worse than a start that stops. `hng serve` refuses
  such a block (exit 2); `hng route --check` reports the same problems without starting anything.
- A channel rule naming a channel the gateway does not hold is **unresolved**, not the plane's
  route. A route is configuration, not protocol: deleting a channel does not rewrite the file, so
  the stale entry is what has to show. (`--check` names it.)

`hng route` prints the table with each rule's provider, model, key name and whether that key is
set. `hng route <conversation>` answers the question behind a wrong-looking answer — which rule won,
what it beat, and whether the key it names is there:

```
$ hng route concord:<community>:<channel id>
concord:<community>:<channel id>  (concord)
  provider   provider-b
  model      model-b
  key        $WORK_CHANNEL_KEY (set)
  rule       channel rule routing.channels[0] beats routing.planes.concord and routing.default
```

The route travels on the inbound frame (`route`: provider, model, the key's *name*, and which rule
won), so the adapter knows what the turn asks for before it runs. A route the gateway cannot honour
becomes a `blocked` frame with `reason: route_key_missing` or `route_unresolved` — never a message,
because a turn that silently runs on another model or another account is the failure this exists to
prevent — and the row stays unacknowledged, so the turn runs once the key is exported or the stale
entry corrected. The adapter hands the route to the host on the event, reads the credential itself,
and reports the route it used on its `send`: the reply's row and the `delivery` frame carry it, so
`hng recent` shows which provider and model actually answered. Applying a provider *inside* the
agent is Hermes' resolution and stays there; this is the gateway deciding what to ask for.
See `docs/decisions.md`, "A route travels on the frame".


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

## Holding a message back: `/queue` and `/guide`

A message that lands while the agent is mid-turn arrives as another instruction, which is
either an interruption or a line lost among the backlog. Two prefixes let the sender choose:

- **`/queue <text>`** — hold it, and deliver it as its own turn once the current one finishes.
  "I am not interrupting you; do this next."
- **`/guide <text>`** — steer it into the turn that is already running, as guidance rather than
  a new request. "Change what you are doing now."

"Current" is per conversation, because that is what a Hermes session is: a `/queue` from one
room cannot hold a message behind another room's turn.

Both are read in the daemon, after the policy has decided the sender's reach and authority and
before the frame is built. Only a sender the policy gives **`authority: instructions` and
`reach: immediate`** can use them: anyone else's `/queue ...` or `/guide ...` is stored exactly
as typed and reaches the agent as data, because a stranger must not be able to queue work or
steer a turn. A sender held to a digest — the loop tier — keeps its text as typed too: a digest
is a batch, not a turn, so there is nothing there to hold behind.

The prefix is stripped before the message is stored, and the row keeps which one it was:

```json
{"type":"message","plane":"nip17","id":"…","content":"read the log after this",
 "reach":"immediate","mode":"queue","authority":"instructions"}
```

`mode` is `normal`, `queue` or `guide`, and a replay carries it too — a message the adapter
never acknowledged arrives as the held message it was, not as a fresh instruction. Dedupe runs
before this path, so the same wrapper arriving on two relays cannot queue twice. The command is
only read at the very start of a message, in lowercase, and only when words follow it:
`/queue` on its own, `/queuex`, `/Queue this` and a command named mid-sentence are all ordinary
text.

Holding is Hermes' job, not the gateway's: the adapter re-addresses the words to Hermes' own
`/queue` and `/steer` commands, whose per-command busy handlers do the holding and the mid-turn
injection. `display.busy_input_mode` sets what a *plain* message does while the agent is busy
(by default it interrupts the turn); a prefix is how one sender chooses for one message, and the
acknowledgement ("Queued for the next turn") comes back from Hermes with it. Both commands fall
back to a normal turn when nothing is running, so a `/guide` that finds no turn to steer is
answered rather than dropped.

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
- **The gateway does not interpret content.** No fetching beyond a Blossom blob a stored
  message references and addresses by sha256 (lazy, capped, verified — see "Media"), no reply
  policy, no read receipts. The one exception is routing: a leading `/queue` or `/guide` is
  read, stripped and recorded as `mode`, after the policy and for an instructing sender only
  (see "Holding a message back"). Nothing else in a message is parsed.

## Credits

- **Marmot / `wn-agent`** (`github.com/marmot-protocol/mdk`) — the agent-connector shape this
  follows, and the Marmot plane itself.
- **Armada** (soapbox.pub) — reference client for NIP-17 and Concord.
- The NIP authors, obviously.

## Licence

MIT — see [`LICENSE`](LICENSE).
