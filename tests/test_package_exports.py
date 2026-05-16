"""The fastmcp_pvl_core package root exports its documented public API."""

from __future__ import annotations


def test_file_exchange_hook_types_are_exported() -> None:
    """#105: the file-exchange hook surface is importable from the package root.

    The downstream-facing surface is two hook types — ``SourceHook``
    (``(origin_id) -> ResolvedSource``) and ``SinkHook``
    (``(file-like, SinkContext) -> mapping``) — plus the value types
    they move (``ResolvedSource``, ``SinkContext``) and the
    upload-direction ``PreLinkValidator`` callback. All five are part
    of the public API and must be reachable from the package root, not
    only via the ``file_exchange`` submodule.

    This is the post-#105 successor to the #67 test that asserted the
    re-export of the now-removed ``BufferedReceiver`` / ``StreamReceiver``
    receiver-callback aliases and the ``ConsumerSink`` / ``FetchContext``
    / ``FetchResult`` download-direction aliases — the four divergent
    hook shapes that #105 collapsed into ``SourceHook`` / ``SinkHook``.
    """
    import fastmcp_pvl_core
    from fastmcp_pvl_core import (
        PreLinkValidator,
        ResolvedSource,
        SinkContext,
        SinkHook,
        SourceHook,
        file_exchange,
    )

    public_names = (
        "SourceHook",
        "SinkHook",
        "SinkContext",
        "ResolvedSource",
        "PreLinkValidator",
    )

    # Listed in the package-root __all__.
    for name in public_names:
        assert name in fastmcp_pvl_core.__all__, (
            f"{name} missing from fastmcp_pvl_core.__all__"
        )

    # Listed in the file_exchange facade's __all__.
    for name in public_names:
        assert name in file_exchange.__all__, (
            f"{name} missing from file_exchange.__all__"
        )

    # The package-root names are the same objects as the facade's.
    assert SourceHook is file_exchange.SourceHook
    assert SinkHook is file_exchange.SinkHook
    assert SinkContext is file_exchange.SinkContext
    assert ResolvedSource is file_exchange.ResolvedSource
    assert PreLinkValidator is file_exchange.PreLinkValidator


def test_removed_download_direction_aliases_are_gone() -> None:
    """#105: the pre-collapse hook aliases are no longer importable.

    ``FetchContext`` / ``FetchResult`` / ``ConsumerSink`` /
    ``ByteSourceResolver`` / ``BufferedReceiver`` / ``StreamReceiver``
    were the four divergent download-/upload-direction hook shapes. #105
    replaced them with the unified ``SourceHook`` / ``SinkHook`` pair, so
    the old names must be gone from both the package root and the
    ``file_exchange`` facade.
    """
    import fastmcp_pvl_core
    from fastmcp_pvl_core import file_exchange

    removed = (
        "FetchContext",
        "FetchResult",
        "ConsumerSink",
        "ByteSourceResolver",
        "BufferedReceiver",
        "StreamReceiver",
    )
    for name in removed:
        assert name not in fastmcp_pvl_core.__all__, (
            f"{name} should be removed from fastmcp_pvl_core.__all__"
        )
        assert not hasattr(fastmcp_pvl_core, name), (
            f"{name} should not be importable from fastmcp_pvl_core"
        )
        assert name not in file_exchange.__all__, (
            f"{name} should be removed from file_exchange.__all__"
        )
        assert not hasattr(file_exchange, name), (
            f"{name} should not be importable from file_exchange"
        )
