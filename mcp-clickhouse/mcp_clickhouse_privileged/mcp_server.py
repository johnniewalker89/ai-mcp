from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
import uuid
from typing import Any, Dict, List, Optional

import clickhouse_connect
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.tools import Tool
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_clickhouse_privileged.mcp_env import TransportType, get_config, get_mcp_config


MCP_SERVER_NAME = "mcp-clickhouse-privileged"
ALLOWED_SELECT_FIRST_KEYWORDS = {"SELECT", "WITH", "EXPLAIN", "SHOW", "DESCRIBE", "DESC"}
DROP_FIRST_KEYWORDS = {"DROP", "TRUNCATE"}
QUERY_ID_RE = re.compile(r"^[A-Za-z0-9_.:\-]{1,160}$")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv()

mcp_config = get_mcp_config()
auth_provider = None
if mcp_config.server_transport in {TransportType.HTTP.value, TransportType.SSE.value}:
    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
    elif mcp_config.auth_token:
        auth_provider = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
    else:
        raise ValueError(
            "Authentication token required for HTTP/SSE transports. "
            "Set CLICKHOUSE_MCP_AUTH_TOKEN or CLICKHOUSE_MCP_AUTH_DISABLED=true."
        )

mcp = FastMCP(name=MCP_SERVER_NAME, auth=auth_provider)
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="clickhouse-mcp")


@dataclass
class AsyncJob:
    query_id: str
    submitted_at: str
    database: str
    query_preview: str
    future: Future = field(repr=False)


async_jobs: Dict[str, AsyncJob] = {}


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    if auth_provider is not None:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return PlainTextResponse("Unauthorized", status_code=401)

        token = auth_header[7:]
        access_token = await auth_provider.verify_token(token)
        if access_token is None:
            return PlainTextResponse("Unauthorized", status_code=401)

    try:
        client = create_client()
        version = client.query("SELECT version()").first_item
        client.close()
        return PlainTextResponse(f"OK - Connected to ClickHouse: {version}")
    except Exception as e:
        return PlainTextResponse(f"ERROR - Cannot connect to ClickHouse: {str(e)}", status_code=503)


def create_client(database: Optional[str] = None):
    cfg = get_config()
    return clickhouse_connect.get_client(
        host=cfg.host,
        port=cfg.port,
        username=cfg.user,
        password=cfg.password,
        database=database or cfg.database,
        secure=cfg.secure,
        verify=cfg.verify,
        connect_timeout=cfg.connect_timeout,
        send_receive_timeout=cfg.send_receive_timeout,
    )


def _strip_sql_comments(sql: str) -> str:
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _extract_first_keyword(sql: str) -> str:
    cleaned = _strip_sql_comments(sql).strip()
    match = re.match(r"^([A-Za-z]+)", cleaned)
    return match.group(1).upper() if match else ""


def _validate_single_statement(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Query must be a non-empty string.")

    sql = query.strip()
    sql_no_trailing_semicolon = sql[:-1].rstrip() if sql.endswith(";") else sql
    if ";" in sql_no_trailing_semicolon:
        raise ToolError("Only a single SQL statement is allowed (remove extra semicolons).")


def _validate_select_query(query: str) -> None:
    _validate_single_statement(query)
    first = _extract_first_keyword(query)
    if first not in ALLOWED_SELECT_FIRST_KEYWORDS:
        raise ToolError("Only SELECT-like queries are allowed via run_select_query.")


def _validate_query(query: str) -> None:
    cfg = get_config()
    _validate_single_statement(query)

    first = _extract_first_keyword(query)
    if first not in ALLOWED_SELECT_FIRST_KEYWORDS and not cfg.allow_write_access:
        raise ToolError(
            "Write operations are disabled. "
            "Set CLICKHOUSE_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations."
        )

    if first in DROP_FIRST_KEYWORDS and not cfg.allow_drop:
        raise ToolError(
            "DROP/TRUNCATE operations are disabled. "
            "Set CLICKHOUSE_ALLOW_DROP=true to allow destructive operations."
        )


def _validate_query_id(query_id: str) -> None:
    if not QUERY_ID_RE.match(query_id):
        raise ToolError("query_id contains unsupported characters.")


def _settings(settings: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if settings is None:
        return {}
    if not isinstance(settings, dict):
        raise ToolError("settings must be an object.")
    return settings


def _query_result_to_dict(result: Any, max_rows: int) -> Dict[str, Any]:
    rows = [list(row) for row in result.result_rows[:max_rows]]
    return {
        "columns": list(result.column_names),
        "rows": rows,
        "truncated": len(result.result_rows) > max_rows,
        "summary": dict(getattr(result, "summary", {}) or {}),
    }


def _execute_query(
    database: Optional[str],
    query: str,
    max_rows: int,
    query_id: Optional[str],
    settings: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    _validate_query(query)
    first = _extract_first_keyword(query)
    request_settings = _settings(settings).copy()
    if query_id:
        request_settings["query_id"] = query_id
    client = create_client(database)
    try:
        if first in ALLOWED_SELECT_FIRST_KEYWORDS:
            result = client.query(
                query,
                settings=request_settings,
            )
            return _query_result_to_dict(result, max_rows)

        command_result = client.command(
            query,
            settings=request_settings,
        )
        return {
            "columns": [],
            "rows": [],
            "truncated": False,
            "result": str(command_result),
            "query_id": query_id,
        }
    finally:
        client.close()


def list_databases() -> Dict[str, Any]:
    client = create_client()
    try:
        result = client.query("SHOW DATABASES")
        return {"databases": [row[0] for row in result.result_rows]}
    finally:
        client.close()


def list_tables(
    database: Optional[str] = None,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_size: int = 50,
    page_token: Optional[str] = None,
    include_detailed_columns: bool = True,
) -> Dict[str, Any]:
    if page_token is not None:
        raise ToolError("page_token is not supported by this lightweight server yet.")
    if page_size <= 0:
        raise ToolError("page_size must be a positive integer.")

    where = ["database = %(database)s"]
    params: Dict[str, Any] = {"database": database or get_config().database, "limit": page_size}
    if like:
        where.append("name LIKE %(like)s")
        params["like"] = like
    if not_like:
        where.append("name NOT LIKE %(not_like)s")
        params["not_like"] = not_like

    client = create_client()
    try:
        tables_result = client.query(
            f"""
            SELECT
                  database
                , name
                , engine
                , total_rows
                , total_bytes
                , comment
            FROM system.tables
            WHERE {' AND '.join(where)}
            ORDER BY name
            LIMIT %(limit)s
            """,
            parameters=params,
        )

        tables: List[Dict[str, Any]] = []
        for db, name, engine, total_rows, total_bytes, comment in tables_result.result_rows:
            columns: List[Dict[str, Any]] = []
            if include_detailed_columns:
                columns_result = client.query(
                    """
                    SELECT name, type, default_expression, comment
                    FROM system.columns
                    WHERE database = %(database)s AND table = %(table)s
                    ORDER BY position
                    """,
                    parameters={"database": db, "table": name},
                )
                columns = [
                    {
                        "name": col_name,
                        "type": col_type,
                        "default_expression": default_expression,
                        "comment": col_comment,
                    }
                    for col_name, col_type, default_expression, col_comment
                    in columns_result.result_rows
                ]

            tables.append(
                {
                    "database": db,
                    "name": name,
                    "engine": engine,
                    "row_count": total_rows,
                    "size_bytes": total_bytes,
                    "comment": comment,
                    "columns": columns,
                }
            )

        return {"tables": tables, "next_page_token": None, "total_tables": len(tables)}
    finally:
        client.close()


def run_select_query(
    query: str,
    database: Optional[str] = None,
    max_rows: int = 1000,
    settings: Optional[Dict[str, Any]] = None,
    query_id: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_select_query(query)
    return _execute_query(database, query, max_rows, query_id, settings)


def run_query(
    query: str,
    database: Optional[str] = None,
    max_rows: int = 1000,
    settings: Optional[Dict[str, Any]] = None,
    query_id: Optional[str] = None,
) -> Dict[str, Any]:
    return _execute_query(database, query, max_rows, query_id, settings)


def start_async_query(
    query: str,
    database: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    query_id: Optional[str] = None,
) -> Dict[str, Any]:
    _validate_query(query)
    final_query_id = query_id or f"ai_mcp_{uuid.uuid4()}"
    _validate_query_id(final_query_id)

    future = executor.submit(_execute_query, database, query, 1000, final_query_id, settings)
    async_jobs[final_query_id] = AsyncJob(
        query_id=final_query_id,
        submitted_at=datetime.now(timezone.utc).isoformat(),
        database=database or get_config().database,
        query_preview=query[:1000],
        future=future,
    )
    return {
        "query_id": final_query_id,
        "status": "running",
        "database": database or get_config().database,
    }


def get_query_status(query_id: str) -> Dict[str, Any]:
    _validate_query_id(query_id)
    job = async_jobs.get(query_id)
    if job and job.future.done():
        try:
            return {
                "query_id": query_id,
                "status": "completed",
                "submitted_at": job.submitted_at,
                "database": job.database,
                "result": job.future.result(),
            }
        except Exception as e:
            return {
                "query_id": query_id,
                "status": "failed",
                "submitted_at": job.submitted_at,
                "database": job.database,
                "error": str(e),
            }

    client = create_client()
    try:
        processes = client.query(
            """
            SELECT query_id, elapsed, read_rows, read_bytes, memory_usage
            FROM system.processes
            WHERE query_id = %(query_id)s
            """,
            parameters={"query_id": query_id},
        )
        if processes.result_rows:
            return {
                "query_id": query_id,
                "status": "running",
                "processes": [
                    {
                        "query_id": row[0],
                        "elapsed": row[1],
                        "read_rows": row[2],
                        "read_bytes": row[3],
                        "memory_usage": row[4],
                    }
                    for row in processes.result_rows
                ],
            }

        history = client.query(
            """
            SELECT
                  type
                , event_time
                , query_duration_ms
                , read_rows
                , read_bytes
                , written_rows
                , written_bytes
                , exception_code
                , exception
            FROM system.query_log
            WHERE query_id = %(query_id)s
            ORDER BY event_time DESC
            LIMIT 5
            """,
            parameters={"query_id": query_id},
        )
        return {
            "query_id": query_id,
            "status": "not_running",
            "history": [list(row) for row in history.result_rows],
        }
    finally:
        client.close()


def kill_query(query_id: str, sync: bool = True) -> Dict[str, Any]:
    cfg = get_config()
    if not cfg.allow_write_access:
        raise ToolError("KILL QUERY requires CLICKHOUSE_ALLOW_WRITE_ACCESS=true.")
    _validate_query_id(query_id)

    mode = "SYNC" if sync else "ASYNC"
    client = create_client()
    try:
        result = client.command(f"KILL QUERY WHERE query_id = %(query_id)s {mode}", parameters={"query_id": query_id})
        return {"query_id": query_id, "status": "kill_sent", "result": result}
    finally:
        client.close()


if get_config().enabled:
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(run_select_query))
    mcp.add_tool(Tool.from_function(run_query))
    mcp.add_tool(Tool.from_function(start_async_query))
    mcp.add_tool(Tool.from_function(get_query_status))
    mcp.add_tool(Tool.from_function(kill_query))
    logger.info("ClickHouse tools registered")
