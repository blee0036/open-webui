"""New-API compatible billing expressions (``pkg/billingexpr`` v1), evaluated as data.

The expression grammar, variables, token normalization and fixed-price rules match
New-API so that a price synced from a New-API instance, its official ratio preset,
models.dev or OpenRouter bills exactly as it would there. Expressions are parsed
by a small, closed parser; nothing is ever executed as Python.

Coefficients are USD per million tokens; ``fixed(x)`` is x USD per request:

  tier("base", p * 2.5 + c * 15 + cr * 0.25)
  len <= 200000 ? tier("standard", p * 3 + c * 15 + cr * 0.3 + cc * 3.75 + cc1h * 6)
                : tier("long_context", p * 6 + c * 22.5 + cr * 0.6 + cc * 7.5 + cc1h * 12)
  tier("image", fixed(0.04)) * image_count
  (tier("base", p * 5 + c * 25)) * (param("service_tier") == "fast" ? 2 : 1)

Variables: p (input not separately priced), c (output not separately priced),
len (whole input, for tier conditions), cr (cache read), cc (cache write, short
TTL), cc1h (Claude 1h write), cc12h (OpenAI 12h write), img / img_cr / img_o
(image input / image cache read / image output), ai / ao (audio in / out) and
image_count. A sub-category the expression does not reference stays in p / c at
the base price; an unpriced long cache write keeps New-API's fixed multiple of
the short write price (Claude 1h: 6 / 3.75, OpenAI 12h: 2).
"""

import datetime as dt
import json
import math
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from zoneinfo import ZoneInfo

MAX_QUOTA = 9_007_199_254_740_991
MAX_EXPRESSION_LENGTH = 16384
MAX_NODES = 2048
MAX_IMAGE_COUNT = 128  # New-API dto.MaxImageN
CLAUDE_CACHE_1H_MULTIPLIER = Decimal(6) / Decimal('3.75')
OPENAI_CACHE_12H_MULTIPLIER = Decimal(2)

TOKEN_VARIABLES = ('p', 'c', 'len', 'cr', 'cc', 'cc1h', 'cc12h', 'img', 'img_cr', 'img_o', 'ai', 'ao')
VARIABLES = set(TOKEN_VARIABLES) | {'image_count'}
REQUEST_PROBES = {'param', 'header', 'hour', 'minute', 'weekday', 'month', 'day'}
# name -> (min args, max args)
FUNCTIONS = {
    'tier': (2, 2),
    'fixed': (1, 1),
    'param': (1, 1),
    'header': (1, 1),
    'has': (2, 2),
    'hour': (1, 1),
    'minute': (1, 1),
    'weekday': (1, 1),
    'month': (1, 1),
    'day': (1, 1),
    'max': (2, 2),
    'min': (2, 2),
    'abs': (1, 1),
    'ceil': (1, 1),
    'floor': (1, 1),
}
# Never snapshot credentials into the ledger, even if an expression asks for them.
SECRET_HEADERS = {'authorization', 'proxy-authorization', 'x-api-key', 'api-key', 'cookie', 'x-goog-api-key'}


# --------------------------------------------------------------------------- parser


class Node:
    __slots__ = ('kind', 'value', 'children')

    def __init__(self, kind, value=None, children=()):
        self.kind = kind
        self.value = value
        self.children = tuple(children)

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


_PUNCT = (
    '||',
    '&&',
    '==',
    '!=',
    '<=',
    '>=',
    '**',
    '(',
    ')',
    ',',
    '?',
    ':',
    '+',
    '-',
    '*',
    '/',
    '%',
    '<',
    '>',
    '!',
    '^',
)
_WORD_OPS = {'and': '&&', 'or': '||', 'not': '!'}


def _tokenize(source):
    tokens, i, n = [], 0, len(source)
    while i < n:
        ch = source[i]
        if ch.isspace():
            i += 1
            continue
        if ch.isdigit() or (ch == '.' and i + 1 < n and source[i + 1].isdigit()):
            j = i
            while j < n and (source[j].isdigit() or source[j] in '._'):
                j += 1
            if j < n and source[j] in 'eE':
                k = j + 1
                if k < n and source[k] in '+-':
                    k += 1
                if k < n and source[k].isdigit():
                    j = k
                    while j < n and source[j].isdigit():
                        j += 1
            text = source[i:j].replace('_', '')
            try:
                tokens.append(('num', Decimal(text)))
            except InvalidOperation as error:
                raise ValueError(f'Invalid number: {source[i:j]}') from error
            i = j
            continue
        if ch.isalpha() or ch == '_':
            j = i
            while j < n and (source[j].isalnum() or source[j] == '_'):
                j += 1
            word = source[i:j]
            tokens.append(('op', _WORD_OPS[word]) if word in _WORD_OPS else ('name', word))
            i = j
            continue
        if ch in '"\'':
            j, quote = i + 1, ch
            while j < n and source[j] != quote:
                j += 2 if source[j] == '\\' else 1
            if j >= n:
                raise ValueError('Unterminated string literal')
            raw = source[i : j + 1]
            try:
                if quote == "'":
                    inner = re.sub(r'(?<!\\)"', r'\\"', raw[1:-1].replace("\\'", "'"))
                    value = json.loads('"' + inner + '"')
                else:
                    value = json.loads(raw)
            except ValueError as error:
                raise ValueError('Invalid string literal') from error
            tokens.append(('str', value))
            i = j + 1
            continue
        for punct in _PUNCT:
            if source.startswith(punct, i):
                tokens.append(('op', punct))
                i += len(punct)
                break
        else:
            raise ValueError(f'Unsupported character in pricing expression: {ch!r}')
    tokens.append(('end', None))
    return tokens


# expr-lang binary precedence (higher binds tighter)
_BINARY = {
    '||': 10,
    '&&': 15,
    '==': 20,
    '!=': 20,
    '<': 20,
    '>': 20,
    '<=': 20,
    '>=': 20,
    '+': 30,
    '-': 30,
    '*': 60,
    '/': 60,
    '%': 60,
    '**': 100,
    '^': 100,
}


class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def take(self, kind=None, value=None):
        token = self.tokens[self.pos]
        if (kind and token[0] != kind) or (value is not None and token[1] != value):
            raise ValueError(f'Unexpected token in pricing expression: {token[1]!r}')
        self.pos += 1
        return token

    def parse(self):
        node = self.ternary()
        if self.peek()[0] != 'end':
            raise ValueError(f'Unexpected token in pricing expression: {self.peek()[1]!r}')
        return node

    def ternary(self):
        cond = self.binary(0)
        if self.peek() == ('op', '?'):
            self.take()
            yes = self.ternary()
            self.take('op', ':')
            no = self.ternary()
            return Node('cond', None, (cond, yes, no))
        return cond

    def binary(self, minimum):
        left = self.unary()
        while True:
            kind, op = self.peek()
            precedence = _BINARY.get(op) if kind == 'op' else None
            if precedence is None or precedence < minimum:
                return left
            self.take()
            # ** and ^ are right-associative in expr-lang
            right = self.binary(precedence if op in ('**', '^') else precedence + 1)
            left = Node('bin', '**' if op == '^' else op, (left, right))

    def unary(self):
        kind, op = self.peek()
        if kind == 'op' and op in ('-', '+', '!'):
            self.take()
            return Node('unary', op, (self.unary(),))
        return self.primary()

    def primary(self):
        kind, value = self.take()
        if kind == 'num':
            return Node('num', value)
        if kind == 'str':
            return Node('str', value)
        if kind == 'op' and value == '(':
            node = self.ternary()
            self.take('op', ')')
            return node
        if kind == 'name':
            if value in ('true', 'false'):
                return Node('bool', value == 'true')
            if value == 'nil':
                return Node('nil')
            if self.peek() == ('op', '('):
                self.take()
                args = []
                if self.peek() != ('op', ')'):
                    args.append(self.ternary())
                    while self.peek() == ('op', ','):
                        self.take()
                        args.append(self.ternary())
                self.take('op', ')')
                return Node('call', value, args)
            return Node('name', value)
        raise ValueError(f'Unexpected token in pricing expression: {value!r}')


def _number_literal(node):
    if node.kind == 'num':
        return node.value
    if node.kind == 'unary' and node.value == '-' and node.children[0].kind == 'num':
        return -node.children[0].value
    return None


def _contains_pricing_marker(node):
    return any(part.kind == 'call' and part.value in ('tier', 'fixed') for part in node.walk())


def _uses_request_probe(node):
    return any(part.kind == 'call' and part.value in REQUEST_PROBES for part in node.walk())


def _is_request_price_multiplier(node):
    if node.kind == 'name' and node.value == 'image_count':
        return True
    if node.kind != 'cond':
        return False
    cond, yes, no = node.children
    if not _uses_request_probe(cond) or _contains_pricing_marker(cond):
        return False
    multiplier, fallback = _number_literal(yes), _number_literal(no)
    return multiplier is not None and fallback == 1 and multiplier >= 0


def _validate_fixed_tree(node):
    """New-API validateFixedPricingTree: one tier(name, fixed(x)) leaf per execution."""
    if node.kind == 'cond' and not _contains_pricing_marker(node.children[0]):
        _validate_fixed_tree(node.children[1])
        _validate_fixed_tree(node.children[2])
        return
    if node.kind == 'bin' and node.value == '*':
        left, right = node.children
        if _is_request_price_multiplier(right):
            return _validate_fixed_tree(left)
        if _is_request_price_multiplier(left):
            return _validate_fixed_tree(right)
    if node.kind == 'call' and node.value == 'tier' and len(node.children) == 2:
        if not _contains_pricing_marker(node.children[0]):
            price = node.children[1]
            if price.kind == 'call' and price.value == 'fixed' and len(price.children) == 1:
                amount = _number_literal(price.children[0])
                if amount is not None and amount >= 0 and Decimal(amount * 1_000_000).is_finite():
                    return
                raise ValueError('fixed price must be a finite, non-negative numeric literal')
            if not _contains_pricing_marker(price):
                return
    raise ValueError(
        'fixed pricing requires tier(name, fixed(amount)) leaves, conditional tiers and request multipliers; '
        'token and fixed charges cannot be combined in one leaf'
    )


class Compiled:
    __slots__ = ('source', 'tree', 'used', 'fixed', 'params', 'headers')

    def __init__(self, source, tree):
        self.source = source
        self.tree = tree
        names = [node for node in tree.walk()]
        self.used = {node.value for node in names if node.kind == 'name' and node.value in VARIABLES}
        self.fixed = any(node.kind == 'call' and node.value == 'fixed' for node in names)
        self.params = sorted(
            {node.children[0].value for node in names if node.kind == 'call' and node.value == 'param'}
        )
        self.headers = sorted(
            {node.children[0].value.strip().lower() for node in names if node.kind == 'call' and node.value == 'header'}
        )

    @property
    def per_call(self):
        """Every branch is a fixed request price: settlement needs no upstream usage."""
        return self.fixed and not (self.used & set(TOKEN_VARIABLES))


_CACHE: dict[str, Compiled] = {}


def compile_expression(source: str) -> Compiled:
    if not isinstance(source, str):
        raise ValueError('Pricing expression must be text')
    cached = _CACHE.get(source)
    if cached:
        return cached
    body = source[3:] if source.startswith('v1:') else source
    if not body.strip() or len(body) > MAX_EXPRESSION_LENGTH:
        raise ValueError(f'Pricing expression must contain 1–{MAX_EXPRESSION_LENGTH} characters')
    try:
        tree = _Parser(_tokenize(body)).parse()
        nodes = list(tree.walk())
    except RecursionError as error:
        raise ValueError('Pricing expression is nested too deeply') from error
    if len(nodes) > MAX_NODES:
        raise ValueError('Pricing expression is too complex')
    for node in nodes:
        if node.kind == 'name' and node.value not in VARIABLES:
            if node.value == 'u':
                raise ValueError('Task usage expressions (u()) are not supported here')
            raise ValueError(f'Unknown pricing variable: {node.value}')
        if node.kind == 'call':
            if node.value not in FUNCTIONS:
                if node.value == 'u':
                    raise ValueError('Task usage expressions (u()) are not supported here')
                raise ValueError(f'Unsupported pricing function: {node.value}')
            low, high = FUNCTIONS[node.value]
            if not low <= len(node.children) <= high:
                raise ValueError(f'Invalid arguments for {node.value}()')
            if node.value in ('param', 'header') and node.children[0].kind != 'str':
                # Literal paths let the ledger snapshot exactly the request values a price reads.
                raise ValueError(f'{node.value}() requires a literal string')
    if any(node.kind == 'call' and node.value == 'fixed' for node in nodes):
        _validate_fixed_tree(tree)
    compiled = Compiled(source, tree)
    if len(_CACHE) > 512:
        _CACHE.clear()
    _CACHE[source] = compiled
    return compiled


def parse_expression(source: str):
    """Compatibility helper: (compiled, used variable names)."""
    compiled = compile_expression(source)
    return compiled, set(compiled.used)


def is_per_call(source_or_used) -> bool:
    if isinstance(source_or_used, str):
        try:
            return compile_expression(source_or_used).per_call
        except ValueError:
            return False
    return not (set(source_or_used) & set(TOKEN_VARIABLES))


# --------------------------------------------------------------------------- evaluation


class _Trace:
    __slots__ = ('tier', 'billing_unit', 'fixed_price')

    def __init__(self):
        self.tier = None
        self.billing_unit = 'token'
        self.fixed_price = None


def _num(value, what='operand'):
    if isinstance(value, bool) or not isinstance(value, Decimal):
        raise ValueError(f'Pricing {what} must be a number')
    return value


def _equal(a, b):
    if isinstance(a, Decimal) and isinstance(b, Decimal):
        return a == b
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b if isinstance(a, bool) and isinstance(b, bool) else False
    return type(a) is type(b) and a == b


def _json_value(value):
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    return value


def lookup_param(body, path: str):
    """gjson subset used by New-API param(): dotted keys, array indexes and '#' (length)."""
    if body is None or not path.strip():
        return None
    parts, current, escaped = [], '', False
    for ch in path.strip():
        if escaped:
            current, escaped = current + ch, False
        elif ch == '\\':
            escaped = True
        elif ch == '.':
            parts.append(current)
            current = ''
        else:
            current += ch
    parts.append(current)
    value = body
    for part in parts:
        if isinstance(value, list):
            if part == '#':
                value = len(value)
            elif part.isdigit() and int(part) < len(value):
                value = value[int(part)]
            else:
                return None
        elif isinstance(value, dict) and part in value:
            value = value[part]
        else:
            return None
    return _json_value(value)


def _go_string(value):
    if value is None:
        return '<nil>'
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, Decimal):
        return format(value.normalize(), 'f') if value == value.to_integral_value() else str(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(',', ':'), default=str)
    return str(value)


def _now(tz):
    try:
        zone = ZoneInfo(str(tz).strip()) if str(tz).strip() else dt.timezone.utc
    except Exception:
        zone = dt.timezone.utc
    return dt.datetime.now(zone)


def _evaluate(node, env, request, trace):
    kind = node.kind
    if kind == 'num':
        return node.value
    if kind == 'str':
        return node.value
    if kind == 'bool':
        return node.value
    if kind == 'nil':
        return None
    if kind == 'name':
        return env[node.value]
    if kind == 'unary':
        value = _evaluate(node.children[0], env, request, trace)
        if node.value == '!':
            if not isinstance(value, bool):
                raise ValueError('! requires a boolean')
            return not value
        value = _num(value)
        return -value if node.value == '-' else value
    if kind == 'cond':
        test = _evaluate(node.children[0], env, request, trace)
        if not isinstance(test, bool):
            raise ValueError('Conditions must be boolean')
        return _evaluate(node.children[1] if test else node.children[2], env, request, trace)
    if kind == 'bin':
        op = node.value
        if op in ('&&', '||'):
            left = _evaluate(node.children[0], env, request, trace)
            if not isinstance(left, bool):
                raise ValueError(f'{op} requires booleans')
            if (op == '&&' and not left) or (op == '||' and left):
                return left
            right = _evaluate(node.children[1], env, request, trace)
            if not isinstance(right, bool):
                raise ValueError(f'{op} requires booleans')
            return right
        left = _evaluate(node.children[0], env, request, trace)
        right = _evaluate(node.children[1], env, request, trace)
        if op == '==':
            return _equal(left, right)
        if op == '!=':
            return not _equal(left, right)
        if op in ('<', '>', '<=', '>='):
            if isinstance(left, str) and isinstance(right, str):
                pass
            else:
                left, right = _num(left), _num(right)
            return {'<': left < right, '>': left > right, '<=': left <= right, '>=': left >= right}[op]
        if op == '+' and isinstance(left, str) and isinstance(right, str):
            return left + right
        left, right = _num(left), _num(right)
        if op == '+':
            return left + right
        if op == '-':
            return left - right
        if op == '*':
            return left * right
        if op == '/':
            if right == 0:
                raise ValueError('Division by zero in pricing expression')
            return left / right
        if op == '%':
            if right == 0 or left != left.to_integral_value() or right != right.to_integral_value():
                raise ValueError('% requires non-zero integers')
            return Decimal(int(left) % int(right))
        if op == '**':
            if abs(right) > 64:
                raise ValueError('Exponent is out of range')
            return left**right
    if kind == 'call':
        name, args = node.value, node.children
        if name == 'tier':
            label = _evaluate(args[0], env, request, trace)
            value = _num(_evaluate(args[1], env, request, trace), 'tier value')
            trace.tier = _go_string(label)
            return value
        if name == 'fixed':
            amount = _num(_evaluate(args[0], env, request, trace), 'fixed price')
            trace.billing_unit, trace.fixed_price = 'request', amount
            return amount * 1_000_000
        if name == 'param':
            return request.get('params', {}).get(args[0].value)
        if name == 'header':
            return request.get('headers', {}).get(args[0].value.strip().lower(), '')
        values = [_evaluate(arg, env, request, trace) for arg in args]
        if name == 'has':
            source, needle = values
            if source is None or not isinstance(needle, str) or needle == '':
                return False
            return needle in _go_string(source)
        if name in ('hour', 'minute', 'weekday', 'month', 'day'):
            now = _now(values[0])
            return Decimal(
                {
                    'hour': now.hour,
                    'minute': now.minute,
                    'weekday': (now.weekday() + 1) % 7,  # Go: Sunday = 0
                    'month': now.month,
                    'day': now.day,
                }[name]
            )
        values = [_num(value, f'{name}() argument') for value in values]
        if name == 'max':
            return max(values)
        if name == 'min':
            return min(values)
        if name == 'abs':
            return abs(values[0])
        if name == 'ceil':
            return values[0].to_integral_value(rounding='ROUND_CEILING')
        if name == 'floor':
            return values[0].to_integral_value(rounding='ROUND_FLOOR')
    raise ValueError('Invalid pricing expression')


# --------------------------------------------------------------------------- usage normalization


_MISSING = object()


def _int(value) -> int:
    """A reported token count. Missing is zero; malformed is an error (the entry stays pending)."""
    if value is _MISSING:
        return 0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Upstream usage contains a malformed token count')
    if value < 0 or value != int(value):
        raise ValueError('Upstream token counts must be non-negative integers')
    return int(value)


def _get(mapping, key) -> int:
    return _int(mapping.get(key, _MISSING) if isinstance(mapping, dict) else _MISSING)


def _details(raw: dict) -> dict:
    """OpenAI prompt_tokens_details, filled from input_tokens_details (New-API mergeInputTokenDetails)."""
    prompt = raw.get('prompt_tokens_details') or {}
    fill = raw.get('input_tokens_details') or raw.get('input_token_details') or {}
    if not isinstance(prompt, dict):
        prompt = {}
    if not isinstance(fill, dict):
        fill = {}
    merged = dict(fill)
    for key, value in prompt.items():
        if isinstance(value, dict):
            merged[key] = {**(merged.get(key) or {}), **{k: v for k, v in value.items() if v is not None}}
        elif (isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0) or key not in merged:
            merged[key] = value
    return merged


def usage_semantic(raw: dict, provider: str) -> str:
    if not isinstance(raw, dict):
        return 'openai'
    if provider == 'realtime':
        return 'realtime'
    if any(key in raw for key in ('promptTokenCount', 'candidatesTokenCount', 'cachedContentTokenCount')):
        return 'gemini'
    if 'prompt_eval_count' in raw or 'eval_count' in raw:
        return 'ollama'
    if raw.get('usage_semantic') == 'anthropic':
        return 'anthropic'
    if (
        provider == 'anthropic'
        and 'prompt_tokens' not in raw
        and ('cache_read_input_tokens' in raw or 'cache_creation_input_tokens' in raw or 'input_tokens' in raw)
    ):
        return 'anthropic'
    return 'openai'


def canonical_usage(raw: dict, provider: str = 'openai') -> dict:
    """Map provider usage to New-API's canonical dto.Usage fields used by billing."""
    raw = raw if isinstance(raw, dict) else {}
    semantic = usage_semantic(raw, provider)
    usage = {
        'semantic': semantic,
        'prompt': 0,
        'completion': 0,
        'cached': 0,
        'cache_write_total': 0,
        'cache_write_12h': 0,
        'claude_5m': 0,
        'claude_1h': 0,
        'image': 0,
        'audio': 0,
        'image_out': 0,
        'audio_out': 0,
        'cached_details': None,
    }
    if semantic == 'gemini':
        usage['prompt'] = _get(raw, 'promptTokenCount') + _get(raw, 'toolUsePromptTokenCount')
        usage['completion'] = _get(raw, 'candidatesTokenCount') + _get(raw, 'thoughtsTokenCount')
        usage['cached'] = _get(raw, 'cachedContentTokenCount')
        for key in ('promptTokensDetails', 'toolUsePromptTokensDetails'):
            for detail in raw.get(key) or []:
                modality = str((detail or {}).get('modality', '')).upper()
                if modality in ('IMAGE', 'AUDIO'):
                    usage['image' if modality == 'IMAGE' else 'audio'] += _get(detail, 'tokenCount')
        for detail in raw.get('candidatesTokensDetails') or []:
            modality = str((detail or {}).get('modality', '')).upper()
            if modality in ('IMAGE', 'AUDIO'):
                usage['image_out' if modality == 'IMAGE' else 'audio_out'] += _get(detail, 'tokenCount')
        total = _get(raw, 'totalTokenCount')
        if total and usage['completion'] <= 0:
            usage['completion'] = max(total - usage['prompt'], 0)
        return usage
    if semantic == 'ollama':
        usage['prompt'] = _get(raw, 'prompt_eval_count')
        usage['completion'] = _get(raw, 'eval_count')
        return usage
    if semantic == 'realtime':
        # New-API settles realtime expressions on input/output totals only.
        usage['prompt'] = _get(raw, 'input_tokens')
        usage['completion'] = _get(raw, 'output_tokens')
        return usage
    if semantic == 'anthropic':
        usage['prompt'] = _get(raw, 'input_tokens') if 'input_tokens' in raw else _get(raw, 'prompt_tokens')
        usage['completion'] = _get(raw, 'output_tokens') if 'output_tokens' in raw else _get(raw, 'completion_tokens')
        usage['cached'] = _get(raw, 'cache_read_input_tokens')
        usage['cache_write_total'] = max(_get(raw, 'cache_creation_input_tokens'), 0)
        creation = raw.get('cache_creation')
        if isinstance(creation, dict):
            usage['claude_5m'] = _get(creation, 'ephemeral_5m_input_tokens')
            usage['claude_1h'] = _get(creation, 'ephemeral_1h_input_tokens')
        else:
            usage['claude_5m'] = _get(raw, 'claude_cache_creation_5_m_tokens')
            usage['claude_1h'] = _get(raw, 'claude_cache_creation_1_h_tokens')
        return usage
    details = _details(raw)
    output = (
        raw.get('completion_tokens_details')
        or raw.get('output_tokens_details')
        or raw.get('output_token_details')
        or {}
    )
    usage['prompt'] = _get(raw, 'prompt_tokens') or _get(raw, 'input_tokens')
    usage['completion'] = _get(raw, 'completion_tokens') or _get(raw, 'output_tokens')
    usage['cached'] = _get(details, 'cached_tokens') or _get(raw, 'prompt_cache_hit_tokens')
    # New-API CacheCreationTokensTotal: the largest reported write field, never their sum.
    usage['cache_write_total'] = max(
        _get(details, 'cache_write_tokens'),
        _get(details, 'cached_creation_tokens'),
        _get(details, 'cache_creation_input_tokens'),
        0,
    )
    ttl = details.get('cache_write_tokens_details')
    if isinstance(ttl, dict):
        usage['cache_write_12h'] = min(max(_get(ttl, 'ttl_12h_tokens'), 0), usage['cache_write_total'])
    usage['image'] = _get(details, 'image_tokens')
    usage['audio'] = _get(details, 'audio_tokens')
    if isinstance(output, dict):
        usage['image_out'] = _get(output, 'image_tokens')
        usage['audio_out'] = _get(output, 'audio_tokens')
    cached_details = details.get('cached_tokens_details')
    if isinstance(cached_details, dict):
        usage['cached_details'] = {
            key: (_int(cached_details[key]) if key in cached_details and cached_details[key] is not None else None)
            for key in ('text_tokens', 'image_tokens', 'audio_tokens')
        }
    return usage


def billing_tokens(raw: dict, used: set[str], provider: str = 'openai') -> dict[str, Decimal]:
    """New-API BuildTieredTokenParams: p / c exclude only the categories the expression prices."""
    usage = canonical_usage(raw, provider)
    claude = usage['semantic'] == 'anthropic'
    p, c = usage['prompt'], usage['completion']
    cr, cc = usage['cached'], usage['cache_write_total']
    cc1h = cc12h = 0
    if claude:
        # Unlabelled remainder of the write total is billed as 5m writes.
        remainder = max(cc - usage['claude_5m'] - usage['claude_1h'], 0)
        cc, cc1h = usage['claude_5m'] + remainder, usage['claude_1h']
    img, ai = usage['image'], usage['audio']
    img_cr = 0
    details = usage['cached_details']
    if 'img_cr' in used and not claude and details and details.get('image_tokens') is not None:
        cached_image, cached, image = details['image_tokens'], usage['cached'], usage['image']
        valid = (
            0 <= cached_image <= cached
            and image >= cached_image
            and cached <= p
            and image <= p - (cached - cached_image)
        )
        remaining = cached - cached_image
        for key in ('text_tokens', 'audio_tokens'):
            count = details.get(key)
            if valid and count is not None:
                if count < 0 or count > remaining:
                    valid = False
                else:
                    remaining -= count
        if valid:
            img_cr = cached_image
            cr -= img_cr
            img -= img_cr
    img_o, ao = usage['image_out'], usage['audio_out']

    p, c, cr, cc, cc1h, img, img_cr, ai, img_o, ao = map(Decimal, (p, c, cr, cc, cc1h, img, img_cr, ai, img_o, ao))
    cc12h = Decimal(0)
    length = p + cr + cc + cc1h if claude else p
    if claude:
        if 'cr' not in used:
            p += cr
        if 'cc1h' not in used:
            cc += cc1h * CLAUDE_CACHE_1H_MULTIPLIER
            cc1h = Decimal(0)
        if 'cc' not in used:
            p += cc
            cc = Decimal(0)
    else:
        if 'cr' in used:
            p -= cr
        cc12h = Decimal(usage['cache_write_12h'])
        cc -= cc12h
        cc_tokens = cc
        if 'cc12h' in used:
            p -= cc12h
        else:
            cc_tokens += cc12h
            cc += cc12h * OPENAI_CACHE_12H_MULTIPLIER
            cc12h = Decimal(0)
        if 'cc' in used:
            p -= cc_tokens
        else:
            p += cc - cc_tokens
            cc = Decimal(0)
        if 'cc1h' in used:
            p -= cc1h
        if 'img' in used:
            p -= img
        if 'img_cr' in used:
            p -= img_cr
        if 'ai' in used:
            p -= ai
        if 'img_o' in used:
            c -= img_o
        if 'ao' in used:
            c -= ao
    # OpenAI cache counts can exceed the prompt; New-API clamps the remainder at zero.
    p, c = max(p, Decimal(0)), max(c, Decimal(0))
    return dict(
        p=p, c=c, len=length, cr=cr, cc=cc, cc1h=cc1h, cc12h=cc12h, img=img, img_cr=img_cr, img_o=img_o, ai=ai, ao=ao
    )


# --------------------------------------------------------------------------- pricing


def normalize_image_count(value) -> int:
    """New-API: n=0 counts as 1; the billable count is 1..MaxImageN."""
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return min(max(value, 1), MAX_IMAGE_COUNT)


def request_snapshot(expression: str, payload=None, headers=None) -> dict:
    """Capture only the request values the expression reads, so settlement can re-evaluate it."""
    compiled = compile_expression(expression)
    if not compiled.params and not compiled.headers:
        return {}
    body = payload if isinstance(payload, (dict, list)) else None
    lowered = {str(k).strip().lower(): str(v).strip() for k, v in (headers or {}).items() if str(v).strip()}
    return {
        'params': {path: lookup_param(body, path) for path in compiled.params},
        'headers': {name: lowered.get(name, '') for name in compiled.headers if name not in SECRET_HEADERS},
    }


def _thaw(request):
    """Accept a live snapshot or its JSON-stored form (see freeze_request)."""
    params = {}
    for key, value in ((request or {}).get('params') or {}).items():
        if isinstance(value, str) and value.startswith('#decimal:'):
            value = Decimal(value[9:])
        params[key] = _json_value(value)
    return {'params': params, 'headers': dict((request or {}).get('headers') or {})}


def freeze_request(request: dict) -> dict:
    """JSON-safe form of a request snapshot (Decimals tagged so they round-trip exactly)."""
    params = {}
    for key, value in (request or {}).get('params', {}).items():
        params[key] = f'#decimal:{value}' if isinstance(value, Decimal) else value
    return {'params': params, 'headers': dict((request or {}).get('headers', {}))} if request else {}


def price_usage(
    expression: str,
    raw: dict,
    *,
    provider='openai',
    multiplier='1',
    credits_per_usd=1_000_000,
    image_count=1,
    request=None,
) -> dict:
    compiled = compile_expression(expression)
    values = billing_tokens(raw, compiled.used, provider)
    count = normalize_image_count(image_count)
    env = {**values, 'image_count': Decimal(count)}
    trace = _Trace()
    try:
        with localcontext() as context:
            context.prec = 48
            factor = decimal(multiplier)
            if factor < 0:
                raise ValueError('Pricing multiplier cannot be negative')
            amount = _evaluate(compiled.tree, env, _thaw(request), trace)
            if isinstance(amount, bool) or not isinstance(amount, Decimal):
                raise ValueError('Pricing expression must return a number')
            if not amount.is_finite() or amount < 0:
                raise ValueError('Price must be finite and non-negative')
            usd = amount / 1_000_000 * factor
            points = usd * decimal(credits_per_usd)
            if not points.is_finite() or points > MAX_QUOTA:
                raise ValueError('Price must fit the quota limit')
            return {
                'credits': int(points.quantize(Decimal(1), rounding=ROUND_HALF_UP)),
                'usd': format(usd.normalize(), 'f') if usd else '0',
                'tier': trace.tier,
                'billing_unit': trace.billing_unit,
                'fixed_price': str(trace.fixed_price) if trace.fixed_price is not None else None,
                'image_count': count if 'image_count' in compiled.used else None,
                'billing_tokens': {k: str(v) for k, v in values.items()},
            }
    except (ArithmeticError, KeyError, TypeError, RecursionError) as error:
        raise ValueError('Pricing evaluation failed') from error


def decimal(value) -> Decimal:
    if isinstance(value, bool):
        raise ValueError('Boolean is not a billing quantity')
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as error:
        raise ValueError('Invalid billing number') from error
    if not result.is_finite() or abs(result) > MAX_QUOTA:
        raise ValueError('Billing number is out of range')
    return result


_SMOKE_VECTORS = [
    {'prompt_tokens': 0, 'completion_tokens': 0},
    {'prompt_tokens': 1000, 'completion_tokens': 1000},
    {'prompt_tokens': 100000, 'completion_tokens': 100000},
    {'prompt_tokens': 1000000, 'completion_tokens': 1000000},
    {
        'prompt_tokens': 1000,
        'completion_tokens': 100,
        'prompt_tokens_details': {
            'cached_tokens': 300,
            'image_tokens': 400,
            'cached_tokens_details': {'image_tokens': 200},
        },
    },
    {
        'prompt_tokens': 1000,
        'completion_tokens': 50,
        'prompt_tokens_details': {'audio_tokens': 200},
        'completion_tokens_details': {'audio_tokens': 50},
    },
    {'prompt_tokens': 2_147_483_647, 'completion_tokens': 0},
]
_SMOKE_REQUESTS = [
    {},
    {
        'headers': {'anthropic-beta': 'fast-mode-2026-02-01'},
        'body': {
            'service_tier': 'fast',
            'stream_options': {'include_usage': True},
            'messages': list(range(1, 22)),
        },
    },
]


def validate_price(expression: str, multiplier='1'):
    """New-API SmokeTestExpr: compile, then require finite non-negative results on sample usage."""
    compile_expression(expression)
    decimal(multiplier)
    for vector in _SMOKE_VECTORS:
        for sample in _SMOKE_REQUESTS:
            request = request_snapshot(expression, sample.get('body'), sample.get('headers'))
            for count in (1, MAX_IMAGE_COUNT):
                price_usage(expression, vector, multiplier=multiplier, image_count=count, request=request)


# --------------------------------------------------------------------------- summaries

_LEN_COMPARE = {'<', '<=', '>', '>='}
_MILLION = 1_000_000


def _len_thresholds(tree) -> list[int]:
    """Context sizes where a `len` comparison switches tiers, e.g. 200000 in `len <= 200000 ? ...`."""
    found = set()
    for node in tree.walk():
        if node.kind != 'bin' or node.value not in _LEN_COMPARE:
            continue
        left, right = node.children
        for name, number in ((left, right), (right, left)):
            value = _number_literal(number)
            if name.kind == 'name' and name.value == 'len' and value is not None and 0 < value < MAX_QUOTA:
                found.add(int(value))
    return sorted(found)


def _usd(expression, multiplier, usage, provider='openai', image_count=1) -> Decimal:
    result = price_usage(
        expression, usage, provider=provider, multiplier=multiplier, credits_per_usd=1, image_count=image_count
    )
    return Decimal(result['usd'])


def _rates(expression, multiplier, context) -> dict:
    """USD per million input / output tokens for a request whose whole input is `context` tokens."""
    prompt = _usd(expression, multiplier, {'prompt_tokens': context, 'completion_tokens': 0})
    both = _usd(expression, multiplier, {'prompt_tokens': context, 'completion_tokens': _MILLION})
    return {'input': float(prompt / context * _MILLION), 'output': float(both - prompt)}


def price_summary(expression: str, multiplier='1') -> dict:
    """What a member sees in the model list: base rates and, if tiered, the long-context rates."""
    compiled = compile_expression(expression)
    if compiled.per_call:
        return {'per_call': float(_usd(expression, multiplier, {}))}
    thresholds = _len_thresholds(compiled.tree)
    if not thresholds:
        return {**_rates(expression, multiplier, _MILLION), 'long': None}
    above = thresholds[0]
    base = _rates(expression, multiplier, min(above, _MILLION))
    return {**base, 'long': {'above': above, **_rates(expression, multiplier, above + _MILLION)}}


_FREE_CACHE: dict[tuple[str, str], bool] = {}


def is_free(expression: str, multiplier='1') -> bool:
    """A price of zero for every kind of usage: such models stay usable with no balance left."""
    key = (expression, str(multiplier))
    if key in _FREE_CACHE:
        return _FREE_CACHE[key]
    try:
        free = decimal(multiplier) == 0 or _always_zero(expression, multiplier)
    except ValueError:
        free = False
    if len(_FREE_CACHE) > 512:
        _FREE_CACHE.clear()
    _FREE_CACHE[key] = free
    return free


def _always_zero(expression, multiplier) -> bool:
    compiled = compile_expression(expression)
    sizes = {1000, _MILLION}
    for threshold in _len_thresholds(compiled.tree):
        sizes |= {max(threshold - 1, 1), threshold, threshold + 1}
    vectors = [*_SMOKE_VECTORS, *({'prompt_tokens': size, 'completion_tokens': _MILLION} for size in sizes)]
    anthropic = {
        'input_tokens': _MILLION,
        'output_tokens': _MILLION,
        'cache_read_input_tokens': _MILLION,
        'cache_creation_input_tokens': _MILLION,
        'cache_creation': {'ephemeral_5m_input_tokens': _MILLION // 2, 'ephemeral_1h_input_tokens': _MILLION // 2},
    }
    probes = [(vector, 'openai') for vector in vectors] + [(anthropic, 'anthropic')]
    for sample in _SMOKE_REQUESTS:
        request = request_snapshot(expression, sample.get('body'), sample.get('headers'))
        for usage, provider in probes:
            for count in (1, MAX_IMAGE_COUNT):
                result = price_usage(
                    expression,
                    usage,
                    provider=provider,
                    multiplier=multiplier,
                    credits_per_usd=1,
                    image_count=count,
                    request=request,
                )
                if Decimal(result['usd']) != 0:
                    return False
    return True
