"""Tests for AuthorizationMiddleware."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastmcp.exceptions import PromptError, ResourceError, ToolError

from fastmcp_pvl_core._authorization import AuthorizationMiddleware, AuthzDenied


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
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_read_resource_with_meta_allowed_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_resource_context(resource_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="contents")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_read_resource(ctx, call_next)
    assert result == "contents"
    call_next.assert_awaited_once_with(ctx)


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
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_get_prompt_with_meta_allowed_passes_through() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_prompt_context(prompt_meta={"required_scope": "read"})
    call_next = AsyncMock(return_value="messages")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_get_prompt(ctx, call_next)
    assert result == "messages"
    call_next.assert_awaited_once_with(ctx)


@pytest.mark.asyncio
async def test_on_call_tool_body_authz_denied_becomes_tool_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_context(tool_meta={})  # static check passes (no meta)

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="write:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "write:foo"}


@pytest.mark.asyncio
async def test_on_read_resource_body_authz_denied_becomes_resource_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_resource_context(resource_meta={})

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="read:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ResourceError) as exc_info:
            await middleware.on_read_resource(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read:foo"}


@pytest.mark.asyncio
async def test_on_get_prompt_body_authz_denied_becomes_prompt_error() -> None:
    middleware = AuthorizationMiddleware(authorizer=_allow_all)
    ctx = _make_prompt_context(prompt_meta={})

    async def body_raises(_ctx: object) -> str:
        raise AuthzDenied(subject="user:alice", required_scope="read:foo")

    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(PromptError) as exc_info:
            await middleware.on_get_prompt(ctx, body_raises)
    payload = json.loads(str(exc_info.value))
    assert payload == {"code": "authz_denied", "required_scope": "read:foo"}


@pytest.mark.asyncio
async def test_on_call_tool_get_tool_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_tool=AsyncMock(side_effect=KeyError("tool not found"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(name="missing", arguments={}),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="result")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_call_tool(ctx, call_next)
    assert result == "result"
    call_next.assert_awaited_once_with(ctx)
    assert any("tool lookup failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_read_resource_get_resource_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_resource=AsyncMock(side_effect=KeyError("nope"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(uri="vault://x"),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="contents")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    result = await middleware.on_read_resource(ctx, call_next)
    assert result == "contents"
    call_next.assert_awaited_once_with(ctx)
    assert any("resource lookup failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_get_prompt_get_prompt_failure_falls_through(
    caplog: pytest.LogCaptureFixture,
) -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    fastmcp_obj = SimpleNamespace(
        get_prompt=AsyncMock(side_effect=KeyError("nope"))
    )
    fastmcp_context = SimpleNamespace(fastmcp=fastmcp_obj)
    ctx = MagicMock(
        message=SimpleNamespace(name="missing", arguments={}),
        fastmcp_context=fastmcp_context,
    )
    call_next = AsyncMock(return_value="messages")
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    result = await middleware.on_get_prompt(ctx, call_next)
    assert result == "messages"
    call_next.assert_awaited_once_with(ctx)
    assert any("prompt lookup failed" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_on_list_tools_filters_denied_tools() -> None:
    """Tools with denied required_scope are dropped; unannotated kept."""
    tool_open = SimpleNamespace(name="open_tool", meta={})
    tool_write = SimpleNamespace(
        name="write_tool", meta={"required_scope": "write"}
    )
    tool_read = SimpleNamespace(
        name="read_tool", meta={"required_scope": "read"}
    )

    def authorize(_subject: str | None, required: str) -> bool:
        return required == "read"

    middleware = AuthorizationMiddleware(authorizer=authorize)
    call_next = AsyncMock(return_value=[tool_open, tool_write, tool_read])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_tools(ctx, call_next)
    assert result == [tool_open, tool_read]


@pytest.mark.asyncio
async def test_on_list_resources_filters_denied() -> None:
    res_open = SimpleNamespace(uri="vault://1", meta={})
    res_locked = SimpleNamespace(uri="vault://2", meta={"required_scope": "x"})
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[res_open, res_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_resources(ctx, call_next)
    assert result == [res_open]


@pytest.mark.asyncio
async def test_on_list_resource_templates_filters_denied() -> None:
    tmpl_open = SimpleNamespace(uri="vault://{a}", meta={})
    tmpl_locked = SimpleNamespace(
        uri="vault://locked/{a}", meta={"required_scope": "x"}
    )
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[tmpl_open, tmpl_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_resource_templates(ctx, call_next)
    assert result == [tmpl_open]


@pytest.mark.asyncio
async def test_on_list_prompts_filters_denied() -> None:
    p_open = SimpleNamespace(name="open", meta={})
    p_locked = SimpleNamespace(name="locked", meta={"required_scope": "x"})
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    call_next = AsyncMock(return_value=[p_open, p_locked])
    ctx = MagicMock()
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        result = await middleware.on_list_prompts(ctx, call_next)
    assert result == [p_open]


@pytest.mark.asyncio
async def test_default_payload_does_not_include_subject() -> None:
    middleware = AuthorizationMiddleware(authorizer=_deny_all)
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert "subject" not in payload


@pytest.mark.asyncio
async def test_expose_subject_in_error_includes_subject() -> None:
    middleware = AuthorizationMiddleware(
        authorizer=_deny_all, expose_subject_in_error=True
    )
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError) as exc_info:
            await middleware.on_call_tool(ctx, call_next)
    payload = json.loads(str(exc_info.value))
    assert payload == {
        "code": "authz_denied",
        "required_scope": "write",
        "subject": "user:alice",
    }


@pytest.mark.asyncio
async def test_subject_always_logged_at_warning_regardless_of_flag(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Subject appears in WARNING log even when expose_subject_in_error=False."""
    caplog.set_level("WARNING", logger="fastmcp_pvl_core._authorization")
    middleware = AuthorizationMiddleware(
        authorizer=_deny_all, expose_subject_in_error=False
    )
    ctx = _make_context(tool_meta={"required_scope": "write"})
    call_next = AsyncMock(return_value="result")
    with patch(
        "fastmcp_pvl_core._authorization.get_subject", return_value="user:alice"
    ):
        with pytest.raises(ToolError):
            await middleware.on_call_tool(ctx, call_next)
    assert any(
        "user:alice" in rec.message and "authz_denied" in rec.message
        for rec in caplog.records
    )
