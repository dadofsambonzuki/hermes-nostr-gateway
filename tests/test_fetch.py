"""The read filters `hng listen` is built from: the `#p` scope, the window, the wiring.

`--once` and the live stream are the two paths a human uses to dogfood the gateway, and
both are wrong in the same silent way if the filter is: a kind-1059 subscription without
`#p: me` is a firehose of every wrap on the relay, and a window inside the NIP-59
backdate hides live messages. So the filter and the callback shape are pinned here rather
than only through the CLI's argument passing.
"""

from __future__ import annotations

import json

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway import fetch

ME = "a" * 64


def spec_of(*args, **kwargs) -> dict:
    return json.loads(fetch._filter(*args, **kwargs).as_json())


def test_a_p_scoped_filter_asks_for_the_tag_not_the_author():
    assert spec_of([1059], None, 50, ME) == {"kinds": [1059], "limit": 50, "#p": [ME]}


def test_a_limit_is_omitted_rather_than_sent_as_zero():
    """A live subscription wants everything from now on; `limit: 0` reads as "nothing"."""
    assert "limit" not in spec_of([1059], None, None, ME)


class FakeEvent:
    def __init__(self, payload: dict):
        self._payload = payload

    def as_json(self) -> str:
        return json.dumps(self._payload)


class FakeNotification:
    def __init__(self, *, event: FakeEvent | None = None, relay: str = "wss://relay.example"):
        self.event = event
        self.relay_url = relay

    def is_new_event(self) -> bool:
        return self.event is not None


class FakeStream:
    """The client's notification channel, pulled with `next()` like the binding's stream."""

    def __init__(self, items):
        self._items = list(items)

    async def next(self):
        return self._items.pop(0) if self._items else None


class FakeClient:
    """Enough of `sdk.Client` for the two read paths: no sockets, recorded calls."""

    def __init__(self, *, notifications=(), events=()):
        self.notifications_in = list(notifications)
        self.events = list(events)
        self.subscribed: list[sdk.ReqTarget] = []
        self.fetched: list[list] = []
        self.shutdowns = 0

    async def add_relay(self, url) -> bool:
        return True

    async def connect(self, and_wait=None) -> None:
        pass

    async def subscribe(self, target, id=None) -> None:
        # The binding validates this: a list of filters is a TypeError at run time, which
        # a fake that accepts anything would hide until the first live `hng listen`.
        assert isinstance(target, sdk.ReqTarget), type(target)
        self.subscribed.append(target)

    async def fetch_events(self, target, timeout):
        self.fetched.append(target)
        return list(self.events)

    def notifications(self) -> FakeStream:
        return FakeStream(self.notifications_in)

    async def shutdown(self) -> None:
        self.shutdowns += 1


async def test_stream_events_hands_each_event_over_with_the_relay_it_came_from(monkeypatch):
    client = FakeClient(notifications=[
        FakeNotification(event=FakeEvent({"id": "e" * 64}), relay="wss://one.example"),
        FakeNotification(),  # a CLOSED/AUTH frame is not an event
    ])
    monkeypatch.setattr(fetch, "_client", lambda _secret=None: client)
    seen: list[tuple[str, dict]] = []

    async def on_event(relay, event):
        seen.append((relay, event))

    await fetch.stream_events(relays=["wss://one.example"], kinds=[1059], p_tag=ME,
                              on_event=on_event)

    assert seen == [("wss://one.example", {"id": "e" * 64})]
    assert client.shutdowns == 1, "the client must be closed on the way out"


async def test_stream_events_subscribes_once_for_the_whole_relay_group(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(fetch, "_client", lambda _secret=None: client)
    seen: dict = {}
    real_filter = fetch._filter

    def record(*args, **kwargs):
        filt = real_filter(*args, **kwargs)
        seen["spec"] = json.loads(filt.as_json())
        return filt

    monkeypatch.setattr(fetch, "_filter", record)

    async def on_event(relay, event):
        raise AssertionError("no event should have arrived")

    await fetch.stream_events(relays=["wss://one.example", "wss://two.example"],
                              kinds=[1059], p_tag=ME, on_event=on_event)

    assert len(client.subscribed) == 1, "a client per relay multiplies connections"
    assert seen["spec"]["#p"] == [ME]
    assert seen["spec"]["since"] > 0, "without a window every reconnect replays the relay's history"


async def test_fetch_many_dedupes_the_same_event_from_two_relays(monkeypatch):
    event = FakeEvent({"id": "f" * 64, "kind": 1059, "tags": []})
    client = FakeClient(events=[event, event])
    monkeypatch.setattr(fetch, "_client", lambda _secret=None: client)

    published = await fetch.fetch_many(relays=["wss://one.example", "wss://two.example"],
                                       kinds=[1059], p_tag=ME, limit=10, since=1000)

    assert [row["id"] for row in published] == ["f" * 64]
    assert client.shutdowns == 1


async def test_fetch_many_asks_for_our_inbox_inside_the_since_window(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(fetch, "_client", lambda _secret=None: client)
    seen: dict = {}
    real_filter = fetch._filter

    def record(*args, **kwargs):
        filt = real_filter(*args, **kwargs)
        seen["spec"] = json.loads(filt.as_json())
        return filt

    monkeypatch.setattr(fetch, "_filter", record)
    await fetch.fetch_many(relays=["wss://one.example"], kinds=[1059], p_tag=ME,
                           limit=10, since=1000)

    assert seen["spec"] == {"kinds": [1059], "limit": 10, "#p": [ME], "since": 1000}


@pytest.mark.parametrize("relays", [[], [""]])
async def test_no_relays_means_no_client_and_no_lookup(monkeypatch, relays):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a lookup with no relays must not build a client")

    monkeypatch.setattr(fetch, "_client", refuse)

    async def on_event(relay, event):
        raise AssertionError("no event should have arrived")

    assert await fetch.fetch_many(relays=relays, kinds=[1059]) == []
    assert await fetch.fetch_first(relays=relays, kinds=[1059]) is None
    await fetch.stream_events(relays=relays, kinds=[1059], on_event=on_event)


# -- one tag per value, and the relay that answered it ------------------------

ADDRESS = f"30617:{'ee' * 32}:example-repo"


def test_a_tagged_filter_carries_the_value_and_the_repository():
    spec = json.loads(fetch._tag_filter([1111], "E", "ab" * 32, 50, ADDRESS).as_json())
    assert spec == {"kinds": [1111], "limit": 50, "#E": ["ab" * 32], "#a": [ADDRESS]}


async def test_fetch_tagged_asks_each_relay_separately_and_keeps_its_answer(monkeypatch):
    event = FakeEvent({"id": "a" * 64, "kind": 1111, "tags": []})
    client = FakeClient(events=[event])
    monkeypatch.setattr(fetch, "_client", lambda _secret=None: client)

    rows = await fetch.fetch_tagged(relays=["wss://one.example", "wss://two.example"],
                                    kinds=[1111], tag="E", values=["ab" * 32, "cd" * 32])

    assert [relay for relay, _ in rows] == ["wss://one.example", "wss://two.example"]
    assert [event["relay"] for _, event in rows] == ["wss://one.example", "wss://two.example"]
    assert len(client.fetched) == 2, "one REQ per relay, not one per relay per value"
    assert client.shutdowns == 1


async def test_fetch_tagged_with_nothing_to_ask_does_not_build_a_client(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a read with no values must not build a client")

    monkeypatch.setattr(fetch, "_client", refuse)

    assert await fetch.fetch_tagged(relays=["wss://one.example"], kinds=[1111],
                                    tag="E", values=[]) == []
