"""Redact credentials before an error reaches logs, APIs or notifications."""
from __future__ import annotations

import re

REDACTED = "***REDACTED***"

_PATTERNS = (
    re.compile(r"(?i)(apikey|api_key|api-key|secret|token|password)=([^&\s'\"]+)"),
    re.compile(r"(?i)(authorization\s*[:=]\s*)([^,\s]+(?:\s+[^,\s]+)?)"),
    re.compile(r"(?i)(bearer\s+)([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"(?i)(/bot)([A-Za-z0-9:_-]{12,})(/)"),
    re.compile(r"(?i)(://[^:/\s]+:)([^@/\s]+)(@)"),
)


def sanitize_text(value: object) -> str:
    text = str(value)
    text = _PATTERNS[0].sub(lambda m: f"{m.group(1)}={REDACTED}", text)
    text = _PATTERNS[1].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _PATTERNS[2].sub(lambda m: f"{m.group(1)}{REDACTED}", text)
    text = _PATTERNS[3].sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)
    return _PATTERNS[4].sub(lambda m: f"{m.group(1)}{REDACTED}{m.group(3)}", text)


def sanitize(value):
    """Recursively sanitize structures used by monitoring and API errors."""
    if isinstance(value, dict):
        return {key: (REDACTED if any(
            marker in str(key).lower() for marker in (
                "apikey", "api_key", "authorization", "secret", "token", "password"))
            else sanitize(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    return sanitize_text(value) if isinstance(value, (str, Exception)) else value

