"""Meter actual model HTTP attempts and observe usage without changing response bytes."""

import asyncio
import json
from contextlib import asynccontextmanager

from open_webui.utils import quotas


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
    def __init__(self, response, attempt):
        self.response = response
        self.quota_attempt = attempt

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
        return ModelResponse(response, attempt) if entry else response
    except BaseException:
        await asyncio.shield(attempt.finish('Upstream request interrupted before a response was received.'))
        raise


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
