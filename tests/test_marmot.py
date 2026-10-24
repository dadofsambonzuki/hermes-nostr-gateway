"""Marmot plane: the control-socket wire, normalisation, policy and the proxy itself.

The fake below is a stand-in for wn-agent, not a mock of our own code: it speaks the
real `marmot.agent-control.v2` envelopes over a real Unix socket, so a change in the
protocol shape fails here rather than in production. What is *not* proven here is
MLS — that stays wn-agent's, and this plane never sees a key.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from hermes_nostr_gateway.planes.marmot import (
    APP_MESSAGE_KIND,
    MarmotControl,
    MarmotError,
    MarmotPlane,
    MarmotResolver,
    MarmotWorker,
    PROTOCOL,
    resolve_account_id,
)
from hermes_nostr_gateway.policy import Policy
from hermes_nostr_gateway.store import Store

ACCOUNT = "740fcd907b10beedb9530aecf8cde87b274edd4706eafce108aa4fbb45cd2e29"
GROUP = "fcc995ef9affe55608ff3fe9f917de51"
OTHER_GROUP = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
MUM = "c4f5e7a75a8ce3683d529cff06368439c529e5243c6b125ba68789198856cac7"
STRANGER = "ab" * 32

POLICY = {
    "groups": {"family": [MUM]},
    "policies": [
        {"name": "family-marmot", "match": {"group": "family", "kinds": [APP_MESSAGE_KIND]},
         "reach": "immediate", "authority": "instructions"},
        {"name": "anyone-marmot", "match": {"group": "any", "kinds": [APP_MESSAGE_KIND]},
         "reach": "digest(15m)", "authority": "none"},
    ],
    "defaults": {"reach": "digest(15m)", "authority": "none"},
}


class FakeWnAgent:
    """wn-agent's control socket, reduced to the calls this plane makes."""

    def __init__(self, path: str | Path, *, account_id_hex: str = ACCOUNT):
        self.path = Path(path)
        self.account_id_hex = account_id_hex
        self.requests: list[dict] = []
        self.subscribers: list[tuple[str, asyncio.Queue]] = []
        self.subscribed = asyncio.Event()
        self.send_fail = False
        self.group_info_fail = False
        self.timeline_rows: list[dict] = []
        self._server: asyncio.AbstractServer | None = None
        self._stamp = 1_790_000_000

    async def start(self) -> "FakeWnAgent":
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        return self

    async def restart(self) -> None:
        """Re-listen on the same path: asyncio leaves a stale socket file behind."""
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def stop(self) -> None:
        # Wake any live subscription *before* waiting for the server to close: in
        # 3.13 `wait_closed()` waits for every handler, and a handler parked on an
        # empty queue is exactly what a live subscription looks like.
        for _, queue in list(self.subscribers):
            queue.put_nowait(None)
        self.subscribed.clear()
        server, self._server = self._server, None
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        self.subscribers.clear()

    async def push(self, *, sender: str = MUM, text: str = "hello from marmot",
                   group: str = GROUP, message_id: str | None = None,
                   is_self: bool = False, reply_to: str | None = None,
                   recorded_at: int | None = None, mentions_self: bool = False) -> dict:
        self._stamp += 1
        message = {
            "message_id_hex": message_id or os.urandom(32).hex(),
            "sender": {"account_id_hex": sender, "is_self": is_self},
            "kind": APP_MESSAGE_KIND,
            "recorded_at": recorded_at or self._stamp,
            "text": text,
        }
        event = {
            "type": "inbound_message",
            "account_id_hex": self.account_id_hex,
            "group_id_hex": group,
            "message": message,
            "reply_to": ({"message_id_hex": reply_to} if reply_to else None),
            "mentions_self": mentions_self,
        }
        for _, queue in list(self.subscribers):
            queue.put_nowait(event)
        return message

    # -- connection handling ---------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            raw = await reader.readline()
            if not raw:
                return
            request = json.loads(raw)
            self.requests.append(request)
            if request.get("marmot_agent_control") != PROTOCOL:
                await self._send(writer, {"type": "error", "code": "wrong_protocol",
                                          "message": "bad protocol"}, request)
                return
            kind = request.get("type")
            if kind == "subscribe_inbound":
                await self._subscribe(reader, writer, request)
            elif kind == "account_list":
                await self._send(writer, {"type": "account_list", "accounts": [
                    {"account_id_hex": self.account_id_hex, "label": "hermes-agent"}]}, request)
            elif kind == "group_info":
                if self.group_info_fail:
                    await self._send(writer, {"type": "error", "code": "group_not_found",
                                              "message": "no such group"}, request)
                else:
                    await self._send(writer, {
                        "type": "group_info", "group_id_hex": request.get("group_id_hex"),
                        "account_id_hex": self.account_id_hex, "agent_created": False,
                        "member_count": 2, "is_direct": True}, request)
            elif kind == "timeline_list":
                await self._send(writer, {"type": "timeline_page",
                                          "messages": self.timeline_rows}, request)
            elif kind == "send_final":
                if self.send_fail:
                    await self._send(writer, {"type": "error", "code": "send_in_progress",
                                              "message": "still sending",
                                              "retryable": True}, request)
                else:
                    await self._send(writer, {
                        "type": "final_sent",
                        "message_ids_hex": [os.urandom(32).hex()]}, request)
            else:
                await self._send(writer, {"type": "error", "code": "unsupported",
                                          "message": f"unknown {kind!r}"}, request)
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    async def _subscribe(self, reader: asyncio.StreamReader,
                         writer: asyncio.StreamWriter, request: dict) -> None:
        queue: asyncio.Queue = asyncio.Queue()
        self.subscribers.append((request["id"], queue))
        self.subscribed.set()
        await self._send(writer, {"type": "ack"}, request)
        closed = asyncio.ensure_future(reader.read(1))
        pending = asyncio.ensure_future(queue.get())
        try:
            while True:
                done, _ = await asyncio.wait({closed, pending},
                                             return_when=asyncio.FIRST_COMPLETED)
                if closed in done:
                    return
                event = pending.result()
                if event is None:
                    return
                await self._send(writer, event, request)
                pending = asyncio.ensure_future(queue.get())
        finally:
            closed.cancel()
            pending.cancel()
            self.subscribers = [pair for pair in self.subscribers if pair[1] is not queue]

    @staticmethod
    async def _send(writer: asyncio.StreamWriter, payload: dict, request: dict) -> None:
        envelope = {"marmot_agent_control": PROTOCOL, "id": request["id"], **payload}
        writer.write((json.dumps(envelope) + "\n").encode())
        await writer.drain()


async def wait_until(predicate, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


@pytest.fixture
async def agent(tmp_path):
    fake = await FakeWnAgent(tmp_path / "wn-agent.sock").start()
    yield fake
    await fake.stop()


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "marmot.db") as opened:
        yield opened


def plane_for(store, agent, policy: Policy | None = None) -> MarmotPlane:
    return MarmotPlane(store, policy or Policy(POLICY),
                       control=MarmotControl(agent.path), account_id_hex=ACCOUNT)


# -- inbound normalisation -------------------------------------------------


async def test_inbound_message_is_normalised_and_policy_applied(store, agent):
    plane = plane_for(store, agent)
    message = {
        "message_id_hex": "11" * 32, "sender": {"account_id_hex": MUM, "is_self": False},
        "kind": 9, "recorded_at": 1_790_000_123, "text": "dinner at seven",
        "reply_to_message_id_hex": "22" * 32,
    }
    msg = plane.handle({"type": "inbound_message", "account_id_hex": ACCOUNT,
                        "group_id_hex": GROUP, "message": message})
    assert msg is not None
    frame = msg.to_frame()
    assert frame["plane"] == "marmot"
    assert frame["conversation"] == GROUP
    assert frame["sender"] == MUM
    assert frame["kind"] == APP_MESSAGE_KIND
    assert frame["content"] == "dinner at seven"
    assert frame["reply_to"] == "22" * 32
    assert (frame["reach"], frame["authority"]) == ("immediate", "instructions")
    assert "family-marmot" in frame["why"]
    assert store.recent(10)[0]["id"] == "11" * 32


async def test_our_own_echo_is_never_delivered(store, agent):
    plane = plane_for(store, agent)
    message = {"message_id_hex": "33" * 32,
               "sender": {"account_id_hex": ACCOUNT, "is_self": True},
               "recorded_at": 1_790_000_200, "text": "my own words"}
    assert plane.handle({"type": "inbound_message", "group_id_hex": GROUP,
                         "message": message}) is None
    assert store.recent(10) == []


async def test_a_repeated_message_id_is_stored_once(store, agent):
    plane = plane_for(store, agent)
    event = {"type": "inbound_message", "group_id_hex": GROUP,
             "message": {"message_id_hex": "44" * 32,
                         "sender": {"account_id_hex": MUM},
                         "recorded_at": 1_790_000_300, "text": "twice"}}
    assert plane.handle(event) is not None
    assert plane.handle(dict(event)) is None
    assert len(store.recent(10)) == 1


async def test_a_stranger_is_stored_but_only_digests_without_authority(store, agent):
    plane = plane_for(store, agent)
    event = {"type": "inbound_message", "group_id_hex": GROUP,
             "message": {"message_id_hex": "55" * 32,
                         "sender": {"account_id_hex": STRANGER},
                         "recorded_at": 1_790_000_400, "text": "buy my token"}}
    msg = plane.handle(event)
    assert msg is not None
    assert (msg.reach, msg.authority) == ("digest", "none")
    assert msg.why.startswith("matched anyone-marmot")


async def test_non_message_control_events_are_ignored(store, agent):
    plane = plane_for(store, agent)
    assert plane.handle({"type": "resync_required", "dropped_events": 2}) is None
    assert plane.handle({"type": "group_invite", "group_id_hex": GROUP}) is None


# -- outbound --------------------------------------------------------------


async def test_send_records_the_outgoing_row(store, agent):
    plane = plane_for(store, agent)
    result = await plane.send(GROUP, "on my way", reply_to="66" * 32,
                              idempotency_key="key-1")
    assert result["group"] == GROUP
    assert len(result["message_id"]) == 64
    sent = [r for r in agent.requests if r.get("type") == "send_final"]
    assert sent and sent[0]["group_id_hex"] == GROUP
    assert sent[0]["reply_to_message_id_hex"] == "66" * 32
    assert sent[0]["idempotency_key"] == "key-1"
    rows = store.recent(5)
    assert rows[0]["authority"] == "self" and rows[0]["plane"] == "marmot"
    assert rows[0]["content"] == "on my way"
    assert rows[0]["delivered_at"] is not None


async def test_send_error_is_reported_not_raised_at_the_socket(store, agent):
    plane = plane_for(store, agent)
    agent.send_fail = True
    with pytest.raises(MarmotError) as exc:
        await plane.send(GROUP, "nope")
    assert exc.value.code == "send_in_progress" and exc.value.retryable


async def test_empty_text_is_refused_before_touching_the_socket(store, agent):
    plane = plane_for(store, agent)
    with pytest.raises(MarmotError):
        await plane.send(GROUP, "  ")
    assert not [r for r in agent.requests if r.get("type") == "send_final"]


# -- resolver --------------------------------------------------------------


async def test_resolver_classifies_targets_without_a_lookup(store, agent):
    resolver = MarmotResolver(plane_for(store, agent))
    assert resolver.parse(GROUP) == GROUP
    assert resolver.parse(f"marmot:{GROUP}") == GROUP
    assert resolver.parse(f"marmot:{GROUP.upper()}") == GROUP
    assert resolver.parse("marmot:") is None
    # A Nostr pubkey is 64 hex, a group id 32: no ambiguity to guess about.
    assert resolver.parse(MUM) is None
    assert resolver.parse("npub1xyz") is None
    assert resolver.parse("not-hex-at-all-but-32-chars-long!") is None


async def test_resolver_verifies_once_then_caches(store, agent):
    resolver = MarmotResolver(plane_for(store, agent))
    first = await resolver.resolve(GROUP)
    assert first["member_count"] == 2 and first["is_direct"] is True
    await resolver.resolve(f"marmot:{GROUP}")
    assert len([r for r in agent.requests if r.get("type") == "group_info"]) == 1
    assert resolver.conversations()[0]["group"] == GROUP


async def test_resolver_reports_an_unknown_group(store, agent):
    resolver = MarmotResolver(plane_for(store, agent))
    agent.group_info_fail = True
    with pytest.raises(MarmotError) as exc:
        await resolver.resolve(OTHER_GROUP)
    assert exc.value.code == "group_not_found"


async def test_resolver_refuses_a_non_group_target(store, agent):
    resolver = MarmotResolver(plane_for(store, agent))
    with pytest.raises(MarmotError) as exc:
        await resolver.resolve(MUM)
    assert exc.value.code == "invalid_target"


# -- worker ----------------------------------------------------------------


async def test_worker_delivers_a_pushed_message_and_reports_state(store, agent):
    plane = plane_for(store, agent)
    worker = MarmotWorker(plane, base_delay=0.01, max_delay=0.05)
    received: list[dict] = []

    async def on_message(event: dict) -> None:
        received.append(event)

    task = asyncio.create_task(worker.run(on_message))
    try:
        await wait_until(lambda: worker.connected)
        await agent.push(text="first")
        await wait_until(lambda: received)
    finally:
        worker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    stats = worker.stats()
    assert stats["events"] == 1 and stats["connected"] is False
    assert stats["account"] == ACCOUNT
    assert received[0]["message"]["text"] == "first"


async def test_worker_reconnects_after_the_subscription_drops(store, agent, tmp_path):
    plane = plane_for(store, agent)
    worker = MarmotWorker(plane, base_delay=0.01, max_delay=0.05)
    received: list[dict] = []

    async def on_message(event: dict) -> None:
        received.append(event)

    task = asyncio.create_task(worker.run(on_message))
    try:
        await wait_until(lambda: worker.connected)
        await agent.stop()
        await wait_until(lambda: not worker.connected)
        await agent.restart()
        await wait_until(lambda: worker.reconnects >= 1)
        await wait_until(lambda: worker.connected)
        await agent.push(text="after reconnect")
        await wait_until(lambda: received)
    finally:
        worker.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert [event["message"]["text"] for event in received] == ["after reconnect"]
    assert worker.last_error is not None  # the drop was recorded, not swallowed


# -- the wire itself -------------------------------------------------------


async def test_wrong_protocol_envelope_is_refused(tmp_path):
    server = await asyncio.start_unix_server(
        lambda r, w: _wrong_protocol(r, w), path=str(tmp_path / "wrong.sock"))
    try:
        control = MarmotControl(tmp_path / "wrong.sock", request_timeout=2)
        with pytest.raises(MarmotError) as exc:
            await control.request({"type": "account_list"})
        assert exc.value.code == "wrong_protocol"
    finally:
        server.close()
        await server.wait_closed()


async def _wrong_protocol(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    await reader.readline()
    writer.write((json.dumps({"marmot_agent_control": "marmot.agent-control.v1",
                              "id": "x", "type": "account_list"}) + "\n").encode())
    await writer.drain()
    writer.close()


async def test_a_missing_socket_is_a_retryable_failure(tmp_path):
    control = MarmotControl(tmp_path / "nobody.sock", request_timeout=1)
    with pytest.raises(MarmotError) as exc:
        await control.request({"type": "account_list"})
    assert exc.value.code == "socket_io" and exc.value.retryable


async def test_subscribe_ack_is_required_before_events(store, agent):
    control = MarmotControl(agent.path, request_timeout=2)
    seen: list[dict] = []
    acknowledged: list[bool] = []

    async def pump() -> None:
        async for event in control.subscribe(account_id_hex=ACCOUNT,
                                             on_ack=lambda: acknowledged.append(True)):
            seen.append(event)
            break

    task = asyncio.create_task(pump())
    await wait_until(lambda: agent.subscribed.is_set())
    await agent.push(text="after ack")
    await asyncio.wait_for(task, timeout=2)
    assert acknowledged == [True]
    assert seen[0]["message"]["text"] == "after ack"


# -- account discovery -----------------------------------------------------


def test_account_id_comes_from_the_daemons_own_file(tmp_path):
    account_dir = tmp_path / "accounts/hermes-agent"
    account_dir.mkdir(parents=True)
    (account_dir / "account.json").write_text(json.dumps({"account_id_hex": ACCOUNT}))
    assert resolve_account_id(tmp_path) == ACCOUNT
    assert resolve_account_id(tmp_path, "hermes-agent") == ACCOUNT


def test_two_accounts_without_a_label_is_an_error_not_a_guess(tmp_path):
    for label in ("one", "two"):
        directory = tmp_path / "accounts" / label
        directory.mkdir(parents=True)
        (directory / "account.json").write_text(json.dumps({"account_id_hex": ACCOUNT}))
    with pytest.raises(MarmotError) as exc:
        resolve_account_id(tmp_path)
    assert exc.value.code == "ambiguous_account"
