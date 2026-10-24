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
- [ ] **Pin the patched wheel** — requirements entry + README section naming the source
      commit and the upstream PR, so a reinstall cannot silently revert it.
- [ ] **Retire `nip17_wrapper.py`** — the CLI covers send/listen now; keep only the
      conformance probes under `scripts/`.
- [ ] **Ops notes in `~/Projects/infra/AGENTS.md`** — service, socket, haven's WoT gate
      and connection limiter, the nak machine-key trap, `TimeoutStopSec`.
- [ ] **Acceptance tests from spec §13** — crash-between-insert-and-ack replays;
      rate-limited peer degrades to blocked.
- [ ] **Packaging** — container image, CI (pytest + ruff), README "run it in five minutes".

Rules for the cron: one item per run; never amend pushed history; never touch a running
service's config without recording why in the commit; if tests fail and the fix is not
obvious in the same run, mark BLOCKED with the failure in the note.
