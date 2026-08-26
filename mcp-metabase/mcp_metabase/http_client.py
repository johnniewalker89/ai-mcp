from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from mcp_metabase.config import MetabaseConfig
from mcp_metabase.sanitization import redact_text

_RETRYABLE_READ_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_AMBIGUOUS_WRITE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
QueryParamScalar = str | int | bool
QueryParams = Mapping[str, QueryParamScalar | list[QueryParamScalar]]


class MetabaseApiError(RuntimeError):
    """Safe Metabase API failure that never includes credentials or response bodies."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        outcome_unknown: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown


class MetabaseHttpClient:
    """Fixed-origin client for the explicitly supported Metabase REST surface."""

    def __init__(
        self,
        config: MetabaseConfig,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.config = config
        self._origin = urlsplit(config.base_url)
        self._client = httpx.Client(
            base_url=config.base_url.rstrip("/") + "/",
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
                "User-Agent": "mcp-metabase/0.1",
                "X-API-Key": config.api_key,
            },
            timeout=httpx.Timeout(config.request_timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    @staticmethod
    def _safe_path(path: str) -> str:
        if (
            not path.startswith("/api/")
            or path.startswith("//")
            or "://" in path
            or "\\" in path
            or "?" in path
            or "#" in path
            or any(ord(character) < 32 or ord(character) == 127 for character in path)
        ):
            raise MetabaseApiError("Metabase client attempted an unsafe API path.")
        return path.lstrip("/")

    def _assert_fixed_origin(self, url: httpx.URL) -> None:
        expected_port = self._origin.port or (443 if self._origin.scheme == "https" else 80)
        actual_port = url.port or (443 if url.scheme == "https" else 80)
        if (
            url.scheme.casefold() != self._origin.scheme.casefold()
            or (url.host or "").casefold() != (self._origin.hostname or "").casefold()
            or actual_port != expected_port
            or bool(url.username)
            or bool(url.password)
        ):
            raise MetabaseApiError("Metabase client refused a request outside its fixed origin.")

    @staticmethod
    def _ensure_identity_encoding(response: httpx.Response) -> None:
        encoding = response.headers.get("content-encoding", "").strip().casefold()
        if encoding not in {"", "identity"}:
            raise MetabaseApiError("Metabase returned an unsupported compressed response.")

    @staticmethod
    def _read_bounded(response: httpx.Response, *, limit: int) -> bytes:
        if response.is_stream_consumed:
            payload = response.content
            if len(payload) > limit:
                raise MetabaseApiError("Metabase JSON response exceeded its configured bound.")
            return payload
        payload = bytearray()
        try:
            for chunk in response.iter_raw(chunk_size=min(64 * 1024, limit + 1)):
                remaining = limit + 1 - len(payload)
                if remaining <= 0:
                    break
                payload.extend(chunk[:remaining])
                if len(payload) > limit or len(chunk) > remaining:
                    break
        except httpx.HTTPError:
            raise MetabaseApiError("Metabase response could not be read safely.") from None
        if len(payload) > limit:
            raise MetabaseApiError("Metabase JSON response exceeded its configured bound.")
        return bytes(payload)

    def _response_error(self, response: httpx.Response, *, mutation: bool) -> MetabaseApiError:
        request_id = response.headers.get("x-request-id", "")[:200]
        request_id = redact_text(request_id, secrets=(self.config.api_key,))
        suffix = f" (request id {request_id})" if request_id else ""
        status = response.status_code
        return MetabaseApiError(
            f"Metabase API request failed with HTTP {status}{suffix}.",
            status_code=status,
            outcome_unknown=mutation and status in _AMBIGUOUS_WRITE_STATUSES,
        )

    def _send_once(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None,
        content: bytes | None,
        mutation: bool,
    ) -> httpx.Response:
        try:
            request = self._client.build_request(
                method,
                self._safe_path(path),
                params=params,
                content=content,
            )
            self._assert_fixed_origin(request.url)
            response = self._client.send(request, stream=True)
            self._client.cookies.clear()
            return response
        except MetabaseApiError:
            raise
        except httpx.HTTPError:
            raise MetabaseApiError(
                "Metabase API request failed before a response.",
                outcome_unknown=mutation,
            ) from None

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: QueryParams | None = None,
        body: dict[str, Any] | None = None,
        mutation: bool = False,
        retry_read: bool = False,
        byte_limit: int | None = None,
    ) -> Any:
        content = None
        if body is not None:
            content = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            if len(content) > self.config.max_json_bytes:
                raise MetabaseApiError("Metabase request body exceeded its configured bound.")
        attempts = self.config.read_attempts if retry_read else 1
        response: httpx.Response | None = None
        for attempt in range(attempts):
            try:
                response = self._send_once(
                    method,
                    path,
                    params=params,
                    content=content,
                    mutation=mutation,
                )
            except MetabaseApiError:
                if retry_read and attempt + 1 < attempts:
                    time.sleep(0.05 * (attempt + 1))
                    continue
                raise
            if (
                retry_read
                and response.status_code in _RETRYABLE_READ_STATUSES
                and attempt + 1 < attempts
            ):
                response.close()
                time.sleep(0.05 * (attempt + 1))
                continue
            break
        if response is None:  # pragma: no cover - defensive loop guard.
            raise MetabaseApiError("Metabase API request produced no response.")
        try:
            if not 200 <= response.status_code < 300:
                raise self._response_error(response, mutation=mutation)
            self._ensure_identity_encoding(response)
            payload = self._read_bounded(
                response,
                limit=byte_limit or self.config.max_json_bytes,
            )
        finally:
            response.close()
        if not payload:
            return {}
        try:
            return json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise MetabaseApiError(
                "Metabase API returned invalid JSON.",
                outcome_unknown=mutation,
            ) from None

    def get_json(
        self,
        path: str,
        *,
        params: QueryParams | None = None,
    ) -> Any:
        return self._request_json("GET", path, params=params, retry_read=True)

    def query_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request_json(
            "POST",
            path,
            body=body,
            byte_limit=self.config.max_query_bytes,
        )

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request_json("POST", path, body=body, mutation=True)

    def put_json(self, path: str, body: dict[str, Any]) -> Any:
        return self._request_json("PUT", path, body=body, mutation=True)

    def close(self) -> None:
        self._client.close()
