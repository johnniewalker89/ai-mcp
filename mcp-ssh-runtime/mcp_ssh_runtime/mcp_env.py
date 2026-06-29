from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
from typing import Optional


class TransportType(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        return [transport.value for transport in cls]


class HostProfile(str, Enum):
    AIRFLOW_DEV = "airflow_dev"
    AIRFLOW_PROD = "airflow_prod"
    AIRFLOW_PROD_LIKE = "airflow_prod_like"
    LEGACY_AIRFLOW_FS_ONLY = "legacy_airflow_fs_only"
    DB_DEV = "db_dev"
    UNKNOWN = "unknown"

    @classmethod
    def values(cls) -> list[str]:
        return [profile.value for profile in cls]


@dataclass(frozen=True)
class SshHostConfig:
    alias: str
    profile: HostProfile


def _as_bool(value: str, default: bool = False) -> bool:
    if value == "":
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _parse_host_profiles(raw: str) -> dict[str, SshHostConfig]:
    if not raw.strip():
        return {}

    pairs: dict[str, str] = {}
    if raw.lstrip().startswith("{"):
        decoded = json.loads(raw)
        if not isinstance(decoded, dict):
            raise ValueError("SSH_RUNTIME_HOST_PROFILES JSON must be an object.")
        pairs = {str(alias): str(profile) for alias, profile in decoded.items()}
    else:
        for item in raw.replace(";", ",").split(","):
            token = item.strip()
            if not token:
                continue
            separator = "=" if "=" in token else ":"
            if separator not in token:
                raise ValueError(
                    "SSH_RUNTIME_HOST_PROFILES entries must use alias=profile."
                )
            alias, profile = token.split(separator, 1)
            pairs[alias.strip()] = profile.strip()

    configs: dict[str, SshHostConfig] = {}
    for alias, profile_name in pairs.items():
        if profile_name not in HostProfile.values():
            valid = ", ".join(HostProfile.values())
            raise ValueError(f"Invalid SSH profile '{profile_name}'. Valid: {valid}.")
        configs[alias] = SshHostConfig(alias=alias, profile=HostProfile(profile_name))
    return configs


class SSHRuntimeConfig:
    """Configuration for SSH runtime and Airflow command wrappers."""

    def __init__(self) -> None:
        self._hosts = _parse_host_profiles(os.getenv("SSH_RUNTIME_HOST_PROFILES", ""))

    @property
    def hosts(self) -> dict[str, SshHostConfig]:
        return dict(self._hosts)

    def get_host(self, alias: str) -> SshHostConfig:
        if alias not in self._hosts:
            known = ", ".join(sorted(self._hosts)) or "<none configured>"
            raise ValueError(f"SSH alias '{alias}' is not configured. Known aliases: {known}.")
        return self._hosts[alias]

    @property
    def ssh_binary(self) -> str:
        return os.getenv("SSH_RUNTIME_SSH_BINARY", "ssh")

    @property
    def connect_timeout(self) -> int:
        return _as_int("SSH_RUNTIME_CONNECT_TIMEOUT", 10)

    @property
    def command_timeout(self) -> int:
        return _as_int("SSH_RUNTIME_COMMAND_TIMEOUT", 120)

    @property
    def max_output_chars(self) -> int:
        return _as_int("SSH_RUNTIME_MAX_OUTPUT_CHARS", 200_000)

    @property
    def airflow_os_user(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_OS_USER", "airflow")

    @property
    def airflow_home(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_HOME", "/opt/airflow")

    @property
    def airflow_workdir(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_WORKDIR", "/opt/airflow/airflow")

    @property
    def airflow_pyenv_root(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_PYENV_ROOT", "/opt/airflow/.pyenv")

    @property
    def airflow_pyenv_version(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_PYENV_VERSION", "airflow_3.12.11")

    @property
    def airflow_path(self) -> str:
        default_path = (
            f"{self.airflow_pyenv_root}/shims:"
            f"{self.airflow_pyenv_root}/bin:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        )
        return os.getenv("SSH_RUNTIME_AIRFLOW_PATH", default_path)

    @property
    def airflow_logs_dir(self) -> str:
        return os.getenv("SSH_RUNTIME_AIRFLOW_LOGS_DIR", "/opt/airflow/airflow/logs")

    @property
    def legacy_airflow_autogen_dir(self) -> str:
        return os.getenv(
            "SSH_RUNTIME_LEGACY_AIRFLOW_AUTOGEN_DIR",
            "/opt/airflow/airflow/dags/autogen",
        )

    @property
    def legacy_airflow_yml_dir(self) -> str:
        return os.getenv("SSH_RUNTIME_LEGACY_AIRFLOW_YML_DIR", "/opt/airflow/airflow/yml")

    @property
    def legacy_airflow_logs_dir(self) -> str:
        return os.getenv("SSH_RUNTIME_LEGACY_AIRFLOW_LOGS_DIR", "/opt/airflow/airflow/logs")


class MCPServerConfig:
    @property
    def server_transport(self) -> str:
        transport = os.getenv("SSH_RUNTIME_MCP_SERVER_TRANSPORT", TransportType.STDIO.value)
        transport = transport.lower()
        if transport not in TransportType.values():
            valid = ", ".join(TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid: {valid}.")
        return transport

    @property
    def bind_host(self) -> str:
        return os.getenv("SSH_RUNTIME_MCP_BIND_HOST", "127.0.0.1")

    @property
    def bind_port(self) -> int:
        return _as_int("SSH_RUNTIME_MCP_BIND_PORT", 8000)

    @property
    def auth_token(self) -> Optional[str]:
        return os.getenv("SSH_RUNTIME_MCP_AUTH_TOKEN")

    @property
    def auth_disabled(self) -> bool:
        return _as_bool(os.getenv("SSH_RUNTIME_MCP_AUTH_DISABLED", "false"))


_CONFIG_INSTANCE: Optional[SSHRuntimeConfig] = None
_MCP_CONFIG_INSTANCE: Optional[MCPServerConfig] = None


def get_config() -> SSHRuntimeConfig:
    global _CONFIG_INSTANCE
    if _CONFIG_INSTANCE is None:
        _CONFIG_INSTANCE = SSHRuntimeConfig()
    return _CONFIG_INSTANCE


def get_mcp_config() -> MCPServerConfig:
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        _MCP_CONFIG_INSTANCE = MCPServerConfig()
    return _MCP_CONFIG_INSTANCE
