from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

REDACTIONS = (
    re.compile(r"(?i)(\bauthorization\s*[:=]\s*)(?:(?:basic|bearer|token)\s+)?[^\s,;]+"),
    re.compile(
        r"(?i)(\b(?:access[_-]?token|token|password|passwd|pwd|secret|client[_-]?secret|"
        r"api[_-]?key|private[_-]?token|credential)\s*[:=]\s*)"
        r"(?:[\"'][^\"'\r\n]*[\"']|[^\s,;]+)"
    ),
    re.compile(r"(?i)([a-z][a-z0-9+.-]*://[^\s/:@]*:)[^\s@]+(?=@)"),
    re.compile(r"\bmb_[A-Za-z0-9+/=_-]{16,}\b"),
)
ANSI_SEQUENCE_RE = re.compile(
    r"(?:"
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d).*?(?:\x07|\x1b\\|\x9c|\Z)"
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f]).*?(?:\x1b\\|\x07|\x9c|\Z)"
    r"|\x1b[@-_])",
    re.S,
)
UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
BIDI_CONTROL_RE = re.compile(r"[\u061c\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def redact_text(value: str, *, secrets: Iterable[str] = ()) -> str:
    result = value.replace("\r\n", "\n").replace("\r", "\n")
    result = ANSI_SEQUENCE_RE.sub("", result)
    result = BIDI_CONTROL_RE.sub("", result)
    result = UNSAFE_CONTROL_RE.sub("", result)
    supplied = {secret for secret in secrets if secret}
    if any(len(secret) < 6 and secret in result for secret in supplied):
        return "<redacted>"
    for secret in sorted(supplied, key=len, reverse=True):
        result = result.replace(secret, "<redacted>")
    for pattern in REDACTIONS:
        result = pattern.sub(
            lambda match: f"{match.group(1) if match.lastindex else ''}<redacted>",
            result,
        )
    return result


def redact_structure(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    if isinstance(value, str):
        return redact_text(value, secrets=secrets)
    if isinstance(value, list):
        return [redact_structure(item, secrets=secrets) for item in value]
    if isinstance(value, dict):
        return {str(key): redact_structure(item, secrets=secrets) for key, item in value.items()}
    return value
