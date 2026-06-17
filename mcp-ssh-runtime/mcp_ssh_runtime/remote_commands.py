from __future__ import annotations

import shlex

from mcp_ssh_runtime.policy import (
    RuntimeAccessError,
    ensure_file_read_allowed,
    validate_approved_command,
    validate_line_limit,
    validate_max_bytes,
    validate_service_name,
)


def shell_join(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


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
