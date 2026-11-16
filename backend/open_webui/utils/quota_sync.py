"""Upstream price synchronization, ported from New-API.

Sources and parsing follow New-API ``controller/ratio_sync.go``; legacy ratio /
per-call prices are turned into billing expressions with the rules of
``model/model_pricing_conversion.go`` (including its runtime defaults and
hard-coded completion ratios), so a synced price bills exactly like New-API's
expression engine would.

Ratio units: model_ratio 1 == $0.002 / 1K tokens == $2 / 1M input tokens.
"""

import asyncio
import json
import math
from decimal import Decimal

import aiohttp

from open_webui.utils.quota_pricing import compile_expression, validate_price

USD = 500  # New-API ratio_setting.USD: $1 == 500 ratio units
QUOTA_PER_UNIT = 500_000  # New-API common.QuotaPerUnit
MODELS_DEV_INPUT_COST_RATIO_BASE = 1000
MAX_BODY = 10 << 20
DEFAULT_ENDPOINT = '/api/pricing'
UNTRUSTED_RATIO = 37.5  # New-API's default ratio for unknown models

PRESETS = [
    {
        'id': 'official',
        'name': 'New-API official ratio preset',
        'base_url': 'https://basellm.github.io',
        'endpoint': '/llm-metadata/api/newapi/ratio_config-v1-base.json',
    },
    {
        'id': 'models.dev',
        'name': 'models.dev price preset',
        'base_url': 'https://models.dev',
        'endpoint': 'https://models.dev/api.json',
    },
]
ENDPOINT_OPTIONS = ['/api/pricing', '/api/ratio_config', 'openrouter']

NUMERIC_FIELDS = (
    'model_ratio',
    'completion_ratio',
    'cache_ratio',
    'create_cache_ratio',
    'image_ratio',
    'audio_ratio',
    'audio_completion_ratio',
    'model_price',
)
BILLING_MODE = 'billing_mode'
BILLING_EXPR = 'billing_expr'
TIERED = 'tiered_expr'
SYNC_FIELDS = NUMERIC_FIELDS + (BILLING_MODE, BILLING_EXPR)

# common.ImageGenerationModels
IMAGE_MODELS = [
    'dall-e-3',
    'dall-e-2',
    'prefix:dall-e',
    'gpt-image-',
    'qwen-image',
    'z-image',
    'wan2.7-image-pro',
    'wan2.7-image',
    'wan2.6-image',
    'wan2.6-t2i',
    'wan2.5-t2i-preview',
    'wan2.2-t2i-flash',
    'wan2.2-t2i-plus',
    'wanx2.1-t2i-turbo',
    'wanx2.1-t2i-plus',
    'wanx2.0-t2i-turbo',
    'prefix:imagen-',
    'flux-',
    'flux.1-',
]


def is_image_generation_model(name: str) -> bool:
    name = name.lower()
    for rule in IMAGE_MODELS:
        if rule.startswith('prefix:'):
            if name.startswith(rule[7:]):
                return True
        elif rule in name:
            return True
    return False


def dec(value) -> Decimal:
    """Go decimal.NewFromFloat(x): the shortest decimal that round-trips the float."""
    return Decimal(repr(float(value)))


def fmt(value: Decimal) -> str:
    value = value.normalize()
    return format(value, 'f') if value != 0 else '0'


def _float(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


# ----------------------------------------------------------------- New-API ratio_setting defaults


def format_matching_model_name(name: str) -> str:
    for prefix in ('gemini-2.5-flash-lite', 'gemini-2.5-flash', 'gemini-2.5-pro'):
        if name.startswith(prefix):
            if '-thinking-' in name:
                name = f'{prefix}-thinking-*'
            break
    if name.startswith('gpt-4-gizmo'):
        name = 'gpt-4-gizmo-*'
    if name.startswith('gpt-4o-gizmo'):
        name = 'gpt-4o-gizmo-*'
    return name


def hardcoded_completion_ratio(name: str) -> tuple[float, bool]:
    """New-API getHardcodedCompletionModelRatio: (ratio, locked)."""
    if name.endswith('-all') or name.endswith('-gizmo-*'):
        return 2, False
    if name.startswith('gpt-'):
        if name.startswith('gpt-4o'):
            if name == 'gpt-4o-2024-05-13':
                return 3, True
            if name.startswith('gpt-4o-mini-tts'):
                return 20, False
            return 4, False
        if name.startswith('gpt-5'):
            if '.' not in name:
                return 8, True
            if name.startswith('gpt-5.4'):
                return (6.25, True) if name.startswith('gpt-5.4-nano') else (6, True)
            return 6, False
        if name.startswith('gpt-4.5-preview'):
            return 2, True
        if name.startswith('gpt-4-turbo') or name.endswith('gpt-4-1106') or name.endswith('gpt-4-1105'):
            return 3, True
        return 2, False
    if name.startswith('o1') or name.startswith('o3'):
        return 4, True
    if name == 'chatgpt-4o-latest':
        return 3, True
    if 'claude-3' in name or any(family in name for family in ('claude-sonnet-4', 'claude-opus-4', 'claude-haiku-4')):
        return 5, True
    if name.startswith('gpt-3.5'):
        if name == 'gpt-3.5-turbo' or name.endswith('0125'):
            return 3, True
        if name.endswith('1106'):
            return 2, True
        return 4.0 / 3.0, True
    if name.startswith('mistral-'):
        return 3, True
    if name.startswith('gemini-'):
        if name.startswith('gemini-1.5') or name.startswith('gemini-2.0'):
            return 4, True
        if name.startswith('gemini-2.5-pro'):
            return 8, False
        if name.startswith('gemini-2.5-flash'):
            if name.startswith('gemini-2.5-flash-preview'):
                return 3.5 / 0.15, False
            if name.startswith('gemini-2.5-flash-lite'):
                return 4, False
            return 2.5 / 0.3, False
        if name.startswith('gemini-robotics-er-1.5'):
            return 2.5 / 0.3, False
        if name.startswith('gemini-3-pro'):
            return (60, False) if name.startswith('gemini-3-pro-image') else (6, False)
        return 4, False
    if name.startswith('command'):
        return {
            'command-r': (3, True),
            'command-r-plus': (5, True),
            'command-r-08-2024': (4, True),
            'command-r-plus-08-2024': (4, True),
        }.get(name, (4, False))
    if name.lower().startswith('deepseek'):
        return 4, True
    if any(name.startswith(prefix) for prefix in ('ERNIE-Speed-', 'ERNIE-Lite-', 'ERNIE-Character', 'ERNIE-Functions')):
        return 2, True
    return {
        'llama2-70b-4096': (0.8 / 0.64, True),
        'llama3-8b-8192': (2, True),
        'llama3-70b-8192': (0.79 / 0.59, True),
    }.get(name, (1, False))


def resolve_completion_ratio(name: str, configured) -> float:
    """New-API ResolveCompletionRatio: locked family ratios override configuration."""
    name = format_matching_model_name(name)
    if '/' in name and configured is not None:
        return configured
    ratio, locked = hardcoded_completion_ratio(name)
    if locked:
        return ratio
    return configured if configured is not None else ratio


def gemini_audio_input_price(name: str) -> float:
    """New-API operation_setting.GetGeminiInputAudioPricePerMillionTokens."""
    for prefix, price in (
        ('gemini-2.5-flash-preview-native-audio', 3.00),
        ('gemini-2.5-flash-preview-lite', 0.50),
        ('gemini-2.5-flash-preview', 1.00),
        ('gemini-2.5-flash', 1.00),
        ('gemini-2.0-flash', 0.70),
        ('gemini-robotics-er-1.5', 1.00),
    ):
        if name.startswith(prefix):
            return price
    return 0


def legacy_dalle_rules(name: str) -> list[tuple[str, float]]:
    """New-API legacyDallePricingRules (size / quality multipliers)."""
    if not name.startswith('dall-e'):
        return []
    rules = []
    if name != 'dall-e-3':
        rules += [('param("size") == "256x256"', 0.4), ('param("size") == "512x512"', 0.45)]
    if name not in ('dall-e-2', 'dall-e'):
        rules.append(('param("size") == "1024x1792" || param("size") == "1792x1024"', 2))
    if name == 'dall-e-3':
        rules += [
            ('param("quality") == "hd" && param("size") != "1024x1792" && param("size") != "1792x1024"', 2),
            ('param("quality") == "hd" && (param("size") == "1024x1792" || param("size") == "1792x1024")', 1.5),
        ]
    return rules


# ----------------------------------------------------------------- upstream parsing


def convert_pricing_list(items) -> dict:
    """New-API type2: /api/pricing returns a list of per-model pricing rows."""
    result = {field: {} for field in SYNC_FIELDS}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get('model_name'):
            continue
        name = item['model_name']
        if item.get('billing_mode') == TIERED:
            result[BILLING_MODE][name] = TIERED
            result[BILLING_EXPR][name] = item.get('billing_expr') or ''
            continue
        if item.get('quota_type') == 1:
            if item.get('model_price') is not None:
                result['model_price'][name] = item['model_price']
        else:
            for field in ('model_ratio', 'completion_ratio'):
                if item.get(field) is not None:
                    result[field][name] = item[field]
        for field in ('cache_ratio', 'create_cache_ratio', 'image_ratio', 'audio_ratio', 'audio_completion_ratio'):
            if item.get(field) is not None:
                result[field][name] = item[field]
    return {field: values for field, values in result.items() if values}


def _round_ratio(value: float) -> float:
    return round(value * 1e6) / 1e6


def convert_openrouter(body) -> dict:
    model_ratio, completion_ratio, cache_ratio = {}, {}, {}
    for model in (body or {}).get('data') or []:
        pricing = (model or {}).get('pricing') or {}
        prompt, completion = _float(pricing.get('prompt')), _float(pricing.get('completion'))
        if prompt is None and completion is None:
            continue
        prompt, completion = prompt or 0, completion or 0
        if prompt < 0 or completion < 0:
            continue
        if prompt == 0 and completion == 0:
            model_ratio[model['id']] = 0.0
            continue
        if prompt <= 0:
            continue
        model_ratio[model['id']] = _round_ratio(prompt * 1000 * USD)
        completion_ratio[model['id']] = _round_ratio(completion / prompt)
        cached = _float(pricing.get('input_cache_read')) if pricing.get('input_cache_read') not in (None, '') else None
        if cached is not None and cached >= 0:
            cache_ratio[model['id']] = _round_ratio(cached / prompt)
    result = {'model_ratio': model_ratio, 'completion_ratio': completion_ratio, 'cache_ratio': cache_ratio}
    return {key: value for key, value in result.items() if value}


def convert_models_dev(body) -> dict:
    """models.dev costs are USD / 1M tokens; duplicates keep the cheapest non-zero input."""
    if not isinstance(body, dict) or not body:
        raise ValueError('Empty models.dev response')
    selected = {}
    for provider in sorted(body):
        models = (body[provider] or {}).get('models') if isinstance(body[provider], dict) else None
        for name in sorted(models or {}):
            cost = (models[name] or {}).get('cost') or {}
            source_input = _float(cost.get('input')) if cost.get('input') is not None else None
            if source_input is None or source_input < 0:
                continue
            output = None
            if cost.get('output') is not None:
                output = _float(cost.get('output'))
                if output is None or output < 0:
                    continue
            if source_input == 0 and output and output > 0:
                continue
            cache = _float(cost.get('cache_read')) if cost.get('cache_read') is not None else None
            candidate = (provider, source_input, output, cache if cache is not None and cache >= 0 else None)
            current = selected.get(name)
            if current is None:
                selected[name] = candidate
                continue
            current_nonzero, next_nonzero = current[1] > 0, source_input > 0
            if current_nonzero != next_nonzero:
                replace = next_nonzero
            elif next_nonzero and abs(source_input - current[1]) >= 1e-9:
                replace = source_input < current[1]
            else:
                replace = provider < current[0]
            if replace:
                selected[name] = candidate
    if not selected:
        raise ValueError('No valid models.dev pricing entries found')
    model_ratio, completion_ratio, cache_ratio = {}, {}, {}
    for name, (_, source_input, output, cache) in selected.items():
        if source_input == 0:
            model_ratio[name] = 0.0
            continue
        model_ratio[name] = _round_ratio(source_input * USD / MODELS_DEV_INPUT_COST_RATIO_BASE)
        if output is not None:
            completion_ratio[name] = _round_ratio(output / source_input)
        if cache is not None:
            cache_ratio[name] = _round_ratio(cache / source_input)
    result = {'model_ratio': model_ratio, 'completion_ratio': completion_ratio, 'cache_ratio': cache_ratio}
    return {key: value for key, value in result.items() if value}


def parse_upstream(body, *, kind: str) -> dict:
    if kind == 'openrouter':
        return convert_openrouter(body)
    if kind == 'models.dev':
        return convert_models_dev(body)
    if not isinstance(body, dict):
        raise ValueError('Unrecognized upstream response')
    if not body.get('success'):
        raise ValueError(str(body.get('message') or 'Upstream reported failure'))
    data = body.get('data')
    if isinstance(data, dict) and any(field in data for field in SYNC_FIELDS):
        return data  # type1: /api/ratio_config
    if isinstance(data, list):
        return convert_pricing_list(data)  # type2: /api/pricing
    raise ValueError('Failed to parse the upstream response')


def effective_sync_data(data: dict) -> dict:
    """New-API effectivePricingSyncData: one effective price kind per model."""
    result = {}
    names = set()
    for field in SYNC_FIELDS:
        entries = {}
        for name, raw in ((data or {}).get(field) or {}).items() if isinstance((data or {}).get(field), dict) else ():
            if field in NUMERIC_FIELDS:
                value = _float(raw)
                if value is None or value < 0:
                    continue
            else:
                value = raw
            entries[name] = value
            names.add(name)
        result[field] = entries
    modes, expressions = result[BILLING_MODE], result[BILLING_EXPR]
    for name in names:
        expression = expressions.get(name)
        if modes.get(name) == TIERED:
            if not isinstance(expression, str) or not expression.strip():
                for field in SYNC_FIELDS:
                    result[field].pop(name, None)
                continue
            expressions[name] = expression.strip()
            for field in NUMERIC_FIELDS:
                result[field].pop(name, None)
            continue
        expressions.pop(name, None)
        fixed, token = name in result['model_price'], name in result['model_ratio']
        if not fixed and not token:
            for field in SYNC_FIELDS:
                result[field].pop(name, None)
            continue
        modes[name] = 'ratio'
        if fixed:
            for field in NUMERIC_FIELDS:
                if field != 'model_price':
                    result[field].pop(name, None)
    return result


def model_values(data: dict, name: str) -> dict:
    return {field: data[field][name] for field in SYNC_FIELDS if name in data.get(field, {})}


# ----------------------------------------------------------------- legacy -> expression


def to_expression(name: str, values: dict) -> tuple[str | None, str | None]:
    """New-API PreviewModelPricingConversion without channel routing checks: (expression, reason)."""
    if values.get(BILLING_MODE) == TIERED:
        expression = values.get(BILLING_EXPR) or ''
        try:
            compile_expression(expression)
        except ValueError as error:
            return None, f'Upstream expression is not supported: {error}'
        return expression, None
    if 'realtime' in name.lower():
        return None, 'Realtime pricing must be converted manually.'
    if 'model_price' in values:
        price = fmt(dec(values['model_price']))
        if is_image_generation_model(name):
            expression = f'tier("image", fixed({price})) * image_count'
            for condition, multiplier in legacy_dalle_rules(name):
                expression += f' * ({condition} ? {fmt(dec(multiplier))} : 1)'
        else:
            expression = f'tier("request", fixed({price}))'
        return expression, None
    if 'model_ratio' not in values:
        return None, 'Configure an input price before converting this model.'

    base = dec(values['model_ratio']) * 1_000_000 / QUOTA_PER_UNIT
    completion = resolve_completion_ratio(name, values.get('completion_ratio'))
    effective = {
        'completion_ratio': completion,
        'cache_ratio': values.get('cache_ratio', 1.0),
        'create_cache_ratio': values.get('create_cache_ratio', 1.25),
        'image_ratio': values.get('image_ratio', 1.0),
    }
    lower = name.lower()
    if 'claude' in lower:
        mode = 'claude_ttl'
    elif 'create_cache_ratio' in values:
        mode = 'openai_ttl' if 'gpt' in lower else 'standard'
    else:
        mode = 'none'

    audio_input = audio_output = None
    text_branches = False
    gemini_audio = gemini_audio_input_price(name)
    if gemini_audio > 0:
        has_in, has_out = 'audio_ratio' in values, 'audio_completion_ratio' in values
        if has_in or has_out:
            audio_price = base * dec(values.get('audio_ratio', 1))
            text_output = base * dec(completion)
            if audio_price != dec(gemini_audio) or audio_price * dec(values.get('audio_completion_ratio', 1)) != text_output:
                return None, (
                    'Gemini and OpenAI audio prices differ for this model. '
                    'Use separate billing model names to convert them.'
                )
        audio_input = dec(gemini_audio)
    elif 'audio_ratio' in values or 'audio_completion_ratio' in values:
        audio_input = base * dec(values.get('audio_ratio', 1))
        audio_output = audio_input * dec(values.get('audio_completion_ratio', 1))
        text_branches = effective['cache_ratio'] != 1 or effective['image_ratio'] != 1 or mode != 'none'

    ordinary_audio = audio_output is not None
    if ordinary_audio and mode != 'none':
        text_branches = True
    merge_cache_read = base == 0 or (
        mode == 'none' and effective['image_ratio'] == 1 and (audio_input is None or ordinary_audio)
    )
    parts = [f'tier("base", p * {fmt(base)}']
    for variable, key, multiplier in (
        ('c', 'completion_ratio', 1),
        ('cr', 'cache_ratio', 1),
        ('cc', 'create_cache_ratio', 1),
        ('cc1h', 'create_cache_ratio', 6 / 3.75),
        ('cc12h', 'create_cache_ratio', 2),
        ('img', 'image_ratio', 1),
    ):
        if ordinary_audio and not text_branches and variable != 'c':
            continue
        if (
            (variable == 'cc' and mode == 'none')
            or (variable == 'cc1h' and mode != 'claude_ttl')
            or (variable == 'cc12h' and mode != 'openai_ttl')
        ):
            continue
        ratio = effective[key]
        if (variable == 'img' or (variable == 'cr' and merge_cache_read)) and ratio == 1:
            continue
        parts.append(f' + {variable} * {fmt(base * dec(ratio) * dec(multiplier))}')
    for variable, price in (('ai', audio_input), ('ao', audio_output)):
        if price is not None and not text_branches:
            parts.append(f' + {variable} * {fmt(price)}')
    expression = ''.join(parts) + ')'
    if ordinary_audio and text_branches:
        completion_price = base * dec(completion)
        expression = (
            f'(ai > 0 || ao > 0) ? tier("audio", max(len - ai, 0) * {fmt(base)} + c * {fmt(completion_price)}'
            f' + ai * {fmt(audio_input)} + ao * {fmt(audio_output)}) : {expression}'
        )
    try:
        validate_price(expression)
    except ValueError as error:
        return None, f'Converted price failed validation: {error}'
    return expression, None


# ----------------------------------------------------------------- fetching


def upstream_url(upstream: dict) -> tuple[str, str]:
    """(url, kind) where kind is 'openrouter', 'models.dev' or 'newapi'."""
    base = str(upstream.get('base_url') or '').rstrip('/')
    endpoint = str(upstream.get('endpoint') or '').strip()
    if endpoint == 'openrouter':
        return f'{base}/v1/models', 'openrouter'
    if endpoint.startswith(('http://', 'https://')):
        url = endpoint
    else:
        if not base.startswith(('http://', 'https://')):
            raise ValueError('Upstream base URL must start with http:// or https://')
        url = base + (endpoint if endpoint.startswith('/') else '/' + endpoint if endpoint else DEFAULT_ENDPOINT)
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if (parsed.hostname or '').lower() == 'models.dev' and (parsed.path.rstrip('/') or '/') == '/api.json':
        return url, 'models.dev'
    return url, 'newapi'


async def fetch_upstream(upstream: dict, timeout: float = 10) -> dict:
    url, kind = upstream_url(upstream)
    headers = {'Accept': 'application/json'}
    if kind == 'openrouter':
        key = str(upstream.get('api_key') or '').strip()
        if not key:
            raise ValueError('OpenRouter requires an API key')
        headers['Authorization'] = f'Bearer {key}'
    last = None
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout), trust_env=True) as session:
        for attempt in range(3):
            try:
                async with session.get(url, headers=headers, allow_redirects=False) as response:
                    if response.status != 200:
                        raise ValueError(f'HTTP {response.status}')
                    body = bytearray()
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        body.extend(chunk)
                        if len(body) > MAX_BODY:
                            raise ValueError('Upstream response is larger than 10 MB')
                    return parse_upstream(json.loads(bytes(body)), kind=kind)
            except (aiohttp.ClientError, asyncio.TimeoutError) as error:
                last = error
                await asyncio.sleep(0.2 * (2**attempt))
    raise ValueError(str(last) or type(last).__name__)


def untrusted(data: dict, name: str) -> bool:
    """New-API marks ratio 37.5 with completion 1 as New-API's unknown-model default."""
    ratio, completion = data.get('model_ratio', {}).get(name), data.get('completion_ratio', {}).get(name)
    return (
        bool(data.get('model_ratio'))
        and bool(data.get('completion_ratio'))
        and ratio is not None
        and completion is not None
        and abs(float(ratio) - UNTRUSTED_RATIO) < 1e-9
        and abs(float(completion) - 1) < 1e-9
    )


async def compare(upstreams: list[dict], local: dict[str, str], *, timeout: float = 10, include_unchanged=False):
    """Fetch every upstream and return per-model candidate expressions next to the local price."""
    semaphore = asyncio.Semaphore(8)

    async def run(upstream):
        async with semaphore:
            try:
                return upstream['name'], effective_sync_data(await fetch_upstream(upstream, timeout)), None
            except Exception as error:  # surfaced per upstream, never aborts the comparison
                return upstream['name'], None, str(error) or type(error).__name__

    results = await asyncio.gather(*(run(upstream) for upstream in upstreams))
    sources = [{'name': name, 'status': 'error' if error else 'success', 'error': error} for name, _, error in results]
    models: dict[str, dict] = {}
    for name, data, error in results:
        if error:
            continue
        names = set()
        for field in SYNC_FIELDS:
            names.update(data.get(field, {}))
        for model_id in names:
            values = model_values(data, model_id)
            expression, reason = to_expression(model_id, values)
            row = models.setdefault(model_id, {'model_id': model_id, 'current': local.get(model_id), 'upstreams': {}})
            row['upstreams'][name] = {
                'values': values,
                'expression': expression,
                'unsupported_reason': reason,
                'trusted': not untrusted(data, model_id),
                'same': expression is not None and expression == (local.get(model_id) or '').strip(),
            }
    for row in models.values():
        # New-API expressionPriority: once any upstream prices a model by expression,
        # legacy ratio candidates for it are not separate prices.
        if any(item['values'].get(BILLING_MODE) == TIERED for item in row['upstreams'].values()):
            row['upstreams'] = {
                name: item for name, item in row['upstreams'].items() if item['values'].get(BILLING_MODE) == TIERED
            }
    rows = []
    for row in models.values():
        changed = any(not item['same'] and item['expression'] for item in row['upstreams'].values())
        if changed or include_unchanged:
            rows.append(row)
    rows.sort(key=lambda row: row['model_id'])
    return {'sources': sources, 'models': rows}
