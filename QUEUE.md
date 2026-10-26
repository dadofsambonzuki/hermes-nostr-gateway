# Work queue

The 20-minute cron takes the first unchecked item, does it, runs the tests, commits and
pushes, then ticks the box with a one-line note. If an item needs a decision it is marked
BLOCKED and a DM is sent; the cron moves to the next item and leaves that one alone.

- [x] **Concord plane** — restore `derive`/`read`/`write` from Hal's PR, replace the
      hand-rolled ECDH with `sdk.nip44_conversation_key`, tests against a known vector,
      route kind-1059/inner-3313 invites to it. Done when: invite parses, a message is
      read and one written, tests green.
      — landed 222bdbc: binding-derived stream keys (vector-tested), channel/guestbook
      read+write, invites verified into `concord_invites` and never delivered; 65 tests green.
- [x] **Marmot proxy plane** — speak wn-agent's control socket as a plane (`marmot`),
      add a `marmot` worker/resolver, expose it over the same socket frames. Done when:
      Hermes reads a Marmot group through the gateway, wn-agent unchanged.
      — landed 35ad31f: control-socket client (protocol + id correlation), worker with
      reconnect/counters, resolver (32-hex group vs 64-hex pubkey, `group_info`-verified),
      `marmot.group`/`marmot.timeline` reads; live read of the home group + established
      subscription on a scratch socket, wn-agent unrestarted, 91 tests green. Inbound push
      proven against a protocol-faithful fake, not yet by a live peer message. Not enabled
      in the live config: attaching it beside the marmot plugin double-delivers.
- [x] **Hal upgrade note** — DM Hal the exact steps: pull master, install the plugin,
      set `NOSTR_GATEWAY_SOCKET`, install the patched `nostr-sdk` wheel, close his PR
      `a3631dbe`. Done when: sent, with the wheel provenance included.
      — sent (giftwrap `1965d0936a67`, read back from nos.lol + primal): five steps, wheel
      provenance (fork `eddb08d`, upstream PR #96) and a sha256-checked download at
      `relay.day.ag/2a7287…`; also records the Marmot-plane double-delivery trap.
- [x] **Pin the patched wheel** — requirements entry + README section naming the source
      commit and the upstream PR, so a reinstall cannot silently revert it.
      — landed c51ac1c: `requirements.txt` pins the `wheel-0.45.1-nip44` release asset by URL +
      sha256 (wrong hash refused), pyproject points at the same wheel for `pip install -e .`,
      README names fork `eddb08d` / upstream PR #96; installed on clean uv and pip venvs, 91 tests green.
- [x] **Rebase the digest PR onto master** — branch `pr/digest-cursor-schema` (`7cc77b5`,
      base `272f9cb`) is four-plus commits behind. Rebase onto current master, then delete
      master's `pending_digest()` / `_digest_loop()` in favour of `seal_digests()` so there
      is exactly one batching path. Keep the socket frame shape additive: `type: "message"`,
      `plane: "digest"`, plus `frame`, `rule`, `cursor_start`, `cursor_end`, `count`,
      `delivered`, `dropped`, `truncated`, `items`, and **rendered `content`** built from the
      joined rumors at delivery/replay time (`digests` keeps `top_ids` only, no plaintext
      copy). `ack(ids)` must treat an id matching `digests.id` as a whole-batch ack: mark
      every `batched` rumor delivered, close the row when none remain, in the same
      transaction as individual id marks, so a second ack is a no-op. Done when: a legacy
      adapter that knows only `type: message` receives the digest, acks the frame id, and a
      reconnect replays nothing; with the ack withheld the identical frame replays; suite green.
      — landed f60d795 (branch `pr/digest-cursor-schema`, 5 commits on 43b8452): one path via
      `seal_digests`, message-shaped digest frames with rendered `content`, `ack(ids)` closes a
      whole batch by `digests.id`; 106 tests green, including the legacy-adapter socket replay;
      live-DB copy migrated 713 rows with contiguous cursors. Pushed; PR to 3/4 relays
      (grasp.day.ag 404).
- [x] **Retire `nip17_wrapper.py`** — the CLI covers send/listen now; keep only the
      conformance probes under `scripts/`.
      — landed f9f3029: wrapper, both spikes and `config.json` deleted; `hng listen` now
      `#p`-scoped and NIP-42-signed as the identity (was a firehose on nak's machine key),
      `hng send` publishes through `publisher.publish` with the secret and records the wrap
      for retry; the six §4/§7 traps live on as `scripts/probe_nip17_conformance.py`
      (`--live` §13.1 round trip run on nos.lol), 101 tests green.
- [x] **Ops notes** — write them to `~/Projects/infra/ops/hermes-nostr-gateway.md`
      (Nathan's call, 2026-10-24: a separate file rather than `infra/AGENTS.md`, which
      needs his approval per write). Source copy already committed at
      `docs/ops-notes.md` (`00dc164`). Also leave a one-line pointer comment in the
      queue for the eventual AGENTS.md entry. Done when: the file exists with the
      notes and the `docs/ops-notes.md` copy is referenced from it.
      — landed 572241a: `~/Projects/infra/ops/hermes-nostr-gateway.md` exists, body identical
      to `docs/ops-notes.md` (which now names the mirror as its ops copy and stays the source),
      README points at it; unit, 15 s drop-in, 0600 socket and WAL db re-checked on the host
      before writing. Pointer line below. 123 tests green.
      <!-- AGENTS.md pointer: hermes-nostr-gateway — ops notes at ops/hermes-nostr-gateway.md
           (socket/XDG trap, systemd sandbox, relay.day.ag WoT + rate limits + per-relay kinds). -->

- [x] **A relay that refuses kind 4 loses kind 1059 too** — `gateway.py` builds
      `kinds = (1059, 4) if restricted else (1059, 4)`, so the restricted branch is a no-op.
      `relay.eu.whitenoise.chat` answers the combined REQ with
      `CLOSED: ERROR: bad req: filter validation failed: kind not allowed: 4`, which drops its
      DM subscription entirely (seen in `hng status`, 2026-10-24). Give each relay a kind set it
      accepts — at minimum drop kind 4 where it is refused while keeping 1059 — and add a test
      for a relay that rejects kind 4 but accepts 1059. Found while writing the ops notes.
      — landed c2dfbf8: `relays.read` entries may be `{url, kinds}` (whitenoise pinned to
      `[1059]`), and a worker refused a kind drops it and resubscribes at once, stopping with
      an error rather than ever building a filterless REQ; `hng status` records the negotiation.
      Live: 126 wraps after dropping kind 4. 123 tests green. The daemon still runs the old
      code (editable install) until someone bounces the unit — unattended runs may not.
- [x] **Acceptance tests from spec §13** — crash-between-insert-and-ack replays;
      rate-limited peer degrades to blocked.
      — landed 0ffbb33: a real-Gateway socket test acks one of two frames, drops the
      connection before the second ack, and gets exactly the unacked frame back once,
      then nothing after it is acked; the per-sender bucket (10 burst, 30/h, all planes)
      stores over-limit messages but emits `blocked`/`rate_limited` with no content, and
      policy drops use the same frame (`not_allowlisted`). 129 tests green; live-DB copy
      migrated 1,693 rows. The policy/store half was swept into 81bae9e by a concurrent
      commit; 0ffbb33 is the frame mapping, CLI/plugin treatment, docs and tests.
- [x] **Packaging** — container image, CI (pytest + ruff), README "run it in five minutes".
      — landed 6689a56: `Dockerfile` (uid-1000 runtime, state volume for socket+store, `nak`
      v0.20.6 pinned by sha256, wheel assertion at build), `.ngit/act/workflows/test.yml`
      (ruff + pytest as separate gates, requirements-first install, `nip44_conversation_key`
      assertion) run green by the coordinator after adding `…/hermes-nostr-gateway` to
      `NGIT_CI_REPOS` and publishing a maintainer service request; README quick start,
      container recipe and CI section; ruff's E4/E7/E9+F/I/B gate fixed seven pre-existing
      findings (wrong annotation name, unused imports, a loop lambda capture); 129 tests green.

Rules for the cron: one item per run; never amend pushed history; never touch a running
service's config without recording why in the commit; if tests fail and the fix is not
obvious in the same run, mark BLOCKED with the failure in the note. To DM Nathan, write the
text to a file and send it with `hermes send --to marmot --file <path>` (`hermes send` takes
a positional body, not `--text`).
- [x] **Media (M3)** — the one milestone with no code: a `media.get` socket op that fetches
      a Blossom blob referenced by a message, with size/type caps and a per-blob cache, so
      the adapter can hand the agent an image instead of a URL. Done when: an inbound DM
      with an attachment yields bytes the adapter can read, and an oversized or unknown
      blob is refused with a named reason.
      — landed 6813263: `media.get` (and `hng media`) resolves a blob a stored message
      references, then downloads lazily, verifies sha256, sniffs MIME, caps at 25 MiB/4 per
      message and caches 0600 under `~/.local/state/…/media` (512 MiB, oldest evicted);
      named refusals (`too_large`, `unknown_blob`, `hash_mismatch`, `type_unknown`,
      `insecure_url`, …), attachments ride the message frame + replay, `[media]` extras gone;
      255 tests green. The daemon picks it up on its next restart.
- [x] **Retry TTL** — `retry_failed()` retries a failed relay publish forever; the 3,370-row
      loop backlog only stopped because it was expired by hand. Bound it by age (24h) so a
      future backlog cannot churn for days. Done when: a row older than the TTL is marked
      `expired` on the next pass, with a test.
      — landed 9fc21f5: `outbound_wraps.created_at` (stamped at first offer, so a retry cannot
      renew its own lease), `expire_wraps()` at the head of every pass marks rows older than
      `retry.ttl_hours` (24 default) `expired`, and `pending_wraps()` now selects only
      `pending`/`failed` — a hand-marked `expired` row was still retried because the old filter
      was `status != 'sent'`. Migration dates pre-TTL rows from `last_attempt`; `hng status`
      reports wrap counts by status; 259 tests green, ruff clean, live-DB copy expired 225 rows
      in one pass (4,406 expired, 44 retryable). The daemon picks it up on its next restart.
- [x] **Marmot conversations after reconnect** — the plane reports `conversations: []` until
      the first event arrives, where it reported this chat before the restart. Enumerate on
      connect (or say why it cannot) so `status` is not ambiguous about what is attached.
      — landed c0ac7fe: `Store.conversations(plane)` makes stored messages the evidence;
      `MarmotResolver.enumerate()` runs on every subscribe ack (stored groups + the configured
      subscription group), `status` entries carry `verified`/`messages`/`last_at` and an
      `enumeration` block names any group `group_info` refused. wn-agent 0.10.4 has no
      group-list request (an unknown `group_list` frame is unanswered), so a group with no
      stored message stays undiscoverable until its first message and `source` says so.
      Live wn-agent on a copy of the live store confirmed the family group (13 messages,
      2 members, `is_direct`) before any new event; 266 tests green, ruff clean.
- [ ] **Queue worker idle behaviour** — when there is no unchecked item, the 20-minute run
      should stop quietly instead of attempting tools and logging blocked-tool warnings.
      Done when: an empty queue produces one line, no tool attempts.


## Decisions (do not re-litigate)

- **White Noise relays are Marmot-only.** `relay.eu/us.whitenoise.chat` serve the White
  Noise plane via wn-agent; they are not part of the gateway's NIP-17/feed read set and
  must not be re-added there. If the Marmot plane needs them, it reads them through
  wn-agent, not through a DM worker.
