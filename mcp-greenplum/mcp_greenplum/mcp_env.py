"""
Environment configuration for the MCP Greenplum server.

This module centralizes all configuration in a typed way and provides sensible
defaults. All values are read from environment variables.
"""

from __future__ import annotations

from enum import Enum
import os
from typing import Optional


class TransportType(str, Enum):
    """Supported MCP server transport types."""

    STDIO = "stdio"
    HTTP = "http"
    SSE = "sse"

    @classmethod
    def values(cls) -> list[str]:
        return [transport.value for transport in cls]


class GreenplumConfig:
    """Configuration for Greenplum connection.

    Required env vars (when enabled):
      - GREENPLUM_HOST
      - GREENPLUM_PORT
      - GREENPLUM_USER
      - GREENPLUM_PASSWORD

    Optional env vars:
      - GREENPLUM_DATABASE (default: "postgres")
      - GREENPLUM_SSLMODE (default: "prefer")
      - GREENPLUM_CONNECT_TIMEOUT (default: 30)

    Safety env vars:
      - GREENPLUM_ALLOW_WRITE_ACCESS (default: false)
      - GREENPLUM_ALLOW_DROP (default: false)
    """

    def __init__(self) -> None:
        if self.enabled:
            self._validate_required_vars()

    @property
    def enabled(self) -> bool:
        return os.getenv("GREENPLUM_ENABLED", "true").lower() == "true"

    @property
    def host(self) -> str:
        return os.environ["GREENPLUM_HOST"]

    @property
    def port(self) -> int:
        return int(os.getenv("GREENPLUM_PORT", "5432"))

    @property
    def user(self) -> str:
        return os.environ["GREENPLUM_USER"]

    @property
    def password(self) -> str:
        return os.environ["GREENPLUM_PASSWORD"]

    @property
    def database(self) -> str:
        return os.getenv("GREENPLUM_DATABASE", "postgres")

    @property
    def sslmode(self) -> str:
        return os.getenv("GREENPLUM_SSLMODE", "prefer")

    @property
    def connect_timeout(self) -> int:
        return int(os.getenv("GREENPLUM_CONNECT_TIMEOUT", "300"))

    @property
    def allow_write_access(self) -> bool:
        return os.getenv("GREENPLUM_ALLOW_WRITE_ACCESS", "false").lower() == "true"

    @property
    def allow_drop(self) -> bool:
        return os.getenv("GREENPLUM_ALLOW_DROP", "false").lower() == "true"

    def _validate_required_vars(self) -> None:
        missing: list[str] = []
        for var in ["GREENPLUM_HOST", "GREENPLUM_USER", "GREENPLUM_PASSWORD"]:
            if var not in os.environ:
                missing.append(var)
        if missing:
            raise ValueError(
                f"Missing required environment variables: {', '.join(missing)}"
            )


class MCPServerConfig:
    """Configuration for MCP server-level settings."""

    @property
    def server_transport(self) -> str:
        transport = os.getenv(
            "GREENPLUM_MCP_SERVER_TRANSPORT", TransportType.STDIO.value
        ).lower()
        if transport not in TransportType.values():
            valid_options = ", ".join(f'"{t}"' for t in TransportType.values())
            raise ValueError(
                f"Invalid transport '{transport}'. Valid options: {valid_options}"
            )
        return transport

    @property
    def bind_host(self) -> str:
        return os.getenv("GREENPLUM_MCP_BIND_HOST", "127.0.0.1")

    @property
    def bind_port(self) -> int:
        return int(os.getenv("GREENPLUM_MCP_BIND_PORT", "8000"))

    @property
    def query_timeout(self) -> int:
        return int(os.getenv("GREENPLUM_MCP_QUERY_TIMEOUT", "300"))

    @property
    def auth_token(self) -> Optional[str]:
        return os.getenv("GREENPLUM_MCP_AUTH_TOKEN", None)

    @property
    def auth_disabled(self) -> bool:
        return os.getenv("GREENPLUM_MCP_AUTH_DISABLED", "false").lower() == "true"


_GREENPLUM_CONFIG_INSTANCE: Optional[GreenplumConfig] = None
_MCP_CONFIG_INSTANCE: Optional[MCPServerConfig] = None


def get_config() -> GreenplumConfig:
    global _GREENPLUM_CONFIG_INSTANCE
    if _GREENPLUM_CONFIG_INSTANCE is None:
        _GREENPLUM_CONFIG_INSTANCE = GreenplumConfig()
    return _GREENPLUM_CONFIG_INSTANCE


def get_mcp_config() -> MCPServerConfig:
    global _MCP_CONFIG_INSTANCE
    if _MCP_CONFIG_INSTANCE is None:
        _MCP_CONFIG_INSTANCE = MCPServerConfig()
    return _MCP_CONFIG_INSTANCE

