"""Filesystem-descriptor URI resolution and path confinement.

Security-critical. Turns an untrusted ``filesystem`` descriptor ``uri``
(§7.2.1, §7.2.3) into a confined local path:

- :func:`canonicalize_and_confine` — the confinement primitive
  (resolves symlinks + ``..``, rejects escapes per §10.1.3 / §15).
- :func:`resolve_filesystem_uri` — parse ``exchange://`` / ``file://``,
  look up the volume, confine.
- :func:`load_volume_map` — env-driven volume-to-mount-point config.

All non-usable outcomes return ``None`` (the §9 "skip this descriptor"
signal); a confinement escape additionally logs a ``WARNING`` carrying
only the volume id, never the attacker-controlled raw path/URI.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastmcp_pvl_core._env import env
from fastmcp_pvl_core._errors import ConfigurationError

logger = logging.getLogger(__name__)

VolumeMap = Mapping[str, Path]
"""A mapping from volume identifier to local mount-point path."""


def _parse_volume_map(raw: str) -> dict[str, Path]:
    """Parse ``name=path`` comma-separated pairs into a volume map.

    Raises:
        ConfigurationError: an entry has no ``=``, an empty name, an
            empty path, a non-absolute path, or a duplicate volume name
            — operator misconfiguration that must fail loudly at startup
            rather than silently make ``exchange://`` URIs unresolvable.
    """
    out: dict[str, Path] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, sep, path = entry.partition("=")
        name = name.strip()
        path = path.strip()
        if not sep or not name or not path:
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES entry must be 'name=path': {entry!r}"
            )
        if not path.startswith("/"):
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES mount path must be absolute: {path!r}"
            )
        if name in out:
            raise ConfigurationError(
                f"FILE_EXCHANGE_VOLUMES duplicate volume name: {name!r}"
            )
        out[name] = Path(path)
    return out


def load_volume_map(prefix: str = "FILE_EXCHANGE") -> dict[str, Path]:
    """Load the volume map from ``{prefix}_VOLUMES`` in the environment.

    Returns an empty map when the variable is unset/blank — a party with
    no volume mappings resolves no filesystem URIs and skips every
    filesystem descriptor during §9 selection.

    ``prefix`` defaults to the canonical ``FILE_EXCHANGE`` (pvl-core owns
    the env contract); it is overridable only to namespace a multi-server
    deployment, not to change the contract's shape.
    """
    raw = env(prefix, "VOLUMES")
    return _parse_volume_map(raw) if raw else {}


def canonicalize_and_confine(candidate: Path | str, root: Path | str) -> Path | None:
    """Resolve symlinks + ``..`` and confirm ``candidate`` is within ``root``.

    Returns the fully-resolved candidate path iff it is ``root`` itself
    or a descendant; ``None`` on any escape (the reject signal — §10.1.3
    / §15 "MUST reject escapes, including via symlinks").

    ``Path.resolve()`` resolves every symlink in the path's existing
    prefix and normalises ``.``/``..``; a not-yet-existing tail is
    appended lexically (so a sink target need not exist). Existence /
    readability / writability is a separate concern (the caller's
    ``os.access`` check), not confinement.

    .. note::
        This is a resolution-time check. A symlink swapped between this
        call and a subsequent ``open()`` (a TOCTOU race) is not defended
        here; the data-plane caller must re-confine at open time or use
        ``O_NOFOLLOW`` / ``openat`` (tracked in #143).
    """
    resolved_root = Path(root).resolve()
    resolved_candidate = Path(candidate).resolve()
    if resolved_candidate.is_relative_to(resolved_root):
        return resolved_candidate
    return None


def _parse_fs_uri(
    uri: str,
) -> tuple[Literal["exchange", "file"], str, str] | None:
    """Decode a filesystem-descriptor URI into ``(scheme, volume, path)``.

    - ``exchange://<volume>/<path>`` → ``("exchange", volume, path)``
      with the path's leading ``/`` stripped (so it joins under a mount;
      ``pathlib`` would discard the mount if the right operand were
      absolute). Volume and a non-empty path are required.
    - ``file:///<abs>`` → ``("file", "", abs_path)``; the authority MUST
      be empty (§10.1.2) and the path absolute.
    - Anything else (query/fragment present, unknown scheme, malformed)
      → ``None``.

    The wire layer (``_FS_URI_PATTERN``) already validates shape at model
    construction; this re-derives validity structurally for direct
    callers and to honour ``file://``'s empty-authority rule. A
    drift-guard test keeps the two in agreement.
    """
    parts = urlsplit(uri)
    if parts.query or parts.fragment:
        return None
    if parts.scheme == "exchange":
        volume = parts.netloc
        path = parts.path.lstrip("/")
        if not volume or not path:
            return None
        return ("exchange", volume, path)
    if parts.scheme == "file":
        if parts.netloc:
            return None
        path = parts.path
        # Match _FS_URI_PATTERN's ``file:///[^/].*``: an absolute path
        # whose first segment char is not itself a slash. Rejects the
        # root-only ``file:///`` (path ``/``) and ``file:////x`` (path
        # ``//x``), keeping the parser no more permissive than the wire.
        if len(path) < 2 or path[0] != "/" or path[1] == "/":
            return None
        return ("file", "", path)
    return None
