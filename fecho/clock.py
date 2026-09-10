"""Fecho's product calendar: always Beijing time, independent of host timezone."""
import re
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo


BEIJING = ZoneInfo("Asia/Shanghai")
DEFAULT_DAILY_TIME = "21:00"


def now(instant: Optional[datetime] = None) -> datetime:
    value = instant or datetime.now(BEIJING)
    if value.tzinfo is None:
        raise ValueError("instant must include a timezone")
    return value.astimezone(BEIJING)


def today(instant: Optional[datetime] = None) -> str:
    return now(instant).strftime("%Y-%m-%d")


def from_iso(value: str) -> datetime:
    return now(datetime.fromisoformat(value.replace("Z", "+00:00")))


def validate_daily_time(value: str) -> str:
    match = re.fullmatch(r"(\d{2}):(\d{2})", value or "")
    if not match:
        raise ValueError("每日执行时间必须是北京时间 HH:MM，且早于 22:00")
    hour, minute = map(int, match.groups())
    if hour > 21 or minute > 59:
        raise ValueError("每日执行时间必须早于北京时间 22:00")
    return value
