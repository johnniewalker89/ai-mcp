from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import logging
import re
import os
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from fastmcp.server.dependencies import get_context
from fastmcp.tools import Tool
from cachetools import TTLCache
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_greenplum.mcp_env import MCPServerConfig, TransportType, get_config, get_mcp_config


MCP_SERVER_NAME = "mcp-greenplum"
CLIENT_CONFIG_OVERRIDES_KEY = "greenplum_client_config_overrides"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv()

mcp_config: MCPServerConfig = get_mcp_config()
http_transports = [TransportType.HTTP.value, TransportType.SSE.value]

auth_provider = None
if mcp_config.server_transport in http_transports:
    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
        logger.warning("Only use this for local development/testing.")
        logger.warning("DO NOT expose to networks.")
    elif mcp_config.auth_token:
        auth_provider = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
        logger.info("Authentication enabled for HTTP/SSE transport")
    else:
        raise ValueError(
            "Authentication token required for HTTP/SSE transports. "
            "Set GREENPLUM_MCP_AUTH_TOKEN environment variable or set "
            "GREENPLUM_MCP_AUTH_DISABLED=true (for development only)."
        )

mcp = FastMCP(name=MCP_SERVER_NAME, auth=auth_provider)
executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="greenplum-mcp")


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
        cfg = get_config()
        conn = create_connection(cfg.database)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                version = cur.fetchone()[0]
        finally:
            conn.close()
        return PlainTextResponse(f"OK - Connected to Greenplum: {version}")
    except Exception as e:
        return PlainTextResponse(f"ERROR - Cannot connect to Greenplum: {str(e)}", status_code=503)


def _to_jsonable_rows(rows: List[Tuple[Any, ...]]) -> List[List[Any]]:
    return [list(row) for row in rows]


def _strip_sql_comments(sql: str) -> str:
    # Naive comment stripping good enough for lightweight "must be SELECT" checks.
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _extract_first_keyword(sql: str) -> str:
    cleaned = _strip_sql_comments(sql).strip()
    match = re.match(r"^([A-Za-z]+)", cleaned)
    if not match:
        return ""
    return match.group(1).upper()


ALLOWED_SELECT_FIRST_KEYWORDS = {"SELECT", "WITH", "EXPLAIN"}
DROP_FIRST_KEYWORDS = {"DROP", "TRUNCATE"}


def _validate_select_query(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Query must be a non-empty string.")

    sql = query.strip()
    sql_no_trailing_semicolon = sql[:-1].rstrip() if sql.endswith(";") else sql

    # Only allow a single statement. This blocks most accidental multi-statement payloads.
    if ";" in sql_no_trailing_semicolon:
        raise ToolError("Only a single SQL statement is allowed (remove extra semicolons).")

    first = _extract_first_keyword(query)
    if first not in ALLOWED_SELECT_FIRST_KEYWORDS:
        raise ToolError(
            "Only read-only SELECT-like queries are allowed via run_select_query "
            "(allowed: SELECT, WITH, EXPLAIN)."
        )

    # Extra safety: block obvious destructive ops even if first keyword is SELECT.
    # We intentionally do NOT scan for destructive keywords inside the statement,
    # because they may appear in string literals and trigger false positives.


def _validate_single_statement(query: str) -> None:
    if not isinstance(query, str) or not query.strip():
        raise ToolError("Query must be a non-empty string.")

    sql = query.strip()
    sql_no_trailing_semicolon = sql[:-1].rstrip() if sql.endswith(";") else sql
    if ";" in sql_no_trailing_semicolon:
        raise ToolError("Only a single SQL statement is allowed (remove extra semicolons).")


def _validate_write_query(query: str) -> None:
    cfg = get_config()
    _validate_single_statement(query)

    if not cfg.allow_write_access:
        raise ToolError(
            "Write operations are disabled. "
            "Set GREENPLUM_ALLOW_WRITE_ACCESS=true to allow DDL and DML operations."
        )

    first = _extract_first_keyword(query)
    if first in DROP_FIRST_KEYWORDS and not cfg.allow_drop:
        raise ToolError(
            "DROP/TRUNCATE operations are disabled. "
            "Set GREENPLUM_ALLOW_DROP=true to allow destructive operations."
        )


def _apply_readonly_transaction(cur, query_timeout_seconds: int) -> None:
    # Ensure we end the transaction to avoid long-lived read-only sessions.
    cur.execute("BEGIN;")
    cur.execute("SET TRANSACTION READ ONLY;")
    # Greenplum/PostgreSQL uses milliseconds for statement_timeout.
    cur.execute(f"SET LOCAL statement_timeout = {int(query_timeout_seconds * 1000)};")


def _apply_write_transaction(cur, query_timeout_seconds: int) -> None:
    cur.execute("BEGIN;")
    cur.execute(f"SET LOCAL statement_timeout = {int(query_timeout_seconds * 1000)};")


def create_connection(database: str):
    cfg = get_config()
    dsn: Dict[str, Any] = {
        "host": cfg.host,
        "port": cfg.port,
        "user": cfg.user,
        "password": cfg.password,
        "dbname": database,
        "connect_timeout": cfg.connect_timeout,
        "sslmode": cfg.sslmode,
        "application_name": MCP_SERVER_NAME,
    }

    # Allow per-request overrides via MCP context state (similar to mcp-clickhouse).
    try:
        ctx = get_context()
        overrides = ctx.get_state(CLIENT_CONFIG_OVERRIDES_KEY)
        if overrides:
            if not isinstance(overrides, dict):
                logger.warning(
                    "%s must be a dict, got %s. Ignoring.",
                    CLIENT_CONFIG_OVERRIDES_KEY,
                    type(overrides).__name__,
                )
            else:
                dsn.update(overrides)
    except RuntimeError:
        # Outside request context.
        pass

    return psycopg2.connect(**dsn)


@dataclass
class TableInfo:
    database: str
    schema: str
    name: str
    distribution_type: Optional[str]
    distribution_keys: List[str]
    storage_type: str


table_pagination_cache: TTLCache = TTLCache(maxsize=100, ttl=3600)


@dataclass
class AsyncJob:
    job_id: str
    submitted_at: str
    database: str
    query_preview: str
    future: Optional[Future] = field(default=None, repr=False)
    connection: Any = field(default=None, repr=False)
    backend_pid: Optional[int] = None


async_jobs: Dict[str, AsyncJob] = {}


def _execute_query_async(
    job: AsyncJob,
    database: str,
    query: str,
    max_rows: int,
) -> Dict[str, Any]:
    mcp_cfg = get_mcp_config()

    conn = create_connection(database)
    job.connection = conn
    try:
        job.backend_pid = conn.get_backend_pid()
        with conn.cursor() as cur:
            _apply_write_transaction(cur, mcp_cfg.query_timeout)
            cur.execute(query)

            columns: List[str] = []
            rows: List[Tuple[Any, ...]] = []
            truncated = False

            if cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                rows = list(cur.fetchmany(max_rows))
                extra = cur.fetchone()
                truncated = extra is not None

            status_message = cur.statusmessage
            conn.commit()

            return {
                "columns": columns,
                "rows": _to_jsonable_rows(rows),
                "truncated": truncated,
                "status_message": status_message,
                "backend_pid": job.backend_pid,
            }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        job.connection = None
        try:
            conn.close()
        except Exception:
            pass


def list_databases() -> List[str]:
    cfg = get_config()
    logger.info("Listing Greenplum databases")
    conn = create_connection(cfg.database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT datname
                FROM pg_database
                WHERE datistemplate = false AND datallowconn = true
                ORDER BY datname;
                """
            )
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def list_schemas(database: str) -> Dict[str, Any]:
    logger.info("Listing schemas for database=%s", database)
    conn = create_connection(database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.nspname
                FROM pg_namespace n
                WHERE n.nspname NOT LIKE 'pg_%'
                  AND n.nspname <> 'information_schema'
                  AND n.nspname <> 'gp_toolkit'
                  AND EXISTS (
                    SELECT 1
                    FROM pg_class c
                    WHERE c.relnamespace = n.oid
                      AND c.relkind IN ('r', 'p')
                  )
                ORDER BY n.nspname;
                """
            )
            schemas = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()
    return {"schemas": schemas}


def _page_token_state_matches(
    state: Dict[str, Any],
    database: str,
    schema: str,
    like: Optional[str],
    not_like: Optional[str],
    page_size: int,
) -> bool:
    return (
        state.get("database") == database
        and state.get("schema") == schema
        and state.get("like") == like
        and state.get("not_like") == not_like
        and state.get("page_size") == page_size
    )


def list_tables(
    database: str,
    schema: str,
    like: Optional[str] = None,
    not_like: Optional[str] = None,
    page_token: Optional[str] = None,
    page_size: int = 50,
) -> Dict[str, Any]:
    """
    List tables in a schema with distribution keys and storage type.

    Greenplum distribution keys come from `gp_distribution_policy.attrnums`.
    Storage type comes from `pg_appendonly.columnstore` (heap/ao_row/ao_column).
    """

    if page_size <= 0:
        raise ToolError("page_size must be a positive integer.")

    offset = 0
    if page_token and page_token in table_pagination_cache:
        state = table_pagination_cache[page_token]
        if _page_token_state_matches(state, database, schema, like, not_like, page_size):
            offset = int(state.get("offset", 0))
        # Token used once; drop it to avoid stale reuse.
        del table_pagination_cache[page_token]

    conn = create_connection(database)
    try:
        with conn.cursor() as cur:
            where_clauses: List[str] = ["n.nspname = %s", "c.relkind IN ('r','p')"]
            params: List[Any] = [schema]

            if like is not None:
                where_clauses.append("c.relname LIKE %s")
                params.append(like)
            if not_like is not None:
                where_clauses.append("c.relname NOT LIKE %s")
                params.append(not_like)

            where_sql = " AND ".join(where_clauses)

            cur.execute(
                f"""
                SELECT count(*)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE {where_sql};
                """,
                params,
            )
            total_tables = int(cur.fetchone()[0])

            cur.execute(
                f"""
                WITH targets AS (
                  SELECT
                    c.oid AS relid,
                    n.nspname AS schema_name,
                    c.relname AS table_name
                  FROM pg_class c
                  JOIN pg_namespace n ON n.oid = c.relnamespace
                  WHERE {where_sql}
                  ORDER BY c.relname
                  LIMIT %s OFFSET %s
                )
                SELECT
                  t.schema_name,
                  t.table_name,
                  CASE
                    WHEN p.localoid IS NULL THEN NULL
                    WHEN p.policytype = 'r' THEN 'random'
                    WHEN p.policytype = 'e' THEN 'replicated'
                    ELSE 'distributed'
                  END AS distribution_type,
                  CASE
                    WHEN p.localoid IS NULL OR p.policytype != 'p' OR p.distkey = ''
                      THEN ARRAY[]::text[]
                    ELSE (
                      SELECT array_agg(a.attname ORDER BY pos.ord)
                      FROM unnest(string_to_array(p.distkey::text, ' ')) WITH ORDINALITY AS pos(attnum_str, ord)
                      JOIN pg_attribute a ON a.attrelid = t.relid AND a.attnum = pos.attnum_str::int
                    )
                  END AS distribution_keys,
                  CASE
                    WHEN ao.relid IS NULL THEN 'heap'
                    WHEN ao.columnstore IS TRUE THEN 'ao_column'
                    ELSE 'ao_row'
                  END AS storage_type
                FROM targets t
                LEFT JOIN gp_distribution_policy p ON p.localoid = t.relid
                LEFT JOIN pg_appendonly ao ON ao.relid = t.relid
                ORDER BY t.table_name;
                """,
                params + [page_size, offset],
            )
            rows = cur.fetchall()
    finally:
        conn.close()

    tables: List[TableInfo] = []
    for schema_name, table_name, distribution_type, distribution_keys, storage_type in rows:
        # psycopg2 may return None for ARRAY aggregates.
        if distribution_keys is None:
            distribution_keys = []
        if not isinstance(distribution_keys, list):
            distribution_keys = list(distribution_keys)
        tables.append(
            TableInfo(
                database=database,
                schema=schema_name,
                name=table_name,
                distribution_type=distribution_type,
                distribution_keys=distribution_keys,
                storage_type=storage_type,
            )
        )

    has_more = (offset + len(tables)) < total_tables
    next_page_token = None
    if has_more:
        next_page_token = str(uuid.uuid4())
        table_pagination_cache[next_page_token] = {
            "database": database,
            "schema": schema,
            "like": like,
            "not_like": not_like,
            "page_size": page_size,
            "offset": offset + page_size,
        }

    return {
        "tables": [asdict(t) for t in tables],
        "next_page_token": next_page_token,
        "total_tables": total_tables,
    }


@dataclass
class ColumnInfo:
    name: str
    data_type: str
    nullable: bool
    default_value: Optional[str]
    comment: Optional[str]


def describe_table(database: str, schema: str, table: str) -> Dict[str, Any]:
    """
    Return column definitions, distribution policy, and partition rules for a table.
    """
    logger.info("Describing table %s.%s.%s", database, schema, table)
    conn = create_connection(database)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    a.attname,
                    pg_catalog.format_type(a.atttypid, a.atttypmod),
                    NOT a.attnotnull,
                    pg_get_expr(d.adbin, d.adrelid),
                    col_description(a.attrelid, a.attnum)
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                LEFT JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum
                WHERE n.nspname = %s
                  AND c.relname = %s
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                ORDER BY a.attnum;
                """,
                [schema, table],
            )
            rows = cur.fetchall()

            if not rows:
                raise ToolError(f"Table {schema}.{table} not found in database {database}.")

            columns = [
                ColumnInfo(
                    name=row[0],
                    data_type=row[1],
                    nullable=row[2],
                    default_value=row[3],
                    comment=row[4],
                )
                for row in rows
            ]

            # Distribution policy
            cur.execute(
                """
                SELECT
                  CASE
                    WHEN p.policytype = 'r' THEN 'random'
                    WHEN p.policytype = 'e' THEN 'replicated'
                    ELSE 'distributed'
                  END,
                  CASE
                    WHEN p.policytype != 'p' OR p.distkey = ''
                      THEN ARRAY[]::text[]
                    ELSE (
                      SELECT array_agg(a.attname ORDER BY pos.ord)
                      FROM unnest(string_to_array(p.distkey::text, ' '))
                             WITH ORDINALITY AS pos(attnum_str, ord)
                      JOIN pg_attribute a
                        ON a.attrelid = c.oid AND a.attnum = pos.attnum_str::int
                    )
                  END
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN gp_distribution_policy p ON p.localoid = c.oid
                WHERE n.nspname = %s AND c.relname = %s;
                """,
                [schema, table],
            )
            dist_row = cur.fetchone()
            distribution_type: Optional[str] = None
            distribution_keys: List[str] = []
            if dist_row:
                distribution_type = dist_row[0]
                distribution_keys = list(dist_row[1]) if dist_row[1] else []

            # Partition definition
            cur.execute(
                """
                SELECT pg_get_partition_def(c.oid, true)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = %s AND c.relname = %s;
                """,
                [schema, table],
            )
            part_row = cur.fetchone()
            partition_def: Optional[str] = None
            if part_row and part_row[0]:
                raw = part_row[0]
                # Strip WITH (...) storage clauses
                raw = re.sub(r"\s*WITH\s*\([^)]*\)", "", raw)
                # Strip COLUMN ... ENCODING (...) lines
                raw = re.sub(r"\s*COLUMN\s+\w+\s+ENCODING\s*\([^)]*\)", "", raw)
                # Collapse multiple blank lines / trailing whitespace
                raw = re.sub(r"[ \t]+\n", "\n", raw)
                raw = re.sub(r"\n{2,}", "\n", raw)
                partition_def = raw.strip()
    finally:
        conn.close()

    return {
        "database": database,
        "schema": schema,
        "table": table,
        "columns": [asdict(c) for c in columns],
        "distribution_type": distribution_type,
        "distribution_keys": distribution_keys,
        "partition_definition": partition_def,
    }


def execute_select_query(database: str, query: str, max_rows: int) -> Dict[str, Any]:
    mcp_cfg = get_mcp_config()

    _validate_select_query(query)
    if max_rows <= 0:
        raise ToolError("max_rows must be a positive integer.")

    conn = create_connection(database)
    try:
        with conn.cursor() as cur:
            _apply_readonly_transaction(cur, mcp_cfg.query_timeout)
            cur.execute(query)

            columns: List[str] = []
            rows: List[Tuple[Any, ...]] = []
            truncated = False

            if cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                rows = list(cur.fetchmany(max_rows))
                # If there is at least one more row, mark as truncated.
                extra = cur.fetchone()
                truncated = extra is not None

            return {
                "columns": columns,
                "rows": _to_jsonable_rows(rows),
                "truncated": truncated,
            }
    finally:
        # Ensure transaction is properly closed.
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass


def execute_query(database: str, query: str, max_rows: int) -> Dict[str, Any]:
    mcp_cfg = get_mcp_config()

    _validate_write_query(query)
    if max_rows <= 0:
        raise ToolError("max_rows must be a positive integer.")

    conn = create_connection(database)
    try:
        with conn.cursor() as cur:
            _apply_write_transaction(cur, mcp_cfg.query_timeout)
            cur.execute(query)

            columns: List[str] = []
            rows: List[Tuple[Any, ...]] = []
            truncated = False

            if cur.description is not None:
                columns = [desc[0] for desc in cur.description]
                rows = list(cur.fetchmany(max_rows))
                extra = cur.fetchone()
                truncated = extra is not None

            status_message = cur.statusmessage
            conn.commit()

            return {
                "columns": columns,
                "rows": _to_jsonable_rows(rows),
                "truncated": truncated,
                "status_message": status_message,
            }
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            conn.close()
        except Exception:
            pass


def run_select_query(
    query: str,
    database: Optional[str] = None,
    max_rows: int = 100,
) -> Dict[str, Any]:
    """
    Execute a read-only SELECT-like query against Greenplum.

    Constraints:
    - Only a single statement is allowed.
    - Only SELECT/WITH/EXPLAIN are allowed.
    """

    cfg = get_config()
    target_db = database or cfg.database
    logger.info("Running read-only select on %s", target_db)

    try:
        return execute_select_query(target_db, query, max_rows)
    except ToolError:
        raise
    except Exception as e:
        logger.exception("run_select_query failed")
        raise ToolError(f"Query execution failed: {str(e)}")


def run_query(
    query: str,
    database: Optional[str] = None,
    max_rows: int = 100,
) -> Dict[str, Any]:
    """
    Execute a single SQL statement against Greenplum.

    Write operations require GREENPLUM_ALLOW_WRITE_ACCESS=true.
    DROP/TRUNCATE additionally require GREENPLUM_ALLOW_DROP=true.
    """

    cfg = get_config()
    target_db = database or cfg.database
    logger.info("Running query on %s", target_db)

    try:
        return execute_query(target_db, query, max_rows)
    except ToolError:
        raise
    except Exception as e:
        logger.exception("run_query failed")
        raise ToolError(f"Query execution failed: {str(e)}")


def start_async_query(
    query: str,
    database: Optional[str] = None,
    max_rows: int = 100,
    job_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Start one Greenplum statement in a background worker.

    Intended for approved privileged sandbox actions that may outlive a normal
    MCP tool call. Use `get_query_status` to collect the result and
    `cancel_query` to cancel this server's own background connection.
    """

    cfg = get_config()
    target_db = database or cfg.database
    _validate_write_query(query)
    if max_rows <= 0:
        raise ToolError("max_rows must be a positive integer.")

    final_job_id = job_id or f"ai_mcp_{uuid.uuid4()}"
    if not re.match(r"^[A-Za-z0-9_.:\-]{1,160}$", final_job_id):
        raise ToolError("job_id contains unsupported characters.")

    job = AsyncJob(
        job_id=final_job_id,
        submitted_at=datetime.now(timezone.utc).isoformat(),
        database=target_db,
        query_preview=query[:1000],
    )
    future = executor.submit(_execute_query_async, job, target_db, query, max_rows)
    job.future = future
    async_jobs[final_job_id] = job

    return {
        "job_id": final_job_id,
        "status": "running",
        "database": target_db,
    }


def get_query_status(job_id: str) -> Dict[str, Any]:
    """
    Return status/result for a Greenplum async job started by this MCP server.
    """

    job = async_jobs.get(job_id)
    if job is None:
        raise ToolError(f"Unknown async job_id: {job_id}")

    if job.future and job.future.done():
        try:
            return {
                "job_id": job_id,
                "status": "completed",
                "submitted_at": job.submitted_at,
                "database": job.database,
                "backend_pid": job.backend_pid,
                "result": job.future.result(),
            }
        except Exception as e:
            return {
                "job_id": job_id,
                "status": "failed",
                "submitted_at": job.submitted_at,
                "database": job.database,
                "backend_pid": job.backend_pid,
                "error": str(e),
            }

    return {
        "job_id": job_id,
        "status": "running",
        "submitted_at": job.submitted_at,
        "database": job.database,
        "backend_pid": job.backend_pid,
    }


def cancel_query(job_id: str) -> Dict[str, Any]:
    """
    Cancel a Greenplum async query started by this MCP server.

    This uses psycopg2 connection.cancel(), so it is scoped to this server's own
    background connection and does not require terminating another user's
    backend.
    """

    job = async_jobs.get(job_id)
    if job is None:
        raise ToolError(f"Unknown async job_id: {job_id}")

    if job.future and job.future.done():
        return {
            "job_id": job_id,
            "status": "already_done",
            "backend_pid": job.backend_pid,
        }

    if job.connection is None:
        return {
            "job_id": job_id,
            "status": "cancel_not_ready",
            "backend_pid": job.backend_pid,
        }

    job.connection.cancel()
    return {
        "job_id": job_id,
        "status": "cancel_sent",
        "backend_pid": job.backend_pid,
    }


# Register tools based on configuration
if os.getenv("GREENPLUM_ENABLED", "true").lower() == "true":
    mcp.add_tool(Tool.from_function(list_databases))
    mcp.add_tool(Tool.from_function(list_schemas))
    mcp.add_tool(Tool.from_function(list_tables))
    mcp.add_tool(Tool.from_function(describe_table))
    mcp.add_tool(Tool.from_function(run_select_query))
    mcp.add_tool(Tool.from_function(run_query))
    mcp.add_tool(Tool.from_function(start_async_query))
    mcp.add_tool(Tool.from_function(get_query_status))
    mcp.add_tool(Tool.from_function(cancel_query))
    logger.info("Greenplum tools registered")

