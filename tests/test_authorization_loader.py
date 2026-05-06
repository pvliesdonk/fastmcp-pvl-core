"""Tests for the load_acl TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from fastmcp_pvl_core._authorization import load_acl


def test_load_acl_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text(
        '''
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]
"service:ci-bot"         = ["read"]
        ''',
        encoding="utf-8",
    )
    acl = load_acl(p)
    assert acl == {
        "user:alice@example.com": frozenset({"read", "write"}),
        "user:admin@example.com": frozenset({"*"}),
        "service:ci-bot": frozenset({"read"}),
    }
    assert all(isinstance(v, frozenset) for v in acl.values())


def test_load_acl_empty_subjects_table_permitted(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text("[subjects]\n", encoding="utf-8")
    assert load_acl(p) == {}


def test_load_acl_expands_user_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "acl.toml"
    real.write_text('[subjects]\n"user:x" = ["read"]\n', encoding="utf-8")
    acl = load_acl(Path("~/acl.toml"))
    assert acl == {"user:x": frozenset({"read"})}
