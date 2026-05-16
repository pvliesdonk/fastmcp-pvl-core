"""The fastmcp_pvl_core package root exports its documented public API."""

from __future__ import annotations


def test_upload_direction_type_aliases_are_exported() -> None:
    """#67: receiver-author type aliases are importable from the package root.

    ``BufferedReceiver`` / ``StreamReceiver`` are the receiver-callback
    types, and ``PreLinkValidator`` is the type of
    ``register_file_exchange_upload``'s ``pre_link_validator`` callback
    (run before an upload token is minted) — all three are part of the
    upload-direction public API. Before #67 the package root did not
    re-export them: ``BufferedReceiver`` / ``StreamReceiver`` were
    reachable only via the private ``_file_exchange_runtime`` submodule,
    and ``PreLinkValidator`` only via the ``file_exchange`` submodule —
    unlike the download-direction aliases ``ConsumerSink`` /
    ``FetchContext`` / ``FetchResult``, which the root already re-exports.
    """
    import fastmcp_pvl_core
    from fastmcp_pvl_core import (
        BufferedReceiver,
        PreLinkValidator,
        StreamReceiver,
        file_exchange,
    )

    # Listed in the package-root __all__.
    for name in ("BufferedReceiver", "StreamReceiver", "PreLinkValidator"):
        assert name in fastmcp_pvl_core.__all__, (
            f"{name} missing from fastmcp_pvl_core.__all__"
        )

    # Listed in the file_exchange facade's __all__.
    for name in ("BufferedReceiver", "StreamReceiver", "PreLinkValidator"):
        assert name in file_exchange.__all__, (
            f"{name} missing from file_exchange.__all__"
        )

    # The package-root names are the same objects as the facade's.
    assert BufferedReceiver is file_exchange.BufferedReceiver
    assert StreamReceiver is file_exchange.StreamReceiver
    assert PreLinkValidator is file_exchange.PreLinkValidator
