"""The ``upload`` transport data plane (#146).

This module accrues the upload-transport helpers task by task:

- ``upload_receiver_mint`` — receiver role: mint a single-use capability
  token and emit an :class:`IntakeTicket` carrying one
  :class:`UploadSink`.
- ``_content_digest_parse`` / ``_content_digest_format`` / ``_media_range_matches``
  — RFC 9530 Content-Digest dictionary-entry parse + format, and an RFC 7231
  media-range matcher used by the route to enforce ``acceptMimeTypes``.
- ``register_upload_route`` — mount ``PUT``/``POST <UPLOAD_PREFIX>/{token}``
  on a FastMCP server: streams the body to a temp file, verifies an optional
  ``Content-Digest`` before the sink sees the bytes, deposits to
  :class:`ArtifactSink`, and consumes the single-use token only after a
  successful store.
- The sender (``upload_sender_consume``) lands in a subsequent commit per
  the implementation plan.

See ``docs/superpowers/specs/2026-05-27-file-exchange-146-failure-modes.md``
for the enumerated failure modes each test in this module exercises and
``docs/superpowers/plans/2026-05-27-file-exchange-146-upload-data-plane-v4.md``
for the implementation plan this module is built against.
"""

from __future__ import annotations

import asyncio
import base64 as _b64
import binascii
import contextlib
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Literal, cast

from fastmcp_pvl_core._file_exchange._spec import SPEC_VERSION, TICKET_TYPE
from fastmcp_pvl_core._file_exchange._staging import (
    _CHUNK,
    _HASHLIB_BY_LABEL,
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
    from fastmcp_pvl_core._file_exchange._hooks import ArtifactSink
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


def _content_digest_parse(header: str) -> tuple[str, bytes] | None:
    """Parse an RFC 9530 ``Content-Digest`` structured-field dictionary entry.

    Returns ``(algo_label, raw_digest_bytes)`` on success, or ``None`` if no
    supported, well-formed entry is present. Unsupported algorithms within a
    dictionary are silently skipped (RFC 9530 §3: a recipient MUST ignore
    digest values associated with algorithms that it does not support); the
    function returns the first supported, well-formed entry it finds. ``None``
    is treated by the caller as a verification failure (``digest-mismatch``),
    never a silent skip of a header that did declare a known algorithm
    (matrix rows D4, D5; spec §10.3).

    Per RFC 8941 §3.2 a dictionary item may carry parameters
    (``sha-256=:<b64>:;foo=bar``); RFC 9530 §3 requires unknown parameters
    to be ignored, so they are stripped before the byte-sequence boundary
    check.
    """
    if not header:
        return None
    for entry in header.split(","):
        entry = entry.strip()
        if not entry:
            continue
        label, sep, rest = entry.partition("=")
        if sep != "=":
            continue
        label = label.strip().lower()
        if label not in _HASHLIB_BY_LABEL:
            continue
        item, _, _ = rest.strip().partition(";")
        item = item.strip()
        if not item.startswith(":") or not item.endswith(":") or len(item) < 2:
            continue
        b64 = item[1:-1]
        if not b64:
            continue
        try:
            raw = _b64.b64decode(b64, validate=True)
        except (binascii.Error, ValueError):
            continue
        return label, raw
    return None


def _content_digest_format(label: str, raw: bytes) -> str:
    """Format ``(label, raw_digest_bytes)`` as ``algo=:base64:`` per RFC 9530."""
    return f"{label}=:{_b64.b64encode(raw).decode('ascii')}:"


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
    """Mount ``PUT``/``POST <UPLOAD_PREFIX>/{token}`` on ``mcp``.

    The route serves §12 capability URLs minted by ``upload_receiver_mint``;
    ambient credentials are ignored — the in-URL token is the only
    authorization. ``config.file_exchange_max_artifact_size`` is the operator
    body-size cap; per-mint ``expected.maxSize`` is the smaller of the two when
    set. See the failure-mode matrix for the full per-status-code contract.
    """
    from starlette.responses import Response

    async def _handle(request: Request) -> Response:
        token = request.path_params["token"]
        rec = await token_store.lookup(token)
        if rec is None:
            return Response(status_code=404)
        artifact_id = cast("str", rec.metadata["artifact_id"])
        expected_raw = rec.metadata.get("expected")
        expected = (
            ArtifactConstraints.model_validate(expected_raw)
            if expected_raw is not None
            else None
        )

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
                hasher = hashlib.new("sha256")
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
                except OSError:
                    logger.exception("file-exchange: upload temp write failed")
                    return Response(status_code=500)
            finally:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(tmp.close)

            if too_large:
                return Response(status_code=413)

            cd_header = request.headers.get("content-digest")
            required = expected.requireDigest if expected is not None else None
            if cd_header is not None:
                parsed = _content_digest_parse(cd_header)
                if parsed is None:
                    return Response(status_code=400)
                cd_algo, cd_raw = parsed
                if required is not None and cd_algo not in required:
                    # requireDigest lists specific algorithms; a digest in a
                    # different algorithm does not satisfy the requirement.
                    return Response(status_code=400)
                if cd_algo == "sha-256":
                    if hasher.digest() != cd_raw:
                        return Response(status_code=400)
                else:
                    rehash = hashlib.new(_HASHLIB_BY_LABEL[cd_algo])
                    try:
                        with open(tmp_path, "rb") as fh:
                            while True:
                                buf = await asyncio.to_thread(fh.read, _CHUNK)
                                if not buf:
                                    break
                                rehash.update(buf)
                    except OSError:
                        logger.exception("file-exchange: upload rehash read failed")
                        return Response(status_code=500)
                    if rehash.digest() != cd_raw:
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

    mcp.custom_route(f"{UPLOAD_PREFIX}/{{token}}", methods=["PUT", "POST"])(_handle)
