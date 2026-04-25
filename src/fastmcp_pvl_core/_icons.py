"""Build :class:`mcp.types.Icon` instances from on-disk files.

Every pvl-family server attaches Lucide icons (and friends) to its tools via
base64-encoded data URIs.  This module ships two helpers — see issue #16 for
the motivation:

- :func:`make_icon` reads one file and returns a single :class:`Icon` with a
  ``data:<mime>;base64,<...>`` ``src``.  Use this at decoration time, e.g.
  ``@mcp.tool(icons=[make_icon(static_dir / "search.svg")])``.
- :func:`register_tool_icons` is the bulk equivalent: takes a
  ``{tool_name: filename | [filenames]}`` mapping plus a ``static_dir`` and
  attaches the resulting icon lists to already-registered tools.
"""

from __future__ import annotations

import base64
import logging
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from mcp.types import Icon

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.tools.base import Tool

logger = logging.getLogger(__name__)


_MIME_BY_SUFFIX: dict[str, str] = {
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/vnd.microsoft.icon",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}
"""Supported icon file extensions and their MIME types.

Keys are lowercased ``Path.suffix`` values; anything not listed here causes
:func:`register_tool_icons` to raise :class:`ValueError`.
"""


def make_icon(
    path: str | Path,
    *,
    sizes: list[str] | None = None,
) -> Icon:
    """Read an icon file and return an :class:`Icon` with a base64 data URI.

    Use this at tool-decoration time when you only need a single icon::

        @mcp.tool(icons=[make_icon(STATIC / "search.svg")])
        def search(...): ...

    Or call it from your own code when :func:`register_tool_icons` doesn't
    fit (e.g. you want different icons per tool version).

    Args:
        path: Path to the icon file.  Must exist and have a supported
            extension: ``.svg``, ``.png``, ``.ico``, ``.jpg``/``.jpeg``.
        sizes: Optional ``["WxH", ...]`` size hints stored on the icon
            (e.g. ``["48x48", "96x96"]``).  Passed straight through to
            :class:`mcp.types.Icon`.

    Returns:
        An :class:`mcp.types.Icon` whose ``src`` is
        ``"data:<mime>;base64,<payload>"`` and whose ``mimeType`` matches
        the file extension.

    Raises:
        ValueError: If the file's extension is not supported.
        FileNotFoundError: If the file does not exist.
    """
    file_path = Path(path)
    suffix = file_path.suffix.lower()
    mime = _MIME_BY_SUFFIX.get(suffix)
    if mime is None:
        supported = ", ".join(sorted(_MIME_BY_SUFFIX))
        raise ValueError(
            f"Unsupported icon extension {suffix!r} for {file_path}; "
            f"supported extensions: {supported}"
        )
    payload = base64.b64encode(file_path.read_bytes()).decode("ascii")
    return Icon(
        src=f"data:{mime};base64,{payload}",
        mimeType=mime,
        sizes=sizes,
    )


IconSpec = str | Path | Sequence[str | Path]
"""Per-tool value type accepted by :func:`register_tool_icons`.

Either a single filename (``str``/``Path``) or a sequence of filenames.
Each filename is resolved relative to ``static_dir``; absolute paths are
accepted as-is.
"""


def register_tool_icons(
    mcp: FastMCP,
    mapping: Mapping[str, IconSpec],
    *,
    static_dir: str | Path,
) -> None:
    """Attach base64-encoded icons to already-registered FastMCP tools.

    Resolves each filename relative to ``static_dir`` (absolute paths pass
    through unchanged), reads it, encodes it as
    ``data:<mime>;base64,<...>``, and assigns the resulting list to the
    matching tool's ``icons`` attribute (replacing any previous icons).
    Tools registered with multiple versions all receive the same icons.

    The mapping is fully validated before any tool is mutated, so a missing
    file or unknown tool name aborts the call without leaving a half-applied
    state.

    For one-off use at decoration time, prefer :func:`make_icon` directly::

        @mcp.tool(icons=[make_icon(STATIC / "search.svg")])
        def search(...): ...

    Args:
        mcp: The :class:`FastMCP` instance whose tools should receive icons.
            Tools must already be registered (e.g. via ``@mcp.tool`` or
            ``mcp.add_tool``) before calling this helper.
        mapping: ``{tool_name: filename}`` or ``{tool_name: [filenames]}``.
            Filenames may be ``str`` or :class:`~pathlib.Path`.  Relative
            paths are resolved against ``static_dir``; absolute paths are
            used as-is.  Supported extensions: ``.svg``, ``.png``, ``.ico``,
            ``.jpg``/``.jpeg``.
        static_dir: Directory containing the icon files.  Must exist and be
            a directory; only used to resolve relative filenames.

    Raises:
        ValueError: If a tool name in ``mapping`` is not registered on
            ``mcp``, or a filename has an unsupported extension.
        FileNotFoundError: If ``static_dir`` does not exist or a referenced
            file is missing.
        NotADirectoryError: If ``static_dir`` exists but is not a directory.
    """
    # Imported lazily so callers that never use this helper don't pay for
    # the fastmcp.tools import at package load time.
    from fastmcp.tools.base import Tool

    base_dir = Path(static_dir)
    if not base_dir.exists():
        raise FileNotFoundError(f"static_dir does not exist: {base_dir}")
    if not base_dir.is_dir():
        raise NotADirectoryError(f"static_dir is not a directory: {base_dir}")

    # LocalProvider keys components as ``"<prefix>:<name>@<version>"`` (the
    # ``@`` is always present, version is empty for unversioned tools).
    # Group tools by name once; the helper applies icons to every registered
    # version of the named tool.
    tools_by_name: dict[str, list[Tool]] = {}
    for component in mcp._local_provider._components.values():
        if isinstance(component, Tool):
            tools_by_name.setdefault(component.name, []).append(component)

    resolved: list[tuple[str, list[Tool], list[Icon], list[str]]] = []

    for tool_name, files in mapping.items():
        targets = tools_by_name.get(tool_name)
        if not targets:
            raise ValueError(
                f"Tool {tool_name!r} is not registered on this FastMCP "
                "instance; register tools before calling "
                "register_tool_icons()."
            )

        if isinstance(files, str | Path):
            entries: list[str | Path] = [files]
        else:
            entries = list(files)

        icons: list[Icon] = []
        display: list[str] = []
        for entry in entries:
            entry_path = Path(entry)
            path = entry_path if entry_path.is_absolute() else base_dir / entry_path
            if not path.is_file():
                raise FileNotFoundError(
                    f"Icon file for tool {tool_name!r} not found: {path}"
                )
            icons.append(make_icon(path))
            display.append(str(entry))
        resolved.append((tool_name, targets, icons, display))

    for tool_name, targets, icons, filenames in resolved:
        for tool in targets:
            tool.icons = icons
        logger.info("icons registered tool=%s files=%s", tool_name, ",".join(filenames))
