"""Tests for any_check (OR-combinator over native AuthChecks)."""

from __future__ import annotations

import pytest
from fastmcp.server.auth import AuthContext

from fastmcp_pvl_core._authorization import any_check


def _ctx() -> AuthContext:
    return AuthContext(token=object(), component=object())


def test_zero_checks_raises() -> None:
    with pytest.raises(ValueError, match="at least one"):
        any_check()


async def test_true_when_any_passes() -> None:
    check = any_check(lambda ctx: False, lambda ctx: True)
    assert await check(_ctx()) is True


async def test_false_when_all_fail() -> None:
    check = any_check(lambda ctx: False, lambda ctx: False)
    assert await check(_ctx()) is False


async def test_short_circuits_on_first_true() -> None:
    calls: list[int] = []

    def first(ctx: AuthContext) -> bool:
        calls.append(1)
        return True

    def second(ctx: AuthContext) -> bool:
        calls.append(2)
        return True

    check = any_check(first, second)
    assert await check(_ctx()) is True
    assert calls == [1]


async def test_awaits_async_sub_checks() -> None:
    async def async_true(ctx: AuthContext) -> bool:
        return True

    check = any_check(lambda ctx: False, async_true)
    assert await check(_ctx()) is True


async def test_raising_sub_check_propagates_and_stops_remaining() -> None:
    # Documented contract: a raising sub-check propagates out of any_check
    # and short-circuits the OR — a later check that would allow is never
    # reached (deny-safe; never silently falls through to an allow).
    calls: list[int] = []

    def raises(ctx: AuthContext) -> bool:
        raise RuntimeError("sub-check error")

    def second(ctx: AuthContext) -> bool:
        calls.append(2)
        return True

    check = any_check(raises, second)
    with pytest.raises(RuntimeError, match="sub-check error"):
        await check(_ctx())
    assert calls == []
