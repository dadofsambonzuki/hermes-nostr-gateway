# Publishing: one signed event, one client, per-relay truth

Plan P2. This records what the write path is now, and what it replaced.

## The decision

Every publish goes through a single `nostr-sdk` `Client` in this process
(`src/hermes_nostr_gateway/publisher.py`). The per-relay `nak event --auth` children are
deleted.

The old publisher did not publish; it spawned a signer. Each relay meant a child, so a
DM to three relays plus the recipient's own inbox relays was up to six processes, each
carrying our secret in `NOSTR_SECRET_KEY`, each answering NIP-42 on its own, and each
reporting only what its stdout happened to say. A retry was a new child, which is
exactly the shape that turns "resend the same event" into "sign it again and hope".

| Concern | The `nak` child (removed) | Bindings (now) |
| --- | --- | --- |
| A send | `nak event --auth` child per relay | `client.send_event(target=relay)` |
| NIP-42 | `NOSTR_SECRET_KEY` in the child's env | `SignerAuthenticator` over our `Keys`, in-process |
| Result | `"success"` substring in stdout | `SendEventOutput.success` / `.failed` per relay |
| Retry | new child, secret re-exported | the same `Event` object, resent |
| Secret exposure | every child's environment | never leaves this process |
| Shutdown | children killed with the process | `Client.shutdown()` |

## What stays the same

The contract the rest of the gateway sees is unchanged: `publish(event, relays)` returns
`{relay: (ok, error)}`, keyed by the caller's own relay strings, and the daemon records
that per relay in `outbox_wraps`. `BindingsPublisher` is the only implementation;
`make_publisher()` builds it with `relays.publish` pinned and nothing else to choose.

The event is signed exactly once, upstream, in `crypto.wrap`. The publisher parses it
(`Event.from_json`), verifies the signature, and sends that object to every relay. A
retry — in the same call, or from `store.pending_wraps` in the outbox loop — resends the
same bytes, so the event id never changes and the peer sees one message (spec §7).

An event that fails local verification is never sent at all: a relay would reject it, and
`(False, "event failed local verification")` is a better diagnosis than a relay's.

## NIP-42, and why a failed relay is retried once

A relay that requires AUTH sends its challenge around the first EVENT, not before it. The
client answers in-process (`Authenticator` over our keys, kind 22242 with the challenge and
relay tags), but the event it already sent was refused. So a failed relay gets one more
send inside the same call, after a short pause — that second send is the one the relay
accepts. Anything the relay *decided* (`blocked:`, `invalid:`, `rate-limited:`, `pow:`)
is not retried here: those go to the durable outbox, which has the backoff and the TTL.

The `uniffi` binding calls the async signer from a Rust worker thread, where there is no
running loop. `_bind_loop()` hands it the gateway's loop first; without that, every auth
attempt dies with `no running event loop` and a restricted relay closes the write.

## The relay set is bounded

A persistent client keeps a connection per relay it has used, and recipients' 10050 lists
change per message. So the client's set is capped (`max_relays`, default 24) and the
least-recently-used relay is removed once it is no longer needed. Relays from
`relays.publish` are pinned and never pruned; the current message's targets are protected
for the duration of the send.

## What is still a subprocess

Nothing. Ad-hoc reads (the kind-3 contact list, the peer kind-10050 inboxes, `hng listen`
in both `--once` and live modes) read through the bindings via `fetch.py`, and publishing
runs on the client described above. `ps` on a running gateway shows no `nak` on any config.

## No rollback flag

The subprocess publisher is deleted, so there is nothing for a config line to bring back.
A relay that behaves differently is diagnosed from `status.publish`: it reports
`mode: bindings` with per-relay `sent`/`failed` counts and the last error, and the durable
outbox retries the same signed event when the relay recovers. A stale `transport:` block
left in an older config file is ignored, not an error — the daemon starts and publishes
through the client.

## How it is tested

`tests/test_publisher.py` runs a real WebSocket relay locally: it records the EVENT frames
and answers OK, so the claims are wire facts rather than mocks. It proves that every relay
receives the same signed bytes, that a transport failure is retried with a byte-identical
frame, that a `blocked` rejection is not retried, that a NIP-42 challenge is answered with
kind 22242 tags for that challenge and relay, that an unverifiable event opens no
connection, that the relay set is pruned, that `make_publisher()` ignores a stale
`transport:` block in an old config, and that a whole `Gateway.send` DM reaches two
relays with two wraps each while `subprocess.run` and `create_subprocess_exec` raise if
called.
