"""Read-only, allowlisted projection of legacy dashboard Pulse subscriptions."""

from __future__ import annotations

from typing import Any


def subscription_public(raw: dict[str, Any], *, max_items: int) -> dict[str, Any]:
    """Never return hydrated users, cards, arbitrary channel details or credentials."""
    for key in ("id", "dashboard_id"):
        if type(raw.get(key)) is not int or raw[key] <= 0:
            raise ValueError(f"Dashboard subscription has an invalid {key}.")
    if type(raw.get("archived")) is not bool:
        raise ValueError("Dashboard subscription has no explicit archived state.")
    result = {key: raw[key] for key in ("id", "dashboard_id", "archived")}
    if isinstance(raw.get("name"), str):
        result["name"] = raw["name"][:500]
    complete = True
    for key, fields in {
        "cards": ("id", "card_id", "dashboard_card_id", "include_csv", "include_xls"),
        "channels": (
            "id",
            "channel_type",
            "enabled",
            "schedule_type",
            "schedule_day",
            "schedule_hour",
            "schedule_frame",
        ),
    }.items():
        rows = raw.get(key)
        if rows is None:
            result[key] = None
            complete = False
            continue
        if not isinstance(rows, list) or len(rows) > max_items:
            raise ValueError(f"Dashboard subscription {key} exceeds its shape/bound.")
        projected = []
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError(f"Dashboard subscription {key} contains a malformed item.")
            required = (
                {"id"} if key == "cards" else {"id", "channel_type", "enabled", "schedule_type"}
            )
            if not required.issubset(row) or any(row.get(field) is None for field in required):
                complete = False
            item = {
                field: row[field][:500] if isinstance(row[field], str) else row[field]
                for field in fields
                if field in row and (row[field] is None or type(row[field]) in (str, int, bool))
            }
            if key == "channels":
                details = row.get("details")
                item["destination"] = {
                    field: details[field][:500]
                    for field in ("channel", "channel-id")
                    if isinstance(details, dict) and isinstance(details.get(field), str)
                }
                recipients = row.get("recipients")
                if recipients is None:
                    item["recipients"] = None
                    complete = False
                elif isinstance(recipients, list) and len(recipients) <= max_items:
                    item["recipients"] = []
                    for recipient in recipients:
                        if isinstance(recipient, str):
                            item["recipients"].append({"email": recipient[:254]})
                        elif isinstance(recipient, dict):
                            item["recipients"].append(
                                {
                                    field: value[:254] if isinstance(value, str) else value
                                    for field in ("id", "email")
                                    if (value := recipient.get(field)) is not None
                                    and type(value) in (str, int)
                                }
                            )
                        else:
                            raise ValueError("Dashboard subscription has malformed recipients.")
                else:
                    raise ValueError("Dashboard subscription recipients exceed their shape/bound.")
            projected.append(item)
        result[key] = projected
    result["settings_complete"] = complete
    result["projection"] = "subscription_settings"
    result["write_supported"] = False
    return result
