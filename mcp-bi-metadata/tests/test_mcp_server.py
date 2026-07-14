from __future__ import annotations

import importlib


def test_api_url_defaults(monkeypatch):
    monkeypatch.delenv("BI_METADATA_MCP_BASE_URL", raising=False)
    monkeypatch.delenv("BI_METADATA_MCP_API_PREFIX", raising=False)

    server = importlib.import_module("mcp_bi_metadata.mcp_server")

    assert (
        server._api_url("/v1/tables", {"limit": 10})
        == "https://bi-metadata.x340.org/api/v1/tables?limit=10"
    )


def test_headers_do_not_emit_auth_without_token(monkeypatch):
    monkeypatch.delenv("BI_METADATA_MCP_TOKEN", raising=False)

    server = importlib.import_module("mcp_bi_metadata.mcp_server")

    assert "Authorization" not in server._headers()


def test_headers_emit_bearer_token(monkeypatch):
    monkeypatch.setenv("BI_METADATA_MCP_TOKEN", "secret-token")

    server = importlib.import_module("mcp_bi_metadata.mcp_server")

    assert server._headers()["Authorization"] == "Bearer secret-token"


def test_compact_table_keeps_metadata_not_sample_data():
    server = importlib.import_module("mcp_bi_metadata.mcp_server")

    compact = server._compact_table(
        {
            "id": "1",
            "name": "events",
            "fullyQualifiedName": "service.db.schema.events",
            "sampleData": {"columns": ["secret"]},
            "columns": [
                {
                    "name": "event_date",
                    "dataType": "DATE",
                    "tags": [{"tagFQN": "PII.None", "source": "Classification"}],
                }
            ],
        }
    )

    assert compact["fullyQualifiedName"] == "service.db.schema.events"
    assert compact["columns"][0]["name"] == "event_date"
    assert "sampleData" not in compact
