import pytest

from mcp_ssh_runtime.policy import RuntimeAccessError
from mcp_ssh_runtime.remote_commands import (
    build_file_read_command,
    build_process_list_command,
    build_service_restart_command,
    build_service_status_command,
)


def test_file_read_default_uses_bounded_head_c() -> None:
    command = build_file_read_command("/opt/airflow/airflow/dags/example.py")

    assert command == "head -c 100000 -- /opt/airflow/airflow/dags/example.py"


def test_file_read_sensitive_path_blocks_without_approval() -> None:
    with pytest.raises(RuntimeAccessError):
        build_file_read_command("/home/airflow/.ssh/id_rsa")


def test_service_status_is_read_only_systemctl_status() -> None:
    command = build_service_status_command("airflow-scheduler")

    assert "systemctl status --no-pager" in command
    assert "airflow-scheduler" in command


def test_service_restart_uses_sudo_systemctl_restart() -> None:
    command = build_service_restart_command("airflow-scheduler")

    assert command == "sudo systemctl restart airflow-scheduler"


def test_process_list_uses_fixed_string_filter() -> None:
    command = build_process_list_command("scheduler")

    assert "grep -F -- scheduler" in command
