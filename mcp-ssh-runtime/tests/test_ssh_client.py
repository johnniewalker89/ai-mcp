import subprocess

from mcp_ssh_runtime.mcp_env import HostProfile, SSHRuntimeConfig, SshHostConfig
from mcp_ssh_runtime.policy import ActionClass
from mcp_ssh_runtime.ssh_client import run_remote_command


def test_ssh_runs_without_stdin(monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("SSH_RUNTIME_HOST_PROFILES", "AF-dev=airflow_dev")
    cfg = SSHRuntimeConfig()
    host = SshHostConfig(alias="AF-dev", profile=HostProfile.AIRFLOW_DEV)

    result = run_remote_command(cfg, host, ActionClass.SSH_READ, "true")

    assert captured["args"][1] == "-n"
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert result.exit_code == 0
