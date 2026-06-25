from __future__ import annotations

import json
import shlex
from typing import Any

from mcp_ssh_runtime.mcp_env import SSHRuntimeConfig
from mcp_ssh_runtime.policy import RuntimeAccessError, validate_identifier


MARK_TASK_STATE_SCRIPT = r"""
import sys
from airflow.models.dagrun import DagRun
from airflow.utils.session import create_session
from airflow.utils.state import State

dag_id, task_id, run_id, target_state = sys.argv[1:5]
state = State.SUCCESS if target_state == "success" else State.FAILED

with create_session() as session:
    dag_run = (
        session.query(DagRun)
        .filter(DagRun.dag_id == dag_id, DagRun.run_id == run_id)
        .one()
    )
    task_instance = dag_run.get_task_instance(task_id, session=session)
    if task_instance is None:
        raise SystemExit(f"Task instance not found: {dag_id}.{task_id} in {run_id}")
    task_instance.set_state(state, session=session)
    print(f"set {dag_id}.{task_id} in {run_id} to {target_state}")
"""


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _airflow_env_args(cfg: SSHRuntimeConfig) -> list[str]:
    return [
        f"HOME={cfg.airflow_home}",
        f"PYENV_ROOT={cfg.airflow_pyenv_root}",
        f"PYENV_VERSION={cfg.airflow_pyenv_version}",
        f"PATH={cfg.airflow_path}",
    ]


def build_airflow_command(cfg: SSHRuntimeConfig, airflow_args: list[str]) -> str:
    command = [
        "cd",
        cfg.airflow_workdir,
        "&&",
        "sudo",
        "-n",
        "-u",
        cfg.airflow_os_user,
        "--",
        "env",
        *_airflow_env_args(cfg),
        "airflow",
        *airflow_args,
    ]
    return shell_join(command[:1]) + " " + shell_join(command[1:2]) + " && " + shell_join(command[3:])


def build_airflow_python_command(cfg: SSHRuntimeConfig, script: str, args: list[str]) -> str:
    command = [
        "cd",
        cfg.airflow_workdir,
        "&&",
        "sudo",
        "-n",
        "-u",
        cfg.airflow_os_user,
        "--",
        "env",
        *_airflow_env_args(cfg),
        "python",
        "-c",
        script,
        *args,
    ]
    return shell_join(command[:1]) + " " + shell_join(command[1:2]) + " && " + shell_join(command[3:])


def checked_json_object(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeAccessError(f"conf_json must be valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise RuntimeAccessError("conf_json must be a JSON object.")
    return json.dumps(decoded, separators=(",", ":"), ensure_ascii=False)


def output_arg(output: str) -> list[str]:
    if output not in {"table", "json", "yaml", "plain"}:
        raise RuntimeAccessError("output must be one of: table, json, yaml, plain.")
    return ["-o", output]


def validate_optional_dag_filter(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return validate_identifier("dag_id_contains", value)


def validate_optional_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or value < 1 or value > 5000:
        raise RuntimeAccessError("limit must be between 1 and 5000, or null for no limit.")
    return value


def filter_airflow_dags_stdout(
    stdout: str,
    output: str,
    dag_id_contains: str | None,
    limit: int | None,
) -> tuple[str, dict[str, object]]:
    checked_filter = validate_optional_dag_filter(dag_id_contains)
    checked_limit = validate_optional_limit(limit)
    if checked_filter is None and checked_limit is None:
        return stdout, {"filtered": False}
    if output == "json":
        return _filter_airflow_dags_json(stdout, checked_filter, checked_limit)
    return _filter_airflow_dags_lines(stdout, checked_filter, checked_limit)


def _dag_row_matches(row: Any, dag_id_contains: str | None) -> bool:
    if dag_id_contains is None:
        return True
    if isinstance(row, dict):
        return dag_id_contains in str(row.get("dag_id", ""))
    return dag_id_contains in str(row)


def _filter_airflow_dags_json(
    stdout: str,
    dag_id_contains: str | None,
    limit: int | None,
) -> tuple[str, dict[str, object]]:
    try:
        decoded = json.loads(stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeAccessError(f"airflow_dags_list returned invalid JSON: {exc}") from exc
    if not isinstance(decoded, list):
        raise RuntimeAccessError("airflow_dags_list JSON output must be a list.")

    matched = [row for row in decoded if _dag_row_matches(row, dag_id_contains)]
    returned = matched[:limit] if limit is not None else matched
    metadata = {
        "filtered": True,
        "matched_count": len(matched),
        "returned_count": len(returned),
        "limit": limit,
        "dag_id_contains": dag_id_contains,
    }
    return json.dumps(returned, ensure_ascii=False, indent=2) + "\n", metadata


def _split_table_header(lines: list[str]) -> tuple[list[str], list[str]]:
    if len(lines) >= 2 and "dag_id" in lines[0] and set(lines[1].replace("+", "").strip()) <= {"="}:
        return lines[:2], lines[2:]
    return [], lines


def _filter_airflow_dags_lines(
    stdout: str,
    dag_id_contains: str | None,
    limit: int | None,
) -> tuple[str, dict[str, object]]:
    had_trailing_newline = stdout.endswith("\n")
    lines = stdout.splitlines()
    header, body = _split_table_header(lines)
    matched = [line for line in body if dag_id_contains is None or dag_id_contains in line]
    returned = matched[:limit] if limit is not None else matched
    result_lines = [*header, *returned]
    result = "\n".join(result_lines)
    if result and had_trailing_newline:
        result += "\n"
    metadata = {
        "filtered": True,
        "matched_count": len(matched),
        "returned_count": len(returned),
        "limit": limit,
        "dag_id_contains": dag_id_contains,
    }
    return result, metadata


def airflow_version_args() -> list[str]:
    return ["version"]


def airflow_list_import_errors_args(output: str) -> list[str]:
    return ["dags", "list-import-errors", *output_arg(output)]


def airflow_dags_list_args(output: str) -> list[str]:
    return ["dags", "list", *output_arg(output)]


def airflow_tasks_list_args(dag_id: str) -> list[str]:
    return ["tasks", "list", validate_identifier("dag_id", dag_id)]


def airflow_dag_runs_args(dag_id: str, output: str, state: str | None) -> list[str]:
    args = ["dags", "list-runs", validate_identifier("dag_id", dag_id), *output_arg(output)]
    if state:
        args.extend(["--state", validate_identifier("state", state)])
    return args


def airflow_task_states_args(dag_id: str, run_id: str, output: str) -> list[str]:
    return [
        "tasks",
        "states-for-dag-run",
        *output_arg(output),
        validate_identifier("dag_id", dag_id),
        validate_identifier("run_id", run_id),
    ]


def airflow_trigger_dag_args(
    dag_id: str,
    run_id: str | None,
    conf_json: str | None,
    logical_date: str | None,
    output: str,
) -> list[str]:
    args = ["dags", "trigger", validate_identifier("dag_id", dag_id), *output_arg(output)]
    if run_id:
        args.extend(["--run-id", validate_identifier("run_id", run_id)])
    checked_conf = checked_json_object(conf_json)
    if checked_conf:
        args.extend(["--conf", checked_conf])
    if logical_date:
        args.extend(["--logical-date", validate_identifier("logical_date", logical_date)])
    return args


def airflow_clear_tasks_args(
    dag_id: str,
    task_regex: str | None,
    start_date: str | None,
    end_date: str | None,
    only_failed: bool,
    only_running: bool,
    downstream: bool,
    upstream: bool,
) -> list[str]:
    args = ["tasks", "clear", validate_identifier("dag_id", dag_id), "--yes"]
    if task_regex:
        args.extend(["--task-regex", validate_identifier("task_regex", task_regex)])
    if start_date:
        args.extend(["--start-date", validate_identifier("start_date", start_date)])
    if end_date:
        args.extend(["--end-date", validate_identifier("end_date", end_date)])
    if only_failed:
        args.append("--only-failed")
    if only_running:
        args.append("--only-running")
    if downstream:
        args.append("--downstream")
    if upstream:
        args.append("--upstream")
    return args


def airflow_pause_args(dag_id: str) -> list[str]:
    return ["dags", "pause", validate_identifier("dag_id", dag_id), "--yes"]


def airflow_unpause_args(dag_id: str) -> list[str]:
    return ["dags", "unpause", validate_identifier("dag_id", dag_id), "--yes"]
