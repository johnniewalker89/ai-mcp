import copy

import pytest
from test_collection_batch_lifecycle import base_runtime as base_runtime
from test_collection_batch_lifecycle import prepare, run
from test_collection_batch_lifecycle import runtime as collection_runtime

from mcp_metabase.normalization import MutationValidationError

runtime = collection_runtime


@pytest.fixture
def timeline(runtime):
    service, fake = runtime
    original = fake.get_json
    state = {"id": 7, "name": "Annotations", "archived": False, "collection_id": 20, "events": []}

    def get(path, *, params=None):
        if path == "/api/timeline/7":
            return copy.deepcopy(state)
        response = original(path, params=params)
        if path == "/api/collection/20/items":
            response["data"].append({"model": "timeline", "id": 7})
            response["total"] += 1
        return response

    fake.get_json = get
    return service, fake, state


def test_empty_timeline_shell_trash_restore_preserves_ancillary_content(timeline):
    service, fake, state = timeline
    baseline = copy.deepcopy(state)
    plan = prepare(service, [20], empty_only=True)
    assert run(service, plan)["outcome"] == "applied_verified"
    restore = service.action_prepare("collection_batch_restore", {"collection_ids": [20]})
    assert run(service, restore)["outcome"] == "applied_verified"
    assert state == baseline and fake.put_calls == 2


def test_timeline_events_block_empty_only_but_not_normal_lifecycle(timeline):
    service, fake, state = timeline
    state["events"] = [{"id": 1, "archived": False, "name": "Keep", "timeline_id": 7}]
    with pytest.raises(MutationValidationError, match="active content"):
        prepare(service, [20], empty_only=True)
    assert fake.put_calls == 0
    assert run(service, prepare(service, [20]))["outcome"] == "applied_verified"


def test_timeline_change_after_plan_is_stale(timeline):
    service, fake, state = timeline
    plan = prepare(service, [20], empty_only=True)
    state["name"] = "Changed"
    assert run(service, plan)["outcome"] == "rejected_stale"
    assert fake.put_calls == 0
