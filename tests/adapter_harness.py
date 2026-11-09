"""Load the Hermes plugin's adapter outside Hermes.

The adapter runs inside Hermes, so its ``gateway.*`` imports are not on this venv's path.
Stub those modules before importing it — with the media helpers backed by a real temp
directory, so an attachment test can read the bytes back instead of trusting a mock.
"""

from __future__ import annotations

import importlib.util
import os
import re
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


class ApprovalPrompt:
    """Hermes' ``ExecApprovalPrompt``: the card body, the choices, and the request it answers.

    Built here rather than imported so a test can shape the host's half: the choices a smart
    deny removes, and a host release that names no request id at all (``request_id=None``).
    """

    def __init__(self, chat_id: str, session_key: str, *, text: str = "Run this command?",
                 actions=None, request_id: str | None = "req-1", metadata=None) -> None:
        self.chat_id = chat_id
        self.session_key = session_key
        self.text = text
        self.actions = list(actions) if actions is not None else [
            ("Approve once", "once", "primary"), ("Approve for session", "session", ""),
            ("Always approve", "always", ""), ("Deny", "deny", "danger"),
        ]
        self.request_id = request_id
        self.metadata = metadata or {}
        self.command = "rm -rf /tmp/x"
        self.description = "destructive"
        self.smart_denied = False

    @property
    def choices(self) -> list[str]:
        return [choice for _label, choice, _style in self.actions]


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
    # The adapter asks Hermes whether a routed profile exists, through the public
    # `hermes_constants`; without the stub that question is unanswerable outside Hermes and
    # every profile would be "not missing".
    stub_profiles()
    spec = importlib.util.spec_from_file_location(name, PLUGIN)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


#: The named profiles Hermes' public predicates call live, in a test's own terms. A module-level
#: set because the stub is installed once, in `sys.modules`, and a test may change its mind
#: between adapters.
KNOWN_PROFILES: set[str] = set()


def stub_profiles(names: Iterable[str] = (), *, tombstoned: Iterable[str] = ()) -> None:
    """Make `hermes_constants` answer as Hermes' own profile predicates do.

    Hermes decides "there is such a profile" itself, and `hermes_constants` is its public half
    of that answer: `named_profile_is_live` is a directory with identity and no tombstone, and
    `PROFILE_ID_RE` is the id grammar `-p` validates against. The stub keeps the split — a temp
    home carrying `profiles/<name>/` for the live names, and a bare directory for the ones a
    delete tombstoned — so a test can see that the adapter asks the host's question rather than
    inventing a second answer.
    """
    KNOWN_PROFILES.clear()
    KNOWN_PROFILES.update(str(name) for name in names or ())
    dead = {str(name) for name in tombstoned or ()}
    root = Path(tempfile.mkdtemp(prefix="hermes-home-"))
    (root / "profiles" / ".deleted").mkdir(parents=True, exist_ok=True)
    for name in KNOWN_PROFILES | dead:
        if name != "default":
            (root / "profiles" / name).mkdir(exist_ok=True)

    def get_default_hermes_root() -> Path:
        return root

    def named_profile_is_live(profile_home) -> bool:
        path = Path(profile_home)
        return path.is_dir() and path.name in KNOWN_PROFILES

    _stub("hermes_constants", PROFILE_ID_RE=re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$"),
          get_default_hermes_root=get_default_hermes_root,
          named_profile_is_live=named_profile_is_live)
