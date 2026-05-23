"""Filesystem-descriptor URI resolution and path confinement.

Security-critical. Turns an untrusted ``filesystem`` descriptor ``uri``
(§7.2.1, §7.2.3) into a confined local path:

- :func:`canonicalize_and_confine` — the confinement primitive
  (resolves symlinks + ``..``, rejects escapes per §10.1.3 / §15).
- :func:`resolve_filesystem_uri` — parse ``exchange://`` / ``file://``,
  look up the volume, confine.
- :func:`load_volume_map` — env-driven volume-to-mount-point config.
- :func:`atomic_write` — atomic write-then-rename of a stream to a local path.

All non-usable outcomes return ``None`` (the §9 "skip this descriptor"
signal); a confinement failure additionally logs a ``WARNING`` carrying
only the volume id (``exchange://``) or nothing identifying (``file://``)
— never the attacker-controlled raw path/URI.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import urlsplit

from fastmcp_pvl_core._env import env
from fastmcp_pvl_core._errors import ConfigurationError

logger = logging.getLogger(__name__)

VolumeMap = Mapping[str, Path]
"""A mapping from volume identifier to local mount-point path."""


def _parse_volume_map(raw: str, var_name: str) -> dict[str, Path]:
    """Parse ``name=path`` comma-separated pairs into a volume map.

    ``var_name`` is the full environment variable name (e.g.
    ``SCHOLAR_FILE_EXCHANGE_VOLUMES``), used only in error messages so the
    operator knows which variable to fix.

    Raises:
        ConfigurationError: an entry has no ``=``, an empty name, a name
            containing ``/``, an empty path, a non-absolute path, or a
            duplicate volume name — operator misconfiguration that must fail
            loudly at startup rather than silently make ``exchange://`` URIs
            unresolvable.
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
            raise ConfigurationError(f"{var_name} entry must be 'name=path': {entry!r}")
        if "/" in name:
            # The wire grammar is exchange://[^/]+/… — a "/" terminates the
            # netloc, so a volume name containing "/" can never match any
            # incoming URI. Fail loudly rather than leave dead config.
            raise ConfigurationError(
                f"{var_name} volume name must not contain '/': {name!r}"
            )
        if not path.startswith("/"):
            raise ConfigurationError(
                f"{var_name} mount path must be absolute: {path!r}"
            )
        if name in out:
            raise ConfigurationError(f"{var_name} duplicate volume name: {name!r}")
        out[name] = Path(path)
    return out


def load_volume_map(env_prefix: str) -> dict[str, Path]:
    """Load the volume map from ``{env_prefix}_FILE_EXCHANGE_VOLUMES``.

    ``env_prefix`` is the downstream server's env prefix (e.g. ``SCHOLAR``),
    threaded from its server config exactly as for every other pvl-core env
    reader (:func:`~fastmcp_pvl_core.ServerConfig.from_env`,
    :func:`~fastmcp_pvl_core.build_event_store`, …). pvl-core owns the
    ``_FILE_EXCHANGE_VOLUMES`` suffix; the prefix is the operator's
    per-server namespace, so two pvl servers on one host never collide on a
    single shared variable.

    Returns an empty map when the variable is unset/blank — a party with no
    volume mappings resolves no filesystem URIs and skips every filesystem
    descriptor during §9 selection.
    """
    var_name = f"{env_prefix.rstrip('_')}_FILE_EXCHANGE_VOLUMES"
    raw = env(env_prefix, "FILE_EXCHANGE_VOLUMES")
    return _parse_volume_map(raw, var_name) if raw else {}


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
    try:
        resolved_root = Path(root).resolve()
        resolved_candidate = Path(candidate).resolve()
    except (ValueError, OSError, RuntimeError):
        # Untrusted input or untrusted filesystem state must yield the
        # reject signal, never propagate. Path.resolve() raises ValueError
        # on an embedded null byte or bad surrogate, RuntimeError on a
        # symlink loop (CPython <=3.12; 3.13 returns the lexical path), and
        # OSError on other errno-based resolution failures.
        return None
    if resolved_candidate.is_relative_to(resolved_root):
        return resolved_candidate
    return None


def atomic_write(target: Path, source: BinaryIO) -> None:
    """Write ``source``'s bytes to ``target`` atomically.

    Reads ``source`` from its *current* position to EOF — it does not seek, so
    a non-zero start position transfers only the bytes from there on (and an
    already-exhausted stream writes an empty file). Callers pass a stream
    positioned at the first byte to write; this keeps non-seekable streams
    (pipes, sockets) usable.

    Streams into a temp file in ``target``'s own directory (so the final
    ``os.replace`` is a same-filesystem atomic rename), flushes + fsyncs it,
    then ``os.replace``s it into place — a concurrent reader never observes
    a partial file (§10.1.3 "made visible atomically: write to a temporary
    path, then rename into place"). The parent directory must already exist.
    On any error the temp file is removed, leaving ``target`` untouched.

    Sync, blocking file I/O — async callers should run it via
    ``asyncio.to_thread`` so it does not block the event loop.
    """
    target = Path(target)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as tmp:
            fd = -1  # fdopen took ownership; its __exit__ closes the fd now
            shutil.copyfileobj(source, tmp)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        # fdopen/copy/fsync/replace failed. If os.fdopen itself raised, the raw
        # fd was never wrapped, so close it here (fd != -1); once fdopen
        # succeeded the with-block already closed it, so closing again would
        # risk killing an unrelated fd that reused the number. The temp may
        # still exist (only gone after a successful os.replace), so remove it —
        # no partial deposit, no orphan temp; target is untouched. Suppress
        # OSError on each cleanup step so a cleanup failure can't mask the
        # original error.
        if fd != -1:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def resolve_filesystem_uri(uri: str, *, volume_map: VolumeMap) -> Path | None:
    """Resolve a filesystem-descriptor URI to a confined local path.

    Returns the confined path, or ``None`` when the URI is malformed,
    names an unmapped volume, or escapes confinement. An escape logs a
    ``WARNING`` (with only the volume id — never the raw path/URI);
    benign no-mapping does not log.

    Args:
        uri: The descriptor ``uri`` (``exchange://`` or ``file://``).
        volume_map: Volume id → mount point. The mount points are also
            the universe of exchange directories a ``file://`` path may
            lie within.
    """
    parsed = _parse_fs_uri(uri)
    if parsed is None:
        return None
    scheme, volume, path = parsed

    if scheme == "exchange":
        root = volume_map.get(volume)
        if root is None:
            return None
        confined = canonicalize_and_confine(root / path, root)
        if confined is None:
            # None covers a genuine escape, a symlink loop (RuntimeError on
            # CPython <=3.12), and an OSError — so the wording stays honest
            # rather than asserting "escaped" for what may be a broken link.
            logger.warning(
                "file-exchange: exchange:// path could not be confined to its "
                "volume root; rejecting (volume=%r)",
                volume,
            )
        return confined

    if not volume_map:
        return None  # party doesn't do filesystem — benign, no warning
        # (symmetric with the exchange:// unknown-volume case)
    # First lexical-confinement match wins (insertion order). With nested
    # volumes (e.g. /data and /data/uploads) a path under the child also
    # confines under the parent; confinement holds either way, but the caller
    # owns non-overlap if it attributes access policy/quota/audit to a volume.
    for root in volume_map.values():
        confined = canonicalize_and_confine(path, root)
        if confined is not None:
            return confined
    # As above, None may be escape, symlink loop, or OSError — not strictly
    # "outside every volume" — so the message says "could not be confined".
    logger.warning(
        "file-exchange: file:// path could not be confined to any configured "
        "volume; rejecting"
    )
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
    - Anything else (any query/fragment delimiter, unknown scheme,
      malformed) → ``None``.

    Paths are taken **literally** — not percent-decoded. §10.1 is silent on
    percent-encoding, so pvl-core defers the decode-vs-literal choice to a
    spec clarification (mcp-file-exchange-ext#14, tracked in #153);
    confinement is safe either way.

    The wire layer (``_FS_URI_PATTERN``) already validates shape at model
    construction; this re-derives validity structurally for direct
    callers and to honour ``file://``'s empty-authority rule. A
    drift-guard test keeps the two in agreement.
    """
    if any(c in uri for c in "\x00\t\n\r"):
        # urlsplit silently strips ASCII tab/newline/CR (WHATWG, a CPython
        # security fix), so without this guard the parser would parse a
        # string the descriptor never carried — a urlsplit-mutated input.
        # Reject all four up front so the parser only ever acts on the exact
        # bytes received; the null byte additionally must go because
        # Path.resolve() raises ValueError on it (the never-raise contract).
        # The drift-guard tests pin that the parser never accepts a URI the
        # wire rejects.
        return None
    try:
        parts = urlsplit(uri)
    except ValueError:
        # urlsplit raises ValueError on a malformed authority (e.g. an
        # invalid bracketed IPv6 literal). Untrusted input must yield the
        # reject signal, not propagate — symmetric with the control-char
        # guard and canonicalize_and_confine's reject-on-ValueError.
        return None
    if "?" in uri or "#" in uri:
        # No query or fragment component is part of the exchange://<volume>/
        # <path> or file:/// forms (§10.1). A raw-character check (not
        # parts.query/parts.fragment) is needed so a *bare* "?"/"#" — which
        # urlsplit yields as an empty, falsy query/fragment — is also
        # rejected, keeping "a#" consistent with the rejected "a#frag". The
        # wire's `.+` matches these, so rejecting keeps the parser stricter
        # than (never looser than) the wire.
        return None
    if parts.scheme == "exchange":
        if not uri.startswith("exchange://"):
            return (
                None  # urlsplit lowercased the scheme; wire pattern is case-sensitive
            )
        volume = parts.netloc
        # lstrip (not [1:]) intentionally collapses leading slashes, so
        # exchange://docs//a and exchange://docs/a normalise to the same
        # relative path. Both forms match the wire pattern's `.+`, and
        # confinement is component-wise downstream, so the collapse is safe.
        path = parts.path.lstrip("/")
        if not volume or not path:
            return None
        return ("exchange", volume, path)
    if parts.scheme == "file":
        if not uri.startswith("file://"):
            return (
                None  # urlsplit lowercased the scheme; wire pattern is case-sensitive
            )
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
