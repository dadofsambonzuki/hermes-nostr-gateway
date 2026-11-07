"""The host catalogue crosses from Hermes to the daemon as a file; pin its shape.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports. What
this pins is the other boundary: the file the daemon validates, written from Hermes' own
registry rather than from a list kept in this repository -- and, because that registry is a
private API that can be renamed under us, the record the plugin leaves when the write fails.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from adapter_harness import load_adapter
from hermes_nostr_gateway.advertise import (
    CatalogueError,
    build_manifest,
    load_catalogue,
)

adapter = load_adapter("nostr_adapter_catalogue")


class _Command:
    """The part of `hermes_cli.commands.CommandDef` the writer reads."""

    def __init__(self, name: str, description: str, args_hint: str = "") -> None:
        self.name = name
        self.description = description
        self.args_hint = args_hint

    def describe(self) -> str:
        return self.description


def stub_hermes(monkeypatch, core, plugins=()):
    """Install the two modules the writer imports, as Hermes would have them."""
    commands = types.ModuleType("hermes_cli.commands")
    commands._iter_plugin_command_entries = lambda: list(plugins)
    platforms = types.ModuleType("hermes_cli.commands_platforms")
    platforms._gateway_available_commands = lambda: list(core)
    parent = types.ModuleType("hermes_cli")
    parent.commands = commands
    parent.commands_platforms = platforms
    monkeypatch.setitem(sys.modules, "hermes_cli", parent)
    monkeypatch.setitem(sys.modules, "hermes_cli.commands", commands)
    monkeypatch.setitem(sys.modules, "hermes_cli.commands_platforms", platforms)


def test_the_file_is_the_host_registry_plus_its_plugins(tmp_path, monkeypatch):
    stub_hermes(
        monkeypatch,
        core=[_Command("help", "Show available commands", "[filter]"),
              _Command("new", "Start over")],
        plugins=[("joke", "Tell a joke", "[topic]"), ("help", "a duplicate", "")],
    )

    written = adapter.write_command_catalogue(tmp_path / "commands.json")

    payload = json.loads(written.read_text())
    assert payload["source"] == "hermes"
    assert [entry["name"] for entry in payload["commands"]] == ["help", "new", "joke"]
    assert payload["commands"][0]["args_hint"] == "[filter]"
    assert payload["commands"][2]["description"] == "Tell a joke"


def test_the_catalogue_sits_beside_the_socket(monkeypatch, tmp_path):
    monkeypatch.delenv("NOSTR_GATEWAY_COMMANDS", raising=False)
    monkeypatch.setenv("NOSTR_GATEWAY_SOCKET", str(tmp_path / "gateway.sock"))

    assert adapter.catalogue_path() == tmp_path / "commands.json"


def test_the_path_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("NOSTR_GATEWAY_COMMANDS", str(tmp_path / "elsewhere.json"))

    assert adapter.catalogue_path() == tmp_path / "elsewhere.json"


def test_a_write_is_atomic_and_leaves_no_staging_file(tmp_path, monkeypatch):
    stub_hermes(monkeypatch, core=[_Command("help", "Show available commands")])

    written = adapter.write_command_catalogue(tmp_path / "commands.json")

    assert [path.name for path in tmp_path.iterdir()] == [written.name]


# -- the whole chain: Hermes' registry -> commands.json -> the daemon's manifest ---------

def test_the_written_catalogue_becomes_the_manifest_the_client_reads(tmp_path, monkeypatch):
    """The only production source of the daemon's input, driven end to end."""
    stub_hermes(
        monkeypatch,
        core=[_Command("help", "Show available commands", "[filter]"),
              _Command("topic", "Set the topic of this session")],
        plugins=[("joke", "Tell a joke", "[subject]")],
    )

    written = adapter.write_command_catalogue(tmp_path / "commands.json")
    commands, warnings = load_catalogue(written)
    manifest = build_manifest(commands)

    # `topic` is a platform affordance this plane cannot render; the rest becomes the menu,
    # ranked so `/help` survives the cap rather than in registry order.
    assert [item.name for item in commands] == ["help", "joke"]
    assert any("topic" in warning for warning in warnings)
    assert [entry["name"] for entry in manifest.content["commands"]] == ["help", "joke"]
    assert manifest.content["commands"][0]["description"] == (
        "Show available commands (usage: /help [filter])")
    assert manifest.content["commands"][1]["description"] == "Tell a joke (usage: /joke [subject])"


# -- the failure path: recorded where `hng status` reads it -----------------------------

class _Ctx:
    """Hermes' plugin context: `register_platform` is all the adapter calls on it."""

    def __init__(self) -> None:
        self.registered: dict = {}

    def register_platform(self, **kwargs) -> None:
        self.registered.update(kwargs)


def test_a_source_that_raises_is_recorded_and_the_platform_still_loads(tmp_path, monkeypatch):
    """An upstream rename must be visible in `hng status`, not only in a log line."""
    target = tmp_path / "commands.json"
    monkeypatch.setenv("NOSTR_GATEWAY_COMMANDS", str(target))
    stub_hermes(monkeypatch, core=[_Command("help", "Show available commands")])

    def renamed_away():
        raise ImportError("cannot import name '_gateway_available_commands' from "
                          "'hermes_cli.commands_platforms'")

    sys.modules["hermes_cli.commands_platforms"]._gateway_available_commands = renamed_away
    ctx = _Ctx()

    adapter.register(ctx)

    assert ctx.registered["name"] == adapter.PLATFORM_NAME, "the platform failed to load"
    assert not target.exists(), "a partial catalogue was written"
    status = json.loads(adapter.catalogue_status_path(target).read_text())
    assert status["ok"] is False
    assert status["catalogue"] == str(target)
    assert "ImportError" in status["error"]
    assert "_gateway_available_commands" in status["error"]


def test_an_empty_source_is_recorded_and_keeps_the_last_good_file(tmp_path, monkeypatch):
    """Writing nothing over a working menu would be the silent death this guards against."""
    target = tmp_path / "commands.json"
    monkeypatch.setenv("NOSTR_GATEWAY_COMMANDS", str(target))
    stub_hermes(monkeypatch, core=[_Command("help", "Show available commands")])
    adapter.register(_Ctx())
    good = json.loads(target.read_text())
    assert [entry["name"] for entry in good["commands"]] == ["help"]
    assert json.loads(adapter.catalogue_status_path(target).read_text())["ok"] is True

    stub_hermes(monkeypatch, core=[], plugins=[])  # the registry now answers nothing
    adapter.register(_Ctx())

    assert json.loads(target.read_text()) == good, "the last good menu was replaced"
    status = json.loads(adapter.catalogue_status_path(target).read_text())
    assert status["ok"] is False
    assert "no gateway commands" in status["error"]


def test_a_missing_catalogue_names_the_plugins_failure(tmp_path):
    """The daemon's own error carries the reason the plugin recorded."""
    target = tmp_path / "commands.json"
    adapter.record_catalogue_status(
        target, ok=False, error="ImportError: cannot import name '_iter_plugin_command_entries'")

    with pytest.raises(CatalogueError) as caught:
        load_catalogue(target)

    assert "no command catalogue" in str(caught.value)
    assert "_iter_plugin_command_entries" in str(caught.value)
