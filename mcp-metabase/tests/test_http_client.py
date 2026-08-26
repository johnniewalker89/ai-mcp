from __future__ import annotations

import json

import httpx
import pytest

from mcp_metabase.http_client import MetabaseApiError, MetabaseHttpClient


def _response(request: httpx.Request, payload: dict, *, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode(), request=request)


def test_reads_retry_but_mutations_never_retry(configured) -> None:
    read_calls = 0

    def read_handler(request: httpx.Request) -> httpx.Response:
        nonlocal read_calls
        read_calls += 1
        if read_calls == 1:
            return _response(request, {}, status=503)
        assert request.headers["x-api-key"] == configured.api_key
        return _response(request, {"ok": True})

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(read_handler))
    assert client.get_json("/api/test") == {"ok": True}
    assert read_calls == 2

    write_calls = 0

    def write_handler(request: httpx.Request) -> httpx.Response:
        nonlocal write_calls
        write_calls += 1
        return _response(request, {}, status=503)

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(write_handler))
    with pytest.raises(MetabaseApiError) as raised:
        client.put_json("/api/card/1", {"name": "Changed"})
    assert raised.value.outcome_unknown is True
    assert write_calls == 1


def test_errors_hide_response_body_and_api_key(configured) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _response(
            request,
            {"secret": configured.api_key, "sql": "select private_data"},
            status=400,
        )

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(handler))
    with pytest.raises(MetabaseApiError) as raised:
        client.post_json("/api/card", {"name": "x"})
    message = str(raised.value)
    assert configured.api_key not in message
    assert "private_data" not in message
    assert raised.value.outcome_unknown is False


def test_unsafe_path_is_blocked_before_network(configured) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _response(request, {})

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(handler))
    with pytest.raises(MetabaseApiError, match="unsafe API path"):
        client.get_json("https://evil.example/api/card/1")
    assert calls == 0


def test_list_query_parameters_are_encoded_as_repeated_keys(configured) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params.get_list("models") == ["card", "dashboard"]
        assert "card%2Cdashboard" not in str(request.url)
        return _response(request, {"data": [], "total": 0})

    client = MetabaseHttpClient(configured, transport=httpx.MockTransport(handler))

    result = client.get_json(
        "/api/search",
        params={"models": ["card", "dashboard"], "limit": 10},
    )

    assert result == {"data": [], "total": 0}
