# AI MCP

Portable MCP servers for Codex and other MCP-compatible agents.

## Packages

- `mcp-greenplum` - Greenplum MCP server with metadata tools, read-only query
  tool, and optional write/drop tool controlled by environment flags.
- `mcp-clickhouse` - ClickHouse MCP server with metadata/query tools,
  optional write/drop access, and async query control for long sandbox writes.

## Install Example

ClickHouse:

```toml
[mcp_servers.privileged_access_mcp_clickhouse]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-clickhouse",
  "mcp-clickhouse-privileged"
]
```

Greenplum:

```toml
[mcp_servers.privileged_access_mcp_greenplum]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-greenplum",
  "mcp-greenplum"
]
```

Credentials and host-specific settings belong only in local MCP client config.
