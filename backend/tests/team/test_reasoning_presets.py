import pytest

from open_webui.utils import reasoning_presets
from open_webui.utils.models import apply_reasoning_preset
from open_webui.utils.payload import apply_model_controls
from open_webui.utils.reasoning_presets import CONTROL_KEY, preset_controls, preset_levels


@pytest.mark.parametrize(
    'model_id, levels',
    [
        ('gpt-5', ['minimal', 'low', 'medium', 'high']),
        ('gpt-5-mini', ['minimal', 'low', 'medium', 'high']),
        ('gpt-5-2025-08-07', ['minimal', 'low', 'medium', 'high']),
        ('gpt-5.1', ['low', 'medium', 'high']),
        ('gpt-5.1-codex-max', ['low', 'medium', 'high', 'xhigh']),
        ('gpt-5.2', ['low', 'medium', 'high', 'xhigh']),
        ('gpt-5.2-pro', ['medium', 'high', 'xhigh']),
        ('openai/gpt-5.5', ['low', 'medium', 'high', 'xhigh']),
        ('gpt-5.6', ['low', 'medium', 'high', 'xhigh', 'max']),
        ('gpt-6', ['low', 'medium', 'high', 'xhigh', 'max']),
        ('claude-opus-5-5', ['low', 'medium', 'high', 'xhigh', 'max']),
        ('claude-sonnet-6', ['low', 'medium', 'high', 'xhigh', 'max']),
        ('gemini-3.1-pro', ['low', 'medium', 'high']),
        ('gemini-4-flash', ['low', 'medium', 'high']),
        ('deepseek-v4-pro', ['low', 'high', 'max']),
        ('deepseek-v4.1-flash', ['low', 'high', 'max']),
        ('glm-5.2', ['high', 'max']),
        ('glm-5.3-flash', ['low', 'high', 'max']),
        ('grok-4.5', ['low', 'medium', 'high']),
        ('grok-4.7-fast', ['low', 'medium', 'high', 'xhigh']),
    ],
)
def test_families(model_id, levels):
    assert preset_levels(model_id) == levels


@pytest.mark.parametrize(
    'model_id',
    [
        'gpt-5-pro',
        'gpt-5-chat-latest',
        'gpt-4o',
        'claude-opus-4-6',
        'gemini-3-pro-image',
        'grok-4-0709',
        'qwen-max',
        None,
    ],
)
def test_not_matched(model_id):
    assert preset_levels(model_id) is None


def test_disabled(monkeypatch):
    monkeypatch.setattr(reasoning_presets, 'ENABLE_REASONING_PRESETS', False)
    assert preset_controls('gpt-5.2') == {}


def test_model_list_gets_public_menu_with_zh_labels():
    model = {'id': 'claude-opus-5-5', 'owned_by': 'openai', 'info': {'meta': {}, 'params': {}}}
    apply_reasoning_preset(model)
    control = model['info']['params']['model_controls'][CONTROL_KEY]
    assert control['options']['max'] == {'label': 'Max'}  # no params leak to clients
    assert model['info']['meta']['i18n']['zh-CN'][f'model_controls.{CONTROL_KEY}.label'] == '思考强度'


def test_own_controls_and_excluded_models_untouched():
    own = {'id': 'gpt-5.2', 'info': {'params': {'model_controls': {'x': {'label': 'X', 'options': {}}}}}}
    apply_reasoning_preset(own)
    assert list(own['info']['params']['model_controls']) == ['x']
    for model in (
        {'id': 'gpt-5.2', 'pipe': {'type': 'pipe'}},
        {'id': 'gpt-5.2', 'owner_id': 'u'},
        {'id': 'gpt-5.2', 'owned_by': 'ollama'},
    ):
        apply_reasoning_preset(model)
        assert 'info' not in model


def test_workspace_model_uses_base_model():
    model = {'id': 'my-helper', 'info': {'base_model_id': 'deepseek-v4-pro', 'params': {}}}
    apply_reasoning_preset(model)
    assert list(model['info']['params']['model_controls'][CONTROL_KEY]['options']) == ['low', 'high', 'max']


def test_choice_becomes_reasoning_effort():
    params = apply_model_controls({}, preset_controls('gpt-5.2'), {CONTROL_KEY: 'xhigh'})
    assert params['custom_params'] == {'reasoning_effort': 'xhigh'}
    assert 'custom_params' not in apply_model_controls({}, preset_controls('gpt-5.2'), {})
