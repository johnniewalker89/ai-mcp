from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

import pytest
from fastmcp import Client
from test_service_and_surface import StatefulApi

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError
from mcp_metabase.service import MetabaseRuntime


class TimelineApi(StatefulApi):
    """v0.63.15 timeline.clj: GET includes all events; PUT cascades both ways."""

    def __init__(self):
        super().__init__()
        self.timelines = {
            7: {
                "id": 7,
                "name": "Release",
                "collection_id": 20,
                "description": None,
                "icon": "star",
                "default": False,
                "archived": False,
                "events": [
                    {"id": 8, "timeline_id": 7, "name": "Live", "archived": False},
                    {"id": 9, "timeline_id": 7, "name": "Old", "archived": True},
                ],
            }
        }
        self.lose_create = False
        self.lose_update = False
        self.skip_event_cascade = False
        self.timeline_reads = []

    def get_json(self, path, *, params=None):
        if path.startswith("/api/timeline/"):
            assert params == {"include": "events", "archived": "true"}
            self.timeline_reads.append(path)
            return copy.deepcopy(self.timelines[int(path.rsplit("/", 1)[1])])
        return super().get_json(path, params=params)

    def post_json(self, path, body):
        if path != "/api/timeline":
            return super().post_json(path, body)
        self.post_calls += 1
        assert "events" not in body
        self.timelines[1000] = {"id": 1000, **copy.deepcopy(body), "events": []}
        if self.lose_create:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return {"id": 1000}

    def put_json(self, path, body):
        if not path.startswith("/api/timeline/"):
            return super().put_json(path, body)
        self.put_calls += 1
        assert set(body) == {"archived"}
        state = self.timelines[int(path.rsplit("/", 1)[1])]
        state.update(body)
        if not self.skip_event_cascade:
            for event in state["events"]:
                event["archived"] = body["archived"]
        if self.lose_update:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return {k: v for k, v in state.items() if k != "events"}


@pytest.fixture
def runtime(configured, monkeypatch):
    fake = TimelineApi()
    service = MetabaseRuntime(configured)
    service.http.close()
    service.http = fake
    monkeypatch.setattr("mcp_metabase.service.time.sleep", lambda _: None)
    return service, fake


def run(service, plan):
    return service.action_execute(plan["plan_id"], plan["digest"])


@pytest.mark.parametrize("archived", [False, True])
def test_create_readback_and_cleanup_route(runtime, archived):
    service, fake = runtime
    body = {"name": "Acceptance", "collection_id": 20, "archived": archived}
    plan = service.action_prepare("timeline_create", {"body": body})
    assert fake.post_calls == 0
    result = run(service, plan)
    assert result["outcome"] == "applied_verified"
    assert result["created_object_id"] == 1000
    assert result["cleanup_candidate"]["recommended_action"] == "timeline_archive"
    assert result["work_session"] is None
    read = service.object_get("timeline", 1000)
    assert read["object"]["archived"] is archived
    assert read["object"]["events"] == [] and len(read["state_sha256"]) == 64
    assert fake.post_calls == 1


@pytest.mark.parametrize(
    "change",
    [
        {"name": " "},
        {"collection_id": True},
        {"collection_id": -1},
        {"icon": "balloons"},
        {"archived": "true"},
        {"default": 1},
        {"events": []},
    ],
)
def test_invalid_create_is_action_specific_and_does_not_write(runtime, change):
    service, fake = runtime
    body = {"name": "Acceptance", "collection_id": 20, **change}
    with pytest.raises(MutationValidationError, match="timeline_create"):
        service.action_prepare("timeline_create", {"body": body})
    assert fake.post_calls == 0


def test_create_requires_explicit_destination(runtime):
    service, _ = runtime
    with pytest.raises(MutationValidationError, match="collection_id"):
        service.action_prepare("timeline_create", {"body": {"name": "Acceptance"}})


def test_lost_create_response_never_reposts(runtime):
    service, fake = runtime
    fake.lose_create = True
    plan = service.action_prepare(
        "timeline_create", {"body": {"name": "Acceptance", "collection_id": 20}}
    )
    assert run(service, plan)["outcome"] == "outcome_unknown"
    with pytest.raises(MetabasePolicyError):
        run(service, plan)
    assert fake.post_calls == 1 and 1000 in fake.timelines


def test_collection_drift_blocks_create(runtime):
    service, fake = runtime
    plan = service.action_prepare(
        "timeline_create", {"body": {"name": "Acceptance", "collection_id": 20}}
    )
    fake.collections[20]["name"] = "Changed"
    assert run(service, plan)["outcome"] == "rejected_stale"
    assert fake.post_calls == 0


def test_archive_and_restore_explicitly_cascade_to_all_events(runtime):
    service, fake = runtime
    plan = service.action_prepare("timeline_archive", {"timeline_id": 7})
    impact = plan["impact"][0]["target"]
    assert impact["event_ids"] == [8, 9] and impact["events_changing_state"] == 1
    assert "reactivates all" in impact["side_effects"]
    assert run(service, plan)["outcome"] == "applied_verified"
    assert all(e["archived"] for e in fake.timelines[7]["events"])
    with pytest.raises(MetabasePolicyError, match="rollback is unavailable"):
        service.rollback_prepare(plan["plan_id"])
    restore = service.action_prepare("timeline_restore", {"timeline_id": 7})
    assert run(service, restore)["outcome"] == "applied_verified"
    assert all(not e["archived"] for e in fake.timelines[7]["events"])
    assert fake.put_calls == 2


@pytest.mark.parametrize("drift", ["event_edit", "event_added", "collection"])
def test_lifecycle_drift_prevents_put(runtime, drift):
    service, fake = runtime
    plan = service.action_prepare("timeline_archive", {"timeline_id": 7})
    if drift == "event_edit":
        fake.timelines[7]["events"][0]["name"] = "Concurrent edit"
    elif drift == "event_added":
        fake.timelines[7]["events"].append({"id": 10, "timeline_id": 7, "archived": False})
    else:
        fake.collections[20]["name"] = "Concurrent edit"
    assert run(service, plan)["outcome"] == "rejected_stale"
    assert fake.put_calls == 0


def test_missing_cascade_is_not_reported_as_verified(runtime):
    service, fake = runtime
    fake.skip_event_cascade = True
    plan = service.action_prepare("timeline_archive", {"timeline_id": 7})
    assert run(service, plan)["outcome"] != "applied_verified"
    assert fake.put_calls == 1


def test_lost_update_response_reconciles_without_second_write(runtime):
    service, fake = runtime
    fake.lose_update = True
    plan = service.action_prepare("timeline_archive", {"timeline_id": 7})
    assert run(service, plan)["outcome"] == "applied_verified"
    assert fake.put_calls == 1


@pytest.mark.parametrize(
    "defect",
    [
        "wrong_id",
        "missing_events",
        "duplicate",
        "truncated",
        "bound",
        "missing_collection",
        "bad_collection",
        "missing_name",
    ],
)
def test_incomplete_timeline_cannot_be_read_or_planned(runtime, defect):
    service, fake = runtime
    state = fake.timelines[7]
    if defect == "wrong_id":
        state["id"] = 8
    elif defect == "missing_events":
        state.pop("events")
    elif defect == "duplicate":
        state["events"].append(copy.deepcopy(state["events"][0]))
    elif defect == "truncated":
        state["has_more"] = True
    elif defect == "missing_collection":
        state.pop("collection_id")
    elif defect == "bad_collection":
        state["collection_id"] = True
    elif defect == "missing_name":
        state.pop("name")
    else:
        service.config = replace(service.config, max_list_items=1)
    with pytest.raises(MutationValidationError):
        service.object_get("timeline", 7)
    with pytest.raises(MutationValidationError):
        service.action_prepare("timeline_archive", {"timeline_id": 7})
    assert fake.put_calls == 0


def test_lifecycle_rejects_wrong_initial_state_and_unsafe_routes(runtime):
    service, fake = runtime
    with pytest.raises(MutationValidationError, match="initial"):
        service.action_prepare("timeline_restore", {"timeline_id": 7})
    with pytest.raises(MutationValidationError):
        service.action_prepare("timeline_delete", {"timeline_id": 7})
    with pytest.raises(MutationValidationError):
        service.action_prepare("timeline_archive", {"timeline_id": True})
    assert fake.put_calls == 0


def test_protocol_stays_compact_and_exposes_timeline(runtime, monkeypatch):
    service, _ = runtime
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def check():
        async with Client(server.mcp) as client:
            catalog = {tool.name: tool for tool in await client.list_tools()}
            assert len(catalog) == 15
            schema = catalog["metabase_action_prepare"].inputSchema
            assert "timeline_create" in schema["properties"]["action"]["enum"]
            schema = catalog["metabase_object_get"].inputSchema
            assert "timeline" in schema["properties"]["object_type"]["enum"]
            read = await client.call_tool(
                "metabase_object_get",
                {
                    "object_type": "timeline",
                    "object_id": 7,
                },
            )
            assert not read.is_error

    asyncio.run(check())
