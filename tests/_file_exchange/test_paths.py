"""Tests for filesystem URI parsing, volume config, and path confinement."""

from __future__ import annotations

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fastmcp_pvl_core._file_exchange._paths import canonicalize_and_confine

# --- canonicalize_and_confine: confined cases ---


def test_confine_plain_file_inside(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    f = root / "a.bin"
    f.write_text("x")
    assert canonicalize_and_confine(f, root) == f.resolve()


def test_confine_internal_dotdot_stays_inside(tmp_path):
    root = tmp_path / "vol"
    (root / "sub").mkdir(parents=True)
    (root / "a.bin").write_text("x")
    target = root / "sub" / ".." / "a.bin"
    assert canonicalize_and_confine(target, root) == (root / "a.bin").resolve()


def test_confine_root_itself(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    assert canonicalize_and_confine(root, root) == root.resolve()


def test_confine_nonexistent_tail_inside(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    target = root / "new" / "sink.bin"  # does not exist yet
    assert canonicalize_and_confine(target, root) == target.resolve()


def test_confine_allows_internal_symlink(tmp_path):
    root = tmp_path / "vol"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.bin").write_text("x")
    (root / "link").symlink_to(root / "real")
    assert (
        canonicalize_and_confine(root / "link" / "f.bin", root)
        == (root / "real" / "f.bin").resolve()
    )


# --- canonicalize_and_confine: escape cases (all must return None) ---


def test_confine_rejects_dotdot_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    assert canonicalize_and_confine(root / ".." / "outside", root) is None


def test_confine_rejects_absolute_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    assert canonicalize_and_confine(outside / "secret", root) is None


def test_confine_rejects_symlink_escape(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "link").symlink_to(outside)
    assert canonicalize_and_confine(root / "link" / "secret", root) is None


def test_confine_rejects_symlink_in_intermediate_component(tmp_path):
    root = tmp_path / "vol"
    (root / "real").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "real" / "esc").symlink_to(outside)
    assert canonicalize_and_confine(root / "real" / "esc" / "x", root) is None


def test_confine_rejects_dotdot_plus_symlink_combo(tmp_path):
    root = tmp_path / "vol"
    (root / "sub").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "sub" / "link").symlink_to(outside)
    # through the symlink, then a sibling — still outside the root
    target = root / "sub" / "link" / ".." / "outside" / "secret"
    assert canonicalize_and_confine(target, root) is None


# --- property: any accepted result is genuinely within the root ---


@pytest.fixture(scope="module")
def _confine_root(tmp_path_factory):
    # Module-scoped so hypothesis (which re-runs the body per example) does
    # not trip the function-scoped-fixture health check.
    return tmp_path_factory.mktemp("confine_root")


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(
    segments=st.lists(
        st.sampled_from(["..", ".", "a", "b", "sub", "x.bin", "...", "vol"]),
        max_size=10,
    )
)
def test_confine_invariant_accepted_paths_are_within_root(_confine_root, segments):
    root = _confine_root
    candidate = root.joinpath(*segments) if segments else root
    result = canonicalize_and_confine(candidate, root)
    if result is not None:
        assert result.is_relative_to(root.resolve())
