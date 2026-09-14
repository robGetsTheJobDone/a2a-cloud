from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


CRON_MACROS = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
}


@dataclass(frozen=True)
class CronSpec:
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]


def normalize_cron(value: str) -> str:
    expr = value.strip().lower()
    expr = CRON_MACROS.get(expr, expr)
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron must have five fields")
    parse_cron(expr)
    return expr


def validate_timezone(value: str) -> str:
    zone = (value or "UTC").strip()
    try:
        ZoneInfo(zone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("unknown timezone") from exc
    return zone


def parse_cron(expr: str) -> CronSpec:
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron must have five fields")
    return CronSpec(
        minutes=_parse_field(parts[0], minimum=0, maximum=59),
        hours=_parse_field(parts[1], minimum=0, maximum=23),
        days=_parse_field(parts[2], minimum=1, maximum=31),
        months=_parse_field(parts[3], minimum=1, maximum=12),
        weekdays=_parse_field(parts[4], minimum=0, maximum=7, sunday_alias=True),
    )


def next_cron_time(
    expr: str,
    tz_name: str,
    *,
    after: datetime | None = None,
) -> datetime:
    spec = parse_cron(normalize_cron(expr))
    zone = ZoneInfo(validate_timezone(tz_name))
    cursor = after or datetime.now(timezone.utc)
    if cursor.tzinfo is None:
        cursor = cursor.replace(tzinfo=timezone.utc)
    local = cursor.astimezone(zone).replace(second=0, microsecond=0)
    local = local + timedelta(minutes=1)
    limit = local + timedelta(days=366 * 2)
    while local <= limit:
        if _matches(spec, local):
            return local.astimezone(timezone.utc)
        local = local + timedelta(minutes=1)
    raise ValueError("cron has no matching time in the next two years")


def _matches(spec: CronSpec, local: datetime) -> bool:
    # Cron uses 0 or 7 for Sunday. Python's weekday is Monday=0.
    cron_weekday = (local.weekday() + 1) % 7
    return (
        local.minute in spec.minutes
        and local.hour in spec.hours
        and local.day in spec.days
        and local.month in spec.months
        and cron_weekday in spec.weekdays
    )


def _parse_field(
    raw: str,
    *,
    minimum: int,
    maximum: int,
    sunday_alias: bool = False,
) -> set[int]:
    values: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            raise ValueError("empty cron field segment")
        values.update(
            _parse_segment(
                item,
                minimum=minimum,
                maximum=maximum,
                sunday_alias=sunday_alias,
            )
        )
    if not values:
        raise ValueError("cron field has no values")
    if sunday_alias:
        values = {0 if value == 7 else value for value in values}
    return values


def _parse_segment(
    item: str,
    *,
    minimum: int,
    maximum: int,
    sunday_alias: bool,
) -> set[int]:
    base, step = _split_step(item)
    if base == "*":
        start, end = minimum, maximum
    elif "-" in base:
        left, right = base.split("-", 1)
        start = _parse_int(left, minimum=minimum, maximum=maximum)
        end = _parse_int(right, minimum=minimum, maximum=maximum)
        if end < start:
            raise ValueError("cron range end must be >= start")
    else:
        value = _parse_int(base, minimum=minimum, maximum=maximum)
        if sunday_alias and value == 7:
            value = 0
        return {value}
    return set(range(start, end + 1, step))


def _split_step(item: str) -> tuple[str, int]:
    if "/" not in item:
        return item, 1
    base, raw_step = item.split("/", 1)
    step = _parse_int(raw_step, minimum=1, maximum=10_000)
    return base or "*", step


def _parse_int(raw: str, *, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"invalid cron value: {raw}") from exc
    if value < minimum or value > maximum:
        raise ValueError(f"cron value out of range: {raw}")
    return value
