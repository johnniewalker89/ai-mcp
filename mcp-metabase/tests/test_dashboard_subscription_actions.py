from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastmcp import Client
from test_service_and_surface import StatefulApi

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.models import ObjectType
from mcp_metabase.normalization import MutationValidationError, project_state
from mcp_metabase.plans import MetabasePolicyError
from mcp_metabase.service import MetabaseRuntime


def channel(*, channel_id=71, kind="email", enabled=True):
    return {
        "id": channel_id,
        "pulse_id": 70,
        "channel_type": kind,
        "channel_id": None,
        "enabled": enabled,
        "schedule_type": "weekly",
        "schedule_hour": 9,
        "schedule_day": "mon",
        "schedule_frame": None,
        "details": {"include_pdf": True, "private_future_setting": "preserve-hidden"}
        if kind == "email"
        else {"channel": "#reports", "channel_id": "C123", "token": "hidden-token"},
        "recipients": [{"id": 7, "email": "fixture@example.org", "first_name": "Hidden"}]
        if kind == "email"
        else [],
    }


def card_ref():
    return {
        "id": 1,
        "dashboard_card_id": 201,
        "include_csv": True,
        "include_xls": False,
        "format_rows": True,
        "pivot_results": False,
    }


def pulse():
    return {
        "id": 70,
        "dashboard_id": 10,
        "name": "Weekly",
        "archived": False,
        "skip_if_empty": True,
        "can_write": True,
        "parameters": [{"id": "city", "type": "string/=", "value": ["Moscow"]}],
        "cards": [{**card_ref(), "dataset_query": {"hidden": "hydrated-sql"}}],
        "channels": [channel(), channel(channel_id=72, kind="slack", enabled=False)],
        "creator": {"password": "hydrated-secret"},
        "future_root": "untouched",
        "updated_at": "before",
    }


def create_body(*, enabled=False, kind="email"):
    row = channel(kind=kind, enabled=enabled)
    row.pop("id")
    row.pop("pulse_id")
    row["details"] = {"include_pdf": True} if kind == "email" else {"channel": "#reports"}
    row["recipients"] = [{"email": "fixture@example.org"}] if kind == "email" else []
    return {
        "dashboard_id": 10,
        "name": "New weekly",
        "cards": [card_ref()],
        "channels": [row],
        "parameters": [{"id": "city", "type": "string/=", "value": ["Moscow"]}],
        "skip_if_empty": True,
    }


class PulseApi(StatefulApi):
    """Faithful Pulse defaulting, archive hook, hydration and channel replacement."""

    def __init__(self):
        super().__init__()
        self.dashboards[10]["dashcards"] = [{"id": 201, "card_id": 1}]
        self.dashboards[10]["parameters"] = [{"id": "city", "type": "string/="}]
        self.pulses = {70: pulse()}
        self.sent = []
        self.admin = True
        self.lost_response = False
        self.readback_failure = False
        self.fail_status = None
        self.test_response = {"ok": True}
        self.created_mismatch = False
        self.update_ignored = False

    def get_json(self, path, *, params=None):
        if path == "/api/user/current":
            return {**super().get_json(path), "is_superuser": self.admin}
        if path.startswith("/api/pulse/"):
            if self.readback_failure and self.sent:
                raise MetabaseApiError("read unavailable", status_code=503)
            return copy.deepcopy(self.pulses[int(path.rsplit("/", 1)[1])])
        return super().get_json(path, params=params)

    def put_json(self, path, body):
        assert path == "/api/pulse/70"
        self.put_calls += 1
        self.sent.append((path, copy.deepcopy(body)))
        if self.fail_status is not None:
            raise MetabaseApiError("provider denied", status_code=self.fail_status)
        if not self.update_ignored:
            state = self.pulses[70]
            archived = body.get("archived", False)
            if state["archived"] != archived:
                for row in state["channels"]:
                    row["enabled"] = not archived
            state["archived"] = archived
            state["skip_if_empty"] = body.get("skip_if_empty", False)
            # Complete inventory replaces channels after the archive hook.
            state.update(copy.deepcopy(body))
            state["channels"].reverse()
            state["updated_at"] = "after"
        if self.lost_response:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return copy.deepcopy(self.pulses[70])

    def post_json(self, path, body):
        assert path in {"/api/pulse", "/api/pulse/test"}
        self.post_calls += 1
        self.sent.append((path, copy.deepcopy(body)))
        if self.fail_status is not None:
            raise MetabaseApiError("provider denied", status_code=self.fail_status)
        if path == "/api/pulse":
            state = {**copy.deepcopy(body), "id": 80, "archived": False, "can_write": True}
            for index, row in enumerate(state["channels"]):
                row["id"] = 81 + index
                row["pulse_id"] = 80
                for recipient in row["recipients"]:
                    recipient["first_name"] = "Hydrated"
            state["channels"].reverse()
            if self.created_mismatch:
                state["skip_if_empty"] = False
            self.pulses[80] = state
        if self.lost_response:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return {"id": 80} if path == "/api/pulse" else copy.deepcopy(self.test_response)


@pytest.fixture
def runtime(configured, monkeypatch):
    monkeypatch.setattr("mcp_metabase.service.MUTATION_RECONCILIATION_DELAY_SECONDS", 0)
    service = MetabaseRuntime(configured)
    service.http.close()
    service.http = PulseApi()
    return service


def execute(runtime, plan):
    return runtime.action_execute(plan["plan_id"], plan["digest"])


def update(runtime, patch):
    return runtime.action_prepare(
        "dashboard_subscription_update", {"subscription_id": 70, "patch": patch}
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("kind", ["email", "slack", "http"])
def test_create_preserves_explicit_mode_and_confirms_readback(runtime, enabled, kind):
    body = create_body(enabled=enabled, kind=kind)
    if kind == "http":
        body["channels"][0]["details"] = {}
        body["channels"][0]["channel_id"] = 99
    plan = runtime.action_prepare("dashboard_subscription_create", {"body": body})
    assert runtime.http.sent == []
    result = execute(runtime, plan)
    assert result["outcome"] == "applied_verified"
    assert result["created_object_id"] == 80 and result["work_session"] is None
    assert runtime.http.pulses[80]["channels"][0]["enabled"] is enabled
    assert runtime.http.post_calls == 1
    assert (
        result["cleanup_candidate"]["recommended_action"]
        == "dashboard_subscription_update(archived=true)"
    )


def test_create_matches_generated_channel_ids_and_reordered_inventory(runtime):
    body = create_body()
    body["channels"].append(create_body(kind="slack")["channels"][0])
    result = execute(
        runtime, runtime.action_prepare("dashboard_subscription_create", {"body": body})
    )
    assert result["outcome"] == "applied_verified"


def test_patch_preserves_filters_cards_other_channels_and_private_details(runtime):
    before = project_state(runtime.http.pulses[70], ObjectType.DASHBOARD_SUBSCRIPTION)
    plan = update(
        runtime,
        {
            "name": "Renamed",
            "channels": [{"id": 71, "schedule_hour": 11, "details": {"attachment_only": True}}],
        },
    )
    serialized = json.dumps(plan)
    for secret in ("hidden-token", "hydrated-sql", "hydrated-secret", "preserve-hidden"):
        assert secret not in serialized
    result = execute(runtime, plan)
    assert result["outcome"] == "applied_verified" and result["rollback_candidates"] == []
    payload = runtime.http.sent[0][1]
    assert "id" not in payload and "dashboard_id" not in payload
    assert payload["skip_if_empty"] is True and payload["archived"] is False
    assert "cards" not in payload and "parameters" not in payload
    observed = project_state(runtime.http.pulses[70], ObjectType.DASHBOARD_SUBSCRIPTION)
    assert observed["cards"] == before["cards"] and observed["parameters"] == before["parameters"]
    assert payload["channels"][1] == before["channels"][1]
    assert payload["channels"][0]["details"]["private_future_setting"] == "preserve-hidden"
    assert payload["channels"][0]["details"]["attachment_only"] is True
    assert runtime.http.pulses[70]["future_root"] == "untouched"
    audit = "".join(path.read_text() for path in runtime.config.audit_dir.glob("*.jsonl"))
    assert "hidden-token" not in audit and "fixture@example.org" not in audit
    with pytest.raises(MetabasePolicyError, match="explicit dashboard_subscription_update"):
        runtime.rollback_prepare(plan["plan_id"])


def test_archive_and_restore_match_server_hook_without_channel_override(runtime):
    disabled = execute(runtime, update(runtime, {"archived": True}))
    assert disabled["outcome"] == "applied_verified"
    assert runtime.http.pulses[70]["archived"] is True
    assert all(row["enabled"] is False for row in runtime.http.pulses[70]["channels"])
    assert "channels" not in runtime.http.sent[0][1]
    # An ordinary edit must not restore the archived subscription via API defaults.
    assert execute(runtime, update(runtime, {"name": "Still off"}))["outcome"] == "applied_verified"
    assert runtime.http.pulses[70]["archived"] is True
    restored = execute(runtime, update(runtime, {"archived": False}))
    assert restored["outcome"] == "applied_verified"
    assert all(row["enabled"] is True for row in runtime.http.pulses[70]["channels"])


@pytest.mark.parametrize("response", ["normal", "lost", "ignored"])
def test_archive_hidden_cards_on_archived_dashboard_preserves_inventory(runtime, response):
    """Архивация скрытых карточек проходит execute/readback без подмены списков."""
    runtime.http.pulses[70]["cards"] = []
    runtime.http.dashboards[10]["archived"] = True
    before = copy.deepcopy(runtime.http.pulses[70])
    plan = update(runtime, {"archived": True})
    assert plan["impact"][0]["target"]["question_bindings"] == []
    runtime.http.lost_response = response == "lost"
    runtime.http.update_ignored = response == "ignored"
    result = execute(runtime, plan)
    assert result["outcome"] == (
        "not_applied_verified" if response == "ignored" else "applied_verified"
    )
    assert runtime.http.sent == [("/api/pulse/70", {"archived": True, "skip_if_empty": True})]
    assert runtime.http.put_calls == 1
    after = runtime.http.pulses[70]
    assert after["cards"] == [] and after["parameters"] == before["parameters"]
    assert after["future_root"] == before["future_root"]
    before_channels = {row["id"]: row for row in before["channels"]}
    for row in after["channels"]:
        expected = copy.deepcopy(before_channels[row["id"]])
        if response != "ignored":
            expected["enabled"] = False
        assert row == expected


def test_archive_hidden_cards_keeps_dashboard_drift_guard(runtime):
    """Изменение архивного дашборда после prepare останавливает запись."""
    runtime.http.pulses[70]["cards"] = []
    runtime.http.dashboards[10]["archived"] = True
    plan = update(runtime, {"archived": True})
    runtime.http.dashboards[10]["name"] = "Concurrent edit"
    assert execute(runtime, plan)["outcome"] == "rejected_stale"
    assert runtime.http.sent == []


@pytest.mark.parametrize("action", ["restore", "test"])
def test_hidden_cards_cannot_be_activated_or_sent(runtime, action):
    """Пустой видимый список допустим для архивации, но не включения или отправки."""
    runtime.http.pulses[70]["cards"] = []
    runtime.http.pulses[70]["archived"] = True
    for row in runtime.http.pulses[70]["channels"]:
        row["enabled"] = False
    with pytest.raises(MutationValidationError, match="at least one visible card"):
        if action == "restore":
            update(runtime, {"archived": False})
        else:
            runtime.action_prepare(
                "dashboard_subscription_test", {"subscription_id": 70, "channel_ids": [71]}
            )
    assert runtime.http.sent == []


@pytest.mark.parametrize(
    "patch",
    [
        {"archived": True, "cards": [{"id": 999, "dashboard_card_id": 201}]},
        {"archived": True, "parameters": [{"id": "unknown", "type": "string/="}]},
    ],
)
def test_archive_flag_does_not_bypass_replacement_content_validation(runtime, patch):
    """Совмещение архивации с заменой карточек или фильтров сохраняет строгую проверку."""
    with pytest.raises(MutationValidationError):
        update(runtime, patch)
    assert runtime.http.sent == []


def test_rename_does_not_round_trip_inventories_hidden_by_pulse_get(runtime):
    result = execute(runtime, update(runtime, {"name": "Renamed"}))
    assert result["outcome"] == "applied_verified"
    assert runtime.http.sent == [
        (
            "/api/pulse/70",
            {
                "name": "Renamed",
                "archived": False,
                "skip_if_empty": True,
            },
        )
    ]


def test_replacement_cards_apply_declared_defaults_and_keep_exact_dashcard_binding(runtime):
    plan = update(runtime, {"cards": [{"id": 1, "dashboard_card_id": 201}]})
    assert execute(runtime, plan)["outcome"] == "applied_verified"
    assert runtime.http.pulses[70]["cards"] == [{**card_ref(), "include_csv": False}]


@pytest.mark.parametrize("change", ["pulse", "dashboard", "question", "recipients", "permission"])
@pytest.mark.parametrize("action", ["dashboard_subscription_update", "dashboard_subscription_test"])
def test_stale_content_or_destination_blocks_before_any_write(runtime, change, action):
    arguments = (
        {"subscription_id": 70, "patch": {"name": "New"}}
        if action.endswith("update")
        else {"subscription_id": 70, "channel_ids": [71]}
    )
    plan = runtime.action_prepare(action, arguments)
    if change == "pulse":
        runtime.http.pulses[70]["name"] = "Concurrent edit"
    elif change == "dashboard":
        runtime.http.dashboards[10]["name"] = "Concurrent edit"
    elif change == "question":
        runtime.http.cards[1]["description"] = "Concurrent edit"
    elif change == "permission":
        runtime.http.pulses[70]["can_write"] = False
    else:
        runtime.http.pulses[70]["channels"][0]["recipients"][0]["email"] = "other@example.org"
    assert execute(runtime, plan)["outcome"] == "rejected_stale"
    assert runtime.http.sent == []


def test_create_binds_dashboard_and_question_before_write(runtime):
    plan = runtime.action_prepare("dashboard_subscription_create", {"body": create_body()})
    runtime.http.cards[1]["name"] = "Changed"
    assert execute(runtime, plan)["outcome"] == "rejected_stale"
    assert runtime.http.sent == []


def test_test_send_uses_only_selected_channels_and_does_not_save(runtime):
    before = copy.deepcopy(runtime.http.pulses[70])
    plan = runtime.action_prepare(
        "dashboard_subscription_test", {"subscription_id": 70, "channel_ids": [72]}
    )
    assert len(plan["impact"][0]["target"]["after"]["channels"]) == 1
    result = execute(runtime, plan)
    assert result["outcome"] == "delivery_request_accepted"
    assert result["recipient_delivery_verified"] is False and result["automatic_retry"] is False
    path, payload = runtime.http.sent[0]
    assert path == "/api/pulse/test" and payload["id"] == 70
    assert [row["id"] for row in payload["channels"]] == [72]
    assert payload["channels"][0]["enabled"] is True
    assert payload["parameters"] == before["parameters"] and payload["skip_if_empty"] is True
    assert runtime.http.pulses[70] == before and runtime.http.put_calls == 0
    with pytest.raises(MetabasePolicyError, match="consumed"):
        execute(runtime, plan)
    assert runtime.http.post_calls == 1
    with pytest.raises(MetabasePolicyError):
        runtime.rollback_prepare(plan["plan_id"])


@pytest.mark.parametrize("action", ["dashboard_subscription_create", "dashboard_subscription_test"])
def test_lost_response_does_not_repeat_create_or_delivery(runtime, action):
    arguments = (
        {"body": create_body()}
        if action.endswith("create")
        else {"subscription_id": 70, "channel_ids": [71]}
    )
    plan = runtime.action_prepare(action, arguments)
    runtime.http.lost_response = True
    result = execute(runtime, plan)
    assert result["outcome"] == "outcome_unknown" and result["automatic_retry"] is False
    with pytest.raises(MetabasePolicyError, match="consumed"):
        execute(runtime, plan)
    assert runtime.http.post_calls == 1


@pytest.mark.parametrize("response", [{"ok": False}, {}, {"ok": 1}])
def test_test_send_requires_explicit_acknowledgement(runtime, response):
    runtime.http.test_response = response
    plan = runtime.action_prepare(
        "dashboard_subscription_test", {"subscription_id": 70, "channel_ids": [71]}
    )
    assert execute(runtime, plan)["outcome"] == "outcome_unknown"
    assert runtime.http.post_calls == 1


def test_update_lost_response_reconciles_without_repeating_put(runtime):
    plan = update(runtime, {"name": "Updated"})
    runtime.http.lost_response = True
    assert execute(runtime, plan)["outcome"] == "applied_verified"
    assert runtime.http.put_calls == 1


def test_unchanged_readback_does_not_claim_update(runtime):
    plan = update(runtime, {"name": "Updated"})
    runtime.http.update_ignored = True
    assert execute(runtime, plan)["outcome"] == "not_applied_verified"
    assert runtime.http.put_calls == 1


@pytest.mark.parametrize(
    "action", ["dashboard_subscription_create", "dashboard_subscription_update"]
)
def test_unreadable_after_write_is_unknown_without_replaying(runtime, action):
    arguments = (
        {"body": create_body()}
        if action.endswith("create")
        else {"subscription_id": 70, "patch": {"name": "Updated"}}
    )
    plan = runtime.action_prepare(action, arguments)
    runtime.http.readback_failure = True
    assert execute(runtime, plan)["outcome"] == "outcome_unknown"
    assert len(runtime.http.sent) == 1


def test_create_mismatched_readback_reports_partial_and_cleanup(runtime):
    runtime.http.created_mismatch = True
    result = execute(
        runtime, runtime.action_prepare("dashboard_subscription_create", {"body": create_body()})
    )
    assert result["outcome"] == "partially_applied"
    assert result["cleanup_candidate"]["object_id"] == 80 and runtime.http.post_calls == 1


@pytest.mark.parametrize(
    "action",
    [
        "dashboard_subscription_create",
        "dashboard_subscription_update",
        "dashboard_subscription_test",
    ],
)
def test_provider_rejection_is_not_success(runtime, action):
    arguments = (
        {"body": create_body()}
        if action.endswith("create")
        else {"subscription_id": 70, "patch": {"name": "New"}}
        if action.endswith("update")
        else {"subscription_id": 70, "channel_ids": [71]}
    )
    plan = runtime.action_prepare(action, arguments)
    runtime.http.fail_status = 403
    assert execute(runtime, plan)["outcome"] == "rejected_validation"
    assert len(runtime.http.sent) == 1


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"name": "Weekly"},
        {"dashboard_id": 999},
        {"archived": None},
        {"channels": [{"id": 999, "schedule_hour": 10}]},
        {"channels": [{"id": 71, "schedule_hour": 25}]},
        {"channels": [{"id": 71, "schedule_hour": 10}, {"id": 71, "schedule_hour": 11}]},
        {"archived": True, "channels": [{"id": 71, "enabled": True}]},
        {"channels": [{"id": 72, "details": {"channel": "#new"}}]},
        {"cards": []},
        {"parameters": [{"id": "unknown", "type": "string/="}]},
    ],
)
def test_invalid_patches_fail_during_prepare(runtime, patch):
    with pytest.raises(MutationValidationError):
        update(runtime, patch)
    assert runtime.http.sent == []


@pytest.mark.parametrize("ids", [[], [71, 71], [True], [0], [999]])
def test_invalid_test_selection_fails_during_prepare(runtime, ids):
    with pytest.raises(MutationValidationError):
        runtime.action_prepare(
            "dashboard_subscription_test", {"subscription_id": 70, "channel_ids": ids}
        )
    assert runtime.http.sent == []


@pytest.mark.parametrize("missing", ["skip_if_empty", "parameters", "recipients", "format_rows"])
def test_incomplete_snapshot_cannot_be_used_for_update(runtime, missing):
    state = runtime.http.pulses[70]
    if missing == "recipients":
        state["channels"][0].pop(missing)
    elif missing == "format_rows":
        state["cards"][0].pop(missing)
    else:
        state.pop(missing)
    with pytest.raises(MutationValidationError, match="incomplete|explicit|complete"):
        update(runtime, {"name": "New"})
    assert runtime.http.sent == []


@pytest.mark.parametrize("change", ["identity", "version", "role"])
def test_execution_rechecks_actor_version_and_visibility(runtime, change):
    plan = runtime.action_prepare(
        "dashboard_subscription_test", {"subscription_id": 70, "channel_ids": [71]}
    )
    if change == "identity":
        runtime.http.user_id = 8
    elif change == "version":
        runtime.http.version = "v0.63.15"
    else:
        runtime.http.admin = False
    assert execute(runtime, plan)["outcome"] in {"rejected_identity", "rejected_validation"}
    assert runtime.http.sent == []


def test_non_admin_cannot_plan_from_potentially_filtered_recipients(runtime):
    runtime.http.admin = False
    with pytest.raises(MutationValidationError, match="complete API recipient visibility"):
        update(runtime, {"name": "New"})


def test_non_admin_create_uses_explicit_recipients_without_reading_existing_pulses(runtime):
    runtime.http.admin = False
    plan = runtime.action_prepare("dashboard_subscription_create", {"body": create_body()})
    assert execute(runtime, plan)["outcome"] == "applied_verified"


def test_card_must_match_exact_dashboard_position(runtime):
    body = create_body()
    body["cards"][0]["dashboard_card_id"] = 999
    with pytest.raises(MutationValidationError, match="exact dashboard dashcard"):
        runtime.action_prepare("dashboard_subscription_create", {"body": body})


def test_compact_transport_advertises_three_actions_without_new_tools(runtime, monkeypatch):
    monkeypatch.setattr(server, "_RUNTIME", runtime)

    async def scenario():
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 15
            prepare = next(tool for tool in tools if tool.name == "metabase_action_prepare")
            actions = prepare.inputSchema["properties"]["action"]["enum"]
            assert {
                "dashboard_subscription_create",
                "dashboard_subscription_update",
                "dashboard_subscription_test",
            } <= set(actions)
            prepared = await client.call_tool(
                "metabase_action_prepare",
                {
                    "action": "dashboard_subscription_test",
                    "arguments": {"subscription_id": 70, "channel_ids": [71]},
                },
            )
            plan = prepared.data
            result = await client.call_tool(
                "metabase_action_execute", {"plan_id": plan["plan_id"], "digest": plan["digest"]}
            )
            assert result.data["outcome"] == "delivery_request_accepted"

    asyncio.run(scenario())
