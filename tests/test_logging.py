"""Tests for configure_logging_from_env."""

from __future__ import annotations

import logging

from fastmcp_pvl_core import configure_logging_from_env


def test_sets_debug_when_verbose_true(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_verbose_sets_fastmcp_log_level_env(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=True)
    import os

    assert os.environ.get("FASTMCP_LOG_LEVEL") == "DEBUG"


def test_respects_fastmcp_log_level(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "WARNING")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_defaults_to_info_when_nothing_set(monkeypatch):
    monkeypatch.delenv("FASTMCP_LOG_LEVEL", raising=False)
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_lowercase_level_name_handled(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "warning")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.WARNING


def test_unknown_level_falls_back_to_info(monkeypatch):
    monkeypatch.setenv("FASTMCP_LOG_LEVEL", "BOGUS")
    configure_logging_from_env(verbose=False)
    assert logging.getLogger().getEffectiveLevel() == logging.INFO
