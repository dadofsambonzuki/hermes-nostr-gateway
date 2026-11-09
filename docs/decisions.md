# Decisions

Calls this project has already made, written down so they are not re-opened by accident. This is
not a queue: work items are cards on the host's Hermes kanban board, and each card carries its own
scope, gates and PR. A decision earned by a blocked card lands here once it is meant to hold.

Each entry says what was decided and *why*, because the reason is the part that stops the same
argument coming back.

## White Noise relays are Marmot-only

`relay.eu.whitenoise.chat` and `relay.us.whitenoise.chat` carry the White Noise plane through
`wn-agent`. They are never part of the gateway's NIP-17 or feed read set.

Why: they are not general-purpose public relays for this identity. Subscribing to them on the DM
or feed planes pulls in a second copy of group traffic the Marmot plane already owns, which shows
the agent the same message twice and makes the per-relay health frame lie about where DMs come
from. A health check that finds no DMs among the live relays is therefore correct, not a
regression.

## No names in the repository

No operator, peer bot, identity or host names in docs, config examples, fixtures or comments.
Placeholders only: `relay.example`, `alice`, `host-a`, `<identity>`.

Why: the tree is published, and it is a mirror of what runs on a specific box. A name in a
fixture is a name that has to be scrubbed later, and a hostname in an example is a config line
someone will copy verbatim. Keys and npubs live in host configuration, never here — including in
test data that "will never be used".

## Two words, Hermes' words: `/queue` and `/steer`

`/queue <text>` (alias `/q`) and `/steer <text>` (alias `/s`) are read in the daemon *after* the
policy decides the sender's reach and authority, and are honoured only for `authority:
instructions` **and** `reach: immediate`. The command is stripped before the row is written, and
the row and the frame carry `mode: normal|queue|steer` — the same words whichever spelling was
typed, because a spelling is not a behaviour. An alias is an entry in the same word-to-mode
table, never a second parser and never a second mode, so it cannot drift from its word's gate or
its semantics.

The pair means "hold until the turn ends" (`/queue`, `/q`) versus "steer the turn now"
(`/steer`, `/s`). Those are Hermes' own words: its command table spells the same two behaviours
`/queue` (`/q`) and `/steer` (`/s`), with mid-run handlers behind them. An agent and a person
should not have to learn two vocabularies for the same idea, so a gateway prefix that means the
same thing reuses the name instead of inventing a synonym — and the same rule covers `/bg` and
`/btw` if a plane ever wants to carry them.

`/guide` was this gateway's own coinage, invented before anyone checked the host's vocabulary.
It is retired: still read as a deprecated alias of `/steer` for a release, so a message written
before the rename is not silently delivered as text, and a row or a frame from that release that
says `mode: guide` is read as `steer`. It is no longer the name in the code, the spec or this
record. There is no `/g`: Hermes has no such command, and `/s` is already the short form.

Why: a mid-task message could previously only mean "another instruction", so a sender had no way
to say "next, not now" without interrupting the turn or being buried in the backlog. Parsing
after the policy is the safety property — a stranger's `/steer` is a word in their text, not a
verb the agent obeys — and parsing before the frame is what lets the row keep the intent, so a
replay of an unacknowledged message stays held instead of becoming a fresh instruction. A digest
tier sender's prefix is left as typed: a batch is not a turn, so there is nothing there to hold
behind or steer.

The decision is deliberately narrow: no other commands, lowercase only, and a prefix with no
words after it is text. `/s` is one letter, so for a sender allowed to steer, a message that
opens `/s ` is a steer request whether or not that was the intent — the parser cannot tell a
sarcasm marker from a command, and the short spelling is Hermes' own. The cost is bounded by the
same gate as the long form: anyone who may not steer keeps the prefix as text. The gateway is not
becoming a command parser, and Hermes is not being reimplemented — the adapter maps `mode` onto
Hermes' own `/queue` and `/steer`.

## Concord is supported, not experimental

Concord (Armada/Vector) is a first-class plane. Do not gate it behind a feature flag, label it
experimental in docs, leave it out of the health frame, or make its tests opt-in.

Why: it runs in production for this gateway and carries real community traffic. A flag that
defaults off means the plane is only exercised by whoever remembers to set it, and "experimental"
in a doc is how a supported transport quietly stops being maintained.

## A route travels on the frame

Which provider, model and credential a turn asks for is decided by the gateway and carried on the
inbound `message` frame (`route`), not fetched by the adapter with a second socket op.

Why: the gateway already knows the conversation when it builds the frame, a route is a property of
the *inbound* message, and a replayed message has to be routed the same way as the first attempt.
A `route` op would be a second place where conversation-to-route is computed, and it would have to
agree with the frame under a race the frame cannot have — the route and the message arrive
together or not at all. The cost is that the adapter must read the credential itself; it gets the
variable's *name* and the `.env` path, never the value.

What the gateway does not do is apply the route: Hermes' provider resolution is out of scope here,
so the adapter hands the route to the host on the event (`metadata.nostr_route`) and reports back
what it ran on, rather than reimplementing model selection.

## A route names a credential, never holds one

`key:` in a routing rule is the name of an environment variable or `.env` entry. The value is read
when the turn is about to run, and never written to a frame, a log line, a stored row or an error.
A rule naming a long token instead of a name is refused at load *without echoing it*, because an
error string is one of the places a pasted secret ends up.

Why: a config is copied between hosts and pasted into issues, and a config that can hold a key is
a config that will. A name that cannot be read fails the turn — it never falls back to another
rule's credential, because one channel's usage billed to another's account is worse than a turn
that does not happen.

## A channel rule for a channel that no longer exists is unresolved

Concord channel routes resolve on the channel id (or its current name) among the channels the
gateway actually holds. A rule naming a channel that is not there resolves to *unresolved*, never
to the plane's route. `hng route --check` reports the same entry as a problem.

Why: a route is configuration, not protocol. Deleting a channel does not rewrite this file, so a
stale entry silently falling back to the plane's model is exactly the shape of bug nobody finds:
the room's answers are on a model chosen for background traffic, and the config says otherwise.

## A project route is the channel's project, not its checkout

`routing.projects` keys a route on the Hermes project slug a `concord.projects` entry declares,
so one rule covers every channel mapped to that project. Precedence is
channel > project > plane > default: a channel rule still wins, because a room is more specific
than the project it sits in.

Why the extra scope: a project is the unit an account is billed in, and without it the same key
has to be copied onto every channel rule the project has — where the copy nobody updated is the
one that bills the wrong account. A plane rule is too wide (a whole transport) and a channel rule
too narrow (one room), so the project is the only scope that can say "this project's work runs on
this key" once.

Two consequences worth keeping. Project rules are Concord-only: a DM or a Marmot group has no
channel, so no project, and a project handed in for one is ignored rather than applied to a
private conversation. And the lookup is by identity, not by path (`ProjectMap.identify`): a moved
or unmounted checkout still has its project, so its route stays in force — falling back to the
plane's key because a directory was missing is the silent substitution this whole block exists to
prevent. A project rule naming a slug no channel declares can never win, and `hng route --check`
reports it.

`profiles` deliberately has no project scope: a profile is a whole brain (its own sessions,
memory and skills), and that is a property of the room a conversation is held in, not of the
checkout its work lands in. `routing` is the block whose subject is the credential, so it is the
one that gets the project.

## A profile route is the frame's, and Hermes runs it

Routing a plane or a Concord channel to a Hermes profile is a routing-table entry, not a second
agent runtime. This gateway resolves the route (`profiles:` in its config) and puts the profile's
name on the message frame; the Hermes plugin sets it on the `SessionSource`; Hermes resolves that
profile's `HERMES_HOME`, its `state.db` and its `agent:<profile>:...` session namespace. Nothing
in this repository creates, configures, supervises or sandboxes a profile, and no profile gets a
session, memory or skills of its own from here.

Why: Hermes already has this machinery, including the isolation and the fail-closed behaviour
(`gateway/profile_routing.py`), and a second implementation would disagree with it exactly where
it matters. What Hermes cannot key on is a *plane* or a *Concord channel by name* — names this
gateway defines — so the route table has to live beside the names it uses. Copying the rules into
Hermes' `gateway.profile_routes` instead would be a second copy that drifts, which is why the
Marmot ownership guard reads Hermes' own flag rather than holding one.

Two consequences worth keeping: a route naming a profile Hermes does not have refuses the start
*and* refuses the frame at delivery (Hermes' own resolver would fall back to the default home with
only a log line), and a route change never migrates a session — the namespace is the profile's,
so the old session stays where it was. The control pane is not re-routed either: the route
changes whose memory a turn runs in, not where a confirmation is asked or whose word it trusts.

## A blocked frame carries its row's fate, never a reason to remember

A `blocked` frame says `final`: `true` when the row is retired (a policy refusal, which the replay
query excludes) and `false` while `Store.pending_immediate` still holds it (a route or a profile
the gateway could not resolve). The adapter settles a `final` frame and holds the other, replacing
its connection so the next `subscribe` offers the row again.

Why: the adapter used to decide this from a list of blocked reasons, and a list only covers the
reasons somebody wrote down. A refusal added later, or a shape another deployment emits, fell
outside it and was settled — which parks a replayable row until something unrelated disconnects,
because `subscribe` is the only op that offers one. The lifecycle is already on the row the gateway
just wrote, so the frame carries the answer rather than every consumer re-deriving it. The adapter
holds a frame that does not say: not knowing that a row is finished is not knowing it is over, and
one reconnect is cheaper than a message parked until a disconnect.

## A replayed reply is stopped at the adapter, not by disabling recovery

The Hermes adapter suppresses an identical send to the same chat, and keeps Hermes' recovery
path intact. A recovered redelivery (Hermes wraps it in a `♻️ Recovered reply` marker) is allowed
exactly once and never a second time; an ordinary identical send is allowed again after a window
(`NOSTR_DEDUPE_WINDOW_S`, default 60s, 0 disables the windowed rule).

Why: both ends of the send path are honestly at-least-once — the daemon can publish a reply
whose answer frame never came back, so Hermes is right to keep the delivery obligation and retry
it. Turning recovery off would trade a visible duplicate for a silently lost reply, and every
retry re-enters the same path, so the only place that can tell "already on the wire" from "never
sent" is the side that writes it. A send whose outcome is unknown (socket error, timeout, daemon
error) is never recorded: suppressing the retry of a reply that may never have left is the loss
this is meant to prevent. The refusal path keeps `success=True` for the same reason — it is
Hermes' "decided, do not retry" outcome, while `success=False` puts the text back into the
delivery ledger's retry sweeps, which is the loop being closed — and the refusal is remembered
for the window so a recovery replay of it is not attempted again.
## An issue is a dedupe key, not an identity

The ngit bridge keys a task on the issue's event id (`ngit:issue:<id>`) so a re-poll cannot
duplicate it. That key is idempotency, not a one-to-one relationship: a task cites the issues it
came from as provenance, one issue may produce several tasks, and a task with no issue is
ordinary. The strong link is task-to-PR — one task, one PR, because every change lands as a PR —
and that link is not this bridge's to invent.

Why: a one-to-one mirror makes the board a copy of the issue tracker, and then every internal
piece of work (a gate, a refactor, a follow-up) needs a fake issue to exist. The idempotency key
is the only thing the bridge actually needs from the issue, and it already has it.

## An unrecognised issue author is asked about, never queued

An issue from a trusted npub becomes a `ready` task. Any other author gets a `blocked` task and
one question in the channel; a trusted npub answers with `hng ngit answer`. The question is
durable state, not a live turn's memory.

Why: NIP-34 relays are open, so anyone can write to a repository's issue stream. If filing an
issue were enough to put work in the queue, the queue would be writable by strangers, and if the
bridge silently ignored them, a real report from a new contributor would vanish. Holding it and
asking a person is the only option that is neither.

## The gateway is a bridge as well as a pump

Planes are live and fail per message; the ngit bridge is a batch reconciler with its own cursor,
outbox and idempotency keys, and it fails per pass. It lives in this repository because the
alternative duplicates the identity, the relay set and the `concord.projects` mapping — the drift
this repository exists to avoid. The pass owns the store, the board and the cursor; a relay handler
may wake a pass but never create a task or move a cursor; the bridge never merges and never speaks
as the operator; its notices are queued, then delivered.

Why: the two engines share configuration, not control flow, and configuration is the part that
cannot be copied without drifting. A second deployment would hold its own key, its own relay list
and its own channel → checkout → project → board map, and the copy that has gone stale is found by
a channel being told about a repository whose board it does not own. One home is only safe because
the split is enforced rather than remembered: only the pass writes tasks, cursors and outbox rows —
a relay handler may wake it and nothing more — so a live event arriving mid-pass cannot half-move a
cursor or invent a task, and a wake that fires twice is just a second idempotent pass. The bridge
stays a narrator for the same reason: a merge is something it reports, never something it does, and
what it posts in the channel is a notice with the gateway's identity on it, not the operator
speaking.

## One repository, two runtimes

systemd runs both: the daemon as a service, and the bridge as a timer that starts a `Type=oneshot`
pass per channel (the daemon may optionally wake another pass on an event). The timer stays the
floor, so a missed event or a restart costs latency, not correctness.

Why: the daemon is a long-lived process that owns a socket and a subscription per relay, so its
lifecycle needs a supervisor — the operator restarts it, and `hng status` reports on it. A pass has
no lifecycle to own: it is a command that reads to a cursor, writes down what it decided and exits,
so a timer can start it with no supervision, no in-memory state and no restart to get wrong. The
scheduler is systemd rather than cron because the identity is a systemd credential: a user crontab
has no `$CREDENTIALS_DIRECTORY`, so a pass scheduled there could only read a key file while the
daemon read a credential — the same key in two forms. Waking a pass from the daemon's read path is
then an optimisation, not a dependency: the same pass runs sooner than the next tick, and because
every pass is idempotent and cursor-driven, losing one to a restart or a busy daemon costs the
channel minutes and never a message. Making the timer the fallback rather than the trigger would
put the bridge's correctness behind the daemon's liveness, which is the coupling this refuses.

## One key, two copies, and the check that makes drift loud

Two files hold the same account key on a headless host: wn-agent's own
`accounts/<label>/secret.json`, and the copy this gateway reads — `identity.secret_file`, or the
root-owned file a systemd `LoadCredential=` names. wn-agent's daemon has no other store:
`AgentConnector::open` calls `AccountHome::open`, which is `LocalFileSecretStore` and nothing else.
The Secret Service path (`zbus_secret_service_keyring_store`, selected per OS in
`crates/marmot-account/src/keyring.rs`) belongs to the interactive `wn` CLI, whose
`resolve_secret_store` defaults to `keychain` and accepts `file` only for local development. So
there is no headless keyring to adopt for the daemon: it never consults one, and a `keyring` file
backend would be a new store inside wn-agent, not a setting.

**The duplicate stays, deliberately**: it is this gateway's independence from wn-agent's data
directory, kept with a startup check rather than removed. The failure that motivated the question
is drift — a replace or a restore that lands in one copy and not the other, where each file is
individually valid and every message still succeeds — and that is a comparison nobody was making,
not a file that should not exist. `Gateway._setup_marmot` now compares wn-agent's account id with
the key this process signs with, and a mismatch disables the Marmot plane with both short ids and
the reference to this entry in `status`. It is plane-scoped on purpose: NIP-17, Concord and ngit
are this process's job, so a wrong Marmot key stops group traffic rather than taking the gateway
down, and `status`/`health` are where it shows.

`marmot.account_id_hex` is a fallback, not a source. wn-agent's `accounts/<label>/account.json`
is read whenever the configured home holds one, and a configured id that disagrees with it is
refused with both ids in the message: the key is a copy of an id, and a copy cannot be the thing
the guard checks — comparing it is exactly how a re-bootstrapped daemon would attach while
signing as a new npub. The one case where the configured id stands alone is a home with no
account file at all, an install in progress.

Why: the two copies exist because the daemon and the gateway are separately supervised and
separately restored — wn-agent's home can be rebuilt, renamed or re-bootstrapped without this
process losing the identity its own planes sign with. Retiring the copy is possible (see below)
but buys one fewer write per replace and costs that independence. What it does not buy is safety:
the silent case is closed by comparing the public halves, which cost nothing — the account id is
already in `accounts/<label>/account.json`, and the gateway already knows its own key.

### The rotation story

A Nostr identity is its key, so nothing rotates in place. What happens is a **replace** (a new npub,
usually under a fresh label) or a **restore** (the same key put back). Both are:

1. stop `wn-agent` and the gateway;
2. write the key to wn-agent's `accounts/<label>/secret.json` (bootstrap or `import-identity` does
   this) and to the gateway's identity source (the `LoadCredential=` file, or the `secret_file`
   path);
3. start `wn-agent`, then the gateway.

Miss the second write and the gateway refuses the Marmot plane at its next start, naming both
short ids; miss the first and wn-agent's own account check fails. What "at its next start" can
see is narrower than it sounds, and two windows stay open:

- **A replace while both processes keep running.** Each holds its key in memory, so nothing
  notices until something restarts; the check runs at gateway start and never at the send.
- **The file replaced, `wn-agent` still running, the gateway restarted.** The gateway reads the
  new account file and agrees with its own new key, while the daemon goes on signing with the key
  it loaded at boot. The files agree and the processes do not. Only a `wn-agent` restart closes
  this one, which is why the runbook restarts both and in that order.

The check could read the daemon's *live* account instead: `{"type": "account_list"}` over the
control socket returns the accounts the process actually serves, and that is the only source that
closes the second window. It is deliberately not what this does — the guard refuses before it
connects, so a process whose identity is in question never talks to the daemon at all
(`tests/test_marmot_identity.py` asserts no request reaches the socket). Connecting first is a
real option rather than an oversight: it would trade "never talk to a daemon we cannot trust" for
a window a two-service restart already covers, and it belongs in a change that says so.

### Options considered and rejected

- **A keyring with a file or encrypted backend.** Not a configuration this stack has. The daemon
  never opens a keyring, and `keyrings.alt` is a Python package for a Rust binary; `keyring` 24's
  file store would have to be added to wn-agent and selected there, which is upstream work and
  would still leave the gateway's copy to reconcile.
- **`LoadCredential=` for wn-agent.** The daemon reads and writes
  `<home>/accounts/<label>/secret.json`; it never looks at `$CREDENTIALS_DIRECTORY`, and a
  read-only credential would break its own bootstrap and removal paths. The gateway already uses
  `LoadCredential=`, which is why the second copy takes the form it does.
- **A user unit that unlocks a file-backed collection at boot.** There is no collection: the store
  is a plain file, so this adds a unit and removes no copy.
- **Retire the copy by pointing the gateway at wn-agent's file.** It works — a scratch config with
  `identity.secret_file` set to wn-agent's `secret.json` resolves the same account id and opens
  the live store — but it makes this process's identity depend on another service's data
  directory. Rejected for that coupling, not for feasibility; if the duplicate is ever retired,
  this is the one-line change, and the check should stay.

## The slash menu is the host's catalogue, and this plane's subset of it

Armada renders a bot's commands only from a replaceable **kind 10304** manifest, discovered per
conversation from the members whose **kind 0** says `bot: true`. This daemon publishes neither by
default; `advertise.publish` turns the advertisement on, per event kind. Invocation is unchanged:
`/name args` is an ordinary message and Hermes' gateway already dispatches it.

**The catalogue is written by Hermes, not copied here.** The gateway does not import Hermes (the
same rule as `profiles.py` copying the profile grammar), so the plugin — which runs inside Hermes —
writes `hermes_cli.commands_platforms._gateway_available_commands()` plus the plugin entries to
`commands.json` beside the socket, and the daemon validates that file. A list maintained in this
repository would drift, and a menu that offers `/whatever` the host no longer dispatches is worse
than no menu: the person selects it and gets "unknown command".

**Three commands are not advertised, and the list is a deny-list.** `/topic` (Telegram DM topics),
`/voice` (an audio surface this plane does not render) and `/save` (writes a file on the host that
a chat reply cannot carry back) need a platform affordance a Nostr client cannot give. Everything
else is kept, including `/queue` and `/steer`: those are exactly the words people type. It is a
deny-list rather than an allow-list so a command Hermes adds tomorrow is advertised without an
edit here — the failure mode of a stale allow-list is a menu that silently stops growing.

**The menu is ranked, then capped at 64.** Hermes reports more gateway-available commands than the
client accepts, and a plain truncation would drop `/help` and `/version` because of where they sit
in the registry. Names in `MENU_PRIORITY` come first (the everyday ones, in the order Telegram's
menu uses the same idea), then the host catalogue in its own order. Anything past the cap is still
dispatchable when typed; it is only absent from the menu.

**One identity, one catalogue.** Kind 10304 is replaceable per pubkey, so there is no per-channel
manifest to build. Per-channel *authorisation* — who may run a command in a shared room — is a
separate decision and is not part of this one; today Hermes gates by dm/group scope only.

**Kind 0 is opt-in, and can be left to the other writer.** The spec's disjoint-kind table gives
kind 0 to wn-agent, because two processes rewriting one replaceable profile fight and the last
write wins. Discovery needs `bot: true`, so this daemon can publish it — but only when configured,
and `publish: [commands]` exists so a deployment whose kind 0 belongs to another process advertises
the menu without touching the profile. Where this daemon is the identity's only profile writer,
`publish: true` publishes both.

**Publishing is judged by the read-back.** A receipt is not retention: the report carries the event
id and, per relay, whether the newest event for that author+kind is the one just published
(`retained`), absent, or superseded by a newer write. The stored content hash is what makes a
restart a no-op when nothing changed.

**A relay's `OK` answer is one of three things, never a binary.** `publisher.classify()` sorts the
NIP-01 answers, and the advertisement acts on the sort rather than on the fact of failure:

- **Refused** — `blocked`, `invalid`, `restricted`, `not allowed`, `pow:`, and `auth-required`
  surviving the in-process authentication. A judgement about the event or the key: it is recorded
  in `advertised.refused` and never tried again, because retrying it on every start would never
  change the answer. (The client's own `authentication failed` is *not* a refusal: it is how the
  relay asks for AUTH, and the next send answers it.)
- **Transient** — `rate-limit`, `error:`, `unavailable`, `busy`, a timeout, a dropped socket, and
  anything unrecognised. A condition, not a verdict: the relay is recorded in `advertised_retry`
  with its attempt count and the moment the next attempt is due (one minute, doubling, capped at
  an hour, six attempts in all), and retried once it is due. After the sixth attempt it is
  recorded as a refusal so a relay that answers "busy" forever stops costing a send on every
  start. Unrecognised answers are transient on purpose: mis-reading a condition as a refusal
  cannot be undone, while mis-reading a refusal as transient costs one bounded retry.
- **Held** — `duplicate:`. The relay already stores this exact event id: an idempotent success,
  counted as accepted and confirmed by the read-back like any other.

Recording a rate limit as a refusal was a real regression: the first `rate-limited: slow down`
froze that relay out of every later publish, and the daemon reported `partial` with no send on
the next start. The rule above is what the code does; `tests/test_advertise.py` drives a
rate-limited relay through the publisher, the backoff and the retry, and a refused one through
"recorded, not retried".

An unchanged copy is only re-sent to relays whose backoff has run out (or when `force` is asked
for), and the retry re-signs with the stored `created_at`, so the event id a relay may already
hold stays the one being sent. A relay dropped from the publish set loses its backoff row.

## A card workspace cannot push master

A kanban worker runs as the operator's user, so it holds push rights to `master`. That is
refused in code, not asked for in prose: a `pre-push` hook installed once per clone refuses a
push whose destination is `refs/heads/master` or `refs/heads/main` when it comes from a card
workspace — a shell the dispatcher marked (`HERMES_KANBAN_TASK` / `HERMES_KANBAN_WORKSPACE`) or
a checkout under `.worktrees/` — and `scripts/merge_pr.sh` refuses the same contexts with exit 4.
Hooks live in the clone's shared git directory, so one install covers every worktree the
dispatcher creates afterwards: nothing has to run inside the card for it to be guarded.

Why a hook and not a key: the hook is a code change with a test that can land the night it was
needed, while "the remote accepts `master` only from an operator key" is a server-side whitelist
nobody can install from a card. The hook is also bypassable — `--no-verify`, deleting the file,
or building a worktree from a shell the dispatcher never marked — and that is the point: it does
not promise to stop a determined worker, it turns an accidental merge into a deliberate one. It
is recorded here so the hook is never mistaken for a boundary; the identity-based gate that
would sit behind it does not exist on this remote (see *A GRASP cannot gate a branch by ref*).

## A GRASP cannot gate a branch by ref

This repository's remote cannot be configured to accept `refs/heads/master` only from an
operator key. GRASP push authorisation is the repository's confirmed maintainer set, applied to
every branch ref alike: a push is accepted when its signer resolves to a confirmed maintainer
and the pushed commit matches the latest state event (kind 30618) authored by a confirmed
maintainer. Any confirmed maintainer can author that state event, so any of them can move
`master`. The published GRASP-01 requirement says the same in one line — a server must accept
pushes "that match the latest repo state announcement on the relay, respecting the recursive
maintainer set" — and it has no per-ref clause.

Checked, not assumed: the origin's server runs stock `ngit-grasp` 3.1.1, the three other
servers the repository's announcement lists run the same software, and its authorization module
is unchanged from 3.0.9 through 3.1.1. `authorize_push` partitions the pushed refs into
`refs/nostr/<event-id>` (PR submission) and everything else, and applies the maintainer set to
the branch refs without looking at which branch they are. ngit-grasp's configuration surface has
no ref, branch or push-allowlist knob. Only `refs/nostr/<event-id>` has a different rule, and
that is the PR path: the contributor endpoint accepts that namespace and must reject every
other, so it cannot host a `pr/*` branch either.

**The fallbacks, and why neither was taken.** Making the operator's key the only confirmed
maintainer would refuse a worker's `master` push — and its `pr/*` push for the same reason,
because the maintainer set is not per-ref. Workers would then have to submit commits through
the contributor endpoint as `refs/nostr/<event-id>`; that is a workflow change (and the
endpoint is enabled on two of the four servers), not a gate. Forking our own instance to add a
per-ref check is the other option, and it gates nothing: the server the `nostr://` origin names
is not ours and would accept the same push under the stock rule. A branch-protection feature
would have to land upstream in `ngit-grasp` and be deployed by every server the announcement
lists before it gated anything.

**So the pre-push hook is the only gate.** `scripts/worker_push_guard.sh` refuses a card
workspace's push to `refs/heads/master`/`main`, and `scripts/merge_pr.sh` refuses the same
contexts with exit 4. Its limits are the honest ones already written down: `--no-verify`,
deleting the hook, or building a worktree from a shell the dispatcher never marked. Each turns
an accidental merge into a deliberate one. There is no remote that catches what the hook misses.

## `done` requires `applied`

A card that carries a PR link is finished by the merge, not by a worker. `hermes kanban
complete` is the worker's word, and a worker's word is not a merge: a card that read `done` on
an open PR made the board report work as finished that a human still had to land.

**The rule.** Completion of a PR-backed card requires the link to be `applied` with a merge
commit id, or a reviewer's approving verdict (`approved` — see "A verdict finishes its card"
below). The bridge enforces it on the board itself (`ngit_guard`): one row per PR card and
one `BEFORE UPDATE OF status` trigger whose refusal names the state it saw — `PR <short> is
open at <tip>`. Both completion paths write the same `tasks.status = 'done'`, so one trigger
covers the agent's tool and the CLI without either knowing the bridge exists. A merge releases
the guard, an approval releases it, and a close keeps it refusing.

**A worker finishes at `review`.** The reviewer records a verdict as a comment and parks the
card with `kanban_block` (the review lane's own approval tool, `kanban_complete`, is exactly
what the guard refuses while the PR is open). The verdict, not the park, is what the card
waits on: the bridge reads it from the PR root and finishes the card — `done` on an approval,
back to the implementer on a changes request. The merge then completes every remaining card
the PR owns, including a re-review a person filed, and releases any card whose own premise was
`after PR X merges`.

**Two decisions.** A card with no PR link — ops, docs, a fix card — is completed by its worker
exactly as before: there is no link row to check, and inventing a gate for it would make the
board lie in the other direction. A PR closed without a merge (kind 1632) does **not** complete
the card as `closed`: it blocks it with `block_kind=dependency`, names the closing event, and
leaves the decision to a person. Completion means the work landed; a close means it did not.

Why the trigger rather than a kernel hook: the gateway and Hermes are separate distributions,
and the kernel has no completion extension point a bridge can register. The board is the one
place both completion paths meet, and the guard is installed next to the rows this bridge
already writes. A kernel schema rebuild can drop the trigger; the next pass re-installs it from
the link, so the rule heals rather than silently lapsing.

## Three labels, one writer

A PR's review state is one of three words — `review-required`, `changes-requested`, `approved`
— and the label pass is the only thing that writes it. `in-review` and `stale` are retired: an
open PR with a review card already *is* in review, and a revision that leaves a verdict behind
means `review-required`, in one word rather than two labels a reader has to combine.

**The verdict is read from the PR root.** A reviewer publishes its verdict as a NIP-22
kind-1111 comment on the PR and reports the same verdict on the task, naming the event id. The
newest comment wins. The card is the queue; the PR is the record, and a label derived from the
card would say something a reader of the PR cannot check. The review brief says so.

**The pass corrects, it does not append.** It reads the label events the PR carries off the
same relays it publishes to, publishes the one that belongs there and retracts every other one
— including a leftover from an earlier writer that this bridge's link row never recorded. That
is what makes a leftover a one-pass fix; a writer that only retracted events it remembered
would leave the column with a history nobody could trust.

**Unknown is not stale.** A newest verdict the parser cannot read leaves the label untouched
and records the event and relay; so does a relay read that failed, and a PR the pass has
labelled but cannot read a verdict for. Falling back to an older approval is the one direction
that can publish permission nobody gave. Relays that disagree are read per relay: the newest
verdict wins, and the pass says which relay it read and which relays saw something else.

## The commit names its card; the merge only checks it

The authorship half of the merge route answers two questions: did anyone else sign this, and is
it finished? The second needs `git log` to answer *which card wrote this line* without the
board, because the tree outlives the board — a card can be archived, a board can be rebuilt, and
the merge commit stays.

**Written by the worker, never synthesised at merge time.** The alternatives were a trailer the
merge route adds when it lands the PR, or one the worker's commit already carries. Synthesising
at merge time loses on three counts: it can only guess for a PR whose commits never said (the
link names the PR's card, not which card each commit was done under); rewriting the commits to
insert it changes the history the verdict covered; and an ngit PR lands as `git am`, so the
commits themselves are what reach `master`. The worker's `commit-msg` hook writes the trailer
once, on the commit that is pushed, and the merge route refuses a commit that carries no card
trailer — none at all, or one that names something that is not a card (exit 6).
`git log` then answers on `master`, and because an ngit PR's description is built from its
commits' messages, so does `ngit pr view`.

**Operator work says so.** The hook writes `Card: operator` from a shell that is not a card
workspace. That is an explicit statement in the record, not a silent absence, and it is the only
way a PR with no card lands. It is also not a bypass: a PR the bridge has linked to a card is
still refused when that card is unfinished. `--no-verify` and deleting the hook remain
possible, as they do for the push guard; the merge route refusing an untrailered commit is what
makes them deliberate rather than invisible.

## A merge needs the card finished

`done` requires applied (`ngit_guard.py`): a card cannot be completed while its PR is open. The
merge route is the same rule read the other way: a PR cannot land while its card is still being
worked on. The merge is the only thing that finishes a PR card, so the two checks are the two
halves of one transition, and both read the card's `status` from the board rather than keeping
a copy of it.

**`review` and `done` both allow the merge.** `review` is a reviewer's turn; `done` is what the
bridge writes once it sees the merge, but the board also holds cards finished before their PR
landed. Refusing `done` would make the rule about a status the merge itself sets, which is
circular; refusing `review` would make the route wait for a decision nobody asked for.
Everything else — `running`, `ready`, `todo`, `scheduled`, `triage`, `archived` — is work that
has not reached a reviewer, and the refusal names the card and the status it has.

**`blocked` is allowed on the PR's own card, and nowhere else.** The review brief's last
instruction is to park a reviewed PR's card with `kanban_block`, because the guard refuses a
completion the verdict has not earned yet; the person such a card waits for is the person who
merges. Refusing it would mean no PR that went through the review lane could ever land — the
card cannot be `done` before the merge, so the merge would be refused for waiting on itself. A
*work* card named by a trailer is the other case: a block there is the work saying it is not
finished, and a merge does not overrule it.

**Which card is the bridge's answer.** The `ngit_links` row for the PR root is the PR's card,
and every `Card:` trailer names a card the work was done under; the merge route checks both, so
a block of operator work cannot carry a worker's PR past its review. When the bridge has not
linked a PR yet, the board comes from the checkout's `concord.projects` entry — the mapping the
rest of the bridge already trusts — so "not linked yet" reads as a card that must be finished
rather than as a reason to skip the check.

**Erratum.** The commit that wrote this section (`8a7f0de`, published as PR 08baeaaf, `0ad9f8d`
before `git am`) lists `blocked` among the statuses the merge refuses. On the PR's own card it
does not, for the reason above. The message stays as written — it is the history the verdict
covered — and this line is the correction.

## A verdict finishes its card

A review used to end by blocking its own card: the reviewer published the verdict on the PR,
parked the card with the verdict in the reason, and the card stayed `blocked` until a person
merged. That one choice broke the loop in both directions. **Approved work was invisible** — a
blocked card is not finished, so three approved PRs read as "not approved yet" on a board whose
labels said `approved`. And **a re-push could not resurrect the review** — `blocked` was outside
the reopen set, so a revision against a parked card was acknowledged in a comment nobody ran on.

**The verdict is the state.** The bridge already reads the newest verdict from the PR root for
the label; the same read now applies it to the PR's one card, in the same pass, under the same
rule (`review_labels`: a verdict covers the tip it was given for). `approved` **completes** the
card with the verdict as its result; `changes-requested` puts it back on the implementer's desk
as `ready`, with the fix brief and the findings — the transition the kernel's
`request_changes` writes, written by the bridge because the review card was never claimed from
`review` and the kernel's provenance check has nothing to route on. The merge stays a person's:
the card being `done` says the review is over, and the PR's label is what the merge route reads.

**Why the bridge and not the reviewer.** The reviewer cannot complete the card (the guard
refuses a completion on an open PR, and it must: a worker's word is not a review), and it cannot
call `kanban_request_changes` (not a review-lane claim). Making the reviewer do it would mean
either trusting a tool that refuses or re-shaping the card into a handoff it is not. The bridge
is the one reader of the verdict, and one writer per fact is the rule this board is built on.

**A card blocked on a question is never a verdict's.** A person's block matches no marker and is
left exactly where it is, by both the verdict pass and a revision. The one reason a blocked card
can still move is the bridge's own hand-off: `Verdict:` (old brief) or `Revision:` (a fix that
was pushed) as the first word. That is a tag, not a state — once the consequence is applied the
card is `done` or `ready`, so nothing re-reads the reason in steady state. The tag exists to heal
cards parked before the rule, and to let a fix round hand itself back.

**The fix round is a round.** A changes-requested verdict re-queues the same card rather than
opening a second one, and its body becomes the fix brief — otherwise the next claim would review
the same tip again and publish the same verdict for ever. The fix worker pushes and parks with
`Revision: <tip>`; the push usually lands first, so the park is the news, and the card goes back
to the review queue with the review brief for the tip the PR carries.

## A verdict behind the cursor is reconciled on demand

The verdict rule above only reaches verdicts the poll carries. It cannot reach one that is
already behind the cursor — and those are exactly the cards a fix to this path is written for,
which made every such fix unhealable in production. Measured after the verdict rule deployed:
three changes-requested cards and one approved card stayed `blocked` with a `Verdict:` reason and
got no event at all, because their verdicts had been consumed before the rule existed.

**The heal is a command, not a pass.** `hng ngit reconcile --channel <id>` walks the PR links
whose card is `blocked` with the `Verdict:` marker, reads each PR root's newest verdict again,
and applies the same rule the label pass uses: `approved` completes the card, `changes-requested`
returns it to its implementer, and a verdict a newer tip has superseded puts the card back in the
review queue for the tip the PR carries — finishing on a superseded verdict, or re-requesting a
fix already pushed, are both wrong. It is on demand because the condition is not a state the pass
could detect: "a verdict exists that nothing will read" is only true of cards parked before a
rule, and a schedule that re-read every parked card for ever would be a poll dressed as a repair.

**Idempotent by construction, and marker-gated.** Every outcome is `done` or `ready`, and neither
is a candidate on a later run, so the second run moves nothing. The marker test — not the reason's
wording — decides what is a candidate: a card blocked on a person's question matches no marker,
so it is not listed by `--dry-run` and not moved by the real run. `--dry-run` prints the blast
radius first, because a repair an operator cannot preview is a repair an operator cannot trust.

## A PR status is read by `e` as well as by `a`

NIP-34 addresses a status to a PR, and `ngit pr close` publishes exactly that: a kind-1631 or
1632 carrying `e` (the PR root), `p` and `alt` — and **no `a`**. The bridge read every PR event
under the repository's `#a` filter, so that close never came back: the link stayed `signalled`,
the guard row stayed `open`, and the PR's card could not finish until an operator signed a
status by hand. Two PRs wedged that way in one day.

**The `#a` filter on proposals stays.** A kind-1618 with no `a` cannot be attributed to a
repository, and dropping the filter would mean reading every ngit repository's PRs. **Statuses
are widened by id, not by repository.** The pass makes a second fetch of kinds 1631/1632 under
`#e` for the PR ids its own link table holds — relays index single-letter tags, so `#e` is a
real relay filter — and folds the answer into the same feed, deduped by event id. A status is
attributed by the root it names, so one for a PR the board does not link is discarded: the
wider read cannot invent work or move a card.

**Unbounded by the cursor, on purpose.** A status that landed behind the PR cursor is exactly
the one the `#a` read has stopped looking for. The status read asks about open links only —
applied and closed are final for the board — and it is re-asked every pass until the link
finishes, so a late status heals on its own.
