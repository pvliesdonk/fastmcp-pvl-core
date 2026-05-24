"""The ``download`` transport data plane (#145).

Three role helpers plus a serving route, free functions mirroring
``_filesystem.py``. The transport is **lazy**: the GET route calls the #142
``ArtifactSource`` hook on demand and streams the result — pvl-core holds no
copy of the artifact. The provider mints a capability URL backed by the #144
token store; the fetcher retrieves it through the #147 ``guarded_stream`` into a
transient temp file, verifies size+digest before handing the sink a real sync
fd, then deletes the temp. See
``docs/superpowers/specs/2026-05-24-file-exchange-145-download-data-plane-design.md``.

An ``ArtifactSource`` offered via ``download`` MUST yield stable bytes for the
token's lifetime: the route re-opens the hook on every GET and on each ``Range``
resume.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING

import httpx

from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream
from fastmcp_pvl_core._file_exchange._spec import HANDLE_TYPE, SPEC_VERSION
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import DownloadSource, TransferHandle

if TYPE_CHECKING:
    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

logger = logging.getLogger(__name__)

# pvl-core's download route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
DOWNLOAD_PREFIX = "/fx/d"

_CHUNK = 1024 * 1024
# Declared-digest label -> hashlib name; an unsupported label fails verification
# (cannot verify -> digest-mismatch), never silently skips. Mirrors _filesystem.
_HASHLIB_BY_LABEL = {"sha-256": "sha256", "sha-384": "sha384", "sha-512": "sha512"}
# Max mid-stream reconnects before giving up on a dropped download.
_MAX_RECONNECTS = 5


async def download_provider_mint(
    artifact: ArtifactMetadata,
    key: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    single_use: bool = True,
) -> TransferHandle:
    """Provider role (pull): mint a download token and emit a TransferHandle.

    ``artifact`` is the caller-supplied metadata for what is being offered
    (lazy serving means the source hook is untouched at mint; the route opens it
    at GET). ``key`` is the server's opaque artifact identifier, stored opaquely
    in the token for the route to read back. ``base_url`` is the server's public
    https origin; ``ttl`` is clamped by the token store's ceiling.
    """
    minted = await token_store.mint({"key": key}, ttl=ttl, single_use=single_use)
    url = capability_url(base_url, DOWNLOAD_PREFIX, minted.token)
    return TransferHandle(
        type=HANDLE_TYPE,
        version=SPEC_VERSION,
        artifact=artifact,
        sources=[
            DownloadSource(
                transport="download",
                url=url,
                expiresAt=minted.expires_at,
                singleUse=single_use,
            )
        ],
    )


def _digest_verifier(
    declared: str | None,
) -> tuple[hashlib._Hash | None, str | None, bool]:
    """Return (hasher | None, expected_hex | None, unverifiable).

    ``unverifiable`` is True when a digest is declared with an unsupported label
    — verification must then fail (cannot verify), never silently skip (§15).
    """
    if declared is None:
        return None, None, False
    label, _, expected_hex = declared.partition(":")
    name = _HASHLIB_BY_LABEL.get(label.lower())
    if name is None:
        return None, expected_hex, True
    return hashlib.new(name), expected_hex.lower(), False


async def download_fetcher_consume(
    handle: TransferHandle,
    descriptor: DownloadSource,
    sink: ArtifactSink,
    *,
    config: ServerConfig,
) -> None:
    """Fetcher role (pull): download ``descriptor`` and deposit into ``sink``.

    Selection (``select_source``) is the caller's step. Streams the body through
    the #147 guard into a transient temp file (hashing as it writes), verifies
    ``handle.artifact`` size+digest **before** opening the temp for the sink
    (verify-before-use), then deletes the temp. Failures map to §13 codes; a
    dropped connection is recovered with ``Range``.

    The download loop is async (it awaits ``guarded_stream``); only the blocking
    temp-file writes are off-loaded with ``asyncio.to_thread``.
    """
    expected_size = handle.artifact.size
    max_size = config.file_exchange_max_artifact_size
    hasher, expected_hex, unverifiable = _digest_verifier(handle.artifact.digest)

    fd, tmp_path = tempfile.mkstemp(prefix="fx-download-")
    tmp = os.fdopen(fd, "wb")
    try:
        try:
            received = 0
            attempts = 0
            while True:
                req_headers = {} if received == 0 else {"Range": f"bytes={received}-"}
                try:
                    async with guarded_stream(
                        "GET",
                        descriptor.url,
                        config=config,
                        transport="download",
                        headers=req_headers,
                    ) as resp:
                        async for chunk in resp.aiter_bytes():
                            await asyncio.to_thread(tmp.write, chunk)
                            if hasher is not None:
                                hasher.update(chunk)
                            received += len(chunk)
                            if max_size is not None and received > max_size:
                                raise FileExchangeTransferError(
                                    TransferErrorCode.TOO_LARGE,
                                    transport="download",
                                    detail="artifact exceeds the configured max size",
                                )
                    break  # body read to completion without a connection error
                except FileExchangeTransferError:
                    raise  # guard refusal / too-large — not a resumable drop
                except (httpx.HTTPError, OSError) as exc:
                    attempts += 1
                    if attempts > _MAX_RECONNECTS:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="download",
                            detail="download interrupted and could not be resumed",
                        ) from exc
                    # loop: resume from `received` via a Range request
            await asyncio.to_thread(tmp.flush)
        finally:
            await asyncio.to_thread(tmp.close)

        # verify-before-use (computed during the single write pass)
        if expected_size is not None and received != expected_size:
            raise FileExchangeTransferError(
                TransferErrorCode.SIZE_MISMATCH,
                transport="download",
                detail="transferred byte count did not match declared size",
            )
        if handle.artifact.digest is not None and (
            unverifiable or hasher is None or hasher.hexdigest() != expected_hex
        ):
            raise FileExchangeTransferError(
                TransferErrorCode.DIGEST_MISMATCH,
                transport="download",
                detail="transferred bytes did not match declared digest",
            )
        # ingest: hand the sink a real sync fd (works whether it reads on the
        # loop or offloads — the async->sync bridge the temp file provides)
        f = await asyncio.to_thread(open, tmp_path, "rb")
        try:
            await sink.store_artifact(handle.artifact.id, handle.artifact, f)
        except FileExchangeTransferError:
            raise
        except Exception as exc:
            raise FileExchangeTransferError(
                TransferErrorCode.TRANSFER_FAILED,
                transport="download",
                detail="artifact transfer failed",
            ) from exc
        finally:
            await asyncio.to_thread(f.close)
    finally:
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.unlink, tmp_path)
