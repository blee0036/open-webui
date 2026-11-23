"""Real local HTTP attempts; no external model or credentials are used."""

import json
from types import SimpleNamespace

import pytest
import pytest_asyncio
from aiohttp import web
from open_webui.internal.db import get_async_db
from open_webui.models.config import Config
from open_webui.models.team import ModelPrice, QuotaEntry, QuotaPlan
from open_webui.utils.quota_transport import ModelAttempt, RealtimeMeter, model_request, plugin_model_request
from open_webui.utils.realtime_transcription import RealtimeAudioSegments, transcribe_segment
from open_webui.utils.quotas import quota_summary
from open_webui.utils.session_pool import cleanup_response, close_session, get_session, stream_wrapper
from sqlalchemy import select


@pytest_asyncio.fixture
async def model_server():
    received = []

    async def infer(request):
        body = await request.read()
        received.append(body)
        if request.path == '/redirect':
            raise web.HTTPTemporaryRedirect(location='/json')
        if request.path.startswith('/invalid-usage/'):
            value = {'null': None, 'false': False, 'empty': '', 'zero': 0}[request.path.rsplit('/', 1)[1]]
            return web.json_response({'usage': {'input_tokens': value, 'output_tokens': 0}})
        if request.path.endswith('/decisions'):
            result = {
                'answers': [
                    {'type': 'predicate', 'name': item['name'], 'probability': 0.9}
                    for item in json.loads(body)['questions']
                ]
            }
            if request.path != '/no-usage/decisions':
                result['usage'] = {'input_tokens': 12, 'output_tokens': 3}
            return web.json_response(result)
        if request.path == '/audio/transcriptions':
            return web.json_response({'text': 'test speech', 'usage': {'input_tokens': 20, 'output_tokens': 2}})
        if request.path == '/stream':
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            event = b'data: {"usage":{"prompt_tokens":12,"completion_tokens":3}}\n\n'
            for chunk in (event[:11], event[11:31], event[31:], b'data: [DONE]\n\n'):
                await response.write(chunk)
            return response
        if request.path == '/binary':
            return web.Response(body=b'ID3test-audio')
        if request.path == '/search-model':
            return web.json_response(
                {
                    'usage': {'prompt_tokens': 12, 'completion_tokens': 3},
                    'citations': ['https://example.com/source'],
                    'choices': [{'message': {'content': 'Local search test'}}],
                }
            )
        return web.json_response({'usage': {'prompt_tokens': 12, 'completion_tokens': 3}})

    app = web.Application()
    app.router.add_post('/{kind:.*}', infer)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f'http://127.0.0.1:{port}', received
    finally:
        await close_session()
        await runner.cleanup()


async def enable(unit='requests'):
    async with get_async_db() as db:
        db.add(
            QuotaPlan(
                id='default',
                name='Default',
                unit=unit,
                allowance=10000,
                reset={'kind': 'month', 'timezone': 'Asia/Singapore'},
                created_at=0,
                updated_at=0,
            )
        )
        db.add(ModelPrice(model_id='test', expression='p+c', multiplier='1', revision=1, updated_at=0))
        await db.commit()
    await Config.upsert({'team.quota.enabled': True, 'team.quota.default_plan_id': 'default'})


@pytest.mark.asyncio
async def test_real_attempts_retry_and_tool_continuation(model_server):
    await enable()
    url, received = model_server
    session = await get_session()
    user = SimpleNamespace(id='actor')
    for kind in ('json', 'stream', 'json'):  # first request, tool continuation, retry
        response = await model_request(
            session, None, user, method='POST', url=f'{url}/{kind}', json={'model': 'test', 'messages': []}
        )
        if kind == 'stream':
            chunks = [chunk async for chunk in stream_wrapper(response)]
            assert b'"prompt_tokens":12' in b''.join(chunks)
        else:
            assert (await response.json())['usage']['completion_tokens'] == 3
        await cleanup_response(response)
    summary = await quota_summary('actor')
    assert len(received) == 3 and summary['period']['used'] == 3
    assert summary['period']['reserved'] == 0


@pytest.mark.asyncio
async def test_binary_without_usage_and_json_read_settlement(model_server):
    await enable('credits')
    url, _ = model_server
    session = await get_session()
    user = SimpleNamespace(id='creator')
    response = await model_request(session, None, user, model_id='test', method='POST', url=url + '/binary')
    assert await response.read() == b'ID3test-audio'
    await cleanup_response(response)
    response = await model_request(session, None, user, method='POST', url=url + '/json', json={'model': 'test'})
    assert json.loads(await response.read())['usage']['prompt_tokens'] == 12
    await cleanup_response(response)
    summary = await quota_summary('creator')
    # The binary response reported no usage, so it settled at zero and released its hold.
    assert summary['period']['used'] == 15 and summary['period']['reserved'] == 0
    async with get_async_db() as db:
        rows = (await db.execute(select(QuotaEntry).where(QuotaEntry.user_id == 'creator'))).scalars().all()
        assert {row.state for row in rows} == {'settled'}


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['null', 'false', 'empty', 'zero'])
async def test_invalid_usage_settles_at_zero(model_server, kind):
    await enable('credits')
    url, received = model_server
    response = await model_request(
        await get_session(),
        None,
        SimpleNamespace(id='usage-check'),
        model_id='test',
        method='POST',
        url=url + '/invalid-usage/' + kind,
    )
    await response.json()
    await cleanup_response(response)
    summary = await quota_summary('usage-check')
    assert len(received) == 1 and summary['period']['used'] == 0
    async with get_async_db() as db:
        entry = (await db.execute(select(QuotaEntry))).scalar_one()
        assert entry.state == 'settled' and entry.charged == 0 and summary['period']['reserved'] == 0


@pytest.mark.asyncio
async def test_metered_requests_do_not_silently_resend_on_redirect(model_server):
    url, received = model_server
    user = SimpleNamespace(id='redirect-actor')
    # Preserve the original connection contract when enforcement is disabled.
    legacy = await model_request(await get_session(), None, user, model_id='test', method='POST', url=url + '/redirect')
    assert legacy.status == 200 and len(received) == 2
    await cleanup_response(legacy)
    await enable()
    response = await model_request(
        await get_session(),
        None,
        user,
        model_id='test',
        method='POST',
        url=url + '/redirect',
        allow_redirects=True,
    )
    assert response.status == 307 and len(received) == 3
    await cleanup_response(response)
    assert (await quota_summary(user.id))['period']['used'] == 1


def test_provider_usage_completion_and_multichunk_anthropic():
    attempt = ModelAttempt({'id': 'test'}, 'anthropic')
    attempt.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":8,"output_tokens":0}}}\n\n')
    assert not attempt.complete_usage
    attempt.feed(b'data: {"type":"message_delta","usage":{"output_tokens":4}}\n\n')
    assert attempt.complete_usage and attempt.usage == {'input_tokens': 8, 'output_tokens': 4}
    other = ModelAttempt({'id': 'other'}, 'openai')
    other.observe({'usage': {'prompt_tokens': 3}})
    assert not other.complete_usage


@pytest.mark.asyncio
async def test_realtime_counts_each_response_and_settles_interruption_at_zero():
    await enable('credits')

    class Socket:
        async def send_json(self, event):
            pass

    meter = RealtimeMeter(SimpleNamespace(id='voice-user'), 'test')
    await meter.send(Socket(), {'type': 'response.create', 'response': {}})
    await meter.observe({'type': 'response.created', 'response': {'id': 'one'}})
    await meter.observe(
        {'type': 'response.done', 'response': {'id': 'one', 'usage': {'input_tokens': 5, 'output_tokens': 7}}}
    )
    await meter.send(Socket(), {'type': 'response.create', 'response': {}})
    await meter.close()
    summary = await quota_summary('voice-user')
    assert summary['period']['used'] == 12 and summary['period']['reserved'] == 0


def test_realtime_vad_segments_are_bounded_and_byte_exact():
    import base64

    segments = RealtimeAudioSegments()
    pcm = bytes(range(240)) * 400
    segments.command({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(pcm).decode()})
    segments.observe({'type': 'input_audio_buffer.speech_started', 'item_id': 'one', 'audio_start_ms': 250})
    segments.observe({'type': 'input_audio_buffer.speech_stopped', 'item_id': 'one', 'audio_end_ms': 1500})
    item, audio = segments.observe({'type': 'input_audio_buffer.committed', 'item_id': 'one'})
    assert item == 'one' and audio == pcm[250 * 48 : 1500 * 48]
    segments.command({'type': 'input_audio_buffer.clear'})
    segments.command({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(pcm).decode()})
    segments.command({'type': 'input_audio_buffer.commit'})
    assert segments.observe({'type': 'input_audio_buffer.committed', 'item_id': 'two'})[1] == pcm
    for _ in range(100):
        segments.command({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(pcm).decode()})
    assert len(segments.audio) <= segments.MAX_BYTES
    with pytest.raises(ValueError, match='unavailable'):
        segments.observe({'type': 'input_audio_buffer.speech_started', 'item_id': 'old', 'audio_start_ms': 0})


@pytest.mark.asyncio
async def test_realtime_transcription_is_gated_before_http_and_counted(model_server):
    from fastapi import HTTPException
    from open_webui.models.team import QuotaPeriod
    from sqlalchemy import update

    await enable()
    url, received = model_server
    config = {'audio.realtime.openai.api_base_url': url, 'audio.realtime.transcription_model': 'test'}
    user = SimpleNamespace(id='voice-asr')
    assert await transcribe_segment(None, user, config, {}, b'\0\0' * 24000) == 'test speech'
    assert len(received) == 1 and (await quota_summary(user.id))['period']['used'] == 1
    async with get_async_db() as db:
        await db.execute(update(QuotaPeriod).where(QuotaPeriod.user_id == user.id).values(used=10000))
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await transcribe_segment(None, user, config, {}, b'\0\0' * 24000)
    assert error.value.status_code == 429 and len(received) == 1


@pytest.mark.asyncio
async def test_search_inference_and_plugin_attempts_use_the_actor_ledger(model_server, monkeypatch):
    from open_webui.retrieval.web import perplexity

    await enable('credits')
    url, received = model_server
    user = SimpleNamespace(id='search-and-plugin')
    monkeypatch.setattr(perplexity, 'API_URL', url + '/search-model')
    results = await perplexity.search_perplexity_metered(None, user, 'local-only', 'query', 3, model='test')
    assert results[0].link == 'https://example.com/source'
    assert results[0].snippet == 'Local search test'
    infer = plugin_model_request(None, user)
    for _ in range(2):  # A plugin retry/continuation is a separate outbound attempt.
        async with infer(model_id='test', url=url + '/json', json={'model': 'test'}) as response:
            await response.json()
    summary = await quota_summary(user.id)
    assert len(received) == 3 and summary['period']['used'] == 45
    assert summary['period']['reserved'] == 0


@pytest.mark.asyncio
async def test_decisions_search_batches_and_auto_review_charge_actual_actor(model_server, monkeypatch):
    from fastapi import HTTPException
    from open_webui.models.team import QuotaPeriod
    from open_webui.utils import tool_search
    from open_webui.utils.tool_permissions import requires_tool_approval
    from sqlalchemy import update

    await enable('credits')
    url, received = model_server
    await Config.upsert(
        {
            'chat.tool_search.engine': 'decisions',
            'chat.tool_search.decisions.api_base_url': url,
            'chat.tool_search.decisions.api_key': 'local-only',
            'chat.tool_search.decisions.model': 'test',
            'chat.tool_search.decisions.threshold': 0.5,
            'chat.tool_search.decisions.prompt': '',
            'chat.tool_permissions.enable': True,
            'chat.tool_permissions.auto.enable': True,
            'chat.tool_permissions.auto.engine': 'decisions',
            'chat.tool_permissions.auto.decisions.api_base_url': url,
            'chat.tool_permissions.auto.decisions.api_key': 'local-only',
            'chat.tool_permissions.auto.decisions.model': 'test',
            'chat.tool_permissions.auto.decisions.threshold': 0.5,
            'chat.tool_permissions.auto.prompt': '',
        }
    )
    monkeypatch.setattr(tool_search, 'DECISIONS_BATCH_SIZE', 1)
    user = SimpleNamespace(id='decisions-actor')
    request = SimpleNamespace(state=SimpleNamespace(model_id='main-chat-model'))
    candidates = {'first': {'name': 'first'}, 'second': {'name': 'second'}}
    assert await tool_search.rank_deferred_tools('find tools', candidates, request=request, user=user) == [
        'first',
        'second',
    ]
    metadata = {'params': {'tool_approval_mode': 'auto'}}
    call = {'name': 'first'}
    tool = {'type': 'builtin', 'spec': candidates['first']}
    assert await requires_tool_approval({}, metadata, call, tool, {}, request=request, user=user)
    assert call['permission_review']['engine'] == 'decisions'
    summary = await quota_summary(user.id)
    assert len(received) == 3 and summary['period']['used'] == 45
    assert summary['period']['reserved'] == 0
    async with get_async_db() as db:
        rows = (await db.execute(select(QuotaEntry))).scalars().all()
        assert all(row.user_id == user.id and row.model_id == 'test' for row in rows)
        await db.execute(update(QuotaPeriod).where(QuotaPeriod.user_id == user.id).values(used=10000))
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await tool_search.rank_deferred_tools('another query', candidates, request=request, user=user)
    assert error.value.status_code == 429 and len(received) == 3
    # An unavailable model reviewer falls back to human approval, never execution.
    blocked_call = {'name': 'second'}
    assert await requires_tool_approval({}, metadata, blocked_call, tool, {}, request=request, user=user)
    assert blocked_call['permission_review']['reason'] == 'review_unavailable'
    assert len(received) == 3


@pytest.mark.asyncio
async def test_decisions_missing_usage_settles_at_zero_and_legacy_actorless_contract(model_server):
    from fastapi import HTTPException
    from open_webui.utils.tool_search import post_decisions

    url, received = model_server
    payload = {'model': 'test', 'input': 'query', 'questions': [{'name': 'read_file'}]}
    assert (await post_decisions(payload, url, ''))['answers'][0]['name'] == 'read_file'
    await enable('credits')
    with pytest.raises(HTTPException) as error:
        await post_decisions(payload, url, '')
    assert error.value.status_code == 400 and len(received) == 1
    await post_decisions(payload, url + '/no-usage', '', user=SimpleNamespace(id='pending-decisions'))
    summary = await quota_summary('pending-decisions')
    assert len(received) == 2 and summary['period']['used'] == 0 and summary['period']['reserved'] == 0
    async with get_async_db() as db:
        row = (await db.execute(select(QuotaEntry).where(QuotaEntry.user_id == 'pending-decisions'))).scalar_one()
        assert row.state == 'settled' and row.charged == 0 and row.model_id == 'test'


@pytest.mark.asyncio
async def test_admin_ollama_unload_is_not_an_inference_attempt(model_server, monkeypatch):
    from unittest.mock import AsyncMock

    from open_webui.routers import ollama

    await enable()
    url, received = model_server
    await Config.upsert({'ollama.base_urls': [url], 'ollama.api_configs': {}})
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(OLLAMA_MODELS={'test': {'urls': [0]}})))
    user = SimpleNamespace(id='admin', role='admin')
    monkeypatch.setattr(ollama, 'get_all_models', AsyncMock())
    monkeypatch.setattr(ollama, 'clear_models_cache', AsyncMock())
    monkeypatch.setattr(ollama, 'get_headers_and_cookies', AsyncMock(return_value=({}, {})))
    assert await ollama.unload_model(request, ollama.ModelNameForm(model='test'), user) == {'status': True}
    assert len(received) == 1 and (await quota_summary(user.id))['period']['used'] == 0
    await ollama.send_request(url + '/api/generate', user=user, payload='{"model":"test","prompt":""}')
    assert len(received) == 2 and (await quota_summary(user.id))['period']['used'] == 1


def test_realtime_multiple_manual_commits_and_inflight_clear_preserve_exact_audio():
    import base64

    segments = RealtimeAudioSegments()
    chunks = [bytes([i, 0]) * 24000 for i in range(1, 4)]
    for pcm in chunks[:2]:
        segments.command({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(pcm).decode()})
        segments.command({'type': 'input_audio_buffer.commit'})
    # Clear arrives before the provider acknowledges both earlier commits.
    segments.command({'type': 'input_audio_buffer.clear'})
    for i, pcm in enumerate(chunks[:2]):
        assert segments.observe({'type': 'input_audio_buffer.committed', 'item_id': str(i)})[1] == pcm
    segments.command({'type': 'input_audio_buffer.append', 'audio': base64.b64encode(chunks[2]).decode()})
    segments.observe({'type': 'input_audio_buffer.speech_started', 'item_id': 'vad', 'audio_start_ms': 2000})
    segments.observe({'type': 'input_audio_buffer.speech_stopped', 'item_id': 'vad', 'audio_end_ms': 3000})
    assert segments.observe({'type': 'input_audio_buffer.committed', 'item_id': 'vad'})[1] == chunks[2]
