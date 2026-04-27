"""Tests for :mod:`fastmcp_pvl_core._file_exchange_protocol`."""

from __future__ import annotations

import asyncio

import pytest
from fastmcp import FastMCP

from fastmcp_pvl_core._file_exchange_protocol import (
    SPEC_VERSION,
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
    def test_empty_to_dict(self) -> None:
        assert FileRefPreview().to_dict() == {}

    def test_full_round_trip(self) -> None:
        desc = "Generated circuit board diagram, top-down view, 4-layer PCB"
        original = FileRefPreview(
            description=desc,
            dimensions=(1024, 768),
            thumbnail_base64="/9j/4AAQSkZJRg",
            thumbnail_mime_type="image/jpeg",
            metadata={"prompt": "top-down view of a 4-layer PCB", "model": "flux"},
        )
        d = original.to_dict()
        assert d == {
            "description": desc,
            "dimensions": {"width": 1024, "height": 768},
            "thumbnail_base64": "/9j/4AAQSkZJRg",
            "thumbnail_mime_type": "image/jpeg",
            "metadata": {"prompt": "top-down view of a 4-layer PCB", "model": "flux"},
        }
        assert FileRefPreview.from_dict(d) == original

    def test_partial_fields_omit_nones(self) -> None:
        d = FileRefPreview(description="Just a description").to_dict()
        assert d == {"description": "Just a description"}

    def test_dimensions_must_have_both(self) -> None:
        with pytest.raises(ValueError, match="dimensions"):
            FileRefPreview.from_dict({"dimensions": {"width": 100}})

    def test_metadata_must_be_mapping(self) -> None:
        with pytest.raises(ValueError, match="metadata"):
            FileRefPreview.from_dict({"metadata": ["not", "a", "mapping"]})


# ---------------------------------------------------------------------------
# FileRef
# ---------------------------------------------------------------------------


class TestFileRef:
    def test_minimum_required_fields(self) -> None:
        ref = FileRef(
            origin_server="image-mcp",
            origin_id="a1b2c3",
            transfer={"http": {"tool": "create_download_link"}},
        )
        assert ref.to_dict() == {
            "origin_server": "image-mcp",
            "origin_id": "a1b2c3",
            "transfer": {"http": {"tool": "create_download_link"}},
        }

    def test_spec_3_1_example_round_trips(self) -> None:
        # Verbatim from spec §3.1 example.
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
        parsed = FileRef.from_dict(wire)
        assert parsed.origin_server == "image-mcp"
        assert parsed.origin_id == "a1b2c3"
        assert parsed.mime_type == "image/png"
        assert parsed.size_bytes == 245760
        assert parsed.preview is not None
        assert parsed.preview.dimensions == (1024, 768)
        assert parsed.to_dict() == wire

    def test_missing_required_field_raises(self) -> None:
        for missing in ("origin_server", "origin_id", "transfer"):
            wire = {
                "origin_server": "image-mcp",
                "origin_id": "x",
                "transfer": {"http": {"tool": "t"}},
            }
            del wire[missing]  # type: ignore[arg-type]
            with pytest.raises(ValueError, match=missing):
                FileRef.from_dict(wire)

    def test_explicit_null_required_field_raises(self) -> None:
        with pytest.raises(ValueError, match="origin_server"):
            FileRef.from_dict(
                {
                    "origin_server": None,
                    "origin_id": "x",
                    "transfer": {"http": {"tool": "t"}},
                }
            )

    def test_empty_transfer_raises(self) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            FileRef.from_dict({"origin_server": "s", "origin_id": "x", "transfer": {}})

    def test_size_bytes_coerces_float_to_int(self) -> None:
        ref = FileRef.from_dict(
            {
                "origin_server": "s",
                "origin_id": "x",
                "transfer": {"http": {"tool": "t"}},
                "size_bytes": 245760.0,
            }
        )
        assert ref.size_bytes == 245760
        assert isinstance(ref.size_bytes, int)


# ---------------------------------------------------------------------------
# ExchangeURI
# ---------------------------------------------------------------------------


class TestExchangeURIParse:
    def test_simple_uri(self) -> None:
        uri = ExchangeURI.parse("exchange://hades-01/image-mcp/a1b2c3.png")
        assert uri.exchange_id == "hades-01"
        assert uri.namespace == "image-mcp"
        assert uri.id == "a1b2c3"
        assert uri.ext == "png"
        assert uri.filename == "a1b2c3.png"
        assert str(uri) == "exchange://hades-01/image-mcp/a1b2c3.png"

    def test_uri_with_compound_extension(self) -> None:
        # rpartition gives the right-most dot — only the last segment is "ext".
        uri = ExchangeURI.parse("exchange://g/n/archive.tar.gz")
        assert uri.id == "archive.tar"
        assert uri.ext == "gz"

    def test_wrong_scheme_rejected(self) -> None:
        with pytest.raises(ExchangeURIError, match="scheme"):
            ExchangeURI.parse("http://hades-01/image-mcp/a.png")

    def test_query_or_fragment_rejected(self) -> None:
        with pytest.raises(ExchangeURIError, match="query or fragment"):
            ExchangeURI.parse("exchange://g/n/a.png?param=1")
        with pytest.raises(ExchangeURIError, match="query or fragment"):
            ExchangeURI.parse("exchange://g/n/a.png#frag")

    @pytest.mark.parametrize(
        "bad_uri",
        [
            "exchange://g/n/file",  # no extension dot
            "exchange://g/n/.png",  # empty id (leading dot)
            "exchange://g/n/file.",  # empty ext (trailing dot)
            "exchange://g/n",  # missing filename segment
            "exchange://g/n/a/b.png",  # too many segments
            "exchange:///n/a.png",  # missing exchange_id
        ],
    )
    def test_malformed_shapes_rejected(self, bad_uri: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.parse(bad_uri)

    @pytest.mark.parametrize(
        "bad_uri",
        [
            "exchange://g/n/%2e%2e.png",  # decodes to '..' → traversal
            "exchange://g/n/%2ffoo.png",  # decodes to '/foo.png' → separator
            "exchange://g/n/%5cfoo.png",  # decodes to '\foo.png' → separator
            "exchange://g/n/%00.png",  # decodes to '\0.png' → null byte
            "exchange://g/n/%252e%252e%252fpasswd.png",  # double-encoded
            "exchange://g/.hidden/a.png",  # namespace starts with dot
        ],
    )
    def test_security_violations_rejected(self, bad_uri: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.parse(bad_uri)


class TestValidateSegment:
    def test_uri_role_decodes_once(self) -> None:
        # %20 decodes to a space — leading/trailing whitespace is forbidden,
        # but a space in the middle is permitted (spec §3.7).
        assert ExchangeURI.validate_segment("foo%20bar", role="uri") == "foo bar"

    def test_uri_role_rejects_double_encoded(self) -> None:
        # %252e is the percent-encoding of %2e — once-decoded yields %2e
        # (the encoding of '.'), which is residual percent-encoding and
        # signals the value was double-encoded. Spec §3.7 rejects this.
        with pytest.raises(ExchangeURIError, match="double-encoded"):
            ExchangeURI.validate_segment("%252e", role="uri")

    def test_json_param_role_does_not_decode(self) -> None:
        # A literal '%' in origin_id is data — must round-trip verbatim.
        # ``req-%20-id`` decoded would mutate to ``req- -id`` (corruption);
        # JSON-param mode never decodes, so the value is preserved.
        assert (
            ExchangeURI.validate_segment("req-%20-id", role="json_param")
            == "req-%20-id"
        )

    def test_json_param_role_rejects_traversal_chars(self) -> None:
        with pytest.raises(ExchangeURIError, match="forbidden character"):
            ExchangeURI.validate_segment("a/b", role="json_param")
        with pytest.raises(ExchangeURIError, match="path traversal"):
            ExchangeURI.validate_segment("..", role="json_param")
        with pytest.raises(ExchangeURIError, match="forbidden character"):
            ExchangeURI.validate_segment("with\x00null", role="json_param")

    def test_invalid_role_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="role must be"):
            ExchangeURI.validate_segment(
                "foo",
                role="bogus",  # type: ignore[arg-type]
            )

    @pytest.mark.parametrize(
        "bad",
        [
            "",  # empty
            " leading",  # leading whitespace
            "trailing ",  # trailing whitespace
            ".",  # current dir
            "..",  # parent dir
        ],
    )
    def test_uri_role_rejects_basic_violations(self, bad: str) -> None:
        with pytest.raises(ExchangeURIError):
            ExchangeURI.validate_segment(bad, role="uri")


# ---------------------------------------------------------------------------
# FileExchangeCapability
# ---------------------------------------------------------------------------


class TestFileExchangeCapability:
    def test_minimum_required_fields(self) -> None:
        cap = FileExchangeCapability(
            namespace="image-mcp",
            transfer_methods={"http": {"tool": "create_download_link"}},
        )
        assert cap.to_capability_dict() == {
            "version": SPEC_VERSION,
            "namespace": "image-mcp",
            "produces": [],
            "consumes": [],
            "transfer_methods": {"http": {"tool": "create_download_link"}},
        }

    def test_full_round_trip_matches_spec_3_9(self) -> None:
        # Verbatim shape from spec §3.9 producer example.
        cap = FileExchangeCapability(
            namespace="image-mcp",
            exchange_id="hades-01",
            produces=("image/png", "image/webp", "image/jpeg"),
            consumes=(),
            transfer_methods={
                "exchange": {},
                "http": {"tool": "create_download_link"},
            },
        )
        d = cap.to_capability_dict()
        assert d == {
            "version": "0.2",
            "namespace": "image-mcp",
            "exchange_id": "hades-01",
            "produces": ["image/png", "image/webp", "image/jpeg"],
            "consumes": [],
            "transfer_methods": {
                "exchange": {},
                "http": {"tool": "create_download_link"},
            },
        }

    def test_invalid_namespace_rejected_at_construction(self) -> None:
        with pytest.raises(ExchangeURIError):
            FileExchangeCapability(
                namespace="bad/namespace",
                transfer_methods={"http": {"tool": "t"}},
            )
        with pytest.raises(ExchangeURIError, match="dot"):
            FileExchangeCapability(
                namespace=".hidden",
                transfer_methods={"http": {"tool": "t"}},
            )

    def test_invalid_exchange_id_rejected_at_construction(self) -> None:
        with pytest.raises(ExchangeURIError):
            FileExchangeCapability(
                namespace="ok",
                exchange_id="bad/id",
                transfer_methods={"http": {"tool": "t"}},
            )

    def test_list_inputs_are_normalised_to_tuples(self) -> None:
        cap = FileExchangeCapability(
            namespace="ok",
            transfer_methods={"http": {"tool": "t"}},
            produces=["image/png"],  # type: ignore[arg-type]
        )
        assert cap.produces == ("image/png",)


# ---------------------------------------------------------------------------
# register_file_exchange_capability — middleware integration with FastMCP
# ---------------------------------------------------------------------------


class TestRegisterCapability:
    def test_install_adds_middleware(self) -> None:
        mcp = FastMCP("test")
        cap = FileExchangeCapability(
            namespace="vault-mcp",
            transfer_methods={"http": {"tool": "fetch_file"}},
        )
        before = len(mcp.middleware)
        register_file_exchange_capability(mcp, cap)
        assert len(mcp.middleware) == before + 1

    def test_re_register_replaces_payload_no_double_install(self) -> None:
        mcp = FastMCP("test")
        cap1 = FileExchangeCapability(
            namespace="vault-mcp",
            transfer_methods={"http": {"tool": "fetch_file"}},
        )
        cap2 = FileExchangeCapability(
            namespace="vault-mcp",
            consumes=("image/png",),
            transfer_methods={"http": {"tool": "fetch_file"}},
        )
        register_file_exchange_capability(mcp, cap1)
        register_file_exchange_capability(mcp, cap2)
        # Only one middleware was added — the payload was updated in place.
        installed = [
            m
            for m in mcp.middleware
            if m is getattr(mcp, "_pvl_experimental_middleware", None)
        ]
        assert len(installed) == 1
        # Verify the payload reflects the second registration.
        mw = mcp._pvl_experimental_middleware  # type: ignore[attr-defined]
        assert mw._payloads["file_exchange"]["consumes"] == ["image/png"]

    def test_initialize_response_contains_capability(self) -> None:
        """End-to-end: the middleware actually mutates the initialize result."""
        mcp = FastMCP("test")
        cap = FileExchangeCapability(
            namespace="vault-mcp",
            exchange_id="hades-01",
            consumes=("image/png", "application/pdf"),
            transfer_methods={
                "exchange": {},
                "http": {"tool": "fetch_file"},
            },
        )
        register_file_exchange_capability(mcp, cap)

        # Drive the middleware directly: build a fake call_next that
        # returns a stub InitializeResult with default capabilities.
        from mcp.types import InitializeResult, ServerCapabilities

        fake_result = InitializeResult(
            protocolVersion="2025-03-26",
            capabilities=ServerCapabilities(),
            serverInfo={"name": "test", "version": "0.0.0"},  # type: ignore[arg-type]
        )

        async def fake_call_next(_ctx: object) -> InitializeResult:
            return fake_result

        mw = mcp._pvl_experimental_middleware  # type: ignore[attr-defined]
        result = asyncio.run(mw.on_initialize(context=None, call_next=fake_call_next))
        assert result is not None
        exp = result.capabilities.experimental
        assert exp is not None
        assert "file_exchange" in exp
        assert exp["file_exchange"]["namespace"] == "vault-mcp"
        assert exp["file_exchange"]["consumes"] == ["image/png", "application/pdf"]
