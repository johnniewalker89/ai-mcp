from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from fastmcp import Client
from test_service_and_surface import _install_ambiguous_second_write
from test_service_and_surface import runtime as runtime_fixture

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.models import Action
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError

runtime = runtime_fixture


@pytest.mark.parametrize("fallback", [False, True])
def test_dashboard_create_empty_tab_sends_coupled_arrays(runtime, fallback):
    service, fake = runtime
    original_put, original_post = fake.put_json, fake.post_json
    posts = []
    writes = []

    def post(path, body):
        posts.append(copy.deepcopy(body))
        if fallback and len(posts) == 1:
            raise MetabaseApiError("simulated", status_code=500, outcome_unknown=True)
        return original_post(path, body)

    def put(path, body):
        writes.append(copy.deepcopy(body))
        accepted = copy.deepcopy(body)
        if "dashcards" not in accepted:
            accepted.pop("tabs", None)
        return original_put(path, accepted)

    fake.post_json, fake.put_json = post, put
    plan = service.action_prepare(
        "dashboard_create",
        {
            "body": {
                "name": "Empty tab",
                "collection_id": 20,
                "width": "full",
                "tabs": [{"id": -1, "name": "Acceptance"}],
                "dashcards": [],
            }
        },
    )
    result = run(service, plan)
    assert result["outcome"] == "applied_verified"
    assert len(posts) == (2 if fallback else 1)
    assert len(writes) == 1 and writes[0]["dashcards"] == []
    assert fake.dashboards[result["created_object_id"]]["tabs"][0]["name"] == "Acceptance"


def populate(fake, count):
    template = copy.deepcopy(fake.dashboards[10])
    template["unknown_options"] = {"preserve": True}
    fake.dashboards = {i: {**copy.deepcopy(template), "id": i} for i in range(1, count + 1)}


def run(service, plan):
    return service.action_execute(plan["plan_id"], plan["digest"])


def test_dashboard_batch100_trash_restore_and_rollback_preserves_cards_and_layout(runtime):
    service, fake = runtime
    service.config = replace(service.config, max_batch_items=100)
    populate(fake, 100)
    before = copy.deepcopy(fake.dashboards)
    cards = copy.deepcopy(fake.cards)
    sent = []
    original = fake.put_json

    def put(path, body):
        sent.append(copy.deepcopy(body))
        return original(path, body)

    fake.put_json = put
    plan = service.action_prepare("dashboard_batch_trash", {"dashboard_ids": list(fake.dashboards)})
    assert len(plan["impact"]) == 100 and all(
        i["object_type"] == "dashboard" for i in plan["impact"]
    )
    result = run(service, plan)
    assert result["outcome"] == "applied_verified" and result["applied_indexes"] == list(range(100))
    assert result["work_session"] is None
    with pytest.raises(MetabasePolicyError):
        run(service, plan)
    restore = service.action_prepare(
        "dashboard_batch_restore", {"dashboard_ids": list(fake.dashboards), "collection_id": 30}
    )
    assert run(service, restore)["outcome"] == "applied_verified"
    rollback = service.rollback_prepare(restore["plan_id"])
    assert (
        service.exact_action_execute(
            rollback["plan_id"], rollback["digest"], expected_actions={Action.BATCH_ROLLBACK}
        )["outcome"]
        == "applied_verified"
    )
    assert (
        sent
        == [{"archived": True}] * 100
        + [{"archived": False, "collection_id": 30}] * 100
        + [{"archived": True, "collection_id": 20}] * 100
    )
    assert fake.cards == cards
    for i, dashboard in fake.dashboards.items():
        assert dashboard["archived"] and dashboard["collection_id"] == 20
        assert {k: v for k, v in dashboard.items() if k not in {"archived", "updated_at"}} == {
            k: v for k, v in before[i].items() if k not in {"archived", "updated_at"}
        }


@pytest.mark.parametrize("ids", [[], [1, 1], [True], [0], ["1"], list(range(1, 102))])
def test_dashboard_inventory_rejected_before_write(runtime, ids):
    service, fake = runtime
    service.config = replace(service.config, max_batch_items=100)
    with pytest.raises(MutationValidationError):
        service.action_prepare("dashboard_batch_trash", {"dashboard_ids": ids})
    assert fake.put_calls == 0


def test_dashboard_batch_stale_and_partial_rollback(runtime):
    service, fake = runtime
    populate(fake, 3)
    plan = service.action_prepare("dashboard_batch_trash", {"dashboard_ids": [1, 2, 3]})
    fake.dashboards[3]["description"] = "changed"
    result = run(service, plan)
    assert result["outcome"] == "rejected_stale" and fake.put_calls == 0
    assert result["object_results"][0]["stale_diagnostic"]["changed_roots"] == ["description"]
    plan = service.action_prepare("dashboard_batch_trash", {"dashboard_ids": [1, 2, 3]})
    fake.fail_put_call = 2
    result = run(service, plan)
    assert result["outcome"] == "partially_applied" and result["applied_indexes"] == [0]
    assert not fake.dashboards[2]["archived"] and not fake.dashboards[3]["archived"]
    rollback = service.rollback_prepare(plan["plan_id"])
    assert (
        service.exact_action_execute(
            rollback["plan_id"], rollback["digest"], expected_actions={Action.DASHBOARD_ROLLBACK}
        )["outcome"]
        == "applied_verified"
    )
    assert not any(d["archived"] for d in fake.dashboards.values())


def test_dashboard_batch_unknown_recovery_does_not_repeat_applied(runtime):
    service, fake = runtime
    populate(fake, 12)
    plan = service.action_prepare("dashboard_batch_trash", {"dashboard_ids": [11, 10, 12]})
    _install_ambiguous_second_write(fake, applied_before_error=True)
    result = run(service, plan)
    assert result["outcome"] == "applied_verified"
    assert result["verified_already_applied_indexes"] == [0, 1]
    assert result["recovery_write_indexes"] == [2] and fake.put_calls == 3


def test_dashboard_restore_to_root_and_wrong_initial_state(runtime):
    service, fake = runtime
    with pytest.raises(MutationValidationError):
        service.action_prepare("dashboard_batch_restore", {"dashboard_ids": [10]})
    fake.dashboards[10]["archived"] = True
    with pytest.raises(MutationValidationError):
        service.action_prepare("dashboard_batch_trash", {"dashboard_ids": [10]})
    plan = service.action_prepare(
        "dashboard_batch_restore", {"dashboard_ids": [10], "to_root": True}
    )
    assert run(service, plan)["outcome"] == "applied_verified"
    assert fake.dashboards[10]["collection_id"] is None


def test_dashboard_lifecycle_actions_in_public_protocol_and_generic_batch_stays_guarded(
    runtime, monkeypatch
):
    service, fake = runtime
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def check():
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 15
            schema = next(t.inputSchema for t in tools if t.name == "metabase_action_prepare")
            assert {"dashboard_batch_trash", "dashboard_batch_restore"} <= set(
                schema["properties"]["action"]["enum"]
            )
            plan = (
                await client.call_tool(
                    "metabase_action_prepare",
                    {"action": "dashboard_batch_trash", "arguments": {"dashboard_ids": [10]}},
                )
            ).data
            result = (
                await client.call_tool(
                    "metabase_action_execute",
                    {"plan_id": plan["plan_id"], "digest": plan["digest"]},
                )
            ).data
            assert result["outcome"] == "applied_verified" and result["work_session"] is None

    asyncio.run(check())
    with pytest.raises(MetabasePolicyError):
        service.action_prepare(
            "batch",
            {
                "items": [
                    {
                        "object_type": "dashboard",
                        "object_id": 10,
                        "operations": [{"op": "set", "path": "/archived", "value": False}],
                    }
                ]
            },
        )
    assert fake.put_calls == 1
