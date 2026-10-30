"""The adapter's half of a recovery re-ask, and what a failed frame costs.

Two properties, both learned from a live incident: Hermes refuses to let `role_authorized`
vouch for itself on a recovered turn and asks the connector again (`reauthorize_native_roles`),
and a handler refusal must not escape the reader loop — letting it out tore the socket down
and the daemon re-offered the same frame every second, so one refusal became a reconnect
storm and a repeated apology in the channel.

A refusal is not one thing, though. Hermes raises `RuntimeStoreError` for a verdict about the
message (`permission_denied`, `not_found`, ...) *and* for the daemon's own condition
(`storage_unavailable`, `session_busy`, `runtime_draining`, ...), and it re-raises every code
except `queue_full`, so the adapter is what has to tell them apart: only a verdict may ack the
frame, because acking a condition drops a message that may yet run.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports.
"""

from collections import OrderedDict, deque
from types import SimpleNamespace

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_reauthorize")

#: The codes Hermes' admission path raises to refuse a message outright.
VERDICT_CODES = ("permission_denied", "not_found", "invalid_params",
                 "profile_mismatch", "admission_conflict")


class RuntimeStoreError(ValueError):
    """Stand-in for Hermes' `RuntimeStoreError`: same name, same ``.reason`` shape.

    The adapter duck-types on the class *name* (it is loaded by more than one Hermes
    build), so the double has to carry the real name for that path to be exercised.
    """

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


def _frame():
    return {"type": "message", "id": "99" * 32, "sender": "ab" * 32,
            "conversation": "ab" * 32, "content": "hi", "authority": "instructions"}


def _acking_adapter(acked):
    inst = _bare_adapter()

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    inst._ack = ack
    return inst


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


async def test_a_reply_that_is_not_an_object_is_a_no():
    """A parsed non-object used to reach `.get` and escape as an AttributeError.

    Hermes' `reauthorize_roles` does not catch what the adapter raises, so that exception
    surfaced as a turn error instead of the refusal this check exists to produce.
    """
    for reply in (["authorized"], "authorized", 1, True, None):
        inst = _bare_adapter()

        async def request(payload, timeout=30.0, answer=reply):
            return answer

        inst._request = request
        assert await inst.reauthorize_native_roles(_source()) is False, reply


async def test_an_unknown_reply_shape_is_a_no():
    """A daemon that answers something new is not a daemon that said yes."""
    inst = _bare_adapter()

    async def request(payload, timeout=30.0):
        return {"type": "nonsense"}

    inst._request = request
    assert await inst.reauthorize_native_roles(_source()) is False


async def test_a_refused_frame_is_acknowledged_and_the_loop_survives():
    """The incident: refusal escaping `_deliver` reconnected once a second."""
    acked = []
    inst = _acking_adapter(acked)
    frame = _frame()

    await inst._frame_failed(frame, RuntimeStoreError("permission_denied"))

    assert acked == ["99" * 32]


async def test_every_frame_verdict_acknowledges_the_frame():
    """Each admission verdict is final for the frame, so the row is retired."""
    for code in VERDICT_CODES:
        acked = []
        inst = _acking_adapter(acked)

        await inst._frame_failed(_frame(), RuntimeStoreError(code))

        assert acked == ["99" * 32], code


async def test_a_transient_failure_stays_pending_so_the_daemon_replays_it():
    acked = []
    inst = _acking_adapter(acked)

    await inst._frame_failed(_frame(), TimeoutError("db is locked"))

    assert acked == [], "a message that may yet run must not be dropped by an ack"


async def test_a_daemon_condition_stays_pending_even_with_a_refusal_code():
    """Hermes re-raises RuntimeStoreError for conditions too: acking one loses the message.

    These are the codes `gateway/session_ingress.py` re-raises past admission (everything
    but `invalid_params`-with-field and `queue_full`). None of them says anything about
    this frame's content; the daemon was busy, draining or out of storage, so the same
    frame may well run on the next reconnect.
    """
    conditions = ("storage_unavailable", "session_busy", "runtime_draining",
                  "runtime_coordination_required", "worker_not_live",
                  "worker_control_backpressure", "launch_credentials_unavailable",
                  "model_resolution_failed", "a_code_from_a_newer_hermes")
    acked = []
    inst = _acking_adapter(acked)

    for code in conditions:
        await inst._frame_failed(_frame(), RuntimeStoreError(code))

    assert acked == [], "a frame the daemon could not answer yet must not be dropped by an ack"


async def test_a_reaction_refusal_is_not_acked_as_a_message():
    """A reaction has no message row to ack, and asking would confuse the delivery ledger."""
    acked = []
    inst = _acking_adapter(acked)
    await inst._frame_failed({"type": "reaction", "id": "77" * 32},
                             RuntimeStoreError("permission_denied"))

    assert acked == []


def test_the_refusal_code_needs_the_class_and_reads_the_reason_from_it():
    assert adapter.refusal_code(RuntimeStoreError("permission_denied")) == "permission_denied"
    assert adapter.refusal_code(TimeoutError("db is locked")) is None

    class Impostor(ValueError):
        """.reason without the class: not a refusal, whatever it looks like."""
        reason = "permission_denied"

    assert adapter.refusal_code(Impostor("boom")) is None


def test_only_the_admission_codes_are_frame_verdicts():
    for code in VERDICT_CODES:
        assert adapter.is_frame_verdict(code) is True, code
    assert adapter.is_frame_verdict("storage_unavailable") is False
    assert adapter.is_frame_verdict("session_busy") is False
    assert adapter.is_frame_verdict("a_code_from_a_newer_hermes") is False
    assert adapter.is_frame_verdict(None) is False
