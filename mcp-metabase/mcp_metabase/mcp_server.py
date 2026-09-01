from __future__ import annotations

import logging
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from mcp_metabase.config import ConfigurationError, MetabaseConfig
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.models import Action
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError
from mcp_metabase.service import MetabaseRuntime

MCP_SERVER_NAME = "mcp-metabase"
logger = logging.getLogger(MCP_SERVER_NAME)
mcp = FastMCP(name=MCP_SERVER_NAME)
_RUNTIME: MetabaseRuntime | None = None
CompactActionName = Literal[
    "question_create",
    "question_copy",
    "question_update",
    "question_delete",
    "question_restore",
    "dashboard_create",
    "dashboard_copy",
    "dashboard_update",
    "dashboard_delete",
    "dashboard_restore",
    "collection_create",
    "collection_copy",
    "collection_update",
    "collection_delete",
    "collection_restore",
    "field_update",
    "field_values_rescan",
    "batch_update",
    "question_clone",
    "question_trash",
    "dashboard_clone",
    "dashboard_trash",
    "collection_clone",
    "collection_trash",
    "batch",
]


def get_runtime() -> MetabaseRuntime:
    global _RUNTIME
    if _RUNTIME is None:
        _RUNTIME = MetabaseRuntime(MetabaseConfig.from_env())
    return _RUNTIME


def _call(method: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
    try:
        result = getattr(get_runtime(), method)(*args, **kwargs)
        if not isinstance(result, dict):
            raise RuntimeError("Metabase runtime returned an invalid tool result.")
        return result
    except (
        ConfigurationError,
        MetabaseApiError,
        MetabasePolicyError,
        MutationValidationError,
    ) as exc:
        raise ToolError(str(exc)) from exc
    except OSError as exc:
        raise ToolError("Metabase MCP local state operation failed safely.") from exc
    except Exception as exc:  # pragma: no cover - final non-secret boundary.
        logger.exception("Unexpected Metabase MCP failure")
        raise ToolError(
            "Metabase operation failed unexpectedly; inspect local server logs."
        ) from exc


def _execute(plan_id: str, digest: str, *actions: Action) -> dict[str, Any]:
    return _call(
        "exact_action_execute",
        plan_id,
        digest,
        expected_actions=set(actions),
    )


@mcp.tool(name="metabase_health")
def compact_metabase_health() -> dict[str, Any]:
    """Verify API-key identity, version compatibility, limits, and safe capabilities."""
    return _call("health")


@mcp.tool(name="metabase_search")
def compact_metabase_search(
    query: str = "",
    models: list[str] | None = None,
    archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search a bounded allowlist of Metabase entity types."""
    return _call(
        "search",
        query,
        models,
        archived=archived,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def metabase_collection_get(collection_id: int | str) -> dict[str, Any]:
    """Read one collection by positive id, root, trash, or entity id."""
    return _call("collection_get", collection_id)


@mcp.tool(name="metabase_collection_items")
def compact_metabase_collection_items(
    collection_id: int | str,
    models: list[str] | None = None,
    archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List a bounded page of direct cards, dashboards, and child collections."""
    return _call(
        "collection_items",
        collection_id,
        models=models,
        archived=archived,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def metabase_question_get_full(question_id: int) -> dict[str, Any]:
    """Read one complete saved question/card and its exact state hash."""
    return _call("question_get_full", question_id)


@mcp.tool()
def metabase_dashboard_get_full(dashboard_id: int) -> dict[str, Any]:
    """Read one dashboard with tabs, dashcards, mappings, layout, and state hash."""
    return _call("dashboard_get_full", dashboard_id)


@mcp.tool()
def metabase_edit_session_open(
    object_type: Literal["question", "dashboard"],
    object_id: int,
    ttl_seconds: int | None = None,
    max_actions: int | None = None,
) -> dict[str, Any]:
    """Open one prompt-gated presentation/layout lease for an exact object and state hash."""
    return _call(
        "edit_session_open",
        object_type,
        object_id,
        ttl_seconds=ttl_seconds,
        max_actions=max_actions,
    )


@mcp.tool()
def metabase_edit_session_apply(
    session_id: str,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply one presentation/layout patch inside an active exact edit-session lease."""
    return _call("edit_session_apply", session_id, operations)


@mcp.tool()
def metabase_edit_session_status(session_id: str) -> dict[str, Any]:
    """Read bounded local state for one edit-session lease without writing to Metabase."""
    return _call("edit_session_status", session_id)


@mcp.tool()
def metabase_edit_session_close(session_id: str) -> dict[str, Any]:
    """Close one edit-session lease locally; no Metabase object is changed."""
    return _call("edit_session_close", session_id)


@mcp.tool()
def metabase_question_execute(
    question_id: int,
    parameters: list[dict[str, Any]] | None = None,
    row_limit: int | None = None,
    ignore_cache: bool = False,
) -> dict[str, Any]:
    """Execute one saved question only and return a bounded result set."""
    return _call(
        "question_execute",
        question_id,
        parameters,
        row_limit=row_limit,
        ignore_cache=ignore_cache,
    )


@mcp.tool()
def metabase_question_preview(
    dataset_query: dict[str, Any],
    parameters: list[dict[str, Any]] | None = None,
    row_limit: int | None = None,
) -> dict[str, Any]:
    """Execute a bounded unsaved MBQL or validated read-only native question."""
    return _call(
        "question_preview",
        dataset_query,
        parameters,
        row_limit=row_limit,
    )


@mcp.tool()
def metabase_database_get(database_id: int) -> dict[str, Any]:
    """Read one Metabase database metadata object."""
    return _call("database_get", database_id)


@mcp.tool()
def metabase_table_get(table_id: int, include_fields: bool = True) -> dict[str, Any]:
    """Read one table, optionally with bounded query metadata and fields."""
    return _call("table_get", table_id, include_fields=include_fields)


@mcp.tool()
def metabase_field_get(field_id: int) -> dict[str, Any]:
    """Read one Metabase field metadata object."""
    return _call("field_get", field_id)


@mcp.tool()
def metabase_field_values_get(field_id: int, limit: int = 100) -> dict[str, Any]:
    """Read bounded cached values for one field."""
    return _call("field_values_get", field_id, limit=limit)


@mcp.tool()
def metabase_question_create_prepare(body: dict[str, Any]) -> dict[str, Any]:
    """Prepare creation of one exact saved question; does not write."""
    return _call("question_create_prepare", body)


@mcp.tool()
def metabase_question_create_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Create the prepared saved question; requires explicit user approval."""
    return _execute(plan_id, digest, Action.QUESTION_CREATE)


@mcp.tool()
def metabase_question_copy_prepare(
    source_question_id: int,
    name: str,
    collection_id: int | None = None,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare an exact copy of one question with a chosen name and collection."""
    return _call(
        "question_clone_prepare",
        source_question_id,
        name=name,
        collection_id=collection_id,
        to_root=to_root,
    )


@mcp.tool()
def metabase_question_copy_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Copy the prepared question; requires explicit user approval."""
    return _execute(plan_id, digest, Action.QUESTION_CLONE)


@mcp.tool()
def metabase_question_update_prepare(
    question_id: int,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare closed patches for question SQL/MBQL, visualization, parameters, or metadata."""
    return _call("question_update_prepare", question_id, operations)


@mcp.tool()
def metabase_question_update_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply the exact prepared question patch; requires explicit user approval."""
    return _execute(plan_id, digest, Action.QUESTION_UPDATE)


@mcp.tool()
def metabase_question_delete_prepare(question_id: int) -> dict[str, Any]:
    """Prepare reversible deletion of one question into Trash; never permanent purge."""
    return _call("question_trash_prepare", question_id)


@mcp.tool()
def metabase_question_delete_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Move the prepared question to Trash; requires explicit user approval."""
    return _execute(plan_id, digest, Action.QUESTION_TRASH)


@mcp.tool()
def metabase_question_restore_prepare(
    question_id: int,
    collection_id: int | None = None,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare restoration of one trashed question, optionally moving it."""
    return _call(
        "question_restore_prepare",
        question_id,
        collection_id=collection_id,
        to_root=to_root,
    )


@mcp.tool()
def metabase_question_restore_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Restore the prepared question; requires explicit user approval."""
    return _execute(plan_id, digest, Action.QUESTION_RESTORE)


@mcp.tool()
def metabase_dashboard_create_prepare(body: dict[str, Any]) -> dict[str, Any]:
    """Prepare a dashboard including tabs, dashcards, mappings, parameters, and layout."""
    return _call("dashboard_create_prepare", body)


@mcp.tool()
def metabase_dashboard_create_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Create the exact prepared dashboard; requires explicit user approval."""
    return _execute(plan_id, digest, Action.DASHBOARD_CREATE)


@mcp.tool()
def metabase_dashboard_copy_prepare(
    source_dashboard_id: int,
    name: str | None = None,
    collection_id: int | None = None,
    is_deep_copy: bool = False,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare a server-side dashboard copy, optionally deep-copying linked cards."""
    return _call(
        "dashboard_clone_prepare",
        source_dashboard_id,
        name=name,
        collection_id=collection_id,
        is_deep_copy=is_deep_copy,
        to_root=to_root,
    )


@mcp.tool()
def metabase_dashboard_copy_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Copy the prepared dashboard; requires explicit user approval."""
    return _execute(plan_id, digest, Action.DASHBOARD_CLONE)


@mcp.tool()
def metabase_dashboard_update_prepare(
    dashboard_id: int,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare exact patches for dashboard tabs, cards, mappings, parameters, or layout."""
    return _call("dashboard_update_prepare", dashboard_id, operations)


@mcp.tool()
def metabase_dashboard_update_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply the exact prepared dashboard patch; requires explicit user approval."""
    return _execute(plan_id, digest, Action.DASHBOARD_UPDATE)


@mcp.tool()
def metabase_dashboard_delete_prepare(dashboard_id: int) -> dict[str, Any]:
    """Prepare reversible deletion of one dashboard into Trash."""
    return _call("dashboard_trash_prepare", dashboard_id)


@mcp.tool()
def metabase_dashboard_delete_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Move the prepared dashboard to Trash; requires explicit user approval."""
    return _execute(plan_id, digest, Action.DASHBOARD_TRASH)


@mcp.tool()
def metabase_dashboard_restore_prepare(
    dashboard_id: int,
    collection_id: int | None = None,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare restoration of one trashed dashboard, optionally moving it."""
    return _call(
        "dashboard_restore_prepare",
        dashboard_id,
        collection_id=collection_id,
        to_root=to_root,
    )


@mcp.tool()
def metabase_dashboard_restore_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Restore the prepared dashboard; requires explicit user approval."""
    return _execute(plan_id, digest, Action.DASHBOARD_RESTORE)


@mcp.tool()
def metabase_collection_create_prepare(body: dict[str, Any]) -> dict[str, Any]:
    """Prepare creation of one collection."""
    return _call("collection_create_prepare", body)


@mcp.tool()
def metabase_collection_create_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Create the exact prepared collection; requires explicit user approval."""
    return _execute(plan_id, digest, Action.COLLECTION_CREATE)


@mcp.tool()
def metabase_collection_copy_prepare(
    source_collection_id: int,
    name: str,
    parent_id: int | None = None,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare a shallow collection copy; contents can be copied with typed copy tools."""
    return _call(
        "collection_clone_prepare",
        source_collection_id,
        name=name,
        parent_id=parent_id,
        to_root=to_root,
    )


@mcp.tool()
def metabase_collection_copy_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Copy the prepared collection metadata; requires explicit user approval."""
    return _execute(plan_id, digest, Action.COLLECTION_CLONE)


@mcp.tool()
def metabase_collection_update_prepare(
    collection_id: int,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare exact collection metadata, parent, or archive-state patches."""
    return _call("collection_update_prepare", collection_id, operations)


@mcp.tool()
def metabase_collection_update_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply the exact prepared collection patch; requires explicit user approval."""
    return _execute(plan_id, digest, Action.COLLECTION_UPDATE)


@mcp.tool()
def metabase_collection_delete_prepare(collection_id: int) -> dict[str, Any]:
    """Prepare reversible deletion of an exact inventoried collection tree into Trash."""
    return _call("collection_trash_prepare", collection_id)


@mcp.tool()
def metabase_collection_delete_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Move the unchanged prepared collection tree to Trash; requires approval."""
    return _execute(plan_id, digest, Action.COLLECTION_TRASH)


@mcp.tool()
def metabase_collection_restore_prepare(
    collection_id: int,
    parent_id: int | None = None,
    to_root: bool = False,
) -> dict[str, Any]:
    """Prepare restoration of an exact inventoried collection tree."""
    return _call(
        "collection_restore_prepare",
        collection_id,
        parent_id=parent_id,
        to_root=to_root,
    )


@mcp.tool()
def metabase_collection_restore_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Restore the prepared collection tree; requires explicit user approval."""
    return _execute(plan_id, digest, Action.COLLECTION_RESTORE)


@mcp.tool()
def metabase_field_update_prepare(
    field_id: int,
    operations: list[dict[str, Any]],
) -> dict[str, Any]:
    """Prepare exact field metadata and semantic-setting patches."""
    return _call("field_update_prepare", field_id, operations)


@mcp.tool()
def metabase_field_update_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply the exact prepared field patch; requires explicit user approval."""
    return _execute(plan_id, digest, Action.FIELD_UPDATE)


@mcp.tool()
def metabase_field_values_rescan_prepare(database_id: int) -> dict[str, Any]:
    """Prepare a database-wide cached field-value rescan; scope is not one field."""
    return _call("field_values_rescan_prepare", database_id)


@mcp.tool()
def metabase_field_values_rescan_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Queue the prepared database-wide rescan; requires explicit user approval."""
    return _execute(plan_id, digest, Action.FIELD_VALUES_RESCAN)


@mcp.tool()
def metabase_batch_update_prepare(items: list[dict[str, Any]]) -> dict[str, Any]:
    """Prepare an exact bounded batch after reading every target; does not write."""
    return _call("batch_prepare", items)


@mcp.tool()
def metabase_batch_update_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply the exact batch with bounded same-plan recovery; requires explicit approval."""
    return _execute(plan_id, digest, Action.BATCH)


@mcp.tool(name="metabase_rollback_prepare")
def compact_metabase_rollback_prepare(source_plan_id: str) -> dict[str, Any]:
    """Prepare rollback only for still-current, previously verified update results."""
    return _call("rollback_prepare", source_plan_id)


@mcp.tool(name="metabase_rollback_execute")
def compact_metabase_rollback_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply an exact prepared rollback; requires explicit user approval."""
    return _execute(
        plan_id,
        digest,
        Action.QUESTION_ROLLBACK,
        Action.DASHBOARD_ROLLBACK,
        Action.COLLECTION_ROLLBACK,
        Action.FIELD_ROLLBACK,
        Action.BATCH_ROLLBACK,
    )


@mcp.tool(name="metabase_exact_action_revoke")
def compact_metabase_exact_action_revoke(plan_id: str) -> dict[str, Any]:
    """Revoke one unconsumed exact Metabase mutation plan."""
    return _call("exact_action_revoke", plan_id)


# Keep the expanded typed surface available to package regression tests and
# internal migration code, but export only the compact surface at runtime.
legacy_mcp = mcp
mcp = FastMCP(name=MCP_SERVER_NAME)


@mcp.tool()
def metabase_health() -> dict[str, Any]:
    """Read identity, exact version compatibility, limits, and degraded-mode state."""
    return _call("health")


@mcp.tool()
def metabase_search(
    query: str = "",
    models: list[str] | None = None,
    archived: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, Any]:
    """Search a bounded allowlist of Metabase entity types."""
    return _call(
        "search",
        query,
        models,
        archived=archived,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def metabase_object_get(
    object_type: Literal[
        "question",
        "dashboard",
        "collection",
        "database",
        "table",
        "field",
        "field_values",
    ],
    object_id: int | str,
    include_fields: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """Read one complete typed object; no generic REST path is accepted."""
    return _call(
        "object_get",
        object_type,
        object_id,
        include_fields=include_fields,
        limit=limit,
    )


@mcp.tool()
def metabase_collection_items(
    collection_id: int | str,
    models: list[str] | None = None,
    archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """List a bounded page of direct cards, dashboards, and child collections."""
    return _call(
        "collection_items",
        collection_id,
        models=models,
        archived=archived,
        limit=limit,
        offset=offset,
    )


@mcp.tool()
def metabase_session_open(
    object_type: Literal["question", "dashboard", "collection", "field"],
    object_id: int,
    ttl_seconds: int | None = None,
    max_actions: int | None = None,
) -> dict[str, Any]:
    """Open one prompt-gated full-object work session bound to exact version and state."""
    return _call(
        "object_session_open",
        object_type,
        object_id,
        ttl_seconds=ttl_seconds,
        max_actions=max_actions,
    )


@mcp.tool()
def metabase_session_apply(
    session_id: str,
    updates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Apply full typed patches to objects already bound by an approved work session."""
    return _call("object_session_apply", session_id, updates)


@mcp.tool()
def metabase_session_query(
    session_id: str,
    question_id: int,
    dataset_query: dict[str, Any] | None = None,
    parameters: list[dict[str, Any]] | None = None,
    row_limit: int | None = None,
    ignore_cache: bool = False,
) -> dict[str, Any]:
    """Run a saved or proposed bounded query for a question bound by the work session."""
    return _call(
        "object_session_query",
        session_id,
        question_id,
        dataset_query=dataset_query,
        parameters=parameters,
        row_limit=row_limit,
        ignore_cache=ignore_cache,
    )


@mcp.tool()
def metabase_session_status(session_id: str) -> dict[str, Any]:
    """Read local state and the exact bound object graph for one work session."""
    return _call("object_session_status", session_id)


@mcp.tool()
def metabase_session_close(session_id: str) -> dict[str, Any]:
    """Close one work session locally without changing a Metabase object."""
    return _call("object_session_close", session_id)


@mcp.tool()
def metabase_action_prepare(
    action: CompactActionName,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Prepare one exact action; create payloads use arguments.body, updates use id+operations."""
    return _call("action_prepare", action, arguments)


@mcp.tool()
def metabase_action_execute(
    plan_id: str,
    digest: str,
    open_session: bool = True,
) -> dict[str, Any]:
    """Execute one prepared typed action after approval and normally open a work session."""
    return _call(
        "action_execute",
        plan_id,
        digest,
        open_session=open_session,
    )


@mcp.tool()
def metabase_rollback_prepare(source_plan_id: str) -> dict[str, Any]:
    """Prepare rollback for still-current verified update results; does not write."""
    return _call("rollback_prepare", source_plan_id)


@mcp.tool()
def metabase_rollback_execute(plan_id: str, digest: str) -> dict[str, Any]:
    """Apply an exact prepared rollback; requires explicit user approval."""
    return _execute(
        plan_id,
        digest,
        Action.QUESTION_ROLLBACK,
        Action.DASHBOARD_ROLLBACK,
        Action.COLLECTION_ROLLBACK,
        Action.FIELD_ROLLBACK,
        Action.BATCH_ROLLBACK,
    )


@mcp.tool()
def metabase_exact_action_revoke(plan_id: str) -> dict[str, Any]:
    """Revoke one unconsumed exact mutation plan without changing Metabase."""
    return _call("exact_action_revoke", plan_id)
