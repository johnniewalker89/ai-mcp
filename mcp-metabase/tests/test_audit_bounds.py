import hashlib
import json

import pytest

from mcp_metabase import audit_io
from mcp_metabase.audit import AuditWriter


def test_writer_preserves_hash_chain_and_uses_bounded_tail(tmp_path):
    writer = AuditWriter(tmp_path)
    writer.write({"action": "first", "outcome": "intent"})
    writer.write({"action": "second", "outcome": "applied_verified"})
    rows = [
        json.loads(line)
        for line in next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    assert rows[1]["previous_hash"] == rows[0]["event_hash"]
    for row in rows:
        expected = row.pop("event_hash")
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        assert hashlib.sha256(encoded).hexdigest() == expected


@pytest.mark.parametrize("bound", ["MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_EVENT_BYTES"])
def test_quota_rejects_append_without_touching_existing_log(tmp_path, monkeypatch, bound):
    path = tmp_path / "test-2026-09.jsonl"
    audit_io.append_event(tmp_path, path, "test", {"action": "first"})
    baseline = path.read_bytes()
    monkeypatch.setattr(audit_io, bound, len(baseline))
    with pytest.raises(RuntimeError, match="limit|exceeded"):
        audit_io.append_event(tmp_path, path, "test", {"action": "x" * 1000})
    assert path.read_bytes() == baseline
    assert not (tmp_path / ".test-audit.write-lock").exists()


def test_busy_writer_does_not_modify_or_remove_another_lock(tmp_path):
    lock = tmp_path / ".test-audit.write-lock"
    lock.write_bytes(b"owner")
    with pytest.raises(RuntimeError, match="busy"):
        audit_io.append_event(tmp_path, tmp_path / "test-2026-09.jsonl", "test", {})
    assert lock.read_bytes() == b"owner"
    assert not list(tmp_path.glob("*.jsonl"))


def test_incomplete_final_line_is_not_extended(tmp_path):
    path = tmp_path / "test-2026-09.jsonl"
    path.write_bytes(b"partial")
    with pytest.raises(RuntimeError, match="incomplete"):
        audit_io.append_event(tmp_path, path, "test", {})
    assert path.read_bytes() == b"partial"


def test_file_count_limit_does_not_delete_evidence(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_io, "MAX_FILES", 1)
    old = tmp_path / "test-old.jsonl"
    old.write_bytes(b"retained")
    with pytest.raises(RuntimeError, match="file-count"):
        audit_io.append_event(tmp_path, tmp_path / "test-new.jsonl", "test", {})
    assert old.read_bytes() == b"retained"
