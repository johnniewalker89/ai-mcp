import copy

import pytest

from mcp_metabase.models import ObjectType, PatchOperation
from mcp_metabase.normalization import MutationValidationError, build_mutation


def broken_dashboard():
    return {
        "id": 1,
        "name": "Legacy",
        "archived": False,
        "collection_id": 20,
        "tabs": [],
        "parameters": [{"id": "p", "type": "string/=", "name": "P"}],
        "dashcards": [
            {
                "id": 2,
                "card_id": 3,
                "card": {
                    "id": 3,
                    "dataset_query": {
                        "type": "native",
                        "native": {"query": "SELECT 1", "template-tags": {}},
                    },
                },
                "parameter_mappings": [
                    {
                        "parameter_id": "p",
                        "card_id": 3,
                        "target": ["variable", ["template-tag", "missing"]],
                    }
                ],
            }
        ],
    }


def test_lifecycle_does_not_revalidate_or_write_untouched_legacy_mapping():
    before = broken_dashboard()
    snapshot = copy.deepcopy(before)
    mutation = build_mutation(
        object_type=ObjectType.DASHBOARD,
        raw_before=before,
        operations=[PatchOperation(op="set", path="/archived", value=True)],
    )
    assert mutation.write_payload == {"archived": True}
    assert before == snapshot
    assert mutation.after_state["dashcards"] == mutation.before_state["dashcards"]


def test_actual_mapping_edit_still_rejects_missing_template_tag():
    with pytest.raises(MutationValidationError):
        build_mutation(
            object_type=ObjectType.DASHBOARD,
            raw_before=broken_dashboard(),
            operations=[PatchOperation(op="replace_array", path="/parameters", value=[])],
        )
