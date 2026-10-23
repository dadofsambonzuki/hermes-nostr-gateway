# nostr-dm-gateway — socket API and trust model (v1)

Status: **approved for build** (decisions §11 resolved 2026-10-23). Supersedes the phase-0
wrapper as the design target; the wrapper stays as the protocol conformance reference (its
four traps are encoded in §4 and §7 below).

Name is deliberately protocol-first. There is **no "Armada gateway"**: Armada is a client
that speaks NIP-17 with a legacy NIP-04 plane. Serving the NIPs serves every client at once.

---

## 1. Scope

**In (v1):** 1:1 private DMs over Nostr — NIP-17 (kind 14 rumor → kind 13 seal → kind 1059
gift wrap) and legacy NIP-04 (kind 4). Attachments via NIP-92 `imeta` + Blossom. Reactions
(kind 7 inside the wrap). Relay discovery via kind 10050. A local socket API for agent
runtimes. Allowlist-only delivery. Shared identity with wn-agent under §2.

**Out (v1):** group chat (Marmot's job, and NIP-29's), typing indicators, calls, edits,
deletions, multi-device sync, any web UI.

**Out (ever):** message content interpretation, tool execution, or auto-reply logic. The
gateway transports bytes; what an agent does with them belongs to the adapter and the agent,
under the contract in §10.

---

## 2. Identity and key ownership

The gateway owns **one key per identity** and every transport for that identity runs inside
this one process.

**Decision A (approved): share the npub with wn-agent, disjoint-kind.** Two processes signing
the same key is safe only if their event spaces are disjoint, so v1 states the invariant:

| Kind | Owner |
|---|---|
| 4, 7, 13, 14, 15, 1059, 10050 | gateway |
| 0, 3, 10002, 443/444/445 (Marmot), group events | wn-agent |

The gateway MUST NOT publish profile (0), contact list (3), relay list (10002) or any Marmot
kind. A startup check refuses to run if a configured `owned_kinds` entry collides with the
wn-agent set. If that rule ever has to bend, introduce a signing broker and delete this table.

The key is read from `~/.config/nostr-dm/identities/<npub>.json` (mode 0600, dir 0700,
`{"npub":…,"secret_hex":…,"transports":["nip17","nip04"]}`). It is then held in memory and
never passed through argv or the environment. `gateway identity import --from <path>` copies
key material from an existing store (e.g. the wn-agent account file) at setup time.

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

## 4b. Media and reactions (v1)

**Attachments ride on the text rumor** as NIP-92 `imeta` tags — not a separate kind — because
that is what the clients we care about already render (Armada re-expresses `imeta` when
forwarding). One tag per attachment:

```
["imeta", "url <https://blossom.example/<sha256>>", "m <mime>", "x <sha256>",
 "size <bytes>", "dim <WxH>", "alt <description>"]
```

- **Outbound:** bytes go to Blossom first (`blossom_servers`, default the day.ag haven); the
  response is verified (sha256 + size match what was uploaded), then the rumor is built with
  the returned URL. Caps: 25 MB per file, 4 files per message, MIME allowlist (`image/*`,
  `audio/*`, `video/mp4`, `application/pdf`, `text/plain`). Refuse **before** uploading, so a
  rejected file never becomes a public blob.
- **Inbound:** parse `imeta`, download to `~/.local/share/nostr-dm/media/<sha256>`, verify the
  hash, sniff the MIME rather than trusting the tag, enforce the same caps, and hand the
  adapter a path plus metadata — never bytes, never a shell-escaped name. Downloads are lazy
  (on `media.get`, not on receipt) so a hostile sender cannot make us pull 25 MB by sending a
  message.
- **Reactions:** kind 7, content `+` or an emoji, tags `e`=<target rumor id>, `p`=<target
  author>, sealed and wrapped exactly like a kind 14. Stored against the target and matched on
  receive; a reaction to a rumor we do not hold is stored unresolved rather than dropped.
  Un-reaction is a kind 5 deletion targeting the reaction's own rumor id, inside a wrap.
- **Receipts stay out of scope:** they leak presence and no v1 caller needs them.

---

## 5. Local API

AF_UNIX socket, `SOCK_STREAM`, NDJSON both ways: one JSON object per line, request/response
correlated by `id`, server-initiated frames keyed by `type`. Path
`$XDG_RUNTIME_DIR/nostr-dm/<npub>.sock` (fallback `~/.local/share/nostr-dm/run/<npub>.sock`),
socket 0600, directory 0700. No TCP in v1; no HTTP.

| op | args | returns |
|---|---|---|
| `identities.list` | — | `npub`, transports, relay counts |
| `status` | — | version, uptime, queue depth, per-relay state, allowlist size |
| `send` | `identity`, `to`, `text`, `transport=auto`, `reply_to?`, `attachments?: [{path, alt?}]` | `rumor_id`, `event_ids`, `relays_ok[]`, `relays_failed[]`, `sent_at`, `attachments[]` |
| `react` | `identity`, `to`, `rumor_id`, `emoji="+"` | `reaction_rumor_id`, per-relay results |
| `media.get` | `identity`, `sha256` or `url` | `path`, `mime`, `size`, verified `sha256` |
| `history` | `identity`, `peer`, `limit`, `before?` | messages, newest first |
| `peers` | `identity` | known peers + last activity + blocked count |
| `ack` | `cursor` | advances the durable cursor (§7) |
| `subscribe` | `since_cursor?` | ack + stream of frames |

Server frames:

```
{"type":"message","cursor":41,"identity":"npub1…","from":"npub1…","transport":"nip17",
 "text":"…","created_at":1792…,"reply_to":null,"rumor_id":"…","received_at":1792…,
 "attachments":[{"url":"https://…","mime":"image/jpeg","size":123456,"sha256":"…","status":"available"}]}
{"type":"reaction","cursor":42,"target_rumor_id":"…","from":"npub1…","emoji":"🔥","resolved":true}
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
- `attachments(sha256 PK, rumor_id, mime, size, url, local_path, fetched_at)`
- `reactions(reaction_id PK, target_rumor_id, identity, peer, emoji, created_at, resolved)`
- `wraps_seen(wrap_id PK, rumor_id, seen_at)` — wrap-level replay guard
- `outbox(rumor_id PK, identity, peer, event_json, attempts, next_attempt_at, state)`
- `relay_state(url PK, identity, last_ok_at, last_error, requires_auth)`
- `cursors(identity PK, acked INTEGER)`

`cursor` is a per-identity monotonic integer assigned at insert. It never moves backwards, so
a consumer that sees a cursor jump knows it missed acked-but-undelivered frames.

---

## 7. Delivery semantics

At-least-once, consumer-acked. A frame is delivered when it is inserted; the cursor advances
only on `ack`. On reconnect the gateway replays from `acked+1` — so a crash between insert and
ack re-delivers rather than loses. Consumers must be idempotent on `rumor_id` / `reaction_id`.

Outbox: send is durable before it is attempted. Failures retry with exponential backoff
(1, 2, 5, 15, 60 s; 6 attempts, then `state=dead` plus a `delivery` frame with `ok:false`).
A retry re-publishes the *same* signed events — never re-wraps, which would change event ids
and risk a duplicate in the peer's client.

---

## 8. Rust implementation map

| Concern | Crate |
|---|---|
| Relays, signing, NIP-44/59, NIP-92 | `nostr-sdk` (`nostr` re-exports nip59) |
| Runtime | `tokio` (multi-thread; `UnixListener` for the socket) |
| Storage | `rusqlite` (bundled) |
| Socket codec | `serde` / `serde_json`, newline framing |
| Blossom | `reqwest` (rustls), `sha2` |
| MIME sniffing | `infer` |
| CLI / logging | `clap`, `tracing` + `tracing-subscriber` |
| Config | `serde` + `figment` (file + env precedence) |

Modules: `identity/`, `transport/{nip17,nip04}.rs`, `relay/` (pool, auth, discovery),
`store/`, `media/`, `api/` (socket), `policy/` (allowlist, rate limits, dedupe), `bin/`.
One crate, one binary, no network listener — wn-agent's operational shape.

**Milestones:** M1 identity + NIP-17 text + allowlist + socket (the family case); M2 NIP-04;
M3 media; M4 reactions; M5 docs, container, CI, conformance suite.

---

## 9. Process and deployment

- One systemd unit per identity (`nostr-dm-gateway@<npub>.service`), `Restart=always`,
  `RuntimeDirectory`, socket 0600, no network listener.
- `SIGTERM` drains the outbox attempt in flight, closes the stream, exits 0.
- Health: `status` over the socket; a heartbeat file with mtime + version for monitors that
  cannot speak the protocol.
- Config: `~/.config/nostr-dm/config.json` — `default_read_relays`, `default_write_relays`,
  `blossom_servers`, `max_message_bytes`, `max_attachment_bytes`, `dedupe_window_days`,
  `log_message_text` (default false).

---

## 10. Trust model (the part to publish first)

Written as a contract the daemon enforces, not advice for callers:

1. **Deny by default.** Inbound messages are stored but **not delivered** to the adapter
   unless the sender's hex is in `allowed_senders` for that identity
   (`~/.config/nostr-dm/allowlist.json`, hot-reloaded, per-identity). Blocked messages emit a
   `blocked` frame with a reason and are never replied to.
2. **The gateway never interprets content.** No URL fetching beyond the configured Blossom
   servers, no command parsing, no policy decisions about replying. It is a transport.
3. **No auto-reply, ever, from the gateway.** An agent may choose to reply; the gateway will
   not, and it will not send read receipts at all in v1 (receipts leak presence).
4. **Rate limits.** Per-sender token bucket: burst 10, sustained 30/hour. Over-limit is
   dropped with a `blocked` frame (`reason:"rate_limited"`). Global in-flight publish cap 8.
   An allowlisted peer that starts flooding degrades to blocked, not to a queue that grows.
5. **Replay/dup:** wrap id and rumor id both deduped durably; a repeated wrap is a no-op.
   `created_at` outside `[now-3d, now+5m]` is rejected as `stale_or_future`.
6. **Keys:** 0600 file, in-memory only, never logged; a `status` frame reports the npub only.
7. **Logging:** metadata by default (peer, transport, size, ids); message text only with an
   explicit debug flag, and the README says so.
8. **Media:** quarantine directory, hash-verified, size/MIME capped, fetched lazily, never
   executed, never handed to the agent as bytes; outbound refused before upload when over cap.
9. **One writer per key-kind set** (§2) so this daemon cannot corrupt the Marmot identity.
10. **No inbound trust escalation by relay.** A message arriving on our own relay is not more
    trustworthy than one from elsewhere; authorisation is the allowlist, not the transport.

---

## 11. Adapter contract (what Hermes/OpenClaw implement)

~200–400 lines, no protocol knowledge:

1. connect to the socket, `subscribe`;
2. map `message` / `reaction` frames to inbound events;
3. `ack` once durably queued;
4. `send` / `react` for replies, passing the peer npub and `reply_to` when threading;
5. `media.get` only when the agent asks for the file;
6. surface `blocked`/`delivery` frames as warnings, never as user-visible chat.

The adapter MUST NOT re-implement allowlisting, unwrapping, relay choice, retries or media
fetching — if it needs to, the gateway is missing an operation.

---

## 12. Decisions (resolved 2026-10-23)

| # | Decision | Outcome |
|---|---|---|
| A | Key ownership | **Share Holly's npub with wn-agent**, disjoint-kind table enforced at startup |
| B | Language | **Rust + `nostr-sdk`** |
| C | NIP-04 in v1 | **Yes**, same process, allowlist-gated |
| D | Allowlist authority | **Gateway-side**, mirrored read-only in the adapter |
| E | v1 scope | Text 1:1 + both planes **+ media + reactions**; docs/container/CI before publishing |

---

## 13. Acceptance tests (v1 is done when all pass)

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
11. Send a 2 MB image → Blossom upload verified by hash; `imeta` carries `url`, `m`, `x`,
    `size`; the wrap is accepted by ≥1 relay.
12. Inbound attachment → `media.get` returns a hash-verified file in quarantine with a sniffed
    MIME, and nothing was fetched before the call.
13. A 30 MB attachment is refused before upload; no blob is published.
14. Reaction round trip → stored, matched to its target, `reaction` frame emitted; a reaction
    to an unknown rumor is stored unresolved rather than dropped.
15. NIP-04 in and out with an allowlisted peer, and no duplicate when that peer also has
    NIP-17.
