"""The ``upload`` transport data plane (#146).

Three role helpers (receiver mint, the PUT/POST serving route, sender) plus the
RFC 9530 / RFC 7231 HTTP helpers the route and sender need, free functions
mirroring ``_filesystem.py`` / ``_download.py``. The receiver mints a capability
URL backed by the #144 token store; the route streams the pushed body to a
transient temp file, verifies the declared ``Content-Digest`` and the ticket's
``expected`` constraints **before** handing the #142 ``ArtifactSink`` a real sync
fd, then consumes the single-use token only on a successful store; the sender
stages the artifact, computes a ``Content-Digest``, and pushes it through the
#147 ``guarded_stream``. See
``docs/superpowers/specs/2026-05-24-file-exchange-146-upload-data-plane-design.md``.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Literal

from starlette.responses import Response

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import _HASHLIB_BY_LABEL, _write_chunk
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

    from fastmcp_pvl_core._config import ServerConfig
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink
    from fastmcp_pvl_core._file_exchange._tokens import CapabilityTokenStore

logger = logging.getLogger(__name__)

# pvl-core's upload route shape (§12 capability URL path). A constant, not a
# kwarg — route structure is a pvl-core shape decision (mirrors DOWNLOAD_PREFIX).
UPLOAD_PREFIX = "/fx/u"

# Default digest algorithm for the receiver's recorded ArtifactMetadata.digest
# and the sender's Content-Digest (pvl-core's shape). Members of _HASHLIB_BY_LABEL
# are also accepted on an inbound Content-Digest.
_DEFAULT_DIGEST_LABEL = "sha-256"


async def upload_receiver_mint(
    artifact_id: str,
    *,
    token_store: CapabilityTokenStore,
    base_url: str,
    ttl: float,
    expected: ArtifactConstraints | None = None,
    method: Literal["PUT", "POST"] = "PUT",
) -> IntakeTicket:
    """Receiver role (push): mint an upload token and emit an IntakeTicket.

    ``artifact_id`` is the server's opaque identifier for the artifact slot,
    stored in the token for the route to correlate the pushed bytes; ``expected``
    is the §7.4 constraint set the route enforces at ingest (stored opaquely too).
    ``base_url`` is the server's public https origin; ``ttl`` is clamped by the
    token store's ceiling. Minting only — no hook runs and no bytes move (the
    sink is threaded into the route, where the bytes actually arrive).
    """
    minted = await token_store.mint(
        {
            "artifact_id": artifact_id,
            "expected": expected.model_dump(mode="json") if expected else None,
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


def _format_content_digest(label: str, raw: bytes) -> str:
    """Format a raw digest as an RFC 9530 ``Content-Digest`` member: ``label=:b64:``.

    The Structured-Field byte-sequence form (base64 wrapped in colons) — distinct
    from the wire ``ArtifactMetadata.digest`` field's ``label:hex`` form.
    """
    return f"{label}=:{base64.b64encode(raw).decode('ascii')}:"


def _parse_content_digest(header: str) -> tuple[str, bytes] | None:
    """Parse the first supported RFC 9530 ``Content-Digest`` member.

    Returns ``(label, raw_digest_bytes)`` for the first member whose algorithm is
    in :data:`_HASHLIB_BY_LABEL`, or ``None`` when the header carries no member
    for a supported algorithm. RFC 8941 member parameters (``;key=value`` after
    the byte sequence) are ignored. A member for a *supported* algorithm whose
    byte sequence is malformed (bad colon-wrapping or invalid base64) yields
    ``None`` rather than falling through to a later member — a supported digest
    we cannot verify is rejected, never silently skipped (§15). Members for
    unsupported algorithms are skipped so a supported member elsewhere in the
    dictionary still wins.
    """
    for member in header.split(","):
        label, sep, value = member.strip().partition("=")
        if sep != "=":
            continue
        label = label.strip().lower()
        if label not in _HASHLIB_BY_LABEL:
            continue
        value = value.split(";", 1)[0].strip()
        if len(value) < 2 or value[0] != ":" or value[-1] != ":":
            return None
        try:
            return label, base64.b64decode(value[1:-1], validate=True)
        except (binascii.Error, ValueError):
            return None
    return None


def _media_type_accepted(content_type: str | None, accept: list[str]) -> bool:
    """Match a request media type against RFC 7231 §3.1.1.1 media-ranges.

    ``type/subtype`` matches exactly; ``type/*`` matches any subtype of ``type``;
    ``*/*`` matches anything. Parameters (``; charset=...``) are ignored. A
    missing or malformed ``Content-Type`` matches nothing (the route rejects).
    """
    media = (content_type or "").split(";", 1)[0].strip().lower()
    if "/" not in media:
        return False
    main, sub = media.split("/", 1)
    for entry in accept:
        candidate = entry.split(";", 1)[0].strip().lower()
        if candidate == "*/*":
            return True
        if "/" not in candidate:
            continue
        cand_main, cand_sub = candidate.split("/", 1)
        if cand_main == main and cand_sub in ("*", sub):
            return True
    return False


def register_upload_route(
    mcp: FastMCP,
    *,
    token_store: CapabilityTokenStore,
    sink: ArtifactSink,
    config: ServerConfig,
) -> None:
    """Mount the ``upload`` PUT/POST route on ``mcp`` (serves §12 capability URLs).

    ``PUT``/``POST <UPLOAD_PREFIX>/{token}`` looks the token up, streams the
    request body to a transient temp file (hashing, bounded by the operator cap
    ``config.file_exchange_max_artifact_size`` and the ticket's
    ``expected.maxSize``), enforces ``acceptMimeTypes`` and verifies a declared
    ``Content-Digest`` **before** depositing through ``sink.store_artifact``, and
    consumes the single-use token only on a successful store (§10.3). Ambient
    credentials are ignored — the in-URL token is the only authorization.
    ``token_store``/``sink``/``config`` are threaded by #148.
    """
    max_artifact = config.file_exchange_max_artifact_size

    @mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT", "POST"])
    async def _serve_upload(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        artifact_id = rec.metadata["artifact_id"]
        expected_raw = rec.metadata.get("expected")
        expected: ArtifactConstraints | None = (
            ArtifactConstraints.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )

        content_type = request.headers.get("content-type")
        if (
            expected is not None
            and expected.acceptMimeTypes
            and not _media_type_accepted(content_type, expected.acceptMimeTypes)
        ):
            return Response(status_code=415)

        cd_header = request.headers.get("content-digest")
        require_digest = bool(expected is not None and expected.requireDigest)
        cd = _parse_content_digest(cd_header) if cd_header is not None else None
        if cd_header is not None and cd is None:
            return Response(status_code=400)
        if cd is None and require_digest:
            return Response(status_code=400)
        algo_label = cd[0] if cd is not None else _DEFAULT_DIGEST_LABEL

        size_cap = max_artifact
        if expected is not None and expected.maxSize is not None:
            size_cap = (
                expected.maxSize
                if size_cap is None
                else min(size_cap, expected.maxSize)
            )

        try:
            fd, tmp_path = tempfile.mkstemp(prefix="fx-upload-")
        except OSError:
            logger.exception("file-exchange: upload temp create failed")
            return Response(status_code=500)
        try:
            try:
                tmp = os.fdopen(fd, "wb")
            except OSError:
                with contextlib.suppress(OSError):
                    os.close(fd)
                logger.exception("file-exchange: upload temp open failed")
                return Response(status_code=500)
            hasher = hashlib.new(_HASHLIB_BY_LABEL[algo_label])
            received = 0
            try:
                try:
                    async for chunk in request.stream():
                        if not chunk:
                            continue
                        received += len(chunk)
                        if size_cap is not None and received > size_cap:
                            return Response(status_code=413)
                        await asyncio.to_thread(_write_chunk, tmp, hasher, chunk)
                    await asyncio.to_thread(tmp.flush)
                except OSError:
                    logger.exception("file-exchange: upload temp write failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    tmp.close()

            if cd is not None and hasher.digest() != cd[1]:
                return Response(status_code=400)

            meta = ArtifactMetadata(
                mimeType=content_type,
                size=received,
                digest=f"{algo_label}:{hasher.hexdigest()}",
            )
            try:
                ingest = await asyncio.to_thread(open, tmp_path, "rb")
            except OSError:
                logger.exception("file-exchange: upload staged open failed")
                return Response(status_code=500)
            try:
                await sink.store_artifact(artifact_id, meta, ingest)
            except Exception:
                logger.exception("file-exchange: upload sink store_artifact failed")
                return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    ingest.close()

            try:
                await token_store.consume(token)
            except Exception:
                logger.warning(
                    "file-exchange: upload token consume failed after store; "
                    "token may remain usable to TTL"
                )
            return Response(status_code=204)
        finally:
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp_path)
