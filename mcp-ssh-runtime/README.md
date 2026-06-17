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
- `airflow_version`
- `airflow_list_import_errors`
- `airflow_dags_list`
- `airflow_tasks_list`
- `airflow_dag_runs`
- `airflow_task_states`
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
- `db_dev`: allows `ssh_read`; `host_change` needs `approved=true`.
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

Override them in the local MCP client config if a host differs.

## Local Config Example

```toml
[mcp_servers.ssh_runtime]
command = "uvx"
args = [
  "--from",
  "git+https://github.com/johnniewalker89/ai-mcp.git#subdirectory=mcp-ssh-runtime",
  "mcp-ssh-runtime"
]

[mcp_servers.ssh_runtime.env]
SSH_RUNTIME_HOST_PROFILES = "AF-dev=airflow_dev,AF-prod=airflow_prod,AF-old=airflow_prod_like,GP-dev=db_dev"
SSH_RUNTIME_COMMAND_TIMEOUT = "120"
SSH_RUNTIME_MAX_OUTPUT_CHARS = "200000"
SSH_RUNTIME_AIRFLOW_OS_USER = "airflow"
SSH_RUNTIME_AIRFLOW_WORKDIR = "/opt/airflow/airflow"
SSH_RUNTIME_AIRFLOW_PYENV_VERSION = "airflow_3.12.11"
```

Host aliases must be configured in the local SSH config. Keep secrets, private
keys, and host-specific credentials out of this repository.

## Development

```bash
uv run --extra dev pytest
uv run --extra dev ruff check .
```
