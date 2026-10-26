"""Inbound Blossom media: NIP-92 `imeta` in, verified bytes on disk out.

Attachments ride on the text rumor as `imeta` tags (spec §4b) and are recorded with the
message but **never fetched on receipt**: a download happens only when the adapter asks
for a blob with `media.get`, so a hostile sender cannot spend our bandwidth or disk by
sending a message.

What this module enforces, and why:

* **The hash is the identity.** `x` (the sha256) is the one field a hostile server
  cannot forge past us: bytes are accepted only when they hash to it, and the cache file
  is named by it. A tag without a valid `x` is shown to the agent but can never be
  fetched (`unverifiable`) — an unverifiable blob is just a URL someone typed.
* **The type is sniffed, never believed.** `m` is a claim. The MIME handed to the
  adapter is detected from the bytes, and an unrecognised blob is refused rather than
  guessed, so an attachment cannot arrive labelled an image and be treated as one.
* **Caps apply before and during the download.** A declared size over the cap refuses
  without a request; the streamed byte count is cut off at the cap even when the server
  omits `Content-Length` or lies about it.
* **Only hash-addressed, public, https URLs are followed** for a peer's own server.
  Configured servers are trusted as configured (a local test server may be http and
  loopback); every other URL must end in the digest, resolve to a public address, and
  must not redirect — a redirect is exactly how a checked URL becomes an unchecked one.
* **Nothing is handed over as bytes.** The result is a path plus metadata; downloads are
  never executed, and the cache is a 0700 directory of 0600 files.

The named refusals (`MediaError.code`) are the contract with the adapter: `bad_request`,
`media_disabled`, `unknown_blob`, `unverifiable`, `too_many_files`, `too_large`,
`insecure_url`, `bad_url`, `host_not_allowed`, `not_found`, `fetch_failed`,
`hash_mismatch`, `type_unknown`, `type_not_allowed`.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

log = logging.getLogger("hng.media")

MIB = 1024 * 1024
DEFAULT_MAX_BYTES = 25 * MIB
DEFAULT_MAX_FILES = 4
DEFAULT_MAX_CACHE_BYTES = 512 * MIB
DEFAULT_TIMEOUT = 20.0
#: The day.ag haven serves Blossom on its own vhost; `relay.day.ag` path-routes it too.
DEFAULT_SERVERS: tuple[str, ...] = ("https://blossom.day.ag",)
#: Deliberately inside the directory the systemd unit can write
#: (`ReadWritePaths=…/.local/state/hermes-nostr-gateway`); a cache under `share/` would
#: be read-only at runtime on this host and fail only when the first blob is fetched.
DEFAULT_DIR = Path.home() / ".local/state/hermes-nostr-gateway/media"

_CHUNK = 64 * 1024
_HEX = frozenset("0123456789abcdef")
_ALLOWED_EXACT = frozenset({"video/mp4", "application/pdf", "text/plain"})
_TEXT_BYTES = frozenset({9, 10, 12, 13})


class MediaError(RuntimeError):
    """A refusal with a code the adapter can branch on."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ── configuration ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MediaConfig:
    """`media`: whether inbound media is fetchable, from where, and how big."""

    enabled: bool = True
    servers: tuple[str, ...] = DEFAULT_SERVERS
    dir: Path = DEFAULT_DIR
    max_bytes: int = DEFAULT_MAX_BYTES
    max_files: int = DEFAULT_MAX_FILES
    max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES
    timeout: float = DEFAULT_TIMEOUT

    def fetcher(self) -> "MediaFetcher":
        return MediaFetcher(
            servers=self.servers, cache_dir=self.dir, max_bytes=self.max_bytes,
            max_files=self.max_files, max_cache_bytes=self.max_cache_bytes,
            timeout=self.timeout,
        )


def _count(raw: dict[str, Any], key: str, default: int, *, minimum: int) -> int:
    value = raw.get(key, default)
    # `bool` is an `int` in Python, and `max_bytes: true` is not a cap.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"media.{key} must be a whole number: {value!r}")
    if value < minimum:
        raise ValueError(f"media.{key} must be at least {minimum}: {value!r}")
    return value


def _seconds(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"media.{key} must be a positive number of seconds: {value!r}")
    return float(value)


def media_config(raw: dict[str, Any] | None) -> MediaConfig:
    """Read `media`, loudly: a malformed cap is a config error, not a silent default.

    Keys: `enabled` (true), `blossom_servers` ([blossom.day.ag]), `dir`
    (~/.local/state/hermes-nostr-gateway/media), `max_bytes` (25 MiB),
    `max_files` (4), `max_cache_bytes` (512 MiB), `timeout` (20 s).
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError(f"media must be a mapping: {raw!r}")

    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"media.enabled must be true or false: {enabled!r}")

    servers = raw.get("blossom_servers", list(DEFAULT_SERVERS))
    if isinstance(servers, str):
        servers = [servers]
    if not isinstance(servers, list):
        raise ValueError(f"media.blossom_servers must be a list: {servers!r}")
    cleaned: list[str] = []
    for server in servers:
        url = str(server).strip().rstrip("/")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError(f"media.blossom_servers entry must be an http(s) URL: {server!r}")
        try:
            _ = parts.port
        except ValueError as exc:
            raise ValueError(f"media.blossom_servers entry has a bad port: {server!r}") from exc
        cleaned.append(url)

    directory = raw.get("dir") or DEFAULT_DIR
    return MediaConfig(
        enabled=enabled,
        servers=tuple(dict.fromkeys(cleaned)),
        dir=Path(os.path.expanduser(str(directory))),
        max_bytes=_count(raw, "max_bytes", DEFAULT_MAX_BYTES, minimum=1),
        max_files=_count(raw, "max_files", DEFAULT_MAX_FILES, minimum=0),
        max_cache_bytes=_count(raw, "max_cache_bytes", DEFAULT_MAX_CACHE_BYTES, minimum=1),
        timeout=_seconds(raw, "timeout", DEFAULT_TIMEOUT),
    )


# ── NIP-92 imeta ─────────────────────────────────────────────────────────────

def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(char in _HEX for char in value)


def imeta_attachments(tags: Iterable[Any]) -> list[dict[str, Any]]:
    """`imeta` tags -> attachment metadata, in tag order.

    Every field is the *sender's claim*; `m` and `size` are re-checked against the
    bytes at fetch time. A tag with no `url` references nothing and is skipped; a tag
    with a missing or malformed `x` is kept (the agent can see the sender claimed an
    attachment) but carries no `sha256`, so it can never be fetched.
    """
    out: list[dict[str, Any]] = []
    for tag in tags or []:
        if not isinstance(tag, list) or not tag or tag[0] != "imeta":
            continue
        fields: dict[str, str] = {}
        for item in tag[1:]:
            key, _, value = str(item).partition(" ")
            if key and value.strip():
                fields.setdefault(key, value.strip())
        url = fields.get("url", "")
        if not url:
            continue
        entry: dict[str, Any] = {"url": url}
        digest = fields.get("x", "").lower()
        if digest and _is_digest(digest):
            entry["sha256"] = digest
            entry["status"] = "available"
        else:
            # No usable hash means no verification, so `media.get` will refuse it by
            # name; the adapter can see that from the frame without trying.
            entry["status"] = "unverifiable"
            if digest:
                log.warning("imeta: ignoring malformed sha256 %r", digest[:16])
        for key, field in (("mime", "m"), ("dim", "dim"), ("alt", "alt")):
            if fields.get(field):
                entry[key] = fields[field]
        size = fields.get("size", "")
        if size.isdigit():
            entry["size"] = int(size)
        out.append(entry)
    return out


# ── MIME sniffing (bounded to the allowlist this gateway accepts) ────────────

def mime_allowed(mime: str | None) -> bool:
    value = (mime or "").split(";")[0].strip().lower()
    return value.startswith(("image/", "audio/")) or value in _ALLOWED_EXACT


def sniff_mime(data: bytes) -> str | None:
    """The MIME the bytes actually are, or None when it cannot be told.

    Deliberately a small table, not a database: only the types this gateway accepts are
    recognised, and anything else is refused as unknown rather than guessed at. SVG is
    not recognised — it is byte-identical to text, so it arrives as `text/plain` and is
    never presented to the adapter as an image.
    """
    if not data:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "audio/wav"
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"M4A ", b"M4B "):
            return "audio/mp4"
        # Any other ISO-BMFF brand is a video container; `video/mp4` is the accepted one.
        return "video/mp4"
    if data.startswith(b"%PDF-"):
        return "application/pdf"
    if data.startswith(b"ID3") or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        return "audio/mpeg"
    if data.startswith(b"OggS"):
        return "audio/ogg"
    if data.startswith(b"fLaC"):
        return "audio/flac"
    if data.startswith(b"\x1aE\xdf\xa3"):
        return "video/webm"
    if _looks_like_text(data):
        return "text/plain"
    return None


def _looks_like_text(data: bytes) -> bool:
    sample = data[:8192]
    if b"\x00" in sample:
        return False
    if any(byte < 32 and byte not in _TEXT_BYTES for byte in sample):
        return False
    try:
        sample.decode("utf-8")
    except UnicodeDecodeError:
        # A multi-byte character cut at the sample boundary is not a binary signal.
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            return False
    return True


# ── resolving a stored attachment ────────────────────────────────────────────

def resolve_attachment(store: Any, *, sha256: str | None = None, url: str | None = None,
                       rumor_id: str | None = None) -> dict[str, Any]:
    """The stored attachment row a `media.get` call names, or a named refusal.

    This is the gate that stops the socket being used as a general fetcher: only a blob
    some message we hold actually references can be fetched, so an adapter (or anything
    that can write to the socket) cannot ask for arbitrary URLs.
    """
    digest = (sha256 or "").strip().lower()
    if digest and not _is_digest(digest):
        raise MediaError("bad_request", "sha256 must be 64 hex characters")
    url = (url or "").strip() or None
    rumor_id = (rumor_id or "").strip() or None
    if not (digest or url or rumor_id):
        raise MediaError("bad_request", "media.get needs a sha256, url or rumor_id")

    row: dict[str, Any] | None = None
    if rumor_id:
        rows = [r for r in store.attachment_rows(rumor_id)
                if (not digest or str(r.get("sha256") or "") == digest)
                and (not url or r.get("url") == url)]
        if not rows:
            raise MediaError("unknown_blob",
                             f"message {rumor_id[:12]}… has no such attachment")
        if len(rows) > 1:
            raise MediaError(
                "bad_request",
                f"message {rumor_id[:12]}… has {len(rows)} matching attachments; pass sha256",
            )
        row = rows[0]
    else:
        row = store.find_attachment(sha256=digest or None, url=url)
    if row is None:
        raise MediaError("unknown_blob", "no message we hold references that blob")
    if not row.get("sha256"):
        raise MediaError("unverifiable",
                         "that attachment's imeta tag carries no sha256, so its bytes "
                         "cannot be verified")
    return dict(row)


# ── fetching ─────────────────────────────────────────────────────────────────

class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """A 3xx is an error: following one would escape the URL checks below."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


class MediaFetcher:
    """Downloads one stored attachment, hash-verifies it, and caches it by digest."""

    def __init__(self, *, servers: Iterable[str] = DEFAULT_SERVERS, cache_dir: str | Path,
                 max_bytes: int = DEFAULT_MAX_BYTES, max_files: int = DEFAULT_MAX_FILES,
                 max_cache_bytes: int = DEFAULT_MAX_CACHE_BYTES,
                 timeout: float = DEFAULT_TIMEOUT):
        self.servers = tuple(str(server).strip().rstrip("/")
                             for server in servers if str(server).strip())
        self.cache_dir = Path(cache_dir)
        self.max_bytes = int(max_bytes)
        self.max_files = int(max_files)
        self.max_cache_bytes = int(max_cache_bytes)
        self.timeout = float(timeout)

    # -- public ---------------------------------------------------------
    def fetch(self, entry: dict[str, Any]) -> dict[str, Any]:
        digest = str(entry.get("sha256") or "").lower()
        if not _is_digest(digest):
            raise MediaError("unverifiable", "the attachment carries no usable sha256")
        position = int(entry.get("position") or 0)
        if position >= self.max_files:
            raise MediaError(
                "too_many_files",
                f"attachment {position + 1} exceeds the limit of {self.max_files} per message",
            )
        declared = entry.get("size")
        if isinstance(declared, int) and declared > self.max_bytes:
            raise MediaError(
                "too_large",
                f"the sender declares {declared} bytes, over the {self.max_bytes} byte cap",
            )

        path = self.cache_dir / digest
        cached = self._cached(path, digest)
        if cached is not None:
            mime, data = cached
            return self._result(entry, path, data, mime, cached=True)

        refusal: MediaError | None = None
        for url in self._candidates(entry):
            try:
                data = self._download(url)
            except MediaError as exc:
                refusal = refusal or exc
                log.info("media: %s refused (%s)", url, exc.code)
                continue
            if hashlib.sha256(data).hexdigest() != digest:
                # Another candidate may be a mirror that has the real blob; a wrong body
                # from one server is not proof the blob is unavailable.
                refusal = refusal or MediaError(
                    "hash_mismatch", f"{url}: the bytes do not hash to the requested sha256")
                log.info("media: %s refused (hash_mismatch)", url)
                continue
            # Hash matched, so these bytes *are* the blob: a type refusal is definitive
            # and asking another mirror would only download the same bytes again.
            mime = sniff_mime(data)
            if mime is None:
                raise MediaError("type_unknown",
                                 f"{url}: the bytes are not a media type this gateway knows")
            if not mime_allowed(mime):
                raise MediaError("type_not_allowed", f"{url}: {mime} is outside the allowlist")
            stored = self._store(data, digest)
            return self._result(entry, stored, data, mime, cached=False, url=url)
        # `_candidates` yields at least one URL or raises, so this is hygiene, not a path.
        if refusal is None:  # pragma: no cover
            raise MediaError("fetch_failed", "no candidate URL could be used")
        raise refusal

    # -- cache ----------------------------------------------------------
    def _cached(self, path: Path, digest: str) -> tuple[str, bytes] | None:
        """A verified cache hit, or None after clearing a file that no longer matches."""
        try:
            size = path.stat().st_size
        except OSError:
            return None
        if size > self.max_bytes:
            path.unlink(missing_ok=True)
            return None
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            log.warning("media: cache file %s no longer matches its digest; refetching", path)
            path.unlink(missing_ok=True)
            return None
        mime = sniff_mime(data)
        if mime is None or not mime_allowed(mime):
            path.unlink(missing_ok=True)
            return None
        return mime, data

    def _store(self, data: bytes, digest: str) -> Path:
        self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(self.cache_dir, 0o700)
        except OSError:  # pragma: no cover - the directory is ours and 0700 by makedirs
            pass
        self._make_room(len(data))
        tmp = self.cache_dir / f".{digest}.part"
        tmp.write_bytes(data)
        os.chmod(tmp, 0o600)
        final = self.cache_dir / digest
        os.replace(tmp, final)
        return final

    def _make_room(self, size: int) -> None:
        """Evict oldest files until this blob fits the cache budget.

        The budget is a bound on disk, not a promise the blob fits: a single blob over
        `max_cache_bytes` still lands, with everything else evicted, because `max_bytes`
        is the per-blob cap and refusing a fetch the message already passed would be
        surprising. Nothing else may make the cache grow without bound.
        """
        if self.max_cache_bytes <= 0:
            return
        try:
            files = sorted(
                (p for p in self.cache_dir.iterdir()
                 if p.is_file() and not p.name.startswith(".")),
                key=lambda p: p.stat().st_mtime,
            )
        except OSError:  # pragma: no cover - directory was just created
            return
        total = sum(p.stat().st_size for p in files)
        while files and total + size > self.max_cache_bytes:
            victim = files.pop(0)
            total -= victim.stat().st_size
            log.info("media: evicting %s (cache over %d bytes)", victim.name[:12], self.max_cache_bytes)
            victim.unlink(missing_ok=True)

    # -- sources --------------------------------------------------------
    def _candidates(self, entry: dict[str, Any]) -> list[str]:
        """Where to look for the blob: the message's own url first, then our mirrors."""
        digest = str(entry["sha256"]).lower()
        urls: list[str] = []
        refusal: MediaError | None = None
        message_url = str(entry.get("url") or "")
        if message_url:
            try:
                self._check_url(message_url, digest)
                urls.append(message_url)
            except MediaError as exc:
                refusal = exc
        for server in self.servers:
            candidate = f"{server}/{digest}"
            if candidate not in urls:
                urls.append(candidate)
        if not urls:
            raise refusal or MediaError("no_source",
                                        "no Blossom server is configured and the message "
                                        "has no usable url")
        return urls

    def _check_url(self, url: str, digest: str) -> None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https"):
            raise MediaError("bad_url", f"{url}: only http(s) Blossom URLs are fetched")
        if not parts.hostname:
            raise MediaError("bad_url", f"{url}: no host")
        try:
            _ = parts.port
        except ValueError as exc:
            raise MediaError("bad_url", f"{url}: malformed port") from exc
        if self._trusted(parts):
            return
        if parts.scheme != "https":
            raise MediaError("insecure_url", f"{parts.hostname}: a peer's blob must be https")
        last = parts.path.rstrip("/").rsplit("/", 1)[-1].lower()
        if last != digest and last.split(".")[0] != digest:
            raise MediaError("bad_url",
                             f"{url}: the last path segment is not the blob's sha256")
        if not _public_host(parts.hostname):
            raise MediaError("host_not_allowed",
                             f"{parts.hostname}: not a public address (private, loopback "
                             "or unresolved)")

    def _trusted(self, parts: Any) -> bool:
        """Is this URL one of the configured servers? Compares origins, not prefixes."""
        origin = _origin(parts)
        return any(_origin(urlsplit(server)) == origin for server in self.servers)

    # -- transport ------------------------------------------------------
    def _download(self, url: str) -> bytes:
        opener = urllib.request.build_opener(_NoRedirects())
        request = urllib.request.Request(
            url, headers={"User-Agent": "hermes-nostr-gateway/0.1 (media.get)"}
        )
        try:
            with opener.open(request, timeout=self.timeout) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise MediaError("fetch_failed", f"{url}: HTTP {status}")
                length = response.headers.get("Content-Length")
                if length and length.isdigit() and int(length) > self.max_bytes:
                    raise MediaError(
                        "too_large",
                        f"{url}: {length} bytes is over the {self.max_bytes} byte cap",
                    )
                hasher = hashlib.sha256()
                chunks: list[bytes] = []
                total = 0
                while chunk := response.read(_CHUNK):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise MediaError(
                            "too_large",
                            f"{url}: the body is over the {self.max_bytes} byte cap",
                        )
                    hasher.update(chunk)
                    chunks.append(chunk)
                return b"".join(chunks)
        except urllib.error.HTTPError as exc:
            code = "not_found" if exc.code == 404 else "fetch_failed"
            raise MediaError(code, f"{url}: HTTP {exc.code}") from exc
        except MediaError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise MediaError("fetch_failed", f"{url}: {exc}") from exc

    # -- shaping --------------------------------------------------------
    @staticmethod
    def _result(entry: dict[str, Any], path: Path, data: bytes, mime: str, *,
                cached: bool, url: str | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "sha256": str(entry["sha256"]).lower(),
            "path": str(path),
            "mime": mime,
            "size": len(data),
            "cached": cached,
        }
        if entry.get("rumor_id"):
            out["rumor_id"] = entry["rumor_id"]
        if url is None and entry.get("url"):
            url = str(entry["url"])
        if url:
            out["url"] = url
        if entry.get("mime"):
            out["mime_declared"] = entry["mime"]
        for key in ("dim", "alt"):
            if entry.get(key):
                out[key] = entry[key]
        return out


def _origin(parts: Any) -> tuple[str, str, int]:
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:  # a malformed configured port must not match anything
        return scheme, host, -1
    port = port or (443 if scheme == "https" else 80 if scheme == "http" else 0)
    return scheme, host, port


def _public_host(host: str) -> bool:
    """False for loopback, private, link-local, reserved or unresolvable names."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        return literal.is_global
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {str(info[4][0]).split("%")[0] for info in infos}
    if not addresses:
        return False
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return False
        except ValueError:  # pragma: no cover - getaddrinfo only returns addresses
            return False
    return True
