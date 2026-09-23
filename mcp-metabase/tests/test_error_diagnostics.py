import httpx
import pytest

from mcp_metabase.http_client import MetabaseApiError, MetabaseHttpClient


def test_query_error_retains_code_and_symbol_without_sql_or_credentials(configured):
    def handler(request):
        return httpx.Response(
            400,
            json={
                "error_type": "invalid-query",
                "error": "Code: 47. DB::Exception: Unknown identifier 'private'. "
                f"In scope SELECT '{configured.api_key}'. (UNKNOWN_IDENTIFIER)",
                "sql": "SELECT secret",
                "data": {"password": "secret"},
            },
            request=request,
        )

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(handler))
    with pytest.raises(MetabaseApiError) as error:
        client.query_json("/api/dataset", {"query": "SELECT 1"})
    assert error.value.diagnostics["code"] == 47
    assert error.value.diagnostics["symbol"] == "UNKNOWN_IDENTIFIER"
    assert "Unknown identifier" in str(error.value)
    assert all(
        value not in str(error.value)
        for value in ["SELECT", "private", "secret", configured.api_key]
    )
    assert not error.value.outcome_unknown


def test_oversized_error_body_keeps_write_uncertainty_and_no_retry(configured):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, content=b"x" * 20000, request=request)

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(handler))
    with pytest.raises(MetabaseApiError) as error:
        client.put_json("/api/card/1", {"name": "x"})
    assert error.value.outcome_unknown and len(calls) == 1
    assert error.value.diagnostics == {"details_unavailable": True}
