"""Parsing operator-supplied URLs without repeating them back.

Every URL an operator sets may carry credentials — ``user:pass@`` in the
userinfo, a pre-signed token in the query string — so nothing derived from
one may reach a log line or an exception message unredacted.

The trap this module exists to close is that :func:`urllib.parse.urlparse`
*itself* leaks. It has several failure shapes, and two of them echo their input. The
one that matters most comes from CPython's ``_checknetloc``, which
interpolates the raw netloc into its ``ValueError``::

    >>> urlparse("redis://alice:hunter2@h℀st")
    ValueError: netloc 'alice:hunter2@h℀st' contains invalid
    characters under NFKC normalization

(``℀`` NFKC-normalises to ``a/c``, so ``_checknetloc`` sees a
delimiter.) A site that lets that escape publishes the password; so does
one that catches it and chains it with ``from exc``, because the rendered
traceback prints the cause's message too. Only ``from None`` with a
message of our own is safe.

Two shapes, because the two contexts have opposite requirements:

- :func:`parse_operator_url` **raises** :class:`ConfigurationError`. Startup
  configuration must fail fast — a server whose store URL is unreadable
  should not start.
- :func:`try_parse_url` **never raises**. Redaction helpers and log lines
  run on paths where raising would be the defect: a sanitiser that throws
  on malformed input emits the unredacted value as its exception message,
  which is precisely the leak it exists to prevent.

(``_check_bracketed_host`` echoes a bracketed host too, though never
userinfo.) This class was fixed once at two sites before it was given one
home (#342, closing #337), and again here (#343). New code that parses an
operator URL routes through here rather than calling ``urlparse`` or
``urlsplit`` directly.
"""

from __future__ import annotations

import re
from urllib.parse import ParseResult, urlparse

from ._errors import ConfigurationError

UNPARSEABLE = "<unparseable>"
"""Stand-in for a URL that could not be parsed, for logs and redactions.

A fixed token rather than any part of the value: the point of reaching for
it is that the value is untrusted.
"""


def parse_operator_url(url: str, *, variable: str) -> ParseResult:
    """Parse an operator-supplied *url*, or reject it as misconfiguration.

    Args:
        url: The operator's value. Never appears in the raised message.
        variable: Display name of the setting it came from, used to tell
            the operator *which* value to fix — the only actionable part,
            since the value itself cannot be quoted back. Callers that
            know the env prefix pass the resolved key
            (``MYAPP_TASKS_URL``); callers that do not pass the
            ``{PREFIX}_``-style form.

    Returns:
        The parsed URL.

    Raises:
        ConfigurationError: *url* does not parse. The message names
            *variable* only. The cause is suppressed with ``from None``
            because ``urlparse``'s own message can embed the netloc, and
            a chained traceback would publish it.
    """
    try:
        return urlparse(url)
    except ValueError:
        raise ConfigurationError(
            f"{variable} could not be parsed as a URL. Its value is withheld "
            "here because it may carry credentials; check the value you set."
        ) from None


def safe_netloc(parsed: ParseResult) -> str | None:
    """Authority of *parsed* with userinfo stripped, or ``None`` if unusable.

    ``ParseResult.port`` is computed on access, not at parse time, and it
    raises ``ValueError`` quoting the text it could not cast. For a
    truncated authority that text is the password::

        urlparse("https://alice:hunter2").port
        ValueError: Port could not be cast to integer value as 'hunter2'

    So a URL that parses cleanly can still leak on the attribute read,
    which is why reading ``.port`` directly is not safe either.

    Blanket-probing the port inside :func:`parse_operator_url` would be
    wrong: a ``mongodb://`` seed list (``h1:27017,h2:27017``) is a
    supported store URL whose port never casts, and rejecting it would
    break a working configuration. The guard belongs at the point of
    *use*, which is here. ``_health.py`` records the same hazard as its
    reason for redacting textually rather than parsing.

    Args:
        parsed: A parsed URL.

    Returns:
        ``host`` or ``host:port``, with any ``user:pass@`` removed.
        IPv6 hosts are bracketed: ``urlparse`` returns them unbracketed,
        but a netloc or ``Host`` header must bracket them (``[::1]``) to
        be well-formed. ``None`` when the authority has no host
        or its port will not cast — callers decide whether that is a
        placeholder or a rejection. Nothing derived from the offending
        text is returned, since that text is the thing that leaks.
    """
    try:
        port = parsed.port
    except ValueError:
        return None
    host = parsed.hostname
    if not host:
        return None
    bracketed = f"[{host}]" if ":" in host else host
    return f"{bracketed}:{port}" if port is not None else bracketed


def try_parse_url(url: str) -> ParseResult | None:
    """Parse *url*, or return ``None`` if it does not parse.

    For callers that must not raise: redaction helpers, and log lines
    emitted on a path that has not validated the URL yet.

    Args:
        url: Any URL, trusted or not.

    Returns:
        The parsed URL, or ``None`` when it does not parse. The
        ``ValueError`` is swallowed rather than returned or logged —
        its message is the thing that leaks.
    """
    try:
        return urlparse(url)
    except ValueError:
        return None


_URL_RE = re.compile(r"\b[a-zA-Z][a-zA-Z0-9+.-]*://\S+")
_USERINFO_RE = re.compile(r"://[^/\s]*@")
_QUERY_RE = re.compile(r"[?#]")


def redact_urls_in_text(text: str) -> str:
    """Strip userinfo and query/fragment from any URL inside *text*.

    Backend URLs reach a log line or a response through exception
    messages, and in this codebase they carry credentials in ``user:pass@``
    userinfo and tokens in the query string. Used by the health routes'
    ``full`` detail and by ``get_server_info``'s upstream error.

    The userinfo pattern is greedy up to the last ``@`` before the path,
    and every occurrence is replaced. A password may itself contain
    ``@``, and one whitespace-free token may carry more than one URL; a
    single non-greedy substitution leaked the tail of both.

    Known limit: the pattern stops at ``/``, so a password carrying a raw
    ``/`` is left unredacted. RFC 3986 requires that character to be
    percent-encoded in userinfo, and widening the pattern to cross ``/``
    would swallow everything between two URLs in one message. Stated
    rather than fixed.

    Deliberately textual rather than parsed. ``urlsplit(...).port`` raises
    ``ValueError`` on authorities this codebase actually supports — a
    ``mongodb://`` seed list such as ``h1:27017,h2:27017`` is the case
    that bit — and a redactor that raises emits the very message it was
    given to clean (see this module's docstring).

    ``_transfer.fetch`` redacts a URL it holds as a URL; this one has to
    find URLs inside free text, so the two are not the same function.

    Args:
        text: Free text, typically ``str(exc)``.

    Returns:
        *text* with every URL's userinfo, query and fragment removed.
    """

    def _strip(match: re.Match[str]) -> str:
        url = _USERINFO_RE.sub("://", match.group(0))
        return _QUERY_RE.split(url, maxsplit=1)[0]

    return _URL_RE.sub(_strip, text)
