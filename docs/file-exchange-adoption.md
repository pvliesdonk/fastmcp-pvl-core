# File-exchange adoption guide

One minimal worked example per role. Each example uses an in-memory
source/sink so the example doesn't drag in a storage backend; replace
those with real implementations when adopting.

For the protocol overview, see [`docs/file-exchange.md`](file-exchange.md).

## 1. Provider — offering an artifact

A server that offers reports. The MCP tool's input is a domain-specific
`report_id`; the response is the `TransferHandle` peers pass to their
fetcher.

```python
import io
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSource:
    def __init__(self, reports: dict[str, bytes]) -> None:
        self._reports = reports

    async def open_artifact(self, key: str):
        body = self._reports[key]
        return io.BytesIO(body), ArtifactMetadata(mimeType="application/pdf")


mcp = FastMCP("report-server")
source = _MemSource({"rpt-1": b"…PDF bytes…"})
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(kv_store_url="memory://"),
    base_url="https://reports.example",
    source=source,
)


@file_exchange.register_file_exchange_provider(mcp, "get_report", fxctx)
async def get_report(report_id: str) -> tuple[ArtifactMetadata, str]:
    body = source._reports[report_id]  # real impl: lookup_meta(report_id)
    return ArtifactMetadata(size=len(body), mimeType="application/pdf"), report_id
```

## 2. Fetcher — importing a report from a peer

A server that consumes a `TransferHandle` handed to it via the
fetcher tool and stores the bytes in its sink.

```python
from typing import BinaryIO
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSink:
    def __init__(self) -> None:
        self.imports: dict[str, bytes] = {}

    async def store_artifact(
        self, artifact_id, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        self.imports[artifact_id or "anonymous"] = stream.read()


mcp = FastMCP("import-server")
sink = _MemSink()
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_allowed_networks=("10.0.0.0/8",),
    ),
    base_url="https://imports.example",
    sink=sink,
)
file_exchange.register_file_exchange_fetcher(mcp, "consume_transfer", fxctx)
```

The peer-facing tool signature is `consume_transfer(handle: TransferHandle) -> None`. Wire-format dicts are validated by Pydantic automatically.

## 3. Receiver — accepting uploads

A server that mints an `IntakeTicket` for peers to push to.

```python
from typing import BinaryIO
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactConstraints, ArtifactMetadata


class _MemSink:
    def __init__(self) -> None:
        self.intake: dict[str, bytes] = {}

    async def store_artifact(
        self, artifact_id, metadata: ArtifactMetadata, stream: BinaryIO
    ) -> None:
        if artifact_id is not None:
            self.intake[artifact_id] = stream.read()


mcp = FastMCP("intake-server")
sink = _MemSink()
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_max_artifact_size=10 * 1024 * 1024,
    ),
    base_url="https://intake.example",
    sink=sink,
)


@file_exchange.register_file_exchange_receiver(mcp, "accept_attachment", fxctx)
async def accept_attachment(case_id: str) -> tuple[str, ArtifactConstraints | None]:
    return f"case-{case_id}-attachment", ArtifactConstraints(maxSize=10 * 1024 * 1024)
```

## 4. Sender — sending to a peer

A server that pushes a local artifact to a peer's receiver. The sender
tool takes the `IntakeTicket` the peer's receiver returned plus the
local `key` for the artifact being sent.

```python
import io
from fastmcp import FastMCP
from fastmcp_pvl_core import file_exchange
from fastmcp_pvl_core._config import ServerConfig
from fastmcp_pvl_core.file_exchange import ArtifactMetadata


class _MemSource:
    def __init__(self, docs: dict[str, bytes]) -> None:
        self._docs = docs

    async def open_artifact(self, key: str):
        return io.BytesIO(self._docs[key]), ArtifactMetadata(mimeType="application/json")


mcp = FastMCP("export-server")
source = _MemSource({"local-doc-1": b'{"hello":"world"}'})
fxctx = file_exchange.register_file_exchange(
    mcp,
    config=ServerConfig(
        kv_store_url="memory://",
        file_exchange_allowed_networks=("10.0.0.0/8",),
    ),
    base_url="https://exports.example",
    source=source,
)
file_exchange.register_file_exchange_sender(mcp, "send_to_intake", fxctx)
```

The peer-facing tool signature is
`send_to_intake(ticket: IntakeTicket, key: str) -> None`.

## Filesystem transport (optional)

To enable the `filesystem://` transport on a fetcher or sender, pass a
`volume_map` to `register_file_exchange`. Without one, a fetcher/sender
tool that selects a filesystem descriptor raises
`FileExchangeTransferError(NO_SUPPORTED_TRANSPORT)` at call time —
filesystem support is opt-in.

```python
from pathlib import Path
from fastmcp_pvl_core._file_exchange._paths import load_volume_map

# Option A: from environment variables (FILE_EXCHANGE_VOLUME_*).
volume_map = load_volume_map(env_prefix="FILE_EXCHANGE_")

# Option B: hand-built mapping.
volume_map = {"reports": Path("/srv/reports"), "intake": Path("/srv/intake")}

fxctx = file_exchange.register_file_exchange(
    mcp,
    config=...,
    base_url=...,
    source=source,
    sink=sink,
    volume_map=volume_map,
)
```

## A single server in multiple roles

A server can register all four helpers. Pass both `source=` and `sink=`
on the setup call (and `volume_map=` if you want filesystem support)
and register the helpers you want.
