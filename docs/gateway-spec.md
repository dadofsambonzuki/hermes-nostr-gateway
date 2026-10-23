# nostr-dm-gateway — socket API and trust model (v1 draft)

Status: **draft for approval, no code yet.** Supersedes the phase-0 wrapper as the design
target; the wrapper stays as the protocol conformance reference (its four traps are encoded
in §4 and §9 below).

Name is deliberately protocol-first. There is **no "Armada gateway"**: Armada is a client
that speaks NIP-17 with a legacy NIP-04 plane. Serving the NIPs serves every client at once.

---

## 1. Scope

**In:** 1:1 private DMs over Nostr — NIP-17 (kind 14 rumor → kind 13 seal → kind 1059 gift
wrap) and legacy NIP-04 (kind 4). Relay discovery via kind 10050. A local socket API for
agent runtimes. Allowlist-only delivery.

**Out (v1):** group chat (that is Marmot's job, and NIP-29's), typing indicators, calls,
reactions, edits, deletions, media attachments, multi-device sync, any web UI.

**Out (ever):** message content interpretation, tool execution, or auto-reply logic. The
gateway transports bytes; policy about what an agent does with them belongs to the adapter
and the agent, under the contract in §8.

---

## 2. Identity and key ownership

The gateway owns **one key per identity** and every transport for that identity runs inside
this one process.

**Decision A — sharing the npub with wn-agent (Marmot).** Two processes signing the same key
is safe *only* if their event spaces are disjoint, so v1 states that invariant explicitly:

| Kind | Owner |
|---|---|
| 4, 13, 14, 1059, 10050 | gateway |
| 0, 3, 10002, 443/444/445 (Marmot), group events | wn-agent |

The gateway MUST NOT publish profile (0), contact list (3), relay list (10002) or any Marmot
kind. If that rule ever has to bend, introduce a signing broker and delete this table.

The key is read from `~/.config/nostr-dm/identities/<npub>.json` (mode 0600, dir 0700,
`{"npub":…,"secret_hex":…,"transports":["nip17","nip04"]}`). It is then held in memory and
never passed through argv or the environment. `gateway identity import --from <path>` can
copy key material from an existing store (e.g. the wn-agent account file) at setup time.

Multiple identities are supported (one socket namespace each); a single-process default is
expected.

---

## 3. Relay policy

- **Read** hinges on the identity's `kind 10050` (DM relay list). Senders deliver wraps to
  those relays; the gateway streams `{kinds:[1059], "#p":[me], since: now-3d, until: now+5m}`
  there, plus `{kinds:[4], "#p":[me]}` for the legacy plane.
- The 3-day `since` is not slack, it is a correctness requirement: NIP-59 randomises gift-wrap
  `created_at` up to two days into the past.
- **Write** uses the *recipient's* 10050 when known (cache it; refresh on miss), else the
  identity's own 10050, else `default_write_relays` from config. Never fan out to a long tail.
- **NIP-42**: relays that require auth (our own `relay.day.ag/chat` and `/inbox`) must be
  authenticated as the identity before publish or stream. Auth failure on one relay is a
  per-relay failure, not a send failure.
- **Success = at least one relay accepted**, reported per relay. Bursts can trip a relay's
  rate limiter; retry once after a short jitter, then report.
- `gateway relays publish` writes/repairs the identity's 10050 so senders can find us.

---

## 4. Transport details that must be encoded, not remembered

1. **NIP-17 send:** rumor kind 14, tags `p`=<recipient hex>, optional `e`=<rumor id being
   replied to>; seal kind 13 NIP-44 to the recipient, signed by the identity; wrap kind 1059
   NIP-44 with a throwaway key, `p`=<recipient>, `created_at` randomised in `[now-2d, now]`.
   Publish the peer copy **and** the self-copy (same rumor wrapped to our own key).
2. **The wrap `p` tag must equal the recipient's identity pubkey.** A NIP-4E decoupled key
   silently changes this and every standard client misses the message with no error. The
   gateway asserts it before publishing and refuses to send otherwise.
3. **Inbound:** unwrap 1059 → verify seal author == rumor author → the rumor's author is the
   sender. Accept only when `p` == our identity on the wrap. Kind 21059 ephemeral wraps are
   dropped in v1 (typing/call signals).
4. **NIP-04 accept:** `p` == us, author allowlisted, content `<ciphertext>?iv=…`. Sent only
   when the caller asks for it or (config) when the peer's 10050 advertises no NIP-17
   capability. Never mirrored to both planes by default — double delivery confuses clients.

---

## 5. Local API

AF_UNIX socket, `SOCK_STREAM`, NDJSON both ways: one JSON object per line, request/response
correlated by `id`, server-initiated frames keyed by `type`. Path
`$XDG_RUNTIME_DIR/nostr-dm/<npub>.sock` (fallback `~/.local/share/nostr-dm/run/<npub>.sock`),
socket 0600, directory 0700. No TCP in v1; no HTTP.

Requests (fields omitted for brevity; all carry `id`, `op`):

| op | args | returns |
|---|---|---|
| `identities.list` | — | `npub`, transports, relay counts |
| `status` | — | version, uptime, queue depth, per-relay state, allowlist size |
| `send` | `identity`, `to`, `text`, `transport=auto`, `reply_to?` | `rumor_id`, `event_ids`, `relays_ok[]`, `relays_failed[]`, `sent_at` |
| `history` | `identity`, `peer`, `limit`, `before?` | messages, newest first |
| `peers` | `identity` | known peers + last activity + blocked count |
| `ack` | `cursor` | advances the durable cursor (see §7) |
| `subscribe` | `since_cursor?` | ack + stream of `message` frames |

Server frames:

```
{"type":"message","cursor":41,"identity":"npub1…","from":"npub1…","transport":"nip17",
 "text":"…","created_at":1792…,"reply_to":null,"rumor_id":"…","received_at":1792…}
{"type":"blocked","from":"npub1…","reason":"not_allowlisted","rumor_id":"…"}
{"type":"delivery","rumor_id":"…","ok":true,"relays_ok":["wss://…"]}
{"type":"status","relays":{…},"queue_depth":0}
```

Text is capped at 8 KB inbound and outbound; larger is truncated with `"truncated":true`
rather than dropped, so the agent can still see that something arrived.

---

## 6. Storage

SQLite (WAL) at `~/.local/share/nostr-dm/state.db`:

- `identities(npub PK, secret_hex, created_at)`
- `messages(rumor_id PK, identity, peer, direction, transport, content, created_at,
  received_at, cursor INTEGER, truncated, blocked_reason)`
- `wraps_seen(wrap_id PK, rumor_id, seen_at)` — wrap-level replay guard
- `outbox(rumor_id PK, identity, peer, event_json, attempts, next_attempt_at, state)`
- `relay_state(url PK, identity, last_ok_at, last_error, requires_auth)`
- `cursors(identity PK, acked INTEGER)`

`cursor` is a per-identity monotonic integer assigned at insert. It never moves backwards, so
a consumer that sees a cursor jump knows it missed acked-but-undelivered frames.

---

## 7. Delivery semantics

At-least-once, consumer-acked. A `message` frame is delivered when it is inserted; the cursor
advances only on `ack`. On reconnect the gateway replays from `acked+1` — so a crash between
insert and ack re-delivers rather than loses. Consumers must be idempotent on `rumor_id`.

Outbox: send is durable before it is attempted. Failures retry with exponential backoff
(1, 2, 5, 15, 60 s; 6 attempts, then `state=dead` plus a `delivery` frame with `ok:false`).
A retry re-publishes the *same* signed events — never re-wraps, which would change event ids
and risk a duplicate in the peer's client.

---

## 8. Trust model (the part to publish first)

Written as a contract the daemon enforces, not advice for callers:

1. **Deny by default.** Inbound messages are stored but **not delivered** to the adapter
   unless the sender's hex is in `allowed_senders` for that identity
   (`~/.config/nostr-dm/allowlist.json`, hot-reloaded, per-identity). Blocked messages emit a
   `blocked` frame with a reason and are never replied to.
2. **The gateway never interprets content.** No URL fetching, no command parsing, no policy
   decisions about replying. It is a transport.
3. **No auto-reply, ever, from the gateway.** An agent may choose to reply; the gateway will
   not, and it will not send read receipts to peers at all in v1 (receipts leak presence).
4. **Rate limits.** Per-sender token bucket: burst 10, sustained 30/hour. Over-limit is
   dropped with a `blocked` frame (`reason:"rate_limited"`). Global in-flight publish cap 8.
   An allowlisted peer that starts flooding degrades to blocked, not to a queue that grows.
5. **Replay/dup:** wrap id and rumor id both deduped durably; a repeated wrap is a no-op.
   `created_at` outside `[now-3d, now+5m]` is rejected as `stale_or_future`.
6. **Keys:** 0600 file, in-memory only, never logged; a `status` frame reports the npub only.
7. **Logging:** metadata by default (peer, transport, size, ids); message text only with an
   explicit debug flag, and the README says so.
8. **Media:** v1 ignores attachments and fetches nothing. v2 will download to a quarantine
   directory with size and MIME limits, and still never hand bytes to the agent unasked.
9. **One writer per key-kind set** (§2) so this daemon cannot corrupt the Marmot identity.
10. **No inbound trust escalation by relay.** A message arriving on our own relay is not more
    trustworthy than one from elsewhere; authorisation is the allowlist, not the transport.

---

## 9. Process and deployment

- One systemd unit per identity (`nostr-dm-gateway@<npub>.service`), `Restart=always`,
  `RuntimeDirectory`, socket 0600, no network listener.
- `SIGTERM` drains the outbox attempt in flight, closes the stream, exits 0.
- Health: `status` over the socket; a heartbeat file with mtime + version for monitors that
  cannot speak the protocol.
- Config: `~/.config/nostr-dm/config.json` — `default_read_relays`, `default_write_relays`,
  `max_message_bytes`, `dedupe_window_days`, `log_message_text` (default false).

---

## 10. Adapter contract (what Hermes/OpenClaw implement)

~200–400 lines, no protocol knowledge:

1. connect to the socket, `subscribe`;
2. map a `message` frame to an inbound "DM from X: text";
3. `ack` after the message is durably in the runtime's queue;
4. `send` for replies, passing the peer npub and `reply_to` when threading;
5. surface `blocked`/`delivery` frames as warnings, never as user-visible chat.

The adapter MUST NOT re-implement allowlisting, unwrapping, relay choice or retries — if it
needs to, the gateway is missing an operation.

---

## 11. Decisions I need from you

| # | Question | Recommendation |
|---|---|---|
| A | Share Holly's npub with wn-agent, or a separate DM identity? | Share it, with the disjoint-kind table in §2 enforced by a startup assertion |
| B | Language | Rust + `nostr-sdk`, matching wn-agent's operational shape (one static binary, systemd, no runtime deps). Python would be quicker to a private v1 but you would rewrite crypto edge cases to publish it |
| C | NIP-04 in v1? | Yes — one process, both planes. Family clients are not all NIP-17, and a separate legacy daemon would fight this one over the same relays |
| D | Allowlist authority | Gateway-side (deny by default), mirrored read-only in the adapter |
| E | Publish target for v1 | Text-only 1:1 + both planes + docs + container + CI. Media/reactions after |

---

## 12. Acceptance tests (v1 is done when all pass)

1. Send to a throwaway key; assert wrap `p` == recipient, and that a second key can decrypt
   the rumor and see the text.
2. Receive from an allowlisted key → row in `messages`, cursor assigned, `message` frame.
3. Receive from an unlisted key → stored, `blocked` frame, no delivery, no reply.
4. Re-publish the same wrap → no second row, no second frame.
5. A wrap stamped 47 h ago → accepted; 4 days old → rejected `stale_or_future`.
6. Kill a relay mid-send → `relays_failed` names it, outbox retries, success on ≥1 relay.
7. Publish through an auth-required relay (`relay.day.ag/chat`) → NIP-42 then success.
8. 11 messages from one sender inside a minute → 10 delivered, 11th `rate_limited`.
9. Self-copy round trip → own sent message appears in `history`.
10. `kill -9` after insert, before ack → consumer replays from `acked+1`, no loss, no duplicate.
