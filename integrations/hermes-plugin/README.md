# Nostr for Hermes

Nostr as a Hermes platform. Everything Nostr — NIP-17 and NIP-04 DMs, kind-1 mentions and feeds,
Concord channels, Marmot groups — arrives as one message stream, and one policy decides what is
worth waking an agent for.

This directory is the **adapter only**. The daemon behind it,
[`hermes-nostr-gateway`](https://github.com/dadofsambonzuki/hermes-nostr-gateway), holds the Nostr
keys, talks to the relays and makes that policy; it is not installed by this pin. Without it the
plugin loads, registers the platform, and has nothing to talk to.

| | |
|---|---|
| **Platform name** | `nostr` |
| **Transport** | the daemon's Unix socket, one local connection |
| **Hermes** | 0.22.1 or newer |
| **OS** | Linux or macOS — an AF_UNIX socket with a `SO_PEERCRED` peer check, so not Windows |

## Install

From the catalog, pinned to the reviewed commit:

```sh
hermes plugins install nostr
```

Or from a checkout of the daemon, which keeps the hand-copied adapter in step with the build:

```sh
hng adapter install        # back up the live copy, write this build's adapter.py, verify it
hermes gateway restart     # the adapter is imported at startup
```

The adapter is a copy on purpose: `adapter.py` in this directory is canonical and Hermes loads a
copy at `$HERMES_HOME/plugins/nostr/adapter.py`. `hng adapter status` prints both SHA-256 hashes
and says whether the gateway started before the live copy was written.

Then point the plugin at the daemon and name a home:

```sh
hermes config set plugins.nostr.socket_path ~/.local/state/hermes-nostr-gateway/gateway.sock
hermes config set plugins.nostr.home_channel <peer-hex>
```

`NOSTR_GATEWAY_SOCKET` and `NOSTR_HOME_CHANNEL` do the same, and win where both are set.

## Settings

| Setting | Meaning |
|---|---|
| `socket_path` | The daemon's Unix socket. Unset, `$XDG_RUNTIME_DIR/hermes-nostr-gateway.sock`, or `~/.local/state/hermes-nostr-gateway/gateway.sock` |
| `home_channel` | The peer a message with no conversation goes to, and where cron delivery lands |
| `dedupe_window_s` | Seconds inside which an identical message to the same peer is a replay and is dropped (default 60). `0` disables the windowed rule and leaves only the recovered-reply one |
| `channel_context` | Room history that rides with a channel turn as background, never as a trigger: on by default at 20 messages / 8 000 characters / 2 hours. `false` pays nothing; a mapping changes `max_messages`, `max_chars`, `max_age_secs`. DMs and Marmot groups never carry it |

## Environment

All optional. A value set here beats the setting of the same name.

| Variable | Meaning |
|---|---|
| `NOSTR_GATEWAY_SOCKET` | the daemon's Unix socket |
| `NOSTR_GATEWAY_COMMANDS` | where the command catalogue is written (default: `commands.json` beside the socket) |
| `NOSTR_HOME_CHANNEL` | home and cron delivery target |
| `NOSTR_CONTROL_CHANNEL` | comma-separated npubs that may receive control-plane notices — naming one is the only exception to dropping control text |
| `NOSTR_ALLOWED_USERS` | comma-separated pubkeys allowed to message Hermes (advisory: the daemon's policy is authoritative) |
| `NOSTR_ALLOW_ALL_USERS` | accept any sender; the daemon still applies its own policy |
| `NOSTR_MEDIA` | `0`/`false`/`no`/`off` stops delivery fetching attachments; a refusal is named in the message text |
| `NOSTR_DEDUPE_WINDOW_S` | the dedupe window, in seconds |

## What it does

- **One stream, one policy.** The adapter does not score a message; reach and authority are the
  daemon's verdict, and a frame that arrives is already meant for an agent.
- **A room turn is not answered blind.** When a channel frame is a turn, recent room history rides
  along as context under the `channel_context` caps. DMs get none.
- **Replays are dropped, not re-answered.** Hermes' delivery ledger re-sends a reply it could not
  prove landed; the adapter recognises an identical send inside the window and settles it without
  a second turn.
- **Approvals work on a surface with no buttons.** A prompt names the typed answer
  (`/approve once|session|always`, `/deny`) and the reactions that stand for them, listing only the
  choices the host actually offers.
- **Sending from outside the process.** The plugin registers a standalone sender that dials the
  same socket, so `hermes send --to nostr:<pubkey|npub|concord:<community>:<channel>|marmot:<group>>`
  and a cron job with `deliver=nostr` reach the daemon from any shell. It carries no second
  allowlist and no identity of its own.
- **The slash menu a client can see.** On load the adapter writes the Hermes command list to
  `commands.json` beside the daemon socket, and a configured npub home or control channel can
  render it in a client.

## Disclosure

- **It holds no keys and speaks to no relay itself.** Its only network-shaped act is the Unix
  socket: the daemon's identity, relays and trusted senders are all on the other side of it, and
  are described in the daemon's own README.
- **It writes one file**: `commands.json`, beside the daemon socket (default
  `~/.local/state/hermes-nostr-gateway/`, or `NOSTR_GATEWAY_COMMANDS`). No other path is written.
- **It runs one command**: `nak decode` when a destination is given as an `npub` rather than hex.
  Without `nak` on `PATH`, give destinations as hex.
- **No background processes** of its own: the adapter runs inside the Hermes gateway, and the
  daemon is a service the operator runs and owns.
- **Trust is not decided here.** `NOSTR_ALLOWED_USERS` and `NOSTR_ALLOW_ALL_USERS` are handed to
  Hermes' own admission check; a sender the daemon distrusts never becomes a message.

## Links

- Daemon, docs and the shipped agent skill: <https://github.com/dadofsambonzuki/hermes-nostr-gateway>
- Why it is built this way: [`docs/decisions.md`](https://github.com/dadofsambonzuki/hermes-nostr-gateway/blob/master/docs/decisions.md)
- The catalog entry: [hermes-agent.nousresearch.com/marketplace/nostr](https://hermes-agent.nousresearch.com/marketplace/nostr)
