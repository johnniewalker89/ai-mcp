from __future__ import annotations

from enum import Enum
import re

from mcp_ssh_runtime.mcp_env import HostProfile, SshHostConfig


class ActionClass(str, Enum):
    SSH_READ = "ssh_read"
    AIRFLOW_READ = "airflow_read"
    AIRFLOW_CONTROL = "airflow_control"
    HOST_CHANGE = "host_change"


class RuntimeAccessError(ValueError):
    """Raised when a remote action violates the configured access policy."""


IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9_.:@+~/=-]{1,300}$")
AIRFLOW_STATE_RE = re.compile(r"^(success|failed)$")
SERVICE_RE = re.compile(r"^[A-Za-z0-9_.@:+-]{1,200}$")
PATH_COMPONENT_RE = re.compile(r"^[A-Za-z0-9_.:@+=-]{1,300}$")
RELATIVE_PATH_RE = re.compile(r"^[A-Za-z0-9_.:@+=/-]{0,1000}$")
REMOTE_PATH_RE = re.compile(r"^[^\x00\r\n]{1,1000}$")
SENSITIVE_PATH_RE = re.compile(
    r"(?i)(^|/)(\.ssh|id_rsa|id_dsa|id_ecdsa|id_ed25519|\.pgpass|\.my\.cnf|"
    r"\.env|credentials?\.json|authorized_user\.json|token|secret|password|"
    r"passwd|private[_-]?key|config\.db[^/]*\.yml)($|/)"
)
FORBIDDEN_RAW_COMMAND_RE = re.compile(
    r"(?i)(^|[\s;&|])(psql|clickhouse-client|mysql|airflow\s+db|kubectl\s+exec)\b"
)


DEFAULT_ALLOWED: dict[HostProfile, set[ActionClass]] = {
    HostProfile.AIRFLOW_DEV: {
        ActionClass.SSH_READ,
        ActionClass.AIRFLOW_READ,
        ActionClass.AIRFLOW_CONTROL,
    },
    HostProfile.AIRFLOW_PROD: {ActionClass.SSH_READ, ActionClass.AIRFLOW_READ},
    HostProfile.AIRFLOW_PROD_LIKE: {ActionClass.SSH_READ, ActionClass.AIRFLOW_READ},
    HostProfile.LEGACY_AIRFLOW_FS_ONLY: {ActionClass.SSH_READ},
    HostProfile.DB_DEV: {ActionClass.SSH_READ},
    HostProfile.FLINK_DEV: {ActionClass.SSH_READ},
    HostProfile.FLINK_PROD: {ActionClass.SSH_READ},
    HostProfile.ARCHIVE: {ActionClass.SSH_READ},
    HostProfile.UNKNOWN: {ActionClass.SSH_READ},
}


APPROVAL_ALLOWED: dict[HostProfile, set[ActionClass]] = {
    HostProfile.AIRFLOW_DEV: {ActionClass.HOST_CHANGE},
    HostProfile.AIRFLOW_PROD: {ActionClass.AIRFLOW_CONTROL, ActionClass.HOST_CHANGE},
    HostProfile.AIRFLOW_PROD_LIKE: {ActionClass.AIRFLOW_CONTROL, ActionClass.HOST_CHANGE},
    HostProfile.LEGACY_AIRFLOW_FS_ONLY: {ActionClass.HOST_CHANGE},
    HostProfile.DB_DEV: {ActionClass.HOST_CHANGE},
    HostProfile.FLINK_DEV: {ActionClass.HOST_CHANGE},
    HostProfile.FLINK_PROD: {ActionClass.HOST_CHANGE},
    HostProfile.ARCHIVE: {ActionClass.HOST_CHANGE},
    HostProfile.UNKNOWN: set(),
}


AIRFLOW_PROFILES = {
    HostProfile.AIRFLOW_DEV,
    HostProfile.AIRFLOW_PROD,
    HostProfile.AIRFLOW_PROD_LIKE,
}

LEGACY_AIRFLOW_FS_ONLY_MESSAGE = (
    "Airflow CLI tools are unavailable for profile legacy_airflow_fs_only. "
    "Use legacy Airflow filesystem/log tools for read-only evidence, or configure "
    "a separate host profile with non-interactive sudo/login access to the Airflow OS user."
)


def validate_identifier(name: str, value: str) -> str:
    if not isinstance(value, str) or not value:
        raise RuntimeAccessError(f"{name} must be a non-empty string.")
    if not IDENTIFIER_RE.match(value):
        raise RuntimeAccessError(
            f"{name} contains unsupported characters. Use Airflow-safe identifiers only."
        )
    return value


def validate_optional_identifier(name: str, value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return validate_identifier(name, value)


def validate_task_state(value: str) -> str:
    if not AIRFLOW_STATE_RE.match(value):
        raise RuntimeAccessError("state must be 'success' or 'failed'.")
    return value


def validate_service_name(value: str) -> str:
    if not isinstance(value, str) or not SERVICE_RE.match(value):
        raise RuntimeAccessError("service must be a systemd-safe service name.")
    return value


def validate_path_component(name: str, value: str) -> str:
    if not isinstance(value, str) or not PATH_COMPONENT_RE.match(value):
        raise RuntimeAccessError(f"{name} must be a safe single path component.")
    if value in {".", ".."}:
        raise RuntimeAccessError(f"{name} must not be '.' or '..'.")
    return value


def validate_relative_path(name: str, value: str | None) -> str:
    if value is None or value == "":
        return ""
    if not isinstance(value, str) or not RELATIVE_PATH_RE.match(value):
        raise RuntimeAccessError(f"{name} must be a safe relative path.")
    if value.startswith("/") or "//" in value:
        raise RuntimeAccessError(f"{name} must be relative and normalized.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise RuntimeAccessError(f"{name} must not contain empty, '.', or '..' segments.")
    return value


def validate_remote_path(name: str, value: str) -> str:
    if not isinstance(value, str) or not value or not REMOTE_PATH_RE.match(value):
        raise RuntimeAccessError(f"{name} must be a non-empty remote path.")
    return value


def is_sensitive_path(path: str) -> bool:
    return bool(SENSITIVE_PATH_RE.search(path))


def ensure_file_read_allowed(path: str, approved_sensitive: bool = False) -> None:
    validate_remote_path("path", path)
    if is_sensitive_path(path) and not approved_sensitive:
        raise RuntimeAccessError(
            "Sensitive file path requires separate approval. "
            "Call with approved_sensitive=true only after chat approval names the path "
            "and why reading it is necessary."
        )


def validate_line_limit(value: int, name: str = "lines", minimum: int = 1, maximum: int = 2000) -> int:
    if not isinstance(value, int) or value < minimum or value > maximum:
        raise RuntimeAccessError(f"{name} must be between {minimum} and {maximum}.")
    return value


def validate_max_bytes(value: int, minimum: int = 1, maximum: int = 1_000_000) -> int:
    if not isinstance(value, int) or value < minimum or value > maximum:
        raise RuntimeAccessError(f"max_bytes must be between {minimum} and {maximum}.")
    return value


def validate_approved_command(command: str) -> str:
    if not isinstance(command, str) or not command.strip():
        raise RuntimeAccessError("command must be a non-empty string.")
    if "\x00" in command or "\r" in command or "\n" in command:
        raise RuntimeAccessError("command must be a single-line command.")
    if len(command) > 2000:
        raise RuntimeAccessError("command is too long for a break-glass tool call.")
    if FORBIDDEN_RAW_COMMAND_RE.search(command):
        raise RuntimeAccessError(
            "Raw database/client commands are forbidden through SSH. Use db-access."
        )
    return command.strip()


def ensure_action_allowed(host: SshHostConfig, action: ActionClass, approved: bool = False) -> None:
    if (
        host.profile == HostProfile.LEGACY_AIRFLOW_FS_ONLY
        and action in {ActionClass.AIRFLOW_READ, ActionClass.AIRFLOW_CONTROL}
    ):
        raise RuntimeAccessError(LEGACY_AIRFLOW_FS_ONLY_MESSAGE)
    if action in DEFAULT_ALLOWED.get(host.profile, set()):
        return
    if approved and action in APPROVAL_ALLOWED.get(host.profile, set()):
        return
    if action in APPROVAL_ALLOWED.get(host.profile, set()):
        raise RuntimeAccessError(
            f"{action.value} on profile {host.profile.value} requires explicit approval. "
            "Call the tool with approved=true only after chat approval names profile, "
            "action, target set, and rollback/cleanup expectation when relevant."
        )
    raise RuntimeAccessError(
        f"{action.value} is not allowed for profile {host.profile.value}."
    )


def ensure_airflow_host(host: SshHostConfig) -> None:
    if host.profile not in AIRFLOW_PROFILES:
        raise RuntimeAccessError(
            f"Host profile {host.profile.value} is not an Airflow profile."
        )


def ensure_legacy_airflow_host(host: SshHostConfig) -> None:
    if host.profile != HostProfile.LEGACY_AIRFLOW_FS_ONLY:
        raise RuntimeAccessError(
            "Legacy Airflow filesystem/log tools are only allowed for profile "
            "legacy_airflow_fs_only. Use Airflow 3 typed tools for airflow_dev, "
            "airflow_prod, and airflow_prod_like profiles."
        )
