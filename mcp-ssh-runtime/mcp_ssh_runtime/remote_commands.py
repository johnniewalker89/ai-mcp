from __future__ import annotations

import shlex

from mcp_ssh_runtime.policy import (
    RuntimeAccessError,
    ensure_file_read_allowed,
    validate_approved_command,
    validate_identifier,
    validate_line_limit,
    validate_max_bytes,
    validate_path_component,
    validate_relative_path,
    validate_remote_path,
    validate_service_name,
)


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _join_remote_path(base: str, relative_path: str) -> str:
    validate_remote_path("base", base)
    if not relative_path:
        return base.rstrip("/") or "/"
    return f"{base.rstrip('/')}/{relative_path}"


def _ensure_under_base(base: str, path: str) -> str:
    validate_remote_path("base", base)
    validate_remote_path("path", path)
    normalized_base = base.rstrip("/") + "/"
    if "/../" in path or path.endswith("/.."):
        raise RuntimeAccessError("path must not contain '..' segments.")
    if not path.startswith(normalized_base):
        raise RuntimeAccessError(f"path must be under {base}.")
    return path


def build_file_stat_command(path: str, approved_sensitive: bool = False) -> str:
    ensure_file_read_allowed(path, approved_sensitive=approved_sensitive)
    quoted_path = shlex.quote(path)
    return (
        f"if [ -e {quoted_path} ]; then "
        f"stat -c 'type=%F\\nmode=%A\\nowner=%U:%G\\nsize=%s\\nmtime=%y\\npath=%n' -- {quoted_path}; "
        "else echo 'missing'; exit 2; fi"
    )


def build_file_list_command(
    path: str,
    max_depth: int = 1,
    max_entries: int = 200,
    approved_sensitive: bool = False,
) -> str:
    ensure_file_read_allowed(path, approved_sensitive=approved_sensitive)
    if max_depth < 0 or max_depth > 5:
        raise RuntimeAccessError("max_depth must be between 0 and 5.")
    validate_line_limit(max_entries, name="max_entries", minimum=1, maximum=2000)
    return (
        "find "
        f"{shlex.quote(path)} -maxdepth {max_depth} -mindepth 1 "
        "-printf '%M %u %g %s %TY-%Tm-%Td %TH:%TM %p\\n' "
        f"| head -n {max_entries}"
    )


def build_file_read_command(
    path: str,
    max_bytes: int = 100_000,
    head_lines: int | None = None,
    tail_lines: int | None = None,
    approved_sensitive: bool = False,
) -> str:
    ensure_file_read_allowed(path, approved_sensitive=approved_sensitive)
    validate_max_bytes(max_bytes)
    quoted_path = shlex.quote(path)
    if head_lines is not None and tail_lines is not None:
        raise RuntimeAccessError("Use either head_lines or tail_lines, not both.")
    if head_lines is not None:
        lines = validate_line_limit(head_lines, name="head_lines")
        return f"head -n {lines} -- {quoted_path}"
    if tail_lines is not None:
        lines = validate_line_limit(tail_lines, name="tail_lines")
        return f"tail -n {lines} -- {quoted_path}"
    return f"head -c {max_bytes} -- {quoted_path}"


def build_legacy_airflow_dag_file_command(
    autogen_dir: str,
    dag_id: str,
    head_lines: int = 80,
) -> str:
    checked_dag_id = validate_path_component("dag_id", dag_id)
    lines = validate_line_limit(head_lines, name="head_lines", maximum=500)
    path = _join_remote_path(autogen_dir, f"{checked_dag_id}.py")
    ensure_file_read_allowed(path)
    quoted_path = shlex.quote(path)
    return (
        f"if [ -f {quoted_path} ]; then "
        f"stat -c 'type=%F\\nmode=%A\\nowner=%U:%G\\nsize=%s\\nmtime=%y\\npath=%n' -- {quoted_path}; "
        "printf '\\n--- head ---\\n'; "
        f"head -n {lines} -- {quoted_path}; "
        f"else echo 'missing legacy Airflow autogen DAG file: {path}'; exit 2; fi"
    )


def build_legacy_airflow_config_list_command(
    yml_dir: str,
    relative_path: str | None = "",
    max_depth: int = 2,
    max_entries: int = 200,
) -> str:
    checked_relative_path = validate_relative_path("relative_path", relative_path)
    if max_depth < 0 or max_depth > 5:
        raise RuntimeAccessError("max_depth must be between 0 and 5.")
    validate_line_limit(max_entries, name="max_entries", minimum=1, maximum=2000)
    path = _join_remote_path(yml_dir, checked_relative_path)
    ensure_file_read_allowed(path)
    return (
        "find "
        f"{shlex.quote(path)} -maxdepth {max_depth} -mindepth 1 "
        "-printf '%M %u %g %s %TY-%Tm-%Td %TH:%TM %p\\n' "
        f"| head -n {max_entries}"
    )


def build_legacy_airflow_log_list_command(
    logs_dir: str,
    dag_id: str,
    task_id: str | None = None,
    max_entries: int = 100,
) -> str:
    checked_dag_id = validate_identifier("dag_id", dag_id)
    checked_task_id = validate_identifier("task_id", task_id) if task_id else None
    validate_line_limit(max_entries, name="max_entries", minimum=1, maximum=2000)
    validate_remote_path("logs_dir", logs_dir)
    path_filters = [f"-path {shlex.quote(f'*{checked_dag_id}*')}"]
    if checked_task_id:
        path_filters.append(f"-path {shlex.quote(f'*{checked_task_id}*')}")
    return (
        f"find {shlex.quote(logs_dir)} -type f {' '.join(path_filters)} "
        "-printf '%T@ %TY-%Tm-%Td %TH:%TM %s %p\\n' "
        f"| sort -rn | head -n {max_entries}"
    )


def build_legacy_airflow_log_tail_command(
    logs_dir: str,
    path: str,
    tail_lines: int = 200,
    approved_sensitive: bool = False,
) -> str:
    checked_path = _ensure_under_base(logs_dir, path)
    ensure_file_read_allowed(checked_path, approved_sensitive=approved_sensitive)
    lines = validate_line_limit(tail_lines, name="tail_lines", maximum=2000)
    return f"tail -n {lines} -- {shlex.quote(checked_path)}"


def build_service_status_command(service: str, lines: int = 80) -> str:
    service = validate_service_name(service)
    lines = validate_line_limit(lines, maximum=500)
    return (
        f"systemctl is-enabled {shlex.quote(service)} 2>/dev/null || true; "
        f"systemctl is-active {shlex.quote(service)} 2>/dev/null || true; "
        f"systemctl status --no-pager --lines={lines} {shlex.quote(service)}"
    )


def build_service_logs_command(service: str, lines: int = 200) -> str:
    service = validate_service_name(service)
    lines = validate_line_limit(lines, maximum=2000)
    return f"journalctl -u {shlex.quote(service)} -n {lines} --no-pager"


def build_service_restart_command(service: str) -> str:
    service = validate_service_name(service)
    return f"sudo systemctl restart {shlex.quote(service)}"


def build_process_list_command(pattern: str | None = None) -> str:
    command = "ps -eo pid,ppid,user,stat,etime,cmd --sort=pid"
    if pattern:
        if "\x00" in pattern or "\r" in pattern or "\n" in pattern or len(pattern) > 200:
            raise RuntimeAccessError("pattern must be a short single-line string.")
        command += f" | grep -F -- {shlex.quote(pattern)} | grep -v grep"
    return command


def build_approved_command(command: str) -> str:
    return validate_approved_command(command)
