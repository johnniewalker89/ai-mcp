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
