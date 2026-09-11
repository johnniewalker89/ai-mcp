from __future__ import annotations

from pathlib import Path

import pytest

from mcp_metabase.config import MetabaseConfig


@pytest.fixture
def native_field_filter_query() -> dict:
    return {
        "lib/type": "mbql/query",
        "database": 50,
        "stages": [
            {
                "lib/type": "mbql.stage/native",
                "native": "select 1 where {{date}}",
                "template-tags": [
                    {
                        "id": "persisted-tag-id",
                        "name": "date",
                        "type": "dimension",
                        "widget-type": "date/all-options",
                        "dimension": [
                            "field",
                            {"lib/uuid": "00000000-0000-4000-8000-000000000001"},
                            123,
                        ],
                    }
                ],
            }
        ],
    }


@pytest.fixture
def configured(tmp_path: Path) -> MetabaseConfig:
    return MetabaseConfig(
        instance="test_metabase",
        base_url="https://metabase.example.org",
        api_key="mb_test_01234567890123456789",
        audit_dir=tmp_path / "audit",
        source_revision="test",
        supported_version_prefixes=("v0.63.",),
        expected_user_id=7,
        max_list_items=50,
        max_batch_items=10,
        plan_ttl_seconds=300,
        max_active_plans=20,
        max_plan_bytes=1_000_000,
        read_attempts=2,
    )
