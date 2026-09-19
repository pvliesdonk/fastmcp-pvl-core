"""No operator-supplied URL reaches a message, on any emit path.

``urlparse`` has several failure shapes, two of which echo their input.
The one that matters most, from CPython's ``_checknetloc``, interpolates
the raw netloc — userinfo included — into its ``ValueError``:

    >>> urlparse("redis://alice:hunter2@h℀st")
    ValueError: netloc 'alice:hunter2@h℀st' contains invalid
    characters under NFKC normalization

``℀`` NFKC-normalises to ``a/c``, so ``_checknetloc`` sees a
delimiter. Any site that lets that exception escape — or chains it as a
cause, since the traceback prints the message too — publishes the
password to logs and Sentry.

This file tests the *class*, not one site. Three separate fixes for it
have landed one site at a time (#337, #342, #343); a per-site test would
have missed each next instance, so every entry point that parses an
operator URL is swept here.
"""

from __future__ import annotations

import ast
import traceback
from pathlib import Path

import pytest

import fastmcp_pvl_core
from fastmcp_pvl_core import (
    ConfigurationError,
    ServerConfig,
    build_kv_store,
    compute_app_domain,
)

# NFKC-normalising host, so ``_checknetloc`` raises with the netloc in it.
_LEAKY = "redis://alice:hunter2@h℀st"
_LEAKY_HTTPS = "https://alice:hunter2@h℀st/x"
_SECRETS = ("hunter2", "alice")


def _assert_clean(exc: BaseException) -> None:
    """No secret in the message, the chain, or the rendered traceback."""
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    for secret in _SECRETS:
        assert secret not in str(exc), f"{secret!r} leaked into the message"
        assert secret not in rendered, f"{secret!r} leaked into the traceback"


class TestOperatorUrlNeverReachesAMessage:
    def test_build_kv_store(self):
        with pytest.raises(ConfigurationError) as exc_info:
            build_kv_store(ServerConfig(kv_store_url=_LEAKY), namespace="ns")
        _assert_clean(exc_info.value)

    def test_build_event_store(self):
        from fastmcp_pvl_core import build_event_store

        with pytest.raises(ConfigurationError) as exc_info:
            build_event_store("MY_APP", ServerConfig(kv_store_url=_LEAKY))
        _assert_clean(exc_info.value)

    def test_task_backend_reusing_the_kv_url(self):
        from fastmcp_pvl_core._tasks import _resolve_url_override

        with pytest.raises(ConfigurationError) as exc_info:
            _resolve_url_override("MY_APP", ServerConfig(kv_store_url=_LEAKY), None)
        _assert_clean(exc_info.value)

    # The explicit ``{PREFIX}_TASKS_URL`` path needs the FastMCP/docket
    # fixtures, so its leak test lives beside them as
    # ``test_tasks.py::TestExplicitTasksUrl::
    # test_unparseable_url_does_not_leak_userinfo``.

    def test_compute_app_domain_strips_userinfo_from_the_csp_origin(self):
        """Not a message path: this value becomes a CSP origin downstream."""
        domain = compute_app_domain(
            ServerConfig(base_url="https://alice:hunter2@example.com/x")
        )
        assert domain == "example.com"

    def test_compute_app_domain(self):
        with pytest.raises(ConfigurationError) as exc_info:
            compute_app_domain(ServerConfig(base_url=_LEAKY_HTTPS))
        _assert_clean(exc_info.value)


class TestRedactorNeverFailsOpen:
    """A sanitiser that raises on malformed input defeats itself.

    ``_redact_url`` is documented as the single choke point every
    message in the fetch primitive passes through (ADR §8). Raising
    there does not merely skip the redaction — it emits the unredacted
    netloc as the exception message.
    """

    @pytest.mark.parametrize(
        "url",
        [
            _LEAKY_HTTPS,
            # The port slot is the second way in: ``ParseResult.port``
            # raises quoting the text it cannot cast, and a truncated
            # authority puts the password there.
            "https://alice:hunter2",
            "https://alice:hunter2@host:notaport/p?token=SECRETTOKEN",
        ],
    )
    def test_redact_url_returns_a_placeholder_instead_of_raising(self, url):
        from fastmcp_pvl_core._transfer.fetch import _redact_url

        redacted = _redact_url(url)
        for secret in (*_SECRETS, "SECRETTOKEN"):
            assert secret not in redacted

    def test_valid_urls_still_redact_rather_than_becoming_placeholders(self):
        """The guard must not swallow URLs it can rebuild."""
        from fastmcp_pvl_core._transfer.fetch import _redact_url

        assert (
            _redact_url("https://alice:hunter2@h.example:8443/x?token=T")
            == "https://h.example:8443/x"
        )
        assert _redact_url("https://[::1]:8443/x") == "https://[::1]:8443/x"


#: Both raise the same input-echoing ``ValueError``. ``urlsplit`` is not
#: hypothetical here: ``_health.py`` documents an ``urlsplit(...).port``
#: idiom, so it is the obvious thing to reach for next.
_PARSERS = frozenset({"urlparse", "urlsplit"})


def _called_name(func: ast.expr) -> str | None:
    """Name of the callable in a call node, for `f()` and `mod.f()` alike."""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _aliases_of_parsers(tree: ast.Module) -> set[str]:
    """Local names bound to a parser by ``from urllib.parse import x as y``."""
    return {
        (alias.asname or alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "urllib.parse"
        for alias in node.names
        if alias.name in _PARSERS
    }


def _urlparse_calls(path: Path) -> list[int]:
    """Line numbers of direct URL-parser calls in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _PARSERS | _aliases_of_parsers(tree)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node.func) in names
    ]


def _source_modules() -> list[Path]:
    """Every shipped module except the one allowed to call a parser."""
    src = Path(fastmcp_pvl_core.__file__).parent
    allowed = (src / "_url.py").resolve()
    return [p for p in sorted(src.rglob("*.py")) if p.resolve() != allowed]


class TestNoDirectUrlparseOutsideTheHelper:
    """`urlparse` is called in one place, so the redaction rule has one home.

    Three separate fixes landed for this class before the helper existed
    (#337, #342, #343), each catching only the site in front of it. A
    conformance guard is what stops the fourth: any new call is a site
    that has not considered whether its ``ValueError`` echoes the netloc.

    Mirrors the AST conformance check the logging grammar uses
    (``tests/test_log_conformance.py``) — and inherits its caveat: a
    clean report is not proof. It matches call sites by name, so it
    still cannot see a parser reached through an alias of the *module*
    (``import urllib.parse as u; u.urlparse(...)`` is caught by
    attribute name, but ``getattr``-style indirection is not), and it
    does not police ``ParseResult.port``, the other attribute that
    raises with its input quoted. ``safe_netloc`` exists for that one.
    """

    def test_only_the_url_module_calls_urlparse(self):
        offenders = [
            f"{path.name}:{line}"
            for path in _source_modules()
            for line in _urlparse_calls(path)
        ]
        assert offenders == [], (
            "call fastmcp_pvl_core._url.parse_operator_url (raises "
            "ConfigurationError) or try_parse_url (never raises) instead of "
            "urlparse directly; urlparse's own ValueError can embed the raw "
            f"netloc, userinfo included. Offending sites: {offenders}"
        )
