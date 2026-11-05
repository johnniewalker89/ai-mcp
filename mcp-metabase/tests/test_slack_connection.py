from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
from fastmcp import Client

import mcp_metabase.mcp_server as server
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError
from mcp_metabase.service import MetabaseRuntime
from mcp_metabase.slack_connection import SLACK_VALID_KEY


class SlackApi:
    """Имитирует штатный settings/app-info API, включая его auth-error reset."""

    def __init__(self) -> None:
        """Хранит индикатор, учётную запись и наблюдаемые запросы для одной проверки."""
        self.indicator = None
        self.admin = True
        self.version = "v0.63.15.7"
        self.requests = []
        self.app_checks = 0
        self.puts = 0
        self.put_mode = "normal"
        self.app_http_status = 200
        self.invalidate_on_check = False
        self.drift_on_check = False
        self.app_info = {
            "app_id": "ABIADAPTER",
            "team_id": "TBIADAPTER",
            "token": "must-not-escape",
            "other": {"private": "must-not-escape"},
        }

    @staticmethod
    def response(value: Any, status: int = 200) -> httpx.Response:
        """Кодирует JSON null как реальный ответ API, а не пустой HTTP body."""
        return httpx.Response(status, content=json.dumps(value).encode("utf-8"))

    def __call__(self, request: httpx.Request) -> httpx.Response:
        """Обрабатывает только разрешённые endpoint и считает одноразовые проверки/записи."""
        path = request.url.raw_path.decode()
        self.requests.append((request.method, path))
        assert not request.url.query
        if path == "/api/session/properties":
            return self.response({"version": {"tag": self.version}})
        if path == "/api/user/current":
            return self.response(
                {
                    "id": 7,
                    "common_name": "Test",
                    "email": "fixture@example.org",
                    "is_active": True,
                    "is_superuser": self.admin,
                }
            )
        if path == "/api/setting/slack-token-valid%3F":
            if request.method == "GET":
                if self.puts and self.put_mode == "unreadable":
                    return self.response({}, 503)
                return self.response(self.indicator)
            assert request.method == "PUT"
            assert json.loads(request.content) == {"value": True}
            self.puts += 1
            if self.put_mode == "denied":
                return self.response({}, 403)
            if self.put_mode != "ignored":
                self.indicator = True
            if self.put_mode == "lost":
                raise httpx.ReadError("lost response", request=request)
            return httpx.Response(204)
        assert path == "/api/slack/app-info" and request.method == "GET"
        self.app_checks += 1
        if self.invalidate_on_check:
            self.indicator = False
            return self.response({"app_id": None, "team_id": None, "scopes": None})
        if self.drift_on_check:
            self.indicator = True
        return self.response(self.app_info, self.app_http_status)


@pytest.fixture
def runtime(configured):
    """Соединяет настоящий runtime/HTTP client с изолированным provider transport."""
    api = SlackApi()
    service = MetabaseRuntime(configured, transport=httpx.MockTransport(api))
    yield service, api
    service.close()


def prepare(service):
    """Готовит закрытое действие без arguments и без живой проверки Slack."""
    return service.action_prepare("slack_status_refresh", {})


@pytest.mark.parametrize("before", [None, False, True])
def test_prepare_is_pure_and_execute_updates_only_verified_indicator(runtime, before):
    """Различает null/false/true, связывает plan и проверяет единственный PUT/readback."""
    service, api = runtime
    api.indicator = before
    plan = prepare(service)
    assert api.indicator is before and api.app_checks == api.puts == 0
    target = plan["impact"][0]["target"]
    assert target["before"] == {SLACK_VALID_KEY: before}
    assert target["validation_phase"] == "execute before the single indicator PUT"
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "applied_verified"
    assert result["indicator_readback"] == {SLACK_VALID_KEY: True}
    assert result["validated_connection"] == {"app_id": "ABIADAPTER", "team_id": "TBIADAPTER"}
    assert result["work_session"] is None
    assert api.app_checks == api.puts == 1 and api.indicator is True
    assert "must-not-escape" not in json.dumps(result)
    with pytest.raises(MetabasePolicyError, match="no automatic rollback"):
        service.rollback_prepare(plan["plan_id"])
    with pytest.raises(MetabasePolicyError, match="consumed"):
        service.action_execute(plan["plan_id"], plan["digest"])
    assert api.app_checks == api.puts == 1


@pytest.mark.parametrize(
    "app_info",
    [
        None,
        {"app_id": None, "team_id": None, "scopes": None},
        {"app_id": "", "team_id": "TBI"},
        {"app_id": "ABI", "team_id": True},
    ],
)
def test_http200_without_app_and_team_does_not_validate_connection(runtime, app_info):
    """HTTP200 с null/неполными идентификаторами не разрешает запись true."""
    service, api = runtime
    api.app_info = app_info
    plan = prepare(service)
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "rejected_validation"
    assert result["reason"] == "slack_authentication_not_confirmed"
    assert api.app_checks == 1 and api.puts == 0


def test_native_auth_error_keeps_false_without_repairing_it(runtime):
    """Штатный auth-error reset сохраняется и не маскируется последующей записью true."""
    service, api = runtime
    api.indicator = True
    plan = prepare(service)
    api.invalidate_on_check = True
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "rejected_validation"
    assert result["native_auth_handler_changed_indicator"] is True
    assert result["indicator_readback"] == {SLACK_VALID_KEY: False}
    assert api.indicator is False and api.puts == 0


@pytest.mark.parametrize("value", [0, "false", {}])
def test_invalid_indicator_response_stops_prepare(runtime, value):
    """Не трактует произвольные JSON-значения как false или отсутствие настройки."""
    service, api = runtime
    api.indicator = value
    with pytest.raises(MutationValidationError, match="boolean or null"):
        prepare(service)
    assert api.app_checks == api.puts == 0


def test_non_admin_is_rejected_before_reading_settings(runtime):
    """Проверяет право superuser до запроса индикатора."""
    service, api = runtime
    api.admin = False
    with pytest.raises(MetabasePolicyError, match="superuser"):
        prepare(service)
    assert not any("setting/" in path for _, path in api.requests)


def test_execution_rechecks_superuser(runtime):
    """Потеря права после prepare блокирует auth-проверку и запись."""
    service, api = runtime
    plan = prepare(service)
    api.admin = False
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] in {"rejected_validation", "rejected_identity"}
    assert api.app_checks == api.puts == 0


@pytest.mark.parametrize("during_check", [False, True])
def test_indicator_drift_blocks_put(runtime, during_check):
    """Сверяет индикатор перед проверкой и после неё, до единственного PUT."""
    service, api = runtime
    plan = prepare(service)
    if during_check:
        api.drift_on_check = True
    else:
        api.indicator = False
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "rejected_stale" and api.puts == 0


def test_effectful_app_info_is_not_retried(runtime):
    """Побочный GET выполняется один раз; его неизвестный эффект не повторяется."""
    service, api = runtime
    plan = prepare(service)
    api.app_http_status = 500
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "outcome_unknown"
    assert result["automatic_retry"] is False
    assert api.app_checks == 1 and api.puts == 0


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("lost", "applied_verified"),
        ("ignored", "not_applied_verified"),
        ("unreadable", "outcome_unknown"),
        ("denied", "rejected_validation"),
    ],
)
def test_update_reconciliation_never_replays_put(runtime, mode, outcome):
    """Различает потерянный ответ, неприменение, отказ и недоступный readback."""
    service, api = runtime
    plan = prepare(service)
    api.put_mode = mode
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == outcome
    assert api.app_checks == api.puts == 1


def test_expiry_and_binding_reject_before_validation(runtime):
    """Срок действия остаётся общим для нового action и не допускает сеть после expiry."""
    service, api = runtime
    plan = prepare(service)
    service.plans._clock = lambda: plan["expires_at_epoch"] + 1
    with pytest.raises(MetabasePolicyError, match="expired"):
        service.action_execute(plan["plan_id"], plan["digest"])
    assert api.app_checks == api.puts == 0


def test_compact_transport_advertises_closed_action_and_prepare_stays_pure(runtime, monkeypatch):
    """Сохраняет15tools, раскрывает action и отвергает произвольные settings/token inputs."""
    service, api = runtime
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def scenario():
        """Читает реальную схему MCP и вызывает только preparation."""
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 15
            tool = next(item for item in tools if item.name == "metabase_action_prepare")
            assert "slack_status_refresh" in tool.inputSchema["properties"]["action"]["enum"]
            result = await client.call_tool(
                "metabase_action_prepare",
                {
                    "action": "slack_status_refresh",
                    "arguments": {},
                },
            )
            assert result.data["prepared"] is True

    asyncio.run(scenario())
    with pytest.raises(MutationValidationError, match="arguments"):
        service.action_prepare("slack_status_refresh", {"token": "must-not-escape"})
    assert api.app_checks == api.puts == 0
