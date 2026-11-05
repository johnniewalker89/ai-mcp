"""Typed dashboard Pulse requests and private preservation snapshots."""

from __future__ import annotations

import copy
import json
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from mcp_metabase.models import JsonValue


class SubscriptionShapeError(ValueError):
    """A Pulse snapshot/request is incomplete or cannot be preserved safely."""


class PulseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PulseCard(PulseModel):
    id: StrictInt = Field(gt=0)
    dashboard_card_id: StrictInt = Field(gt=0)
    include_csv: StrictBool = False
    include_xls: StrictBool = False
    format_rows: StrictBool = True
    pivot_results: StrictBool = False


class PulseRecipient(PulseModel):
    id: StrictInt | None = Field(default=None, gt=0)
    email: StrictStr = Field(min_length=3, max_length=254)

    @model_validator(mode="after")
    def validate_recipient(self) -> PulseRecipient:
        if self.email.count("@") != 1 or any(c.isspace() for c in self.email):
            raise ValueError("Expected a single email address.")
        return self


class PulseDetails(PulseModel):
    channel: StrictStr | None = Field(default=None, min_length=1, max_length=254)
    channel_id: StrictStr | None = Field(default=None, min_length=1, max_length=254)
    attachment_only: StrictBool = False
    include_pdf: StrictBool = False


class PulseChannel(PulseModel):
    channel_type: Literal["email", "slack", "http"]
    channel_id: StrictInt | None = Field(default=None, gt=0)
    enabled: StrictBool
    details: PulseDetails = Field(default_factory=PulseDetails)
    recipients: list[PulseRecipient] = Field(default_factory=list, max_length=100)
    schedule_type: Literal["hourly", "daily", "weekly", "monthly"]
    schedule_hour: StrictInt | None = Field(default=None, ge=0, le=23)
    schedule_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    schedule_frame: Literal["first", "mid", "last"] | None = None

    @model_validator(mode="after")
    def validate_channel(self) -> PulseChannel:
        validate_channel(self.model_dump(mode="json"))
        return self


class PulseChannelPatch(PulseModel):
    id: StrictInt = Field(gt=0)
    enabled: StrictBool | None = None
    details: PulseDetails | None = None
    recipients: list[PulseRecipient] | None = Field(default=None, max_length=100)
    schedule_type: Literal["hourly", "daily", "weekly", "monthly"] | None = None
    schedule_hour: StrictInt | None = Field(default=None, ge=0, le=23)
    schedule_day: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] | None = None
    schedule_frame: Literal["first", "mid", "last"] | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> PulseChannelPatch:
        if not self.model_fields_set - {"id"}:
            raise ValueError("A channel patch requires changes.")
        if any(
            getattr(self, key) is None
            for key in self.model_fields_set - {"schedule_hour", "schedule_day", "schedule_frame"}
        ):
            raise ValueError("Only schedule components can be cleared with null.")
        return self


class PulseCreate(PulseModel):
    dashboard_id: StrictInt = Field(gt=0)
    name: StrictStr = Field(min_length=1, max_length=254, pattern=r"\S")
    cards: list[PulseCard] = Field(min_length=1, max_length=100)
    channels: list[PulseChannel] = Field(min_length=1, max_length=100)
    parameters: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)
    skip_if_empty: StrictBool = False


class PulsePatch(PulseModel):
    name: StrictStr | None = Field(default=None, min_length=1, max_length=254, pattern=r"\S")
    archived: StrictBool | None = None
    skip_if_empty: StrictBool | None = None
    cards: list[PulseCard] | None = Field(default=None, min_length=1, max_length=100)
    channels: list[PulseChannelPatch] | None = Field(default=None, min_length=1, max_length=100)
    parameters: list[dict[str, JsonValue]] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_patch(self) -> PulsePatch:
        if not self.model_fields_set or any(
            getattr(self, key) is None for key in self.model_fields_set
        ):
            raise ValueError("A Pulse patch requires non-null changes.")
        ids = [channel.id for channel in self.channels or []]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate channel patch identity.")
        if (
            "archived" in self.model_fields_set
            and self.channels is not None
            and any("enabled" in channel.model_fields_set for channel in self.channels)
        ):
            raise ValueError("Archive/restore controls all channel enabled states.")
        return self


class PulseTest(PulseModel):
    subscription_id: StrictInt = Field(gt=0)
    channel_ids: list[StrictInt] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_selection(self) -> PulseTest:
        if any(i <= 0 for i in self.channel_ids) or len(set(self.channel_ids)) != len(
            self.channel_ids
        ):
            raise ValueError("Test requires distinct positive channel ids.")
        return self


def validate_channel(channel: dict[str, Any]) -> None:
    """Validate the merged schedule/destination, including preserved private details."""
    schedule = channel.get("schedule_type")
    hour, day, frame = (channel.get(f"schedule_{key}") for key in ("hour", "day", "frame"))
    if schedule not in {"hourly", "daily", "weekly", "monthly"}:
        raise SubscriptionShapeError("Unsupported Pulse schedule_type.")
    if type(channel.get("enabled")) is not bool:
        raise SubscriptionShapeError("Pulse channel requires an explicit enabled state.")
    if schedule != "hourly" and (type(hour) is not int or not 0 <= hour <= 23):
        raise SubscriptionShapeError("Pulse schedule requires an hour from 0 to 23.")
    if schedule == "weekly" and day not in {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}:
        raise SubscriptionShapeError("Weekly Pulse schedule requires a weekday.")
    if schedule == "monthly" and (
        frame not in {"first", "mid", "last"} or (frame == "mid" and day is not None)
    ):
        raise SubscriptionShapeError("Monthly Pulse schedule requires a valid frame/day.")
    details, recipients = channel.get("details"), channel.get("recipients")
    if not isinstance(details, dict) or not isinstance(recipients, list) or len(recipients) > 100:
        raise SubscriptionShapeError("Pulse destination/recipients snapshot is incomplete.")
    kind = channel.get("channel_type")
    if kind == "email":
        if not recipients:
            raise SubscriptionShapeError("Email Pulse channel requires explicit recipients.")
    elif kind == "slack":
        if not any(
            isinstance(details.get(k), str) and details[k]
            for k in ("channel", "channels", "channel_id")
        ):
            raise SubscriptionShapeError("Slack Pulse channel requires a destination.")
        if recipients:
            raise SubscriptionShapeError("Slack Pulse uses details, not email recipients.")
    elif kind == "http":
        if type(channel.get("channel_id")) is not int or channel["channel_id"] <= 0 or recipients:
            raise SubscriptionShapeError(
                "HTTP Pulse requires a configured channel id and no email recipients."
            )
    else:
        raise SubscriptionShapeError("Unsupported Pulse channel_type.")


def _canonical_channel(raw: dict[str, Any], *, saved: bool) -> dict[str, Any]:
    keys = (
        "channel_type",
        "channel_id",
        "enabled",
        "schedule_type",
        "schedule_hour",
        "schedule_day",
        "schedule_frame",
    )
    channel = {key: copy.deepcopy(raw.get(key)) for key in keys}
    if saved:
        if type(raw.get("id")) is not int or raw["id"] <= 0:
            raise SubscriptionShapeError("Pulse channel has no immutable id.")
        channel["id"] = raw["id"]
    if not isinstance(raw.get("details"), dict) or not isinstance(raw.get("recipients"), list):
        raise SubscriptionShapeError("Pulse destination/recipients snapshot is incomplete.")
    channel["details"] = copy.deepcopy(raw["details"])
    # Metabase hydrates these internal emails into recipients and omits details.emails in JSON.
    channel["details"].pop("emails", None)
    recipients = []
    for recipient in raw["recipients"]:
        if not isinstance(recipient, dict):
            raise SubscriptionShapeError("Pulse recipient must be an object.")
        try:
            parsed = PulseRecipient.model_validate(
                {k: recipient[k] for k in ("id", "email") if k in recipient}
            )
        except ValidationError:
            raise SubscriptionShapeError(
                "Pulse recipient identity/email snapshot is incomplete."
            ) from None
        recipients.append(parsed.model_dump(mode="json", exclude_none=True))
    identities = [("id", r["id"]) if "id" in r else ("email", r["email"]) for r in recipients]
    if len(identities) != len(set(identities)):
        raise SubscriptionShapeError("Pulse recipients contain duplicate identities.")
    channel["recipients"] = sorted(recipients, key=lambda r: json.dumps(r, sort_keys=True))
    validate_channel(channel)
    if channel["schedule_type"] == "hourly":
        channel["schedule_hour"] = None
    if channel["schedule_type"] not in {"weekly", "monthly"}:
        channel["schedule_day"] = None
    if channel["schedule_type"] != "monthly":
        channel["schedule_frame"] = None
    return channel


def subscription_state(raw: dict[str, Any]) -> dict[str, Any]:
    """Сохраняет полный снимок Pulse; GET может скрывать все архивные карточки."""
    for key in ("id", "dashboard_id"):
        if type(raw.get(key)) is not int or raw[key] <= 0:
            raise SubscriptionShapeError(f"Pulse has no positive {key} binding.")
    for key in ("archived", "skip_if_empty"):
        if type(raw.get(key)) is not bool:
            raise SubscriptionShapeError(f"Pulse has no explicit {key} state.")
    if not isinstance(raw.get("name"), str) or not raw["name"].strip():
        raise SubscriptionShapeError("Pulse has no name.")
    for key in ("cards", "channels", "parameters"):
        if not isinstance(raw.get(key), list) or len(raw[key]) > 100:
            raise SubscriptionShapeError(f"Pulse {key} is not a complete bounded array.")
    if not raw["channels"]:
        raise SubscriptionShapeError("Pulse channels cannot be empty.")
    state = {
        key: copy.deepcopy(raw[key])
        for key in ("id", "dashboard_id", "name", "archived", "skip_if_empty", "parameters")
    }
    state["cards"] = []
    for card in raw["cards"]:
        if not isinstance(card, dict):
            raise SubscriptionShapeError("Pulse card must be an object.")
        # GET hydrates cards with queries/metadata; only CardRef fields are persisted by this API.
        fields = {key: card[key] for key in PulseCard.model_fields if key in card}
        if set(fields) != set(PulseCard.model_fields):
            raise SubscriptionShapeError("Pulse CardRef snapshot is incomplete.")
        try:
            state["cards"].append(PulseCard.model_validate(fields).model_dump(mode="json"))
        except ValidationError:
            raise SubscriptionShapeError("Pulse CardRef binding/settings are invalid.") from None
    if any(not isinstance(row, dict) for row in raw["channels"]):
        raise SubscriptionShapeError("Pulse channel must be an object.")
    state["channels"] = sorted(
        (_canonical_channel(row, saved=True) for row in raw["channels"]), key=lambda row: row["id"]
    )
    ids = [row["id"] for row in state["channels"]]
    if len(ids) != len(set(ids)):
        raise SubscriptionShapeError("Pulse channels contain duplicate identities.")
    return state


def subscription_create_payload(request: PulseCreate) -> dict[str, Any]:
    payload = request.model_dump(mode="json", exclude_none=True)
    payload["channels"] = [_canonical_channel(row, saved=False) for row in payload["channels"]]
    return payload


def apply_subscription_patch(before: dict[str, Any], patch: PulsePatch) -> dict[str, Any]:
    after = copy.deepcopy(before)
    values = patch.model_dump(mode="json", exclude_unset=True)
    if patch.cards is not None:
        # A replacement CardRef uses API defaults; exclude_unset is only for merged patches.
        values["cards"] = [card.model_dump(mode="json") for card in patch.cards]
    channel_patches = values.pop("channels", [])
    after.update(values)
    channels = {row["id"]: row for row in after["channels"]}
    for item in channel_patches:
        channel = channels.get(item["id"])
        if channel is None:
            raise SubscriptionShapeError(
                "Pulse patch references a channel outside this subscription."
            )
        changes = {key: value for key, value in item.items() if key != "id"}
        if "details" in changes:
            destination = changes["details"]
            if (
                channel["channel_type"] == "slack"
                and "channel" in destination
                and channel["details"].get("channel_id")
                and "channel_id" not in destination
            ):
                raise SubscriptionShapeError(
                    "Changing Slack destination requires an explicit channel_id "
                    "or null to clear the old binding."
                )
            changes["details"] = {**channel["details"], **changes["details"]}
        channel.update(changes)
    if "archived" in values and values["archived"] != before["archived"]:
        # Root archive hook runs before channel updates: full PUT must agree with that hook.
        for channel in after["channels"]:
            channel["enabled"] = not after["archived"]
    after = subscription_state(after)
    if after["archived"] and any(row["enabled"] for row in after["channels"]):
        raise SubscriptionShapeError("Archived Pulse cannot contain enabled channels.")
    if before == after:
        raise SubscriptionShapeError("Pulse patch has no effective changes.")
    return after


def subscription_update_payload(state: dict[str, Any], patch: PulsePatch) -> dict[str, Any]:
    # Preserve API defaults explicitly, but do not round-trip untouched inventories:
    # GET hides archived cards, so writing that list could delete unseen CardRefs.
    roots = patch.model_fields_set | {"archived", "skip_if_empty"}
    return copy.deepcopy({key: state[key] for key in sorted(roots)})


def subscription_test_payload(state: dict[str, Any], request: PulseTest) -> dict[str, Any]:
    selected = {row["id"]: row for row in state["channels"]}
    if not set(request.channel_ids) <= selected.keys():
        raise SubscriptionShapeError("Pulse test channel is outside this subscription.")
    payload = copy.deepcopy(
        {
            key: state[key]
            for key in ("id", "dashboard_id", "name", "cards", "parameters", "skip_if_empty")
        }
    )
    parameter_fields = {"id", "type", "name", "slug", "value", "default", "target", "options"}
    payload["parameters"] = [
        {key: value for key, value in row.items() if key in parameter_fields}
        for row in payload["parameters"]
    ]
    payload["channels"] = [
        copy.deepcopy(selected[channel_id]) for channel_id in request.channel_ids
    ]
    for channel in payload["channels"]:
        channel["enabled"] = True
    return payload


def subscription_created_matches(expected: dict[str, Any], raw: dict[str, Any]) -> bool:
    actual = subscription_state(raw)
    if actual["archived"]:
        return False
    if any(
        expected[key] != actual[key]
        for key in ("dashboard_id", "name", "cards", "parameters", "skip_if_empty")
    ):
        return False
    # Channel ids are generated; API channel ordering is not a contract.
    expected_channels = sorted(json.dumps(row, sort_keys=True) for row in expected["channels"])
    actual_channels = sorted(
        json.dumps({k: v for k, v in row.items() if k != "id"}, sort_keys=True)
        for row in actual["channels"]
    )
    return expected_channels == actual_channels


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
        "cards": (
            "id",
            "card_id",
            "dashboard_card_id",
            "include_csv",
            "include_xls",
            "format_rows",
            "pivot_results",
        ),
        "channels": (
            "id",
            "channel_type",
            "enabled",
            "schedule_type",
            "schedule_day",
            "schedule_hour",
            "schedule_frame",
            "channel_id",
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
                    if isinstance(details[field], str)
                    else details[field]
                    for field in (
                        "channel",
                        "channel-id",
                        "channel_id",
                        "channels",
                        "attachment_only",
                        "include_pdf",
                    )
                    if isinstance(details, dict) and type(details.get(field)) in (str, bool)
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
    result["write_supported"] = True
    return result
