"""Tests for the bearer-tokens TOML loader."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from fastmcp_pvl_core import (
    ConfigurationError,
    ServerConfig,
    build_bearer_auth,
)


def _write_tokens(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "tokens.toml"
    path.write_text(content, encoding="utf-8")
    return path


class TestBearerTokensFileLoader:
    def test_returns_verifier_with_mapped_subjects(self, tmp_path: Path):
        path = _write_tokens(
            tmp_path,
            (
                "[tokens]\n"
                '"alice-token" = "user:alice@example.com"\n'
                '"ci-bot-token" = "service:ci-bot"\n'
            ),
        )
        auth = build_bearer_auth(ServerConfig(bearer_tokens_file=path))
        assert auth is not None
        alice = auth.tokens["alice-token"]
        bot = auth.tokens["ci-bot-token"]
        assert alice["client_id"] == "user:alice@example.com"
        assert alice["scopes"] == ["read", "write"]
        assert bot["client_id"] == "service:ci-bot"

    def test_missing_file_raises_configuration_error(self, tmp_path: Path):
        with pytest.raises(ConfigurationError, match="not found"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=tmp_path / "nope.toml"))

    def test_malformed_toml_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens\n"x" = "y"')
        with pytest.raises(ConfigurationError, match="parse"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_blank_file_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, "")
        with pytest.raises(ConfigurationError, match="empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_missing_tokens_table_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[other]\nkey = "v"\n')
        with pytest.raises(ConfigurationError, match="\\[tokens\\]"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_non_string_subject_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"x" = 42\n')
        with pytest.raises(ConfigurationError, match="string"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_empty_subject_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"x" = ""\n')
        with pytest.raises(ConfigurationError, match="empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_empty_tokens_table_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, "[tokens]\n")
        with pytest.raises(ConfigurationError, match="non-empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_whitespace_only_subject_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"x" = "   "\n')
        with pytest.raises(ConfigurationError, match="empty"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_empty_token_key_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"" = "user:alice"\n')
        with pytest.raises(ConfigurationError, match="empty or whitespace"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_whitespace_only_token_key_raises_configuration_error(self, tmp_path: Path):
        path = _write_tokens(tmp_path, '[tokens]\n"   " = "user:alice"\n')
        with pytest.raises(ConfigurationError, match="empty or whitespace"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_nested_subtable_raises_clear_diagnostic(self, tmp_path: Path):
        # Common operator slip: writing `[tokens.foo]` instead of
        # `[tokens]\n"foo" = "..."` produces a nested-table structure;
        # the loader should surface a clear "nested table" diagnostic
        # rather than the misleading "subject must be a string".
        path = _write_tokens(tmp_path, '[tokens.foo]\nbar = "baz"\n')
        with pytest.raises(ConfigurationError, match="nested table"):
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))

    def test_error_messages_do_not_leak_token_value(self, tmp_path: Path):
        # Defensive: error messages must never embed the raw token (the
        # KEY in the [tokens] map), so an operator pasting a startup
        # exception into a bug report doesn't leak credentials.
        secret_token = "ghp_secretvalue_xxx_must_not_appear"
        path = _write_tokens(tmp_path, f'[tokens]\n"{secret_token}" = ""\n')
        with pytest.raises(ConfigurationError) as excinfo:
            build_bearer_auth(ServerConfig(bearer_tokens_file=path))
        assert secret_token not in str(excinfo.value)


class TestBearerTokensFilePrecedence:
    def test_file_takes_precedence_over_single_token(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ):
        path = _write_tokens(tmp_path, '[tokens]\n"file-token" = "user:via-file"\n')
        with caplog.at_level(logging.WARNING):
            auth = build_bearer_auth(
                ServerConfig(
                    bearer_token="single-token",
                    bearer_tokens_file=path,
                )
            )
        assert auth is not None
        # Single token must NOT appear; only the file's tokens are loaded.
        assert "single-token" not in auth.tokens
        assert auth.tokens["file-token"]["client_id"] == "user:via-file"
        # WARNING surfaced.
        assert any(
            "bearer_tokens_file" in r.message
            and "bearer_token" in r.message
            and r.levelname == "WARNING"
            for r in caplog.records
        )


class TestBearerSingleDefaultSubject:
    def test_default_subject_is_bearer_anon(self):
        auth = build_bearer_auth(ServerConfig(bearer_token="t"))
        assert auth is not None
        assert auth.tokens["t"]["client_id"] == "bearer-anon"

    def test_custom_default_subject(self):
        auth = build_bearer_auth(
            ServerConfig(bearer_token="t", bearer_default_subject="service:x")
        )
        assert auth is not None
        assert auth.tokens["t"]["client_id"] == "service:x"
