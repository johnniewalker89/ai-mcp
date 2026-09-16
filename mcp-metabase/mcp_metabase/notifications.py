from __future__ import annotations

import copy
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator


class NotificationShapeError(ValueError):
    """Неполный снимок нельзя использовать для сохранного spec-diff update."""


class SchedulePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subscription_id: StrictInt = Field(gt=0)
    cron_schedule: StrictStr = Field(min_length=1, max_length=200)
    ui_display_type: Literal["cron/raw", "cron/builder"] | None = None


class RecipientPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handler_id: StrictInt = Field(gt=0)
    recipient_id: StrictInt = Field(gt=0)
    value: StrictStr = Field(min_length=1, max_length=254)


class NotificationCreate(BaseModel):
    """Создаём неактивную Slack notification; включение — отдельный exact update."""

    model_config = ConfigDict(extra="forbid")
    question_id: StrictInt = Field(gt=0)
    cron_schedule: StrictStr = Field(min_length=1, max_length=200)
    slack_recipient: StrictStr = Field(min_length=2, max_length=254)
    send_once: StrictBool = False

    @model_validator(mode="after")
    def validate_create(self) -> NotificationCreate:
        validate_cron(self.cron_schedule)
        if not self.slack_recipient.startswith(("#", "@")) or any(
            char.isspace() for char in self.slack_recipient
        ):
            raise ValueError("Slack recipient must be a channel/user name prefixed # or @.")
        return self

    def payload(self) -> dict[str, Any]:
        return {
            "payload_type": "notification/card",
            "active": False,
            "payload": {
                "card_id": self.question_id,
                "send_condition": "has_result",
                "send_once": self.send_once,
            },
            "subscriptions": [
                {
                    "type": "notification-subscription/cron",
                    "cron_schedule": self.cron_schedule,
                    "ui_display_type": "cron/raw",
                }
            ],
            "handlers": [
                {
                    "channel_type": "channel/slack",
                    "active": True,
                    "recipients": [
                        {
                            "type": "notification-recipient/raw-value",
                            "details": {"value": self.slack_recipient},
                        }
                    ],
                }
            ],
        }


class NotificationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    send_once: StrictBool | None = None
    active: StrictBool | None = None
    schedules: list[SchedulePatch] = Field(default_factory=list, max_length=100)
    recipients: list[RecipientPatch] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_patch(self) -> NotificationPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("Null is not a notification patch value.")
        if not self.model_fields_set or not (
            self.schedules or self.recipients or self.model_fields_set & {"active", "send_once"}
        ):
            raise ValueError("Notification patch has no changes.")
        keys = [p.subscription_id for p in self.schedules]
        recipients = [(p.handler_id, p.recipient_id) for p in self.recipients]
        if len(keys) != len(set(keys)) or len(recipients) != len(set(recipients)):
            raise ValueError("Duplicate notification patch target.")
        for schedule in self.schedules:
            validate_cron(schedule.cron_schedule)
        return self


def validate_cron(value: str) -> None:
    # Quartz проверяет сервер; здесь ограничиваем форму, размер и управляющие символы.
    if len(value.split()) not in {6, 7} or not re.fullmatch(r"[A-Za-z0-9*?,/\-#LW\s]+", value):
        raise ValueError("Expected a six/seven-field Quartz cron expression.")
    if any(ord(char) < 32 for char in value):
        raise ValueError("Cron expression cannot contain control characters.")


def _rows(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 100:
        raise NotificationShapeError(f"Notification {label} must be a bounded complete array.")
    ids = []
    for row in value:
        if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
            raise NotificationShapeError(f"Notification {label} contains an invalid identity.")
        ids.append(row["id"])
    if len(ids) != len(set(ids)):
        raise NotificationShapeError(f"Notification {label} contains duplicate identities.")
    return value


def notification_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Сохраняем неизвестные поля, исключая только известные hydrated ресурсы."""
    state = copy.deepcopy(raw)
    if type(state.get("id")) is not int or state["id"] <= 0:
        raise NotificationShapeError("Notification has no immutable id.")
    if state.get("payload_type") != "notification/card":
        raise NotificationShapeError("Only card notifications are supported; not legacy Pulse.")
    if type(state.get("creator_id")) is not int or state["creator_id"] <= 0:
        raise NotificationShapeError("Notification creator binding is incomplete.")
    payload = state.get("payload")
    if (
        not isinstance(payload, dict)
        or type(payload.get("card_id")) is not int
        or payload["card_id"] <= 0
    ):
        raise NotificationShapeError("Notification has no card payload.")
    if type(payload.get("id")) is not int or payload["id"] <= 0:
        raise NotificationShapeError("Notification card payload has no immutable id.")
    if type(state.get("active")) is not bool or type(payload.get("send_once")) is not bool:
        raise NotificationShapeError("Notification active/send_once state is incomplete.")
    state.pop("creator", None)
    payload.pop("card", None)
    state["subscriptions"] = sorted(
        _rows(state.get("subscriptions"), "subscriptions"), key=lambda row: row["id"]
    )
    state["handlers"] = sorted(_rows(state.get("handlers"), "handlers"), key=lambda row: row["id"])
    for handler in state["handlers"]:
        # Channel — внешний ресурс с credentials, не часть notification update spec.
        handler.pop("channel", None)
        if handler.get("template_id") is not None:
            template = handler.get("template")
            if not isinstance(template, dict) or template.get("id") != handler["template_id"]:
                raise NotificationShapeError("Notification template binding is incomplete.")
        handler["recipients"] = sorted(
            _rows(handler.get("recipients"), "recipients"), key=lambda row: row["id"]
        )
        for recipient in handler["recipients"]:
            recipient.pop("user", None)
            recipient.pop("permissions_group", None)
    # Метки времени — server-owned; содержимое и immutable IDs остаются связанными.
    rows = [state, payload, *state["subscriptions"], *state["handlers"]]
    for handler in state["handlers"]:
        rows.extend(handler["recipients"])
        if isinstance(handler.get("template"), dict):
            rows.append(handler["template"])
    for row in rows:
        row.pop("created_at", None)
        row.pop("updated_at", None)
    return state


def notification_public(raw: dict[str, Any]) -> dict[str, Any]:
    """Read projection: не выдаём channel credentials, templates и неизвестные blobs."""
    state = notification_state(raw)
    return {
        "id": state["id"],
        "kind": "card_notification",
        "active": state["active"],
        "card_id": state["payload"]["card_id"],
        "send_once": state["payload"]["send_once"],
        "send_condition": state["payload"].get("send_condition"),
        "timezone_source": "Metabase instance scheduler; no per-subscription timezone field",
        "subscriptions": [
            {
                key: row[key]
                for key in ("id", "type", "cron_schedule", "ui_display_type")
                if key in row
            }
            for row in state["subscriptions"]
        ],
        "handlers": [
            {
                **{
                    key: row[key]
                    for key in ("id", "channel_type", "channel_id", "active")
                    if key in row
                },
                "recipients": [
                    {
                        **{
                            key: r[key]
                            for key in ("id", "type", "user_id", "permissions_group_id")
                            if key in r
                        },
                        "value": r.get("details", {}).get("value")
                        if row.get("channel_type") == "channel/slack"
                        and r.get("type") == "notification-recipient/raw-value"
                        and isinstance(r.get("details"), dict)
                        else None,
                    }
                    for r in row["recipients"]
                ],
            }
            for row in state["handlers"]
        ],
    }


def apply_notification_patch(before: dict[str, Any], patch: NotificationPatch) -> dict[str, Any]:
    after = copy.deepcopy(before)
    # API запрещает resource templates; пропуск template удалил бы связанный ресурс.
    for handler in after["handlers"]:
        template = handler.get("template")
        if (
            isinstance(template, dict)
            and isinstance(template.get("details"), dict)
            and template["details"].get("type") == "email/handlebars-resource"
        ):
            raise NotificationShapeError("Resource-template notification cannot be safely updated.")
    if "active" in patch.model_fields_set:
        after["active"] = patch.active
    if "send_once" in patch.model_fields_set:
        after["payload"]["send_once"] = patch.send_once
    subscriptions = {row["id"]: row for row in after["subscriptions"]}
    for change in patch.schedules:
        row = subscriptions.get(change.subscription_id)
        if row is None or row.get("type") != "notification-subscription/cron":
            raise NotificationShapeError("Schedule patch must bind an existing cron subscription.")
        row["cron_schedule"] = change.cron_schedule
        if change.ui_display_type is not None:
            row["ui_display_type"] = change.ui_display_type
    handlers = {row["id"]: row for row in after["handlers"]}
    for change in patch.recipients:
        handler = handlers.get(change.handler_id)
        if handler is None or handler.get("channel_type") != "channel/slack":
            raise NotificationShapeError(
                "Recipient update currently supports existing Slack handlers."
            )
        recipient = next((r for r in handler["recipients"] if r["id"] == change.recipient_id), None)
        if recipient is None or recipient.get("type") != "notification-recipient/raw-value":
            raise NotificationShapeError(
                "Recipient patch must bind an existing raw-value recipient."
            )
        if not change.value.startswith(("#", "@")) or any(c.isspace() for c in change.value):
            raise NotificationShapeError(
                "Slack recipient must be a channel/user name prefixed # or @."
            )
        # channel_id внутри details мог указывать на старый destination: не переносим его молча.
        if not isinstance(recipient.get("details"), dict):
            raise NotificationShapeError("Recipient details are incomplete.")
        if recipient["details"].get("channel_id") is not None:
            raise NotificationShapeError(
                "Recipient has a bound channel_id; changing value alone is unsafe."
            )
        recipient["details"]["value"] = change.value
    if after == before:
        raise NotificationShapeError("Notification patch produces no state change.")
    return after
