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
- [x] **P1 — Read transport on nostr-sdk's relay pool** — replace per-relay `nak` children,
      in-process NIP-42, legacy path behind a flag. See plan P1.
      → PR nevent1qqszukd2xa0mz8zjv3g6va5y7cq8f2qcwsxlqtc844tyf4dd6mfwkzqur5urm (pr/read-transport-pool)
- [x] **P2 — Publish through the bindings** — one signed event, per-relay send, same-event
      retry, no subprocess. See plan P2.
      → PR nevent1qqstfl2fujt8t3ftz4yzq2wnqasmnsp8f9w05n33kknknpz5d6nmtxcpz3mhxue69uhhyetvv9ujumn8d96zuer9wcljzvaw (pr/publish-bindings)
- [x] **P3 — Drops first-class** — `drops` op, TTL, status count. See plan P3.
      → PR nevent1qqsx2yvad6xl03pd7wkqqckm72x7saf6dc5t9dsuzh7trx4nqz8dxpspz3mhxue69uhhyetvv9ujumn8d96zuer9wcwc5egd (pr/drops-first-class)
- [x] **P4 — Bot and echo policy** — kind-0 bot flag, reply cap, echo suppression. See plan P4.
      → PR nevent1qqsx0j9w954d6senywzyc37rkctq6xsgver40dn6rfk6ap9t4e4rk9spz3mhxue69uhhyetvv9ujumn8d96zuer9wc3nxmlv (pr/bot-echo-policy)
- [x] **P5 — Truthful status + `health` op** — live vs historical relays, fix the mangled
      error string, one-call health. See plan P5.
      → PR nevent1qqstypl33pqz702tt370hgqrymmwv84l96427qyuuqj2uyenxg4es5qpz3mhxue69uhhyetvv9ujumn8d96zuer9wc5c2ts7 (pr/truthful-status-health)
- [x] **P6 — Store off the loop** — WAL, batching, 500-event burst benchmark. See plan P6.
      → PR nevent1qqsxqafuxtyezhnjqn8hsa00gyc4zur5j6nmzdyh59vsv8w3a85arfqpz3mhxue69uhhyetvv9ujumn8d96zuer9wcup3vq3 (pr/store-off-loop)
- [x] **P7 — Concord: a supported plane, documented.** Corrected after review: it runs
      in production here and on Bitfest, so it is neither experimental nor gated. See
      `pr/concord-experimental`.
      → PR nevent1qqsvyraaescf2skzzypvtu2arh38847pfnfxh959ff4vv7730n4kqegpz3mhxue69uhhyetvv9ujumn8d96zuer9wc65yaqx (pr/concord-experimental)
- [x] **P8 — Security hardening** — SO_PEERCRED, LoadCredential, ops notes. See plan P8.
      → PR nevent1qqsyzdqn6hpfrg7unw3q2enm9a4wz49r3krnwtas5362nrq452lenzqpz3mhxue69uhhyetvv9ujumn8d96zuer9wccg7xdz (pr/security-hardening)
- [x] **P9 — Attachments reach the agent** — adapter calls `media.get`. See plan P9.
      → PR nevent1qqsy75h05s0y569nfd0k6ytfy67uwm4y0x8x537lmha03xz6euy6u5spz3mhxue69uhhyetvv9ujumn8d96zuer9wcpzqaq0ekg8ky97aku4xzhvlrx7s7e8fmw5wph2lnss32j0hdzu6t3ffq0e2h (pr/attachment-delivery)

## Decisions (do not re-litigate)

- **White Noise relays are Marmot-only.** `relay.eu/us.whitenoise.chat` serve the White
  Noise plane via wn-agent; they are not part of the gateway's NIP-17/feed read set and
  must not be re-added there.
