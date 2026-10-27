# Work queue

One item per run. Each item is a **scoped PR**, never a commit to master:

    git fetch origin && git reset --hard origin/master
    git checkout -b pr/<slug>
    ... implement, test ...
    git push origin HEAD:refs/heads/pr/<slug> -o title="..." -o description="..."

The 20-minute worker takes the first unchecked item, implements it, runs the full suite,
pushes the branch, opens the PR, then ticks the box with the PR id. If it needs a decision,
mark it BLOCKED with the question and DM the operator; move to the next item.

## Nak removal (zero nak dependencies, one PR each)

The daemon and CLI no longer call nak (`fetch.py`, merged as `6b06df1`). These five close
the remaining dependency: the binary, the tests that spawn it, and the legacy transport.

- [x] **1. `tests/test_relay.py` without `RelayWorker`** — keep the refusal, read-plan,
      restricted-relay and store-recording tests; delete the nine that construct the
      subprocess worker (that behaviour now lives in `PoolWorker`, covered by
      `tests/test_pool.py`). Done when: the file imports no worker class and the suite is
      green with no skipped tests.
      → PR nevent1qqs03kwez4r0hn0cr9jaxy55vy7g3qr0me6u4cne892gpffhk7hq7sgpz3mhxue69uhhyetvv9ujumn8d96zuer9wc4sggu2 (pr/test-relay-no-worker)
- [x] **2. Delete the subprocess transport** — move `hng listen` to
      `fetch.fetch_many_blocking` (`--once`) and `fetch.stream_events` (live); delete
      `RelayWorker` and `OneShot`; reduce `relay.py` to `refused_kinds` and the handler
      types. Done when: no `subprocess` import in `relay.py` or `cli.py`, and `hng listen
      --once` works against a relay.
      → PR nevent1qqszuqe49e8ymdj4g85p0nemck8z85rjnn4jr5ldgtjg5tq7q747p5qpz3mhxue69uhhyetvv9ujumn8d96zuer9wcvh4fqj (pr/delete-subprocess-transport); also moved `NakInboxResolver` → `SdkInboxResolver`, the last `nak` child in `src/` outside the publish rollback
- [x] **3. Scripts off nak** — `scripts/probe_nip17_conformance.py` (four uses) and
      `scripts/verify_adapter.py` (key generate, publish) use the bindings instead. Done
      when: neither file mentions `nak`, and the probe still runs against a live relay.
      → PR nevent1qqs205vzfv60pxu3wypuaz4juvmrxu2j5hkp6yl346nwn4yvyqx5hfqpz3mhxue69uhhyetvv9ujumn8d96zuer9wc3dvrze (pr/scripts-off-nak)
- [x] **4. Image and CI** — drop the `nak` download from `Dockerfile`; remove any CI step
      that installs or asserts the binary; keep the wheel assertion. Done when: an image
      builds without network access to the nak release and CI is green.
      → PR nevent1qqsxprk29maf84r6yp5e85xmeywwnm4w8z93z3cqgcw4hk6spmrew3spz3mhxue69uhhyetvv9ujumn8d96zuer9wcdzm8f0 (pr/image-and-ci); image built without nak, CI never had one
- [x] **5. Delete `transport.legacy`** — remove the flag, its branches, and the `legacy`
      wording in comments and docs. Done when: `grep -rn "legacy" src config.example.yaml
      README.md` returns only NIP-04's "legacy DMs" wording, which is a protocol fact.
      → PR nevent1qqsycw3wz0f6r44sxtxk2mtkwdtmuzlvvp4vk0xv0qfffdqzyhus6yqpz3mhxue69uhhyetvv9ujumn8d96zuer9wcfsld3s (pr/delete-transport-legacy), cut from the pr/delete-subprocess-transport tip so it diffs as one commit; `NakPublisher` and the `transport:` block are gone, and a stale key is ignored rather than rejected

## Decisions (do not re-litigate)

- **White Noise relays are Marmot-only.** `relay.eu/us.whitenoise.chat` serve the White
  Noise plane via wn-agent; they are not part of the gateway's NIP-17/feed read set.
- **No names in the repo.** No operator, peer bot or host names in docs, config examples,
  fixtures or comments; placeholders only.
- **Concord is supported, not experimental.** It runs in production; do not gate it.
