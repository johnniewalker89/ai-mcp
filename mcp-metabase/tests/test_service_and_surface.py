from __future__ import annotations

import asyncio
import copy
import json
import re
import sys
from typing import Any

import pytest

import mcp_metabase.main as main_module
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.mcp_server import legacy_mcp, mcp
from mcp_metabase.models import Action, ObjectType, Outcome
from mcp_metabase.normalization import MutationValidationError, canonical_sha256, project_state
from mcp_metabase.plans import MetabasePolicyError
from mcp_metabase.service import (
    COMPACT_ACTION_ARGUMENT_KEYS,
    COMPACT_MUTATION_ACTIONS,
    MetabaseRuntime,
)


class StatefulApi:
    def __init__(self) -> None:
        self.cards = {
            1: {
                "id": 1,
                "name": "Revenue",
                "description": "Stable",
                "display": "table",
                "dataset_query": {"type": "query", "database": 50, "query": {}},
                "parameters": [],
                "parameter_mappings": [],
                "visualization_settings": {"table.columns": [{"name": "revenue"}]},
                "archived": False,
                "collection_id": 20,
                "type": "question",
                "updated_at": "u0",
            }
        }
        self.dashboards = {
            10: {
                "id": 10,
                "name": "Sales",
                "description": None,
                "parameters": [],
                "tabs": [{"id": 101, "name": "Main"}],
                "dashcards": [],
                "archived": False,
                "collection_id": 20,
                "width": "fixed",
                "updated_at": "u0",
            }
        }
        self.collections = {
            20: {
                "id": 20,
                "name": "BI",
                "description": None,
                "parent_id": None,
                "archived": False,
                "can_write": True,
                "updated_at": "u0",
            },
            30: {
                "id": 30,
                "name": "Child",
                "description": None,
                "parent_id": 20,
                "archived": False,
                "can_write": True,
                "updated_at": "u0",
            },
        }
        self.fields = {
            40: {
                "id": 40,
                "name": "city",
                "display_name": "City",
                "description": None,
                "settings": {},
                "table_id": 60,
                "database_id": 50,
                "updated_at": "u0",
            }
        }
        self.databases = {
            50: {
                "id": 50,
                "name": "Analytics",
                "engine": "clickhouse",
                "initial_sync_status": "complete",
                "updated_at": "u0",
            }
        }
        self.put_calls = 0
        self.post_calls = 0
        self.user_id = 7
        self.version = "v0.63.2"
        self.query_calls: list[tuple[str, dict[str, Any]]] = []
        self.compiled_query: dict[str, Any] = {"query": "SELECT 1"}
        self.query_result: dict[str, Any] = {
            "status": "completed",
            "row_count": 0,
            "data": {"cols": [], "rows": []},
        }
        self.fail_put_call: int | None = None
        self.fail_put_unknown = False
        self.next_id = 1000

    def get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        if path == "/api/session/properties":
            return {"version": {"tag": self.version}}
        if path == "/api/user/current":
            return {
                "id": self.user_id,
                "common_name": "Test User",
                "email": "test@example.org",
                "is_active": True,
            }
        if path == "/api/collection/root":
            return {"name": "Our analytics", "can_write": True, "updated_at": "root-u0"}
        if path == "/api/collection/trash":
            return {"name": "Trash", "can_write": True, "updated_at": "trash-u0"}
        match = re.fullmatch(r"/api/(card|dashboard|collection|field|database)/(\d+)", path)
        if match:
            stores = {
                "card": self.cards,
                "dashboard": self.dashboards,
                "collection": self.collections,
                "field": self.fields,
                "database": self.databases,
            }
            return copy.deepcopy(stores[match.group(1)][int(match.group(2))])
        items_match = re.fullmatch(r"/api/collection/(\d+)/items", path)
        if items_match:
            collection_id = int(items_match.group(1))
            archived = bool((params or {}).get("archived", False))
            items: list[dict[str, Any]] = []
            items.extend(
                {"model": "card", "id": object_id}
                for object_id, item in self.cards.items()
                if item.get("collection_id") == collection_id
                and bool(item.get("archived")) == archived
            )
            items.extend(
                {"model": "dashboard", "id": object_id}
                for object_id, item in self.dashboards.items()
                if item.get("collection_id") == collection_id
                and bool(item.get("archived")) == archived
            )
            items.extend(
                {"model": "collection", "id": object_id}
                for object_id, item in self.collections.items()
                if item.get("parent_id") == collection_id and bool(item.get("archived")) == archived
            )
            return {"data": copy.deepcopy(items), "total": len(items)}
        raise AssertionError(f"unexpected GET {path}")

    def put_json(self, path: str, body: dict[str, Any]) -> Any:
        self.put_calls += 1
        if self.fail_put_call == self.put_calls:
            raise MetabaseApiError(
                "simulated safe failure",
                status_code=503 if self.fail_put_unknown else 400,
                outcome_unknown=self.fail_put_unknown,
            )
        match = re.fullmatch(r"/api/(card|dashboard|collection|field)/(\d+)", path)
        if not match:
            raise AssertionError(f"unexpected PUT {path}")
        stores = {
            "card": self.cards,
            "dashboard": self.dashboards,
            "collection": self.collections,
            "field": self.fields,
        }
        target = stores[match.group(1)][int(match.group(2))]
        target.update(copy.deepcopy(body))
        target["updated_at"] = f"u{self.put_calls}"
        return copy.deepcopy(target)

    def post_json(self, path: str, body: dict[str, Any]) -> Any:
        self.post_calls += 1
        self.next_id += 1
        created_id = self.next_id
        if path == "/api/card":
            self.cards[created_id] = {"id": created_id, "archived": False, **copy.deepcopy(body)}
            return copy.deepcopy(self.cards[created_id])
        if path == "/api/dashboard":
            self.dashboards[created_id] = {
                "id": created_id,
                "archived": False,
                "tabs": [],
                "dashcards": [],
                "width": "fixed",
                **copy.deepcopy(body),
            }
            return copy.deepcopy(self.dashboards[created_id])
        dashboard_copy = re.fullmatch(r"/api/dashboard/(\d+)/copy", path)
        if dashboard_copy:
            source = copy.deepcopy(self.dashboards[int(dashboard_copy.group(1))])
            source.update({key: value for key, value in body.items() if key != "is_deep_copy"})
            source["id"] = created_id
            self.dashboards[created_id] = source
            return copy.deepcopy(source)
        if path == "/api/collection":
            self.collections[created_id] = {
                "id": created_id,
                "archived": False,
                "can_write": True,
                **copy.deepcopy(body),
            }
            return copy.deepcopy(self.collections[created_id])
        if re.fullmatch(r"/api/database/\d+/rescan_values", path):
            return {}
        raise AssertionError(f"unexpected POST {path}")

    def query_json(self, path: str, body: dict[str, Any]) -> Any:
        self.query_calls.append((path, copy.deepcopy(body)))
        if path == "/api/dataset/native":
            return copy.deepcopy(self.compiled_query)
        return copy.deepcopy(self.query_result)

    def close(self) -> None:
        return None


@pytest.fixture
def runtime(configured) -> tuple[MetabaseRuntime, StatefulApi]:
    value = MetabaseRuntime(configured)
    fake = StatefulApi()
    value.http.close()
    value.http = fake
    return value, fake


def _execute(runtime: MetabaseRuntime, plan: dict[str, Any], action: Action) -> dict[str, Any]:
    return runtime.exact_action_execute(
        plan["plan_id"],
        plan["digest"],
        expected_actions={action},
    )


def _v063_native_query(sql: str = "select {{city}}") -> dict[str, Any]:
    return {
        "lib/type": "mbql/query",
        "database": 50,
        "stages": [
            {
                "lib/type": "mbql.stage/native",
                "native": sql,
                "template-tags": [
                    {
                        "name": "city",
                        "type": "text",
                        "widget-type": "string/=",
                        "default": "Moscow",
                    }
                ],
            }
        ],
    }


def _dashboard_create_with_mapping() -> dict[str, Any]:
    return {
        "name": "Mapped dashboard",
        "collection_id": 20,
        "width": "fixed",
        "parameters": [{"id": "city-param", "type": "string/=", "name": "City"}],
        "dashcards": [
            {
                "id": -1,
                "card_id": 1,
                "row": 0,
                "col": 0,
                "size_x": 12,
                "size_y": 6,
                "parameter_mappings": [
                    {
                        "parameter_id": "city-param",
                        "target": ["variable", ["template-tag", "city"]],
                    }
                ],
                "card": {"name": "stale caller-owned value"},
                "visualization_settings": {},
            }
        ],
    }


def test_question_update_is_verified_and_one_shot(runtime) -> None:
    service, fake = runtime
    plan = service.question_update_prepare(
        1,
        [{"op": "set", "path": "/name", "value": "Revenue verified"}],
    )
    result = _execute(service, plan, Action.QUESTION_UPDATE)
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert fake.cards[1]["name"] == "Revenue verified"
    with pytest.raises(MetabasePolicyError, match="already consumed"):
        _execute(service, plan, Action.QUESTION_UPDATE)


def test_stale_object_is_rejected_before_put(runtime) -> None:
    service, fake = runtime
    plan = service.question_update_prepare(
        1,
        [{"op": "set", "path": "/description", "value": "Prepared"}],
    )
    fake.cards[1]["updated_at"] = "external-change"
    result = _execute(service, plan, Action.QUESTION_UPDATE)
    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert fake.put_calls == 0


def test_batch_prechecks_every_target_before_first_write(runtime) -> None:
    service, fake = runtime
    plan = service.batch_prepare(
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/name", "value": "Prepared"}],
            },
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Prepared"}],
            },
        ]
    )
    fake.dashboards[10]["updated_at"] = "external-change"
    result = _execute(service, plan, Action.BATCH)
    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert fake.put_calls == 0


def test_partial_batch_exposes_and_executes_exact_rollback(runtime) -> None:
    service, fake = runtime
    plan = service.batch_prepare(
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/name", "value": "Changed"}],
            },
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Will fail"}],
            },
        ]
    )
    fake.fail_put_call = 2
    result = _execute(service, plan, Action.BATCH)
    assert result["outcome"] == Outcome.PARTIALLY_APPLIED.value
    assert result["applied_indexes"] == [0]
    assert fake.cards[1]["name"] == "Changed"

    fake.fail_put_call = None
    rollback = service.rollback_prepare(result["plan_id"])
    rolled_back = service.exact_action_execute(
        rollback["plan_id"],
        rollback["digest"],
        expected_actions={Action.QUESTION_ROLLBACK},
    )
    assert rolled_back["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert fake.cards[1]["name"] == "Revenue"


def test_dashboard_create_applies_owned_elements(runtime) -> None:
    service, fake = runtime
    plan = service.dashboard_create_prepare(
        {
            "name": "New dashboard",
            "collection_id": 20,
            "description": None,
            "parameters": [],
            "width": "full",
            "tabs": [{"id": -1, "name": "Overview"}],
            "dashcards": [
                {
                    "id": -2,
                    "dashboard_tab_id": -1,
                    "card_id": 1,
                    "row": 0,
                    "col": 0,
                    "size_x": 12,
                    "size_y": 6,
                    "parameter_mappings": [],
                    "visualization_settings": {},
                }
            ],
        }
    )
    result = _execute(service, plan, Action.DASHBOARD_CREATE)
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    created = fake.dashboards[result["created_object_id"]]
    assert created["width"] == "full"
    assert len(created["tabs"]) == 1
    assert len(created["dashcards"]) == 1


def test_question_create_reconciles_legacy_native_canonicalization(runtime) -> None:
    service, fake = runtime
    original_post = fake.post_json

    def post_with_v063_canonicalization(path, body):  # noqa: ANN001, ANN202
        canonical_body = copy.deepcopy(body)
        if path == "/api/card":
            canonical_body["dataset_query"] = _v063_native_query()
        return original_post(path, canonical_body)

    fake.post_json = post_with_v063_canonicalization
    prepared = service.action_prepare(
        "question_create",
        {
            "body": {
                "name": "Legacy native",
                "dataset_query": {
                    "type": "native",
                    "database": 50,
                    "native": {
                        "query": "select {{city}}",
                        "template-tags": {
                            "city": {
                                "type": "text",
                                "widget-type": "string/=",
                                "default": "Moscow",
                            }
                        },
                    },
                },
                "display": "table",
                "collection_id": 20,
            }
        },
    )

    result = service.action_execute(prepared["plan_id"], prepared["digest"])

    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert result["work_session"]["opened"] is True
    assert fake.cards[result["created_object_id"]]["dataset_query"] == _v063_native_query()


def test_dashboard_create_hydrates_mapped_question_and_binds_its_hash(runtime) -> None:
    service, fake = runtime
    fake.cards[1]["dataset_query"] = _v063_native_query()

    prepared = service.dashboard_create_prepare(_dashboard_create_with_mapping())
    mutation = service.plans.peek(prepared["plan_id"]).mutations[0]
    result = _execute(service, prepared, Action.DASHBOARD_CREATE)

    assert mutation.write_payload["dashcards"][0].get("card") is None
    assert mutation.target["question_bindings"] == [
        {
            "question_id": 1,
            "state_sha256": canonical_sha256(project_state(fake.cards[1], ObjectType.QUESTION)),
        }
    ]
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value


def test_dashboard_create_rejects_question_drift_after_mapping_prepare(runtime) -> None:
    service, fake = runtime
    fake.cards[1]["dataset_query"] = _v063_native_query()
    prepared = service.dashboard_create_prepare(_dashboard_create_with_mapping())
    fake.cards[1]["updated_at"] = "external-question-change"

    result = _execute(service, prepared, Action.DASHBOARD_CREATE)

    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert fake.post_calls == 0


def test_batch_orders_linked_dashboard_before_question_to_avoid_self_stale(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 12,
            "size_y": 6,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    original_get = fake.get_json

    def get_with_embedded_card(path, *, params=None):  # noqa: ANN001, ANN202
        payload = original_get(path, params=params)
        if path == "/api/dashboard/10":
            for dashcard in payload["dashcards"]:
                dashcard["card"] = copy.deepcopy(fake.cards[dashcard["card_id"]])
        return payload

    fake.get_json = get_with_embedded_card
    prepared = service.batch_prepare(
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/collection_id", "value": 30}],
            },
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/collection_id", "value": 30}],
            },
        ]
    )
    plan = service.plans.peek(prepared["plan_id"])

    result = _execute(service, prepared, Action.BATCH)

    assert [(item.object_type, item.object_id) for item in plan.mutations] == [
        (ObjectType.DASHBOARD, 10),
        (ObjectType.QUESTION, 1),
    ]
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert result["applied_indexes"] == [0, 1]
    assert fake.dashboards[10]["collection_id"] == 30
    assert fake.cards[1]["collection_id"] == 30


def test_collection_tree_change_invalidates_delete_plan(runtime) -> None:
    service, fake = runtime
    plan = service.collection_trash_prepare(20)
    fake.cards[2] = {**copy.deepcopy(fake.cards[1]), "id": 2, "name": "New child"}
    result = _execute(service, plan, Action.COLLECTION_TRASH)
    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert fake.put_calls == 0


def test_question_delete_and_restore_are_reversible(runtime) -> None:
    service, fake = runtime
    delete = service.question_trash_prepare(1)
    deleted = _execute(service, delete, Action.QUESTION_TRASH)
    assert deleted["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert fake.cards[1]["archived"] is True

    restore = service.question_restore_prepare(1)
    restored = _execute(service, restore, Action.QUESTION_RESTORE)
    assert restored["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert fake.cards[1]["archived"] is False


def test_saved_question_execute_remains_bounded(runtime) -> None:
    service, fake = runtime
    fake.query_result = {
        "status": "completed",
        "row_count": 2,
        "data": {"cols": [{"name": "id"}], "rows": [[1], [2]]},
    }

    result = service.question_execute(1, row_limit=1, ignore_cache=True)

    assert result["question_id"] == 1
    assert result["rows_returned"] == 1
    assert result["truncated"] is True
    assert fake.query_calls == [("/api/card/1/query", {"parameters": [], "ignore_cache": True})]


def test_question_preview_executes_bounded_unsaved_mbql(runtime) -> None:
    service, fake = runtime
    fake.query_result = {
        "status": "completed",
        "row_count": 3,
        "data": {
            "cols": [{"name": "id"}],
            "rows": [[1], [2], [3]],
        },
    }

    result = service.question_preview(
        {
            "type": "query",
            "database": 50,
            "query": {"source-table": 60},
            "constraints": {"max-results": 9999},
            "middleware": {"unsafe": True},
        },
        row_limit=2,
    )

    assert result["query_mode"] == "mbql"
    assert result["rows_returned"] == 2
    assert result["truncated"] is True
    assert fake.query_calls == [
        (
            "/api/dataset",
            {
                "type": "query",
                "database": 50,
                "query": {"source-table": 60},
                "constraints": {
                    "max-results": 3,
                    "max-results-bare-rows": 3,
                },
            },
        )
    ]


def test_question_preview_compiles_and_validates_native_sql(runtime) -> None:
    service, fake = runtime
    fake.compiled_query = {"query": "SELECT number FROM system.numbers LIMIT 2"}
    fake.query_result = {
        "status": "completed",
        "row_count": 1,
        "data": {"cols": [{"name": "number"}], "rows": [[1]]},
    }
    dataset_query = {
        "type": "native",
        "database": 50,
        "native": {
            "query": "SELECT number FROM system.numbers WHERE number = {{value}}",
            "template-tags": {"value": {"name": "value", "type": "number"}},
        },
    }
    parameters = [
        {
            "type": "category",
            "target": ["variable", ["template-tag", "value"]],
            "value": 1,
        }
    ]

    result = service.question_preview(dataset_query, parameters, row_limit=10)

    assert result["query_mode"] == "native"
    assert result["database_engine"] == "clickhouse"
    assert result["native_sql_validated"] is True
    assert len(result["native_sql_sha256"]) == 64
    assert [path for path, _ in fake.query_calls] == [
        "/api/dataset/native",
        "/api/dataset",
    ]
    assert fake.query_calls[0][1]["parameters"] == parameters
    assert fake.query_calls[1][1]["constraints"] == {
        "max-results": 11,
        "max-results-bare-rows": 11,
    }


@pytest.mark.parametrize(
    "compiled_sql",
    [
        "DROP TABLE production.events",
        "SELECT 1; SELECT 2",
        "SELECT * FROM remote('cluster', 'db.table')",
        "SELECT 1 -- hidden payload",
    ],
)
def test_question_preview_rejects_unsafe_native_sql_before_execution(
    runtime,
    compiled_sql: str,
) -> None:
    service, fake = runtime
    fake.compiled_query = {"query": compiled_sql}

    with pytest.raises(MutationValidationError):
        service.question_preview(
            {
                "type": "native",
                "database": 50,
                "native": {"query": "SELECT 1"},
            }
        )

    assert [path for path, _ in fake.query_calls] == ["/api/dataset/native"]


def test_question_preview_rejects_native_driver_options_before_compilation(runtime) -> None:
    service, fake = runtime

    with pytest.raises(MutationValidationError, match="driver options"):
        service.question_preview(
            {
                "type": "native",
                "database": 50,
                "native": {"query": "SELECT 1", "params": ["unsafe"]},
            }
        )

    assert fake.query_calls == []


def test_edit_session_applies_repeated_question_presentation_updates_and_keeps_rollback(
    runtime,
) -> None:
    service, fake = runtime
    opened = service.edit_session_open(
        "question",
        1,
        ttl_seconds=120,
        max_actions=2,
    )
    session_id = opened["session"]["session_id"]
    assert opened["approval_scope"] == "presentation_layout_only"

    first = service.edit_session_apply(
        session_id,
        [{"op": "set", "path": "/display", "value": "bar"}],
    )
    second = service.edit_session_apply(
        session_id,
        [
            {
                "op": "set",
                "path": "/visualization_settings/graph.colors",
                "value": {"revenue": "#509EE3"},
            }
        ],
    )

    assert first["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert first["session"]["active"] is True
    assert second["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert second["session"]["active"] is False
    assert second["session"]["close_reason"] == "max_actions_reached"
    assert second["rollback_source_plan_id"]
    assert fake.cards[1]["display"] == "bar"
    assert fake.cards[1]["visualization_settings"]["graph.colors"] == {"revenue": "#509EE3"}
    assert fake.put_calls == 2

    health = service.health()
    assert health["capabilities"]["scoped_edit_sessions"] is True
    assert health["limits"]["edit_session_ttl_seconds"] == 900
    assert health["limits"]["edit_session_max_actions"] == 20
    assert health["limits"]["max_active_edit_sessions"] == 20

    rollback = service.rollback_prepare(str(second["rollback_source_plan_id"]))
    rollback_result = _execute(service, rollback, Action.QUESTION_ROLLBACK)
    assert rollback_result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert "graph.colors" not in fake.cards[1]["visualization_settings"]
    assert fake.cards[1]["display"] == "bar"


def test_edit_session_rejects_query_change_without_consuming_or_closing_lease(runtime) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1, max_actions=2)
    session_id = opened["session"]["session_id"]

    with pytest.raises(MutationValidationError, match="presentation"):
        service.edit_session_apply(
            session_id,
            [
                {
                    "op": "set",
                    "path": "/dataset_query",
                    "value": {
                        "type": "native",
                        "database": 50,
                        "native": {"query": "SELECT 2"},
                    },
                }
            ],
        )

    status = service.edit_session_status(session_id)
    assert status["session"]["active"] is True
    assert status["session"]["actions_used"] == 0
    assert fake.put_calls == 0


def test_edit_session_external_edit_closes_lease_before_write(runtime) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1)
    session_id = opened["session"]["session_id"]
    fake.cards[1]["updated_at"] = "external-change"

    result = service.edit_session_apply(
        session_id,
        [{"op": "set", "path": "/display", "value": "bar"}],
    )

    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert result["session"]["active"] is False
    assert result["session"]["close_reason"] == "stale_external_edit"
    assert fake.put_calls == 0


def test_edit_session_identity_drift_closes_lease_before_write(runtime) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1)
    session_id = opened["session"]["session_id"]
    fake.user_id = 8

    with pytest.raises(MetabasePolicyError, match="identity or version"):
        service.edit_session_apply(
            session_id,
            [{"op": "set", "path": "/display", "value": "bar"}],
        )

    status = service.edit_session_status(session_id)
    assert status["session"]["active"] is False
    assert status["session"]["close_reason"] == "identity_or_version_changed"
    assert fake.put_calls == 0


def test_edit_session_safe_write_rejection_closes_lease_without_changing_object(runtime) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1)
    session_id = opened["session"]["session_id"]
    fake.fail_put_call = 1

    result = service.edit_session_apply(
        session_id,
        [{"op": "set", "path": "/display", "value": "bar"}],
    )

    assert result["outcome"] == Outcome.REJECTED_VALIDATION.value
    assert result["session"]["active"] is False
    assert result["session"]["close_reason"] == "apply_rejected_validation"
    assert result["rollback_source_plan_id"] is None
    assert fake.cards[1]["display"] == "table"
    assert fake.put_calls == 1


def test_edit_session_blocks_write_when_session_intent_audit_is_unavailable(
    runtime,
    monkeypatch,
) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1)
    session_id = opened["session"]["session_id"]

    def fail_audit(_event):  # noqa: ANN001, ANN202
        raise OSError("simulated audit failure")

    monkeypatch.setattr(service.audit, "write", fail_audit)
    with pytest.raises(MetabasePolicyError, match="pre-write audit"):
        service.edit_session_apply(
            session_id,
            [{"op": "set", "path": "/display", "value": "bar"}],
        )

    status = service.edit_session_status(session_id)
    assert status["session"]["active"] is False
    assert status["session"]["close_reason"] == "intent_audit_unavailable"
    assert fake.cards[1]["display"] == "table"
    assert fake.put_calls == 0


def test_edit_session_closes_after_applied_write_when_exact_terminal_audit_fails(
    runtime,
    monkeypatch,
) -> None:
    service, fake = runtime
    original_write = service.audit.write
    audit_calls = 0

    def fail_exact_terminal(event):  # noqa: ANN001, ANN202
        nonlocal audit_calls
        audit_calls += 1
        if audit_calls == 4:
            raise OSError("simulated exact terminal audit failure")
        return original_write(event)

    monkeypatch.setattr(service.audit, "write", fail_exact_terminal)
    opened = service.edit_session_open("question", 1)
    result = service.edit_session_apply(
        opened["session"]["session_id"],
        [{"op": "set", "path": "/display", "value": "bar"}],
    )

    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert result["exact_action"]["terminal_audit_recorded"] is False
    assert result["session"]["active"] is False
    assert result["session"]["close_reason"] == "exact_terminal_audit_unavailable"
    assert result["rollback_source_plan_id"]
    assert fake.cards[1]["display"] == "bar"


def test_edit_session_rejects_move_to_unknown_tab_without_consuming_lease(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 4,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    opened = service.edit_session_open("dashboard", 10)
    session_id = opened["session"]["session_id"]

    with pytest.raises(MutationValidationError, match="existing dashboard tab"):
        service.edit_session_apply(
            session_id,
            [
                {
                    "op": "dashboard_item_set",
                    "path": "/dashcards",
                    "item_id": 201,
                    "item_path": "/dashboard_tab_id",
                    "value": 999,
                }
            ],
        )

    status = service.edit_session_status(session_id)
    assert status["session"]["active"] is True
    assert status["session"]["in_flight"] is False
    assert status["session"]["actions_used"] == 0
    assert fake.put_calls == 0


def test_edit_session_opens_dashboard_with_v063_native_template_tags(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["parameters"] = [
        {"id": "city-param", "type": "location/city", "name": "City"}
    ]
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 4,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [
                {
                    "parameter_id": "city-param",
                    "card_id": 1,
                    "target": [
                        "dimension",
                        ["template-tag", "city"],
                        {"stage-number": 0},
                    ],
                }
            ],
            "visualization_settings": {},
            "card": {
                "dataset_query": {
                    "lib/type": "mbql/query",
                    "database": 50,
                    "stages": [
                        {
                            "lib/type": "mbql.stage/native",
                            "native": "select {{city}}",
                            "template-tags": [
                                {
                                    "name": "city",
                                    "type": "dimension",
                                    "widget-type": "location/city",
                                }
                            ],
                        }
                    ],
                }
            },
        }
    ]

    opened = service.edit_session_open("dashboard", 10)

    assert opened["opened"] is True
    assert opened["session"]["object_id"] == 10
    assert opened["session"]["active"] is True
    assert fake.put_calls == 0


def test_edit_session_updates_existing_dashboard_layout_without_changing_composition(
    runtime,
) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 4,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {"graph.metrics": ["old"]},
        }
    ]
    opened = service.edit_session_open("dashboard", 10)
    result = service.edit_session_apply(
        opened["session"]["session_id"],
        [
            {
                "op": "dashboard_item_set",
                "path": "/dashcards",
                "item_id": 201,
                "item_path": "/size_x",
                "value": 8,
            },
            {
                "op": "dashboard_item_replace_array",
                "path": "/dashcards",
                "item_id": 201,
                "item_path": "/visualization_settings/graph.metrics",
                "value": ["revenue"],
            },
            {
                "op": "dashboard_item_set",
                "path": "/tabs",
                "item_id": 101,
                "item_path": "/name",
                "value": "Overview",
            },
        ],
    )

    dashcard = fake.dashboards[10]["dashcards"][0]
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert dashcard["size_x"] == 8
    assert dashcard["visualization_settings"]["graph.metrics"] == ["revenue"]
    assert dashcard["card_id"] == 1
    assert dashcard["parameter_mappings"] == []
    assert fake.dashboards[10]["tabs"] == [{"id": 101, "name": "Overview"}]


def test_edit_session_close_is_local_and_audited(runtime) -> None:
    service, fake = runtime
    opened = service.edit_session_open("question", 1)
    closed = service.edit_session_close(opened["session"]["session_id"])

    assert closed["closed"] is True
    assert closed["session"]["active"] is False
    assert closed["session"]["close_reason"] == "closed_by_user"
    assert closed["audit_recorded"] is True
    assert fake.put_calls == 0


def test_mcp_surface_is_compact_without_losing_legacy_contract() -> None:
    tools = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert tools == {
        "metabase_health",
        "metabase_search",
        "metabase_object_get",
        "metabase_collection_items",
        "metabase_session_open",
        "metabase_session_apply",
        "metabase_session_query",
        "metabase_session_status",
        "metabase_session_close",
        "metabase_action_prepare",
        "metabase_action_execute",
        "metabase_rollback_prepare",
        "metabase_rollback_execute",
        "metabase_exact_action_revoke",
    }
    legacy_tools = {tool.name for tool in asyncio.run(legacy_mcp.list_tools())}
    assert len(legacy_tools) == 55
    assert "metabase_question_create_prepare" in legacy_tools
    assert "metabase_dashboard_update_execute" in legacy_tools
    assert all("raw" not in name and "permanent" not in name for name in tools)
    assert set(COMPACT_ACTION_ARGUMENT_KEYS) == set(COMPACT_MUTATION_ACTIONS)


@pytest.mark.parametrize(
    ("action", "arguments", "expected", "restore_type"),
    [
        (
            "question_create",
            {
                "body": {
                    "name": "New question",
                    "dataset_query": {"type": "query", "database": 50, "query": {}},
                    "display": "table",
                    "collection_id": 20,
                }
            },
            Action.QUESTION_CREATE,
            None,
        ),
        (
            "question_copy",
            {"source_question_id": 1, "name": "Question copy"},
            Action.QUESTION_CLONE,
            None,
        ),
        (
            "question_update",
            {
                "question_id": 1,
                "operations": [{"op": "set", "path": "/name", "value": "Updated"}],
            },
            Action.QUESTION_UPDATE,
            None,
        ),
        ("question_delete", {"question_id": 1}, Action.QUESTION_TRASH, None),
        ("question_restore", {"question_id": 1}, Action.QUESTION_RESTORE, "question"),
        (
            "dashboard_create",
            {"body": {"name": "New dashboard", "collection_id": 20}},
            Action.DASHBOARD_CREATE,
            None,
        ),
        (
            "dashboard_copy",
            {"source_dashboard_id": 10, "name": "Dashboard copy"},
            Action.DASHBOARD_CLONE,
            None,
        ),
        (
            "dashboard_update",
            {
                "dashboard_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Updated"}],
            },
            Action.DASHBOARD_UPDATE,
            None,
        ),
        ("dashboard_delete", {"dashboard_id": 10}, Action.DASHBOARD_TRASH, None),
        (
            "dashboard_restore",
            {"dashboard_id": 10},
            Action.DASHBOARD_RESTORE,
            "dashboard",
        ),
        (
            "collection_create",
            {"body": {"name": "New collection", "parent_id": 20}},
            Action.COLLECTION_CREATE,
            None,
        ),
        (
            "collection_copy",
            {"source_collection_id": 30, "name": "Collection copy"},
            Action.COLLECTION_CLONE,
            None,
        ),
        (
            "collection_update",
            {
                "collection_id": 30,
                "operations": [{"op": "set", "path": "/description", "value": "Updated"}],
            },
            Action.COLLECTION_UPDATE,
            None,
        ),
        ("collection_delete", {"collection_id": 30}, Action.COLLECTION_TRASH, None),
        (
            "collection_restore",
            {"collection_id": 30},
            Action.COLLECTION_RESTORE,
            "collection",
        ),
        (
            "field_update",
            {
                "field_id": 40,
                "operations": [{"op": "set", "path": "/display_name", "value": "City name"}],
            },
            Action.FIELD_UPDATE,
            None,
        ),
        (
            "field_values_rescan",
            {"database_id": 50},
            Action.FIELD_VALUES_RESCAN,
            None,
        ),
        (
            "batch_update",
            {
                "items": [
                    {
                        "object_type": "question",
                        "object_id": 1,
                        "operations": [{"op": "set", "path": "/description", "value": "Batch"}],
                    }
                ]
            },
            Action.BATCH,
            None,
        ),
    ],
)
def test_compact_action_prepare_routes_every_public_mutation(
    runtime,
    action,
    arguments,
    expected,
    restore_type,
) -> None:  # noqa: ANN001
    service, fake = runtime
    if restore_type == "question":
        fake.cards[1]["archived"] = True
    elif restore_type == "dashboard":
        fake.dashboards[10]["archived"] = True
    elif restore_type == "collection":
        fake.collections[30]["archived"] = True

    prepared = service.action_prepare(action, arguments)

    assert prepared["action"] == expected.value
    assert prepared["server_version"] == "v0.63.2"
    assert fake.put_calls == 0
    assert fake.post_calls == 0


def test_exact_plan_rejects_supported_but_different_server_version(runtime) -> None:
    service, fake = runtime
    plan = service.question_update_prepare(
        1,
        [{"op": "set", "path": "/description", "value": "Prepared"}],
    )
    fake.version = "v0.63.3"

    result = _execute(service, plan, Action.QUESTION_UPDATE)

    assert result["outcome"] == Outcome.REJECTED_VALIDATION.value
    assert result["reason"] == "server_version_changed_after_prepare"
    assert fake.put_calls == 0


def test_unknown_version_keeps_bounded_reads_and_disables_writes(runtime) -> None:
    service, fake = runtime
    fake.version = "v0.64.0"

    health = service.health()
    question = service.object_get("question", 1)

    assert health["compatibility_mode"] == "read_only_degraded"
    assert health["writes_ready"] is False
    assert question["object_id"] == 1
    with pytest.raises(MetabasePolicyError, match="outside the supported contract"):
        service.question_update_prepare(
            1,
            [{"op": "set", "path": "/name", "value": "Blocked"}],
        )


def test_multi_model_reads_forward_lists_and_reject_invalid_pages_before_network(
    runtime,
) -> None:
    service, _ = runtime
    original_get = service.http.get_json
    calls: list[tuple[str, dict[str, Any]]] = []

    def capture_get(path, *, params=None):  # noqa: ANN001, ANN202
        calls.append((path, copy.deepcopy(params or {})))
        if path == "/api/search":
            return {"data": [], "total": 0}
        return original_get(path, params=params)

    service.http.get_json = capture_get

    service.search("Revenue", ["card", "dashboard", "card"], limit=10)
    service.collection_items(
        20,
        models=["card", "dashboard", "card"],
        limit=10,
    )

    assert calls[0][1]["models"] == ["card", "dashboard"]
    assert calls[1][1]["models"] == ["card", "dashboard"]
    completed_calls = len(calls)

    with pytest.raises(MutationValidationError, match="limit"):
        service.search(limit=service.config.max_list_items + 1)
    with pytest.raises(MutationValidationError, match="offset"):
        service.collection_items(20, limit=10, offset=-1)

    assert len(calls) == completed_calls


def test_compact_action_prepare_explains_create_argument_wrapper(runtime) -> None:
    service, fake = runtime

    with pytest.raises(MutationValidationError) as raised:
        service.action_prepare(
            "collection_create",
            {"name": "Child", "description": "Test", "parent_id": 20},
        )

    message = str(raised.value)
    assert "required keys [body]" in message
    assert "unknown [description, name, parent_id]" in message
    assert "arguments.body" in message
    assert fake.post_calls == 0


def test_full_session_apply_response_does_not_echo_full_object_state(runtime) -> None:
    service, fake = runtime
    query_marker = "SELECT_PRIVATE_MARKER_" + "x" * 10_000
    metadata_marker = "RESULT_METADATA_PRIVATE_MARKER"
    fake.cards[1]["dataset_query"] = {
        "type": "native",
        "database": 50,
        "native": {"query": query_marker},
    }
    fake.cards[1]["result_metadata"] = [{"name": metadata_marker}]
    opened = service.object_session_open("question", 1)

    result = service.object_session_apply(
        opened["session"]["session_id"],
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/description", "value": "Compact response"}],
            }
        ],
    )
    encoded = json.dumps(result)

    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert query_marker not in encoded
    assert metadata_marker not in encoded
    assert len(encoded) < 10_000


def test_full_question_session_updates_logic_and_runs_v063_native_preview(runtime) -> None:
    service, fake = runtime
    opened = service.object_session_open("question", 1)
    session_id = opened["session"]["session_id"]
    dataset_query = {
        "lib/type": "mbql/query",
        "database": 50,
        "stages": [
            {
                "lib/type": "mbql.stage/native",
                "native": "select {{city}}",
                "template-tags": [{"name": "city", "type": "text", "default": "Moscow"}],
            }
        ],
    }

    applied = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [
                    {"op": "set", "path": "/dataset_query", "value": dataset_query},
                    {"op": "set", "path": "/display", "value": "bar"},
                    {
                        "op": "replace_array",
                        "path": "/parameters",
                        "value": [{"id": "city", "type": "string/="}],
                    },
                ],
            }
        ],
    )
    queried = service.object_session_query(
        session_id,
        1,
        dataset_query=dataset_query,
        row_limit=10,
    )

    assert applied["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert fake.cards[1]["dataset_query"] == dataset_query
    assert queried["result"]["query_mode"] == "native"
    assert queried["result"]["query_shape"] == "mbql5_native_stage"
    assert [path for path, _ in fake.query_calls[-2:]] == [
        "/api/dataset/native",
        "/api/dataset",
    ]
    assert queried["session"]["actions_used"] == 2


def test_dashboard_session_binds_and_updates_linked_question(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    opened = service.object_session_open("dashboard", 10)

    result = service.object_session_apply(
        opened["session"]["session_id"],
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Session graph"}],
            },
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [
                    {
                        "op": "set",
                        "path": "/visualization_settings/graph.type",
                        "value": "bar",
                    }
                ],
            },
        ],
    )

    assert opened["session"]["bound_object_count"] == 2
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert result["session"]["bound_object_count"] == 2
    assert fake.dashboards[10]["description"] == "Session graph"
    assert fake.cards[1]["visualization_settings"]["graph.type"] == "bar"


def test_full_session_keeps_archive_as_separate_lifecycle_action(runtime) -> None:
    service, fake = runtime
    opened = service.object_session_open("question", 1)

    with pytest.raises(MetabasePolicyError, match="Archive/delete"):
        service.object_session_apply(
            opened["session"]["session_id"],
            [
                {
                    "object_type": "question",
                    "object_id": 1,
                    "operations": [{"op": "set", "path": "/archived", "value": True}],
                }
            ],
        )

    assert fake.put_calls == 0
    assert (
        service.object_session_status(opened["session"]["session_id"])["session"]["active"] is True
    )


def test_compact_create_execute_opens_full_work_session(runtime) -> None:
    service, _ = runtime
    prepared = service.action_prepare(
        "question_create",
        {
            "body": {
                "name": "Created compactly",
                "dataset_query": {"type": "query", "database": 50, "query": {}},
                "display": "table",
                "collection_id": 20,
            }
        },
    )

    result = service.action_execute(prepared["plan_id"], prepared["digest"])

    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert result["work_session"]["opened"] is True
    assert result["work_session"]["approval_scope"] == "full_object_graph"


def test_dashboard_session_rebinds_exact_graph_after_composition_change(runtime) -> None:
    service, fake = runtime
    fake.cards[2] = copy.deepcopy(fake.cards[1])
    fake.cards[2].update({"id": 2, "name": "Margin"})
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    opened = service.object_session_open("dashboard", 10)
    replacement = [
        {
            "id": 202,
            "card_id": 2,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]

    result = service.object_session_apply(
        opened["session"]["session_id"],
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "replace_array", "path": "/dashcards", "value": replacement}],
            }
        ],
    )

    bindings = {
        (item["object_type"], item["object_id"]) for item in result["session"]["bound_objects"]
    }
    assert result["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert bindings == {("dashboard", 10), ("question", 2)}
    assert fake.dashboards[10]["dashcards"] == replacement


def test_dashboard_session_refreshes_primary_hash_after_linked_question_update(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    original_get = fake.get_json

    def get_with_embedded_card(path, *, params=None):  # noqa: ANN001, ANN202
        payload = original_get(path, params=params)
        if path == "/api/dashboard/10":
            for dashcard in payload["dashcards"]:
                dashcard["card"] = copy.deepcopy(fake.cards[dashcard["card_id"]])
        return payload

    fake.get_json = get_with_embedded_card
    opened = service.object_session_open("dashboard", 10)
    session_id = opened["session"]["session_id"]

    first = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/display", "value": "bar"}],
            }
        ],
    )
    second = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Still active"}],
            }
        ],
    )

    assert first["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert first["session"]["active"] is True
    assert second["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert second["session"]["active"] is True
    assert fake.cards[1]["display"] == "bar"
    assert fake.dashboards[10]["description"] == "Still active"


def test_question_session_query_ignores_derived_result_metadata_refresh(runtime) -> None:
    service, fake = runtime
    fake.cards[1]["result_metadata"] = [
        {
            "name": "revenue",
            "fingerprint": {"type": {"type/Number": {"avg": 10.0, "sd": 1.0}}},
        }
    ]
    original_query = fake.query_json

    def query_with_result_metadata_refresh(path, body):  # noqa: ANN001, ANN202
        result = original_query(path, body)
        fake.cards[1]["result_metadata"][0]["fingerprint"]["type"]["type/Number"].update(
            {"avg": 11.0, "sd": 1.5}
        )
        return result

    fake.query_json = query_with_result_metadata_refresh
    opened = service.object_session_open("question", 1)
    session_id = opened["session"]["session_id"]

    queried = service.object_session_query(session_id, 1, row_limit=5)
    applied = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "question",
                "object_id": 1,
                "operations": [{"op": "set", "path": "/description", "value": "Still active"}],
            }
        ],
    )
    queried_after_apply = service.object_session_query(session_id, 1, row_limit=5)

    assert queried["outcome"] == "query_completed"
    assert queried["session"]["active"] is True
    assert applied["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert applied["session"]["active"] is True
    assert queried_after_apply["outcome"] == "query_completed"
    assert queried_after_apply["session"]["active"] is True
    assert fake.cards[1]["description"] == "Still active"


def test_dashboard_session_query_ignores_volatile_embedded_card_query_metadata(runtime) -> None:
    service, fake = runtime
    fake.cards[1]["result_metadata"] = [
        {
            "name": "revenue",
            "fingerprint": {"type": {"type/Number": {"avg": 10.0, "sd": 1.0}}},
        }
    ]
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    original_get = fake.get_json
    original_query = fake.query_json

    def get_with_embedded_card(path, *, params=None):  # noqa: ANN001, ANN202
        payload = original_get(path, params=params)
        if path == "/api/dashboard/10":
            for dashcard in payload["dashcards"]:
                dashcard["card"] = copy.deepcopy(fake.cards[dashcard["card_id"]])
        return payload

    def query_with_usage_update(path, body):  # noqa: ANN001, ANN202
        result = original_query(path, body)
        fake.cards[1].update(
            {
                "last_used_at": "query-used",
                "view_count": 9,
                "query_average_duration": 123,
            }
        )
        fake.cards[1]["result_metadata"][0]["fingerprint"]["type"]["type/Number"].update(
            {"avg": 11.0, "sd": 1.5}
        )
        return result

    fake.get_json = get_with_embedded_card
    fake.query_json = query_with_usage_update
    opened = service.object_session_open("dashboard", 10)
    session_id = opened["session"]["session_id"]

    queried = service.object_session_query(session_id, 1, row_limit=5)
    applied = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Still active"}],
            }
        ],
    )

    assert queried["outcome"] == "query_completed"
    assert queried["session"]["active"] is True
    assert applied["outcome"] == Outcome.APPLIED_VERIFIED.value
    assert applied["session"]["active"] is True
    assert fake.dashboards[10]["description"] == "Still active"


@pytest.mark.parametrize(
    ("server_tab_changes", "expected_outcome", "expected_active", "expected_close_reason"),
    [
        ({"updated_at": "tab-u1"}, Outcome.APPLIED_VERIFIED.value, True, None),
        (
            {"position": 1, "updated_at": "tab-u1"},
            Outcome.OUTCOME_UNKNOWN.value,
            False,
            "apply_outcome_unknown",
        ),
    ],
    ids=["timestamp-only", "logical-drift"],
)
def test_dashboard_session_tab_rename_ignores_only_server_managed_item_timestamps(
    runtime,
    server_tab_changes: dict[str, Any],
    expected_outcome: str,
    expected_active: bool,
    expected_close_reason: str | None,
) -> None:
    service, fake = runtime
    fake.dashboards[10]["tabs"] = [
        {"id": 101, "name": "Main", "position": 0, "updated_at": "tab-u0"}
    ]
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
            "updated_at": "dashcard-u0",
        }
    ]
    original_put = fake.put_json

    def put_with_server_changes(path, body):  # noqa: ANN001, ANN202
        result = original_put(path, body)
        if path == "/api/dashboard/10":
            fake.dashboards[10]["tabs"][0].update(server_tab_changes)
            fake.dashboards[10]["dashcards"][0]["updated_at"] = "dashcard-u1"
        return result

    fake.put_json = put_with_server_changes
    opened = service.object_session_open("dashboard", 10)
    session_id = opened["session"]["session_id"]

    queried = service.object_session_query(session_id, 1, row_limit=5)
    applied = service.object_session_apply(
        session_id,
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [
                    {
                        "op": "dashboard_item_set",
                        "path": "/tabs",
                        "item_id": 101,
                        "item_path": "/name",
                        "value": "Overview",
                    }
                ],
            }
        ],
    )

    assert queried["outcome"] == "query_completed"
    assert applied["outcome"] == expected_outcome
    assert applied["session"]["active"] is expected_active
    assert applied["session"]["close_reason"] == expected_close_reason
    assert fake.dashboards[10]["tabs"][0]["name"] == "Overview"
    if expected_active:
        assert applied["session"]["actions_used"] == 2
    assert fake.dashboards[10]["dashcards"][0]["updated_at"] == "dashcard-u1"


def test_dashboard_session_still_detects_embedded_card_definition_drift(runtime) -> None:
    service, fake = runtime
    fake.dashboards[10]["dashcards"] = [
        {
            "id": 201,
            "card_id": 1,
            "row": 0,
            "col": 0,
            "size_x": 6,
            "size_y": 4,
            "dashboard_tab_id": 101,
            "parameter_mappings": [],
            "visualization_settings": {},
        }
    ]
    original_get = fake.get_json

    def get_with_embedded_card(path, *, params=None):  # noqa: ANN001, ANN202
        payload = original_get(path, params=params)
        if path == "/api/dashboard/10":
            for dashcard in payload["dashcards"]:
                dashcard["card"] = copy.deepcopy(fake.cards[dashcard["card_id"]])
        return payload

    fake.get_json = get_with_embedded_card
    opened = service.object_session_open("dashboard", 10)
    fake.cards[1].update(
        {
            "display": "bar",
            "updated_at": "external-definition-change",
            "last_used_at": "query-used",
        }
    )

    result = service.object_session_apply(
        opened["session"]["session_id"],
        [
            {
                "object_type": "dashboard",
                "object_id": 10,
                "operations": [{"op": "set", "path": "/description", "value": "Must not apply"}],
            }
        ],
    )

    assert result["outcome"] == Outcome.REJECTED_STALE.value
    assert result["session"]["close_reason"] == "stale_external_edit"
    assert result["stale_bindings"][0]["object_type"] == "dashboard"
    assert fake.put_calls == 0


def test_full_session_closes_on_exact_supported_version_drift(runtime) -> None:
    service, fake = runtime
    opened = service.object_session_open("question", 1)
    fake.version = "v0.63.3"

    with pytest.raises(MetabasePolicyError, match="exact server-version"):
        service.object_session_apply(
            opened["session"]["session_id"],
            [
                {
                    "object_type": "question",
                    "object_id": 1,
                    "operations": [{"op": "set", "path": "/description", "value": "Blocked"}],
                }
            ],
        )

    status = service.edit_sessions.get(opened["session"]["session_id"])
    assert status.closed is True
    assert status.close_reason == "identity_or_version_changed"
    assert fake.put_calls == 0


def test_main_keeps_runtime_visible_when_startup_preflight_fails(monkeypatch) -> None:
    class BrokenRuntime:
        def health(self):  # noqa: ANN201
            raise RuntimeError("temporary health failure")

    started: list[bool] = []
    monkeypatch.setattr(main_module, "get_runtime", lambda: BrokenRuntime())
    monkeypatch.setattr(main_module.mcp, "run", lambda: started.append(True))
    monkeypatch.setattr(sys, "argv", ["mcp-metabase"])

    main_module.main()

    assert started == [True]
