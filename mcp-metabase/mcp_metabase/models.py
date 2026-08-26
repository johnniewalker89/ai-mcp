from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]


class ObjectType(StrEnum):
    QUESTION = "question"
    DASHBOARD = "dashboard"
    COLLECTION = "collection"
    FIELD = "field"
    DATABASE = "database"


class Action(StrEnum):
    QUESTION_CREATE = "question_create"
    QUESTION_UPDATE = "question_update"
    QUESTION_CLONE = "question_clone"
    QUESTION_TRASH = "question_trash"
    QUESTION_RESTORE = "question_restore"
    DASHBOARD_CREATE = "dashboard_create"
    DASHBOARD_UPDATE = "dashboard_update"
    DASHBOARD_CLONE = "dashboard_clone"
    DASHBOARD_TRASH = "dashboard_trash"
    DASHBOARD_RESTORE = "dashboard_restore"
    COLLECTION_CREATE = "collection_create"
    COLLECTION_UPDATE = "collection_update"
    COLLECTION_CLONE = "collection_clone"
    COLLECTION_TRASH = "collection_trash"
    COLLECTION_RESTORE = "collection_restore"
    FIELD_UPDATE = "field_update"
    FIELD_VALUES_RESCAN = "field_values_rescan"
    BATCH = "batch"
    BATCH_ROLLBACK = "batch_rollback"
    QUESTION_ROLLBACK = "question_rollback"
    DASHBOARD_ROLLBACK = "dashboard_rollback"
    COLLECTION_ROLLBACK = "collection_rollback"
    FIELD_ROLLBACK = "field_rollback"


class Outcome(StrEnum):
    APPLIED_VERIFIED = "applied_verified"
    NOT_APPLIED_VERIFIED = "not_applied_verified"
    REJECTED_VALIDATION = "rejected_validation"
    REJECTED_STALE = "rejected_stale"
    REJECTED_IDENTITY = "rejected_identity"
    PARTIALLY_APPLIED = "partially_applied"
    OUTCOME_UNKNOWN = "outcome_unknown"


class PatchOperation(BaseModel):
    """One closed mutation against a JSON Pointer path."""

    model_config = ConfigDict(extra="forbid")

    op: Literal[
        "set",
        "remove",
        "replace_array",
        "dashboard_item_set",
        "dashboard_item_remove",
        "dashboard_item_replace_array",
    ]
    path: str = Field(min_length=1, max_length=512)
    value: JsonValue = None
    item_id: StrictInt | StrictStr | None = None
    item_path: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_shape(self) -> PatchOperation:
        has_value = "value" in self.model_fields_set
        if (
            self.op
            in {
                "set",
                "replace_array",
                "dashboard_item_set",
                "dashboard_item_replace_array",
            }
            and not has_value
        ):
            raise ValueError(f"{self.op} requires value")
        if self.op == "remove" and (has_value or self.item_id is not None or self.item_path):
            raise ValueError("remove accepts only op and path")
        if self.op == "dashboard_item_remove" and has_value:
            raise ValueError("dashboard_item_remove does not accept value")
        if self.op in {
            "dashboard_item_set",
            "dashboard_item_remove",
            "dashboard_item_replace_array",
        }:
            if self.item_id is None or not self.item_path:
                raise ValueError(f"{self.op} requires item_id and item_path")
        elif self.item_id is not None or self.item_path:
            raise ValueError("item fields are valid only for dashboard item operations")
        if self.op in {"replace_array", "dashboard_item_replace_array"} and not isinstance(
            self.value, list
        ):
            raise ValueError(f"{self.op} requires an array value")
        return self


class QuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=254)
    dataset_query: dict[str, JsonValue]
    display: str = Field(min_length=1, max_length=64)
    visualization_settings: dict[str, JsonValue] = Field(default_factory=dict)
    collection_id: int | None = None
    description: str | None = None
    parameters: list[dict[str, JsonValue]] = Field(default_factory=list)
    parameter_mappings: list[dict[str, JsonValue]] = Field(default_factory=list)
    cache_ttl: int | None = Field(default=None, gt=0)
    type: Literal["question", "model", "metric"] = "question"


class QuestionPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_query: dict[str, JsonValue]
    parameters: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)


class DashboardCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=254)
    collection_id: int | None = None
    description: str | None = None
    parameters: list[dict[str, JsonValue]] = Field(default_factory=list)
    cache_ttl: int | None = Field(default=None, gt=0)
    width: Literal["fixed", "full"] | None = None
    dashcards: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=200)
    tabs: list[dict[str, JsonValue]] = Field(default_factory=list, max_length=100)


class CollectionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=254)
    description: str | None = None
    parent_id: int | None = Field(default=None, gt=0)


class BatchUpdateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object_type: Literal["question", "dashboard", "collection", "field"]
    object_id: int = Field(gt=0)
    operations: list[PatchOperation] = Field(min_length=1, max_length=100)


@dataclass
class PlannedMutation:
    object_type: ObjectType
    object_id: int | None
    before_state: dict[str, Any] | None
    after_state: dict[str, Any] | None
    write_payload: dict[str, Any]
    changed_roots: tuple[str, ...]
    before_sha256: str | None
    after_sha256: str | None
    target: dict[str, Any] = field(default_factory=dict)
    verified_after_state: dict[str, Any] | None = None
    verified_after_sha256: str | None = None


@dataclass
class ExactPlan:
    plan_id: str
    digest: str
    instance: str
    origin: str
    credential_fingerprint: str
    identity_marker: str
    server_version: str
    action: Action
    mutations: list[PlannedMutation]
    arguments: dict[str, Any]
    expires_at: float
    serialized_bytes: int
    consumed: bool = False
    revoked: bool = False
    outcome: Outcome | None = None
    result: dict[str, Any] | None = None


@dataclass
class EditSession:
    session_id: str
    instance: str
    origin: str
    credential_fingerprint: str
    identity_marker: str
    server_version: str
    object_type: ObjectType
    object_id: int
    initial_state_sha256: str
    current_state_sha256: str
    object_state_sha256: dict[str, str]
    approval_scope: str
    expires_at: float
    max_actions: int
    actions_used: int = 0
    in_flight: bool = False
    closed: bool = False
    close_reason: str | None = None
