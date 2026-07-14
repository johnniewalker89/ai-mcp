# mcp-bi-metadata

Read-only fallback MCP-сервер для BI OpenMetadata API.

Сервер не содержит credentials и читает токен только из локального env-файла.
По умолчанию использует `https://bi-metadata.x340.org/api`.

Основная локальная установка для Codex на Windows использует командный remote
OpenMetadata MCP через OAuth:

```toml
[mcp_servers.bi_metadata]
command = "C:\\Program Files\\nodejs\\npx.cmd"
args = ["-y", "mcp-remote", "https://bi-metadata.x340.org/mcp", "--auth-server-url=https://bi-metadata.x340.org/mcp", "--client-id=OpenMetadata"]
default_tools_approval_mode = "prompt"
```

Этот пакет нужен как переносимый read-only fallback, если remote OAuth MCP
недоступен или нужен узкий catalog-only контур без write/admin tools.

## Tools

- `bi_metadata_config` — показать локальную конфигурацию без токена.
- `bi_metadata_version` — проверить версию OpenMetadata.
- `bi_metadata_search` — поиск metadata entities, по умолчанию таблиц.
- `bi_metadata_list_tables` — bounded список таблиц.
- `bi_metadata_get_table_by_fqn` — таблица, колонки, owners, tags, domains по FQN.
- `bi_metadata_get_table_by_id` — таблица по id.
- `bi_metadata_table_lineage_by_fqn` — lineage таблицы по FQN.
- `bi_metadata_list_database_services` — database services.
- `bi_metadata_list_databases` — databases.
- `bi_metadata_list_database_schemas` — schemas.

Sample data и write/update endpoints намеренно не реализованы.

## Local env

```dotenv
BI_METADATA_MCP_BASE_URL=https://bi-metadata.x340.org
BI_METADATA_MCP_TOKEN=<local only>
# optional:
# BI_METADATA_MCP_API_PREFIX=/api
# BI_METADATA_MCP_AUTH_HEADER=Authorization
# BI_METADATA_MCP_AUTH_SCHEME=Bearer
```

## Codex fallback install

```toml
[mcp_servers.bi_metadata]
command = "uvx"
args = [
  "--refresh",
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-bi-metadata",
  "mcp-bi-metadata"
]
default_tools_approval_mode = "prompt"

[mcp_servers.bi_metadata.env]
BI_METADATA_MCP_ENV_FILE = "C:\\Users\\Admin\\.codex\\bi-metadata-mcp.env"
```

После настройки токена read-only tools можно перевести в approve на стороне
Codex-клиента, если нужно убрать подтверждения для безопасного чтения.
