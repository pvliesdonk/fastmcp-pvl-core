"""Tests for CLI helpers."""

from __future__ import annotations

from fastmcp_pvl_core import make_serve_parser, normalise_http_path


class TestNormaliseHttpPath:
    def test_none_returns_default(self):
        assert normalise_http_path(None) == "/mcp"

    def test_empty_returns_default(self):
        assert normalise_http_path("") == "/mcp"

    def test_whitespace_returns_default(self):
        assert normalise_http_path("   ") == "/mcp"

    def test_adds_leading_slash(self):
        assert normalise_http_path("mcp") == "/mcp"

    def test_strips_trailing_slash(self):
        assert normalise_http_path("/mcp/") == "/mcp"

    def test_preserves_multi_segment(self):
        assert normalise_http_path("/api/mcp") == "/api/mcp"

    def test_root_path_preserved(self):
        assert normalise_http_path("/") == "/"

    def test_custom_default(self):
        assert normalise_http_path(None, default="/api") == "/api"

    def test_strips_surrounding_whitespace(self):
        assert normalise_http_path("  /mcp  ") == "/mcp"


class TestMakeServeParser:
    def test_parses_verbose_flag(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["-v"])
        assert args.verbose is True

    def test_default_transport_is_stdio(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args([])
        assert args.transport == "stdio"

    def test_parses_http_transport_and_port(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["--transport", "http", "--port", "9000"])
        assert args.transport == "http"
        assert args.port == 9000

    def test_default_host(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args([])
        assert args.host == "127.0.0.1"

    def test_custom_host(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["--host", "0.0.0.0"])
        assert args.host == "0.0.0.0"

    def test_default_http_path_is_none(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args([])
        assert args.http_path is None

    def test_custom_http_path(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["--http-path", "/api/mcp"])
        assert args.http_path == "/api/mcp"

    def test_sse_transport(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["--transport", "sse"])
        assert args.transport == "sse"

    def test_verbose_long_form(self):
        parser = make_serve_parser(prog="myapp")
        args = parser.parse_args(["--verbose"])
        assert args.verbose is True

    def test_prog_name_preserved(self):
        parser = make_serve_parser(prog="myapp", description="my description")
        assert parser.prog == "myapp"
        assert parser.description == "my description"
