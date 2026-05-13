# MCP ClickHouse Privileged

ClickHouse MCP server for controlled privileged work from Codex.

The server keeps short queries simple through `run_query` / `run_select_query`
and adds an async flow for long sandbox writes:

1. `start_async_query` returns a `query_id` immediately.
2. `get_query_status` polls `system.processes` / `system.query_log`.
3. `kill_query` cancels a runaway query when write access is enabled.

This avoids waiting for a long-running ClickHouse write inside a single MCP tool
call.

## Install

```toml
[mcp_servers.privileged_access_mcp_clickhouse]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-clickhouse",
  "mcp-clickhouse-privileged"
]
```

Credentials and host-specific settings belong only in local MCP client config.

## Environment

- `CLICKHOUSE_HOST`
- `CLICKHOUSE_PORT`, default `8123`
- `CLICKHOUSE_USER`
- `CLICKHOUSE_PASSWORD`
- `CLICKHOUSE_DATABASE`, default `default`
- `CLICKHOUSE_SECURE`, default `false`
- `CLICKHOUSE_VERIFY`, default `true`
- `CLICKHOUSE_CONNECT_TIMEOUT`, default `30`
- `CLICKHOUSE_MCP_QUERY_TIMEOUT`, default `300`
- `CLICKHOUSE_MCP_MAX_EXECUTION_TIME`, default `CLICKHOUSE_MCP_QUERY_TIMEOUT`, applied to async queries unless caller overrides it
- `CLICKHOUSE_ALLOW_WRITE_ACCESS`, default `false`
- `CLICKHOUSE_ALLOW_DROP`, default `false`
