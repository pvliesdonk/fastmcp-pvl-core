"""Mechanism-agnostic artifact byte-source / byte-sink hook protocols.

Downstream servers implement these to produce and deposit artifact bytes.
The transport that carries the bytes (a shared filesystem volume, an HTTPS
download/upload, ...) lives entirely behind the hook and MUST NOT appear in
its signature — a hook cannot tell which transport is in use. The two
protocols are exact mirrors over one synchronous BinaryIO.
"""

from __future__ import annotations

from typing import BinaryIO, Protocol, runtime_checkable

from fastmcp_pvl_core._file_exchange._wire import ArtifactMetadata


@runtime_checkable
class ArtifactSource(Protocol):
    """Downstream hook: produce the bytes for an artifact this server offers.

    Mechanism-agnostic. pvl-core bridges this to whatever transport carries
    the bytes; the transport never appears here.
    """

    async def open_artifact(self, key: str) -> tuple[BinaryIO, ArtifactMetadata]:
        """Return a readable byte stream plus the metadata the server knows.

        ``key`` is the server's own opaque identifier for the artifact it is
        offering (a domain key, not a wire field). The returned stream MUST be
        positioned at the first byte to transfer (typically the start): the
        caller reads it from its *current* position to completion and does not
        seek. The caller (pvl-core) closes the stream and computes/records
        size + digest itself — so the returned ``ArtifactMetadata`` need
        only carry what the server knows (e.g. name, mimeType). Raise on
        failure.
        """
        ...


@runtime_checkable
class ArtifactSink(Protocol):
    """Downstream hook: deposit the bytes for an artifact this server receives.

    The exact mirror of :class:`ArtifactSource`. Mechanism-agnostic.
    """

    async def store_artifact(
        self, artifact_id: str | None, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        """Read ``stream`` to completion and deposit its bytes durably.

        ``artifact_id`` is the wire id of the artifact being received (an
        ``IntakeTicket.artifactId`` on the push side, or a
        ``TransferHandle.artifact.id`` on the pull side); ``None`` when the
        handle's ``artifact.id`` field is absent (§7.1 makes it optional).
        The caller (pvl-core) owns ``stream`` and is responsible for
        verifying the artifact's size + digest by its own means (before or
        as the sink reads); the sink reads but does **not** close it, and
        MUST NOT assume the stream's concrete type. The caller closes
        ``stream`` and may delete its backing storage immediately after this
        method returns, so the sink MUST finish reading before returning (it
        may read on the loop or off-load to a thread, but MUST NOT retain the
        handle for deferred reads). Return ``None`` on success; raise on
        failure.
        """
        ...
