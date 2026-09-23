from __future__ import annotations

import copy
import json

import pytest

from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.service import MetabaseRuntime


def pulse(subscription_id=10, archived=False):
    return {
        "id": subscription_id,
        "dashboard_id": 1392,
        "archived": archived,
        "name": "Weekly",
        "creator": {"password": "hydrated-secret"},
        "cards": [{"id": 7, "include_csv": True, "dataset_query": "hidden-sql"}],
        "channels": [
            {
                "id": 1,
                "channel_type": "slack",
                "enabled": True,
                "schedule_type": "weekly",
                "schedule_day": "mon",
                "schedule_hour": 9,
                "details": {"channel": "#reports", "token": "hidden-token"},
                "recipients": [{"id": 7, "email": "fixture@example.org", "secret": "hidden"}],
            }
        ],
    }


class PulseApi:
    def __init__(self):
        self.rows = [pulse(), pulse(11, True)]
        self.calls = []

    def get_json(self, path, *, params=None):
        self.calls.append((path, params))
        if path == "/api/pulse":
            return copy.deepcopy(
                [r for r in self.rows if r["archived"] == (params["archived"] == "true")]
            )
        return copy.deepcopy(self.rows[0])


@pytest.fixture
def runtime(configured):
    service = MetabaseRuntime(configured)
    service.http.close()
    service.http = PulseApi()
    return service


def test_filtered_read_projection_and_paging(runtime):
    page = runtime.notification_list(dashboard_id=1392, include_inactive=True, limit=1)
    assert page["total"] == 2 and page["next_offset"] == 1
    assert runtime.http.calls == [
        ("/api/pulse", {"dashboard_id": 1392, "archived": "false"}),
        ("/api/pulse", {"dashboard_id": 1392, "archived": "true"}),
    ]
    second = runtime.notification_list(dashboard_id=1392, include_inactive=True, offset=1)
    assert second["items"][0]["archived"] is True and not second["truncated"]
    exact = runtime.object_get("dashboard_subscription", 10)
    item = exact["subscription"]
    assert item["settings_complete"] and item["write_supported"] is False
    assert item["channels"][0]["destination"] == {"channel": "#reports"}
    for forbidden in ("hydrated-secret", "hidden-sql", "hidden-token"):
        assert forbidden not in json.dumps([page, exact])


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"question_id": 1, "dashboard_id": 1392},
        {"dashboard_id": True},
        {"dashboard_id": -1},
        {"dashboard_id": 1392, "limit": 0},
    ],
)
def test_invalid_selectors_fail_before_io(runtime, arguments):
    with pytest.raises(MutationValidationError):
        runtime.notification_list(**arguments)
    assert runtime.http.calls == []


def test_wrong_dashboard_and_duplicate_bindings_rejected(runtime):
    runtime.http.rows[0]["dashboard_id"] = 999
    with pytest.raises(MutationValidationError, match="binding"):
        runtime.notification_list(dashboard_id=1392)
    runtime.http.rows = [pulse(), pulse()]
    with pytest.raises(MutationValidationError, match="duplicate"):
        runtime.notification_list(dashboard_id=1392)
    with pytest.raises(MutationValidationError, match="binding"):
        runtime.object_get("dashboard_subscription", 42)


def test_incomplete_settings_are_explicit_and_nested_bounds_enforced(runtime):
    runtime.http.rows[0].pop("channels")
    result = runtime.object_get("dashboard_subscription", 10)["subscription"]
    assert result["channels"] is None and not result["settings_complete"]
    runtime.http.rows[0] = pulse()
    runtime.http.rows[0]["channels"][0]["recipients"] *= 51
    with pytest.raises(MutationValidationError, match="bound"):
        runtime.object_get("dashboard_subscription", 10)
