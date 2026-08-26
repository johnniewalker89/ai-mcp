from __future__ import annotations

import copy
import re
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from mcp_metabase.audit import AuditWriter
from mcp_metabase.config import MetabaseConfig
from mcp_metabase.edit_sessions import EditSessionStore
from mcp_metabase.http_client import MetabaseApiError, MetabaseHttpClient
from mcp_metabase.models import (
    Action,
    BatchUpdateItem,
    CollectionCreate,
    DashboardCreate,
    EditSession,
    ExactPlan,
    ObjectType,
    Outcome,
    PatchOperation,
    PlannedMutation,
    QuestionCreate,
    QuestionPreview,
)
from mcp_metabase.normalization import (
    MutationValidationError,
    build_mutation,
    canonical_sha256,
    dataset_query_semantically_matches,
    mutation_summary,
    project_state,
    rollback_mutation,
    validate_edit_session_operations,
    validate_edit_session_state_bindings,
    validate_state,
    verify_mutation,
)
from mcp_metabase.plans import ExactPlanStore, MetabasePolicyError
from mcp_metabase.query_policy import validate_native_preview_sql

COLLECTION_REF_RE = re.compile(r"^(?:root|trash|[1-9][0-9]*|[A-Za-z0-9_-]{21})$")
SEARCH_MODELS = frozenset(
    {
        "card",
        "dataset",
        "metric",
        "dashboard",
        "collection",
        "database",
        "table",
        "segment",
        "measure",
        "document",
        "action",
        "transform",
        "indexed-entity",
    }
)
COMPACT_MUTATION_ACTIONS = frozenset(
    {
        Action.QUESTION_CREATE,
        Action.QUESTION_UPDATE,
        Action.QUESTION_CLONE,
        Action.QUESTION_TRASH,
        Action.QUESTION_RESTORE,
        Action.DASHBOARD_CREATE,
        Action.DASHBOARD_UPDATE,
        Action.DASHBOARD_CLONE,
        Action.DASHBOARD_TRASH,
        Action.DASHBOARD_RESTORE,
        Action.COLLECTION_CREATE,
        Action.COLLECTION_UPDATE,
        Action.COLLECTION_CLONE,
        Action.COLLECTION_TRASH,
        Action.COLLECTION_RESTORE,
        Action.FIELD_UPDATE,
        Action.FIELD_VALUES_RESCAN,
        Action.BATCH,
    }
)
COMPACT_ACTION_ARGUMENT_KEYS: dict[Action, tuple[frozenset[str], frozenset[str]]] = {
    Action.QUESTION_CREATE: (frozenset({"body"}), frozenset()),
    Action.QUESTION_CLONE: (
        frozenset({"source_question_id", "name"}),
        frozenset({"collection_id", "to_root"}),
    ),
    Action.QUESTION_UPDATE: (frozenset({"question_id", "operations"}), frozenset()),
    Action.QUESTION_TRASH: (frozenset({"question_id"}), frozenset()),
    Action.QUESTION_RESTORE: (
        frozenset({"question_id"}),
        frozenset({"collection_id", "to_root"}),
    ),
    Action.DASHBOARD_CREATE: (frozenset({"body"}), frozenset()),
    Action.DASHBOARD_CLONE: (
        frozenset({"source_dashboard_id"}),
        frozenset({"name", "collection_id", "is_deep_copy", "to_root"}),
    ),
    Action.DASHBOARD_UPDATE: (frozenset({"dashboard_id", "operations"}), frozenset()),
    Action.DASHBOARD_TRASH: (frozenset({"dashboard_id"}), frozenset()),
    Action.DASHBOARD_RESTORE: (
        frozenset({"dashboard_id"}),
        frozenset({"collection_id", "to_root"}),
    ),
    Action.COLLECTION_CREATE: (frozenset({"body"}), frozenset()),
    Action.COLLECTION_CLONE: (
        frozenset({"source_collection_id", "name"}),
        frozenset({"parent_id", "to_root"}),
    ),
    Action.COLLECTION_UPDATE: (
        frozenset({"collection_id", "operations"}),
        frozenset(),
    ),
    Action.COLLECTION_TRASH: (frozenset({"collection_id"}), frozenset()),
    Action.COLLECTION_RESTORE: (
        frozenset({"collection_id"}),
        frozenset({"parent_id", "to_root"}),
    ),
    Action.FIELD_UPDATE: (frozenset({"field_id", "operations"}), frozenset()),
    Action.FIELD_VALUES_RESCAN: (frozenset({"database_id"}), frozenset()),
    Action.BATCH: (frozenset({"items"}), frozenset()),
}


class MetabaseRuntime:
    def __init__(
        self,
        config: MetabaseConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.http = MetabaseHttpClient(config, transport=transport)
        self.plans = ExactPlanStore(
            ttl_seconds=config.plan_ttl_seconds,
            max_plans=config.max_active_plans,
            max_plan_bytes=config.max_plan_bytes,
            clock=clock,
        )
        self.edit_sessions = EditSessionStore(
            ttl_seconds=config.edit_session_ttl_seconds,
            max_actions=config.edit_session_max_actions,
            max_sessions=config.max_active_edit_sessions,
            clock=clock,
        )
        self.audit = AuditWriter(config.audit_dir)

    @staticmethod
    def _positive_id(value: int, label: str = "id") -> int:
        if type(value) is not int or value <= 0:
            raise MutationValidationError(f"Metabase {label} must be a positive integer.")
        return value

    @staticmethod
    def _collection_ref(value: int | str) -> str:
        candidate = str(value)
        if not COLLECTION_REF_RE.fullmatch(candidate):
            raise MutationValidationError("Metabase collection reference is invalid.")
        return candidate

    @staticmethod
    def _extract_version(properties: dict[str, Any]) -> str | None:
        raw = properties.get("version")
        if isinstance(raw, str):
            return raw
        if isinstance(raw, dict):
            for key in ("tag", "version"):
                if isinstance(raw.get(key), str):
                    return str(raw[key])
        for key in ("version-tag", "version_tag"):
            if isinstance(properties.get(key), str):
                return str(properties[key])
        return None

    @staticmethod
    def _identity_marker(user: dict[str, Any]) -> str | None:
        user_id = user.get("id")
        if type(user_id) is int and user_id > 0:
            return f"user:{user_id}"
        return None

    def _health_context(self) -> dict[str, Any]:
        properties = self.http.get_json("/api/session/properties")
        user = self.http.get_json("/api/user/current")
        if not isinstance(properties, dict) or not isinstance(user, dict):
            raise MetabasePolicyError("Metabase health endpoints returned invalid object shapes.")
        version = self._extract_version(properties)
        marker = self._identity_marker(user)
        user_id = user.get("id") if marker else None
        expected_matches = self.config.expected_user_id in {None, user_id}
        return {
            "version": version,
            "version_supported": self.config.version_supported(version),
            "identity_marker": marker,
            "identity_verified": marker is not None and expected_matches,
            "expected_identity_matches": expected_matches,
            "user": {
                key: user[key]
                for key in ("id", "common_name", "email", "is_superuser", "is_active")
                if key in user
            },
        }

    def health(self) -> dict[str, Any]:
        context = self._health_context()
        if not context["version_supported"]:
            status = "incompatible"
        elif not context["identity_verified"]:
            status = "identity_unverified"
        else:
            status = "ready"
        compatibility_mode = "read_write" if status == "ready" else "read_only_degraded"
        return {
            "status": status,
            "compatibility_mode": compatibility_mode,
            "instance": self.config.instance,
            "origin": self.config.origin,
            "source_revision": self.config.source_revision,
            "server_version": context["version"],
            "supported_version_prefixes": list(self.config.supported_version_prefixes),
            "version_supported": context["version_supported"],
            "credential_fingerprint": self.config.credential_fingerprint,
            "identity_verified": context["identity_verified"],
            "expected_identity_matches": context["expected_identity_matches"],
            "subject": context["user"],
            "writes_ready": bool(context["version_supported"] and context["identity_verified"]),
            "capabilities": {
                "bounded_reads": True,
                "saved_question_query": True,
                "question_crud": True,
                "dashboard_crud": True,
                "collection_crud": True,
                "field_update": True,
                "exact_batch": True,
                "rollback": True,
                "full_object_sessions": True,
                "scoped_edit_sessions": True,
                "public_tool_count": 14,
                "permanent_delete": False,
                "generic_api": False,
                "arbitrary_sql": False,
            },
            "limits": {
                "default_query_rows": self.config.default_query_rows,
                "max_query_rows": self.config.max_query_rows,
                "max_list_items": self.config.max_list_items,
                "max_batch_items": self.config.max_batch_items,
                "max_response_bytes": self.config.max_json_bytes,
                "plan_ttl_seconds": self.config.plan_ttl_seconds,
                "edit_session_ttl_seconds": self.config.edit_session_ttl_seconds,
                "edit_session_max_actions": self.config.edit_session_max_actions,
                "max_active_edit_sessions": self.config.max_active_edit_sessions,
            },
        }

    def _write_context(self) -> dict[str, Any]:
        context = self._health_context()
        if not context["version_supported"]:
            raise MetabasePolicyError("Metabase server version is outside the supported contract.")
        if not context["identity_verified"] or not context["identity_marker"]:
            raise MetabasePolicyError("Metabase API-key subject identity could not be verified.")
        return context

    @staticmethod
    def _list_payload(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
        total: int | None = None
        if isinstance(payload, list):
            raw_items = payload
        elif isinstance(payload, dict) and isinstance(payload.get("data"), list):
            raw_items = payload["data"]
            if type(payload.get("total")) is int:
                total = payload["total"]
        else:
            raise MutationValidationError("Metabase list endpoint returned an invalid shape.")
        if any(not isinstance(item, dict) for item in raw_items):
            raise MutationValidationError("Metabase list endpoint returned a non-object item.")
        return raw_items, total

    def _bounded_envelope(
        self,
        payload: Any,
        *,
        limit: int,
        offset: int,
    ) -> dict[str, Any]:
        self._validate_page(limit=limit, offset=offset)
        items, total = self._list_payload(payload)
        selected = items[:limit]
        has_more = len(items) > limit or (total is not None and offset + len(selected) < total)
        return {
            "origin": self.config.origin,
            "items": selected,
            "count": len(selected),
            "total": total,
            "offset": offset,
            "next_offset": offset + len(selected) if has_more else None,
            "has_more": has_more,
            "truncated": has_more,
        }

    def _validate_page(self, *, limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= self.config.max_list_items:
            raise MutationValidationError("Metabase list limit is outside the configured bound.")
        if type(offset) is not int or offset < 0:
            raise MutationValidationError("Metabase list offset must be non-negative.")

    def search(
        self,
        query: str = "",
        models: list[str] | None = None,
        *,
        archived: bool = False,
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, Any]:
        if not isinstance(query, str) or len(query) > 500:
            raise MutationValidationError("Metabase search query is invalid or too long.")
        self._validate_page(limit=limit, offset=offset)
        selected_models = list(dict.fromkeys(models or []))
        if any(model not in SEARCH_MODELS for model in selected_models):
            raise MutationValidationError("Metabase search model is outside the closed allowlist.")
        params: dict[str, str | int | bool | list[str]] = {
            "q": query,
            "archived": archived,
            "limit": limit,
            "offset": offset,
        }
        if selected_models:
            params["models"] = selected_models
        payload = self.http.get_json("/api/search", params=params)
        return self._bounded_envelope(payload, limit=limit, offset=offset)

    def _collection_path(self, collection_id: int | str, *, items: bool = False) -> str:
        segment = quote(self._collection_ref(collection_id), safe="")
        return f"/api/collection/{segment}{'/items' if items else ''}"

    def collection_get(self, collection_id: int | str) -> dict[str, Any]:
        payload = self.http.get_json(self._collection_path(collection_id))
        if not isinstance(payload, dict):
            raise MutationValidationError("Metabase collection endpoint returned an invalid shape.")
        return {"origin": self.config.origin, "collection": payload}

    def collection_items(
        self,
        collection_id: int | str,
        *,
        models: list[str] | None = None,
        archived: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._validate_page(limit=limit, offset=offset)
        selected_models = list(dict.fromkeys(models or []))
        if any(model not in {"card", "dashboard", "collection"} for model in selected_models):
            raise MutationValidationError("Collection item model is outside the closed allowlist.")
        params: dict[str, str | int | bool | list[str]] = {
            "archived": archived,
            "limit": limit,
            "offset": offset,
        }
        if selected_models:
            params["models"] = selected_models
        payload = self.http.get_json(
            self._collection_path(collection_id, items=True), params=params
        )
        return self._bounded_envelope(payload, limit=limit, offset=offset)

    def _object_raw(self, object_type: ObjectType, object_id: int) -> dict[str, Any]:
        object_id = self._positive_id(object_id, f"{object_type.value} id")
        paths = {
            ObjectType.QUESTION: f"/api/card/{object_id}",
            ObjectType.DASHBOARD: f"/api/dashboard/{object_id}",
            ObjectType.COLLECTION: f"/api/collection/{object_id}",
            ObjectType.FIELD: f"/api/field/{object_id}",
            ObjectType.DATABASE: f"/api/database/{object_id}",
        }
        payload = self.http.get_json(paths[object_type])
        if not isinstance(payload, dict):
            raise MutationValidationError(
                f"Metabase {object_type.value} endpoint returned an invalid shape."
            )
        return payload

    def _full_object(self, object_type: ObjectType, object_id: int) -> dict[str, Any]:
        raw = self._object_raw(object_type, object_id)
        state = project_state(raw, object_type)
        return {
            "origin": self.config.origin,
            "object_type": object_type.value,
            "object_id": state["id"],
            "state_sha256": canonical_sha256(state),
            "object": raw,
        }

    def question_get_full(self, question_id: int) -> dict[str, Any]:
        return self._full_object(ObjectType.QUESTION, question_id)

    def dashboard_get_full(self, dashboard_id: int) -> dict[str, Any]:
        return self._full_object(ObjectType.DASHBOARD, dashboard_id)

    def object_get(
        self,
        object_type: str,
        object_id: int | str,
        *,
        include_fields: bool = True,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Read one typed object through a compact, closed dispatcher."""

        if object_type == "collection":
            return self.collection_get(object_id)
        if type(object_id) is not int:
            raise MutationValidationError(
                "Metabase object id must be a positive integer for this object type."
            )
        if object_type == "question":
            return self.question_get_full(object_id)
        if object_type == "dashboard":
            return self.dashboard_get_full(object_id)
        if object_type == "database":
            return self.database_get(object_id)
        if object_type == "table":
            return self.table_get(object_id, include_fields=include_fields)
        if object_type == "field":
            return self.field_get(object_id)
        if object_type == "field_values":
            return self.field_values_get(object_id, limit=limit)
        raise MutationValidationError("Metabase object type is outside the closed read allowlist.")

    @staticmethod
    def _edit_session_object_type(value: str) -> ObjectType:
        try:
            object_type = ObjectType(value)
        except ValueError:
            raise MutationValidationError(
                "Metabase edit sessions support only question or dashboard objects."
            ) from None
        if object_type not in {ObjectType.QUESTION, ObjectType.DASHBOARD}:
            raise MutationValidationError(
                "Metabase edit sessions support only question or dashboard objects."
            )
        return object_type

    @staticmethod
    def _edit_session_allowed_scope(object_type: ObjectType) -> list[str]:
        if object_type is ObjectType.QUESTION:
            return [
                "/name",
                "/description",
                "/display",
                "/visualization_settings/** (except click/link behavior)",
            ]
        return [
            "/name",
            "/description",
            "/width",
            "/dashcards/<existing-id>/{row,col,size_x,size_y,dashboard_tab_id}",
            "/dashcards/<existing-id>/visualization_settings/** (except click/link behavior)",
            "/tabs/<existing-id>/name",
        ]

    def _audit_edit_session(
        self,
        session: EditSession,
        *,
        action: str,
        outcome: str,
        state_sha256: str | None = None,
        changed_roots: tuple[str, ...] | list[str] | None = None,
        rollback_source_plan_id: str | None = None,
    ) -> str:
        return self.audit.write(
            {
                "action": action,
                "actions_used": session.actions_used,
                "changed_roots": list(changed_roots or []),
                "close_reason": session.close_reason,
                "credential_fingerprint": session.credential_fingerprint,
                "expires_at_epoch": session.expires_at,
                "identity_marker": session.identity_marker,
                "instance": session.instance,
                "max_actions": session.max_actions,
                "object_id": session.object_id,
                "object_ids": sorted(
                    int(key.split(":", 1)[1]) for key in session.object_state_sha256
                ),
                "object_type": session.object_type.value,
                "origin": session.origin,
                "outcome": outcome,
                "rollback_source_plan_id": rollback_source_plan_id,
                "server_version": session.server_version,
                "session_id": session.session_id,
                "state_sha256": state_sha256 or session.current_state_sha256,
            }
        )

    def edit_session_open(
        self,
        object_type: str,
        object_id: int,
        *,
        ttl_seconds: int | None = None,
        max_actions: int | None = None,
    ) -> dict[str, Any]:
        selected_type = self._edit_session_object_type(object_type)
        requested_object_id = self._positive_id(object_id, f"{selected_type.value} id")
        context = self._write_context()
        raw = self._object_raw(selected_type, requested_object_id)
        state = project_state(raw, selected_type)
        validate_state(state, selected_type)
        if state["id"] != requested_object_id:
            raise MutationValidationError(
                "Metabase edit-session readback returned a different object id."
            )
        state_sha256 = canonical_sha256(state)
        session = self.edit_sessions.open(
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(context["identity_marker"]),
            server_version=str(context["version"]),
            object_type=selected_type,
            object_id=int(state["id"]),
            state_sha256=state_sha256,
            ttl_seconds=ttl_seconds,
            max_actions=max_actions,
        )
        try:
            audit_id = self._audit_edit_session(
                session,
                action="edit_session_open",
                outcome="opened",
            )
        except (OSError, RuntimeError) as exc:
            self.edit_sessions.close(
                session.session_id,
                reason="open_audit_unavailable",
            )
            raise MetabasePolicyError(
                "Metabase edit session could not record its durable open audit."
            ) from exc
        return {
            "opened": True,
            "approval_scope": "presentation_layout_only",
            "allowed_paths": self._edit_session_allowed_scope(selected_type),
            "forbidden_scope": [
                "SQL/MBQL definition",
                "parameters and mappings",
                "click/link/cross-filter behavior",
                "collection move",
                "archive/delete",
                "dashboard element composition",
                "another object",
            ],
            "session": self.edit_sessions.snapshot(session),
            "audit_id": audit_id,
        }

    def edit_session_status(self, session_id: str) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id)
        return {
            "session": self.edit_sessions.snapshot(session),
            "allowed_paths": self._edit_session_allowed_scope(session.object_type),
        }

    def edit_session_close(self, session_id: str) -> dict[str, Any]:
        session = self.edit_sessions.close(session_id)
        try:
            audit_id = self._audit_edit_session(
                session,
                action="edit_session_close",
                outcome="closed",
            )
            audit_recorded = True
        except (OSError, RuntimeError):
            audit_id = None
            audit_recorded = False
        return {
            "closed": True,
            "session": self.edit_sessions.snapshot(session),
            "audit_id": audit_id,
            "audit_recorded": audit_recorded,
        }

    def edit_session_apply(
        self,
        session_id: str,
        raw_operations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id, require_active=True)
        operations = self._parse_operations(raw_operations)
        validate_edit_session_operations(session.object_type, operations)

        context = self._health_context()
        identity_marker = context.get("identity_marker")
        if (
            not context["version_supported"]
            or not context["identity_verified"]
            or identity_marker != session.identity_marker
            or context.get("version") != session.server_version
        ):
            session = self.edit_sessions.fail_apply(
                session_id,
                reason="identity_or_version_changed",
            )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    session,
                    action="edit_session_apply",
                    outcome=Outcome.REJECTED_IDENTITY.value,
                )
            raise MetabasePolicyError("Metabase edit-session identity or version binding changed.")

        session = self.edit_sessions.begin_apply(
            session_id,
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(identity_marker),
            server_version=str(context["version"]),
        )
        try:
            raw = self._object_raw(session.object_type, session.object_id)
            current_state = project_state(raw, session.object_type)
            current_sha256 = canonical_sha256(current_state)
        except MetabaseApiError:
            self.edit_sessions.release_apply(session_id)
            raise
        except MutationValidationError:
            self.edit_sessions.fail_apply(session_id, reason="invalid_current_state")
            raise

        if current_sha256 != session.current_state_sha256:
            session = self.edit_sessions.fail_apply(session_id, reason="stale_external_edit")
            try:
                audit_id = self._audit_edit_session(
                    session,
                    action="edit_session_apply",
                    outcome=Outcome.REJECTED_STALE.value,
                    state_sha256=current_sha256,
                )
                audit_recorded = True
            except (OSError, RuntimeError):
                audit_id = None
                audit_recorded = False
            return {
                "outcome": Outcome.REJECTED_STALE.value,
                "reason": "current_state_no_longer_matches_edit_session",
                "session": self.edit_sessions.snapshot(session),
                "current_state_sha256": current_sha256,
                "audit_id": audit_id,
                "audit_recorded": audit_recorded,
                "rollback_source_plan_id": None,
            }

        try:
            validate_edit_session_state_bindings(
                session.object_type,
                operations,
                current_state,
            )
            mutation = build_mutation(
                object_type=session.object_type,
                raw_before=raw,
                operations=operations,
            )
        except MutationValidationError:
            self.edit_sessions.release_apply(session_id)
            raise
        if mutation.before_sha256 != session.current_state_sha256:
            self.edit_sessions.fail_apply(session_id, reason="state_hash_binding_changed")
            raise MetabasePolicyError("Metabase edit-session state binding changed.")
        mutation.target.update(
            {
                "edit_session_id": session.session_id,
                "name": raw.get("name"),
                "archived": raw.get("archived"),
            }
        )

        try:
            session_intent_audit_id = self._audit_edit_session(
                session,
                action="edit_session_apply",
                outcome="intent",
                changed_roots=mutation.changed_roots,
            )
        except (OSError, RuntimeError) as exc:
            self.edit_sessions.fail_apply(session_id, reason="intent_audit_unavailable")
            raise MetabasePolicyError(
                "Metabase edit session could not record its durable pre-write audit."
            ) from exc

        action = (
            Action.QUESTION_UPDATE
            if session.object_type is ObjectType.QUESTION
            else Action.DASHBOARD_UPDATE
        )
        try:
            plan = self.plans.prepare(
                instance=self.config.instance,
                origin=self.config.origin,
                credential_fingerprint=self.config.credential_fingerprint,
                identity_marker=session.identity_marker,
                server_version=session.server_version,
                action=action,
                mutations=[mutation],
                arguments={
                    "edit_session_id": session.session_id,
                    "operations": [
                        item.model_dump(mode="json", exclude_unset=True) for item in operations
                    ],
                },
            )
        except Exception:
            session = self.edit_sessions.fail_apply(
                session_id,
                reason="exact_plan_prepare_failed",
            )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    session,
                    action="edit_session_apply",
                    outcome=Outcome.NOT_APPLIED_VERIFIED.value,
                    changed_roots=mutation.changed_roots,
                )
            raise
        try:
            exact_result = self.exact_action_execute(
                plan.plan_id,
                plan.digest,
                expected_actions={action},
            )
        except Exception:
            session = self.edit_sessions.fail_apply(
                session_id,
                reason="exact_executor_failed",
            )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    session,
                    action="edit_session_apply",
                    outcome=Outcome.OUTCOME_UNKNOWN.value,
                    changed_roots=mutation.changed_roots,
                    rollback_source_plan_id=plan.plan_id,
                )
            raise

        outcome = Outcome(str(exact_result["outcome"]))
        if outcome is Outcome.APPLIED_VERIFIED and mutation.verified_after_sha256:
            session = self.edit_sessions.finish_applied(
                session_id,
                state_sha256=mutation.verified_after_sha256,
            )
            if not exact_result.get("terminal_audit_recorded"):
                session = self.edit_sessions.close(
                    session_id,
                    reason="exact_terminal_audit_unavailable",
                )
            rollback_source_plan_id: str | None = plan.plan_id
        else:
            session = self.edit_sessions.fail_apply(
                session_id,
                reason=f"apply_{outcome.value}",
            )
            rollback_source_plan_id = None

        try:
            session_terminal_audit_id = self._audit_edit_session(
                session,
                action="edit_session_apply",
                outcome=outcome.value,
                state_sha256=(mutation.verified_after_sha256 or current_sha256),
                changed_roots=mutation.changed_roots,
                rollback_source_plan_id=rollback_source_plan_id,
            )
            session_terminal_audit_recorded = True
        except (OSError, RuntimeError):
            session_terminal_audit_id = None
            session_terminal_audit_recorded = False
            if not session.closed:
                session = self.edit_sessions.close(
                    session_id,
                    reason="session_terminal_audit_unavailable",
                )

        return {
            "outcome": outcome.value,
            "changed_roots": list(mutation.changed_roots),
            "session": self.edit_sessions.snapshot(session),
            "session_intent_audit_id": session_intent_audit_id,
            "session_terminal_audit_id": session_terminal_audit_id,
            "session_terminal_audit_recorded": session_terminal_audit_recorded,
            "rollback_source_plan_id": rollback_source_plan_id,
            "exact_action": {
                key: exact_result.get(key)
                for key in (
                    "plan_id",
                    "digest",
                    "outcome",
                    "intent_audit_id",
                    "terminal_audit_id",
                    "terminal_audit_recorded",
                    "object_results",
                )
            },
        }

    @staticmethod
    def _full_session_object_type(value: str) -> ObjectType:
        try:
            object_type = ObjectType(value)
        except ValueError:
            raise MutationValidationError(
                "Metabase sessions support question, dashboard, collection, or field objects."
            ) from None
        if object_type not in {
            ObjectType.QUESTION,
            ObjectType.DASHBOARD,
            ObjectType.COLLECTION,
            ObjectType.FIELD,
        }:
            raise MutationValidationError(
                "Metabase sessions support question, dashboard, collection, or field objects."
            )
        return object_type

    @staticmethod
    def _dashboard_question_ids(state: dict[str, Any]) -> list[int]:
        dashcards = state.get("dashcards", [])
        if not isinstance(dashcards, list):
            raise MutationValidationError(
                "Dashboard dashcards are unavailable for session binding."
            )
        return sorted(
            {
                int(item["card_id"])
                for item in dashcards
                if isinstance(item, dict)
                and type(item.get("card_id")) is int
                and item["card_id"] > 0
            }
        )

    @staticmethod
    def _full_session_question_binding_state(state: dict[str, Any]) -> dict[str, Any]:
        binding_state = copy.deepcopy(state)
        # Saved queries may refresh derived column fingerprints without changing
        # the question definition or its updated_at timestamp.
        binding_state.pop("result_metadata", None)
        return binding_state

    @classmethod
    def _full_session_binding_sha256(
        cls,
        state: dict[str, Any],
        object_type: ObjectType,
    ) -> str:
        if object_type is ObjectType.QUESTION:
            return canonical_sha256(cls._full_session_question_binding_state(state))
        if object_type is not ObjectType.DASHBOARD:
            return canonical_sha256(state)

        binding_state = copy.deepcopy(state)
        for dashcard in binding_state.get("dashcards", []):
            if not isinstance(dashcard, dict):
                continue
            embedded_card = dashcard.get("card")
            if isinstance(embedded_card, dict):
                # Dashboard reads embed volatile card query metadata. Bind the same
                # stable question state used by the linked-question CAS instead.
                question_state = project_state(embedded_card, ObjectType.QUESTION)
                dashcard["card"] = cls._full_session_question_binding_state(question_state)
        return canonical_sha256(binding_state)

    def _full_session_graph(
        self,
        object_type: ObjectType,
        object_id: int,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
        raw = self._object_raw(object_type, object_id)
        state = project_state(raw, object_type)
        validate_state(state, object_type)
        if state["id"] != object_id:
            raise MutationValidationError(
                "Metabase session readback returned a different primary object id."
            )
        primary_key = self.edit_sessions.binding_key(object_type, object_id)
        bindings = {primary_key: self._full_session_binding_sha256(state, object_type)}
        if object_type is not ObjectType.DASHBOARD:
            return raw, state, bindings

        question_ids = self._dashboard_question_ids(state)
        if len(question_ids) + 1 > self.config.max_list_items:
            raise MetabasePolicyError(
                "Dashboard object graph exceeds the configured session binding bound."
            )
        for question_id in question_ids:
            question = self._object_raw(ObjectType.QUESTION, question_id)
            question_state = project_state(question, ObjectType.QUESTION)
            validate_state(question_state, ObjectType.QUESTION)
            key = self.edit_sessions.binding_key(ObjectType.QUESTION, question_id)
            bindings[key] = self._full_session_binding_sha256(
                question_state,
                ObjectType.QUESTION,
            )
        return raw, state, bindings

    @staticmethod
    def _full_session_allowed_scope(object_type: ObjectType) -> list[str]:
        scopes = {
            ObjectType.QUESTION: [
                "metadata and collection placement",
                "SQL/MBQL dataset_query and template tags",
                "parameters and parameter mappings",
                "visualization settings and display",
                "bounded saved or proposed query execution",
            ],
            ObjectType.DASHBOARD: [
                "dashboard metadata and collection placement",
                "tabs, dashcards, parameters, filters, mappings, and layout",
                "visualization and click/cross-filter behavior",
                "all questions linked to the exact dashboard graph",
                "bounded saved or proposed query execution for bound questions",
            ],
            ObjectType.COLLECTION: [
                "collection metadata and parent placement",
            ],
            ObjectType.FIELD: [
                "field metadata, semantic settings, and display settings",
            ],
        }
        return scopes[object_type]

    def _full_session_context(self, session: EditSession, *, action: str) -> dict[str, Any]:
        context = self._health_context()
        identity_marker = context.get("identity_marker")
        if (
            not context["version_supported"]
            or not context["identity_verified"]
            or identity_marker != session.identity_marker
            or context.get("version") != session.server_version
        ):
            closed = self.edit_sessions.fail_apply(
                session.session_id,
                reason="identity_or_version_changed",
            )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    closed,
                    action=action,
                    outcome=Outcome.REJECTED_IDENTITY.value,
                )
            raise MetabasePolicyError(
                "Metabase session identity or exact server-version binding changed."
            )
        return context

    def object_session_open(
        self,
        object_type: str,
        object_id: int,
        *,
        ttl_seconds: int | None = None,
        max_actions: int | None = None,
    ) -> dict[str, Any]:
        selected_type = self._full_session_object_type(object_type)
        selected_id = self._positive_id(object_id, f"{selected_type.value} id")
        context = self._write_context()
        _, state, bindings = self._full_session_graph(selected_type, selected_id)
        if bool(state.get("archived")):
            raise MetabasePolicyError(
                "Archived Metabase objects must be restored by an exact lifecycle action first."
            )
        primary_sha256 = self._full_session_binding_sha256(state, selected_type)
        session = self.edit_sessions.open(
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(context["identity_marker"]),
            server_version=str(context["version"]),
            object_type=selected_type,
            object_id=selected_id,
            state_sha256=primary_sha256,
            object_state_sha256=bindings,
            approval_scope="full_object_graph",
            ttl_seconds=ttl_seconds,
            max_actions=max_actions,
        )
        try:
            audit_id = self._audit_edit_session(
                session,
                action="object_session_open",
                outcome="opened",
            )
        except (OSError, RuntimeError) as exc:
            self.edit_sessions.close(session.session_id, reason="open_audit_unavailable")
            raise MetabasePolicyError(
                "Metabase session could not record its durable open audit."
            ) from exc
        return {
            "opened": True,
            "approval_scope": "full_object_graph",
            "allowed_scope": self._full_session_allowed_scope(selected_type),
            "forbidden_scope": [
                "archive/delete/permanent delete",
                "objects outside the exact bound graph",
                "writes after identity or exact Metabase version drift",
            ],
            "session": self.edit_sessions.snapshot(session),
            "audit_id": audit_id,
        }

    def object_session_status(self, session_id: str) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id)
        if session.approval_scope != "full_object_graph":
            raise MetabasePolicyError("Metabase session is not a full-object session.")
        return {
            "session": self.edit_sessions.snapshot(session),
            "allowed_scope": self._full_session_allowed_scope(session.object_type),
        }

    def object_session_close(self, session_id: str) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id)
        if session.approval_scope != "full_object_graph":
            raise MetabasePolicyError("Metabase session is not a full-object session.")
        return self.edit_session_close(session_id)

    def _parse_full_session_updates(
        self,
        raw_updates: list[dict[str, Any]],
        session: EditSession,
    ) -> list[BatchUpdateItem]:
        if (
            not isinstance(raw_updates, list)
            or not 1 <= len(raw_updates) <= self.config.max_batch_items
        ):
            raise MutationValidationError(
                "Metabase session update batch is outside the configured bound."
            )
        try:
            updates = [BatchUpdateItem.model_validate(item) for item in raw_updates]
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        keys = [
            self.edit_sessions.binding_key(ObjectType(item.object_type), item.object_id)
            for item in updates
        ]
        if len(set(keys)) != len(keys):
            raise MutationValidationError("Metabase session update contains a duplicate object.")
        missing = sorted(set(keys) - set(session.object_state_sha256))
        if missing:
            raise MetabasePolicyError(
                "Metabase session update targets an object outside its exact bound graph."
            )
        for item in updates:
            for operation in item.operations:
                root = operation.path[1:].split("/", 1)[0]
                if root == "archived":
                    raise MetabasePolicyError(
                        "Archive/delete remains a separate exact lifecycle action."
                    )
        return updates

    def _stale_full_session_result(
        self,
        session: EditSession,
        *,
        stale_bindings: list[dict[str, Any]],
        action: str,
    ) -> dict[str, Any]:
        closed = self.edit_sessions.fail_apply(
            session.session_id,
            reason="stale_external_edit",
        )
        try:
            audit_id = self._audit_edit_session(
                closed,
                action=action,
                outcome=Outcome.REJECTED_STALE.value,
            )
            audit_recorded = True
        except (OSError, RuntimeError):
            audit_id = None
            audit_recorded = False
        return {
            "outcome": Outcome.REJECTED_STALE.value,
            "reason": "bound_object_state_changed_outside_session",
            "stale_bindings": stale_bindings,
            "session": self.edit_sessions.snapshot(closed),
            "audit_id": audit_id,
            "audit_recorded": audit_recorded,
            "rollback_source_plan_id": None,
        }

    def object_session_apply(
        self,
        session_id: str,
        raw_updates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id, require_active=True)
        if session.approval_scope != "full_object_graph":
            raise MetabasePolicyError("Metabase session is not a full-object session.")
        updates = self._parse_full_session_updates(raw_updates, session)
        context = self._full_session_context(session, action="object_session_apply")
        session = self.edit_sessions.begin_apply(
            session_id,
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(context["identity_marker"]),
            server_version=str(context["version"]),
        )
        primary_key = self.edit_sessions.binding_key(session.object_type, session.object_id)
        update_keys = {
            self.edit_sessions.binding_key(ObjectType(item.object_type), item.object_id)
            for item in updates
        }
        read_keys = update_keys | {primary_key}
        raw_by_key: dict[str, dict[str, Any]] = {}
        stale_bindings: list[dict[str, Any]] = []
        try:
            for key in sorted(read_keys):
                raw_type, raw_id = key.split(":", 1)
                selected_type = ObjectType(raw_type)
                selected_id = int(raw_id)
                raw = self._object_raw(selected_type, selected_id)
                state = project_state(raw, selected_type)
                current_sha256 = self._full_session_binding_sha256(state, selected_type)
                raw_by_key[key] = raw
                if current_sha256 != session.object_state_sha256[key]:
                    stale_bindings.append(
                        {
                            "object_type": selected_type.value,
                            "object_id": selected_id,
                            "expected_state_sha256": session.object_state_sha256[key],
                            "current_state_sha256": current_sha256,
                        }
                    )
        except MetabaseApiError:
            self.edit_sessions.release_apply(session_id)
            raise
        except (MutationValidationError, ValueError):
            self.edit_sessions.fail_apply(session_id, reason="invalid_current_state")
            raise
        if stale_bindings:
            return self._stale_full_session_result(
                session,
                stale_bindings=stale_bindings,
                action="object_session_apply",
            )

        mutations: list[PlannedMutation] = []
        next_bindings = copy.deepcopy(session.object_state_sha256)
        try:
            for item in updates:
                object_type = ObjectType(item.object_type)
                key = self.edit_sessions.binding_key(object_type, item.object_id)
                mutation = build_mutation(
                    object_type=object_type,
                    raw_before=raw_by_key[key],
                    operations=item.operations,
                )
                mutation.target.update(
                    {
                        "edit_session_id": session.session_id,
                        "name": raw_by_key[key].get("name") or raw_by_key[key].get("display_name"),
                    }
                )
                if object_type is ObjectType.COLLECTION and "parent_id" in mutation.changed_roots:
                    mutation.target["inventory"] = self._inventory_collection_tree(item.object_id)
                mutations.append(mutation)

            if session.object_type is ObjectType.DASHBOARD:
                mutations.sort(
                    key=lambda mutation: (
                        mutation.object_type is not ObjectType.DASHBOARD
                        or mutation.object_id != session.object_id
                    )
                )

            dashboard_mutation = next(
                (
                    mutation
                    for mutation in mutations
                    if session.object_type is ObjectType.DASHBOARD
                    and mutation.object_type is ObjectType.DASHBOARD
                    and mutation.object_id == session.object_id
                ),
                None,
            )
            if dashboard_mutation is not None and dashboard_mutation.after_state is not None:
                desired_question_keys = {
                    self.edit_sessions.binding_key(ObjectType.QUESTION, question_id)
                    for question_id in self._dashboard_question_ids(dashboard_mutation.after_state)
                }
                if len(desired_question_keys) + 1 > self.config.max_list_items:
                    raise MetabasePolicyError(
                        "Dashboard object graph exceeds the configured session binding bound."
                    )
                desired_keys = desired_question_keys | {primary_key}
                for key in sorted(desired_keys - set(next_bindings)):
                    _, raw_id = key.split(":", 1)
                    question = self._object_raw(ObjectType.QUESTION, int(raw_id))
                    question_state = project_state(question, ObjectType.QUESTION)
                    validate_state(question_state, ObjectType.QUESTION)
                    next_bindings[key] = self._full_session_binding_sha256(
                        question_state,
                        ObjectType.QUESTION,
                    )
                next_bindings = {
                    key: state_sha256
                    for key, state_sha256 in next_bindings.items()
                    if key in desired_keys
                }
            session = self.edit_sessions.reserve_bindings(
                session_id,
                object_state_sha256=next_bindings,
            )
        except Exception:
            with suppress(MetabasePolicyError):
                self.edit_sessions.release_apply(session_id)
            raise

        changed_roots = tuple(
            dict.fromkeys(
                f"{mutation.object_type.value}:{mutation.object_id}/{root}"
                for mutation in mutations
                for root in mutation.changed_roots
            )
        )
        try:
            intent_audit_id = self._audit_edit_session(
                session,
                action="object_session_apply",
                outcome="intent",
                changed_roots=changed_roots,
            )
        except (OSError, RuntimeError) as exc:
            self.edit_sessions.fail_apply(session_id, reason="intent_audit_unavailable")
            raise MetabasePolicyError(
                "Metabase session could not record its durable pre-write audit."
            ) from exc

        plan: ExactPlan | None = None
        try:
            plan = self.plans.prepare(
                instance=self.config.instance,
                origin=self.config.origin,
                credential_fingerprint=self.config.credential_fingerprint,
                identity_marker=session.identity_marker,
                server_version=session.server_version,
                action=Action.BATCH,
                mutations=mutations,
                arguments={
                    "object_session_id": session.session_id,
                    "updates": [item.model_dump(mode="json") for item in updates],
                },
            )
            exact_result = self.exact_action_execute(
                plan.plan_id,
                plan.digest,
                expected_actions={Action.BATCH},
            )
        except Exception:
            closed = self.edit_sessions.fail_apply(
                session_id,
                reason="exact_executor_failed",
            )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    closed,
                    action="object_session_apply",
                    outcome=Outcome.OUTCOME_UNKNOWN.value,
                    changed_roots=changed_roots,
                    rollback_source_plan_id=plan.plan_id if plan is not None else None,
                )
            raise

        if plan is None:  # pragma: no cover - defensive narrowing after successful prepare.
            raise MetabasePolicyError("Metabase session exact plan was not prepared.")
        outcome = Outcome(str(exact_result["outcome"]))
        rollback_source_plan_id = (
            plan.plan_id
            if outcome in {Outcome.APPLIED_VERIFIED, Outcome.PARTIALLY_APPLIED}
            else None
        )
        if outcome is Outcome.APPLIED_VERIFIED:
            for mutation in mutations:
                if (
                    mutation.verified_after_state is None
                    or mutation.verified_after_sha256 is None
                    or mutation.object_id is None
                ):
                    self.edit_sessions.fail_apply(
                        session_id,
                        reason="verified_state_binding_missing",
                    )
                    raise MetabasePolicyError(
                        "Metabase session executor returned no verified state binding."
                    )
                key = self.edit_sessions.binding_key(
                    mutation.object_type,
                    mutation.object_id,
                )
                if key in next_bindings:
                    next_bindings[key] = self._full_session_binding_sha256(
                        mutation.verified_after_state,
                        mutation.object_type,
                    )
            continuation_error: str | None = None
            if session.object_type is ObjectType.DASHBOARD:
                try:
                    dashboard = self._object_raw(ObjectType.DASHBOARD, session.object_id)
                    dashboard_state = project_state(dashboard, ObjectType.DASHBOARD)
                    validate_state(dashboard_state, ObjectType.DASHBOARD)
                    observed_keys = {
                        self.edit_sessions.binding_key(ObjectType.DASHBOARD, session.object_id),
                        *(
                            self.edit_sessions.binding_key(ObjectType.QUESTION, question_id)
                            for question_id in self._dashboard_question_ids(dashboard_state)
                        ),
                    }
                    if observed_keys != set(next_bindings):
                        raise MetabasePolicyError(
                            "Dashboard composition readback differs from the reserved "
                            "session graph."
                        )
                    next_bindings[primary_key] = self._full_session_binding_sha256(
                        dashboard_state,
                        ObjectType.DASHBOARD,
                    )
                except (MetabaseApiError, MetabasePolicyError, MutationValidationError):
                    continuation_error = "post_apply_dashboard_graph_readback_failed"
            if continuation_error is None:
                session = self.edit_sessions.finish_applied(
                    session_id,
                    object_state_sha256=next_bindings,
                )
            else:
                session = self.edit_sessions.fail_apply(
                    session_id,
                    reason=continuation_error,
                )
            if not exact_result.get("terminal_audit_recorded") and not session.closed:
                session = self.edit_sessions.close(
                    session_id,
                    reason="exact_terminal_audit_unavailable",
                )
        else:
            session = self.edit_sessions.fail_apply(
                session_id,
                reason=f"apply_{outcome.value}",
            )

        try:
            terminal_audit_id = self._audit_edit_session(
                session,
                action="object_session_apply",
                outcome=outcome.value,
                changed_roots=changed_roots,
                rollback_source_plan_id=rollback_source_plan_id,
            )
            terminal_audit_recorded = True
        except (OSError, RuntimeError):
            terminal_audit_id = None
            terminal_audit_recorded = False
            if not session.closed:
                session = self.edit_sessions.close(
                    session_id,
                    reason="session_terminal_audit_unavailable",
                )
        return {
            "outcome": outcome.value,
            "changed_roots": list(changed_roots),
            "session": self.edit_sessions.snapshot(session),
            "session_intent_audit_id": intent_audit_id,
            "session_terminal_audit_id": terminal_audit_id,
            "session_terminal_audit_recorded": terminal_audit_recorded,
            "rollback_source_plan_id": rollback_source_plan_id,
            "exact_action": exact_result,
        }

    def object_session_query(
        self,
        session_id: str,
        question_id: int,
        *,
        dataset_query: dict[str, Any] | None = None,
        parameters: list[dict[str, Any]] | None = None,
        row_limit: int | None = None,
        ignore_cache: bool = False,
    ) -> dict[str, Any]:
        session = self.edit_sessions.get(session_id, require_active=True)
        if session.approval_scope != "full_object_graph":
            raise MetabasePolicyError("Metabase session is not a full-object session.")
        selected_id = self._positive_id(question_id, "question id")
        question_key = self.edit_sessions.binding_key(ObjectType.QUESTION, selected_id)
        if question_key not in session.object_state_sha256:
            raise MetabasePolicyError(
                "Metabase session query targets a question outside its exact bound graph."
            )
        context = self._full_session_context(session, action="object_session_query")
        session = self.edit_sessions.begin_apply(
            session_id,
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(context["identity_marker"]),
            server_version=str(context["version"]),
        )
        primary_key = self.edit_sessions.binding_key(session.object_type, session.object_id)
        stale_bindings: list[dict[str, Any]] = []
        try:
            for key in sorted({primary_key, question_key}):
                raw_type, raw_id = key.split(":", 1)
                object_type = ObjectType(raw_type)
                raw = self._object_raw(object_type, int(raw_id))
                current_sha256 = self._full_session_binding_sha256(
                    project_state(raw, object_type),
                    object_type,
                )
                if current_sha256 != session.object_state_sha256[key]:
                    stale_bindings.append(
                        {
                            "object_type": object_type.value,
                            "object_id": int(raw_id),
                            "expected_state_sha256": session.object_state_sha256[key],
                            "current_state_sha256": current_sha256,
                        }
                    )
            if stale_bindings:
                return self._stale_full_session_result(
                    session,
                    stale_bindings=stale_bindings,
                    action="object_session_query",
                )
            with suppress(OSError, RuntimeError):
                self._audit_edit_session(
                    session,
                    action="object_session_query",
                    outcome="intent",
                )
            if dataset_query is None:
                query_result = self.question_execute(
                    selected_id,
                    parameters,
                    row_limit=row_limit,
                    ignore_cache=ignore_cache,
                )
                query_kind = "saved_question"
            else:
                query_result = self.question_preview(
                    dataset_query,
                    parameters,
                    row_limit=row_limit,
                )
                query_kind = "proposed_dataset_query"
        except Exception:
            with suppress(MetabasePolicyError):
                self.edit_sessions.release_apply(session_id)
            raise
        session = self.edit_sessions.finish_query(session_id)
        try:
            audit_id = self._audit_edit_session(
                session,
                action="object_session_query",
                outcome="completed",
            )
            audit_recorded = True
        except (OSError, RuntimeError):
            audit_id = None
            audit_recorded = False
        return {
            "outcome": "query_completed",
            "query_kind": query_kind,
            "question_id": selected_id,
            "result": query_result,
            "session": self.edit_sessions.snapshot(session),
            "audit_id": audit_id,
            "audit_recorded": audit_recorded,
        }

    def database_get(self, database_id: int) -> dict[str, Any]:
        payload = self._object_raw(ObjectType.DATABASE, database_id)
        return {"origin": self.config.origin, "database": payload}

    def table_get(self, table_id: int, *, include_fields: bool = True) -> dict[str, Any]:
        table_id = self._positive_id(table_id, "table id")
        suffix = "/query_metadata" if include_fields else ""
        payload = self.http.get_json(f"/api/table/{table_id}{suffix}")
        if not isinstance(payload, dict):
            raise MutationValidationError("Metabase table endpoint returned an invalid shape.")
        return {"origin": self.config.origin, "table": payload}

    def field_get(self, field_id: int) -> dict[str, Any]:
        payload = self._object_raw(ObjectType.FIELD, field_id)
        return {"origin": self.config.origin, "field": payload}

    def field_values_get(self, field_id: int, *, limit: int = 100) -> dict[str, Any]:
        field_id = self._positive_id(field_id, "field id")
        if type(limit) is not int or not 1 <= limit <= self.config.max_list_items:
            raise MutationValidationError(
                "Metabase field-values limit is outside the configured bound."
            )
        payload = self.http.get_json(f"/api/field/{field_id}/values")
        if not isinstance(payload, dict) or not isinstance(payload.get("values", []), list):
            raise MutationValidationError(
                "Metabase field-values endpoint returned an invalid shape."
            )
        values = payload.get("values", [])
        result = copy.deepcopy(payload)
        result["values"] = values[:limit]
        result["truncated"] = len(values) > limit or bool(payload.get("has_more_values"))
        result["origin"] = self.config.origin
        return result

    def question_execute(
        self,
        question_id: int,
        parameters: list[dict[str, Any]] | None = None,
        *,
        row_limit: int | None = None,
        ignore_cache: bool = False,
    ) -> dict[str, Any]:
        question_id = self._positive_id(question_id, "question id")
        selected_limit = self.config.default_query_rows if row_limit is None else row_limit
        if type(selected_limit) is not int or not 1 <= selected_limit <= self.config.max_query_rows:
            raise MutationValidationError(
                "Metabase query row limit is outside the configured bound."
            )
        if parameters is not None and (
            not isinstance(parameters, list)
            or len(parameters) > 100
            or any(not isinstance(item, dict) for item in parameters)
        ):
            raise MutationValidationError(
                "Metabase query parameters must be a bounded object array."
            )
        payload = self.http.query_json(
            f"/api/card/{question_id}/query",
            {"parameters": parameters or [], "ignore_cache": bool(ignore_cache)},
        )
        return self._bounded_query_result(
            payload,
            selected_limit=selected_limit,
            context={"question_id": question_id},
        )

    def question_preview(
        self,
        dataset_query: dict[str, Any],
        parameters: list[dict[str, Any]] | None = None,
        *,
        row_limit: int | None = None,
    ) -> dict[str, Any]:
        selected_limit = self.config.default_query_rows if row_limit is None else row_limit
        if type(selected_limit) is not int or not 1 <= selected_limit <= self.config.max_query_rows:
            raise MutationValidationError(
                "Metabase query row limit is outside the configured bound."
            )
        try:
            request = QuestionPreview.model_validate(
                {
                    "dataset_query": dataset_query,
                    "parameters": [] if parameters is None else parameters,
                }
            )
        except ValidationError as exc:
            raise self._validation_error(exc) from None

        query = copy.deepcopy(request.dataset_query)
        database_id = self._positive_id(query.get("database"), "query database id")
        query_type = query.get("type")
        stages = query.get("stages")
        native_stages = (
            [
                stage
                for stage in stages
                if isinstance(stage, dict) and stage.get("lib/type") == "mbql.stage/native"
            ]
            if isinstance(stages, list)
            else []
        )
        if query_type == "native":
            query_mode = "native"
            query_shape = "legacy"
        elif query.get("lib/type") == "mbql/query" and native_stages:
            query_mode = "native"
            query_shape = "mbql5_native_stage"
        elif query_type == "query" or (
            query_type is None and query.get("lib/type") == "mbql/query"
        ):
            query_mode = "mbql"
            query_shape = "mbql"
        else:
            raise MutationValidationError(
                "Metabase preview accepts only MBQL or native dataset_query objects."
            )

        for reserved_key in ("constraints", "info", "middleware", "parameters", "pretty"):
            query.pop(reserved_key, None)
        if request.parameters:
            query["parameters"] = request.parameters

        context: dict[str, Any] = {
            "database_id": database_id,
            "query_mode": query_mode,
            "query_shape": query_shape,
        }
        if query_mode == "native":
            if query_shape == "legacy":
                native = query.get("native")
                if not isinstance(native, dict) or not isinstance(native.get("query"), str):
                    raise MutationValidationError(
                        "Native dataset_query must contain a native.query string."
                    )
                unexpected_native_keys = set(native) - {"query", "template-tags"}
                if unexpected_native_keys:
                    raise MutationValidationError(
                        "Native dataset_query contains unsupported driver options."
                    )
                tag_containers = [native.get("template-tags", {})]
            else:
                if not native_stages or any(
                    not isinstance(stage.get("native"), str) for stage in native_stages
                ):
                    raise MutationValidationError(
                        "MBQL5 native stages must contain native SQL strings."
                    )
                tag_containers = [stage.get("template-tags", []) for stage in native_stages]
            for template_tags in tag_containers:
                if isinstance(template_tags, dict):
                    if any(not isinstance(value, dict) for value in template_tags.values()):
                        raise MutationValidationError(
                            "Native dataset_query template-tags contain an invalid item."
                        )
                elif isinstance(template_tags, list):
                    if any(
                        not isinstance(value, dict)
                        or not isinstance(value.get("name"), str)
                        or not value["name"]
                        for value in template_tags
                    ):
                        raise MutationValidationError(
                            "Native dataset_query template-tags contain an invalid item."
                        )
                else:
                    raise MutationValidationError(
                        "Native dataset_query template-tags must be an object or array."
                    )
            database = self._object_raw(ObjectType.DATABASE, database_id)
            engine = database.get("engine")
            compiled = self.http.query_json(
                "/api/dataset/native",
                {**copy.deepcopy(query), "pretty": False},
            )
            if not isinstance(compiled, dict) or not isinstance(compiled.get("query"), str):
                raise MutationValidationError(
                    "Metabase native compilation endpoint returned an invalid shape."
                )
            validated = validate_native_preview_sql(engine, compiled["query"])
            context.update(
                {
                    "database_engine": validated.engine,
                    "native_sql_sha256": validated.sql_sha256,
                    "native_sql_validated": True,
                }
            )

        query["constraints"] = {
            "max-results": selected_limit + 1,
            "max-results-bare-rows": selected_limit + 1,
        }
        payload = self.http.query_json("/api/dataset", query)
        return self._bounded_query_result(
            payload,
            selected_limit=selected_limit,
            context=context,
        )

    def _bounded_query_result(
        self,
        payload: Any,
        *,
        selected_limit: int,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise MutationValidationError("Metabase query endpoint returned an invalid shape.")
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("rows", []), list):
            return {
                "origin": self.config.origin,
                **context,
                "status": payload.get("status"),
                "result": payload,
                "rows_returned": 0,
                "truncated": False,
            }
        rows = data.get("rows", [])
        selected = rows[:selected_limit]
        row_count = payload.get("row_count", len(rows))
        return {
            "origin": self.config.origin,
            **context,
            "status": payload.get("status"),
            "row_count": row_count,
            "rows_returned": len(selected),
            "truncated": len(rows) > len(selected)
            or (type(row_count) is int and row_count > len(selected)),
            "data": {
                "cols": data.get("cols", []),
                "rows": selected,
            },
            "running_time": payload.get("running_time"),
        }

    def _collection_baseline(self, collection_id: int | None) -> dict[str, Any]:
        reference: int | str = "root" if collection_id is None else collection_id
        collection = self.http.get_json(self._collection_path(reference))
        if not isinstance(collection, dict):
            raise MutationValidationError("Metabase target collection is unavailable.")
        if collection.get("can_write") is False:
            raise MetabasePolicyError(
                "Metabase API-key subject cannot write the target collection."
            )
        return {
            "collection_id": collection_id,
            "state_sha256": canonical_sha256(collection),
            "can_write": collection.get("can_write"),
        }

    def _prepare_plan(
        self,
        *,
        context: dict[str, Any],
        action: Action,
        mutations: list[PlannedMutation],
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        plan = self.plans.prepare(
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=str(context["identity_marker"]),
            server_version=str(context["version"]),
            action=action,
            mutations=mutations,
            arguments=arguments,
        )
        return {
            "prepared": True,
            "approval_required_for": "execute",
            "plan_id": plan.plan_id,
            "digest": plan.digest,
            "expires_at_epoch": plan.expires_at,
            "instance": plan.instance,
            "origin": plan.origin,
            "credential_fingerprint": plan.credential_fingerprint,
            "identity_marker": plan.identity_marker,
            "server_version": plan.server_version,
            "action": plan.action.value,
            "impact": [mutation_summary(item) for item in plan.mutations],
        }

    @staticmethod
    def _validation_error(exc: ValidationError) -> MutationValidationError:
        return MutationValidationError(
            f"Metabase request validation failed: {exc.errors()[0]['msg']}."
        )

    def question_create_prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        context = self._write_context()
        try:
            request = QuestionCreate.model_validate(body)
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        payload = request.model_dump(mode="json")
        target = self._collection_baseline(request.collection_id)
        target.update({"name": request.name, "create_kind": "question"})
        mutation = PlannedMutation(
            object_type=ObjectType.QUESTION,
            object_id=None,
            before_state=None,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=None,
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.QUESTION_CREATE,
            mutations=[mutation],
            arguments={"body_sha256": canonical_sha256(payload)},
        )

    def question_clone_prepare(
        self,
        source_question_id: int,
        *,
        name: str,
        collection_id: int | None = None,
        to_root: bool = False,
    ) -> dict[str, Any]:
        context = self._write_context()
        source = self._object_raw(ObjectType.QUESTION, source_question_id)
        source_state = project_state(source, ObjectType.QUESTION)
        if to_root and collection_id is not None:
            raise MutationValidationError(
                "Question copy cannot bind both a collection id and the root collection."
            )
        chosen_collection = (
            None
            if to_root
            else source_state.get("collection_id")
            if collection_id is None
            else collection_id
        )
        body = {
            "name": name,
            "dataset_query": source_state.get("dataset_query"),
            "display": source_state.get("display"),
            "visualization_settings": source_state.get("visualization_settings", {}),
            "collection_id": chosen_collection,
            "description": source_state.get("description"),
            "parameters": source_state.get("parameters", []),
            "parameter_mappings": source_state.get("parameter_mappings", []),
            "cache_ttl": source_state.get("cache_ttl"),
            "type": source_state.get("type", "question"),
        }
        try:
            request = QuestionCreate.model_validate(body)
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        payload = request.model_dump(mode="json")
        target = self._collection_baseline(request.collection_id)
        target.update(
            {
                "name": request.name,
                "create_kind": "question_clone",
                "source_id": source_state["id"],
                "source_sha256": canonical_sha256(source_state),
            }
        )
        mutation = PlannedMutation(
            object_type=ObjectType.QUESTION,
            object_id=None,
            before_state=source_state,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=canonical_sha256(source_state),
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.QUESTION_CLONE,
            mutations=[mutation],
            arguments={
                "source_question_id": source_state["id"],
                "body_sha256": canonical_sha256(payload),
            },
        )

    def dashboard_create_prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        context = self._write_context()
        try:
            request = DashboardCreate.model_validate(body)
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        payload = request.model_dump(mode="json")
        for dashcard in payload.get("dashcards", []):
            if isinstance(dashcard, dict):
                dashcard.pop("card", None)
        # Validate owned element compatibility against exact live card definitions,
        # without requiring callers to send server-owned embedded card payloads.
        candidate = {"id": 1, "archived": False, **copy.deepcopy(payload)}
        question_bindings: list[dict[str, Any]] = []
        questions: dict[int, dict[str, Any]] = {}
        for dashcard in candidate.get("dashcards", []):
            if not isinstance(dashcard, dict):
                continue
            card_id = dashcard.get("card_id")
            if type(card_id) is not int or card_id <= 0:
                continue
            if card_id not in questions:
                raw_question = self._object_raw(ObjectType.QUESTION, card_id)
                question_state = project_state(raw_question, ObjectType.QUESTION)
                questions[card_id] = question_state
                question_bindings.append(
                    {
                        "question_id": card_id,
                        "state_sha256": canonical_sha256(question_state),
                    }
                )
            dashcard["card"] = copy.deepcopy(questions[card_id])
        validate_state(candidate, ObjectType.DASHBOARD)
        target = self._collection_baseline(request.collection_id)
        target.update(
            {
                "name": request.name,
                "create_kind": "dashboard",
                "question_bindings": question_bindings,
            }
        )
        mutation = PlannedMutation(
            object_type=ObjectType.DASHBOARD,
            object_id=None,
            before_state=None,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=None,
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.DASHBOARD_CREATE,
            mutations=[mutation],
            arguments={"body_sha256": canonical_sha256(payload)},
        )

    def dashboard_clone_prepare(
        self,
        source_dashboard_id: int,
        *,
        name: str | None = None,
        collection_id: int | None = None,
        is_deep_copy: bool = False,
        to_root: bool = False,
    ) -> dict[str, Any]:
        context = self._write_context()
        source = self._object_raw(ObjectType.DASHBOARD, source_dashboard_id)
        source_state = project_state(source, ObjectType.DASHBOARD)
        if to_root and collection_id is not None:
            raise MutationValidationError(
                "Dashboard copy cannot bind both a collection id and the root collection."
            )
        chosen_collection = (
            None
            if to_root
            else source_state.get("collection_id")
            if collection_id is None
            else collection_id
        )
        chosen_name = source_state.get("name") if name is None else name
        if not isinstance(chosen_name, str) or not chosen_name.strip():
            raise MutationValidationError("Dashboard clone name must be non-empty.")
        payload = {
            "name": chosen_name,
            "description": source_state.get("description"),
            "collection_id": chosen_collection,
            "is_deep_copy": bool(is_deep_copy),
        }
        target = self._collection_baseline(chosen_collection)
        target.update(
            {
                "name": chosen_name,
                "create_kind": "dashboard_clone",
                "source_id": source_state["id"],
                "source_sha256": canonical_sha256(source_state),
                "expected_dashcard_count": len(source_state.get("dashcards", [])),
                "expected_tab_count": len(source_state.get("tabs", [])),
                "is_deep_copy": bool(is_deep_copy),
            }
        )
        mutation = PlannedMutation(
            object_type=ObjectType.DASHBOARD,
            object_id=None,
            before_state=source_state,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=canonical_sha256(source_state),
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.DASHBOARD_CLONE,
            mutations=[mutation],
            arguments={
                "source_dashboard_id": source_state["id"],
                "body_sha256": canonical_sha256(payload),
            },
        )

    def collection_create_prepare(self, body: dict[str, Any]) -> dict[str, Any]:
        context = self._write_context()
        try:
            request = CollectionCreate.model_validate(body)
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        payload = request.model_dump(mode="json")
        target = self._collection_baseline(request.parent_id)
        target.update({"name": request.name, "create_kind": "collection"})
        mutation = PlannedMutation(
            object_type=ObjectType.COLLECTION,
            object_id=None,
            before_state=None,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=None,
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.COLLECTION_CREATE,
            mutations=[mutation],
            arguments={"body_sha256": canonical_sha256(payload)},
        )

    def collection_clone_prepare(
        self,
        source_collection_id: int,
        *,
        name: str,
        parent_id: int | None = None,
        to_root: bool = False,
    ) -> dict[str, Any]:
        context = self._write_context()
        source = self._object_raw(ObjectType.COLLECTION, source_collection_id)
        source_state = project_state(source, ObjectType.COLLECTION)
        if to_root and parent_id is not None:
            raise MutationValidationError(
                "Collection copy cannot bind both a parent id and the root collection."
            )
        chosen_parent = (
            None if to_root else source_state.get("parent_id") if parent_id is None else parent_id
        )
        try:
            request = CollectionCreate.model_validate(
                {
                    "name": name,
                    "description": source_state.get("description"),
                    "parent_id": chosen_parent,
                }
            )
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        payload = request.model_dump(mode="json")
        target = self._collection_baseline(request.parent_id)
        target.update(
            {
                "name": request.name,
                "create_kind": "collection_clone_shallow",
                "source_id": source_state["id"],
                "source_sha256": canonical_sha256(source_state),
            }
        )
        mutation = PlannedMutation(
            object_type=ObjectType.COLLECTION,
            object_id=None,
            before_state=source_state,
            after_state=copy.deepcopy(payload),
            write_payload=payload,
            changed_roots=tuple(payload),
            before_sha256=canonical_sha256(source_state),
            after_sha256=canonical_sha256(payload),
            target=target,
        )
        return self._prepare_plan(
            context=context,
            action=Action.COLLECTION_CLONE,
            mutations=[mutation],
            arguments={
                "source_collection_id": source_state["id"],
                "body_sha256": canonical_sha256(payload),
            },
        )

    @staticmethod
    def _parse_operations(raw_operations: list[dict[str, Any]]) -> list[PatchOperation]:
        if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= 100:
            raise MutationValidationError(
                "Metabase patch operations must contain between 1 and 100 items."
            )
        try:
            return [PatchOperation.model_validate(item) for item in raw_operations]
        except ValidationError as exc:
            raise MutationValidationError(
                f"Metabase patch validation failed: {exc.errors()[0]['msg']}."
            ) from None

    def _inventory_collection_tree(self, collection_id: int) -> dict[str, Any]:
        root_id = self._positive_id(collection_id, "collection id")
        pending = [root_id]
        visited: set[int] = set()
        items: list[dict[str, Any]] = []
        while pending:
            current_id = pending.pop()
            if current_id in visited:
                raise MutationValidationError("Collection tree contains a repeated collection id.")
            visited.add(current_id)
            collection = self._object_raw(ObjectType.COLLECTION, current_id)
            archived = bool(collection.get("archived"))
            payload = self.http.get_json(
                self._collection_path(current_id, items=True),
                params={
                    "archived": archived,
                    "limit": self.config.max_list_items,
                    "offset": 0,
                },
            )
            direct, total = self._list_payload(payload)
            if len(direct) > self.config.max_list_items or (
                total is not None and total > len(direct)
            ):
                raise MutationValidationError(
                    "Collection tree exceeds the exact inventory bound; narrow the operation."
                )
            for item in direct:
                item_id = item.get("id")
                model = item.get("model")
                if type(item_id) is not int or item_id <= 0 or not isinstance(model, str):
                    raise MutationValidationError(
                        "Collection inventory item has no exact model/id."
                    )
                items.append({"model": model, "id": item_id})
                if model == "collection":
                    pending.append(item_id)
                if len(items) + len(visited) > self.config.max_list_items:
                    raise MutationValidationError(
                        "Collection tree exceeds the exact inventory bound; narrow the operation."
                    )
        inventory = {
            "root_collection_id": root_id,
            "collections": sorted(visited),
            "items": sorted(items, key=lambda item: (str(item["model"]), int(item["id"]))),
        }
        inventory["sha256"] = canonical_sha256(inventory)
        return inventory

    def _update_prepare(
        self,
        *,
        object_type: ObjectType,
        object_id: int,
        raw_operations: list[dict[str, Any]],
        action: Action,
    ) -> dict[str, Any]:
        context = self._write_context()
        operations = self._parse_operations(raw_operations)
        raw = self._object_raw(object_type, object_id)
        mutation = build_mutation(
            object_type=object_type,
            raw_before=raw,
            operations=operations,
        )
        mutation.target.update(
            {
                "name": raw.get("name") or raw.get("display_name"),
                "archived": raw.get("archived"),
            }
        )
        if object_type is ObjectType.QUESTION:
            mutation.target["dashboard_count"] = raw.get("dashboard_count")
        if object_type is ObjectType.DASHBOARD:
            mutation.target.update(
                {
                    "dashcard_count": len(raw.get("dashcards", []) or []),
                    "tab_count": len(raw.get("tabs", []) or []),
                    "parameter_count": len(raw.get("parameters", []) or []),
                }
            )
        if object_type is ObjectType.COLLECTION and (
            "archived" in mutation.changed_roots or "parent_id" in mutation.changed_roots
        ):
            mutation.target["inventory"] = self._inventory_collection_tree(object_id)
        arguments = {
            "object_type": object_type.value,
            "object_id": object_id,
            "operations": [item.model_dump(mode="json", exclude_unset=True) for item in operations],
        }
        return self._prepare_plan(
            context=context,
            action=action,
            mutations=[mutation],
            arguments=arguments,
        )

    def question_update_prepare(
        self, question_id: int, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.QUESTION,
            object_id=question_id,
            raw_operations=operations,
            action=Action.QUESTION_UPDATE,
        )

    def question_trash_prepare(self, question_id: int) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.QUESTION,
            object_id=question_id,
            raw_operations=[{"op": "set", "path": "/archived", "value": True}],
            action=Action.QUESTION_TRASH,
        )

    def question_restore_prepare(
        self,
        question_id: int,
        *,
        collection_id: int | None = None,
        to_root: bool = False,
    ) -> dict[str, Any]:
        operations: list[dict[str, Any]] = [{"op": "set", "path": "/archived", "value": False}]
        if to_root and collection_id is not None:
            raise MutationValidationError(
                "Question restore cannot bind both a collection id and the root collection."
            )
        if to_root:
            operations.append({"op": "set", "path": "/collection_id", "value": None})
        elif collection_id is not None:
            operations.append({"op": "set", "path": "/collection_id", "value": collection_id})
        return self._update_prepare(
            object_type=ObjectType.QUESTION,
            object_id=question_id,
            raw_operations=operations,
            action=Action.QUESTION_RESTORE,
        )

    def dashboard_update_prepare(
        self, dashboard_id: int, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.DASHBOARD,
            object_id=dashboard_id,
            raw_operations=operations,
            action=Action.DASHBOARD_UPDATE,
        )

    def dashboard_trash_prepare(self, dashboard_id: int) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.DASHBOARD,
            object_id=dashboard_id,
            raw_operations=[{"op": "set", "path": "/archived", "value": True}],
            action=Action.DASHBOARD_TRASH,
        )

    def dashboard_restore_prepare(
        self,
        dashboard_id: int,
        *,
        collection_id: int | None = None,
        to_root: bool = False,
    ) -> dict[str, Any]:
        operations: list[dict[str, Any]] = [{"op": "set", "path": "/archived", "value": False}]
        if to_root and collection_id is not None:
            raise MutationValidationError(
                "Dashboard restore cannot bind both a collection id and the root collection."
            )
        if to_root:
            operations.append({"op": "set", "path": "/collection_id", "value": None})
        elif collection_id is not None:
            operations.append({"op": "set", "path": "/collection_id", "value": collection_id})
        return self._update_prepare(
            object_type=ObjectType.DASHBOARD,
            object_id=dashboard_id,
            raw_operations=operations,
            action=Action.DASHBOARD_RESTORE,
        )

    def collection_update_prepare(
        self, collection_id: int, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.COLLECTION,
            object_id=collection_id,
            raw_operations=operations,
            action=Action.COLLECTION_UPDATE,
        )

    def collection_trash_prepare(self, collection_id: int) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.COLLECTION,
            object_id=collection_id,
            raw_operations=[{"op": "set", "path": "/archived", "value": True}],
            action=Action.COLLECTION_TRASH,
        )

    def collection_restore_prepare(
        self,
        collection_id: int,
        *,
        parent_id: int | None = None,
        to_root: bool = False,
    ) -> dict[str, Any]:
        operations: list[dict[str, Any]] = [{"op": "set", "path": "/archived", "value": False}]
        if to_root and parent_id is not None:
            raise MutationValidationError(
                "Collection restore cannot bind both a parent id and the root collection."
            )
        if to_root:
            operations.append({"op": "set", "path": "/parent_id", "value": None})
        elif parent_id is not None:
            operations.append({"op": "set", "path": "/parent_id", "value": parent_id})
        return self._update_prepare(
            object_type=ObjectType.COLLECTION,
            object_id=collection_id,
            raw_operations=operations,
            action=Action.COLLECTION_RESTORE,
        )

    def field_update_prepare(
        self, field_id: int, operations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        return self._update_prepare(
            object_type=ObjectType.FIELD,
            object_id=field_id,
            raw_operations=operations,
            action=Action.FIELD_UPDATE,
        )

    def field_values_rescan_prepare(self, database_id: int) -> dict[str, Any]:
        context = self._write_context()
        raw = self._object_raw(ObjectType.DATABASE, database_id)
        state = project_state(raw, ObjectType.DATABASE)
        mutation = PlannedMutation(
            object_type=ObjectType.DATABASE,
            object_id=database_id,
            before_state=state,
            after_state=None,
            write_payload={},
            changed_roots=(),
            before_sha256=canonical_sha256(state),
            after_sha256=None,
            target={
                "database_name": raw.get("name"),
                "effect": "queue_rescan_for_all_cached_field_values_in_database",
            },
        )
        return self._prepare_plan(
            context=context,
            action=Action.FIELD_VALUES_RESCAN,
            mutations=[mutation],
            arguments={"database_id": database_id},
        )

    @staticmethod
    def _dashboard_linked_question_ids(mutation: PlannedMutation) -> set[int]:
        linked: set[int] = set()
        for state in (mutation.before_state, mutation.after_state):
            if not isinstance(state, dict):
                continue
            for dashcard in state.get("dashcards", []) or []:
                if not isinstance(dashcard, dict):
                    continue
                card_id = dashcard.get("card_id")
                if type(card_id) is int and card_id > 0:
                    linked.add(card_id)
        return linked

    @classmethod
    def _dependency_safe_batch_order(cls, mutations: list[PlannedMutation]) -> list[int]:
        question_indexes = {
            mutation.object_id: index
            for index, mutation in enumerate(mutations)
            if mutation.object_type is ObjectType.QUESTION and mutation.object_id is not None
        }
        dependencies = [set() for _ in mutations]
        for dashboard_index, mutation in enumerate(mutations):
            if mutation.object_type is not ObjectType.DASHBOARD:
                continue
            for question_id in cls._dashboard_linked_question_ids(mutation):
                question_index = question_indexes.get(question_id)
                if question_index is not None:
                    dependencies[question_index].add(dashboard_index)

        ordered: list[int] = []
        visiting: set[int] = set()
        visited: set[int] = set()

        def visit(index: int) -> None:
            if index in visited:
                return
            if index in visiting:
                raise MetabasePolicyError("Metabase batch dependency graph contains a cycle.")
            visiting.add(index)
            for dependency in sorted(dependencies[index]):
                visit(dependency)
            visiting.remove(index)
            visited.add(index)
            ordered.append(index)

        for index in range(len(mutations)):
            visit(index)
        return ordered

    def batch_prepare(self, raw_items: list[dict[str, Any]]) -> dict[str, Any]:
        context = self._write_context()
        if (
            not isinstance(raw_items, list)
            or not 1 <= len(raw_items) <= self.config.max_batch_items
        ):
            raise MutationValidationError("Metabase batch size is outside the configured bound.")
        try:
            items = [BatchUpdateItem.model_validate(item) for item in raw_items]
        except ValidationError as exc:
            raise self._validation_error(exc) from None
        bindings = [(item.object_type, item.object_id) for item in items]
        if len(set(bindings)) != len(bindings):
            raise MutationValidationError("Metabase batch contains a duplicate object binding.")
        mutations: list[PlannedMutation] = []
        for item in items:
            object_type = ObjectType(item.object_type)
            raw = self._object_raw(object_type, item.object_id)
            mutation = build_mutation(
                object_type=object_type,
                raw_before=raw,
                operations=item.operations,
            )
            mutation.target["name"] = raw.get("name") or raw.get("display_name")
            if object_type is ObjectType.COLLECTION and (
                "archived" in mutation.changed_roots or "parent_id" in mutation.changed_roots
            ):
                mutation.target["inventory"] = self._inventory_collection_tree(item.object_id)
            mutations.append(mutation)
        order = self._dependency_safe_batch_order(mutations)
        mutations = [mutations[index] for index in order]
        items = [items[index] for index in order]
        arguments = {
            "items": [
                {
                    "object_type": item.object_type,
                    "object_id": item.object_id,
                    "operations": [
                        operation.model_dump(mode="json", exclude_unset=True)
                        for operation in item.operations
                    ],
                }
                for item in items
            ]
        }
        return self._prepare_plan(
            context=context,
            action=Action.BATCH,
            mutations=mutations,
            arguments=arguments,
        )

    @staticmethod
    def _closed_action_arguments(
        requested_action: str,
        selected_action: Action,
        arguments: dict[str, Any],
    ) -> None:
        required, optional = COMPACT_ACTION_ARGUMENT_KEYS[selected_action]
        expected = (
            f"required keys [{', '.join(sorted(required)) or 'none'}]; "
            f"optional keys [{', '.join(sorted(optional)) or 'none'}]"
        )
        if not isinstance(arguments, dict):
            raise MutationValidationError(
                f"Metabase action {requested_action} arguments must be an object; {expected}."
            )
        missing = required - set(arguments)
        unknown = set(arguments) - required - optional
        if missing or unknown:
            problems = []
            if missing:
                problems.append(f"missing [{', '.join(sorted(missing))}]")
            if unknown:
                problems.append(f"unknown [{', '.join(sorted(unknown))}]")
            hint = (
                " Create payloads must be nested under arguments.body."
                if "body" in required
                else ""
            )
            raise MutationValidationError(
                f"Metabase action {requested_action} arguments are invalid; {expected}; "
                f"{'; '.join(problems)}.{hint}"
            )

    @staticmethod
    def _reject_lifecycle_patch(raw_operations: Any) -> None:
        if not isinstance(raw_operations, list):
            return
        for operation in raw_operations:
            if not isinstance(operation, dict):
                continue
            path = operation.get("path")
            if isinstance(path, str) and path[1:].split("/", 1)[0] == "archived":
                raise MetabasePolicyError("Archive/delete must use a named exact lifecycle action.")

    @classmethod
    def _compact_action(cls, value: str) -> Action:
        aliases = {
            "question_copy": Action.QUESTION_CLONE.value,
            "question_delete": Action.QUESTION_TRASH.value,
            "dashboard_copy": Action.DASHBOARD_CLONE.value,
            "dashboard_delete": Action.DASHBOARD_TRASH.value,
            "collection_copy": Action.COLLECTION_CLONE.value,
            "collection_delete": Action.COLLECTION_TRASH.value,
            "batch_update": Action.BATCH.value,
        }
        try:
            action = Action(aliases.get(value, value))
        except (TypeError, ValueError):
            raise MutationValidationError(
                "Metabase action is outside the closed typed allowlist."
            ) from None
        if action not in COMPACT_MUTATION_ACTIONS:
            raise MutationValidationError(
                "Metabase action is outside the compact mutation allowlist."
            )
        return action

    def action_prepare(self, action: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Prepare any supported typed mutation through one compact dispatcher."""

        selected = self._compact_action(action)
        self._closed_action_arguments(action, selected, arguments)
        if selected is Action.QUESTION_CREATE:
            result = self.question_create_prepare(arguments["body"])
        elif selected is Action.QUESTION_CLONE:
            result = self.question_clone_prepare(
                arguments["source_question_id"],
                name=arguments["name"],
                collection_id=arguments.get("collection_id"),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.QUESTION_UPDATE:
            self._reject_lifecycle_patch(arguments["operations"])
            result = self.question_update_prepare(arguments["question_id"], arguments["operations"])
        elif selected is Action.QUESTION_TRASH:
            result = self.question_trash_prepare(arguments["question_id"])
        elif selected is Action.QUESTION_RESTORE:
            result = self.question_restore_prepare(
                arguments["question_id"],
                collection_id=arguments.get("collection_id"),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.DASHBOARD_CREATE:
            result = self.dashboard_create_prepare(arguments["body"])
        elif selected is Action.DASHBOARD_CLONE:
            result = self.dashboard_clone_prepare(
                arguments["source_dashboard_id"],
                name=arguments.get("name"),
                collection_id=arguments.get("collection_id"),
                is_deep_copy=arguments.get("is_deep_copy", False),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.DASHBOARD_UPDATE:
            self._reject_lifecycle_patch(arguments["operations"])
            result = self.dashboard_update_prepare(
                arguments["dashboard_id"], arguments["operations"]
            )
        elif selected is Action.DASHBOARD_TRASH:
            result = self.dashboard_trash_prepare(arguments["dashboard_id"])
        elif selected is Action.DASHBOARD_RESTORE:
            result = self.dashboard_restore_prepare(
                arguments["dashboard_id"],
                collection_id=arguments.get("collection_id"),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.COLLECTION_CREATE:
            result = self.collection_create_prepare(arguments["body"])
        elif selected is Action.COLLECTION_CLONE:
            result = self.collection_clone_prepare(
                arguments["source_collection_id"],
                name=arguments["name"],
                parent_id=arguments.get("parent_id"),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.COLLECTION_UPDATE:
            self._reject_lifecycle_patch(arguments["operations"])
            result = self.collection_update_prepare(
                arguments["collection_id"], arguments["operations"]
            )
        elif selected is Action.COLLECTION_TRASH:
            result = self.collection_trash_prepare(arguments["collection_id"])
        elif selected is Action.COLLECTION_RESTORE:
            result = self.collection_restore_prepare(
                arguments["collection_id"],
                parent_id=arguments.get("parent_id"),
                to_root=arguments.get("to_root", False),
            )
        elif selected is Action.FIELD_UPDATE:
            self._reject_lifecycle_patch(arguments["operations"])
            result = self.field_update_prepare(arguments["field_id"], arguments["operations"])
        elif selected is Action.FIELD_VALUES_RESCAN:
            result = self.field_values_rescan_prepare(arguments["database_id"])
        else:
            for item in arguments["items"] if isinstance(arguments["items"], list) else []:
                if isinstance(item, dict):
                    self._reject_lifecycle_patch(item.get("operations"))
            result = self.batch_prepare(arguments["items"])
        return {"requested_action": action, **result}

    def action_execute(
        self,
        plan_id: str,
        digest: str,
        *,
        open_session: bool = True,
    ) -> dict[str, Any]:
        """Execute one compact typed plan and open a work session when unambiguous."""

        plan = self.plans.peek(plan_id)
        if plan.action not in COMPACT_MUTATION_ACTIONS:
            raise MetabasePolicyError(
                "Metabase exact plan action does not match the compact execute tool."
            )
        result = self.exact_action_execute(
            plan_id,
            digest,
            expected_actions=set(COMPACT_MUTATION_ACTIONS),
        )
        if not open_session or result.get("outcome") != Outcome.APPLIED_VERIFIED.value:
            return result
        if plan.action in {
            Action.QUESTION_TRASH,
            Action.DASHBOARD_TRASH,
            Action.COLLECTION_TRASH,
            Action.FIELD_VALUES_RESCAN,
            Action.BATCH,
        }:
            return {**result, "work_session": None}
        object_results = result.get("object_results")
        candidates = (
            [
                item
                for item in object_results
                if isinstance(item, dict)
                and item.get("object_type") in {"question", "dashboard", "collection", "field"}
                and type(item.get("object_id")) is int
                and item.get("outcome") == Outcome.APPLIED_VERIFIED.value
            ]
            if isinstance(object_results, list)
            else []
        )
        if len(candidates) != 1:
            return {**result, "work_session": None}
        candidate = candidates[0]
        object_type = ObjectType(str(candidate["object_type"]))
        object_id = int(candidate["object_id"])
        existing = self.edit_sessions.active_for_object(
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=plan.identity_marker,
            object_type=object_type,
            object_id=object_id,
        )
        if existing is not None:
            self.edit_sessions.close(
                existing.session_id,
                reason="superseded_by_exact_action",
            )
        try:
            work_session = self.object_session_open(object_type.value, object_id)
        except (MetabaseApiError, MetabasePolicyError, MutationValidationError) as exc:
            work_session = {
                "opened": False,
                "reason": "automatic_session_open_failed",
                "detail": str(exc),
            }
        return {**result, "work_session": work_session}

    @staticmethod
    def _plan_object_ids(plan: ExactPlan) -> list[int]:
        return sorted(
            {
                mutation.object_id
                for mutation in plan.mutations
                if type(mutation.object_id) is int and mutation.object_id > 0
            }
        )

    def _audit_plan(self, plan: ExactPlan, outcome: str) -> str:
        return self.audit.write(
            {
                "action": plan.action.value,
                "credential_fingerprint": plan.credential_fingerprint,
                "digest": plan.digest,
                "identity_marker": plan.identity_marker,
                "server_version": plan.server_version,
                "instance": plan.instance,
                "object_ids": self._plan_object_ids(plan),
                "origin": plan.origin,
                "outcome": outcome,
                "plan_id": plan.plan_id,
            }
        )

    def _complete_execution(
        self,
        plan: ExactPlan,
        outcome: Outcome,
        details: dict[str, Any],
        *,
        intent_audit_id: str | None,
    ) -> dict[str, Any]:
        result = {
            "plan_id": plan.plan_id,
            "digest": plan.digest,
            "instance": plan.instance,
            "origin": plan.origin,
            "action": plan.action.value,
            "outcome": outcome.value,
            "intent_audit_id": intent_audit_id,
            **details,
        }
        self.plans.complete(plan.plan_id, outcome, result)
        try:
            result["terminal_audit_id"] = self._audit_plan(plan, outcome.value)
            result["terminal_audit_recorded"] = True
        except (OSError, RuntimeError):
            # The caller still needs the reconciled remote outcome if disk state
            # changed after the durable pre-write intent record was created.
            result["terminal_audit_id"] = None
            result["terminal_audit_recorded"] = False
        return result

    def exact_action_revoke(self, plan_id: str) -> dict[str, Any]:
        context = self._write_context()
        plan = self.plans.peek(plan_id)
        if context["identity_marker"] != plan.identity_marker:
            raise MetabasePolicyError("Metabase exact plan identity binding changed.")
        if context.get("version") != plan.server_version:
            raise MetabasePolicyError("Metabase exact plan server-version binding changed.")
        self.plans.revoke(plan_id, identity_marker=str(context["identity_marker"]))
        audit_id = self._audit_plan(plan, "revoked")
        return {
            "plan_id": plan.plan_id,
            "digest": plan.digest,
            "action": plan.action.value,
            "revoked": True,
            "audit_id": audit_id,
        }

    def _mutation_preflight(self, mutation: PlannedMutation) -> tuple[bool, dict[str, Any] | None]:
        if mutation.object_id is None or mutation.before_sha256 is None:
            return False, None
        current_raw = self._object_raw(mutation.object_type, mutation.object_id)
        current = project_state(current_raw, mutation.object_type)
        if canonical_sha256(current) != mutation.before_sha256:
            return False, current_raw
        expected_inventory = mutation.target.get("inventory")
        if expected_inventory is not None:
            if mutation.object_type is not ObjectType.COLLECTION:
                raise MetabasePolicyError("Collection inventory was bound to a non-collection.")
            current_inventory = self._inventory_collection_tree(mutation.object_id)
            if canonical_sha256(current_inventory) != canonical_sha256(expected_inventory):
                return False, current_raw
        return True, current_raw

    @staticmethod
    def _update_path(mutation: PlannedMutation) -> str:
        if mutation.object_id is None:
            raise MutationValidationError("Metabase update mutation has no object id.")
        prefixes = {
            ObjectType.QUESTION: "card",
            ObjectType.DASHBOARD: "dashboard",
            ObjectType.COLLECTION: "collection",
            ObjectType.FIELD: "field",
        }
        prefix = prefixes.get(mutation.object_type)
        if prefix is None:
            raise MutationValidationError("Metabase object type has no update endpoint.")
        return f"/api/{prefix}/{mutation.object_id}"

    def _reconcile_update(
        self,
        mutation: PlannedMutation,
        *,
        request_error: MetabaseApiError | None,
    ) -> dict[str, Any]:
        if request_error is not None and not request_error.outcome_unknown:
            return {
                "object_type": mutation.object_type.value,
                "object_id": mutation.object_id,
                "outcome": Outcome.REJECTED_VALIDATION.value,
                "http_status": request_error.status_code,
                "verified_after_sha256": None,
            }
        try:
            if mutation.object_id is None:
                raise MutationValidationError("Metabase update mutation has no object id.")
            readback = self._object_raw(mutation.object_type, mutation.object_id)
            readback_state = project_state(readback, mutation.object_type)
        except (MetabaseApiError, MutationValidationError):
            return {
                "object_type": mutation.object_type.value,
                "object_id": mutation.object_id,
                "outcome": Outcome.OUTCOME_UNKNOWN.value,
                "http_status": request_error.status_code if request_error else None,
                "verified_after_sha256": None,
            }
        readback_sha256 = canonical_sha256(readback_state)
        if verify_mutation(mutation, readback):
            mutation.verified_after_state = readback_state
            mutation.verified_after_sha256 = readback_sha256
            return {
                "object_type": mutation.object_type.value,
                "object_id": mutation.object_id,
                "outcome": Outcome.APPLIED_VERIFIED.value,
                "http_status": request_error.status_code if request_error else None,
                "verified_after_sha256": readback_sha256,
            }
        if readback_sha256 == mutation.before_sha256:
            return {
                "object_type": mutation.object_type.value,
                "object_id": mutation.object_id,
                "outcome": Outcome.NOT_APPLIED_VERIFIED.value,
                "http_status": request_error.status_code if request_error else None,
                "verified_after_sha256": readback_sha256,
            }
        return {
            "object_type": mutation.object_type.value,
            "object_id": mutation.object_id,
            "outcome": Outcome.OUTCOME_UNKNOWN.value,
            "http_status": request_error.status_code if request_error else None,
            "verified_after_sha256": readback_sha256,
        }

    def _apply_update(self, mutation: PlannedMutation) -> dict[str, Any]:
        try:
            self.http.put_json(self._update_path(mutation), mutation.write_payload)
        except MetabaseApiError as exc:
            return self._reconcile_update(mutation, request_error=exc)
        return self._reconcile_update(mutation, request_error=None)

    def _execute_single_update(self, mutation: PlannedMutation) -> tuple[Outcome, dict[str, Any]]:
        fresh, _ = self._mutation_preflight(mutation)
        if not fresh:
            return Outcome.REJECTED_STALE, {
                "object_results": [
                    {
                        "object_type": mutation.object_type.value,
                        "object_id": mutation.object_id,
                        "outcome": Outcome.REJECTED_STALE.value,
                    }
                ],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        item_result = self._apply_update(mutation)
        outcome = Outcome(str(item_result["outcome"]))
        applied = outcome is Outcome.APPLIED_VERIFIED
        return outcome, {
            "object_results": [item_result],
            "applied_indexes": [0] if applied else [],
            "rollback_candidates": (
                [
                    {
                        "object_type": mutation.object_type.value,
                        "object_id": mutation.object_id,
                    }
                ]
                if applied
                else []
            ),
        }

    def _execute_batch(self, plan: ExactPlan) -> tuple[Outcome, dict[str, Any]]:
        for index, mutation in enumerate(plan.mutations):
            fresh, _ = self._mutation_preflight(mutation)
            if not fresh:
                return Outcome.REJECTED_STALE, {
                    "object_results": [
                        {
                            "object_type": mutation.object_type.value,
                            "object_id": mutation.object_id,
                            "outcome": Outcome.REJECTED_STALE.value,
                            "index": index,
                        }
                    ],
                    "stale_index": index,
                    "applied_indexes": [],
                    "rollback_candidates": [],
                }

        object_results: list[dict[str, Any]] = []
        applied_indexes: list[int] = []
        terminal_failure: Outcome | None = None
        for index, mutation in enumerate(plan.mutations):
            fresh, _ = self._mutation_preflight(mutation)
            if not fresh:
                object_results.append(
                    {
                        "object_type": mutation.object_type.value,
                        "object_id": mutation.object_id,
                        "outcome": Outcome.REJECTED_STALE.value,
                        "index": index,
                    }
                )
                terminal_failure = Outcome.REJECTED_STALE
                break
            item_result = self._apply_update(mutation)
            item_result["index"] = index
            object_results.append(item_result)
            item_outcome = Outcome(str(item_result["outcome"]))
            if item_outcome is Outcome.APPLIED_VERIFIED:
                applied_indexes.append(index)
                continue
            terminal_failure = item_outcome
            break

        if len(applied_indexes) == len(plan.mutations):
            overall = Outcome.APPLIED_VERIFIED
        elif applied_indexes:
            overall = Outcome.PARTIALLY_APPLIED
        else:
            overall = terminal_failure or Outcome.NOT_APPLIED_VERIFIED
        return overall, {
            "object_results": object_results,
            "applied_indexes": applied_indexes,
            "stopped_after_index": len(object_results) - 1,
            "unattempted_indexes": list(range(len(object_results), len(plan.mutations))),
            "rollback_candidates": [
                {
                    "index": index,
                    "object_type": plan.mutations[index].object_type.value,
                    "object_id": plan.mutations[index].object_id,
                }
                for index in applied_indexes
            ],
        }

    def _create_preflight(self, mutation: PlannedMutation) -> bool:
        target_collection = mutation.target.get("collection_id")
        baseline = self._collection_baseline(target_collection)
        if baseline["state_sha256"] != mutation.target.get("state_sha256"):
            return False
        question_bindings = mutation.target.get("question_bindings", [])
        if not isinstance(question_bindings, list):
            raise MetabasePolicyError("Metabase dashboard question bindings are invalid.")
        for binding in question_bindings:
            if not isinstance(binding, dict):
                raise MetabasePolicyError("Metabase dashboard question binding is invalid.")
            question_id = binding.get("question_id")
            if type(question_id) is not int or question_id <= 0:
                raise MetabasePolicyError("Metabase dashboard question binding id is invalid.")
            question = self._object_raw(ObjectType.QUESTION, question_id)
            question_state = project_state(question, ObjectType.QUESTION)
            if canonical_sha256(question_state) != binding.get("state_sha256"):
                return False
        source_id = mutation.target.get("source_id")
        if source_id is None:
            return True
        if type(source_id) is not int or source_id <= 0:
            raise MetabasePolicyError("Metabase clone source binding is invalid.")
        source = self._object_raw(mutation.object_type, source_id)
        source_state = project_state(source, mutation.object_type)
        return canonical_sha256(source_state) == mutation.target.get("source_sha256")

    @classmethod
    def _requested_subset_matches(cls, expected: Any, actual: Any) -> bool:
        if isinstance(expected, dict):
            if not isinstance(actual, dict):
                return False
            for key, value in expected.items():
                if (
                    key == "dataset_query"
                    and key in actual
                    and dataset_query_semantically_matches(value, actual[key])
                ):
                    continue
                if (
                    key in {"id", "dashboard_id", "dashboard_tab_id"}
                    and type(value) is int
                    and value <= 0
                ):
                    continue
                if key not in actual or not cls._requested_subset_matches(value, actual[key]):
                    return False
            return True
        if isinstance(expected, list):
            return (
                isinstance(actual, list)
                and len(expected) == len(actual)
                and all(
                    cls._requested_subset_matches(left, right)
                    for left, right in zip(expected, actual, strict=True)
                )
            )
        return expected == actual

    @staticmethod
    def _created_id(payload: Any) -> int | None:
        if isinstance(payload, dict) and type(payload.get("id")) is int and payload["id"] > 0:
            return int(payload["id"])
        return None

    def _created_readback(
        self,
        mutation: PlannedMutation,
        created_id: int,
    ) -> tuple[dict[str, Any] | None, str | None]:
        try:
            raw = self._object_raw(mutation.object_type, created_id)
            return raw, canonical_sha256(project_state(raw, mutation.object_type))
        except (MetabaseApiError, MutationValidationError):
            return None, None

    def _create_result(
        self,
        mutation: PlannedMutation,
        created_id: int,
        *,
        partial_stage: str | None = None,
    ) -> tuple[Outcome, dict[str, Any]]:
        readback, readback_sha256 = self._created_readback(mutation, created_id)
        expected = copy.deepcopy(mutation.after_state or {})
        if mutation.target.get("create_kind") == "dashboard_clone":
            expected.pop("is_deep_copy", None)
        matches = readback is not None and self._requested_subset_matches(expected, readback)
        if matches and mutation.target.get("create_kind") == "dashboard_clone":
            matches = bool(
                len(readback.get("dashcards", []) or [])
                == mutation.target.get("expected_dashcard_count")
                and len(readback.get("tabs", []) or []) == mutation.target.get("expected_tab_count")
            )
        if matches:
            outcome = Outcome.APPLIED_VERIFIED
        elif partial_stage is not None or readback is not None:
            outcome = Outcome.PARTIALLY_APPLIED
        else:
            outcome = Outcome.OUTCOME_UNKNOWN
        details = {
            "created_object_id": created_id,
            "object_results": [
                {
                    "object_type": mutation.object_type.value,
                    "object_id": created_id,
                    "outcome": outcome.value,
                    "verified_after_sha256": readback_sha256,
                }
            ],
            "applied_indexes": [0],
            "rollback_candidates": [],
            "cleanup_candidate": {
                "object_type": mutation.object_type.value,
                "object_id": created_id,
                "recommended_action": "trash_prepare",
            },
        }
        if partial_stage is not None and outcome is not Outcome.APPLIED_VERIFIED:
            details["partial_stage"] = partial_stage
        return outcome, details

    def _execute_dashboard_create(
        self, mutation: PlannedMutation
    ) -> tuple[Outcome, dict[str, Any]]:
        if not self._create_preflight(mutation):
            return Outcome.REJECTED_STALE, {
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        create_keys = {"name", "description", "collection_id", "parameters", "cache_ttl"}
        create_payload = {
            key: copy.deepcopy(value)
            for key, value in mutation.write_payload.items()
            if key in create_keys
        }
        element_payload = {
            key: copy.deepcopy(mutation.write_payload[key])
            for key in ("width", "dashcards", "tabs")
            if key in mutation.write_payload and mutation.write_payload[key] not in (None, [])
        }
        try:
            response = self.http.post_json("/api/dashboard", create_payload)
        except MetabaseApiError as exc:
            outcome = (
                Outcome.OUTCOME_UNKNOWN if exc.outcome_unknown else Outcome.REJECTED_VALIDATION
            )
            return outcome, {
                "object_results": [],
                "http_status": exc.status_code,
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        created_id = self._created_id(response)
        if created_id is None:
            return Outcome.OUTCOME_UNKNOWN, {
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        if element_payload:
            try:
                self.http.put_json(f"/api/dashboard/{created_id}", element_payload)
            except MetabaseApiError:
                return self._create_result(
                    mutation,
                    created_id,
                    partial_stage="dashboard_shell_created_before_element_update",
                )
        return self._create_result(mutation, created_id)

    def _execute_simple_create(
        self,
        plan: ExactPlan,
        mutation: PlannedMutation,
    ) -> tuple[Outcome, dict[str, Any]]:
        if not self._create_preflight(mutation):
            return Outcome.REJECTED_STALE, {
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        if plan.action in {Action.QUESTION_CREATE, Action.QUESTION_CLONE}:
            path = "/api/card"
        elif plan.action in {Action.COLLECTION_CREATE, Action.COLLECTION_CLONE}:
            path = "/api/collection"
        elif plan.action is Action.DASHBOARD_CLONE:
            source_id = mutation.target.get("source_id")
            if type(source_id) is not int or source_id <= 0:
                raise MetabasePolicyError("Metabase dashboard clone source is invalid.")
            path = f"/api/dashboard/{source_id}/copy"
        else:
            raise MetabasePolicyError("Metabase create action has no exact endpoint.")
        try:
            response = self.http.post_json(path, mutation.write_payload)
        except MetabaseApiError as exc:
            outcome = (
                Outcome.OUTCOME_UNKNOWN if exc.outcome_unknown else Outcome.REJECTED_VALIDATION
            )
            return outcome, {
                "object_results": [],
                "http_status": exc.status_code,
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        created_id = self._created_id(response)
        if created_id is None:
            return Outcome.OUTCOME_UNKNOWN, {
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        return self._create_result(mutation, created_id)

    def _execute_field_values_rescan(
        self, mutation: PlannedMutation
    ) -> tuple[Outcome, dict[str, Any]]:
        fresh, _ = self._mutation_preflight(mutation)
        if not fresh or mutation.object_id is None:
            return Outcome.REJECTED_STALE, {
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        try:
            self.http.post_json(
                f"/api/database/{mutation.object_id}/rescan_values",
                {},
            )
        except MetabaseApiError as exc:
            outcome = (
                Outcome.OUTCOME_UNKNOWN if exc.outcome_unknown else Outcome.REJECTED_VALIDATION
            )
            return outcome, {
                "object_results": [],
                "http_status": exc.status_code,
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        return Outcome.APPLIED_VERIFIED, {
            "object_results": [
                {
                    "object_type": ObjectType.DATABASE.value,
                    "object_id": mutation.object_id,
                    "outcome": Outcome.APPLIED_VERIFIED.value,
                }
            ],
            "verification_scope": "request_accepted_for_database_wide_cached_value_rescan",
            "applied_indexes": [0],
            "rollback_candidates": [],
        }

    def exact_action_execute(
        self,
        plan_id: str,
        digest: str,
        *,
        expected_actions: set[Action] | frozenset[Action] | None = None,
    ) -> dict[str, Any]:
        plan = self.plans.peek(plan_id)
        if expected_actions is not None and plan.action not in expected_actions:
            raise MetabasePolicyError(
                "Metabase exact plan action does not match this execute tool."
            )

        context = self._health_context()
        identity_changed = bool(
            not context["identity_verified"]
            or not context["identity_marker"]
            or context["identity_marker"] != plan.identity_marker
        )
        version_changed = bool(
            not context["version_supported"] or context.get("version") != plan.server_version
        )
        consume_identity = (
            plan.identity_marker
            if identity_changed or version_changed
            else str(context["identity_marker"])
        )
        consume_version = (
            plan.server_version if identity_changed or version_changed else str(context["version"])
        )
        plan = self.plans.consume(
            plan_id,
            digest,
            instance=self.config.instance,
            origin=self.config.origin,
            credential_fingerprint=self.config.credential_fingerprint,
            identity_marker=consume_identity,
            server_version=consume_version,
            action=plan.action,
        )
        if identity_changed:
            return self._complete_execution(
                plan,
                Outcome.REJECTED_IDENTITY,
                {
                    "reason": "api_key_subject_changed_after_prepare",
                    "object_results": [],
                    "applied_indexes": [],
                    "rollback_candidates": [],
                },
                intent_audit_id=None,
            )
        if version_changed:
            return self._complete_execution(
                plan,
                Outcome.REJECTED_VALIDATION,
                {
                    "reason": "server_version_changed_after_prepare",
                    "object_results": [],
                    "applied_indexes": [],
                    "rollback_candidates": [],
                },
                intent_audit_id=None,
            )

        try:
            intent_audit_id = self._audit_plan(plan, "intent_consumed")
        except (OSError, RuntimeError):
            return self._complete_execution(
                plan,
                Outcome.NOT_APPLIED_VERIFIED,
                {
                    "reason": "durable_pre_write_audit_unavailable",
                    "object_results": [],
                    "applied_indexes": [],
                    "rollback_candidates": [],
                },
                intent_audit_id=None,
            )

        update_actions = {
            Action.QUESTION_UPDATE,
            Action.QUESTION_TRASH,
            Action.QUESTION_RESTORE,
            Action.DASHBOARD_UPDATE,
            Action.DASHBOARD_TRASH,
            Action.DASHBOARD_RESTORE,
            Action.COLLECTION_UPDATE,
            Action.COLLECTION_TRASH,
            Action.COLLECTION_RESTORE,
            Action.FIELD_UPDATE,
            Action.QUESTION_ROLLBACK,
            Action.DASHBOARD_ROLLBACK,
            Action.COLLECTION_ROLLBACK,
            Action.FIELD_ROLLBACK,
        }
        simple_create_actions = {
            Action.QUESTION_CREATE,
            Action.QUESTION_CLONE,
            Action.DASHBOARD_CLONE,
            Action.COLLECTION_CREATE,
            Action.COLLECTION_CLONE,
        }
        try:
            if plan.action in update_actions:
                outcome, details = self._execute_single_update(plan.mutations[0])
            elif plan.action in {Action.BATCH, Action.BATCH_ROLLBACK}:
                outcome, details = self._execute_batch(plan)
            elif plan.action is Action.DASHBOARD_CREATE:
                outcome, details = self._execute_dashboard_create(plan.mutations[0])
            elif plan.action in simple_create_actions:
                outcome, details = self._execute_simple_create(plan, plan.mutations[0])
            elif plan.action is Action.FIELD_VALUES_RESCAN:
                outcome, details = self._execute_field_values_rescan(plan.mutations[0])
            else:
                raise MetabasePolicyError("Metabase exact plan action has no executor.")
        except MetabaseApiError as exc:
            outcome = (
                Outcome.OUTCOME_UNKNOWN if exc.outcome_unknown else Outcome.NOT_APPLIED_VERIFIED
            )
            details = {
                "reason": "metabase_api_failure_during_execution",
                "http_status": exc.status_code,
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        except (MetabasePolicyError, MutationValidationError):
            outcome = Outcome.REJECTED_VALIDATION
            details = {
                "reason": "exact_plan_validation_failed_during_execution",
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        except Exception:  # pragma: no cover - terminal fail-closed guard.
            outcome = Outcome.OUTCOME_UNKNOWN
            details = {
                "reason": "internal_execution_failure",
                "object_results": [],
                "applied_indexes": [],
                "rollback_candidates": [],
            }
        return self._complete_execution(
            plan,
            outcome,
            details,
            intent_audit_id=intent_audit_id,
        )

    def rollback_prepare(self, source_plan_id: str) -> dict[str, Any]:
        context = self._write_context()
        source = self.plans.completed(source_plan_id)
        if source.server_version != context.get("version"):
            raise MetabasePolicyError(
                "Metabase source plan belongs to a different exact server version."
            )
        if source.outcome not in {Outcome.APPLIED_VERIFIED, Outcome.PARTIALLY_APPLIED}:
            raise MetabasePolicyError(
                "Only a plan with verified applied mutations can be rolled back."
            )
        source_result = source.result or {}
        raw_indexes = source_result.get("applied_indexes", [])
        if not isinstance(raw_indexes, list) or any(
            type(index) is not int for index in raw_indexes
        ):
            raise MetabasePolicyError("Source plan has no exact applied-mutation index set.")
        indexes = sorted(set(raw_indexes))
        if not indexes:
            raise MetabasePolicyError("Source plan has no verified applied mutation to roll back.")

        mutations: list[PlannedMutation] = []
        for index in indexes:
            if not 0 <= index < len(source.mutations):
                raise MetabasePolicyError("Source plan applied-mutation index is invalid.")
            source_mutation = source.mutations[index]
            if source_mutation.object_id is None:
                raise MetabasePolicyError(
                    "Created objects use their typed trash action instead of automatic rollback."
                )
            current_raw = self._object_raw(
                source_mutation.object_type,
                source_mutation.object_id,
            )
            mutation = rollback_mutation(source_mutation, current_raw)
            mutation.target.update(
                {
                    "rollback_of_plan_id": source.plan_id,
                    "rollback_of_index": index,
                    "name": current_raw.get("name") or current_raw.get("display_name"),
                }
            )
            if mutation.object_type is ObjectType.COLLECTION and (
                "archived" in mutation.changed_roots or "parent_id" in mutation.changed_roots
            ):
                mutation.target["inventory"] = self._inventory_collection_tree(mutation.object_id)
            mutations.append(mutation)

        rollback_actions = {
            ObjectType.QUESTION: Action.QUESTION_ROLLBACK,
            ObjectType.DASHBOARD: Action.DASHBOARD_ROLLBACK,
            ObjectType.COLLECTION: Action.COLLECTION_ROLLBACK,
            ObjectType.FIELD: Action.FIELD_ROLLBACK,
        }
        action = (
            rollback_actions[mutations[0].object_type]
            if len(mutations) == 1
            else Action.BATCH_ROLLBACK
        )
        return self._prepare_plan(
            context=context,
            action=action,
            mutations=mutations,
            arguments={
                "source_plan_id": source.plan_id,
                "source_digest": source.digest,
                "source_applied_indexes": indexes,
            },
        )

    def close(self) -> None:
        self.http.close()
