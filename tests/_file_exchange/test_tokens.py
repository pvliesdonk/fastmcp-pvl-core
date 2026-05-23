import pytest

from fastmcp_pvl_core._errors import ConfigurationError
from fastmcp_pvl_core._file_exchange import _tokens


def test_capability_url_joins_base_path_token():
    url = _tokens.capability_url("https://x.example.com", "/d", "abc123")
    assert url == "https://x.example.com/d/abc123"


def test_capability_url_normalizes_slashes():
    assert (
        _tokens.capability_url("https://x.example.com/", "/d/", "tok")
        == "https://x.example.com/d/tok"
    )


def test_capability_url_empty_path():
    assert _tokens.capability_url("https://x.example.com", "", "tok") == (
        "https://x.example.com/tok"
    )


def test_capability_url_requires_base_url():
    with pytest.raises(ConfigurationError):
        _tokens.capability_url("", "/d", "tok")


def test_capability_url_requires_https():
    with pytest.raises(ConfigurationError):
        _tokens.capability_url("http://x.example.com", "/d", "tok")
