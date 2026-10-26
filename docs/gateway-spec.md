# hermes-nostr-gateway — planes, socket API and trust model (v2)

Status: **approved for build** — v2 2026-10-23: planes and attention policy added, seed code
identified. Canonical home: this repository, published over NIP-34. Supersedes the phase-0
wrapper as the design target; that wrapper is now retired (`nip17_wrapper.py` deleted — `hng
send` / `hng listen` and the daemon cover it), and its four traps are encoded in §4 and §7 and
checked executably by `scripts/probe_nip17_conformance.py`.

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

- **Read** hinges on the identity's `kind 10050` (DM relay list) **and the sender's app-relay set**.
  Measured over four real DMs (Oct 2026): 2 of 4 existed *only* on `relay.ditto.pub`,
  `relay.dreamith.to` and `relay.eu.whitenoise.chat` — none of which the adapter read — so they
  were missed with no error and no log line. Stream `{kinds:[1059], "#p":[me], since: now-3d,
  until: now+5m}` over 10050 **plus** the app-relay defaults (ditto, dreamith, eu/us
  whitenoise), all configurable.
- **Peer coverage closes the fixed-list gap.** At startup and on
  `relays.peer_inbox.refresh_minutes` (default 60; `0` = once), resolve the kind-10050 list of
  every pubkey named in `groups` and add those relays to the DM read set: deduplicated against
  `relays.read` on a normalised URL, capped by `relays.peer_inbox.max_extra` (default 4, hard,
  for the process lifetime), one DM subscription each. `enabled: false` turns it off. A failed
  or slow lookup must never remove a relay already being read, and an added relay that refuses
  a kind is negotiated exactly like any other relay.
- A relay being down or empty is invisible: `relay.damus.io` answered 503 for a whole day
  (0 events) and the operator's own relay held **zero** wrappers. Log **per-relay event counts**
  and surface a relay that has delivered nothing while others have.
- The 3-day `since` is not slack, it is a correctness requirement: NIP-59 randomises gift-wrap
  `created_at` up to two days into the past. Watermark on the **inner rumor** time, not the
  wrapper's.
- **Write** uses the *recipient's* 10050 when known (cache it; refresh on miss), else the
  identity's own 10050, else `default_write_relays` from config. Never fan out to a long tail.
- **NIP-42**: relays that gate reads on web-of-trust (a self-hosted relay typically does) must be
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

- **Outbound:** bytes go to Blossom first (`blossom_servers`, default: the configured server); the
  response is verified (sha256 + size match what was uploaded), then the rumor is built with
  the returned URL. Caps: 25 MB per file, 4 files per message, MIME allowlist (`image/*`,
  `audio/*`, `video/mp4`, `application/pdf`, `text/plain`). Refuse **before** uploading, so a
  rejected file never becomes a public blob.
- **Inbound:** parse `imeta`, download to the gateway's state directory
  (`<state>/media/<sha256>`), verify the hash, sniff the MIME rather than trusting the tag,
  enforce the same caps, and hand the adapter a path plus metadata — never bytes, never a
  shell-escaped name. Downloads are lazy (on `media.get`, not on receipt) so a hostile
  sender cannot make us pull 25 MB by sending a message. A blob is fetched only when a
  stored message references it: the message's own url is used when it ends in that sha256,
  is https and resolves to a public address (no redirects), and the configured
  `blossom_servers` are then tried by hash. The hash check is what makes a peer's server
  safe to talk to: a wrong body is refused, never cached.
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
| `health` | `quiet_minutes?`, `pending_stale_minutes?` | per plane: last event + age, pending, blocked, degraded + reasons; relay totals; one `summary` line |
| `send` | `identity`, `to`, `text`, `transport=auto`, `reply_to?`, `attachments?: [{path, alt?}]` | `rumor_id`, `event_ids`, `relays_ok[]`, `relays_failed[]`, `sent_at`, `attachments[]` |
| `react` | `identity`, `to`, `rumor_id`, `emoji="+"` | `reaction_rumor_id`, per-relay results |
| `media.get` | `identity`, `sha256` or `url` (or `rumor_id`) | `path`, `mime`, `size`, verified `sha256`, `cached` |
| `history` | `identity`, `peer`, `limit`, `before?` | messages, newest first |
| `drops` | `plane?`, `reason?`, `since?` (epoch seconds), `limit?` | refused messages: id, plane, sender, `reason`, `why`, `received_at`, `age_s`, `expired`; plus `total`/`live`/`expired` over the whole matching set |
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

## 8. Python implementation map

**Language decision (2026-10-23): Python 3.11+ on rust-nostr's official bindings**, not a Rust
core. `nostr-sdk` 0.45.1 ships wheels for py>=3.9, so the Rust implementation's protocol coverage
(NIP-44, NIP-59, NIP-42, subscriptions, signing) is available behind a Python API, and Hermes
contributors need one toolchain rather than two. `pynostr` (0.7.0, one wheel, unmaintained) is
not a candidate.

The one deliberate exception to "official": the installed 0.45.1 binding is our fork build that
exposes `nip44_v2_conversation_key` (Concord derives stream keys from it). `requirements.txt` pins
it by URL and hash — see README, "The patched `nostr-sdk` wheel".

Consequence for the seed: the reference plugin is Python, so §18 is a **Python extraction, not a
port**. Anything in this table that reads Rust-is, read it as "the binding does this for us".

| Concern | Choice |
|---|---|
| Relays, signing, NIP-44/59, NIP-42, subscriptions | `nostr-sdk` 0.45.1 (official rust-nostr bindings, upstream `nip44_v2_conversation_key`) |
| Runtime | `asyncio`; `asyncio.start_unix_server` for the socket |
| Storage | `sqlite3` (stdlib), WAL, one writer task — no ORM |
| Socket codec | `json` stdlib, newline framing (NDJSON) |
| Blossom | `httpx` + `hashlib` |
| MIME sniffing | `puremagic` |
| CLI / logging | `argparse`; `structlog` (JSON lines) |
| Config / validation | `PyYAML` + `pydantic` |
| Tests | `pytest` + `pytest-asyncio`; probes as fixtures |
| Packaging | `pyproject.toml` (hatchling), `uv` for dev, one console script |

Modules mirror §18: `src/planes/{crypto,nip17,nip04,nip01,concord,marmot}.py`,
`src/relay/pool.py`, `src/store/`, `src/media/`, `src/api/socket.py`, `src/policy/`,
`src/adapter/` (the thin Hermes-facing part). One package, one process, no network listener —
wn-agent's operational shape.

If a real binding gap appears (not a hypothetical one — no spike first), the fallback is a thin
Rust helper crate for exactly those calls, not a rewrite of the daemon.

**Milestones:** M1 identity + NIP-17 text + policy/allowlist + socket (the family case);
M2 NIP-04; M3 media + reactions; M4 Concord; M5 Marmot proxy; M6 kind-1 feeds and mentions
(policy engine already in M1); M7 docs, container, CI, conformance suite.

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
2. **The gateway never interprets content.** No fetching beyond a Blossom blob a stored
   message references, addressed by its sha256 (lazy, capped, hash-verified; §4b), no
   command parsing, no policy decisions about replying. It is a transport.
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
| A | Key ownership | **Share the agent's key with wn-agent**, disjoint-kind table enforced at startup |
| B | Language | **Python on `nostr-sdk` bindings** (decided; §8) |
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
7. Publish through an auth-required relay (`relay.example.com/chat`) → NIP-42 then success.
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

---

## 14. Live-testing corrections (Oct 2026)

Measured against real relays while debugging client-to-bot DMs.

1. **Dedupe on the inner rumor, not the wrapper.** The same message arrives once per relay
   with a *different* wrapper id each time, so wrapper-level dedupe cannot work across relays.
   Key on `rumor.pubkey + rumor.created_at + rumor.content` (the rumor id is the natural key),
   and keep wrapper ids only to avoid reprocessing the same event twice.
2. **Inner kind filtering is mandatory.** Kind 1059 also carries Concord invites (inner kind
   `3313`, outer `k=3313`). Anything whose inner kind is not `14` (DM) or `7` (reaction) must be
   routed by kind — invites to the Concord plane — and must never surface as a message or
   trigger an agent run.
3. **NIP-04 is legacy-only.** Armada sends NIP-17 only; older clients still send kind 4. Do not
   dual-send both dialects by default: it doubles cost and duplicates in clients that read both.
4. **Authorisation drops need a voice.** A DM from a non-allowlisted key is correctly refused,
   but silently — the sender concludes the bot is broken. Log at INFO with the sender npub, and
   either send one short refusal or notify the owner.
5. **Replies:** seal from the real sender key, wrap to the recipient's `p`; never send from the
   ephemeral wrap key (it is not ours to reuse).

### Extra acceptance criteria (from the same note)

- A DM sent from Armada appears in our own evidence probe on **≥2 relays including one of
  ditto/dreamith/whitenoise**.
- The gateway logs an inbound message within ~10 s of send.
- The reply is visible in Armada.
- A DM from a non-allowlisted key produces a log line and **no agent run**.
- An invite (inner kind 3313) produces **no agent run**.
- A restart does not replay already-answered DMs (persist processed rumor ids).

---

## 15. Prior art and seed code

- **`wn-agent` / mdk** (`github.com/marmot-protocol/mdk`, `wn-agent-v0.10.4`) — the Marmot agent
  connector, its Unix control socket, and the Hermes integration under
  `integrations/hermes/marmot`. This repo's socket contract is modelled on it; the Marmot plane
  proxies it rather than reimplementing MLS.
- **Armada** (`soapbox.pub`) — reference client for NIP-17 DMs and Concord; its DM planes
  (`nip17`, `nip04`) and `imeta` handling informed §4b.
- **Existing platform adapters for Hermes** — a working `nostr` adapter (crypto, Concord write
  path, seen-ids state) is the intended **seed** for the daemon: lift those paths out rather
  than rewrite them. Any third-party code is used only with compatible licensing.

---

## 16. Planes (v2)

One socket, one normalised event, N planes behind it:

| plane | reads | writes | notes |
|---|---|---|---|
| `nip01` | kinds 1, 6, 16, 1111; NIP-50 search where the relay offers it | kind 1, 7, 1111 — **config-gated off by default** | feeds and mentions; attention policy §17 |
| `nip17` | 1059 → inner 14 / 7 / 15 | 14, 7, 15 inside 1059 | primary DM plane |
| `nip04` | kind 4 | kind 4 | legacy, allowlist-gated, never dual-sent by default |
| `concord` | 1059 with inner 3313 (invites) + Concord control/chat kinds | invites, chat, rekey | serverless E2EE communities; own control plane |
| `marmot` | via the wn-agent socket | via the wn-agent socket | **proxy**, not an implementation — MLS stays theirs; **one owner**, opt-in until the flip (see below) |

Normalised event on the wire:

```
{plane, conversation, author, content, media[], reply_to, why, authority, cursor}
```

Adding a plane must not require touching the socket schema: `plane` is a string and
`conversation` is opaque to the client (channel id, DM peer, group id).

`marmot` is the one plane that is **off unless asked for** (`marmot.enabled: true`). Hermes'
own Marmot platform plugin can subscribe to the same `wn-agent` socket; a gateway plane
attached alongside it would hand the agent every group message twice, so presence of the
socket is not consent. An idle plane logs why at startup and reports
`marmot: {enabled: false, reason: …}` on the `status` op.

The socket has **one owner** (`docs/marmot-ownership.md`): the gateway is the transport, and
the Hermes plugin is the carrier only until a clean week has been carried here. Both enabled
at once is a startup error rather than a log line — the guard reads
`platforms.marmot.enabled` out of Hermes' own config and refuses to build, because a
duplicate delivery is not something an operator can notice from a status line.

---

## 17. Attention policy (feeds and mentions)

Two **independent** dimensions per sender group. Keeping them separate is the whole point: a
public bot can *hear* strangers without letting them *instruct* it.

- **reach:** `immediate | digest(interval) | drop`
- **authority:** `instructions | none`

Built-in groups: `any` (everyone matched), `unknown` (not in any named group). Named groups are
lists of npubs in config. Rules are ordered, first match wins, and anything unmatched falls to
`defaults`.

```yaml
groups:
  family: [npub1cn67…, npub1jwh6…]
  team:   [npub1…]
  peer:   [npub1example…]
policies:
  - match: {group: family, kinds: [1], mention: true}    -> {reach: immediate, authority: instructions}
  - match: {group: any,    kinds: [1], mention: true}    -> {reach: digest(15m), authority: none}
  - match: {group: peer,   kinds: [1]}                   -> {reach: digest(60m), authority: instructions}
  - match: {group: unknown, kinds: [4, 1059]}            -> {reach: digest(60m), authority: none}
defaults: {reach: digest(15m), authority: none, quota: 60/h}
```

Rules:
- `mention: true` means a `p` tag naming the identity (and, for `nip01`, replies via `e`).
- **Digest** batches over the configured interval: one message carrying a count, the top N items
  (author, time, snippet) and the `why` for each, plus the count of items dropped by quota. A
  digest with nothing in it is not sent — silence is the "nothing happened" signal.
- **Quota** is per rule per hour; a global floor (default 60 deliveries/hour overall) stops a
  misconfiguration from spinning the agent.
- **Dedupe across rules** by event id, so one post matching two rules arrives once, attributed to
  the highest-priority rule.
- **Authority is not reach.** `authority: none` means the text reaches the agent as *data*; the
  agent must not act on instructions in it, and the adapter marks it as untrusted in the turn.
  This is what makes a public bot safe: strangers can talk, nobody can instruct.
- The policy engine also decides DM delivery (§10 item 1) — one mechanism, not a separate feed
  allowlist bolted on.

---

## 18. Seed from an existing adapter

The extraction target, in review order:

1. `nostr_crypto.py` (392 lines) → `src/planes/crypto.py` — NIP-44/NIP-59 primitives, key handling.
   Review for: key in argv/env, temporary files, error paths that log secrets.
2. `concord_write.py` (120 lines) → `src/planes/concord/write.py` — invite/control writes.
   Review for: replay/rekey edge cases, state handling.
3. `adapter.py` (19 KB) → **split**, not moved: the relay/subscription logic becomes
   `src/planes/nip17.py` + `src/relay/pool.py`; the Hermes-specific parts become the thin adapter
   (§11). Anything in the adapter that allowlists, retries or picks relays is a bug to delete, not
   port.
4. `~/.hermes/platforms/nostr/seen-ids.json` → seed data for `wraps_seen`, migrated once at first
   start, then owned by SQLite.

Authorship stays with the contributor; it lands on an extraction branch and
is reviewed against the checklist in §10 plus: no key material in logs, no network writes outside
configured relays, no `eval`/`exec`/shell, dependencies pinned.
