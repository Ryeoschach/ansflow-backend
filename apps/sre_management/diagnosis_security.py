from __future__ import annotations

import re
from typing import Any


REDACTED_VALUE = '******'
SENSITIVE_KEY_PATTERN = re.compile(
    r'(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|'
    r'authorization|cookie|private[_-]?key|client[_-]?secret)',
    re.IGNORECASE,
)
SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r'(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|'
        r'authorization|client[_-]?secret)\b(\s*[:=]\s*)([^\s,;]+)',
    ),
    re.compile(r'(?i)\b(bearer)\s+([a-z0-9._~+/-]+=*)'),
)


def is_sensitive_key(key: Any) -> bool:
    return bool(SENSITIVE_KEY_PATTERN.search(str(key or '')))


def redact_sensitive_text(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in SENSITIVE_TEXT_PATTERNS:
        if pattern.groups == 3:
            redacted = pattern.sub(lambda match: f'{match.group(1)}{match.group(2)}{REDACTED_VALUE}', redacted)
        else:
            redacted = pattern.sub(lambda match: f'{match.group(1)} {REDACTED_VALUE}', redacted)
    return redacted


def redact_sensitive_data(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED_VALUE if is_sensitive_key(key) else redact_sensitive_data(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_data(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive_data(item) for item in value)
    return redact_sensitive_text(value)


def merge_redacted_secrets(incoming: Any, existing: Any) -> Any:
    if incoming == REDACTED_VALUE:
        return existing
    if isinstance(incoming, dict):
        existing_dict = existing if isinstance(existing, dict) else {}
        return {
            key: merge_redacted_secrets(item, existing_dict.get(key))
            for key, item in incoming.items()
        }
    if isinstance(incoming, list):
        existing_list = existing if isinstance(existing, list) else []
        return [
            merge_redacted_secrets(item, existing_list[index] if index < len(existing_list) else None)
            for index, item in enumerate(incoming)
        ]
    return incoming
