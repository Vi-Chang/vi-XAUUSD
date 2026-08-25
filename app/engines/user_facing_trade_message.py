"""Single sanitizing gateway for every user-facing trade message."""
from __future__ import annotations

import math
import re
from collections.abc import Callable
from typing import Any

from app.engines.multi_timeframe_bias import (
    derive_multi_timeframe_bias,
    timeframe_bias_lines,
)

NULLISH = {"", "none", "null", "undefined", "nan", "n/a", "—"}
_NULLISH_TEXT = re.compile(r"(?i)(?:^|[：:\s])(none|null|undefined|nan|n/a)(?:$|[\s，,。])")


def _is_nullish(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return isinstance(value, str) and value.strip().lower() in NULLISH


def sanitize_user_facing_payload(value: Any) -> Any:
    """Recursively omit transport/debug nulls before rendering."""
    if _is_nullish(value):
        return None
    if isinstance(value, dict):
        # Keep the schema intact because legacy render helpers may index an
        # optional key directly. Nulls become empty transport values and the
        # completed line is omitted after rendering.
        return {key: (clean if (clean := sanitize_user_facing_payload(item))
                      is not None else "") for key, item in value.items()}
    if isinstance(value, list):
        return [clean for item in value
                if (clean := sanitize_user_facing_payload(item)) is not None]
    return value


def assert_no_nullish_user_facing_text(text: str) -> None:
    if _NULLISH_TEXT.search(text) or any(
            line.rstrip().endswith(("：—", ":—", "：", ":")) for line in text.splitlines()):
        raise ValueError("NULLISH_OR_INCOMPLETE_USER_FACING_TEXT")


def _clean_lines(text: str) -> list[str]:
    cleaned: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        lower = line.lower()
        if not line:
            continue
        if _NULLISH_TEXT.search(line):
            continue
        if line.endswith(("：—", ":—", "：", ":")):
            continue
        if line.startswith("持倉：未取得"):
            continue
        if any(token in lower for token in ("delivery_unknown", "event uuid", "internal diagnostics")):
            continue
        cleaned.append(line)
    return cleaned


class UserFacingTradeMessageBuilder:
    """Adds the shared bias view and enforces null-safe compact output."""

    def build(self, event: dict, legacy_renderer: Callable[[dict], str]) -> str:
        payload = sanitize_user_facing_payload(event) or {}
        rendered = legacy_renderer(payload)
        lines = _clean_lines(rendered)
        canonical = payload.get("canonicalDecision") or {}
        snapshot = (payload.get("multiTimeframeBias") or
                    canonical.get("multiTimeframeBias"))
        if not snapshot:
            source = payload.get("normalized_analysis") or canonical or payload
            snapshot = derive_multi_timeframe_bias(
                source, canonical_bias=str(payload.get("marketBias") or
                                           canonical.get("marketBias") or "NEUTRAL"))
        bias_lines = timeframe_bias_lines(snapshot)
        # A multi-timeframe block supersedes every ambiguous single market
        # direction row. Unknown timeframes are simply omitted.
        if bias_lines:
            lines = [line for line in lines if not line.startswith((
                "市場方向：", "高週期方向：", "大方向："))]
            insert_at = 1
            if len(lines) > 1 and lines[1].startswith("現價："):
                insert_at = 2
            lines[insert_at:insert_at] = bias_lines
        result = "\n".join(lines)
        assert_no_nullish_user_facing_text(result)
        return result
