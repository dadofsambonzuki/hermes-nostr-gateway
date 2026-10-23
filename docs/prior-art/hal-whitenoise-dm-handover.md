# Prior art — Hal's White Noise DM gateway handover

Source: Hal (`npub19etsn3snus0mgps7jhw6dn0cf57haxedeuqqk6n6duewuz2j02lq6pm7ry`), the BitFest bot,
`bitfest.uk:/home/ubuntu/notes/whitenoise-dm-gateway-handover.md`, 23 Oct 2026. Reproduced
verbatim below with attribution; probes referenced live beside it on the BitFest host
(`notes/probes/dm_evidence.py`, `dm_relays.py`).

---

---
title: White Noise / Armada DM gateway — handover
date: 2026-10-23
tags: [nostr, nip-17, whitenoise, armada, gateway, handover]
status: findings only, no changes made
---

# White Noise / Armada DM gateway — handover notes

Findings from debugging why DMs sent from **Armada** to the BitFest bot ("Hal",
`npub19etsn3snus0mgps7jhw6dn0cf57haxedeuqqk6n6duewuz2j02lq6pm7ry`) were not answered.
Everything below is verified against live relays, not inferred from the specs.

The existing adapter (Hermes `nostr` plugin) already speaks NIP-17 correctly. It failed on
**relay coverage** and **authorisation**, not on protocol. A White Noise–native gateway should
treat both as first-class requirements.

## TL;DR for the implementer

1. Armada DMs are **NIP-17**: `1059` giftwrap → `13` seal → `14` rumor.
2. The wrapper's **author is a throwaway key** and its `created_at` is randomised into the past —
   never filter wraps by author, and never use a short `since`.
3. **Delivery is not where the spec implies.** 2 of the 4 messages examined existed *only* on
   `relay.ditto.pub`, `relay.dreamith.to` and `relay.eu.whitenoise.chat` — none of which were in
   the adapter's relay list. Silent miss: no error, no log line.
4. **Authorisation is a list, not room membership.** A core-team member's DM *was* delivered and was
   dropped as unauthorised, also silently.
5. Non-DM traffic shares kind `1059`: Concord invites wrap a `3313` rumor (`k=3313`). Any DM handler
   must ignore inner kinds other than `14`.

## 1. Wire format (confirmed by unwrapping live events)

```
kind 1059  giftwrap   author = random ephemeral key   tags: [["p", <recipient>]]
   └─ content: NIP-44(recipient, seal)
        kind 13  seal     author = real sender   (signed by sender)
          └─ content: NIP-44(sender, rumor)
               kind 14  rumor    pubkey = sender, created_at = real time, unsigned
```

Observed properties:

- Outer `p` = recipient only. That is the only reliable way to query: `{"kinds":[1059],"#p":[me]}`.
- Outer `created_at` is backdated up to **2 days** (NIP-59). A `since` window shorter than that
  hides real messages.
- The same message arrives **once per relay** with a different wrapper id each time, so dedupe on
  the inner rumor (`pubkey + created_at + content`) or accept duplicates.
- Legacy NIP-04 (`kind 4`) still arrives from older clients. Armada itself sends NIP-17 only.

Non-message traffic on 1059 seen in the same inbox: **Concord invites** (inner kind `3313`, `k` tag
`3313`). Filtering only on "kind 1059 to me" will surface invites as DMs.

## 2. Relay delivery — the actual root cause

Adapter was connected to four relays: `relay.damus.io`, `nos.lol`, `relay.primal.net`,
`relay.bitfest.uk`. Observed presence for every inbound DM in the window:

| when (UTC) | from | content | found on | handled |
|---|---|---|---|---|
| 2026-10-22 08:26:50 | Nathan | "Hey 👋" | nos.lol, primal, ditto, dreamith | **answered** |
| 2026-10-22 11:45:39 | Nathan | "Hey, you respond to nip-17 messages here too?" | **ditto, dreamith, eu.whitenoise** | missed |
| 2026-10-22 11:57:23 | core team `npub18p9u0cw5c` | "Hey @Hal how's it going?" | nos.lol, primal, ditto, dreamith | **dropped by allowlist** |
| 2026-10-23 08:52:06 | Nathan | "We can chat here as well as white noise?" | **ditto, dreamith, eu.whitenoise** | missed |

Wrapper ids, for reproduction:

- c44dbc61e223abfe92eb12be96e9fd7064793c96f4e00ab3a62fa2309562c443 — 11:45:39
- 5c9dd5de61cc49b68c4e93b9f245a9ec20f451687b2a33b3ac27fde1e9980bb5 — 08:52:06
- 7f05d8a266f2a9ad… — the core-team DM (present on all four connected relays)

Two further observations from the same probe:

- `relay.damus.io` returned **503 for the whole period** (0 events) — a relay can be down without
  the client noticing.
- `relay.bitfest.uk` (our own relay) held **0 wrappers**. Being the operator's own relay is not
  evidence of delivery.

**Rule:** subscribe to the sender's app-relay set (Concord/White Noise: ditto, dreamith, eu/us
whitenoise) *in addition to* the `10050` inbox relays, and log per-relay event counts so a relay
that has gone quiet is visible rather than silent.

## 3. Authorisation is explicit, and currently silent

The adapter's `NOSTR_ALLOWED_USERS` held only the owner's pubkey. The core-team DM at 11:57:23 was
delivered normally and dropped:

```
2026-10-22 11:57:24,589 WARNING hermes_plugins.nostr.adapter:
Nostr: ignored DM from unauthorised pubkey npub18p9u0cw5c…
```

That is the right call — anyone on the list gets full tool access — but a silent drop reads as "the
bot is broken" to the sender. Recommend logging at INFO with the sender, and sending one short
refusal ("I only take instructions from the BitFest team") or pinging the owner.

## 4. Protocol pitfalls worth encoding as tests

1. **Backdated `since`** — kind 1059 must use a ≥2-day lookback; live subscription alone is
   best-effort. A watermark on the inner rumor time is more reliable than the wrapper's.
2. **Ephemeral author** — any filter on the outer `pubkey` is a bug.
3. **Duplicate wrappers** across relays — dedupe on the inner rumor.
4. **Mixed inner kinds** — invites (`3313`) and DMs (`14`) share the wrapper kind.
5. **Reply shape** — seal from the real sender key, wrap to the recipient's `p`; Armada reads
   NIP-17 and ignores kind 4. Sending both dialects doubles cost for no benefit here.

## 5. Probes (run these before trusting any gateway)

Copied alongside this note in `probes/`:

- `dm_evidence.py` — unwraps every inbound 1059 to a given key and prints
  `time | sender | wrapper id | relays holding it | first 60 chars`.
- `dm_relays.py` — marks which relays hold specific DMs, for checking coverage per relay.

Run with the venv that has the Nostr deps:

```
/home/ubuntu/.hermes/installs/5f0a1ff4045d044e/environments/d75d936358924d5ca50cc377070d9dfb/venv/bin/python3 \
  probes/dm_evidence.py
```

Pass criteria for a gateway:

- a DM sent from Armada appears in `dm_evidence.py` output on ≥2 relays including one of
  ditto/dreamith/whitenoise;
- the gateway logs an inbound message within ~10 s of send;
- the reply is visible in Armada;
- a DM from a non-allowlisted key produces a log line and no agent run;
- an invite (inner kind 3313) produces no agent run.

## 6. State of this host (unchanged)

- Identity: `npub19etsn3snus0mgps7jhw6dn0cf57haxedeuqqk6n6duewuz2j02lq6pm7ry`.
- Adapter: Hermes `nostr` plugin, `NOSTR_DM_PROTOCOL=both`, relays from `config.yaml`
  (damus, nos.lol, primal, relay.bitfest.uk), allowlist = owner only.
- Proposed relay/allowlist changes were **reverted**; no gateway restart was performed, so runtime
  behaviour is exactly as before this investigation.
- Two wrapper ids were added to `~/.hermes/platforms/nostr/seen-ids.json` so a restart does not
  replay the day-old DMs out of context.

## Related

- [[Armada]] · [[White Noise]] · [[Concord]] · [[Hal]]
