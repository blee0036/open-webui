"""Usage log and statistics: per-request token/cost columns, the hourly rollup and the monthly report.

Every settled request is normalized once, at settlement, into quota_entry's token and cost columns
and added to quota_usage_hourly. Request rows are purged after QUOTA_ENTRY_RETENTION_DAYS; the hourly
rollup is kept, so charts and monthly reports cover any period.

Cost is micro-USD (1 USD = 1,000,000) at the model price frozen on the request. Credit plans charge
exactly that; request-count plans charge one request but still show what it would have cost.
"""

import datetime as dt
import time
from collections import defaultdict
from zoneinfo import ZoneInfo

from open_webui.models.team import QuotaUsageHourly
from open_webui.utils.quota_pricing import canonical_usage, price_usage
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

MICRO_USD = 1_000_000
HOUR = 3600
DAY = 86400
TOKEN_FIELDS = ('prompt_tokens', 'completion_tokens', 'cache_read_tokens', 'cache_write_tokens', 'total_tokens')
SUM_FIELDS = ('requests', *TOKEN_FIELDS, 'cost')


def usage_tokens(raw, provider='openai') -> dict:
    """Token counts in one shape for every provider. prompt_tokens includes cache reads and writes."""
    if not isinstance(raw, dict) or not raw:
        return dict.fromkeys(TOKEN_FIELDS, 0)
    try:
        usage = canonical_usage(raw, provider or 'openai')
    except ValueError:  # Malformed counts were settled at zero; there is nothing to show.
        return dict.fromkeys(TOKEN_FIELDS, 0)
    read, write = max(usage['cached'], 0), max(usage['cache_write_total'], 0)
    prompt, completion = max(usage['prompt'], 0), max(usage['completion'], 0)
    if usage['semantic'] == 'anthropic':
        prompt += read + write  # Anthropic reports cache tokens outside input_tokens.
    return {
        'prompt_tokens': prompt,
        'completion_tokens': completion,
        'cache_read_tokens': read,
        'cache_write_tokens': write,
        'total_tokens': prompt + completion,
    }


def entry_cost(unit, charged, pricing, usage) -> int:
    """Micro-USD for a settled request."""
    if unit == 'credits':
        return max(int(charged or 0), 0)
    pricing = pricing or {}
    if not pricing.get('expression'):
        return 0
    try:
        return price_usage(
            pricing['expression'],
            usage if isinstance(usage, dict) else {},
            provider=pricing.get('provider', 'openai'),
            multiplier=pricing.get('multiplier', '1'),
            credits_per_usd=MICRO_USD,
            image_count=pricing.get('image_count', 1),
            request=pricing.get('request'),
        )['credits']
    except ValueError:
        return 0


def usage_numbers(unit, charged, pricing, usage) -> dict:
    provider = (pricing or {}).get('provider', 'openai')
    return {**usage_tokens(usage, provider), 'cost': entry_cost(unit, charged, pricing, usage)}


async def _add_hourly(db, user_id, model_id, created_at, values):
    hour = int(created_at) // HOUR * HOUR
    row = {'user_id': user_id, 'model_id': model_id, 'hour': hour, 'requests': 1, **values}
    dialect = db.bind.dialect.name
    if dialect in {'sqlite', 'postgresql'}:
        insert = sqlite_insert if dialect == 'sqlite' else pg_insert
        statement = insert(QuotaUsageHourly).values(**row)
        await db.execute(
            statement.on_conflict_do_update(
                index_elements=['user_id', 'model_id', 'hour'],
                set_={field: getattr(QuotaUsageHourly, field) + statement.excluded[field] for field in SUM_FIELDS},
            )
        )
        return
    existing = await db.get(QuotaUsageHourly, (user_id, model_id, hour))
    if existing is None:
        db.add(QuotaUsageHourly(**row))
    else:
        await db.execute(
            update(QuotaUsageHourly)
            .where(
                QuotaUsageHourly.user_id == user_id,
                QuotaUsageHourly.model_id == model_id,
                QuotaUsageHourly.hour == hour,
            )
            .values({field: getattr(QuotaUsageHourly, field) + row[field] for field in SUM_FIELDS})
        )


async def record_usage(db, row, unit, usage):
    """Fill a settled entry's statistics columns and add it to the hourly rollup (same transaction)."""
    values = usage_numbers(unit, row.charged, row.pricing, usage)
    for field, value in values.items():
        setattr(row, field, value)
    await _add_hourly(db, row.user_id, row.model_id, row.created_at, values)


# ---------------------------------------------------------------------------
# Queries
# ---------------------------------------------------------------------------


def _empty():
    return dict.fromkeys(SUM_FIELDS, 0)


def _add(target, source):
    for field in SUM_FIELDS:
        target[field] += int(source[field] or 0)
    return target


async def hourly_rows(db, start, end, *, user_id=None, model_id=None):
    """Rollup rows with start <= hour < end (epoch seconds)."""
    stmt = select(QuotaUsageHourly).where(QuotaUsageHourly.hour >= start // HOUR * HOUR, QuotaUsageHourly.hour < end)
    if user_id:
        stmt = stmt.where(QuotaUsageHourly.user_id == user_id)
    if model_id:
        stmt = stmt.where(QuotaUsageHourly.model_id == model_id)
    return (await db.execute(stmt)).scalars().all()


def statistics(rows, *, tz_offset_minutes=0, by_user=False):
    """Summary, a per-day series split by model, model totals and (for admins) user totals.

    Days are in the viewer's time zone: whole-hour offsets are exact, others round to the hour.
    """
    shift = int(tz_offset_minutes) * 60
    summary, models, users = _empty(), defaultdict(_empty), defaultdict(_empty)
    days = defaultdict(lambda: defaultdict(_empty))
    for row in rows:
        values = {field: getattr(row, field) for field in SUM_FIELDS}
        _add(summary, values)
        _add(models[row.model_id], values)
        if by_user:
            _add(users[row.user_id], values)
        day = dt.datetime.fromtimestamp((row.hour + shift) // DAY * DAY, dt.UTC).strftime('%Y-%m-%d')
        _add(days[day][row.model_id], values)
    order = sorted(models, key=lambda m: (-models[m]['cost'], -models[m]['total_tokens'], m))
    return {
        'summary': summary,
        'models': [{'model_id': m, **models[m]} for m in order],
        'series': [{'day': day, 'models': {m: v for m, v in days[day].items()}} for day in sorted(days)],
        'users': sorted(
            ({'user_id': u, **v} for u, v in users.items()),
            key=lambda item: (-item['cost'], -item['total_tokens'], item['user_id']),
        ),
    }


# ---------------------------------------------------------------------------
# Monthly report (Markdown)
# ---------------------------------------------------------------------------


LOCAL_ACCOUNT = '本地账号'


def _org(value):
    """Enterprise (SSO connect) IDs a member signs in through; local accounts have none."""
    return value or LOCAL_ACCOUNT


def _usd(micro):
    return f'{micro / MICRO_USD:.2f}'


def _int(value):
    return f'{int(value):,}'


def _cell(value):
    return str(value).replace('|', '\\|').replace('\n', ' ')


def month_bounds(month: str, tz: ZoneInfo):
    start = dt.datetime.strptime(month, '%Y-%m').replace(tzinfo=tz)
    end = (start + dt.timedelta(days=32)).replace(day=1)
    return start, end


def render_monthly_report(title, month, tz, rows, users, now=None):
    """`rows`: hourly rollup rows of the month; `users`: (id, name, enterprise IDs) of everyone to list."""
    start, end = month_bounds(month, tz)
    totals, per_model, per_day = defaultdict(_empty), defaultdict(lambda: defaultdict(_empty)), defaultdict(dict)
    for row in rows:
        values = {field: getattr(row, field) for field in SUM_FIELDS}
        _add(totals[row.user_id], values)
        _add(per_model[row.user_id][row.model_id], values)
        day = dt.datetime.fromtimestamp(row.hour, tz).strftime('%Y-%m-%d')
        _add(per_day[row.user_id].setdefault(day, _empty()), values)
    people = {user_id: (name or user_id, _org(org)) for user_id, name, org in users}
    for user_id in totals:  # Deleted accounts still count.
        people.setdefault(user_id, (user_id, '—'))
    ranking = sorted(
        people,
        key=lambda u: (
            -totals[u]['cost'] if u in totals else 0,
            -totals[u]['total_tokens'] if u in totals else 0,
            -totals[u]['requests'] if u in totals else 0,
            people[u][0].lower(),
        ),
    )
    grand = _add(_empty(), {f: sum(t[f] for t in totals.values()) for f in SUM_FIELDS})
    generated = dt.datetime.fromtimestamp(now if now is not None else time.time(), tz)
    lines = [
        f'# {title} {month} 使用记录月报',
        '',
        f'- 统计周期：{start:%Y-%m-%d %H:%M:%S} 至 {end:%Y-%m-%d %H:%M:%S}（{tz.key}）',
        f'- 生成时间：{generated:%Y-%m-%d %H:%M:%S}',
        f'- 统计用户数：{len(people)}',
        f'- 总花销：{_usd(grand["cost"])} USD',
        f'- 总 token 消耗：{_int(grand["total_tokens"])}',
        f'- 请求数：{_int(grand["requests"])}',
        '',
        '## Leader Board',
        '',
        '| 排名 | 用户 | 企业 ID | 总花销（USD） | 总 token 消耗 | 请求数 |',
        '| ---: | --- | --- | ---: | ---: | ---: |',
    ]
    for rank, user_id in enumerate(ranking, 1):
        name, org = people[user_id]
        total = totals.get(user_id, _empty())
        lines.append(
            f'| {rank} | {_cell(name)} | {_cell(org)} | {_usd(total["cost"])} | '
            f'{_int(total["total_tokens"])} | {_int(total["requests"])} |'
        )
    lines += ['', '## 每人使用状况报表', '']
    for user_id in ranking:
        name, org = people[user_id]
        total = totals.get(user_id, _empty())
        lines += [
            f'### {_cell(name)} 的使用报告',
            '',
            f'- 企业 ID：{_cell(org)}',
            f'- 总花销：{_usd(total["cost"])} USD',
            f'- 总 token 消耗：{_int(total["total_tokens"])}',
            f'- 请求数：{_int(total["requests"])}',
            '',
            '#### 按模型统计',
            '',
        ]
        models = per_model.get(user_id, {})
        if models:
            lines += ['| 模型 | 花销（USD） | token 消耗 | 请求数 |', '| --- | ---: | ---: | ---: |']
            for model_id, v in sorted(models.items(), key=lambda i: (-i[1]['cost'], -i[1]['total_tokens'], i[0])):
                lines.append(
                    f'| {_cell(model_id)} | {_usd(v["cost"])} | {_int(v["total_tokens"])} | {_int(v["requests"])} |'
                )
        else:
            lines.append('无模型统计数据。')
        lines += ['', '#### 前三用量日', '']
        days = per_day.get(user_id, {})
        if days:
            lines += ['| 日期 | 花销（USD） | token 消耗 |', '| --- | ---: | ---: |']
            top = sorted(days.items(), key=lambda i: (-i[1]['cost'], -i[1]['total_tokens'], i[0]))[:3]
            for day, v in top:
                lines.append(f'| {day} | {_usd(v["cost"])} | {_int(v["total_tokens"])} |')
        else:
            lines.append('无按日统计数据。')
        lines.append('')
    return '\n'.join(lines)
