from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import replace

import pytest
from fastmcp import Client
from test_service_and_surface import runtime as runtime_fixture

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.models import Action
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError

base_runtime = runtime_fixture


@pytest.fixture
def runtime(base_runtime):
    service, fake = base_runtime
    get, put = fake.get_json, fake.put_json
    fake.admin = True
    fake.cards = {}
    fake.dashboards = {}
    fake.collections[30]["parent_id"] = None

    def admin_get(path, *, params=None):
        result = get(path, params=params)
        if path == "/api/user/current":
            result["is_superuser"] = fake.admin
        return result

    def cascade_put(path, body):
        match = re.fullmatch(r"/api/collection/(\d+)", path)
        nodes = set()
        if match:
            nodes.add(int(match[1]))
            while True:
                children = {
                    i for i, obj in fake.collections.items() if obj.get("parent_id") in nodes
                }
                if children <= nodes:
                    break
                nodes |= children
        result = put(path, body)
        if match and "archived" in body:
            for cid in nodes:
                fake.collections[cid]["archived"] = body["archived"]
            for obj in [*fake.cards.values(), *fake.dashboards.values()]:
                if obj.get("collection_id") in nodes:
                    obj["archived"] = body["archived"]
        return result

    fake.get_json, fake.put_json = admin_get, cascade_put
    return service, fake


def run(service, plan):
    return service.action_execute(plan["plan_id"], plan["digest"])


def prepare(service, ids, **kwargs):
    return service.action_prepare("collection_batch_trash", {"collection_ids": ids, **kwargs})


def test_batch100_empty_trash_restore_rollback(runtime):
    service, fake = runtime
    service.config = replace(service.config, max_batch_items=100)
    template = copy.deepcopy(fake.collections[20])
    fake.collections = {i: {**copy.deepcopy(template), "id": i} for i in range(1, 101)}
    plan = prepare(service, list(fake.collections), empty_only=True)
    result = run(service, plan)
    assert result["outcome"] == "applied_verified"
    assert result["applied_indexes"] == list(range(100)) and result["work_session"] is None
    with pytest.raises(MetabasePolicyError):
        run(service, plan)
    restore = service.action_prepare(
        "collection_batch_restore", {"collection_ids": list(fake.collections), "to_root": True}
    )
    assert run(service, restore)["outcome"] == "applied_verified"
    rollback = service.rollback_prepare(restore["plan_id"])
    assert (
        service.exact_action_execute(
            rollback["plan_id"], rollback["digest"], expected_actions={Action.BATCH_ROLLBACK}
        )["outcome"]
        == "applied_verified"
    )
    assert all(c["archived"] for c in fake.collections.values())


def test_nested_empty_and_overlapping_ids_collapse_with_cascade_readback(runtime):
    service, fake = runtime
    fake.collections[30]["parent_id"] = 20
    plan = prepare(service, [30, 20], empty_only=True)
    assert len(plan["impact"]) == 1 and plan["impact"][0]["object_id"] == 20
    assert plan["impact"][0]["target"]["covered_requested_ids"] == [30, 20]
    result = run(service, plan)
    assert result["outcome"] == "applied_verified" and fake.put_calls == 1
    assert result["object_results"][0]["cascade_readback"]["objects"] == [
        {"object_type": "collection", "object_id": 30, "verified": True}
    ]
    rollback = service.rollback_prepare(plan["plan_id"])
    result = service.exact_action_execute(
        rollback["plan_id"], rollback["digest"], expected_actions={Action.COLLECTION_ROLLBACK}
    )
    assert result["outcome"] == "applied_verified"
    assert not fake.collections[20]["archived"] and not fake.collections[30]["archived"]


def test_empty_only_rejects_nested_content_ordinary_batch_and_single_still_work(runtime):
    service, fake = runtime
    fake.collections[30]["parent_id"] = 20
    fake.cards[1] = {"id": 1, "archived": False, "collection_id": 30}
    with pytest.raises(MutationValidationError, match="active content"):
        prepare(service, [20], empty_only=True)
    assert fake.put_calls == 0
    assert run(service, prepare(service, [20], empty_only=False))["outcome"] == "applied_verified"
    assert fake.cards[1]["archived"]
    plan = service.action_prepare("collection_batch_restore", {"collection_ids": [20]})
    assert run(service, plan)["outcome"] == "applied_verified" and not fake.cards[1]["archived"]
    fake.admin = False
    plan = service.action_prepare("collection_trash", {"collection_id": 20})
    assert run(service, plan)["outcome"] == "applied_verified"


@pytest.mark.parametrize("ids", [[], [20, 20], [True], [0], ["20"], list(range(1, 102))])
def test_invalid_inventory(runtime, ids):
    service, fake = runtime
    with pytest.raises(MutationValidationError):
        prepare(service, ids)
    assert fake.put_calls == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [], "total": 1},
        {"data": []},
        {"data": [], "total": 0, "truncated": True},
        {"data": [], "total": 0, "has_more": True},
    ],
)
def test_incomplete_listing_rejected(runtime, payload):
    service, fake = runtime
    original = fake.get_json
    fake.get_json = lambda path, **kwargs: (
        payload if path.endswith("/items") else original(path, **kwargs)
    )
    with pytest.raises(MutationValidationError, match="complete"):
        prepare(service, [20], empty_only=True)
    assert fake.put_calls == 0


def test_admin_binding_and_protected_root(runtime):
    service, fake = runtime
    fake.admin = False
    with pytest.raises(MetabasePolicyError, match="superuser"):
        prepare(service, [20])

    fake.admin = True
    plan = prepare(service, [20])
    fake.admin = False
    assert run(service, plan)["outcome"] == "rejected_validation" and fake.put_calls == 0
    fake.admin = True
    fake.collections[20]["personal_owner_id"] = 7
    with pytest.raises(MutationValidationError, match="protected"):
        prepare(service, [20])


def test_provider_empty_page_with_null_total(runtime):
    service, fake = runtime
    original = fake.get_json

    def get(path, **kwargs):
        if path.endswith("/items"):
            return {"data": [], "total": None, "offset": 0, "limit": service.config.max_list_items}
        return original(path, **kwargs)

    fake.get_json = get
    assert run(service, prepare(service, [20], empty_only=True))["outcome"] == "applied_verified"


@pytest.mark.parametrize("after_first", [False, True])
def test_new_content_blocks_before_each_write(runtime, after_first):
    service, fake = runtime
    plan = prepare(service, [20, 30], empty_only=True)
    original = fake.put_json

    def put(path, body):
        result = original(path, body)
        fake.cards[1] = {"id": 1, "collection_id": 30, "archived": False}
        return result

    if after_first:
        fake.put_json = put
    else:
        fake.cards[1] = {"id": 1, "collection_id": 30, "archived": False}
    result = run(service, plan)
    assert result["outcome"] == ("partially_applied" if after_first else "rejected_stale")
    assert result["applied_indexes"] == ([0] if after_first else [])
    assert not fake.collections[30]["archived"] and not fake.cards[1]["archived"]
    assert fake.put_calls == int(after_first)
    assert result["object_results"][-1]["stale_diagnostic"]["inventory_drift"]


def test_incomplete_cascade_blocks_recovery_without_repeating_write(runtime):
    service, fake = runtime
    fake.collections[30]["parent_id"] = 20
    original = fake.put_json

    def put(path, body):
        result = original(path, body)
        fake.collections[30]["archived"] = False
        return result

    fake.put_json = put
    result = run(service, prepare(service, [20], empty_only=True))
    assert result["outcome"] == "outcome_unknown" and fake.put_calls == 1
    assert result["recovery_blocked_reason"] == "inconclusive_full_inventory_readback"
    assert result["object_results"][0]["cascade_readback"]["verified"] is False


def test_restore_destination_inside_selected_tree_rejected(runtime):
    service, fake = runtime
    fake.collections[30]["parent_id"] = 20
    for c in fake.collections.values():
        c["archived"] = True
    with pytest.raises(MutationValidationError, match="inside"):
        service.action_prepare(
            "collection_batch_restore", {"collection_ids": [20], "parent_id": 30}
        )
    assert fake.put_calls == 0


def test_incomplete_second_preflight_preserves_applied_prefix(runtime):
    service, fake = runtime
    plan = prepare(service, [20, 30], empty_only=True)
    original = fake.get_json

    def get(path, **kwargs):
        if fake.put_calls and path == "/api/collection/30/items":
            return {"data": [], "total": 1}
        return original(path, **kwargs)

    fake.get_json = get
    result = run(service, plan)
    assert result["outcome"] == "partially_applied" and result["applied_indexes"] == [0]
    assert fake.put_calls == 1 and not fake.collections[30]["archived"]


def test_unknown_write_reconciles_without_repeating(runtime):
    service, fake = runtime
    original = fake.put_json

    def put(path, body):
        result = original(path, body)
        if path == "/api/collection/20":
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return result

    fake.put_json = put
    result = run(service, prepare(service, [20, 30], empty_only=True))
    assert result["outcome"] == "applied_verified" and fake.put_calls == 2


def test_restore_contract_and_protocol(runtime, monkeypatch):
    service, fake = runtime
    for args in (
        {"collection_ids": [20], "parent_id": 30, "to_root": True},
        {"collection_ids": [20], "empty_only": True},
    ):
        with pytest.raises(MutationValidationError):
            service.action_prepare("collection_batch_restore", args)
    for value in (None, 1, "true"):
        with pytest.raises(MutationValidationError):
            prepare(service, [20], empty_only=value)
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def check():
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 15
            schema = next(t.inputSchema for t in tools if t.name == "metabase_action_prepare")
            assert {"collection_batch_trash", "collection_batch_restore"} <= set(
                schema["properties"]["action"]["enum"]
            )
            p = (
                await client.call_tool(
                    "metabase_action_prepare",
                    {
                        "action": "collection_batch_trash",
                        "arguments": {"collection_ids": [20], "empty_only": True},
                    },
                )
            ).data
            r = (
                await client.call_tool(
                    "metabase_action_execute", {"plan_id": p["plan_id"], "digest": p["digest"]}
                )
            ).data
            assert r["outcome"] == "applied_verified" and r["work_session"] is None

    asyncio.run(check())
