"""Tests for the file-exchange spec sync script."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "sync_file_exchange_spec.py"


def test_script_exists():
    assert SCRIPT.is_file()


def test_schema_is_vendored():
    schema = (
        REPO_ROOT / "src/fastmcp_pvl_core/_file_exchange/_schema/file-exchange.json"
    )
    assert schema.is_file()
    # Schema $id is version-pathed to 0.1 — smoke check we vendored the right file.
    text = schema.read_text()
    assert "mcp-file-exchange/0.1/file-exchange.json" in text


def test_conformance_fixtures_vendored():
    base = REPO_ROOT / "tests/_file_exchange/conformance"
    for kind in ("capability", "error", "handle", "ticket"):
        valid = base / "valid" / kind
        invalid = base / "invalid" / kind
        assert valid.is_dir(), f"missing valid/{kind}/"
        assert invalid.is_dir(), f"missing invalid/{kind}/"
        assert any(valid.glob("*.json")), f"valid/{kind}/ has no fixtures"
        assert any(invalid.glob("*.json")), f"invalid/{kind}/ has no fixtures"


def test_write_pin_regex_matches_current_spec_py():
    """If _spec.py's SPEC_SOURCE_SHA line drifts, --bump silently no-ops.

    The regex in scripts/sync_file_exchange_spec.py:_write_pin must
    match the current shape of _spec.py at all times.
    """
    spec_py = REPO_ROOT / "src/fastmcp_pvl_core/_file_exchange/_spec.py"
    text = spec_py.read_text()
    pattern = re.compile(r'SPEC_SOURCE_SHA\s*=\s*"([0-9a-f]{40})"')
    match = pattern.search(text)
    assert match is not None, (
        "_write_pin's regex no longer matches _spec.py's SPEC_SOURCE_SHA "
        "line — sync_file_exchange_spec.py --bump would silently no-op."
    )


@pytest.mark.network
def test_check_mode_passes_against_pin():
    """`--check` mode reports vendored ≡ upstream at the pinned SHA."""
    if not os.environ.get("GITHUB_TOKEN"):
        # The script hits api.github.com 8× per --check run. GitHub's
        # 60/hr anonymous rate limit per egress IP makes this flaky
        # locally; CI provides GITHUB_TOKEN via secrets.GITHUB_TOKEN.
        pytest.skip("requires GITHUB_TOKEN to avoid GitHub anonymous rate limit")
    result = subprocess.run(
        ["uv", "run", "python", str(SCRIPT), "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"--check failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )
