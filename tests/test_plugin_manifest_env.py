"""Catalog rule 6: the manifest declares the env vars the adapter actually reads.

A plugin's ``optional_env`` is the declared surface — what ``hermes config`` offers and what a
reviewer reads before admitting an entry. An undeclared read is capability creep, so this
fails on one: every name the adapter passes to ``env_setting`` must appear in plugin.yaml,
and no setting may be read straight from the process environment.
"""
import ast
from pathlib import Path

import yaml

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin"
ADAPTER = PLUGIN_DIR / "adapter.py"


def _manifest() -> dict:
    return yaml.safe_load((PLUGIN_DIR / "plugin.yaml").read_text(encoding="utf-8"))


def _declared(entries) -> dict[str, dict]:
    declared: dict[str, dict] = {}
    for entry in entries or []:
        if isinstance(entry, str):
            declared[entry] = {"name": entry}
        else:
            declared[str(entry.get("name") or "")] = dict(entry)
    declared.pop("", None)
    return declared


def _module_tree() -> ast.Module:
    return ast.parse(ADAPTER.read_text(encoding="utf-8"))


def _reader_names(tree: ast.Module, reader: str) -> set[str]:
    """Every literal name passed to *reader*(...) — ``env_setting("NOSTR_MEDIA")``."""
    names = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == reader and node.args):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            names.add(first.value)
    return names


def _direct_environ_names(tree: ast.Module) -> set[str]:
    """Every literal name read straight from ``os.environ`` / ``os.getenv``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            func, value = node.func, node.func.value
            environ_get = (func.attr == "get" and isinstance(value, ast.Attribute)
                           and value.attr == "environ" and isinstance(value.value, ast.Name)
                           and value.value.id == "os")
            getenv = (func.attr == "getenv" and isinstance(value, ast.Name)
                      and value.id == "os")
            if (environ_get or getenv) and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    names.add(first.value)
        elif (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
              and node.value.attr == "environ" and isinstance(node.value.value, ast.Name)
              and node.value.value.id == "os"):
            index = node.slice
            if isinstance(index, ast.Constant) and isinstance(index.value, str):
                names.add(index.value)
    return names


def test_every_env_var_the_adapter_reads_is_declared():
    tree = _module_tree()
    manifest = _manifest()
    declared = set(_declared(manifest.get("requires_env"))) | set(
        _declared(manifest.get("optional_env")))
    read = _reader_names(tree, "env_setting")
    assert read, "the adapter is expected to read its settings through env_setting()"
    undeclared = sorted(read - declared)
    assert undeclared == [], (
        f"rule 6: plugin.yaml does not declare {undeclared}, which the adapter reads")


def test_the_three_settings_the_adapter_gained_are_declared():
    """The finding, as a regression: these were read but not declared."""
    declared = _declared(_manifest().get("optional_env"))
    for name in ("NOSTR_CONTROL_CHANNEL", "NOSTR_GATEWAY_COMMANDS", "NOSTR_MEDIA"):
        assert name in declared, name


def test_no_named_setting_is_read_straight_from_the_process_environment():
    """Under multiplexing the process env is the *default* profile's; the host route is not."""
    direct = {name for name in _direct_environ_names(_module_tree()) if name.startswith("NOSTR_")}
    assert direct == set(), f"read through env_setting() instead: {sorted(direct)}"


def test_each_declaration_says_what_it_does_and_whether_it_is_a_secret():
    for name, entry in _declared(_manifest().get("optional_env")).items():
        assert entry.get("description"), f"{name} has no description"
        assert isinstance(entry.get("password"), bool), f"{name} has no password flag"
