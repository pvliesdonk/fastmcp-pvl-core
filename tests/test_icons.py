"""Tests for ``register_tool_icons``."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import register_tool_icons

SVG_BYTES = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"></svg>'
PNG_BYTES = b"\x89PNG\r\n\x1a\nfake-png-payload"
ICO_BYTES = b"\x00\x00\x01\x00fake-ico-payload"
JPG_BYTES = b"\xff\xd8\xff\xe0fake-jpg-payload"


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _tool_icons(mcp: FastMCP, tool_name: str) -> list:
    for component in mcp._local_provider._components.values():
        if getattr(component, "name", None) == tool_name:
            return component.icons or []
    raise KeyError(tool_name)


def _make_mcp_with_tool(tool_name: str = "ping") -> FastMCP:
    mcp = FastMCP("t")

    @mcp.tool(name=tool_name)
    def _ping() -> str:
        return "pong"

    return mcp


class TestRegisterToolIcons:
    def test_single_svg(self, tmp_path: Path):
        _write(tmp_path, "ping.svg", SVG_BYTES)
        mcp = _make_mcp_with_tool()

        register_tool_icons(mcp, {"ping": "ping.svg"}, static_dir=tmp_path)

        icons = _tool_icons(mcp, "ping")
        assert len(icons) == 1
        assert icons[0].mimeType == "image/svg+xml"
        assert icons[0].src.startswith("data:image/svg+xml;base64,")
        payload = icons[0].src.split(",", 1)[1]
        assert base64.b64decode(payload) == SVG_BYTES

    def test_list_of_files_preserves_order(self, tmp_path: Path):
        _write(tmp_path, "a.svg", SVG_BYTES)
        _write(tmp_path, "b.png", PNG_BYTES)
        mcp = _make_mcp_with_tool()

        register_tool_icons(
            mcp, {"ping": ["a.svg", "b.png"]}, static_dir=tmp_path
        )

        icons = _tool_icons(mcp, "ping")
        assert [i.mimeType for i in icons] == ["image/svg+xml", "image/png"]

    @pytest.mark.parametrize(
        ("filename", "data", "expected_mime"),
        [
            ("p.png", PNG_BYTES, "image/png"),
            ("p.ico", ICO_BYTES, "image/vnd.microsoft.icon"),
            ("p.jpg", JPG_BYTES, "image/jpeg"),
            ("p.JPEG", JPG_BYTES, "image/jpeg"),
        ],
    )
    def test_supported_extensions(
        self,
        tmp_path: Path,
        filename: str,
        data: bytes,
        expected_mime: str,
    ):
        _write(tmp_path, filename, data)
        mcp = _make_mcp_with_tool()

        register_tool_icons(mcp, {"ping": filename}, static_dir=tmp_path)

        icons = _tool_icons(mcp, "ping")
        assert icons[0].mimeType == expected_mime
        assert icons[0].src.startswith(f"data:{expected_mime};base64,")

    def test_unknown_extension_rejected(self, tmp_path: Path):
        _write(tmp_path, "ping.bmp", b"bmp")
        mcp = _make_mcp_with_tool()

        with pytest.raises(ValueError, match=r"\.bmp"):
            register_tool_icons(mcp, {"ping": "ping.bmp"}, static_dir=tmp_path)

    def test_missing_file_raises_with_path(self, tmp_path: Path):
        mcp = _make_mcp_with_tool()

        with pytest.raises(FileNotFoundError, match="missing.svg"):
            register_tool_icons(
                mcp, {"ping": "missing.svg"}, static_dir=tmp_path
            )

    def test_missing_static_dir_raises(self, tmp_path: Path):
        mcp = _make_mcp_with_tool()
        missing = tmp_path / "does-not-exist"

        with pytest.raises(FileNotFoundError, match="does-not-exist"):
            register_tool_icons(mcp, {"ping": "x.svg"}, static_dir=missing)

    def test_static_dir_is_a_file_raises(self, tmp_path: Path):
        regular_file = _write(tmp_path, "not-a-dir.txt", b"hi")
        mcp = _make_mcp_with_tool()

        with pytest.raises(NotADirectoryError):
            register_tool_icons(mcp, {"ping": "x.svg"}, static_dir=regular_file)

    def test_unregistered_tool_raises_and_does_not_mutate(
        self, tmp_path: Path
    ):
        _write(tmp_path, "ping.svg", SVG_BYTES)
        mcp = _make_mcp_with_tool()

        with pytest.raises(ValueError, match="ghost"):
            register_tool_icons(
                mcp,
                {"ping": "ping.svg", "ghost": "ping.svg"},
                static_dir=tmp_path,
            )

        # The valid entry must NOT have been applied — validate-before-mutate.
        assert _tool_icons(mcp, "ping") == []

    def test_second_call_replaces_icons(self, tmp_path: Path):
        _write(tmp_path, "a.svg", SVG_BYTES)
        _write(tmp_path, "b.png", PNG_BYTES)
        mcp = _make_mcp_with_tool()

        register_tool_icons(mcp, {"ping": "a.svg"}, static_dir=tmp_path)
        register_tool_icons(mcp, {"ping": "b.png"}, static_dir=tmp_path)

        icons = _tool_icons(mcp, "ping")
        assert len(icons) == 1
        assert icons[0].mimeType == "image/png"

    def test_empty_mapping_is_noop(self, tmp_path: Path):
        mcp = _make_mcp_with_tool()

        register_tool_icons(mcp, {}, static_dir=tmp_path)

        assert _tool_icons(mcp, "ping") == []

    def test_static_dir_accepts_string(self, tmp_path: Path):
        _write(tmp_path, "ping.svg", SVG_BYTES)
        mcp = _make_mcp_with_tool()

        register_tool_icons(mcp, {"ping": "ping.svg"}, static_dir=str(tmp_path))

        assert _tool_icons(mcp, "ping")[0].mimeType == "image/svg+xml"
