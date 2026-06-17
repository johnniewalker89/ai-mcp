from mcp_ssh_runtime.airflow_commands import (
    airflow_dag_runs_args,
    airflow_pause_args,
    airflow_trigger_dag_args,
    build_airflow_command,
)
from mcp_ssh_runtime.mcp_env import SSHRuntimeConfig


def test_airflow_command_uses_airflow_os_user(monkeypatch) -> None:
    monkeypatch.setenv("SSH_RUNTIME_HOST_PROFILES", "AF-dev=airflow_dev")
    monkeypatch.setenv("SSH_RUNTIME_AIRFLOW_OS_USER", "airflow")
    monkeypatch.setenv("SSH_RUNTIME_AIRFLOW_WORKDIR", "/opt/airflow/airflow")
    cfg = SSHRuntimeConfig()

    command = build_airflow_command(cfg, ["version"])

    assert "cd /opt/airflow/airflow && sudo -n -u airflow -- env" in command
    assert " airflow version" in command


def test_trigger_dag_args_are_typed_and_json_safe() -> None:
    args = airflow_trigger_dag_args(
        dag_id="example_dag",
        run_id="manual__2026-06-17T00:00:00+00:00",
        conf_json='{"sensor":"skip"}',
        logical_date=None,
        output="json",
    )

    assert args[:3] == ["dags", "trigger", "example_dag"]
    assert "--conf" in args
    assert '{"sensor":"skip"}' in args


def test_list_runs_uses_positional_dag_id() -> None:
    args = airflow_dag_runs_args("example_dag", "json", None)

    assert args == ["dags", "list-runs", "example_dag", "-o", "json"]


def test_pause_uses_yes_to_avoid_prompt() -> None:
    assert airflow_pause_args("example_dag") == ["dags", "pause", "example_dag", "--yes"]
