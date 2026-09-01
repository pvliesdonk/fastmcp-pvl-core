"""Unit tests for the internal helpers extracted from ``register_tool_icons``.

These cover the three helpers carved out of the high-complexity
``register_tool_icons`` orchestrator (``_resolve_static_dir``,
``_index_tools_by_name``, ``_resolve_icon_entry``) directly, at a finer
grain than the public-builder tests in ``test_icons.py`` reach them.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastmcp import FastMCP
from fastmcp.tools.base import Tool

from fastmcp_pvl_core._icons import (
    _index_tools_by_name,
    _resolve_icon_entry,
    _resolve_static_dir,
)

SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"></svg>'


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _make_mcp_with_tool(tool_name: str = "ping") -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool(name=tool_name)
    def _ping() -> str:
        return "pong"

    return mcp


# --------------------------------------------------------------------------
# _resolve_static_dir
# --------------------------------------------------------------------------


def test_resolve_static_dir_returns_resolved_dir(tmp_path: Path) -> None:
    result = _resolve_static_dir(tmp_path)
    assert result == tmp_path.resolve()
    assert result.is_absolute()


def test_resolve_static_dir_accepts_string(tmp_path: Path) -> None:
    assert _resolve_static_dir(str(tmp_path)) == tmp_path.resolve()


def test_resolve_static_dir_missing_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(FileNotFoundError, match="does not exist"):
        _resolve_static_dir(missing)


def test_resolve_static_dir_file_raises(tmp_path: Path) -> None:
    regular_file = _write(tmp_path, "not-a-dir.txt", b"hi")
    with pytest.raises(NotADirectoryError, match="not a directory"):
        _resolve_static_dir(regular_file)


def test_resolve_static_dir_collapses_dotdot(tmp_path: Path) -> None:
    # The returned base_dir is what _resolve_icon_entry's containment check
    # compares against, so it must be fully normalised — a ``..`` segment
    # has to collapse rather than survive into the comparison.
    sub = tmp_path / "sub"
    sub.mkdir()
    assert _resolve_static_dir(sub / "..") == tmp_path.resolve()


# --------------------------------------------------------------------------
# _index_tools_by_name
# --------------------------------------------------------------------------


def test_index_single_tool() -> None:
    mcp = _make_mcp_with_tool("ping")
    index = _index_tools_by_name(mcp)
    assert list(index["ping"])[0].name == "ping"
    assert len(index["ping"]) == 1


def test_index_multiple_tools() -> None:
    mcp = FastMCP("t")

    @mcp.tool(name="alpha")
    def _alpha() -> str:
        return "a"

    @mcp.tool(name="beta")
    def _beta() -> str:
        return "b"

    index = _index_tools_by_name(mcp)
    assert {"alpha", "beta"} <= set(index)


def test_index_groups_multiple_versions_under_one_name() -> None:
    # The dict[str, list[Tool]] return type exists for exactly this case:
    # one tool name registered at multiple versions must accumulate into a
    # list, not overwrite. Mimic a second version by injecting another
    # same-named component under a distinct store key.
    mcp = _make_mcp_with_tool("ping")

    def _ping_v2() -> str:
        return "pong2"

    mcp.local_provider._components["tool:ping@v2"] = Tool.from_function(
        _ping_v2, name="ping"
    )

    index = _index_tools_by_name(mcp)
    assert len(index["ping"]) == 2
    assert all(tool.name == "ping" for tool in index["ping"])


def test_index_unknown_tool_absent() -> None:
    index = _index_tools_by_name(_make_mcp_with_tool("ping"))
    assert "ghost" not in index


def test_index_raises_runtime_error_on_api_change() -> None:
    # Mirror the documented failure mode: ``local_provider`` is present but
    # ``_components`` is gone (a rename), so the AttributeError fires at the
    # ``._components`` access the guard describes — not at ``.local_provider``.
    class _FakeProvider:
        pass  # no _components attribute

    class _FakeMCP:
        local_provider = _FakeProvider()

    with pytest.raises(RuntimeError, match="FastMCP internal API changed"):
        _index_tools_by_name(_FakeMCP())  # type: ignore[arg-type]


# --------------------------------------------------------------------------
# _resolve_icon_entry
# --------------------------------------------------------------------------


def test_resolve_entry_relative_returns_icon(tmp_path: Path) -> None:
    _write(tmp_path, "ping.svg", SVG_BYTES)
    icon = _resolve_icon_entry("ping.svg", tmp_path.resolve(), "ping")
    assert icon.mime_type == "image/svg+xml"
    assert base64.b64decode(icon.src.split(",", 1)[1]) == SVG_BYTES


def test_resolve_entry_absolute_bypasses_containment(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    absolute = _write(elsewhere, "abs.svg", SVG_BYTES)
    icon = _resolve_icon_entry(absolute, tmp_path.resolve(), "ping")
    assert icon.mime_type == "image/svg+xml"


def test_resolve_entry_relative_traversal_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.svg").write_bytes(SVG_BYTES)
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    with pytest.raises(ValueError, match="escapes static_dir"):
        _resolve_icon_entry("../outside/secret.svg", static_dir.resolve(), "ping")


def test_resolve_entry_missing_file_raises_with_tool(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="for tool 'ping' not found"):
        _resolve_icon_entry("missing.svg", tmp_path.resolve(), "ping")


def test_resolve_entry_unsupported_extension_includes_tool(tmp_path: Path) -> None:
    _write(tmp_path, "ping.bmp", b"bmp")
    with pytest.raises(ValueError, match=r"Tool 'ping'"):
        _resolve_icon_entry("ping.bmp", tmp_path.resolve(), "ping")
