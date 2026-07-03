# mcp-ssh-runtime

Controlled SSH runtime MCP server for read-only remote evidence, typed
Airflow scheduler control, and approval-gated host changes.

This server exposes typed tools, validates host profiles from local environment,
wraps Airflow commands under the Airflow OS user, and keeps database work
outside SSH. SSH subprocesses run without stdin (`ssh -n` plus `DEVNULL`) so MCP
stdio cannot be consumed or held open by remote commands. A break-glass command
tool exists only for explicit approval cases.

## Tools

- `list_configured_hosts`
- `ssh_check`
- `file_stat`
- `file_list`
- `file_read`
- `service_status`
- `service_logs`
- `service_restart`
- `process_list`
- `run_approved_command`
- `flink_list_jobs`
- `flink_job_exceptions`
- `flink_restart_job`
- `legacy_airflow_dag_file`
- `legacy_airflow_config_list`
- `legacy_airflow_task_log_list`
- `legacy_airflow_task_log_tail`
- `airflow_version`
- `airflow_list_import_errors`
- `airflow_dags_list` (`dag_id_contains` and `limit` keep large DAG lists bounded)
- `airflow_tasks_list`
- `airflow_dag_runs`
- `airflow_task_states`
- `airflow_task_log_list`
- `airflow_task_log_tail`
- `airflow_trigger_dag`
- `airflow_clear_tasks`
- `airflow_pause_dag`
- `airflow_unpause_dag`
- `airflow_mark_task_state`

## Action Policy

Host profiles are configured locally through `SSH_RUNTIME_HOST_PROFILES`.

- `airflow_dev`: allows `ssh_read`, `airflow_read`, and `airflow_control`;
  `host_change` needs `approved=true`.
- `airflow_prod`: allows `ssh_read` and `airflow_read`; `airflow_control`
  and `host_change` need `approved=true`.
- `airflow_prod_like`: same as `airflow_prod`.
- `legacy_airflow_fs_only`: old Airflow/Pentaho-style host with no typed
  Airflow CLI capability; allows `ssh_read` only by default and `host_change`
  only with `approved=true`.
- `db_dev`: allows `ssh_read`; `host_change` needs `approved=true`.
- `flink_dev`: allows `ssh_read`; `host_change` needs `approved=true`.
- `flink_prod`: allows `ssh_read`; `host_change` needs `approved=true`.
- `archive`: allows `ssh_read`; `host_change` needs `approved=true`.
- `unknown`: allows `ssh_read` only.

Default read-only DAG/runtime investigation includes bounded file
stat/list/read, service status/log tails, process list, SSH identity checks, and
Airflow read tools. Sensitive file paths such as private keys, `.env`, token,
password, and credential files require separate chat approval and
`approved_sensitive=true`.

`service_restart` and `run_approved_command` are `host_change` tools. Use them
only after approval names profile, action, target, reason, and rollback/cleanup
expectation. Raw database client commands through SSH are blocked.

No host profile exposes database query tools. Database metadata, queries,
writes, cleanup, and privileged operations must go through the database MCP
access path, not through SSH.

## Flink Runtime

Flink tools are available only for `flink_dev` and `flink_prod` host profiles.
They run on the remote host as the Flink working OS user:

```text
sudo -n -iu gitlab-runner ...
```

- `flink_list_jobs`: read-only `flink list` evidence with parsed job metadata.
- `flink_job_exceptions`: read-only Flink REST `/jobs/<job_id>/exceptions`.
- `flink_restart_job`: approval-gated `host_change`; stops one job by exact
  `[job_name]` with a savepoint and starts the configured jar with the same
  savepoint.

Default restart paths follow the repo deployment convention:

- jar: `/opt/flink/jobs/<job_name>.jar`
- schema dir: `/opt/flink/jobs/`
- config: `/opt/flink/jobs/config.properties`
- savepoint dir: `/opt/flink/savepoints/<job_name>`

## Airflow Runtime

Airflow commands run on the remote host as:

```text
cd <SSH_RUNTIME_AIRFLOW_WORKDIR>
sudo -n -u <SSH_RUNTIME_AIRFLOW_OS_USER> -- env ... airflow ...
```

`sudo -n` is intentional: Airflow tools must fail fast when the configured SSH
user cannot run as the Airflow OS user without an interactive password prompt.

Default Airflow values:

- `SSH_RUNTIME_AIRFLOW_OS_USER=airflow`
- `SSH_RUNTIME_AIRFLOW_HOME=/opt/airflow`
- `SSH_RUNTIME_AIRFLOW_WORKDIR=/opt/airflow/airflow`
- `SSH_RUNTIME_AIRFLOW_PYENV_ROOT=/opt/airflow/.pyenv`
- `SSH_RUNTIME_AIRFLOW_PYENV_VERSION=airflow_3.12.11`
- `SSH_RUNTIME_AIRFLOW_LOGS_DIR=/opt/airflow/airflow/logs`

Override them in the local MCP client config if a host differs.

`airflow_dags_list` is bounded by default (`limit=200`) to avoid flooding the
MCP client context on large Airflow installations. Use `dag_id_contains` for
normal DAG discovery, for example `dag_id_contains="mkt"` or
`dag_id_contains="main_cube"`. Pass `limit=null` only when the caller
intentionally needs the full list.

Use `airflow_task_log_list` and `airflow_task_log_tail` for Airflow 3 task logs
on `airflow_dev`, `airflow_prod`, and `airflow_prod_like` profiles. These tools
read modern Airflow task log paths under `SSH_RUNTIME_AIRFLOW_LOGS_DIR` as the
Airflow OS user. Do not use `legacy_airflow_task_log_*` for Airflow 3 hosts.

For legacy Airflow hosts without non-interactive sudo/login access to the
Airflow OS user, use `legacy_airflow_fs_only` and the legacy filesystem/log
tools instead of typed Airflow CLI tools:

- `legacy_airflow_dag_file`
- `legacy_airflow_config_list`
- `legacy_airflow_task_log_list`
- `legacy_airflow_task_log_tail`

## Local Config Example

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
SSH_RUNTIME_HOST_PROFILES = "AF-dev=airflow_dev,AF-prod=airflow_prod,AF-old=legacy_airflow_fs_only,GP-dev=db_dev,Archive=archive,Flink-dev=flink_dev,Flink-prod-n1=flink_prod,Flink-prod-n2=flink_prod,Flink-prod-n3=flink_prod"
SSH_RUNTIME_COMMAND_TIMEOUT = "120"
SSH_RUNTIME_MAX_OUTPUT_CHARS = "200000"
SSH_RUNTIME_AIRFLOW_OS_USER = "airflow"
SSH_RUNTIME_AIRFLOW_WORKDIR = "/opt/airflow/airflow"
SSH_RUNTIME_AIRFLOW_PYENV_VERSION = "airflow_3.12.11"
SSH_RUNTIME_AIRFLOW_LOGS_DIR = "/opt/airflow/airflow/logs"

# Optional Codex client policy: avoid prompting for every read-only evidence tool.
# Keep control/change/sensitive actions prompt-gated.
[mcp_servers.ssh_runtime.tools.list_configured_hosts]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.ssh_check]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.file_stat]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.file_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.file_read]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.service_status]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.service_logs]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.process_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.flink_list_jobs]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.flink_job_exceptions]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.legacy_airflow_dag_file]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.legacy_airflow_config_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.legacy_airflow_task_log_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.legacy_airflow_task_log_tail]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_version]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_list_import_errors]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_dags_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_tasks_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_dag_runs]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_task_states]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_task_log_list]
approval_mode = "approve"

[mcp_servers.ssh_runtime.tools.airflow_task_log_tail]
approval_mode = "approve"
```

Host aliases must be configured in the local SSH config. Keep secrets, private
keys, and host-specific credentials out of this repository.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```
