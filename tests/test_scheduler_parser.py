from datetime import datetime, timedelta, timezone

from app.scheduler.parser import parse_one_shot_time


def test_parse_clock_today_when_future() -> None:
    now = datetime(2026, 5, 4, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    parsed = parse_one_shot_time("10点", timezone="Asia/Shanghai", now=now)

    assert parsed.ok is True
    assert parsed.schedule_kind == "at"
    assert parsed.next_run_at.isoformat() == "2026-05-04T02:00:00+00:00"


def test_parse_clock_tomorrow_when_past() -> None:
    now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone(timedelta(hours=8)))
    parsed = parse_one_shot_time("10点", timezone="Asia/Shanghai", now=now)

    assert parsed.ok is True
    assert parsed.next_run_at.isoformat() == "2026-05-05T02:00:00+00:00"


def test_parse_relative_minutes() -> None:
    now = datetime(2026, 5, 4, 8, 30, tzinfo=timezone(timedelta(hours=8)))
    parsed = parse_one_shot_time("20分钟后", timezone="Asia/Shanghai", now=now)

    assert parsed.ok is True
    assert parsed.next_run_at.isoformat() == "2026-05-04T00:50:00+00:00"
