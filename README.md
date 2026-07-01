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
