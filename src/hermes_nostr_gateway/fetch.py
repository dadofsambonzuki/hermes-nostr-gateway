"""Cold, one-shot event lookups through the bindings.

The daemon's steady state is the relay pool (`pool.py`); these are the queries that have
to happen before any subscription exists or outside the daemon entirely: our kind-3
contact list, a peer's NIP-65 inbox relays, a peer's kind-0 bot flag, an npub decode.

They used to be `nak req -l 1` child processes whose stdout was parsed. Same query, same
answer, but through the library that already signs our events — so the package has no
runtime dependency on an external binary.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import logging
from typing import Any, Iterable, Sequence

import nostr_sdk as sdk

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


def _filter(kinds: Sequence[int], author: str | None, limit: int) -> sdk.Filter:
    filt = sdk.Filter().kinds([sdk.Kind(int(kind)) for kind in kinds]).limit(limit)
    if author:
        filt = filt.author(sdk.PublicKey.parse(hex_of(author)))
    return filt


async def fetch_first(*, relays: Iterable[str], kinds: Sequence[int],
                      author: str | None = None, limit: int = 1,
                      timeout: float = DEFAULT_TIMEOUT_S) -> dict[str, Any] | None:
    """The first event matching `kinds` (and `author`) from any of `relays`, or None.

    One short-lived client, closed in every path: this runs on the cold path (once per
    peer, once per start), so a connection that is not reused is cheaper than one that
    leaks a socket per call.
    """
    urls = [str(url) for url in relays if url]
    if not urls:
        return None
    client = sdk.Client()
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001 - one bad URL must not lose the lookup
                log.debug("fetch: cannot add relay %s", url)
        await client.connect()
        events = await asyncio.wait_for(
            client.fetch_events(
                sdk.ReqTarget.auto([_filter(kinds, author, limit)]),
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
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch_first(**kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(fetch_first(**kwargs))).result()


async def fetch_many(*, relays: Iterable[str], kinds: Sequence[int], author: str | None = None,
                     limit: int = 200, since: int | None = None,
                     timeout: float = 12.0) -> list[dict[str, Any]]:
    """Every event matching the filter, from every relay given (deduped by id)."""
    urls = [str(url) for url in relays if url]
    if not urls:
        return []
    client = sdk.Client()
    out: dict[str, dict[str, Any]] = {}
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001
                continue
        await client.connect()
        filt = _filter(kinds, author, limit)
        if since:
            filt = filt.since(sdk.Timestamp.from_secs(int(since)))
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
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(fetch_many(**kwargs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(fetch_many(**kwargs))).result()


async def stream_events(*, relays: Iterable[str], kinds: Sequence[int], author: str | None,
                        on_event) -> None:
    """Live subscription through the binding, one client for every relay."""
    urls = [str(url) for url in relays if url]
    client = sdk.Client()
    try:
        for url in urls:
            try:
                await client.add_relay(sdk.RelayUrl.parse(url))
            except Exception:  # noqa: BLE001
                continue
        await client.connect()
        await client.subscribe([_filter(kinds, author, 0)], None)
        async for notification in client.notifications():
            event = getattr(notification, "event", None)
            if event is None:
                continue
            try:
                raw = json.loads(event.as_json())
            except Exception:  # noqa: BLE001
                continue
            await on_event(raw)
    finally:
        try:
            await client.shutdown()
        except Exception:  # noqa: BLE001
            pass
