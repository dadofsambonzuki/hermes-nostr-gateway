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

- [ ] **1. `tests/test_relay.py` without `RelayWorker`** — keep the refusal, read-plan,
      restricted-relay and store-recording tests; delete the nine that construct the
      subprocess worker (that behaviour now lives in `PoolWorker`, covered by
      `tests/test_pool.py`). Done when: the file imports no worker class and the suite is
      green with no skipped tests.
- [ ] **2. Delete the subprocess transport** — move `hng listen` to
      `fetch.fetch_many_blocking` (`--once`) and `fetch.stream_events` (live); delete
      `RelayWorker` and `OneShot`; reduce `relay.py` to `refused_kinds` and the handler
      types. Done when: no `subprocess` import in `relay.py` or `cli.py`, and `hng listen
      --once` works against a relay.
- [ ] **3. Scripts off nak** — `scripts/probe_nip17_conformance.py` (four uses) and
      `scripts/verify_adapter.py` (key generate, publish) use the bindings instead. Done
      when: neither file mentions `nak`, and the probe still runs against a live relay.
- [ ] **4. Image and CI** — drop the `nak` download from `Dockerfile`; remove any CI step
      that installs or asserts the binary; keep the wheel assertion. Done when: an image
      builds without network access to the nak release and CI is green.
- [ ] **5. Delete `transport.legacy`** — remove the flag, its branches, and the `legacy`
      wording in comments and docs. Done when: `grep -rn "legacy" src config.example.yaml
      README.md` returns only NIP-04's "legacy DMs" wording, which is a protocol fact.

## Decisions (do not re-litigate)

- **White Noise relays are Marmot-only.** `relay.eu/us.whitenoise.chat` serve the White
  Noise plane via wn-agent; they are not part of the gateway's NIP-17/feed read set.
- **No names in the repo.** No operator, peer bot or host names in docs, config examples,
  fixtures or comments; placeholders only.
- **Concord is supported, not experimental.** It runs in production; do not gate it.
