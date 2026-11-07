# Dependency proposals (`hng deps`)

The scheduled pipeline that keeps the repositories' dependency manifests clear of known
vulnerabilities. It began as a standalone shell script with its own unit and its config in
the unit's environment; it now runs from the gateway's own CLI and reads the gateway's
`config.yaml`. The unit stays separate — that part was never the problem.

## Why it is not in the daemon

A run clones or fetches, scans with `osv-scanner`, rewrites a lockfile, then runs the
repository's own gates: `npm ci`, typecheck, lint, tests, build and a start-the-server smoke
test. That is minutes to an hour of npm, cargo and docker. In the daemon's poll loop it would
be a turn that holds the relay past every timeout; as a oneshot it can take as long as it
likes while the gateway keeps serving. `deploy/nostr-deps.service` is that oneshot,
`deploy/nostr-deps.timer` its daily schedule.

## Commands

```sh
hng deps run                 # every repository in deps.repositories
hng deps run --repo <name>   # one repository, for a re-run or a first look
hng deps run --dry-run       # say what would be proposed; push nothing, record nothing
hng deps dry-run             # the same command spelled as its own subcommand
```

A dry run still fetches, scans, applies the proposed bump and runs the gates, because that
is what decides whether a proposal is worth making — but it publishes nothing, triggers no
CI run, records no state, and puts the checkout back where it found it. It is the safe way
to see what the next scheduled run would do.

## Configuration

Everything site-specific lives in the `deps:` block of `config.yaml` (see
`config.example.yaml` for the shape and placeholders):

| Key | Meaning |
|---|---|
| `repositories` | `name`, `owner` (the npub or NIP-05 that published the repository) and `branch` a proposal is cut from |
| `min_severity` | the CVSS floor; `7` is high and above |
| `workspace` | where the bot's clones live (`/opt/deps-bot`); kept across runs, never inside a checkout |
| `state_dir` | last proposal's lockfile hash per repository, so a pending bump is not repeated; and the kept gate log of the last failure, `build-<name>.log` |
| `log` | the run log, appended by the oneshot and readable by `journalctl` too |
| `signer` | the Deps Bot npub that signs the proposal push and the scan trigger |
| `coordinator` | whose name a manual CI run is requested from |
| `workflow` | the CI workflow triggered for a fresh signed scan |
| `open_prs` | `false` scans, bumps and gates but publishes nothing |

A misspelled key is a config error, not a silent default: a unit that quietly stops
proposing is worse than one that fails.

## What a run does per repository

1. Clone into `workspace/<name>` if it is not there; otherwise fetch and reset `work/deps`
   to `origin/<branch>`.
2. Ask for this week's signed scan (`ngit ci trigger`) so a scan result exists even in a
   week with nothing to bump.
3. Count the findings at or above the CVSS floor with `osv-scanner scan`.
4. Let `osv-scanner fix --strategy in-place` propose the minimum upgrade. No diff, no
   proposal: the manifest is left untouched.
5. Run the repository's own gates against the bump. A bump that fails a gate is discarded,
   never proposed; the failing gate's output is kept at `state_dir/build-<name>.log`, so the
   `(see …)` path the journal prints still opens after the run has returned.
6. Compare the lockfile's hash with `state_dir/last-<name>`: the same proposal still waiting
   for review is not repeated.
7. Commit as the Deps Bot and push `pr/deps-<date>` with a title and a description naming
   what the bump clears. The `nostr://` remote publishes it as a pull request.

`git push` needs `nostr.signer` set per repository (the account store holds more than one
identity and the helper will not guess); the run sets it from `deps.signer` immediately
before the push.

## Installing the unit

The unit and timer are shipped in `deploy/`. `hng` is the gateway checkout's console script,
so the unit runs from that checkout and picks up whichever revision is deployed there:

```sh
cd /home/ubuntu/Projects/hermes-nostr-gateway
sudo install -m 0644 deploy/nostr-deps.service deploy/nostr-deps.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nostr-deps.timer

# prove it once through the unit, then read the journal
sudo systemctl start nostr-deps.service
journalctl -u nostr-deps.service -n 60 --no-pager
```

The old `/usr/local/bin/nostr-deps-weekly` script is retired to a timestamped `.bak`; the
workspace and its clones do not move.

## Testing

`tests/test_deps.py` covers the `deps:` block (the full shape, the defaults and each way it
can be wrong), the report helpers, and the pipeline end to end against a miniature
repository with stub tools: a dry run that writes nothing, the "same proposal" guard,
`open_prs: false`, and a failing gate that leaves its output at the path the message names.
`tests/test_deploy_units.py` pins the unit's shape: a oneshot that calls
`hng deps run`, never the daemon.
