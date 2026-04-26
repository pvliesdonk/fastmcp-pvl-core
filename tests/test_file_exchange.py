"""Tests for the MCP File Exchange v0.3 protocol surface."""

from __future__ import annotations

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core import (
    FILE_EXCHANGE_SPEC_VERSION,
    ExchangeURI,
    ExchangeURIError,
    FileExchangeCapability,
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
