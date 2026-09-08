"""Bounded hash-chain audit append. No automatic deletion of audit evidence."""

import hashlib
import json
import os
from contextlib import suppress
from pathlib import Path

MAX_EVENT_BYTES = 16_384
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
MAX_FILES = 24


def append_event(directory: Path, path: Path, prefix: str, event: dict) -> None:
    lock_path = directory / f".{prefix}-audit.write-lock"
    try:
        lock_stream = lock_path.open("xb")
    except FileExistsError:
        raise RuntimeError(
            "Audit writer is busy or its lock is stale; retry after the owner stops."
        ) from None
    try:
        with lock_stream:
            files = list(directory.glob(f"{prefix}-*.jsonl"))
            if path.is_symlink() or any(item.is_symlink() for item in files):
                raise RuntimeError("Audit files must be regular local files.")
            if len(files) > MAX_FILES or (path not in files and len(files) >= MAX_FILES):
                raise RuntimeError(
                    "Audit file-count limit reached; archive old audit files before new mutations."
                )
            size = path.stat().st_size if path.exists() else 0
            previous_hash = "0" * 64
            if size:
                with path.open("rb") as stream:
                    stream.seek(max(0, size - MAX_EVENT_BYTES))
                    tail = stream.read(MAX_EVENT_BYTES)
                if not tail.endswith(b"\n"):
                    raise RuntimeError(
                        "Audit chain has an incomplete final line; mutation blocked."
                    )
                try:
                    previous_hash = json.loads(tail.splitlines()[-1])["event_hash"]
                    if not isinstance(previous_hash, str) or len(previous_hash) != 64:
                        raise ValueError("invalid hash")
                except (KeyError, TypeError, ValueError):
                    raise RuntimeError("Audit chain is unreadable; mutation blocked.") from None
            event["previous_hash"] = previous_hash
            canonical = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            event["event_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            line = (
                json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            total_size = sum(item.stat().st_size for item in files)
            if len(line) > MAX_EVENT_BYTES:
                raise RuntimeError("Audit event exceeded 16384 bytes; mutation blocked.")
            if size + len(line) > MAX_FILE_BYTES or total_size + len(line) > MAX_TOTAL_BYTES:
                raise RuntimeError(
                    "Audit size limit reached; archive old audit files before new mutations."
                )
            with path.open("ab") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
            with suppress(OSError):
                path.chmod(0o600)
    finally:
        lock_path.unlink()
