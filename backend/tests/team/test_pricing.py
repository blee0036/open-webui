import datetime as dt

import pytest
from open_webui.utils.quota_periods import period_bounds, validate_reset
from open_webui.utils.quota_pricing import (
    billing_tokens,
    compile_expression,
    freeze_request,
    price_usage,
    request_snapshot,
    validate_price,
)
from open_webui.utils.quota_sync import (
    convert_models_dev,
    convert_openrouter,
    convert_pricing_list,
    effective_sync_data,
    to_expression,
)


def test_whole_context_tier_includes_cache():
    price = 'len <= 200000 ? tier("short", p * 3 + cr * .3 + c * 15) : tier("long", p * 6 + cr * .6 + c * 22.5)'
    actual = price_usage(
        price, {'prompt_tokens': 250000, 'completion_tokens': 1000, 'prompt_tokens_details': {'cached_tokens': 240000}}
    )
    assert actual['tier'] == 'long'
    assert actual['credits'] == 226500
    assert actual['billing_tokens']['p'] == '10000'


def test_progressive_decimal_and_zero():
    assert (
        price_usage(
            'min(p, 200000) * 3 + max(p - 200000, 0) * 6 + c * 15', {'prompt_tokens': 250000, 'completion_tokens': 1000}
        )['credits']
        == 915000
    )
    assert price_usage('p * .1 + c * .2', {'prompt_tokens': 1, 'completion_tokens': 2})['credits'] == 1
    assert price_usage('0', {'prompt_tokens': 999999, 'completion_tokens': 999})['credits'] == 0
    fixed = price_usage('tier("request", fixed(.003))', {}, multiplier='1.5')
    assert fixed['credits'] == 4500 and fixed['billing_unit'] == 'request'


# ---- New-API BuildTieredTokenParams parity: cache read / write in every upstream dialect


def test_new_api_input_exclusion_table():
    # pkg/billingexpr/expr.md: prompt 1000 incl. 200 cache read and 100 image; completion 500 incl. 100 audio.
    usage = {
        'prompt_tokens': 1000,
        'completion_tokens': 500,
        'prompt_tokens_details': {'cached_tokens': 200, 'image_tokens': 100},
        'completion_tokens_details': {'audio_tokens': 100},
    }
    for expression, p, c in [
        ('p * 3 + c * 15', 1000, 500),
        ('p * 3 + c * 15 + cr * 0.3', 800, 500),
        ('p * 3 + c * 15 + cr * 0.3 + img * 2', 700, 500),
        ('p * 3 + c * 15 + ao * 50', 1000, 400),
    ]:
        tokens = billing_tokens(usage, compile_expression(expression).used)
        assert (tokens['p'], tokens['c']) == (p, c), expression


def test_claude_cache_read_and_ttl_writes():
    usage = {
        'input_tokens': 100,
        'output_tokens': 20,
        'cache_read_input_tokens': 200,
        'cache_creation_input_tokens': 400,
        'cache_creation': {'ephemeral_1h_input_tokens': 300},
    }
    # 1h = 300; the unlabelled 100 of the 400 total is billed as 5m.
    actual = price_usage('p*3 + cr*.3 + cc*3.75 + cc1h*6 + c*15', usage, provider='anthropic')
    assert actual['credits'] == 2835
    assert actual['billing_tokens']['len'] == '700'
    # Unpriced 1h writes join cc at 6 / 3.75; unpriced cache joins input.
    assert billing_tokens(usage, {'p', 'cr', 'cc', 'c'}, 'anthropic')['cc'] == 580
    assert billing_tokens(usage, {'p', 'c'}, 'anthropic')['p'] == 880
    # Legacy flat Claude fields are used only without the cache_creation object.
    flat = {**usage, 'cache_creation': None, 'claude_cache_creation_1_h_tokens': 300}
    assert billing_tokens(flat, {'p', 'cr', 'cc', 'cc1h', 'c'}, 'anthropic')['cc1h'] == 300


def test_openai_cache_write_fields_and_12h_ttl():
    usage = {
        'prompt_tokens': 1000,
        'completion_tokens': 10,
        'prompt_tokens_details': {
            'cached_tokens': 100,
            'cache_write_tokens': 300,
            'cache_write_tokens_details': {'ttl_30m_tokens': 100, 'ttl_12h_tokens': 200},
        },
    }
    all_priced = billing_tokens(usage, {'p', 'cr', 'cc', 'cc12h', 'c'})
    assert (all_priced['p'], all_priced['cr'], all_priced['cc'], all_priced['cc12h']) == (600, 100, 100, 200)
    # Unpriced 12h writes join cc at twice their count.
    assert billing_tokens(usage, {'p', 'cr', 'cc', 'c'})['cc'] == 500
    # With no write price at all, the 12h surcharge stays in p.
    assert billing_tokens(usage, {'p', 'c'})['p'] == 1200
    # Write fields are not added together: the largest reported one counts.
    for key in ('cached_creation_tokens', 'cache_creation_input_tokens'):
        same = {'prompt_tokens': 1000, 'prompt_tokens_details': {'cache_write_tokens': 300, key: 250}}
        assert billing_tokens(same, {'p', 'cc'})['cc'] == 300
    # Responses API details fill what Chat details omit.
    responses = {'input_tokens': 1000, 'output_tokens': 5, 'input_tokens_details': {'cached_tokens': 400}}
    assert billing_tokens(responses, {'p', 'cr', 'c'})['p'] == 600


def test_cache_overlap_clamps_instead_of_failing():
    # OpenAI can report cache counts above the prompt; New-API clamps p at zero.
    usage = {'prompt_tokens': 100, 'prompt_tokens_details': {'cached_tokens': 80, 'cache_write_tokens': 80}}
    tokens = billing_tokens(usage, {'p', 'cr', 'cc'})
    assert tokens['p'] == 0 and tokens['cr'] == 80 and tokens['cc'] == 80


def test_deepseek_gemini_ollama_and_realtime_usage():
    assert billing_tokens({'prompt_tokens': 100, 'prompt_cache_hit_tokens': 60}, {'p', 'cr'})['p'] == 40
    gemini = {
        'promptTokenCount': 1000,
        'candidatesTokenCount': 100,
        'thoughtsTokenCount': 50,
        'cachedContentTokenCount': 400,
        'promptTokensDetails': [{'modality': 'TEXT', 'tokenCount': 800}, {'modality': 'AUDIO', 'tokenCount': 200}],
    }
    tokens = billing_tokens(gemini, {'p', 'cr', 'ai', 'c'}, 'gemini')
    assert (tokens['p'], tokens['cr'], tokens['ai'], tokens['c']) == (400, 400, 200, 150)
    assert billing_tokens({'prompt_eval_count': 7, 'eval_count': 3}, {'p', 'c'}, 'ollama')['c'] == 3
    # New-API settles realtime expressions on input / output totals only.
    realtime = {
        'input_tokens': 1000,
        'output_tokens': 100,
        'input_token_details': {'cached_tokens': 300, 'audio_tokens': 600},
    }
    tokens = billing_tokens(realtime, {'p', 'cr', 'ai', 'c'}, 'realtime')
    assert (tokens['p'], tokens['cr'], tokens['ai'], tokens['len']) == (1000, 0, 0, 1000)


def test_image_cache_split_matches_new_api_example():
    usage = {
        'prompt_tokens': 1000,
        'completion_tokens': 100,
        'prompt_tokens_details': {
            'image_tokens': 600,
            'cached_tokens': 300,
            'cached_tokens_details': {'image_tokens': 200},
        },
    }
    actual = price_usage('p * 5 + cr * 1.25 + img * 8 + img_cr * 2 + c * 30', usage)
    assert actual['usd'] == '0.008225'
    # Without img_cr the aggregate cache stays in cr (no inferred split).
    assert billing_tokens(usage, {'p', 'cr', 'img'})['p'] == 100
    # An invalid breakdown falls back to aggregate cache billing.
    details = {**usage['prompt_tokens_details'], 'cached_tokens_details': {'image_tokens': 900}}
    bad = {**usage, 'prompt_tokens_details': details}
    assert billing_tokens(bad, {'p', 'cr', 'img', 'img_cr'})['img_cr'] == 0


# ---- expression grammar and fixed-price rules


def test_fixed_price_rules_and_image_count():
    assert price_usage('tier("image", fixed(0.04)) * image_count', {}, image_count=4)['usd'] == '0.16'
    # New-API does not multiply a plain request price by the image count.
    assert price_usage('tier("request", fixed(0.04))', {}, image_count=4)['usd'] == '0.04'
    assert price_usage('tier("image", fixed(1)) * image_count', {}, image_count=10**6)['image_count'] == 128
    for expression in (
        'fixed(0.01)',
        'tier("x", fixed(0.01) + p)',
        'tier("x", fixed(0.01)) * p',
        'tier("a", fixed(1)) + tier("b", fixed(1))',
        'tier("x", fixed(-1))',
    ):
        with pytest.raises(ValueError):
            compile_expression(expression)
    assert compile_expression('len <= 32000 ? tier("short", fixed(0.01)) : tier("long", p * 2 + c * 8)')


def test_request_rules_read_only_frozen_request_fields():
    expression = (
        '(tier("base", p * 2)) * (param("service_tier") == "fast" ? 2 : 1)'
        ' * (has(header("anthropic-beta"), "fast-mode") ? 2.5 : 1)'
        ' * (header("authorization") != "" ? 100 : 1)'
    )
    request = request_snapshot(
        expression,
        {'service_tier': 'fast', 'messages': [1, 2]},
        {'Anthropic-Beta': 'fast-mode-2026-02-01', 'Authorization': 'Bearer secret'},
    )
    assert request == {'params': {'service_tier': 'fast'}, 'headers': {'anthropic-beta': 'fast-mode-2026-02-01'}}
    stored = freeze_request(request)
    assert 'secret' not in str(stored)
    assert price_usage(expression, {'prompt_tokens': 1_000_000}, request=stored)['usd'] == '10'
    count = 'param("messages.#") > 20 ? tier("big", p) : tier("small", p)'
    assert price_usage(count, {}, request=request_snapshot(count, {'messages': list(range(21))}))['tier'] == 'big'


def test_time_rules_and_expr_syntax():
    validate_price('tier("base", p * 2) * (hour("Asia/Shanghai") >= 21 || hour("Asia/Shanghai") < 6 ? 0.5 : 1)')
    validate_price("v1:tier('base', p * 2 + c * 8)")
    assert price_usage('p > 5 && !(c > 5) ? tier("a", p) : tier("b", c)', {'prompt_tokens': 10})['tier'] == 'a'


@pytest.mark.parametrize(
    'expression',
    [
        "__import__('os')",
        'p ** 999',
        "tier('x', fixed('x'))",
        "'x' * 99999999",
        'True',
        'p[0]',
        '(lambda: 1)()',
        'p/0',
        '1e999',
        'p +',
        'u("seconds") * 0.4',
        'param(p)',
        '(' * 5000 + 'p' + ')' * 5000,
        "tier('base', p) if len else 0",
    ],
)
def test_reject_unsafe_prices(expression):
    with pytest.raises(ValueError):
        validate_price(expression)


# ---- upstream sync parity with New-API controller/model_management_test.go


@pytest.mark.parametrize(
    'name,values,expected',
    [
        ('conversion-defaults', {'model_ratio': 2}, 'tier("base", p * 4 + c * 4)'),
        ('gpt-4-32k', {'model_ratio': 30.0, 'cache_ratio': 1.0}, 'tier("base", p * 60 + c * 120)'),
        ('conversion-free-cache', {'model_ratio': 0.0, 'cache_ratio': 0.0}, 'tier("base", p * 0 + c * 0 + cr * 0)'),
        (
            'claude-3-7-sonnet-20250219',
            {'model_ratio': 1.5, 'cache_ratio': 0.1, 'create_cache_ratio': 1.25},
            'tier("base", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)',
        ),
        ('conversion-image-free', {'model_ratio': 2, 'image_ratio': 0}, 'tier("base", p * 4 + c * 4 + cr * 4 + img * 0)'),
        ('deepseek-chat', {'model_ratio': 0.135, 'cache_ratio': 0.25}, 'tier("base", p * 0.27 + c * 1.08 + cr * 0.0675)'),
        ('gpt-4o-custom', {'model_ratio': 2}, 'tier("base", p * 4 + c * 16)'),
        ('gemini-2.5-pro-custom', {'model_ratio': 2}, 'tier("base", p * 4 + c * 32)'),
        (
            'vendor/claude-sonnet-4',
            {'model_ratio': 2, 'completion_ratio': 0},
            'tier("base", p * 4 + c * 0 + cr * 4 + cc * 5 + cc1h * 8)',
        ),
        (
            'conversion-custom',
            {'model_ratio': 2, 'completion_ratio': 3, 'cache_ratio': 0, 'create_cache_ratio': 1.5, 'image_ratio': 2},
            'tier("base", p * 4 + c * 12 + cr * 0 + cc * 6 + img * 8)',
        ),
        (
            'gpt-5.6-terra',
            {'model_ratio': 5.0, 'completion_ratio': 5.0, 'cache_ratio': 0.1, 'create_cache_ratio': 1.25},
            'tier("base", p * 10 + c * 50 + cr * 1 + cc * 12.5 + cc12h * 25)',
        ),
        ('gpt-4o-2024-05-13', {'model_ratio': 2, 'completion_ratio': 99}, 'tier("base", p * 4 + c * 12)'),
        ('conversion-free', {'model_price': 0}, 'tier("request", fixed(0))'),
        ('gpt-image-2', {'model_price': 1}, 'tier("image", fixed(1)) * image_count'),
        ('conversion-audio', {'model_ratio': 1, 'audio_ratio': 2}, 'tier("base", p * 2 + c * 2 + ai * 4 + ao * 4)'),
    ],
)
def test_legacy_prices_convert_like_new_api(name, values, expected):
    assert to_expression(name, values) == (expected, None)


def test_dalle_rules_and_unsupported_sync_rows():
    expression, _ = to_expression('dall-e-3', {'model_price': 0.04})
    assert expression.startswith('tier("image", fixed(0.04)) * image_count * (')
    hd = request_snapshot(expression, {'quality': 'hd', 'size': '1024x1792'})
    assert price_usage(expression, {}, image_count=2, request=hd)['usd'] == '0.24'  # 0.04 * 2 * 2 * 1.5
    assert to_expression('gpt-4o-realtime-preview', {'model_ratio': 2})[0] is None
    tiered = {'billing_mode': 'tiered_expr', 'billing_expr': 'u("s")'}
    assert to_expression('task', tiered)[0] is None


def test_upstream_formats_normalize_like_new_api():
    pricing = convert_pricing_list(
        [
            {'model_name': 'a', 'quota_type': 0, 'model_ratio': 1, 'completion_ratio': 2, 'cache_ratio': 0.1},
            {'model_name': 'b', 'quota_type': 1, 'model_price': 0.05, 'model_ratio': 9},
            {'model_name': 'c', 'billing_mode': 'tiered_expr', 'billing_expr': 'tier("base", p * 2)'},
        ]
    )
    data = effective_sync_data(pricing)
    assert data['model_ratio'] == {'a': 1.0} and data['model_price'] == {'b': 0.05}
    assert data['billing_expr'] == {'c': 'tier("base", p * 2)'}
    models_dev = convert_models_dev(
        {
            'x': {'models': {'m': {'cost': {'input': 3, 'output': 15, 'cache_read': 0.3}}}},
            'y': {'models': {'m': {'cost': {'input': 2.5, 'output': 10}}}},
        }
    )
    # Cheapest non-zero input wins; $2.5 / 1M input == ratio 1.25.
    assert models_dev['model_ratio'] == {'m': 1.25} and models_dev['completion_ratio'] == {'m': 4.0}
    router = convert_openrouter(
        {
            'data': [
                {'id': 'o', 'pricing': {'prompt': '0.000003', 'completion': '0.000015', 'input_cache_read': '0.0000003'}}
            ]
        }
    )
    assert router == {'model_ratio': {'o': 1.5}, 'completion_ratio': {'o': 5.0}, 'cache_ratio': {'o': 0.1}}


def test_calendar_and_fixed_boundaries():
    now = int(dt.datetime(2026, 11, 13, 10, tzinfo=dt.timezone.utc).timestamp())
    start, end = period_bounds({'kind': 'month', 'timezone': 'Asia/Shanghai'}, now)
    assert dt.datetime.fromtimestamp(start, dt.timezone.utc).isoformat() == '2026-10-31T16:00:00+00:00'
    assert dt.datetime.fromtimestamp(end, dt.timezone.utc).isoformat() == '2026-11-30T16:00:00+00:00'
    assert period_bounds({'kind': 'days', 'days': 7}, now, now - 10 * 86400) == (now - 3 * 86400, now + 4 * 86400)
    assert period_bounds({'kind': 'never'}, now) == (now, None)
    with pytest.raises(ValueError):
        validate_reset({'kind': 'days', 'days': 0})
