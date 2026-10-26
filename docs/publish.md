# Publishing: one signed event, one client, per-relay truth

Plan P2. This records what the write path is now, what it was, and how to go back.

## The decision

Every publish goes through a single `nostr-sdk` `Client` in this process
(`src/hermes_nostr_gateway/publisher.py`). The per-relay `nak event --auth` children are
gone from the default path.

The old publisher did not publish; it spawned a signer. Each relay meant a child, so a
DM to three relays plus the recipient's own inbox relays was up to six processes, each
carrying our secret in `NOSTR_SECRET_KEY`, each answering NIP-42 on its own, and each
reporting only what its stdout happened to say. A retry was a new child, which is
exactly the shape that turns "resend the same event" into "sign it again and hope".

| Concern | Legacy (`transport.legacy: true`) | Bindings (default) |
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
that per relay in `outbox_wraps`. `BindingsPublisher` and `NakPublisher` both satisfy it,
so `make_publisher()` is the only place that knows which transport is running.

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

- **Ad-hoc reads** — `Gateway._load_follows()` (the kind-3 lookup at startup) and
  `hng --once` / `OneShot` still shell out to `nak`. They are one-shot reads, not
  workers; moving them is a separate change.
- **`transport.legacy: true`** — the rollback, below.

So `ps` on a running gateway shows `nak` only for those one-shot reads, and never for a
publish.

## Rolling back

```yaml
transport:
  legacy: true
```

That selects `NakPublisher` (`nak event --auth` per relay) and logs a warning at startup.
It is kept for one release, until a relay that behaves differently under the bindings has
been compared side by side; the flag will be removed together with the class — and it is
the same switch the read pool uses when P1 lands, so one line rolls back both transports.

## How it is tested

`tests/test_publisher.py` runs a real WebSocket relay locally: it records the EVENT frames
and answers OK, so the claims are wire facts rather than mocks. It proves that every relay
receives the same signed bytes, that a transport failure is retried with a byte-identical
frame, that a `blocked` rejection is not retried, that a NIP-42 challenge is answered with
kind 22242 tags for that challenge and relay, that an unverifiable event opens no
connection, that the relay set is pruned, and that a whole `Gateway.send` DM reaches two
relays with two wraps each while `subprocess.run` and `create_subprocess_exec` raise if
called.
