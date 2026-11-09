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

## A replay is shaped before delivery: superseded rows are context, not turns

A reconnect re-offers undelivered messages, but not as a page of history. In one plane and one
conversation only the newest deliverable message is a turn. The rows a later message, the age
horizon (`replay.max_age_secs`, default 600) or the batch cap (`replay.max_messages`, default 5)
leaves behind are marked `superseded` on the frame, with `superseded_by` naming the newer row
when there is one. The adapter files such a frame as context and acks it with `reason:
superseded`; the daemon records `delivered_at` and `settled_reason`, and the text stays in
`history`. `settled_reason` is deliberately a separate column from `blocked_reason`: `drops`
lists refusals, and a superseded message was accepted, not refused.

Why: a peer who writes six lines into a dead stream costs six agent turns today, because
Hermes' busy-mode `interrupt` only queues the later ones — it does not collapse them. The text
is already in the store; re-running it to understand it is the cost this removes. The decision
is written onto the row (`superseded_by`), not held only in the batch: if the newer row's ack
lands and the older row's does not, the older row must stay context rather than become a stale
turn out of order on the next reconnect. A shaped row is not un-shaped by a config change, for
the same reason — the turn it lost is context now.

The batch must not swallow a decision. A reaction (kind 7) and a typed `/approve`/`/deny`
answer are never superseded and do not count against the cap: a gate answered by a superseded
message is a lost decision. (A prompt itself is an outbound row — `authority: self` — which the
replay query never offers; the typed answer is the half that can be pending.) A message carrying
media is never superseded unless a newer deliverable message carries the same media, and such a
row is delivered even past the cap: losing the picture is worse than one extra turn. Shaping is
per plane as well as per conversation — the same peer on NIP-17 and NIP-04 is two transports —
and digest frames are batches with their own replay, never shaped. A superseded frame is
settled, not held: it is acked, so it never enters the adapter's hold/reconnect path, and
`final` keeps its existing meaning on `blocked` frames. The bounds are read once at worker
construction, so a change needs a restart; `0` disables a bound, and both at `0` is the old
replay.

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

## Every event type has an author, and a repository tag is not one

A PR's author is gated as an issue's: a trusted npub or a repository maintainer gets a `ready`
review card, and anyone else's PR is blocked with one question. A kind-1631/1632 status is accepted only from
a maintainer (the announcement's own list, unioned with `ngit.maintainers`), a close also from the
PR's author, and a kind-1619 revision only from the PR's author. A kind-1111 verdict is accepted
only from `ngit.reviewers` (or the review key it is derived from), never from anyone who tags the
PR. The allowlists are applied in the relay filter and checked again on the parsed event; an empty
list trusts nobody, and a commit id that is not hex or a `created_at` in the future is dropped.

Why: findings 1–4 of the security review all had one root cause — the reads filtered on the
repository address (`#a`) and nothing else, so a stranger could get a review card dispatched
(which checks code out and runs `pytest`), release a held issue into the queue with an `after PR`
title, finish or rewrite a review card with a forged `Verdict:` comment, or complete every card
with a forged merge. A review task is a code-execution path as well as a queue entry, so the
question is asked before the card can be claimed.

## A joined Concord community is pinned to the invite that joined it

A re-invite that changes the community root, control key, epoch or relays — or replaces or adds a
channel grant — is held for a person unless its inviter is trusted or is the owner. Streams are
built only from rows that were actually joined, not from every invite ever recorded.

Why: `community_id` self-certifies the owner and salt and nothing else, and an untrusted member
could otherwise replace the root key and relays of a community the agent had joined, sending the
agent's replies, approval prompts and ngit notices to the attacker, and hiding them from the real
members. The same pinning keeps a held invite from becoming a subscription or a send target.

## Every plane refuses a message outside the age window

An inbound rumor whose own `created_at` is older than three days or more than five minutes in the
future is stored as blocked (`stale_or_future`) and never delivered. The check runs in the store
before the row is written — where the deferred writer cannot race it — and again at the gateway's
one emission point, so it applies to NIP-17, NIP-04, kind-1, Concord and Marmot alike, and the
blocked row cannot be replayed after a restart.

Why: a relay in the read set can serve an old, never-seen message from a trusted sender with full
authority. The spec already promised this window (§10.5); the planes did not implement it, and the
Marmot path bypassed the reach filter entirely.

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

## The adapter copy is compared, not removed

`integrations/hermes-plugin/adapter.py` is canonical and Hermes loads a **copy** of it from its
plugins directory (`<HERMES_HOME>/plugins/nostr/adapter.py` by default). The copy stays: packaging
the plugin for `hermes plugins install` is a different decision, and until it is made the copy is
how a checkout's adapter becomes a running gateway's adapter. What was missing is not the file but
the comparison — a merged adapter change read as deployed while the running process still held the
module it imported at its last restart, which is the "a merge is not a deployment" failure one
level up.

**Three surfaces, one comparison.** `hng doctor` gained an `adapter` check that fails the run when
the two `adapter.py` files differ, naming both SHA-256 hashes; `hng adapter status` is the same
comparison on its own, printing both hashes, both mtimes, and whether `hermes-gateway`'s
`ActiveEnterTimestamp` predates the live copy; `hng adapter install` backs the live copy up beside
itself (`adapter.py.bak-<UTC stamp>`), writes this build's file and hashes what landed. Only the
`.py` is compared — `__pycache__` holds bytecode from whichever interpreter last imported it, and
no restart reconciles that.

**Exit codes follow the vocabulary already there.** `doctor` is binary (0, or 1 when any check
fails), so a mismatch is a failed check named `adapter` and gets no code of its own. `adapter
status` answers 0 when the copies match, 1 when they differ or one cannot be read, and 2 — the
CLI's existing "refused before touching anything" — when the config it was pointed at will not
parse.

**A stale import is a warning, not a failure.** The files matching is the invariant; the process
age is evidence. The two are different repairs: a mismatch is `hng adapter install` and a restart,
while an older process start only means the next restart will load a copy that is already correct.
Failing the preflight for it would make `doctor` red on every host that had just installed and not
yet restarted, which is the state `install` deliberately leaves behind. So the passing check says
it, and `install` prints the restart as the operator's step.

**`install` never restarts a unit.** A copy is not a deployment. Restarting `hermes-gateway` cuts
in-flight turns, and a command whose advertised job is "make the file match" must not also decide
when that happens. `--live`, `NOSTR_PLUGIN_PATH` and `adapter.path` exist so a copy installed
somewhere else can be compared and refreshed, and `$HERMES_HOME` wins over `$HOME` because the
caller's home is not necessarily the gateway's.

Why compare at all instead of restoring the copy from the checkout at every start: the gateway is
not the thing that loads the plugin, so a startup hook there would still be a copy — one that
rewrites a file Hermes may be mid-import on, with no backup and nothing for `doctor` to report.
The comparison has no such window, and it answers the question an operator actually asks — which
file is the running process holding?

## A reaction answers the one gate the host named, not the oldest one waiting

An approval prompt delivered to Nostr is a message someone can react to: 👍 is the `/approve` they
would have typed. The adapter takes the prompt through Hermes' platform hook
(`send_exec_approval` / `_send_exec_approval_prompt`) rather than letting the host fall back to
plain text, because that is the only surface that carries the `request_id` naming *this* gate. The
reaction settles through `resolve_gateway_approval_card(session_key, choice, request_id, …)`, so a
stale 👍 on an old prompt cannot approve whatever the session asked next, and two gates waiting at
once are not guessed between.

A prompt that names no request id is not answerable by reaction at all: the typed `/approve` and
`/deny` still work, and leaving a reaction inert is the only safe answer when the host cannot say
which gate it would settle. The adapter also stops tracking plain-text `is_approval_prompt`
messages (an `/approve` confirmation carries that flag without being a gate), and it no longer
reads `tools.approval.list_gateway_approvals` — that queue is not a public surface, and the request
id the hook carries makes the read unnecessary.

**A reaction is a click, and the host's approver list applies to it.** A room's admitted members
all see the prompt, so `approvals.approvers` narrowed to one person must be honoured on this
surface too — otherwise a 👍 from any admitted member is an approval the typed `/approve` would
have refused. The adapter asks `gateway.approval_actor.approval_refusal` before it consumes the
prompt, replies with the requester's name when the host refuses, and leaves the gate pending; it
settles through `resolve_gateway_approval_card(..., actor_id=…, extra=…)`, whose own re-check
happens in the same critical section as the settle. Those two names — `approval_refusal` and
`refusal_text` — are public on hermes-agent main (verified at `9dcf7714df`, 2026-11-09:
`gateway/approval_actor.py` is a tracked blob, `hermes_cli/data/plugin_api.json` lists both names
at lines 94-95, and the adding-platform-adapters guide documents them under "Public helpers for
platform plugins"). The core installed on this host predates them, so
`hermes plugins validate` against *this* machine still names the two imports; that is a version
warning, not a missing seam. A host without the helper has no narrowed list to honour, so the
import failing there is not a silent bypass: the feature did not exist there.


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

**That pair of imports is the one rule-9 coupling upstream has no seam for, and it is upstream's
to close.** Hermes documents no public read surface for a platform's command catalogue, and its
own in-tree adapters import the same two functions; the alternative — a snapshot regenerated at
build time — is exactly the drift the previous paragraph rejects. Upstream issue
`NousResearch/hermes-agent#149673` asks for the seam (`gateway_command_catalog()`: the registry
filtered for the gateway, plugin commands included, as plain data), and the plugin moves onto it
when it exists. Until then the failure is recorded where `hng status` reads it, so a rename is
visible rather than silent. The approval helpers are the other two names a validator can print
(the reaction decision above explains them); unlike the catalogue, those have a public home on
main and only read as coupling against a core that predates it.

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
below). `applied` is the status record *or* the content on the branch (see "Applied has two
witnesses" below) — the distinction is what this rule needed most. The bridge enforces it on
the board itself (`ngit_guard`): one row per PR card and one `BEFORE UPDATE OF status` trigger
whose refusal names the state it saw and what was compared — `PR <short> is open at <tip> --
`<evidence>`. Both completion paths write the same `tasks.status = 'done'`, so one trigger
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

## The merge publishes its own record

`ngit pr apply` replays a proposal's patches as **new** commits. The proposal's own commits
never become ancestors of the target, so ngit's inference ("the proposal is in master") never
fires, and the PR reads `open` with its content already in. Nothing infers `applied` here; a
maintainer kind-1631 has to say it, naming the commit the push left. So `scripts/merge_pr.sh`
publishes one after the push (`hng ngit applied`), and the bridge never does: only the merge
knows the commit.

**Both readers decide the tags.** `merge_from_event` takes the root `e` and the commit from
`merge-commit-id` or `merge-commit`; the repository feed (`RelayPrs.events`) is filtered by the
repository's `a` coordinate. A status with one and not the other parses in a test and is
invisible in production — which is how a hand-published close (`e`/`p`/`alt`) vanished, and why
`hng ngit closed` exists rather than `ngit pr close` (which writes no `a` tag).

**A merge that publishes no record is not a success.** The content is in and the card still
refuses completion, so the helper exits 8 and prints the retry rather than reporting the merge
as done. The alternative — treating the push as the end of the job — is the failure this record
exists to remove.
## Applied has two witnesses

`done` requires `applied`, and for a while the only answer to "is this PR applied" was the
kind-1631 status record. That is the right answer on a forge that fast-forwards — `git merge`,
`gh pr merge` — because the proposal's tip becomes an ancestor of the target. It is the wrong
answer on this repository's only route: `ngit pr apply` replays the patch series as **new**
commits, so the tip a status would name is never an ancestor, and the status itself is only
published by a merge route that may not have run. Every merged PR then read `open` for ever
and its card refused for ever with `PR <short> is open at <tip>` — a sentence literally true
of the object graph and false about the work. Three merged PRs (`30500fef`, `6699c28f`,
`63f6a63b`) sat behind that refusal while their changes were on `master`.

**Decided: the record first, the checkout second** (`applied.py`).

* A kind-1631 that names the PR is the record. Nothing else is asked.
* Otherwise, every patch the proposal adds is compared with the base branch by `git patch-id`
  — a hash of the diff with line numbers stripped, so a replayed, rebased or cherry-picked
  commit keeps its id while its commit id changes. The proposal is applied when every patch it
  adds is on the branch, or when the proposal taken as one patch is (a squash).
* Ancestry is still checked, first and cheaply: exact when a forge fast-forwards, and now one
  signal among three rather than the signal.

**The refusal names the evidence it used.** The guard row and its trigger carry the sentence
the pass compared — `no applied status names it, and no commit on master carries its patch
(1 compared against 12 since 9b52622)` — so "open at <tip>" is never printed about a PR whose
content is in without also saying what was checked. A card completed by content says
`applied as <commit> (its patch is already on master …)` and that no status event claims it,
so a reader checks the commit instead of taking the board's word. The answer is stamped on the
link with the tip and base it was read at; a pass with no news costs one `rev-parse`.

**A close with nothing on the branch still needs a person.** The kind-1632 rule survives
unchanged: no merge, no card completion, and the closing event is named. What a close cannot do
is undo a landing: a proposal whose content is on the branch is applied, and a 1632 that
arrived afterwards is recorded rather than obeyed. Completion means the work is in; a close
means someone decided it should stop — when both are true, the work is still in.

**Rejected: trusting `ngit pr view`'s status.** It reports the same record the feed does, so
it inherits the same silence.

**Rejected: a merge route that publishes the status itself.** `scripts/merge_pr.sh` should
publish the kind-1631 for a merge it applies (a separate change), but the bridge cannot depend
on it: PRs land through the route as it is deployed today, and a card that refuses until an
operator publishes an event is a card that was never actually gated by a merge.

## Three labels, one writer

A PR's review state is one of three words — `review-required`, `changes-requested`, `approved`
— and the label pass is the only thing that writes it. `in-review` and `stale` are retired: an
open PR with a review card already *is* in review, and a revision that leaves a verdict behind
means `review-required`, in one word rather than two labels a reader has to combine.

**The verdict is read from the PR root, and the bridge puts it there.** A verdict written only
into the card is a review nobody outside the board can see, so the pass publishes the newest
verdict comment from the review card on the PR itself: a NIP-22 kind-1111 comment signed by the
review identity, the card's words exactly. A reader of `ngit pr view <pr> --comments` then sees
the review that decided the PR, the label pass derives the label from that event, and the merge
route reads the same one. Idempotent on the card and the comment — the event's timestamp is the
comment's own, so a retry signs the same event id rather than a second comment. The newest
comment wins. The card is the queue; the PR is the record.

**Authorship is part of the merge.** `scripts/merge_pr.sh` refuses a PR whose newest verdict is
its own author's: a self-authored change lands only with a second party's signature on it.
That is why the review identity has to be a different key from the gateway's — the pass refuses
to publish a verdict with the gateway's own key, and the merge route refuses one from the PR's
author. It is not a security boundary (a person can publish anything by hand); it is the line
between "reviewed" and "the author says it is fine".

**The pass corrects, it does not append.** It reads the label events the PR carries off the
same relays it publishes to, publishes the one that belongs there and retracts every other one
— including a leftover from an earlier writer that this bridge's link row never recorded, and
including a second event carrying the *same* value. Two publishes racing leave a pair that
says one thing twice, and a test by value cannot see the second one; the pass picks the
survivor first (the row's own event when the relays still serve it, otherwise the newest live
one) and retracts the rest. That is what makes a leftover a one-pass fix; a writer that only
retracted events it remembered would leave the column with a history nobody could trust.

**The read is by `e` alone.** NIP-01 filters conjoin, so asking for `#e` *and* the repository
`#a` in one filter only ever returns the shape this bridge writes — while `ngit pr label`
publishes a label with `e` and `p` and no `a`. That event is visible to every reader and
invisible to a filtered pass, so it counts in the column and can never be retracted. The
repository scopes the read afterwards instead, on the events: this repository's `a` when one is
carried, and an event with no `a` at all is this PR's. The same tolerance the issue-label gate
applies, for the same reason. The kind-5 limit is unchanged: a retraction only removes events
from our own key, so another key's label can be ignored by a reader but not deleted by one.

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
the card being `done` says the review is over, and the published verdict carries the tip it
covers (the `Tip:` line) so the merge route refuses one that does not name the tip it would
land. The link's `verdict_tip` is the same fact cached for the pass's own bookkeeping; the
merge route reads the record. The label is a public copy of the state, not the gate.

*Partly superseded — see "A changes-requested verdict queues a fix card" below: `approved` still
completes the card, and `changes-requested` queues a fix card instead of re-queuing this one.*

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

*Superseded — see "A changes-requested verdict queues a fix card" below. The review card keeps
the review brief now, and the fix is its own card.*

## A changes-requested verdict queues a fix card

The rule above made one card carry three jobs: review this tip, fix what the review found, then
review the fixed tip. The card's identity changed with the verdict, the next claim had to be told
which of the three it was doing, and the per-verdict idempotency lived in a `changes_requested`
event on the card — the last one, so two rounds of findings on one PR could not both be
remembered. The fix also competed with the review for the card's status: `done` on an open PR is
what the guard exists to refuse, so a card mid-fix could not be finished and a card mid-review
could not be handed to a fixer without rewriting what it was.

**A verdict is a statement, and the work it asks for is its own unit.** So a changes-requested
verdict now **opens a fix card**, keyed on the verdict event:
`ngit:pr:<pr root>:changes:<verdict event id>` (`Board.fix_key`). The idempotency is the
statement: a re-read of one verdict finds its card by the key, and a second review of the same PR
— the fix pushed, reviewed again, changes asked for again — is a second statement and earns a
second card. The card carries the verdict's own words, not a summary, plus the PR id, the tip
the verdict covered, and the push recipe; it is dispatched to `ngit.assignee`, or
`ngit.review_fix_assignee` when an operator names one.

**The review card stays the PR's.** It remains parked on the verdict it answered (`Verdict:` in
the reason), and a revision reopens it for the tip the fix leaves — so the PR still has one
review card for its life, and the next claim still reviews. It is not re-queued to the
implementer any more: two cards for one fix is the duplication this is written to avoid. The
review card is therefore still `blocked` while the fix is outstanding, which is the truthful
state — the review is over, the PR is not approved, and nothing may complete it (`done` requires
`applied`).

**The one-card rule is scoped, not repealed.** One PR root has one *review* card. A fix card is a
strict extension of the same key prefix, so `Board.review_task` and `Board.pr_cards` exclude it:
answering a verdict against the fix card, or completing it with the PR, would put a second writer
on one card. A fix card carries no `done requires applied` guard — its work is a pushed revision,
not a landing — and a merge finishes one that is still queued, because a fix for a PR that landed
is work nobody should claim.

**Ownership decides whose queue it is.** `ngit.owns` (default: the gateway's own identity) names
the npubs this instance speaks for. A fix belongs to the queue that can push to the PR's branch,
so a PR authored outside that list opens nothing here: the label is still published — it is the
public half, and every instance derives it — and the pass says once, per verdict, that it skipped
the card. Two instances watching one repository therefore do not both hand the same fix to their
own implementer, and an external author's PR stays theirs. An approval is not gated: it finishes
the review and queues no work.

**Nothing here merges.** The strongest thing a verdict's consequence can do is put work in a
queue. The merge route still decides whether the PR lands, and it reads the verdict and the tip
it covers from the PR, not from either card.

## A verdict behind the cursor is reconciled on demand

The verdict rule above only reaches verdicts the poll carries. It cannot reach one that is
already behind the cursor — and those are exactly the cards a fix to this path is written for,
which made every such fix unhealable in production. Measured after the verdict rule deployed:
three changes-requested cards and one approved card stayed `blocked` with a `Verdict:` reason and
got no event at all, because their verdicts had been consumed before the rule existed.

**The heal is a command, not a pass.** `hng ngit reconcile --channel <id>` walks the PR links
whose card is `blocked` with the `Verdict:` marker, reads each PR root's newest verdict again,
and applies the same rule the label pass uses: `approved` completes the card, `changes-requested`
queues the fix card its verdict asks for, and a verdict a newer tip has superseded puts the card
back in the review queue for the tip the PR carries — finishing on a superseded verdict, or
re-requesting a fix already pushed, are both wrong. It is on demand because the condition is not
a state the pass could detect: "a verdict exists that nothing will read" is only true of cards
parked before a rule, and a schedule that re-read every parked card for ever would be a poll
dressed as a repair.

**Idempotent by construction, and marker-gated.** An approval leaves `done` and a superseded
verdict leaves `ready`; neither is a candidate on a later run, so the second run moves nothing. A
changes-requested card stays `blocked` on its verdict, so the guard against a second run is the
fix card's key instead: the first run queues it and a later one finds it and says so. The marker
test — not the reason's wording — decides what is a candidate: a card blocked on a person's
question matches no marker, so it is not listed by `--dry-run` and not moved by the real run.
`--dry-run` prints the blast radius first, because a repair an operator cannot preview is a
repair an operator cannot trust.

## A close names the successor

`ngit pr close` publishes a kind-1632 whose `content` is the reason, and the reason is optional.
That default is wrong for the case closes exist for here: a branch amended and re-pushed
publishes a second proposal, the older one is closed, and a reader of the closed record needs
to know which proposal survived. An empty close reads as a withdrawal and gives them nothing
to follow — and the bridge's own comment quotes the closing event, so the pointer has to be in
the event itself.

So the close route (`scripts/close_pr.sh`) requires a successor, reads its tip from
`ngit pr view --json`, and publishes `Superseded by <id> (tip <sha>): <why>`. The merge route
uses the same lookup when a proposal applies no commits: it names the other open proposal on
the branch and prints the exact close command. A branch with no sibling still has to name the
successor in the reason — the rule is about the record, not about what the lookup found.

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
finishes, so a late status heals on its own. The watcher is unchanged: it still wakes a pass
from the repository's `#a` subscription, so an `e`-only status is picked up by the next
scheduled pass rather than in seconds.

## An issue is queued by a trusted label, not by its author

The bridge used to decide from the author: a trusted npub's issue became a card on sight, anyone
else's was held and asked about in the channel. That made "who wrote this" stand in for "do we
want this work", and it spent a card (and a question) on every issue anyone filed — including the
ones a human had not read yet. The authorised-author rule also conflated two different people:
the contributor asking for something, and the maintainer deciding it is wanted.

So the authorisation moved to a label: an issue becomes a card when a trusted npub has published
`ready` (NIP-32 kind 1985, targeting the issue; any namespace, because the namespace is a display
convention and the question is who said it) — `ngit.ready_label`, `""` to restore the author
rule. The read asks for the issue's `e` **alone**: `#e` and `#a` in one filter conjoin, and
`ngit issue label` writes the label with no repository `a`, so a read that asked for both would
never be served the shape a maintainer actually publishes. The repository scopes the events
afterwards instead — this repository's `a` when one is carried, no `a` at all accepted — which
is the same tolerance the PR review read applies, for the same reason. Three consequences are
deliberate:

- **The decision is public and durable.** It is written on the issue, where the contributor and
  every other reader can see it, instead of in a channel the contributor may not be in. Taking
  the label off does not retract a card that already exists — a card is work in flight, and the
  board is not a mirror.
- **A waiting issue is a link, not a card.** `ngit_links.state = waiting` records that it was
  seen and is not queued. That row is what makes the one channel line repeatable-free, and what
  lets a later pass notice the label without re-reading the whole issue list.
- **The author stops mattering at the card.** Once a trusted person has authorised the work, the
  issue is queued `ready` whoever filed it. The old `held`/`asked`/`answer` path is kept for
  links written before the gate and for `ready_label: ""`; it is not the default.

## A mention is the one thing in a repo addressed to the bot

Every other event in a repository is addressed to the repository: an issue is a request for the
project, a PR is a proposed change, a comment is part of a thread. An @mention with the
gateway's `p` tag is addressed to the agent, and it is the one case where "the bot should answer
here" is what the person asked for. It becomes one reply task (`mentions: true`), carrying the
comment, the thread's nevent and the exact `ngit issue|pr comment --reply-to … --nsec-file …`
command, signed with `reply_secret_file` — the gateway identity, because a reply in the bot's
voice has to come from the bot's key.

Trust is the control pane's, as everywhere else: a stranger's mention is recorded with no task.
The worker writes the reply; the bridge only routes it and supplies the spelling of the publish
step, which is where a model left to guess would put the comment in the wrong thread or sign it
as the wrong npub.

## A relay that refuses our filters is not a read relay

The read set stood at eight relays and four of them contributed nothing: two answered nothing at
all, one answered `auth-required` even after NIP-42 and kept refusing the key, and one group relay
had held nothing for a fortnight. None of that is a fault in the gateway, and all of it was
reported as one — `health` read `degraded` on every call, which is the same as making it
unreadable.

**A read set is defined by what it can serve, so the config says which set is which.**
`relays.read` is the relays whose filters are the gateway's business. `relays.keep` is a relay
retained for web-of-trust checks or as history, with a `why`; `relays.whitenoise` is the same
thing with the Marmot plane as its purpose. A kept relay gets no worker, no verdict and no say in
the gateway's health — and a relay named in both lists is a config error, because the choice has
one place to live. A peer's kind-10050 list cannot re-add a kept relay: this host's decision about
what it reads outranks a peer's inbox advertisement.

**`refused` is a state, not a fault.** A relay that will not serve the filter is reported once, as
`refused (<reason>)`, and counted apart from `down` — "8 relays, 4 refused" is a sentence about the
read set while "8 relays, 4 down" is a sentence about the transport, and they want different fixes.
A refusal does not degrade `health` either; only a read set with nothing delivering on it does.
`hng doctor` carries the counts in one line, so the read set can be wrong out loud without turning
every other verdict red.

Why: a signal that is always amber is a signal nobody reads, and the cost of a relay whose policy
is not ours is one config line, not a degraded gateway.

## A room turn carries the room's preceding lines, bounded by the client

A Concord message reached the agent as the trigger alone. The store already held every
message per `concord:<community>:<channel>`, but nothing put the lines before the trigger in
front of the model, so a mention in a busy room was answered blind and the agent had to think
to call `history` — which was the whole store, newest first, with no way to ask for one
conversation or stop at one message.

**The daemon answers a scoped page; the adapter decides how much.** `history` gained an
optional `conversation` with `before_at` + `before_id` (the trigger's `(created_at, id)` anchor)
and honours only `limit`. The adapter chooses the window — 20 messages, 8000 characters, 2
hours back from the trigger — because that is a cost preference per deployment, like the
Marmot plugin's own timeline page, and a per-client cap can change without a daemon release.
The cost is real: `channel_context` is charged to the model like any other text, and a
20-message room turn can dwarf the trigger, so the character cap is part of the feature, not a
safety valve on it.

**The anchor is exact.** The predicate is `(created_at, id) < (before_at, before_id)`, so the
trigger is never read back as its own context and nothing after it leaks in; the id breaks a
same-second tie rather than letting the trigger's second become a window. `limit` takes the
rows nearest the anchor, because the line just before the question is the one it is about.

**Only a turn that already passed the gate gets context.** The adapter attaches the page in
`_deliver`, after the reaction, digest and blocked branches: a message frame there is already
a mention, a solo room, or a command the policy admits. Context never widens delivery and a
missed chatter line never starts a turn. DMs and Marmot groups are excluded — a DM's history
is scrollback, not a room — and the trigger remains the user message.

**On by default, with the caps, and failure is silence.** The failure being fixed is an agent
answering a room it has not read; the caps bound what fixing it costs, so the shipped setting
is on. A failed lookup logs at debug and the turn runs without context — never a refusal,
never a second turn. An older daemon that ignores the extra fields answers with unscoped rows,
so a scoped reply echoes `scoped`, `conversation`, `before_at` and `before_id`; an adapter
that does not see its own request echoed back warns once and runs without context rather than
treating another room's lines as this one's.

## A channel id is announced, not guessed

`concord.projects` is keyed by the channel id, and the failure mode of a name is silent: a
name-keyed entry resolves an inbound message, passes every check, and then loses every notice
at send time, because a notice is addressed by `concord:<community>:<channel-id>` and a name
never becomes one. Two changes make that self-solving instead of a lesson learned per incident.

**A join announces the ids.** On a successful join — a trusted invite, or the connect-time
refresh a restart runs — the gateway logs each channel id at INFO and posts one notice per
community into the concord `control:` pane: the community's name, then
`channel_id: <64-hex>  <name>` per readable channel. The destination is the control pane and
never a guessed DM home channel: a notice aimed at home is how one bot aimed an echo loop at a
peer. A parked or untrusted invite stays silent, as it was — nothing joined it, so its rooms
are not live.

**The record is a store table, not a file.** `concord_announced` holds one row per community
and channel id, written before the send (so two racing refreshes announce once) and given back
if the send fails (so a transient relay is a delay, not a loss). A restart re-reads the same
grants, finds the row, and says nothing. A JSON file beside the state dir would be a second
writer — the daemon and `hng` both read these grants — and a thing a human could edit into a
false claim.

**The CLI can always print them.** `hng concord channels [--community <id>] [--json]` reads the
Control plane exactly as a refresh does — one read per joined community, no new kind of round
trip — because the grants in the store are not the channel list: one owner action mints one
invite per room, so a joined row can name only a fraction of the community's channels. When a
relay does not answer it falls back to the stored grants and says so on stderr, because a
listing that cannot see the Control plane is still better than none as long as it says which it
is. `--json` is `[{id, name, community}]` in a stable order; the bridge and the dashboard can
consume it.

**`projects --check` fails a name.** It is an error, not a warning, because the failure is
silent loss. The entry is still resolved for inbound messages — removing that would break a
working mapping for the sake of a notice — but the check names the key and the pitfall. A key is
usable when it is the channel id or the whole `concord:<community-id>:<channel-id>`: the two
forms a scheduled pass can act on.

A channel the bot creates itself arrives through the same refresh, so it is announced by the
same path; the claim makes a second restart harmless.

## Python 3.12 is the floor

`requires-python` is `>=3.12`, the README says 3.12+, CI runs 3.12 and 3.13, and
`tests/test_python_floor.py` fails if any of them disagree. The Dockerfile's Python is checked
against the same floor.

Why: the floor had been 3.11 since the language decision, CI pinned 3.12, and nobody ran the
suite on the declared floor — so the claim survived although it was false. On 3.11.16
`tests/test_adapter_pending_replay.py` hangs: the reader loop is cancelled while an ack reply is
in flight, `asyncio.wait_for` returns the reply and **drops the `CancelledError`**, and the loop
then waits on a socket nothing will write to again. 3.11 wraps the awaitable in a task and, on
the branch where that task has already completed, returns its result instead of re-raising;
3.12 rewrote `wait_for` on `asyncio.timeout` and keeps the cancellation, which is why the same
test passes there. A minimal reproduction — a task cancelled in the same loop pass that
completes the awaited task — returns the value on 3.11 and raises `CancelledError` on 3.13.

Why it is not worked around in this code: `asyncio.wait_for` is used throughout the daemon and
the adapter, and any of those call sites can lose a shutdown the same way when the cancel lands
as the awaited read completes. Replacing every one of them with a hand-rolled timeout to dodge a
stdlib bug that CPython has already fixed is a worse trade than declining to support the
interpreter — and the daemon kept serving on 3.11 throughout, so nothing here needs 3.11 either.

Consequence: a contributor on 3.11 gets a resolver refusal that names the floor, not a hung test
run; and the tested range is the declared range, so the next floor change has to move CI with it.

## The merge gate reads the record, and the verdict names the tip it covers

The merge route checked one thing about a verdict: that its signer was not the PR's author. The
label pass beside it checked more — it honoured `ngit.reviewers` — so the thing that gated a
merge was looser than the thing that coloured a label, and the difference was invisible until a
stranger's approval landed a change. The other half was just as weak: an approval was final
forever, so a push after the review inherited it.

**One allowlist, one place.** The guard resolves `ngit.reviewers` through `cli.reviewer_keys`,
the same function `cmd_ngit_sync` uses to build `BridgeConfig.reviewers` before `Sync` exists.
An empty list permits nothing — never "anyone who tags the PR". The refusal names the npub it
saw and says which list refused it, because "not permitted" without a key is a message the
operator cannot act on.

**A verdict names its tip, in the comment.** The pass publishes the reviewer's words with a
`Tip: <sha>` line — theirs if they wrote one, otherwise the tip the pass knows, and only when
the comment is at least as new as the tip's event. The merge route reads that line back and
compares it with the branch head `git ls-remote` reports. An approval of an earlier revision
reads `approved` on the PR and is still refused at the merge (exit 9); the operator's answer is
a re-review of the tip that is there, which is the one act that turns an unreviewed revision
into a reviewed one. A verdict that cannot name its tip is refused rather than assumed to cover
whatever is on the branch now.

**The link row stops being a second opinion.** The bridge's `verdict`/`verdict_tip` note was
also read by the merge route (`ngit_merge.approval_problem`), which meant a cache could refuse a
merge the PR's own record allowed and allow one it refused — and it reported that refusal with
the card-state code (7), a third meaning for a code the merge route documents as "the card is
not in review or done". The rule now lives in `ngit_verdict` alone, reading the record; the note
stays as the pass's own bookkeeping. Exit 9 is a verdict refusal and 3 is a guard that could not
run (no config, no tip, no `ngit`), and nothing else.

## An unread config key is an advisory, never a refusal

`hng doctor` names the top-level config keys this build does not read in one advisory line: the
entry is marked `advisory`, renders as `note` rather than `ok`/`FAIL`, is counted separately from
the checks ("12/12 check(s) passed; 1 advisory"), and cannot decide the exit code. A key that is
deliberately ignored keeps its reason in the same line — `transport`, whose `nak` publisher is
deleted, so an older config still boots.

Why: config keys are set ahead of merges on purpose (a block the running build does not know must
not stop it), and the same silence that permits that is what lets residue accumulate across
upgrades. Refusing a start over a key would break the first case; saying nothing breaks the
second. The preflight can say "this build does not read it" without taking a side, and the
allowlist keeps the line from re-teaching the same exception: tolerated-but-ignored is a decision
with a reason, not an unknown. The set of read keys lives beside the doctor and is checked against
the shipped example, so a key added to the example without the merge that reads it is a red test
rather than a fresh install that ships with an advisory.

## A log line shortens keys; `-v` is how you read one whole

`gateway up: <pubkey> on N relays` printed the identity in hex, and the lines that did carry a
key either sliced it by hand or printed it whole. Neither is a rule, and a pasted `journalctl`
excerpt with a peer's npub beside the relays it appears on is a social graph that outlives the
incident. So the daemon installs one filter on its logger tree (`logs.py`) and the adapter
carries the same two patterns beside its own logger: `nsec1…` is always `nsec1[redacted]`, and
`npub1…` keeps eight characters unless the emitting logger is running at DEBUG. The startup
line now names the identity as an npub — the form a person recognises — so it is shortened at
INFO and whole at DEBUG.

**DEBUG prints the npub whole, and `hng serve -v` is the documented way to ask for it.** The
decision is the logger's effective level rather than the record's, because the startup line is
logged at INFO in both modes: a filter that keyed on the record would leave `-v` unable to show
the one key the runbook asks for.

Three things stay as they are, deliberately. **A 64-hex id is not touched**: event ids are the
store's debugging handle, and a hex pubkey is a key, but the two cannot be told apart by shape
and hiding the first would cost more than it protects. **Socket frames are not logs**: they are
the protocol, byte for byte, and the filter never sees them. **CLI output is evidence**: `hng
status`, `hng health`, `hng peers` and `hng adapter status` print the full value they always
printed, because the operator reading it is the one asking the question.

The filter rewrites the *record*, not a handler's output string, and it is attached to the
loggers that exist when logging is configured as well as to their handlers. That is what catches
a logger added later that propagates to the root handler, and what makes the rewrite visible to
every handler — a test's `caplog` included — rather than only the one it sits on. The adapter
cannot import the daemon's module (it is installed by copying one file into
`$HERMES_HOME/plugins/nostr`), so it carries the same two patterns with a comment naming the
daemon's copy as the source of truth. The `nostr-sdk` uniffi binding declares no Python logger at
all, so there is no third-party logger here to filter; if one appears, the root handler already
covers anything that propagates, and a binding that prints straight to stderr is outside the
logging tree and would need its own boundary.

## A standalone send is one socket request, not a second transport

Hermes can deliver from two processes: the gateway, which holds the platform adapter, and a
fresh `hermes send` or cron process, which holds only the plugin. The second one used to fail
with "No live adapter" because the plugin registered no `standalone_sender_fn`; every job and
note then named `hng send` instead. The gateway's socket already carries a `send` op, so the
fix is one request over it — no HTTP layer, no sidecar, no second daemon.

**No replay ledger.** The adapter's `_SendLedger` exists because Hermes re-sends a reply it
cannot prove landed; the daemon's `outbound_wraps` rows are the durable record of what was
published. A standalone caller has no recovery loop to suppress: a duplicate from a scheduled
job is a job configuration error, not a transport replay. Each call therefore carries a fresh
`id` and is published as asked. The daemon still keys on that id, so a caller that does retry
with the same id gets the message it already published rather than a second copy.

**The live path's bound, and the path in the error.** The daemon publishes before it answers,
and a recipient relay list that has to be fetched plus a relay that has to time out are both
part of that answer; a short request timeout would report failure for a message that lands.
So the standalone bound is the live send's 180s, and a caller with its own tighter bound (cron's
`standalone_send_timeout_seconds` defaults to 60) still owns that decision. A daemon that is not
running fails at connect, immediately. Every failure of this path says `the nostr gateway daemon
did not answer at <socket path>` — the first question an operator asks is which file was dialled,
and "No live adapter" answered a different one.

**`deliver=nostr` needs nothing else.** Cron resolves a bare platform through
`PlatformEntry.cron_deliver_env_var`, which the plugin already sets to `NOSTR_HOME_CHANNEL`;
with the sender registered, that destination has a transport. A missing home names the env var:
`hermes send --to nostr` asks for `NOSTR_HOME_CHANNEL` by name (Host's own home-channel error),
and the sender repeats it if it is ever called with no destination.

**A pubkey is target syntax, not a channel name.** Registration also adds
`parse_target_ref_fn`: without it a plugin platform has no native syntax, so
`hermes send --to nostr:<hex>` fails in target resolution before the sender is reached ("The
plugin parser did not recognize it"). The parser accepts exactly the forms the daemon addresses
itself — a 64-hex pubkey, an npub, `concord:<community>:<channel>`, `marmot:<group>` — and
leaves everything else to Hermes' own resolution. It says which strings this platform owns; the
daemon still validates the destination on the `send` op.

**Control text still stops at the adapter.** The standalone path uses the same
`is_control_text()` as the live `send()`, returns success so Hermes does not retry, and writes
no frame; only a destination explicitly named in `NOSTR_CONTROL_CHANNEL` is exempt. Text only:
attachments are refused, never silently dropped. The daemon's policy remains the one allowlist
— this adapter sets `enforces_own_access_policy`, and a second list here would be the drift that
flag exists to prevent.

## A re-review reads the history first

One card serves a PR for its life, so a revision hands the second reviewer the first one's
work — and the brief used to start again from the procedure. The card held the history (the
verdict comment, the revision note, the implementer's reply) and told nobody to read it; the
PR's own thread held the verdicts and the brief only said how to *publish* to it. A second
review could therefore re-derive a settled finding, miss an open one, or contradict the last
verdict without noticing — on a PR whose whole point is answering findings, the one context it
must not guess at.

**The brief opens with what was decided.** A card reopened by a revision or a pushed fix gets
a preamble before the procedure: every earlier verdict with its comment id, its time, the tip
it was given for and its findings in the comment's own words; the newest tip move; and the
last few comments on the card itself. It names both records — `ngit pr view <id> --comments`
and this card's thread — because a brief can quote a comment and cannot carry a conversation.
A first review passes no history and gets exactly the brief it always got.

**The tip is the bridge's knowledge, not the comment's.** A verdict covers the tip that was
current when it was published; the comment rarely says which commit that was, and the review
that needs to know is written after the tip has moved. So the pass records each verdict it
reads on the link row with the tip `tip_covered` attributes to it, and each move of the tip as
a revision performs it. Without the move log, a verdict given two revisions back could only be
reported as "over some earlier tip" — the one fact a re-review exists to be exact about.

**What the pass owes the last verdict.** A changes-requested verdict's findings become a
checklist: each one answered `fixed`, `still open` or `not addressed`, with the evidence, in
the new verdict. A conclusion the earlier verdict did not reach is stated as such, with the
reason: a contradiction between two verdicts is information, and neither re-litigating a
finding nor silently dropping one is allowed.

**Bounded on both sides.** The row keeps the newest ten verdicts, the brief prints the newest
five and quotes the newest three card comments, and a trimmed history says how many it left
out rather than pretending to be the whole thread. A PR that has been round the loop ten times
still gets a brief that fits in a card body, and the pointer to the PR's thread is what makes
the truncation safe: the complete record never moved.

## A comment to a bot is work, and the gate is the person

The review bot was addressable in principle and unreachable in practice. A `p` tag naming its
key was read by nothing: the mention pass compared against the gateway's own key, and every
other comment on a PR was read only for a verdict — so "Review Bot - re-review pls." sat in the
thread while the card stayed finished and the label stayed `changes-required` on a tip nobody
would look at.

**Roles are derived, not registered.** The gateway's key, the reviewer's (from
`ngit.review_secret_file`, through the derivation the verdict gate already uses) and the Deps
Bot's (`deps.signer`) are the identities this installation speaks as, and `roles.addressed`
resolves any of them from the same comment. Adding a bot is naming its key. A plane that reads a
new kind of message calls `addressed` and the one gate; it does not grow its own list.

**The gate belongs to the person, not the transport.** `control.trusted_npubs` decides whether a
request is work, exactly as it does for a mention, because naming a bot must not be a second way
to spend a worker turn. What differs by role is only the answer: the gateway replies in the
thread, the review role re-queues the PR's review (its answer *is* a verdict), and a role with
no profile — the Deps Bot — gets a task carrying the run that answers it.

**A request older than the card is answered already.** The card's completion time is the fact
that separates a request from a replay: re-queuing a review that has since happened would review
a tip nobody asked about. The decision is recorded once, refusals included, like every other
comment decision.

**The heal is a comparison, not an event.** The same pass also asks the board what a revision
asks: a still-open PR whose link records a tip newer than its card's completion has a card behind
its branch, whether or not the revision was noticed. That is the state an older version left
behind — it wrote the tip and had no re-open — and the state a missed event leaves. Comparing
costs one row per PR and cannot loop; waiting for the event again would wait forever.

## One review is one verdict comment, and the bridge is its only writer

The review card's brief told a reviewer that the bridge publishes its comment; the review skill
told it to publish by hand as well, and claimed the bridge would not then re-publish. Both fired.
The bridge treats an existing comment as its own only when the text is byte-identical, and a card
comment carrying a readback note or a gates summary never is -- so one review left two kind-1111
verdicts on the PR, and 22 of the 61 PRs carrying verdicts had that pair. PR 4e6493f7 got three
in 2m38s, the third from a second review card.

**The reviewer writes the comment; the bridge publishes it.** The brief says so, the skill says
so, and a hand publish is reserved for the cards the bridge cannot reach: a hand-made card with
no `ngit:pr:` key, a review older than the bridge, or a root the link row already marks `closed`
because a revision superseded it.

Why: the verdict is the record, and a record with two authors is not a record -- the label pass
and the merge guard both read the newest comment, so a duplicate is a second opinion nobody gave.
