from __future__ import annotations

import copy

import pytest

from mcp_metabase.models import Action, ObjectType, PatchOperation, PlannedMutation
from mcp_metabase.normalization import (
    MutationValidationError,
    build_mutation,
    canonical_sha256,
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
                "dashboard_tab_id": 101,
                "visualization_settings": {"card.title": "Old", "card.hide_empty": False},
                "parameter_mappings": [
                    {
                        "parameter_id": "city-param",
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
