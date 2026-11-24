"""Meter actual model HTTP attempts and observe usage without changing response bytes."""

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

import aiohttp
from open_webui.env import MODEL_REQUEST_RETRIES
from open_webui.utils import quotas

log = logging.getLogger(__name__)


class ModelAttempt:
    def __init__(self, entry, provider):
        self.entry = entry
        self.provider = provider
        self.usage = None
        self.buffer = b''
        self.event = []
        self.finished = False
        self.complete_usage = False

    async def sent(self):
        if self.entry:
            self.entry['sent'] = True
            await quotas.mark_sent(self.entry)

    def observe(self, data):
        if not isinstance(data, dict):
            return
        value = (
            data.get('usage') or (data.get('response') or {}).get('usage') or (data.get('message') or {}).get('usage')
        )
        if value is None:
            value = data.get('usageMetadata')
        if value is None and 'eval_count' in data:
            value = {key: data[key] for key in ('prompt_eval_count', 'eval_count') if key in data}
        if isinstance(value, dict) and any(
            key in value
            for key in (
                'prompt_tokens',
                'input_tokens',
                'output_tokens',
                'completion_tokens',
                'promptTokenCount',
                'candidatesTokenCount',
                'eval_count',
            )
        ):
            current = dict(self.usage or {})
            for key, item in value.items():
                current[key] = {**(current.get(key) or {}), **item} if isinstance(item, dict) else item
            self.usage = current
            has_input = any(
                key in current for key in ('prompt_tokens', 'input_tokens', 'promptTokenCount', 'prompt_eval_count')
            )
            if self.provider == 'anthropic':
                self.complete_usage = (
                    has_input
                    and data.get('type') in {'message_delta', 'message_stop', 'message'}
                    and 'output_tokens' in current
                )
            elif self.provider == 'ollama':
                self.complete_usage = has_input and data.get('done') is True and 'eval_count' in current
            else:
                self.complete_usage = has_input and any(
                    key in current for key in ('completion_tokens', 'output_tokens', 'candidatesTokenCount')
                )

    def _event(self, value):
        try:
            self.observe(json.loads(value))
        except (ValueError, TypeError):
            pass  # Unavailable usage retains its reservation rather than inventing a zero charge.

    def feed(self, chunk):
        if not self.entry:
            return
        if isinstance(chunk, str):
            chunk = chunk.encode()
        self.buffer += chunk
        if len(self.buffer) > 8 * 1024 * 1024:
            self.buffer = b''
            self.event = []
            return
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            line = line.rstrip(b'\r')
            if line.startswith(b'data:'):
                self.event.append(line[5:].lstrip(b' '))
            elif not line:
                if self.event:
                    self._event(b'\n'.join(self.event))
                    self.event = []
            elif line.startswith(b'{'):
                self._event(line)  # Ollama NDJSON

    async def finish(self, detail=None):
        if not self.entry or self.finished:
            return
        if self.buffer:
            self.feed(b'\n\n')
        if self.event:
            self._event(b'\n'.join(self.event))
        await quotas.settle(self.entry, self.usage if self.complete_usage else None, detail)
        self.finished = True


class ModelResponse:
    def __init__(self, response, attempt, content=None):
        self.response = response
        self.quota_attempt = attempt
        if content is not None:
            self.content = content  # Replays the bytes read while checking the stream.

    def __getattr__(self, name):
        return getattr(self.response, name)

    async def json(self, *args, **kwargs):
        data = await self.response.json(*args, **kwargs)
        self.quota_attempt.observe(data)
        await self.quota_attempt.finish(f'HTTP {self.status}' if self.status >= 400 else None)
        return data

    async def read(self):
        data = await self.response.read()
        try:
            self.quota_attempt.observe(json.loads(data))
        except (ValueError, UnicodeDecodeError):
            pass
        await self.quota_attempt.finish(f'HTTP {self.status}' if self.status >= 400 else None)
        return data

    async def text(self, *args, **kwargs):
        data = await self.response.text(*args, **kwargs)
        try:
            self.quota_attempt.observe(json.loads(data))
        except ValueError:
            pass
        await self.quota_attempt.finish(f'HTTP {self.status}' if self.status >= 400 else None)
        return data


async def model_request(
    session, request, user, *, provider='openai', model_id=None, estimated_usage=None, image_count=1, **kwargs
):
    """Only inference call sites use this; embeddings/search/file HTTP stays unmetered."""
    payload = kwargs.get('json') or kwargs.get('data') or {}
    if isinstance(payload, (bytes, str)):
        try:
            payload = json.loads(payload)
        except ValueError:
            payload = {}
    if not isinstance(payload, dict):
        payload = {}  # Multipart inference uses the explicitly supplied model ID.
    requested = model_id or getattr(getattr(request, 'state', None), 'model_id', None) or payload.get('model', '')
    # Conservative input-byte bound; reserve the requested output limit, or 1024 tokens.
    # Upstream actual usage is authoritative at settlement, including any overrun.
    estimated = len(
        json.dumps(
            payload.get('messages', payload.get('input', payload.get('prompt', ''))), ensure_ascii=False
        ).encode()
    )
    output_limit = (
        payload.get('max_completion_tokens')
        or payload.get('max_output_tokens')
        or payload.get('max_tokens')
        or (payload.get('options') or {}).get('num_predict')
        or 1024
    )
    output_limit = output_limit if isinstance(output_limit, int) and 0 < output_limit < 10_000_000 else 1024
    if not user:
        from fastapi import HTTPException

        raise HTTPException(400, 'Model requests require an attributable user')
    # Each try is its own ledger entry: a failed one settles at zero, so nothing is billed twice.
    retries = MODEL_REQUEST_RETRIES if _replayable(kwargs) else 0
    for number in range(retries + 1):
        final = number == retries
        entry = await quotas.reserve(
            user.id,
            requested,
            estimated_usage
            if estimated_usage is not None
            else {'prompt_tokens': estimated, 'completion_tokens': output_limit},
            provider=provider,
            price_model_id=payload.get('model'),
            image_count=image_count,
            payload=payload,
            headers=kwargs.get('headers'),
        )
        attempt = ModelAttempt(entry, provider)
        if entry:
            # A 307/308 must not silently resend inference outside its reservation.
            # Existing unmetered connections retain their original redirect behavior.
            kwargs['allow_redirects'] = False
        try:
            response = await session.request(**kwargs, trace_request_ctx={'quota_attempt': attempt})
        except TimeoutError:
            await asyncio.shield(attempt.finish('Upstream request timed out before a response was received.'))
            raise
        except aiohttp.ClientError as error:
            await asyncio.shield(attempt.finish('Upstream request interrupted before a response was received.'))
            if final:
                raise
            await _wait_retry(requested, number, retries, f'{type(error).__name__}: {error}')
            continue
        except BaseException:
            await asyncio.shield(attempt.finish('Upstream request interrupted before a response was received.'))
            raise
        try:
            problem, delay, result = await _check(response, attempt, provider, final)
        except BaseException:
            await asyncio.shield(attempt.finish('Upstream response interrupted.'))
            response.close()
            raise
        if problem is None:
            # Unmetered replies stay plain aiohttp responses unless the stream needs its replay.
            return result if entry or 'content' in vars(result) else response
        await asyncio.shield(attempt.finish(problem))
        response.close()
        await _wait_retry(requested, number, retries, problem, delay)


# ---------------------------------------------------------------------------
# Upstream failure checks and retries
# ---------------------------------------------------------------------------

RETRY_BASE_SECONDS = 1.0
RETRY_AFTER_LIMIT = 30.0  # A longer Retry-After goes back to the caller instead of blocking here.
PEEK_LIMIT = 1024 * 1024  # Stop holding a stream back once this much arrived without output.
INCOMPLETE = 'Upstream closed the connection before the reply finished; the reply is incomplete.'
_FINISHED = re.compile(
    rb'\[DONE\]|"(?:finish_reason|finishReason|stop_reason)"\s*:\s*"|"done"\s*:\s*true|"error"\s*:'
    rb'|"type"\s*:\s*"(?:message_stop|response\.(?:completed|incomplete|failed)|error)"'
)
_OUTPUT_KEYS = (
    'content',
    'reasoning_content',
    'reasoning',
    'thinking',
    'tool_calls',
    'function_call',
    'refusal',
    'audio',
)


def _replayable(kwargs):
    """Only bodies that can be sent again; multipart forms and streams are single-use."""
    data = kwargs.get('data')
    return data is None or isinstance(data, (bytes, str, dict))


def _retryable_status(status):
    return status in {408, 425, 429} or (status >= 500 and status not in {501, 505})


def _retry_after(headers):
    try:
        if headers.get('retry-after-ms'):
            return float(headers['retry-after-ms']) / 1000
        if headers.get('retry-after'):
            return float(headers['retry-after'])
    except (TypeError, ValueError):
        pass
    return None


async def _wait_retry(model, number, retries, reason, delay=None):
    delay = RETRY_BASE_SECONDS * 2**number if delay is None else delay
    log.warning('Model %s upstream failed (%s); retry %d/%d in %.1fs', model, reason, number + 1, retries, delay)
    await asyncio.sleep(delay)


def _classify(data):
    """'output' once a reply has visible content, 'finished' for an end with none, 'error', or None."""
    if not isinstance(data, dict):
        return None
    kind = data.get('type') or ''
    if kind in {'error', 'response.failed'} or (data.get('error') and not data.get('choices')):
        return 'error'
    if kind.endswith('.delta') and kind.startswith('response.') and data.get('delta'):
        return 'output'
    if kind == 'content_block_delta' or (
        kind == 'content_block_start' and (data.get('content_block') or {}).get('type') != 'text'
    ):
        return 'output'
    if kind in {'message_stop', 'response.completed', 'response.incomplete'}:
        return 'finished'
    if kind == 'message' and 'content' in data:  # Anthropic, non-streaming
        return 'output' if data['content'] else 'finished'
    if isinstance(data.get('output'), list):  # Responses API, non-streaming
        return 'output' if data['output'] else 'finished'
    finished = False
    for choice in data.get('choices') or []:
        if not isinstance(choice, dict):
            continue
        body = choice.get('delta') or choice.get('message') or {}
        if choice.get('text') or (isinstance(body, dict) and any(body.get(key) for key in _OUTPUT_KEYS)):
            return 'output'
        finished = finished or bool(choice.get('finish_reason'))
    for candidate in data.get('candidates') or []:  # Gemini
        if isinstance(candidate, dict) and ((candidate.get('content') or {}).get('parts')):
            return 'output'
        finished = finished or bool(isinstance(candidate, dict) and candidate.get('finishReason'))
    message = data.get('message')  # Ollama
    if isinstance(message, dict) and any(message.get(key) for key in _OUTPUT_KEYS):
        return 'output'
    if isinstance(data.get('response'), str) and data['response'] or data.get('thinking'):
        return 'output'
    if finished or data.get('done') is True:
        return 'finished'
    return None


def _line_state(line, kind):
    line = line.strip()
    if kind == 'sse':
        if line.startswith(b'event:'):
            return 'error' if line[6:].strip() == b'error' else None
        if not line.startswith(b'data:'):
            return None
        line = line[5:].strip()
        if line == b'[DONE]':
            return 'finished'
    if not line:
        return None
    try:
        return _classify(json.loads(line))
    except (ValueError, UnicodeDecodeError):
        return None


def _stream_kind(response):
    content_type = response.headers.get('Content-Type', '').lower()
    if 'text/event-stream' in content_type:
        return 'sse'
    if 'ndjson' in content_type or 'jsonl' in content_type:
        return 'ndjson'
    return 'json' if 'json' in content_type else None


async def _check(response, attempt, provider, final):
    """(problem, retry delay, response to return). Streams are held back until their first output."""
    wrapped = ModelResponse(response, attempt)
    kind = _stream_kind(response)
    if response.status >= 400:
        if final or not _retryable_status(response.status):
            return None, None, wrapped
        delay = _retry_after(response.headers)
        if delay is not None and delay > RETRY_AFTER_LIMIT:
            return None, None, wrapped
        try:
            await wrapped.read()  # Settles this try at zero with the status as its note.
        except aiohttp.ClientError:
            pass
        return f'HTTP {response.status}', delay, wrapped
    if kind in {'sse', 'ndjson'}:
        if final:
            return None, None, ModelResponse(response, attempt, ReplayContent(response.content, kind, provider))
        prefix, problem = await _peek(response.content, kind)
        if problem is None:
            return None, None, ModelResponse(response, attempt, ReplayContent(response.content, kind, provider, prefix))
        attempt.feed(prefix)
        return problem, None, wrapped
    if kind == 'json' and not final:
        try:
            body = await response.read()  # Cached: callers' json()/text() reuse it.
        except aiohttp.ClientPayloadError as error:
            return f'reply cut off ({error})', None, wrapped
        if not body.strip():
            return 'empty reply', None, wrapped
        try:
            state = _classify(json.loads(body))
        except (ValueError, UnicodeDecodeError):
            return 'unreadable reply', None, wrapped
        if state == 'error':
            return 'error in reply', None, wrapped
        if state == 'finished':
            return 'empty reply', None, wrapped
    return None, None, wrapped


async def _peek(content, kind):
    """Read until the reply shows output. Returns (bytes read, problem or None)."""
    buffer, pending = bytearray(), b''
    try:
        async for data in content.iter_any():
            buffer += data
            pending += data
            while b'\n' in pending:
                line, pending = pending.split(b'\n', 1)
                state = _line_state(line, kind)
                if state == 'output':
                    return bytes(buffer), None
                if state == 'error':
                    return bytes(buffer), 'error event before any output'
                if state == 'finished':
                    return bytes(buffer), 'empty reply'
            if len(buffer) > PEEK_LIMIT:
                return bytes(buffer), None
    except TimeoutError:
        raise
    except aiohttp.ClientError as error:
        return bytes(buffer), f'stream closed before any output ({type(error).__name__})'
    if _line_state(pending, kind) == 'output':
        return bytes(buffer), None
    return bytes(buffer), 'stream closed before any output'


class ReplayContent:
    """The stream body as aiohttp's StreamReader iterators see it: bytes already read, then the rest.

    A stream that ends without its finishing event after output began cannot be retried (the caller
    has the first part), so it gets an error event instead of ending as if complete.
    """

    def __init__(self, source, kind, provider, prefix=b''):
        self.source = source
        self.kind = kind
        self.provider = provider
        self.prefix = prefix
        self.pending = b''
        self.finished = False
        self.done = False

    def _watch(self, data):
        if self.finished:
            return
        lines = (self.pending + data).split(b'\n')
        self.pending = lines.pop()[-4096:]
        self.finished = any(_FINISHED.search(line) for line in lines)

    def _incomplete(self):
        if self.kind == 'ndjson':
            return json.dumps({'error': INCOMPLETE}).encode() + b'\n'
        if self.provider == 'anthropic':
            event = {'type': 'error', 'error': {'type': 'api_error', 'message': INCOMPLETE}}
            return b'event: error\ndata: ' + json.dumps(event).encode() + b'\n\n'
        return (
            b'data: ' + json.dumps({'error': {'message': INCOMPLETE, 'type': 'upstream_incomplete'}}).encode() + b'\n\n'
        )

    async def iter_any(self):
        if self.done:
            return
        if self.prefix:
            prefix, self.prefix = self.prefix, b''
            self._watch(prefix)
            yield prefix
        error = None
        try:
            async for data in self.source.iter_any():
                self._watch(data)
                yield data
        except aiohttp.ClientError as caught:  # Includes a read timeout mid-stream.
            error = caught
        self.done = True
        if self.pending and not self.finished:
            self.finished = bool(_FINISHED.search(self.pending))
        if not self.finished:
            log.warning('Model stream ended without finishing (%s)', error or 'connection closed')
            separator = b'\n' if self.pending else b''
            yield separator + self._incomplete()

    async def iter_chunks(self):
        async for data in self.iter_any():
            yield data, True

    async def iter_chunked(self, size):
        buffer = b''
        async for data in self.iter_any():
            buffer += data
            while len(buffer) >= size:
                yield buffer[:size]
                buffer = buffer[size:]
        if buffer:
            yield buffer

    def __aiter__(self):
        return self._lines()

    async def _lines(self):
        buffer = b''
        async for data in self.iter_any():
            buffer += data
            *lines, buffer = buffer.split(b'\n')
            for line in lines:
                yield line + b'\n'
        if buffer:
            yield buffer

    async def read(self, n=-1):
        return b''.join([data async for data in self.iter_any()])

    def at_eof(self):
        return self.done


@asynccontextmanager
async def metered_request(
    session, request, user, *, model_id, provider='openai', method='POST', image_count=1, **kwargs
):
    """Context-manager form for image/audio adapters, using the same outbound ledger.

    ``image_count`` is the requested image count, exposed to prices as New-API's ``image_count``.
    """
    from open_webui.utils.session_pool import cleanup_response

    response = await model_request(
        session, request, user, model_id=model_id, provider=provider, method=method, image_count=image_count, **kwargs
    )
    try:
        yield response
    finally:
        await cleanup_response(response)


def plugin_model_request(request, user):
    """An actor-bound, opt-in inference context for trusted admin Functions.

    Pipe/Filter/Action extensions request ``__model_request__`` in their signature.
    Each context is one real HTTP attempt, including retries and continuation
    rounds. Extensions calling generate_chat_completion already use its gate.
    """
    from types import SimpleNamespace

    if isinstance(user, dict):
        user = SimpleNamespace(id=user.get('id'))

    @asynccontextmanager
    async def infer(*, model_id, provider='openai', method='POST', **kwargs):
        from fastapi import HTTPException
        from open_webui.utils.session_pool import get_session

        if not user or not getattr(user, 'id', None):
            raise HTTPException(400, 'Plugin inference requires an attributable user')
        async with metered_request(
            await get_session(),
            request,
            user,
            model_id=model_id,
            provider=provider,
            method=method,
            **kwargs,
        ) as response:
            yield response

    return infer


class RealtimeMeter:
    """Reserve before response.create; settle response.done, retaining interrupted usage.

    Server VAD must use create_response=False so no inference can start outside
    this gate. Multiple in-flight responses are matched by response.created order.
    """

    def __init__(self, user, model_id):
        self.user = user
        self.model_id = model_id
        self.waiting = []
        self.responses = {}

    async def send(self, socket, event):
        if event.get('type') != 'response.create':
            await socket.send_json(event)
            return
        payload = event.get('response') or {}
        entry = await quotas.reserve(
            self.user.id,
            self.model_id,
            {
                'input_tokens': len(json.dumps(payload, ensure_ascii=False).encode()),
                'output_tokens': 4096,
            },
            provider='realtime',
            payload=payload,
        )
        attempt = ModelAttempt(entry, 'realtime')
        self.waiting.append(attempt)
        # Once a write is attempted, a disconnect cannot prove non-delivery.
        await attempt.sent()
        await socket.send_json(event)

    async def observe(self, event):
        response = event.get('response') or {}
        identifier = response.get('id')
        if event.get('type') == 'response.created' and self.waiting:
            self.responses[identifier] = self.waiting.pop(0)
        if event.get('type') == 'response.done':
            attempt = self.responses.pop(identifier, None)
            if attempt:
                attempt.observe(event)
                await attempt.finish()

    async def close(self):
        for attempt in [*self.waiting, *self.responses.values()]:
            await attempt.finish('Realtime response ended without complete usage.')
        self.waiting.clear()
        self.responses.clear()
