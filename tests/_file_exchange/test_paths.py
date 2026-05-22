"""Tests for filesystem URI parsing, volume config, and path confinement."""

from __future__ import annotations

import re

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fastmcp_pvl_core._file_exchange._paths import (
    _parse_fs_uri,
    canonicalize_and_confine,
)
from fastmcp_pvl_core._file_exchange._wire import _FS_URI_PATTERN

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


# --- _parse_fs_uri: valid URIs ---


@pytest.mark.parametrize(
    "uri,expected",
    [
        ("exchange://docs/a/b.bin", ("exchange", "docs", "a/b.bin")),
        ("exchange://v/single", ("exchange", "v", "single")),
        ("exchange://docs/a/../etc", ("exchange", "docs", "a/../etc")),
        ("file:///mnt/x", ("file", "", "/mnt/x")),
        ("file:///mnt/../etc", ("file", "", "/mnt/../etc")),
    ],
)
def test_parse_fs_uri_valid(uri, expected):
    assert _parse_fs_uri(uri) == expected


@pytest.mark.parametrize(
    "uri",
    [
        "exchange:///x",  # empty volume
        "exchange://docs",  # no path
        "exchange://docs/",  # empty path
        "exchange://docs/a?q=1",  # query
        "exchange://docs/a#f",  # fragment
        "file://host/x",  # non-empty authority
        "file://x",  # not absolute (authority 'x', empty path)
        "file:///",  # root-only path
        "file:////x",  # double slash after authority
        "https://example/x",  # unknown scheme
        "not-a-uri",  # no scheme
        "",  # empty
    ],
)
def test_parse_fs_uri_rejects(uri):
    assert _parse_fs_uri(uri) is None


def test_parse_agrees_with_wire_pattern_on_valid():
    """Every URI _parse_fs_uri accepts must also match the wire pattern."""
    for uri in (
        "exchange://docs/a/b.bin",
        "exchange://v/single",
        "file:///mnt/x",
    ):
        assert re.match(_FS_URI_PATTERN, uri), uri
        assert _parse_fs_uri(uri) is not None, uri


def test_parse_agrees_with_wire_pattern_on_invalid():
    """URIs the wire pattern rejects must also fail to parse."""
    for uri in (
        "exchange:///x",
        "exchange://docs/",
        "file://host/x",
        "file:///",
        "file:////x",
    ):
        assert not re.match(_FS_URI_PATTERN, uri), uri
        assert _parse_fs_uri(uri) is None, uri
