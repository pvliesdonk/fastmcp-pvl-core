"""Tests for the MCP File Exchange v0.3 protocol surface and runtime."""

from __future__ import annotations

import os
import threading
import time as time_module
from pathlib import Path

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FILE_EXCHANGE_SPEC_VERSION,
    ExchangeGroupMismatch,
    ExchangeURI,
    ExchangeURIError,
    FileExchange,
    FileExchangeCapability,
    FileExchangeConfigError,
    FileRef,
    FileRefPreview,
    register_file_exchange_capability,
)

# ---------------------------------------------------------------------------
# FileRefPreview
# ---------------------------------------------------------------------------


class TestFileRefPreview:
    def test_to_dict_omits_unset_optional_fields(self) -> None:
        p = FileRefPreview(description="hello")

        assert p.to_dict() == {"description": "hello"}

    def test_dimensions_emit_width_height_dict(self) -> None:
        p = FileRefPreview(dimensions=(1024, 768))

        assert p.to_dict() == {"dimensions": {"width": 1024, "height": 768}}

    def test_full_round_trip(self) -> None:
        p = FileRefPreview(
            description="circuit board",
            dimensions=(1024, 768),
            thumbnail_base64="abc",
            thumbnail_mime_type="image/jpeg",
            metadata={"prompt": "PCB", "model": "flux"},
        )

        round_tripped = FileRefPreview.from_dict(p.to_dict())

        assert round_tripped == p

    def test_from_dict_rejects_partial_dimensions(self) -> None:
        with pytest.raises(ValueError, match="width.+height"):
            FileRefPreview.from_dict({"dimensions": {"width": 100}})

    def test_from_dict_rejects_null_dimension_value(self) -> None:
        # ``int(None)`` raises TypeError — guard with a None check
        # before coercion so the message stays helpful.
        with pytest.raises(ValueError, match="non-null"):
            FileRefPreview.from_dict({"dimensions": {"width": None, "height": 100}})

    def test_from_dict_rejects_non_mapping_metadata(self) -> None:
        with pytest.raises(ValueError, match="metadata"):
            FileRefPreview.from_dict({"metadata": "not a mapping"})


# ---------------------------------------------------------------------------
# FileRef
# ---------------------------------------------------------------------------


class TestFileRef:
    def test_minimal_round_trip(self) -> None:
        ref = FileRef(
            origin_server="image-mcp",
            origin_id="a1b2c3",
            transfer={"http": {"tool": "create_download_link"}},
        )

        assert FileRef.from_dict(ref.to_dict()) == ref

    def test_spec_example_round_trips(self) -> None:
        # The first example from spec §3.1.
        wire = {
            "origin_server": "image-mcp",
            "origin_id": "a1b2c3",
            "mime_type": "image/png",
            "size_bytes": 245760,
            "preview": {
                "description": "Generated circuit board diagram, top-down view",
                "dimensions": {"width": 1024, "height": 768},
            },
            "transfer": {
                "exchange": {"uri": "exchange://hades-01/image-mcp/a1b2c3.png"},
                "http": {"tool": "create_download_link"},
            },
        }

        ref = FileRef.from_dict(wire)

        assert ref.origin_server == "image-mcp"
        assert ref.preview is not None
        assert ref.preview.dimensions == (1024, 768)
        assert ref.to_dict() == wire

    def test_from_dict_rejects_missing_required_field(self) -> None:
        with pytest.raises(ValueError, match="origin_server"):
            FileRef.from_dict({"origin_id": "x", "transfer": {"http": {}}})

    def test_from_dict_rejects_empty_transfer(self) -> None:
        with pytest.raises(ValueError, match="transfer"):
            FileRef.from_dict({"origin_server": "s", "origin_id": "x", "transfer": {}})

    def test_from_dict_coerces_size_bytes_to_int(self) -> None:
        # JSON parsers may yield size_bytes as a float (e.g. 245760.0);
        # the dataclass annotation promises int, so coerce.
        ref = FileRef.from_dict(
            {
                "origin_server": "s",
                "origin_id": "x",
                "size_bytes": 245760.0,
                "transfer": {"http": {}},
            }
        )

        assert ref.size_bytes == 245760
        assert isinstance(ref.size_bytes, int)

    def test_from_dict_rejects_explicit_null_required_field(self) -> None:
        # ``str(None)`` would silently become ``"None"`` — catch that.
        with pytest.raises(ValueError, match="origin_server"):
            FileRef.from_dict(
                {
                    "origin_server": None,
                    "origin_id": "x",
                    "transfer": {"http": {}},
                }
            )

    def test_from_dict_rejects_non_mapping_transfer_value(self) -> None:
        with pytest.raises(ValueError, match=r"transfer\['http'\]"):
            FileRef.from_dict(
                {
                    "origin_server": "s",
                    "origin_id": "x",
                    "transfer": {"http": "not a mapping"},
                }
            )


# ---------------------------------------------------------------------------
# ExchangeURI.parse
# ---------------------------------------------------------------------------


class TestExchangeURIParse:
    def test_parses_canonical_form(self) -> None:
        uri = ExchangeURI.parse("exchange://hades-01/image-mcp/a1b2c3.png")

        assert uri.exchange_id == "hades-01"
        assert uri.namespace == "image-mcp"
        assert uri.id == "a1b2c3"
        assert uri.ext == "png"
        assert uri.filename == "a1b2c3.png"

    def test_str_round_trips(self) -> None:
        original = "exchange://hades-01/image-mcp/a1b2c3.png"

        assert str(ExchangeURI.parse(original)) == original

    def test_id_with_dots_uses_rightmost_split(self) -> None:
        uri = ExchangeURI.parse("exchange://g/n/a.b.c.png")

        assert uri.id == "a.b.c"
        assert uri.ext == "png"

    @pytest.mark.parametrize(
        "uri",
        [
            "https://example.com/foo",  # wrong scheme
            "exchange://only-two/segments",  # too few segments
            "exchange://a/b/c/d.png",  # too many segments
        ],
    )
    def test_rejects_malformed_uri(self, uri: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.parse(uri)

    @pytest.mark.parametrize(
        "uri",
        [
            "exchange://g/../escaped.png",  # parent traversal in namespace
            "exchange://g/n/..",  # filename is ".."
            "exchange://g/n/.",  # filename is "."
            "exchange://g/n/foo%2Fbar.png",  # encoded slash decodes to /
            "exchange://g/n/foo%5Cbar.png",  # encoded backslash
            "exchange://g/n/foo%00.png",  # null byte
            "exchange://g/ /file.png",  # whitespace-only namespace
            "exchange://g/n/file.png?foo=bar",  # query string slipped in
            "exchange://g/n/file.png#frag",  # fragment slipped in
            "exchange://g/n%3Ffoo/file.png",  # encoded ? in namespace
            "exchange://g/n/file.png%23frag",  # encoded # in ext
        ],
    )
    def test_rejects_path_traversal_and_forbidden_chars(self, uri: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.parse(uri)

    def test_rejects_double_encoded_payload(self) -> None:
        # %252e%252e%252f decodes once to %2e%2e%2f — residual %XX
        # patterns are the double-encoding signature and must be
        # rejected before a second decode could turn them into "../".
        with pytest.raises(ExchangeURIError, match="double-encoded"):
            ExchangeURI.parse("exchange://g/n/%252e%252e%252f.png")

    def test_rejects_namespace_starting_with_dot(self) -> None:
        with pytest.raises(ExchangeURIError, match="dot"):
            ExchangeURI.parse("exchange://g/.hidden/file.png")

    def test_rejects_filename_without_extension(self) -> None:
        with pytest.raises(ExchangeURIError, match="dot"):
            ExchangeURI.parse("exchange://g/n/no-extension")

    def test_rejects_filename_with_trailing_dot(self) -> None:
        with pytest.raises(ExchangeURIError, match="ext is empty"):
            ExchangeURI.parse("exchange://g/n/file.")

    def test_rejects_filename_with_leading_dot_only(self) -> None:
        # ".png" rpartitions to ("", ".", "png") — id is empty.
        with pytest.raises(ExchangeURIError, match="id is empty"):
            ExchangeURI.parse("exchange://g/n/.png")


# ---------------------------------------------------------------------------
# ExchangeURI.validate_segment
# ---------------------------------------------------------------------------


class TestValidateSegment:
    def test_uri_role_decodes_once(self) -> None:
        # %2d is "-"; one decode pass is the spec contract.
        result = ExchangeURI.validate_segment("foo%2dbar", role="uri")

        assert result == "foo-bar"

    def test_json_param_role_does_not_decode(self) -> None:
        # An origin_id with a literal "%" must round-trip verbatim;
        # decoding it would corrupt the value (spec §6.3).
        result = ExchangeURI.validate_segment("req-%20-id", role="json_param")

        assert result == "req-%20-id"

    def test_uri_role_rejects_residual_encoding(self) -> None:
        with pytest.raises(ExchangeURIError, match="double-encoded"):
            ExchangeURI.validate_segment("%252e", role="uri")

    @pytest.mark.parametrize(
        ("value", "role"),
        [
            ("..", "json_param"),
            (".", "uri"),
            ("foo/bar", "json_param"),
            ("foo\\bar", "json_param"),
            ("foo\x00bar", "json_param"),
            ("foo\x01bar", "json_param"),
            (" foo", "json_param"),
            ("foo ", "json_param"),
            ("", "json_param"),
        ],
    )
    def test_segment_rules(self, value: str, role: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.validate_segment(value, role=role)

    def test_unknown_role_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="role must be"):
            ExchangeURI.validate_segment("x", role="bogus")


# ---------------------------------------------------------------------------
# FileExchangeCapability + register_file_exchange_capability
# ---------------------------------------------------------------------------


class TestCapabilityDeclaration:
    def test_to_capability_dict_matches_spec_producer_example(self) -> None:
        # Spec §3.9 producer example, verbatim.
        cap = FileExchangeCapability(
            namespace="image-mcp",
            exchange_id="hades-01",
            produces=["image/png", "image/webp", "image/jpeg"],
            consumes=[],
            transfer_methods={
                "exchange": {},
                "http": {"tool": "create_download_link"},
            },
        )

        assert cap.to_capability_dict() == {
            "version": "0.3",
            "namespace": "image-mcp",
            "exchange_id": "hades-01",
            "produces": ["image/png", "image/webp", "image/jpeg"],
            "consumes": [],
            "transfer_methods": {
                "exchange": {},
                "http": {"tool": "create_download_link"},
            },
        }

    def test_default_version_is_spec_version_constant(self) -> None:
        cap = FileExchangeCapability(namespace="n", transfer_methods={"http": {}})

        assert cap.version == FILE_EXCHANGE_SPEC_VERSION
        assert cap.version == "0.3"

    def test_rejects_namespace_starting_with_dot(self) -> None:
        # Spec §3.8: namespace MUST NOT start with a dot. Catching at
        # construction prevents propagation into capability dicts and
        # exchange:// URIs.
        with pytest.raises(ExchangeURIError, match="dot"):
            FileExchangeCapability(
                namespace=".hidden",
                transfer_methods={"http": {"tool": "fetch"}},
            )

    def test_rejects_namespace_with_path_separator(self) -> None:
        with pytest.raises(ExchangeURIError):
            FileExchangeCapability(
                namespace="bad/namespace",
                transfer_methods={"http": {"tool": "fetch"}},
            )

    def test_rejects_invalid_exchange_id(self) -> None:
        with pytest.raises(ExchangeURIError):
            FileExchangeCapability(
                namespace="ok",
                exchange_id="bad/id",
                transfer_methods={"http": {"tool": "fetch"}},
            )

    def test_exchange_id_omitted_when_none(self) -> None:
        cap = FileExchangeCapability(
            namespace="n",
            transfer_methods={"http": {"tool": "fetch"}},
            exchange_id=None,
        )

        assert "exchange_id" not in cap.to_capability_dict()

    def test_register_populates_initialize_options(self) -> None:
        # End-to-end: registering the capability must make it visible in
        # the InitializationOptions FastMCP returns to MCP clients.
        mcp = FastMCP("test")
        cap = FileExchangeCapability(
            namespace="image-mcp",
            transfer_methods={"http": {"tool": "create_download_link"}},
            exchange_id="hades-01",
            produces=["image/png"],
        )

        register_file_exchange_capability(mcp, cap)

        opts = mcp._mcp_server.create_initialization_options()
        assert opts.capabilities.experimental == {
            "file_exchange": cap.to_capability_dict()
        }

    def test_register_replaces_previous_payload_without_stacking(self) -> None:
        # Calling twice must not nest wrappers — the second registration
        # wins; the first payload is gone.
        mcp = FastMCP("test")
        register_file_exchange_capability(
            mcp,
            FileExchangeCapability(namespace="v1", transfer_methods={"http": {}}),
        )
        register_file_exchange_capability(
            mcp,
            FileExchangeCapability(namespace="v2", transfer_methods={"http": {}}),
        )

        opts = mcp._mcp_server.create_initialization_options()
        assert opts.capabilities.experimental is not None
        assert opts.capabilities.experimental["file_exchange"]["namespace"] == "v2"

    def test_register_preserves_caller_supplied_experimental_capabilities(
        self,
    ) -> None:
        # If FastMCP (or another wrapper) ever passes its own dict, our
        # injection must merge — not stomp.
        mcp = FastMCP("test")
        register_file_exchange_capability(
            mcp,
            FileExchangeCapability(namespace="n", transfer_methods={"http": {}}),
        )

        opts = mcp._mcp_server.create_initialization_options(
            experimental_capabilities={"other_extension": {"foo": "bar"}}
        )
        assert opts.capabilities.experimental == {
            "other_extension": {"foo": "bar"},
            "file_exchange": {
                "version": "0.3",
                "namespace": "n",
                "produces": [],
                "consumes": [],
                "transfer_methods": {"http": {}},
            },
        }


# ---------------------------------------------------------------------------
# FileExchange runtime
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_unconfigured_when_dir_unset(self) -> None:
        fx = FileExchange.from_env(default_namespace="test", env={})

        assert fx.is_configured is False
        with pytest.raises(FileExchangeConfigError):
            _ = fx.exchange_id
        with pytest.raises(FileExchangeConfigError):
            _ = fx.namespace

    def test_unconfigured_when_dir_blank(self) -> None:
        # Empty / whitespace-only env value should be treated as unset.
        fx = FileExchange.from_env(
            default_namespace="test", env={"MCP_EXCHANGE_DIR": "   "}
        )

        assert fx.is_configured is False

    def test_raises_when_dir_does_not_exist(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope"

        with pytest.raises(FileExchangeConfigError, match="does not exist"):
            FileExchange.from_env(
                default_namespace="test",
                env={"MCP_EXCHANGE_DIR": str(missing)},
            )

    def test_raises_when_dir_is_a_file(self, tmp_path: Path) -> None:
        not_a_dir = tmp_path / "f"
        not_a_dir.write_text("hi")

        with pytest.raises(FileExchangeConfigError, match="not a directory"):
            FileExchange.from_env(
                default_namespace="test",
                env={"MCP_EXCHANGE_DIR": str(not_a_dir)},
            )

    def test_namespace_env_wins_over_default(self, tmp_path: Path) -> None:
        fx = FileExchange.from_env(
            default_namespace="from-default",
            env={
                "MCP_EXCHANGE_DIR": str(tmp_path),
                "MCP_EXCHANGE_NAMESPACE": "from-env",
            },
        )

        assert fx.namespace == "from-env"

    def test_default_namespace_used_when_env_unset(self, tmp_path: Path) -> None:
        fx = FileExchange.from_env(
            default_namespace="from-default",
            env={"MCP_EXCHANGE_DIR": str(tmp_path)},
        )

        assert fx.namespace == "from-default"

    def test_missing_namespace_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileExchangeConfigError, match="namespace"):
            FileExchange.from_env(env={"MCP_EXCHANGE_DIR": str(tmp_path)})

    def test_namespace_with_dot_prefix_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExchangeURIError, match="dot"):
            FileExchange.from_env(
                default_namespace=".hidden",
                env={"MCP_EXCHANGE_DIR": str(tmp_path)},
            )

    def test_namespace_with_path_separator_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ExchangeURIError):
            FileExchange.from_env(
                default_namespace="bad/ns",
                env={"MCP_EXCHANGE_DIR": str(tmp_path)},
            )

    def test_explicit_exchange_id_with_path_separator_rejected(
        self, tmp_path: Path
    ) -> None:
        # An explicit MCP_EXCHANGE_ID containing forbidden chars would
        # otherwise persist to .exchange-id and only break later at
        # write/read time. Fail at config-load instead.
        with pytest.raises(ExchangeURIError):
            FileExchange.from_env(
                default_namespace="ns",
                env={
                    "MCP_EXCHANGE_DIR": str(tmp_path),
                    "MCP_EXCHANGE_ID": "bad/group",
                },
            )

    def test_explicit_exchange_id_persists_to_disk(self, tmp_path: Path) -> None:
        fx = FileExchange.from_env(
            default_namespace="test",
            env={
                "MCP_EXCHANGE_DIR": str(tmp_path),
                "MCP_EXCHANGE_ID": "hades-01",
            },
        )

        assert fx.exchange_id == "hades-01"
        assert (tmp_path / ".exchange-id").read_text(
            encoding="utf-8"
        ).strip() == "hades-01"

    def test_explicit_exchange_id_conflict_raises(self, tmp_path: Path) -> None:
        # Pre-populate .exchange-id with a different value.
        (tmp_path / ".exchange-id").write_text("hades-01\n", encoding="utf-8")

        with pytest.raises(ExchangeGroupMismatch, match="conflicts"):
            FileExchange.from_env(
                default_namespace="test",
                env={
                    "MCP_EXCHANGE_DIR": str(tmp_path),
                    "MCP_EXCHANGE_ID": "cloud-02",
                },
            )

    def test_explicit_exchange_id_matching_existing_succeeds(
        self, tmp_path: Path
    ) -> None:
        # Pre-existing file with whitespace + uppercase to exercise the
        # spec-mandated normalisation: strip trailing whitespace, accept
        # case as-is.
        (tmp_path / ".exchange-id").write_text("HADES-01\n", encoding="utf-8")

        fx = FileExchange.from_env(
            default_namespace="test",
            env={
                "MCP_EXCHANGE_DIR": str(tmp_path),
                "MCP_EXCHANGE_ID": "HADES-01",
            },
        )

        assert fx.exchange_id == "HADES-01"

    def test_concurrent_from_env_creates_exactly_one_exchange_id(
        self, tmp_path: Path
    ) -> None:
        # Race test (acceptance criterion): N concurrent from_env calls
        # against an empty exchange dir produce exactly one .exchange-id
        # file. Verifies O_EXCL semantics — without it, the rename-based
        # alternative would silently overwrite.
        env = {"MCP_EXCHANGE_DIR": str(tmp_path)}
        barrier = threading.Barrier(16)
        ids: list[str] = []
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                barrier.wait()
                fx = FileExchange.from_env(default_namespace="test", env=env)
                ids.append(fx.exchange_id)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        assert len(ids) == 16
        assert len(set(ids)) == 1  # all winners agree
        # Exactly one .exchange-id; no .exchange-id.tmp leftovers.
        files = sorted(p.name for p in tmp_path.iterdir())
        assert files == [".exchange-id"]


class TestWriteAtomic:
    def _make(self, tmp_path: Path, **kwargs: object) -> FileExchange:
        env = {
            "MCP_EXCHANGE_DIR": str(tmp_path),
            "MCP_EXCHANGE_ID": "g1",
            "MCP_EXCHANGE_NAMESPACE": "image-mcp",
        }
        return FileExchange.from_env(default_namespace="image-mcp", env=env, **kwargs)  # type: ignore[arg-type]

    def test_writes_file_and_returns_uri(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        uri = fx.write_atomic(
            origin_id="a1b2c3",
            ext="png",
            content=b"PNG-bytes",
            mime_type="image/png",
        )

        assert uri == "exchange://g1/image-mcp/a1b2c3.png"
        on_disk = tmp_path / "image-mcp" / "a1b2c3.png"
        assert on_disk.read_bytes() == b"PNG-bytes"

    def test_creates_namespace_directory(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        fx.write_atomic(origin_id="x", ext="png", content=b"")

        ns_dir = tmp_path / "image-mcp"
        assert ns_dir.is_dir()

    def test_rejects_origin_id_with_path_separator(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        with pytest.raises(ExchangeURIError):
            fx.write_atomic(origin_id="../escape", ext="png", content=b"")

    def test_rejects_dot_prefix_origin_id(self, tmp_path: Path) -> None:
        # Storage-leak guard: a dot-prefix filename would be invisible
        # to consumers (read_exchange_uri rejects it) AND skipped by
        # sweep, so it would accumulate forever on the shared volume.
        fx = self._make(tmp_path)

        with pytest.raises(ExchangeURIError, match="dot"):
            fx.write_atomic(origin_id=".hidden", ext="png", content=b"x")

    def test_rejects_dot_prefix_ext(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        with pytest.raises(ExchangeURIError, match="dot"):
            fx.write_atomic(origin_id="x", ext=".png", content=b"x")

    def test_rejects_ext_with_dot(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        # An ext value of "." or ".." should fail the segment rules.
        with pytest.raises(ExchangeURIError):
            fx.write_atomic(origin_id="x", ext="..", content=b"")

    def test_unconfigured_write_raises(self) -> None:
        fx = FileExchange.from_env(default_namespace="x", env={})

        with pytest.raises(FileExchangeConfigError):
            fx.write_atomic(origin_id="x", ext="png", content=b"")

    def test_no_tmp_file_lingers_after_successful_write(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)
        fx.write_atomic(origin_id="x", ext="png", content=b"data")

        ns_files = sorted(p.name for p in (tmp_path / "image-mcp").iterdir())
        assert ns_files == ["x.png"]

    def test_simulated_crash_leaves_only_dotfile_invisible_to_consumer(
        self, tmp_path: Path
    ) -> None:
        # Atomic-write acceptance: simulate kill -9 mid-write by
        # manually creating ``.{id}.{ext}.tmp`` in the namespace dir.
        # A consumer asking for the final URI must get FileNotFoundError —
        # the dotfile filter holds at the URI scheme level.
        fx = self._make(tmp_path)
        ns_dir = tmp_path / "image-mcp"
        ns_dir.mkdir()
        (ns_dir / ".x.png.tmp").write_bytes(b"partial")

        with pytest.raises(FileNotFoundError):
            fx.read_exchange_uri("exchange://g1/image-mcp/x.png")


class TestReadExchangeUri:
    def _make(self, tmp_path: Path, *, exchange_id: str = "g1") -> FileExchange:
        env = {
            "MCP_EXCHANGE_DIR": str(tmp_path),
            "MCP_EXCHANGE_ID": exchange_id,
            "MCP_EXCHANGE_NAMESPACE": "vault-mcp",
        }
        return FileExchange.from_env(default_namespace="vault-mcp", env=env)

    def test_round_trips_own_namespace(self, tmp_path: Path) -> None:
        env = {
            "MCP_EXCHANGE_DIR": str(tmp_path),
            "MCP_EXCHANGE_ID": "g1",
            "MCP_EXCHANGE_NAMESPACE": "image-mcp",
        }
        fx = FileExchange.from_env(default_namespace="image-mcp", env=env)
        uri = fx.write_atomic(origin_id="abc", ext="png", content=b"hello")

        data = fx.read_exchange_uri(uri)

        assert data == b"hello"

    def test_cross_namespace_read(self, tmp_path: Path) -> None:
        # Acceptance: vault-mcp can read exchange://g1/image-mcp/foo.png
        # written by image-mcp on the same volume.
        producer_env = {
            "MCP_EXCHANGE_DIR": str(tmp_path),
            "MCP_EXCHANGE_ID": "g1",
            "MCP_EXCHANGE_NAMESPACE": "image-mcp",
        }
        producer = FileExchange.from_env(
            default_namespace="image-mcp", env=producer_env
        )
        producer.write_atomic(origin_id="foo", ext="png", content=b"image-bytes")

        consumer = self._make(tmp_path, exchange_id="g1")

        data = consumer.read_exchange_uri("exchange://g1/image-mcp/foo.png")

        assert data == b"image-bytes"

    def test_group_mismatch_raises_with_both_ids(self, tmp_path: Path) -> None:
        # .exchange-id is fixed to g1 so the from_env call below would
        # ordinarily collide with the explicit MCP_EXCHANGE_ID we pass.
        # Build the consumer with a different volume so it gets its own
        # exchange_id, then read a URI from the wrong group.
        local_dir = tmp_path / "local"
        local_dir.mkdir()
        consumer = self._make(local_dir, exchange_id="local-grp")

        with pytest.raises(ExchangeGroupMismatch) as exc_info:
            consumer.read_exchange_uri("exchange://remote-grp/n/file.png")

        msg = str(exc_info.value)
        assert "local-grp" in msg
        assert "remote-grp" in msg

    def test_missing_file_raises_filenotfound(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        with pytest.raises(FileNotFoundError):
            fx.read_exchange_uri("exchange://g1/vault-mcp/missing.png")

    def test_double_encoded_uri_rejected(self, tmp_path: Path) -> None:
        # Acceptance: read_exchange_uri rejects %252e%252e%252f payloads
        # before any filesystem access.
        fx = self._make(tmp_path)

        with pytest.raises(ExchangeURIError, match="double-encoded"):
            fx.read_exchange_uri("exchange://g1/vault-mcp/%252e%252e%252f.png")

    def test_path_traversal_uri_rejected(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        with pytest.raises(ExchangeURIError):
            fx.read_exchange_uri("exchange://g1/../escaped/file.png")

    def test_dotfile_filename_rejected(self, tmp_path: Path) -> None:
        # Even when the URI parses, a dotfile id is the producer's
        # in-progress write — consumers MUST ignore it.
        fx = self._make(tmp_path)
        # First write a real file we'll point near.
        (tmp_path / "vault-mcp").mkdir()
        (tmp_path / "vault-mcp" / ".secret.png").write_bytes(b"hidden")

        with pytest.raises(ExchangeURIError, match="dot"):
            fx.read_exchange_uri("exchange://g1/vault-mcp/.secret.png")

    def test_unconfigured_read_raises(self) -> None:
        fx = FileExchange.from_env(default_namespace="x", env={})

        with pytest.raises(FileExchangeConfigError):
            fx.read_exchange_uri("exchange://g/n/x.png")


class TestSweep:
    def _make(self, tmp_path: Path, **kwargs: object) -> FileExchange:
        env = {
            "MCP_EXCHANGE_DIR": str(tmp_path),
            "MCP_EXCHANGE_ID": "g1",
            "MCP_EXCHANGE_NAMESPACE": "ns",
        }
        return FileExchange.from_env(default_namespace="ns", env=env, **kwargs)  # type: ignore[arg-type]

    def test_no_op_when_unconfigured(self) -> None:
        fx = FileExchange.from_env(default_namespace="x", env={})

        assert fx.sweep() == 0

    def test_no_op_when_namespace_dir_missing(self, tmp_path: Path) -> None:
        fx = self._make(tmp_path)

        # Don't write anything; namespace dir doesn't exist yet.
        assert fx.sweep() == 0

    def test_ttl_eviction_removes_old_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fx = self._make(tmp_path, ttl_seconds=60)
        fx.write_atomic(origin_id="old", ext="png", content=b"a")
        fx.write_atomic(origin_id="fresh", ext="png", content=b"b")

        # Backdate "old" so it predates the TTL window.
        ns = tmp_path / "ns"
        old_path = ns / "old.png"
        os.utime(old_path, (time_module.time() - 3600, time_module.time() - 3600))

        evicted = fx.sweep()

        assert evicted == 1
        assert not old_path.exists()
        assert (ns / "fresh.png").exists()

    def test_lru_eviction_below_ceiling(self, tmp_path: Path) -> None:
        # ttl very long so TTL pass is a no-op — only LRU runs.
        fx = self._make(tmp_path, ttl_seconds=10**9, storage_ceiling_bytes=10)
        # Three files, 5 bytes each → 15 total, ceiling 10 → must evict
        # at least the oldest until <=10.
        fx.write_atomic(origin_id="a", ext="bin", content=b"AAAAA")
        fx.write_atomic(origin_id="b", ext="bin", content=b"BBBBB")
        fx.write_atomic(origin_id="c", ext="bin", content=b"CCCCC")
        ns = tmp_path / "ns"
        # Force a deterministic mtime ordering: a oldest, c newest.
        now = time_module.time()
        os.utime(ns / "a.bin", (now - 30, now - 30))
        os.utime(ns / "b.bin", (now - 20, now - 20))
        os.utime(ns / "c.bin", (now - 10, now - 10))

        evicted = fx.sweep()

        assert evicted >= 1
        # Oldest must be gone.
        assert not (ns / "a.bin").exists()
        # Newest must still be there.
        assert (ns / "c.bin").exists()

    def test_sweep_ignores_dotfiles(self, tmp_path: Path) -> None:
        # An in-progress write (dotfile) MUST NOT be evicted — that
        # would race with the producer's own atomic write.
        fx = self._make(tmp_path, ttl_seconds=1)
        ns = tmp_path / "ns"
        ns.mkdir()
        (ns / ".inflight.png.tmp").write_bytes(b"x")
        os.utime(ns / ".inflight.png.tmp", (0, 0))  # ancient

        fx.sweep()

        assert (ns / ".inflight.png.tmp").exists()


class TestModuleDocs:
    """Acceptance: module docstring documents the lifecycle constraints."""

    def test_docstring_mentions_producer_and_consumer_constraints(self) -> None:
        from fastmcp_pvl_core import _file_exchange

        doc = _file_exchange.__doc__ or ""
        assert "producing server" in doc.lower() or "producer" in doc.lower()
        assert "consum" in doc.lower()
        assert "read-only" in doc.lower()
