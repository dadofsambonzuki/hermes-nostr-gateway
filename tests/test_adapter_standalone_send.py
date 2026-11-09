"""`hermes send` and cron reach the daemon without a live adapter in this process.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports. The
transport these tests exercise is real: a Unix socket server answering the one ``send`` frame,
so the frame shape and the reply mapping are asserted on the wire, not on a mock.

Placeholders only -- no npubs, hostnames or personal names.
"""

from __future__ import annotations

import asyncio
import json
import types

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_standalone")

PEER = "a" * 64
CONTROL = "b" * 64


class FakeGateway:
    """A one-connection socket server: records the frame, writes one reply envelope."""

    def __init__(self, path, reply=None, *, silent=False):
        self.path = path
        self.reply = reply or {"type": "delivery", "ok": True, "message_id": "m-1"}
        self.silent = silent
        self.frames: list[dict] = []
        self.connections = 0

    async def handle(self, reader, writer):
        self.connections += 1
        line = await reader.readline()
        if line:
            self.frames.append(json.loads(line))
        if not self.silent:
            writer.write((json.dumps(self.reply, separators=(",", ":")) + "\n").encode())
            await writer.drain()
        writer.close()
        try:
            await writer.wait_closed()
        except ConnectionError:
            pass


def send_with(gateway, chat_id=PEER, text="probe", **kwargs):
    """Run the sender against *gateway* on one loop; return its result."""

    async def scenario():
        server = await asyncio.start_unix_server(gateway.handle, path=str(gateway.path))
        try:
            return await adapter.standalone_send(None, chat_id, text, **kwargs)
        finally:
            server.close()
            await server.wait_closed()

    return asyncio.run(scenario())


def use_socket(monkeypatch, tmp_path):
    path = tmp_path / "gateway.sock"
    monkeypatch.setenv("NOSTR_GATEWAY_SOCKET", str(path))
    return path


def test_the_frame_and_the_reply_become_a_success(tmp_path, monkeypatch):
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path)

    result = send_with(gateway, text="probe")

    assert gateway.connections == 1
    assert len(gateway.frames) == 1
    frame = gateway.frames[0]
    assert frame["op"] == "send"
    assert frame["to"] == PEER
    assert frame["text"] == "probe"
    assert frame["id"], "a send without its own id cannot be told apart from a replay"
    assert len(frame["id"]) == 32, "the id is a uuid4 hex"
    assert result == {"success": True, "platform": "nostr", "chat_id": PEER,
                      "message_id": "m-1"}


def test_the_configured_socket_is_used_when_the_env_var_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("NOSTR_GATEWAY_SOCKET", raising=False)
    path = tmp_path / "configured.sock"
    config = types.SimpleNamespace(extra={"socket_path": str(path)})
    gateway = FakeGateway(path)

    async def scenario():
        server = await asyncio.start_unix_server(gateway.handle, path=str(path))
        try:
            return await adapter.standalone_send(config, PEER, "probe")
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario())["success"] is True
    assert gateway.frames[0]["to"] == PEER


def test_control_text_returns_success_and_writes_no_frame(tmp_path, monkeypatch):
    monkeypatch.delenv("NOSTR_CONTROL_CHANNEL", raising=False)
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path)

    result = send_with(gateway, text="\u2014")

    assert gateway.connections == 0, "a control notice reached the socket"
    assert result == {"success": True, "skipped": "control_notice", "delivered": False}


def test_a_nominated_control_channel_receives_control_text(tmp_path, monkeypatch):
    monkeypatch.setenv("NOSTR_CONTROL_CHANNEL", CONTROL)
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path)

    result = send_with(gateway, chat_id=CONTROL,
                       text="\u26a0\ufe0f Hermes is shutting down")

    assert [frame["to"] for frame in gateway.frames] == [CONTROL]
    assert result["success"] is True


def test_a_daemon_refusal_maps_to_failure_with_its_reason(tmp_path, monkeypatch):
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path, {"type": "error", "error": "peer is not in the policy"})

    result = send_with(gateway)

    assert result["success"] is False
    assert "peer is not in the policy" in result["error"]


def test_a_relay_refusal_maps_to_failure_with_the_daemon_error(tmp_path, monkeypatch):
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path, {"type": "delivery", "ok": False,
                                 "errors": {"wss://relay.invalid": "blocked"}})

    result = send_with(gateway)

    assert result["success"] is False
    assert "blocked" in result["error"]


def test_a_missing_destination_names_the_home_channel_env_var(tmp_path, monkeypatch):
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    use_socket(monkeypatch, tmp_path)

    result = asyncio.run(adapter.standalone_send(None, "", "probe"))

    assert result["success"] is False
    assert "NOSTR_HOME_CHANNEL" in result["error"]


def test_a_missing_daemon_names_the_socket_it_tried(tmp_path, monkeypatch):
    path = use_socket(monkeypatch, tmp_path)  # nothing is listening

    result = asyncio.run(adapter.standalone_send(None, PEER, "probe"))

    assert result["success"] is False
    assert "did not answer" in result["error"]
    assert str(path) in result["error"]


def test_a_silent_daemon_names_the_socket_it_tried(tmp_path, monkeypatch):
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path, silent=True)

    result = send_with(gateway)

    assert result["success"] is False
    assert "did not answer" in result["error"]
    assert str(path) in result["error"]


def test_attachments_are_refused_rather_than_dropped(tmp_path, monkeypatch):
    use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(tmp_path / "gateway.sock")

    result = asyncio.run(adapter.standalone_send(
        None, PEER, "chart", media_files=[{"path": "/tmp/chart.png"}]))

    assert result["success"] is False
    assert "text-only" in result["error"]
    assert gateway.connections == 0


def test_a_hex_pubkey_is_a_native_target():
    assert adapter.parse_target_ref(PEER.upper()) == (PEER, None)


def test_the_daemons_conversation_shapes_are_native_targets():
    for ref in ("npub1" + "q" * 58, "concord:community:channel", "marmot:group-1"):
        assert adapter.parse_target_ref(ref) == (ref, None), ref


def test_anything_else_falls_through_target_resolution():
    for ref in ("", "   ", "hello", "#ops", "concord:only-one", "marmot:"):
        assert adapter.parse_target_ref(ref) is None, ref


class _Ctx:
    """Hermes' plugin context: `register_platform` is all the adapter calls on it."""

    def __init__(self) -> None:
        self.registered: dict = {}

    def register_platform(self, **kwargs) -> None:
        self.registered.update(kwargs)


def test_registration_hands_hermes_the_standalone_sender(tmp_path, monkeypatch):
    """Without this entry Hermes answers `hermes send` with "No live adapter"."""
    monkeypatch.setenv("NOSTR_GATEWAY_COMMANDS", str(tmp_path / "commands.json"))
    monkeypatch.setattr(adapter, "command_catalogue",
                        lambda: [{"name": "help", "description": "Show help",
                                  "args_hint": ""}])
    ctx = _Ctx()

    adapter.register(ctx)

    assert ctx.registered["standalone_sender_fn"] is adapter.standalone_send
    assert ctx.registered["parse_target_ref_fn"] is adapter.parse_target_ref
    assert ctx.registered["cron_deliver_env_var"] == "NOSTR_HOME_CHANNEL"


def test_an_explicit_destination_needs_no_prior_conversation(tmp_path, monkeypatch):
    """The daemon decides reach; this path does not keep (or invent) a peer set."""
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path)
    stranger = "c" * 64

    result = send_with(gateway, chat_id=stranger)

    assert [frame["to"] for frame in gateway.frames] == [stranger]
    assert result["success"] is True


def test_identical_sends_are_each_published(tmp_path, monkeypatch):
    """No replay window here: the daemon's outbound rows are the record."""
    path = use_socket(monkeypatch, tmp_path)
    gateway = FakeGateway(path)

    async def scenario():
        server = await asyncio.start_unix_server(gateway.handle, path=str(path))
        try:
            first = await adapter.standalone_send(None, PEER, "the same text")
            second = await adapter.standalone_send(None, PEER, "the same text")
        finally:
            server.close()
            await server.wait_closed()
        return first, second

    first, second = asyncio.run(scenario())

    assert first["success"] is True and second["success"] is True
    assert len(gateway.frames) == 2
    assert gateway.frames[0]["id"] != gateway.frames[1]["id"]
