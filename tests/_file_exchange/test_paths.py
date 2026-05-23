"""Tests for filesystem URI parsing, volume config, and path confinement."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from fastmcp_pvl_core import ConfigurationError
from fastmcp_pvl_core._file_exchange._paths import (
    _parse_fs_uri,
    _parse_volume_map,
    atomic_write,
    canonicalize_and_confine,
    load_volume_map,
    resolve_filesystem_uri,
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


def test_confine_rejects_sibling_with_shared_prefix(tmp_path):
    """A sibling dir whose name shares a string prefix with root must be
    rejected — pins the is_relative_to (component-wise) choice against a
    naive str.startswith confinement that would treat /vol-data as inside
    /vol and silently bypass confinement."""
    root = tmp_path / "vol"
    root.mkdir()
    sibling = tmp_path / "vol-data"  # shares the "vol" string prefix
    sibling.mkdir()
    (sibling / "secret").write_text("x")
    assert canonicalize_and_confine(sibling / "secret", root) is None


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
        # leading double-slash collapses (lstrip), same path as single-slash
        ("exchange://docs//a/b", ("exchange", "docs", "a/b")),
        # userinfo/port are part of the opaque volume id (§10.1.1: "<volume>
        # — the URI authority — is an opaque identifier"); not split off to a
        # bare host. Pins the decision against a future parts.hostname
        # refactor (tracked in mcp-file-exchange-ext#15).
        ("exchange://user@docs/a", ("exchange", "user@docs", "a")),
        ("exchange://docs:8080/a", ("exchange", "docs:8080", "a")),
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
        "exchange://docs/a?",  # bare query delimiter (empty query)
        "exchange://docs/a#",  # bare fragment delimiter (empty fragment)
        "file:///mnt/x?",  # bare query delimiter
        "file:///mnt/x#",  # bare fragment delimiter
        "file://host/x",  # non-empty authority
        "file://x",  # not absolute (authority 'x', empty path)
        "file:///",  # root-only path
        "file:////x",  # double slash after authority
        "https://example/x",  # unknown scheme
        "not-a-uri",  # no scheme
        "",  # empty
        "EXCHANGE://docs/a",  # uppercase scheme — wire pattern is case-sensitive
        "FILE:///mnt/x",  # uppercase scheme
        "exchange://[::1/x",  # malformed IPv6 authority — must not raise
        "file://[bad/x",  # malformed IPv6 authority
        "exchange://docs/a\nb",  # embedded newline — urlsplit strips, wire rejects
        "file:///mnt/a\nb",  # embedded newline
        "exchange://docs/a\tb",  # embedded tab — urlsplit strips it
        "exchange://docs/a\rb",  # embedded CR — urlsplit strips it
    ],
)
def test_parse_fs_uri_rejects(uri):
    assert _parse_fs_uri(uri) is None


_DRIFT_URIS = [
    # valid (parser accepts, wire matches)
    "exchange://docs/a/b.bin",
    "exchange://v/single",
    "file:///mnt/x",
    # userinfo/port are wire-valid (the [^/]+ netloc accepts @ and :) and
    # parser-accepted as part of the opaque volume id, not split to a host
    # (§10.1.1, mcp-file-exchange-ext#15) — so they satisfy the invariant.
    "exchange://user@docs/a",
    "exchange://docs:8080/a",
    # parser rejects these — wire may or may not match; the invariant is
    # only "accepted => wire matches", so rejected ones can't violate it,
    # but we include them to document the cases.
    "exchange:///x",
    "exchange://docs/",
    "file://host/x",
    "file:///",
    "file:////x",
    "EXCHANGE://docs/a",  # uppercase scheme — wire rejects, parser must too
    "FILE:///mnt/x",  # uppercase scheme
    "exchange://[::1/x",  # malformed IPv6 authority — must not raise
    "file://[bad/x",  # malformed IPv6 authority
    "exchange://docs/a?q=1",  # query — parser stricter than wire (safe)
    "https://example/x",  # unknown scheme
    # bare query/fragment delimiters: wire's `.+` accepts them, parser must
    # reject. Pinned here so a refactor of the raw-string guard back to
    # parts.query/parts.fragment (falsy for empty) would surface as drift.
    "exchange://docs/a?",
    "exchange://docs/a#",
    "file:///mnt/x?",
    "file:///mnt/x#",
    # control chars urlsplit silently strips (WHATWG) — parser must reject
    # so it never accepts a URI the newline-anchored wire pattern refuses.
    "exchange://docs/a\nb",
    "file:///mnt/a\nb",
    "exchange://docs/a\tb",
    "exchange://docs/a\rb",
]


def test_parse_never_more_permissive_than_wire_pattern():
    """The load-bearing drift invariant: any URI the parser accepts must
    also match _FS_URI_PATTERN. (The reverse is not required — the parser
    may be stricter, e.g. rejecting query/fragment or malformed IPv6.)

    Uses re.fullmatch, not re.match: the real wire validator is pydantic's
    anchored Field(pattern=...), which rejects e.g. a trailing newline that
    re.match would accept (re's $ matches before a final \\n). fullmatch
    mirrors the genuine wire surface, so a future regression making the
    parser accept such an input would be caught here, not just by CI's
    pydantic layer."""
    for uri in _DRIFT_URIS:
        if _parse_fs_uri(uri) is not None:
            assert re.fullmatch(_FS_URI_PATTERN, uri), (
                f"parser accepted {uri!r} but wire pattern rejects it"
            )


def test_parse_never_raises_on_drift_uris():
    """No URI in the corpus may raise — every non-usable input returns None."""
    for uri in _DRIFT_URIS:
        _parse_fs_uri(uri)  # must not raise


@given(
    prefix=st.sampled_from(["exchange://", "file://", "EXCHANGE://", "FILE://", ""]),
    # Single-char alphabet mixing URI structure chars with the chars urlsplit
    # normalizes (control chars) and IPv6-bracket triggers, so the fuzzer
    # probes the parser-vs-wire boundary rather than random noise.
    body=st.text(
        alphabet=st.sampled_from(list("docsab/:.[]?#@") + ["\n", "\t", "\r", "\x00"]),
        max_size=20,
    ),
)
def test_parse_never_more_permissive_than_wire_pattern_fuzzed(prefix, body):
    """Property form of the drift invariant: for ANY input, parser-accepted
    ⇒ wire-matches. Backs the fixed corpus against unforeseen normalization
    rings (the fixed-list-only guard is how embedded-newline drift slipped
    in originally). Also asserts the parser never raises."""
    uri = prefix + body
    result = _parse_fs_uri(uri)  # must not raise
    if result is not None:
        # fullmatch (not match) to mirror pydantic's anchored wire validator
        # — see test_parse_never_more_permissive_than_wire_pattern.
        assert re.fullmatch(_FS_URI_PATTERN, uri), (
            f"parser accepted {uri!r} but wire pattern rejects it"
        )


# --- _parse_volume_map and load_volume_map ---


_VAR = "SCHOLAR_FILE_EXCHANGE_VOLUMES"


def test_parse_volume_map_basic():
    result = _parse_volume_map("docs=/mnt/docs,scratch=/mnt/scratch", _VAR)
    assert result == {
        "docs": Path("/mnt/docs"),
        "scratch": Path("/mnt/scratch"),
    }


def test_parse_volume_map_trims_and_drops_empty():
    result = _parse_volume_map(" docs = /mnt/docs , , scratch=/mnt/s ", _VAR)
    assert result == {"docs": Path("/mnt/docs"), "scratch": Path("/mnt/s")}


def test_parse_volume_map_splits_on_first_equals():
    # A path containing '=' (contrived) keeps everything after the first '='.
    result = _parse_volume_map("v=/mnt/a=b", _VAR)
    assert result == {"v": Path("/mnt/a=b")}


@pytest.mark.parametrize(
    "raw",
    [
        "docs",  # no '='
        "=/mnt/docs",  # empty name
        "docs=",  # empty path
        "docs=relative/x",  # non-absolute path
        "docs=/a,docs=/b",  # duplicate name
        "docs/extra=/mnt/x",  # name contains '/' (unreachable: wire is [^/]+)
    ],
)
def test_parse_volume_map_rejects(raw):
    with pytest.raises(ConfigurationError) as exc_info:
        _parse_volume_map(raw, _VAR)
    # The error names the actual env var so the operator knows what to fix.
    assert _VAR in str(exc_info.value)


def test_load_volume_map_unset_returns_empty(monkeypatch):
    monkeypatch.delenv("SCHOLAR_FILE_EXCHANGE_VOLUMES", raising=False)
    assert load_volume_map("SCHOLAR") == {}


def test_load_volume_map_reads_env(monkeypatch):
    monkeypatch.setenv("SCHOLAR_FILE_EXCHANGE_VOLUMES", "docs=/mnt/docs")
    assert load_volume_map("SCHOLAR") == {"docs": Path("/mnt/docs")}


def test_load_volume_map_prefix_is_threaded(monkeypatch):
    # A different prefix reads a different variable — confirms env_prefix is
    # actually threaded through, not a hardcoded name.
    monkeypatch.setenv("MARKDOWN_VAULT_FILE_EXCHANGE_VOLUMES", "uploads=/mnt/uploads")
    monkeypatch.delenv("SCHOLAR_FILE_EXCHANGE_VOLUMES", raising=False)
    assert load_volume_map("MARKDOWN_VAULT") == {"uploads": Path("/mnt/uploads")}
    assert load_volume_map("SCHOLAR") == {}


def test_load_volume_map_blank_env_returns_empty(monkeypatch):
    monkeypatch.setenv("SCHOLAR_FILE_EXCHANGE_VOLUMES", "   ")
    assert load_volume_map("SCHOLAR") == {}


# --- resolve_filesystem_uri ---


def test_resolve_exchange_confined(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.bin").write_text("x")
    vm = {"docs": root}
    assert (
        resolve_filesystem_uri("exchange://docs/a.bin", volume_map=vm)
        == (root / "a.bin").resolve()
    )


def test_resolve_exchange_unknown_volume_returns_none(tmp_path):
    vm = {"docs": tmp_path / "docs"}
    assert resolve_filesystem_uri("exchange://other/a.bin", volume_map=vm) is None


def test_resolve_exchange_escape_returns_none_and_warns(tmp_path, caplog):
    root = tmp_path / "docs"
    root.mkdir()
    (tmp_path / "outside").mkdir()
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri("exchange://docs/../outside", volume_map=vm)
    assert result is None
    assert any(
        "could not be confined to its volume root" in r.message for r in caplog.records
    )
    # The raw path is never logged — only the volume id.
    assert not any("outside" in r.getMessage() for r in caplog.records)


def test_resolve_exchange_unknown_volume_does_not_warn(tmp_path, caplog):
    vm = {"docs": tmp_path / "docs"}
    with caplog.at_level(logging.WARNING):
        resolve_filesystem_uri("exchange://other/a.bin", volume_map=vm)
    assert caplog.records == []


def test_resolve_exchange_userinfo_or_port_is_unmapped_volume(tmp_path, caplog):
    """userinfo/port make the netloc a distinct opaque volume id, so
    exchange://user@docs/x does NOT resolve under a configured 'docs' volume
    — it's an unmapped volume, skipped (None) and silently (no warning, like
    any unknown volume). Pins the §10.1.1 opaque-authority decision against a
    parts.hostname refactor (mcp-file-exchange-ext#15)."""
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.bin").write_text("x")
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        assert (
            resolve_filesystem_uri("exchange://user@docs/a.bin", volume_map=vm) is None
        )
        assert (
            resolve_filesystem_uri("exchange://docs:8080/a.bin", volume_map=vm) is None
        )
    # Unmapped volume is a benign skip — no warning (parts.hostname aliasing
    # to the real 'docs' volume would change both resolution and this assert).
    assert caplog.records == []


def test_resolve_file_within_a_volume(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.bin").write_text("x")
    vm = {"docs": root}
    uri = f"file://{(root / 'a.bin')}"  # file:///<abs>
    assert resolve_filesystem_uri(uri, volume_map=vm) == (root / "a.bin").resolve()


def test_resolve_file_outside_all_volumes_returns_none_and_warns(tmp_path, caplog):
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri(f"file://{(outside / 'secret')}", volume_map=vm)
    assert result is None
    assert any(
        "could not be confined to any configured volume" in r.message
        for r in caplog.records
    )


def test_resolve_malformed_uri_returns_none(tmp_path):
    vm = {"docs": tmp_path / "docs"}
    assert resolve_filesystem_uri("https://example/x", volume_map=vm) is None


def test_parse_fs_uri_rejects_null_byte():
    assert _parse_fs_uri("exchange://docs/foo\x00bar") is None
    assert _parse_fs_uri("file:///foo\x00bar") is None


def test_canonicalize_and_confine_null_byte_returns_none(tmp_path):
    root = tmp_path / "vol"
    root.mkdir()
    assert canonicalize_and_confine(root / "foo\x00bar", root) is None


def test_resolve_null_byte_uri_returns_none_not_raise(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    vm = {"docs": root}
    assert resolve_filesystem_uri("exchange://docs/foo\x00bar", volume_map=vm) is None
    assert resolve_filesystem_uri("file:///foo\x00bar", volume_map=vm) is None


def test_resolve_file_empty_volume_map_returns_none_no_warn(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri("file:///mnt/x", volume_map={})
    assert result is None
    assert caplog.records == []


# --- symlink-loop safety ---
# A symlink loop must NEVER raise. CPython <=3.12 raises RuntimeError on a
# loop (caught -> None); 3.13+ returns the lexical path instead. For a loop
# that stays WITHIN root, that 3.13 lexical path is itself within root, so
# the function returns it — confinement still holds; the broken link is a
# data-plane concern open() rejects with ELOOP, not a confinement escape.
# The version-independent contract these pin: never raises, and any returned
# path is confined within root (None on <=3.12, the within-root path on
# 3.13+).


def test_confine_symlink_self_loop_confined_or_none_never_raises(tmp_path):
    """A self-referential symlink must never raise; any returned path is
    confined (None on CPython <=3.12, the within-root lexical path on 3.13+)."""
    root = tmp_path / "vol"
    root.mkdir()
    os.symlink(root / "a", root / "a")  # a -> a (self-loop)
    result = canonicalize_and_confine(root / "a" / "x", root)  # must not raise
    assert result is None or result.is_relative_to(root.resolve())


def test_confine_symlink_mutual_loop_confined_or_none_never_raises(tmp_path):
    """A mutual symlink cycle must never raise; any returned path is confined."""
    root = tmp_path / "vol"
    root.mkdir()
    os.symlink(root / "b", root / "a")  # a -> b
    os.symlink(root / "a", root / "b")  # b -> a (mutual loop)
    result = canonicalize_and_confine(root / "a" / "x", root)  # must not raise
    assert result is None or result.is_relative_to(root.resolve())


def test_resolve_exchange_symlink_loop_confined_or_none_never_raises(tmp_path, caplog):
    """exchange:// resolver must never raise on a symlink loop; any returned
    path stays within the volume root, and any warning uses the honest
    'could not be confined' wording (never a false 'escaped' claim)."""
    root = tmp_path / "docs"
    root.mkdir()
    os.symlink(root / "a", root / "a")  # self-loop inside the volume
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri("exchange://docs/a/x", volume_map=vm)
    assert result is None or result.is_relative_to(root.resolve())
    # Version-dependent: <=3.12 warns (loop -> None), 3.13 returns the path
    # silently. Either way, no warning may falsely claim an escape.
    for r in caplog.records:
        assert "escaped" not in r.getMessage()
        assert "could not be confined to its volume root" in r.getMessage()


def test_resolve_file_symlink_loop_confined_or_none_never_raises(tmp_path, caplog):
    """file:// resolver must never raise on a symlink loop; any returned path
    stays within the volume root, and any warning uses the honest 'could not
    be confined' wording (never a false 'not within any volume' claim)."""
    root = tmp_path / "docs"
    root.mkdir()
    os.symlink(root / "a", root / "a")  # self-loop inside the volume
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri(
            "file://" + str(root / "a" / "x"), volume_map=vm
        )
    assert result is None or result.is_relative_to(root.resolve())
    for r in caplog.records:
        assert "could not be confined to any configured volume" in r.getMessage()


# --- absent-root (lazy mount): confinement holds before the mount exists ---
# A volume may mount lazily, so the root can be absent at resolution time and
# resolve() then takes a fully-lexical path. These pin that confinement still
# rejects escapes (and a within-root path is still usable) when root does not
# exist — guarding against a future strict-resolve / root.is_dir() refactor.


def test_confine_escape_rejected_when_root_absent(tmp_path):
    root = tmp_path / "docs"  # intentionally not created
    assert canonicalize_and_confine(root / ".." / "outside", root) is None


def test_confine_within_root_when_root_absent(tmp_path):
    root = tmp_path / "docs"  # intentionally not created
    target = root / "a.bin"
    assert canonicalize_and_confine(target, root) == target.resolve()


def test_resolve_exchange_escape_rejected_when_root_absent(tmp_path, caplog):
    root = tmp_path / "docs"  # intentionally not created
    vm = {"docs": root}
    with caplog.at_level(logging.WARNING):
        result = resolve_filesystem_uri("exchange://docs/../outside", volume_map=vm)
    assert result is None
    assert any(
        "could not be confined to its volume root" in r.getMessage()
        for r in caplog.records
    )


# --- file:// confinement via resolver (Finding 3) ---


def test_resolve_file_symlink_escape_through_resolver_returns_none(tmp_path):
    """A file:// path lexically inside a volume that symlinks out must
    return None — catches a future refactor that swaps canonicalize_and_confine
    for a lexical startswith check."""
    root = tmp_path / "docs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("x")
    (root / "link").symlink_to(outside)
    # Lexically inside root, but resolves outside via the symlink.
    uri = "file://" + str(root / "link" / "secret")
    assert resolve_filesystem_uri(uri, volume_map={"docs": root}) is None


def test_resolve_file_two_volume_map_matches_second_volume(tmp_path):
    """A file:// path inside the SECOND volume entry resolves correctly —
    pins the 'try each volume, accept on first match' loop."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (second / "target.bin").write_text("data")
    vm = {"first": first, "second": second}
    uri = "file://" + str(second / "target.bin")
    result = resolve_filesystem_uri(uri, volume_map=vm)
    assert result == (second / "target.bin").resolve()


def test_resolve_file_nested_volumes_resolve_within_confinement(tmp_path):
    """With nested volumes (/data and /data/uploads), a path under the child
    still resolves — confinement holds whichever volume the first-match loop
    credits. Pins that nested volumes are not a failure; attribution is
    first-match (insertion order), the caller's concern, not confinement's."""
    parent = tmp_path / "data"
    child = parent / "uploads"
    child.mkdir(parents=True)
    (child / "f.bin").write_text("x")
    vm = {"data": parent, "uploads": child}  # parent inserted first
    uri = "file://" + str(child / "f.bin")
    assert resolve_filesystem_uri(uri, volume_map=vm) == (child / "f.bin").resolve()


def test_resolve_file_sibling_with_shared_prefix_returns_none(tmp_path):
    """A file:// path in a sibling dir sharing a string prefix with a volume
    root must return None — pins the resolver's is_relative_to confinement
    against a str.startswith regression (docs vs docs-archive bypass)."""
    docs = tmp_path / "docs"
    docs.mkdir()
    archive = tmp_path / "docs-archive"  # shares the "docs" string prefix
    archive.mkdir()
    (archive / "secret").write_text("x")
    uri = "file://" + str(archive / "secret")
    assert resolve_filesystem_uri(uri, volume_map={"docs": docs}) is None


# --- atomic_write ---


def test_atomic_write_writes_content(tmp_path):
    from io import BytesIO

    target = tmp_path / "out.bin"
    atomic_write(target, BytesIO(b"hello"))
    assert target.read_bytes() == b"hello"


def test_atomic_write_overwrites_existing(tmp_path):
    from io import BytesIO

    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    atomic_write(target, BytesIO(b"new"))
    assert target.read_bytes() == b"new"


def test_atomic_write_cleans_up_and_leaves_target_absent_on_source_error(tmp_path):
    class _Boom:
        def read(self, *_a):
            raise RuntimeError("boom")

    target = tmp_path / "out.bin"
    with pytest.raises(RuntimeError, match="boom"):
        atomic_write(target, _Boom())
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_does_not_clobber_existing_on_source_error(tmp_path):
    target = tmp_path / "out.bin"
    target.write_bytes(b"keep")

    class _Boom:
        def read(self, *_a):
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        atomic_write(target, _Boom())
    assert target.read_bytes() == b"keep"
    assert list(tmp_path.iterdir()) == [target]


def test_atomic_write_requires_existing_parent(tmp_path):
    from io import BytesIO

    target = tmp_path / "missing" / "out.bin"
    with pytest.raises(FileNotFoundError):
        atomic_write(target, BytesIO(b"x"))


def test_atomic_write_closes_fd_when_fdopen_fails(tmp_path, monkeypatch):
    # If os.fdopen raises, the raw fd from mkstemp must still be closed (and the
    # temp removed) — otherwise it leaks for the life of the process.
    import tempfile as _tempfile
    from io import BytesIO

    captured: dict[str, int] = {}
    real_mkstemp = _tempfile.mkstemp

    def spy_mkstemp(*args, **kwargs):
        fd, name = real_mkstemp(*args, **kwargs)
        captured["fd"] = fd
        return fd, name

    monkeypatch.setattr(_tempfile, "mkstemp", spy_mkstemp)

    def boom_fdopen(*_a, **_k):
        raise OSError("fdopen failed")

    monkeypatch.setattr(os, "fdopen", boom_fdopen)

    target = tmp_path / "out.bin"
    with pytest.raises(OSError, match="fdopen failed"):
        atomic_write(target, BytesIO(b"x"))

    # The captured fd must no longer be open (fstat raises on a closed fd),
    # and no temp file or target may be left behind.
    with pytest.raises(OSError):
        os.fstat(captured["fd"])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_atomic_write_reads_from_current_position(tmp_path):
    # atomic_write must NOT seek to 0 — it transfers from the stream's current
    # position so non-seekable streams stay usable and partial transfers work.
    from io import BytesIO

    src = BytesIO(b"skip" + b"keep")
    src.seek(4)  # past "skip"
    atomic_write(tmp_path / "out.bin", src)
    assert (tmp_path / "out.bin").read_bytes() == b"keep"


def test_atomic_write_accepts_str_target(tmp_path):
    # Pins the widened Path | str contract — the body coerces via Path(target).
    from io import BytesIO

    target = tmp_path / "out.bin"
    atomic_write(str(target), BytesIO(b"hi"))
    assert target.read_bytes() == b"hi"


def test_atomic_write_deposits_owner_only_mode(tmp_path):
    # Pins the documented current behavior: deposits inherit mkstemp's 0o600,
    # preserved by os.replace, on both create and overwrite. The deposit
    # permission policy is the filesystem sink's to define (#143, tracked #155).
    from io import BytesIO

    target = tmp_path / "out.bin"
    target.write_bytes(b"old")
    target.chmod(0o644)
    atomic_write(target, BytesIO(b"new"))
    assert target.stat().st_mode & 0o777 == 0o600


def test_atomic_write_explicit_mode_is_applied(tmp_path):
    from io import BytesIO

    from fastmcp_pvl_core._file_exchange._paths import atomic_write

    target = tmp_path / "out.bin"
    atomic_write(target, BytesIO(b"data"), mode=0o664)
    assert target.stat().st_mode & 0o777 == 0o664
