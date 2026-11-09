"""An invite joins on a trusted word, and waits for a human's otherwise.

Every invite is verified and recorded by the plane, which is all it ever did: joining is a
relay write, so it happens where the identity and the publish path live (the daemon, and
`hng concord`). These tests pin the rule the queue item asks for — an inviter in
`control.trusted_npubs` (global or the plane's own list) auto-accepts and joins the
Guestbook; anyone else's invite is held and the question is posted into the control pane —
and the promise underneath it: nothing joins silently, so every path writes down what it
did.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

import nostr_sdk as sdk
import yaml

from hermes_nostr_gateway import cli, crypto
from hermes_nostr_gateway.control import ControlConfig
from hermes_nostr_gateway.crypto import SdkCrypto
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord import write
from hermes_nostr_gateway.planes.concord.plane import ConcordPlane
from hermes_nostr_gateway.store import Store
from test_cli import make_config
from test_concord_reply import captured_publish
from test_publisher import LocalRelay

OPS = "11" * 32
GENERAL = "22" * 32
RELAYS = ["wss://relay.example", "wss://relay2.example"]
ASK_HERE = "33" * 32          # the control community's #ops channel


def make_invite(owner_pubkey: str, *, name: str = "Test Community",
                relays: list[str] | None = None) -> tuple[c.Invite, list[c.Channel]]:
    """A community with a private channel (granted key) and a public one (root-derived)."""
    salt, root = os.urandom(32), os.urandom(32)
    ops_key = os.urandom(32).hex()
    community_id = hashlib.sha256(
        b"concord/community" + bytes.fromhex(owner_pubkey) + salt).digest()
    invite = c.Invite(bundle={
        "owner": owner_pubkey, "owner_salt": salt.hex(), "community_id": community_id.hex(),
        "community_root": root.hex(), "root_epoch": 0, "control_pk": os.urandom(32).hex(),
        "name": name, "relays": list(relays or RELAYS),
        "channels": [{"id": OPS, "key": ops_key, "epoch": 0, "name": "ops"},
                     {"id": GENERAL, "epoch": 0, "name": "general"}],
    }, inviter=owner_pubkey, relay="", wrap_id=os.urandom(32).hex())
    return invite, [c.Channel(id=OPS, name="ops", private=True, key=ops_key, epoch=0),
                    c.Channel(id=GENERAL, name="general", private=False, epoch=0)]


def reinvite(invite: c.Invite) -> c.Invite:
    """The same community, wrapped again: how a new channel grant reaches us."""
    return c.Invite(bundle=dict(invite.bundle), inviter=invite.inviter, relay="",
                    wrap_id=os.urandom(32).hex())


def record(store: Store, invite: c.Invite) -> None:
    """What the plane does the moment a bundle verifies: record it, decide nothing."""
    store.record_invite(id=invite.wrap_id, inviter=invite.inviter,
                        community_id=invite.community_id.hex(),
                        name=str(invite.bundle.get("name") or ""),
                        relays=invite.relays, bundle=invite.bundle)


def gateway_for(tmp_path, identity: sdk.Keys, *, control: dict | None = None) -> Gateway:
    secret_file = tmp_path / "identity.json"
    secret_file.write_text(json.dumps({"secret_key_hex": identity.secret_key().to_hex()}))
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": False, "hermes_platform": False},
    }
    if control is not None:
        cfg["control"] = control
    return Gateway(cfg, socket_path=tmp_path / "gateway.sock")


def npub(keys: sdk.Keys) -> str:
    return keys.public_key().to_bech32()


def chat_for(invite: c.Invite, channel_id: str) -> str:
    return ch.chat_id(invite.community_id.hex(), channel_id)


def test_no_control_pane_means_every_invite_waits(tmp_path):
    """The default is "ask": config that names nobody trusts nobody."""
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    with Store(tmp_path / "verdict.db") as store:
        verdict = ConcordPlane(store).invite_verdict(invite)
        assert verdict.accept is False
        assert "human decides" in verdict.why


def test_a_trusted_inviter_joins_on_their_word(tmp_path):
    owner, stranger = sdk.Keys.generate(), sdk.Keys.generate()
    trusted, _ = make_invite(owner.public_key().to_hex())
    untrusted, _ = make_invite(stranger.public_key().to_hex())
    control = ControlConfig.from_config({"trusted_npubs": [npub(owner)]})
    with Store(tmp_path / "trust.db") as store:
        plane = ConcordPlane(store, control=control)
        assert plane.invite_verdict(trusted).accept is True
        assert "trusted_npubs" in plane.invite_verdict(trusted).why
        assert plane.invite_verdict(untrusted).accept is False
        assert "not in control.trusted_npubs" in plane.invite_verdict(untrusted).why


def test_a_planes_own_trusted_list_is_a_union(tmp_path):
    """Adding a key for concord must not remove the keys trusted everywhere."""
    global_owner, concord_owner = sdk.Keys.generate(), sdk.Keys.generate()
    control = ControlConfig.from_config({
        "trusted_npubs": [npub(global_owner)],
        "planes": {"concord": {"trusted_npubs": [npub(concord_owner)]}},
    })
    with Store(tmp_path / "union.db") as store:
        plane = ConcordPlane(store, control=control)
        for keys in (global_owner, concord_owner):
            invite, _ = make_invite(keys.public_key().to_hex())
            assert plane.invite_verdict(invite).accept is True


# ── the daemon ──────────────────────────────────────────────────────────────

async def test_a_trusted_invite_is_joined_and_the_join_says_so(tmp_path, monkeypatch):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        record(gateway.store, invite)
        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "joined" and frame["ok"] is True
        assert len(sent["events"]) == 1
        wrap = sent["events"][0]
        # The join lands on the Guestbook stream, authored by the derived stream key, and
        # a member opening it with the key they hold reads a join from us.
        gk = c.guestbook_key(invite)
        assert wrap["pubkey"] == gk.pk
        opened = c.open_wrap(gk, wrap)
        assert opened["content"] == "join"
        assert opened["pubkey"] == me.public_key().to_hex()
        assert sent["relays"] == RELAYS, "a join goes to the community's own relays"

        row = gateway.store.concord_invite(invite.wrap_id)
        assert row["status"] == "joined"
        assert row["decided_by"] == "auto"
        assert "trusted_npubs" in row["why"]
        assert gateway.store.stats()["invites_held"] == 0
    finally:
        gateway.store.close()


async def test_an_invite_on_the_wire_joins_through_the_daemons_own_sink(tmp_path, monkeypatch):
    """The seam this item names: the wrap reaches the DM plane, and a join goes out.

    The tests above drive `decide_invite` directly. This one hands the daemon a real
    kind-3313 rumor inside a 1059 wrap exactly as `Nip17Plane` sees it, so "an invite is
    recorded and nothing ever joins" is pinned at the place the invite actually arrives.
    """
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        alice, bob = SdkCrypto(owner.secret_key().to_hex()), gateway.crypto
        rumor = crypto.build_rumor(alice.our_pubkey(), bob.our_pubkey(),
                                   json.dumps(invite.bundle), kind=3313)
        wrap = alice.wrap(rumor, bob.our_pubkey())

        assert gateway.plane.handle("wss://relay.example", wrap) is None
        # Recorded before anything is decided: the row exists, and it is not a message.
        recorded = gateway.store.concord_invite(invite.community_id.hex())
        assert recorded is not None and recorded["status"] == "held"
        assert gateway.store.recent(10) == []

        # The decision runs as its own task so the relay worker never waits on a publish.
        assert gateway._invites, "the sink recorded the invite and scheduled no decision"
        await asyncio.gather(*list(gateway._invites))

        assert len(sent["events"]) == 1, "the invite arrived and no join went out"
        row = gateway.store.concord_invite(invite.community_id.hex())
        assert row is not None
        assert row["status"] == "joined" and row["decided_by"] == "auto"
    finally:
        gateway.store.close()


async def test_an_untrusted_invite_is_asked_about_in_the_control_channel(tmp_path, monkeypatch):
    owner, me, operator = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    control_invite, control_channels = make_invite(operator.public_key().to_hex(),
                                                   name="#ops")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(control_invite, OPS)})
    invite, _ = make_invite(owner.public_key().to_hex(), name="Some Community")
    try:
        gateway.concord.set_streams(control_invite, control_channels,
                                    {operator.public_key().to_hex()})
        record(gateway.store, invite)
        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "held" and frame["announced"] is True
        assert len(sent["events"]) == 1, "a held invite must not join anything"
        _, ops = gateway.concord.channel_for_conversation(chat_for(control_invite, OPS))
        opened = c.open_wrap(ch.stream_key(control_invite, ops, ops.key), sent["events"][0])
        # The question is readable in #ops, names the inviter, and says how to accept it.
        assert "Some Community" in opened["content"]
        assert npub(owner) in opened["content"]
        assert f"hng concord {invite.wrap_id}" in opened["content"]

        row = gateway.store.concord_invite(invite.wrap_id)
        assert row["status"] == "held"
        assert row["announced_at"], "the question reached the pane, so it is recorded"
        assert gateway.store.stats()["invites_held"] == 1
    finally:
        gateway.store.close()


async def test_a_held_invite_with_nowhere_to_ask_is_still_recorded(tmp_path, monkeypatch):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)          # no control config at all
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        record(gateway.store, invite)
        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "held" and frame["announced"] is False
        assert frame["error"] == "no control destination"
        assert sent["events"] == [], "a confirmation that cannot be delivered is not a join"
        row = gateway.store.concord_invite(invite.wrap_id)
        assert row["status"] == "held" and row["announced_at"] is None
    finally:
        gateway.store.close()


async def test_a_join_no_relay_accepted_is_not_recorded_as_joined(tmp_path, monkeypatch):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch, outcome="rejected: blocked")
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        record(gateway.store, invite)
        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "failed" and frame["ok"] is False
        assert sent["events"], "the join was attempted"
        row = gateway.store.concord_invite(invite.wrap_id)
        assert row["status"] == "failed"
        assert "no relay accepted" in row["why"]
    finally:
        gateway.store.close()


async def test_a_join_that_raises_is_a_state_and_not_a_crash(tmp_path, monkeypatch):
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    invite, _ = make_invite(owner.public_key().to_hex())

    async def explode(_keys, _invite):
        raise RuntimeError("relay list is empty")

    monkeypatch.setattr(write, "join_guestbook", explode)
    try:
        record(gateway.store, invite)
        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "failed"
        row = gateway.store.concord_invite(invite.wrap_id)
        assert row["status"] == "failed" and "relay list is empty" in row["why"]
    finally:
        gateway.store.close()


async def test_a_reinvite_for_a_joined_community_does_not_join_again(tmp_path, monkeypatch):
    """One owner action mints one invite per room: that is one join, not one per invite."""
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    first, _ = make_invite(owner.public_key().to_hex())
    second = reinvite(first)
    try:
        for invite in (first, second):
            record(gateway.store, invite)
        assert (await gateway.decide_invite(first))["status"] == "joined"
        frame = await gateway.decide_invite(second)

        assert len(sent["events"]) == 1, "the second invite published a second join"
        assert frame["status"] == "joined"
        assert frame["why"] == "already a member of this community"
        assert gateway.store.concord_invite(second.wrap_id)["status"] == "joined"
    finally:
        gateway.store.close()


async def test_a_reinvite_that_changes_the_root_is_held_not_joined(tmp_path, monkeypatch):
    """Key substitution: a member's re-invite must not replace a joined community's root."""
    owner, attacker, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    first, _ = make_invite(owner.public_key().to_hex())
    hostile = c.Invite(
        bundle=dict(first.bundle, community_root=os.urandom(32).hex()),
        inviter=attacker.public_key().to_hex(), relay="", wrap_id=os.urandom(32).hex())
    try:
        record(gateway.store, first)
        assert (await gateway.decide_invite(first))["status"] == "joined"
        record(gateway.store, hostile)

        frame = await gateway.decide_invite(hostile)

        assert frame["status"] == "held"
        assert "changes the community" in frame["why"]
        assert len(sent["events"]) == 1, "a held re-invite must not publish a second join"
        row = gateway.store.concord_invite(hostile.wrap_id)
        assert row is not None and row["status"] == "held"
    finally:
        gateway.store.close()


async def test_the_owner_may_rotate_a_joined_community_s_root(tmp_path, monkeypatch):
    """The owner's own re-invite is the one expected way the pinned fields change."""
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    first, _ = make_invite(owner.public_key().to_hex())
    rotated = c.Invite(
        bundle=dict(first.bundle, community_root=os.urandom(32).hex(),
                    root_epoch=1),
        inviter=owner.public_key().to_hex(), relay="", wrap_id=os.urandom(32).hex())
    try:
        record(gateway.store, first)
        assert (await gateway.decide_invite(first))["status"] == "joined"
        record(gateway.store, rotated)

        frame = await gateway.decide_invite(rotated)

        assert frame["status"] == "joined"
        assert len(sent["events"]) == 1, "the owner's rotation is not a second Guestbook join"
        row = gateway.store.concord_invite(rotated.wrap_id)
        assert row is not None and row["status"] == "joined"
    finally:
        gateway.store.close()


async def test_a_member_may_not_add_a_channel_grant_to_a_joined_community(tmp_path, monkeypatch):
    """A new room is additive only from the owner (or a trusted npub); a member's is held."""
    owner, member, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    first, _ = make_invite(owner.public_key().to_hex())
    injected = c.Invite(
        bundle=dict(first.bundle,
                    channels=[*first.bundle["channels"],
                              {"id": os.urandom(32).hex(), "name": "injected", "epoch": 0}]),
        inviter=member.public_key().to_hex(), relay="", wrap_id=os.urandom(32).hex())
    try:
        record(gateway.store, first)
        assert (await gateway.decide_invite(first))["status"] == "joined"
        record(gateway.store, injected)

        frame = await gateway.decide_invite(injected)

        assert frame["status"] == "held"
        assert "adds a channel grant" in frame["why"]
        assert len(sent["events"]) == 1
    finally:
        gateway.store.close()


async def test_a_held_community_builds_no_streams(tmp_path, monkeypatch):
    """A held invite is a record, not a subscription: it may not become a send target."""
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={})   # nobody trusted
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        record(gateway.store, invite)
        assert (await gateway.decide_invite(invite))["status"] == "held"

        watched = await gateway.refresh_concord()

        assert watched == 0
        assert gateway.concord.channels() == []
        assert gateway.concord.channel_for_conversation(
            f"concord:{invite.community_id.hex()}:{OPS}") is None
    finally:
        gateway.store.close()


async def test_a_reinvite_that_only_adds_a_channel_is_still_accepted(tmp_path, monkeypatch):
    """One owner action mints one invite per room; a later room is additive, not hostile."""
    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"trusted_npubs": [npub(owner)]})
    first, _ = make_invite(owner.public_key().to_hex())
    extra = os.urandom(32).hex()
    more = c.Invite(
        bundle=dict(first.bundle,
                    channels=[*first.bundle["channels"],
                              {"id": extra, "name": "later", "epoch": 0}]),
        inviter=owner.public_key().to_hex(), relay="", wrap_id=os.urandom(32).hex())
    try:
        record(gateway.store, first)
        assert (await gateway.decide_invite(first))["status"] == "joined"
        record(gateway.store, more)

        assert (await gateway.decide_invite(more))["status"] == "joined"
        assert len(sent["events"]) == 1
    finally:
        gateway.store.close()


async def test_the_same_wrap_from_two_relays_asks_only_once(tmp_path, monkeypatch):
    """One invite arrives from every relay that carries it: one question, not one each."""
    owner, other, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    control_invite, control_channels = make_invite(other.public_key().to_hex(), name="#ops")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(control_invite, OPS)})
    invite, _ = make_invite(owner.public_key().to_hex())
    try:
        gateway.concord.set_streams(control_invite, control_channels,
                                    {other.public_key().to_hex()})
        record(gateway.store, invite)
        await gateway.decide_invite(invite)
        frame = await gateway.decide_invite(invite)     # the same wrap, a second relay

        assert len(sent["events"]) == 1
        assert frame["status"] == "held" and frame["announced"] is True
    finally:
        gateway.store.close()


# ── the wire ────────────────────────────────────────────────────────────────

async def test_a_join_is_a_real_event_on_the_wire(monkeypatch):
    """Not a stub: the Guestbook wrap goes out as an EVENT and the relay says OK."""
    relay = await LocalRelay().start()
    try:
        # The local test relay is plain `ws://127.0.0.1`; production refuses exactly that
        # (a bundle must not point the daemon into its own network), so this test opts the
        # one URL in rather than weakening the check for every caller.
        monkeypatch.setattr(c, "relay_url", lambda url: str(url))
        owner, me = sdk.Keys.generate(), sdk.Keys.generate()
        invite, _ = make_invite(owner.public_key().to_hex(), relays=[relay.url])
        assert await write.join_guestbook(me, invite) == {relay.url: "ok"}
        gk = c.guestbook_key(invite)
        assert relay.events, "the join never reached the relay"
        assert relay.events[0]["pubkey"] == gk.pk
        assert c.open_wrap(gk, relay.events[0])["content"] == "join"
    finally:
        await relay.stop()


# ── the store ───────────────────────────────────────────────────────────────

def test_an_old_invite_row_migrates_to_held(tmp_path):
    """A row recorded before the join path existed has not been joined: `held` it is."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE concord_invites ("
        " id TEXT PRIMARY KEY, inviter TEXT NOT NULL, community_id TEXT NOT NULL,"
        " name TEXT, relays TEXT, bundle TEXT NOT NULL, received_at INTEGER NOT NULL);"
    )
    conn.execute("INSERT INTO concord_invites VALUES (?,?,?,?,?,?,?)",
                 ("ab" * 32, "cd" * 32, "ef" * 32, "Old Community", "[]", "{}", 1))
    conn.commit()
    conn.close()

    with Store(path) as store:
        row = store.concord_invite("ab" * 32)
        assert row["status"] == "held"
        assert row["decided_at"] is None and row["announced_at"] is None
        assert store.stats()["invites_held"] == 1
        # And a decision lands on the migrated row like any other.
        assert store.decide_invite("ab" * 32, status="joined", decided_by="cli") is True
        assert store.concord_joined("ef" * 32) is True


def test_an_invite_is_found_by_id_or_by_community(tmp_path):
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    with Store(tmp_path / "lookup.db") as store:
        record(store, invite)
        assert store.concord_invite(invite.wrap_id)["id"] == invite.wrap_id
        assert store.concord_invite(invite.community_id.hex())["id"] == invite.wrap_id
        assert store.concord_invite("ff" * 32) is None


# ── the CLI: a human's yes ──────────────────────────────────────────────────

def test_hng_concord_lists_what_is_held(tmp_path, capsys):
    path, _ = make_config(tmp_path)
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    with Store(tmp_path / "cli.db") as store:
        record(store, invite)
        store.decide_invite(invite.wrap_id, status="held",
                            why="inviter is not in control.trusted_npubs")

    assert cli.main(["--config", path, "concord"]) == 0
    out = capsys.readouterr().out
    assert "1 concord invite(s), 1 held" in out
    assert "Test Community" in out
    assert f"accept: hng concord {invite.wrap_id}" in out

    assert cli.main(["--config", path, "concord", "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)["invites"]
    assert [row["id"] for row in listed] == [invite.wrap_id]
    assert "bundle" not in listed[0], "a listing is not a place to print key material"


def test_hng_concord_accepts_a_held_invite(tmp_path, monkeypatch, capsys):
    path, secret = make_config(tmp_path)
    me = sdk.Keys.parse(secret)
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    sent = captured_publish(monkeypatch)
    with Store(tmp_path / "cli.db") as store:
        record(store, invite)

    assert cli.main(["--config", path, "concord", invite.wrap_id]) == 0

    assert len(sent["events"]) == 1
    gk = c.guestbook_key(invite)
    assert sent["events"][0]["pubkey"] == gk.pk
    assert c.open_wrap(gk, sent["events"][0])["pubkey"] == me.public_key().to_hex()
    with Store(tmp_path / "cli.db") as store:
        row = store.concord_invite(invite.wrap_id)
        assert row["status"] == "joined" and row["decided_by"] == "cli"
    assert "joined" in capsys.readouterr().out

    # Accepting twice is not a second join.
    assert cli.main(["--config", path, "concord", invite.wrap_id]) == 0
    assert len(sent["events"]) == 1
    assert "already joined" in capsys.readouterr().out


def test_hng_concord_refuses_an_id_it_never_recorded(tmp_path, capsys):
    path, _ = make_config(tmp_path)
    assert cli.main(["--config", path, "concord", "ff" * 32]) == 1
    assert "no invite" in capsys.readouterr().err


def test_hng_concord_reports_a_refused_join(tmp_path, monkeypatch, capsys):
    path, _ = make_config(tmp_path)
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    captured_publish(monkeypatch, outcome="rejected: blocked")
    with Store(tmp_path / "cli.db") as store:
        record(store, invite)

    assert cli.main(["--config", path, "concord", invite.wrap_id]) == 1
    assert "FAILED" in capsys.readouterr().out
    with Store(tmp_path / "cli.db") as store:
        assert store.concord_invite(invite.wrap_id)["status"] == "failed"


def test_hng_listen_records_an_invite_and_decides_nothing(tmp_path):
    """A one-shot listener is a reader: it records the invite, and joins nothing."""
    path, _ = make_config(tmp_path)
    owner = sdk.Keys.generate()
    invite, _ = make_invite(owner.public_key().to_hex())
    _crypto, store, _policy, plane = cli.build(cli.load_config(path))
    try:
        assert plane.on_invite is not None, "a listener that drops an invite loses it"
        rumor = {"kind": 3313, "pubkey": invite.inviter,
                 "content": json.dumps(invite.bundle)}
        assert plane.on_invite("wss://relay.example", rumor, invite.wrap_id) is not None
        row = store.concord_invite(invite.wrap_id)
        assert row["status"] == "held"
        assert row["announced_at"] is None and row["decided_at"] is None
    finally:
        store.close()


def test_the_config_example_documents_the_invite_rule():
    """The surface an operator reads has to say joins are confirmed, or they are not."""
    cfg = yaml.safe_load(open("config.example.yaml"))
    text = open("config.example.yaml").read()
    assert "control" in (cfg or {}) or "control:" in text
    assert "hng concord" in text
