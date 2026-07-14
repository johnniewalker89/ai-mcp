from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError


MCP_SERVER_NAME = "mcp_discord"
DISCORD_API_BASE = "https://discord.com/api/v10"
DEFAULT_ENV_FILE = Path.home() / ".codex" / "discord-mcp.env"
DEFAULT_TIMEOUT_SEC = 20
TEXT_LIKE_CHANNEL_TYPES = {0, 5, 10, 11, 12, 15}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(MCP_SERVER_NAME)

load_dotenv(os.environ.get("DISCORD_MCP_ENV_FILE", DEFAULT_ENV_FILE))

mcp = FastMCP(name=MCP_SERVER_NAME)


def _csv_set(value: str | None) -> set[str]:
    return {item.strip() for item in (value or "").split(",") if item.strip()}


def _allowed_guild_ids() -> set[str]:
    return _csv_set(os.environ.get("DISCORD_MCP_ALLOWED_GUILD_IDS"))


def _allowed_channel_ids() -> set[str]:
    return _csv_set(os.environ.get("DISCORD_MCP_ALLOWED_CHANNEL_IDS"))


def _release_channel_ids() -> set[str]:
    return _csv_set(os.environ.get("DISCORD_MCP_RELEASE_CHANNEL_IDS"))


def _token() -> str:
    token = os.environ.get("DISCORD_MCP_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        raise ToolError(
            "Discord bot token is not configured. Set DISCORD_MCP_BOT_TOKEN "
            "in the local env file or MCP server env."
        )
    return token


def _require_guild_allowed(guild_id: str) -> None:
    allowed = _allowed_guild_ids()
    if allowed and guild_id not in allowed:
        raise ToolError(f"Guild {guild_id!r} is not allowed by DISCORD_MCP_ALLOWED_GUILD_IDS.")


def _require_channel_allowed(channel_id: str) -> None:
    allowed = _allowed_channel_ids()
    if allowed and channel_id not in allowed:
        raise ToolError(f"Channel {channel_id!r} is not allowed by DISCORD_MCP_ALLOWED_CHANNEL_IDS.")


def _require_approval(approved: bool, action: str) -> None:
    if not approved:
        raise ToolError(
            f"{action} changes Discord server state. Get explicit approval and call "
            "the tool with approved=true."
        )


def _clean_text(value: str, field: str, max_length: int) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ToolError(f"{field} is required")
    if len(cleaned) > max_length:
        raise ToolError(f"{field} is too long; keep it under {max_length} characters")
    return cleaned


def _discord_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
) -> Any:
    url = f"{DISCORD_API_BASE}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"

    data = None
    headers = {
        "Authorization": f"Bot {_token()}",
        "Accept": "application/json",
        "User-Agent": "mcp_discord/0.1",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)

    try:
        with urlopen(request, timeout=DEFAULT_TIMEOUT_SEC) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else None
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ToolError(f"Discord API {method} {path} failed: HTTP {exc.code}: {detail}") from exc
    except URLError as exc:
        raise ToolError(f"Discord API {method} {path} failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise ToolError(f"Discord API {method} {path} returned invalid JSON: {exc}") from exc


def _channel_view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "type": row.get("type"),
        "parent_id": row.get("parent_id"),
    }


def _message_view(row: dict[str, Any]) -> dict[str, Any]:
    author = row.get("author") or {}
    return {
        "id": row.get("id"),
        "channel_id": row.get("channel_id"),
        "timestamp": row.get("timestamp"),
        "author": {
            "id": author.get("id"),
            "username": author.get("username"),
            "global_name": author.get("global_name"),
            "bot": author.get("bot", False),
        },
        "content": row.get("content") or "",
    }


def _current_bot_user_id() -> str:
    row = _discord_request("GET", "/users/@me")
    bot_user_id = row.get("id")
    if not bot_user_id:
        raise ToolError("Discord API did not return bot user id.")
    return str(bot_user_id)


def _message(channel_id: str, message_id: str) -> dict[str, Any]:
    return _discord_request("GET", f"/channels/{channel_id}/messages/{message_id}")


def _require_own_message(channel_id: str, message_id: str) -> None:
    row = _message(channel_id, message_id)
    author = row.get("author") or {}
    if str(author.get("id") or "") != _current_bot_user_id():
        raise ToolError("Refusing to edit/delete a message that was not authored by this bot.")


@mcp.tool()
def list_allowed_scope() -> dict[str, object]:
    """Show Discord MCP local scope without exposing the bot token."""

    token_present = bool(os.environ.get("DISCORD_MCP_BOT_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN"))
    return {
        "env_file": str(os.environ.get("DISCORD_MCP_ENV_FILE", DEFAULT_ENV_FILE)),
        "token_present": token_present,
        "allowed_guild_ids": sorted(_allowed_guild_ids()),
        "allowed_channel_ids": sorted(_allowed_channel_ids()),
        "release_channel_ids": sorted(_release_channel_ids()),
    }


@mcp.tool()
def list_channels(guild_id: str) -> dict[str, object]:
    """List text-like channels for an allowed Discord guild."""

    _require_guild_allowed(guild_id)
    rows = _discord_request("GET", f"/guilds/{guild_id}/channels")
    channels = [
        _channel_view(row)
        for row in rows
        if row.get("type") in TEXT_LIKE_CHANNEL_TYPES
    ]
    channels.sort(key=lambda row: str(row.get("name") or ""))
    return {
        "guild_id": guild_id,
        "returned": len(channels),
        "channels": channels,
    }


@mcp.tool()
def get_recent_messages(channel_id: str, limit: int = 10) -> dict[str, object]:
    """Read recent messages from an allowed Discord channel."""

    _require_channel_allowed(channel_id)
    bounded_limit = max(1, min(limit, 25))
    rows = _discord_request("GET", f"/channels/{channel_id}/messages", query={"limit": bounded_limit})
    messages = [_message_view(row) for row in reversed(rows)]
    return {
        "channel_id": channel_id,
        "returned": len(messages),
        "messages": messages,
    }


@mcp.tool()
def send_message(channel_id: str, content: str) -> dict[str, object]:
    """Send a plain text message to an allowed Discord channel."""

    _require_channel_allowed(channel_id)
    text = _clean_text(content, "content", 1900)

    row = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        payload={
            "content": text,
            "allowed_mentions": {"parse": []},
        },
    )
    return {
        "channel_id": channel_id,
        "message_id": row.get("id"),
        "sent": True,
    }


@mcp.tool()
def send_embed(
    channel_id: str,
    title: str,
    description: str,
    url: str = "",
    color: int = 3447003,
    fields_json: str = "[]",
    content: str = "",
) -> dict[str, object]:
    """Send a simple embed announcement to an allowed Discord channel."""

    _require_channel_allowed(channel_id)
    embed_title = title.strip()
    embed_description = description.strip()
    if not embed_title:
        raise ToolError("title is required")
    if not embed_description:
        raise ToolError("description is required")
    if len(embed_title) > 256:
        raise ToolError("title is too long; keep it under 256 characters")
    if len(embed_description) > 3900:
        raise ToolError("description is too long; keep it under 3900 characters")
    if color < 0 or color > 16777215:
        raise ToolError("color must be a decimal RGB value from 0 to 16777215")

    try:
        fields = json.loads(fields_json)
    except json.JSONDecodeError as exc:
        raise ToolError(f"fields_json must be a JSON array: {exc}") from exc

    if not isinstance(fields, list):
        raise ToolError("fields_json must be a JSON array")
    if len(fields) > 10:
        raise ToolError("fields_json supports up to 10 fields")
    for index, field in enumerate(fields):
        if not isinstance(field, dict):
            raise ToolError(f"fields_json item {index} must be an object")
        if not str(field.get("name") or "").strip():
            raise ToolError(f"fields_json item {index} is missing name")
        if not str(field.get("value") or "").strip():
            raise ToolError(f"fields_json item {index} is missing value")

    embed: dict[str, Any] = {
        "title": embed_title,
        "description": embed_description,
        "color": color,
        "fields": fields,
    }
    if url.strip():
        embed["url"] = url.strip()

    payload: dict[str, Any] = {
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    if content.strip():
        payload["content"] = _clean_text(content, "content", 1900)

    row = _discord_request(
        "POST",
        f"/channels/{channel_id}/messages",
        payload=payload,
    )
    return {
        "channel_id": channel_id,
        "message_id": row.get("id"),
        "sent": True,
    }


@mcp.tool()
def create_text_channel(
    guild_id: str,
    name: str,
    topic: str = "",
    category_id: str = "",
    approved: bool = False,
) -> dict[str, object]:
    """Create a text channel in an allowed guild. State-changing; requires approved=true."""

    _require_approval(approved, "create_text_channel")
    _require_guild_allowed(guild_id)
    channel_name = _clean_text(name, "name", 100)

    payload: dict[str, Any] = {
        "name": channel_name,
        "type": 0,
    }
    if topic.strip():
        payload["topic"] = _clean_text(topic, "topic", 1024)
    if category_id.strip():
        payload["parent_id"] = category_id.strip()

    row = _discord_request("POST", f"/guilds/{guild_id}/channels", payload=payload)
    return {
        "guild_id": guild_id,
        "channel": _channel_view(row),
        "created": True,
        "next_step": "Add the new channel id to DISCORD_MCP_ALLOWED_CHANNEL_IDS before posting there.",
    }


@mcp.tool()
def update_channel(
    channel_id: str,
    name: str = "",
    topic: str = "",
    approved: bool = False,
) -> dict[str, object]:
    """Update name and/or topic for an allowed channel. State-changing; requires approved=true."""

    _require_approval(approved, "update_channel")
    _require_channel_allowed(channel_id)

    payload: dict[str, Any] = {}
    if name.strip():
        payload["name"] = _clean_text(name, "name", 100)
    if topic.strip():
        payload["topic"] = _clean_text(topic, "topic", 1024)
    if not payload:
        raise ToolError("Provide name and/or topic.")

    row = _discord_request("PATCH", f"/channels/{channel_id}", payload=payload)
    return {
        "channel": _channel_view(row),
        "updated": True,
    }


@mcp.tool()
def pin_message(channel_id: str, message_id: str, approved: bool = False) -> dict[str, object]:
    """Pin a message in an allowed channel. State-changing; requires approved=true."""

    _require_approval(approved, "pin_message")
    _require_channel_allowed(channel_id)
    _discord_request("PUT", f"/channels/{channel_id}/pins/{message_id}")
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "pinned": True,
    }


@mcp.tool()
def unpin_message(channel_id: str, message_id: str, approved: bool = False) -> dict[str, object]:
    """Unpin a message in an allowed channel. State-changing; requires approved=true."""

    _require_approval(approved, "unpin_message")
    _require_channel_allowed(channel_id)
    _discord_request("DELETE", f"/channels/{channel_id}/pins/{message_id}")
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "unpinned": True,
    }


@mcp.tool()
def edit_own_message(
    channel_id: str,
    message_id: str,
    content: str,
    approved: bool = False,
) -> dict[str, object]:
    """Edit a message authored by this bot in an allowed channel. Requires approved=true."""

    _require_approval(approved, "edit_own_message")
    _require_channel_allowed(channel_id)
    _require_own_message(channel_id, message_id)
    text = _clean_text(content, "content", 1900)

    row = _discord_request(
        "PATCH",
        f"/channels/{channel_id}/messages/{message_id}",
        payload={
            "content": text,
            "allowed_mentions": {"parse": []},
        },
    )
    return {
        "channel_id": channel_id,
        "message": _message_view(row),
        "updated": True,
    }


@mcp.tool()
def delete_own_message(channel_id: str, message_id: str, approved: bool = False) -> dict[str, object]:
    """Delete a message authored by this bot in an allowed channel. Requires approved=true."""

    _require_approval(approved, "delete_own_message")
    _require_channel_allowed(channel_id)
    _require_own_message(channel_id, message_id)
    _discord_request("DELETE", f"/channels/{channel_id}/messages/{message_id}")
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "deleted": True,
    }
