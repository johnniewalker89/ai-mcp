from __future__ import annotations

import asyncio
import copy
import json

import pytest
from fastmcp import Client
from test_service_and_surface import StatefulApi

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.models import Action
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.notifications import NotificationShapeError, notification_state
from mcp_metabase.service import MetabaseRuntime


def notification():
    return {
        "id": 70,
        "active": False,
        "payload_type": "notification/card",
        "creator_id": 7,
        "payload_id": 71,
        "payload": {
            "id": 71,
            "card_id": 1,
            "send_once": False,
            "send_condition": "has_result",
            "card": {"secret": "hydrated-card"},
        },
        "subscriptions": [
            {
                "id": 72,
                "notification_id": 70,
                "type": "notification-subscription/cron",
                "cron_schedule": "0 0 9 ? * 2 *",
                "ui_display_type": "cron/raw",
            },
            {
                "id": 73,
                "notification_id": 70,
                "type": "notification-subscription/cron",
                "cron_schedule": "0 0 10 ? * 3 *",
                "ui_display_type": "cron/builder",
            },
        ],
        "handlers": [
            {
                "id": 74,
                "notification_id": 70,
                "channel_type": "channel/slack",
                "channel_id": None,
                "template_id": None,
                "active": True,
                "channel": {"token": "hydrated-secret"},
                "unknown": {"preserve": True},
                "recipients": [
                    {
                        "id": 75,
                        "notification_handler_id": 74,
                        "type": "notification-recipient/raw-value",
                        "details": {"value": "#first"},
                    },
                    {
                        "id": 76,
                        "notification_handler_id": 74,
                        "type": "notification-recipient/raw-value",
                        "details": {"value": "#second"},
                    },
                ],
            }
        ],
        "updated_at": "before",
        "future_property": {"preserve": "root"},
    }


class NotificationApi(StatefulApi):
    def __init__(self):
        super().__init__()
        self.notifications = {70: notification()}
        self.sent = []
        self.lost_response = False
        self.cards[2] = {**copy.deepcopy(self.cards[1]), "id": 2, "name": "Second"}

    def get_json(self, path, *, params=None):
        if path == "/api/notification":
            return [
                copy.deepcopy(n)
                for n in self.notifications.values()
                if n["payload"]["card_id"] == params["card_id"]
                and (params["include_inactive"] == "true" or n["active"])
            ]
        if path.startswith("/api/notification/"):
            return copy.deepcopy(self.notifications[int(path.rsplit("/", 1)[1])])
        return super().get_json(path, params=params)

    def put_json(self, path, body):
        if not path.startswith("/api/notification/"):
            return super().put_json(path, body)
        self.put_calls += 1
        self.sent.append(copy.deepcopy(body))
        assert body["id"] == 70 and body["payload"]["id"] == 71
        assert {r["id"] for r in body["subscriptions"]} == {72, 73}
        assert {r["id"] for r in body["handlers"][0]["recipients"]} == {75, 76}
        # Spec-update заменяет коллекции по переданным IDs; неполный body теряет дочерние строки.
        self.notifications[70] = copy.deepcopy(body)
        self.notifications[70]["updated_at"] = "server-new"
        self.notifications[70]["subscriptions"].reverse()
        if self.lost_response:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        return copy.deepcopy(self.notifications[70])

    def post_json(self, path, body):
        if path != "/api/notification":
            return super().post_json(path, body)
        self.post_calls += 1
        created = copy.deepcopy(body)
        created.update({"id": 80, "creator_id": 7, "payload_id": 81})
        created["payload"]["id"] = 81
        created["subscriptions"][0].update({"id": 82, "notification_id": 80})
        created["handlers"][0].update({"id": 83, "notification_id": 80})
        created["handlers"][0]["recipients"][0]["id"] = 84
        self.notifications[80] = created
        return copy.deepcopy(created)


@pytest.fixture
def runtime(configured):
    service = MetabaseRuntime(configured)
    service.http.close()
    fake = NotificationApi()
    service.http = fake
    return service, fake


def execute(service, plan):
    return service.action_execute(plan["plan_id"], plan["digest"], open_session=False)


def test_notification_update_preserves_nested_ids_unknowns_and_rollback(runtime):
    service, fake = runtime
    before = notification_state(fake.notifications[70])
    plan = service.action_prepare(
        "notification_update",
        {
            "notification_id": 70,
            "patch": {
                "send_once": True,
                "schedules": [{"subscription_id": 72, "cron_schedule": "0 0 11 ? * 4 *"}],
                "recipients": [{"handler_id": 74, "recipient_id": 75, "value": "#changed"}],
            },
        },
    )
    assert fake.put_calls == 0
    assert "hydrated-secret" not in json.dumps(plan)
    result = execute(service, plan)
    assert result["outcome"] == "applied_verified"
    sent = fake.sent[0]
    assert sent["future_property"] == before["future_property"]
    assert sent["handlers"][0]["unknown"] == {"preserve": True}
    assert sent["subscriptions"][1] == before["subscriptions"][1]
    assert "channel" not in sent["handlers"][0] and "card" not in sent["payload"]
    rollback = service.rollback_prepare(plan["plan_id"])
    rolled = service.exact_action_execute(
        rollback["plan_id"], rollback["digest"], expected_actions={Action.NOTIFICATION_ROLLBACK}
    )
    assert rolled["outcome"] == "applied_verified"
    assert notification_state(fake.notifications[70]) == before


def test_notification_stale_rejects_unrelated_nested_drift(runtime):
    service, fake = runtime
    plan = service.notification_update_prepare(70, {"send_once": True})
    fake.notifications[70]["handlers"][0]["recipients"][1]["details"]["value"] = "#elsewhere"
    assert execute(service, plan)["outcome"] == "rejected_stale"
    assert fake.put_calls == 0


def test_notification_lost_response_reconciles_without_second_write(runtime):
    service, fake = runtime
    plan = service.notification_update_prepare(70, {"send_once": True})
    fake.lost_response = True
    assert execute(service, plan)["outcome"] == "applied_verified"
    assert fake.put_calls == 1


@pytest.mark.parametrize(
    "patch",
    [
        {},
        {"send_once": "false"},
        {"send_once": None},
        {"timezone": "Europe/Moscow"},
        {"schedules": [{"subscription_id": 72, "cron_schedule": "invalid"}]},
        {
            "schedules": [
                {
                    "subscription_id": 72,
                    "cron_schedule": "0 0 9 ? * 2 *",
                    "ui_display_type": "weekly",
                }
            ]
        },
        {"schedules": [{"subscription_id": 999, "cron_schedule": "0 0 9 ? * 2 *"}]},
        {"recipients": [{"handler_id": 74, "recipient_id": 999, "value": "#x"}]},
    ],
)
def test_invalid_notification_patch_never_writes(runtime, patch):
    service, fake = runtime
    with pytest.raises((MutationValidationError, NotificationShapeError)):
        service.action_prepare("notification_update", {"notification_id": 70, "patch": patch})
    assert fake.put_calls == 0


@pytest.mark.parametrize("damage", ["missing_recipients", "duplicate_subscription", "resource"])
def test_incomplete_or_unsupported_notification_rejects_prepare(runtime, damage):
    service, fake = runtime
    raw = fake.notifications[70]
    if damage == "missing_recipients":
        del raw["handlers"][0]["recipients"]
    elif damage == "duplicate_subscription":
        raw["subscriptions"].append(copy.deepcopy(raw["subscriptions"][0]))
    else:
        raw["handlers"][0]["template"] = {"details": {"type": "email/handlebars-resource"}}
    with pytest.raises(NotificationShapeError):
        service.notification_update_prepare(70, {"send_once": True})
    assert fake.put_calls == 0


def test_notification_read_and_local_paging(runtime):
    service, fake = runtime
    second = copy.deepcopy(fake.notifications[70])
    second["id"] = 90
    fake.notifications[90] = second
    page = service.notification_list(1, include_inactive=True, limit=1)
    assert page["total"] == 2 and page["next_offset"] == 1
    assert page["upstream_pagination"] is False
    assert "hydrated-secret" not in json.dumps(page)
    assert service.notification_list(1)["total"] == 0
    assert service.notification_get(70)["notification"]["active"] is False


def test_notification_create_inactive_binds_question_and_reads_back(runtime):
    service, fake = runtime
    body = {"question_id": 1, "cron_schedule": "0 0 9 ? * 2 *", "slack_recipient": "#test"}
    plan = service.action_prepare("notification_create", {"body": body})
    assert fake.post_calls == 0
    result = execute(service, plan)
    assert result["outcome"] == "applied_verified"
    assert fake.notifications[80]["active"] is False
    assert fake.post_calls == 1
    stale = service.action_prepare("notification_create", {"body": body})
    fake.cards[1]["name"] = "Changed"
    assert execute(service, stale)["outcome"] == "rejected_stale"
    assert fake.post_calls == 1


def test_batch_trash_restore_and_prefix_rollback(runtime):
    service, fake = runtime
    plan = service.action_prepare("question_batch_trash", {"question_ids": [1, 2]})
    assert execute(service, plan)["outcome"] == "applied_verified"
    assert all(c["archived"] for c in fake.cards.values())
    restore = service.action_prepare(
        "question_batch_restore",
        {
            "question_ids": [1, 2],
            "collection_id": 30,
        },
    )
    fake.fail_put_call = 4
    result = execute(service, restore)
    assert result["outcome"] == "partially_applied"
    assert result["applied_indexes"] == [0]
    assert fake.cards[1]["collection_id"] == 30
    rollback = service.rollback_prepare(restore["plan_id"])
    result = service.exact_action_execute(
        rollback["plan_id"], rollback["digest"], expected_actions={Action.QUESTION_ROLLBACK}
    )
    assert result["outcome"] == "applied_verified"
    assert fake.cards[1]["archived"] is True
    assert fake.cards[1]["collection_id"] == 20


@pytest.mark.parametrize("ids", [[], [1, 1], [True], list(range(1, 12))])
def test_batch_inventory_rejected_before_writes(runtime, ids):
    service, fake = runtime
    with pytest.raises(MutationValidationError):
        service.action_prepare("question_batch_trash", {"question_ids": ids})
    assert fake.put_calls == 0


def test_batch_all_inventory_stale_preflight_no_partial_write(runtime):
    service, fake = runtime
    plan = service.action_prepare("question_batch_trash", {"question_ids": [1, 2]})
    fake.cards[2]["name"] = "Changed"
    assert execute(service, plan)["outcome"] == "rejected_stale"
    assert fake.put_calls == 0


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("provider_returns_empty_arrays", [False, True])
def test_legacy_null_arrays_lifecycle_restore_rollback_preserve_payload(
    runtime, batch, provider_returns_empty_arrays
):
    service, fake = runtime
    for card in fake.cards.values():
        card.update(parameters=None, parameter_mappings=None)
    original = copy.deepcopy(fake.cards)
    sent = []
    original_put = fake.put_json

    def put(path, body):
        sent.append(copy.deepcopy(body))
        result = original_put(path, body)
        if provider_returns_empty_arrays:
            fake.cards[int(path.rsplit("/", 1)[1])].update(parameters=[], parameter_mappings=[])
        return result

    fake.put_json = put
    ids = [1, 2] if batch else [1]
    args = {"question_ids": ids} if batch else {"question_id": 1}
    prefix = "question_batch" if batch else "question"
    plan = service.action_prepare(prefix + "_trash", args)
    assert fake.cards == original and fake.put_calls == 0
    assert execute(service, plan)["outcome"] == "applied_verified"
    restore = service.action_prepare(prefix + "_restore", {**args, "collection_id": 30})
    assert execute(service, restore)["outcome"] == "applied_verified"
    rollback = service.rollback_prepare(restore["plan_id"])
    result = service.exact_action_execute(
        rollback["plan_id"],
        rollback["digest"],
        expected_actions={Action.BATCH_ROLLBACK if batch else Action.QUESTION_ROLLBACK},
    )
    assert result["outcome"] == "applied_verified"
    assert sent == (
        [{"archived": True}] * len(ids)
        + [{"archived": False, "collection_id": 30}] * len(ids)
        + [{"archived": True, "collection_id": 20}] * len(ids)
    )
    for question_id in ids:
        card = fake.cards[question_id]
        assert card["archived"] is True and card["collection_id"] == 20
        expected = [] if provider_returns_empty_arrays else None
        assert card["parameters"] == card["parameter_mappings"] == expected


@pytest.mark.parametrize("root", ["parameters", "parameter_mappings"])
def test_legacy_null_arrays_batch_rejects_real_parameter_drift(runtime, root):
    service, fake = runtime
    for card in fake.cards.values():
        card.update(parameters=None, parameter_mappings=None)
    plan = service.action_prepare("question_batch_trash", {"question_ids": [1, 2]})
    fake.cards[2][root] = [{"id": "new-parameter"}]
    assert execute(service, plan)["outcome"] == "rejected_stale"
    assert fake.put_calls == 0
    assert not any(card["archived"] for card in fake.cards.values())


def test_new_protocol_tools_and_actions(runtime, monkeypatch):
    service, _ = runtime
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def run():
        async with Client(server.mcp) as client:
            listed = await client.call_tool(
                "metabase_notification_list",
                {
                    "question_id": 1,
                    "include_inactive": True,
                },
            )
            assert listed.data["total"] == 1
            read = await client.call_tool(
                "metabase_object_get",
                {
                    "object_type": "notification",
                    "object_id": 70,
                },
            )
            assert read.data["notification"]["card_id"] == 1
            prepared = await client.call_tool(
                "metabase_action_prepare",
                {
                    "action": "notification_update",
                    "arguments": {"notification_id": 70, "patch": {"send_once": True}},
                },
            )
            executed = await client.call_tool(
                "metabase_action_execute",
                {
                    "plan_id": prepared.data["plan_id"],
                    "digest": prepared.data["digest"],
                },
            )
            assert executed.data["outcome"] == "applied_verified"

    asyncio.run(run())


def test_notification_rejects_missing_template_and_unbound_recipient_channel(runtime):
    service, fake = runtime
    handler = fake.notifications[70]["handlers"][0]
    handler["template_id"] = 99
    with pytest.raises(NotificationShapeError, match="template binding"):
        service.notification_update_prepare(70, {"send_once": True})
    handler["template_id"] = None
    handler["recipients"][0]["details"]["channel_id"] = "C123"
    with pytest.raises(NotificationShapeError, match="channel_id"):
        service.notification_update_prepare(
            70,
            {
                "recipients": [
                    {"handler_id": 74, "recipient_id": 75, "value": "#changed"},
                ]
            },
        )
    assert fake.put_calls == 0


@pytest.mark.parametrize("bad_list", [None, [{"id": 70, "payload": None}]])
def test_notification_invalid_list_is_not_empty_success(runtime, bad_list):
    service, fake = runtime
    fake.get_json = lambda *args, **kwargs: bad_list
    with pytest.raises(MutationValidationError):
        service.notification_list(1, include_inactive=True)


def test_batch_unknown_outcome_recovers_in_same_plan(runtime):
    service, fake = runtime
    fake.fail_put_call = 2
    fake.fail_put_unknown = True
    original_get = fake.get_json
    unavailable_reads = 3

    def temporary_unavailable(path, *, params=None):
        nonlocal unavailable_reads
        if fake.put_calls == 2 and path == "/api/card/2" and unavailable_reads:
            unavailable_reads -= 1
            raise MetabaseApiError("read unavailable", status_code=503)
        return original_get(path, params=params)

    fake.get_json = temporary_unavailable
    plan = service.action_prepare("question_batch_trash", {"question_ids": [1, 2]})
    result = execute(service, plan)
    assert result["outcome"] == "applied_verified"
    assert fake.put_calls == 3
    assert result["recovery_write_indexes"] == [1]
    assert all(card["archived"] for card in fake.cards.values())


def test_notification_creation_cannot_activate_or_set_raw_api_fields(runtime):
    service, fake = runtime
    body = {
        "question_id": 1,
        "cron_schedule": "0 0 9 ? * 2 *",
        "slack_recipient": "#test",
        "active": True,
    }
    with pytest.raises(MutationValidationError):
        service.action_prepare("notification_create", {"body": body})
    assert fake.post_calls == 0
