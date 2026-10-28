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

## Concord is supported, not experimental

Concord (Armada/Vector) is a first-class plane. Do not gate it behind a feature flag, label it
experimental in docs, leave it out of the health frame, or make its tests opt-in.

Why: it runs in production for this gateway and carries real community traffic. A flag that
defaults off means the plane is only exercised by whoever remembers to set it, and "experimental"
in a doc is how a supported transport quietly stops being maintained.

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

