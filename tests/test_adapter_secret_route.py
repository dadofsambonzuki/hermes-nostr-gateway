"""A route's key is checked through Hermes' secret route, never by reading a file.

Catalog rule 11: a plugin's credentials come through Hermes
(``agent.secret_scope.get_secret``), and a plugin never opens a ``.env`` file. The gateway
names the variable and the file it checked when it resolved the rule; all this adapter may
ask is whether the host can resolve that variable right now — and a key it cannot resolve
refuses the turn rather than letting it run on some other credential.
"""
from pathlib import Path

from adapter_harness import load_adapter

ADAPTER = (Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "adapter.py")

adapter = load_adapter("nostr_adapter_secret_route")

MISSING = "$ROUTE_KEY is not set, and the turn will not borrow another key"


def _routed_frame(env_file: str = "") -> dict:
    route = {"provider": "p3", "model": "model-b", "key": "ROUTE_KEY", "rule": "channel",
             "why": "channel rule"}
    if env_file:
        route["env_file"] = env_file
    return {"type": "message", "id": "a" * 64, "sender": "b" * 64, "conversation": "b" * 64,
            "content": "hello", "route": route}


def test_the_host_route_is_what_decides_a_key_is_present(monkeypatch):
    """Presence is the host's answer, asked by name — not a value this process looked up."""
    asked = []

    def host_holds(name, default=None):
        asked.append(name)
        return "value" if name == "ROUTE_KEY" else default

    monkeypatch.setattr(adapter, "_get_scoped_secret", host_holds)
    route, refusal = adapter.route_for_turn(_routed_frame())
    assert refusal == ""
    assert route["key"] == "ROUTE_KEY"
    assert asked == ["ROUTE_KEY"]


def test_a_key_the_host_cannot_resolve_refuses_the_turn(monkeypatch):
    monkeypatch.setattr(adapter, "_get_scoped_secret", lambda name, default=None: default)
    assert adapter.route_for_turn(_routed_frame()) == (None, MISSING)


def test_an_env_file_naming_the_key_is_not_read(tmp_path, monkeypatch):
    """The file the daemon checked is not this process's to parse (rule 11)."""
    env_file = tmp_path / ".env"
    env_file.write_text("ROUTE_KEY='from-the-file'\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "_get_scoped_secret", lambda name, default=None: default)
    assert adapter.route_for_turn(_routed_frame(str(env_file))) == (None, MISSING)


def test_a_host_that_cannot_answer_counts_as_a_missing_key(monkeypatch):
    """An unscoped read under multiplexing raises; that is a refusal, not a borrowed key."""
    def unscoped(name, default=None):
        raise RuntimeError("UnscopedSecretError")

    monkeypatch.setattr(adapter, "_get_scoped_secret", unscoped)
    assert adapter.secret_present("ROUTE_KEY") is False
    assert adapter.route_for_turn(_routed_frame()) == (None, MISSING)


def test_the_value_never_travels_on_the_route(monkeypatch):
    monkeypatch.setattr(adapter, "_get_scoped_secret", lambda name, default=None: "s3cret-value")
    route, refusal = adapter.route_for_turn(_routed_frame())
    assert refusal == ""
    assert "s3cret-value" not in str(route)
    assert "s3cret-value" not in adapter.route_note(route)


def test_no_second_env_file_parser_was_left_behind():
    """The rule-11 fix removed the parser outright; a re-added one is a regression."""
    source = ADAPTER.read_text(encoding="utf-8")
    assert "def env_value" not in source
    assert "open(env_file" not in source
