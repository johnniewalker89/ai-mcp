from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
INSTANCE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class ConfigurationError(RuntimeError):
    """Raised for fail-closed Metabase MCP configuration errors."""


def _positive_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside its allowed bounds.")
    return value


def _positive_float(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = os.getenv(name)
    try:
        value = float(raw) if raw is not None else default
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a number.") from exc
    if not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} is outside its allowed bounds.")
    return value


def _boolean(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().casefold()
    if value not in {"true", "false"}:
        raise ConfigurationError(f"{name} must be true or false.")
    return value == "true"


def _version_prefixes() -> tuple[str, ...]:
    raw = os.getenv("METABASE_MCP_SUPPORTED_VERSION_PREFIXES", "v0.63.,0.63.")
    values = tuple(dict.fromkeys(item.strip() for item in raw.split(",") if item.strip()))
    if not values or any(
        len(value) > 32 or any(char.isspace() for char in value) for value in values
    ):
        raise ConfigurationError("METABASE_MCP_SUPPORTED_VERSION_PREFIXES is invalid.")
    return values


@dataclass(frozen=True)
class MetabaseConfig:
    instance: str
    base_url: str
    api_key: str = field(repr=False)
    audit_dir: Path
    source_revision: str
    supported_version_prefixes: tuple[str, ...]
    expected_user_id: int | None = None
    max_json_bytes: int = 4_000_000
    max_query_bytes: int = 4_000_000
    default_query_rows: int = 100
    max_query_rows: int = 200
    max_list_items: int = 200
    max_batch_items: int = 50
    plan_ttl_seconds: int = 300
    max_active_plans: int = 100
    max_plan_bytes: int = 8_000_000
    edit_session_ttl_seconds: int = 900
    edit_session_max_actions: int = 20
    max_active_edit_sessions: int = 20
    request_timeout_seconds: float = 30.0
    read_attempts: int = 2

    @property
    def origin(self) -> str:
        return self.base_url

    @property
    def host(self) -> str:
        return str(urlsplit(self.base_url).hostname)

    @property
    def credential_fingerprint(self) -> str:
        material = f"{self.base_url}\0{self.api_key}".encode()
        return hashlib.sha256(material).hexdigest()[:16]

    @classmethod
    def from_env(cls) -> MetabaseConfig:
        instance = os.getenv("METABASE_MCP_INSTANCE", "metabase_work")
        if not INSTANCE_RE.fullmatch(instance):
            raise ConfigurationError("METABASE_MCP_INSTANCE is invalid.")

        base_url = os.getenv("METABASE_BASE_URL", "").rstrip("/")
        parsed = urlsplit(base_url)
        allow_local_http = _boolean("METABASE_MCP_ALLOW_HTTP_LOCALHOST")
        local_host = (parsed.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
        scheme_allowed = parsed.scheme.casefold() == "https" or (
            allow_local_http and local_host and parsed.scheme.casefold() == "http"
        )
        if (
            not scheme_allowed
            or not parsed.hostname
            or not parsed.hostname.isascii()
            or (not HOST_RE.fullmatch(parsed.hostname) and parsed.hostname.casefold() != "::1")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ConfigurationError("METABASE_BASE_URL must be one exact HTTPS origin.")

        api_key = os.getenv("METABASE_API_KEY", "")
        if (
            not 20 <= len(api_key) <= 4_096
            or api_key != api_key.strip()
            or any(character.isspace() or ord(character) < 32 for character in api_key)
        ):
            raise ConfigurationError("METABASE_API_KEY is missing or malformed.")

        expected_user_id_raw = os.getenv("METABASE_MCP_EXPECTED_USER_ID")
        expected_user_id: int | None = None
        if expected_user_id_raw is not None:
            try:
                expected_user_id = int(expected_user_id_raw)
            except ValueError as exc:
                raise ConfigurationError(
                    "METABASE_MCP_EXPECTED_USER_ID must be an integer."
                ) from exc
            if expected_user_id <= 0:
                raise ConfigurationError("METABASE_MCP_EXPECTED_USER_ID must be positive.")

        default_rows = _positive_int("METABASE_MCP_DEFAULT_QUERY_ROWS", 100, 1, 200)
        max_rows = _positive_int("METABASE_MCP_MAX_QUERY_ROWS", 200, 1, 200)
        if default_rows > max_rows:
            raise ConfigurationError("Default query rows cannot exceed the maximum.")

        return cls(
            instance=instance,
            base_url=base_url,
            api_key=api_key,
            audit_dir=Path(
                os.getenv(
                    "METABASE_MCP_AUDIT_DIR",
                    str(Path.home() / ".codex" / "metabase-mcp-audit"),
                )
            ).resolve(),
            source_revision=os.getenv("METABASE_MCP_SOURCE_REVISION", "unreported"),
            supported_version_prefixes=_version_prefixes(),
            expected_user_id=expected_user_id,
            max_json_bytes=_positive_int(
                "METABASE_MCP_MAX_JSON_BYTES", 4_000_000, 64_000, 16_000_000
            ),
            max_query_bytes=_positive_int(
                "METABASE_MCP_MAX_QUERY_BYTES", 4_000_000, 64_000, 16_000_000
            ),
            default_query_rows=default_rows,
            max_query_rows=max_rows,
            max_list_items=_positive_int("METABASE_MCP_MAX_LIST_ITEMS", 200, 1, 500),
            max_batch_items=_positive_int("METABASE_MCP_MAX_BATCH_ITEMS", 50, 1, 100),
            plan_ttl_seconds=_positive_int("METABASE_MCP_PLAN_TTL_SECONDS", 300, 30, 900),
            max_active_plans=_positive_int("METABASE_MCP_MAX_ACTIVE_PLANS", 100, 1, 1_000),
            max_plan_bytes=_positive_int(
                "METABASE_MCP_MAX_PLAN_BYTES", 8_000_000, 64_000, 32_000_000
            ),
            edit_session_ttl_seconds=_positive_int(
                "METABASE_MCP_EDIT_SESSION_TTL_SECONDS", 900, 60, 3_600
            ),
            edit_session_max_actions=_positive_int(
                "METABASE_MCP_EDIT_SESSION_MAX_ACTIONS", 20, 1, 100
            ),
            max_active_edit_sessions=_positive_int(
                "METABASE_MCP_MAX_ACTIVE_EDIT_SESSIONS", 20, 1, 100
            ),
            request_timeout_seconds=_positive_float(
                "METABASE_MCP_REQUEST_TIMEOUT_SECONDS", 30.0, 1.0, 120.0
            ),
            read_attempts=_positive_int("METABASE_MCP_READ_ATTEMPTS", 2, 1, 3),
        )

    def version_supported(self, version: str | None) -> bool:
        return bool(version) and any(
            str(version).startswith(prefix) for prefix in self.supported_version_prefixes
        )
