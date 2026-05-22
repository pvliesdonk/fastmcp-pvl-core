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
from pathlib import Path

logger = logging.getLogger(__name__)


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
