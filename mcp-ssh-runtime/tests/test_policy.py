import pytest

from mcp_ssh_runtime.mcp_env import HostProfile, SshHostConfig
from mcp_ssh_runtime.policy import (
    ActionClass,
    RuntimeAccessError,
    ensure_action_allowed,
    ensure_file_read_allowed,
    validate_approved_command,
)


def host(profile: HostProfile) -> SshHostConfig:
    return SshHostConfig(alias="alias", profile=profile)


def test_dev_airflow_allows_control_by_default() -> None:
    ensure_action_allowed(host(HostProfile.AIRFLOW_DEV), ActionClass.AIRFLOW_CONTROL)


def test_prod_airflow_control_requires_approval() -> None:
    with pytest.raises(RuntimeAccessError):
        ensure_action_allowed(host(HostProfile.AIRFLOW_PROD), ActionClass.AIRFLOW_CONTROL)

    ensure_action_allowed(
        host(HostProfile.AIRFLOW_PROD),
        ActionClass.AIRFLOW_CONTROL,
        approved=True,
    )


def test_prod_like_airflow_control_requires_approval() -> None:
    with pytest.raises(RuntimeAccessError):
        ensure_action_allowed(host(HostProfile.AIRFLOW_PROD_LIKE), ActionClass.AIRFLOW_CONTROL)


def test_db_dev_blocks_airflow_read() -> None:
    with pytest.raises(RuntimeAccessError):
        ensure_action_allowed(host(HostProfile.DB_DEV), ActionClass.AIRFLOW_READ)


def test_host_change_requires_approval_on_dev() -> None:
    with pytest.raises(RuntimeAccessError):
        ensure_action_allowed(host(HostProfile.AIRFLOW_DEV), ActionClass.HOST_CHANGE)

    ensure_action_allowed(
        host(HostProfile.AIRFLOW_DEV),
        ActionClass.HOST_CHANGE,
        approved=True,
    )


def test_sensitive_file_read_requires_separate_approval() -> None:
    with pytest.raises(RuntimeAccessError):
        ensure_file_read_allowed("/opt/airflow/.env")

    ensure_file_read_allowed("/opt/airflow/.env", approved_sensitive=True)


def test_raw_database_commands_are_forbidden() -> None:
    with pytest.raises(RuntimeAccessError):
        validate_approved_command("psql -c 'select 1'")
