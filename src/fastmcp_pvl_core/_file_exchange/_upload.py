"""The ``upload`` transport data plane (#146).

This module accrues the upload-transport helpers task by task:

- ``upload_receiver_mint`` — receiver role: mint a single-use capability
  token and emit an :class:`IntakeTicket` carrying one
  :class:`UploadSink`.
- ``_media_range_matches`` — RFC 7231 §3.1.1.1 media-range matcher
  used by the route to enforce ``acceptMimeTypes``. (The Content-Digest
  parse + policy lives in :mod:`._content_digest`.)
- ``register_upload_route`` — mount ``PUT <UPLOAD_PREFIX>/{token}``
  on a FastMCP server: streams the body to a temp file, verifies an optional
  ``Content-Digest`` before the sink sees the bytes, deposits to
  :class:`ArtifactSink`, and consumes the single-use token only after a
  successful store.
- ``upload_sender_consume`` — sender role (push): stage the source bytes to a
  transient temp file (hashing on the fly), then PUT them through the #147
  SSRF guard with ``Content-Type``/``Content-Length``/``Content-Digest``
  headers. Non-2xx maps to ``transfer-failed``; guard refusals propagate.

See ``docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md``
for the enumerated failure modes each test in this module exercises and
``docs/superpowers/plans/2026-05-27-file-exchange-146-upload-data-plane-v4.md``
for the implementation plan this module is built against.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import tempfile
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Literal

from fastmcp_pvl_core._file_exchange import _content_digest
from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream
from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
    _rehash_file,
    _write_chunk,
)
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import (
    ArtifactConstraints,
    ArtifactMetadata,
    IntakeTicket,
    UploadSink,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request
    from starlette.responses import Response

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore


logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.
UPLOAD_PREFIX = "/fx/u"

# pvl-core's upload route HTTP method. A constant, not a kwarg — the method
# choice is a shape decision (CLAUDE.md classification test: pvl-core can
# pick PUT and downstream has no domain-specific basis to disagree). If a
# future downstream genuinely needs POST, the resolution is to evolve the
# spec and migrate all downstreams, not to grow an override kwarg.
_UPLOAD_METHOD: Literal["PUT"] = "PUT"


async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
) -> IntakeTicket:
    """Receiver role (push): mint an upload token and emit an IntakeTicket.

    Mint-only — no hook is called, no bytes move. The token stores the
    ``artifact_id`` and the ``expected`` constraints so the route can
    correlate received bytes back to a wire id and enforce limits at
    deposit time. The token store treats the metadata as opaque (#144).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            # ``mode="json"`` coerces any forward-compat extra fields
            # (``_WireBase`` has ``extra="allow"``) to JSON-native
            # primitives so the KV backend's serialiser never sees a
            # Python object it cannot handle.
            "expected": (
                expected.model_dump(mode="json") if expected is not None else None
            ),
        },
        ttl=ttl,
        single_use=True,
    )
    url = capability_url(base_url, UPLOAD_PREFIX, minted.token)
    return IntakeTicket(
        type=TICKET_TYPE,
        version=SPEC_VERSION,
        artifactId=artifact_id,
        expected=expected,
        sinks=[
            UploadSink(
                transport="upload",
                url=url,
                method=_UPLOAD_METHOD,
                expiresAt=minted.expires_at,
            )
        ],
    )


def _media_range_matches(content_type: str, accept: list[str]) -> bool:
    """RFC 7231 §3.1.1.1 media-range match: parameters ignored, case-insensitive.

    ``type/*`` matches any subtype; ``*/*`` matches anything. The
    grammatically invalid ``*/subtype`` form is rejected, not treated as
    a wildcard. An empty ``content_type`` or an empty ``accept`` list
    never matches (matrix rows F3, F4).
    """
    if not content_type or not accept:
        return False
    main = content_type.split(";", 1)[0].strip().lower()
    if "/" not in main:
        return False
    main_type, main_sub = main.split("/", 1)
    for entry in accept:
        if not entry:
            continue
        entry_main = entry.split(";", 1)[0].strip().lower()
        if "/" not in entry_main:
            continue
        e_type, e_sub = entry_main.split("/", 1)
        if e_type == "*" and e_sub != "*":
            # RFC 7231 §3.1.1.1 grammar only allows */* and type/*; reject
            # the invalid */subtype form rather than silently honouring it.
            continue
        if (e_type == "*" or e_type == main_type) and (
            e_sub == "*" or e_sub == main_sub
        ):
            return True
    return False


def register_upload_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    sink: ArtifactSink,
    config: ServerConfig,
) -> None:
    """Mount ``PUT <UPLOAD_PREFIX>/{token}`` on ``mcp``.

    The route serves §12 capability URLs minted by ``upload_receiver_mint``;
    ambient credentials are ignored — the in-URL token is the only
    authorization. ``config.file_exchange_max_artifact_size`` is the operator
    body-size cap; per-mint ``expected.maxSize`` is the smaller of the two when
    set. See the failure-mode matrix for the full per-status-code contract.

    Kwargs (per CLAUDE.md classification):

    - ``token_store`` (**shape**): the capability-token store. pvl-core owns
      the token-store contract; downstream constructs but does not subclass.
    - ``sink`` (**hook**): downstream's ``ArtifactSink`` implementation.
      pvl-core cannot answer "where do these bytes go?" — domain hook.
    - ``config`` (**config**): operator-side ``ServerConfig`` carrying
      ``file_exchange_max_artifact_size`` (the body-size cap).
    """
    from starlette.requests import ClientDisconnect
    from starlette.responses import Response

    async def _handle(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        # A unified token_store (#148) holds both download and upload tokens;
        # a download-minted token presented to the upload route has no
        # ``artifact_id`` key. Treat the cross-transport case as ``404``
        # (uniform with the unknown-token branch above, no token-state leak).
        artifact_id = rec.metadata.get("artifact_id")
        if artifact_id is None:
            return Response(status_code=404)
        expected_raw = rec.metadata.get("expected")
        try:
            expected = (
                ArtifactConstraints.model_validate(expected_raw)
                if expected_raw is not None
                else None
            )
        except Exception:
            # KV-stored metadata that fails Pydantic validation should
            # never happen (the mint side validated before storing) but
            # KV corruption / schema-version drift can produce it. Log
            # and return a structured 500 rather than letting a
            # ValidationError escape unwrapped to ASGI.
            logger.exception(
                "file-exchange: upload expected-constraints deserialise failed"
            )
            return Response(status_code=500)

        content_type = request.headers.get("content-type", "")
        if expected is not None and expected.acceptMimeTypes is not None:
            if not _media_range_matches(content_type, expected.acceptMimeTypes):
                return Response(status_code=415)

        cap_per_mint = expected.maxSize if expected is not None else None
        cap_operator = config.file_exchange_max_artifact_size
        cap: int | None
        if cap_per_mint is None:
            cap = cap_operator
        elif cap_operator is None:
            cap = cap_per_mint
        else:
            cap = min(cap_per_mint, cap_operator)

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError:
            logger.exception("file-exchange: upload mkstemp failed")
            return Response(status_code=500)
        try:
            try:
                tmp = os.fdopen(fd, "wb")
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            try:
                try:
                    # Use the canonical mapping so the streaming hasher
                    # and the rehash branch can never drift apart.
                    hasher = hashlib.new(_HASHLIB_BY_LABEL["sha-256"])
                except Exception:
                    logger.exception("file-exchange: upload hasher init failed")
                    return Response(status_code=500)
                received = 0
                too_large = False
                try:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        if cap is not None and received + len(chunk) > cap:
                            too_large = True
                            break
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                        received += len(chunk)
                except ClientDisconnect:
                    # Client dropped mid-upload — no response needed; outer
                    # finally still unlinks the temp.
                    logger.debug("file-exchange: upload client disconnected mid-stream")
                    raise
                except OSError:
                    logger.exception("file-exchange: upload temp write failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            if too_large:
                return Response(status_code=413, headers={"Connection": "close"})

            cd_header = request.headers.get("content-digest")
            required = expected.requireDigest if expected is not None else None
            if cd_header is not None:
                parsed = _content_digest.parse_header(cd_header, preferred=required)
                if parsed is None:
                    return Response(status_code=400)
                cd_algo, cd_raw = parsed
                if not _content_digest.satisfies_requirement(cd_algo, required):
                    # The parser tried ``preferred=required`` first, so
                    # arriving here means the client's header contained no
                    # entry whose algorithm is in ``required`` — only a
                    # fallback entry in a non-required algorithm.
                    return Response(status_code=400)
                if cd_algo == "sha-256":
                    if hasher.digest() != cd_raw:
                        return Response(status_code=400)
                else:
                    try:
                        rehash_digest = await asyncio.to_thread(
                            _rehash_file, tmp_path, _HASHLIB_BY_LABEL[cd_algo]
                        )
                    except OSError:
                        logger.exception("file-exchange: upload rehash read failed")
                        return Response(status_code=500)
                    if rehash_digest != cd_raw:
                        return Response(status_code=400)
            elif required is not None:
                return Response(status_code=400)

            meta = ArtifactMetadata(
                mimeType=content_type or None,
                size=received,
                digest="sha-256:" + hasher.hexdigest(),
            )
            try:
                f = await asyncio.to_thread(open, tmp_path, "rb")
            except OSError:
                logger.exception("file-exchange: upload temp re-open failed")
                return Response(status_code=500)
            try:
                try:
                    await sink.store_artifact(artifact_id, meta, f)
                except Exception:
                    logger.exception("file-exchange: upload sink store_artifact failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(f.close)

            try:
                consumed = await token_store.consume(token)
            except Exception:
                # At-most-once *delivery*, not at-most-once *deposit*: the
                # sink already stored these bytes, but the consume failure
                # left the token live, so a retry by the sender (e.g. on
                # TCP timeout) will lookup-then-store again. Non-idempotent
                # sinks need to be aware of this; pvl-core does not retry
                # the consume internally.
                logger.warning(
                    "file-exchange: upload token consume failed after store; "
                    "token may remain usable to TTL and a sender retry would "
                    "deposit the same artifact a second time"
                )
            else:
                if not consumed:
                    # The store wrote, but the token was already gone
                    # (concurrent PUT raced and consumed first, or a
                    # revoke landed between lookup and consume). The
                    # bytes are still in the sink — observability only,
                    # not an error.
                    logger.info(
                        "file-exchange: upload token already consumed at deposit "
                        "time (concurrent race or revoke); bytes are in the sink"
                    )
            return Response(status_code=204)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)

    # PUT only: ``upload_receiver_mint`` advertises ``method="PUT"`` (a
    # pvl-core shape decision per ``_UPLOAD_METHOD``), so the route never
    # needs to honour POST. Accepting it would let clients use a method
    # pvl-core never minted, which is dead surface.
    mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT"])(_handle)


async def upload_sender_consume(
    sink: UploadSink,
    source: ArtifactSource,
    key: str,
    *,
    config: ServerConfig,
) -> None:
    """Sender role (push): stage ``source[key]`` and PUT it to ``sink.url``.

    Streams the source bytes through a transient temp file (hashing on the
    fly), then sends the temp through the #147 SSRF guard with
    ``Content-Type`` / ``Content-Length`` / ``Content-Digest`` headers.
    Non-2xx maps to ``transfer-failed``; guard refusals propagate verbatim.
    The temp is deleted on every path (matrix rows B2, B3, B5, C3, D1, D2,
    D3, D9, F6).
    """
    stream, metadata = await source.open_artifact(key)
    try:
        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError as exc:
            raise FileExchangeTransferError(
                TransferErrorCode.TRANSFER_FAILED,
                transport="upload",
                detail="failed to create a temporary file",
            ) from exc
        try:
            try:
                tmp = os.fdopen(fd, "wb")
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise
            try:
                hasher = hashlib.new("sha256")
                size = 0
                while True:
                    chunk = await asyncio.to_thread(stream.read, _CHUNK)
                    if not chunk:
                        break
                    try:
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                    except OSError as exc:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="upload",
                            detail="failed to stage the artifact",
                        ) from exc
                    size += len(chunk)
                try:
                    await asyncio.to_thread(tmp.flush)
                except OSError as exc:
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="failed to flush the staged artifact",
                    ) from exc
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            cd_header = _content_digest.format_header("sha-256", hasher.digest())
            headers: dict[str, str] = {
                "Content-Length": str(size),
                "Content-Digest": cd_header,
            }
            if metadata.mimeType:
                headers["Content-Type"] = metadata.mimeType

            async def _body() -> AsyncIterator[bytes]:
                try:
                    f = await asyncio.to_thread(open, tmp_path, "rb")
                except OSError as exc:
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="failed to read the staged artifact",
                    ) from exc
                try:
                    while True:
                        try:
                            chunk = await asyncio.to_thread(f.read, _CHUNK)
                        except OSError as exc:
                            raise FileExchangeTransferError(
                                TransferErrorCode.TRANSFER_FAILED,
                                transport="upload",
                                detail="failed to read the staged artifact",
                            ) from exc
                        if not chunk:
                            break
                        yield chunk
                finally:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(f.close)

            async with guarded_stream(
                sink.method,
                sink.url,
                config=config,
                transport="upload",
                headers=headers,
                content=_body(),
            ) as resp:
                if not (200 <= resp.status < 300):
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="unexpected upload response status",
                    )
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)
    finally:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)
