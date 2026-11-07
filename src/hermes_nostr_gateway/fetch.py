"""Cold and streaming reads through the bindings.

The daemon's steady state is the relay pool (`pool.py`); these are the queries that have
to happen before any subscription exists or outside the daemon entirely: our kind-3
contact list, a peer's NIP-65 inbox relays, a peer's kind-0 bot flag, an npub decode —
and the two reads `hng listen` is built from (one bounded fetch, one live subscription).

They used to be `nak` child processes whose stdout was parsed. Same query, same answer,
but through the library that already signs our events — so the package has no runtime
dependency on an external binary, and a lookup that needs NIP-42 authenticates as the
identity rather than as whatever key the child happened to hold.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
import time
from typing import Any, Iterable, Sequence

import nostr_sdk as sdk

from . import sdkloop
from .crypto import parse_secret
from .pool import KeysSigner

log = logging.getLogger("hng.fetch")

DEFAULT_TIMEOUT_S = 8.0


def hex_of(who: str) -> str:
    """An npub or hex key as hex. Pure: bech32 decoding is in the binding."""
    value = (who or "").strip()
    if not value:
        return value
    try:
        return sdk.PublicKey.parse(value).to_hex()
    except Exception:  # noqa: BLE001 - leave whatever we were given untouched
        return value


def _client(secret: str | None = None) -> sdk.Client:
    """A short-lived client, answering NIP-42 as our identity when given a secret.

    A lookup relay that gates reads closes an unauthenticated REQ as `restricted`, which
    reads exactly like a peer who publishes nothing — so the cold paths authenticate the
    same way the daemon does. uniffi calls the async signer from a Rust worker thread,
    where there is no running loop: bind it here or every AUTH attempt fails.
    """
    if not secret:
        return sdk.Client()
    keys = sdk.Keys.parse(parse_secret(secret))
    # `ensure`, not `bind`: this coroutine may be running on the daemon's home loop (when
    # `run_isolated` handed it there) or on a private one (a CLI, a test). Either way the
    # loop it runs on is the one the bindings must resolve its futures on, and a bare
    # `bind` that nobody restores is the shape that leaves a dead loop behind.
    sdkloop.ensure()
    return sdk.ClientBuilder().authenticator(sdk.SignerAuthenticator(KeysSigner(keys))).build()


def _filter(kinds: Sequence[int], author: str | None, limit: int | None,
            p_tag: str | None = None, since: int | None = None,
            a_tag: str | None = None) -> sdk.Filter:
    """One filter: the kinds, an optional author, `#p`, `#a`, cap and `since` floor.

    A limit of 0 or None is omitted rather than sent as `limit: 0`: a live subscription
    wants everything from now on, and some relays read an explicit zero as "nothing".
    `since` is epoch seconds and is what keeps a reconnect from replaying history.
    `a_tag` is NIP-34's addressable-event filter: a repo's issues and PRs are found by the
    repository announcement address, not by an author who may be any contributor.
    """
    filt = sdk.Filter().kinds([sdk.Kind(int(kind)) for kind in kinds])
    if limit:
        filt = filt.limit(int(limit))
    if since:
        filt = filt.since(sdk.Timestamp.from_secs(int(since)))
    if p_tag:
        filt = filt.custom_tag(sdk.SingleLetterTag.from_byte(ord("p")), p_tag)
    if a_tag:
        filt = filt.custom_tag(sdk.SingleLetterTag.from_byte(ord("a")), a_tag)
    if author:
        filt = filt.author(sdk.PublicKey.parse(hex_of(author)))
    return filt


async def fetch_first(*, relays: Iterable[str], kinds: Sequence[int],
                      author: str | None = None, limit: int = 1,
                      timeout: float = DEFAULT_TIMEOUT_S, p_tag: str | None = None,
                      secret: str | None = None) -> dict[str, Any] | None:
    """The first event matching `kinds` (and `author`) from any of `relays`, or None.

    One short-lived client, closed in every path: this runs on the cold path (once per
    peer, once per start), so a connection that is not reused is cheaper than one that
    leaks a socket per call.
    """
    urls = [str(url) for url in relays if url]
    if not urls:
        return None
    client = _client(secret)
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001 - one bad URL must not lose the lookup
                log.debug("fetch: cannot add relay %s", url)
        await client.connect()
        events = await asyncio.wait_for(
            client.fetch_events(
                sdk.ReqTarget.auto([_filter(kinds, author, limit, p_tag)]),
                dt.timedelta(seconds=timeout)),
            timeout + 2)
        for event in events:
            try:
                return json.loads(event.as_json())
            except Exception:  # noqa: BLE001
                continue
    except (asyncio.TimeoutError, TimeoutError):
        log.debug("fetch: timed out after %ss (%s)", timeout, urls[:2])
    except Exception as exc:  # noqa: BLE001 - a cold lookup failing is never fatal
        log.debug("fetch: %s failed: %s", kinds, exc)
    finally:
        try:
            await client.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return None


def fetch_first_blocking(**kwargs: Any) -> dict[str, Any] | None:
    """`fetch_first` for callers that are not async.

    A caller already inside a running loop cannot `asyncio.run`; it gets a worker thread
    and blocks — which is what the subprocess did before this, only without a binary.
    Either way the loop is one-shot: `run_isolated` puts the bindings' global back, so
    the daemon's pump keeps its own loop (see `sdkloop`).
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return sdkloop.run_isolated(fetch_first(**kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: sdkloop.run_isolated(fetch_first(**kwargs))).result()


async def fetch_many(*, relays: Iterable[str], kinds: Sequence[int], author: str | None = None,
                     limit: int = 200, since: int | None = None,
                     timeout: float = 12.0, p_tag: str | None = None,
                     a_tag: str | None = None,
                     secret: str | None = None) -> list[dict[str, Any]]:
    """Every event matching the filter, from every relay given (deduped by id)."""
    urls = [str(url) for url in relays if url]
    if not urls:
        return []
    client = _client(secret)
    out: dict[str, dict[str, Any]] = {}
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001
                continue
        await client.connect()
        filt = _filter(kinds, author, limit, p_tag, since, a_tag)
        events = await asyncio.wait_for(
            client.fetch_events(sdk.ReqTarget.auto([filt]), dt.timedelta(seconds=timeout)),
            timeout + 2)
        for event in events:
            try:
                raw = json.loads(event.as_json())
            except Exception:  # noqa: BLE001
                continue
            out[str(raw.get("id"))] = raw
    except (asyncio.TimeoutError, TimeoutError):
        log.debug("fetch_many: timed out after %ss", timeout)
    except Exception as exc:  # noqa: BLE001
        log.debug("fetch_many failed: %s", exc)
    finally:
        try:
            await client.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return list(out.values())


def fetch_many_blocking(**kwargs: Any) -> list[dict[str, Any]]:
    """`fetch_many` for callers that are not async; one-shot loop, binding restored."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return sdkloop.run_isolated(fetch_many(**kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: sdkloop.run_isolated(fetch_many(**kwargs))).result()


async def fetch_tagged(*, relays: Iterable[str], kinds: Sequence[int], tag: str,
                       values: Sequence[str], limit: int = 50, a_tag: str | None = None,
                       timeout: float = 8.0,
                       secret: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Events matching one tag per value, asked of every relay separately.

    Used where the relay that answered is part of the answer: the label pass has to say
    which relay's verdict it read, and `fetch_many` merges every relay's events into one
    list without saying where they came from. One filter per value (`#E=<pr>` for each PR)
    keeps this to one REQ per relay per kind, and a relay that refuses or times out costs
    only its own events -- the others still answer.
    """
    urls = [str(url) for url in relays if url]
    wanted = [str(value) for value in values if str(value or "").strip()]
    if not urls or not wanted:
        return []
    filters = [_tag_filter(kinds, tag, value, limit, a_tag) for value in wanted]
    client = _client(secret)
    out: list[tuple[str, dict[str, Any]]] = []
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001 - one bad URL must not lose the read
                log.debug("fetch: cannot add relay %s", url)
        await client.connect()
        for url in urls:
            try:
                events = await asyncio.wait_for(
                    client.fetch_events(sdk.ReqTarget.single(sdk.RelayUrl.parse(url), filters),
                                        dt.timedelta(seconds=timeout)),
                    timeout + 2)
            except Exception as exc:  # noqa: BLE001 - one relay down is reported by omission
                log.debug("fetch: %s on %s failed: %s", kinds, url, exc)
                continue
            for event in events:
                try:
                    raw = json.loads(event.as_json())
                except Exception:  # noqa: BLE001
                    continue
                raw["relay"] = url
                out.append((url, raw))
    finally:
        try:
            await client.shutdown()
        except Exception:  # noqa: BLE001
            pass
    return out


def fetch_tagged_blocking(**kwargs: Any) -> list[tuple[str, dict[str, Any]]]:
    """`fetch_tagged` for callers that are not async; one-shot loop, binding restored."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return sdkloop.run_isolated(fetch_tagged(**kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: sdkloop.run_isolated(fetch_tagged(**kwargs))).result()


def _tag_filter(kinds: Sequence[int], tag: str, value: str, limit: int,
                a_tag: str | None = None) -> sdk.Filter:
    """One filter for `#<tag>=<value>`, with the address tag when there is one."""
    filt = sdk.Filter().kinds([sdk.Kind(int(kind)) for kind in kinds])
    if limit:
        filt = filt.limit(int(limit))
    if tag:
        filt = filt.custom_tag(sdk.SingleLetterTag.from_byte(ord(tag)), str(value))
    if a_tag:
        filt = filt.custom_tag(sdk.SingleLetterTag.from_byte(ord("a")), str(a_tag))
    return filt


async def stream_events(*, relays: Iterable[str], kinds: Sequence[int],
                        author: str | None = None, p_tag: str | None = None,
                        since_days: int = 3, on_event,
                        secret: str | None = None) -> None:
    """Live subscription through the binding, one client for every relay given.

    `on_event(relay, event)` is awaited for each new event, with the relay it arrived on:
    the same shape the daemon's workers use, so `hng listen` hands both paths the same
    handler. `since_days` bounds the history a reconnect replays — NIP-59 backdates wraps
    up to two days, so a narrower window hides live messages (spec §3).

    One subscription for the whole relay group is deliberate: `kinds` and `p_tag` are what
    the caller pinned for those relays, and a client per relay would multiply connections
    against the same relay set the daemon already reads.
    """
    urls = [str(url) for url in relays if url]
    if not urls:
        return
    client = _client(secret)
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001 - one bad URL must not lose the stream
                log.debug("stream: cannot add relay %s", url)
        await client.connect()
        window = int(time.time()) - since_days * 86400 if since_days else None
        # `subscribe` takes a ReqTarget, not a filter list: `auto` targets every relay
        # this client holds, which is the group the caller asked for.
        await client.subscribe(sdk.ReqTarget.auto([_filter(kinds, author, None, p_tag, window)]),
                               None)
        # The binding's stream is pulled, not iterated: `ClientNotificationStream.next()`
        # answers None once the client has shut down.
        stream = client.notifications()
        while True:
            notification = await stream.next()
            if notification is None:
                break
            if not getattr(notification, "is_new_event", lambda: False)():
                continue
            event = getattr(notification, "event", None)
            if event is None:
                continue
            try:
                raw = json.loads(event.as_json())
            except Exception:  # noqa: BLE001
                continue
            await on_event(_relay_of(notification, urls), raw)
    finally:
        try:
            await client.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _relay_of(notification: Any, fallback: Sequence[str]) -> str:
    """Which relay delivered this event — the fallback is only for a client that omits it."""
    url = getattr(notification, "relay_url", None)
    return str(url) if url else (fallback[0] if fallback else "")
