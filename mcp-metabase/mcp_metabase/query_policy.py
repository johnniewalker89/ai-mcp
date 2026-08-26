from __future__ import annotations

import hashlib
from dataclasses import dataclass

import sqlglot
from sqlglot import Dialect, exp

from mcp_metabase.normalization import MutationValidationError

MAX_NATIVE_SQL_BYTES = 16_000

DIALECT_BY_ENGINE = {
    "clickhouse": "clickhouse",
    "greenplum": "postgres",
    "mysql": "mysql",
    "postgres": "postgres",
    "postgresql": "postgres",
}
QUERY_ROOTS = {"Except", "Intersect", "Select", "Union"}
MUTATING_OR_CONTROL_NODES = {
    "Alter",
    "Analyze",
    "Attach",
    "Cache",
    "Command",
    "Commit",
    "Copy",
    "Create",
    "Delete",
    "Detach",
    "Drop",
    "Execute",
    "Grant",
    "Insert",
    "Kill",
    "LoadData",
    "Merge",
    "Pragma",
    "Refresh",
    "Replace",
    "Revoke",
    "Rollback",
    "Set",
    "Transaction",
    "TruncateTable",
    "Uncache",
    "Update",
    "Use",
}
FORBIDDEN_QUERY_NODES = {"Into", "Lock"}
FORBIDDEN_QUERY_ARGUMENTS = {"format", "into", "locks", "settings"}

GENERIC_DANGEROUS_FUNCTIONS = {
    "benchmark",
    "get_lock",
    "is_free_lock",
    "is_used_lock",
    "load_file",
    "master_pos_wait",
    "release_all_locks",
    "release_lock",
    "sleep",
    "sys_eval",
    "sys_exec",
}
POSTGRES_DANGEROUS_FUNCTIONS = {
    "dblink",
    "dblink_cancel_query",
    "dblink_connect",
    "dblink_connect_u",
    "dblink_disconnect",
    "dblink_exec",
    "dblink_send_query",
    "lo_create",
    "lo_export",
    "lo_from_bytea",
    "lo_import",
    "lo_put",
    "lo_unlink",
    "nextval",
    "pg_advisory_lock",
    "pg_advisory_lock_shared",
    "pg_cancel_backend",
    "pg_export_snapshot",
    "pg_ls_dir",
    "pg_ls_logdir",
    "pg_ls_tmpdir",
    "pg_ls_waldir",
    "pg_read_binary_file",
    "pg_read_file",
    "pg_reload_conf",
    "pg_rotate_logfile",
    "pg_sleep",
    "pg_stat_file",
    "pg_stat_statements_reset",
    "pg_terminate_backend",
    "set_config",
    "setval",
}
POSTGRES_DANGEROUS_FUNCTION_PREFIXES = (
    "dblink",
    "lo_",
    "pg_advisory",
    "pg_backup_",
    "pg_cancel_",
    "pg_create_",
    "pg_copy_",
    "pg_drop_",
    "pg_file_",
    "pg_import_",
    "pg_log_",
    "pg_logical_",
    "pg_ls_",
    "pg_notify",
    "pg_promote",
    "pg_read_",
    "pg_reload_",
    "pg_replication_",
    "pg_rotate_",
    "pg_stat_force_",
    "pg_stat_reset",
    "pg_start_backup",
    "pg_stop_backup",
    "pg_switch_",
    "pg_sync_",
    "pg_terminate_",
    "pg_try_advisory",
    "pg_wal_",
    "pg_xlog_",
)
CLICKHOUSE_DANGEROUS_FUNCTIONS = {
    "azureblobstorage",
    "cluster",
    "clusterallreplicas",
    "deltalake",
    "dictionary",
    "dynamodb",
    "executable",
    "executablepool",
    "file",
    "filesystem",
    "generaterandom",
    "hdfs",
    "hudi",
    "iceberg",
    "input",
    "jdbc",
    "mongodb",
    "mysql",
    "odbc",
    "postgresql",
    "redis",
    "remote",
    "remotesecure",
    "s3",
    "s3cluster",
    "sleep",
    "sleepeachrow",
    "sqlite",
    "url",
}
SAFE_TABLE_FUNCTIONS = {
    "clickhouse": {"numbers", "numbers_mt", "zeros", "zeros_mt"},
    "mysql": {"json_table"},
    "postgres": {
        "generate_series",
        "json_array_elements",
        "json_each",
        "jsonb_array_elements",
        "jsonb_each",
        "regexp_split_to_table",
        "unnest",
    },
}


@dataclass(frozen=True)
class ValidatedNativeQuery:
    engine: str
    dialect: str
    sql_sha256: str


def _function_name(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Anonymous):
        return node.name.lower()
    if isinstance(node, exp.Func):
        name = node.sql_name().lower()
        return {
            "exploding_generate_series": "generate_series",
            "j_s_o_n_table": "json_table",
        }.get(name, name)
    return None


def _is_table_function(node: exp.Expression) -> bool:
    parent = node.parent
    return bool(
        isinstance(parent, (exp.From, exp.Join, exp.Lateral, exp.Table)) and parent.this is node
    )


def _qualified_table_function_is_safe(node: exp.Expression, dialect: str) -> bool:
    parent = node.parent
    if not isinstance(parent, exp.Table):
        return True
    catalog = parent.args.get("catalog")
    database = parent.args.get("db")
    if catalog is None and database is None:
        return True
    return bool(
        dialect == "postgres"
        and catalog is None
        and isinstance(database, exp.Identifier)
        and (
            (not database.args.get("quoted") and database.name.lower() == "pg_catalog")
            or (bool(database.args.get("quoted")) and database.name == "pg_catalog")
        )
    )


def _dangerous_functions(dialect: str) -> set[str]:
    result = set(GENERIC_DANGEROUS_FUNCTIONS)
    if dialect == "postgres":
        result.update(POSTGRES_DANGEROUS_FUNCTIONS)
    if dialect == "clickhouse":
        result.update(CLICKHOUSE_DANGEROUS_FUNCTIONS)
    return result


def _validate_ast(expression: exp.Expression, dialect: str) -> None:
    if type(expression).__name__ not in QUERY_ROOTS:
        raise MutationValidationError("Native preview allows only one SELECT/WITH query.")

    dangerous_functions = _dangerous_functions(dialect)
    for node in expression.walk():
        node_name = type(node).__name__
        if node_name in MUTATING_OR_CONTROL_NODES:
            raise MutationValidationError(
                f"SQL node {node_name} is not allowed in a native preview."
            )
        if node_name in FORBIDDEN_QUERY_NODES:
            raise MutationValidationError(
                f"SQL clause {node_name.upper()} is not allowed in a native preview."
            )
        for argument in FORBIDDEN_QUERY_ARGUMENTS:
            if node.args.get(argument):
                raise MutationValidationError(
                    f"SQL clause {argument.upper()} is not allowed in a native preview."
                )

        function_name = _function_name(node)
        if function_name and _is_table_function(node):
            if function_name not in SAFE_TABLE_FUNCTIONS[dialect]:
                raise MutationValidationError(
                    f"Table function {function_name} is not allowed in a native preview."
                )
            if (
                isinstance(node, exp.Anonymous)
                and isinstance(node.this, exp.Identifier)
                and node.this.args.get("quoted")
            ):
                raise MutationValidationError(
                    f"Quoted table function {function_name} is not allowed in a native preview."
                )
            if not _qualified_table_function_is_safe(node, dialect):
                raise MutationValidationError(
                    f"Qualified table function {function_name} is not allowed in a native preview."
                )
        if function_name and function_name in dangerous_functions:
            raise MutationValidationError(
                f"Function {function_name} is not allowed in a native preview."
            )
        if (
            function_name
            and dialect == "postgres"
            and function_name.startswith(POSTGRES_DANGEROUS_FUNCTION_PREFIXES)
        ):
            raise MutationValidationError(
                f"Function {function_name} is not allowed in a native preview."
            )


def validate_native_preview_sql(engine: str, sql: str) -> ValidatedNativeQuery:
    if not isinstance(engine, str):
        raise MutationValidationError("Metabase database engine is unavailable.")
    normalized_engine = engine.strip().casefold()
    dialect = DIALECT_BY_ENGINE.get(normalized_engine)
    if dialect is None:
        supported = ", ".join(sorted(DIALECT_BY_ENGINE))
        raise MutationValidationError(
            f"Native preview supports only these Metabase engines: {supported}."
        )
    if not isinstance(sql, str) or not sql.strip():
        raise MutationValidationError("Compiled native preview SQL must be non-empty.")
    if "\x00" in sql or len(sql.encode("utf-8")) > MAX_NATIVE_SQL_BYTES:
        raise MutationValidationError(
            f"Compiled native preview SQL must be <= {MAX_NATIVE_SQL_BYTES} UTF-8 bytes "
            "and contain no NUL."
        )

    try:
        tokenizer = Dialect.get_or_raise(dialect).tokenizer()
        tokens = tokenizer.tokenize(sql)
        if any(token.comments for token in tokens):
            raise MutationValidationError("SQL comments are not allowed in a native preview.")
        expressions = sqlglot.parse(sql, read=dialect)
    except MutationValidationError:
        raise
    except Exception as exc:
        raise MutationValidationError(
            f"SQL parser could not safely validate the {normalized_engine} preview."
        ) from exc
    if len(expressions) != 1 or expressions[0] is None:
        raise MutationValidationError("Native preview requires exactly one SQL statement.")

    _validate_ast(expressions[0], dialect)
    return ValidatedNativeQuery(
        engine=normalized_engine,
        dialect=dialect,
        sql_sha256=hashlib.sha256(sql.encode("utf-8")).hexdigest(),
    )
