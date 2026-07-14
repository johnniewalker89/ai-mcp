from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


MCP_SERVER_NAME = "mcp-bi-metadata"
DEFAULT_ENV_FILE = Path.home() / ".codex" / "bi-metadata-mcp.env"
DEFAULT_BASE_URL = "https://bi-metadata.x340.org"
DEFAULT_API_PREFIX = "/api"
DEFAULT_TIMEOUT_SEC = 20
MAX_LIMIT = 200

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv(os.environ.get("BI_METADATA_MCP_ENV_FILE", DEFAULT_ENV_FILE))

mcp = FastMCP(name=MCP_SERVER_NAME)


@dataclass(frozen=True)
class MetadataConfig:
    base_url: str
    api_prefix: str
    token: str | None
    auth_header: str
    auth_scheme: str
    timeout_sec: int

    @property
    def has_token(self) -> bool:
        return bool(self.token)


def _config() -> MetadataConfig:
    timeout = os.environ.get("BI_METADATA_MCP_TIMEOUT_SEC", str(DEFAULT_TIMEOUT_SEC))
    try:
        timeout_sec = max(1, min(int(timeout), 120))
    except ValueError:
        timeout_sec = DEFAULT_TIMEOUT_SEC

    return MetadataConfig(
        base_url=os.environ.get("BI_METADATA_MCP_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
        api_prefix=os.environ.get("BI_METADATA_MCP_API_PREFIX", DEFAULT_API_PREFIX).strip("/"),
        token=os.environ.get("BI_METADATA_MCP_TOKEN"),
        auth_header=os.environ.get("BI_METADATA_MCP_AUTH_HEADER", "Authorization"),
        auth_scheme=os.environ.get("BI_METADATA_MCP_AUTH_SCHEME", "Bearer"),
        timeout_sec=timeout_sec,
    )


def _bounded_limit(limit: int) -> int:
    return max(1, min(limit, MAX_LIMIT))


def _query_string(params: dict[str, Any]) -> str:
    clean = {
        key: value
        for key, value in params.items()
        if value is not None and value != "" and value != []
    }
    return urlencode(clean, doseq=True)


def _api_url(path: str, params: dict[str, Any] | None = None) -> str:
    config = _config()
    normalized_path = path if path.startswith("/") else f"/{path}"
    if not normalized_path.startswith("/v1/"):
        raise ToolError("Only OpenMetadata /v1 read-only API paths are allowed.")
    prefix = f"/{config.api_prefix}" if config.api_prefix else ""
    url = f"{config.base_url}{prefix}{normalized_path}"
    if params:
        query = _query_string(params)
        if query:
            url = f"{url}?{query}"
    return url


def _headers() -> dict[str, str]:
    config = _config()
    headers = {
        "Accept": "application/json",
        "User-Agent": MCP_SERVER_NAME,
    }
    if config.token:
        value = config.token
        if config.auth_scheme:
            value = f"{config.auth_scheme} {value}"
        headers[config.auth_header] = value
    return headers


def _api_get(path: str, params: dict[str, Any] | None = None) -> Any:
    config = _config()
    url = _api_url(path, params=params)
    request = Request(url, headers=_headers(), method="GET")

    try:
        with urlopen(request, timeout=config.timeout_sec) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise ToolError(
                "BI metadata API returned HTTP 401. Set BI_METADATA_MCP_TOKEN "
                "in the local env file."
            ) from exc
        raise ToolError(f"BI metadata API GET {url} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ToolError(f"BI metadata API GET {url} failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(f"BI metadata API GET {url} returned invalid JSON: {exc}") from exc


def _entity_ref(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "id": value.get("id"),
        "type": value.get("type"),
        "name": value.get("name"),
        "fullyQualifiedName": value.get("fullyQualifiedName"),
        "displayName": value.get("displayName"),
    }


def _tag_labels(labels: Any) -> list[dict[str, Any]]:
    if not isinstance(labels, list):
        return []
    return [
        {
            "tagFQN": label.get("tagFQN"),
            "labelType": label.get("labelType"),
            "source": label.get("source"),
            "state": label.get("state"),
        }
        for label in labels
        if isinstance(label, dict)
    ]


def _compact_columns(columns: Any) -> list[dict[str, Any]]:
    if not isinstance(columns, list):
        return []
    compact: list[dict[str, Any]] = []
    for column in columns:
        if not isinstance(column, dict):
            continue
        compact.append(
            {
                "name": column.get("name"),
                "displayName": column.get("displayName"),
                "dataType": column.get("dataType"),
                "arrayDataType": column.get("arrayDataType"),
                "description": column.get("description"),
                "tags": _tag_labels(column.get("tags")),
                "children": _compact_columns(column.get("children")),
            }
        )
    return compact


def _compact_table(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "fullyQualifiedName": row.get("fullyQualifiedName"),
        "displayName": row.get("displayName"),
        "description": row.get("description"),
        "service": _entity_ref(row.get("service")),
        "database": _entity_ref(row.get("database")),
        "databaseSchema": _entity_ref(row.get("databaseSchema")),
        "tableType": row.get("tableType"),
        "owners": [_entity_ref(owner) for owner in row.get("owners", [])],
        "domains": [_entity_ref(domain) for domain in row.get("domains", [])],
        "tags": _tag_labels(row.get("tags")),
        "columns": _compact_columns(row.get("columns")),
        "usageSummary": row.get("usageSummary"),
        "updatedAt": row.get("updatedAt"),
        "updatedBy": row.get("updatedBy"),
    }


def _compact_entity_rows(data: dict[str, Any], key: str) -> list[dict[str, Any]]:
    rows = data.get("data", [])
    if not isinstance(rows, list):
        return []
    if key == "tables":
        return [_compact_table(row) for row in rows if isinstance(row, dict)]
    return [
        {
            "id": row.get("id"),
            "name": row.get("name"),
            "fullyQualifiedName": row.get("fullyQualifiedName"),
            "displayName": row.get("displayName"),
            "description": row.get("description"),
            "service": _entity_ref(row.get("service")),
            "database": _entity_ref(row.get("database")),
            "databaseSchema": _entity_ref(row.get("databaseSchema")),
            "owners": [_entity_ref(owner) for owner in row.get("owners", [])],
            "tags": _tag_labels(row.get("tags")),
        }
        for row in rows
        if isinstance(row, dict)
    ]


@mcp.tool()
def bi_metadata_config() -> dict[str, object]:
    """List BI metadata MCP configuration without exposing token contents."""

    config = _config()
    return {
        "env_file": str(os.environ.get("BI_METADATA_MCP_ENV_FILE", DEFAULT_ENV_FILE)),
        "base_url": config.base_url,
        "api_prefix": f"/{config.api_prefix}" if config.api_prefix else "",
        "has_token": config.has_token,
        "auth_header": config.auth_header,
        "auth_scheme": config.auth_scheme,
        "timeout_sec": config.timeout_sec,
    }


@mcp.tool()
def bi_metadata_version() -> dict[str, object]:
    """Read OpenMetadata server version."""

    return _api_get("/v1/system/version")


@mcp.tool()
def bi_metadata_search(
    query: str,
    index: str = "table_search_index",
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    """Search OpenMetadata entities. Defaults to table search index."""

    if not query:
        raise ToolError("query is required")
    data = _api_get(
        "/v1/search/query",
        {
            "q": query,
            "index": index,
            "size": _bounded_limit(limit),
            "from": max(0, offset),
        },
    )
    hits = data.get("hits", {}).get("hits", []) if isinstance(data, dict) else []
    return {
        "query": query,
        "index": index,
        "returned": len(hits),
        "hits": [
            {
                "id": hit.get("_id"),
                "score": hit.get("_score"),
                "source": {
                    "name": hit.get("_source", {}).get("name"),
                    "fullyQualifiedName": hit.get("_source", {}).get("fullyQualifiedName"),
                    "displayName": hit.get("_source", {}).get("displayName"),
                    "description": hit.get("_source", {}).get("description"),
                    "entityType": hit.get("_source", {}).get("entityType"),
                    "service": hit.get("_source", {}).get("service"),
                    "database": hit.get("_source", {}).get("database"),
                    "databaseSchema": hit.get("_source", {}).get("databaseSchema"),
                    "tags": hit.get("_source", {}).get("tags"),
                    "owners": hit.get("_source", {}).get("owners"),
                },
            }
            for hit in hits
            if isinstance(hit, dict)
        ],
        "raw_total": data.get("hits", {}).get("total") if isinstance(data, dict) else None,
    }


@mcp.tool()
def bi_metadata_list_tables(
    service: str = "",
    database: str = "",
    database_schema: str = "",
    name_contains: str = "",
    fields: str = "columns,tags,owners,domains,usageSummary",
    limit: int = 50,
    after: str = "",
) -> dict[str, object]:
    """List OpenMetadata tables with bounded metadata fields."""

    data = _api_get(
        "/v1/tables",
        {
            "service": service,
            "database": database,
            "databaseSchema": database_schema,
            "fields": fields,
            "limit": _bounded_limit(limit),
            "after": after,
        },
    )
    tables = _compact_entity_rows(data, "tables") if isinstance(data, dict) else []
    if name_contains:
        needle = name_contains.lower()
        tables = [
            table
            for table in tables
            if needle in str(table.get("name", "")).lower()
            or needle in str(table.get("fullyQualifiedName", "")).lower()
        ]
    return {
        "returned": len(tables),
        "paging": data.get("paging", {}) if isinstance(data, dict) else {},
        "tables": tables,
    }


@mcp.tool()
def bi_metadata_get_table_by_fqn(
    fqn: str,
    fields: str = "columns,tags,owners,domains,usageSummary",
) -> dict[str, object]:
    """Get one OpenMetadata table by fully qualified name."""

    if not fqn:
        raise ToolError("fqn is required")
    data = _api_get(f"/v1/tables/name/{quote(fqn, safe='')}", {"fields": fields})
    return {"table": _compact_table(data)} if isinstance(data, dict) else {"table": data}


@mcp.tool()
def bi_metadata_get_table_by_id(
    table_id: str,
    fields: str = "columns,tags,owners,domains,usageSummary",
) -> dict[str, object]:
    """Get one OpenMetadata table by id."""

    if not table_id:
        raise ToolError("table_id is required")
    data = _api_get(f"/v1/tables/{quote(table_id, safe='')}", {"fields": fields})
    return {"table": _compact_table(data)} if isinstance(data, dict) else {"table": data}


@mcp.tool()
def bi_metadata_table_lineage_by_fqn(
    fqn: str,
    upstream_depth: int = 1,
    downstream_depth: int = 1,
) -> dict[str, object]:
    """Get lineage for one table FQN without reading sample data."""

    if not fqn:
        raise ToolError("fqn is required")
    return _api_get(
        f"/v1/lineage/table/name/{quote(fqn, safe='')}",
        {
            "upstreamDepth": max(0, min(upstream_depth, 5)),
            "downstreamDepth": max(0, min(downstream_depth, 5)),
        },
    )


@mcp.tool()
def bi_metadata_list_database_services(limit: int = 50, after: str = "") -> dict[str, object]:
    """List OpenMetadata database services."""

    data = _api_get("/v1/services/databaseServices", {"limit": _bounded_limit(limit), "after": after})
    return {
        "returned": len(data.get("data", [])) if isinstance(data, dict) else 0,
        "paging": data.get("paging", {}) if isinstance(data, dict) else {},
        "services": _compact_entity_rows(data, "services") if isinstance(data, dict) else [],
    }


@mcp.tool()
def bi_metadata_list_databases(
    service: str = "",
    fields: str = "owners,tags",
    limit: int = 50,
    after: str = "",
) -> dict[str, object]:
    """List OpenMetadata databases."""

    data = _api_get(
        "/v1/databases",
        {"service": service, "fields": fields, "limit": _bounded_limit(limit), "after": after},
    )
    return {
        "returned": len(data.get("data", [])) if isinstance(data, dict) else 0,
        "paging": data.get("paging", {}) if isinstance(data, dict) else {},
        "databases": _compact_entity_rows(data, "databases") if isinstance(data, dict) else [],
    }


@mcp.tool()
def bi_metadata_list_database_schemas(
    database: str = "",
    fields: str = "owners,tags",
    limit: int = 50,
    after: str = "",
) -> dict[str, object]:
    """List OpenMetadata database schemas."""

    data = _api_get(
        "/v1/databaseSchemas",
        {"database": database, "fields": fields, "limit": _bounded_limit(limit), "after": after},
    )
    return {
        "returned": len(data.get("data", [])) if isinstance(data, dict) else 0,
        "paging": data.get("paging", {}) if isinstance(data, dict) else {},
        "schemas": _compact_entity_rows(data, "schemas") if isinstance(data, dict) else [],
    }
