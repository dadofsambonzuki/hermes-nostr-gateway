"""Concord channel ids are announced once on join, and can be listed on demand.

The failure this pins is silent: `concord.projects` keyed by a channel's *name* resolves an
inbound message, passes `--check`, and then loses every notice at send time — a notice is
addressed by the 64-hex channel id. So a successful join announces the ids it can now read,
a restart does not repeat the notice, a parked invite says nothing at all, and the CLI prints
the same list from the same Control-plane read, with the stored grants as the fallback when a
relay is silent.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import nostr_sdk as sdk
import yaml

from hermes_nostr_gateway import cli
from hermes_nostr_gateway.planes.concord import channels as ch
from hermes_nostr_gateway.planes.concord import read as c
from hermes_nostr_gateway.planes.concord.plane import channel_notice, joined_communities
from hermes_nostr_gateway.store import Store
from test_concord_invites import GENERAL, OPS, chat_for, gateway_for, make_invite, npub, record

EXTRA = "44" * 32


def joined(store: Store, invite: c.Invite) -> None:
    """What a completed join leaves behind: the row, decided, with no relay traffic."""
    record(store, invite)
    store.decide_invite(invite.wrap_id, status="joined", why="test", decided_by="test")


def serve_channels(monkeypatch, by_community: dict[str, list[c.Channel]]) -> None:
    """Answer the Control-plane and Guestbook reads from a dict, not a relay."""
    async def load_community(invite, **kwargs):
        community = invite.community_id.hex()
        return SimpleNamespace(name=invite.bundle.get("name") or "Community",
                               channels=by_community.get(community, []))

    async def read_guestbook(invite, **kwargs):
        return []

    monkeypatch.setattr(c, "load_community", load_community)
    monkeypatch.setattr(c, "read_guestbook", read_guestbook)


def notice_text(gateway, sent: dict, invite: c.Invite, channel_id: str = OPS) -> str:
    """Open the last published wrap as the channel it was sent to, and return its text."""
    _invite, stream = gateway.concord.channel_for_conversation(chat_for(invite, channel_id))
    opened = c.open_wrap(ch.stream_key(invite, stream, stream.key), sent["events"][-1])
    return opened["content"]


def counts(store: Store) -> dict[str, int]:
    rows = store.db.execute(
        "SELECT channel_id, COUNT(*) AS n FROM concord_announced GROUP BY channel_id"
    ).fetchall()
    return {row["channel_id"]: row["n"] for row in rows}


def cli_config(tmp_path) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({
        "store": str(tmp_path / "gateway.db"),
        "relays": {"read": [], "publish": []},
    }))
    return str(path)


# ── the join announcement ───────────────────────────────────────────────────

async def test_a_refresh_announces_each_channel_id_once(tmp_path, monkeypatch):
    """The connect-time read is also a restart's first read: one notice, then silence."""
    from test_concord_reply import captured_publish

    operator, owner, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    pane, pane_channels = make_invite(operator.public_key().to_hex(), name="Control Room")
    joined_invite, joined_channels = make_invite(owner.public_key().to_hex(), name="Work")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS)})
    try:
        gateway.concord.set_streams(pane, pane_channels, set())
        joined(gateway.store, joined_invite)
        serve_channels(monkeypatch, {joined_invite.community_id.hex(): joined_channels})

        assert await gateway.refresh_concord() == 2
        assert len(sent["events"]) == 1, "one notice per community, not one per channel"
        notice = notice_text(gateway, sent, pane)
        assert "Work" in notice
        assert joined_invite.community_id.hex() in notice
        assert f"channel_id: {OPS}  ops" in notice
        assert f"channel_id: {GENERAL}  general" in notice
        assert "loses every notice at send time" in notice
        # One row per channel id is the dedupe record a restart is checked against.
        assert counts(gateway.store) == {OPS: 1, GENERAL: 1}

        # A second refresh — what a restart's first pass is — says nothing.
        await gateway.refresh_concord()
        assert len(sent["events"]) == 1
        assert counts(gateway.store) == {OPS: 1, GENERAL: 1}
    finally:
        gateway.store.close()


async def test_a_restarted_gateway_does_not_announce_again(tmp_path, monkeypatch):
    from test_concord_reply import captured_publish

    operator, owner, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    pane, pane_channels = make_invite(operator.public_key().to_hex(), name="Control Room")
    joined_invite, joined_channels = make_invite(owner.public_key().to_hex(), name="Work")
    sent = captured_publish(monkeypatch)
    first = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS)})
    try:
        first.concord.set_streams(pane, pane_channels, set())
        joined(first.store, joined_invite)
        serve_channels(monkeypatch, {joined_invite.community_id.hex(): joined_channels})
        await first.refresh_concord()
        assert len(sent["events"]) == 1
    finally:
        first.store.close()

    second = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS)})
    try:
        second.concord.set_streams(pane, pane_channels, set())
        await second.refresh_concord()
        assert len(sent["events"]) == 1, "a restart repeated the notice"
    finally:
        second.store.close()


async def test_a_parked_invite_is_not_announced(tmp_path, monkeypatch):
    from test_concord_reply import captured_publish

    operator, owner, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    pane, pane_channels = make_invite(operator.public_key().to_hex(), name="Control Room")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS)})
    try:
        gateway.concord.set_streams(pane, pane_channels, set())
        record(gateway.store, make_invite(owner.public_key().to_hex(), name="Stranger")[0])

        async def no_read(*args, **kwargs):
            raise AssertionError("a parked invite was read or announced")

        monkeypatch.setattr(c, "load_community", no_read)
        monkeypatch.setattr(c, "read_guestbook", no_read)
        assert await gateway.refresh_concord() == 0
        assert sent["events"] == []
        assert counts(gateway.store) == {}
    finally:
        gateway.store.close()


async def test_a_trusted_join_announces_the_ids_without_waiting_for_a_refresh(tmp_path,
                                                                           monkeypatch):
    from test_concord_reply import captured_publish

    operator, owner, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    pane, pane_channels = make_invite(operator.public_key().to_hex(), name="Control Room")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS),
                                                 "trusted_npubs": [npub(owner)]})
    invite, channels = make_invite(owner.public_key().to_hex(), name="Work")
    try:
        gateway.concord.set_streams(pane, pane_channels, set())
        serve_channels(monkeypatch, {invite.community_id.hex(): channels})
        record(gateway.store, invite)

        frame = await gateway.decide_invite(invite)

        assert frame["status"] == "joined" and frame["ok"] is True
        assert len(sent["events"]) == 2, "the Guestbook join and the channel notice"
        notice = notice_text(gateway, sent, pane)
        assert f"channel_id: {OPS}  ops" in notice
        assert f"channel_id: {GENERAL}  general" in notice
        assert counts(gateway.store) == {OPS: 1, GENERAL: 1}
    finally:
        gateway.store.close()


async def test_a_reinvite_for_a_joined_community_does_not_announce_again(tmp_path,
                                                                        monkeypatch):
    """`already a member` is not a join: the ids were announced when the community joined."""
    from test_concord_invites import reinvite
    from test_concord_reply import captured_publish

    operator, owner, me = sdk.Keys.generate(), sdk.Keys.generate(), sdk.Keys.generate()
    pane, pane_channels = make_invite(operator.public_key().to_hex(), name="Control Room")
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me, control={"concord": chat_for(pane, OPS),
                                                 "trusted_npubs": [npub(owner)]})
    invite, channels = make_invite(owner.public_key().to_hex(), name="Work")
    try:
        gateway.concord.set_streams(pane, pane_channels, set())
        serve_channels(monkeypatch, {invite.community_id.hex(): channels})
        record(gateway.store, invite)
        assert (await gateway.decide_invite(invite))["status"] == "joined"
        assert len(sent["events"]) == 2, "the Guestbook join and the channel notice"

        later = reinvite(invite)
        record(gateway.store, later)
        frame = await gateway.decide_invite(later)

        assert frame["status"] == "joined"
        assert frame["why"] == "already a member of this community"
        assert len(sent["events"]) == 2, "a re-invite is not a second join or a second notice"
    finally:
        gateway.store.close()


async def test_no_pane_logs_the_ids_once_and_still_records_them(tmp_path, monkeypatch, caplog):
    from test_concord_reply import captured_publish

    owner, me = sdk.Keys.generate(), sdk.Keys.generate()
    sent = captured_publish(monkeypatch)
    gateway = gateway_for(tmp_path, me)          # no control pane at all
    invite, channels = make_invite(owner.public_key().to_hex(), name="Work")
    try:
        joined(gateway.store, invite)
        serve_channels(monkeypatch, {invite.community_id.hex(): channels})
        with caplog.at_level("INFO", logger="hng.gateway"):
            await gateway.refresh_concord()
            await gateway.refresh_concord()
        assert sent["events"] == []
        assert caplog.text.count(OPS) == 1 and caplog.text.count(GENERAL) == 1
        assert "no control pane" in caplog.text
        assert counts(gateway.store) == {OPS: 1, GENERAL: 1}
    finally:
        gateway.store.close()


def test_the_notice_names_the_key_the_pitfall_and_every_channel():
    text = channel_notice("Work", "ab" * 32, [(OPS, "ops"), (GENERAL, "general")])
    assert text.splitlines()[0] == "Concord: Work"
    assert f"community: {'ab' * 32}" in text
    assert f"channel_id: {OPS}  ops" in text
    assert f"channel_id: {GENERAL}  general" in text
    assert "loses every notice at send time" in text


# ── the listing ─────────────────────────────────────────────────────────────

def test_channels_json_lists_what_the_control_plane_names(tmp_path, monkeypatch, capsys):
    """The Control plane, not the invite: a joined row may grant only one of the rooms.

    This is the live case the verb exists for — the store's grants name the room the invite
    carried, while the Control plane names every room the community has.
    """
    owner = sdk.Keys.generate()
    invite, granted = make_invite(owner.public_key().to_hex(), name="Work")
    config = cli_config(tmp_path)
    with Store(tmp_path / "gateway.db") as store:
        joined(store, invite)
    # The invite granted only #ops; the Control plane also names #general and #later, with
    # names that differ from anything the grant could have carried.
    serve_channels(monkeypatch, {invite.community_id.hex(): [
        c.Channel(id=OPS, name="ops", private=True, key=granted[0].key, epoch=0),
        c.Channel(id=GENERAL, name="General Chat", private=False, epoch=0),
        c.Channel(id=EXTRA, name="later", private=False, epoch=0),
    ]})

    assert cli.main(["--config", config, "concord", "channels", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)

    assert [list(row) for row in rows] == [["id", "name", "community"]] * 3
    assert {(row["id"], row["name"]) for row in rows} == {
        (OPS, "ops"), (GENERAL, "General Chat"), (EXTRA, "later")}
    assert {row["community"] for row in rows} == {invite.community_id.hex()}
    # Stable order: the same listing twice is the same bytes.
    assert cli.main(["--config", config, "concord", "channels", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == rows


def test_channels_falls_back_to_the_stored_grants_and_says_so(tmp_path, monkeypatch, capsys):
    owner = sdk.Keys.generate()
    invite, _channels = make_invite(owner.public_key().to_hex(), name="Work")
    config = cli_config(tmp_path)
    with Store(tmp_path / "gateway.db") as store:
        joined(store, invite)

    async def unreachable(*args, **kwargs):
        raise OSError("every relay timed out")

    monkeypatch.setattr(c, "load_community", unreachable)
    assert cli.main(["--config", config, "concord", "channels", "--json"]) == 0
    captured = capsys.readouterr()
    rows = json.loads(captured.out)
    assert {row["id"] for row in rows} == {OPS, GENERAL}
    assert "listing the stored grants" in captured.err


def test_channels_prints_ids_and_names_and_filters_by_community(tmp_path, monkeypatch, capsys):
    owner = sdk.Keys.generate()
    invite, channels = make_invite(owner.public_key().to_hex(), name="Work")
    config = cli_config(tmp_path)
    with Store(tmp_path / "gateway.db") as store:
        joined(store, invite)
        # A parked invite contributes nothing: nothing joined it, so its rooms are not live.
        record(store, make_invite(sdk.Keys.generate().public_key().to_hex(),
                                  name="Parked")[0])
    serve_channels(monkeypatch, {invite.community_id.hex(): channels})

    assert cli.main(["--config", config, "concord", "channels"]) == 0
    out = capsys.readouterr().out
    assert "2 channel(s) in 1 community(ies)" in out
    assert f"Work  {invite.community_id.hex()}" in out
    assert f"  {OPS}  ops" in out and f"  {GENERAL}  general" in out
    assert "Parked" not in out

    community = invite.community_id.hex()
    assert cli.main(["--config", config, "concord", "channels",
                     "--community", community.upper()]) == 0
    assert f"{OPS}  ops" in capsys.readouterr().out

    assert cli.main(["--config", config, "concord", "channels",
                     "--community", "cd" * 32]) == 1
    assert "no joined community" in capsys.readouterr().err


def test_channels_with_no_store_is_an_error_not_an_invention(tmp_path, capsys):
    config = cli_config(tmp_path)
    assert cli.main(["--config", config, "concord", "channels"]) == 1
    assert "nothing has been joined yet" in capsys.readouterr().err


def test_joined_communities_skips_a_reinvite_that_changes_the_identity(tmp_path):
    """The same rule the daemon refreshes under: a hostile re-invite is not a grants source."""
    owner = sdk.Keys.generate()
    invite, _channels = make_invite(owner.public_key().to_hex(), name="Work")
    hostile = c.Invite(bundle=dict(invite.bundle, community_root="cd" * 32),
                       inviter="ee" * 32, relay="", wrap_id="a" * 64)
    with Store(tmp_path / "identity.db") as store:
        joined(store, hostile)
        # Pin the genuine invite explicitly: two joins in the same second would otherwise
        # leave `max(decided_at)` up to row order, and the test would be a coin toss.
        store.db.execute("UPDATE concord_invites SET decided_at = 1 WHERE id = ?",
                         (hostile.wrap_id,))
        joined(store, invite)
        (community,) = joined_communities(store.concord_invites(50))
        assert community.community_id == invite.community_id.hex()
        assert community.invite.bundle["community_root"] == invite.bundle["community_root"]
