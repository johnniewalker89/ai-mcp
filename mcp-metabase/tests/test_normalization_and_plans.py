from __future__ import annotations

import copy

import pytest

from mcp_metabase.models import Action, ObjectType, PatchOperation, PlannedMutation
from mcp_metabase.normalization import (
    MutationValidationError,
    build_mutation,
    canonical_sha256,
    project_state,
    verify_mutation,
)
from mcp_metabase.plans import ExactPlanStore, MetabasePolicyError


def _question() -> dict:
    return {
        "id": 1,
        "name": "Revenue",
        "description": "Stable",
        "display": "line",
        "dataset_query": {
            "type": "native",
            "database": 5,
            "native": {
                "query": "select * from fact where city = {{city}}",
                "template-tags": {
                    "city": {"type": "text", "default": "Moscow"},
                    "from": {"type": "date", "default": None},
                },
            },
        },
        "parameters": [],
        "parameter_mappings": [],
        "visualization_settings": {
            "graph": {"metrics": "count", "legacy": True},
            "series_settings": {"revenue": {"color": "#000000"}},
        },
        "archived": False,
        "collection_id": 20,
        "type": "question",
        "updated_at": "2026-08-24T10:00:00Z",
    }


def _dashboard() -> dict:
    return {
        "id": 10,
        "name": "Sales",
        "description": None,
        "parameters": [{"id": "city-param", "type": "location/city", "name": "City"}],
        "tabs": [{"id": 101, "name": "Main"}],
        "dashcards": [
            {
                "id": 110,
                "card_id": 1,
                "dashboard_tab_id": 101,
                "visualization_settings": {"card.title": "Old", "card.hide_empty": False},
                "parameter_mappings": [
                    {
                        "parameter_id": "city-param",
                        "card_id": 1,
                        "target": ["variable", ["template-tag", "city"]],
                    }
                ],
                "card": {
                    "dataset_query": {
                        "native": {
                            "query": "select {{city}}",
                            "template-tags": {
                                "city": {"type": "text", "widget-type": "location/city"}
                            },
                        }
                    }
                },
            }
        ],
        "archived": False,
        "collection_id": 20,
        "width": "fixed",
        "updated_at": "2026-08-24T10:00:00Z",
    }


def _dashboard_v063() -> dict:
    dashboard = _dashboard()
    dashboard["dashcards"][0]["card"]["dataset_query"] = {
        "lib/type": "mbql/query",
        "database": 5,
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
    dashboard["dashcards"][0]["parameter_mappings"][0]["target"] = [
        "dimension",
        ["template-tag", "city"],
    ]
    return dashboard


def _dashboard_v063_raw_value() -> dict:
    dashboard = _dashboard_v063()
    dashboard["parameters"][0]["type"] = "string/="
    dashboard["dashcards"][0]["card"]["dataset_query"]["stages"][0]["template-tags"][0].update(
        {"type": "text", "widget-type": "string/="}
    )
    dashboard["dashcards"][0]["parameter_mappings"][0]["target"] = [
        "variable",
        ["template-tag", "city"],
    ]
    dashboard["dashcards"][0]["parameter_mappings"][0].pop("card_id")
    return dashboard


def test_nested_question_patch_preserves_sql_tags_and_sibling_visualization() -> None:
    before = _question()
    mutation = build_mutation(
        object_type=ObjectType.QUESTION,
        raw_before=before,
        operations=[
            PatchOperation(
                op="set",
                path="/visualization_settings/graph/metrics",
                value="sum",
            )
        ],
    )

    assert mutation.write_payload == {
        "visualization_settings": {
            "graph": {"metrics": "sum", "legacy": True},
            "series_settings": {"revenue": {"color": "#000000"}},
        }
    }
    assert mutation.after_state["dataset_query"] == before["dataset_query"]


def test_question_verification_accepts_legacy_native_canonicalized_to_mbql_v2() -> None:
    before = _question()
    legacy = copy.deepcopy(before["dataset_query"])
    legacy["native"]["query"] = "select {{city}} as city"
    mutation = build_mutation(
        object_type=ObjectType.QUESTION,
        raw_before=before,
        operations=[PatchOperation(op="set", path="/dataset_query", value=legacy)],
    )
    readback = copy.deepcopy(mutation.after_state)
    readback["dataset_query"] = {
        "lib/type": "mbql/query",
        "lib.convert/converted?": True,
        "database": 5,
        "stages": [
            {
                "lib/type": "mbql.stage/native",
                "native": "select {{city}} as city",
                "template-tags": [
                    {"name": "from", "type": "date", "default": None},
                    {"name": "city", "type": "text", "default": "Moscow"},
                ],
            }
        ],
    }

    assert verify_mutation(mutation, readback) is True

    readback["dataset_query"]["stages"][0]["native"] = "select 2"
    assert verify_mutation(mutation, readback) is False

    readback["dataset_query"]["stages"][0]["native"] = "select {{city}} as city"
    readback["dataset_query"]["stages"][0]["template-tags"].append(
        {"name": "unexpected", "type": "text"}
    )
    assert verify_mutation(mutation, readback) is False


def test_question_verification_accepts_server_added_visual_defaults() -> None:
    mutation = build_mutation(
        object_type=ObjectType.QUESTION,
        raw_before=_question(),
        operations=[
            PatchOperation(
                op="set",
                path="/visualization_settings/graph/metrics",
                value="sum",
            )
        ],
    )
    readback = copy.deepcopy(mutation.after_state)
    readback["visualization_settings"]["server.default"] = True

    assert verify_mutation(mutation, readback) is True

    readback["visualization_settings"]["graph"].pop("legacy")
    assert verify_mutation(mutation, readback) is False


def test_null_is_not_remove_and_top_level_remove_is_forbidden() -> None:
    mutation = build_mutation(
        object_type=ObjectType.QUESTION,
        raw_before=_question(),
        operations=[PatchOperation(op="set", path="/description", value=None)],
    )
    assert "description" in mutation.write_payload
    assert mutation.write_payload["description"] is None

    with pytest.raises(MutationValidationError, match="top-level"):
        build_mutation(
            object_type=ObjectType.QUESTION,
            raw_before=_question(),
            operations=[PatchOperation(op="remove", path="/description")],
        )


def test_field_server_null_settings_normalizes_without_accepting_other_invalid_types() -> None:
    raw = {
        "id": 40,
        "name": "city",
        "display_name": "City",
        "description": None,
        "settings": None,
        "table_id": 60,
        "database_id": 50,
    }

    state = project_state(raw, ObjectType.FIELD)
    mutation = build_mutation(
        object_type=ObjectType.FIELD,
        raw_before=raw,
        operations=[PatchOperation(op="set", path="/display_name", value="City name")],
    )

    assert state["settings"] == {}
    assert mutation.before_state["settings"] == {}
    assert mutation.write_payload == {"display_name": "City name"}

    missing = copy.deepcopy(raw)
    missing.pop("settings")
    assert "settings" not in project_state(missing, ObjectType.FIELD)

    invalid = copy.deepcopy(raw)
    invalid["settings"] = []
    with pytest.raises(MutationValidationError, match="Field settings must be an object"):
        build_mutation(
            object_type=ObjectType.FIELD,
            raw_before=invalid,
            operations=[PatchOperation(op="set", path="/display_name", value="City name")],
        )


def test_dashboard_item_patch_preserves_other_element_fields() -> None:
    before = _dashboard()
    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="dashboard_item_set",
                path="/dashcards",
                item_id=110,
                item_path="/visualization_settings/card.title",
                value="New title",
            )
        ],
    )

    dashcard = mutation.write_payload["dashcards"][0]
    assert dashcard["visualization_settings"] == {
        "card.title": "New title",
        "card.hide_empty": False,
    }
    assert dashcard["parameter_mappings"] == before["dashcards"][0]["parameter_mappings"]


def test_dashboard_verification_keeps_requested_item_timestamp_strict() -> None:
    before = _dashboard()
    before["tabs"][0]["updated_at"] = "tab-u0"
    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="dashboard_item_set",
                path="/tabs",
                item_id=101,
                item_path="/updated_at",
                value="requested",
            )
        ],
    )
    readback = copy.deepcopy(mutation.after_state)
    readback["tabs"][0]["updated_at"] = "server-u1"

    assert verify_mutation(mutation, readback) is False


def test_dashboard_geo_mapping_type_mismatch_is_rejected() -> None:
    with pytest.raises(MutationValidationError, match="incompatible"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=_dashboard(),
            operations=[
                PatchOperation(
                    op="dashboard_item_set",
                    path="/parameters",
                    item_id="city-param",
                    item_path="/type",
                    value="string/=",
                )
            ],
        )


def test_dashboard_v063_template_tag_list_is_accepted() -> None:
    before = _dashboard_v063()

    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[PatchOperation(op="set", path="/description", value="Updated")],
    )

    assert mutation.after_state["dashcards"] == before["dashcards"]


def test_dashboard_v063_mapping_is_canonicalized_for_an_executable_write() -> None:
    before = _dashboard_v063()
    replacement = copy.deepcopy(before["dashcards"])

    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[PatchOperation(op="replace_array", path="/dashcards", value=replacement)],
    )

    mapping = mutation.write_payload["dashcards"][0]["parameter_mappings"][0]
    assert mapping["card_id"] == 1
    assert mapping["target"][-1] == {"stage-number": 0}
    assert "card" not in mutation.write_payload["dashcards"][0]


def test_dashboard_v063_raw_value_mapping_keeps_schema_valid_variable_target() -> None:
    before = _dashboard_v063_raw_value()

    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="replace_array",
                path="/dashcards",
                value=copy.deepcopy(before["dashcards"]),
            )
        ],
    )

    mapping = mutation.write_payload["dashcards"][0]["parameter_mappings"][0]
    assert mapping["card_id"] == 1
    assert mapping["target"] == ["variable", ["template-tag", "city"]]


def test_dashboard_v063_raw_value_mapping_rejects_stage_number() -> None:
    before = _dashboard_v063_raw_value()
    replacement = copy.deepcopy(before["dashcards"])
    replacement[0]["parameter_mappings"][0]["target"].append({"stage-number": 0})

    with pytest.raises(MutationValidationError, match="must not include stage-number"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=before,
            operations=[PatchOperation(op="replace_array", path="/dashcards", value=replacement)],
        )


def test_dashboard_v063_visual_write_rejects_an_unapproved_mapping_repair() -> None:
    before = _dashboard_v063()

    with pytest.raises(MutationValidationError, match="stage-number"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=before,
            operations=[
                PatchOperation(
                    op="dashboard_item_set",
                    path="/dashcards",
                    item_id=110,
                    item_path="/visualization_settings/card.title",
                    value="Updated",
                )
            ],
        )


@pytest.mark.parametrize(
    "mapping_change",
    [
        {"card_id": 2},
        {"target": ["variable", ["template-tag", "city"], {"stage-number": 1}]},
    ],
    ids=["foreign-card", "foreign-stage"],
)
def test_dashboard_v063_mapping_rejects_conflicting_execution_binding(
    mapping_change: dict,
) -> None:
    before = _dashboard_v063()
    replacement = copy.deepcopy(before["dashcards"])
    replacement[0]["parameter_mappings"][0].update(mapping_change)

    with pytest.raises(MutationValidationError, match="card_id|stage-number"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=before,
            operations=[PatchOperation(op="replace_array", path="/dashcards", value=replacement)],
        )


def test_dashboard_verification_rejects_saved_but_non_executable_native_mapping() -> None:
    before = _dashboard_v063()
    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="replace_array",
                path="/dashcards",
                value=copy.deepcopy(before["dashcards"]),
            )
        ],
    )
    readback = copy.deepcopy(mutation.after_state)
    readback["dashcards"][0]["parameter_mappings"][0]["target"].pop()

    assert verify_mutation(mutation, readback) is False


def test_dashboard_v063_missing_template_tag_is_rejected() -> None:
    before = _dashboard_v063()
    before["dashcards"][0]["card"]["dataset_query"]["stages"][0]["template-tags"] = []

    with pytest.raises(MutationValidationError, match="missing native template tag"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=before,
            operations=[PatchOperation(op="set", path="/description", value="Updated")],
        )


def test_collection_put_always_binds_current_archived_state() -> None:
    mutation = build_mutation(
        object_type=ObjectType.COLLECTION,
        raw_before={"id": 20, "name": "Team", "archived": True, "parent_id": None},
        operations=[PatchOperation(op="set", path="/name", value="Team BI")],
    )
    assert mutation.write_payload == {"name": "Team BI", "archived": True}


def _planned_mutation() -> PlannedMutation:
    before = _question()
    after = copy.deepcopy(before)
    after["name"] = "Changed"
    return PlannedMutation(
        object_type=ObjectType.QUESTION,
        object_id=1,
        before_state=before,
        after_state=after,
        write_payload={"name": "Changed"},
        changed_roots=("name",),
        before_sha256=canonical_sha256(before),
        after_sha256=canonical_sha256(after),
        target={"name": "Revenue"},
    )


def _store(clock=lambda: 100.0) -> ExactPlanStore:
    return ExactPlanStore(
        ttl_seconds=30,
        max_plans=5,
        max_plan_bytes=100_000,
        clock=clock,
    )


def test_exact_plan_digest_is_one_shot_and_target_bound() -> None:
    store = _store()
    plan = store.prepare(
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="fingerprint",
        identity_marker="user:7",
        server_version="v0.63.2",
        action=Action.QUESTION_UPDATE,
        mutations=[_planned_mutation()],
        arguments={"question_id": 1},
    )
    consumed = store.consume(
        plan.plan_id,
        plan.digest,
        instance=plan.instance,
        origin=plan.origin,
        credential_fingerprint=plan.credential_fingerprint,
        identity_marker=plan.identity_marker,
        server_version=plan.server_version,
        action=plan.action,
    )
    assert consumed.consumed is True
    with pytest.raises(MetabasePolicyError, match="already consumed"):
        store.consume(
            plan.plan_id,
            plan.digest,
            instance=plan.instance,
            origin=plan.origin,
            credential_fingerprint=plan.credential_fingerprint,
            identity_marker=plan.identity_marker,
            server_version=plan.server_version,
            action=plan.action,
        )

    second = store.prepare(
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="fingerprint",
        identity_marker="user:7",
        server_version="v0.63.2",
        action=Action.QUESTION_UPDATE,
        mutations=[_planned_mutation()],
        arguments={"question_id": 1},
    )
    second.mutations[0].target["name"] = "tampered"
    with pytest.raises(MetabasePolicyError, match="binding or digest changed"):
        store.consume(
            second.plan_id,
            second.digest,
            instance=second.instance,
            origin=second.origin,
            credential_fingerprint=second.credential_fingerprint,
            identity_marker=second.identity_marker,
            server_version=second.server_version,
            action=second.action,
        )


def test_exact_plan_expires_without_execution() -> None:
    now = [100.0]
    store = _store(clock=lambda: now[0])
    plan = store.prepare(
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="fingerprint",
        identity_marker="user:7",
        server_version="v0.63.2",
        action=Action.QUESTION_UPDATE,
        mutations=[_planned_mutation()],
        arguments={},
    )
    now[0] = 131.0
    with pytest.raises(MetabasePolicyError, match="expired"):
        store.peek(plan.plan_id)
