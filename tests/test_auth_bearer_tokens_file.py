"""Tests for FASTMCP_BEARER_TOKENS_FILE token→subject mapping."""

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
            "BEARER_TOKENS_FILE" in r.message
            and "BEARER_TOKEN" in r.message
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
