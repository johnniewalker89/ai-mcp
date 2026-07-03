from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import subprocess

from mcp_ssh_runtime.mcp_env import SSHRuntimeConfig, SshHostConfig
from mcp_ssh_runtime.policy import ActionClass


SECRET_PATTERNS = [
    re.compile(
        r"(?i)(^|[\s,{;])"
        r"([A-Za-z0-9_.-]*(?:password|passwd|pass|pwd|token|secret|private[_-]?key)[A-Za-z0-9_.-]*)"
        r"\s*=\s*[^,\s;]+"
    ),
    re.compile(
        r'(?i)"([^"]*(?:password|passwd|pass|pwd|token|secret|private[_-]?key)[^"]*)"\s*:\s*"[^"]*"'
    ),
    re.compile(
        r"(?i)'([^']*(?:password|passwd|pass|pwd|token|secret|private[_-]?key)[^']*)'\s*:\s*'[^']*'"
    ),
]


@dataclass
class CommandResult:
    host_alias: str
    profile: str
    action_class: str
    command_preview: str
    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)(^|"):
            redacted = pattern.sub(lambda match: f"{match.group(1)}{match.group(2)}=<redacted>", redacted)
        else:
            redacted = pattern.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return redacted


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n...<truncated>...", True


def run_remote_command(
    cfg: SSHRuntimeConfig,
    host: SshHostConfig,
    action: ActionClass,
    remote_command: str,
) -> CommandResult:
    ssh_args = [
        cfg.ssh_binary,
        "-n",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"ConnectTimeout={cfg.connect_timeout}",
        host.alias,
        remote_command,
    ]

    try:
        completed = subprocess.run(
            ssh_args,
            check=False,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=cfg.command_timeout,
        )
        stdout, stdout_truncated = _truncate(redact_text(completed.stdout), cfg.max_output_chars)
        stderr, stderr_truncated = _truncate(redact_text(completed.stderr), cfg.max_output_chars)
        return CommandResult(
            host_alias=host.alias,
            profile=host.profile.value,
            action_class=action.value,
            command_preview=redact_text(remote_command),
            exit_code=completed.returncode,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        stdout, stdout_truncated = _truncate(redact_text(stdout), cfg.max_output_chars)
        stderr, stderr_truncated = _truncate(redact_text(stderr), cfg.max_output_chars)
        return CommandResult(
            host_alias=host.alias,
            profile=host.profile.value,
            action_class=action.value,
            command_preview=redact_text(remote_command),
            exit_code=124,
            stdout=stdout,
            stderr=stderr + f"\nCommand timed out after {cfg.command_timeout} seconds.",
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
        )
