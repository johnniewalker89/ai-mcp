from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from mcp_metabase.audit_io import append_event

ALLOWED_FIELDS = {
    "action",
    "actions_used",
    "audit_id",
    "changed_roots",
    "close_reason",
    "credential_fingerprint",
    "digest",
    "expires_at_epoch",
    "identity_marker",
    "instance",
    "max_actions",
    "object_id",
    "object_ids",
    "object_type",
    "origin",
    "outcome",
    "plan_id",
    "rollback_source_plan_id",
    "session_id",
    "state_sha256",
}


class AuditWriter:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> str:
        safe = {key: event[key] for key in ALLOWED_FIELDS if key in event}
        safe["timestamp"] = datetime.now(UTC).isoformat()
        safe["audit_id"] = str(uuid4())
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._directory / f"metabase-{datetime.now(UTC):%Y-%m}.jsonl"
        with self._lock:
            append_event(self._directory, path, "metabase", safe)
        return str(safe["audit_id"])
