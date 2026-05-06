"""Tests for AuthorizationMiddleware."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import PromptError, ResourceError, ToolError

from fastmcp_pvl_core._authorization import AuthorizationMiddleware


def _make_context(
    *,
    tool_name: str = "do_thing",
    tool_meta: dict[str, Any] | None = None,
) -> MagicMock:
    """Build a minimal MiddlewareContext-shaped mock for tool calls."""
    tool = SimpleNamespace(meta=tool_meta or {})
    fastmcp_obj = SimpleNamespace(get_tool=AsyncMock(return_value=tool))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(name=tool_name, arguments={})
    return MagicMock(
        message=message,
        fastmcp_context=fastmcp_context,
    )


def _allow_all(_subject: str | None, _required_scope: str) -> bool:
    return True


def _deny_all(_subject: str | None, _required_scope: str) -> bool:
    return False


@pytest.mark.asyncio
async def test_on_call_tool_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_call_tool_with_meta_allowed_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_call_tool_with_meta_denied_raises_tool_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "write"}
    call_next.assert_not_awaited()


def test_init_publishes_authorizer_for_ambient_check_authorization() -> None:
    """__init__ makes the authorizer available to check_authorization without kwarg."""
    from fastmcp_pvl_core._authorization import (
        AuthzDenied,
        check_authorization,
    )

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        AuthorizationMiddleware(authorizer=_allow_all)
        check_authorization("any_scope")  # ambient authorizer found, allows

        AuthorizationMiddleware(authorizer=_deny_all)
        with pytest.raises(AuthzDenied) as exc_info:
            check_authorization("any_scope")
    assert exc_info.value.subject == "user:alice"
    assert exc_info.value.required_scope == "any_scope"


def _make_resource_context(
    *, uri: str = "vault://doc-1", resource_meta: dict[str, Any] | None = None
) -> MagicMock:
    resource = SimpleNamespace(meta=resource_meta or {})
    fastmcp_obj = SimpleNamespace(get_resource=AsyncMock(return_value=resource))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(uri=uri)
    return MagicMock(message=message, fastmcp_context=fastmcp_context)


def _make_prompt_context(
    *, prompt_name: str = "the_prompt", prompt_meta: dict[str, Any] | None = None
) -> MagicMock:
    prompt = SimpleNamespace(meta=prompt_meta or {})
    fastmcp_obj = SimpleNamespace(get_prompt=AsyncMock(return_value=prompt))
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    message = SimpleNamespace(name=prompt_name, arguments={})
    return MagicMock(message=message, fastmcp_context=fastmcp_context)


@pytest.mark.asyncio
async def test_on_read_resource_with_meta_denied_raises_resource_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_resource_context(resource_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="contents")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ResourceError) as exc_info:
            await middleware.on_read_resource(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read"}
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_read_resource_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_resource_context(resource_meta={})
    call_next = AsyncMock(return_value="contents")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_read_resource(ctx, call_next)
    assert result == "contents"


@pytest.mark.asyncio
async def test_on_get_prompt_with_meta_denied_raises_prompt_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_prompt_context(prompt_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="messages")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(PromptError) as exc_info:
            await middleware.on_get_prompt(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read"}
    call_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_on_get_prompt_no_meta_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_prompt_context(prompt_meta={})
    call_next = AsyncMock(return_value="messages")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_get_prompt(ctx, call_next)
    assert result == "messages"
