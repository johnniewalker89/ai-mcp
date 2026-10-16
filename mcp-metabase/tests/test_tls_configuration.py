from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from mcp_metabase.config import ConfigurationError, MetabaseConfig
from mcp_metabase.http_client import MetabaseHttpClient


@pytest.fixture
def tls_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("METABASE_BASE_URL", "https://metabase.example.org")
    monkeypatch.setenv("METABASE_API_KEY", "mb_test_01234567890123456789")
    monkeypatch.setenv("METABASE_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.delenv("METABASE_MCP_VERIFY_TLS", raising=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, True), ("true", True), ("false", False), (" FALSE ", False)],
)
def test_tls_setting_reaches_http_transport(tls_environment, monkeypatch, raw, expected):
    if raw is not None:
        monkeypatch.setenv("METABASE_MCP_VERIFY_TLS", raw)
    config = MetabaseConfig.from_env()
    assert config.verify_tls is expected

    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"ok": True}, request=request)
    )
    constructor = httpx.Client
    with patch("mcp_metabase.http_client.httpx.Client", wraps=constructor) as factory:
        client = MetabaseHttpClient(config, transport=transport)
    try:
        assert factory.call_args.kwargs["verify"] is expected
        assert client.get_json("/api/test") == {"ok": True}
    finally:
        client.close()


@pytest.mark.parametrize("raw", ["", "0", "yes"])
def test_invalid_tls_setting_fails_closed(tls_environment, monkeypatch, raw):
    monkeypatch.setenv("METABASE_MCP_VERIFY_TLS", raw)
    with pytest.raises(ConfigurationError, match="METABASE_MCP_VERIFY_TLS must be"):
        MetabaseConfig.from_env()
