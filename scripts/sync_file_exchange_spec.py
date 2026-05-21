#!/usr/bin/env python
"""Sync the vendored file-exchange spec artifacts against upstream.

Vendored artifacts:

- ``src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json``
- ``tests/_file_exchange/conformance/{valid,invalid}/<kind>/*.json``
  where ``<kind>`` is one of ``capability``, ``error``, ``handle``, ``ticket``.

The upstream commit pin lives at
``fastmcp_pvl_core._file_exchange._spec.SPEC_SOURCE_SHA``.

Modes:

- ``--check`` (default): fetch upstream at the pinned SHA; diff against
  vendored copies; exit non-zero on drift. CI runs this on every push.
- ``--bump <sha>``: rewrite the pin constant + all vendored files.

``GITHUB_TOKEN`` (auto-provided by GitHub Actions, settable locally) is
honored to escape GitHub's 60/hr anonymous rate limit per egress IP.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PY = REPO_ROOT / "src/fastmcp_pvl_core/_file_exchange/_spec.py"
SCHEMA_DEST = (
    REPO_ROOT / "src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json"
)
CONFORMANCE_DEST = REPO_ROOT / "tests/_file_exchange/conformance"

UPSTREAM_REPO = "pvliesdonk/mcp-file-exchange-ext"
KINDS = ("capability", "error", "handle", "ticket")
BUCKETS = ("valid", "invalid")

# Network/parse failures the script knows how to convert into a clean
# sys.exit message. Listed explicitly rather than catching `Exception`
# — that breadth would hide parsing bugs (KeyError, AttributeError)
# behind misleading "failed to fetch" messages. RuntimeError is in the
# tuple because _fetch and _list_remote_dir raise it deliberately on
# HTTP non-200, empty bodies, and non-list directory listings (the
# rate-limit-with-200-body case the comment in _fetch explains);
# leaving it out would let those documented failure modes surface as
# raw tracebacks instead of clean exit messages.
_NETWORK_ERRORS = (
    URLError,
    HTTPError,
    TimeoutError,
    OSError,
    json.JSONDecodeError,
    RuntimeError,
)


def _auth_headers() -> dict[str, str]:
    """Build request headers, including bearer auth when GITHUB_TOKEN is set.

    GitHub's anonymous API rate limit is 60 req/hour per IP, and GH
    Actions runners share egress IPs across the whole fleet — so
    anonymous CI calls fail intermittently. With a token (the runner's
    ``GITHUB_TOKEN`` works), the limit jumps to 5000/hour.
    """
    headers = {"User-Agent": "fastmcp-pvl-core-sync/1"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _read_pin() -> str:
    """Read SPEC_SOURCE_SHA from _spec.py without importing it."""
    text = SPEC_PY.read_text()
    m = re.search(r'SPEC_SOURCE_SHA\s*=\s*"([0-9a-f]{40})"', text)
    if not m:
        sys.exit("could not find SPEC_SOURCE_SHA in _spec.py")
    return m.group(1)


def _write_pin(new_sha: str) -> None:
    """Rewrite the SPEC_SOURCE_SHA constant in _spec.py to a new SHA."""
    text = SPEC_PY.read_text()
    new_text = re.sub(
        r'SPEC_SOURCE_SHA\s*=\s*"[0-9a-f]{40}"',
        f'SPEC_SOURCE_SHA = "{new_sha}"',
        text,
    )
    SPEC_PY.write_text(new_text)


def _fetch(sha: str, path: str) -> bytes:
    """GET a file from raw.githubusercontent.com at the pinned SHA.

    Validates HTTP status and rejects empty bodies — some
    200-with-error-body cases (rate-limit interstitials) slip past
    urlopen's built-in non-2xx raising.
    """
    url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{sha}/{path}"
    req = Request(url, headers=_auth_headers())
    with urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GET {url} returned HTTP {resp.status}")
        data = resp.read()
    if not data:
        raise RuntimeError(f"GET {url} returned an empty body")
    return data  # type: ignore[no-any-return]


def _list_remote_dir(sha: str, path: str) -> list[str]:
    """List files in a remote directory via the GitHub API."""
    url = f"https://api.github.com/repos/{UPSTREAM_REPO}/contents/{path}?ref={sha}"
    headers = _auth_headers() | {"Accept": "application/vnd.github.v3+json"}
    req = Request(url, headers=headers)
    with urlopen(req, timeout=30) as resp:
        if resp.status != 200:
            raise RuntimeError(f"GET {url} returned HTTP {resp.status}")
        items = json.loads(resp.read())
    if not isinstance(items, list):
        # GitHub returns an object (not a list) when `path` is a file,
        # or a JSON error body for rate-limit / abuse responses.
        raise RuntimeError(
            f"GET {url} did not return a directory listing (got {type(items).__name__})"
        )
    out: list[str] = []
    for item in items:
        # Defensive against GitHub API shape changes: a partially-truncated
        # response (or a future API revision) could omit `name`/`type` on
        # an entry. Surface that as a clean RuntimeError (covered by
        # _NETWORK_ERRORS) rather than letting a KeyError traceback through.
        if not isinstance(item, dict) or "name" not in item or "type" not in item:
            raise RuntimeError(
                f"GET {url} returned a directory listing with malformed entries"
            )
        if item["type"] == "file":
            out.append(item["name"])
    return out


def _gather_upstream(sha: str) -> dict[Path, bytes]:
    """Return {local destination path -> upstream bytes} for everything to vendor.

    All network errors short-circuit via :func:`sys.exit` with a
    contextual message naming the path and exception class — operators
    see the cause, not a urllib traceback. Any non-network failure
    propagates as a normal exception with its traceback intact.
    """
    out: dict[Path, bytes] = {}
    try:
        out[SCHEMA_DEST] = _fetch(sha, "schema/file-exchange.json")
    except _NETWORK_ERRORS as exc:
        sys.exit(
            f"failed to fetch schema/file-exchange.json: {type(exc).__name__}: {exc}"
        )
    for bucket in BUCKETS:
        for kind in KINDS:
            remote_dir = f"conformance/{bucket}/{kind}"
            try:
                names = _list_remote_dir(sha, remote_dir)
            except _NETWORK_ERRORS as exc:
                sys.exit(f"failed to list {remote_dir}: {type(exc).__name__}: {exc}")
            for name in names:
                if not name.endswith(".json"):
                    continue
                try:
                    content = _fetch(sha, f"{remote_dir}/{name}")
                except _NETWORK_ERRORS as exc:
                    sys.exit(
                        f"failed to fetch {remote_dir}/{name}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                out[CONFORMANCE_DEST / bucket / kind / name] = content
    return out


def _digest(b: bytes) -> str:
    """Return the SHA-256 digest of ``b`` as a lowercase hex string."""
    return hashlib.sha256(b).hexdigest()


def _local_bytes(path: Path) -> bytes | None:
    """Read ``path`` as bytes, or return None if the file does not exist."""
    if not path.is_file():
        return None
    return path.read_bytes()


def cmd_check(sha: str) -> int:
    """Compare vendored artifacts against upstream at ``sha``.

    Returns 0 on clean match, 1 on any drift. Drift includes: missing
    locally, content mismatch, or files vendored locally that upstream
    no longer ships.
    """
    upstream = _gather_upstream(sha)
    drift: list[tuple[Path, str]] = []
    for dest, expected in upstream.items():
        local = _local_bytes(dest)
        if local is None:
            drift.append((dest, "missing locally"))
            continue
        if _digest(local) != _digest(expected):
            drift.append((dest, "content differs"))
    for sub in (SCHEMA_DEST.parent, CONFORMANCE_DEST):
        if sub.is_dir():
            for path in sub.rglob("*.json"):
                if path not in upstream:
                    drift.append((path, "no longer in upstream"))
    if drift:
        for path, why in drift:
            rel = path.relative_to(REPO_ROOT)
            print(f"DRIFT: {rel} — {why}", file=sys.stderr)
        return 1
    print(f"sync_file_exchange_spec: vendored ≡ upstream @ {sha[:8]} ✓")
    return 0


def cmd_bump(new_sha: str) -> int:
    """Rewrite the pin + all vendored files to upstream at ``new_sha``.

    Fetches upstream contents fully into memory before any local write,
    so a fetch failure leaves the working tree unchanged. If a write
    fails mid-flight (disk full, permission), the working tree may be
    left half-bumped — recover with ``git checkout`` of the affected
    paths.
    """
    if not re.fullmatch(r"[0-9a-f]{40}", new_sha):
        sys.exit("--bump requires a full 40-char hex SHA")
    upstream = _gather_upstream(new_sha)
    expected_set = set(upstream)
    for sub in (SCHEMA_DEST.parent, CONFORMANCE_DEST):
        if sub.is_dir():
            for path in sub.rglob("*.json"):
                if path not in expected_set:
                    path.unlink()
    for dest, content in upstream.items():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    old = _read_pin()
    _write_pin(new_sha)
    print(f"sync_file_exchange_spec: bumped {old[:8]} → {new_sha[:8]}")
    return 0


def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--check",
        action="store_true",
        help="diff vendored against upstream at the pinned SHA (default)",
    )
    group.add_argument(
        "--bump",
        metavar="SHA",
        help="rewrite the pin + all vendored files at <SHA>",
    )
    args = parser.parse_args()

    if args.bump:
        return cmd_bump(args.bump)
    sha = _read_pin()
    return cmd_check(sha)


if __name__ == "__main__":
    sys.exit(main())
