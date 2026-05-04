from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class ParsedSchedule:
    ok: bool
    schedule_kind: str | None = None
    schedule_expr: str | None = None
    next_run_at: datetime | None = None
    timezone: str | None = None
    confidence: float = 0.0
    reason: str = ""


_RELATIVE_PATTERN = re.compile(r"(?P<num>\d+)\s*(?P<unit>分钟|分鐘|分|小时|小時|钟头|鐘頭|天)\s*后")
_CLOCK_PATTERN = re.compile(r"(?P<hour>\d{1,2})\s*(?:点|點|:|：)\s*(?P<minute>\d{1,2}|半)?")


def parse_one_shot_time(
    time_text: str,
    *,
    timezone: str,
    now: datetime | None = None,
) -> ParsedSchedule:
    text = (time_text or "").strip()
    if not text:
        return ParsedSchedule(ok=False, timezone=timezone, reason="missing time_text")

    try:
        tz = _timezone(timezone)
    except ZoneInfoNotFoundError:
        return ParsedSchedule(ok=False, timezone=timezone, reason=f"unknown timezone: {timezone}")

    base = now.astimezone(tz) if now is not None else datetime.now(tz)

    relative = _parse_relative(text, base)
    if relative is not None:
        return _parsed(relative, timezone=timezone, confidence=0.98, reason="relative time")

    absolute = _parse_clock(text, base)
    if absolute is not None:
        return _parsed(absolute, timezone=timezone, confidence=0.95, reason="clock time")

    return ParsedSchedule(ok=False, timezone=timezone, confidence=0.0, reason="unsupported time expression")


def _parse_relative(text: str, base: datetime) -> datetime | None:
    match = _RELATIVE_PATTERN.search(text)
    if match is None:
        return None
    amount = int(match.group("num"))
    unit = match.group("unit")
    if unit in {"分钟", "分鐘", "分"}:
        return base + timedelta(minutes=amount)
    if unit in {"小时", "小時", "钟头", "鐘頭"}:
        return base + timedelta(hours=amount)
    if unit == "天":
        return base + timedelta(days=amount)
    return None


def _parse_clock(text: str, base: datetime) -> datetime | None:
    match = _CLOCK_PATTERN.search(text)
    if match is None:
        return None

    hour = int(match.group("hour"))
    minute_raw = match.group("minute")
    minute = 30 if minute_raw == "半" else int(minute_raw or 0)
    if hour > 23 or minute > 59:
        return None

    lowered = text.lower()
    if any(marker in lowered for marker in ("下午", "晚上", "今晚", "傍晚")) and 1 <= hour < 12:
        hour += 12
    if any(marker in lowered for marker in ("中午",)) and hour < 11:
        hour += 12

    days = 0
    if "明天" in text or "明日" in text or "tomorrow" in lowered:
        days = 1
    elif "后天" in text:
        days = 2

    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
    if days == 0 and candidate <= base:
        candidate += timedelta(days=1)
    return candidate


def _parsed(local_dt: datetime, *, timezone: str, confidence: float, reason: str) -> ParsedSchedule:
    if local_dt.tzinfo is None:
        raise ValueError("local_dt must be timezone-aware")
    utc_dt = local_dt.astimezone(UTC)
    return ParsedSchedule(
        ok=True,
        schedule_kind="at",
        schedule_expr=local_dt.isoformat(),
        next_run_at=utc_dt,
        timezone=timezone,
        confidence=confidence,
        reason=reason,
    )


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        fallback = {
            "UTC": UTC,
            "Etc/UTC": UTC,
            "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
            "Asia/Chongqing": timezone(timedelta(hours=8), name="Asia/Chongqing"),
            "Asia/Beijing": timezone(timedelta(hours=8), name="Asia/Beijing"),
        }.get(name)
        if fallback is None:
            raise
        return fallback
