"""The host catalogue crosses from Hermes to the daemon as a file; pin its shape.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports. What
this pins is the other boundary: the file the daemon validates, written from Hermes' own
registry rather than from a list kept in this repository.
"""

from __future__ import annotations

import json
import sys
import types

from adapter_harness import load_adapter

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
