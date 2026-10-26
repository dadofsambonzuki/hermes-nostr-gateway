# Work queue

One item per run. Each item is a **scoped PR**, never a commit to master:

    git checkout -b pr/<slug> master && ... && git push origin HEAD:refs/heads/pr/<slug> \
      -o title="..." -o description="..."

The 20-minute worker takes the first unchecked item, implements it, runs the full suite,
pushes the branch, opens the PR, then ticks the box with the PR id. If it needs a decision,
mark it BLOCKED with the question and DM Nathan; move to the next item.

Plan of record: `docs/plan-v2.md` (also filed as an ngit issue).

- [x] **P0 — Marmot ownership, in writing and in code** — gateway owns the wn-agent socket;
      the plugin retires after a clean week. Enforce the double-delivery guard as a startup
      error. See plan P0.
      → PR nevent1qqs0848n23umnl6ryshelqnyhn9na7w9d58rwedgwz6hcgtuhtsrkggpz3mhxue69uhhyetvv9ujumn8d96zuer9wc78tts2 (pr/marmot-ownership)
- [ ] **P1 — Read transport on nostr-sdk's relay pool** — replace per-relay `nak` children,
      in-process NIP-42, legacy path behind a flag. See plan P1.
- [ ] **P2 — Publish through the bindings** — one signed event, per-relay send, same-event
      retry, no subprocess. See plan P2.
- [ ] **P3 — Drops first-class** — `drops` op, TTL, status count. See plan P3.
- [ ] **P4 — Bot and echo policy** — kind-0 bot flag, reply cap, echo suppression. See plan P4.
- [ ] **P5 — Truthful status + `health` op** — live vs historical relays, fix the mangled
      error string, one-call health. See plan P5.
- [ ] **P6 — Store off the loop** — WAL, batching, 500-event burst benchmark. See plan P6.
- [ ] **P7 — Concord: real vector or explicitly experimental.** See plan P7.
- [ ] **P8 — Security hardening** — SO_PEERCRED, LoadCredential, ops notes. See plan P8.
- [ ] **P9 — Attachments reach the agent** — adapter calls `media.get`. See plan P9.

## Decisions (do not re-litigate)

- **White Noise relays are Marmot-only.** `relay.eu/us.whitenoise.chat` serve the White
  Noise plane via wn-agent; they are not part of the gateway's NIP-17/feed read set and
  must not be re-added there.
