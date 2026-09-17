from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from test_notifications_and_lifecycle import execute
from test_notifications_and_lifecycle import runtime as lifecycle_fixture

from mcp_metabase.normalization import dataset_query_semantically_matches

runtime = lifecycle_fixture


def complex_query():
    return {
        "lib/type": "mbql/query",
        "database": 50,
        "stages": [
            {
                "lib/type": "mbql.stage/mbql",
                "source-table": 100,
                "aggregation": [
                    ["sum", {"lib/uuid": "agg-a"}, ["field", {"lib/uuid": "field-a"}, 101]],
                    ["count", {"lib/uuid": "agg-b"}],
                ],
                "filters": [
                    [
                        "=",
                        {"lib/uuid": "filter"},
                        ["field", {"lib/uuid": "field-b"}, 102],
                        "literal",
                    ]
                ],
                "order-by": [
                    ["asc", {"lib/uuid": "sort"}, ["aggregation", {"lib/uuid": "ref"}, "agg-a"]]
                ],
                "joins": [
                    {
                        "alias": "joined",
                        "conditions": [["=", {"lib/uuid": "join-condition"}, 1, 1]],
                        "stages": [
                            {
                                "lib/type": "mbql.stage/mbql",
                                "source-table": 200,
                                "aggregation": [["count", {"lib/uuid": "nested-agg"}]],
                                "order-by": [["asc", {}, ["aggregation", {}, "nested-agg"]]],
                            }
                        ],
                    }
                ],
            },
            {
                "lib/type": "mbql.stage/mbql",
                "expressions": [
                    [
                        "*",
                        {"lib/uuid": "expression", "lib/expression-name": "ratio"},
                        [
                            "/",
                            {"lib/uuid": "division"},
                            ["field", {"lib/uuid": "field-c"}, "sum"],
                            2,
                        ],
                        100,
                    ]
                ],
                "fields": [["expression", {"lib/uuid": "expression-ref"}, "ratio"]],
            },
        ],
    }


def renamed_ids(value, suffix):
    """Fixture generator: rename every clause ID and aggregation reference together."""
    value = copy.deepcopy(value)

    def walk(item):
        if isinstance(item, dict):
            if isinstance(item.get("lib/uuid"), str):
                item["lib/uuid"] += suffix
            for child in item.values():
                walk(child)
        elif isinstance(item, list):
            if len(item) == 3 and item[0] == "aggregation" and isinstance(item[2], str):
                item[2] += suffix
            for child in item:
                walk(child)

    walk(value)
    return value


def test_complex_clause_uuid_renaming_preserves_query_and_payload():
    before = complex_query()
    after = renamed_ids(before, "-new")
    untouched = copy.deepcopy((before, after))
    assert dataset_query_semantically_matches(before, after)
    assert (before, after) == untouched


def test_mbql_field_conversion_marker_can_disappear_but_type_cannot_change():
    before = complex_query()
    options = before["stages"][0]["aggregation"][0][2][1]
    options.update({"lib/transformation-added-base-type": True, "base-type": "type/Integer"})
    after = renamed_ids(before, "-new")
    actual = after["stages"][0]["aggregation"][0][2][1]
    del actual["lib/transformation-added-base-type"]
    assert dataset_query_semantically_matches(before, after)
    actual["base-type"] = "type/Text"
    assert not dataset_query_semantically_matches(before, after)


@pytest.mark.parametrize(
    "change",
    [
        "operator",
        "reference",
        "literal",
        "field",
        "name",
        "alias",
        "type",
        "stage",
        "unknown",
        "value",
        "sort",
    ],
)
def test_complex_clause_uuid_renaming_keeps_real_changes_bound(change):
    before = complex_query()
    before["stages"][0]["filters"].append(
        ["value", {"lib/uuid": "value"}, ["sum", {"lib/uuid": "literal-id"}, 1]]
    )
    # Keep literal data equal while changing only the outer clause's generated ID.
    after = renamed_ids(before, "-new")
    after["stages"][0]["filters"][-1][2] = copy.deepcopy(before["stages"][0]["filters"][-1][2])
    assert dataset_query_semantically_matches(before, after)
    stage = after["stages"][0]
    if change == "operator":
        stage["aggregation"][0][0] = "avg"
    elif change == "reference":
        stage["order-by"][0][2][2] = "agg-b-new"
    elif change == "literal":
        stage["filters"][0][3] = "other"
    elif change == "field":
        stage["aggregation"][0][2][2] = 103
    elif change == "name":
        after["stages"][1]["expressions"][0][1]["lib/expression-name"] = "different"
    elif change == "alias":
        stage["joins"][0]["alias"] = "other"
    elif change == "type":
        stage["aggregation"][0][2][1]["base-type"] = "type/Text"
    elif change == "stage":
        stage["lib/uuid"] = "stage-id"
    elif change == "unknown":
        stage["unknown-data"] = {"lib/uuid": "unknown-id"}
    elif change == "value":
        stage["filters"][-1][2][1]["lib/uuid"] = "literal-changed"
    else:
        stage["order-by"][0][0] = "desc"
    assert not dataset_query_semantically_matches(before, after)


@pytest.mark.parametrize("reference", ["missing", "agg-b", ["resolved-index", 0]])
def test_aggregation_reference_cannot_collide_or_change_target(reference):
    before = complex_query()
    after = copy.deepcopy(before)
    after["stages"][0]["order-by"][0][2][2] = reference
    assert not dataset_query_semantically_matches(before, after)


def test_duplicate_aggregation_ids_remain_unresolved_and_bound():
    before = complex_query()
    before["stages"][0]["aggregation"][1][1]["lib/uuid"] = "agg-a"
    after = renamed_ids(before, "-new")
    assert not dataset_query_semantically_matches(before, after)


def test_complete_batch100_with_volatile_aggregation_filter_and_expression_ids(
    runtime, monkeypatch
):
    service, fake = runtime
    service.config = replace(service.config, max_batch_items=100)
    fake.cards = {
        i: {**copy.deepcopy(fake.cards[1]), "id": i, "dataset_query": complex_query()}
        for i in range(1, 101)
    }
    original = fake.get_json
    reads = 0

    def get(path, *, params=None):
        nonlocal reads
        result = original(path, params=params)
        if path.startswith("/api/card/"):
            reads += 1
            result["dataset_query"] = renamed_ids(result["dataset_query"], f"-read-{reads}")
        return result

    monkeypatch.setattr(fake, "get_json", get)
    for action in ("question_batch_trash", "question_batch_restore"):
        plan = service.action_prepare(action, {"question_ids": list(fake.cards)})
        result = execute(service, plan)
        assert result["outcome"] == "applied_verified" and len(result["applied_indexes"]) == 100
    assert fake.put_calls == 200 and all(not c["archived"] for c in fake.cards.values())
    assert all(c["dataset_query"] == complex_query() for c in fake.cards.values())
