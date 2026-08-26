from __future__ import annotations

import pytest

from mcp_metabase.normalization import MutationValidationError
from mcp_metabase.query_policy import validate_native_preview_sql


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        ("clickhouse", "SELECT count() FROM analytics.events"),
        ("greenplum", "WITH sample AS (SELECT 1 AS id) SELECT * FROM sample"),
        ("mysql", "SELECT id FROM users LIMIT 10"),
        ("postgres", "SELECT * FROM pg_catalog.generate_series(1, 3)"),
    ],
)
def test_native_preview_policy_allows_ordinary_selects(engine: str, sql: str) -> None:
    result = validate_native_preview_sql(engine, sql)

    assert result.engine == engine
    assert len(result.sql_sha256) == 64


@pytest.mark.parametrize(
    ("engine", "sql"),
    [
        ("postgres", "WITH changed AS (DELETE FROM t RETURNING id) SELECT id FROM changed"),
        ("postgres", "SELECT * INTO copied FROM source"),
        ("postgres", "SELECT * FROM source FOR UPDATE"),
        ("postgres", "SELECT pg_read_file('/etc/passwd')"),
        ("greenplum", "SELECT nextval('unsafe_sequence')"),
        ("mysql", "SELECT load_file('/etc/passwd')"),
        ("clickhouse", "SELECT 1 SETTINGS max_threads = 10"),
        ("clickhouse", "SELECT * FROM url('https://example.test/file.csv', CSV)"),
    ],
)
def test_native_preview_policy_rejects_side_effects(engine: str, sql: str) -> None:
    with pytest.raises(MutationValidationError):
        validate_native_preview_sql(engine, sql)


def test_native_preview_policy_fails_closed_for_unknown_engine() -> None:
    with pytest.raises(MutationValidationError, match="supports only"):
        validate_native_preview_sql("oracle", "SELECT 1 FROM dual")
