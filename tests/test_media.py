"""Inbound media end to end: an `imeta` claim becomes verified bytes on disk.

The acceptance criterion this file exists for (QUEUE.md, M3): an inbound DM with an
attachment yields bytes the adapter can read, and an oversized or unknown blob is
refused with a named reason. The Blossom server here is a real HTTP server on loopback,
because a mocked transport cannot prove a download cap: the cap has to be enforced while
reading a socket that keeps sending.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import nostr_sdk as sdk
import pytest

from hermes_nostr_gateway.crypto import SdkCrypto, build_rumor
from hermes_nostr_gateway.gateway import Gateway
from hermes_nostr_gateway.media import (
    MediaError,
    MediaFetcher,
    imeta_attachments,
    resolve_attachment,
    sniff_mime,
)
from hermes_nostr_gateway.store import Inbound, Store

ME = "aa" * 32
PEER = "bb" * 32
PNG = b"\x89PNG\r\n\x1a\n" + b"pixels"


# ── a real Blossom-shaped server on loopback ─────────────────────────────────

class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # noqa: D102 - silence the cap test
        pass


class FakeBlossom:
    """Serves `blobs[path]` over HTTP; records every hit so caching is testable."""

    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.hits: list[str] = []
        self.omit_length = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 - http.server's spelling
                owner.hits.append(self.path)
                body = owner.blobs.get(self.path)
                if body is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                if not owner.omit_length:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # noqa: D102 - no request logging in tests
                pass

        self.server = _Server(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def blossom():
    server = FakeBlossom()
    try:
        yield server
    finally:
        server.close()


def seed(store: Store, *, blob: bytes, url: str, mime: str = "image/png",
         size: int | None = None, content: str = "a photo", rumor_id: str = "r1") -> dict:
    digest = hashlib.sha256(blob).hexdigest()
    entry = {"url": url, "sha256": digest, "mime": mime,
             "size": len(blob) if size is None else size}
    assert store.add(Inbound(
        id=rumor_id, wrapper_id=f"w-{rumor_id}", sender=PEER, kind=14,
        created_at=1_700_000_000, content=content, conversation=PEER, reply_to=None,
        attachments=[entry],
    ))
    return store.attachment_rows(rumor_id)[0]


def fetcher(tmp_path, blossom=None, **kwargs) -> MediaFetcher:
    return MediaFetcher(servers=[blossom.base] if blossom else [],
                        cache_dir=tmp_path / "cache", **kwargs)


# ── imeta parsing ────────────────────────────────────────────────────────────

def test_an_imeta_tag_becomes_attachment_metadata():
    digest = "ab" * 32
    (entry,) = imeta_attachments([[
        "imeta", f"url https://blossom.example/{digest}", "m image/jpeg",
        f"x {digest}", "size 1234", "dim 800x600", "alt a cat on a keyboard",
    ]])
    assert entry == {
        "url": f"https://blossom.example/{digest}", "sha256": digest, "mime": "image/jpeg",
        "dim": "800x600", "alt": "a cat on a keyboard", "size": 1234,
        "status": "available",
    }


def test_an_imeta_without_a_hash_is_kept_but_not_fetchable():
    (entry,) = imeta_attachments([["imeta", "url https://blossom.example/x", "m image/png"]])
    assert "sha256" not in entry
    assert entry["status"] == "unverifiable"
    assert entry["url"] == "https://blossom.example/x"


def test_a_malformed_hash_is_dropped_rather_than_trusted():
    (entry,) = imeta_attachments([["imeta", "url https://blossom.example/x", "x not-a-hash"]])
    assert "sha256" not in entry
    assert entry["status"] == "unverifiable"


def test_tags_that_are_not_imeta_are_ignored():
    assert imeta_attachments([["e", "abc"], ["p", "def"]]) == []


# ── MIME sniffing ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("blob,mime", [
    (PNG, "image/png"),
    (b"\xff\xd8\xff\xe0" + b"jpg", "image/jpeg"),
    (b"GIF89a" + b"gif", "image/gif"),
    (b"%PDF-1.7\n" + b"pdf", "application/pdf"),
    (b"ID3" + b"mp3", "audio/mpeg"),
    (b"plain words\n", "text/plain"),
])
def test_sniffing_names_the_type_from_the_bytes(blob, mime):
    assert sniff_mime(blob) == mime


def test_unknown_bytes_are_not_guessed():
    assert sniff_mime(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 32) is None
    assert sniff_mime(b"") is None


def test_a_webm_video_is_recognised_but_outside_the_allowlist():
    assert sniff_mime(b"\x1aE\xdf\xa3" + b"0" * 32) == "video/webm"


# ── resolving what a message references ──────────────────────────────────────

def test_a_digest_no_message_references_is_unknown(tmp_path):
    store = Store(tmp_path / "t.db", identity=ME)
    with pytest.raises(MediaError) as caught:
        resolve_attachment(store, sha256="cd" * 32)
    assert caught.value.code == "unknown_blob"


def test_an_attachment_without_a_hash_cannot_be_fetched(tmp_path):
    store = Store(tmp_path / "t.db", identity=ME)
    store.add(Inbound(
        id="r1", wrapper_id="w1", sender=PEER, kind=14, created_at=1_700_000_000,
        content="look", conversation=PEER, reply_to=None,
        attachments=[{"url": "https://blossom.example/x", "mime": "image/png"}],
    ))
    with pytest.raises(MediaError) as caught:
        resolve_attachment(store, url="https://blossom.example/x")
    assert caught.value.code == "unverifiable"


def test_media_get_without_a_reference_is_a_bad_request(tmp_path):
    store = Store(tmp_path / "t.db", identity=ME)
    with pytest.raises(MediaError) as caught:
        resolve_attachment(store)
    assert caught.value.code == "bad_request"


def test_an_ambiguous_rumor_id_needs_a_digest(tmp_path):
    store = Store(tmp_path / "t.db", identity=ME)
    first = hashlib.sha256(b"one").hexdigest()
    second = hashlib.sha256(b"two").hexdigest()
    store.add(Inbound(
        id="r1", wrapper_id="w1", sender=PEER, kind=14, created_at=1_700_000_000,
        content="two photos", conversation=PEER, reply_to=None,
        attachments=[{"url": f"https://b/{first}", "sha256": first},
                     {"url": f"https://b/{second}", "sha256": second}],
    ))
    with pytest.raises(MediaError) as caught:
        resolve_attachment(store, rumor_id="r1")
    assert caught.value.code == "bad_request"
    (entry,) = [store.attachment_rows("r1")[1]]
    assert resolve_attachment(store, rumor_id="r1", sha256=entry["sha256"])["sha256"] == second


# ── fetching, caps and refusals ──────────────────────────────────────────────

def test_a_referenced_blob_arrives_as_verified_bytes_then_caches(tmp_path, blossom):
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}")

    first = fetcher(tmp_path, blossom).fetch(entry)
    assert first["cached"] is False and first["mime"] == "image/png"
    assert first["size"] == len(blob) and first["sha256"] == digest
    assert Path(first["path"]).read_bytes() == blob
    assert Path(first["path"]).stat().st_mode & 0o777 == 0o600

    second = fetcher(tmp_path, blossom).fetch(entry)
    assert second["cached"] is True and second["path"] == first["path"]
    assert blossom.hits == [f"/{digest}"]  # the bytes were pulled once


def test_a_declared_size_over_the_cap_is_refused_before_any_request(tmp_path, blossom):
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}", size=30 * 1024 * 1024)

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path, blossom).fetch(entry)
    assert caught.value.code == "too_large"
    assert blossom.hits == []
    assert not (tmp_path / "cache" / digest).exists()


def test_a_server_that_omits_length_is_cut_off_mid_stream(tmp_path, blossom):
    blossom.omit_length = True
    blob = PNG + b"x" * 4096
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    store = Store(tmp_path / "t.db", identity=ME)
    # The sender claims one byte: only the streamed check can catch this.
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}", size=1)

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path, blossom, max_bytes=1024).fetch(entry)
    assert caught.value.code == "too_large"
    assert blossom.hits == [f"/{digest}"]
    assert not (tmp_path / "cache" / digest).exists()
    assert not (tmp_path / "cache" / f".{digest}.part").exists()


def test_bytes_that_do_not_hash_to_the_digest_are_refused(tmp_path, blossom):
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = b"\x89PNG\r\n\x1a\nsomething else entirely"
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path, blossom).fetch(entry)
    assert caught.value.code == "hash_mismatch"
    assert not (tmp_path / "cache" / digest).exists()


def test_a_blob_of_an_unknown_type_is_refused_not_guessed(tmp_path, blossom):
    blob = b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 64
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}", mime="image/png")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path, blossom).fetch(entry)
    assert caught.value.code == "type_unknown"
    assert blossom.hits == [f"/{digest}"]  # the bytes are read to sniff, never kept
    assert not (tmp_path / "cache" / digest).exists()


def test_a_type_outside_the_allowlist_is_refused(tmp_path, blossom):
    blob = b"\x1aE\xdf\xa3" + b"0" * 64
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"{blossom.base}/{digest}", mime="video/webm")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path, blossom).fetch(entry)
    assert caught.value.code == "type_not_allowed"


def test_a_peer_url_must_be_https(tmp_path):
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"http://example.com/{digest}")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path).fetch(entry)
    assert caught.value.code == "insecure_url"


def test_a_peer_url_must_not_point_at_a_private_address(tmp_path):
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url=f"https://127.0.0.1/{digest}")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path).fetch(entry)
    assert caught.value.code == "host_not_allowed"


def test_a_peer_url_must_be_hash_addressed(tmp_path):
    blob = PNG
    store = Store(tmp_path / "t.db", identity=ME)
    entry = seed(store, blob=blob, url="https://example.com/not-the-hash")

    with pytest.raises(MediaError) as caught:
        fetcher(tmp_path).fetch(entry)
    assert caught.value.code == "bad_url"


def test_the_cache_budget_evicts_the_oldest_blob(tmp_path, blossom):
    first_blob = PNG + b"a" * 200
    second_blob = PNG + b"b" * 200
    first, second = (hashlib.sha256(b).hexdigest() for b in (first_blob, second_blob))
    blossom.blobs[f"/{first}"] = first_blob
    blossom.blobs[f"/{second}"] = second_blob
    store = Store(tmp_path / "t.db", identity=ME)
    one = seed(store, blob=first_blob, url=f"{blossom.base}/{first}", content="one")
    two = seed(store, blob=second_blob, url=f"{blossom.base}/{second}", content="two",
               rumor_id="r2")

    fetch = fetcher(tmp_path, blossom, max_cache_bytes=300)
    fetch.fetch(one)
    fetch.fetch(two)
    cached = sorted(p.name for p in (tmp_path / "cache").iterdir())
    assert cached == [second]
    assert not (tmp_path / "cache" / first).exists()


# ── over the socket, where the adapter meets it ──────────────────────────────

def _config(tmp_path, blossom, peer_hex: str, *, media: dict | None = None) -> dict:
    me = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    secret_file = tmp_path / "secret.json"
    secret_file.write_text(json.dumps({"secret_key_hex": me.keys.secret_key().to_hex()}))
    cfg = {
        "identity": {"secret_file": str(secret_file)},
        "relays": {"read": [], "publish": [], "peer_inbox": {"enabled": False}},
        "store": str(tmp_path / "gateway.db"),
        "socket": str(tmp_path / "gateway.sock"),
        "groups": {"family": [peer_hex]},
        "policies": [{"name": "family-dms", "match": {"group": "family", "dm": True},
                      "reach": "immediate", "authority": "instructions"}],
        "defaults": {"reach": "drop", "authority": "none"},
        "media": media if media is not None else {
            "blossom_servers": [blossom.base], "dir": str(tmp_path / "media"),
        },
    }
    return cfg


async def _call(path, frame: dict) -> dict:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write((json.dumps(frame) + "\n").encode())
        await writer.drain()
        return json.loads(await asyncio.wait_for(reader.readline(), 3))
    finally:
        writer.close()
        await writer.wait_closed()


async def _next_message(reader) -> dict:
    while True:
        frame = json.loads(await asyncio.wait_for(reader.readline(), 3))
        if frame.get("type") == "message":
            return frame


async def test_an_inbound_dm_with_an_attachment_yields_bytes_over_the_socket(
    tmp_path, blossom, monkeypatch,
):
    """The queue's done-condition, through the same socket the Hermes adapter reads."""
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    peer = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    blob = PNG
    digest = hashlib.sha256(blob).hexdigest()
    blossom.blobs[f"/{digest}"] = blob
    gateway = Gateway(_config(tmp_path, blossom, peer.our_pubkey()))
    try:
        await gateway.start()
        reader, writer = await asyncio.open_unix_connection(str(gateway.socket_path))
        writer.write(b'{"op":"subscribe"}\n')
        await writer.drain()
        assert json.loads(await asyncio.wait_for(reader.readline(), 3))["type"] == "subscribed"

        rumor = build_rumor(peer.our_pubkey(), gateway.me, "look at this", tags=[[
            "imeta", f"url {blossom.base}/{digest}", "m image/png", f"x {digest}",
            f"size {len(blob)}", "alt the garden",
        ]])
        gateway._on_event("relay", peer.wrap(rumor, gateway.me))

        frame = await _next_message(reader)
        assert frame["attachments"] == [{
            "url": f"{blossom.base}/{digest}", "sha256": digest, "mime": "image/png",
            "size": len(blob), "alt": "the garden", "status": "available",
        }]

        fetched = await _call(gateway.socket_path, {"op": "media.get", "sha256": digest})
        assert fetched["type"] == "media" and fetched["mime"] == "image/png"
        assert Path(fetched["path"]).read_bytes() == blob

        by_message = await _call(gateway.socket_path,
                                 {"op": "media.get", "rumor_id": frame["id"]})
        assert by_message["type"] == "media" and by_message["cached"] is True

        unknown = await _call(gateway.socket_path,
                              {"op": "media.get", "sha256": "cd" * 32})
        assert unknown == {
            "type": "error", "op": "media.get", "code": "unknown_blob",
            "error": "no message we hold references that blob",
        }
        assert blossom.hits == [f"/{digest}"]
        writer.close()
        await writer.wait_closed()
    finally:
        await gateway.stop()


async def test_media_can_be_switched_off(tmp_path, blossom, monkeypatch):
    monkeypatch.setattr(Gateway, "_load_follows", lambda self: ())
    peer = SdkCrypto(sdk.Keys.generate().secret_key().to_hex())
    gateway = Gateway(_config(tmp_path, blossom, peer.our_pubkey(),
                              media={"enabled": False}))
    try:
        reply = await gateway.media_get(sha256="cd" * 32)
        assert reply["type"] == "error" and reply["code"] == "media_disabled"
    finally:
        await gateway.stop()
