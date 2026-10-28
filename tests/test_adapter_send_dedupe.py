"""A replayed reply must not become a second delivery.

Hermes' delivery ledger re-sends a reply it could not prove landed, wrapped in a
"Recovered reply" marker, and the daemon can have published a send whose reply frame never
reached this process. Both are at-least-once behaviours that are right to keep, so the
adapter is where a replay is made harmless: it remembers what it put on the wire and drops an
identical send to the same chat.

The evidence this file pins:

* a burst of identical sends delivers exactly one message;
* two different replies in the same second both deliver;
* the same reply after the window delivers again (the window is a parameter, not a constant);
* a genuinely lost reply is still redelivered once, and only once.

The adapter runs inside Hermes, so ``adapter_harness`` stubs its ``gateway.*`` imports.
"""
import asyncio
from collections import deque

import pytest

from adapter_harness import load_adapter

adapter = load_adapter("nostr_adapter_dedupe")

CHAT = "ab" * 32
OTHER = "cd" * 32
BODY = "Sit rep: the queue is live and the worker is scheduled."
MARKER = adapter.RECOVERY_MARKER


def recovered(body=BODY) -> str:
    """What Hermes' ledger sends when it redelivers a reply after a restart."""
    return (f"{MARKER} \u2014 the gateway restarted during delivery, so this may be a "
            f"duplicate:\n\n{body}")


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _no_configured_channels(monkeypatch):
    """The chat under test is a peer, not the home or control channel."""
    monkeypatch.delenv("NOSTR_HOME_CHANNEL", raising=False)
    monkeypatch.delenv("NOSTR_CONTROL_CHANNEL", raising=False)
    monkeypatch.delenv("NOSTR_DEDUPE_WINDOW_S", raising=False)


def _adapter(clock=None, *, window_s: float = adapter.DEFAULT_DEDUPE_WINDOW_S):
    """An adapter whose transport records payloads instead of touching a socket."""
    inst = object.__new__(adapter.NostrGatewayAdapter)
    inst.platform = "nostr"
    inst._peers = {CHAT, OTHER}
    inst._delivered_ids = deque(maxlen=512)
    inst._config = None
    inst._delivery_dedupe = adapter.DeliveryDedupe(window_s=window_s, clock=clock or Clock())
    inst.sent = []

    async def _request(payload, timeout=30.0):
        inst.sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": f"m{len(inst.sent)}"}

    inst._request = _request
    return inst


# -- the incident: five sends, one delivery --------------------------------------

async def test_five_sends_of_one_content_deliver_exactly_once():
    """The 22:52 restart replay: five identical sends in twelve seconds, one message."""
    inst = _adapter()
    results = [await inst.send(CHAT, BODY) for _ in range(5)]

    assert len(inst.sent) == 1
    assert [result.success for result in results] == [True] * 5
    assert [result.raw_response.get("skipped") for result in results] == [
        None, "duplicate", "duplicate", "duplicate", "duplicate"]


async def test_five_recovered_sends_deliver_exactly_once_with_the_marker_intact():
    """The replay arrives already wrapped; the first one is a real redelivery, the rest are not."""
    inst = _adapter()
    results = [await inst.send(CHAT, recovered()) for _ in range(5)]

    assert len(inst.sent) == 1
    assert inst.sent[0]["text"] == recovered()  # the marker went out with the one send
    assert all(result.success for result in results)
    assert [result.raw_response.get("skipped") for result in results[1:]] == ["duplicate"] * 4


async def test_a_racing_duplicate_is_suppressed_before_the_first_send_returns():
    """Each attempt looks new until it settles, so the claim happens before the first await."""
    inst = _adapter()
    started, release = asyncio.Event(), asyncio.Event()

    async def _request(payload, timeout=30.0):
        inst.sent.append(payload)
        started.set()
        await release.wait()
        return {"type": "delivery", "ok": True, "message_id": "m1"}

    inst._request = _request
    first = asyncio.create_task(inst.send(CHAT, BODY))
    await started.wait()
    second = await inst.send(CHAT, BODY)
    release.set()
    first_result = await first

    assert len(inst.sent) == 1
    assert first_result.success
    assert second.raw_response.get("skipped") == "duplicate"
    assert "in flight" in second.raw_response.get("reason")


# -- the window is a dial --------------------------------------------------------

async def test_two_different_contents_in_the_same_second_both_deliver():
    clock = Clock()
    inst = _adapter(clock=clock)

    first = await inst.send(CHAT, "the queue is live")
    second = await inst.send(CHAT, "and so is the worker")

    assert clock.now == 1000.0  # same second, same window
    assert [payload["text"] for payload in inst.sent] == ["the queue is live",
                                                          "and so is the worker"]
    assert first.success and second.success


async def test_the_same_content_after_the_window_delivers_again():
    """'ping' twice is a repeat, not a replay: once the window has passed, it goes."""
    clock = Clock()
    inst = _adapter(clock=clock)

    assert (await inst.send(CHAT, "ping")).success
    clock.now += adapter.DEFAULT_DEDUPE_WINDOW_S - 1
    assert (await inst.send(CHAT, "ping")).raw_response.get("skipped") == "duplicate"
    assert len(inst.sent) == 1

    clock.now += 2  # now past the window
    assert (await inst.send(CHAT, "ping")).success
    assert [payload["text"] for payload in inst.sent] == ["ping", "ping"]


async def test_the_window_is_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("NOSTR_DEDUPE_WINDOW_S", "5")
    assert adapter.dedupe_window_seconds(None) == 5.0
    monkeypatch.setenv("NOSTR_DEDUPE_WINDOW_S", "0")
    assert adapter.dedupe_window_seconds(None) == 0.0
    monkeypatch.setenv("NOSTR_DEDUPE_WINDOW_S", "not-a-number")
    assert adapter.dedupe_window_seconds(None) == adapter.DEFAULT_DEDUPE_WINDOW_S
    monkeypatch.delenv("NOSTR_DEDUPE_WINDOW_S")
    assert adapter.dedupe_window_seconds(None) == adapter.DEFAULT_DEDUPE_WINDOW_S


async def test_a_zero_window_leaves_ordinary_chat_alone():
    """0 disables the windowed rule; only the recovered-reply rule is left."""
    inst = _adapter(window_s=0)
    for _ in range(3):
        assert (await inst.send(CHAT, BODY)).success
    assert len(inst.sent) == 3

    assert (await inst.send(CHAT, recovered("another reply"))).success
    assert (await inst.send(CHAT, recovered("another reply"))).raw_response.get("skipped") == "duplicate"
    assert len(inst.sent) == 4


# -- a recovered reply: tried once, then never again -----------------------------

async def test_a_recovered_send_after_the_window_is_still_suppressed():
    """Its own text says it has already been attempted: age does not make it new."""
    clock = Clock()
    inst = _adapter(clock=clock)

    assert (await inst.send(CHAT, recovered())).success
    clock.now += adapter.DEFAULT_DEDUPE_WINDOW_S * 10
    assert (await inst.send(CHAT, recovered())).raw_response.get("skipped") == "duplicate"
    assert len(inst.sent) == 1


async def test_a_recovered_send_of_a_body_already_delivered_is_suppressed():
    """The plain send landed; replaying the same body under the marker would duplicate it."""
    inst = _adapter()
    assert (await inst.send(CHAT, BODY)).success

    replay = await inst.send(CHAT, recovered())
    assert replay.raw_response.get("skipped") == "duplicate"
    assert len(inst.sent) == 1


async def test_a_genuinely_lost_reply_is_still_redelivered_once():
    """A fresh process (a restart) has no memory, so recovery gets its one attempt."""
    before = _adapter()
    assert (await before.send(CHAT, BODY)).success
    assert len(before.sent) == 1

    after_restart = _adapter()  # new process, empty memory
    assert (await after_restart.send(CHAT, recovered())).success
    assert len(after_restart.sent) == 1
    assert after_restart.sent[0]["text"] == recovered()

    assert (await after_restart.send(CHAT, recovered())).raw_response.get("skipped") == "duplicate"
    assert len(after_restart.sent) == 1


# -- a refusal is recorded, not silently retried ---------------------------------

async def test_a_refused_destination_is_not_replayed():
    """A refusal is remembered, so the recovery path cannot turn it into a second refusal."""
    inst = _adapter()
    inst._peers.clear()  # nothing has written here: nothing may be addressed

    refused = await inst.send(CHAT, recovered())
    assert refused.success is True
    assert refused.retryable is False
    assert refused.raw_response == {"skipped": "unknown_destination", "delivered": False}
    assert inst.sent == []

    replay = await inst.send(CHAT, recovered())
    assert replay.raw_response.get("skipped") == "duplicate"
    assert inst.sent == []


async def test_a_refusal_expires_with_the_window():
    """A peer that writes in becomes addressable, so a refusal is not permanent."""
    clock = Clock()
    inst = _adapter(clock=clock)
    inst._peers.clear()

    assert (await inst.send(CHAT, BODY)).raw_response.get("skipped") == "unknown_destination"
    clock.now += adapter.DEFAULT_DEDUPE_WINDOW_S + 1
    inst._peers.add(CHAT)  # the peer wrote in

    assert (await inst.send(CHAT, BODY)).success
    assert [payload["text"] for payload in inst.sent] == [BODY]


# -- an unknown outcome never suppresses the retry -------------------------------

async def test_a_failed_send_does_not_suppress_its_retry():
    """The daemon may not have published it: the retry must be allowed to carry the text."""
    inst = _adapter()
    calls = []

    async def _request(payload, timeout=30.0):
        calls.append(payload)
        raise ConnectionError("gateway closed the connection without a reply")

    inst._request = _request
    first = await inst.send(CHAT, BODY)
    assert first.success is False and first.retryable is True

    async def _ok(payload, timeout=30.0):
        inst.sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "m1"}

    inst._request = _ok
    assert (await inst.send(CHAT, BODY)).success
    assert len(inst.sent) == 1


async def test_a_per_relay_failure_does_not_suppress_its_retry():
    inst = _adapter()

    async def _no_relay(payload, timeout=30.0):
        return {"type": "delivery", "ok": False, "errors": {"wss://relay.example": "timeout"}}

    inst._request = _no_relay
    first = await inst.send(CHAT, BODY)
    assert first.success is False and first.retryable is True

    async def _ok(payload, timeout=30.0):
        inst.sent.append(payload)
        return {"type": "delivery", "ok": True, "message_id": "m1"}

    inst._request = _ok
    assert (await inst.send(CHAT, BODY)).success
    assert len(inst.sent) == 1


# -- the key is (chat, content) --------------------------------------------------

async def test_the_same_content_to_two_chats_both_deliver():
    inst = _adapter()
    assert (await inst.send(CHAT, BODY)).success
    assert (await inst.send(OTHER, BODY)).success
    assert [payload["to"] for payload in inst.sent] == [CHAT, OTHER]


async def test_a_chat_id_differing_only_in_case_is_the_same_chat():
    inst = _adapter()
    assert (await inst.send(CHAT, BODY)).success
    assert (await inst.send(CHAT.upper(), BODY)).raw_response.get("skipped") == "duplicate"
    assert len(inst.sent) == 1


async def test_two_identical_approval_prompts_both_send():
    """Two gates with the same wording are two questions; dropping one blocks the host."""
    inst = _adapter()
    first = await inst.send(CHAT, "Allow this command?", metadata={"is_approval_prompt": True})
    second = await inst.send(CHAT, "Allow this command?", metadata={"is_approval_prompt": True})

    assert len(inst.sent) == 2
    assert first.success and second.success


# -- the memory is bounded, and a hung claim is not forever ----------------------

def test_the_memory_keeps_only_the_newest_entries():
    dedupe = adapter.DeliveryDedupe(window_s=60.0, max_entries=2)
    for body in ("one", "two", "three"):
        assert dedupe.claim(CHAT, body, recovered=False) is None
        dedupe.settle(CHAT, body, delivered=True)
    assert len(dedupe) == 2


def test_a_hung_send_stops_blocking_the_chat_after_the_send_timeout():
    clock = Clock()
    dedupe = adapter.DeliveryDedupe(window_s=60.0, clock=clock)

    assert dedupe.claim(CHAT, BODY, recovered=False) is None
    assert dedupe.claim(CHAT, BODY, recovered=False)  # in flight
    clock.now += adapter.SEND_TIMEOUT_S + 1  # the attempt never settled
    assert dedupe.claim(CHAT, BODY, recovered=False) is None


# -- the marker parser -----------------------------------------------------------

def test_a_recovered_marker_is_recognised_and_its_body_recovered():
    assert adapter.is_recovery_send(recovered()) is True
    assert adapter.reply_body(recovered()) == BODY
    assert adapter.is_recovery_send(BODY) is False
    assert adapter.reply_body(BODY) == BODY


def test_ordinary_text_that_mentions_recovery_is_left_alone():
    for text in ("Recovered reply is the bug I mean",
                 "the gateway restarted during delivery, so this may be a duplicate: yes"):
        assert adapter.is_recovery_send(text) is False
        assert adapter.reply_body(text) == text
