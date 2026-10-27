"""The control pane: where a confirmation goes, and whose word needs none.

A gateway can act on what it is told, so anything consequential is confirmed first. Both
halves of that are configuration, and both are per plane:

```yaml
control:
  concord: concord:<community>:<channel>   # a Concord channel: where #ops lives
  nip17: npub1...                          # a DM peer that answers confirmations
  marmot: <group id>                       # a White Noise group
  trusted_npubs:                           # trusted everywhere, whatever the plane
    - npub1...
  planes:
    concord:
      trusted_npubs:                       # trusted here as well, not instead
        - npub1...
```

Rules:

- A plane with no `control` entry has no destination, and `target()` says so rather than
  guessing one: a confirmation that cannot be delivered must not be silently dropped.
- `trusted_npubs` is the global list; a plane's own list is a union, not an override, so
  adding a key for one plane never removes the keys trusted everywhere.
- Keys are normalised to hex, so an `npub` in the config matches the hex a frame carries.
  An entry that is not a key is kept in `problems` and reported, never ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import nostr_sdk as sdk

log = logging.getLogger("hng.control")

#: The planes a gateway knows how to ask through. Not a closed set: a new plane only
#: needs a destination string here and a caller that can deliver to it.
PLANES: tuple[str, ...] = ("nip17", "marmot", "concord")


def as_hex(who: str) -> str:
    """A pubkey as lowercase hex, or "" if it is not one (npub or hex).

    Case is normalised first: bech32 is defined as all-lower or all-upper, and the SDK
    only parses the lower form, so an upper-case npub pasted from a client would otherwise
    read as "not a key".
    """
    text = str(who or "").strip().lower()
    if not text:
        return ""
    try:
        return sdk.PublicKey.parse(text).to_hex().lower()
    except Exception:  # noqa: BLE001 - a bad key is a config problem, not a crash
        return ""


def _keys(values: Any) -> tuple[set[str], list[str]]:
    """Normalise a list of npubs/hex; return (hex set, the entries that were not keys)."""
    out: set[str] = set()
    bad: list[str] = []
    if values is None:
        return out, bad
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, Iterable):
        return out, [str(values)]
    for value in values:
        hexed = as_hex(value)
        if hexed:
            out.add(hexed)
        else:
            bad.append(str(value))
    return out, bad


@dataclass(frozen=True)
class ControlConfig:
    """Where confirmations go, and who does not need to be asked."""

    #: plane -> destination string, only for planes that configured one.
    targets: dict[str, str] = field(default_factory=dict)
    #: hex pubkeys trusted in every plane.
    trusted: frozenset[str] = frozenset()
    #: plane -> hex pubkeys trusted there in addition to the global list.
    per_plane: dict[str, frozenset[str]] = field(default_factory=dict)
    #: config entries that could not be used, for the operator to see.
    problems: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, raw: Any) -> "ControlConfig":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            return cls(problems=(f"control: expected a mapping, got {type(raw).__name__}",))
        targets: dict[str, str] = {}
        for plane in PLANES:
            value = raw.get(plane)
            if isinstance(value, str) and value.strip():
                targets[plane.lower()] = value.strip()
            elif value not in (None, ""):
                log.warning("control.%s is not a destination string: %r", plane, value)
        trusted, bad = _keys(raw.get("trusted_npubs"))
        per_plane: dict[str, frozenset[str]] = {}
        planes = raw.get("planes") or {}
        if isinstance(planes, dict):
            for plane, block in planes.items():
                if not isinstance(block, dict):
                    bad.append(f"planes.{plane}: expected a mapping")
                    continue
                extra, bad_keys = _keys(block.get("trusted_npubs"))
                bad.extend(f"planes.{plane}: {entry}" for entry in bad_keys)
                if extra:
                    per_plane[str(plane).lower()] = frozenset(extra)
        elif planes:
            bad.append(f"planes: expected a mapping, got {type(planes).__name__}")
        return cls(targets=targets, trusted=frozenset(trusted), per_plane=per_plane,
                   problems=tuple(bad))

    # -- questions the gateway asks -------------------------------------

    def target(self, plane: str) -> Optional[str]:
        """Where a confirmation for this plane goes, or None when none is configured."""
        return self.targets.get(str(plane or "").lower())

    def trusted_in(self, plane: str = "") -> frozenset[str]:
        """The keys trusted in this plane: the global list plus the plane's own."""
        extra = self.per_plane.get(str(plane or "").lower(), frozenset())
        return self.trusted | extra

    def is_trusted(self, who: str, plane: str = "") -> bool:
        """Is this key (npub or hex) trusted, globally or in this plane?"""
        hexed = as_hex(who)
        if not hexed:
            return False
        return hexed in self.trusted_in(plane)

    def describe(self) -> list[str]:
        """One line per plane, for `hng control` and the startup log."""
        lines: list[str] = []
        for plane in PLANES:
            destination = self.target(plane)
            count = len(self.trusted_in(plane))
            where = destination or "not configured"
            lines.append(f"{plane:<8} {where}  ({count} trusted npub(s))")
        for plane in sorted(set(self.targets) - set(PLANES)):
            lines.append(f"{plane:<8} {self.targets[plane]}  "
                         f"({len(self.trusted_in(plane))} trusted npub(s))")
        return lines
