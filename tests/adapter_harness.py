"""Load the Hermes plugin's adapter outside Hermes.

The adapter runs inside Hermes, so its ``gateway.*`` imports are not on this venv's path.
Stub those modules before importing it — with the media helpers backed by a real temp
directory, so an attachment test can read the bytes back instead of trusting a mock.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import uuid
from collections.abc import Iterable
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "adapter.py"

#: The cap ``validate_inbound_media_size`` applies. A test can monkeypatch this module's
#: value to force an oversize refusal without allocating 128 MiB.
INBOUND_MEDIA_MAX_BYTES = 128 * 1024 * 1024


def _stub(name: str, **attrs: object) -> None:
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module


class SendResult:
    """Hermes' send result: success, an error string, and whether a retry could help."""

    def __init__(self, success=True, error=None, retryable=False, message_id=None,
                 raw_response=None):
        self.success = success
        self.error = error
        self.retryable = retryable
        self.message_id = message_id
        self.raw_response = raw_response


class Kw:
    """Stand-in for SessionSource/MessageEvent: keyword attributes, nothing else."""

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class MessageType:
    TEXT = "text"
    LOCATION = "location"
    PHOTO = "photo"
    VIDEO = "video"
    AUDIO = "audio"
    VOICE = "voice"
    DOCUMENT = "document"
    STICKER = "sticker"
    COMMAND = "command"


class CachedMedia:
    """What Hermes' ``cache_media_bytes`` returns: a path plus the type it classified."""

    def __init__(self, path: str, media_type: str, kind: str, display_name: str = ""):
        self.path = path
        self.media_type = media_type
        self.kind = kind
        self.display_name = display_name


CACHE_ROOT = Path(tempfile.mkdtemp(prefix="nostr-adapter-cache-"))


async def cache_media_bytes_async(data: bytes, *, filename: str = "", mime_type: str = "",
                                  default_kind: str | None = None) -> CachedMedia:
    """Hermes' media cache, honestly: real bytes on disk, classified by the MIME given."""
    mime = (mime_type or "").lower()
    kind = default_kind or (
        "image" if mime.startswith("image/")
        else "video" if mime.startswith("video/")
        else "audio" if mime.startswith("audio/")
        else "document"
    )
    ext = os.path.splitext(filename)[1].lower()
    path = CACHE_ROOT / f"cached_{uuid.uuid4().hex[:12]}{ext}"
    path.write_bytes(data)
    return CachedMedia(str(path), mime or "application/octet-stream", kind, filename or path.name)


def validate_inbound_media_size(size: int, *, media_type: str = "media",
                                max_bytes: int | None = None) -> None:
    """Hermes' inbound cap: refuse before the bytes are read into the turn."""
    limit = INBOUND_MEDIA_MAX_BYTES if max_bytes is None else max_bytes
    if limit and size > limit:
        raise ValueError(
            f"Inbound {media_type} payload is too large ({size} bytes > {limit} bytes)")


def get_scoped_secret(name: str, default=None, *, external_fallback: bool = False):
    """Hermes' profile-scoped secret reader, in the form a harness can honour.

    The real one reads the active profile's secret scope (``agent.secret_scope``); outside
    Hermes there is no profile, so the process environment *is* that profile's scope. A test
    that needs another answer monkeypatches the loaded adapter's ``_get_scoped_secret``.
    """
    return os.environ.get(name, default)


def load_adapter(name: str = "nostr_adapter"):
    """Import the plugin module against the stubs and return it.

    *name* keeps each test module's copy distinct in ``sys.modules``.
    """
    _stub("gateway")
    _stub("gateway.config", Platform=object, PlatformConfig=object)
    _stub("gateway.platforms")
    _stub("gateway.platforms.base", BasePlatformAdapter=object, SendResult=SendResult,
          SessionSource=Kw, CachedMedia=CachedMedia,
          cache_media_bytes_async=cache_media_bytes_async,
          validate_inbound_media_size=validate_inbound_media_size)
    _stub("gateway.platforms.event", MessageEvent=Kw, MessageType=MessageType)
    # Credentials and settings are read through the host's secret route (catalog rule 11), so
    # the harness has to answer that route rather than let the adapter fall back to `os.environ`.
    _stub("gateway.platforms._shared", get_scoped_secret=get_scoped_secret)
    # The adapter asks Hermes whether a routed profile exists; without the stub that question
    # is unanswerable outside Hermes and every profile would be "not missing".
    stub_profiles()
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: The profiles `hermes_cli.profiles.profile_exists` answers for, in a test's own terms. A
#: module-level set because the stub is installed once, in `sys.modules`, and a test may change
#: its mind between adapters.
KNOWN_PROFILES: set[str] = set()


def stub_profiles(names: Iterable[str] = ()) -> None:
    """Make Hermes' own profile lookup answer for *names* (default: none at all)."""
    KNOWN_PROFILES.clear()
    KNOWN_PROFILES.update(str(name) for name in names or ())

    def profile_exists(name: str) -> bool:
        return str(name).strip() in KNOWN_PROFILES

    _stub("hermes_cli")
    _stub("hermes_cli.profiles", profile_exists=profile_exists,
          get_profile_dir=lambda name: Path(f"/nonexistent/profiles/{name}"))
    # `from hermes_cli.profiles import …` resolves the submodule through its parent.
    sys.modules["hermes_cli"].profiles = sys.modules["hermes_cli.profiles"]  # type: ignore[attr-defined]
