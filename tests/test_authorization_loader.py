"""Tests for the load_acl TOML loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from fastmcp_pvl_core._authorization import load_acl
from fastmcp_pvl_core._errors import ConfigurationError


def test_load_acl_happy_path(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text(
        """
[subjects]
"user:alice@example.com" = ["read", "write"]
"user:admin@example.com" = ["*"]
"service:ci-bot"         = ["read"]
        """,
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


def test_load_acl_strips_padded_scopes(tmp_path: Path) -> None:
    """Scopes with surrounding whitespace are stored in canonical form."""
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = ["  read  ", " write"]\n', encoding="utf-8")
    acl = load_acl(p)
    assert acl == {"user:x": frozenset({"read", "write"})}


def test_load_acl_missing_file(tmp_path: Path) -> None:
    p = tmp_path / "nope.toml"
    with pytest.raises(ConfigurationError, match="not found"):
        load_acl(p)


def test_load_acl_directory_not_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found or not a regular file"):
        load_acl(tmp_path)


def test_load_acl_invalid_utf8(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_bytes(b"\xff\xfe\xfd not utf-8")
    with pytest.raises(ConfigurationError, match="could not be read"):
        load_acl(p)


def test_load_acl_malformed_toml(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text("[subjects\nbroken =", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="could not be parsed"):
        load_acl(p)


def test_load_acl_missing_subjects_table(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[other]\nkey = "val"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"\[subjects\] table"):
        load_acl(p)


def test_load_acl_subjects_not_a_table(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('subjects = "scalar"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r"\[subjects\] table"):
        load_acl(p)


def test_load_acl_blank_subject_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"" = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="subject key is empty"):
        load_acl(p)


def test_load_acl_whitespace_subject_key_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"   " = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="subject key is empty"):
        load_acl(p)


def test_load_acl_subject_wildcard_rejected(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"*" = ["read"]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=r'"\*" as a subject key'):
        load_acl(p)


def test_load_acl_non_list_value(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = "read"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="must be an array"):
        load_acl(p)


def test_load_acl_non_string_scope(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = ["read", 42]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="scope must be a string"):
        load_acl(p)


def test_load_acl_blank_scope(tmp_path: Path) -> None:
    p = tmp_path / "acl.toml"
    p.write_text('[subjects]\n"user:x" = ["read", "  "]\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="scope is empty"):
        load_acl(p)
