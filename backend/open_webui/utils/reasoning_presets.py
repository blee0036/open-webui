"""Built-in "reasoning effort" model control for known reasoning model families.

A model that matches a family below and has no model controls of its own gets a
menu that sends ``reasoning_effort``. The Responses API conversion moves it to
``reasoning.effort``. new-api maps the effort onto each Claude and Gemini model's
own thinking settings and picks the nearest level a model accepts. Other
providers receive the value as is, so each family only lists levels its API
accepts. Model controls set in the model editor replace the preset.
"""

import re

from open_webui.env import ENABLE_REASONING_PRESETS

CONTROL_KEY = 'reasoning_effort'

_LABELS = {
    'minimal': ('Minimal', '最低'),
    'low': ('Low', '低'),
    'medium': ('Medium', '中'),
    'high': ('High', '高'),
    'xhigh': ('Extra high', '超高'),
    'max': ('Max', '最大'),
}

# First match wins; None means the family gets no menu. Matched against the last
# path segment of the lowercased model id, so provider prefixes such as
# "openai/gpt-5.2" still match.
_FAMILIES: list[tuple[re.Pattern, list[str] | None]] = [
    # OpenAI (developers.openai.com model pages, reasoning guide; Azure "reasoning models" page):
    # gpt-5 minimal-high; gpt-5.1 none-high; gpt-5.2+ adds xhigh; max only on gpt-5.6 and gpt-6.
    # gpt-5-pro runs at high only; gpt-5.2-pro takes medium, high, xhigh; chat models do not reason.
    (re.compile(r'^gpt-5(\.\d+)?-chat'), None),
    (re.compile(r'^gpt-5-pro'), None),
    (re.compile(r'^gpt-5\.\d+-pro'), ['medium', 'high', 'xhigh']),
    (re.compile(r'^gpt-5(-mini|-nano)?($|-\d)'), ['minimal', 'low', 'medium', 'high']),
    (re.compile(r'^gpt-5-codex'), ['low', 'medium', 'high']),
    (re.compile(r'^gpt-5\.1-codex-max'), ['low', 'medium', 'high', 'xhigh']),
    (re.compile(r'^gpt-5\.1(?!\d)'), ['low', 'medium', 'high']),
    (re.compile(r'^gpt-5\.[2-5](?!\d)'), ['low', 'medium', 'high', 'xhigh']),
    (re.compile(r'^(gpt-5\.([6-9]|\d{2})|gpt-6)'), ['low', 'medium', 'high', 'xhigh', 'max']),
    # new-api picks the nearest effort each Claude model accepts (claude.go normalizeClaudeEffort).
    (re.compile(r'^claude-([a-z]+-)?[56]'), ['low', 'medium', 'high', 'xhigh', 'max']),
    # new-api maps effort onto each Gemini model's thinking level (gemini.go geminiLevelForEffort).
    (re.compile(r'^gemini-[34].*(image|tts|audio|live|embedding)'), None),
    (re.compile(r'^gemini-[34]'), ['low', 'medium', 'high']),
    # DeepSeek V4 (api-docs.deepseek.com): none, low, high (default), max.
    (re.compile(r'^deepseek-(v4|flash)'), ['low', 'high', 'max']),
    # Zhipu (docs.bigmodel.cn deep thinking): GLM-5 and 5.1 only take thinking.type;
    # reasoning_effort from GLM-5.2 (high, max), low from GLM-5.3.
    (re.compile(r'^glm-5\.2'), ['high', 'max']),
    (re.compile(r'^glm-5\.([3-9]|\d{2})'), ['low', 'high', 'max']),
    # xAI (docs.x.ai reasoning): grok-4.5 low-high; grok-4.6 and 4.7 add xhigh.
    (re.compile(r'^grok-4[.-]5'), ['low', 'medium', 'high']),
    (re.compile(r'^grok-4[.-][67]'), ['low', 'medium', 'high', 'xhigh']),
]


def preset_levels(model_id: str | None) -> list[str] | None:
    if not ENABLE_REASONING_PRESETS or not model_id:
        return None
    name = model_id.lower().rsplit('/', 1)[-1]
    for pattern, levels in _FAMILIES:
        if pattern.search(name):
            return levels
    return None


def preset_controls(*model_ids: str | None) -> dict:
    """Full controls (with params) for the first id that matches a family, else {}."""
    for model_id in model_ids:
        levels = preset_levels(model_id)
        if levels:
            return {
                CONTROL_KEY: {
                    'label': 'Reasoning effort',
                    'default': None,
                    'options': {
                        level: {'label': _LABELS[level][0], 'params': {'reasoning_effort': level}} for level in levels
                    },
                }
            }
    return {}


def preset_i18n(controls: dict) -> dict:
    """zh-CN labels in the shape localizeModelControls reads from info.meta.i18n."""
    control = controls.get(CONTROL_KEY)
    if not control:
        return {}
    labels = {f'model_controls.{CONTROL_KEY}.label': '思考强度'}
    for level in control['options']:
        labels[f'model_controls.{CONTROL_KEY}.options.{level}.label'] = _LABELS[level][1]
    return {'zh-CN': labels}
