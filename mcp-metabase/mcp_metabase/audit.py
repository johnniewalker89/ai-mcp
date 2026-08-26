from __future__ import annotations

import hashlib
import json
import os
import threading
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

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
            previous_hash = "0" * 64
            if path.exists():
                with path.open("rb") as existing:
                    existing.seek(0, os.SEEK_END)
                    size = existing.tell()
                    tail_size = min(size, 16_384)
                    existing.seek(size - tail_size)
                    lines = [line for line in existing.read(tail_size).splitlines() if line]
                if lines:
                    try:
                        previous_hash = json.loads(lines[-1])["event_hash"]
                    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError(
                            "Metabase audit chain is unreadable; mutation blocked."
                        ) from exc
            safe["previous_hash"] = previous_hash
            canonical = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            safe["event_hash"] = hashlib.sha256(canonical.encode()).hexdigest()
            with path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    + "\n"
                )
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(OSError):
                path.chmod(0o600)
        return str(safe["audit_id"])
