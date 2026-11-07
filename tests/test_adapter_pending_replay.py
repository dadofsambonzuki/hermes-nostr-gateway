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

The fourth case is the other side of the third. A frame the adapter handled but consumed nothing
(a route it cannot honour, a digest with no home channel) is held: never acked, but read past
exactly like a skip — the frames behind it are still answered — and only once the batch the
daemon wrote is finished is the connection replaced, so `subscribe` offers the hold again.
Reconnecting at the held frame instead would put it at the head of every replay; that is the
same starvation, and it is the shape the first review of this branch measured.

All of it is driven through the real `_reader_loop` against the real `SocketServer`, because the
defect is in the loop's control flow: a test that calls `_frame_failed` directly cannot see
whether the subscription was reset or the next frame delivered.
"""

import asyncio
import json
import time
from collections import OrderedDict, deque

import nostr_sdk as sdk
import pytest
import yaml

from adapter_harness import load_adapter
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.planes.concord.channels import ChannelStream
from hermes_nostr_gateway.server import SocketServer
from hermes_nostr_gateway.store import Inbound

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


ROUTE_KEY = "NOSTR_TEST_ROUTE_KEY"
DIGEST_ID = "dd" * 32
HOME_CHAT = "cd" * 32


class ReplayDaemon(ConditionDaemon):
    """`ConditionDaemon` whose re-offers can seed the world between two attempts.

    The two frames below are held for something that can appear later (a route's key, a home
    channel), so it has to be seeded *between* one offer and the next. Only the daemon can say
    when a re-offer happened without the test sleeping on a guess: `replay_pending` runs
    synchronously in the server's `subscribe` handler, so the frames it returns are the first
    ones that can be honoured.
    """

    def __init__(self, *frames, on_replay=None):
        super().__init__(*frames)
        self.on_replay = on_replay
        self.replays: list[int] = []

    def replay_pending(self, limit=20):
        replay = super().replay_pending(limit)
        self.replays.append(self.subscriptions)
        if self.on_replay is not None:
            self.on_replay(self.subscriptions)
        return replay


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


# ── frames held without an exception ────────────────────────────────────────
# The branches below never raise, so `_frame_failed` is not in the picture: `_deliver` handles
# the frame as far as it can go, consumes nothing and acks nothing. Left unacknowledged on a
# live subscription the frame is offered again only by `subscribe`, so the adapter has to
# reset the connection here too — the same signal, one path further along.


def _routed_frame():
    """A frame whose route names a key: the turn runs on it or not at all."""
    return {**_frame(), "route": {"provider": "p3", "model": "model-b",
                                  "key": ROUTE_KEY, "rule": "channel"}}


def _digest_frame():
    return {"type": "message", "plane": "digest", "id": DIGEST_ID, "sender": "digest",
            "conversation": "digest", "content": "3 item(s) in the ops digest",
            "rule": "ops", "frame": 7}


async def test_a_frame_held_for_its_route_is_reoffered_and_delivered_once(tmp_path, monkeypatch):
    """`route_key_missing` consumes nothing, so it has to cost a resubscribe — and only one ack."""
    monkeypatch.delenv(ROUTE_KEY, raising=False)
    acks_before_the_key = []

    def export_key_on_reoffer(count):
        if count == 2:
            # What the daemon held when it re-offered the frame: nothing acked it, because
            # nothing consumed it.
            acks_before_the_key.append(list(daemon.acks))
            monkeypatch.setenv(ROUTE_KEY, "route-value")

    daemon = ReplayDaemon(_routed_frame(), on_replay=export_key_on_reoffer)
    server = SocketServer(tmp_path / "r.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "r.sock")
    delivered = []

    async def handle_message(event):
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        # The ack is the observable end of the retry: it only exists if the held frame was
        # offered again, taken this time, and retired.
        await daemon.wait_settled(MESSAGE_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert acks_before_the_key == [[]], "a route it cannot honour is a turn that does not happen"
    assert delivered == [MESSAGE_ID], "the replayed turn had to run exactly once"
    assert daemon.acks == [MESSAGE_ID], "the replayed frame must be acked once it is taken"
    assert daemon.subscriptions == 2, (
        "a held frame is only retried by a new subscription: the reader has to reset the "
        "socket rather than read on"
    )


async def test_a_digest_with_no_home_channel_resubscribes_and_is_not_acked(tmp_path, monkeypatch):
    """A batch with nowhere to go is held, not parked — and never acked as if Hermes had it."""
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    acks_before_the_home = []

    def set_home_on_reoffer(count):
        if count == 2:
            acks_before_the_home.append(list(daemon.acks))
            monkeypatch.setenv("NOSTR_HOME_CHANNEL", HOME_CHAT)

    daemon = ReplayDaemon(_digest_frame(), on_replay=set_home_on_reoffer)
    server = SocketServer(tmp_path / "d.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "d.sock")
    delivered = []

    async def handle_message(event):
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(DIGEST_ID)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert acks_before_the_home == [[]], (
        "an ack claims Hermes took the batch, and with no home channel nothing did"
    )
    assert delivered == [DIGEST_ID], "the batch had to reach its home channel exactly once"
    assert daemon.acks == [DIGEST_ID]
    assert daemon.subscriptions == 2, (
        "the batch was held for a home channel, so the reader has to resubscribe to be "
        "offered it again"
    )


async def test_a_digest_with_no_home_channel_keeps_being_offered(tmp_path, monkeypatch):
    """Holding is not a one-shot: until a home channel exists the batch stays pending and unacked."""
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    daemon = ReplayDaemon(_digest_frame())
    server = SocketServer(tmp_path / "d2.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "d2.sock")
    delivered = []

    async def handle_message(event):
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        deadline = asyncio.get_running_loop().time() + 10
        while len(daemon.replays) < 3:
            if asyncio.get_running_loop().time() > deadline:
                raise AssertionError(
                    f"the batch was offered {len(daemon.replays)} time(s) and then parked"
                )
            await asyncio.sleep(0.05)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert delivered == [], "no home channel means no destination, however many offers"
    assert daemon.acks == [], "an unacked batch stays pending until it has somewhere to go"
    assert [frame["id"] for frame in daemon.pending] == [DIGEST_ID]


async def test_a_held_frame_backs_off_instead_of_being_retried_every_second(tmp_path, monkeypatch):
    """A hold that lasts is a condition like any other: the wait between offers has to grow."""
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    waits = []

    async def no_wait(delay):
        waits.append(delay)

    monkeypatch.setattr(adapter.asyncio, "sleep", no_wait)

    inst = _reader_adapter(tmp_path / "b.sock")

    def stop_after_four(count):
        # Stop on the fourth offer, so the loop leaves by its own route instead of the test
        # cancelling it mid-cycle: the delays recorded are then exactly the ones it slept.
        if count == 4:
            inst._closing = True

    daemon = ReplayDaemon(_digest_frame(), on_replay=stop_after_four)
    server = SocketServer(tmp_path / "b.sock", daemon)
    await server.start()

    async def handle_message(event):
        raise AssertionError("a batch with no home channel must never be delivered")

    inst.handle_message = handle_message
    try:
        await asyncio.wait_for(asyncio.create_task(inst._reader_loop()), 10)
    finally:
        await server.stop()

    assert waits == [1.0, 2.0, 4.0], "a lasting hold must back off, not retry in a loop"
    assert daemon.acks == []


# ── a hold must not take the batch with it ──────────────────────────────────
# The measured repro from the first review: two pending frames, the held one first and a plain
# deliverable message behind it, `replay_pending` oldest-first. Replacing the connection at the
# held frame re-offers that frame ahead of the message on every attempt, so the message is never
# delivered. Both hold shapes this branch adds have to be read past, and both have to leave the
# frame pending for the next offer.

async def _until(condition, what, timeout=10):
    """Wait for *condition*, failing at the deadline instead of hanging on the loop."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"{what} (waited {timeout}s)")
        await asyncio.sleep(0.05)


#: The two ways this adapter holds a frame without raising: a route whose key it cannot read (the
#: gateway resolved the route, this process is the one that has to hold the credential), and a
#: digest batch with no home channel to put it in.
_HOLD_SHAPES = {
    "route": (_routed_frame, MESSAGE_ID, adapter._HELD_ROUTE),
    "digest": (_digest_frame, DIGEST_ID, adapter._HELD_DIGEST),
}


@pytest.mark.parametrize("shape", sorted(_HOLD_SHAPES))
async def test_a_held_frame_does_not_stall_the_frames_behind_it(
        tmp_path, monkeypatch, caplog, shape):
    """Held first, deliverable second: the second is delivered, the first stays unacked.

    The blocked path was the loop leaving at the first held frame, which closed a socket the
    daemon had already written the whole replay to. So the batch behind it was discarded, and
    the next subscription re-offered the same held frame first. A hold is read past like a
    skipped frame; the connection is replaced when the batch is finished, where the hold is
    offered again and is still pending.
    """
    held_frame, held_id, reason = _HOLD_SHAPES[shape]
    # Neither shape has what it needs: no credential for the route, no home for the digest.
    monkeypatch.delenv(ROUTE_KEY, raising=False)
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)

    daemon = ReplayDaemon(held_frame(), _frame(SECOND_ID))
    sock = tmp_path / f"held-batch-{shape}.sock"
    server = SocketServer(sock, daemon)
    await server.start()

    inst = _reader_adapter(sock)
    handled, at_subscription = [], []

    async def handle_message(event):
        handled.append(event.message_id)
        # Which subscription this frame was answered on: the held frame's own, or a replay?
        at_subscription.append(daemon.subscriptions)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(SECOND_ID)
        await _until(lambda: len(daemon.replays) >= 2,
                     f"the {shape} hold was never offered again on a later subscription")
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert handled == [SECOND_ID], "the frame behind the held one was never delivered"
    assert at_subscription == [1], (
        "the frame behind a hold has to be answered on the subscription that held it: a "
        "reconnect first re-offers the held frame, ahead of everything behind it"
    )
    assert daemon.acks == [SECOND_ID], "only the frame Hermes took may be acked"
    assert [frame["id"] for frame in daemon.pending] == [held_id], (
        "a held frame is never acked and never lost: it stays pending for the next offer"
    )
    assert "holding frame" in caplog.text and held_id[:12] in caplog.text \
        and reason in caplog.text, "the hold has to be named in the log with its reason"


# ── the daemon's own `blocked` frame ────────────────────────────────────────
# The hold shapes above are frames the adapter decides on. The daemon has a third one: a route
# or profile it could not resolve arrives as `{"type": "blocked", "reason": …}`, whose row it
# keeps offering on every `subscribe` (only a *policy* block writes `blocked_reason`, and
# `pending_immediate` excludes exactly those). Settling that frame is the same park the hold
# branch exists to prevent, so it is driven here through the real `Gateway` and `Store` rather
# than a hand-written frame: the reason strings, the `rumor_id` field and the replay are the
# daemon's own, and a test double could agree with itself about a shape it never sends.
#
# Which of the two a frame is, is not a question this adapter answers from the reason — a list
# of strings it remembered settled every refusal the daemon added later. The daemon sets
# `final` from the row it wrote: true when the row is retired, false while `pending_immediate`
# still holds it. These tests pin the flag to the store for every refusal site, and pin the
# adapter to the flag rather than to the reason.

PEER_HEX = "ab" * 32
ROUTE_KEY = "NOSTR_TEST_ROUTE_KEY"


def _gateway(tmp_path, routing=None, *, profiles=None, hermes_config=None) -> Gateway:
    """A real `Gateway` with an empty policy, for its frames and its store's replay."""
    secret = tmp_path / "identity.json"
    secret.write_text(json.dumps(
        {"secret_key_hex": sdk.Keys.generate().secret_key().to_hex()}))
    config = {
        "identity": {"secret_file": str(secret)},
        "relays": {"read": [], "publish": []},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {}, "policies": [], "defaults": {},
        "marmot": {"enabled": False, "hermes_platform": False},
    }
    if routing is not None:
        config["routing"] = routing
    if profiles is not None:
        config["profiles"] = profiles
        config["hermes_config"] = hermes_config
    return Gateway(config, socket_path=tmp_path / "gateway.sock")


def _inbound(ident, conversation, plane, content, *, reach="immediate",
             blocked_reason=None) -> Inbound:
    return Inbound(id=ident, wrapper_id="w" * 64, sender=PEER_HEX, kind=14,
                   created_at=int(time.time()), content=content,
                   conversation=conversation, reply_to=None, plane=plane,
                   reach=reach, authority="instructions", rule="r", why="w",
                   blocked_reason=blocked_reason)


class GatewayDaemon:
    """The socket's `subscribe`/`ack` contract, answered by the real `Gateway`.

    Nothing here restates the daemon's rules: a subscription gets `Gateway.replay_pending`'s
    own frames and an ack is `Gateway.ack`, so the test cannot pass on a shape the daemon does
    not send.
    """

    def __init__(self, gateway, *, on_replay=None):
        self.gateway = gateway
        self.on_replay = on_replay
        self.subscriptions = 0
        self.replays: list[int] = []
        self.acks: list[str] = []

    def replay_pending(self, limit=20):
        self.subscriptions += 1
        self.replays.append(self.subscriptions)
        if self.on_replay is not None:
            self.on_replay(self.subscriptions)
        return self.gateway.replay_pending()

    def ack(self, ids=None, *, rule=None, frame=None):
        ids = list(ids or [])
        self.acks.extend(ids)
        return self.gateway.ack(ids)

    async def status(self):
        return {}


#: One plane rule whose credential this process cannot read: a DM on `nip17` is refused by the
#: daemon, while everything on another plane still takes the default route.
_MISSING_KEY_ROUTING = {
    "models": ["model-a"],
    "default": {"provider": "p1", "model": "model-a", "key": "GATEWAY_KEY"},
    "planes": {"nip17": {"provider": "p2", "model": "model-a", "key": ROUTE_KEY}},
}


async def test_a_blocked_route_refusal_is_held_and_the_message_behind_it_is_delivered(
        tmp_path, monkeypatch, caplog):
    """Held first, deliverable second, with the daemon's own refusal frames.

    `Gateway.frame_for`/`replay_pending` answer a route they cannot honour with
    `{"type": "blocked", "reason": "route_key_missing"}`, and the row stays in
    `pending_immediate`. Settled, that frame parks the row until something unrelated
    disconnects; held, the message behind it is still delivered on the same subscription and
    the refused row comes back as a `message` frame once the key exists.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    monkeypatch.delenv(ROUTE_KEY, raising=False)

    gateway = _gateway(tmp_path, _MISSING_KEY_ROUTING)
    held_id, behind_id = "a1" * 32, "b2" * 32
    gateway.store.add(_inbound(held_id, PEER_HEX, "nip17", "held route"))
    gateway.store.add(_inbound(behind_id, "marmot:group", "marmot", "deliverable"))
    try:
        # The daemon's half first: this is its refusal shape, and its row is replayable.
        offered = gateway.replay_pending()
        assert [item["type"] for item in offered] == ["blocked", "message"]
        assert offered[0]["reason"] == "route_key_missing"
        assert offered[0]["rumor_id"] == held_id, "a blocked frame names its row `rumor_id`"
        assert offered[0]["final"] is False, (
            "the row is still in `pending_immediate`, so the daemon must say the frame is "
            "not the last word on it"
        )
        assert held_id in [row["id"] for row in gateway.store.pending_immediate()], (
            "a route refusal is not a policy block: the daemon keeps offering the row"
        )

        acks_at_the_second_offer = []

        def export_key_on_reoffer(count):
            if count == 2:
                acks_at_the_second_offer.append(list(daemon.acks))
                monkeypatch.setenv(ROUTE_KEY, "route-value")

        daemon = GatewayDaemon(gateway, on_replay=export_key_on_reoffer)
        sock = tmp_path / "blocked.sock"
        server = SocketServer(sock, daemon)
        await server.start()

        inst = _reader_adapter(sock)
        handled, at_subscription = [], []

        async def handle_message(event):
            handled.append(event.message_id)
            at_subscription.append(daemon.subscriptions)

        inst.handle_message = handle_message
        task = asyncio.create_task(inst._reader_loop())
        try:
            await _until(lambda: held_id in daemon.acks,
                         "the refused frame was never delivered once the key existed")
        finally:
            inst._closing = True
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await server.stop()
    finally:
        gateway.store.close()

    assert acks_at_the_second_offer == [[behind_id]], (
        "the refusal is not a delivery: while it was blocked nothing may be acked for it"
    )
    assert handled == [behind_id, held_id], (
        "the message behind the refusal has to be delivered, and the refusal exactly once"
    )
    assert at_subscription == [1, 2], (
        "the message behind the hold is answered on the subscription that held it; the held "
        "frame only comes back on the next one"
    )
    assert daemon.acks == [behind_id, held_id]
    assert gateway.store.pending_immediate() == [], "nothing is left pending once both land"
    assert "route_key_missing" in caplog.text and "holding frame" in caplog.text, (
        "the refusal and the hold have to be named in the log"
    )


async def test_a_route_refusal_is_held_until_the_channel_is_held_then_delivered_once(
        tmp_path, monkeypatch, caplog):
    """A refusal that lasts, on the daemon's own `route_unresolved`: held, held, delivered.

    Settled, the first offer parks the row until something unrelated disconnects. Held, every
    `subscribe` offers it again — the reader replaces the connection each time the batch ends
    with the hold — and once the channel is held the row arrives exactly once. Nothing may be
    acked while the condition lasts: an ack would claim Hermes took a message it never saw.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    gateway, msg = _refused_channel_route(tmp_path)

    offers = []

    def hold_the_channel_on_third_offer(count):
        offers.append(count)
        if count == 3:
            # The channel list the Control plane would have refreshed: the room is held now,
            # so the same routing rule resolves and the row becomes a turn.
            gateway.concord.channels = lambda: [ChannelStream(
                community_id=COMMUNITY, channel_id=CHANNEL, name="room", private=False,
                epoch=0, address="cd" * 32, chat_id=f"concord:{COMMUNITY}:{CHANNEL}")]

    daemon = GatewayDaemon(gateway, on_replay=hold_the_channel_on_third_offer)
    sock = tmp_path / "refusal.sock"
    server = SocketServer(sock, daemon)
    await server.start()

    inst = _reader_adapter(sock)
    handled = []

    async def handle_message(event):
        handled.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await _until(lambda: msg.id in daemon.acks,
                     "the refused row was never delivered once the channel was held")
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert offers[:3] == [1, 2, 3], (
        "an unacked refusal is re-offered by a new subscription and nowhere else: the reader "
        "has to replace the connection for each hold instead of settling the frame"
    )
    assert handled == [msg.id], "the row is delivered exactly once, after the channel returns"
    assert daemon.acks == [msg.id], "nothing may be acked while the refusal is a hold"
    assert gateway.store.pending_immediate() == []
    assert caplog.text.count("holding frame") >= 2, (
        "every offer of the refusal is held and named in the log, not settled"
    )


async def test_a_policy_block_is_settled_because_its_row_is_never_replayed(tmp_path, monkeypatch):
    """The complement: a refusal with no replay behind it must settle, not wait for one.

    `not_allowlisted` is a `drop` and `rate_limited` writes `blocked_reason`; both are what
    `pending_immediate` excludes. A hold could never be answered — every reconnect would pay
    the backoff for a frame the next `subscribe` cannot offer — so the daemon says `final` on
    the frame and the adapter settles it.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    gateway = _gateway(tmp_path)
    try:
        dropped_msg = _inbound("c3" * 32, PEER_HEX, "nip17", "stranger", reach="drop")
        limited_msg = _inbound("d4" * 32, PEER_HEX, "nip17", "flood",
                               reach="drop", blocked_reason="rate_limited")
        for msg in (dropped_msg, limited_msg):
            gateway.store.add(msg)
        dropped = gateway.frame_for(dropped_msg)
        limited = gateway.frame_for(limited_msg)

        assert (dropped["type"], dropped["reason"]) == ("blocked", "not_allowlisted")
        assert (limited["type"], limited["reason"]) == ("blocked", "rate_limited")
        assert (dropped["final"], limited["final"]) == (True, True), (
            "a policy refusal retires its row: no `subscribe` can offer it again, and the "
            "frame has to say so or the adapter waits for a replay that cannot come"
        )
        assert gateway.store.pending_immediate() == [], (
            "a policy refusal is not replayable: the hold branch must not apply to it"
        )

        inst = _reader_adapter("/nonexistent")
        assert await inst._frame(dropped) is None, "a dropped sender is settled, not held"
        assert await inst._frame(limited) is None, "a rate-limited sender is settled, not held"
    finally:
        gateway.store.close()


COMMUNITY = "c0" * 32
CHANNEL = "11" * 32


def _refused_channel_route(tmp_path):
    """A channel rule for a room this gateway does not hold: the daemon's `route_unresolved`."""
    routing = {
        "models": ["model-a"],
        "default": {"provider": "p1", "model": "model-a", "key": "GATEWAY_KEY"},
        "channels": [{"community": COMMUNITY, "channel": CHANNEL, "provider": "p3",
                      "model": "model-a", "key": "GATEWAY_KEY"}],
    }
    gateway = _gateway(tmp_path, routing)
    gateway.concord.refreshed = True  # a refresh that found nothing: the room is gone
    msg = _inbound("e5" * 32, f"concord:{COMMUNITY}:{CHANNEL}", "concord", "hello")
    gateway.store.add(msg)
    return gateway, msg


def _refused_profile(tmp_path):
    """A profile deleted after the daemon loaded it: the daemon's `profile_unknown`."""
    home = tmp_path / "hermes"
    (home / "profiles" / "ops").mkdir(parents=True)
    (home / "profiles" / "ops" / ".env").write_text("A=1\n")
    (home / "config.yaml").write_text(yaml.safe_dump({"gateway": {"multiplex_profiles": True}}))
    gateway = _gateway(tmp_path, profiles={"default": "default", "planes": {"marmot": "ops"}},
                       hermes_config=str(home / "config.yaml"))
    msg = _inbound("e5" * 32, "marmot:group", "marmot", "hello")
    gateway.store.add(msg)
    (home / "profiles" / "ops" / ".env").unlink()
    return gateway, msg


#: The refusals that are a condition on the daemon's side: the row stays replayable, so the
#: adapter has to ask again rather than settle the frame.
_DAEMON_REFUSALS = {"route_unresolved": _refused_channel_route,
                    "profile_unknown": _refused_profile}


@pytest.mark.parametrize("reason", sorted(_DAEMON_REFUSALS))
async def test_a_blocked_refusal_whose_row_is_replayable_is_held(tmp_path, monkeypatch, reason):
    """`route_unresolved` and `profile_unknown` are conditions, not verdicts about the message.

    The daemon refuses the turn rather than running it somewhere nobody chose, and keeps the
    row pending so it runs once the room or the profile is back. The adapter has to hold the
    frame: settling it parks the row until something unrelated disconnects, because `subscribe`
    is the only op that offers a pending row again.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    gateway, _msg = _DAEMON_REFUSALS[reason](tmp_path)
    try:
        (refused,) = gateway.replay_pending()
        assert (refused["type"], refused["reason"]) == ("blocked", reason)
        assert refused["final"] is False, "the daemon is still offering this row"
        assert gateway.store.pending_immediate(), "the row is kept for when the condition lifts"

        inst = _reader_adapter("/nonexistent")
        assert await inst._frame(refused) == adapter._HELD_BLOCKED, (
            "an ack would claim Hermes took a message it never saw"
        )
    finally:
        gateway.store.close()


def test_the_final_flag_is_the_rows_lifecycle_at_every_refusal_site(tmp_path, monkeypatch):
    """Every `blocked` shape the daemon can build, checked against the store that replays it.

    `final` may only ever mean one thing — no later `subscribe` offers this row again — so at
    each refusal site the flag has to agree with `Store.pending_immediate`, the query that *is*
    the replay. That is what lets the adapter stop keeping a list of reasons: a refusal added
    at either site is classified by where its row stands, not by a string someone remembered.
    """
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    monkeypatch.setenv("GATEWAY_KEY", "gateway-value")
    monkeypatch.delenv(ROUTE_KEY, raising=False)

    def under(name):
        path = tmp_path / name
        path.mkdir(parents=True, exist_ok=True)
        return path

    # One gateway per branch: the policy branch of `frame_for`, and the routing and profile
    # branches of `_routed` (which the replay path runs again).
    policy = _gateway(under("policy"))
    keyed = _gateway(under("keyed"), _MISSING_KEY_ROUTING)
    keyed_msg = _inbound("a1" * 32, PEER_HEX, "nip17", "held route")
    keyed.store.add(keyed_msg)
    channel, channel_msg = _refused_channel_route(under("channel"))
    profile, profile_msg = _refused_profile(under("profile"))
    gateways = [policy, keyed, channel, profile]

    shapes = {
        "drop": (policy, _inbound("c3" * 32, PEER_HEX, "nip17", "stranger", reach="drop")),
        "blocked_reason": (policy, _inbound("d4" * 32, PEER_HEX, "nip17", "flood",
                                            reach="drop", blocked_reason="rate_limited")),
        "route_key_missing": (keyed, keyed_msg),
        "route_unresolved": (channel, channel_msg),
        "profile_unknown": (profile, profile_msg),
    }
    try:
        for name, (gateway, msg) in shapes.items():
            gateway.store.add(msg)  # the two helpers stored theirs; a duplicate is a no-op
            frame = gateway.frame_for(msg)
            assert frame["type"] == "blocked", name
            replayable = [row["id"] for row in gateway.store.pending_immediate()]
            assert frame["final"] is (msg.id not in replayable), (
                f"{name}: final={frame['final']} but the row is "
                f"{'still' if msg.id in replayable else 'no longer'} in pending_immediate"
            )
            if msg.id in replayable:
                # The replay builds the frame again, and it has to say the same thing.
                replayed = [item for item in gateway.replay_pending()
                            if item["type"] == "blocked"]
                assert replayed and all(item["final"] is False for item in replayed), name
    finally:
        for gateway in gateways:
            gateway.store.close()


async def test_the_hold_follows_the_frame_not_the_reason_string():
    """The reason says what was refused; `final` says whether it comes back. Only one decides.

    Both directions are pinned, because the defect was a list of reasons: a policy-looking
    reason on a row the daemon is still offering is held, and a reason this adapter used to
    hold by name, on a row the daemon has retired, is settled. A frame that carries no `final`
    at all is held too — a daemon too old to know the row's fate is not evidence that the row
    is finished, and one reconnect is cheaper than a parked message.
    """
    inst = _reader_adapter("/nonexistent")

    held = await inst._frame({"type": "blocked", "reason": "not_allowlisted",
                              "rumor_id": "a1" * 32, "final": False})
    assert held == adapter._HELD_BLOCKED, "the row is still coming back, whatever it is called"

    retired = await inst._frame({"type": "blocked", "reason": "route_key_missing",
                                 "rumor_id": "b2" * 32, "final": True})
    assert retired is None, "a row the daemon has retired must not hold the subscription"

    silent = await inst._frame({"type": "blocked", "reason": "not_allowlisted",
                                "rumor_id": "c3" * 32})
    assert silent == adapter._HELD_BLOCKED, (
        "not knowing the row's fate is not knowing it is over"
    )


async def test_a_settled_blocked_frame_does_not_cost_a_resubscribe(tmp_path, monkeypatch):
    """The complement at the socket: a refusal with no replay must not wait for one.

    A policy-blocked row is never offered again, so holding it would replace a healthy
    connection — and pay the backoff — for a frame the daemon cannot produce, while the
    message behind it waits. The batch has to be read straight through instead.
    """
    monkeypatch.setattr(adapter, "_HELD_BATCH_SECONDS", 0.05)
    settled = {"type": "blocked", "plane": "nip17", "from": "cd" * 32,
               "reason": "not_allowlisted", "rumor_id": "ee" * 32, "final": True}
    daemon = ConditionDaemon(_frame())
    daemon.pending.insert(0, settled)  # offered ahead of the message, oldest-first
    server = SocketServer(tmp_path / "settled.sock", daemon)
    await server.start()

    inst = _reader_adapter(tmp_path / "settled.sock")
    delivered = []

    async def handle_message(event):
        delivered.append(event.message_id)

    inst.handle_message = handle_message
    task = asyncio.create_task(inst._reader_loop())
    try:
        await daemon.wait_settled(MESSAGE_ID)
        # Past the hold window: had the refusal been held, the connection would have been
        # replaced by now and the resubscribe would show up in `subscriptions`.
        await asyncio.sleep(0.3)
    finally:
        inst._closing = True
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await server.stop()

    assert delivered == [MESSAGE_ID], "a settled refusal must not stop the frames behind it"
    assert daemon.acks == [MESSAGE_ID], "only the message Hermes took may be acked"
    assert daemon.subscriptions == 1, (
        "a refusal whose row is retired has no replay to wait for: replacing the connection "
        "for it pays the backoff for a frame the daemon cannot offer again"
    )

