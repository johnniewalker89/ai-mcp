from __future__ import annotations

import asyncio
import copy

import pytest
from fastmcp import Client
from test_service_and_surface import runtime as runtime_fixture

import mcp_metabase.mcp_server as server
from mcp_metabase.http_client import MetabaseApiError
from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.plans import MetabasePolicyError

runtime = runtime_fixture


@pytest.fixture
def embedded_question(runtime, monkeypatch):
    """Добавляет две карточки дашборда и имитирует гидратацию их плиток."""
    service, api = runtime
    dashboard = api.dashboards[10]
    api.cards[1].update(dashboard_id=10, entity_id="question-1")
    api.cards[2] = {**copy.deepcopy(api.cards[1]), "id": 2, "entity_id": "question-2"}
    for card_id in (1, 2):
        dashboard["dashcards"].append(
            {
                "id": 100 + card_id,
                "card_id": card_id,
                "dashboard_tab_id": 101,
                "row": card_id * 4,
                "col": 0,
                "size_x": 6,
                "size_y": 4,
                "parameter_mappings": [],
                "series": [],
            }
        )
    original_get = api.get_json

    def get_json(path, *, params=None):
        """Использует актуальные тела карточек при ответе о дашборде."""
        raw = original_get(path, params=params)
        if path == "/api/dashboard/10":
            for tile in raw["dashcards"]:
                tile["card"] = copy.deepcopy(api.cards[tile["card_id"]])
        return raw

    monkeypatch.setattr(api, "get_json", get_json)
    return service, api


@pytest.mark.parametrize(("collection_id", "lost_response"), [(20, False), (None, True)])
def test_detach_preserves_question_when_later_archiving_dashboard(
    embedded_question, monkeypatch, collection_id, lost_response
):
    """Исполняет два отдельных плана и проверяет каскад по владельцу карточек."""
    service, api = embedded_question
    for card in api.cards.values():
        card["collection_id"] = collection_id
    api.dashboards[10]["collection_id"] = collection_id
    original_question = copy.deepcopy(api.cards[1])
    original_layout = service.dashboard_get_layout(10)["layout"]
    original_put = api.put_json
    writes = []

    def put_json(path, body):
        """Имитирует каскад Metabase и потерю ответа после принятого переноса."""
        writes.append((path, copy.deepcopy(body)))
        answer = original_put(path, body)
        if path == "/api/card/1" and lost_response:
            raise MetabaseApiError("lost response", outcome_unknown=True)
        if path == "/api/dashboard/10" and body.get("archived") is True:
            for card in api.cards.values():
                if card.get("dashboard_id") == 10:
                    card["archived"] = True
        return answer

    monkeypatch.setattr(api, "put_json", put_json)
    monkeypatch.setattr(server, "_RUNTIME", service)

    async def run_actions():
        """Проверяет реальную схему MCP и исполняет отдельные согласованные планы."""
        async with Client(server.mcp) as client:
            tools = await client.list_tools()
            assert len(tools) == 15
            schema = next(
                tool.inputSchema for tool in tools if tool.name == "metabase_action_prepare"
            )
            assert "question_detach" in schema["properties"]["action"]["enum"]
            prepared = (
                await client.call_tool(
                    "metabase_action_prepare",
                    {
                        "action": "question_detach",
                        "arguments": {"question_id": 1, "dashboard_id": 10},
                    },
                )
            ).data
            assert writes == []
            assert prepared["impact"][0]["target"]["source_dashboard"]["collection_id"] == (
                collection_id
            )
            moved = (
                await client.call_tool(
                    "metabase_action_execute",
                    {"plan_id": prepared["plan_id"], "digest": prepared["digest"]},
                )
            ).data
            assert moved["outcome"] == "applied_verified"
            assert moved["source_dashboard_verified"] is True
            assert moved["work_session"] is None and moved["rollback_candidates"] == []
            assert writes == [
                ("/api/card/1", {"dashboard_id": None, "collection_id": collection_id})
            ]
            assert not api.dashboards[10]["archived"]
            assert service.dashboard_get_layout(10)["layout"] == original_layout
            assert api.cards[1] == {**original_question, "dashboard_id": None, "updated_at": "u1"}
            with pytest.raises(MetabasePolicyError, match="consumed"):
                service.action_execute(prepared["plan_id"], prepared["digest"])
            with pytest.raises(MetabasePolicyError, match="separate supported move"):
                service.rollback_prepare(prepared["plan_id"])
            archive = service.action_prepare("dashboard_trash", {"dashboard_id": 10})
            assert service.action_execute(archive["plan_id"], archive["digest"])["outcome"] == (
                "applied_verified"
            )

    asyncio.run(run_actions())
    assert len(writes) == 2 and writes[1] == ("/api/dashboard/10", {"archived": True})
    assert api.cards[1]["archived"] is False
    assert api.cards[2]["archived"] is True


@pytest.mark.parametrize(
    ("location", "changes"),
    [
        ("question", {"dashboard_id": None}),
        ("question", {"archived": True}),
        ("question", {"collection_id": 30}),
        ("dashboard", {"archived": True}),
    ],
)
def test_detach_rejects_unbound_or_archived_objects(embedded_question, location, changes):
    """Отклоняет карточку вне дашборда и перенос в другую коллекцию."""
    service, api = embedded_question
    target = api.cards[1] if location == "question" else api.dashboards[10]
    target.update(changes)
    with pytest.raises(MutationValidationError):
        service.action_prepare("question_detach", {"question_id": 1, "dashboard_id": 10})
    assert api.put_calls == 0


def test_generic_update_does_not_move_question(embedded_question):
    """Проверяет отдельный контракт изменения владельца карточки."""
    service, api = embedded_question
    with pytest.raises(MutationValidationError):
        service.action_prepare(
            "question_update",
            {
                "question_id": 1,
                "operations": [{"op": "set", "path": "/dashboard_id", "value": None}],
            },
        )
    assert api.put_calls == 0


@pytest.mark.parametrize("changed", ["owner", "layout"])
def test_detach_rejects_stale_question_or_dashboard(embedded_question, changed):
    """Перепроверяет принадлежность и плитки после подготовки плана."""
    service, api = embedded_question
    plan = service.action_prepare("question_detach", {"question_id": 1, "dashboard_id": 10})
    if changed == "owner":
        api.cards[1]["dashboard_id"] = 11
    else:
        api.dashboards[10]["dashcards"][0]["row"] = 9
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "rejected_stale" and api.put_calls == 0


def test_detach_reports_verified_card_even_if_layout_changed(embedded_question, monkeypatch):
    """Сообщает о завершённом переносе и об отличии плиток без повторной записи."""
    service, api = embedded_question
    original_put = api.put_json

    def put_json(path, body):
        """Имитирует изменение раскладки после записи карточки."""
        answer = original_put(path, body)
        api.dashboards[10]["dashcards"].pop()
        return answer

    monkeypatch.setattr(api, "put_json", put_json)
    plan = service.action_prepare("question_detach", {"question_id": 1, "dashboard_id": 10})
    result = service.action_execute(plan["plan_id"], plan["digest"])
    assert result["outcome"] == "partially_applied"
    assert result["applied_indexes"] == [0]
    assert result["source_dashboard_verified"] is False
    assert result["reason"] == "source_dashboard_changed_after_write"
    assert api.cards[1]["dashboard_id"] is None and api.put_calls == 1
