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
