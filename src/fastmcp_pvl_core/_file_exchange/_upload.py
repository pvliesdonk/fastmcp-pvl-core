"""The ``upload`` transport data plane (#146).

Two role helpers (receiver, sender — ``upload`` is push-only) plus a serving
route, mirroring ``_download.py``. The receiver mints a capability URL backed
by the #144 token store; the upload ``PUT``/``POST`` route accepts the artifact
bytes, streams them to a transient temp file (hashing + size-bounded), verifies
the ``Content-Digest`` (RFC 9530) and constraints **before** the sink sees the
bytes (verify-before-use, §15), deposits them through the #142
``ArtifactSink``, and consumes the single-use token only on a successful store.
The sender stages the source bytes to a temp (hashing in a single pass), then
pushes through the #147 ``guarded_stream`` with ``Content-Digest``. See
``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``.

``UPLOAD_PREFIX`` is a pvl-core route-shape constant, not a kwarg and not
exported — route structure is a pvl-core shape decision (``CLAUDE.md``).

Concurrency note: ``single-use`` means the first successful store burns the
token (§10.3), not a concurrency lock. Two concurrent PUTs begun before either
completes can both pass verification and reach ``store_artifact`` before one
``consume`` wins. The ``ArtifactSink`` MUST therefore tolerate duplicate
``store_artifact`` calls for the same ``artifact_id`` within the token's TTL.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import logging
import os
import tempfile
from collections.abc import AsyncGenerator
from typing import TYPE_CHECKING, Literal

import httpx
from starlette.responses import Response

from fastmcp_pvl_core._file_exchange._codes import TransferErrorCode
from fastmcp_pvl_core._file_exchange._errors import FileExchangeTransferError
from fastmcp_pvl_core._file_exchange._outbound import guarded_stream
from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
    _write_chunk,
)
from fastmcp_pvl_core._file_exchange._tokens import capability_url
from fastmcp_pvl_core._file_exchange._wire import ArtifactConstraints, UploadSink

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from starlette.requests import Request

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink, ArtifactSource
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore
    from fastmcp_pvl_core._file_exchange._wire import IntakeTicket

logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision.  Not re-exported.
UPLOAD_PREFIX = "/fx/u"

# pvl-core always sends (and prefers) sha-256. The supported set is inherited
# from _HASHLIB_BY_LABEL; this is just the default.
_DEFAULT_DIGEST_LABEL = "sha-256"


# ---------------------------------------------------------------------------
# RFC 9530 Content-Digest helpers
# ---------------------------------------------------------------------------


def _format_content_digest(algo: str, raw_digest: bytes) -> str:
    """Format a raw digest as an RFC 9530 Structured-Field member.

    Returns e.g. ``sha-256=:<base64>:``. ``algo`` must be a key in
    ``_HASHLIB_BY_LABEL``; callers are responsible for validation.
    """
    return f"{algo}=:{base64.b64encode(raw_digest).decode()}:"


def _parse_content_digest(header: str | None) -> tuple[str, bytes] | None:
    """Parse the first supported member of a ``Content-Digest`` header.

    Implements first-supported-member semantics (§15): iterates members in
    order; the first member whose algorithm is in ``_HASHLIB_BY_LABEL`` is
    returned as ``(label, raw_bytes)``. An unsupported algorithm member is
    skipped (a later supported member can still win). A supported-but-malformed
    member returns ``None`` immediately — it does NOT fall through to a later
    member (§15 "never silently skip a supported digest").

    RFC 8941 member parameters (e.g. ``sha-256;foo=bar=:...:`` are stripped
    before processing.

    Returns ``None`` when the header is absent, when no supported member is
    present, or when a supported member is malformed or carries an empty
    digest.
    """
    if header is None:
        return None
    for raw_member in header.split(","):
        raw_member = raw_member.strip()
        if not raw_member:
            continue
        # Strip RFC 8941 member parameters (e.g. `;foo=bar`) before the value.
        # The parameter separator appears after the key= but before the value :...:
        # e.g. "sha-256;params=:b64:". Split on ";" to isolate any params, then
        # re-join key= with the value portion.
        algo_part, _, rest = raw_member.partition("=")
        algo_part = algo_part.strip()
        if not algo_part:
            continue
        # Strip any RFC 8941 parameters from the algo name (e.g. algo;p=1)
        algo_label = algo_part.split(";", 1)[0].strip()
        # Strip RFC 8941 parameters from the value portion before the colon-wrap.
        value_raw = rest.strip()
        value = value_raw.split(";", 1)[0].strip() if ";" in value_raw else value_raw
        if algo_label.lower() not in _HASHLIB_BY_LABEL:
            # Unsupported algorithm — skip; a later supported member may win.
            continue
        # Supported algorithm found. If malformed, return None immediately
        # (never fall through to a later member per §15).
        if not value.startswith(":") or not value.endswith(":"):
            return None  # supported but malformed — no fall-through
        inner = value[1:-1]
        # length guard: "::" is a 2-char string that decodes to b"" (empty digest).
        if len(value) <= 2:
            return None  # empty digest — reject
        try:
            raw_bytes = base64.b64decode(inner, validate=True)
        except Exception:
            return None
        if not raw_bytes:
            return None  # decoded to empty — reject
        return algo_label.lower(), raw_bytes
    return None


def _media_type_accepted(
    content_type: str | None, accept_mime_types: list[str]
) -> bool:
    """Return True iff ``content_type`` matches any pattern in ``accept_mime_types``.

    Applies RFC 7231 §3.1.1.1 media-range matching: parameters are stripped,
    ``type/*`` matches any subtype of ``type``, ``*/*`` matches anything.
    A missing (``None``) ``content_type`` is treated as no match.
    """
    if content_type is None:
        return False
    # Strip parameters: "text/plain; charset=utf-8" -> "text/plain"
    media = content_type.split(";", 1)[0].strip().lower()
    if not media or "/" not in media:
        return False
    req_type, _, req_subtype = media.partition("/")
    for pattern in accept_mime_types:
        pattern = pattern.strip().lower()
        if pattern == "*/*":
            return True
        pat_type, _, pat_sub = pattern.partition("/")
        if pat_type == req_type and (pat_sub == "*" or pat_sub == req_subtype):
            return True
    return False


# ---------------------------------------------------------------------------
# upload_receiver_mint
# ---------------------------------------------------------------------------


async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
    method: Literal["PUT", "POST"] = "PUT",
) -> IntakeTicket:
    """Receiver role (push): mint an upload capability token and emit an IntakeTicket.

    Minting only — no hook is called and no bytes move (mirrors
    ``filesystem_receiver_mint``). The ``ArtifactSink`` is threaded into the
    route (not into mint), since bytes arrive at the route, not at mint time.

    The token stores ``{"artifact_id": artifact_id, "expected": ...}`` so
    the route can correlate received bytes and enforce constraints. The token
    store treats the metadata as opaque (#144).

    ``base_url`` must be an ``https://`` origin (§12). ``ttl`` is clamped by
    the token store's ceiling. A token-store failure propagates unwrapped
    (offering-side op, per §16 — same as ``download_provider_mint``).
    """
    from fastmcp_pvl_core._file_exchange._wire import IntakeTicket

    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump() if expected is not None else None,
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
                method=method,
                expiresAt=minted.expires_at,
            )
        ],
    )


# ---------------------------------------------------------------------------
# register_upload_route
# ---------------------------------------------------------------------------


def register_upload_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    sink: ArtifactSink,
    config: ServerConfig,
) -> None:
    """Mount the ``upload`` PUT/POST route on ``mcp`` (serves §12 capability URLs).

    ``PUT``/``POST <UPLOAD_PREFIX>/{token}`` looks the token up in
    ``token_store``, streams the request body to a transient temp file
    (hashing + size-bounded), verifies ``Content-Digest`` (RFC 9530) and
    ``expected`` constraints **before** calling ``sink.store_artifact``
    (verify-before-use, §15), and consumes the single-use token only on
    a successful store (§10.3). Ambient credentials are ignored — the
    in-URL token is the only authorization.

    ``single-use`` means the first successful store burns the token
    (§10.3), not a concurrency lock: two concurrent PUTs begun before
    either completes can both pass verification and reach ``store_artifact``
    before one ``consume`` wins. The ``ArtifactSink`` MUST therefore
    tolerate duplicate ``store_artifact`` calls for the same ``artifact_id``
    within the token's TTL window.

    First-supported-member semantics for ``Content-Digest``: a sender that
    includes multiple members MUST place a required algorithm first.
    """
    size_cap = config.file_exchange_max_artifact_size

    @mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT", "POST"])
    async def _handle_upload(request: Request) -> Response:
        token = request.path_params["token"]

        # Step 1: look up token — 404 for unknown/expired/consumed (leaks no state).
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)

        # Step 2: re-hydrate metadata from the opaque token payload.
        artifact_id: str = rec.metadata["artifact_id"]
        expected_raw = rec.metadata.get("expected")
        expected: ArtifactConstraints | None = (
            ArtifactConstraints.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )

        # Step 3: acceptMimeTypes check (RFC 7231 media-range) — 415, no consume.
        if expected is not None and expected.acceptMimeTypes:
            if not _media_type_accepted(
                request.headers.get("content-type"),
                expected.acceptMimeTypes,
            ):
                return Response(status_code=415)

        # Step 4: stream request body to temp file (hashing + size-bounded).
        # Enforce the byte bound mid-stream: reject 413 when received count
        # exceeds min(expected.maxSize, config cap) — whichever is set.
        ticket_cap: int | None = expected.maxSize if expected is not None else None
        effective_cap: int | None = None
        if size_cap is not None and ticket_cap is not None:
            effective_cap = min(size_cap, ticket_cap)
        elif size_cap is not None:
            effective_cap = size_cap
        elif ticket_cap is not None:
            effective_cap = ticket_cap

        # Determine which digest algorithm the sender is expected to use.
        # We hash with sha-256 always (for ArtifactMetadata.digest) AND with
        # whatever algo the Content-Digest declares (for verification).
        # If the sender used sha-256, a single hasher serves both purposes.
        sha256_hasher = hashlib.new("sha256")

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError:
            logger.exception("file-exchange: upload failed to create temp file")
            return Response(status_code=500)

        tmp_path_to_clean: str | None = tmp_path
        try:
            # Open the fd; guard against fd leak on BaseException (A2).
            try:
                tmp = os.fdopen(fd, "wb")
            except OSError:
                with contextlib.suppress(OSError):
                    os.close(fd)
                logger.exception("file-exchange: upload fdopen failed")
                return Response(status_code=500)
            except BaseException:
                with contextlib.suppress(OSError):
                    os.close(fd)
                raise

            received = 0
            try:
                # Stream body chunks; only _write_chunk and flush are wrapped
                # in OSError (A3 — ClientDisconnect is non-OSError and propagates).
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if effective_cap is not None and received > effective_cap:
                        # Close the temp before returning 413 (the outer finally
                        # will unlink; close here so the file handle is freed).
                        with contextlib.suppress(OSError):
                            await asyncio.to_thread(tmp.close)
                        return Response(status_code=413)
                    try:
                        await asyncio.to_thread(_write_chunk, tmp, sha256_hasher, chunk)
                    except OSError:
                        logger.exception("file-exchange: upload temp write failed")
                        with contextlib.suppress(OSError):
                            await asyncio.to_thread(tmp.close)
                        return Response(status_code=500)
                try:
                    await asyncio.to_thread(tmp.flush)
                except OSError:
                    logger.exception("file-exchange: upload temp flush failed")
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(tmp.close)
                    return Response(status_code=500)
            finally:
                # Suppress close failure so it can't replace an in-flight error.
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            # Step 5: verify-before-use — check Content-Digest and requireDigest.
            cd_header = request.headers.get("content-digest")
            cd = _parse_content_digest(cd_header)

            # A present-but-unverifiable Content-Digest is a verification failure,
            # never a silent skip (§15).
            if cd_header is not None and cd is None:
                return Response(status_code=400)

            # Determine which algorithms are required.
            required_algos: set[str] | None = None
            if expected is not None and expected.requireDigest:
                required_algos = {a.lower() for a in expected.requireDigest}

            if cd is None and required_algos is not None:
                # Digest is required but absent.
                return Response(status_code=400)
            if (
                cd is not None
                and required_algos is not None
                and cd[0] not in required_algos
            ):
                # Digest present but wrong algorithm.
                return Response(status_code=400)

            if cd is not None:
                # Verify the digest with the declared algorithm.
                algo_label, declared_raw = cd
                if algo_label == "sha-256":
                    # Re-use the already-computed sha-256 hash.
                    verify_hasher: hashlib._Hash = sha256_hasher
                else:
                    # Need a fresh hasher for a different algo — re-read the temp.
                    verify_hasher = hashlib.new(_HASHLIB_BY_LABEL[algo_label])
                    try:
                        f_verify = await asyncio.to_thread(open, tmp_path, "rb")
                    except OSError:
                        logger.exception(
                            "file-exchange: upload cannot re-open temp for verification"
                        )
                        return Response(status_code=500)
                    try:
                        try:
                            while True:
                                vchunk = await asyncio.to_thread(f_verify.read, _CHUNK)
                                if not vchunk:
                                    break
                                verify_hasher.update(vchunk)
                        except OSError:
                            logger.exception("file-exchange: upload verify-read failed")
                            return Response(status_code=500)
                    finally:
                        with contextlib.suppress(OSError):
                            await asyncio.to_thread(f_verify.close)

                computed = verify_hasher.digest()
                # Constant-time comparison with length pre-check (E2).
                if len(computed) != len(declared_raw) or not hmac.compare_digest(
                    computed, declared_raw
                ):
                    return Response(status_code=400)

            # Step 6: construct metadata and call sink.store_artifact.
            from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata

            content_type = request.headers.get("content-type")
            # Strip parameters from mime type for the metadata field.
            mime = content_type.split(";", 1)[0].strip() if content_type else None
            sha256_hex = sha256_hasher.hexdigest()
            meta = ArtifactMetadata(
                mimeType=mime if mime else None,
                size=received,
                digest=f"sha-256:{sha256_hex}",
            )

            try:
                ingest = await asyncio.to_thread(open, tmp_path, "rb")
            except OSError:
                logger.exception("file-exchange: upload cannot open temp for sink")
                return Response(status_code=500)
            try:
                try:
                    await sink.store_artifact(artifact_id, meta, ingest)
                except Exception:
                    # Sink failure — log locally, never echo. Token NOT consumed.
                    logger.exception("file-exchange: upload sink store_artifact failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(ingest.close)

            # Step 7: consume the single-use token after successful store (M1).
            try:
                await token_store.consume(token)
            except Exception:
                # The bytes are stored; failing the request would mislead the client.
                # Log with traceback (exception, not warning) and return 204 anyway.
                logger.exception(
                    "file-exchange: upload token consume failed after successful "
                    "store; token may remain usable to TTL"
                )

            # Step 8: success — body-free 204.
            return Response(status_code=204)

        finally:
            # Temp file is deleted on every path (success and all error paths).
            if tmp_path_to_clean is not None:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(os.unlink, tmp_path_to_clean)


# ---------------------------------------------------------------------------
# upload_sender_consume
# ---------------------------------------------------------------------------


async def upload_sender_consume(
    sink: UploadSink,
    source: ArtifactSource,
    key: str,
    *,
    config: ServerConfig,
    digest_algo: str = _DEFAULT_DIGEST_LABEL,
) -> None:
    """Sender role (push): stage ``key``'s bytes and push to ``sink``.

    Selection (``select_sink``) is the caller's step. Opens
    ``source.open_artifact(key)`` and stages the bytes to a transient temp
    file in a single pass (hashing with ``digest_algo``). Staging is
    required because the hook stream is non-seekable and ``Content-Digest``
    must be computed before the header is sent. Pushes via
    ``guarded_stream`` (§147 SSRF guard) with ``Content-Type``,
    ``Content-Length``, and ``Content-Digest`` headers. Strips ambient
    credentials (guard handles that). Deletes the temp on every path.

    Raises :class:`FileExchangeTransferError`:

    - ``not-accessible`` — guard refusal (SSRF check, non-https, etc.).
    - ``transfer-failed`` — any other failure (source hook, temp I/O,
      non-2xx response, network error).

    A non-2xx response is always ``transfer-failed`` — the route's
    status codes are pvl-core shape, not §13 codes.

    Args:
        sink: the selected ``UploadSink`` descriptor (from ``select_sink``).
        source: the ``ArtifactSource`` hook for this server.
        key: opaque artifact key for ``source.open_artifact``.
        config: server configuration (SSRF guard, timeout).
        digest_algo: RFC 9530 label to use for ``Content-Digest``
            (default ``sha-256``; must be in ``_HASHLIB_BY_LABEL``).

    Raises:
        ValueError: ``digest_algo`` is not in the supported set.
    """
    if digest_algo not in _HASHLIB_BY_LABEL:
        raise ValueError(
            f"upload_sender_consume: digest_algo {digest_algo!r} not in supported set "
            f"{sorted(_HASHLIB_BY_LABEL)}"
        )

    # Stage: open the source hook and write bytes to a temp file, hashing as we go.
    # Open the source hook FIRST (B1 — wrap hook failures as TRANSFER_FAILED).
    # Acquiring this before the temp file means a hook failure leaks nothing.
    try:
        stream, meta = await source.open_artifact(key)
    except FileExchangeTransferError:
        raise
    except Exception as exc:
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="upload",
            detail="failed to open the artifact source",
        ) from exc

    # Now create the temp file. On failure, close the hook stream we just got.
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-send-")
    except OSError as exc:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(stream.close)
        raise FileExchangeTransferError(
            TransferErrorCode.TRANSFER_FAILED,
            transport="upload",
            detail="failed to create a temporary staging file",
        ) from exc

    try:
        # Open the fd; guard against fd leak on OSError AND BaseException (A2).
        # On both failure paths, close both the fd and the hook stream.
        try:
            tmp = os.fdopen(fd, "wb")
        except OSError as exc:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)
            raise FileExchangeTransferError(
                TransferErrorCode.TRANSFER_FAILED,
                transport="upload",
                detail="failed to open the staging file",
            ) from exc
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(fd)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)
            raise

        hasher = hashlib.new(_HASHLIB_BY_LABEL[digest_algo])
        size = 0

        try:
            # Staging loop (A4): separate scopes for _write_chunk, flush, outer.
            try:
                while True:
                    chunk = await asyncio.to_thread(stream.read, _CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    try:
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                    except OSError as exc:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="upload",
                            detail="failed to stage the artifact for upload",
                        ) from exc
                try:
                    await asyncio.to_thread(tmp.flush)
                except OSError as exc:
                    raise FileExchangeTransferError(
                        TransferErrorCode.TRANSFER_FAILED,
                        transport="upload",
                        detail="failed to stage the artifact for upload",
                    ) from exc
            except FileExchangeTransferError:
                raise  # re-raise without re-wrapping (A4 essential)
            except Exception as exc:
                raise FileExchangeTransferError(
                    TransferErrorCode.TRANSFER_FAILED,
                    transport="upload",
                    detail="artifact source read failed",
                ) from exc
        finally:
            # Suppress close failure so it can't replace an in-flight error (A1).
            with contextlib.suppress(OSError):
                await asyncio.to_thread(tmp.close)
            # Also close the source stream.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(stream.close)

        # Build the headers for the upload request.
        digest_header = _format_content_digest(digest_algo, hasher.digest())
        req_headers: dict[str, str] = {
            "Content-Length": str(size),
            "Content-Digest": digest_header,
        }
        if meta.mimeType is not None:
            req_headers["Content-Type"] = meta.mimeType

        # Push via guarded_stream: stream from the temp file (not in memory).
        # C1/C2: async generator, explicit aclose() in finally.
        async def _content() -> AsyncGenerator[bytes, None]:
            handle = await asyncio.to_thread(open, tmp_path, "rb")
            try:
                while True:
                    chunk = await asyncio.to_thread(handle.read, _CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(handle.close)

        content_gen = _content()
        try:
            # B2: catch both httpx.HTTPError and OSError around guarded_stream.
            try:
                async with guarded_stream(
                    sink.method,
                    sink.url,
                    config=config,
                    transport="upload",
                    headers=req_headers,
                    content=content_gen,
                ) as resp:
                    if not 200 <= resp.status < 300:
                        raise FileExchangeTransferError(
                            TransferErrorCode.TRANSFER_FAILED,
                            transport="upload",
                            detail="upload endpoint returned a non-success status",
                        )
            except FileExchangeTransferError:
                raise
            except httpx.HTTPError as exc:
                raise FileExchangeTransferError(
                    TransferErrorCode.TRANSFER_FAILED,
                    transport="upload",
                    detail="upload transfer failed due to a network error",
                ) from exc
            except OSError as exc:
                raise FileExchangeTransferError(
                    TransferErrorCode.TRANSFER_FAILED,
                    transport="upload",
                    detail="failed to read the staged artifact during upload",
                ) from exc
        finally:
            with contextlib.suppress(Exception):
                await content_gen.aclose()

    finally:
        # Temp is deleted on every path (success and all error paths).
        with contextlib.suppress(OSError):
            await asyncio.to_thread(os.unlink, tmp_path)
