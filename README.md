# AI MCP

Переносимые MCP-серверы для Codex и других MCP-совместимых агентов.

## Пакеты

- `mcp-greenplum` — MCP-сервер Greenplum с tools для метаданных,
  read-only запросов и опциональных write/drop действий, которые включаются
  только через переменные окружения.
- `mcp-clickhouse` — MCP-сервер ClickHouse с tools для метаданных и запросов,
  опциональным write/drop доступом и async-управлением долгими sandbox writes.
- `mcp-ssh-runtime` — MCP-сервер SSH runtime с tools для read-only
  SSH/Airflow/file/service/process evidence, bounded Airflow DAG discovery,
  Airflow 3 task-log reads, approval-gated Airflow control, approval-gated host
  changes и host-profile policy.
- `mcp-rabbitmq` — MCP-сервер RabbitMQ Management API: read-only просмотр
  brokers/exchanges/queues/bindings/consumers и approval-gated declare/bind/
  purge/delete операции.
- `mcp-bi-metadata` — read-only MCP-сервер для BI OpenMetadata API:
  поиск таблиц, чтение FQN/колонок/owners/tags/domains, lineage и database
  service/schema metadata без sample-data и write endpoints.
- `mcp_discord` — MCP-сервер Discord Bot API для безопасной работы с
  allowlisted community-каналами: read/post tools и approval-gated управление
  каналами/закрепами.

## Пример установки

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
  "mcp-greenplum-privileged"
]
```

SSH runtime:

```toml
[mcp_servers.ssh_runtime]
command = "uvx"
args = [
  "--refresh",
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-ssh-runtime",
  "mcp-ssh-runtime"
]

[mcp_servers.ssh_runtime.env]
SSH_RUNTIME_HOST_PROFILES = "AF-dev=airflow_dev,AF-prod=airflow_prod,AF-old=legacy_airflow_fs_only,GP-dev=db_dev"
```

Для Codex можно дополнительно поставить `approval_mode = "approve"` на
read-only tools `ssh_runtime` (`ssh_check`, file/service/process reads,
legacy Airflow read tools, Airflow read tools), чтобы приложение не спрашивало
подтверждение на каждое безопасное чтение. Не auto-approve
`airflow_control`, `service_restart`, `run_approved_command` и sensitive file
reads без отдельного решения.

Для больших Airflow-контуров используйте `airflow_dags_list` с
`dag_id_contains` и/или `limit`. По умолчанию tool возвращает не больше 200 DAG,
чтобы не забивать контекст MCP-клиента.

Для логов Airflow 3 используйте `airflow_task_log_list` и
`airflow_task_log_tail`. `legacy_airflow_task_log_*` предназначены только для
старого `legacy_airflow_fs_only` контура.

Credentials и настройки конкретного хоста должны жить только в локальном
конфиге MCP-клиента.

BI Metadata:

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

В локальном env-файле:

```dotenv
BI_METADATA_MCP_BASE_URL=https://bi-metadata.x340.org
BI_METADATA_MCP_TOKEN=<local only>
```

Для Codex можно auto-approve read-only tools `bi_metadata_config`,
`bi_metadata_version`, `bi_metadata_search`, `bi_metadata_list_tables`,
`bi_metadata_get_table_by_fqn`, `bi_metadata_get_table_by_id`,
`bi_metadata_table_lineage_by_fqn`, `bi_metadata_list_database_services`,
`bi_metadata_list_databases` и `bi_metadata_list_database_schemas`.
Sample-data и write/update tools в этом MCP не реализованы.

RabbitMQ:

```toml
[mcp_servers.rabbitmq]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-rabbitmq",
  "mcp-rabbitmq"
]
default_tools_approval_mode = "prompt"

[mcp_servers.rabbitmq.env]
RABBITMQ_MCP_ENV_FILE = "C:\\Users\\Admin\\.codex\\rabbitmq-mcp.env"
```

В локальном env-файле:

```dotenv
RABBITMQ_MCP_ALIASES=dev,prod
RABBITMQ_MCP_DEV_URL=http://rabbit-shot-1.x340.org:15672
RABBITMQ_MCP_DEV_PROFILE=dev
RABBITMQ_MCP_DEV_USERNAME=<local only>
RABBITMQ_MCP_DEV_PASSWORD=<local only>
RABBITMQ_MCP_PROD_URL=http://bi-rabbitmq81.x340.org:15672
RABBITMQ_MCP_PROD_PROFILE=prod
RABBITMQ_MCP_PROD_USERNAME=<local only>
RABBITMQ_MCP_PROD_PASSWORD=<local only>
```

Для Codex можно поставить `approval_mode = "approve"` только на read-only
RabbitMQ tools. Не auto-approve `declare_exchange`, `declare_queue`,
`bind_queue`, `purge_queue`, `delete_queue`; сами tools дополнительно требуют
`approved=true`.

Discord:

```toml
[mcp_servers.mcp_discord]
command = "uvx"
args = [
  "--refresh",
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp_discord",
  "mcp-discord"
]
default_tools_approval_mode = "prompt"

[mcp_servers.mcp_discord.env]
DISCORD_MCP_ENV_FILE = "C:\\Users\\Admin\\.codex\\discord-mcp.env"
```

В локальном env-файле:

```dotenv
DISCORD_MCP_BOT_TOKEN=<local only>
DISCORD_MCP_ALLOWED_GUILD_IDS=<server id>
DISCORD_MCP_ALLOWED_CHANNEL_IDS=<channel id>
DISCORD_MCP_RELEASE_CHANNEL_IDS=<release channel id>
```

Для Codex можно auto-approve только `list_allowed_scope`, `list_channels` и
`get_recent_messages` после настройки channel allowlist. `send_message`,
`send_embed`, `create_text_channel`, `update_channel`, `pin_message`,
`unpin_message`, `edit_own_message` и `delete_own_message` нужно оставить
prompt-gated; state-changing tools дополнительно требуют `approved=true`.
