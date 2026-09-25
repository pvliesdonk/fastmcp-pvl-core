"""The tool boundary: no exception leaves a tool unclassified.

A tool call ends in one of four outcomes (the ``designing-tool-outcomes``
skill). Three of them are deliberate and the tool raises them itself as a
``ToolError`` with a model-facing message and a ``log_level``. The fourth,
the server failing, is everything else: this module turns it into one
ERROR line with the traceback and a fixed message that tells the model
the request was fine. Without it, the exception reaches FastMCP's own
handler, which logs its own ERROR traceback and, under
``mask_error_details``, drops the reason. The decision and its
alternatives are in ``docs/adr/0005-tool-boundary.md``; how FastMCP and
the MCP SDK treat each case is in
``docs/reference/mcp-tool-outcomes-and-errors.md``.
"""

from __future__ import annotations

import functools
import inspect
import logging
from collections.abc import Callable
from typing import Any, TypeVar

from fastmcp.exceptions import FastMCPError, ToolError

logger = logging.getLogger(__name__)

_F = TypeVar("_F", bound=Callable[..., Any])

_MARKER = "_pvl_tool_boundary"

FAULT_MESSAGE = (
    "The call failed because of a server-side error; the request itself was "
    "fine. Retry later, and tell the user if it keeps failing."
)
"""What the model reads for outcome 4: the request was fine, and what to do."""


def _fault(fn: Callable[..., Any], exc: Exception) -> ToolError:
    # Called inside the ``except`` block, so ``logger.exception`` attaches the
    # traceback of *exc*. This is the one traceback the fault gets: the
    # ToolError below is raised ``from None`` so no later handler prints it
    # again (ADR 0005 §2.2).
    logger.exception(
        "tool_failed function=%s error_type=%s", fn.__name__, type(exc).__name__
    )
    return ToolError(FAULT_MESSAGE)


def tool_boundary(fn: _F) -> _F:
    """Turn any exception from *fn* that is not a ``FastMCPError`` into a fault.

    Apply it under ``@mcp.tool`` so FastMCP registers the wrapper::

        @mcp.tool
        @tool_boundary
        async def read_note(path: str) -> dict[str, Any]:
            try:
                note = await store.read(path)
            except FileNotFoundError:
                raise ToolError(
                    f"No note at '{path}'. Find the path with search_notes.",
                    log_level=logging.INFO,
                ) from None
            return {"text": note.text}

    A ``FastMCPError`` (``ToolError``, ``ResourceError``, ...) passes through
    unchanged: the tool raised it on purpose, with its own message and
    ``log_level``. Any other ``Exception`` is logged once at ERROR with its
    traceback as ``tool_failed`` and replaced by a ``ToolError`` carrying
    :data:`FAULT_MESSAGE`, so the model never sees the exception's own text,
    with or without ``mask_error_details``. Cancellation and other
    ``BaseException`` subclasses pass through.

    ``functools.wraps`` keeps the signature and annotations, so the input and
    output schemas, ``Context`` injection and ``TaskConfig`` (which needs a
    coroutine function) behave as for *fn* itself. The decorator takes no
    options: the message, the log line and its level are the same decision
    for every server in the family.

    Args:
        fn: The tool function, sync or async.

    Returns:
        The wrapped function, of the same kind as *fn*.
    """
    wrapper: Callable[..., Any]
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except FastMCPError:
                raise
            except Exception as exc:
                raise _fault(fn, exc) from None

        wrapper = async_wrapper
    else:

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except FastMCPError:
                raise
            except Exception as exc:
                raise _fault(fn, exc) from None

        wrapper = sync_wrapper

    setattr(wrapper, _MARKER, True)
    return wrapper  # type: ignore[return-value]


def is_tool_boundary(fn: Callable[..., Any]) -> bool:
    """Report whether *fn* is wrapped by :func:`tool_boundary`.

    Follows ``__wrapped__`` through other ``functools.wraps`` decorators, so
    a boundary applied under another wrapper is still found. Meant for a
    test that enumerates a server's registered tools and fails any that can
    leak an unclassified exception.

    Args:
        fn: A tool function, e.g. a registered ``FunctionTool.fn``.

    Returns:
        ``True`` if *fn* or anything it wraps carries the boundary.
    """
    seen: set[int] = set()
    current: Any = fn
    while current is not None and id(current) not in seen:
        if getattr(current, _MARKER, False):
            return True
        seen.add(id(current))
        current = getattr(current, "__wrapped__", None)
    return False
