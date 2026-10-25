"""Hermes sends its own control notices through the platform path; they must not ship.

The adapter runs inside Hermes, so its `gateway.*` imports are not on this venv's path.
Stub the three modules it needs before loading it: the filter itself is stdlib-only.
"""
import importlib.util
import sys
import types
from pathlib import Path


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _SendResult:
    def __init__(self, success=True, error=None, retryable=False, message_id=None, raw_response=None):
        self.success = success
        self.error = error
        self.retryable = retryable
        self.message_id = message_id
        self.raw_response = raw_response


class _Kw:
    """Stand-in for SessionSource/MessageEvent: keyword attributes, nothing else."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MessageType:
    TEXT = "text"


_stub("gateway")
_stub("gateway.config", Platform=object, PlatformConfig=object)
_stub("gateway.platforms")
_stub("gateway.platforms.base", BasePlatformAdapter=object, SendResult=_SendResult,
      SessionSource=_Kw)
_stub("gateway.platforms.event", MessageEvent=_Kw, MessageType=_MessageType)

_spec = importlib.util.spec_from_file_location(
    "nostr_adapter",
    Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "adapter.py",
)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)


def test_control_notices_are_recognised():
    for text in ["", "   ", "\u2014", "- ", "[interrupt: no assistant output for this turn]",
                 "\u21aa Redirected current run. I'll adjust.",
                 "\u23f3 Provider temporarily unavailable \u2014 retrying automatically in 15s",
                 "\U0001f4ec No home channel is set for Nostr."]:
        assert adapter.is_control_text(text), text


def test_real_messages_are_not():
    for text in ["Morning. Sit rep pls", "Done \u2014 the queue is live and the worker is scheduled.",
                 "So you can't just run to a goal?", "Thanks, noted.",
                 "Redirected current run is the bug I mean"]:
        assert not adapter.is_control_text(text), text


def test_shutdown_warnings_are_control_notices():
    for text in ["\u26a0\ufe0f Hermes is shutting down \u2014 your current task will be interrupted.",
                 "\u26a0\ufe0f Hermes is restarting; reconnect in a moment"]:
        assert adapter.is_control_text(text), text


def test_the_class_still_owns_its_transport_methods():
    """A module-level block spliced into the class body once ate send() and _request()."""
    for name in ("send", "_request", "_deliver", "_reader_loop", "_may_address"):
        assert name in vars(adapter.NostrGatewayAdapter), name


def _bare_adapter():
    inst = adapter.NostrGatewayAdapter.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._home_hex = ""
    inst._config = None
    inst.acked = []
    inst.delivered = []

    async def _request(payload, timeout=30.0):
        if payload.get("op") == "ack":
            inst.acked.extend(payload.get("ids") or [])
        return {"type": "ok"}

    async def handle_message(event):
        inst.delivered.append(event)

    inst._request = _request
    inst.handle_message = handle_message
    return inst


def test_inbound_control_notice_is_acked_and_never_delivered():
    inst = _bare_adapter()
    frame = {"type": "message", "id": "abc", "sender": "a" * 64,
             "conversation": "a" * 64, "content": "[interrupt: no assistant output for this turn]"}
    import asyncio
    asyncio.run(inst._deliver(frame))
    assert inst.acked == ["abc"]
    assert inst.delivered == []


def test_inbound_real_message_is_delivered_and_the_peer_remembered():
    inst = _bare_adapter()
    frame = {"type": "message", "id": "def", "sender": "b" * 64,
             "conversation": "b" * 64, "content": "Morning. Sit rep pls"}
    import asyncio
    asyncio.run(inst._deliver(frame))
    assert inst.acked == ["def"]
    assert len(inst.delivered) == 1
    assert inst._may_address("B" * 64)


def test_send_refuses_an_invented_destination(monkeypatch):
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    inst = _bare_adapter()
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "x"}

    inst._request = _request
    import asyncio
    result = asyncio.run(inst.send("c" * 64, "real message"))
    assert sent == []
    assert result.success is True
    assert (result.raw_response or {}).get("skipped") == "unknown_destination"


def test_send_allows_the_home_channel(monkeypatch):
    monkeypatch.setenv("NOSTR_HOME_CHANNEL", "D" * 64)
    inst = _bare_adapter()
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "x"}

    inst._request = _request
    import asyncio
    result = asyncio.run(inst.send("d" * 64, "real message"))
    assert [p.get("to") for p in sent] == ["d" * 64]
    assert result.success is True


def test_control_text_is_still_dropped_for_an_ordinary_peer(monkeypatch):
    monkeypatch.delenv("NOSTR_CONTROL_CHANNEL", raising=False)
    inst = _bare_adapter()
    inst._peers.add("e" * 64)
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "x"}

    inst._request = _request
    import asyncio
    result = asyncio.run(inst.send("E" * 64, "[interrupt: no assistant output for this turn]"))
    assert sent == []
    assert (result.raw_response or {}).get("skipped") == "control_notice"


def test_a_nominated_control_channel_receives_control_text(monkeypatch):
    """Deliberate control plane: naming the npub is the exception to the drop rule."""
    monkeypatch.setenv("NOSTR_CONTROL_CHANNEL", "f" * 64)
    inst = _bare_adapter()
    sent = []

    async def _request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "x"}

    inst._request = _request
    import asyncio
    result = asyncio.run(inst.send("f" * 64, "[interrupt: no assistant output for this turn]"))
    assert [p.get("to") for p in sent] == ["f" * 64]
    assert result.success is True
