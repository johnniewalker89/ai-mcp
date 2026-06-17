from __future__ import annotations

import logging

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from mcp_ssh_runtime.airflow_commands import (
    MARK_TASK_STATE_SCRIPT,
    airflow_clear_tasks_args,
    airflow_dag_runs_args,
    airflow_dags_list_args,
    airflow_list_import_errors_args,
    airflow_pause_args,
    airflow_task_states_args,
    airflow_tasks_list_args,
    airflow_trigger_dag_args,
    airflow_unpause_args,
    airflow_version_args,
    build_airflow_command,
    build_airflow_python_command,
)
from mcp_ssh_runtime.mcp_env import (
    SSHRuntimeConfig,
    SshHostConfig,
    TransportType,
    get_config,
    get_mcp_config,
)
from mcp_ssh_runtime.policy import (
    ActionClass,
    RuntimeAccessError,
    ensure_action_allowed,
    ensure_airflow_host,
    validate_identifier,
    validate_task_state,
)
from mcp_ssh_runtime.remote_commands import (
    build_approved_command,
    build_file_list_command,
    build_file_read_command,
    build_file_stat_command,
    build_process_list_command,
    build_service_logs_command,
    build_service_restart_command,
    build_service_status_command,
)
from mcp_ssh_runtime.ssh_client import run_remote_command


MCP_SERVER_NAME = "mcp-ssh-runtime"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv()

mcp_config = get_mcp_config()
auth_provider = None
if mcp_config.server_transport in {TransportType.HTTP.value, TransportType.SSE.value}:
    if mcp_config.auth_disabled:
        logger.warning("WARNING: MCP SERVER AUTHENTICATION IS DISABLED")
    elif mcp_config.auth_token:
        auth_provider = StaticTokenVerifier(
            tokens={mcp_config.auth_token: {"client_id": "mcp-client", "scopes": []}},
            required_scopes=[],
        )
    else:
        raise ValueError(
            "Authentication token required for HTTP/SSE transports. "
            "Set SSH_RUNTIME_MCP_AUTH_TOKEN or SSH_RUNTIME_MCP_AUTH_DISABLED=true."
        )

mcp = FastMCP(name=MCP_SERVER_NAME, auth=auth_provider)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    if auth_provider is not None:
        auth_header = request.headers.get("Authorization")
        if not auth_header or not auth_header.startswith("Bearer "):
            return PlainTextResponse("Unauthorized", status_code=401)
        access_token = await auth_provider.verify_token(auth_header[7:])
        if access_token is None:
            return PlainTextResponse("Unauthorized", status_code=401)

    try:
        cfg = get_config()
        return PlainTextResponse(f"OK - configured SSH aliases: {len(cfg.hosts)}")
    except Exception as exc:
        return PlainTextResponse(f"ERROR - invalid config: {exc}", status_code=503)


def _host(action: ActionClass, alias: str, approved: bool = False) -> tuple[SSHRuntimeConfig, SshHostConfig]:
    try:
        cfg = get_config()
        host = cfg.get_host(alias)
        ensure_action_allowed(host, action, approved=approved)
        return cfg, host
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc
    except ValueError as exc:
        raise ToolError(str(exc)) from exc


def _run_airflow(
    alias: str,
    airflow_args: list[str],
    action: ActionClass,
    approved: bool = False,
) -> dict[str, object]:
    cfg, host = _host(action, alias, approved=approved)
    try:
        ensure_airflow_host(host)
        command = build_airflow_command(cfg, airflow_args)
        return run_remote_command(cfg, host, action, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def list_configured_hosts() -> dict[str, object]:
    """List configured SSH aliases and their policy profiles."""

    cfg = get_config()
    return {
        "hosts": [
            {"alias": host.alias, "profile": host.profile.value}
            for host in sorted(cfg.hosts.values(), key=lambda item: item.alias)
        ],
        "note": "Host aliases come from SSH_RUNTIME_HOST_PROFILES.",
    }


@mcp.tool()
def ssh_check(alias: str) -> dict[str, object]:
    """Run a safe read-only SSH connectivity and identity check."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    command = (
        "printf 'hostname=%s\\nuser=%s\\ndate_utc=%s\\n' "
        '"$(hostname -f 2>/dev/null || hostname)" "$(id -un)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)"'
    )
    return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()


@mcp.tool()
def file_stat(alias: str, path: str, approved_sensitive: bool = False) -> dict[str, object]:
    """Read remote file metadata. Sensitive paths require approved_sensitive=true."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_file_stat_command(path, approved_sensitive=approved_sensitive)
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def file_list(
    alias: str,
    path: str,
    max_depth: int = 1,
    max_entries: int = 200,
    approved_sensitive: bool = False,
) -> dict[str, object]:
    """List remote files under a path. Sensitive paths require approved_sensitive=true."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_file_list_command(
            path,
            max_depth=max_depth,
            max_entries=max_entries,
            approved_sensitive=approved_sensitive,
        )
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def file_read(
    alias: str,
    path: str,
    max_bytes: int = 100_000,
    head_lines: int | None = None,
    tail_lines: int | None = None,
    approved_sensitive: bool = False,
) -> dict[str, object]:
    """Read bounded remote file content. Sensitive paths require separate approval."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_file_read_command(
            path,
            max_bytes=max_bytes,
            head_lines=head_lines,
            tail_lines=tail_lines,
            approved_sensitive=approved_sensitive,
        )
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def service_status(alias: str, service: str, lines: int = 80) -> dict[str, object]:
    """Inspect remote systemd service status."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_service_status_command(service, lines=lines)
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def service_logs(alias: str, service: str, lines: int = 200) -> dict[str, object]:
    """Read bounded remote systemd journal logs for one service."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_service_logs_command(service, lines=lines)
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def service_restart(
    alias: str,
    service: str,
    reason: str,
    rollback: str,
    approved: bool = False,
) -> dict[str, object]:
    """Restart a remote systemd service. Requires approved=true after chat approval."""

    if not reason.strip() or not rollback.strip():
        raise ToolError("reason and rollback must be non-empty.")
    cfg, host = _host(ActionClass.HOST_CHANGE, alias, approved=approved)
    try:
        command = build_service_restart_command(service)
        result = run_remote_command(cfg, host, ActionClass.HOST_CHANGE, command).to_dict()
        result["approval_context"] = {
            "reason": reason,
            "target": service,
            "rollback": rollback,
        }
        return result
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def process_list(alias: str, pattern: str | None = None) -> dict[str, object]:
    """List remote processes, optionally filtered by a plain text pattern."""

    cfg, host = _host(ActionClass.SSH_READ, alias)
    try:
        command = build_process_list_command(pattern=pattern)
        return run_remote_command(cfg, host, ActionClass.SSH_READ, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def run_approved_command(
    alias: str,
    command: str,
    reason: str,
    target: str,
    rollback: str,
    approved: bool = False,
) -> dict[str, object]:
    """Run a single approved break-glass remote command. Requires approved=true."""

    if not reason.strip() or not target.strip() or not rollback.strip():
        raise ToolError("reason, target, and rollback must be non-empty.")
    cfg, host = _host(ActionClass.HOST_CHANGE, alias, approved=approved)
    try:
        remote_command = build_approved_command(command)
        result = run_remote_command(cfg, host, ActionClass.HOST_CHANGE, remote_command).to_dict()
        result["approval_context"] = {
            "reason": reason,
            "target": target,
            "rollback": rollback,
        }
        return result
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc


@mcp.tool()
def airflow_version(alias: str) -> dict[str, object]:
    """Return Airflow CLI version from a configured Airflow host."""

    return _run_airflow(alias, airflow_version_args(), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_list_import_errors(alias: str, output: str = "json") -> dict[str, object]:
    """List Airflow DAG import errors."""

    return _run_airflow(alias, airflow_list_import_errors_args(output), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_dags_list(alias: str, output: str = "json") -> dict[str, object]:
    """List Airflow DAGs."""

    return _run_airflow(alias, airflow_dags_list_args(output), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_tasks_list(alias: str, dag_id: str) -> dict[str, object]:
    """List tasks for an Airflow DAG."""

    return _run_airflow(alias, airflow_tasks_list_args(dag_id), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_dag_runs(
    alias: str,
    dag_id: str,
    state: str | None = None,
    output: str = "json",
) -> dict[str, object]:
    """List DAG runs for one Airflow DAG."""

    return _run_airflow(alias, airflow_dag_runs_args(dag_id, output, state), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_task_states(alias: str, dag_id: str, run_id: str, output: str = "json") -> dict[str, object]:
    """List task instance states for an Airflow DAG run."""

    return _run_airflow(alias, airflow_task_states_args(dag_id, run_id, output), ActionClass.AIRFLOW_READ)


@mcp.tool()
def airflow_trigger_dag(
    alias: str,
    dag_id: str,
    run_id: str | None = None,
    conf_json: str | None = None,
    logical_date: str | None = None,
    output: str = "json",
    approved: bool = False,
) -> dict[str, object]:
    """Trigger an Airflow DAG. Prod/prod-like profiles require approved=true after chat approval."""

    args = airflow_trigger_dag_args(dag_id, run_id, conf_json, logical_date, output)
    return _run_airflow(alias, args, ActionClass.AIRFLOW_CONTROL, approved=approved)


@mcp.tool()
def airflow_clear_tasks(
    alias: str,
    dag_id: str,
    task_regex: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    only_failed: bool = False,
    only_running: bool = False,
    downstream: bool = False,
    upstream: bool = False,
    approved: bool = False,
) -> dict[str, object]:
    """Clear Airflow task instances. Prod/prod-like profiles require approved=true after chat approval."""

    args = airflow_clear_tasks_args(
        dag_id,
        task_regex,
        start_date,
        end_date,
        only_failed,
        only_running,
        downstream,
        upstream,
    )
    return _run_airflow(alias, args, ActionClass.AIRFLOW_CONTROL, approved=approved)


@mcp.tool()
def airflow_pause_dag(alias: str, dag_id: str, approved: bool = False) -> dict[str, object]:
    """Pause an Airflow DAG. Prod/prod-like profiles require approved=true after chat approval."""

    return _run_airflow(alias, airflow_pause_args(dag_id), ActionClass.AIRFLOW_CONTROL, approved=approved)


@mcp.tool()
def airflow_unpause_dag(alias: str, dag_id: str, approved: bool = False) -> dict[str, object]:
    """Unpause an Airflow DAG. Prod/prod-like profiles require approved=true after chat approval."""

    return _run_airflow(alias, airflow_unpause_args(dag_id), ActionClass.AIRFLOW_CONTROL, approved=approved)


@mcp.tool()
def airflow_mark_task_state(
    alias: str,
    dag_id: str,
    task_id: str,
    run_id: str,
    state: str = "success",
    approved: bool = False,
) -> dict[str, object]:
    """Set one Airflow task instance to success or failed through Airflow's Python runtime."""

    cfg, host = _host(ActionClass.AIRFLOW_CONTROL, alias, approved=approved)
    try:
        ensure_airflow_host(host)
        checked_state = validate_task_state(state)
        args = [
            validate_identifier("dag_id", dag_id),
            validate_identifier("task_id", task_id),
            validate_identifier("run_id", run_id),
            checked_state,
        ]
        command = build_airflow_python_command(cfg, MARK_TASK_STATE_SCRIPT, args)
        return run_remote_command(cfg, host, ActionClass.AIRFLOW_CONTROL, command).to_dict()
    except RuntimeAccessError as exc:
        raise ToolError(str(exc)) from exc
