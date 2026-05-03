"""Library-internal exception types.

All exceptions raised by builder/loader code that operators see during
startup live here so downstream catch sites have one stable import path.
"""

from __future__ import annotations


class ConfigurationError(Exception):
    """Operator-visible misconfiguration detected at startup or load time.

    Raised eagerly so a misconfigured server fails fast instead of
    silently denying every request.
    """
