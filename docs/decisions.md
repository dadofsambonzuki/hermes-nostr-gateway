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

## Two prefixes, and who may use them

`/queue <text>` and `/guide <text>` are read in the daemon *after* the policy decides the
sender's reach and authority, and are honoured only for `authority: instructions` **and**
`reach: immediate`. The command is stripped before the row is written, and the row and the frame
carry `mode: normal|queue|guide`.

Why: a mid-task message could previously only mean "another instruction", so a sender had no way
to say "next, not now" without interrupting the turn or being buried in the backlog. Parsing
after the policy is the safety property — a stranger's `/guide` is a word in their text, not a
verb the agent obeys — and parsing before the frame is what lets the row keep the intent, so a
replay of an unacknowledged message stays held instead of becoming a fresh instruction. A digest
tier sender's prefix is left as typed: a batch is not a turn, so there is nothing there to hold
behind or steer.

The decision is deliberately narrow: no other commands, no aliases, lowercase only, and a prefix
with no words after it is text. The gateway is not becoming a command parser, and Hermes is not
being reimplemented — the adapter maps `mode` onto Hermes' own `/queue` and `/steer`.

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

systemd runs the daemon; cron runs the bridge; the daemon may optionally wake the bridge on an
event. Cron stays the floor, so a missed event or a restart costs latency, not correctness.

Why: the daemon is a long-lived process that owns a socket and a subscription per relay, so its
lifecycle needs a supervisor — the operator restarts it, and `hng status` reports on it. A pass has
no lifecycle to own: it is a command that reads to a cursor, writes down what it decided and exits,
so its scheduler can be the host's own cron, with no supervision, no in-memory state and no restart
to get wrong. Waking a pass from the daemon's read path is then an optimisation, not a dependency:
the same pass runs sooner than the next tick, and because every pass is idempotent and
cursor-driven, losing one to a restart or a busy daemon costs the channel minutes and never a
message. Making cron the fallback rather than the trigger would put the bridge's correctness behind
the daemon's liveness, which is the coupling this refuses.

