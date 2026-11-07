"""A frame left pending has to be offered again — and only a pending frame may cost a reconnect.

The daemon re-offers an unacknowledged frame from exactly one place: the `subscribe` op
(`socket_server` → `replay_pending`). So an unacked frame is only ever retried if the
subscription is replaced. Leaving the frame pending and reading on down the same socket
therefore parks the message until something unrelated disconnects — the loss the pending
branch exists to prevent, only slower.

The other half is just as load-bearing: a verdict about the frame is final, so it is acked and
the subscription stays up. Making every failure reconnect is the reconnect storm the verdict
branch was written to stop.

The third case is why the two were not enough. A failure that is neither a verdict nor the
daemon's condition fails the same way on every attempt, and since the replay is oldest-first a
reconnect re-offers it ahead of everything else: one poisoned frame holds the whole batch until
it ages out of the replay window. So it is skipped — left pending, never acked, named in the log
— and the reader carries on to the next frame.

All of it is driven through the real `_reader_loop` against the real `SocketServer`, because the
defect is in the loop's control flow: a test that calls `_frame_failed` directly cannot see
whether the subscription was reset or the next frame delivered.
"""

import asyncio
from collections import OrderedDict, deque

import pytest

from adapter_harness import load_adapter
from hermes_nostr_gateway.server import SocketServer

adapter = load_adapter("nostr_adapter_pending_replay")

MESSAGE_ID = "99" * 32
SECOND_ID = "77" * 32

#: The daemon's conditions: the bounded set that is worth replacing the connection for. Anything
#: else that fails a frame is that frame's own failure and must not cost the batch.
TRANSIENT_CODES = [
    "session_busy", "storage_unavailable", "runtime_draining",
    "runtime_coordination_required", "worker_not_live", "worker_control_backpressure",
    "launch_credentials_unavailable", "model_resolution_failed",
]


class RuntimeStoreError(ValueError):
    """Stand-in for Hermes' `RuntimeStoreError`: same name, same ``.reason`` shape."""

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


class ConditionDaemon:
    """The daemon's delivery contract as the socket implements it.

    A frame is offered once on acceptance and again on every `subscribe` until it is acked,
    which is what makes a reconnect observable: the subscription count is the number of times
    the daemon was asked to replay.
    """

    def __init__(self, *frames):
        self.pending = [dict(frame) for frame in frames]
        self.acks = []
        self.subscriptions = 0
        self.settled: dict[str, asyncio.Event] = {f["id"]: asyncio.Event() for f in frames}

    def wait_settled(self, ident, timeout=10):
        """Wait until the daemon has retired *ident* by ack; fail the test if it never is."""
        return asyncio.wait_for(self.settled[ident].wait(), timeout)

    def replay_pending(self, limit=20):
        self.subscriptions += 1
        # The real store tags a re-offer, so the adapter can tell "the ack failed" from
        # "the first delivery never happened" (gateway.replay_pending).
        return [{**frame, "replay": True} for frame in self.pending[:limit]]

    def ack(self, ids=None, *, rule=None, frame=None):
        ids = list(ids or [])
        self.acks.extend(ids)
        self.pending = [f for f in self.pending if f.get("id") not in ids]
        for ident in ids:
            self.settled.setdefault(ident, asyncio.Event()).set()
        return {"acked": ids}

    async def status(self):
        return {}


def _frame(ident=MESSAGE_ID):
    return {"type": "message", "id": ident, "sender": "ab" * 32,
            "conversation": "ab" * 32, "content": "hello", "authority": "instructions"}


def _reader_adapter(path):
    """An instance with the reader loop's dependencies and Hermes' constructor skipped."""
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._socket = str(path)
    inst._config = None
    inst._closing = False
    inst._connected = asyncio.Event()
    inst._peers = set()
    inst._delivered_ids = deque(maxlen=512)
    inst._kinds = OrderedDict()
    inst._routes = OrderedDict()
    inst._chat_profiles = OrderedDict()
    return inst


async def test_a_condition_resets_the_subscription_and_the_replay_delivers_it(tmp_path):
    """`session_busy` → nothing consumed → the daemon has to be asked again, and only once."""
    daemon = ConditionDaemon(_frame())
    server = SocketServer(tmp_path / "s.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "s.sock")
    attempts, delivered = [], []

    async def handle_message(event):
        attempts.append(event.message_id)
        if len(attempts) == 1:
            # The daemon's own condition, not a verdict about the message: `handle_message`
            # raised, so Hermes took nothing and the row is still pending.
            raise RuntimeStoreError("session_busy")
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        # The ack is the observable end of the retry: it only exists if the frame was
        # offered again, taken, and retired.
        await daemon.wait_settled(MESSAGE_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert attempts == [MESSAGE_ID, MESSAGE_ID], "the pending frame was never offered again"
    assert delivered == [MESSAGE_ID]
    assert daemon.acks == [MESSAGE_ID], "the replayed frame must be acked once it is taken"
    assert daemon.subscriptions == 2, (
        "an unacked frame is only retried by a new subscription: the reader has to reset the "
        "socket rather than read on"
    )


async def test_a_condition_that_lasts_backs_off_instead_of_looping(tmp_path, monkeypatch):
    """Retrying is the point, but one retry a second is the storm: the wait has to grow."""
    daemon = ConditionDaemon(_frame())
    server = SocketServer(tmp_path / "b.sock", daemon)
    await server.start()

    waits = []

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(adapter.asyncio, "sleep", no_wait)

    inst = _reader_adapter(tmp_path / "b.sock")
    attempts = []

    async def handle_message(event):
        attempts.append(event.message_id)
        if len(attempts) <= 3:
            raise RuntimeStoreError("runtime_draining")

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(MESSAGE_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert attempts == [MESSAGE_ID] * 4
    assert daemon.subscriptions == 4
    assert waits == [1.0, 2.0, 4.0], "a lasting condition must back off, not retry in a loop"


async def test_a_frame_that_always_fails_does_not_stall_the_frames_behind_it(tmp_path, caplog):
    """Two pending frames, the first deterministically broken: the second must be delivered.

    The starvation, measured on the daemon's own contract. `replay_pending` offers oldest-first
    and a reconnect starts the batch again, so replacing the subscription for a frame that can
    never succeed means the frames behind it are never reached — they age out of the replay
    window unanswered (120 minutes by default). A failure that is neither a verdict nor a
    condition is therefore skipped: named in the log, left pending, and read past.
    """
    daemon = ConditionDaemon(_frame(), _frame(SECOND_ID))
    server = SocketServer(tmp_path / "p.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "p.sock")
    attempts, delivered = [], []

    async def handle_message(event):
        attempts.append(event.message_id)
        if event.message_id == MESSAGE_ID:
            # Deterministic: the same frame fails the same way on every attempt, forever.
            raise ValueError("this frame is broken")
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(SECOND_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert attempts == [MESSAGE_ID, SECOND_ID], "the frame behind the broken one was never offered"
    assert delivered == [SECOND_ID]
    assert daemon.acks == [SECOND_ID]
    assert [f["id"] for f in daemon.pending] == [MESSAGE_ID], (
        "a skipped frame stays pending for a later pass: it must not be acked, and not be lost"
    )
    assert daemon.subscriptions == 1, (
        "a deterministic failure must not resubscribe: the replay would re-offer it first, ahead "
        "of every frame behind it"
    )
    assert MESSAGE_ID[:12] in caplog.text and "skipped" in caplog.text, (
        "the skipped frame has to be named in the log so an operator can find it"
    )


async def test_only_a_verdict_tells_the_loop_to_read_on():
    """`_frame_failed`'s answer is the loop's only signal, and it has three values, not two.

    Unit-level because the loop tests above cannot reach the classification on its own: Hermes
    raises plenty of things that are not `RuntimeStoreError` at all, and a newer Hermes can raise
    a code this adapter has never seen. Neither is a verdict about the frame, and neither is one
    of the conditions that is worth replacing the connection for.
    """
    inst = _reader_adapter("/nonexistent")
    acked = []

    async def ack(message_id, attempts=3):
        acked.append(message_id)
        return True

    inst._ack = ack
    frame = _frame()

    # A verdict is final: acked, and the stream carries on.
    assert await inst._frame_failed(frame, RuntimeStoreError("permission_denied")) is \
        adapter.FrameOutcome.CONTINUE
    # The daemon's condition: unacked, and worth a reconnect.
    for code in TRANSIENT_CODES:
        assert await inst._frame_failed(frame, RuntimeStoreError(code)) is \
            adapter.FrameOutcome.RETRY, code
    # Neither: a failure of this frame. Skipped, never acked, never a reconnect.
    assert await inst._frame_failed(frame, TimeoutError("db is locked")) is \
        adapter.FrameOutcome.SKIP
    assert await inst._frame_failed(frame, RuntimeStoreError("a_code_from_a_newer_hermes")) is \
        adapter.FrameOutcome.SKIP
    assert acked == [MESSAGE_ID], "only a verdict acks: a skipped frame must stay pending"


async def test_a_verdict_acks_without_resetting_the_subscription(tmp_path):
    """A verdict is final, so it must not cost a reconnect — that is the storm.

    A second frame pushed down the same subscription is the proof: it can only arrive if the
    loop is still reading the socket it subscribed on.
    """
    daemon = ConditionDaemon(_frame())
    second = _frame(SECOND_ID)
    daemon.settled[SECOND_ID] = asyncio.Event()
    server = SocketServer(tmp_path / "v.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "v.sock")
    attempts = []

    async def handle_message(event):
        attempts.append(event.message_id)
        if event.message_id == MESSAGE_ID:
            raise RuntimeStoreError("permission_denied")

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(MESSAGE_ID)
        await server.broadcast(second)
        await daemon.wait_settled(SECOND_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert attempts == [MESSAGE_ID, SECOND_ID]
    assert daemon.acks == [MESSAGE_ID, SECOND_ID]
    assert daemon.subscriptions == 1, "a verdict must not cost a resubscribe"
