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

