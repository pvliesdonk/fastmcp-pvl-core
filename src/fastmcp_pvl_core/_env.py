"""Environment variable helpers.

All env var reads in the library and downstream projects route
through :func:`env` to keep naming consistent.
"""

from __future__ import annotations

import os
from typing import overload


@overload
def env(prefix: str, name: str) -> str | None: ...
@overload
def env(prefix: str, name: str, default: None) -> str | None: ...
@overload
def env(prefix: str, name: str, default: str) -> str: ...
def env(prefix: str, name: str, default: str | None = None) -> str | None:
    """Read ``{PREFIX}_{NAME}`` from the environment.

    Args:
        prefix: Env var prefix (trailing underscore optional).
        name: Variable name (without prefix).
        default: Value to return if unset or empty after strip.

    Returns:
        The env var value stripped of whitespace, or ``default``.
    """
    key = f"{prefix.rstrip('_')}_{name}"
    raw = os.environ.get(key)
    if raw is None:
        return default
    value = raw.strip()
    return value or default


def parse_bool(value: str) -> bool:
    """Parse common truthy strings to ``bool``.

    Args:
        value: Raw string value.

    Returns:
        ``True`` for ``1``, ``true``, ``yes``, ``on`` (case-insensitive);
        ``False`` otherwise.
    """
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_list(value: str) -> list[str]:
    """Parse a comma-separated list, trimming and dropping empties.

    Args:
        value: Comma-separated string.

    Returns:
        List of non-empty, stripped items.  Returns ``[]`` when *value* is
        blank.
    """
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_scopes(value: str | None) -> list[str] | None:
    """Parse an OIDC/OAuth scopes string (space- or comma-separated).

    Args:
        value: Raw scopes string, or ``None``.

    Returns:
        List of scope tokens.  ``None`` when *value* is ``None``; ``[]``
        when *value* is a blank string.
    """
    if value is None:
        return None
    normalized = value.replace(",", " ")
    return [s for s in normalized.split() if s]
