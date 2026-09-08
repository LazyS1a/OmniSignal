"""Pure presentation helpers shared by Streamlit pages and tests."""

from __future__ import annotations

from datetime import datetime
from typing import Mapping
from urllib.parse import urlsplit


_STATUS_META = {
    "succeeded": ("成功", "success"),
    "success": ("成功", "success"),
    "ready": ("就绪", "success"),
    "allowed": ("已允许", "success"),
    "accepted": ("可用", "success"),
    "running": ("运行中", "info"),
    "pending": ("待确认", "warning"),
    "warning": ("需关注", "warning"),
    "paused": ("已暂停", "warning"),
    "quarantined": ("已隔离", "danger"),
    "failed": ("失败", "danger"),
    "error": ("错误", "danger"),
    "disabled": ("已停用", "neutral"),
}


def status_meta(value: object) -> tuple[str, str]:
    text = str(value or "unknown")
    return _STATUS_META.get(text.casefold(), (text, "neutral"))


def format_ratio(metric: Mapping[str, object]) -> str:
    numerator = _as_int(metric.get("numerator"))
    denominator = _as_int(metric.get("denominator"))
    ratio = metric.get("ratio")
    if ratio is None:
        return f"暂无可用分母 · {numerator}/{denominator}"
    try:
        value = float(ratio)
    except (TypeError, ValueError):
        return f"口径异常 · {numerator}/{denominator}"
    if not 0 <= value <= 1:
        return f"口径异常 · {numerator}/{denominator}"
    return f"{value * 100:.1f}% · {numerator}/{denominator}"


def safe_api_label(base_url: str) -> str:
    parsed = urlsplit(base_url)
    return parsed.netloc or "未配置"


def compact_id(value: object, length: int = 10) -> str:
    text = str(value or "—")
    return text if len(text) <= length else f"{text[:length]}…"


def format_timestamp(value: object) -> str:
    if value in {None, ""}:
        return "—"
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _as_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
