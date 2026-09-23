from dataclasses import replace

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


def test_large_query_stack_preserves_small_allowlisted_error(configured):
    # Live v0.63.15 query rejection: ~26 KB JSON, only 62 bytes of useful error.
    body = {
        "error_type": "invalid-query",
        "error": "Unknown column 'mcp_acceptance_missing_column' in 'field list'",
        "stacktrace": ["private-stack-frame" * 100] * 20,
        "json_query": {"native": {"query": f"SELECT '{configured.api_key}'"}},
    }
    client = MetabaseHttpClient(
        configured,
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body)),
    )
    with pytest.raises(MetabaseApiError) as error:
        client.query_json("/api/dataset", {"query": "SELECT missing"})
    assert error.value.diagnostics["error_type"] == "invalid-query"
    assert error.value.diagnostics["message"] == "Unknown column <literal> in <literal>"
    assert "details_unavailable" not in error.value.diagnostics
    assert all(x not in str(error.value) for x in ["private-stack", "SELECT", configured.api_key])


def test_error_json_still_obeys_configured_response_bound(configured):
    cfg = replace(configured, max_json_bytes=64_000)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "temporary", "stacktrace": "x" * 65_000})

    client = MetabaseHttpClient(cfg, transport=httpx.MockTransport(handler))
    with pytest.raises(MetabaseApiError) as error:
        client.put_json("/api/card/1", {"name": "x"})
    assert error.value.diagnostics == {"details_unavailable": True}
    assert error.value.outcome_unknown and len(calls) == 1
