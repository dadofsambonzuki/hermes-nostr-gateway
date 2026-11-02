"""The advertisement: a manifest the client will accept, and the publish path around it.

Armada fails closed on a kind-10304 manifest -- one unaddressable name, one over-long
description, one 65th command and nothing renders. So the builder is tested against those
limits entry by entry, and the publisher is tested through a fake relay with the read-back
that distinguishes "accepted" from "served".

Placeholders only: no real identity, relay or host belongs in this file.
"""

from __future__ import annotations

import json
from pathlib import Path

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.advertise import (
    MANIFEST_KIND,
    MAX_COMMANDS,
    MAX_DESCRIPTION,
    PROFILE_KIND,
    AdvertiseError,
    AdvertisementConfig,
    Advertiser,
    CatalogueError,
    build_manifest,
    build_profile,
    content_hash,
    load_catalogue,
    resolve_relays,
    select_menu,
)
from hermes_nostr_gateway.crypto import SdkCrypto
from hermes_nostr_gateway.store import Store

RELAYS = ["wss://relay.example", "wss://relay.example/two"]


def command(name: str, description: str = "does a thing", **extra) -> dict:
    return {"name": name, "description": description, **extra}


def manifest_of(*entries: dict):
    return build_manifest(entries)


# -- the client's limits -----------------------------------------------------

def test_a_name_the_client_cannot_address_is_dropped_not_emitted():
    manifest = manifest_of(command("help"), command("Bad Name"), command("Ünicode"),
                           command("ok-name"))

    assert manifest.names == ["help", "ok-name"]
    assert any("bad name" in warning for warning in manifest.warnings)
    assert any("ünicode" in warning for warning in manifest.warnings)


def test_a_leading_slash_and_upper_case_are_normalised():
    assert manifest_of(command("/Help")).names == ["help"]


def test_a_long_description_is_clamped_to_the_byte_limit():
    manifest = manifest_of(command("chat", "word " * 100))

    description = manifest.content["commands"][0]["description"]
    assert len(description.encode("utf-8")) <= MAX_DESCRIPTION
    assert description.endswith("...")
    assert any("clamped" in warning for warning in manifest.warnings)


def test_a_multibyte_description_is_clamped_on_a_codepoint_boundary():
    description = "é" * 150  # 300 bytes

    manifest = manifest_of(command("chat", description))

    kept = manifest.content["commands"][0]["description"]
    assert len(kept.encode("utf-8")) <= MAX_DESCRIPTION
    kept.encode("utf-8").decode("utf-8")  # would raise on a split codepoint


def test_more_than_sixty_four_commands_are_capped_in_order():
    manifest = manifest_of(*(command(f"cmd-{index}") for index in range(MAX_COMMANDS + 6)))

    assert len(manifest.content["commands"]) == MAX_COMMANDS
    assert manifest.names[0] == "cmd-0"
    assert any("6 command(s)" in warning for warning in manifest.warnings)


def test_duplicate_names_keep_the_first():
    manifest = manifest_of(command("help", "first"), command("help", "second"))

    assert manifest.names == ["help"]
    assert manifest.content["commands"][0]["description"] == "first"
    assert any("duplicate" in warning for warning in manifest.warnings)


def test_an_argument_list_the_client_refuses_drops_only_that_command():
    too_many = [{"name": f"a{index}", "type": "string"} for index in range(9)]
    manifest = manifest_of(
        command("fine"),
        command("many", args=too_many),
        command("bad-type", args=[{"name": "x", "type": "money"}]),
        command("bad-order", args=[{"name": "x", "required": False},
                                   {"name": "y", "required": True}]),
    )

    assert manifest.names == ["fine"]
    assert len(manifest.warnings) == 3


def test_a_choice_argument_is_bounded():
    good = {"name": "mode", "type": "choice", "required": True,
            "choices": ["one", "two"]}
    manifest = manifest_of(command("pick", args=[good]))

    assert manifest.content["commands"][0]["args"][0]["choices"] == ["one", "two"]

    empty = manifest_of(command("pick", args=[{"name": "mode", "type": "choice"}]))
    assert empty.names == []
    long_choice = manifest_of(command("pick", args=[
        {"name": "mode", "type": "choice", "choices": ["x" * 33]}]))
    assert long_choice.names == []


def test_the_manifest_is_the_shape_the_client_declares():
    content = manifest_of(command("help", "what it says"), command("new", "start over",
                                                                    args=[
                                                                        {"name": "name",
                                                                         "type": "string",
                                                                         "required": False}]
                                                                    )).content

    assert content["v"] == 1
    assert [item["name"] for item in content["commands"]] == ["help", "new"]
    for item in content["commands"]:
        assert set(item) == {"name", "description", "args"}
        assert isinstance(item["args"], list)
    assert len(json.dumps(content).encode()) <= 32768


# -- the hash is the decision ------------------------------------------------

def test_the_hash_does_not_depend_on_key_order():
    assert content_hash({"a": 1, "b": [1, 2]}) == content_hash({"b": [1, 2], "a": 1})
    assert content_hash({"a": 1}) != content_hash({"a": 2})


# -- which commands this plane advertises ------------------------------------

def test_platform_affordances_are_not_advertised_and_priority_names_come_first():
    entries = [
        command("topic"), command("voice"), command("save"),
        command("agents"), command("help"), command("model"),
    ]

    commands, warnings = select_menu(entries)

    assert [item.name for item in commands] == ["help", "model", "agents"]
    assert len(warnings) == 3
    assert any("no topics" in warning for warning in warnings)


def test_an_argument_hint_reaches_the_description():
    commands, _warnings = select_menu(
        [{"name": "new", "description": "Start over", "args_hint": "[name]"}])

    assert commands[0].description == "Start over (usage: /new [name])"


# -- the catalogue file ------------------------------------------------------

def test_the_catalogue_reads_both_shapes(tmp_path):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([command("help")]))
    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(json.dumps({"commands": [command("help"), command("topic")]}))

    assert [item.name for item in load_catalogue(bare)[0]] == ["help"]
    assert [item.name for item in load_catalogue(wrapped)[0]] == ["help"]


def test_a_missing_catalogue_names_the_path(tmp_path):
    with pytest.raises(CatalogueError) as caught:
        load_catalogue(tmp_path / "absent.json")

    assert "absent.json" in str(caught.value)


# -- the publish path --------------------------------------------------------

class FakePublisher:
    def __init__(self, fail: dict[str, str] | None = None):
        self.events: list[dict] = []
        self.calls = 0
        self.fail = fail or {}

    async def publish(self, event: dict, relays):
        self.calls += 1
        self.events.append(event)
        return {relay: (relay not in self.fail, self.fail.get(relay)) for relay in relays}

    async def aclose(self) -> None:
        pass


class FakeReader:
    def __init__(self, publisher: FakePublisher, absent: set[str] | None = None,
                 clock=None):
        self.publisher = publisher
        self.absent = absent or set()

    async def __call__(self, relay: str, kind: int) -> dict | None:
        if relay in self.absent:
            return None
        for event in reversed(self.publisher.events):
            if int(event["kind"]) == kind:
                return event
        return None


@pytest.fixture()
def store(tmp_path):
    store = Store(tmp_path / "gateway.db")
    try:
        yield store
    finally:
        store.close()


@pytest.fixture()
def crypto():
    return SdkCrypto(sdk.Keys.generate().secret_key().to_hex())


def contents() -> dict[int, dict]:
    return {PROFILE_KIND: build_profile("example", {"about": "a placeholder"}),
            MANIFEST_KIND: build_manifest([command("help", "say what exists")]).content}


async def test_publish_signs_both_kinds_and_records_what_each_relay_holds(crypto, store):
    publisher = FakePublisher()
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))

    reports = await advertiser.publish(contents(), RELAYS)

    assert [report.kind for report in reports] == [PROFILE_KIND, MANIFEST_KIND]
    assert all(report.status == "published" for report in reports)
    assert all(report.stored for report in reports)
    assert all(set(report.readback.values()) == {"retained"} for report in reports)
    assert [event["kind"] for event in publisher.events] == [PROFILE_KIND, MANIFEST_KIND]
    assert int(publisher.events[0]["kind"]) == 0
    assert json.loads(publisher.events[0]["content"])["bot"] is True
    stored = store.advertised(MANIFEST_KIND)
    assert stored["event_id"] == reports[1].event_id
    assert stored["content_hash"] == content_hash(contents()[MANIFEST_KIND])


async def test_an_unchanged_copy_is_not_published_again(crypto, store):
    publisher = FakePublisher()
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))
    await advertiser.publish(contents(), RELAYS)
    calls = publisher.calls

    reports = await advertiser.publish(contents(), RELAYS)

    assert publisher.calls == calls
    assert [report.status for report in reports] == ["unchanged", "unchanged"]


async def test_changed_content_is_republished(crypto, store):
    publisher = FakePublisher()
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))
    await advertiser.publish(contents(), RELAYS)

    changed = contents()
    changed[MANIFEST_KIND] = build_manifest([command("help", "different wording")]).content
    reports = await advertiser.publish(changed, RELAYS)

    assert publisher.calls == 3
    assert [report.status for report in reports] == ["unchanged", "published"]


async def test_a_partial_publish_is_not_stored_so_the_next_start_retries(crypto, store):
    publisher = FakePublisher(fail={RELAYS[1]: "timeout"})
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))

    reports = await advertiser.publish(contents(), RELAYS)

    assert all(report.status == "partial" for report in reports)
    assert all(report.transient for report in reports)
    assert store.advertised(PROFILE_KIND) is None
    reports = await advertiser.publish(contents(), RELAYS)
    assert [report.status for report in reports] == ["partial", "partial"]


async def test_a_relay_that_refuses_the_kind_is_recorded_not_retried(crypto, store):
    reason = "blocked: only chat related events are allowed"
    publisher = FakePublisher(fail={RELAYS[0]: reason})
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))
    only_manifest = {MANIFEST_KIND: contents()[MANIFEST_KIND]}

    reports = await advertiser.publish(only_manifest, RELAYS)

    assert reports[0].status == "partial"
    assert reports[0].refused == {RELAYS[0]: reason}
    assert reports[0].transient == {}
    assert reports[0].stored is True
    stored = store.advertised(MANIFEST_KIND)
    assert stored["refused"] == {RELAYS[0]: reason}
    # The next start sees the same content hash and does not publish into the refusal.
    reports = await advertiser.publish(only_manifest, RELAYS)
    assert reports[0].status == "unchanged"


async def test_a_receipt_that_is_not_served_is_reported(crypto, store):
    publisher = FakePublisher()
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher, absent={RELAYS[0]}))

    reports = await advertiser.publish(contents(), RELAYS)

    assert reports[0].readback == {RELAYS[0]: "absent", RELAYS[1]: "retained"}
    # Accepted everywhere, so the stored copy is still recorded: a relay that does not
    # serve is an operator problem, and republishing the same bytes would not change it.
    assert reports[0].stored is True


async def test_force_publishes_even_when_the_hash_matches(crypto, store):
    publisher = FakePublisher()
    advertiser = Advertiser(crypto=crypto, publisher=publisher, store=store,
                            reader=FakeReader(publisher))
    await advertiser.publish(contents(), RELAYS)

    reports = await advertiser.publish(contents(), RELAYS, force=True)

    assert [report.status for report in reports] == ["published", "published"]


async def test_no_relays_is_a_failure_not_a_silent_success(crypto, store):
    advertiser = Advertiser(crypto=crypto, publisher=FakePublisher(), store=store,
                            reader=FakeReader(FakePublisher()))

    reports = await advertiser.publish(contents(), [])

    assert all(report.status == "failed" for report in reports)
    assert reports[0].warnings == ["no relays to publish to"]


# -- configuration -----------------------------------------------------------

def test_publish_defaults_to_off():
    config = AdvertisementConfig.from_config(None)

    assert config.enabled is False
    assert config.from_config({"publish": False}).enabled is False


def test_publish_true_advertises_both_and_a_list_narrows_it():
    both = AdvertisementConfig.from_config({"publish": True, "name": "example"})
    commands = AdvertisementConfig.from_config({"publish": ["commands"]})

    assert both.targets == (PROFILE_KIND, MANIFEST_KIND)
    assert both.profile == {"name": "example", "bot": True}
    assert commands.targets == (MANIFEST_KIND,)
    assert commands.profile is None


def test_an_unknown_target_is_a_config_error():
    with pytest.raises(AdvertiseError):
        AdvertisementConfig.from_config({"publish": ["evrything"]})


def test_the_profile_target_requires_a_name():
    with pytest.raises(AdvertiseError):
        AdvertisementConfig.from_config({"publish": True})


def test_the_catalogue_defaults_beside_the_socket(tmp_path):
    config = AdvertisementConfig.from_config(
        {"publish": ["commands"]}, socket_path=tmp_path / "gateway.sock")

    assert config.catalogue_file == tmp_path / "commands.json"


def test_profile_fields_cannot_unset_the_bot_flag():
    assert build_profile("example", {"bot": False})["bot"] is True


async def test_relays_are_publish_plus_config_plus_write_relays(monkeypatch):
    seen = {}

    async def fake_fetch(**kwargs):
        seen.update(kwargs)
        return {"kind": 10002,
                "tags": [["r", "wss://write.example"], ["r", "wss://read.example", "read"],
                         ["r", "wss://both.example", "write"]]}

    monkeypatch.setattr("hermes_nostr_gateway.fetch.fetch_first", fake_fetch)

    relays = await resolve_relays(["wss://relay.example"], ["wss://extra.example"],
                                  me="ab" * 32, secret="11" * 32)

    assert relays == ["wss://relay.example", "wss://extra.example", "wss://write.example",
                      "wss://both.example"]
    assert seen["author"] == "ab" * 32


# -- the CLI ----------------------------------------------------------------

def write_config(tmp_path, advertise: dict) -> tuple[str, dict]:
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    config = {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": ["wss://relay.example"]},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "advertise": advertise,
    }
    path = tmp_path / "config.yaml"
    path.write_text(json.dumps(config))
    return str(path), config


def test_cli_show_counts_the_catalogue_and_says_what_is_published(tmp_path, capsys):
    from hermes_nostr_gateway import cli

    path, config = write_config(tmp_path, {"publish": ["commands"]})
    Path(config["socket"]).with_name("commands.json").write_text(json.dumps(
        {"commands": [command("help"), command("topic")]}))

    assert cli.main(["--config", path, "profile", "show"]) == 0

    out = capsys.readouterr().out
    assert "1 command(s) in the menu" in out
    assert "nothing yet" in out


def test_cli_publish_reports_the_event_id_and_the_read_back(tmp_path, monkeypatch, capsys):
    from hermes_nostr_gateway import cli

    path, config = write_config(tmp_path, {"publish": ["commands"]})
    Path(config["socket"]).with_name("commands.json").write_text(json.dumps(
        {"commands": [command("help", "say what exists")]}))
    Store(Path(config["store"])).close()  # the CLI refuses to invent a store

    publisher = FakePublisher()

    async def fake_resolve(publish, extra=(), *, me=None, secret=None):
        return ["wss://relay.example"]

    async def fake_fetch(**kwargs):
        for event in reversed(publisher.events):
            if int(event["kind"]) in kwargs["kinds"]:
                return event
        return None

    monkeypatch.setattr("hermes_nostr_gateway.publisher.make_publisher",
                        lambda cfg, secret: publisher)
    monkeypatch.setattr("hermes_nostr_gateway.advertise.resolve_relays", fake_resolve)
    monkeypatch.setattr("hermes_nostr_gateway.fetch.fetch_first", fake_fetch)

    assert cli.main(["--config", path, "profile", "publish"]) == 0

    out = capsys.readouterr().out
    assert f"kind {MANIFEST_KIND}" in out
    assert "retained" in out
    assert publisher.events[0]["id"] in out


def test_cli_publish_refuses_when_advertising_is_off(tmp_path, capsys):
    from hermes_nostr_gateway import cli

    path, _config = write_config(tmp_path, {"publish": False})

    assert cli.main(["--config", path, "profile", "publish"]) == 2
    assert "advertise.publish" in capsys.readouterr().err


# -- the daemon --------------------------------------------------------------

def gateway_config(tmp_path, advertise: dict) -> dict:
    secret = sdk.Keys.generate().secret_key().to_hex()
    (tmp_path / "secret.json").write_text(json.dumps({"secret_key_hex": secret}))
    return {
        "identity": {"secret_file": str(tmp_path / "secret.json")},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {},
        "policies": [],
        "defaults": {"reach": "immediate", "authority": "instructions"},
        "advertise": advertise,
    }


def test_the_daemon_builds_both_contents_from_the_host_catalogue(tmp_path):
    from hermes_nostr_gateway.gateway import Gateway

    config = gateway_config(tmp_path, {"publish": True, "name": "example"})
    Path(config["socket"]).with_name("commands.json").write_text(json.dumps(
        {"commands": [command("help"), command("topic")]}))

    gateway = Gateway(config)
    try:
        contents, warnings = gateway.advertisement_contents()
        status = gateway.advertisement_status()
    finally:
        gateway.store.close()

    assert set(contents) == {PROFILE_KIND, MANIFEST_KIND}
    assert contents[PROFILE_KIND] == {"name": "example", "bot": True}
    assert [item["name"] for item in contents[MANIFEST_KIND]["commands"]] == ["help"]
    assert any("topic" in warning for warning in warnings)
    assert status["enabled"] is True and status["published"] == []


def test_a_bad_publish_target_refuses_to_build_the_daemon(tmp_path):
    from hermes_nostr_gateway.gateway import Gateway

    with pytest.raises(AdvertiseError):
        Gateway(gateway_config(tmp_path, {"publish": ["everything"]}))
