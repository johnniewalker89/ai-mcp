from __future__ import annotations

from enum import Enum
import os
from typing import Optional


class TransportType(str, Enum):
    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        return [transport.value for transport in cls]


def _bool_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() == "true"


class ClickHouseConfig:
    def __init__(self) -> None:
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        return _bool_env("CLICKHOUSE_ENABLED", "true")

    @property
    def host(self) -> str:
        return os.environ["CLICKHOUSE_HOST"]

    @property
    def port(self) -> int:
        return int(os.getenv("CLICKHOUSE_PORT", "8123"))

    @property
    def user(self) -> str:
        return os.environ["CLICKHOUSE_USER"]

    @property
    def password(self) -> str:
        return os.environ["CLICKHOUSE_PASSWORD"]

    @property
    def database(self) -> str:
        return os.getenv("CLICKHOUSE_DATABASE", "default")

    @property
    def secure(self) -> bool:
        return _bool_env("CLICKHOUSE_SECURE", "false")

    @property
    def verify(self) -> bool:
        return _bool_env("CLICKHOUSE_VERIFY", "true")

    @property
    def connect_timeout(self) -> int:
        return int(os.getenv("CLICKHOUSE_CONNECT_TIMEOUT", "30"))

    @property
    def send_receive_timeout(self) -> int:
        return int(os.getenv("CLICKHOUSE_MCP_QUERY_TIMEOUT", "300"))

    @property
    def allow_write_access(self) -> bool:
        return _bool_env("CLICKHOUSE_ALLOW_WRITE_ACCESS", "false")

    @property
    def allow_drop(self) -> bool:
        return _bool_env("CLICKHOUSE_ALLOW_DROP", "false")

    def _validate_required_vars(self) -> None:
        missing = [
            name
            for name in ["CLICKHOUSE_HOST", "CLICKHOUSE_USER", "CLICKHOUSE_PASSWORD"]
            if name not in os.environ
        ]
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")


class MCPServerConfig:
    @property
    def server_transport(self) -> str:
        transport = os.getenv("CLICKHOUSE_MCP_SERVER_TRANSPORT", TransportType.STDIO.value).lower()
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(f"Invalid transport '{transport}'. Valid options: {valid_options}")
        return transport

    @property
    def bind_host(self) -> str:
        return os.getenv("CLICKHOUSE_MCP_BIND_HOST", "127.0.0.1")

    @property
    def bind_port(self) -> int:
        return int(os.getenv("CLICKHOUSE_MCP_BIND_PORT", "8001"))

    @property
    def auth_token(self) -> Optional[str]:
        return os.getenv("CLICKHOUSE_MCP_AUTH_TOKEN")

    @property
    def auth_disabled(self) -> bool:
        return _bool_env("CLICKHOUSE_MCP_AUTH_DISABLED", "false")


_CLICKHOUSE_CONFIG_INSTANCE: Optional[ClickHouseConfig] = None
_MCP_CONFIG_INSTANCE: Optional[MCPServerConfig] = None


def get_config() -> ClickHouseConfig:
    global _CLICKHOUSE_CONFIG_INSTANCE
    if _CLICKHOUSE_CONFIG_INSTANCE is None:
        _CLICKHOUSE_CONFIG_INSTANCE = ClickHouseConfig()
    return _CLICKHOUSE_CONFIG_INSTANCE


def get_mcp_config() -> MCPServerConfig:
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        _MCP_CONFIG_INSTANCE = MCPServerConfig()
    return _MCP_CONFIG_INSTANCE
