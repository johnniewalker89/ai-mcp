from __future__ import annotations

import pytest
from pydantic import ValidationError

from mcp_metabase.config import ConfigurationError, MetabaseConfig
from mcp_metabase.edit_sessions import EditSessionStore
from mcp_metabase.models import ObjectType, PatchOperation
from mcp_metabase.normalization import (
    MutationValidationError,
    build_mutation,
    rollback_mutation,
    validate_edit_session_operations,
    validate_edit_session_state_bindings,
)
from mcp_metabase.plans import MetabasePolicyError


def _store(clock: list[float]) -> EditSessionStore:
    return EditSessionStore(
        ttl_seconds=300,
        max_actions=3,
        max_sessions=4,
        clock=lambda: clock[0],
    )


def _open(store: EditSessionStore, **overrides):  # noqa: ANN003, ANN202
    values = {
        "instance": "test",
        "origin": "https://metabase.example.org",
        "credential_fingerprint": "credential",
        "identity_marker": "user:7",
        "server_version": "v0.63.2",
        "object_type": ObjectType.QUESTION,
        "object_id": 1,
        "state_sha256": "a" * 64,
        "ttl_seconds": 120,
        "max_actions": 2,
    }
    values.update(overrides)
    return store.open(**values)


def test_config_reads_and_bounds_edit_session_limits(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("METABASE_BASE_URL", "https://metabase.example.org")
    monkeypatch.setenv("METABASE_API_KEY", "mb_test_01234567890123456789")
    monkeypatch.setenv("METABASE_MCP_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("METABASE_MCP_EDIT_SESSION_TTL_SECONDS", "120")
    monkeypatch.setenv("METABASE_MCP_EDIT_SESSION_MAX_ACTIONS", "7")
    monkeypatch.setenv("METABASE_MCP_MAX_ACTIVE_EDIT_SESSIONS", "4")

    config = MetabaseConfig.from_env()
    assert config.edit_session_ttl_seconds == 120
    assert config.edit_session_max_actions == 7
    assert config.max_active_edit_sessions == 4

    monkeypatch.setenv("METABASE_MCP_EDIT_SESSION_TTL_SECONDS", "3601")
    with pytest.raises(ConfigurationError, match="outside its allowed bounds"):
        MetabaseConfig.from_env()


def test_edit_session_store_binds_identity_serializes_apply_and_counts_actions() -> None:
    clock = [100.0]
    store = _store(clock)
    session = _open(store)

    begun = store.begin_apply(
        session.session_id,
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="credential",
        identity_marker="user:7",
        server_version="v0.63.2",
    )
    assert begun.in_flight is True
    with pytest.raises(MetabasePolicyError, match="in-flight"):
        store.begin_apply(
            session.session_id,
            instance="test",
            origin="https://metabase.example.org",
            credential_fingerprint="credential",
            identity_marker="user:7",
            server_version="v0.63.2",
        )

    first = store.finish_applied(session.session_id, state_sha256="b" * 64)
    assert first.actions_used == 1
    assert first.closed is False
    store.begin_apply(
        session.session_id,
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="credential",
        identity_marker="user:7",
        server_version="v0.63.2",
    )
    second = store.finish_applied(session.session_id, state_sha256="c" * 64)
    assert second.actions_used == 2
    assert second.closed is True
    assert second.close_reason == "max_actions_reached"


def test_edit_session_store_does_not_expire_an_apply_already_in_flight() -> None:
    clock = [100.0]
    store = _store(clock)
    session = _open(store, ttl_seconds=60)
    store.begin_apply(
        session.session_id,
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="credential",
        identity_marker="user:7",
        server_version="v0.63.2",
    )

    clock[0] = 161.0
    finished = store.finish_applied(session.session_id, state_sha256="b" * 64)
    assert finished.actions_used == 1
    expired = store.get(session.session_id)
    assert expired.closed is True
    assert expired.close_reason == "expired"


def test_edit_session_store_cannot_close_an_apply_already_in_flight() -> None:
    clock = [100.0]
    store = _store(clock)
    session = _open(store)
    store.begin_apply(
        session.session_id,
        instance="test",
        origin="https://metabase.example.org",
        credential_fingerprint="credential",
        identity_marker="user:7",
        server_version="v0.63.2",
    )

    with pytest.raises(MetabasePolicyError, match="in flight"):
        store.close(session.session_id)

    current = store.get(session.session_id)
    assert current.closed is False
    assert current.in_flight is True
    store.finish_applied(session.session_id, state_sha256="b" * 64)


def test_edit_session_store_rejects_duplicate_and_out_of_bound_scope() -> None:
    clock = [100.0]
    store = _store(clock)
    _open(store)

    with pytest.raises(MetabasePolicyError, match="already owns"):
        _open(store)
    with pytest.raises(MetabasePolicyError, match="TTL"):
        _open(store, object_id=2, ttl_seconds=301)
    with pytest.raises(MetabasePolicyError, match="max-actions"):
        _open(store, object_id=2, max_actions=4)


def test_question_edit_session_allowlist_blocks_query_parameters_and_move() -> None:
    allowed = [
        PatchOperation(op="set", path="/display", value="bar"),
        PatchOperation(
            op="set",
            path="/visualization_settings/graph/colors",
            value={"revenue": "#509EE3"},
        ),
    ]
    validate_edit_session_operations(ObjectType.QUESTION, allowed)

    for path in ("/dataset_query", "/parameters", "/parameter_mappings", "/collection_id"):
        with pytest.raises(MutationValidationError, match="presentation"):
            validate_edit_session_operations(
                ObjectType.QUESTION,
                [PatchOperation(op="set", path=path, value={})],
            )


def test_edit_session_allowlist_blocks_interaction_behavior_and_invalid_metadata() -> None:
    blocked_visualization_operations = [
        PatchOperation(
            op="set",
            path="/visualization_settings/click_behavior",
            value={"type": "crossfilter"},
        ),
        PatchOperation(
            op="set",
            path="/visualization_settings",
            value={"column_settings": {"field": {"view_as": "link"}}},
        ),
        PatchOperation(
            op="set",
            path="/visualization_settings",
            value={"parameterMapping": {"parameter_id": "p1"}},
        ),
    ]
    for operation in blocked_visualization_operations:
        with pytest.raises(MutationValidationError, match="Click, link, and parameter"):
            validate_edit_session_operations(ObjectType.QUESTION, [operation])

    for operation in (
        PatchOperation(op="set", path="/description", value={"not": "text"}),
        PatchOperation(op="set", path="/width", value="wide"),
    ):
        object_type = (
            ObjectType.QUESTION if operation.path == "/description" else ObjectType.DASHBOARD
        )
        with pytest.raises(MutationValidationError, match="description|width"):
            validate_edit_session_operations(object_type, [operation])


def test_dashboard_edit_session_allows_existing_layout_but_blocks_composition() -> None:
    allowed = [
        PatchOperation(
            op="dashboard_item_set",
            path="/dashcards",
            item_id=201,
            item_path="/size_x",
            value=8,
        ),
        PatchOperation(
            op="dashboard_item_replace_array",
            path="/dashcards",
            item_id=201,
            item_path="/visualization_settings/graph.metrics",
            value=["revenue"],
        ),
        PatchOperation(
            op="dashboard_item_set",
            path="/tabs",
            item_id=101,
            item_path="/name",
            value="Overview",
        ),
    ]
    validate_edit_session_operations(ObjectType.DASHBOARD, allowed)

    blocked = [
        PatchOperation(op="replace_array", path="/dashcards", value=[]),
        PatchOperation(
            op="dashboard_item_set",
            path="/dashcards",
            item_id=201,
            item_path="/card_id",
            value=2,
        ),
        PatchOperation(
            op="dashboard_item_set",
            path="/dashcards",
            item_id=201,
            item_path="/parameter_mappings",
            value={},
        ),
    ]
    for operation in blocked:
        with pytest.raises(MutationValidationError, match="layout|identity|composition"):
            validate_edit_session_operations(ObjectType.DASHBOARD, [operation])

    with pytest.raises(ValidationError, match="valid integer|valid string"):
        PatchOperation(
            op="dashboard_item_set",
            path="/dashcards",
            item_id=True,
            item_path="/size_x",
            value=8,
        )


def test_dashboard_item_replace_array_preserves_other_dashcard_fields() -> None:
    before = {
        "id": 10,
        "name": "Sales",
        "parameters": [],
        "tabs": [{"id": 101, "name": "Main"}],
        "dashcards": [
            {
                "id": 201,
                "card_id": 1,
                "parameter_mappings": [],
                "visualization_settings": {
                    "graph.metrics": ["old"],
                    "click_behavior": {"type": "link", "targetId": 10},
                },
            }
        ],
        "archived": False,
        "collection_id": 20,
        "width": "fixed",
        "updated_at": "u0",
    }
    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="dashboard_item_replace_array",
                path="/dashcards",
                item_id=201,
                item_path="/visualization_settings/graph.metrics",
                value=["revenue"],
            )
        ],
    )

    dashcard = mutation.write_payload["dashcards"][0]
    assert mutation.write_payload["tabs"] == before["tabs"]
    assert dashcard["visualization_settings"]["graph.metrics"] == ["revenue"]
    assert dashcard["card_id"] == 1
    assert dashcard["parameter_mappings"] == []
    assert dashcard["visualization_settings"]["click_behavior"] == {
        "type": "link",
        "targetId": 10,
    }


def test_dashboard_tab_rename_payload_includes_unchanged_dashcards() -> None:
    before = {
        "id": 10,
        "name": "Sales",
        "parameters": [],
        "tabs": [{"id": 101, "name": "Main"}],
        "dashcards": [
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
        ],
        "archived": False,
        "collection_id": 20,
        "width": "fixed",
        "updated_at": "u0",
    }

    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[
            PatchOperation(
                op="dashboard_item_set",
                path="/tabs",
                item_id=101,
                item_path="/name",
                value="Overview",
            )
        ],
    )

    assert mutation.changed_roots == ("tabs",)
    assert mutation.write_payload["tabs"] == [{"id": 101, "name": "Overview"}]
    assert mutation.write_payload["dashcards"] == before["dashcards"]

    rollback = rollback_mutation(mutation, mutation.after_state)
    assert rollback.changed_roots == ("tabs",)
    assert rollback.write_payload["tabs"] == before["tabs"]
    assert rollback.write_payload["dashcards"] == before["dashcards"]


def test_dashboard_coupled_payload_requires_a_complete_snapshot() -> None:
    incomplete = {
        "id": 10,
        "name": "Sales",
        "parameters": [],
        "tabs": [{"id": 101, "name": "Main"}],
        "archived": False,
        "collection_id": 20,
        "width": "fixed",
    }

    with pytest.raises(MutationValidationError, match="complete tabs and dashcards"):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=incomplete,
            operations=[
                PatchOperation(
                    op="dashboard_item_set",
                    path="/tabs",
                    item_id=101,
                    item_path="/name",
                    value="Overview",
                )
            ],
        )


def test_dashboard_edit_session_move_binds_to_an_existing_tab() -> None:
    state = {
        "tabs": [{"id": 101, "name": "Main"}],
    }
    valid = PatchOperation(
        op="dashboard_item_set",
        path="/dashcards",
        item_id=201,
        item_path="/dashboard_tab_id",
        value=101,
    )
    validate_edit_session_state_bindings(ObjectType.DASHBOARD, [valid], state)

    invalid = valid.model_copy(update={"value": 999})
    with pytest.raises(MutationValidationError, match="existing dashboard tab"):
        validate_edit_session_state_bindings(ObjectType.DASHBOARD, [invalid], state)
