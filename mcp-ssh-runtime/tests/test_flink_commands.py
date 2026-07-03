from mcp_ssh_runtime.flink_commands import (
    build_flink_job_exceptions_command,
    build_flink_list_jobs_command,
    build_flink_restart_job_command,
)
from mcp_ssh_runtime.policy import RuntimeAccessError


def test_flink_list_jobs_runs_as_gitlab_runner_python() -> None:
    command = build_flink_list_jobs_command()

    assert command.startswith("sudo -n -iu gitlab-runner python3 -c ")
    assert "/opt/flink/bin/flink" in command
    assert "subprocess.run" in command


def test_flink_restart_job_uses_savepoint_and_default_paths() -> None:
    command = build_flink_restart_job_command("appsflyer_skan")

    assert command.startswith("sudo -n -iu gitlab-runner python3 -c ")
    assert "/opt/flink/jobs/appsflyer_skan.jar" in command
    assert "/opt/flink/savepoints/appsflyer_skan" in command
    assert '"stop"' in command
    assert '"run"' in command
    assert '"-s"' in command


def test_flink_job_exceptions_validates_job_id() -> None:
    command = build_flink_job_exceptions_command("a" * 32)

    assert "http://localhost:8081/jobs/" in command


def test_flink_job_exceptions_rejects_invalid_job_id() -> None:
    try:
        build_flink_job_exceptions_command("not-a-job-id")
    except RuntimeAccessError as exc:
        assert "job_id" in str(exc)
    else:
        raise AssertionError("Expected RuntimeAccessError")
