from __future__ import annotations

import json
import re
import shlex

from mcp_ssh_runtime.policy import (
    RuntimeAccessError,
    validate_line_limit,
    validate_path_component,
    validate_remote_path,
)


FLINK_JOB_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def validate_flink_job_id(job_id: str) -> str:
    if not isinstance(job_id, str) or not FLINK_JOB_ID_RE.match(job_id):
        raise RuntimeAccessError("job_id must be a 32-character lowercase hex Flink job id.")
    return job_id


def _python_as_gitlab_runner(script: str) -> str:
    return "sudo -n -iu gitlab-runner python3 -c " + shlex.quote(script)


def _validate_flink_paths(
    flink_bin: str,
    jar_path: str | None = None,
    schema_dir: str | None = None,
    config_path: str | None = None,
    savepoint_dir: str | None = None,
) -> None:
    validate_remote_path("flink_bin", flink_bin)
    for name, value in {
        "jar_path": jar_path,
        "schema_dir": schema_dir,
        "config_path": config_path,
        "savepoint_dir": savepoint_dir,
    }.items():
        if value is not None:
            validate_remote_path(name, value)


def build_flink_list_jobs_command(flink_bin: str = "/opt/flink/bin/flink") -> str:
    _validate_flink_paths(flink_bin)
    params = {"flink_bin": flink_bin}
    script = f"""
import json
import re
import subprocess
import sys

params = {json.dumps(params)}
completed = subprocess.run(
    [params["flink_bin"], "list"],
    check=False,
    capture_output=True,
    text=True,
)
jobs = []
pattern = re.compile(r"^(?P<submitted>\\d{{2}}\\.\\d{{2}}\\.\\d{{4}} \\d{{2}}:\\d{{2}}:\\d{{2}}) : (?P<job_id>[0-9a-f]+) : (?P<description>.*) \\((?P<state>[^()]*)\\)$")
for line in completed.stdout.splitlines():
    match = pattern.match(line.strip())
    if not match:
        continue
    description = match.group("description")
    name_match = re.search(r"\\[([^\\]]+)\\]", description)
    jobs.append({{
        "submitted": match.group("submitted"),
        "job_id": match.group("job_id"),
        "description": description,
        "job_name": name_match.group(1) if name_match else None,
        "state": match.group("state"),
    }})
print(json.dumps({{"jobs": jobs, "stderr": completed.stderr}}, ensure_ascii=False))
sys.exit(completed.returncode)
"""
    return _python_as_gitlab_runner(script)


def build_flink_job_exceptions_command(
    job_id: str,
    max_bytes: int = 4_000,
) -> str:
    checked_job_id = validate_flink_job_id(job_id)
    max_bytes = validate_line_limit(max_bytes, name="max_bytes", minimum=1, maximum=200_000)
    script = f"""
import json
import sys
import urllib.request

job_id = {json.dumps(checked_job_id)}
max_bytes = {max_bytes}
url = "http://localhost:8081/jobs/" + job_id + "/exceptions"
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        body = response.read(max_bytes).decode("utf-8", errors="replace")
except Exception as exc:
    print(json.dumps({{"error": str(exc), "job_id": job_id}}, ensure_ascii=False))
    sys.exit(1)
print(body)
"""
    return _python_as_gitlab_runner(script)


def build_flink_restart_job_command(
    job_name: str,
    flink_bin: str = "/opt/flink/bin/flink",
    jar_path: str | None = None,
    schema_dir: str = "/opt/flink/jobs/",
    config_path: str = "/opt/flink/jobs/config.properties",
    savepoint_dir: str | None = None,
    start_if_missing: bool = False,
) -> str:
    checked_job_name = validate_path_component("job_name", job_name)
    jar_path = jar_path or f"/opt/flink/jobs/{checked_job_name}.jar"
    savepoint_dir = savepoint_dir or f"/opt/flink/savepoints/{checked_job_name}"
    _validate_flink_paths(
        flink_bin,
        jar_path=jar_path,
        schema_dir=schema_dir,
        config_path=config_path,
        savepoint_dir=savepoint_dir,
    )
    params = {
        "job_name": checked_job_name,
        "flink_bin": flink_bin,
        "jar_path": jar_path,
        "schema_dir": schema_dir,
        "config_path": config_path,
        "savepoint_dir": savepoint_dir,
        "start_if_missing": start_if_missing,
    }
    script = f"""
import json
import re
import subprocess
import sys
import time

params = {json.dumps(params)}

def run(args):
    return subprocess.run(args, check=False, capture_output=True, text=True)

list_before = run([params["flink_bin"], "list"])
active_states = {{"CREATED", "RUNNING", "RESTARTING", "FAILING", "SUSPENDED"}}
job_line_re = re.compile(r"^(?P<submitted>\\d{{2}}\\.\\d{{2}}\\.\\d{{4}} \\d{{2}}:\\d{{2}}:\\d{{2}}) : (?P<job_id>[0-9a-f]+) : (?P<description>.*) \\((?P<state>[^()]*)\\)$")
matches = []
for line in list_before.stdout.splitlines():
    match = job_line_re.match(line.strip())
    if not match:
        continue
    if "[" + params["job_name"] + "]" in match.group("description") and match.group("state") in active_states:
        matches.append(match)

result = {{"list_before_stdout": list_before.stdout, "list_before_stderr": list_before.stderr}}
savepoint = None
if matches:
    old_job_id = matches[0].group("job_id")
    result["old_job_id"] = old_job_id
    stop = run([params["flink_bin"], "stop", old_job_id, "-p", params["savepoint_dir"]])
    result["stop_stdout"] = stop.stdout
    result["stop_stderr"] = stop.stderr
    result["stop_exit_code"] = stop.returncode
    if stop.returncode != 0:
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(stop.returncode)
    savepoint_match = re.search(r"file:(\\S+)", stop.stdout)
    if not savepoint_match:
        result["error"] = "savepoint path not found in flink stop output"
        print(json.dumps(result, ensure_ascii=False))
        sys.exit(3)
    savepoint = savepoint_match.group(1)
    result["savepoint"] = savepoint
elif not params["start_if_missing"]:
    result["error"] = "active job not found"
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(2)

run_args = [params["flink_bin"], "run"]
if savepoint:
    run_args += ["-s", savepoint]
run_args += [
    "-d",
    params["jar_path"],
    "--schema",
    params["schema_dir"],
    "--config",
    params["config_path"],
]
started = run(run_args)
result["run_stdout"] = started.stdout
result["run_stderr"] = started.stderr
result["run_exit_code"] = started.returncode
job_id_match = re.search(r"JobID\\s+([0-9a-f]+)", started.stdout)
if job_id_match:
    result["new_job_id"] = job_id_match.group(1)
if started.returncode != 0:
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(started.returncode)
time.sleep(3)
list_after = run([params["flink_bin"], "list"])
result["list_after_stdout"] = list_after.stdout
result["list_after_stderr"] = list_after.stderr
print(json.dumps(result, ensure_ascii=False))
"""
    return _python_as_gitlab_runner(script)
