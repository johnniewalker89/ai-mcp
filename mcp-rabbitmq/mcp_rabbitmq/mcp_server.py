from __future__ import annotations

import base64
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


MCP_SERVER_NAME = "mcp-rabbitmq"
DEFAULT_ENV_FILE = Path.home() / ".codex" / "rabbitmq-mcp.env"
DEFAULT_TIMEOUT_SEC = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv(os.environ.get("RABBITMQ_MCP_ENV_FILE", DEFAULT_ENV_FILE))

mcp = FastMCP(name=MCP_SERVER_NAME)


@dataclass(frozen=True)
class BrokerConfig:
    alias: str
    profile: str
    url: str
    username: str | None
    password: str | None

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


def _env_name(alias: str, key: str) -> str:
    normalized = alias.upper().replace("-", "_")
    return f"RABBITMQ_MCP_{normalized}_{key}"


def _configured_brokers() -> dict[str, BrokerConfig]:
    aliases = [
        alias.strip()
        for alias in os.environ.get("RABBITMQ_MCP_ALIASES", "dev,prod").split(",")
        if alias.strip()
    ]

    brokers: dict[str, BrokerConfig] = {}
    for alias in aliases:
        url = os.environ.get(_env_name(alias, "URL"))
        if not url:
            continue
        brokers[alias] = BrokerConfig(
            alias=alias,
            profile=os.environ.get(_env_name(alias, "PROFILE"), alias),
            url=url.rstrip("/"),
            username=os.environ.get(_env_name(alias, "USERNAME")),
            password=os.environ.get(_env_name(alias, "PASSWORD")),
        )
    return brokers


def _broker(alias: str) -> BrokerConfig:
    brokers = _configured_brokers()
    if alias not in brokers:
        known = ", ".join(sorted(brokers)) or "<none>"
        raise ToolError(f"Unknown RabbitMQ alias {alias!r}. Known aliases: {known}.")
    broker = brokers[alias]
    if not broker.has_credentials:
        raise ToolError(
            f"RabbitMQ alias {alias!r} is configured without credentials. "
            "Set username/password in the local env file."
        )
    return broker


def _api_request(
    alias: str,
    method: str,
    path: str,
    query: str | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    broker = _broker(alias)
    url = f"{broker.url}/api/{path.lstrip('/')}"
    if query:
        url = f"{url}?{query}"

    credentials = f"{broker.username}:{broker.password}".encode("utf-8")
    auth_header = base64.b64encode(credentials).decode("ascii")
    data = None
    headers = {
        "Authorization": f"Basic {auth_header}",
        "Accept": "application/json",
        "User-Agent": "mcp-rabbitmq",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolError(
            f"RabbitMQ API {method} {url} failed: HTTP {exc.code}: {detail}"
        ) from exc
    except URLError as exc:
        raise ToolError(f"RabbitMQ API {method} {url} failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(f"RabbitMQ API {method} {url} returned invalid JSON: {exc}") from exc


def _api_get(alias: str, path: str, query: str | None = None) -> Any:
    return _api_request(alias, "GET", path, query=query)


def _require_approval(approved: bool, action: str) -> None:
    if not approved:
        raise ToolError(
            f"{action} is a RabbitMQ state-changing action. "
            "Get explicit approval and call the tool with approved=true."
        )


def _vhost_path(vhost: str) -> str:
    return quote(vhost, safe="")


def _limit_rows(rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    return rows[: max(0, min(limit, 1000))]


def _filter_contains(rows: list[dict[str, Any]], field: str, contains: str | None) -> list[dict[str, Any]]:
    if not contains:
        return rows
    needle = contains.lower()
    return [row for row in rows if needle in str(row.get(field, "")).lower()]


@mcp.tool()
def list_configured_brokers() -> dict[str, object]:
    """List configured RabbitMQ aliases without exposing credentials."""

    brokers = _configured_brokers()
    return {
        "env_file": str(os.environ.get("RABBITMQ_MCP_ENV_FILE", DEFAULT_ENV_FILE)),
        "brokers": [
            {
                "alias": broker.alias,
                "profile": broker.profile,
                "url": broker.url,
                "has_credentials": broker.has_credentials,
            }
            for broker in brokers.values()
        ],
    }


@mcp.tool()
def list_exchanges(alias: str = "dev", vhost: str = "/", name_contains: str = "", limit: int = 200) -> dict[str, object]:
    """List exchanges from RabbitMQ Management API."""

    rows = _api_get(alias, f"exchanges/{_vhost_path(vhost)}")
    filtered = _filter_contains(rows, "name", name_contains)
    selected = _limit_rows(filtered, limit)
    return {
        "alias": alias,
        "vhost": vhost,
        "returned": len(selected),
        "total_after_filter": len(filtered),
        "exchanges": [
            {
                "name": row.get("name"),
                "type": row.get("type"),
                "durable": row.get("durable"),
                "auto_delete": row.get("auto_delete"),
                "internal": row.get("internal"),
            }
            for row in selected
        ],
    }


@mcp.tool()
def list_queues(alias: str = "dev", vhost: str = "/", name_contains: str = "", limit: int = 200) -> dict[str, object]:
    """List queues from RabbitMQ Management API."""

    rows = _api_get(alias, f"queues/{_vhost_path(vhost)}")
    filtered = _filter_contains(rows, "name", name_contains)
    selected = _limit_rows(filtered, limit)
    return {
        "alias": alias,
        "vhost": vhost,
        "returned": len(selected),
        "total_after_filter": len(filtered),
        "queues": [
            {
                "name": row.get("name"),
                "durable": row.get("durable"),
                "state": row.get("state"),
                "messages": row.get("messages"),
                "messages_ready": row.get("messages_ready"),
                "messages_unacknowledged": row.get("messages_unacknowledged"),
                "consumers": row.get("consumers"),
            }
            for row in selected
        ],
    }


@mcp.tool()
def get_queue(alias: str = "dev", vhost: str = "/", queue: str = "") -> dict[str, object]:
    """Get one queue details without reading messages."""

    if not queue:
        raise ToolError("queue is required")
    row = _api_get(alias, f"queues/{_vhost_path(vhost)}/{quote(queue, safe='')}")
    return {
        "alias": alias,
        "vhost": vhost,
        "queue": {
            "name": row.get("name"),
            "durable": row.get("durable"),
            "state": row.get("state"),
            "messages": row.get("messages"),
            "messages_ready": row.get("messages_ready"),
            "messages_unacknowledged": row.get("messages_unacknowledged"),
            "consumers": row.get("consumers"),
            "consumer_utilisation": row.get("consumer_utilisation"),
            "memory": row.get("memory"),
            "message_bytes": row.get("message_bytes"),
        },
    }


@mcp.tool()
def list_bindings(
    alias: str = "dev",
    vhost: str = "/",
    source: str = "",
    destination: str = "",
    routing_key: str = "",
    limit: int = 500,
) -> dict[str, object]:
    """List bindings and optionally filter by source/destination/routing key."""

    rows = _api_get(alias, f"bindings/{_vhost_path(vhost)}")
    filtered = rows
    if source:
        filtered = [row for row in filtered if row.get("source") == source]
    if destination:
        filtered = [row for row in filtered if row.get("destination") == destination]
    if routing_key:
        filtered = [row for row in filtered if row.get("routing_key") == routing_key]

    selected = _limit_rows(filtered, limit)
    return {
        "alias": alias,
        "vhost": vhost,
        "returned": len(selected),
        "total_after_filter": len(filtered),
        "bindings": [
            {
                "source": row.get("source"),
                "destination": row.get("destination"),
                "destination_type": row.get("destination_type"),
                "routing_key": row.get("routing_key"),
            }
            for row in selected
        ],
    }


@mcp.tool()
def list_consumers(alias: str = "dev", vhost: str = "/", queue: str = "", limit: int = 500) -> dict[str, object]:
    """List consumers and optionally filter by queue."""

    rows = _api_get(alias, f"consumers/{_vhost_path(vhost)}")
    filtered = rows
    if queue:
        filtered = [row for row in filtered if row.get("queue", {}).get("name") == queue]
    selected = _limit_rows(filtered, limit)
    return {
        "alias": alias,
        "vhost": vhost,
        "returned": len(selected),
        "total_after_filter": len(filtered),
        "consumers": [
            {
                "queue": row.get("queue", {}).get("name"),
                "consumer_tag": row.get("consumer_tag"),
                "channel_details": row.get("channel_details", {}),
                "ack_required": row.get("ack_required"),
                "prefetch_count": row.get("prefetch_count"),
            }
            for row in selected
        ],
    }


@mcp.tool()
def inspect_flow(
    alias: str = "dev",
    vhost: str = "/",
    exchange: str = "APPSFLYER_SKAN_DEV",
    queue: str = "appsflyer_skan_push",
    routing_key: str = "skan",
) -> dict[str, object]:
    """Check whether exchange, queue, binding, and consumers exist for a flow."""

    exchanges = list_exchanges(alias=alias, vhost=vhost, name_contains=exchange, limit=1000)[
        "exchanges"
    ]
    queues = list_queues(alias=alias, vhost=vhost, name_contains=queue, limit=1000)["queues"]
    bindings = list_bindings(
        alias=alias,
        vhost=vhost,
        source=exchange,
        destination=queue,
        routing_key=routing_key,
        limit=1000,
    )["bindings"]
    consumers = list_consumers(alias=alias, vhost=vhost, queue=queue, limit=1000)["consumers"]

    exchange_exists = any(row.get("name") == exchange for row in exchanges)
    queue_exists = any(row.get("name") == queue for row in queues)
    binding_exists = any(
        row.get("source") == exchange
        and row.get("destination") == queue
        and row.get("routing_key") == routing_key
        for row in bindings
    )

    return {
        "alias": alias,
        "vhost": vhost,
        "exchange": exchange,
        "queue": queue,
        "routing_key": routing_key,
        "exchange_exists": exchange_exists,
        "queue_exists": queue_exists,
        "binding_exists": binding_exists,
        "consumer_count": len(consumers),
        "ready": exchange_exists and queue_exists and binding_exists,
    }


@mcp.tool()
def declare_exchange(
    alias: str = "dev",
    vhost: str = "/",
    exchange: str = "",
    exchange_type: str = "topic",
    durable: bool = True,
    auto_delete: bool = False,
    internal: bool = False,
    approved: bool = False,
) -> dict[str, object]:
    """Declare an exchange. State-changing; requires approved=true."""

    _require_approval(approved, "declare_exchange")
    if not exchange:
        raise ToolError("exchange is required")
    payload = {
        "type": exchange_type,
        "durable": durable,
        "auto_delete": auto_delete,
        "internal": internal,
        "arguments": {},
    }
    _api_request(alias, "PUT", f"exchanges/{_vhost_path(vhost)}/{quote(exchange, safe='')}", payload=payload)
    return {
        "alias": alias,
        "vhost": vhost,
        "exchange": exchange,
        "type": exchange_type,
        "durable": durable,
        "declared": True,
    }


@mcp.tool()
def declare_queue(
    alias: str = "dev",
    vhost: str = "/",
    queue: str = "",
    durable: bool = True,
    auto_delete: bool = False,
    approved: bool = False,
) -> dict[str, object]:
    """Declare a queue. State-changing; requires approved=true."""

    _require_approval(approved, "declare_queue")
    if not queue:
        raise ToolError("queue is required")
    payload = {
        "durable": durable,
        "auto_delete": auto_delete,
        "arguments": {},
    }
    _api_request(alias, "PUT", f"queues/{_vhost_path(vhost)}/{quote(queue, safe='')}", payload=payload)
    return {
        "alias": alias,
        "vhost": vhost,
        "queue": queue,
        "durable": durable,
        "declared": True,
    }


@mcp.tool()
def bind_queue(
    alias: str = "dev",
    vhost: str = "/",
    exchange: str = "",
    queue: str = "",
    routing_key: str = "",
    approved: bool = False,
) -> dict[str, object]:
    """Bind a queue to an exchange. State-changing; requires approved=true."""

    _require_approval(approved, "bind_queue")
    if not exchange:
        raise ToolError("exchange is required")
    if not queue:
        raise ToolError("queue is required")
    payload = {
        "routing_key": routing_key,
        "arguments": {},
    }
    _api_request(
        alias,
        "POST",
        f"bindings/{_vhost_path(vhost)}/e/{quote(exchange, safe='')}/q/{quote(queue, safe='')}",
        payload=payload,
    )
    return {
        "alias": alias,
        "vhost": vhost,
        "exchange": exchange,
        "queue": queue,
        "routing_key": routing_key,
        "bound": True,
    }


@mcp.tool()
def purge_queue(
    alias: str = "dev",
    vhost: str = "/",
    queue: str = "",
    approved: bool = False,
) -> dict[str, object]:
    """Purge queue messages. Destructive; requires approved=true."""

    _require_approval(approved, "purge_queue")
    if not queue:
        raise ToolError("queue is required")
    _api_request(alias, "DELETE", f"queues/{_vhost_path(vhost)}/{quote(queue, safe='')}/contents")
    return {
        "alias": alias,
        "vhost": vhost,
        "queue": queue,
        "purged": True,
    }


@mcp.tool()
def delete_queue(
    alias: str = "dev",
    vhost: str = "/",
    queue: str = "",
    if_empty: bool = True,
    if_unused: bool = True,
    approved: bool = False,
) -> dict[str, object]:
    """Delete a queue. Destructive; requires approved=true."""

    _require_approval(approved, "delete_queue")
    if not queue:
        raise ToolError("queue is required")
    query = f"if-empty={'true' if if_empty else 'false'}&if-unused={'true' if if_unused else 'false'}"
    _api_request(
        alias,
        "DELETE",
        f"queues/{_vhost_path(vhost)}/{quote(queue, safe='')}",
        query=query,
    )
    return {
        "alias": alias,
        "vhost": vhost,
        "queue": queue,
        "deleted": True,
        "if_empty": if_empty,
        "if_unused": if_unused,
    }
