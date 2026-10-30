"""The adapter's half of a recovery re-ask, and what a failed frame costs.

Two properties, both learned from a live incident: Hermes refuses to let `role_authorized`
vouch for itself on a recovered turn and asks the connector again (`reauthorize_native_roles`),
and a handler refusal must not escape the reader loop — letting it out tore the socket down
and the daemon re-offered the same frame every second, so one refusal became a reconnect
storm and a repeated apology in the channel.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports.
"""

from collections import OrderedDict, deque
from types import SimpleNamespace

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_reauthorize")


class Refused(ValueError):
    """Stand-in for Hermes' RuntimeStoreError: the code is what the adapter reads."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


def _bare_adapter():
    """An instance without Hermes' constructor: only the paths under test are set."""
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = set()
    inst._delivered_ids = deque(maxlen=512)
    inst._kinds = OrderedDict()
    inst._config = None
    return inst


def _source(**overrides):
    base = {"user_id": "ab" * 32, "chat_id": "concord:" + "cd" * 32 + ":" + "ef" * 32,
            "message_id": "12" * 32}
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_a_live_grant_is_relayed_not_remembered():
    inst = _bare_adapter()
    sent = []

    async def request(payload, timeout=30.0):
        sent.append(payload)
        return {"type": "authorized", "authorized": True, "authority": "instructions"}

    inst._request = request
    inst._remember_kind("12" * 32, 9)

    assert await inst.reauthorize_native_roles(_source()) is True
    assert sent == [{"op": "authorize", "pubkey": "ab" * 32,
                     "conversation": "concord:" + "cd" * 32 + ":" + "ef" * 32, "kind": 9}]


async def test_a_daemon_that_no_longer_trusts_the_sender_is_a_no():
    inst = _bare_adapter()

    async def request(payload, timeout=30.0):
        return {"type": "authorized", "authorized": False, "authority": "none"}

    inst._request = request
    assert await inst.reauthorize_native_roles(_source()) is False


async def test_a_daemon_too_old_to_answer_is_a_no():
    """`unsupported` is a refusal, never a silent pass: the grant cannot outlive the check."""
    inst = _bare_adapter()

    async def request(payload, timeout=30.0):
        return {"type": "error", "op": "authorize", "code": "unsupported",
                "error": "authorize is not available on this daemon"}

    inst._request = request
    assert await inst.reauthorize_native_roles(_source()) is False


async def test_a_broken_socket_is_a_no():
    inst = _bare_adapter()

    async def request(payload, timeout=30.0):
        raise ConnectionError("gateway closed the connection without a reply")

    inst._request = request
    assert await inst.reauthorize_native_roles(_source()) is False


async def test_a_source_without_a_conversation_is_a_no():
    inst = _bare_adapter()
    called = []

    async def request(payload, timeout=30.0):
        called.append(payload)
        return {"type": "authorized", "authorized": True}

    inst._request = request
    assert await inst.reauthorize_native_roles(_source(chat_id="")) is False
    assert called == [], "nothing to ask about must not reach the socket"


async def test_a_refused_frame_is_acknowledged_and_the_loop_survives():
    """The incident: refusal escaping `_deliver` reconnected once a second."""
    inst = _bare_adapter()
    acked = []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    inst._ack = ack
    frame = {"type": "message", "id": "99" * 32, "sender": "ab" * 32,
             "conversation": "ab" * 32, "content": "hi", "authority": "instructions"}

    await inst._frame_failed(frame, Refused("permission_denied"))

    assert acked == ["99" * 32]


async def test_a_transient_failure_stays_pending_so_the_daemon_replays_it():
    inst = _bare_adapter()
    acked = []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    inst._ack = ack
    frame = {"type": "message", "id": "99" * 32, "sender": "ab" * 32,
             "conversation": "ab" * 32, "content": "hi", "authority": "instructions"}

    await inst._frame_failed(frame, TimeoutError("db is locked"))

    assert acked == [], "a message that may yet run must not be dropped by an ack"


async def test_a_reaction_refusal_is_not_acked_as_a_message():
    """A reaction has no message row to ack, and asking would confuse the delivery ledger."""
    inst = _bare_adapter()
    acked = []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    inst._ack = ack
    await inst._frame_failed({"type": "reaction", "id": "77" * 32}, Refused("permission_denied"))

    assert acked == []


def test_the_refusal_code_is_read_from_the_reason_not_the_class():
    assert adapter.refusal_code(Refused("permission_denied")) == "permission_denied"
    assert adapter.refusal_code(TimeoutError("db is locked")) is None
