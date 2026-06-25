from mcp_ssh_runtime.airflow_commands import (
    airflow_dag_runs_args,
    airflow_pause_args,
    airflow_trigger_dag_args,
    build_airflow_command,
    filter_airflow_dags_stdout,
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


def test_filter_airflow_dags_json_limits_and_filters() -> None:
    stdout = (
        '[{"dag_id": "mkt_main_cube"}, {"dag_id": "ods_daily"}, '
        '{"dag_id": "mkt_costs"}]'
    )

    filtered, metadata = filter_airflow_dags_stdout(
        stdout,
        output="json",
        dag_id_contains="mkt",
        limit=1,
    )

    assert filtered == '[\n  {\n    "dag_id": "mkt_main_cube"\n  }\n]\n'
    assert metadata["matched_count"] == 2
    assert metadata["returned_count"] == 1


def test_filter_airflow_dags_table_keeps_header() -> None:
    stdout = (
        "dag_id        | fileloc\n"
        "==============+========\n"
        "mkt_main_cube | /dags/mkt.py\n"
        "ods_daily     | /dags/ods.py\n"
    )

    filtered, metadata = filter_airflow_dags_stdout(
        stdout,
        output="table",
        dag_id_contains="mkt",
        limit=10,
    )

    assert filtered == (
        "dag_id        | fileloc\n"
        "==============+========\n"
        "mkt_main_cube | /dags/mkt.py\n"
    )
    assert metadata["matched_count"] == 1
    assert metadata["returned_count"] == 1
