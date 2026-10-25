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


_stub("gateway")
_stub("gateway.config", Platform=object, PlatformConfig=object)
_stub("gateway.platforms")
_stub("gateway.platforms.base", BasePlatformAdapter=object, SendResult=_SendResult,
      SessionSource=object)
_stub("gateway.platforms.event", MessageEvent=object, MessageType=object)

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
