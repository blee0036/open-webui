"""Calendar boundaries are computed in the plan timezone, stored as UTC seconds."""

import datetime as dt
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_reset(reset: dict) -> dict:
    kind = reset.get('kind', 'month')
    if kind not in {'day', 'week', 'month', 'days', 'never'}:
        raise ValueError('Unknown quota reset rule')
    timezone = reset.get('timezone', 'Asia/Singapore')
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError, ValueError) as error:
        raise ValueError('Use a valid IANA timezone') from error
    days = reset.get('days', 30)
    if kind == 'days' and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3660):
        raise ValueError('Fixed reset interval must be 1–3660 days')
    return {'kind': kind, 'timezone': timezone, **({'days': days} if kind == 'days' else {})}


def period_bounds(reset: dict, now: int, anchor: int | None = None) -> tuple[int, int | None]:
    reset = validate_reset(reset)
    timezone = ZoneInfo(reset['timezone'])
    local = dt.datetime.fromtimestamp(now, timezone)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    kind = reset['kind']
    if kind == 'never':
        return anchor if anchor is not None else now, None
    if kind == 'days':
        anchor = anchor if anchor is not None else now
        length = reset['days'] * 86400
        start_at = anchor + max(0, (now - anchor) // length) * length
        return start_at, start_at + length
    if kind == 'week':
        start -= dt.timedelta(days=start.weekday())
        end = start + dt.timedelta(days=7)
    elif kind == 'month':
        start = start.replace(day=1)
        end = start.replace(year=start.year + 1, month=1) if start.month == 12 else start.replace(month=start.month + 1)
    else:
        end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())
