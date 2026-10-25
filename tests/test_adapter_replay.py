"""Delivery is at-least-once: the consumer must make a replay harmless.

Two properties are load-bearing. An ack that fails may not tear down the reader loop
(that converts a transient socket error into a duplicate-delivery loop), and a replayed
frame for a message Hermes already has must be acknowledged rather than answered again.

The adapter runs inside Hermes, so its `gateway.*` imports are not on this venv's path;
the stubs below are the same shape `test_adapter_control_filter.py` uses.
"""
import importlib.util
import sys
import types
from collections import deque
from pathlib import Path


def _stub(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class _Kw:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _MessageType:
    TEXT = "text"


_stub("gateway")
_stub("gateway.config", Platform=object, PlatformConfig=object)
_stub("gateway.platforms")
_stub("gateway.platforms.base", BasePlatformAdapter=object, SendResult=_Kw,
      SessionSource=_Kw)
_stub("gateway.platforms.event", MessageEvent=_Kw, MessageType=_MessageType)

_spec = importlib.util.spec_from_file_location(
    "nostr_adapter_delivery",
    Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "adapter.py",
)
adapter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(adapter)


def _bare_adapter():
    """An instance without Hermes' constructor: only the delivery path is under test."""
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._delivered_ids = deque(maxlen=512)
    inst._config = None
    return inst


def _frame(ident, content="hello", **extra):
    return {"type": "message", "id": ident, "sender": "ab" * 32,
            "conversation": "ab" * 32, "content": content,
            "authority": "instructions", **extra}


async def test_ack_failure_is_retried_and_swallowed(monkeypatch):
    """A dead ack socket must not raise out of _deliver; the reader loop stays up."""
    inst = _bare_adapter()
    calls = []

    async def boom(payload, timeout=30.0):
        calls.append(payload)
        raise RuntimeError("gateway closed the connection without a reply")

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(adapter.asyncio, "sleep", no_sleep)
    inst._request = boom

    assert await inst._ack("r1") is False
    assert len(calls) == 3  # retried, then gave up with a warning rather than raising


async def test_ack_retry_succeeds_after_one_transient_failure(monkeypatch):
    inst = _bare_adapter()
    attempts = []

    async def flaky(payload, timeout=30.0):
        attempts.append(payload)
        if len(attempts) == 1:
            raise ConnectionError("transient")
        return {"type": "ok"}

    async def no_sleep(_delay):
        return None

    monkeypatch.setattr(adapter.asyncio, "sleep", no_sleep)
    inst._request = flaky

    assert await inst._ack("r2") is True
    assert len(attempts) == 2


async def test_replayed_frame_already_delivered_is_acked_not_answered_again():
    inst = _bare_adapter()
    inst._delivered_ids.append("seen-id")
    acked, handled = [], []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    async def handle(event):
        handled.append(event.text)

    inst._ack = ack
    inst.handle_message = handle

    await inst._deliver(_frame("seen-id", "said this yesterday", replay=True))

    assert handled == []
    assert acked == ["seen-id"]  # the row was still pending: ack it so replay stops


async def test_live_frame_with_a_known_id_is_still_delivered():
    """Only a frame the gateway marked as a replay may be skipped."""
    inst = _bare_adapter()
    inst._delivered_ids.append("seen-id")
    acked, handled = [], []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    async def handle(event):
        handled.append(event.text)

    inst._ack = ack
    inst.handle_message = handle

    await inst._deliver(_frame("seen-id", "a genuinely new message"))

    assert handled == ["a genuinely new message"]
    assert acked == ["seen-id"]
