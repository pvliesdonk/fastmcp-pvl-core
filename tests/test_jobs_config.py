"""Tests for ``JobsConfig`` (ADR 0002 §5).

Covers defaults, ``__post_init__`` invariants (all-positive), the
``from_env`` literal-suffix read surface, and the ``domain_env_suffixes``
drift gate.
"""

from __future__ import annotations

import pytest

from fastmcp_pvl_core import (
    ConfigurationError,
    JobsConfig,
    domain_env_suffixes,
    domain_env_surface,
)

# The full literal env surface JobsConfig.from_env reads; the drift gate
# (domain_env_suffixes) and from_env must agree on exactly this set.
_EXPECTED_SUFFIXES = frozenset(
    {
        "JOBS_SOFT_DEADLINE_S",
        "JOBS_RESULT_TTL_S",
        "JOBS_MAX_PER_SUBJECT",
    }
)


class TestDefaults:
    def test_defaults(self):
        cfg = JobsConfig()
        assert cfg.soft_deadline_s == 25.0
        assert cfg.result_ttl_s == 3600.0
        assert cfg.max_per_subject == 256

    def test_frozen(self):
        cfg = JobsConfig()
        with pytest.raises(AttributeError):
            cfg.soft_deadline_s = 1.0  # type: ignore[misc]


class TestValidation:
    @pytest.mark.parametrize(
        "kwargs",
        [
            {"soft_deadline_s": 0.0},
            {"soft_deadline_s": -1.0},
            {"soft_deadline_s": float("inf")},
            {"soft_deadline_s": float("nan")},
            {"result_ttl_s": 0.0},
            {"max_per_subject": 0},
        ],
    )
    def test_non_positive_or_non_finite_rejected(self, kwargs):
        with pytest.raises(ConfigurationError):
            JobsConfig(**kwargs)


class TestFromEnv:
    def test_unset_yields_defaults(self, clean_env):
        assert JobsConfig.from_env("PVLCORE_TEST") == JobsConfig()

    def test_reads_all_vars(self, monkeypatch):
        monkeypatch.setenv("PVLCORE_TEST_JOBS_SOFT_DEADLINE_S", "10.5")
        monkeypatch.setenv("PVLCORE_TEST_JOBS_RESULT_TTL_S", "120")
        monkeypatch.setenv("PVLCORE_TEST_JOBS_MAX_PER_SUBJECT", "8")
        cfg = JobsConfig.from_env("PVLCORE_TEST")
        assert cfg.soft_deadline_s == 10.5
        assert cfg.result_ttl_s == 120.0
        assert cfg.max_per_subject == 8

    def test_invalid_value_raises_naming_var(self, monkeypatch):
        monkeypatch.setenv("PVLCORE_TEST_JOBS_SOFT_DEADLINE_S", "soon")
        with pytest.raises(
            ConfigurationError, match="PVLCORE_TEST_JOBS_SOFT_DEADLINE_S"
        ):
            JobsConfig.from_env("PVLCORE_TEST")

    def test_zero_cap_raises(self, monkeypatch):
        monkeypatch.setenv("PVLCORE_TEST_JOBS_MAX_PER_SUBJECT", "0")
        with pytest.raises(
            ConfigurationError, match="PVLCORE_TEST_JOBS_MAX_PER_SUBJECT"
        ):
            JobsConfig.from_env("PVLCORE_TEST")


class TestDriftGate:
    def test_env_surface_matches(self):
        assert domain_env_suffixes(JobsConfig) == _EXPECTED_SUFFIXES

    def test_surface_records_resolve_to_fields(self):
        records = domain_env_surface(JobsConfig)
        assert {r.suffix for r in records} == _EXPECTED_SUFFIXES
        # Every read is tied to its constructor field, so a downstream
        # config generator can document these vars with field metadata.
        assert all(r.name is not None for r in records)
        assert all(r.help for r in records)
